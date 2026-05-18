"""TELOS 处理管线 —— 纯函数，从原始 Anthropic 请求出 wire 请求。

把 ``TelosAnthropicTransport._do_create`` 里 parse → bridge → emit 这一段
拆出来，让 proxy 和 transport 共用同一份实现，不会出现 wire 行为漂移。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from telos import Bridge, load_engine, load_harness
from telos.bridge import BridgeSessionState
from telos.ir import Band, TelosIR
from telos.registry import canonical_harness
from telos.scripts.telos_anthropic_transport import _detect_harness


# 透传给上游、不参与 TELOS 管线的字段。
_PASSTHROUGH_FIELDS = (
    "max_tokens", "temperature", "top_p", "stream", "stop_sequences",
    "tool_choice", "thinking", "metadata", "service_tier", "top_k",
)


@dataclass
class PipelineResult:
    """TELOS 管线的输出。

    Attributes:
        wire:        可直接发到 ``api.anthropic.com/v1/messages`` 的请求体。
        harness:     实际使用的 harness 名（自动检测或显式传入）。
        plan_slots:  ``EmitPlan`` 的 slot 名列表（诊断用）。
        routing_key: 对 Anthropic 始终为 ``None``；保留字段以对齐通用 schema。
        model:       请求里的 model 字段（透传，给 dashboard 算成本用）。
        cumulative_cache_creation: 跨 turn 累计的 cache_write tokens（来自
                                   session_state）。新 session 第一次为 0。
        real_requests_since_refresh: 距上次 refresh 的真实请求计数。
        ir_layout:   开发者面板用的 IR 结构快照（segment × band 字符数 /
                     block 数，加上每条 message 的 role/kind/band 列表）。
                     完整 IR 不进 wire dict，怕日志膨胀。
        tool_uses:   本轮请求中 assistant 发起的 tool_use 列表（name + 参数体
                     字符长度），用于开发者面板的工具调用统计。
        tool_results: 本轮请求中 user 段里的 tool_result 块（tool_use_id +
                     content 字符长度）。
    """

    wire: dict[str, Any]
    harness: str
    plan_slots: list[str]
    routing_key: str | None
    model: str = ""
    cumulative_cache_creation: int = 0
    real_requests_since_refresh: int = 0
    ir_layout: dict[str, Any] = field(default_factory=dict)
    tool_uses: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    # ↓ proxy 层在管线跑完后回填的字段（见 proxy/server.py）。
    # 对比实验需要按 (mode, compare_group) 切片 usage_log，故放进 result
    # 一并落盘。pipeline 本身不设置它们。
    mode: str = "telos"
    compare_group: str | None = None
    tool_output_reduction: dict[str, Any] = field(default_factory=dict)
    # 原始（TELOS 改写前）请求里每条 message 的摘要，供 developer 页面展示。
    # 由 proxy 层在 handle_messages 里回填；pipeline 本身不设置。
    raw_messages: list[dict[str, Any]] = field(default_factory=list)


def process_anthropic_request(
    raw: Mapping[str, Any],
    *,
    session_id: str,
    session_state: BridgeSessionState | None = None,
    harness_name: str | None = None,
    engine_name: str = "anthropic",
) -> PipelineResult:
    """跑一次 TELOS 管线，返回处理后的 wire 请求 + 诊断信息。

    Args:
        raw:           原始 ``/v1/messages`` 请求体（dict）。
        session_id:    TELOS session 标识，用于 Bridge 内部 IR.session_id 字段。
        session_state: 跨 turn 持久化的 Bridge 状态。**传入则 ref-pool /
                       R8 计数器跨调用累积**；不传则每轮独立（行为退化为
                       早期版本）。
        harness_name:  ``"openclaw"`` / ``"hermes"`` / ``None``（自动检测）。
        engine_name:   默认 ``"anthropic"``。

    Returns:
        ``PipelineResult``。注意 ``wire`` 已经过 ``_canonicalize_ir``
        （tools 排序、payload key 排序），可直接转发。
    """
    if harness_name:
        name = harness_name
    elif session_state is not None and session_state.sticky_harness:
        name = session_state.sticky_harness
    else:
        name = _detect_harness(raw)
        if session_state is not None:
            session_state.sticky_harness = name
    # 别名（claude-code → hermes）统一成 canonical 名，让 usage log /
    # dashboard 不论调用方传别名还是 canonical 名都一致。
    name = canonical_harness(name)
    harness = load_harness(name)
    engine = load_engine(engine_name)

    ir = harness.parse(
        raw,
        session_id=session_id,
        engine=engine_name,
        model=raw.get("model", ""),
    )
    bridge = Bridge(ir, engine, session_state=session_state)

    # bridge.emit_with_plan() 在内部跑 canonicalize → plan_marks → emit，
    # 把 cache_control 标记和 tool canonical 排序一起做好。
    wire_dict, plan = bridge.emit_with_plan()
    wire: dict[str, Any] = dict(wire_dict)

    # 透传调用方原始的非 TELOS 字段
    for k in _PASSTHROUGH_FIELDS:
        if k in raw and raw[k] is not None:
            wire[k] = raw[k]

    state = bridge.session_state
    snapshot = bridge.snapshot_ir()
    layout = _summarize_ir_layout(snapshot)
    tool_uses, tool_results = _extract_tool_calls(snapshot)
    return PipelineResult(
        wire=wire,
        harness=name,
        plan_slots=[s.name for s in plan.slots],
        routing_key=plan.routing_key,
        model=raw.get("model", ""),
        cumulative_cache_creation=state.stats.cumulative_cache_creation,
        real_requests_since_refresh=state.stats.real_requests_since_refresh,
        ir_layout=layout,
        tool_uses=tool_uses,
        tool_results=tool_results,
    )


# ---------------------------------------------------------------------------
# IR 摘要：给开发者面板用的 region byte counts + per-message band 序列
# ---------------------------------------------------------------------------

_BANDS = ("pin", "fold", "drop")


def _payload_size(payload: Any) -> int:
    """估算 payload 的字符体积（用于"prompt regions"展示）。

    text 用 len()；dict / list 走 json 序列化的字符数（这与 wire 大致同阶）。
    任何异常都退化到 ``len(str(payload))``，绝不抛错（开发者面板永远要能渲染）。
    """
    if isinstance(payload, str):
        return len(payload)
    try:
        import json as _json
        return len(_json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                default=str))
    except Exception:  # noqa: BLE001
        return len(str(payload))


def _summarize_ir_layout(ir: TelosIR) -> dict[str, Any]:
    """返回 ``{segment: {pin/fold/drop: {blocks, chars}}, messages: [...]}``。

    - segment ∈ {tools, system, messages}
    - 每个 message 单独记录 (role, blocks: [(band, kind, chars, id)])
    便于开发者面板逐 message 追溯 fold 区域的增减。
    """
    out: dict[str, Any] = {
        "session_id": ir.session_id,
        "engine": ir.hints.engine,
        "model": ir.hints.model,
        "segments": {seg: {b: {"blocks": 0, "chars": 0} for b in _BANDS}
                     for seg in ("tools", "system", "messages")},
        "messages": [],
        "ref_pool": [],
    }
    # tools
    for blk in ir.tools:
        s = out["segments"]["tools"][blk.band.value]
        s["blocks"] += 1
        s["chars"] += _payload_size(blk.payload)
    # system
    for blk in ir.system:
        s = out["segments"]["system"][blk.band.value]
        s["blocks"] += 1
        s["chars"] += _payload_size(blk.payload)
    # messages（同时汇总到 segments.messages.* 桶里 & per-message 详情）
    for mi, msg in enumerate(ir.messages):
        detail = {"index": mi, "role": msg.role, "blocks": []}
        for blk in msg.blocks:
            chars = _payload_size(blk.payload)
            s = out["segments"]["messages"][blk.band.value]
            s["blocks"] += 1
            s["chars"] += chars
            detail["blocks"].append({
                "id": blk.id,
                "band": blk.band.value,
                "kind": blk.kind,
                "chars": chars,
                "source_tag": blk.source_tag,
                "ref_slug": blk.ref_slug,
            })
        out["messages"].append(detail)
    # ref-pool：列出 slug + 当前 payload 字符数
    for slug, blk in ir.ref_pool.items():
        out["ref_pool"].append({
            "slug": slug,
            "band": blk.band.value,
            "chars": _payload_size(blk.payload),
        })
    return out


def _extract_tool_calls(ir: TelosIR) -> tuple[list[dict[str, Any]],
                                                list[dict[str, Any]]]:
    """从 IR 里捞出 (tool_uses, tool_results)。

    tool_use 来自 assistant message；tool_result 来自 user message。每条记录
    包含 name / args_chars / result_chars / tool_use_id（如果有），供
    SessionInspector 累加统计。
    """
    uses: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for mi, msg in enumerate(ir.messages):
        for blk in msg.blocks:
            if blk.kind == "tool_use" and isinstance(blk.payload, Mapping):
                p = blk.payload
                args = p.get("input") or p.get("arguments") or {}
                uses.append({
                    "message_index": mi,
                    "id": p.get("id") or blk.id,
                    "name": p.get("name") or "?",
                    "args_chars": _payload_size(args),
                })
            elif blk.kind == "tool_result" and isinstance(blk.payload, Mapping):
                p = blk.payload
                results.append({
                    "message_index": mi,
                    "tool_use_id": p.get("tool_use_id", ""),
                    "result_chars": _payload_size(p.get("content", "")),
                })
    return uses, results
