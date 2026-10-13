from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_new_memory_and_reading_contracts_are_indexed() -> None:
    required_docs = [
        "docs/memory-feedback-loop-trial.md",
        "docs/memory-write-granularity-contract.md",
        "docs/hybrid-memory-retrieval-contract.md",
        "docs/content-reading-contract.md",
        "docs/skill-lifecycle-contract.md",
    ]
    for relative in required_docs:
        assert (ROOT / relative).is_file(), relative

    readme = read_text("README.md")
    architecture = read_text("docs/architecture.md")
    doctor = read_text("tools/cbh_doctor.py")
    test_cases = read_text("docs/test-cases.md")

    for relative in required_docs:
        name = Path(relative).name
        assert name in readme
        assert name in architecture
        assert relative in doctor

    assert "TC-032" in test_cases
    assert "TC-036" in test_cases


def test_agent_self_deployment_map_is_required_and_machine_visible() -> None:
    relative = "docs/agent-deployment-map.md"
    assert (ROOT / relative).is_file()
    assert relative in read_text("AGENTS.md")
    assert relative in read_text("README.md")
    assert relative in read_text("README_zh.md")
    assert relative in read_text("tools/cbh_doctor.py")
    profiles = json.loads(read_text("integrations/codex-local/deployment-profile.json"))
    for profile in profiles["profiles"].values():
        assert profile["required_predeployment_read"] == relative


def test_task_checkpoint_consumer_is_declared_in_adapter_contract() -> None:
    manifest = json.loads(read_text("templates/adapter-contract/compatibility.manifest.json"))
    checkpoint = manifest["task_memory_checkpoint"]
    assert checkpoint["candidate_schema"] == "cbh.task_memory_checkpoint_candidate.v1"
    assert checkpoint["promotion_receipt_schema"] == (
        "cbh.task_memory_checkpoint_promotion_receipt.v1"
    )
    assert checkpoint["background_queue_required"] is False
    assert checkpoint["host_promotes_eligible_candidate"] == "unverified"


def test_bilingual_readme_and_local_overlay_template_are_present() -> None:
    readme = read_text("README.md")
    readme_zh = read_text("README_zh.md")
    overlay = json.loads(read_text("skills/embedded-harness/embedded_harness_policy.local.example.json"))
    policy = json.loads(read_text("skills/embedded-harness/embedded_harness_policy.json"))

    assert "[中文版](./README_zh.md) | English" in readme
    assert "[English](./README.md) | 中文" in readme_zh
    assert "Current main-branch version: `v1.2.8`." in readme
    assert "当前 main 分支版本：`v1.2.8`。" in readme_zh
    assert "Latest tagged GitHub release: [`v1.2.8`]" in readme
    assert "最新已打 tag 的 GitHub Release：[`v1.2.8`]" in readme_zh
    assert "releases/latest" in readme
    assert "releases/latest" in readme_zh
    assert read_text("VERSION").strip() == "v1.2.8"
    assert overlay["schema"] == "cbh.project_lane_overlay.v1"
    assert policy["local_project_lane_overlay"]["default_filename"] == "embedded_harness_policy.local.json"
    assert "embedded_harness_policy.local.json" in readme
    assert "CBH_PROJECT_LANES_FILE" in readme_zh


def test_public_docs_describe_current_nonblocking_runtime_and_existing_minimal_profiles() -> None:
    readme = read_text("README.md")
    readme_zh = read_text("README_zh.md")
    deployment = read_text("docs/deployment-risk-patterns.md")

    for retired in (
        "harness_runtime_enforcer.ps1",
        "harness_task_wrapper.ps1",
        "harness_tool_proxy.ps1",
    ):
        assert retired not in readme

    assert "selective tool-proxy blocking" not in deployment
    assert "Run the intake router or runtime enforcer directly" not in deployment
    assert "high-risk tasks return blocked" not in deployment
    assert "host-native" in deployment
    assert "advisory" in deployment

    for text in (readme, readme_zh):
        assert "codex-local-minimal" in text
        assert "deployment-profile.json" in text
        assert "build-deployment-bundle.py" in text
    assert "Copy this package into a new workspace" not in readme
    assert "把这个包复制到目标 workspace" not in readme_zh


def test_public_docs_preserve_task_authorization_and_host_enforcement() -> None:
    policy = json.loads(read_text("skills/embedded-harness/embedded_harness_policy.json"))
    assert "Task-scoped authorization remains valid" in policy["gate_enforcement_boundary"]
    assert "Host-enforced execution blocking" in policy["gate_enforcement_boundary"]
    agents = read_text("AGENTS.md")
    assert "Existing authorization remains valid" in agents
    assert "approval" in agents and "does not consume it" in agents
    for name in ("README.md", "README_zh.md"):
        assert "skills/text-source-reader/SKILL.md" in read_text(name)


