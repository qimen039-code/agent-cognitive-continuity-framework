from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_OVERLAY = ROOT / "embedded_harness_policy.local.json"
DEFAULT_CONVERSATION_REGISTRY = ROOT / "active_conversation_memory_registry.json"
DEFAULT_OUTPUT = ROOT / "memory_file_index.local.jsonl"
DEFAULT_LEGACY_LINKS = ROOT / "legacy_memory_links.local.jsonl"
GLOBAL_MEMORY_ROOTS = (
    ("memory-skill:agent-error-memory", ROOT.parent / "agent-error-memory", "global_error_memory"),
    ("memory-skill:bug-solution-memory", ROOT.parent / "bug-solution-memory", "global_solution_memory"),
    ("memory-skill:shared-semantic-anchors", ROOT.parent / "shared-semantic-anchors", "global_semantic_anchor_memory"),
    ("common-error-corpus", ROOT.parent / "troubleshooting-skill-matrix" / "common-error-corpus", "common_error_memory"),
    ("interaction-error-corpus", ROOT.parent / "troubleshooting-skill-matrix" / "interaction-error-corpus", "interaction_error_memory"),
    ("major-incidents", ROOT.parent / "troubleshooting-skill-matrix" / "major-incidents", "major_incident_index"),
    ("legacy-memory-links", DEFAULT_LEGACY_LINKS, "legacy_record_link_manifest"),
)
EXCLUDED_PARTS = {".git", "__pycache__", ".pytest_cache"}
COLD_PARTS = {"archive", "archives", "archived", "backup", "backups", "cold", "quarantine"}
LEDGER_POINTER_SCHEMA = "cbh.ledger_generation_pointer.v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_id(root_id: str, relative_path: str) -> str:
    return f"memory-file:{root_id.casefold()}:{relative_path.casefold()}"


def _is_cold(relative: Path) -> bool:
    if any(part.casefold() in COLD_PARTS for part in relative.parts):
        return True
    name = relative.name.casefold()
    return bool(re.search(r"\.(?:bak|backup|old)(?:[-.]|$)|~$", name))


def _root_file_candidates(
    root_path: Path,
    root_kind: str,
) -> list[tuple[Path, Path]]:
    if not root_path.is_dir():
        return [(root_path, Path(root_path.name))]
    if root_kind != "conversation_ledger":
        return [
            (path, path.relative_to(root_path))
            for path in sorted(root_path.rglob("*"), key=lambda item: str(item).casefold())
        ]
    pointer_path = root_path / "CURRENT.json"
    if not pointer_path.is_file():
        return [
            (path, path.relative_to(root_path))
            for path in sorted(root_path.rglob("*"), key=lambda item: str(item).casefold())
        ]
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8-sig", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("conversation_ledger_pointer_unreadable") from exc
    if pointer.get("schema") != LEDGER_POINTER_SCHEMA:
        raise ValueError("conversation_ledger_pointer_schema_invalid")
    relative_path = str(pointer.get("relative_path") or "")
    if not relative_path:
        raise ValueError("conversation_ledger_pointer_path_missing")
    active_generation = (root_path / relative_path).resolve()
    try:
        active_generation.relative_to(root_path.resolve())
    except ValueError as exc:
        raise ValueError("conversation_ledger_pointer_outside_root") from exc
    if not active_generation.is_dir():
        raise ValueError("conversation_ledger_generation_missing")
    candidates = [(pointer_path, Path("CURRENT.json"))]
    candidates.extend(
        (path, Path("active") / path.relative_to(active_generation))
        for path in sorted(active_generation.rglob("*"), key=lambda item: str(item).casefold())
    )
    return candidates


