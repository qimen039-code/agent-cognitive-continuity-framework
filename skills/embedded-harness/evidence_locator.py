from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_REGISTRY = ROOT / "source_locator_registry.local.jsonl"
DEFAULT_MEMORY_FILE_INDEX = ROOT / "memory_file_index.local.jsonl"
SESSION_ID_PATTERN = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
LOCATOR_PRIORITY = {
    "original_path": 0,
    "codex_active": 1,
    "codex_archive": 2,
    "local_sealed_copy": 3,
    "registry_path": 4,
}


def session_id_from_source(value: str | Path) -> str:
    match = SESSION_ID_PATTERN.search(str(value))
    return match.group(1).lower() if match else ""


def source_id_for_path(
    path: Path | str,
    *,
    index_path: Path = DEFAULT_MEMORY_FILE_INDEX,
) -> str:
    target = str(Path(path)).casefold()
    matches: set[str] = set()
    for record in _registry_records(index_path):
        for locator in record.get("locators") or []:
            if not isinstance(locator, dict):
                continue
            if str(Path(str(locator.get("path") or ""))).casefold() == target:
                source_id = str(record.get("source_id") or "")
                if source_id:
                    matches.add(source_id)
    return next(iter(matches)) if len(matches) == 1 else ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _line_sha256_16(path: Path, line_no: int) -> str:
    if line_no < 1:
        raise ValueError("line number must be positive")
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for current, raw in enumerate(handle, start=1):
            if current == line_no:
                return hashlib.sha256(raw.rstrip("\n").encode("utf-8")).hexdigest()[:16]
    raise ValueError(f"line out of range: {path}:{line_no}")


def _registry_records(path: Path | None) -> list[dict[str, Any]]:
    paths: list[Path] = []
    if path is not None:
        paths.append(path)
        if path == DEFAULT_REGISTRY and DEFAULT_MEMORY_FILE_INDEX.is_file():
            paths.append(DEFAULT_MEMORY_FILE_INDEX)
    records: list[dict[str, Any]] = []
    for source_path in paths:
        if not source_path.is_file():
            continue
        with source_path.open("r", encoding="utf-8", errors="strict") as handle:
            for line_no, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                record = json.loads(raw)
                if not isinstance(record, dict):
                    raise ValueError(f"{source_path}:{line_no}: locator record must be an object")
                records.append(record)
    return records


def _default_codex_candidates(original_path: Path, source_id: str) -> list[dict[str, str]]:
    session_id = session_id_from_source(source_id) or session_id_from_source(original_path)
    if not session_id:
        return []
    codex_home = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".codex"
    filename = original_path.name or f"rollout-{session_id}.jsonl"
    archive_path = codex_home / "archived_sessions" / filename
    candidates = [
        {
            "kind": "codex_archive",
            "path": str(archive_path),
            "status": "candidate",
        }
    ]
    # Active sessions are date-partitioned. Search only after the recorded path
    # moved and only for the stable session id.
    sessions_root = codex_home / "sessions"
    if not original_path.is_file() and not archive_path.is_file() and sessions_root.is_dir():
        for path in sessions_root.rglob(f"*{session_id}*.jsonl"):
            candidates.append({"kind": "codex_active", "path": str(path), "status": "candidate"})
    return candidates


