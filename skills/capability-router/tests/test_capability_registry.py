from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "capability_registry.py"
SPEC = importlib.util.spec_from_file_location("capability_registry", SCRIPT)
assert SPEC and SPEC.loader
capability_registry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capability_registry)


def candidate(index: int, *, exposed: str = "not_checked_current_task") -> dict[str, object]:
    return {
        "score": 100 - index,
        "capability_id": f"capability-{index}",
        "surface_id": f"surface-{index}",
        "kind": "plugin_skill",
        "family_id": "interactive-control",
        "name": f"wps-control-{index}",
        "description": "SECRET-LONG-DESCRIPTION " + ("x" * 500),
        "registration_state": "registered_enabled",
        "current_exposure_state": exposed,
        "discovery_terms": ["SECRET-DISCOVERY-TERM"],
        "expected_tool_prefix": "mcp__wps__",
        "source_ref": f"plugin:surface-{index}",
        "schema": {"secret": "must-not-project"},
        "token": "must-not-project",
    }


def projection(*, fingerprint: str = "a" * 64, candidates=None, live_state=None):
    return capability_registry.build_capability_route_projection(
        query="WPS 表格 写入",
        anchor="WPS",
        family="interactive-control",
        source_fingerprint=fingerprint,
        candidates=list(candidates or [candidate(1)]),
        live_state=live_state,
    )


def test_registered_candidate_keeps_live_axes_unverified() -> None:
    result = projection()
    axes = result["payload"]["state_axes"]

    assert result["schema"] == "agent.runtime_projection.v1"
    assert result["projection_type"] == "capability_route"
    assert axes == {
        "registered_or_configured": "verified",
        "current_exposed": "not_checked",
        "authenticated": "not_checked",
        "callable": "not_checked",
        "action_covered": "not_checked",
    }
    assert result["payload"]["discovery_status"] == "checked_configured_not_exposed"
    assert result["authority_granted"] is False


def test_live_state_can_verify_axes_without_registry_inference() -> None:
    result = projection(
        live_state={
            "current_exposed": "verified",
            "authenticated": "not_required",
            "callable": "verified",
            "action_covered": "verified",
            "discovery_status": "resolved_current_exposure",
        }
    )

    assert result["payload"]["state_axes"]["registered_or_configured"] == "verified"
    assert result["payload"]["state_axes"]["current_exposed"] == "verified"
    assert result["payload"]["state_axes"]["action_covered"] == "verified"
    assert result["payload"]["discovery_status"] == "resolved_current_exposure"


def test_projection_is_bounded_and_does_not_copy_registry_bodies() -> None:
    result = projection(candidates=[candidate(1), candidate(2), candidate(3)])
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["coverage_status"] == "semantic_review_required"
    assert result["payload"]["selected_surface"] is None
    assert result["payload"]["ambiguity"] == {
        "status": "semantic_review_required",
        "candidate_surface_ids": ["surface-1", "surface-2"],
    }
    assert "surface-3" not in serialized
    assert "SECRET-LONG-DESCRIPTION" not in serialized
    assert "SECRET-DISCOVERY-TERM" not in serialized
    assert "must-not-project" not in serialized


def test_projection_id_is_deterministic_and_source_fingerprint_bound() -> None:
    first = projection()
    replay = projection()
    changed = projection(fingerprint="b" * 64)

    assert first["projection_id"] == replay["projection_id"]
    assert first["projection_id"] != changed["projection_id"]
    assert first["source_digest"] == "a" * 64
    assert changed["source_digest"] == "b" * 64


def test_query_response_profile_preserves_diagnostic_compatibility() -> None:
    diagnostic = capability_registry.build_query_response(
        response_profile="diagnostic",
        query="WPS",
        anchor="WPS",
        family="interactive-control",
        refresh_status="cached",
        refresh_reason="cache_fresh",
        miss_refresh_performed=False,
        miss_refresh_suppressed=False,
        source_fingerprint="a" * 64,
        surface_count=10,
        candidates=[candidate(1)],
    )
    compact = capability_registry.build_query_response(
        response_profile="projection",
        query="WPS",
        anchor="WPS",
        family="interactive-control",
        refresh_status="cached",
        refresh_reason="cache_fresh",
        miss_refresh_performed=False,
        miss_refresh_suppressed=False,
        source_fingerprint="a" * 64,
        surface_count=10,
        candidates=[candidate(1)],
    )

    assert diagnostic["schema"] == "codex.capability_registry_query.v1"
    assert diagnostic["candidates"][0]["description"].startswith("SECRET")
    assert compact["schema"] == "agent.runtime_projection.v1"
    assert "candidates" not in compact
    assert len(json.dumps(compact, ensure_ascii=False)) < len(
        json.dumps(diagnostic, ensure_ascii=False)
    )


