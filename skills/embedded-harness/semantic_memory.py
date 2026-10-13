"""Pure-file semantic memory records with meta-first retrieval.

The canonical payload is append-only JSONL.  A separate compact JSONL meta
surface is navigation-only and points to one exact payload byte range (or a
legacy line locator). Discovery and explicit expansion can run separately. Legacy
records remain immutable and are exposed through hash-bound link records.

This module does not choose a memory lane, grant write authority, promote task
progress to durable memory, or open raw evidence automatically.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


RECORD_SCHEMA = "cbh.semantic_memory_record.v3"
META_SCHEMA = "cbh.semantic_memory_meta.v3"
STORE_SCHEMA = "cbh.semantic_memory_store.v3"
APPEND_RECEIPT_SCHEMA = "cbh.semantic_memory_append_receipt.v1"
SEARCH_RECEIPT_SCHEMA = "cbh.semantic_memory_search_receipt.v3"
UNIFIED_SEARCH_SCHEMA = "cbh.unified_memory_search_receipt.v1"
LEGACY_LINK_SCHEMA = "cbh.semantic_memory_legacy_link.v1"
DRAFT_SCHEMA = "cbh.semantic_memory_draft.v1"

QUERY_TYPES = {"current_state", "history_reason", "contradiction_check"}
LANE_KINDS = {"global", "project", "conversation"}
STATES = {
    "active",
    "stale",
    "superseded",
    "contradicted",
    "frozen_readonly",
    "historical",
}
MEMORY_CLASSES = {
    "error",
    "solution",
    "common_error",
    "interaction_error",
    "semantic_anchor",
    "major_incident",
    "conversation_event",
    "project_event",
    "event_cluster",
    "decision",
    "open_loop",
    "reference",
    "governance",
}
CURRENT_EXCLUDED_STATES = {
    "stale",
    "superseded",
    "contradicted",
    "frozen_readonly",
    "historical",
}
CONTENT_CONTEXT_FIELDS = {
    "event_summary",
    "details",
    "applicable_boundaries",
    "non_applicable_boundaries",
    "evidence_boundary",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_bytes(value: Any) -> bytes:
    return _canonical_json(value).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy(value: Any) -> Any:
    return json.loads(_canonical_json(value))


def _without(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    return {key: copy.deepcopy(item) for key, item in value.items() if key != field}


def _with_hash(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    base = _without(value, field)
    return {**base, field: _sha256_bytes(_canonical_bytes(base))}


def _normalized_strings(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


_RAW_EVIDENCE_READ_DEPTHS = {
    "artifact_output_window",
    "full_lane_audit",
    "raw_context_window",
    "source_cascade_review",
}


def _cascade_record_ids(records: Sequence[Mapping[str, Any]]) -> list[str]:
    return list(
        dict.fromkeys(
            str(record.get("record_id") or "").strip()
            for record in records
            if isinstance(record, Mapping)
            and str(record.get("record_id") or "").strip()
        )
    )


def _cascade_source_refs(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        ref = {
            key: _copy(record[key])
            for key in ("record_id", "source_id", "path", "line", "sha256")
            if record.get(key) not in (None, "")
        }
        if not ref:
            continue
        key = _canonical_json(ref)
        if key not in seen:
            refs.append(ref)
            seen.add(key)
    return refs


def _cascade_evidence_handles(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    handles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        for field in ("evidence_handles", "derived_from"):
            value = record.get(field)
            values = [value] if isinstance(value, Mapping) else value
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                continue
            for item in values:
                if not isinstance(item, Mapping):
                    continue
                handle = {
                    str(key): _copy(entry)
                    for key, entry in item.items()
                    if entry not in (None, "")
                }
                if not handle or not any(
                    key in handle
                    for key in (
                        "source_id",
                        "path",
                        "original_path",
                        "line",
                        "sha256",
                        "line_sha256_16",
                    )
                ):
                    continue
                key = _canonical_json(handle)
                if key not in seen:
                    handles.append(handle)
                    seen.add(key)
    return handles


def plan_memory_source_cascade(
    *,
    read_depth_profile: str,
    selected_records: Sequence[Mapping[str, Any]],
    semantic_review_candidates: Sequence[Mapping[str, Any]],
    retrieval_requested: bool,
    conversation_navigation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Plan the next source-preserving read without opening another source."""

    profile = str(read_depth_profile or "none")
    selected = [
        record for record in selected_records if isinstance(record, Mapping)
    ]
    candidates = [
        record
        for record in semantic_review_candidates
        if isinstance(record, Mapping)
    ]
    navigation_bundles = (
        list(conversation_navigation.get("bundles") or [])
        if isinstance(conversation_navigation, Mapping)
        else []
    )
    selected_ids = _cascade_record_ids(selected)
    candidate_ids = _cascade_record_ids(candidates)
    source_refs = _cascade_source_refs(selected)
    evidence_handles = _cascade_evidence_handles(selected)
    raw_required = profile in _RAW_EVIDENCE_READ_DEPTHS

    if not retrieval_requested and not navigation_bundles:
        status = "not_requested"
        current_layer = "none"
        required_layer = "none"
        next_action = "none"
        reasons = ["memory_retrieval_not_requested"]
        stop_condition = "no_memory_work"
    elif candidates and not selected:
        status = "semantic_review_required"
        current_layer = "recall_node"
        required_layer = "semantic_source"
        next_action = "select_and_materialize_semantic_source"
        reasons = ["candidate_payload_not_opened"]
        stop_condition = (
            "stop_after_one_selected_semantic_source_is_materialized_or_mark_unresolved"
        )
    elif selected:
        current_layer = "semantic_source"
        if raw_required:
            status = "drill_down_required"
            required_layer = "raw_evidence"
            if evidence_handles:
                next_action = "open_bounded_raw_evidence"
                reasons = ["read_depth_requires_raw_evidence", "evidence_handle_available"]
            else:
                next_action = "resolve_bounded_evidence_handle"
                reasons = ["read_depth_requires_raw_evidence", "evidence_handle_missing"]
            stop_condition = (
                "stop_after_required_bounded_evidence_is_opened_and_support_is_sufficient"
            )
        else:
            status = "semantic_source_ready"
            required_layer = "semantic_source"
            next_action = "stop_at_semantic_source"
            reasons = ["selected_semantic_source_is_sufficient_for_requested_depth"]
            stop_condition = (
                "stop_unless_ambiguity_or_strong_claim_requires_deeper_evidence"
            )
    elif navigation_bundles:
        status = "semantic_source_required"
        current_layer = "lane_resolver"
        required_layer = "semantic_source"
        next_action = "open_linked_semantic_source"
        reasons = ["conversation_lane_resolved_without_selected_semantic_record"]
        stop_condition = "stop_after_one_relevant_semantic_source_is_materialized"
    else:
        status = "no_match"
        current_layer = "lane_resolver"
        required_layer = "semantic_source"
        next_action = "no_match"
        reasons = ["no_recall_node_or_semantic_source_selected"]
        stop_condition = "stop_and_report_unresolved_memory_lookup"

    return {
        "schema": "cbh.memory_source_cascade.v1",
        "status": status,
        "read_depth_profile": profile,
        "current_layer": current_layer,
        "required_layer": required_layer,
        "next_action": next_action,
        "reason_codes": reasons,
        "selected_record_ids": selected_ids,
        "candidate_record_ids": candidate_ids,
        "semantic_source_refs": source_refs,
        "evidence_handles": evidence_handles,
        "raw_evidence_opened": False,
        "automatic_open": False,
        "authority_granted": False,
        "stop_condition": stop_condition,
    }