def test_citation_notice_are_visible_and_public_report_draft_is_absent() -> None:
    required_files = [
        "CITATION.cff",
        "NOTICE.md",
        "docs/assets/doi-badge.svg",
    ]
    for relative in required_files:
        assert (ROOT / relative).is_file(), relative
    assert not (ROOT / "docs/articles/claim-boundary-harness-technical-report.md").exists()

    readme = read_text("README.md")
    readme_zh = read_text("README_zh.md")
    citation = read_text("CITATION.cff")
    doi_badge = read_text("docs/assets/doi-badge.svg")
    notice = read_text("NOTICE.md")
    license_text = read_text("LICENSE")
    changelog = read_text("CHANGELOG.md")
    manifest = json.loads(read_text("templates/adapter-contract/compatibility.manifest.json"))

    assert "CITATION.cff" in readme
    assert "NOTICE.md" in readme
    assert "10.5281/zenodo.21189879" in readme
    assert "./docs/assets/doi-badge.svg" in readme
    assert "Latest tagged GitHub release:" in readme
    assert "Historical releases and tags remain" in readme
    assert "claim-boundary-harness-technical-report.md" not in readme
    assert "CITATION.cff" in readme_zh
    assert "NOTICE.md" in readme_zh
    assert "10.5281/zenodo.21189879" in readme_zh
    assert "./docs/assets/doi-badge.svg" in readme_zh
    assert "最新已打 tag 的 GitHub Release：" in readme_zh
    assert "已删除的历史 tag 链接" in readme_zh
    assert "claim-boundary-harness-technical-report.md" not in readme_zh
    assert "title: \"Agent Cognitive Continuity Framework: Memory, Execution-State, and Attention Continuity for LLM Agent Workflows\"" in citation
    assert "qimen039-code" in citation
    assert "version: \"1.2.8\"" in citation
    assert "date-released: \"2026-10-13\"" in citation
    assert "doi: \"10.5281/zenodo.21189879\"" in citation
    assert "10.5281/zenodo.21189879" in doi_badge
    assert 'role="img"' in doi_badge
    assert "Recommended short attribution" in notice
    assert "submitted arXiv record exists" in notice
    assert "Copyright (c) 2026 qimen039-code" in license_text
    assert "Zenodo DOI trigger release" in changelog
    assert "## v1.0.0 - 2026-07-20" in changelog
    stale_version = "v0." + "14.0"
    assert stale_version not in changelog
    assert manifest["harness_version"] == "v1.2.8"
    assert "## v1.2.8 - 2026-10-13" in changelog
    agents = read_text("AGENTS.md")
    assert "An explicit version-update task includes" in agents
    assert "latest Release afterwards" in agents
    assert "mandatory model-layer pre-action stops" in changelog
    planner = manifest["external_retrieval_planner"]
    assert planner["receipt_schema"] == "cbh.external_retrieval_receipt.v1"
    assert planner["performs_network_access"] is False
    assert planner["writes_durable_memory"] is False
    assert planner["host_model_executes_search"] is True
    assert planner["provider_miss_is_verified_absence"] is False


def test_memory_feedback_loop_trial_is_optional_and_template_visible() -> None:
    trial = read_text("docs/memory-feedback-loop-trial.md")
    schema = read_text("docs/source-monitoring-memory-schema.md")
    meta_contract = read_text("docs/memory-meta-index-contract.md")
    routing_contract = read_text("docs/memory-routing-contract.md")
    common_error_doc = read_text("docs/common-error-corpus.md")
    common_error_template = read_text("templates/common-error-corpus/CE-EXAMPLE-YYYY-MM-DD.md")
    project_meta = read_text("templates/project/memory-library/_META_INDEX.md")
    conversation_meta = read_text("templates/conversation-memory/_META_INDEX.md")
    manifest = json.loads(read_text("templates/adapter-contract/compatibility.manifest.json"))

    for text in [trial, schema, common_error_doc, common_error_template, project_meta]:
        assert "feedback_loop" in text

    for text in [schema, project_meta, conversation_meta]:
        assert "source_validity_dependency" in text

    assert "Conflict Resolution Policy" in schema
    assert "source invalidation" in read_text("docs/architecture.md")
    assert "lane_state" in meta_contract
    assert "frozen_readonly" in routing_contract
    assert "not a task-cost ledger" in trial
    assert "per-task token ledger" in read_text("docs/architecture.md")
    assert "status: pending" in common_error_template
    assert manifest["memory_feedback_loop"]["field_name"] == "feedback_loop"
    assert manifest["memory_feedback_loop"]["advisory_only"] is True
    assert manifest["memory_feedback_loop"]["host_hard_stop_gate"] is False
    assert manifest["memory_feedback_loop"]["internalized_on_reusable_memory_selection"] is True
    assert manifest["memory_feedback_loop"]["does_not_create_task_cost_ledger"] is True
    assert manifest["memory_feedback_loop"]["profile_controls_cost"] is True
    assert manifest["memory_feedback_loop"]["feedback_loop_profile_values"] == [
        "none",
        "index_hint",
        "record_candidate",
        "prevention_review",
        "explicit_cycle",
    ]
    assert manifest["memory_integrity_policy"]["recency_is_context_not_truth"] is True
    assert manifest["memory_integrity_policy"]["source_invalidity_cascade_blocks_validated_retrieval"] is True
    assert manifest["memory_integrity_policy"]["lane_state_values"] == [
        "active",
        "frozen_readonly",
        "cleared",
    ]
    assert manifest["memory_integrity_policy"]["frozen_readonly_excluded_from_default_retrieval_and_writes"] is True
    assert manifest["observation_and_causal_attribution"]["public_private_boundary_is_separate"] is True
    assert manifest["observation_and_causal_attribution"]["blocks_ordinary_local_causal_reasoning"] is False
    assert "causal-attribution" in trial
    assert "does not prove causality" in trial


