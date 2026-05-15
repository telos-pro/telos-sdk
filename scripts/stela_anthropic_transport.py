"""StelaAnthropicTransport：把 Anthropic ``messages.create`` 串到 stela。

OpenClaw / Hermes agent 调用：

    self.client.messages.create(model=..., system=..., messages=[...], tools=[...])

本 transport 实现同样接口，但内部走对应 harness → STELA Bridge →
canonicalize / band-reorder → 用 ``AnthropicAdapter.emit()`` 重新生成带
``cache_control`` 标记的 wire → 真正发到 Anthropic ``/v1/messages``。

自动检测 harness：
- 若 ``system`` 字段包含 ``<system-reminder>`` / ``<command-message>`` 标签，
  或消息中含 ``thinking`` 块 → ``hermes``（Claude Code）
- 否则 → ``openclaw``

也可在构造时显式传 ``harness_name`` 覆盖自动检测。

设计要点（与 stela_transport.py 对齐）：
- 使用 ``engine.emit(ir2, plan)`` 而非自定义 wire builder，确保 Anthropic
  ``cache_control`` breakpoint 正确插入（§4.2 BP-T / BP-S / BP-R / BP-X）。
- ``max_tokens``：Anthropic 必填字段，从调用方传入；若未传则默认 8192。
- usage 同时记 raw 与 normalized，对齐 ``compute-metrics.py`` schema。
"""

from __future__ import annotations

import json
import os
import re
import time
from os.path import commonprefix
from pathlib import Path
from typing import Any, Iterable, Mapping

from stela import Bridge, load_engine, load_harness
from stela.bridge import BridgeSessionState


# ---------------------------------------------------------------------------
# Harness 自动检测
# ---------------------------------------------------------------------------
#
# Claude Code (Hermes) 把 envelope 标签注入的位置不止一处：
#   - `system` 段：少数客户端配置下出现
#   - `user message` 的 text 块：**绝大多数 turn 在这里**——每轮新一份
#                                  `<system-reminder>` / `<command-message>`
# 旧实现只扫 `system`，导致绝大多数 Claude Code 流量被误判成 openclaw
# （dashboard 上看到 ``openclaw/*`` source_tag 的真正原因）。
#
# 修复思路：
#   1) 用 (open + close) 配对的正则，避免用户在 prompt 里讨论标签时误中
#   2) 扫 system 段 **以及** 所有 user message 的 text 内容
#   3) 增加 `<command-name>`（slash 命令面板会单独注入此标签）
#   4) 兜底：assistant 已有 `thinking` 块
#   5) 兜底：tools 列表命中 Claude Code 固有工具集合 ≥ 3 个

_HERMES_MARKER_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"<{tag}>.*?</{tag}>", re.DOTALL)
    for tag in ("system-reminder", "command-message", "command-name")
)

# Claude Code 总是带这一组工具中的若干个；命中 ≥ 3 即认为是 Claude Code。
_HERMES_TOOL_FINGERPRINT: frozenset[str] = frozenset({
    "Bash", "Edit", "Read", "Write", "Grep", "Glob",
    "TodoWrite", "Task", "WebFetch", "WebSearch", "NotebookEdit",
})
_HERMES_TOOL_HITS_REQUIRED = 3


def _flatten_system_text(raw_request: Mapping[str, Any]) -> str:
    system = raw_request.get("system", [])
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for item in system:
            if isinstance(item, dict):
                t = item.get("text", "")
                if isinstance(t, str):
                    parts.append(t)
            else:
                parts.append(str(item))
        return " ".join(parts)
    return ""


def _iter_user_text(raw_request: Mapping[str, Any]) -> Iterable[str]:
    """逐条 yield user message 里 text 块的纯字符串。"""
    for msg in raw_request.get("messages", []) or []:
        if not isinstance(msg, Mapping) or msg.get("role") != "user":
            continue
        content = msg.get("content", [])
        if isinstance(content, str):
            yield content
            continue
        if not isinstance(content, list):
            continue
        for blk in content:
            if isinstance(blk, Mapping) and blk.get("type") == "text":
                text = blk.get("text")
                if isinstance(text, str):
                    yield text


