# Router Decision Contract

The router and dynamic decision layer are the front door of the framework. They must stay cheap enough to run on every nontrivial task, but precise enough to decide which deeper module is worth opening.

## Contract

For any nontrivial task, produce or internally satisfy this minimal receipt before doing expensive work:

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
read_semantic_boundary
read_depth_profile
edit_operation_profile
task_continuity_decision
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

Field meanings:

| Field | Purpose |
| --- | --- |
| `task_type` | Classify ordinary chat, read-only inspection, documentation, code/config change, runtime/current-fact work, or high-risk action. |
| `target_surface` | Name the surface being changed or consulted: public docs, local harness, project memory, conversation memory, skill matrix, tool call, adapter, git action, or private rule. |
| `audience` | Separate public users, local maintainer, project-only operator, or current chat. |
| `project_lane` | Keep memory and project instructions scoped to one lane unless cross-project work is explicit. |
| `risk_level` | Use additive R0-R5 routing; keep the highest risk and union of gates. |
| `semantic_ambiguity` | Mark terms that could mean multiple actions, such as update, record, publish, call, route, memory, or skill. |
| `module_need` | Decide whether to use no module, project router, semantic anchors, skill matrix, memory meta index, conversation memory index, static knowledge index, external research gate, claim verifier, or runtime hard gate. |
| `skill_lifecycle_profile` | Decide whether selected skill work stays listing-only, opens an active frame, requires a release receipt, or reactivates from a previous receipt. |
| `feedback_loop_profile` | Decide the feedback-loop depth without replacing risk, external, claim, record, memory-isolation, or gate-union decisions. |
| `memory_need` | Decide whether memory is unnecessary, meta-only, index-only, capsule-level, paired ERR/SOL retrieval, common error corpus, or conversation state. |
| `hybrid_retrieval_profile` | Decide whether memory lookup stays unused, uses the normal meta-first surface, or must add the hybrid lexical/original-language enhancement over the already bounded candidate set. |
| `memory_mode` | Decide whether memory should be skipped, read, written, or updated. |
| `memory_write_profile` | Decide whether a memory write is absent, context-complete, or strict reusable-capsule shape. |
| `read_semantic_boundary` | Decide what semantic evidence demand is active before selecting reading depth. |
| `read_depth_profile` | Decide whether to use capsule-only, segment, raw context, artifact output, cross-lane, source cascade, or full-lane audit reading. |
| `edit_operation_profile` | Decide whether a mutating task is append, local patch, section replace, new artifact, supersession, archive/move, full rewrite, in-file deletion, or filesystem deletion. |
| `task_continuity_decision` | Keep the continuity layer dormant for answer-only work; arm it for tool, write, long-running, multi-stage, resumed, or explicitly requested work; continue an existing active capsule. CBH itself remains active. |
| `memory_lane` | Decide whether the memory action belongs to a current project, current conversation, referenced conversation, emergent project candidate, common error corpus, self-reflection matrix, global inbox, or no lane. |
| `record_intent` | Decide whether there is no record request, explicit user request, inferred reusable error, projectization review, conversation checkpoint, or explicit conversation memory request. |
| `external_need` | Decide whether external lookup is unnecessary, official-source, GitHub/open-source, general cross-check, source-grounded learning, or local validation. |
| `claim_risk` | Decide whether the final answer contains an operational note, a weak claim, or a strong factual claim needing schema evidence. |
| `projectization_decision` | Decide whether projectless work is still not a project, belongs to a current project, or should be treated as an emergent project candidate. |
| `conversation_memory_decision` | Decide whether projectless long-chat state should skip memory, create/update current conversation memory, become a checkpoint candidate, read a referenced conversation, or perform an explicit cross-conversation update. |
| `conversation_full_lane_triggered` | Boolean showing whether grouped full-lane thresholds, not only the legacy flat signal count, crossed a conversation-memory boundary. |
| `conversation_full_lane_groups` | Compact debug/audit detail for the threshold groups that hit: context loss, durable decision, open loop, or artifact/code-change cluster. |
| `link_intent` | Decide whether to continue from latest memory, continue from a referenced memory, explicitly merge memories, archive/seal a memory, or create no link. |
| `receipt_profile` | Decide whether to expose a compact runtime receipt, expanded governance receipt, or debug receipt. |
| `required_gates` | List the concrete gates to run or honor. |

