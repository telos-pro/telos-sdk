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
import json
import logging
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiohttp
from aiohttp import web

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
# usage 归一化（与 stela_anthropic_transport._normalize_usage 同 schema）
# ---------------------------------------------------------------------------

def _normalize_usage(u: dict[str, Any]) -> dict[str, int]:
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
    ):
        self.upstream = upstream.rstrip("/")
        self.usage_log = usage_log
        self.harness_override = harness_override
        self.request_timeout = request_timeout

        if usage_log is not None:
            usage_log.parent.mkdir(parents=True, exist_ok=True)

        self._session: aiohttp.ClientSession | None = None
        self._call_count = 0

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
            or raw.get("metadata", {}).get("user_id")
            or str(uuid4())
        )

        # ---- 1. STELA 管线 ----
        try:
            result = process_anthropic_request(
                raw,
                session_id=session_id,
                harness_name=self.harness_override,
            )
        except Exception as e:  # noqa: BLE001
            _log.exception("STELA pipeline failed (call=%d)", call_index)
            return _anthropic_error(500, "api_error",
                                    f"STELA pipeline failed: {e}")

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
                request, upstream, session_id, result, call_index, t0,
            )
        return await self._buffered_response(
            upstream, session_id, result, call_index, t0,
        )

    # ------------------------------------------------------------------
    # 非流式路径
    # ------------------------------------------------------------------

    async def _buffered_response(
        self,
        upstream: aiohttp.ClientResponse,
        session_id: str,
        result: PipelineResult,
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

        self._log_usage(
            session_id, result, usage,
            latency_s=time.time() - t0,
            streaming=False,
            status=status,
            call_index=call_index,
        )
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
                session_id, result, {},
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

            await downstream.write_eof()
        finally:
            upstream.release()

        self._log_usage(
            session_id, result, usage_aggregate,
            latency_s=time.time() - t0,
            streaming=True,
            status=200,
            call_index=call_index,
        )
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
        elif event == "message_delta":
            u = data.get("usage") or {}
            if "output_tokens" in u:
                usage["output_tokens"] = int(u["output_tokens"])

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

    def _log_usage(
        self,
        session_id: str,
        result: PipelineResult,
        usage: dict[str, Any],
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
                    "session_id": session_id,
                    "call_index": call_index,
                    "harness": result.harness,
                    "n_slots": len(result.plan_slots),
                    "slots": result.plan_slots,
                    "latency_s": round(latency_s, 3),
                    "streaming": streaming,
                    "status": status,
                    "raw_usage": usage,
                    "normalized": _normalize_usage(usage),
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
) -> web.Application:
    """构造一个完整的 aiohttp 应用。可被测试 / ASGI 嵌入复用。"""
    proxy = ProxyApp(
        upstream=upstream,
        usage_log=usage_log,
        harness_override=harness_override,
    )
    app = web.Application()
    app.router.add_post("/v1/messages", proxy.handle_messages)
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
    )
    _log.info("STELA proxy listening on http://%s:%d → %s", host, port, upstream)
    if usage_log:
        _log.info("usage log → %s", usage_log)
    web.run_app(app, host=host, port=port, print=None, access_log=None)
