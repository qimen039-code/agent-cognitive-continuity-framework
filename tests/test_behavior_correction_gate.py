from __future__ import annotations

import unittest

import behavior_correction_gate as correction_gate
from behavior_correction_gate import (
    SCHEMA,
    _profile_inventory,
    build_behavior_correction_receipt,
)


class BehaviorCorrectionGateTests(unittest.TestCase):
    def classify_effectiveness(self, **kwargs):
        self.assertTrue(
            hasattr(correction_gate, "classify_correction_effectiveness"),
            "correction effectiveness classifier is not implemented",
        )
        return correction_gate.classify_correction_effectiveness(**kwargs)

    def test_profile_inventory_exposes_all_hash_bound_profiles(self) -> None:
        receipt = _profile_inventory()
        self.assertEqual(SCHEMA, receipt["schema"])
        self.assertEqual("pass", receipt["status"])
        self.assertEqual(7, receipt["profile_count"])
        self.assertTrue(all(item["profile_sha256"] for item in receipt["profiles"]))

    def test_powershell_bash_heredoc_requires_preflight_validation(self) -> None:
        receipt = build_behavior_correction_receipt(
            stage="pretool",
            environment="powershell",
            tool_role="unknown",
            tool_surface="shell_command",
            text="python - <<'PY'\nprint('x')\nPY",
        )
        self.assertEqual("correction_candidate", receipt["status"])
        self.assertEqual("validation_required", receipt["decision"])
        self.assertEqual(
            "powershell_bash_heredoc_translation",
            receipt["matches"][0]["profile_id"],
        )
        self.assertEqual("preflight_validate", receipt["matches"][0]["decision_mode"])

    def test_mixed_bash_heredocs_use_most_conservative_matching_mode(self) -> None:
        receipt = build_behavior_correction_receipt(
            stage="pretool",
            environment="powershell",
            tool_role="shell",
            tool_surface="exec_command",
            text=(
                "python - <<'PY'\n"
                "print('quoted')\n"
                "PY\n"
                "python - <<PY\n"
                "print('unquoted')\n"
                "PY"
            ),
        )
        self.assertEqual(2, receipt["match_count"])
        self.assertEqual(
            {"preflight_validate", "predictive_review"},
            {item["decision_mode"] for item in receipt["matches"]},
        )
        self.assertEqual("semantic_review_required", receipt["decision"])

    def test_accepted_foreach_profile_returns_rewrite_candidate(self) -> None:
        receipt = build_behavior_correction_receipt(
            stage="pretool",
            environment="powershell",
            tool_role="read_only",
            tool_surface="shell_command",
            text=(
                "$candidates=@('C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'); "
                "foreach($p in $candidates){[pscustomobject]@{Path=$p;Exists=(Test-Path -LiteralPath $p)}} "
                "| Format-Table -AutoSize"
            ),
        )
        self.assertEqual("correction_candidate", receipt["status"])
        self.assertEqual("rewrite_candidate", receipt["decision"])
        self.assertEqual(
            "powershell_statement_loop_pipeline_pretool_migration",
            receipt["matches"][0]["profile_id"],
        )
        self.assertEqual("auto_rewrite", receipt["matches"][0]["decision_mode"])
        self.assertEqual(receipt["text_sha256"], receipt["candidate_key"])

    def test_unrecoverable_unicode_payload_requests_semantic_review(self) -> None:
        receipt = build_behavior_correction_receipt(
            stage="pretool",
            environment="any",
            tool_role="mutating",
            tool_surface="filesystem_content_mutation",
            text="*** Update File: report.txt\n+损坏文本�",
            target_binding_sha256="f" * 64,
        )
        self.assertEqual("correction_candidate", receipt["status"])
        self.assertEqual("semantic_review_required", receipt["decision"])
        self.assertEqual(
            "predictive_review",
            receipt["matches"][0]["decision_mode"],
        )

    def test_ordinary_command_is_not_changed(self) -> None:
        receipt = build_behavior_correction_receipt(
            stage="pretool",
            environment="powershell",
            tool_role="readonly",
            tool_surface="shell_command",
            text="Get-ChildItem -LiteralPath C:\\work",
        )
        self.assertEqual("pass", receipt["status"])
        self.assertEqual("no_match", receipt["decision"])
        self.assertEqual([], receipt["matches"])

    def test_gate_cannot_freeze_write_memory_mutate_policy_or_block_host(self) -> None:
        receipt = build_behavior_correction_receipt(
            stage="pretool",
            environment="powershell",
            tool_role="unknown",
            tool_surface="shell_command",
            text="python - <<'PY'\nprint('x')\nPY",
        )
        self.assertFalse(receipt["automatic_freeze"])
        self.assertFalse(receipt["automatic_long_term_memory_write"])
        self.assertFalse(receipt["automatic_policy_mutation"])
        self.assertFalse(receipt["host_blocking"])

    def test_ordinary_no_match_is_not_applicable_instead_of_a_coverage_gap(self) -> None:
        receipt = self.classify_effectiveness(
            correction_receipt={"status": "pass", "decision": "no_match", "matches": []},
            host_observation={"known_failure_observed": False},
        )
        self.assertEqual("not_applicable", receipt["applicability"])
        self.assertEqual("none", receipt["gap_class"])
        self.assertEqual("not_applicable", receipt["correction_effect"])

    def test_known_failure_without_a_profile_is_a_coverage_gap(self) -> None:
        source_signature = "a" * 64
        receipt = self.classify_effectiveness(
            correction_receipt={
                "status": "pass",
                "decision": "no_match",
                "matches": [],
                "text_sha256": source_signature,
            },
            host_observation={
                "known_failure_observed": True,
                "source_signature": source_signature,
            },
        )
        self.assertEqual("known_failure_uncovered", receipt["applicability"])
        self.assertEqual("coverage_gap", receipt["gap_class"])
        self.assertEqual("not_applied", receipt["correction_effect"])

    def test_effectiveness_is_activation_gap_when_profile_never_reaches_hook(self) -> None:
        receipt = self.classify_effectiveness(
            correction_receipt={
                "status": "correction_candidate",
                "decision": "rewrite_candidate",
                "matches": [{"profile_id": "accepted-profile"}],
            },
            host_observation={"hook_fired": False, "rewrite_applied": False},
        )
        self.assertEqual("activation_gap", receipt["gap_class"])

    def test_effectiveness_requires_a_bound_passed_verifier_after_rewrite(self) -> None:
        base = {
            "status": "correction_candidate",
            "decision": "rewrite_candidate",
            "text_sha256": "a" * 64,
            "matches": [
                {
                    "profile_id": "accepted-profile",
                    "profile_version": 1,
                    "profile_sha256": "b" * 64,
                }
            ],
        }
        pending = self.classify_effectiveness(
            correction_receipt=base,
            host_observation={
                "hook_fired": True,
                "rewrite_applied": True,
                "validator_consumed": True,
                "tool_use_id": "tool-1",
                "source_signature": "a" * 64,
                "dispatch_signature": "c" * 64,
                "result_hash": "d" * 64,
            },
        )
        effective = self.classify_effectiveness(
            correction_receipt=base,
            host_observation={
                "hook_fired": True,
                "rewrite_applied": True,
                "tool_use_id": "tool-1",
                "source_signature": "a" * 64,
                "dispatch_signature": "c" * 64,
                "result_hash": "d" * 64,
                "verifier_result": {
                    "verifier_id": "powershell-parser",
                    "verifier_version": "1",
                    "status": "passed",
                    "evidence_sha256": "e" * 64,
                },
            },
        )
        self.assertEqual("consumer_verification_gap", pending["gap_class"])
        self.assertEqual("applied_unverified", pending["correction_effect"])
        self.assertEqual("applied_verified", effective["correction_effect"])
        self.assertEqual("no_gap", effective["gap_class"])
        self.assertEqual("unbound", effective["task_effect"])


if __name__ == "__main__":
    unittest.main()
