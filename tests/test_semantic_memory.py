from __future__ import annotations

import json
import io
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness"
HARNESS = ROOT / "skills" / "embedded-harness"
if not HARNESS.is_dir():
    HARNESS = ROOT
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

from semantic_memory import (  # noqa: E402
    append_memory_record,
    build_memory_record,
    build_memory_record_from_draft,
    check_memory_store,
    materialize_legacy_link,
    materialize_memory_record,
    rebuild_memory_meta,
    search_memory_store,
    unified_memory_search,
    write_legacy_link_manifest,
)


def record(record_id: str = "MEM-TEST-NEW-001", *, state: str = "active") -> dict[str, object]:
    return build_memory_record(
        record_id=record_id,
        memory_class="solution",
        lane_id="TEST",
        lane_kind="global",
        owner_skill="codex-bug-solution-memory",
        observed_at="2026-08-14T00:00:00Z",
        state=state,
        current_status="CURRENT" if state == "active" else "HISTORICAL",
        confidence={"label": "high", "basis": "fixture evidence"},
        query_types=["current_state", "history_reason"],
        candidate_label="UTF-8 BOM 解析修复",
        summary="写 JSON 前使用 UTF-8 无 BOM，并在写后严格解析。",
        retrieval_terms=["UTF8Encoding false", "U+FEFF", "JSON.parse", "UTF-8 BOM"],
        facets={"surface": ["json", "powershell"], "symptom": ["U+FEFF"]},
        content={
            "event_summary": "PowerShell 写出的 BOM 使严格 JSON 解析失败。",
            "details": "使用 UTF8Encoding(false) 后回读和 JSON.parse 均通过。",
            "applicable_boundaries": ["PowerShell 写 JSON"],
            "non_applicable_boundaries": ["原始证据字节不得重编码"],
            "evidence_boundary": "fixture-only",
        },
        edges=[{"type": "solves", "target_id": "ERR-TEST-BOM-001"}],
        evidence_refs=[{"source_id": "fixture:json-bom", "sha256": "a" * 64}],
    )


def test_append_separates_compact_meta_from_full_payload_and_round_trips(tmp_path: Path) -> None:
    store = tmp_path / "memory-v3"
    receipt = append_memory_record(store, record())

    assert receipt["schema"] == "cbh.semantic_memory_append_receipt.v1"
    assert receipt["status"] == "appended"
    assert receipt["record_line"] == 1
    assert receipt["meta_line"] == 1

    raw_record = (store / "records.jsonl").read_bytes()
    raw_meta = (store / "meta.jsonl").read_bytes()
    assert not raw_record.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw_record
    assert "使用 UTF8Encoding(false)" in raw_record.decode("utf-8")
    assert "使用 UTF8Encoding(false)" not in raw_meta.decode("utf-8")

    meta = json.loads(raw_meta.decode("utf-8"))
    assert meta["schema"] == "cbh.semantic_memory_meta.v3"
    assert meta["candidate_label"] == "UTF-8 BOM 解析修复"
    assert "content" not in meta

    materialized = materialize_memory_record(store, meta)
    assert materialized["record_id"] == "MEM-TEST-NEW-001"
    assert materialized["record_sha256"] == receipt["record_sha256"]
    assert check_memory_store(store)["status"] == "pass"


def test_meta_first_search_opens_no_payload_for_no_match_and_one_payload_for_hit(
    tmp_path: Path,
) -> None:
    store = tmp_path / "memory-v3"
    append_memory_record(store, record())

    miss = search_memory_store(store, "完全无关的天气问题", query_type="current_state")
    assert miss["coverage_status"] == "no_match"
    assert miss["metrics"]["payload_bytes_read"] == 0

    hit = search_memory_store(store, "U+FEFF 导致 JSON.parse 失败", query_type="current_state")
    assert hit["selected_record_ids"] == ["MEM-TEST-NEW-001"]
    assert hit["metrics"]["meta_bytes_read"] > 0
    assert hit["metrics"]["payload_bytes_read"] > 0
    assert len(hit["selected_records"]) == 1


def test_discovery_returns_semantic_metadata_without_reading_payload(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "memory-v3"
    append_memory_record(store, record())
    original_open = Path.open
    def guard(path, *args, **kwargs):
        if path == store / "records.jsonl":
            raise AssertionError("discovery opened full memory payload")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guard)
    result = search_memory_store(store, "U+FEFF 导致 JSON.parse 失败", query_type="current_state", metadata_only=True)
    assert result["selected_records"] == []
    assert result["metrics"]["payload_bytes_read"] == 0
    candidate = result["candidate_views"][0]
    assert candidate["record_id"] == "MEM-TEST-NEW-001"
    assert candidate["facets"]["surface"] == ["json", "powershell"]
    assert candidate["edges"] == [{"type": "solves", "target_id": "ERR-TEST-BOM-001"}]
    assert result["coverage_status"] == "semantic_review_required"


