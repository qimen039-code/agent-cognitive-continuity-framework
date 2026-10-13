"""Bind an existing CBH memory-consumption result to one exact task workfile.

This adapter is intentionally narrow: it selects and records navigation state.
It does not open raw evidence, write durable memory, or grant authority.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from harness_action_consumer import DEFAULT_COMMON_ERROR_META, build_action_consumption
from task_continuity import apply_task_event
from task_continuity_workfile import append_cumcwork_snapshot, rehydrate_cumcwork


RECEIPT_SCHEMA = "cbh.memory_runtime_bridge_receipt.v1"
QUERY_TYPES = {"current_state", "history_reason", "contradiction_check"}
QUERY_BASES = {"global_goal", "current_turn", "both"}


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _compact_result(working_set: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "query_type": working_set.get("query_type"),
        "query_basis": working_set.get("query_basis"),
        "bound_goal_revision": working_set.get("bound_goal_revision"),
        "coverage_status": working_set.get("coverage_status"),
        "selected_record_ids": list(working_set.get("selected_record_ids") or []),
        "candidate_views": list(working_set.get("candidate_views") or []),
        "source_ids": list(working_set.get("source_ids") or []),
        "evidence_handles": list(working_set.get("evidence_handles") or []),
        "unresolved_ambiguity": bool(working_set.get("unresolved_ambiguity")),
        "receipt_sha256": working_set.get("receipt_sha256"),
    }


def bind_memory_query_to_workfile(
    path: Path,
    *,
    expected_host_task_key_sha256: str,
    route: Mapping[str, Any],
    prompt: str,
    event_id: str,
    query_type: str = "history_reason",
    query_basis: str = "global_goal",
    tool_input_text: str = "",
    meta_path: Path = DEFAULT_COMMON_ERROR_META,
) -> dict[str, Any]:
    if query_type not in QUERY_TYPES:
        raise ValueError("unsupported_memory_query_type")
    if query_basis not in QUERY_BASES:
        raise ValueError("unsupported_memory_query_basis")
    if not event_id.strip():
        raise ValueError("memory_event_id_required")
    state = rehydrate_cumcwork(
        Path(path),
        expected_host_task_key_sha256=expected_host_task_key_sha256,
    )
    if state.get("status") != "clean":
        raise ValueError(f"cumcwork_not_clean:{state.get('status')}")
    active = state.get("active_capsule")
    if not isinstance(active, Mapping):
        raise ValueError("active_task_working_set_required")
    anchor = active.get("global_goal_anchor")
    global_prompt = str(
        anchor.get("objective")
        if isinstance(anchor, Mapping) and anchor.get("objective")
        else active.get("objective") or ""
    )
    if query_basis == "global_goal":
        if not global_prompt.strip():
            raise ValueError("global_goal_query_requires_nonempty_objective")
        effective_prompt = global_prompt
    elif query_basis == "current_turn":
        effective_prompt = prompt
    else:
        effective_prompt = "\n".join(
            value for value in (global_prompt, prompt) if value
        )
        if not effective_prompt.strip():
            raise ValueError("memory_query_requires_nonempty_prompt")
    consumption = build_action_consumption(
        dict(route),
        prompt=effective_prompt,
        tool_input_text=tool_input_text,
        meta_path=Path(meta_path),
        query_type=query_type,
    )
    applied = apply_task_event(
        active,
        {
            "schema": "cbh.task_event.v1",
            "event_id": event_id,
            "type": "memory_context_selected",
            "observed_at": _now_utc(),
            "memory_query_type": query_type,
            "memory_query_basis": query_basis,
            "memory_consumption_receipt": consumption,
        },
    )
    updated = applied["capsule"]
    append_receipt = append_cumcwork_snapshot(
        Path(path),
        updated,
        state.get("pending_reminders") or (),
        expected_head_sha256=state.get("head_sha256"),
        expected_work_revision=int(state.get("work_revision") or 0),
    )
    working_set = updated.get("memory_working_set") or {}
    return {
        "schema": RECEIPT_SCHEMA,
        "status": "bound",
        "work_revision": append_receipt.get("work_revision"),
        "head_sha256": append_receipt.get("head_sha256"),
        **_compact_result(working_set),
        "authority_granted": False,
        "raw_evidence_opened": False,
        "durable_memory_written": False,
    }


def record_opened_evidence_to_workfile(
    path: Path,
    *,
    expected_host_task_key_sha256: str,
    event_id: str,
    evidence_ref: Mapping[str, Any],
) -> dict[str, Any]:
    if not event_id.strip():
        raise ValueError("memory_event_id_required")
    state = rehydrate_cumcwork(
        Path(path),
        expected_host_task_key_sha256=expected_host_task_key_sha256,
    )
    if state.get("status") != "clean":
        raise ValueError(f"cumcwork_not_clean:{state.get('status')}")
    active = state.get("active_capsule")
    if not isinstance(active, Mapping):
        raise ValueError("active_task_working_set_required")
    applied = apply_task_event(
        active,
        {
            "schema": "cbh.task_event.v1",
            "event_id": event_id,
            "type": "memory_evidence_opened",
            "observed_at": _now_utc(),
            "evidence_ref": dict(evidence_ref),
        },
    )
    updated = applied["capsule"]
    append_receipt = append_cumcwork_snapshot(
        Path(path),
        updated,
        state.get("pending_reminders") or (),
        expected_head_sha256=state.get("head_sha256"),
        expected_work_revision=int(state.get("work_revision") or 0),
    )
    opened_refs = updated.get("memory_working_set", {}).get("opened_evidence_refs") or []
    return {
        "schema": RECEIPT_SCHEMA,
        "status": "recorded",
        "work_revision": append_receipt.get("work_revision"),
        "head_sha256": append_receipt.get("head_sha256"),
        "opened_evidence_ref": opened_refs[-1],
        "authority_granted": False,
        "durable_memory_written": False,
    }


def _load_route(args: argparse.Namespace) -> dict[str, Any]:
    if args.route_file:
        value = json.loads(Path(args.route_file).read_text(encoding="utf-8-sig"))
    else:
        value = json.loads(args.route_json)
    if not isinstance(value, dict):
        raise ValueError("route_must_be_an_object")
    return value


def _load_evidence_ref(args: argparse.Namespace) -> dict[str, Any]:
    if args.evidence_ref_file:
        value = json.loads(Path(args.evidence_ref_file).read_text(encoding="utf-8-sig"))
    else:
        value = json.loads(args.evidence_ref_json)
    if not isinstance(value, dict):
        raise ValueError("evidence_ref_must_be_an_object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--operation",
        choices=("bind-memory-query", "record-opened-evidence"),
        default="bind-memory-query",
    )
    parser.add_argument("--workfile", required=True)
    parser.add_argument("--host-task-key-sha256", required=True)
    route_group = parser.add_mutually_exclusive_group()
    route_group.add_argument("--route-file")
    route_group.add_argument("--route-json")
    evidence_group = parser.add_mutually_exclusive_group()
    evidence_group.add_argument("--evidence-ref-file")
    evidence_group.add_argument("--evidence-ref-json")
    parser.add_argument("--prompt")
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--query-type", choices=sorted(QUERY_TYPES), default="history_reason")
    parser.add_argument("--query-basis", choices=sorted(QUERY_BASES), default="global_goal")
    parser.add_argument("--tool-input-text", default="")
    parser.add_argument("--meta-path", default=str(DEFAULT_COMMON_ERROR_META))
    args = parser.parse_args()
    if args.operation == "record-opened-evidence":
        if not (args.evidence_ref_file or args.evidence_ref_json):
            parser.error("record-opened-evidence requires --evidence-ref-file or --evidence-ref-json")
        result = record_opened_evidence_to_workfile(
            Path(args.workfile),
            expected_host_task_key_sha256=args.host_task_key_sha256,
            event_id=args.event_id,
            evidence_ref=_load_evidence_ref(args),
        )
    else:
        if not (args.route_file or args.route_json) or args.prompt is None:
            parser.error("bind-memory-query requires a route and --prompt")
        result = bind_memory_query_to_workfile(
            Path(args.workfile),
            expected_host_task_key_sha256=args.host_task_key_sha256,
            route=_load_route(args),
            prompt=args.prompt,
            event_id=args.event_id,
            query_type=args.query_type,
            query_basis=args.query_basis,
            tool_input_text=args.tool_input_text,
            meta_path=Path(args.meta_path),
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
