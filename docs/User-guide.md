# TELOS User Guide

> 端到端使用手册：从安装到接入 agent，到多轮观测和调优。
>
> 协议层面看 [`2026-05-06-telos-protocol.md`](2026-05-06-telos-protocol.md)；
> 改动历史看根目录 [`CHANGELOG.md`](../CHANGELOG.md)。

---

## 1. 决策树：你该用哪条接入路径？

```
你能修改 agent 的源码 / import 站点吗？
│
├─ 能（自研 Python agent / mini_swe_runner 这类 vendored 代码）
│      ↓
│   路径 A —— SDK Transport
│   import TelosAnthropicTransport / TelosOpenAITransport
│   优点：完整 typed 响应、与 agent 进程同生命周期、调试直接
│   缺点：每个 agent 要单独改 import；流式还没 wrap
│
└─ 不能（npm 全局装的 Claude Code、闭源二进制、共享主机多 agent）
       ↓
    路径 B —— HTTP 反向代理
    telos proxy 起本机 7171，agent 设 ANTHROPIC_BASE_URL=http://127.0.0.1:7171
    优点：零侵入、agent 升级不丢、多 agent 共享一份代理
    缺点：多一层进程、白名单外的 header 会被丢弃
```

两条路径**功能等价**：
- 同样的 TELOS 管线（`process_anthropic_request` / `bridge.emit_with_plan`）
- 同样的多轮状态累积（`BridgeSessionState`）
- 同样的 `cache_control` 注入 / canonical 排序
- 同样的 usage 累计字段在日志里

差异只在「进程边界 / 错误处理 / 流式」这些操作层面。

---

## 2. 安装

```bash
cd /Users/george/Code/telos-sdk
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

[`pyproject.toml`](../pyproject.toml) 把项目根目录映射成 `telos` 包。安装后：

```bash
python -c "import telos; print(telos.__file__)"
# .../telos-sdk/__init__.py

telos --help
# usage: telos <subcommand> [...]
```

需要 Python ≥ 3.10。依赖 `anthropic ≥ 0.49`、`openai ≥ 1.72`、`aiohttp ≥ 3.10`。

---

## 3. 路径 A：SDK Transport（代码内接入）

### 3.1 Anthropic 客户端 — Claude Code / Openclaw / Hermes / 自研 agent

把 `anthropic.Anthropic()` 换成 `TelosAnthropicTransport`，其余 `.messages.create()` 调用不变：

```python
# 改前
import anthropic
client = anthropic.Anthropic()

# 改后
from telos.scripts.telos_anthropic_transport import TelosAnthropicTransport
client = TelosAnthropicTransport(
    session_id="my-agent-session",       # 同一对话用同一 id
    usage_log="logs/usage.jsonl",
    prompt_trace_log="logs/trace.jsonl",
    # harness_name="hermes",             # 不填则自动检测
)

# 调用完全不变
response = client.messages.create(
    model="claude-opus-4-7",
    max_tokens=8192,
    system=[{"type": "text", "text": "You are an engineer."}],
    tools=[...],
    messages=[...],
)
print(response.content[0].text)
```

构造参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `api_key` | `$ANTHROPIC_API_KEY` | Anthropic API key |
| `base_url` | `None`（走 SDK 默认） | 调试时可指向本机代理 |
| `session_id` | `"telos-session"` | 同一对话保持同一个 id；多轮 cache 累积的 key |
| `harness_name` | `None`（auto-detect） | 强制 `"openclaw"` / `"hermes"` |
| `engine_name` | `"anthropic"` | 一般不动 |
| `usage_log` | `None` | jsonl 路径，每次调用追加一行（标准化 usage） |
| `prompt_trace_log` | `None` | jsonl 路径，记录 IR layout / plan / 累积状态等诊断 |
| `session_state` | `None`（内部 new） | 多个 transport 共享同一对话时显式传入 |

Harness 自动检测：system 含 `<system-reminder>` 或 `<command-message>`、或消息中有 `thinking` block → 选 `hermes`；否则 `openclaw`。

### 3.2 OpenAI 客户端 — telos / mini_swe_runner / 自研 OpenAI-shape agent

```python
# 改前
from openai import OpenAI
client = OpenAI(base_url="https://openrouter.ai/api/v1")

