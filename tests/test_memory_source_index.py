from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memory_source_index import (
    build_memory_file_records,
    discover_roots,
    merge_previous_locators,
)


class MemorySourceIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_logical_source_id_survives_memory_root_path_move(self) -> None:
        first_root = self.root / "first-location"
        second_root = self.root / "second-location"
        first_root.mkdir()
        (first_root / "events.jsonl").write_text('{"record_id":"EVENT-1"}\n', encoding="utf-8")
        first = build_memory_file_records(
            [{"root_id": "conversation-memory:stable-lane", "path": str(first_root), "kind": "conversation_memory"}]
        )
        first_root.replace(second_root)
        second = build_memory_file_records(
            [{"root_id": "conversation-memory:stable-lane", "path": str(second_root), "kind": "conversation_memory"}]
        )
        self.assertEqual(first[0]["source_id"], second[0]["source_id"])
        self.assertNotEqual(first[0]["locators"][0]["path"], second[0]["locators"][0]["path"])
        self.assertEqual(first[0]["sha256"], second[0]["sha256"])

    def test_backup_files_are_located_but_not_active_retrieval_candidates(self) -> None:
        memory_root = self.root / "memory"
        backup_root = memory_root / "backups"
        backup_root.mkdir(parents=True)
        (memory_root / "_META_INDEX.md").write_text("# Active\n", encoding="utf-8")
        (memory_root / "_META_INDEX.md.bak-20260805").write_text("# Old\n", encoding="utf-8")
        (backup_root / "old.md").write_text("old\n", encoding="utf-8")
        records = build_memory_file_records(
            [{"root_id": "memory-root:test", "path": str(memory_root), "kind": "project_memory"}]
        )
        by_relative = {item["relative_path"]: item for item in records}
        self.assertTrue(by_relative["_META_INDEX.md"]["retrieval_eligible"])
        self.assertEqual(by_relative["_META_INDEX.md"]["lifecycle"], "active_locator")
        self.assertFalse(by_relative["backups/old.md"]["retrieval_eligible"])
        self.assertEqual(by_relative["backups/old.md"]["lifecycle"], "cold_reference")
        self.assertFalse(by_relative["_META_INDEX.md.bak-20260805"]["retrieval_eligible"])
        self.assertEqual(
            by_relative["_META_INDEX.md.bak-20260805"]["lifecycle"], "cold_reference"
        )

    def test_standalone_record_link_manifest_is_indexed_without_scanning_its_parent(self) -> None:
        manifest = self.root / "legacy-links.jsonl"
        manifest.write_text('{"record_id":"ERR-1"}\n', encoding="utf-8")
        records = build_memory_file_records(
            [
                {
                    "root_id": "legacy-memory-links",
                    "path": str(manifest),
                    "kind": "legacy_record_link_manifest",
                }
            ]
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["relative_path"], "legacy-links.jsonl")
        self.assertEqual(records[0]["locators"][0]["path"], str(manifest))

    def test_refresh_preserves_previous_path_as_relocated_locator_for_same_logical_source(self) -> None:
        previous = [
            {
                "source_id": "memory-file:stable:events.jsonl",
                "locators": [
                    {"kind": "memory_root", "path": r"C:\old-root\events.jsonl", "status": "verified"}
                ],
            }
        ]
        current = [
            {
                "source_id": "memory-file:stable:events.jsonl",
                "locators": [
                    {"kind": "memory_root", "path": r"D:\new-root\events.jsonl", "status": "verified"}
                ],
            }
        ]
        merged = merge_previous_locators(current, previous)
        self.assertEqual(
            merged[0]["locators"],
            [
                {"kind": "memory_root", "path": r"D:\new-root\events.jsonl", "status": "verified"},
                {"kind": "memory_root", "path": r"C:\old-root\events.jsonl", "status": "relocated"},
            ],
        )

    def test_frozen_non_head_conversation_lane_is_link_only_not_retrieval_eligible(self) -> None:
        memory_root = self.root / "conversation-memory"
        ledger_root = self.root / "conversation-ledger"
        memory_root.mkdir()
        ledger_root.mkdir()
        (memory_root / "_META_INDEX.md").write_text("# Frozen lane\n", encoding="utf-8")
        (ledger_root / "_LEDGER_INDEX.md").write_text("# Frozen ledger\n", encoding="utf-8")
        registry_path = self.root / "registry.json"
        registry_path.write_text(
            """{
  "entries": [
    {
      "memory_id": "conversation:frozen-lane",
      "state": "FROZEN_READONLY",
      "chain_head": false,
      "root_path": "%s",
      "ledger_root_path": "%s"
    }
  ]
}
"""
            % (str(memory_root).replace("\\", "\\\\"), str(ledger_root).replace("\\", "\\\\")),
            encoding="utf-8",
        )
        roots = discover_roots(
            overlay_path=self.root / "missing-overlay.json",
            conversation_registry_path=registry_path,
        )
        records = [
            record
            for record in build_memory_file_records(roots)
            if record["root_id"].startswith("conversation-")
        ]
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record["lifecycle"] == "frozen_readonly_reference" for record in records))
        self.assertTrue(all(record["retrieval_eligible"] is False for record in records))
        self.assertTrue(all(record["registry_state"] == "FROZEN_READONLY" for record in records))
        self.assertTrue(all(record["chain_head"] is False for record in records))

    def test_registry_cannot_freeze_same_active_lane_without_successor(self) -> None:
        memory_root = self.root / "conversation-memory"
        ledger_root = self.root / "conversation-ledger"
        memory_root.mkdir()
        ledger_root.mkdir()
        (memory_root / "index.json").write_text(
            json.dumps(
                {
                    "memory_id": "conversation:active-lane",
                    "lane_state": "ACTIVE",
                    "thread_id": "thread-active",
                    "active_successor_memory_id": None,
                }
            ),
            encoding="utf-8",
        )
        (ledger_root / "_LEDGER_INDEX.md").write_text("# Active ledger\n", encoding="utf-8")
        registry_path = self.root / "registry.json"
        registry_path.write_text(
            json.dumps(
                {
                    "schema": "cbh.active_conversation_memory_registry.v1",
                    "entries": [
                        {
                            "memory_id": "conversation:active-lane",
                            "state": "FROZEN_READONLY",
                            "chain_head": False,
                            "thread_id": "thread-active",
                            "root_path": str(memory_root),
                            "ledger_root_path": str(ledger_root),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "conversation_registry_lane_state_conflict"):
            discover_roots(
                overlay_path=self.root / "missing-overlay.json",
                conversation_registry_path=registry_path,
            )

    def test_conversation_ledger_indexes_only_current_generation_with_stable_logical_paths(self) -> None:
        ledger_root = self.root / "conversation-ledger"
        current = ledger_root / "generations" / "generation-current"
        old = ledger_root / "generations" / "generation-old"
        current.mkdir(parents=True)
        old.mkdir(parents=True)
        (ledger_root / "_LEDGER_INDEX.md").write_text("legacy flat\n", encoding="utf-8")
        (current / "_LEDGER_INDEX.md").write_text("current\n", encoding="utf-8")
        (current / "sessions.jsonl").write_text("{}\n", encoding="utf-8")
        (current / "generation.json").write_text("{}\n", encoding="utf-8")
        (old / "_LEDGER_INDEX.md").write_text("old\n", encoding="utf-8")
        (ledger_root / "CURRENT.json").write_text(
            json.dumps(
                {
                    "schema": "cbh.ledger_generation_pointer.v1",
                    "generation_id": "generation-current",
                    "relative_path": "generations/generation-current",
                }
            ),
            encoding="utf-8",
        )

        records = build_memory_file_records(
            [
                {
                    "root_id": "conversation-ledger:conversation:test",
                    "path": str(ledger_root),
                    "kind": "conversation_ledger",
                }
            ]
        )
        by_relative = {item["relative_path"]: item for item in records}
        self.assertEqual(
            set(by_relative),
            {
                "CURRENT.json",
                "active/_LEDGER_INDEX.md",
                "active/generation.json",
                "active/sessions.jsonl",
            },
        )
        self.assertIn(
            "generation-current",
            by_relative["active/_LEDGER_INDEX.md"]["locators"][0]["path"],
        )
        self.assertFalse(any("generation-old" in item["locators"][0]["path"] for item in records))
        self.assertFalse(any("legacy flat" in Path(item["locators"][0]["path"]).read_text(encoding="utf-8") for item in records if item["relative_path"].endswith("_LEDGER_INDEX.md")))


if __name__ == "__main__":
    unittest.main()
