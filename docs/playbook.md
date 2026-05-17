# TELOS Playbook —— 从理念到最佳实践

> 用户侧操作手册。读完你能回答三个问题：TELOS 为什么存在、怎么把它接进
> 你的 agent、接进去之后怎么用好它。
>
> - 想要逐项 CLI 参考 → [User-guide.md](User-guide.md)
> - 想懂代码架构 → [ARCHITECTURE.md](ARCHITECTURE.md)
> - 想懂协议规范 → [2026-05-06-telos-protocol.md](2026-05-06-telos-protocol.md)
>
> 最后更新：2026-05-16

---

## 第一部分 · 理念

### 1.1 钱花在哪了

一个跑多轮的编码 agent，每轮请求都把 system prompt + 工具定义 + 整段
对话历史重新发给模型。第 20 轮的请求里，95% 的内容和第 19 轮一字不差。

LLM 推理引擎的 **KV cache** 本可以把这些重复前缀的计算结果留住，命中时
input token 只按 ~10% 计价（Anthropic）。但 cache 命中有一个苛刻前提：
**前缀必须逐字节稳定**。而 agent 的请求默认做不到 ——

- JSON 序列化在不同语言里 key 顺序会变；
- 工具数组的顺序随 MCP server 启动竞态抖动；
- 时间戳、cwd、git status 这类每轮都变的内容混在前缀里；
- 历史对话里某个 tool_result 被改写，后面全部位移。

任何一个抖动，前缀 hash 变，cache 整段失效，这一轮按全价计费。

### 1.2 TELOS 做的唯一一件事

**把真正稳定的部分稳住，让它持续命中 KV cache。**

TELOS 不是一个"更聪明的 prompt 框架"。它只做一件事：识别请求里哪些是
"石碑底座"（一刻一辈子的稳定前缀），哪些是"可擦改的题字"（每轮新增），
然后保证底座的字节绝不因为可避免的原因而抖动。

### 1.3 心智模型：石碑

TELOS = **S**table prefix · **T**iered bands · **E**phemeral tail ·
**L**ayered adapters · **A**nchored marks。取"石碑"之意：

- 石碑**底座的铭文**（durable prefix）—— 刻一次用一辈子。
- 上方**按时间累加的题字**（每轮 user/assistant 内容）—— 随时可擦改，
  但不会动到底座。

### 1.4 三色带 —— TELOS 给每段内容贴的标签

| 带 | 含义 | 典型内容 |
|---|---|---|
| **PIN** | 长寿稳定段，要进 cache、要稳 | 工具定义、system prompt、用户当下的提问 |
| **FOLD** | 可缓存但必要时可丢弃/折叠 | assistant 历史回答、tool_result、大文档 |
| **DROP** | 永远不进 cache hash | 时间戳、cwd、git status、`<system-reminder>` envelope |

唯一的硬规则：每段内容里，三色带必须物理排成 `PIN → FOLD → DROP`。把
DROP（每轮都变的东西）赶到最后，前面的 PIN+FOLD 前缀才稳得住。

### 1.5 两条正交的优化线

TELOS 稳的是**请求前缀**。但 agent 每轮还会往对话尾巴追加大段工具输出
（bash / pytest / docker 日志，动辄几千 token）。这部分 TELOS 管不到。