# 改后
from telos.scripts.telos_transport import TelosOpenAITransport
client = TelosOpenAITransport(
    base_url="https://openrouter.ai/api/v1",
    session_id="telos-session",
    usage_log="logs/usage.jsonl",
    engine_name="deepseek",              # 或 "openai"
    harness_name="telos",                # 固定
)

response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[...],
    tools=[...],
)
```

### 3.3 跨 transport 共享同一对话

如果一段对话由多个 transport 实例处理（例如 retry 后重建 client），把 `BridgeSessionState` 显式传进去：

```python
from telos.bridge import BridgeSessionState
from telos.scripts.telos_anthropic_transport import TelosAnthropicTransport

shared = BridgeSessionState()
t1 = TelosAnthropicTransport(session_id="conv-1", session_state=shared)
# ... t1 出错被销毁 ...
t2 = TelosAnthropicTransport(session_id="conv-1", session_state=shared)
# t2 看得到 t1 累积的 ref-pool 和 R8 计数
```

---

## 4. 路径 B：HTTP 反向代理（零侵入接入）

### 4.1 启动代理

```bash
telos proxy --port 7171 --usage-log ~/.telos/usage.jsonl
# 后台跑：
telos proxy --port 7171 --usage-log ~/.telos/usage.jsonl &
```

启动后输出：

```
TELOS proxy listening on http://127.0.0.1:7171 → https://api.anthropic.com
usage log → /Users/.../usage.jsonl
```

代理路径接受所有 Anthropic 协议路径；`/v1/messages` 经 TELOS 改写，其他原样 passthrough。

### 4.2 接入 Claude Code（一行命令）

```bash
telos init --agent claude-code
```

往 `~/.claude/settings.json` 的 `env` 字段写入：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:7171",
    "__telos_installed": true
  }
}
```

之后任何启动 Claude Code 的进程都会自动用本机代理。**不改 npm 包**，**不改 PATH**，**npm update 也不会丢**。

撤销：

```bash
telos init --agent claude-code --uninstall
# 还原任何 install 之前的 ANTHROPIC_BASE_URL（如有）
```

查状态：

```bash
telos init --agent claude-code --status
```

### 4.3 接入其它 Anthropic-SDK 客户端（generic）

```bash
telos init --agent generic
# 打印一段 export 指令，自己加到 shell rc / Dockerfile / k8s env
# export ANTHROPIC_BASE_URL=http://127.0.0.1:7171
```

适用于 Cursor、Gemini CLI、自研 Node/Python agent —— 任何尊重 `ANTHROPIC_BASE_URL` 的客户端。

### 4.4 完整 CLI 参考

```
telos proxy [options]
  --host HOST          监听地址（默认 127.0.0.1）
  --port PORT          监听端口（默认 7171）
  --upstream URL       真实 Anthropic API endpoint（默认 https://api.anthropic.com）
  --usage-log PATH     每次调用追加一行 jsonl
  --harness {openclaw,hermes,claude-code}
                       强制 harness（默认按内容自动检测）；claude-code 是 hermes 的别名
  --strict             TELOS 失败时返 500，而不是降级到 passthrough

telos init [options]
  --agent {claude-code,generic}    必填
  --proxy-url URL      代理 URL（默认 http://127.0.0.1:7171）
  --uninstall          还原 install 之前的状态
  --status             只查看，不改文件
```

---

## 5. 多轮状态累积（关键能力）

TELOS 协议设计文档第 §4 / §6 提到的 ref-pool 持久化、R8 自适应 refresh 都依赖**跨 turn 的状态累积**。本节解释机制和如何观测。

### 5.1 状态都在哪

```python
@dataclass
class BridgeSessionState:
    refpool: RefPool          # ref-pool slug 注册表（冻结后跨轮保持，fold 也保持）
    stats: _SessionStats      # cumulative_cache_creation + real_requests_since_refresh
```

### 5.2 在路径 A 自动持有

`TelosAnthropicTransport` / `TelosOpenAITransport` 实例 = 一个 session。`__init__` 内部创建 `BridgeSessionState`，每次 `_do_create` 传给 `Bridge`，response 回来时 `bridge.absorb_usage(...)` 累加 cache_creation。

