# Architecture

Agent Cognitive Continuity Framework has four layers.

## 1. Root Microkernel

`AGENTS.md` is the smallest always-on rule set. It asks the agent to classify work before loading large context:

- task type;
- active project lane;
- evidence needs;
- memory boundary;
- risk boundary.

The microkernel should stay short. Put project details elsewhere.

## 2. Embedded Harness

`skills/embedded-harness` contains local scripts:

- `harness_intake_router.ps1`;
- `harness_memory_isolation_gate.ps1`;
- `harness_external_research_gate.ps1`;
- `harness_claim_schema_verifier.ps1`;
- `harness_action_consumer.py`;
- `task_continuity.py`;
- `embedded_harness_policy.json`.

The intake router classifies work into R0-R5 internally and returns required gates. User-facing surfaces stay silent by default unless the classification changes execution path, cost, permission, memory, search, or claim boundaries.

Machine-local project lane roots are loaded through a private overlay file:
`embedded_harness_policy.local.json` beside the runtime policy, or the path in
`CBH_PROJECT_LANES_FILE`. This keeps concrete cwd project evidence available to
the router without publishing private local paths in the shared policy JSON.

The local deployment profile and bundle builder live under
`integrations/codex-local/`. They stage the declared runtime closure but do not
establish host activation without the corresponding lifecycle checks.

The mandatory model-layer pre-action control plane sits around the router:

```text
routing receipt (including optional task_continuity_decision)
-> intake route and cheapest sufficient gate selection
-> optional stable global task frame plus local deltas for qualifying tasks
-> event-triggered re-evaluation
-> final claim, memory, version, and verification boundary check
-> selective hard runtime gate only for critical risks
```

The host model decides whether the current task needs project instructions,
memory, skills, tools, external evidence or missing authorization. ACCF offers
optional navigation, state and mechanical receipts for those choices. It does
not require a routing receipt for every task or expand into every available source.

For a protected high-risk action, the control plane is an execution-state gate:
without exact current human authorization, the model must stop before forming
or calling the tool action. That model-layer pre-action stop is distinct from a
host-enforced execution stop. The latter requires a tested hook, proxy,
permission system, sandbox, or operating-system boundary that can reject the
action independently of model compliance.

Authorization follows the agreed task scope and remains valid while that scope
and its effects remain unchanged. Routing and skill boundaries do not consume
it. New external effects or irreversible operations require their own authorization.

## Hook Capture Point Matrix

Adopters can map the control plane onto host lifecycle hooks when the runtime exposes them. These stages capture the right context at the right time; they are not a requirement to run a full memory backend.

| Capture point | Main purpose | Harness action | Default cost boundary |
| --- | --- | --- | --- |
| `session_start` | Rehydrate only the minimum active lane context. | Read root instructions and memory meta index when needed. | Meta/index only; no payload scan. |
| `user_prompt_submit` | Preserve the original user task before tools lose context. | Build route receipt, classify memory/search/claim needs, store compact prompt state if hooks support it. | Compact receipt; no full history. |
| `pre_tool` | Stop or warn before protected actions. | Enforce R5, low-confidence route review, memory write, high-risk command, and external-search requirements. | Model-layer stop is mandatory; host hard denial exists only on hooked paths. |
| `post_tool` | Capture what actually happened. | Store observations, tool failures, or candidate CE records as raw or working memory; do not mark success as validated without evidence. | Raw observation or working memory only. |
| `pre_compact` | Avoid context-compression loss. | Write a conversation checkpoint or open-loop summary when routed. | Summary and links only. |
| `stop` / `final` | Prevent final overclaims and decide durable updates. | Run final claim boundary, update memory only when routed, and record remaining verification debt. | Claim schema plus optional capsule update. |

If a host lacks one of these hooks, record the missing host-enforcement surface
in the compatibility manifest. The model-layer pre-action stop still applies
for protected high-risk actions; only the independent execution-time denial is
unavailable.

## Router Decision Contract

The router and dynamic decision layer use a compact contract before opening deeper modules:

