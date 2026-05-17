"""OpenAI adapter（Responses API / gpt-5+ / gpt-4.1）。

依据：
- prompt-caching 文档：自动 prefix cache（≥ 1024 token 起 cache）、
  ``prompt_cache_key`` 影响路由、``prompt_cache_retention: "24h"`` 可选
  扩展保留（gpt-5.x / gpt-4.1）。
- **没有显式 BP / TTL 控制**，TELOS 的政策完全靠 layout 与 routing key。
- 修复 R1：``prompt_cache_key`` 粒度规则反过来——OpenAI 文档明示
  *超过* 15 RPM/key 会 overflow，所以应当让单个 key **不超过** 15 RPM。
  实现策略：把 key 取得"足够细以覆盖前缀差异，但若 RPM 估计过高再加
  worker-id 切片"。这里给一个简版：``hash(toolset, system_pin, refslugs)``，
  并暴露 ``shard()`` 让上层在监控到过热时自行加切片。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from telos.engine.base import EmitPlan, EngineAdapter, EngineCapabilities
from telos.ir import Band, TelosIR, UsageReport


# 监控到 key 接近这个值时上层应调用 shard()
KEY_RPM_SOFT_CAP = 12  # 预留 25% buffer 到 OpenAI 文档的 ~15 RPM


class OpenAIAdapter(EngineAdapter):
    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            explicit_breakpoints=False,
            ttl_control="presets",          # in_memory vs 24h，二选一
            prewarmable=False,
            routing_key=True,
            retention_policy="configurable",
            max_breakpoints=0,
        )

    # ------------------------------------------------------------------
    # plan_marks：OpenAI 没有 BP，唯一"政策"是 routing key
    # ------------------------------------------------------------------

    def plan_marks(self, ir: TelosIR) -> EmitPlan:
        return EmitPlan(
            slots=(),
            routing_key=self._derive_cache_key(ir),
            extras={"retention": self._choose_retention(ir)},
        )

    # ------------------------------------------------------------------
    # emit
    # ------------------------------------------------------------------

    def emit(self, ir: TelosIR, plan: EmitPlan) -> Mapping[str, Any]:
        # OpenAI 没有独立的 system 段配置；把 TELOS system 拼成一条
        # leading system message，DROP 的部分放最后但仍在 system 内
        # （仍占 cache 但 OpenAI 自动 prefix 会跳过尾部 mismatch）。
        system_pieces = [str(b.payload) for b in ir.system if b.band is not Band.DROP]
        system_drop = [str(b.payload) for b in ir.system if b.band is Band.DROP]
        system_text = "\n\n".join(system_pieces + system_drop)

        wire_messages = [{"role": "system", "content": system_text}]
        for msg in ir.messages:
            # 同样：把一条 message 内的 blocks 平铺成单一文本，
            # PIN/FOLD 在前、DROP 在后，让 OpenAI prefix matcher 在中间命中。
            ordered = sorted(msg.blocks, key=lambda b: 0 if b.band is not Band.DROP else 1)
            content_parts: list[Any] = []
            for blk in ordered:
                content_parts.append(_render_block_for_openai(blk))
            wire_messages.append({"role": msg.role, "content": content_parts})

        wire: dict[str, Any] = {
            "model": ir.hints.model or "gpt-5",
            "input": wire_messages,
            "tools": [b.payload for b in ir.tools],
        }
        if plan.routing_key:
            wire["prompt_cache_key"] = plan.routing_key
        retention = plan.extras.get("retention")
        if retention:
            wire["prompt_cache_retention"] = retention
        return wire

    # ------------------------------------------------------------------
    # parse_usage
    # ------------------------------------------------------------------

    def parse_usage(self, response: Mapping[str, Any]) -> UsageReport:
        usage = response.get("usage", {})
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        cached = int(usage.get("prompt_tokens_details", {}).get("cached_tokens", 0))
        # OpenAI 不分 read / write，cached_tokens 全算 read；write 一律记 0
        # （首次 miss 的 cache write 由 OpenAI 内部隐式做，pricing 未额外计费）
        return UsageReport(
            raw_input=max(prompt_tokens - cached, 0),
            cache_read=cached,
            cache_write=0,
            output=int(usage.get("completion_tokens", 0)),
            raw=usage,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _derive_cache_key(self, ir: TelosIR) -> str:
        """hash(toolset, system_pin_payload, ref_slug_set) → routing 亲和键。"""
        h = hashlib.sha256()
        for blk in ir.tools:
            h.update(json.dumps(blk.payload, sort_keys=True).encode())
        for blk in ir.system:
            if blk.band is Band.PIN:
                h.update(b"\x00pin:")
                h.update(str(blk.payload).encode())
        for slug in sorted(ir.ref_pool.keys()):
            h.update(b"\x00ref:")
            h.update(slug.encode())
        return f"telos-{h.hexdigest()[:16]}"

    def _choose_retention(self, ir: TelosIR) -> str | None:
        """``expected_turns >= 4`` 才开 24h（仅在 docs 列出的支持模型上）。"""
        supported = {
            "gpt-5", "gpt-5-codex", "gpt-5.1", "gpt-5.1-codex",
            "gpt-5.1-codex-mini", "gpt-5.1-chat-latest", "gpt-5.1-codex-max",
            "gpt-5.2", "gpt-5.4", "gpt-5.5", "gpt-5.5-pro", "gpt-4.1",
        }
        if ir.hints.model in supported and ir.hints.expected_turns >= 4:
            return "24h"
        return None

    def shard(self, base_key: str, worker_id: int) -> str:
        """监控到单 key RPM 接近 ``KEY_RPM_SOFT_CAP`` 时调用，把热 key 切片。"""
        return f"{base_key}-w{worker_id}"


def _render_block_for_openai(blk):
    if blk.kind == "text":
        return {"type": "text", "text": str(blk.payload)}
    if blk.kind == "image":
        # 修复 R5 / R6：``detail`` 必须稳定，从 extra 取
        return {"type": "image", "image_url": blk.payload, **dict(blk.extra)}
    if blk.kind == "tool_use":
        return {"type": "tool_use", **blk.payload}
    if blk.kind == "tool_result":
        return {"type": "tool_result", **blk.payload}
    return {"type": blk.kind, "data": blk.payload}
