"""Smoke test for accf_core (stdlib only). Run: python -B test_accf.py"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HOME = os.path.join(tempfile.gettempdir(), "accf-test-home")
shutil.rmtree(HOME, ignore_errors=True)

import accf_core

c = accf_core.AccfCore(HOME)
ok = 0
fail = 0

def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name}  {extra}")

print("== save ==")
r1 = c.save("sqlite-vec 索引层选型", "ACCF v2 索引层采用 SQLite FTS5 + 本地 n-gram 向量，零外部 API 依赖，中文用 bigram 分词。",
            kind="fact", source="accf-mcp/design-decision", tags=["index", "sqlite"], disconfirmation="若中文召回准确率 < 60% 则需换真 embedding 模型")
check("save fact w/ disconfirmation", r1["id"].startswith("fac-") and not r1.get("warning"))
r2 = c.save("GitHub 绑定结论", "qimen039-code 的 Trae GitHub 连接器实测可用，能看到 3 个公开仓库。", kind="fact",
            source="mcp_GitHub_search_repositories/user:@me", tags=["github", "trae"])
check("save fact w/o disconfirmation warns", bool(r2.get("warning")))
r3 = c.save("EverOS 混合检索", "BM25 keyword + vector ANN 并行，层级融合排序；Markdown 为事实源。", kind="fact",
            source="docs.evermind.ai/memory-pipeline", tags=["everos", "retrieval"])
r4 = c.save("会话: 讨论记忆项目", "对比了 OpenMemory、mcp-memory-service、EverOS、episteme，确定 ACCF v2 混合架构。", kind="episode",
            source="conversation/2026-10-14", tags=["session", "memory"])

print("== recall ==")
q = c.recall("中文向量召回 索引层", k=3)
check("recall hits", len(q["hits"]) >= 1, str(q))
check("recall top is sqlite entry", q["hits"] and q["hits"][0]["id"] == r1["id"], str(q["hits"][:1]))
check("recall fast", q["elapsed_ms"] < 500)
q2 = c.recall("GitHub connector binding", k=2)
check("recall lexical cross-language", q2["hits"] and q2["hits"][0]["id"] == r2["id"], str(q2["hits"][:1]))
q3 = c.recall("EverOS BM25 fusion retrieval", k=2)
check("recall english->english", q3["hits"] and q3["hits"][0]["id"] == r3["id"], str(q3["hits"][:1]))
tok = q["est_tokens"]
check("recall cheap (<300 tokens)", tok < 300, f"est={tok}")

print("== full-body FTS channel + gate policy ==")
# Regression: recall used to be title-addressed (postings = title only, embeddings
# = title only) and the FTS5 table was written on every upsert but never read, so an
# entry reachable only by its body was unreachable. These entries have titles that
# share no token with the queries below.
c.save("MCP 协议测试条目", "server.py stdio 握手后的 accf_recall 端到端验证。", source="test", tags=["mcp"])
c.save("索引层选型结论", "BM25 关键词 + 向量 ANN 并行，层级融合排序；Markdown 为事实源。", source="test", tags=["index"])
# Shares exactly two non-adjacent bigrams (完全 / 无关) with the noise query below:
# 2/8 coverage is below the channel's floor, so it must be rejected rather than
# reported as a hit. (A body containing the contiguous phrase 完全无关 would score
# 3/8 and legitimately pass — see the README's known-gap table.)
c.save("无关词测试条目", "完全独立的内容，与检索主题无关。", source="test", tags=["noise"])
body_q = c.recall("stdio 端到端", k=3)
check("body-only query reaches entry", body_q["hits"] and body_q["hits"][0]["title"] == "MCP 协议测试条目",
      str(body_q["hits"][:1]))
check("fts channel contributed", body_q["channels"]["fts"] >= 1, str(body_q["channels"]))
check("channels declare scope", body_q["channels"].get("lexical_scope") == "title"
      and body_q["channels"].get("fts_scope") == "title+body", str(body_q["channels"])[:200])
# Query-side single CJK characters are dropped by _query_tokens: they appear in
# almost every document and turn an unrelated query into a confident hit.
check("query tokens exclude single CJK chars",
      not [t for t in accf_core._query_tokens("完全无关的航天术语") if len(t) == 1])
check("query tokens keep a short CJK query usable",
      accf_core._query_tokens("书") == ["书"])
# The full-body channel's own strength floor: the fixture shares 完全 / 无关 (2 of 8
# query tokens) and must be rejected by _fts_rank itself...
_weak_titles = [c._get_row(e)["title"] for e in c._fts_rank("完全无关的航天术语", k=9)]
check("fts strength floor rejects the weak fixture", "无关词测试条目" not in _weak_titles, str(_weak_titles))
# ...while a real body match passes it.
_strong = [c._get_row(e)["title"] for e in c._fts_rank("stdio 端到端", k=9)]
check("fts strength floor accepts a real body match", "MCP 协议测试条目" in _strong, str(_strong))
# End-to-end: queries sharing no token with anything must return nothing. (A query
# like 完全无关 still surfaces via the *lexical* channel when no vector channel
# exists — the semantic gate cannot run, see channels.gate_applied.)
for noise in ("今天天气不错我们出去散步", "量子纠缠 超导 拓扑绝缘体"):
    nq = c.recall(noise, k=3)
    check(f"noise query returns nothing: {noise}", not nq["hits"], str(nq["hits"][:2]))

# Gate policy: the cosine gate is *semantic* — it must suppress the vector channel's
# fuzzy neighbours but not veto exact full-body token evidence, otherwise a
# title-only embedding would veto every body match on an unrelated-titled entry.
# test_accf.py runs without fastembed, so the gate-on situation is simulated.
_qv = c._query_vec
c._query_vec = lambda query: [1.0, 0.0]                       # pretend vectors exist
c._entry_cos_many = lambda eids, qv: {e: 0.01 for e in eids}  # everything below gate
gated = c.recall("stdio 端到端", k=3)
check("body match survives the gate", gated["hits"] and gated["hits"][0]["title"] == "MCP 协议测试条目",
      str(gated["hits"][:1]) + str(gated["channels"])[:160])
check("gate bypass is reported", gated["channels"]["gate_applied"]
      and gated["channels"]["gate_bypassed_by_exact_tokens"] >= 1, str(gated["channels"])[:240])
# A candidate only one channel puts forward stays gated: the exemption is for
# exact full-body tokens and for lexical+semantic agreement, never for a lone
# title-token match — otherwise the gate would only ever bind the semantic
# channel and would not be a gate at all. Pin _semantic_rank so it no longer
# vouches for the entry lexical matched, leaving that entry single-sourced.
_lex_probe = c._lexical_rank("GitHub connector binding", 9, None)
_sr = c._semantic_rank
c._semantic_rank = lambda query, k: [e for e in _sr(query, k)
                                     if e not in set(_lex_probe)]
gated_lex = c.recall("GitHub connector binding", k=2)
check("single-channel title-only candidate still gated out", not gated_lex["hits"],
      str(gated_lex["hits"][:2]))
c._semantic_rank = _sr
del c._query_vec, c._entry_cos_many
check("gate stubs removed", "_query_vec" not in c.__dict__ and "_entry_cos_many" not in c.__dict__)
check("original gate restored", c._query_vec.__func__ is _qv.__func__)
miss = c.validate()
check("no entry missing from full-body index", not any("full-body index" in i for i in miss["issues"]),
      str(miss["issues"]))

print("== backfill repairs index holes ==")
# Recall reads the full-body channel now, so backfill must be able to repair it —
# previously it wrote only embeddings/vec/postings and could never fix a missing row.
tmp_home = os.path.join(tempfile.gettempdir(), "accf-test-backfill")
shutil.rmtree(tmp_home, ignore_errors=True)
bc = accf_core.AccfCore(tmp_home)
b1 = bc.save("MCP 协议测试条目", "server.py stdio 握手后的 accf_recall 端到端验证。", source="test")
bc.db.execute("DELETE FROM fts WHERE id=?", (b1["id"],)); bc.db.commit()
check("hole is reported", any("full-body index" in i for i in bc.validate()["issues"]))
check("hole hides the entry", not bc.recall("stdio 端到端", k=3)["hits"])
bf = bc.backfill()
check("backfill repairs the row", bf["backfilled"] >= 1 and not any(
    "full-body index" in i for i in bc.validate()["issues"]), str(bf))
check("entry findable again", bc.recall("stdio 端到端", k=3)["hits"])
bc.db.execute("INSERT INTO fts(id,title,body) VALUES('ghost-1','幽灵','幽灵')")
bc.db.execute("INSERT INTO token_postings(token,id,weight) VALUES('幽灵','ghost-1',1.0)")
bc.db.commit()
check("orphan is reported", any("orphan" in i for i in bc.validate()["issues"]))
bf2 = bc.backfill()
check("orphan is purged", bf2["orphans_purged"] == 1 and "none" in bc.validate()["issues"][0], str(bf2))
bc.db.close()
shutil.rmtree(tmp_home, ignore_errors=True)

print("== dig ==")
d = c.dig(r1["id"])
check("dig returns original", "FTS5" in d.get("content", "") and d["meta"].get("disconfirmation"))
check("dig provenance", d["meta"].get("source") == "accf-mcp/design-decision")

print("== handoff ==")
h1 = c.handoff_write("task-accf-v2", "把互补能力合并进 ACCF 并做成 MCP", "实现核心引擎", "写 server.py 并测试", unknowns=["真实 embedding 未接入"], disconfirmation="若 accf_call 无法执行脚本则路由层设计失败")
h2 = c.handoff_write("task-accf-v2", "把互补能力合并进 ACCF 并做成 MCP", "测试中", "跑 test_accf.py", unknowns=[], disconfirmation="失败则回退")
hr = c.handoff_read("task-accf-v2")
check("handoff supersedes (only latest active)", len(hr["handoffs"]) == 1 and hr["handoffs"][0]["stage"] == "测试中", str(hr))
check("handoff epistemic fields", hr["handoffs"][0]["disconfirmation"] != "")
he = c.handoff_write("task-empty", "g", "s", "n", unknowns=[])
check("empty unknowns warns", any("unknowns" in w for w in he["warnings"]))

print("== protocol ==")
p1 = c.protocol_add("在 context 高于 80% 时，先写 handoff 再继续新探索", ["context", "session"], [r4["id"]])
p2 = c.protocol_add("依赖安装失败时先查沙箱写入限制再重试", ["pip", "sandbox", "windows"], [r1["id"]])
pm = c.protocol_match(["sandbox", "windows"])
check("protocol match by tags", pm["protocols"] and pm["protocols"][0]["id"] == p2["id"], str(pm))
v = c.validate()
check("chain head matches", v["chain_head_match"] and v["chain_ok"], str(v))
check("validate no issues", v["issues"] == ["none — store is consistent"], str(v["issues"]))

print("== routing ==")
rr = c.route("修复 github 上的 pull request 审查问题")
check("route finds github connector", rr["routes"] and rr["routes"][0]["name"] == "github-connector", str(rr["routes"][:2]))
rt = c.route("校验记忆库一致性")
check("route finds script", any(r["name"] == "accf-store-health" and r["callable"] for r in rt["routes"]), str(rt["routes"][:2]))
rg = c.register_tool("web-search", "mcp", "Web search via Trae", ["search", "web", "搜索"], states={"configured": True, "exposed": True, "authenticated": False, "callable": False, "action_covered": True})
# The fresh test home is seeded from the packaged map, so the expected total
# follows the seed instead of a hardcoded number (a seed update must not fail here).
_seed = json.loads(Path(accf_core.__file__).with_name("capability_map.json").read_text(encoding="utf-8"))
check("register updates map", rg["total_tools"] == len(_seed.get("tools", [])) + 1,
      f"seed={len(_seed.get('tools', []))} got={rg['total_tools']}")
rr2 = c.route("帮我搜索最新的模型发布信息")
check("route new tool", rr2["routes"] and rr2["routes"][0]["name"] == "web-search")

print("== call ==")
# register with an absolute path: accf_call executes in the data home, so the
# packaged template's relative form only works when the data home IS the code dir
core_py = Path(accf_core.__file__).resolve()
c.register_tool("accf-store-health", "script", "Run mechanical validation over this ACCF store",
                ["validate store", "校验"], command=f'"{sys.executable}" -B "{core_py}"')
cr = c.call("accf-store-health")
check("call whitelisted script", cr.get("exit_code") == 0, str(cr)[:300])
cn = c.call("web-search")
check("call blocks non-script", "error" in cn)

print("== rebuild ==")
c.db.close()
c2 = accf_core.AccfCore(HOME)
rb = c2.rebuild_index()
check("rebuild counts", rb["rebuilt"] >= 5, str(rb))
v2 = c2.validate()
check("post-rebuild consistent", v2["issues"] == ["none — store is consistent"], str(v2["issues"]))
q4 = c2.recall("EverOS 混合检索 BM25", k=1)
check("post-rebuild recall", q4["hits"] and q4["hits"][0]["id"] == r3["id"])

print("== distillation gate & dreaming sandbox ==")
import json as _json
import tempfile as _tempfile
SB_HOME = _tempfile.mkdtemp(prefix="accf-sb-home-")
try:
    sb_core = accf_core.AccfCore(SB_HOME)
    for world, pair, success in (("world-A", [1, 1], 1), ("world-B", [2, 3], 1),
                                 ("world-C", [0, 0], 0)):
        sb_core.save(f"log {world}", _json.dumps({"world": world, "trace": [
            {"step": s, "bits": [1, 0, 0, 0], "pair": pair, "success": success}
            for s in range(4)]}), kind="episode", tags=["episode"], disconfirmation="test")
    p_raw = sb_core.protocol_add("raw: in context X do Y", ["sandbox"], [])
    p_dist = sb_core.protocol_add("distilled: always press cobalt then jade",
                                  ["sandbox"], [], distilled=True)
    ids = [h["id"] for h in sb_core.protocol_match(["sandbox"])["protocols"]]
    check("distilled protocol withheld by default", p_dist["id"] not in ids and p_raw["id"] in ids, str(ids))
    ids_all = [h["id"] for h in sb_core.protocol_match(["sandbox"], include_distilled=True)["protocols"]]
    check("distilled surfaced on opt-in", p_dist["id"] in ids_all, str(ids_all))
    sb = sb_core.sandbox([1, 0, 0, 0])
    check("sandbox dreams over raw episodes", sb["status"] == "dreamed" and sb["episodes_sampled"] == 12, str(sb)[:200])
    check("sandbox keeps untried neutral",
          all(cd["score"] == 0.5 for cd in sb["candidates"] if cd["observed"] == 0))
    check("sandbox flags cross-world split",
          len({cd["worlds_confirmed"] for cd in sb["cross_validated"]}) >= 1, str(sb["cross_validated"]))
    check("sandbox rejects invalid bits", sb_core.sandbox([1, 2, 3])["status"] == "invalid")
finally:
    shutil.rmtree(SB_HOME, ignore_errors=True)

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
