"""``_derive_session_id`` stability regression.

Expected behavior:
- multiple turns of the same conversation (only appending to the tail of ``messages[]``) → the same session_id
- conversations with different ``messages[0]`` → different session_ids
- different ``system`` / ``tools`` configurations → different session_ids
- different ``x-api-key`` → different session_ids (multi-tenant isolation)
- an explicit ``x-telos-session`` header always overrides (highest priority)
"""

from __future__ import annotations

import asyncio

import aiohttp
from aiohttp import web

from telos.proxy.server import _derive_session_id, make_app


# ---------------------------------------------------------------------------
# pure-function unit tests
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
    """A growing conversation (appending assistant + user) must keep the same session_id."""
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
    """Same api-key, same system+tools, but different messages[0] → different session."""
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
    """``authorization: Bearer X`` and ``x-api-key: X`` should be recognized as the same client."""
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
# end-to-end: verify via the proxy server that session_id appears in the usage_log
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

        # both turns should have the same session_id
        lines = tmp_log.read_text().strip().splitlines()
        assert len(lines) >= 2
        sids = [json.loads(l)["session_id"] for l in lines]
        assert sids[0] == sids[1], \
            f"session_id drifted between turns: {sids[0]} != {sids[1]}"
        assert sids[0].startswith("telos-")
        print(f"✓ test_end_to_end_multi_turn_same_session_id ({sids[0]})")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_explicit_header_overrides_derivation() -> None:
    """The ``x-telos-session`` header must always win over the derivation algorithm."""
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