```text
task_type
target_surface
audience
project_lane
risk_level
semantic_ambiguity
module_need
skill_lifecycle_profile
feedback_loop_profile
memory_need
hybrid_retrieval_profile
memory_mode
memory_write_profile
memory_lane
record_intent
external_need
claim_risk
projectization_decision
conversation_memory_decision
link_intent
receipt_profile
required_gates
```

This contract keeps the decision layer stronger than a suggestion while avoiding full runtime wrapping. It tells the agent which surface is being touched, who the content is for, whether a term is ambiguous, which module to open, whether memory or external lookup is needed, and whether the final answer needs a claim schema.

For bounded user-visible mutations, the conditional
`direct_outcome_first_instruction` carries the first-mutation rule, the finite
set of allowed expansion conditions, and the retirement condition. The router
also emits a `direct_outcome_first` action binding. A compatible model loop
consumes it at `before_first_substantive_mutation`: after the minimum necessary
read, the first substantive write targets the requested surface, or the loop
records which declared expansion condition is supported by evidence.

The design borrows a few lightweight patterns: separate decision from execution, run prechecks before critical boundaries, route by metadata, reassess on trigger events, preflight likely failure modes, and keep audience/ownership explicit. These are design influences, not proof that this framework is validated in every runtime.

When self-check exposes substantial memory pollution, target pollution,
dirty-tree debt, or accumulated technical debt, the same control plane can
route a debt hygiene pass. That pass inventories and groups issues, cleans the
current required set, and records deferrable items as
`candidate_technical_debt` for later review instead of trying to force the debt
to zero.

## Receipt Profile Layer

The full receipt schema remains the canonical governance model, but runtime adapters do not need to expose every field on every turn. The router computes the full decision and selects a profile:

- `compact_runtime`: default low-cost output for local agent loops. It carries the fields needed to enforce R5, memory route, external search budget, claim gate, and confirmation.
- `extended_governance`: public repository, local harness, adapter, project memory, semantic ambiguity, memory write, or projectization work.
- `debug_receipt`: full route diagnostics and trigger evidence.

This mirrors a common systems pattern: keep the policy decision point complete, but keep risk labels silent by default and expand emitted context only when the execution boundary needs it.

See [router-decision-contract.md](router-decision-contract.md).

## Declarative Governance Contract

Adapters can publish a small governance contract that declares which stages they support and what each stage must honor:

```text
prompt_stage
pre_tool
memory_access
external_research
final_claim
```

The contract is not a new runtime layer. It is a leverage point that lets adopters check the minimum adapter promises without loading every skill, memory file, or tool schema.

The contract should cover:

- stage support;
- decision vocabulary: `allow`, `warn`, `require_approval`, `deny`;
- minimum receipt fields;
- denial payload and blocked exit code;
- payload safety;
- cost profile.

See [declarative-governance-contract.md](declarative-governance-contract.md) and `templates/adapter-contract/governance.contract.json`.

## Version Compatibility Manifest

Adapters can also publish a compact compatibility manifest for the target runtime/client version. It records what was checked, not what is assumed.

The manifest should cover:

- runtime and adapter versions;
- instruction entry;
- hook schema;
- wrapper paths and language availability;
- denial behavior;
- payload safety checks;
- minimum acceptance results;
- bypass surfaces;
- drift policy.

Do not refresh this manifest every turn. Refresh it on adapter install, client update, hook/wrapper edits, failed smoke tests, or explicit user request.

See [version-compatibility-management.md](version-compatibility-management.md) and `templates/adapter-contract/compatibility.manifest.json`.

## Memory Routing Contract

Memory use has its own routed decision:

```text
memory_need
hybrid_retrieval_profile
memory_mode
memory_write_profile
memory_lane
record_intent
projectization_decision
```

This prevents the framework from treating every mistake as permanent memory or every memory mention as a full history read. Common small mistakes can be stored as lightweight CE error-and-solution records. Full ERR/SOL pairs are reserved for explicit self-reflection requests, high-impact incidents, or repeated failures.

See [memory-routing-contract.md](memory-routing-contract.md) and [common-error-corpus.md](common-error-corpus.md).

Memory retrieval results should preserve provenance. A selected record should return at least these fields alongside the snippet: `source_tag` `derived_from` `belief_status` `confidence` `score_method`. A relevance score can rank candidates, but it does not turn a raw observation or source prior into a validated claim.

