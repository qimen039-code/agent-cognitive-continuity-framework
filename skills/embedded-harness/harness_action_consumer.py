from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from behavior_correction_gate import build_behavior_correction_receipt
from engineering_execution import build_engineering_execution_receipt
from execution_feedback import CorrectionProfileRegistryError, matches_behavior_pattern
from external_retrieval_strategy import build_external_retrieval_receipt
from evidence_locator import source_id_for_path
from codex_session_ledger import load_active_generation
from semantic_memory import LEGACY_LINK_SCHEMA, META_SCHEMA as SEMANTIC_MEMORY_META_SCHEMA
from semantic_memory import STORE_SCHEMA as SEMANTIC_MEMORY_STORE_SCHEMA
from semantic_memory import materialize_legacy_link, materialize_memory_record
from semantic_memory import plan_memory_source_cascade
from task_continuity import (
    apply_task_event,
    build_dynamic_reminders,
    build_task_capsule_context,
    decide_task_continuity,
    initialize_task_capsule,
    page_result,
    plan_transport,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_COMMON_ERROR_META = ROOT.parent / "troubleshooting-skill-matrix" / "common-error-corpus" / "_META_INDEX.md"
DEFAULT_MATRIX_META = ROOT.parent / "troubleshooting-skill-matrix" / "_META_INDEX.md"
DEFAULT_INTERACTION_ROOT = ROOT.parent / "troubleshooting-skill-matrix" / "interaction-error-corpus"
DEFAULT_MAJOR_INCIDENT_META = ROOT.parent / "troubleshooting-skill-matrix" / "major-incidents" / "_META_INDEX.md"
DEFAULT_ERROR_SKILL = ROOT.parent / "agent-error-memory" / "SKILL.md"
DEFAULT_ERROR_PAYLOAD = ROOT.parent / "agent-error-memory" / "references" / "error-points.md"
DEFAULT_SOLUTION_SKILL = ROOT.parent / "bug-solution-memory" / "SKILL.md"
DEFAULT_SOLUTION_PAYLOAD = ROOT.parent / "bug-solution-memory" / "references" / "solution-points.md"
DEFAULT_ANCHOR_SKILL = ROOT.parent / "shared-semantic-anchors" / "SKILL.md"
DEFAULT_ANCHOR_PAYLOAD = ROOT.parent / "shared-semantic-anchors" / "references" / "semantic-anchors.md"
DEFAULT_LEGACY_MEMORY_LINKS = ROOT / "legacy_memory_links.local.jsonl"
DEFAULT_SEMANTIC_V3_STORES = (
    ROOT.parent / "agent-error-memory" / "references" / "memory-v3",
    ROOT.parent / "bug-solution-memory" / "references" / "memory-v3",
    ROOT.parent / "shared-semantic-anchors" / "references" / "memory-v3",
    ROOT.parent / "troubleshooting-skill-matrix" / "common-error-corpus" / "memory-v3",
    ROOT.parent / "troubleshooting-skill-matrix" / "interaction-error-corpus" / "memory-v3",
    ROOT.parent / "troubleshooting-skill-matrix" / "major-incidents" / "memory-v3",
    ROOT / "memory-v3",
)
DEFAULT_CAPABILITY_CONTRACT = ROOT / "host_capability_contract.json"
SOFT_TARGET_RECORDS = 2
CONTEXT_SOFT_TARGET_CHARS = 1800
DIRECT_MATCH_SCORE = 8
MEMORY_QUERY_TYPES = {"current_state", "history_reason", "contradiction_check"}
SEMANTIC_REVIEW_CANDIDATE_LIMIT = 6
INCIDENT_DISAMBIGUATION_LIMIT = 5
NONCURRENT_STATUS_MARKERS = (
    "superseded",
    "historical",
    "harmful_after_update",
    "do_not_auto_reuse",
    "do_not_apply",
    "quarantined",
    "cleared",
    "reclassified",
)
REGISTERED_FACT_PRODUCERS = {
    "typed_tool_result",
    "deterministic_parser",
    "mechanical_verifier",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _retrieval_terms(cell: str) -> list[str]:
    exact = [item.strip() for item in re.findall(r"`([^`]+)`", cell) if item.strip()]
    if exact:
        return list(dict.fromkeys(exact))
    return list(
        dict.fromkeys(
            item.strip(" `")
            for item in re.split(r"[,;；，]", cell)
            if item.strip(" `")
        )
    )


def _incident_date_from_id(record_id: str) -> str:
    match = re.search(r"(20\d{2})-(0[1-9]|1[0-2])-([0-2]\d|3[01])", record_id)
    return "-".join(match.groups()) if match else ""


def _cjk_ngrams(text: str) -> set[str]:
    grams: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        for width in (2, 3, 4):
            grams.update(run[index : index + width] for index in range(len(run) - width + 1))
    return grams


def _query_date_parts(text: str) -> dict[str, int | None]:
    iso = re.search(r"\b(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])\b", text)
    if iso:
        return {"year": int(iso.group(1)), "month": int(iso.group(2)), "day": int(iso.group(3))}
    chinese = re.search(
        r"(?:(20\d{2})\s*年)?\s*(1[0-2]|0?[1-9])\s*月(?:\s*(3[01]|[12]\d|0?[1-9])\s*[日号])?",
        text,
    )
    if chinese:
        return {
            "year": int(chinese.group(1)) if chinese.group(1) else None,
            "month": int(chinese.group(2)),
            "day": int(chinese.group(3)) if chinese.group(3) else None,
        }
    chinese_named = re.search(r"(十二|十一|十|[一二三四五六七八九])\s*月份?", text)
    if chinese_named:
        named_months = {
            "一": 1,
            "二": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
            "十一": 11,
            "十二": 12,
        }
        return {"year": None, "month": named_months[chinese_named.group(1)], "day": None}
    english = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december|"
        r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b",
        text,
        flags=re.IGNORECASE,
    )
    if english:
        english_months = {
            "jan": 1,
            "feb": 2,
            "mar": 3,
            "apr": 4,
            "may": 5,
            "jun": 6,
            "jul": 7,
            "aug": 8,
            "sep": 9,
            "oct": 10,
            "nov": 11,
            "dec": 12,
        }
        return {"year": None, "month": english_months[english.group(1).casefold()[:3]], "day": None}
    return {"year": None, "month": None, "day": None}


def _catalog(index_path: Path, family: str = "CE") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    prefix = f"{family}-"
    for raw in index_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line.startswith("|") or prefix not in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        record_id = cells[0].strip("`")
        if not record_id.startswith(prefix):
            continue
        candidate_label = ""
        event_date = ""
        if family == "CE" and len(cells) >= 7:
            retrieval_cell = cells[3]
        elif family == "IE" and len(cells) >= 4:
            retrieval_cell = cells[2]
        elif family == "INC" and len(cells) >= 7:
            candidate_label = cells[1].strip("`")
            event_date = cells[2].strip("`")
            retrieval_cell = cells[3]
        else:
            retrieval_cell = cells[1]
        linked_record_ids = (
            re.findall(r"(?:ERR|SOL|INC)-[A-Z0-9-]+", " ".join(cells[2:]))
            if family in {"ERR", "SOL"} and len(cells) >= 3
            else re.findall(r"(?:ERR|SOL)-[A-Z0-9-]+", " ".join(cells[5:7]))
            if family == "INC" and len(cells) >= 7
            else re.findall(r"(?:ERR|SOL)-[A-Z0-9-]+", " ".join(cells[3:5]))
            if family == "INC" and len(cells) >= 5
            else []
        )
        status = (
            cells[2]
            if family == "CE" and len(cells) >= 7
            else cells[4]
            if family == "INC" and len(cells) >= 7
            else cells[2]
            if family == "INC" and len(cells) >= 3
            else cells[1]
            if family == "IE" and len(cells) >= 2
            else "INDEXED"
        ).strip("`")
        terms = _retrieval_terms(retrieval_cell)
        if family == "INC":
            event_date = event_date or _incident_date_from_id(record_id)
            candidate_label = candidate_label or "、".join(terms[:3])
        rows.append(
            {
                "record_id": record_id,
                "family": family,
                "class": (
                    cells[1]
                    if family == "CE" and len(cells) >= 7
                    else "interaction_error"
                    if family == "IE"
                    else "major_incident"
                    if family == "INC"
                    else "indexed_memory"
                ),
                "status": status,
                "retrieval_terms": terms,
                "candidate_label": candidate_label,
                "event_date": event_date,
                "solution_summary": cells[5] if family == "CE" and len(cells) >= 7 else "",
                "index_path": str(index_path),
                "linked_record_ids": list(dict.fromkeys(linked_record_ids)),
            }
        )
    return rows


def _flatten_text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_flatten_text_values(item))
        return result
    return []


def _catalog_jsonl(index_path: Path, family: str = "LANE") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with index_path.open("r", encoding="utf-8", errors="strict") as handle:
        for line_no, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            record = json.loads(raw)
            record_id = str(record.get("record_id") or record.get("link_id") or "")
            if not record_id:
                continue
            terms: list[str] = [record_id]
            for field in (
                "retrieval_terms",
                "summary",
                "decision",
                "domain",
                "event_id",
                "error_id",
                "solution_id",
                "current_status",
            ):
                terms.extend(_flatten_text_values(record.get(field)))
            linked_text = json.dumps(record, ensure_ascii=False)
            rows.append(
                {
                    "record_id": record_id,
                    "family": family,
                    "class": str(record.get("domain") or "lane_record"),
                    "status": str(record.get("status") or record.get("lifecycle") or "UNKNOWN"),
                    "retrieval_terms": list(dict.fromkeys(term for term in terms if term)),
                    "solution_summary": str(record.get("summary") or ""),
                    "index_path": str(index_path),
                    "linked_record_ids": list(
                        dict.fromkeys(re.findall(r"(?:INC|ERR|SOL|CE)-[A-Z0-9-]+", linked_text))
                    ),
                    "json_record": record,
                    "jsonl_line": line_no,
                }
            )
    return rows


def _catalog_semantic_v3(index_path: Path) -> list[dict[str, Any]]:
    family_by_class = {
        "error": "ERR",
        "solution": "SOL",
        "common_error": "CE",
        "interaction_error": "IE",
        "semantic_anchor": "ANCHOR",
        "major_incident": "INC",
        "event_cluster": "CLUSTER",
    }
    rows: list[dict[str, Any]] = []
    with index_path.open("r", encoding="utf-8", errors="strict", newline="") as handle:
        for line_no, raw in enumerate(handle, start=1):
            if raw.endswith("\r\n"):
                raise ValueError("memory_jsonl_crlf_not_allowed")
            if not raw.strip():
                continue
            meta = json.loads(raw)
            if not isinstance(meta, dict) or meta.get("schema") != SEMANTIC_MEMORY_META_SCHEMA:
                raise ValueError("invalid_memory_meta_schema")
            memory_class = str(meta.get("memory_class") or "reference")
            linked_ids = [
                str(edge.get("target_id"))
                for edge in meta.get("edges") or []
                if isinstance(edge, dict) and edge.get("target_id")
            ]
            rows.append(
                {
                    "record_id": str(meta["record_id"]),
                    "family": family_by_class.get(memory_class, "V3"),
                    "class": memory_class,
                    "status": str(meta.get("state") or "UNKNOWN"),
                    "retrieval_terms": list(
                        dict.fromkeys(
                            [
                                str(meta["record_id"]),
                                str(meta.get("candidate_label") or ""),
                                str(meta.get("summary") or ""),
                                *[str(item) for item in meta.get("retrieval_terms") or []],
                            ]
                        )
                    ),
                    "candidate_label": str(meta.get("candidate_label") or ""),
                    "event_date": str(meta.get("observed_at") or "")[:10],
                    "solution_summary": str(meta.get("summary") or ""),
                    "index_path": str(index_path),
                    "linked_record_ids": list(dict.fromkeys(linked_ids)),
                    "query_types": [str(item) for item in meta.get("query_types") or []],
                    "semantic_meta": meta,
                    "jsonl_line": line_no,
                }
            )
    return rows


