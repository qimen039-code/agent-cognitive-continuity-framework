from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from execution_feedback import (
    CorrectionProfileRegistryError,
    POWERSHELL_STATEMENT_LOOP_PIPELINE,
    apply_behavior_rewrite,
    detect_correction_profile,
    derive_subject_binding,
    feedback_context,
    load_correction_profiles,
    new_correction_capsule,
    new_feedback_capsule,
    observe_posttool_verification,
    observe_related_input,
    observe_related_parse,
    registered_handler_ids,
)


class ExecutionFeedbackTests(unittest.TestCase):
    def test_profile_registry_is_generic_bounded_and_extensible(self) -> None:
        profiles = load_correction_profiles()
        self.assertEqual(
            {
                "powershell_statement_loop_pipeline",
                "powershell_statement_loop_pipeline_pretool_migration",
                "powershell_interpolated_variable_colon",
                "powershell_bash_heredoc_translation",
                "powershell_unquoted_bash_heredoc_translation",
                "unicode_replacement_character_text_mutation",
                "unicode_replacement_character_python_inline_write",
            },
            set(profiles),
        )
        for profile in profiles.values():
            self.assertGreaterEqual(profile["profile_version"], 1)
            self.assertEqual(64, len(profile["profile_sha256"]))
            self.assertIn(profile["behavior_class"], {
                "shell_syntax_translation",
                "shell_dialect_translation",
                "text_encoding_integrity",
            })
            self.assertTrue(profile["verifier"])
            self.assertTrue(profile["rewrite_boundary"])
            self.assertTrue(profile["escalation_boundary"])
            self.assertTrue(profile["correction_family_id"])
            self.assertIn(
                profile["decision_mode"],
                {"auto_rewrite", "preflight_validate", "predictive_review"},
            )
            self.assertIn(
                profile["promotion_status"],
                {"accepted", "validation_only", "semantic_review_only"},
            )
            self.assertIsInstance(profile["source_record_ids"], list)
            self.assertTrue(profile["postcondition"])
            self.assertNotEqual("deny_current_event", profile["enforcement"])
        handlers = registered_handler_ids()
        self.assertTrue(
            {profile["match_kind"] for profile in profiles.values()}
            <= handlers["matcher"]
        )
        self.assertTrue(
            {profile["binding_handler"] for profile in profiles.values()}
            <= handlers["binding"]
        )
        self.assertTrue(
            {profile["related_handler"] for profile in profiles.values()}
            <= handlers["related"]
        )
        self.assertTrue(
            {profile["verifier"] for profile in profiles.values()}
            <= handlers["verifier"]
        )
        migration = profiles["powershell_statement_loop_pipeline_pretool_migration"]
        self.assertEqual("rewrite_current_event_nonblocking", migration["enforcement"])
        self.assertEqual("auto_rewrite", migration["decision_mode"])
        self.assertEqual("accepted", migration["promotion_status"])
        self.assertTrue(migration["historical_replay_refs"])
        self.assertIn(
            migration["runtime_migration"]["rewrite_handler"], handlers["rewrite"]
        )
        rewritten = apply_behavior_rewrite(
            migration,
            "foreach ($n in 1..3) { $n } | Sort-Object",
        )
        self.assertEqual(
            "$(foreach ($n in 1..3) { $n }) | Sort-Object",
            rewritten,
        )

    def test_runtime_registry_hash_mismatch_fails_closed(self) -> None:
        source = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness" / "behavior_correction_profiles.json"
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / "profiles.json"
            payload = json.loads(source.read_text(encoding="utf-8"))
            payload["profiles"][0]["rewrite_instruction"] = "drifted"
            candidate.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                CorrectionProfileRegistryError,
                "behavior_correction_profile_registry_hash_mismatch",
            ):
                load_correction_profiles(
                    candidate,
                    expected_registry_sha256="0" * 64,
                )

    def test_explicit_default_registry_hash_cannot_bypass_policy_contract(self) -> None:
        with self.assertRaisesRegex(
            CorrectionProfileRegistryError,
            "behavior_correction_explicit_hash_conflicts_policy",
        ):
            load_correction_profiles(expected_registry_sha256="0" * 64)

    def test_generic_detector_separates_environment_role_and_trigger_stage(self) -> None:
        statement = detect_correction_profile(
            stage="parser_rejection",
            environment="powershell",
            tool_role="read_only",
            text="foreach ($n in 1..3) { $n } | Sort-Object",
            parser_error_ids=["EmptyPipeElement"],
        )
        self.assertEqual("powershell_statement_loop_pipeline", statement["profile_id"])
        self.assertIsNone(
            detect_correction_profile(
                stage="parser_rejection",
                environment="bash",
                tool_role="read_only",
                text="for n in 1 2 3; do echo \"$n\"; done | sort",
                parser_error_ids=["EmptyPipeElement"],
            )
        )

        interpolation = detect_correction_profile(
            stage="parser_rejection",
            environment="powershell",
            tool_role="read_only",
            text='$errors | ForEach-Object { "$file:$($_.ErrorId)" }',
            parser_error_ids=["InvalidVariableReferenceWithDrive"],
        )
        self.assertEqual(
            "powershell_interpolated_variable_colon",
            interpolation["profile_id"],
        )
        self.assertIsNone(
            detect_correction_profile(
                stage="parser_rejection",
                environment="powershell",
                tool_role="read_only",
                text='$errors | ForEach-Object { "$file:$($_.ErrorId)" }',
                parser_error_ids=["UnexpectedToken"],
            )
        )

        self.assertEqual(
            derive_subject_binding(
                interpolation,
                text='$errors | ForEach-Object { "$file:$($_.ErrorId)" }',
                target_binding_sha256="",
            ),
            derive_subject_binding(
                interpolation,
                text='$errors | ForEach-Object { "${file}:$($_.ErrorId)" }',
                target_binding_sha256="",
            ),
        )

        heredoc = detect_correction_profile(
            stage="pretool",
            environment="powershell",
            tool_role="unknown",
            text="python - <<'PY'\nprint('ok')\nPY",
        )
        self.assertEqual("powershell_bash_heredoc_translation", heredoc["profile_id"])

        expanding_heredoc = detect_correction_profile(
            stage="pretool",
            environment="powershell",
            tool_role="unknown",
            text="python - <<PY\nprint('$HOME')\nPY",
        )
        self.assertEqual(
            "powershell_unquoted_bash_heredoc_translation",
            expanding_heredoc["profile_id"],
        )

        encoding = detect_correction_profile(
            stage="pretool",
            environment="any",
            tool_role="mutating",
            tool_surface="filesystem_content_mutation",
            text="*** Update File: report.txt\n+损坏文本�",
        )
        self.assertEqual(
            "unicode_replacement_character_text_mutation",
            encoding["profile_id"],
        )
        self.assertIsNone(
            detect_correction_profile(
                stage="pretool",
                environment="any",
                tool_role="read_only",
                tool_surface="filesystem_content_mutation",
                text="rg -n '�' README.md",
            )
        )

        inline_encoding = detect_correction_profile(
            stage="pretool",
            environment="powershell",
            tool_role="unknown",
            tool_surface="filesystem_content_mutation",
            text=r"""python -c "open('report.txt','w').write('bad\ufffd')" """,
        )
        self.assertEqual(
            "unicode_replacement_character_python_inline_write",
            inline_encoding["profile_id"],
        )
        self.assertIsNone(
            detect_correction_profile(
                stage="pretool",
                environment="powershell",
                tool_role="unknown",
                tool_surface="filesystem_content_mutation",
                text="""python -c "print('bad�')" """,
            )
        )
        self.assertIsNone(
            detect_correction_profile(
                stage="pretool",
                environment="any",
                tool_role="mutating",
                tool_surface="non_filesystem_mutation",
                text="send message to agent: damaged�",
            )
        )

    def test_statement_loop_matcher_ignores_non_code_lexical_regions(self) -> None:
        samples = [
            (
                '$text = "prefix\n'
                'foreach ($n in 1) { $n } | Sort-Object\n'
                'suffix"'
            ),
            (
                "$text = @'\n"
                "foreach ($n in 1) { $n } | Sort-Object\n"
                "'@\n"
                "Write-Output done"
            ),
            (
                "<# outer block\n"
                "foreach ($n in 1) { $n } | Sort-Object\n"
                "<# nested block #>\n"
                "#>\n"
                "Write-Output done"
            ),
            (
                '$text = "escaped `" quote '
                'foreach ($n in 1) { $n } | Sort-Object"'
            ),
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertIsNone(
                    detect_correction_profile(
                        stage="parser_rejection",
                        environment="powershell",
                        tool_role="read_only",
                        text=sample,
                        parser_error_ids=["EmptyPipeElement"],
                    )
                )

        detected = detect_correction_profile(
            stage="parser_rejection",
            environment="powershell",
            tool_role="read_only",
            text="foreach ($n in 1) { $n } | Sort-Object",
            parser_error_ids=["EmptyPipeElement"],
        )
        self.assertEqual(
            "powershell_statement_loop_pipeline",
            detected["profile_id"],
        )

    def capsule(self) -> dict:
        return new_feedback_capsule(
            session_id_sha256="a" * 64,
            episode_id="EP-" + "b" * 32,
            task_sha256="c" * 64,
            shell="powershell",
            error_class=POWERSHELL_STATEMENT_LOOP_PIPELINE,
            parser_error_ids=["EmptyPipeElement"],
            source_record_id="CE-POWERSHELL-FOREACH-PIPELINE-2026-07-10",
            tool_sha256="d" * 64,
            now="2026-07-16T00:00:00Z",
        )

    def test_capsule_is_compact_source_bound_and_contains_no_raw_command(self) -> None:
        capsule = self.capsule()
        self.assertEqual("cbh.execution_feedback_capsule.v1", capsule["schema"])
        self.assertEqual("active", capsule["status"])
        self.assertEqual("candidate", capsule["strategy_status"])
        self.assertEqual(1, capsule["attempt_count"])
        self.assertEqual(
            "cbh.execution_subject_target_binding.v1",
            capsule["binding_schema"],
        )
        self.assertEqual(64, len(capsule["profile_sha256"]))
        self.assertNotIn("action_binding", capsule)
        self.assertNotIn("safe_template", capsule)
        serialized = repr(capsule)
        self.assertNotIn("candidate-secret", serialized)
        self.assertNotIn("raw_command", capsule)
        self.assertRegex(capsule["error_signature_sha256"], r"^[0-9a-f]{64}$")

    def test_unrelated_valid_command_does_not_retire_capsule(self) -> None:
        updated, outcome = observe_related_parse(
            self.capsule(),
            command="Get-Content -LiteralPath .\\candidate.txt",
            parser_error_ids=[],
            tool_sha256="e" * 64,
            now="2026-07-16T00:01:00Z",
        )
        self.assertEqual("unrelated", outcome)
        self.assertEqual("active", updated["status"])
        self.assertEqual(1, updated["attempt_count"])

    def test_related_parser_pass_retires_capsule(self) -> None:
        updated, outcome = observe_related_parse(
            self.capsule(),
            command="$rows = foreach ($n in 1..3) { $n }; $rows | Sort-Object",
            parser_error_ids=[],
            tool_sha256="e" * 64,
            now="2026-07-16T00:01:00Z",
        )
        self.assertEqual("resolved", outcome)
        self.assertEqual("resolved", updated["status"])
        self.assertEqual(
            "subject_binding_preserved_and_powershell_parser_zero_errors",
            updated["resolution"],
        )

    def test_related_parser_failure_marks_review_required_and_increments_attempt(self) -> None:
        updated, outcome = observe_related_parse(
            self.capsule(),
            command="foreach ($n in 1..3) { $n } | Sort-Object",
            parser_error_ids=["EmptyPipeElement"],
            tool_sha256="e" * 64,
            now="2026-07-16T00:01:00Z",
        )
        self.assertEqual("repeated_failure", outcome)
        self.assertEqual("active", updated["status"])
        self.assertEqual("review_required", updated["strategy_status"])
        self.assertEqual(
            "repeated_same_parser_signature",
            updated["review_reason"],
        )
        self.assertNotIn("quarantine_reason", updated)
        self.assertEqual(2, updated["attempt_count"])

    def test_different_parser_signature_does_not_mark_current_candidate_for_review(self) -> None:
        updated, outcome = observe_related_parse(
            self.capsule(),
            command="foreach ($n in 1..3) { $n } | Sort-Object",
            parser_error_ids=["DifferentParserError"],
            tool_sha256="e" * 64,
            now="2026-07-16T00:01:00Z",
        )
        self.assertEqual("different_signature", outcome)
        self.assertEqual("active", updated["status"])
        self.assertEqual("candidate", updated["strategy_status"])
        self.assertEqual(1, updated["attempt_count"])

    def test_feedback_context_is_bounded_and_actionable(self) -> None:
        context = feedback_context(self.capsule())
        self.assertLessEqual(len(context), 1000)
        self.assertIn("CE-POWERSHELL-FOREACH-PIPELINE-2026-07-10", context)
        self.assertIn("$rows = foreach", context)
        self.assertIn("同一 PowerShell Parser", context)
        self.assertIn("保留原目标内容和精确目标位置", context)
        self.assertIn("只改写语法结构", context)
        self.assertNotIn("拒绝", context)
        self.assertNotIn("隔离", context)

    def test_encoding_correction_is_related_by_exact_target_and_retires_on_clean_input(self) -> None:
        profile = load_correction_profiles()[
            "unicode_replacement_character_text_mutation"
        ]
        invalid = "*** Update File: report.txt\n+损坏文本�"
        capsule = new_correction_capsule(
            profile=profile,
            session_id_sha256="a" * 64,
            episode_id="EP-" + "b" * 32,
            task_sha256="c" * 64,
            evidence_ids=[
                "unicode_replacement_character_text_mutation",
                "f" * 64,
            ],
            tool_sha256="d" * 64,
            target_binding_sha256="f" * 64,
            subject_binding_sha256=derive_subject_binding(
                profile,
                text=invalid,
                target_binding_sha256="f" * 64,
            ),
            now="2026-07-16T00:00:00Z",
        )

        unrelated, outcome = observe_related_input(
            capsule,
            text="*** Update File: other.txt\n+修复文本",
            tool_role="mutating",
            tool_surface="filesystem_content_mutation",
            target_binding_sha256="e" * 64,
            tool_sha256="1" * 64,
            now="2026-07-16T00:00:30Z",
        )
        self.assertEqual("unrelated", outcome)
        self.assertEqual("active", unrelated["status"])

        pending, outcome = observe_related_input(
            capsule,
            text="*** Update File: report.txt\n+修复文本",
            tool_role="mutating",
            tool_surface="filesystem_content_mutation",
            target_binding_sha256="f" * 64,
            tool_sha256="2" * 64,
            now="2026-07-16T00:01:00Z",
        )
        self.assertEqual("candidate_verified", outcome)
        self.assertEqual("active", pending["status"])
        self.assertEqual(
            "awaiting_posttool_readback",
            pending["verification_status"],
        )

        unavailable, outcome = observe_posttool_verification(
            pending,
            tool_sha256="2" * 64,
            outcome_status="no_failure_observed",
            readback_status="unavailable",
            readback_text="",
            now="2026-07-16T00:01:15Z",
        )
        self.assertEqual("verification_pending", outcome)
        self.assertEqual(
            "exact_target_readback_unavailable",
            unavailable["verification_status"],
        )
        self.assertEqual(
            "exact_target_readback_unavailable",
            unavailable["last_verifier_reason"],
        )

        resolved, outcome = observe_posttool_verification(
            unavailable,
            tool_sha256="2" * 64,
            outcome_status="no_failure_observed",
            readback_status="verified",
            readback_text="修复文本",
            now="2026-07-16T00:01:30Z",
        )
        self.assertEqual("resolved", outcome)
        self.assertEqual("resolved", resolved["status"])
        self.assertEqual(
            "exact_target_utf8_decodable_without_u_fffd",
            resolved["resolution"],
        )

    def test_python_inline_encoding_binding_preserves_static_operation_envelope(self) -> None:
        profile = load_correction_profiles()[
            "unicode_replacement_character_python_inline_write"
        ]
        target = "f" * 64
        invalid = r"""python -c "open('report.txt','w').write('bad\ufffd')" """
        capsule = new_correction_capsule(
            profile=profile,
            session_id_sha256="a" * 64,
            episode_id="EP-" + "b" * 32,
            task_sha256="c" * 64,
            evidence_ids=[profile["profile_id"]],
            tool_sha256="d" * 64,
            target_binding_sha256=target,
            subject_binding_sha256=derive_subject_binding(
                profile,
                text=invalid,
                target_binding_sha256=target,
            ),
            now="2026-07-16T00:00:00Z",
        )

        changed_mode, outcome = observe_related_input(
            capsule,
            text="""python -c "open('report.txt','a').write('clean')" """,
            tool_role="unknown",
            tool_surface="filesystem_content_mutation",
            target_binding_sha256=target,
            tool_sha256="0" * 64,
            now="2026-07-16T00:00:30Z",
        )
        self.assertEqual("unrelated", outcome)
        self.assertEqual("active", changed_mode["status"])

        compound, outcome = observe_related_input(
            capsule,
            text=(
                """python -c "print('diagnostic�'); """
                """open('report.txt','w').write('clean')" """
            ),
            tool_role="unknown",
            tool_surface="filesystem_content_mutation",
            target_binding_sha256=target,
            tool_sha256="1" * 64,
            now="2026-07-16T00:01:00Z",
        )
        self.assertEqual("unrelated", outcome)
        self.assertEqual("active", compound["status"])

        unresolved, outcome = observe_related_input(
            capsule,
            text="""python -c "open('report.txt','w').write(value)" """,
            tool_role="unknown",
            tool_surface="filesystem_content_mutation",
            target_binding_sha256=target,
            tool_sha256="2" * 64,
            now="2026-07-16T00:02:00Z",
        )
        self.assertEqual("unrelated", outcome)
        self.assertEqual("active", unresolved["status"])

        verified, outcome = observe_related_input(
            capsule,
            text="""python -c "open('report.txt','w').write('clean')" """,
            tool_role="unknown",
            tool_surface="filesystem_content_mutation",
            target_binding_sha256=target,
            tool_sha256="3" * 64,
            now="2026-07-16T00:03:00Z",
        )
        self.assertEqual("candidate_verified", outcome)
        self.assertEqual("awaiting_posttool_readback", verified["verification_status"])

    def test_subject_binding_separates_different_inline_scripts_in_one_cwd(self) -> None:
        profile = load_correction_profiles()[
            "powershell_bash_heredoc_translation"
        ]
        target = "f" * 64
        first = derive_subject_binding(
            profile,
            text="python - <<'PY'\nprint('one')\nPY",
            target_binding_sha256=target,
        )
        second = derive_subject_binding(
            profile,
            text="python - <<'PY'\nprint('two')\nPY",
            target_binding_sha256=target,
        )
        corrected = derive_subject_binding(
            profile,
            text=(
                "$script = @'\n"
                "print('one')\n"
                "'@\n"
                "$script | python -"
            ),
            target_binding_sha256=target,
        )
        self.assertNotEqual(first, second)
        self.assertEqual(first, corrected)

        interpolating = derive_subject_binding(
            profile,
            text=(
                '$script = @"\n'
                "print('one')\n"
                '"@\n'
                "$script | python -"
            ),
            target_binding_sha256=target,
        )
        self.assertNotEqual(first, interpolating)

    def test_unquoted_heredoc_subject_matches_literal_transport_to_bash(self) -> None:
        profile = load_correction_profiles()[
            "powershell_unquoted_bash_heredoc_translation"
        ]
        invalid = "python - <<PY\nprint('$HOME')\nPY"
        transport = (
            "$script = @'\n"
            "python - <<PY\n"
            "print('$HOME')\n"
            "PY\n"
            "'@\n"
            "$script | bash -s"
        )
        wrong_literal_translation = (
            "$script = @'\n"
            "print('$HOME')\n"
            "'@\n"
            "$script | python -"
        )

        subject = derive_subject_binding(
            profile,
            text=invalid,
            target_binding_sha256="",
        )
        self.assertEqual(
            subject,
            derive_subject_binding(
                profile,
                text=transport,
                target_binding_sha256="",
            ),
        )
        self.assertNotEqual(
            subject,
            derive_subject_binding(
                profile,
                text=wrong_literal_translation,
                target_binding_sha256="",
            ),
        )

        capsule = new_correction_capsule(
            profile=profile,
            session_id_sha256="a" * 64,
            episode_id="EP-" + "b" * 32,
            task_sha256="c" * 64,
            evidence_ids=[profile["profile_id"]],
            tool_sha256="d" * 64,
            now="2026-07-16T00:00:00Z",
            subject_binding_sha256=subject,
        )
        pending, outcome = observe_related_parse(
            capsule,
            command=transport,
            parser_error_ids=[],
            tool_sha256="e" * 64,
            now="2026-07-16T00:01:00Z",
        )
        self.assertEqual("verification_pending", outcome)
        self.assertEqual("active", pending["status"])
        self.assertEqual("pending", pending["last_verifier_status"])
        self.assertEqual(
            "awaiting_same_bash_posttool",
            pending["verification_status"],
        )

        resolved, outcome = observe_posttool_verification(
            pending,
            tool_sha256="e" * 64,
            outcome_status="no_failure_observed",
            readback_status="unavailable",
            readback_text="",
            now="2026-07-16T00:01:30Z",
        )
        self.assertEqual("resolved", outcome)
        self.assertEqual("resolved", resolved["status"])

    def test_subject_binding_preserves_internal_whitespace_and_argument_case(self) -> None:
        profiles = load_correction_profiles()
        loop = profiles["powershell_statement_loop_pipeline"]
        first_loop = derive_subject_binding(
            loop,
            text='foreach ($n in 1) { "a  b" } | Write-Output',
            target_binding_sha256="",
        )
        second_loop = derive_subject_binding(
            loop,
            text='foreach ($n in 1) { "a b" } | Write-Output',
            target_binding_sha256="",
        )
        self.assertNotEqual(first_loop, second_loop)

        heredoc = profiles["powershell_bash_heredoc_translation"]
        first_module = derive_subject_binding(
            heredoc,
            text="python -m MyModule - <<'PY'\nprint('ok')\nPY",
            target_binding_sha256="",
        )
        second_module = derive_subject_binding(
            heredoc,
            text="python -m mymodule - <<'PY'\nprint('ok')\nPY",
            target_binding_sha256="",
        )
        self.assertNotEqual(first_module, second_module)

    def test_nested_loop_binding_covers_complete_balanced_body(self) -> None:
        profile = load_correction_profiles()[
            "powershell_statement_loop_pipeline"
        ]
        first = derive_subject_binding(
            profile,
            text=(
                "foreach ($n in 1..3) { "
                "if ($n) { Write-Output 'inner' }; Write-Output 'one' "
                "} | Sort-Object"
            ),
            target_binding_sha256="",
            execution_cwd=r"C:\workspace",
        )
        second = derive_subject_binding(
            profile,
            text=(
                "foreach ($n in 1..3) { "
                "if ($n) { Write-Output 'inner' }; Write-Output 'two' "
                "} | Sort-Object"
            ),
            target_binding_sha256="",
            execution_cwd=r"C:\workspace",
        )
        corrected = derive_subject_binding(
            profile,
            text=(
                "$rows = foreach ($n in 1..3) { "
                "if ($n) { Write-Output 'inner' }; Write-Output 'one' "
                "}; $rows | Sort-Object"
            ),
            target_binding_sha256="",
            execution_cwd=r"C:\workspace",
        )
        self.assertNotEqual(first, second)
        self.assertEqual(first, corrected)

    def test_loop_binding_preserves_prefix_context_and_multiline_lexical_state(self) -> None:
        profile = load_correction_profiles()[
            "powershell_statement_loop_pipeline"
        ]
        invalid = (
            "Set-Location C:\\A\n"
            "foreach ($n in 1..3) {\n"
            'Write-Output "literal\n} inside string"\n'
            "<# block comment with }\n"
            "   and nested <# inner } #> text\n"
            "#>\n"
            "Write-Output $n\n"
            "} | Sort-Object"
        )
        corrected = (
            "Set-Location C:\\A\n"
            "$rows = foreach ($n in 1..3) {\n"
            'Write-Output "literal\n} inside string"\n'
            "<# block comment with }\n"
            "   and nested <# inner } #> text\n"
            "#>\n"
            "Write-Output $n\n"
            "}; $rows | Sort-Object"
        )
        changed_prefix = corrected.replace(
            "Set-Location C:\\A",
            "Set-Location C:\\B",
        )
        expected = derive_subject_binding(
            profile,
            text=invalid,
            target_binding_sha256="",
            execution_cwd=r"C:\workspace",
        )
        self.assertEqual(
            expected,
            derive_subject_binding(
                profile,
                text=corrected,
                target_binding_sha256="",
                execution_cwd=r"C:\workspace",
            ),
        )
        self.assertNotEqual(
            expected,
            derive_subject_binding(
                profile,
                text=changed_prefix,
                target_binding_sha256="",
                execution_cwd=r"C:\workspace",
            ),
        )

    def test_heredoc_binding_preserves_command_context_and_cwd(self) -> None:
        profile = load_correction_profiles()[
            "powershell_bash_heredoc_translation"
        ]
        invalid = (
            "Set-Location C:\\A\n"
            "python - <<'PY'\n"
            "print('one')\n"
            "PY\n"
            "Write-Output done"
        )
        corrected = (
            "Set-Location C:\\A\n"
            "$script = @'\n"
            "print('one')\n"
            "'@\n"
            "$script | python -\n"
            "Write-Output done"
        )
        changed_prefix = corrected.replace(
            "Set-Location C:\\A",
            "Set-Location C:\\B",
        )
        changed_literal_whitespace = corrected.replace(
            "Set-Location C:\\A",
            'Write-Output "a b"\nSet-Location C:\\A',
        )
        original_literal_whitespace = invalid.replace(
            "Set-Location C:\\A",
            'Write-Output "a  b"\nSet-Location C:\\A',
        )
        expected = derive_subject_binding(
            profile,
            text=invalid,
            target_binding_sha256="",
            execution_cwd=r"C:\workspace",
        )
        self.assertEqual(
            expected,
            derive_subject_binding(
                profile,
                text=corrected,
                target_binding_sha256="",
                execution_cwd=r"C:\workspace",
            ),
        )
        self.assertNotEqual(
            expected,
            derive_subject_binding(
                profile,
                text=changed_prefix,
                target_binding_sha256="",
                execution_cwd=r"C:\workspace",
            ),
        )
        self.assertNotEqual(
            expected,
            derive_subject_binding(
                profile,
                text=corrected,
                target_binding_sha256="",
                execution_cwd=r"C:\other",
            ),
        )
        self.assertNotEqual(
            derive_subject_binding(
                profile,
                text=original_literal_whitespace,
                target_binding_sha256="",
                execution_cwd=r"C:\workspace",
            ),
            derive_subject_binding(
                profile,
                text=changed_literal_whitespace,
                target_binding_sha256="",
                execution_cwd=r"C:\workspace",
            ),
        )

    def test_heredoc_text_inside_powershell_data_is_not_a_command_match(self) -> None:
        command = (
            "$doc = @'\n"
            "python - <<'PY'\n"
            "print('documentation only')\n"
            "PY\n"
            "'@\n"
            "Write-Output $doc"
        )
        self.assertIsNone(
            detect_correction_profile(
                stage="pretool",
                environment="powershell",
                tool_role="unknown",
                text=command,
            )
        )

    def test_encoding_matcher_only_checks_candidate_write_delta(self) -> None:
        deleting_corruption = detect_correction_profile(
            stage="pretool",
            environment="any",
            tool_role="mutating",
            tool_surface="filesystem_content_mutation",
            text=(
                "*** Begin Patch\n"
                "*** Update File: report.txt\n"
                "@@\n"
                "-损坏文本�\n"
                "+修复文本\n"
                "*** End Patch"
            ),
        )
        self.assertIsNone(deleting_corruption)

        adding_corruption = detect_correction_profile(
            stage="pretool",
            environment="any",
            tool_role="mutating",
            tool_surface="filesystem_content_mutation",
            text=(
                "*** Begin Patch\n"
                "*** Update File: report.txt\n"
                "@@\n"
                "-修复文本\n"
                "+损坏文本�\n"
                "*** End Patch"
            ),
        )
        self.assertEqual(
            "unicode_replacement_character_text_mutation",
            adding_corruption["profile_id"],
        )


if __name__ == "__main__":
    unittest.main()