## Receipt Profiles

The router should compute the full decision internally, then expose the smallest useful surface:

| Profile | Use when | Expose |
| --- | --- | --- |
| `compact_runtime` | Internal default for local runtime and single-user agents. | Expose only action-changing fields: gates, memory mode/lane, external need, claim risk, or human confirmation need. |
| `extended_governance` | Public docs, local harness, adapters, project memory, conversation memory, semantic ambiguity, memory writes, projectization drift, or audience-boundary work. | Full governance receipt fields. |
| `debug_receipt` | Router debugging, misroute analysis, or user asks for full receipt. | Full receipt plus matched/negated triggers, confidence, and profile reasons. |

This keeps local Codex execution cheap while preserving the full whiteboard
schema for migration, audits, and public framework work.

## Task Continuity Boundary

`task_continuity.py` maintains a process-local stable global goal, acceptance
progress, earliest incomplete work, next action, local-turn relation, bounded
side-task resume stack, reusable-source refs, dynamic reminders, and hash-bound
transport pages. An unmatched active-task turn is a local ambiguous delta and
cannot replace the global goal. Global replacement requires high-confidence
explicit or current-revision-reviewed evidence. A bounded unrelated question
can be answered as a temporary side conversation while the same global frame
remains resumable; it does not need to create another durable task frame.
An explicit return resumes that preserved frame. It stores no chain-of-thought,
grants no permission, and writes no task-memory file. A `host_delivery: ready`
decision only means a bounded context entry was constructed; automatic model
visibility requires a verified host adapter. Without that adapter the status
is library-ready/host-pending, not an automatic continuous-memory claim.

## Capability Exposure Short-Circuit

`tool_surface_need` says that a capability family is needed; it does not prove
that discovery work is still required. The host/model must check the current
turn's exposed skill, tool, app, and connector surface first. If one exposed
candidate covers the requested action and its current instructions or schema
are available, resolve the task-local state as
`tool_discovery_status: resolved_current_exposure` and call it directly.

Do not query the capability map, registry, `tool_search`, install state, or a
host refresh against the unchanged exposure state. One discovery pass is
appropriate only when no exposed candidate covers the action, action coverage
is ambiguous, authentication or callability is unresolved, or the exposed call
fails. The static intake router cannot observe host exposure by itself; its
`not_checked` result is an input to this host-side short-circuit, not proof that
another search must run.

Full candidate route sets are not part of ordinary compact or extended
receipts. Emit them only for debug or bounded semantic adjudication after a
real conflict, invalid selection, or fallback. When execution evidence needs a
route link, store one `route_receipt_ref` on the existing ledger segment or
evidence record; do not duplicate the task as a second event chain.

## External Mechanism Fit

Source-grounded intake must separate relevance from adoption. Before an
external mechanism becomes an implementation candidate, use the existing
`global_task_context_gate` to compare the primary outcome, current architecture,
local coverage, explicit non-goals, and acceptance tests. Record the source
assumptions and source shortcomings, then select the smallest missing local
delta.

The default adoption mode is `adapted_mechanism`: preserve the useful mechanism
while translating it into existing CBH concepts and improving source gaps.
Select `direct_reuse` only when the original artifact is already usable,
license-compatible, interface-compatible, and cheaper and safer than an
adaptation. Otherwise classify it as rejected or deferred without creating a
new gate, schema, runtime, or technical-debt artifact solely because the source
is related.

## Skill Lifecycle Profile

`skill_lifecycle_profile` is mandatory for skill-layer work and subordinate to
the route. It does not itself authorize tools, memory writes, or claim
promotion.

Allowed values:

| Value | Meaning |
| --- | --- |
| `none` | No skill body should be loaded. |
| `listing_only` | Use skill name, meta-summary, route tags, and activation condition only. |
| `active_frame_required` | Load `SKILL.md` and only the support files needed for the current skill phase. |
| `release_receipt_required` | The skill phase should end by writing a compact `skill_release_receipt` and releasing large rendered skill body content where the host supports context GC. |
| `reactivate_from_receipt` | Resume by rereading current skill source files from the receipt's `resume_entry`, not by trusting stale compressed fragments. |