def _has_thinking_block(raw_request: Mapping[str, Any]) -> bool:
    for msg in raw_request.get("messages", []) or []:
        if not isinstance(msg, Mapping):
            continue
        content = msg.get("content", [])
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, Mapping) and blk.get("type") == "thinking":
                    return True
    return False


def _has_hermes_marker(text: str) -> bool:
    return any(p.search(text) for p in _HERMES_MARKER_PATTERNS)


def _tool_fingerprint_matches_hermes(raw_request: Mapping[str, Any]) -> bool:
    names: set[str] = set()
    for t in raw_request.get("tools", []) or []:
        if isinstance(t, Mapping):
            n = t.get("name")
            if isinstance(n, str):
                names.add(n)
    return len(names & _HERMES_TOOL_FINGERPRINT) >= _HERMES_TOOL_HITS_REQUIRED


def _detect_harness(raw_request: Mapping[str, Any]) -> str:
    # 1) envelope 标签（system 段 + 所有 user message text）
    if _has_hermes_marker(_flatten_system_text(raw_request)):
        return "hermes"
    for ut in _iter_user_text(raw_request):
        if _has_hermes_marker(ut):
            return "hermes"

    # 2) assistant thinking 块
    if _has_thinking_block(raw_request):
        return "hermes"

    # 3) tool 集合指纹（首轮 turn 没注入 reminder 时也能识别）
    if _tool_fingerprint_matches_hermes(raw_request):
        return "hermes"

    return "openclaw"


# ---------------------------------------------------------------------------
# Usage 归一化：对齐 Anthropic usage schema
# ---------------------------------------------------------------------------

