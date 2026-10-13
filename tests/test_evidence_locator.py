from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from evidence_locator import resolve_evidence_source


class EvidenceLocatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_id = "codex-session:00000000-0000-0000-0000-000000000001"
        self.original = self.root / "sessions" / "rollout-00000000-0000-0000-0000-000000000001.jsonl"
        self.archived = self.root / "archived_sessions" / self.original.name
        self.archived.parent.mkdir(parents=True)
        self.archived.write_text("first\nimportant evidence\nlast\n", encoding="utf-8")
        self.registry = self.root / "source_locator_registry.local.jsonl"
        self.registry.write_text(
            json.dumps(
                {
                    "schema": "cbh.source_locator_registry.v1",
                    "source_id": self.source_id,
                    "source_type": "raw_session_jsonl",
                    "locators": [
                        {"kind": "original_path", "path": str(self.original), "status": "relocated"},
                        {"kind": "codex_archive", "path": str(self.archived), "status": "available"},
                    ],
                }
            ) + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_missing_original_resolves_verified_archive_by_stable_source_id(self) -> None:
        expected = hashlib.sha256("important evidence".encode("utf-8")).hexdigest()[:16]
        receipt = resolve_evidence_source(
            self.original,
            source_id=self.source_id,
            expected_line=2,
            expected_line_sha256_16=expected,
            registry_path=self.registry,
        )
        self.assertEqual(receipt["status"], "relocated_verified")
        self.assertEqual(Path(receipt["resolved_path"]), self.archived)
        self.assertEqual(receipt["locator_kind"], "codex_archive")
        self.assertEqual(receipt["original_path"], str(self.original))

    def test_hash_mismatch_never_becomes_a_verified_relocation(self) -> None:
        receipt = resolve_evidence_source(
            self.original,
            source_id=self.source_id,
            expected_line=2,
            expected_line_sha256_16="0" * 16,
            registry_path=self.registry,
        )
        self.assertEqual(receipt["status"], "hash_mismatch")
        self.assertEqual(receipt["resolved_path"], "")

    def test_same_session_and_line_anchor_with_conflicting_full_payloads_is_not_chosen_arbitrarily(self) -> None:
        conflicting = self.root / "sealed" / self.archived.name
        conflicting.parent.mkdir()
        conflicting.write_text("different first\nimportant evidence\ndifferent last\n", encoding="utf-8")
        record = json.loads(self.registry.read_text(encoding="utf-8"))
        record["locators"].append(
            {"kind": "local_sealed_copy", "path": str(conflicting), "status": "available"}
        )
        self.registry.write_text(json.dumps(record) + "\n", encoding="utf-8")
        expected = hashlib.sha256("important evidence".encode("utf-8")).hexdigest()[:16]

        receipt = resolve_evidence_source(
            self.original,
            source_id=self.source_id,
            expected_line=2,
            expected_line_sha256_16=expected,
            registry_path=self.registry,
        )
        self.assertEqual(receipt["status"], "conflict")
        self.assertEqual(receipt["resolved_path"], "")

    def test_content_hash_recovers_memory_file_after_logical_path_and_source_id_change(self) -> None:
        moved = self.root / "new-memory-root" / "renamed.jsonl"
        moved.parent.mkdir()
        moved.write_text("preserved memory payload\n", encoding="utf-8")
        content_hash = hashlib.sha256(moved.read_bytes()).hexdigest()
        registry = self.root / "memory_file_index.local.jsonl"
        registry.write_text(
            json.dumps(
                {
                    "schema": "cbh.source_locator_registry.v1",
                    "source_id": "memory-file:new-root:renamed.jsonl",
                    "source_type": "memory_file",
                    "sha256": content_hash,
                    "locators": [
                        {"kind": "memory_root", "path": str(moved), "status": "verified"}
                    ],
                }
            ) + "\n",
            encoding="utf-8",
        )
        receipt = resolve_evidence_source(
            self.root / "old-memory-root" / "old-name.jsonl",
            source_id="memory-file:old-root:old-name.jsonl",
            expected_sha256=content_hash,
            registry_path=registry,
        )
        self.assertEqual(receipt["status"], "relocated_verified")
        self.assertEqual(Path(receipt["resolved_path"]), moved)
        self.assertEqual(receipt["match_basis"], "content_hash_fallback")


if __name__ == "__main__":
    unittest.main()