def build_memory_file_records(roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_roots: set[str] = set()
    for root in roots:
        root_id = str(root.get("root_id") or "").strip()
        root_path = Path(str(root.get("path") or ""))
        root_kind = str(root.get("kind") or "memory_root")
        root_retrieval_eligible = root.get("retrieval_eligible") is not False
        registry_state = str(root.get("registry_state") or "") or None
        chain_head = root.get("chain_head") if "chain_head" in root else None
        if not root_id or not root_path.exists():
            continue
        resolved_key = str(root_path.resolve()).casefold()
        if resolved_key in seen_roots:
            continue
        seen_roots.add(resolved_key)
        candidates = _root_file_candidates(root_path, root_kind)
        for path, relative in candidates:
            if any(part.casefold() in EXCLUDED_PARTS for part in relative.parts):
                continue
            if not path.is_file():
                continue
            relative_text = relative.as_posix()
            file_cold = _is_cold(relative)
            retrieval_eligible = root_retrieval_eligible and not file_cold
            if not root_retrieval_eligible and registry_state == "FROZEN_READONLY":
                lifecycle = "frozen_readonly_reference"
            else:
                lifecycle = "cold_reference" if not retrieval_eligible else "active_locator"
            stat = path.stat()
            records.append(
                {
                    "schema": "cbh.source_locator_registry.v1",
                    "source_id": _source_id(root_id, relative_text),
                    "source_type": "memory_file",
                    "root_id": root_id,
                    "root_kind": root_kind,
                    "relative_path": relative_text,
                    "sha256": _sha256(path),
                    "size_bytes": stat.st_size,
                    "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    "locators": [
                        {
                            "kind": "memory_root",
                            "path": str(path),
                            "status": "verified",
                        }
                    ],
                    "lifecycle": lifecycle,
                    "retrieval_eligible": retrieval_eligible,
                    "registry_state": registry_state,
                    "chain_head": chain_head,
                    "evidence_boundary": "physical_locator_and_hash_not_semantic_fact",
                    "managed_by": "memory_source_index.py",
                }
            )
    return sorted(records, key=lambda item: str(item["source_id"]))


