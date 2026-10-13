from __future__ import annotations

import sys
import importlib.util
import hashlib
import json
from pathlib import Path

import pytest


HARNESS = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

from evidence_locator import resolve_evidence_source  # noqa: E402
from task_continuity import apply_task_event, new_task_capsule, process_worker_request  # noqa: E402
from task_continuity_workfile import (  # noqa: E402
    append_cumcwork_snapshot,
    rehydrate_cumcwork,
)


def load_runtime_bridge():
    path = HARNESS / "memory_runtime_bridge.py"
    assert path.is_file(), "memory_runtime_bridge is not implemented"
    spec = importlib.util.spec_from_file_location("memory_runtime_bridge", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event(event_id: str, event_type: str, **payload: object) -> dict[str, object]:
    return {
        "schema": "cbh.task_event.v1",
        "event_id": event_id,
        "type": event_type,
        "observed_at": "2026-08-13T00:00:00Z",
        "task_key": "thread-memory-integration",
        **payload,
    }


def route() -> dict[str, object]:
    return {
        "memory_need": "paired_err_sol",
        "action_bindings": [
            {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"}
        ],
    }


def sample_receipt(tmp_path: Path) -> tuple[dict, Path]:
    original = tmp_path / "original.txt"
    archived = tmp_path / "archived.txt"
    archived.write_text("Synthetic public evidence\n", encoding="utf-8")
    handle = {
        "source_id": "public-fixture:evidence",
        "original_path": str(original),
        "line": 1,
        "line_sha256_16": hashlib.sha256(b"Synthetic public evidence").hexdigest()[:16],
    }
    registry = tmp_path / "locators.jsonl"
    registry.write_text(json.dumps({
        "source_id": handle["source_id"],
        "locators": [{"kind": "local_sealed_copy", "path": str(archived)}],
    }) + "\n", encoding="utf-8")
    receipt = {
        "selected_records": [{
            "record_id": "CE-PUBLIC-WORKING-SET-001",
            "summary": "Synthetic selected evidence",
            "source_id": handle["source_id"],
            "evidence_handles": [handle],
        }],
        "retrieval": {"coverage_status": "complete"},
    }
    return receipt, registry


def test_selected_receipt_survives_restart_and_resolves_relocated_evidence(
    tmp_path: Path,
) -> None:
    prompt = "Recall the synthetic public evidence"
    receipt, registry = sample_receipt(tmp_path)
    capsule = new_task_capsule(
        route(),
        event("memory:start", "task_observed", objective=prompt, intent_kind="new_task"),
    )
    updated = apply_task_event(
        capsule,
        event(
            "memory:selected",
            "memory_context_selected",
            memory_query_type="history_reason",
            memory_consumption_receipt=receipt,
        ),
    )["capsule"]

    working_set = updated["memory_working_set"]
    assert working_set["query_type"] == "history_reason"
    assert working_set["coverage_status"] == "complete"
    assert working_set["selected_record_ids"] == [
        "CE-PUBLIC-WORKING-SET-001",
    ]
    assert working_set["unresolved_ambiguity"] is False
    assert len(working_set["evidence_handles"]) == 1

    path = tmp_path / "memory.cumcwork"
    write = append_cumcwork_snapshot(
        path,
        updated,
        expected_head_sha256=None,
        expected_work_revision=0,
    )
    restored = rehydrate_cumcwork(
        path,
        expected_host_task_key_sha256=updated["host_task_key_sha256"],
    )
    assert restored["head_sha256"] == write["head_sha256"]
    assert restored["active_capsule"]["memory_working_set"] == working_set

    handle = working_set["evidence_handles"][0]
    resolved = resolve_evidence_source(
        handle["original_path"],
        source_id=handle["source_id"],
        expected_line=handle["line"],
        expected_line_sha256_16=handle["line_sha256_16"],
        registry_path=registry,
    )
    assert resolved["status"] == "relocated_verified"
    assert Path(resolved["resolved_path"]).is_file()
    opened = apply_task_event(
        restored["active_capsule"],
        event(
            "memory:evidence-opened",
            "memory_evidence_opened",
            evidence_ref={
                key: resolved[key]
                for key in (
                    "source_id",
                    "resolved_path",
                    "status",
                )
            }
            | {
                "line": handle["line"],
                "line_sha256_16": handle["line_sha256_16"],
            },
        ),
    )["capsule"]
    assert opened["memory_working_set"]["opened_evidence_refs"][0]["status"] == (
        "relocated_verified"
    )


def test_vague_incident_recall_stays_candidate_only_in_the_working_set() -> None:
    prompt = "List possible records for the synthetic incident"
    receipt = {
        "selected_records": [],
        "retrieval": {
            "coverage_status": "semantic_review_required",
            "disambiguation_candidates": [{
                "record_id": "CE-PUBLIC-CANDIDATE-001",
                "summary": "Unconfirmed synthetic candidate",
            }],
        },
    }
    capsule = new_task_capsule(
        route(),
        event("vague:start", "task_observed", objective=prompt, intent_kind="new_task"),
    )
    updated = apply_task_event(
        capsule,
        event(
            "vague:candidates",
            "memory_context_selected",
            memory_query_type="history_reason",
            memory_consumption_receipt=receipt,
        ),
    )["capsule"]

    working_set = updated["memory_working_set"]
    assert working_set["selected_record_ids"] == []
    assert working_set["candidate_views"][0]["record_id"] == (
        "CE-PUBLIC-CANDIDATE-001"
    )
    assert working_set["unresolved_ambiguity"] is True
    assert working_set["evidence_handles"] == []
    assert working_set["opened_evidence_refs"] == []


def test_runtime_bridge_binds_selection_and_retains_opened_evidence_at_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = load_runtime_bridge()
    prompt = "Recall and verify the synthetic public evidence"
    receipt, registry = sample_receipt(tmp_path)
    monkeypatch.setattr(bridge, "build_action_consumption", lambda *args, **kwargs: receipt)
    active = new_task_capsule(
        route(),
        event(
            "bridge:start",
            "task_observed",
            objective=prompt,
            intent_kind="new_task",
            acceptance_criteria=[
                {"id": "memory-loop-complete", "text": "历史证据已核验并保留引用"}
            ],
        ),
    )
    path = tmp_path / "bridge.cumcwork"
    append_cumcwork_snapshot(
        path,
        active,
        expected_head_sha256=None,
        expected_work_revision=0,
    )

    result = bridge.bind_memory_query_to_workfile(
        path,
        expected_host_task_key_sha256=active["host_task_key_sha256"],
        route=route(),
        prompt=prompt,
        event_id="bridge:memory",
        query_type="history_reason",
    )

    assert result["status"] == "bound"
    assert result["work_revision"] == 2
    assert result["selected_record_ids"][0] == (
        "CE-PUBLIC-WORKING-SET-001"
    )
    restored = rehydrate_cumcwork(
        path,
        expected_host_task_key_sha256=active["host_task_key_sha256"],
    )
    assert restored["active_capsule"]["memory_working_set"]["receipt_sha256"] == (
        result["receipt_sha256"]
    )
    handle = result["evidence_handles"][0]
    resolved = resolve_evidence_source(
        handle["original_path"],
        source_id=handle["source_id"],
        expected_line=handle["line"],
        expected_line_sha256_16=handle["line_sha256_16"],
        registry_path=registry,
    )
    opened = bridge.record_opened_evidence_to_workfile(
        path,
        expected_host_task_key_sha256=active["host_task_key_sha256"],
        event_id="bridge:evidence-opened",
        evidence_ref={
            "source_id": resolved["source_id"],
            "resolved_path": resolved["resolved_path"],
            "status": resolved["status"],
            "line": handle["line"],
            "line_sha256_16": handle["line_sha256_16"],
        },
    )
    assert opened["status"] == "recorded"
    assert opened["work_revision"] == 3
    final_state = rehydrate_cumcwork(
        path,
        expected_host_task_key_sha256=active["host_task_key_sha256"],
    )
    assert final_state["active_capsule"]["memory_working_set"]["opened_evidence_refs"] == [
        opened["opened_evidence_ref"]
    ]
    retired = process_worker_request(
        {
            "op": "observe",
            "route_receipt": route(),
            "task_event": event(
                "bridge:verified",
                "verifier_completed",
                acceptance_id="memory-loop-complete",
                postcondition_satisfied=True,
                evidence_refs=[opened["opened_evidence_ref"]],
            ),
            "capsule": None,
            "workfile": {"path": str(path)},
        }
    )
    assert retired["capsule"]["lifecycle"] == "RETIRED"
    sealed = rehydrate_cumcwork(
        path,
        expected_host_task_key_sha256=active["host_task_key_sha256"],
    )
    assert sealed["active_capsule"] is None
    assert sealed["latest_capsule"]["memory_working_set"]["opened_evidence_refs"] == [
        opened["opened_evidence_ref"]
    ]
    assert sealed["latest_capsule"]["retirement"]["evidence_refs"] == [
        opened["opened_evidence_ref"]
    ]


def test_runtime_bridge_rejects_empty_global_goal_before_memory_consumption(
    tmp_path: Path,
) -> None:
    bridge = load_runtime_bridge()
    active = new_task_capsule(
        route(),
        event(
            "empty-memory:start",
            "task_observed",
            objective="临时有效目标",
            continuity_requested=True,
        ),
    )
    active["objective"] = ""
    active["global_goal_anchor"]["objective"] = ""
    path = tmp_path / "empty-global-goal.cumcwork"
    append_cumcwork_snapshot(
        path,
        active,
        expected_head_sha256=None,
        expected_work_revision=0,
    )

    with pytest.raises(ValueError, match="global_goal_query_requires_nonempty_objective"):
        bridge.bind_memory_query_to_workfile(
            path,
            expected_host_task_key_sha256=active["host_task_key_sha256"],
            route=route(),
            prompt="当前问题",
            event_id="empty-memory:query",
            query_basis="global_goal",
        )
