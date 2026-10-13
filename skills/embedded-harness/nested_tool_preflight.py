"""Typed, advisory preflight for tools nested inside ``functions.exec``.

Host interception depends on the actual nested dispatch surface. This module
evaluates one explicitly selected call directly. It never parses the surrounding
JavaScript, blocks the host, grants authority, writes memory, or persists state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from behavior_correction_gate import build_behavior_correction_receipt
from behavior_correction_hook import handle_event, powershell_parser_error_ids
from task_continuity import page_result


SCHEMA = "cbh.nested_tool_preflight_receipt.v1"

_DECISION_RANK = {
    "allow": 0,
    "rewrite_candidate": 1,
    "validation_required": 2,
    "semantic_review_required": 3,
}

_SHELL_FIELDS = {
    "command",
    "workdir",
    "timeout_ms",
    "sandbox_permissions",
    "justification",
    "prefix_rule",
    "login",
}

_EXEC_FIELDS = (_SHELL_FIELDS - {"command", "timeout_ms"}) | {
    "cmd", "shell", "tty", "max_output_tokens", "yield_time_ms",
}

_WEB_FIELDS = {
    "search_query",
    "image_query",
    "open",
    "click",
    "find",
    "screenshot",
    "finance",
    "weather",
    "sports",
    "time",
    "response_length",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _promote(current: str, candidate: str) -> str:
    if _DECISION_RANK[candidate] > _DECISION_RANK[current]:
        return candidate
    return current


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _schema_issue(reasons: list[str], reason: str) -> None:
    _append_reason(reasons, reason)


def _validate_shell_arguments(arguments: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    unknown = sorted(set(arguments) - _SHELL_FIELDS)
    if unknown:
        _schema_issue(issues, "shell_command_unknown_argument_fields")
    command = arguments.get("command")
    if not isinstance(command, str) or not command:
        _schema_issue(issues, "shell_command_command_must_be_nonempty_string")
    if "workdir" in arguments and not isinstance(arguments["workdir"], str):
        _schema_issue(issues, "shell_command_workdir_must_be_string")
    if "justification" in arguments and not isinstance(arguments["justification"], str):
        _schema_issue(issues, "shell_command_justification_must_be_string")
    if "timeout_ms" in arguments:
        timeout = arguments["timeout_ms"]
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            _schema_issue(issues, "shell_command_timeout_must_be_positive_integer")
    if "login" in arguments and not isinstance(arguments["login"], bool):
        _schema_issue(issues, "shell_command_login_must_be_boolean")
    if "sandbox_permissions" in arguments:
        sandbox_permissions = arguments["sandbox_permissions"]
        if not isinstance(sandbox_permissions, str) or sandbox_permissions not in (
            "use_default",
            "require_escalated",
        ):
            _schema_issue(issues, "shell_command_invalid_sandbox_permissions")
    if "prefix_rule" in arguments:
        prefix = arguments["prefix_rule"]
        if not isinstance(prefix, list) or not all(isinstance(item, str) for item in prefix):
            _schema_issue(issues, "shell_command_prefix_rule_must_be_string_array")
        if arguments.get("sandbox_permissions") != "require_escalated":
            _schema_issue(issues, "shell_command_prefix_rule_requires_escalation")
    return issues


def _validate_exec_arguments(arguments: Mapping[str, Any]) -> list[str]:
    # Share common field validation without changing the actual dispatch schema.
    common = {key: value for key, value in arguments.items() if key in _SHELL_FIELDS - {"command", "timeout_ms"}}
    issues = _validate_shell_arguments({**common, "command": arguments.get("cmd")})
    issues = [issue.replace("shell_command_", "exec_command_") for issue in issues]
    if set(arguments) - _EXEC_FIELDS:
        issues.append("exec_command_unknown_argument_fields")
    if "shell" in arguments and not isinstance(arguments["shell"], str):
        issues.append("exec_command_shell_must_be_string")
    if "tty" in arguments and not isinstance(arguments["tty"], bool):
        issues.append("exec_command_tty_must_be_boolean")
    for field in ("max_output_tokens", "yield_time_ms"):
        if field in arguments and (isinstance(arguments[field], bool) or not isinstance(arguments[field], int)):
            issues.append(f"exec_command_{field}_must_be_integer")
    return issues


def _uses_powershell(tool_name: str, arguments: Mapping[str, Any]) -> bool:
    if tool_name == "shell_command" or not arguments.get("shell"):
        return True  # This local adapter's default shell is PowerShell.
    executable = str(arguments["shell"]).replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return executable in {"pwsh", "pwsh.exe", "powershell", "powershell.exe"}


def _is_integer(value: Any, *, minimum: int | None = None) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return minimum is None or value >= minimum


def _validate_string_array(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _matches_web_kind(value: Any, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "nonempty_string":
        return isinstance(value, str) and bool(value)
    if kind == "string_array":
        return _validate_string_array(value)
    if kind == "integer":
        return _is_integer(value)
    if kind == "positive_integer":
        return _is_integer(value, minimum=1)
    if kind == "nonnegative_integer":
        return _is_integer(value, minimum=0)
    if kind == "finance_type":
        return isinstance(value, str) and value in {"equity", "fund", "crypto", "index"}
    if kind == "sports_fn":
        return isinstance(value, str) and value in {"schedule", "standings"}
    if kind == "sports_league":
        return isinstance(value, str) and value in {"nba", "wnba", "nfl", "nhl", "mlb", "epl", "ncaamb", "ncaawb", "ipl"}
    if kind == "sports_tool":
        return value == "sports"
    if kind == "utc_offset":
        return isinstance(value, str) and bool(re.fullmatch(r"[+-]\d{2}:\d{2}", value))
    raise ValueError(f"unknown web schema kind: {kind}")


def _validate_web_item(
    field: str,
    item: Mapping[str, Any],
    issues: list[str],
) -> None:
    schemas: dict[str, tuple[set[str], dict[str, str], dict[str, str]]] = {
        "search_query": (
            {"q", "domains", "recency"},
            {"q": "nonempty_string"},
            {"domains": "string_array", "recency": "positive_integer"},
        ),
        "image_query": (
            {"q", "domains", "recency"},
            {"q": "nonempty_string"},
            {"domains": "string_array", "recency": "positive_integer"},
        ),
        "click": ({"ref_id", "id"}, {"ref_id": "nonempty_string", "id": "integer"}, {}),
        "find": ({"ref_id", "pattern"}, {"ref_id": "nonempty_string", "pattern": "nonempty_string"}, {}),
        "screenshot": ({"ref_id", "pageno"}, {"ref_id": "nonempty_string", "pageno": "nonnegative_integer"}, {}),
        "finance": (
            {"ticker", "type", "market"},
            {"ticker": "nonempty_string", "type": "finance_type"},
            {"market": "string"},
        ),
        "weather": (
            {"location", "duration", "start"},
            {"location": "nonempty_string"},
            {"duration": "positive_integer", "start": "string"},
        ),
        "sports": (
            {"fn", "league", "date_from", "date_to", "locale", "num_games", "opponent", "team", "tool"},
            {"fn": "sports_fn", "league": "sports_league"},
            {
                "date_from": "string", "date_to": "string", "locale": "string",
                "num_games": "positive_integer", "opponent": "string", "team": "string",
                "tool": "sports_tool",
            },
        ),
        "time": ({"utc_offset"}, {"utc_offset": "utc_offset"}, {}),
    }
    if field not in schemas:
        return
    allowed, required, optional = schemas[field]
    if set(item) - allowed:
        _schema_issue(issues, f"web_run_{field}_item_unknown_fields")
    for name in required:
        if name not in item:
            _schema_issue(issues, f"web_run_{field}_{name}_required")
    checks = {**required, **optional}
    for name, kind in checks.items():
        if name not in item:
            continue
        value = item[name]
        valid = _matches_web_kind(value, kind)
        if not valid:
            _schema_issue(issues, f"web_run_{field}_{name}_invalid")


def _validate_web_arguments(arguments: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    unknown = sorted(set(arguments) - _WEB_FIELDS)
    if unknown:
        _schema_issue(issues, "web_run_unknown_argument_fields")
    if "response_length" in arguments:
        response_length = arguments["response_length"]
        if not isinstance(response_length, str) or response_length not in (
            "short",
            "medium",
            "long",
        ):
            _schema_issue(issues, "web_run_invalid_response_length")
    if "open" in arguments:
        opened = arguments["open"]
        if not isinstance(opened, list):
            _schema_issue(issues, "web_run_open_must_be_array")
        else:
            if not opened:
                _schema_issue(issues, "web_run_open_must_not_be_empty")
            for item in opened:
                if not isinstance(item, Mapping):
                    _schema_issue(issues, "web_run_open_item_must_be_object")
                    continue
                if set(item) - {"ref_id", "lineno"}:
                    _schema_issue(issues, "web_run_open_item_unknown_fields")
                if not isinstance(item.get("ref_id"), str):
                    _schema_issue(issues, "web_run_open_ref_id_must_be_string")
                if "lineno" in item:
                    line = item["lineno"]
                    if isinstance(line, bool) or not isinstance(line, int):
                        _schema_issue(issues, "web_run_open_lineno_must_be_integer")
    for field in _WEB_FIELDS - {"response_length", "open"}:
        if field not in arguments:
            continue
        value = arguments[field]
        if not isinstance(value, list):
            _schema_issue(issues, f"web_run_{field}_must_be_array")
            continue
        if not value:
            _schema_issue(issues, f"web_run_{field}_must_not_be_empty")
        for item in value:
            if not isinstance(item, Mapping):
                _schema_issue(issues, f"web_run_{field}_item_must_be_object")
                continue
            _validate_web_item(field, item, issues)
    queries = arguments.get("search_query")
    if isinstance(queries, list):
        if len(queries) > 4:
            _schema_issue(issues, "web_run_search_query_max_four")
        if len(queries) > 3 and arguments.get("response_length") not in ("medium", "long"):
            _schema_issue(issues, "web_run_four_queries_require_medium_or_long")
    return issues


def _direct_github_api_open(arguments: Mapping[str, Any]) -> bool:
    opened = arguments.get("open")
    if not isinstance(opened, list):
        return False
    for item in opened:
        if not isinstance(item, Mapping):
            continue
        ref = item.get("ref_id")
        if not isinstance(ref, str):
            continue
        try:
            if urlsplit(ref).hostname == "api.github.com":
                return True
        except ValueError:
            continue
    return False


def _web_operation_count(arguments: Mapping[str, Any]) -> int:
    total = 0
    for key in _WEB_FIELDS - {"response_length"}:
        value = arguments.get(key)
        if isinstance(value, list):
            total += len(value)
    return total


def _tokenize_powershell_command(command: str) -> list[str]:
    return re.findall(r"'[^']*'|\"[^\"]*\"|\S+", command)


def _has_literal_rg_wildcard_path(command: str) -> bool:
    if not re.search(r"(?i)(?:^|[;|\s])rg(?:\.exe)?(?:\s|$)", command):
        return False
    tokens = _tokenize_powershell_command(command)
    try:
        start = next(
            index
            for index, raw in enumerate(tokens)
            if raw.strip("'\"").casefold() in {"rg", "rg.exe"}
        )
    except StopIteration:
        return False
    short_value_options = {"-e", "-g", "-f", "-t", "-m", "-A", "-B", "-C"}
    long_value_options = {
        "--regexp", "--glob", "--iglob", "--file", "--type", "--max-count",
        "--after-context", "--before-context", "--context", "--threads",
        "--encoding", "--engine", "--replace", "--sort", "--sortr",
        "--path-separator", "--max-columns", "--type-add", "--type-clear",
    }
    explicit_pattern = False
    positionals: list[str] = []
    skip_value = False
    options_ended = False
    for raw in tokens[start + 1 :]:
        token = raw.strip("'\"")
        lowered = token.casefold()
        if token in {"|", ";"}:
            break
        if skip_value:
            skip_value = False
            continue
        if not options_ended and token == "--":
            options_ended = True
            continue
        if not options_ended and token in short_value_options:
            explicit_pattern = explicit_pattern or token == "-e"
            skip_value = True
            continue
        if not options_ended and lowered in long_value_options:
            explicit_pattern = explicit_pattern or lowered == "--regexp"
            skip_value = True
            continue
        if not options_ended and token.startswith("--") and "=" in token:
            option = token.split("=", 1)[0].casefold()
            if option in long_value_options:
                explicit_pattern = explicit_pattern or option == "--regexp"
                continue
        if not options_ended and len(token) > 2 and token[:2] in short_value_options:
            explicit_pattern = explicit_pattern or token[:2] == "-e"
            continue
        if not options_ended and token.startswith("-"):
            continue
        positionals.append(token)
    path_operands = positionals if explicit_pattern else positionals[1:]
    for token in path_operands:
        if "*" not in token and "?" not in token:
            continue
        looks_pathlike = (
            "\\" in token
            or "/" in token
            or token.casefold().startswith("readme")
            or bool(re.search(r"\.[a-z0-9]+[*?]?$", token, re.IGNORECASE))
        )
        if looks_pathlike:
            return True
    return False


def _get_content_invocations(command: str) -> list[str]:
    return [
        match.group(0)
        for match in re.finditer(r"(?is)\bGet-Content\b[^;|]*", command)
    ]


def _literal_path_from_get_content(invocation: str) -> str | None:
    match = re.search(r"(?i)-LiteralPath\s+(['\"])(.*?)\1", invocation)
    return match.group(2) if match else None


def _get_content_has_literal_source_budget(invocation: str) -> bool:
    for match in re.finditer(
        r"(?i)(?:-(?:Tail|Last|TotalCount|First|Head))\s+([^\s;|]+)",
        invocation,
    ):
        value = match.group(1).strip("'\"")
        if re.fullmatch(r"\d+", value):
            return True
    return False


def _exact_literal_read_findings(command: str) -> list[str]:
    findings: list[str] = []
    for invocation in _get_content_invocations(command):
        raw_path = _literal_path_from_get_content(invocation)
        if not raw_path or "$" in raw_path:
            continue
        target = Path(raw_path)
        if not target.exists():
            _append_reason(findings, "exact_read_target_missing")
            continue
        if (
            target.is_file()
            and target.stat().st_size > 256 * 1024
            and not _get_content_has_literal_source_budget(invocation)
        ):
            _append_reason(findings, "large_exact_read_requires_budget")
    return findings


def _known_shell_findings(command: str) -> list[str]:
    findings: list[str] = []
    if _has_literal_rg_wildcard_path(command):
        findings.append("windows_rg_literal_wildcard_path")
    if (
        re.search(r"(?i)\bInvoke-RestMethod\b", command)
        and re.search(r"(?i)\bWhere-Object\b", command)
        and re.search(r"(?i)\[datetime\]\s*\(?\s*\$_\.published_at", command)
    ):
        findings.append("powershell_irm_runtime_cardinality_probe_required")
    if re.search(r"(?i)(?:-f|--raw-field)\s+\w+=\$_\.\w+", command):
        findings.append("powershell_native_argument_property_interpolation_required")
    if re.search(r"(?i)git/trees/\$[a-z_][a-z0-9_]*\?", command):
        findings.append("powershell_uri_variable_boundary_required")
    if (
        re.search(r"(?i)\bgh(?:\.exe)?\s+api\b", command)
        and "--slurp" in command
        and ("--jq" in command or "-q " in command)
    ):
        findings.append("cli_option_combination_validation_required")
    normalized = command.replace("/", "\\").casefold()
    for invocation in _get_content_invocations(command):
        raw_path = _literal_path_from_get_content(invocation)
        normalized_path = (raw_path or "").replace("/", "\\").casefold()
        if (
            ".codex\\sessions" in normalized_path
            and "rollout-" in normalized_path
            and normalized_path.endswith(".jsonl")
            and (
                re.search(r"(?i)-Raw\b", invocation)
                or not _get_content_has_literal_source_budget(invocation)
            )
        ):
            _append_reason(findings, "active_rollout_read_requires_bounded_get_content")
    if (
        ".codex\\sessions" in normalized
        and re.search(r"(?i)(?:-Recurse\b|\brg\b)", command)
        and not re.search(r"(?i)(?:Select-Object\s+-First\b|-Tail\b)", command)
    ):
        findings.append("broad_output_scope_review_required")
    findings.extend(_exact_literal_read_findings(command))
    return findings


def _normalize_envelope(candidate: Mapping[str, Any]) -> tuple[str, dict[str, Any], str | None, str | None]:
    tool_name = candidate.get("tool_name")
    arguments = candidate.get("arguments")
    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("tool_name_must_be_nonempty_string")
    if not isinstance(arguments, Mapping):
        raise ValueError("arguments_must_be_object")
    normalized_arguments = json.loads(_canonical_json(dict(arguments)))
    agent_path = candidate.get("agent_path")
    previous = candidate.get("previous_failure_signature")
    if agent_path is not None and not isinstance(agent_path, str):
        raise ValueError("agent_path_must_be_string")
    if previous is not None and (
        not isinstance(previous, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", previous)
    ):
        raise ValueError("previous_failure_signature_must_be_sha256")
    return tool_name, normalized_arguments, agent_path, previous.casefold() if previous else None


def _shell_carrier(command: str) -> str:
    """Classify the command carrier before any shell/runtime-specific analysis.

    Inline source passed across functions.exec -> PowerShell -> another runtime has
    repeatedly failed before the nested preflight could protect it.  Keep such
    payloads file-backed; a short ``-File`` dispatch is the reusable safe carrier.
    """

    if re.search(r"(?is)\bnode(?:\.exe)?\b.*?(?:^|\s)(?:-e|--eval)\b", command):
        return "inline_multi_runtime"
    if re.search(r"(?is)\b(?:python|python3|py)(?:\.exe)?\b.*?(?:^|\s)-c\b", command):
        return "inline_multi_runtime"
    if re.search(r"(?is)\b(?:pwsh|powershell)(?:\.exe)?\b.*?(?:^|\s)-(?:Command|EncodedCommand)\b", command):
        return "inline_multi_runtime"
    if re.search(r"(?i)\b(?:pwsh|powershell)(?:\.exe)?\b[^\r\n]*\s-File\s+", command):
        return "file_backed_script"
    if re.search(r"(?i)\b(?:python|python3|py)(?:\.exe)?\b\s+(?!-)[^\s]+\.py\b", command):
        return "file_backed_script"
    if re.search(r"(?i)\bnode(?:\.exe)?\b\s+(?!-)[^\s]+\.(?:mjs|cjs|js)\b", command):
        return "file_backed_script"
    return "direct_shell"


def preflight_nested_tool_call(
    candidate: Mapping[str, Any],
    *,
    tool_schema_validator: Callable[[str, Mapping[str, Any]], list[str]] | None = None,
    cli_validator: Callable[[str], list[str]] | None = None,
) -> dict[str, Any]:
    """Return one hash-bound advisory receipt for an exact nested tool call."""

    reasons: list[str] = []
    decision = "allow"
    behavior_receipt: dict[str, Any] | None = None
    try:
        if not isinstance(candidate, Mapping):
            raise ValueError("candidate_must_be_object")
        tool_name, arguments, agent_path, previous = _normalize_envelope(candidate)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        reason = str(exc) or "malformed_nested_tool_envelope"
        return {
            "schema": SCHEMA,
            "decision": "semantic_review_required",
            "normalized_arguments": {},
            "signature": None,
            "verifier": "typed_envelope_schema_review",
            "reasons": [reason],
            "agent_path": None,
            "output_budget": {"max_chars": 20000, "max_items": 100},
            "forward_contract": "string_or_typed_content_or_canonical_json",
            "same_candidate": False,
            "host_blocking": False,
            "hook_intercepted": False,
            "summary_is_navigation_only": True,
        }

    source_signature = _sha256_text(
        _canonical_json({"tool_name": tool_name, "arguments": arguments})
    )
    dispatch_arguments = dict(arguments)
    rewrite_applied = False
    carrier: str | None = None

    if tool_name == "shell_command":
        schema_issues = _validate_shell_arguments(arguments)
    elif tool_name == "exec_command":
        schema_issues = _validate_exec_arguments(arguments)
    elif tool_name == "web__run":
        schema_issues = _validate_web_arguments(arguments)
    else:
        schema_issues = ["unknown_nested_tool_schema"]
    if tool_schema_validator is not None:
        schema_issues.extend(tool_schema_validator(tool_name, arguments) or [])
    for issue in schema_issues:
        _append_reason(reasons, issue)
    if schema_issues:
        decision = _promote(decision, "semantic_review_required")

    if tool_name == "web__run" and not schema_issues:
        if _direct_github_api_open(arguments):
            _append_reason(reasons, "web_open_api_github_known_unsafe_nonretryable")
            decision = _promote(decision, "validation_required")
        if arguments.get("response_length") == "long" and _web_operation_count(arguments) >= 4:
            _append_reason(reasons, "broad_output_scope_review_required")
            decision = _promote(decision, "validation_required")

    is_shell = tool_name in {"shell_command", "exec_command"}
    if is_shell and not schema_issues and not _uses_powershell(tool_name, arguments):
        _append_reason(reasons, "shell_parser_not_covered")
        decision = _promote(decision, "validation_required")

    if is_shell and not schema_issues and _uses_powershell(tool_name, arguments):
        command_key = "cmd" if tool_name == "exec_command" else "command"
        command = str(arguments[command_key])
        carrier = _shell_carrier(command)
        if carrier == "inline_multi_runtime":
            _append_reason(reasons, "file_backed_carrier_required")
            decision = _promote(decision, "validation_required")
        parser_errors = powershell_parser_error_ids(command)
        if parser_errors is None:
            _append_reason(reasons, "powershell_parser_unavailable")
            decision = _promote(decision, "validation_required")
        if parser_errors:
            _append_reason(reasons, "powershell_parser_rejection")
        behavior_receipt = build_behavior_correction_receipt(
            stage="pretool",
            environment="powershell",
            tool_role="unknown",
            tool_surface=f"functions.exec_nested_{tool_name}",
            text=command,
            parser_error_ids=parser_errors or (),
            execution_cwd=str(arguments.get("workdir") or ""),
        )
        mapped = {
            "rewrite_candidate": "rewrite_candidate",
            "validation_required": "validation_required",
            "semantic_review_required": "semantic_review_required",
        }.get(str(behavior_receipt.get("decision")))
        if mapped == "rewrite_candidate":
            hook_output = handle_event(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "cwd": str(arguments.get("workdir") or "."),
                    "tool_input": {"command": command},
                }
            )
            updated = (
                hook_output.get("hookSpecificOutput", {}).get("updatedInput")
                if isinstance(hook_output, Mapping)
                else None
            )
            if isinstance(updated, Mapping) and isinstance(updated.get("command"), str):
                dispatch_arguments = {**arguments, command_key: updated["command"]}
                rewrite_applied = True
                decision = _promote(decision, "rewrite_candidate")
                _append_reason(reasons, "verified_current_event_rewrite")
        if mapped and not rewrite_applied:
            decision = _promote(decision, mapped)
            _append_reason(reasons, "behavior_correction_profile_match")
        if parser_errors and not rewrite_applied:
            decision = _promote(decision, "validation_required")
        for finding in _known_shell_findings(command):
            _append_reason(reasons, finding)
            decision = _promote(decision, "validation_required")
        if cli_validator is not None:
            for finding in cli_validator(command) or []:
                _append_reason(reasons, finding)
                decision = _promote(decision, "validation_required")

    dispatch_signature = _sha256_text(
        _canonical_json({"tool_name": tool_name, "arguments": dispatch_arguments})
    )
    if previous == dispatch_signature:
        matched_signature_kind = "dispatch"
    elif previous == source_signature:
        matched_signature_kind = "source"
    else:
        matched_signature_kind = None
    same_candidate = matched_signature_kind == "dispatch"
    same_source_candidate = previous == source_signature if previous else False
    if same_candidate:
        _append_reason(reasons, "unchanged_dispatch_after_failure")
        decision = _promote(decision, "validation_required")

    verifier = reasons[0] if reasons else "typed_schema_and_known_failure_shapes_passed"
    receipt = {
        "schema": SCHEMA,
        "decision": decision,
        "tool_name": tool_name,
        "normalized_arguments": dispatch_arguments,
        "signature": dispatch_signature,
        "source_signature": source_signature,
        "dispatch_signature": dispatch_signature,
        "verifier": verifier,
        "reasons": reasons,
        "agent_path": agent_path,
        "output_budget": {
            "max_chars": 20000,
            "max_items": 100,
            "truncation_requires_hash_and_uncovered_statement": True,
        },
        "forward_contract": "string_or_typed_content_or_canonical_json",
        "same_candidate": same_candidate,
        "same_source_candidate": same_source_candidate,
        "matched_signature_kind": matched_signature_kind,
        "rewrite_applied": rewrite_applied,
        "carrier": carrier,
        "host_blocking": False,
        "hook_intercepted": False,
        "summary_is_navigation_only": True,
    }
    if behavior_receipt and behavior_receipt.get("status") != "pass":
        receipt["behavior_correction_receipt"] = behavior_receipt
    return receipt


def _bounded_text(text: str, max_chars: int) -> dict[str, Any]:
    digest = _sha256_text(text)
    if len(text) <= max_chars:
        return {
            "forwarded_text": text,
            "truncated": False,
            "original_chars": len(text),
            "sha256": digest,
        }
    return {
        "forwarded_text": text[:max_chars],
        "truncated": True,
        "original_chars": len(text),
        "sha256": digest,
        "uncovered": f"{len(text) - max_chars} trailing characters were not forwarded",
    }


def _chunk_transport_text_item(
    text: str, *, source_item_index: int, max_chars: int
) -> list[dict[str, Any]]:
    plain = {"type": "text", "text": text}
    if len(_canonical_json(plain)) <= max_chars:
        return [plain]
    digest = _sha256_text(text)
    chunks: list[dict[str, Any]] = []
    offset = 0
    part = 0
    while offset < len(text):
        take = max(1, max_chars // 2)
        item: dict[str, Any]
        while True:
            item = {
                "type": "text",
                "text": text[offset : offset + take],
                "source_item_index": source_item_index,
                "part_index": part,
                "text_offset": offset,
                "full_text_sha256": digest,
            }
            if len(_canonical_json(item)) <= max_chars or take == 1:
                break
            take = max(1, take // 2)
        if len(_canonical_json(item)) > max_chars:
            raise ValueError("text_item_metadata_exceeds_transport_char_limit")
        chunks.append(item)
        offset += len(item["text"])
        part += 1
    return chunks


def _transport_safe_result(result: Any, *, max_chars: int) -> Any:
    """Build the complete text-safe representation before any paging occurs."""

    if isinstance(result, Mapping) and isinstance(result.get("content"), list):
        items: list[dict[str, Any]] = []
        for source_item_index, block in enumerate(result["content"]):
            if not isinstance(block, Mapping):
                items.append({"type": "unknown", "forward": "semantic_review"})
                continue
            block_type, type_review = _bounded_metadata(block.get("type"), 80)
            if not block_type:
                block_type = "unknown"
                type_review = True
            if block_type == "text":
                items.extend(
                    _chunk_transport_text_item(
                        str(block.get("text") or ""),
                        source_item_index=source_item_index,
                        max_chars=max_chars,
                    )
                )
                continue
            elif block_type in {"image", "audio"}:
                mime_type, mime_review = _bounded_metadata(block.get("mimeType"), 160)
                item = {
                    "type": block_type,
                    "mimeType": mime_type,
                    "forward": "typed_only",
                    "inline_payload_omitted_from_text": True,
                }
                type_review = type_review or mime_review
            else:
                redacted, omitted = _redact_inline_payloads(block)
                item = dict(redacted) if isinstance(redacted, Mapping) else {
                    "type": block_type,
                    "forward": "semantic_review",
                }
                if omitted:
                    item["inline_payloads_omitted_from_text"] = omitted
            if type_review:
                item["metadata_review_required"] = True
            items.append(item)
        return items
    redacted, _ = _redact_inline_payloads(result)
    return redacted


def normalize_nested_tool_result(
    result: Any,
    *,
    max_chars: int = 20000,
    max_items: int = 100,
    transport_plan: Mapping[str, Any] | None = None,
    cursor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize nested results without dropping strings or stringifying media data."""

    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or max_chars <= 0
        or isinstance(max_items, bool)
        or not isinstance(max_items, int)
        or max_items <= 0
    ):
        raise ValueError("result budgets must be positive integers")
    if transport_plan is not None:
        max_transport_chars = transport_plan.get("max_chars")
        if (
            isinstance(max_transport_chars, bool)
            or not isinstance(max_transport_chars, int)
            or max_transport_chars <= 0
        ):
            raise ValueError("transport plan max_chars must be a positive integer")
        page = page_result(
            _transport_safe_result(result, max_chars=max_transport_chars),
            transport_plan,
            cursor,
        )
        return {
            "kind": "transport_page",
            "transport_page": page,
            "source_sha256": page["full_result_sha256"],
            "inline_payload_present": False,
        }
    if result is None:
        return {"kind": "empty", "truncated": False, "original_items": 0}
    if isinstance(result, str):
        return {"kind": "text", **_bounded_text(result, max_chars)}
    if isinstance(result, Mapping) and result.get("type") in {"image", "audio"}:
        mime_type, mime_review = _bounded_metadata(result.get("mimeType"), 160)
        normalized_media = {
            "kind": "typed_media",
            "type": result.get("type"),
            "mimeType": mime_type,
            "forward": "typed_only",
            "inline_payload_omitted_from_text": True,
            "truncated": False,
            "original_items": 1,
            "sha256": _sha256_text(_canonical_json(result)),
        }
        if mime_review:
            normalized_media["metadata_review_required"] = True
        return normalized_media
    if isinstance(result, Mapping) and isinstance(result.get("content"), list):
        blocks = result["content"]
        items: list[dict[str, Any]] = []
        remaining = max_chars
        original_chars = sum(
            len(str(block.get("text") or ""))
            for block in blocks
            if isinstance(block, Mapping) and block.get("type") == "text"
        )
        forwarded_chars = 0
        metadata_review_required = False
        for block in blocks[:max_items]:
            if not isinstance(block, Mapping):
                items.append({"type": "unknown", "forward": "semantic_review"})
                continue
            block_type, type_review = _bounded_metadata(block.get("type"), 80)
            if not block_type:
                block_type = "unknown"
                type_review = True
            metadata_review_required = metadata_review_required or type_review
            if block_type == "text":
                text = str(block.get("text") or "")
                bounded = _bounded_text(text, remaining)
                remaining = max(0, remaining - len(bounded["forwarded_text"]))
                forwarded_chars += len(bounded["forwarded_text"])
                items.append({"type": "text", "text": bounded["forwarded_text"], **{k: v for k, v in bounded.items() if k != "forwarded_text"}})
            elif block_type in {"image", "audio"}:
                mime_type, mime_review = _bounded_metadata(block.get("mimeType"), 160)
                metadata_review_required = metadata_review_required or mime_review
                item = {
                    "type": block_type,
                    "mimeType": mime_type,
                    "forward": "typed_only",
                    "inline_payload_omitted_from_text": True,
                }
                if type_review or mime_review:
                    item["metadata_review_required"] = True
                items.append(item)
            else:
                item = {"type": block_type, "forward": "typed_or_semantic_review"}
                if type_review:
                    item["metadata_review_required"] = True
                items.append(item)
        uncovered_chars = max(0, original_chars - forwarded_chars)
        truncated = (
            len(blocks) > max_items
            or uncovered_chars > 0
            or any(item.get("truncated") for item in items)
        )
        normalized = {
            "kind": "typed_content",
            "items": items,
            "truncated": bool(truncated),
            "original_items": len(blocks),
            "original_chars": original_chars,
            "forwarded_chars": forwarded_chars,
            "uncovered_chars": uncovered_chars,
            "sha256": _sha256_text(_canonical_json(result)),
        }
        uncovered: list[str] = []
        if uncovered_chars:
            uncovered.append(f"{uncovered_chars} text characters were not forwarded")
        if len(blocks) > max_items:
            uncovered.append(f"{len(blocks) - max_items} trailing content blocks were not forwarded")
        if uncovered:
            normalized["uncovered"] = "; ".join(uncovered)
        if metadata_review_required:
            normalized["metadata_review_required"] = True
        return normalized
    redacted, omitted = _redact_inline_payloads(result)
    canonical = _canonical_json(redacted)
    normalized = {"kind": "canonical_json", **_bounded_text(canonical, max_chars)}
    normalized["sha256"] = _sha256_text(_canonical_json(result))
    if omitted:
        normalized["inline_payloads_omitted_from_text"] = omitted
    return normalized