Reusable memory content must also preserve meaning. A promoted capsule should be
context-complete, preserve the source language in the content plane, and keep
English structure fields for adapters. Hybrid retrieval is meta-first and
source-preserving: lane/category/status filters select a small candidate set
before exact phrase, original-language keyword, Chinese character n-gram,
English term, or optional lexical ranking is used.
The route exposes this as `hybrid_retrieval_profile` so adapters can strengthen
recall inside the existing meta-first path without replacing it. Durable memory
writes expose `memory_write_profile` so the selected write shape is
context-complete or strict-capsule as needed.

Reusable records that are meant to prevent repeated mistakes can optionally
carry a lightweight feedback loop: memory -> prediction -> verification ->
calibration. This is a trial field inside existing capsules, CE records,
ERR/SOL pairs, or decision records, and it becomes a decision-layer requirement
when such a reusable record is selected for recurrence prevention. It is not a
per-task token ledger and does not promote predictions into validated facts.

Memory integrity checks keep long-lived records from silently drifting:
contradicting capsules are resolved by scope plus evidence strength rather than
recency alone; source invalidation follows `derived_from` and
`source_validity_dependency` links before reusable retrieval or strong claim
promotion; and frozen lanes remain available for audit or A/B/C comparison
without participating in normal retrieval or writes.

Reading is a separate routed step after retrieval. The route or decision layer
chooses `baseline`, `evidence_window`, `middle_safe`, or `full_audit` reading.
This keeps ordinary reads cheap while still enabling source context headers,
bounded evidence windows, unread-zone notes, and middle-safe evidence layout
when a strong claim, memory promotion, release note, R4/R5 decision, long
source, or multi-hop evidence chain requires it.

See [memory-write-granularity-contract.md](memory-write-granularity-contract.md),
[hybrid-memory-retrieval-contract.md](hybrid-memory-retrieval-contract.md),
[memory-feedback-loop-trial.md](memory-feedback-loop-trial.md), and
[content-reading-contract.md](content-reading-contract.md). Source dependency
and conflict rules live in
[source-monitoring-memory-schema.md](source-monitoring-memory-schema.md), and
lane availability states live in
[memory-meta-index-contract.md](memory-meta-index-contract.md).
Optional cross-workspace packaging uses
[portable-context-bundle-contract.md](portable-context-bundle-contract.md): it
binds existing indexes and capsules to a manifest, lane root, source refs, and
hash-triggered validation cache without creating a new memory backend or
per-read verifier.

## Skill Lifecycle Contract

Skill work is also routed. Idle skills should stay at name, description,
meta-summary, route tags, and activation-condition level. When a selected skill
is needed, the route exposes `skill_lifecycle_profile:
active_frame_required` and the runtime should load `SKILL.md` plus only the
needed support files.

After the skill phase ends, the route can require
`skill_lifecycle_profile: release_receipt_required`. The adopting runtime should
release the large rendered skill body where its host supports context garbage
collection, and must preserve a compact `skill_release_receipt` with
`skill_id`, completed steps, current stage, artifact paths, evidence refs, open
loops, and `resume_entry`.

Later work reactivates from the receipt by rereading current source files. Do
not rely on long-lived compressed skill fragments as the authority.

See [skill-lifecycle-contract.md](skill-lifecycle-contract.md).

## Conversation Memory Lane

Projectless long conversations can use a separate conversation memory lane when the router detects explicit checkpoint instructions or durable long-chat signals:

```text
conversation-memory/_META_INDEX.md
-> conversation_state.md or index.json
-> decisions.jsonl / open_loops.jsonl / errors_and_solutions.jsonl / references.jsonl
```

This lane is isolated by conversation or thread id. It can be read by later conversations only through explicit reference, and cross-conversation writes require explicit user instruction. If the work becomes a real project, the router should mark `projectization_decision: emergent_project_candidate` instead of silently mixing conversation memory into project memory.

Conversation memories use stable `memory_id`, `updated_at`, index-level retrieval terms, and append-only links. A later conversation can continue an old one through a `continues` edge without writing new content into the old lane. Explicit merges create a new merged memory and link old memories into it.

