# CHANGELOG

本文件记录用户可见的代码改动；协议层面的设计变动看
[`docs/2026-05-06-stela-protocol.md`](docs/2026-05-06-stela-protocol.md)。

格式参照 Keep a Changelog；时间用绝对日期。

---

## [Unreleased] — 2026-05-14

本批次围绕两件事：**零侵入接入路径（HTTP 反向代理）**，以及**多轮状态真累积**。
中期目标全部完成；SDK transport 与代理两条路径功能等价（除 SDK 流式尚未补全）。

### Added

- **`stela.proxy`** —— aiohttp SSE-aware Anthropic 反向代理（路径 B）。
  - 监听 `POST /v1/messages`，自动检测 harness（openclaw / hermes），跑 STELA 管线后转发到 Anthropic
  - 非 `/v1/messages` 路径透明 passthrough
  - SSE 流式响应支持；旁路解析 `message_start` / `message_delta` 取 usage
  - LRU session 注册表（默认 10000 上限），按 session_id keyed
  - CLI: `python -m stela.proxy` / `stela proxy`
- **`stela.init`** —— agent 配置注入器，RTK 同款模式。
  - `claude-code` installer: patch `~/.claude/settings.json` 的 `env.ANTHROPIC_BASE_URL`，保留用户原值，幂等，可 `--uninstall` 还原
  - `generic` installer: 打印 shell export 指令
  - CLI: `python -m stela.init --agent <name>` / `stela init --agent <name>`
- **`stela` 统一 CLI**：dispatch 子命令 `proxy` / `init`，由 `pyproject.toml` `[project.scripts]` 注册。
- **`StelaAnthropicTransport`** ([scripts/stela_anthropic_transport.py](scripts/stela_anthropic_transport.py)) —— SDK transport（路径 A）的 Anthropic 端，对称于已有的 `StelaOpenAITransport`。
  - `messages.create(**kwargs)` 鸭子接口
  - 自动检测 harness（hermes 标记 → hermes，否则 openclaw）；可显式 `harness_name=` 覆盖
- **`BridgeSessionState`**（公开 dataclass，[bridge.py](bridge.py)）—— 跨 turn 持久化的 Bridge 状态容器。封装 `RefPool` + `_SessionStats`。
  - `Bridge.__init__` 新增可选参数 `session_state`；缺省时内部 new 一个（行为退化为旧版每轮独立）
  - `Bridge.session_state` property 暴露状态给上游
- **`Bridge.emit_with_plan() -> (wire, plan)`** —— `emit()` 的二元返回版本，包内含完整 `_canonicalize_ir → assert_invariants → plan_marks → engine.emit` 流程。
- **`RefPool.register_or_skip(slug, block) -> bool`** —— 幂等注册，已存在的 slug 跳过。跨 turn 共享 RefPool 必备。
- **`ir.enforce_band_order(blocks)`** —— 稳定按 `pin* → fold* → drop*` 排序，公开辅助函数。
- **稳定 session-id 派生** ([proxy/server.py](proxy/server.py))：内容派生策略 `blake2b(api_key + system + tools + messages[0])`，多轮对话保持同一 session_id。优先级链：`x-stela-session` header → `metadata.user_id` → 派生 hash。
- **`pyproject.toml`** —— 标准 PEP 517 包，`pip install -e .` 即可让 `stela` 全局可导入。
- **可观测的累积字段**：proxy usage log 和 transport trace log 都新增 `cumulative.{cache_creation, real_requests_since_refresh, refpool_slugs}` 块。
- **新 8 套测试**（45 个测试函数）：
  - [tests/test_proxy_pipeline.py](tests/test_proxy_pipeline.py)（5）—— 管线纯函数
  - [tests/test_proxy_server.py](tests/test_proxy_server.py)（6）—— mock upstream 端到端
  - [tests/test_proxy_session_id.py](tests/test_proxy_session_id.py)（9）—— session-id 派生稳定性
  - [tests/test_proxy_accumulation.py](tests/test_proxy_accumulation.py)（2）—— HTTP 路径多轮累积
  - [tests/test_bridge_session_state.py](tests/test_bridge_session_state.py)（6）—— Bridge state 共享语义
  - [tests/test_sdk_transport_accumulation.py](tests/test_sdk_transport_accumulation.py)（3）—— SDK transport 多轮累积
  - [tests/test_harness_multiblock.py](tests/test_harness_multiblock.py)（4）—— §5 顺序回归
  - [tests/test_init_claude_code.py](tests/test_init_claude_code.py)（8）—— installer 幂等 / 还原

