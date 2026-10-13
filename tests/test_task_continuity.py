from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import tomllib


ROOT = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness"
PUBLIC_HARNESS = ROOT / "skills" / "embedded-harness"
HARNESS = PUBLIC_HARNESS if PUBLIC_HARNESS.is_dir() else ROOT
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

from task_continuity import (  # noqa: E402
    apply_task_event,
    build_dynamic_reminders,
    build_task_capsule_context,
    decide_task_continuity,
    initialize_task_capsule,
    new_task_capsule,
    page_result,
    plan_transport,
    process_worker_request,
)


def event(event_id: str, event_type: str, **payload: object) -> dict[str, object]:
    return {
        "schema": "cbh.task_event.v1",
        "event_id": event_id,
        "type": event_type,
        "observed_at": "2026-08-13T00:00:00Z",
        **payload,
    }


def write_route() -> dict[str, object]:
    return {
        "edit_operation_profile": "in_place_patch",
        "memory_mode": "none",
        "tool_surface_need": "local_filesystem",
        "action_bindings": [],
    }


def test_short_answer_only_question_stays_dormant() -> None:
    decision = decide_task_continuity(
        {
            "edit_operation_profile": "read_only",
            "memory_mode": "none",
            "tool_surface_need": "none",
            "action_bindings": [],
        },
        event("answer:1", "task_observed", objective="什么是检索增强生成？"),
    )

    assert decision == {
        "schema": "cbh.task_continuity_decision.v1",
        "decision": "dormant",
        "reasons": [],
        "source_event_ids": ["answer:1"],
        "host_delivery": "not_needed",
    }


def test_initial_write_intent_is_turn_local_even_when_turn_relation_is_ambiguous() -> None:
    decision = decide_task_continuity(
        {
            "edit_operation_profile": "none",
            "memory_mode": "none",
            "tool_surface_need": "none",
            "action_bindings": [],
        },
        event(
            "initial-write:1",
            "task_observed",
            objective="修改并验证专用动态投影",
            intent_kind="ambiguous",
            intent_confidence="low",
            intent_source="fallback",
        ),
    )

    assert decision["decision"] == "observe_once"
    assert decision["reasons"] == ["write_intent", "tool_required"]
    assert decision["host_delivery"] == "ready"


def test_empty_objective_stage_exit_cannot_create_a_capsule() -> None:
    route = {
        "edit_operation_profile": "none",
        "memory_mode": "none",
        "tool_surface_need": "host_lifecycle_event",
        "action_bindings": [],
    }
    observed = event(
        "empty:stage-exit",
        "stage_exit",
        task_key="thread-empty",
        objective="",
        retire_if_complete=True,
    )

    assert decide_task_continuity(route, observed)["decision"] == "dormant"
    response = process_worker_request(
        {
            "op": "observe",
            "route_receipt": route,
            "task_event": observed,
            "capsule": None,
        }
    )
    assert response["capsule"] is None
    assert response["additional_context_entry"] is None
    assert response["persistence"] == "none"


def test_short_then_phrase_does_not_promote_cross_turn_continuity() -> None:
    decision = decide_task_continuity(
        {
            "edit_operation_profile": "read_only",
            "memory_mode": "none",
            "tool_surface_need": "none",
            "action_bindings": [],
        },
        event(
            "short-then:1",
            "task_observed",
            objective="先检查一个文件，然后告诉我结果",
        ),
    )
    assert decision["decision"] == "observe_once"
    assert "multi_stage_task" not in decision["reasons"]


def test_explicit_plan_to_completion_promotes_cross_turn_continuity() -> None:
    decision = decide_task_continuity(
        {
            "edit_operation_profile": "none",
            "memory_mode": "none",
            "tool_surface_need": "none",
            "action_bindings": [],
        },
        event(
            "long-plan:1",
            "task_observed",
            objective="先制定 15 步计划，然后按计划执行到全部完成为止",
            multi_stage=True,
        ),
    )
    assert decision["decision"] == "arm"
    assert "multi_stage_task" in decision["reasons"]


