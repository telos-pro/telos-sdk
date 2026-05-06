#!/usr/bin/env python3
"""生成一份 self-contained 的 HTML dashboard，可视化 prompt_trace + cache 命中。

用法::

    python -m stela.scripts.build_dashboard \\
        --results-dir /tmp/stela-telos-runs \\
        [--instance pallets__flask-5014]... \\
        [--out /tmp/stela-telos-runs/benchmark/dashboard.html]

默认扫描 ``<results-dir>/telos-*.prompt_trace.jsonl``，同名配件
``.usage.jsonl / .result.json / .eval.json`` 会被自动带入。输出一个
纯静态 HTML（inline SVG + CSS，零 JS 依赖，可离线打开）。
"""

from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 加载一个 instance 的所有制品
# ---------------------------------------------------------------------------

def _read_jsonl(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.open():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _read_json(p: Path) -> dict[str, Any]:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {}


def load_instance(trace_path: Path) -> dict[str, Any]:
    # telos-<inst>.prompt_trace.jsonl → tag = telos-<inst>
    name = trace_path.name
    assert name.endswith(".prompt_trace.jsonl"), name
    tag = name[: -len(".prompt_trace.jsonl")]
    inst = tag[len("telos-"):] if tag.startswith("telos-") else tag
    base = trace_path.parent / tag

    trace = _read_jsonl(trace_path)
    result = _read_json(base.with_suffix(".result.json"))
    evald = _read_json(base.with_suffix(".eval.json"))

    raw_in = sum(c["cache"]["raw_input"] for c in trace)
    cache_read = sum(c["cache"]["cache_read"] for c in trace)
    output = sum(c["cache"]["output"] for c in trace)
    inp_total = raw_in + cache_read
    cache_share = (cache_read / inp_total) if inp_total else 0.0
    prefix_vals = [c["prefix"]["prefix_stability"] for c in trace
                   if c["prefix"].get("prefix_stability") is not None]
    prefix_avg = sum(prefix_vals) / len(prefix_vals) if prefix_vals else None

    return {
        "tag": tag,
        "instance_id": inst,
        "model": result.get("model"),
        "duration_s": result.get("duration_s"),
        "patch_bytes": result.get("patch_bytes"),
        "completed": result.get("completed"),
        "api_calls": result.get("api_calls") or len(trace),
        "resolved": evald.get("resolved"),
        "fail_to_pass": (
            f"{evald.get('fail_to_pass_passed', 0)}/"
            f"{evald.get('fail_to_pass_total', 0)}"
            if evald else None
        ),
        "totals": {
            "raw_input": raw_in,
            "cache_read": cache_read,
            "output": output,
            "input_total": inp_total,
            "cache_share": cache_share,
            "prefix_avg": prefix_avg,
            "calls": len(trace),
        },
        "trace": trace,
    }


# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------

CSS = """
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  margin: 0; padding: 24px;
  background: #0e1116; color: #e6edf3;
}
h1 { margin: 0 0 4px 0; font-size: 22px; }
.sub { color: #7d8590; font-size: 12px; margin-bottom: 24px; }

.kpis {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px; margin-bottom: 24px;
}
.kpi {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 14px 16px;
}
.kpi .label { color: #7d8590; font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.05em; margin-bottom: 4px; }
.kpi .value { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
.kpi .delta { font-size: 11px; color: #7d8590; margin-top: 2px; }

.card {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 16px 20px; margin-bottom: 16px;
}
.card-header {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 12px; flex-wrap: wrap; gap: 8px;
}
.card-header h2 { margin: 0; font-size: 15px; font-family: monospace; }
.card-header .meta { color: #7d8590; font-size: 12px; }

.badge {
  display: inline-block; padding: 2px 8px; border-radius: 12px;
  font-size: 11px; font-weight: 600; margin-left: 6px;
}
.badge-ok    { background: #1f6feb33; color: #58a6ff; border: 1px solid #1f6feb66; }
.badge-good  { background: #23863633; color: #3fb950; border: 1px solid #23863666; }
.badge-bad   { background: #da363322; color: #f85149; border: 1px solid #da363366; }
.badge-mute  { background: #21262d; color: #7d8590; border: 1px solid #30363d; }

.row {
  display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px;
  margin-bottom: 14px;
}
.metric { font-size: 12px; }
.metric .label { color: #7d8590; }
.metric .value { font-size: 16px; font-weight: 600; color: #e6edf3;
  font-variant-numeric: tabular-nums; }

table.trace {
  width: 100%; border-collapse: collapse; font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}
table.trace th, table.trace td {
  padding: 6px 8px; text-align: right; border-bottom: 1px solid #21262d;
  vertical-align: middle;
}
table.trace th {
  font-weight: 500; color: #7d8590; text-align: right;
  text-transform: uppercase; font-size: 10px; letter-spacing: 0.04em;
}
table.trace td.left, table.trace th.left { text-align: left; }
table.trace tr:hover td { background: #1c222a; }

.bar-cell { width: 260px; }
.bar { display: flex; height: 14px; border-radius: 3px; overflow: hidden;
  background: #21262d; }
.bar > span { display: block; height: 100%; }
.bar .raw   { background: #f85149; }
.bar .cache { background: #3fb950; }
.bar .out   { background: #58a6ff; }

.prefix-cell { width: 110px; }
.prefix-bar { background: #21262d; height: 6px; border-radius: 3px;
  overflow: hidden; margin-top: 3px; }
.prefix-bar > span { display: block; height: 100%; background: #d29922; }

.legend { display: flex; gap: 16px; font-size: 11px; color: #7d8590;
  margin-bottom: 8px; }
.legend .swatch { display: inline-block; width: 10px; height: 10px;
  border-radius: 2px; vertical-align: middle; margin-right: 4px; }

.plan { font-family: monospace; font-size: 10px; color: #7d8590; }
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_int(n: int | float | None) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}"


def _fmt_pct(x: float | None, decimals: int = 1) -> str:
    if x is None:
        return "—"
    return f"{100 * x:.{decimals}f}%"


def _cache_color(share: float) -> str:
    if share >= 0.6:
        return "#3fb950"
    if share >= 0.3:
        return "#d29922"
    if share > 0:
        return "#f0883e"
    return "#7d8590"


def _role_color(role: str) -> str:
    return {"system": "#d29922", "user": "#58a6ff",
            "assistant": "#3fb950", "tool": "#bc8cff"}.get(role, "#7d8590")


def _role_summary(by_role: dict[str, dict[str, int]]) -> str:
    return " ".join(
        f'<span style="color:{_role_color(r)}">{html.escape(r[0])}={d["count"]}</span>'
        for r, d in by_role.items()
    )


def _resolved_badge(inst: dict[str, Any]) -> str:
    r = inst["resolved"]
    if r is True:
        return '<span class="badge badge-good">resolved</span>'
    if r is False:
        return '<span class="badge badge-bad">unresolved</span>'
    return '<span class="badge badge-mute">not evaluated</span>'


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_call_row(c: dict[str, Any], max_total: int) -> str:
    cache = c["cache"]
    raw = cache["raw_input"]
    cr = cache["cache_read"]
    out = cache["output"]
    total = max(raw + cr + out, 1)
    width_pct = 100.0 * total / max(max_total, 1)
    raw_pct = 100.0 * raw / total
    cr_pct = 100.0 * cr / total
    out_pct = 100.0 * out / total
    cache_share = cache.get("cache_share") or 0.0
    prefix = c["prefix"].get("prefix_stability")
    prefix_pct = _fmt_pct(prefix) if prefix is not None else "—"

    plan = c.get("plan", {}) or {}
    slot_str = ",".join(s["name"] for s in plan.get("slots", [])) or "—"
    if plan.get("routing_key"):
        slot_str = f"key={str(plan['routing_key'])[:10]}+{slot_str}"

    bar_html = (
        f'<div class="bar" style="width:{width_pct:.1f}%">'
        f'<span class="raw"   style="width:{raw_pct:.1f}%" title="raw_input {raw:,}"></span>'
        f'<span class="cache" style="width:{cr_pct:.1f}%"  title="cache_read {cr:,}"></span>'
        f'<span class="out"   style="width:{out_pct:.1f}%" title="output {out:,}"></span>'
        f'</div>'
    )
    prefix_bar_html = (
        f'<div class="prefix-bar"><span style="width:{100*(prefix or 0):.1f}%"></span></div>'
        if prefix is not None else ""
    )

    return (
        '<tr>'
        f'<td>{c.get("call_index", "?")}</td>'
        f'<td>{c.get("latency_s", 0):.2f}s</td>'
        f'<td class="left">{_role_summary(c["wire"]["by_role"])}</td>'
        f'<td>{c["wire"]["total_chars"]:,}</td>'
        f'<td class="bar-cell left">{bar_html}</td>'
        f'<td>{_fmt_int(raw)}</td>'
        f'<td>{_fmt_int(cr)}</td>'
        f'<td>{_fmt_int(out)}</td>'
        f'<td><b style="color:{_cache_color(cache_share)}">{_fmt_pct(cache_share)}</b></td>'
        f'<td class="prefix-cell">{prefix_pct}{prefix_bar_html}</td>'
        f'<td class="left plan">{html.escape(slot_str)}</td>'
        '</tr>'
    )


def render_instance(inst: dict[str, Any]) -> str:
    t = inst["totals"]
    trace = inst["trace"]
    max_total = max(
        (c["cache"]["raw_input"] + c["cache"]["cache_read"] + c["cache"]["output"]
         for c in trace),
        default=1,
    )
    rows = "\n".join(render_call_row(c, max_total) for c in trace) or (
        '<tr><td colspan="11" class="left" style="color:#7d8590">'
        '(no trace rows)</td></tr>'
    )

    badges = _resolved_badge(inst)
    if inst["fail_to_pass"]:
        badges += f' <span class="badge badge-mute">F2P {inst["fail_to_pass"]}</span>'
    if inst.get("completed"):
        badges += ' <span class="badge badge-ok">completed</span>'

    return f"""
<div class="card">
  <div class="card-header">
    <h2>{html.escape(inst["instance_id"])} {badges}</h2>
    <div class="meta">
      {html.escape(str(inst.get("model") or ""))}
      &nbsp;·&nbsp; {inst.get("duration_s") or 0}s
      &nbsp;·&nbsp; patch {_fmt_int(inst.get("patch_bytes"))}B
    </div>
  </div>

  <div class="row">
    <div class="metric"><div class="label">api calls</div>
      <div class="value">{t["calls"]}</div></div>
    <div class="metric"><div class="label">raw_input</div>
      <div class="value">{_fmt_int(t["raw_input"])}</div></div>
    <div class="metric"><div class="label">cache_read</div>
      <div class="value">{_fmt_int(t["cache_read"])}</div></div>
    <div class="metric"><div class="label">output</div>
      <div class="value">{_fmt_int(t["output"])}</div></div>
    <div class="metric"><div class="label">cache_share</div>
      <div class="value" style="color:{_cache_color(t["cache_share"])}">{_fmt_pct(t["cache_share"])}</div></div>
    <div class="metric"><div class="label">prefix_stability avg</div>
      <div class="value">{_fmt_pct(t["prefix_avg"])}</div></div>
  </div>

  <div class="legend">
    <span><span class="swatch" style="background:#f85149"></span>raw_input</span>
    <span><span class="swatch" style="background:#3fb950"></span>cache_read</span>
    <span><span class="swatch" style="background:#58a6ff"></span>output</span>
    <span><span class="swatch" style="background:#d29922"></span>prefix stability</span>
  </div>

  <table class="trace">
    <thead><tr>
      <th>#</th><th>lat</th><th class="left">roles</th>
      <th>wire chars</th>
      <th class="left bar-cell">raw + cache + out (per-call total, normalized)</th>
      <th>raw_in</th><th>cache</th><th>out</th><th>cache%</th>
      <th class="prefix-cell">prefix%</th>
      <th class="left">plan</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
"""


def render_dashboard(instances: list[dict[str, Any]]) -> str:
    n = len(instances)
    n_eval = sum(1 for i in instances if i["resolved"] is not None)
    n_resolved = sum(1 for i in instances if i["resolved"] is True)
    sum_raw = sum(i["totals"]["raw_input"] for i in instances)
    sum_cache = sum(i["totals"]["cache_read"] for i in instances)
    sum_out = sum(i["totals"]["output"] for i in instances)
    sum_calls = sum(i["totals"]["calls"] for i in instances)
    inp_total = sum_raw + sum_cache
    cache_share = (sum_cache / inp_total) if inp_total else 0.0

    resolved_kpi = (
        f"{n_resolved}/{n_eval} ({_fmt_pct(n_resolved / n_eval if n_eval else 0)})"
        if n_eval else "—"
    )
    cards = "\n".join(render_instance(i) for i in instances)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>STELA × Telos · prompt-cache dashboard</title>
<style>{CSS}</style>
</head><body>
<h1>STELA × Telos — prompt &amp; cache dashboard</h1>
<div class="sub">{n} instance(s) · generated {ts}</div>

<div class="kpis">
  <div class="kpi"><div class="label">instances</div>
    <div class="value">{n}</div>
    <div class="delta">{n_eval} evaluated</div></div>
  <div class="kpi"><div class="label">resolved</div>
    <div class="value">{resolved_kpi}</div></div>
  <div class="kpi"><div class="label">total api calls</div>
    <div class="value">{sum_calls:,}</div></div>
  <div class="kpi"><div class="label">raw_input</div>
    <div class="value">{sum_raw:,}</div></div>
  <div class="kpi"><div class="label">cache_read</div>
    <div class="value">{sum_cache:,}</div></div>
  <div class="kpi"><div class="label">output</div>
    <div class="value">{sum_out:,}</div></div>
  <div class="kpi"><div class="label">cache_share</div>
    <div class="value" style="color:{_cache_color(cache_share)}">{_fmt_pct(cache_share)}</div>
    <div class="delta">cache_read / (raw + cache)</div></div>
</div>

{cards}

</body></html>
"""


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--results-dir", default="/tmp/stela-telos-runs",
                    help="扫描该目录下的 telos-*.prompt_trace.jsonl")
    ap.add_argument("--out", default=None,
                    help="输出 HTML 路径（默认 <results-dir>/benchmark/dashboard.html）")
    ap.add_argument("--instance", action="append",
                    help="只包含指定 instance_id（可重复）")
    args = ap.parse_args()

    results_dir = Path(args.results_dir)
    traces = sorted(results_dir.glob("telos-*.prompt_trace.jsonl"))
    if args.instance:
        wanted = set(args.instance)
        traces = [t for t in traces
                  if t.name[len("telos-"):-len(".prompt_trace.jsonl")] in wanted]
    if not traces:
        raise SystemExit(f"no telos-*.prompt_trace.jsonl under {results_dir}")

    instances = [load_instance(t) for t in traces]
    # 未 resolved 在前 → 字典序
    instances.sort(key=lambda x: (x["resolved"] is True, x["instance_id"]))

    out = Path(args.out) if args.out else results_dir / "benchmark" / "dashboard.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_dashboard(instances))
    print(f"[dashboard] wrote {out}  ({len(instances)} instance(s))")
    print(f"           open with: open {out}")


if __name__ == "__main__":
    main()