### Fixed

- **harness §5 顺序违反**（[harness/openclaw.py](harness/openclaw.py)、[harness/hermes.py](harness/hermes.py)）：user message 含多个 content block 时，每个 block 各自 expand 成 `(PIN, FOLD*, DROP*)`，旧代码直接拼接导致 `PIN, DROP, PIN, DROP, ...` 违反 `pin* → fold* → drop*`。这是真实 Claude Code 流量必触发的 bug（多 part 内容是常态）。修复：message 级别用 `enforce_band_order` 兜底排序。
- **canonicalize 漏洞（SDK transport 和 proxy 都有）**：旧代码 `bridge.mark()` 后用 `engine.emit(snapshot_ir, plan)` 直接出 wire，**跳过了 `_canonicalize_ir`**（tools 顺序、payload key 顺序）。导致 tool 数组的多 server / builtin / user 混排顺序不稳，prefix cache 隐性失效。
  - [proxy/pipeline.py](proxy/pipeline.py) 改用 `bridge.emit_with_plan()`
  - [scripts/stela_anthropic_transport.py](scripts/stela_anthropic_transport.py) 改用 `bridge.emit_with_plan()`
  - [scripts/stela_transport.py](scripts/stela_transport.py) 保留自定义 chat-completions wire builder，但补一次 `_canonicalize_ir(snapshot)` 再喂
- **多轮 Bridge 状态永远归零**：proxy 与 SDK transport 都每次新建 `Bridge`，所以 R8 cache_creation 累计、real_requests 计数永远是 0，refresh 自适应门控永远不触发。`BridgeSessionState` 把这两个字段外置到 session 范围；proxy 用 LRU 注册表 keyed by session_id 持有；transport 用实例字段持有。
- **proxy 500 风暴**：STELA 管线抛异常时旧代码返回 500，Anthropic SDK 重试 10 次后崩溃。新增 **passthrough fallback**：默认行为是降级到原 raw 透传，确保优化层 fail 不破坏正确性。`--strict` 标志可恢复 500 行为（用于测试/调试）。
- **proxy 日志噪音**：连续 STELA 失败时旧代码每次打完整 traceback。新行为：首次失败完整 traceback，后续每条 WARNING 单行。

### Changed

- **`Bridge.__init__` 签名扩展**：新增可选 keyword-only 参数 `session_state`。默认为 `None`，内部 new 一个 → 完全向后兼容，现有调用方无需改动。
- **`PipelineResult` 新增字段**：`cumulative_cache_creation`、`real_requests_since_refresh`。旧字段未变。
- **`StelaOpenAITransport.__init__` 新增可选参数** `session_state`。

### Removed

- `proxy/server.py` 旧的 `uuid4()` 兜底已被内容派生 session-id 替换。
- `Bridge._refpool` 和 `Bridge._stats` 实例属性内部改成转发到 `_state.refpool` / `_state.stats` 的 property。外部访问点未变（旧代码继续工作）。

---

## [0.1.0] — 2026-05-06（初始公开版本）

- STELA 协议 Python 参考实现
- 3 个 harness plugin: `openclaw` / `hermes` / `telos`
- 5 个 engine adapter: `anthropic` / `openai` / `deepseek` / `vllm` / `sglang`
- `Bridge` 5 原语：`place` / `pin` / `mark` / `fold` / `refresh`
- `BidirectionalEngineAdapter` mixin 用于 vLLM / SGLang
- `StelaOpenAITransport`（仅 OpenAI shape，给 telos / mini_swe_runner 用）
- `test_smoke.py` 9 个测试覆盖 R1–R8 修复点
