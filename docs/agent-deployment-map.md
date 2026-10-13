# Agent Self-Deployment Map

This is the required pre-deployment map for an agent installing or adapting
Agent Cognitive Continuity Framework (ACCF). Read it before copying files, editing a host
configuration, or claiming that a capability is active.

## Core Rule

Select one complete declared deployment profile, stage every file in its
resolved dependency closure, map that integrated baseline to the real host
surfaces, and verify every claimed capability before adapting it. Do not
cherry-pick individual capability files during initial deployment. Repository
presence is not deployment evidence.

ACCF's routing, memory, retrieval, correction, and verification components are
designed to exchange bounded context and evidence as one control loop. Removing
one part can break those links and leave the remaining parts less useful than
their isolated file list suggests. A runtime profile may exclude public-source
documentation, papers, examples, and development tests, but its declared
runtime dependency closure should be treated as one unit.

```text
read this map
-> inspect real host instruction / model-loop / hook surfaces
-> select one complete deployment profile
-> stage its full resolved runtime bundle and keep its receipt
-> initialize deployment-local lane/config state from public templates
-> wire only supported host surfaces
-> run policy, behavior, and host-lifecycle checks
-> claim only capabilities whose receipts pass
```

## Component Classes

### A. Required Runtime Core

| Component | Purpose | Required when |
| --- | --- | --- |
| `AGENTS.md` | low-cost microkernel and risk/memory/claim/tool boundaries | every deployment |
| `skills/embedded-harness/embedded_harness_policy.json` | compiled runtime policy | every scripted or adapter deployment |
| `harness_intake_router.ps1` | deterministic route receipt | PowerShell/Codex direct-script profile |
| `harness_action_consumer.py` | bounded memory context, external retrieval plan, task-local correction receipt, and continuity capsule handoff | memory retrieval, external lookup, or full model-loop integration |
| `task_continuity.py` | dormant-by-default stable global task frame, progress reconciliation, resumable local side tasks, reuse-first reminders, and hash-bound transport paging | library dependency of every profile that deploys the action consumer; automatic host delivery is currently verified only on a separately tested host adapter |
| `runtime_projection.py` | deterministic, disposable global-causal-task and dynamic-reminder model views with split control/evidence entries | required library dependency of `task_continuity.py`; it is rebuilt from canonical capsule/reminder state and is not a memory source |
| `engineering_execution.py` | optional tracer-bullet, module-depth, invocation-topology, and seam-review advisory receipts | library dependency of every profile that deploys the action consumer; file inclusion alone does not prove host-loop routing or consumption |
| `task_continuity_workfile.py` | append-only, hash-chained `.cumcwork` snapshots, source-preserving v2-to-v3 projection, and exact task restart rehydration | continuity-aware hosts that need working state across adapter/process restarts |
| `semantic_memory.py` | append-only v3 records, rebuildable meta-first index, exact materialization, and store checks | every profile that claims future durable memory writes or v3 retrieval |
| `memory_runtime_bridge.py` | binds selected memory IDs/evidence handles into one task working set without promoting it to durable memory | profiles that combine memory retrieval with task continuity |
| `task_memory_checkpoint.py` | prepares a hash-bound candidate from one fully verified retired task, then synchronously appends and proves exact v3 retrieval when the caller separately promotes it | complete profiles that connect task continuity to durable project or conversation memory |
| `external_retrieval_strategy.py` | task-local exact-anchor, source-native, per-target retrieval receipt | any profile that deploys the action consumer or external research gate |
| `harness_memory_isolation_gate.ps1` | project-lane path receipt | PowerShell memory reads/writes |
| `harness_external_research_gate.ps1` | external-evidence route receipt | external/current fact routing |
| `external_route_trigger_helpers.ps1` | shared trigger and negation semantics for router and external gate | every profile that deploys either PowerShell route surface |
| `harness_claim_schema_verifier.ps1` | claim/evidence-boundary receipt | PowerShell claim validation |

The Bash directory is a trigger/router reference for model-facing decision gates and
requires `bash` plus `jq`. It does not implement the Python
`cbh.external_retrieval_receipt.v1` planner. Deploy it only when that target
shell is real and tested.

