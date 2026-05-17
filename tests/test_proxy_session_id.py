"""``_derive_session_id`` 稳定性回归。

期望行为：
- 同一对话的多轮（只在 ``messages[]`` 尾部追加）→ 同一 session_id
- 不同 ``messages[0]`` 的对话 → 不同 session_id
- 不同 ``system`` / ``tools`` 配置 → 不同 session_id
- 不同 ``x-api-key`` → 不同 session_id（多租户隔离）
- 显式 ``x-telos-session`` header 永远覆盖（最高优先级）
"""

from __future__ import annotations

import asyncio

import aiohttp
from aiohttp import web

from telos.proxy.server import _derive_session_id, make_app


# ---------------------------------------------------------------------------
# 纯函数单测
# ---------------------------------------------------------------------------

_BASE_REQ = {
    "model": "claude-opus-4-7",
    "max_tokens": 64,
    "system": [{"type": "text", "text": "You are an agent."}],
    "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "Turn 1 query"}]},
    ],
}
_HDR = {"x-api-key": "sk-ant-test"}


def test_stable_across_turns() -> None:
    """conversation 增长（assistant + user 追加）必须保持同一 session_id。"""
    turn1 = dict(_BASE_REQ)
    turn2 = dict(_BASE_REQ, messages=[
        _BASE_REQ["messages"][0],
        {"role": "assistant", "content": [{"type": "text", "text": "reply 1"}]},
        {"role": "user", "content": [{"type": "text", "text": "Turn 2 query"}]},
    ])
    turn3 = dict(_BASE_REQ, messages=turn2["messages"] + [
        {"role": "assistant", "content": [{"type": "text", "text": "reply 2"}]},
        {"role": "user", "content": [{"type": "text", "text": "Turn 3 query"}]},
    ])
    sid1 = _derive_session_id(turn1, _HDR)
    sid2 = _derive_session_id(turn2, _HDR)
    sid3 = _derive_session_id(turn3, _HDR)
    assert sid1 == sid2 == sid3, f"drift: {sid1} / {sid2} / {sid3}"
    print(f"✓ test_stable_across_turns ({sid1})")


def test_different_first_message_different_session() -> None:
    """同 api-key、同 system+tools，但 messages[0] 不同 → 不同 session。"""
    req_a = dict(_BASE_REQ)
    req_b = dict(_BASE_REQ, messages=[
        {"role": "user", "content": [{"type": "text", "text": "DIFFERENT initial"}]},
    ])
    assert _derive_session_id(req_a, _HDR) != _derive_session_id(req_b, _HDR)
    print("✓ test_different_first_message_different_session")


def test_different_api_key_different_session() -> None:
    sid_a = _derive_session_id(_BASE_REQ, {"x-api-key": "sk-A"})
    sid_b = _derive_session_id(_BASE_REQ, {"x-api-key": "sk-B"})
    assert sid_a != sid_b
    print("✓ test_different_api_key_different_session")


def test_different_system_different_session() -> None:
    req2 = dict(_BASE_REQ, system=[{"type": "text", "text": "DIFFERENT system"}])
    assert _derive_session_id(_BASE_REQ, _HDR) != _derive_session_id(req2, _HDR)
    print("✓ test_different_system_different_session")


def test_different_tools_different_session() -> None:
    req2 = dict(_BASE_REQ, tools=[
        {"name": "OtherTool", "input_schema": {"type": "object"}},
    ])
    assert _derive_session_id(_BASE_REQ, _HDR) != _derive_session_id(req2, _HDR)
    print("✓ test_different_tools_different_session")


def test_bearer_auth_normalized() -> None:
    """``authorization: Bearer X`` 与 ``x-api-key: X`` 应被识别为同 client。"""
    sid_a = _derive_session_id(_BASE_REQ, {"x-api-key": "sk-same"})
    sid_b = _derive_session_id(_BASE_REQ, {"authorization": "Bearer sk-same"})
    assert sid_a == sid_b, f"{sid_a} != {sid_b}"
    print("✓ test_bearer_auth_normalized")


