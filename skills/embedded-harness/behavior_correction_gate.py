from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from execution_feedback import (
    CorrectionProfileRegistryError,
    derive_subject_binding,
    detect_correction_profiles,
    load_correction_profiles,
)


SCHEMA = "cbh.behavior_correction_gate_receipt.v1"
EFFECTIVENESS_SCHEMA = "cbh.correction_effectiveness_receipt.v2"

_DECISION_BY_MODE = {
    "auto_rewrite": "rewrite_candidate",
    "preflight_validate": "validation_required",
    "predictive_review": "semantic_review_required",
}

_DECISION_RANK = {
    "rewrite_candidate": 0,
    "validation_required": 1,
    "semantic_review_required": 2,
}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _profile_receipt(
    profile: dict[str, Any],
    *,
    text: str,
    target_binding_sha256: str,
    execution_cwd: str,
) -> dict[str, Any]:
    subject_binding_sha256 = derive_subject_binding(
        profile,
        text=text,
        target_binding_sha256=target_binding_sha256,
        execution_cwd=execution_cwd,
    )
    return {
        "profile_id": str(profile["profile_id"]),
        "profile_version": int(profile.get("profile_version") or 1),
        "profile_sha256": str(profile.get("profile_sha256") or ""),
        "priority": int(profile.get("priority") or 0),
        "enforcement": str(profile.get("enforcement") or "advisory_predictive_review"),
        "correction_family_id": str(profile.get("correction_family_id") or ""),
        "decision_mode": str(profile.get("decision_mode") or "predictive_review"),
        "promotion_status": str(profile.get("promotion_status") or "semantic_review_only"),
        "source_record_ids": list(profile.get("source_record_ids") or []),
        "historical_replay_refs": list(profile.get("historical_replay_refs") or []),
        "postcondition": str(profile.get("postcondition") or ""),
        "behavior_class": str(profile.get("behavior_class") or ""),
        "rewrite_boundary": str(profile.get("rewrite_boundary") or ""),
        "rewrite_instruction": str(profile.get("rewrite_instruction") or ""),
        "safe_template": str(profile.get("safe_template") or ""),
        "verifier": str(profile.get("verifier") or ""),
        "verifier_channel": str(profile.get("verifier_channel") or ""),
        "verifier_instruction": str(profile.get("verifier_instruction") or ""),
        "evidence_boundary": str(profile.get("evidence_boundary") or ""),
        "source_kind": str(profile.get("source_kind") or ""),
        "source_ref": str(profile.get("source_ref") or ""),
        "target_binding_sha256": target_binding_sha256 or None,
        "subject_binding_sha256": subject_binding_sha256,
    }


def _most_conservative_decision(matches: Iterable[dict[str, Any]]) -> str:
    decisions = [
        _DECISION_BY_MODE.get(
            str(match.get("decision_mode") or ""),
            "semantic_review_required",
        )
        for match in matches
    ]
    return max(
        decisions,
        key=lambda decision: _DECISION_RANK[decision],
        default="no_match",
    )


def build_behavior_correction_receipt(
    *,
    stage: str,
    environment: str,
    tool_role: str,
    tool_surface: str,
    text: str,
    parser_error_ids: Iterable[str] = (),
    execution_cwd: str = "",
    target_binding_sha256: str = "",
) -> dict[str, Any]:
    profiles = load_correction_profiles()
    matches = detect_correction_profiles(
        stage=stage,
        environment=environment,
        tool_role=tool_role,
        tool_surface=tool_surface,
        text=text,
        parser_error_ids=parser_error_ids,
        profiles=profiles,
    )
    match_receipts = [
        _profile_receipt(
            profile,
            text=text,
            target_binding_sha256=target_binding_sha256,
            execution_cwd=execution_cwd,
        )
        for profile in matches
    ]
    decision = _most_conservative_decision(match_receipts)
    candidate_key = _sha256_text(text)
    return {
        "schema": SCHEMA,
        "status": "correction_candidate" if match_receipts else "pass",
        "decision": decision,
        "stage": stage,
        "environment": environment,
        "tool_role": tool_role,
        "tool_surface": tool_surface,
        "text_sha256": candidate_key,
        "candidate_key": candidate_key,
        "parser_error_ids": [str(item) for item in parser_error_ids if str(item)],
        "match_count": len(match_receipts),
        "matches": match_receipts,
        "scope": "current_event_only",
        "automatic_freeze": False,
        "automatic_long_term_memory_write": False,
        "automatic_policy_mutation": False,
        "host_blocking": False,
        "rule": "Bind one unchanged current candidate to the most conservative applicable correction mode and its declared verifier; priority orders matching profiles, and no match is a no-op.",
    }


