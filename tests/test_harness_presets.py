"""Harness preset / 别名支持的单测（无网络）。

覆盖：
- ``registry.canonical_harness`` / ``load_harness`` 的别名解析
- ``TelosTransport`` 各 preset 的工厂构造（不发请求）
- proxy pipeline 在 ``harness_name`` 传别名时把 ``result.harness`` 归一化
"""

from __future__ import annotations

import os

from telos import PRESETS, HarnessPreset, TelosTransport
from telos.harness.hermes import HermesPlugin
from telos.harness.openclaw import OpenClawPlugin
from telos.harness.telos import TelosPlugin
from telos.proxy.pipeline import process_anthropic_request
from telos.registry import canonical_harness, harness_display_name, load_harness


_HERMES_REQ = {
    "model": "claude-opus-4-7",
    "max_tokens": 1024,
    "system": [{"type": "text",
                "text": "You are Claude Code. <system-reminder>cwd=/repo</system-reminder>"}],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "Fix bug"}]},
    ],
}


def test_canonical_harness_resolves_aliases() -> None:
    assert canonical_harness("claude-code") == "hermes"
    assert canonical_harness("deepseek-cli") == "telos"
    # 非别名原样返回
    assert canonical_harness("openclaw") == "openclaw"
    assert canonical_harness("hermes") == "hermes"
    print("✓ test_canonical_harness_resolves_aliases")


def test_load_harness_accepts_aliases() -> None:
    assert isinstance(load_harness("claude-code"), HermesPlugin)
    assert isinstance(load_harness("deepseek-cli"), TelosPlugin)
    assert isinstance(load_harness("openclaw"), OpenClawPlugin)
    print("✓ test_load_harness_accepts_aliases")


def test_harness_display_name() -> None:
    # canonical 名 → 友好展示名
    assert harness_display_name("hermes") == "Claude Code"
    assert harness_display_name("openclaw") == "OpenClaw"
    assert harness_display_name("telos") == "Telos"
    # 别名先归一化再映射
    assert harness_display_name("claude-code") == "Claude Code"
    assert harness_display_name("deepseek-cli") == "Telos"
    # 非 harness 名（proxy 的伪 harness）原样返回
    assert harness_display_name("passthrough") == "passthrough"
    assert harness_display_name("rtk-only") == "rtk-only"
    assert harness_display_name("?") == "?"
    assert harness_display_name("") == ""
    print("✓ test_harness_display_name")


def test_presets_cover_all_harnesses() -> None:
    assert set(PRESETS) == {"openclaw", "hermes", "claude-code", "deepseek-cli"}
    for name, preset in PRESETS.items():
        assert isinstance(preset, HarnessPreset)
        assert preset.wire_protocol in ("anthropic", "openai")
        assert preset.description
    # claude-code 是 hermes 的别名 preset
    assert PRESETS["claude-code"].harness_name == "hermes"
    print("✓ test_presets_cover_all_harnesses")


def test_transport_for_harness_builds_each_preset() -> None:
    # 不发网络请求，只验证构造路径 + 鸭子接口挂载正确。
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")

    for name in ("openclaw", "hermes", "claude-code"):
        t = TelosTransport.for_harness(name)
        assert hasattr(t, "messages"), f"{name} 应暴露 Anthropic 接口"
        assert t.preset.wire_protocol == "anthropic"

    t = TelosTransport.for_harness("deepseek-cli")
    assert hasattr(t, "chat"), "deepseek-cli 应暴露 OpenAI 接口"
    assert t.preset.wire_protocol == "openai"
    print("✓ test_transport_for_harness_builds_each_preset")


def test_transport_for_harness_rejects_unknown() -> None:
    try:
        TelosTransport.for_harness("does-not-exist")
    except ValueError as e:
        assert "does-not-exist" in str(e)
    else:
        raise AssertionError("未知 harness 应抛 ValueError")
    print("✓ test_transport_for_harness_rejects_unknown")


def test_pipeline_canonicalizes_alias_override() -> None:
    # 调用方传别名 claude-code，result.harness 应归一化成 hermes，
    # 保证 usage log / dashboard 与自动检测出的 hermes 一致。
    r = process_anthropic_request(
        _HERMES_REQ, session_id="t-alias", harness_name="claude-code",
    )
    assert r.harness == "hermes"
    print("✓ test_pipeline_canonicalizes_alias_override")


if __name__ == "__main__":
    test_canonical_harness_resolves_aliases()
    test_load_harness_accepts_aliases()
    test_harness_display_name()
    test_presets_cover_all_harnesses()
    test_transport_for_harness_builds_each_preset()
    test_transport_for_harness_rejects_unknown()
    test_pipeline_canonicalizes_alias_override()
    print("all harness-preset tests passed")
