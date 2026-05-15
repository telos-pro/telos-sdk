"""端到端 proxy 测试：起一个 mock upstream，跑真实代理，断 wire 内容。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from stela.proxy.server import make_app


# ---------------------------------------------------------------------------
# mock upstream（充当 api.anthropic.com）
# ---------------------------------------------------------------------------

class _MockUpstream:
    """记录收到的 wire 请求；返回固定 JSON 或 SSE 流。"""

    def __init__(self, *, sse: bool = False) -> None:
        self.last_body: dict[str, Any] | None = None
        self.last_headers: dict[str, str] | None = None
        self.sse = sse

    async def handler(self, request: web.Request) -> web.StreamResponse:
        self.last_body = await request.json()
        self.last_headers = dict(request.headers)
        if self.sse:
            response = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await response.prepare(request)
            chunks = [
                b'event: message_start\n'
                b'data: {"type":"message_start","message":{"id":"msg_x","usage":'
                b'{"input_tokens":120,"cache_read_input_tokens":1000,'
                b'"cache_creation_input_tokens":0,"output_tokens":1}}}\n\n',
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":0,"delta":'
                b'{"type":"text_delta","text":"Hello"}}\n\n',
                b'event: message_delta\n'
                b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
                b'"usage":{"output_tokens":42}}\n\n',
                b'event: message_stop\n'
                b'data: {"type":"message_stop"}\n\n',
            ]
            for c in chunks:
                await response.write(c)
            await response.write_eof()
            return response

        return web.json_response({
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "model": "claude-opus-4-7",
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 120,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 0,
                "output_tokens": 5,
            },
        })


# ---------------------------------------------------------------------------
# Fixture helpers（不引 pytest-aiohttp，直接手摆）
# ---------------------------------------------------------------------------

async def _start_upstream(mock: _MockUpstream) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_post("/v1/messages", mock.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


async def _start_proxy(upstream_url: str, *, usage_log: Path | None = None) -> tuple[web.AppRunner, str]:
    app = make_app(upstream=upstream_url, usage_log=usage_log)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def _test_non_streaming() -> None:
    mock = _MockUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)

    try:
        async with aiohttp.ClientSession() as client:
            req_body = {
                "model": "claude-opus-4-7",
                "max_tokens": 256,
                "tools": [
                    {"name": "Read", "input_schema": {"type": "object",
                        "properties": {"path": {"type": "string"}}}},
                ],
                "system": [{"type": "text", "text": "You are an agent."}],
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "Hi"}]},
                ],
                "stream": False,
            }
            async with client.post(
                f"{px_url}/v1/messages",
                json=req_body,
                headers={
                    "x-api-key": "test-key",
                    "anthropic-version": "2023-06-01",
                    "x-stela-session": "smoke-non-stream",
                },
            ) as resp:
                assert resp.status == 200, await resp.text()
                body = await resp.json()
                assert body["id"] == "msg_test"
                assert body["usage"]["cache_read_input_tokens"] == 1000

        # ---- 验证 upstream 拿到的是 STELA 处理过的 wire ----
        wire = mock.last_body
        assert wire is not None
        # tools 段或 system 段至少有一处出现 cache_control（BP-T 或 BP-S）
        all_blocks = list(wire.get("tools") or []) + list(wire.get("system") or [])
        assert any("cache_control" in b for b in all_blocks), \
            f"upstream 拿到的 wire 没有 cache_control: {wire}"
        # 认证 header 应被透传
        assert mock.last_headers.get("x-api-key") == "test-key"
        assert mock.last_headers.get("anthropic-version") == "2023-06-01"
        print("✓ test_non_streaming")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_streaming_sse() -> None:
    mock = _MockUpstream(sse=True)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)

    try:
        async with aiohttp.ClientSession() as client:
            req_body = {
                "model": "claude-opus-4-7",
                "max_tokens": 256,
                "system": [{"type": "text", "text": "agent"}],
                "messages": [{"role": "user",
                              "content": [{"type": "text", "text": "Hi"}]}],
                "stream": True,
            }
            async with client.post(
                f"{px_url}/v1/messages",
                json=req_body,
                headers={"x-api-key": "test-key",
                         "anthropic-version": "2023-06-01"},
            ) as resp:
                assert resp.status == 200
                # SSE: 累积 chunks
                received = b""
                async for chunk in resp.content.iter_any():
                    received += chunk
                assert b"message_start" in received
                assert b"message_delta" in received
                assert b'"output_tokens":42' in received
        print("✓ test_streaming_sse")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_usage_log_written(tmp_log: Path) -> None:
    mock = _MockUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url, usage_log=tmp_log)

    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages",
                json={
                    "model": "claude-opus-4-7",
                    "max_tokens": 64,
                    "system": [{"type": "text", "text": "agent"}],
                    "messages": [{"role": "user",
                                  "content": [{"type": "text", "text": "x"}]}],
                },
                headers={"x-api-key": "k", "anthropic-version": "2023-06-01",
                         "x-stela-session": "log-test"},
            ) as resp:
                assert resp.status == 200

        line = tmp_log.read_text().strip().splitlines()[-1]
        record = json.loads(line)
        assert record["session_id"] == "log-test"
        assert record["harness"] in ("openclaw", "hermes")
        assert record["normalized"]["cache_read"] == 1000
        assert record["normalized"]["output"] == 5
        print("✓ test_usage_log_written")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_pipeline_error_returns_anthropic_error() -> None:
    """非 JSON body 必须按 Anthropic 错误 schema 返回 400。"""
    mock = _MockUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)

    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages",
                data=b"not json",
                headers={"content-type": "application/json"},
            ) as resp:
                assert resp.status == 400
                body = await resp.json()
                assert body["type"] == "error"
                assert body["error"]["type"] == "invalid_request_error"
        print("✓ test_pipeline_error_returns_anthropic_error")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_pipeline_failure_falls_back_to_passthrough() -> None:
    """STELA 失败时（默认非 strict），原 raw 透传到 upstream，client 看到正常响应。

    构造方法：发一个 model 字段缺失但 content 合法的请求 —— harness 能解析，
    但我们 monkey-patch process_anthropic_request 让它抛异常，模拟 STELA bug。
    """
    from stela.proxy import server as srv_mod

    mock = _MockUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)

    # monkey-patch：让管线必抛
    original = srv_mod.process_anthropic_request

    def boom(*args, **kwargs):
        raise RuntimeError("simulated STELA bug")

    srv_mod.process_anthropic_request = boom
    try:
        async with aiohttp.ClientSession() as client:
            req_body = {
                "model": "claude-opus-4-7",
                "max_tokens": 16,
                "system": [{"type": "text", "text": "agent"}],
                "messages": [{"role": "user",
                              "content": [{"type": "text", "text": "x"}]}],
            }
            async with client.post(
                f"{px_url}/v1/messages",
                json=req_body,
                headers={"x-api-key": "k", "anthropic-version": "2023-06-01"},
            ) as resp:
                # passthrough 模式：仍然 200，body 来自 upstream
                assert resp.status == 200, await resp.text()
                body = await resp.json()
                assert body["id"] == "msg_test"

        # upstream 收到的应当是 ORIGINAL raw（无 cache_control 改写）
        wire = mock.last_body
        all_blocks = list(wire.get("tools") or []) + list(wire.get("system") or [])
        # 不应该出现 cache_control（因为没跑 STELA）
        assert not any("cache_control" in b for b in all_blocks), \
            "passthrough 不应有 cache_control 改写"
        print("✓ test_pipeline_failure_falls_back_to_passthrough")
    finally:
        srv_mod.process_anthropic_request = original
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_strict_mode_returns_500_on_pipeline_failure() -> None:
    """strict=True 时 STELA 失败必须返 500（不降级）。"""
    from stela.proxy import server as srv_mod
    from stela.proxy.server import make_app

    mock = _MockUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)

    # 手动起 strict app
    app = make_app(upstream=up_url, strict=True)
    px_runner = web.AppRunner(app)
    await px_runner.setup()
    px_site = web.TCPSite(px_runner, "127.0.0.1", 0)
    await px_site.start()
    px_port = px_site._server.sockets[0].getsockname()[1]
    px_url = f"http://127.0.0.1:{px_port}"

    original = srv_mod.process_anthropic_request

    def boom(*args, **kwargs):
        raise RuntimeError("simulated")

    srv_mod.process_anthropic_request = boom
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages",
                json={"model": "x", "max_tokens": 1, "messages": []},
            ) as resp:
                assert resp.status == 500
                body = await resp.json()
                assert body["error"]["type"] == "api_error"
        print("✓ test_strict_mode_returns_500_on_pipeline_failure")
    finally:
        srv_mod.process_anthropic_request = original
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _run_all(tmp_log: Path) -> None:
    await _test_non_streaming()
    await _test_streaming_sse()
    await _test_usage_log_written(tmp_log)
    await _test_pipeline_error_returns_anthropic_error()
    await _test_pipeline_failure_falls_back_to_passthrough()
    await _test_strict_mode_returns_500_on_pipeline_failure()


def main() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp_log = Path(td) / "usage.jsonl"
        asyncio.run(_run_all(tmp_log))
    print("\nall proxy server tests passed.")


if __name__ == "__main__":
    main()
