# Embedded Agent Harness

A source-preserving memory, task-state and mechanical-validation layer for
agent workflows. The host model selects relevant sources and actions; optional
consumers return bounded context and evidence. The model plans, uses tools,
handles errors and writes the final answer.

Available integration chain (select only the parts needed by the task):

```text
root AGENTS.md microkernel
-> host model chooses the next action
-> optional deterministic intake router and routing receipt
-> event-triggered re-evaluation
-> optional project router / project AGENTS / project memory
-> static knowledge index / selected manual page, if project navigation is needed
-> memory meta summary / category index / matching capsule, if memory is needed
-> bounded action consumer context returned to the model agent
-> optional dormant-by-default task continuity capsule for tool/write/long tasks
-> optional engineering-execution receipt for matched architecture work
-> optional nonblocking current-candidate correction
-> final claim and memory boundary check
```

Design boundaries:

- Risk rules are additive: keep the highest risk label, but return the union of all gates matched by the task.
- R5 trigger terms are candidates first. The router promotes them to R5 only when context shows an actionable delete, git, install, login, permission, network/proxy, sensitive-transfer, or memory-write operation.
- `R4` includes `R3` change and claim gates, plus external research and verification gates.
- If no deterministic risk rule matches, fallback review is reserved for medium-length text with fallback terms or long unclassified text. Short ordinary questions stay cheap R0 unless another rule fires.
- `GLOBAL` memory is manual-only by default.
- Ordinary tasks use exposed tools and valid sources directly. Routing receipts are optional compatibility interfaces; they do not mandate another approval or a fixed planning/testing sequence.
- User-visible edits should advance the requested surface after the necessary read. Broaden only when the goal, a concrete failure or an evidence gap requires it.
- R0-R5 labels describe action scope when the router is called. They do not grant or revoke permission. `debug_receipt` is for route diagnosis or an explicit full-receipt request.
- Receipt profiles keep runtime cost low: `compact_runtime` is used only when fields change the next action, `extended_governance` expands for public/framework/project-boundary work, and `debug_receipt` is only for route diagnosis or explicit full-receipt requests.
- Do not wrap every tool call. Invoke correction only for a current candidate whose surface and profile can be mechanically identified.
- Memory discovery may use metadata to locate sources; read known exact sources directly and expand the context needed for the current claim.
- `memory_source_hints` bind retrieval to exact active roots. `harness_action_consumer.py` promotes exact record or anchor matches into compact model context; bounded weaker candidates are returned to the host model for semantic reranking and do not demote an exact match into mandatory manual review.
- `action_bindings` describe work for the host model agent. They do not make ACCF an autonomous task runner and are not completion evidence until the matching model/tool path returns a receipt.
- `task_continuity.py` is a process-local continuity reducer and adaptive transport worker. For an active long/tool/write task it keeps one stable global goal ahead of verified progress, the latest local delta, resumable side tasks, and exact reusable-source refs. An unmatched turn preserves that frame for semantic review instead of becoming a new task: the host model can treat it as a global-goal delta, a bounded side conversation that is answered without replacing the global goal, or an explicit global replacement that still needs current-frame evidence. An explicit return resumes the preserved global task. It stays dormant for answer-only turns with no active frame, stores no chain-of-thought or authority, and requires a verified host adapter before its context can be called automatically model-visible.
- `engineering_execution.py` materializes only a routed engineering profile: tracer-bullet frontier and blocking edges, a reversible deep-module freeze/temporary-isolation probe, an invocation envelope that distinguishes a claim from a host-bound actual caller, or a two-adapter seam check. The isolation probe adapts the deletion thought experiment, and the seam criterion is adapted directly, from `mattpocock/skills` `codebase-design`; tracer-bullet delivery and invocation topology are local ACCF profiles. The probe itself performs no mutation and requires an external executor to restore state and produce an audit receipt. All receipts are advisory, nonpersistent, and grant no authority; a seam remains `candidate_real` until non-model producer hashes are trusted by the caller.
- Host adapters should explicitly request compact receipts. The raw PowerShell router keeps its legacy diagnostic default for callers that omit `ReceiptMode`; `-ReceiptMode compact` is the bounded runtime path. Adapter-facing task-continuity responses serialize the capsule and evidence entry once, while transport receipts expose evidence, control, and total budgets. Oversized model context is hash-bound and paged instead of silently truncated.
- Static knowledge retrieval is index-first: read `_STATIC_KNOWLEDGE_INDEX.md` before opening a project manual page, and treat static notes as `source_tag: static_knowledge` / `belief_status: source_prior` until checked.
- `behavior_correction_gate.py` returns a task-local receipt; `behavior_correction_hook.py` may return one verified `allow + updatedInput` rewrite for an accepted deterministic profile.
- `nested_tool_preflight.py` validates an already selected nested `shell_command` or `web__run` envelope when the native hook cannot observe it. It is advisory, stateless, and does not parse the surrounding JavaScript.
- `compact_failure_audit.py` reads bounded JSONL windows and emits strict, size-limited root/child failure summaries without treating an unverified path as bound evidence.
- `dangerous_delete_guard.py` is an on-demand delete-risk classifier only; it never grants authorization or installs a host denial hook.
- Ambiguity, verifier failure, unsupported host protocol, registry failure, or no match leaves the event unchanged. Correction never grants permission, denies, freezes, stores approval state, writes memory, or mutates policy.
- Helper gates may return structured decision receipts rather than host denial
  payloads. For protected high-risk actions, however, the governing model must
  stop before tool execution until exact human authorization exists. This is a
  model-layer pre-action gate; independent host execution blocking remains a
  separate, adapter-specific capability.
