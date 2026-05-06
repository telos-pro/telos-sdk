# Janus-Prompt：KV-Cache 友好的双向感知 Agent Prompt 架构

**Status**: Design draft (v2, second-round review applied)
**Date**: 2026-05-06
**Audience**: agent-janus 实现者、上游 agent (OpenClaw / Claude Code) 集成方
**Related**:
- [docs/ARCHITECTURE_AND_GUIDE.md](ARCHITECTURE_AND_GUIDE.md)
- [agent-janus/PLAN_AND_PROGRESS.md](../agent-janus/PLAN_AND_PROGRESS.md) §5（Strategy A–F）
- [docs/2026-05-04-monotonic-prompt-stream.md](2026-05-04-monotonic-prompt-stream.md)

---

## 0. Abstract

Janus-Prompt 是面向 agent 任务的 prompt 物理布局规范，把 KV-cache 友好性作为**布局不变量**而非事后纠错对象。核心做法：

1. **三色标记**（`need_cache` / `need_cache_foldable` / `no_cache`）显式声明每个 content block 的缓存生命周期。
2. **4 段物理布局**保证标记按时序、按 TTL 分层；动态字段（时间戳等）永远落在所有 breakpoint 之外。
3. **双 BP 锚定 + 滚动中段锚**在 4 个 cache_control slot 限制下兼顾"系统级永久缓存"与"长会话 lookback 命中"。
4. **Compact 双模**：默认走公共 Anthropic API（仅靠物理布局让旧段自然过期）；推理后端支持时走 `/v1/cache/fold` 协同协议，做细粒度 KV 块淘汰。
5. **引用槽**：所有大上下文（文档、tool_result）集中在 G1 引用区，prompt 其它位置用 `[ref:slug]` 锚名引用——折叠 G1 不改变任何引用点的字节。

---

## 1. 设计目标（需求复述）

| # | 特性 | 含义 | 验收准则（v2 修订）|
|---|---|---|---|
| **P1** | 天然 KV-cache 友好 | prompt 物理布局保证 prefix-stable，无需事后纠错 | **绝对值**：`tokens / resolved_task` 与 baseline（关闭 Janus-Prompt 的同 prompt）相比下降 ≥ X%，且 `resolved_rate` 不下降。**禁止单独使用 cache_read 比率**——可被分母缩放滥用，详见 §1.1 |
| **P2** | 稳态增量缓存 | 在两次 compact 之间的稳态轮，每轮新增 cache_creation 受 turn block 自身限制 | **稳态期**：`cache_creation_input_tokens(turn_N) ≤ tokens(new_turn_block_N)`。**Compact 事件**：单独核算，amortized over 两次 compact 间隔（典型 60 turn）。详见 §7.1 修订 |
| **P3** | 引用天然折叠 | 所有文档/工具结果通过引用槽间接寻址；compact 时仅折叠引用槽内容，引用点字节不变 | compact 前后所有 `need_cache` block 的 hash 不变。**且**：引用锚必须由协议层强制（结构化 ref block 或 emit-time lint），不能依赖文本约定，详见 §8.5 |
| **P4** | client/server 双向感知 | 同一套标记体系：client 知道哪些可丢、server 也能据此做细粒度 KV 淘汰 | span_map 双向序列化；`/v1/cache/fold` 端点与 client compact 路径行为等价 |

### 1.1 关于命中率指标的陷阱（C5 修订）

v1 版 P1 用 `cache_read / total_prefix_tokens ≥ 0.9` 作为指标。第二轮 review 指出该指标可被三种方式合法滥用：

1. **重复 BP**：把同一段挂多个 cache_control，命中率分子虚高、绝对成本不变。
2. **缩短 session**：少跑真实请求、多跑续期，比率上去但 `tokens/resolved_task` 反而恶化。
3. **拒绝难任务**：resolved 率塌、缓存命中率涨。

这与 [CLAUDE.md](../CLAUDE.md) 已确立的 "north-star metrics" 直接冲突——后者明确把绝对值（`tokens/resolved`、`cache_read_total`）作为不可被分母缩放游戏的指标。**v2 版本统一以 CLAUDE.md 的四元组为准**：

| # | 指标 | 方向 |
|---|---|---|
| 1 | `resolved_rate`（strict + lenient）| ≥ baseline |
| 2 | `input_tokens_per_task` | ≤ baseline（绝对值）|
| 3 | `cache_read_total` | ≥ baseline（绝对值）|
| 4 | `tokens_per_resolved_task` | ≤ baseline（north-star）|

比率类指标（cache_hit_rate）仅作为**诊断信号**报告，不作为验收门槛。

---

## 2. Review 修订记录

### 2.1 v0 → v1（首轮 review）

初版设计存在 5 处正确性问题：

| # | v0 问题 | v1 修复 |
|---|---|---|
| **R1** | "G0 + G1 共用一个 BP1" → 一旦 compact 折叠 G1，BP1 的 prefix hash 改变 → 整个永久基础失效 | **拆为 BP0（G0 末尾，1h）+ BP1（G1 末尾，1h）**。compact 只让 BP1 失效，BP0 永生 |
| **R2** | "G1 用 1h 写、5m 续"逻辑混乱（同一段同时声明两个 TTL 不合法）| G1 写入即为 1h；compact 时**停止再发送 G1 内容**，让旧 KV 自然 GC，不依赖 TTL 切换 |
| **R3** | "Compact 占位符必须等长" | 占位符无需等长。约束实际是：占位符之后的 `need_cache` 块的**字节**必须不变（引用槽设计已经满足这一点） |
| **R4** | 没考虑 Anthropic 的 TTL 排序约束："1h cache 必须出现在 5m cache 之前"（同一请求内）| 显式约束：BP0 (1h) → BP1 (1h) → BP2_mid (5m) → BP3_latest (5m)，按物理顺序排列 |
| **R5** | `max_tokens: 0` 预热未考虑限制（stream / extended thinking / structured outputs / tool_choice="tool"\|"any" 时被拒）| 续期 loop 使用独立的"非流式、关闭 thinking、tool_choice=auto"配置发起预热 |