访问：`transport.session_state.stats.cumulative_cache_creation`。

### 5.3 在路径 B 按 session-id keyed 自动持有

代理内部 `_SessionRegistry`（OrderedDict LRU，默认 10000）按 session_id 持有 state。session_id 派生优先级：

1. `x-telos-session` HTTP header（显式覆盖）
2. `metadata.user_id`（Anthropic SDK 内建字段）
3. `blake2b(api_key + system + tools + messages[0])` → `telos-<16 hex>`

派生规则的语义：
- 同一对话的 N 轮（只在 `messages[]` 尾部追加）→ 同一 session_id ✓
- 不同初始 prompt（`messages[0]` 变）→ 不同 session_id ✓
- 不同 API key 的两个用户 → 不同 session_id ✓

LRU 上限超了之后最旧的 session 被驱逐，会打 INFO 日志。

### 5.4 观测累积

usage_log 每行新增 `cumulative` 块：

```json
{
  "session_id": "telos-46bbb9d3d3df581e",
  "call_index": 4,
  "harness": "openclaw",
  "normalized": {"raw_input": 50, "cache_read": 6500, "cache_write": 0, "output": 5},
  "cumulative": {
    "cache_creation": 6500,
    "real_requests_since_refresh": 4,
    "refpool_slugs": ["system-doc-1"]
  }
}
```

`cache_creation` 单调递增即说明累积工作；`refpool_slugs` 数组在多轮中不应反复增长（同一文档不应被重复注册）。

### 5.5 关闭累积（每轮独立 Bridge）

不传 `session_state`、或代理重启，行为退化为每轮新建 state。这是 1.0 之前的默认行为，不会破坏 wire 字节。

---

## 6. 故障排查

### 6.1 Proxy 返 500 / SDK 重试 10 次

老版本 TELOS 抛异常 → 代理返 500。现已**默认降级到 passthrough**：
- 代理日志首次失败：完整 traceback + `"falling back to passthrough"`
- 后续失败：WARNING 单行
- Wire 是 raw 透传（不带 cache_control 改写），但响应正常

要在 dev 阶段让 TELOS 失败立刻显式爆，启 `--strict`：

```bash
telos proxy --strict
```

### 6.2 `Band order violated`

如果你看到：

```
TelosInvariantError: Band order violated in messages[0]:
  block 'msg0/blk3/q' has band 'pin' after a higher-band block.
```

说明 harness 输出违反了 §5。**这是 TELOS-side 的 bug，不是你的请求问题。**

最常见原因：harness 不知道某种 content block 类型，或多 part 拼接没按 band 排。当前 openclaw / hermes 都已用 `enforce_band_order` 兜底；如果你扩展了新 harness，记得在 message 末尾过一遍 `enforce_band_order(blocks)`。

### 6.3 多轮 cache_creation 永远 0

如果 usage_log 里 `cumulative.cache_creation` 永远是 0，可能：

| 症状 | 检查 |
|---|---|
| 同一对话每次 session_id 都不同 | header 是不是漏了 `x-api-key`；`messages[0]` 是不是真不变 |
| `real_requests_since_refresh` 也永远 1 | 没传 `session_state`（路径 A）或代理重启过（路径 B） |
| `cache_read` 数字也是 0 | Anthropic 模型不支持 prompt caching、或 `cache_control` 没生效 |
| `refpool_slugs` 是空 | 没有大文档触发 ref-pool（默认 2KB 阈值）|

### 6.4 Header 没透传

代理只白名单转发：`x-api-key` / `authorization` / `anthropic-version` / `anthropic-beta` / `anthropic-dangerous-direct-browser-access` / `user-agent`。

其它 header 想透传，目前只能改 `_FORWARD_HEADER_WHITELIST`（[proxy/server.py](../proxy/server.py)）。SDK transport 路径不受此限制。

### 6.5 流式响应（Claude Code 默认开）

- 路径 A（SDK transport）：当前 `messages.create(stream=True)` 不做 TELOS 处理，直接调底层 SDK。**避免在 SDK transport 路径用流式**。
- 路径 B（proxy）：完整 SSE 支持，旁路解析 `message_start` / `message_delta` 抽 usage 字段。

