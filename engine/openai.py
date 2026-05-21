"""OpenAI adapter (Responses API / gpt-5+ / gpt-4.1).

Basis:
- prompt-caching docs: automatic prefix cache (caching kicks in at
  ≥ 1024 tokens), ``prompt_cache_key`` affects routing,
  ``prompt_cache_retention: "24h"`` optional extended retention
  (gpt-5.x / gpt-4.1).
- **No explicit BP / TTL control**, so TELOS's policy relies entirely on
  layout and the routing key.
- Fix R1: the ``prompt_cache_key`` granularity rule is the other way
  around — the OpenAI docs explicitly state that *exceeding* 15 RPM/key
  causes overflow, so a single key should stay **under** 15 RPM.
  Implementation strategy: make the key "fine enough to cover prefix
  differences, but add a worker-id shard if the RPM estimate runs too
  high". Here is a simple version: ``hash(toolset, system_pin, refslugs)``,
  and ``shard()`` is exposed so the upper layer can add shards itself when
  it observes overheating.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from telos.engine.base import EmitPlan, EngineAdapter, EngineCapabilities
from telos.ir import Band, TelosIR, UsageReport


# when a key is observed approaching this value, the upper layer should call shard()
KEY_RPM_SOFT_CAP = 12  # leaves a 25% buffer below the OpenAI docs' ~15 RPM


class OpenAIAdapter(EngineAdapter):
    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            explicit_breakpoints=False,
            ttl_control="presets",          # in_memory vs 24h, pick one
            prewarmable=False,
            routing_key=True,
            retention_policy="configurable",
            max_breakpoints=0,
        )

    # ------------------------------------------------------------------
    # plan_marks: OpenAI has no BP, the only "policy" is the routing key
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
        # OpenAI has no separate system-segment configuration; concatenate
        # the TELOS system into a single leading system message, with the
        # DROP part placed last but still within the system message
        # (it still occupies cache, but OpenAI's automatic prefix matching
        # will skip the trailing mismatch).
        system_pieces = [str(b.payload) for b in ir.system if b.band is not Band.DROP]
        system_drop = [str(b.payload) for b in ir.system if b.band is Band.DROP]
        system_text = "\n\n".join(system_pieces + system_drop)

        wire_messages = [{"role": "system", "content": system_text}]
        for msg in ir.messages:
            # Likewise: flatten the blocks within a message into a single text,
            # PIN/FOLD first and DROP last, so OpenAI's prefix matcher hits in the middle.
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
        # OpenAI does not split read / write; cached_tokens all count as read, write is always 0
        # (the cache write on the first miss is done implicitly inside OpenAI, with no extra pricing)
        return UsageReport(
            raw_input=max(prompt_tokens - cached, 0),
            cache_read=cached,
            cache_write=0,
            output=int(usage.get("completion_tokens", 0)),
            raw=usage,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _derive_cache_key(self, ir: TelosIR) -> str:
        """hash(toolset, system_pin_payload, ref_slug_set) → routing affinity key."""
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
        """24h is only enabled when ``expected_turns >= 4`` (and only on the supported models listed in the docs)."""
        supported = {
            "gpt-5", "gpt-5-codex", "gpt-5.1", "gpt-5.1-codex",
            "gpt-5.1-codex-mini", "gpt-5.1-chat-latest", "gpt-5.1-codex-max",
            "gpt-5.2", "gpt-5.4", "gpt-5.5", "gpt-5.5-pro", "gpt-4.1",
        }
        if ir.hints.model in supported and ir.hints.expected_turns >= 4:
            return "24h"
        return None

    def shard(self, base_key: str, worker_id: int) -> str:
        """Called when a single key's RPM is observed approaching ``KEY_RPM_SOFT_CAP``, to shard the hot key."""
        return f"{base_key}-w{worker_id}"


def _render_block_for_openai(blk):
    if blk.kind == "text":
        return {"type": "text", "text": str(blk.payload)}
    if blk.kind == "image":
        # Fix R5 / R6: ``detail`` must be stable, taken from extra
        return {"type": "image", "image_url": blk.payload, **dict(blk.extra)}
    if blk.kind == "tool_use":
        return {"type": "tool_use", **blk.payload}
    if blk.kind == "tool_result":
        return {"type": "tool_result", **blk.payload}
    return {"type": blk.kind, "data": blk.payload}