def _validate_query_type(query_type: str) -> None:
    if query_type not in QUERY_TYPES:
        raise ValueError("unsupported_memory_query_type")


def build_memory_record(
    *,
    record_id: str,
    memory_class: str,
    lane_id: str,
    lane_kind: str,
    owner_skill: str | None,
    observed_at: str,
    state: str,
    current_status: str,
    confidence: Mapping[str, Any],
    query_types: Sequence[str],
    candidate_label: str,
    summary: str,
    retrieval_terms: Sequence[str],
    facets: Mapping[str, Any],
    content: Mapping[str, Any],
    edges: Sequence[Mapping[str, Any]] = (),
    evidence_refs: Sequence[Mapping[str, Any]] = (),
    valid_from: str | None = None,
    valid_to: str | None = None,
    source_tag: str = "local_memory_write",
    belief_status: str = "recorded_observation",
    source_monitoring: Mapping[str, Any] | None = None,
    entity_id: str | None = None,
    slot: str | None = None,
) -> dict[str, Any]:
    base = {
        "schema": RECORD_SCHEMA,
        "record_id": record_id.strip(),
        "memory_class": memory_class,
        "owner": {
            "lane_id": lane_id.strip(),
            "lane_kind": lane_kind,
            "skill_id": owner_skill,
        },
        "entity": {
            "entity_id": entity_id or record_id.strip(),
            "slot": slot or memory_class,
        },
        "time": {
            "observed_at": observed_at,
            "valid_from": valid_from or observed_at,
            "valid_to": valid_to,
        },
        "recency": {
            "age_score": 1.0,
            "score_basis": "at_write",
            "anchor": observed_at,
        },
        "status": {"state": state, "current_status": current_status},
        "confidence": _copy(confidence),
        "query_types": _normalized_strings(query_types),
        "meta": {
            "candidate_label": candidate_label.strip(),
            "summary": summary.strip(),
            "retrieval_terms": _normalized_strings(retrieval_terms),
            "facets": _copy(facets),
        },
        "content": _copy(content),
        "edges": [_copy(edge) for edge in edges],
        "evidence_refs": [_copy(ref) for ref in evidence_refs],
        "provenance": {
            "source_tag": source_tag,
            "belief_status": belief_status,
            "source_monitoring": _copy(
                source_monitoring
                or {
                    "mode": "manual_or_event_driven",
                    "last_checked_at": observed_at,
                }
            ),
        },
        "lifecycle": {
            "state": state,
            "supersedes": [
                str(edge.get("target_id"))
                for edge in edges
                if edge.get("type") == "supersedes" and edge.get("target_id")
            ],
        },
    }
    return _with_hash(base, "record_sha256")


