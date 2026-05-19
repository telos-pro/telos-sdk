"""End-to-end demo: OpenClaw-style request → TELOS IR → the wire requests for three engines.

How to run:
    python -m telos.demo
"""

from __future__ import annotations

import json

from telos import Bridge, load_engine, load_harness


# A simplified "OpenClaw-style" request
RAW_REQUEST = {
    "model": "claude-opus-4-7",
    "tools": [
        {"name": "Read", "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"}}}},
        {"name": "Bash", "input_schema": {"type": "object", "properties": {
            "cmd": {"type": "string"}}}},
    ],
    "system": [
        {"type": "text", "text": "You are a senior engineer agent."},
        # Simulate a large document (>2KB → automatically goes into the ref-pool)
        {"type": "text", "text": "AUTH SPEC:\n" + ("rule details…\n" * 400)},
    ],
    "messages": [
        {
            "role": "user",
            "content": [{"type": "text", "text": (
                "Please refactor login.py based on the above.\n"
                "<environment_info>cwd=/repo, branch=main, dirty=3 files</environment_info>\n"
                "Current time: 2026-05-06 14:32:07"
            )}],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Sure, I'll read the file first."},
                {"type": "tool_use", "id": "toolu_01", "name": "Read",
                 "input": {"path": "login.py"}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_01",
                         "content": [{"type": "text", "text": "<contents of login.py…>"}]}],
        },
    ],
}


def run_for_engine(engine_name: str) -> None:
    print(f"\n{'='*60}\n[engine = {engine_name}]\n{'='*60}")
    harness = load_harness("openclaw")
    engine = load_engine(engine_name)

    ir = harness.parse(
        RAW_REQUEST,
        session_id=f"demo-{engine_name}",
        engine=engine_name,
        model={
            "anthropic": "claude-opus-4-7",
            "openai": "gpt-5.1",
            "deepseek": "deepseek-chat",
            "vllm": "Qwen/Qwen3-32B",
            "sglang": "deepseek-ai/DeepSeek-V3",
        }[engine_name],
        expected_turns=20,
    )

    bridge = Bridge(ir, engine)
    print("\n--- IR layout (band distribution) ---")
    print(bridge.dump_layout())

    plan = bridge.mark()
    print(f"\n--- Mark plan ({len(plan.slots)} slots, routing_key={plan.routing_key}) ---")
    for s in plan.slots:
        print(f"  {s.name:8s}  {s.segment}[{s.index}]"
              f"  msg={s.message_index}  ttl={s.ttl_class}")

    wire = bridge.emit()
    # Print only the first 600 chars to avoid flooding the screen
    rendered = json.dumps(wire, ensure_ascii=False, indent=2)
    print(f"\n--- Wire request (first 600 chars) ---\n{rendered[:600]}\n…")

    # Simulate a feedback loop: the usage returned by the engine
    fake_response = {
        "anthropic": {"usage": {"input_tokens": 80, "cache_read_input_tokens": 21043,
                                "cache_creation_input_tokens": 250, "output_tokens": 120}},
        "openai":    {"usage": {"prompt_tokens": 21373,
                                "prompt_tokens_details": {"cached_tokens": 20000},
                                "completion_tokens": 120}},
        "deepseek":  {"usage": {"prompt_cache_hit_tokens": 19000,
                                "prompt_cache_miss_tokens": 2373, "completion_tokens": 120}},
        "vllm":      {"usage": {"prompt_tokens": 21373, "cached_tokens": 20500,
                                "completion_tokens": 120}},
        "sglang":    {"usage": {"prompt_tokens": 21373, "cached_tokens": 20800,
                                "completion_tokens": 120,
                                "cache_hierarchy_breakdown": {
                                    "gpu": 18000, "cpu": 2800, "disk": 0}}},
    }[engine_name]
    report = bridge.absorb_usage(fake_response)
    print(f"\n--- normalized usage ---\n{report}")

    # Demonstrate bidirectional operations only for open-source inference engines
    if bridge.is_bidirectional:
        print("\n--- bidirectional operation demo ---")
        probe = bridge.probe_cache()
        print(f"  probe → hit={probe.hit} cached={probe.cached_token_count} tier={probe.tier}")
        # Cooperative fold: fold msg[1..3] into a summary, and obtain the server-side cache control fragment
        if len(bridge.snapshot_ir().messages) >= 3:
            ctrl = bridge.cooperative_fold(
                message_range=(1, 3),
                summary="<the previous turn's Read login.py is done, the file has been read>",
            )
            print(f"  cooperative_fold → server-side cache instruction fragment: {ctrl}")
            wire2 = bridge.emit_with_extras(ctrl)
            ext_field = "cache_policy" if engine_name == "vllm" else "cache_control"
            print(f"  {ext_field} in the next emit:")
            print(f"    {json.dumps(wire2.get(ext_field, {}), ensure_ascii=False, indent=2)}")


def main() -> None:
    for name in ("anthropic", "openai", "deepseek", "vllm", "sglang"):
        run_for_engine(name)


if __name__ == "__main__":
    main()
