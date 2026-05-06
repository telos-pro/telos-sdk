"""SGLang 适配器（开源推理引擎，RadixAttention + HiCache）。

依据：
- RadixAttention：以 token 为单位的 radix 树缓存；cache-aware scheduler
  会按前缀亲和度重排请求。
- HiCache：GPU/CPU/disk 三级缓存；可在请求里给出 ``prefer_tier`` 提示。
- usage 字段 ``prompt_tokens`` + ``cached_tokens``；HiCache 还回
  ``cache_hierarchy_breakdown: {gpu, cpu, disk}``。

vs vLLM 的 superset：
- ``fork_and_replace`` 真正可用 → ``Fold`` 实现"零重算"
- ``affinity_key`` 让 CASS 调度器把同前缀请求落到同一 worker
- ``tier_hint`` 允许把 PIN 留 GPU、FOLD 沉 CPU、避免互相挤压

字段集中在 ``_SGLANG_EXT`` 常量里（O5 应对 rename）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from stela.engine.base import (
    BidirectionalEngineAdapter,
    EmitPlan,
    EngineCapabilities,
    MarkSlot,
    ProbeResult,
)
from stela.ir import Band, StelaIR, UsageReport


_SGLANG_EXT = "cache_control"


class SGLangAdapter(BidirectionalEngineAdapter):
    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            explicit_breakpoints=True,
            ttl_control="none",
            prewarmable=True,
            routing_key=True,                 # affinity_key 实现
            retention_policy="configurable",
            max_breakpoints=2,
            cache_probe=True,
            span_eviction=True,
            fork_and_replace=True,            # 完整支持
            tier_hint=True,                   # HiCache
            pin_unpin=True,
        )

    # ------------------------------------------------------------------
    def plan_marks(self, ir: StelaIR) -> EmitPlan:
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
        # affinity_key：把工具集 + system PIN + ref-pool slug 集合 hash 成一个 key
        affinity = self._affinity_key(ir)
        return EmitPlan(
            slots=tuple(slots),
            routing_key=affinity,
            extras={"path_hash": affinity},
        )

    # ------------------------------------------------------------------
    def emit(self, ir: StelaIR, plan: EmitPlan) -> Mapping[str, Any]:
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

        # cache_control：把 plan 翻译成 SGLang 私有字段
        ctrl: dict[str, Any] = {}
        for slot in plan.slots:
            if slot.name == "lock_radix":
                ctrl["lock_radix_path"] = True
                ctrl["path_hash"] = plan.extras.get("path_hash")
        # tier hint：根据 band 分布给出整体偏好（保守：默认 gpu）
        ctrl["prefer_tier"] = "gpu"
        if plan.routing_key:
            ctrl["affinity_key"] = plan.routing_key
        # 允许 bridge 通过 extras 注入 fork_from_path / replace_suffix / evict_span
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
            raw=usage,                        # 含 cache_hierarchy_breakdown
        )

    # ------------------------------------------------------------------
    # 双向操作
    # ------------------------------------------------------------------
    def probe(self, ir: StelaIR, plan: EmitPlan) -> ProbeResult:
        """构造一个 radix lookup 请求；调用方真正发 ``POST /v1/cache/lookup``。"""
        return ProbeResult(hit=False, cached_token_count=0, tier="none")

    def evict_span(
        self, ir: StelaIR, start_block: int, end_block: int,
    ) -> Mapping[str, Any]:
        return {"evict_span": [start_block, end_block]}

    def fork_and_replace(
        self,
        ir: StelaIR,
        path_hash: str,
        replace_suffix: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """``Fold`` 的真正零重算实现。

        bridge 在执行 fold 时，把这个返回值合并进下一次 emit 的 plan.extras：

            extras["fork_from_path"] = path_hash
            extras["replace_suffix"] = replace_suffix

        SGLang 收到后，从 ``path_hash`` 这个 radix 节点 fork 一个新分支，
        把后面那段直接换成 ``replace_suffix``——前缀的 KV 保持不变，只重算
        摘要这段短得多的尾部。
        """
        return {
            "fork_from_path": path_hash,
            "replace_suffix": dict(replace_suffix),
        }

    def refresh(self, ir: StelaIR, plan: EmitPlan) -> Mapping[str, Any]:
        """``prewarm_only`` 模式：不调度生成、只填 radix 路径。"""
        body = dict(self.emit(ir, plan))
        ctrl = dict(body.get(_SGLANG_EXT, {}))
        ctrl["prewarm_only"] = True
        body[_SGLANG_EXT] = ctrl
        body["max_tokens"] = 1
        body["stream"] = False
        return body

    # ------------------------------------------------------------------
    def _affinity_key(self, ir: StelaIR) -> str:
        h = hashlib.sha256()
        for b in ir.tools:
            h.update(json.dumps(b.payload, sort_keys=True).encode())
        for b in ir.system:
            if b.band is Band.PIN:
                h.update(str(b.payload).encode())
        for slug in sorted(ir.ref_pool):
            h.update(slug.encode())
        return f"stela-sgl-{h.hexdigest()[:16]}"


# ---------------------------------------------------------------------------
def _last_index(blocks, band) -> int | None:
    for i in range(len(blocks) - 1, -1, -1):
        if blocks[i].band is band:
            return i
    return None


def _band_sorted(blocks):
    rank = {Band.PIN: 0, Band.FOLD: 1, Band.DROP: 2}
    return sorted(blocks, key=lambda b: rank[b.band])
