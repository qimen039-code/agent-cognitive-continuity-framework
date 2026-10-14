"""ACCF-MCP server: exposes ACCF memory/handoff/protocol/routing capabilities as MCP tools.

Run (stdio):  python server.py
Env:          ACCF_HOME  — data home (default ~/.accf-mcp)
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

import accf_core

mcp = FastMCP(
    "accf",
    instructions=(
        "ACCF: source-preserving memory + task continuity + capability routing. "
        "Markdown files are the source of truth; SQLite index accelerates recall. "
        "Prefer accf_recall (cheap, ~hundreds of tokens) over reading memory files directly; "
        "use accf_dig to load full sources only when needed. Write handoffs with unknowns+"
        "disconfirmation so the next session can detect a wrong direction."
    ),
)

core = accf_core.AccfCore()


def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False, indent=1)


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
def accf_recall(query: str, k: int = 5, kinds: list[str] | None = None) -> str:
    """Hybrid recall (vector + lexical + FTS via RRF) over memory/handoffs/protocols.
    Returns top-k titles with provenance pointers — cheap in tokens.
    kinds filter: fact|episode|protocol|handoff."""
    return _j(core.recall(query, k, kinds))


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
    """Rebuild secondary indexes (embeddings, vector tables, token postings)
    for entries written before those indexes existed. Idempotent — safe to
    re-run at any time; cheap when nothing is missing."""
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
    """Read the latest handoff (by task_id) or the 10 most recent."""
    return _j(core.handoff_read(task_id))


@mcp.tool()
def accf_protocol_add(statement: str, context_tags: list[str],
                      evidence_ids: list[str]) -> str:
    """Record a verified lesson as a hash-chained protocol: 'In context X, do Y'.
    statement should embed the context. evidence_ids = memory/handoff ids backing it."""
    return _j(core.protocol_add(statement, context_tags, evidence_ids))


@mcp.tool()
def accf_protocol_match(context_tags: list[str]) -> str:
    """Deterministically resurface protocols whose context_tags overlap the
    current task context. Call at task start with 2-5 descriptive tags."""
    return _j(core.protocol_match(context_tags))


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
def accf_validate() -> str:
    """Mechanical validation: file/index consistency, hash drift, protocol chain
    integrity, orphan files. Returns stats + issue list."""
    return _j(core.validate())


if __name__ == "__main__":
    mcp.run(transport="stdio")
