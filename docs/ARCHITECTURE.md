# STELA — 架构设计与代码实现参考

> 本文是 `telos-sdk` 仓库（Python `stela` 包）的权威架构文档，覆盖设计理念、
> 三层结构、核心数据结构、每个模块的代码实现、两条接入路径、RTK 过滤层、
> 录制/重放对照，以及全部不变量与扩展点。
>
> - 想上手用 → [playbook.md](playbook.md)、[User-guide.md](User-guide.md)
> - 想看协议规范 → [2026-05-06-stela-protocol.md](2026-05-06-stela-protocol.md)
> - 想看改动史 → [../CHANGELOG.md](../CHANGELOG.md)
>
> 注：`docs/ARCHITECTURE_AND_GUIDE.md` 描述的是更早的 TypeScript `agent-janus/`
> 布局，**已过时**，以本文为准。
>
> 最后更新：2026-05-16

---

## 目录

1. [设计理念](#1-设计理念)
2. [仓库地图](#2-仓库地图)
3. [三层架构总览](#3-三层架构总览)
4. [核心数据结构 —— StelaIR](#4-核心数据结构--stelair)
5. [Bridge —— 策略核心](#5-bridge--策略核心)
6. [Harness 层](#6-harness-层)
7. [Engine 层](#7-engine-层)
8. [ref-pool](#8-ref-pool)
9. [接入路径 A / B](#9-接入路径-a--b)
10. [RTK 输出过滤层](#10-rtk-输出过滤层)
11. [录制与重放对照](#11-录制与重放对照)
12. [可观测性](#12-可观测性)
13. [不变量与设计约束](#13-不变量与设计约束)
14. [端到端数据流](#14-端到端数据流)
15. [扩展点](#15-扩展点)

---

## 1. 设计理念

### 1.1 要解决的问题

LLM 推理的 KV cache 能把「重复出现的前缀」的计算结果留住，命中时
input token 价格打到 ~10%（Anthropic）。但 agent 的多轮对话默认**不**
cache 友好：每轮请求里 system prompt、工具定义、历史对话拼接顺序稍有
抖动（JSON key 乱序、工具数组顺序变化、时间戳混进前缀），前缀 hash
就变，cache 全部失效。

STELA 的全部价值是一句话：**把真正稳定的部分稳住，让它一直命中 KV
cache。** 其余复杂度全部砍掉。

### 1.2 "石碑" 比喻

STELA = **S**table prefix · **T**iered bands · **E**phemeral tail ·
**L**ayered adapters · **A**nchored marks。

石碑底座的铭文（durable prefix）刻一次用一辈子；上方按时间累加的题字
（每轮新内容）随时可擦改，但不会动到底座。KV cache 的全部价值就是把
底座留住。

### 1.3 三个核心设计决策

1. **三色带（Band）一刀切**。每个内容块必落 `PIN` / `FOLD` / `DROP`
   之一，没有"既可缓存又不可缓存"的灰色态。
2. **顺序不变量是唯一硬约束**。每个段内 blocks 必须物理排成
   `pin* → fold* → drop*`（协议 §5）。违反即抛 `StelaInvariantError`。
   其余都是软建议。
3. **三层只往下传值，不反向引用**。harness 不认识 engine，engine 不认识
   harness，中间的 IR 是唯一契约。跨请求状态只允许存在于 Bridge 持有的
   `BridgeSessionState`。

### 1.4 正交的第二条优化线 —— RTK

STELA 稳的是**请求前缀**（system / tools / 对话前缀）。但每轮 agent 还会
往对话尾部追加大段工具输出（bash / pytest / docker 日志）。STELA 吸收了
[rtk-ai/rtk](https://github.com/rtk-ai/rtk) 的思路，加了一层正交的
**RTK 输出过滤**：在请求进 STELA 管线前，把 `tool_result` 里的大段重复
输出压缩掉。两条线由 `StelaMode` 四态开关独立控制（见 §10）。

---

## 2. 仓库地图

```
telos-sdk/                         (Python 包名 = stela，pyproject 把根目录映射成 stela)
│
├── ir.py                  核心数据结构：Band / StelaBlock / StelaMessage / StelaIR / UsageReport
├── bridge.py              策略核心：5 原语 + canonicalize + BridgeSessionState
├── refpool.py             ref-pool：大内容的"指针表"，slug 冻结
├── registry.py            按名加载 harness / engine 的工厂
├── cli.py                 `stela` 统一 CLI：proxy / init / dashboard / replay
├── corpus.py              会话语料库：录原始请求供 replay
│
├── harness/               第 1 层：上游 agent 请求 → StelaIR
│   ├── base.py            HarnessPlugin ABC
│   ├── _user_split.py     user 文本 envelope 切分（PIN/FOLD/DROP）
│   ├── openclaw.py        OpenClaw（Anthropic /v1/messages 形状）
│   ├── hermes.py          Hermes / Claude Code（Anthropic 形状 + 不同 envelope）
│   └── telos.py           Telos（OpenAI ChatCompletions 形状）
│
├── engine/                第 3 层：StelaIR → 各引擎 wire 请求
│   ├── base.py            EngineAdapter / BidirectionalEngineAdapter / EngineCapabilities
│   ├── anthropic.py       AnthropicAdapter（唯一支持显式 BP）
│   ├── openai.py          OpenAIAdapter（layout + routing key）
│   ├── deepseek.py        DeepSeekAdapter（零控制面）
│   ├── vllm.py            VLLMAdapter（双向）
│   └── sglang.py          SGLangAdapter（双向，vLLM 超集）
│
├── output_filter/         RTK 风格工具结果过滤层（与 STELA 正交）
│   ├── mode.py            StelaMode 四态开关
│   ├── filters.py         ToolResultFilter / RtkFilter / FallbackFilter / CompositeFilter
│   └── preprocess.py      apply_filter：改写 raw 请求里的 tool_result
│
├── proxy/                 接入路径 B：HTTP 反向代理
│   ├── server.py          aiohttp 反向代理（SSE-aware）
│   ├── pipeline.py        process_anthropic_request：parse→bridge→emit 纯函数
│   ├── inspector.py       SessionInspector：内存里的诊断快照存储
│   └── __main__.py        `python -m stela.proxy` 入口
│
├── replay/                录制 → 重放对照引擎
│   ├── __init__.py        replay_session 引擎
│   └── __main__.py        `stela replay` CLI
│
├── init/                  接入路径 B 的安装器
│   ├── base.py            AgentInstaller ABC + InstallResult
│   ├── claude_code.py     patch ~/.claude/settings.json
│   ├── generic.py         打印 export 指令
│   └── __main__.py        `python -m stela.init` 入口
│
├── scripts/
│   ├── stela_anthropic_transport.py   接入路径 A（Anthropic 形状）+ _detect_harness
│   ├── stela_transport.py             接入路径 A（OpenAI 形状）
│   ├── build_savings_dashboard.py     省钱看板（含 mode / A/B 对比面板）
│   ├── build_developer_page.py        开发者 inspector 页面
│   └── show_prompt_trace.py           prompt_trace.jsonl 的终端美化打印
│
├── tests/                 测试套件（77 个测试函数）
└── docs/                  设计与使用文档
```

---

## 3. 三层架构总览

```
上游 agent（Claude Code / OpenClaw / Hermes / 自研）
    │  原始请求（Anthropic /v1/messages 或 OpenAI ChatCompletions）
    ▼
┌─────────────────────────────────────────────────────────┐
│ 第 1 层 · HARNESS（无状态纯函数）                          │
│   harness.parse(raw) → StelaIR                           │
│   职责：envelope 切分、大文档进 ref-pool、内容分带          │
└──────────────────────────┬────────────────────────────────┘
                           ▼  StelaIR
┌─────────────────────────────────────────────────────────┐
│ 第 2 层 · BRIDGE（有状态，每 session 一个）                │
│   5 原语：place / pin / mark / fold / refresh             │
│   canonicalize（key 排序、工具排序）+ §5 不变量校验        │
│   持有 BridgeSessionState（ref-pool、R8 计数）             │
└──────────────────────────┬────────────────────────────────┘
                           ▼  StelaIR（改写后）+ EmitPlan
┌─────────────────────────────────────────────────────────┐
│ 第 3 层 · ENGINE（无状态，capability-aware）               │
│   plan_marks(ir) → EmitPlan                              │
│   emit(ir, plan) → wire 请求                              │
│   parse_usage(response) → UsageReport                    │
└──────────────────────────┬────────────────────────────────┘
                           ▼  引擎原生 wire 请求
真实 LLM 服务（Anthropic / OpenAI / DeepSeek / vLLM / SGLang）
```

**核心不变量**：跨请求状态只能存在于第 2 层。harness 和 engine 都是纯
函数 / 无状态对象，相同输入永远输出相同结果。

`registry.py` 提供 `load_harness(name)` / `load_engine(name)`，让 bridge
不直接 import 任何具体实现 —— 这是"三层只往下传值"在代码层的落地。

---

## 4. 核心数据结构 —— StelaIR

定义在 [ir.py](../ir.py)。所有 dataclass 都 **frozen（不可变）**：bridge 的
"修改"返回新 IR，不在原对象上改字节，避免共享状态写竞争。

### 4.1 Band —— 三色带

```python
class Band(str, Enum):
    PIN  = "pin"    # 长寿稳定段：tools 定义、system prompt、用户当下提问
    FOLD = "fold"   # 可缓存但 compact 时可丢：assistant 回答、tool_result、ref-pool 大文档
    DROP = "drop"   # 永不进 cache hash：timestamp、cwd、git status、envelope；必须在段末
```

排序权重 `_BAND_RANK = {PIN: 0, FOLD: 1, DROP: 2}`。

### 4.2 StelaBlock —— 最小内容单元

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | `str` | 会话内稳定标识（诊断 / 引用用） |
| `band` | `Band` | 三色带归属 |
| `kind` | `BlockKind` | `text` / `tool_def` / `tool_use` / `tool_result` / `image` / `thinking` |
| `payload` | `Any` | engine-agnostic 内容；emit 时由 adapter 翻译 |
| `ref_slug` | `str \| None` | 非空表示此 block 来自 ref-pool |
| `source_tag` | `str \| None` | 诊断字段：哪条 harness 规则分的带 |
| `extra` | `Mapping` | engine 需要的稳定旁信息（如 image 的 `detail`、工具的 `source`/`mcp_server`） |

> **关键约束**：必须进 cache hash 的字段都要写进 `extra`、由 adapter 在
> emit 时一并序列化。harness 决不能在 emit 时刻才注入字段，否则字节稳定
> 性被破坏。

### 4.3 StelaMessage / StelaIR / StelaHints

```python
@dataclass(frozen=True)
class StelaMessage:
    role: Literal["system", "user", "assistant"]
    blocks: tuple[StelaBlock, ...]      # 必须满足 §5：pin* → fold* → drop*

@dataclass(frozen=True)
class StelaIR:
    session_id: str
    tools:    tuple[StelaBlock, ...]    # 全部 band=PIN
    system:   tuple[StelaBlock, ...]    # pin* → fold*(含 ref-pool) → drop*
    messages: tuple[StelaMessage, ...]
    ref_pool: Mapping[str, StelaBlock]  # slug → block
    hints:    StelaHints

@dataclass(frozen=True)
class StelaHints:
    engine: Literal["anthropic", "openai", "deepseek"] = "anthropic"
    model:  str = ""
    expected_turns: int = 0             # 影响 mid-rolling 锚的开关
```

### 4.4 UsageReport —— 归一化用量

所有引擎的原始 `usage` 字段都归一到这四个数，否则成本指标算不出来：

```python
@dataclass(frozen=True)
class UsageReport:
    raw_input:   int   # 未命中且未写缓存的 input token
    cache_read:  int   # 从缓存读入
    cache_write: int   # 本次请求写入缓存的新增 token
    output:      int
    raw: Mapping       # 原始 usage 字段，留作诊断
```

### 4.5 §5 校验函数

- `enforce_band_order(blocks)` —— 稳定排序成 `pin* → fold* → drop*`
  （同带内保留插入序）。harness 拼 message 时兜底调用。
- `assert_band_order(blocks, where)` —— O(n) 单次扫描，发现 rank 回退
  即抛 `StelaInvariantError`。
- `assert_ir_invariants(ir)` —— 对整份 IR 跑完整校验；额外要求 `tools`
  全部 `band=PIN`。

---

## 5. Bridge —— 策略核心

定义在 [bridge.py](../bridge.py)。每个 session 一个实例，**有状态**。

### 5.1 五个原语

| 原语 | 方法 | 作用 | 协议节 |
|---|---|---|---|
| **Place** | `place(segment, blocks)` / `append_message(msg)` | 替换某段全部 blocks / 追加一条 message，立即跑 §5 校验 | §6.1 |
| **Pin** | `pin(slug, payload)` | 注册一个 ref-pool 条目，slug 立即冻结 | §6.2 |
| **Mark** | `mark()` | 委托 engine adapter 决定本次 emit 的 cache 锚位（返回 `EmitPlan`） | §6.3 |
| **Fold** | `fold(slugs=, message_range=, summary=)` | 折叠 ref-pool 条目（只换 payload 不换 slug），或把一段历史 message 折成摘要 | §6.4 |
| **Refresh** | `refresh(plan)` | 触发 engine keep-alive；自适应门控（见 §5.4） | §6.5 |

> 注意：`pin()` 注册的是 `band=FOLD` 的可折叠条目。原语名 "Pin" 指
> "把这段大内容固定在 ref-pool 里、给它稳定指针"，**不是** `band=PIN`。

### 5.2 emit 流程

```python
def emit_with_plan(self) -> tuple[wire, EmitPlan]:
    canon = _canonicalize_ir(self._ir)        # ① 规范化
    assert_ir_invariants(canon)               # ② §5 完整校验（最后一道防线）
    refpool.lint_blocks(...)                  # ③ 扫描 [ref:slug] 引用，未注册即 fail-fast
    plan = engine.plan_marks(canon)           # ④ engine 决定锚位
    wire = engine.emit(canon, plan)           # ⑤ engine 出 wire
    stats.real_requests_since_refresh += 1
    return wire, plan
```

`emit()` 是 `emit_with_plan()` 的一元返回版。**调用方必须走 `emit*`，不能
直接 `engine.emit(ir, plan)`** —— 否则跳过 `_canonicalize_ir`。

### 5.3 Canonicalization（修复 R5）

跨 engine 通用，必须在 emit 前统一做掉。问题根源：Swift / Go 的 JSON
序列化会随机化 key 顺序，导致前缀 hash 漂移、cache 失效。

- `_canonicalize_payload` —— dict key 字典序排序（递归）。
- `_canonicalize_schema` —— JSON-Schema 子树专用：除 key 排序外，还把
  **集合语义数组**排序。当前只有 `required`（`_SCHEMA_SET_ARRAY_KEYS`）。
  故意**不**排序 `enum` / `examples` / `anyOf` / `oneOf` / `allOf`（顺序有语义）。
- `_canonicalize_tool_def` —— 识别 Anthropic（`input_schema`）/ OpenAI
  （`function.parameters`）两种工具形状，schema 子树走 schema 规范化。
- `_tool_sort_key` —— 工具数组稳定排序键 `(source_rank, mcp_server, name)`。
  `source_rank`：builtin(0) → mcp(1) → user(2) → 未标记(3)。保证多 MCP
  server 启动竞态不会在两个 server 之间交替插入而破坏前缀。

> `tool_use` / `tool_result` 的 payload 是**用户数据**，只做 key 排序，
> 绝不动数组顺序（payload 里碰巧叫 `required` 的字段不能被静默重排）。

### 5.4 BridgeSessionState —— 跨 turn 状态

```python
@dataclass
class BridgeSessionState:
    refpool: RefPool                    # slug 注册表，冻结后跨轮保持
    stats: _SessionStats                # cumulative_cache_creation + real_requests_since_refresh
    sticky_harness: str | None = None   # 首轮识别出的 harness，锁定后复用
    sticky_mode: str | None = None      # 首轮的 mode（none/stela/rtk/both），锁定
    compare_group: str | None = None    # 对比实验分组标签
```

上游（proxy / SDK transport）按 session_id 持有一份，每轮构造新 `Bridge`
时传进来。不传则 Bridge 自己 new 一个，退化为"每轮独立"。

`REFRESH_THRESHOLD = 11`（Janus §6.3.1）：**R8 自适应门控** —— 续期窗口内
真实请求数低于阈值就跳过 refresh，让 cache 自然过期，避免低活跃 session
续期成本 > 收益。

### 5.5 双向操作（仅 vLLM / SGLang）

`is_bidirectional` = `isinstance(engine, BidirectionalEngineAdapter)`。

| Bridge 方法 | 闭源 API | vLLM / SGLang |
|---|---|---|
| `probe_cache()` | 返回 `ProbeResult(hit=False)` | 真发 lookup，问"前缀还在缓存吗" |
| `cooperative_fold(...)` | 等同 `fold()` + 返回 `{}` | 客户端折叠 + 返回服务端 `evict_span` / `fork_and_replace` 片段 |
| `emit_with_extras(extras)` | 把片段合并进 `plan.extras` 再 emit | 同左 |

`cooperative_fold` 是"零重算 Fold"：闭源 API 每次 fold 都要 server 重新
prefill 整段；vLLM/SGLang 让 server 保留前缀 KV 不动、只重算摘要尾段。

---

## 6. Harness 层

第 1 层。每个 harness plugin 是**纯无状态函数**：`parse(raw) → StelaIR`。
基类 [harness/base.py](../harness/base.py) 的 `HarnessPlugin.parse` 签名：

```python
def parse(self, raw_request, *, session_id, engine, model="", expected_turns=0) -> StelaIR
```

### 6.1 分带规则（三个 harness 共通）

| 上游内容 | Band | 说明 |
|---|---|---|
| `tools[]` | PIN | `kind=tool_def` |
| `system` 文本（≤ 2048 字符）| PIN | |
| `system` 文本（> 2048）/ `<file>` 块 | FOLD（进 ref-pool）+ PIN 引用 stub | 阈值 `_REFPOOL_THRESHOLD = 2048` |
| user 文本 | → `split_user_text`（PIN/FOLD/DROP）| 见 §6.2 |
| user `tool_result` | FOLD | |
| assistant 文本 / `tool_use` / `thinking` | FOLD | |

每条 message 拼完后过一遍 `enforce_band_order`（openclaw / hermes 显式调用；
telos 因构造顺序天然合规未调用）。

### 6.2 `_user_split.split_user_text`

把一条 user 文本切成 PIN（真正的提问）+ FOLD（历史回声）+ DROP（envelope）。
正则集：

- **DROP**（每轮变化的 envelope）：`<environment_info>` / `<system-reminder>`
  / `<command-message>` / `<command-name>` / `Current time:...`
- **FOLD**（显式包裹的历史回声）：`<prev>...</prev>`
- **PIN**：剥掉上述之后剩下的就是用户提问。

返回的 tuple 已经是 §5 顺序，可直接塞进 `StelaMessage`。

### 6.3 三个 harness 的区别

| | OpenClaw | Hermes | Telos |
|---|---|---|---|
| Wire 形状 | Anthropic `/v1/messages` | Anthropic `/v1/messages` | OpenAI ChatCompletions |
| 身份标记 | 默认 / fallback | `<system-reminder>`、`<command-message>`、thinking 块、Claude Code 工具集 | 独立 transport（OpenAI 形状）|
| 工具分类 | `_classify_anthropic_tool`（type 前缀 + server 字段 + metadata）| 复用 `_classify_anthropic_tool` | `_classify_openai_tool`（仅 metadata）|
| `<file>` 入 ref-pool | 否（整个超大 system item 入池，slug `system-doc-{i}`）| 是（`<file path=...>`，点号 slug）| 是（dash slug）|
| `thinking` 块 | 不处理 | FOLD `kind=thinking` | 不适用 |
| `tool_result` 来源 | 嵌在 user message | 嵌在 user message | 独立 `role=tool` → 包装成 user |
| system 提取 | `system` 字段 | `system` 字段 | `messages[]` 开头连续的 `role=system` |
| `source_tag` 前缀 | `openclaw/*` | `hermes/*` | `telos/*` |

工具分类把 `source`（builtin / mcp / user）写进 `StelaBlock.extra`，喂给
bridge 的 `_tool_sort_key`，保护 PIN 前缀稳定。

### 6.4 `_detect_harness`（在 [scripts/stela_anthropic_transport.py](../scripts/stela_anthropic_transport.py)）

对 Anthropic 形状的流量在 `hermes` / `openclaw` 之间二选一。检测顺序
（首个命中即返回）：

1. **Envelope 标签** —— 在 system 文本**和每条 user 文本块**里跑成对
   开/闭正则（`system-reminder` / `command-message` / `command-name`）。
   成对匹配避免用户只是在散文里讨论标签而误判。命中 → `hermes`。
2. **thinking 块** —— 任意 assistant message 含 `thinking` 内容块 → `hermes`。
3. **工具指纹** —— 工具名与 `{Bash, Edit, Read, Write, Grep, Glob,
   TodoWrite, Task, WebFetch, WebSearch, NotebookEdit}` 的交集 ≥ 3 → `hermes`。
   捕捉首轮（还没注入 reminder）的请求。
4. 兜底 → `openclaw`。

结果在 session 内 sticky（写进 `BridgeSessionState.sticky_harness`），避免
逐 call 重新探测导致 harness 翻转、前缀失稳。

---

## 7. Engine 层

第 3 层。基类 [engine/base.py](../engine/base.py)。bridge 只面向抽象接口
编程，从不按引擎名分支。

### 7.1 接口契约

`EngineAdapter`（ABC）四个成员：
- `capabilities` (property) → `EngineCapabilities`
- `plan_marks(ir) → EmitPlan` —— 决定锚位
- `emit(ir, plan) → wire dict`
- `parse_usage(response) → UsageReport`
- `refresh(ir, plan)` —— 可选 keep-alive，基类默认 no-op

`MarkSlot`（引擎无关的逻辑 cache 锚）：`name` / `segment` / `index` /
`message_index` / `ttl_class`。bridge 只看 slot 列表，永远不知道
`cache_control` 长什么样。

`BidirectionalEngineAdapter` 额外加读路径 + 显式服务端状态变更：
`probe` / `evict_span` / `fork_and_replace`。闭源 API 不继承它，bridge
靠 `isinstance` 保证永不误调。

### 7.2 能力矩阵

| 字段 | Anthropic | OpenAI | DeepSeek | vLLM | SGLang |
|---|:---:|:---:|:---:|:---:|:---:|
| `explicit_breakpoints` | ✓ | ✗ | ✗ | ✓ | ✓ |
| `max_breakpoints` | **4** | 0 | 0 | 2 | 2 |
| `ttl_control` | presets(5m/1h) | presets(内存/24h) | none | none | none |
| `prewarmable` | ✓(`max_tokens:0`) | ✗ | ✗ | ✓(`max_tokens:1`) | ✓(`prewarm_only`) |
| `routing_key` | ✗ | ✓(`prompt_cache_key`) | ✗ | ✓(`cache_salt`) | ✓(`affinity_key`) |
| `cache_probe` | ✗ | ✗ | ✗ | ✓ | ✓ |
| `span_eviction` | ✗ | ✗ | ✗ | ✓ | ✓ |
| `fork_and_replace` | ✗ | ✗ | ✗ | ✗ | ✓ |
| `tier_hint` | ✗ | ✗ | ✗ | ✗ | ✓ |
| 双向类 | 否 | 否 | 否 | 是 | 是 |

### 7.3 每个引擎的 emit 策略

- **Anthropic**（[anthropic.py](../engine/anthropic.py)）—— 唯一支持显式
  断点。`plan_marks` 出至多 4 个候选 slot：**BP-T**（tools 段尾）、
  **BP-S**（system 最后一个 PIN）、**BP-R**（system 最后一个 FOLD =
  ref-pool 尾）、**BP-X**（最近 message 末非 DROP 块，5m 滚动）、
  **BP-mid**（messages ≥ 19 时在 `len-19` 处加锚，修复 R2）。超过 4 个
  按 R7 优先级 `BP-T < BP-S < BP-R < BP-mid < BP-X` 截断。emit 时在
  slot 落点的 block 上挂 `cache_control`（5m `{"type":"ephemeral"}` /
  1h 加 `"ttl":"1h"`）；DROP 块不挂、必须在所有 BP 之后。常量
  `_LOOKBACK=20`、`_MID_ANCHOR_STRIDE=19`。
- **OpenAI**（[openai.py](../engine/openai.py)）—— 无显式 BP。`plan_marks`
  只出 `routing_key`（`stela-<sha256[:16]>`，对工具 + PIN system + ref-pool
  键做 hash）+ retention。emit 把 block 排成 非DROP→DROP，让 OpenAI 的
  自动前缀匹配命中稳定头；写 `prompt_cache_key` / `prompt_cache_retention`。
  `cache_write` 恒为 0（OpenAI 隐式写缓存不单独计费）。`KEY_RPM_SOFT_CAP=12`。
- **DeepSeek**（[deepseek.py](../engine/deepseek.py)）—— 零控制面。磁盘
  context cache 永远开。`plan_marks` 返回空 `EmitPlan()`。emit 只靠把
  block 排成 非DROP→DROP 让 exact-match 前缀命中。
- **vLLM**（[vllm.py](../engine/vllm.py)）—— 双向。emit 私有扩展字段
  `cache_policy`（`pin_prefix_until_block` / `evict_span`）+ `cache_salt`。
- **SGLang**（[sglang.py](../engine/sglang.py)）—— vLLM 严格超集。emit
  私有 `cache_control`（`lock_radix_path` / `path_hash` / `prefer_tier`
  / `affinity_key` / `fork_from_path` / `replace_suffix`）。多 `fork_and_replace`
  和 `tier_hint`（HiCache GPU/CPU/disk）。

### 7.4 usage 解析

| 引擎 | cache_read 字段 | cache_write 字段 |
|---|---|---|
| Anthropic | `cache_read_input_tokens` | `cache_creation_input_tokens` |
| OpenAI | `prompt_tokens_details.cached_tokens` | 恒 0 |
| DeepSeek | `prompt_cache_hit_tokens` | 恒 0（写成本并入 miss 价） |
| vLLM / SGLang | `cached_tokens` | 恒 0 |

---

## 8. ref-pool

定义在 [refpool.py](../refpool.py)。所有大段内容的"指针表"。

- **slug 一经注册即冻结**。`register` 校验 slug 正则 `^[A-Za-z0-9_\-./]+$`、
  block 必须 `band=FOLD`、`ref_slug` 必须等于 slug。重复注册抛错。
- **`register_or_skip`** —— 幂等注册。跨 turn 共享 RefPool 时用：harness
  每轮都生产完整 payload 的 ref_pool，本方法防止第二轮把第一轮已 `fold`
  成占位符的条目覆盖回完整内容。
- **`fold(slug, summary=)`** —— 把条目换成短占位符。**slug 不动**，文本里
  所有 `[ref:slug]` 引用点字节不变 → 后续 BP 仍可命中。这是"引用天然
  折叠"的落地方式。
- **`render_blocks`** —— 按 slug 字典序渲染，保证多次 emit 字节稳定。
- **`lint_text` / `lint_blocks`** —— emit 前扫描所有 `[ref:slug]`，发现
  未注册的 slug 立即 `fail-fast`。

bridge 的 `_sync_refpool_into_system` 把 ref-pool 渲染进 system 段：
`pin* + ref-pool fold* + drop*`。

---

## 9. 接入路径 A / B

两条路径**功能等价**（同一 STELA 管线、同一状态累积、同一 `cache_control`
注入），区别只在进程边界 / 错误处理 / 流式。

### 9.1 路径 A —— SDK Transport（进程内）

把 agent 的 LLM client 换成 STELA transport，鸭子接口完全相同。

- **`StelaAnthropicTransport`**（[scripts/stela_anthropic_transport.py](../scripts/stela_anthropic_transport.py)）——
  包 `anthropic.Anthropic`，暴露 `.messages.create(...)`。`_do_create`
  流程：快照入参 → 选 harness（显式 > sticky > 自动检测）→ `harness.parse`
  → `Bridge(...).emit_with_plan()` → 透传非 STELA 字段 → 发真请求 →
  `bridge.absorb_usage` 累积 → 写 `usage_log` / `prompt_trace_log`。
- **`StelaOpenAITransport`**（[scripts/stela_transport.py](../scripts/stela_transport.py)）——
  包 `openai.OpenAI`，暴露 `.chat.completions.create(...)`。走 `telos`
  harness，用自定义 `_ir_to_chat_completions` 出 wire（保留 OpenAI 的
  `tool_calls` / `role=tool` 结构，不内联成文本）。

transport 实例 = 一个 session，内部持有 `BridgeSessionState`。

### 9.2 路径 B —— HTTP 反向代理（进程外）

agent 设 `ANTHROPIC_BASE_URL=http://127.0.0.1:7171`，零代码改动。

- **`proxy/server.py`** —— aiohttp 反向代理。`POST /v1/messages` 经
  STELA 管线后转发；SSE 流式支持（旁路解析 `message_start` /
  `message_delta` 抽 usage）；其他路径透明 passthrough。内嵌
  `/__stela/dashboard`、`/__stela/developer`、`/__stela/developer.json`。
  默认非 strict：STELA 失败降级 passthrough（`--strict` 改成返 500）。
- **`proxy/pipeline.py`** —— `process_anthropic_request(raw, ...)` 纯函数，
  把 parse → bridge → emit 拆出来，proxy 和 transport 共用，杜绝 wire 漂移。
- **session-id 派生**优先级：`x-stela-session` header → `metadata.user_id`
  → `blake2b(api_key + system + tools + messages[0])` → `stela-<16hex>`。
- **`_SessionRegistry`** —— OrderedDict LRU（默认上限 10000），按 session_id
  持有 `BridgeSessionState`。

### 9.3 安装器（路径 B）

[init/](../init/)。`AgentInstaller` ABC，要求**幂等** + `uninstall` 精确还原。

- **`ClaudeCodeInstaller`** —— 往 `~/.claude/settings.json` 的 `env` 字段
  写 `ANTHROPIC_BASE_URL`。首次 patch 备份成 `.stela.bak`；保留用户原值到
  `__stela_previous_base_url`；标记键 `__stela_installed`。原子写
  （`.tmp` + `os.replace`）。不动 npm 包、不动 PATH、`npm update` 不丢。
- **`GenericInstaller`** —— 只打印 `export ANTHROPIC_BASE_URL=...` 指令。

---

## 10. RTK 输出过滤层

[output_filter/](../output_filter/)。与 STELA 管线**正交**：STELA 稳请求
前缀拿 KV cache，本层缩工具结果尾巴减少每轮新增 token。

### 10.1 StelaMode —— 四态开关

```python
@dataclass(frozen=True)
class StelaMode:
    stela: bool = True    # 跑 STELA 管线（cache_control / ref-pool）
    rtk:   bool = False   # 跑 RTK 工具结果过滤
```

| label | stela | rtk | 含义 |
|---|:---:|:---:|---|
| `none` | ✗ | ✗ | 纯透传，proxy 不改一个字节 |
| `stela` | ✓ | ✗ | 只 STELA 前缀缓存（proxy 默认）|
| `rtk` | ✗ | ✓ | 只 RTK 工具过滤，不打 cache 标记 |
| `both` | ✓ | ✓ | 两者都开 |

未知 / 空值退化到默认 `stela`（保持引入开关前的历史行为）。

### 10.2 过滤器

- **`RtkFilter`** —— shell-out 到 `rtk` 二进制（`rtk filter --command <cmd>`
  读 stdin）。约定调用形式；任何失败退化 passthrough。
- **`FallbackFilter`** —— 无依赖的纯 Python 过滤器：连续重复行折叠成
  `<line> (×N)`、头尾截断、pytest 摘要保留。rtk 没装时保证开关仍生效。
- **`CompositeFilter`** —— rtk 优先、未省下字节再退回 fallback。
- `build_filter()` —— rtk 可用 → `Composite(rtk, fallback)`，否则纯
  `FallbackFilter`。
- 阈值：短于 600 字符的输出不过滤；dedup 后超过 4000 字符走头尾截断。

### 10.3 apply_filter

`preprocess.apply_filter(raw, flt) → (new_raw, FilterStats)` —— 纯函数，
深拷贝 raw，改写所有 `tool_result` 的文本内容（支持 str 和 block-list 两种
content 形态），从前一条 assistant message 的 `tool_use` 里查命令 hint。
`FilterStats` 记 `original_chars` / `filtered_chars` / `blocks_filtered`
/ `by_rule`。

### 10.4 proxy 接线

- `--mode` CLI 开关 + `X-Stela-Mode` header（首请求 sticky 到 session）。
- `X-Stela-Compare-Group` header → 对比实验分组。
- `mode.rtk` 开 → 进 STELA 前先 `apply_filter`；`mode.stela` 关 → 跳过
  管线走 passthrough。
- usage_log 加 `mode` / `compare_group` / `tool_output_reduction` 字段。

---

## 11. 录制与重放对照

### 11.1 corpus —— 会话语料库

[corpus.py](../corpus.py)。proxy 默认把每次调用的**原始请求**录到
`~/.stela/corpus/<session>.jsonl`（只录请求、不录响应 —— Anthropic 无状态，
第 N 轮请求已含前 N-1 轮全部内容）。`--no-record` 可关，`--corpus-dir`
可改目录。函数：`record_call` / `load_session` / `list_sessions`。

### 11.2 replay —— 受控重放对照

[replay/](../replay/)。`replay_session(turns, mode, ...)` 把一个真实会话按
某个 mode 重放：逐字节相同的轮次序列 → RTK 过滤（若 `mode.rtk`）→ STELA
管线（若 `mode.stela`）→ `max_tokens=1` 发上游 → 只取 usage。

- **为什么 `max_tokens=1`**：只测 prefill / 缓存计费，输出生成被刻意阉割。
- **缓存隔离**：默认给每个 mode 在 system 段最前注入唯一前缀
  `[stela-replay ns=<session>/<mode>]`，让 Anthropic 端缓存各自独立，
  避免先重放的 mode 把缓存暖好被后者白蹭。
- 结果 append 到 usage_log，`compare_group` = 原会话 id，`replay: true`。

CLI：`stela replay --list` / `stela replay --session <id> --modes ...`。
原理与边界详见 [replay-comparison.md](replay-comparison.md)。

### 11.3 replay vs 双 session

| | 成本 | 控制变量 | 适合论断 |
|---|---|---|---|
| replay | 1 次真实会话 + 廉价 prefill | 好（轮次钉死）| 「对给定工作负载，token 账单降 X」|
| 双 session | N×K 个完整会话 | 差（trajectory 分叉）| 「用了 STELA，agent 整体更便宜」|

---

## 12. 可观测性

### 12.1 usage_log

proxy 和 SDK transport 共有。每次调用一行 jsonl。关键字段：`session_id`
/ `call_index` / `harness` / `mode` / `compare_group` / `tool_output_reduction`
/ `normalized`（4 字段）/ `raw_usage` / `cumulative`（`cache_creation` /
`real_requests_since_refresh` / `refpool_slugs`）。

### 12.2 省钱看板（[build_savings_dashboard.py](../scripts/build_savings_dashboard.py)）

`stela dashboard` 或 proxy 内嵌 `/__stela/dashboard`。把 usage_log 聚合成
"省了多少 token / 多少美刀"。含 2026 价格表（含 cache_write 5m/1h 拆分）。
本批新增：**Breakdown by mode** 表 + **A/B 对比** 面板（同 `compare_group`
下不同 mode 并排，replay 组标 `replay` 徽章、双 session 标 `live A/B`）+
**RTK tool output removed** KPI。

### 12.3 开发者页面（[build_developer_page.py](../scripts/build_developer_page.py)）

proxy 内嵌 `/__stela/developer`。渲染**当前内存里**所有 session 的 IR
结构、prompt 区域 PIN/FOLD/DROP 字符分布、最近调用表、逐 message band
视图、工具调用统计。数据源是 `proxy/inspector.py` 的 `SessionInspector`
（OrderedDict LRU，每 session 留最近 `INSPECTOR_HISTORY=25` 次调用）。

### 12.4 prompt_trace + show_prompt_trace

SDK transport 额外写 `prompt_trace_log`（IR layout 快照、plan 细节、跨
call 的 prefix 重合度）。`scripts/show_prompt_trace.py` 在终端美化打印。

---

## 13. 不变量与设计约束

| 编号 | 约束 | 落地 |
|---|---|---|
| §5 | 每段内 `pin* → fold* → drop*` | `assert_band_order`，emit 前后各校验一次 |
| I3 | ref-pool slug 一经注册即冻结 | `RefPool.register` 重复注册抛错 |
| §4 | `[ref:slug]` 引用必须能在 ref-pool 找到 | `lint_blocks` emit 前 fail-fast |
| R2 | 长对话需要 mid-rolling 锚 | Anthropic `BP-mid`（messages ≥ 19）|
| R5 | 跨语言 JSON key 乱序破坏 cache | `_canonicalize_*` 统一在 bridge 做 |
| R6 | thinking 块不能直接挂 cache_control | harness 分 FOLD，engine emit 不挂 |
| R7 | BP 超过 4 个要按优先级截断 | Anthropic `BP-T<BP-S<BP-R<BP-mid<BP-X` |
| R8 | 低活跃 session 续期亏本 | `refresh` 自适应门控，阈值 `REFRESH_THRESHOLD=11` |

---

## 14. 端到端数据流

以路径 B（proxy）+ `mode=both` 为例：

```
1. agent 发 POST /v1/messages 到 proxy
2. proxy 派生 session_id，取 BridgeSessionState
3. [录制] record_call 把原始请求写进 corpus
4. 解析 mode（header > sticky > 进程默认）+ compare_group
5. [RTK] mode.rtk → apply_filter 缩短 tool_result
6. [STELA] mode.stela → process_anthropic_request:
     a. _detect_harness → 选 harness（sticky）
     b. harness.parse → StelaIR
     c. Bridge(ir, engine, session_state).emit_with_plan():
        - _canonicalize_ir（key 排序、工具排序）
        - assert_ir_invariants（§5 校验）
        - refpool.lint_blocks（引用校验）
        - engine.plan_marks → EmitPlan（BP 锚位）
        - engine.emit → wire（挂 cache_control）
     d. 透传非 STELA 字段
7. proxy 转发 wire 到真实 Anthropic
8. 收到响应：旁路解析 usage
9. bridge 累积 cache_creation；写 usage_log + inspector
10. 响应原样回 agent
```

---

## 15. 扩展点

| 想做的事 | 改哪 |
|---|---|
| 新增 agent installer | [init/](../init/) 加 `<name>.py` 实现 `AgentInstaller`，注册到 `init.INSTALLERS` |
| 新增 harness | [harness/](../harness/) 加 plugin，注册到 [registry.py](../registry.py) |
| 新增 engine adapter | [engine/](../engine/) 加 `EngineAdapter` / `BidirectionalEngineAdapter` 子类，注册到 registry |
| 新增工具过滤规则 | [output_filter/filters.py](../output_filter/filters.py) 的 `FallbackFilter`，或让 `RtkFilter` 走 rtk 二进制 |
| 加 `/v1/chat/completions` 代理路径 | [proxy/server.py](../proxy/server.py) 加 route，复用 OpenAI 同款管线 |
| 持久化 session state | `BridgeSessionState` 是普通 dataclass，序列化成 JSON；改 `_SessionRegistry` 走外部存储 |
| 调整 canonical 排序 | bridge 的 `_SCHEMA_SET_ARRAY_KEYS`、`_TOOL_SOURCE_RANK` 都是模块级名字，可 monkey-patch |
