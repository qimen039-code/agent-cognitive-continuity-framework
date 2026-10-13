param(
  [string]$TaskText = "",
  [string]$ClaimText = "",
  [string]$AttemptJson = "",
  [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "external_route_trigger_helpers.ps1")
$policy = Get-Content -LiteralPath (Join-Path $PSScriptRoot "embedded_harness_policy.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$combined = "$TaskText`n$ClaimText"
$matchedTriggers = @()
$negatedTriggers = @()

foreach ($trigger in (ConvertTo-TriggerList $policy.external_research_triggers)) {
  if ([string]::IsNullOrWhiteSpace($trigger)) {
    continue
  }
  $regex = New-TriggerRegex ([string]$trigger)
  foreach ($hit in [regex]::Matches($combined, $regex)) {
    if (Test-ExternalNegatedMatch -source $combined -index $hit.Index) {
      $negatedTriggers += [string]$trigger
    } else {
      $matchedTriggers += [string]$trigger
    }
  }
}

$externalRetrievalContract = $policy.search_and_learning_decision_matrix.external_retrieval_contract
$explicitSearchIntentMatches = @()
foreach ($trigger in (ConvertTo-TriggerList $externalRetrievalContract.explicit_search_intent_terms)) {
  if ([string]::IsNullOrWhiteSpace($trigger)) {
    continue
  }
  $regex = New-TriggerRegex ([string]$trigger)
  foreach ($hit in [regex]::Matches($combined, $regex)) {
    if (Test-ExternalNegatedMatch -source $combined -index $hit.Index) {
      $negatedTriggers += [string]$trigger
    } else {
      $matchedTriggers += [string]$trigger
      $explicitSearchIntentMatches += [string]$trigger
    }
  }
}

$localOnlyExclusionHits = @()
foreach ($trigger in (ConvertTo-TriggerList $externalRetrievalContract.local_only_exclusion_terms)) {
  if ([string]::IsNullOrWhiteSpace($trigger)) {
    continue
  }
  $regex = New-TriggerRegex ([string]$trigger)
  if ([regex]::IsMatch($combined, $regex)) {
    $localOnlyExclusionHits += [string]$trigger
  }
}

$derivedExternalSignals = Get-DerivedExternalSignalMatchSet $combined
$matchedTriggers += @($derivedExternalSignals.positive)
$negatedTriggers += @($derivedExternalSignals.negated)

$recommendedModes = @()
foreach ($mode in $policy.search_and_learning_decision_matrix.search_modes.PSObject.Properties) {
  $modeMatched = $false
  foreach ($trigger in (ConvertTo-TriggerList $mode.Value.triggers)) {
    if ([string]::IsNullOrWhiteSpace($trigger)) {
      continue
    }
    $regex = New-TriggerRegex ([string]$trigger)
    foreach ($hit in [regex]::Matches($combined, $regex)) {
      if (-not (Test-ExternalNegatedMatch -source $combined -index $hit.Index)) {
        $modeMatched = $true
        break
      }
    }
    if ($modeMatched) {
      break
    }
  }
  if ($modeMatched) {
    $recommendedModes += [string]$mode.Name
  }
}

$hasExplicitSearchIntent = $explicitSearchIntentMatches.Count -gt 0
$ownerRepoPattern = '(?i)(?<![A-Za-z0-9_.-])[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}(?![A-Za-z0-9_.-])'
if ($hasExplicitSearchIntent) {
  $recommendedModes += "general_web_cross_check"
}
if ([regex]::IsMatch($combined, '(?i)github\.com|\bGitHub\b|仓库|开源')) {
  $recommendedModes += "github_open_source_repository_search"
}

$needs = ($matchedTriggers.Count -gt 0) -and ($localOnlyExclusionHits.Count -eq 0)
if ($needs -and $recommendedModes.Count -eq 0) {
  $recommendedModes += "general_web_cross_check"
}
if (-not $needs) {
  $recommendedModes = @()
}
$recommendedModes = @($recommendedModes | Select-Object -Unique)

$plannerPath = Join-Path $PSScriptRoot "external_retrieval_strategy.py"
$pythonCommand = Get-Command python -ErrorAction Stop
$plannerInput = if ([string]::IsNullOrWhiteSpace($ClaimText)) { $TaskText } else { $combined.Trim() }
$taskTextBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($plannerInput))
$plannerArgs = @("-B", $plannerPath, "--task-text-base64", $taskTextBase64, "--ascii-output", "--policy-path", (Join-Path $PSScriptRoot "embedded_harness_policy.json"))
foreach ($mode in $recommendedModes) {
  $plannerArgs += @("--mode", [string]$mode)
}
if (-not [string]::IsNullOrWhiteSpace($AttemptJson)) {
  $attemptJsonBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($AttemptJson))
  $plannerArgs += @("--attempt-json-base64", $attemptJsonBase64)
}
$retrievalReceiptJson = & $pythonCommand.Source @plannerArgs
if ($LASTEXITCODE -ne 0) {
  throw "external retrieval planner failed with exit code $LASTEXITCODE"
}
$retrievalReceipt = $retrievalReceiptJson | ConvertFrom-Json

$result = [ordered]@{
  ts = (Get-Date).ToString("o")
  phase = "external_research_gate"
  status = "pass"
  needs_external_research = $needs
  matched_triggers = @($matchedTriggers | Select-Object -Unique)
  negated_triggers = @($negatedTriggers | Select-Object -Unique)
  local_only_exclusion_hits = @($localOnlyExclusionHits | Select-Object -Unique)
  recommended_search_modes = @($recommendedModes)
  learning_classification_labels = @($policy.search_and_learning_decision_matrix.classification_labels)
  external_retrieval_receipt = $retrievalReceipt
  rule = "policy-driven trigger and source-mode routing plus an anchor-preserving task-local retrieval plan; no browsing, blocking, or memory write"
}

$json = $result | ConvertTo-Json -Depth 20
if ($OutputPath) {
  $dir = Split-Path -Parent $OutputPath
  if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  Set-Content -LiteralPath $OutputPath -Value $json -Encoding UTF8
}
$json
