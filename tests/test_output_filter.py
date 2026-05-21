"""``telos.output_filter`` unit tests: mode parsing, fallback filter, apply_filter."""

from __future__ import annotations

from telos.output_filter import (
    FallbackFilter,
    TelosMode,
    apply_filter,
    build_filter,
)
from telos.output_filter.filters import FilterRecord
from telos.output_filter.tokens import estimate_tokens


def test_mode_labels_roundtrip() -> None:
    for label, (telos, rtk) in {
        "none": (False, False),
        "telos": (True, False),
        "rtk": (False, True),
        "both": (True, True),
    }.items():
        m = TelosMode.from_label(label)
        assert (m.telos, m.rtk) == (telos, rtk), label
        assert m.label == label
    print("✓ test_mode_labels_roundtrip")


def test_mode_unknown_falls_back_to_telos() -> None:
    """Empty / None / garbage values all fall back to the default telos (preserving the historical behavior before the switch was introduced)."""
    for bad in (None, "", "garbage", "TELOS+RTK"):
        m = TelosMode.from_label(bad)
        assert m.label == "telos", bad
    print("✓ test_mode_unknown_falls_back_to_telos")


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
    """Short output is not worth filtering -- returned unchanged."""
    flt = FallbackFilter()
    rec = flt.filter_text("ok\n", tool_name="Bash", command="ls")
    assert rec.saved_chars == 0
    assert rec.rule == "passthrough"
    print("✓ test_fallback_skips_short_output")


def test_fallback_truncates_long_unique_output() -> None:
    """Long output with no duplicates goes through head/tail truncation."""
    flt = FallbackFilter()
    text = "\n".join(f"unique line number {i} with some content" for i in range(2000))
    rec = flt.filter_text(text, tool_name="Bash", command="find .")
    assert rec.saved_chars > 0
    assert "characters omitted" in rec.text
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
    # the original raw is not modified
    assert raw["messages"][1]["content"][0]["content"] == big
    # the tool_result in the new raw is shortened
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
    """A tool_result's content may also be a block list -- only the text blocks are filtered."""
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
    assert blk[1]["type"] == "image"  # non-text blocks are kept unchanged
    assert stats.blocks_filtered == 1
    print("✓ test_apply_filter_handles_block_list_content")


def test_estimate_tokens_heuristic() -> None:
    """estimate_tokens: empty string is 0, non-empty is always ≥1, punctuation-heavy text exceeds chars/4."""
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello world") >= 1
    # pure punctuation: 1 token per symbol, far more than chars/4
    puncts = "(){}[];,.<>" * 10
    assert estimate_tokens(puncts) > len(puncts) // 4
    # monotonicity: long text has more tokens than its prefix
    long = "def foo(x): return x + 1\n" * 50
    assert estimate_tokens(long) > estimate_tokens(long[:100])
    print("✓ test_estimate_tokens_heuristic")


def test_filter_record_token_fields() -> None:
    """When the filter hits, FilterRecord carries a token estimate, and saved_tokens moves in the same direction as characters."""
    flt = FallbackFilter()
    text = "head\n" + ("repeated build line\n" * 100) + "tail"
    rec = flt.filter_text(text, tool_name="Bash", command="cargo build")
    assert rec.original_tokens > 0
    assert rec.filtered_tokens > 0
    assert rec.original_tokens > rec.filtered_tokens
    assert rec.saved_tokens > 0
    # passthrough: tokens before and after are equal → saved_tokens == 0
    pt = FilterRecord.passthrough("short")
    assert pt.saved_tokens == 0
    assert pt.original_tokens == pt.filtered_tokens
    print("✓ test_filter_record_token_fields")


def test_apply_filter_emits_token_fields() -> None:
    """FilterStats.as_dict() carries original/filtered/saved_tokens, written into usage_log."""
    flt = build_filter()
    big = "start\n" + ("compiling module\n" * 300) + "done\n"
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
    _, stats = apply_filter(raw, flt)
    d = stats.as_dict()
    assert d["original_tokens"] > 0
    assert d["filtered_tokens"] > 0
    assert d["saved_tokens"] == d["original_tokens"] - d["filtered_tokens"]
    assert d["saved_tokens"] > 0
    print("✓ test_apply_filter_emits_token_fields")


def main() -> None:
    test_mode_labels_roundtrip()
    test_mode_unknown_falls_back_to_telos()
    test_fallback_dedup_collapses_repeats()
    test_fallback_skips_short_output()
    test_fallback_truncates_long_unique_output()
    test_apply_filter_rewrites_tool_result_and_counts()
    test_apply_filter_handles_block_list_content()
    test_estimate_tokens_heuristic()
    test_filter_record_token_fields()
    test_apply_filter_emits_token_fields()
    print("\nall output_filter tests passed.")


if __name__ == "__main__":
    main()
