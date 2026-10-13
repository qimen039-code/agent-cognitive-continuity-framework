#!/usr/bin/env python3
"""Build a lightweight conversation ledger from Codex Desktop JSONL sessions.

The tool is intentionally read-only with respect to raw sessions. It writes a
derived ledger directory containing indexes, bounded summaries, and evidence
references back to raw JSONL line ranges. Raw session logs remain canonical.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from evidence_locator import DEFAULT_REGISTRY as DEFAULT_SOURCE_LOCATOR_REGISTRY
from evidence_locator import resolve_evidence_source


EVIDENCE_EVENT_KINDS = {
    "user_message": "user_message",
    "task_complete": "task_complete",
    "context_compacted": "compaction",
    "compacted": "compaction",
    "token_count": "token_count",
    "patch_apply_end": "patch",
    "function_call": "tool_call",
    "custom_tool_call": "tool_call",
    "function_call_output": "tool_output",
    "custom_tool_call_output": "tool_output",
    "agent_message": "agent_message",
    "web_search_call": "web_search",
    "web_search_end": "web_search",
}

GENERATION_POINTER_SCHEMA = "cbh.ledger_generation_pointer.v1"
GENERATION_MANIFEST_SCHEMA = "cbh.ledger_generation_manifest.v1"
GENERATION_POINTER_FILE = "CURRENT.json"
GENERATION_MANIFEST_FILE = "generation.json"


class LedgerGenerationError(RuntimeError):
    def __init__(self, code: str, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


@dataclass
class TurnState:
    turn_id: str
    session_id: str
    line_start: int
    line_end: int
    time_start: str | None = None
    time_end: str | None = None
    event_counts: Counter[str] = field(default_factory=Counter)
    evidence_refs: list[str] = field(default_factory=list)
    domain_tags: set[str] = field(default_factory=set)
    summary_parts: list[str] = field(default_factory=list)
    compaction_boundary: bool = False
    token_pressure: dict[str, int | None] = field(
        default_factory=lambda: {"last_input_tokens": None, "model_context_window": None}
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def mtime_utc(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def raw_file_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path)}
    stat = path.stat()
    return {
        "exists": True,
        "path": str(path),
        "size_bytes": stat.st_size,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def sha16(raw_line: str) -> str:
    return hashlib.sha256(raw_line.encode("utf-8", errors="strict")).hexdigest()[:16]


def truncate(value: Any, limit: int = 420) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def payload_type(event: dict[str, Any]) -> str:
    payload = event.get("payload")
    if isinstance(payload, dict) and payload.get("type"):
        return str(payload["type"])
    return str(event.get("type") or "unknown")


def event_turn_id(event: dict[str, Any], current_turn_id: str | None) -> str | None:
    payload = event.get("payload")
    if isinstance(payload, dict) and payload.get("turn_id"):
        return str(payload["turn_id"])
    if isinstance(payload, dict):
        passthrough = payload.get("internal_chat_message_metadata_passthrough")
        if isinstance(passthrough, dict) and passthrough.get("turn_id"):
            return str(passthrough["turn_id"])
    return current_turn_id


def session_id_from_path(path: Path) -> str:
    match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", path.name)
    if match:
        return match.group(1)
    return path.stem


def json_line_records(path: Path) -> Iterable[tuple[int, str, dict[str, Any]]]:
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            raw = raw_line.rstrip("\n")
            if not raw.strip():
                continue
            try:
                yield line_no, raw, json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL: {exc}") from exc


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for line_no, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                records.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL: {exc}") from exc
    return records


def payload_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := payload_text(item)))
    if isinstance(value, dict):
        for key in ("text", "output", "message", "content"):
            if key in value:
                text = payload_text(value.get(key))
                if text:
                    return text
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def patch_artifact_refs(event: dict[str, Any]) -> list[dict[str, Any]]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    changes = payload.get("changes")
    if not isinstance(changes, dict):
        return []
    refs: list[dict[str, Any]] = []
    for raw_path, raw_change in changes.items():
        change = raw_change if isinstance(raw_change, dict) else {}
        refs.append(
            {
                "path": str(raw_path),
                "change_type": str(change.get("type") or "unknown"),
                "diff_present": bool(change.get("unified_diff")),
                "event_content_hash": None,
                "hash_boundary": "raw patch event contains the canonical diff; no event-time file hash was emitted",
            }
        )
    return refs


def ensure_turn(
    turns: dict[str, TurnState],
    session_id: str,
    turn_id: str,
    line_no: int,
    timestamp: str | None,
) -> TurnState:
    if turn_id not in turns:
        turns[turn_id] = TurnState(
            turn_id=turn_id,
            session_id=session_id,
            line_start=line_no,
            line_end=line_no,
            time_start=timestamp,
            time_end=timestamp,
        )
    turn = turns[turn_id]
    turn.line_end = max(turn.line_end, line_no)
    turn.time_end = timestamp or turn.time_end
    if not turn.time_start:
        turn.time_start = timestamp
    return turn


def evidence_summary(kind: str, event: dict[str, Any]) -> tuple[str, list[str]]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    tags: list[str] = []
    if kind == "user_message":
        return f"user: {truncate(payload.get('message'), 240)}", ["user_intent"]
    if kind == "task_complete":
        return f"task_complete: {truncate(payload.get('last_agent_message'), 320)}", ["task_complete"]
    if kind == "patch_apply_end":
        changes = payload.get("changes")
        change_count = len(changes) if isinstance(changes, dict) else 0
        success = payload.get("success")
        return f"patch_apply_end: success={success} changes={change_count}", ["code_change", "artifact"]
    if kind in {"context_compacted", "compacted"}:
        return "context compacted; raw evidence required for exact details", ["compaction_boundary"]
    if kind == "token_count":
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
        return (
            f"token_count: last_input_tokens={last.get('input_tokens')} context_window={info.get('model_context_window')}",
            ["token_pressure"],
        )
    if kind in {"web_search_call", "web_search_end"}:
        return f"web_search: {truncate(payload.get('query') or payload.get('action'), 240)}", ["external_research"]
    if kind in {"function_call", "custom_tool_call"}:
        name = payload.get("name") or payload.get("call_id") or "tool_call"
        call_id = payload.get("call_id") or payload.get("id") or "unknown"
        return f"tool_call: name={truncate(name, 120)} call_id={truncate(call_id, 160)}", ["tool_call", "execution_log"]
    if kind in {"function_call_output", "custom_tool_call_output"}:
        call_id = payload.get("call_id") or "unknown"
        output = payload_text(payload.get("output"))
        return f"tool_output: call_id={truncate(call_id, 160)} {truncate(output, 320)}", ["tool_output", "execution_log"]
    if kind == "agent_message":
        phase = payload.get("phase") or "unknown"
        return f"agent_message[{truncate(phase, 60)}]: {truncate(payload.get('message'), 320)}", ["agent_message", "conversation_detail"]
    return f"{kind}: raw event observed", []


def build_ledger(
    session_paths: list[Path],
    output_dir: Path,
    *,
    ledger_id: str | None = None,
    title: str = "Conversation ledger",
    project_lane: str = "PROJECTLESS",
    conversation_memory_id: str | None = None,
    conversation_memory_path: str | None = None,
    project_memory_id: str | None = None,
    continues_from_ledger_id: str | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    if not ledger_id:
        first = session_id_from_path(session_paths[0]) if session_paths else "empty"
        ledger_id = f"ledger:{first}"

    sessions: list[dict[str, Any]] = []
    all_turn_records: list[dict[str, Any]] = []
    all_segments: list[dict[str, Any]] = []
    all_capsules: list[dict[str, Any]] = []
    all_anchors: list[dict[str, Any]] = []
    all_evidence: list[dict[str, Any]] = []
    all_links: list[dict[str, Any]] = []
    domain_index: dict[str, list[str]] = defaultdict(list)
    total_raw_replacement_characters = 0
    total_raw_lines_with_replacement = 0

    for session_index, session_path in enumerate(session_paths, start=1):
        session_path = session_path.resolve()
        starting_raw_state = raw_file_state(session_path)
        session_id = session_id_from_path(session_path)
        host = "codex"
        session_meta: dict[str, Any] = {}
        event_counts: Counter[str] = Counter()
        turns: dict[str, TurnState] = {}
        current_turn_id: str | None = None
        started_at: str | None = None
        ended_at: str | None = None
        first_line: int | None = None
        last_line = 0
        raw_replacement_character_count = 0
        raw_lines_with_replacement_character = 0
        tool_call_refs_by_call_id: dict[str, dict[str, Any]] = {}

        for line_no, raw, event in json_line_records(session_path):
            first_line = first_line or line_no
            last_line = line_no
            timestamp = event.get("timestamp")
            started_at = started_at or timestamp
            ended_at = timestamp or ended_at
            kind = payload_type(event)
            event_counts[kind] += 1
            replacement_count = raw.count("\ufffd")
            if replacement_count:
                raw_replacement_character_count += replacement_count
                raw_lines_with_replacement_character += 1

            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event.get("type") == "session_meta" and isinstance(payload, dict):
                session_meta = payload
                session_id = str(payload.get("session_id") or payload.get("id") or session_id)
                host = str(payload.get("originator") or "codex")
                all_anchors.append(
                    {
                        "anchor_id": f"ANCHOR-{session_id}-session-start",
                        "ledger_id": ledger_id,
                        "session_id": session_id,
                        "turn_id": None,
                        "anchor_type": "session_start",
                        "timestamp": timestamp,
                        "summary": "session_meta observed",
                        "evidence_refs": [],
                        "source_tag": "conversation_ledger",
                        "belief_status": "raw_observed",
                        "confidence": "high",
                        "derived_from": [],
                        "score_method": "none",
                    }
                )

            if kind == "turn_context" and isinstance(payload, dict) and payload.get("turn_id"):
                current_turn_id = str(payload["turn_id"])
                turn = ensure_turn(turns, session_id, current_turn_id, line_no, timestamp)
                turn.domain_tags.add("turn_context")
                all_anchors.append(
                    {
                        "anchor_id": f"ANCHOR-{session_id}-{current_turn_id}-{line_no:06d}-turn-start",
                        "ledger_id": ledger_id,
                        "session_id": session_id,
                        "turn_id": current_turn_id,
                        "anchor_type": "turn_start",
                        "timestamp": timestamp,
                        "summary": "turn_context observed",
                        "evidence_refs": [],
                        "source_tag": "conversation_ledger",
                        "belief_status": "raw_observed",
                        "confidence": "high",
                        "derived_from": [],
                        "score_method": "none",
                    }
                )

            turn_id = event_turn_id(event, current_turn_id)
            if turn_id:
                turn = ensure_turn(turns, session_id, turn_id, line_no, timestamp)
                turn.event_counts[kind] += 1
            else:
                turn = None

            evidence_kind = EVIDENCE_EVENT_KINDS.get(kind)
            if evidence_kind:
                ref_id = f"EVID-{session_id}-{line_no:06d}"
                summary, tags = evidence_summary(kind, event)
                ref = {
                    "ref_id": ref_id,
                    "ledger_id": ledger_id,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "evidence_kind": evidence_kind,
                    "event_type": kind,
                    "summary": summary,
                    "source_tag": "raw_session",
                    "belief_status": "raw_observed",
                    "confidence": "high",
                    "derived_from": {
                        "source_type": "raw_session_jsonl",
                        "path": str(session_path),
                        "line_start": line_no,
                        "line_end": line_no,
                        "sha256_16": sha16(raw),
                    },
                    "source_monitoring": {
                        "raw_log_is_canonical": True,
                        "summary_is_navigation_only": True,
                    },
                    "lifecycle": "active",
                    "created_at": now,
                }
                call_id = str(payload.get("call_id") or payload.get("id") or "")
                if call_id:
                    ref["call_id"] = call_id
                    if evidence_kind == "tool_call":
                        ref["paired_output_ref_ids"] = []
                        tool_call_refs_by_call_id[call_id] = ref
                    elif evidence_kind == "tool_output":
                        call_ref = tool_call_refs_by_call_id.get(call_id)
                        if call_ref:
                            ref["paired_call_ref_id"] = call_ref["ref_id"]
                            call_ref["paired_output_ref_ids"].append(ref_id)
                if evidence_kind == "patch":
                    ref["artifact_refs"] = patch_artifact_refs(event)
                all_evidence.append(ref)
                if turn:
                    turn.evidence_refs.append(ref_id)
                    turn.domain_tags.update(tags)
                    if len(turn.summary_parts) < 6 and summary:
                        turn.summary_parts.append(summary)
                    if evidence_kind == "compaction":
                        turn.compaction_boundary = True
                    if kind == "token_count":
                        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                        last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
                        if isinstance(last.get("input_tokens"), int):
                            turn.token_pressure["last_input_tokens"] = last.get("input_tokens")
                        if isinstance(info.get("model_context_window"), int):
                            turn.token_pressure["model_context_window"] = info.get("model_context_window")

                if evidence_kind == "compaction":
                    all_anchors.append(
                        {
                            "anchor_id": f"ANCHOR-{session_id}-{line_no:06d}-compaction",
                            "ledger_id": ledger_id,
                            "session_id": session_id,
                            "turn_id": turn_id,
                            "anchor_type": "compaction_boundary",
                            "timestamp": timestamp,
                            "summary": "context compaction observed; exact details require raw evidence",
                            "evidence_refs": [ref_id],
                            "source_tag": "conversation_ledger",
                            "belief_status": "raw_observed",
                            "confidence": "high",
                            "derived_from": [ref_id],
                            "score_method": "none",
                        }
                    )
                elif evidence_kind in {"task_complete", "patch"}:
                    all_anchors.append(
                        {
                            "anchor_id": f"ANCHOR-{session_id}-{line_no:06d}-{evidence_kind}",
                            "ledger_id": ledger_id,
                            "session_id": session_id,
                            "turn_id": turn_id,
                            "anchor_type": evidence_kind,
                            "timestamp": timestamp,
                            "summary": summary,
                            "evidence_refs": [ref_id],
                            "source_tag": "conversation_ledger",
                            "belief_status": "bounded_claim",
                            "confidence": "medium",
                            "derived_from": [ref_id],
                            "score_method": "none",
                        }
                    )

        raw_source_id = f"codex-session:{session_id}"
        raw_ref = {
            "source_type": "raw_session_jsonl",
            "source_id": raw_source_id,
            "path": str(session_path),
            "line_start": first_line or 0,
            "line_end": last_line,
        }
        total_raw_replacement_characters += raw_replacement_character_count
        total_raw_lines_with_replacement += raw_lines_with_replacement_character
        sessions.append(
            {
                "record_id": f"SESSION-{session_id}",
                "status": "active",
                "ledger_id": ledger_id,
                "session_id": session_id,
                "raw_source_id": raw_source_id,
                "host": host,
                "raw_session_path": str(session_path),
                "project_lane": project_lane,
                "cwd": session_meta.get("cwd"),
                "model": session_meta.get("model_provider") or session_meta.get("model"),
                "client_version": session_meta.get("cli_version"),
                "started_at": started_at,
                "ended_at": ended_at,
                "raw_size_bytes": starting_raw_state.get("size_bytes"),
                "raw_mtime_utc": starting_raw_state.get("mtime_utc"),
                "raw_line_count": last_line,
                "raw_replacement_character_count": raw_replacement_character_count,
                "raw_lines_with_replacement_character": raw_lines_with_replacement_character,
                "utf8_decode_policy": "strict",
                "event_counts": dict(sorted(event_counts.items())),
                "source_tag": "raw_session_manifest",
                "belief_status": "raw_observed",
                "confidence": "high",
                "derived_from": raw_ref,
                "source_monitoring": {
                    "raw_log_is_canonical": True,
                    "summary_is_navigation_only": True,
                },
                "lifecycle": "active",
            }
        )
        all_links.append(
            {
                "link_id": f"LINK-{session_id}-raw-session-ledger",
                "status": "active",
                "ledger_id": ledger_id,
                "link_type": "raw_session_to_ledger",
                "from_id": str(session_path),
                "to_id": ledger_id,
                "created_at": now,
                "created_by": "codex_session_ledger.py",
                "write_policy": "link_only",
                "evidence_boundary": "raw_log_is_canonical_no_payload_copy",
                "evidence_refs": [],
            }
        )

        for ordinal, turn in enumerate(turns.values(), start=1):
            turn_record_id = f"TURN-{session_id}-{ordinal:04d}"
            all_turn_records.append(
                {
                    "record_id": turn_record_id,
                    "status": "active",
                    "ledger_id": ledger_id,
                    "session_id": session_id,
                    "turn_id": turn.turn_id,
                    "line_start": turn.line_start,
                    "line_end": turn.line_end,
                    "time_start": turn.time_start,
                    "time_end": turn.time_end,
                    "event_counts": dict(sorted(turn.event_counts.items())),
                    "token_pressure": turn.token_pressure,
                    "evidence_refs": turn.evidence_refs,
                    "source_tag": "conversation_ledger",
                    "belief_status": "bounded_claim",
                    "confidence": "medium",
                    "derived_from": turn.evidence_refs,
                    "score_method": "none",
                }
            )
            tags = sorted(turn.domain_tags or {"general"})
            segment_id = f"SEG-{session_id}-{ordinal:04d}"
            summary = " | ".join(turn.summary_parts) if turn.summary_parts else "turn observed; inspect evidence_refs for details"
            segment = {
                "segment_id": segment_id,
                "status": "active",
                "ledger_id": ledger_id,
                "session_id": session_id,
                "turn_id": turn.turn_id,
                "time_start": turn.time_start,
                "time_end": turn.time_end,
                "line_start": turn.line_start,
                "line_end": turn.line_end,
                "domain_tags": tags,
                "summary": truncate(summary, 1000),
                "compaction_boundary": turn.compaction_boundary,
                "evidence_refs": turn.evidence_refs,
                "source_tag": "conversation_ledger",
                "belief_status": "bounded_claim",
                "confidence": "medium",
                "derived_from": turn.evidence_refs,
                "score_method": "none",
            }
            all_segments.append(segment)
            for tag in tags:
                domain_index[tag].append(segment_id)

    if conversation_memory_id:
        all_links.append(
            {
                "link_id": f"LINK-{ledger_id}-conversation-memory",
                "status": "active",
                "ledger_id": ledger_id,
                "link_type": "ledger_to_conversation_memory",
                "from_id": ledger_id,
                "to_id": conversation_memory_id,
                "to_path": conversation_memory_path,
                "created_at": now,
                "created_by": "codex_session_ledger.py",
                "write_policy": "link_only",
                "evidence_boundary": "ledger_links_to_memory_no_payload_copy",
                "evidence_refs": [],
            }
        )
    if project_memory_id:
        all_links.append(
            {
                "link_id": f"LINK-{ledger_id}-project-memory",
                "status": "active",
                "ledger_id": ledger_id,
                "link_type": "ledger_to_project_memory",
                "from_id": ledger_id,
                "to_id": project_memory_id,
                "created_at": now,
                "created_by": "codex_session_ledger.py",
                "write_policy": "link_only",
                "evidence_boundary": "ledger_links_to_project_memory_no_payload_copy",
                "evidence_refs": [],
            }
        )
    if continues_from_ledger_id:
        all_links.append(
            {
                "link_id": f"LINK-{continues_from_ledger_id}-continues-{ledger_id}",
                "status": "active",
                "ledger_id": ledger_id,
                "link_type": "continuation",
                "from_id": continues_from_ledger_id,
                "to_id": ledger_id,
                "created_at": now,
                "created_by": "codex_session_ledger.py",
                "write_policy": "link_only",
                "evidence_boundary": "continuation_link_no_payload_copy",
                "evidence_refs": [],
            }
        )

    evidence_kind_by_ref = {str(record.get("ref_id")): str(record.get("evidence_kind")) for record in all_evidence}
    for segment in all_segments:
        evidence_refs = [str(ref_id) for ref_id in segment.get("evidence_refs", [])]
        event_kinds = sorted({evidence_kind_by_ref[ref_id] for ref_id in evidence_refs if ref_id in evidence_kind_by_ref})
        capsule_id = str(segment["segment_id"]).replace("SEG-", "CAP-", 1)
        all_capsules.append(
            {
                "capsule_id": capsule_id,
                "capsule_type": "event_domain_classification",
                "status": "active",
                "ledger_id": ledger_id,
                "session_id": segment.get("session_id"),
                "turn_id": segment.get("turn_id"),
                "time_start": segment.get("time_start"),
                "time_end": segment.get("time_end"),
                "domain_tags": segment.get("domain_tags", []),
                "event_kinds": event_kinds,
                "meta_summary": segment.get("summary"),
                "compaction_boundary": segment.get("compaction_boundary", False),
                "evidence_refs": evidence_refs,
                "source_tag": "conversation_ledger_capsule",
                "belief_status": "bounded_claim",
                "confidence": "medium",
                "derived_from": {
                    "segment_id": segment.get("segment_id"),
                    "evidence_refs": evidence_refs,
                },
                "source_monitoring": {
                    "raw_log_is_canonical": True,
                    "summary_is_navigation_only": True,
                    "classification_is_derivative": True,
                },
                "score_method": "deterministic_event_mapping",
                "lifecycle": "active",
                "created_at": now,
            }
        )

    capsule_domain_index: dict[str, list[str]] = defaultdict(list)
    for capsule in all_capsules:
        for tag in capsule.get("domain_tags", []):
            capsule_domain_index[str(tag)].append(str(capsule["capsule_id"]))

    write_jsonl(output_dir / "sessions.jsonl", sessions)
    write_jsonl(output_dir / "turns.jsonl", all_turn_records)
    write_jsonl(output_dir / "segments.jsonl", all_segments)
    write_jsonl(output_dir / "capsules.jsonl", all_capsules)
    write_jsonl(output_dir / "time_anchors.jsonl", all_anchors)
    write_jsonl(output_dir / "evidence_refs.jsonl", all_evidence)
    write_jsonl(output_dir / "links.jsonl", all_links)

    domain_payload = {
        "ledger_id": ledger_id,
        "status": "active",
        "updated_at": now,
        "domains": {key: sorted(value) for key, value in sorted(domain_index.items())},
        "capsule_domains": {key: sorted(value) for key, value in sorted(capsule_domain_index.items())},
        "retrieval_order": [
            "_LEDGER_INDEX.md",
            "domain_index.json, capsules.jsonl, or sessions.jsonl",
            "segments.jsonl",
            "evidence_refs.jsonl",
            "raw session snippet only when exact detail matters",
        ],
    }
    (output_dir / "domain_index.json").write_text(json.dumps(domain_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "_LEDGER_INDEX.md").write_text(
        ledger_index_text(
            ledger_id=ledger_id,
            title=title,
            project_lane=project_lane,
            now=now,
            session_count=len(sessions),
            turn_count=len(all_turn_records),
            segment_count=len(all_segments),
            capsule_count=len(all_capsules),
            evidence_count=len(all_evidence),
            conversation_memory_id=conversation_memory_id,
            project_memory_id=project_memory_id,
        ),
        encoding="utf-8",
    )

    return {
        "status": "warn" if total_raw_replacement_characters else "pass",
        "ledger_id": ledger_id,
        "output_dir": str(output_dir.resolve()),
        "sessions": len(sessions),
        "turns": len(all_turn_records),
        "segments": len(all_segments),
        "capsules": len(all_capsules),
        "evidence_refs": len(all_evidence),
        "links": len(all_links),
        "raw_replacement_character_count": total_raw_replacement_characters,
        "raw_lines_with_replacement_character": total_raw_lines_with_replacement,
    }


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n")


def status_from(checks: list[dict[str, Any]]) -> str:
    statuses = {str(check.get("status")) for check in checks}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "pass"


def add_check(checks: list[dict[str, Any]], check_id: str, status: str, summary: str, **evidence: Any) -> None:
    checks.append({"id": check_id, "status": status, "summary": summary, "evidence": evidence})


def doctor_ledger(
    ledger_dir: Path,
    *,
    locator_registry_path: Path | None = DEFAULT_SOURCE_LOCATOR_REGISTRY,
) -> dict[str, Any]:
    """Validate ledger structure without reading raw session payloads."""

    ledger_dir = load_active_generation(ledger_dir)
    checks: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    stale_sessions: list[dict[str, Any]] = []
    raw_replacement_sessions: list[dict[str, Any]] = []
    missing_replacement_metadata: list[str] = []
    required_jsonl = [
        "sessions.jsonl",
        "turns.jsonl",
        "segments.jsonl",
        "time_anchors.jsonl",
        "evidence_refs.jsonl",
        "links.jsonl",
    ]
    optional_jsonl = ["capsules.jsonl"]
    required_files = ["_LEDGER_INDEX.md", "domain_index.json", *required_jsonl]

    ledger_replacement_character_count = 0
    for name in required_files:
        path = ledger_dir / name
        add_check(
            checks,
            f"file.{name}",
            "pass" if path.exists() else "fail",
            "required ledger file exists" if path.exists() else "required ledger file is missing",
            path=str(path),
        )
        if path.exists():
            try:
                ledger_replacement_character_count += path.read_text(
                    encoding="utf-8", errors="strict"
                ).count("\ufffd")
            except UnicodeDecodeError as exc:
                add_check(
                    checks,
                    f"utf8.{name}",
                    "fail",
                    "ledger file is not strict UTF-8",
                    path=str(path),
                    error=str(exc),
                )

    add_check(
        checks,
        "ledger.replacement_character",
        "warn" if ledger_replacement_character_count else "pass",
        (
            "literal U+FFFD replacement characters exist in ledger navigation data"
            if ledger_replacement_character_count
            else "no literal U+FFFD replacement characters in required ledger files"
        ),
        count=ledger_replacement_character_count,
    )

    parsed: dict[str, list[dict[str, Any]]] = {}
    for name in [*required_jsonl, *optional_jsonl]:
        path = ledger_dir / name
        if not path.exists():
            if name in optional_jsonl:
                add_check(
                    checks,
                    f"optional.{name}",
                    "warn",
                    "optional capsule compatibility view is missing; rebuild to create it",
                    path=str(path),
                )
            continue
        try:
            records = read_jsonl(path)
            parsed[name] = records
            counts[name] = len(records)
            add_check(checks, f"jsonl.{name}", "pass", "JSONL parsed", records=len(records))
        except ValueError as exc:
            add_check(checks, f"jsonl.{name}", "fail", "JSONL parse failed", error=str(exc))

    domain_path = ledger_dir / "domain_index.json"
    if domain_path.exists():
        try:
            json.loads(domain_path.read_text(encoding="utf-8"))
            add_check(checks, "json.domain_index", "pass", "domain_index.json parsed")
        except json.JSONDecodeError as exc:
            add_check(checks, "json.domain_index", "fail", "domain_index.json parse failed", error=str(exc))

    for session in parsed.get("sessions.jsonl", []):
        recorded_raw_path = Path(str(session.get("raw_session_path") or ""))
        source_id = str(
            session.get("raw_source_id")
            or f"codex-session:{session.get('session_id')}"
        )
        locator = resolve_evidence_source(
            recorded_raw_path,
            source_id=source_id,
            registry_path=locator_registry_path,
            verify_registry_hash=False,
        )
        raw_path = Path(str(locator.get("resolved_path") or recorded_raw_path))
        raw_state = raw_file_state(raw_path)
        if not raw_state["exists"]:
            add_check(
                checks,
                "raw.exists",
                "fail",
                "raw session source cannot be resolved",
                path=str(recorded_raw_path),
                locator_status=locator.get("status"),
            )
            continue
        add_check(
            checks,
            "raw.exists",
            "pass",
            "raw session source resolves",
            path=str(raw_path),
            recorded_path=str(recorded_raw_path),
            locator_status=locator.get("status"),
            locator_kind=locator.get("locator_kind"),
        )
        replacement_count = session.get("raw_replacement_character_count")
        if replacement_count is None:
            missing_replacement_metadata.append(str(session.get("session_id") or "unknown"))
        elif int(replacement_count) > 0:
            raw_replacement_sessions.append(
                {
                    "session_id": session.get("session_id"),
                    "count": int(replacement_count),
                    "lines": int(session.get("raw_lines_with_replacement_character") or 0),
                }
            )
        recorded_size = session.get("raw_size_bytes")
        recorded_mtime = session.get("raw_mtime_utc")
        size_changed = recorded_size is not None and recorded_size != raw_state.get("size_bytes")
        mtime_changed = recorded_mtime is not None and recorded_mtime != raw_state.get("mtime_utc")
        if recorded_size is None or recorded_mtime is None:
            add_check(
                checks,
                "raw.staleness",
                "warn",
                "raw staleness metadata is missing; rebuild once to enable stat-only stale checks",
                path=str(raw_path),
            )
            stale_sessions.append({"session_id": session.get("session_id"), "reason": "missing_stat_metadata"})
        elif size_changed or mtime_changed:
            add_check(
                checks,
                "raw.staleness",
                "warn",
                "raw session stat differs from ledger record",
                path=str(raw_path),
                recorded_size_bytes=recorded_size,
                current_size_bytes=raw_state.get("size_bytes"),
                recorded_mtime_utc=recorded_mtime,
                current_mtime_utc=raw_state.get("mtime_utc"),
            )
            stale_sessions.append({"session_id": session.get("session_id"), "reason": "raw_stat_changed"})
        else:
            add_check(checks, "raw.staleness", "pass", "raw session stat matches ledger record", path=str(raw_path))

    if raw_replacement_sessions:
        add_check(
            checks,
            "raw.replacement_character",
            "warn",
            "raw session contains literal U+FFFD; ledger preserves and exposes the source corruption boundary",
            sessions=raw_replacement_sessions,
        )
    elif missing_replacement_metadata:
        add_check(
            checks,
            "raw.replacement_character",
            "warn",
            "raw replacement-character metadata is missing; rebuild once with the strict ledger builder",
            session_ids=missing_replacement_metadata,
        )
    else:
        add_check(
            checks,
            "raw.replacement_character",
            "pass",
            "raw session replacement-character count is zero",
        )

    links = parsed.get("links.jsonl", [])
    link_types = {str(record.get("link_type")) for record in links}
    add_check(
        checks,
        "links.raw_session_to_ledger",
        "pass" if "raw_session_to_ledger" in link_types else "fail",
        "raw session link exists" if "raw_session_to_ledger" in link_types else "raw session link is missing",
    )
    add_check(
        checks,
        "links.memory",
        "pass" if {"ledger_to_conversation_memory", "ledger_to_project_memory"} & link_types else "warn",
        "memory link exists" if {"ledger_to_conversation_memory", "ledger_to_project_memory"} & link_types else "no project/conversation memory link recorded",
    )

    anchors = parsed.get("time_anchors.jsonl", [])
    anchor_ids = [str(record.get("anchor_id") or "") for record in anchors]
    duplicate_anchor_ids = sorted(
        key for key, count in Counter(anchor_ids).items() if key and count > 1
    )
    add_check(
        checks,
        "anchors.unique_ids",
        "fail" if duplicate_anchor_ids else "pass",
        "duplicate anchor ids found" if duplicate_anchor_ids else "anchor ids are unique",
        duplicate_ids=duplicate_anchor_ids,
    )

    evidence_records = parsed.get("evidence_refs.jsonl", [])
    evidence_ids = [str(record.get("ref_id") or "") for record in evidence_records]
    duplicate_evidence_ids = sorted(
        key for key, count in Counter(evidence_ids).items() if key and count > 1
    )
    add_check(
        checks,
        "evidence.unique_ids",
        "fail" if duplicate_evidence_ids else "pass",
        "duplicate evidence ref ids found" if duplicate_evidence_ids else "evidence ref ids are unique",
        duplicate_ids=duplicate_evidence_ids,
    )

    evidence_id_set = set(evidence_ids)
    missing_evidence_refs: list[str] = []
    for file_name in ("turns.jsonl", "segments.jsonl", "time_anchors.jsonl", "capsules.jsonl"):
        for record in parsed.get(file_name, []):
            for ref_id in record.get("evidence_refs", []):
                if str(ref_id) not in evidence_id_set:
                    missing_evidence_refs.append(str(ref_id))
    missing_evidence_refs = sorted(set(missing_evidence_refs))
    add_check(
        checks,
        "evidence.navigation_links",
        "fail" if missing_evidence_refs else "pass",
        "ledger points to missing evidence refs" if missing_evidence_refs else "ledger evidence navigation links resolve",
        missing_ref_ids=missing_evidence_refs,
    )

    dangling_tool_pairs: list[str] = []
    unpaired_tool_outputs: list[str] = []
    nonreciprocal_tool_pairs: list[str] = []
    evidence_by_id = {
        str(record.get("ref_id") or ""): record for record in evidence_records
    }
    for record in evidence_records:
        ref_id = str(record.get("ref_id") or "")
        paired_call = str(record.get("paired_call_ref_id") or "")
        if record.get("evidence_kind") == "tool_output" and not paired_call:
            unpaired_tool_outputs.append(ref_id)
        elif paired_call:
            call_record = evidence_by_id.get(paired_call)
            if call_record is None:
                dangling_tool_pairs.append(paired_call)
            elif (
                call_record.get("evidence_kind") != "tool_call"
                or str(call_record.get("call_id") or "")
                != str(record.get("call_id") or "")
                or ref_id not in [
                    str(item) for item in call_record.get("paired_output_ref_ids", [])
                ]
            ):
                nonreciprocal_tool_pairs.append(ref_id)
        for output_ref in record.get("paired_output_ref_ids", []):
            output_id = str(output_ref)
            output_record = evidence_by_id.get(output_id)
            if output_record is None:
                dangling_tool_pairs.append(output_id)
            elif (
                output_record.get("evidence_kind") != "tool_output"
                or str(output_record.get("paired_call_ref_id") or "") != ref_id
                or str(output_record.get("call_id") or "")
                != str(record.get("call_id") or "")
            ):
                nonreciprocal_tool_pairs.append(output_id)
    add_check(
        checks,
        "evidence.tool_call_output_pairs",
        (
            "fail"
            if dangling_tool_pairs or nonreciprocal_tool_pairs
            else "warn"
            if unpaired_tool_outputs
            else "pass"
        ),
        (
            "tool call/output pair points to missing evidence"
            if dangling_tool_pairs
            else "tool call/output pair is not reciprocal"
            if nonreciprocal_tool_pairs
            else "some tool outputs have no preceding call ref"
            if unpaired_tool_outputs
            else "tool call/output evidence pairs resolve"
        ),
        dangling_ref_ids=sorted(set(dangling_tool_pairs)),
        nonreciprocal_ref_ids=sorted(set(nonreciprocal_tool_pairs)),
        unpaired_output_ref_ids=unpaired_tool_outputs,
    )

    compaction_count = sum(1 for record in anchors if record.get("anchor_type") == "compaction_boundary")
    counts["compaction_anchors"] = compaction_count
    add_check(
        checks,
        "anchors.compaction",
        "pass" if compaction_count else "warn",
        "compaction anchors recorded" if compaction_count else "no compaction anchors recorded",
        count=compaction_count,
    )

    result = {
        "status": status_from(checks),
        "mode": "doctor",
        "ledger_dir": str(ledger_dir),
        "stale": bool(stale_sessions),
        "stale_sessions": stale_sessions,
        "counts": counts,
        "checks": checks,
        "cost_boundary": "does not read raw session payloads; uses ledger JSON/JSONL and raw file stat only",
    }
    return result


def find_evidence_ref(ledger_dir: Path, ref_id: str) -> dict[str, Any] | None:
    ledger_dir = load_active_generation(ledger_dir)
    evidence_path = ledger_dir / "evidence_refs.jsonl"
    if not evidence_path.exists():
        return None
    with evidence_path.open("r", encoding="utf-8", errors="strict") as handle:
        for line_no, raw in enumerate(handle, start=1):
            if ref_id not in raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{evidence_path}:{line_no}: invalid JSONL: {exc}") from exc
            if record.get("ref_id") == ref_id:
                return record
    return None


def read_raw_window(path: Path, line_start: int, line_end: int, *, context_lines: int, max_chars_per_line: int) -> list[dict[str, Any]]:
    start = max(1, line_start - context_lines)
    end = max(line_end, line_end + context_lines)
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for line_no, raw in enumerate(handle, start=1):
            if line_no < start:
                continue
            if line_no > end:
                break
            full_text = raw.rstrip("\n")
            text = full_text
            truncated = len(text) > max_chars_per_line
            if truncated:
                text = text[: max_chars_per_line - 3] + "..."
            rows.append({"line": line_no, "text": text, "sha256_16": sha16(full_text), "truncated": truncated})
    return rows


def resolve_evidence(
    ledger_dir: Path,
    ref_id: str,
    *,
    context_lines: int = 20,
    max_chars_per_line: int = 800,
    locator_registry_path: Path | None = DEFAULT_SOURCE_LOCATOR_REGISTRY,
) -> dict[str, Any]:
    ledger_dir = load_active_generation(ledger_dir)
    evidence = find_evidence_ref(ledger_dir, ref_id)
    if evidence is None:
        return {"status": "fail", "mode": "resolve", "ref_id": ref_id, "error": "evidence ref not found"}
    derived = evidence.get("derived_from") if isinstance(evidence.get("derived_from"), dict) else {}
    recorded_raw_path = Path(str(derived.get("path") or ""))
    locator = resolve_evidence_source(
        recorded_raw_path,
        source_id=str(derived.get("source_id") or ""),
        expected_line=int(derived.get("line_start") or 1),
        expected_line_sha256_16=str(derived.get("sha256_16") or ""),
        registry_path=locator_registry_path,
    )
    if not locator.get("resolved_path"):
        return {
            "status": "fail",
            "mode": "resolve",
            "ref_id": ref_id,
            "error": "raw source not found or failed verification",
            "path": str(recorded_raw_path),
            "locator_status": locator.get("status"),
        }
    raw_path = Path(str(locator["resolved_path"]))
    line_start = int(derived.get("line_start") or 1)
    line_end = int(derived.get("line_end") or line_start)
    window = read_raw_window(
        raw_path,
        line_start,
        line_end,
        context_lines=max(0, context_lines),
        max_chars_per_line=max(80, max_chars_per_line),
    )
    target_row = next((row for row in window if row["line"] == line_start), None)
    expected_hash = derived.get("sha256_16")
    hash_match = target_row.get("sha256_16") == expected_hash if expected_hash and target_row else None
    return {
        "status": "pass" if hash_match is not False else "warn",
        "mode": "resolve",
        "ref_id": ref_id,
        "evidence": evidence,
        "raw_window": window,
        "hash_match": hash_match,
        "locator_status": locator.get("status"),
        "locator_kind": locator.get("locator_kind"),
        "resolved_path": str(raw_path),
        "cost_boundary": "reads only the selected raw line window, not the full session into memory",
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        errors="strict",
        newline="\n",
    )
    os.replace(temporary, path)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        text,
        encoding="utf-8",
        errors="strict",
        newline="\n",
    )
    os.replace(temporary, path)


def _generation_root_index_text() -> str:
    return """# Conversation Ledger Generation Root

