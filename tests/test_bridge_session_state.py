"""``BridgeSessionState`` 跨 turn 累积语义的回归测试。

验证三件事：
1. ref-pool slug 第二轮 register 是幂等的（不抛错）
2. fold 状态跨 turn 保持（不被新一轮的完整内容覆盖）
3. R8 计数器跨 turn 累加
4. ``cumulative_cache_creation`` 跨 turn 累加
5. 缺省 (不传 ``session_state``) 时退化到每轮独立 —— 保后向兼容
"""

from __future__ import annotations

from telos import Bridge, Band, load_engine, load_harness
from telos.bridge import BridgeSessionState


def _make_req_with_large_doc(turn: int) -> dict:
    """每轮请求都带同一个大 system 文档（会进 ref-pool）。"""
    return {
        "model": "claude-opus-4-7",
        "max_tokens": 64,
        "system": [
            {"type": "text", "text": "You are an engineer agent."},
            # >2KB 文本，harness 会自动搬到 ref-pool
            {"type": "text", "text": "AUTH SPEC:\n" + ("规则细节…\n" * 400)},
        ],
        "messages": [
            {"role": "user", "content": [{"type": "text",
                "text": f"Turn {turn} question."}]},
        ],
    }


def test_refpool_persists_across_turns() -> None:
    """同一 session_state 喂多轮：slug 注册一次，第二轮不抛。"""
    state = BridgeSessionState()
    harness = load_harness("openclaw")
    engine = load_engine("anthropic")

    for turn in range(3):
        ir = harness.parse(
            _make_req_with_large_doc(turn),
            session_id="multi-turn", engine="anthropic",
        )
        bridge = Bridge(ir, engine, session_state=state)
        bridge.mark()

    # 三轮共享同一个 RefPool；slug 集只有一份
    assert len(state.refpool.slugs) == 1, \
        f"ref-pool 应只注册一次，实际：{state.refpool.slugs}"
    print(f"✓ test_refpool_persists_across_turns (slug={list(state.refpool.slugs)})")


def test_fold_survives_across_turns() -> None:
    """第一轮 fold 后，第二轮 harness 重新提供完整 payload 也不会还原。"""
    state = BridgeSessionState()
    harness = load_harness("openclaw")
    engine = load_engine("anthropic")

    # 第一轮：拿到 slug 后 fold 掉
    ir1 = harness.parse(_make_req_with_large_doc(0), session_id="s", engine="anthropic")
    bridge1 = Bridge(ir1, engine, session_state=state)
    slug = next(iter(state.refpool.slugs))
    bridge1.fold(slugs=(slug,))

    # 检查：ref-pool entry 已变成 placeholder
    folded_block = state.refpool._entries[slug]
    assert "folded" in str(folded_block.payload).lower()

    # 第二轮：harness 重新喂同样的 IR（含完整 payload）
    ir2 = harness.parse(_make_req_with_large_doc(1), session_id="s", engine="anthropic")
    bridge2 = Bridge(ir2, engine, session_state=state)

    # 关键：第二轮的 Bridge __init__ 应该用 register_or_skip，不覆盖
    still_folded = state.refpool._entries[slug]
    assert "folded" in str(still_folded.payload).lower(), \
        f"fold 状态被新一轮 IR 覆盖回完整内容了：{still_folded.payload[:80]}"
    print("✓ test_fold_survives_across_turns")


def test_r8_counter_accumulates() -> None:
    """emit_with_plan 每次 +1 ``real_requests_since_refresh``。"""
    state = BridgeSessionState()
    harness = load_harness("openclaw")
    engine = load_engine("anthropic")

    for turn in range(5):
        ir = harness.parse(_make_req_with_large_doc(turn),
                            session_id="r8", engine="anthropic")
        bridge = Bridge(ir, engine, session_state=state)
        bridge.emit_with_plan()

    assert state.stats.real_requests_since_refresh == 5, \
        f"期望 5，实际 {state.stats.real_requests_since_refresh}"
    print(f"✓ test_r8_counter_accumulates ({state.stats.real_requests_since_refresh})")


