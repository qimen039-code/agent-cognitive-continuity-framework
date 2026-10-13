from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from dangerous_delete_guard import advisory_receipt, classify_command, handle_event


class DangerousDeleteGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.inside = self.cwd / "one.txt"
        self.outside = self.root / "outside.txt"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def classify(self, command: str) -> dict:
        return classify_command(command, self.cwd)

    def event(self, command: str, *, session: str = "s") -> dict:
        return {
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "cwd": str(self.cwd),
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }

    def prompt(self, text: str, *, session: str = "s") -> dict:
        return {
            "hook_event_name": "UserPromptSubmit",
            "session_id": session,
            "cwd": str(self.cwd),
            "prompt": text,
        }

    def test_ordinary_commands_and_single_inside_delete_are_not_high_risk(self) -> None:
        commands = [
            "Get-ChildItem -LiteralPath .",
            "rg -n 'Remove-Item' README.md",
            f"Remove-Item -LiteralPath '{self.inside}' -Force",
            f"Remove-Item -LiteralPath '{self.inside}' -WhatIf",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(self.classify(command)["dangerous"])

    def test_dangerous_delete_shapes_are_classified(self) -> None:
        cases = {
            f"Remove-Item -LiteralPath '{self.cwd / 'tree'}' -Recurse -Force": "recursive_delete",
            "Remove-Item -Path '.\\*.tmp' -Force": "unresolved_or_spread_target",
            "Remove-Item -LiteralPath $target -Force": "unresolved_or_spread_target",
            f"Remove-Item -LiteralPath '{self.inside}','{self.cwd / 'two.txt'}'": "unresolved_or_spread_target",
            f"Remove-Item -LiteralPath '{self.outside}'": "target_not_strictly_inside_cwd",
            "Get-ChildItem *.tmp | Remove-Item -Force": "pipeline_delete",
            "git clean -fdx": "git_clean_force",
            "git reset --hard HEAD": "git_reset_hard",
            "find . -name '*.tmp' -delete": "find_delete",
            "rsync -a --delete src/ dst/": "rsync_delete",
            "robocopy source destination /MIR": "robocopy_mirror_or_purge",
            "Clear-RecycleBin -Force": "permanent_recycle_bin_clear",
            "cmd /c rmdir /s /q C:\\temp\\nested": "nested_shell_delete",
            'pwsh -NoProfile -Command "Remove-Item -LiteralPath C:\\temp\\nested -Recurse -Force"': "nested_shell_delete",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                result = self.classify(command)
                self.assertTrue(result["dangerous"], result)
                self.assertIn(expected, result["reasons"])

    def test_equivalent_working_directory_spellings_keep_the_same_scope(self) -> None:
        equivalent = self.cwd / ".." / "workspace"
        inside = classify_command(f"Remove-Item -LiteralPath '{self.inside}' -Force", equivalent)
        outside = classify_command(f"Remove-Item -LiteralPath '{self.outside}' -Force", equivalent)
        self.assertFalse(inside["dangerous"], inside)
        self.assertTrue(outside["dangerous"], outside)
        self.assertIn("target_not_strictly_inside_cwd", outside["reasons"])

    def test_dry_runs_are_not_high_risk(self) -> None:
        for command in ("git clean -ndx", "robocopy source destination /MIR /L", "Clear-RecycleBin -WhatIf"):
            with self.subTest(command=command):
                self.assertFalse(self.classify(command)["dangerous"])

    def test_hook_payload_is_always_nonblocking_and_stateless(self) -> None:
        command = f"Remove-Item -LiteralPath '{self.cwd / 'tree'}' -Recurse -Force"
        state_root = self.root / "state"
        self.assertEqual({}, handle_event(self.event(command), state_root=state_root))
        self.assertEqual(
            {},
            handle_event(
                self.prompt("确认永久删除这些已列明目标"),
                state_root=state_root,
            ),
        )
        self.assertFalse(state_root.exists())

    def test_advisory_receipt_reports_risk_without_authority_or_blocking(self) -> None:
        command = f"Remove-Item -LiteralPath '{self.cwd / 'tree'}' -Recurse -Force"
        receipt = advisory_receipt(command, self.cwd)
        self.assertEqual("cbh.dangerous_delete_advisory.v1", receipt["schema"])
        self.assertEqual("risk_shape_detected", receipt["status"])
        self.assertFalse(receipt["blocking"])
        self.assertIn("recursive_delete", receipt["reasons"])
        self.assertIn("not authorization", receipt["evidence_boundary"])

    def test_advisory_classifier_has_no_approval_or_replay_state(self) -> None:
        first_command = f"Remove-Item -LiteralPath '{self.cwd / 'a'}' -Recurse -Force"
        changed_command = f"Remove-Item -LiteralPath '{self.cwd / 'b'}' -Recurse -Force"
        state_root = self.root / "state"
        self.assertEqual({}, handle_event(self.event(first_command), state_root=state_root))
        self.assertEqual({}, handle_event(self.event(changed_command), state_root=state_root))
        self.assertFalse(state_root.exists())

    def test_user_prompt_and_ordinary_command_are_both_noop(self) -> None:
        command = f"Remove-Item -LiteralPath '{self.cwd / 'tree'}' -Recurse -Force"
        state_root = self.root / "state"
        self.assertEqual({}, handle_event(self.event(command), state_root=state_root))
        self.assertEqual(
            {}, handle_event(self.prompt("取消，不要删除"), state_root=state_root)
        )
        self.assertEqual(
            {},
            handle_event(
                self.event("Get-ChildItem -LiteralPath ."), state_root=state_root
            ),
        )
        self.assertFalse(state_root.exists())

    def test_no_pending_state_is_created_for_ordinary_shell(self) -> None:
        state_root = self.root / "state"
        self.assertEqual({}, handle_event(self.event("Get-Content README.md"), state_root=state_root))
        self.assertFalse(state_root.exists())


if __name__ == "__main__":
    unittest.main()
