---
name: agent-error-memory
description: "Use for an explicitly selected historical agent-error record or authorized durable recording of a material incident. Ordinary execution mistakes are fixed in the current task; they do not automatically invoke the incident-memory workflow."
---

# Agent Error Memory

This file starts empty by design. Add real records only after a solved incident.

## Pairing Rule

- Error records live here.
- Solution records live in `bug-solution-memory`.
- Each error point must include `paired_solution_ids`.
- Do not hide agent-caused failure inside a success note.

## Storage Contract

The table and field list below document the record shape; they are not the
writable memory store. New records use `cbh.semantic_memory_record.v3` and are
appended with `../embedded-harness/semantic_memory.py` to the adopting lane's
private `memory-v3/` directory. The canonical `records.jsonl` is append-only;
`meta.jsonl` is a compact, rebuildable navigation surface. Existing Markdown
records, when present in an older deployment, stay read-only and are exposed
through hash-bound legacy links instead of being copied or rewritten.

## Reference Index Shape

| Error ID | Outer retrieval surface | Paired solution |
| --- | --- | --- |
| `ERR-EXAMPLE-YYYY-MM-DD` | example trigger words | `SOL-EXAMPLE-YYYY-MM-DD` |

## Draft Field Guide

```text
## ERR-EXAMPLE-YYYY-MM-DD

status:
point_kind: agent_error_solid_point
purpose_anchor:
meaning_anchor:
outer_retrieval_surface:
event_summary:
agent_fault:
why_it_was_wrong:
impact:
evidence:
applicable_boundaries:
non_applicable_boundaries:
risk_failure_conditions:
paired_solution_ids:
prevention_rule:
not_to_repeat:
```
