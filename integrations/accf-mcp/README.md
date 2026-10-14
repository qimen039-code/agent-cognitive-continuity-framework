# ACCF-MCP

ACCF（agent-cognitive-continuity-framework）的本地 MCP 服务化网关。把记忆、交接、协议、路由、判断五类能力挂载为 MCP 工具，任何 MCP 客户端（Trae / Claude Desktop / Cursor）配置一次即全局可用，覆盖所有对话。

## 能力总览

| 层 | 能力 | 实现 |
|---|---|---|
| 数据层 | 来源保真记忆 | `memory/ handoffs/ protocols/` 下的 Markdown 是唯一事实源，frontmatter 携带 provenance（world_id / step_count / success_count / disconfirmation） |
| 索引层 | 百万级混合召回 | 双道 RRF 融合：`token_postings` 倒排 TF-IDF + sqlite-vec 向量两阶段（bit 量化粗筛 k=100 → float 精确重排，bge-small-zh-v1.5 本地嵌入） |
| 认识层 | 可证伪记忆 | `disconfirmation` 字段定义"何种观察下此记忆被推翻"；handoff 携带 `unknowns`；验证过的教训沉淀为 hash-chain protocol，按 context_tags 确定性浮现 |
| 路由层 | 能力挂载 | `capability_map.json` 路由地图，保留 configured/exposed/authenticated/callable/action_covered 状态契约；`accf_call` 白名单执行本地脚本 |
| 判断层 | 决策头建议者 | Laya P6t 双语决策头经 `laya_judge` 接入，候选动作 logits 只作建议；`accf_episodes` 证据门保证喂入的经历与当前状态精确匹配 |

## 索引分级（随记忆量增长）

| 规模 | 行为 |
|---|---|
| 0-1 千条 | 全量索引即时建成，交互无感 |
| 1 千-10 万 | `accf_save_many` 批量导入（约 70 条/秒，流水线+分块提交） |
| 10 万-百万 | 向量道两阶段检索（100 万条实测 recall@10=0.97，亚秒级）；文本道倒排毫秒级 |

`accf_backfill` 幂等重建二级索引（嵌入 / 向量表 / 倒排），超时中断后重跑自动续上。

## 部署

### 1. 环境

```powershell
python -m venv C:\Users\<你>\.local\share\accf-mcp\venv
C:\Users\<你>\.local\share\accf-mcp\venv\Scripts\python.exe -m pip install "mcp<2" fastembed sqlite-vec
```

- `mcp<2`：2.x 把 FastMCP 改名为 MCPServer，本项目钉在 1.x
- `fastembed` + `sqlite-vec`：向量道。缺失时自动降级为纯文本道（FTS + 倒排），`recall` 返回的 `channels.vector_available` 显式标记
- 嵌入模型 `BAAI/bge-small-zh-v1.5`（512 维，约 50MB）首次调用自动下载到 `<部署目录>/models/`，之后完全离线

### 2. 数据目录

默认 `~/.accf-mcp`，env `ACCF_HOME` 可覆盖到任意位置。目录结构：

```
~/.accf-mcp/
  memory/      # fact / episode 条目（Markdown，事实源）
  handoffs/    # 任务交接
  protocols/   # hash-chain 协议
  index.db     # SQLite：entries + FTS5 + vec_index + vec_coarse + token_postings
  capability_map.json
```

### 3. 启动

stdio 方式运行：`python server.py`（env `ACCF_HOME` 可选）。

### 4. Trae / Claude Desktop 挂载

```json
{
  "mcpServers": {
    "accf": {
      "command": "C:\\Users\\<你>\\.local\\share\\accf-mcp\\venv\\Scripts\\python.exe",
      "args": ["-B", "C:\\Users\\<你>\\.local\\share\\accf-mcp\\server.py"]
    }
  }
}
```

## 工具清单（14 个）

### 记忆

| 工具 | 作用 |
|---|---|
| `accf_save` | 存单条记忆（fact/episode/protocol）。episode 若 content 为 `{"world":..., "trace":[...]}` 自动提取结构化 meta 供证据门使用 |
| `accf_save_many` | 批量存入：分批嵌入 + 分块提交，单条失败不影响整批，返回 `complete` 标记是否全部完成 |
| `accf_recall` | 混合召回（向量道 + 文本道 RRF 融合），返回标题 + 溯源指针，token 便宜 |
| `accf_dig` | 两段式下钻：按 id 读完整原文 |
| `accf_backfill` | 重建二级索引（幂等，可续跑） |

### 交接与协议