def _bounded_metadata(value: Any, max_chars: int) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    if not isinstance(value, str):
        return None, True
    bounded = _bounded_text(value, max_chars)
    return bounded["forwarded_text"], bool(bounded["truncated"])


def _redact_inline_payloads(value: Any) -> tuple[Any, int]:
    omitted = 0
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        typed_media = value.get("type") in {"image", "audio"}
        for key, child in value.items():
            lowered = str(key).casefold()
            inline = isinstance(child, str) and (
                child.startswith("data:")
                or lowered == "blob"
                or (lowered == "data" and (typed_media or len(child) > 256))
            )
            if inline:
                out[str(key)] = "[inline payload omitted]"
                omitted += 1
            else:
                out[str(key)], child_omitted = _redact_inline_payloads(child)
                omitted += child_omitted
        return out, omitted
    if isinstance(value, list):
        out_list = []
        for child in value:
            cleaned, child_omitted = _redact_inline_payloads(child)
            out_list.append(cleaned)
            omitted += child_omitted
        return out_list, omitted
    return value, 0


def summarize_nested_tool_failure(
    *,
    agent_path: str,
    tool_name: str,
    signature: str,
    error_class: str,
    raw_ref: str | None,
    side_effects: str,
    recovered: bool,
) -> dict[str, Any]:
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise ValueError("signature must be a canonical lowercase SHA-256 hex digest")
    if not isinstance(recovered, bool):
        raise ValueError("recovered must be a boolean")
    agent = _bounded_text(agent_path, 160)
    tool = _bounded_text(tool_name, 80)
    error = _bounded_text(error_class, 160)
    effects = _bounded_text(side_effects, 240)
    raw_ref_sha256 = _raw_ref_sha256(raw_ref) if raw_ref else None
    row = {
        "agent_path": agent["forwarded_text"],
        "tool_name": tool["forwarded_text"],
        "signature": signature,
        "error_class": error["forwarded_text"],
        "raw_ref": raw_ref,
        "side_effects": effects["forwarded_text"],
        "recovered": recovered,
        "evidence_status": "raw_log_bound" if raw_ref_sha256 else "semantic_review_required",
        "summary_is_navigation_only": True,
        "raw_log_is_canonical": bool(raw_ref_sha256),
    }
    if raw_ref_sha256:
        row["raw_ref_sha256"] = raw_ref_sha256
    else:
        row["evidence_issue"] = "raw_ref_unverified" if raw_ref else "raw_ref_missing"
    for name, bounded in {
        "agent_path": agent,
        "tool_name": tool,
        "error_class": error,
        "side_effects": effects,
    }.items():
        row[f"{name}_sha256"] = bounded["sha256"]
        row[f"{name}_truncated"] = bounded["truncated"]
        if bounded.get("uncovered"):
            row[f"{name}_uncovered"] = bounded["uncovered"]
    return row


