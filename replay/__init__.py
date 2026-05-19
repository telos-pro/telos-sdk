"""``telos replay`` —— record → replay comparison engine.

Principle
---------
Take the "request sequence" recorded from a real session in the corpus, and for
each mode (none / telos / rtk / both) re-run the **byte-for-byte identical**
turns through the pipeline and send them upstream, taking only the usage. Because
every mode sees exactly the same input, the only variable is the optimization
switch itself —— this is a controlled experiment, with less confounding from
trajectory divergence than "running two independent sessions".

To keep cost down, replay forces ``max_tokens`` to 1 (and strips ``stream`` /
``tool_choice`` / ``thinking``): we only care about the ``cache_read`` /
``cache_write`` billing on the prompt / prefill side, and output generation is
deliberately stubbed. One full real session + a string of cheap prefills per mode
is one or two orders of magnitude cheaper than "running a full agent session for
each of N modes".

Limitations
-----------
- Replay **pins** the trajectory. It measures "the cost of the same conversation
  under different encodings", not "the cost of the same task under different
  configurations". It cannot capture second-order effects —— for example, after
  RTK shortens a tool result, the agent might make a different decision on the
  next step in a real run.
- Cross-mode cache isolation: by default each mode is injected with a unique
  system prefix block (``[telos-replay ns=...]``), so prefix caching on the
  Anthropic side is independent per mode, avoiding "an earlier-replayed mode
  warms the cache and a later-replayed mode freeloads on the hits". This prefix
  is only a few tokens, equal-length across modes, and does not affect the
  relative comparison; it can be disabled with ``cache_isolation=False``.
- It measures prefill / cache billing, not end-to-end task cost. For the latter
  you must run independent sessions.
"""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from telos.bridge import BridgeSessionState
from telos.output_filter import TelosMode, ToolResultFilter, apply_filter, build_filter
from telos.proxy.pipeline import process_anthropic_request

_log = logging.getLogger("telos.replay")

# Upstream sender: takes a wire dict, returns an Anthropic-style raw usage dict
# (or None to indicate that turn's call failed). Injection-style design —— tests
# pass a fake sender and hit no network.
Sender = Callable[[Mapping[str, Any]], "dict[str, Any] | None"]


# ---------------------------------------------------------------------------
# usage normalization
# ---------------------------------------------------------------------------

def _normalize(raw: Mapping[str, Any]) -> dict[str, int]:
    return {
        "raw_input": int(raw.get("input_tokens", 0) or 0),
        "cache_read": int(raw.get("cache_read_input_tokens", 0) or 0),
        "cache_write": int(raw.get("cache_creation_input_tokens", 0) or 0),
        "output": int(raw.get("output_tokens", 0) or 0),
    }


def _usage_obj_to_raw(usage: Any) -> dict[str, Any]:
    """Convert the Anthropic SDK ``Usage`` object into the raw_usage dict the dashboard expects."""
    raw: dict[str, Any] = {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens":
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }
    cc = getattr(usage, "cache_creation", None)
    if cc is not None:
        raw["cache_creation"] = {
            "ephemeral_5m_input_tokens":
                getattr(cc, "ephemeral_5m_input_tokens", 0) or 0,
            "ephemeral_1h_input_tokens":
                getattr(cc, "ephemeral_1h_input_tokens", 0) or 0,
        }
    return raw


# ---------------------------------------------------------------------------
# upstream sender factory
# ---------------------------------------------------------------------------

