"""STELA 协议的最小自检：跑 demo 中的请求，验证不变量、Mark slot、usage 解析。

运行方式：
    python -m stela.tests.test_smoke
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许直接 python -m stela.tests.test_smoke 运行
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from stela import Band, Bridge, StelaInvariantError  # noqa: E402
from stela import load_engine, load_harness          # noqa: E402
from stela.demo import RAW_REQUEST                   # noqa: E402
from stela.ir import assert_ir_invariants            # noqa: E402


def test_harness_band_split() -> None:
    """OpenClaw 用户消息必须切成 (PIN, DROP)，envelope 不能污染 PIN。"""
    harness = load_harness("openclaw")
    ir = harness.parse(RAW_REQUEST, session_id="t1", engine="anthropic",
                       model="claude-opus-4-7")
    user_msg = ir.messages[0]
    bands = [b.band for b in user_msg.blocks]
    assert Band.PIN in bands, f"user message missing PIN block: {bands}"
    assert Band.DROP in bands, f"user message missing DROP block: {bands}"
    assert bands.index(Band.PIN) < bands.index(Band.DROP), \
        f"PIN must precede DROP, got {bands}"
    # 任何 PIN block 的 payload 都不能含 envelope 模式
    for blk in user_msg.blocks:
        if blk.band is Band.PIN:
            assert "<environment_info>" not in str(blk.payload)
            assert "Current time:" not in str(blk.payload)
    print("✓ test_harness_band_split")


def test_refpool_lifts_large_doc() -> None:
    """大段 system 文本必须被搬到 ref-pool，留 [ref:...] 引用。"""
    harness = load_harness("openclaw")
    ir = harness.parse(RAW_REQUEST, session_id="t2", engine="anthropic")
    assert ir.ref_pool, "expected ref-pool entries for large system doc"
    # ref 引用应该出现在 system 段
    found_ref = any("[ref:" in str(b.payload) for b in ir.system)
    assert found_ref, "system should contain a [ref:...] pointer"
    print("✓ test_refpool_lifts_large_doc")


def test_anthropic_mark_slots() -> None:
    """Anthropic adapter 应给出 ≤4 slot，且 1h 必须排在 5m 前。"""
    harness = load_harness("openclaw")
    engine = load_engine("anthropic")
    ir = harness.parse(RAW_REQUEST, session_id="t3", engine="anthropic",
                       model="claude-opus-4-7")
    bridge = Bridge(ir, engine)
    plan = bridge.mark()
    assert len(plan.slots) <= 4
    # 长 TTL 的 slot 必须出现在短 TTL 之前（按 segment 顺序）
    seg_order = {"tools": 0, "system": 1, "message": 2}
    last_ttl = "long"
    for s in sorted(plan.slots, key=lambda s: (seg_order[s.segment], s.index)):
        if last_ttl == "long" and s.ttl_class == "short":
            last_ttl = "short"
        elif last_ttl == "short":
            assert s.ttl_class == "short", \
                f"1h slot {s.name} appears after a 5m slot — violates Anthropic ordering"
    print("✓ test_anthropic_mark_slots")


def test_emit_round_trip_three_engines() -> None:
    """五个 engine 都能把同一份 IR emit 成各自的 wire 请求且不抛异常。"""
    harness = load_harness("openclaw")
    for name, model in (
        ("anthropic", "claude-opus-4-7"),
        ("openai",    "gpt-5.1"),
        ("deepseek",  "deepseek-chat"),
        ("vllm",      "Qwen/Qwen3-32B"),
        ("sglang",    "deepseek-ai/DeepSeek-V3"),
    ):
        ir = harness.parse(RAW_REQUEST, session_id=f"t4-{name}", engine=name, model=model)
        bridge = Bridge(ir, load_engine(name))
        wire = bridge.emit()
        assert wire["model"]
        assert "messages" in wire or "input" in wire
    print("✓ test_emit_round_trip_three_engines")


def test_bidirectional_only_on_open_engines() -> None:
    """vLLM / SGLang 是 bidirectional；闭源三家都不是。"""
    harness = load_harness("openclaw")
    bidi = {"vllm", "sglang"}
    for name in ("anthropic", "openai", "deepseek", "vllm", "sglang"):
        ir = harness.parse(RAW_REQUEST, session_id=f"b-{name}", engine=name)
        bridge = Bridge(ir, load_engine(name))
        assert bridge.is_bidirectional == (name in bidi), \
            f"[{name}] is_bidirectional should be {name in bidi}"
        # probe 总能调用，闭源就直接返回 hit=False（no-op）
        probe = bridge.probe_cache()
        if name not in bidi:
            assert probe.hit is False
    print("✓ test_bidirectional_only_on_open_engines")


def test_cooperative_fold_emits_cache_control() -> None:
    """SGLang 的 fork-and-replace 必须把 cache_control 字段放进下次 wire。"""
    harness = load_harness("openclaw")
    ir = harness.parse(RAW_REQUEST, session_id="cf-sgl", engine="sglang",
                       model="deepseek-ai/DeepSeek-V3")
    bridge = Bridge(ir, load_engine("sglang"))
    ctrl = bridge.cooperative_fold(message_range=(1, 3), summary="<已折叠>")
    assert "fork_from_path" in ctrl, f"expected fork_from_path in {ctrl}"
    wire = bridge.emit_with_extras(ctrl)
    cc = wire.get("cache_control", {})
    assert "fork_from_path" in cc and "replace_suffix" in cc, \
        f"wire.cache_control missing fork fields: {cc}"
    print("✓ test_cooperative_fold_emits_cache_control")


def test_vllm_pin_cache_policy() -> None:
    """vLLM 的 pin_until 必须出现在 wire body 的 cache_policy 字段里。"""
    harness = load_harness("openclaw")
    ir = harness.parse(RAW_REQUEST, session_id="cf-vllm", engine="vllm",
                       model="Qwen/Qwen3-32B")
    bridge = Bridge(ir, load_engine("vllm"))
    wire = bridge.emit()
    policy = wire.get("cache_policy", {})
    assert "pin_prefix_until_block" in policy, \
        f"vLLM wire missing pin_prefix_until_block: {policy}"
    assert "cache_salt" in wire
    print("✓ test_vllm_pin_cache_policy")


def test_band_order_violation_caught() -> None:
    """手动构造一个违反 §5 的 IR，确认 bridge 会拒绝。"""
    from stela.ir import StelaBlock, StelaIR, StelaMessage, StelaHints

    bad = StelaIR(
        session_id="bad",
        tools=(),
        system=(),
        messages=(StelaMessage(role="user", blocks=(
            StelaBlock(id="d", band=Band.DROP, kind="text", payload="x"),
            StelaBlock(id="p", band=Band.PIN,  kind="text", payload="y"),  # PIN 在 DROP 后 → 违反
        )),),
        ref_pool={},
        hints=StelaHints(engine="anthropic"),
    )
    try:
        assert_ir_invariants(bad)
    except StelaInvariantError:
        print("✓ test_band_order_violation_caught")
        return
    raise AssertionError("expected StelaInvariantError")


def test_usage_normalization() -> None:
    """三个 engine 的 usage 都能归一到 (raw_input, cache_read, cache_write)。"""
    harness = load_harness("openclaw")
    cases = [
        ("anthropic", {"usage": {"input_tokens": 80, "cache_read_input_tokens": 1000,
                                  "cache_creation_input_tokens": 50, "output_tokens": 10}},
         (80, 1000, 50)),
        ("openai",    {"usage": {"prompt_tokens": 1100,
                                  "prompt_tokens_details": {"cached_tokens": 1000},
                                  "completion_tokens": 10}},
         (100, 1000, 0)),
        ("deepseek",  {"usage": {"prompt_cache_hit_tokens": 1000,
                                  "prompt_cache_miss_tokens": 100,
                                  "completion_tokens": 10}},
         (100, 1000, 0)),
        ("vllm",      {"usage": {"prompt_tokens": 1100, "cached_tokens": 1000,
                                  "completion_tokens": 10}},
         (100, 1000, 0)),
        ("sglang",    {"usage": {"prompt_tokens": 1100, "cached_tokens": 1000,
                                  "completion_tokens": 10}},
         (100, 1000, 0)),
    ]
    for name, response, expected in cases:
        ir = harness.parse(RAW_REQUEST, session_id=f"u-{name}", engine=name)
        bridge = Bridge(ir, load_engine(name))
        report = bridge.absorb_usage(response)
        got = (report.raw_input, report.cache_read, report.cache_write)
        assert got == expected, f"[{name}] expected {expected}, got {got}"
    print("✓ test_usage_normalization")


def main() -> None:
    test_harness_band_split()
    test_refpool_lifts_large_doc()
    test_anthropic_mark_slots()
    test_emit_round_trip_three_engines()
    test_bidirectional_only_on_open_engines()
    test_cooperative_fold_emits_cache_control()
    test_vllm_pin_cache_policy()
    test_band_order_violation_caught()
    test_usage_normalization()
    print("\nall smoke tests passed.")


if __name__ == "__main__":
    main()
