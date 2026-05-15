"""``stela.proxy.pipeline`` 的纯函数单测（无网络）。"""

from __future__ import annotations

from stela.bridge import BridgeSessionState
from stela.proxy.pipeline import process_anthropic_request
from stela.scripts.stela_anthropic_transport import _detect_harness


_OPENCLAW_REQ = {
    "model": "claude-opus-4-7",
    "max_tokens": 1024,
    "tools": [
        {"name": "Read", "input_schema": {"type": "object",
                                            "properties": {"path": {"type": "string"}}}},
    ],
    "system": [{"type": "text", "text": "You are an engineer agent."}],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "Read login.py"}]},
    ],
    "stream": False,
}

_HERMES_REQ = {
    "model": "claude-opus-4-7",
    "max_tokens": 1024,
    "system": [{"type": "text",
                "text": "You are Claude Code. <system-reminder>cwd=/repo</system-reminder>"}],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "Fix bug"}]},
    ],
}


def test_pipeline_detects_openclaw() -> None:
    r = process_anthropic_request(_OPENCLAW_REQ, session_id="t-oc")
    assert r.harness == "openclaw"
    assert r.wire["model"] == "claude-opus-4-7"
    assert r.wire["max_tokens"] == 1024
    assert r.wire.get("stream") is False
    print("✓ test_pipeline_detects_openclaw")


def test_pipeline_detects_hermes() -> None:
    r = process_anthropic_request(_HERMES_REQ, session_id="t-h")
    assert r.harness == "hermes"
    print("✓ test_pipeline_detects_hermes")


def test_pipeline_injects_cache_control() -> None:
    """tools 段或 system 段必须出现 cache_control（Anthropic BP 标记）。"""
    r = process_anthropic_request(_OPENCLAW_REQ, session_id="t-cc")
    bins = list(r.wire.get("tools", [])) + list(r.wire.get("system", []))
    assert any("cache_control" in b for b in bins), \
        f"wire 没有任何 cache_control 标记: {r.wire}"
    assert r.plan_slots, "EmitPlan 应至少返回一个 slot"
    print(f"✓ test_pipeline_injects_cache_control (slots={r.plan_slots})")


def test_pipeline_explicit_harness_override() -> None:
    r = process_anthropic_request(_HERMES_REQ, session_id="t-ov",
                                   harness_name="openclaw")
    assert r.harness == "openclaw", \
        "显式 harness_name 应覆盖自动检测"
    print("✓ test_pipeline_explicit_harness_override")


def test_pipeline_passthrough_fields() -> None:
    req = dict(_OPENCLAW_REQ)
    req["temperature"] = 0.7
    req["top_p"] = 0.95
    req["stop_sequences"] = ["</done>"]
    r = process_anthropic_request(req, session_id="t-pt")
    assert r.wire["temperature"] == 0.7
    assert r.wire["top_p"] == 0.95
    assert r.wire["stop_sequences"] == ["</done>"]
    print("✓ test_pipeline_passthrough_fields")


def test_detect_hermes_marker_in_user_message() -> None:
    """Claude Code 把 ``<system-reminder>`` 注入到 user message 而非 system。

    旧实现只扫 system 段，会把这种请求误判成 openclaw。
    """
    req = {
        "model": "claude-opus-4-7",
        "max_tokens": 1024,
        "system": [{"type": "text", "text": "You are an assistant."}],
        "messages": [
            {"role": "user", "content": [
                {"type": "text",
                 "text": "Fix the bug.\n<system-reminder>cwd=/repo</system-reminder>"},
            ]},
        ],
    }
    assert _detect_harness(req) == "hermes"
    print("✓ test_detect_hermes_marker_in_user_message")


def test_detect_hermes_command_name_in_user_message() -> None:
    """``<command-name>`` 也是 Hermes envelope（slash command 面板）。"""
    req = {
        "model": "claude-opus-4-7",
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "<command-name>/init</command-name>"},
            ]},
        ],
    }
    assert _detect_harness(req) == "hermes"
    print("✓ test_detect_hermes_command_name_in_user_message")