def _catalog_legacy_manifest(index_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with index_path.open("r", encoding="utf-8", errors="strict", newline="") as handle:
        for line_no, raw in enumerate(handle, start=1):
            if raw.endswith("\r\n"):
                raise ValueError("memory_jsonl_crlf_not_allowed")
            if not raw.strip():
                continue
            link = json.loads(raw)
            if not isinstance(link, dict) or link.get("schema") != LEGACY_LINK_SCHEMA:
                raise ValueError("invalid_legacy_link_schema")
            family = str(link.get("family") or "LEGACY")
            linked_ids = [
                str(edge.get("target_id"))
                for edge in link.get("typed_edges") or []
                if isinstance(edge, dict) and edge.get("target_id")
            ]
            rows.append(
                {
                    "record_id": str(link["record_id"]),
                    "family": family,
                    "class": str(link.get("memory_class") or "reference"),
                    "status": str(link.get("state") or "UNKNOWN"),
                    "retrieval_terms": list(
                        dict.fromkeys(
                            [
                                str(link["record_id"]),
                                str(link.get("candidate_label") or ""),
                                str(link.get("summary") or ""),
                                *[str(item) for item in link.get("retrieval_terms") or []],
                            ]
                        )
                    ),
                    "candidate_label": str(link.get("candidate_label") or ""),
                    "event_date": _incident_date_from_id(str(link["record_id"])),
                    "solution_summary": str(link.get("summary") or ""),
                    "index_path": str(index_path),
                    "linked_record_ids": list(dict.fromkeys(linked_ids)),
                    "legacy_link": link,
                    "jsonl_line": line_no,
                }
            )
    return rows


def _catalog_meta(index_path: Path, family: str = "META") -> list[dict[str, Any]]:
    text = index_path.read_text(encoding="utf-8-sig")
    exact = [item.strip() for item in re.findall(r"`([^`]+)`", text) if item.strip()]
    headings = [
        match.group(1).strip()
        for match in re.finditer(r"(?m)^#{1,4}\s+(.+?)\s*$", text)
        if match.group(1).strip()
    ]
    stable = hashlib.sha256(str(index_path.resolve()).casefold().encode("utf-8")).hexdigest()[:12].upper()
    return [
        {
            "record_id": f"META-{stable}",
            "family": family,
            "class": "memory_meta_index",
            "status": "NAVIGATION_ONLY",
            "retrieval_terms": list(dict.fromkeys([index_path.parent.name, *headings, *exact])),
            "solution_summary": "",
            "index_path": str(index_path),
            "linked_record_ids": [],
            "meta_text": text,
        }
    ]


def _catalog_source(source: dict[str, Any]) -> list[dict[str, Any]]:
    index_path = Path(source["index_path"])
    source_format = str(source.get("format") or "markdown_table")
    if source_format == "semantic_v3":
        return _catalog_semantic_v3(index_path)
    if source_format == "legacy_manifest":
        return _catalog_legacy_manifest(index_path)
    if source_format == "jsonl":
        return _catalog_jsonl(index_path, str(source.get("family") or "LANE"))
    if source_format == "meta_markdown":
        return _catalog_meta(index_path, str(source.get("family") or "META"))
    return _catalog(index_path, str(source["family"]))


def _legacy_memory_sources(meta_path: Path) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = [
        {
            "family": "CE",
            "index_path": meta_path,
            "record_dir": meta_path.parent,
            "rule_fields": ["Prevention"],
        }
    ]
    # Unit/fixture callers that inject a temporary CE index remain isolated.
    # The production default adds the other registered memory families.
    if meta_path.resolve() != DEFAULT_COMMON_ERROR_META.resolve():
        return sources
    sources = [
        {
            "family": "META",
            "format": "meta_markdown",
            "index_path": DEFAULT_MATRIX_META,
        },
        {
            "family": "INC",
            "index_path": DEFAULT_MAJOR_INCIDENT_META,
            "payload_path": DEFAULT_MAJOR_INCIDENT_META,
            "heading_level": 2,
            "rule_fields": ["current_status", "causal_chain", "retrieval_rule"],
        },
        *sources,
    ]
    sources.extend(
        [
            {
                "family": "ERR",
                "index_path": DEFAULT_ERROR_SKILL,
                "payload_path": DEFAULT_ERROR_PAYLOAD,
                "heading_level": 2,
                "rule_fields": ["prevention_rule", "not_to_repeat"],
            },
            {
                "family": "SOL",
                "index_path": DEFAULT_SOLUTION_SKILL,
                "payload_path": DEFAULT_SOLUTION_PAYLOAD,
                "heading_level": 2,
                "rule_fields": ["future_reuse_rule", "solution_summary"],
            },
            {
                "family": "ANCHOR",
                "index_path": DEFAULT_ANCHOR_SKILL,
                "payload_path": DEFAULT_ANCHOR_PAYLOAD,
                "heading_level": 3,
                "rule_fields": ["anchored_meaning", "update_rule"],
            },
        ]
    )
    return sources


def _default_memory_sources(meta_path: Path) -> list[dict[str, Any]]:
    legacy_sources = _legacy_memory_sources(meta_path)
    if meta_path.resolve() != DEFAULT_COMMON_ERROR_META.resolve():
        return legacy_sources
    sources = [legacy_sources[0]]
    if DEFAULT_LEGACY_MEMORY_LINKS.is_file():
        sources.append(
            {
                "family": "LEGACY",
                "format": "legacy_manifest",
                "index_path": DEFAULT_LEGACY_MEMORY_LINKS,
            }
        )
    else:
        sources.extend(legacy_sources[1:])
    for store_root in DEFAULT_SEMANTIC_V3_STORES:
        meta_index = store_root / "meta.jsonl"
        if meta_index.is_file():
            sources.append(
                {
                    "family": "V3",
                    "format": "semantic_v3",
                    "index_path": meta_index,
                    "store_root": store_root,
                }
            )
    return sources


def _bare_foreach_pipeline(text: str) -> bool:
    return matches_behavior_pattern("powershell_statement_loop_direct_pipeline", text)


def _score_catalog_row(row: dict[str, Any], text: str, tool_text: str) -> tuple[int, list[str]]:
    haystack = f"{text}\n{tool_text}".casefold()
    score = 0
    reasons: list[str] = []
    record_id = str(row["record_id"])

    if record_id.casefold() in haystack:
        score += 100
        reasons.append("exact_record_id")

    if _bare_foreach_pipeline(tool_text) and (
        "FOREACH" in record_id or any("foreach" in term.casefold() for term in row["retrieval_terms"])
    ):
        score += 100
        reasons.append("generated_tool_matches_powershell_statement_loop_pipeline")

    if (
        record_id == "CE-POWERSHELL-FOREACH-PIPELINE-2026-07-10"
        and "foreach" in haystack
        and ("管道" in haystack or "pipeline" in haystack or "pipe" in haystack)
        and ("emptypipeelement" in haystack or "empty pipe element" in haystack)
    ):
        score += 100
        reasons.append("known_recurrence_foreach_pipeline_empty_pipe_element")

    for term in row["retrieval_terms"]:
        normalized = term.casefold()
        if normalized and normalized in haystack:
            score += 8
            reasons.append(f"retrieval_term:{term}")
            continue
        tokens = [token for token in re.split(r"[^\w\u4e00-\u9fff]+", normalized) if len(token) >= 4]
        token_hits = sum(1 for token in tokens if token in haystack)
        if token_hits:
            score += token_hits
            reasons.append(f"retrieval_tokens:{term}")

    if str(row.get("family")) == "INC":
        date_parts = _query_date_parts(haystack)
        event_date = str(row.get("event_date") or "")
        event_match = re.fullmatch(r"(20\d{2})-(\d{2})-(\d{2})", event_date)
        if event_match and date_parts["month"] is not None:
            event_year, event_month, event_day = (int(value) for value in event_match.groups())
            temporal_mismatch = (
                (date_parts["year"] is not None and date_parts["year"] != event_year)
                or date_parts["month"] != event_month
                or (date_parts["day"] is not None and date_parts["day"] != event_day)
            )
            if temporal_mismatch:
                score -= 5
                reasons.append("temporal_fragment_mismatch")
            elif date_parts["day"] is not None:
                score += 6
                reasons.append("temporal_exact_day")
            else:
                score += 3
                reasons.append("temporal_month_match")
        if re.search(r"重大(?:事件|问题)|历史(?:事件|事故)|事故|major\s+incident", haystack):
            score += 1
            reasons.append("incident_intent")
        if re.search(r"记得|回忆|那次|之前|上次|remember|recall", haystack):
            score += 1
            reasons.append("retrospective_intent")
        query_grams = _cjk_ngrams(haystack)
        facet_text = " ".join(
            [str(row.get("candidate_label") or ""), *[str(term) for term in row["retrieval_terms"]]]
        )
        overlap = len(query_grams & _cjk_ngrams(facet_text))
        if overlap:
            bounded_overlap = min(3, overlap)
            score += bounded_overlap
            reasons.append(f"cjk_facet_overlap:{bounded_overlap}")
    return score, reasons


def _incident_candidate_view(
    item: tuple[int, int, dict[str, Any], list[str], dict[str, Any]]
) -> dict[str, Any]:
    score, _negative_index, row, reasons, _source = item
    return {
        "record_id": str(row["record_id"]),
        "event_date": str(row.get("event_date") or "unknown"),
        "summary": str(row.get("candidate_label") or "、".join(row.get("retrieval_terms") or [])[:160]),
        "current_status": str(row.get("status") or "UNKNOWN"),
        "matched_facets": reasons,
        "score": score,
        "evidence_boundary": "navigation_candidate_only_not_fact_match",
    }


def _navigation_candidate(
    row: dict[str, Any],
    source: dict[str, Any],
    *,
    score: int,
    reasons: list[str],
) -> dict[str, Any]:
    """Build a meta-only weak candidate without opening its payload."""

    family = str(row.get("family") or source.get("family") or "UNKNOWN")
    status = str(row.get("status") or "UNKNOWN")
    return {
        "record_id": str(row["record_id"]),
        "family": family,
        "class": str(row.get("class") or "memory_record"),
        "status": status,
        "record_status": status,
        "candidate_label": str(row.get("candidate_label") or row["record_id"]),
        "solution_summary": str(row.get("solution_summary") or ""),
        "path": str(source.get("index_path") or row.get("index_path") or ""),
        "index_path": str(source.get("index_path") or row.get("index_path") or ""),
        "source_tag": "memory_meta_index",
        "belief_status": "navigation_only",
        "evidence_boundary": "navigation_candidate_only_payload_not_opened",
        "eligible_for_current_reuse": _is_currently_reusable(family, status),
        "score": score,
        "score_method": "field_weighted_exact_and_lexical_v1",
        "match_confidence": "weak",
        "selection_reasons": reasons,
        "payload_opened": False,
    }


def _extract_section(text: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^##\s+{re.escape(heading)}\s*$\s*(.*?)(?=^##\s+|\Z)",
        text,
    )
    if not match:
        return ""
    paragraphs = [line.strip() for line in match.group(1).splitlines() if line.strip()]
    return " ".join(paragraphs)


def _extract_record_section(text: str, record_id: str, heading_level: int) -> str:
    hashes = "#" * heading_level
    match = re.search(
        rf"(?ms)^{re.escape(hashes)}\s+{re.escape(record_id)}\s*$\s*"
        rf"(.*?)(?=^{re.escape(hashes)}\s+|\Z)",
        text,
    )
    return match.group(1) if match else ""


def _extract_labeled_field(text: str, field: str) -> str:
    match = re.search(
        rf"(?m)^{re.escape(field)}:\s*(.*?)\s*$",
        text,
    )
    if not match:
        return ""
    inline = match.group(1).strip().strip("`")
    if inline:
        return inline
    start = match.end()
    boundary = re.search(
        r"(?m)^[A-Za-z_][A-Za-z0-9_-]*:\s*(?:.*)?$|^#{2,}\s+",
        text[start:],
    )
    block = text[start : start + boundary.start()] if boundary else text[start:]
    lines = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        lines.append(re.sub(r"^-\s*", "", line).strip("`"))
    return " ".join(lines)


def _is_currently_reusable(family: str, status: str) -> bool:
    if family in {"ERR", "INC", "META"}:
        return False
    normalized = status.casefold()
    return not any(marker in normalized for marker in NONCURRENT_STATUS_MARKERS)


def _historical_review_requested(text: str) -> bool:
    normalized = text.casefold()
    explicit_phrases = (
        "历史回查",
        "历史追溯",
        "历史证据",
        "历史事故",
        "旧事故",
        "原始事件",
        "因果回查",
        "historical audit",
        "historical evidence",
        "historical incident",
        "superseded record",
        "provenance review",
    )
    if any(phrase in normalized for phrase in explicit_phrases):
        return True
    return re.search(r"\b(?:CE|ERR|SOL)-[A-Z0-9-]+\b", text, flags=re.IGNORECASE) is not None


def _materialize_record(row: dict[str, Any], source: dict[str, Any]) -> dict[str, Any] | None:
    record = _materialize_record_payload(row, source)
    if record is None:
        return None
    if record.get("family") == "ERR":
        record["rule"] = ""
        record["prevention"] = ""
        record["eligible_for_current_reuse"] = False
        record["reuse_policy"] = "diagnostic_only"
    elif record.get("family") in {"CE", "IE"} and not str(record.get("rule") or "").strip():
        record["eligible_for_current_reuse"] = False
        record["reuse_policy"] = "diagnostic_only"
    return record


def _materialize_record_payload(row: dict[str, Any], source: dict[str, Any]) -> dict[str, Any] | None:
    family = str(source["family"])
    source_format = str(source.get("format") or "markdown_table")
    if source_format == "semantic_v3":
        meta = row.get("semantic_meta")
        if not isinstance(meta, dict):
            return None
        store_root = Path(str(source.get("store_root") or Path(source["index_path"]).parent))
        record = materialize_memory_record(store_root, meta)
        content = record.get("content") if isinstance(record.get("content"), dict) else {}
        record_meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
        provenance = (
            record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
        )
        status = str(record.get("status", {}).get("state") or row.get("status") or "UNKNOWN")
        records_path = store_root / "records.jsonl"
        evidence_handles = [
            dict(item)
            for item in record.get("evidence_refs") or []
            if isinstance(item, dict)
        ]
        details = content.get("details") if isinstance(content.get("details"), dict) else {}
        rule = str(content.get("prevention_rule") or content.get("future_reuse_rule")
                   or details.get("prevention") or "")
        if str(row.get("family")) not in {"ERR", "CE", "IE"}:
            rule = rule or str(content.get("consumption_hint") or record_meta.get("summary")
                               or content.get("event_summary") or content.get("details") or "")
        return {
            **{key: value for key, value in row.items() if key != "semantic_meta"},
            "path": str(records_path),
            "source_id": source_id_for_path(records_path),
            "line": int(meta.get("payload", {}).get("line") or 0),
            "sha256": _sha256(records_path),
            "rule": rule,
            "prevention": rule,
            "record_status": str(record.get("status", {}).get("current_status") or status),
            "source_tag": provenance.get("source_tag"),
            "belief_status": provenance.get("belief_status"),
            "confidence": record.get("confidence"),
            "derived_from": record.get("evidence_refs") or None,
            "source_validity_dependency": provenance.get("source_monitoring"),
            "applicable_boundaries": content.get("applicable_boundaries"),
            "non_applicable_boundaries": content.get("non_applicable_boundaries"),
            "evidence_boundary": content.get("evidence_boundary"),
            "evidence_handles": evidence_handles,
            "eligible_for_current_reuse": _is_currently_reusable(str(row["family"]), status),
            "content_schema": record.get("schema"),
        }
    if source_format == "legacy_manifest":
        link = row.get("legacy_link")
        if not isinstance(link, dict):
            return None
        try:
            legacy = materialize_legacy_link(link)
        except (OSError, UnicodeError, ValueError):
            return None
        section = str(legacy.get("legacy_text") or "")
        family = str(row.get("family") or link.get("family") or "LEGACY")
        if family == "CE":
            rule = _extract_section(section, "Prevention")
        elif family == "IE":
            rule = " ".join(
                item
                for item in (
                    _extract_section(section, "Solution Applied"),
                    _extract_section(section, "Postcondition"),
                    _extract_section(section, "Fallback Boundary"),
                )
                if item
            )
        else:
            fields = {
                "ERR": ("prevention_rule", "not_to_repeat"),
                "SOL": ("future_reuse_rule", "solution_summary"),
                "ANCHOR": ("anchored_meaning", "update_rule"),
                "INC": ("current_status", "causal_chain", "retrieval_rule"),
            }.get(family, ("summary",))
            rule = " ".join(
                dict.fromkeys(
                    value
                    for value in (_extract_labeled_field(section, field) for field in fields)
                    if value
                )
            )
        path = Path(str(link.get("locator", {}).get("path") or ""))
        status = str(link.get("state") or row.get("status") or "UNKNOWN")
        evidence = _extract_labeled_field(section, "evidence") or _extract_labeled_field(
            section, "current_evidence"
        )
        def clean_legacy_scalar(field: str) -> str:
            value = _extract_labeled_field(section, field).strip()
            value = re.sub(r"^-\s*", "", value)
            return value.strip().strip("`")

        raw_evidence_source_id = clean_legacy_scalar("raw_evidence_source_id")
        raw_evidence_path = clean_legacy_scalar("raw_evidence_path")
        raw_evidence_line = clean_legacy_scalar("raw_evidence_line")
        raw_evidence_sha256_16 = clean_legacy_scalar("raw_evidence_sha256_16")
        evidence_handles: list[dict[str, Any]] = []
        if raw_evidence_source_id and raw_evidence_path:
            handle: dict[str, Any] = {
                "source_id": raw_evidence_source_id,
                "original_path": raw_evidence_path,
            }
            if raw_evidence_line.isdigit():
                handle["line"] = int(raw_evidence_line)
            if raw_evidence_sha256_16:
                handle["line_sha256_16"] = raw_evidence_sha256_16
            evidence_handles.append(handle)
        return {
            **{key: value for key, value in row.items() if key != "legacy_link"},
            "path": str(path),
            "source_id": source_id_for_path(path),
            "line": int(row.get("jsonl_line") or 0),
            "sha256": str(link.get("locator", {}).get("file_sha256") or ""),
            "rule": rule,
            "prevention": rule,
            "record_status": status,
            "source_tag": "legacy_memory_link",
            "belief_status": "legacy_status_only",
            "confidence": {
                "level": "bounded",
                "basis": "exact legacy heading and file hash verified",
            },
            "derived_from": evidence or legacy.get("derived_from"),
            "source_validity_dependency": None,
            "applicable_boundaries": _extract_labeled_field(section, "applicable_boundaries") or None,
            "non_applicable_boundaries": _extract_labeled_field(
                section, "non_applicable_boundaries"
            )
            or None,
            "evidence_boundary": legacy.get("evidence_boundary"),
            "evidence_handles": evidence_handles,
            "eligible_for_current_reuse": _is_currently_reusable(family, status),
            "content_schema": link.get("content_schema"),
        }
    if source_format == "jsonl":
        path = Path(source["index_path"])
        record = row.get("json_record")
        if not isinstance(record, dict):
            return None
        status = str(record.get("current_status") or record.get("status") or record.get("lifecycle") or row.get("status") or "UNKNOWN")
        rule = str(
            record.get("prevention_rule")
            or record.get("future_reuse_rule")
            or record.get("decision")
            or record.get("summary")
            or ""
        )
        return {
            **{key: value for key, value in row.items() if key != "json_record"},
            "path": str(path),
            "source_id": source_id_for_path(path),
            "line": int(row.get("jsonl_line") or 0),
            "sha256": _sha256(path),
            "rule": rule,
            "prevention": rule,
            "record_status": status,
            "source_tag": record.get("source_tag"),
            "belief_status": record.get("belief_status"),
            "confidence": record.get("confidence"),
            "derived_from": record.get("derived_from") or record.get("evidence_refs"),
            "source_validity_dependency": record.get("source_validity_dependency"),
            "applicable_boundaries": record.get("applicable_boundaries"),
            "non_applicable_boundaries": record.get("non_applicable_boundaries"),
            "evidence_boundary": record.get("evidence_boundary"),
            "eligible_for_current_reuse": _is_currently_reusable(family, status),
        }
    if source_format == "meta_markdown":
        path = Path(source["index_path"])
        return {
            **{key: value for key, value in row.items() if key != "meta_text"},
            "path": str(path),
            "source_id": source_id_for_path(path),
            "sha256": _sha256(path),
            "rule": "",
            "prevention": "",
            "record_status": "NAVIGATION_ONLY",
            "source_tag": "memory_meta_index",
            "belief_status": "navigation_only",
            "confidence": {"level": "none", "basis": "meta index is not fact evidence"},
            "derived_from": None,
            "source_validity_dependency": None,
            "applicable_boundaries": None,
            "non_applicable_boundaries": None,
            "evidence_boundary": "navigation_only_open_linked_payload_for_facts",
            "eligible_for_current_reuse": False,
        }
    if family in {"CE", "IE"}:
        path = Path(source.get("record_dir") or Path(source["index_path"]).parent) / f"{row['record_id']}.md"
        if not path.is_file():
            return None
        payload = path.read_text(encoding="utf-8-sig")
        rule = (
            _extract_section(payload, "Prevention")
            if family == "CE"
            else " ".join(
                item
                for item in (
                    _extract_section(payload, "Solution Applied"),
                    _extract_section(payload, "Postcondition"),
                    _extract_section(payload, "Fallback Boundary"),
                )
                if item
            )
        )
        section = payload
    else:
        path = Path(source["payload_path"])
        if not path.is_file():
            return None
        payload = path.read_text(encoding="utf-8-sig")
        section = _extract_record_section(
            payload,
            str(row["record_id"]),
            int(source.get("heading_level") or 2),
        )
        if not section:
            return None
        rules = [
            _extract_labeled_field(section, str(field))
            for field in source.get("rule_fields", [])
        ]
        rule = " ".join(dict.fromkeys(item for item in rules if item))
    status = _extract_labeled_field(section, "current_status") or _extract_labeled_field(section, "status") or str(row.get("status") or "UNKNOWN")
    source_tag = _extract_labeled_field(section, "source_tag")
    belief_status = _extract_labeled_field(section, "belief_status")
    source_validity_dependency = _extract_labeled_field(section, "source_validity_dependency")
    applicable_boundaries = _extract_labeled_field(section, "applicable_boundaries")
    non_applicable_boundaries = _extract_labeled_field(section, "non_applicable_boundaries")
    evidence = _extract_labeled_field(section, "evidence") or _extract_labeled_field(section, "current_evidence")
    def clean_evidence_scalar(field: str) -> str:
        value = _extract_labeled_field(section, field).strip()
        value = re.sub(r"^-\s*", "", value)
        return value.strip().strip("`")

    raw_evidence_source_id = clean_evidence_scalar("raw_evidence_source_id")
    raw_evidence_path = clean_evidence_scalar("raw_evidence_path")
    raw_evidence_line = clean_evidence_scalar("raw_evidence_line")
    raw_evidence_sha256_16 = clean_evidence_scalar("raw_evidence_sha256_16")
    evidence_handles: list[dict[str, Any]] = []
    if raw_evidence_source_id and raw_evidence_path:
        handle: dict[str, Any] = {
            "source_id": raw_evidence_source_id,
            "original_path": raw_evidence_path,
        }
        if raw_evidence_line.isdigit():
            handle["line"] = int(raw_evidence_line)
        if raw_evidence_sha256_16:
            handle["line_sha256_16"] = raw_evidence_sha256_16
        evidence_handles.append(handle)
    materialized = {
        **row,
        "path": str(path),
        "source_id": source_id_for_path(path),
        "sha256": _sha256(path),
        "rule": rule,
        # Backward-compatible key for CE-era callers.
        "prevention": rule,
        "record_status": status,
        "source_tag": source_tag or "legacy_memory_point",
        "belief_status": belief_status or "legacy_status_only",
        "confidence": {
            "level": "bounded" if belief_status else "unknown",
            "basis": belief_status or "legacy record has no structured confidence",
        },
        "derived_from": evidence or None,
        "source_validity_dependency": source_validity_dependency or None,
        "applicable_boundaries": applicable_boundaries or None,
        "non_applicable_boundaries": non_applicable_boundaries or None,
        "evidence_boundary": evidence or None,
        "evidence_handles": evidence_handles,
        "eligible_for_current_reuse": _is_currently_reusable(family, status),
    }
    if family == "IE":
        materialized.update(
            {
                "interaction_surface": _extract_labeled_field(section, "interaction_surface"),
                "target_app": _extract_labeled_field(section, "target_app"),
                "postcondition": _extract_section(section, "Postcondition"),
                "fallback_boundary": _extract_section(section, "Fallback Boundary"),
                "risk_level": _extract_labeled_field(section, "risk_level"),
            }
        )
    return materialized


def select_memory_records(
    text: str,
    tool_text: str,
    *,
    memory_sources: list[dict[str, Any]],
    soft_target_records: int = SOFT_TARGET_RECORDS,
    expand_linked_pairs: bool = False,
    query_type: str = "history_reason",
) -> dict[str, Any]:
    if query_type not in MEMORY_QUERY_TYPES:
        raise ValueError("unsupported_memory_query_type")
    soft_target_records = max(1, int(soft_target_records))
    historical_review_requested = _historical_review_requested(f"{text}\n{tool_text}")
    candidates: list[tuple[int, int, dict[str, Any], list[str], dict[str, Any]]] = []
    ordinal = 0
    source_indexes: list[str] = []
    indexed_records: dict[str, tuple[dict[str, Any], dict[str, Any], int]] = {}
    for source in memory_sources:
        index_path = Path(source["index_path"])
        source_indexes.append(str(index_path))
        if not index_path.is_file():
            continue
        for row in _catalog_source(source):
            if str(row.get("family")) == "ERR" and query_type == "current_state":
                ordinal += 1
                continue
            declared_query_types = row.get("query_types")
            if (
                isinstance(declared_query_types, list)
                and declared_query_types
                and query_type not in declared_query_types
            ):
                ordinal += 1
                continue
            indexed_records[str(row["record_id"])] = (row, source, ordinal)
            score, reasons = _score_catalog_row(row, text, tool_text)
            if score > 0:
                candidates.append((score, -ordinal, row, reasons, source))
            ordinal += 1
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)

    direct = [item for item in candidates if item[0] >= DIRECT_MATCH_SCORE]
    weak = [item for item in candidates if item[0] < DIRECT_MATCH_SCORE]
    incident_candidates = [item for item in candidates if str(item[2].get("family")) == "INC"]
    incident_direct = [item for item in direct if str(item[2].get("family")) == "INC"]
    exact_direct = [item for item in direct if "exact_record_id" in item[3]]
    strong_incident_direct = [
        item
        for item in incident_direct
        if (
            "exact_record_id" in item[3]
            or "temporal_exact_day" in item[3]
            or sum(1 for reason in item[3] if reason.startswith("retrieval_term:")) >= 2
        )
    ]
    major_incident_routed = len(strong_incident_direct) == 1
    date_parts = _query_date_parts(f"{text}\n{tool_text}")
    incident_disambiguation_requested = not exact_direct and bool(incident_candidates) and (
        date_parts["month"] is not None
        or bool(
            re.search(
                r"重大(?:事件|问题)|历史(?:事件|事故)|事故|那次客户端|major\s+incident",
                f"{text}\n{tool_text}",
                flags=re.IGNORECASE,
            )
        )
    )
    disambiguation_candidates = (
        []
        if major_incident_routed
        else [
            _incident_candidate_view(item)
            for item in incident_candidates[:INCIDENT_DISAMBIGUATION_LIMIT]
        ]
        if incident_disambiguation_requested
        else []
    )
    nonincident_direct = [item for item in direct if str(item[2].get("family")) != "INC"]
    nonincident_weak = [item for item in weak if str(item[2].get("family")) != "INC"]
    chosen = (
        list(exact_direct)
        if exact_direct
        else list(strong_incident_direct)
        if major_incident_routed
        else list(nonincident_direct)
        if nonincident_direct
        else []
    )
    paired_expansion_ids: list[str] = []
    expand_pairs_for_incident = major_incident_routed
    if (expand_linked_pairs or expand_pairs_for_incident) and direct:
        chosen_ids = {str(item[2]["record_id"]) for item in chosen}
        queue = list(chosen)
        while queue:
            _score, _negative_index, row, _reasons, _source = queue.pop(0)
            for linked_id in row.get("linked_record_ids", []):
                if linked_id in chosen_ids or linked_id not in indexed_records:
                    continue
                linked_row, linked_source, linked_ordinal = indexed_records[linked_id]
                linked_item = (
                    DIRECT_MATCH_SCORE,
                    -linked_ordinal,
                    linked_row,
                    [f"paired_with:{row['record_id']}"],
                    linked_source,
                )
                chosen.append(linked_item)
                queue.append(linked_item)
                chosen_ids.add(linked_id)
                paired_expansion_ids.append(linked_id)

    selected: list[dict[str, Any]] = []
    unresolved_ids: list[str] = []
    for score, _negative_index, row, reasons, source in chosen:
        materialized = _materialize_record(row, source)
        if materialized is None:
            unresolved_ids.append(str(row["record_id"]))
            continue
        selected.append(
            {
                **materialized,
                "score": score,
                "score_method": "field_weighted_exact_and_lexical_v1",
                "match_confidence": (
                    "linked_pair"
                    if any(reason.startswith("paired_with:") for reason in reasons)
                    else ("direct" if score >= DIRECT_MATCH_SCORE else "weak")
                ),
                "selection_reasons": reasons,
            }
        )

    selected_ids = {str(item["record_id"]) for item in selected}
    semantic_review_candidates: list[dict[str, Any]] = []
    visible_weak_candidate_ids: list[str] = []
    if not major_incident_routed and not exact_direct:
        for score, _negative_index, row, reasons, source in weak:
            if str(row.get("family")) == "INC":
                continue
            materialized = _navigation_candidate(
                row,
                source,
                score=score,
                reasons=reasons,
            )
            if (
                not bool(materialized.get("eligible_for_current_reuse", True))
                and str(materialized.get("family")) not in {"INC", "META"}
                and not historical_review_requested
            ):
                continue
            visible_weak_candidate_ids.append(str(row["record_id"]))
            if str(row["record_id"]) in selected_ids:
                continue
            semantic_review_candidates.append(materialized)
            if len(semantic_review_candidates) >= SEMANTIC_REVIEW_CANDIDATE_LIMIT:
                break

    weak_candidate_ids = [str(item[2]["record_id"]) for item in weak]
    # A direct hit proves only that record's facet. Until indexes expose a
    # reliable cross-family facet key, keep every weaker material candidate
    # visible for semantic review instead of declaring whole-task coverage.
    review_candidate_ids = (
        []
        if major_incident_routed or exact_direct
        else list(
            dict.fromkeys(
                [item["record_id"] for item in disambiguation_candidates]
                + visible_weak_candidate_ids
            )
        )
    )
    deferred_weak_candidate_ids = (
        weak_candidate_ids if major_incident_routed or exact_direct else []
    )
    if unresolved_ids:
        coverage_status = "incomplete_missing_payload"
    elif review_candidate_ids:
        coverage_status = "semantic_review_required"
    elif selected:
        coverage_status = "complete"
    else:
        coverage_status = "no_match"
    return {
        "selected_records": selected,
        "semantic_review_candidates": semantic_review_candidates,
        "soft_target_records": soft_target_records,
        "direct_candidate_count": len(direct),
        "weak_candidate_count": len(weak),
        "expanded_for_direct_coverage": len(chosen) > soft_target_records,
        "paired_expansion_ids": paired_expansion_ids,
        "major_incident_routed": major_incident_routed,
        "coverage_status": coverage_status,
        "review_candidate_ids": review_candidate_ids,
        "deferred_weak_candidate_ids": deferred_weak_candidate_ids,
        "disambiguation_candidates": disambiguation_candidates,
        "disambiguation_required": bool(disambiguation_candidates),
        "unresolved_record_ids": unresolved_ids,
        "source_indexes": source_indexes,
        "query_type": query_type,
    }


