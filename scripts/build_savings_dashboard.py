#!/usr/bin/env python3
"""STELA savings dashboard —— 把 usage_log 聚合成「节约了多少 token / 多少美刀」。

输入：一个或多个 ``usage_log`` jsonl 文件（proxy 或 SDK transport 都行；
schema 见 docs/User-guide.md §7.1）。

用法::

    stela dashboard --usage-log ~/.stela/usage.jsonl
    # 或多个：
    stela dashboard --usage-log a.jsonl --usage-log b.jsonl --out savings.html

输出：纯静态 HTML（inline SVG + CSS，零 JS），离线可开。
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# 价格表（USD / 1M tokens，2026 年 Anthropic / DeepSeek 公开价）
# ---------------------------------------------------------------------------

_PRICING: dict[str, dict[str, float]] = {
    # Anthropic
    "claude-opus-4-7":   {"input": 15.00, "cache_read": 1.50, "cache_write": 18.75, "output": 75.00},
    "claude-opus-4-6":   {"input": 15.00, "cache_read": 1.50, "cache_write": 18.75, "output": 75.00},
    "claude-opus-4-5":   {"input": 15.00, "cache_read": 1.50, "cache_write": 18.75, "output": 75.00},
    "claude-opus-4":     {"input": 15.00, "cache_read": 1.50, "cache_write": 18.75, "output": 75.00},
    "claude-sonnet-4-6": {"input":  3.00, "cache_read": 0.30, "cache_write":  3.75, "output": 15.00},
    "claude-sonnet-4-5": {"input":  3.00, "cache_read": 0.30, "cache_write":  3.75, "output": 15.00},
    "claude-sonnet-4":   {"input":  3.00, "cache_read": 0.30, "cache_write":  3.75, "output": 15.00},
    "claude-haiku-4-5":  {"input":  0.80, "cache_read": 0.08, "cache_write":  1.00, "output":  4.00},
    "claude-haiku-4":    {"input":  0.80, "cache_read": 0.08, "cache_write":  1.00, "output":  4.00},
    # OpenAI / DeepSeek（供参考；cache 字段在某些 model 上 == input）
    "gpt-5":             {"input":  5.00, "cache_read": 1.25, "cache_write": 0.00, "output": 15.00},
    "gpt-5.1":           {"input":  5.00, "cache_read": 1.25, "cache_write": 0.00, "output": 15.00},
    "deepseek-chat":     {"input":  0.27, "cache_read": 0.07, "cache_write": 0.00, "output":  1.10},
    "deepseek-v3":       {"input":  0.27, "cache_read": 0.07, "cache_write": 0.00, "output":  1.10},
    # 兜底：按 Sonnet 价当作"中等价位"估
    "_default":          {"input":  3.00, "cache_read": 0.30, "cache_write":  3.75, "output": 15.00},
}


def _price_for(model: str) -> dict[str, float]:
    """模糊匹配 ``model`` 字段到价格表。仅匹配前缀，长 prefix 优先。"""
    if not model:
        return _PRICING["_default"]
    candidates = sorted(
        (k for k in _PRICING if k != "_default" and model.startswith(k)),
        key=len, reverse=True,
    )
    return _PRICING[candidates[0]] if candidates else _PRICING["_default"]


def _cost_usd(model: str, n: dict[str, int]) -> dict[str, float]:
    """单条 call 在该 model 价表下的成本拆解（USD）。"""
    p = _price_for(model)
    return {
        "raw_input":   p["input"]       * n["raw_input"]   / 1_000_000,
        "cache_read":  p["cache_read"]  * n["cache_read"]  / 1_000_000,
        "cache_write": p["cache_write"] * n["cache_write"] / 1_000_000,
        "output":      p["output"]      * n["output"]      / 1_000_000,
    }


def _saved_usd_for_call(model: str, n: dict[str, int]) -> float:
    """这一 call 因为命中 cache 而省下的钱：
    cache_read 量 × (input_price − cache_read_price) / 1M。

    解释：cache_read 这些 token 如果不命中 cache，本来要按 input_price 计费；
    现在按 cache_read_price 计费，差价就是节省。
    """
    p = _price_for(model)
    saving_per_token = max(p["input"] - p["cache_read"], 0.0)
    return saving_per_token * n["cache_read"] / 1_000_000


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------

@dataclass
class _Agg:
    """累计 4 个 token bucket + 美元 + 计次。"""
    raw_input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    cost_usd: float = 0.0
    saved_usd: float = 0.0
    calls: int = 0
    last_ts: float = 0.0

    def add(self, n: dict[str, int], cost: dict[str, float], saved: float,
            ts: float) -> None:
        self.raw_input += n["raw_input"]
        self.cache_read += n["cache_read"]
        self.cache_write += n["cache_write"]
        self.output += n["output"]
        self.cost_usd += sum(cost.values())
        self.saved_usd += saved
        self.calls += 1
        if ts > self.last_ts:
            self.last_ts = ts


@dataclass
class Summary:
    total: _Agg = field(default_factory=_Agg)
    by_harness: dict[str, _Agg] = field(default_factory=lambda: defaultdict(_Agg))
    by_model: dict[str, _Agg] = field(default_factory=lambda: defaultdict(_Agg))
    by_session: dict[str, _Agg] = field(default_factory=lambda: defaultdict(_Agg))
    # 时间序列：以 hour bucket 累计 cache_read / saved_usd / calls
    timeline: dict[str, dict[str, float]] = field(
        default_factory=lambda: defaultdict(lambda: {"cache_read": 0.0,
                                                       "saved_usd": 0.0,
                                                       "calls": 0.0})
    )
    first_ts: float | None = None
    last_ts: float | None = None
    sessions_seen: set[str] = field(default_factory=set)


def aggregate(records: Iterable[dict[str, Any]]) -> Summary:
    s = Summary()
    for rec in records:
        n = rec.get("normalized") or {}
        if not n:
            continue
        n_dict = {
            "raw_input": int(n.get("raw_input", 0) or 0),
            "cache_read": int(n.get("cache_read", 0) or 0),
            "cache_write": int(n.get("cache_write", 0) or 0),
            "output": int(n.get("output", 0) or 0),
        }
        model = rec.get("model") or ""
        harness = rec.get("harness") or "?"
        session = rec.get("session_id") or "(no-session)"
        ts = float(rec.get("ts") or 0.0)

        cost = _cost_usd(model, n_dict)
        saved = _saved_usd_for_call(model, n_dict)

        s.total.add(n_dict, cost, saved, ts)
        s.by_harness[harness].add(n_dict, cost, saved, ts)
        s.by_model[model or "(unknown)"].add(n_dict, cost, saved, ts)
        s.by_session[session].add(n_dict, cost, saved, ts)
        s.sessions_seen.add(session)

        if ts > 0:
            bucket = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:00")
            tb = s.timeline[bucket]
            tb["cache_read"] += n_dict["cache_read"]
            tb["saved_usd"] += saved
            tb["calls"] += 1
            if s.first_ts is None or ts < s.first_ts:
                s.first_ts = ts
            if s.last_ts is None or ts > s.last_ts:
                s.last_ts = ts
    return s


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _resolve_inputs(patterns: list[str]) -> list[Path]:
    """支持 glob 通配。返回去重 + 字典序的路径列表。"""
    paths: list[Path] = []
    for p in patterns:
        expanded = glob.glob(p, recursive=True)
        if not expanded and Path(p).exists():
            expanded = [p]
        for e in expanded:
            paths.append(Path(e))
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in sorted(paths):
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', sans-serif;
  margin: 0;
  padding: 0;
  background: radial-gradient(ellipse at top left, #1a2030 0%, #0a0d12 60%);
  color: #e6edf3;
  min-height: 100vh;
}
.wrap { max-width: 1200px; margin: 0 auto; padding: 32px 24px 64px; }

header { margin-bottom: 32px; }
header h1 { margin: 0 0 6px 0; font-size: 28px; font-weight: 700;
  letter-spacing: -0.01em;
  background: linear-gradient(120deg, #79c0ff 0%, #d2a8ff 100%);
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent;
}
header .sub { color: #7d8590; font-size: 13px; }

/* ---- hero stats ---- */
.hero {
  display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-bottom: 24px;
}
.hero-card {
  background: linear-gradient(140deg, #1a2436 0%, #131822 100%);
  border: 1px solid #2a3346; border-radius: 14px;
  padding: 24px 28px; position: relative; overflow: hidden;
}
.hero-card.green::before, .hero-card.purple::before {
  content: ''; position: absolute; right: -40px; top: -40px;
  width: 200px; height: 200px; border-radius: 50%;
  filter: blur(60px); opacity: 0.3;
}
.hero-card.green::before  { background: #3fb950; }
.hero-card.purple::before { background: #d2a8ff; }

.hero-card .label {
  color: #8b949e; font-size: 12px; text-transform: uppercase;
  letter-spacing: 0.08em; font-weight: 500; position: relative;
}
.hero-card .value {
  font-size: 44px; font-weight: 700; font-variant-numeric: tabular-nums;
  margin: 6px 0 4px 0; position: relative; letter-spacing: -0.02em;
}
.hero-card .sub {
  color: #8b949e; font-size: 13px; position: relative;
}
.hero-card.green .value  { color: #56d364; }
.hero-card.purple .value { color: #d2a8ff; }

/* ---- KPI strip ---- */
.kpis {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px; margin-bottom: 32px;
}
.kpi {
  background: #161b22; border: 1px solid #30363d; border-radius: 10px;
  padding: 14px 16px;
}
.kpi .label { color: #7d8590; font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.06em; margin-bottom: 4px; }
.kpi .value { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums;
  letter-spacing: -0.01em; }
.kpi .sub { font-size: 11px; color: #7d8590; margin-top: 2px; }

/* ---- card ---- */
.card {
  background: #0f141c; border: 1px solid #21262d; border-radius: 12px;
  padding: 22px 24px; margin-bottom: 18px;
}
.card h2 {
  margin: 0 0 14px 0; font-size: 14px; font-family: monospace;
  color: #7d8590; text-transform: uppercase; letter-spacing: 0.06em;
  font-weight: 500;
}

/* ---- segmented bar ---- */
.seg-bar {
  display: flex; height: 28px; border-radius: 6px; overflow: hidden;
  background: #161b22; margin: 8px 0 14px 0;
}
.seg-bar > span {
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 600; color: #0a0d12;
  font-variant-numeric: tabular-nums;
}
.seg-bar .raw_input   { background: #f0883e; }
.seg-bar .cache_read  { background: #3fb950; }
.seg-bar .cache_write { background: #d29922; }
.seg-bar .output      { background: #79c0ff; }
.seg-bar > span:empty { color: transparent; }

.seg-legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; }
.seg-legend > span { color: #8b949e; }
.seg-legend .sw { display: inline-block; width: 10px; height: 10px;
  border-radius: 2px; vertical-align: middle; margin-right: 5px; }

/* ---- table ---- */
table {
  width: 100%; border-collapse: collapse; font-size: 12.5px;
  font-variant-numeric: tabular-nums;
}
th, td { padding: 8px 10px; border-bottom: 1px solid #21262d; text-align: right;
  vertical-align: middle; }
th { font-weight: 500; color: #7d8590; text-transform: uppercase;
  font-size: 10.5px; letter-spacing: 0.05em; }
th.left, td.left { text-align: left; }
td.left { font-family: monospace; }
tr:hover td { background: #131822; }

.bar-cell {
  position: relative; width: 220px;
  background: #161b22; border-radius: 3px; overflow: hidden;
}
.bar-cell .fill {
  position: absolute; left: 0; top: 0; bottom: 0;
  background: linear-gradient(90deg, #3fb950 0%, #56d364 100%);
  border-radius: 3px;
}
.bar-cell .label-overlay {
  position: relative; padding: 2px 8px; color: #e6edf3;
  font-size: 11px; font-weight: 500;
}

/* ---- timeline ---- */
.timeline { margin-top: 4px; }
.timeline svg { display: block; }

.muted { color: #7d8590; }
.gold  { color: #d29922; }
.green { color: #56d364; }
.blue  { color: #79c0ff; }
.lilac { color: #d2a8ff; }

.footer { margin-top: 40px; color: #4f5862; font-size: 11px; text-align: center; }
"""


