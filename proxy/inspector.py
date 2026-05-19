"""Session inspector -- stores a side-channel snapshot of each session's live IR / usage /
tool calls, for rendering the ``/__telos/developer`` page.

Design:
- Parallel to ``BridgeSessionState``: the bridge itself only cares about the ref-pool /
  statistics counters, the inspector cares about "the diagnostics a developer wants to
  see", and they do not pollute each other.
- Entirely in memory, lost on restart; high-frequency writes, low-frequency reads (the
  whole page is re-rendered on each GET).
- Bounded LRU: evicts the least-recently-accessed session when exceeding ``max_size``.
- Does not depend on aiohttp / any server-side library -- all server paths read from the
  inspector, so the inspector can be unit-tested independently.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any


INSPECTOR_HISTORY = 25  # keep a snapshot of the most recent N calls per session
DEFAULT_MAX_SESSIONS = 10_000


@dataclass
class ToolStat:
    """Cumulative call statistics for a single tool within a session.

    ``invocations`` comes from the assistant-initiated tool_use; ``result_chars_*``
    comes from the tool_result inside user messages (linked to the tool name via tool_use_id).
    """
    name: str
    invocations: int = 0
    args_chars_total: int = 0
    result_chars_total: int = 0
    result_chars_max: int = 0
    result_chars_min: int | None = None
    last_args_chars: int = 0
    last_result_chars: int = 0

    def absorb_use(self, args_chars: int) -> None:
        self.invocations += 1
        self.args_chars_total += args_chars
        self.last_args_chars = args_chars

    def absorb_result(self, result_chars: int) -> None:
        self.result_chars_total += result_chars
        self.last_result_chars = result_chars
        self.result_chars_max = max(self.result_chars_max, result_chars)
        self.result_chars_min = (
            result_chars if self.result_chars_min is None
            else min(self.result_chars_min, result_chars)
        )


@dataclass
class SessionInspectorEntry:
    """All inspector data for a single session."""

    session_id: str
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    last_layout: dict[str, Any] = field(default_factory=dict)
    last_plan_slots: list[str] = field(default_factory=list)
    last_usage_norm: dict[str, int] = field(default_factory=dict)
    last_usage_raw: dict[str, Any] = field(default_factory=dict)
    last_model: str = ""
    last_harness: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)
    tools_stat: dict[str, ToolStat] = field(default_factory=dict)
    tool_result_chars_total: int = 0
    tool_result_count: int = 0

    def record(
        self, *,
        call_index: int,
        layout: dict[str, Any],
        plan_slots: list[str],
        tool_uses: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        usage_norm: dict[str, int],
        usage_raw: dict[str, Any],
        latency_s: float,
        model: str,
        harness: str,
        raw_messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """After a call completes, accumulate inspector state.

        Registers a short summary in the ``calls`` history (with a "previous turn" Δ
        comparison), and links a subsequent ``tool_result`` back to the tool name via
        ``tool_use_id``.
        """
        self.last_seen = time.time()
        self.last_layout = layout
        self.last_plan_slots = plan_slots
        self.last_usage_norm = usage_norm
        self.last_usage_raw = usage_raw
        self.last_model = model
        self.last_harness = harness

        # Total chars of the current segments (pin+fold+drop per segment)
        segs = layout.get("segments") or {}
        cur_seg = {
            seg: sum(b.get("chars", 0) for b in (segs.get(seg) or {}).values())
            for seg in ("tools", "system", "messages")
        }
        prev = self.calls[-1] if self.calls else None
        prev_seg = (prev or {}).get("segment_chars") or {}
        delta = {
            seg: cur_seg[seg] - prev_seg.get(seg, cur_seg[seg])
            for seg in cur_seg
        }
        self.calls.append({
            "call_index": call_index,
            "ts": time.time(),
            "latency_s": round(latency_s, 3),
            "plan_slots": list(plan_slots),
            "usage_norm": dict(usage_norm),
            "segment_chars": cur_seg,
            "segment_chars_delta": delta,
            "n_tool_uses": len(tool_uses),
            "n_tool_results": len(tool_results),
            "tool_uses": list(tool_uses),
            "tool_results": list(tool_results),
            "raw_messages": list(raw_messages or []),
        })
        if len(self.calls) > INSPECTOR_HISTORY:
            del self.calls[: -INSPECTOR_HISTORY]

        # Update tool statistics
        for u in tool_uses:
            name = u.get("name") or "?"
            stat = self.tools_stat.setdefault(name, ToolStat(name=name))
            stat.absorb_use(int(u.get("args_chars", 0)))
        for r in tool_results:
            chars = int(r.get("result_chars", 0))
            self.tool_result_chars_total += chars
            self.tool_result_count += 1
            tid = r.get("tool_use_id")
            name = None
            if tid:
                # Look back through recent calls history for the tool name where use.id == tid
                for c in reversed(self.calls):
                    for u in c.get("tool_uses") or []:
                        if u.get("id") == tid:
                            name = u.get("name")
                            break
                    if name:
                        break
            if name:
                stat = self.tools_stat.setdefault(name, ToolStat(name=name))
                stat.absorb_result(chars)


class SessionInspector:
    """``session_id → SessionInspectorEntry``, a bounded LRU."""

    def __init__(self, max_size: int = DEFAULT_MAX_SESSIONS) -> None:
        self._max = max_size
        self._entries: OrderedDict[str, SessionInspectorEntry] = OrderedDict()

    def touch(self, session_id: str) -> SessionInspectorEntry:
        if session_id in self._entries:
            self._entries.move_to_end(session_id)
            return self._entries[session_id]
        entry = SessionInspectorEntry(session_id=session_id)
        self._entries[session_id] = entry
        if len(self._entries) > self._max:
            self._entries.popitem(last=False)
        return entry

    def get(self, session_id: str) -> SessionInspectorEntry | None:
        return self._entries.get(session_id)

    def __len__(self) -> int:
        return len(self._entries)

    def items(self):
        return self._entries.items()


def entry_to_json(entry: SessionInspectorEntry) -> dict[str, Any]:
    """``SessionInspectorEntry`` → JSON-safe dict (for /__telos/developer.json)."""
    return {
        "session_id": entry.session_id,
        "model": entry.last_model,
        "harness": entry.last_harness,
        "created_at": entry.created_at,
        "last_seen": entry.last_seen,
        "last_layout": entry.last_layout,
        "last_plan_slots": entry.last_plan_slots,
        "last_usage_norm": entry.last_usage_norm,
        "last_usage_raw": entry.last_usage_raw,
        "tool_result_count": entry.tool_result_count,
        "tool_result_chars_total": entry.tool_result_chars_total,
        "tools": [
            {
                "name": s.name,
                "invocations": s.invocations,
                "args_chars_total": s.args_chars_total,
                "result_chars_total": s.result_chars_total,
                "result_chars_max": s.result_chars_max,
                "result_chars_min": s.result_chars_min,
                "last_args_chars": s.last_args_chars,
                "last_result_chars": s.last_result_chars,
            }
            for s in entry.tools_stat.values()
        ],
        "calls": entry.calls,
    }