def test_detect_hermes_via_tool_fingerprint() -> None:
    """首轮无 reminder、无 thinking 时，Claude Code 的 tool 集合也是强指纹。"""
    req = {
        "model": "claude-opus-4-7",
        "tools": [
            {"name": "Bash", "input_schema": {"type": "object"}},
            {"name": "Read", "input_schema": {"type": "object"}},
            {"name": "Edit", "input_schema": {"type": "object"}},
            {"name": "Write", "input_schema": {"type": "object"}},
        ],
        "system": [{"type": "text", "text": "You are Claude Code."}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Hi"}]},
        ],
    }
    assert _detect_harness(req) == "hermes"
    print("✓ test_detect_hermes_via_tool_fingerprint")


def test_detect_openclaw_when_no_signals() -> None:
    """既没有 envelope 标签、没有 thinking、tools 又不像 Claude Code → openclaw。"""
    req = {
        "model": "claude-opus-4-7",
        "tools": [{"name": "search_api", "input_schema": {"type": "object"}}],
        "system": [{"type": "text", "text": "Be helpful."}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
        ],
    }
    assert _detect_harness(req) == "openclaw"
    print("✓ test_detect_openclaw_when_no_signals")


def test_detect_substring_no_false_positive() -> None:
    """裸字符串 ``<system-reminder>`` 不带闭合标签时不应触发 hermes 判定。"""
    req = {
        "model": "claude-opus-4-7",
        "messages": [
            {"role": "user", "content": [
                {"type": "text",
                 "text": "Discussing the <system-reminder> tag in our docs."},
            ]},
        ],
    }
    assert _detect_harness(req) == "openclaw"
    print("✓ test_detect_substring_no_false_positive")


def test_sticky_harness_across_calls() -> None:
    """同一 session_state 下，首轮识别结果应被锁定，避免后续翻转。"""
    state = BridgeSessionState()
    # 第一轮：典型的 Claude Code 请求
    hermes_req = {
        "model": "claude-opus-4-7",
        "max_tokens": 1024,
        "system": [{"type": "text", "text": "Claude Code system."}],
        "messages": [
            {"role": "user", "content": [
                {"type": "text",
                 "text": "Help.\n<system-reminder>x</system-reminder>"},
            ]},
        ],
    }
    r1 = process_anthropic_request(hermes_req, session_id="t-sticky",
                                     session_state=state)
    assert r1.harness == "hermes"
    assert state.sticky_harness == "hermes"

    # 第二轮：构造一个会被裸检测识别成 openclaw 的请求（无任何 hermes 指纹）
    follow_up = {
        "model": "claude-opus-4-7",
        "max_tokens": 1024,
        "system": [{"type": "text", "text": "Continue."}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Next step"}]},
        ],
    }
    r2 = process_anthropic_request(follow_up, session_id="t-sticky",
                                     session_state=state)
    # session 已锁定 → 应仍是 hermes
    assert r2.harness == "hermes", \
        f"sticky_harness 应让后续 call 仍走 hermes，得到 {r2.harness}"
    print("✓ test_sticky_harness_across_calls")


def test_explicit_harness_overrides_sticky() -> None:
    """显式 harness_name 必须能覆盖 sticky_harness。"""
    state = BridgeSessionState(sticky_harness="hermes")
    req = dict(_OPENCLAW_REQ)
    r = process_anthropic_request(req, session_id="t-ov2",
                                    session_state=state,
                                    harness_name="openclaw")
    assert r.harness == "openclaw"
    # 显式覆盖时**不**改写 sticky_harness（保留 session 的"自然"识别）
    assert state.sticky_harness == "hermes"
    print("✓ test_explicit_harness_overrides_sticky")


def main() -> None:
    test_pipeline_detects_openclaw()
    test_pipeline_detects_hermes()
    test_pipeline_injects_cache_control()
    test_pipeline_explicit_harness_override()
    test_pipeline_passthrough_fields()
    test_detect_hermes_marker_in_user_message()
    test_detect_hermes_command_name_in_user_message()
    test_detect_hermes_via_tool_fingerprint()
    test_detect_openclaw_when_no_signals()
    test_detect_substring_no_false_positive()
    test_sticky_harness_across_calls()
    test_explicit_harness_overrides_sticky()
    print("\nall pipeline tests passed.")


if __name__ == "__main__":
    main()