def build_memory_record_from_draft(draft: Mapping[str, Any]) -> dict[str, Any]:
    if draft.get("schema") != DRAFT_SCHEMA or not isinstance(draft.get("record"), Mapping):
        raise ValueError("invalid_memory_draft_schema")
    return build_memory_record(**dict(draft["record"]))


def validate_memory_record(record: Mapping[str, Any]) -> None:
    if record.get("schema") != RECORD_SCHEMA:
        raise ValueError("invalid_memory_record_schema")
    if not str(record.get("record_id") or "").strip():
        raise ValueError("memory_record_id_required")
    if record.get("memory_class") not in MEMORY_CLASSES:
        raise ValueError("unsupported_memory_class")
    owner = record.get("owner")
    if not isinstance(owner, Mapping) or owner.get("lane_kind") not in LANE_KINDS:
        raise ValueError("invalid_memory_owner")
    if not str(owner.get("lane_id") or "").strip():
        raise ValueError("memory_lane_id_required")
    status = record.get("status")
    if not isinstance(status, Mapping) or status.get("state") not in STATES:
        raise ValueError("invalid_memory_lifecycle_state")
    query_types = record.get("query_types")
    if (
        not isinstance(query_types, list)
        or not query_types
        or any(item not in QUERY_TYPES for item in query_types)
    ):
        raise ValueError("invalid_memory_query_types")
    meta = record.get("meta")
    if not isinstance(meta, Mapping):
        raise ValueError("memory_meta_required")
    if not str(meta.get("candidate_label") or "").strip():
        raise ValueError("memory_candidate_label_required")
    if not str(meta.get("summary") or "").strip():
        raise ValueError("memory_meta_summary_required")
    retrieval_terms = meta.get("retrieval_terms")
    if not isinstance(retrieval_terms, list) or not any(str(item).strip() for item in retrieval_terms):
        raise ValueError("memory_retrieval_terms_required")
    content = record.get("content")
    if not isinstance(content, Mapping) or not CONTENT_CONTEXT_FIELDS.issubset(content):
        raise ValueError("memory_content_context_incomplete")
    if not str(content.get("event_summary") or "").strip() or not str(
        content.get("evidence_boundary") or ""
    ).strip():
        raise ValueError("memory_content_context_incomplete")
    if not isinstance(content.get("applicable_boundaries"), list) or not isinstance(
        content.get("non_applicable_boundaries"), list
    ):
        raise ValueError("memory_content_context_incomplete")
    confidence = record.get("confidence")
    if not isinstance(confidence, Mapping) or not str(confidence.get("label") or "").strip():
        raise ValueError("memory_confidence_required")
    for edge in record.get("edges") or []:
        if not isinstance(edge, Mapping) or not edge.get("type") or not edge.get("target_id"):
            raise ValueError("invalid_memory_edge")
    expected = _sha256_bytes(_canonical_bytes(_without(record, "record_sha256")))
    if record.get("record_sha256") != expected:
        raise ValueError("memory_record_hash_mismatch")


def _store_paths(root: Path) -> tuple[Path, Path, Path]:
    return root / "store.json", root / "records.jsonl", root / "meta.jsonl"


