# Token Efficient Framework — 架构梳理 & 上手指南

> 本仓库包含两个**相互独立但主题相关**的子项目：
>
> - **`agent-janus/`** — Agent ↔ 推理引擎之间的"缓存友好、协议感知"代理 / 桥接层（TypeScript Monorepo）。
> - **`benchmark/`** — 衡量编码 Agent（Claude Code / OpenClaw）token 利用率与 KV cache 命中率的基准框架（TypeScript + Python 混合）。
>
> 两者都围绕同一个核心问题：**如何让 Agent 把更多 token 命中 KV cache，少花钱、多解题**。`benchmark` 负责"测量"，`agent-janus` 负责"优化"。

---

## 第一部分 · 顶层架构

### 1.1 仓库整体地图

```
token-efficient-framework/
│
├── agent-janus/          ← 优化层：拦截 Agent → Engine 流量，保留/重组 cache 原语
│   ├── bridge/                    # 核心：IR、Session、Router、HTTP/SSE
│   └── plugins/
│       ├── harness/               # 解析 Agent 侧协议（Anthropic Messages、…）
│       └── engine/                # 调用引擎侧协议（Anthropic / vLLM / SGLang …）
│
├── benchmark/            ← 测量层：跑任务 + 算指标 + 评测 patch
│   ├── src/                       # TypeScript CLI（analyze / run-polyglot / run-swebench / report）
│   ├── scripts/                   # Python/bash runner（VM 调度、SWE-bench 全量、指标聚合）
│   ├── datasets/                  # polyglot / SWE-bench Verified（gitignored）
│   ├── results/                   # 每次 run 的会话 JSONL + sidecar
│   └── docker-eval/               # 官方 SWE-bench Docker 评测器封装
│
├── docs/                          # 设计/分析笔记
├── reviews/                       # 4 轮代码评审记录（修过的 bug 都在里面）
└── CLAUDE.md                      # 给 Claude 看的项目使命陈述（也是人类总览）
```

### 1.2 数据/控制流（端到端）

```
┌─────────────┐  ① 派发任务    ┌──────────────────────┐
│  benchmark/ │ ─────────────▶ │  Coding Agent         │
│  scripts/   │                │  (Claude Code / OC)   │
└─────────────┘                └──────────┬───────────┘
       ▲                                   │ ② Anthropic / OpenAI 调用
       │ ④ JSONL + patch                   │   （未来：经过 agent-janus 桥）
       │                                   ▼
       │                       ┌──────────────────────┐
       │                       │  Inference Engine     │
       │                       │  (Anthropic / vLLM /  │
       │                       │   SGLang / Codex)     │
       │                       └──────────┬───────────┘
       │                                   │ ③ 写 ~/.claude/  或  ~/.openclaw/
       │                                   ▼
       │                       ┌──────────────────────┐
       └──────────────────────│  Session JSONL + Patch │
                               └──────────┬───────────┘
                                          │ ⑤ Docker harness 评测
                                          ▼
                               ┌──────────────────────┐
                               │  compute-metrics.py  │
                               │  → 4 个北极星指标     │
                               └──────────────────────┘
```

> 当前 **agent-janus 尚未串入 benchmark 流水线**，二者目前是独立工件：benchmark 直接打 Anthropic/OpenAI；agent-janus 仍在 Phase 1（Anthropic 透传 + 单元/集成测试）。文档第 4 节给出衔接路径。

---

## 第二部分 · agent-janus 架构

### 2.1 三层切分（核心不变量）

```
Harness (CC / OpenClaw / Codex CLI / 自研 agent)
    │  HTTP / SSE / WS    ANTHROPIC_BASE_URL or OPENAI_BASE_URL
    ▼
┌──────────────────────────────────────────────────────────┐
│ HARNESS PLUGIN（无状态）                                  │
│   parseOperation:  wire 字节  → BridgeOperation IR        │
│   formatResponse:  BridgeResponse → wire 字节             │
└────────────────────────┬─────────────────────────────────┘
                         ▼  BridgeOperation
┏══════════════════════════════════════════════════════════┓
┃ CORE BRIDGE（唯一可持有跨请求状态的地方）                 ┃
┃   Router · Session · Capabilities · PluginRegistry        ┃
┃   Observability · Config · IR/Hash                        ┃
┃   （未来：PrefixNormalizer / FieldPreservation / 等策略） ┃
┗════════════════════════╤═════════════════════════════════┛
                         ▼  ResolvedRequest
┌──────────────────────────────────────────────────────────┐
│ ENGINE PLUGIN（无状态）                                    │
│   executeGenerate / executeCountTokens /                  │
│   executeCompact / applyCacheEdits?                        │
└────────────────────────┬─────────────────────────────────┘
                         ▼ 引擎原生 wire
Engine (Anthropic Cloud / vLLM / SGLang / OpenAI / Azure)
```