def anthropic_sender(*, api_key: str | None = None,
                     upstream: str | None = None) -> Sender:
    """Construct a real sender that goes through the Anthropic SDK."""
    from anthropic import Anthropic

    kwargs: dict[str, Any] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if upstream:
        kwargs["base_url"] = upstream
    client = Anthropic(**kwargs)

    def send(wire: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            resp = client.messages.create(**dict(wire))
        except Exception as e:  # noqa: BLE001
            _log.warning("replay upstream call failed: %s", e)
            return None
        return _usage_obj_to_raw(resp.usage)

    return send


# ---------------------------------------------------------------------------
# Cache isolation: inject a unique system prefix per mode
# ---------------------------------------------------------------------------

def _inject_namespace(raw: dict[str, Any], session_id: str, mode_label: str) -> None:
    """Insert a mode-specific namespace block in place at the very front of the ``system`` band.

    Each mode's prefix is therefore different → the Anthropic-side caches are
    independent, and replay order no longer pollutes the comparison numbers. The
    block itself is only ~10 tokens, equal-length across modes.
    """
    tag = {"type": "text", "text": f"[telos-replay ns={session_id}/{mode_label}]"}
    system = raw.get("system")
    if system is None:
        raw["system"] = [tag]
    elif isinstance(system, str):
        raw["system"] = [tag, {"type": "text", "text": system}]
    elif isinstance(system, list):
        raw["system"] = [tag, *system]


# ---------------------------------------------------------------------------
# single-mode replay
# ---------------------------------------------------------------------------

@dataclass
class ReplayResult:
    """A summary of one finished (session, mode) replay."""

    mode: str
    session_id: str
    compare_group: str
    records: list[dict[str, Any]] = field(default_factory=list)
    turns_ok: int = 0
    turns_failed: int = 0

    @property
    def total_cache_read(self) -> int:
        return sum(r["normalized"]["cache_read"] for r in self.records)

    @property
    def total_cache_write(self) -> int:
        return sum(r["normalized"]["cache_write"] for r in self.records)

    @property
    def total_raw_input(self) -> int:
        return sum(r["normalized"]["raw_input"] for r in self.records)


def replay_session(
    turns: list[Mapping[str, Any]],
    mode: TelosMode,
    *,
    session_id: str,
    compare_group: str,
    sender: Sender,
    flt: ToolResultFilter | None = None,
    cache_isolation: bool = True,
) -> ReplayResult:
    """Replay a session's turn sequence once under ``mode``.

    Args:
        turns:           the list of turn records from the corpus (each contains ``request``).
        mode:            the switch combination used for this replay.
        session_id:      the original session id (usage_log records ``<id>/<mode>``).
        compare_group:   the comparison group key (the dashboard places these side by side).
        sender:          the wire → raw_usage callable; tests can inject a fake implementation.
        flt:             the RTK filter; ``build_filter()`` is called on demand when ``None``.
        cache_isolation: whether to inject a unique system prefix per mode (see the module docstring).
    """
    if flt is None:
        flt = build_filter()
    state = BridgeSessionState()
    result = ReplayResult(mode=mode.label, session_id=session_id,
                          compare_group=compare_group)
    replay_sid = f"{session_id}/{mode.label}"

    for turn in turns:
        request = turn.get("request")
        if not isinstance(request, Mapping):
            continue
        raw = copy.deepcopy(dict(request))
        if cache_isolation:
            _inject_namespace(raw, session_id, mode.label)

        reduction: dict[str, Any] = {}
        effective: Mapping[str, Any] = raw
        if mode.rtk:
            effective, fstats = apply_filter(raw, flt)
            reduction = fstats.as_dict()

        if mode.telos:
            try:
                pr = process_anthropic_request(
                    effective, session_id=replay_sid, session_state=state)
                wire = dict(pr.wire)
                harness = pr.harness
            except Exception:  # noqa: BLE001
                _log.exception("replay pipeline failed, falling back to passthrough")
                wire = dict(effective)
                harness = "passthrough"
        else:
            wire = dict(effective)
            harness = "rtk-only" if mode.rtk else "passthrough"

        # Stub output generation: only measure prompt / prefill side cost.
        wire["max_tokens"] = 1
        for k in ("stream", "tool_choice", "thinking"):
            wire.pop(k, None)

        raw_usage = sender(wire)
        if raw_usage is None:
            result.turns_failed += 1
            continue

        result.turns_ok += 1
        result.records.append({
            "ts": time.time(),
            "session_id": replay_sid,
            "call_index": int(turn.get("call_index") or len(result.records) + 1),
            "model": wire.get("model") or request.get("model") or "",
            "harness": harness,
            "mode": mode.label,
            "compare_group": compare_group,
            "replay": True,
            "tool_output_reduction": reduction,
            "raw_usage": raw_usage,
            "normalized": _normalize(raw_usage),
        })

    return result