### B. Behavior-Correction Closure

Deploy this set as one unit; omitting one member invalidates the
profile/hash/verifier chain:

```text
behavior_correction_gate.py
behavior_correction_hook.py
behavior_correction_profiles.json
nested_tool_preflight.py
execution_feedback.py
python_inline_write_analysis.py
embedded_harness_policy.json
```

`behavior_correction_source_map.md` is provenance documentation, not a runtime
dependency. The hook is stateless and nonblocking: one accepted, mechanically
verified current-input rewrite or silent no-op. `nested_tool_preflight.py`
provides the same task-local review for an already selected nested shell or web
call when the native hook cannot observe that surface. It does not parse the
surrounding JavaScript, grant permission, deny, freeze, write memory, or mutate
policy.

### C. Codex Integration Components

| Profile | Required adapter surface | Boundary |
| --- | --- | --- |
| `codex-local-minimal` | root instruction, compiled policy, direct gates, action consumer, correction closure | hook activation still requires the exact installed Codex lifecycle |
| `codex-local-source-recall` | complete minimal profile plus text reader and explicitly configured context-recall adapter | private source configuration and real host callbacks must be supplied and verified by the adopter |

### D. Build And Maintenance Files

These maintain or validate a deployment, but are not always runtime files:

```text
embedded_harness_policy.authoring.toml
compile_policy_from_toml.py
compact_failure_audit.py
dangerous_delete_guard.py
validate_policy.ps1
tools/cbh_doctor.py
tests/
tests/test_codex_deployment_profile.py
```

Keep the TOML authoring source and compiler together in a source checkout. The
runtime consumes generated JSON; do not hand-edit both and let them drift.

### E. Documentation / Review Only

These explain, reproduce, cite, or review the framework and are excluded from
minimal runtime bundles by default:

```text
README*.md
docs/** (this map must still be read before deployment)
examples/**
paper/**
research/**
CHANGELOG.md
CITATION.cff
NOTICE.md
```

They remain required in a public source release, but copying them into an agent
runtime does not activate capabilities.

## Deployment Procedure

1. Inspect the exact host version and its real instruction, model-loop,
   updated-input, permission, and sandbox surfaces.
2. Run the bundle builder with `--list`; review the complete resolved file list.
3. Stage that entire profile into an empty directory and retain
   `cbh-deployment-receipt.json`.
4. Map the root instruction entry and compiled policy.
5. Initialize project/memory roots and lane state from the published templates
   through a deployment-local overlay.
6. Treat prompt routing as a model-layer pre-action contract when the model
   consumes it; do not claim independent host execution enforcement unless a
   real host loop consumes and honors its actions.
7. Enable correction only if the host accepts the declared updated-input
   protocol without bypassing native permission checks.
8. Run the compiler check, validator, doctor, profile tests, and one real
   host-lifecycle smoke test.
9. Record `checked_available`, `checked_missing`, or `checked_blocked` for each
   host capability; do not infer activation from files alone.

## Minimum Acceptance Matrix

| Claim | Required evidence |
| --- | --- |
| instruction active | a fresh task demonstrates that the root microkernel loaded |
| router active | route receipt from the deployed path |
| memory retrieval active | selected record with lane, source, and provenance returned to the model |
| v3 memory write active | append receipt, store check, meta-first search, and exact selected-record materialization all pass in the deployment-local lane |
| task continuity active | a fresh adapter process rehydrates the same stable global goal before adjudicating the current turn, preserves criteria/progress/reusable handles, and resumes bounded side tasks from the exact task workfile |
| task checkpoint promotion active | a completed task produces a read-only eligible candidate; explicit promotion returns both canonical durable and immediately searchable receipts; a new task retrieves the exact record and binds it to its working set |
| model-loop integration active | complete host-owned action-consumption receipt |
| correction active | known historical regression rewritten, verifier passes, invariants preserved |
| correction fallback active | no-match and forced verifier/module failure both produce silent no-op |
| client compatibility current | checks rerun after client or hook-configuration changes |

If any row lacks evidence, downgrade it to advisory, reference-only, or
unverified. Do not silently remove required components to make a smaller
bundle; update a named deployment profile and its tests instead.
