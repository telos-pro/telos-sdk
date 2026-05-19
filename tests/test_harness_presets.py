"""Unit tests for harness preset / alias support (no network).

Covers:
- alias resolution in ``registry.canonical_harness`` / ``load_harness``
- factory construction of each ``TelosTransport`` preset (no requests sent)
- the proxy pipeline normalizing ``result.harness`` when an alias is passed as ``harness_name``
"""

from __future__ import annotations

import os

from telos import PRESETS, HarnessPreset, TelosTransport
from telos.harness.hermes import HermesPlugin
from telos.harness.openclaw import OpenClawPlugin
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
    # non-aliases are returned unchanged
    assert canonical_harness("openclaw") == "openclaw"
    assert canonical_harness("hermes") == "hermes"
    assert canonical_harness("telos") == "telos"
    print("✓ test_canonical_harness_resolves_aliases")


def test_load_harness_accepts_aliases() -> None:
    assert isinstance(load_harness("claude-code"), HermesPlugin)
    assert isinstance(load_harness("openclaw"), OpenClawPlugin)
    print("✓ test_load_harness_accepts_aliases")


def test_harness_display_name() -> None:
    # canonical name → friendly display name
    assert harness_display_name("hermes") == "Claude Code"
    assert harness_display_name("openclaw") == "OpenClaw"
    assert harness_display_name("telos") == "Telos"
    # aliases are normalized first, then mapped
    assert harness_display_name("claude-code") == "Claude Code"
    # a non-harness name (proxy's pseudo-harness) is returned unchanged
    assert harness_display_name("passthrough") == "passthrough"
    assert harness_display_name("rtk-only") == "rtk-only"
    assert harness_display_name("?") == "?"
    assert harness_display_name("") == ""
    print("✓ test_harness_display_name")


def test_presets_cover_all_harnesses() -> None:
    assert set(PRESETS) == {"openclaw", "hermes", "claude-code"}
    for name, preset in PRESETS.items():
        assert isinstance(preset, HarnessPreset)
        assert preset.wire_protocol == "anthropic"
        assert preset.description
    # claude-code is an alias preset of hermes
    assert PRESETS["claude-code"].harness_name == "hermes"
    print("✓ test_presets_cover_all_harnesses")


def test_transport_for_harness_builds_each_preset() -> None:
    # no network request is sent; only verifies the construction path + that the duck interface is wired correctly.
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

    for name in ("openclaw", "hermes", "claude-code"):
        t = TelosTransport.for_harness(name)
        assert hasattr(t, "messages"), f"{name} should expose the Anthropic interface"
        assert t.preset.wire_protocol == "anthropic"
    print("✓ test_transport_for_harness_builds_each_preset")


def test_transport_for_harness_rejects_unknown() -> None:
    try:
        TelosTransport.for_harness("does-not-exist")
    except ValueError as e:
        assert "does-not-exist" in str(e)
    else:
        raise AssertionError("an unknown harness should raise ValueError")
    print("✓ test_transport_for_harness_rejects_unknown")


def test_pipeline_canonicalizes_alias_override() -> None:
    # the caller passes the alias claude-code; result.harness should be normalized to hermes,
    # ensuring the usage log / dashboard stay consistent with the auto-detected hermes.
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
