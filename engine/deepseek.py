"""DeepSeek adapter（V3+）。

依据：
- DeepSeek context-caching 文档：硬盘缓存默认开启，无 API 控制面；
  prefix unit 在三类边界持久化——request boundary（user 输入末 / 模型
  输出末）、common-prefix detection、固定 token interval。
- 命中要求 prefix unit 完全匹配。
- 唯一可行的"政策"是 layout：让稳定大段集中在 system 末尾，让每条
  user message 内 PIN/FOLD 在前、DROP 在后，让 prefix unit 切在稳定
  边界上。
- ``parse_usage`` 直接读 ``prompt_cache_hit_tokens / prompt_cache_miss_tokens``。
"""

from __future__ import annotations

from typing import Any, Mapping

from stela.engine.base import EmitPlan, EngineAdapter, EngineCapabilities
from stela.ir import Band, StelaIR, UsageReport


class DeepSeekAdapter(EngineAdapter):
    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            explicit_breakpoints=False,
            ttl_control="none",
            prewarmable=False,
            routing_key=False,
            retention_policy="fixed",
            max_breakpoints=0,
        )

    def plan_marks(self, ir: StelaIR) -> EmitPlan:
        return EmitPlan()  # 完全无控制面

    def emit(self, ir: StelaIR, plan: EmitPlan) -> Mapping[str, Any]:
        # OpenAI-compatible chat/completions 形态
        # 关键：把所有 PIN/FOLD 集中到 system 头部、DROP 全部沉到 system 尾部
        # （DeepSeek 的 prefix unit 是 exact-match，DROP 必须放最后否则前缀漂移）
        ordered_system = sorted(
            ir.system, key=lambda b: 0 if b.band is not Band.DROP else 1,
        )
        system_text = "\n\n".join(str(b.payload) for b in ordered_system)

        wire_messages: list[dict[str, Any]] = [{"role": "system", "content": system_text}]
        for msg in ir.messages:
            ordered = sorted(msg.blocks, key=lambda b: 0 if b.band is not Band.DROP else 1)
            text_parts: list[str] = []
            for blk in ordered:
                if blk.kind == "text":
                    text_parts.append(str(blk.payload))
                elif blk.kind == "tool_result":
                    # DeepSeek 把 tool_result 作为 role=tool 的 message 处理；这里
                    # 简化为内联到 user 文本，与其文档示例 "<file content>\n问题" 一致
                    text_parts.append(str(blk.payload.get("content", "")))
                else:
                    text_parts.append(str(blk.payload))
            wire_messages.append({"role": msg.role, "content": "\n".join(text_parts)})

        return {
            "model": ir.hints.model or "deepseek-chat",
            "messages": wire_messages,
            "tools": [b.payload for b in ir.tools] if ir.tools else None,
        }

    def parse_usage(self, response: Mapping[str, Any]) -> UsageReport:
        usage = response.get("usage", {})
        hit = int(usage.get("prompt_cache_hit_tokens", 0))
        miss = int(usage.get("prompt_cache_miss_tokens", 0))
        return UsageReport(
            raw_input=miss,
            cache_read=hit,
            cache_write=0,           # DeepSeek 不单独计 write，含在 miss 价格里
            output=int(usage.get("completion_tokens", 0)),
            raw=usage,
        )
