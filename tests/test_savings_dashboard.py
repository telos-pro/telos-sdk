"""``build_savings_dashboard`` 单测：RTK token 来源、缓存加权计价、STELA/RTK 分项。"""

from __future__ import annotations

from pathlib import Path

from stela.scripts.build_savings_dashboard import aggregate, render_dashboard


def _rec(*, model: str = "claude-opus-4-7", raw_input: int = 0,
         cache_read: int = 0, cache_write: int = 0, output: int = 0,
         tool: dict | None = None, mode: str = "both", session: str = "s",
         ts: float = 1.0) -> dict:
    r: dict = {
        "model": model, "harness": "claude-code", "session_id": session,
        "mode": mode, "ts": ts,
        "normalized": {
            "raw_input": raw_input, "cache_read": cache_read,
            "cache_write": cache_write, "output": output,
        },
    }
    if tool is not None:
        r["tool_output_reduction"] = tool
    return r


def test_rtk_tokens_prefer_logged_token_fields() -> None:
    """tool_output_reduction 带 token 字段时，tool_saved_tokens 取日志值而非 chars/4。"""
    tool = {
        "original_chars": 40000, "filtered_chars": 12000,  # saved_chars/4 = 7000
        "original_tokens": 9000, "filtered_tokens": 4000,  # saved_tokens   = 5000
        "saved_tokens": 5000, "blocks_filtered": 2,
    }
    summary = aggregate([_rec(raw_input=1000, tool=tool)])
    assert summary.total.tool_saved_tokens == 5000, summary.total.tool_saved_tokens
    print("✓ test_rtk_tokens_prefer_logged_token_fields")


def test_rtk_tokens_fall_back_to_chars_for_old_logs() -> None:
    """旧日志只有 chars，无 token 字段 → 回退 saved_chars // 4。"""
    tool = {"original_chars": 40000, "filtered_chars": 12000, "blocks_filtered": 1}
    summary = aggregate([_rec(raw_input=1000, tool=tool)])
    assert summary.total.tool_saved_tokens == 28000 // 4
    print("✓ test_rtk_tokens_fall_back_to_chars_for_old_logs")


def test_combined_equals_stela_plus_rtk() -> None:
    """combined_saved_usd 严格等于 STELA + RTK 两路之和，不双算。"""
    tool = {"original_tokens": 8000, "filtered_tokens": 2000, "saved_tokens": 6000,
            "original_chars": 32000, "filtered_chars": 8000}
    summary = aggregate([_rec(raw_input=5000, cache_read=50000, tool=tool)])
    t = summary.total
    assert abs(t.combined_saved_usd - (t.saved_usd + t.tool_saved_usd)) < 1e-12
    assert t.saved_usd > 0 and t.tool_saved_usd > 0
    print("✓ test_combined_equals_stela_plus_rtk")


def test_cache_hit_weights_down_rtk_savings() -> None:
    """同样省下 N token：高缓存命中率的 call，RTK $ 应低于零命中（边际价更便宜）。"""
    tool = {"original_tokens": 12000, "filtered_tokens": 2000, "saved_tokens": 10000}
    # A：全 raw_input → hit=0 → eff_price = input 价
    # B：全 cache_read → hit=1 → eff_price = cache_read 价（便宜 10×）
    summary = aggregate([
        _rec(raw_input=100000, tool=dict(tool), session="A"),
        _rec(cache_read=100000, tool=dict(tool), session="B"),
    ])
    a = summary.by_session["A"].tool_saved_usd
    b = summary.by_session["B"].tool_saved_usd
    assert a > b > 0, (a, b)
    # opus 4.7：input $5 / cache_read $0.5 → 比值 ~10×
    assert abs(a / b - 10.0) < 0.5, a / b
    print("✓ test_cache_hit_weights_down_rtk_savings")


def test_render_shows_total_cost_saved() -> None:
    """渲染产物含 combined 口径的 hero 卡与 STELA/RTK 分项 KPI。"""
    tool = {"original_tokens": 8000, "filtered_tokens": 2000, "saved_tokens": 6000}
    summary = aggregate([_rec(raw_input=5000, cache_read=50000, tool=tool)])
    html_doc = render_dashboard(summary, [Path("sample.jsonl")])
    assert "total cost saved" in html_doc
    assert "STELA saved $" in html_doc
    assert "RTK saved $" in html_doc
    print("✓ test_render_shows_total_cost_saved")


def test_rtk_status_distinguishes_disabled_from_zero_save() -> None:
    """rtk_status 把「$0」拆开：从未启用 / 启用但空闲 / 跑了但没省 / 实际省。"""
    # disabled：根本没有 tool_output_reduction
    s = aggregate([_rec(raw_input=1000)])
    assert s.total.rtk_status == "disabled"
    # disabled：tool_output_reduction 是空 dict（RTK 关时 proxy 写的就是 {}）
    s = aggregate([_rec(raw_input=1000, tool={})])
    assert s.total.rtk_status == "disabled"
    # idle：RTK 跑了但没扫到 tool_result
    s = aggregate([_rec(raw_input=1000, tool={"blocks_seen": 0,
                                              "original_chars": 0})])
    assert s.total.rtk_status == "idle"
    # nosave：扫到了工具输出但没省（原文 == 过滤后）
    s = aggregate([_rec(raw_input=1000, tool={"blocks_seen": 3, "saved_tokens": 0,
                                              "original_chars": 500,
                                              "filtered_chars": 500})])
    assert s.total.rtk_status == "nosave"
    # active：实际省下 token
    s = aggregate([_rec(raw_input=1000, tool={"blocks_seen": 3, "saved_tokens": 4000,
                                              "original_tokens": 9000,
                                              "filtered_tokens": 5000})])
    assert s.total.rtk_status == "active"
    print("✓ test_rtk_status_distinguishes_disabled_from_zero_save")


def test_render_marks_rtk_not_enabled() -> None:
    """RTK 从未启用时，渲染产物显式标 not enabled，而非和省 0 混同。"""
    summary = aggregate([_rec(raw_input=5000, cache_read=50000, mode="stela")])
    html_doc = render_dashboard(summary, [Path("sample.jsonl")])
    assert "not enabled" in html_doc or "未启用" in html_doc
    print("✓ test_render_marks_rtk_not_enabled")


def main() -> None:
    test_rtk_tokens_prefer_logged_token_fields()
    test_rtk_tokens_fall_back_to_chars_for_old_logs()
    test_combined_equals_stela_plus_rtk()
    test_cache_hit_weights_down_rtk_savings()
    test_render_shows_total_cost_saved()
    test_rtk_status_distinguishes_disabled_from_zero_save()
    test_render_marks_rtk_not_enabled()
    print("\nall savings_dashboard tests passed.")


if __name__ == "__main__":
    main()