def resolve_evidence_source(
    original_path: Path | str,
    *,
    source_id: str = "",
    expected_line: int | None = None,
    expected_line_sha256_16: str = "",
    expected_sha256: str = "",
    registry_path: Path | str | None = DEFAULT_REGISTRY,
    verify_registry_hash: bool = True,
) -> dict[str, Any]:
    """Resolve a stable source identity to a verified current local path.

    Paths are mutable locators. A relocation is accepted only when the supplied
    content anchor, or a registry content hash, verifies the candidate.
    """

    original = Path(original_path)
    normalized_source_id = source_id or (
        f"codex-session:{session_id_from_source(original)}"
        if session_id_from_source(original)
        else f"local-file:{str(original).casefold()}"
    )
    registry = Path(registry_path) if registry_path else None
    all_registry_records = _registry_records(registry)
    records = [
        item
        for item in all_registry_records
        if str(item.get("source_id") or "").casefold() == normalized_source_id.casefold()
    ]
    match_basis = "source_id"
    if expected_sha256:
        hash_records = [
            item
            for item in all_registry_records
            if str(item.get("sha256") or "").casefold() == expected_sha256.casefold()
        ]
        known = {id(item) for item in records}
        records.extend(item for item in hash_records if id(item) not in known)
        if hash_records and not any(
            str(item.get("source_id") or "").casefold() == normalized_source_id.casefold()
            for item in hash_records
        ):
            match_basis = "content_hash_fallback"
    registry_hashes = {
        str(item.get("sha256") or "").casefold() for item in records if item.get("sha256")
    }
    required_full_hash = expected_sha256.casefold()
    if (
        verify_registry_hash
        and not required_full_hash
        and not expected_line_sha256_16
        and len(registry_hashes) == 1
    ):
        required_full_hash = next(iter(registry_hashes))

    candidates: list[dict[str, str]] = [
        {"kind": "original_path", "path": str(original), "status": "recorded"}
    ]
    for record in records:
        for locator in record.get("locators") or []:
            if isinstance(locator, dict) and locator.get("path"):
                candidates.append(
                    {
                        "kind": str(locator.get("kind") or "registry_path"),
                        "path": str(locator["path"]),
                        "status": str(locator.get("status") or "candidate"),
                    }
                )
    candidates.extend(_default_codex_candidates(original, normalized_source_id))

    unique: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        key = str(Path(candidate["path"])).casefold()
        if key not in unique or LOCATOR_PRIORITY.get(candidate["kind"], 99) < LOCATOR_PRIORITY.get(
            unique[key]["kind"], 99
        ):
            unique[key] = candidate

    checked: list[dict[str, Any]] = []
    verified: list[dict[str, Any]] = []
    mismatch_seen = False
    for candidate in unique.values():
        path = Path(candidate["path"])
        check: dict[str, Any] = {**candidate, "exists": path.is_file()}
        if not check["exists"]:
            checked.append(check)
            continue
        try:
            if expected_line is not None and expected_line_sha256_16:
                actual_line_hash = _line_sha256_16(path, expected_line)
                check["line_sha256_16"] = actual_line_hash
                check["line_hash_match"] = actual_line_hash.casefold() == expected_line_sha256_16.casefold()
                if not check["line_hash_match"]:
                    mismatch_seen = True
                    checked.append(check)
                    continue
            if required_full_hash:
                actual_hash = _sha256(path)
                check["sha256"] = actual_hash
                check["full_hash_match"] = actual_hash.casefold() == required_full_hash
                if not check["full_hash_match"]:
                    mismatch_seen = True
                    checked.append(check)
                    continue
            check["verified"] = bool(expected_line_sha256_16 or required_full_hash)
            verified.append(check)
            checked.append(check)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            check["error"] = str(exc)
            checked.append(check)

    if not verified:
        return {
            "schema": "cbh.evidence_locator_receipt.v1",
            "status": "hash_mismatch" if mismatch_seen else "not_found",
            "source_id": normalized_source_id,
            "original_path": str(original),
            "resolved_path": "",
            "locator_kind": "",
            "match_basis": match_basis,
            "checked_locators": checked,
        }

    if len(verified) > 1 and not required_full_hash:
        verified_hashes: set[str] = set()
        for item in verified:
            try:
                full_hash = _sha256(Path(str(item["path"])))
            except OSError as exc:
                item["error"] = str(exc)
                continue
            item["sha256"] = full_hash
            verified_hashes.add(full_hash)
        if len(verified_hashes) > 1:
            return {
                "schema": "cbh.evidence_locator_receipt.v1",
                "status": "conflict",
                "source_id": normalized_source_id,
                "original_path": str(original),
                "resolved_path": "",
                "locator_kind": "",
                "match_basis": match_basis,
                "checked_locators": checked,
                "conflicting_sha256": sorted(verified_hashes),
            }

    verified.sort(key=lambda item: LOCATOR_PRIORITY.get(str(item.get("kind")), 99))
    chosen = verified[0]
    return {
        "schema": "cbh.evidence_locator_receipt.v1",
        "status": (
            "original_verified"
            if Path(chosen["path"]) == original and chosen.get("verified")
            else "original_available"
            if Path(chosen["path"]) == original
            else "relocated_verified"
            if chosen.get("verified")
            else "relocated_identity_only"
        ),
        "source_id": normalized_source_id,
        "original_path": str(original),
        "resolved_path": str(Path(chosen["path"])),
        "locator_kind": str(chosen["kind"]),
        "match_basis": match_basis,
        "checked_locators": checked,
    }
