# Project Skill Matrix Registry

A compact index that routes the agent to project-specific skill matrix routers. Keep this file small; put detailed behavior inside each project router skill.

## Select the task-relevant entry

Use the user's goal, actual object, next action and side effects to select a
skill. An exposed action-specific tool is the default; the retained intake
router can produce an advisory receipt when a caller needs that interface.
It is not an obligatory first step for ordinary requests.

| Need | Entry |
| --- | --- |
| Repeated scoped text/code reading | `skills/text-source-reader/SKILL.md` |
| A concrete missing tool or named capability | `skills/capability-router/SKILL.md` |
| ACCF maintenance or a selected incident | `skills/troubleshooting-skill-matrix/SKILL.md` |
| Authorized reusable error/solution recording | `skills/agent-error-memory/SKILL.md`, `skills/bug-solution-memory/SKILL.md` |
| User-defined project terminology | `skills/shared-semantic-anchors/SKILL.md` |

Read only the selected skill and necessary references. Use relevant native
recall or an exact valid source first; expand a lane's meta/category/payload
navigation only when discovery, precision or a conflict requires it. Summaries
remain locators, and actual original content supports conclusions.

Existing task-scoped authorization survives routing, skill activation and
phase changes. The host owns permission and execution. Risk levels and legacy
receipt fields do not create a new approval requirement or authorize actions.
New external effects or irreversible operations need the appropriate scope.

Errors are fixed and verified in the current task before any authorized durable
recording. A record does not become a rule automatically; historical or
superseded material remains diagnostic. Avoid a new keyword rule for each new
phrase when model judgment and existing action-specific tools are sufficient.

## Nonblocking Behavior Correction Layer

Known mechanically recognizable recurrence shapes may be reviewed immediately before a tool action:

```text
profile inventory / advisory receipt -> behavior_correction_gate.py
verified current-input rewrite -> behavior_correction_hook.py
memory-selected task-local receipt -> harness_action_consumer.py
```

Only an accepted deterministic profile may return `allow + updatedInput`; ambiguity, verifier failure, unavailable host protocol, or no match is a silent no-op. This layer does not own authorization or denial.

## Mandatory Search And Learning Decision Matrix

When a task needs public facts, open-source repository evidence, external mechanism learning, or anti-closed-door-invention review, choose the appropriate search and learning route:

| Route | Use when | Required boundary |
| --- | --- | --- |
| Official / authority source search | product, institution, policy, law, price, version, release date, named role, or other drift-prone public fact | Prefer official or authority sources first, then cross-check when practical. |
| GitHub / open-source repository search | repository, release, issue, license, changelog, source tree, project activity, examples | Separate README claims, source-code facts, release/issue evidence, and license boundaries. |
| General web cross-check | ecosystem trend, mechanism comparison, third-party guide, community experience, or uncertain public claim | Use independent sources when practical; mark source limits when not. |
| Source-grounded learning intake | external mechanism, external architecture comparison, learn-from-open-source work, or anti-closed-door-invention task | Build a source ledger and classify material as fact, source_prior, hypothesis, inspiration, unverified implementation path, or not_applicable. |
| Local validation route | adopting a mechanism, making a strong claim, or reporting success | Do not upgrade to locally validated without local files, scripts, tests, reproduction, or a concrete evidence chain. |

Outside material can supply completion ideas, boundary control, engineering constraints, validation methods, failure lessons, or non-applicable examples. It must not replace the active project objective or be presented as local validation by itself.

## Native Skill / Plugin / Connector Discovery Layer

Codex-native skills and installed plugins are not always callable until the
current turn exposes them through the skill list, `tool_search`, MCP/app tools,
or an explicit user mention. When the task depends on a platform object or a
specialized client capability, run a cheap discovery pass before falling back
to shell, raw web, clone, or manual download.

| Object / capability | Preferred discovery route | Fallback boundary |
| --- | --- | --- |
| GitHub repo, file, issue, PR, release, Actions log/artifact | Discover GitHub MCP/app tools first; use direct file/log/artifact tools when available | Shell `git`/`gh` or web only after plugin surface is missing, blocked, or insufficient for the needed local write |
| Hosted or SaaS platform objects | Discover the named plugin/connector first when one exists | General web or shell only after `checked_missing`, `checked_blocked`, or local filesystem execution is the actual target |
| PDF, documents, spreadsheets, presentations, templates, OpenAI docs, skill/plugin creation or install | Use the Codex native/system or primary-runtime skill listed for the turn | Shell only when the native skill cannot read/write the needed local artifact or the task is a build/test/edit step |
| Browser, Chrome, Computer Use | Prefer Browser/Chrome/node_repl control when available; Computer Use only when the user names it or lighter browser paths are insufficient | Screenshots/OCR/manual UI only after lighter browser surfaces fail |
| Local build/test/edit, filesystem copy, Windows config, large local repo clone | Shell/local tools | Plugin discovery is optional unless a platform API object is also part of the task |

Record the internal decision as `tool_surface_need`,
`tool_discovery_status`, `skill_or_tool_need`, `plugin_need`, and
`preferred_call_surface`. Show it only in extended/debug receipts or when it
changes the action path.

## Registered Routers

| Scope | Path or trigger | Router skill / file | Status | Notes |
| --- | --- | --- | --- | --- |
| Shared troubleshooting | agent errors, tool failures, skill matrix updates, reusable incidents | `skills/troubleshooting-skill-matrix/SKILL.md` | active | Routes to semantic anchors and paired ERR/SOL ledgers. |
| Embedded harness | nontrivial task intake, direct-outcome-first routing, memory isolation, external research trigger, claim schema, policy validation | `skills/embedded-harness/README.md` | active | Low-cost deterministic entry route; PowerShell and the Codex hook surface expose the direct-outcome action contract. |
| Example project | `<PROJECT_ROOT>`, `EXAMPLE_PROJECT` | `<PROJECT_ROOT>/AGENTS.md` or a future project router skill | template | Replace with real project details after adoption. |

## Project Router Manifest Contract

Each project router should declare:

- scope
- capabilities
- permission declaration
- risk level and escalation triggers
- default invocation mode
- human-confirmation requirements
- linked point sets
- memory meta summary and category index roots
- external mechanism intake rules
- provenance and rollback policy for executable content
