"""StelaOpenAITransport：把 OpenAI-shape 的 chat.completions client 串到 stela。

mini_swe_runner（telos vendored hermes）调用：

    self.client.chat.completions.create(model=..., messages=[...], tools=[...])

本 transport 实现同样接口，但内部走 ``telos`` harness → STELA Bridge →
canonicalize / band-reorder → 转回 chat-completions shape → 真正发到
OpenRouter 的 ``/v1/chat/completions``。响应回来后用 ``deepseek``
adapter 归一化 usage（DeepSeek V3+ 在 OpenRouter 的 usage 字段是
``prompt_cache_hit_tokens / prompt_cache_miss_tokens``），写入 jsonl 日志。

设计要点：

- **不破坏 tool_calls 结构**：role=assistant 的 ``tool_calls`` 与 role=tool
  的 ``tool_call_id`` 必须按 OpenAI 协议挂回 wire，不能像 DeepSeek
  adapter 那样直接 inline 成文本——否则 agent 循环拿不到工具结果。
- **应用 STELA 政策的最小子集**：DROP 段（``<environment_info>`` /
  ``Current time:`` 等）下沉到每个 user message 文本的尾部；tool 定义
  做 canonicalize（key 排序）；其余按 §5 顺序。这是 cache-命中收益最
  直接的两条规则。
- **usage 同时记 raw 与 normalized**：raw 留作诊断，normalized 按
  ``UsageReport`` schema 直接对齐 ``compute-metrics.py``。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from os.path import commonprefix
from pathlib import Path
from typing import Any, Mapping

from stela import Band, Bridge, load_engine, load_harness
from stela.bridge import BridgeSessionState, _canonicalize_ir


# ---------------------------------------------------------------------------
# IR -> OpenAI ChatCompletions wire（保留 tool_calls / role=tool 结构）
# ---------------------------------------------------------------------------

def _ir_to_chat_completions(ir, *, model: str) -> dict[str, Any]:
    # system: PIN 在前、DROP 在后
    sys_blocks = sorted(ir.system, key=lambda b: 0 if b.band is not Band.DROP else 1)
    sys_text = "\n\n".join(str(b.payload) for b in sys_blocks)

    wire_messages: list[dict[str, Any]] = []
    if sys_text.strip():
        wire_messages.append({"role": "system", "content": sys_text})

    for m in ir.messages:
        ordered = sorted(m.blocks, key=lambda b: 0 if b.band is not Band.DROP else 1)

        if m.role == "user":
            # 把 tool_result 单独抽出来变成 role=tool；剩余文本拼成一条 user
            tr = [b for b in ordered if b.kind == "tool_result"]
            for trb in tr:
                payload = trb.payload or {}
                wire_messages.append({
                    "role": "tool",
                    "tool_call_id": payload.get("tool_use_id", ""),
                    "content": str(payload.get("content", "")),
                })
            text_parts = [str(b.payload) for b in ordered if b.kind == "text"]
            joined = "\n".join(p for p in text_parts if p)
            if joined.strip():
                wire_messages.append({"role": "user", "content": joined})

        elif m.role == "assistant":
            text_parts = [str(b.payload) for b in ordered if b.kind == "text"]
            tool_calls = [b.payload for b in ordered if b.kind == "tool_use"]
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(text_parts) if text_parts else None,
            }
            if tool_calls:
                entry["tool_calls"] = list(tool_calls)
            wire_messages.append(entry)

    wire: dict[str, Any] = {"model": model, "messages": wire_messages}
    if ir.tools:
        wire["tools"] = [b.payload for b in ir.tools]
    return wire


# ---------------------------------------------------------------------------
# Usage 归一化：兼容 DeepSeek-style 与 OpenAI-style 字段
# ---------------------------------------------------------------------------

def _normalize_usage(response_usage: Mapping[str, Any]) -> dict[str, int]:
    if not response_usage:
        return {"raw_input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
    # DeepSeek（OpenRouter 透传）
    if "prompt_cache_hit_tokens" in response_usage or "prompt_cache_miss_tokens" in response_usage:
        hit = int(response_usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(response_usage.get("prompt_cache_miss_tokens") or 0)
        return {
            "raw_input": miss,
            "cache_read": hit,
            "cache_write": 0,
            "output": int(response_usage.get("completion_tokens") or 0),
        }
    # OpenAI / 其它：``cached_tokens`` 在 prompt_tokens 子段或顶层
    pt = int(response_usage.get("prompt_tokens") or 0)
    cached = int(
        (response_usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        or response_usage.get("cached_tokens", 0)
    )
    return {
        "raw_input": max(pt - cached, 0),
        "cache_read": cached,
        "cache_write": 0,
        "output": int(response_usage.get("completion_tokens") or 0),
    }


# ---------------------------------------------------------------------------
# Prompt-construction trace helpers
# ---------------------------------------------------------------------------

def _msg_text(m: Mapping[str, Any]) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for p in c:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, Mapping):
                parts.append(str(p.get("text") or p.get("content") or ""))
        return "\n".join(parts)
    return "" if c is None else str(c)


def _summarize_messages(msgs: list[Mapping[str, Any]]) -> dict[str, Any]:
    by_role: dict[str, dict[str, int]] = {}
    for m in msgs:
        role = str(m.get("role", "?"))
        slot = by_role.setdefault(role, {"count": 0, "chars": 0, "tool_calls": 0})
        slot["count"] += 1
        slot["chars"] += len(_msg_text(m))
        if m.get("tool_calls"):
            slot["tool_calls"] += len(m["tool_calls"])  # type: ignore[arg-type]
    return {
        "n_messages": len(msgs),
        "total_chars": sum(s["chars"] for s in by_role.values()),
        "by_role": by_role,
    }


def _summarize_ir(ir) -> dict[str, Any]:
    """Per-band, per-segment block stats. PIN+FOLD+DROP visibility."""
    def _bucket():
        return {b.name: {"blocks": 0, "chars": 0} for b in Band}

    def _add(buck, blocks):
        for b in blocks:
            slot = buck[b.band.name]
            slot["blocks"] += 1
            try:
                slot["chars"] += len(json.dumps(b.payload, ensure_ascii=False)
                                      if not isinstance(b.payload, str)
                                      else b.payload)
            except Exception:  # noqa: BLE001
                slot["chars"] += len(str(b.payload))

    tools = _bucket(); _add(tools, ir.tools)
    system = _bucket(); _add(system, ir.system)
    msgs_band = _bucket()
    msg_kinds: dict[str, int] = {}
    for m in ir.messages:
        _add(msgs_band, m.blocks)
        for b in m.blocks:
            msg_kinds[b.kind] = msg_kinds.get(b.kind, 0) + 1
    return {
        "n_tools": len(ir.tools),
        "n_system_blocks": len(ir.system),
        "n_messages": len(ir.messages),
        "bands": {"tools": tools, "system": system, "messages": msgs_band},
        "msg_block_kinds": msg_kinds,
    }


def _flatten_regions(ir_summary: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce per-band/per-segment summary into flat numbers convenient for the
    dashboard: total chars per segment, total chars per band, grand total.
    """
    bands = ir_summary.get("bands") or {}
    regions: dict[str, Any] = {}
    band_totals = {b.name: 0 for b in Band}
    grand = 0
    for seg in ("tools", "system", "messages"):
        seg_buck = bands.get(seg) or {}
        seg_entry = {b.name: int((seg_buck.get(b.name) or {}).get("chars", 0))
                     for b in Band}
        seg_entry["total"] = sum(seg_entry.values())
        regions[seg] = seg_entry
        for b in Band:
            band_totals[b.name] += seg_entry[b.name]
        grand += seg_entry["total"]
    return {"by_segment": regions, "by_band": band_totals, "total": grand}


