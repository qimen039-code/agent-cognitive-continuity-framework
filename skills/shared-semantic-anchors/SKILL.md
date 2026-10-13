---
name: shared-semantic-anchors
description: Use when a user explicitly defines or corrects reusable project terminology. Store confirmed meaning and boundaries in the adopting workspace, with authorization for durable writes.
---

# Shared Semantic Anchors

This file starts empty by design. It is not a glossary and not a fact database.

## Rules

1. Only anchor semantics the user explicitly defines or corrects.
2. Store both meaning and non-meaning.
3. Preserve exact phrase boundaries.
4. Do not store assistant guesses as anchors.
5. Route troubleshooting-related anchors through the troubleshooting skill matrix when needed.

## Anchor Index

| Anchor ID | Phrase | Use when |
| --- | --- | --- |
| `ANCHOR-EXAMPLE` | example phrase | Replace after adoption. |

## Anchor Template

```text
## ANCHOR-EXAMPLE

phrase:
exact_trigger:
anchored_meaning:
non_meaning:
applicable_boundaries:
non_applicable_boundaries:
source:
update_rule:
related_points:
```
