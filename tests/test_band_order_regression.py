"""回归测试：Claude Code / Hermes 风格的多 content-block user message。

§5 顺序不变量：message 段里 ``tool_result`` 块必须物理居首，其后
``pin* → fold* → drop*``。

历史 bug：harness 把 ``tool_result`` 定为 FOLD、用户提问 text 定为 PIN，
``enforce_band_order`` 按纯 band 排序会把 tool_result 排到 text 之后，
而 Anthropic 要求 user message 的 tool_result 物理居首 → API 400。
"""

from __future__ import annotations

from stela import Band, Bridge, load_engine, load_harness
from stela.ir import assert_ir_invariants
from stela.proxy.pipeline import process_anthropic_request


_CLAUDE_CODE_USER_MSG = {
    "role": "user",
    "content": [
        # 上一轮的 tool_result（FOLD）—— 源序在前
        {"type": "tool_result", "tool_use_id": "toolu_x",
         "content": [{"type": "text", "text": "<file content...>"}]},
        # 本轮新提问（PIN）+ envelope（DROP）
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


def _assert_tool_result_first(msg, where: str) -> None:
    """断言一条 StelaMessage 里 tool_result 块都排在非 tool_result 之前。"""
    kinds = [b.kind for b in msg.blocks]
    tr_idx = [i for i, k in enumerate(kinds) if k == "tool_result"]
    if tr_idx:
        assert tr_idx == list(range(len(tr_idx))), \
            f"{where}: tool_result not contiguous-at-front: {kinds}"


def test_hermes_tool_result_stays_first() -> None:
    harness = load_harness("hermes")
    ir = harness.parse(_CLAUDE_CODE_REQ, session_id="r-h",
                        engine="anthropic", model="claude-opus-4-7")
    assert_ir_invariants(ir)  # §5 invariant must hold

    user_msg = ir.messages[0]
    # tool_result 必须是首个 block —— Anthropic 协议硬约束
    assert user_msg.blocks[0].kind == "tool_result", \
        f"first block must be tool_result, got {[b.kind for b in user_msg.blocks]}"
    _assert_tool_result_first(user_msg, "hermes user_msg")
    # tool_result 仍是 FOLD band（band 归属不变，只改物理顺序）
    assert user_msg.blocks[0].band is Band.FOLD
    print("✓ test_hermes_tool_result_stays_first")


def test_openclaw_tool_result_stays_first() -> None:
    harness = load_harness("openclaw")
    ir = harness.parse(_CLAUDE_CODE_REQ, session_id="r-o",
                        engine="anthropic", model="claude-opus-4-7")
    assert_ir_invariants(ir)
    assert ir.messages[0].blocks[0].kind == "tool_result"
    print("✓ test_openclaw_tool_result_stays_first")


def test_wire_user_message_leads_with_tool_result() -> None:
    """端到端：跑完整 STELA 管线，wire 里 user message 必须 tool_result 居首。

    这是 Anthropic 400 "tool use concurrency" 的直接回归——修复前 wire 会把
    tool_result 排到 text 之后。
    """
    res = process_anthropic_request(
        _CLAUDE_CODE_REQ, session_id="r-w", session_state=None, harness_name=None,
    )
    for i, m in enumerate(res.wire["messages"]):
        content = m.get("content")
        if not isinstance(content, list):
            continue
        types = [b.get("type") for b in content if isinstance(b, dict)]
        tr_idx = [j for j, t in enumerate(types) if t == "tool_result"]
        if tr_idx:
            assert tr_idx == list(range(len(tr_idx))), \
                f"wire msg{i}: tool_result not first: {types}"
    print("✓ test_wire_user_message_leads_with_tool_result")


def test_bridge_accepts_reordered_message() -> None:
    """Bridge 构造不应抛 StelaInvariantError。"""
    harness = load_harness("hermes")
    engine = load_engine("anthropic")
    ir = harness.parse(_CLAUDE_CODE_REQ, session_id="r-b",
                        engine="anthropic", model="claude-opus-4-7")
    bridge = Bridge(ir, engine)
    plan = bridge.mark()
    assert plan is not None
    print("✓ test_bridge_accepts_reordered_message")


def test_deeply_interleaved_content() -> None:
    """病态用例：text, tool_result, text, text, tool_result, text。

    每个 text 切成 PIN+DROP，每个 tool_result 是 FOLD。重排后必须是
    tool_result* 居首，其后非 tool_result 块按 pin* → fold* → drop*。
    """
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
    _assert_tool_result_first(msg, "interleaved user_msg")
    # 非 tool_result 块仍严格 pin* → fold* → drop*
    rest = [b.band.value for b in msg.blocks if b.kind != "tool_result"]
    assert rest == sorted(rest, key=lambda b: {"pin": 0, "fold": 1, "drop": 2}[b]), \
        f"non-tool_result bands not sorted: {rest}"
    kinds = [b.kind for b in msg.blocks]
    print(f"✓ test_deeply_interleaved_content (kinds={kinds})")


def main() -> None:
    test_hermes_tool_result_stays_first()
    test_openclaw_tool_result_stays_first()
    test_wire_user_message_leads_with_tool_result()
    test_bridge_accepts_reordered_message()
    test_deeply_interleaved_content()
    print("\nall band-order regression tests passed.")


if __name__ == "__main__":
    main()
