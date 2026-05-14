# STELA —— Stable prefix · Tiered bands · Ephemeral tail · Layered adapters · Anchored marks

> 三层 cache-友好 prompt 协议的 Python 参考实现。
>
> - **想直接用**：看 [`docs/User-guide.md`](docs/User-guide.md)（安装、接入路径、CLI、故障排查）
> - **想看改动历史**：看 [`CHANGELOG.md`](CHANGELOG.md)
> - **想理解协议**：看 [`docs/2026-05-06-stela-protocol.md`](docs/2026-05-06-stela-protocol.md)
> - **本 README**：核心概念速览（协议简介、不变量、5 原语、capability 矩阵）

**STELA**（"石碑"）取意：石碑底座的铭文（durable prefix）刻一次用一辈子；
上方按时间累加的题字（每轮 user/assistant 内容）随时可擦改，但不会动到底座。
KV cache 的全部价值就是把"底座"留住——这正是协议的核心。

STELA 是 Janus-Prompt v2（[`docs/2026-05-06-janus-prompt-architecture.md`](../docs/2026-05-06-janus-prompt-architecture.md)）的精简、独立、可移植版本。
保留唯一真正赢 KV cache 的东西——**带顺序不变量**——把其余复杂度全部砍掉。

---

## 1. 协议简介

```
agent harness ──► STELA Bridge ──► engine adapter ──► LLM 服务
   (parse)          (5 原语)         (capability-aware)
```

- **harness 插件**：把上游 agent (OpenClaw / Hermes/Claude Code) 的原始请求翻译成 `StelaIR`。
- **bridge**：纯 Python，5 个原语 + 1 条不变量，不依赖任何 LLM SDK。
- **engine adapter**：根据 capability 矩阵把 IR 翻译成 Anthropic / OpenAI / DeepSeek 的 wire 请求；并把 usage 归一化成 `UsageReport`。

---

## 2. 三层架构

| 层 | 文件 | 职责 |
|---|---|---|
| harness | `stela/harness/openclaw.py`、`hermes.py` | envelope 切分、大文档进 ref-pool、生成 `StelaIR` |
| bridge | `stela/bridge.py`、`stela/ir.py`、`stela/refpool.py` | 5 原语、不变量校验、ref-pool 冻结 slug、canonicalize |
| engine | `stela/engine/anthropic.py`、`openai.py`、`deepseek.py` | capability-aware Mark、wire 序列化、usage 解析 |

---

## 3. 五个原语（`Bridge` 方法）

| 原语 | 作用 | 协议节 |
|---|---|---|
| `place(segment, blocks)` | 把 block 放进 tools / system / 当前 message | §6.1 |
| `pin(slug, payload)` | 在 system 段写一个 PIN 块 | §6.2 |
| `mark()` | 让 engine 给出本轮的 BP / routing-key 计划 | §6.3 |
| `fold(slugs= or message_range=, summary=)` | 把旧轮折叠成 ref-pool 引用 | §6.4 |
| `refresh(plan)` | 满足节流后发 `max_tokens=0` prewarm（仅 Anthropic） | §6.5 |

---

## 4. 顺序不变量（§5）

每个 segment（tools / system / 单条 message）内：

```
PIN*  →  FOLD*  →  DROP*
```

违反就抛 `StelaInvariantError`。这是协议唯一的硬约束——其他都是软建议。

---

## 5. ref-pool（§4）

- slug 一旦 `register()` 就**冻结**：内容可以变（`fold()`），slug 不能变。
- 文本里的 `[ref:slug]` 引用，必须能在 ref-pool 里找到，否则 `lint_blocks` 报错。
- ref-pool 渲染时按 slug 字典序排，保证字节稳定。

---

## 6. Engine capability 对照表

| 能力 | Anthropic 4.6+ | OpenAI 4+ / 5.x | DeepSeek V3+ | **vLLM** | **SGLang** |
|---|:---:|:---:|:---:|:---:|:---:|
| 显式 BP / 锚位 | ✓（最多 4） | ✗ | ✗ | ✓（pin index） | ✓（radix lock） |
| TTL 控制 | 5m / 1h | 24h（部分模型） | 无 | pin / unpinned | pin + tier |
| 显式 prewarm | ✓（`max_tokens:0`） | ✗ | ✗ | ✓ | ✓（`prewarm_only`） |
| 路由 key | ✗ | `prompt_cache_key` | ✗ | `cache_salt` | `affinity_key` (CASS) |
| **缓存查询（read）** | ✗ | ✗ | ✗ | ✓ | ✓ |
| **段淘汰（write）** | ✗ | ✗ | ✗ | ✓ | ✓ |
| **fork-and-replace** | ✗ | ✗ | ✗ | 部分 | ✓ |
| **HiCache 层级提示** | ✗ | ✗ | ✗ | ✗ | ✓ |
| usage 字段 | `cache_creation_input_tokens` / `cache_read_input_tokens` | `prompt_tokens_details.cached_tokens` | `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` | `prompt_tokens` + `cached_tokens` | 同左 + `cache_hierarchy_breakdown` |

