"""DeepSeek adapter (V3+).

Basis:
- DeepSeek context-caching docs: disk caching is enabled by default with no
  API control plane; a prefix unit is persisted at three kinds of
  boundaries — request boundary (end of user input / end of model output),
  common-prefix detection, and a fixed token interval.
- A hit requires the prefix unit to match exactly.
- The only viable "policy" is layout: keep the stable large spans clustered
  at the end of the system message, and within each user message put
  PIN/FOLD first and DROP last, so the prefix unit is cut at stable
  boundaries.
- ``parse_usage`` reads ``prompt_cache_hit_tokens / prompt_cache_miss_tokens`` directly.
"""

from __future__ import annotations

from typing import Any, Mapping

from telos.engine.base import EmitPlan, EngineAdapter, EngineCapabilities
from telos.ir import Band, TelosIR, UsageReport


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

    def plan_marks(self, ir: TelosIR) -> EmitPlan:
        return EmitPlan()  # no control plane at all

    def emit(self, ir: TelosIR, plan: EmitPlan) -> Mapping[str, Any]:
        # OpenAI-compatible chat/completions shape
        # Key point: cluster all PIN/FOLD at the head of the system message and sink all DROP to its tail
        # (DeepSeek's prefix unit is exact-match; DROP must go last or the prefix drifts)
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
                    # DeepSeek treats tool_result as a role=tool message; here we
                    # simplify by inlining it into the user text, consistent with
                    # the doc example "<file content>\nquestion"
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
            cache_write=0,           # DeepSeek does not bill write separately; it is included in the miss price
            output=int(usage.get("completion_tokens", 0)),
            raw=usage,
        )