def merge_previous_locators(
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    previous_by_id = {
        str(record.get("source_id") or ""): record
        for record in previous
        if str(record.get("source_id") or "")
    }
    merged: list[dict[str, Any]] = []
    current_ids: set[str] = set()
    for record in current:
        source_id = str(record.get("source_id") or "")
        current_ids.add(source_id)
        prior = previous_by_id.get(source_id) or {}
        locators = [dict(locator) for locator in record.get("locators") or [] if isinstance(locator, dict)]
        known_paths = {str(Path(str(locator.get("path") or ""))).casefold() for locator in locators}
        for locator in prior.get("locators") or []:
            if not isinstance(locator, dict) or not locator.get("path"):
                continue
            key = str(Path(str(locator["path"]))).casefold()
            if key in known_paths:
                continue
            historical = dict(locator)
            historical["status"] = "relocated"
            locators.append(historical)
            known_paths.add(key)
        updated = dict(record)
        updated["locators"] = locators
        merged.append(updated)
    for source_id, record in previous_by_id.items():
        if source_id in current_ids:
            continue
        unresolved = dict(record)
        unresolved["lifecycle"] = "unresolved_previous_locator"
        unresolved["retrieval_eligible"] = False
        unresolved["locators"] = [
            {**locator, "status": "unresolved_previous"}
            for locator in record.get("locators") or []
            if isinstance(locator, dict)
        ]
        merged.append(unresolved)
    return sorted(merged, key=lambda item: str(item.get("source_id") or ""))


def discover_roots(
    overlay_path: Path = DEFAULT_OVERLAY,
    conversation_registry_path: Path = DEFAULT_CONVERSATION_REGISTRY,
) -> list[dict[str, Any]]:
    roots: list[dict[str, Any]] = []
    if conversation_registry_path.is_file():
        registry = json.loads(conversation_registry_path.read_text(encoding="utf-8-sig"))
        for entry in registry.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            memory_id = str(entry.get("memory_id") or "").strip()
            memory_path = str(entry.get("root_path") or "").strip()
            registry_state = str(entry.get("state") or "").strip().upper()
            chain_head = entry.get("chain_head") is True
            if memory_id and memory_path:
                lane_index_path = Path(memory_path) / "index.json"
                if lane_index_path.is_file():
                    lane_index = json.loads(lane_index_path.read_text(encoding="utf-8-sig"))
                    lane_memory_id = str(lane_index.get("memory_id") or "").strip()
                    lane_thread_id = str(lane_index.get("thread_id") or "").strip()
                    registry_thread_id = str(entry.get("thread_id") or "").strip()
                    lane_state = str(
                        lane_index.get("lane_state") or lane_index.get("status") or ""
                    ).strip().upper()
                    successor = str(lane_index.get("active_successor_memory_id") or "").strip()
                    same_identity = lane_memory_id == memory_id and (
                        not registry_thread_id or not lane_thread_id or registry_thread_id == lane_thread_id
                    )
                    if (
                        same_identity
                        and lane_state == "ACTIVE"
                        and not successor
                        and (registry_state != "ACTIVE" or not chain_head)
                    ):
                        raise ValueError(
                            "conversation_registry_lane_state_conflict: "
                            f"{memory_id} is ACTIVE without a successor but registry state is "
                            f"{registry_state or '<empty>'} and chain_head={chain_head}"
                        )
            retrieval_eligible = registry_state == "ACTIVE" and chain_head
            if memory_id and memory_path:
                roots.append(
                    {
                        "root_id": f"conversation-memory:{memory_id}",
                        "path": memory_path,
                        "kind": "conversation_memory",
                        "registry_state": registry_state,
                        "chain_head": chain_head,
                        "retrieval_eligible": retrieval_eligible,
                    }
                )
            ledger_path = str(entry.get("ledger_root_path") or "").strip()
            if memory_id and ledger_path:
                roots.append(
                    {
                        "root_id": f"conversation-ledger:{memory_id}",
                        "path": ledger_path,
                        "kind": "conversation_ledger",
                        "registry_state": registry_state,
                        "chain_head": chain_head,
                        "retrieval_eligible": retrieval_eligible,
                    }
                )
    if overlay_path.is_file():
        overlay = json.loads(overlay_path.read_text(encoding="utf-8-sig"))
        for lane, paths in (overlay.get("memory_roots") or {}).items():
            for path_text in paths if isinstance(paths, list) else []:
                path = Path(str(path_text))
                suffix = path.name or "root"
                roots.append(
                    {
                        "root_id": f"memory-root:{lane}:{suffix}",
                        "path": str(path),
                        "kind": "project_memory",
                    }
                )
    for root_id, path, kind in GLOBAL_MEMORY_ROOTS:
        roots.append({"root_id": root_id, "path": str(path), "kind": kind})
    return roots


def write_index(records: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    payload = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)
    temporary.write_text(payload, encoding="utf-8", errors="strict", newline="\n")
    os.replace(temporary, output_path)


def read_index(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for line_no, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_no}: index record must be an object")
            records.append(value)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or check the private CBH memory-file locator index.")
    parser.add_argument("command", choices=("refresh", "check"), nargs="?", default="check")
    parser.add_argument("--overlay", type=Path, default=DEFAULT_OVERLAY)
    parser.add_argument("--conversation-registry", type=Path, default=DEFAULT_CONVERSATION_REGISTRY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    roots = discover_roots(args.overlay, args.conversation_registry)
    expected = build_memory_file_records(roots)
    current = read_index(args.output)
    expected = merge_previous_locators(expected, current)
    if args.command == "refresh":
        write_index(expected, args.output)
        status = "pass"
        changed = True
    else:
        status = "pass" if current == expected else "stale"
        changed = current != expected
    result = {
        "schema": "cbh.memory_file_index_receipt.v1",
        "status": status,
        "mode": args.command,
        "output_path": str(args.output),
        "root_count": len({record["root_id"] for record in expected}),
        "file_count": len(expected),
        "cold_reference_count": sum(1 for record in expected if not record["retrieval_eligible"]),
        "changed": changed,
        "boundary": "physical locators and hashes only; no memory payload is copied or promoted",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