| 工具 | 作用 |
|---|---|
| `accf_handoff_write` | 写任务交接（自动取代同 task_id 旧交接），`unknowns` + `disconfirmation` 保持诚实 |
| `accf_handoff_read` | 读最新交接（按任务或最近 10 条） |
| `accf_protocol_add` | 把验证过的教训沉淀为 hash-chain 协议 |
| `accf_protocol_match` | 任务开始时按 context_tags 确定性浮现相关协议 |

### 路由与执行

| 工具 | 作用 |
|---|---|
| `accf_route` | 任务描述 → 已注册 skills/MCP/scripts 路由匹配（含 ACCF 状态契约） |
| `accf_register_tool` | 新增/更新路由地图条目（自动落盘） |
| `accf_call` | 白名单执行 kind=script 的本地脚本（command 模板注册时固定，只能追加 args） |
| `accf_validate` | 机械校验：hash 漂移 / 索引一致性 / 协议链完整性 / 孤儿文件 |

### 判断层（Laya 决策头接入）

| 工具 | 作用 |
|---|---|
| `accf_episodes` | 证据门经历检索：混合召回 → 内容校验（只保留含当前状态成功经历的日志）→ 同 world 取最新版本。status: `linked` / `empty` / `no_log`——`empty` 时禁止退化喂无关经历（会污染判断） |

## Laya 判断头调用协议

`laya_judge`（注册在 capability_map，kind=script）是 P6t 双语决策头的单一边界适配器，输出 `selected_index` + logits，只作建议不作门禁。

```
1. accf_episodes([b1,b2,b3,b4])        → status=linked 时取 episodes
2. laya_judge request {
     "op":"choose_fields", "lang":"en",
     "indicators":[b1,b2,b3,b4],
     "candidates":[[i//4, i%4] for i in range(16)],
     "episodes": <上一步的 episodes> }
3. 执行后把 (bits, pair, success) 结果 accf_save 回写 → 下次 episodes 更强
```

- `lang`: `en`（24/24 开发集）或 `zh`（8/24，弱于英文，仅建议）
- inline 模式：`laya_judge.py inline en 0101 00_12 [episodes 0101:12:1_1010:23:0]`（分隔符只用 `_` 和 `:`，避开 shell 元字符）
- CPU 冷启动约 8-16s，单批推理约 0.6-1s

实测（256 步真实 xor_mix 世界）：按状态检索成功经历喂入时 3/4 状态 top-1 命中；无差别滑动窗口喂法反而把准确率从 rank 1 拖到 rank 13——**记忆链接质量决定判断头强弱**。

## 挂载存量脚本

任何本地脚本登记一条 script 条目即可被 `accf_route` 命中、`accf_call` 执行：

```
accf_register_tool(
  name="my-script", kind="script",
  description="脚本作用描述（路由匹配用）",
  match=["关键词1", "关键词2"],
  command='"...python.exe" -B "C:\\path\\to\\script.py"')
```

command 是注册时固定的白名单模板，`accf_call` 只能追加 args。

## 命令行

```powershell
python -B accf_core.py validate              # 校验 store 一致性
python -B accf_core.py rebuild               # 全量重建索引
python -B accf_core.py recall --query "..."  # 快速召回
```

## 测试

```powershell
python -B test_accf.py        # 核心引擎（stdlib only，任意 python3.10+）
python -B test_accf_mcp.py    # MCP 协议端到端（需 mcp<2）
```

## 大规模导入

```python
core.save_many(items, timeout=300)
# items: [{"title","content","kind?","source?","tags?","disconfirmation?","task_id?"}]
# 返回 {"saved","requested","complete","elapsed_seconds","failed":[{"index","error"}]}
```

- 分批嵌入（每批 64）+ 每批提交：超时后已完成部分全部落盘，`complete=false` 时重跑续传
- 嵌入输入只取 title（实测比 title+body 快 22 倍且 top-1 更准；body 经 FTS 完整可搜）
- 单条 `save` 约 6-14ms（随库增大），交互式使用无感

## 性能基线（实测，RTX 4060 机型 CPU）

| 指标 | 数值 |
|---|---|
| 100 万条向量 recall@10 | 0.97（bit 粗筛 88ms + float 重排 407ms） |
| 文本道召回 | 毫秒级（倒排，只算共享 token 的文档） |
| 批量写入 | 约 70 条/秒（含嵌入+全索引） |
| 单条 save | 6-14ms（随库大小） |
| Laya judge | CPU 冷启动 8-16s，单批推理 0.6-1s |