See [skill-lifecycle-contract.md](skill-lifecycle-contract.md).

## Feedback Loop Profile

`feedback_loop_profile` controls only the memory -> prediction ->
verification -> calibration loop. It is not a risk level and does not replace
`required_gates`; multiple gates still combine by union.

Allowed values:

| Value | Meaning |
| --- | --- |
| `none` | No feedback-loop work is selected. |
| `index_hint` | A reusable-error corpus may be relevant, but only a compact index hint should be exposed. |
| `record_candidate` | The task may write a compact CE candidate after verification; do not run the full loop just because a record is being created. |
| `prevention_review` | A selected CE, ERR/SOL, capsule, or decision record should actively prevent recurrence; run the loop over the selected payload only. |
| `explicit_cycle` | The user or task explicitly asks for memory -> prediction -> verification -> calibration; full loop review is allowed within normal evidence and memory budgets. |

This profile exists to prevent the loop from becoming a token sink. A common
error mention can set `index_hint`; explicit recording can set
`record_candidate`; only prevention wording or selected prevention memory should
set `prevention_review`.

## Memory Retrieval And Write Profiles

`hybrid_retrieval_profile` is subordinate to `memory_need`. It must never bypass
`memory_summary`, `_META_INDEX`, lane/category narrowing, memory isolation, or
source-monitoring fields.

Allowed values:

| Value | Meaning |
| --- | --- |
| `none` | No memory lookup is needed. |
| `meta_first_hybrid_enhancement` | Use meta-first retrieval, then add bounded retrieval terms, exact phrases, original-language keywords, Chinese character n-grams, English terms, and optional lexical ranking over the selected candidate set. |
| `meta_first_hybrid_required` | The task reads or writes reusable capsules, ERR/SOL records, common-error records, conversation state, or memory links; the adapter should expose the hybrid enhancement as an execution requirement, not a replacement retrieval stack. |

`memory_write_profile` is subordinate to `memory_mode`. It does not authorize
writing by itself.

Allowed values:

| Value | Meaning |
| --- | --- |
| `none` | No durable memory write/update is selected. |
| `context_complete_required` | Any durable memory write/update must include actor/source, action or decision, object/scope, time or version when relevant, provenance boundary, and non-applicable boundary when needed. |
| `strict_capsule_required` | Explicit reusable memory, conversation-memory, or cross-conversation writes must use source-preserving capsule shape and cannot write orphan fragments. |

For field-budget, silent classification, and delta-receipt rules, see [cost-control-contract.md](cost-control-contract.md). The short rule is: classify every task internally, but emit a field by default only when it can change the next action.

## Fallback Boundary Rule

Fallback routing is a last-resort ambiguity signal, not a broad nontrivial-task
classifier. If no deterministic R1-R5 rule matches:

- very short text stays R0 unless another rule fires;
- medium-length text needs a configured fallback term before low-confidence
  review is recommended;
- long unclassified text may request boundary review even without a fallback
  term, because the agent lacks a deterministic route.

This keeps phrases such as "what is the task?" from becoming low-confidence
governance work while still catching long, underspecified requests.

## Low-Cost Rule

Do not load all skills, all memory, or all history because this contract exists. The preferred expansion order is:

```text
L0 microkernel
-> router decision contract
-> one matching project or skill entry
-> one needed gate
-> one needed index
-> only matching payload
```

When raw host sessions are available and conversation continuity matters,
`module_need` may include `conversation_ledger_index`. That module reads the
ledger meta/index before opening selected segment or evidence-ref records. It
must not load a full raw transcript by default.

If the receipt is obvious from the current request, it can stay implicit. Keep R0-R5 labels internal by default. If the classification changes execution path, cost, permission, memory, external search, or claim wording, expose only that minimal boundary. If the user asks for debug or audit, expose the complete debug receipt.

## Proportional Engineering

