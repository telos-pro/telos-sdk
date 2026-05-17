"""Anthropic adapter（Claude Opus / Sonnet 4.6+）。

依据：
- prompt-caching 文档：4 个显式 ``cache_control`` slot、5m / 1h TTL、
  ``max_tokens:0`` prewarm、20-block lookback、hierarchy
  ``tools → system → messages``、混合 TTL 时 1h 必须先于 5m。
- 修复 R2：长会话插入 mid-rolling 锚（19 轮一动），覆盖 lookback 窗口。
- 修复 R7：候选 > 4 时按 ``pin-tools > pin-system > ref-pool > rolling-mid > latest-turn``
  优先级砍。
"""

from __future__ import annotations

from typing import Any, Mapping

from telos.engine.base import EmitPlan, EngineAdapter, EngineCapabilities, MarkSlot
from telos.ir import Band, TelosIR, UsageReport


_LOOKBACK = 20         # Anthropic 文档明示的 lookback 上限
_MID_ANCHOR_STRIDE = 19  # 留 1 块 buffer，确保下次必能在窗口内找到上一个 BP


class AnthropicAdapter(EngineAdapter):
    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            explicit_breakpoints=True,
            ttl_control="presets",          # 仅 5m / 1h
            prewarmable=True,
            routing_key=False,
            retention_policy="fixed",
            max_breakpoints=4,
            thinking_preserved_across_non_tool_result=True,  # 默认按 4.6+ 走
        )

    # ------------------------------------------------------------------
    # plan_marks：4 个 slot 的优先级分配
    # ------------------------------------------------------------------

    def plan_marks(self, ir: TelosIR) -> EmitPlan:
        candidates: list[MarkSlot] = []

        # BP-T：tools 段末尾（仅当有 tools）
        if ir.tools:
            candidates.append(MarkSlot(
                name="BP-T", segment="tools",
                index=len(ir.tools) - 1, ttl_class="long",
            ))

        # BP-S：system 段中最后一个 PIN block（不含 ref-pool）
        last_pin_idx = _last_band_index(ir.system, Band.PIN)
        if last_pin_idx is not None:
            candidates.append(MarkSlot(
                name="BP-S", segment="system",
                index=last_pin_idx, ttl_class="long",
            ))

        # BP-R：system 段中最后一个 FOLD block（即 ref-pool 末尾）
        last_fold_idx = _last_band_index(ir.system, Band.FOLD)
        if last_fold_idx is not None:
            candidates.append(MarkSlot(
                name="BP-R", segment="system",
                index=last_fold_idx, ttl_class="long",
            ))

        # BP-X：最后一条 message 内最后一个非 DROP block（5m 滚动）
        if ir.messages:
            for msg_idx in range(len(ir.messages) - 1, -1, -1):
                last_keep = _last_non_drop_index(ir.messages[msg_idx].blocks)
                if last_keep is not None:
                    candidates.append(MarkSlot(
                        name="BP-X", segment="message",
                        index=last_keep, message_index=msg_idx, ttl_class="short",
                    ))
                    break

        # 修复 R2：会话长到一定程度（>= 19 个 message），插一个 mid-rolling 锚
        # 位置：当前 - 19 轮处的最后非 DROP block，TTL=5m
        if len(ir.messages) >= _MID_ANCHOR_STRIDE:
            mid_msg_idx = len(ir.messages) - _MID_ANCHOR_STRIDE
            last_keep = _last_non_drop_index(ir.messages[mid_msg_idx].blocks)
            if last_keep is not None:
                candidates.append(MarkSlot(
                    name="BP-mid", segment="message",
                    index=last_keep, message_index=mid_msg_idx, ttl_class="short",
                ))

        # 修复 R7：候选可能 > 4，按优先级砍
        priority = {"BP-T": 0, "BP-S": 1, "BP-R": 2, "BP-mid": 3, "BP-X": 4}
        candidates.sort(key=lambda s: priority.get(s.name, 99))
        slots = tuple(candidates[: self.capabilities.max_breakpoints])

        # 物理顺序：tools → system → message（emit 时按 segment+index 排序天然成立）
        # TTL 排序：长 TTL（1h）必须先于短 TTL（5m）—— 这由 segment 顺序保证
        return EmitPlan(slots=slots)

    # ------------------------------------------------------------------
    # emit：翻译成 Anthropic /v1/messages 请求
    # ------------------------------------------------------------------

    def emit(self, ir: TelosIR, plan: EmitPlan) -> Mapping[str, Any]:
        slot_index = _build_slot_index(plan)

        wire_tools = [
            self._render_tool(blk, slot_index.get(("tools", i)))
            for i, blk in enumerate(ir.tools)
        ]

        wire_system = []
        for i, blk in enumerate(ir.system):
            if blk.band is Band.DROP:
                # DROP 必须在所有 BP 之后；不挂 cache_control（§5）
                wire_system.append({"type": "text", "text": str(blk.payload)})
            else:
                wire_system.append(self._render_text_block(blk, slot_index.get(("system", i))))

        wire_messages = []
        for mi, msg in enumerate(ir.messages):
            content = []
            for bi, blk in enumerate(msg.blocks):
                key = ("message", bi, mi)
                slot = slot_index.get(key)
                content.append(self._render_message_block(blk, slot))
            wire_messages.append({"role": msg.role, "content": content})

        request: dict[str, Any] = {
            "model": ir.hints.model or "claude-opus-4-7",
            "tools": wire_tools,
            "system": wire_system,
            "messages": wire_messages,
        }
        return request

    # ------------------------------------------------------------------
    # refresh：max_tokens=0 prewarm
    # ------------------------------------------------------------------

    def refresh(self, ir: TelosIR, plan: EmitPlan) -> None:
        # 真实使用方应 POST 这个 dict；此处仅返回构造好的请求体，
        # 让上层（譬如 benchmark harness）决定如何发送。
        wire = self.emit(ir, plan)
        wire["max_tokens"] = 0
        wire["stream"] = False
        # tool_choice 必须为 auto；thinking 必须 disabled —— 见 Anthropic 文档限制
        wire["tool_choice"] = {"type": "auto"}
        wire.pop("thinking", None)
        # 真实实现：requests.post(...) ；这里 demo 只构造
        # 把构造好的请求体挂到一个 attribute 给测试 / 演示用
        self.last_refresh_request = wire

    # ------------------------------------------------------------------
    # parse_usage
    # ------------------------------------------------------------------

    def parse_usage(self, response: Mapping[str, Any]) -> UsageReport:
        usage = response.get("usage", {})
        return UsageReport(
            raw_input=int(usage.get("input_tokens", 0)),
            cache_read=int(usage.get("cache_read_input_tokens", 0)),
            cache_write=int(usage.get("cache_creation_input_tokens", 0)),
            output=int(usage.get("output_tokens", 0)),
            raw=usage,
        )

    # ------------------------------------------------------------------
    # 内部渲染辅助
    # ------------------------------------------------------------------

    def _render_tool(self, blk, slot: MarkSlot | None) -> dict[str, Any]:
        # 假设 tool_def payload 已是 {"name": ..., "input_schema": {...}}
        out: dict[str, Any] = dict(blk.payload)
        if slot is not None:
            out["cache_control"] = _cache_control_for(slot)
        return out

    def _render_text_block(self, blk, slot: MarkSlot | None) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "text", "text": str(blk.payload)}
        if slot is not None and blk.band is not Band.DROP:
            out["cache_control"] = _cache_control_for(slot)
        return out

    def _render_message_block(self, blk, slot: MarkSlot | None) -> dict[str, Any]:
        if blk.kind == "text":
            return self._render_text_block(blk, slot)
        if blk.kind == "tool_use":
            out = {"type": "tool_use", **blk.payload}
            if slot is not None:
                out["cache_control"] = _cache_control_for(slot)
            return out
        if blk.kind == "tool_result":
            out = {"type": "tool_result", **blk.payload}
            if slot is not None:
                out["cache_control"] = _cache_control_for(slot)
            return out
        if blk.kind == "thinking":
            # thinking 块不能直接挂 cache_control；slot 应该不会落在这里
            return {"type": "thinking", **blk.payload}
        if blk.kind == "image":
            # detail 字段必须稳定（必须从 extra 取，不能 emit 时再算）
            out = {"type": "image", "source": blk.payload, **dict(blk.extra)}
            return out
        raise ValueError(f"Unknown block kind: {blk.kind}")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _last_band_index(blocks, band: Band) -> int | None:
    for i in range(len(blocks) - 1, -1, -1):
        if blocks[i].band is band:
            return i
    return None


def _last_non_drop_index(blocks) -> int | None:
    for i in range(len(blocks) - 1, -1, -1):
        if blocks[i].band is not Band.DROP:
            return i
    return None


def _cache_control_for(slot: MarkSlot) -> dict[str, str]:
    if slot.ttl_class == "long":
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}  # 默认 5m


def _build_slot_index(plan: EmitPlan):
    """({segment, index}) 或 ({segment, block_index, message_index}) → MarkSlot"""
    out: dict[tuple, MarkSlot] = {}
    for s in plan.slots:
        if s.segment == "message":
            out[("message", s.index, s.message_index)] = s
        else:
            out[(s.segment, s.index)] = s
    return out
