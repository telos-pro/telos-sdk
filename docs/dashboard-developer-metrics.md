# Developer Dashboard 指标说明

入口：`GET /__stela/developer`（HTML）或 `/__stela/developer.json`（JSON）。
源码：[scripts/build_developer_page.py](../scripts/build_developer_page.py) 渲染，
[proxy/inspector.py](../proxy/inspector.py) 累计内存状态。

> 与面向用户的 Savings Dashboard 不同：这个页面**只看内存里的实时状态**，
> 每次 GET 都从 `SessionInspector` 重渲染；进程重启即丢。

---

## 1. Overview（session 列表）

源：`SessionInspector.items()` 按 `last_seen` 倒序。

| 列 | 含义 | 来源 |
|---|---|---|
| `session_id` | 会话标识；点击进入详情 | `SessionInspectorEntry.session_id` |
| `model` | 该 session 最近一次请求里 `ir.hints.model` | `entry.last_model` |
| `harness` | 该 session 最近一次请求识别出的 harness（`hermes` / `openclaw` / `passthrough`） | `entry.last_harness`（见 `_detect_harness`） |
| `calls` | 已观察到的 call 次数（封顶 `INSPECTOR_HISTORY=25`，超出后滑窗丢最旧） | `len(entry.calls)` |
| `tool calls` | 所有 call 中 assistant 发起的 `tool_use` 块总数 | `sum(s.invocations)` |
| `distinct tools` | 不重复工具名个数 | `len(entry.tools_stat)` |
| `tool_result chars` | 所有 `tool_result` content 字符数累计（response body 体量） | `entry.tool_result_chars_total` |
| `last seen` | 距最后一次 call 的相对时间 | `now - entry.last_seen` |

---

## 2. Session Detail · KPI strip

| KPI | 含义 |
|---|---|
| `model` | 该 session 最近一次模型名 |
| `harness` | 最近一次识别出的 harness |
| `calls seen` | 内存中保留的 call 数（≤ `INSPECTOR_HISTORY`） |
| `plan slots` | 最近一次 `EmitPlan` 实际落下的 cache-breakpoint slot 名列表（见下） |
| `last raw_input` | 最近一次响应 `usage.input_tokens`（未命中缓存的 prompt token） |
| `last cache_read` | 最近一次响应 `usage.cache_read_input_tokens`（命中缓存的 prompt token） |
| `last cache_write` | 最近一次响应 `usage.cache_creation_input_tokens`（本次新写入缓存的 prompt token） |
| `last output` | 最近一次响应 `usage.output_tokens` |

### Plan slot 名（BP-*）