**核心不变量**：跨请求状态只能存在于 `CORE BRIDGE`。Plugin 只解协议，不持状态。

### 2.2 模块 → 目录 → 关键文件 对应表

| 层 | 模块 | 目录 | 关键文件 / 类型 | 职责 |
|---|---|---|---|---|
| 入口 | CLI | [agent-janus/bridge/src/server-main.ts](agent-janus/bridge/src/server-main.ts) | `main()` | 解析 `--port / --no-echo`，启 HTTP 服务，注册插件 |
| 入口 | 公共导出 | [agent-janus/bridge/src/index.ts](agent-janus/bridge/src/index.ts) | re-exports | 整库出口（IR / 协议 / Server） |
| Core | **IR** | `bridge/src/core/ir/` | `BridgeOperation`, `NormalizedMessage`, `ContentBlock`, `UpstreamHints`, `hash.ts`, `brand.ts` | 协议无关的内部表示；带品牌（branded）类型；`stableJson` + `sha256` 出 prefix hash |
| Core | **Session** | `bridge/src/core/session/` | `SessionStore`（接口）, `MemorySessionStore` | 跟踪 `(sessionId → state)`，含 TTL、可注入时钟。Phase 4 再加 Redis/SQLite |
| Core | **Capabilities** | `bridge/src/core/capabilities/` | `EngineCapabilities`, `validateCapabilities()` | 引擎自描述能力包；注册时做自洽校验 |
| Core | **PluginRegistry** | `bridge/src/core/plugin-registry/` | `HarnessPlugin`/`EnginePlugin` 接口、`InMemoryPluginRegistry` | 唯一名校验；容错查找（坏插件不影响其他） |
| Core | **Server / Router** | `bridge/src/core/server/` | `Router`, `startHttpServer`, `BridgeHttpError` | wire → harness → engine → harness → wire；echo mode；16 MB body cap；4xx/413 |
| Core | **Observability** | `bridge/src/core/observability/` | `Counter` / `Gauge` / `Histogram`, `AuditSink` | Prometheus 文本格式；标签排序规范化；审计：null/console/file/collecting |
| Core | **Config** | `bridge/src/core/config/` | `BridgeConfig`, `defaultEchoConfig()` | 配置 schema |
| Protocol | **Anthropic Messages** | `bridge/src/protocols/anthropic-messages/` | `wire-types.ts`, `parse.ts`, `format.ts` | 解 `/v1/messages` 请求 → IR；IR → 响应；保留 `cache_control` 位置、`tool_choice`、`metadata`、`thinking` |
| Plugin/Harness | Anthropic Messages | `plugins/harness/anthropic-messages/` | `AnthropicMessagesHarness` | 匹配 `POST /v1/messages` & `/v1/messages/count_tokens`；`x-session-id` → SessionId（缺省由 Router 生成 UUID）|
| Plugin/Engine | Anthropic 透传 | `plugins/engine/anthropic-passthrough/` | `AnthropicPassthroughEngine` | IR → Anthropic wire；AbortController 控超时；当前**拒绝 `stream:true`**（Phase 1.5） |
| Plugin/Engine | vLLM-native | `plugins/engine/vllm-native/` | `VllmNativeEngine` | 包装透传，目标 `/v1/messages`；只声明 anthropic-* 协议（避免误路由）|
| Plugin/Engine | SGLang-native | `plugins/engine/sglang-native/` | `SglangNativeEngine` | 同上 |
| 测试 | E2E | `bridge/src/e2e/roundtrip.test.ts` | 13 项端到端 | 起 mock upstream + bridge，覆盖 3 个 engine 变体、count_tokens、502、wire-shape 往返、session 隔离 |
| 测试 | Smoke | `agent-janus/scripts/smoke-anthropic-passthrough.mjs` | 实际 curl | 启动 mock upstream + bridge，curl 一发，校验 `cache_read_input_tokens` 透传 |

### 2.3 关键 IR：`BridgeOperation`

```typescript
type BridgeOperation =
  | { kind: "generate";        req: GenerateRequest }
  | { kind: "count_tokens";    req: CountTokensRequest }
  | { kind: "compact_history"; req: CompactHistoryRequest }
```