def _ensure_store(root: Path, record: Mapping[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    store_path, _records_path, _meta_path = _store_paths(root)
    owner = record["owner"]
    manifest = {
        "schema": STORE_SCHEMA,
        "lane_id": owner["lane_id"],
        "lane_kind": owner["lane_kind"],
        "owner_skill": owner.get("skill_id"),
        "records": "records.jsonl",
        "meta": "meta.jsonl",
        "write_mode": "append_only",
        "retrieval_order": ["meta", "selected_record", "evidence_on_demand"],
        "evidence_boundary": "meta_is_navigation_not_fact_evidence",
    }
    encoded = _canonical_bytes(manifest) + b"\n"
    if not store_path.exists():
        store_path.write_bytes(encoded)
        return
    current = json.loads(store_path.read_text(encoding="utf-8", errors="strict"))
    if current != manifest:
        raise ValueError("memory_store_owner_mismatch")


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as handle:
        return sum(1 for line in handle if line.endswith(b"\n"))


def _append_line(path: Path, value: Mapping[str, Any]) -> tuple[int, int]:
    payload = _canonical_bytes(value) + b"\n"
    with path.open("ab") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return _line_count(path), len(payload)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="strict", newline="") as handle:
        for raw in handle:
            if raw.endswith("\r\n"):
                raise ValueError("memory_jsonl_crlf_not_allowed")
            if not raw.strip():
                continue
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("memory_jsonl_record_must_be_object")
            rows.append(value)
    return rows


def _meta_from_record(record: Mapping[str, Any], *, line: int, record_bytes: int, byte_offset: int | None = None) -> dict[str, Any]:
    meta = record["meta"]
    base = {
        "schema": META_SCHEMA,
        "record_id": record["record_id"],
        "memory_class": record["memory_class"],
        "owner": _copy(record["owner"]),
        "entity": _copy(record["entity"]),
        "candidate_label": meta["candidate_label"],
        "summary": meta["summary"],
        "retrieval_terms": _copy(meta["retrieval_terms"]),
        "facets": _copy(meta.get("facets") or {}),
        "observed_at": record["time"]["observed_at"],
        "state": record["status"]["state"],
        "current_status": record["status"]["current_status"],
        "query_types": _copy(record["query_types"]),
        "edges": _copy(record.get("edges") or []),
        "payload": {
            "relative_path": "records.jsonl",
            "line": line,
            "record_bytes": record_bytes,
            "record_sha256": record["record_sha256"],
        },
        "source_tag": record["provenance"]["source_tag"],
        "belief_status": record["provenance"]["belief_status"],
        "evidence_boundary": "navigation_only_open_selected_record_for_facts",
    }
    if byte_offset is not None:
        base["payload"]["byte_offset"] = byte_offset
    return _with_hash(base, "meta_sha256")


def append_memory_record(root: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    record_value = _copy(record)
    validate_memory_record(record_value)
    store_root = Path(root)
    _ensure_store(store_root, record_value)
    _store_path, records_path, meta_path = _store_paths(store_root)
    existing_records = _load_jsonl(records_path)
    existing_meta = _load_jsonl(meta_path)
    if len(existing_records) != len(existing_meta):
        raise ValueError("memory_store_recovery_required")
    existing = next(
        (item for item in existing_meta if item.get("record_id") == record_value["record_id"]),
        None,
    )
    if existing is not None:
        if existing.get("payload", {}).get("record_sha256") == record_value["record_sha256"]:
            return {
                "schema": APPEND_RECEIPT_SCHEMA,
                "status": "unchanged",
                "record_id": record_value["record_id"],
                "record_sha256": record_value["record_sha256"],
                "record_line": existing["payload"]["line"],
                "meta_line": existing_meta.index(existing) + 1,
            }
        raise ValueError("memory_record_id_conflict")
    record_line = _line_count(records_path) + 1
    record_bytes_value = len(_canonical_bytes(record_value) + b"\n")
    record_byte_offset = records_path.stat().st_size if records_path.exists() else 0
    appended_line, _written = _append_line(records_path, record_value)
    if appended_line != record_line:
        raise ValueError("memory_record_append_line_mismatch")
    meta = _meta_from_record(record_value, line=record_line, record_bytes=record_bytes_value, byte_offset=record_byte_offset)
    meta_line, _meta_written = _append_line(meta_path, meta)
    return {
        "schema": APPEND_RECEIPT_SCHEMA,
        "status": "appended",
        "record_id": record_value["record_id"],
        "record_sha256": record_value["record_sha256"],
        "record_line": record_line,
        "meta_line": meta_line,
        "store_root": str(store_root.resolve()),
        "durable_memory_written": True,
        "authority_granted": False,
    }


def rebuild_memory_meta(root: Path) -> dict[str, Any]:
    """Rebuild the disposable meta projection from canonical record bytes."""

    store_root = Path(root)
    store_path, records_path, meta_path = _store_paths(store_root)
    if not store_path.is_file() or not records_path.is_file():
        raise ValueError("memory_store_canonical_source_missing")
    manifest = json.loads(store_path.read_text(encoding="utf-8", errors="strict"))
    if manifest.get("schema") != STORE_SCHEMA:
        raise ValueError("invalid_store_schema")
    raw_lines = records_path.read_bytes().splitlines(keepends=True)
    metas: list[dict[str, Any]] = []
    byte_offset = 0
    for line_number, raw in enumerate(raw_lines, start=1):
        if not raw.endswith(b"\n") or raw.endswith(b"\r\n"):
            raise ValueError("memory_jsonl_lf_required")
        record = json.loads(raw[:-1].decode("utf-8", errors="strict"))
        if not isinstance(record, dict):
            raise ValueError("memory_jsonl_record_must_be_object")
        validate_memory_record(record)
        if raw != _canonical_bytes(record) + b"\n":
            raise ValueError("memory_record_not_canonical")
        metas.append(_meta_from_record(record, line=line_number, record_bytes=len(raw), byte_offset=byte_offset))
        byte_offset += len(raw)
    temporary = meta_path.with_suffix(meta_path.suffix + ".tmp")
    payload = b"".join(_canonical_bytes(meta) + b"\n" for meta in metas)
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, meta_path)
    return {
        "schema": "cbh.semantic_memory_meta_rebuild_receipt.v1",
        "status": "rebuilt",
        "store_root": str(store_root.resolve()),
        "record_count": len(metas),
        "meta_sha256": _sha256_bytes(payload),
        "canonical_records_changed": False,
        "authority_granted": False,
    }


def materialize_memory_record(root: Path, meta: Mapping[str, Any]) -> dict[str, Any]:
    if meta.get("schema") != META_SCHEMA:
        raise ValueError("invalid_memory_meta_schema")
    expected_meta_hash = _sha256_bytes(_canonical_bytes(_without(meta, "meta_sha256")))
    if meta.get("meta_sha256") != expected_meta_hash:
        raise ValueError("memory_meta_hash_mismatch")
    payload = meta.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("memory_meta_payload_locator_required")
    relative = str(payload.get("relative_path") or "")
    if relative != "records.jsonl":
        raise ValueError("memory_payload_path_not_allowed")
    line_number = int(payload.get("line") or 0)
    records_path = Path(root) / relative
    selected: bytes | None = None
    with records_path.open("rb") as handle:
        if "byte_offset" in payload:
            offset, size = payload["byte_offset"], payload.get("record_bytes")
            if (not isinstance(offset, int) or isinstance(offset, bool) or offset < 0
                    or not isinstance(size, int) or isinstance(size, bool) or size <= 0
                    or line_number <= 0):
                raise ValueError("invalid_memory_byte_range")
            handle.seek(offset)
            selected = handle.read(size)
            if len(selected) != size:
                raise ValueError("memory_payload_byte_range_incomplete")
        else:
            # Original v3 metadata remains readable until an explicit rebuild.
            for current, raw in enumerate(handle, start=1):
                if current == line_number:
                    selected = raw
                    break
    if selected is None or not selected.endswith(b"\n") or selected.endswith(b"\r\n"):
        raise ValueError("memory_payload_line_not_found")
    record = json.loads(selected[:-1].decode("utf-8", errors="strict"))
    validate_memory_record(record)
    if record.get("record_id") != meta.get("record_id"):
        raise ValueError("memory_payload_record_id_mismatch")
    if record.get("record_sha256") != payload.get("record_sha256"):
        raise ValueError("memory_payload_hash_mismatch")
    return record


def _terms(text: str) -> list[str]:
    lowered = text.casefold()
    values = re.findall(r"[a-z0-9][a-z0-9_.:+/-]{1,}|[\u4e00-\u9fff]{2,}", lowered)
    for run in re.findall(r"[\u4e00-\u9fff]{3,}", lowered):
        values.extend(run[index : index + 2] for index in range(len(run) - 1))
    return list(dict.fromkeys(values))


def _score_meta(meta: Mapping[str, Any], query: str) -> tuple[int, list[str]]:
    haystack = query.casefold()
    score = 0
    reasons: list[str] = []
    record_id = str(meta.get("record_id") or "")
    if record_id and record_id.casefold() in haystack:
        score += 100
        reasons.append("exact_record_id")
    for term in meta.get("retrieval_terms") or []:
        normalized = str(term).casefold().strip()
        if normalized and normalized in haystack:
            score += 20
            reasons.append(f"retrieval_term:{term}")
        else:
            hits = sum(1 for token in _terms(normalized) if len(token) >= 2 and token in haystack)
            if hits:
                score += min(6, hits * 2)
                reasons.append(f"retrieval_tokens:{term}")
    compact = " ".join(
        [str(meta.get("candidate_label") or ""), str(meta.get("summary") or "")]
    ).casefold()
    query_terms = [token for token in _terms(query) if len(token) >= 2]
    overlap = sum(1 for token in query_terms if token in compact)
    if overlap:
        score += min(8, overlap * 2)
        reasons.append(f"meta_overlap:{overlap}")
    return score, reasons


def _eligible(meta: Mapping[str, Any], query_type: str) -> bool:
    if query_type not in set(meta.get("query_types") or []):
        return False
    # Incident records describe observations, not the current operating policy.
    # Keep immutable payloads available through history/contradiction queries.
    if query_type == "current_state" and meta.get("memory_class") in {
        "error", "common_error", "interaction_error", "major_incident"
    }:
        return False
    state = str(meta.get("state") or "")
    if query_type == "current_state" and state in CURRENT_EXCLUDED_STATES:
        return False
    return True


def search_memory_store(
    root: Path,
    query: str,
    *,
    query_type: str,
    limit: int = 5,
    metadata_only: bool = False,
) -> dict[str, Any]:
    _validate_query_type(query_type)
    store_root = Path(root)
    _store_path, _records_path, meta_path = _store_paths(store_root)
    meta_bytes = meta_path.stat().st_size if meta_path.is_file() else 0
    metas = _load_jsonl(meta_path)
    candidates: list[tuple[int, int, dict[str, Any], list[str]]] = []
    for ordinal, meta in enumerate(metas):
        if meta.get("schema") != META_SCHEMA:
            raise ValueError("invalid_memory_meta_schema")
        if not _eligible(meta, query_type):
            continue
        score, reasons = _score_meta(meta, query)
        if score > 0:
            candidates.append((score, -ordinal, meta, reasons))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected_records: list[dict[str, Any]] = []
    candidate_views: list[dict[str, Any]] = []
    payload_bytes = 0
    for score, _ordinal, meta, reasons in candidates[: max(1, limit)]:
        if metadata_only or score < 8:
            candidate_views.append(
                {
                    "record_id": meta["record_id"],
                    "candidate_label": meta["candidate_label"],
                    "summary": meta["summary"],
                    "state": meta["state"],
                    "current_status": meta["current_status"],
                    "memory_class": meta["memory_class"],
                    "owner": _copy(meta["owner"]),
                    "facets": _copy(meta.get("facets") or {}),
                    "edges": _copy(meta.get("edges") or []),
                    "score": score,
                    "selection_reasons": reasons,
                    "evidence_boundary": "navigation_candidate_only_not_fact_match",
                }
            )
            continue
        record = materialize_memory_record(store_root, meta)
        selected_records.append(record)
        payload_bytes += int(meta["payload"].get("record_bytes") or 0)
    coverage = (
        "complete"
        if selected_records and not candidate_views
        else "semantic_review_required"
        if selected_records or candidate_views
        else "no_match"
    )
    return {
        "schema": SEARCH_RECEIPT_SCHEMA,
        "coverage_status": coverage,
        "query_type": query_type,
        "selected_record_ids": [item["record_id"] for item in selected_records],
        "selected_records": selected_records,
        "candidate_views": candidate_views,
        "metrics": {
            "meta_bytes_read": meta_bytes,
            "payload_bytes_read": payload_bytes,
            "selected_context_chars": sum(len(_canonical_json(item)) for item in selected_records),
        },
        "raw_evidence_opened": False,
        "authority_granted": False,
    }


def read_memory_record(root: Path, record_id: str) -> dict[str, Any]:
    """Expand one explicitly selected record, verifying its exact source bytes."""
    _store_path, _records_path, meta_path = _store_paths(Path(root))
    for meta in _load_jsonl(meta_path):
        if meta.get("record_id") == record_id:
            return materialize_memory_record(root, meta)
    raise ValueError("memory_record_not_found")


def _extract_heading(text: str, heading: str, level: int) -> str:
    hashes = "#" * int(level)
    match = re.search(
        rf"(?ms)^{re.escape(hashes)}\s+{re.escape(heading)}\s*$\s*"
        rf"(.*?)(?=^{re.escape(hashes)}\s+|\Z)",
        text,
    )
    if not match:
        raise ValueError("legacy_heading_not_found")
    return match.group(1).rstrip()


def _legacy_link(link: Mapping[str, Any]) -> dict[str, Any]:
    locator = link.get("locator")
    if not isinstance(locator, Mapping):
        raise ValueError("legacy_locator_required")
    path = Path(str(locator.get("path") or ""))
    if not path.is_file():
        raise ValueError("legacy_payload_not_found")
    text = path.read_text(encoding="utf-8-sig", errors="strict")
    heading = str(locator.get("heading") or link.get("record_id") or "")
    level = int(locator.get("heading_level") or 1)
    block = _extract_heading(text, heading, level)
    normalized_locator = {
        **_copy(locator),
        "path": str(path.resolve()),
        "heading": heading,
        "heading_level": level,
        "file_sha256": _sha256_file(path),
        "block_sha256": _sha256_bytes(block.encode("utf-8")),
    }
    base = {
        "schema": LEGACY_LINK_SCHEMA,
        "link_id": str(link.get("link_id") or f"legacy:{link.get('record_id')}") ,
        "link_type": "legacy_payload",
        "record_id": str(link.get("record_id") or ""),
        "family": str(link.get("family") or "LEGACY"),
        "memory_class": str(link.get("memory_class") or "reference"),
        "state": str(link.get("state") or "frozen_readonly"),
        "candidate_label": str(link.get("candidate_label") or link.get("record_id") or ""),
        "summary": str(link.get("summary") or "Legacy read-only memory record."),
        "retrieval_terms": _normalized_strings(link.get("retrieval_terms") or []),
        "query_types": _normalized_strings(link.get("query_types") or ["history_reason"]),
        "locator": normalized_locator,
        "typed_edges": [_copy(edge) for edge in link.get("typed_edges") or []],
        "content_schema": str(link.get("content_schema") or "legacy_unversioned"),
        "retrieval_mode": "meta_first_link_only",
        "legacy_adapter": str(link.get("legacy_adapter") or "markdown_heading"),
        "evidence_boundary": "navigation_only_open_legacy_payload_for_facts",
    }
    if not base["record_id"] or not base["retrieval_terms"]:
        raise ValueError("legacy_link_context_incomplete")
    if any(item not in QUERY_TYPES for item in base["query_types"]):
        raise ValueError("invalid_memory_query_types")
    return _with_hash(base, "link_sha256")


def write_legacy_link_manifest(path: Path, links: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    normalized = [_legacy_link(link) for link in links]
    ids = [item["record_id"] for item in normalized]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_legacy_record_id")
    payload = b"".join(_canonical_bytes(item) + b"\n" for item in normalized)
    output.write_bytes(payload)
    return {
        "schema": "cbh.semantic_memory_legacy_manifest_receipt.v1",
        "status": "written",
        "path": str(output.resolve()),
        "record_count": len(normalized),
        "sha256": _sha256_bytes(payload),
        "payload_copied": False,
    }


def materialize_legacy_link(link: Mapping[str, Any]) -> dict[str, Any]:
    if link.get("schema") != LEGACY_LINK_SCHEMA:
        raise ValueError("invalid_legacy_link_schema")
    expected_link_hash = _sha256_bytes(_canonical_bytes(_without(link, "link_sha256")))
    if link.get("link_sha256") != expected_link_hash:
        raise ValueError("legacy_link_hash_mismatch")
    locator = link.get("locator")
    if not isinstance(locator, Mapping):
        raise ValueError("legacy_locator_required")
    path = Path(str(locator.get("path") or ""))
    if not path.is_file():
        raise ValueError("legacy_payload_not_found")
    if _sha256_file(path) != locator.get("file_sha256"):
        raise ValueError("legacy_payload_hash_mismatch")
    text = path.read_text(encoding="utf-8-sig", errors="strict")
    block = _extract_heading(
        text,
        str(locator.get("heading") or ""),
        int(locator.get("heading_level") or 1),
    )
    if _sha256_bytes(block.encode("utf-8")) != locator.get("block_sha256"):
        raise ValueError("legacy_record_hash_mismatch")
    return {
        "schema": "cbh.semantic_memory_legacy_materialized.v1",
        "record_id": link["record_id"],
        "memory_class": link["memory_class"],
        "state": link["state"],
        "legacy_text": block,
        "typed_edges": _copy(link.get("typed_edges") or []),
        "source_tag": "legacy_memory_link",
        "belief_status": "legacy_status_only",
        "evidence_boundary": "legacy_payload_exact_heading_verified",
        "derived_from": {
            "path": str(path.resolve()),
            "file_sha256": locator["file_sha256"],
            "block_sha256": locator["block_sha256"],
        },
    }


def _search_legacy_links(
    path: Path,
    query: str,
    *,
    query_type: str,
    limit: int,
) -> dict[str, Any]:
    _validate_query_type(query_type)
    links = _load_jsonl(Path(path))
    candidates: list[tuple[int, int, dict[str, Any], list[str]]] = []
    for ordinal, link in enumerate(links):
        if link.get("schema") != LEGACY_LINK_SCHEMA:
            raise ValueError("invalid_legacy_link_schema")
        if not _eligible(link, query_type):
            continue
        meta = {
            "record_id": link["record_id"],
            "candidate_label": link["candidate_label"],
            "summary": link["summary"],
            "retrieval_terms": link["retrieval_terms"],
        }
        score, reasons = _score_meta(meta, query)
        if score > 0:
            candidates.append((score, -ordinal, link, reasons))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected: list[dict[str, Any]] = []
    views: list[dict[str, Any]] = []
    payload_bytes = 0
    for score, _ordinal, link, reasons in candidates[: max(1, limit)]:
        if score < 8:
            views.append(
                {
                    "record_id": link["record_id"],
                    "candidate_label": link["candidate_label"],
                    "summary": link["summary"],
                    "state": link["state"],
                    "score": score,
                    "selection_reasons": reasons,
                    "evidence_boundary": "navigation_candidate_only_not_fact_match",
                }
            )
            continue
        item = materialize_legacy_link(link)
        selected.append(item)
        payload_bytes += len(item["legacy_text"].encode("utf-8"))
    return {
        "selected_records": selected,
        "candidate_views": views,
        "metrics": {
            "meta_bytes_read": Path(path).stat().st_size if Path(path).is_file() else 0,
            "payload_bytes_read": payload_bytes,
            "selected_context_chars": sum(len(_canonical_json(item)) for item in selected),
        },
    }


def unified_memory_search(
    query: str,
    *,
    query_type: str,
    store_roots: Sequence[Path] = (),
    legacy_manifest: Path | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    _validate_query_type(query_type)
    selected: list[dict[str, Any]] = []
    views: list[dict[str, Any]] = []
    metrics = {"meta_bytes_read": 0, "payload_bytes_read": 0, "selected_context_chars": 0}
    for root in store_roots:
        result = search_memory_store(root, query, query_type=query_type, limit=limit)
        selected.extend(result["selected_records"])
        views.extend(result["candidate_views"])
        for key in metrics:
            metrics[key] += int(result["metrics"][key])
    if legacy_manifest is not None and Path(legacy_manifest).is_file():
        legacy = _search_legacy_links(
            Path(legacy_manifest),
            query,
            query_type=query_type,
            limit=limit,
        )
        selected.extend(legacy["selected_records"])
        views.extend(legacy["candidate_views"])
        for key in metrics:
            metrics[key] += int(legacy["metrics"][key])
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in selected:
        record_id = str(item.get("record_id") or "")
        if not record_id or record_id in seen:
            continue
        seen.add(record_id)
        unique.append(item)
    unique = unique[: max(1, limit)]
    coverage = (
        "complete"
        if unique and not views
        else "semantic_review_required"
        if unique or views
        else "no_match"
    )
    return {
        "schema": UNIFIED_SEARCH_SCHEMA,
        "coverage_status": coverage,
        "query_type": query_type,
        "selected_record_ids": [item["record_id"] for item in unique],
        "selected_records": unique,
        "candidate_views": views[: max(1, limit)],
        "metrics": metrics,
        "raw_evidence_opened": False,
        "authority_granted": False,
    }


def check_memory_store(root: Path) -> dict[str, Any]:
    store_root = Path(root)
    store_path, records_path, meta_path = _store_paths(store_root)
    issues: list[dict[str, Any]] = []
    if not store_path.is_file():
        issues.append({"reason": "store_manifest_missing"})
    else:
        try:
            manifest = json.loads(store_path.read_text(encoding="utf-8", errors="strict"))
            if manifest.get("schema") != STORE_SCHEMA:
                issues.append({"reason": "invalid_store_schema"})
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            issues.append({"reason": f"store_manifest_unreadable:{type(exc).__name__}"})
    try:
        records = _load_jsonl(records_path)
        metas = _load_jsonl(meta_path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "schema": "cbh.semantic_memory_store_check.v1",
            "status": "fail",
            "record_count": 0,
            "meta_count": 0,
            "issues": [*issues, {"reason": str(exc)}],
        }
    if len(records) != len(metas):
        issues.append(
            {
                "reason": "record_meta_count_mismatch",
                "record_count": len(records),
                "meta_count": len(metas),
            }
        )
    for line, record in enumerate(records, start=1):
        try:
            validate_memory_record(record)
        except ValueError as exc:
            issues.append({"line": line, "surface": "record", "reason": str(exc)})
    for line, meta in enumerate(metas, start=1):
        try:
            materialized = materialize_memory_record(store_root, meta)
            if int(meta.get("payload", {}).get("line") or 0) != line:
                raise ValueError("memory_meta_line_order_mismatch")
            if line <= len(records) and materialized.get("record_id") != records[line - 1].get(
                "record_id"
            ):
                raise ValueError("memory_meta_record_order_mismatch")
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            issues.append({"line": line, "surface": "meta", "reason": str(exc)})
    return {
        "schema": "cbh.semantic_memory_store_check.v1",
        "status": "pass" if not issues else "fail",
        "record_count": len(records),
        "meta_count": len(metas),
        "issues": issues,
    }


def _load_record_argument(args: argparse.Namespace) -> dict[str, Any]:
    if args.record_file:
        value = json.loads(Path(args.record_file).read_text(encoding="utf-8", errors="strict"))
    else:
        value = json.loads(args.record_json)
    if not isinstance(value, dict):
        raise ValueError("memory_record_must_be_object")
    return build_memory_record_from_draft(value) if value.get("schema") == DRAFT_SCHEMA else value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("append", "search", "discover", "read", "check", "rebuild-meta"))
    parser.add_argument("--store-root", required=True)
    record_group = parser.add_mutually_exclusive_group()
    record_group.add_argument("--record-file")
    record_group.add_argument("--record-json")
    parser.add_argument("--query")
    parser.add_argument("--record-id")
    parser.add_argument("--query-type", choices=sorted(QUERY_TYPES), default="history_reason")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    if args.operation == "append":
        if not (args.record_file or args.record_json):
            parser.error("append requires --record-file or --record-json")
        result = append_memory_record(Path(args.store_root), _load_record_argument(args))
    elif args.operation in {"search", "discover"}:
        if args.query is None:
            parser.error("search requires --query")
        result = search_memory_store(
            Path(args.store_root),
            args.query,
            query_type=args.query_type,
            limit=args.limit,
            metadata_only=args.operation == "discover",
        )
    elif args.operation == "read":
        if not args.record_id:
            parser.error("read requires --record-id")
        result = read_memory_record(Path(args.store_root), args.record_id)
    elif args.operation == "check":
        result = check_memory_store(Path(args.store_root))
    else:
        result = rebuild_memory_meta(Path(args.store_root))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    print(_canonical_json(result))
    return 1 if result.get("status") == "fail" else 0


__all__ = [
    "APPEND_RECEIPT_SCHEMA",
    "LEGACY_LINK_SCHEMA",
    "META_SCHEMA",
    "RECORD_SCHEMA",
    "STORE_SCHEMA",
    "append_memory_record",
    "build_memory_record",
    "build_memory_record_from_draft",
    "check_memory_store",
    "materialize_legacy_link",
    "materialize_memory_record",
    "plan_memory_source_cascade",
    "rebuild_memory_meta",
    "read_memory_record",
    "search_memory_store",
    "unified_memory_search",
    "validate_memory_record",
    "write_legacy_link_manifest",
]


if __name__ == "__main__":
    raise SystemExit(main())
