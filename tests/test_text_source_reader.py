import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness" / "text_source_reader.py"


def reader():
    assert MODULE.is_file(), "A source-file reader is required beyond the v3 record reader"
    spec = importlib.util.spec_from_file_location("text_source_reader", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_markdown_hierarchy_ignores_fenced_headings_and_expands_exact_source(tmp_path):
    api = reader()
    path = tmp_path / "brief.md"
    raw = "# 选题\r\n总体目标。\r\n## 受众\r\n面向自媒体作者。\r\n```python\r\n# not a heading\r\n```\r\n## 节奏\r\n每周两篇。\r\n".encode("utf-8")
    path.write_bytes(raw)
    index = api.build_index(tmp_path, ["brief.md"])
    candidates = api.search(index, "自媒体 受众")
    unit = next(item for item in candidates["candidates"] if item["title"] == "受众")
    assert not any(item["title"] == "not a heading" for item in index["units"])
    expanded = api.read_unit(index, unit["unit_id"])
    assert "面向自媒体作者" in expanded["text"] and "每周两篇" not in expanded["text"]
    assert expanded["sha256_verified"] is True
    assert expanded["bytes_read"] == expanded["end_byte"] - expanded["start_byte"]
    assert "每周两篇" in api.read_unit(index, unit["unit_id"], scope="parent")["text"]
    assert path.read_bytes() == raw


def test_python_symbol_query_returns_complete_function_not_an_arbitrary_chunk(tmp_path):
    api = reader()
    path = tmp_path / "source.py"
    path.write_text('def load_event(path):\n    """读取完整事件并校验来源。"""\n    return path.read_text()\n\ndef unrelated():\n    return 0\n', encoding="utf-8")
    index = api.build_index(tmp_path, ["source.py"])
    result = api.search(index, "读取 事件 校验")
    unit = next(item for item in result["candidates"] if item["title"] == "load_event")
    text = api.read_unit(index, unit["unit_id"])["text"]
    assert "return path.read_text()" in text
    assert "def unrelated" not in text


def test_memory_jsonl_preserves_full_event_metadata_and_body(tmp_path):
    api = reader()
    row = {"record_id": "EVENT-1", "meta": {"candidate_label": "发布流程", "summary": "发布前检查引用"},
           "owner": {"lane_id": "content"}, "edges": [{"type": "belongs_to", "target_id": "CLUSTER-1"}],
           "content": {"details": "完整细节，不应进入候选预览。"}}
    (tmp_path / "events.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    index = api.build_index(tmp_path, ["events.jsonl"])
    candidate = api.search(index, "发布 引用")["candidates"][0]
    assert candidate["title"] == "发布流程"
    assert candidate["edges"] == row["edges"]
    assert "完整细节" not in json.dumps(candidate, ensure_ascii=False)
    assert json.loads(api.read_unit(index, candidate["unit_id"])["text"]) == row


def test_changed_source_is_reported_and_old_range_is_not_read(tmp_path):
    api = reader()
    path = tmp_path / "note.txt"
    path.write_text("alpha event\n", encoding="utf-8")
    index = api.build_index(tmp_path, ["note.txt"])
    unit = api.search(index, "alpha")["candidates"][0]
    path.write_text("a changed alpha event with more text\n", encoding="utf-8")
    assert api.search(index, "alpha")["stale_sources"] == ["note.txt"]
    with pytest.raises(ValueError, match="source_changed"):
        api.read_unit(index, unit["unit_id"])


def test_explicit_source_scope_and_unknown_query_do_not_expand_to_other_files(tmp_path):
    api = reader()
    (tmp_path / "one.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "two.txt").write_text("beta", encoding="utf-8")
    index = api.build_index(tmp_path, ["one.txt"])
    result = api.search(index, "beta")
    assert result["candidates"] == []
    assert result["coverage_status"] == "no_lexical_match"
    assert set(index["sources"]) == {"one.txt"}


def test_query_uses_index_and_read_opens_only_selected_byte_range(tmp_path, monkeypatch):
    api = reader()
    path = tmp_path / "notes.md"
    path.write_text("# A\n" + "other words\n" * 1000 + "# Target\nselected payload\n", encoding="utf-8")
    index = api.build_index(tmp_path, ["notes.md"])
    original_open = Path.open
    def forbid_source(path_obj, *args, **kwargs):
        if path_obj == path:
            raise AssertionError("search read a full source file")
        return original_open(path_obj, *args, **kwargs)
    monkeypatch.setattr(Path, "open", forbid_source)
    selected = api.search(index, "selected payload")["candidates"][0]
    monkeypatch.setattr(Path, "open", original_open)
    expanded = api.read_unit(index, selected["unit_id"])
    assert expanded["bytes_read"] < 100
    assert "selected payload" in expanded["text"]


def test_crawled_html_preserves_origin_and_offers_a_derived_readable_view(tmp_path):
    api = reader()
    raw = '<article><h1>编辑计划</h1><p>每周发布两篇 &amp; 保留出处。</p><script>const mode = "draft";</script></article>'
    (tmp_path / "crawl.snapshot").write_text(raw, encoding="utf-8")
    origin = {"uri": "https://example.test/editorial", "media_type": "text/html", "retrieved_at": "2026-09-05T00:00:00Z"}
    index = api.build_index(tmp_path, ["crawl.snapshot"], origins={"crawl.snapshot": origin})
    candidates = api.search(index, "保留 出处")["candidates"]
    paragraph = next(item for item in candidates if item.get("tag") == "p")
    result = api.read_unit(index, paragraph["unit_id"], view="text")
    assert result["text"] == "每周发布两篇 & 保留出处。"
    assert result["origin"] == origin
    source = api.read_unit(index, paragraph["unit_id"])
    assert source["text"] == "<p>每周发布两篇 &amp; 保留出处。</p>"
    code = next(item for item in index["units"] if item["kind"] == "embedded_code")
    assert 'const mode = "draft";' in api.read_unit(index, code["unit_id"])["text"]
    article = next(unit for unit in index["units"] if unit.get("tag") == "article")
    assert "draft" not in article["preview"]


def test_utf16_source_and_declared_utf8_sig_have_exact_character_boundaries(tmp_path):
    api = reader()
    (tmp_path / "utf16.md").write_bytes("# 标题\n完整语义。\n".encode("utf-16"))
    (tmp_path / "plain.md").write_bytes("# 标题\n完整语义。\n".encode("utf-8"))
    index = api.build_index(tmp_path, ["utf16.md", "plain.md"], origins={"plain.md": {"encoding": "utf-8-sig"}})
    for unit in index["units"]:
        assert api.read_unit(index, unit["unit_id"])["text"] == "# 标题\n完整语义。\n"


def test_outline_exposes_hierarchy_without_forcing_keyword_matches(tmp_path):
    api = reader()
    (tmp_path / "book.md").write_text("# Overview\nintro\n## First\nalpha\n## Second\nbeta\n", encoding="utf-8")
    index = api.build_index(tmp_path, ["book.md"])
    root = api.outline(index)["children"][0]
    section = api.outline(index, unit_id=root["unit_id"])["children"][0]
    children = api.outline(index, unit_id=section["unit_id"], limit=1)
    assert children["children"][0]["title"] == "First"
    assert children["next_offset"] == 1
    assert api.outline(index, unit_id=section["unit_id"], offset=1)["children"][0]["title"] == "Second"


def test_index_output_cannot_overwrite_source_or_unrelated_file(tmp_path):
    api = reader()
    source = tmp_path / "source.txt"
    source.write_text("source content", encoding="utf-8")
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text('{"keep": true}', encoding="utf-8")
    index = api.build_index(tmp_path, ["source.txt"])
    with pytest.raises(ValueError, match="index_destination_is_source"):
        api.save_index(index, source)
    with pytest.raises(ValueError, match="index_destination_is_not_an_index"):
        api.save_index(index, unrelated)
    assert source.read_text() == "source content"
    target = tmp_path / "index.json"
    api.save_index(index, target)
    assert json.loads(target.read_text(encoding="utf-8")) == index


def test_new_structure_adapter_reuses_core_without_a_new_file_type_branch(tmp_path):
    api = reader()
    (tmp_path / "capture.anything").write_text("intro|selected semantic unit|tail", encoding="utf-8")
    def adapter(text, path, origin):
        return [(6, 28, "custom_event", "Selected event", {"preview": "selected semantic unit"})]
    index = api.build_index(tmp_path, ["capture.anything"], structure_adapter=adapter)
    candidate = api.search(index, "semantic unit")["candidates"][0]
    assert api.read_unit(index, candidate["unit_id"])["text"] == "selected semantic unit"
    def invalid(text, path, origin):
        return [(0, 5, "custom", "bad", {"path": "other-file"})]
    with pytest.raises(ValueError, match="adapter_cannot_replace_source_locator"):
        api.build_index(tmp_path, ["capture.anything"], structure_adapter=invalid)


def test_stateful_encoding_uses_complete_source_instead_of_inventing_byte_boundaries(tmp_path):
    api = reader()
    text = "# Event\n日本語の文書。\n"
    (tmp_path / "source.md").write_bytes(text.encode("iso2022_jp"))
    index = api.build_index(tmp_path, ["source.md"], origins={"source.md": {"encoding": "iso2022_jp"}})
    assert index["sources"]["source.md"]["mapping_mode"] == "whole_source_only"
    assert len(index["units"]) == 1
    unit = api.search(index, "Event")["candidates"][0]
    assert api.read_unit(index, unit["unit_id"])["text"] == text


def test_unicode_words_are_indexed_without_requiring_an_ascii_language(tmp_path):
    api = reader()
    (tmp_path / "note.txt").write_text("Проверка источника", encoding="utf-8")
    index = api.build_index(tmp_path, ["note.txt"])
    assert api.search(index, "источника")["candidates"]


def test_packed_index_preserves_search_and_expansion_without_loading_all_postings(tmp_path):
    api = reader()
    assert hasattr(api, "search_packed"), "Queries need selective file-index loading"
    (tmp_path / "book.md").write_text("# Plan\nintro\n## Audience\nwriters and editors\n## Cadence\nweekly stories\n", encoding="utf-8")
    index = api.build_index(tmp_path, ["book.md"])
    packed = tmp_path / "reader.zip"
    api.save_index(index, packed, packed=True)
    for query in ("writers", "weekly", "unmatched"):
        expected = api.search(index, query)
        actual = api.search_packed(packed, query)
        assert actual["candidates"] == expected["candidates"]
        assert actual["coverage_status"] == expected["coverage_status"]
    picked = api.search_packed(packed, "writers")["candidates"][0]
    assert api.read_packed(packed, picked["unit_id"]) == api.read_unit(index, picked["unit_id"])
    assert api.read_packed(packed, picked["unit_id"], scope="parent") == api.read_unit(index, picked["unit_id"], scope="parent")
    assert api.outline_packed(packed)["children"] == api.outline(index)["children"]


def test_headingless_text_and_code_preamble_have_narrow_read_units(tmp_path):
    api = reader()
    (tmp_path / "article.md").write_text("opening\n" * 100 + "specific claim\n", encoding="utf-8")
    (tmp_path / "code.py").write_text('EXCLUDED = {"historical", "stale"}\n\ndef run():\n    return 1\n', encoding="utf-8")
    index = api.build_index(tmp_path, ["article.md", "code.py"])
    chosen = api.search(index, "specific claim")["candidates"][0]
    assert chosen["kind"] != "file"
    assert api.read_unit(index, chosen["unit_id"])["bytes_read"] < (tmp_path / "article.md").stat().st_size
    constant = api.search(index, "EXCLUDED historical")["candidates"][0]
    assert 'EXCLUDED = ' in api.read_unit(index, constant["unit_id"])["text"]
    assert 'def run' not in api.read_unit(index, constant["unit_id"])["text"]


def test_outline_scope_resolves_parent_and_file_consistently(tmp_path):
    api = reader()
    (tmp_path / "book.md").write_text("# Main\n## Child\nbody\n", encoding="utf-8")
    index = api.build_index(tmp_path, ["book.md"])
    child = next(unit for unit in index["units"] if unit["title"] == "Child")
    assert api.outline(index, unit_id=child["unit_id"], scope="file")["children"][0]["title"] == "Main"
    packed = tmp_path / "index.zip"
    api.save_index(index, packed, packed=True)
    assert api.outline_packed(packed, unit_id=child["unit_id"], scope="parent")["children"][0]["title"] == "Child"


def test_cli_outputs_utf8_without_expanding_chinese_to_unicode_escapes(tmp_path):
    api = reader()
    (tmp_path / "note.txt").write_text("完整语义原文", encoding="utf-8")
    index = api.build_index(tmp_path, ["note.txt"])
    packed = tmp_path / "index.zip"
    api.save_index(index, packed, packed=True)
    unit = api.search(index, "语义")["candidates"][0]
    completed = subprocess.run([sys.executable, "-B", str(MODULE), "read", "--index", str(packed), "--unit-id", unit["unit_id"]], capture_output=True)
    assert completed.returncode == 0
    output = completed.stdout.decode("utf-8")
    assert "完整语义原文" in output
    assert json.loads(output)["text"] == "完整语义原文"
    assert json.loads(output)["index_bytes_read"] > 4
    assert json.loads(output)["index_detection_bytes_read"] == 4


def test_compact_cards_keep_every_candidate_and_semantic_field(tmp_path):
    api = reader()
    (tmp_path / "source.py").write_text('def check_source():\n    """Check exact source bytes."""\n    return True\n', encoding="utf-8")
    index = api.build_index(tmp_path, ["source.py"])
    full = api.search(index, "check source")
    compact = api.compact_navigation(full)
    assert [item["unit_id"] for item in compact["candidates"]] == [item["unit_id"] for item in full["candidates"]]
    for before, after in zip(full["candidates"], compact["candidates"]):
        assert compact["sources"][after["source"]] == before["path"]
        assert after["title"] == before["title"]
        assert after.get("preview", after["title"]) == before["preview"]
        assert api.read_unit(index, after["unit_id"])["sha256_verified"] is True
