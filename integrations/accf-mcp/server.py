"""ACCF-MCP server: exposes ACCF memory/handoff/protocol/routing capabilities as MCP tools.

Run (stdio):  python server.py
Env:          ACCF_HOME  — data home (default ~/.accf-mcp)

Before changing behaviour here, read the design contracts under docs/ in the
ACCF source repo (architecture.md, non-goals.md, context-recall.md,
conversation-memory-lane.md, router-decision-contract.md). The tool surface is
derived from them, and non-goals.md is what keeps this server from growing a
second memory backend.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

import accf_core
import accf_clock

_NATIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native")
if os.path.isdir(_NATIVE_DIR):
    sys.path.insert(0, _NATIVE_DIR)
try:
    import accf_native
    _NATIVE_ERROR = ""
except Exception as _exc:  # native bridge is optional; never break the gateway
    accf_native = None
    _NATIVE_ERROR = f"{type(_exc).__name__}: {_exc}"

mcp = FastMCP(
    "accf",
    instructions=(
        "ACCF: source-preserving memory + task continuity + capability routing. "
        "Markdown files are the source of truth; SQLite index accelerates recall. "
        "Prefer accf_recall (cheap, ~hundreds of tokens) over reading memory files directly; "
        "use accf_dig to load full sources only when needed. Write handoffs with unknowns+"
        "disconfirmation so the next session can detect a wrong direction. "
        "accf_now is a glance, not a record: one line, no side effects — call it when "
        "the time frozen in context may be stale. Stored memories already carry their "
        "own 'age' via accf_recall/accf_handoff_read, so never do date arithmetic. "
        "This host keeps its own memory, so ACCF does not duplicate it: accf_native_recall "
        "reads the host's native memory files and past transcripts read-only, and accf_recall "
        "falls back to that channel automatically when its own store has nothing above the "
        "relevance gate. Native scores are lexical and are NOT comparable to accf_recall's cosine."
    ),
)

core = accf_core.AccfCore()


def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False, indent=1)


def _annotate_age(item: dict, key: str = "created") -> None:
    """Stamp a memory item with how long ago it was written. Memories are read as
    history, so staleness must be visible; the value is computed at read time from
    the live clock, never from a timestamp frozen in context."""
    try:
        a = accf_clock.age(item.get(key))
        item["age"] = a["age_human"] + (" (future)" if a.get("future") else "")
    except Exception:
        pass


def _native_fallback(query: str, k: int, excerpt_chars: int = 300) -> dict | None:
    """When ACCF's own store has nothing above the relevance gate, delegate the
    lookup to the host's native memory instead of returning an empty answer. This
    is a hand-off, not a merge: the native channel keeps its own score scale and
    says so, so the two are never silently compared."""
    if accf_native is None:
        return None
    try:
        res = accf_native.recall(query, k=k)
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    hits = res.get("hits") or []
    if not hits:
        return {"status": res.get("status", "empty"), "note": res.get("note", ""),
                "sources_searched": (res.get("index") or {}).get("sources")}
    return {
        "status": res.get("status"),
        "channel": "host_native_memory",
        "sources_searched": (res.get("index") or {}).get("sources"),
        "score_semantics": res.get("score_semantics"),
        "hits": [{**{f: h[f] for f in ("channel", "handle", "source", "score",
                                       "timestamp_iso", "reuse_policy") if f in h},
                  "excerpt": (h.get("excerpt") or "")[:excerpt_chars]}
                 for h in hits],
        "next": "accf_native_recall for fuller excerpts; accf_native_read(handle) for the original text.",
    }


@mcp.tool()
def accf_save(title: str, content: str, kind: str = "fact", source: str = "",
              tags: list[str] | None = None, disconfirmation: str = "") -> str:
    """Save a memory entry (kind: fact|episode). Markdown file + hybrid index.
    source = provenance pointer (URL/path/id). disconfirmation = the observable
    condition under which this memory should be considered overturned (recommended)."""
    return _j(core.save(title, content, kind, source, tags, disconfirmation))


@mcp.tool()
def accf_save_many(items: list[dict]) -> str:
    """Bulk-save memory entries: batched embedding (bounded memory) + one
    transaction. Use for ingesting logs/history at scale (thousands+).
    Each item: {title, content, kind?, source?, tags?, disconfirmation?, task_id?}.
    Returns ids; per-item failures never abort the batch."""
    return _j(core.save_many(items))


@mcp.tool()
def accf_recall(query: str, k: int = 5, kinds: list[str] | None = None,
                min_score: float | None = None, native_fallback: bool = True) -> str:
    """Hybrid recall over memory/handoffs/protocols. Three channels fused by RRF
    (k=60), with deliberately different coverage:
      lexical  — bigram TF-IDF postings, TITLE only
      semantic — bge-small-zh embedding, TITLE only
      fts      — FTS5 over title + full body
    A body-only query therefore lands via the fts channel; check
    channels.lexical/semantic/fts and *_scope to see which one answered.
    Returns top-k titles with provenance pointers — cheap in tokens.
    Each hit carries 'age' (how long ago it was written, computed at read time), so
    recency is visible without any date arithmetic.
    Gated: candidates whose embedding cosine falls below min_score (default
    accf_core.RECALL_MIN_COS) are dropped, so an EMPTY hits list means nothing
    relevant is stored — not that the search failed. In that case report the gap
    instead of substituting weaker candidates. (With no vector channel there is no
    gate — channels.gate_applied says which case you are in.)
    hits[].cosine is the only absolute relevance signal; hits[].score (fused RRF)
    and hits[].rank are ranking quantities, not similarities.
    kinds filter: fact|episode|protocol|handoff.
    native_fallback (default on): when this store yields nothing above the gate,
    the lookup is handed off to the host's own memory/transcripts and returned
    under 'native_fallback' — different (lexical) score scale, never merged."""
    kw = {} if min_score is None else {"min_score": min_score}
    out = core.recall(query, k, kinds, **kw)
    for h in out.get("hits", []) or []:
        _annotate_age(h)
    if native_fallback and not out.get("hits"):
        fallback = _native_fallback(query, k)
        if fallback is not None:
            out["native_fallback"] = fallback
    return _j(out)


@mcp.tool()
def accf_episodes(indicators: list[int], k: int = 6, limit: int = 4, query: str = "") -> str:
    """Evidence-gated episode retrieval for the laya_judge advisory loop:
    hybrid recall (lexical + vector via RRF) -> content gate (only logs holding
    successful attempts for this exact context survive) -> freshest-version
    selection per world. status: linked|empty|no_log — on empty do NOT fall
    back to unrelated history (it would poison the judge); record new attempts
    instead. Output 'episodes' feeds laya_judge 'episodes' directly."""
    return _j(core.episodes(indicators, k=k, limit=limit, query=query))


@mcp.tool()
def accf_backfill() -> str:
    """Rebuild secondary indexes (embeddings, vector tables, token postings,
    full-body FTS) for active entries whose index row is missing, and purge index
    rows whose entry no longer exists. Idempotent — safe to re-run at any time;
    cheap when nothing is missing. Every issue accf_validate reports about the
    index is repairable by this call."""
    return _j(core.backfill())


@mcp.tool()
def accf_dig(entry_id: str) -> str:
    """Load the full original content of one entry by id (two-stage: recall -> dig)."""
    return _j(core.dig(entry_id))


@mcp.tool()
def accf_handoff_write(task_id: str, goal: str, stage: str, next_action: str,
                       unknowns: list[str], disconfirmation: str = "",
                       evidence: str = "") -> str:
    """Write a task-state handoff (supersedes previous handoff of same task_id).
    unknowns = named knowledge gaps; disconfirmation = what would prove the
    current direction wrong. Both keep the next session honest."""
    return _j(core.handoff_write(task_id, goal, stage, next_action, unknowns,
                                 disconfirmation, evidence))


@mcp.tool()
def accf_handoff_read(task_id: str = "") -> str:
    """Read the latest handoff (by task_id) or the 10 most recent. Each handoff
    carries 'age' (real-time, computed at read time from its 'created' timestamp)
    so the reader sees how stale the carried state is — the live clock replaces any
    frozen timestamp as the reference for 'now'."""
    out = core.handoff_read(task_id)
    for h in out.get("handoffs", []) or []:
        _annotate_age(h)
    return _j(out)


@mcp.tool()
def accf_protocol_add(statement: str, context_tags: list[str],
                      evidence_ids: list[str], allow_dangling: bool = False,
                      distilled: bool = False) -> str:
    """Record a verified lesson as a hash-chained protocol: 'In context X, do Y'.
    statement should embed the context. evidence_ids = memory/handoff ids backing
    it; they are verified against the store, and a dangling id refuses the write
    (pass allow_dangling=True to record anyway — validate() keeps reporting it).
    distilled=True marks a one-line generalization of raw episodes — such entries
    are withheld from accf_protocol_match unless explicitly requested, because
    early textual distillation over-constrains the search space (Dream-RSI,
    arXiv:2609.14858: semantic guidance "consistently underperforms its
    unguided counterpart under equal budgets")."""
    return _j(core.protocol_add(statement, context_tags, evidence_ids,
                                allow_dangling=allow_dangling, distilled=distilled))


@mcp.tool()
def accf_protocol_match(context_tags: list[str], include_distilled: bool = False) -> str:
    """Deterministically resurface protocols whose context_tags overlap the
    current task context. Call at task start with 2-5 descriptive tags.
    Distilled protocols are held back unless include_distilled=True."""
    return _j(core.protocol_match(context_tags, include_distilled=include_distilled))


@mcp.tool()
def accf_sandbox(indicators: list[int], k: int = 8) -> str:
    """Dreaming sandbox: replay candidate actions against the raw episodes held
    in memory — history itself is an exact simulator of the search space it
    reached, no learned world model needed (Dream-RSI, arXiv:2609.14858).
    Untried candidates stay neutral so no single success locks in a strategy.
    Replay scores prune candidates; real outcomes remain the arbiter.
    Advisory output only."""
    return _j(core.sandbox(indicators, k=k))


@mcp.tool()
def accf_route(task_description: str) -> str:
    """Route a task to known skills/plugins/MCP tools/scripts from the capability
    map (keyword + semantic match). Preserves ACCF state contract: configured /
    exposed / authenticated / callable / action_covered."""
    return _j(core.route(task_description))


@mcp.tool()
def accf_register_tool(name: str, kind: str, description: str, match: list[str],
                       command: str = "") -> str:
    """Register/update a capability in the routing map (auto-updates the map).
    kind: mcp|skill|script|note. For script kind, command = fixed template with
    {args} position; only these are executable via accf_call."""
    return _j(core.register_tool(name, kind, description, match, command))


@mcp.tool()
def accf_call(name: str, args: str = "", timeout: int = 60) -> str:
    """Execute a whitelisted local script registered in the capability map
    (kind=script). command template is fixed at registration; args are appended."""
    return _j(core.call(name, args, timeout))


@mcp.tool()
def accf_now() -> str:
    """Look at the clock — one line: current local date/time with UTC offset.

    A glance, not a record: no side effects, nothing written, nothing to analyse.
    Call it whenever the conversation's own timestamps may be stale, then take the
    returned time as 'now'. Reading how old a *stored* memory is needs no call here
    — accf_recall / accf_handoff_read already carry an 'age' annotation."""
    return accf_clock.anchor()


@mcp.tool()
def accf_native_recall(query: str, k: int = 5, channels: list[str] | None = None) -> str:
    """Read the HOST's own memory, read-only — this is the bridge that makes ACCF
    work on harnesses that already keep their own memory (WorkBuddy: per-workspace
    memory/*.md, user MEMORY.md/USER.md, 重大问题记录.md, plus past transcripts).

    Two channels, two score scales, never merged:
      memory  — native memory files (lexical index over the .md sources)
      history — past conversation transcripts (per-turn question+answer)
    'score' is a LEXICAL keyword-overlap weight in both; it is a ranking
    quantity, NOT a similarity, and NOT comparable to accf_recall's cosine.
    An empty hits list with status=no_relevant_native_memory means nothing
    relevant is stored — not that the lookup failed. Nothing is written back.
    channels: ["memory"], ["history"], or both (default)."""
    if accf_native is None:
        return _j({"status": "unavailable", "error": _NATIVE_ERROR,
                   "hint": "the native bridge is optional; ACCF still works without it"})
    return _j(accf_native.recall(query, k=k, channels=channels))


@mcp.tool()
def accf_native_read(handle: str, offset: int = 0, limit: int = 6000) -> str:
    """Load the original text behind an accf_native_recall handle (sha256-verified
    against the live source; a changed source fails closed with source_changed).
    Read-only: nothing is rewritten. Use offset to page long records."""
    if accf_native is None:
        return _j({"status": "unavailable", "error": _NATIVE_ERROR})
    try:
        return _j(accf_native.read(handle, offset=offset, limit=limit))
    except Exception as exc:
        return _j({"status": "error", "error": f"{type(exc).__name__}: {exc}"})


@mcp.tool()
def accf_native_status() -> str:
    """What native memory ACCF can currently see: source files/bytes by directory,
    index freshness, transcript count, and the declared boundaries (no writes to
    host memory, no model, no network, no third-party deps). Call this to tell
    'nothing stored' apart from 'bridge not configured'."""
    if accf_native is None:
        return _j({"status": "unavailable", "error": _NATIVE_ERROR})
    return _j(accf_native.status())


@mcp.tool()
def accf_validate() -> str:
    """Mechanical validation: file/index consistency, hash drift, protocol chain
    integrity, orphan files. Returns stats + issue list."""
    return _j(core.validate())


if __name__ == "__main__":
    mcp.run(transport="stdio")