def test_selected_record_uses_exact_byte_range_without_scanning_prefix(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "memory-v3"
    append_memory_record(store, record("MEM-FIRST"))
    expected = record("MEM-SECOND")
    append_memory_record(store, expected)
    payload = (store / "records.jsonl").read_bytes()
    meta = json.loads((store / "meta.jsonl").read_text(encoding="utf-8").splitlines()[1])
    first_bytes = len(payload.splitlines(keepends=True)[0])
    assert meta["payload"]["byte_offset"] == first_bytes
    class RangeRead(io.BytesIO):
        def __iter__(self):
            raise AssertionError("indexed reading scanned preceding records")
        def read(self, size=-1):
            assert self.tell() == first_bytes
            assert size == meta["payload"]["record_bytes"]
            return super().read(size)
    original_open = Path.open
    def instrument(path, *args, **kwargs):
        if path == store / "records.jsonl":
            return RangeRead(payload)
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", instrument)
    assert materialize_memory_record(store, meta) == expected


def test_meta_rebuild_adds_offsets_without_changing_canonical_bytes(tmp_path: Path) -> None:
    store = tmp_path / "memory-v3"
    append_memory_record(store, record("MEM-FIRST"))
    append_memory_record(store, record("MEM-SECOND"))
    before = (store / "records.jsonl").read_bytes()
    rebuilt = rebuild_memory_meta(store)
    metas = [json.loads(line) for line in (store / "meta.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [item["payload"]["byte_offset"] for item in metas] == [0, len(before.splitlines(keepends=True)[0])]
    assert (store / "records.jsonl").read_bytes() == before
    assert rebuilt["canonical_records_changed"] is False


def test_discover_then_read_cli_expands_exact_selected_record(tmp_path: Path) -> None:
    store = tmp_path / "memory-v3"
    expected = record()
    append_memory_record(store, expected)
    base = [sys.executable, "-B", str(HARNESS / "semantic_memory.py")]
    discovery = subprocess.run(base + ["discover", "--store-root", str(store), "--query", "U+FEFF"], capture_output=True)
    assert discovery.returncode == 0
    candidates = json.loads(discovery.stdout.decode("utf-8"))["candidate_views"]
    expanded = subprocess.run(base + ["read", "--store-root", str(store), "--record-id", candidates[0]["record_id"]], capture_output=True)
    assert expanded.returncode == 0, expanded.stderr.decode("utf-8")
    assert json.loads(expanded.stdout.decode("utf-8")) == expected


def test_legacy_link_is_navigation_only_and_materializes_one_exact_heading(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "legacy.md"
    payload.write_text(
        "# Legacy\n\n## ERR-OLD-001\nerror: first\nsecret-detail: retained\n\n"
        "## ERR-OLD-002\nerror: second\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = tmp_path / "legacy-links.jsonl"
    links = [
        {
            "record_id": "ERR-OLD-001",
            "family": "ERR",
            "memory_class": "error",
            "state": "frozen_readonly",
            "candidate_label": "旧错误一",
            "summary": "旧格式只读链接。",
            "retrieval_terms": ["legacy first"],
            "query_types": ["history_reason"],
            "locator": {
                "path": str(payload),
                "heading": "ERR-OLD-001",
                "heading_level": 2,
            },
            "typed_edges": [],
        }
    ]
    write_legacy_link_manifest(manifest, links)

    manifest_text = manifest.read_text(encoding="utf-8")
    assert "secret-detail" not in manifest_text
    link = json.loads(manifest_text)
    assert link["schema"] == "cbh.semantic_memory_legacy_link.v1"
    assert link["evidence_boundary"] == "navigation_only_open_legacy_payload_for_facts"

    materialized = materialize_legacy_link(link)
    assert "secret-detail: retained" in materialized["legacy_text"]
    assert "ERR-OLD-002" not in materialized["legacy_text"]

    payload.write_text(payload.read_text(encoding="utf-8") + "\nchanged", encoding="utf-8")
    with pytest.raises(ValueError, match="legacy_payload_hash_mismatch"):
        materialize_legacy_link(link)


def test_unified_search_keeps_history_for_history_query_but_prefers_current_for_current_state(
    tmp_path: Path,
) -> None:
    store = tmp_path / "memory-v3"
    append_memory_record(store, record("SOL-NEW-CURRENT"))

    legacy_payload = tmp_path / "legacy.md"
    legacy_payload.write_text(
        "## SOL-OLD-HISTORICAL\nstatus: HISTORICAL\nsolution: old\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = tmp_path / "legacy-links.jsonl"
    write_legacy_link_manifest(
        manifest,
        [
            {
                "record_id": "SOL-OLD-HISTORICAL",
                "family": "SOL",
                "memory_class": "solution",
                "state": "superseded",
                "candidate_label": "旧 UTF-8 BOM 方案",
                "summary": "历史方案。",
                "retrieval_terms": ["U+FEFF", "JSON.parse"],
                "query_types": ["current_state", "history_reason"],
                "locator": {
                    "path": str(legacy_payload),
                    "heading": "SOL-OLD-HISTORICAL",
                    "heading_level": 2,
                },
                "typed_edges": [
                    {"type": "superseded_by", "target_id": "SOL-NEW-CURRENT"}
                ],
            }
        ],
    )

    current = unified_memory_search(
        "U+FEFF JSON.parse",
        query_type="current_state",
        store_roots=[store],
        legacy_manifest=manifest,
    )
    assert current["selected_record_ids"] == ["SOL-NEW-CURRENT"]
    assert "SOL-OLD-HISTORICAL" not in current["selected_record_ids"]

    history = unified_memory_search(
        "U+FEFF JSON.parse",
        query_type="history_reason",
        store_roots=[store],
        legacy_manifest=manifest,
    )
    assert history["selected_record_ids"][:2] == [
        "SOL-NEW-CURRENT",
        "SOL-OLD-HISTORICAL",
    ]


def test_record_validation_rejects_ambiguous_or_contextless_memory(tmp_path: Path) -> None:
    bad = record()
    bad["meta"]["summary"] = ""
    with pytest.raises(ValueError, match="memory_meta_summary_required"):
        append_memory_record(tmp_path / "memory-v3", bad)

    bad = record()
    bad["content"].pop("non_applicable_boundaries")
    with pytest.raises(ValueError, match="memory_content_context_incomplete"):
        append_memory_record(tmp_path / "memory-v3", bad)


def test_draft_is_compiled_into_the_same_canonical_record() -> None:
    expected = record()
    draft = {
        "schema": "cbh.semantic_memory_draft.v1",
        "record": {
            "record_id": "MEM-TEST-NEW-001",
            "memory_class": "solution",
            "lane_id": "TEST",
            "lane_kind": "global",
            "owner_skill": "codex-bug-solution-memory",
            "observed_at": "2026-08-14T00:00:00Z",
            "state": "active",
            "current_status": "CURRENT",
            "confidence": {"label": "high", "basis": "fixture evidence"},
            "query_types": ["current_state", "history_reason"],
            "candidate_label": "UTF-8 BOM 解析修复",
            "summary": "写 JSON 前使用 UTF-8 无 BOM，并在写后严格解析。",
            "retrieval_terms": ["UTF8Encoding false", "U+FEFF", "JSON.parse", "UTF-8 BOM"],
            "facets": {"surface": ["json", "powershell"], "symptom": ["U+FEFF"]},
            "content": {
                "event_summary": "PowerShell 写出的 BOM 使严格 JSON 解析失败。",
                "details": "使用 UTF8Encoding(false) 后回读和 JSON.parse 均通过。",
                "applicable_boundaries": ["PowerShell 写 JSON"],
                "non_applicable_boundaries": ["原始证据字节不得重编码"],
                "evidence_boundary": "fixture-only",
            },
            "edges": [{"type": "solves", "target_id": "ERR-TEST-BOM-001"}],
            "evidence_refs": [{"source_id": "fixture:json-bom", "sha256": "a" * 64}],
        },
    }
    assert build_memory_record_from_draft(draft) == expected


def test_rebuildable_meta_recovers_without_changing_canonical_records(tmp_path: Path) -> None:
    store = tmp_path / "memory-v3"
    first = record()
    second = build_memory_record(
        record_id="MEM-TEST-NEW-002",
        memory_class="solution",
        lane_id="TEST",
        lane_kind="global",
        owner_skill="codex-bug-solution-memory",
        observed_at="2026-08-14T01:00:00Z",
        state="active",
        current_status="verified_solution",
        confidence={"label": "high", "basis": "fixture evidence"},
        query_types=["current_state", "history_reason"],
        candidate_label="第二条测试记忆",
        summary="用于验证派生元索引可从 canonical JSONL 重建。",
        retrieval_terms=["第二条测试记忆", "rebuild meta"],
        facets={"surface": ["test"]},
        content={
            "event_summary": "第二条 canonical 记录保持不变。",
            "details": "删除派生 meta 的最后一行后重建。",
            "applicable_boundaries": ["derived meta recovery"],
            "non_applicable_boundaries": ["canonical payload repair"],
            "evidence_boundary": "fixture-only",
        },
    )
    append_memory_record(store, first)
    append_memory_record(store, second)
    canonical_before = (store / "records.jsonl").read_bytes()
    meta_lines = (store / "meta.jsonl").read_bytes().splitlines(keepends=True)
    (store / "meta.jsonl").write_bytes(b"".join(meta_lines[:1]))

    with pytest.raises(ValueError, match="memory_store_recovery_required"):
        append_memory_record(store, second)

    receipt = rebuild_memory_meta(store)
    assert receipt["status"] == "rebuilt"
    assert receipt["record_count"] == 2
    assert (store / "records.jsonl").read_bytes() == canonical_before
    assert check_memory_store(store)["status"] == "pass"


@pytest.mark.parametrize(
    "memory_class",
    [
        "error",
        "solution",
        "common_error",
        "interaction_error",
        "semantic_anchor",
        "major_incident",
        "conversation_event",
        "project_event",
        "event_cluster",
        "decision",
        "open_loop",
        "reference",
        "governance",
    ],
)
def test_all_future_memory_classes_share_the_v3_append_and_meta_first_contract(
    tmp_path: Path,
    memory_class: str,
) -> None:
    store = tmp_path / "memory-v3"
    record_id = f"MEM-{memory_class.upper().replace('_', '-')}-001"
    append_memory_record(
        store,
        build_memory_record(
            record_id=record_id,
            memory_class=memory_class,
            lane_id="TEST-ALL-CLASSES",
            lane_kind="project",
            owner_skill="cbh-semantic-memory-test",
            observed_at="2026-08-14T02:00:00Z",
            state="active",
            current_status="current",
            confidence={"label": "high", "basis": "schema verifier"},
            query_types=["current_state", "history_reason", "contradiction_check"],
            candidate_label=f"{memory_class} v3 test",
            summary="所有新记忆族共享相同 canonical/meta/evidence 分层。",
            retrieval_terms=[record_id, memory_class],
            facets={"memory_class": [memory_class]},
            content={
                "event_summary": "新记忆族写入统一 v3。",
                "details": "payload 只在精确命中后打开。",
                "applicable_boundaries": [memory_class],
                "non_applicable_boundaries": ["legacy rewrite"],
                "evidence_boundary": "test verifier",
            },
        ),
    )
    receipt = search_memory_store(
        store,
        record_id,
        query_type="history_reason",
        limit=1,
    )
    assert receipt["selected_record_ids"] == [record_id]
    assert receipt["raw_evidence_opened"] is False
    assert check_memory_store(store)["status"] == "pass"


def test_event_cluster_meta_points_to_typed_member_events_without_copying_them(
    tmp_path: Path,
) -> None:
    store = tmp_path / "memory-v3"
    cluster = build_memory_record(
        record_id="CLUSTER-CLIENT-RECOVERY-001",
        memory_class="event_cluster",
        lane_id="TEST-CLUSTER",
        lane_kind="project",
        owner_skill="cbh-semantic-memory-test",
        observed_at="2026-08-14T03:00:00Z",
        state="active",
        current_status="current",
        confidence={"label": "high", "basis": "typed member list"},
        query_types=["history_reason"],
        candidate_label="客户端恢复事件簇",
        summary="把同一恢复情景下的事件按 ID 聚合，不复制成员正文。",
        retrieval_terms=["客户端恢复", "recovery cluster"],
        facets={"time_bucket": ["2026-08"], "surface": ["client"]},
        content={
            "event_summary": "客户端恢复事件导航簇。",
            "details": "成员正文按 typed edge 下钻。",
            "applicable_boundaries": ["multi-event recall"],
            "non_applicable_boundaries": ["fact evidence"],
            "evidence_boundary": "cluster is navigation, member event is the fact record",
        },
        edges=[
            {"type": "contains", "target_id": "EVENT-CLIENT-RECOVERY-001"},
            {"type": "contains", "target_id": "EVENT-CLIENT-RECOVERY-002"},
            {"type": "current_head", "target_id": "EVENT-CLIENT-RECOVERY-002"},
        ],
    )
    append_memory_record(store, cluster)

    receipt = search_memory_store(
        store,
        "CLUSTER-CLIENT-RECOVERY-001",
        query_type="history_reason",
        limit=1,
    )

    assert receipt["selected_record_ids"] == ["CLUSTER-CLIENT-RECOVERY-001"]
    assert receipt["selected_records"][0]["edges"] == cluster["edges"]
    assert "EVENT-CLIENT-RECOVERY-001" not in receipt["selected_records"][0][
        "content"
    ]["details"]
