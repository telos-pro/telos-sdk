"""aiohttp 反向代理 server。

监听 ``POST /v1/messages``：跑 STELA 管线、转发到 Anthropic（或自定义 upstream），
支持 SSE 流式响应。其他路径（``/v1/messages/batches``、``/v1/models`` 等）
按原样透传，不经过 STELA 改写。

零侵入接入：

::

    # 启动代理
    python -m stela.proxy --port 7171

    # 任意 Anthropic-SDK 客户端：
    export ANTHROPIC_BASE_URL=http://localhost:7171
    claude  # 或自家 agent

设计：

- ``ProxyApp`` 持有共享的 ``aiohttp.ClientSession``，复用 keep-alive 连接。
- 流式路径（``stream=true``）：边读 upstream content 边 write 到 downstream；
  同时旁路解析 SSE 事件，从 ``message_start`` / ``message_delta`` 抽 usage。
- 非流式路径：完整 read → forward；从 JSON 抽 usage。
- 错误必须按 Anthropic wire schema 回传 (``{"type": "error", "error": {...}}``)，
  否则 client SDK 会拿到结构化异常。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import aiohttp
from aiohttp import web

from stela.bridge import BridgeSessionState
from stela.proxy.inspector import (
    SessionInspector as _SessionInspector,
    SessionInspectorEntry as _SessionInspectorEntry,
    ToolStat as _ToolStat,
    entry_to_json as _inspector_entry_to_json,
)
from stela.proxy.pipeline import PipelineResult, process_anthropic_request


_DEFAULT_UPSTREAM = "https://api.anthropic.com"

# 转发到 upstream 时保留的 header（认证 / 协议版本 / Anthropic 私有 beta）。
# Host / Content-Length 由 aiohttp 自己算；不要从 client 透传。
_FORWARD_HEADER_WHITELIST = (
    "x-api-key",
    "authorization",
    "anthropic-version",
    "anthropic-beta",
    "anthropic-dangerous-direct-browser-access",
    "user-agent",
)

_log = logging.getLogger("stela.proxy")


# ---------------------------------------------------------------------------
# 稳定 session-id 派生
# ---------------------------------------------------------------------------

def _client_identity(headers: Mapping[str, str]) -> str:
    """从 headers 取一个能区分不同 caller 的稳定串。

    优先 ``x-api-key``；其次 ``authorization``（去掉 ``Bearer`` 前缀）；
    都没有就返回空串（多个匿名 client 会共享 session-id —— 单机开发够用）。
    """
    api_key = headers.get("x-api-key")
    if api_key:
        return api_key
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return auth.strip()


def _derive_session_id(raw: Mapping[str, Any], headers: Mapping[str, str]) -> str:
    """从「client 身份 + 对话 seed」派生稳定 session-id。

    一个"对话"在 Anthropic /v1/messages 协议下，跨轮不变的部分是：
    ``system`` / ``tools`` / ``messages[0]``（即 conversation 第一条 user
    消息）。每轮请求只在 ``messages[]`` 尾部追加新内容。所以这四项的 hash
    就唯一标识一段对话。

    返回形如 ``"stela-<16字节hex>"``，方便 grep。
    """
    seed = {
        "client": _client_identity(headers),
        "system": raw.get("system") or [],
        "tools": raw.get("tools") or [],
        # messages[0] 通常是 conversation 的第一条 user message；
        # 即使 list 为空也用 None placeholder 保证 hash 可计算。
        "msg0": (raw.get("messages") or [None])[0],
    }
    body = json.dumps(seed, sort_keys=True, ensure_ascii=False,
                       default=str).encode("utf-8")
    digest = hashlib.blake2b(body, digest_size=8).hexdigest()
    return f"stela-{digest}"


# ---------------------------------------------------------------------------
# Session 注册表：keyed by session_id，LRU 上限避免长跑时内存爆炸
# ---------------------------------------------------------------------------

_DEFAULT_MAX_SESSIONS = 10_000


class _SessionRegistry:
    """``dict[session_id, BridgeSessionState]`` 的有界 LRU 包装。

    在 aiohttp 单事件循环下，所有访问发生在同一线程，无需加锁。同一
    session 的并发请求会顺序看到累积——足够好。如果未来要支持真并发，
    每 entry 加一个 ``asyncio.Lock`` 即可。
    """

    def __init__(self, max_size: int = _DEFAULT_MAX_SESSIONS) -> None:
        self._max = max_size
        self._sessions: OrderedDict[str, BridgeSessionState] = OrderedDict()

    def get_or_create(self, session_id: str) -> BridgeSessionState:
        if session_id in self._sessions:
            self._sessions.move_to_end(session_id)
            return self._sessions[session_id]
        state = BridgeSessionState()
        self._sessions[session_id] = state
        if len(self._sessions) > self._max:
            evicted, _ = self._sessions.popitem(last=False)
            _log.info("session LRU evicted: %s (size=%d)", evicted, self._max)
        return state

    def __len__(self) -> int:
        return len(self._sessions)


# ---------------------------------------------------------------------------
# usage 归一化（与 stela_anthropic_transport._normalize_usage 同 schema）
# ---------------------------------------------------------------------------

def _normalize_usage(u: dict[str, Any]) -> dict[str, int]:
    """4 个 bucket 的归一化；保留 ``cache_creation.ephemeral_{5m,1h}_input_tokens``
    在 raw_usage 里，dashboard 端会读取拆分以正确按 5m / 1h 价计费。"""
    return {
        "raw_input": int(u.get("input_tokens", 0) or 0),
        "cache_read": int(u.get("cache_read_input_tokens", 0) or 0),
        "cache_write": int(u.get("cache_creation_input_tokens", 0) or 0),
        "output": int(u.get("output_tokens", 0) or 0),
    }


def _anthropic_error(status: int, err_type: str, message: str) -> web.Response:
    return web.json_response(
        {"type": "error", "error": {"type": err_type, "message": message}},
        status=status,
    )


# ---------------------------------------------------------------------------
# ProxyApp：持有共享 session + 处理 handler
# ---------------------------------------------------------------------------

class ProxyApp:
    def __init__(
        self,
        *,
        upstream: str = _DEFAULT_UPSTREAM,
        usage_log: Path | None = None,
        harness_override: str | None = None,
        request_timeout: float = 600.0,
        strict: bool = False,
        max_sessions: int = _DEFAULT_MAX_SESSIONS,
        dashboard_refresh: int = 5,
    ):
        self.upstream = upstream.rstrip("/")
        self.usage_log = usage_log
        self.harness_override = harness_override
        self.request_timeout = request_timeout
        # strict=False（默认）：STELA 失败时原样透传到 upstream，保证
        # 优化层永远不会破坏正确性（RTK 同款"rewrite 失败 → 原命令"原则）。
        # strict=True：测试 / 调试用，STELA 失败直接 500。
        self.strict = strict
        # /__stela/dashboard 的 meta-refresh 间隔（秒）；0 = 关闭 auto-refresh。
        self.dashboard_refresh = dashboard_refresh

        if usage_log is not None:
            usage_log.parent.mkdir(parents=True, exist_ok=True)

        self._session: aiohttp.ClientSession | None = None
        self._call_count = 0
        self._pipeline_failures = 0
        self._registry = _SessionRegistry(max_size=max_sessions)
        self._inspector = _SessionInspector(max_size=max_sessions)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.request_timeout),
                # 关闭自动解压，确保我们看到的字节就是 wire 字节。
                auto_decompress=True,
            )
        return self._session

    async def on_shutdown(self, app: web.Application) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def _forward_headers(self, request: web.Request) -> dict[str, str]:
        out: dict[str, str] = {"content-type": "application/json"}
        for h in _FORWARD_HEADER_WHITELIST:
            v = request.headers.get(h)
            if v is not None:
                out[h] = v
        accept = request.headers.get("accept")
        if accept:
            out["accept"] = accept
        return out

    # ------------------------------------------------------------------
    # POST /v1/messages —— 主 handler
    # ------------------------------------------------------------------

    async def handle_messages(self, request: web.Request) -> web.StreamResponse:
        self._call_count += 1
        call_index = self._call_count

        try:
            raw = await request.json()
        except Exception as e:  # noqa: BLE001
            return _anthropic_error(400, "invalid_request_error", f"Invalid JSON: {e}")

        session_id = (
            request.headers.get("x-stela-session")
            or (raw.get("metadata") or {}).get("user_id")
            or _derive_session_id(raw, request.headers)
        )
        session_state = self._registry.get_or_create(session_id)

        # ---- 1. STELA 管线 ----
        try:
            result = process_anthropic_request(
                raw,
                session_id=session_id,
                session_state=session_state,
                harness_name=self.harness_override,
            )
        except Exception as e:  # noqa: BLE001
            self._pipeline_failures += 1
            # 第一次失败打完整 traceback；后续只打一行短消息减少日志噪音。
            if self._pipeline_failures == 1:
                _log.exception("STELA pipeline failed (call=%d) — falling back to "
                               "passthrough. Further failures will log a single line.",
                               call_index)
            else:
                _log.warning("STELA pipeline failed (call=%d, total=%d): %s",
                             call_index, self._pipeline_failures, e)
            if self.strict:
                return _anthropic_error(500, "api_error",
                                        f"STELA pipeline failed: {e}")
            # 优雅降级：用原 raw 当 wire，造一个空 result 走透传路径。
            result = PipelineResult(
                wire=dict(raw),
                harness="passthrough",
                plan_slots=[],
                routing_key=None,
                model=raw.get("model", ""),
            )

        is_streaming = bool(raw.get("stream", False))
        url = f"{self.upstream}/v1/messages"
        headers = self._forward_headers(request)
        body_bytes = json.dumps(result.wire).encode("utf-8")

        session = await self._session_get()
        t0 = time.time()

        try:
            upstream = await session.post(url, data=body_bytes, headers=headers)
        except aiohttp.ClientError as e:
            _log.exception("Upstream connection failed (call=%d)", call_index)
            return _anthropic_error(502, "api_error", f"Upstream error: {e}")

        if is_streaming:
            return await self._stream_response(
                request, upstream, session_id, result, session_state,
                call_index, t0,
            )
        return await self._buffered_response(
            upstream, session_id, result, session_state, call_index, t0,
        )

    # ------------------------------------------------------------------
    # 非流式路径
    # ------------------------------------------------------------------

    async def _buffered_response(
        self,
        upstream: aiohttp.ClientResponse,
        session_id: str,
        result: PipelineResult,
        session_state: BridgeSessionState,
        call_index: int,
        t0: float,
    ) -> web.Response:
        try:
            body = await upstream.read()
            status = upstream.status
            ct = upstream.headers.get("content-type", "application/json")
        finally:
            upstream.release()

        usage: dict[str, Any] = {}
        if status == 200:
            try:
                parsed = json.loads(body.decode("utf-8"))
                usage = parsed.get("usage") or {}
            except Exception:  # noqa: BLE001
                pass

        self._accumulate_into_state(session_state, usage)
        latency_s = time.time() - t0
        self._log_usage(
            session_id, result, usage, session_state,
            latency_s=latency_s,
            streaming=False,
            status=status,
            call_index=call_index,
        )
        self._update_inspector(session_id, result, usage, call_index, latency_s)
        return web.Response(body=body, status=status, headers={"Content-Type": ct})

    # ------------------------------------------------------------------
    # 流式路径（SSE）
    # ------------------------------------------------------------------

    async def _stream_response(
        self,
        request: web.Request,
        upstream: aiohttp.ClientResponse,
        session_id: str,
        result: PipelineResult,
        session_state: BridgeSessionState,
        call_index: int,
        t0: float,
    ) -> web.StreamResponse:
        status = upstream.status

        # 错误响应不会是 SSE：read 完整 body 一次性返回。
        if status != 200:
            try:
                body = await upstream.read()
                ct = upstream.headers.get("content-type", "application/json")
            finally:
                upstream.release()
            self._log_usage(
                session_id, result, {}, session_state,
                latency_s=time.time() - t0,
                streaming=True,
                status=status,
                call_index=call_index,
            )
            return web.Response(body=body, status=status, headers={"Content-Type": ct})

        # 200：开始流式转发。
        downstream = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": upstream.headers.get(
                    "content-type", "text/event-stream"
                ),
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await downstream.prepare(request)

        usage_aggregate: dict[str, Any] = {}
        sse_buf = b""

        try:
            async for chunk in upstream.content.iter_any():
                # 立即转发，不等 SSE 完整事件
                try:
                    await downstream.write(chunk)
                except (ConnectionResetError, asyncio.CancelledError):
                    _log.info("downstream disconnected mid-stream (call=%d)", call_index)
                    break

                # 旁路：累积并解析完整 SSE block 取 usage
                sse_buf += chunk
                while b"\n\n" in sse_buf:
                    block, sse_buf = sse_buf.split(b"\n\n", 1)
                    self._peek_sse_block(block, usage_aggregate)
        except aiohttp.ClientPayloadError:
            _log.warning("upstream closed connection mid-stream (call=%d)", call_index)

        try:
            await downstream.write_eof()
        except Exception:
            pass
        finally:
            upstream.release()

        self._accumulate_into_state(session_state, usage_aggregate)
        latency_s = time.time() - t0
        self._log_usage(
            session_id, result, usage_aggregate, session_state,
            latency_s=latency_s,
            streaming=True,
            status=200,
            call_index=call_index,
        )
        self._update_inspector(session_id, result, usage_aggregate, call_index, latency_s)
        return downstream

    # ------------------------------------------------------------------
    # SSE 解析（旁路）
    # ------------------------------------------------------------------

    def _peek_sse_block(self, block: bytes, usage: dict[str, Any]) -> None:
        """从 SSE 事件块抽 usage 字段。

        Anthropic SSE 协议：``message_start`` 携带 input/cache 字段，
        ``message_delta`` 携带累计 output_tokens。出错就静默吞，绝不影响代理。
        """
        event: str | None = None
        data_raw: bytes | None = None
        for line in block.split(b"\n"):
            if line.startswith(b"event:"):
                event = line[6:].strip().decode("ascii", "ignore")
            elif line.startswith(b"data:"):
                data_raw = line[5:].strip()
        if data_raw is None:
            return
        try:
            data = json.loads(data_raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return

        if event == "message_start":
            u = (data.get("message") or {}).get("usage") or {}
            for k in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "output_tokens",
            ):
                if k in u:
                    usage[k] = int(u[k])
            # 关键：把 5m / 1h 拆分原样带过来，dashboard 端做精确计费用
            cc = u.get("cache_creation")
            if isinstance(cc, dict):
                usage["cache_creation"] = {
                    "ephemeral_5m_input_tokens":
                        int(cc.get("ephemeral_5m_input_tokens", 0) or 0),
                    "ephemeral_1h_input_tokens":
                        int(cc.get("ephemeral_1h_input_tokens", 0) or 0),
                }
        elif event == "message_delta":
            u = data.get("usage") or {}
            if "output_tokens" in u:
                usage["output_tokens"] = int(u["output_tokens"])

    # ------------------------------------------------------------------
    # GET /__stela/dashboard —— live savings dashboard
    # ------------------------------------------------------------------

    async def handle_dashboard(self, request: web.Request) -> web.Response:
        """实时读 usage_log → aggregate → 渲染 HTML。

        每次请求都重新读全文件 + 重渲染。usage_log 不会大（几十 K/天的
        量级，每行 jsonl 一两百字节），所以不用搞缓存或 incremental。
        """
        # 延迟 import，避免 proxy 冷启动时强依赖 dashboard 模块。
        from stela.scripts.build_savings_dashboard import render_from_usage_log

        try:
            body = render_from_usage_log(
                self.usage_log,
                refresh_seconds=self.dashboard_refresh,
            )
        except Exception as e:  # noqa: BLE001
            _log.exception("dashboard render failed")
            return web.Response(
                text=f"<pre>dashboard render failed: {e}</pre>",
                content_type="text/html", status=500,
            )
        return web.Response(text=body, content_type="text/html")

    # ------------------------------------------------------------------
    # GET /__stela/developer —— 面向开发者的实时 session 结构 / 工具调用统计
    # ------------------------------------------------------------------

    async def handle_developer(self, request: web.Request) -> web.Response:
        """实时渲染当前内存里所有 session 的 IR 结构、bp 区域、工具调用。

        如果 query 里带 ``?session=<id>``，仅渲染那一个 session 的详情；
        否则渲染概览（session 列表）+ 最近一次 call 的 session 详情。
        """
        from stela.scripts.build_developer_page import render_developer

        try:
            body = render_developer(
                self._inspector,
                self._registry,
                focus_session=request.query.get("session"),
                refresh_seconds=self.dashboard_refresh,
                tab=request.query.get("tab", "overview"),
            )
        except Exception as e:  # noqa: BLE001
            _log.exception("developer page render failed")
            return web.Response(
                text=f"<pre>developer page render failed: {e}</pre>",
                content_type="text/html", status=500,
            )
        return web.Response(text=body, content_type="text/html")

    # ------------------------------------------------------------------
    # GET /__stela/developer.json —— 同样的数据，机器可读
    # ------------------------------------------------------------------

    async def handle_developer_json(self, request: web.Request) -> web.Response:
        """JSON 视图：scripts / 第三方工具读取 session 状态用。"""
        sid = request.query.get("session")
        if sid:
            entry = self._inspector.get(sid)
            if entry is None:
                return web.json_response(
                    {"error": "unknown session", "session_id": sid}, status=404)
            return web.json_response(_inspector_entry_to_json(entry))

        return web.json_response({
            "session_count": len(self._inspector),
            "sessions": [
                {
                    "session_id": sid,
                    "last_seen": e.last_seen,
                    "calls": len(e.calls),
                    "model": e.last_model,
                    "harness": e.last_harness,
                    "tools_seen": sorted(e.tools_stat.keys()),
                }
                for sid, e in self._inspector.items()
            ],
        })

    # ------------------------------------------------------------------
    # 内部：每收一次响应都把状态推给 inspector
    # ------------------------------------------------------------------

    def _update_inspector(
        self,
        session_id: str,
        result: "PipelineResult",  # noqa: F821 — 跨模块前向引用
        usage: Mapping[str, Any],
        call_index: int,
        latency_s: float,
    ) -> None:
        try:
            entry = self._inspector.touch(session_id)
            entry.record(
                call_index=call_index,
                layout=dict(result.ir_layout),
                plan_slots=list(result.plan_slots),
                tool_uses=list(result.tool_uses),
                tool_results=list(result.tool_results),
                usage_norm=_normalize_usage(dict(usage)),
                usage_raw=dict(usage),
                latency_s=latency_s,
                model=result.model,
                harness=result.harness,
            )
        except Exception:  # noqa: BLE001
            _log.exception("inspector record failed (call=%d)", call_index)

    # ------------------------------------------------------------------
    # 透明 passthrough：非 /v1/messages 的所有路径
    # ------------------------------------------------------------------

    async def handle_passthrough(self, request: web.Request) -> web.StreamResponse:
        url = f"{self.upstream}{request.rel_url}"
        headers = self._forward_headers(request)
        body = await request.read()
        session = await self._session_get()

        try:
            upstream = await session.request(
                request.method, url, headers=headers, data=body,
            )
        except aiohttp.ClientError as e:
            return _anthropic_error(502, "api_error", f"Upstream error: {e}")

        try:
            body_bytes = await upstream.read()
            status = upstream.status
            ct = upstream.headers.get("content-type", "application/octet-stream")
        finally:
            upstream.release()

        return web.Response(body=body_bytes, status=status, headers={"Content-Type": ct})

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------

    def _accumulate_into_state(
        self, session_state: BridgeSessionState, usage: Mapping[str, Any],
    ) -> None:
        """把 upstream 响应的 cache_creation tokens 累加进 session_state。

        这是 STELA 的 ``bridge.absorb_usage`` 在 proxy 路径下的等价物。
        没有这一步，R8 触发条件永远凑不齐 ``cumulative_cache_creation``
        阈值。
        """
        cache_write = int(usage.get("cache_creation_input_tokens", 0) or 0)
        if cache_write:
            session_state.stats.cumulative_cache_creation += cache_write

    def _log_usage(
        self,
        session_id: str,
        result: PipelineResult,
        usage: dict[str, Any],
        session_state: BridgeSessionState,
        *,
        latency_s: float,
        streaming: bool,
        status: int,
        call_index: int,
    ) -> None:
        if self.usage_log is None:
            return
        try:
            with self.usage_log.open("a") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "session_id": session_id,
                    "call_index": call_index,
                    "model": result.model,
                    "harness": result.harness,
                    "n_slots": len(result.plan_slots),
                    "slots": result.plan_slots,
                    "latency_s": round(latency_s, 3),
                    "streaming": streaming,
                    "status": status,
                    "raw_usage": usage,
                    "normalized": _normalize_usage(usage),
                    "cumulative": {
                        "cache_creation": session_state.stats.cumulative_cache_creation,
                        "real_requests_since_refresh":
                            session_state.stats.real_requests_since_refresh,
                        "refpool_slugs": sorted(session_state.refpool.slugs),
                    },
                }, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            _log.exception("usage log write failed")


# ---------------------------------------------------------------------------
# 应用构造
# ---------------------------------------------------------------------------

def make_app(
    *,
    upstream: str = _DEFAULT_UPSTREAM,
    usage_log: Path | None = None,
    harness_override: str | None = None,
    strict: bool = False,
    dashboard_refresh: int = 5,
) -> web.Application:
    """构造一个完整的 aiohttp 应用。可被测试 / ASGI 嵌入复用。"""
    proxy = ProxyApp(
        upstream=upstream,
        usage_log=usage_log,
        harness_override=harness_override,
        strict=strict,
        dashboard_refresh=dashboard_refresh,
    )
    app = web.Application()
    app.router.add_post("/v1/messages", proxy.handle_messages)
    # 必须在 catch-all passthrough 之前注册，否则会被吞掉。
    app.router.add_get("/__stela/dashboard", proxy.handle_dashboard)
    app.router.add_get("/__stela/developer", proxy.handle_developer)
    app.router.add_get("/__stela/developer.json", proxy.handle_developer_json)
    app.router.add_route("*", "/{tail:.*}", proxy.handle_passthrough)
    app.on_shutdown.append(proxy.on_shutdown)
    app["proxy"] = proxy
    return app


def run(
    *,
    host: str = "127.0.0.1",
    port: int = 7171,
    upstream: str = _DEFAULT_UPSTREAM,
    usage_log: Path | None = None,
    harness_override: str | None = None,
    strict: bool = False,
    dashboard_refresh: int = 5,
) -> None:
    """阻塞式启动（CLI 入口用）。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = make_app(
        upstream=upstream,
        usage_log=usage_log,
        harness_override=harness_override,
        strict=strict,
        dashboard_refresh=dashboard_refresh,
    )
    _log.info("STELA proxy listening on http://%s:%d → %s", host, port, upstream)
    if usage_log:
        _log.info("usage log    → %s", usage_log)
        _log.info("dashboard    → http://%s:%d/__stela/dashboard"
                  " (refresh=%ds)", host, port, dashboard_refresh)
    else:
        _log.info("dashboard    → http://%s:%d/__stela/dashboard"
                  " (no usage_log; will show empty state)", host, port)
    _log.info("developer    → http://%s:%d/__stela/developer"
              " (live session inspector; JSON at /__stela/developer.json)",
              host, port)
    if strict:
        _log.info("strict mode ON — STELA failure 返回 500（不降级到 passthrough）")
    web.run_app(app, host=host, port=port, print=None, access_log=None)