`GenerateRequest` 字段：

| 字段 | 含义 |
|---|---|
| `systemSegments[]` | 多段 system，每段可独立带 `cache_control` |
| `tools[]` | 工具定义；保留 source（`built-in` / `mcp`） |
| `messages[]` | NormalizedMessage 链；`ContentBlock` 含 text/tool_use/tool_result/thinking/image |
| `upstreamHints` | **协议原语保留袋**：所有引擎特定字段（`cache_control` / `prompt_cache_key` / `previous_response_id` / `x-codex-turn-state` …）都进这里，确保字节级回写 |
| `inferenceParams` | 通用：`maxTokens`/`temperature`/`stopSequences`/`stream` |

### 2.4 已实现的 Phase 路线图

| Phase | 内容 | 状态 |
|---|---|:---:|
| 0 | 脚手架、IR、HTTP echo、PluginRegistry | ✅ |
| 1 | Anthropic 透传 MVP（harness + 3 engine + count_tokens + audit/metrics） | ✅ |
| 1.1 | 7 项评审修正：`cache_control` 位置、`tool_choice`、`thinking.budget_tokens`、协议路由 | ✅ |
| 1.2 | 4 项后续修正：byte-stable wire shape、嵌套 `cache_control`、`BridgeHttpError`(4xx/413) | ✅ |
| 1.5 | **SSE 流式** | ⏳ 暂未支持，所有 engine 对 `stream:true` 直接抛错 |
| 2 | OpenAI Responses 路径（`prompt_cache_key` / `previous_response_id` / `/responses/compact`） | ⏳ |
| 3 | Prefix 规范化 + `cache_salt` + Anthropic↔OpenAI-Chat 翻译器 | ⏳ |
| 4 | Microcompact 等价裁剪 + Redis session store | ⏳ |
| 5 | Anthropic `cache_edits` 透传（条件开启） | ⏳ |
| 6 | 引擎补丁子项目（vLLM/SGLang 暴露 evict/pin/unpin） | ⏳ 独立轨道 |

权威细节见 [agent-janus/PLAN_AND_PROGRESS.md](agent-janus/PLAN_AND_PROGRESS.md)（约 600 行的 single source of truth）。

---

## 第三部分 · benchmark 架构

### 3.1 三个数据集的分工

| 数据集 | 规模 | 测什么 | 入口命令 |
|---|---|---|---|
| **历史会话** | 用户本地 `~/.claude` & `~/.openclaw` | 既有缓存效率（无侵入） | `npx tsx src/cli.ts analyze` |
| **Aider Polyglot** | 225 道短编程题，6 语言 | 短任务 cache 命中率 | `scripts/vm-fair-parallel-v3.sh` |
| **SWE-bench Verified** | 500 个真实 GitHub issue | 多步 bug 修复 + resolved 率 | `scripts/vm-swebench-full-v2.py` |

### 3.2 模块 → 目录 → 关键文件 对应表

