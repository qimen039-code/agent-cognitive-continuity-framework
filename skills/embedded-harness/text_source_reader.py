"""Indexed discovery and exact reading of explicitly scoped text snapshots.

Original files remain canonical. Structure adapters emit byte-addressable units;
an inverted lexical index generates candidates, not semantic truth judgments.
No database, embedding service, network call, or model call is required.
"""
from __future__ import annotations

import argparse
import ast
import bisect
import codecs
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import zipfile
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

SCHEMA = "text_source_index.v1"


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _tokens(text):
    values = re.findall(r"\w+", text.casefold())
    tokens = []
    for value in values:
        if re.fullmatch(r"[\u4e00-\u9fff]+", value):
            tokens.extend(value[i:i + 2] for i in range(len(value) - 1))
            tokens.append(value)
        else:
            tokens.append(value)
            tokens.extend(part for part in value.split("_") if part != value and part)
    return tokens


def _stamp(path):
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _decode(raw, declared=None):
    markers = ((b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"),
               (b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))
    marker, detected = next(((marker, name) for marker, name in markers if raw.startswith(marker)), (b"", "utf-8"))
    codec = codecs.lookup(declared).name if declared else detected
    if codec in {"utf-16", "utf-32"}:
        if not marker or not detected.startswith(codec):
            raise ValueError("byte_order_required_for_declared_encoding")
        codec = detected
    if codec == "utf-8-sig":
        codec = "utf-8"
    bom = len(marker) if detected == codec else 0
    return raw[bom:].decode(codec, errors="strict"), codec, bom


def _line_offsets(text):
    result = [0]
    for line in text.splitlines(keepends=True):
        result.append(result[-1] + len(line))
    return result


class _HTMLStructure(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.text, self.lines, self.stack, self.spans = text, _line_offsets(text), [], []
    def position(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column
    def handle_starttag(self, tag, attrs):
        if tag in {"article", "section", "p", "li", "pre", "script", "style", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.stack.append((tag, self.position(), []))
    def handle_data(self, data):
        for _, _, texts in self.stack:
            texts.append(data)
    def handle_endtag(self, tag):
        match = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if match is None:
            return
        _, start, texts = self.stack[match]
        self.stack = self.stack[:match]
        end_tag = self.text.find(">", self.position())
        end = end_tag + 1 if end_tag >= 0 else len(self.text)
        label = " ".join(" ".join(texts).split())
        kind = "embedded_code" if tag in {"script", "style", "pre"} else "html_block"
        self.spans.append((start, end, kind, label[:100] or tag, {"preview": label[:280], "tag": tag}))


def _structure(text, path, origin):
    """Adapters describe source ranges; the core accepts ordinary text as fallback."""
    lines, offsets = text.splitlines(keepends=True), _line_offsets(text)
    suffix = path.suffix.casefold()
    if suffix == ".py":
        try:
            tree = ast.parse(text)
            return [(offsets[min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]) - 1],
                     offsets[node.end_lineno], type(node).__name__, node.name,
                     {"preview": (ast.get_docstring(node) or node.name)[:280]})
                    for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        except SyntaxError:
            pass  # Malformed source remains readable as text.
    if suffix in {".md", ".markdown"}:
        headings, fence = [], None
        for i, line in enumerate(lines):
            marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
            if marker:
                value = marker.group(1)
                if fence is None:
                    fence = value
                elif value[0] == fence[0] and len(value) >= len(fence):
                    fence = None
                continue
            match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if match and fence is None:
                headings.append((i, len(match.group(1)), match.group(2)))
        return [(offsets[line], offsets[next((n for n, depth, _ in headings[j + 1:] if depth <= level), len(lines))],
                 "heading", title, {}) for j, (line, level, title) in enumerate(headings)]
    if suffix in {".jsonl", ".ndjson"}:
        records = []
        for i, line in enumerate(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            meta = row.get("meta") if isinstance(row.get("meta"), dict) else row
            title = str(meta.get("candidate_label") or row.get("record_id") or f"record {i + 1}")
            records.append((offsets[i], offsets[i + 1], "record", title,
                            {"preview": str(meta.get("summary") or title)[:280],
                             "record_id": row.get("record_id"), "owner": row.get("owner"),
                             "edges": row.get("edges") or row.get("typed_edges") or []}))
        return records
    if "html" in str(origin.get("media_type", "")) or suffix in {".html", ".htm"}:
        parser = _HTMLStructure(text)
        parser.feed(text)
        parser.close()
        result = []
        for start, end, kind, title, extra in parser.spans:
            if kind == "html_block":
                visible = _VisibleText()
                visible.feed(text[start:end])
                preview = " ".join("".join(visible.parts).split())
                title, extra = preview[:100] or title, {**extra, "preview": preview[:280]}
            result.append((start, end, kind, title, extra))
        return result
    # Unknown formats still use source-preserving text windows, with full-file expansion.
    return [(offsets[i], offsets[min(i + 80, len(lines))], "text_window", path.name, {})
            for i in range(0, len(lines), 80)]


def _uncovered_text_spans(text, spans, path):
    cursor, gaps = 0, []
    for start, end, *_ in sorted(spans, key=lambda span: (span[0], span[1])):
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < len(text):
        gaps.append((cursor, len(text)))
    result = []
    for start, end in gaps:
        segment = text[start:end]
        if not segment.strip():
            continue
        offsets = _line_offsets(segment)
        for i in range(0, len(offsets) - 1, 80):
            result.append((start + offsets[i], start + offsets[min(i + 80, len(offsets) - 1)],
                           "text_window", path.name, {}))
    return result


def build_index(root, paths, *, origins=None, structure_adapter=None):
    root, origins = Path(root).resolve(), origins or {}
    sources, units, postings = {}, [], {}
    total_read = 0
    for name in dict.fromkeys(str(value) for value in paths):
        path = (root / name).resolve()
        relative = path.relative_to(root).as_posix()
        before = _stamp(path)
        raw = path.read_bytes()
        if _stamp(path) != before:
            raise ValueError(f"source_changed_during_index:{relative}")
        origin = origins.get(name) or origins.get(relative) or {}
        text, codec, bom = _decode(raw, origin.get("encoding"))
        revision = _hash(raw)
        total_read += len(raw)
        addressable = codec in {"utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be",
                                "ascii", "iso8859-1", "cp1252", "gb18030", "gbk", "shift_jis", "big5"}
        addressable = addressable and text.encode(codec) == raw[bom:]
        sources[relative] = {**before, "sha256": revision, "encoding": codec, "bom_bytes": bom, "origin": origin,
                             "mapping_mode": "character_spans" if addressable else "whole_source_only"}
        structured = structure_adapter(text, path, origin) if structure_adapter else None
        if structured is None:
            structured = _structure(text, path, origin)
        if not addressable:
            structured = []  # Stateful/noncanonical encodings retain an exact whole-source view.
        else:
            structured += _uncovered_text_spans(text, structured, path)
        spans = [(0, len(text), "file", relative, {"preview": relative})] + structured
        for start, end, _kind, _title, extra in spans:
            if not (isinstance(start, int) and isinstance(end, int) and 0 <= start <= end <= len(text)):
                raise ValueError("invalid_structure_span")
            if set(extra) & {"unit_id", "path", "kind", "title", "start_byte", "end_byte", "sha256", "parent_id", "line_start", "line_end"}:
                raise ValueError("adapter_cannot_replace_source_locator")
        positions = sorted({position for span in spans for position in span[:2]})
        byte_positions, previous, current_byte = {}, 0, bom
        for position in positions:
            current_byte += len(text[previous:position].encode(codec))
            byte_positions[position], previous = current_byte, position
        line_offsets = _line_offsets(text)
        file_units = []
        for start, end, kind, title, extra in spans:
            start_byte = 0 if kind == "file" else byte_positions[start]
            end_byte = len(raw) if kind == "file" else byte_positions[end]
            body = text[start:end]
            unit_id = _hash(f"{relative}|{revision}|{kind}|{start_byte}|{end_byte}".encode())[:24]
            unit = {"unit_id": unit_id, "path": relative, "kind": kind, "title": title,
                    "start_byte": start_byte, "end_byte": end_byte,
                    "line_start": bisect.bisect_right(line_offsets, start),
                    "line_end": bisect.bisect_right(line_offsets, max(start, end - 1)),
                    "sha256": _hash(raw[start_byte:end_byte]), "parent_id": None,
                    "preview": " ".join(body.split())[:280], **extra}
            units.append(unit)
            file_units.append(unit)
            counts = Counter(_tokens(body + " " + title + " " + relative))
            unit["term_count"] = max(1, sum(counts.values()))
            for token, frequency in counts.items():
                postings.setdefault(token, {})[unit_id] = frequency
        stack = []
        for unit in sorted(file_units, key=lambda value: (value["start_byte"], -value["end_byte"], value["kind"] != "file")):
            while stack and not (stack[-1]["start_byte"] <= unit["start_byte"] and stack[-1]["end_byte"] >= unit["end_byte"]):
                stack.pop()
            if stack:
                unit["parent_id"] = stack[-1]["unit_id"]
            stack.append(unit)
    return {"schema": SCHEMA, "root": str(root), "sources": sources, "units": units, "postings": postings,
            "metrics": {"source_bytes_indexed": total_read, "files": len(sources), "units": len(units)},
            "source_boundary": "Explicitly scoped originals remain canonical; index is rebuildable."}


def _current(index, relative):
    try:
        path = (Path(index["root"]) / relative).resolve()
        path.relative_to(Path(index["root"]).resolve())
        return _stamp(path) == {key: index["sources"][relative][key] for key in ("size", "mtime_ns")}
    except (OSError, ValueError):
        return False


def search(index, query, *, limit=8):
    if index.get("schema") != SCHEMA:
        raise ValueError("invalid_text_source_index")
    units = {unit["unit_id"]: unit for unit in index["units"]}
    scores = Counter()
    average = sum(unit["term_count"] for unit in units.values()) / max(1, len(units))
    for token in set(_tokens(query)):
        posting = index["postings"].get(token, {})
        weight = math.log(1 + (len(units) - len(posting) + 0.5) / (len(posting) + 0.5))
        for unit_id, frequency in posting.items():
            unit = units[unit_id]
            normalization = 1.2 * (0.25 + 0.75 * unit["term_count"] / max(1, average))
            scores[unit_id] += weight * frequency * 2.2 / (frequency + normalization)
            if token in _tokens(unit["title"]):
                scores[unit_id] += weight * 1.5
    candidates, stale = [], set()
    current = {relative: _current(index, relative) for relative in {units[unit_id]["path"] for unit_id in scores}}
    specific_paths = {units[unit_id]["path"] for unit_id in scores if units[unit_id]["kind"] != "file"}
    for unit_id, score in sorted(scores.items(), key=lambda pair: (-pair[1], pair[0])):
        unit = units[unit_id]
        if not current[unit["path"]]:
            stale.add(unit["path"])
            continue
        # Containers remain available for expansion; more specific hits lead discovery.
        if unit["kind"] == "file" and unit["path"] in specific_paths:
            continue
        if len(candidates) < max(1, limit):
            candidates.append({key: value for key, value in unit.items() if key not in {"sha256", "term_count"}} | {"score": round(score, 6)})
    return {"coverage_status": "semantic_selection_required" if candidates else "stale_index" if stale else "no_lexical_match",
            "candidates": candidates, "stale_sources": sorted(stale), "source_bytes_read": 0,
            "evidence_boundary": "Lexical and structural candidates only; no match is not proof of absence."}


def outline(index, *, unit_id=None, offset=0, limit=12, scope="unit"):
    if index.get("schema") != SCHEMA or offset < 0 or limit < 1 or scope not in {"unit", "parent", "file"}:
        raise ValueError("invalid_outline_request")
    if unit_id is not None and not any(unit["unit_id"] == unit_id for unit in index["units"]):
        raise ValueError("unknown_outline_parent")
    if unit_id is not None:
        unit = next(unit for unit in index["units"] if unit["unit_id"] == unit_id)
        if scope == "parent":
            unit_id = unit["parent_id"] or unit_id
        elif scope == "file":
            unit_id = next(value["unit_id"] for value in index["units"] if value["path"] == unit["path"] and value["kind"] == "file")
    children = [unit for unit in index["units"] if unit["parent_id"] == unit_id]
    page = children[offset:offset + limit]
    return {"parent_id": unit_id, "children": [{key: unit[key] for key in
            ("unit_id", "path", "kind", "title", "preview", "line_start", "line_end")} for unit in page],
            "next_offset": offset + limit if offset + limit < len(children) else None,
            "total_children": len(children), "source_bytes_read": 0}


def _packed_members(index):
    units = {unit["unit_id"]: dict(unit) for unit in index["units"]}
    children = {}
    for unit in units.values():
        children.setdefault(unit["parent_id"], []).append(unit["unit_id"])
    paths = list(index["sources"])
    file_numbers = {name: i for i, name in enumerate(paths)}
    manifest = {key: value for key, value in index.items() if key not in {"units", "postings"}}
    manifest.update(storage="sharded_zip", file_paths=paths, root_units=children.get(None, []),
                    file_units={unit["path"]: unit["unit_id"] for unit in units.values() if unit["kind"] == "file"},
                    unit_count=len(units), average_terms=sum(unit["term_count"] for unit in units.values()) / max(1, len(units)))
    buckets = {}
    for unit_id, unit in units.items():
        unit["children"] = children.get(unit_id, [])
        buckets.setdefault(f"units/{unit_id[:2]}.json", {})[unit_id] = unit
    title_tokens = {unit_id: set(_tokens(unit["title"])) for unit_id, unit in units.items()}
    for token, posting in index["postings"].items():
        values = {unit_id: [frequency, units[unit_id]["term_count"], int(token in title_tokens[unit_id]),
                           file_numbers[units[unit_id]["path"]]] for unit_id, frequency in posting.items()}
        member = f"postings/{_hash(token.encode())[:2]}.json"
        buckets.setdefault(member, {})[token] = values
    return {"manifest.json": manifest, **buckets}


class _CountedFile:
    def __init__(self, handle):
        self.handle, self.bytes_read = handle, 0
    def read(self, *args):
        value = self.handle.read(*args)
        self.bytes_read += len(value)
        return value
    def __getattr__(self, name):
        return getattr(self.handle, name)


class _PackedIndex:
    def __init__(self, path):
        self.path = Path(path)
        self.raw = self.path.open("rb")
        self.counted = _CountedFile(self.raw)
        try:
            self.archive = zipfile.ZipFile(self.counted)
            self.cache = {}
            self.manifest = self.load("manifest.json")
            if self.manifest.get("schema") != SCHEMA or self.manifest.get("storage") != "sharded_zip":
                raise ValueError("invalid_packed_text_source_index")
        except Exception:
            if hasattr(self, "archive"):
                self.archive.close()
            self.raw.close()
            raise
    def load(self, member):
        if member not in self.cache:
            try:
                self.cache[member] = json.loads(self.archive.read(member))
            except KeyError:
                self.cache[member] = {}
        return self.cache[member]
    def unit(self, unit_id):
        unit = dict(self.load(f"units/{unit_id[:2]}.json")[unit_id])
        return unit
    def close(self):
        self.archive.close()
        self.raw.close()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()


def search_packed(path, query, *, limit=8):
    with _PackedIndex(path) as packed:
        manifest, scores, unit_paths = packed.manifest, Counter(), {}
        for token in set(_tokens(query)):
            posting = packed.load(f"postings/{_hash(token.encode())[:2]}.json").get(token, {})
            weight = math.log(1 + (manifest["unit_count"] - len(posting) + 0.5) / (len(posting) + 0.5))
            for unit_id, (frequency, length, title_hit, file_number) in posting.items():
                normalization = 1.2 * (0.25 + 0.75 * length / max(1, manifest["average_terms"]))
                scores[unit_id] += weight * frequency * 2.2 / (frequency + normalization) + weight * 1.5 * title_hit
                unit_paths[unit_id] = manifest["file_paths"][file_number]
        roots = set(manifest["root_units"])
        specific_paths = {relative for unit_id, relative in unit_paths.items() if unit_id not in roots}
        current = {relative: _current(manifest, relative) for relative in set(unit_paths.values())}
        stale = {relative for relative, valid in current.items() if not valid}
        candidates = []
        for unit_id, score in sorted(scores.items(), key=lambda pair: (-pair[1], pair[0])):
            relative = unit_paths[unit_id]
            if not current[relative] or (unit_id in roots and relative in specific_paths):
                continue
            if len(candidates) >= max(1, limit):
                break
            unit = packed.unit(unit_id)
            candidates.append({key: value for key, value in unit.items() if key not in {"sha256", "term_count", "children"}} | {"score": round(score, 6)})
        return {"coverage_status": "semantic_selection_required" if candidates else "stale_index" if stale else "no_lexical_match",
                "candidates": candidates, "stale_sources": sorted(stale), "source_bytes_read": 0,
                "index_bytes_read": packed.counted.bytes_read, "index_file_bytes": packed.path.stat().st_size,
                "index_members_loaded": len(packed.cache),
                "evidence_boundary": "Lexical and structural candidates only; index_bytes_read counts logical file reads, including ZIP directory data."}


def read_packed(path, unit_id, *, scope="unit", view="source", measure=False):
    with _PackedIndex(path) as packed:
        unit = packed.unit(unit_id)
        units = [unit]
        if scope == "parent" and unit["parent_id"]:
            units.append(packed.unit(unit["parent_id"]))
        elif scope == "file":
            units.append(packed.unit(packed.manifest["file_units"][unit["path"]]))
        result = read_unit(packed.manifest | {"units": units}, unit_id, scope=scope, view=view)
        if measure:
            result.update(index_bytes_read=packed.counted.bytes_read, index_members_loaded=len(packed.cache))
        return result


def outline_packed(path, *, unit_id=None, offset=0, limit=12, scope="unit", measure=False):
    if offset < 0 or limit < 1 or scope not in {"unit", "parent", "file"}:
        raise ValueError("invalid_outline_request")
    with _PackedIndex(path) as packed:
        if unit_id is not None:
            unit = packed.unit(unit_id)
            if scope == "parent":
                unit_id = unit["parent_id"] or unit_id
            elif scope == "file":
                unit_id = packed.manifest["file_units"][unit["path"]]
        children = packed.unit(unit_id)["children"] if unit_id else packed.manifest["root_units"]
        result = {"parent_id": unit_id, "children": [{key: unit[key] for key in
                ("unit_id", "path", "kind", "title", "preview", "line_start", "line_end")}
                for unit in [packed.unit(child) for child in children[offset:offset + limit]]],
                "next_offset": offset + limit if offset + limit < len(children) else None,
                "total_children": len(children), "source_bytes_read": 0}
        if measure:
            result.update(index_bytes_read=packed.counted.bytes_read, index_members_loaded=len(packed.cache))
        return result


def compact_navigation(result):
    """Remove redundant transport locators, retaining candidate meaning and identity."""
    field = "candidates" if "candidates" in result else "children" if "children" in result else None
    if field is None:
        return result
    sources, cards = {}, []
    for item in result[field]:
        path = item.get("path")
        if path not in sources:
            sources[path] = f"s{len(sources) + 1}"
        card = {key: value for key, value in item.items() if key not in
                {"path", "start_byte", "end_byte", "line_start", "line_end", "parent_id", "score"}}
        card["source"] = sources[path]
        if card.get("preview") == card.get("title"):
            card.pop("preview")
        cards.append(card)
    return {**result, field: cards, "sources": {value: key for key, value in sources.items()},
            "locator_details": "Exact ranges and provenance are returned by read using the same unit_id."}


def save_index(index, destination, *, packed=False):
    destination = Path(destination).resolve()
    originals = {(Path(index["root"]) / relative).resolve() for relative in index["sources"]}
    if destination in originals:
        raise ValueError("index_destination_is_source")
    if destination.exists():
        try:
            if zipfile.is_zipfile(destination):
                with zipfile.ZipFile(destination) as archive:
                    previous = json.loads(archive.read("manifest.json"))
            else:
                previous = json.loads(destination.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            raise ValueError("index_destination_is_not_an_index") from None
        if not isinstance(previous, dict) or previous.get("schema") != SCHEMA:
            raise ValueError("index_destination_is_not_an_index")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w+b", dir=destination.parent,
                                         prefix=destination.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            if packed:
                with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
                    for name, value in _packed_members(index).items():
                        archive.writestr(name, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            else:
                handle.write((json.dumps(index, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "li", "section", "article", "br", "h1", "h2", "h3"}:
            self.parts.append("\n")
    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def read_unit(index, unit_id, *, scope="unit", view="source"):
    if index.get("schema") != SCHEMA:
        raise ValueError("invalid_text_source_index")
    if scope not in {"unit", "parent", "file"} or view not in {"source", "text"}:
        raise ValueError("invalid_read_scope_or_view")
    units = {unit["unit_id"]: unit for unit in index["units"]}
    unit = units[unit_id]
    if scope == "parent" and unit["parent_id"]:
        unit = units[unit["parent_id"]]
    elif scope == "file":
        unit = next(value for value in units.values() if value["path"] == unit["path"] and value["kind"] == "file")
    if not _current(index, unit["path"]):
        raise ValueError(f"source_changed:{unit['path']}")
    path = (Path(index["root"]) / unit["path"]).resolve()
    with path.open("rb") as handle:
        handle.seek(unit["start_byte"])
        raw = handle.read(unit["end_byte"] - unit["start_byte"])
    if not _current(index, unit["path"]) or _hash(raw) != unit["sha256"]:
        raise ValueError(f"source_changed:{unit['path']}")
    source = index["sources"][unit["path"]]
    content = raw[source["bom_bytes"]:] if unit["start_byte"] == 0 else raw
    text = content.decode(source["encoding"], errors="strict")
    transformation = "none"
    if view == "text" and unit["kind"] == "html_block":
        parser = _VisibleText()
        parser.feed(text)
        text, transformation = "".join(parser.parts).strip(), "html_visible_text"
    return {"unit_id": unit["unit_id"], "path": str(path), "text": text,
            "start_byte": unit["start_byte"], "end_byte": unit["end_byte"],
            "line_start": unit["line_start"], "line_end": unit["line_end"],
            "sha256_verified": True, "source_revision": source["sha256"], "origin": source["origin"],
            "revision_check": "file_stat_and_selected_range_hash",
            "bytes_read": len(raw), "transformation": transformation, "scope": scope}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("index", "search", "outline", "read"))
    parser.add_argument("--index", required=True)
    parser.add_argument("--root")
    parser.add_argument("--files", nargs="+")
    parser.add_argument("--origins-file")
    parser.add_argument("--query")
    parser.add_argument("--unit-id")
    parser.add_argument("--scope", choices=("unit", "parent", "file"), default="unit")
    parser.add_argument("--view", choices=("source", "text"), default="source")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--packed", action="store_true")
    parser.add_argument("--ascii-json", action="store_true", help="Use escaped ASCII JSON for legacy transports; default is UTF-8 JSON")
    parser.add_argument("--compact", action="store_true", help="Return concise navigation cards; all candidates and semantic metadata remain available")
    args = parser.parse_args()
    if args.operation == "index":
        if not args.root or not args.files:
            parser.error("index requires --root and --files; no implicit recursive scan")
        origins = json.loads(Path(args.origins_file).read_text(encoding="utf-8")) if args.origins_file else None
        index = build_index(args.root, args.files, origins=origins)
        path = Path(args.index)
        save_index(index, path, packed=args.packed)
        result = index["metrics"] | {"index": str(path.resolve()), "originals_changed": False}
    else:
        with Path(args.index).open("rb") as handle:
            packed = handle.read(4) == b"PK\x03\x04"
        flat_bytes = None if packed else Path(args.index).read_bytes()
        index = None if packed else json.loads(flat_bytes)
        if args.operation == "search":
            if args.query is None:
                parser.error("search requires --query")
            result = search_packed(args.index, args.query, limit=args.limit) if packed else search(index, args.query, limit=args.limit)
        elif args.operation == "outline":
            function, source = (outline_packed, args.index) if packed else (outline, index)
            result = function(source, unit_id=args.unit_id, offset=args.offset, limit=args.limit, scope=args.scope,
                              **({"measure": True} if packed else {}))
        else:
            if not args.unit_id:
                parser.error("read requires --unit-id")
            function, source = (read_packed, args.index) if packed else (read_unit, index)
            result = function(source, args.unit_id, scope=args.scope, view=args.view, **({"measure": True} if packed else {}))
        result["index_detection_bytes_read"] = 4
        result["index_bytes_read"] = result.get("index_bytes_read", 0) + 4 if packed else len(flat_bytes) + 4
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    if args.compact:
        result = compact_navigation(result)
    print(json.dumps(result, ensure_ascii=args.ascii_json))


if __name__ == "__main__":
    main()
