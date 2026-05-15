"""``stela.output_filter`` 单测：mode 解析、fallback 过滤器、apply_filter。"""

from __future__ import annotations

from stela.output_filter import (
    FallbackFilter,
    StelaMode,
    apply_filter,
    build_filter,
)


def test_mode_labels_roundtrip() -> None:
    for label, (stela, rtk) in {
        "none": (False, False),
        "stela": (True, False),
        "rtk": (False, True),
        "both": (True, True),
    }.items():
        m = StelaMode.from_label(label)
        assert (m.stela, m.rtk) == (stela, rtk), label
        assert m.label == label
    print("✓ test_mode_labels_roundtrip")


def test_mode_unknown_falls_back_to_stela() -> None:
    """空 / None / 垃圾值都退化到默认 stela（保持引入开关前的历史行为）。"""
    for bad in (None, "", "garbage", "STELA+RTK"):
        m = StelaMode.from_label(bad)
        assert m.label == "stela", bad
    print("✓ test_mode_unknown_falls_back_to_stela")


def test_fallback_dedup_collapses_repeats() -> None:
    flt = FallbackFilter()
    text = "head\n" + ("repeated line of build output\n" * 100) + "tail"
    rec = flt.filter_text(text, tool_name="Bash", command="cargo build")
    assert rec.saved_chars > 0
    assert rec.rule.startswith("fallback")
    assert "(×100)" in rec.text
    assert "head" in rec.text and "tail" in rec.text
    print("✓ test_fallback_dedup_collapses_repeats")


def test_fallback_skips_short_output() -> None:
    """短输出不值得过滤 —— 原样返回。"""
    flt = FallbackFilter()
    rec = flt.filter_text("ok\n", tool_name="Bash", command="ls")
    assert rec.saved_chars == 0
    assert rec.rule == "passthrough"
    print("✓ test_fallback_skips_short_output")


def test_fallback_truncates_long_unique_output() -> None:
    """无重复但很长的输出走头尾截断。"""
    flt = FallbackFilter()
    text = "\n".join(f"unique line number {i} with some content" for i in range(2000))
    rec = flt.filter_text(text, tool_name="Bash", command="find .")
    assert rec.saved_chars > 0
    assert "已省略" in rec.text
    print("✓ test_fallback_truncates_long_unique_output")


def test_apply_filter_rewrites_tool_result_and_counts() -> None:
    flt = build_filter()
    big = "start\n" + ("compiling\n" * 300) + "done\n"
    raw = {
        "model": "claude-opus-4-7",
        "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu1", "name": "Bash",
                 "input": {"command": "cargo build"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": big},
            ]},
        ],
    }
    new, stats = apply_filter(raw, flt)
    # 原 raw 不被改动
    assert raw["messages"][1]["content"][0]["content"] == big
    # 新 raw 的 tool_result 被缩短
    filtered = new["messages"][1]["content"][0]["content"]
    assert len(filtered) < len(big)
    assert stats.blocks_seen == 1
    assert stats.blocks_filtered == 1
    assert stats.saved_chars > 0
    d = stats.as_dict()
    assert d["original_chars"] == len(big)
    assert sum(d["by_rule"].values()) == 1
    print("✓ test_apply_filter_rewrites_tool_result_and_counts")


def test_apply_filter_handles_block_list_content() -> None:
    """tool_result 的 content 也可能是 block 列表 —— 只过滤 text 块。"""
    flt = build_filter()
    big = "x\n" + ("dup\n" * 400)
    raw = {
        "messages": [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu9", "content": [
                    {"type": "text", "text": big},
                    {"type": "image", "source": {"data": "..."}},
                ]},
            ]},
        ],
    }
    new, stats = apply_filter(raw, flt)
    blk = new["messages"][0]["content"][0]["content"]
    assert len(blk[0]["text"]) < len(big)
    assert blk[1]["type"] == "image"  # 非 text 块原样保留
    assert stats.blocks_filtered == 1
    print("✓ test_apply_filter_handles_block_list_content")


def main() -> None:
    test_mode_labels_roundtrip()
    test_mode_unknown_falls_back_to_stela()
    test_fallback_dedup_collapses_repeats()
    test_fallback_skips_short_output()
    test_fallback_truncates_long_unique_output()
    test_apply_filter_rewrites_tool_result_and_counts()
    test_apply_filter_handles_block_list_content()
    print("\nall output_filter tests passed.")


if __name__ == "__main__":
    main()
