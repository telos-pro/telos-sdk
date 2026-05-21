"""Regression tests for the cross-turn accumulation semantics of ``BridgeSessionState``.

Verifies the following:
1. registering a ref-pool slug on the second turn is idempotent (does not raise)
2. fold state is preserved across turns (not overwritten by the new turn's full content)
3. the R8 counter accumulates across turns
4. ``cumulative_cache_creation`` accumulates across turns
5. by default (not passing ``session_state``) it degrades to per-turn independence -- preserving backward compatibility
"""

from __future__ import annotations

from telos import Bridge, Band, load_engine, load_harness
from telos.bridge import BridgeSessionState


def _make_req_with_large_doc(turn: int) -> dict:
    """Every request carries the same large system document (which goes into the ref-pool)."""
    return {
        "model": "claude-opus-4-7",
        "max_tokens": 64,
        "system": [
            {"type": "text", "text": "You are an engineer agent."},
            # >2KB of text, the harness will automatically move it to the ref-pool
            {"type": "text", "text": "AUTH SPEC:\n" + ("Rule detail line.\n" * 400)},
        ],
        "messages": [
            {"role": "user", "content": [{"type": "text",
                "text": f"Turn {turn} question."}]},
        ],
    }


def test_refpool_persists_across_turns() -> None:
    """Feed multiple turns into the same session_state: the slug is registered once, the second turn does not raise."""
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

    # the three turns share the same RefPool; the slug set has only one entry
    assert len(state.refpool.slugs) == 1, \
        f"ref-pool should be registered only once, got: {state.refpool.slugs}"
    print(f"✓ test_refpool_persists_across_turns (slug={list(state.refpool.slugs)})")


def test_fold_survives_across_turns() -> None:
    """After the first turn folds, the harness re-providing the full payload on the second turn does not restore it."""
    state = BridgeSessionState()
    harness = load_harness("openclaw")
    engine = load_engine("anthropic")

    # turn 1: fold it after getting the slug
    ir1 = harness.parse(_make_req_with_large_doc(0), session_id="s", engine="anthropic")
    bridge1 = Bridge(ir1, engine, session_state=state)
    slug = next(iter(state.refpool.slugs))
    bridge1.fold(slugs=(slug,))

    # check: the ref-pool entry has become a placeholder
    folded_block = state.refpool._entries[slug]
    assert "folded" in str(folded_block.payload).lower()

    # turn 2: the harness feeds the same IR again (with the full payload)
    ir2 = harness.parse(_make_req_with_large_doc(1), session_id="s", engine="anthropic")
    bridge2 = Bridge(ir2, engine, session_state=state)

    # key: the second turn's Bridge __init__ should use register_or_skip, not overwrite
    still_folded = state.refpool._entries[slug]
    assert "folded" in str(still_folded.payload).lower(), \
        f"fold state was overwritten back to full content by the new turn's IR: {still_folded.payload[:80]}"
    print("✓ test_fold_survives_across_turns")


def test_r8_counter_accumulates() -> None:
    """emit_with_plan adds +1 to ``real_requests_since_refresh`` each time."""
    state = BridgeSessionState()
    harness = load_harness("openclaw")
    engine = load_engine("anthropic")

    for turn in range(5):
        ir = harness.parse(_make_req_with_large_doc(turn),
                            session_id="r8", engine="anthropic")
        bridge = Bridge(ir, engine, session_state=state)
        bridge.emit_with_plan()

    assert state.stats.real_requests_since_refresh == 5, \
        f"expected 5, got {state.stats.real_requests_since_refresh}"
    print(f"✓ test_r8_counter_accumulates ({state.stats.real_requests_since_refresh})")


def test_cumulative_cache_creation_via_absorb_usage() -> None:
    """``bridge.absorb_usage`` accumulates cache_write tokens into the state."""
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
        f"expected 6500, got {state.stats.cumulative_cache_creation}"
    print(f"✓ test_cumulative_cache_creation_via_absorb_usage "
          f"({state.stats.cumulative_cache_creation})")


def test_default_state_is_fresh_per_bridge() -> None:
    """When session_state is not passed, each Bridge holds its own state -- backward compatibility."""
    harness = load_harness("openclaw")
    engine = load_engine("anthropic")

    fake = {"usage": {"input_tokens": 1, "cache_read_input_tokens": 0,
                       "cache_creation_input_tokens": 9999, "output_tokens": 1}}

    for _ in range(3):
        ir = harness.parse(_make_req_with_large_doc(0),
                            session_id="iso", engine="anthropic")
        bridge = Bridge(ir, engine)  # ← session_state not passed
        bridge.emit_with_plan()
        bridge.absorb_usage(fake)
        # each new Bridge starts from 0
        assert bridge.session_state.stats.real_requests_since_refresh == 1
        assert bridge.session_state.stats.cumulative_cache_creation == 9999
    print("✓ test_default_state_is_fresh_per_bridge")


def test_explicit_state_shared_across_bridges() -> None:
    """The same state given to two Bridges: each can see the other's writes."""
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
    # before b2 even emits, it should be able to see the cumulative value written by b1
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