来自 [engine/anthropic.py:41-96](../engine/anthropic.py#L41-L96) 的 `plan_marks`。Anthropic
只允许 4 个 `cache_control` breakpoint，按下表优先级砍：

| Slot | 位置 | TTL | 触发条件 |
|---|---|---|---|
| **BP-T** | `tools` 段最后一个 block | `1h` | 请求带 `tools` |
| **BP-S** | `system` 段最后一个 **PIN** block | `1h` | system 段存在 PIN block（不含 ref-pool） |
| **BP-R** | `system` 段最后一个 **FOLD** block | `1h` | system 段存在 FOLD block（典型即 ref-pool 末尾） |
| **BP-mid** | `messages[len-19]` 内最后一个非 DROP block | `5m` | `len(messages) ≥ 19`（修复 R2，确保下次仍在 20-block lookback 窗内） |
| **BP-X** | 最后一条 message 内最后一个非 DROP block | `5m` | 存在非空 message |

优先级 `BP-T > BP-S > BP-R > BP-mid > BP-X`。物理顺序由 `tools → system → messages`
保证；TTL 长（1h）必先于短（5m），由 segment 顺序天然成立。

---

## 3. "Prompt regions · pin·fold·drop chars per segment"

为 `tools` / `system` / `messages` 三段各画一条堆叠条：
- **P (PIN)**：`#d29922` 金黄，长期稳定的内容（system prompt、tool defs、用户提问主体）
- **F (FOLD)**：`#58a6ff` 蓝，ref-pool / 上轮 tool_result / assistant 历史回应
- **D (DROP)**：`#7d8590` 灰，每轮变化的 envelope（`<system-reminder>`、`<environment_info>`、时间戳）

每段标题展示该段总字符数 + 三个 band 各自的 (chars, blocks) 数字。
delta 来自 [scripts/build_developer_page.py:159-164](../scripts/build_developer_page.py#L159-L164)：
红色 `+N` 表示本轮比上一轮长；绿色 `−N` 表示缩短。

---

## 4. "Recent calls"（按 call 时间倒序）

每行一条 call 的快照，源：`entry.calls`（保留最近 25 条，[INSPECTOR_HISTORY](../proxy/inspector.py#L21)）。

| 列 | 含义 |
|---|---|
| `#` | call index（自 session 起单调递增） |
| `lat` | 调用延迟（秒） |
| `raw_in` / `cache_read` / `cache_write` / `output` | 该 call 响应的 `usage` 四元组（normalized） |
| `tools chars · Δ` | 本轮 `tools` 段总字符 + 与上一轮的差 |
| `system chars · Δ` | 同上，`system` 段 |
| `messages chars · Δ` | 同上，`messages` 段 |
| `plan slots` | 本轮实际下的 4 个 BP slot 名（见 §2） |
| `uses` | 本轮 assistant 响应里 `tool_use` 块数（assistant → tool） |
| `results` | 本轮 user message 里 `tool_result` 块数（tool → assistant） |

> `uses` 与 `results` 在时间上**错一轮**：第 N 轮 assistant 发起的 `tool_use` 一般要
> 等第 N+1 轮 user 才把 `tool_result` 送回。

---

## 5. "Latest IR · per-message blocks (band · kind · chars)"

最近一次请求的 **StelaIR.messages 快照**，按 message index 顺序铺开。每条 message
左边是 `msg[index]`，中间是 `role`（user / assistant），右边是 block pill 序列。

每个 pill 形如 `P·text 1,234c [openclaw/user-query]`：

| 字段 | 含义 |
|---|---|
| 颜色 / 首字母 | band：**P** 金黄=PIN · **F** 蓝=FOLD · **D** 灰=DROP |
| `kind` | block 类型：`text` / `tool_use` / `tool_result` / `thinking` / `image` / `tool_def` |
| `Nc` | block payload 字符数 |
| 灰色尾部 | `source_tag` 或 `ref_slug`，记录这个 block 是被哪个 harness 哪段逻辑切出来的 |

`source_tag` 的前缀就是 harness 名（`openclaw/...` / `hermes/...` / `harness/...`），
可用来核查 [§7](#7-source_tag-参考表) 的判定是否正确。

---

## 6. "Tool calls in this session"

源：`SessionInspectorEntry.tools_stat`（[proxy/inspector.py:26-53](../proxy/inspector.py#L26-L53)）。
assistant 的每个 `tool_use` → `absorb_use(args_chars)`；user 的每个 `tool_result` →
通过 `tool_use_id` 反查工具名 → `absorb_result(result_chars)`。

| 列 | 含义 |
|---|---|
| `tool name` | 工具名 |
| `invocations` | 调用次数（assistant → tool 的请求次数） |
| `args chars total` | 累计入参 JSON 字符数 |
| `args avg` | `args_chars_total / invocations` |
| `args last` | 最近一次入参字符数 |
| `result chars total` | 累计返回内容字符数（所有 `tool_result` content 累加） |
| `result avg` | `result_chars_total / invocations`（分母用 invocations，而非 result 次数；少数情况下结果未到） |
| `result max` | 单次返回的最大字符数（抓"输出爆炸"工具） |
| `result last` | 最近一次返回字符数 |

> **注意**：`tool_use` 与 `tool_result` 通过 `tool_use_id` 关联，反查窗口是 `entry.calls`
> （≤ 25 条）。如果一个工具的 result 比对应 use 晚 25 个 call 才到，会归不到正确名下、
> 只计入 `tool_result_chars_total`（在 Overview 列里能看到）。

---

## 7. "Last API usage · cache-related fields (raw)"

把 `entry.last_usage_raw` 里的以下原始字段直接 JSON-dump 给你看：
`input_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens` /
`output_tokens` / `cache_creation`。

最有信息量的是 `cache_creation.ephemeral_5m_input_tokens` 和
`ephemeral_1h_input_tokens`：用来核对 BP slot 的 TTL 分配是否生效。

---

## 7. source_tag 参考表

`source_tag` 前缀 = harness 名；后缀描述切片来源。

| Harness | 段 | 常见 tag |
|---|---|---|
| `openclaw` | tools | `openclaw/tools` |
| `openclaw` | system | `openclaw/system-large` / `openclaw/system-ref-stub` / `openclaw/system` |
| `openclaw` | messages | `openclaw/tool-result` / `openclaw/assistant-text` / `openclaw/assistant-tool-use` / `openclaw/other` |
| `hermes`   | tools | `hermes/tools` |
| `hermes`   | system | `hermes/system` / `hermes/file-block`（>2KB 的 `<file path=…>` ref-pool 块） |
| `hermes`   | messages | `hermes/tool-result` / `hermes/assistant-text` / `hermes/assistant-tool-use` / `hermes/thinking` / `hermes/other` |
| 共享 | user message 切分 | `harness/user-query` (PIN) · `harness/system_reminder` / `command_message` / `command_name` / `env_info` / `current_time` (DROP) · `harness/prev_result` (FOLD) |

如果你期望是 Claude Code（hermes）但看到 `openclaw/*` 前缀，多半是
`_detect_harness` 漏判（见 §"已知问题"）。

---

## 已知问题

- `_detect_harness` 仅看 `system` 段文本里有没有 `<system-reminder>` / `<command-message>`，
  但 Claude Code 实际把这些标签注入到 **user message** 里，所以大部分 hermes 流量
  会被识别成 openclaw。详见 `scripts/stela_anthropic_transport.py` 检测函数的待修复清单。
