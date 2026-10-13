"""Task-local continuity, progress, reminder, and transport primitives for CBH.

Calls remain process-local by default.  A host may explicitly opt into the
separate append-only ``.cumcwork`` adapter by supplying one exact task path;
the reducer itself remains deterministic and has no implicit file discovery.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from runtime_projection import classify_finalization_review_need, project_runtime


DECISION_SCHEMA = "cbh.task_continuity_decision.v1"
CAPSULE_SCHEMA = "cbh.task_capsule.v1"
EVENT_SCHEMA = "cbh.task_event.v1"
TRANSITION_SCHEMA = "cbh.task_transition.v1"
REMINDER_SCHEMA = "cbh.dynamic_reminder.v1"
TRANSPORT_SCHEMA = "cbh.transport_plan.v1"
PAGE_SCHEMA = "cbh.transport_page.v1"
WORKER_SCHEMA = "cbh.task_continuity_worker_response.v1"
CONTEXT_EPOCH_SCHEMA = "cbh.context_epoch.v1"
WORKSPACE_ANCHOR_SCHEMA = "cbh.workspace_fact_anchor.v1"
TURN_RELATION_RECEIPT_SCHEMA = "cbh.turn_relation_receipt.v1"

LIFECYCLES = {"DORMANT", "ARMED", "ACTIVE", "VERIFYING", "RETIRED"}
INTENT_KINDS = {
    "new_task",
    "refine",
    "continue_ack",
    "correction",
    "side_task",
    "ambiguous",
}
RELATION_CONFIDENCES = {"low", "medium", "high"}
RELATION_SOURCES = {
    "fallback",
    "explicit_marker",
    "semantic_review",
    "typed_receipt",
    "legacy_projection",
}
DEFAULT_MAX_CHARS = 20_000
DEFAULT_MAX_ITEMS = 100
MAX_EVENT_HISTORY = 256
MAX_OBJECTIVE_CHARS = 4_000
MAX_CONTEXT_LIST_ITEMS = 12

_WRITE_PROFILES = {
    "in_place_patch",
    "append_delta",
    "add_new_artifact",
    "section_replace",
    "full_rewrite",
    "supersede_with_link",
    "archive_or_move",
    "delete_record_content",
    "delete_from_disk",
}
_TOOL_ACTIONS = {
    "perform_external_research_route",
    "direct_outcome_first",
    "retrieve_matching_memory",
    "resolve_conversation_link",
    "prepare_task_local_correction_bundle",
}
_WRITE_PROMPT = re.compile(
    r"(?i)(修改|更新|修复|写入|保存|创建|新增|删除|部署|安装|提交|推送|配置|"
    r"\b(?:modify|update|fix|patch|write|save|create|add|delete|deploy|install|commit|push|configure)\b)"
)
_TOOL_PROMPT = re.compile(
    r"(?i)(检查|打开|搜索|运行|执行|验证|抓取|下载|调用|浏览|登录|"
    r"\b(?:inspect|check|open|search|run|execute|verify|fetch|download|browse|login|tool)\b)"
)
_NON_TOOL_HOST_SURFACES = {
    "",
    "none",
    "not_required",
    "host_lifecycle_event",
    "host_plan_event",
}
_PERSISTENT_REASONS = {
    "multi_stage_task",
    "task_resume",
    "long_running_task",
    "open_loop",
    "prior_failure",
    "explicit_request",
    "plan_observed",
}
_LEGACY_GOAL_REVISION_PROMPT = re.compile(
    r"(?i)(不对|不是.+而是|做偏了|判断逻辑.+窄|完全不符合预期|"
    r"重点(?:是|中的重点)|需求.+不是|应该.+而不是|"
    r"\b(?:correction|not .+ but|the requirement is|global goal)\b)"
)
_READ_ONLY_PROMPT = re.compile(
    r"(?i)(只读|不要修改|不修改|仅解释|只回答|讨论一下|"
    r"\b(?:read[- ]only|do not modify|explain only|discussion only)\b)"
)
_INLINE_PAYLOAD_KEYS = {"data", "blob", "bytes", "base64"}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_workspace_anchor(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    workspace_id = _bounded_text(value.get("workspace_id_sha256"), 128)
    if not workspace_id:
        return None
    source = str(value.get("source") or "path_only")
    if source not in {"git", "path_only"}:
        source = "path_only"
    identity = {
        "workspace_id_sha256": workspace_id,
        "source": source,
        "git_head": _bounded_text(value.get("git_head"), 160) or None,
        "git_branch": _bounded_text(value.get("git_branch"), 320) or None,
        "worktree_state_sha256": (
            _bounded_text(value.get("worktree_state_sha256"), 128) or None
        ),
    }
    return {
        "schema": WORKSPACE_ANCHOR_SCHEMA,
        **identity,
        "anchor_sha256": _sha256_text(_canonical_json(identity)),
        "observed_at": _bounded_text(value.get("observed_at"), 80) or None,
        "validity": str(value.get("validity") or "aligned"),
    }


def _derive_context_epoch(capsule: Mapping[str, Any]) -> dict[str, Any]:
    goal_anchor = (
        capsule.get("global_goal_anchor")
        if isinstance(capsule.get("global_goal_anchor"), Mapping)
        else {}
    )
    workspace = (
        capsule.get("workspace_anchor")
        if isinstance(capsule.get("workspace_anchor"), Mapping)
        else {}
    )
    identity = {
        "capsule_id": capsule.get("capsule_id"),
        "goal_revision": int(capsule.get("goal_revision") or 1),
        "global_goal_anchor_sha256": goal_anchor.get("objective_sha256"),
        "workspace_id_sha256": workspace.get("workspace_id_sha256"),
        "git_head": workspace.get("git_head"),
        "git_branch": workspace.get("git_branch"),
        "workspace_review_required": bool(
            capsule.get("workspace_review_required")
        ),
    }
    return {
        "schema": CONTEXT_EPOCH_SCHEMA,
        "epoch_id": _sha256_text(_canonical_json(identity)),
        "goal_revision": identity["goal_revision"],
        "global_goal_anchor_sha256": identity["global_goal_anchor_sha256"],
        "workspace_scope_sha256": _sha256_text(
            _canonical_json(
                {
                    "workspace_id_sha256": identity["workspace_id_sha256"],
                    "git_head": identity["git_head"],
                    "git_branch": identity["git_branch"],
                }
            )
        ),
        "workspace_review_required": identity["workspace_review_required"],
    }


def _reconcile_workspace_anchor(
    capsule: dict[str, Any],
    task_event: Mapping[str, Any],
    reasons: list[str],
) -> None:
    incoming = _normalize_workspace_anchor(task_event.get("workspace_anchor"))
    if incoming is None:
        return
    prior = _normalize_workspace_anchor(capsule.get("workspace_anchor"))
    expected_change = task_event.get("workspace_change_expected") is True
    changed = bool(
        prior
        and prior.get("anchor_sha256") != incoming.get("anchor_sha256")
    )
    if prior is None:
        incoming["validity"] = "aligned"
        capsule["workspace_anchor"] = incoming
        capsule["workspace_review_required"] = False
        capsule["workspace_review_reason"] = None
        return
    if changed and not expected_change:
        incoming["validity"] = "review_required"
        incoming["previous_anchor_sha256"] = prior.get("anchor_sha256")
        capsule["workspace_review_required"] = True
        capsule["workspace_review_reason"] = "workspace_anchor_changed"
        reasons.append("workspace_anchor_changed")
    else:
        incoming["validity"] = (
            "review_required"
            if capsule.get("workspace_review_required")
            else "aligned"
        )
    if (
        task_event.get("workspace_revalidated") is True
        and task_event.get("postcondition_satisfied") is True
    ):
        incoming["validity"] = "aligned"
        capsule["workspace_review_required"] = False
        capsule["workspace_review_reason"] = None
        reasons.append("workspace_anchor_revalidated")
    capsule["workspace_anchor"] = incoming


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _bounded_text(value: Any, limit: int = MAX_OBJECTIVE_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit]


_EVIDENCE_REF_KEYS = (
    "ref_id", "record_id", "source_id", "path", "artifact_path", "original_path",
    "resolved_path", "line", "line_start", "line_end", "line_sha256_16",
    "sha256", "status", "locator_kind", "candidate_label",
    "eligible_for_current_reuse",
)


def _bounded_evidence_ref(value: Any) -> str | dict[str, Any] | None:
    if not isinstance(value, Mapping):
        text = _bounded_text(value, 600)
        return text or None
    result: dict[str, Any] = {}
    for key in _EVIDENCE_REF_KEYS:
        item = value.get(key)
        if item is None:
            continue
        if isinstance(item, (int, float, bool)):
            result[key] = item
        else:
            bounded = _bounded_text(item, 800)
            if bounded:
                result[key] = bounded
    if len(_canonical_json(result)) > 2_400:
        result = {
            key: result[key]
            for key in ("ref_id", "source_id", "line", "line_sha256_16", "sha256", "status")
            if key in result
        }
    return result or None


def _estimated_tokens(value: str) -> int:
    """Conservative mixed-language estimate for the host's 1000-token entry cap."""

    ascii_chars = sum(1 for char in value if ord(char) < 128)
    non_ascii_chars = len(value) - ascii_chars
    return non_ascii_chars + ((ascii_chars + 2) // 3)


def _event_id(task_event: Mapping[str, Any]) -> str:
    value = task_event.get("event_id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("task_event.event_id must be a non-empty string")
    return value


def _event_type(task_event: Mapping[str, Any]) -> str:
    value = task_event.get("type")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("task_event.type must be a non-empty string")
    return value


def _intent_kind(task_event: Mapping[str, Any]) -> str | None:
    value = task_event.get("intent_kind") or task_event.get("intent_relation")
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized if normalized in INTENT_KINDS else None


def _last_user_delta(task_event: Mapping[str, Any]) -> dict[str, Any] | None:
    kind = _intent_kind(task_event)
    if kind is None:
        return None
    return {
        "kind": kind,
        "text": _bounded_text(task_event.get("objective") or task_event.get("prompt")),
        "turn_ref": _event_id(task_event),
    }


def _turn_relation(
    task_event: Mapping[str, Any],
    capsule: Mapping[str, Any] | None,
) -> dict[str, Any]:
    kind = _intent_kind(task_event) or "ambiguous"
    confidence = str(task_event.get("intent_confidence") or "low")
    if confidence not in RELATION_CONFIDENCES:
        confidence = "low"
    source = str(task_event.get("intent_source") or "fallback")
    if source not in RELATION_SOURCES:
        source = "fallback"
    raw_reasons = task_event.get("intent_reason_codes") or []
    reasons = (
        [
            _bounded_text(value, 160)
            for value in raw_reasons
            if _bounded_text(value, 160)
        ][:8]
        if isinstance(raw_reasons, Sequence) and not isinstance(raw_reasons, (str, bytes))
        else []
    )
    reviewed = task_event.get("reviewed_against")
    if isinstance(reviewed, Mapping):
        reviewed_against = {
            "capsule_id": _bounded_text(reviewed.get("capsule_id"), 160),
            "goal_revision": int(reviewed.get("goal_revision") or 0),
        }
    elif isinstance(capsule, Mapping):
        reviewed_against = {
            "capsule_id": _bounded_text(capsule.get("capsule_id"), 160),
            "goal_revision": int(capsule.get("goal_revision") or 1),
        }
    else:
        reviewed_against = None
    relation = {
        "kind": kind,
        "confidence": confidence,
        "source": source,
        "reason_codes": reasons,
        "reviewed_against": reviewed_against,
    }
    if task_event.get("semantic_review_passed") is True:
        relation["semantic_review_passed"] = True
    return relation


def _source_units(task_event: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = task_event.get("source_units")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    return [
        {
            key: _copy(unit.get(key))
            for key in ("id", "source_role", "text_sha256")
            if unit.get(key) is not None
        }
        for unit in values
        if isinstance(unit, Mapping)
    ][:8]


def _requires_relation_binding_event(task_event: Mapping[str, Any]) -> bool:
    return bool(
        task_event.get("event_origin") == "current_user_input"
        or any(
            unit.get("source_role") == "current_user_request"
            for unit in _source_units(task_event)
        )
    )


def _derived_relation_nonce(
    task_event: Mapping[str, Any], capsule: Mapping[str, Any]
) -> str:
    return _sha256_text(
        _canonical_json(
            {
                "task_key": task_event.get("task_key"),
                "event_id": _event_id(task_event),
                "capsule_id": capsule.get("capsule_id"),
                "goal_revision": int(capsule.get("goal_revision") or 1),
                "source_units": _source_units(task_event),
            }
        )
    )[:24]


def _validated_turn_relation_receipt(
    task_event: Mapping[str, Any], capsule: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Validate one current-turn relation receipt without granting authority."""

    receipt = task_event.get("turn_relation_receipt")
    if not isinstance(receipt, Mapping) or not isinstance(capsule, Mapping):
        return None
    relation_nonce = _bounded_text(task_event.get("relation_nonce"), 256)
    if (
        receipt.get("schema") != TURN_RELATION_RECEIPT_SCHEMA
        or not relation_nonce
        or _bounded_text(receipt.get("relation_nonce"), 256) != relation_nonce
        or _bounded_text(receipt.get("candidate_task_id"), 160)
        != _bounded_text(capsule.get("capsule_id"), 160)
        or _bounded_text(receipt.get("target_task_id"), 160)
        != _bounded_text(capsule.get("capsule_id"), 160)
        or int(receipt.get("expected_goal_revision") or 0)
        != int(capsule.get("goal_revision") or 1)
    ):
        return None
    relation_kind = _bounded_text(receipt.get("relation_kind"), 64)
    if relation_kind not in INTENT_KINDS:
        return None
    raw_source_ids = receipt.get("source_unit_ids")
    source_units = task_event.get("source_units")
    if (
        not isinstance(raw_source_ids, Sequence)
        or isinstance(raw_source_ids, (str, bytes))
        or not raw_source_ids
        or not isinstance(source_units, Sequence)
        or isinstance(source_units, (str, bytes))
    ):
        return None
    source_by_id = {
        _bounded_text(unit.get("id"), 256): unit
        for unit in source_units
        if isinstance(unit, Mapping) and _bounded_text(unit.get("id"), 256)
    }
    source_ids = list(dict.fromkeys(_bounded_text(value, 256) for value in raw_source_ids))
    if not source_ids or any(source_id not in source_by_id for source_id in source_ids):
        return None
    objective_sha256 = _sha256_text(
        str(task_event.get("objective") or task_event.get("prompt") or "")
    )
    for source_id in source_ids:
        unit = source_by_id[source_id]
        digest = _bounded_text(unit.get("text_sha256"), 64)
        if (
            unit.get("source_role") != "current_user_request"
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or digest != objective_sha256
        ):
            return None
    raw_ambiguity = receipt.get("ambiguity_codes") or []
    ambiguity_codes = (
        [
            text
            for text in (_bounded_text(value, 160) for value in raw_ambiguity)
            if text
        ][:8]
        if isinstance(raw_ambiguity, Sequence)
        and not isinstance(raw_ambiguity, (str, bytes))
        else []
    )
    if ambiguity_codes and relation_kind != "ambiguous":
        return None
    return {
        "schema": TURN_RELATION_RECEIPT_SCHEMA,
        "relation_nonce": relation_nonce,
        "candidate_task_id": capsule.get("capsule_id"),
        "expected_goal_revision": int(capsule.get("goal_revision") or 1),
        "relation_kind": relation_kind,
        "target_task_id": capsule.get("capsule_id"),
        "source_unit_ids": source_ids,
        "ambiguity_codes": ambiguity_codes,
    }


def _relation_is_current(
    relation: Mapping[str, Any], capsule: Mapping[str, Any]
) -> bool:
    reviewed = relation.get("reviewed_against")
    return bool(
        isinstance(reviewed, Mapping)
        and reviewed.get("capsule_id") == capsule.get("capsule_id")
        and int(reviewed.get("goal_revision") or 0)
        == int(capsule.get("goal_revision") or 1)
    )


def _global_replacement_allowed(
    relation: Mapping[str, Any], capsule: Mapping[str, Any]
) -> bool:
    if relation.get("kind") != "new_task" or relation.get("confidence") != "high":
        return False
    source = relation.get("source")
    if source == "semantic_review" and relation.get("semantic_review_passed") is not True:
        return False
    return source == "typed_receipt" and _relation_is_current(
        relation, capsule
    )


def _normalize_reuse_candidates(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    normalized: list[dict[str, Any]] = []
    for value in values:
        ref = _bounded_evidence_ref(value)
        if not isinstance(ref, Mapping):
            continue
        candidate = dict(ref)
        if not any(candidate.get(key) for key in ("record_id", "ref_id", "source_id", "path", "artifact_path")):
            continue
        if candidate not in normalized:
            normalized.append(candidate)
    return normalized[:MAX_CONTEXT_LIST_ITEMS]


def _global_goal_anchor(capsule: Mapping[str, Any]) -> dict[str, Any]:
    objective = _bounded_text(capsule.get("objective"))
    return {
        "schema": "cbh.global_goal_anchor.v1",
        "source_capsule_id": capsule.get("capsule_id"),
        "objective": objective,
        "objective_sha256": _sha256_text(objective),
        "purpose": _bounded_text(capsule.get("purpose"), 1_000),
        "required_outputs": _copy(capsule.get("required_outputs") or []),
        "acceptance_criteria": _copy(capsule.get("acceptance_criteria") or []),
        "constraints": _copy(capsule.get("constraints") or []),
        "non_goals": _copy(capsule.get("non_goals") or []),
        "stop_condition": _bounded_text(capsule.get("stop_condition"), 800),
        "source_refs": _copy(capsule.get("evidence_refs") or []),
        "goal_revision": int(capsule.get("goal_revision") or 1),
    }


def _local_delta(
    task_event: Mapping[str, Any], relation: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "kind": relation.get("kind"),
        "text": _bounded_text(task_event.get("objective") or task_event.get("prompt")),
        "turn_ref": _event_id(task_event),
        "confidence": relation.get("confidence"),
        "source": relation.get("source"),
        "reason_codes": _copy(relation.get("reason_codes") or []),
    }


def _binding_ids(route_receipt: Mapping[str, Any]) -> set[str]:
    bindings = route_receipt.get("action_bindings")
    if not isinstance(bindings, Sequence) or isinstance(bindings, (str, bytes)):
        return set()
    return {
        str(item.get("action"))
        for item in bindings
        if isinstance(item, Mapping) and item.get("action")
    }


def _event_binding_ids(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [
        text
        for text in (_bounded_text(item, 160) for item in value)
        if text
    ][:MAX_CONTEXT_LIST_ITEMS]


def _decision_reasons(
    route_receipt: Mapping[str, Any], task_event: Mapping[str, Any]
) -> list[str]:
    reasons: list[str] = []
    objective = _bounded_text(task_event.get("objective") or task_event.get("prompt"))
    edit_profile = str(route_receipt.get("edit_operation_profile") or "none")
    memory_mode = str(route_receipt.get("memory_mode") or "none")
    tool_surface = str(route_receipt.get("tool_surface_need") or "none")
    event_type = _event_type(task_event)

    explicit_write = bool(task_event.get("write_intent"))
    route_write = edit_profile in _WRITE_PROFILES or memory_mode in {"write", "update"}
    prompt_write = bool(_WRITE_PROMPT.search(objective)) and not bool(
        _READ_ONLY_PROMPT.search(objective)
    )
    if explicit_write or route_write or prompt_write or event_type == "write_result_received":
        reasons.append("write_intent")

    tool_event = event_type in {
        "candidate_selected",
        "preflight_completed",
        "tool_dispatched",
        "tool_result_received",
        "write_result_received",
        "verifier_pending",
        "verifier_completed",
        "unchanged_dispatch_repeated",
        "transport_threshold_crossed",
    }
    route_tool = tool_surface not in _NON_TOOL_HOST_SURFACES or bool(
        _binding_ids(route_receipt) & _TOOL_ACTIONS
    )
    if bool(task_event.get("tool_required")) or tool_event or route_tool or _TOOL_PROMPT.search(objective):
        reasons.append("tool_required")

    if bool(task_event.get("multi_stage")):
        reasons.append("multi_stage_task")
    if bool(task_event.get("resume")) or event_type in {"task_interrupted", "continuation_required"}:
        reasons.append("task_resume")
    if bool(task_event.get("long_running")):
        reasons.append("long_running_task")
    if bool(task_event.get("open_loops")):
        reasons.append("open_loop")
    if event_type == "unchanged_dispatch_repeated" or bool(task_event.get("prior_failure")):
        reasons.append("prior_failure")
    if task_event.get("continuity_requested") is True:
        reasons.append("explicit_request")
    plan_steps = task_event.get("plan_steps")
    if (
        event_type == "progress_snapshot"
        and isinstance(plan_steps, Sequence)
        and not isinstance(plan_steps, (str, bytes))
        and any(isinstance(item, Mapping) for item in plan_steps)
    ):
        reasons.append("plan_observed")
    return list(dict.fromkeys(reasons))


def decide_task_continuity(
    route_receipt: Mapping[str, Any],
    task_event: Mapping[str, Any],
    capsule: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the task-local activation decision without allocating state."""

    if not isinstance(route_receipt, Mapping):
        raise ValueError("route_receipt must be an object")
    if not isinstance(task_event, Mapping):
        raise ValueError("task_event must be an object")
    event_id = _event_id(task_event)
    reasons = _decision_reasons(route_receipt, task_event)
    lifecycle = str(capsule.get("lifecycle")) if isinstance(capsule, Mapping) else None
    if (
        lifecycle in {"ARMED", "ACTIVE", "VERIFYING"}
        and _requires_relation_binding_event(task_event)
        and task_event.get("relation_commit_validated") is not True
    ):
        decision = "safe_readonly"
        reasons = ["turn_relation_receipt_required"]
        host_delivery = "ready"
    elif lifecycle in {"ARMED", "ACTIVE", "VERIFYING"}:
        decision = "continue"
        if not reasons:
            reasons = ["existing_active_capsule"]
        host_delivery = "ready"
    elif not _bounded_text(task_event.get("objective") or task_event.get("prompt")):
        decision = "dormant"
        reasons = []
        host_delivery = "not_needed"
    elif _PERSISTENT_REASONS.intersection(reasons):
        decision = "arm"
        host_delivery = "ready"
    elif reasons:
        decision = "observe_once"
        host_delivery = "ready"
    elif (
        _intent_kind(task_event) in {"continue_ack", "ambiguous"}
        and _event_type(task_event) == "task_observed"
    ):
        decision = "dormant"
        reasons = []
        host_delivery = "not_needed"
    else:
        decision = "dormant"
        host_delivery = "not_needed"
    return {
        "schema": DECISION_SCHEMA,
        "decision": decision,
        "reasons": reasons,
        "source_event_ids": [event_id],
        "host_delivery": host_delivery,
    }


def _normalize_criteria(
    criteria: Any,
    *,
    write_intent: bool,
    tool_required: bool,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if isinstance(criteria, Sequence) and not isinstance(criteria, (str, bytes)):
        for index, item in enumerate(criteria):
            if isinstance(item, Mapping):
                criterion_id = _bounded_text(item.get("id") or f"criterion_{index + 1}", 160)
                text = _bounded_text(item.get("text") or item.get("description") or criterion_id, 800)
            else:
                criterion_id = f"criterion_{index + 1}"
                text = _bounded_text(item, 800)
            if criterion_id and text:
                normalized.append({"id": criterion_id, "text": text, "status": "unknown"})
    return normalized


def _normalize_plan_steps(plan_steps: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if not isinstance(plan_steps, Sequence) or isinstance(plan_steps, (str, bytes)):
        return normalized
    allowed = {"completed", "in_progress", "pending", "inferred", "unknown"}
    aliases = {"inProgress": "in_progress", "in-progress": "in_progress"}
    for index, item in enumerate(plan_steps):
        if isinstance(item, Mapping):
            step_id = _bounded_text(item.get("id") or f"plan-{index}", 160)
            text = _bounded_text(item.get("text") or item.get("step") or step_id, 800)
            status = aliases.get(str(item.get("status") or "unknown"), str(item.get("status") or "unknown"))
        else:
            step_id = f"plan-{index}"
            text = _bounded_text(item, 800)
            status = "unknown"
        if step_id and text:
            normalized.append(
                {
                    "id": step_id,
                    "text": text,
                    "status": status if status in allowed else "unknown",
                }
            )
    return normalized


def _remaining_plan_steps(capsule: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        _copy(item)
        for item in capsule.get("plan_steps") or []
        if isinstance(item, Mapping)
        and item.get("status") not in {"completed", "inferred"}
    ]


def _first_pending_plan_step(capsule: Mapping[str, Any]) -> Mapping[str, Any] | None:
    return next(iter(_remaining_plan_steps(capsule)), None)


def _normalize_required_outputs(outputs: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if not isinstance(outputs, Sequence) or isinstance(outputs, (str, bytes)):
        return normalized
    for index, item in enumerate(outputs):
        if isinstance(item, Mapping):
            output_id = _bounded_text(item.get("id") or f"output-{index + 1}", 160)
            text = _bounded_text(item.get("text") or item.get("description") or output_id, 800)
            status = str(item.get("status") or "unknown")
        else:
            output_id = f"output-{index + 1}"
            text = _bounded_text(item, 800)
            status = "unknown"
        if output_id and text:
            normalized.append(
                {
                    "id": output_id,
                    "text": text,
                    "status": status if status in {"verified", "inferred", "unknown"} else "unknown",
                }
            )
    return normalized


def _memory_record_view(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, Mapping) or not record.get("record_id"):
        return None
    return {
        key: _copy(record[key])
        for key in (
            "record_id",
            "family",
            "record_status",
            "source_id",
            "sha256",
            "event_date",
            "candidate_label",
            "eligible_for_current_reuse",
        )
        if record.get(key) is not None
    }


def _memory_working_set(
    task_event: Mapping[str, Any], capsule: Mapping[str, Any]
) -> dict[str, Any]:
    receipt = task_event.get("memory_consumption_receipt")
    if not isinstance(receipt, Mapping):
        raise ValueError("memory_consumption_receipt must be an object")
    query_type = str(task_event.get("memory_query_type") or "history_reason")
    if query_type not in {"current_state", "history_reason", "contradiction_check"}:
        raise ValueError("invalid_memory_query_type")
    query_basis = str(task_event.get("memory_query_basis") or "global_goal")
    if query_basis not in {"global_goal", "current_turn", "both"}:
        raise ValueError("invalid_memory_query_basis")
    selected = [
        view
        for view in (_memory_record_view(item) for item in (receipt.get("selected_records") or []))
        if view is not None
    ][:8]
    retrieval = receipt.get("retrieval") if isinstance(receipt.get("retrieval"), Mapping) else {}
    candidate_source = (
        retrieval.get("disambiguation_candidates")
        or receipt.get("semantic_review_candidates")
        or []
    )
    candidate_views = [
        {
            key: _copy(item[key])
            for key in ("record_id", "event_date", "summary", "candidate_label", "status", "matched_facets")
            if isinstance(item, Mapping) and item.get(key) is not None
        }
        for item in candidate_source
        if isinstance(item, Mapping) and item.get("record_id")
    ][:5]
    evidence_handles: list[dict[str, Any]] = []
    for record in receipt.get("selected_records") or []:
        if not isinstance(record, Mapping):
            continue
        for handle in record.get("evidence_handles") or []:
            if isinstance(handle, Mapping) and handle.get("source_id"):
                value = {
                    key: _copy(handle[key])
                    for key in ("source_id", "original_path", "line", "line_sha256_16", "sha256")
                    if handle.get(key) is not None
                }
                if value not in evidence_handles:
                    evidence_handles.append(value)
    conversation_handles: list[dict[str, Any]] = []
    conversation_navigation = receipt.get("conversation_navigation")
    if isinstance(conversation_navigation, Mapping):
        for bundle in conversation_navigation.get("bundles") or []:
            if not isinstance(bundle, Mapping) or not bundle.get("memory_id"):
                continue
            ledger = bundle.get("ledger") if isinstance(bundle.get("ledger"), Mapping) else {}
            selected_link_ids = [
                str(link["link_id"])
                for link in (bundle.get("selected_links") or [])
                if isinstance(link, Mapping) and link.get("link_id")
            ][:8]
            conversation_handles.append(
                {
                    "memory_id": str(bundle["memory_id"]),
                    "root_path": str(bundle.get("root_path") or ""),
                    "registry_path": str(bundle.get("registry_path") or ""),
                    "isolation": str(bundle.get("isolation") or "route_declared"),
                    "selected_link_ids": selected_link_ids,
                    "ledger_index_path": str(ledger.get("index_path") or ""),
                    "ledger_capsules_path": str(ledger.get("capsules_path") or ""),
                    "ledger_evidence_refs_path": str(ledger.get("evidence_refs_path") or ""),
                }
            )
    coverage_status = str(retrieval.get("coverage_status") or "not_requested")
    return {
        "query_type": query_type,
        "query_basis": query_basis,
        "bound_goal_revision": int(capsule.get("goal_revision") or 1),
        "coverage_status": coverage_status,
        "selected_record_ids": [str(item["record_id"]) for item in selected],
        "selected_records": selected,
        "candidate_views": candidate_views,
        "source_ids": list(
            dict.fromkeys(
                str(item["source_id"])
                for item in selected
                if item.get("source_id")
            )
        ),
        "evidence_handles": evidence_handles[:8],
        "conversation_handles": conversation_handles[:4],
        "opened_evidence_refs": [],
        "unresolved_ambiguity": bool(candidate_views) or coverage_status == "semantic_review_required",
        "receipt_sha256": _sha256_text(_canonical_json(_sanitize_for_transport(receipt))),
    }


def _progress_lists(capsule: dict[str, Any]) -> None:
    criteria = capsule.get("acceptance_criteria") or []
    capsule["verified_completed"] = [
        _copy(item) for item in criteria if item.get("status") == "verified"
    ]
    capsule["inferred_progress"] = [
        _copy(item) for item in criteria if item.get("status") == "inferred"
    ]
    capsule["unknown_progress"] = [
        _copy(item) for item in criteria if item.get("status") == "unknown"
    ]
    capsule["remaining_work"] = [
        _copy(item) for item in criteria if item.get("status") != "verified"
    ]


def _first_remaining(capsule: Mapping[str, Any]) -> Mapping[str, Any] | None:
    values = capsule.get("remaining_work")
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        return next((item for item in values if isinstance(item, Mapping)), None)
    return None


def _legacy_side_frame(capsule: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_capsule_id": capsule.get("capsule_id"),
        "objective": _bounded_text(capsule.get("objective")),
        "purpose": _bounded_text(capsule.get("purpose"), 1_000),
        "progress_revision": int(capsule.get("progress_revision") or 0),
        "resume_entry": _copy(capsule.get("resume_entry")) or {
            "source": "legacy_projection",
            "next_action": capsule.get("next_action"),
        },
    }


def ensure_v3_capsule(
    capsule: Mapping[str, Any],
    *,
    capsule_history: Sequence[Mapping[str, Any]] | None = None,
    source_head_sha256: str | None = None,
    source_chain_digest: str | None = None,
) -> dict[str, Any]:
    """Return an additive stable-frame projection without rewriting legacy records."""

    if not isinstance(capsule, Mapping) or capsule.get("schema") != CAPSULE_SCHEMA:
        raise ValueError("invalid task capsule")
    current = _copy(capsule)
    if int(current.get("state_version") or 0) >= 3 and isinstance(
        current.get("global_goal_anchor"), Mapping
    ):
        current.setdefault("active_local_delta", None)
        current.setdefault("turn_relation", None)
        current.setdefault("suspended_task_stack", [])
        current.setdefault("reuse_candidates", [])
        current["workspace_anchor"] = _normalize_workspace_anchor(
            current.get("workspace_anchor")
        )
        current.setdefault("workspace_review_required", False)
        current.setdefault("workspace_review_reason", None)
        current["context_epoch"] = _derive_context_epoch(current)
        return current

    ordered: list[Mapping[str, Any]] = []
    if isinstance(capsule_history, Sequence) and not isinstance(
        capsule_history, (str, bytes)
    ):
        ordered = [item for item in capsule_history if isinstance(item, Mapping)]
    root = ordered[0] if ordered else capsule
    latest = ordered[-1] if ordered else capsule
    anchor = _global_goal_anchor(root)
    current["state_version"] = 3
    current["objective"] = anchor["objective"]
    current["purpose"] = anchor["purpose"]
    current["required_outputs"] = _copy(anchor["required_outputs"])
    current["acceptance_criteria"] = _copy(anchor["acceptance_criteria"])
    current["constraints"] = _copy(anchor["constraints"])
    current["non_goals"] = _copy(anchor["non_goals"])
    current["stop_condition"] = anchor["stop_condition"]
    current["goal_revision"] = int(anchor["goal_revision"] or 1)
    current["global_goal_anchor"] = anchor
    distinct_history = [
        item
        for index, item in enumerate(ordered)
        if item.get("capsule_id")
        and item.get("capsule_id")
        not in {prior.get("capsule_id") for prior in ordered[:index]}
    ]
    latest_is_local = bool(
        distinct_history
        and latest.get("capsule_id") != root.get("capsule_id")
    )
    if latest_is_local:
        legacy_relation = {
            "kind": "ambiguous",
            "confidence": "low",
            "source": "legacy_projection",
            "reason_codes": ["legacy_supersedes_chain_requires_semantic_review"],
            "reviewed_against": {
                "capsule_id": current.get("capsule_id"),
                "goal_revision": current["goal_revision"],
            },
        }
        current["active_local_delta"] = {
            "kind": "ambiguous",
            "text": _bounded_text(latest.get("objective")),
            "turn_ref": (
                latest.get("last_event", {}).get("event_id")
                if isinstance(latest.get("last_event"), Mapping)
                else None
            ),
            "confidence": "low",
            "source": "legacy_projection",
            "reason_codes": legacy_relation["reason_codes"],
        }
        current["turn_relation"] = legacy_relation
        current["semantic_review_required"] = True
    else:
        initial_relation = {
            "kind": (
                current.get("last_user_delta", {}).get("kind")
                if isinstance(current.get("last_user_delta"), Mapping)
                else "ambiguous"
            ),
            "confidence": "low",
            "source": "legacy_projection" if source_head_sha256 else "fallback",
            "reason_codes": (
                ["legacy_capsule_projected"] if source_head_sha256 else ["initial_task_frame"]
            ),
            "reviewed_against": {
                "capsule_id": current.get("capsule_id"),
                "goal_revision": current["goal_revision"],
            },
        }
        current["turn_relation"] = initial_relation
        current["active_local_delta"] = _copy(current.get("last_user_delta"))
    current["suspended_task_stack"] = [
        _legacy_side_frame(item) for item in distinct_history[1:-1]
    ][-MAX_CONTEXT_LIST_ITEMS:]
    legacy_goal_deltas = [
        {
            "kind": "correction",
            "text": _bounded_text(item.get("objective")),
            "source_ref": f"legacy_capsule:{item.get('capsule_id')}",
        }
        for item in distinct_history[1:]
        if _bounded_text(item.get("objective"))
        and _LEGACY_GOAL_REVISION_PROMPT.search(_bounded_text(item.get("objective")))
    ][-MAX_CONTEXT_LIST_ITEMS:]
    if legacy_goal_deltas:
        current["goal_deltas"] = legacy_goal_deltas
        current["goal_revision"] = 1 + len(legacy_goal_deltas)
        current["global_goal_anchor"]["goal_revision"] = current["goal_revision"]
        current["global_goal_anchor"]["goal_deltas"] = _copy(legacy_goal_deltas)
        if isinstance(current.get("turn_relation"), Mapping):
            current["turn_relation"]["reviewed_against"] = {
                "capsule_id": current.get("capsule_id"),
                "goal_revision": current["goal_revision"],
            }
    reuse_source: Any = current.get("reuse_candidates") or []
    if not reuse_source and isinstance(current.get("memory_working_set"), Mapping):
        reuse_source = current["memory_working_set"].get("evidence_handles") or []
    current["reuse_candidates"] = _normalize_reuse_candidates(reuse_source)
    if source_head_sha256:
        current["legacy_projection"] = {
            "source_head_sha256": source_head_sha256,
            "source_chain_digest": source_chain_digest,
            "projected_capsule_ids": [
                str(item.get("capsule_id"))
                for item in distinct_history
                if item.get("capsule_id")
            ],
        }
    _progress_lists(current)
    first = _first_remaining(current)
    if first is not None:
        current["next_action"] = first.get("text")
        current["next_action_reason"] = "earliest_incomplete_acceptance_item"
    current["workspace_anchor"] = _normalize_workspace_anchor(
        current.get("workspace_anchor")
    )
    current.setdefault("workspace_review_required", False)
    current.setdefault("workspace_review_reason", None)
    current["context_epoch"] = _derive_context_epoch(current)
    return current


def new_task_capsule(
    route_receipt: Mapping[str, Any], task_event: Mapping[str, Any]
) -> dict[str, Any]:
    """Create one deterministic, process-local ARMED capsule."""

    decision = decide_task_continuity(route_receipt, task_event)
    if decision["decision"] == "dormant":
        raise ValueError("cannot create a capsule for a dormant task")
    objective = _bounded_text(task_event.get("objective") or task_event.get("prompt"))
    if not objective:
        raise ValueError("task_objective_required")
    event_id = _event_id(task_event)
    task_key = _canonical_json(
        {
            "objective": objective,
            "task_key": task_event.get("task_key"),
            "lane": route_receipt.get("project_lane"),
        }
    )
    task_key_sha256 = _sha256_text(task_key)
    reasons = set(decision["reasons"])
    plan_steps = _normalize_plan_steps(task_event.get("plan_steps"))
    criteria = _normalize_criteria(
        task_event.get("acceptance_criteria"),
        write_intent="write_intent" in reasons,
        tool_required="tool_required" in reasons,
    )
    capsule_id = _sha256_text(f"{task_key_sha256}:{event_id}")[:24]
    capsule: dict[str, Any] = {
        "schema": CAPSULE_SCHEMA,
        "capsule_id": capsule_id,
        "working_set_id": capsule_id,
        "task_key_sha256": task_key_sha256,
        "host_task_key_sha256": (
            _sha256_text(str(task_event.get("task_key")))
            if task_event.get("task_key") is not None
            else None
        ),
        "state_version": 3,
        "lifecycle": "ARMED",
        "activation_reasons": decision["reasons"],
        "objective": objective,
        "purpose": _bounded_text(task_event.get("purpose"), 1_000),
        "required_outputs": _normalize_required_outputs(task_event.get("required_outputs")),
        "stop_condition": _bounded_text(task_event.get("stop_condition"), 800),
        "non_goals": [
            _bounded_text(value, 800)
            for value in (task_event.get("non_goals") or [])
            if _bounded_text(value, 800)
        ][:MAX_CONTEXT_LIST_ITEMS],
        "goal_revision": 1,
        "goal_deltas": [],
        "last_user_delta": _last_user_delta(task_event),
        "semantic_review_required": bool(task_event.get("semantic_review_required")),
        "supersedes_capsule_id": task_event.get("supersedes_capsule_id"),
        "retirement": None,
        "memory_working_set": {
            "query_type": None,
            "query_basis": "global_goal",
            "bound_goal_revision": 1,
            "coverage_status": "not_requested",
            "selected_record_ids": [],
            "selected_records": [],
            "candidate_views": [],
            "source_ids": [],
            "evidence_handles": [],
            "conversation_handles": [],
            "opened_evidence_refs": [],
            "unresolved_ambiguity": False,
            "receipt_sha256": None,
        },
        "acceptance_criteria": criteria,
        "plan_steps": plan_steps,
        "constraints": [
            _bounded_text(value, 800)
            for value in (task_event.get("constraints") or [])
            if _bounded_text(value, 800)
        ][:MAX_CONTEXT_LIST_ITEMS],
        "current_stage": _bounded_text(task_event.get("current_stage") or "planning", 160),
        "verified_completed": [],
        "inferred_progress": [],
        "unknown_progress": [],
        "current_step": None,
        "current_action": None,
        "remaining_work": [],
        "next_action": None,
        "next_action_reason": "earliest_incomplete_acceptance_item",
        "blocking_condition": None,
        "last_postcondition": None,
        "unresolved_failures": [],
        "progress_revision": 1,
        "execution_log_cursor": task_event.get("execution_log_cursor"),
        "evidence_refs": [
            ref
            for ref in (
                _bounded_evidence_ref(value)
                for value in (task_event.get("evidence_refs") or [])
            )
            if ref is not None
        ][:MAX_CONTEXT_LIST_ITEMS],
        "active_frames": [],
        "global_goal_anchor": None,
        "active_local_delta": None,
        "turn_relation": None,
        "suspended_task_stack": [],
        "reuse_candidates": _normalize_reuse_candidates(
            task_event.get("reuse_candidates") or []
        ),
        "workspace_anchor": _normalize_workspace_anchor(
            task_event.get("workspace_anchor")
        ),
        "workspace_review_required": False,
        "workspace_review_reason": None,
        "context_epoch": None,
        "transport": {},
        "resume_entry": None,
        "applied_event_ids": [event_id],
        "last_event": {
            "event_id": event_id,
            "type": _event_type(task_event),
            "observed_at": task_event.get("observed_at"),
        },
        "reminder_state": {"emitted": []},
        "authority": {"granted": False, "consumed": False, "source": "none"},
        "persistence": "process_local_only",
        "persistence_scope": (
            "turn_local" if decision["decision"] == "observe_once" else "cross_turn"
        ),
    }
    _progress_lists(capsule)
    first_plan = _first_pending_plan_step(capsule)
    first = _first_remaining(capsule)
    capsule["next_action"] = (
        first_plan.get("text") if first_plan else first.get("text") if first else None
    )
    capsule["next_action_reason"] = (
        "earliest_incomplete_plan_step"
        if first_plan
        else "earliest_incomplete_acceptance_item"
        if first
        else None
    )
    relation = _turn_relation(task_event, capsule)
    capsule["global_goal_anchor"] = _global_goal_anchor(capsule)
    capsule["active_local_delta"] = _local_delta(task_event, relation)
    capsule["turn_relation"] = relation
    capsule["context_epoch"] = _derive_context_epoch(capsule)
    return capsule


def initialize_task_capsule(
    route_receipt: Mapping[str, Any], task_event: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Create a capsule and preserve a substantive first event exactly once."""

    if _event_type(task_event) == "task_observed":
        return new_task_capsule(route_receipt, task_event), None
    bootstrap = _copy(task_event)
    bootstrap["event_id"] = f"{_event_id(task_event)}:activation"
    bootstrap["type"] = "task_observed"
    if _event_type(task_event) == "progress_snapshot" and task_event.get("plan_steps"):
        bootstrap["continuity_requested"] = True
        bootstrap["multi_stage"] = True
    capsule = new_task_capsule(route_receipt, bootstrap)
    transition = apply_task_event(capsule, task_event)
    return transition["capsule"], transition


def _set_criterion_status(
    capsule: dict[str, Any], criterion_id: str | None, status: str
) -> bool:
    if status not in {"verified", "inferred", "unknown"}:
        raise ValueError("invalid progress status")
    criteria = capsule.get("acceptance_criteria") or []
    selected: dict[str, Any] | None = None
    if not criterion_id:
        return False
    selected = next(
        (item for item in criteria if str(item.get("id")) == criterion_id), None
    )
    if selected is None or selected.get("status") == status:
        return False
    selected["status"] = status
    return True


def _merge_progress_snapshot(
    capsule: dict[str, Any], task_event: Mapping[str, Any]
) -> bool:
    incoming = task_event.get("acceptance_criteria")
    if not isinstance(incoming, Sequence) or isinstance(incoming, (str, bytes)):
        return False
    normalized = _normalize_criteria(incoming, write_intent=False, tool_required=False)
    if not normalized:
        return False
    for index, item in enumerate(incoming):
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or "unknown")
        normalized[index]["status"] = status if status in {"inferred", "unknown"} else "unknown"
    changed = normalized != capsule.get("acceptance_criteria")
    if changed:
        capsule["acceptance_criteria"] = normalized
    return changed


def _merge_plan_snapshot(
    capsule: dict[str, Any], task_event: Mapping[str, Any]
) -> bool:
    incoming = task_event.get("plan_steps")
    normalized = _normalize_plan_steps(incoming)
    if incoming is None:
        return False
    changed = normalized != capsule.get("plan_steps")
    if changed:
        capsule["plan_steps"] = normalized
    return changed


def _append_unique_failure(capsule: dict[str, Any], task_event: Mapping[str, Any]) -> None:
    signature = _bounded_text(task_event.get("subject_signature"), 128)
    identity = {
        "signature": signature or None,
        "error_class": _bounded_text(task_event.get("error_class") or "unknown", 160),
        "event_id": _event_id(task_event),
    }
    failures = capsule.setdefault("unresolved_failures", [])
    if not any(
        item.get("signature") == identity["signature"]
        and item.get("error_class") == identity["error_class"]
        for item in failures
        if isinstance(item, Mapping)
    ):
        failures.append(identity)
        del failures[MAX_CONTEXT_LIST_ITEMS:]


def apply_task_event(
    capsule: Mapping[str, Any], task_event: Mapping[str, Any]
) -> dict[str, Any]:
    """Apply one event idempotently and return an auditable transition."""

    if not isinstance(capsule, Mapping) or capsule.get("schema") != CAPSULE_SCHEMA:
        raise ValueError("invalid task capsule")
    if str(capsule.get("lifecycle")) not in LIFECYCLES:
        raise ValueError("invalid task capsule lifecycle")
    if not isinstance(task_event, Mapping):
        raise ValueError("task_event must be an object")
    event_id = _event_id(task_event)
    event_type = _event_type(task_event)
    expected_task_key = capsule.get("host_task_key_sha256")
    event_task_key = task_event.get("task_key")
    if (
        expected_task_key is not None
        and event_task_key is not None
        and _sha256_text(str(event_task_key)) != expected_task_key
    ):
        raise ValueError("task_identity_mismatch")
    if capsule.get("lifecycle") == "RETIRED":
        unchanged = _copy(capsule)
        return {
            "schema": TRANSITION_SCHEMA,
            "capsule": unchanged,
            "changed": False,
            "previous_lifecycle": "RETIRED",
            "lifecycle": "RETIRED",
            "progress_delta": [],
            "event_outcome": "retired_ignored",
            "transition_reasons": ["retired_capsule_is_terminal"],
            "event_type": event_type,
        }
    prior_ids = list(capsule.get("applied_event_ids") or [])
    if event_id in prior_ids:
        unchanged = _copy(capsule)
        return {
            "schema": TRANSITION_SCHEMA,
            "capsule": unchanged,
            "changed": False,
            "previous_lifecycle": unchanged["lifecycle"],
            "lifecycle": unchanged["lifecycle"],
            "progress_delta": [],
            "event_outcome": "duplicate_ignored",
            "transition_reasons": ["event_id_already_applied"],
            "event_type": event_type,
        }

    event_evidence_refs = [
        ref
        for ref in (
            _bounded_evidence_ref(value)
            for value in (task_event.get("evidence_refs") or [])
        )
        if ref is not None
    ][:MAX_CONTEXT_LIST_ITEMS]
    updated = ensure_v3_capsule(capsule)
    previous_lifecycle = str(updated["lifecycle"])
    progress_delta: list[str] = []
    reasons: list[str] = []
    _reconcile_workspace_anchor(updated, task_event, reasons)
    updated["applied_event_ids"] = [*prior_ids, event_id]
    updated["last_event"] = {
        "event_id": event_id,
        "type": event_type,
        "observed_at": task_event.get("observed_at"),
        "evidence_refs": _copy(event_evidence_refs),
    }
    relation = _turn_relation(task_event, updated)
    user_delta = _last_user_delta(task_event)
    if user_delta is not None:
        updated["last_user_delta"] = user_delta
        active_local = _local_delta(task_event, relation)
        if (
            updated.get("suspended_task_stack")
            and isinstance(updated.get("active_local_delta"), Mapping)
            and updated["active_local_delta"].get("kind") == "side_task"
            and relation.get("kind") != "side_task"
        ):
            active_local = _copy(updated["active_local_delta"])
            active_local["latest_turn"] = {
                "kind": relation.get("kind"),
                "turn_ref": _event_id(task_event),
            }
        updated["active_local_delta"] = active_local
        updated["turn_relation"] = relation
        if user_delta["kind"] in {"ambiguous", "new_task"}:
            updated["semantic_review_required"] = True
        elif user_delta["kind"] in {"continue_ack", "refine", "correction"}:
            if relation.get("confidence") == "high" and _relation_is_current(
                relation, updated
            ):
                updated["semantic_review_required"] = False
            if user_delta["kind"] in {"refine", "correction"}:
                updated.setdefault("goal_deltas", []).append(
                    {
                        "kind": user_delta["kind"],
                        "text": user_delta["text"],
                        "source_ref": user_delta["turn_ref"],
                    }
                )
                updated["goal_deltas"] = updated["goal_deltas"][-MAX_CONTEXT_LIST_ITEMS:]
                updated["goal_revision"] = int(updated.get("goal_revision") or 1) + 1
                if isinstance(updated.get("global_goal_anchor"), dict):
                    updated["global_goal_anchor"]["goal_revision"] = updated["goal_revision"]
                    updated["global_goal_anchor"]["goal_deltas"] = _copy(
                        updated["goal_deltas"]
                    )
        elif user_delta["kind"] == "side_task":
            if relation.get("confidence") == "high" and _relation_is_current(
                relation, updated
            ):
                updated.setdefault("suspended_task_stack", []).append(
                    {
                        "source_capsule_id": updated.get("capsule_id"),
                        "resume_entry": {
                            "global_objective": updated.get("objective"),
                            "current_stage": updated.get("current_stage"),
                            "next_action": updated.get("next_action"),
                            "progress_revision": updated.get("progress_revision"),
                        },
                    }
                )
                updated["suspended_task_stack"] = updated["suspended_task_stack"][-MAX_CONTEXT_LIST_ITEMS:]
                updated["semantic_review_required"] = False
            else:
                updated["semantic_review_required"] = True
    if task_event.get("current_stage"):
        updated["current_stage"] = _bounded_text(task_event["current_stage"], 160)
    if "current_step" in task_event:
        updated["current_step"] = (
            _bounded_text(task_event["current_step"], 800)
            if task_event.get("current_step") is not None
            else None
        )
    if "next_action" in task_event:
        updated["next_action"] = (
            _bounded_text(task_event["next_action"], 800)
            if task_event.get("next_action") is not None
            else None
        )
    if event_type in {"candidate_selected", "preflight_completed", "tool_dispatched"}:
        action_text = _bounded_text(
            task_event.get("current_step")
            or task_event.get("next_action")
            or event_type,
            800,
        )
        explicit_binding = any(
            key in task_event
            for key in ("serves_output_ids", "serves_criterion_ids")
        )
        if explicit_binding:
            requested_output_ids = _event_binding_ids(
                task_event.get("serves_output_ids")
            )
            requested_criterion_ids = _event_binding_ids(
                task_event.get("serves_criterion_ids")
            )
            known_output_ids = {
                str(item.get("id"))
                for item in updated.get("required_outputs") or []
                if isinstance(item, Mapping) and item.get("id") is not None
            }
            known_criterion_ids = {
                str(item.get("id"))
                for item in updated.get("acceptance_criteria") or []
                if isinstance(item, Mapping) and item.get("id") is not None
            }
            output_ids = [
                value for value in requested_output_ids if value in known_output_ids
            ]
            criterion_ids = [
                value
                for value in requested_criterion_ids
                if value in known_criterion_ids
            ]
            unresolved_binding_ids = [
                value
                for value in [*requested_output_ids, *requested_criterion_ids]
                if value not in {*known_output_ids, *known_criterion_ids}
            ]
            binding_source = (
                "explicit_event"
                if (output_ids or criterion_ids) and not unresolved_binding_ids
                else "explicit_event_unresolved"
            )
        else:
            criterion_ids = []
            output_ids = []
            binding_source = "unbound"
            unresolved_binding_ids = []
        explicit_reason = _bounded_text(task_event.get("action_binding_reason"), 320)
        if explicit_reason:
            reason = explicit_reason
        elif criterion_ids:
            reason = f"serves_acceptance_criterion:{criterion_ids[0]}"
        elif output_ids:
            reason = f"serves_required_output:{output_ids[0]}"
        else:
            reason = "unbound_action"
        current_action = {
            "text": action_text,
            "serves_output_ids": output_ids,
            "serves_criterion_ids": criterion_ids,
            "reason": reason,
            "binding_source": binding_source,
            "state_progress_allowed": binding_source == "explicit_event",
        }
        if unresolved_binding_ids:
            current_action["unresolved_binding_ids"] = unresolved_binding_ids
        updated["current_action"] = current_action
        updated["next_action_reason"] = (
            _bounded_text(task_event.get("next_action_reason"), 320)
            if task_event.get("next_action") is not None
            else None
        )
    if task_event.get("blocking_condition") is not None:
        updated["blocking_condition"] = _bounded_text(task_event["blocking_condition"], 400)
    if task_event.get("postcondition") is not None:
        updated["last_postcondition"] = _bounded_text(task_event["postcondition"], 800)
    evidence_refs = event_evidence_refs
    for ref in evidence_refs:
        if ref not in updated["evidence_refs"]:
            updated["evidence_refs"].append(ref)
    updated["evidence_refs"] = updated["evidence_refs"][-MAX_CONTEXT_LIST_ITEMS:]

    if event_type in {"candidate_selected", "preflight_completed", "tool_dispatched"}:
        updated["lifecycle"] = "ACTIVE"
        reasons.append("substantive_action_observed")
    elif event_type in {"tool_result_received", "write_result_received"}:
        satisfied = task_event.get("postcondition_satisfied") is True
        status = "verified" if satisfied else "inferred"
        if _set_criterion_status(
            updated,
            _bounded_text(task_event.get("acceptance_id"), 160) or None,
            status,
        ):
            progress_delta.append(f"criterion_{status}")
        else:
            reasons.append("result_unbound_to_acceptance")
        updated["lifecycle"] = "VERIFYING"
        reasons.append("result_requires_acceptance_reconciliation")
    elif event_type == "verifier_pending":
        updated["lifecycle"] = "VERIFYING"
        reasons.append("verifier_pending")
    elif event_type == "verifier_completed":
        satisfied = task_event.get("postcondition_satisfied") is True
        status = "verified" if satisfied else "inferred"
        if _set_criterion_status(
            updated,
            _bounded_text(task_event.get("acceptance_id"), 160) or None,
            status,
        ):
            progress_delta.append(f"criterion_{status}")
        else:
            reasons.append("verifier_unbound_to_acceptance")
        if satisfied:
            updated["blocking_condition"] = None
        updated["lifecycle"] = "VERIFYING"
        reasons.append("verifier_result_observed")
    elif event_type == "unchanged_dispatch_repeated":
        updated["lifecycle"] = "ACTIVE"
        _append_unique_failure(updated, task_event)
        reasons.append("unchanged_failed_dispatch_requires_changed_path")
    elif event_type == "task_interrupted":
        updated["lifecycle"] = "ACTIVE"
        updated["resume_entry"] = {
            "event_id": event_id,
            "current_stage": updated.get("current_stage"),
            "next_action": updated.get("next_action"),
            "remaining_ids": [item.get("id") for item in updated.get("remaining_work", [])],
        }
        reasons.append("task_interrupted_with_open_state")
    elif event_type == "side_task_completed":
        stack = list(updated.get("suspended_task_stack") or [])
        if task_event.get("postcondition_satisfied") is not True:
            updated["lifecycle"] = "VERIFYING"
            reasons.append("side_task_completion_requires_verification")
        elif stack:
            resume = stack.pop()
            updated["suspended_task_stack"] = stack
            updated["active_local_delta"] = {
                "kind": "resume",
                "turn_ref": event_id,
                "resume_entry": _copy(resume.get("resume_entry"))
                if isinstance(resume, Mapping)
                else None,
            }
            updated["turn_relation"] = {
                "kind": "continue_ack",
                "confidence": "high",
                "source": "semantic_review",
                "reason_codes": ["verified_side_task_completed"],
                "reviewed_against": {
                    "capsule_id": updated.get("capsule_id"),
                    "goal_revision": updated.get("goal_revision"),
                },
            }
            updated["semantic_review_required"] = False
            updated["lifecycle"] = "ACTIVE"
            reasons.append("suspended_global_task_resumed")
        else:
            updated["lifecycle"] = "ACTIVE"
            reasons.append("side_task_stack_already_empty")
    elif event_type == "task_abandoned":
        updated["lifecycle"] = "RETIRED"
        updated["retirement"] = {
            "outcome": "abandoned",
            "reason": "task_explicitly_abandoned",
            "evidence_refs": evidence_refs,
        }
        reasons.append("task_explicitly_abandoned")
    elif event_type == "task_complete_requested":
        updated["lifecycle"] = "VERIFYING"
        reasons.append("completion_requires_all_acceptance_items")
    elif event_type == "progress_snapshot":
        if _merge_plan_snapshot(updated, task_event):
            progress_delta.append("plan_progress_refreshed")
        updated["lifecycle"] = "ACTIVE"
        reasons.append("observable_plan_progress_refreshed")
    elif event_type == "memory_context_selected":
        updated["memory_working_set"] = _memory_working_set(task_event, updated)
        updated["reuse_candidates"] = _normalize_reuse_candidates(
            [
                *(updated["memory_working_set"].get("selected_records") or []),
                *(updated["memory_working_set"].get("evidence_handles") or []),
            ]
        )
        updated["lifecycle"] = "ACTIVE"
        reasons.append("memory_context_bound_to_task_working_set")
    elif event_type == "memory_evidence_opened":
        evidence_ref = task_event.get("evidence_ref")
        if not isinstance(evidence_ref, Mapping):
            raise ValueError("memory_evidence_ref_must_be_an_object")
        memory_state = updated.get("memory_working_set")
        if not isinstance(memory_state, dict):
            raise ValueError("memory_working_set_required")
        source_id = str(evidence_ref.get("source_id") or "")
        selected_handles = [
            item
            for item in (memory_state.get("evidence_handles") or [])
            if isinstance(item, Mapping) and str(item.get("source_id") or "") == source_id
        ]
        if not selected_handles:
            raise ValueError("evidence_source_not_selected")
        if str(evidence_ref.get("status") or "") not in {"original_verified", "relocated_verified"}:
            raise ValueError("evidence_open_not_verified")
        selected_handle = selected_handles[0]
        for key in ("line", "line_sha256_16"):
            if selected_handle.get(key) is not None and evidence_ref.get(key) != selected_handle.get(key):
                raise ValueError("evidence_anchor_mismatch")
        opened = {
            key: _copy(evidence_ref[key])
            for key in ("source_id", "resolved_path", "line", "line_sha256_16", "sha256", "status")
            if evidence_ref.get(key) is not None
        }
        opened_refs = list(memory_state.get("opened_evidence_refs") or [])
        if opened not in opened_refs:
            opened_refs.append(opened)
        memory_state["opened_evidence_refs"] = opened_refs[:8]
        updated["lifecycle"] = "ACTIVE"
        reasons.append("verified_selected_memory_evidence_opened")
    elif event_type in {"stage_exit", "continuation_required", "transport_threshold_crossed"}:
        updated["lifecycle"] = "ACTIVE"
        reasons.append(event_type)
    elif event_type == "task_observed":
        reasons.append("task_observation_refreshed")
    else:
        reasons.append("event_recorded_without_semantic_promotion")

    _progress_lists(updated)
    remaining_plan_steps = _remaining_plan_steps(updated)
    all_verified = bool(updated["acceptance_criteria"]) and not updated["remaining_work"]
    can_retire = (
        all_verified
        and not remaining_plan_steps
        and not updated.get("blocking_condition")
    )
    if event_type in {"verifier_completed", "task_complete_requested", "stage_exit"}:
        if can_retire and (
            event_type == "verifier_completed"
            or task_event.get("retire_if_complete") is True
        ):
            updated["lifecycle"] = "RETIRED"
            updated["next_action"] = None
            updated["next_action_reason"] = "all_acceptance_items_verified"
            updated["retirement"] = {
                "outcome": "completed",
                "reason": "all_acceptance_items_verified",
                "evidence_refs": evidence_refs,
            }
            reasons.append("all_acceptance_items_verified")
        elif event_type == "task_complete_requested" and not all_verified:
            updated["lifecycle"] = "VERIFYING"

    if updated["lifecycle"] != "RETIRED":
        first_plan = _first_pending_plan_step(updated)
        first = _first_remaining(updated)
        if first_plan is not None:
            updated["next_action"] = first_plan.get("text")
            updated["next_action_reason"] = "earliest_incomplete_plan_step"
        elif first is not None and "next_action" not in task_event:
            updated["next_action"] = first.get("text")
            updated["next_action_reason"] = "earliest_incomplete_acceptance_item"
    updated["progress_revision"] = int(updated.get("progress_revision") or 0) + 1
    updated["context_epoch"] = _derive_context_epoch(updated)
    return {
        "schema": TRANSITION_SCHEMA,
        "capsule": updated,
        "changed": True,
        "previous_lifecycle": previous_lifecycle,
        "lifecycle": updated["lifecycle"],
        "progress_delta": progress_delta,
        "event_outcome": _bounded_text(task_event.get("outcome") or "observed", 160),
        "transition_reasons": list(dict.fromkeys(reasons)),
        "event_type": event_type,
    }


def reconcile_progress(
    capsule: Mapping[str, Any], evidence_event: Mapping[str, Any]
) -> dict[str, Any]:
    """Compatibility name for applying one evidence-bearing progress event."""

    return apply_task_event(capsule, evidence_event)


def _reminder_candidate(
    capsule: Mapping[str, Any], transition: Mapping[str, Any]
) -> tuple[str, str, str, str] | None:
    event_type = str(transition.get("event_type") or "")
    last_event = capsule.get("last_event") if isinstance(capsule.get("last_event"), Mapping) else {}
    if "workspace_anchor_changed" in (transition.get("transition_reasons") or []):
        epoch = (
            capsule.get("context_epoch", {}).get("epoch_id")
            if isinstance(capsule.get("context_epoch"), Mapping)
            else "unknown"
        )
        return (
            "workspace_revalidation_required",
            f"workspace_revalidation:{epoch}",
            "re-read workspace-bound evidence and revalidate prior progress before reusing it as current fact",
            "before_next_action",
        )
    if event_type == "task_observed" and capsule.get("semantic_review_required"):
        return (
            "global_alignment_required",
            f"global_alignment:{capsule.get('goal_revision')}:{last_event.get('event_id')}",
            "interpret the current turn against the stable global goal; preserve it unless current replacement evidence is explicit and reviewed",
            "before_action_selection",
        )
    if event_type == "progress_snapshot" and capsule.get("plan_steps"):
        return (
            "plan_alignment_review_required",
            f"plan_alignment:{capsule.get('goal_revision')}:{last_event.get('event_id')}",
            "relate the next plan action to a required output and an acceptance criterion before executing it",
            "before_next_plan_action",
        )
    action = (
        capsule.get("current_action")
        if isinstance(capsule.get("current_action"), Mapping)
        else {}
    )
    if event_type in {"candidate_selected", "preflight_completed", "tool_dispatched"} and not capsule.get(
        "reuse_candidates"
    ) and (
        action.get("binding_source") != "explicit_event"
        or not (action.get("serves_output_ids") or action.get("serves_criterion_ids"))
    ):
        timing = (
            "next_model_boundary_after_dispatch"
            if event_type == "tool_dispatched"
            else "before_tool_dispatch"
        )
        required_action = (
            "before choosing the next action, bind it to a required output or acceptance criterion and state its semantic postcondition; this does not validate the already-started tool"
            if event_type == "tool_dispatched"
            else "bind the selected action to a required output or acceptance criterion and state its semantic postcondition before tool dispatch"
        )
        return (
            "action_alignment_review_required",
            f"action_alignment:{capsule.get('goal_revision')}:{last_event.get('event_id')}",
            required_action,
            timing,
        )
    if event_type in {"candidate_selected", "preflight_completed", "tool_dispatched"} and capsule.get(
        "reuse_candidates"
    ):
        return (
            "reuse_before_regenerate",
            f"reuse_before_regenerate:{capsule.get('goal_revision')}:{last_event.get('event_id')}",
            "reuse or copy the exact compatible source before generating an equivalent replacement",
            (
                "next_model_boundary_after_dispatch"
                if event_type == "tool_dispatched"
                else "before_tool_dispatch"
            ),
        )
    if event_type == "side_task_completed":
        return (
            "resume_suspended_task",
            f"resume_suspended_task:{last_event.get('event_id')}",
            "resume the preserved global task from its earliest incomplete acceptance item",
            "before_next_action",
        )
    if event_type == "unchanged_dispatch_repeated":
        failures = capsule.get("unresolved_failures") or []
        signature = failures[-1].get("signature") if failures and isinstance(failures[-1], Mapping) else "unknown"
        return (
            "unchanged_dispatch_repeated",
            f"unchanged_dispatch:{signature}",
            "select a changed, mechanically verified dispatch path before retrying",
            "before_retry",
        )
    if event_type == "verifier_pending":
        return (
            "verifier_pending",
            f"verifier_pending:{last_event.get('event_id')}",
            "run or obtain the declared verifier before claiming completion",
            "before_completion_claim",
        )
    pending_plan = _first_pending_plan_step(capsule)
    if event_type in {"tool_result_received", "write_result_received"} and pending_plan:
        step_text = _bounded_text(pending_plan.get("text"), 400)
        return (
            "plan_continuation_required",
            f"plan_continuation:{capsule.get('goal_revision')}:{pending_plan.get('id')}",
            f"verify the current result, then continue with the next planned step: {step_text}",
            "before_next_plan_action",
        )
    if event_type in {"tool_result_received", "write_result_received"} and transition.get(
        "progress_delta"
    ) == ["criterion_inferred"]:
        return (
            "missing_postcondition",
            f"missing_postcondition:{last_event.get('event_id')}",
            "verify the semantic postcondition; command success alone is insufficient",
            "before_completion_claim",
        )
    if event_type in {"stage_exit", "task_complete_requested"} and pending_plan:
        return (
            "open_plan_steps_at_stage_exit",
            f"open_plan_steps:{capsule.get('goal_revision')}:{pending_plan.get('id')}",
            f"continue with the earliest incomplete planned step: {_bounded_text(pending_plan.get('text'), 400)}",
            "before_stage_exit",
        )
    if event_type in {"stage_exit", "task_complete_requested"} and capsule.get("remaining_work"):
        return (
            "open_loops_at_stage_exit",
            f"open_loops:{capsule.get('current_stage')}",
            "continue from the earliest incomplete acceptance item or report the blocker",
            "before_stage_exit",
        )
    finalization_review = classify_finalization_review_need(
        capsule,
        finalization_event=event_type in {"stage_exit", "task_complete_requested"},
    )
    if finalization_review["status"] == "required" and event_type in {
        "stage_exit",
        "task_complete_requested",
        "verifier_completed",
        "write_result_received",
    }:
        epoch = (
            capsule.get("context_epoch", {}).get("epoch_id")
            if isinstance(capsule.get("context_epoch"), Mapping)
            else "unknown"
        )
        return (
            "finalization_surface_review_required",
            f"finalization_review:{epoch}:{finalization_review.get('surface')}",
            "review reader-facing final surfaces against the accepted state, remove rejected-alternative residue, then read them back",
            "before_final_delivery",
        )
    if event_type in {"transport_threshold_crossed", "continuation_required"}:
        return (
            "transport_continuation_required",
            f"transport:{last_event.get('event_id')}",
            "continue with the content-bound cursor and preserve the full-result hash",
            "before_continuation",
        )
    return None


def build_dynamic_reminders(
    capsule: Mapping[str, Any], transition: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Emit at most one transition reminder and bind dedupe to the revision."""

    candidate = _reminder_candidate(capsule, transition)
    if candidate is None:
        return []
    trigger, base_key, required_action, timing = candidate
    revision = int(capsule.get("progress_revision") or 0)
    receipt_key = f"{base_key}@{revision}"
    reminder_state = capsule.get("reminder_state")
    emitted = list(reminder_state.get("emitted") or []) if isinstance(reminder_state, Mapping) else []
    if receipt_key in emitted:
        return []
    snapshot = _copy(capsule)
    snapshot["reminder_state"] = {"emitted": [*emitted, receipt_key][-MAX_EVENT_HISTORY:]}
    reminder_id = _sha256_text(f"{snapshot.get('capsule_id')}:{receipt_key}")[:24]
    return [
        {
            "schema": REMINDER_SCHEMA,
            "reminder_id": reminder_id,
            "capsule_id": snapshot.get("capsule_id"),
            "progress_revision": revision,
            "context_epoch_id": (
                snapshot.get("context_epoch", {}).get("epoch_id")
                if isinstance(snapshot.get("context_epoch"), Mapping)
                else None
            ),
            "trigger": trigger,
            "scope": "current_task_only",
            "severity": "action_required",
            "dedupe_key": base_key,
            "required_action": required_action,
            "timing": timing,
            "expires_when": "required_action_satisfied_or_task_retired",
            "evidence_refs": list(snapshot.get("evidence_refs") or []),
            "capsule_snapshot": snapshot,
        }
    ]


def _positive_int(value: Any, *, name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def plan_transport(
    capsule: Mapping[str, Any] | None,
    result_shape: Mapping[str, Any],
    host_limits: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Plan one bounded forwarding envelope; host hard limits only tighten."""

    if not isinstance(result_shape, Mapping):
        raise ValueError("result_shape must be an object")
    limits = host_limits if isinstance(host_limits, Mapping) else {}
    max_chars = min(
        DEFAULT_MAX_CHARS,
        _positive_int(limits.get("max_chars"), name="max_chars", default=DEFAULT_MAX_CHARS),
    )
    max_items = min(
        DEFAULT_MAX_ITEMS,
        _positive_int(limits.get("max_items"), name="max_items", default=DEFAULT_MAX_ITEMS),
    )
    kind = str(result_shape.get("kind") or "canonical_json")
    if kind not in {"text", "items", "canonical_json"}:
        kind = "canonical_json"
    return {
        "schema": TRANSPORT_SCHEMA,
        "profile": "evidence_preserving",
        "mode": kind,
        "max_chars": max_chars,
        "max_items": max_items,
        "host_hard_chars": limits.get("max_chars"),
        "host_hard_items": limits.get("max_items"),
        "mandatory_paths": [
            "schema",
            "objective",
            "acceptance_criteria",
            "unresolved_failures",
            "next_action",
            "full_result_sha256",
            "next_cursor",
        ],
        "capsule_revision": capsule.get("progress_revision") if isinstance(capsule, Mapping) else None,
        "continuation_on_overflow": True,
    }


def _sanitize_for_transport(value: Any) -> Any:
    if isinstance(value, Mapping):
        media = value.get("type") in {"image", "audio", "resource"}
        sanitized: dict[str, Any] = {}
        omitted = False
        for key, item in value.items():
            if key in _INLINE_PAYLOAD_KEYS and (media or isinstance(item, (bytes, bytearray))):
                omitted = True
                continue
            if key in {"image_url", "audio_url"} and isinstance(item, str) and item.startswith("data:"):
                omitted = True
                continue
            sanitized[str(key)] = _sanitize_for_transport(item)
        if omitted:
            sanitized["inline_payload_omitted_from_text"] = True
        return sanitized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_sanitize_for_transport(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return {"binary_payload_omitted": True, "sha256": hashlib.sha256(bytes(value)).hexdigest()}
    return value


def _validate_cursor(cursor: Mapping[str, Any] | None, digest: str, mode: str) -> None:
    if cursor is None:
        return
    if not isinstance(cursor, Mapping):
        raise ValueError("cursor must be an object or null")
    if cursor.get("result_sha256") != digest:
        raise ValueError("cursor_result_hash_mismatch")
    if cursor.get("mode") not in {None, mode}:
        raise ValueError("cursor_mode_mismatch")


def page_result(
    normalized_result: Any,
    transport_plan: Mapping[str, Any],
    cursor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Page safe normalized content with a hash-bound continuation cursor."""

    if not isinstance(transport_plan, Mapping) or transport_plan.get("schema") != TRANSPORT_SCHEMA:
        raise ValueError("invalid transport plan")
    max_chars = min(
        DEFAULT_MAX_CHARS,
        _positive_int(
            transport_plan.get("max_chars"),
            name="max_chars",
            default=DEFAULT_MAX_CHARS,
        ),
    )
    max_items = min(
        DEFAULT_MAX_ITEMS,
        _positive_int(
            transport_plan.get("max_items"),
            name="max_items",
            default=DEFAULT_MAX_ITEMS,
        ),
    )
    safe = _sanitize_for_transport(normalized_result)
    if isinstance(safe, list):
        mode = "items"
        full_text = _canonical_json(safe)
        digest = _sha256_text(full_text)
        _validate_cursor(cursor, digest, mode)
        start = int(cursor.get("next_item") or 0) if cursor else 0
        if start < 0 or start > len(safe):
            raise ValueError("cursor_item_out_of_range")
        page_items: list[Any] = []
        index = start
        while index < len(safe) and len(page_items) < max_items:
            prospective = [*page_items, safe[index]]
            if len(_canonical_json(prospective)) > max_chars:
                if not page_items:
                    raise ValueError("single_item_exceeds_transport_char_limit")
                break
            page_items = prospective
            index += 1
        next_cursor = (
            {"result_sha256": digest, "mode": mode, "next_item": index}
            if index < len(safe)
            else None
        )
        page_text = _canonical_json(page_items)
        return {
            "schema": PAGE_SCHEMA,
            "mode": mode,
            "items": page_items,
            "page_sha256": _sha256_text(page_text),
            "full_result_sha256": digest,
            "original_items": len(safe),
            "forwarded_items": len(page_items),
            "uncovered_items": len(safe) - index,
            "forwarded_chars": len(page_text),
            "next_cursor": next_cursor,
            "complete": next_cursor is None,
        }

    mode = "text"
    full_text = safe if isinstance(safe, str) else _canonical_json(safe)
    digest = _sha256_text(full_text)
    _validate_cursor(cursor, digest, mode)
    start = int(cursor.get("next_char") or 0) if cursor else 0
    if start < 0 or start > len(full_text):
        raise ValueError("cursor_char_out_of_range")
    end = min(len(full_text), start + max_chars)
    content = full_text[start:end]
    next_cursor = (
        {"result_sha256": digest, "mode": mode, "next_char": end}
        if end < len(full_text)
        else None
    )
    return {
        "schema": PAGE_SCHEMA,
        "mode": mode,
        "content": content,
        "page_sha256": _sha256_text(content),
        "full_result_sha256": digest,
        "original_chars": len(full_text),
        "forwarded_chars": len(content),
        "uncovered_chars": len(full_text) - end,
        "next_cursor": next_cursor,
        "complete": next_cursor is None,
    }


def _context_item(item: Any) -> Any:
    if isinstance(item, Mapping):
        return {
            key: item.get(key)
            for key in ("id", "text", "status", "signature", "error_class")
            if item.get(key) is not None
        }
    return _bounded_text(item, 800)


def _context_global_anchor(anchor: Any) -> dict[str, Any] | None:
    if not isinstance(anchor, Mapping):
        return None
    return _compact_context_mapping(
        {
            "schema": anchor.get("schema"),
            "source_capsule_id": anchor.get("source_capsule_id"),
            "objective": anchor.get("objective"),
            "objective_sha256": anchor.get("objective_sha256"),
            "purpose": anchor.get("purpose"),
            "required_output_ids": [
                str(item.get("id"))
                for item in (anchor.get("required_outputs") or [])
                if isinstance(item, Mapping) and item.get("id")
            ][:MAX_CONTEXT_LIST_ITEMS],
            "acceptance_ids": [
                str(item.get("id"))
                for item in (anchor.get("acceptance_criteria") or [])
                if isinstance(item, Mapping) and item.get("id")
            ][:MAX_CONTEXT_LIST_ITEMS],
            "stop_condition": anchor.get("stop_condition"),
            "goal_revision": anchor.get("goal_revision"),
        },
        required={"schema", "objective", "objective_sha256", "goal_revision"},
    )


def _context_side_frame(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    resume = item.get("resume_entry") if isinstance(item.get("resume_entry"), Mapping) else {}
    return _compact_context_mapping(
        {
            "source_capsule_id": item.get("source_capsule_id"),
            "objective": item.get("objective"),
            "resume_entry": {
                key: _copy(resume.get(key))
                for key in (
                    "global_objective",
                    "current_stage",
                    "next_action",
                    "progress_revision",
                )
                if resume.get(key) is not None
            },
        },
        required=set(),
    )


def _compact_context_mapping(
    payload: Mapping[str, Any],
    *,
    required: set[str],
) -> dict[str, Any]:
    """Drop only semantically empty optional fields from model-facing context."""

    compact: dict[str, Any] = {}
    for key, value in payload.items():
        if key in required:
            compact[key] = value
            continue
        if value is None or value == "" or value is False:
            continue
        if isinstance(value, (list, tuple, dict, set)) and not value:
            continue
        compact[key] = value
    return compact


def build_task_capsule_context(
    capsule: Mapping[str, Any],
    reminders: Sequence[Mapping[str, Any]],
    *,
    host_limits: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one bounded model-facing operational context entry."""

    if capsule.get("lifecycle") == "RETIRED":
        return {
            "schema": "cbh.task_capsule_context.v1",
            "entry": None,
            "control_entry": None,
            "char_count": 0,
            "delivery": "not_needed",
        }
    runtime_projection = project_runtime(
        "global_causal_task",
        capsule=capsule,
    )
    projection_envelope = runtime_projection["envelope"]
    causal_relation = _copy(projection_envelope["payload"]["relation"])
    execution_attention = _copy(
        projection_envelope["payload"].get("execution_attention") or []
    )
    max_chars = min(3_200, _positive_int(
        host_limits.get("max_chars") if isinstance(host_limits, Mapping) else None,
        name="max_chars",
        default=3_200,
    ))
    max_tokens = min(
        900,
        _positive_int(
            host_limits.get("max_tokens") if isinstance(host_limits, Mapping) else None,
            name="max_tokens",
            default=900,
        ),
    )
    full_capsule_sha256 = _sha256_text(
        _canonical_json(_sanitize_for_transport(capsule))
    )
    source_lists = {
        "constraints": list(capsule.get("constraints") or []),
        "required_outputs": list(capsule.get("required_outputs") or []),
        "plan_steps": list(capsule.get("plan_steps") or []),
        "verified_completed": list(capsule.get("verified_completed") or []),
        "inferred_progress": list(capsule.get("inferred_progress") or []),
        "remaining_work": list(capsule.get("remaining_work") or []),
        "unresolved_failures": list(capsule.get("unresolved_failures") or []),
        "suspended_task_stack": list(capsule.get("suspended_task_stack") or []),
        "reuse_candidates": list(capsule.get("reuse_candidates") or []),
    }
    memory_working_set = _copy(capsule.get("memory_working_set") or {})
    if (
        isinstance(memory_working_set, Mapping)
        and memory_working_set.get("coverage_status") == "not_requested"
        and not any(
            memory_working_set.get(key)
            for key in (
                "selected_record_ids",
                "selected_records",
                "candidate_views",
                "source_ids",
                "evidence_handles",
                "conversation_handles",
                "opened_evidence_refs",
            )
        )
        and memory_working_set.get("unresolved_ambiguity") is not True
    ):
        memory_working_set = {}
    uncovered_counts = {
        name: max(0, len(values) - MAX_CONTEXT_LIST_ITEMS)
        for name, values in source_lists.items()
        if len(values) > MAX_CONTEXT_LIST_ITEMS
    }
    payload = {
        "schema": CAPSULE_SCHEMA,
        "capsule_id": capsule.get("capsule_id"),
        "state_version": capsule.get("state_version"),
        "lifecycle": capsule.get("lifecycle"),
        "progress_revision": capsule.get("progress_revision"),
        "reading_order": [
            "global_goal_anchor",
            "workspace_fact_status",
            "goal_deltas",
            "remaining_work",
            "next_action",
            "active_local_delta",
            "suspended_task_stack",
            "reuse_candidates",
        ],
        "global_goal_anchor": _context_global_anchor(capsule.get("global_goal_anchor")),
        "context_epoch_id": (
            capsule.get("context_epoch", {}).get("epoch_id")
            if isinstance(capsule.get("context_epoch"), Mapping)
            else None
        ),
        "runtime_projection_id": projection_envelope["projection_id"],
        "causal_relation": causal_relation,
        "workspace_fact_status": (
            {
                "status": "review_required",
                "reason": capsule.get("workspace_review_reason"),
                "required_action": (
                    "re-read workspace-bound evidence and revalidate prior progress"
                ),
            }
            if capsule.get("workspace_review_required")
            else None
        ),
        "objective": capsule.get("objective"),
        "purpose": capsule.get("purpose"),
        "required_outputs": [
            _context_item(item)
            for item in source_lists["required_outputs"][:MAX_CONTEXT_LIST_ITEMS]
        ],
        "stop_condition": capsule.get("stop_condition"),
        "goal_revision": capsule.get("goal_revision"),
        "goal_deltas": list(capsule.get("goal_deltas") or [])[-MAX_CONTEXT_LIST_ITEMS:],
        "last_user_delta": (
            {
                key: _copy(capsule["last_user_delta"].get(key))
                for key in ("kind", "turn_ref")
                if capsule["last_user_delta"].get(key) is not None
            }
            if isinstance(capsule.get("last_user_delta"), Mapping)
            else None
        ),
        "semantic_review_required": bool(capsule.get("semantic_review_required")),
        "turn_relation": _copy(capsule.get("turn_relation")),
        "active_local_delta": (
            {
                key: _copy(value)
                for key, value in capsule.get("active_local_delta", {}).items()
                if key != "text"
            }
            if isinstance(capsule.get("active_local_delta"), Mapping)
            else None
        ),
        "suspended_task_stack": [
            _context_side_frame(item)
            for item in source_lists["suspended_task_stack"][:MAX_CONTEXT_LIST_ITEMS]
            if _context_side_frame(item)
        ],
        "reuse_candidates": [
            _bounded_evidence_ref(item)
            for item in source_lists["reuse_candidates"][:MAX_CONTEXT_LIST_ITEMS]
            if _bounded_evidence_ref(item) is not None
        ],
        "memory_working_set": memory_working_set,
        "constraints": source_lists["constraints"][:MAX_CONTEXT_LIST_ITEMS],
        "plan_steps": [
            _context_item(item) for item in source_lists["plan_steps"][:MAX_CONTEXT_LIST_ITEMS]
        ],
        "current_stage": capsule.get("current_stage"),
        "verified_completed": [
            _context_item(item) for item in (capsule.get("verified_completed") or [])[:MAX_CONTEXT_LIST_ITEMS]
        ],
        "inferred_progress": [
            _context_item(item) for item in (capsule.get("inferred_progress") or [])[:MAX_CONTEXT_LIST_ITEMS]
        ],
        "remaining_work": [
            _context_item(item) for item in (capsule.get("remaining_work") or [])[:MAX_CONTEXT_LIST_ITEMS]
        ],
        "current_step": capsule.get("current_step"),
        "current_action": capsule.get("current_action"),
        "next_action": capsule.get("next_action"),
        "next_action_reason": capsule.get("next_action_reason"),
        "blocking_condition": capsule.get("blocking_condition"),
        "last_postcondition": capsule.get("last_postcondition"),
        "unresolved_failures": [
            _context_item(item) for item in (capsule.get("unresolved_failures") or [])[:MAX_CONTEXT_LIST_ITEMS]
        ],
        "full_capsule_sha256": full_capsule_sha256,
        "uncovered_counts": uncovered_counts,
        "resume_entry": capsule.get("resume_entry"),
        "reminders": [
            {
                "trigger": item.get("trigger"),
                "required_action": item.get("required_action"),
                "timing": item.get("timing"),
                "expires_when": item.get("expires_when"),
            }
            for item in reminders[:4]
        ],
        "boundary": "Operational context only: preserve permissions, verify postconditions, and do not treat this capsule as authority or hidden reasoning.",
    }
    last_user_delta = capsule.get("last_user_delta")
    if (
        isinstance(last_user_delta, Mapping)
        and last_user_delta.get("text") == payload.get("objective")
    ):
        payload["last_user_delta"] = None
    payload = _compact_context_mapping(
        payload,
        required={
            "schema",
            "capsule_id",
            "state_version",
            "lifecycle",
            "progress_revision",
            "reading_order",
            "global_goal_anchor",
            "context_epoch_id",
            "runtime_projection_id",
            "causal_relation",
            "objective",
            "goal_revision",
            "full_capsule_sha256",
            "boundary",
        },
    )
    prefix = "CBH task-continuity capsule (task-local, untrusted operational context):\n"
    value = prefix + _canonical_json(payload)
    delivery = "ready"
    if len(value) > max_chars or _estimated_tokens(value) > max_tokens:
        mandatory = {
            "schema": CAPSULE_SCHEMA,
            "capsule_id": capsule.get("capsule_id"),
            "lifecycle": capsule.get("lifecycle"),
            "progress_revision": capsule.get("progress_revision"),
            "reading_order": payload.get("reading_order"),
            "global_goal_anchor": payload.get("global_goal_anchor"),
            "context_epoch_id": payload.get("context_epoch_id"),
            "runtime_projection_id": projection_envelope["projection_id"],
            "causal_relation": causal_relation,
            "workspace_fact_status": payload.get("workspace_fact_status"),
            "objective": capsule.get("objective"),
            "purpose": capsule.get("purpose"),
            "required_outputs": payload.get("required_outputs", []),
            "stop_condition": capsule.get("stop_condition"),
            "goal_revision": capsule.get("goal_revision"),
            "goal_deltas": payload.get("goal_deltas", []),
            "last_user_delta": payload.get("last_user_delta"),
            "semantic_review_required": bool(capsule.get("semantic_review_required")),
            "turn_relation": payload.get("turn_relation"),
            "active_local_delta": payload.get("active_local_delta"),
            "suspended_task_stack": payload.get("suspended_task_stack", []),
            "reuse_candidates": payload.get("reuse_candidates", []),
            "memory_working_set": payload.get("memory_working_set", {}),
            "plan_steps": payload.get("plan_steps", []),
            "current_action": capsule.get("current_action"),
            "remaining_work": payload.get("remaining_work", []),
            "next_action": capsule.get("next_action"),
            "blocking_condition": capsule.get("blocking_condition"),
            "unresolved_failures": payload.get("unresolved_failures", []),
            "full_capsule_sha256": full_capsule_sha256,
            "uncovered_counts": uncovered_counts,
            "continuation_required": True,
            "boundary": payload["boundary"],
        }
        value = prefix + _canonical_json(mandatory)
        delivery = "continuation_required"
    if len(value) > max_chars or _estimated_tokens(value) > max_tokens:
        anchor = _context_global_anchor(capsule.get("global_goal_anchor"))
        if anchor:
            objective = str(anchor.get("objective") or "")
            if len(objective) > 600:
                anchor["objective"] = objective[:420] + " … " + objective[-160:]
                anchor["objective_original_chars"] = len(objective)
                anchor["objective_truncated"] = True
            if anchor.get("purpose"):
                anchor["purpose"] = _bounded_text(anchor["purpose"], 240)
            for key in ("required_output_ids", "acceptance_ids"):
                if isinstance(anchor.get(key), list):
                    anchor[key] = anchor[key][:4]
        identity = {
            "schema": CAPSULE_SCHEMA,
            "capsule_id": capsule.get("capsule_id"),
            "progress_revision": capsule.get("progress_revision"),
            "full_capsule_sha256": full_capsule_sha256,
            "global_goal_anchor": anchor,
            "context_epoch_id": payload.get("context_epoch_id"),
            "runtime_projection_id": projection_envelope["projection_id"],
            "causal_relation": causal_relation,
            "workspace_fact_status": payload.get("workspace_fact_status"),
            "goal_revision": capsule.get("goal_revision"),
            "semantic_review_required": bool(capsule.get("semantic_review_required")),
            "turn_relation": _copy(capsule.get("turn_relation")),
            "next_action": _bounded_text(capsule.get("next_action"), 320),
            "continuation_required": True,
        }
        identity = _compact_context_mapping(
            identity,
            required={
                "schema",
                "capsule_id",
                "progress_revision",
                "full_capsule_sha256",
                "global_goal_anchor",
                "context_epoch_id",
                "runtime_projection_id",
                "causal_relation",
                "goal_revision",
                "semantic_review_required",
                "continuation_required",
            },
        )
        value = prefix + _canonical_json(identity)
        delivery = "semantic_review_required"
    if len(value) > max_chars or _estimated_tokens(value) > max_tokens:
        raw_anchor = (
            capsule.get("global_goal_anchor")
            if isinstance(capsule.get("global_goal_anchor"), Mapping)
            else {}
        )
        objective = re.sub(r"\s+", " ", str(raw_anchor.get("objective") or "")).strip()
        relation = (
            {
                key: _copy(capsule["turn_relation"].get(key))
                for key in ("kind", "confidence", "source")
                if capsule["turn_relation"].get(key) is not None
            }
            if isinstance(capsule.get("turn_relation"), Mapping)
            else None
        )
        compact_causal_relation = {
            "status": causal_relation.get("status"),
            "specificity": causal_relation.get("specificity"),
        }
        focused_relation_id = next(
            iter(
                causal_relation.get("serves_output_ids")
                or causal_relation.get("serves_criterion_ids")
                or []
            ),
            None,
        )
        if focused_relation_id is not None:
            compact_causal_relation["focused_id"] = focused_relation_id

        def compact_identity(objective_limit: int, next_action_limit: int) -> dict[str, Any]:
            objective_preview = _bounded_text(objective, objective_limit)
            compact_anchor = {
                "objective": objective_preview,
                "objective_sha256": raw_anchor.get("objective_sha256") or _sha256_text(objective),
                "goal_revision": raw_anchor.get("goal_revision") or capsule.get("goal_revision"),
            }
            if len(objective_preview) < len(objective):
                compact_anchor["objective_original_chars"] = len(objective)
                compact_anchor["objective_truncated"] = True
            return _compact_context_mapping(
                {
                    "schema": CAPSULE_SCHEMA,
                    "capsule_id": capsule.get("capsule_id"),
                    "progress_revision": capsule.get("progress_revision"),
                    "full_capsule_sha256": full_capsule_sha256,
                    "global_goal_anchor": compact_anchor,
                    "context_epoch_id": (
                        capsule.get("context_epoch", {}).get("epoch_id")
                        if isinstance(capsule.get("context_epoch"), Mapping)
                        else None
                    ),
                    "causal_relation": compact_causal_relation,
                    "goal_revision": capsule.get("goal_revision"),
                    "semantic_review_required": bool(capsule.get("semantic_review_required")),
                    "turn_relation": relation,
                    "next_action": _bounded_text(capsule.get("next_action"), next_action_limit),
                    "continuation_required": True,
                },
                required={
                    "schema",
                    "capsule_id",
                    "progress_revision",
                    "full_capsule_sha256",
                    "global_goal_anchor",
                    "context_epoch_id",
                    "causal_relation",
                    "goal_revision",
                    "semantic_review_required",
                    "continuation_required",
                },
            )

        fitted_identity: dict[str, Any] | None = None
        for next_action_limit in (160, 80, 0):
            low = 1 if objective else 0
            high = min(600, len(objective))
            best_for_next: dict[str, Any] | None = None
            while low <= high:
                middle = (low + high) // 2
                candidate = compact_identity(middle, next_action_limit)
                candidate_value = prefix + _canonical_json(candidate)
                if len(candidate_value) <= max_chars and _estimated_tokens(candidate_value) <= max_tokens:
                    best_for_next = candidate
                    low = middle + 1
                else:
                    high = middle - 1
            if best_for_next is not None:
                fitted_identity = best_for_next
                break
        if fitted_identity is None:
            raise ValueError("mandatory_capsule_identity_exceeds_host_limit")
        identity = fitted_identity
        value = prefix + _canonical_json(identity)
        delivery = "semantic_review_required"
    first_remaining = _first_remaining(capsule)
    first_pending_plan = _first_pending_plan_step(capsule)
    lifecycle = str(capsule.get("lifecycle") or "ACTIVE")
    semantic_review_required = bool(capsule.get("semantic_review_required"))
    if capsule.get("workspace_review_required"):
        required_action_code = "workspace_revalidation_required"
    elif execution_attention:
        required_action_code = str(execution_attention[0].get("trigger"))
    elif semantic_review_required:
        required_action_code = "adjudicate_current_turn_without_replacing_global_goal"
    elif first_pending_plan is not None:
        required_action_code = "continue_pending_plan_before_completion"
    elif lifecycle == "VERIFYING":
        required_action_code = "verify_semantic_postcondition_before_completion"
    elif capsule.get("unresolved_failures"):
        required_action_code = "change_failed_action_then_verify"
    elif capsule.get("blocking_condition"):
        required_action_code = "report_blocker_or_continue_from_evidence"
    else:
        required_action_code = "continue_task_from_evidence_entry"
    control_payload = {
        "schema": "cbh.task_continuity_control.v1",
        "capsule_id": capsule.get("capsule_id"),
        "lifecycle": lifecycle,
        "progress_revision": capsule.get("progress_revision"),
        "goal_revision": capsule.get("goal_revision"),
        "context_epoch_id": (
            capsule.get("context_epoch", {}).get("epoch_id")
            if isinstance(capsule.get("context_epoch"), Mapping)
            else None
        ),
        "runtime_projection_id": projection_envelope["projection_id"],
        "runtime_projection_source_digest": projection_envelope["source_digest"],
        "causal_relation_status": causal_relation.get("status"),
        "causal_relation_specificity": causal_relation.get("specificity"),
        "global_goal_anchor_sha256": (
            capsule.get("global_goal_anchor", {}).get("objective_sha256")
            if isinstance(capsule.get("global_goal_anchor"), Mapping)
            else None
        ),
        "turn_delta_kind": (
            capsule.get("last_user_delta", {}).get("kind")
            if isinstance(capsule.get("last_user_delta"), Mapping)
            else None
        ),
        "semantic_review_required": bool(capsule.get("semantic_review_required")),
        "workspace_review_required": bool(capsule.get("workspace_review_required")),
        "required_action_code": required_action_code,
        "earliest_pending_id": (
            first_pending_plan.get("id")
            if first_pending_plan is not None
            else first_remaining.get("id") if first_remaining else None
        ),
        "reminder_triggers": [
            str(item.get("trigger"))
            for item in reminders[:4]
            if isinstance(item, Mapping) and item.get("trigger")
        ],
        "evidence_entry": "cbh.task_continuity.evidence",
        "authority_granted": False,
        "boundary": "Use the separate untrusted evidence entry for task text. Preserve permissions and verify postconditions before completion.",
    }
    if execution_attention:
        control_payload["required_action_timing"] = execution_attention[0].get(
            "timing"
        )
    if semantic_review_required:
        control_payload["turn_relation_options"] = [
            "global_goal_delta",
            "bounded_side_conversation",
            "explicit_global_replacement",
        ]
        control_payload["bounded_side_conversation_policy"] = (
            "answer_current_turn_then_keep_global_goal_resumable"
        )
    control_value = (
        "CBH task-continuity control (generated lifecycle codes only):\n"
        + _canonical_json(control_payload)
    )
    if len(control_value) > 1_600 or _estimated_tokens(control_value) > 450:
        raise ValueError("task_continuity_control_exceeds_host_limit")
    return {
        "schema": "cbh.task_capsule_context.v1",
        "entry": {"kind": "untrusted", "value": value},
        "control_entry": {"kind": "application", "value": control_value},
        "char_count": len(value),
        "control_char_count": len(control_value),
        "estimated_tokens": _estimated_tokens(value),
        "control_estimated_tokens": _estimated_tokens(control_value),
        "delivery": delivery,
    }


def build_task_candidate_context(
    capsule: Mapping[str, Any], task_event: Mapping[str, Any]
) -> dict[str, Any]:
    """Project an unbound turn without presenting it as committed task state."""

    source_units = _source_units(task_event)[:4]
    source_unit_ids = [str(unit.get("id")) for unit in source_units if unit.get("id")]
    candidate = {
        "schema": "cbh.task_candidate_evidence.v1",
        "active_task": {
            "capsule_id": capsule.get("capsule_id"),
            "goal_revision": int(capsule.get("goal_revision") or 1),
            "lifecycle": capsule.get("lifecycle"),
            "objective": _bounded_text(capsule.get("objective"), 240),
            "next_action": _bounded_text(capsule.get("next_action"), 120),
        },
        "candidate_turn": {
            "event_id": _event_id(task_event),
            "relation_nonce": _bounded_text(task_event.get("relation_nonce"), 256),
            "source_unit_ids": source_unit_ids,
        },
        "boundary": "unbound_readonly_comparison",
    }
    control = {
        "schema": "cbh.task_relation_control.v1",
        "required_action_code": "relation_receipt_required",
        "relation_receipt_schema": TURN_RELATION_RECEIPT_SCHEMA,
        "candidate_task_id": capsule.get("capsule_id"),
        "expected_goal_revision": int(capsule.get("goal_revision") or 1),
        "relation_nonce": _bounded_text(task_event.get("relation_nonce"), 256),
        "source_unit_ids": source_unit_ids,
        "state_mutation_allowed": False,
        "authority_granted": False,
        "boundary": "no_state_mutation_without_bound_receipt",
    }
    evidence_value = "CBH unbound task-candidate evidence:\n" + _canonical_json(candidate)
    control_value = "CBH task-relation control:\n" + _canonical_json(control)
    return {
        "schema": "cbh.task_capsule_context.v1",
        "entry": {"kind": "untrusted", "value": evidence_value},
        "control_entry": {"kind": "application", "value": control_value},
        "char_count": len(evidence_value),
        "control_char_count": len(control_value),
        "estimated_tokens": _estimated_tokens(evidence_value),
        "control_estimated_tokens": _estimated_tokens(control_value),
        "delivery": "relation_review_required",
    }


def process_worker_request(
    request: Mapping[str, Any],
    *,
    _workfile_transaction: Any | None = None,
) -> dict[str, Any]:
    if request.get("op") != "observe":
        raise ValueError("unsupported worker operation")
    route = request.get("route_receipt")
    task_event = request.get("task_event")
    capsule = request.get("capsule")
    if not isinstance(route, Mapping) or not isinstance(task_event, Mapping):
        raise ValueError("route_receipt and task_event must be objects")
    workfile = request.get("workfile")
    if workfile is not None and _workfile_transaction is None:
        if not isinstance(workfile, Mapping) or not isinstance(workfile.get("path"), str):
            raise ValueError("workfile.path must be a non-empty string")
        if (
            capsule is None
            and not Path(str(workfile["path"])).exists()
            and decide_task_continuity(route, task_event)["decision"]
            in {"dormant", "observe_once"}
        ):
            request_without_workfile = dict(request)
            request_without_workfile.pop("workfile", None)
            return process_worker_request(request_without_workfile)
        task_key = task_event.get("task_key")
        if task_key is None:
            raise ValueError("workfile_requires_task_key")
        from task_continuity_workfile import cumcwork_transaction

        with cumcwork_transaction(
            Path(str(workfile["path"])),
            expected_host_task_key_sha256=_sha256_text(str(task_key)),
        ) as transaction:
            return process_worker_request(
                request,
                _workfile_transaction=transaction,
            )
    workfile_state: dict[str, Any] | None = None
    workfile_path: Path | None = None
    workfile_scope: str | None = None
    workfile_recovery: dict[str, Any] | None = None
    if workfile is not None:
        if not isinstance(workfile, Mapping) or not isinstance(workfile.get("path"), str):
            raise ValueError("workfile.path must be a non-empty string")
        task_key = task_event.get("task_key")
        if task_key is None:
            raise ValueError("workfile_requires_task_key")
        workfile_path = Path(str(workfile["path"]))
        expected_scope = _sha256_text(str(task_key))
        workfile_scope = expected_scope
        if _workfile_transaction is None:
            raise ValueError("cumcwork_transaction_required")
        workfile_state = _workfile_transaction.state
        if workfile_state["status"] == "tail_repair_required":
            raise ValueError("cumcwork_tail_repair_required")
        persisted_capsule = workfile_state.get("active_capsule")
        if isinstance(capsule, Mapping) and isinstance(persisted_capsule, Mapping):
            if capsule.get("capsule_id") != persisted_capsule.get("capsule_id"):
                raise ValueError("cumcwork_capsule_identity_mismatch")
        restored_capsule = (
            ensure_v3_capsule(
                persisted_capsule,
                capsule_history=workfile_state.get("capsule_history"),
                source_head_sha256=workfile_state.get("head_sha256"),
                source_chain_digest=workfile_state.get("chain_digest"),
            )
            if isinstance(persisted_capsule, Mapping)
            else None
        )
        if isinstance(restored_capsule, Mapping) and not _bounded_text(
            restored_capsule.get("objective")
        ):
            retired_invalid = _copy(restored_capsule)
            retired_invalid["lifecycle"] = "RETIRED"
            retired_invalid["next_action"] = None
            retired_invalid["next_action_reason"] = "invalid_empty_objective_retired"
            retired_invalid["semantic_review_required"] = False
            retired_invalid["retirement"] = {
                "outcome": "invalidated",
                "reason": "invalid_empty_objective",
                "evidence_refs": list(retired_invalid.get("evidence_refs") or []),
            }
            retired_invalid["last_event"] = {
                "event_id": f"{_event_id(task_event)}:invalid-workfile-retirement",
                "type": "invalid_capsule_retired",
                "observed_at": task_event.get("observed_at"),
            }
            retired_invalid["progress_revision"] = int(
                retired_invalid.get("progress_revision") or 0
            ) + 1
            retired_invalid["context_epoch"] = _derive_context_epoch(retired_invalid)
            retirement_receipt = _workfile_transaction.append_snapshot(
                retired_invalid,
                (),
                expected_head_sha256=workfile_state.get("head_sha256"),
                expected_work_revision=int(workfile_state.get("work_revision") or 0),
            )
            workfile_state = _workfile_transaction.state
            workfile_recovery = {
                "status": "retired_invalid_empty_objective",
                "capsule_id": retired_invalid.get("capsule_id"),
                "work_revision": retirement_receipt.get("work_revision"),
                "head_sha256": retirement_receipt.get("head_sha256"),
            }
            capsule = None
            request = {**request, "pending_reminders": []}
        else:
            capsule = restored_capsule
        persisted_reminders = workfile_state.get("pending_reminders") or []
        if request.get("pending_reminders") is None:
            request = {**request, "pending_reminders": persisted_reminders}
    prior_capsule = (
        ensure_v3_capsule(capsule)
        if isinstance(capsule, Mapping)
        else None
    )
    effective_event = _copy(task_event)
    relation_binding_required = bool(
        prior_capsule
        and prior_capsule.get("lifecycle") != "RETIRED"
        and _requires_relation_binding_event(effective_event)
    )
    if relation_binding_required and not _bounded_text(
        effective_event.get("relation_nonce"), 256
    ):
        effective_event["relation_nonce"] = _derived_relation_nonce(
            effective_event, prior_capsule
        )
    typed_relation_receipt = _validated_turn_relation_receipt(
        effective_event, prior_capsule
    )
    if typed_relation_receipt is not None:
        effective_event["intent_kind"] = typed_relation_receipt["relation_kind"]
        effective_event["intent_confidence"] = "high"
        effective_event["intent_source"] = "typed_receipt"
        effective_event["intent_reason_codes"] = [
            "typed_relation_receipt_validated",
            *typed_relation_receipt["ambiguity_codes"],
        ][:8]
        effective_event["reviewed_against"] = {
            "capsule_id": typed_relation_receipt["candidate_task_id"],
            "goal_revision": typed_relation_receipt["expected_goal_revision"],
        }
        effective_event["semantic_review_passed"] = True
        effective_event["relation_commit_validated"] = (
            typed_relation_receipt["relation_kind"] != "ambiguous"
        )
    relation = _turn_relation(effective_event, prior_capsule)
    safe_readonly = bool(
        relation_binding_required
        and (
            typed_relation_receipt is None
            or typed_relation_receipt.get("relation_kind") == "ambiguous"
        )
    )
    superseding = bool(
        prior_capsule
        and prior_capsule.get("lifecycle") != "RETIRED"
        and not safe_readonly
        and _global_replacement_allowed(relation, prior_capsule)
    )
    preserve_global_goal = bool(
        prior_capsule
        and prior_capsule.get("lifecycle") != "RETIRED"
        and relation.get("kind") == "new_task"
        and not superseding
    )
    if superseding:
        effective_event["continuity_requested"] = True
        effective_event["supersedes_capsule_id"] = prior_capsule.get("capsule_id")
    elif preserve_global_goal:
        effective_event["intent_kind"] = "ambiguous"
        effective_event["semantic_review_required"] = True
        effective_event["intent_reason_codes"] = [
            *list(effective_event.get("intent_reason_codes") or [])[:7],
            "global_replacement_evidence_not_current",
        ]
    decision = (
        {
            "schema": DECISION_SCHEMA,
            "decision": "safe_readonly",
            "reasons": ["turn_relation_receipt_required"],
            "source_event_ids": [_event_id(effective_event)],
            "host_delivery": "ready",
        }
        if safe_readonly
        else decide_task_continuity(
            route,
            effective_event,
            None if superseding else prior_capsule,
        )
    )
    transition: dict[str, Any] | None = None
    reminders: list[dict[str, Any]] = []
    pending_reminders = request.get("pending_reminders")
    carried_reminders = (
        [
            {
                key: item.get(key)
                for key in (
                    "schema",
                    "trigger",
                    "required_action",
                    "timing",
                    "expires_when",
                    "dedupe_key",
                    "context_epoch_id",
                )
                if item.get(key) is not None
            }
            for item in pending_reminders[:8]
            if isinstance(item, Mapping)
        ]
        if not superseding
        and isinstance(pending_reminders, Sequence)
        and not isinstance(pending_reminders, (str, bytes))
        else []
    )
    current: dict[str, Any] | None
    if safe_readonly:
        current = _copy(prior_capsule)
        transition = {
            "schema": TRANSITION_SCHEMA,
            "capsule": current,
            "changed": False,
            "previous_lifecycle": str(current.get("lifecycle")),
            "lifecycle": str(current.get("lifecycle")),
            "progress_delta": [],
            "event_outcome": "unbound_turn_observed_readonly",
            "transition_reasons": ["typed_turn_relation_not_yet_validated"],
            "event_type": _event_type(effective_event),
        }
    elif superseding:
        current, _ = initialize_task_capsule(route, effective_event)
        transition = {
            "schema": TRANSITION_SCHEMA,
            "capsule": current,
            "changed": True,
            "previous_lifecycle": str(prior_capsule.get("lifecycle")),
            "lifecycle": current["lifecycle"],
            "progress_delta": ["task_superseded"],
            "event_outcome": "new_task_started",
            "transition_reasons": ["incompatible_new_task_superseded_prior_working_set"],
            "event_type": _event_type(effective_event),
        }
    elif capsule is None:
        if decision["decision"] != "dormant":
            current, transition = initialize_task_capsule(route, effective_event)
            if transition is not None:
                reminders = build_dynamic_reminders(current, transition)
        else:
            current = None
    else:
        if capsule.get("lifecycle") == "RETIRED":
            current = _copy(capsule)
        else:
            transition = apply_task_event(capsule, effective_event)
            current = transition["capsule"]
            if preserve_global_goal:
                transition["transition_reasons"] = [
                    "global_goal_preserved_pending_semantic_review"
                ]
            reminders = build_dynamic_reminders(current, transition)
    if reminders:
        current = reminders[-1]["capsule_snapshot"]
        reminders = [
            {key: value for key, value in reminder.items() if key != "capsule_snapshot"}
            for reminder in reminders
        ]
    context = (
        build_task_candidate_context(current, effective_event)
        if safe_readonly and current is not None
        else build_task_capsule_context(
            current,
            [*carried_reminders, *reminders],
            host_limits=request.get("host_limits") if isinstance(request.get("host_limits"), Mapping) else None,
        )
        if current is not None
        else {
            "schema": "cbh.task_capsule_context.v1",
            "entry": None,
            "control_entry": None,
            "char_count": 0,
            "control_char_count": 0,
            "estimated_tokens": 0,
            "control_estimated_tokens": 0,
            "delivery": "not_needed",
        }
    )
    dynamic_projection = (
        project_runtime(
            "dynamic_reminder",
            capsule=current,
            reminder=reminders[0],
            host_limits=(
                request.get("dynamic_host_limits")
                if isinstance(request.get("dynamic_host_limits"), Mapping)
                else None
            ),
        )
        if current is not None and reminders
        else None
    )
    result = {
        "schema": WORKER_SCHEMA,
        "decision": decision,
        "capsule": current,
        "transition": transition,
        "dynamic_reminders": reminders,
        "dynamic_context_entries": (
            {
                "control": dynamic_projection["control_entry"],
                "evidence": dynamic_projection["evidence_entry"],
            }
            if dynamic_projection is not None
            else None
        ),
        "dynamic_projection_id": (
            dynamic_projection["envelope"]["projection_id"]
            if dynamic_projection is not None
            else None
        ),
        "additional_context_entry": context["entry"],
        "additional_context_entries": {
            "control": context.get("control_entry"),
            "evidence": context["entry"],
        },
        "transport_receipt": {
            "delivery": context["delivery"],
            "char_count": context["char_count"],
            "estimated_tokens": context.get("estimated_tokens", 0),
            "evidence_char_count": context["char_count"],
            "control_char_count": context.get("control_char_count", 0),
            "total_char_count": (
                context["char_count"] + context.get("control_char_count", 0)
            ),
            "evidence_estimated_tokens": context.get("estimated_tokens", 0),
            "control_estimated_tokens": context.get("control_estimated_tokens", 0),
            "total_estimated_tokens": (
                context.get("estimated_tokens", 0)
                + context.get("control_estimated_tokens", 0)
            ),
        },
        "persistence": (
            "turn_local"
            if decision["decision"] == "observe_once" and current is not None
            else "none"
        ),
        "authority_granted": False,
    }
    if relation_binding_required and prior_capsule is not None:
        result["relation_binding"] = {
            "schema": "cbh.turn_relation_binding.v1",
            "required": True,
            "relation_nonce": effective_event.get("relation_nonce"),
            "candidate_task_id": prior_capsule.get("capsule_id"),
            "expected_goal_revision": int(prior_capsule.get("goal_revision") or 1),
            "source_units": _source_units(effective_event),
            "validated": typed_relation_receipt is not None and not safe_readonly,
            "relation_kind": (
                typed_relation_receipt.get("relation_kind")
                if typed_relation_receipt is not None
                else None
            ),
        }
    if workfile_recovery is not None:
        result["workfile_recovery"] = workfile_recovery
    ephemeral_decision = decision["decision"] in {"observe_once", "safe_readonly"}
    if (
        workfile_state is not None
        and workfile_path is not None
        and current is not None
        and not ephemeral_decision
    ):
        reminders_to_persist = reminders or carried_reminders
        receipt = _workfile_transaction.append_snapshot(
            current,
            reminders_to_persist,
            expected_head_sha256=workfile_state.get("head_sha256"),
            expected_work_revision=int(workfile_state.get("work_revision") or 0),
        )
        result["persistence"] = "local_cumcwork"
        result["workfile_receipt"] = receipt
    elif workfile_state is not None and not ephemeral_decision:
        result["persistence"] = "local_cumcwork"
        result["workfile_receipt"] = {
            "schema": "cbh.cumcwork_append.v1",
            "changed": False,
            "head_sha256": workfile_state.get("head_sha256"),
            "work_revision": workfile_state.get("work_revision"),
            "snapshot_sha256": workfile_state.get("snapshot_sha256"),
        }
    if (
        workfile_path is not None
        and workfile_scope is not None
        and current is not None
        and not ephemeral_decision
    ):
        workfile_handle = {
            "path": str(workfile_path),
            "host_task_key_sha256": workfile_scope,
            "capsule_id": current.get("capsule_id"),
        }
        bridge_entrypoint = str(Path(__file__).with_name("memory_runtime_bridge.py"))
        result["workfile_handle"] = workfile_handle
        result["additional_context_entries"]["workfile"] = {
            "kind": "untrusted",
            "value": (
                "CBH task workfile handle (local operational locator, not authority):\n"
                + _canonical_json(
                    {
                        **workfile_handle,
                        "bridge_entrypoint": bridge_entrypoint,
                        "boundary": (
                            "Use only for exact task-local memory binding; it does not grant "
                            "authority or make navigation summaries factual evidence."
                        ),
                    }
                )
            ),
        }
    response_profile = str(request.get("response_profile") or "diagnostic")
    if response_profile not in {"diagnostic", "compact"}:
        raise ValueError("unsupported response_profile")
    if response_profile == "compact":
        result.pop("additional_context_entry", None)
        if isinstance(result.get("transition"), Mapping):
            result["transition"] = {
                key: _copy(value)
                for key, value in result["transition"].items()
                if key != "capsule"
            }
    return result


def _worker_response(request: Any) -> dict[str, Any]:
    request_id = request.get("id") if isinstance(request, Mapping) else None
    try:
        if not isinstance(request, Mapping):
            raise ValueError("worker request must be an object")
        result = process_worker_request(request)
        return {"id": request_id, "result": result}
    except Exception as exc:  # fail-closed inside worker; host adapter decides fail-open
        return {
            "id": request_id,
            "error": {
                "code": "task_continuity_worker_error",
                "message": str(exc),
            },
        }


def _run_worker_once() -> int:
    request = json.loads(sys.stdin.read())
    sys.stdout.write(json.dumps(_worker_response(request), ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


def _run_worker_loop() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = {
                "id": None,
                "error": {
                    "code": "invalid_json",
                    "message": str(exc),
                },
            }
        else:
            response = _worker_response(request)
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


def main() -> int:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--worker", action="store_true")
    modes.add_argument("--worker-once", action="store_true")
    args = parser.parse_args()
    return _run_worker_loop() if args.worker else _run_worker_once()


if __name__ == "__main__":
    raise SystemExit(main())
