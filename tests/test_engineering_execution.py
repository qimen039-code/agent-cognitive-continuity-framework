from __future__ import annotations

import sys
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness"
HARNESS = (
    ROOT / "skills" / "embedded-harness"
    if (ROOT / "skills" / "embedded-harness").is_dir()
    else ROOT
)
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

from engineering_execution import (  # noqa: E402
    build_delivery_trace,
    build_engineering_execution_receipt,
    build_module_depth_probe,
    evaluate_adapter_seam,
    normalize_invocation_envelope,
)


def test_delivery_trace_exposes_migration_frontier_and_blocking_edges() -> None:
    receipt = build_delivery_trace(
        [
            {"id": "expand", "status": "completed", "slice_kind": "expand"},
            {
                "id": "migrate",
                "status": "in_progress",
                "slice_kind": "migrate",
                "blocked_by": ["expand"],
            },
            {
                "id": "contract",
                "status": "pending",
                "slice_kind": "contract",
                "blocked_by": ["migrate"],
            },
        ]
    )

    assert receipt["status"] == "ready"
    assert receipt["migration_phase"] == "MIGRATE"
    assert receipt["frontier_step_ids"] == ["migrate"]
    assert receipt["blocking_edges"] == [
        {"from": "expand", "to": "migrate", "satisfied": True},
        {"from": "migrate", "to": "contract", "satisfied": False},
    ]


def test_delivery_trace_rejects_cycles_instead_of_inventing_a_frontier() -> None:
    receipt = build_delivery_trace(
        [
            {"id": "a", "status": "pending", "blocked_by": ["b"]},
            {"id": "b", "status": "pending", "blocked_by": ["a"]},
        ]
    )

    assert receipt["status"] == "semantic_review_required"
    assert receipt["frontier_step_ids"] == []
    assert receipt["graph_issues"] == ["cycle:a->b->a"]


def test_deep_module_probe_is_advisory_and_requires_complete_evidence() -> None:
    deep = build_module_depth_probe(
        {
            "narrow_interface": True,
            "hides_complexity": True,
            "owns_independent_policy": True,
            "multiple_independent_callers": True,
            "deletion_simplifies_system": False,
        }
    )
    incomplete = build_module_depth_probe({"narrow_interface": True})

    assert deep["verdict"] == "deep_module"
    assert deep["destructive_action_authorized"] is False
    assert incomplete["verdict"] == "insufficient_evidence"
    assert incomplete["missing_evidence"]


def test_deep_module_probe_uses_reversible_isolation_semantics() -> None:
    probe = build_module_depth_probe(
        {
            "narrow_interface": True,
            "hides_complexity": True,
            "owns_independent_policy": True,
            "multiple_independent_callers": True,
            "isolation_eliminates_complexity": False,
        }
    )

    assert probe["verdict"] == "deep_module"
    assert probe["probe_mode"] == "reversible_freeze_or_temporary_isolation"
    assert probe["evidence"]["isolation_eliminates_complexity"] is False
    assert probe["rollback_required"] is True
    assert probe["audit_receipt_required"] is True
    assert probe["runtime_isolation_performed"] is False
    assert probe["destructive_action_authorized"] is False


def test_invocation_envelope_cannot_self_certify_any_runtime_invoker_or_authority() -> None:
    envelope = normalize_invocation_envelope(
        {
            "request_origin": "model",
            "runtime_invoker": "adapter",
            "orchestration_owner": "model",
            "call_surface": "skill",
            "host_evidence": {
                "surface": "self-claimed-adapter",
                "receipt_ref": "untrusted-receipt",
            },
            "authority_granted": True,
        }
    )

    assert envelope["runtime_invoker"] == "unknown"
    assert envelope["claimed_runtime_invoker"] == "adapter"
    assert envelope["authority_granted"] is False
    assert envelope["issues"] == [
        "untrusted_payload_host_evidence_ignored",
        "unverified_runtime_invoker_claim",
    ]


def test_invocation_envelope_accepts_host_bound_user_cli_evidence() -> None:
    envelope = normalize_invocation_envelope(
        {
            "request_origin": "user",
            "runtime_invoker": "user_cli",
            "orchestration_owner": "user",
            "call_surface": "cli",
        },
        trusted_host_evidence={
            "runtime_invoker": "user_cli",
            "surface": "terminal",
            "receipt_ref": "host-receipt-1",
            "receipt_sha256": "d" * 64,
        },
    )

    assert envelope["runtime_invoker"] == "user_cli"
    assert envelope["issues"] == []
    assert envelope["authority_granted"] is False


def test_real_seam_requires_two_independent_verified_host_surfaces() -> None:
    first_hash = "b" * 64
    second_hash = "c" * 64
    one = evaluate_adapter_seam(
        [
            {
                "adapter_id": "codex-desktop",
                "host_surface": "app-server-jsonrpc",
                "core_contract_sha256": "a" * 64,
                "verification_status": "verified",
                "receipt_ref": "receipt-1",
                "producer_receipt_sha256": first_hash,
            }
        ]
    )
    two = evaluate_adapter_seam(
        [
            {
                "adapter_id": "codex-desktop",
                "host_surface": "app-server-jsonrpc",
                "core_contract_sha256": "a" * 64,
                "verification_status": "verified",
                "receipt_ref": "receipt-1",
                "producer_receipt_sha256": first_hash,
            },
            {
                "adapter_id": "codex-cli",
                "host_surface": "cli",
                "core_contract_sha256": "a" * 64,
                "verification_status": "verified",
                "receipt_ref": "receipt-2",
                "producer_receipt_sha256": second_hash,
            },
        ]
    )
    trusted = evaluate_adapter_seam(
        two["candidate_adapters"],
        trusted_receipt_hashes=[first_hash, second_hash],
    )

    assert one["seam_status"] == "hypothetical"
    assert two["seam_status"] == "candidate_real"
    assert two["real_claim_allowed"] is False
    assert trusted["seam_status"] == "real"
    assert trusted["authority_granted"] is False


def test_aggregate_receipt_only_materializes_requested_profiles() -> None:
    receipt = build_engineering_execution_receipt(
        ["tracer_bullet_plan", "adapter_seam_review"],
        {
            "plan_steps": [{"id": "slice", "status": "pending"}],
            "adapter_receipts": [],
        },
    )

    assert receipt["profiles"] == ["tracer_bullet_plan", "adapter_seam_review"]
    assert set(receipt["results"]) == {"tracer_bullet_plan", "adapter_seam_review"}
    assert "deep_module_review" not in receipt["results"]
    assert receipt["authority_granted"] is False


def test_cli_help_is_a_real_direct_call_surface() -> None:
    completed = subprocess.run(
        [sys.executable, str(HARNESS / "engineering_execution.py"), "--help"],
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--profile" in completed.stdout