def select_common_errors(
    text: str,
    tool_text: str,
    *,
    meta_path: Path = DEFAULT_COMMON_ERROR_META,
    max_records: int = 2,
) -> list[dict[str, Any]]:
    if max_records < 1:
        return []
    result = select_memory_records(
        text,
        tool_text,
        memory_sources=_default_memory_sources(meta_path)[:1],
        soft_target_records=max_records,
    )
    return list(result["selected_records"])


def _route_field(route: dict[str, Any], name: str, default: Any = "none") -> Any:
    receipt = route.get("routing_receipt")
    if isinstance(receipt, dict) and name in receipt:
        return receipt[name]
    compact = route.get("compact_receipt")
    if isinstance(compact, dict) and name in compact:
        return compact[name]
    return route.get(name, default)


def _normalized_confirmation_request(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    scalar_fields = (
        "schema",
        "action",
        "target_binding",
        "scope",
        "impact",
        "recovery",
        "persistence",
    )
    request: dict[str, Any] = {
        key: str(value[key])
        for key in scalar_fields
        if isinstance(value.get(key), str) and value[key]
    }
    for key in ("target", "non_targets", "required_disclosures"):
        items = value.get(key)
        if isinstance(items, list):
            request[key] = [str(item) for item in items if str(item)]
    required = {"action", "target", "scope", "impact", "recovery", "non_targets"}
    if not required.issubset(request):
        return None
    request["persistence"] = "none"
    return request


def _bindings(route: dict[str, Any]) -> list[dict[str, Any]]:
    value = _route_field(route, "action_bindings", [])
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict) and item.get("action"):
            binding: dict[str, Any] = {
                "action": str(item["action"]),
                "completion_evidence": str(item.get("completion_evidence") or "unspecified"),
            }
            confirmation_request = _normalized_confirmation_request(
                item.get("confirmation_request")
            )
            if confirmation_request is not None:
                binding["confirmation_request"] = confirmation_request
            result.append(binding)
    return result