### 2.2 v1 → v2（第二轮 review，本次修订）

第二轮 review 发现 v1 自身仍有 6 处问题，本文档为 v2 修订版：

| # | v1 问题 | v2 修复 |
|---|---|---|
| **R6 (C1)** | §7.1 "占位符无需等长" 论证只覆盖 BP0；BP2/BP3 的 prefix 包含 placeholder 字节，必然失效。与 P2 "每轮 cache_creation ≤ tokens(new_turn_block)" 的承诺直接冲突 | (a) 重写 §7.1 论证：诚实承认 fallback 模式 compact 后 BP2/BP3 必然失效，新一轮请求需要按 cache_creation 重写整个 "placeholder + 之后所有 turn block"；(b) 把 P2 拆分为 "稳态期" 与 "compact 事件" 两个核算口径；(c) 协同模式 §7.2 给出真正满足原 P2 承诺的路径 |
| **R7 (C2)** | §9 表格说 G1 初始 TTL=1h，§7.1 又说 "折叠后的 G1 末尾 TTL 降为 5m"，读者不清楚是同一 BP slot 改 TTL（违反 I6）还是 slot 销毁后重分配 | §7.1 / §9 / I6 三处统一表述：**compact 时 BP1 被销毁**（停止发送 G1 内容，对应 cache 条目自然 GC）；新建一个独立的 BP1' 挂在折叠 G1 末尾，TTL=5m。这不是 TTL 降级，是 slot 回收 + 重分配 |
| **R8 (C3)** | `[ref:slug]` 是裸字符串，靠 I3 "约定" 维持。模型重述、harness rewrite、大小写差异都会让锚名漂移 → user_input 字节变 → 整列 BP 失效 | 新增 §8.5：protocol-level enforcement —— (a) ref-pool 注册时 freeze 锚名，emit 阶段对 user 文本做 regex 校验，未注册 slug 直接 fail-fast；(b) 长期方案：把 ref 提升为结构化 content block（类似 tool_use id），不依赖文本子串 |
| **R9 (C4)** | §6.3 "几乎零成本续期" 是 hand-wave。30k prefix × 50min 续期一次 × cache_read 0.1× = 单 session/天 ~87k token-eq；多 session 并发是真钱 | 新增 §6.3.1：(a) 续期成本明算（cache_read 1× × 0.1 计费）；(b) 盈亏点公式 $N_{requests}/N_{refresh} \geq 1/9 \approx 11\%$；(c) **自适应续期**：跟踪过去窗口内真实请求数，低于阈值就跳过续期，让 cache 自然过期 |
| **R10 (C5)** | P1 指标 `cache_read / total_prefix_tokens ≥ 0.9` 可被分母缩放游戏（重复 BP / 缩短 session / 拒绝难任务）| §1.1 + §1 表格：**直接复用 CLAUDE.md 的四元 north-star metrics**（绝对值 + baseline 对照），比率类指标降为诊断信号 |
| **R11 (C6)** | v1 §3.2 "user_input 是 need_cache" 只对纯用户提问成立。CC / OpenClaw 的 harness 在每个 user turn 注入 git status / cwd / file tree / system-reminder，每轮都变 → 整个 Turn-N 用户块 hash 漂移 → I2 增量不变量直接破 | (a) §3.2 强制规则：user message **必须**是多 block 复合体，纯提问标 need_cache，harness envelope 切到 no_cache 子块；(b) §4.2 Turn Block 图示更新；(c) 新增 I10：user message 内同样适用 "need_cache* → foldable* → no_cache*" 的内部顺序约束；(d) 落地路径：扩展现有 Strategy A-4 的 stripDynamicFields 到 user 段；(e) 标记 Q2 已被 R11 的 splitter 规则解决 |

---

## 3. 三色标记体系（Tri-Color Marking）

每个 content block 在 client IR 上携带**单一标记**：

| 标记 | 语义 | 默认 TTL | 物理位置约束 | 典型内容 |
|---|---|---|---|---|
| 🟢 `need_cache` | 稳定、长寿、绝不折叠 | **1h** | 段组开头 | tools 定义、system prompt、**user 的提问** |
| 🟡 `need_cache_foldable` | 可缓存但 compact 时可丢弃 | **5m**（活跃轮）/ **1h**（初始引用区）| `need_cache` 之后 | 文档引用槽内容、assistant 回答、tool_result |
| 🔴 `no_cache` | 永不进缓存计算 | — | 段组**末尾**（所有 BP 之后）| timestamp、pid、cwd、`Current time:` |

### 3.1 顺序不变量

```
∀ 段组 G:
  layout(G) = [need_cache*] [need_cache_foldable*] [no_cache*]
              ───────┬──── ─────────┬────────── ─────┬────
                     └─ 永久前缀     └─ 可折叠区     └─ 永远不进 hash
```

违反此顺序就破坏整套架构：
- `no_cache` 出现在中间 → 后面所有段重新计算 hash
- `need_cache_foldable` 跑到 `need_cache` 前面 → 折叠操作会把稳定段连带扯掉

### 3.2 user message 的强制切分规则（v2 修订，R11 / C6）

**v1 表述错误**："user_input 是 need_cache" 只对**用户的纯提问字符串**成立。实际 agent harness（Claude Code、OpenClaw）在每个 user turn 注入大量动态 envelope：`<system-reminder>` / `<environment_info>`、cwd、`git status`、改动文件列表、时间戳、上一次工具调用摘要、当前打开的文件 / 编辑器选择。

如果整个 user message 标 `need_cache`，I2（增量不变量）直接破——每个 Turn-N 用户块 hash 漂移、BP3 滚动锚每轮失效。

**v2 强制规则**：user message **必须**是多 block 复合体，按三色切分：

