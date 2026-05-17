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


class _FlakyUpstream:
    """前 ``fail_first`` 个连接收到请求后直接关闭（不返回响应），

    其余正常返回 JSON。客户端侧表现为 ``ServerDisconnectedError`` —— 模拟
    api.anthropic.com 建连/早期阶段的瞬时抖动。
    """

    def __init__(self, *, fail_first: int) -> None:
        self.fail_first = fail_first
        self.requests = 0
        self.served = 0

    async def handler(self, request: web.Request) -> web.StreamResponse:
        self.requests += 1
        if self.requests <= self.fail_first:
            # 收到请求后直接关闭连接，不发任何响应。
            request.transport.close()
            raise asyncio.CancelledError()
        self.served += 1
        return web.json_response({
            "id": "msg_flaky",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "model": "claude-opus-4-7",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0, "output_tokens": 1},
        })


def _req_body() -> dict[str, Any]:
    return {
        "model": "claude-opus-4-7",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hi"}]}],
        "stream": False,
    }


async def _test_retries_transient_connect_failure() -> None:
    """前 2 次建连瞬时失败 → proxy 自己退避重试，客户端最终拿到 200。"""
    mock = _FlakyUpstream(fail_first=2)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages", json=_req_body(),
                headers={"x-api-key": "k", "x-stela-session": "retry-ok"},
            ) as resp:
                assert resp.status == 200, await resp.text()
                body = await resp.json()
                assert body["id"] == "msg_flaky"
        # upstream 共收到 3 次（2 次失败 + 1 次成功），只成功服务 1 次。
        assert mock.requests == 3, mock.requests
        assert mock.served == 1, mock.served
        print("✓ test_retries_transient_connect_failure")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_502_after_exhausting_retries() -> None:
    """建连一直失败 → 重试耗尽后 proxy 回 502（anthropic error 结构）。"""
    mock = _FlakyUpstream(fail_first=999)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages", json=_req_body(),
                headers={"x-api-key": "k", "x-stela-session": "retry-exhaust"},
            ) as resp:
                assert resp.status == 502, await resp.text()
                body = await resp.json()
                assert body["error"]["type"] == "api_error"
        # 1 次初始 + 3 次重试 = 4 次尝试。
        assert mock.requests == 4, mock.requests
        print("✓ test_502_after_exhausting_retries")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


def test_wire_tool_result_first_helper() -> None:
    """_wire_tool_result_first：tool_result 居首 → True，殿后 → False。"""
    from stela.proxy.server import _wire_tool_result_first
    ok = {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "r"},
        {"type": "text", "text": "go"}]}]}
    bad = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "go"},
        {"type": "tool_result", "tool_use_id": "t", "content": "r"}]}]}
    assert _wire_tool_result_first(ok) is True
    assert _wire_tool_result_first(bad) is False
    # 纯文本 / 纯 tool_result / 字符串 content 都算合法
    assert _wire_tool_result_first({"messages": [
        {"role": "user", "content": "hi"}]}) is True
    print("✓ test_wire_tool_result_first_helper")


async def _test_invalid_wire_falls_back_to_passthrough() -> None:
    """STELA 产出 tool_result 殿后的非法 wire → proxy 退回 passthrough。"""
    from stela.proxy import server as srv_mod

    mock = _MockUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)

    real = srv_mod.process_anthropic_request

    def bad_wire(raw, **kw):
        # 模拟 STELA bug：把每条 user message 的 tool_result 排到最后
        res = real(raw, **kw)
        for m in res.wire.get("messages", []):
            c = m.get("content")
            if m.get("role") == "user" and isinstance(c, list):
                tr = [b for b in c if isinstance(b, dict)
                      and b.get("type") == "tool_result"]
                rest = [b for b in c if not (isinstance(b, dict)
                        and b.get("type") == "tool_result")]
                if tr and rest:
                    m["content"] = rest + tr
        return res

    srv_mod.process_anthropic_request = bad_wire
    try:
        req = {
            "model": "claude-opus-4-7", "max_tokens": 16, "stream": False,
            "system": [{"type": "text", "text": "agent"}],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "do it"}]},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tu1", "name": "Bash",
                     "input": {"command": "ls"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu1",
                     "content": "output"},
                    {"type": "text", "text": "continue"}]},
            ],
        }
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages", json=req,
                headers={"x-api-key": "k", "x-stela-session": "bad-wire"},
            ) as resp:
                assert resp.status == 200, await resp.text()
        # upstream 收到的是退回 passthrough 的请求 —— tool_result 仍居首
        last = mock.last_body["messages"][-1]["content"]
        assert last[0]["type"] == "tool_result", last
        print("✓ test_invalid_wire_falls_back_to_passthrough")
    finally:
        srv_mod.process_anthropic_request = real
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _run_all(tmp_log: Path) -> None:
    test_wire_tool_result_first_helper()
    await _test_non_streaming()
    await _test_streaming_sse()
    await _test_usage_log_written(tmp_log)
    await _test_pipeline_error_returns_anthropic_error()
    await _test_pipeline_failure_falls_back_to_passthrough()
    await _test_strict_mode_returns_500_on_pipeline_failure()
    await _test_retries_transient_connect_failure()
    await _test_502_after_exhausting_retries()
    await _test_invalid_wire_falls_back_to_passthrough()


def main() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp_log = Path(td) / "usage.jsonl"
        asyncio.run(_run_all(tmp_log))
    print("\nall proxy server tests passed.")


if __name__ == "__main__":
    main()