| 类别 | 子模块 | 文件 | 职责 |
|---|---|---|---|
| **TS CLI** | 入口 | [benchmark/src/cli.ts](benchmark/src/cli.ts) | 命令分发：`analyze` / `run-polyglot` / `run-swebench` / `report` |
| TS CLI | 核心解析 | [benchmark/src/core/jsonl-parser.ts](benchmark/src/core/jsonl-parser.ts) | 双格式解 OpenClaw JSONL（`entry.usage` 和 `message.usage` 都要看）+ Claude Code JSONL |
| TS CLI | 指标 | [benchmark/src/core/metrics.ts](benchmark/src/core/metrics.ts) | cache hit rate / 平均 token / 排名 |
| TS CLI | 类型 | [benchmark/src/core/types.ts](benchmark/src/core/types.ts) | `Session`, `SessionEfficiency`, `Report` |
| TS CLI | 适配 | `src/datasets/{session-logs,aider-polyglot,swe-bench}.ts` | 三类数据源各自的 reader/runner |
| TS CLI | 本地驱动 | [benchmark/src/harness/openclaw-driver.ts](benchmark/src/harness/openclaw-driver.ts) | 直接在本机起 OpenClaw 子进程跑任务 |
| TS CLI | 报表 | `src/report/format.ts` | 控制台聚合表 + 单会话明细 |
| **Python 脚本** | 数据集下载 | [scripts/setup-datasets.sh](benchmark/scripts/setup-datasets.sh) | clone polyglot；HF 拉 SWE-bench Verified |
| Python | VM 中继 | `vm-relay-agent.sh` (VM 内) + `vm-exec.sh` (host 端) | 共享目录做 host↔VM 命令通道（绕开 SSH/pf 限制） |
| Python | 端到端 sample | [scripts/vm-eval-sample.py](benchmark/scripts/vm-eval-sample.py) | 跑一小批 + 评测，全流程演示 |
| Python | **SWE-bench 主跑** | [scripts/vm-swebench-full-v2.py](benchmark/scripts/vm-swebench-full-v2.py) | 500 任务 × 2 agent，可断点续跑（`--run-id`） |
| Python | Patch 重放 | [scripts/replay-patches.py](benchmark/scripts/replay-patches.py) | 从 JSONL 的 `tool_uses` 重放 Edit/Write 到 worktree → `git diff` 出 patch |
| Python | **指标聚合** | [scripts/compute-metrics.py](benchmark/scripts/compute-metrics.py) | 4 个北极星指标；汇总 parent + subagent + attempt 全套 JSONL |
| Python | （旧）评测 | `scripts/evaluate-patches.py`, `reeval-patches.py` | **已废弃**，请改用 docker-eval |
| Python | runners 实战 | `scripts/runners/*.py` | 历次具体 run 用过的脚本（regen/retry/拉镜像/平行 53）|
| **Docker 评测** | 入口 | [benchmark/docker-eval/README.md](benchmark/docker-eval/README.md) | 用官方 `swebench/sweb.eval.x86_64.*` 镜像，每任务一容器 |

### 3.3 单次 run 的产物结构

每次 run 在 `benchmark/results/<run-id>/` 下落产物：

```
results/<run-id>/
├── manifest.json                              # 元数据（agent 版本、数据集、时间）
├── summary.json                               # 状态/patch 总览
├── eval-summary.json                          # 每 agent 的 resolved 率
├── <agent>-<instance>.jsonl                   # 当前 attempt 的 parent session（token 主源）
├── <agent>-<instance>.subagent-<M>.jsonl      # CC 子 agent（必须一并采集，否则少算）
├── <agent>-<instance>.result.json             # sidecar：status / patch_status / duration / session_uuid
├── <agent>-<instance>.patch                   # 提取出来的 git diff
├── <agent>-<instance>.eval.json               # Docker harness 评测结果
└── <agent>-<instance>.attempt-<N>.{...}       # 历次失败重试的归档（token 不能丢）
```

### 3.4 北极星指标（来自 `compute-metrics.py`）

| # | 指标 | 公式 | 用途 |
|---|---|---|---|
| 1 | Resolved 率（strict） | `resolved / dispatched` | 把 infra/quota 失败也算在 agent 头上（悲观） |
| 1 | Resolved 率（lenient） | `resolved / tasks_with_session` | 仅算 agent 跑出会话的子集 |
| 2 | 输入 token / task | `Σ input_total / N` | 平均花费 |
| 3 | Cache 读取总量 | `Σ cache_read` | 绝对值，无法靠缩分母作弊 |
| 4 | **Tokens / resolved** | `Σ input_total / resolved_count` | 北极星：解一道题花多少钱 |

> `input_total = raw_input + cache_read + cache_write`，且要把 parent + 所有 subagent + 所有 `.attempt-N.*` 一并加总。漏掉子 agent 会让 CC 看起来比实际省 ≥30 % 的 token。

### 3.5 Status 命名规范（sidecar `status` 字段）

| status | 含义 | 续跑时 |
|---|---|---|
| `session_collected` | JSONL 落盘成功 | **跳过** |
| `skipped` | 之前已完成 | **跳过** |
| `quota_error` | ChatGPT/Anthropic 限流 | 重试（归档为 `.attempt-N.*`） |
| `agent_error` | 会话里有 `stopReason: error` | 重试 |
| `empty_session` | 文件存在但无 assistant 消息 | 重试 |
| `no_session` | CLI 跑了但没产文件 | 重试，**该次 token 不可恢复**（biases strict[2]） |
| `checkout_*` | repo 准备失败 | 重试 |

---

## 第四部分 · 上手指南（环境配置 → 安装 → 启动）

### 4.0 全局前置

