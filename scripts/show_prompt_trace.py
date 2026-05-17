#!/usr/bin/env python3
"""Pretty-print prompt_trace.jsonl produced by ``TelosOpenAITransport``.

Per call we now surface, beyond the headline cache numbers:

- **Prompt regions** — chars per (segment × band): tools / system / messages
  crossed with PIN / FOLD / DROP. This is the prompt's "physical layout"
  that Bridge sees right before emit.
- **Growth (Δ)** — change vs the previous call, segment-by-segment. Lets
  you spot which region is bloating across iterations (typically the
  ``messages`` band growing because of tool_result turns).
- **Breakpoints (BP)** — which cache anchors the engine adapter placed,
  with their TTL class. These are the "bp 点".
- **Task status footer** — if a sibling ``<tag>.result.json`` exists,
  we print whether the task completed and (if not) the inferred reason.

Usage::

    python -m telos.scripts.show_prompt_trace \\
        /tmp/telos-telos-runs/telos-pallets__flask-5014.prompt_trace.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _signed(n: int | float | None) -> str:
    if n is None:
        return "·"
    n = int(n)
    if n == 0:
        return "0"
    return f"{n:+,}"


def _result_path(trace_path: Path) -> Path:
    name = trace_path.name
    if name.endswith(".prompt_trace.jsonl"):
        tag = name[: -len(".prompt_trace.jsonl")]
        return trace_path.parent / f"{tag}.result.json"
    return trace_path.with_suffix(".result.json")


def classify_status(result: dict[str, Any]) -> tuple[str, str | None]:
    """Return ``(status, reason)``. ``status`` is ``"completed"`` or ``"anomalous"``.

    Used by both the CLI footer and the dashboard so the two views agree.
    """
    if not result:
        return "anomalous", "no result.json (run aborted before write?)"
    if result.get("error"):
        return "anomalous", f"error: {result['error']}"
    if not result.get("completed"):
        return "anomalous", ("agent did not emit MINI_SWE_AGENT_FINAL_OUTPUT "
                             "(max-iter / hung?)")
    if not result.get("non_empty_patch"):
        return "anomalous", "completed but produced empty patch"
    return "completed", None


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def _print_call_table(rows: list[dict[str, Any]]) -> None:
    print(f"{'#':>3}  {'lat':>5}  "
          f"{'roles':<22}  {'wire':>9}  "
          f"{'raw_in':>8}  {'cache':>8}  {'out':>5}  {'cache%':>6}  "
          f"{'pfx%':>5}  "
          f"{'tools/Δ':>13}  {'system/Δ':>13}  {'msgs/Δ':>15}  "
          f"{'BPs (name/ttl)':<28}")
    print("-" * 170)
    cum_raw = cum_cache = cum_out = 0
    for r in rows:
        wire = r["wire"]
        roles = wire["by_role"]
        role_str = " ".join(f"{k[0]}={v['count']}" for k, v in roles.items())
        prefix = r["prefix"]
        ps = prefix.get("prefix_stability")
        prefix_pct = f"{100 * ps:.1f}" if ps is not None else "-"
        c = r["cache"]
        cum_raw += c["raw_input"]
        cum_cache += c["cache_read"]
        cum_out += c["output"]

        regions = (r.get("regions") or {}).get("by_segment") or {}
        deltas = r.get("region_deltas") or {}
        d_seg = (deltas.get("by_segment") or {}) if not deltas.get("first_call", True) else {}

        def _seg(seg: str) -> str:
            cur = (regions.get(seg) or {}).get("total", 0)
            d = d_seg.get(seg) if d_seg else None
            return f"{cur:>6,}/{_signed(d):>6}"

        bps = r.get("breakpoints") or (r.get("plan", {}).get("slots") or [])
        bp_str = " ".join(f"{s['name']}({s['ttl_class'][0]})" for s in bps) or "-"
        if r.get("plan", {}).get("routing_key"):
            bp_str = f"key+{bp_str}"

        print(f"{r['call_index']:>3}  {r.get('latency_s', 0):>4.1f}s  "
              f"{role_str:<22}  {wire['total_chars']:>9,}  "
              f"{c['raw_input']:>8,}  {c['cache_read']:>8,}  "
              f"{c['output']:>5,}  {100 * c['cache_share']:>5.1f}%  "
              f"{prefix_pct:>5}  "
              f"{_seg('tools'):>13}  {_seg('system'):>13}  {_seg('messages'):>15}  "
              f"{bp_str:<28}")
    print("-" * 170)
    inp_total = cum_raw + cum_cache
    overall = (cum_cache / inp_total * 100) if inp_total else 0.0
    print(f"TOTAL  raw_input={cum_raw:,}  cache_read={cum_cache:,}  "
          f"output={cum_out:,}  cache_share={overall:.1f}%  "
          f"calls={len(rows)}")


def _print_band_breakdown(rows: list[dict[str, Any]]) -> None:
    """Aggregate prompt chars summed across all calls, per band × segment."""
    by_band = {b: {"tools": 0, "system": 0, "messages": 0}
               for b in ("PIN", "FOLD", "DROP")}
    for r in rows:
        regions = (r.get("regions") or {}).get("by_segment") or {}
        for seg, vals in regions.items():
            for b in ("PIN", "FOLD", "DROP"):
                by_band[b][seg] = by_band[b].get(seg, 0) + int(vals.get(b, 0))
    total = sum(sum(v.values()) for v in by_band.values()) or 1
    print("\nPrompt region totals  (chars sent across all calls; share of cumulative input):")
    print(f"  {'band':<6}  {'tools':>10}  {'system':>10}  {'messages':>10}  "
          f"{'sum':>10}  {'share':>6}")
    for band, segs in by_band.items():
        s = sum(segs.values())
        print(f"  {band:<6}  {segs['tools']:>10,}  {segs['system']:>10,}  "
              f"{segs['messages']:>10,}  {s:>10,}  {100 * s / total:>5.1f}%")


def _print_status_footer(trace_path: Path) -> None:
    rp = _result_path(trace_path)
    if not rp.exists():
        print(f"\n[status] no companion result.json next to {trace_path.name}")
        return
    try:
        result = json.loads(rp.read_text())
    except Exception as e:  # noqa: BLE001
        print(f"\n[status] failed to parse {rp.name}: {e}")
        return
    status, reason = classify_status(result)
    badge = "✓ completed" if status == "completed" else "✗ ANOMALOUS"
    print(f"\n[status] {badge}   instance={result.get('instance_id')}   "
          f"duration={result.get('duration_s')}s   "
          f"api_calls={result.get('api_calls')}   "
          f"patch_bytes={result.get('patch_bytes')}")
    if reason:
        print(f"         reason: {reason}")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def show(path: Path) -> None:
    rows: list[dict[str, Any]] = []
    for line in path.open():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    if not rows:
        print(f"(empty trace) {path}")
        _print_status_footer(path)
        return

    print(f"# {path}\n")
    _print_call_table(rows)
    _print_band_breakdown(rows)
    _print_status_footer(path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("trace", nargs="+", help="prompt_trace.jsonl path(s)")
    args = ap.parse_args()
    for i, t in enumerate(args.trace):
        if i:
            print()
        show(Path(t))


if __name__ == "__main__":
    main()