This stable directory is a navigation root, not a ledger snapshot.

## Active generation

```text
read CURRENT.json
-> resolve its relative_path beneath this directory
-> read that generation's _LEDGER_INDEX.md
-> select bounded records
-> inspect raw session evidence only when exact detail matters
```

If `CURRENT.json` is absent, the remaining flat files are a legacy compatibility
snapshot. Raw session JSONL remains canonical evidence.
"""


def _generation_id(prefix_sha256: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"generation-{stamp}-{prefix_sha256[:12]}-{uuid.uuid4().hex[:8]}"


def _safe_generation_path(root: Path, relative_path: str) -> Path:
    root = root.resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("ledger_generation_pointer_outside_root") from exc
    return candidate


def load_active_generation(root: Path) -> Path:
    root = root.resolve()
    pointer_path = root / GENERATION_POINTER_FILE
    if not pointer_path.is_file():
        if (root / "_LEDGER_INDEX.md").is_file():
            return root
        raise FileNotFoundError(f"ledger generation pointer not found: {pointer_path}")
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8-sig", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("ledger_generation_pointer_unreadable") from exc
    if pointer.get("schema") != GENERATION_POINTER_SCHEMA:
        raise ValueError("ledger_generation_pointer_schema_invalid")
    relative_path = str(pointer.get("relative_path") or "")
    if not relative_path:
        raise ValueError("ledger_generation_pointer_path_missing")
    generation = _safe_generation_path(root, relative_path)
    if not generation.is_dir():
        raise FileNotFoundError(f"active ledger generation not found: {generation}")
    return generation


def _read_complete_prefix(path: Path, line_count: int) -> dict[str, Any]:
    hasher = hashlib.sha256()
    offset = 0
    last_line_sha256_16 = ""
    completed = 0
    with path.open("rb") as handle:
        while completed < line_count:
            raw_bytes = handle.readline()
            if not raw_bytes:
                raise LedgerGenerationError(
                    "raw_prefix_shorter_than_checkpoint",
                    expected_lines=line_count,
                    observed_lines=completed,
                )
            if not raw_bytes.endswith(b"\n"):
                raise LedgerGenerationError(
                    "raw_prefix_ends_with_incomplete_line",
                    line=completed + 1,
                )
            raw_text = raw_bytes[:-1].decode("utf-8", errors="strict")
            hasher.update(raw_bytes)
            offset += len(raw_bytes)
            completed += 1
            last_line_sha256_16 = sha16(raw_text)
    return {
        "checkpoint_offset_bytes": offset,
        "checkpoint_line_count": completed,
        "prefix_sha256": hasher.hexdigest(),
        "last_line_sha256_16": last_line_sha256_16,
    }


def _pending_tool_calls(evidence_records: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(record.get("call_id")): str(record.get("ref_id"))
        for record in evidence_records
        if record.get("evidence_kind") == "tool_call"
        and record.get("call_id")
        and not record.get("paired_output_ref_ids")
    }


def _generation_manifest(
    generation_dir: Path,
    session_paths: list[Path],
    *,
    parent_generation_id: str | None,
    build_kwargs: dict[str, Any],
) -> dict[str, Any]:
    sessions = read_jsonl(generation_dir / "sessions.jsonl")
    evidence = read_jsonl(generation_dir / "evidence_refs.jsonl")
    paths_by_id = {
        session_id_from_path(path.resolve()): path.resolve() for path in session_paths
    }
    checkpoints: list[dict[str, Any]] = []
    for session in sessions:
        session_id = str(session.get("session_id") or "")
        raw_path = Path(str(session.get("raw_session_path") or "")).resolve()
        if session_id in paths_by_id:
            raw_path = paths_by_id[session_id]
        line_count = int(session.get("raw_line_count") or 0)
        checkpoint = _read_complete_prefix(raw_path, line_count)
        session["raw_session_path"] = str(raw_path)
        session["raw_size_bytes"] = checkpoint["checkpoint_offset_bytes"]
        session["raw_mtime_utc"] = mtime_utc(raw_path)
        session["derived_from"]["path"] = str(raw_path)
        session["derived_from"]["line_end"] = line_count
        checkpoints.append(
            {
                "session_id": session_id,
                "raw_source_id": str(session.get("raw_source_id") or f"codex-session:{session_id}"),
                "raw_session_path": str(raw_path),
                **checkpoint,
            }
        )
    write_jsonl(generation_dir / "sessions.jsonl", sessions)
    pending = _pending_tool_calls(evidence)
    return {
        "schema": GENERATION_MANIFEST_SCHEMA,
        "generation_id": generation_dir.name,
        "parent_generation_id": parent_generation_id,
        "created_at": utc_now(),
        "ledger_id": str(sessions[0].get("ledger_id") or "") if sessions else "",
        "build_options": {
            "ledger_id": build_kwargs.get("ledger_id"),
            "title": build_kwargs.get("title", "Conversation ledger"),
            "project_lane": build_kwargs.get("project_lane", "PROJECTLESS"),
            "conversation_memory_id": build_kwargs.get("conversation_memory_id"),
            "conversation_memory_path": build_kwargs.get("conversation_memory_path"),
            "project_memory_id": build_kwargs.get("project_memory_id"),
            "continues_from_ledger_id": build_kwargs.get("continues_from_ledger_id"),
        },
        "sessions": checkpoints,
        "pending_tool_calls": pending,
    }


def _publish_generation(root: Path, staging: Path, manifest: dict[str, Any]) -> Path:
    root = root.resolve()
    generations = root / "generations"
    generations.mkdir(parents=True, exist_ok=True)
    generation_id = _generation_id(
        str(manifest.get("sessions", [{}])[0].get("prefix_sha256") or uuid.uuid4().hex)
    )
    final_dir = generations / generation_id
    if final_dir.exists():
        raise FileExistsError(f"generation already exists: {final_dir}")
    manifest["generation_id"] = generation_id
    (staging / GENERATION_MANIFEST_FILE).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        errors="strict",
        newline="\n",
    )
    staging.replace(final_dir)
    pointer = {
        "schema": GENERATION_POINTER_SCHEMA,
        "generation_id": generation_id,
        "relative_path": final_dir.relative_to(root).as_posix(),
        "published_at": utc_now(),
    }
    _write_text_atomic(root / "_LEDGER_INDEX.md", _generation_root_index_text())
    _write_json_atomic(root / GENERATION_POINTER_FILE, pointer)
    return final_dir


def initialize_generation_root(
    session_paths: list[Path],
    root: Path,
    **build_kwargs: Any,
) -> dict[str, Any]:
    root = root.resolve()
    if not session_paths:
        return {"status": "fail", "mode": "generation_init", "reason": "session_paths_required"}
    if (root / GENERATION_POINTER_FILE).exists():
        return {"status": "fail", "mode": "generation_init", "reason": "generation_root_already_initialized"}
    generations = root / "generations"
    generations.mkdir(parents=True, exist_ok=True)
    staging = generations / f".staging-{uuid.uuid4().hex}"
    try:
        result = build_ledger(session_paths, staging, **build_kwargs)
        manifest = _generation_manifest(
            staging,
            session_paths,
            parent_generation_id=None,
            build_kwargs=dict(build_kwargs),
        )
        doctor = doctor_ledger(staging)
        if doctor["status"] == "fail":
            raise LedgerGenerationError("staging_doctor_failed", doctor=doctor)
        final_dir = _publish_generation(root, staging, manifest)
        return {
            **result,
            "mode": "generation_init",
            "published": True,
            "generation_id": final_dir.name,
            "generation_dir": str(final_dir),
            "output_dir": str(final_dir),
            "doctor_status": doctor["status"],
        }
    except LedgerGenerationError as exc:
        if staging.exists():
            shutil.rmtree(staging)
        return {
            "status": "fail",
            "mode": "generation_init",
            "reason": exc.code,
            **exc.details,
        }
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _collect_verified_tail(
    raw_path: Path,
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    offset = int(checkpoint.get("checkpoint_offset_bytes") or 0)
    line_count = int(checkpoint.get("checkpoint_line_count") or 0)
    expected_prefix = str(checkpoint.get("prefix_sha256") or "")
    hasher = hashlib.sha256()
    tail: list[tuple[int, str, dict[str, Any], bytes]] = []
    with raw_path.open("rb") as handle:
        remaining = offset
        while remaining:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                raise LedgerGenerationError(
                    "raw_prefix_shorter_than_checkpoint",
                    checkpoint_offset_bytes=offset,
                )
            hasher.update(chunk)
            remaining -= len(chunk)
        actual_prefix = hasher.hexdigest()
        if actual_prefix != expected_prefix:
            raise LedgerGenerationError(
                "raw_prefix_identity_mismatch",
                expected_prefix_sha256=expected_prefix,
                actual_prefix_sha256=actual_prefix,
            )
        next_line = line_count
        checkpoint_offset = offset
        last_line_sha256_16 = str(checkpoint.get("last_line_sha256_16") or "")
        while True:
            raw_bytes = handle.readline()
            if not raw_bytes:
                break
            if not raw_bytes.endswith(b"\n"):
                break
            raw_text = raw_bytes[:-1].decode("utf-8", errors="strict")
            hasher.update(raw_bytes)
            checkpoint_offset += len(raw_bytes)
            next_line += 1
            last_line_sha256_16 = sha16(raw_text)
            if not raw_text.strip():
                continue
            try:
                event = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                raise LedgerGenerationError(
                    "invalid_tail_jsonl",
                    line=next_line,
                    error=str(exc),
                ) from exc
            tail.append((next_line, raw_text, event, raw_bytes))
    return {
        "tail": tail,
        "checkpoint_offset_bytes": checkpoint_offset,
        "checkpoint_line_count": next_line,
        "prefix_sha256": hasher.hexdigest(),
        "last_line_sha256_16": last_line_sha256_16,
        "physical_lines_read": next_line - line_count,
    }


def _append_tail_to_generation(
    generation_dir: Path,
    *,
    session_path: Path,
    checkpoint: dict[str, Any],
    tail_result: dict[str, Any],
    now: str,
) -> None:
    sessions = read_jsonl(generation_dir / "sessions.jsonl")
    turns = read_jsonl(generation_dir / "turns.jsonl")
    segments = read_jsonl(generation_dir / "segments.jsonl")
    capsules = read_jsonl(generation_dir / "capsules.jsonl")
    anchors = read_jsonl(generation_dir / "time_anchors.jsonl")
    evidence = read_jsonl(generation_dir / "evidence_refs.jsonl")
    links = read_jsonl(generation_dir / "links.jsonl")
    session_id = str(checkpoint.get("session_id") or session_id_from_path(session_path))
    session = next((item for item in sessions if str(item.get("session_id")) == session_id), None)
    if session is None:
        raise LedgerGenerationError("session_checkpoint_not_found", session_id=session_id)

    turn_by_id = {
        str(item.get("turn_id")): item
        for item in turns
        if str(item.get("session_id")) == session_id and item.get("turn_id")
    }
    segment_by_turn = {
        str(item.get("turn_id")): item
        for item in segments
        if str(item.get("session_id")) == session_id and item.get("turn_id")
    }
    capsule_by_turn = {
        str(item.get("turn_id")): item
        for item in capsules
        if str(item.get("session_id")) == session_id and item.get("turn_id")
    }
    evidence_by_id = {str(item.get("ref_id")): item for item in evidence}
    call_by_id = {
        str(item.get("call_id")): item
        for item in evidence
        if item.get("evidence_kind") == "tool_call" and item.get("call_id")
    }
    current_turn_id = None
    session_turns = [item for item in turns if str(item.get("session_id")) == session_id]
    if session_turns:
        current_turn_id = str(max(session_turns, key=lambda item: int(item.get("line_end") or 0)).get("turn_id") or "") or None
    next_ordinal = len(session_turns) + 1
    touched_turns: set[str] = set()
    tags_by_turn: dict[str, set[str]] = {
        turn_id: set(item.get("domain_tags") or []) for turn_id, item in segment_by_turn.items()
    }
    session_event_counts = Counter(session.get("event_counts") or {})
    replacement_count = int(session.get("raw_replacement_character_count") or 0)
    replacement_lines = int(session.get("raw_lines_with_replacement_character") or 0)
    last_timestamp = session.get("ended_at")

    for line_no, raw, event, _raw_bytes in tail_result["tail"]:
        timestamp = event.get("timestamp")
        last_timestamp = timestamp or last_timestamp
        kind = payload_type(event)
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        session_event_counts[kind] += 1
        count = raw.count("\ufffd")
        if count:
            replacement_count += count
            replacement_lines += 1
        if kind == "turn_context" and payload.get("turn_id"):
            current_turn_id = str(payload["turn_id"])
            anchors.append(
                {
                    "anchor_id": f"ANCHOR-{session_id}-{current_turn_id}-{line_no:06d}-turn-start",
                    "ledger_id": session.get("ledger_id"),
                    "session_id": session_id,
                    "turn_id": current_turn_id,
                    "anchor_type": "turn_start",
                    "timestamp": timestamp,
                    "summary": "turn_context observed",
                    "evidence_refs": [],
                    "source_tag": "conversation_ledger",
                    "belief_status": "raw_observed",
                    "confidence": "high",
                    "derived_from": [],
                    "score_method": "none",
                }
            )
        turn_id = event_turn_id(event, current_turn_id)
        turn = None
        if turn_id:
            turn = turn_by_id.get(turn_id)
            if turn is None:
                turn = {
                    "record_id": f"TURN-{session_id}-{next_ordinal:04d}",
                    "status": "active",
                    "ledger_id": session.get("ledger_id"),
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "line_start": line_no,
                    "line_end": line_no,
                    "time_start": timestamp,
                    "time_end": timestamp,
                    "event_counts": {},
                    "token_pressure": {"last_input_tokens": None, "model_context_window": None},
                    "evidence_refs": [],
                    "source_tag": "conversation_ledger",
                    "belief_status": "bounded_claim",
                    "confidence": "medium",
                    "derived_from": [],
                    "score_method": "none",
                }
                next_ordinal += 1
                turns.append(turn)
                turn_by_id[turn_id] = turn
            turn["line_end"] = max(int(turn.get("line_end") or line_no), line_no)
            turn["time_end"] = timestamp or turn.get("time_end")
            counts = Counter(turn.get("event_counts") or {})
            counts[kind] += 1
            turn["event_counts"] = dict(sorted(counts.items()))
            touched_turns.add(turn_id)

        evidence_kind = EVIDENCE_EVENT_KINDS.get(kind)
        if not evidence_kind:
            continue
        ref_id = f"EVID-{session_id}-{line_no:06d}"
        if ref_id in evidence_by_id:
            raise LedgerGenerationError("duplicate_tail_evidence_id", ref_id=ref_id)
        summary, tags = evidence_summary(kind, event)
        ref: dict[str, Any] = {
            "ref_id": ref_id,
            "ledger_id": session.get("ledger_id"),
            "session_id": session_id,
            "turn_id": turn_id,
            "evidence_kind": evidence_kind,
            "event_type": kind,
            "summary": summary,
            "source_tag": "raw_session",
            "belief_status": "raw_observed",
            "confidence": "high",
            "derived_from": {
                "source_type": "raw_session_jsonl",
                "path": str(session_path.resolve()),
                "line_start": line_no,
                "line_end": line_no,
                "sha256_16": sha16(raw),
            },
            "source_monitoring": {
                "raw_log_is_canonical": True,
                "summary_is_navigation_only": True,
            },
            "lifecycle": "active",
            "created_at": now,
        }
        call_id = str(payload.get("call_id") or payload.get("id") or "")
        if call_id:
            ref["call_id"] = call_id
            if evidence_kind == "tool_call":
                if call_id in call_by_id:
                    raise LedgerGenerationError("duplicate_tool_call_id", call_id=call_id)
                ref["paired_output_ref_ids"] = []
                call_by_id[call_id] = ref
            elif evidence_kind == "tool_output":
                call_ref = call_by_id.get(call_id)
                if call_ref is None:
                    raise LedgerGenerationError(
                        "orphan_tool_output",
                        call_id=call_id,
                        line=line_no,
                    )
                ref["paired_call_ref_id"] = call_ref["ref_id"]
                outputs = list(call_ref.get("paired_output_ref_ids") or [])
                outputs.append(ref_id)
                call_ref["paired_output_ref_ids"] = outputs
        if evidence_kind == "patch":
            ref["artifact_refs"] = patch_artifact_refs(event)
        evidence.append(ref)
        evidence_by_id[ref_id] = ref
        if turn is not None:
            refs = list(turn.get("evidence_refs") or [])
            refs.append(ref_id)
            turn["evidence_refs"] = refs
            turn["derived_from"] = refs
            tags_by_turn.setdefault(turn_id, set()).update(tags)
            if evidence_kind == "compaction":
                tags_by_turn[turn_id].add("compaction_boundary")
            if kind == "token_count":
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
                pressure = dict(turn.get("token_pressure") or {})
                if isinstance(last.get("input_tokens"), int):
                    pressure["last_input_tokens"] = last["input_tokens"]
                if isinstance(info.get("model_context_window"), int):
                    pressure["model_context_window"] = info["model_context_window"]
                turn["token_pressure"] = pressure
        if evidence_kind == "compaction":
            anchors.append(
                {
                    "anchor_id": f"ANCHOR-{session_id}-{line_no:06d}-compaction",
                    "ledger_id": session.get("ledger_id"),
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "anchor_type": "compaction_boundary",
                    "timestamp": timestamp,
                    "summary": "context compaction observed; exact details require raw evidence",
                    "evidence_refs": [ref_id],
                    "source_tag": "conversation_ledger",
                    "belief_status": "raw_observed",
                    "confidence": "high",
                    "derived_from": [ref_id],
                    "score_method": "none",
                }
            )
        elif evidence_kind in {"task_complete", "patch"}:
            anchors.append(
                {
                    "anchor_id": f"ANCHOR-{session_id}-{line_no:06d}-{evidence_kind}",
                    "ledger_id": session.get("ledger_id"),
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "anchor_type": evidence_kind,
                    "timestamp": timestamp,
                    "summary": summary,
                    "evidence_refs": [ref_id],
                    "source_tag": "conversation_ledger",
                    "belief_status": "bounded_claim",
                    "confidence": "medium",
                    "derived_from": [ref_id],
                    "score_method": "none",
                }
            )

    for turn_id in touched_turns:
        turn = turn_by_id[turn_id]
        segment = segment_by_turn.get(turn_id)
        if segment is None:
            ordinal = str(turn["record_id"]).rsplit("-", 1)[-1]
            segment = {
                "segment_id": f"SEG-{session_id}-{ordinal}",
                "status": "active",
                "ledger_id": session.get("ledger_id"),
                "session_id": session_id,
                "turn_id": turn_id,
                "source_tag": "conversation_ledger",
                "belief_status": "bounded_claim",
                "confidence": "medium",
                "score_method": "none",
            }
            segments.append(segment)
            segment_by_turn[turn_id] = segment
        refs = [str(item) for item in turn.get("evidence_refs") or []]
        summaries = [
            str(evidence_by_id[ref_id].get("summary") or "")
            for ref_id in refs
            if ref_id in evidence_by_id
        ][:6]
        tags = sorted(tags_by_turn.get(turn_id) or {"general"})
        segment.update(
            {
                "time_start": turn.get("time_start"),
                "time_end": turn.get("time_end"),
                "line_start": turn.get("line_start"),
                "line_end": turn.get("line_end"),
                "domain_tags": tags,
                "summary": truncate(" | ".join(summaries) or "turn observed; inspect evidence_refs for details", 1000),
                "compaction_boundary": "compaction_boundary" in tags,
                "evidence_refs": refs,
                "derived_from": refs,
            }
        )
        capsule = capsule_by_turn.get(turn_id)
        if capsule is None:
            capsule = {
                "capsule_id": str(segment["segment_id"]).replace("SEG-", "CAP-", 1),
                "capsule_type": "event_domain_classification",
                "status": "active",
                "ledger_id": session.get("ledger_id"),
                "session_id": session_id,
                "turn_id": turn_id,
                "source_tag": "conversation_ledger_capsule",
                "belief_status": "bounded_claim",
                "confidence": "medium",
                "score_method": "deterministic_event_mapping",
                "lifecycle": "active",
                "created_at": now,
            }
            capsules.append(capsule)
            capsule_by_turn[turn_id] = capsule
        event_kinds = sorted(
            {
                str(evidence_by_id[ref_id].get("evidence_kind"))
                for ref_id in refs
                if ref_id in evidence_by_id
            }
        )
        capsule.update(
            {
                "time_start": turn.get("time_start"),
                "time_end": turn.get("time_end"),
                "domain_tags": tags,
                "event_kinds": event_kinds,
                "meta_summary": segment.get("summary"),
                "compaction_boundary": segment.get("compaction_boundary", False),
                "evidence_refs": refs,
                "derived_from": {
                    "segment_id": segment.get("segment_id"),
                    "evidence_refs": refs,
                },
                "source_monitoring": {
                    "raw_log_is_canonical": True,
                    "summary_is_navigation_only": True,
                    "classification_is_derivative": True,
                },
            }
        )

    session["ended_at"] = last_timestamp
    session["raw_session_path"] = str(session_path.resolve())
    session["raw_size_bytes"] = int(tail_result["checkpoint_offset_bytes"])
    session["raw_mtime_utc"] = mtime_utc(session_path)
    session["raw_line_count"] = int(tail_result["checkpoint_line_count"])
    session["raw_replacement_character_count"] = replacement_count
    session["raw_lines_with_replacement_character"] = replacement_lines
    session["event_counts"] = dict(sorted(session_event_counts.items()))
    session["derived_from"]["path"] = str(session_path.resolve())
    session["derived_from"]["line_end"] = int(tail_result["checkpoint_line_count"])

    write_jsonl(generation_dir / "sessions.jsonl", sessions)
    write_jsonl(generation_dir / "turns.jsonl", turns)
    write_jsonl(generation_dir / "segments.jsonl", segments)
    write_jsonl(generation_dir / "capsules.jsonl", capsules)
    write_jsonl(generation_dir / "time_anchors.jsonl", anchors)
    write_jsonl(generation_dir / "evidence_refs.jsonl", evidence)
    write_jsonl(generation_dir / "links.jsonl", links)

    domain_index: dict[str, list[str]] = defaultdict(list)
    capsule_index: dict[str, list[str]] = defaultdict(list)
    for segment in segments:
        for tag in segment.get("domain_tags") or ["general"]:
            domain_index[str(tag)].append(str(segment.get("segment_id")))
    for capsule in capsules:
        for tag in capsule.get("domain_tags") or ["general"]:
            capsule_index[str(tag)].append(str(capsule.get("capsule_id")))
    domain_payload = {
        "ledger_id": session.get("ledger_id"),
        "status": "active",
        "updated_at": now,
        "domains": {key: sorted(value) for key, value in sorted(domain_index.items())},
        "capsule_domains": {key: sorted(value) for key, value in sorted(capsule_index.items())},
        "retrieval_order": [
            "_LEDGER_INDEX.md",
            "domain_index.json, capsules.jsonl, or sessions.jsonl",
            "segments.jsonl",
            "evidence_refs.jsonl",
            "raw session snippet only when exact detail matters",
        ],
    }
    (generation_dir / "domain_index.json").write_text(
        json.dumps(domain_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        errors="strict",
        newline="\n",
    )


def _rewrite_generation_index(
    generation_dir: Path,
    build_options: dict[str, Any],
    *,
    now: str,
) -> None:
    sessions = read_jsonl(generation_dir / "sessions.jsonl")
    turns = read_jsonl(generation_dir / "turns.jsonl")
    segments = read_jsonl(generation_dir / "segments.jsonl")
    capsules = read_jsonl(generation_dir / "capsules.jsonl")
    evidence = read_jsonl(generation_dir / "evidence_refs.jsonl")
    ledger_id = str(sessions[0].get("ledger_id") or "") if sessions else ""
    (generation_dir / "_LEDGER_INDEX.md").write_text(
        ledger_index_text(
            ledger_id=ledger_id,
            title=str(build_options.get("title") or "Conversation ledger"),
            project_lane=str(build_options.get("project_lane") or "PROJECTLESS"),
            now=now,
            session_count=len(sessions),
            turn_count=len(turns),
            segment_count=len(segments),
            capsule_count=len(capsules),
            evidence_count=len(evidence),
            conversation_memory_id=build_options.get("conversation_memory_id"),
            project_memory_id=build_options.get("project_memory_id"),
        ),
        encoding="utf-8",
        errors="strict",
        newline="\n",
    )


def refresh_generation(
    session_paths: list[Path],
    root: Path,
    **build_kwargs: Any,
) -> dict[str, Any]:
    root = root.resolve()
    try:
        current = load_active_generation(root)
        manifest = json.loads(
            (current / GENERATION_MANIFEST_FILE).read_text(
                encoding="utf-8-sig", errors="strict"
            )
        )
        if manifest.get("schema") != GENERATION_MANIFEST_SCHEMA:
            raise LedgerGenerationError("generation_manifest_schema_invalid")
        checkpoints = {
            str(item.get("session_id")): item for item in manifest.get("sessions") or []
        }
        tails: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
        total_physical_lines = 0
        for raw_path in [path.resolve() for path in session_paths]:
            session_id = session_id_from_path(raw_path)
            checkpoint = checkpoints.get(session_id)
            if checkpoint is None:
                raise LedgerGenerationError(
                    "session_checkpoint_not_found",
                    session_id=session_id,
                )
            tail_result = _collect_verified_tail(raw_path, checkpoint)
            total_physical_lines += int(tail_result["physical_lines_read"])
            tails.append((raw_path, checkpoint, tail_result))
        if total_physical_lines == 0:
            return {
                "status": "pass",
                "mode": "generation_refresh",
                "refreshed": False,
                "reason": "no_new_complete_lines",
                "generation_dir": str(current),
            }

        generations = root / "generations"
        staging = generations / f".staging-{uuid.uuid4().hex}"
        shutil.copytree(current, staging)
        now = utc_now()
        reused_evidence = len(read_jsonl(current / "evidence_refs.jsonl"))
        resume_from_line = min(
            int(checkpoint.get("checkpoint_line_count") or 0) + 1
            for _, checkpoint, _ in tails
        )
        for raw_path, checkpoint, tail_result in tails:
            _append_tail_to_generation(
                staging,
                session_path=raw_path,
                checkpoint=checkpoint,
                tail_result=tail_result,
                now=now,
            )
        options = dict(manifest.get("build_options") or {})
        options.update({key: value for key, value in build_kwargs.items() if value is not None})
        _rewrite_generation_index(staging, options, now=now)
        next_manifest = _generation_manifest(
            staging,
            [path for path, _, _ in tails],
            parent_generation_id=str(manifest.get("generation_id") or current.name),
            build_kwargs=options,
        )
        doctor = doctor_ledger(staging)
        if doctor["status"] == "fail":
            raise LedgerGenerationError("staging_doctor_failed", doctor=doctor)
        final_dir = _publish_generation(root, staging, next_manifest)
        return {
            "status": "warn" if doctor["status"] == "warn" else "pass",
            "mode": "generation_refresh",
            "refreshed": True,
            "refresh_strategy": "verified_incremental_tail",
            "generation_id": final_dir.name,
            "generation_dir": str(final_dir),
            "parent_generation_id": current.name,
            "resume_from_line": resume_from_line,
            "raw_lines_read": total_physical_lines,
            "reused_evidence_refs": reused_evidence,
            "doctor_status": doctor["status"],
        }
    except LedgerGenerationError as exc:
        staging_value = locals().get("staging")
        if isinstance(staging_value, Path) and staging_value.exists():
            shutil.rmtree(staging_value)
        return {
            "status": "fail",
            "mode": "generation_refresh",
            "refreshed": False,
            "reason": exc.code,
            **exc.details,
        }


def refresh_ledger(
    session_paths: list[Path],
    output_dir: Path,
    *,
    force: bool = False,
    auto: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    if output_dir.exists() and not force:
        doctor = doctor_ledger(output_dir)
        if doctor["status"] != "fail" and not doctor.get("stale"):
            return {
                "status": "pass",
                "mode": "refresh",
                "refreshed": False,
                "reason": "ledger_is_fresh",
                "doctor": doctor,
            }
        if auto and doctor["status"] == "warn" and not doctor.get("stale"):
            return {
                "status": "warn",
                "mode": "refresh",
                "refreshed": False,
                "reason": "auto_mode_warn_only_no_stale_raw_stat",
                "doctor": doctor,
                "cost_boundary": "auto mode does not rebuild on non-stale warnings",
            }
    result = build_ledger(session_paths, output_dir, **kwargs)
    result["mode"] = "refresh"
    result["refreshed"] = True
    return result


def auto_check(
    output_dir: Path,
    *,
    session_paths: list[Path] | None = None,
    refresh_on_stale: bool = False,
    build_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Cheap boundary check for normal turns and stop/final hooks.

    This is the default low-cost guard: inspect ledger files and raw file stats.
    It does not read raw payloads. If `refresh_on_stale` is set and a session
    path is provided, it rebuilds only when the stat-only check says stale.
    """

    output_dir = output_dir.resolve()
    if not output_dir.exists():
        if refresh_on_stale and session_paths:
            result = build_ledger(session_paths, output_dir, **(build_kwargs or {}))
            result["mode"] = "auto"
            result["action"] = "built_missing_ledger"
            return result
        return {
            "status": "warn",
            "mode": "auto",
            "action": "missing_ledger",
            "ledger_dir": str(output_dir),
            "cost_boundary": "checked ledger directory only",
        }

    doctor = doctor_ledger(output_dir)
    if refresh_on_stale and doctor.get("stale") and session_paths:
        result = build_ledger(session_paths, output_dir, **(build_kwargs or {}))
        result["mode"] = "auto"
        result["action"] = "refreshed_stale_ledger"
        result["previous_doctor"] = doctor
        return result

    return {
        "status": doctor["status"],
        "mode": "auto",
        "action": "checked_only",
        "doctor": doctor,
        "cost_boundary": "stat-only by default; no raw session payload read",
    }


