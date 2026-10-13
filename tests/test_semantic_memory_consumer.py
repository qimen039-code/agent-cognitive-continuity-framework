from __future__ import annotations

import sys
from pathlib import Path


HARNESS = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

from harness_action_consumer import (  # noqa: E402
    DEFAULT_COMMON_ERROR_META,
    build_action_consumption,
)
from semantic_memory import (  # noqa: E402
    append_memory_record,
    build_memory_record,
    write_legacy_link_manifest,
)


def route() -> dict[str, object]:
    return {
        "memory_need": "paired_err_sol",
        "action_bindings": [
            {"action": "retrieve_matching_memory", "completion_evidence": "selected_record_id"}
        ],
    }


def test_consumer_materializes_selected_v3_record_from_compact_meta(tmp_path: Path) -> None:
    store = tmp_path / "memory-v3"
    append_memory_record(
        store,
        build_memory_record(
            record_id="SOL-V3-EXACT-001",
            memory_class="solution",
            lane_id="TEST",
            lane_kind="global",
            owner_skill="codex-bug-solution-memory",
            observed_at="2026-08-14T00:00:00Z",
            state="active",
            current_status="CURRENT",
            confidence={"label": "high", "basis": "test verifier"},
            query_types=["current_state", "history_reason"],
            candidate_label="PowerShell UTF-8 BOM 修复",
            summary="避免 U+FEFF 进入 JSON/TOML。",
            retrieval_terms=["UTF8Encoding($false)", "U+FEFF", "JSON.parse"],
            facets={"surface": ["PowerShell", "JSON"]},
            content={
                "event_summary": "BOM 导致严格解析失败。",
                "details": "写入无 BOM 后回读成功。",
                "applicable_boundaries": ["结构化文本写入"],
                "non_applicable_boundaries": ["原始字节证据"],
                "evidence_boundary": "test verifier",
            },
        ),
    )
    receipt = build_action_consumption(
        route(),
        prompt="U+FEFF 导致 JSON.parse 失败",
        memory_sources=[
            {
                "family": "V3",
                "format": "semantic_v3",
                "index_path": store / "meta.jsonl",
                "store_root": store,
            }
        ],
    )
    assert [item["record_id"] for item in receipt["selected_records"]] == [
        "SOL-V3-EXACT-001"
    ]
    selected = receipt["selected_records"][0]
    assert selected["source_tag"] == "local_memory_write"
    assert selected["evidence_boundary"] == "test verifier"
    assert selected["eligible_for_current_reuse"] is True
    assert selected["rule"] == "避免 U+FEFF 进入 JSON/TOML。"


def public_record(store: Path, record_id: str, memory_class: str, terms: list[str]) -> None:
    append_memory_record(store, build_memory_record(
        record_id=record_id, memory_class=memory_class,
        lane_id="PUBLIC-TEST", lane_kind="project", owner_skill="test",
        observed_at="2026-08-14T00:00:00Z", state="active",
        current_status="CURRENT", confidence={"label": "high", "basis": "synthetic verifier"},
        query_types=["current_state", "history_reason"], candidate_label=terms[0],
        summary=terms[0], retrieval_terms=terms, facets={"surface": ["test"]},
        content={"event_summary": terms[0], "details": "Synthetic public evidence.",
                 "applicable_boundaries": ["test"], "non_applicable_boundaries": ["production"],
                 "evidence_boundary": "synthetic test only"},
    ))


def public_sources(store: Path) -> list[dict]:
    return [{"family": "V3", "format": "semantic_v3",
             "index_path": store / "meta.jsonl", "store_root": store}]


def test_foreach_recurrence_query_selects_specific_ce_first(tmp_path: Path) -> None:
    store = tmp_path / "memory-v3"
    public_record(store, "CE-PUBLIC-FOREACH-001", "common_error", ["EmptyPipeElement"])
    receipt = build_action_consumption(
        route(),
        prompt="PowerShell foreach 后直接接管道出现 EmptyPipeElement",
        memory_sources=public_sources(store),
    )
    assert receipt["selected_records"]
    assert receipt["selected_records"][0]["record_id"] == (
        "CE-PUBLIC-FOREACH-001"
    )


def test_anchor_query_is_not_suppressed_by_unrelated_weak_incident_candidate(tmp_path: Path) -> None:
    store = tmp_path / "memory-v3"
    public_record(store, "ANCHOR-PUBLIC-MEANING", "semantic_anchor", ["public project meaning"])
    public_record(store, "ANCHOR-PUBLIC-MAPPING", "semantic_anchor", ["public mapping"])
    public_record(store, "INC-PUBLIC-2026-08-14", "major_incident", ["unrelated project story"])
    receipt = build_action_consumption(
        route(),
        prompt="Recall public project meaning and public mapping",
        memory_sources=public_sources(store),
    )
    selected_ids = [item["record_id"] for item in receipt["selected_records"]]
    assert "ANCHOR-PUBLIC-MEANING" in selected_ids
    assert "ANCHOR-PUBLIC-MAPPING" in selected_ids
    assert receipt["retrieval"]["major_incident_routed"] is False