| 工具 | 版本 | 装哪儿 | 备注 |
|---|---|---|---|
| Node.js | ≥ 20 | macOS / Linux | `agent-janus` 必需；benchmark TS CLI 用 |
| npm | 与 Node 同捆 | — | `agent-janus` 用 npm workspaces（不是 pnpm）|
| Python | ≥ 3.10 | macOS / Linux | benchmark scripts + Docker harness |
| Docker | 最新 | 单独装 | 仅 SWE-bench Docker 评测 + agent VM |
| `tsx` | ≥ 4.19 | npm 自动拉 | TypeScript 直跑器 |

> **macOS 使用者**：建议把 Docker 数据目录指向有空闲的盘（SWE-bench 镜像每个 1–3 GB，38 个任务 ~28 GB）。

---

### 4.1 子项目 A：agent-janus

#### 4.1.1 安装

```bash
cd agent-janus
npm install            # 触发 npm workspaces 全装
npm run build          # tsc -b 编译 bridge + 4 个 plugin
npm test               # vitest 跑 127 个用例（应全绿）
```

#### 4.1.2 三种启动方式

**(a) Echo 模式 —— 把 `/v1/messages` 请求原样回显（开发调试）**

```bash
cd agent-janus
npx tsx bridge/src/server-main.ts --port 8787
# 输出：{"msg":"agent-janus listening","host":"127.0.0.1","port":8787,"echoMode":true}
```

测试一发：

```bash
curl -X POST http://127.0.0.1:8787/v1/messages \
  -H 'content-type: application/json' \
  -d '{"model":"claude-sonnet-4-6","max_tokens":256,"messages":[{"role":"user","content":"hi"}]}'
```

**(b) 真透传到 Anthropic Cloud（最简生产场景）**

写一个 5 行启动脚本（仿 `scripts/smoke-anthropic-passthrough.mjs`）：

```javascript
import { InMemoryPluginRegistry, startHttpServer } from "@agent-janus/bridge";
import { AnthropicMessagesHarness } from "@agent-janus/harness-anthropic-messages";
import { AnthropicPassthroughEngine } from "@agent-janus/engine-anthropic-passthrough";

const registry = new InMemoryPluginRegistry();
registry.registerHarness(new AnthropicMessagesHarness());
registry.registerEngine(new AnthropicPassthroughEngine({
  baseUrl: "https://api.anthropic.com",
  apiKey: process.env.ANTHROPIC_API_KEY,
}));

const server = await startHttpServer({ registry, port: 8787 });
console.log(`bridge → ${server.host}:${server.port}`);
```

把 Claude Code 指过来：

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude
```

**(c) 本地 mock smoke test（无需 API key、5 秒搞定）**

```bash
cd agent-janus
node scripts/smoke-anthropic-passthrough.mjs
# 启动 mock upstream + bridge + curl，应看到 cache_read_input_tokens=7 透传成功
```

#### 4.1.3 当前限制（实操必读）

- 所有 engine 对 `"stream": true` 都会 **抛错**（Phase 1.5 才解锁）。
- 仅 Anthropic 协议路径走通；OpenAI Responses（Codex/OpenClaw 用）尚未实现。
- Session store 只有 `MemorySessionStore`，进程重启即丢；多副本部署待 Phase 4。

---

### 4.2 子项目 B：benchmark

#### 4.2.1 安装 + 数据集

```bash
cd benchmark

# Node 依赖（仅 tsx + typescript）
npm install

# Python 依赖（用于 SWE-bench 下载和评测）
pip install datasets unidiff swebench

# 拉数据集（polyglot ~10 MB，SWE-bench Verified ~5 MB）
bash scripts/setup-datasets.sh
```

成功后会看到：

```
[1/2] Aider Polyglot: cloned, ~225 exercises
[2/2] SWE-bench Verified: 500 instances written
```

#### 4.2.2 4 个常用命令（按场景）

**场景 1 · 分析你本机已有的会话日志（无需下数据）**

```bash
cd benchmark
npx tsx src/cli.ts analyze --max-files 100 --min-turns 4
# 扫描 ~/.claude/projects/ + ~/.openclaw/agents/，输出 cache hit rate、最差会话时间线
```

**场景 2 · 干跑（dry-run）确认数据集就绪**

```bash
npx tsx src/cli.ts run-polyglot --max 5 --dry-run
npx tsx src/cli.ts run-swebench --max 5 --dry-run
```

**场景 3 · 真跑一次 SWE-bench 端到端 sample（10 任务 × 2 agent）**

> 需要装好 `claude` 和 `openclaw` 两个 CLI 并各自登录。

```bash
# 在 VM 内或本机直跑都可以
python3 scripts/vm-eval-sample.py
# 产物：results/eval-sample-<TS>/{manifest.json, *.jsonl, *.patch, *.result.json}
```

**场景 4 · Docker 评测 + 算指标**

```bash
# 1) 用官方 harness 评测 patch（详见 docker-eval/README.md）
cd benchmark/docker-eval
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Verified \
  --predictions_path predictions-cc.jsonl \
  --max_workers 4 \
  --run_id cc-eval-001