def ledger_index_text(
    *,
    ledger_id: str,
    title: str,
    project_lane: str,
    now: str,
    session_count: int,
    turn_count: int,
    segment_count: int,
    capsule_count: int,
    evidence_count: int,
    conversation_memory_id: str | None,
    project_memory_id: str | None,
) -> str:
    return f"""# Conversation Ledger Index

This is a generated first-read routing surface for a conversation ledger.

## Ledger

```text
ledger_id: {ledger_id}
ledger_type: conversation_ledger
status: active
title: {title}
project_lane: {project_lane}
created_or_updated_at: {now}
conversation_memory_id: {conversation_memory_id or "null"}
project_memory_id: {project_memory_id or "null"}
```

## Counts

```text
sessions: {session_count}
turns: {turn_count}
segments: {segment_count}
capsules: {capsule_count}
evidence_refs: {evidence_count}
```

## Retrieval Rule

```text
read _LEDGER_INDEX.md
-> choose domain_index.json, capsules.jsonl, or sessions.jsonl
-> open matching segments.jsonl records
-> open selected evidence_refs.jsonl records
-> inspect raw session snippets or artifacts only when exact detail matters
```

## Source Boundary

- Raw session JSONL and artifact/test outputs are canonical evidence.
- Ledger summaries are navigation only.
- Client compaction summaries are not full-detail evidence.
- Exact user wording, code diffs, test results, external facts, R5 confirmations,
  and memory links require evidence refs.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        choices=[
            "build",
            "doctor",
            "refresh",
            "resolve",
            "auto",
            "generation-init",
            "generation-refresh",
        ],
        default="build",
        help="Operation mode. Omit for legacy build behavior.",
    )
    parser.add_argument("--session-jsonl", action="append", help="Codex session JSONL path. Can be repeated.")
    parser.add_argument("--output-dir", help="Directory where ledger files will be written.")
    parser.add_argument("--ledger-dir", help="Existing ledger directory for doctor/resolve. Defaults to --output-dir.")
    parser.add_argument("--ledger-id", default=None, help="Stable ledger id. Defaults to ledger:<first_session_id>.")
    parser.add_argument("--title", default="Conversation ledger", help="Human title for _LEDGER_INDEX.md.")
    parser.add_argument("--project-lane", default="PROJECTLESS")
    parser.add_argument("--conversation-memory-id", default=None)
    parser.add_argument("--conversation-memory-path", default=None)
    parser.add_argument("--project-memory-id", default=None)
    parser.add_argument("--continues-from-ledger-id", default=None)
    parser.add_argument("--ref-id", help="Evidence ref id for resolve mode.")
    parser.add_argument("--context-lines", type=int, default=20, help="Raw line context for resolve mode.")
    parser.add_argument("--max-chars-per-line", type=int, default=800, help="Truncate raw lines in resolve mode.")
    parser.add_argument("--force", action="store_true", help="Force refresh even when stat-only doctor says fresh.")
    parser.add_argument("--refresh-on-stale", action="store_true", help="In auto mode, rebuild only when stat-only checks say stale.")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    args = parse_args()
    ledger_dir_arg = args.ledger_dir or args.output_dir
    if args.command in {"generation-init", "generation-refresh"}:
        if not args.session_jsonl:
            raise SystemExit("--session-jsonl is required for generation mode")
        if not args.output_dir:
            raise SystemExit("--output-dir is required for generation mode")
        build_kwargs = {
            "ledger_id": args.ledger_id,
            "title": args.title,
            "project_lane": args.project_lane,
            "conversation_memory_id": args.conversation_memory_id,
            "conversation_memory_path": args.conversation_memory_path,
            "project_memory_id": args.project_memory_id,
            "continues_from_ledger_id": args.continues_from_ledger_id,
        }
        generation_function = (
            initialize_generation_root
            if args.command == "generation-init"
            else refresh_generation
        )
        result = generation_function(
            [Path(item) for item in args.session_jsonl],
            Path(args.output_dir),
            **build_kwargs,
        )
    elif args.command == "doctor":
        if not ledger_dir_arg:
            raise SystemExit("--ledger-dir or --output-dir is required for doctor mode")
        result = doctor_ledger(Path(ledger_dir_arg))
    elif args.command == "resolve":
        if not ledger_dir_arg:
            raise SystemExit("--ledger-dir or --output-dir is required for resolve mode")
        if not args.ref_id:
            raise SystemExit("--ref-id is required for resolve mode")
        result = resolve_evidence(
            Path(ledger_dir_arg),
            args.ref_id,
            context_lines=args.context_lines,
            max_chars_per_line=args.max_chars_per_line,
        )
    elif args.command == "auto":
        if not ledger_dir_arg:
            raise SystemExit("--ledger-dir or --output-dir is required for auto mode")
        build_kwargs = {
            "ledger_id": args.ledger_id,
            "title": args.title,
            "project_lane": args.project_lane,
            "conversation_memory_id": args.conversation_memory_id,
            "conversation_memory_path": args.conversation_memory_path,
            "project_memory_id": args.project_memory_id,
            "continues_from_ledger_id": args.continues_from_ledger_id,
        }
        result = auto_check(
            Path(ledger_dir_arg),
            session_paths=[Path(item) for item in args.session_jsonl] if args.session_jsonl else None,
            refresh_on_stale=args.refresh_on_stale,
            build_kwargs=build_kwargs,
        )
    else:
        if not args.session_jsonl:
            raise SystemExit("--session-jsonl is required for build/refresh mode")
        if not args.output_dir:
            raise SystemExit("--output-dir is required for build/refresh mode")
        build_kwargs = {
            "ledger_id": args.ledger_id,
            "title": args.title,
            "project_lane": args.project_lane,
            "conversation_memory_id": args.conversation_memory_id,
            "conversation_memory_path": args.conversation_memory_path,
            "project_memory_id": args.project_memory_id,
            "continues_from_ledger_id": args.continues_from_ledger_id,
        }
        if args.command == "refresh":
            result = refresh_ledger(
                [Path(item) for item in args.session_jsonl],
                Path(args.output_dir),
                force=args.force,
                **build_kwargs,
            )
        else:
            result = build_ledger(
                [Path(item) for item in args.session_jsonl],
                Path(args.output_dir),
                **build_kwargs,
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"pass", "warn"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
