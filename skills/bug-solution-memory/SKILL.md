---
name: bug-solution-memory
description: "Use when a specific stored Codex/local-tooling solution needs source validation, or the user authorizes saving an incident solution. Ordinary debugging and sufficient native recall do not require this record workflow."
---

# Bug Solution Memory

This file starts empty by design. Add real solution records only after the corresponding incident is understood.

## Pairing Rule

- Solution records live here.
- Agent error records live in `agent-error-memory`.
- Each solution point must include `paired_error_ids`.

## Storage Contract

The table and field list below document the record shape; they are not the
writable memory store. New records use `cbh.semantic_memory_record.v3` and are
appended with `../embedded-harness/semantic_memory.py` to the adopting lane's
private `memory-v3/` directory. The canonical `records.jsonl` is append-only;
`meta.jsonl` is a compact, rebuildable navigation surface. Existing Markdown
records, when present in an older deployment, stay read-only and are exposed
through hash-bound legacy links instead of being copied or rewritten.

## Reference Index Shape

| Solution ID | Outer retrieval surface | Paired errors |
| --- | --- | --- |
| `SOL-EXAMPLE-YYYY-MM-DD` | example solution trigger words | `ERR-EXAMPLE-YYYY-MM-DD` |

## Draft Field Guide

```text
## SOL-EXAMPLE-YYYY-MM-DD

status:
point_kind: bug_solution_solid_point
purpose_anchor:
meaning_anchor:
outer_retrieval_surface:
problem_class:
solution_summary:
investigation_order:
fix_path:
rollback:
validation:
caveats:
applicable_boundaries:
non_applicable_boundaries:
paired_error_ids:
future_reuse_rule:
references:
```
