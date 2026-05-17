"""vLLM 适配器（开源推理引擎，APC = Automatic Prefix Caching）。

依据：
- vLLM 启用 ``--enable-prefix-caching`` 后，按 16-token 块做 radix-hash 命中。
- 0.6+ 起暴露 ``cache_salt``（请求级命名空间）和 KV offload（GPU→CPU→disk）。
- usage 字段 ``prompt_tokens`` + ``cached_tokens``（需 ``--collect-detailed-traces``）。

TELOS 在 vLLM 上获得"双向感知"：
- read：``probe`` 调 ``HEAD /v1/cache/prefix?hash=...``
- write：``cache_policy.{pin_prefix_until, evict_span}`` 嵌进请求体
- prewarm：``max_tokens=1`` 真触发 KV 物化

由于 vLLM 的 cache 控制面字段名仍在演化（O5），这里把字段集中在
``_VLLM_EXT`` 常量里，未来 rename 只动一处。
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


_VLLM_EXT = "cache_policy"  # vLLM 私有扩展字段名（统一在此）


class VLLMAdapter(BidirectionalEngineAdapter):
    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            explicit_breakpoints=True,        # pin index 算显式 BP
            ttl_control="none",
            prewarmable=True,
            routing_key=True,                 # cache_salt 充当
            retention_policy="configurable",
            max_breakpoints=2,                # pin_until + rolling tail
            cache_probe=True,
            span_eviction=True,
            fork_and_replace=False,           # vLLM 仅部分支持，保守关掉
            tier_hint=False,
            pin_unpin=True,
        )

    # ------------------------------------------------------------------
    # plan：找到 system 段最后一个 PIN 块作为 pin_until 边界
    # ------------------------------------------------------------------
    def plan_marks(self, ir: TelosIR) -> EmitPlan:
        slots: list[MarkSlot] = []
        # 锚 1：system 段尾部 PIN 块 → 永久 pin
        last_sys_pin = _last_index(ir.system, Band.PIN)
        if last_sys_pin is not None:
            slots.append(MarkSlot(
                name="pin_until", segment="system", index=last_sys_pin,
                ttl_class="long",
            ))
        # 锚 2：最近一条 message 的最后一个非-DROP 块 → 滚动锚（不 pin）
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
    # emit：OpenAI-compatible body + cache_policy / cache_salt 扩展
    # ------------------------------------------------------------------
    def emit(self, ir: TelosIR, plan: EmitPlan) -> Mapping[str, Any]:
        # 复用 OpenAI 风格的扁平 messages 数组
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

        # cache_policy：把 plan 翻译成 vLLM 私有字段
        policy: dict[str, Any] = {}
        for slot in plan.slots:
            if slot.name == "pin_until":
                # vLLM 用 token-block index 寻址；这里给逻辑 hint，
                # 真实 token 计算由 server-side hashing 完成
                policy["pin_prefix_until_block"] = self._estimate_block_index(
                    ir, slot.segment, slot.index, slot.message_index,
                )
        # extras 里允许 bridge 注入 evict_span（来自 fold 操作）
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
            cache_write=0,                    # vLLM 不区分；并入 raw_input
            output=int(usage.get("completion_tokens", 0)),
            raw=usage,
        )

    # ------------------------------------------------------------------
    # 双向操作
    # ------------------------------------------------------------------
    def probe(self, ir: TelosIR, plan: EmitPlan) -> ProbeResult:
        """构造一个 prefix probe 请求；调用方负责 HTTP 发送。

        返回值仅在 demo / 测试里用 fake；真实环境会被 caller 替换为带网
        络 IO 的版本。这里给出 ``ProbeResult`` 的占位，并把要查询的 hash
        附在 ``raw`` 上以便上层取用。
        """
        prefix_hash = self._prefix_hash(ir)
        # 真实实现：``http.head(f"/v1/cache/prefix?hash={prefix_hash}")``
        return ProbeResult(hit=False, cached_token_count=0, tier="none")

    def evict_span(
        self, ir: TelosIR, start_block: int, end_block: int,
    ) -> Mapping[str, Any]:
        """返回一段嵌进下次 emit 的 ``cache_policy`` 片段。

        bridge 在 ``Bridge.fold`` 之后会把这个 dict 合并进 ``EmitPlan.extras``。
        """
        return {"evict_span": [start_block, end_block]}

    def refresh(self, ir: TelosIR, plan: EmitPlan) -> Mapping[str, Any]:
        """返回 prewarm 请求体；调用方真正 POST。

        与 ``EngineAdapter.refresh`` 不同（基类返回 None），这里返回 dict 是
        因为我们想让 caller 看到 prewarm 请求长什么样、便于审计。
        """
        body = dict(self.emit(ir, plan))
        body["max_tokens"] = 1
        body["stream"] = False
        # vLLM 没有 ``ignore_eos`` 必要性，但加上确保不会因为意外 EOS 提前停
        body["ignore_eos"] = True
        return body

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _estimate_block_index(
        self, ir: TelosIR, segment: str, index: int, message_index: int | None,
    ) -> int:
        """粗估 token block 边界。vLLM 默认 16 token / block。

        粗估足够了——server 端会按真实 tokenization 命中前缀；这里的数字
        只是给 ``pin_prefix_until_block`` 一个保守上界。
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
        # 4 chars ≈ 1 token 是英文经验值；中文偏低，宁取保守
        approx_tokens = char_count // 4
        return approx_tokens // BLOCK

    def _prefix_hash(self, ir: TelosIR) -> str:
        """前缀 hash：给 probe 用。tools + system 的 PIN 段。"""
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
    """§5 顺序的稳定排序键：PIN(0) < FOLD(1) < DROP(2)。"""
    rank = {Band.PIN: 0, Band.FOLD: 1, Band.DROP: 2}
    return sorted(blocks, key=lambda b: rank[b.band])