---

## 7. 观测：两份日志的字段对照

### 7.1 `usage_log`（代理 + SDK transport 共有）

```jsonc
{
  "session_id": "telos-...",          // 跨轮稳定
  "call_index": 1,                     // 进程内递增
  "harness": "openclaw" | "hermes" | "telos" | "passthrough",
  "n_slots": 3,                        // EmitPlan 的 slot 数
  "slots": ["BP-T", "BP-S", "BP-X"],
  "latency_s": 1.234,
  "streaming": true | false,
  "status": 200,                       // upstream HTTP 状态
  "raw_usage": {...},                  // 原 wire usage 字段
  "normalized": {                      // 统一到 4 字段
    "raw_input": 50,
    "cache_read": 6500,
    "cache_write": 0,
    "output": 5
  },
  "cumulative": {
    "cache_creation": 6500,
    "real_requests_since_refresh": 4,
    "refpool_slugs": ["system-doc-1"]
  }
}
```

### 7.2 `prompt_trace_log`（仅 SDK transport）

包含 IR layout 快照、plan 细节、跨 call 的 prefix 重合度等诊断信息——粒度比 usage_log 重，用于 cache 行为深度分析。具体字段见 [scripts/telos_anthropic_transport.py](../scripts/telos_anthropic_transport.py)。

### 7.3 看日志的几个常用命令

```bash
# 查看每轮 cache_read 增量（验证多轮命中）
jq -c '{call: .call_index, cache_read: .normalized.cache_read, cum: .cumulative.cache_creation}' \
    < ~/.telos/usage.jsonl

# 查看 ref-pool 是否稳定（不应反复变化）
jq -c '.cumulative.refpool_slugs' < ~/.telos/usage.jsonl | sort -u

# 找所有降级到 passthrough 的请求
jq -c 'select(.harness == "passthrough")' < ~/.telos/usage.jsonl
```

---

## 8. 测试

完整测试矩阵：

```bash
for t in test_smoke test_harness_multiblock \
         test_proxy_pipeline test_proxy_server test_proxy_session_id \
         test_proxy_accumulation test_bridge_session_state \
         test_sdk_transport_accumulation test_init_claude_code; do
  python -m telos.tests.$t
done
```

每个套件单独可读，套件名映射看 [tests/](../tests/) 下的 docstring。

---

## 9. 已知局限

| 局限 | 解释 | 影响 |
|---|---|---|
| SDK transport 不 wrap `.stream()` | Anthropic SDK 的流式 context manager 没接 | 用 SDK transport 时避免 `stream=True` |
| 代理 header 白名单 | 只透传 6 个 header | 自定义 header 静默丢失 |
| 代理 LRU 上限默认 10000 | 长跑超过后驱逐旧 session | 大并发 / 长跑场景按需调 `max_sessions=` |
| 没有 OpenAI 反向代理 | 代理只听 `/v1/messages` | telos 类 OpenAI shape 只能走 SDK transport |
| `R8 refresh` 仅当 engine 支持 prewarm | 闭源 API 都 `prewarmable=False` | refresh 永远 no-op；只有 vLLM/SGLang 走得到 |
| 单进程代理 | 一个 aiohttp event loop | 想 scale 出去要前置一层 LB |

---

## 10. 扩展点

| 想做的事 | 改哪 |
|---|---|
| 新增 agent installer（Cursor / Gemini CLI / Hermes 本地版） | 在 [init/](../init/) 添一个 `<name>.py`，实现 `AgentInstaller` |
| 新增 harness | 在 [harness/](../harness/) 添一个 plugin，注册到 [registry.py](../registry.py) |
| 新增 engine adapter | 在 [engine/](../engine/) 添一个 `EngineAdapter` 子类 |
| 加 `/v1/chat/completions` 代理路径 | [proxy/server.py](../proxy/server.py) 加 route + 复用 `process_anthropic_request` 的 OpenAI 同款管线 |
| 持久化 session state 到 Redis / disk | `BridgeSessionState` 是普通 dataclass，序列化成 JSON 即可；改 `_SessionRegistry` 走外部存储 |