def test_conversation_templates_expose_reading_profiles() -> None:
    conversation_index = json.loads(read_text("templates/conversation-memory/index.json"))
    ledger_index = json.loads(read_text("templates/conversation-ledger/domain_index.json"))

    for payload in [conversation_index, ledger_index]:
        policy = payload["content_reading_policy"]
        assert policy["profile_selected_by"] == "routing_or_decision_layer"
        assert policy["default_profile"] == "baseline"
        assert policy["available_profiles"] == [
            "baseline",
            "evidence_window",
            "middle_safe",
            "full_audit",
        ]
        assert "conditional_triggers" in policy
        assert "middle_safe_layout" in policy

    middle_safe = conversation_index["content_reading_policy"]["middle_safe_layout"]
    assert middle_safe["dual_anchor"] == "inventory_plus_original_window"
    assert middle_safe["head_tail_middle_reread_gate"]["enabled"] is True
    assert middle_safe["head_tail_middle_reread_gate"]["action"] == "bounded_middle_reread_around_structural_anchors"


def test_memory_profiles_are_routed_and_template_visible() -> None:
    policy = json.loads(read_text("skills/embedded-harness/embedded_harness_policy.json"))
    conversation_index = json.loads(read_text("templates/conversation-memory/index.json"))
    manifest = json.loads(read_text("templates/adapter-contract/compatibility.manifest.json"))

    receipt_fields = policy["router_decision_contract"]["receipt_fields"]
    assert "skill_lifecycle_profile" in receipt_fields
    assert "feedback_loop_profile" in receipt_fields
    assert "hybrid_retrieval_profile" in receipt_fields
    assert "memory_write_profile" in receipt_fields
    assert "task_continuity_decision" in receipt_fields
    assert "task_continuity" in policy["router_decision_contract"]["module_need_values"]
    continuity = policy["router_decision_contract"]["task_continuity_contract"]
    assert continuity["schema"] == "cbh.task_continuity_contract.v1"
    assert continuity["state_storage"] == "persistent_tasks_local_cumcwork_only"
    action_contract = policy["router_decision_contract"]["action_binding_contract"]
    assert "prepare_task_continuity_capsule" in action_contract["next_action_values"]
    assert "task_continuity_capsule_or_dormant_receipt" in action_contract[
        "completion_evidence_values"
    ]
    assert "debt_hygiene_gate" in policy["router_decision_contract"]["module_need_values"]
    assert "debt_hygiene_rule" in policy["router_decision_contract"]
    assert "candidate_technical_debt" in read_text("docs/router-decision-contract.md")
    assert "candidate_technical_debt" in read_text("docs/cost-control-contract.md")
    assert policy["router_decision_contract"]["skill_lifecycle_profile_values"] == [
        "none",
        "listing_only",
        "active_frame_required",
        "release_receipt_required",
        "reactivate_from_receipt",
    ]
    assert policy["router_decision_contract"]["feedback_loop_profile_values"] == [
        "none",
        "index_hint",
        "record_candidate",
        "prevention_review",
        "explicit_cycle",
    ]
    assert policy["router_decision_contract"]["hybrid_retrieval_profile_values"] == [
        "none",
        "meta_first_hybrid_enhancement",
        "meta_first_hybrid_required",
    ]
    assert policy["router_decision_contract"]["memory_write_profile_values"] == [
        "none",
        "context_complete_required",
        "strict_capsule_required",
    ]

    assert conversation_index["hybrid_retrieval_profile_default"] == "meta_first_hybrid_required"
    assert conversation_index["content_plane"]["memory_write_profile_default"] == "context_complete_required"
    assert conversation_index["lane_state"] == "active"
    assert conversation_index["lane_state_policy"]["allowed_values"] == [
        "active",
        "frozen_readonly",
        "cleared",
    ]
    assert conversation_index["memory_integrity_policy"]["conflict_resolution"] == "scope_and_confidence_before_recency"
    assert conversation_index["memory_integrity_policy"]["recency_is_context_not_truth"] is True
    assert manifest["skill_lifecycle"]["receipt_schema"] == "cbh.skill_release_receipt.v1"
    assert manifest["skill_lifecycle"]["reactivation_reads_current_source_files"] is True
    assert manifest["memory_feedback_loop"]["prediction_is_hypothesis_until_verified"] is True
    assert manifest["tool_surface_discovery"]["checks_before_fallback_to_shell_or_raw_web"] is True
    assert "tool_surface_need" in manifest["tool_surface_discovery"]["field_names"]
    assert manifest["observation_and_causal_attribution"]["attribution_levels"] == [
        "mechanism_property",
        "empirical_record",
        "causal_hypothesis",
        "validated_causality",
    ]
    assert "global_task_context_gate" in policy["router_decision_contract"]["issue_prevention_gates"]
    assert "global_task_context_gate" in read_text("docs/router-decision-contract.md")
    assert "global task context" in read_text("README.md")
    assert "局部因果先看任务全貌" in read_text("README_zh.md")
    assert "task_continuity.py" in read_text("docs/agent-deployment-map.md")
    assert manifest["memory_retrieval_result"]["hybrid_retrieval_is_meta_first_enhancement"] is True
    assert manifest["memory_write_granularity"]["strict_capsules_reject_orphan_fragments"] is True