def classify_correction_effectiveness(
    *,
    correction_receipt: dict[str, Any],
    host_observation: dict[str, Any],
) -> dict[str, Any]:
    """Classify why a correction did or did not affect one observed dispatch."""

    matches = correction_receipt.get("matches")
    match_count = len(matches) if isinstance(matches, list) else 0
    decision = str(correction_receipt.get("decision") or "no_match")
    surface_status = str(host_observation.get("surface_status") or "observable")
    hook_fired = host_observation.get("hook_fired") is True
    rewrite_applied = host_observation.get("rewrite_applied") is True
    known_failure_observed = host_observation.get("known_failure_observed") is True
    source_signature = str(host_observation.get("source_signature") or "")
    receipt_source_signature = str(correction_receipt.get("text_sha256") or "")
    dispatch_signature = str(host_observation.get("dispatch_signature") or "")
    result_hash = str(host_observation.get("result_hash") or "")
    tool_use_id = str(host_observation.get("tool_use_id") or "")
    verifier_raw = host_observation.get("verifier_result")
    verifier = dict(verifier_raw) if isinstance(verifier_raw, dict) else {}
    verifier_status = str(verifier.get("status") or "not_run")
    verifier_evidence = str(verifier.get("evidence_sha256") or "")
    hash_pattern = re.compile(r"[0-9a-f]{64}")
    source_bound = bool(
        hash_pattern.fullmatch(source_signature)
        and source_signature == receipt_source_signature
    )
    profile_hashes_valid = bool(
        match_count
        and all(
            isinstance(match, dict)
            and hash_pattern.fullmatch(str(match.get("profile_sha256") or ""))
            for match in matches
        )
    )
    observation_identity_complete = bool(
        tool_use_id
        and source_bound
        and hash_pattern.fullmatch(dispatch_signature)
        and hash_pattern.fullmatch(result_hash)
    )
    verifier_passed = bool(
        verifier_status == "passed"
        and hash_pattern.fullmatch(verifier_evidence)
        and verifier.get("verifier_id")
        and verifier.get("verifier_version")
    )
    task_effect = str(host_observation.get("task_effect") or "unbound")
    if task_effect not in {"satisfied", "failed", "inconclusive", "unbound", "stale_or_orphan"}:
        task_effect = "unbound"

    if surface_status in {"unsupported", "not_exposed", "unobservable"}:
        applicability = "unsupported_surface"
        gap_class = "interface_gap"
        correction_effect = "unsupported"
    elif match_count == 0 or decision == "no_match":
        if known_failure_observed and source_bound:
            applicability = "known_failure_uncovered"
            gap_class = "coverage_gap"
            correction_effect = "not_applied"
        elif known_failure_observed:
            applicability = "ambiguous_candidate"
            gap_class = "consumer_verification_gap"
            correction_effect = "stale_or_orphan"
        else:
            applicability = "not_applicable"
            gap_class = "none"
            correction_effect = "not_applicable"
    elif not hook_fired:
        applicability = "exact_profile_match"
        gap_class = "activation_gap"
        correction_effect = "not_applied"
    elif decision == "rewrite_candidate" and not rewrite_applied:
        applicability = "exact_profile_match"
        gap_class = "action_application_gap"
        correction_effect = "not_applied"
    elif not observation_identity_complete or not profile_hashes_valid:
        applicability = "exact_profile_match"
        gap_class = "consumer_verification_gap"
        correction_effect = (
            "stale_or_orphan" if source_signature and not source_bound else "applied_unverified"
        )
    elif verifier_status == "failed":
        applicability = "exact_profile_match"
        gap_class = "no_gap"
        correction_effect = "applied_failed"
    elif not verifier_passed:
        applicability = "exact_profile_match"
        gap_class = "consumer_verification_gap"
        correction_effect = "applied_unverified"
    else:
        applicability = "exact_profile_match"
        gap_class = "no_gap"
        correction_effect = "applied_verified"

    effectiveness = (
        "effective"
        if correction_effect == "applied_verified"
        else "not_applicable"
        if correction_effect == "not_applicable"
        else "not_effective"
    )

    return {
        "schema": EFFECTIVENESS_SCHEMA,
        "status": "observed",
        "effectiveness": effectiveness,
        "applicability": applicability,
        "gap_class": gap_class,
        "pipeline_gap": gap_class,
        "correction_effect": correction_effect,
        "task_effect": task_effect,
        "decision": decision,
        "profile_match_count": match_count,
        "surface_status": surface_status,
        "hook_fired": hook_fired,
        "rewrite_applied": rewrite_applied,
        "known_failure_observed": known_failure_observed,
        "observation_identity_complete": observation_identity_complete,
        "source_signature_bound": source_bound,
        "verifier_result": {
            "verifier_id": verifier.get("verifier_id"),
            "verifier_version": verifier.get("verifier_version"),
            "status": verifier_status,
            "evidence_sha256": verifier.get("evidence_sha256"),
        },
        "scope": "current_observed_dispatch_only",
        "host_blocking": False,
    }


