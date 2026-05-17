"""端到端 demo：OpenClaw 风格请求 → TELOS IR → 三种 engine 各自的 wire 请求。

运行方式：
    python -m telos.demo
"""

from __future__ import annotations

import json

from telos import Bridge, load_engine, load_harness


# 一个简化的"OpenClaw 风格"请求
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
        # 模拟一段大文档（>2KB → 自动进 ref-pool）
        {"type": "text", "text": "AUTH SPEC:\n" + ("规则细节…\n" * 400)},
    ],
    "messages": [
        {
            "role": "user",
            "content": [{"type": "text", "text": (
                "请基于上文重构 login.py。\n"
                "<environment_info>cwd=/repo, branch=main, dirty=3 files</environment_info>\n"
                "Current time: 2026-05-06 14:32:07"
            )}],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "好的，我先读取文件。"},
                {"type": "tool_use", "id": "toolu_01", "name": "Read",
                 "input": {"path": "login.py"}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_01",
                         "content": [{"type": "text", "text": "<login.py 内容…>"}]}],
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
    print("\n--- IR layout (band 分布) ---")
    print(bridge.dump_layout())

    plan = bridge.mark()
    print(f"\n--- Mark plan ({len(plan.slots)} slots, routing_key={plan.routing_key}) ---")
    for s in plan.slots:
        print(f"  {s.name:8s}  {s.segment}[{s.index}]"
              f"  msg={s.message_index}  ttl={s.ttl_class}")

    wire = bridge.emit()
    # 只打前 600 字符以免刷屏
    rendered = json.dumps(wire, ensure_ascii=False, indent=2)
    print(f"\n--- Wire request (前 600 字符) ---\n{rendered[:600]}\n…")

    # 模拟一次回流：engine 返回的 usage
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
    print(f"\n--- 归一化 usage ---\n{report}")

    # 仅对开源推理引擎演示双向操作
    if bridge.is_bidirectional:
        print("\n--- 双向操作演示 ---")
        probe = bridge.probe_cache()
        print(f"  probe → hit={probe.hit} cached={probe.cached_token_count} tier={probe.tier}")
        # 协同 fold：把 msg[1..3] 折成一段摘要，并拿到 server 端 cache 控制片段
        if len(bridge.snapshot_ir().messages) >= 3:
            ctrl = bridge.cooperative_fold(
                message_range=(1, 3),
                summary="<上一轮 Read login.py 已完成，文件已读>",
            )
            print(f"  cooperative_fold → 服务端 cache 指令片段：{ctrl}")
            wire2 = bridge.emit_with_extras(ctrl)
            ext_field = "cache_policy" if engine_name == "vllm" else "cache_control"
            print(f"  下次 emit 中的 {ext_field}：")
            print(f"    {json.dumps(wire2.get(ext_field, {}), ensure_ascii=False, indent=2)}")


def main() -> None:
    for name in ("anthropic", "openai", "deepseek", "vllm", "sglang"):
        run_for_engine(name)


if __name__ == "__main__":
    main()
