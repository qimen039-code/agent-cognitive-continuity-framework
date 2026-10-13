from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness"
HARNESS = ROOT / "skills" / "embedded-harness"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

from runtime_projection import project_runtime  # noqa: E402


def capsule(*, epoch: str = "epoch-a") -> dict[str, object]:
    return {
        "schema": "cbh.task_capsule.v1",
        "capsule_id": "capsule-a",
        "lifecycle": "ACTIVE",
        "goal_revision": 3,
        "progress_revision": 7,
        "context_epoch": {"epoch_id": epoch},
        "global_goal_anchor": {
            "objective": "完成投影层并验证",
            "objective_sha256": "a" * 64,
            "purpose": "让模型保持全局并降低临时上下文成本",
            "required_outputs": [
                {"id": "runtime", "text": "投影运行时", "status": "unknown"},
            ],
            "acceptance_criteria": [
                {"id": "verified-output", "text": "投影结果已验证", "status": "unknown"},
            ],
            "constraints": ["不保存思维链"],
            "non_goals": ["不建立后台服务"],
            "stop_condition": "投影与回归通过后停止",
        },
        "current_stage": "implementation",
        "current_action": {
            "text": "commandExecution",
            "serves_output_ids": [],
            "serves_criterion_ids": ["verified-output"],
            "reason": "serves_acceptance_criterion:verified-output",
        },
        "next_action": "运行投影回归测试",
        "blocking_condition": None,
        "verified_completed": [],
        "inferred_progress": [
            {"id": "runtime", "text": "投影运行时", "status": "inferred"},
        ],
        "remaining_work": [
            {"id": "verified-output", "text": "投影结果已验证", "status": "unknown"},
        ],
        "semantic_review_required": False,
        "workspace_review_required": False,
        "evidence_refs": [
            {"source_id": f"source-{index}", "sha256": str(index) * 64}
            for index in range(6)
        ],
    }


def reminder() -> dict[str, object]:
    return {
        "schema": "cbh.dynamic_reminder.v1",
        "reminder_id": "reminder-a",
        "context_epoch_id": "epoch-a",
        "progress_revision": 7,
        "trigger": "missing_postcondition",
        "severity": "action_required",
        "dedupe_key": "missing:dispatch-a",
        "required_action": "verify the semantic postcondition",
        "expires_when": "required_action_satisfied_or_task_retired",
        "evidence_refs": [{"source_id": "dispatch-a"}],
    }


def parse_entry(entry: dict[str, str]) -> dict[str, object]:
    return json.loads(entry["value"].split("\n", 1)[1])


def test_projection_id_is_deterministic_and_epoch_bound() -> None:
    first = project_runtime("global_causal_task", capsule=capsule())
    replay = project_runtime("global_causal_task", capsule=capsule())
    changed = project_runtime(
        "global_causal_task",
        capsule=capsule(epoch="epoch-b"),
    )

    assert first["envelope"]["projection_id"] == replay["envelope"]["projection_id"]
    assert first["envelope"]["projection_id"] != changed["envelope"]["projection_id"]
    assert first["envelope"]["source_digest"] == replay["envelope"]["source_digest"]


def test_global_projection_separates_control_from_untrusted_task_content() -> None:
    result = project_runtime("global_causal_task", capsule=capsule())
    control = parse_entry(result["control_entry"])
    evidence = parse_entry(result["evidence_entry"])

    assert control["schema"] == "agent.runtime_projection.control.v1"
    assert control["projection_type"] == "global_causal_task"
    assert control["authority_granted"] is False
    assert "完成投影层并验证" not in result["control_entry"]["value"]
    assert evidence["payload"]["global"]["objective"] == "完成投影层并验证"
    assert evidence["payload"]["relation"] == {
        "status": "explicit_binding",
        "specificity": "host_kind_only",
        "serves_output_ids": [],
        "serves_criterion_ids": ["verified-output"],
        "reason": "serves_acceptance_criterion:verified-output",
    }
    assert evidence["payload"]["progress"]["inferred_ids"] == ["runtime"]
    assert evidence["payload"]["progress"]["verified_ids"] == []
    assert len(evidence["source_refs"]) == 4


def test_unbound_action_projection_cannot_claim_task_progress() -> None:
    current = capsule()
    current["current_action"] = {
        "text": "commandExecution",
        "serves_output_ids": [],
        "serves_criterion_ids": [],
        "reason": "unbound_action",
        "binding_source": "unbound",
        "state_progress_allowed": False,
    }

    result = project_runtime("global_causal_task", capsule=current)
    evidence = parse_entry(result["evidence_entry"])

    assert evidence["payload"]["relation"] == {
        "status": "unbound",
        "specificity": "host_kind_only",
        "serves_output_ids": [],
        "serves_criterion_ids": [],
        "reason": "unbound_action",
        "binding_source": "unbound",
        "state_progress_allowed": False,
    }


def test_dynamic_projection_is_reminder_only_and_claim_bound() -> None:
    result = project_runtime(
        "dynamic_reminder",
        capsule=capsule(),
        reminder=reminder(),
    )
    evidence = parse_entry(result["evidence_entry"])
    serialized = json.dumps(evidence, ensure_ascii=False)

    assert evidence["payload"] == {
        "trigger": "missing_postcondition",
        "why_now_code": "missing_postcondition",
        "required_action": "verify the semantic postcondition",
        "serves_output_ids": [],
        "serves_criterion_ids": ["verified-output"],
        "claim_blocked_until": "semantic_postcondition_verified",
        "expires_when": "required_action_satisfied_or_task_retired",
    }
    assert "完成投影层并验证" not in serialized
    assert "remaining_work" not in serialized
    assert "global_goal_anchor" not in serialized
    assert result["envelope"]["invalidates_on"] == [
        "context_epoch_change",
        "required_action_satisfied",
        "task_retired",
    ]