def _route_string_list(route: dict[str, Any], name: str) -> list[str]:
    value = _route_field(route, name, [])
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _route_object_list(route: dict[str, Any], name: str) -> list[dict[str, Any]]:
    value = _route_field(route, name, [])
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _is_path_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _dynamic_memory_sources(route: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for hint in _route_object_list(route, "memory_source_hints"):
        raw_root = str(hint.get("root_path") or "")
        if not raw_root:
            rejected.append({"root_path": raw_root, "reason": "missing_root_path"})
            continue
        root = Path(raw_root)
        if not root.is_dir():
            rejected.append({"root_path": raw_root, "reason": "root_not_found"})
            continue
        meta_path = Path(str(hint.get("meta_path") or root / "_META_INDEX.md"))
        if not _is_path_inside(meta_path, root):
            rejected.append({"root_path": raw_root, "reason": "meta_outside_root"})
            continue
        root_sources: list[str] = []
        if meta_path.is_file():
            key = str(meta_path.resolve()).casefold()
            if key not in seen_paths:
                sources.append(
                    {
                        "family": "META",
                        "format": "meta_markdown",
                        "index_path": meta_path,
                    }
                )
                seen_paths.add(key)
                root_sources.append(str(meta_path))

        semantic_store_path = root / "store.json"
        semantic_meta_path = root / "meta.jsonl"
        semantic_store_rejected = False
        if semantic_store_path.is_file() and semantic_meta_path.is_file():
            try:
                store_manifest = json.loads(
                    semantic_store_path.read_text(encoding="utf-8", errors="strict")
                )
                if (
                    not isinstance(store_manifest, dict)
                    or store_manifest.get("schema") != SEMANTIC_MEMORY_STORE_SCHEMA
                    or not str(store_manifest.get("lane_id") or "").strip()
                    or store_manifest.get("lane_kind") not in {"global", "project", "conversation"}
                    or store_manifest.get("records") != "records.jsonl"
                    or store_manifest.get("meta") != "meta.jsonl"
                ):
                    raise ValueError("invalid_semantic_memory_store_manifest")
                expected_lane_id = str(hint.get("lane_id") or "").strip()
                expected_lane_kind = str(hint.get("lane_kind") or "").strip()
                if expected_lane_id and expected_lane_id != store_manifest.get("lane_id"):
                    raise ValueError("semantic_memory_store_lane_id_mismatch")
                if expected_lane_kind and expected_lane_kind != store_manifest.get("lane_kind"):
                    raise ValueError("semantic_memory_store_lane_kind_mismatch")
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                rejected.append(
                    {
                        "root_path": raw_root,
                        "reason": f"invalid_semantic_memory_store_manifest:{exc}",
                    }
                )
                semantic_store_rejected = True
            if not semantic_store_rejected:
                key = str(semantic_meta_path.resolve()).casefold()
                if key not in seen_paths:
                    sources.append(
                        {
                            "family": "V3",
                            "format": "semantic_v3",
                            "index_path": semantic_meta_path,
                            "store_root": root,
                            "store_manifest": store_manifest,
                        }
                    )
                    seen_paths.add(key)
                    root_sources.append(str(semantic_meta_path))

        index_json_path = root / "index.json"
        if index_json_path.is_file():
            index = json.loads(index_json_path.read_text(encoding="utf-8-sig"))
            families = index.get("record_families")
            if isinstance(families, dict):
                for family_name in (
                    "decisions",
                    "open_loops",
                    "errors_and_solutions",
                    "memory_links",
                    "references",
                ):
                    relative = families.get(family_name)
                    if not isinstance(relative, str) or not relative:
                        continue
                    path = root / relative
                    if not _is_path_inside(path, root) or not path.is_file():
                        continue
                    key = str(path.resolve()).casefold()
                    if key in seen_paths:
                        continue
                    sources.append(
                        {
                            "family": "LANE",
                            "format": "jsonl",
                            "index_path": path,
                        }
                    )
                    seen_paths.add(key)
                    root_sources.append(str(path))
        if root_sources:
            accepted.append(
                {
                    "lane": str(hint.get("lane") or "unknown"),
                    "root_path": str(root.resolve()),
                    "isolation": str(hint.get("isolation") or "route_declared"),
                    "source_paths": root_sources,
                }
            )
        elif not semantic_store_rejected:
            rejected.append({"root_path": raw_root, "reason": "no_meta_or_indexed_family"})
    return sources, {
        "status": "accepted" if accepted and not rejected else "partial" if accepted else "none",
        "accepted": accepted,
        "rejected": rejected,
    }


def _read_navigation_document(
    path: Path,
    *,
    root: Path,
    kind: str,
    query_terms: list[str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not _is_path_inside(path, root):
        return None, {"kind": kind, "path": str(path), "reason": "path_outside_declared_root"}
    if not path.is_file():
        return None, {"kind": kind, "path": str(path), "reason": "file_not_found"}
    try:
        text = path.read_text(encoding="utf-8-sig", errors="strict")
    except (OSError, UnicodeError):
        return None, {"kind": kind, "path": str(path), "reason": "utf8_read_failed"}
    if "\ufffd" in text:
        return None, {"kind": kind, "path": str(path), "reason": "replacement_character_present"}
    lines = text.splitlines()
    if len(text) <= 5000:
        excerpt = text
        excerpt_mode = "full"
    else:
        selected_indexes = set(range(min(28, len(lines))))
        normalized_terms = [term.casefold() for term in query_terms if len(term.strip()) >= 2]
        for index, line in enumerate(lines):
            normalized_line = line.casefold()
            if any(term in normalized_line for term in normalized_terms):
                selected_indexes.update(range(max(0, index - 1), min(len(lines), index + 2)))
        excerpt_lines: list[str] = []
        excerpt_chars = 0
        for index in sorted(selected_indexes):
            line = lines[index]
            if excerpt_lines and excerpt_chars + len(line) + 1 > 4000:
                break
            excerpt_lines.append(line)
            excerpt_chars += len(line) + 1
        excerpt = "\n".join(excerpt_lines)
        excerpt_mode = "identity_plus_query_matches"
    return (
        {
            "kind": kind,
            "path": str(path.resolve()),
            "sha256": _sha256(path),
            "excerpt": excerpt,
            "excerpt_mode": excerpt_mode,
            "source_char_count": len(text),
            "excerpt_char_count": len(excerpt),
            "omitted_char_count": max(0, len(text) - len(excerpt)),
            "full_source_available_at_path": True,
            "source_tag": "conversation_navigation",
            "belief_status": "navigation_only",
        },
        None,
    )


def _ledger_snapshot_status(ledger_root: Path) -> dict[str, Any]:
    sessions_path = ledger_root / "sessions.jsonl"
    if not sessions_path.is_file():
        return {"status": "unknown", "reason": "sessions_index_missing"}
    try:
        records = [
            json.loads(line)
            for line in sessions_path.read_text(encoding="utf-8-sig", errors="strict").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"status": "unknown", "reason": "sessions_index_unreadable"}
    if not records:
        return {"status": "unknown", "reason": "sessions_index_empty"}
    record = records[-1]
    raw_path = Path(str(record.get("raw_session_path") or ""))
    recorded_size = record.get("raw_size_bytes")
    if not raw_path.is_file() or not isinstance(recorded_size, int):
        return {
            "status": "unknown",
            "reason": "raw_session_or_recorded_stat_missing",
            "raw_session_path": str(raw_path),
        }
    current_size = raw_path.stat().st_size
    return {
        "status": "current_snapshot" if current_size == recorded_size else "stale_readonly",
        "raw_session_path": str(raw_path.resolve()),
        "recorded_size_bytes": recorded_size,
        "current_size_bytes": current_size,
        "rule": "Stale ledgers remain read-only navigation; this consumer never refreshes or writes them.",
    }


def _conversation_navigation_bundle(route: dict[str, Any], prompt: str) -> dict[str, Any]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    matched = _route_field(route, "matched_risk_triggers", {})
    registry_terms: list[str] = []
    if isinstance(matched, dict):
        values = matched.get("referenced_conversation_registry", [])
        if isinstance(values, list):
            registry_terms = [str(item) for item in values if str(item)]
    prompt_terms = [
        token
        for token in re.split(r"[^\w\u4e00-\u9fff]+", prompt)
        if len(token) >= 3
    ]
    query_terms = list(dict.fromkeys([*registry_terms, *prompt_terms]))
    for hint in _route_object_list(route, "memory_source_hints"):
        if str(hint.get("navigation_profile") or "") != "meta_state_links_ledger_one_hop":
            continue
        root = Path(str(hint.get("root_path") or ""))
        if not root.is_dir():
            rejected.append({"root_path": str(root), "reason": "root_not_found"})
            continue
        documents: list[dict[str, Any]] = []
        document_errors: list[dict[str, Any]] = []
        index_path = root / "index.json"
        try:
            index = json.loads(index_path.read_text(encoding="utf-8-sig", errors="strict"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            rejected.append({"root_path": str(root), "reason": "lane_index_unreadable"})
            continue
        memory_id = str(hint.get("memory_id") or index.get("memory_id") or index.get("lane") or "")
        current_paths = [
            (Path(str(hint.get("meta_path") or root / "_META_INDEX.md")), "lane_meta", root),
            (root / "conversation_state.md", "conversation_state", root),
            (root / "memory_links.jsonl", "memory_links", root),
        ]
        for path, kind, declared_root in current_paths:
            document, error = _read_navigation_document(
                path,
                root=declared_root,
                kind=kind,
                query_terms=query_terms,
            )
            if document is not None:
                documents.append(document)
            elif error is not None:
                document_errors.append(error)

        ledger_root_text = str(hint.get("ledger_root_path") or "")
        if not ledger_root_text:
            ledger_info = index.get("conversation_ledger")
            if isinstance(ledger_info, dict):
                ledger_root_text = str(ledger_info.get("path") or "")
        ledger_root: Path | None = None
        ledger_index: Path | None = None
        ledger_snapshot: dict[str, Any] = {"status": "unknown", "reason": "ledger_root_missing"}
        if ledger_root_text:
            declared_ledger_root = Path(ledger_root_text)
            try:
                ledger_root = load_active_generation(declared_ledger_root)
            except (OSError, ValueError):
                ledger_root = declared_ledger_root
            if ledger_root != declared_ledger_root.resolve():
                ledger_index = ledger_root / "_LEDGER_INDEX.md"
            else:
                ledger_index = Path(
                    str(hint.get("ledger_index_path") or ledger_root / "_LEDGER_INDEX.md")
                )
            ledger_document, ledger_error = _read_navigation_document(
                ledger_index,
                root=ledger_root,
                kind="ledger_index",
                query_terms=query_terms,
            )
            if ledger_document is not None:
                documents.append(ledger_document)
            elif ledger_error is not None:
                document_errors.append(ledger_error)
            ledger_snapshot = _ledger_snapshot_status(ledger_root)
        else:
            document_errors.append(
                {"kind": "ledger_index", "path": "", "reason": "ledger_root_missing"}
            )

        selected_links: list[dict[str, Any]] = []
        links_path = root / "memory_links.jsonl"
        if links_path.is_file():
            try:
                links = [
                    json.loads(line)
                    for line in links_path.read_text(encoding="utf-8-sig", errors="strict").splitlines()
                    if line.strip()
                ]
            except (OSError, UnicodeError, json.JSONDecodeError):
                links = []
                document_errors.append(
                    {"kind": "memory_links", "path": str(links_path), "reason": "link_parse_failed"}
                )
            candidates: list[tuple[int, dict[str, Any], str, str]] = []
            for link in links:
                if str(link.get("status") or "ACTIVE").upper() != "ACTIVE":
                    continue
                if str(link.get("link_type") or "") not in {"continuation", "reference"}:
                    continue
                if memory_id and str(link.get("to_memory_id") or "") == memory_id:
                    candidates.append(
                        (0, link, str(link.get("from_path") or ""), str(link.get("from_ledger_path") or ""))
                    )
                elif memory_id and str(link.get("from_memory_id") or "") == memory_id:
                    candidates.append(
                        (1, link, str(link.get("to_path") or ""), str(link.get("to_ledger_path") or ""))
                    )
            if candidates:
                _, link, linked_root_text, linked_ledger_root_text = sorted(candidates, key=lambda item: item[0])[0]
                selected_links.append(
                    {
                        "link_id": str(link.get("link_id") or ""),
                        "link_type": str(link.get("link_type") or ""),
                        "from_memory_id": str(link.get("from_memory_id") or ""),
                        "to_memory_id": str(link.get("to_memory_id") or ""),
                        "write_policy": str(link.get("write_policy") or ""),
                        "evidence_boundary": str(link.get("evidence_boundary") or ""),
                    }
                )
                linked_root = Path(linked_root_text)
                if linked_root_text and linked_root.is_dir():
                    document, error = _read_navigation_document(
                        linked_root / "_META_INDEX.md",
                        root=linked_root,
                        kind="linked_lane_meta",
                        query_terms=query_terms,
                    )
                    if document is not None:
                        documents.append(document)
                    elif error is not None:
                        document_errors.append(error)
                linked_ledger_root = Path(linked_ledger_root_text)
                if linked_ledger_root_text and linked_ledger_root.is_dir():
                    document, error = _read_navigation_document(
                        linked_ledger_root / "_LEDGER_INDEX.md",
                        root=linked_ledger_root,
                        kind="linked_ledger_index",
                        query_terms=query_terms,
                    )
                    if document is not None:
                        documents.append(document)
                    elif error is not None:
                        document_errors.append(error)

        accepted.append(
            {
                "memory_id": memory_id,
                "root_path": str(root.resolve()),
                "registry_path": str(hint.get("registry_path") or ""),
                "isolation": str(hint.get("isolation") or "route_declared"),
                "documents": documents,
                "document_errors": document_errors,
                "selected_links": selected_links,
                "ledger": {
                    "root_path": (
                        str(ledger_root.resolve())
                        if ledger_root is not None and ledger_root.is_dir()
                        else str(ledger_root or "")
                    ),
                    "index_path": str(ledger_index or ""),
                    "capsules_path": str(ledger_root / "capsules.jsonl") if ledger_root else "",
                    "evidence_refs_path": (
                        str(ledger_root / "evidence_refs.jsonl") if ledger_root else ""
                    ),
                    "snapshot": ledger_snapshot,
                },
            }
        )
    if accepted:
        status = "resolved" if not rejected and all(not item["document_errors"] for item in accepted) else "partial"
    else:
        status = "not_requested" if not rejected else "unresolved"
    return {
        "status": status,
        "bundles": accepted,
        "rejected": rejected,
        "write_performed": False,
        "raw_payload_opened": False,
        "rule": "Read-only meta/state/link/ledger navigation; one explicit continuation hop maximum.",
    }


def _reflexive_gap_review(route: dict[str, Any]) -> dict[str, Any] | None:
    required_gates = set(_route_string_list(route, "required_gates"))
    if "reflexive_gap_gate" not in required_gates:
        return None
    matched = _route_field(route, "matched_risk_triggers", {})
    raw_sources: list[Any] = []
    if isinstance(matched, dict):
        value = matched.get("reflexive_gap_gate", [])
        raw_sources = value if isinstance(value, list) else [value]
    trigger_sources = list(dict.fromkeys(str(item) for item in raw_sources if str(item)))
    return {
        "schema": "cbh.reflexive_gap_probe.v1",
        "status": "candidate_review_required",
        "trigger_sources": trigger_sources,
        "candidate_classes": [
            "knowledge_coverage_gap",
            "knowledge_activation_gap",
            "goal_fidelity_gap",
            "environment_or_interface_gap",
            "goal_changed",
            "insufficient_evidence",
            "no_gap",
        ],
        "required_receipt_fields": [
            "gap_class",
            "counterevidence",
            "next_action_binding",
            "verifier",
            "retire_condition",
        ],
        "depth": "shallow_then_skill_if_unresolved",
        "standing_constraint": False,
        "memory_write": "not_authorized",
    }


def _compose_reasoning_activation(route: dict[str, Any]) -> dict[str, Any]:
    """Compose existing CBH signals into one bounded, task-local reasoning cue.

    This is deliberately an orchestration receipt, not a new standing rule and
    not a claim that a hidden reasoning process was completed. It prevents the
    first-principles lens from becoming the sole frame by pairing it with the
    concrete case, a counterfactual, and a verifier choice.
    """

    first_principles = str(_route_field(route, "first_principles_profile", "none"))
    feedback = str(_route_field(route, "feedback_loop_profile", "none"))
    classification_confidence = str(
        _route_field(route, "classification_confidence", "high")
    )
    required_gates = set(_route_string_list(route, "required_gates"))
    semantic_boundaries = set(_route_string_list(route, "read_semantic_boundary"))

    lenses: list[str] = ["concrete_case_evidence"]
    reasons: list[str] = []
    if first_principles not in {"", "none"}:
        lenses.append("first_principles_constraints")
        reasons.append(f"first_principles_profile:{first_principles}")
    if "global_task_context_gate" in required_gates:
        lenses.append("global_system_context")
        reasons.append("required_gate:global_task_context_gate")
    if "causal_scope" in semantic_boundaries:
        lenses.append("causal_relationships")
        reasons.append("read_semantic_boundary:causal_scope")
    if feedback in {"prevention_review", "explicit_cycle"}:
        lenses.append("prediction_observation")
        reasons.append(f"feedback_loop_profile:{feedback}")
    if classification_confidence == "low":
        lenses.append("uncertainty_boundary")
        reasons.append("classification_confidence:low")
    if "reflexive_gap_gate" in required_gates:
        lenses.append("reflexive_gap_classification")
        reasons.append("required_gate:reflexive_gap_gate")

    if not reasons:
        return {
            "status": "not_requested",
            "profile": "none",
            "lenses": [],
            "activation_reasons": [],
            "scope": "consumer_task_local",
            "standing_constraint": False,
            "context": "",
        }

    lenses.extend(["counterfactual_alternative", "verifier_selection"])
    independent_signals = len(reasons)
    if "reflexive_gap_classification" in lenses and independent_signals == 1:
        profile = "reflexive_gap_probe"
        context = (
            "CBH 任务局部自反缺口探针（非答案、非长驻约束）：只核对当前目标、证据、"
            "候选动作与可证伪验证器；没有证据支持缺口时立即退役。"
        )
    elif first_principles == "micro_constraints" and independent_signals == 1:
        profile = "micro_review"
        context = (
            "CBH 任务局部轻量复核（非答案、非长驻约束）：以当前具体事实和目标为基线，"
            "检查不可违反约束与一个反例；能机械验证时先验证拟执行内容。"
        )
    elif feedback == "explicit_cycle" and (
        "causal_relationships" in lenses or "global_system_context" in lenses
    ):
        profile = "causal_prediction_cycle"
        context = (
            "CBH 任务局部多视角推理激活（非答案、非长驻约束，无需外显思维过程）："
            "先以当前目标和具体事实为基线；同时检查不可违反约束、全局依赖与下游影响、"
            "因果关系和预测—观察闭环；至少检验一个替代假设或反例，并优先选择可证伪或"
            "机械验证。各视角冲突时保留不确定性。能力完整和目标满足优先，采用最小充分"
            "干预，不把最小改动本身当成目的。"
        )
    elif independent_signals >= 2:
        profile = "multi_lens_review"
        context = (
            "CBH 任务局部多视角复核（非答案、非长驻约束）：以当前目标和具体事实为基线；"
            "综合已选约束、全局或因果视角，检查一个替代假设，并选择可证伪或机械验证。"
            "视角冲突时保留不确定性；能力完整和目标满足优先。"
        )
    else:
        profile = "focused_review"
        context = (
            "CBH 任务局部聚焦复核（非答案、非长驻约束）：先核对当前目标和具体事实，"
            "再检查不可违反约束、一个替代假设及可证伪或机械验证；不要让单一第一性解释"
            "覆盖实际系统证据。"
        )

    return {
        "status": "active",
        "profile": profile,
        "lenses": lenses,
        "activation_reasons": reasons,
        "scope": "consumer_task_local",
        "standing_constraint": False,
        "context": context,
    }


def _expanded_retrieval_text(route: dict[str, Any], prompt: str) -> str:
    facets = [prompt]
    memory_need = str(_route_field(route, "memory_need", "none"))
    target_surface = str(_route_field(route, "target_surface", "current_chat"))
    feedback = str(_route_field(route, "feedback_loop_profile", "none"))
    skill_audit = str(_route_field(route, "skill_audit_profile", "none"))
    first_principles = str(_route_field(route, "first_principles_profile", "none"))

    normalized_prompt = prompt.casefold()
    governance_recall = any(
        term in normalized_prompt
        for term in (
            "governance route",
            "routing trigger",
            "route miss",
            "harness route",
            "治理路由",
            "路由触发",
            "路由漏判",
            "主动触发断联",
        )
    )
    if governance_recall and (
        memory_need == "paired_err_sol"
        or (
            target_surface in {"local_harness", "adapter", "skill_matrix"}
            and feedback == "prevention_review"
        )
    ):
        facets.append(
            "embedded harness intake router missed R3; governance layer update; "
            "routing trigger update; framework behavior change; "
            "embedded harness R3 trigger fix; routing rule; framework behavior update"
        )
    if skill_audit not in {"", "none"}:
        facets.append(
            "skill矩阵; skill matrix; wrong decomposition; too many workflow skills; "
            "skill safety audit; redundancy audit"
        )
    if first_principles not in {"", "none"}:
        facets.append(
            "first principles; architecture; constraint preservation; concept-preserving engineering"
        )
    return "\n".join(facets)


def _prepare_prediction_reviews(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    by_rule: dict[str, dict[str, Any]] = {}
    for record in selected:
        if not bool(record.get("eligible_for_current_reuse", True)):
            continue
        rule = re.sub(r"\s+", " ", str(record.get("rule") or "")).strip()
        if not rule:
            continue
        normalized = rule.casefold()
        existing = by_rule.get(normalized)
        if existing:
            existing["source_record_ids"].append(record["record_id"])
            existing["source_paths"].append(record["path"])
            continue
        review = {
            "record_id": record["record_id"],
            "source_record_ids": [record["record_id"]],
            "risk_signature": list(record.get("retrieval_terms") or []),
            "candidate_safe_form": rule,
            "source_path": record["path"],
            "source_paths": [record["path"]],
            "source_sha256": record["sha256"],
            "selection_reasons": record["selection_reasons"],
            "match_confidence": record["match_confidence"],
            "applicability_status": "candidate",
            "evidence_target": "current_task_observation",
            "lifecycle": "task_local_until_observation",
        }
        by_rule[normalized] = review
        reviews.append(review)
    return reviews


def _interaction_lane(
    route: dict[str, Any],
    prompt: str,
    tool_input_text: str,
) -> str:
    preferred = str(_route_field(route, "preferred_call_surface", ""))
    need = str(_route_field(route, "tool_surface_need", ""))
    haystack = f"{preferred}\n{need}\n{prompt}\n{tool_input_text}".casefold()
    lane_terms = (
        (
            "keyboard-mouse-control",
            (
                "keyboard_mouse",
                "keyboard",
                "mouse",
                "sendkeys",
                "foreground focus",
                "wrong window",
                "coordinate",
                "clipboard",
                "drag",
                "键盘",
                "鼠标",
                "焦点",
                "坐标",
                "剪贴板",
            ),
        ),
        (
            "browser-control",
            (
                "browser_or_chrome",
                "browser",
                "chrome",
                "edge",
                "playwright",
                "cdp",
                "网页",
                "浏览器",
            ),
        ),
        (
            "desktop-app-control",
            (
                "desktop",
                "computer use",
                "native app",
                "wps",
                "photoshop",
                "桌面应用",
                "电脑控制",
            ),
        ),
        (
            "structured-tool-control",
            (
                "plugin_or_connector",
                "plugin_mcp",
                "mcp",
                "connector",
                "structured tool",
                "app tool",
                "插件",
                "连接器",
            ),
        ),
    )
    for lane, terms in lane_terms:
        if any(term in haystack for term in terms):
            return lane
    return ""


def _interaction_memory_source(lane: str) -> dict[str, Any] | None:
    if lane not in {
        "structured-tool-control",
        "browser-control",
        "desktop-app-control",
        "keyboard-mouse-control",
    }:
        return None
    lane_root = DEFAULT_INTERACTION_ROOT / lane
    return {
        "family": "IE",
        "index_path": lane_root / "_INDEX.md",
        "record_dir": lane_root,
        "rule_fields": ["Solution Applied", "Postcondition", "Fallback Boundary"],
    }


def _interaction_action_binding(
    route: dict[str, Any],
    lane: str,
) -> dict[str, Any] | None:
    if not lane:
        return None
    raw = _route_field(route, "interaction_action_binding", {})
    binding = dict(raw) if isinstance(raw, dict) else {}
    required_fields = [
        "interaction_surface",
        "target_app",
        "target_window",
        "target_control",
        "focus_evidence",
        "observation_ref",
        "observation_age",
        "coordinate_space",
        "action",
        "expected_postcondition",
        "fallback_boundary",
        "risk",
    ]
    missing_fields = [
        field for field in required_fields if not str(binding.get(field) or "").strip()
    ]
    observation_age = str(binding.get("observation_age") or "").strip().casefold()
    observation_is_fresh = observation_age in {"current", "fresh", "now"}
    age_match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|s|sec|secs|second|seconds)", observation_age)
    if age_match:
        age_value = float(age_match.group(1))
        age_seconds = age_value / 1000.0 if age_match.group(2) == "ms" else age_value
        observation_is_fresh = age_seconds <= 30.0
    status = (
        "incomplete"
        if missing_fields
        else "complete"
        if observation_is_fresh
        else "stale_observation"
    )
    return {
        "schema": "cbh.interaction_action_binding.v1",
        "lane": lane,
        **{field: binding.get(field) for field in required_fields},
        "missing_fields": missing_fields,
        "observation_freshness": "fresh" if observation_is_fresh else "stale_or_unverified",
        "status": status,
    }


def _task_local_correction_bundle(
    route: dict[str, Any],
    *,
    prompt: str,
    tool_input_text: str,
    selected_records: list[dict[str, Any]],
    lane: str,
) -> dict[str, Any]:
    environment = str(_route_field(route, "execution_environment", ""))
    if not environment:
        environment = (
            "powershell"
            if re.search(r"(?is)\bforeach\s*\(|\$[A-Za-z_]", tool_input_text)
            else "any"
        )
    tool_surface = str(_route_field(route, "candidate_tool_surface", ""))
    if not tool_surface:
        tool_surface = (
            "shell_command"
            if str(_route_field(route, "tool_surface_need", "")) == "shell"
            else str(_route_field(route, "tool_surface_need", ""))
        )
    try:
        gate = (
            build_behavior_correction_receipt(
                stage="pretool",
                environment=environment,
                tool_role=str(_route_field(route, "tool_role", "unknown")),
                tool_surface=tool_surface,
                text=tool_input_text,
                execution_cwd=str(_route_field(route, "execution_cwd", "")),
                target_binding_sha256=str(
                    _route_field(route, "target_binding_sha256", "")
                ),
            )
            if tool_input_text
            else {"decision": "no_match", "matches": []}
        )
    except CorrectionProfileRegistryError:
        gate = {
            "decision": "semantic_review_required",
            "matches": [],
            "issues": ["behavior_correction_registry_unavailable"],
        }

    action_binding = _interaction_action_binding(route, lane)
    decision = str(gate.get("decision") or "no_match")
    if decision == "no_match" and action_binding is not None:
        decision = (
            "validation_required"
            if action_binding["status"] == "complete"
            else "semantic_review_required"
        )
    current_records = [
        record
        for record in selected_records
        if bool(record.get("eligible_for_current_reuse", True))
        or str(record.get("family") or "") in {"IE", "INC"}
    ]
    if decision == "no_match" and current_records:
        decision = "semantic_review_required"

    matches = list(gate.get("matches") or [])
    source_record_ids: list[str] = []
    for match in matches:
        source_record_ids.extend(str(item) for item in match.get("source_record_ids") or [])
    source_record_ids.extend(str(record["record_id"]) for record in current_records)
    source_record_ids = list(dict.fromkeys(item for item in source_record_ids if item))
    profile_ids = [str(match["profile_id"]) for match in matches]
    candidate_material = tool_input_text or json.dumps(
        action_binding or {"prompt": prompt},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    candidate_key = hashlib.sha256(candidate_material.encode("utf-8")).hexdigest()
    postconditions = list(
        dict.fromkeys(
            item
            for item in [
                *(str(match.get("postcondition") or "") for match in matches),
                *(str(record.get("postcondition") or "") for record in current_records),
                str((action_binding or {}).get("expected_postcondition") or ""),
            ]
            if item
        )
    )
    return {
        "schema": "cbh.task_local_correction_bundle.v1",
        "status": "no_match" if decision == "no_match" else "candidate_ready",
        "decision": decision,
        "candidate_key": candidate_key,
        "same_candidate_policy": "evaluate_once_per_unchanged_candidate",
        "correction_family_ids": list(
            dict.fromkeys(
                str(match.get("correction_family_id") or "")
                for match in matches
                if str(match.get("correction_family_id") or "")
            )
        ),
        "profile_ids": profile_ids,
        "source_record_ids": source_record_ids,
        "verifiers": list(
            dict.fromkeys(
                str(match.get("verifier") or "")
                for match in matches
                if str(match.get("verifier") or "")
            )
        ),
        "postconditions": postconditions,
        "action_binding": action_binding,
        "lifecycle": (
            "task_local_until_postcondition" if decision != "no_match" else "retired_no_match"
        ),
        "automatic_promotion": False,
        "automatic_long_term_memory_write": False,
    }


def _context_parts(
    route: dict[str, Any],
    selected_records: list[dict[str, Any]],
    semantic_review_candidates: list[dict[str, Any]],
    disambiguation_candidates: list[dict[str, Any]],
    prediction_reviews: list[dict[str, Any]],
    reasoning_activation: dict[str, Any],
    reflexive_gap_review: dict[str, Any] | None,
    conversation_navigation: dict[str, Any],
) -> list[str]:
    parts: list[str] = []
    for bundle in conversation_navigation.get("bundles", []):
        for document in bundle.get("documents", []):
            parts.append(
                "只读跨对话导航（摘要/索引仅作定位，raw 仍是事实源）："
                f"kind={document['kind']} source={document['path']} sha256={document['sha256']}\n"
                f"excerpt_mode={document['excerpt_mode']} omitted_chars={document['omitted_char_count']}\n"
                f"{document['excerpt']}"
            )
    if disambiguation_candidates:
        lines = [
            f"{index}. {item['event_date']} — {item['summary']} "
            f"(status={item['current_status']})"
            for index, item in enumerate(disambiguation_candidates, start=1)
        ]
        parts.append(
            "模糊历史线索只定位到以下导航候选，尚未确认具体事件，也未下钻原始证据或配对方案。"
            "请让用户确认一个候选或补充一个线索；简短列出：\n"
            + "\n".join(lines)
        )
    for review in prediction_reviews:
        record_ids = ",".join(review["source_record_ids"])
        parts.append(
            "当前任务候选预测（非约束；先判断是否适用，观察结果后退役）："
            f"[{record_ids}] 候选安全形式：{review['candidate_safe_form']}"
        )

    for record in semantic_review_candidates:
        candidate_text = re.sub(
            r"\s+",
            " ",
            str(record.get("rule") or record.get("solution_summary") or ""),
        ).strip()
        parts.append(
            "模型语义复排候选（尚未选中、不得直接当作事实或约束）："
            f"[{record['record_id']}] score={record.get('score')} "
            f"source={record.get('path')} 候选内容：{candidate_text}"
        )

    for record in selected_records:
        if bool(record.get("eligible_for_current_reuse", True)):
            continue
        if str(record.get("family")) == "META":
            continue
        parts.append(
            "历史或非当前动作记录（只作因果/来源导航，不得自动复用）："
            f"[{record['record_id']}] status={record.get('record_status') or 'unknown'}"
        )

    reasoning_context = str(reasoning_activation.get("context") or "")
    if reasoning_context:
        parts.append(reasoning_context)

    if reflexive_gap_review is not None:
        parts.append(
            "自反缺口浅层分诊：区分知识覆盖缺口、知识激活缺口、目标保真缺口、环境或接口缺口，"
            "并保留目标已改变、证据不足或无缺口。先问：已知内容在哪个动作边界未激活；当前推进"
            "原目标还是代理产物；什么反证能推翻顺手解释；哪个事实或机械验证器能区分原因。"
            "形成 gap_class、counterevidence、next_action_binding、verifier、retire_condition；"
            "证据足够即退役，仍有竞争解释或高影响不确定性时才调用 $reflexive-gap-analysis 深钻。"
            "不得据此自动写长期记忆或实施语义硬拒绝。"
        )

    skill_audit = str(_route_field(route, "skill_audit_profile", "none"))
    if skill_audit not in {"", "none"}:
        parts.append(
            "Skill 审计近动作：核对权限/脚本/外部依赖安全；检查功能重叠、长期未用、"
            "可合并项和唯一触发表达；只有等价测试通过才允许精简。"
        )
    return parts


def _partition_exact(text: str, target_chars: int) -> list[str]:
    if not text:
        return []
    target_chars = max(1, target_chars)
    return [text[index : index + target_chars] for index in range(0, len(text), target_chars)]


def _parse_aware_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _value_matches_declared_type(value: Any, value_type: str) -> bool:
    if value_type == "string":
        return isinstance(value, str)
    if value_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "null":
        return value is None
    if value_type == "string_list":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    if value_type == "json":
        return isinstance(value, (dict, list))
    return False


def _json_pointer_parts(pointer: str) -> list[str] | None:
    if not pointer.startswith("/"):
        return None
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _bind_typed_value(
    payload: dict[str, Any], pointer: str, fact_id: str, value: Any
) -> bool:
    parts = _json_pointer_parts(pointer)
    if not parts:
        return False
    current: Any = payload
    for part in parts[:-1]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False
    final = parts[-1]
    expected = {"$fact": fact_id}
    if isinstance(current, dict):
        if current.get(final) != expected:
            return False
        current[final] = copy.deepcopy(value)
        return True
    if isinstance(current, list) and final.isdigit() and int(final) < len(current):
        index = int(final)
        if current[index] != expected:
            return False
        current[index] = copy.deepcopy(value)
        return True
    return False


def _build_canonical_fact_binding(
    packet: Any,
    request: Any,
) -> dict[str, Any]:
    packet_object = packet if isinstance(packet, dict) else {}
    request_object = request if isinstance(request, dict) else {}
    original_tool_call = copy.deepcopy(request_object.get("tool_call"))
    original_answer = request_object.get("answer_template")
    working_tool_call = copy.deepcopy(original_tool_call)
    working_answer = original_answer
    errors: list[str] = []
    facts = packet_object.get("facts")
    if packet_object.get("schema") != "cbh.canonical_fact_packet.v1":
        errors.append("invalid_packet_schema")
    if request_object.get("schema") != "cbh.fact_binding_request.v1":
        errors.append("invalid_binding_request_schema")
    if not isinstance(facts, list):
        errors.append("facts_must_be_array")
        facts = []

    observed_at = _parse_aware_datetime(request_object.get("observed_at"))
    if observed_at is None:
        observed_at = datetime.now(timezone.utc)

    fact_ids: list[str] = []
    control_fact_ids: list[str] = []
    model_context_facts: list[dict[str, Any]] = []
    audit_records: list[dict[str, Any]] = []
    unregistered_fact_ids: list[str] = []
    failed_fact_ids: list[str] = []
    seen: set[str] = set()

    for fact in facts:
        if not isinstance(fact, dict):
            errors.append("fact_must_be_object")
            continue
        fact_id = str(fact.get("fact_id") or "")
        if not fact_id:
            errors.append("missing_fact_id")
            continue
        fact_ids.append(fact_id)
        if fact_id in seen:
            errors.append(f"duplicate_fact_id:{fact_id}")
            failed_fact_ids.append(fact_id)
            continue
        seen.add(fact_id)

        channel = str(fact.get("channel") or "")
        consumer = fact.get("consumer")
        if channel not in {"control_fact", "model_context", "audit_only"}:
            errors.append(f"unregistered_channel:{fact_id}")
            unregistered_fact_ids.append(fact_id)
            failed_fact_ids.append(fact_id)
            continue
        if not isinstance(consumer, dict):
            errors.append(f"missing_consumer:{fact_id}")
            unregistered_fact_ids.append(fact_id)
            failed_fact_ids.append(fact_id)
            continue

        status = str(fact.get("status") or "")
        source_refs = fact.get("source_refs")
        verification_method = str(fact.get("verification_method") or "")
        valid_scope = str(fact.get("valid_scope") or "")
        value_type = str(fact.get("value_type") or "")
        if status != "verified":
            errors.append(f"fact_not_verified:{fact_id}")
            failed_fact_ids.append(fact_id)
            continue
        if not isinstance(source_refs, list) or not source_refs or not all(
            isinstance(item, str) and item for item in source_refs
        ):
            errors.append(f"missing_source_refs:{fact_id}")
            failed_fact_ids.append(fact_id)
            continue
        if not verification_method or not valid_scope:
            errors.append(f"missing_verification_boundary:{fact_id}")
            failed_fact_ids.append(fact_id)
            continue
        if fact.get("conflict_status") != "none":
            errors.append(f"unresolved_conflict:{fact_id}")
            failed_fact_ids.append(fact_id)
            continue
        if fact.get("on_conflict") != "do_not_bind" or fact.get("on_unknown") != "do_not_bind":
            errors.append(f"unsafe_unknown_or_conflict_policy:{fact_id}")
            failed_fact_ids.append(fact_id)
            continue
        if fact.get("audit_destination") != "receipt":
            errors.append(f"unsupported_audit_destination:{fact_id}")
            failed_fact_ids.append(fact_id)
            continue
        if not _value_matches_declared_type(fact.get("value"), value_type):
            errors.append(f"value_type_mismatch:{fact_id}")
            failed_fact_ids.append(fact_id)
            continue

        producer = fact.get("producer")
        if not isinstance(producer, dict):
            errors.append(f"missing_producer:{fact_id}")
            failed_fact_ids.append(fact_id)
            continue
        producer_id = str(producer.get("producer_id") or "")
        receipt_ref = str(producer.get("receipt_ref") or "")
        declared_value_sha256 = str(producer.get("value_sha256") or "")
        actual_value_sha256 = hashlib.sha256(
            json.dumps(
                fact.get("value"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if producer_id not in REGISTERED_FACT_PRODUCERS:
            errors.append(f"unregistered_producer:{fact_id}")
            failed_fact_ids.append(fact_id)
            continue
        if not receipt_ref:
            errors.append(f"missing_producer_receipt_ref:{fact_id}")
            failed_fact_ids.append(fact_id)
            continue
        if declared_value_sha256 != actual_value_sha256:
            errors.append(f"producer_value_hash_mismatch:{fact_id}")
            failed_fact_ids.append(fact_id)
            continue

        volatility = str(fact.get("volatility") or "stable")
        if volatility == "revision_sensitive":
            checked_at = _parse_aware_datetime(fact.get("checked_at"))
            freshness_window = fact.get("freshness_window_seconds")
            if checked_at is None or not isinstance(freshness_window, int) or freshness_window <= 0:
                errors.append(f"missing_freshness_boundary:{fact_id}")
                failed_fact_ids.append(fact_id)
                continue
            age_seconds = (observed_at.astimezone(timezone.utc) - checked_at.astimezone(timezone.utc)).total_seconds()
            if age_seconds < -300 or age_seconds > freshness_window:
                errors.append(f"fact_outside_freshness_window:{fact_id}")
                failed_fact_ids.append(fact_id)
                continue
        elif volatility != "stable":
            errors.append(f"unsupported_volatility:{fact_id}")
            failed_fact_ids.append(fact_id)
            continue

        consumer_id = str(consumer.get("consumer_id") or "")
        phase = str(consumer.get("consumption_phase") or "")
        effect = str(consumer.get("effect") or "")
        target = str(consumer.get("target") or "")
        value = fact.get("value")

        if channel == "control_fact":
            control_fact_ids.append(fact_id)
            if consumer_id == "typed_tool_argument_binder" and phase == "pretool" and effect == "typed_value_set":
                if not isinstance(working_tool_call, dict) or not _bind_typed_value(
                    working_tool_call, target, fact_id, value
                ):
                    errors.append(f"tool_binding_target_mismatch:{fact_id}")
                    failed_fact_ids.append(fact_id)
            elif consumer_id == "answer_slot_binder" and phase == "final_assembly" and effect == "literal_slot_replace":
                placeholder = "{{fact:" + fact_id + "}}"
                if not isinstance(working_answer, str) or placeholder not in working_answer:
                    errors.append(f"answer_binding_target_mismatch:{fact_id}")
                    failed_fact_ids.append(fact_id)
                else:
                    replacement = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
                    working_answer = working_answer.replace(placeholder, replacement)
            else:
                errors.append(f"unregistered_control_consumer:{fact_id}")
                unregistered_fact_ids.append(fact_id)
                failed_fact_ids.append(fact_id)
        elif channel == "model_context":
            if not (
                consumer_id == "host_model_agent"
                and phase == "reasoning"
                and effect == "minimal_context"
                and target == "additional_context"
            ):
                errors.append(f"unregistered_model_context_consumer:{fact_id}")
                unregistered_fact_ids.append(fact_id)
                failed_fact_ids.append(fact_id)
                continue
            model_context_facts.append(
                {
                    "fact_id": fact_id,
                    "value": copy.deepcopy(value),
                    "value_type": value_type,
                    "source_refs": list(source_refs),
                    "valid_scope": valid_scope,
                    "producer_id": producer_id,
                    "producer_receipt_ref": receipt_ref,
                }
            )
        else:
            if not (
                consumer_id == "receipt_audit"
                and phase == "postconsume"
                and effect == "audit_only"
                and target == "canonical_fact_binding.audit_records"
            ):
                errors.append(f"unregistered_audit_consumer:{fact_id}")
                unregistered_fact_ids.append(fact_id)
                failed_fact_ids.append(fact_id)
                continue
            audit_records.append(
                {
                    "fact_id": fact_id,
                    "status": status,
                    "source_refs": list(source_refs),
                    "producer_id": producer_id,
                    "producer_receipt_ref": receipt_ref,
                    "value_sha256": actual_value_sha256,
                }
            )

    failed = set(failed_fact_ids)
    atomic_complete = not errors
    unconsumed_fact_ids = [
        fact_id
        for fact_id in fact_ids
        if fact_id in failed or (not atomic_complete and fact_id in control_fact_ids)
    ]
    return {
        "schema": "cbh.canonical_fact_binding_receipt.v1",
        "status": "complete" if atomic_complete else "incomplete",
        "binding_mode": "atomic_late_binding",
        "packet_sha256": hashlib.sha256(
            json.dumps(packet_object, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "bound_tool_call": working_tool_call if atomic_complete else original_tool_call,
        "rendered_answer": working_answer if atomic_complete else original_answer,
        "bound_control_fact_ids": control_fact_ids if atomic_complete else [],
        "model_context_fact_ids": [item["fact_id"] for item in model_context_facts],
        "model_context_facts": model_context_facts,
        "audit_only_fact_ids": [item["fact_id"] for item in audit_records],
        "audit_records": audit_records,
        "unconsumed_fact_ids": unconsumed_fact_ids,
        "errors": errors,
        "runtime_field_audit": {
            "channels": {
                "control_fact": control_fact_ids,
                "model_context": [item["fact_id"] for item in model_context_facts],
                "audit_only": [item["fact_id"] for item in audit_records],
            },
            "unregistered_fact_ids": list(dict.fromkeys(unregistered_fact_ids)),
            "invariant_status": "complete" if not unregistered_fact_ids else "incomplete",
            "invariant": "all_facts_minus_registered_consumers_minus_explicit_audit_only_is_empty",
        },
        "truth_boundary": "validates_binding_contract_and_provenance_metadata_not_external_truth",
        "persistence": "none",
    }


def build_action_consumption(
    route: dict[str, Any],
    *,
    prompt: str,
    tool_input_text: str = "",
    query_type: str = "history_reason",
    meta_path: Path = DEFAULT_COMMON_ERROR_META,
    capability_contract_path: Path = DEFAULT_CAPABILITY_CONTRACT,
    memory_sources: list[dict[str, Any]] | None = None,
    soft_target_records: int = SOFT_TARGET_RECORDS,
    task_event: Mapping[str, Any] | None = None,
    task_capsule: Mapping[str, Any] | None = None,
    host_limits: Mapping[str, Any] | None = None,
    transport_cursor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    contract = json.loads(capability_contract_path.read_text(encoding="utf-8-sig"))
    support = contract.get("action_support", {})
    bindings = _bindings(route)
    binding_ids = {str(item.get("action")) for item in bindings if item.get("action")}
    engineering_profiles_value = _route_field(route, "engineering_execution_profiles", [])
    engineering_profiles = (
        [str(value) for value in engineering_profiles_value if str(value)]
        if isinstance(engineering_profiles_value, list)
        else []
    )
    engineering_execution_receipt = (
        build_engineering_execution_receipt(engineering_profiles, task_event)
        if "apply_engineering_execution_profile" in binding_ids and engineering_profiles
        else None
    )
    wants_retrieval = any(item["action"] == "retrieve_matching_memory" for item in bindings)
    wants_prediction_review = any(
        item["action"] == "prepare_task_local_prediction_review"
        for item in bindings
    )
    wants_correction_bundle = any(
        item["action"] == "prepare_task_local_correction_bundle"
        for item in bindings
    )
    wants_external_retrieval = any(
        item["action"] == "perform_external_research_route" for item in bindings
    )
    wants_fact_binding = any(
        item["action"] == "bind_verified_facts" for item in bindings
    )
    canonical_fact_binding = (
        _build_canonical_fact_binding(
            _route_field(route, "canonical_fact_packet", None),
            _route_field(route, "fact_binding_request", None),
        )
        if wants_fact_binding
        else None
    )
    external_modes = [
        mode for mode in _route_string_list(route, "external_need") if mode != "none"
    ]
    if wants_external_retrieval and not external_modes:
        external_modes = ["general_web_cross_check"]
    external_retrieval_receipt = (
        build_external_retrieval_receipt(
            prompt,
            recommended_modes=external_modes,
        )
        if wants_external_retrieval
        else None
    )
    interaction_lane = _interaction_lane(route, prompt, tool_input_text)
    dynamic_sources, dynamic_source_receipt = _dynamic_memory_sources(route)
    conversation_navigation = _conversation_navigation_bundle(route, prompt)
    if memory_sources is not None:
        sources = list(memory_sources)
    elif (
        str(_route_field(route, "memory_lane", "none")) == "referenced_conversation"
        and str(_route_field(route, "memory_need", "none")) not in {"paired_err_sol", "common_error_corpus"}
        and dynamic_sources
    ):
        sources = []
    else:
        sources = _default_memory_sources(meta_path)
    interaction_source: dict[str, Any] | None = None
    if wants_correction_bundle and memory_sources is None:
        interaction_source = _interaction_memory_source(interaction_lane)
        if interaction_source is not None:
            sources.append(interaction_source)
    sources.extend(dynamic_sources)
    retrieval_text = _expanded_retrieval_text(route, prompt)
    expand_linked_pairs = str(_route_field(route, "memory_need", "none")) == "paired_err_sol"
    retrieval = (
        select_memory_records(
            retrieval_text,
            tool_input_text,
            memory_sources=sources,
            soft_target_records=soft_target_records,
            expand_linked_pairs=expand_linked_pairs,
            query_type=query_type,
        )
        if wants_retrieval or wants_prediction_review or wants_correction_bundle
        else {
            "selected_records": [],
            "semantic_review_candidates": [],
            "disambiguation_candidates": [],
            "disambiguation_required": False,
            "soft_target_records": soft_target_records,
            "direct_candidate_count": 0,
            "weak_candidate_count": 0,
            "expanded_for_direct_coverage": False,
            "paired_expansion_ids": [],
            "coverage_status": "not_requested",
            "review_candidate_ids": [],
            "deferred_weak_candidate_ids": [],
            "unresolved_record_ids": [],
            "source_indexes": [str(Path(source["index_path"])) for source in sources],
        }
    )
    selected = list(retrieval["selected_records"])
    semantic_review_candidates = list(retrieval["semantic_review_candidates"])
    memory_source_cascade = plan_memory_source_cascade(
        read_depth_profile=str(_route_field(route, "read_depth_profile", "none")),
        selected_records=selected,
        semantic_review_candidates=semantic_review_candidates,
        retrieval_requested=wants_retrieval,
        conversation_navigation=conversation_navigation,
    )
    correction_records = list(selected)
    if interaction_source is not None:
        interaction_retrieval = select_memory_records(
            prompt,
            tool_input_text,
            memory_sources=[interaction_source],
            soft_target_records=soft_target_records,
            query_type=query_type,
        )
        known_ids = {str(record["record_id"]) for record in correction_records}
        correction_records.extend(
            record
            for record in interaction_retrieval["selected_records"]
            if str(record["record_id"]) not in known_ids
        )
    prediction_reviews = (
        _prepare_prediction_reviews(selected)
        if wants_prediction_review or wants_correction_bundle
        else []
    )
    prediction_review = prediction_reviews[0] if prediction_reviews else None
    correction_bundle = (
        _task_local_correction_bundle(
            route,
            prompt=prompt,
            tool_input_text=tool_input_text,
            selected_records=correction_records,
            lane=interaction_lane,
        )
        if wants_correction_bundle
        else None
    )

    task_continuity_decision: dict[str, Any] | None = None
    task_transition: dict[str, Any] | None = None
    dynamic_reminders: list[dict[str, Any]] = []
    current_task_capsule: dict[str, Any] | None = None
    task_capsule_context = ""
    task_capsule_control_context = ""
    task_transport_receipt: dict[str, Any] | None = None
    if task_event is not None:
        task_continuity_decision = decide_task_continuity(route, task_event, task_capsule)
        if task_capsule is None:
            if task_continuity_decision["decision"] != "dormant":
                current_task_capsule, task_transition = initialize_task_capsule(
                    route, task_event
                )
        else:
            task_transition = apply_task_event(task_capsule, task_event)
            current_task_capsule = task_transition["capsule"]
            dynamic_reminders = build_dynamic_reminders(
                current_task_capsule, task_transition
            )
            if dynamic_reminders:
                current_task_capsule = dynamic_reminders[-1]["capsule_snapshot"]
                dynamic_reminders = [
                    {key: value for key, value in item.items() if key != "capsule_snapshot"}
                    for item in dynamic_reminders
                ]
        if current_task_capsule is not None:
            context_receipt = build_task_capsule_context(
                current_task_capsule,
                dynamic_reminders,
                host_limits=host_limits,
            )
            entry = context_receipt.get("entry")
            if isinstance(entry, Mapping):
                task_capsule_context = str(entry.get("value") or "")
            control_entry = context_receipt.get("control_entry")
            if isinstance(control_entry, Mapping):
                task_capsule_control_context = str(
                    control_entry.get("value") or ""
                )
            task_transport_receipt = {
                "delivery": context_receipt.get("delivery"),
                "char_count": context_receipt.get("char_count"),
                "cursor": dict(transport_cursor) if isinstance(transport_cursor, Mapping) else None,
            }
    elif isinstance(_route_field(route, "task_continuity_decision"), Mapping):
        task_continuity_decision = dict(_route_field(route, "task_continuity_decision"))

    actions: list[dict[str, Any]] = []
    pending_user_confirmation: dict[str, Any] | None = None
    for binding in bindings:
        action_id = binding["action"]
        declared_support = str(support.get(action_id, "agent_required"))
        status = "deferred_to_agent"
        evidence: Any = None
        if action_id == "await_scoped_user_confirmation":
            confirmation_request = binding.get("confirmation_request")
            if isinstance(confirmation_request, dict):
                status = "pending_user_confirmation"
                evidence = confirmation_request
                pending_user_confirmation = confirmation_request
            else:
                status = "deferred_to_agent"
                evidence = "semantic_confirmation_binding_required"
        elif action_id == "retrieve_matching_memory":
            if selected:
                if retrieval["coverage_status"] == "complete":
                    status = "completed"
                    evidence = [item["record_id"] for item in selected]
                elif retrieval["coverage_status"] == "semantic_review_required":
                    status = "ready_for_model_semantic_selection"
                    evidence = [
                        item["record_id"]
                        for item in [*selected, *semantic_review_candidates]
                    ]
                else:
                    status = retrieval["coverage_status"]
                    evidence = [item["record_id"] for item in selected]
            else:
                if retrieval.get("disambiguation_candidates"):
                    status = "ready_for_model_semantic_selection"
                    evidence = [
                        item["record_id"] for item in retrieval["disambiguation_candidates"]
                    ]
                elif semantic_review_candidates:
                    status = "ready_for_model_semantic_selection"
                    evidence = [item["record_id"] for item in semantic_review_candidates]
                else:
                    status = "not_applicable_with_reason"
                    evidence = (
                        "no_matching_record"
                        if retrieval["coverage_status"] == "no_match"
                        else retrieval["coverage_status"]
                    )
        elif action_id == "prepare_task_local_prediction_review":
            if prediction_reviews:
                status = (
                    "completed"
                    if retrieval["coverage_status"] == "complete"
                    else "semantic_review_required"
                )
                evidence = [item["record_id"] for item in prediction_reviews]
            else:
                status = "not_applicable_with_reason"
                evidence = "no_selected_prediction_source"
        elif action_id == "prepare_task_local_correction_bundle":
            if correction_bundle is not None:
                status = (
                    "not_applicable_with_reason"
                    if correction_bundle["decision"] == "no_match"
                    else "completed"
                )
                evidence = (
                    "no_current_candidate_match"
                    if correction_bundle["decision"] == "no_match"
                    else correction_bundle["candidate_key"]
                )
            else:
                status = "not_applicable_with_reason"
                evidence = "correction_bundle_not_requested"
        elif action_id == "prepare_task_continuity_capsule":
            if current_task_capsule is None:
                status = "not_applicable_with_reason"
                evidence = "task_continuity_dormant_or_no_task_event"
            else:
                status = "completed"
                evidence = current_task_capsule.get("capsule_id")
        elif action_id == "apply_engineering_execution_profile":
            if engineering_execution_receipt is None:
                status = "not_applicable_with_reason"
                evidence = "engineering_execution_profile_not_routed"
            else:
                status = "completed"
                evidence = {
                    "schema": engineering_execution_receipt["schema"],
                    "profiles": engineering_execution_receipt["profiles"],
                    "result_schemas": {
                        profile: result.get("schema")
                        for profile, result in engineering_execution_receipt["results"].items()
                    },
                }
        elif action_id == "resolve_conversation_link":
            bundles = list(conversation_navigation.get("bundles") or [])
            resolved_links = [link for bundle in bundles for link in bundle.get("selected_links", [])]
            if conversation_navigation.get("status") in {"resolved", "partial"} and bundles:
                status = "completed" if resolved_links else "ready_for_model_semantic_selection"
                evidence = {
                    "memory_ids": [bundle.get("memory_id") for bundle in bundles],
                    "selected_link_ids": [link.get("link_id") for link in resolved_links],
                    "ledger_indexes": [bundle.get("ledger", {}).get("index_path") for bundle in bundles],
                }
            else:
                status = "deferred_to_agent"
                evidence = conversation_navigation.get("status")
        elif action_id == "perform_external_research_route":
            status = "deferred_to_agent"
            evidence = {
                "receipt_schema": external_retrieval_receipt["schema"],
                "retrieval_profile": external_retrieval_receipt["retrieval_profile"],
                "coverage_status": external_retrieval_receipt["coverage_status"],
                "query_ids": [
                    item["query_id"]
                    for item in external_retrieval_receipt["query_plan"]
                ],
                "pending_source_routes": [
                    {
                        "target_id": item["target_id"],
                        "source_route_id": item["source_route_id"],
                        "mode": item["mode"],
                        "activation_condition": item["activation_condition"],
                    }
                    for item in external_retrieval_receipt[
                        "source_capability_candidates"
                    ]
                ],
                "target_coverage": external_retrieval_receipt[
                    "target_coverage"
                ],
                "negative_evidence_boundary": external_retrieval_receipt[
                    "negative_evidence_boundary"
                ],
                "currentness_evidence_rule": external_retrieval_receipt[
                    "currentness_evidence_rule"
                ],
            }
        elif action_id == "bind_verified_facts":
            if canonical_fact_binding is None:
                status = "deferred_to_agent"
                evidence = "canonical_fact_packet_and_binding_request_required"
            elif canonical_fact_binding["status"] == "complete":
                status = "completed"
                evidence = {
                    "receipt_schema": canonical_fact_binding["schema"],
                    "packet_sha256": canonical_fact_binding["packet_sha256"],
                    "bound_control_fact_ids": canonical_fact_binding[
                        "bound_control_fact_ids"
                    ],
                }
            else:
                status = "binding_incomplete"
                evidence = {
                    "errors": canonical_fact_binding["errors"],
                    "unconsumed_fact_ids": canonical_fact_binding[
                        "unconsumed_fact_ids"
                    ],
                }
        elif declared_support == "context_binding_only":
            status = "bound_to_context"
            evidence = "additional_context"
        elif declared_support == "host_pending":
            status = "host_pending"
            evidence = "no_host_owned_consumer_receipt"
        elif declared_support == "consumer_supported":
            status = "consumer_handler_missing"
            evidence = "no_concrete_consumer_receipt"
        action_receipt = {
            "action_id": action_id,
            "status": status,
            "declared_support": declared_support,
            "completion_evidence": evidence,
        }
        if binding.get("activation_source"):
            action_receipt["activation_source"] = binding["activation_source"]
        actions.append(action_receipt)

    reasoning_activation = _compose_reasoning_activation(route)
    reflexive_gap_review = _reflexive_gap_review(route)
    context_parts = _context_parts(
            route,
            selected,
            semantic_review_candidates,
            list(retrieval.get("disambiguation_candidates") or []),
            prediction_reviews,
            reasoning_activation,
            reflexive_gap_review,
            conversation_navigation,
        )
    priority_context_parts: list[str] = []
    if correction_bundle is not None and correction_bundle["decision"] != "no_match":
        context_parts.append(
            "当前候选纠偏："
            f"decision={correction_bundle['decision']}; "
            f"candidate={correction_bundle['candidate_key']}; "
            f"sources={','.join(correction_bundle['source_record_ids']) or 'profile_only'}。"
            "仅绑定本次未变化候选，完成声明的后置验证后退役。"
        )
    if external_retrieval_receipt is not None:
        exact_values = [
            item["raw_text"] for item in external_retrieval_receipt["exact_anchors"]
        ]
        source_routes = [
            item["source_route_id"]
            for item in external_retrieval_receipt["source_capability_candidates"]
        ]
        unknown_targets = [
            item["raw_text"]
            for item in external_retrieval_receipt["exact_anchors"]
            if item.get("provider_hint") == "source_registry_unknown"
        ]
        context_parts.append(
            "外部检索近动作："
            f"profile={external_retrieval_receipt['retrieval_profile']}; "
            f"exact_anchors={json.dumps(exact_values, ensure_ascii=False)}; "
            f"source_routes={json.dumps(source_routes, ensure_ascii=False)}; "
            f"unknown_source_targets={json.dumps(unknown_targets, ensure_ascii=False)}; "
            "逐字保留并独立执行原始查询与精确锚点查询；弱、空、冲突或来源不可用时转向匹配的原生来源/直接地址；"
            "未知命名空间先做来源能力发现，不得默认解释为 GitHub、模型库或包注册表；"
            "多实体任务先按局部子句使用 linked_target_ids 绑定分面，无法唯一归属则保留语义复核；逐目标打开匹配主源核对，并把 query_id、计划允许的 source_route_id、source_role、supports_target_ids、canonical refs 与 conflict status 绑定到同一目标；没有机械已知 canonical URL 的路线还要给出 verified-for-target authority binding；"
            "现势或版本声明必须使用带时区 ISO checked_at，并声明适合该事实波动速度的正 freshness window 与依据；"
            "单一 provider、索引或 Top-K 未命中不得写成对象不存在。"
        )
    if pending_user_confirmation is not None:
        priority_context_parts.append(
            "高风险近动作边界：等待当前事件的一次性明确确认。"
            f"confirmation_request={json.dumps(pending_user_confirmation, ensure_ascii=False)}。"
            "先向用户说明动作、精确目标、范围、影响、恢复方式和明确非目标；"
            "本 receipt 不授予权限、不保存许可，也不得自行把确认标记为已消费。"
        )
    if canonical_fact_binding is not None:
        for fact in canonical_fact_binding["model_context_facts"]:
            priority_context_parts.append(
                "规范事实的最小推理上下文（不参与工具参数或答案槽位机械绑定）："
                f"fact_id={fact['fact_id']}; value="
                f"{json.dumps(fact['value'], ensure_ascii=False, sort_keys=True)}; "
                f"scope={fact['valid_scope']}; "
                f"source_refs={json.dumps(fact['source_refs'], ensure_ascii=False)}。"
            )
    if task_capsule_control_context:
        priority_context_parts.append(task_capsule_control_context)
    if task_capsule_context:
        priority_context_parts.append(task_capsule_context)
    if engineering_execution_receipt is not None:
        engineering_summary = {
            "profiles": engineering_execution_receipt["profiles"],
            "results": {
                profile: {
                    key: result.get(key)
                    for key in (
                        "status", "migration_phase", "frontier_step_ids", "graph_issues",
                        "verdict", "missing_evidence", "runtime_invoker", "issues", "seam_status",
                    )
                    if result.get(key) not in (None, [], {})
                }
                for profile, result in engineering_execution_receipt["results"].items()
            },
            "authority_granted": False,
        }
        priority_context_parts.append(
            "CBH engineering execution receipt (advisory, task-local): "
            + json.dumps(engineering_summary, ensure_ascii=False, separators=(",", ":"))
        )
    context_parts = [*priority_context_parts, *context_parts]
    additional_context = "\n".join(context_parts)
    effective_host_limits = (
        dict(host_limits)
        if isinstance(host_limits, Mapping)
        else {
            "max_chars": CONTEXT_SOFT_TARGET_CHARS,
            "max_tokens": 900,
            "max_items": 100,
        }
    )
    if additional_context:
        transport_plan = plan_transport(
            current_task_capsule,
            {"kind": "text", "original_chars": len(additional_context), "original_items": 1},
            effective_host_limits,
        )
        context_page = page_result(additional_context, transport_plan, transport_cursor)
        additional_context = context_page["content"]
        task_transport_receipt = {
            "schema": context_page["schema"],
            "delivery": "complete" if context_page["complete"] else "continuation_required",
            "page_sha256": context_page["page_sha256"],
            "full_result_sha256": context_page["full_result_sha256"],
            "original_chars": context_page["original_chars"],
            "forwarded_chars": context_page["forwarded_chars"],
            "uncovered_chars": context_page["uncovered_chars"],
            "next_cursor": context_page["next_cursor"],
        }
    continuity_control_in_first_page = (
        not task_capsule_control_context
        or task_capsule_control_context in additional_context
    )
    context_segments = _partition_exact(additional_context, CONTEXT_SOFT_TARGET_CHARS)
    complete_statuses = {
        "completed",
        "not_applicable_with_reason",
        "bound_to_context",
        "ready_for_model_semantic_selection",
    }
    unconsumed = [
        item["action_id"] for item in actions if item["status"] not in complete_statuses
    ]
    effective_route_delta = None
    routing_status = str(_route_field(route, "routing_status", "classified"))
    execution_disposition = (
        "pending_user_confirmation"
        if pending_user_confirmation is not None
        else str(_route_field(route, "execution_disposition", "advisory_route_ready"))
    )
    return {
        "schema": "cbh.action_consumption_receipt.v1",
        "consumer": str(contract.get("consumer") or "unknown"),
        "status": "complete" if not unconsumed else "incomplete",
        "routing_status": routing_status,
        "execution_disposition": execution_disposition,
        "pending_user_confirmation": pending_user_confirmation,
        "selected_records": selected,
        "semantic_review_candidates": semantic_review_candidates,
        "semantic_review_owner": "host_model_agent",
        "retrieval": {
            key: value
            for key, value in retrieval.items()
            if key not in {"selected_records", "semantic_review_candidates"}
        },
        "prediction_reviews": prediction_reviews,
        "task_local_prediction_review": prediction_review,
        "task_local_correction_bundle": correction_bundle,
        "task_continuity_decision": task_continuity_decision,
        "task_capsule": current_task_capsule,
        "task_transition": task_transition,
        "dynamic_reminders": dynamic_reminders,
        "engineering_execution_receipt": engineering_execution_receipt,
        "transport_receipt": task_transport_receipt,
        "external_retrieval_receipt": external_retrieval_receipt,
        "canonical_fact_binding": canonical_fact_binding,
        "actions": actions,
        "unconsumed_action_ids": unconsumed,
        "additional_context": additional_context,
        "context_char_count": len(additional_context),
        "context_soft_target_chars": CONTEXT_SOFT_TARGET_CHARS,
        "context_over_soft_target": len(additional_context) > CONTEXT_SOFT_TARGET_CHARS,
        "continuity_control_in_first_page": continuity_control_in_first_page,
        "context_segments": context_segments,
        "source_meta_index": str(meta_path),
        "dynamic_memory_sources": dynamic_source_receipt,
        "conversation_navigation": conversation_navigation,
        "memory_source_cascade": memory_source_cascade,
        "effective_route_delta": effective_route_delta,
        "reasoning_activation": {
            key: value for key, value in reasoning_activation.items() if key != "context"
        },
        "reflexive_gap_review": reflexive_gap_review,
    }


def _compact_record_metadata(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    return {
        key: record[key]
        for key in (
            "record_id",
            "family",
            "class",
            "status",
            "record_status",
            "path",
            "source_id",
            "index_path",
            "sha256",
            "source_tag",
            "belief_status",
            "confidence",
            "score",
            "score_method",
            "match_confidence",
            "selection_reasons",
            "eligible_for_current_reuse",
            "linked_record_ids",
            "evidence_handles",
        )
        if key in record
    }


def _compact_runtime_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    external = receipt.get("external_retrieval_receipt")
    if isinstance(external, dict):
        external = {
            key: external[key]
            for key in (
                "schema",
                "status",
                "mode",
                "queries",
                "source_capability_candidates",
                "source_route_plan",
                "negative_evidence_boundary",
            )
            if key in external
        }
    capsule = receipt.get("task_capsule")
    if isinstance(capsule, dict):
        capsule = {
            key: capsule.get(key)
            for key in (
                "schema",
                "capsule_id",
                "task_key_sha256",
                "lifecycle",
                "progress_revision",
                "objective",
                "current_stage",
                "remaining_work",
                "next_action",
                "next_action_reason",
                "blocking_condition",
                "unresolved_failures",
                "persistence",
            )
        }
    transition = receipt.get("task_transition")
    if isinstance(transition, dict):
        transition = {
            key: transition.get(key)
            for key in (
                "schema",
                "changed",
                "previous_lifecycle",
                "lifecycle",
                "progress_delta",
                "event_outcome",
                "transition_reasons",
                "event_type",
            )
        }
    return {
        "schema": "cbh.action_consumption_receipt.compact.v1",
        "output_profile": "compact_runtime",
        "consumer": receipt.get("consumer"),
        "status": receipt.get("status"),
        "routing_status": receipt.get("routing_status"),
        "execution_disposition": receipt.get("execution_disposition"),
        "pending_user_confirmation": receipt.get("pending_user_confirmation"),
        "selected_records": [
            _compact_record_metadata(record) for record in receipt.get("selected_records", [])
        ],
        "semantic_review_candidates": [
            _compact_record_metadata(record)
            for record in receipt.get("semantic_review_candidates", [])
        ],
        "semantic_review_owner": receipt.get("semantic_review_owner"),
        "retrieval": receipt.get("retrieval", {}),
        "prediction_reviews": [
            {
                key: review[key]
                for key in (
                    "record_id",
                    "applicability_status",
                    "evidence_target",
                    "lifecycle",
                    "match_confidence",
                    "selection_reasons",
                    "source_path",
                    "source_paths",
                    "source_record_ids",
                    "source_sha256",
                )
                if key in review
            }
            for review in receipt.get("prediction_reviews", [])
            if isinstance(review, dict)
        ],
        "task_local_correction_bundle": receipt.get("task_local_correction_bundle"),
        "task_continuity_decision": receipt.get("task_continuity_decision"),
        "task_capsule": capsule,
        "task_transition": transition,
        "dynamic_reminders": receipt.get("dynamic_reminders", []),
        "engineering_execution_receipt": receipt.get("engineering_execution_receipt"),
        "transport_receipt": receipt.get("transport_receipt"),
        "external_retrieval_receipt": external,
        "canonical_fact_binding": receipt.get("canonical_fact_binding"),
        "actions": receipt.get("actions", []),
        "unconsumed_action_ids": receipt.get("unconsumed_action_ids", []),
        "additional_context": receipt.get("additional_context", ""),
        "context_char_count": receipt.get("context_char_count", 0),
        "context_soft_target_chars": receipt.get("context_soft_target_chars"),
        "context_over_soft_target": receipt.get("context_over_soft_target", False),
        "continuity_control_in_first_page": receipt.get(
            "continuity_control_in_first_page", True
        ),
        "source_meta_index": receipt.get("source_meta_index"),
        "conversation_navigation": receipt.get("conversation_navigation"),
        "memory_source_cascade": receipt.get("memory_source_cascade"),
        "effective_route_delta": receipt.get("effective_route_delta"),
    }


def _load_route(args: argparse.Namespace) -> dict[str, Any]:
    if args.route_json is not None:
        source = args.route_json
    elif args.route_file is not None:
        source = Path(args.route_file).read_text(encoding="utf-8-sig")
    else:
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="strict")
        source = sys.stdin.read()
    route = json.loads(source)
    if not isinstance(route, dict):
        raise ValueError("route input must be a JSON object")
    return route


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    parser = argparse.ArgumentParser(description="Consume CBH route actions without overstating host execution.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--route-json")
    source.add_argument("--route-file")
    source.add_argument("--route-stdin", action="store_true")
    parser.add_argument("--receipt-mode", choices=("compact", "diagnostic"), default="compact")
    parser.add_argument("--prompt", default="")
    parser.add_argument("--tool-input-text", default="")
    parser.add_argument(
        "--query-type",
        choices=sorted(MEMORY_QUERY_TYPES),
        default="history_reason",
    )
    args = parser.parse_args()
    route = _load_route(args)
    receipt = build_action_consumption(
        route,
        prompt=args.prompt,
        tool_input_text=args.tool_input_text,
        query_type=args.query_type,
    )
    if args.receipt_mode == "compact":
        receipt = _compact_runtime_receipt(receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
