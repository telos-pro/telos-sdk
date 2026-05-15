"""端到端测试：proxy 内嵌的 live savings dashboard 端点。

跑法（同 tests/test_proxy_server.py 风格，不依赖 pytest-aiohttp）::

    python -m stela.tests.test_proxy_dashboard
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import aiohttp
from aiohttp import web

from stela.proxy.server import make_app


# ---------------------------------------------------------------------------
# 辅助：起一个 proxy，不需要 upstream（dashboard 路径不打 upstream）
# ---------------------------------------------------------------------------

async def _start_proxy(
    *, usage_log: Path | None, dashboard_refresh: int = 5,
) -> tuple[web.AppRunner, str]:
    app = make_app(
        upstream="http://127.0.0.1:1",  # 任意，dashboard 路径用不到
        usage_log=usage_log,
        dashboard_refresh=dashboard_refresh,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def _write_records(log: Path, records: list[dict]) -> None:
    with log.open("a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def _test_dashboard_empty_state() -> None:
    """没 usage_log 时也要返回一个可显示的 HTML（带 refresh tag）。"""
    runner, url = await _start_proxy(usage_log=None, dashboard_refresh=3)
    try:
        async with aiohttp.ClientSession() as c:
            async with c.get(f"{url}/__stela/dashboard") as resp:
                assert resp.status == 200
                assert resp.headers["Content-Type"].startswith("text/html")
                body = await resp.text()
                assert "No usage_log configured" in body
                # auto-refresh tag 必须在，浏览器才会自己重试
                assert 'http-equiv="refresh"' in body
                assert 'content="3"' in body
        print("✓ test_dashboard_empty_state")
    finally:
        await runner.cleanup()


async def _test_dashboard_renders_records() -> None:
    """有数据时把 record 渲染出来，hero card 的金额 / token 数应正确。"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "usage.jsonl"
        _write_records(log, [
            {"ts": 1_700_000_000, "model": "claude-opus-4-7",
             "harness": "claude-code", "session_id": "s1",
             "normalized": {"raw_input": 500, "cache_read": 20_000,
                             "cache_write": 0, "output": 300}},
            {"ts": 1_700_003_600, "model": "claude-sonnet-4-6",
             "harness": "claude-code", "session_id": "s1",
             "normalized": {"raw_input": 1_000, "cache_read": 9_000,
                             "cache_write": 0, "output": 200}},
        ])

        runner, url = await _start_proxy(usage_log=log, dashboard_refresh=10)
        try:
            async with aiohttp.ClientSession() as c:
                async with c.get(f"{url}/__stela/dashboard") as resp:
                    assert resp.status == 200
                    body = await resp.text()
                    assert "STELA · Token Savings" in body
                    # 累计 cache_read = 29K
                    assert "29.00K" in body
                    # opus saved 20K * $13.5/M = $0.27; sonnet 9K * $2.7/M = $0.0243
                    # 总 ~ $0.294 → 格式化成 $0.294
                    assert "$0.294" in body or "$0.29" in body
                    assert "claude-opus-4-7" in body
                    assert "claude-sonnet-4-6" in body
                    assert "claude-code" in body
                    # refresh tag 也要带
                    assert 'content="10"' in body
            print("✓ test_dashboard_renders_records")
        finally:
            await runner.cleanup()


async def _test_dashboard_reflects_new_records() -> None:
    """实时更新关键：写新行 → 立刻 GET → cache_read 总和必须变大。"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "usage.jsonl"
        _write_records(log, [
            {"ts": 1_700_000_000, "model": "claude-sonnet-4-6",
             "harness": "h", "session_id": "s",
             "normalized": {"raw_input": 0, "cache_read": 1_000,
                             "cache_write": 0, "output": 0}},
        ])
        runner, url = await _start_proxy(usage_log=log, dashboard_refresh=5)
        try:
            async with aiohttp.ClientSession() as c:
                async with c.get(f"{url}/__stela/dashboard") as resp:
                    body1 = await resp.text()
                # 追加一条 100K cache_read 的大单
                _write_records(log, [
                    {"ts": 1_700_007_200, "model": "claude-opus-4-7",
                     "harness": "h", "session_id": "s",
                     "normalized": {"raw_input": 0, "cache_read": 100_000,
                                     "cache_write": 0, "output": 0}},
                ])
                async with c.get(f"{url}/__stela/dashboard") as resp:
                    body2 = await resp.text()
            # 第一次 1K，第二次 101K
            assert "1.00K" in body1
            assert "101.00K" in body2
            print("✓ test_dashboard_reflects_new_records")
        finally:
            await runner.cleanup()


async def _test_dashboard_refresh_zero_disables_meta() -> None:
    """--dashboard-refresh 0 时不能注入 meta-refresh（用户显式关掉）。"""
    runner, url = await _start_proxy(usage_log=None, dashboard_refresh=0)
    try:
        async with aiohttp.ClientSession() as c:
            async with c.get(f"{url}/__stela/dashboard") as resp:
                body = await resp.text()
                assert 'http-equiv="refresh"' not in body
        print("✓ test_dashboard_refresh_zero_disables_meta")
    finally:
        await runner.cleanup()


async def _run_all() -> None:
    await _test_dashboard_empty_state()
    await _test_dashboard_renders_records()
    await _test_dashboard_reflects_new_records()
    await _test_dashboard_refresh_zero_disables_meta()


def main() -> None:
    asyncio.run(_run_all())
    print("\nall proxy dashboard tests passed.")


if __name__ == "__main__":
    main()
