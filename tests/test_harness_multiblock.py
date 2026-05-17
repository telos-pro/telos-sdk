"""harness §5 顺序回归：多 content-block user message 必须产生合法 IR。

针对的真实 bug：Claude Code / Hermes 的 user message 常常是多 part 结构
（text + tool_result + image），每个 text 自带 envelope。旧代码每个 content
item 各自 expand 成 ``(PIN, FOLD*, DROP*)``，然后 ``extend`` 拼接，结果是
``PIN, DROP, PIN, DROP, ...`` —— 违反 ``pin* → fold* → drop*``。

修复后：harness 在 message 级别用 ``enforce_band_order`` 兜底，确保 §5 成立。
"""

from __future__ import annotations

from stela import Band, Bridge, load_engine, load_harness


def _user_with_n_text_blocks(n: int) -> dict:
    """构造一个 user message，里面有 n 个 text content blocks，
    每个都自带 ``<environment_info>`` envelope（DROP 候选）。"""
    return {
        "model": "claude-opus-4-7",
        "max_tokens": 64,
        "system": [{"type": "text", "text": "You are an agent."}],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text",
                 "text": f"Question {i}. "
                         f"<environment_info>turn={i}</environment_info>"}
                for i in range(n)
            ],
        }],
    }


def _assert_band_order(blocks) -> None:
    """§5 顺序：tool_result* → pin* → fold* → drop*（tool_result 视作 rank -1）。"""
    rank = {Band.PIN: 0, Band.FOLD: 1, Band.DROP: 2}
    last = -2
    for b in blocks:
        r = -1 if b.kind == "tool_result" else rank[b.band]
        assert r >= last, f"violation: {b.id} band={b.band} after rank={last}"
        last = r


def test_openclaw_multiple_text_blocks() -> None:
    h = load_harness("openclaw")
    for n in (2, 4, 13):  # 13 模拟真实 Claude Code 长对话首条
        ir = h.parse(_user_with_n_text_blocks(n), session_id=f"oc-{n}",
                     engine="anthropic", model="claude-opus-4-7")
        msg = ir.messages[0]
        _assert_band_order(msg.blocks)
        # Bridge 构造会再次校验全 IR 不变量
        Bridge(ir, load_engine("anthropic"))
    print("✓ test_openclaw_multiple_text_blocks")


def test_hermes_multiple_text_blocks() -> None:
    h = load_harness("hermes")
    for n in (2, 4, 13):
        ir = h.parse(_user_with_n_text_blocks(n), session_id=f"h-{n}",
                     engine="anthropic", model="claude-opus-4-7")
        _assert_band_order(ir.messages[0].blocks)
        Bridge(ir, load_engine("anthropic"))
    print("✓ test_hermes_multiple_text_blocks")


def test_pins_preserved_in_source_order() -> None:
    """同一带内必须保留 content 源顺序（stable sort）。
    Question 0 / 1 / 2 三个 PIN 在排序后仍按 0, 1, 2。"""
    h = load_harness("openclaw")
    ir = h.parse(_user_with_n_text_blocks(3), session_id="stable",
                 engine="anthropic")
    pins = [b for b in ir.messages[0].blocks if b.band is Band.PIN]
    assert [str(b.payload).startswith(f"Question {i}.") for i, b in enumerate(pins)]
    print("✓ test_pins_preserved_in_source_order")


def test_user_text_then_tool_result_then_text() -> None:
    """混合 text + tool_result + text：tool_result 居首，其余 pin*→fold*→drop*。"""
    req = {
        "model": "claude-opus-4-7",
        "max_tokens": 64,
        "system": [{"type": "text", "text": "agent"}],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text",
                 "text": "Q1. <environment_info>x</environment_info>"},
                {"type": "tool_result", "tool_use_id": "tu_1",
                 "content": [{"type": "text", "text": "stdout..."}]},
                {"type": "text",
                 "text": "Q2. Current time: now"},
            ],
        }],
    }
    h = load_harness("openclaw")
    ir = h.parse(req, session_id="mix", engine="anthropic")
    blocks = ir.messages[0].blocks
    _assert_band_order(blocks)
    bands = [b.band for b in blocks]
    assert bands.count(Band.PIN) == 2
    assert bands.count(Band.FOLD) == 1  # tool_result
    assert bands.count(Band.DROP) == 2  # 两个 envelope
    Bridge(ir, load_engine("anthropic"))
    print("✓ test_user_text_then_tool_result_then_text")


def main() -> None:
    test_openclaw_multiple_text_blocks()
    test_hermes_multiple_text_blocks()
    test_pins_preserved_in_source_order()
    test_user_text_then_tool_result_then_text()
    print("\nall harness-multiblock regression tests passed.")


if __name__ == "__main__":
    main()
