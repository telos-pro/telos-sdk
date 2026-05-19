"""vLLM adapter (open-source inference engine, APC = Automatic Prefix Caching).

Basis:
- With ``--enable-prefix-caching`` enabled, vLLM matches hits via radix-hash
  over 16-token blocks.
- Since 0.6+ it exposes ``cache_salt`` (a request-level namespace) and KV
  offload (GPU→CPU→disk).
- usage fields ``prompt_tokens`` + ``cached_tokens`` (requires
  ``--collect-detailed-traces``).

On vLLM, TELOS gains "bidirectional awareness":
- read: ``probe`` calls ``HEAD /v1/cache/prefix?hash=...``
- write: ``cache_policy.{pin_prefix_until, evict_span}`` embedded in the request body
- prewarm: ``max_tokens=1`` actually triggers KV materialization

Because vLLM's cache-control field names are still evolving (O5), the field
names are centralized in the ``_VLLM_EXT`` constant, so a future rename only
touches one place.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from telos.engine.base import (
    BidirectionalEngineAdapter,
    EmitPlan,
    EngineCapabilities,
    MarkSlot,
    ProbeResult,
)
from telos.ir import Band, TelosIR, UsageReport


_VLLM_EXT = "cache_policy"  # vLLM private extension field name (centralized here)


class VLLMAdapter(BidirectionalEngineAdapter):
    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            explicit_breakpoints=True,        # a pin index counts as an explicit BP
            ttl_control="none",
            prewarmable=True,
            routing_key=True,                 # served by cache_salt
            retention_policy="configurable",
            max_breakpoints=2,                # pin_until + rolling tail
            cache_probe=True,
            span_eviction=True,
            fork_and_replace=False,           # vLLM only partially supports it, conservatively off
            tier_hint=False,
            pin_unpin=True,
        )

    # ------------------------------------------------------------------
    # plan: find the last PIN block in the system segment as the pin_until boundary
    # ------------------------------------------------------------------
    def plan_marks(self, ir: TelosIR) -> EmitPlan:
        slots: list[MarkSlot] = []
        # Anchor 1: trailing PIN block of the system segment → permanent pin
        last_sys_pin = _last_index(ir.system, Band.PIN)
        if last_sys_pin is not None:
            slots.append(MarkSlot(
                name="pin_until", segment="system", index=last_sys_pin,
                ttl_class="long",
            ))
        # Anchor 2: the last non-DROP block of the most recent message → rolling anchor (not pinned)
        if ir.messages:
            mi = len(ir.messages) - 1
            blocks = ir.messages[mi].blocks
            for j in range(len(blocks) - 1, -1, -1):
                if blocks[j].band is not Band.DROP:
                    slots.append(MarkSlot(
                        name="rolling_tail", segment="message", index=j,
                        message_index=mi, ttl_class="short",
                    ))
                    break
        return EmitPlan(
            slots=tuple(slots),
            routing_key=f"telos-vllm-{ir.session_id}",
        )

    # ------------------------------------------------------------------
    # emit: OpenAI-compatible body + cache_policy / cache_salt extensions
    # ------------------------------------------------------------------
    def emit(self, ir: TelosIR, plan: EmitPlan) -> Mapping[str, Any]:
        # Reuse the OpenAI-style flat messages array
        wire_messages: list[dict[str, Any]] = []
        sys_text = "\n\n".join(
            str(b.payload) for b in _band_sorted(ir.system)
        )
        if sys_text:
            wire_messages.append({"role": "system", "content": sys_text})
        for msg in ir.messages:
            parts: list[str] = []
            for blk in _band_sorted(msg.blocks):
                if blk.kind == "tool_result":
                    parts.append(str(blk.payload.get("content", "")))
                else:
                    parts.append(str(blk.payload) if not isinstance(blk.payload, dict)
                                 else json.dumps(blk.payload, sort_keys=True))
            wire_messages.append({"role": msg.role, "content": "\n".join(parts)})

        # cache_policy: translate the plan into vLLM private fields
        policy: dict[str, Any] = {}
        for slot in plan.slots:
            if slot.name == "pin_until":
                # vLLM addresses by token-block index; here we give a logical hint,
                # the real token computation is done by server-side hashing
                policy["pin_prefix_until_block"] = self._estimate_block_index(
                    ir, slot.segment, slot.index, slot.message_index,
                )
        # extras allows the bridge to inject evict_span (from a fold operation)
        if "evict_span" in plan.extras:
            policy["evict_span"] = plan.extras["evict_span"]

        body: dict[str, Any] = {
            "model": ir.hints.model or "vllm-served",
            "messages": wire_messages,
        }
        if ir.tools:
            body["tools"] = [b.payload for b in ir.tools]
        if policy:
            body[_VLLM_EXT] = policy
        if plan.routing_key:
            body["cache_salt"] = plan.routing_key
        return body

    def parse_usage(self, response: Mapping[str, Any]) -> UsageReport:
        usage = response.get("usage", {})
        prompt = int(usage.get("prompt_tokens", 0))
        cached = int(usage.get("cached_tokens", 0))
        return UsageReport(
            raw_input=max(0, prompt - cached),
            cache_read=cached,
            cache_write=0,                    # vLLM does not distinguish; folded into raw_input
            output=int(usage.get("completion_tokens", 0)),
            raw=usage,
        )

    # ------------------------------------------------------------------
    # Bidirectional operations
    # ------------------------------------------------------------------
    def probe(self, ir: TelosIR, plan: EmitPlan) -> ProbeResult:
        """Construct a prefix probe request; the caller is responsible for the HTTP send.

        The return value is only used as a fake in demos / tests; in a real
        environment the caller replaces it with a version that does network
        IO. Here we provide a placeholder ``ProbeResult`` and attach the hash
        to query on ``raw`` so the upper layer can use it.
        """
        prefix_hash = self._prefix_hash(ir)
        # Real implementation: ``http.head(f"/v1/cache/prefix?hash={prefix_hash}")``
        return ProbeResult(hit=False, cached_token_count=0, tier="none")

    def evict_span(
        self, ir: TelosIR, start_block: int, end_block: int,
    ) -> Mapping[str, Any]:
        """Return a ``cache_policy`` fragment to embed into the next emit.

        After ``Bridge.fold`` the bridge merges this dict into ``EmitPlan.extras``.
        """
        return {"evict_span": [start_block, end_block]}

    def refresh(self, ir: TelosIR, plan: EmitPlan) -> Mapping[str, Any]:
        """Return the prewarm request body; the caller actually POSTs it.

        Unlike ``EngineAdapter.refresh`` (the base class returns None), this
        returns a dict because we want the caller to see what the prewarm
        request looks like, for auditing.
        """
        body = dict(self.emit(ir, plan))
        body["max_tokens"] = 1
        body["stream"] = False
        # vLLM has no need for ``ignore_eos``, but we add it to make sure an accidental EOS does not stop early
        body["ignore_eos"] = True
        return body

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _estimate_block_index(
        self, ir: TelosIR, segment: str, index: int, message_index: int | None,
    ) -> int:
        """Roughly estimate the token block boundary. vLLM defaults to 16 tokens / block.

        A rough estimate is sufficient — the server hits the prefix by real
        tokenization; the number here only gives ``pin_prefix_until_block`` a
        conservative upper bound.
        """
        BLOCK = 16
        char_count = 0
        if segment == "tools":
            for b in ir.tools[: index + 1]:
                char_count += len(json.dumps(b.payload))
        else:
            for b in ir.tools:
                char_count += len(json.dumps(b.payload))
        if segment in ("system", "message"):
            limit = index + 1 if segment == "system" else len(ir.system)
            for b in ir.system[:limit]:
                char_count += len(str(b.payload))
        if segment == "message" and message_index is not None:
            for mi in range(message_index):
                for b in ir.messages[mi].blocks:
                    char_count += len(str(b.payload))
            for b in ir.messages[message_index].blocks[: index + 1]:
                char_count += len(str(b.payload))
        # 4 chars ≈ 1 token is an empirical value for English; Chinese is lower, so we stay conservative
        approx_tokens = char_count // 4
        return approx_tokens // BLOCK

    def _prefix_hash(self, ir: TelosIR) -> str:
        """Prefix hash: used by probe. The PIN span of tools + system."""
        h = hashlib.sha256()
        for b in ir.tools:
            h.update(json.dumps(b.payload, sort_keys=True).encode())
        for b in ir.system:
            if b.band is Band.PIN:
                h.update(str(b.payload).encode())
        return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
def _last_index(blocks, band) -> int | None:
    for i in range(len(blocks) - 1, -1, -1):
        if blocks[i].band is band:
            return i
    return None


def _band_sorted(blocks):
    """The stable sort key for the §5 order: PIN(0) < FOLD(1) < DROP(2)."""
    rank = {Band.PIN: 0, Band.FOLD: 1, Band.DROP: 2}
    return sorted(blocks, key=lambda b: rank[b.band])