def test_skill_release_receipt_template_is_reactivation_ready() -> None:
    receipt = json.loads(read_text("templates/skill-lifecycle/skill_release_receipt.json"))

    assert receipt["schema"] == "cbh.skill_release_receipt.v1"
    for field in [
        "skill_id",
        "status",
        "completed_steps",
        "current_stage",
        "artifact_paths",
        "evidence_refs",
        "open_loops",
        "resume_entry",
        "last_used_at",
        "ttl_policy",
    ]:
        assert field in receipt
    assert "SKILL.md" in receipt["resume_entry"]


def test_external_learning_and_exposed_capability_short_circuit_are_bounded() -> None:
    agents = read_text("AGENTS.md")
    router = read_text("docs/router-decision-contract.md")
    memory_writes = read_text("docs/memory-write-granularity-contract.md")

    assert "Use an exposed tool that covers the next action" in agents
    assert "Historical error records are diagnostic evidence" in agents
    for text in (router,):
        assert "resolved_current_exposure" in text
        assert "unchanged exposure state" in text
        assert "adapted_mechanism" in text
        assert "local coverage" in text
        assert "source shortcomings" in text

    assert "adapted_mechanism" in memory_writes
    assert "direct_reuse" in memory_writes
    assert "rejected_part" in memory_writes
    assert "full candidate route sets" in router.lower()
    assert "route_receipt_ref" in router


def test_portable_context_bundle_contract_is_lazy_and_indexed() -> None:
    contract_path = ROOT / "docs/portable-context-bundle-contract.md"
    manifest_path = ROOT / "templates/portable-context-bundle/manifest.json"

    assert contract_path.is_file()
    assert manifest_path.is_file()

    contract = contract_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for relative in (
        "docs/portable-context-bundle-contract.md",
        "templates/portable-context-bundle/manifest.json",
    ):
        assert relative in read_text("README.md")
        assert relative in read_text("README_zh.md")
    assert "portable-context-bundle-contract.md" in read_text("docs/architecture.md")

    assert manifest["schema"] == "cbh.portable_context_bundle.v1"
    policy = manifest["validation_policy"]
    assert policy["trigger_events"] == [
        "bundle_creation",
        "bundle_import",
        "activate_as_current_guidance",
        "source_or_payload_hash_drift",
        "strong_claim_use",
    ]
    assert policy["no_revalidation_events"] == [
        "ordinary_index_lookup",
        "ordinary_capsule_read",
        "unchanged_hash_reuse",
    ]
    assert policy["reuse_cached_structural_validation_when_hashes_match"] is True
    assert manifest["runtime_boundary"]["registers_host_hook"] is False
    assert manifest["runtime_boundary"]["adds_per_task_validation"] is False
    assert "does not create a new memory lane" in contract
