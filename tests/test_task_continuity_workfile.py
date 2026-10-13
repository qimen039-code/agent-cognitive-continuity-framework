from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness"
HARNESS = ROOT
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

from task_continuity import (  # noqa: E402
    apply_task_event,
    new_task_capsule,
    process_worker_request,
)


def load_workfile_module():
    path = HARNESS / "task_continuity_workfile.py"
    assert path.is_file(), "task_continuity_workfile module is not implemented"
    spec = importlib.util.spec_from_file_location("task_continuity_workfile", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def task_event(event_id: str, event_type: str, **payload: object) -> dict[str, object]:
    return {
        "schema": "cbh.task_event.v1",
        "event_id": event_id,
        "type": event_type,
        "observed_at": "2026-08-13T00:00:00Z",
        "task_key": "thread-a",
        **payload,
    }


def route() -> dict[str, object]:
    return {
        "edit_operation_profile": "in_place_patch",
        "memory_mode": "none",
        "tool_surface_need": "local_filesystem",
        "action_bindings": [],
    }


def capsule(objective: str = "实现可靠的短期连续记忆") -> dict[str, object]:
    return new_task_capsule(
        route(),
        task_event(
            f"start:{objective}",
            "task_observed",
            objective=objective,
            purpose="准确恢复当前目标与进度",
            stop_condition="验收通过后停止",
            acceptance_criteria=[{"id": "verified", "text": "恢复结果已验证"}],
        ),
    )


def test_snapshot_commit_is_canonical_utf8_and_forms_a_strict_head_chain(tmp_path: Path) -> None:
    workfile = load_workfile_module()
    path = tmp_path / "任务.cumcwork"
    first_capsule = capsule()
    first = workfile.append_cumcwork_snapshot(
        path,
        first_capsule,
        expected_head_sha256=None,
        expected_work_revision=0,
    )
    second_capsule = apply_task_event(
        first_capsule,
        task_event(
            "dispatch:1",
            "tool_dispatched",
            current_step="读取当前实现",
        ),
    )["capsule"]
    second = workfile.append_cumcwork_snapshot(
        path,
        second_capsule,
        expected_head_sha256=first["head_sha256"],
        expected_work_revision=1,
    )

    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.endswith(b"\n")
    assert b"\r\n" not in raw
    records = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    assert records[0]["work_revision"] == 1
    assert records[0]["parent_head_sha256"] is None
    assert records[1]["work_revision"] == 2
    assert records[1]["parent_head_sha256"] == first["head_sha256"]
    assert second["head_sha256"] == records[1]["record_sha256"]

    restored = workfile.rehydrate_cumcwork(
        path,
        expected_host_task_key_sha256=first_capsule["host_task_key_sha256"],
    )
    assert restored["status"] == "clean"
    assert restored["work_revision"] == 2
    assert restored["head_sha256"] == second["head_sha256"]
    assert restored["active_capsule"] == second_capsule
    assert restored["active_capsule"]["persistence_scope"] == "turn_local"
    assert restored["focus"]["objective"] == "实现可靠的短期连续记忆"
    assert restored["focus"]["current_action"]["text"] == "读取当前实现"


def test_empty_objective_workfile_is_append_only_retired_before_new_turn(tmp_path: Path) -> None:
    workfile = load_workfile_module()
    path = tmp_path / "polluted-empty-objective.cumcwork"
    polluted = capsule("临时有效目标")
    polluted["objective"] = ""
    polluted["global_goal_anchor"]["objective"] = ""
    polluted["global_goal_anchor"]["objective_sha256"] = hashlib.sha256(b"").hexdigest()
    polluted["activation_reasons"] = ["tool_required"]
    polluted["acceptance_criteria"] = [
        {
            "id": "tool_result_observed",
            "text": "required tool result is observed and checked",
            "status": "unknown",
        }
    ]
    polluted["remaining_work"] = list(polluted["acceptance_criteria"])
    polluted["next_action"] = "required tool result is observed and checked"
    polluted["last_event"] = {
        "event_id": "host:polluted-stage-exit",
        "type": "stage_exit",
        "observed_at": "2026-08-25T01:58:55Z",
    }
    workfile.append_cumcwork_snapshot(
        path,
        polluted,
        expected_head_sha256=None,
        expected_work_revision=0,
    )

    response = process_worker_request(
        {
            "op": "observe",
            "route_receipt": {
                "edit_operation_profile": "read_only",
                "memory_mode": "none",
                "tool_surface_need": "none",
                "action_bindings": [],
            },
            "task_event": task_event(
                "polluted:next-turn",
                "task_observed",
                objective="解释一个短问题",
            ),
            "capsule": None,
            "workfile": {"path": str(path)},
        }
    )
    restored = workfile.rehydrate_cumcwork(
        path,
        expected_host_task_key_sha256=polluted["host_task_key_sha256"],
    )

    assert response["capsule"] is None
    assert response["workfile_recovery"]["status"] == "retired_invalid_empty_objective"
    assert restored["work_revision"] == 2
    assert restored["active_capsule"] is None
    assert restored["latest_capsule"]["lifecycle"] == "RETIRED"
    assert restored["latest_capsule"]["retirement"]["reason"] == "invalid_empty_objective"


def test_observe_once_with_workfile_has_no_persistent_locator(tmp_path: Path) -> None:
    work_path = tmp_path / "observe-once.cumcwork"
    response = process_worker_request(
        {
            "op": "observe",
            "route_receipt": {
                "edit_operation_profile": "none",
                "memory_mode": "none",
                "tool_surface_need": "none",
                "action_bindings": [],
            },
            "task_event": task_event(
                "observe-once:turn",
                "task_observed",
                objective="检查一个文件并告诉我结果",
            ),
            "capsule": None,
            "workfile": {"path": str(work_path)},
        }
    )

    assert response["decision"]["decision"] == "observe_once"
    assert response["persistence"] == "turn_local"
    assert "workfile_receipt" not in response
    assert "workfile_handle" not in response
    assert response["additional_context_entries"].get("workfile") is None
    assert not work_path.exists()


def test_workfile_preserves_context_epoch_and_private_workspace_anchor(tmp_path: Path) -> None:
    workfile = load_workfile_module()
    path = tmp_path / "workspace-bound.cumcwork"
    current = new_task_capsule(
        route(),
        task_event(
            "workspace:start",
            "task_observed",
            objective="恢复后保持目标与工作区事实对齐",
            workspace_anchor={
                "schema": "cbh.workspace_fact_anchor.v1",
                "workspace_id_sha256": "a" * 64,
                "source": "git",
                "git_head": "1" * 40,
                "git_branch": "main",
                "worktree_state_sha256": "b" * 64,
                "observed_at": "2026-08-24T00:00:00Z",
            },
        ),
    )
    appended = workfile.append_cumcwork_snapshot(
        path,
        current,
        expected_head_sha256=None,
        expected_work_revision=0,
    )
    restored = workfile.rehydrate_cumcwork(
        path,
        expected_host_task_key_sha256=current["host_task_key_sha256"],
    )

    assert restored["active_capsule"]["context_epoch"] == current["context_epoch"]
    assert restored["active_capsule"]["workspace_anchor"] == current["workspace_anchor"]
    assert restored["focus"]["context_epoch_id"] == current["context_epoch"]["epoch_id"]
    assert restored["focus"]["workspace_review_required"] is False
    assert appended["changed"] is True


def test_rehydrate_marks_unexpected_workspace_drift_before_reusing_progress(tmp_path: Path) -> None:
    from task_continuity import process_worker_request

    path = tmp_path / "workspace-drift.cumcwork"
    first_anchor = {
        "schema": "cbh.workspace_fact_anchor.v1",
        "workspace_id_sha256": "a" * 64,
        "source": "git",
        "git_head": "1" * 40,
        "git_branch": "main",
        "worktree_state_sha256": "b" * 64,
        "observed_at": "2026-08-24T00:00:00Z",
    }
    process_worker_request(
        {
            "op": "observe",
            "route_receipt": route(),
            "task_event": task_event(
                "drift:start",
                "task_observed",
                objective="恢复后继续验证当前任务",
                multi_stage=True,
                workspace_anchor=first_anchor,
            ),
            "capsule": None,
            "workfile": {"path": str(path)},
        }
    )
    resumed = process_worker_request(
        {
            "op": "observe",
            "route_receipt": route(),
            "task_event": task_event(
                "drift:resume",
                "task_observed",
                objective="继续",
                intent_kind="continue_ack",
                intent_confidence="high",
                intent_source="explicit_marker",
                workspace_anchor={
                    **first_anchor,
                    "git_head": "2" * 40,
                    "observed_at": "2026-08-24T01:00:00Z",
                },
            ),
            "capsule": None,
            "workfile": {"path": str(path)},
        }
    )

    assert resumed["capsule"]["workspace_review_required"] is True
    assert resumed["capsule"]["objective"] == "恢复后继续验证当前任务"
    assert "workspace_revalidation_required" in resumed["additional_context_entry"]["value"]


def test_worker_request_performs_one_full_workfile_scan(tmp_path: Path, monkeypatch) -> None:
    import task_continuity_workfile as workfile
    from task_continuity import process_worker_request

    scanner = getattr(workfile, "_rehydrate_cumcwork_bytes", None)
    assert callable(scanner), "single-scan transaction reader is missing"
    work_path = tmp_path / "one-scan.cumcwork"
    initial_capsule = capsule("一次扫描恢复并提交任务状态")
    workfile.append_cumcwork_snapshot(
        work_path,
        initial_capsule,
        expected_head_sha256=None,
        expected_work_revision=0,
    )
    calls = 0

    def counting_scanner(raw: bytes, *, expected_host_task_key_sha256: str):
        nonlocal calls
        calls += 1
        return scanner(
            raw,
            expected_host_task_key_sha256=expected_host_task_key_sha256,
        )

    monkeypatch.setattr(workfile, "_rehydrate_cumcwork_bytes", counting_scanner)
    response = process_worker_request(
        {
            "op": "observe",
            "route_receipt": route(),
            "task_event": task_event(
                "one-scan:continue",
                "task_observed",
                objective="继续",
                intent_kind="continue_ack",
            ),
            "capsule": None,
            "workfile": {"path": str(work_path)},
        }
    )

    assert response["workfile_receipt"]["work_revision"] == 2
    assert calls == 1


def test_transaction_lock_preserves_compare_and_swap_chain(tmp_path: Path) -> None:
    workfile = load_workfile_module()
    transaction_factory = getattr(workfile, "cumcwork_transaction", None)
    assert callable(transaction_factory), "cumcwork transaction interface is missing"
    path = tmp_path / "locked.cumcwork"
    first_capsule = capsule("事务 A")
    second_capsule = capsule("事务 B")
    scope = first_capsule["host_task_key_sha256"]
    second_error: list[BaseException] = []

    def competing_writer() -> None:
        try:
            workfile.append_cumcwork_snapshot(
                path,
                second_capsule,
                expected_head_sha256=None,
                expected_work_revision=0,
            )
        except BaseException as exc:  # captured for the parent assertion
            second_error.append(exc)

    with transaction_factory(
        path,
        expected_host_task_key_sha256=scope,
    ) as transaction:
        thread = threading.Thread(target=competing_writer)
        thread.start()
        time.sleep(0.05)
        assert thread.is_alive(), "competing writer bypassed the transaction lock"
        first = transaction.append_snapshot(
            first_capsule,
            expected_head_sha256=None,
            expected_work_revision=0,
        )

    thread.join(timeout=3)
    assert not thread.is_alive()
    assert first["work_revision"] == 1
    assert len(second_error) == 1
    assert "cumcwork_stale_head" in str(second_error[0])
    restored = workfile.rehydrate_cumcwork(
        path,
        expected_host_task_key_sha256=scope,
    )
    assert restored["work_revision"] == 1
    assert restored["head_sha256"] == first["head_sha256"]


def test_identical_snapshot_is_noop_and_stale_compare_and_swap_is_rejected(tmp_path: Path) -> None:
    workfile = load_workfile_module()
    path = tmp_path / "state.cumcwork"
    current = capsule()
    first = workfile.append_cumcwork_snapshot(
        path,
        current,
        expected_head_sha256=None,
        expected_work_revision=0,
    )
    before = path.read_bytes()
    noop = workfile.append_cumcwork_snapshot(
        path,
        current,
        expected_head_sha256=first["head_sha256"],
        expected_work_revision=1,
    )
    assert noop["changed"] is False
    assert path.read_bytes() == before

    with pytest.raises(ValueError, match="cumcwork_stale_head"):
        workfile.append_cumcwork_snapshot(
            path,
            apply_task_event(current, task_event("dispatch:2", "tool_dispatched"))["capsule"],
            expected_head_sha256="0" * 64,
            expected_work_revision=1,
        )
    assert path.read_bytes() == before


def test_truncated_tail_recovers_previous_head_and_repair_is_cas_bound(tmp_path: Path) -> None:
    workfile = load_workfile_module()
    path = tmp_path / "tail.cumcwork"
    first_capsule = capsule()
    first = workfile.append_cumcwork_snapshot(
        path,
        first_capsule,
        expected_head_sha256=None,
        expected_work_revision=0,
    )
    second_capsule = apply_task_event(
        first_capsule,
        task_event("dispatch:tail", "tool_dispatched", current_step="写入第二条"),
    )["capsule"]
    workfile.append_cumcwork_snapshot(
        path,
        second_capsule,
        expected_head_sha256=first["head_sha256"],
        expected_work_revision=1,
    )
    complete = path.read_bytes()
    second_start = complete.find(b"\n") + 1
    path.write_bytes(complete[: second_start + 19])

    restored = workfile.rehydrate_cumcwork(
        path,
        expected_host_task_key_sha256=first_capsule["host_task_key_sha256"],
    )
    assert restored["status"] == "tail_repair_required"
    assert restored["head_sha256"] == first["head_sha256"]
    assert restored["work_revision"] == 1
    assert restored["active_capsule"] == first_capsule

    repaired = workfile.repair_cumcwork_tail(path, repair_token=restored["repair_token"])
    assert repaired["status"] == "clean"
    assert path.read_bytes() == complete[:second_start]


def test_committed_corruption_is_never_treated_as_repairable_tail(tmp_path: Path) -> None:
    workfile = load_workfile_module()
    path = tmp_path / "corrupt.cumcwork"
    current = capsule()
    workfile.append_cumcwork_snapshot(
        path,
        current,
        expected_head_sha256=None,
        expected_work_revision=0,
    )
    with path.open("ab") as stream:
        stream.write(b'{"broken":true}\n')

    with pytest.raises(ValueError, match="cumcwork_committed_corruption"):
        workfile.rehydrate_cumcwork(
            path,
            expected_host_task_key_sha256=current["host_task_key_sha256"],
        )


def test_scope_mismatch_and_active_capsule_switch_are_rejected(tmp_path: Path) -> None:
    workfile = load_workfile_module()
    path = tmp_path / "scope.cumcwork"
    current = capsule("任务 A")
    first = workfile.append_cumcwork_snapshot(
        path,
        current,
        expected_head_sha256=None,
        expected_work_revision=0,
    )

    with pytest.raises(ValueError, match="cumcwork_scope_mismatch"):
        workfile.rehydrate_cumcwork(
            path,
            expected_host_task_key_sha256="f" * 64,
        )

    other = capsule("任务 B")
    with pytest.raises(ValueError, match="cumcwork_active_capsule_switch"):
        workfile.append_cumcwork_snapshot(
            path,
            other,
            expected_head_sha256=first["head_sha256"],
            expected_work_revision=1,
        )


def test_persisted_snapshot_drops_raw_payload_and_cannot_grant_authority(tmp_path: Path) -> None:
    workfile = load_workfile_module()
    path = tmp_path / "bounded.cumcwork"
    current = capsule()
    current["raw_output"] = "secret-output"
    current["authority"] = {"granted": True, "consumed": False, "source": "bad"}
    workfile.append_cumcwork_snapshot(
        path,
        current,
        expected_head_sha256=None,
        expected_work_revision=0,
    )

    raw = path.read_text(encoding="utf-8")
    assert "secret-output" not in raw
    restored = workfile.rehydrate_cumcwork(
        path,
        expected_host_task_key_sha256=current["host_task_key_sha256"],
    )
    assert "raw_output" not in restored["latest_capsule"]
    assert restored["latest_capsule"]["authority"] == {
        "granted": False,
        "consumed": False,
        "source": "none",
    }


def test_worker_restart_rehydrates_but_does_not_bind_a_new_user_turn(tmp_path: Path) -> None:
    load_workfile_module()
    work_path = tmp_path / "restart.cumcwork"
    script = HARNESS / "task_continuity.py"
    first_request = {
        "id": "restart:1",
        "op": "observe",
        "route_receipt": route(),
        "task_event": task_event(
            "restart:start",
            "task_observed",
            objective="实现并验证可恢复工作记忆",
            intent_kind="new_task",
            continuity_requested=True,
        ),
        "capsule": None,
        "workfile": {"path": str(work_path)},
    }
    first_process = subprocess.run(
        [sys.executable, "-B", str(script), "--worker-once"],
        input=json.dumps(first_request, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert first_process.returncode == 0, first_process.stderr
    first = json.loads(first_process.stdout)["result"]
    assert first["persistence"] == "local_cumcwork"
    assert first["workfile_receipt"]["work_revision"] == 1
    handle = first["workfile_handle"]
    assert handle["path"] == str(work_path)
    assert handle["host_task_key_sha256"] == first["capsule"]["host_task_key_sha256"]
    assert first["additional_context_entries"]["workfile"]["kind"] == "untrusted"
    assert "memory_runtime_bridge.py" in first["additional_context_entries"]["workfile"]["value"]

    source_text = "把两个新问题交给另一个模型研究"
    second_request = {
        "id": "restart:2",
        "op": "observe",
        "route_receipt": route(),
        "task_event": task_event(
            "restart:continue",
            "task_observed",
            objective=source_text,
            intent_kind="new_task",
            intent_source="explicit_marker",
            event_origin="current_user_input",
            binding_required=False,
            source_units=[
                {
                    "id": "restart:continue:user-request",
                    "source_role": "current_user_request",
                    "text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                }
            ],
        ),
        "capsule": None,
        "workfile": {"path": str(work_path)},
    }
    second_process = subprocess.run(
        [sys.executable, "-B", str(script), "--worker-once"],
        input=json.dumps(second_request, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert second_process.returncode == 0, second_process.stderr
    second = json.loads(second_process.stdout)["result"]
    assert second["capsule"]["capsule_id"] == first["capsule"]["capsule_id"]
    assert second["capsule"]["objective"] == "实现并验证可恢复工作记忆"
    assert second["decision"]["decision"] == "safe_readonly"
    assert second["transition"]["changed"] is False
    assert second["persistence"] == "none"
    assert "workfile_receipt" not in second
    restored = load_workfile_module().rehydrate_cumcwork(
        work_path,
        expected_host_task_key_sha256=first["capsule"]["host_task_key_sha256"],
    )
    assert restored["work_revision"] == 1
    assert restored["active_capsule"] == first["capsule"]


def test_explicit_new_task_marker_cannot_supersede_workfile_without_receipt(tmp_path: Path) -> None:
    load_workfile_module()
    work_path = tmp_path / "switch.cumcwork"
    script = HARNESS / "task_continuity.py"
    requests = [
        {
            "id": "switch:1",
            "op": "observe",
            "route_receipt": route(),
            "task_event": task_event(
                "switch:old",
                "task_observed",
                objective="修复旧 R5 路由",
                intent_kind="new_task",
                continuity_requested=True,
            ),
            "capsule": None,
            "workfile": {"path": str(work_path)},
        },
        {
            "id": "switch:2",
            "op": "observe",
            "route_receipt": route(),
            "task_event": task_event(
                "switch:new",
                "task_observed",
                objective="实现可恢复的短期连续记忆",
                intent_kind="new_task",
                intent_confidence="high",
                intent_source="explicit_marker",
                intent_reason_codes=["explicit_global_replacement"],
                event_origin="current_user_input",
                binding_required=False,
                source_units=[
                    {
                        "id": "switch:new:user-request",
                        "source_role": "current_user_request",
                        "text_sha256": hashlib.sha256(
                            "实现可恢复的短期连续记忆".encode("utf-8")
                        ).hexdigest(),
                    }
                ],
            ),
            "capsule": None,
            "workfile": {"path": str(work_path)},
        },
    ]
    results = []
    for request in requests:
        completed = subprocess.run(
            [sys.executable, "-B", str(script), "--worker-once"],
            input=json.dumps(request, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        results.append(json.loads(completed.stdout))

    old = results[0]["result"]["capsule"]
    assert "error" not in results[1], results[1]
    new = results[1]["result"]["capsule"]
    assert new["capsule_id"] == old["capsule_id"]
    assert new["objective"] == old["objective"]
    assert results[1]["result"]["decision"]["decision"] == "safe_readonly"
    assert results[1]["result"]["persistence"] == "none"
    assert "workfile_receipt" not in results[1]["result"]
