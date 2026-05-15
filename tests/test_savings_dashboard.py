"""``stela.scripts.build_savings_dashboard`` 的纯函数单测。

覆盖：
- 价格表前缀匹配（已知 model + 未知 model 走 _default）
- 单 call 的省钱公式 cache_read * (input - cache_read_price) / 1M
- aggregate() 在 multi-record / multi-harness / multi-session 下的累计
- 端到端 main() 写出 HTML 文件
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from stela.scripts.build_savings_dashboard import (
    _cost_usd,
    _price_for,
    _saved_usd_for_call,
    aggregate,
    main,
)


def test_price_for_prefix_match() -> None:
    """长 prefix 优先：claude-opus-4-7 不能错配到 claude-opus-4。"""
    p = _price_for("claude-opus-4-7")
    assert p["input"] == 15.00
    assert p["cache_read"] == 1.50

    p = _price_for("claude-sonnet-4-6")
    assert p["input"] == 3.00

    p = _price_for("deepseek-chat")
    assert p["input"] == 0.27


def test_price_for_unknown_falls_back() -> None:
    p_unknown = _price_for("some-future-model-x")
    p_default = _price_for("")
    assert p_unknown == p_default
    assert p_default["input"] == 3.00  # _default 走 Sonnet 价


def test_saved_usd_formula() -> None:
    """sonnet：input=$3 / cache_read=$0.30，省 $2.70 per 1M。10K cache_read → $0.027。"""
    n = {"raw_input": 0, "cache_read": 10_000, "cache_write": 0, "output": 0}
    saved = _saved_usd_for_call("claude-sonnet-4-6", n)
    assert abs(saved - 0.027) < 1e-9


def test_cost_breakdown_sums_correctly() -> None:
    n = {"raw_input": 1_000_000, "cache_read": 0, "cache_write": 0, "output": 0}
    cost = _cost_usd("claude-sonnet-4-6", n)
    assert abs(cost["raw_input"] - 3.00) < 1e-9
    assert cost["cache_read"] == 0.0


def _record(*, ts: float, model: str, harness: str, session: str,
             raw_input: int, cache_read: int, cache_write: int = 0,
             output: int = 0) -> dict:
    return {
        "ts": ts, "model": model, "harness": harness, "session_id": session,
        "normalized": {"raw_input": raw_input, "cache_read": cache_read,
                        "cache_write": cache_write, "output": output},
    }


def test_aggregate_multi_record() -> None:
    records = [
        _record(ts=1_700_000_000, model="claude-sonnet-4-6", harness="claude-code",
                session="s1", raw_input=1_000, cache_read=9_000, output=200),
        _record(ts=1_700_003_600, model="claude-opus-4-7", harness="claude-code",
                session="s1", raw_input=500, cache_read=15_000, output=800),
        _record(ts=1_700_007_200, model="gpt-5", harness="codex",
                session="s2", raw_input=2_000, cache_read=1_000, output=400),
    ]
    s = aggregate(records)

    # 累计
    assert s.total.calls == 3
    assert s.total.raw_input == 3_500
    assert s.total.cache_read == 25_000
    assert s.total.output == 1_400

    # 分组
    assert s.by_harness["claude-code"].calls == 2
    assert s.by_harness["codex"].calls == 1
    assert s.by_model["claude-opus-4-7"].cache_read == 15_000
    assert "s1" in s.by_session and "s2" in s.by_session
    assert s.sessions_seen == {"s1", "s2"}

    # saved_usd：sonnet 9K (省 $2.70/M) + opus 15K (省 $13.50/M) + gpt-5 1K (省 $3.75/M)
    expected = 9_000 * 2.70 / 1e6 + 15_000 * 13.50 / 1e6 + 1_000 * 3.75 / 1e6
    assert abs(s.total.saved_usd - expected) < 1e-9

    # timeline 至少有 3 个 hour bucket
    assert len(s.timeline) == 3
    assert s.first_ts == 1_700_000_000
    assert s.last_ts == 1_700_007_200


def test_aggregate_skips_records_without_normalized() -> None:
    records = [
        _record(ts=1, model="claude-sonnet-4-6", harness="h", session="s",
                raw_input=10, cache_read=20),
        {"ts": 2, "model": "x", "harness": "y"},  # 没 normalized
        {"ts": 3, "model": "x", "normalized": {}},  # 空 normalized 也跳
    ]
    s = aggregate(records)
    assert s.total.calls == 1


def test_main_writes_html(tmp_path: Path | None = None) -> None:
    """端到端：写 jsonl → main() → 检查 HTML 关键字段都在。"""
    workdir = Path(tempfile.mkdtemp())
    log = workdir / "usage.jsonl"
    out = workdir / "savings.html"

    records = [
        _record(ts=1_700_000_000, model="claude-opus-4-7", harness="claude-code",
                session="abc-123", raw_input=500, cache_read=20_000, output=300),
        _record(ts=1_700_003_600, model="claude-haiku-4-5", harness="bench",
                session="def-456", raw_input=100, cache_read=200, output=50),
    ]
    with log.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    rc = main(["--usage-log", str(log), "--out", str(out)])
    assert rc == 0
    assert out.exists()

    body = out.read_text(encoding="utf-8")
    assert "STELA · Token Savings" in body
    assert "tokens saved" in body
    assert "claude-opus-4-7" in body
    assert "claude-code" in body
    # opus saved = 20_000 * 13.50 / 1M = $0.270; haiku 微量 → 总 ~ $0.272
    assert "$0.27" in body or "$0.27" in body.replace(",", "")


def _run_all() -> None:
    test_price_for_prefix_match()
    test_price_for_unknown_falls_back()
    test_saved_usd_formula()
    test_cost_breakdown_sums_correctly()
    test_aggregate_multi_record()
    test_aggregate_skips_records_without_normalized()
    test_main_writes_html()
    print("OK · all savings-dashboard tests passed")


if __name__ == "__main__":
    _run_all()