```jsonc
{
  "role": "user",
  "content": [
    /* 🟢 纯用户提问（不可重生事实，need_cache）*/
    {"type": "text", "janusMark": "need_cache",
     "text": "<纯用户问题>"},

    /* 🟡 上一轮 tool_result 摘要、引用片段（可重生，foldable）*/
    {"type": "text", "janusMark": "need_cache_foldable",
     "text": "<前轮工具结果摘要 / [ref:...] 引用>"},

    /* 🔴 harness envelope（每轮都变，no_cache）*/
    {"type": "text", "janusMark": "no_cache",
     "text": "<system-reminder>cwd=/repo, branch=main, dirty=3 files</system-reminder>\nCurrent time: ..."}
  ]
}
```

**判定原则**：
- 用户**当下输入**的、模型推理依赖的事实 → `need_cache`
- 工具调用 / assistant 输出的回声 → `need_cache_foldable`
- 由 harness 在 emit 时刻派生、每轮都变化的元数据 → `no_cache`

**落地**：扩展 [agent-janus/bridge/src/efficiency/prefix-normalization/system.ts](../agent-janus/bridge/src/efficiency/prefix-normalization/system.ts) 中现有的 `stripDynamicFields` 策略到 user 段，识别已知 harness 注入模式（CC 的 `<system-reminder>...</system-reminder>`，OC 的 `<environment_info>...</environment_info>`）并自动切到 no_cache 子块。

**user message 内部仍适用顺序不变量**（见 I10）：`need_cache*` → `foldable*` → `no_cache*`，否则末尾的 no_cache 会污染前面 block 的 prefix hash。

---

## 4. 物理布局

### 4.1 初始 prompt（任务发起时）

```
┌─────────────────────────────────────────────┐
│ [G0] 固定前缀                                │
│   ├─ 🟢 tools[]              (need_cache, 1h)│
│   └─ 🟢 system prompt        (need_cache, 1h)│
├═════════════════════════════════════════════┤  ◀━━ ★ BP0：G0 末尾（1h，永生）
│ [G1] 引用区 (Reference Pool)                 │
│   ├─ 🟡 ref:doc-A "<内容>"   (foldable, 1h) │   所有文档/大上下文集中在这里
│   ├─ 🟡 ref:doc-B "<内容>"   (foldable, 1h) │
│   └─ 🟡 ref:tool-defs-extra  (foldable, 1h) │
├═════════════════════════════════════════════┤  ◀━━ ★ BP1：G1 末尾（1h，compact 时失效）
│ [G2] 动态尾部                                │
│   └─ 🔴 "Current time: ..."  (no_cache)     │
├─────────────────────────────────────────────┤
│ [Turn-1 user input]                         │
│   └─ 🟢 "请基于 [ref:doc-A] 重构"            │   引用"指针"在这里，内容在 G1
│       (need_cache, 1h)                      │
└─────────────────────────────────────────────┘
```

**关键修订（R1）**：与 v0 不同，v1 拆出 BP0 和 BP1 两个独立锚。

| 锚 | 位置 | TTL | 命中条件 | 失效条件 |
|---|---|---|---|---|
| **BP0** | G0 末尾 | 1h | tools + system 字节不变 | 修改 tools/system 才失效（极少）|
| **BP1** | G1 末尾 | 1h | G0 + G1 字节都不变 | compact 折叠 G1 时失效（频繁）|

→ 即使 BP1 失效，BP0 仍能 read 命中 G0 全部前缀，损失最小。

### 4.2 每轮对话的归档单元（Turn Block，v2 修订）

```
┌─ Turn-N Block ───────────────────────────────────────────┐
│ user message (复合体，§3.2 强制切分):                      │
│   🟢 user_query (pure)         (need_cache, 1h)          │   用户当下提问
│   🟡 prev_tool_summary / refs  (foldable, 5m)            │   上一轮回声、引用
│   🔴 harness_envelope          (no_cache)                │   cwd/git/timestamp
│                                                           │
│ assistant message:                                        │
│   🟡 assistant_response        (foldable, 5m)            │   模型输出可重生成
│                                                           │
│ user message (tool_result):                               │
│   🟡 tool_result × M           (foldable, 5m)            │   工具结果可重抓取
│   🔴 turn_metadata             (no_cache)                │   timestamp / latency
└───────────────────────────────────────────────────────────┘
```

关键点：
- **user message 不是单 block**，而是按 §3.2 强制切分的复合体。
- 每个 message 内部仍然遵循 "need_cache\* → foldable\* → no_cache\*" 顺序（I10）。
- BP 只能挂在 message 内**最后一个非 no_cache 块**上——挂在 no_cache 块上等同于不挂（每轮都 miss）。
- Turn 末尾的 no_cache 块在该 turn 的 BP 之后、下一个 turn 的 user_query 之前。详见 §5 wire format。

### 4.3 滚动 BP（19 轮一锚，4 个 slot 用满）

Anthropic lookback 窗口 = **20 个 block**。一个 Turn Block 平均 3-5 个 block，**每 19 轮**插入一个滚动锚使 lookback 总能找到上一个 BP。

但 cache_control slot 只有 **4 个**，必须复用：

| Slot | 物理位置 | TTL | 角色 | 替换策略 |
|---|---|---|---|---|
| **BP0** | G0 末尾 | 1h | 系统级永生（90% 命中靠它）| 永不替换 |
| **BP1** | G1 末尾 | 1h | 引用区基础（compact 时失效）| 永不替换（失效后不再续期）|
| **BP2** | 当前 - 19 轮处的"中段锚" | 5m | lookback 安全网 | 每 19 轮滚动一次，向后移 |
| **BP3** | 最新 turn 末尾 | 5m | 增量命中 | 每轮滚动 |

**Anthropic TTL 排序约束**：同一请求内 1h cache 必须出现在 5m cache 之前。
→ 物理顺序 BP0 (1h) → BP1 (1h) → BP2 (5m) → BP3 (5m) **天然满足**约束。