- Machine-local project roots should be loaded from `embedded_harness_policy.local.json` or `CBH_PROJECT_LANES_FILE`, not committed into the public runtime policy.

Mandatory model-layer pre-action control plane:

```text
routing receipt
-> execute the cheapest sufficient route
-> when routed, bind the first substantive mutation to the requested surface
-> re-evaluate only after trigger events
-> final claim/memory/version boundary check
-> optional nonblocking correction when a verified recurrence profile matches
```

Authorization is bound to one concrete event, one scope, and one use. It is
consumed by the approved operation and cannot be replayed or expanded. Human
authorization accepts the disclosed risk for that exact operation; it does not
certify safety or make ACCF responsible for the operation's consequences.

Receipt fields: task type, target surface, audience, project lane, risk level, semantic ambiguity, module need, memory need, memory mode, memory lane, memory source hints, action bindings, task continuity decision, engineering execution profiles, record intent, external need, claim risk, projectization decision, conversation memory decision, link intent, receipt profile, and required gates. Runtime adapters can expose `compact_runtime` by default and expand only for governance or debug cases.

If this control plane cannot be completed, the final response must say so and must not present the result as fully verified.

Mandatory search and learning decision matrix:

```text
official / authority source search
-> GitHub / open-source repository search when repo evidence is involved
-> general web cross-check when independent public context is needed
-> source-grounded learning intake for external mechanisms
-> local validation before strong adoption or success claims
```

Use this matrix when a task mentions current facts, latest versions, public products, policies, prices, laws, GitHub, open-source repositories, unfamiliar mechanisms, external architecture comparisons, or avoiding closed-door invention.
Do not treat local architecture maintenance as external research unless it includes external comparison or open-source learning.
Classify outside material as `fact`, `source_prior`, `hypothesis`, `inspiration`, `unverified_implementation_path`, or `not_applicable`.
Do not upgrade external reading into local validation unless local evidence, tests, reproduction, or a concrete project-specific proof path exists.

`harness_external_research_gate.ps1` now calls
`external_retrieval_strategy.py` to produce a task-local
`cbh.external_retrieval_receipt.v1`. It preserves the original query and exact
identifiers, selects canonical/source-native routes, separates targets and
facets, and keeps provider misses below verified absence. The receipt is a
plan, not proof of browsing: it performs no network access, blocking, policy
mutation, or durable memory write, and the host model agent remains the search
and claim owner.

Mandatory memory retrieval chain:

```text
meta summary / _META_INDEX / router manifest
-> category index / point index / outer_retrieval_surface
-> matching capsule / ERR-* / SOL-* payload
```

Recommended meta index fields: lane, scope, category, record type, status, retrieval terms, applies-when, does-not-apply-when, linked modules, linked records, and last-reviewed marker. Default lookup should open at most one meta index, one category index, and two payload records unless the task explicitly asks for a full audit or migration.

Memory recording is routed separately from memory reading. Use `common_error_corpus` for lightweight recurring error-and-solution samples with symptom, cause, applied solution, prevention, validation, and evidence. Use paired `ERR-*` / `SOL-*` records for explicit, repeated, or high-impact self-reflection incidents.

`task_memory_checkpoint.py` closes the bounded handoff from task working state to long-term project or conversation history. `prepare` is read-only and accepts only a fully verified retired `.cumcwork` task; it emits a hash-bound candidate that excludes plans, hidden reasoning, raw tool output, and authority. `promote` is a separate caller-owned memory-write action. It synchronously appends the canonical v3 record, checks the store, and proves exact meta-first retrieval before returning `searchable_ready`. ACCF does not run a background derivation queue for this path, and candidate creation never authorizes promotion.

Reusable memory capsules should carry source-monitoring fields: `source_tag` `belief_status` `confidence` `derived_from` `source_monitoring` `lifecycle` `belief_trace_summary`. The router decides whether memory is needed; the capsule schema preserves the source and status boundary after the route chooses to write or update memory.

Memory retrieval results used as reusable context should return these fields with the selected snippet: `source_tag` `derived_from` `belief_status` `confidence` `score_method`. If no numeric score was computed, use `score_method: none` and omit `score`.

Scripts:

```powershell
.\harness_intake_router.ps1 -TaskText "fix the build and run benchmark" -Cwd "<PROJECT_ROOT>" -ReceiptMode compact -OutputPath ".cbh-route.json" | Out-Null
python .\harness_action_consumer.py --route-file ".cbh-route.json" --receipt-mode compact --prompt '<USER_TASK>'
python .\task_memory_checkpoint.py prepare --workfile '<TASK.cumcwork>' --host-task-key-sha256 '<HASH>' --lane-id '<LANE_ID>' --lane-kind conversation
python .\task_memory_checkpoint.py promote --candidate-file '<CANDIDATE.json>' --expected-candidate-sha256 '<HASH>' --store-root '<V3_STORE_ROOT>'
python .\behavior_correction_gate.py --list-profiles
python .\behavior_correction_hook.py < pretool-event.json
'{"tool_name":"shell_command","arguments":{"command":"Get-ChildItem","workdir":"<PROJECT_ROOT>"}}' | python .\nested_tool_preflight.py
python .\dangerous_delete_guard.py --command 'Remove-Item -LiteralPath <EXACT_TARGET>' --cwd '<PROJECT_ROOT>'
.\validate_policy.ps1
.\harness_memory_isolation_gate.ps1 -ProjectLane EXAMPLE_PROJECT -RequestedPath "<PROJECT_ROOT>/.agent-memory/item.md"
.\harness_external_research_gate.ps1 -TaskText "check latest version"
python .\external_retrieval_strategy.py --task-text "核对 DOI 10.1145/3596512" --mode official_authority_source_search
python .\task_continuity.py --worker < task-events.jsonl
python -c "from engineering_execution import build_delivery_trace; print(build_delivery_trace([{'id':'slice','status':'pending'}]))"
.\harness_claim_schema_verifier.ps1 -ClaimJson '{"claim_type":"architecture_decision","source_type":"local_file","source_ref":"README.md","evidence_boundary":"whiteboard_smoke"}'
```

Policy authoring:

```powershell
python .\compile_policy_from_toml.py --check
python .\compile_policy_from_toml.py --output .\embedded_harness_policy.json
```

`embedded_harness_policy.authoring.toml` is for human-maintained high-churn
sections. `embedded_harness_policy.json` remains the runtime source consumed by
PowerShell, Bash, and Python adapters.

R5 confirmation is intentionally outside the correction hook. The model agent
must follow its governing instructions, and the host's native security boundary
remains authoritative for actual execution.

Bash counterparts:

```bash
bash ./bash/validate_policy.sh
bash ./bash/harness_intake_router.sh --task-text "fix the build and run benchmark" --cwd "<PROJECT_ROOT>"
bash ./bash/harness_memory_isolation_gate.sh --project-lane EXAMPLE_PROJECT --requested-path "<PROJECT_ROOT>/.agent-memory/item.md"
bash ./bash/harness_external_research_gate.sh --task-text "check latest version"
bash ./bash/harness_claim_schema_verifier.sh --claim-json '{"claim_type":"architecture_decision","source_type":"local_file","source_ref":"README.md","evidence_boundary":"whiteboard_smoke"}'
```

Bash scripts require `jq`. They are reference adapters, not a package-manager distribution.

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Gate completed, returned a nonblocking receipt, or performed a silent no-op. |
| Other | Standalone diagnostic/runtime error; never an authorization decision. |

Gate statuses:

| Status | Meaning |
| --- | --- |
| `pass` | The advisory check completed without an issue. |
| `correction_candidate` | A task-local profile matched and declares its required verifier. |
| `cross_reference_allowed` | Memory path is outside the active lane, but explicit cross-reference allowance was provided. |

Behavior correction has no hard-stop state. It either emits one mechanically
verified current-input rewrite or leaves the event unchanged.

Configure `embedded_harness_policy.authoring.toml` for high-churn trigger and threshold sections, then keep `embedded_harness_policy.json` synchronized for runtime use. Machine-local project lanes and memory roots should live in a private `embedded_harness_policy.local.json` overlay or the path named by `CBH_PROJECT_LANES_FILE`; use `embedded_harness_policy.local.example.json` as the shape. Promote trigger terms only for recurring routing classes that change gates or boundaries; keep one-off task wording out of long-lived policy.

## Limitations

- This harness is not a hard sandbox.
- Most gate results are caller-honored by default.
- The correction hook is nonblocking and cannot substitute for host authorization or sandboxing.
- Published adapters are local smoke-test references, not complete cross-device or cross-client compatibility guarantees.

## Source reading and optional context recall

`text_source_reader.py` provides scoped structural/lexical discovery and exact
source reads. A known small file can be read directly; repeated collections can
use disposable packed indexes, `search --compact`, `outline`, and
`read --scope unit|parent|file`. Scores locate candidates rather than prove
semantic relevance or absence. Original text, encoding and selected ranges
remain the evidence. `semantic_memory.py discover` returns compact candidates;
`read --record-id` expands a verified selected v3 record.

`context_recall.py` is an optional read-only consumer of configured native
memory and retained public user/assistant messages. Copy and adapt
`context_recall.local.example.json` into a private `context_recall.local.json`;
do not publish source roots, state databases or derived catalogs. Hooks require
compatible host configuration and fresh invocation tests. See
`../../docs/context-recall.md`. No host callback is installed by this package.