def test_exact_anchor_filters_generic_registry_candidates() -> None:
    surfaces = [
        {
            "capability_id": "skill:codex-troubleshooting-skill-matrix",
            "surface_id": "standalone-skill:codex-troubleshooting-skill-matrix",
            "kind": "standalone_skill",
            "family_id": "general-tool",
            "name": "codex-troubleshooting-skill-matrix",
            "description": "Use when work mentions CBH or the Codex embedded harness.",
            "aliases": ["CBH", "codex troubleshooting skill matrix"],
            "discovery_terms": ["codex-troubleshooting-skill-matrix"],
            "registration_state": "registered_enabled",
        },
        {
            "capability_id": "plugin:generic-registry",
            "surface_id": "plugin-skill:generic-registry",
            "kind": "plugin_skill",
            "family_id": "general-tool",
            "name": "generic-registry",
            "description": "Use for framework registry and architecture discovery.",
            "aliases": ["registry"],
            "discovery_terms": ["framework", "registry"],
            "registration_state": "registered_enabled",
        },
        {
            "capability_id": "skill:skillopt-training-layer",
            "surface_id": "standalone-skill:skillopt-training-layer",
            "kind": "standalone_skill",
            "family_id": "general-tool",
            "name": "skillopt-training-layer",
            "description": "Optional optimizer that is not a public CBH runtime component.",
            "aliases": ["skillopt training layer"],
            "discovery_terms": ["skillopt-training-layer"],
            "registration_state": "registered_enabled",
        },
    ]

    candidates = capability_registry.query_registry(
        surfaces,
        query="CBH framework canonical location and registry",
        anchor="CBH",
        family=None,
        limit=8,
    )

    assert [item["surface_id"] for item in candidates] == [
        "standalone-skill:codex-troubleshooting-skill-matrix"
    ]


def test_frontmatter_metadata_aliases_are_available_for_exact_discovery(tmp_path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "---\n"
        "name: codex-troubleshooting-skill-matrix\n"
        "description: Use when work mentions CBH.\n"
        "metadata:\n"
        "  aliases:\n"
        "    - CBH\n"
        "    - Codex embedded harness\n"
        "---\n",
        encoding="utf-8",
    )

    name, description, aliases = capability_registry.parse_frontmatter(skill)

    assert name == "codex-troubleshooting-skill-matrix"
    assert description == "Use when work mentions CBH."
    assert aliases == ["CBH", "Codex embedded harness"]


def test_named_missing_product_is_not_replaced_by_a_generic_match() -> None:
    rows = [{**candidate(1), "name": "generic-browser", "description": "browser automation", "aliases": ["browser"]}]
    assert capability_registry.query_registry(rows, "browser automation", "UserSelectedBrowser", None, 4) == []


def test_disabled_skills_are_not_execution_candidates() -> None:
    rows = [{**candidate(1), "registration_state": "registered_disabled"}]
    assert capability_registry.query_registry(rows, "wps", "", None, 4) == []


def test_official_path_disable_applies_to_standalone_skills(tmp_path) -> None:
    skill = tmp_path / "skills" / "example" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: example\ndescription: example action\n---\n", encoding="utf-8")
    (tmp_path / "config.toml").write_text(
        "[[skills.config]]\npath = " + json.dumps(str(skill)) + "\nenabled = false\n", encoding="utf-8")
    rows, disabled_fingerprint, _ = capability_registry.collect_registry(tmp_path)
    assert next(row for row in rows if row["name"] == "example")["registration_state"] == "registered_disabled"
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    _, enabled_fingerprint, _ = capability_registry.collect_registry(tmp_path)
    assert disabled_fingerprint != enabled_fingerprint


def test_explicit_only_skill_policy_is_preserved_and_not_implicitly_suggested(tmp_path) -> None:
    skill = tmp_path / "skills" / "workflow" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: workflow\ndescription: workflow planning\n---\n", encoding="utf-8")
    rows_before, fingerprint_before, _ = capability_registry.collect_registry(tmp_path)
    agents = skill.parent / "agents"
    agents.mkdir()
    (agents / "openai.yaml").write_text("interface:\n  display_name: Workflow\npolicy:\n  allow_implicit_invocation: false\n", encoding="utf-8")
    rows, fingerprint, _ = capability_registry.collect_registry(tmp_path)
    assert fingerprint != fingerprint_before
    assert capability_registry.query_registry(rows_before, "workflow", "", None, 4)
    assert capability_registry.query_registry(rows, "workflow", "", None, 4) == []
    explicit = capability_registry.query_registry(rows, "workflow", "workflow", None, 4)
    assert explicit[0]["allow_implicit_invocation"] is False
