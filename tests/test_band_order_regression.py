"""回归测试：Claude Code / Hermes 风格的多 content-block user message
不允许触发 §5 band order 违反。

复现场景：``content`` 数组含 ``[tool_result(FOLD), text(PIN+DROP)]``
或更复杂的混合，源序拼接会产生 (FOLD, PIN, DROP) —— 违反 ``pin* → fold* → drop*``。
harness 必须在 message 级别调一次 ``enforce_band_order`` 兜底。
"""

from __future__ import annotations

from stela import Band, Bridge, load_engine, load_harness
from stela.ir import assert_ir_invariants


_CLAUDE_CODE_USER_MSG = {
    "role": "user",
    "content": [
        # 上一轮的 tool_result（FOLD）—— 源序在前
        {"type": "tool_result", "tool_use_id": "toolu_x",
         "content": [{"type": "text", "text": "<file content...>"}]},
        # 本轮新提问（PIN）+ envelope（DROP）—— 源序在后，违反 §5
        {"type": "text",
         "text": "请继续修改\n<system-reminder>cwd=/repo</system-reminder>"},
    ],
}

_CLAUDE_CODE_REQ = {
    "model": "claude-opus-4-7",
    "max_tokens": 256,
    "system": [{"type": "text", "text": "You are Claude Code."}],
    "messages": [_CLAUDE_CODE_USER_MSG],
}


def test_hermes_multi_content_no_violation() -> None:
    harness = load_harness("hermes")
    ir = harness.parse(_CLAUDE_CODE_REQ, session_id="r-h",
                        engine="anthropic", model="claude-opus-4-7")
    assert_ir_invariants(ir)  # §5 invariant must hold

    # 排序后：PIN(q) 在前、FOLD(tr + system-reminder?)、DROP(env) 最后
    user_msg = ir.messages[0]
    bands = [b.band for b in user_msg.blocks]
    pin_ids = [b.id for b in user_msg.blocks if b.band is Band.PIN]
    fold_ids = [b.id for b in user_msg.blocks if b.band is Band.FOLD]
    # 至少有一个 PIN (用户的新提问) 和一个 FOLD (tool_result)
    assert pin_ids, f"missing PIN block: {bands}"
    assert fold_ids, f"missing FOLD block: {bands}"
    # PIN 必须出现在所有 FOLD 之前
    last_pin = max(i for i, b in enumerate(user_msg.blocks) if b.band is Band.PIN)
    first_fold = min(i for i, b in enumerate(user_msg.blocks) if b.band is Band.FOLD)
    assert last_pin < first_fold, \
        f"PIN appears after FOLD in user message: bands={[b.value for b in bands]}"
    print("✓ test_hermes_multi_content_no_violation")


def test_openclaw_multi_content_no_violation() -> None:
    harness = load_harness("openclaw")
    ir = harness.parse(_CLAUDE_CODE_REQ, session_id="r-o",
                        engine="anthropic", model="claude-opus-4-7")
    assert_ir_invariants(ir)
    print("✓ test_openclaw_multi_content_no_violation")


def test_bridge_accepts_reordered_message() -> None:
    """harness 修好之后，Bridge 构造不应再抛 StelaInvariantError。"""
    harness = load_harness("hermes")
    engine = load_engine("anthropic")
    ir = harness.parse(_CLAUDE_CODE_REQ, session_id="r-b",
                        engine="anthropic", model="claude-opus-4-7")
    # 任何构造异常都会从这里抛——bug 时这一行就是炸点
    bridge = Bridge(ir, engine)
    plan = bridge.mark()
    assert plan is not None
    print("✓ test_bridge_accepts_reordered_message")


def test_deeply_interleaved_content() -> None:
    """更病态的用例：text, tool_result, text, text, tool_result, text。
    每个 text 都会被切成 PIN+DROP，每个 tool_result 是 FOLD。
    源序：[PIN,DROP, FOLD, PIN,DROP, PIN,DROP, FOLD, PIN,DROP]
    必须重排成连续 pin*-fold*-drop*。"""
    req = {
        "model": "claude-opus-4-7",
        "max_tokens": 256,
        "system": [{"type": "text", "text": "agent"}],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "q1\nCurrent time: 2026"},
                {"type": "tool_result", "tool_use_id": "a", "content": "r1"},
                {"type": "text", "text": "q2\nCurrent time: 2026"},
                {"type": "text", "text": "q3"},
                {"type": "tool_result", "tool_use_id": "b", "content": "r2"},
                {"type": "text", "text": "q4\n<system-reminder>x</system-reminder>"},
            ],
        }],
    }
    harness = load_harness("hermes")
    ir = harness.parse(req, session_id="r-d", engine="anthropic")
    assert_ir_invariants(ir)

    msg = ir.messages[0]
    bands = [b.band.value for b in msg.blocks]
    # 严格按 pin* → fold* → drop* 划分
    assert bands == sorted(bands, key=lambda b: {"pin": 0, "fold": 1, "drop": 2}[b]), \
        f"bands not sorted: {bands}"
    print(f"✓ test_deeply_interleaved_content (bands={bands})")


def main() -> None:
    test_hermes_multi_content_no_violation()
    test_openclaw_multi_content_no_violation()
    test_bridge_accepts_reordered_message()
    test_deeply_interleaved_content()
    print("\nall band-order regression tests passed.")


if __name__ == "__main__":
    main()
