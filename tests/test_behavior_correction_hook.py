from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from behavior_correction_hook import (
    _powershell_executable,
    handle_event,
    powershell_parser_error_ids,
)


class BehaviorCorrectionHookTests(unittest.TestCase):
    def event(self, command: str, *, tool_name: str = "Bash") -> dict:
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "cwd": str(Path(__file__).resolve().parent),
            "tool_input": {
                "command": command,
                "timeout_ms": 10000,
            },
        }

    @staticmethod
    def deterministic_parser(text: str) -> list[str]:
        if "foreach" in text and "$(foreach" not in text:
            return ["EmptyPipeElement"]
        return []

    def test_exact_invalid_shape_is_transparently_rewritten(self) -> None:
        output = handle_event(
            self.event("foreach ($n in 1..3) { $n } | Sort-Object"),
            parser=self.deterministic_parser,
        )
        specific = output["hookSpecificOutput"]
        self.assertEqual("PreToolUse", specific["hookEventName"])
        self.assertEqual("allow", specific["permissionDecision"])
        self.assertEqual(
            "$(foreach ($n in 1..3) { $n }) | Sort-Object",
            specific["updatedInput"]["command"],
        )
        self.assertEqual(10000, specific["updatedInput"]["timeout_ms"])
        self.assertNotIn("permissionDecisionReason", specific)

    def test_non_bash_valid_ambiguous_and_replacement_character_are_noop(self) -> None:
        cases = [
            self.event("foreach ($n in 1) { $n } | Sort-Object", tool_name="apply_patch"),
            self.event("$(foreach ($n in 1) { $n }) | Sort-Object"),
            self.event("Write-Output 'foreach ($n in 1) { $n } | Sort-Object'"),
            self.event("foreach ($n in 1) { '损坏�' } | Sort-Object"),
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(
                    {}, handle_event(payload, parser=self.deterministic_parser)
                )

    def test_functions_exec_envelope_is_still_outside_the_native_hook(self) -> None:
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "exec",
            "tool_input": {
                "tool_name": "shell_command",
                "arguments": {
                    "command": "foreach ($n in 1..3) { $n } | Sort-Object"
                },
            },
        }
        self.assertEqual({}, handle_event(payload, parser=self.deterministic_parser))

    def test_unexpected_or_unavailable_parser_result_never_rewrites_or_blocks(self) -> None:
        payload = self.event("foreach ($n in 1) { $n } | Sort-Object")
        self.assertEqual({}, handle_event(payload, parser=lambda _: None))
        self.assertEqual({}, handle_event(payload, parser=lambda _: ["UnexpectedToken"]))

    @unittest.skipUnless(_powershell_executable(), "PowerShell parser unavailable")
    def test_real_powershell_parser_accepts_rewritten_candidate(self) -> None:
        output = handle_event(
            self.event("foreach ($n in 1..3) { $n } | Sort-Object")
        )
        candidate = output["hookSpecificOutput"]["updatedInput"]["command"]
        self.assertEqual([], powershell_parser_error_ids(candidate))

    @unittest.skipUnless(_powershell_executable(), "PowerShell parser unavailable")
    def test_three_historical_empty_pipe_candidates_are_rewritten_before_execution(self) -> None:
        historical_candidates = [
            (
                "$candidates=@('C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',"
                "'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',"
                "'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',"
                "'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',"
                "'C:\\Users\\example\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe'); "
                "foreach($p in $candidates){[pscustomobject]@{Path=$p;Exists=(Test-Path -LiteralPath $p)}} "
                "| Format-Table -AutoSize"
            ),
            (
                "$files=Get-ChildItem -LiteralPath 'C:\\Users\\example\\.codex\\sessions\\2026\\07\\15' "
                "-File -Filter '*.jsonl' | Sort-Object LastWriteTime -Descending | Select-Object -First 8; "
                "foreach($f in $files){ [pscustomobject]@{Name=$f.Name;Bytes=$f.Length;"
                "Created=$f.CreationTime.ToString('o');Modified=$f.LastWriteTime.ToString('o')} } "
                "| Format-List"
            ),
            (
                "$files=@('C:\\Users\\example\\.codex\\skills\\.system\\openai-docs\\SKILL.md',"
                "'C:\\skill-tools\\codex-troubleshooting-skill-matrix\\SKILL.md'); "
                "foreach($f in $files){$lines=Get-Content -LiteralPath $f; "
                "[pscustomobject]@{Path=$f; Lines=$lines.Count}} | ConvertTo-Json"
            ),
        ]
        for command in historical_candidates:
            with self.subTest(command=command):
                self.assertIn("EmptyPipeElement", powershell_parser_error_ids(command))
                output = handle_event(self.event(command))
                candidate = output["hookSpecificOutput"]["updatedInput"]["command"]
                self.assertEqual([], powershell_parser_error_ids(candidate))

    def test_cli_stdout_is_one_protocol_object_or_empty(self) -> None:
        script = Path(__file__).resolve().parents[1] / "skills" / "embedded-harness" / "behavior_correction_hook.py"
        payload = self.event("Get-ChildItem -LiteralPath .")
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", str(script)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, completed.returncode)
        self.assertEqual("", completed.stdout)


if __name__ == "__main__":
    unittest.main()
