"""端到端 proxy 测试：mode 开关 + RTK 过滤 + compare_group 落盘。

起一个 mock upstream，跑真实代理，断言 wire 内容 / usage_log 记录。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from stela.corpus import load_session
from stela.output_filter import StelaMode
from stela.proxy.server import make_app


class _MockUpstream:
    """记录收到的 wire 请求，返回固定 JSON。"""

    def __init__(self) -> None:
        self.last_body: dict[str, Any] | None = None

    async def handler(self, request: web.Request) -> web.StreamResponse:
        self.last_body = await request.json()
        return web.json_response({
            "id": "msg_test", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "model": "claude-opus-4-7", "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 500,
                      "cache_creation_input_tokens": 0, "output_tokens": 5},
        })


async def _start_upstream(mock: _MockUpstream) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_post("/v1/messages", mock.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


async def _start_proxy(
    upstream_url: str, *, usage_log: Path | None = None,
    mode: StelaMode | None = None, corpus_dir: Path | None = None,
    record: bool = True,
) -> tuple[web.AppRunner, str]:
    app = make_app(upstream=upstream_url, usage_log=usage_log, mode=mode,
                   corpus_dir=corpus_dir, record=record)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


def _req_with_big_tool_result() -> dict[str, Any]:
    """构造一个带大段重复 bash 输出的请求（RTK 过滤的理想目标）。"""
    big = "build start\n" + ("compiling module foo bar baz qux\n" * 300) + "build done\n"
    return {
        "model": "claude-opus-4-7",
        "max_tokens": 64,
        "system": [{"type": "text", "text": "You are an agent."}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "build it"}]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu1", "name": "Bash",
                 "input": {"command": "cargo build"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": big},
            ]},
        ],
    }


def _tool_result_len(wire: dict[str, Any]) -> int:
    for msg in wire.get("messages", []):
        if msg.get("role") != "user":
            continue
        for item in msg.get("content", []):
            if isinstance(item, dict) and item.get("type") == "tool_result":
                c = item.get("content")
                if isinstance(c, str):
                    return len(c)
    return -1


async def _test_mode_rtk_shrinks_tool_result() -> None:
    """mode=rtk：upstream 收到的 tool_result 被过滤缩短，且无 cache_control。"""
    mock = _MockUpstream()
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)
    try:
        req = _req_with_big_tool_result()
        original_len = _tool_result_len(req)
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages", json=req,
                headers={"x-api-key": "k", "x-stela-mode": "rtk",
                         "x-stela-session": "rtk-test"},
            ) as resp:
                assert resp.status == 200, await resp.text()

        wire = mock.last_body
        assert wire is not None
        assert _tool_result_len(wire) < original_len, "tool_result 未被 RTK 缩短"
        # rtk-only 模式不打 cache_control
        blocks = list(wire.get("tools") or []) + list(wire.get("system") or [])
        assert not any("cache_control" in b for b in blocks)
        print("✓ test_mode_rtk_shrinks_tool_result")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_mode_none_is_byte_identical() -> None:
    """mode=none：纯透传，upstream 收到的 body 与原 raw 完全一致。"""
    mock = _MockUpstream()
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)
    try:
        req = _req_with_big_tool_result()
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages", json=req,
                headers={"x-api-key": "k", "x-stela-mode": "none",
                         "x-stela-session": "none-test"},
            ) as resp:
                assert resp.status == 200

        assert mock.last_body == req, "mode=none 应原样透传"
        print("✓ test_mode_none_is_byte_identical")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_mode_stela_marks_cache_control() -> None:
    """mode=stela：跑管线打 cache_control，但 tool_result 不被过滤。"""
    mock = _MockUpstream()
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url)
    try:
        req = _req_with_big_tool_result()
        original_len = _tool_result_len(req)
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages", json=req,
                headers={"x-api-key": "k", "x-stela-mode": "stela",
                         "x-stela-session": "stela-test"},
            ) as resp:
                assert resp.status == 200

        wire = mock.last_body
        blocks = list(wire.get("tools") or []) + list(wire.get("system") or [])
        assert any("cache_control" in b for b in blocks), "mode=stela 应有 cache_control"
        # STELA 不动 tool_result 文本
        assert _tool_result_len(wire) == original_len
        print("✓ test_mode_stela_marks_cache_control")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_compare_group_and_reduction_logged(tmp_log: Path) -> None:
    """mode=both + compare_group header：usage_log 记录 mode / group / 过滤量。"""
    mock = _MockUpstream()
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url, usage_log=tmp_log)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages", json=_req_with_big_tool_result(),
                headers={"x-api-key": "k", "x-stela-mode": "both",
                         "x-stela-session": "cmp-test",
                         "x-stela-compare-group": "task-42"},
            ) as resp:
                assert resp.status == 200

        record = json.loads(tmp_log.read_text().strip().splitlines()[-1])
        assert record["mode"] == "both"
        assert record["compare_group"] == "task-42"
        red = record["tool_output_reduction"]
        assert red["blocks_filtered"] == 1
        assert red["saved_chars"] > 0
        print("✓ test_compare_group_and_reduction_logged")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_mode_is_sticky_per_session() -> None:
    """首个请求用 header 设 mode=rtk，后续同 session 无 header 仍走 rtk。"""
    mock = _MockUpstream()
    up_runner, up_url = await _start_upstream(mock)
    # proxy 进程默认 mode=stela —— 若 sticky 失效，第二个请求会打 cache_control
    px_runner, px_url = await _start_proxy(up_url, mode=StelaMode.from_label("stela"))
    try:
        async with aiohttp.ClientSession() as client:
            # 第一轮：显式 header rtk
            async with client.post(
                f"{px_url}/v1/messages", json=_req_with_big_tool_result(),
                headers={"x-api-key": "k", "x-stela-mode": "rtk",
                         "x-stela-session": "sticky-test"},
            ) as resp:
                assert resp.status == 200
            # 第二轮：同 session，无 header
            async with client.post(
                f"{px_url}/v1/messages", json=_req_with_big_tool_result(),
                headers={"x-api-key": "k", "x-stela-session": "sticky-test"},
            ) as resp:
                assert resp.status == 200

        wire = mock.last_body
        blocks = list(wire.get("tools") or []) + list(wire.get("system") or [])
        assert not any("cache_control" in b for b in blocks), \
            "sticky 失效：第二个请求退回到了默认 stela mode"
        print("✓ test_mode_is_sticky_per_session")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_proxy_records_corpus(corpus_dir: Path) -> None:
    """proxy 默认把原始请求录进语料库，replay 之后能 load 回来。"""
    mock = _MockUpstream()
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url, corpus_dir=corpus_dir)
    try:
        async with aiohttp.ClientSession() as client:
            for _ in range(2):
                async with client.post(
                    f"{px_url}/v1/messages", json=_req_with_big_tool_result(),
                    headers={"x-api-key": "k", "x-stela-mode": "both",
                             "x-stela-session": "corpus-test"},
                ) as resp:
                    assert resp.status == 200

        turns = load_session(corpus_dir, "corpus-test")
        assert len(turns) == 2, f"语料应录到 2 轮，实得 {len(turns)}"
        # 录的是原始请求（未被 RTK 缩短）
        tr_len = _tool_result_len(turns[1]["request"])
        assert tr_len == _tool_result_len(_req_with_big_tool_result())
        print("✓ test_proxy_records_corpus")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _test_no_record_disables_corpus(corpus_dir: Path) -> None:
    mock = _MockUpstream()
    up_runner, up_url = await _start_upstream(mock)
    px_runner, px_url = await _start_proxy(up_url, corpus_dir=corpus_dir,
                                           record=False)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{px_url}/v1/messages", json=_req_with_big_tool_result(),
                headers={"x-api-key": "k", "x-stela-session": "norecord-test"},
            ) as resp:
                assert resp.status == 200
        assert not (corpus_dir / "norecord-test.jsonl").exists(), \
            "--no-record 时不应写语料"
        print("✓ test_no_record_disables_corpus")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


async def _run_all(tmp_log: Path, corpus_dir: Path) -> None:
    await _test_mode_rtk_shrinks_tool_result()
    await _test_mode_none_is_byte_identical()
    await _test_mode_stela_marks_cache_control()
    await _test_compare_group_and_reduction_logged(tmp_log)
    await _test_mode_is_sticky_per_session()
    await _test_proxy_records_corpus(corpus_dir)
    await _test_no_record_disables_corpus(corpus_dir)


def test_proxy_mode() -> None:
    """pytest 入口：在单个 event loop 里跑完整套。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        asyncio.run(_run_all(Path(td) / "usage.jsonl", Path(td) / "corpus"))


def main() -> None:
    test_proxy_mode()
    print("\nall proxy mode tests passed.")


if __name__ == "__main__":
    main()
