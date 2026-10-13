from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import codex_session_ledger as ledger
from codex_session_ledger import build_ledger, doctor_ledger, read_jsonl, resolve_evidence


class SessionLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = self.root / "rollout-00000000-0000-0000-0000-000000000001.jsonl"
        self.output = self.root / "ledger"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_events(self, events: list[dict]) -> None:
        self.session.write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
            encoding="utf-8",
        )

    def append_events(self, events: list[dict]) -> None:
        with self.session.open("a", encoding="utf-8", newline="\n") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def test_generation_manager_exposes_minimal_interface(self) -> None:
        self.assertTrue(callable(getattr(ledger, "initialize_generation_root", None)))
        self.assertTrue(callable(getattr(ledger, "refresh_generation", None)))
        self.assertTrue(callable(getattr(ledger, "load_active_generation", None)))

    def test_generation_root_index_routes_through_current_pointer(self) -> None:
        self.write_events(self.base_events())
        generation_root = self.root / "generation-root"

        result = ledger.initialize_generation_root([self.session], generation_root)

        self.assertIn(result["status"], {"pass", "warn"})
        root_index = (generation_root / "_LEDGER_INDEX.md").read_text(encoding="utf-8")
        self.assertIn("CURRENT.json", root_index)
        self.assertIn("relative_path", root_index)
        self.assertNotIn("turns:", root_index)
        active_index = (
            ledger.load_active_generation(generation_root) / "_LEDGER_INDEX.md"
        ).read_text(encoding="utf-8")
        self.assertIn("turns: 1", active_index)

    def test_generation_refresh_appends_verified_tail_and_pairs_cross_checkpoint_output(self) -> None:
        initial = self.base_events()[:8]
        self.write_events(initial)
        generation_root = self.root / "generation-root"
        initialized = ledger.initialize_generation_root(
            [self.session],
            generation_root,
            conversation_memory_id="conversation:test",
        )
        self.assertIn(initialized["status"], {"pass", "warn"})
        generation_a = ledger.load_active_generation(generation_root)
        evidence_a_path = generation_a / "evidence_refs.jsonl"
        evidence_a_bytes = evidence_a_path.read_bytes()
        evidence_a_count = len(read_jsonl(evidence_a_path))

        self.append_events(
            [
                {
                    "type": "response_item",
                    "timestamp": "2026-07-18T00:00:08Z",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-2",
                        "output": [{"type": "input_text", "text": "late output"}],
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-18T00:00:09Z",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": "增量完成。",
                    },
                },
            ]
        )

        refreshed = ledger.refresh_generation(
            [self.session],
            generation_root,
            conversation_memory_id="conversation:test",
        )
        self.assertIn(refreshed["status"], {"pass", "warn"})
        self.assertEqual(refreshed["refresh_strategy"], "verified_incremental_tail")
        self.assertEqual(refreshed["resume_from_line"], len(initial) + 1)
        self.assertEqual(refreshed["raw_lines_read"], 2)
        self.assertEqual(refreshed["reused_evidence_refs"], evidence_a_count)

        generation_b = ledger.load_active_generation(generation_root)
        self.assertNotEqual(generation_a, generation_b)
        self.assertEqual(evidence_a_path.read_bytes(), evidence_a_bytes)
        evidence_b = read_jsonl(generation_b / "evidence_refs.jsonl")
        call = next(
            item
            for item in evidence_b
            if item.get("call_id") == "call-2" and item["evidence_kind"] == "tool_call"
        )
        output = next(
            item
            for item in evidence_b
            if item.get("call_id") == "call-2" and item["evidence_kind"] == "tool_output"
        )
        self.assertEqual(output["paired_call_ref_id"], call["ref_id"])
        self.assertEqual(call["paired_output_ref_ids"], [output["ref_id"]])
        self.assertIn(
            f"evidence_refs: {evidence_a_count + 2}",
            (generation_b / "_LEDGER_INDEX.md").read_text(encoding="utf-8"),
        )

        doctor = doctor_ledger(generation_root)
        self.assertNotEqual(doctor["status"], "fail")
        resolved = resolve_evidence(generation_root, output["ref_id"], context_lines=0)
        self.assertEqual(resolved["status"], "pass")
        self.assertTrue(resolved["hash_match"])

    def test_generation_refresh_rejects_changed_verified_prefix_without_switching(self) -> None:
        events = self.base_events()
        self.write_events(events)
        generation_root = self.root / "generation-root"
        ledger.initialize_generation_root([self.session], generation_root)
        generation_before = ledger.load_active_generation(generation_root)
        pointer_before = (generation_root / "CURRENT.json").read_bytes()
        evidence_before = (generation_before / "evidence_refs.jsonl").read_bytes()

        changed = self.base_events()
        changed[3]["payload"]["message"] = "请篡改工具调用与输出。"
        self.write_events(changed)

        result = ledger.refresh_generation([self.session], generation_root)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["reason"], "raw_prefix_identity_mismatch")
        self.assertEqual((generation_root / "CURRENT.json").read_bytes(), pointer_before)
        self.assertEqual(ledger.load_active_generation(generation_root), generation_before)
        self.assertEqual((generation_before / "evidence_refs.jsonl").read_bytes(), evidence_before)

    def test_generation_refresh_rejects_orphan_tail_output_without_switching(self) -> None:
        self.write_events(self.base_events()[:5])
        generation_root = self.root / "generation-root"
        ledger.initialize_generation_root([self.session], generation_root)
        generation_before = ledger.load_active_generation(generation_root)
        pointer_before = (generation_root / "CURRENT.json").read_bytes()
        self.append_events(
            [
                {
                    "type": "response_item",
                    "timestamp": "2026-07-18T00:00:06Z",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "missing-call",
                        "output": "orphan",
                    },
                }
            ]
        )

        result = ledger.refresh_generation([self.session], generation_root)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["reason"], "orphan_tool_output")
        self.assertEqual((generation_root / "CURRENT.json").read_bytes(), pointer_before)
        self.assertEqual(ledger.load_active_generation(generation_root), generation_before)

    def test_generation_refresh_without_complete_tail_is_noop(self) -> None:
        self.write_events(self.base_events())
        generation_root = self.root / "generation-root"
        ledger.initialize_generation_root([self.session], generation_root)
        generation_before = ledger.load_active_generation(generation_root)
        pointer_before = (generation_root / "CURRENT.json").read_bytes()

        result = ledger.refresh_generation([self.session], generation_root)
        self.assertEqual(result["status"], "pass")
        self.assertFalse(result["refreshed"])
        self.assertEqual(result["reason"], "no_new_complete_lines")
        self.assertEqual((generation_root / "CURRENT.json").read_bytes(), pointer_before)
        self.assertEqual(ledger.load_active_generation(generation_root), generation_before)

    def test_generation_doctor_rejects_nonreciprocal_tool_pair(self) -> None:
        self.write_events(self.base_events())
        generation_root = self.root / "generation-root"
        ledger.initialize_generation_root([self.session], generation_root)
        generation = ledger.load_active_generation(generation_root)
        evidence = read_jsonl(generation / "evidence_refs.jsonl")
        call = next(
            item
            for item in evidence
            if item.get("call_id") == "call-1" and item["evidence_kind"] == "tool_call"
        )
        call["paired_output_ref_ids"] = []
        ledger.write_jsonl(generation / "evidence_refs.jsonl", evidence)

        doctor = doctor_ledger(generation_root)
        pair_check = next(
            check for check in doctor["checks"] if check["id"] == "evidence.tool_call_output_pairs"
        )
        self.assertEqual(pair_check["status"], "fail")

    def test_generation_cli_initializes_and_refreshes_current_pointer(self) -> None:
        initial = self.base_events()[:8]
        self.write_events(initial)
        generation_root = self.root / "generation-root"
        script = Path(ledger.__file__).resolve()
        initialized = subprocess.run(
            [
                sys.executable,
                "-B",
                str(script),
                "generation-init",
                "--session-jsonl",
                str(self.session),
                "--output-dir",
                str(generation_root),
                "--conversation-memory-id",
                "conversation:test",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        init_receipt = json.loads(initialized.stdout)
        self.assertTrue(init_receipt["published"])
        self.assertEqual(
            Path(init_receipt["output_dir"]),
            Path(init_receipt["generation_dir"]),
        )
        generation_a = ledger.load_active_generation(generation_root)

        self.append_events(
            [
                {
                    "type": "response_item",
                    "timestamp": "2026-07-18T00:00:08Z",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-2",
                        "output": "late output",
                    },
                }
            ]
        )
        refreshed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(script),
                "generation-refresh",
                "--session-jsonl",
                str(self.session),
                "--output-dir",
                str(generation_root),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
        refresh_receipt = json.loads(refreshed.stdout)
        self.assertEqual(refresh_receipt["refresh_strategy"], "verified_incremental_tail")
        self.assertNotEqual(ledger.load_active_generation(generation_root), generation_a)

    @staticmethod
    def base_events() -> list[dict]:
        return [
            {
                "type": "session_meta",
                "timestamp": "2026-07-18T00:00:00Z",
                "payload": {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "cwd": r"C:\workspace",
                    "originator": "codex",
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-18T00:00:01Z",
                "payload": {"type": "turn_context", "turn_id": "turn-1"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-18T00:00:02Z",
                "payload": {"type": "turn_context", "turn_id": "turn-1"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-18T00:00:03Z",
                "payload": {"type": "user_message", "message": "请验证工具调用与输出。"},
            },
            {
                "type": "response_item",
                "timestamp": "2026-07-18T00:00:04Z",
                "payload": {
                    "type": "agent_message",
                    "phase": "commentary",
                    "message": "开始执行。",
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-07-18T00:00:05Z",
                "payload": {
                    "type": "function_call",
                    "name": "shell_command",
                    "call_id": "call-1",
                    "arguments": "{}",
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-07-18T00:00:06Z",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": [{"type": "input_text", "text": "Exit code: 0\nOutput: 完成"}],
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-07-18T00:00:07Z",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "call-2",
                    "input": "{}",
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-07-18T00:00:08Z",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-2",
                    "output": [{"type": "input_text", "text": "Script completed"}],
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-18T00:00:09Z",
                "payload": {
                    "type": "patch_apply_end",
                    "call_id": "patch-1",
                    "turn_id": "turn-1",
                    "success": True,
                    "changes": {
                        r"C:\workspace\example.txt": {
                            "type": "update",
                            "unified_diff": "@@ -1 +1 @@\n-old\n+new\n",
                        }
                    },
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-18T00:00:10Z",
                "payload": {"type": "task_complete", "last_agent_message": "验证完成。"},
            },
        ]

    def test_build_pairs_tool_calls_outputs_and_keeps_unique_turn_anchors(self) -> None:
        self.write_events(self.base_events())
        result = build_ledger([self.session], self.output, conversation_memory_id="conversation:test")
        self.assertEqual(result["status"], "pass")

        evidence = read_jsonl(self.output / "evidence_refs.jsonl")
        kinds = {record["evidence_kind"] for record in evidence}
        self.assertTrue(
            {"user_message", "agent_message", "tool_call", "tool_output", "patch", "task_complete"}
            <= kinds
        )
        by_call_id = {
            (record.get("call_id"), record.get("evidence_kind")): record
            for record in evidence
            if record.get("call_id")
        }
        call = by_call_id[("call-1", "tool_call")]
        output = by_call_id[("call-1", "tool_output")]
        self.assertEqual(output["paired_call_ref_id"], call["ref_id"])
        self.assertEqual(call["paired_output_ref_ids"], [output["ref_id"]])
        self.assertIn("完成", output["summary"])

        patch = next(record for record in evidence if record["evidence_kind"] == "patch")
        self.assertEqual(patch["artifact_refs"][0]["path"], r"C:\workspace\example.txt")
        self.assertEqual(patch["artifact_refs"][0]["change_type"], "update")
        self.assertTrue(patch["artifact_refs"][0]["diff_present"])

        anchors = read_jsonl(self.output / "time_anchors.jsonl")
        anchor_ids = [record["anchor_id"] for record in anchors]
        self.assertEqual(len(anchor_ids), len(set(anchor_ids)))
        turn_starts = [record for record in anchors if record["anchor_type"] == "turn_start"]
        self.assertEqual(len(turn_starts), 2)

        doctor = doctor_ledger(self.output)
        checks = {check["id"]: check for check in doctor["checks"]}
        self.assertEqual(checks["anchors.unique_ids"]["status"], "pass")
        self.assertEqual(checks["evidence.unique_ids"]["status"], "pass")
        self.assertEqual(checks["evidence.navigation_links"]["status"], "pass")
        self.assertEqual(checks["evidence.tool_call_output_pairs"]["status"], "pass")

        resolved = resolve_evidence(self.output, output["ref_id"], context_lines=0)
        self.assertEqual(resolved["status"], "pass")
        self.assertTrue(resolved["hash_match"])

    def test_literal_replacement_character_is_preserved_and_reported(self) -> None:
        events = self.base_events()
        events[3]["payload"]["message"] = "损坏边界：\ufffd"
        self.write_events(events)
        result = build_ledger([self.session], self.output)
        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["raw_replacement_character_count"], 1)

        sessions = read_jsonl(self.output / "sessions.jsonl")
        self.assertEqual(sessions[0]["raw_replacement_character_count"], 1)
        doctor = doctor_ledger(self.output)
        checks = {check["id"]: check for check in doctor["checks"]}
        self.assertEqual(checks["raw.replacement_character"]["status"], "warn")
        self.assertEqual(checks["ledger.replacement_character"]["status"], "warn")

    def test_invalid_utf8_fails_instead_of_replacing_bytes(self) -> None:
        self.session.write_bytes(
            b'{"type":"event_msg","payload":{"type":"user_message","message":"\xff"}}\n'
        )
        with self.assertRaises(UnicodeDecodeError):
            build_ledger([self.session], self.output)

    def test_invalid_jsonl_fails_with_exact_line(self) -> None:
        self.session.write_text('{"type":"event_msg"\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r":1: invalid JSONL"):
            build_ledger([self.session], self.output)

    def test_doctor_and_resolve_follow_verified_registry_locator_after_raw_path_moves(self) -> None:
        self.write_events(self.base_events())
        build_ledger([self.session], self.output)
        archived = self.root / "archived_sessions" / self.session.name
        archived.parent.mkdir()
        self.session.replace(archived)
        registry = self.root / "source_locator_registry.local.jsonl"
        registry.write_text(
            json.dumps(
                {
                    "schema": "cbh.source_locator_registry.v1",
                    "source_id": "codex-session:00000000-0000-0000-0000-000000000001",
                    "source_type": "raw_session_jsonl",
                    "sha256": hashlib.sha256(archived.read_bytes()).hexdigest(),
                    "locators": [
                        {"kind": "codex_archive", "path": str(archived), "status": "available"}
                    ],
                }
            ) + "\n",
            encoding="utf-8",
        )

        doctor = doctor_ledger(self.output, locator_registry_path=registry)
        raw_exists = next(check for check in doctor["checks"] if check["id"] == "raw.exists")
        self.assertEqual(raw_exists["status"], "pass")
        self.assertEqual(raw_exists["evidence"]["locator_status"], "relocated_identity_only")
        self.assertIn("does not read raw session payloads", doctor["cost_boundary"])

        evidence = read_jsonl(self.output / "evidence_refs.jsonl")
        target = next(item for item in evidence if item["evidence_kind"] == "user_message")
        resolved = resolve_evidence(
            self.output,
            target["ref_id"],
            context_lines=0,
            locator_registry_path=registry,
        )
        self.assertEqual(resolved["status"], "pass")
        self.assertEqual(resolved["locator_status"], "relocated_verified")
        self.assertEqual(Path(resolved["resolved_path"]), archived)


if __name__ == "__main__":
    unittest.main()
