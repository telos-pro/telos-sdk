# Savings Dashboard 指标说明

入口：`GET /__telos/dashboard`（proxy 内嵌）或 CLI:

```sh
telos dashboard --usage-log ~/.telos/usage.jsonl --out savings.html
```

源码：[scripts/build_savings_dashboard.py](../scripts/build_savings_dashboard.py)。

> **看板视角**：面向"用户/老板"——回答"接入 TELOS 之后省了多少 token、省了多少钱"。
> 数据源是 jsonl 形式的 `usage_log`，每行一条 call 的 `normalized` + `raw_usage` 字段。

---

## 1. 计费模型：token 到美刀的换算

### 1.1 四种 token bucket（来自 Anthropic `usage` 字段，已归一化）

| Bucket | 出处 | 含义 |
|---|---|---|
| `raw_input` | `usage.input_tokens` | **未命中缓存且未写缓存**的 prompt token |
| `cache_read` | `usage.cache_read_input_tokens` | **命中缓存**的 prompt token（节省的关键） |
| `cache_write` | `usage.cache_creation_input_tokens` | 本次新写入缓存的 prompt token（用于下一次复用） |
| `output` | `usage.output_tokens` | 模型生成的 token |

> 关键约束：Anthropic 的 `input_tokens` 字段**只是 `raw_input`**，不含 `cache_read`
> 与 `cache_write`。所以
> **总 prompt tokens = raw_input + cache_read + cache_write**。
> 看板里所有 `hit%` 分母都是这个和（旧实现只用 raw_input 当分母会高估命中率）。

### 1.2 单价表（USD per 1M tokens，2026 年公开价）