**Compact 后的 BP1 状态（v2 R7 / C2 修订）**：v1 表述容易让人以为 "BP1 的 TTL 从 1h 降为 5m"——这违反 I6（TTL 单调）。正确表述是：compact 时 **BP1 slot 被销毁**（client 停止发送 G1 内容，原 KV 条目自然 GC），随后**新建一个独立的 BP1'** 挂在折叠后的 G1 末尾，TTL=5m。这是 slot 回收 + 重分配，不是同一 slot 改 TTL。详见 §7.1 / §9 / I6。

**为什么不用更多中段锚？** Slot 只有 4 个，BP0/BP1 两个永久锚占去一半，剩下 2 个给"过去 19 轮 + 当前 19 轮"的滚动窗口刚好够用——超出当前 19 轮 + 中段 19 轮的更老历史依赖 BP1 的全段 read 命中，命中粒度粗但仍是命中。

---

## 5. Wire Format：双协议输出

### 5.1 公共 Anthropic API 模式（无服务端协议扩展，默认）

将三色标记翻译成标准 Anthropic 字段。**`no_cache` 的关键技巧**：放在每段最后，让 BP 落在它之前。

```jsonc
{
  "model": "claude-sonnet-4-5",
  "tools": [
    /* 🟢 G0: need_cache */
    {"name": "Read",  "input_schema": {/* canonicalized */}},
    {"name": "Bash",  "input_schema": {/* canonicalized */}}
    // BP0 不挂 tools 末尾——挂 system 段尾，因为 tools 之后还有 system 段全部属于 G0
  ],
  "system": [
    /* 🟢 G0: need_cache */
    {"type": "text", "text": "You are a senior engineer agent..."},
    {"type": "text", "text": "Rules: never delete without confirmation...",
     "cache_control": {"type": "ephemeral", "ttl": "1h"}},   // ★ BP0：G0 末尾

    /* 🟡 G1: need_cache_foldable，集中所有引用 */
    {"type": "text", "text": "[ref:doc-A]\n<doc-A 全文>\n[/ref:doc-A]"},
    {"type": "text", "text": "[ref:doc-B]\n<doc-B 全文>\n[/ref:doc-B]",
     "cache_control": {"type": "ephemeral", "ttl": "1h"}},   // ★ BP1：G1 末尾

    /* 🔴 no_cache：必须在所有 BP 之后 */
    {"type": "text", "text": "Current time: 2026-05-06 14:32:07"}
  ],
  "messages": [
    /* 🟢 Turn-1 user_input */
    {"role": "user", "content": [
      {"type": "text", "text": "请用 [ref:doc-A] 的规则重构 login.py"}
    ]},
    /* 🟡 Turn-1 assistant + tool_result */
    {"role": "assistant", "content": [
      {"type": "text", "text": "好的，先读取..."},
      {"type": "tool_use", "id": "toolu_01", "name": "Read", "input": {/* sorted keys */}}
    ]},
    {"role": "user", "content": [
      {"type": "tool_result", "tool_use_id": "toolu_01",
       "content": [{"type": "text", "text": "<login.py 内容>"}]}
    ]},

    /* ... Turn-2 ... Turn-19 ... */

    /* ★ BP2：当前-19 轮处的中段锚（5m）—— 挂在 Turn-1 末尾的最后一块 */
    /* ★ BP3：最新 turn 末尾（5m）*/

    /* 🔴 Turn 末尾的 no_cache 元数据，挂在 BP3 之后 */
    {"role": "user", "content": [
      {"type": "text", "text": "[meta] turn=20 wallclock=2026-05-06T14:35:01Z"}
      // 此 block 不带 cache_control，且在 BP3 之后 → 不进任何 cache hash
    ]}
  ]
}
```

### 5.2 协同模式（推理侧支持 Janus 扩展，可选）

引入 3 个自定义 header：

```http
POST /v1/messages
X-Janus-Marking: tri-color-v1
X-Janus-Span-Map: <base64(JSON)>
X-Janus-Compact-Policy: cooperative
```

`X-Janus-Span-Map` 内容：

```jsonc
{
  "version": 1,
  "spans": [
    {"id": "g0",     "range": [0, 3],   "mark": "need_cache",          "ttl_s": 3600},
    {"id": "g1",     "range": [3, 12],  "mark": "need_cache_foldable", "ttl_s": 3600,
     "fold_group": "ref-pool"},
    {"id": "t1-u",   "range": [13, 14], "mark": "need_cache",          "ttl_s": 3600},
    {"id": "t1-a",   "range": [14, 18], "mark": "need_cache_foldable", "ttl_s": 300,
     "fold_group": "turn-1"},
    {"id": "t1-tr",  "range": [18, 22], "mark": "need_cache_foldable", "ttl_s": 300,
     "fold_group": "turn-1"}
  ],
  "breakpoints": [
    {"after_block": 3,  "name": "BP0", "ttl_s": 3600},
    {"after_block": 12, "name": "BP1", "ttl_s": 3600}
  ]
}
```

服务端据此**按 fold_group 维度管理 KV blocks**——比"按 cache_control hash"细一个量级。range 是 block 级（不是 token 级），与 lookback 单位对齐。

---

## 6. 生命周期：5 个事件循环

```
┌─────────────┐  init       ┌──────────────┐  per-turn   ┌──────────────┐
│ Task Start  │────────────▶│ Active Loop  │◀───────────▶│ Append Turn  │
└─────────────┘             └──────┬───────┘             └──────────────┘
                                   │
                ┌──────────────────┼──────────────────┐
                ▼                  ▼                  ▼
         ┌────────────┐     ┌────────────┐     ┌────────────┐
         │ 19-turn BP │     │ TTL        │     │  Compact   │
         │  Insert    │     │  Refresh   │     │  Trigger   │
         └────────────┘     └────────────┘     └────────────┘
```

### 6.1 Init

- 构造 G0 + G1，挂 BP0 / BP1。
- G1 内每个 ref 给一个**稳定** `ref:<slug>` 锚名（不带版本号、不带时间戳——锚名一旦改变，引用槽设计的"折叠不影响 hash"前提就破了）。