def _fmt_int(n: int | float) -> str:
    return f"{int(n):,}"


def _fmt_tokens(n: int) -> str:
    """7,453,210 → 7.45M / 1234 → 1.23K"""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.2f}K"
    return f"{n:,}"


def _fmt_usd(x: float) -> str:
    if x >= 100:
        return f"${x:,.2f}"
    if x >= 1:
        return f"${x:.2f}"
    if x >= 0.01:
        return f"${x:.3f}"
    return f"${x:.4f}"


def _fmt_pct(x: float, decimals: int = 1) -> str:
    return f"{100 * x:.{decimals}f}%"


def _render_seg_bar(parts: list[tuple[str, int, str]]) -> str:
    """parts: [(class, n, hover_label), ...]"""
    total = max(sum(p[1] for p in parts), 1)
    segs = []
    for cls, n, label in parts:
        pct = 100 * n / total
        text = _fmt_tokens(n) if pct >= 5 else ""
        segs.append(
            f'<span class="{cls}" style="width:{pct:.2f}%" title="{html.escape(label)}: {n:,}">{text}</span>'
        )
    return '<div class="seg-bar">' + "".join(segs) + "</div>"


def _render_breakdown_table(label: str, data: dict[str, _Agg],
                             *, key_label: str, max_rows: int = 12) -> str:
    if not data:
        return ""
    rows_sorted = sorted(data.items(), key=lambda kv: -kv[1].cache_read)
    if len(rows_sorted) > max_rows:
        rows_sorted = rows_sorted[:max_rows]
    if not rows_sorted:
        return ""

    max_saved = max((a.saved_usd for _, a in rows_sorted), default=0.0)

    rows = []
    for key, a in rows_sorted:
        share = (a.cache_read / (a.cache_read + a.raw_input)) if (a.cache_read + a.raw_input) else 0.0
        bar_pct = (100 * a.saved_usd / max_saved) if max_saved > 0 else 0.0
        rows.append(
            f"<tr>"
            f'<td class="left">{html.escape(str(key))}</td>'
            f"<td>{_fmt_int(a.calls)}</td>"
            f"<td>{_fmt_tokens(a.raw_input)}</td>"
            f'<td class="green">{_fmt_tokens(a.cache_read)}</td>'
            f"<td>{_fmt_pct(share)}</td>"
            f'<td class="bar-cell">'
            f'<span class="fill" style="width:{bar_pct:.1f}%"></span>'
            f'<span class="label-overlay">{_fmt_usd(a.saved_usd)}</span>'
            f"</td>"
            f"</tr>"
        )
    return f"""
<div class="card">
  <h2>{html.escape(label)}</h2>
  <table>
    <thead><tr>
      <th class="left">{html.escape(key_label)}</th>
      <th>calls</th>
      <th>raw_input</th>
      <th>cache_read</th>
      <th>hit%</th>
      <th class="left">saved $</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</div>
"""


