"""Build and validate record-level links for immutable legacy CBH memories."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from semantic_memory import materialize_legacy_link, write_legacy_link_manifest


DEFAULT_OUTPUT = Path(__file__).with_name("legacy_memory_links.local.jsonl")
SKILLS = Path(__file__).resolve().parent.parent
ERROR_SKILL = SKILLS / "agent-error-memory" / "SKILL.md"
ERROR_PAYLOAD = SKILLS / "agent-error-memory" / "references" / "error-points.md"
SOLUTION_SKILL = SKILLS / "bug-solution-memory" / "SKILL.md"
SOLUTION_PAYLOAD = SKILLS / "bug-solution-memory" / "references" / "solution-points.md"
ANCHOR_SKILL = SKILLS / "shared-semantic-anchors" / "SKILL.md"
ANCHOR_PAYLOAD = SKILLS / "shared-semantic-anchors" / "references" / "semantic-anchors.md"
CE_META = SKILLS / "troubleshooting-skill-matrix" / "common-error-corpus" / "_META_INDEX.md"
IE_META = SKILLS / "troubleshooting-skill-matrix" / "interaction-error-corpus" / "_META_INDEX.md"
INC_META = SKILLS / "troubleshooting-skill-matrix" / "major-incidents" / "_META_INDEX.md"


def _table_rows(path: Path, prefix: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for raw in path.read_text(encoding="utf-8-sig", errors="strict").splitlines():
        line = raw.strip()
        if not line.startswith("|") or prefix not in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and cells[0].strip("`").startswith(prefix):
            rows.append(cells)
    return rows


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


def _extract_heading(text: str, record_id: str, level: int) -> str:
    hashes = "#" * level
    match = re.search(
        rf"(?ms)^{re.escape(hashes)}\s+{re.escape(record_id)}\s*$\s*"
        rf"(.*?)(?=^{re.escape(hashes)}\s+|\Z)",
        text,
    )
    if not match:
        raise ValueError(f"legacy_heading_not_found:{record_id}")
    return match.group(1).rstrip()


def _field(section: str, name: str) -> str:
    match = re.search(rf"(?m)^{re.escape(name)}:\s*(.*?)\s*$", section)
    return match.group(1).strip().strip("`") if match else ""


def _semantic_state(value: str) -> str:
    normalized = value.casefold()
    if "contradict" in normalized:
        return "contradicted"
    if "stale" in normalized:
        return "stale"
    if "supersed" in normalized or "do_not" in normalized or "do not" in normalized:
        return "superseded"
    if "histor" in normalized:
        return "historical"
    return "active"


def _query_types(family: str) -> list[str]:
    if family == "INC":
        return ["history_reason", "contradiction_check"]
    return ["current_state", "history_reason", "contradiction_check"]


def _linked_edges(row_text: str) -> list[dict[str, str]]:
    return [
        {"type": "paired_with", "target_id": record_id}
        for record_id in dict.fromkeys(
            re.findall(r"(?:INC|ERR|SOL|CE|IE|ANCHOR)-[A-Z0-9-]+", row_text)
        )
    ]


def _skill_links(
    *,
    family: str,
    memory_class: str,
    index_path: Path,
    payload_path: Path,
    heading_level: int,
) -> list[dict[str, Any]]:
    payload_text = payload_path.read_text(encoding="utf-8-sig", errors="strict")
    links: list[dict[str, Any]] = []
    for cells in _table_rows(index_path, f"{family}-"):
        record_id = cells[0].strip("`")
        section = _extract_heading(payload_text, record_id, heading_level)
        terms = _retrieval_terms(cells[1] if len(cells) > 1 else record_id)
        status = _field(section, "current_status") or _field(section, "status") or "ACTIVE"
        linked = [
            edge
            for edge in _linked_edges(" ".join(cells[2:]))
            if edge["target_id"] != record_id
        ]
        links.append(
            {
                "record_id": record_id,
                "family": family,
                "memory_class": memory_class,
                "state": _semantic_state(status),
                "source_lifecycle": "frozen_readonly",
                "candidate_label": "、".join(terms[:4]) or record_id,
                "summary": f"Legacy {family} record; open the exact verified heading for facts.",
                "retrieval_terms": [record_id, *terms],
                "query_types": _query_types(family),
                "locator": {
                    "path": str(payload_path),
                    "heading": record_id,
                    "heading_level": heading_level,
                },
                "typed_edges": linked,
                "content_schema": _field(section, "schema_version") or "legacy_unversioned",
            }
        )
    return links


def _ce_links() -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    root = CE_META.parent
    for cells in _table_rows(CE_META, "CE-"):
        record_id = cells[0].strip("`")
        payload = root / f"{record_id}.md"
        section = _extract_heading(
            payload.read_text(encoding="utf-8-sig", errors="strict"), record_id, 1
        )
        terms = _retrieval_terms(cells[3] if len(cells) > 3 else record_id)
        status = _field(section, "status") or (cells[2] if len(cells) > 2 else "ACTIVE")
        summary = " ".join(cells[4:6]).strip() if len(cells) > 5 else "Legacy common error."
        links.append(
            {
                "record_id": record_id,
                "family": "CE",
                "memory_class": "common_error",
                "state": _semantic_state(status),
                "source_lifecycle": "frozen_readonly",
                "candidate_label": "、".join(terms[:4]) or record_id,
                "summary": summary[:320],
                "retrieval_terms": [record_id, *terms],
                "query_types": _query_types("CE"),
                "locator": {"path": str(payload), "heading": record_id, "heading_level": 1},
                "typed_edges": [
                    edge
                    for edge in _linked_edges(section)
                    if edge["target_id"] != record_id
                ],
                "content_schema": _field(section, "schema") or "legacy_unversioned",
            }
        )
    return links


def _ie_links() -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    root = IE_META.parent
    for cells in _table_rows(IE_META, "IE-"):
        record_id = cells[0].strip("`")
        matches = list(root.glob(f"*/{record_id}.md"))
        if len(matches) != 1:
            raise ValueError(f"legacy_ie_payload_resolution_failed:{record_id}")
        payload = matches[0]
        section = _extract_heading(
            payload.read_text(encoding="utf-8-sig", errors="strict"), record_id, 1
        )
        terms = _retrieval_terms(cells[3] if len(cells) > 3 else record_id)
        status = _field(section, "status") or (cells[2] if len(cells) > 2 else "ACTIVE")
        summary = cells[4] if len(cells) > 4 else "Legacy interaction error."
        links.append(
            {
                "record_id": record_id,
                "family": "IE",
                "memory_class": "interaction_error",
                "state": _semantic_state(status),
                "source_lifecycle": "frozen_readonly",
                "candidate_label": "、".join(terms[:4]) or record_id,
                "summary": summary[:320],
                "retrieval_terms": [record_id, *terms],
                "query_types": _query_types("IE"),
                "locator": {"path": str(payload), "heading": record_id, "heading_level": 1},
                "typed_edges": [
                    edge
                    for edge in _linked_edges(section)
                    if edge["target_id"] != record_id
                ],
                "content_schema": _field(section, "schema") or "legacy_unversioned",
            }
        )
    return links


def _incident_links() -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    text = INC_META.read_text(encoding="utf-8-sig", errors="strict")
    for cells in _table_rows(INC_META, "INC-"):
        record_id = cells[0].strip("`")
        section = _extract_heading(text, record_id, 2)
        terms = _retrieval_terms(cells[3] if len(cells) > 3 else record_id)
        status = _field(section, "current_status") or (cells[4] if len(cells) > 4 else "HISTORICAL")
        links.append(
            {
                "record_id": record_id,
                "family": "INC",
                "memory_class": "major_incident",
                "state": _semantic_state(status),
                "source_lifecycle": "frozen_readonly",
                "candidate_label": cells[1].strip("`") if len(cells) > 1 else record_id,
                "summary": _field(section, "causal_chain") or "Legacy major incident.",
                "retrieval_terms": [record_id, *terms],
                "query_types": _query_types("INC"),
                "locator": {"path": str(INC_META), "heading": record_id, "heading_level": 2},
                "typed_edges": [
                    edge
                    for edge in _linked_edges(" ".join(cells[5:7]))
                    if edge["target_id"] != record_id
                ],
                "content_schema": _field(section, "schema_version") or "cbh.major_incident.v2",
            }
        )
    return links


def default_legacy_links() -> list[dict[str, Any]]:
    return [
        *_skill_links(
            family="ERR",
            memory_class="error",
            index_path=ERROR_SKILL,
            payload_path=ERROR_PAYLOAD,
            heading_level=2,
        ),
        *_skill_links(
            family="SOL",
            memory_class="solution",
            index_path=SOLUTION_SKILL,
            payload_path=SOLUTION_PAYLOAD,
            heading_level=2,
        ),
        *_skill_links(
            family="ANCHOR",
            memory_class="semantic_anchor",
            index_path=ANCHOR_SKILL,
            payload_path=ANCHOR_PAYLOAD,
            heading_level=3,
        ),
        *_ce_links(),
        *_ie_links(),
        *_incident_links(),
    ]


def build_default_legacy_manifest(path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    links = default_legacy_links()
    receipt = write_legacy_link_manifest(Path(path), links)
    receipt["families"] = dict(Counter(str(link["family"]) for link in links))
    return receipt


def check_legacy_manifest(path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    manifest = Path(path)
    verified = 0
    issues: list[dict[str, str]] = []
    if not manifest.is_file():
        return {"status": "fail", "verified_records": 0, "issues": [{"reason": "not_found"}]}
    for line_no, raw in enumerate(manifest.read_text(encoding="utf-8", errors="strict").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            link = json.loads(raw)
            materialize_legacy_link(link)
            verified += 1
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            issues.append({"line": str(line_no), "reason": str(exc)})
    return {
        "schema": "cbh.semantic_memory_legacy_manifest_check.v1",
        "status": "pass" if not issues else "fail",
        "verified_records": verified,
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("build", "check"))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    result = (
        build_default_legacy_manifest(Path(args.output))
        if args.operation == "build"
        else check_legacy_manifest(Path(args.output))
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("status") in {"written", "pass"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
