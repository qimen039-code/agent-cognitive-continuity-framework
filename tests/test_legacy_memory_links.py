from __future__ import annotations

import json
import sys
from pathlib import Path


HARNESS = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

from legacy_memory_links import (  # noqa: E402
    build_default_legacy_manifest,
    check_legacy_manifest,
)


def test_all_legacy_families_build_hash_bound_links_from_public_fixtures(tmp_path: Path, monkeypatch) -> None:
    import legacy_memory_links as links
    for family, index_name, payload_name, level in (
        ("ERR", "ERROR_SKILL", "ERROR_PAYLOAD", 2),
        ("SOL", "SOLUTION_SKILL", "SOLUTION_PAYLOAD", 2),
        ("ANCHOR", "ANCHOR_SKILL", "ANCHOR_PAYLOAD", 3),
    ):
        index = tmp_path / f"{family}-index.md"
        payload = tmp_path / f"{family}-payload.md"
        index.write_text(f"| `{family}-PUBLIC-001` | synthetic public fixture | none |\n", encoding="utf-8")
        payload.write_text(f"{'#' * level} {family}-PUBLIC-001\n\nstatus:\n- ACTIVE\n\nSynthetic evidence.\n", encoding="utf-8")
        monkeypatch.setattr(links, index_name, index)
        monkeypatch.setattr(links, payload_name, payload)
    for family, meta_name in (("CE", "CE_META"), ("IE", "IE_META"), ("INC", "INC_META")):
        root = tmp_path / family
        root.mkdir()
        meta = root / "_META_INDEX.md"
        meta.write_text(f"| `{family}-PUBLIC-001` | synthetic | ACTIVE | public fixture | ACTIVE | none | none |\n", encoding="utf-8")
        if family == "INC":
            with meta.open("a", encoding="utf-8") as handle:
                handle.write("\n## INC-PUBLIC-001\n\ncurrent_status:\n- historical\n\ncausal_chain:\n- Synthetic public evidence.\n")
        else:
            payload_root = root
            if family == "IE":
                payload_root = root / "test-surface"
                payload_root.mkdir()
            (payload_root / f"{family}-PUBLIC-001.md").write_text(f"# {family}-PUBLIC-001\n\nstatus:\n- ACTIVE\n\nSynthetic public evidence.\n", encoding="utf-8")
        monkeypatch.setattr(links, meta_name, meta)
    output = tmp_path / "legacy-links.jsonl"
    receipt = build_default_legacy_manifest(output)
    assert receipt["status"] == "written"
    assert receipt["record_count"] == 6
    assert receipt["families"] == {family: 1 for family in ("ERR", "SOL", "ANCHOR", "CE", "IE", "INC")}

    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert all(row["retrieval_mode"] == "meta_first_link_only" for row in rows)
    assert all("legacy_text" not in row for row in rows)
    assert all(row["locator"]["block_sha256"] for row in rows)

    checked = check_legacy_manifest(output)
    assert checked["status"] == "pass"
    assert checked["verified_records"] == receipt["record_count"]

    original = Path(rows[0]["locator"]["path"])
    original.write_text(original.read_text(encoding="utf-8") + "Changed source\n", encoding="utf-8")
    assert check_legacy_manifest(output)["status"] == "fail"