def _render_timeline_svg(timeline: dict[str, dict[str, float]]) -> str:
    if not timeline:
        return '<p class="muted">no timestamped data</p>'

    items = sorted(timeline.items())
    W = 1100
    H = 180
    pad_l, pad_r, pad_t, pad_b = 50, 14, 12, 24
    plot_w = W - pad_l - pad_r
    plot_h = H - pad_t - pad_b

    n = len(items)
    max_saved = max((it[1]["saved_usd"] for it in items), default=0.0) or 1.0
    max_cache = max((it[1]["cache_read"] for it in items), default=0.0) or 1.0

    # cache_read bars
    bar_w = plot_w / n * 0.7
    gap = plot_w / n - bar_w
    bars: list[str] = []
    for i, (_, v) in enumerate(items):
        h_px = plot_h * (v["cache_read"] / max_cache)
        x = pad_l + i * (plot_w / n) + gap / 2
        y = pad_t + plot_h - h_px
        bars.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" '
            f'height="{h_px:.2f}" fill="#3fb950" opacity="0.55" rx="2">'
            f'<title>{items[i][0]}\ncache_read: {int(v["cache_read"]):,}</title>'
            f'</rect>'
        )

    # saved_usd line
    pts = []
    for i, (_, v) in enumerate(items):
        x = pad_l + i * (plot_w / n) + (plot_w / n) / 2
        y = pad_t + plot_h - plot_h * (v["saved_usd"] / max_saved)
        pts.append((x, y))
    line = "M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in pts)

    # y-axis labels (left = cache_read, right = saved)
    y_labels = []
    for frac, label in [(1.0, max_cache), (0.5, max_cache / 2), (0.0, 0)]:
        y = pad_t + plot_h * (1 - frac)
        y_labels.append(
            f'<text x="{pad_l - 6:.1f}" y="{y + 3:.1f}" text-anchor="end" '
            f'font-size="9" fill="#7d8590">{_fmt_tokens(int(label))}</text>'
        )
        y_labels.append(
            f'<line x1="{pad_l:.1f}" y1="{y:.1f}" x2="{W - pad_r:.1f}" y2="{y:.1f}" '
            f'stroke="#21262d" stroke-width="1"/>'
        )

    # x-axis labels (3 evenly-spaced)
    x_labels = []
    for i in (0, n // 2, n - 1) if n >= 3 else range(n):
        x = pad_l + i * (plot_w / n) + (plot_w / n) / 2
        x_labels.append(
            f'<text x="{x:.1f}" y="{H - 6:.1f}" text-anchor="middle" '
            f'font-size="9" fill="#7d8590">{html.escape(items[i][0][-13:])}</text>'
        )

    # markers on the line
    dots = "".join(
        f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.5" fill="#d2a8ff">'
        f'<title>{items[i][0]}\nsaved: {_fmt_usd(items[i][1]["saved_usd"])}</title>'
        f'</circle>'
        for i, (x, y) in enumerate(pts)
    )

    return f"""
<svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="xMidYMid meet">
  {''.join(y_labels)}
  {''.join(bars)}
  <path d="{line}" stroke="#d2a8ff" stroke-width="1.8" fill="none"/>
  {dots}
  {''.join(x_labels)}
  <text x="{pad_l}" y="{pad_t - 2}" font-size="9" fill="#3fb950" font-family="monospace">
    cache_read (bars)
  </text>
  <text x="{pad_l + 130}" y="{pad_t - 2}" font-size="9" fill="#d2a8ff" font-family="monospace">
    saved $ (line)
  </text>
</svg>
"""


def render_dashboard(
    summary: Summary,
    sources: list[Path],
    *,
    refresh_seconds: int | None = None,
) -> str:
    total = summary.total
    inp = total.raw_input + total.cache_read
    hit_rate = total.cache_read / inp if inp else 0.0
    counterfactual_cost = total.cost_usd + total.saved_usd  # 没有 STELA 估计要花多少
    saved_share = total.saved_usd / counterfactual_cost if counterfactual_cost else 0.0

    if summary.first_ts and summary.last_ts:
        span = summary.last_ts - summary.first_ts
        if span < 60:
            span_s = f"{int(span)} 秒"
        elif span < 3600:
            span_s = f"{span / 60:.1f} 分钟"
        elif span < 86400:
            span_s = f"{span / 3600:.1f} 小时"
        else:
            span_s = f"{span / 86400:.1f} 天"
    else:
        span_s = "—"

    n_sessions = len(summary.sessions_seen)
    n_calls = total.calls

    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sources_html = "<br>".join(f"<code>{html.escape(str(s))}</code>" for s in sources)

    seg_bar = _render_seg_bar([
        ("raw_input",   total.raw_input,   "raw_input"),
        ("cache_read",  total.cache_read,  "cache_read"),
        ("cache_write", total.cache_write, "cache_write"),
        ("output",      total.output,      "output"),
    ])

    by_harness = _render_breakdown_table(
        "Breakdown by harness", summary.by_harness, key_label="harness"
    )
    by_model = _render_breakdown_table(
        "Breakdown by model", summary.by_model, key_label="model"
    )
    by_session = _render_breakdown_table(
        "Top sessions by saved $", summary.by_session,
        key_label="session_id", max_rows=15
    )

    timeline_svg = _render_timeline_svg(summary.timeline)

    refresh_tag = (
        f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'
        if refresh_seconds and refresh_seconds > 0 else ""
    )
    refresh_note = (
        f' · auto-refresh {int(refresh_seconds)}s'
        if refresh_seconds and refresh_seconds > 0 else ""
    )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
{refresh_tag}
<title>STELA · Token Savings Dashboard</title>
<style>{CSS}</style>
</head><body>
<div class="wrap">

<header>
  <h1>STELA · Token Savings</h1>
  <div class="sub">
    {n_calls:,} calls · {n_sessions:,} sessions · span {span_s}
    · generated {ts_now}{refresh_note}
  </div>
</header>

<section class="hero">
  <div class="hero-card green">
    <div class="label">tokens saved</div>
    <div class="value">{_fmt_tokens(total.cache_read)}</div>
    <div class="sub">
      cache hits 占总 input 的 <b class="green">{_fmt_pct(hit_rate)}</b>
      ·  绝对量 <code>{_fmt_int(total.cache_read)}</code> tokens
    </div>
  </div>
  <div class="hero-card purple">
    <div class="label">cost saved (estimated)</div>
    <div class="value">{_fmt_usd(total.saved_usd)}</div>
    <div class="sub">
      若无 STELA，预计要付 <b class="lilac">{_fmt_usd(counterfactual_cost)}</b>
      · 实付 <b>{_fmt_usd(total.cost_usd)}</b>
      · 节省 <b class="lilac">{_fmt_pct(saved_share)}</b>
    </div>
  </div>
</section>

<div class="kpis">
  <div class="kpi"><div class="label">total calls</div>
    <div class="value">{_fmt_int(total.calls)}</div></div>
  <div class="kpi"><div class="label">unique sessions</div>
    <div class="value">{_fmt_int(n_sessions)}</div></div>
  <div class="kpi"><div class="label">raw input</div>
    <div class="value gold">{_fmt_tokens(total.raw_input)}</div>
    <div class="sub">{_fmt_int(total.raw_input)}</div></div>
  <div class="kpi"><div class="label">cache read</div>
    <div class="value green">{_fmt_tokens(total.cache_read)}</div>
    <div class="sub">{_fmt_int(total.cache_read)}</div></div>
  <div class="kpi"><div class="label">cache write</div>
    <div class="value gold">{_fmt_tokens(total.cache_write)}</div>
    <div class="sub">{_fmt_int(total.cache_write)}</div></div>
  <div class="kpi"><div class="label">output</div>
    <div class="value blue">{_fmt_tokens(total.output)}</div>
    <div class="sub">{_fmt_int(total.output)}</div></div>
</div>

<div class="card">
  <h2>Token mix（全局）</h2>
  {seg_bar}
  <div class="seg-legend">
    <span><span class="sw" style="background:#f0883e"></span>raw_input · {_fmt_tokens(total.raw_input)}</span>
    <span><span class="sw" style="background:#3fb950"></span>cache_read · {_fmt_tokens(total.cache_read)}</span>
    <span><span class="sw" style="background:#d29922"></span>cache_write · {_fmt_tokens(total.cache_write)}</span>
    <span><span class="sw" style="background:#79c0ff"></span>output · {_fmt_tokens(total.output)}</span>
  </div>
</div>

<div class="card timeline">
  <h2>Activity over time（按小时聚合）</h2>
  {timeline_svg}
</div>

{by_harness}
{by_model}
{by_session}

<div class="footer">
  数据源 · {sources_html}<br>
  价格表 = Anthropic / DeepSeek 公开定价（USD per 1M tokens）；未识别 model 走 Sonnet 价位估算。
</div>

</div>
</body></html>
"""


def render_from_usage_log(
    path: Path | None,
    *,
    refresh_seconds: int | None = None,
) -> str:
    """Live-server helper：从单个 usage_log 文件读 → 渲染。

    专给 proxy 内嵌端点用：永远返回一个可显示的 HTML，即使日志缺失 / 为空。
    缺失或空时也带上 ``refresh_tag``，浏览器自己会等下一波。
    """
    if path is None or not path.exists():
        return _render_empty(
            "No usage_log configured.",
            "Restart the proxy with --usage-log <path> to enable.",
            refresh_seconds=refresh_seconds,
        )
    records = list(_read_jsonl(path))
    if not records:
        return _render_empty(
            "Waiting for first request…",
            f"Watching <code>{html.escape(str(path))}</code>. "
            "Send a request through the proxy and this page will populate.",
            refresh_seconds=refresh_seconds,
        )
    summary = aggregate(records)
    return render_dashboard(summary, [path], refresh_seconds=refresh_seconds)


def _render_empty(title: str, body: str, *,
                   refresh_seconds: int | None) -> str:
    """空状态 HTML stub —— 保留 auto-refresh，等数据进来。"""
    refresh_tag = (
        f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'
        if refresh_seconds and refresh_seconds > 0 else ""
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
{refresh_tag}
<title>STELA · Token Savings</title>
<style>{CSS}</style>
</head><body><div class="wrap">
<header><h1>STELA · Token Savings</h1></header>
<div class="card">
  <h2>{html.escape(title)}</h2>
  <p class="muted">{body}</p>
</div>
</div></body></html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="stela.scripts.build_savings_dashboard",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--usage-log", action="append", required=True,
                    help="usage_log jsonl 路径或 glob，可重复")
    ap.add_argument("--out", default="stela_savings.html",
                    help="输出 HTML 路径（默认 ./stela_savings.html）")
    args = ap.parse_args(argv)

    sources = _resolve_inputs(args.usage_log)
    if not sources:
        raise SystemExit("no usage_log files matched")

    records: list[dict[str, Any]] = []
    for src in sources:
        records.extend(_read_jsonl(src))

    if not records:
        raise SystemExit(f"all {len(sources)} usage_log file(s) were empty / malformed")

    summary = aggregate(records)
    html_doc = render_dashboard(summary, sources)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_doc, encoding="utf-8")
    print(f"[savings-dashboard] wrote {out}")
    print(f"  {len(records):,} records · {summary.total.calls:,} calls "
          f"· {len(summary.sessions_seen):,} sessions")
    print(f"  saved: {_fmt_tokens(summary.total.cache_read)} tokens "
          f"· {_fmt_usd(summary.total.saved_usd)}")
    print(f"  open with:  open {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
