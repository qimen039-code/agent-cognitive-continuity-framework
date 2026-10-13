from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import pytest


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "integrations" / "codex-local"


def load_bundle_module():
    path = INTEGRATION / "build-deployment-bundle.py"
    spec = importlib.util.spec_from_file_location("accf_build_deployment_bundle", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load deployment bundle builder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_codex_profile_resolves_a_complete_runtime_bundle() -> None:
    bundle = load_bundle_module()
    profile, files = bundle.selected_files("codex-local-minimal")

    assert profile["required_predeployment_read"] == "docs/agent-deployment-map.md"
    assert profile["runtime_mode"] == "instruction_plus_selective_scripts"
    assert profile["host_blocking"] is False
    assert files
    required = {
        "AGENTS.md",
        "skills/embedded-harness/embedded_harness_policy.json",
        "skills/embedded-harness/behavior_correction_hook.py",
        "skills/embedded-harness/behavior_correction_profiles.json",
        "skills/embedded-harness/engineering_execution.py",
        "skills/embedded-harness/external_route_trigger_helpers.ps1",
        "skills/embedded-harness/external_retrieval_strategy.py",
        "skills/embedded-harness/harness_action_consumer.py",
        "skills/embedded-harness/harness_intake_router.ps1",
        "skills/embedded-harness/harness_memory_isolation_gate.ps1",
        "skills/embedded-harness/harness_claim_schema_verifier.ps1",
        "skills/embedded-harness/nested_tool_preflight.py",
        "skills/embedded-harness/runtime_projection.py",
        "skills/embedded-harness/semantic_memory.py",
        "skills/embedded-harness/task_continuity.py",
        "skills/embedded-harness/validate_policy.ps1",
    }
    assert required.issubset(files)
    if "skills/embedded-harness/harness_action_consumer.py" in files:
        assert "skills/embedded-harness/external_retrieval_strategy.py" in files
    assert not any(path.startswith("docs/") for path in files)
    assert not any("/tests/" in f"/{path}/" for path in files)
    for relative in files:
        assert (ROOT / relative).is_file(), relative


@pytest.mark.parametrize("profile_name", ["codex-local-minimal", "codex-local-source-recall"])
def test_codex_bundle_receipt_matches_staged_files(profile_name: str) -> None:
    bundle = load_bundle_module()
    temp_root = INTEGRATION / ".test-tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="deployment-", dir=temp_root))
    try:
        receipt = bundle.stage(profile_name, output)
        stored = json.loads(
            (output / "cbh-deployment-receipt.json").read_text(encoding="utf-8")
        )
        assert stored == receipt
        assert receipt["required_predeployment_read"] == "docs/agent-deployment-map.md"
        assert receipt["full_repository_copy"] is False
        assert receipt["file_count"] == len(receipt["files"])
        for relative in receipt["files"]:
            assert (output / relative).is_file(), relative
        consumer = output / "skills" / "embedded-harness" / "harness_action_consumer.py"
        completed = subprocess.run(
            [sys.executable, "-B", str(consumer), "--help"],
            cwd=output,
            text=True,
            encoding="utf-8",
            errors="strict",
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        if profile_name == "codex-local-source-recall":
            for name in ("context_recall.py", "text_source_reader.py"):
                completed = subprocess.run(
                    [sys.executable, "-B", str(output / "skills" / "embedded-harness" / name), "--help"],
                    cwd=output, capture_output=True, text=True, encoding="utf-8", check=False,
                )
                assert completed.returncode == 0, completed.stderr
    finally:
        shutil.rmtree(output, ignore_errors=True)
        try:
            temp_root.rmdir()
        except OSError:
            pass