def _summarize_plan(plan) -> dict[str, Any]:
    return {
        "routing_key": plan.routing_key,
        "n_slots": len(plan.slots),
        "slots": [
            {
                "name": s.name,
                "segment": s.segment,
                "index": s.index,
                "message_index": s.message_index,
                "ttl_class": s.ttl_class,
            }
            for s in plan.slots
        ],
        "extras": dict(plan.extras) if plan.extras else {},
    }


def _wire_text(wire: Mapping[str, Any]) -> str:
    """Concatenate role-tagged messages — used to measure cross-call prefix match."""
    parts = []
    for m in wire.get("messages", []):
        parts.append(f"[{m.get('role', '?')}]\n{_msg_text(m)}")
        if m.get("tool_calls"):
            parts.append(json.dumps(m["tool_calls"], ensure_ascii=False, sort_keys=True))
    if wire.get("tools"):
        parts.append(json.dumps(wire["tools"], ensure_ascii=False, sort_keys=True))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Transport（鸭子接口：mini_swe_runner 只用到 .chat.completions.create）
# ---------------------------------------------------------------------------

class StelaOpenAITransport:
    """OpenAI-鸭子接口的 client，内部走 STELA。

    Args:
        base_url: 底层真实端点，例如 ``https://openrouter.ai/api/v1``。
        api_key:  envvar 读不到时显式传入。
        session_id: 同一个 session 内复用 Bridge stats。
        usage_log: 每次调用追加一行 jsonl 的路径；``None`` 表示不写。
        engine_name: engine adapter 名（``deepseek`` 用于 OpenRouter+DS）。
        harness_name: 默认 ``telos``。
    """

    def __init__(
        self,
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        api_key: str | None = None,
        session_id: str = "telos-session",
        usage_log: str | None = None,
        prompt_trace_log: str | None = None,
        engine_name: str = "deepseek",
        harness_name: str = "telos",
        session_state: BridgeSessionState | None = None,
    ):
        from openai import OpenAI  # 延迟导入

        self.base_url = base_url
        self._inner = OpenAI(
            base_url=base_url,
            api_key=api_key or os.environ.get("OPENROUTER_API_KEY", ""),
        )
        self._harness = load_harness(harness_name)
        self._engine = load_engine(engine_name)
        self._session_id = session_id
        self._usage_log = Path(usage_log) if usage_log else None
        self._trace_log = Path(prompt_trace_log) if prompt_trace_log else None
        self._call_count = 0
        self._prev_wire_text: str = ""
        self._prev_regions: dict[str, Any] | None = None

        # Bridge 跨 turn 状态：transport 一个实例 = 一个 session。
        self._session_state = (
            session_state if session_state is not None else BridgeSessionState()
        )

        # 鸭子接口
        self.chat = _ChatNS(self)

    @property
    def session_state(self) -> BridgeSessionState:
        return self._session_state

    # ------------------------------------------------------------------
    # 内部：执行一次 create
    # ------------------------------------------------------------------
    def _do_create(self, kwargs: dict[str, Any]):
        self._call_count += 1
        model = kwargs.get("model", "")
        # ---- 0. caller 原始输入快照 ----
        in_msgs = list(kwargs.get("messages") or [])
        in_tools = list(kwargs.get("tools") or [])
        input_summary = _summarize_messages(in_msgs)
        input_summary["n_tools"] = len(in_tools)

        # 1. parse → IR
        ir = self._harness.parse(
            kwargs,
            session_id=self._session_id,
            engine="deepseek",
            model=model,
        )
        ir_in_summary = _summarize_ir(ir)

        # 2. Bridge：传 session_state 让 R8 计数器 / cache_creation 跨 turn 累积
        bridge = Bridge(ir, self._engine, session_state=self._session_state)
        plan = bridge.mark()
        plan_summary = _summarize_plan(plan)

        # 3. 规范化（tools 排序、payload key 排序）—— 不能直接用 ir 的 snapshot，
        # 必须跑过 _canonicalize_ir 才能保证多轮 prefix 字节稳定。这是 OpenAI
        # 路径的关键修复：以前直接喂 ir2 给 wire builder，没做这一步。
        ir2 = _canonicalize_ir(bridge.snapshot_ir())
        ir_out_summary = _summarize_ir(ir2)
        regions = _flatten_regions(ir_out_summary)
        # 增长过程：相对上一次调用的 chars 变动（按 segment & band）
        if self._prev_regions is None:
            region_deltas: dict[str, Any] = {"first_call": True}
        else:
            prev = self._prev_regions
            region_deltas = {
                "first_call": False,
                "by_segment": {
                    seg: regions["by_segment"][seg]["total"]
                         - prev["by_segment"][seg]["total"]
                    for seg in ("tools", "system", "messages")
                },
                "by_band": {
                    b.name: regions["by_band"][b.name] - prev["by_band"][b.name]
                    for b in Band
                },
                "total": regions["total"] - prev["total"],
            }
        wire = _ir_to_chat_completions(ir2, model=model)
        # 4. 透传一些非 stela 关心的字段
        for k in ("temperature", "top_p", "max_tokens", "stream",
                  "timeout", "tool_choice", "response_format"):
            if k in kwargs and kwargs[k] is not None:
                wire[k] = kwargs[k]

        wire_summary = _summarize_messages(wire.get("messages", []))
        wire_summary["n_tools"] = len(wire.get("tools") or [])
        # 跨调用前缀稳定性：cache 命中的最强先行指标
        wire_text = _wire_text(wire)
        prefix_match_chars = len(commonprefix([self._prev_wire_text, wire_text])) \
            if self._prev_wire_text else 0

        # 真请求即将发出 —— 等同 bridge.emit_with_plan 末尾的 +1，因为这条
        # OpenAI 路径走自定义 _ir_to_chat_completions 而不是 engine.emit。
        self._session_state.stats.real_requests_since_refresh += 1

        # 5. 真发请求
        t0 = time.time()
        response = self._inner.chat.completions.create(**wire)
        dt = time.time() - t0

        # 6. usage 归一化 + 跨 turn 累积
        usage_obj = getattr(response, "usage", None)
        usage_dict = usage_obj.model_dump() if usage_obj is not None else {}
        normalized = _normalize_usage(usage_dict)
        inp_total = normalized["raw_input"] + normalized["cache_read"]
        cache_share = (normalized["cache_read"] / inp_total) if inp_total else 0.0

        # bridge.absorb_usage：通过 engine.parse_usage 抽出 cache_write 并累加
        # 进 session_state。DeepSeek/OpenAI 的 cache_write 通常为 0，但调用
        # 形式与 anthropic transport 对齐，保留 R8 可见性。
        try:
            bridge.absorb_usage({"usage": usage_dict})
        except Exception:  # noqa: BLE001
            pass

        if self._usage_log is not None:
            self._usage_log.parent.mkdir(parents=True, exist_ok=True)
            with self._usage_log.open("a") as f:
                f.write(json.dumps({
                    "session_id": self._session_id,
                    "call_index": self._call_count,
                    "model": model,
                    "latency_s": round(dt, 3),
                    "routing_key": plan.routing_key,
                    "raw_usage": usage_dict,
                    "normalized": normalized,
                }, ensure_ascii=False) + "\n")

        if self._trace_log is not None:
            self._trace_log.parent.mkdir(parents=True, exist_ok=True)
            with self._trace_log.open("a") as f:
                f.write(json.dumps({
                    "session_id": self._session_id,
                    "call_index": self._call_count,
                    "model": model,
                    "latency_s": round(dt, 3),
                    "input": input_summary,
                    "ir_after_parse": ir_in_summary,
                    "ir_after_canonicalize": ir_out_summary,
                    "regions": regions,
                    "region_deltas": region_deltas,
                    "plan": plan_summary,
                    "breakpoints": plan_summary["slots"],
                    "wire": wire_summary,
                    "prefix": {
                        "prev_wire_chars": len(self._prev_wire_text),
                        "this_wire_chars": len(wire_text),
                        "common_prefix_chars": prefix_match_chars,
                        "prefix_stability": (
                            prefix_match_chars / len(self._prev_wire_text)
                            if self._prev_wire_text else None
                        ),
                    },
                    "cache": {
                        "raw_input": normalized["raw_input"],
                        "cache_read": normalized["cache_read"],
                        "output": normalized["output"],
                        "input_total": inp_total,
                        "cache_share": round(cache_share, 4),
                    },
                    "cumulative": {
                        "cache_creation":
                            self._session_state.stats.cumulative_cache_creation,
                        "real_requests_since_refresh":
                            self._session_state.stats.real_requests_since_refresh,
                        "refpool_slugs": sorted(self._session_state.refpool.slugs),
                    },
                }, ensure_ascii=False) + "\n")

        self._prev_wire_text = wire_text
        self._prev_regions = regions
        return response


class _ChatNS:
    def __init__(self, t: StelaOpenAITransport):
        self.completions = _CompletionsNS(t)


class _CompletionsNS:
    def __init__(self, t: StelaOpenAITransport):
        self._t = t

    def create(self, **kwargs):
        return self._t._do_create(kwargs)