See [conversation-memory-lane.md](conversation-memory-lane.md) and [memory-linking-contract.md](memory-linking-contract.md).

## Static Knowledge Layer

Projects may also keep a static knowledge layer for stable repository manuals:

```text
static-knowledge-layer/_STATIC_KNOWLEDGE_INDEX.md
-> project-map.md
-> entrypoints-and-commands.md
-> conventions.md
-> interfaces-and-data.md
```

This layer is not a memory backend and not a validation source by itself. It is a
low-cost orientation layer for module maps, entry points, commands, conventions,
and interface notes. Static notes should be returned with `source_tag:
static_knowledge`, `belief_status: source_prior`, `derived_from`, `confidence`,
and `score_method` metadata.

Use it when a task needs project navigation. Use the memory library when a task
needs decisions, incidents, evolving state, or source-monitoring capsules. A
static page can point to memory records, but it should not copy volatile memory
payloads by default.

See [static-knowledge-layer.md](static-knowledge-layer.md).

## Format Layering

The framework uses Markdown for human-facing docs and meta summaries, but machine-owned facts should use structured formats:

```text
Markdown -> public explanation, meta index, short capsules
JSON -> router policy and machine-readable indexes
JSONL -> append-only decisions, open loops, errors, solutions, references
CSV/TSV -> large tabular matrices
JSON/JSONL/CSV or explicitly approved local store -> non-semantic operational indexes
```

See [format-layering.md](format-layering.md).

## Cost Control Contract

The framework keeps a complete internal contract but emits only the smallest useful receipt:

```text
R0 -> no explicit receipt
R1/R2 -> compact runtime receipt
R3/R4 -> compact receipt plus triggered fields
R5 / public / archive / persona / memory write / debug -> extended or debug receipt
```

After the initial receipt, event-triggered re-evaluation should emit a delta receipt with changed fields only. A field belongs in the default receipt only if it changes the next action.

See [cost-control-contract.md](cost-control-contract.md).

## Edit Operation Boundary

Content changes are routed as append, in-place patch, bounded section
replacement, new artifact, supersession, archive/move, full rewrite, in-file
content deletion, or filesystem deletion. Ordinary "update", "sync", "improve",
or "adapt" wording defaults to the narrowest anchored edit. Full rewrites and
deletions require their own triggers and review.

See [edit-operation-contract.md](edit-operation-contract.md).

## Archive And Persona Boundaries

Global archive is optional cold storage. It is checked after active project or conversation memory, not before. Archive defaults to moving or copying source files/directories so provenance stays intact.

Persona state is conversation-only and default-off. It may shape tone inside an ordinary conversation, but it cannot influence factual claims, risk, verification, project boundaries, memory boundaries, external research, claim schema checks, or tests.

See [archive-and-persona-boundaries.md](archive-and-persona-boundaries.md).

## Nonblocking Behavior Correction Layer

The framework may review one current candidate immediately before execution when a host exposes a compatible hook:

```text
current candidate
-> behavior_correction_gate.py profile receipt
-> accepted deterministic profile only
-> behavior_correction_hook.py verifier-backed rewrite
-> allow + updatedInput, or silent no-op
```

There is no ACCF hard-stop state inside this correction layer. Ambiguity,
verifier failure, unsupported protocol, or no match leaves the event unchanged.
This narrow statement does not describe the whole ACCF control plane: R5 and
other protected actions still stop at the model decision layer until exact
authorization, while independent execution-time denial remains host-native.
Claims, memory writes, and execution authority remain governed by the model's
instructions and the host's native boundaries.

## Search And Learning Decision Matrix

External research is split into route types so the agent does not treat all outside lookup as the same operation:

| Route | Evidence surface | Output |
| --- | --- | --- |
| Official / authority source search | official docs, public notices, policy, law, price, version, release date, named role | current fact with source boundary |
| GitHub / open-source repository search | README, source tree, release notes, issues, changelog, license, examples | repository-grounded evidence with reuse boundary |
| General web cross-check | independent articles, tutorials, ecosystem notes, community reports | cross-checked public context with source limits |
| Source-grounded learning intake | external mechanism, external architecture comparison, learn-from-open-source task | source ledger plus classification labels |
| Local validation route | local files, scripts, tests, reproduction, smoke checks | local evidence boundary for strong claims |

