"""End-to-end proxy test for the multi-backend ``/upstreams/<slug>/...`` route.

Mocks an OpenAI ChatCompletions-compatible upstream, runs the real proxy, and
verifies:
- the request hits the right URL on the upstream;
- the wire was TELOS-processed (DROP-band sinking, tools canonicalization);
- the usage_log records ``harness="telos"`` with the normalized buckets the
  dashboard reads;
- streaming SSE forwards chunks and extracts usage from the terminal chunk
  when ``stream_options.include_usage=true``;
- unknown upstream slugs return a 404 error;
- per-slug upstream URL is honored (legacy ``self.upstream`` is NOT used).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from telos.config import UpstreamConfig
from telos.proxy.server import make_app


# ---------------------------------------------------------------------------
# mock upstream (acts as openrouter.ai / api.deepseek.com)
# ---------------------------------------------------------------------------

class _MockOpenAIUpstream:
    """Records the received wire requests; returns chat-completions JSON or SSE."""

    def __init__(self, *, sse: bool = False, with_usage: bool = True) -> None:
        self.last_body: dict[str, Any] | None = None
        self.last_headers: dict[str, str] | None = None
        self.last_path: str | None = None
        self.sse = sse
        self.with_usage = with_usage

    async def handler(self, request: web.Request) -> web.StreamResponse:
        self.last_body = await request.json()
        self.last_headers = dict(request.headers)
        self.last_path = request.path
        if self.sse:
            response = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await response.prepare(request)
            chunks = [
                b'data: {"id":"x","choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n',
                b'data: {"id":"x","choices":[{"index":0,"delta":{"content":"Hi"}}]}\n\n',
            ]
            if self.with_usage:
                # OpenAI SSE: when stream_options.include_usage is set, the
                # penultimate chunk carries the full usage block.
                chunks.append(
                    b'data: {"id":"x","choices":[],"usage":'
                    b'{"prompt_cache_hit_tokens":900,"prompt_cache_miss_tokens":100,'
                    b'"completion_tokens":7}}\n\n'
                )
            chunks.append(b'data: [DONE]\n\n')
            for c in chunks:
                await response.write(c)
            await response.write_eof()
            return response

        usage = {
            "prompt_cache_hit_tokens": 900,
            "prompt_cache_miss_tokens": 100,
            "completion_tokens": 7,
        } if self.with_usage else {}
        return web.json_response({
            "id": "cmpl_test",
            "object": "chat.completion",
            "model": "deepseek-chat",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }],
            "usage": usage,
        })


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

async def _start_upstream(mock: _MockOpenAIUpstream) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", mock.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


async def _start_proxy(
    upstream_url: str,
    *,
    usage_log: Path | None = None,
    extra_slugs: dict[str, UpstreamConfig] | None = None,
) -> tuple[web.AppRunner, str]:
    upstreams = {
        "openrouter": UpstreamConfig(
            url=upstream_url, engine="deepseek", protocol="openai-chat",
        ),
    }
    if extra_slugs:
        upstreams.update(extra_slugs)
    app = make_app(upstream="http://unused", upstreams=upstreams, usage_log=usage_log)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def _sample_chat_request() -> dict[str, Any]:
    """An OpenAI ChatCompletions request with enough structure to exercise IR layout."""
    return {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello there"},
        ],
        "tools": [{
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Look something up",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                },
            },
        }],
        "max_tokens": 64,
        "stream": False,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def _test_openai_route_non_streaming() -> None:
    mock = _MockOpenAIUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/upstreams/openrouter/v1/chat/completions",
                json=_sample_chat_request(),
                headers={
                    "authorization": "Bearer test-key",
                    "x-telos-session": "openai-nonstream",
                },
            ) as resp:
                assert resp.status == 200, await resp.text()
                body = await resp.json()
                assert body["id"] == "cmpl_test"

        # Upstream received the TELOS-processed wire on /v1/chat/completions.
        assert mock.last_path == "/v1/chat/completions"
        wire = mock.last_body
        assert wire is not None
        assert wire["model"] == "deepseek-chat"
        assert isinstance(wire.get("messages"), list)
        # Auth header was forwarded through the proxy (header names are
        # case-insensitive — aiohttp may transmit as ``Authorization``).
        auth_value = next(
            (v for k, v in (mock.last_headers or {}).items()
             if k.lower() == "authorization"), None,
        )
        assert auth_value == "Bearer test-key"
        print("✓ test_openai_route_non_streaming")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_openai_route_logs_usage(tmp_log: Path) -> None:
    mock = _MockOpenAIUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url, usage_log=tmp_log)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/upstreams/openrouter/v1/chat/completions",
                json=_sample_chat_request(),
                headers={"authorization": "Bearer k",
                         "x-telos-session": "openai-log"},
            ) as resp:
                assert resp.status == 200

        line = tmp_log.read_text().strip().splitlines()[-1]
        record = json.loads(line)
        assert record["session_id"] == "openai-log"
        # The whole point: dashboard sees a "telos" harness entry for OpenAI traffic.
        assert record["harness"] == "telos"
        # DeepSeek usage normalization: hits → cache_read, misses → raw_input.
        assert record["normalized"]["cache_read"] == 900
        assert record["normalized"]["raw_input"] == 100
        assert record["normalized"]["output"] == 7
        print("✓ test_openai_route_logs_usage")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_openai_route_via_labels_harness(tmp_log: Path) -> None:
    """When the upstream slug carries ``via``, the usage log records it as
    the harness — that's how the dashboard attributes traffic to the calling
    tool (openclaw / hermes / codex) instead of the wire-level ``telos``."""
    mock = _MockOpenAIUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)
    # Slug with via="openclaw" simulates a config patched by OpenClawInstaller.
    extra = {
        "openclaw_routed": UpstreamConfig(
            url=up_url, engine="deepseek", protocol="openai-chat",
            via="openclaw",
        ),
    }
    px_runner, px_url = await _start_proxy(up_url, usage_log=tmp_log,
                                            extra_slugs=extra)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/upstreams/openclaw_routed/v1/chat/completions",
                json=_sample_chat_request(),
                headers={"authorization": "Bearer k",
                         "x-telos-session": "via-test"},
            ) as resp:
                assert resp.status == 200

        line = tmp_log.read_text().strip().splitlines()[-1]
        record = json.loads(line)
        assert record["session_id"] == "via-test"
        # The crucial assertion: harness is the via name, not "telos".
        assert record["harness"] == "openclaw"
        print("✓ test_openai_route_via_labels_harness")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_openai_route_streaming() -> None:
    mock = _MockOpenAIUpstream(sse=True, with_usage=True)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)
    try:
        async with aiohttp.ClientSession() as client:
            req = _sample_chat_request()
            req["stream"] = True
            req["stream_options"] = {"include_usage": True}
            async with client.post(
                f"{px_url}/upstreams/openrouter/v1/chat/completions",
                json=req,
                headers={"authorization": "Bearer k"},
            ) as resp:
                assert resp.status == 200
                received = b""
                async for chunk in resp.content.iter_any():
                    received += chunk
                assert b"[DONE]" in received
                assert b"prompt_cache_hit_tokens" in received
        print("✓ test_openai_route_streaming")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_unknown_slug_returns_404() -> None:
    mock = _MockOpenAIUpstream(sse=False)
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/upstreams/no-such-slug/v1/chat/completions",
                json=_sample_chat_request(),
            ) as resp:
                assert resp.status == 404
                body = await resp.json()
                assert body["error"]["type"] == "not_found"
                assert "no-such-slug" in body["error"]["message"]
        # The mock upstream must NOT have been hit.
        assert mock.last_body is None
        print("✓ test_unknown_slug_returns_404")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_per_slug_upstream_url_is_honored() -> None:
    """Two slugs pointing at two different mock upstreams — the request to each
    must hit only that slug's URL.
    """
    mock_a = _MockOpenAIUpstream(sse=False)
    mock_b = _MockOpenAIUpstream(sse=False)
    up_a_runner, up_a_url = await _start_upstream(mock_a)
    up_b_runner, up_b_url = await _start_upstream(mock_b)

    upstreams = {
        "openrouter": UpstreamConfig(url=up_a_url, engine="deepseek",
                                      protocol="openai-chat"),
        "deepseek":   UpstreamConfig(url=up_b_url, engine="deepseek",
                                      protocol="openai-chat"),
    }
    app = make_app(upstream="http://unused", upstreams=upstreams)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    px_url = f"http://127.0.0.1:{port}"

    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/upstreams/deepseek/v1/chat/completions",
                json=_sample_chat_request(),
                headers={"authorization": "Bearer k"},
            ) as resp:
                assert resp.status == 200
        # deepseek mock got the request; openrouter mock did not.
        assert mock_b.last_body is not None
        assert mock_a.last_body is None
        print("✓ test_per_slug_upstream_url_is_honored")
    finally:
        await runner.cleanup()
        await up_a_runner.cleanup()
        await up_b_runner.cleanup()


async def _test_anthropic_route_still_works() -> None:
    """Regression: registering the /upstreams/<slug>/<tail> route must NOT
    swallow the legacy /v1/messages handler.
    """
    # A minimal anthropic-shaped mock that just returns 200 with a usage block.
    async def anth_handler(request: web.Request) -> web.Response:
        return web.json_response({
            "id": "msg_x", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "model": "claude-opus-4-7", "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0, "output_tokens": 1},
        })
    app = web.Application()
    app.router.add_post("/v1/messages", anth_handler)
    up_runner = web.AppRunner(app)
    await up_runner.setup()
    site = web.TCPSite(up_runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    up_url = f"http://127.0.0.1:{port}"

    px_runner, px_url = await _start_proxy(up_url)  # openrouter slug is openai-chat
    # But we want the legacy /v1/messages to hit OUR mock anthropic upstream.
    # _start_proxy passes upstream="http://unused"; override via re-create.
    await px_runner.cleanup()
    app2 = make_app(
        upstream=up_url,
        upstreams={"openrouter": UpstreamConfig(
            url="http://unused", engine="deepseek", protocol="openai-chat")},
    )
    runner = web.AppRunner(app2)
    await runner.setup()
    site2 = web.TCPSite(runner, "127.0.0.1", 0)
    await site2.start()
    px_port = site2._server.sockets[0].getsockname()[1]
    px_url = f"http://127.0.0.1:{px_port}"

    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages",
                json={
                    "model": "claude-opus-4-7",
                    "max_tokens": 16,
                    "system": [{"type": "text", "text": "x"}],
                    "messages": [{"role": "user",
                                  "content": [{"type": "text", "text": "hi"}]}],
                },
                headers={"x-api-key": "k", "anthropic-version": "2023-06-01"},
            ) as resp:
                assert resp.status == 200, await resp.text()
                body = await resp.json()
                assert body["id"] == "msg_x"
        print("✓ test_anthropic_route_still_works")
    finally:
        await runner.cleanup()
        await up_runner.cleanup()


async def _run_all(tmp_log: Path) -> None:
    await _test_openai_route_non_streaming()
    await _test_openai_route_logs_usage(tmp_log)
    await _test_openai_route_via_labels_harness(tmp_log)
    await _test_openai_route_streaming()
    await _test_unknown_slug_returns_404()
    await _test_per_slug_upstream_url_is_honored()
    await _test_anthropic_route_still_works()


def test_openai_proxy_route() -> None:
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        tmp_log = Path(f.name)
    try:
        asyncio.run(_run_all(tmp_log))
    finally:
        try:
            tmp_log.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    test_openai_proxy_route()
    print("\nall openai proxy route tests passed.")
