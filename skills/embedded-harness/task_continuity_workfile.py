"""Deterministic, append-only workfile persistence for CBH task continuity.

This module is an explicit opt-in adapter around ``task_continuity``.  It does
not discover workfiles, perform semantic retrieval, replay side effects, or
grant authority.  A caller supplies one exact path and one host-task identity.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


RECORD_SCHEMA = "cbh.cumcwork_record.v1"
REHYDRATE_SCHEMA = "cbh.cumcwork_rehydrate.v1"
APPEND_SCHEMA = "cbh.cumcwork_append.v1"
REPAIR_SCHEMA = "cbh.cumcwork_repair.v1"

_CAPSULE_KEYS = {
    "schema",
    "capsule_id",
    "working_set_id",
    "task_key_sha256",
    "host_task_key_sha256",
    "state_version",
    "lifecycle",
    "activation_reasons",
    "objective",
    "purpose",
    "required_outputs",
    "stop_condition",
    "non_goals",
    "goal_revision",
    "goal_deltas",
    "last_user_delta",
    "semantic_review_required",
    "supersedes_capsule_id",
    "retirement",
    "memory_working_set",
    "acceptance_criteria",
    "plan_steps",
    "constraints",
    "current_stage",
    "verified_completed",
    "inferred_progress",
    "unknown_progress",
    "current_step",
    "current_action",
    "remaining_work",
    "next_action",
    "next_action_reason",
    "blocking_condition",
    "last_postcondition",
    "unresolved_failures",
    "progress_revision",
    "execution_log_cursor",
    "evidence_refs",
    "active_frames",
    "global_goal_anchor",
    "active_local_delta",
    "turn_relation",
    "suspended_task_stack",
    "reuse_candidates",
    "workspace_anchor",
    "workspace_review_required",
    "workspace_review_reason",
    "context_epoch",
    "legacy_projection",
    "transport",
    "resume_entry",
    "applied_event_ids",
    "last_event",
    "reminder_state",
    "persistence",
    "persistence_scope",
}
_REMINDER_KEYS = {
    "schema",
    "trigger",
    "required_action",
    "expires_when",
    "dedupe_key",
    "context_epoch_id",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_bytes(value: Any) -> bytes:
    return _canonical_json(value).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_copy(value: Any) -> Any:
    return json.loads(_canonical_json(value))


def _sanitize_capsule(capsule: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(capsule, Mapping) or capsule.get("schema") != "cbh.task_capsule.v1":
        raise ValueError("cumcwork_invalid_capsule")
    sanitized = {
        key: copy.deepcopy(capsule[key])
        for key in _CAPSULE_KEYS
        if key in capsule
    }
    sanitized["authority"] = {
        "granted": False,
        "consumed": False,
        "source": "none",
    }
    return _json_copy(sanitized)


def _sanitize_reminders(reminders: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(reminders, Sequence) or isinstance(reminders, (str, bytes)):
        raise ValueError("cumcwork_invalid_pending_reminders")
    return [
        _json_copy({key: item[key] for key in _REMINDER_KEYS if key in item})
        for item in reminders
        if isinstance(item, Mapping)
    ][:8]


def _focus_from_capsule(capsule: Mapping[str, Any]) -> dict[str, Any]:
    focus = {
        "schema": "cbh.cumcwork_focus.v1",
        "capsule_id": capsule.get("capsule_id"),
        "lifecycle": capsule.get("lifecycle"),
        "progress_revision": capsule.get("progress_revision"),
        "goal_revision": capsule.get("goal_revision"),
        "context_epoch_id": (
            capsule.get("context_epoch", {}).get("epoch_id")
            if isinstance(capsule.get("context_epoch"), Mapping)
            else None
        ),
        "workspace_review_required": bool(capsule.get("workspace_review_required")),
        "objective": capsule.get("objective"),
        "purpose": capsule.get("purpose"),
        "required_outputs": copy.deepcopy(capsule.get("required_outputs") or []),
        "acceptance_criteria": copy.deepcopy(capsule.get("acceptance_criteria") or []),
        "plan_steps": copy.deepcopy(capsule.get("plan_steps") or []),
        "current_stage": capsule.get("current_stage"),
        "current_step": capsule.get("current_step"),
        "current_action": copy.deepcopy(capsule.get("current_action")),
        "next_action": capsule.get("next_action"),
        "next_action_reason": capsule.get("next_action_reason"),
        "blocking_condition": capsule.get("blocking_condition"),
        "stop_condition": capsule.get("stop_condition"),
        "semantic_review_required": bool(capsule.get("semantic_review_required")),
    }
    if int(capsule.get("state_version") or 0) >= 3 and isinstance(
        capsule.get("global_goal_anchor"), Mapping
    ):
        focus["global_goal_anchor"] = copy.deepcopy(capsule["global_goal_anchor"])
    return focus


def _snapshot(
    capsule: Mapping[str, Any], pending_reminders: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    return {
        "capsule": _sanitize_capsule(capsule),
        "pending_reminders": _sanitize_reminders(pending_reminders),
    }


def _record(
    snapshot: Mapping[str, Any],
    *,
    work_revision: int,
    parent_head_sha256: str | None,
) -> dict[str, Any]:
    capsule = snapshot["capsule"]
    host_task_key_sha256 = str(capsule.get("host_task_key_sha256") or "")
    if not host_task_key_sha256:
        raise ValueError("cumcwork_missing_host_task_identity")
    snapshot_value = _json_copy(snapshot)
    base = {
        "schema": RECORD_SCHEMA,
        "kind": "snapshot_commit",
        "host_task_key_sha256": host_task_key_sha256,
        "work_revision": work_revision,
        "parent_head_sha256": parent_head_sha256,
        "source_event": copy.deepcopy(capsule.get("last_event")),
        "snapshot_sha256": _sha256_bytes(_canonical_bytes(snapshot_value)),
        "snapshot": snapshot_value,
        "focus": _focus_from_capsule(capsule),
    }
    return {**base, "record_sha256": _sha256_bytes(_canonical_bytes(base))}


def _validate_record(
    record: Any,
    raw_line: bytes,
    *,
    expected_scope: str,
    expected_revision: int,
    expected_parent: str | None,
) -> None:
    if not isinstance(record, Mapping) or record.get("schema") != RECORD_SCHEMA:
        raise ValueError("invalid_record_schema")
    if record.get("kind") != "snapshot_commit":
        raise ValueError("invalid_record_kind")
    if record.get("host_task_key_sha256") != expected_scope:
        raise ValueError("cumcwork_scope_mismatch")
    if record.get("work_revision") != expected_revision:
        raise ValueError("invalid_work_revision")
    if record.get("parent_head_sha256") != expected_parent:
        raise ValueError("invalid_parent_head")
    if _canonical_bytes(record) != raw_line:
        raise ValueError("noncanonical_record")
    record_without_hash = {
        key: copy.deepcopy(value)
        for key, value in record.items()
        if key != "record_sha256"
    }
    if record.get("record_sha256") != _sha256_bytes(_canonical_bytes(record_without_hash)):
        raise ValueError("invalid_record_hash")
    snapshot = record.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("invalid_snapshot")
    if record.get("snapshot_sha256") != _sha256_bytes(_canonical_bytes(snapshot)):
        raise ValueError("invalid_snapshot_hash")
    capsule = snapshot.get("capsule")
    if not isinstance(capsule, Mapping):
        raise ValueError("invalid_snapshot_capsule")
    if record.get("focus") != _focus_from_capsule(capsule):
        raise ValueError("invalid_focus_projection")


def _empty_result(status: str, *, file_size: int = 0) -> dict[str, Any]:
    return {
        "schema": REHYDRATE_SCHEMA,
        "status": status,
        "head_sha256": None,
        "work_revision": 0,
        "snapshot_sha256": None,
        "latest_capsule": None,
        "active_capsule": None,
        "pending_reminders": [],
        "focus": None,
        "valid_bytes": 0,
        "file_size": file_size,
        "repair_token": None,
        "capsule_history": [],
        "record_hashes": [],
        "chain_digest": None,
    }


def _rehydrate_cumcwork_bytes(
    raw: bytes,
    *,
    expected_host_task_key_sha256: str,
) -> dict[str, Any]:
    """Parse one complete workfile image without performing another read."""

    if not raw:
        return _empty_result("empty")
    last_lf = raw.rfind(b"\n")
    committed = raw[: last_lf + 1] if last_lf >= 0 else b""
    tail = raw[last_lf + 1 :] if last_lf >= 0 else raw
    records: list[Mapping[str, Any]] = []
    head: str | None = None
    for revision, physical_line in enumerate(committed.splitlines(keepends=True), start=1):
        if not physical_line.endswith(b"\n") or physical_line.endswith(b"\r\n"):
            raise ValueError("cumcwork_committed_corruption:invalid_line_ending")
        raw_line = physical_line[:-1]
        try:
            record = json.loads(raw_line.decode("utf-8", errors="strict"))
            _validate_record(
                record,
                raw_line,
                expected_scope=expected_host_task_key_sha256,
                expected_revision=revision,
                expected_parent=head,
            )
        except ValueError as exc:
            if str(exc) == "cumcwork_scope_mismatch":
                raise
            raise ValueError(f"cumcwork_committed_corruption:{exc}") from exc
        records.append(record)
        head = str(record["record_sha256"])
    if not records and not tail:
        return _empty_result("empty", file_size=len(raw))
    latest = records[-1] if records else None
    latest_snapshot = latest.get("snapshot") if latest else None
    latest_capsule = latest_snapshot.get("capsule") if isinstance(latest_snapshot, Mapping) else None
    reminders = (
        latest_snapshot.get("pending_reminders")
        if isinstance(latest_snapshot, Mapping)
        else []
    )
    status = "tail_repair_required" if tail else "clean"
    valid_bytes = len(committed)
    repair_token = None
    if tail:
        repair_token = {
            "host_task_key_sha256": expected_host_task_key_sha256,
            "observed_file_size": len(raw),
            "valid_bytes": valid_bytes,
            "valid_prefix_sha256": _sha256_bytes(committed),
            "tail_sha256": _sha256_bytes(tail),
            "head_sha256": head,
            "work_revision": len(records),
        }
    capsule_order: list[str] = []
    capsules_by_id: dict[str, Mapping[str, Any]] = {}
    record_hashes = [str(record["record_sha256"]) for record in records]
    for record in records:
        snapshot = record.get("snapshot")
        record_capsule = snapshot.get("capsule") if isinstance(snapshot, Mapping) else None
        capsule_id = str(record_capsule.get("capsule_id") or "") if isinstance(record_capsule, Mapping) else ""
        if not capsule_id:
            continue
        if capsule_id not in capsules_by_id:
            capsule_order.append(capsule_id)
        capsules_by_id[capsule_id] = record_capsule
    capsule_history = [
        _json_copy(capsules_by_id[capsule_id]) for capsule_id in capsule_order
    ]
    return {
        "schema": REHYDRATE_SCHEMA,
        "status": status,
        "head_sha256": head,
        "work_revision": len(records),
        "snapshot_sha256": latest.get("snapshot_sha256") if latest else None,
        "latest_capsule": _json_copy(latest_capsule) if latest_capsule else None,
        "active_capsule": (
            _json_copy(latest_capsule)
            if latest_capsule and latest_capsule.get("lifecycle") != "RETIRED"
            else None
        ),
        "pending_reminders": _json_copy(reminders or []),
        "focus": _json_copy(latest.get("focus")) if latest else None,
        "valid_bytes": valid_bytes,
        "file_size": len(raw),
        "repair_token": repair_token,
        "capsule_history": capsule_history,
        "record_hashes": record_hashes,
        "chain_digest": (
            _sha256_bytes(_canonical_bytes(record_hashes)) if record_hashes else None
        ),
    }


def rehydrate_cumcwork(
    path: Path,
    *,
    expected_host_task_key_sha256: str,
) -> dict[str, Any]:
    """Return the last complete, chain-valid snapshot at one exact path."""

    work_path = Path(path)
    if not work_path.exists():
        return _empty_result("not_found")
    return _rehydrate_cumcwork_bytes(
        work_path.read_bytes(),
        expected_host_task_key_sha256=expected_host_task_key_sha256,
    )


def _acquire_exclusive_lock(stream: Any, *, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        while True:
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError("cumcwork_lock_timeout") from exc
                time.sleep(0.01)
    else:
        import fcntl

        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError("cumcwork_lock_timeout") from exc
                time.sleep(0.01)


def _release_exclusive_lock(stream: Any) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class CumcworkTransaction:
    """One locked read/compare/append cycle for one exact workfile."""

    def __init__(
        self,
        path: Path,
        stream: Any,
        expected_host_task_key_sha256: str,
        state: Mapping[str, Any],
    ) -> None:
        self.path = Path(path)
        self._stream = stream
        self.expected_host_task_key_sha256 = expected_host_task_key_sha256
        self._state = _json_copy(state)

    @property
    def state(self) -> dict[str, Any]:
        return _json_copy(self._state)

    def append_snapshot(
        self,
        capsule: Mapping[str, Any],
        pending_reminders: Sequence[Mapping[str, Any]] = (),
        *,
        expected_head_sha256: str | None,
        expected_work_revision: int,
    ) -> dict[str, Any]:
        snapshot = _snapshot(capsule, pending_reminders)
        host_scope = str(snapshot["capsule"].get("host_task_key_sha256") or "")
        if not host_scope:
            raise ValueError("cumcwork_missing_host_task_identity")
        if host_scope != self.expected_host_task_key_sha256:
            raise ValueError("cumcwork_scope_mismatch")
        current = self._state
        if current["status"] == "tail_repair_required":
            raise ValueError("cumcwork_tail_repair_required")
        if current["head_sha256"] != expected_head_sha256:
            raise ValueError("cumcwork_stale_head")
        if current["work_revision"] != expected_work_revision:
            raise ValueError("cumcwork_stale_revision")
        latest_capsule = current.get("latest_capsule")
        if (
            isinstance(latest_capsule, Mapping)
            and latest_capsule.get("capsule_id") != snapshot["capsule"].get("capsule_id")
            and latest_capsule.get("lifecycle") != "RETIRED"
            and snapshot["capsule"].get("supersedes_capsule_id")
            != latest_capsule.get("capsule_id")
        ):
            raise ValueError("cumcwork_active_capsule_switch")
        snapshot_sha256 = _sha256_bytes(_canonical_bytes(snapshot))
        if snapshot_sha256 == current.get("snapshot_sha256"):
            return {
                "schema": APPEND_SCHEMA,
                "changed": False,
                "head_sha256": current["head_sha256"],
                "work_revision": current["work_revision"],
                "snapshot_sha256": snapshot_sha256,
            }
        record = _record(
            snapshot,
            work_revision=expected_work_revision + 1,
            parent_head_sha256=expected_head_sha256,
        )
        raw_record = _canonical_bytes(record)
        _validate_record(
            record,
            raw_record,
            expected_scope=host_scope,
            expected_revision=expected_work_revision + 1,
            expected_parent=expected_head_sha256,
        )
        encoded = raw_record + b"\n"
        prior_size = int(current.get("file_size") or 0)
        self._stream.seek(0, os.SEEK_END)
        if self._stream.tell() != prior_size:
            raise ValueError("cumcwork_locked_size_changed")
        view = memoryview(encoded)
        while view:
            written = self._stream.write(view)
            if written is None or written <= 0:
                raise OSError("cumcwork_append_failed")
            view = view[written:]
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._stream.seek(prior_size)
        observed = self._stream.read(len(encoded) + 1)
        if observed != encoded:
            raise ValueError("cumcwork_postwrite_verification_failed")
        latest_capsule = snapshot["capsule"]
        capsule_history = list(current.get("capsule_history") or [])
        history_index = next(
            (
                index
                for index, item in enumerate(capsule_history)
                if isinstance(item, Mapping)
                and item.get("capsule_id") == latest_capsule.get("capsule_id")
            ),
            None,
        )
        if history_index is None:
            capsule_history.append(_json_copy(latest_capsule))
        else:
            capsule_history[history_index] = _json_copy(latest_capsule)
        record_hashes = [
            *list(current.get("record_hashes") or []),
            record["record_sha256"],
        ]
        new_size = prior_size + len(encoded)
        self._state = {
            "schema": REHYDRATE_SCHEMA,
            "status": "clean",
            "head_sha256": record["record_sha256"],
            "work_revision": expected_work_revision + 1,
            "snapshot_sha256": record["snapshot_sha256"],
            "latest_capsule": _json_copy(latest_capsule),
            "active_capsule": (
                _json_copy(latest_capsule)
                if latest_capsule.get("lifecycle") != "RETIRED"
                else None
            ),
            "pending_reminders": _json_copy(snapshot["pending_reminders"]),
            "focus": _json_copy(record["focus"]),
            "valid_bytes": new_size,
            "file_size": new_size,
            "repair_token": None,
            "capsule_history": capsule_history,
            "record_hashes": record_hashes,
            "chain_digest": _sha256_bytes(_canonical_bytes(record_hashes)),
        }
        return {
            "schema": APPEND_SCHEMA,
            "changed": True,
            "head_sha256": record["record_sha256"],
            "work_revision": expected_work_revision + 1,
            "snapshot_sha256": record["snapshot_sha256"],
        }


@contextmanager
def cumcwork_transaction(
    path: Path,
    *,
    expected_host_task_key_sha256: str,
) -> Iterator[CumcworkTransaction]:
    """Hold the single-writer lock across one scan, transition, and append."""

    work_path = Path(path)
    work_path.parent.mkdir(parents=True, exist_ok=True)
    initially_existed = work_path.exists()
    stream = work_path.open("a+b", buffering=0)
    locked = False
    try:
        _acquire_exclusive_lock(stream)
        locked = True
        stream.seek(0)
        raw = stream.read()
        state = (
            _rehydrate_cumcwork_bytes(
                raw,
                expected_host_task_key_sha256=expected_host_task_key_sha256,
            )
            if raw or initially_existed
            else _empty_result("not_found")
        )
        yield CumcworkTransaction(
            work_path,
            stream,
            expected_host_task_key_sha256,
            state,
        )
    finally:
        if locked:
            _release_exclusive_lock(stream)
        stream.close()


def append_cumcwork_snapshot(
    path: Path,
    capsule: Mapping[str, Any],
    pending_reminders: Sequence[Mapping[str, Any]] = (),
    *,
    expected_head_sha256: str | None,
    expected_work_revision: int,
) -> dict[str, Any]:
    """CAS-append one snapshot; an identical snapshot is a zero-write no-op."""

    sanitized_capsule = _sanitize_capsule(capsule)
    host_scope = str(sanitized_capsule.get("host_task_key_sha256") or "")
    if not host_scope:
        raise ValueError("cumcwork_missing_host_task_identity")
    with cumcwork_transaction(
        path,
        expected_host_task_key_sha256=host_scope,
    ) as transaction:
        return transaction.append_snapshot(
            capsule,
            pending_reminders,
            expected_head_sha256=expected_head_sha256,
            expected_work_revision=expected_work_revision,
        )


def repair_cumcwork_tail(
    path: Path,
    *,
    repair_token: Mapping[str, Any],
) -> dict[str, Any]:
    """Truncate only the exact uncommitted EOF suffix bound by a scan token."""

    if not isinstance(repair_token, Mapping):
        raise ValueError("cumcwork_invalid_repair_token")
    work_path = Path(path)
    raw = work_path.read_bytes()
    valid_bytes = int(repair_token.get("valid_bytes", -1))
    if valid_bytes < 0 or valid_bytes > len(raw):
        raise ValueError("cumcwork_repair_token_mismatch")
    prefix = raw[:valid_bytes]
    tail = raw[valid_bytes:]
    checks = {
        "observed_file_size": len(raw),
        "valid_prefix_sha256": _sha256_bytes(prefix),
        "tail_sha256": _sha256_bytes(tail),
    }
    if any(repair_token.get(key) != value for key, value in checks.items()):
        raise ValueError("cumcwork_repair_token_mismatch")
    expected_scope = str(repair_token.get("host_task_key_sha256") or "")
    current = rehydrate_cumcwork(
        work_path,
        expected_host_task_key_sha256=expected_scope,
    )
    if current.get("repair_token") != dict(repair_token):
        raise ValueError("cumcwork_repair_token_mismatch")
    with work_path.open("r+b") as stream:
        stream.truncate(valid_bytes)
        stream.flush()
        os.fsync(stream.fileno())
    restored = rehydrate_cumcwork(
        work_path,
        expected_host_task_key_sha256=expected_scope,
    )
    return {**restored, "schema": REPAIR_SCHEMA}