Avoiding overengineering means keeping simple work direct; it does not mean
forcing complex work through a shallow workflow. Engineering rigor scales with
complexity, impact, uncertainty, durability, public exposure, cross-surface
coupling, and acceptance strictness. `minimum sufficient` limits unrelated
scope and repeated cost, not necessary reasoning depth, stages, or
verification.

Choose only the mechanisms that close an identified dependency, risk, claim,
or acceptance gap. Stage decomposition, interface and invariant definitions,
multiple evidence chains, independent review, parallel lanes, and stage-level
acceptance are available mechanisms, not a mandatory bundle. Engineering rigor
is orthogonal to R0-R5 and authorization: complexity cannot change the risk
label, grant permission, or expand a one-event approval.

## Observation Scope And Causal Attribution

`observation_scope_gate` is an input-side gate. It expands the observation
range when the task asks for framework definitions, global or cross-project
behavior, long-term trends, historical comparisons, model-change claims, or
phrases such as "always" / "since June 15" / "整体上" / "长期". It prevents the
agent from answering a global or historical question from only the current
chat window. It does not decide the causal label by itself.

`global_task_context_gate` is a task-context gate. It applies when a local fix,
local causal diagnosis, or narrow file edit may depend on the upstream goal,
active lane, current status table, file map, workflow state, or cross-step
constraints. For complex or multi-stage work, it also remains active at each
substantive stage boundary where a local step crosses an interface, changes
shared state, or affects downstream acceptance.

The gate uses the smallest task-relevant architecture, workflow, or evidence
boundary. At a substantive stage boundary, map the stage to its global output,
upstream assumptions, downstream consumers, interfaces, invariants, side
effects, and exit evidence. Reuse an unchanged mapping; reread or remap only
when evidence, scope, dependencies, risk, or acceptance changes. This is not a
per-tool-call check and is not permission for a full repository, history, rule,
or capability scan. A locally plausible result is invalid when it conflicts
with the component's system role, a downstream consumer, a later stage, or
final acceptance.

`causal_attribution_gate` is an output-side or draft-final gate. It reviews
high-risk assertion patterns before display:

- abstract system/framework/mechanism subject + causal predicate + global effect;
- time range + stability assertion;
- single sample, case, or local observation wording + generalization;
- origin or formation-path wording mixed into a mechanism definition.

It should not fire on ordinary local reasoning merely because the text contains
"because", "therefore", "导致", "说明", or "所以". Scoped local statements such
as "in this task's third turn, missing memory anchors caused this output drift"
should pass unless they omit needed scope or hypothesis language.

The attribution levels are intentionally small:

| Level | Meaning | Boundary |
| --- | --- | --- |
| `mechanism_property` | Structural property of the mechanism, independent of local origin. | May be used as a formal mechanism statement when grounded in the design itself. |
| `empirical_record` | Local observation, formation path, or case/example record. | Must keep scope and sample boundaries; it is not proof. |
| `causal_hypothesis` | Directional causal explanation without sufficient controls. | Must be marked as a hypothesis. |
| `validated_causality` | Causal claim supported by controls, repeatable evidence, or a concrete validation chain. | May be stated as causality only with evidence boundaries. |

Information visibility is not part of this gate. Public/private export,
privacy, and local-only release decisions remain in the separate
public/private boundary gate.

The feedback loop is a separate experience-deposition loop:

```text
memory -> prediction -> verification -> calibration
```

Use `feedback_loop_gate` when the task or selected memory record is meant to
prevent repeated mistakes, guide future route decisions, or verify whether a
previous prediction matched later behavior. This is primarily an internal memory
reuse requirement: if a selected reusable capsule, CE record, ERR/SOL pair, or
decision record has a `feedback_loop` or recurrence-prevention role, the agent
should apply the loop without waiting for the user to request prediction. User
corrections and explicit requests still force the same gate.

For common-error records, distinguish lookup, prevention, and write intent.
Reading a record should select `memory_need: common_error_corpus`,
`memory_mode: read`, and `feedback_loop_profile: index_hint`. Prevention wording
or a selected prevention record may add `feedback_loop_gate` with
`feedback_loop_profile: prevention_review`. Durable CE writes require explicit
record/write intent or a verified post-tool issue capture and should use
`feedback_loop_profile: record_candidate` unless a full cycle was explicitly
requested.

