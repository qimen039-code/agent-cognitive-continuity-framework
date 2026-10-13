"""Deterministic, disposable model-facing projections for CBH runtime state.

Canonical truth remains in the supplied capsule, reminder, registry, or
evidence source.  This module performs no I/O, grants no authority, and stores
no hidden reasoning.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


PROJECTION_SCHEMA = "agent.runtime_projection.v1"
CONTROL_SCHEMA = "agent.runtime_projection.control.v1"
EVIDENCE_SCHEMA = "agent.runtime_projection.evidence.v1"
PROJECTION_TYPES = {"global_causal_task", "dynamic_reminder"}
HOST_ACTION_KINDS = {
    "commandExecution",
    "fileChange",
    "mcp",
    "dynamicTool",
    "webSearch",
    "subagent",
}
MAX_SOURCE_REFS = 4
DEFAULT_GLOBAL_CHARS = 3_200
DEFAULT_DYNAMIC_CHARS = 1_200

_READER_SURFACE_PATTERNS = (
    ("commit", re.compile(r"(?i)(\bcommit\b|提交说明|提交信息|提交标题)")),
    ("pull_request", re.compile(r"(?i)(pull request|\bPR\b|合并请求|拉取请求)")),
    ("release", re.compile(r"(?i)(\brelease\b|release notes?|发布说明|版本说明)")),
    ("readme", re.compile(r"(?i)(\bREADME\b|项目说明)")),
    ("ui_copy", re.compile(r"(?i)(UI copy|user-facing copy|界面文案|用户可见文案)")),
    ("title", re.compile(r"(?i)(\btitle\b|\bheading\b|标题|开篇|标签|文件名|注释|字幕)")),
    ("handoff", re.compile(r"(?i)(\bhandoff\b|final answer|final delivery|交付说明|交付总结|最终答复|最终交付)")),
)

_REF_KEYS = (
    "ref_id",
    "record_id",
    "source_id",
    "sha256",
    "status",
    "locator_kind",
    "candidate_label",
    "eligible_for_current_reuse",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _bounded_text(value: Any, limit: int = 800) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit]


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _ids(values: Any) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    result: list[str] = []
    for value in values:
        if isinstance(value, Mapping) and value.get("id") is not None:
            item = str(value["id"])
            if item not in result:
                result.append(item)
    return result[:12]


def _bounded_refs(*sources: Any) -> list[dict[str, Any] | str]:
    result: list[dict[str, Any] | str] = []
    for source in sources:
        if not isinstance(source, Sequence) or isinstance(source, (str, bytes)):
            continue
        for value in source:
            if isinstance(value, Mapping):
                item = {
                    key: _copy(value[key])
                    for key in _REF_KEYS
                    if key in value and value[key] is not None
                }
                if not item:
                    continue
            else:
                item = _bounded_text(value, 320)
                if not item:
                    continue
            if item not in result:
                result.append(item)
            if len(result) >= MAX_SOURCE_REFS:
                return result
    return result


def _task_scope(capsule: Mapping[str, Any]) -> dict[str, Any]:
    epoch = capsule.get("context_epoch")
    return {
        "capsule_id": capsule.get("capsule_id"),
        "context_epoch_id": (
            epoch.get("epoch_id") if isinstance(epoch, Mapping) else None
        ),
        "goal_revision": int(capsule.get("goal_revision") or 1),
        "progress_revision": int(capsule.get("progress_revision") or 0),
    }


def _relation(capsule: Mapping[str, Any]) -> dict[str, Any]:
    action = capsule.get("current_action")
    if not isinstance(action, Mapping):
        return {
            "status": "unresolved",
            "specificity": "unresolved",
            "serves_output_ids": [],
            "serves_criterion_ids": [],
            "reason": None,
        }
    output_ids = [str(value) for value in action.get("serves_output_ids") or []]
    criterion_ids = [str(value) for value in action.get("serves_criterion_ids") or []]
    action_text = _bounded_text(action.get("text"), 800)
    binding_source = _bounded_text(action.get("binding_source"), 80) or None
    state_progress_allowed = action.get("state_progress_allowed") is True
    if binding_source == "unbound":
        status = "unbound"
    elif binding_source == "explicit_event_unresolved":
        status = "unresolved_binding"
    elif output_ids or criterion_ids:
        status = (
            "explicit_binding"
            if binding_source in {None, "explicit_event"}
            else "fallback_binding"
        )
    elif action_text:
        status = "fallback_objective"
    else:
        status = "unresolved"
    relation = {
        "status": status,
        "specificity": (
            "host_kind_only"
            if action_text in HOST_ACTION_KINDS
            else "exact" if action_text else "unresolved"
        ),
        "serves_output_ids": output_ids[:12],
        "serves_criterion_ids": criterion_ids[:12],
        "reason": _bounded_text(action.get("reason"), 320) or None,
    }
    if binding_source is not None:
        relation["binding_source"] = binding_source
    if binding_source == "unbound" or "state_progress_allowed" in action:
        relation["state_progress_allowed"] = state_progress_allowed
    unresolved_binding_ids = [
        str(value) for value in action.get("unresolved_binding_ids") or []
    ][:12]
    if unresolved_binding_ids:
        relation["unresolved_binding_ids"] = unresolved_binding_ids
    return relation


def _correction_present(capsule: Mapping[str, Any]) -> bool:
    goal_deltas = capsule.get("goal_deltas")
    if isinstance(goal_deltas, Sequence) and not isinstance(goal_deltas, (str, bytes)):
        if any(
            isinstance(item, Mapping) and item.get("kind") == "correction"
            for item in goal_deltas
        ):
            return True
    for key in ("last_user_delta", "active_local_delta"):
        value = capsule.get(key)
        if isinstance(value, Mapping) and value.get("kind") == "correction":
            return True
    return False


def _reader_surface(capsule: Mapping[str, Any]) -> str | None:
    anchor = (
        capsule.get("global_goal_anchor")
        if isinstance(capsule.get("global_goal_anchor"), Mapping)
        else {}
    )
    values: list[Any] = [
        anchor.get("objective") or capsule.get("objective"),
        anchor.get("purpose") or capsule.get("purpose"),
        anchor.get("stop_condition") or capsule.get("stop_condition"),
        capsule.get("current_step"),
        capsule.get("next_action"),
    ]
    action = capsule.get("current_action")
    if isinstance(action, Mapping):
        values.append(action.get("text"))
    outputs = anchor.get("required_outputs") or capsule.get("required_outputs") or []
    if isinstance(outputs, Sequence) and not isinstance(outputs, (str, bytes)):
        values.extend(
            item.get("text")
            for item in outputs
            if isinstance(item, Mapping)
        )
    haystack = " ".join(_bounded_text(value, 1_000) for value in values if value)
    for surface, pattern in _READER_SURFACE_PATTERNS:
        if pattern.search(haystack):
            return surface
    return None


def classify_finalization_review_need(
    capsule: Mapping[str, Any], *, finalization_event: bool = False
) -> dict[str, Any]:
    """Classify a capability-neutral review of corrected final surfaces."""

    correction = _correction_present(capsule)
    if not correction:
        return {
            "status": "not_needed",
            "surface": None,
            "capability_query": None,
            "reason_codes": ["correction_absent"],
        }
    if finalization_event:
        return {
            "status": "required",
            "surface": "final_handoff",
            "capability_query": "review corrected reader-facing final surfaces",
            "reason_codes": ["correction_present", "finalization_event"],
        }
    surface = _reader_surface(capsule)
    if surface is None:
        return {
            "status": "not_needed",
            "surface": None,
            "capability_query": None,
            "reason_codes": [
                "correction_present",
                "reader_facing_surface_absent",
            ],
        }
    return {
        "status": "required",
        "surface": surface,
        "capability_query": "review corrected reader-facing final surfaces",
        "reason_codes": ["correction_present", "reader_facing_surface"],
    }


def classify_execution_attention(capsule: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project the smallest useful check before the first substantive action."""

    if int(capsule.get("progress_revision") or 0) != 1:
        return []
    if isinstance(capsule.get("current_action"), Mapping):
        return []
    reasons = {str(value) for value in capsule.get("activation_reasons") or []}
    if "write_intent" in reasons:
        return [
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
    if "tool_required" in reasons:
        return [
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
    return []


def _global_payload(capsule: Mapping[str, Any]) -> dict[str, Any]:
    anchor = (
        capsule.get("global_goal_anchor")
        if isinstance(capsule.get("global_goal_anchor"), Mapping)
        else {}
    )
    relation = _relation(capsule)
    focused = (
        relation["serves_output_ids"][0]
        if relation["serves_output_ids"]
        else relation["serves_criterion_ids"][0]
        if relation["serves_criterion_ids"]
        else (_ids(capsule.get("remaining_work")) or [None])[0]
    )
    must_preserve = [
        _bounded_text(value, 800)
        for value in [
            *(anchor.get("constraints") or capsule.get("constraints") or []),
            *(anchor.get("non_goals") or capsule.get("non_goals") or []),
        ]
        if _bounded_text(value, 800)
    ][:12]
    action = capsule.get("current_action")
    payload = {
        "global": {
            "objective": _bounded_text(anchor.get("objective") or capsule.get("objective"), 4_000),
            "purpose": _bounded_text(anchor.get("purpose") or capsule.get("purpose"), 1_000) or None,
            "required_output_ids": _ids(anchor.get("required_outputs") or capsule.get("required_outputs")),
            "acceptance_ids": _ids(anchor.get("acceptance_criteria") or capsule.get("acceptance_criteria")),
            "must_preserve": must_preserve,
            "stop_condition": _bounded_text(anchor.get("stop_condition") or capsule.get("stop_condition"), 800) or None,
        },
        "focus": {
            "current_stage": _bounded_text(capsule.get("current_stage"), 160) or None,
            "current_action": (
                _bounded_text(action.get("text"), 800)
                if isinstance(action, Mapping)
                else None
            ),
            "next_action": _bounded_text(capsule.get("next_action"), 800) or None,
            "blocking_condition": _bounded_text(capsule.get("blocking_condition"), 400) or None,
            "focused_output_or_criterion": focused,
        },
        "relation": relation,
        "progress": {
            "verified_ids": _ids(capsule.get("verified_completed")),
            "inferred_ids": _ids(capsule.get("inferred_progress")),
            "remaining_ids": _ids(capsule.get("remaining_work")),
        },
        "validity": {
            "semantic_review_required": bool(capsule.get("semantic_review_required")),
            "workspace_review_required": bool(capsule.get("workspace_review_required")),
        },
    }
    attention = classify_execution_attention(capsule)
    if attention:
        payload["execution_attention"] = attention
    finalization_review = classify_finalization_review_need(capsule)
    if finalization_review["status"] == "required":
        payload["finalization_review"] = finalization_review
    return payload


def _claim_block(trigger: str) -> str | None:
    return {
        "missing_postcondition": "semantic_postcondition_verified",
        "verifier_pending": "declared_verifier_completed",
        "workspace_revalidation_required": "workspace_revalidated",
        "open_loops_at_stage_exit": "remaining_work_resolved",
        "unchanged_dispatch_repeated": "changed_dispatch_verified",
        "finalization_surface_review_required": "final_surface_reviewed_and_read_back",
    }.get(trigger)


def _dynamic_payload(
    capsule: Mapping[str, Any], reminder: Mapping[str, Any]
) -> dict[str, Any]:
    relation = _relation(capsule)
    trigger = str(reminder.get("trigger") or "unknown")
    payload = {
        "trigger": trigger,
        "why_now_code": trigger,
        "required_action": _bounded_text(reminder.get("required_action"), 800),
        "serves_output_ids": relation["serves_output_ids"],
        "serves_criterion_ids": relation["serves_criterion_ids"],
        "claim_blocked_until": _claim_block(trigger),
        "expires_when": _bounded_text(reminder.get("expires_when"), 320) or None,
    }
    timing = _bounded_text(reminder.get("timing"), 160)
    if timing:
        payload["timing"] = timing
    return payload


def _projection_source(
    projection_type: str,
    capsule: Mapping[str, Any],
    reminder: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if projection_type == "global_causal_task":
        return {"capsule": _copy(capsule)}
    return {
        "task_scope": _task_scope(capsule),
        "current_action": _copy(capsule.get("current_action")),
        "reminder": _copy(reminder),
    }


def _with_projection_id(base: Mapping[str, Any]) -> dict[str, Any]:
    value = _copy(base)
    value.pop("projection_id", None)
    return {**value, "projection_id": _sha256(value)}


def _entry(prefix: str, value: Mapping[str, Any], *, kind: str) -> dict[str, str]:
    return {"kind": kind, "value": f"{prefix}\n{_canonical_json(value)}"}


def _estimated_tokens(value: str) -> int:
    return (len(value) + 3) // 4


def project_runtime(
    projection_type: str,
    *,
    capsule: Mapping[str, Any],
    reminder: Mapping[str, Any] | None = None,
    host_limits: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one deterministic projection and split model-facing entries."""

    if projection_type not in PROJECTION_TYPES:
        raise ValueError("unsupported_projection_type")
    if not isinstance(capsule, Mapping) or capsule.get("schema") != "cbh.task_capsule.v1":
        raise ValueError("invalid_projection_capsule")
    if projection_type == "dynamic_reminder":
        if not isinstance(reminder, Mapping):
            raise ValueError("dynamic_reminder_required")
        reminder_epoch = reminder.get("context_epoch_id")
        current_epoch = _task_scope(capsule).get("context_epoch_id")
        if reminder_epoch and current_epoch and reminder_epoch != current_epoch:
            raise ValueError("dynamic_reminder_epoch_mismatch")

    task_scope = _task_scope(capsule)
    source = _projection_source(projection_type, capsule, reminder)
    payload = (
        _global_payload(capsule)
        if projection_type == "global_causal_task"
        else _dynamic_payload(capsule, reminder or {})
    )
    source_refs = _bounded_refs(
        (reminder or {}).get("evidence_refs") if isinstance(reminder, Mapping) else [],
        capsule.get("evidence_refs") or [],
    )
    invalidates_on = (
        ["progress_revision_change", "context_epoch_change", "task_retired"]
        if projection_type == "global_causal_task"
        else [
            "context_epoch_change",
            "required_action_satisfied",
            "task_retired",
        ]
    )
    expires_when = (
        None
        if projection_type == "global_causal_task"
        else _bounded_text((reminder or {}).get("expires_when"), 320) or None
    )
    base = {
        "schema": PROJECTION_SCHEMA,
        "projection_type": projection_type,
        "task_scope": task_scope,
        "source_digest": _sha256(source),
        "coverage_status": "complete",
        "control": {
            "relation_status": (
                payload.get("relation", {}).get("status")
                if projection_type == "global_causal_task"
                else "action_required"
            ),
            "validity_review_required": bool(
                capsule.get("semantic_review_required")
                or capsule.get("workspace_review_required")
            ),
            "capability_query": (
                payload.get("finalization_review", {}).get("capability_query")
                if projection_type == "global_causal_task"
                else None
            ),
        },
        "payload": payload,
        "source_refs": source_refs,
        "invalidates_on": invalidates_on,
        "expires_when": expires_when,
        "authority_granted": False,
    }
    envelope = _with_projection_id(base)
    control_value = {
        "schema": CONTROL_SCHEMA,
        "projection_id": envelope["projection_id"],
        "projection_type": projection_type,
        "task_scope": task_scope,
        "source_digest": envelope["source_digest"],
        "coverage_status": envelope["coverage_status"],
        "control": _copy(envelope["control"]),
        "invalidates_on": invalidates_on,
        "expires_when": expires_when,
        "authority_granted": False,
    }
    evidence_value = {
        "schema": EVIDENCE_SCHEMA,
        "projection_id": envelope["projection_id"],
        "projection_type": projection_type,
        "coverage_status": envelope["coverage_status"],
        "payload": _copy(payload),
        "source_refs": _copy(source_refs),
        "invalidates_on": invalidates_on,
        "expires_when": expires_when,
    }
    control_entry = _entry(
        "CBH runtime projection control (generated codes only):",
        control_value,
        kind="application",
    )
    evidence_entry = _entry(
        "CBH runtime projection evidence (task-local, untrusted):",
        evidence_value,
        kind="untrusted",
    )
    default_chars = (
        DEFAULT_GLOBAL_CHARS
        if projection_type == "global_causal_task"
        else DEFAULT_DYNAMIC_CHARS
    )
    max_chars = int((host_limits or {}).get("max_chars") or default_chars)
    if len(evidence_entry["value"]) > max_chars:
        evidence_value["coverage_status"] = "bounded"
        evidence_value["source_refs"] = []
        if projection_type == "global_causal_task":
            evidence_value["payload"]["global"]["must_preserve"] = []
            for key in ("verified_ids", "inferred_ids", "remaining_ids"):
                evidence_value["payload"]["progress"][key] = evidence_value["payload"]["progress"][key][:4]
        evidence_entry = _entry(
            "CBH runtime projection evidence (task-local, untrusted):",
            evidence_value,
            kind="untrusted",
        )
    if len(evidence_entry["value"]) > max_chars:
        if projection_type == "global_causal_task":
            projected = evidence_value["payload"]
            evidence_value = {
                "schema": EVIDENCE_SCHEMA,
                "projection_type": projection_type,
                "coverage_status": "bounded",
                "payload": {
                    "global": {
                        "objective": projected["global"].get("objective"),
                        "required_output_ids": projected["global"].get("required_output_ids", [])[:4],
                        "acceptance_ids": projected["global"].get("acceptance_ids", [])[:4],
                        "stop_condition": projected["global"].get("stop_condition"),
                    },
                    "focus": {
                        "next_action": projected["focus"].get("next_action"),
                        "focused_output_or_criterion": projected["focus"].get("focused_output_or_criterion"),
                    },
                    "relation": {
                        "status": projected["relation"].get("status"),
                        "specificity": projected["relation"].get("specificity"),
                        "serves_output_ids": projected["relation"].get("serves_output_ids", [])[:2],
                        "serves_criterion_ids": projected["relation"].get("serves_criterion_ids", [])[:2],
                    },
                    "validity": _copy(projected["validity"]),
                },
            }
        else:
            evidence_value = {
                "schema": EVIDENCE_SCHEMA,
                "projection_type": projection_type,
                "coverage_status": "bounded",
                "payload": _copy(evidence_value["payload"]),
            }
        evidence_entry = _entry(
            "CBH runtime projection evidence (task-local, untrusted):",
            evidence_value,
            kind="untrusted",
        )
    if len(evidence_entry["value"]) > max_chars:
        raise ValueError("projection_mandatory_content_exceeds_host_limit")
    return {
        "schema": "agent.runtime_projection.bundle.v1",
        "envelope": envelope,
        "control_entry": control_entry,
        "evidence_entry": evidence_entry,
        "transport_receipt": {
            "coverage_status": evidence_value["coverage_status"],
            "control_chars": len(control_entry["value"]),
            "evidence_chars": len(evidence_entry["value"]),
            "control_estimated_tokens": _estimated_tokens(control_entry["value"]),
            "evidence_estimated_tokens": _estimated_tokens(evidence_entry["value"]),
        },
        "authority_granted": False,
    }