def _raw_ref_sha256(raw_ref: str) -> str | None:
    try:
        raw_path, raw_line = raw_ref.rsplit(":", 1)
        line = int(raw_line)
    except (AttributeError, TypeError, ValueError):
        return None
    if line <= 0:
        return None
    source = Path(raw_path)
    if not source.is_file():
        return None
    try:
        with source.open("r", encoding="utf-8", errors="strict") as handle:
            for number, raw in enumerate(handle, start=1):
                if number == line:
                    text = raw.rstrip("\r\n")
                    parsed = json.loads(text)
                    if not isinstance(parsed, Mapping):
                        return None
                    return _sha256_text(text)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate one typed nested tool envelope from JSON stdin."
    )
    parser.add_argument(
        "--input-file",
        help="Read the candidate JSON from an exact local file instead of stdin.",
    )
    args = parser.parse_args()
    try:
        raw = (
            Path(args.input_file).read_text(encoding="utf-8")
            if args.input_file
            else sys.stdin.read()
        )
        candidate = json.loads(raw)
        receipt = preflight_nested_tool_call(candidate)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        receipt = {
            "schema": SCHEMA,
            "decision": "semantic_review_required",
            "reasons": [f"invalid_cli_input:{exc}"],
            "host_blocking": False,
            "hook_intercepted": False,
        }
    sys.stdout.write(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