Do not trigger it for ordinary chat, one-off notes, static manuals, or task
states that are not meant to shape future behavior. The feedback loop and the
causal attribution gate can both apply to one event, but they do not authorize
each other. A feedback prediction remains a hypothesis until verified; a causal
hypothesis remains a hypothesis until controlled evidence supports it.

## Research Triage Gate

`research_triage_gate` is a method-selection gate. It applies before proposing
target functions, reinforcement-learning loops, automated evaluators, semantic
judges, causal evaluators, benchmark conclusions, or claims that a system can
self-correct toward an objective optimum.

The gate asks three questions:

1. Does this task type have an external verification signal that is independent
   of human preference and not self-circular?
2. If such a signal exists, is it a mechanical judge or human judgment wrapped
   in a technical surface?
3. If no independent verifier exists, what governance structure should manage
   the uncertainty?

The possible route outcomes are `mechanical_verifier_path`,
`verifier_audit_path`, `governance_path`, and `mixed_path`. See
[research-triage-three-questions.md](research-triage-three-questions.md).

This gate is not for ordinary implementation, direct lookup, or local
debugging. It is for choosing the research or evaluator path itself.

## Direct Outcome First Gate

`direct_outcome_first_gate` is a goal-fidelity gate for bounded user-visible
mutations such as a local UI/UX, interaction, form, layout, or animation
change. It is not a ban on reading context: the agent may first read the
smallest amount needed to locate and safely edit the requested surface. After
that read, the first substantive mutation must directly affect that surface.

Creating a protective mechanism, schema, policy, framework, generalized
abstraction, or system-wide refactor does not satisfy the gate by itself. The
router emits a `direct_outcome_first_instruction` and a
`direct_outcome_first` action binding at
`before_first_substantive_mutation`. Scope may expand only when at least one
condition has reviewable support:

- the direct implementation failed;
- acceptance objectively spans multiple surfaces;
- safety or data integrity blocks the direct path; or
- the user explicitly requested systemic or reusable scope.

Retire the gate after the direct result passes acceptance, or after evidence
shows that one declared expansion condition applies. Explanation-only requests
and explicitly systemic design/refactor requests are negative controls.

## Issue Prevention Gates

Issue-prevention gates are narrow recurrence guards for failure classes that are
too specific to be risk levels and too operational to be only prose advice. They
are additive `required_gates`; they do not authorize memory writes, external
search, public release, or R5 actions by themselves.

Current generic gates:

| Gate | Use when | Required behavior |
| --- | --- | --- |
| `exact_anchor_preservation_gate` | DOI, version, tag, hash, path, client-support status, deployment status, or lane id appears in a public, release, citation, handoff, or memory surface. | Preserve exact strings from source or stop for verification; do not normalize from memory. |
| `current_status_table_evidence_gate` | A current/status/latest table is built from notes, drafts, stale handoffs, or unverified memory. | Verify mutable fields, rename them as note/draft values, omit them, or show cell-level verification debt. |
| `unknown_memory_reference_gate` | The user refers to a forgotten prior term, storage point, event, or decision. | Run bounded meta-first memory lookup before providing a named answer; report no hit if none is found. |
| `hallucination_detection_anchor_gate` | The task asks whether an answer is hallucinated, grounded, complete, unsupported, or a non-answer. | Use source labels or requested-output contract anchors; separate unsupported, incomplete, and non-answer outcomes. |
| `global_task_context_gate` | A local fix, local causal diagnosis, or narrow edit may depend on upstream context, or a substantive stage of complex work must stay aligned with the task-relevant system. | Map the stage to the smallest relevant architecture/workflow/evidence boundary; preserve upstream/downstream roles and invariants, reuse unchanged mappings, and remap only after a material trigger. |
| `novel_recurrence_candidate_gate` | A new-looking failure resembles a prior failure shape but does not match a known narrow gate. | Run only a lightweight re-evaluation and mark candidate recurrence/global issue status; upgrade to global, causal, feedback, memory-write, or R5 paths only when that review justifies it. |
| `linked_surface_sync_gate` | A router, policy, AGENTS, memory, ledger, adapter, or profile-field change may need matching updates across supported runtime surfaces. | Enumerate target supported surfaces and explicit non-target surfaces before editing; update only user-requested or active supported surfaces, and leave legacy or unnamed adapters as candidate debt. |
| `public_private_surface_gate` | A public README, docs, release note, citation, package, or other public artifact is being prepared or reviewed. | Scan for private or local-only traces before publishing; keep public surfaces generic unless disclosure is explicitly authorized. |
| `self_report_log_grounding_gate` | The agent describes what it previously checked, ran, verified, skipped, or failed. | Ground the statement in command/tool/session logs when logs exist; otherwise state that no log evidence exists. |
| `root_cause_cleanup_gate` | Incident analysis asks what went wrong or how to prevent recurrence. | Prefer logs, diffs, hashes, and source records; keep subjective intent unknown unless proven; produce cleanup/prevention candidates. |
| `lane_ownership_gate` | A project or client is mentioned in evidence but may not own the memory record. | Determine ownership from task target, provenance, impact surface, active lane, and user authorization; default to link-only when unresolved. |