def test_existing_active_capsule_does_not_authorize_current_user_input() -> None:
    route = write_route()
    capsule = new_task_capsule(
        route,
        event("start:1", "task_observed", objective="修复文件"),
    )
    source_text = "继续修复"
    decision = decide_task_continuity(
        route,
        event(
            "observe:2",
            "task_observed",
            objective=source_text,
            event_origin="current_user_input",
            source_units=[
                {
                    "id": "observe:2:user-request",
                    "source_role": "current_user_request",
                    "text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                }
            ],
        ),
        capsule,
    )
    assert decision["decision"] == "safe_readonly"
    assert decision["reasons"] == ["turn_relation_receipt_required"]


def test_discussing_persistence_and_resume_words_does_not_arm_continuity() -> None:
    decision = decide_task_continuity(
        {
            "edit_operation_profile": "read_only",
            "memory_mode": "none",
            "tool_surface_need": "none",
            "action_bindings": [],
        },
        event(
            "discussion:continuity",
            "task_observed",
            objective=(
                "检查连续记忆胶囊持续残留是否影响新目标，并解释恢复全局目标"
                "为何会误绑定；只汇报结果"
            ),
        ),
    )

    assert decision["decision"] == "observe_once"
    assert "multi_stage_task" not in decision["reasons"]
    assert "task_resume" not in decision["reasons"]


def test_unbound_user_turn_keeps_active_capsule_byte_equivalent() -> None:
    route = write_route()
    active = new_task_capsule(
        route,
        event(
            "safe-readonly:start",
            "task_observed",
            task_key="thread-safe-readonly",
            objective="完成原任务",
            acceptance_criteria=[{"id": "verified", "text": "原任务已验证"}],
        ),
    )
    before = json.loads(json.dumps(active, ensure_ascii=False))
    source_text = "把两个新问题交给另一个模型研究"

    response = process_worker_request(
        {
            "op": "observe",
            "route_receipt": route,
            "task_event": event(
                "safe-readonly:new-turn",
                "task_observed",
                task_key="thread-safe-readonly",
                objective=source_text,
                intent_kind="refine",
                intent_confidence="high",
                intent_source="explicit_marker",
                binding_required=False,
                event_origin="current_user_input",
                relation_nonce="nonce-safe-readonly",
                source_units=[
                    {
                        "id": "safe-readonly:new-turn:user-request",
                        "source_role": "current_user_request",
                        "text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                    }
                ],
            ),
            "capsule": active,
        }
    )

    assert response["decision"]["decision"] == "safe_readonly"
    assert response["capsule"] == before
    assert response["transition"]["changed"] is False
    assert response["persistence"] == "none"
    assert response["additional_context_entries"]["control"]["kind"] == "application"
    assert response["transport_receipt"]["total_estimated_tokens"] <= 300
    assert response["relation_binding"]["required"] is True
    assert "workfile_handle" not in response


def test_validated_typed_relation_receipt_allows_bound_refinement() -> None:
    route = write_route()
    active = new_task_capsule(
        route,
        event(
            "typed-relation:start",
            "task_observed",
            task_key="thread-typed-relation",
            objective="完成原任务",
            acceptance_criteria=[{"id": "verified", "text": "原任务已验证"}],
        ),
    )
    source_text = "补充原任务的验收说明"
    source_id = "typed-relation:turn:user-request"
    nonce = "typed-relation-nonce"
    response = process_worker_request(
        {
            "op": "observe",
            "route_receipt": route,
            "task_event": event(
                "typed-relation:turn",
                "task_observed",
                task_key="thread-typed-relation",
                objective=source_text,
                binding_required=True,
                relation_nonce=nonce,
                source_units=[
                    {
                        "id": source_id,
                        "source_role": "current_user_request",
                        "text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                    }
                ],
                turn_relation_receipt={
                    "schema": "cbh.turn_relation_receipt.v1",
                    "relation_nonce": nonce,
                    "candidate_task_id": active["capsule_id"],
                    "expected_goal_revision": active["goal_revision"],
                    "relation_kind": "refine",
                    "target_task_id": active["capsule_id"],
                    "source_unit_ids": [source_id],
                    "ambiguity_codes": [],
                },
            ),
            "capsule": active,
        }
    )

    assert response["decision"]["decision"] == "continue"
    assert response["capsule"]["goal_revision"] == active["goal_revision"] + 1
    assert response["capsule"]["goal_deltas"][-1]["text"] == source_text
    assert response["capsule"]["turn_relation"]["source"] == "typed_receipt"


def test_ambiguous_refinement_receipt_cannot_mutate_the_active_goal() -> None:
    route = write_route()
    active = new_task_capsule(
        route,
        event(
            "ambiguous-relation:start",
            "task_observed",
            task_key="thread-ambiguous-relation",
            objective="完成原任务",
            acceptance_criteria=[{"id": "verified", "text": "原任务已验证"}],
        ),
    )
    source_text = "补充验收，也可能是新任务"
    source_id = "ambiguous-relation:turn:user-request"
    nonce = "ambiguous-relation-nonce"
    response = process_worker_request(
        {
            "op": "observe",
            "route_receipt": route,
            "task_event": event(
                "ambiguous-relation:turn",
                "task_observed",
                task_key="thread-ambiguous-relation",
                objective=source_text,
                event_origin="current_user_input",
                relation_nonce=nonce,
                source_units=[
                    {
                        "id": source_id,
                        "source_role": "current_user_request",
                        "text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                    }
                ],
                turn_relation_receipt={
                    "schema": "cbh.turn_relation_receipt.v1",
                    "relation_nonce": nonce,
                    "candidate_task_id": active["capsule_id"],
                    "expected_goal_revision": active["goal_revision"],
                    "relation_kind": "refine",
                    "target_task_id": active["capsule_id"],
                    "source_unit_ids": [source_id],
                    "ambiguity_codes": ["possible_new_task"],
                },
            ),
            "capsule": active,
        }
    )

    assert response["decision"]["decision"] == "safe_readonly"
    assert response["capsule"] == active
    assert response["transition"]["changed"] is False


def test_first_substantive_event_is_applied_when_capsule_is_created() -> None:
    capsule, transition = initialize_task_capsule(
        write_route(),
        event(
            "write:first",
            "write_result_received",
            objective="写入并验证文件",
            acceptance_criteria=[
                {"id": "write_applied", "text": "请求的写入结果已核对"},
            ],
            acceptance_id="write_applied",
            postcondition_satisfied=False,
        ),
    )
    assert transition is not None
    assert capsule["lifecycle"] == "VERIFYING"
    assert capsule["acceptance_criteria"][0]["status"] == "inferred"
    assert "write:first" in capsule["applied_event_ids"]


def test_plan_progress_is_separate_from_the_acceptance_contract() -> None:
    capsule = new_task_capsule(
        write_route(),
        event(
            "plan:start",
            "task_observed",
            objective="实现并验证功能",
            acceptance_criteria=[
                {"id": "delivery_verified", "text": "交付物通过独立验证"},
            ],
        ),
    )
    transition = apply_task_event(
        capsule,
        event(
            "plan:update",
            "progress_snapshot",
            plan_steps=[
                {"id": "plan-0", "text": "实现功能", "status": "inferred"},
                {"id": "plan-1", "text": "运行验证", "status": "unknown"},
            ],
            current_step="运行验证",
            next_action="运行验证",
        ),
    )
    updated = transition["capsule"]
    assert updated["lifecycle"] == "ACTIVE"
    assert updated["next_action"] == "运行验证"
    assert not updated["verified_completed"]
    assert updated["acceptance_criteria"] == [
        {
            "id": "delivery_verified",
            "text": "交付物通过独立验证",
            "status": "unknown",
        }
    ]
    assert updated["plan_steps"] == [
        {"id": "plan-0", "text": "实现功能", "status": "inferred"},
        {"id": "plan-1", "text": "运行验证", "status": "unknown"},
    ]


def test_fifteen_step_packing_plan_arms_persistent_continuity_and_keeps_order() -> None:
    plan_steps = [
        {
            "id": f"task-{index}",
            "text": f"Task {index}: 装柜算法改进步骤",
            "status": "completed" if index == 1 else "in_progress" if index == 2 else "pending",
        }
        for index in range(1, 16)
    ]
    route = {
        "edit_operation_profile": "none",
        "memory_mode": "none",
        "tool_surface_need": "host_plan_event",
        "action_bindings": [],
    }
    response = process_worker_request(
        {
            "op": "observe",
            "route_receipt": route,
            "task_event": event(
                "packing-plan:updated",
                "progress_snapshot",
                task_key="thread-packing",
                objective="装柜算法优化方案",
                plan_steps=plan_steps,
            ),
            "capsule": None,
        }
    )

    assert response["decision"]["decision"] == "arm"
    assert "plan_observed" in response["decision"]["reasons"]
    assert response["capsule"]["persistence_scope"] == "cross_turn"
    assert response["capsule"]["plan_steps"] == plan_steps
    assert response["capsule"]["next_action"] == "Task 2: 装柜算法改进步骤"
    assert response["capsule"]["next_action_reason"] == "earliest_incomplete_plan_step"
    assert response["capsule"]["acceptance_criteria"] == []
    assert response["persistence"] == "none"
    assert response["dynamic_reminders"][0]["trigger"] == "plan_alignment_review_required"
    control = json.loads(
        response["additional_context_entries"]["control"]["value"].split("\n", 1)[1]
    )
    assert control["required_action_code"] == "continue_pending_plan_before_completion"


def test_tool_result_with_pending_plan_projects_verification_and_next_step() -> None:
    current = new_task_capsule(
        {"tool_surface_need": "host_plan_event"},
        event(
            "packing-plan:start",
            "progress_snapshot",
            objective="装柜算法优化方案",
            plan_steps=[
                {"id": "task-1", "text": "Task 1: 取得完整计划", "status": "completed"},
                {"id": "task-2", "text": "Task 2: 修复布局算法", "status": "in_progress"},
                {"id": "task-3", "text": "Task 3: 验证 release", "status": "pending"},
            ],
        ),
    )
    transition = apply_task_event(
        current,
        event(
            "packing-plan:tool-result",
            "tool_result_received",
            outcome="completed",
            postcondition_satisfied=False,
        ),
    )
    reminders = build_dynamic_reminders(transition["capsule"], transition)

    assert transition["capsule"]["next_action"] == "Task 2: 修复布局算法"
    assert reminders[0]["trigger"] == "plan_continuation_required"
    assert "verify the current result" in reminders[0]["required_action"]
    assert "Task 2: 修复布局算法" in reminders[0]["required_action"]


def test_explicit_marker_cannot_supersede_stale_working_set_without_receipt() -> None:
    stale = new_task_capsule(
        write_route(),
        event(
            "stale:start",
            "task_observed",
            objective="修复旧版 R5 路由",
            acceptance_criteria=[{"id": "old-r5", "text": "旧 R5 修复完成"}],
        ),
    )
    stale = apply_task_event(
        stale,
        event(
            "stale:failure",
            "unchanged_dispatch_repeated",
            subject_signature="old-dispatch",
            error_class="host_error_observed",
        ),
    )["capsule"]

    source_text = "比较数据库记忆与语义文件记忆"
    response = process_worker_request(
        {
            "op": "observe",
            "route_receipt": {
                "edit_operation_profile": "read_only",
                "memory_mode": "none",
                "tool_surface_need": "none",
                "action_bindings": [],
            },
            "task_event": event(
                "fresh:start",
                "task_observed",
                objective=source_text,
                intent_kind="new_task",
                intent_confidence="high",
                intent_source="explicit_marker",
                intent_reason_codes=["explicit_global_replacement"],
                event_origin="current_user_input",
                relation_nonce="explicit-marker-is-not-authority",
                source_units=[
                    {
                        "id": "fresh:start:user-request",
                        "source_role": "current_user_request",
                        "text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                    }
                ],
                reviewed_against={
                    "capsule_id": stale["capsule_id"],
                    "goal_revision": stale["goal_revision"],
                },
            ),
            "capsule": stale,
        }
    )

    assert response["decision"]["decision"] == "safe_readonly"
    assert response["capsule"] == stale
    assert response["transition"]["changed"] is False


def test_continue_ack_does_not_replace_the_active_goal() -> None:
    capsule = new_task_capsule(
        write_route(),
        event("ack:start", "task_observed", objective="修复并验证工作记忆"),
    )

    source_text = "可以，继续"
    response = process_worker_request(
        {
            "op": "observe",
            "route_receipt": write_route(),
            "task_event": event(
                "ack:continue",
                "task_observed",
                objective=source_text,
                intent_kind="continue_ack",
                event_origin="current_user_input",
                source_units=[
                    {
                        "id": "ack:continue:user-request",
                        "source_role": "current_user_request",
                        "text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                    }
                ],
            ),
            "capsule": capsule,
        }
    )

    assert response["capsule"]["objective"] == "修复并验证工作记忆"
    assert response["decision"]["decision"] == "safe_readonly"
    assert response["capsule"] == capsule


def test_all_verified_acceptance_items_retire_without_adapter_flag() -> None:
    capsule = new_task_capsule(
        write_route(),
        event(
            "auto-retire:start",
            "task_observed",
            objective="完成并验证唯一交付物",
            acceptance_criteria=[{"id": "done", "text": "交付物已验证"}],
        ),
    )

    verified = apply_task_event(
        capsule,
        event(
            "auto-retire:verified",
            "verifier_completed",
            acceptance_id="done",
            postcondition_satisfied=True,
        ),
    )["capsule"]

    assert verified["lifecycle"] == "RETIRED"
    assert verified["retirement"] == {
        "outcome": "completed",
        "reason": "all_acceptance_items_verified",
        "evidence_refs": [],
    }


def test_retirement_preserves_bounded_structured_evidence_references() -> None:
    capsule = new_task_capsule(
        write_route(),
        event(
            "structured-ref:start",
            "task_observed",
            objective="核验一条可回钻证据",
            acceptance_criteria=[{"id": "verified", "text": "证据已核验"}],
        ),
    )
    evidence_ref = {
        "source_id": "codex-session:abc",
        "resolved_path": "C:/archive/session.jsonl",
        "line": 42,
        "line_sha256_16": "0123456789abcdef",
        "status": "relocated_verified",
        "raw_payload": "must-not-be-persisted",
    }
    retired = apply_task_event(
        capsule,
        event(
            "structured-ref:verified",
            "verifier_completed",
            acceptance_id="verified",
            postcondition_satisfied=True,
            evidence_refs=[evidence_ref],
        ),
    )["capsule"]

    expected = {key: value for key, value in evidence_ref.items() if key != "raw_payload"}
    assert retired["evidence_refs"] == [expected]
    assert retired["retirement"]["evidence_refs"] == [expected]


def test_refinement_and_correction_are_source_bound_goal_deltas() -> None:
    capsule = new_task_capsule(
        write_route(),
        event("delta:start", "task_observed", objective="实现并验证工作记忆"),
    )

    refined = apply_task_event(
        capsule,
        event(
            "delta:refine",
            "task_observed",
            objective="再补充重启后的精确恢复",
            intent_kind="refine",
        ),
    )["capsule"]
    corrected = apply_task_event(
        refined,
        event(
            "delta:correction",
            "task_observed",
            objective="不对，恢复时必须校验最新 head",
            intent_kind="correction",
        ),
    )["capsule"]

    assert corrected["objective"] == "实现并验证工作记忆"
    assert corrected["goal_revision"] == 3
    assert corrected["goal_deltas"] == [
        {
            "kind": "refine",
            "text": "再补充重启后的精确恢复",
            "source_ref": "delta:refine",
        },
        {
            "kind": "correction",
            "text": "不对，恢复时必须校验最新 head",
            "source_ref": "delta:correction",
        },
    ]


def test_context_epoch_changes_only_for_global_or_validity_changes() -> None:
    initial_anchor = {
        "schema": "cbh.workspace_fact_anchor.v1",
        "workspace_id_sha256": "a" * 64,
        "source": "git",
        "git_head": "1" * 40,
        "git_branch": "main",
        "worktree_state_sha256": "b" * 64,
        "observed_at": "2026-08-24T00:00:00Z",
    }
    capsule = new_task_capsule(
        write_route(),
        event(
            "epoch:start",
            "task_observed",
            objective="保持全局目标并完成验证",
            workspace_anchor=initial_anchor,
        ),
    )
    initial_epoch = capsule["context_epoch"]["epoch_id"]

    progress_transition = apply_task_event(
        capsule,
        event(
            "epoch:progress",
            "tool_dispatched",
            current_step="读取实现",
            workspace_anchor={
                **initial_anchor,
                "worktree_state_sha256": "c" * 64,
                "observed_at": "2026-08-24T00:01:00Z",
            },
            workspace_change_expected=True,
        ),
    )
    progressed = progress_transition["capsule"]
    assert progressed["context_epoch"]["epoch_id"] == initial_epoch
    assert progressed["workspace_review_required"] is False
    progress_reminders = build_dynamic_reminders(progressed, progress_transition)
    assert progress_reminders[0]["trigger"] == "action_alignment_review_required"
    assert progress_reminders[0]["timing"] == "next_model_boundary_after_dispatch"

    refined = apply_task_event(
        progressed,
        event(
            "epoch:refine",
            "task_observed",
            objective="再补充一项明确验收标准",
            intent_kind="refine",
            workspace_anchor={
                **initial_anchor,
                "worktree_state_sha256": "c" * 64,
                "observed_at": "2026-08-24T00:02:00Z",
            },
        ),
    )["capsule"]
    assert refined["context_epoch"]["epoch_id"] != initial_epoch
    context = build_task_capsule_context(refined, [])
    payload = json.loads(context["entry"]["value"].split("\n", 1)[1])
    control = json.loads(context["control_entry"]["value"].split("\n", 1)[1])
    assert payload["context_epoch_id"] == refined["context_epoch"]["epoch_id"]
    assert control["context_epoch_id"] == refined["context_epoch"]["epoch_id"]


def test_unexpected_workspace_change_keeps_goal_but_invalidates_old_progress() -> None:
    initial_anchor = {
        "schema": "cbh.workspace_fact_anchor.v1",
        "workspace_id_sha256": "a" * 64,
        "source": "git",
        "git_head": "1" * 40,
        "git_branch": "main",
        "worktree_state_sha256": "b" * 64,
        "observed_at": "2026-08-24T00:00:00Z",
    }
    capsule = new_task_capsule(
        write_route(),
        event(
            "workspace:start",
            "task_observed",
            objective="修改并验证目标文件",
            acceptance_criteria=[
                {"id": "verified", "text": "目标文件已验证"},
                {"id": "final-review", "text": "最终状态已复核"},
            ],
            workspace_anchor=initial_anchor,
        ),
    )
    verified = apply_task_event(
        capsule,
        event(
            "workspace:verified",
            "verifier_completed",
            acceptance_id="verified",
            postcondition_satisfied=True,
            workspace_anchor=initial_anchor,
            workspace_change_expected=True,
        ),
    )["capsule"]
    prior_epoch = verified["context_epoch"]["epoch_id"]

    changed_transition = apply_task_event(
        verified,
        event(
            "workspace:changed",
            "task_observed",
            objective="继续",
            intent_kind="continue_ack",
            workspace_anchor={
                **initial_anchor,
                "git_head": "2" * 40,
                "worktree_state_sha256": "d" * 64,
                "observed_at": "2026-08-24T01:00:00Z",
            },
        ),
    )
    changed = changed_transition["capsule"]

    assert changed["objective"] == "修改并验证目标文件"
    assert changed["acceptance_criteria"][0]["status"] == "verified"
    assert changed["workspace_review_required"] is True
    assert changed["workspace_review_reason"] == "workspace_anchor_changed"
    assert changed["context_epoch"]["epoch_id"] != prior_epoch
    context = build_task_capsule_context(changed, [])
    assert "workspace_revalidation_required" in context["control_entry"]["value"]
    assert "workspace_fact_status" in context["entry"]["value"]
    reminders = build_dynamic_reminders(changed, changed_transition)
    assert reminders[0]["trigger"] == "workspace_revalidation_required"
    assert reminders[0]["context_epoch_id"] == changed["context_epoch"]["epoch_id"]

    revalidated = apply_task_event(
        changed,
        event(
            "workspace:revalidated",
            "verifier_completed",
            acceptance_id="final-review",
            postcondition_satisfied=True,
            workspace_anchor=changed["workspace_anchor"],
            workspace_change_expected=True,
            workspace_revalidated=True,
        ),
    )["capsule"]
    assert revalidated["workspace_review_required"] is False
    assert revalidated["workspace_review_reason"] is None
    assert revalidated["context_epoch"]["epoch_id"] != changed["context_epoch"]["epoch_id"]


def test_working_frame_preserves_goal_outputs_purpose_and_stop_condition() -> None:
    capsule = new_task_capsule(
        write_route(),
        event(
            "frame:start",
            "task_observed",
            objective="实现可靠的短期连续记忆",
            purpose="让模型始终知道为什么做、做到什么程度",
            required_outputs=[
                {"id": "runtime", "text": "可恢复的工作记忆运行时"},
                {"id": "tests", "text": "真实复发回归测试"},
            ],
            acceptance_criteria=[
                {"id": "latest-head", "text": "只恢复校验后的最新 head"},
            ],
            stop_condition="实现与回归均通过后停止扩张",
        ),
    )
    context = build_task_capsule_context(capsule, [], host_limits={"max_chars": 3_200})

    assert capsule["purpose"] == "让模型始终知道为什么做、做到什么程度"
    assert capsule["required_outputs"] == [
        {"id": "runtime", "text": "可恢复的工作记忆运行时", "status": "unknown"},
        {"id": "tests", "text": "真实复发回归测试", "status": "unknown"},
    ]
    assert capsule["stop_condition"] == "实现与回归均通过后停止扩张"
    assert "可恢复的工作记忆运行时" in context["entry"]["value"]
    assert "让模型始终知道为什么做、做到什么程度" in context["entry"]["value"]
    assert "实现与回归均通过后停止扩张" in context["entry"]["value"]


def test_dispatched_action_without_explicit_ids_remains_unbound() -> None:
    capsule = new_task_capsule(
        write_route(),
        event(
            "action:start",
            "task_observed",
            objective="实现并验证工作记忆",
            acceptance_criteria=[
                {"id": "behavior-fixed", "text": "旧目标不再污染新任务"},
                {"id": "tests-green", "text": "真实复发测试通过"},
            ],
        ),
    )

    active = apply_task_event(
        capsule,
        event(
            "action:dispatch",
            "tool_dispatched",
            current_step="修改连续记忆 reducer",
        ),
    )["capsule"]

    assert active["current_action"] == {
        "text": "修改连续记忆 reducer",
        "serves_output_ids": [],
        "serves_criterion_ids": [],
        "reason": "unbound_action",
        "binding_source": "unbound",
        "state_progress_allowed": False,
    }


@pytest.mark.parametrize("acceptance_id", [None, "missing-criterion"])
def test_result_without_an_exact_criterion_id_cannot_advance_progress(
    acceptance_id: str | None,
) -> None:
    capsule = new_task_capsule(
        write_route(),
        event(
            "unbound-result:start",
            "task_observed",
            objective="实现并验证工作记忆",
            acceptance_criteria=[
                {"id": "behavior-fixed", "text": "旧目标不再污染新任务"},
                {"id": "tests-green", "text": "真实复发测试通过"},
            ],
        ),
    )
    payload: dict[str, object] = {"postcondition_satisfied": True}
    if acceptance_id is not None:
        payload["acceptance_id"] = acceptance_id

    updated = apply_task_event(
        capsule,
        event("unbound-result:complete", "tool_result_received", **payload),
    )["capsule"]

    assert [item["status"] for item in updated["acceptance_criteria"]] == [
        "unknown",
        "unknown",
    ]


def test_retired_capsule_cannot_be_reactivated_by_a_late_event() -> None:
    capsule = new_task_capsule(
        write_route(),
        event(
            "retire:start",
            "task_observed",
            objective="完成一个动作",
            acceptance_criteria=[{"id": "done", "text": "done"}],
        ),
    )
    verified = apply_task_event(
        capsule,
        event(
            "retire:verify",
            "verifier_completed",
            acceptance_id="done",
            postcondition_satisfied=True,
            retire_if_complete=True,
        ),
    )["capsule"]
    late = apply_task_event(
        verified,
        event("retire:late", "candidate_selected", next_action="must not run"),
    )
    assert late["capsule"]["lifecycle"] == "RETIRED"
    assert late["changed"] is False
    assert late["event_outcome"] == "retired_ignored"


def test_host_task_identity_prevents_cross_task_event_contamination() -> None:
    capsule = new_task_capsule(
        write_route(),
        event("identity:start", "task_observed", task_key="thread-a", objective="修改 A"),
    )
    with pytest.raises(ValueError, match="task_identity_mismatch"):
        apply_task_event(
            capsule,
            event("identity:wrong", "tool_dispatched", task_key="thread-b"),
        )


@pytest.mark.parametrize(
    ("route", "task_event", "reason", "expected_decision"),
    [
        (write_route(), event("write:1", "task_observed", objective="修改 README"), "write_intent", "observe_once"),
        (
            {
                "edit_operation_profile": "read_only",
                "tool_surface_need": "browser",
                "action_bindings": [],
            },
            event("tool:1", "task_observed", objective="检查这个网页"),
            "tool_required",
            "observe_once",
        ),
        (
            {"edit_operation_profile": "read_only", "action_bindings": []},
            event("long:1", "task_observed", objective="分三阶段完成并持续验证", multi_stage=True),
            "multi_stage_task",
            "arm",
        ),
        (
            {"edit_operation_profile": "read_only", "action_bindings": []},
            event("resume:1", "task_observed", objective="继续之前未完成的任务", resume=True),
            "task_resume",
            "arm",
        ),
    ],
)
def test_write_tool_long_and_resume_tasks_arm(
    route: dict[str, object], task_event: dict[str, object], reason: str, expected_decision: str
) -> None:
    decision = decide_task_continuity(route, task_event)

    assert decision["decision"] == expected_decision
    assert reason in decision["reasons"]
    assert decision["host_delivery"] == "ready"


def test_runtime_reasons_are_declared_by_the_policy_contract() -> None:
    authoring = tomllib.loads(
        (HARNESS / "embedded_harness_policy.authoring.toml").read_text(encoding="utf-8")
    )
    declared = set(
        authoring["router_decision_contract"]["task_continuity_contract"][
            "activation_reasons"
        ]
    )
    probes = [
        (write_route(), event("reason:write", "task_observed", objective="修改文件")),
        ({"tool_surface_need": "web"}, event("reason:tool", "task_observed", objective="检查网页")),
        ({}, event("reason:multi", "task_observed", objective="分阶段完成", multi_stage=True)),
        ({}, event("reason:resume", "task_observed", objective="继续", resume=True)),
        ({}, event("reason:long", "task_observed", objective="执行", long_running=True)),
        ({}, event("reason:open", "task_observed", objective="任务", open_loops=["x"])),
        ({}, event("reason:prior", "task_observed", objective="任务", prior_failure=True)),
        ({}, event("reason:explicit", "task_observed", objective="任务", continuity_requested=True)),
        (
            {"tool_surface_need": "host_plan_event"},
            event(
                "reason:plan",
                "progress_snapshot",
                objective="任务",
                plan_steps=[{"id": "plan-1", "text": "执行计划", "status": "pending"}],
            ),
        ),
    ]
    emitted: set[str] = set()
    for route, task_event in probes:
        emitted.update(decide_task_continuity(route, task_event)["reasons"])
    capsule = new_task_capsule(write_route(), event("reason:active", "task_observed", objective="修改"))
    emitted.update(decide_task_continuity({}, event("reason:next", "task_observed", objective="任务"), capsule)["reasons"])
    assert emitted <= declared
    assert declared <= emitted


def test_write_capsule_requires_both_application_and_verification() -> None:
    observed = event(
        "write:observed",
        "task_observed",
        objective="更新配置并确认解析成功",
        acceptance_criteria=[
            {"id": "write_applied", "text": "目标写入已应用"},
            {"id": "write_verified", "text": "写入结果已验证"},
        ],
    )
    capsule = new_task_capsule(write_route(), observed)
    assert capsule["lifecycle"] == "ARMED"

    active = apply_task_event(
        capsule,
        event("write:selected", "candidate_selected", next_action="应用最小补丁"),
    )["capsule"]
    assert active["lifecycle"] == "ACTIVE"

    applied = apply_task_event(
        active,
        event(
            "write:result",
            "write_result_received",
            outcome="completed",
            acceptance_id="write_applied",
            postcondition_satisfied=True,
        ),
    )["capsule"]
    assert applied["lifecycle"] == "VERIFYING"
    assert [item["id"] for item in applied["verified_completed"]] == ["write_applied"]
    assert [item["id"] for item in applied["remaining_work"]] == ["write_verified"]

    verified = apply_task_event(
        applied,
        event(
            "write:verified",
            "verifier_completed",
            outcome="completed",
            acceptance_id="write_verified",
            postcondition_satisfied=True,
            retire_if_complete=True,
        ),
    )["capsule"]
    assert verified["lifecycle"] == "RETIRED"
    assert verified["remaining_work"] == []


def test_success_without_semantic_postcondition_is_only_inferred() -> None:
    capsule = new_task_capsule(
        {"tool_surface_need": "shell", "edit_operation_profile": "read_only"},
        event(
            "tool:observed",
            "task_observed",
            objective="检查项目状态",
            acceptance_criteria=[{"id": "status_checked", "text": "项目状态已核对"}],
        ),
    )
    capsule = apply_task_event(
        capsule,
        event("tool:dispatch", "tool_dispatched", next_action="运行状态检查"),
    )["capsule"]

    transition = apply_task_event(
        capsule,
        event(
            "tool:result",
            "tool_result_received",
            outcome="completed",
            acceptance_id="status_checked",
            postcondition_satisfied=False,
        ),
    )

    assert transition["capsule"]["verified_completed"] == []
    assert [item["id"] for item in transition["capsule"]["inferred_progress"]] == [
        "status_checked"
    ]
    assert transition["capsule"]["lifecycle"] == "VERIFYING"


def test_duplicate_event_is_idempotent() -> None:
    capsule = new_task_capsule(
        write_route(),
        event("dup:observed", "task_observed", objective="修改一个文件"),
    )
    first = apply_task_event(capsule, event("dup:event", "tool_dispatched"))
    second = apply_task_event(first["capsule"], event("dup:event", "tool_dispatched"))

    assert first["changed"] is True
    assert second["changed"] is False
    assert second["capsule"] == first["capsule"]


def test_unchanged_failure_reminder_is_deduplicated_by_revision() -> None:
    capsule = new_task_capsule(
        {"tool_surface_need": "shell", "edit_operation_profile": "read_only"},
        event("failure:observed", "task_observed", objective="执行并验证命令"),
    )
    transition = apply_task_event(
        capsule,
        event(
            "failure:repeat",
            "unchanged_dispatch_repeated",
            subject_signature="a" * 64,
            error_class="parser_error",
        ),
    )
    first = build_dynamic_reminders(transition["capsule"], transition)
    second = build_dynamic_reminders(first[0]["capsule_snapshot"], transition)

    assert len(first) == 1
    assert first[0]["trigger"] == "unchanged_dispatch_repeated"
    assert second == []


def test_continuity_never_creates_or_consumes_authority() -> None:
    capsule = new_task_capsule(
        write_route(),
        event("auth:observed", "task_observed", objective="执行受保护写入"),
    )
    transition = apply_task_event(
        capsule,
        event(
            "auth:pending",
            "verifier_pending",
            blocking_condition="pending_user_confirmation",
        ),
    )

    assert transition["capsule"]["lifecycle"] == "VERIFYING"
    assert transition["capsule"]["blocking_condition"] == "pending_user_confirmation"
    assert transition["capsule"]["authority"] == {
        "granted": False,
        "consumed": False,
        "source": "none",
    }


def test_text_transport_pages_round_trip_and_binds_cursor() -> None:
    payload = "甲乙丙丁" * 40
    plan = plan_transport(
        None,
        {"kind": "text", "original_chars": len(payload), "original_items": 1},
        {"max_chars": 37, "max_items": 10},
    )
    pages: list[str] = []
    cursor = None
    while True:
        page = page_result(payload, plan, cursor)
        pages.append(page["content"])
        if page["next_cursor"] is None:
            break
        cursor = page["next_cursor"]

    rebuilt = "".join(pages)
    assert rebuilt == payload
    assert hashlib.sha256(rebuilt.encode("utf-8")).hexdigest() == page["full_result_sha256"]
    assert all(len(part) <= 37 for part in pages)

    with pytest.raises(ValueError, match="cursor_result_hash_mismatch"):
        page_result(payload, plan, {"result_sha256": "0" * 64, "next_char": 1})


def test_item_transport_respects_item_and_character_limits() -> None:
    items = [{"id": index, "text": f"item-{index}"} for index in range(11)]
    plan = plan_transport(
        None,
        {"kind": "items", "original_items": len(items), "original_chars": 500},
        {"max_chars": 90, "max_items": 3},
    )
    rebuilt: list[dict[str, object]] = []
    cursor = None
    while True:
        page = page_result(items, plan, cursor)
        rebuilt.extend(page["items"])
        assert len(page["items"]) <= 3
        if page["next_cursor"] is None:
            break
        cursor = page["next_cursor"]

    assert rebuilt == items
    canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == page["full_result_sha256"]


def test_typed_media_payload_is_never_serialized_into_context() -> None:
    secret = "base64-secret-payload"
    plan = plan_transport(
        None,
        {"kind": "items", "original_items": 1, "original_chars": 1000},
        {"max_chars": 1000, "max_items": 10},
    )
    page = page_result(
        [{"type": "image", "mimeType": "image/png", "data": secret}],
        plan,
    )
    serialized = json.dumps(page, ensure_ascii=False)

    assert secret not in serialized
    assert page["items"][0]["inline_payload_omitted_from_text"] is True


def test_model_context_contains_progress_not_chain_of_thought() -> None:
    capsule = new_task_capsule(
        write_route(),
        event("context:observed", "task_observed", objective="修改并验证配置"),
    )
    context = build_task_capsule_context(capsule, [], host_limits={"max_chars": 1800})

    assert context["entry"]["kind"] == "untrusted"
    assert context["control_entry"]["kind"] == "application"
    assert "修改并验证配置" not in context["control_entry"]["value"]
    assert "write_preflight_review_required" in context["control_entry"]["value"]
    assert "修改并验证配置" in context["entry"]["value"]
    assert "chain-of-thought" not in context["entry"]["value"].lower()
    assert context["char_count"] <= 1800


def test_task_context_exposes_projection_identity_and_causal_relation() -> None:
    current = new_task_capsule(
        write_route(),
        event(
            "projection:start",
            "task_observed",
            objective="实现并验证运行时投影",
            acceptance_criteria=[
                {"id": "projection-verified", "text": "运行时投影已验证"},
            ],
        ),
    )
    current = apply_task_event(
        current,
        event(
            "projection:dispatch",
            "tool_dispatched",
            current_step="commandExecution",
        ),
    )["capsule"]
    context = build_task_capsule_context(current, [])
    evidence = json.loads(context["entry"]["value"].split("\n", 1)[1])
    control = json.loads(context["control_entry"]["value"].split("\n", 1)[1])

    assert len(evidence["runtime_projection_id"]) == 64
    assert len(control["runtime_projection_source_digest"]) == 64
    assert len(evidence["full_capsule_sha256"]) == 64
    assert evidence["causal_relation"] == {
        "status": "unbound",
        "specificity": "host_kind_only",
        "serves_output_ids": [],
        "serves_criterion_ids": [],
        "reason": "unbound_action",
        "binding_source": "unbound",
        "state_progress_allowed": False,
    }
    assert control["runtime_projection_id"] == evidence["runtime_projection_id"]
    assert "实现并验证运行时投影" not in context["control_entry"]["value"]


def test_worker_returns_separate_small_dynamic_projection() -> None:
    current = new_task_capsule(
        write_route(),
        event(
            "dynamic:start",
            "task_observed",
            objective="修改并验证动态提醒投影",
            acceptance_criteria=[
                {"id": "postcondition", "text": "语义后置条件已验证"},
            ],
        ),
    )
    response = process_worker_request(
        {
            "op": "observe",
            "response_profile": "compact",
            "route_receipt": write_route(),
            "task_event": event(
                "dynamic:result",
                "tool_result_received",
                acceptance_id="postcondition",
                postcondition_satisfied=False,
            ),
            "capsule": current,
        }
    )

    dynamic = response["dynamic_context_entries"]
    dynamic_text = json.dumps(dynamic, ensure_ascii=False)
    normal_text = json.dumps(response["additional_context_entries"], ensure_ascii=False)
    assert dynamic["control"]["kind"] == "application"
    assert len(response["dynamic_projection_id"]) == 64
    assert dynamic["evidence"]["kind"] == "untrusted"
    assert "missing_postcondition" in dynamic_text
    assert "修改并验证动态提醒投影" not in dynamic_text
    assert len(dynamic_text) < len(normal_text)


def test_model_context_omits_empty_optionals_and_duplicate_initial_delta() -> None:
    """Catches token growth from serializing empty state and the objective twice."""

    capsule = new_task_capsule(
        write_route(),
        event(
            "context:compact",
            "task_observed",
            objective="修改并验证路由器",
            intent_kind="new_task",
            turn_ref="turn:compact",
        ),
    )
    context = build_task_capsule_context(capsule, [])
    payload = json.loads(context["entry"]["value"].split("\n", 1)[1])

    assert payload["objective"] == "修改并验证路由器"
    assert "remaining_work" not in payload
    assert "next_action" not in payload
    assert "next_action_reason" not in payload
    assert payload["full_capsule_sha256"]
    for redundant in (
        "constraints",
        "goal_deltas",
        "inferred_progress",
        "memory_working_set",
        "plan_steps",
        "reminders",
        "resume_entry",
        "uncovered_counts",
        "unresolved_failures",
        "verified_completed",
    ):
        assert redundant not in payload
    assert "last_user_delta" not in payload


def test_context_reports_unforwarded_progress_instead_of_silent_list_clipping() -> None:
    capsule = new_task_capsule(
        write_route(),
        event(
            "many:1",
            "task_observed",
            objective="完成多阶段写入",
            acceptance_criteria=[
                {"id": f"criterion-{index}", "text": f"step {index}"}
                for index in range(20)
            ],
        ),
    )
    context = build_task_capsule_context(capsule, [], host_limits={"max_chars": 3_200})
    assert "uncovered_counts" in context["entry"]["value"]
    assert "full_capsule_sha256" in context["entry"]["value"]


def test_pending_dynamic_reminder_is_rendered_on_the_next_supported_turn() -> None:
    capsule = new_task_capsule(
        write_route(),
        event("reminder:start", "task_observed", task_key="thread-a", objective="修改文件"),
    )
    response = process_worker_request(
        {
            "op": "observe",
            "route_receipt": {},
            "task_event": event(
                "reminder:next",
                "task_observed",
                task_key="thread-a",
                objective="继续",
            ),
            "capsule": capsule,
            "pending_reminders": [
                {
                    "schema": "cbh.dynamic_reminder.v1",
                    "trigger": "missing_postcondition",
                    "required_action": "verify the semantic postcondition",
                    "expires_when": "postcondition_verified",
                }
            ],
            "host_limits": {"max_chars": 3000, "max_tokens": 850},
        }
    )
    assert "missing_postcondition" in response["additional_context_entry"]["value"]
    assert "verify the semantic postcondition" in response["additional_context_entry"]["value"]


def test_initial_write_turn_projects_pre_action_attention_without_waiting_for_tool_event() -> None:
    response = process_worker_request(
        {
            "op": "observe",
            "route_receipt": write_route(),
            "task_event": event(
                "attention:start",
                "task_observed",
                task_key="thread-attention",
                objective="修改配置并验证写入结果",
            ),
            "capsule": None,
            "host_limits": {"max_chars": 3000, "max_tokens": 850},
        }
    )
    control = json.loads(
        response["additional_context_entries"]["control"]["value"].split("\n", 1)[1]
    )

    assert control["required_action_code"] == "write_preflight_review_required"
    assert control["required_action_timing"] == "before_first_action"
    assert response["dynamic_reminders"] == []
    assert response["decision"]["decision"] == "observe_once"
    assert response["persistence"] == "turn_local"


def test_transport_fallback_preserves_real_semantic_review_state() -> None:
    current = new_task_capsule(
        write_route(),
        event(
            "transport-semantic:start",
            "task_observed",
            objective="完成一个明确的长程任务",
            continuity_requested=True,
            acceptance_criteria=[
                {"id": f"criterion-{index}", "text": "验收内容" * 80}
                for index in range(12)
            ],
        ),
    )
    assert current["semantic_review_required"] is False

    context = build_task_capsule_context(
        current,
        [],
        host_limits={"max_chars": 900, "max_tokens": 850},
    )
    evidence = json.loads(context["entry"]["value"].split("\n", 1)[1])
    control = json.loads(context["control_entry"]["value"].split("\n", 1)[1])

    assert context["delivery"] in {"continuation_required", "semantic_review_required"}
    assert evidence["semantic_review_required"] is False
    assert control["semantic_review_required"] is False


def test_plan_update_emits_alignment_reminder_before_next_plan_action() -> None:
    current = new_task_capsule(
        write_route(),
        event("plan-reminder:start", "task_observed", objective="修改并验证配置"),
    )
    transition = apply_task_event(
        current,
        event(
            "plan-reminder:update",
            "progress_snapshot",
            plan_steps=[
                {"id": "inspect", "text": "检查配置", "status": "inferred"},
                {"id": "patch", "text": "修改配置", "status": "unknown"},
            ],
            next_action="修改配置",
        ),
    )

    reminders = build_dynamic_reminders(transition["capsule"], transition)

    assert reminders[0]["trigger"] == "plan_alignment_review_required"
    assert reminders[0]["timing"] == "before_next_plan_action"
    assert "required output" in reminders[0]["required_action"]
    assert "acceptance criterion" in reminders[0]["required_action"]


def test_fallback_action_binding_triggers_review_and_discloses_dispatch_timing() -> None:
    current = new_task_capsule(
        write_route(),
        event("action-reminder:start", "task_observed", objective="修改并验证配置"),
    )
    transition = apply_task_event(
        current,
        event(
            "action-reminder:dispatch",
            "tool_dispatched",
            current_step="fileChange",
        ),
    )

    assert transition["capsule"]["current_action"]["binding_source"] == "unbound"
    reminders = build_dynamic_reminders(transition["capsule"], transition)
    assert reminders[0]["trigger"] == "action_alignment_review_required"
    assert reminders[0]["timing"] == "next_model_boundary_after_dispatch"
    assert "already-started tool" in reminders[0]["required_action"]


def test_unknown_explicit_binding_cannot_suppress_action_alignment_review() -> None:
    current = new_task_capsule(
        write_route(),
        event("explicit-binding:start", "task_observed", objective="修改并验证配置"),
    )
    transition = apply_task_event(
        current,
        event(
            "explicit-binding:selected",
            "candidate_selected",
            next_action="修改配置",
            serves_criterion_ids=["criterion-that-does-not-exist"],
        ),
    )

    assert transition["capsule"]["current_action"]["binding_source"] == (
        "explicit_event_unresolved"
    )
    assert transition["capsule"]["current_action"]["unresolved_binding_ids"] == [
        "criterion-that-does-not-exist"
    ]
    reminders = build_dynamic_reminders(transition["capsule"], transition)
    assert reminders[0]["trigger"] == "action_alignment_review_required"
    assert reminders[0]["timing"] == "before_tool_dispatch"


def test_known_explicit_binding_suppresses_redundant_alignment_review() -> None:
    current = new_task_capsule(
        write_route(),
        event(
            "known-binding:start",
            "task_observed",
            objective="修改并验证配置",
            acceptance_criteria=[
                {"id": "write_applied", "text": "配置修改已验证"},
            ],
        ),
    )
    transition = apply_task_event(
        current,
        event(
            "known-binding:selected",
            "candidate_selected",
            next_action="修改配置",
            serves_criterion_ids=["write_applied"],
        ),
    )

    assert transition["capsule"]["current_action"]["binding_source"] == "explicit_event"
    assert build_dynamic_reminders(transition["capsule"], transition) == []


def test_finalization_with_correction_emits_capability_neutral_review_reminder() -> None:
    current = new_task_capsule(
        write_route(),
        event(
            "finalization-review:start",
            "task_observed",
            objective="完成代码修改并给出最终交付说明",
        ),
    )
    current["goal_deltas"] = [
        {"kind": "correction", "text": "旧交付措辞已被否决"},
    ]
    current["remaining_work"] = []
    transition = {
        "event_type": "stage_exit",
        "transition_reasons": [],
    }
    reminders = build_dynamic_reminders(current, transition)

    assert reminders[0]["trigger"] == "finalization_surface_review_required"
    assert reminders[0]["required_action"] == (
        "review reader-facing final surfaces against the accepted state, remove "
        "rejected-alternative residue, then read them back"
    )
    assert reminders[0]["timing"] == "before_final_delivery"
    assert reminders[0]["context_epoch_id"] == current["context_epoch"]["epoch_id"]


def test_jsonl_worker_keeps_state_external_and_returns_one_context_entry() -> None:
    request = {
        "id": "worker:1",
        "op": "observe",
        "route_receipt": write_route(),
        "task_event": event("worker:event", "task_observed", objective="修改一个文件"),
        "capsule": None,
        "host_limits": {"max_chars": 1600, "max_items": 20},
    }
    completed = subprocess.run(
        [sys.executable, str(HARNESS / "task_continuity.py"), "--worker-once"],
        input=json.dumps(request, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    response = json.loads(completed.stdout)
    assert response["id"] == "worker:1"
    assert response["result"]["decision"]["decision"] == "observe_once"
    assert response["result"]["capsule"]["lifecycle"] == "ARMED"
    assert response["result"]["persistence"] == "turn_local"
    assert response["result"]["additional_context_entry"]["kind"] == "untrusted"
    assert response["result"]["additional_context_entries"]["control"]["kind"] == "application"
    assert response["result"]["additional_context_entries"]["evidence"]["kind"] == "untrusted"


def test_compact_worker_response_removes_exact_duplicate_payloads() -> None:
    """Catches duplicate capsule/context copies in adapter-facing JSONL responses."""

    route = write_route()
    capsule = new_task_capsule(
        route,
        event("compact:start", "task_observed", objective="修改并验证文件"),
    )
    response = process_worker_request(
        {
            "op": "observe",
            "response_profile": "compact",
            "route_receipt": route,
            "task_event": event(
                "compact:progress",
                "progress_snapshot",
                plan_steps=[
                    {"id": "inspect", "text": "检查", "status": "completed"},
                    {"id": "patch", "text": "修改", "status": "in_progress"},
                    {"id": "verify", "text": "验证", "status": "pending"},
                ],
            ),
            "capsule": capsule,
            "host_limits": {"max_chars": 3_200, "max_tokens": 900},
        }
    )

    assert response["capsule"]["progress_revision"] == 2
    assert response["additional_context_entries"]["evidence"]["kind"] == "untrusted"
    assert response["transport_receipt"]["delivery"] in {
        "ready",
        "continuation_required",
    }
    transport = response["transport_receipt"]
    assert transport["total_char_count"] == (
        transport["evidence_char_count"] + transport["control_char_count"]
    )
    assert transport["total_estimated_tokens"] == (
        transport["evidence_estimated_tokens"]
        + transport["control_estimated_tokens"]
    )
    assert "additional_context_entry" not in response
    assert "capsule" not in response["transition"]


def test_page_result_reclamps_a_forged_oversized_transport_plan() -> None:
    page = page_result(
        "x" * 50_000,
        {
            "schema": "cbh.transport_plan.v1",
            "max_chars": 1_000_000,
            "max_items": 1_000_000,
        },
    )

    assert page["forwarded_chars"] == 20_000
    assert page["uncovered_chars"] == 30_000
    assert page["next_cursor"] is not None


def test_memory_working_set_preserves_conversation_and_ledger_navigation_handles() -> None:
    capsule = new_task_capsule(
        write_route(),
        event("memory-nav:start", "task_observed", objective="回顾之前的 CBH 记忆"),
    )
    receipt = {
        "selected_records": [],
        "retrieval": {"coverage_status": "complete"},
        "conversation_navigation": {
            "status": "resolved",
            "bundles": [
                {
                    "memory_id": "cbh-long-conversation",
                    "root_path": "C:/memory/cbh",
                    "registry_path": "C:/memory/registry.json",
                    "isolation": "link_only",
                    "selected_links": [{"link_id": "LINK-CBH-001"}],
                    "ledger": {
                        "index_path": "C:/memory/ledger/_LEDGER_INDEX.md",
                        "capsules_path": "C:/memory/ledger/capsules.jsonl",
                        "evidence_refs_path": "C:/memory/ledger/evidence_refs.jsonl",
                    },
                }
            ],
        },
    }
    updated = apply_task_event(
        capsule,
        event(
            "memory-nav:selected",
            "memory_context_selected",
            memory_query_type="history_reason",
            memory_consumption_receipt=receipt,
        ),
    )["capsule"]

    assert updated["memory_working_set"]["conversation_handles"] == [
        {
            "memory_id": "cbh-long-conversation",
            "root_path": "C:/memory/cbh",
            "registry_path": "C:/memory/registry.json",
            "isolation": "link_only",
            "selected_link_ids": ["LINK-CBH-001"],
            "ledger_index_path": "C:/memory/ledger/_LEDGER_INDEX.md",
            "ledger_capsules_path": "C:/memory/ledger/capsules.jsonl",
            "ledger_evidence_refs_path": "C:/memory/ledger/evidence_refs.jsonl",
        }
    ]


def test_only_a_verified_selected_evidence_handle_can_be_marked_opened() -> None:
    capsule = new_task_capsule(
        write_route(),
        event("evidence:start", "task_observed", objective="核对历史原始证据"),
    )
    selected = apply_task_event(
        capsule,
        event(
            "evidence:selected",
            "memory_context_selected",
            memory_query_type="history_reason",
            memory_consumption_receipt={
                "retrieval": {"coverage_status": "complete"},
                "selected_records": [
                    {
                        "record_id": "INC-001",
                        "source_id": "event:INC-001",
                        "evidence_handles": [
                            {
                                "source_id": "codex-session:abc",
                                "original_path": "C:/old/session.jsonl",
                                "line": 42,
                                "line_sha256_16": "0123456789abcdef",
                            }
                        ],
                    }
                ],
            },
        ),
    )["capsule"]
    opened = apply_task_event(
        selected,
        event(
            "evidence:opened",
            "memory_evidence_opened",
            evidence_ref={
                "source_id": "codex-session:abc",
                "resolved_path": "C:/archive/session.jsonl",
                "line": 42,
                "line_sha256_16": "0123456789abcdef",
                "status": "relocated_verified",
            },
        ),
    )["capsule"]

    assert opened["memory_working_set"]["opened_evidence_refs"] == [
        {
            "source_id": "codex-session:abc",
            "resolved_path": "C:/archive/session.jsonl",
            "line": 42,
            "line_sha256_16": "0123456789abcdef",
            "status": "relocated_verified",
        }
    ]
    with pytest.raises(ValueError, match="evidence_source_not_selected"):
        apply_task_event(
            opened,
            event(
                "evidence:unselected",
                "memory_evidence_opened",
                evidence_ref={
                    "source_id": "codex-session:other",
                    "resolved_path": "C:/archive/other.jsonl",
                    "status": "relocated_verified",
                },
            ),
        )
