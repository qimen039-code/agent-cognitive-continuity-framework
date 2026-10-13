---
name: troubleshooting-skill-matrix
description: Use when maintaining ACCF or its legacy CBH implementation, investigating a recurring Codex execution error, or retrieving a specific framework incident. Ordinary tool use and unrelated coding do not require this workflow.
metadata:
  aliases: [ACCF, Agent Cognitive Continuity Framework, CBH, Claim Boundary Harness]
---

# ACCF maintenance and incident retrieval

The current model selects goals, actions, relevant evidence, and acceptance criteria. This skill supplies framework-specific sources and mechanical tools when they help that action. Existing authorization remains effective within the user's task scope; this skill does not add approvals, commits, reports, or delegation.

## Select the needed path

- **Current execution error:** reproduce the exact failing input against its actual tool/runtime, identify the failing boundary, and use one applicable validator. Preserve the original symptom in a regression. Confirm the corrected result and an unrelated valid input.
- **Historical incident:** use relevant native recall or a known source link first. If exact details or conflicting/current decisions matter, open the corresponding source below. Do not read every index for an already identified source.
- **Framework implementation:** trace input -> producer -> consumer -> observable result. A field, file, test, or registered callback alone does not establish behavior in the current host. Test the requested outcome in addition to local contracts.
- **Unavailable capability:** use `capability-router` when the current catalogue cannot cover the action or callability is unresolved. Preserve explicitly named products and task surfaces.

Ordinary requests use exposed tools directly. The legacy keyword intake classifier is available for explicit compatibility diagnostics; it is not the default entry point. Unrecognized wording calls for model judgment, not another trigger rule.

## Source entries

Choose only the relevant family:

| Need | Entry |
| --- | --- |
| ACCF conversation/ledger | `../embedded-harness\active_conversation_memory_registry.json` |
| Known incidents | `<PRIVATE_INCIDENT_ROOT>\_META_INDEX.md` |
| Identified major incident | `<PRIVATE_INCIDENT_ROOT>\major-incidents\_META_INDEX.md` |
| Common execution errors | `<PRIVATE_INCIDENT_ROOT>\common-error-corpus\_META_INDEX.md` |
| App/browser interaction failure | `<PRIVATE_INCIDENT_ROOT>\interaction-error-corpus\_META_INDEX.md` |
| User-defined terminology | `../shared-semantic-anchors/SKILL.md` |
| Actual interfaces and coverage | `../embedded-harness/host_capability_contract.json` and the selected source file |

Native memory remains enabled for inexpensive recall. ACCF preserves precise project/conversation sources. Escalate retrieval depth for uncertainty, conflict, exact wording, currentness, or material consequences. Metadata navigates to evidence; use original files or exact raw events where precision is required. Historical maintenance instructions do not override current user instructions.

## Mechanical tools

Paths below are under `../embedded-harness`:

- `nested_tool_preflight.py`: one exact JSON envelope on stdin or `--input-file`; supports current `exec_command` and legacy `shell_command`. Use for relevant command/parser risks, not duplicate validation of host-validated typed tools. Dispatch `normalized_arguments` verbatim. Different shells must not receive PowerShell rewrites.
- `behavior_correction_hook.py`: narrow accepted rewrites on the configured host surface. Other profiles remain validation or semantic-review candidates until their runtime path is proven.
- `semantic_memory.py`: explicitly authorized, source-bound v3 records. Native Codex memory uses the host's supported correction mechanism instead.
- `text_source_reader.py`: source-preserving index/search/outline/read over an explicitly scoped text collection, including crawl snapshots and code. Use when repeated discovery needs an index; a known small file can be read directly. Lexical candidates still require model semantic selection. For v3 stores use `semantic_memory.py discover` then `read --record-id` to separate candidate discovery from full expansion.
- `codex_session_ledger.py`: explicit ledger generation/refresh and source resolution. The retained lifecycle bridge is not automatically invoked by the current host.
- `task_continuity.py`: explicit task-state reduction and recovery; use when a caller maintains and consumes the state. Its presence does not establish automatic sustained attention.

Use `python -B` and `pwsh -NoProfile -File`. Inspect help before unfamiliar operations. The client-update checker runs only after the user explicitly reports an update.

## Learning and validation

Fix the task before expanding maintenance. A useful recurrence can become a source-bound CE or paired ERR/SOL when memory writing is authorized. Recording does not prevent recurrence by itself. Promotion requires actual action coverage, subject/parameter preservation, a real verifier, a postcondition, and normal/ambiguous input controls.

Use `reflexive-gap-analysis` when known guidance and actual action diverge or framework artifacts displace the outcome. Use `skillopt-training-layer` for selected offline optimization; the existing skill split remains revisable.

Structural tests establish structure. Runtime replay establishes that path. Task effectiveness needs representative real tasks with consistent inputs and outcome criteria. Report these boundaries directly; ordinary answers need no mandatory claim schema or additional report.