## R5 Candidate And Context Rule

R5 trigger terms are a recall layer, not the final decision. Terms such as
`delete`, `commit`, `push`, `删除`, and `提交` may appear inside documentation,
examples, quoted text, negated requests, or non-git phrases such as "submit a
report". The router should record these hits under `risk_candidates` first,
then decide whether the context is an actionable high-risk operation.

Use this low-cost order:

```text
lexical trigger hit
-> risk_candidates
-> context decision
-> promote to R5 only when the surface is actionable
```

Expected behavior:

| Request shape | Expected route |
| --- | --- |
| "do not delete anything" | Not R5; record a negated R5 trigger. |
| "trigger list contains commit push 删除 提交" | Not R5; record R5 candidates with `documentation_or_discussion`. |
| "提交报告" | Not R5; treat as non-action or artifact/report context. |
| "删除旧 release" | R5; concrete delete action requiring confirmation. |
| "git push" or "commit changes" | R5; concrete git action requiring confirmation. |

The route labels concrete `git push`, `Remove-Item`, and other R5 actions for
scope review. Existing user authorization remains valid across the necessary
steps of its agreed task; a receipt or phase change does not consume it. Request
missing authorization only for a new scope, new external effect or irreversible
operation that is not already authorized. The host model and permission system
own that decision. ACCF does not certify an authorized operation as safe, and
the optional nonblocking behavior hook does not grant or revoke authorization.

## Composite Task And Scope Reassessment Rule

Some user requests contain more than one task shape. A request may start as a
report review, then also ask to update public docs, add tests, change an adapter,
record an issue, or absorb a mechanism. Do not classify these by the first or
narrowest phrase.

Use this order:

```text
split obvious sub-intents
-> classify each sub-intent
-> keep the highest risk level
-> union the required gates
-> expose only the action-changing boundary
```

Examples:

| Request shape | Route |
| --- | --- |
| "Read this report and summarize it." | R2 artifact or R1/R2 read/report route. |
| "Read this report and update public docs/tests from it." | R3 governance/docs route plus artifact and claim gates. |
| "Check whether a feature exists." | R1 read-only inspection. |
| "Check whether it exists, then implement it if missing." | R3 implementation route; do not stop at R1. |
| "Self-check README/docs wording for boundary errors." | R1 read-only inspection until an edit is selected. |
| "Self-check README/docs wording; if needed, update README." | R3 governance/docs route because the task contains a possible edit path. |
| "Discuss an external project and absorb useful mechanisms." | External source intake plus R3 candidate integration route. |
| "Record these issues and classify them." | Memory record route plus lane/sensitivity checks. |

Trigger words such as "also", "plus", "and", "not only", "but also",
"同时", "还有", "另外", "以及", "不只是", "不仅", "并且", "组合",
or "多个问题" should set a scope-reassessment marker. This marker does not make
the task expensive by itself; it tells the agent to avoid under-routing and to
re-evaluate before editing, writing memory, searching externally, or making a
strong claim.

