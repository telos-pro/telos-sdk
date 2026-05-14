"""STELA 处理管线 —— 纯函数，从原始 Anthropic 请求出 wire 请求。

把 ``StelaAnthropicTransport._do_create`` 里 parse → bridge → emit 这一段
拆出来，让 proxy 和 transport 共用同一份实现，不会出现 wire 行为漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from stela import Bridge, load_engine, load_harness
from stela.scripts.stela_anthropic_transport import _detect_harness


# 透传给上游、不参与 STELA 管线的字段。
_PASSTHROUGH_FIELDS = (
    "max_tokens", "temperature", "top_p", "stream", "stop_sequences",
    "tool_choice", "thinking", "metadata", "service_tier", "top_k",
)


@dataclass
class PipelineResult:
    """STELA 管线的输出。

    Attributes:
        wire:    可直接发到 ``api.anthropic.com/v1/messages`` 的请求体。
        harness: 实际使用的 harness 名（自动检测或显式传入）。
        plan_slots: ``EmitPlan`` 的 slot 名列表（诊断用）。
        routing_key: 对 Anthropic 始终为 ``None``；保留字段以对齐通用 schema。
    """

    wire: dict[str, Any]
    harness: str
    plan_slots: list[str]
    routing_key: str | None


def process_anthropic_request(
    raw: Mapping[str, Any],
    *,
    session_id: str,
    harness_name: str | None = None,
    engine_name: str = "anthropic",
) -> PipelineResult:
    """跑一次 STELA 管线，返回处理后的 wire 请求 + 诊断信息。

    Args:
        raw:          原始 ``/v1/messages`` 请求体（dict）。
        session_id:   STELA session 标识，用于 Bridge 复用同一份 ref-pool。
        harness_name: ``"openclaw"`` / ``"hermes"`` / ``None``（自动检测）。
        engine_name:  默认 ``"anthropic"``。
    """
    name = harness_name or _detect_harness(raw)
    harness = load_harness(name)
    engine = load_engine(engine_name)

    ir = harness.parse(
        raw,
        session_id=session_id,
        engine=engine_name,
        model=raw.get("model", ""),
    )
    bridge = Bridge(ir, engine)
    plan = bridge.mark()
    ir2 = bridge.snapshot_ir()

    wire: dict[str, Any] = dict(engine.emit(ir2, plan))
    for k in _PASSTHROUGH_FIELDS:
        if k in raw and raw[k] is not None:
            wire[k] = raw[k]

    return PipelineResult(
        wire=wire,
        harness=name,
        plan_slots=[s.name for s in plan.slots],
        routing_key=plan.routing_key,
    )
