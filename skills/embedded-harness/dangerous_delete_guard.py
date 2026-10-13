#!/usr/bin/env python3
"""On-demand advisory classifier for potentially dangerous deletion commands.

This module is deliberately not a Codex hook and never blocks an event. Host
sandboxing and agent/user authorization remain separate control planes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ADVISORY_SCHEMA = "cbh.dangerous_delete_advisory.v1"

DELETE_COMMANDS = {"remove-item", "rm", "del", "erase", "rd", "rmdir"}
SHELL_WRAPPERS = {"cmd", "powershell", "pwsh", "bash", "sh"}

POWERSHELL_AST_SCRIPT = r"""
$source = [Console]::In.ReadToEnd()
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput(
    $source,
    [ref]$tokens,
    [ref]$parseErrors
)
$records = @(
    $ast.FindAll(
        { param($node) $node -is [System.Management.Automation.Language.CommandAst] },
        $true
    ) | ForEach-Object {
        $pipelineCount = 1
        $inLoop = $false
        $parent = $_.Parent
        while ($null -ne $parent) {
            if ($parent -is [System.Management.Automation.Language.PipelineAst]) {
                $pipelineCount = [Math]::Max($pipelineCount, $parent.PipelineElements.Count)
            }
            $typeName = $parent.GetType().Name
            if ($typeName -in @(
                'ForEachStatementAst', 'ForStatementAst', 'WhileStatementAst',
                'DoWhileStatementAst', 'DoUntilStatementAst'
            )) {
                $inLoop = $true
            }
            $parent = $parent.Parent
        }
        [ordered]@{
            name = [string]$_.GetCommandName()
            elements = @($_.CommandElements | ForEach-Object { [string]$_.Extent.Text })
            pipeline_count = [int]$pipelineCount
            in_loop = [bool]$inLoop
        }
    }
)
[ordered]@{
    status = 'parsed'
    errors = @($parseErrors | ForEach-Object { [string]$_.Message })
    commands = $records
} | ConvertTo-Json -Depth 6 -Compress
""".strip()


def _event_name(payload: dict[str, Any]) -> str:
    raw = str(payload.get("hook_event_name") or payload.get("hookEventName") or "")
    return re.sub(r"[^a-z]", "", raw.lower())


def _normal_cwd(value: Any) -> Path:
    return Path(str(value or os.getcwd())).expanduser().resolve(strict=False)


def _command_name(value: Any) -> str:
    name = str(value or "").strip().strip("'\"").replace("\\", "/").rsplit("/", 1)[-1]
    lowered = name.lower()
    return lowered[:-4] if lowered.endswith(".exe") else lowered


def _parse_commands(command: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_AST_SCRIPT],
            input=command,
            text=True,
            encoding="utf-8",
            errors="strict",
            capture_output=True,
            timeout=5,
            check=False,
        )
        if completed.returncode != 0:
            return {"status": "unavailable", "errors": [f"pwsh_exit_{completed.returncode}"], "commands": []}
        parsed = json.loads(completed.stdout.lstrip("\ufeff"))
        return parsed if isinstance(parsed, dict) else {"status": "invalid", "errors": [], "commands": []}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, UnicodeError) as exc:
        return {"status": "unavailable", "errors": [type(exc).__name__], "commands": []}


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        inner = value[1:-1]
        return inner.replace("''", "'") if value[0] == "'" else inner.replace('`"', '"')
    return value


def _is_dry_run(elements: list[str], name: str) -> bool:
    lowered = [item.lower() for item in elements[1:]]
    if name in DELETE_COMMANDS or name == "clear-recyclebin":
        return any(item in {"-whatif", "-whatif:$true"} for item in lowered)
    if name == "git" and "clean" in lowered:
        return any(item in {"-n", "--dry-run"} or (item.startswith("-") and "n" in item[1:]) for item in lowered)
    if name == "robocopy":
        return "/l" in lowered
    return False


def _recursive_flag(elements: list[str], name: str) -> bool:
    for raw in elements[1:]:
        item = raw.lower()
        if item in {"-recurse", "-recurse:$true", "-r", "-r:$true", "--recursive", "/s"}:
            return True
        if name == "rm" and re.fullmatch(r"-[rf]+", item) and "r" in item:
            return True
    return False


def _extract_targets(elements: list[str]) -> tuple[list[str], list[str]]:
    targets: list[str] = []
    reasons: list[str] = []
    value_options = {"-path", "-literalpath"}
    broad_options = {"-filter", "-include", "-exclude"}
    skip_value_options = broad_options | {
        "-erroraction", "-warningaction", "-informationaction", "-progressaction",
        "-errorvariable", "-warningvariable", "-informationvariable", "-outvariable",
    }
    index = 1
    positional_mode = False
    while index < len(elements):
        raw = elements[index].strip()
        lowered = raw.lower()
        if lowered == "--":
            positional_mode = True
            index += 1
            continue
        inline = re.match(r"^-(?:literal)?path:(.+)$", raw, re.IGNORECASE)
        if inline:
            targets.append(inline.group(1))
            index += 1
            continue
        if lowered in value_options:
            if index + 1 < len(elements):
                targets.append(elements[index + 1])
                index += 2
            else:
                reasons.append("missing_target")
                index += 1
            continue
        if lowered in skip_value_options:
            if lowered in broad_options:
                reasons.append("filtered_or_spread_delete")
            index += 2
            continue
        if not positional_mode and (raw.startswith("-") or raw.startswith("/")):
            index += 1
            continue
        targets.append(raw)
        index += 1
    return targets, reasons


def _resolve_target(raw: str, cwd: Path) -> tuple[str, Path | None, str | None]:
    quoted = raw.strip()
    value = _unquote(quoted)
    if not value:
        return raw, None, "missing_target"
    if (
        "$" in value
        or "%" in value
        or "*" in value
        or "?" in value
        or "[" in value
        or value.startswith("@(")
        or value.startswith("$(")
        or bool(re.search(r"['\"]\s*,\s*['\"]", quoted))
        or ("," in quoted and not (len(quoted) >= 2 and quoted[0] == quoted[-1] and quoted[0] in {"'", '"'}))
    ):
        return value, None, "unresolved_or_spread_target"
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    resolved = candidate.resolve(strict=False)
    return str(resolved), resolved, None


def _strictly_inside(target: Path, cwd: Path) -> bool:
    try:
        relative = target.relative_to(cwd)
    except ValueError:
        return False
    return bool(relative.parts)


def _delete_record(record: dict[str, Any], cwd: Path, total_delete_commands: int) -> dict[str, Any] | None:
    name = _command_name(record.get("name"))
    elements = [str(item) for item in (record.get("elements") or [])]
    if not elements or _is_dry_run(elements, name):
        return None
    raw_targets, reasons = _extract_targets(elements)
    if _recursive_flag(elements, name):
        reasons.append("recursive_delete")
    if int(record.get("pipeline_count") or 1) > 1:
        reasons.append("pipeline_delete")
    if bool(record.get("in_loop")):
        reasons.append("loop_delete")
    if total_delete_commands > 1:
        reasons.append("multiple_delete_commands")
    targets: list[str] = []
    resolved_targets: list[Path] = []
    for raw in raw_targets:
        display, resolved, issue = _resolve_target(raw, cwd)
        targets.append(display)
        if issue:
            reasons.append(issue)
        elif resolved is not None:
            resolved_targets.append(resolved)
            if not _strictly_inside(resolved, cwd):
                reasons.append("target_not_strictly_inside_cwd")
    if not raw_targets:
        reasons.append("target_not_resolved")
    if len({str(item).lower() for item in resolved_targets}) + sum(1 for item in targets if item.startswith(("$", "%", "@(", "$("))) > 1:
        reasons.append("multiple_targets")
    unique_reasons = sorted(set(reasons))
    if not unique_reasons:
        return None
    return {"reasons": unique_reasons, "targets": targets or ["<unresolved>"]}


def _special_record(record: dict[str, Any]) -> dict[str, Any] | None:
    name = _command_name(record.get("name"))
    elements = [str(item) for item in (record.get("elements") or [])]
    lowered = [item.lower() for item in elements[1:]]
    if _is_dry_run(elements, name):
        return None
    if name == "git" and "clean" in lowered and any(item == "--force" or (item.startswith("-") and "f" in item[1:]) for item in lowered):
        return {"reasons": ["git_clean_force"], "targets": ["<git-worktree>"]}
    if name == "git" and "reset" in lowered and "--hard" in lowered:
        return {"reasons": ["git_reset_hard"], "targets": ["<git-worktree>"]}
    if name == "find" and "-delete" in lowered:
        return {"reasons": ["find_delete"], "targets": ["<find-selection>"]}
    if name == "rsync" and "--delete" in lowered:
        return {"reasons": ["rsync_delete"], "targets": ["<rsync-destination>"]}
    if name == "robocopy" and any(item in {"/mir", "/purge"} for item in lowered):
        return {"reasons": ["robocopy_mirror_or_purge"], "targets": ["<robocopy-destination>"]}
    if name == "clear-recyclebin":
        return {"reasons": ["permanent_recycle_bin_clear"], "targets": ["<recycle-bin>"]}
    return None


def _lexical_delete_candidate(command: str) -> bool:
    return bool(
        re.search(
            r"(?im)(?:^|[;|&\r\n]\s*)(?:remove-item|rm|del|erase|rd|rmdir|clear-recyclebin)\b"
            r"|(?:^|[;|&\r\n]\s*)git\s+(?:clean\b.*(?:-f|--force)|reset\b.*--hard)"
            r"|(?:^|[;|&\r\n]\s*)(?:find\b.*-delete|rsync\b.*--delete|robocopy\b.*(?:/mir|/purge))",
            command,
        )
    )


def _classify_command(command: str, cwd: Path, depth: int) -> dict[str, Any]:
    parsed = _parse_commands(command)
    records = [item for item in (parsed.get("commands") or []) if isinstance(item, dict)]
    delete_records = [item for item in records if _command_name(item.get("name")) in DELETE_COMMANDS]
    findings: list[dict[str, Any]] = []
    for record in delete_records:
        finding = _delete_record(record, cwd, len(delete_records))
        if finding:
            findings.append(finding)
    for record in records:
        finding = _special_record(record)
        if finding:
            findings.append(finding)
        name = _command_name(record.get("name"))
        elements = [str(item) for item in (record.get("elements") or [])]
        if depth < 2 and name in SHELL_WRAPPERS:
            lowered = [item.lower() for item in elements]
            flags = {"cmd": {"/c"}, "powershell": {"-command", "-c"}, "pwsh": {"-command", "-c"}, "bash": {"-c"}, "sh": {"-c"}}[name]
            flag_index = next((index for index, item in enumerate(lowered) if item in flags), None)
            if flag_index is not None and flag_index + 1 < len(elements):
                inner = _unquote(" ".join(elements[flag_index + 1 :]))
                nested = _classify_command(inner, cwd, depth + 1)
                if nested["dangerous"]:
                    findings.append(
                        {
                            "reasons": ["nested_shell_delete", *nested["reasons"]],
                            "targets": nested["targets"],
                        }
                    )
    if not findings and (parsed.get("status") != "parsed" or parsed.get("errors")) and _lexical_delete_candidate(command):
        findings.append({"reasons": ["delete_candidate_not_safely_parsed"], "targets": ["<unresolved>"]})
    reasons = sorted({reason for item in findings for reason in item["reasons"]})
    targets = list(dict.fromkeys(target for item in findings for target in item["targets"]))
    return {"dangerous": bool(findings), "reasons": reasons, "targets": targets}


def classify_command(command: str, cwd: Path) -> dict[str, Any]:
    return _classify_command(command, cwd, 0)


def advisory_receipt(command: str, cwd: Path) -> dict[str, Any]:
    finding = classify_command(command, cwd)
    return {
        "schema": ADVISORY_SCHEMA,
        "status": (
            "risk_shape_detected"
            if finding["dangerous"]
            else "no_dangerous_shape_detected"
        ),
        "blocking": False,
        "cwd": str(cwd),
        "reasons": finding["reasons"],
        "targets": finding["targets"],
        "evidence_boundary": (
            "local lexical and PowerShell-AST risk classification only; "
            "not authorization, denial, sandboxing, or proof of effect"
        ),
    }


def handle_event(payload: dict[str, Any], *, state_root: Path | None = None) -> dict[str, Any]:
    """Compatibility no-op: dangerous deletion is not a registered hook."""

    del payload, state_root
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="On-demand nonblocking deletion-risk advisor"
    )
    parser.add_argument("--command", default="")
    parser.add_argument("--cwd", default="")
    args = parser.parse_args()
    raw = sys.stdin.read()
    command = args.command
    cwd_value = args.cwd
    if not command and raw.strip():
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return 0
        if _event_name(payload):
            return 0
        command = str(payload.get("command") or "")
        cwd_value = str(payload.get("cwd") or cwd_value)
    if not command:
        return 0
    output = advisory_receipt(command, _normal_cwd(cwd_value))
    sys.stdout.write(json.dumps(output, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
