param(
  [string]$PolicyPath = (Join-Path $PSScriptRoot "embedded_harness_policy.json"),
  [string]$RepoRoot = "",
  [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
$issues = @()
$warnings = @()
$policy = $null

function ConvertTo-Array($value) {
  if ($null -eq $value) { return @() }
  if ($value -is [System.Array]) { return @($value) }
  return @($value)
}

function Add-Issue([string]$issue) {
  $script:issues += $issue
}

function Add-Warning([string]$warning) {
  $script:warnings += $warning
}

function Get-RelativeDisplayPath([string]$Path, [string]$Root) {
  try {
    $resolvedPath = (Resolve-Path -LiteralPath $Path).Path
    $resolvedRoot = (Resolve-Path -LiteralPath $Root).Path
    if ($resolvedPath.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
      return $resolvedPath.Substring($resolvedRoot.Length).TrimStart('\','/')
    }
  } catch {
  }
  return $Path
}

function Get-ObjectPropertyValue($Object, [string]$Name) {
  if ($null -eq $Object) { return $null }
  $property = $Object.PSObject.Properties[$Name]
  if ($null -eq $property) { return $null }
  return $property.Value
}

function Test-JsonBeliefInvariant($Node, [string]$Source, [string]$Root) {
  if ($null -eq $Node) { return }
  if ($Node -is [System.Array]) {
    foreach ($item in $Node) { Test-JsonBeliefInvariant $item $Source $Root }
    return
  }
  if ($Node -isnot [pscustomobject]) { return }

  $beliefStatus = Get-ObjectPropertyValue $Node "belief_status"
  $traceSummary = Get-ObjectPropertyValue $Node "belief_trace_summary"
  if (($null -ne $beliefStatus) -and ($null -ne $traceSummary)) {
    $currentStatus = Get-ObjectPropertyValue $traceSummary "current_status"
    $relative = Get-RelativeDisplayPath $Source $Root
    if ($null -eq $currentStatus) {
      Add-Issue "belief_trace_summary_current_status_missing:$relative"
    } elseif ([string]$currentStatus -ne [string]$beliefStatus) {
      Add-Issue "belief_trace_current_status_mismatch:${relative}:${beliefStatus}!=$currentStatus"
    }
  }

  foreach ($property in $Node.PSObject.Properties) {
    Test-JsonBeliefInvariant $property.Value $Source $Root
  }
}

function Test-TextBeliefInvariant([string]$Path, [string]$Root) {
  $relative = Get-RelativeDisplayPath $Path $Root
  $lines = Get-Content -LiteralPath $Path -Encoding UTF8
  $beliefStatus = $null
  $sawSummary = $false
  $lineNumber = 0
  foreach ($line in $lines) {
    $lineNumber += 1
    if ($line -match '^\s*belief_status:\s*`?([^`\s#]+)`?') {
      $beliefStatus = $Matches[1].Trim()
      $sawSummary = $false
      continue
    }
    if ($null -eq $beliefStatus) { continue }
    if ($line -match '^\s*belief_trace_summary:\s*') {
      $sawSummary = $true
      continue
    }
    if ($sawSummary -and ($line -match '^\s*current_status:\s*`?([^`\s#]+)`?')) {
      $currentStatus = $Matches[1].Trim()
      if ($currentStatus -ne $beliefStatus) {
        Add-Issue "belief_trace_current_status_mismatch:${relative}:${lineNumber}:${beliefStatus}!=$currentStatus"
      }
      $beliefStatus = $null
      $sawSummary = $false
    }
  }
  if (($null -ne $beliefStatus) -and $sawSummary) {
    Add-Issue "belief_trace_summary_current_status_missing:$relative"
  }
}

function Test-BeliefTraceInvariants([string]$Root) {
  if ([string]::IsNullOrWhiteSpace($Root) -or -not (Test-Path -LiteralPath $Root)) {
    return
  }
  $resolvedRoot = (Resolve-Path -LiteralPath $Root).Path
  $textExtensions = @(".md", ".yaml", ".yml")
  $jsonExtensions = @(".json", ".jsonl")
  $scanRoots = @("docs", "examples", "templates", "skills", "AGENTS.md", "README.md")
  foreach ($entry in $scanRoots) {
    $entryPath = Join-Path $resolvedRoot $entry
    if (-not (Test-Path -LiteralPath $entryPath)) { continue }
    $files = if ((Get-Item -LiteralPath $entryPath).PSIsContainer) {
      Get-ChildItem -LiteralPath $entryPath -Recurse -File
    } else {
      @(Get-Item -LiteralPath $entryPath)
    }
    foreach ($file in $files) {
      if ($textExtensions -contains $file.Extension.ToLowerInvariant()) {
        Test-TextBeliefInvariant $file.FullName $resolvedRoot
      } elseif ($jsonExtensions -contains $file.Extension.ToLowerInvariant()) {
        try {
          if ($file.Extension.ToLowerInvariant() -eq ".jsonl") {
            foreach ($line in (Get-Content -LiteralPath $file.FullName -Encoding UTF8)) {
              if ([string]::IsNullOrWhiteSpace($line)) { continue }
              Test-JsonBeliefInvariant ($line | ConvertFrom-Json) $file.FullName $resolvedRoot
            }
          } else {
            Test-JsonBeliefInvariant ((Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8) | ConvertFrom-Json) $file.FullName $resolvedRoot
          }
        } catch {
          Add-Issue "belief_invariant_json_parse_failed:$(Get-RelativeDisplayPath $file.FullName $resolvedRoot)"
        }
      }
    }
  }
}

try {
  if (-not (Test-Path -LiteralPath $PolicyPath)) {
    Add-Issue "policy_file_missing"
  } else {
    $policy = Get-Content -LiteralPath $PolicyPath -Raw -Encoding UTF8 | ConvertFrom-Json
  }
} catch {
  Add-Issue "json_parse_failed"
}

if ($null -ne $policy) {
  $riskRules = $policy.risk_trigger_rules
  if ($null -eq $riskRules) {
    $riskRules = $policy.risk_keyword_rules
  }

  if ($null -eq $riskRules) {
    Add-Issue "risk_trigger_rules_missing"
  } else {
    foreach ($risk in @("R0","R1","R2","R3","R4","R5")) {
      if (-not ($riskRules.PSObject.Properties.Name -contains $risk)) {
        Add-Issue "risk_rule_missing:$risk"
      }
    }
  }

  $r5ContextRules = $policy.r5_context_decision_rules
  if ($null -eq $r5ContextRules) {
    Add-Issue "r5_context_decision_rules_missing"
  } else {
    foreach ($field in @("direct_action_terms","context_required_candidate_terms","always_action_candidate_terms","action_context_terms","non_action_context_terms","documentation_context_terms")) {
      $value = Get-ObjectPropertyValue $r5ContextRules $field
      if ((ConvertTo-Array $value).Count -eq 0) {
        Add-Issue "r5_context_decision_rule_empty:$field"
      }
    }
  }

  if ($null -eq $policy.memory_roots) {
    Add-Issue "memory_roots_missing"
  } else {
    $invalidPathChars = [System.IO.Path]::GetInvalidPathChars()
    foreach ($lane in $policy.memory_roots.PSObject.Properties) {
      foreach ($path in (ConvertTo-Array $lane.Value)) {
        $pathText = [string]$path
        if ([string]::IsNullOrWhiteSpace($pathText)) {
          Add-Issue "memory_root_empty:$($lane.Name)"
          continue
        }
        foreach ($char in $invalidPathChars) {
          if ($pathText.Contains([string]$char)) {
            Add-Issue "memory_root_invalid_path_char:$($lane.Name)"
            break
          }
        }
      }
    }
  }

  $routerContract = $policy.router_decision_contract
  if ($null -eq $routerContract) {
    Add-Issue "router_decision_contract_missing"
  } else {
    $actionBindingContract = Get-ObjectPropertyValue $routerContract "action_binding_contract"
    if ($null -eq $actionBindingContract) {
      Add-Issue "action_binding_contract_missing"
    } else {
      if ((Get-ObjectPropertyValue $actionBindingContract "binding_mode") -ne "inline_receipt_no_extra_tool_call") {
        Add-Issue "action_binding_contract_mode_invalid"
      }
      $softPredictionReviews = [int](Get-ObjectPropertyValue $actionBindingContract "soft_target_prediction_reviews")
      $softNextActions = [int](Get-ObjectPropertyValue $actionBindingContract "soft_target_next_actions")
      if (($softPredictionReviews -lt 1) -or ($softPredictionReviews -gt 12)) {
        Add-Issue "action_binding_contract_prediction_review_soft_target_invalid"
      }
      if (($softNextActions -lt 1) -or ($softNextActions -gt 32)) {
        Add-Issue "action_binding_contract_next_action_soft_target_invalid"
      }
      if ((Get-ObjectPropertyValue $actionBindingContract "coverage_expansion_allowed") -ne $true) {
        Add-Issue "action_binding_contract_coverage_expansion_disabled"
      }
      if (($null -ne (Get-ObjectPropertyValue $actionBindingContract "max_prediction_reviews")) -or ($null -ne (Get-ObjectPropertyValue $actionBindingContract "max_next_actions"))) {
        Add-Issue "action_binding_contract_legacy_hard_cap_present"
      }
      foreach ($field in @("profile_values","prediction_review_profile_values","prediction_review_source_fields","next_action_values","completion_evidence_values")) {
        if ((ConvertTo-Array (Get-ObjectPropertyValue $actionBindingContract $field)).Count -eq 0) {
          Add-Issue "action_binding_contract_field_empty:$field"
        }
      }
    }
    $taskContinuityContract = Get-ObjectPropertyValue $routerContract "task_continuity_contract"
    if ($null -eq $taskContinuityContract) {
      Add-Issue "task_continuity_contract_missing"
    } else {
      if ([string](Get-ObjectPropertyValue $taskContinuityContract "schema") -ne "cbh.task_continuity_contract.v1") {
        Add-Issue "task_continuity_contract_schema_invalid"
      }
      if ((Get-ObjectPropertyValue $taskContinuityContract "short_answer_default") -ne "dormant") {
        Add-Issue "task_continuity_short_answer_default_invalid"
      }
      if ((Get-ObjectPropertyValue $taskContinuityContract "state_storage") -ne "persistent_tasks_local_cumcwork_only") {
        Add-Issue "task_continuity_state_storage_invalid"
      }
      if ((Get-ObjectPropertyValue $taskContinuityContract "authority_granted") -ne $false) {
        Add-Issue "task_continuity_authority_invalid"
      }
    }
    $engineeringExecutionContract = Get-ObjectPropertyValue $routerContract "engineering_execution_contract"
    if ($null -eq $engineeringExecutionContract) {
      Add-Issue "engineering_execution_contract_missing"
    } else {
      if ([string](Get-ObjectPropertyValue $engineeringExecutionContract "schema") -ne "cbh.engineering_execution_contract.v1") {
        Add-Issue "engineering_execution_contract_schema_invalid"
      }
      if ((Get-ObjectPropertyValue $engineeringExecutionContract "state_storage") -ne "none") {
        Add-Issue "engineering_execution_state_storage_invalid"
      }
      if ((Get-ObjectPropertyValue $engineeringExecutionContract "authority_granted") -ne $false) {
        Add-Issue "engineering_execution_authority_invalid"
      }
      foreach ($profile in @("tracer_bullet_plan","deep_module_review","skill_invocation_topology","adapter_seam_review")) {
        $triggers = Get-ObjectPropertyValue (Get-ObjectPropertyValue $engineeringExecutionContract "profile_triggers") $profile
        if ((ConvertTo-Array $triggers).Count -eq 0) {
          Add-Issue "engineering_execution_profile_triggers_missing:$profile"
        }
      }
    }
    if (-not ((ConvertTo-Array (Get-ObjectPropertyValue $routerContract "receipt_fields")) -contains "task_continuity_decision")) {
      Add-Issue "task_continuity_receipt_field_missing"
    }
    if (-not ((ConvertTo-Array (Get-ObjectPropertyValue $routerContract "receipt_fields")) -contains "engineering_execution_profiles")) {
      Add-Issue "engineering_execution_receipt_field_missing"
    }
    foreach ($moduleValue in @("task_continuity","engineering_execution")) {
      if (-not ((ConvertTo-Array (Get-ObjectPropertyValue $routerContract "module_need_values")) -contains $moduleValue)) {
        Add-Issue "module_need_value_missing:$moduleValue"
      }
    }
    foreach ($actionValue in @("prepare_task_continuity_capsule","apply_engineering_execution_profile")) {
      if (-not ((ConvertTo-Array (Get-ObjectPropertyValue $actionBindingContract "next_action_values")) -contains $actionValue)) {
        Add-Issue "action_binding_value_missing:$actionValue"
      }
    }
    $reflexiveGapContract = Get-ObjectPropertyValue $routerContract "reflexive_gap_contract"
    if ($null -eq $reflexiveGapContract) {
      Add-Issue "reflexive_gap_contract_missing"
    } else {
      if ((Get-ObjectPropertyValue $reflexiveGapContract "enabled") -ne $true) {
        Add-Issue "reflexive_gap_contract_disabled"
      }
      foreach ($field in @("required_gate","semantic_marker")) {
        if ([string]::IsNullOrWhiteSpace([string](Get-ObjectPropertyValue $reflexiveGapContract $field))) {
          Add-Issue "reflexive_gap_contract_field_empty:$field"
        }
      }
      foreach ($field in @("explicit_request_triggers","exclusion_triggers")) {
        if ((ConvertTo-Array (Get-ObjectPropertyValue $reflexiveGapContract $field)).Count -eq 0) {
          Add-Issue "reflexive_gap_contract_field_empty:$field"
        }
      }
      $reflexiveGroups = @{
        knowledge_action_groups = @("knowledge","execution","contrast")
        goal_fidelity_groups = @("goal","proxy_or_stall")
        counterevidence_groups = @("attribution","unverified")
        knowledge_coverage_groups = @("unmodeled","high_impact","uncertainty")
      }
      foreach ($groupName in $reflexiveGroups.Keys) {
        $group = Get-ObjectPropertyValue $reflexiveGapContract $groupName
        if ($null -eq $group) {
          Add-Issue "reflexive_gap_contract_group_missing:$groupName"
          continue
        }
        foreach ($facet in $reflexiveGroups[$groupName]) {
          if ((ConvertTo-Array (Get-ObjectPropertyValue $group $facet)).Count -eq 0) {
            Add-Issue "reflexive_gap_contract_group_empty:${groupName}:$facet"
          }
        }
      }
    }
    $fullLaneConfig = Get-ObjectPropertyValue $routerContract "conversation_memory_full_lane_triggers"
    $thresholdGroups = Get-ObjectPropertyValue $fullLaneConfig "threshold_groups"
    if ($null -eq $thresholdGroups) {
      Add-Warning "conversation_memory_full_lane_triggers_missing"
    } else {
      foreach ($group in $thresholdGroups.PSObject.Properties) {
        $threshold = Get-ObjectPropertyValue $group.Value "threshold"
        $triggers = ConvertTo-Array (Get-ObjectPropertyValue $group.Value "triggers")
        $thresholdInt = 0
        try {
          if ($null -ne $threshold) { $thresholdInt = [int]$threshold }
        } catch {
          $thresholdInt = 0
        }
        if ($thresholdInt -lt 1) {
          Add-Issue "conversation_full_lane_group_invalid_threshold:$($group.Name)"
        }
        if ($triggers.Count -eq 0) {
          Add-Issue "conversation_full_lane_group_empty_triggers:$($group.Name)"
        }
      }
    }
    $referencedConversationContract = Get-ObjectPropertyValue $routerContract "referenced_conversation_memory_contract"
    if ($null -eq $referencedConversationContract) {
      Add-Issue "referenced_conversation_memory_contract_missing"
    } else {
      if ((Get-ObjectPropertyValue $referencedConversationContract "enabled") -ne $true) {
        Add-Issue "referenced_conversation_memory_contract_disabled"
      }
      $registryName = [string](Get-ObjectPropertyValue $referencedConversationContract "registry_path")
      $registryPath = Join-Path (Split-Path -Parent $PolicyPath) $registryName
      if (-not (Test-Path -LiteralPath $registryPath -PathType Leaf)) {
        Add-Issue "active_conversation_memory_registry_missing:$registryName"
      } else {
        try {
          $registry = Get-Content -LiteralPath $registryPath -Raw -Encoding UTF8 | ConvertFrom-Json
          $expectedSchema = [string](Get-ObjectPropertyValue $referencedConversationContract "registry_schema")
          if ([string](Get-ObjectPropertyValue $registry "schema") -ne $expectedSchema) {
            Add-Issue "active_conversation_memory_registry_schema_mismatch"
          }
          $entries = @(ConvertTo-Array (Get-ObjectPropertyValue $registry "entries"))
          if ($entries.Count -eq 0) {
            if ((Get-ObjectPropertyValue $registry "configured") -eq $false) {
              Add-Warning "active_conversation_memory_registry_template_unconfigured"
            } else {
              Add-Issue "active_conversation_memory_registry_empty"
            }
          }
          foreach ($entry in $entries) {
            foreach ($field in @("registry_id","memory_id","state","root_path","meta_path","ledger_root_path","ledger_index_path")) {
              if ([string]::IsNullOrWhiteSpace([string](Get-ObjectPropertyValue $entry $field))) {
                Add-Issue "active_conversation_memory_registry_field_empty:$field"
              }
            }
            foreach ($field in @("root_path","meta_path","ledger_root_path","ledger_index_path")) {
              $entryPath = [string](Get-ObjectPropertyValue $entry $field)
              if ((-not [string]::IsNullOrWhiteSpace($entryPath)) -and (-not (Test-Path -LiteralPath $entryPath))) {
                Add-Issue "active_conversation_memory_registry_target_missing:$field"
              }
            }
          }
        } catch {
          Add-Issue "active_conversation_memory_registry_parse_failed"
        }
      }
    }
  }

  $runtimeEnforcement = $policy.runtime_enforcement
  if ($null -eq $runtimeEnforcement) {
    Add-Issue "runtime_enforcement_missing"
  } else {
    $obsoletePermit = Get-ObjectPropertyValue $runtimeEnforcement "human_confirmation_permit"
    if ($null -ne $obsoletePermit) {
      Add-Issue "obsolete_runtime_blocking_field_present:human_confirmation_permit"
    }
    if (@(ConvertTo-Array (Get-ObjectPropertyValue $runtimeEnforcement "hard_tool_patterns")).Count -gt 0) {
      Add-Issue "obsolete_runtime_blocking_field_present:hard_tool_patterns"
    }
    if ([bool](Get-ObjectPropertyValue $runtimeEnforcement "mandatory")) {
      Add-Issue "runtime_enforcement_must_not_be_mandatory"
    }
    if (@(ConvertTo-Array (Get-ObjectPropertyValue $runtimeEnforcement "entry_scripts")).Count -gt 0) {
      Add-Issue "runtime_blocking_entry_scripts_must_be_empty"
    }
    if (@(ConvertTo-Array (Get-ObjectPropertyValue $runtimeEnforcement "hard_block_conditions")).Count -gt 0) {
      Add-Issue "runtime_hard_block_conditions_must_be_empty"
    }

    $deleteAdvisor = Get-ObjectPropertyValue $runtimeEnforcement "dangerous_delete_advisory_contract"
    if ($null -eq $deleteAdvisor) {
      Add-Issue "dangerous_delete_advisory_contract_missing"
    } else {
      if ((Get-ObjectPropertyValue $deleteAdvisor "schema") -ne "cbh.dangerous_delete_advisory.v1") {
        Add-Issue "dangerous_delete_advisory_contract_schema_invalid"
      }
      if (-not [bool](Get-ObjectPropertyValue $deleteAdvisor "enabled")) {
        Add-Issue "dangerous_delete_advisory_contract_not_enabled"
      }
      if ((Get-ObjectPropertyValue $deleteAdvisor "invocation_mode") -ne "direct_on_demand_only") {
        Add-Issue "dangerous_delete_advisory_invocation_mode_invalid"
      }
      if (@(ConvertTo-Array (Get-ObjectPropertyValue $deleteAdvisor "registered_hook_events")).Count -gt 0) {
        Add-Issue "dangerous_delete_advisory_must_not_register_hook_events"
      }
      foreach ($field in @("blocking", "stateful")) {
        if ([bool](Get-ObjectPropertyValue $deleteAdvisor $field)) {
          Add-Issue "dangerous_delete_advisory_forbidden_capability:$field"
        }
      }
      $deleteAdvisorName = [string](Get-ObjectPropertyValue $deleteAdvisor "entrypoint")
      $deleteAdvisorPath = Join-Path (Split-Path -Parent $PolicyPath) $deleteAdvisorName
      if (-not (Test-Path -LiteralPath $deleteAdvisorPath -PathType Leaf)) {
        Add-Issue "dangerous_delete_advisory_entrypoint_missing:$deleteAdvisorName"
      } else {
        $expectedDeleteAdvisorHash = [string](Get-ObjectPropertyValue $deleteAdvisor "entrypoint_sha256")
        $actualDeleteAdvisorHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $deleteAdvisorPath).Hash.ToLowerInvariant()
        if ($actualDeleteAdvisorHash -ne $expectedDeleteAdvisorHash) {
          Add-Issue "dangerous_delete_advisory_entrypoint_hash_mismatch"
        }
      }
    }

    $correctionContract = Get-ObjectPropertyValue $runtimeEnforcement "behavior_correction_contract"
    if ($null -eq $correctionContract) {
      Add-Issue "behavior_correction_contract_missing"
    } else {
      if ((Get-ObjectPropertyValue $correctionContract "schema") -ne "cbh.behavior_correction_contract.v1") {
        Add-Issue "behavior_correction_contract_schema_invalid"
      }
      foreach ($field in @("automatic_freeze", "automatic_long_term_memory_write", "automatic_policy_mutation")) {
        if ([bool](Get-ObjectPropertyValue $correctionContract $field)) {
          Add-Issue "behavior_correction_contract_forbidden_auto_action:$field"
        }
      }
      $migrationHook = Get-ObjectPropertyValue $correctionContract "migration_hook"
      if ($null -eq $migrationHook) {
        Add-Issue "behavior_correction_migration_hook_missing"
      } else {
        if ((Get-ObjectPropertyValue $migrationHook "schema") -ne "cbh.behavior_correction_migration_hook.v1") {
          Add-Issue "behavior_correction_migration_hook_schema_invalid"
        }
        if ((Get-ObjectPropertyValue $migrationHook "hook_event") -ne "PreToolUse" -or
            (Get-ObjectPropertyValue $migrationHook "tool_name_matcher") -ne "^Bash$" -or
            (Get-ObjectPropertyValue $migrationHook "output_contract") -ne "allow_updated_input_only") {
          Add-Issue "behavior_correction_migration_hook_boundary_invalid"
        }
        foreach ($field in @("host_blocking", "stateful", "automatic_memory_write", "automatic_policy_mutation")) {
          if ([bool](Get-ObjectPropertyValue $migrationHook $field)) {
            Add-Issue "behavior_correction_migration_hook_forbidden_capability:$field"
          }
        }
        $migrationHookName = [string](Get-ObjectPropertyValue $migrationHook "entrypoint")
        $migrationHookPath = Join-Path (Split-Path -Parent $PolicyPath) $migrationHookName
        if (-not (Test-Path -LiteralPath $migrationHookPath -PathType Leaf)) {
          Add-Issue "behavior_correction_migration_hook_entrypoint_missing:$migrationHookName"
        } else {
          $expectedMigrationHookHash = [string](Get-ObjectPropertyValue $migrationHook "entrypoint_sha256")
          $actualMigrationHookHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $migrationHookPath).Hash.ToLowerInvariant()
          if ($actualMigrationHookHash -ne $expectedMigrationHookHash) {
            Add-Issue "behavior_correction_migration_hook_entrypoint_hash_mismatch"
          }
        }
      }
      $profileName = [string](Get-ObjectPropertyValue $correctionContract "profile_registry_path")
      $profilePath = Join-Path (Split-Path -Parent $PolicyPath) $profileName
      if (-not (Test-Path -LiteralPath $profilePath -PathType Leaf)) {
        Add-Issue "behavior_correction_profile_registry_missing:$profileName"
      } else {
        $expectedHash = [string](Get-ObjectPropertyValue $correctionContract "profile_registry_sha256")
        $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $profilePath).Hash.ToLowerInvariant()
        if ($actualHash -ne $expectedHash) {
          Add-Issue "behavior_correction_profile_registry_hash_mismatch"
        }
      }
    }
  }

  $placeholderPaths = @()
  if ($null -ne $policy.project_lanes) {
    foreach ($lane in $policy.project_lanes.PSObject.Properties) {
      if ($lane.Name -eq "EXAMPLE_PROJECT") {
        Add-Warning "template_project_lane_present:EXAMPLE_PROJECT"
      }
      foreach ($path in (ConvertTo-Array $lane.Value)) {
        if ([string]$path -like "C:\path\to\*") {
          $placeholderPaths += [string]$path
        }
      }
    }
  }
  if ($null -ne $policy.memory_roots) {
    foreach ($lane in $policy.memory_roots.PSObject.Properties) {
      foreach ($path in (ConvertTo-Array $lane.Value)) {
        if ([string]$path -like "C:\path\to\*") {
          $placeholderPaths += [string]$path
        }
      }
    }
  }
  if ($placeholderPaths.Count -gt 0) {
    Add-Warning "template_placeholder_paths_present:replace_before_production_use"
  }

  $searchMatrix = $policy.search_and_learning_decision_matrix
  if ($null -eq $searchMatrix) {
    Add-Issue "search_and_learning_decision_matrix_missing"
  } else {
    $externalRetrievalContract = Get-ObjectPropertyValue $searchMatrix "external_retrieval_contract"
    if ($null -eq $externalRetrievalContract) {
      Add-Issue "external_retrieval_contract_missing"
    } else {
      if ((Get-ObjectPropertyValue $externalRetrievalContract "schema") -ne "cbh.external_retrieval_contract.v1") {
        Add-Issue "external_retrieval_contract_schema_invalid"
      }
      if ((Get-ObjectPropertyValue $externalRetrievalContract "receipt_schema") -ne "cbh.external_retrieval_receipt.v1") {
        Add-Issue "external_retrieval_receipt_schema_invalid"
      }
      foreach ($field in @(
        "profile_values",
        "objective_order",
        "explicit_search_intent_terms",
        "local_only_exclusion_terms",
        "generic_memory_feature_terms",
        "local_memory_access_intent_terms",
        "memory_write_intent_terms",
        "anchor_kinds",
        "query_stage_order",
        "coverage_status_values",
        "target_coverage_status_values",
        "source_status_values",
        "generic_facet_source_route_ids",
        "authority_binding_status_values",
        "source_ledger_fields",
        "source_refs"
      )) {
        if ((ConvertTo-Array (Get-ObjectPropertyValue $externalRetrievalContract $field)).Count -eq 0) {
          Add-Issue "external_retrieval_contract_field_empty:$field"
        }
      }
      foreach ($field in @(
        "merge_policy",
        "fallback_rule",
        "negative_claim_rule",
        "coverage_rule",
        "cost_rule",
        "external_memory_boundary",
        "source_capability_rule",
        "unknown_source_rule",
        "currentness_evidence_rule",
        "rule"
      )) {
        if ([string]::IsNullOrWhiteSpace([string](Get-ObjectPropertyValue $externalRetrievalContract $field))) {
          Add-Issue "external_retrieval_contract_field_empty:$field"
        }
      }
      foreach ($forbidden in @("max_queries", "max_sources", "fixed_source_count")) {
        if ($null -ne (Get-ObjectPropertyValue $externalRetrievalContract $forbidden)) {
          Add-Issue "external_retrieval_contract_hard_cap_forbidden:$forbidden"
        }
      }
      $maxFutureSkewSeconds = Get-ObjectPropertyValue $externalRetrievalContract "max_future_skew_seconds"
      if ($null -eq $maxFutureSkewSeconds -or [int]$maxFutureSkewSeconds -lt 0 -or [int]$maxFutureSkewSeconds -gt 3600) {
        Add-Issue "external_retrieval_contract_max_future_skew_invalid"
      }
      $maxFreshnessWindowSeconds = Get-ObjectPropertyValue $externalRetrievalContract "max_freshness_window_seconds"
      if ($null -eq $maxFreshnessWindowSeconds -or [int]$maxFreshnessWindowSeconds -lt 1 -or [int]$maxFreshnessWindowSeconds -gt 2592000) {
        Add-Issue "external_retrieval_contract_max_freshness_window_invalid"
      }
      $sourceNativeRoutes = Get-ObjectPropertyValue $externalRetrievalContract "source_native_routes"
      if ($null -eq $sourceNativeRoutes -or $sourceNativeRoutes.PSObject.Properties.Count -eq 0) {
        Add-Issue "external_retrieval_source_native_routes_missing"
      } else {
        foreach ($route in $sourceNativeRoutes.PSObject.Properties) {
          foreach ($field in @("anchor_types", "provider_hints")) {
            if ((ConvertTo-Array (Get-ObjectPropertyValue $route.Value $field)).Count -eq 0) {
              Add-Issue "external_retrieval_source_route_field_empty:$($route.Name):$field"
            }
          }
          foreach ($field in @("mode", "query_template", "activation_condition", "authority_boundary")) {
            if ([string]::IsNullOrWhiteSpace([string](Get-ObjectPropertyValue $route.Value $field))) {
              Add-Issue "external_retrieval_source_route_field_empty:$($route.Name):$field"
            }
          }
          $directUrlTemplate = Get-ObjectPropertyValue $route.Value "direct_url_template"
          if ($null -eq $directUrlTemplate) {
            Add-Issue "external_retrieval_source_route_field_missing:$($route.Name):direct_url_template"
          }
        }
        foreach ($routeId in (ConvertTo-Array (Get-ObjectPropertyValue $externalRetrievalContract "generic_facet_source_route_ids"))) {
          if ($null -eq (Get-ObjectPropertyValue $sourceNativeRoutes ([string]$routeId))) {
            Add-Issue "external_retrieval_generic_facet_route_unknown:$routeId"
          }
        }
      }
    }
  }

  $claimContract = $policy.claim_schema_contract
  if ($null -eq $claimContract) {
    Add-Issue "claim_schema_contract_missing"
  } else {
    foreach ($field in @("allowed_source_types","source_ref_required_for","evidence_boundary_enum","strong_claim_evidence_boundaries")) {
      if ((ConvertTo-Array (Get-ObjectPropertyValue $claimContract $field)).Count -eq 0) {
        Add-Issue "claim_schema_contract_empty:$field"
      }
    }
  }
}

$repoCandidate = if ($RepoRoot) { $RepoRoot } else { Join-Path $PSScriptRoot "..\.." }
if ((Test-Path -LiteralPath (Join-Path $repoCandidate "VERSION")) -and
    (Test-Path -LiteralPath (Join-Path $repoCandidate "skills\embedded-harness\embedded_harness_policy.json"))) {
  Test-BeliefTraceInvariants $repoCandidate
}

$status = if ($issues.Count -gt 0) { "blocked" } else { "pass" }
$result = [ordered]@{
  ts = (Get-Date).ToString("o")
  phase = "validate_policy"
  status = $status
  policy_path = $PolicyPath
  issues = @($issues | Select-Object -Unique)
  warnings = @($warnings | Select-Object -Unique)
  rule = "lightweight policy parse, shape check, claim schema contract check, placeholder warnings, and belief_trace_summary.current_status invariant check when repo root is available; not a full JSON Schema validator"
}

$json = $result | ConvertTo-Json -Depth 20
if ($OutputPath) {
  $dir = Split-Path -Parent $OutputPath
  if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  Set-Content -LiteralPath $OutputPath -Value $json -Encoding UTF8
}
$json
if ($status -eq "blocked") { exit 2 }