**双向能力**（`BidirectionalEngineAdapter` mixin）只在开源推理引擎上实现：

- `bridge.probe_cache()` —— 问 server "前缀还在缓存里吗？"，闭源 API 返回 `hit=False`。
- `bridge.cooperative_fold(message_range=..., summary=...)` —— 折叠 IR + 拿到服务端 cache 指令片段（`evict_span` / `fork_from_path`）。
- `bridge.emit_with_extras(ctrl)` —— 把上一步的指令合并进 wire 请求。

vLLM 走 `cache_policy.{pin_prefix_until_block, evict_span}` + `cache_salt`；SGLang 走 `cache_control.{lock_radix_path, fork_from_path, replace_suffix, prefer_tier, affinity_key}`。

> **核心收益**：闭源 API 的 `Fold` 是客户端 rewrite，每次 fold 都要 server 重新 prefill 整段；vLLM/SGLang 的 `cooperative_fold` 让 server 保留前缀 KV 不动、只重算摘要尾段——这是闭源 API 完全做不到的。

---

## 7. 用法示例

```python
from stela import Bridge, load_engine, load_harness

harness = load_harness("openclaw")          # or "hermes"
engine  = load_engine("anthropic")          # or "openai" / "deepseek"

ir = harness.parse(raw_request, session_id="task-001",
                   engine="anthropic", model="claude-opus-4-7",
                   expected_turns=20)

bridge = Bridge(ir, engine)
plan   = bridge.mark()        # 让 engine 决定 BP / routing-key
wire   = bridge.emit()        # 拿到可发的 wire 请求

response = call_llm(wire)     # 你自己的 HTTP 客户端
report   = bridge.absorb_usage(response)
print(report.cache_read, report.raw_input)
```

完整端到端跑通见 [`stela/demo.py`](demo.py)：

```bash
python -m stela.demo
python -m stela.tests.test_smoke
```

---

## 8. 与 CLAUDE.md 北极星指标对接

`UsageReport` 字段直接对齐 [`benchmark/scripts/compute-metrics.py`](../benchmark/scripts/compute-metrics.py) 的 schema：

```
input_total = raw_input + cache_read + cache_write
```

四个北极星指标都是绝对量，不是比例：

1. resolved rate
2. input tokens / task
3. cache_read total
4. tokens per resolved

STELA 不去优化"比例"——比例可以靠缩小分母作弊。它只优化**绝对的 cache_read 增量**和**绝对的 raw_input 减量**。

---

## 9. R1–R8 修复在代码中的位置

review 阶段发现协议设计有 8 个隐患（R1–R8）。Python 实现里全部修掉了：

| 编号 | 问题 | 修复位置 |
|---|---|---|
| R1 | OpenAI `prompt_cache_key` 单 key ≥15 RPM 才会扩槽位，STELA 反向写错 | `stela/engine/openai.py :: KEY_RPM_SOFT_CAP = 12` + `shard()` |
| R2 | Anthropic 4 BP 只覆盖 head + tail，中间 20+ 轮的稳定段落空 | `stela/engine/anthropic.py :: _MID_ANCHOR_STRIDE = 19` 的 BP-mid |
| R3 | 子 agent IR 与父 IR 的 session_id 混用 | `stela/harness/hermes.py` 注释明示子 IR 独立 parse |
| R4 | `fold()` 后 Mark slot 落在已折叠区，需重 plan | `stela/bridge.py :: fold()` docstring 说明，需调 `mark()` 重生成 |
| R5 | tool_def / tool_use / tool_result 的字段顺序 canonicalize 漏在 adapter；tools 数组顺序 / `required` 数组顺序未稳 | `stela/bridge.py :: _canonicalize_ir()` 在 emit 前统一做：dict key 排序 + tools 数组按 `(source, mcp_server, name)` 稳定排 + tool_def schema 子树里 `required` 集合排序（详见协议 §5.1） |
| R6 | thinking 块跨非 tool_result 调用会失效 | `stela/engine/base.py :: thinking_preserved_across_non_tool_result` 能力位 |
| R7 | Anthropic BP 候选数 > 4 时没有显式优先级 | `stela/engine/anthropic.py :: plan_marks` 的优先级字典 + 截断 |
| R8 | refresh 频率没节流，可能反向打满 quota | `stela/bridge.py :: REFRESH_THRESHOLD = 11` 的自适应门控 |

---

## 10. 不做什么

- ❌ 不做 token 计数（让 engine 自己回 usage）。
- ❌ 不做 retry / backoff（属于 HTTP 客户端的事）。
- ❌ 不做 KV-cache 物理实现（这是服务侧的事；STELA 只决定**喂什么、按什么顺序喂**）。
- ❌ 不做 streaming SSE 解析（`absorb_usage` 接受最终 response object）。