def _profile_inventory() -> dict[str, Any]:
    profiles = load_correction_profiles()
    return {
        "schema": SCHEMA,
        "status": "pass",
        "decision": "profile_inventory",
        "profile_count": len(profiles),
        "profiles": [
            {
                "profile_id": str(profile["profile_id"]),
                "profile_version": int(profile.get("profile_version") or 1),
                "profile_sha256": str(profile.get("profile_sha256") or ""),
                "priority": int(profile.get("priority") or 0),
                "trigger_stage": str(profile.get("trigger_stage") or ""),
                "environment": str(profile.get("environment") or ""),
                "enforcement": str(profile.get("enforcement") or ""),
                "correction_family_id": str(profile.get("correction_family_id") or ""),
                "decision_mode": str(profile.get("decision_mode") or ""),
                "promotion_status": str(profile.get("promotion_status") or ""),
            }
            for profile in profiles.values()
        ],
        "scope": "inventory_only",
        "host_blocking": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate hash-bound CBH behavior-correction profiles without host blocking."
    )
    parser.add_argument("--stage", default="pretool")
    parser.add_argument("--environment", default="any")
    parser.add_argument("--tool-role", default="unknown")
    parser.add_argument("--tool-surface", default="")
    parser.add_argument("--text", default="")
    parser.add_argument("--text-from-stdin", action="store_true")
    parser.add_argument("--parser-error-id", action="append", default=[])
    parser.add_argument("--execution-cwd", default="")
    parser.add_argument("--target-binding-sha256", default="")
    parser.add_argument("--list-profiles", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    if args.text_from_stdin and args.text:
        parser.error("--text and --text-from-stdin are mutually exclusive")
    text = sys.stdin.read() if args.text_from_stdin else args.text

    try:
        receipt = (
            _profile_inventory()
            if args.list_profiles
            else build_behavior_correction_receipt(
                stage=args.stage,
                environment=args.environment,
                tool_role=args.tool_role,
                tool_surface=args.tool_surface,
                text=text,
                parser_error_ids=args.parser_error_id,
                execution_cwd=args.execution_cwd,
                target_binding_sha256=args.target_binding_sha256,
            )
        )
    except CorrectionProfileRegistryError as exc:
        receipt = {
            "schema": SCHEMA,
            "status": "unavailable",
            "decision": "registry_or_policy_contract_failed",
            "issues": [str(exc)],
            "host_blocking": False,
        }

    encoded = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if receipt["status"] != "unavailable" else 1


if __name__ == "__main__":
    raise SystemExit(main())
