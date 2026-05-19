"""SGLang adapter (open-source inference engine, RadixAttention + HiCache).

Basis:
- RadixAttention: a token-level radix-tree cache; the cache-aware scheduler
  reorders requests by prefix affinity.
- HiCache: a three-tier GPU/CPU/disk cache; a ``prefer_tier`` hint can be
  given in the request.
- usage fields ``prompt_tokens`` + ``cached_tokens``; HiCache additionally
  returns ``cache_hierarchy_breakdown: {gpu, cpu, disk}``.

A superset of vLLM:
- ``fork_and_replace`` is truly usable → ``Fold`` achieves "zero recomputation"
- ``affinity_key`` lets the CASS scheduler land same-prefix requests on the
  same worker
- ``tier_hint`` allows keeping PIN on the GPU and sinking FOLD to the CPU,
  avoiding mutual contention

Field names are centralized in the ``_SGLANG_EXT`` constant (to cope with O5 renames).
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


_SGLANG_EXT = "cache_control"


class SGLangAdapter(BidirectionalEngineAdapter):
    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            explicit_breakpoints=True,
            ttl_control="none",
            prewarmable=True,
            routing_key=True,                 # implemented by affinity_key
            retention_policy="configurable",
            max_breakpoints=2,
            cache_probe=True,
            span_eviction=True,
            fork_and_replace=True,            # fully supported
            tier_hint=True,                   # HiCache
            pin_unpin=True,
        )

    # ------------------------------------------------------------------
    def plan_marks(self, ir: TelosIR) -> EmitPlan:
        slots: list[MarkSlot] = []
        last_sys_pin = _last_index(ir.system, Band.PIN)
        if last_sys_pin is not None:
            slots.append(MarkSlot(
                name="lock_radix", segment="system", index=last_sys_pin,
                ttl_class="long",
            ))
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
        # affinity_key: hash the toolset + system PIN + ref-pool slug set into a single key
        affinity = self._affinity_key(ir)
        return EmitPlan(
            slots=tuple(slots),
            routing_key=affinity,
            extras={"path_hash": affinity},
        )

    # ------------------------------------------------------------------
    def emit(self, ir: TelosIR, plan: EmitPlan) -> Mapping[str, Any]:
        wire_messages: list[dict[str, Any]] = []
        sys_text = "\n\n".join(str(b.payload) for b in _band_sorted(ir.system))
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

        # cache_control: translate the plan into SGLang private fields
        ctrl: dict[str, Any] = {}
        for slot in plan.slots:
            if slot.name == "lock_radix":
                ctrl["lock_radix_path"] = True
                ctrl["path_hash"] = plan.extras.get("path_hash")
        # tier hint: give an overall preference based on the band distribution (conservative: default gpu)
        ctrl["prefer_tier"] = "gpu"
        if plan.routing_key:
            ctrl["affinity_key"] = plan.routing_key
        # allow the bridge to inject fork_from_path / replace_suffix / evict_span via extras
        for k in ("fork_from_path", "replace_suffix", "evict_span"):
            if k in plan.extras:
                ctrl[k] = plan.extras[k]

        body: dict[str, Any] = {
            "model": ir.hints.model or "sglang-served",
            "messages": wire_messages,
        }
        if ir.tools:
            body["tools"] = [b.payload for b in ir.tools]
        if ctrl:
            body[_SGLANG_EXT] = ctrl
        return body

    def parse_usage(self, response: Mapping[str, Any]) -> UsageReport:
        usage = response.get("usage", {})
        prompt = int(usage.get("prompt_tokens", 0))
        cached = int(usage.get("cached_tokens", 0))
        return UsageReport(
            raw_input=max(0, prompt - cached),
            cache_read=cached,
            cache_write=0,
            output=int(usage.get("completion_tokens", 0)),
            raw=usage,                        # contains cache_hierarchy_breakdown
        )

    # ------------------------------------------------------------------
    # Bidirectional operations
    # ------------------------------------------------------------------
    def probe(self, ir: TelosIR, plan: EmitPlan) -> ProbeResult:
        """Construct a radix lookup request; the caller actually sends ``POST /v1/cache/lookup``."""
        return ProbeResult(hit=False, cached_token_count=0, tier="none")

    def evict_span(
        self, ir: TelosIR, start_block: int, end_block: int,
    ) -> Mapping[str, Any]:
        return {"evict_span": [start_block, end_block]}

    def fork_and_replace(
        self,
        ir: TelosIR,
        path_hash: str,
        replace_suffix: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """``Fold``'s true zero-recomputation implementation.

        When the bridge performs a fold, it merges this return value into the
        plan.extras of the next emit:

            extras["fork_from_path"] = path_hash
            extras["replace_suffix"] = replace_suffix

        On receiving it, SGLang forks a new branch from the radix node
        ``path_hash`` and replaces the span after it directly with
        ``replace_suffix`` — the prefix KV stays unchanged, only the much
        shorter summary tail is recomputed.
        """
        return {
            "fork_from_path": path_hash,
            "replace_suffix": dict(replace_suffix),
        }

    def refresh(self, ir: TelosIR, plan: EmitPlan) -> Mapping[str, Any]:
        """``prewarm_only`` mode: do not schedule generation, only fill the radix path."""
        body = dict(self.emit(ir, plan))
        ctrl = dict(body.get(_SGLANG_EXT, {}))
        ctrl["prewarm_only"] = True
        body[_SGLANG_EXT] = ctrl
        body["max_tokens"] = 1
        body["stream"] = False
        return body

    # ------------------------------------------------------------------
    def _affinity_key(self, ir: TelosIR) -> str:
        h = hashlib.sha256()
        for b in ir.tools:
            h.update(json.dumps(b.payload, sort_keys=True).encode())
        for b in ir.system:
            if b.band is Band.PIN:
                h.update(str(b.payload).encode())
        for slug in sorted(ir.ref_pool):
            h.update(slug.encode())
        return f"telos-sgl-{h.hexdigest()[:16]}"


# ---------------------------------------------------------------------------
def _last_index(blocks, band) -> int | None:
    for i in range(len(blocks) - 1, -1, -1):
        if blocks[i].band is band:
            return i
    return None


def _band_sorted(blocks):
    rank = {Band.PIN: 0, Band.FOLD: 1, Band.DROP: 2}
    return sorted(blocks, key=lambda b: rank[b.band])
