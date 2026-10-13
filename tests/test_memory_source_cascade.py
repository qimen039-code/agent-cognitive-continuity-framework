from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness"
HARNESS = ROOT / "skills" / "embedded-harness"
if not HARNESS.is_dir():
    HARNESS = ROOT
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

import harness_action_consumer as consumer  # noqa: E402
import semantic_memory  # noqa: E402


def selected_record() -> dict[str, object]:
    return {
        "record_id": "MEM-CASCADE-SELECTED-001",
        "source_id": "memory-file:test:selected",
        "path": "C:/fixture/semantic-record.md",
        "line": 12,
        "sha256": "a" * 64,
        "payload_opened": True,
        "evidence_handles": [
            {
                "source_id": "raw:test:execution",
                "original_path": "C:/fixture/session.jsonl",
                "line": 44,
                "line_sha256_16": "b" * 16,
            }
        ],
    }


def test_raw_depth_requires_bounded_evidence_after_semantic_source() -> None:
    planner = getattr(semantic_memory, "plan_memory_source_cascade", None)
    assert callable(planner), "memory source cascade planner is missing"

    cascade = planner(
        read_depth_profile="raw_context_window",
        selected_records=[selected_record()],
        semantic_review_candidates=[],
        retrieval_requested=True,
    )

    assert cascade["schema"] == "cbh.memory_source_cascade.v1"
    assert cascade["status"] == "drill_down_required"
    assert cascade["current_layer"] == "semantic_source"
    assert cascade["required_layer"] == "raw_evidence"
    assert cascade["next_action"] == "open_bounded_raw_evidence"
    assert cascade["selected_record_ids"] == ["MEM-CASCADE-SELECTED-001"]
    assert cascade["evidence_handles"][0]["source_id"] == "raw:test:execution"
    assert cascade["raw_evidence_opened"] is False
    assert cascade["automatic_open"] is False
    assert cascade["authority_granted"] is False


def test_weak_recall_node_must_materialize_semantic_source_before_raw() -> None:
    planner = getattr(semantic_memory, "plan_memory_source_cascade", None)
    assert callable(planner), "memory source cascade planner is missing"

    cascade = planner(
        read_depth_profile="raw_context_window",
        selected_records=[],
        semantic_review_candidates=[
            {
                "record_id": "MEM-CASCADE-CANDIDATE-001",
                "source_tag": "memory_meta_index",
                "belief_status": "navigation_only",
                "payload_opened": False,
            }
        ],
        retrieval_requested=True,
    )

    assert cascade["status"] == "semantic_review_required"
    assert cascade["current_layer"] == "recall_node"
    assert cascade["required_layer"] == "semantic_source"
    assert cascade["next_action"] == "select_and_materialize_semantic_source"
    assert cascade["candidate_record_ids"] == ["MEM-CASCADE-CANDIDATE-001"]
    assert cascade["evidence_handles"] == []


def test_consumer_and_compact_receipt_expose_not_requested_cascade() -> None:
    receipt = consumer.build_action_consumption(
        {
            "memory_need": "none",
            "read_depth_profile": "none",
            "action_bindings": [],
        },
        prompt="ordinary task without memory",
    )

    assert "memory_source_cascade" in receipt
    assert receipt["memory_source_cascade"]["status"] == "not_requested"
    compact = consumer._compact_runtime_receipt(receipt)
    assert compact["memory_source_cascade"] == receipt["memory_source_cascade"]