源：[scripts/build_savings_dashboard.py:56-76](../scripts/build_savings_dashboard.py#L56-L76)。

| Model 前缀 | input | cache_read | cache_write 5m | cache_write 1h | output |
|---|---:|---:|---:|---:|---:|
| `claude-opus-4-7` / `4-6` | 5.00 | 0.50 | 6.25 | 10.00 | 25.00 |
| `claude-opus-4-5` / `4`   | 15.00 | 1.50 | 18.75 | 30.00 | 75.00 |
| `claude-sonnet-4-6` / `4-5` / `4` | 3.00 | 0.30 | 3.75 | 6.00 | 15.00 |
| `claude-haiku-4-5` / `4`  | 1.00 | 0.10 | 1.25 | 2.00 | 5.00 |
| `gpt-5` / `gpt-5.1`       | 5.00 | 1.25 | 0 | 0 | 15.00 |
| `deepseek-chat` / `v3`    | 0.27 | 0.07 | 0 | 0 | 1.10 |
| `_default`（未识别）     | 3.00 | 0.30 | 3.75 | 6.00 | 15.00 |

**Anthropic 的 cache 计费规则**（已写入价格表）：

```
cache_read 价     = 0.10 × input 价        命中缓存 → 90% off
cache_write 5m   = 1.25 × input 价        短 TTL 写入 → 多付 25%
cache_write 1h   = 2.00 × input 价        长 TTL 写入 → 多付 100%
```

`cache_write` 在 5m / 1h 之间的拆分来自 `raw_usage.cache_creation.ephemeral_{5m,1h}_input_tokens`。
若缺失则**全部按 5m 计**（保守低估 1h 部分）。

### 1.3 单条 call 的实付成本

[`_cost_usd`](../scripts/build_savings_dashboard.py#L116-L129)：

```
cost = raw_input  × input_price
     + cache_read × cache_read_price
     + cache_write_5m × cache_write_5m_price
     + cache_write_1h × cache_write_1h_price
     + output     × output_price
```

### 1.4 反事实成本（"如果不开 TELOS"）

[`_counterfactual_cost_usd`](../scripts/build_savings_dashboard.py#L132-L143)：
假设保持 prompt 内容不变但**移除所有 `cache_control`**——所有 prompt token 都按
基础 `input` 价计费，没有 cache_read 折扣，也没有 cache_write 溢价：

```
counterfactual = (raw_input + cache_read + cache_write) × input_price
               + output × output_price
```

### 1.5 节省金额

[`_saved_usd_for_call`](../scripts/build_savings_dashboard.py#L146-L164)：

```
saved = counterfactual − actual
      = cache_read     × (input_price − cache_read_price)        ← 命中缓存赚回来的
      + cache_write_5m × (input_price − cache_write_5m_price)    ← 5m 写入溢价，负数
      + cache_write_1h × (input_price − cache_write_1h_price)    ← 1h 写入溢价，更负
```

**重要**：对 Anthropic 来说 `cache_write` 项是**负贡献**（写入比基础价贵 25–100%）。
只有当 `cache_read` 量足够大（即缓存被复用了多次），总和才正。这是相对早期
实现的关键修正——旧版本只算 cache_read 折扣，会高估省下的钱。

---

## 2. 顶部 Hero（"with TELOS 实际"视角）

| Hero | 计算 |
|---|---|
| **tokens saved (cache hits)** | `total.cache_read`（命中缓存的 prompt token 总数） |
| **cost saved (estimated)** | `total.saved_usd`（上一节 §1.5 累加） |

副标题：
- `cache hits 占总 prompt tokens 的 X%` —— `cache_read / (raw_input + cache_read + cache_write)`
- `若关 TELOS 预计要付 $A · 实付 $B · 节省 X%` —— 反事实 vs 实付 vs 占比

切到"without TELOS · 反事实"视角时，hero 变成：
- **prompt tokens (no cache)** = `raw_input + cache_read + cache_write`（全部按 input 价）
- **cost (no TELOS)** = §1.4 的反事实成本

---

## 3. KPI 条

| KPI | 含义 |
|---|---|
| `total calls` | 累计 call 数 |
| `unique sessions` | 出现过的不同 `session_id` 数 |
| `raw input` | 累计 `raw_input` token |
| `cache read` | 累计 `cache_read` token（看板的"主角"） |
| `cache write` | 累计 `cache_write` token，副标题展示 5m / 1h 拆分 |
| `output` | 累计 output token |

---

## 4. Token mix 堆叠条

按四个 bucket 染色：
- 🟠 `raw_input` — 实际付 input 全价的
- 🟢 `cache_read` — 命中缓存（0.1× 折扣）
- 🟡 `cache_write` — 写缓存（1.25× / 2× 溢价）
- 🔵 `output` — 生成 token

"with TELOS"视角下显示真实四色分布。
"without TELOS · 反事实"视角下，前三个塌成一个橙色块（所有 prompt token 都按 input 价计）。

---

## 5. Activity over time

按**每小时**桶 (`%Y-%m-%d %H:00`) 聚合。SVG 双轴：
- 绿色柱 = 该小时的 `cache_read` token 量
- 紫色折线 = 该小时的 `saved_usd`

x 轴只标 3 个时间点（首、中、末），y 轴左侧标 cache_read 量级。

---

## 6. Breakdown by harness / model / session

三张表结构相同，按 `cache_read` 倒序排前 N 行（harness/model 取前 12 行，session 前 15 行）。

| 列 | 含义 |
|---|---|
| key | `harness` / `model` / `session_id` |
| `calls` | 该 key 下的 call 数 |
| `raw_input` | 该 key 累计 `raw_input` |
| `cache_read` | 该 key 累计 `cache_read`（绿色，看板的核心列） |
| `cache_write` | 该 key 累计 `cache_write` |
| `hit%` | `cache_read / (raw_input + cache_read + cache_write)`（注意分母含 cache_write，与早期实现不同） |
| `saved $` | 横条 + 数字。横条以该表里"最大 saved 金额"为 100% |

> session 表只保留前 15 行——长会话累计 cache_read 高，新会话排在后面；
> 想看全量请用 `/__telos/developer.json`。

---

## 7. 视角切换

页面顶部三按钮：

| 模式 | 看什么 |
|---|---|
| **实际（开 TELOS）** | 真实四色 token mix、命中率、实际 saved $ |
| **反事实（不开 TELOS）** | 把所有 prompt token 当 raw_input、按 input 价估算总成本 |
| **并排对比** | 左侧反事实大数字、右侧实际大数字、底下一行"净节省 $X · X% off" |

选择会通过 `localStorage.telos.dashboard.mode` 持久化。

---

## 8. 数据来源 schema

`usage_log` 每行一个 JSON record（[docs/User-guide.md §7.1](User-guide.md)）。看板用到的字段：

| 字段 | 必需 | 说明 |
|---|---|---|
| `normalized.raw_input` | ✅ | int |
| `normalized.cache_read` | ✅ | int |
| `normalized.cache_write` | ✅ | int |
| `normalized.output` | ✅ | int |
| `raw_usage.cache_creation.ephemeral_5m_input_tokens` | 否 | 缺则按 5m 价兜底 |
| `raw_usage.cache_creation.ephemeral_1h_input_tokens` | 否 | 缺则不算 1h |
| `model` | 推荐 | 用于查价格表；未识别走 `_default`（Sonnet 价位） |
| `harness` | 推荐 | 用于 by_harness 分组 |
| `session_id` | 推荐 | 用于 unique-sessions 和 by_session 分组 |
| `ts` | 推荐 | 浮点 unix 时间戳，决定 timeline 桶 |

---

## 9. 常见解读问题

**Q：cache_read 越多越省钱吗？**
是。命中缓存的 token 只付 10% 价，每多 1M tokens cache_read 在 Opus 4.7 上省 $4.50。

**Q：cache_write 越多越好吗？**
**不一定**。每写 1M token 到 5m 缓存要付 $6.25（比 input $5 多 25%），写到 1h 缓存要付 $10
（多 100%）。**只有当这块缓存后续被多次命中**，写入溢价才能被命中折扣摊平。
看板的 `saved $` 已经把这个溢价算成负贡献了。

**Q：为什么 hit% 分母里要算 cache_write？**
因为 Anthropic 的 `input_tokens` 字段只指未命中且未写缓存的部分，
真正的"prompt 总量" = `raw_input + cache_read + cache_write`。用更小的分母会高估命中率。

**Q：反事实价是不是高估了？**
反事实 = 关掉所有 `cache_control` 的情况，**完全不包含**任何缓存折扣或溢价。这是
最干净的对照基线。TELOS 的价值就是缩小 (counterfactual − actual) 这个差。

**Q：未识别 model 怎么算？**
落到 `_default`（按 Sonnet 价位估）。`saved $` 在这种情况下只是"近似估算"，
不应该被当作精确账单。