### 6.2 Per-turn append

- 拼接 Turn-N Block，**绝不修改任何已存在的 block**（增量不变量）。
- 当 `N % 19 == 0` 时，给 Turn-N 末尾的最后一个 block 挂新 BP3；旧 BP3 降级为 BP2，旧 BP2 取消。

### 6.3 TTL Refresh（cron-like 后台任务）

```python
def refresh_loop():
    """每 60s 醒一次，做接近过期的 BP 续期。"""
    now = time.monotonic()
    for bp in [BP0, BP1, BP2, BP3]:
        if bp is None or bp.invalidated:
            continue
        # 5m TTL 在剩 60s 时续；1h TTL 在剩 10min 时续
        ttl_s = 300 if bp.ttl == "5m" else 3600
        margin = 60 if bp.ttl == "5m" else 600
        if (bp.last_refreshed_at + ttl_s - margin) < now:
            send_prewarm_keepalive(prefix_up_to=bp)
            bp.last_refreshed_at = now
```

利用 Anthropic 的 **`max_tokens: 0` 预热**：服务端跑完 prefill、写缓存、立刻返回空 content，**但绝非零成本**——见 §6.3.1。

**预热请求的限制**（R5 修订）：`max_tokens: 0` **不兼容** `stream: true`、extended thinking (`thinking.type: "enabled"`)、structured outputs (`output_config.format`)、`tool_choice` 为 `{"type":"tool",...}` 或 `{"type":"any"}`。续期 loop 必须用独立的"非流式、关闭 thinking、tool_choice=auto"配置发起，与业务请求解耦。

### 6.3.1 续期成本核算与自适应策略（v2 R9 / C4 新增）

v1 把续期描述为 "几乎零成本" 是错的。`max_tokens: 0` 不计 output，但 prefill 仍按 **cache_read（已命中）** 或 **cache_creation（首次或失效后）** 计费：

**单 session/天 续期成本估算**（典型 G0+G1 = 30k tokens）：

$$\text{cost}_{\text{refresh/day}} = N_{\text{refresh}} \times \text{tokens}_{\text{prefix}} \times \text{rate}_{\text{read}}$$

- 50min 续 1 次 → 24h ≈ 29 次
- 30k × 29 × 0.1× ≈ **87k token-equivalent / 天 / session**
- 100 并发 session → ~9M token-eq/天，是真钱

失效后首次续期按 cache_creation 1.25× 计费——若 BP0 因 tools/system 偶尔变化，整轮都按 1.25× 走。

**盈亏点**：单次续期之间至少要服务多少真实请求才回本？

$$\text{net\_savings} = N_{\text{requests}} \cdot \text{tokens}_{\text{prefix}} \cdot 0.9 \;-\; N_{\text{refresh}} \cdot \text{tokens}_{\text{prefix}} \cdot 0.1$$

要 net_savings ≥ 0，需 $N_{\text{requests}} / N_{\text{refresh}} \geq 1/9 \approx 11\%$。即每次续期之间至少要服务 ~11 次真实请求才回本。低活跃 session（夜间、异步任务、长 idle 的对话）容易跌破。

**自适应续期策略**（替代 §6.3 的固定 cron）：

```python
def adaptive_refresh_loop():
    """按需续期：仅在过去窗口内有足够真实请求时才续。"""
    REFRESH_THRESHOLD = 11  # 每续期间隔至少这么多真实请求才值得续
    for bp in [BP0, BP1, BP2, BP3]:
        if bp is None or bp.invalidated:
            continue
        ttl_s = 300 if bp.ttl == "5m" else 3600
        margin = 60 if bp.ttl == "5m" else 600
        if not (bp.last_refreshed_at + ttl_s - margin) < now:
            continue
        # 关键：检查窗口内真实请求数
        recent_real_reqs = count_real_requests_since(bp.last_refreshed_at)
        if recent_real_reqs < REFRESH_THRESHOLD:
            # 不值得续——让 cache 自然过期
            # 下次真实请求到来时按 cache_creation 重建（一次 1.25× 而非持续 0.1×）
            bp.invalidated = True
            continue
        send_prewarm_keepalive(prefix_up_to=bp)
        bp.last_refreshed_at = now
```

这把固定成本变成 demand-driven。**activity-rich session 仍享受持续命中；idle session 不再被续期账单蚕食。**

### 6.4 Compact Trigger（client 主动）

触发条件（任一即可）：
- 累计 `cache_creation_input_tokens` > 阈值（默认 50K）
- Turn 数 > 阈值（默认 60）
- foldable 区总长度 > prompt 长度的 70%
- 服务端响应 header `X-Janus-Cache-Pressure: high`（协同模式才有）

执行步骤见 §7。

### 6.5 Server Pressure Feedback（协同模式可选）

```http
HTTP/1.1 200 OK
X-Janus-Cache-Pressure: high
X-Janus-Suggest-Fold: turn-1,turn-2,turn-5
```

client 据此**主动发起部分 fold**，不等到 compact 阈值。

---

## 7. Compact 操作：双模实现

### 7.1 公共 API 模式（fallback，默认）

```python
def compact_public_api():
    """
    重建 prompt：保留所有 need_cache，把所有 need_cache_foldable 替换为占位符。
    BP0（G0 末尾）字节未变 → 下次请求 BP0 仍 lookback 命中。
    BP1 slot 销毁，新建 BP1' 挂在折叠后的 G1 末尾（TTL=5m）。
    BP2/BP3 必然失效（其 prefix 包含 placeholder 字节）。
    """
    new_prompt = []
    for block in current_prompt:
        if block.mark == "need_cache":
            new_prompt.append(block)
        elif block.mark == "need_cache_foldable":
            new_prompt.append(make_placeholder(block.fold_group))
        # no_cache 块照旧重新生成（本来就不进 hash）

    # ─── BP slot 重新分配（不是 TTL 降级，是销毁 + 重建）─────
    # BP0  : G0 字节未变 → 命中
    # BP1  : 销毁（停止发送原 G1 内容，原 KV 条目自然 GC）
    # BP1' : 新建，挂折叠后 G1 末尾，TTL=5m
    # BP2/BP3 : 必须重新规划（其 prefix 已含 placeholder）

    return rebuild_with_breakpoints(new_prompt, [
        (g0_end,        "1h"),   # BP0 不变
        (folded_g1_end, "5m"),   # BP1' (新 slot)
        # BP2'/BP3' 重新规划
    ])
```