def test_projection_rejects_unknown_type_and_epoch_mismatch() -> None:
    try:
        project_runtime("unknown", capsule=capsule())
    except ValueError as exc:
        assert str(exc) == "unsupported_projection_type"
    else:
        raise AssertionError("unknown projection type must fail")

    mismatched = reminder()
    mismatched["context_epoch_id"] = "epoch-stale"
    try:
        project_runtime(
            "dynamic_reminder",
            capsule=capsule(),
            reminder=mismatched,
        )
    except ValueError as exc:
        assert str(exc) == "dynamic_reminder_epoch_mismatch"
    else:
        raise AssertionError("stale reminder must not be projected")


def test_projection_respects_tight_host_limits_without_losing_focus() -> None:
    global_result = project_runtime(
        "global_causal_task",
        capsule=capsule(),
        host_limits={"max_chars": 1_000},
    )
    dynamic_result = project_runtime(
        "dynamic_reminder",
        capsule=capsule(),
        reminder=reminder(),
        host_limits={"max_chars": 700},
    )
    global_evidence = parse_entry(global_result["evidence_entry"])
    dynamic_evidence = parse_entry(dynamic_result["evidence_entry"])

    assert len(global_result["evidence_entry"]["value"]) <= 1_000
    assert global_evidence["coverage_status"] == "bounded"
    assert global_evidence["payload"]["global"]["objective"] == "完成投影层并验证"
    assert global_evidence["payload"]["focus"]["next_action"] == "运行投影回归测试"
    assert global_evidence["payload"]["relation"]["status"] == "explicit_binding"
    assert len(dynamic_result["evidence_entry"]["value"]) <= 700
    assert dynamic_evidence["payload"]["trigger"] == "missing_postcondition"
    assert dynamic_evidence["payload"]["required_action"]


def test_initial_write_intent_projects_attention_before_action_selection() -> None:
    pending_write = capsule()
    pending_write["activation_reasons"] = ["write_intent", "tool_required"]
    pending_write["current_action"] = None
    pending_write["progress_revision"] = 1

    evidence = parse_entry(
        project_runtime("global_causal_task", capsule=pending_write)["evidence_entry"]
    )

    assert evidence["payload"]["execution_attention"] == [
        {
            "trigger": "write_preflight_review_required",
            "timing": "before_first_action",
            "reason_codes": ["write_intent", "action_unbound"],
            "required_checks": [
                "exact_target_and_environment",
                "write_method_format_and_encoding",
                "minimal_change_boundary",
                "post_write_verifier",
            ],
        }
    ]


def test_tool_only_projection_requests_action_alignment_before_first_tool() -> None:
    tool_only = capsule()
    tool_only["activation_reasons"] = ["tool_required"]
    tool_only["current_action"] = None
    tool_only["progress_revision"] = 1

    evidence = parse_entry(
        project_runtime("global_causal_task", capsule=tool_only)["evidence_entry"]
    )

    assert evidence["payload"]["execution_attention"] == [
        {
            "trigger": "action_alignment_review_required",
            "timing": "before_first_action",
            "reason_codes": ["tool_required", "action_unbound"],
            "required_checks": [
                "required_output_or_acceptance_binding",
                "semantic_postcondition",
            ],
        }
    ]


def test_projection_without_execution_trigger_does_not_invent_attention() -> None:
    inactive = capsule()
    inactive["activation_reasons"] = []
    inactive["current_action"] = None
    inactive["progress_revision"] = 1

    evidence = parse_entry(
        project_runtime("global_causal_task", capsule=inactive)["evidence_entry"]
    )

    assert "execution_attention" not in evidence["payload"]


def test_finalization_review_is_capability_neutral_for_corrected_reader_surface() -> None:
    corrected = capsule()
    corrected["global_goal_anchor"]["objective"] = (
        "根据最终 diff 写 commit、release 说明和交付总结"
    )
    corrected["goal_deltas"] = [
        {"kind": "correction", "text": "不要沿用被否的旧方案标题"},
    ]
    result = project_runtime("global_causal_task", capsule=corrected)
    control = parse_entry(result["control_entry"])
    evidence = parse_entry(result["evidence_entry"])
    serialized = json.dumps(evidence, ensure_ascii=False)

    assert evidence["payload"]["finalization_review"] == {
        "status": "required",
        "surface": "commit",
        "capability_query": "review corrected reader-facing final surfaces",
        "reason_codes": ["correction_present", "reader_facing_surface"],
    }
    assert control["control"]["capability_query"] == (
        "review corrected reader-facing final surfaces"
    )
    assert "required_skill_id" not in control["control"]
    assert "被否的旧方案标题" not in serialized


def test_finalization_review_is_noop_for_ordinary_code_write() -> None:
    corrected = capsule()
    corrected["global_goal_anchor"]["objective"] = "修改 runtime_projection.py 并运行测试"
    corrected["goal_deltas"] = [
        {"kind": "correction", "text": "不要使用旧函数名"},
    ]
    evidence = parse_entry(
        project_runtime("global_causal_task", capsule=corrected)["evidence_entry"]
    )

    assert "finalization_review" not in evidence["payload"]