Read-only self-checks are allowed to start as R1. If the self-check discovers a
needed public docs, README, policy, router, adapter, test, or framework-behavior
change, the next action must be reclassified as R3 before editing. Do not solve
this by promoting every self-check to R3; the promotion is tied to the selected
edit path.

## Debt Hygiene Gate

Use `debt_hygiene_gate` when self-check confirms substantial memory pollution,
target pollution, dirty-tree debt, or accumulated technical debt in the current
project or long conversation. This gate is a cleanup discipline, not a mandate
to clear every debt item immediately.

Execution order:

```text
inventory
-> group by surface and urgency
-> clean must-clean-now items
-> defer acceptable items as candidate_technical_debt
-> list candidate debt in the next cleanup review
```

Definitions:

| Term | Meaning |
| --- | --- |
| `memory_pollution` | Durable memory, ledger, or capsule content is in the wrong lane, wrong scope, or carries stale/overbroad meaning. |
| `target_pollution` | Public docs, local rules, adapter files, or project surfaces contain content meant for another audience or lane. |
| `dirty_tree_debt` | The worktree contains unrelated or partially finished changes that obscure the current task boundary. |
| `technical_debt` | Known design, test, implementation, or documentation debt. It can remain when bounded and marked. |
| `candidate_technical_debt` | Deferred debt with owner/surface, reason for deferral, next-review trigger, and cleanup condition. |

Do not use this gate to authorize destructive cleanup. Deleting, resetting,
archiving, or rewriting still follows the normal R5, archive, and local-patch
rules.

## Trigger Promotion Gate

Do not add every successful task phrase to the long-lived trigger list. Promote
a trigger term only when all of these are true:

- it describes a recurring routing class, not one user's one-off wording;
- it changes the needed gates, risk, memory, search, claim, or permission path;
- it is not already covered by a generic edit, report, search, memory, or risk
  trigger;
- it can be tested with at least one positive and one non-applicable example.

One-off phrases should remain in the task receipt, changelog, issue note, or
candidate maintenance proposal. They should not become deterministic router
policy until repeated evidence or explicit maintainer approval shows that the
term is durable.

## Dynamic Re-Evaluation Triggers

Re-run the contract when any of these appears:

```text
new evidence
missing file
tool error
scope change
user correction
cross-project term
public/internal audience ambiguity
version/currentness claim
GitHub or open-source mechanism
cost or risk escalation
before a strong factual claim
before R5 action
before long-term memory write
before conversation checkpoint or cross-conversation memory update
before memory continuation, merge, archive, or link creation
```

## Source-Grounded Influences

This contract adapts several established ideas as lightweight design influences:

| Influence | Absorbed mechanism | Boundary |
| --- | --- | --- |
| Open Policy Agent | Separate the decision point from the execution/enforcement path. | This framework is not OPA and does not require Rego. |
| Kubernetes admission control | Run checks before critical persistence or execution boundaries. | Only selected hard gates should intercept; ordinary tools stay cheap. |
| OpenTelemetry sampling | Control emitted detail to balance usefulness and overhead. | Receipt profiles are not telemetry sampling; they only decide how much routing context to expose. |
| LangChain middleware | Use lifecycle hooks or wrap-style interception for cross-cutting concerns. | The framework stays runtime-neutral and does not require LangChain. |
| LlamaIndex and Haystack routers | Select a candidate tool, route, or retrieval path from metadata instead of opening everything. | Router output is a control receipt, not an autonomous success claim. |
| OODA loop | Reassess when the situation changes. | Reassessment is trigger-based, not continuous deliberation. |
| Cynefin-style sensemaking | Match response style to context uncertainty. | The public contract uses simple fields instead of complex domain taxonomy. |
| FMEA | Think about likely failure modes before changing a process. | Only high-risk or repeated failure modes need detailed records. |
| RACI-style role clarity | Separate public, local maintainer, project, and current-chat audiences. | The framework does not add organization-heavy role management. |

External references are source-prior design inputs. They do not prove this framework works in a target agent until the adopting environment runs local smoke checks.