def test_consumer_uses_hash_bound_legacy_manifest_as_its_meta_surface(tmp_path: Path) -> None:
    payload = tmp_path / "old.md"
    payload.write_text(
        "## ERR-LEGACY-LINK-001\nstatus: ACTIVE\nprevention_rule: keep exact target\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = tmp_path / "legacy-links.jsonl"
    write_legacy_link_manifest(
        manifest,
        [
            {
                "record_id": "ERR-LEGACY-LINK-001",
                "family": "ERR",
                "memory_class": "error",
                "state": "active",
                "candidate_label": "legacy exact target",
                "summary": "旧记录通过精确 heading 链接。",
                "retrieval_terms": ["legacy exact target"],
                "query_types": ["current_state", "history_reason"],
                "locator": {
                    "path": str(payload),
                    "heading": "ERR-LEGACY-LINK-001",
                    "heading_level": 2,
                },
                "typed_edges": [],
            }
        ],
    )
    receipt = build_action_consumption(
        route(),
        prompt="legacy exact target",
        memory_sources=[
            {
                "family": "LEGACY",
                "format": "legacy_manifest",
                "index_path": manifest,
            }
        ],
    )
    assert [item["record_id"] for item in receipt["selected_records"]] == [
        "ERR-LEGACY-LINK-001"
    ]
    assert receipt["selected_records"][0]["source_tag"] == "legacy_memory_link"


def test_exact_v3_id_suppresses_unrelated_weak_candidates_and_payload_context(
    tmp_path: Path,
) -> None:
    store = tmp_path / "memory-v3"
    for record_id, label, details in (
        ("CE-V3-EXACT-001", "精确测试记忆", "只应消费这一条。"),
        ("CE-V3-UNRELATED-002", "无关测试记忆", "UNRELATED-PAYLOAD-MUST-NOT-APPEAR"),
    ):
        append_memory_record(
            store,
            build_memory_record(
                record_id=record_id,
                memory_class="common_error",
                lane_id="TEST",
                lane_kind="global",
                owner_skill="codex-troubleshooting-skill-matrix",
                observed_at="2026-08-14T00:00:00Z",
                state="active",
                current_status="verified_solution",
                confidence={"label": "high", "basis": "test verifier"},
                query_types=["current_state", "history_reason"],
                candidate_label=label,
                summary=label,
                retrieval_terms=["shared PowerShell parser surface", label],
                facets={"surface": ["PowerShell"]},
                content={
                    "event_summary": label,
                    "details": details,
                    "applicable_boundaries": ["test"],
                    "non_applicable_boundaries": ["other"],
                    "evidence_boundary": "test verifier",
                },
            ),
        )
    receipt = build_action_consumption(
        route(),
        prompt="CE-V3-EXACT-001",
        memory_sources=[
            {
                "family": "V3",
                "format": "semantic_v3",
                "index_path": store / "meta.jsonl",
                "store_root": store,
            }
        ],
    )
    assert [item["record_id"] for item in receipt["selected_records"]] == [
        "CE-V3-EXACT-001"
    ]
    assert receipt["retrieval"]["coverage_status"] == "complete"
    assert receipt["semantic_review_candidates"] == []
    assert "UNRELATED-PAYLOAD-MUST-NOT-APPEAR" not in receipt["additional_context"]


def test_weak_v3_candidate_uses_meta_only_when_payload_is_unavailable(tmp_path: Path) -> None:
    store = tmp_path / "memory-v3"
    append_memory_record(
        store,
        build_memory_record(
            record_id="CE-V3-WEAK-001",
            memory_class="common_error",
            lane_id="TEST",
            lane_kind="global",
            owner_skill="codex-troubleshooting-skill-matrix",
            observed_at="2026-08-14T00:00:00Z",
            state="active",
            current_status="verified_solution",
            confidence={"label": "high", "basis": "test verifier"},
            query_types=["current_state", "history_reason"],
            candidate_label="PowerShell 解析候选",
            summary="弱匹配只能作为导航候选。",
            retrieval_terms=["PowerShell strict parser boundary"],
            facets={"surface": ["PowerShell"]},
            content={
                "event_summary": "弱候选 payload 不应打开。",
                "details": "PAYLOAD-MUST-STAY-CLOSED",
                "applicable_boundaries": ["test"],
                "non_applicable_boundaries": ["direct match"],
                "evidence_boundary": "test verifier",
            },
        ),
    )
    (store / "records.jsonl").replace(store / "records.unavailable")
    receipt = build_action_consumption(
        route(),
        prompt="PowerShell",
        memory_sources=[
            {
                "family": "V3",
                "format": "semantic_v3",
                "index_path": store / "meta.jsonl",
                "store_root": store,
            }
        ],
    )
    assert receipt["selected_records"] == []
    assert [item["record_id"] for item in receipt["semantic_review_candidates"]] == [
        "CE-V3-WEAK-001"
    ]
    assert "PAYLOAD-MUST-STAY-CLOSED" not in receipt["additional_context"]


def test_exact_v3_id_does_not_trigger_date_based_incident_disambiguation(tmp_path: Path) -> None:
    store = tmp_path / "memory-v3"
    public_record(store, "CE-PUBLIC-DATED-2026-08-14", "common_error", ["dated public error"])
    public_record(store, "INC-PUBLIC-2026-08-14", "major_incident", ["unrelated project story"])
    receipt = build_action_consumption(
        route(),
        prompt="CE-PUBLIC-DATED-2026-08-14",
        memory_sources=public_sources(store),
    )
    assert receipt["selected_records"][0]["record_id"] == (
        "CE-PUBLIC-DATED-2026-08-14"
    )
    assert receipt["retrieval"]["coverage_status"] == "complete"
    assert receipt["retrieval"]["disambiguation_candidates"] == []