所以有第二条线 —— **RTK 输出过滤**（吸收 [rtk-ai/rtk](https://github.com/rtk-ai/rtk)
的思路）：在请求进 TELOS 之前，把 `tool_result` 里的大段重复输出压掉。

两条线互相独立，由一个四态开关控制：

| 开关 | TELOS 前缀缓存 | RTK 工具过滤 |
|---|:---:|:---:|
| `none` | ✗ | ✗ |
| `telos` | ✓ | ✗ |
| `rtk` | ✗ | ✓ |
| `both` | ✓ | ✓ |

> 不开 RTK：前缀 cache 命中再高，每轮的工具输出仍线性撑大对话。
> 不开 TELOS：工具输出缩了，但稳定前缀仍每轮重算。两条线合起来收益最大。

---

## 第二部分 · 安装

```bash
cd /path/to/telos-sdk
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

验证：

```bash
python -c "import telos; print(telos.__file__)"   # .../telos-sdk/__init__.py
telos --help                                       # proxy / init / dashboard / replay
```

要求 Python ≥ 3.10；依赖 `anthropic≥0.49`、`openai≥1.72`、`aiohttp≥3.10`。
RTK 过滤想用真 rtk 引擎需另装 `rtk` 二进制（没装会自动退回纯 Python
fallback 过滤器，开关仍生效）。

---

## 第三部分 · 选接入路径

```
你能改 agent 的源码 / import 站点吗？
│
├─ 不能（npm 全局装的 Claude Code、闭源二进制、共享主机多 agent）
│     → 路径 B · HTTP 反向代理      ← 推荐，零侵入
│
└─ 能（自研 Python agent、vendored 的 mini_swe_runner 这类）
      → 路径 A · SDK Transport      ← 完整 typed 响应、与进程同生命周期
```

两条路径**功能等价**（同一 TELOS 管线、同一状态累积），区别只在进程
边界 / 错误处理 / 流式。**没有特殊理由就选路径 B。**

---

## 第四部分 · 接入流程

### 4.1 路径 B —— Claude Code（最常见，三步）

```bash
# ① 起代理（默认 mode=telos，默认录会话到 ~/.telos/corpus）
telos proxy --usage-log ~/.telos/usage.jsonl

# ② 一行接入 Claude Code（patch ~/.claude/settings.json 的 env 字段）
telos init --agent claude-code

# ③ 正常用 claude —— 流量自动经过代理
claude
```

撤销 / 查状态：

```bash
telos init --agent claude-code --uninstall   # 精确还原 install 前状态
telos init --agent claude-code --status
```

`init` 不改 npm 包、不改 PATH，`npm update` 也不会丢配置。

### 4.2 路径 B —— 其它 Anthropic-SDK 客户端

```bash
telos init --agent generic    # 打印 export 指令，自己加到 shell rc / Dockerfile / k8s env
# export ANTHROPIC_BASE_URL=http://127.0.0.1:7171
```

适用于 Cursor、Gemini CLI、自研 Node/Python agent —— 任何尊重
`ANTHROPIC_BASE_URL` 的客户端。

### 4.3 路径 A —— SDK Transport（改代码）

把 `anthropic.Anthropic()` 换成 `TelosAnthropicTransport`，`.messages.create()`
调用一字不改：

```python
from telos.scripts.telos_anthropic_transport import TelosAnthropicTransport
client = TelosAnthropicTransport(
    session_id="my-agent-session",        # 同一对话用同一 id
    usage_log="logs/usage.jsonl",
)
response = client.messages.create(model="claude-opus-4-7", max_tokens=8192,
                                   system=[...], tools=[...], messages=[...])
```

OpenAI 形状的 agent 用 `TelosOpenAITransport`（`.chat.completions.create`）。
详细构造参数见 [User-guide.md §3](User-guide.md)。

---

## 第五部分 · 开关与对比实验

### 5.1 设置 mode

```bash
# 进程级默认
telos proxy --mode both

# 单条请求覆盖（首请求的取值会 sticky 到该 session）
curl ... -H 'X-Telos-Mode: rtk'
```

四态：`none` / `telos` / `rtk` / `both`，含义见 §1.5。

> **默认建议**：先用 `telos`（稳妥，不改工具结果字节），观察一段时间确认
> 无异常，再切 `both`。RTK 会改写 `tool_result`，先验证你的 agent 不依赖
> 工具输出的逐字节原文。

### 5.2 对比实验 —— 哪种优化值得开？

TELOS 提供两种受控对照，回答"开 TELOS / RTK 到底省多少"。

**方式一 · replay（推荐，受控、便宜）**

录一个真实会话，按多种 mode 各重放一遍逐字节相同的轮次：

```bash
telos replay --list                              # 看语料库里有哪些会话
telos replay --session <id>                       # 默认 4 mode 全跑
telos dashboard --usage-log ~/.telos/usage.jsonl  # A/B 对比面板看结果
```

每个 mode 看到的输入完全一致，唯一变量是开关 —— 受控实验，数字干净。
成本低（1 次真实会话 + 每 mode 一串廉价 `max_tokens=1` prefill）。

**方式二 · 双 session（端到端，有噪声）**

起两个独立 agent 会话、用户输入相同，各带不同 `X-Telos-Mode` + 相同
`X-Telos-Compare-Group`，dashboard 同一面板并排。能测端到端任务成本，
但 trajectory 会分叉，单次跑的 delta 不可信。

> 何时用哪个：日常对照、回归基准 → replay；偶尔做端到端校验 → 双 session。
> 原理与边界详见 [replay-comparison.md](replay-comparison.md)。

---

## 第六部分 · 最佳实践（DO）

1. **同一对话用同一 `session_id`**。多轮 cache 累积全靠它。路径 A 传
   `session_id=`，路径 B 靠内容派生（一般自动正确）或显式
   `x-telos-session` header。
2. **先 `telos` 后 `both`**。先验证 TELOS 前缀缓存稳定无异常，再叠加会
   改写工具结果的 RTK。
3. **接入后第一件事是看 dashboard**。`/__telos/dashboard` 或
   `telos dashboard`，确认 `cache_read` 在涨、`cache hit%` 合理。
4. **用 replay 决定要不要全量开某个 mode**。别凭感觉，跑一次 replay 看
   A/B 面板的实测数字。
5. **让代理一直录会话**（默认开启）。语料库是 replay 的燃料，也是回归
   基准。介意原始 prompt 落盘才用 `--no-record`。
6. **长跑 / 高并发场景调 `max_sessions`**。代理 LRU 默认上限 10000。
7. **生产用非 strict（默认）**。TELOS 失败自动降级 passthrough，正确性
   永不受影响；`--strict` 只在 dev 调试时用。

---

## 第七部分 · 反模式（DON'T）

| 别这样 | 为什么 | 改成 |
|---|---|---|
| SDK transport 路径用 `stream=True` | 流式没接 TELOS 处理，直接透传 | 路径 A 用非流式；要流式走路径 B（代理完整支持 SSE）|
| 每轮换 `session_id` | cache 累积归零，`cache_creation` 永远 0 | 整段对话固定一个 id |
| 把每轮变化的内容（时间戳/cwd）塞进 system prompt 头部 | 污染 PIN 前缀，cache 整段失效 | 它们会被 harness 归到 DROP；别手动前置 |
| 指望 RTK 改 agent 的本地上下文 | RTK 只过滤 proxy→上游这一段，agent 本地副本不变 | 这是设计如此；省的是计费 token |
| 凭单次双 session 跑分下结论 | trajectory 分叉，delta 是噪声 | 用 replay，或双 session 多跑取平均 |
| 把 replay 数字当端到端任务成本 | replay 把轨迹钉死、`max_tokens=1` 不计 output | replay 测的是 prefill/缓存计费；端到端用双 session |
| 自定义 header 指望透传 | 代理只白名单转发 6 个 header | 改 `_FORWARD_HEADER_WHITELIST`，或走路径 A |

---

## 第八部分 · 观测与判断健康

### 8.1 三个看板

| 看板 | 入口 | 看什么 |
|---|---|---|
| 省钱看板 | `/__telos/dashboard` 或 `telos dashboard` | 省了多少 token / 美刀、A/B 对比、mode breakdown |
| 开发者页面 | `/__telos/developer` | 当前内存里每个 session 的 IR 结构、PIN/FOLD/DROP 分布、工具统计 |
| usage_log | `~/.telos/usage.jsonl` | 逐调用的原始数据 |

### 8.2 健康信号

```bash
# 多轮 cache_read 是否在涨（命中在工作）
jq -c '{call:.call_index, cache_read:.normalized.cache_read, cum:.cumulative.cache_creation}' \
    < ~/.telos/usage.jsonl

# ref-pool 是否稳定（同一文档不应反复重新注册）
jq -c '.cumulative.refpool_slugs' < ~/.telos/usage.jsonl | sort -u

# 有没有降级到 passthrough（TELOS 出错的信号）
jq -c 'select(.harness == "passthrough")' < ~/.telos/usage.jsonl
```

**健康**：`cache_read` 随轮次上升、`cache_creation` 单调递增、`refpool_slugs`
不反复增长、没有 `passthrough` 记录。

---

## 第九部分 · 故障排查速查

| 现象 | 根因 | 修法 |
|---|---|---|
| `cache_read` 永远 0 | session_id 每轮在变 / 模型不支持 prompt caching / `cache_control` 没生效 | 固定 session_id；确认模型支持；看 dashboard 的 hit% |
| `cumulative.cache_creation` 永远 0 | 没传 `session_state`（路径 A）或代理重启过 | 路径 A 显式传 `session_state`；路径 B 别频繁重启 |
| 看到 `passthrough` 记录 | TELOS 管线抛异常、自动降级 | 看代理日志首次 traceback；dev 阶段加 `--strict` 让它显式爆 |
| `TelosInvariantError: Band order violated` | harness 输出违反 §5 | 这是 TELOS-side bug；扩展新 harness 时 message 末尾过一遍 `enforce_band_order` |
| RTK 没省下 token | 工具输出短于 600 字符阈值 / 没有重复 | 正常；小输出本就不值得过滤 |
| `rtk` mode 但 dashboard 显示 `fallback:*` rule | `rtk` 二进制没装 | 装 rtk 二进制，或接受 Python fallback |
| 自定义 header 丢失 | 代理只白名单转发 6 个 header | 改 `_FORWARD_HEADER_WHITELIST` 或走路径 A |
| replay 报缺 API key | 没设 `ANTHROPIC_API_KEY` | `export ANTHROPIC_API_KEY=...` 或 `--api-key` |

---

## 第十部分 · 推荐上手顺序

1. 读本文第一部分，建立"石碑 + 三色带 + 两条优化线"的心智模型。
2. `pip install -e .`，`telos proxy` 起代理，`telos init --agent claude-code`。
3. 正常用几天 agent，让语料库自然积累。
4. `telos dashboard` 看省钱看板，确认 cache 在命中。
5. `telos replay --session <id>` 对一个真实会话做 4-mode 对照，看 A/B 面板
   决定要不要全量切 `both`。
6. 想深入 → [ARCHITECTURE.md](ARCHITECTURE.md)（代码架构）、
   [replay-comparison.md](replay-comparison.md)（对照原理）。