def _normalize_usage(response_usage: Mapping[str, Any]) -> dict[str, int]:
    if not response_usage:
        return {"raw_input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
    return {
        "raw_input": int(response_usage.get("input_tokens", 0)),
        "cache_read": int(response_usage.get("cache_read_input_tokens", 0)),
        "cache_write": int(response_usage.get("cache_creation_input_tokens", 0)),
        "output": int(response_usage.get("output_tokens", 0)),
    }


# ---------------------------------------------------------------------------
# IR summary helpers（从 stela_transport.py 复用）
# ---------------------------------------------------------------------------

def _summarize_ir(ir) -> dict[str, Any]:
    from stela.ir import Band

    def _bucket():
        return {b.name: {"blocks": 0, "chars": 0} for b in Band}

    def _add(buck, blocks):
        for b in blocks:
            slot = buck[b.band.name]
            slot["blocks"] += 1
            try:
                slot["chars"] += len(
                    json.dumps(b.payload, ensure_ascii=False)
                    if not isinstance(b.payload, str)
                    else b.payload
                )
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
    from stela.ir import Band

    bands = ir_summary.get("bands") or {}
    regions: dict[str, Any] = {}
    band_totals = {b.name: 0 for b in Band}
    grand = 0
    for seg in ("tools", "system", "messages"):
        seg_buck = bands.get(seg) or {}
        seg_entry = {b.name: int((seg_buck.get(b.name) or {}).get("chars", 0)) for b in Band}
        seg_entry["total"] = sum(seg_entry.values())
        regions[seg] = seg_entry
        for b in Band:
            band_totals[b.name] += seg_entry[b.name]
        grand += seg_entry["total"]
    return {"by_segment": regions, "by_band": band_totals, "total": grand}


def _summarize_messages(raw_request: Mapping[str, Any]) -> dict[str, Any]:
    msgs = raw_request.get("messages", [])
    by_role: dict[str, dict[str, int]] = {}
    for m in msgs:
        role = str(m.get("role", "?"))
        slot = by_role.setdefault(role, {"count": 0, "chars": 0})
        slot["count"] += 1
        content = m.get("content", "")
        if isinstance(content, str):
            slot["chars"] += len(content)
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict):
                    slot["chars"] += len(blk.get("text", "") or str(blk.get("content", "")))
    return {
        "n_messages": len(msgs),
        "total_chars": sum(s["chars"] for s in by_role.values()),
        "by_role": by_role,
        "n_tools": len(raw_request.get("tools", [])),
    }


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
    parts = []
    system = wire.get("system", [])
    if isinstance(system, list):
        for blk in system:
            if isinstance(blk, dict):
                parts.append(f"[system]\n{blk.get('text', '')}")
    elif isinstance(system, str):
        parts.append(f"[system]\n{system}")
    for m in wire.get("messages", []):
        content = m.get("content", "")
        if isinstance(content, str):
            parts.append(f"[{m.get('role', '?')}]\n{content}")
        elif isinstance(content, list):
            text = "\n".join(
                b.get("text", "") or json.dumps(b, ensure_ascii=False, sort_keys=True)
                for b in content
                if isinstance(b, dict)
            )
            parts.append(f"[{m.get('role', '?')}]\n{text}")
    if wire.get("tools"):
        parts.append(json.dumps(wire["tools"], ensure_ascii=False, sort_keys=True))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class StelaAnthropicTransport:
    """Anthropic-鸭子接口的 client，内部走 STELA（openclaw 或 hermes harness）。

    Args:
        api_key:      envvar 读不到时显式传入。
        base_url:     覆盖默认的 Anthropic API URL（调试用）。
        session_id:   同一个 session 内复用 Bridge stats。
        harness_name: ``"openclaw"`` / ``"hermes"`` / ``None``（自动检测）。
        engine_name:  默认 ``"anthropic"``。
        usage_log:    每次调用追加一行 jsonl 的路径；``None`` 表示不写。
        prompt_trace_log: 结构化 prompt trace 日志路径。
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        session_id: str = "stela-session",
        harness_name: str | None = None,
        engine_name: str = "anthropic",
        usage_log: str | None = None,
        prompt_trace_log: str | None = None,
        session_state: BridgeSessionState | None = None,
    ):
        import anthropic  # 延迟导入

        kwargs: dict[str, Any] = {
            "api_key": api_key or os.environ.get("ANTHROPIC_API_KEY", ""),
        }
        if base_url is not None:
            kwargs["base_url"] = base_url

        self._inner = anthropic.Anthropic(**kwargs)
        self._engine = load_engine(engine_name)
        self._explicit_harness = harness_name
        self._session_id = session_id
        self._usage_log = Path(usage_log) if usage_log else None
        self._trace_log = Path(prompt_trace_log) if prompt_trace_log else None
        self._call_count = 0
        self._prev_wire_text: str = ""
        self._prev_regions: dict[str, Any] | None = None

        # Bridge 跨 turn 状态：transport 一个实例 = 一个 session，state 自然
        # 跟着这个实例的生命周期累积。caller 想自带 state 也行（多 transport
        # 共享同一段 conversation 的场景）。
        self._session_state = (
            session_state if session_state is not None else BridgeSessionState()
        )

        # harness 缓存（auto-detect 时每次可能不同；explicit 时只建一次）
        self._harness_cache: dict[str, Any] = {}

        # 鸭子接口
        self.messages = _MessagesNS(self)

    @property
    def session_state(self) -> BridgeSessionState:
        return self._session_state

    def _get_harness(self, name: str):
        if name not in self._harness_cache:
            self._harness_cache[name] = load_harness(name)
        return self._harness_cache[name]

    # ------------------------------------------------------------------
    # 内部：执行一次 create
    # ------------------------------------------------------------------

    def _do_create(self, kwargs: dict[str, Any]):
        from stela.ir import Band

        self._call_count += 1
        model = kwargs.get("model", "")
        max_tokens = kwargs.get("max_tokens", 8192)

        # ---- 0. caller 原始输入快照 ----
        input_summary = _summarize_messages(kwargs)

        # ---- 1. 选 harness（explicit > sticky > auto-detect）----
        if self._explicit_harness:
            harness_name = self._explicit_harness
        elif self._session_state.sticky_harness:
            harness_name = self._session_state.sticky_harness
        else:
            harness_name = _detect_harness(kwargs)
            self._session_state.sticky_harness = harness_name
        harness = self._get_harness(harness_name)

        # ---- 2. parse → IR ----
        ir = harness.parse(
            kwargs,
            session_id=self._session_id,
            engine="anthropic",
            model=model,
        )
        ir_in_summary = _summarize_ir(ir)

        # ---- 3. Bridge：canonicalize + plan + emit ----
        # 关键：传 session_state 让 ref-pool / R8 计数器跨 turn 累积；
        # 使用 emit_with_plan() 才能跑 _canonicalize_ir（tools 排序、payload
        # key 排序）—— 直接 engine.emit(ir2, plan) 会漏掉这一步。
        bridge = Bridge(ir, self._engine, session_state=self._session_state)
        wire_dict, plan = bridge.emit_with_plan()
        plan_summary = _summarize_plan(plan)

        # ---- 4. 整 wire / 日志快照 ----
        ir2 = bridge.snapshot_ir()
        ir_out_summary = _summarize_ir(ir2)
        regions = _flatten_regions(ir_out_summary)

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

        wire: dict[str, Any] = dict(wire_dict)
        wire["max_tokens"] = max_tokens

        # 透传调用方传入的非 stela 字段
        for k in ("temperature", "top_p", "stream", "stop_sequences",
                  "tool_choice", "thinking", "metadata", "timeout"):
            if k in kwargs and kwargs[k] is not None:
                wire[k] = kwargs[k]

        wire_text = _wire_text(wire)
        prefix_match_chars = (
            len(commonprefix([self._prev_wire_text, wire_text]))
            if self._prev_wire_text else 0
        )

        # ---- 5. 真发请求 ----
        t0 = time.time()
        response = self._inner.messages.create(**wire)
        dt = time.time() - t0

        # ---- 6. usage 归一化 + 跨 turn 累积 ----
        usage_obj = getattr(response, "usage", None)
        usage_dict = usage_obj.model_dump() if usage_obj is not None else {}
        normalized = _normalize_usage(usage_dict)
        inp_total = normalized["raw_input"] + normalized["cache_read"]
        cache_share = (normalized["cache_read"] / inp_total) if inp_total else 0.0

        # bridge.absorb_usage：调 engine.parse_usage + state.cumulative_cache_creation += ...
        # 包成 dict 喂给它（anthropic engine 期望 ``{"usage": {...}}``）。
        try:
            bridge.absorb_usage({"usage": usage_dict})
        except Exception:  # noqa: BLE001
            pass  # 累积失败不影响主路径

        if self._usage_log is not None:
            self._usage_log.parent.mkdir(parents=True, exist_ok=True)
            with self._usage_log.open("a") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "session_id": self._session_id,
                    "call_index": self._call_count,
                    "model": model,
                    "harness": harness_name,
                    "latency_s": round(dt, 3),
                    "routing_key": plan.routing_key,
                    "raw_usage": usage_dict,
                    "normalized": normalized,
                    "cumulative": {
                        "cache_creation":
                            self._session_state.stats.cumulative_cache_creation,
                        "real_requests_since_refresh":
                            self._session_state.stats.real_requests_since_refresh,
                        "refpool_slugs": sorted(self._session_state.refpool.slugs),
                    },
                }, ensure_ascii=False) + "\n")

        if self._trace_log is not None:
            self._trace_log.parent.mkdir(parents=True, exist_ok=True)
            with self._trace_log.open("a") as f:
                f.write(json.dumps({
                    "session_id": self._session_id,
                    "call_index": self._call_count,
                    "model": model,
                    "harness": harness_name,
                    "latency_s": round(dt, 3),
                    "input": input_summary,
                    "ir_after_parse": ir_in_summary,
                    "ir_after_canonicalize": ir_out_summary,
                    "regions": regions,
                    "region_deltas": region_deltas,
                    "plan": plan_summary,
                    "breakpoints": plan_summary["slots"],
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
                        "cache_write": normalized["cache_write"],
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


class _MessagesNS:
    def __init__(self, t: StelaAnthropicTransport):
        self._t = t

    def create(self, **kwargs):
        return self._t._do_create(kwargs)
