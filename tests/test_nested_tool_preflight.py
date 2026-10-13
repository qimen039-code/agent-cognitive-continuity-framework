from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from nested_tool_preflight import (
    normalize_nested_tool_result,
    preflight_nested_tool_call,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "nested_tool_failures.json"


class NestedToolPreflightTests(unittest.TestCase):
    def test_current_exec_command_validates_syntax_outside_registered_rewrites(self) -> None:
        arguments = {"cmd": "Write-Output (", "shell": "pwsh"}
        receipt = preflight_nested_tool_call({"tool_name": "exec_command", "arguments": arguments})
        self.assertEqual("validation_required", receipt["decision"])
        self.assertIn("powershell_parser_rejection", receipt["reasons"])
        self.assertEqual(arguments, receipt["normalized_arguments"])
        self.assertFalse(receipt["rewrite_applied"])

    def test_unavailable_parser_does_not_produce_an_allow_receipt(self) -> None:
        with patch("nested_tool_preflight.powershell_parser_error_ids", return_value=None):
            receipt = preflight_nested_tool_call({"tool_name": "exec_command", "arguments": {"cmd": "Get-Location"}})
        self.assertEqual("validation_required", receipt["decision"])
        self.assertIn("powershell_parser_unavailable", receipt["reasons"])

    def test_current_exec_command_roundtrips_actual_host_arguments(self) -> None:
        arguments = {
            "cmd": "Get-Location", "workdir": r"C:\work",
            "shell": "pwsh.exe", "login": False, "tty": False,
            "max_output_tokens": 1800, "yield_time_ms": 10000,
        }
        receipt = preflight_nested_tool_call({"tool_name": "exec_command", "arguments": arguments})
        self.assertEqual("allow", receipt["decision"])
        self.assertEqual(arguments, receipt["normalized_arguments"])
        self.assertEqual("exec_command", receipt["tool_name"])

    def test_current_exec_command_rewrite_preserves_transport_and_retry_identity(self) -> None:
        arguments = {
            "cmd": "foreach ($n in 1..3) { $n } | Sort-Object",
            "shell": "pwsh", "max_output_tokens": 900, "yield_time_ms": 10000,
        }
        candidate = {"tool_name": "exec_command", "arguments": arguments}
        receipt = preflight_nested_tool_call(candidate)
        self.assertEqual("rewrite_candidate", receipt["decision"])
        expected = {**arguments, "cmd": "$(foreach ($n in 1..3) { $n }) | Sort-Object"}
        self.assertEqual(expected, receipt["normalized_arguments"])
        self.assertNotIn("command", receipt["normalized_arguments"])
        retried = preflight_nested_tool_call({**candidate, "previous_failure_signature": receipt["dispatch_signature"]})
        self.assertTrue(retried["same_candidate"])
        self.assertIn("unchanged_dispatch_after_failure", retried["reasons"])

    def test_current_exec_command_validates_native_fields(self) -> None:
        for arguments in ({"cmd": 3}, {"cmd": "Get-Location", "tty": "false"},
                          {"cmd": "Get-Location", "yield_time_ms": True},
                          {"cmd": "Get-Location", "max_output_tokens": "100"},
                          {"cmd": "Get-Location", "command": "Get-Date"}):
            with self.subTest(arguments=arguments):
                receipt = preflight_nested_tool_call({"tool_name": "exec_command", "arguments": arguments})
                self.assertEqual("semantic_review_required", receipt["decision"])
                self.assertEqual(arguments, receipt["normalized_arguments"])

    def test_other_shell_is_never_rewritten_as_powershell(self) -> None:
        arguments = {"cmd": "foreach ($n in 1..3) { $n } | Sort-Object", "shell": "/bin/bash"}
        receipt = preflight_nested_tool_call({"tool_name": "exec_command", "arguments": arguments})
        self.assertFalse(receipt["rewrite_applied"])
        self.assertEqual(arguments, receipt["normalized_arguments"])
        self.assertIn("shell_parser_not_covered", receipt["reasons"])

    def test_inline_multi_runtime_commands_require_a_file_backed_carrier(self) -> None:
        for command in (
            "node --input-type=module -e \"console.log('ok')\"",
            "python -c \"print('ok')\"",
            "pwsh -NoProfile -Command \"Get-Date\"",
            "$script = @'\nconsole.log('ok')\n'@; node -e $script",
        ):
            with self.subTest(command=command):
                receipt = preflight_nested_tool_call({
                    "tool_name": "shell_command",
                    "arguments": {"command": command, "workdir": r"C:\work"},
                })
                self.assertIn("file_backed_carrier_required", receipt["reasons"])
                self.assertNotEqual(receipt["decision"], "allow")

    def test_verified_file_backed_carrier_is_allowed_and_stable(self) -> None:
        candidate = {
            "tool_name": "shell_command",
            "arguments": {
                "command": r"pwsh -NoProfile -File C:\work\probe.ps1",
                "workdir": r"C:\work",
                "timeout_ms": 10000,
            },
        }
        first = preflight_nested_tool_call(candidate)
        self.assertEqual(first["decision"], "allow")
        self.assertEqual(first["carrier"], "file_backed_script")
        repeated = preflight_nested_tool_call({
            **candidate,
            "previous_failure_signature": first["dispatch_signature"],
        })
        self.assertTrue(repeated["same_candidate"])
        self.assertIn("unchanged_dispatch_after_failure", repeated["reasons"])

    @classmethod
    def setUpClass(cls) -> None:
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.cases = {case["id"]: case for case in payload["cases"]}

    def receipt_for(self, case_id: str) -> dict:
        return preflight_nested_tool_call(self.cases[case_id]["candidate"])

    def test_every_historical_fixture_replays_its_expected_reasons(self) -> None:
        for case_id, case in self.cases.items():
            with self.subTest(case_id=case_id):
                receipt = self.receipt_for(case_id)
                for reason in case["expected_reasons"]:
                    self.assertIn(reason, receipt["reasons"])

    def test_direct_github_api_web_open_requires_validation(self) -> None:
        receipt = self.receipt_for("root_web_open_api_github")
        self.assertEqual("validation_required", receipt["decision"])
        self.assertIn(
            "web_open_api_github_known_unsafe_nonretryable", receipt["reasons"]
        )
        self.assertIn("broad_output_scope_review_required", receipt["reasons"])
        self.assertRegex(receipt["signature"], r"^[0-9a-f]{64}$")

    def test_unrelated_open_ref_is_allowed(self) -> None:
        receipt = preflight_nested_tool_call(
            {
                "tool_name": "web__run",
                "arguments": {"open": [{"ref_id": "turn1search0"}]},
                "agent_path": "/root/child",
            }
        )
        self.assertEqual("allow", receipt["decision"])
        self.assertEqual([], receipt["reasons"])

    def test_unchanged_failed_candidate_requires_a_new_validation(self) -> None:
        candidate = self.cases["root_web_open_api_github"]["candidate"]
        first = preflight_nested_tool_call(candidate)
        second = preflight_nested_tool_call(
            {**candidate, "previous_failure_signature": first["signature"]}
        )
        self.assertEqual("validation_required", second["decision"])
        self.assertIn("unchanged_dispatch_after_failure", second["reasons"])
        self.assertTrue(second["same_candidate"])

    def test_rewritten_dispatch_signature_prevents_an_unchanged_retry(self) -> None:
        candidate = {
            "tool_name": "shell_command",
            "arguments": {
                "command": "foreach ($n in 1..3) { $n } | Sort-Object"
            },
        }
        first = preflight_nested_tool_call(candidate)
        second = preflight_nested_tool_call(
            {
                **candidate,
                "previous_failure_signature": first["dispatch_signature"],
            }
        )
        self.assertTrue(second["same_candidate"])
        self.assertEqual("dispatch", second["matched_signature_kind"])
        self.assertIn("unchanged_dispatch_after_failure", second["reasons"])
        self.assertEqual("validation_required", second["decision"])

        source_only = preflight_nested_tool_call(
            {
                **candidate,
                "previous_failure_signature": first["source_signature"],
            }
        )
        self.assertFalse(source_only["same_candidate"])
        self.assertTrue(source_only["same_source_candidate"])
        self.assertEqual("source", source_only["matched_signature_kind"])
        self.assertNotIn("unchanged_dispatch_after_failure", source_only["reasons"])
        self.assertEqual(source_only["signature"], source_only["dispatch_signature"])
        self.assertEqual("rewrite_candidate", source_only["decision"])

    def test_schema_errors_are_not_dispatched(self) -> None:
        cases = [
            {"tool_name": "shell_command", "arguments": {"command": 3}},
            {
                "tool_name": "shell_command",
                "arguments": {"command": "Get-Date", "timeout_ms": 0},
            },
            {
                "tool_name": "web__run",
                "arguments": {"open": [{"ref_id": 12}]},
            },
            {
                "tool_name": "web__run",
                "arguments": {"response_length": "huge"},
            },
        ]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                receipt = preflight_nested_tool_call(candidate)
                self.assertEqual("semantic_review_required", receipt["decision"])
                self.assertTrue(receipt["reasons"])

    def test_nested_tool_schemas_validate_required_item_fields_and_constraints(self) -> None:
        cases = [
            {
                "tool_name": "shell_command",
                "arguments": {"command": "Get-Date", "justification": 3},
            },
            {"tool_name": "web__run", "arguments": {"search_query": [{}]}},
            {
                "tool_name": "web__run",
                "arguments": {
                    "search_query": [{"q": str(i)} for i in range(4)]
                },
            },
            {
                "tool_name": "web__run",
                "arguments": {
                    "screenshot": [{"ref_id": "turn1view0", "pageno": "zero"}]
                },
            },
        ]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                receipt = preflight_nested_tool_call(candidate)
                self.assertEqual("semantic_review_required", receipt["decision"])
                self.assertTrue(receipt["reasons"])

        direct_only = preflight_nested_tool_call(
            {"tool_name": "wait_agent", "arguments": {"timeout_ms": 10000}}
        )
        self.assertEqual("semantic_review_required", direct_only["decision"])
        self.assertIn("unknown_nested_tool_schema", direct_only["reasons"])

    def test_enum_schema_fields_fail_closed_without_type_errors(self) -> None:
        for value in ([], {}, 3):
            with self.subTest(field="sandbox_permissions", value=value):
                receipt = preflight_nested_tool_call(
                    {
                        "tool_name": "shell_command",
                        "arguments": {
                            "command": "Get-Date",
                            "sandbox_permissions": value,
                        },
                    }
                )
                self.assertEqual("semantic_review_required", receipt["decision"])
                self.assertIn(
                    "shell_command_invalid_sandbox_permissions", receipt["reasons"]
                )
            with self.subTest(field="response_length", value=value):
                receipt = preflight_nested_tool_call(
                    {
                        "tool_name": "web__run",
                        "arguments": {
                            "search_query": [{"q": str(index)} for index in range(4)],
                            "response_length": value,
                        },
                    }
                )
                self.assertEqual("semantic_review_required", receipt["decision"])
                self.assertIn("web_run_invalid_response_length", receipt["reasons"])

        crypto = preflight_nested_tool_call(
            {
                "tool_name": "web__run",
                "arguments": {
                    "finance": [{"ticker": "BTC", "type": "crypto", "market": ""}]
                },
            }
        )
        self.assertEqual("allow", crypto["decision"])

    def test_each_supported_web_operation_accepts_a_minimal_valid_item(self) -> None:
        cases = [
            {"search_query": [{"q": "codex"}]},
            {"image_query": [{"q": "browser"}]},
            {"open": [{"ref_id": "turn1search0"}]},
            {"click": [{"ref_id": "turn1view0", "id": 3}]},
            {"find": [{"ref_id": "turn1view0", "pattern": "text"}]},
            {"screenshot": [{"ref_id": "turn1view0", "pageno": 0}]},
            {"finance": [{"ticker": "AMD", "type": "equity"}]},
            {"weather": [{"location": "Shanghai"}]},
            {"sports": [{"fn": "standings", "league": "nba"}]},
            {"time": [{"utc_offset": "+08:00"}]},
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                receipt = preflight_nested_tool_call(
                    {"tool_name": "web__run", "arguments": arguments}
                )
                self.assertEqual("allow", receipt["decision"])

    def test_unknown_tool_is_semantic_review_not_guessed(self) -> None:
        receipt = preflight_nested_tool_call(
            {"tool_name": "made_up", "arguments": {"command": "Get-Date"}}
        )
        self.assertEqual("semantic_review_required", receipt["decision"])
        self.assertIn("unknown_nested_tool_schema", receipt["reasons"])

    def test_historical_runtime_and_cli_shapes_require_validation(self) -> None:
        for case_id in (
            "child_irm_object_array_datetime",
            "child_irm_parenthesized_repeat",
            "child_graphql_hashtable_property",
            "child_git_tree_variable_boundary",
            "child_gh_slurp_jq_incompatible",
        ):
            with self.subTest(case_id=case_id):
                receipt = self.receipt_for(case_id)
                self.assertEqual("validation_required", receipt["decision"])
                for reason in self.cases[case_id]["expected_reasons"]:
                    self.assertIn(reason, receipt["reasons"])

    def test_powershell_parser_rejection_is_detected_before_dispatch(self) -> None:
        receipt = preflight_nested_tool_call(
            {
                "tool_name": "shell_command",
                "arguments": {
                    "command": "foreach ($n in 1..3) { $n } | Sort-Object"
                },
            }
        )
        self.assertIn(receipt["decision"], {"rewrite_candidate", "validation_required"})
        self.assertIn("powershell_parser_rejection", receipt["reasons"])

    def test_accepted_profile_returns_the_parser_verified_dispatch_candidate(self) -> None:
        original = "foreach ($n in 1..3) { $n } | Sort-Object"
        receipt = preflight_nested_tool_call(
            {
                "tool_name": "shell_command",
                "arguments": {"command": original, "timeout_ms": 10000},
            }
        )
        self.assertEqual("rewrite_candidate", receipt["decision"])
        self.assertEqual(
            "$(foreach ($n in 1..3) { $n }) | Sort-Object",
            receipt["normalized_arguments"]["command"],
        )
        self.assertNotEqual(receipt["source_signature"], receipt["dispatch_signature"])
        self.assertEqual(receipt["signature"], receipt["dispatch_signature"])
        self.assertTrue(receipt["rewrite_applied"])

    def test_windows_rg_wildcard_path_is_flagged_but_glob_filter_is_allowed(self) -> None:
        bad = preflight_nested_tool_call(
            {
                "tool_name": "shell_command",
                "arguments": {
                    "command": "rg -n pattern 'C:\\skill-tools\\*.py'"
                },
            }
        )
        good = preflight_nested_tool_call(
            {
                "tool_name": "shell_command",
                "arguments": {
                    "command": "rg -n pattern 'C:\\skill-tools' -g '*.py'"
                },
            }
        )
        self.assertEqual("validation_required", bad["decision"])
        self.assertIn("windows_rg_literal_wildcard_path", bad["reasons"])
        self.assertEqual("allow", good["decision"])

        regex_pattern = preflight_nested_tool_call(
            {
                "tool_name": "shell_command",
                "arguments": {
                    "command": "rg -n -F -e 'README*' 'C:\\skill-tools\\notes.md'"
                },
            }
        )
        self.assertNotIn("windows_rg_literal_wildcard_path", regex_pattern["reasons"])

        for command in (
            "rg --regexp='needle' 'C:\\skill-tools\\*.py'",
            "rg -eneedle 'C:\\skill-tools\\*.py'",
        ):
            with self.subTest(command=command):
                receipt = preflight_nested_tool_call(
                    {"tool_name": "shell_command", "arguments": {"command": command}}
                )
                self.assertIn("windows_rg_literal_wildcard_path", receipt["reasons"])

        context_pattern = preflight_nested_tool_call(
            {
                "tool_name": "shell_command",
                "arguments": {
                    "command": "rg -C 2 'README*' 'C:\\skill-tools\\notes.md'"
                },
            }
        )
        self.assertNotIn(
            "windows_rg_literal_wildcard_path", context_pattern["reasons"]
        )

    def test_broad_session_read_requires_scope_but_bounded_read_is_allowed(self) -> None:
        bad = preflight_nested_tool_call(
            {
                "tool_name": "shell_command",
                "arguments": {
                    "command": "Get-ChildItem -Recurse -LiteralPath 'C:\\Users\\example\\.codex\\sessions'"
                },
            }
        )
        good = preflight_nested_tool_call(
            {
                "tool_name": "shell_command",
                "arguments": {
                    "command": "Get-ChildItem -LiteralPath 'C:\\Users\\example\\.codex\\sessions\\2026\\08\\10' | Select-Object -First 10"
                },
            }
        )
        self.assertIn("broad_output_scope_review_required", bad["reasons"])
        self.assertNotIn("broad_output_scope_review_required", good["reasons"])

    def test_rg_against_one_active_rollout_still_requires_an_output_budget(self) -> None:
        receipt = preflight_nested_tool_call(
            {
                "tool_name": "shell_command",
                "arguments": {
                    "command": "rg -n -F error 'C:\\Users\\example\\.codex\\sessions\\2026\\08\\03\\rollout-active.jsonl'"
                },
            }
        )
        self.assertEqual("validation_required", receipt["decision"])
        self.assertIn("broad_output_scope_review_required", receipt["reasons"])

    def test_active_rollout_get_content_requires_a_real_source_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".codex" / "sessions" / "2026" / "08" / "11" / "rollout-active.jsonl"
            target.parent.mkdir(parents=True)
            target.write_text("one\ntwo\n", encoding="utf-8")
            rejected_commands = [
                f"Get-Content -LiteralPath '{target}'",
                f"Get-Content -LiteralPath '{target}' -Raw | Select-Object -First 1",
                f"Get-Content -LiteralPath '{target}' -Tail $n",
                f"Get-Content -LiteralPath '{target}' -Tail",
                f"Get-Content -LiteralPath '{target}' -TotalCount -1",
                f"Get-Content -LiteralPath '{target}'; Get-Content -LiteralPath '{target.parent / 'other.jsonl'}' -Tail 1",
            ]
            for command in rejected_commands:
                with self.subTest(command=command):
                    receipt = preflight_nested_tool_call(
                        {"tool_name": "shell_command", "arguments": {"command": command}}
                    )
                    self.assertIn(
                        "active_rollout_read_requires_bounded_get_content",
                        receipt["reasons"],
                    )
            for budget in ("-Tail 0", "-TotalCount 1", "-First 1", "-Head 1", "-Last 1"):
                with self.subTest(budget=budget):
                    bounded = preflight_nested_tool_call(
                        {
                            "tool_name": "shell_command",
                            "arguments": {
                                "command": f"Get-Content -LiteralPath '{target}' {budget}"
                            },
                        }
                    )
                    self.assertNotIn(
                        "active_rollout_read_requires_bounded_get_content",
                        bounded["reasons"],
                    )

    def test_large_and_missing_exact_get_content_targets_require_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            large = Path(tmp) / "large.txt"
            large.write_text("x" * 300_000, encoding="utf-8")
            large_receipt = preflight_nested_tool_call(
                {
                    "tool_name": "shell_command",
                    "arguments": {
                        "command": f"Get-Content -LiteralPath '{large}' -Raw"
                    },
                }
            )
            large_stream_receipt = preflight_nested_tool_call(
                {
                    "tool_name": "shell_command",
                    "arguments": {
                        "command": f"Get-Content -LiteralPath '{large}'"
                    },
                }
            )
            missing_receipt = preflight_nested_tool_call(
                {
                    "tool_name": "shell_command",
                    "arguments": {
                        "command": "Get-Content -LiteralPath 'C:\\definitely-missing-cbh-fixture.txt' -Raw"
                    },
                }
            )
            bounded_receipt = preflight_nested_tool_call(
                {
                    "tool_name": "shell_command",
                    "arguments": {
                        "command": f"Get-Content -LiteralPath '{large}' -First 1"
                    },
                }
            )
        self.assertIn("large_exact_read_requires_budget", large_receipt["reasons"])
        self.assertIn("large_exact_read_requires_budget", large_stream_receipt["reasons"])
        self.assertIn("exact_read_target_missing", missing_receipt["reasons"])
        self.assertNotIn("large_exact_read_requires_budget", bounded_receipt["reasons"])

    def test_result_normalization_preserves_strings_and_typed_media_boundaries(self) -> None:
        self.assertEqual(
            "ok", normalize_nested_tool_result("ok")["forwarded_text"]
        )
        normalized = normalize_nested_tool_result(
            {
                "content": [
                    {"type": "text", "text": "done"},
                    {"type": "image", "data": "QUJD", "mimeType": "image/png"},
                    {"type": "audio", "data": "REVG", "mimeType": "audio/wav"},
                ]
            }
        )
        self.assertEqual("typed_content", normalized["kind"])
        self.assertEqual("done", normalized["items"][0]["text"])
        self.assertNotIn("data", normalized["items"][1])
        self.assertEqual("typed_only", normalized["items"][1]["forward"])

        direct_media = normalize_nested_tool_result(
            {"type": "image", "data": "QUJD", "mimeType": "image/png"}
        )
        self.assertEqual("typed_media", direct_media["kind"])
        self.assertNotIn("data", direct_media)
        self.assertNotIn("QUJD", json.dumps(direct_media))

        direct_audio = normalize_nested_tool_result(
            {"type": "audio", "data": "REVG", "mimeType": "audio/wav"}
        )
        self.assertEqual("typed_media", direct_audio["kind"])
        self.assertNotIn("REVG", json.dumps(direct_audio))

        generated = normalize_nested_tool_result(
            {"image_url": "data:image/png;base64,QUJD", "output_hint": "saved"}
        )
        self.assertNotIn("data:image", generated["forwarded_text"])
        self.assertEqual(1, generated["inline_payloads_omitted_from_text"])

        resource = normalize_nested_tool_result(
            {"type": "resource", "resource": {"blob": "QUJD", "mimeType": "image/png"}}
        )
        self.assertNotIn("QUJD", resource["forwarded_text"])
        self.assertEqual(1, resource["inline_payloads_omitted_from_text"])

        nested_media = normalize_nested_tool_result(
            {"wrapper": {"type": "image", "data": "QUJD"}}
        )
        self.assertNotIn("QUJD", nested_media["forwarded_text"])
        self.assertEqual(1, nested_media["inline_payloads_omitted_from_text"])

        long_https_url = "https://example.com/" + ("x" * 400)
        linked_media = normalize_nested_tool_result({"image_url": long_https_url})
        self.assertIn(long_https_url, linked_media["forwarded_text"])

    def test_text_blocks_share_one_total_character_budget(self) -> None:
        normalized = normalize_nested_tool_result(
            {
                "content": [
                    {"type": "text", "text": "a"},
                    {"type": "text", "text": "b"},
                    {"type": "text", "text": "c"},
                ]
            },
            max_chars=1,
        )
        forwarded = "".join(item["text"] for item in normalized["items"])
        self.assertEqual("a", forwarded)
        self.assertTrue(normalized["truncated"])
        self.assertEqual(3, normalized["original_chars"])
        self.assertEqual(1, normalized["forwarded_chars"])
        self.assertEqual(2, normalized["uncovered_chars"])

    def test_result_metadata_and_budget_types_are_bounded(self) -> None:
        direct = normalize_nested_tool_result(
            {"type": "image", "data": "QUJD", "mimeType": "x" * 1000}
        )
        self.assertLessEqual(len(direct["mimeType"]), 160)
        self.assertTrue(direct["metadata_review_required"])

        typed = normalize_nested_tool_result(
            {"content": [{"type": "x" * 1000, "mimeType": {"bad": True}}]}
        )
        self.assertLessEqual(len(typed["items"][0]["type"]), 80)
        self.assertTrue(typed["items"][0]["metadata_review_required"])

        for kwargs in (
            {"max_chars": True},
            {"max_chars": 1.5},
            {"max_items": True},
            {"max_items": 1.5},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, "positive integers"):
                    normalize_nested_tool_result("ok", **kwargs)

    def test_result_budget_never_silently_truncates(self) -> None:
        normalized = normalize_nested_tool_result("x" * 100, max_chars=20)
        self.assertTrue(normalized["truncated"])
        self.assertEqual(100, normalized["original_chars"])
        self.assertRegex(normalized["sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("uncovered", normalized)


if __name__ == "__main__":
    unittest.main()