def test_cumulative_cache_creation_via_absorb_usage() -> None:
    """``bridge.absorb_usage`` 把 cache_write tokens 累加进 state。"""
    state = BridgeSessionState()
    harness = load_harness("openclaw")
    engine = load_engine("anthropic")

    fake_responses = [
        {"usage": {"input_tokens": 100, "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 5000, "output_tokens": 200}},
        {"usage": {"input_tokens": 50, "cache_read_input_tokens": 4000,
                    "cache_creation_input_tokens": 1500, "output_tokens": 100}},
        {"usage": {"input_tokens": 30, "cache_read_input_tokens": 5500,
                    "cache_creation_input_tokens": 0, "output_tokens": 80}},
    ]

    for turn, resp in enumerate(fake_responses):
        ir = harness.parse(_make_req_with_large_doc(turn),
                            session_id="cum", engine="anthropic")
        bridge = Bridge(ir, engine, session_state=state)
        bridge.emit_with_plan()
        bridge.absorb_usage(resp)

    assert state.stats.cumulative_cache_creation == 5000 + 1500 + 0, \
        f"期望 6500，实际 {state.stats.cumulative_cache_creation}"
    print(f"✓ test_cumulative_cache_creation_via_absorb_usage "
          f"({state.stats.cumulative_cache_creation})")


def test_default_state_is_fresh_per_bridge() -> None:
    """不传 session_state 时，每个 Bridge 独立持有状态 —— 后向兼容。"""
    harness = load_harness("openclaw")
    engine = load_engine("anthropic")

    fake = {"usage": {"input_tokens": 1, "cache_read_input_tokens": 0,
                       "cache_creation_input_tokens": 9999, "output_tokens": 1}}

    for _ in range(3):
        ir = harness.parse(_make_req_with_large_doc(0),
                            session_id="iso", engine="anthropic")
        bridge = Bridge(ir, engine)  # ← 不传 session_state
        bridge.emit_with_plan()
        bridge.absorb_usage(fake)
        # 每个新 Bridge 都是从 0 开始
        assert bridge.session_state.stats.real_requests_since_refresh == 1
        assert bridge.session_state.stats.cumulative_cache_creation == 9999
    print("✓ test_default_state_is_fresh_per_bridge")


def test_explicit_state_shared_across_bridges() -> None:
    """同一 state 给两个 Bridge：能看到对方的写入。"""
    state = BridgeSessionState()
    harness = load_harness("openclaw")
    engine = load_engine("anthropic")

    ir1 = harness.parse(_make_req_with_large_doc(0),
                         session_id="x", engine="anthropic")
    b1 = Bridge(ir1, engine, session_state=state)
    b1.emit_with_plan()
    b1.absorb_usage({"usage": {"input_tokens": 1, "cache_read_input_tokens": 0,
                                 "cache_creation_input_tokens": 100, "output_tokens": 1}})

    ir2 = harness.parse(_make_req_with_large_doc(1),
                         session_id="x", engine="anthropic")
    b2 = Bridge(ir2, engine, session_state=state)
    # b2 还没 emit 就应能看到 b1 写入的累计值
    assert b2.session_state.stats.cumulative_cache_creation == 100
    assert b2.session_state.stats.real_requests_since_refresh == 1
    print("✓ test_explicit_state_shared_across_bridges")


def main() -> None:
    test_refpool_persists_across_turns()
    test_fold_survives_across_turns()
    test_r8_counter_accumulates()
    test_cumulative_cache_creation_via_absorb_usage()
    test_default_state_is_fresh_per_bridge()
    test_explicit_state_shared_across_bridges()
    print("\nall bridge session-state tests passed.")


if __name__ == "__main__":
    main()
