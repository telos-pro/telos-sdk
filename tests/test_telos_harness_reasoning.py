"""Regression: telos harness must preserve ``reasoning_content`` on assistant
messages.

DeepSeek (v4-flash and similar thinking models) and OpenAI's o-series require
the previous turn's ``reasoning_content`` to be echoed back in subsequent
requests. If telos's IR drops it, the upstream returns HTTP 400 with:

    "The reasoning_content in the thinking mode must be passed back to the API."

This test verifies the parse → IR → chat_completions round-trip preserves
``reasoning_content`` byte-for-byte.
"""

from __future__ import annotations

from telos.proxy.pipeline import process_openai_request


def _multi_turn_with_reasoning() -> dict:
    """A small conversation with a thinking-model assistant in the middle."""
    return {
        "model": "deepseek-reasoner",
        "messages": [
            {"role": "system", "content": "You are concise."},
            {"role": "user", "content": "What is 2+2?"},
            {
                "role": "assistant",
                "content": "4",
                "reasoning_content": "The user asked basic arithmetic. 2 + 2 = 4.",
            },
            {"role": "user", "content": "And 3+3?"},
        ],
        "max_tokens": 32,
    }


def test_reasoning_content_round_trip() -> None:
    raw = _multi_turn_with_reasoning()
    result = process_openai_request(raw, session_id="r1")
    wire = result.wire
    assert len(wire["messages"]) == 4  # system + 2 user + 1 assistant

    assistant_msgs = [m for m in wire["messages"] if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    am = assistant_msgs[0]
    assert am["content"] == "4"
    assert am["reasoning_content"] == "The user asked basic arithmetic. 2 + 2 = 4."
    print("✓ test_reasoning_content_round_trip")


def test_reasoning_content_with_tool_calls_round_trip() -> None:
    """Assistant has both reasoning_content AND tool_calls (the openclaw failure
    pattern we hit on deepseek-v4-flash).
    """
    raw = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "Help the user."},
            {"role": "user", "content": "Read IDENTITY.md"},
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "User wants me to read a file. I'll call the read tool.",
                "tool_calls": [{
                    "id": "call_xyz",
                    "type": "function",
                    "function": {
                        "name": "read",
                        "arguments": '{"path":"IDENTITY.md"}',
                    },
                }],
            },
            {"role": "tool", "tool_call_id": "call_xyz", "content": "file body"},
            {"role": "user", "content": "Now read USER.md"},
        ],
        "tools": [{
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        }],
    }
    result = process_openai_request(raw, session_id="r2")
    wire = result.wire
    asm = next(m for m in wire["messages"]
                if m["role"] == "assistant" and m.get("tool_calls"))
    assert asm["reasoning_content"] == (
        "User wants me to read a file. I'll call the read tool."
    )
    assert len(asm["tool_calls"]) == 1
    assert asm["tool_calls"][0]["id"] == "call_xyz"
    # role=tool round-trips correctly (separate concern; covered for safety).
    tool_msgs = [m for m in wire["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_xyz"
    print("✓ test_reasoning_content_with_tool_calls_round_trip")


def test_assistant_without_reasoning_unchanged() -> None:
    """An assistant without reasoning_content must not gain the field on the wire."""
    raw = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "ok"},
        ],
    }
    result = process_openai_request(raw, session_id="r3")
    asm = next(m for m in result.wire["messages"] if m["role"] == "assistant")
    assert "reasoning_content" not in asm
    print("✓ test_assistant_without_reasoning_unchanged")


def test_empty_reasoning_content_dropped() -> None:
    """An empty reasoning_content string must not produce an empty wire field
    (deepseek may also reject the empty string)."""
    raw = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "hello",
                "reasoning_content": "",
            },
            {"role": "user", "content": "ok"},
        ],
    }
    result = process_openai_request(raw, session_id="r4")
    asm = next(m for m in result.wire["messages"] if m["role"] == "assistant")
    assert "reasoning_content" not in asm
    print("✓ test_empty_reasoning_content_dropped")


def main() -> None:
    test_reasoning_content_round_trip()
    test_reasoning_content_with_tool_calls_round_trip()
    test_assistant_without_reasoning_unchanged()
    test_empty_reasoning_content_dropped()
    print("\nall reasoning_content round-trip tests passed.")


if __name__ == "__main__":
    main()