def test_empty_messages_does_not_crash() -> None:
    req = dict(_BASE_REQ, messages=[])
    sid = _derive_session_id(req, _HDR)
    assert sid.startswith("telos-")
    print("✓ test_empty_messages_does_not_crash")


# ---------------------------------------------------------------------------
# 端到端：通过 proxy server 验证 session_id 出现在 usage_log 里
# ---------------------------------------------------------------------------

class _CapturingUpstream:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def handler(self, request: web.Request) -> web.Response:
        self.requests.append(await request.json())
        return web.json_response({
            "id": "msg_t", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "model": "claude-opus-4-7", "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0, "output_tokens": 1},
        })


async def _test_end_to_end_multi_turn_same_session_id(tmp_log) -> None:
    import json

    mock = _CapturingUpstream()
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
            headers = {"x-api-key": "sk-e2e", "anthropic-version": "2023-06-01"}
            base = dict(_BASE_REQ)

            # turn 1
            async with client.post(f"http://127.0.0.1:{px_port}/v1/messages",
                                    json=base, headers=headers) as r:
                assert r.status == 200

            # turn 2: append assistant + user
            turn2 = dict(base, messages=base["messages"] + [
                {"role": "assistant", "content": [{"type": "text", "text": "ack"}]},
                {"role": "user", "content": [{"type": "text", "text": "follow up"}]},
            ])
            async with client.post(f"http://127.0.0.1:{px_port}/v1/messages",
                                    json=turn2, headers=headers) as r:
                assert r.status == 200

        # 两轮都该有同 session_id
        lines = tmp_log.read_text().strip().splitlines()
        assert len(lines) >= 2
        sids = [json.loads(l)["session_id"] for l in lines]
        assert sids[0] == sids[1], \
            f"两轮 session_id 漂移：{sids[0]} != {sids[1]}"
        assert sids[0].startswith("telos-")
        print(f"✓ test_end_to_end_multi_turn_same_session_id ({sids[0]})")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_explicit_header_overrides_derivation() -> None:
    """``x-telos-session`` header 必须永远胜过派生算法。"""
    import json
    import tempfile
    from pathlib import Path

    log_path = Path(tempfile.mkdtemp()) / "u.jsonl"

    mock = _CapturingUpstream()
    up_app = web.Application()
    up_app.router.add_post("/v1/messages", mock.handler)
    up_runner = web.AppRunner(up_app); await up_runner.setup()
    up_site = web.TCPSite(up_runner, "127.0.0.1", 0); await up_site.start()
    up_port = up_site._server.sockets[0].getsockname()[1]

    app = make_app(upstream=f"http://127.0.0.1:{up_port}", usage_log=log_path)
    px_runner = web.AppRunner(app); await px_runner.setup()
    px_site = web.TCPSite(px_runner, "127.0.0.1", 0); await px_site.start()
    px_port = px_site._server.sockets[0].getsockname()[1]

    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"http://127.0.0.1:{px_port}/v1/messages",
                json=_BASE_REQ,
                headers={"x-api-key": "k", "anthropic-version": "2023-06-01",
                         "x-telos-session": "my-custom-session"},
            ) as r:
                assert r.status == 200

        record = json.loads(log_path.read_text().strip().splitlines()[-1])
        assert record["session_id"] == "my-custom-session"
        print("✓ test_explicit_header_overrides_derivation")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


def main() -> None:
    import tempfile
    from pathlib import Path

    test_stable_across_turns()
    test_different_first_message_different_session()
    test_different_api_key_different_session()
    test_different_system_different_session()
    test_different_tools_different_session()
    test_bearer_auth_normalized()
    test_empty_messages_does_not_crash()

    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "u.jsonl"
        asyncio.run(_test_end_to_end_multi_turn_same_session_id(log))
    asyncio.run(_test_explicit_header_overrides_derivation())
    print("\nall session-id tests passed.")


if __name__ == "__main__":
    main()
