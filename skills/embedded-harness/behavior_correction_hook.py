#!/usr/bin/env python3
"""Stateless, nonblocking behavior-correction migration hook for Codex.

Only accepted deterministic profiles may rewrite the current Bash tool input.
No profile match, ambiguity, verifier failure, or runtime error leaves the
original event untouched. This hook never denies, freezes, persists state,
writes memory, mutates policy, or grants authority.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from execution_feedback import (
    CorrectionProfileRegistryError,
    apply_behavior_rewrite,
    derive_subject_binding,
    load_correction_profiles,
    matches_behavior_pattern,
)


Parser = Callable[[str], list[str] | None]

_PARSER_SCRIPT = r"""
$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$source = [Console]::In.ReadToEnd()
$tokens = $null
$errors = $null
[System.Management.Automation.Language.Parser]::ParseInput(
    $source,
    [ref]$tokens,
    [ref]$errors
) | Out-Null
$ids = @($errors | ForEach-Object { $_.ErrorId })
[Console]::Out.Write(($ids -join "`n"))
"""


def _powershell_executable() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def powershell_parser_error_ids(text: str) -> list[str] | None:
    executable = _powershell_executable()
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", _PARSER_SCRIPT],
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    if completed.returncode != 0:
        return None
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _event_name(payload: dict[str, Any]) -> str:
    return str(
        payload.get("hook_event_name") or payload.get("hookEventName") or ""
    ).casefold()


def _candidate_for_profile(
    profile: dict[str, Any],
    *,
    command: str,
    cwd: str,
    parser: Parser,
) -> str | None:
    migration = profile.get("runtime_migration")
    if not isinstance(migration, dict) or migration.get("status") != "accepted":
        return None
    if not matches_behavior_pattern(str(profile.get("match_kind") or ""), command):
        return None
    original_errors = parser(command)
    if original_errors is None or not original_errors:
        return None
    expected = {str(item) for item in migration["expected_parser_error_ids"]}
    observed = set(original_errors)
    if not observed.intersection(expected) or observed.difference(expected):
        return None
    candidate = apply_behavior_rewrite(profile, command)
    if not candidate or candidate == command or "\ufffd" in candidate:
        return None
    before_binding = derive_subject_binding(
        profile,
        text=command,
        target_binding_sha256="",
        execution_cwd=cwd,
    )
    after_binding = derive_subject_binding(
        profile,
        text=candidate,
        target_binding_sha256="",
        execution_cwd=cwd,
    )
    if before_binding != after_binding:
        return None
    candidate_errors = parser(candidate)
    if candidate_errors is None or candidate_errors:
        return None
    return candidate


def handle_event(
    payload: dict[str, Any],
    *,
    parser: Parser | None = None,
) -> dict[str, Any]:
    if _event_name(payload) != "pretooluse" or payload.get("tool_name") != "Bash":
        return {}
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return {}
    command = tool_input.get("command")
    if not isinstance(command, str) or not command or "\ufffd" in command:
        return {}
    cwd = str(Path(str(payload.get("cwd") or ".")).resolve(strict=False))
    parse = parser or powershell_parser_error_ids
    try:
        profiles = load_correction_profiles()
    except CorrectionProfileRegistryError:
        return {}
    ordered = sorted(
        profiles.values(),
        key=lambda item: (-int(item.get("priority") or 0), str(item["profile_id"])),
    )
    for profile in ordered:
        if profile.get("enforcement") != "rewrite_current_event_nonblocking":
            continue
        candidate = _candidate_for_profile(
            profile,
            command=command,
            cwd=cwd,
            parser=parse,
        )
        if candidate is None:
            continue
        updated_input = dict(tool_input)
        updated_input["command"] = candidate
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": updated_input,
            }
        }
    return {}


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
        output = handle_event(payload)
    except (TypeError, ValueError, UnicodeError):
        output = {}
    if output:
        sys.stdout.write(json.dumps(output, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