The source-grounded learning intake uses these classification labels:

```text
fact
source_prior
hypothesis
inspiration
unverified_implementation_path
not_applicable
local_validated
```

This keeps external learning useful without letting it become an unsupported local success claim.

## 3. Skill Tree

The skill tree has a router and three ledgers:

- `troubleshooting-skill-matrix`: routes incidents, anchors, and project manifests.
- `agent-error-memory`: stores agent-caused process errors.
- `bug-solution-memory`: stores solution patterns.
- `shared-semantic-anchors`: stores user-confirmed meanings and boundaries.

The ledgers start empty in this whiteboard package.

## 4. Project Templates

`templates/project` contains a project instruction file and a memory-library skeleton. Each adopting project should copy and edit these templates instead of placing private project content into the shared core.

## Layered Memory Library

Project memory is intentionally not a single flat summary file. A flat file is cheap at first, but it becomes another overloaded history blob as records grow. The mandatory retrieval path is:

```text
memory-library/_META_INDEX.md
-> choose one category or link ledger
-> category/_INDEX.md / memory_links.jsonl
-> open only the matching capsule
```

Do not skip the meta index. If a project has no meta index yet, use the smallest available top-level index or router manifest as a temporary meta layer and mark the missing layer as an adoption gap.

The default categories are:

- `governance`: project rules, decision records, boundaries, and operating contracts.
- `memory_hierarchy`: reusable memory capsules, active context, progress notes, and supersession chains.
- `external_references`: source notes, outside mechanisms, citations, and adoption boundaries.
- `raw_logs`: raw or near-raw observations that should not be treated as final memory.

Each category index should keep records small and searchable:

```text
ID | Type | Status | Summary | Retrieval Terms | Applies | Not Applies | Linked Modules | Linked Records | Supersedes | Superseded By
```

Recommended status values:

- `ACTIVE`: currently preferred record.
- `SUPERSEDED_BY:<ID>`: replaced by a newer record.
- `DEPRECATED`: retained for audit value but not used as current guidance.
- `TEMPLATE`: example or placeholder only.

When a new capsule replaces an old one, update both sides: the old row should point to `SUPERSEDED_BY:<ID>`, and the new row should declare `Supersedes`. This keeps long-horizon memory auditable without forcing the agent to reread old records on every task.

If a capsule or CE record has a feedback loop, keep the full loop in the
payload and expose only a compact index hint such as `feedback_loop: pending`
or `feedback_loop: matched`. A failed prediction should trigger calibration and
may promote the record to a paired incident or a router regression.
Use `feedback_loop_profile` to decide whether the current task needs only an
index hint, a compact record candidate, selected prevention review, or an
explicit full cycle.

See [../examples/memory-library-demo/_META_INDEX.md](../examples/memory-library-demo/_META_INDEX.md) for a synthetic end-to-end example.

For the full recommended index fields and retrieval budget, see [memory-meta-index-contract.md](memory-meta-index-contract.md).

## Routing Model

```text
R0 ordinary response
R1 read-only inspection
R2 durable artifact
R3 code or config change
R4 experiment, runtime, or current source work
R5 high-risk action requiring confirmation
```

Routing is additive. A task can match R3 and R4 at the same time. The router keeps the highest label and returns all required gates.

## Boundary

The harness has a mandatory model-layer pre-action contract whenever the host
model loads it. It can stop that governed model before an unauthorized
protected action, but it cannot independently suppress a tool process unless a
wrapper, hook, proxy, permission system, or sandbox is connected and honors the
decision.

Even after hook or wrapper integration, enforcement is limited to paths that actually invoke the scripts. Use local smoke checks after agent client updates because launch paths, hook behavior, and bundled runtimes can change.

Do not treat this layer as a hard sandbox. If an agent has a direct execution
route that bypasses the wrapper or tool proxy, host-enforced denial is absent on
that route. The model-layer pre-action stop still applies when that agent loads
and follows the ACCF control path.

PowerShell and hook paths must be smoke-tested against the installed client,
shell, and lifecycle surface before any enforcement claim is made.
