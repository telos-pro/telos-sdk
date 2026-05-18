<div align="center">

<img src="branding/logo.svg" alt="TELOS — 可移植的 Agent 上下文" width="420"/>

### 一个可移植、cache-友好的 LLM agent 上下文协议。

<sub>一份规范 IR —— 你的 tools、system、对话轮次与记忆 —— 在 Anthropic、OpenAI、DeepSeek、vLLM、SGLang 上原样运行;跨轮保持 KV-cache 命中,记忆按需取用,成本以绝对美元计。</sub>

<br/>

[![Core](https://img.shields.io/badge/core-Apache%202.0-2C5F66?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-4FB3BF?style=flat-square)](pyproject.toml)
[![Status](https://img.shields.io/badge/status-Beta-d8851f?style=flat-square)](CHANGELOG.md)
[![Protocol](https://img.shields.io/badge/protocol-TELOS%20IR-7FD8E0?style=flat-square)](docs/2026-05-06-telos-protocol.md)

[**快速上手**](#-30-秒上手) · [**引擎**](#-引擎一份-ir五个后端) · [**为什么**](#-为什么是-telos别再做别人-agent-里的租客) · [**三件事**](#-一个表示三件事) · [**协议**](docs/2026-05-06-telos-protocol.md) · [**User Guide**](docs/User-guide.md)

<sub>📖 &nbsp;[English](README.md) · **简体中文**</sub>

</div>

---

## ⬢ &nbsp;30 秒上手

```python
from telos import Bridge, load_engine, load_harness

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

完整端到端见 [`telos/demo.py`](demo.py):`python -m telos.demo`。

---

## ⬢ &nbsp;长这样

<div align="center">

<img src="branding/dashboard.png" alt="TELOS 节省看板 —— 按 harness、model、session 拆分的绝对美元节省" width="820"/>

<sub>每次 call 的 normalized usage 落进 jsonl,聚合成单文件 HTML 看板。<br/>它算的是<strong>省下的绝对美元</strong> —— 不是靠缩小分母就能作弊的比例。</sub>

</div>

---

## ⬢ &nbsp;引擎:一份 IR,五个后端

TELOS 的姿态是规范性的:它定义上下文应如何表示,引擎按能力对齐。同一份 `TelosIR` 落到不同引擎,由 adapter 做**确定性降级** —— 不悄悄丢、不丢语义。

| 能力 | Anthropic 4.6+ | OpenAI 4+/5.x | DeepSeek V3+ | vLLM | SGLang |
|---|:---:|:---:|:---:|:---:|:---:|
| 显式 BP / 锚位 | ✓(≤4) | ✗ | ✗ | ✓ | ✓ |
| 显式 prewarm | ✓ | ✗ | ✗ | ✓ | ✓ |
| 路由 key | ✗ | `prompt_cache_key` | ✗ | `cache_salt` | `affinity_key` |
| 缓存查询 / 段淘汰 | ✗ | ✗ | ✗ | ✓ | ✓ |
| fork-and-replace | ✗ | ✗ | ✗ | 部分 | ✓ |

> **双向能力**(`BidirectionalEngineAdapter`,只在开源推理引擎上实现):`cooperative_fold()` 让 server 保留前缀 KV 不动、只重算摘要尾段 —— 闭源 API 的 `fold` 是客户端 rewrite,每次都要 server 重新 prefill 整段,这是它做不到的。完整对照见[协议 §6](docs/2026-05-06-telos-protocol.md)。

---

## ⬢ &nbsp;为什么是 TELOS:别再做别人 agent 里的租客

<p align="center">
  <em>把 agent 接进生产时,所有人都撞上同样的四堵墙。<br/>
  TELOS 是这四堵墙的同一个答案 —— 一份上下文的规范表示。</em>
</p>

<table>
<tr>
<td width="50%" valign="top">

### 🔒 &nbsp;上下文不再锁死在一家

> *"换个模型跑同一个任务,就得从头再来。"*

`TelosIR` 是一份**引擎无关、可序列化、能带走**的上下文表示。Anthropic 的会话原样搬到 DeepSeek、搬到你自己的 vLLM —— 由 adapter 做确定性降级,不丢语义。

<sub>📁 [`telos/ir.py`](ir.py) · [`telos/engine/`](engine/)</sub>

</td>
<td width="50%" valign="top">

### 💸 &nbsp;不再为同一段开头重复付费

> *"跑了二十轮,每轮都在为相同的前缀重新 prefill。"*

三色带 **PIN · FOLD · DROP** + 一条顺序不变量,把"底座"留在 KV cache 里。记忆按需取用,而非每轮全量塞进 prompt。

<sub>📁 [`telos/bridge.py`](bridge.py) · [`telos/refpool.py`](refpool.py)</sub>

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🧾 &nbsp;成本看得见,以绝对 $ 计

> *"只拿得到一个被分母稀释过的比例。"*

每次 call 的 normalized usage 落进 jsonl,聚合成单文件 HTML 看板。算的是**绝对量**:cache_read、cost saved —— 比例能靠缩小分母作弊,绝对 $ 不能。

<sub>📁 [`scripts/build_savings_dashboard.py`](scripts/build_savings_dashboard.py)</sub>

</td>
<td width="50%" valign="top">

### 🎛 &nbsp;控制器在你手上

> *"想把任务交给更擅长它的 agent,做不到。"*

上下文能带走,你才真正握着控制器:一个任务跨 harness 分发,集众家所长。**TELOS 给机制,绝不给政策** —— 永不替你决定、永不为路由烧一次 LLM 调用。

<sub>📁 [`telos/harness/`](harness/)</sub>

</td>
</tr>
</table>

<p align="center">
  <strong>一句话:</strong> TELOS 是 agent 栈里唯一耐久的资产 —— 上下文 —— 的规范表示。<br/>
  上下文归你,harness 只是雇来的。
</p>

---

## ⬢ &nbsp;一个表示,三件事

**TELOS** —— 希腊语 τέλος,"目的、归宿";也取"石碑"之意。石碑底座的铭文刻一次、用一辈子;上方逐轮题字随时可擦,但动不到底座。而石碑还有第二层意思 —— **石碑是你的**:它可以搬到任何刻字的工匠(harness)、任何拓印作坊(engine)面前。

```
③ 主权     你握着控制器 —— 任何任务,雇任何 harness、任何模型,不必住进谁的笼子
                 ▲  唯一能实现它的
① 可移植   上下文 / 记忆是一份引擎无关、可序列化、能带走的资产
                 ▼  同一份表示顺带兑现的
② 效率     极致 KV cache 命中 + 按需记忆;成本以绝对 $ 计,看得见
```

> **③ 是目的,① 是机制,② 是回报也是楔子。** 三者不是并列,是一个栈。铁律:TELOS 提供机制,绝不提供政策 —— 一旦它替你决定,控制器就被它拿回去了。

---

## ⬢ &nbsp;架构

```
agent harness ──► TELOS Bridge ──► engine adapter ──► LLM 服务
   (parse)          (5 原语)         (capability-aware)
```

| 层 | 文件 | 职责 |
|---|---|---|
| harness | [`harness/openclaw.py`](harness/) `hermes.py` | envelope 切分、大文档进 ref-pool、生成 `TelosIR` |
| bridge | [`bridge.py`](bridge.py) [`ir.py`](ir.py) [`refpool.py`](refpool.py) | 5 原语、不变量校验、ref-pool 冻结 slug、canonicalize |
| engine | [`engine/anthropic.py`](engine/) `openai.py` `deepseek.py` | capability-aware Mark、wire 序列化、usage 解析 |

bridge 是纯 Python,不依赖任何 LLM SDK。`TelosIR` 是三层之间唯一通过的数据结构 —— frozen 不可变、字段窄、引擎无关。

---

## ⬢ &nbsp;一个不变量

整个协议只有一条硬约束。每个段(`tools` / `system` / 单条 `message`)内,blocks 必须按物理顺序排列:

```
PIN*  →  FOLD*  →  DROP*
```

<sub>（`message` 段里 `tool_result` 块一律居首,Anthropic 协议要求。）</sub>

| 带 | 含义 | 典型内容 |
|---|---|---|
| **PIN** | 长寿稳定段 | tools 定义、system prompt、用户当下提问 |
| **FOLD** | 可缓存但 compact 时可丢弃 | assistant 回答、tool_result、ref-pool 大文档 |
| **DROP** | 永不进 cache hash | timestamp、cwd、git status、envelope |

违反就抛 `TelosInvariantError`。其余一切都是软建议。

### 五个原语 &nbsp;<sub>(`Bridge` 方法)</sub>

| 原语 | 作用 |
|---|---|
| `place(segment, blocks)` | 把 block 放进 tools / system / 当前 message |
| `pin(slug, payload)` | 在 system 段写一个 PIN 块 |
| `mark()` | 让 engine 给出本轮的 BP / routing-key 计划 |
| `fold(slugs= / message_range=, summary=)` | 把旧轮折叠成 ref-pool 引用 |
| `refresh(plan)` | 满足节流后发 `max_tokens=0` prewarm(仅 Anthropic) |

### ref-pool —— 上下文的"指针表"

slug 一旦 `register()` 就**冻结**:内容可变(`fold()`),slug 不能变。`fold()` 换 payload 不换 slug → 所有 `[ref:slug]` 引用点的字节不变 → 折叠后 BP 仍命中。这是"上下文可移植"在协议里的落地:**指针稳定,内容可流动。**

---

## ⬢ &nbsp;看得见的成本 · savings dashboard

任何 TELOS 入口(proxy / SDK transport)都会把每次 call 的 normalized usage 追加进 `usage_log` jsonl,聚合成单文件 HTML(零 JS、离线可开):

```bash
telos dashboard --usage-log ~/.telos/usage.jsonl --out savings.html

# proxy 内嵌的自动刷新看板
telos proxy --port 7171 --usage-log ~/.telos/usage.jsonl
open http://127.0.0.1:7171/__telos/dashboard
```

看板算的是**绝对量**:累计 cache_read、cost saved = cache_read ×(input_price − cache_read_price)、token mix、按 harness / model / session 三维拆分。

---

## ⬢ &nbsp;TELOS 不做什么

| | 战略非目标 —— 强愿景必须敢说"永不做" |
|---|---|
| ❌ | 永不跑 agent 的推理循环 —— TELOS 碰上下文,不碰 planning / tool execution |
| ❌ | 永不变成 orchestration 框架 |
| ❌ | 路由不是产品,是"拥有可移植上下文"的演示;给机制,不给政策 |

| | 战术非目标 |
|---|---|
| ❌ | 不做 token 计数(让 engine 自己回 usage) |
| ❌ | 不做 retry / backoff(属于 HTTP 客户端) |
| ❌ | 不做 KV-cache 物理实现(服务侧的事;TELOS 只决定喂什么、按什么顺序喂) |
| ❌ | 不做 streaming SSE 解析(`absorb_usage` 接受最终 response object) |

---

## ⬢ &nbsp;附录:R1–R8 协议隐患修复

review 阶段发现协议设计有 8 个隐患,Python 实现已全部修掉:

| 编号 | 问题 | 修复位置 |
|---|---|---|
| R1 | OpenAI `prompt_cache_key` 单 key ≥15 RPM 才扩槽位 | `engine/openai.py :: KEY_RPM_SOFT_CAP = 12` + `shard()` |
| R2 | Anthropic 4 BP 只覆盖 head + tail,中间轮落空 | `engine/anthropic.py :: _MID_ANCHOR_STRIDE = 19` |
| R3 | 子 agent IR 与父 IR 的 session_id 混用 | `harness/hermes.py` 子 IR 独立 parse |
| R4 | `fold()` 后 Mark slot 落在已折叠区 | `bridge.py :: fold()`,需重调 `mark()` |
| R5 | tool 字段 / 数组顺序 canonicalize 未稳 | `bridge.py :: _canonicalize_ir()` |
| R6 | thinking 块跨非 tool_result 调用失效 | `engine/base.py :: thinking_preserved_across_non_tool_result` |
| R7 | Anthropic BP 候选 > 4 时无显式优先级 | `engine/anthropic.py :: plan_marks` 优先级 + 截断 |
| R8 | refresh 频率无节流,可能反向打满 quota | `bridge.py :: REFRESH_THRESHOLD = 11` 自适应门控 |

---

## ⬢ &nbsp;深入

| 想做什么 | 去哪 |
|---|---|
| 直接上手(安装、接入、CLI、故障排查) | [`docs/User-guide.md`](docs/User-guide.md) |
| 理解协议 | [`docs/2026-05-06-telos-protocol.md`](docs/2026-05-06-telos-protocol.md) |
| 看架构 | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| 看改动史 | [`CHANGELOG.md`](CHANGELOG.md) |

---

## ⬢ &nbsp;License

Apache-2.0 —— 协议核心永远开源。见 [LICENSE](LICENSE)。
