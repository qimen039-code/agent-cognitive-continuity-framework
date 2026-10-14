"""MCP protocol-level test: spawn server.py over stdio, list tools, exercise the
core tools (recall / save_many / episodes / backfill / validate)."""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
HOME = os.path.join(tempfile.gettempdir(), "accf-mcp-home")
os.makedirs(HOME, exist_ok=True)
sys.path.insert(0, os.path.dirname(SERVER))
import accf_core

# seed entries so recall/episodes have data
c = accf_core.AccfCore(HOME)
c.save("MCP 协议测试条目", "server.py stdio 握手后的 accf_recall 端到端验证。",
       source="test_accf_mcp.py", tags=["mcp"])
c.save_many([{"title": f"协议测试 episode {i}",
              "content": json.dumps({"world": "mcp-test", "trace": [
                  {"step": s, "bits": [1, 0, 0, 0], "pair": [1, 1], "success": 1}
                  for s in range(4)]}),
              "kind": "episode", "tags": ["mcp"], "disconfirmation": "test"} for i in range(2)])
c.db.close()

EXPECTED = 14


async def main():
    params = StdioServerParameters(
        command=sys.executable, args=["-B", SERVER], env={**os.environ, "ACCF_HOME": HOME})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("TOOLS:", names)
            assert len(names) == EXPECTED, f"expected {EXPECTED} tools, got {len(names)}"

            r = await s.call_tool("accf_recall", {"query": "stdio 端到端", "k": 5})
            hits = json.loads(r.content[0].text)["hits"]
            print("RECALL HIT:", hits[0]["title"] if hits else "NONE")
            assert hits and any("MCP 协议测试条目" in h["title"] for h in hits)

            r = await s.call_tool("accf_save_many", {"items": [
                {"title": "协议测试 批量写入", "content": "save_many over stdio",
                 "kind": "fact", "tags": ["mcp"], "disconfirmation": "test"}]})
            body = json.loads(r.content[0].text)
            print("SAVE_MANY:", body["saved"], body["complete"])
            assert body["saved"] == 1 and body["complete"]

            r = await s.call_tool("accf_episodes", {"indicators": [1, 0, 0, 0]})
            body = json.loads(r.content[0].text)
            print("EPISODES:", body["status"], len(body.get("episodes", [])))
            assert body["status"] == "linked" and body["episodes"]

            r = await s.call_tool("accf_backfill", {})
            body = json.loads(r.content[0].text)
            print("BACKFILL:", body["backfilled"], body["complete"])
            assert body["complete"]

            r2 = await s.call_tool("accf_validate", {})
            print("VALIDATE:", r2.content[0].text[:120])
            print("MCP PROTOCOL TEST OK")

asyncio.run(main())
