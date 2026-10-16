# ACCF-MCP

ACCF（agent-cognitive-continuity-framework）的本地 MCP 服务化网关。把记忆、交接、协议、路由、判断五类能力挂载为 MCP 工具，任何 MCP 客户端（Trae / Claude Desktop / Cursor）配置一次即全局可用，覆盖所有对话。

## 改行为前必读设计文档

工具面是从设计契约推导的，不是随手长出来的。改 `server.py` / `accf_core.py` 的行为之前，
先读仓库 `docs/` 下这几份（**跳过这一步最容易加进"看起来该有"的能力，破坏来源保真或
重复宿主已有能力**）：

| 契约 | 为什么必须先读 |
|---|---|
| `docs/architecture.md` | 各层职责与允许的依赖方向 |
| `docs/non-goals.md` | 明确排除项：不做完整记忆后端、不做跨 agent 自动记忆共享。这条决定了"宿主已有记忆时 ACCF 不再写一份" |
| `docs/context-recall.md` | 可选上下文召回的契约（宿主原生记忆适配层的实现依据） |
| `docs/conversation-memory-lane.md` | 会话记忆通道的量与边界 |
| `docs/hybrid-memory-retrieval-contract.md` | 混合召回的分数语义（RRF 是排序量，余弦才是相似度） |
| `docs/router-decision-contract.md` | accf_route / accf_call 的状态契约与白名单边界 |

部署侧若按 `docs/context-recall.md` 接入宿主原生记忆（如 WorkBuddy），那是**部署层**的
桥接：只读宿主记忆文件与会话记录，不写宿主记忆、不调模型，也不把两套分数混在一起比。

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

## 工具清单（19 个）

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
| `accf_protocol_add` | 把验证过的教训沉淀为 hash-chain 协议。`distilled=True` 标记一行式概括（从原味经历蒸馏的格言），此类条目默认不被 `accf_protocol_match` 浮现 |
| `accf_protocol_match` | 任务开始时按 context_tags 确定性浮现相关协议；`include_distilled=True` 才包含蒸馏条目 |

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
| `accf_sandbox` | 梦境沙盒：对记忆中的原始经历做确定性重放，对候选动作打分（收缩先验防单次成功锁死），并做跨 world 校验。重放分数只做剪枝，真实结果才是最终裁决者 |

### 宿主记忆桥（native）

| 工具 | 作用 |
|---|---|
| `accf_native_recall` | 只读宿主原生记忆/会话记录，返回 handle（不混分、不写宿主） |
| `accf_native_read` | 按 handle 分页读宿主记忆内容 |
| `accf_native_status` | 宿主桥可用性探测（未部署宿主适配时返回 unavailable，属预期降级） |

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

## 梦境沙盒（Dream-RSI 复刻）

动机来自 Dream-RSI（arXiv:2609.14858，Google/DeepMind 等，2026-09）：把已实现的搜索空间做**确定性重放**，比在经历上再做"语义指导/格言蒸馏"更强——论文原话 "semantic guidance is *worse* than replay ... consistently underperforms its unguided counterpart under equal budgets"。历史本身就是精确模拟器，不需要学习型世界模型。

`accf_sandbox([b1,b2,b3,b4], k=8)` 的三条复刻纪律：

1. **只读确定性重放**——只读已记录的 (bits, pair, success) 三元组，不做想象 rollout
2. **候选集永远完整**——16 个候选动作全部打分，未试过的保持中性 0.5，不会因为"没试过"被压制
3. **收缩先验**——`score = (sum(hits) + 1.5) / (len(hits) + 3)`，单次成功不会锁死策略

配套的蒸馏闸：`protocol_add(distilled=True)` 标记从原味经历概括出的一行式格言，`protocol_match` 默认**不浮现**蒸馏条目，opt-in 才 surfaced。原味经历走 `accf_episodes` / `accf_sandbox`，格言只是备查。

调用闭环：

```
accf_episodes([b1,b2,b3,b4])     → status=linked 时取 episodes
accf_sandbox([b1,b2,b3,b4])      → dreamed：16 候选重放分 + 跨 world 校验
  （两者独立：sandbox 自己重放 memory 里的原始 trace，不依赖 episodes 的喂法）
laya_judge ...                   → 决策头建议
执行后 accf_save 回写            → 下次 sandbox / episodes 更强
```

沙盒输出示例（live store，状态 0000，1 个 world）：

```json
{"status": "dreamed", "bits": [0,0,0,0], "worlds_sampled": 1, "episodes_sampled": 4,
 "candidates": [{"candidate": [2,2], "score": 0.7857, "observed": 4}, ...],
 "report": "4 raw episodes from 1 world(s) replayed; replay scores prune only — real outcomes remain the arbiter"}
```

跨 world 采样 ≥2 时，前 4 候选附 `worlds_confirmed` / `worlds_sampled` / `transferable`：只在单一 world 成立的候选被标为局部偶然，不作为可迁移结论。

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
