#!/usr/bin/env python3
"""\u53ef\u8bfb\u5316\u5c55\u793a prompt_trace.jsonl\u3002

\u7528\u6cd5::

    python -m stela.scripts.show_prompt_trace \\
        /tmp/stela-telos-runs/telos-pallets__flask-5014.prompt_trace.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _fmt(n: int) -> str:
    return f"{n:>8,}" if isinstance(n, int) else f"{n:>8}"


def show(path: Path) -> None:
    rows: list[dict] = []
    for line in path.open():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    if not rows:
        print(f"(empty trace) {path}")
        return

    print(f"# {path}\n")
    print(f"{'#':>3}  {'role-counts':<28}  {'wire chars':>10}  "
          f"{'prefix%':>7}  {'raw_in':>8}  {'cache':>8}  {'out':>6}  "
          f"{'cache%':>6}  {'plan':<28}")
    print("-" * 130)
    cum_raw = cum_cache = cum_out = 0
    for r in rows:
        wire = r["wire"]
        roles = wire["by_role"]
        role_str = " ".join(f"{k[0]}={v['count']}" for k, v in roles.items())
        prefix = r["prefix"]
        ps = prefix.get("prefix_stability")
        prefix_pct = f"{100 * ps:.1f}" if ps is not None else "  -  "
        c = r["cache"]
        cum_raw += c["raw_input"]
        cum_cache += c["cache_read"]
        cum_out += c["output"]
        plan = r["plan"]
        slot_str = ",".join(s["name"] for s in plan["slots"]) or "-"
        if plan.get("routing_key"):
            slot_str = f"key={plan['routing_key'][:14]} {slot_str}"
        print(f"{r['call_index']:>3}  {role_str:<28}  "
              f"{wire['total_chars']:>10,}  {prefix_pct:>7}  "
              f"{c['raw_input']:>8,}  {c['cache_read']:>8,}  "
              f"{c['output']:>6,}  {100 * c['cache_share']:>5.1f}%  "
              f"{slot_str:<28}")

    print("-" * 130)
    inp_total = cum_raw + cum_cache
    overall = (cum_cache / inp_total * 100) if inp_total else 0.0
    print(f"TOTAL  raw_input={cum_raw:,}  cache_read={cum_cache:,}  "
          f"output={cum_out:,}  cache_share={overall:.1f}%  "
          f"calls={len(rows)}")


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
