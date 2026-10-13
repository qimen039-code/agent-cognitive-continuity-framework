#!/usr/bin/env python3
"""Build and query a compact registry of locally registered Codex capabilities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tomllib
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "codex.capability_registry.v1"
SUMMARY_SCHEMA = "codex.capability_registry_summary.v1"
DEFAULT_MAP_ROOT = Path(__file__).resolve().parents[1] / "registry.local"
DEFAULT_TTL_HOURS = 24.0
MAX_DESCRIPTION = 360
PROJECTION_SCHEMA = "agent.runtime_projection.v1"
PROJECTION_STATES = {
    "verified",
    "not_checked",
    "not_required",
    "blocked",
    "failed",
    "unavailable",
}
DISCOVERY_STATUSES = {
    "resolved_current_exposure",
    "checked_available",
    "checked_configured_not_exposed",
    "checked_loaded_not_callable",
    "checked_blocked",
    "checked_missing",
    "host_refresh_required",
}


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def normalize_id(value: str) -> str:
    text = value.casefold().replace("_", "-").replace(":", "-")
    text = re.sub(r"[^a-z0-9.-]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-") or "unnamed"


def compact_text(value: Any, limit: int = MAX_DESCRIPTION) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def unique_text(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = compact_text(value, 160)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def parse_metadata_aliases(lines: list[str], end: int) -> list[str]:
    aliases: list[str] = []
    for index in range(1, end):
        if lines[index].strip() != "metadata:" or lines[index].startswith((" ", "\t")):
            continue
        cursor = index + 1
        while cursor < end and (lines[cursor].startswith((" ", "\t")) or not lines[cursor].strip()):
            match = re.match(r"^\s+aliases:\s*(.*)$", lines[cursor])
            if not match:
                cursor += 1
                continue
            inline = match.group(1).strip()
            if inline.startswith("[") and inline.endswith("]"):
                aliases.extend(
                    item.strip().strip("\"'")
                    for item in inline[1:-1].split(",")
                    if item.strip().strip("\"'")
                )
                return unique_text(aliases)
            cursor += 1
            while cursor < end:
                item = re.match(r"^\s+-\s*(.+?)\s*$", lines[cursor])
                if not item:
                    break
                value = item.group(1).strip().strip("\"'")
                if value:
                    aliases.append(value)
                cursor += 1
            return unique_text(aliases)
    return []


def parse_frontmatter(path: Path) -> tuple[str, str, list[str]]:
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return path.parent.name, "", []
    try:
        end = next(i for i in range(1, min(len(lines), 160)) if lines[i].strip() == "---")
    except StopIteration:
        return path.parent.name, "", []

    values: dict[str, str] = {}
    i = 1
    while i < end:
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", lines[i])
        if not match:
            i += 1
            continue
        key, raw = match.group(1), match.group(2).strip()
        if raw in {"|", ">"}:
            parts: list[str] = []
            i += 1
            while i < end and (lines[i].startswith(" ") or not lines[i].strip()):
                if lines[i].strip():
                    parts.append(lines[i].strip())
                i += 1
            values[key] = " ".join(parts)
            continue
        values[key] = raw.strip("\"'")
        i += 1
    return (
        values.get("name", path.parent.name),
        values.get("description", ""),
        parse_metadata_aliases(lines, end),
    )


def classify_family(text: str) -> str:
    haystack = text.casefold()
    groups: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "interactive-control",
            (
                "browser",
                "chrome",
                "computer use",
                "desktop",
                "windows",
                "wps",
                "figma",
                "photoshop",
                "blender",
                "screenshot",
                "keyboard",
                "mouse",
                "playwright",
                "浏览器",
                "桌面",
                "窗口",
                "键盘",
                "鼠标",
            ),
        ),
        (
            "account-service",
            (
                "github",
                "gmail",
                "slack",
                "notion",
                "linear",
                "canva",
                "vercel",
                "supabase",
                "circleci",
                "datadog",
                "twilio",
                "connector",
                "账户",
                "仓库",
                "邮件",
            ),
        ),
        (
            "local-artifact",
            (
                "document",
                "docx",
                "pdf",
                "spreadsheet",
                "presentation",
                "latex",
                "template",
                "image",
                "video",
                "remotion",
                "文档",
                "表格",
                "演示",
                "图片",
                "视频",
            ),
        ),
        (
            "public-read",
            (
                "search",
                "research",
                "paper",
                "documentation",
                "hugging face",
                "web",
                "搜索",
                "检索",
                "论文",
                "资料",
            ),
        ),
        (
            "specialized-local",
            (
                "nvidia",
                "cuda",
                "ios",
                "macos",
                "xcode",
                "temporal",
                "analytics",
                "data visualization",
                "cad",
                "simulation",
                "科研",
                "分析",
            ),
        ),
    )
    for family, terms in groups:
        if any(term in haystack for term in terms):
            return family
    return "general-tool"


def skill_paths(skill_root: Path) -> list[Path]:
    result: list[Path] = []
    if not skill_root.is_dir():
        return result
    for child in sorted(skill_root.iterdir(), key=lambda item: item.name.casefold()):
        if not child.is_dir():
            continue
        direct = child / "SKILL.md"
        if direct.is_file():
            result.append(direct)
            continue
        if child.name == ".system":
            for system_skill in sorted(child.iterdir(), key=lambda item: item.name.casefold()):
                candidate = system_skill / "SKILL.md"
                if candidate.is_file():
                    result.append(candidate)
    return result


def manifest_version_key(path: Path) -> tuple[tuple[int, ...], float]:
    try:
        manifest = read_json(path)
        version = str(manifest.get("version", ""))
    except Exception:
        version = ""
    numbers = tuple(int(value) for value in re.findall(r"\d+", version))
    return numbers, path.stat().st_mtime


def selected_plugin_manifest(cache_root: Path, plugin_id: str) -> Path | None:
    if "@" not in plugin_id:
        return None
    name, source = plugin_id.rsplit("@", 1)
    base = cache_root / source / name
    if not base.is_dir():
        return None
    candidates = list(base.glob("*/.codex-plugin/plugin.json"))
    if not candidates:
        candidates = list(base.glob("**/.codex-plugin/plugin.json"))
    return max(candidates, key=manifest_version_key) if candidates else None


def registration_fingerprint(
    plugin_states: dict[str, bool],
    mcp_state: dict[str, dict[str, Any]],
    skill_state: dict[str, bool],
    source_hashes: dict[str, str],
) -> str:
    payload = {
        "plugins": plugin_states,
        "mcp": mcp_state,
        "skills": skill_state,
        "sources": source_hashes,
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(encoded.encode("ascii"))


def collect_registry(codex_home: Path) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    config_path = codex_home / "config.toml"
    config: dict[str, Any] = {}
    if config_path.is_file():
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)

    configured_plugins = config.get("plugins") or {}
    plugin_states = {
        str(plugin_id): bool((settings or {}).get("enabled", True))
        for plugin_id, settings in configured_plugins.items()
        if isinstance(settings, dict)
    }
    configured_skills = {
        str(entry.get("name")): bool(entry.get("enabled", True))
        for entry in ((config.get("skills") or {}).get("config") or [])
        if isinstance(entry, dict) and entry.get("name")
    }
    configured_skill_paths = {
        str(Path(entry["path"]).resolve()).casefold(): bool(entry.get("enabled", True))
        for entry in ((config.get("skills") or {}).get("config") or [])
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    configured_mcp = config.get("mcp_servers") or {}
    mcp_state = {
        str(name): {
            "enabled": not bool((settings or {}).get("disabled", False)),
            "command": compact_text((settings or {}).get("command"), 240),
            "url": compact_text((settings or {}).get("url"), 240),
        }
        for name, settings in configured_mcp.items()
        if isinstance(settings, dict)
    }

    surfaces: list[dict[str, Any]] = []
    source_hashes: dict[str, str] = {}
    diagnostics: list[dict[str, str]] = []

    def add_surface(record: dict[str, Any]) -> None:
        source_path = Path(str(record.get("source_ref", "")))
        if source_path.name == "SKILL.md":
            metadata_path = source_path.parent / "agents" / "openai.yaml"
            record["allow_implicit_invocation"] = True
            if metadata_path.is_file():
                source_hashes[str(metadata_path)] = sha256_file(metadata_path)
                # Read the documented scalar policy form; do not interpret skill prose.
                in_policy = False
                for line in metadata_path.read_text(encoding="utf-8-sig").splitlines():
                    if re.match(r"^policy:\s*(?:#.*)?$", line):
                        in_policy = True
                        continue
                    if line and not line[0].isspace() and not line.startswith("#"):
                        in_policy = False
                    if in_policy:
                        match = re.match(r"^\s+allow_implicit_invocation:\s*(true|false)\s*(?:#.*)?$", line, re.I)
                        if match:
                            record["allow_implicit_invocation"] = match.group(1).casefold() == "true"
        record["schema"] = SCHEMA
        record["description"] = compact_text(record.get("description"))
        record["aliases"] = unique_text(record.get("aliases") or [])
        searchable = " ".join(
            [
                str(record.get("name", "")),
                str(record.get("description", "")),
                " ".join(record["aliases"]),
            ]
        )
        record.setdefault("family_id", classify_family(searchable))
        record.setdefault("current_exposure_state", "not_checked_current_task")
        record.setdefault("runtime_truth_required", True)
        surfaces.append(record)

    for path in skill_paths(codex_home / "skills"):
        try:
            name, description, metadata_aliases = parse_frontmatter(path)
            source_hashes[str(path)] = sha256_file(path)
        except (OSError, UnicodeError, ValueError) as error:
            diagnostics.append({"source": str(path), "error": type(error).__name__})
            continue
        enabled = configured_skill_paths.get(str(path.resolve()).casefold(), configured_skills.get(name, True))
        add_surface(
            {
                "capability_id": f"skill:{normalize_id(name)}",
                "surface_id": f"standalone-skill:{normalize_id(name)}",
                "kind": "standalone_skill",
                "name": name,
                "description": description,
                "aliases": [path.parent.name, name.replace("-", " "), *metadata_aliases],
                "registration_state": "registered_enabled" if enabled else "registered_disabled",
                "source_ref": str(path),
                "discovery_terms": [name, path.parent.name, *metadata_aliases],
            }
        )

    cache_root = codex_home / "plugins" / "cache"
    for plugin_id, enabled in sorted(plugin_states.items()):
        manifest_path = selected_plugin_manifest(cache_root, plugin_id)
        if manifest_path is None:
            add_surface(
                {
                    "capability_id": f"plugin:{normalize_id(plugin_id)}",
                    "surface_id": f"plugin-config:{normalize_id(plugin_id)}",
                    "kind": "plugin",
                    "name": plugin_id,
                    "description": "Configured plugin with no matching local manifest in the active cache.",
                    "aliases": [plugin_id.split("@", 1)[0]],
                    "registration_state": (
                        "configured_enabled_manifest_missing"
                        if enabled
                        else "configured_disabled_manifest_missing"
                    ),
                    "source_ref": f"{config_path}#[plugins.{plugin_id}]",
                    "discovery_terms": [plugin_id, plugin_id.split("@", 1)[0]],
                }
            )
            continue
        try:
            manifest = read_json(manifest_path)
            source_hashes[str(manifest_path)] = sha256_file(manifest_path)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            diagnostics.append({"source": str(manifest_path), "error": type(error).__name__})
            continue

        plugin_root = manifest_path.parent.parent
        name = str(manifest.get("name") or plugin_id.split("@", 1)[0])
        interface = manifest.get("interface") or {}
        aliases = [
            plugin_id,
            name,
            interface.get("displayName"),
            *(manifest.get("keywords") or []),
        ]
        components = {
            "skills": bool(manifest.get("skills")),
            "apps": bool(manifest.get("apps")),
            "mcp_servers": bool(manifest.get("mcpServers")),
        }
        description = (
            interface.get("longDescription")
            or manifest.get("description")
            or interface.get("shortDescription")
            or ""
        )
        capability_id = f"plugin:{normalize_id(plugin_id)}"
        add_surface(
            {
                "capability_id": capability_id,
                "surface_id": f"plugin:{normalize_id(plugin_id)}",
                "kind": "plugin",
                "name": name,
                "description": description,
                "aliases": aliases,
                "registration_state": "registered_enabled" if enabled else "registered_disabled",
                "source_ref": str(manifest_path),
                "discovery_terms": unique_text([name, plugin_id, *(manifest.get("keywords") or [])]),
                "components": components,
            }
        )

        skill_refs = manifest.get("skills")
        skill_roots = skill_refs if isinstance(skill_refs, list) else [skill_refs]
        for skill_ref in (item for item in skill_roots if isinstance(item, str) and item):
            root = (plugin_root / skill_ref).resolve()
            if not root.is_dir():
                continue
            for skill_path in sorted(root.rglob("SKILL.md")):
                try:
                    skill_name, skill_description, skill_metadata_aliases = parse_frontmatter(skill_path)
                    source_hashes[str(skill_path)] = sha256_file(skill_path)
                except (OSError, UnicodeError, ValueError) as error:
                    diagnostics.append({"source": str(skill_path), "error": type(error).__name__})
                    continue
                add_surface(
                    {
                        "capability_id": capability_id,
                        "surface_id": (
                            f"plugin-skill:{normalize_id(plugin_id)}:{normalize_id(skill_name)}"
                        ),
                        "kind": "plugin_skill",
                        "name": skill_name,
                        "description": skill_description,
                        "aliases": [name, plugin_id, skill_path.parent.name, *skill_metadata_aliases],
                        "registration_state": (
                            "registered_enabled"
                            if enabled and configured_skill_paths.get(
                                str(skill_path.resolve()).casefold(),
                                configured_skills.get(f"{name}:{skill_name}", configured_skills.get(skill_name, True)),
                            ) else "registered_disabled"
                        ),
                        "source_ref": str(skill_path),
                        "discovery_terms": unique_text([skill_name, name, plugin_id, *skill_metadata_aliases]),
                    }
                )

    for name, settings in sorted(configured_mcp.items()):
        if not isinstance(settings, dict):
            continue
        enabled = not bool(settings.get("disabled", False))
        command = compact_text(settings.get("command"), 240)
        url = compact_text(settings.get("url"), 240)
        command_name = Path(command).stem if command else ""
        aliases = [name, name.replace("_", " "), command_name]
        add_surface(
            {
                "capability_id": f"mcp:{normalize_id(name)}",
                "surface_id": f"mcp-config:{normalize_id(name)}",
                "kind": "mcp_server",
                "name": name,
                "description": f"Configured MCP server {name}.",
                "aliases": aliases,
                "registration_state": "configured_enabled" if enabled else "configured_disabled",
                "source_ref": f"{config_path}#[mcp_servers.{name}]",
                "discovery_terms": unique_text(
                    [name, name.replace("_", " "), f"mcp__{name}__", command_name, url]
                ),
                "expected_tool_prefix": f"mcp__{name}__",
            }
        )

    surfaces.sort(
        key=lambda record: (
            str(record.get("family_id", "")),
            str(record.get("capability_id", "")),
            str(record.get("surface_id", "")),
        )
    )
    fingerprint = registration_fingerprint(
        plugin_states, mcp_state,
        {**configured_skills, **{f"path:{key}": value for key, value in configured_skill_paths.items()}},
        source_hashes,
    )
    evidence = {
        "config_path": str(config_path),
        "source_file_count": len(source_hashes),
        "registered_plugin_count": len(plugin_states),
        "configured_mcp_count": len(configured_mcp),
        "standalone_skill_count": sum(
            1 for record in surfaces if record["kind"] == "standalone_skill"
        ),
        "diagnostics": diagnostics,
    }
    return surfaces, fingerprint, evidence


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    try:
        temp_path.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def write_registry(
    inventory_root: Path,
    surfaces: list[dict[str, Any]],
    fingerprint: str,
    evidence: dict[str, Any],
    ttl_hours: float,
    reason: str,
    miss_query_hash: str | None = None,
) -> dict[str, Any]:
    generated_at = utc_now()
    counts = Counter(str(record.get("kind", "unknown")) for record in surfaces)
    families = Counter(str(record.get("family_id", "unknown")) for record in surfaces)
    summary = {
        "schema": SUMMARY_SCHEMA,
        "generated_at": iso_utc(generated_at),
        "fresh_until": iso_utc(generated_at + timedelta(hours=ttl_hours)),
        "ttl_hours": ttl_hours,
        "refresh_reason": reason,
        "source_fingerprint": fingerprint,
        "surface_count": len(surfaces),
        "surface_counts_by_kind": dict(sorted(counts.items())),
        "surface_counts_by_family": dict(sorted(families.items())),
        "registry_path": str(inventory_root / "surfaces.jsonl"),
        "evidence": evidence,
        "refresh_policy": {
            "mode": "lazy_ttl_plus_source_fingerprint_plus_single_miss_refresh",
            "background_schedule": False,
            "miss_refresh_limit_per_query": 1,
        },
        "truth_boundary": (
            "Registration metadata is persistent discovery evidence only. "
            "Current exposure, authentication, callability, and action coverage "
            "must be checked in the active task."
        ),
    }
    if miss_query_hash:
        summary["last_miss_refresh"] = {
            "query_hash": miss_query_hash,
            "source_fingerprint": fingerprint,
            "performed_at": iso_utc(generated_at),
        }
    jsonl = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in surfaces
    )
    atomic_write_text(inventory_root / "surfaces.jsonl", jsonl)
    atomic_write_text(
        inventory_root / "current.json",
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return summary


def load_summary(path: Path) -> dict[str, Any] | None:
    try:
        value = read_json(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    return value if value.get("schema") == SUMMARY_SCHEMA else None


def summary_is_fresh(
    summary: dict[str, Any] | None,
    fingerprint: str,
    surfaces_path: Path,
) -> bool:
    if summary is None or not surfaces_path.is_file():
        return False
    if summary.get("source_fingerprint") != fingerprint:
        return False
    try:
        fresh_until = datetime.fromisoformat(str(summary["fresh_until"]))
    except (KeyError, TypeError, ValueError):
        return False
    return utc_now() <= fresh_until.astimezone(timezone.utc)


def load_surfaces(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def query_tokens(query: str) -> list[str]:
    raw = [
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9_.:+-]+|[\u3400-\u9fff]+", query)
    ]
    return unique_text(token for token in raw if len(token) >= 2)


def record_has_explicit_anchor(record: dict[str, Any], anchor: str) -> bool:
    raw_anchor = compact_text(anchor, 240).casefold()
    if not raw_anchor:
        return False
    name = str(record.get("name", "")).casefold()
    aliases = [str(value).casefold() for value in record.get("aliases") or []]
    discovery = [str(value).casefold() for value in record.get("discovery_terms") or []]
    return raw_anchor == name or raw_anchor in aliases or raw_anchor in discovery


def record_matches_anchor(record: dict[str, Any], anchor: str) -> bool:
    if record_has_explicit_anchor(record, anchor):
        return True
    raw_anchor = compact_text(anchor, 240).casefold()
    if not raw_anchor:
        return False
    name = str(record.get("name", "")).casefold()
    aliases = [str(value).casefold() for value in record.get("aliases") or []]
    discovery = [str(value).casefold() for value in record.get("discovery_terms") or []]
    anchor_tokens = set(query_tokens(raw_anchor))
    searchable_tokens = set(
        query_tokens(
            " ".join(
                [
                    name,
                    str(record.get("description", "")),
                    " ".join(aliases),
                    " ".join(discovery),
                ]
            )
        )
    )
    return bool(anchor_tokens) and anchor_tokens.issubset(searchable_tokens)


def record_score(
    record: dict[str, Any],
    tokens: list[str],
    raw_query: str,
    anchor_tokens: list[str],
) -> int:
    name = str(record.get("name", "")).casefold()
    aliases = [str(value).casefold() for value in record.get("aliases") or []]
    discovery = [str(value).casefold() for value in record.get("discovery_terms") or []]
    haystack = " ".join(
        [
            name,
            str(record.get("description", "")).casefold(),
            " ".join(aliases),
            " ".join(discovery),
            str(record.get("family_id", "")).casefold(),
        ]
    )
    score = 0
    for anchor in anchor_tokens:
        if anchor == name:
            score += 100
        elif anchor in name:
            score += 70
        if anchor in aliases:
            score += 90
        elif any(anchor in alias for alias in aliases):
            score += 60
        if anchor in discovery:
            score += 40
    if raw_query and raw_query in haystack:
        score += 20
    for token in tokens:
        if token == name:
            score += 22
        elif token in name:
            score += 16
        if token in aliases:
            score += 14
        elif any(token in alias for alias in aliases):
            score += 10
        if token in discovery:
            score += 5
        elif token in haystack:
            score += 2
    if str(record.get("registration_state", "")).endswith("disabled"):
        score -= 1
    return score


def query_registry(
    surfaces: list[dict[str, Any]],
    query: str,
    anchor: str,
    family: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    tokens = query_tokens(query)
    anchor_tokens = query_tokens(anchor)
    raw_query = compact_text(query, 500).casefold()
    eligible = [
        record
        for record in surfaces
        if (not family or record.get("family_id") == family)
        and not str(record.get("registration_state", "")).endswith("disabled")
        and (record.get("allow_implicit_invocation", True) or record_has_explicit_anchor(record, anchor))
    ]
    explicit_anchor_matches = [
        record for record in eligible if record_has_explicit_anchor(record, anchor)
    ]
    semantic_anchor_matches = [
        record for record in eligible if record_matches_anchor(record, anchor)
    ]
    if explicit_anchor_matches:
        eligible = explicit_anchor_matches
    elif semantic_anchor_matches:
        eligible = semantic_anchor_matches
    elif anchor.strip():
        return []
    ranked: list[tuple[int, dict[str, Any]]] = []
    for record in eligible:
        score = record_score(record, tokens, raw_query, anchor_tokens)
        if score > 0:
            ranked.append((score, record))
    ranked.sort(
        key=lambda item: (
            -item[0],
            str(item[1].get("capability_id", "")),
            str(item[1].get("surface_id", "")),
        )
    )

    selected: list[dict[str, Any]] = []
    seen_capabilities: set[str] = set()
    seen_names: set[str] = set()
    for score, record in ranked:
        capability_id = str(record.get("capability_id", ""))
        name_key = normalize_id(str(record.get("name", "")))
        if capability_id in seen_capabilities or name_key in seen_names:
            continue
        seen_capabilities.add(capability_id)
        seen_names.add(name_key)
        selected.append(
            {
                "score": score,
                "capability_id": capability_id,
                "surface_id": record.get("surface_id"),
                "kind": record.get("kind"),
                "family_id": record.get("family_id"),
                "name": record.get("name"),
                "description": record.get("description"),
                "registration_state": record.get("registration_state"),
                "current_exposure_state": record.get("current_exposure_state"),
                "allow_implicit_invocation": record.get("allow_implicit_invocation", True),
                "discovery_terms": record.get("discovery_terms"),
                "expected_tool_prefix": record.get("expected_tool_prefix"),
                "source_ref": record.get("source_ref"),
            }
        )
        if len(selected) >= limit:
            break
    return selected


def _projection_axis(value: Any, *, default: str = "not_checked") -> str:
    text = str(value or default)
    return text if text in PROJECTION_STATES else default


def _minimal_surface(candidate: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(candidate, Mapping):
        return None
    return {
        "capability_id": candidate.get("capability_id"),
        "surface_id": candidate.get("surface_id"),
        "kind": candidate.get("kind"),
        "name": candidate.get("name"),
    }


def _registration_axis(candidate: Mapping[str, Any] | None) -> str:
    if not isinstance(candidate, Mapping):
        return "unavailable"
    state = str(candidate.get("registration_state") or "")
    return "unavailable" if state.endswith("disabled") else "verified"


def build_capability_route_projection(
    *,
    query: str,
    anchor: str,
    family: str | None,
    source_fingerprint: str,
    candidates: Sequence[Mapping[str, Any]],
    live_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one bounded discovery view without upgrading live capability facts."""

    bounded = [dict(item) for item in candidates if isinstance(item, Mapping)][:2]
    live = dict(live_state) if isinstance(live_state, Mapping) else {}
    selected = bounded[0] if len(bounded) == 1 else None
    axes = {
        "registered_or_configured": _registration_axis(selected),
        "current_exposed": _projection_axis(live.get("current_exposed")),
        "authenticated": _projection_axis(live.get("authenticated")),
        "callable": _projection_axis(live.get("callable")),
        "action_covered": _projection_axis(live.get("action_covered")),
    }
    requested_status = str(live.get("discovery_status") or "")
    if requested_status in DISCOVERY_STATUSES:
        discovery_status = requested_status
    elif not bounded:
        discovery_status = "checked_missing"
    elif axes["current_exposed"] == "verified" and axes["callable"] == "verified":
        discovery_status = "checked_available"
    else:
        discovery_status = "checked_configured_not_exposed"
    coverage = (
        "unresolved"
        if not bounded
        else "complete" if len(bounded) == 1 else "semantic_review_required"
    )
    preferred = (
        selected.get("expected_tool_prefix") or selected.get("name")
        if selected is not None
        else None
    )
    payload = {
        "intent_signature": {
            "query": compact_text(query, 500),
            "exact_anchor": compact_text(anchor, 240) or None,
        },
        "family_id": family,
        "selected_surface": _minimal_surface(selected),
        "state_axes": axes,
        "discovery_status": discovery_status,
        "preferred_call_surface": preferred,
        "ambiguity": {
            "status": (
                "semantic_review_required" if len(bounded) > 1 else "none"
            ),
            "candidate_surface_ids": [
                str(item.get("surface_id"))
                for item in bounded
                if item.get("surface_id") is not None
            ] if len(bounded) > 1 else [],
        },
        "next_action": (
            "use_current_exposed_surface"
            if discovery_status == "resolved_current_exposure"
            else "exact_current_task_discovery"
            if len(bounded) == 1
            else "model_semantic_selection_then_exact_discovery"
            if len(bounded) > 1
            else "perform_one_runtime_surface_refresh_then_recheck"
        ),
    }
    base = {
        "schema": PROJECTION_SCHEMA,
        "projection_type": "capability_route",
        "task_scope": {
            "intent_signature_digest": sha256_bytes(
                json.dumps(
                    payload["intent_signature"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        },
        "source_digest": source_fingerprint,
        "coverage_status": coverage,
        "control": {
            "runtime_truth_required": True,
            "discovery_status": discovery_status,
        },
        "payload": payload,
        "source_refs": [
            str(item.get("source_ref"))
            for item in bounded
            if item.get("source_ref")
        ],
        "invalidates_on": [
            "intent_change",
            "current_exposure_change",
            "authentication_or_call_failure",
            "registry_source_fingerprint_change",
            "task_end",
        ],
        "expires_when": "task_or_current_exposure_changes",
        "authority_granted": False,
    }
    projection_id = sha256_bytes(
        json.dumps(
            base,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return {**base, "projection_id": projection_id}


def build_query_response(
    *,
    response_profile: str,
    query: str,
    anchor: str,
    family: str | None,
    refresh_status: str,
    refresh_reason: str,
    miss_refresh_performed: bool,
    miss_refresh_suppressed: bool,
    source_fingerprint: str,
    surface_count: int,
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if response_profile == "projection":
        return build_capability_route_projection(
            query=query,
            anchor=anchor,
            family=family,
            source_fingerprint=source_fingerprint,
            candidates=candidates,
        )
    if response_profile != "diagnostic":
        raise ValueError("unsupported_response_profile")
    return {
        "schema": "codex.capability_registry_query.v1",
        "query": query,
        "anchor": anchor or None,
        "family": family,
        "refresh_status": refresh_status,
        "refresh_reason": refresh_reason,
        "miss_refresh_performed": miss_refresh_performed,
        "miss_refresh_suppressed": miss_refresh_suppressed,
        "source_fingerprint": source_fingerprint,
        "surface_count": surface_count,
        "candidate_count": len(candidates),
        "candidates": list(candidates),
        "runtime_truth_required": True,
        "next_action": (
            "exact_current_task_discovery"
            if candidates
            else "perform_one_runtime_surface_refresh_then_recheck"
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh and query the compact Codex capability registry."
    )
    parser.add_argument("--query", default="", help="Bilingual discovery terms.")
    parser.add_argument(
        "--anchor",
        default="",
        help="Exact user-named product, service, format, skill, plugin, MCP, or tool.",
    )
    parser.add_argument("--family", default="", help="Optional exact family id.")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--response-profile",
        choices=("projection", "diagnostic"),
        default="diagnostic",
    )
    parser.add_argument(
        "--no-refresh-on-miss",
        action="store_true",
        help="Do not perform the one allowed forced registry rebuild after a query miss.",
    )
    parser.add_argument("--map-root", type=Path, default=DEFAULT_MAP_ROOT)
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(
            os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
        ),
    )
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    if args.limit < 1 or args.limit > 32:
        raise SystemExit("--limit must be between 1 and 32")
    if args.ttl_hours <= 0:
        raise SystemExit("--ttl-hours must be positive")

    inventory_root = args.map_root / "inventory"
    summary_path = inventory_root / "current.json"
    surfaces_path = inventory_root / "surfaces.jsonl"
    surfaces, fingerprint, evidence = collect_registry(args.codex_home)
    previous = load_summary(summary_path)

    if args.force:
        refresh_reason = "forced"
    elif previous is None:
        refresh_reason = "initial_or_schema_migration"
    elif previous.get("source_fingerprint") != fingerprint:
        refresh_reason = "source_fingerprint_changed"
    elif not summary_is_fresh(previous, fingerprint, surfaces_path):
        refresh_reason = "ttl_expired"
    else:
        refresh_reason = ""

    if refresh_reason:
        summary = write_registry(
            inventory_root,
            surfaces,
            fingerprint,
            evidence,
            args.ttl_hours,
            refresh_reason,
        )
        refresh_status = "refreshed"
    else:
        summary = previous or {}
        surfaces = load_surfaces(surfaces_path)
        refresh_status = "cached"

    candidates = (
        query_registry(
            surfaces, args.query, args.anchor, args.family or None, args.limit
        )
        if args.query
        else []
    )
    miss_refresh_performed = False
    miss_refresh_suppressed = False
    if args.query and not candidates and refresh_status == "refreshed":
        miss_key = f"{args.anchor.casefold().strip()}\n{args.query.casefold().strip()}"
        query_hash = sha256_bytes(miss_key.encode("utf-8"))
        summary["last_miss_refresh"] = {
            "query_hash": query_hash,
            "source_fingerprint": fingerprint,
            "performed_at": summary.get("generated_at"),
        }
        atomic_write_text(
            summary_path,
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    if (
        args.query
        and not candidates
        and not args.no_refresh_on_miss
        and refresh_status == "cached"
    ):
        miss_key = f"{args.anchor.casefold().strip()}\n{args.query.casefold().strip()}"
        query_hash = sha256_bytes(miss_key.encode("utf-8"))
        last_miss = summary.get("last_miss_refresh") or {}
        if (
            last_miss.get("query_hash") == query_hash
            and last_miss.get("source_fingerprint") == fingerprint
        ):
            miss_refresh_suppressed = True
        else:
            summary = write_registry(
                inventory_root,
                surfaces,
                fingerprint,
                evidence,
                args.ttl_hours,
                "query_miss_single_refresh",
                miss_query_hash=query_hash,
            )
            refresh_status = "refreshed"
            miss_refresh_performed = True
            candidates = query_registry(
                surfaces,
                args.query,
                args.anchor,
                args.family or None,
                args.limit,
            )

    output = build_query_response(
        response_profile=args.response_profile,
        query=args.query,
        anchor=args.anchor,
        family=args.family or None,
        refresh_status=refresh_status,
        refresh_reason=(
            summary.get("refresh_reason")
            if refresh_status == "refreshed"
            else "cache_fresh"
        ),
        miss_refresh_performed=miss_refresh_performed,
        miss_refresh_suppressed=miss_refresh_suppressed,
        source_fingerprint=fingerprint,
        surface_count=len(surfaces),
        candidates=candidates,
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
