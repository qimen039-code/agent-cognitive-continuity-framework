"""Smoke test for accf_core (stdlib only). Run: python -B test_accf.py"""
import os
import shutil
import sys
import tempfile

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
check("register updates map", rg["total_tools"] == 6)
rr2 = c.route("帮我搜索最新的模型发布信息")
check("route new tool", rr2["routes"] and rr2["routes"][0]["name"] == "web-search")

print("== call ==")
# register with an absolute path: accf_call executes in the data home, so the
# packaged template's relative form only works when the data home IS the code dir
core_py = os.path.abspath("accf_core.py")
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

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