# 2) 算 4 个北极星指标
cd ..
python3 scripts/compute-metrics.py \
  --sessions-dir results/<run-id> \
  --harness-logs docker-eval/logs/run_evaluation \
  --run-id cc-eval-001
```

#### 4.2.3 在 Lume VM 里跑（推荐用于全量 500 任务）

```bash
# host：起 VM（已预装 OpenClaw + Claude Code）
lume run --network bridged \
  --shared-dir "$(pwd):rw" openclaw

# VM（VNC 进去，开终端）：
bash "/Volumes/My Shared Files/benchmark/scripts/vm-relay-agent.sh"

# host：发命令
bash benchmark/scripts/vm-exec.sh "openclaw --version"

# host：把全量 runner 推到 VM（共享目录有 ≥10 KB 文件 sync 延迟，必须 base64 转）
B=$(base64 < benchmark/scripts/vm-swebench-full-v2.py)
bash benchmark/scripts/vm-exec.sh "echo '$B' | base64 -d > /tmp/swe.py"
bash benchmark/scripts/vm-exec.sh "nohup python3 /tmp/swe.py > /tmp/swe.log 2>&1 &"
```

> **断点续跑**：`python3 /tmp/swe.py --run-id <existing-run-id>` 会跳过 `session_collected` / `skipped` 的任务，归档失败的 attempt 做 retry。

---

### 4.3 常见踩坑速查

| 现象 | 根因 | 修法 |
|---|---|---|
| `IndentationError` 在合法 Python 上 | Lume 共享目录 sync 延迟 | 用 `base64` 走 vm-exec 重传 |
| OpenClaw 跑 ~25 任务后全 `quota_error` | ChatGPT Plus 限流 | 等几小时，断点续跑 |
| CC 在 root 用户报 `--dangerously-skip-permissions` 拒绝 | CC root 防呆 | 设环境变量 `IS_SANDBOX=1`（仅在 VM 内） |
| 多 agent 并行 `git worktree add` 失败 | `.git/worktrees/*.lock` 竞争 | 串行 retry：用 `runners/retry_checkout_fail_serial.py` |
| Docker harness ~10–30 % 任务 SSL EOF | `raw.githubusercontent.com` / docker hub 代理抖动 | `--max_workers 1` 重跑；或预拉镜像（见 `runners/prepull_*.sh`） |
| compute-metrics 算出来 CC 比实际省 30 % | 漏采 subagent JSONL | 检查 `<tag>.subagent-*.jsonl` 是否一并落到 `results/<run-id>/` |
| agent-janus engine 报 `stream not supported` | Phase 1.5 还没做 | 在 Harness 侧把 `stream:false` 强制下发 |

---

### 4.4 推荐的探索顺序

1. 先读 [CLAUDE.md](CLAUDE.md) —— 项目使命 & 已知最优结果。
2. 跑 `benchmark` 的 `analyze` 命令，看自己的历史 cache 命中率。
3. 跑 `agent-janus` 的 `smoke-anthropic-passthrough.mjs`，理解 IR 透传链路。
4. 看 [agent-janus/PLAN_AND_PROGRESS.md](agent-janus/PLAN_AND_PROGRESS.md) 的 §6.1 三层图。
5. 看 [reviews/](reviews/) 目录 —— 每轮评审都修过真实 bug，是理解为什么这么写的最快路径。

---

## 附录 · 衔接路线（agent-janus × benchmark）

未来把两者串起来的方式：

```
benchmark/scripts/vm-swebench-full-v2.py
   │
   │  (新增: ANTHROPIC_BASE_URL=http://janus:8787)
   ▼
agent-janus  (Phase 2+ 上 OpenAI Responses 协议后)
   │
   ▼
真实 Anthropic / vLLM / SGLang
   │
   ▼
session JSONL 流回 benchmark/results/<run-id>/
   │
   ▼
compute-metrics.py 出指标 → 对比 "走桥 vs. 不走桥" 的 4 个北极星
```

这正是项目长期目标：**用 benchmark 量化 agent-janus 的优化收益**。