**正确性论证（v2 R6 / C1 修订，重写）**：v1 §7.1 "占位符无需等长" 的论证只覆盖 BP0，没覆盖 BP1/BP2/BP3。Anthropic 的 prefix cache 是 **token-sequence content-addressed**：

$$\text{hash}(\text{BP}_k) = H(\text{token}[0..\text{position}(\text{BP}_k)])$$

placeholder 字节（甚至单字节）改变后：

| BP | 位置相对 placeholder | Compact 后状态 |
|---|---|---|
| **BP0** | 之前 | ✓ 命中（prefix 字节未变）|
| **BP1** | — | 销毁（不再发送）|
| **BP1'** | 折叠 G1 末尾，新 slot | 首次请求按 cache_creation 写入 |
| **BP2/BP3** | 之后 | ✗ 必然失效（prefix 包含了 placeholder 字节）|

**诚实的代价**（v1 "5-10%" 的估算是错的）：fallback 模式 compact 后，未命中区 = `placeholder + 之后所有 turn block`（包括 user_query 的 need_cache 子块、所有 foldable assistant/tool_result）。这远远超出 v1 P2 "每轮 cache_creation ≤ tokens(new_turn_block)" 的承诺。

**v2 解决方案**：把 P2 拆成两个核算口径：

1. **稳态期**（两次 compact 之间的常规轮次）：仍承诺 `cache_creation ≤ tokens(new_turn_block)`。
2. **Compact 事件**（每 ~60 turn 触发一次）：单独核算，amortized over 间隔。典型成本是
   $$\text{cost}_{\text{compact}} = \text{tokens}(\text{placeholder}) + \sum_i \text{tokens}(\text{user\_query}_i) + \text{tokens}(\text{folded\_assistant\_summary})$$
   按 60 turn amortize 后，平均每轮额外摊销 ~`tokens(historical_user_queries)/60`。

**真正满足 v1 原 P2 承诺的路径**只有协同模式（§7.2）——server 维持 BP1' 之后 token 的 KV 不变，client 只是发新 prompt。fallback 模式下这是不可能的，因为 client 无法控制 server 的 hash 计算。

### 7.2 协同模式（cooperative compact）

新增端点 `/v1/cache/fold`：

```http
POST /v1/cache/fold
X-Janus-Marking: tri-color-v1
Content-Type: application/json

{
  "session_id": "<sid>",
  "fold_groups": ["turn-1", "turn-2", "ref-pool"],
  "policy": "evict_kv_blocks"
}
```

服务端动作：
1. 查 span_map → 找到 `fold_groups` 涉及的 KV block 范围
2. 从 prefix-cache radix tree 中**剪掉这些子树**（vLLM `evict_blocks`、SGLang radix-cache `unpin`）
3. **保留** `need_cache` 部分的 KV
4. 返回新的 cache state hash 与剩余 block 范围

后续请求：
- client 重发 prompt（已 compact 过）
- 服务端 prefix-cache 命中**只剩 need_cache 的那部分**——真正的 fine-grained cache eviction
- 比 fallback 模式省一次"foldable 内容做 read 计费"

### 7.3 统一接口（client 不感知模式差异）

```typescript
async function compact(): Promise<CompactResult> {
  if (engineCaps.supportsCooperativeCompact) {
    return await server.foldGroups(activeFoldableGroups());   // 7.2
  } else {
    return await rebuildAndResend();                          // 7.1
  }
}
```

由 `EngineCapabilities.supportsCooperativeCompact: boolean` 路由（agent-janus 现有 capability 系统的天然扩展）。

---

## 8. 引用天然折叠（P3 实现细节）

整个架构最巧妙的一处：**约束 prompt 任何位置提到文档时只用引用锚名，不复制内容**。

### 8.1 错误用法 vs 正确用法

```
✗ Bad:
  user: "请基于以下代码重构: <粘贴 4000 行 login.py>"
       └─ 4000 tokens 的内容直接进 user_input

✓ Good (Janus-Prompt):
  G1 ref-pool: "[ref:login.py]\n<4000 行 login.py>\n[/ref:login.py]"  (foldable)
  user: "请基于 [ref:login.py] 重构"                                    (need_cache, 8 tokens)
```

### 8.2 Compact 前后对比

```
Compact 前:
  G1: "[ref:login.py]\n<4000 行内容>\n[/ref:login.py]"    ← 4000 tokens
  user: "请基于 [ref:login.py] 重构"                       ← 8 tokens

Compact 后（fallback 模式）:
  G1: "[ref:login.py: folded, 4000 tokens, available on demand]"   ← 12 tokens
  user: "请基于 [ref:login.py] 重构"                                ← 8 tokens (字节未变!)
```

**user 消息字节 0 改动** → user 这条 `need_cache` 块仍可被未来请求的 lookback 命中。

### 8.3 引用槽是单向的

如果模型说"我需要重新看 login.py"，agent 重新 `Read` 一次（产生新的 turn block，新的 foldable tool_result）即可——**不**在 G1 里直接 unfold。这避免了"折叠/反折叠"的状态机复杂度。

### 8.4 锚名稳定性约束

`ref:<slug>` 必须满足：
- 不带版本号、不带时间戳、不带 hash
- 同一文档在整个会话期内锚名固定
- 跨会话可以变（不同会话的 cache 本来就互相隔离）

违反此约束 → 引用点字节变化 → user_input 的 prefix hash 变 → BP 失效。

