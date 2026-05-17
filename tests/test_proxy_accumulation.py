"""端到端：多轮请求经过 proxy 后，``cumulative_cache_creation`` 真累积。

这是验证「中期目标完整实现」的关键测试。在修复前，proxy 每次新建
``BridgeSessionState`` → 任何累计字段都重置为 0；修复后，相同 session_id
的请求共享同一份 state，cumulative_cache_creation / real_requests 跨调
用单调递增。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import aiohttp
from aiohttp import web

from telos.proxy.server import make_app


class _CountingUpstream:
    """每次响应都汇报 cache_creation_input_tokens 累计的 mock upstream。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        # 模拟：第一次 cache_write=5000，第二次 1500，之后 0（已被缓存）
        self._sequence = [5000, 1500, 0, 0]

    async def handler(self, request: web.Request) -> web.Response:
        self.requests.append(await request.json())
        idx = len(self.requests) - 1
        cache_write = self._sequence[min(idx, len(self._sequence) - 1)]
        return web.json_response({
            "id": f"msg_{idx}", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "OK"}],
            "model": "claude-opus-4-7", "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 50,
                "cache_read_input_tokens": 6500 - cache_write,
                "cache_creation_input_tokens": cache_write,
                "output_tokens": 5,
            },
        })


def _multi_turn_req(turn: int) -> dict:
    """逐轮加长 messages —— 同一对话的多轮，messages[0] 不变。"""
    base_messages = [
        {"role": "user", "content": [{"type": "text",
            "text": "Turn 0 question."}]},
    ]
    extra = []
    for t in range(1, turn + 1):
        extra.extend([
            {"role": "assistant", "content": [{"type": "text",
                "text": f"reply {t}"}]},
            {"role": "user", "content": [{"type": "text",
                "text": f"Turn {t} question."}]},
        ])
    return {
        "model": "claude-opus-4-7",
        "max_tokens": 64,
        "system": [
            {"type": "text", "text": "You are an engineer agent."},
            {"type": "text", "text": "AUTH SPEC:\n" + ("规则细节…\n" * 400)},
        ],
        "tools": [
            {"name": "Bash", "input_schema": {"type": "object",
                "properties": {"cmd": {"type": "string"}}}},
        ],
        "messages": base_messages + extra,
    }


async def _test_cumulative_growth_through_proxy(tmp_log: Path) -> None:
    mock = _CountingUpstream()
    up_app = web.Application()
    up_app.router.add_post("/v1/messages", mock.handler)
    up_runner = web.AppRunner(up_app); await up_runner.setup()
    up_site = web.TCPSite(up_runner, "127.0.0.1", 0); await up_site.start()
    up_port = up_site._server.sockets[0].getsockname()[1]

    app = make_app(upstream=f"http://127.0.0.1:{up_port}", usage_log=tmp_log)
    px_runner = web.AppRunner(app); await px_runner.setup()
    px_site = web.TCPSite(px_runner, "127.0.0.1", 0); await px_site.start()
    px_port = px_site._server.sockets[0].getsockname()[1]

    try:
        async with aiohttp.ClientSession() as client:
            headers = {"x-api-key": "sk-cum", "anthropic-version": "2023-06-01"}
            for turn in range(4):
                async with client.post(
                    f"http://127.0.0.1:{px_port}/v1/messages",
                    json=_multi_turn_req(turn),
                    headers=headers,
                ) as r:
                    assert r.status == 200, await r.text()

        # 关键断言：日志里 cumulative.cache_creation 单调递增
        lines = [json.loads(l) for l in tmp_log.read_text().strip().splitlines()]
        assert len(lines) == 4
        cums = [rec["cumulative"]["cache_creation"] for rec in lines]
        # 期望：5000 → 6500 → 6500 → 6500
        assert cums == [5000, 6500, 6500, 6500], f"cumulative drift: {cums}"

        # 全部 4 轮应当是同一个 session_id（来自内容派生）
        sids = [rec["session_id"] for rec in lines]
        assert len(set(sids)) == 1, f"session_id 漂移：{sids}"

        # real_requests_since_refresh 也单调递增
        reqs = [rec["cumulative"]["real_requests_since_refresh"] for rec in lines]
        assert reqs == [1, 2, 3, 4], f"R8 计数漂移：{reqs}"

        # refpool slugs 至少有一个（大 system 文档被搬进 ref-pool）
        for rec in lines:
            assert rec["cumulative"]["refpool_slugs"], \
                f"ref-pool 应非空（call={rec['call_index']}）"
            # 所有轮共享同一个 slug 集合
        assert all(rec["cumulative"]["refpool_slugs"] == lines[0]["cumulative"]["refpool_slugs"]
                   for rec in lines), "ref-pool slugs 漂移"

        print(f"✓ test_cumulative_growth_through_proxy")
        print(f"  cache_creation: {cums}")
        print(f"  real_requests:  {reqs}")
        print(f"  refpool_slugs:  {lines[0]['cumulative']['refpool_slugs']}")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_different_sessions_dont_share_state(tmp_log: Path) -> None:
    """两个不同 api-key 的并发 client 必须有独立 state。"""
    mock = _CountingUpstream()
    up_app = web.Application()
    up_app.router.add_post("/v1/messages", mock.handler)
    up_runner = web.AppRunner(up_app); await up_runner.setup()
    up_site = web.TCPSite(up_runner, "127.0.0.1", 0); await up_site.start()
    up_port = up_site._server.sockets[0].getsockname()[1]

    app = make_app(upstream=f"http://127.0.0.1:{up_port}", usage_log=tmp_log)
    px_runner = web.AppRunner(app); await px_runner.setup()
    px_site = web.TCPSite(px_runner, "127.0.0.1", 0); await px_site.start()
    px_port = px_site._server.sockets[0].getsockname()[1]

    try:
        async with aiohttp.ClientSession() as client:
            for api_key in ("sk-alice", "sk-bob"):
                async with client.post(
                    f"http://127.0.0.1:{px_port}/v1/messages",
                    json=_multi_turn_req(0),
                    headers={"x-api-key": api_key,
                             "anthropic-version": "2023-06-01"},
                ) as r:
                    assert r.status == 200

        lines = [json.loads(l) for l in tmp_log.read_text().strip().splitlines()]
        # 两个 session_id 必须不同
        assert lines[0]["session_id"] != lines[1]["session_id"]
        # 每个都是这个 session 的第 1 次请求
        assert lines[0]["cumulative"]["real_requests_since_refresh"] == 1
        assert lines[1]["cumulative"]["real_requests_since_refresh"] == 1
        print("✓ test_different_sessions_dont_share_state")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        log1 = Path(td) / "growth.jsonl"
        asyncio.run(_test_cumulative_growth_through_proxy(log1))
    with tempfile.TemporaryDirectory() as td:
        log2 = Path(td) / "iso.jsonl"
        asyncio.run(_test_different_sessions_dont_share_state(log2))
    print("\nall proxy accumulation tests passed.")


if __name__ == "__main__":
    main()