### 8.5 协议层强制（v2 R8 / C3 新增）

**v1 设计的最薄一环**：`[ref:slug]` 是裸字符串，I3 不变量靠 "约定" 维持，没有协议层强制。失败模式：

- agent 框架做 query rewriting / paraphrasing
- 模型在 assistant 回复里重述 user 提问时改了写法
- 多 agent 系统不同 agent 用不同锚名约定
- 大小写、连字符 / 下划线、Unicode normalization 差异

**v2 强制三层防线**（按强度递增）：

**L1 — Emit-time lint（必须实现，门槛低）**：

ref-pool 注册时 freeze 一个 `Set<slug>`。emit 阶段对所有 user/assistant 文本块跑 regex 扫描：

```typescript
const REF_RE = /\[ref:([a-zA-Z0-9_\-\.\/]+)\]/g;
for (const block of allTextBlocks) {
  for (const match of block.text.matchAll(REF_RE)) {
    const slug = match[1];
    if (!refPool.has(slug)) {
      throw new JanusInvariantError(
        `Unregistered ref slug "${slug}" in block. Either register it in ref-pool or remove the reference.`
      );
    }
    // 大小写 / 拼写漂移检测
    const canonical = refPool.canonicalize(slug);
    if (canonical !== slug) {
      throw new JanusInvariantError(
        `Ref slug "${slug}" doesn't match canonical form "${canonical}". Refusing to emit.`
      );
    }
  }
}
```

fail-fast，不要让坏 prompt 偷偷上线。

**L2 — 结构化 ref block（强烈建议，长期）**：

把 ref 提升为**结构化 content block 类型**（类似 tool_use 的 id 引用），而不是文本子串：

```jsonc
{"type": "text", "text": "请用"},
{"type": "janus_ref", "ref_id": "login.py"},  // 结构化 block，emit 时翻译
{"type": "text", "text": "的规则重构"}
```

emit 阶段把 `janus_ref` block 翻译成稳定的 `[ref:login.py]` 文本。这样模型 / 上游 agent 即使重写文本，结构化 block 也不会被改写。P3 才真正变成不变量而非约定。

**L3 — Span-map 引用（仅协同模式）**：

服务端协同模式（§5.2）下，span_map 直接用 block index 引用，文本里完全不出现锚名字符串——彻底消除文本漂移面。

**v2 实现优先级**：L1 必须做（agent-janus 实现 Janus-Prompt 时一并落地）；L2 列入 Phase 2 路线图；L3 与 §7.2 cooperative compact 同期。

---

## 9. 双 TTL 自然分层

| 区域 | BP slot | cache_control TTL | 续期策略 | 实际生命周期 |
|---|---|---|---|---|
| G0 (need_cache) | BP0 | 1h | 自适应（§6.3.1）| 持续命中，仅在 tools/system 改变时失效 |
| G1 (foldable, 初始) | BP1 | 1h | 自适应 | 持续命中，**直到 compact 销毁 BP1** |
| 折叠后 G1 (compact 后) | **BP1'**（新 slot）| **5m** | 自适应 | compact 后到下次 compact 之间 |
| Turn-N foldable 段 | — | 5m | 每 4min 自适应（活跃时）| 5m 滑动窗口，让旧轮自然回收 |
| 当前 Turn 末尾 | BP3 | 5m | 每轮自动刷新 | 增量命中 |

**v2 R7 / C2 关键澄清**：v1 §9 表格说 "G1 初始 TTL=1h"、§7.1 又说 "折叠后 TTL 降为 5m"，容易让人以为是同一 BP slot 改 TTL（违反 I6 单调约束）。**正确语义是 slot 销毁 + 重分配**：

1. Compact 触发时，**BP1 slot 被销毁**：client 停止在请求里发送原 G1 内容（也就停止了对该 cache 条目的引用），原 KV 条目在 1h TTL 内自然 GC。
2. 重建 prompt 时，**新建一个独立的 BP1'** 挂在折叠后的 G1 末尾，TTL=5m（因为可能再次折叠）。
3. 这两步合在一起看似 "TTL 从 1h 降到 5m"，但物理上是两个不同的 cache 条目，I6 单调约束并未被违反。

v0 "G1 用 1h 写、5m 续"在同一条目上同时声明两个 TTL 仍然是非法的。

---

## 10. 与 agent-janus 现有代码的对接

| Janus-Prompt 概念 | agent-janus 现有载体 | 需要新增/扩展 |
|---|---|---|
| 三色标记 | `Annotation` 接口（[bridge/src/core/ir/types.ts](../agent-janus/bridge/src/core/ir/types.ts)）| 新增 `janusMark: "need_cache" \| "need_cache_foldable" \| "no_cache"` |
| 4 段物理布局 | `BridgeOperation.req.{systemSegments, messages}` | 新增 `Operation.layoutPolicy: "tri-color"` |
| 滚动 BP 调度 | 当前没有 | 新增 `bridge/src/efficiency/bp-scheduler/` |
| TTL 续期 | 当前没有 | 新增 `bridge/src/efficiency/ttl-refresh/` 后台 timer |
| Compact (fallback) | Strategy E-1 dedup 雏形（[result-externalization/dedup.ts](../agent-janus/bridge/src/efficiency/result-externalization/dedup.ts)）| 扩展为整个 fold_group 维度的剪枝 |
| Compact (cooperative) | `/v1/cache/fold` 端点 | 新增 `bridge/src/protocols/janus-compact/` 协议 |
| `no_cache` 自动后置 | Strategy A-4 `stripDynamicFields`（[prefix-normalization/system.ts](../agent-janus/bridge/src/efficiency/prefix-normalization/system.ts)）| 把 5 个 regex 剥离结果统一翻成 `mark: no_cache` 的尾段 |
| Span map 序列化 | 没有 | 新增 `bridge/src/core/span-map/` |
| 引用锚名管理 | 没有 | 新增 `bridge/src/efficiency/ref-pool/` |

落到现有 Strategy 表（[PLAN_AND_PROGRESS.md](../agent-janus/PLAN_AND_PROGRESS.md) §5）：

> **Janus-Prompt = Strategy A + E-1 + 新增 Strategy G（三色 layout）+ 新增 Strategy H（cooperative compact）**

Strategy A/E 是底层不变量；G 是 layout policy；H 是协同协议。

---

## 11. 不变量清单（实现/审计 checklist）

| # | 不变量 | 违反后果 | 检测方式 |
|---|---|---|---|
| **I1** | 顺序：每段组**及每个 message 内部** `[need_cache*, foldable*, no_cache*]` 严格有序 | hash 漂移、无法增量 | 启动期 lint + assert |
| **I2** | 增量：新追加的 block 永远在已有 block 之后，不插入中间。**适用粒度**：检测目标是 user message 内的 `need_cache` 子块字节，不是整个 user message（user message 的 no_cache 子块允许每轮变） | 历史 BP 全部失效 | wire-emit 期 sequence check（按 janusMark 分桶 hash）|
| **I3** | 引用锚不变：`ref:<slug>` 在整个会话期不变。**v2 强化**：必须由 emit-time lint 强制（§8.5 L1），不能依赖文本约定；长期目标是结构化 ref block（§8.5 L2）| 引用点 hash 变 → user_input 失效 | ref-pool 注册时 freeze + emit 期 regex 校验 + canonicalization 检查 |
| **I4** | BP0 永生：BP0 prefix hash 在整个会话期不变（除非 G0 真改）| 整个永久基础失效 | 每次 emit 比对 `lastBp0Hash` |
| **I5** | Fold 安全性：折叠 foldable 不影响任何 need_cache 的 prefix hash。**v2 修订**：fallback 模式下此不变量仅对 BP0 之前的 `need_cache` 成立；BP0 之后的 `need_cache` block prefix 必然包含 placeholder 字节 → 必然失效（详见 §7.1 R6 修订）| compact 后期望命中的 BP miss | compact 路径单元测试 + 按 BP 位置分类 |
| **I6** | TTL 单调：**同一 BP slot** 的 TTL 只能升级（5m→1h），不能降级。**v2 澄清**：compact 时 BP1→BP1' 是 slot 销毁 + 重分配，**不是同一 slot 改 TTL**，不违反此约束（§9 / §7.1 R7 修订）| Anthropic 返回 400 | BP-scheduler 状态机约束 |
| **I7** | TTL 排序：同一请求内 1h BP 必须在 5m BP 之前（按物理顺序）| Anthropic 返回 400 | wire-emit 期校验 |
| **I8** | no_cache 后置：所有 `no_cache` block 必须在所有 BP 之后 | 这些块进了 hash → 每次都 miss | wire-emit 期校验 |
| **I9** | BP slot ≤ 4：cache_control 总数不超过 4 | Anthropic 返回 400 | BP-scheduler 总量约束 |
| **I10** | **User message 内部切分（v2 R11 / C6 新增）**：user message **必须**是多 block 复合体，纯提问标 `need_cache`、harness envelope（cwd/git/timestamp/system-reminder）必须切到 `no_cache` 子块，且子块顺序遵循 I1 | 整个 Turn-N 用户块 hash 漂移 → BP3 每轮失效 | wire-emit 期 splitter（扩展 Strategy A-4 `stripDynamicFields` 到 user 段）|
| **I11** | **续期成本守门（v2 R9 / C4 新增）**：BP-scheduler 在续期前必须检查 `recent_real_requests >= REFRESH_THRESHOLD`，否则跳过续期、让 cache 自然过期（§6.3.1）| 低活跃 session 续期成本 > 收益 | refresh loop 内置 demand-driven gate |

---

## 12. 未决问题 / Roadmap

| # | 问题 | 状态 |
|---|---|---|
| **Q1** | 当 G1 引用区初始就超过 lookback 窗口（>20 blocks）时如何分片？ | 待设计：可能需要 G1a / G1b 双 BP，但会挤占 BP slot |
| ~~Q2~~ | ~~"user_input 是 need_cache" 在用户复制粘贴大段文本到 user 消息时仍成立吗？~~ | **v2 R11 已解决**：§3.2 强制 user message 多 block 切分；harness 注入 + 大段粘贴文本统一走 no_cache / ref-pool 迁移 |
| **Q3** | `/v1/cache/fold` 协议在 vLLM / SGLang 的实现路径 | 列入 Strategy F（engine-side patches），独立 Phase 6 sub-project |
| **Q4** | 协同模式下 server 主动 evict 与 client 续期 loop 的竞争条件 | 需要乐观并发：server 返回 `cache_state_hash`，client 续期时带上做 CAS |
| **Q5** | 与 Anthropic 自动 caching（顶层 `cache_control`）的冲突 | 需要文档：Janus-Prompt 必须用显式 BP，不能开自动 |
| **Q6** | extended thinking 块的处理 | thinking 不能直接挂 cache_control，但它在缓存中的隐式行为对 Opus 4.5+ 与早期模型不同；需在 span_map 里单独标 `mark: implicit_cached` |
| **Q7** | image / document 块的引用槽化 | 文档说 image 任意位置变化会全段失效；需要"image 必须只在 G1 出现"的强约束 |

---

## 13. 一句话总结

> Janus-Prompt 用 **三色标记 + 4 段布局 + 双锚 + 滚动中段锚 + 引用槽** 五件事，把"agent prompt 必然变化"的事实从"cache 的敌人"转成"cache 的设计输入"：稳定段永驻（need_cache, 1h），可重生段可丢（foldable, 5m），动态段永远在 hash 之外（no_cache）。在公共 Anthropic API 上靠 BP0/BP1 双锚 + lookback + `max_tokens:0` 续期实现；在协同推理后端上靠 `/v1/cache/fold` 实现细粒度 KV 淘汰。client 和 server 共用一套 span_map 词汇表，互相知道哪些 token 重要、哪些可以现在就扔。
