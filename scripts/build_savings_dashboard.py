#!/usr/bin/env python3
"""TELOS savings dashboard —— 把 usage_log 聚合成「节约了多少 token / 多少美刀」。

输入：一个或多个 ``usage_log`` jsonl 文件（proxy 或 SDK transport 都行；
schema 见 docs/User-guide.md §7.1）。

用法::

    telos dashboard --usage-log ~/.telos/usage.jsonl
    # 或多个：
    telos dashboard --usage-log a.jsonl --usage-log b.jsonl --out savings.html

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
from typing import Any, Iterable, Mapping

from telos.registry import harness_display_name


# ---------------------------------------------------------------------------
# 价格表（USD / 1M tokens，2026 年 Anthropic / OpenAI / DeepSeek 公开价）
# ---------------------------------------------------------------------------
#
# Anthropic prompt-caching 计费规则（2026 年公开价，见
# https://platform.claude.com/docs/en/about-claude/pricing 与
# https://platform.claude.com/docs/en/build-with-claude/prompt-caching）：
#
#   cache_read 价      = 0.10 × input 价     （命中缓存 90% off）
#   cache_write (5m)   = 1.25 × input 价     （短 TTL 写入溢价 25%）
#   cache_write (1h)   = 2.00 × input 价     （长 TTL 写入溢价 100%）
#
# 因此每个 model 表里同时记 ``cache_write_5m`` 和 ``cache_write_1h``。
# 旧字段 ``cache_write`` 保留为 5m 价的别名，仅当 raw_usage 没有
# ``cache_creation.ephemeral_*_input_tokens`` 拆分时使用。
#
# 2026 年 Claude 公开价：
#   Opus 4.7 / 4.6  : $5  / $25   per 1M tok (input / output)
#   Sonnet 4.6 / 4.5: $3  / $15
#   Haiku 4.5       : $1  / $5
#
# **修复**：早期版本沿用了 Opus 4 系列 $15 / $75 的旧报价，2026 年定价
# 已下调到 $5 / $25。Haiku 同样从 $0.80 / $4 调整为 $1 / $5。
# 历史 sonnet-4 / opus-4 报价保留旧值（按当时定价 USD / 1M），仅为
# 重放历史日志时不重新换算；新调用走 4.6 / 4.7 走新价。

_PRICING: dict[str, dict[str, float]] = {
    # Anthropic — 2026 新报价
    "claude-opus-4-7":   {"input":  5.00, "cache_read": 0.50, "cache_write_5m":  6.25, "cache_write_1h": 10.00, "output": 25.00},
    "claude-opus-4-6":   {"input":  5.00, "cache_read": 0.50, "cache_write_5m":  6.25, "cache_write_1h": 10.00, "output": 25.00},
    # Opus 4 / 4.5：保留 2024-2025 历史价，便于重放旧日志
    "claude-opus-4-5":   {"input": 15.00, "cache_read": 1.50, "cache_write_5m": 18.75, "cache_write_1h": 30.00, "output": 75.00},
    "claude-opus-4":     {"input": 15.00, "cache_read": 1.50, "cache_write_5m": 18.75, "cache_write_1h": 30.00, "output": 75.00},
    "claude-sonnet-4-6": {"input":  3.00, "cache_read": 0.30, "cache_write_5m":  3.75, "cache_write_1h":  6.00, "output": 15.00},
    "claude-sonnet-4-5": {"input":  3.00, "cache_read": 0.30, "cache_write_5m":  3.75, "cache_write_1h":  6.00, "output": 15.00},
    "claude-sonnet-4":   {"input":  3.00, "cache_read": 0.30, "cache_write_5m":  3.75, "cache_write_1h":  6.00, "output": 15.00},
    "claude-haiku-4-5":  {"input":  1.00, "cache_read": 0.10, "cache_write_5m":  1.25, "cache_write_1h":  2.00, "output":  5.00},
    "claude-haiku-4":    {"input":  1.00, "cache_read": 0.10, "cache_write_5m":  1.25, "cache_write_1h":  2.00, "output":  5.00},
    # OpenAI：prompt cache 是 0.25× input，无 cache_write（写入 0 价）
    "gpt-5":             {"input":  5.00, "cache_read": 1.25, "cache_write_5m":  0.00, "cache_write_1h":  0.00, "output": 15.00},
    "gpt-5.1":           {"input":  5.00, "cache_read": 1.25, "cache_write_5m":  0.00, "cache_write_1h":  0.00, "output": 15.00},
    # DeepSeek：cache hit 与 miss 分别计费；不区分 5m/1h
    "deepseek-chat":     {"input":  0.27, "cache_read": 0.07, "cache_write_5m":  0.00, "cache_write_1h":  0.00, "output":  1.10},
    "deepseek-v3":       {"input":  0.27, "cache_read": 0.07, "cache_write_5m":  0.00, "cache_write_1h":  0.00, "output":  1.10},
    # 兜底：按 Sonnet 价当作"中等价位"估
    "_default":          {"input":  3.00, "cache_read": 0.30, "cache_write_5m":  3.75, "cache_write_1h":  6.00, "output": 15.00},
}


def _price_for(model: str) -> dict[str, float]:
    """模糊匹配 ``model`` 字段到价格表。仅匹配前缀，长 prefix 优先。

    返回的 dict 里 ``cache_write`` 是 5m TTL 价的别名（旧调用 site 不传
    breakdown 时退化用），同时保留 ``cache_write_5m`` / ``cache_write_1h``
    分别字段。
    """
    if not model:
        base = _PRICING["_default"]
    else:
        candidates = sorted(
            (k for k in _PRICING if k != "_default" and model.startswith(k)),
            key=len, reverse=True,
        )
        base = _PRICING[candidates[0]] if candidates else _PRICING["_default"]
    # 暴露 cache_write 别名（== 5m 价）方便老调用；不污染原 dict
    out = dict(base)
    out.setdefault("cache_write", base["cache_write_5m"])
    return out


def _split_cache_write(n: dict[str, int]) -> tuple[int, int]:
    """从 normalized usage 里拆出 (cache_write_5m, cache_write_1h)。

    优先用 raw_usage.cache_creation.ephemeral_{5m,1h}_input_tokens 拆分
    （Anthropic 在 SSE / JSON 里都会返回）；缺失时按 5m 计（与历史等价）。
    调用方应把 raw_usage 也放进 dict 里（key ``_breakdown``）。
    """
    bd = n.get("_breakdown") if isinstance(n, dict) else None
    if isinstance(bd, Mapping):
        w5 = int(bd.get("ephemeral_5m_input_tokens", 0) or 0)
        w1 = int(bd.get("ephemeral_1h_input_tokens", 0) or 0)
        if w5 + w1 > 0:
            return w5, w1
    return int(n.get("cache_write", 0) or 0), 0


def _cost_usd(model: str, n: dict[str, int]) -> dict[str, float]:
    """单条 call 在该 model 价表下的成本拆解（USD）。

    ``cache_write`` 总额 = 5m 部分 × cache_write_5m 价 + 1h 部分 ×
    cache_write_1h 价；缺 breakdown 时按 5m 计（保守低估 1h 部分）。
    """
    p = _price_for(model)
    w5, w1 = _split_cache_write(n)
    return {
        "raw_input":   p["input"]          * n["raw_input"]   / 1_000_000,
        "cache_read":  p["cache_read"]     * n["cache_read"]  / 1_000_000,
        "cache_write": (p["cache_write_5m"] * w5 + p["cache_write_1h"] * w1) / 1_000_000,
        "output":      p["output"]         * n["output"]      / 1_000_000,
    }


def _counterfactual_cost_usd(model: str, n: dict[str, int]) -> float:
    """"如果完全不开 TELOS / cache_control" 的对照价。

    在这个反事实里，所有 prompt tokens（raw_input + cache_read +
    cache_write）都会按基础 input 价计费——没有 cache_read 折扣，也没有
    cache_write 溢价。output 价不变。
    """
    p = _price_for(model)
    w5, w1 = _split_cache_write(n)
    cache_write_total = w5 + w1
    prompt_tokens = int(n.get("raw_input", 0)) + int(n.get("cache_read", 0)) + cache_write_total
    return (p["input"] * prompt_tokens + p["output"] * int(n.get("output", 0))) / 1_000_000


def _saved_usd_for_call(model: str, n: dict[str, int]) -> float:
    """这一 call 因为接入 TELOS / cache_control 实际省下（或多花）的钱。

    与"完全不开 cache_control"对照：
      saved = counterfactual_cost − actual_cost
            = cache_read × (input − cache_read_price)              # 命中省钱
              + cache_write_5m × (input − cache_write_5m_price)    # 短写溢价 -25%
              + cache_write_1h × (input − cache_write_1h_price)    # 长写溢价 -100%

    对 Anthropic 来说 cache_write 项是 *负* 贡献（写入比基础价贵），但
    只要 cache_read 量足够大，总和仍然正。这是相对早期实现的重要修正：
    旧版本仅算 cache_read 折扣，会高估"省下的钱"。
    """
    p = _price_for(model)
    w5, w1 = _split_cache_write(n)
    saved_read  = (p["input"] - p["cache_read"])      * int(n.get("cache_read", 0))
    saved_w5    = (p["input"] - p["cache_write_5m"])  * w5
    saved_w1    = (p["input"] - p["cache_write_1h"])  * w1
    return (saved_read + saved_w5 + saved_w1) / 1_000_000


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------

# tool_result token 估算的字符/token 比。**仅作旧日志的兜底**——新日志
# 里 tool_output_reduction 已带过滤时按真实文本算好的 original/filtered_tokens。
_CHARS_PER_TOKEN = 4


@dataclass
class _Agg:
    """累计 4 个 token bucket + 美元 + 计次。

    新增字段：
    - ``cache_write_5m`` / ``cache_write_1h``：拆分后的 cache_write 量
    - ``counterfactual_usd``：反事实成本（关闭 TELOS 时要付多少）
    - ``tool_orig_chars`` / ``tool_filtered_chars``：RTK 过滤前/后的工具
      输出字符数；``tool_orig_tokens`` / ``tool_filtered_tokens`` 是过滤时
      按真实文本算好的 token 估算。``tool_saved_usd`` 是据此（缓存命中率
      加权计价）估算的省钱。
    """
    raw_input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    output: int = 0
    cost_usd: float = 0.0
    saved_usd: float = 0.0
    counterfactual_usd: float = 0.0
    calls: int = 0
    last_ts: float = 0.0
    # RTK 工具输出过滤
    tool_orig_chars: int = 0
    tool_filtered_chars: int = 0
    tool_orig_tokens: int = 0
    tool_filtered_tokens: int = 0
    tool_blocks_filtered: int = 0
    tool_blocks_seen: int = 0
    tool_rtk_calls: int = 0   # RTK 实际执行过过滤的 call 数（非空 reduction）
    tool_saved_usd: float = 0.0

    @property
    def tool_saved_chars(self) -> int:
        return max(0, self.tool_orig_chars - self.tool_filtered_chars)

    @property
    def rtk_status(self) -> str:
        """RTK 在这组数据里的状态——把「没省钱」拆成三种本质不同的情况：

        - ``disabled``：RTK 从未对任何 call 执行（proxy mode 不含 rtk）。
        - ``idle``：RTK 开了，但没扫到任何 tool_result（对话还没产生工具输出）。
        - ``nosave``：RTK 跑了、也扫到工具输出，但没省下 token。
        - ``active``：RTK 实际省下了 token。
        """
        if self.tool_rtk_calls == 0:
            return "disabled"
        if self.tool_blocks_seen == 0:
            return "idle"
        if self.tool_saved_tokens == 0:
            return "nosave"
        return "active"

    @property
    def tool_saved_tokens(self) -> int:
        """RTK 过滤省下的 token。

        优先用日志里过滤时按真实文本算好的 token 估算（``original_tokens``
        / ``filtered_tokens``，见 output_filter/tokens.py）；旧日志缺这两个
        字段时回退到 ``saved_chars / _CHARS_PER_TOKEN`` 的粗估。
        """
        logged = max(0, self.tool_orig_tokens - self.tool_filtered_tokens)
        if logged > 0:
            return logged
        return self.tool_saved_chars // _CHARS_PER_TOKEN

    def add(self, n: dict[str, int], cost: dict[str, float], saved: float,
            counterfactual: float, ts: float,
            tool_reduction: Mapping[str, Any] | None = None,
            tool_saved_usd: float = 0.0) -> None:
        self.raw_input += n["raw_input"]
        self.cache_read += n["cache_read"]
        self.cache_write += n["cache_write"]
        # n["_w5"] / n["_w1"] 是 aggregate() 拆好的 breakdown
        self.cache_write_5m += int(n.get("_w5", 0))
        self.cache_write_1h += int(n.get("_w1", 0))
        self.output += n["output"]
        self.cost_usd += sum(cost.values())
        self.saved_usd += saved
        self.counterfactual_usd += counterfactual
        self.calls += 1
        if tool_reduction:
            self.tool_orig_chars += int(tool_reduction.get("original_chars", 0) or 0)
            self.tool_filtered_chars += int(tool_reduction.get("filtered_chars", 0) or 0)
            self.tool_orig_tokens += int(tool_reduction.get("original_tokens", 0) or 0)
            self.tool_filtered_tokens += int(tool_reduction.get("filtered_tokens", 0) or 0)
            self.tool_blocks_filtered += int(tool_reduction.get("blocks_filtered", 0) or 0)
            self.tool_blocks_seen += int(tool_reduction.get("blocks_seen", 0) or 0)
            self.tool_rtk_calls += 1
            self.tool_saved_usd += tool_saved_usd
        if ts > self.last_ts:
            self.last_ts = ts

    @property
    def combined_saved_usd(self) -> float:
        """TELOS 前缀缓存省钱 + RTK 工具输出过滤省钱。"""
        return self.saved_usd + self.tool_saved_usd


@dataclass
class Summary:
    total: _Agg = field(default_factory=_Agg)
    by_harness: dict[str, _Agg] = field(default_factory=lambda: defaultdict(_Agg))
    by_model: dict[str, _Agg] = field(default_factory=lambda: defaultdict(_Agg))
    by_session: dict[str, _Agg] = field(default_factory=lambda: defaultdict(_Agg))
    # 开关维度：mode ∈ {none, telos, rtk, both, passthrough, rtk-only}
    by_mode: dict[str, _Agg] = field(default_factory=lambda: defaultdict(_Agg))
    # 对比实验：compare_group → mode → _Agg。同一 group 下不同 mode 的
    # session 会在 dashboard 上并排展示。
    compare_groups: dict[str, dict[str, _Agg]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(_Agg))
    )
    # 哪些 compare_group 来自 `telos replay`（受控重放）而非真实双 session。
    replay_groups: set[str] = field(default_factory=set)
    # 时间序列：以 hour bucket 累计 cache_read / saved_usd / counterfactual / cost / calls
    timeline: dict[str, dict[str, float]] = field(
        default_factory=lambda: defaultdict(lambda: {
            "cache_read": 0.0,
            "saved_usd": 0.0,
            "counterfactual_usd": 0.0,
            "cost_usd": 0.0,
            "calls": 0.0,
        })
    )
    first_ts: float | None = None
    last_ts: float | None = None
    sessions_seen: set[str] = field(default_factory=set)


def _extract_breakdown(rec: Mapping[str, Any]) -> dict[str, int] | None:
    """从 record 里挖出 cache_creation 的 5m/1h 拆分。

    Proxy / SDK transport 都把原始 ``usage`` 字段塞进 ``raw_usage``；
    Anthropic 在那里返回 ``cache_creation.ephemeral_{5m,1h}_input_tokens``。
    缺失或非 dict 时返回 None（调用方按 5m 退化处理）。
    """
    raw = rec.get("raw_usage")
    if not isinstance(raw, Mapping):
        return None
    cc = raw.get("cache_creation")
    if not isinstance(cc, Mapping):
        return None
    return {
        "ephemeral_5m_input_tokens": int(cc.get("ephemeral_5m_input_tokens", 0) or 0),
        "ephemeral_1h_input_tokens": int(cc.get("ephemeral_1h_input_tokens", 0) or 0),
    }


def aggregate(records: Iterable[dict[str, Any]]) -> Summary:
    s = Summary()
    for rec in records:
        n = rec.get("normalized") or {}
        if not n:
            continue
        breakdown = _extract_breakdown(rec)
        n_dict: dict[str, Any] = {
            "raw_input": int(n.get("raw_input", 0) or 0),
            "cache_read": int(n.get("cache_read", 0) or 0),
            "cache_write": int(n.get("cache_write", 0) or 0),
            "output": int(n.get("output", 0) or 0),
        }
        if breakdown is not None:
            n_dict["_breakdown"] = breakdown
        # 把 5m/1h 拆分放进 dict，供 _Agg.add 累加（方便后面分类做条形图）
        w5, w1 = _split_cache_write(n_dict)
        n_dict["_w5"] = w5
        n_dict["_w1"] = w1
        model = rec.get("model") or ""
        # "Breakdown by harness" 按展示名归组：hermes → "Claude Code"。
        harness = harness_display_name(rec.get("harness") or "?")
        session = rec.get("session_id") or "(no-session)"
        mode = rec.get("mode") or "telos"
        compare_group = rec.get("compare_group")
        ts = float(rec.get("ts") or 0.0)

        cost = _cost_usd(model, n_dict)
        saved = _saved_usd_for_call(model, n_dict)
        counterfactual = _counterfactual_cost_usd(model, n_dict)

        # RTK 工具输出过滤省钱估算。
        #
        # token 数：优先用日志里过滤时按真实文本算好的估算
        # （original_tokens / filtered_tokens，见 output_filter/tokens.py）；
        # 旧日志缺这些字段时回退 chars/4。
        #
        # 计价：缓存命中率加权。这些被过滤掉的 token 若不过滤，会进入 prompt——
        # 其边际成本介于 cache_read 价（若命中前缀缓存）与 input 价（未命中）
        # 之间。用这条 call 自身的命中率 h 加权：
        #   eff_price = h × cache_read 价 + (1 − h) × input 价
        # TELOS 命中率越高，RTK 省下的钱越接近（更便宜的）cache_read 口径；
        # 不开 TELOS（h≈0）时退化为完整 input 价。
        tool_reduction = rec.get("tool_output_reduction")
        if not isinstance(tool_reduction, Mapping):
            tool_reduction = None
        tool_saved_usd = 0.0
        if tool_reduction:
            saved_tokens = int(tool_reduction.get("saved_tokens", 0) or 0)
            if saved_tokens <= 0:
                ot = int(tool_reduction.get("original_tokens", 0) or 0)
                ft = int(tool_reduction.get("filtered_tokens", 0) or 0)
                saved_tokens = max(0, ot - ft)
            if saved_tokens <= 0:
                saved_chars = max(0, int(tool_reduction.get("original_chars", 0) or 0)
                                  - int(tool_reduction.get("filtered_chars", 0) or 0))
                saved_tokens = saved_chars // _CHARS_PER_TOKEN
            p = _price_for(model)
            prompt_tokens = (n_dict["raw_input"] + n_dict["cache_read"]
                             + n_dict["cache_write"])
            hit = (n_dict["cache_read"] / prompt_tokens) if prompt_tokens else 0.0
            eff_price = hit * p["cache_read"] + (1.0 - hit) * p["input"]
            tool_saved_usd = eff_price * saved_tokens / 1_000_000

        def _add(agg: _Agg) -> None:
            agg.add(n_dict, cost, saved, counterfactual, ts,
                    tool_reduction=tool_reduction, tool_saved_usd=tool_saved_usd)

        _add(s.total)
        _add(s.by_harness[harness])
        _add(s.by_model[model or "(unknown)"])
        _add(s.by_session[session])
        _add(s.by_mode[mode])
        if compare_group:
            _add(s.compare_groups[compare_group][mode])
            if rec.get("replay"):
                s.replay_groups.add(compare_group)
        s.sessions_seen.add(session)

        if ts > 0:
            bucket = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:00")
            tb = s.timeline[bucket]
            tb["cache_read"] += n_dict["cache_read"]
            tb["saved_usd"] += saved
            tb["counterfactual_usd"] += counterfactual
            tb["cost_usd"] += sum(cost.values())
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
        # 修正：hit% 的分母应包含 cache_write（Anthropic 的 input_tokens
        # 仅指未命中且未写缓存的部分，"总 prompt tokens" = raw + read + write）
        prompt_tokens = a.cache_read + a.raw_input + a.cache_write
        share = (a.cache_read / prompt_tokens) if prompt_tokens else 0.0
        bar_pct = (100 * a.saved_usd / max_saved) if max_saved > 0 else 0.0
        rows.append(
            f"<tr>"
            f'<td class="left">{html.escape(str(key))}</td>'
            f"<td>{_fmt_int(a.calls)}</td>"
            f"<td>{_fmt_tokens(a.raw_input)}</td>"
            f'<td class="green">{_fmt_tokens(a.cache_read)}</td>'
            f"<td>{_fmt_tokens(a.cache_write)}</td>"
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
      <th>cache_write</th>
      <th>hit%</th>
      <th class="left">saved $</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</div>
"""


# mode label → 展示用颜色 / 说明
_MODE_META: dict[str, tuple[str, str]] = {
    "both":        ("#d2a8ff", "TELOS 前缀缓存 + RTK 工具过滤"),
    "telos":       ("#3fb950", "只 TELOS 前缀缓存"),
    "rtk":         ("#f0883e", "只 RTK 工具过滤"),
    "rtk-only":    ("#f0883e", "只 RTK 工具过滤"),
    "none":        ("#7d8590", "纯透传，无优化"),
    "passthrough": ("#7d8590", "纯透传 / 管线降级"),
}


def _mode_color(mode: str) -> str:
    return _MODE_META.get(mode, ("#79c0ff", ""))[0]


def _render_mode_table(by_mode: dict[str, _Agg]) -> str:
    """开关维度 breakdown：每种 mode 一行，展示 TELOS / RTK 两路省钱。"""
    if not by_mode:
        return ""
    order = {"both": 0, "telos": 1, "rtk": 2, "rtk-only": 2, "none": 3,
             "passthrough": 4}
    rows_sorted = sorted(by_mode.items(), key=lambda kv: order.get(kv[0], 9))
    max_combined = max((a.combined_saved_usd for _, a in rows_sorted), default=0.0)

    rows = []
    for mode, a in rows_sorted:
        color = _mode_color(mode)
        desc = _MODE_META.get(mode, ("", ""))[1]
        bar_pct = (100 * a.combined_saved_usd / max_combined) if max_combined > 0 else 0.0
        # RTK 没在这个 mode 下跑过 → 两个 RTK 列显式标 not enabled，
        # 而不是和「跑了但没省」一样都显示 0。
        if a.rtk_status == "disabled":
            rtk_cells = ('<td class="muted" colspan="2" '
                         'style="text-align:center">RTK not enabled</td>')
        else:
            rtk_cells = (f'<td class="gold">{_fmt_tokens(a.tool_saved_tokens)}</td>'
                         f'<td class="gold">{_fmt_usd(a.tool_saved_usd)}</td>')
        rows.append(
            f"<tr>"
            f'<td class="left"><b style="color:{color}">{html.escape(mode)}</b>'
            f'<br><span class="muted" style="font-size:10px">{html.escape(desc)}</span></td>'
            f"<td>{_fmt_int(a.calls)}</td>"
            f'<td class="green">{_fmt_usd(a.saved_usd)}</td>'
            f"{rtk_cells}"
            f'<td class="bar-cell">'
            f'<span class="fill" style="width:{bar_pct:.1f}%"></span>'
            f'<span class="label-overlay">{_fmt_usd(a.combined_saved_usd)}</span>'
            f"</td>"
            f"</tr>"
        )
    return f"""
<div class="card">
  <h2>Breakdown by mode（开关对照）</h2>
  <table>
    <thead><tr>
      <th class="left">mode</th>
      <th>calls</th>
      <th>TELOS saved $</th>
      <th>RTK tokens removed</th>
      <th>RTK saved $ (est)</th>
      <th class="left">combined saved $</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <p class="muted" style="font-size:11px;margin-top:8px">
    TELOS saved $ = 前缀缓存相对「不开 cache_control」省下的钱；
    RTK saved $ = 工具输出过滤省下的 token（过滤时按真实文本估算）×
    缓存命中率加权的边际价（命中→cache_read 价、未命中→input 价）。
    两路相加 = combined。
  </p>
</div>
"""


def _render_compare_section(compare_groups: dict[str, dict[str, _Agg]],
                             replay_groups: set[str] | None = None) -> str:
    """对比实验：同一 compare_group 下、不同 mode 的 session 并排展示。

    每个 group 一张卡，卡里每个 mode 一个 cell；自动高亮 combined saved $
    最高的 mode。用于「同任务、相同用户输入、不同开关」的 A/B 对照。

    ``replay_groups`` 里的 group 来自 `telos replay`（受控重放），卡标题
    标 ``replay`` 徽章；其余来自真实双 session，标 ``live A/B``。
    """
    if not compare_groups:
        return ""
    replay_groups = replay_groups or set()
    cards = []
    for group, by_mode in sorted(compare_groups.items()):
        if not by_mode:
            continue
        is_replay = group in replay_groups
        _badge_base = ("display:inline-block;margin-left:8px;padding:2px 9px;"
                       "border-radius:999px;font-size:10px;vertical-align:middle;"
                       "font-family:sans-serif;letter-spacing:0")
        src_badge = (
            f'<span style="{_badge_base};background:#2d2150;color:#d2a8ff">replay</span>'
            if is_replay else
            f'<span style="{_badge_base};background:#1f3a4d;color:#79c0ff">live A/B</span>'
        )
        best_mode = max(by_mode.items(),
                        key=lambda kv: kv[1].combined_saved_usd)[0]
        # 最贵基线：成本最高的 mode，用来算各 mode 省了多少
        max_cost = max((a.cost_usd for a in by_mode.values()), default=0.0)
        cells = []
        order = {"both": 0, "telos": 1, "rtk": 2, "rtk-only": 2, "none": 3,
                 "passthrough": 4}
        for mode, a in sorted(by_mode.items(), key=lambda kv: order.get(kv[0], 9)):
            color = _mode_color(mode)
            is_best = mode == best_mode
            delta = max_cost - a.cost_usd
            delta_html = (
                f'<div class="row"><span>vs 最贵 mode</span>'
                f'<b class="green">−{_fmt_usd(delta)}</b></div>'
                if delta > 0 else
                '<div class="row"><span>vs 最贵 mode</span><b class="muted">基线</b></div>'
            )
            prompt_tokens = a.raw_input + a.cache_read + a.cache_write
            hit = (a.cache_read / prompt_tokens) if prompt_tokens else 0.0
            badge = ('<span class="pill" style="background:#1a4d2e;color:#56d364">'
                     'best</span>') if is_best else ""
            rtk_removed = ('<b class="muted">not enabled</b>'
                           if a.rtk_status == "disabled"
                           else f'<b class="gold">{_fmt_tokens(a.tool_saved_tokens)}</b>')
            cells.append(f"""
    <div class="compare-cell" style="border-color:{color}55">
      <h3><b style="color:{color}">{html.escape(mode)}</b> {badge}</h3>
      <div class="big" style="color:{color}">{_fmt_usd(a.cost_usd)}</div>
      <div class="row"><span>calls</span><b>{_fmt_int(a.calls)}</b></div>
      <div class="row"><span>cache hit%</span><b>{_fmt_pct(hit)}</b></div>
      <div class="row"><span>TELOS saved $</span><b class="green">{_fmt_usd(a.saved_usd)}</b></div>
      <div class="row"><span>RTK tokens removed</span>{rtk_removed}</div>
      <div class="row"><span>combined saved $</span><b class="lilac">{_fmt_usd(a.combined_saved_usd)}</b></div>
      {delta_html}
    </div>""")
        n_cols = min(len(cells), 4)
        cards.append(f"""
<div class="card">
  <h2>Compare group · {html.escape(group)} {src_badge}</h2>
  <div class="compare-grid" style="grid-template-columns:repeat({n_cols},1fr)">
    {''.join(cells)}
  </div>
</div>""")
    if not cards:
        return ""
    return ("""
<div class="card" style="background:transparent;border:none;padding:0;margin-bottom:6px">
  <h2 style="color:#d2a8ff">A/B 对比 · 同任务不同开关</h2>
</div>""" + "".join(cards))


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
    # 修正：hit_rate 分母应包含 cache_write（Anthropic 的 input_tokens 字段
    # 不含 cached_creation / cached_read，所以"总 prompt tokens" = 三者之和）
    prompt_tokens_total = total.raw_input + total.cache_read + total.cache_write
    hit_rate = total.cache_read / prompt_tokens_total if prompt_tokens_total else 0.0
    # 反事实成本：直接累加每条 call 的（关掉 cache_control 后）的预估总价。
    # 旧实现用 ``cost + saved``，在 cache_write 溢价场景下会少算；改成累计
    # 来自 _counterfactual_cost_usd 的逐条值。
    counterfactual_cost = total.counterfactual_usd or (total.cost_usd + total.saved_usd)
    saved_share = total.saved_usd / counterfactual_cost if counterfactual_cost else 0.0
    # 合并口径：TELOS + RTK。RTK 的反事实成本 = 实付 + RTK 省下的钱，所以
    # 合并反事实 = TELOS 反事实 + RTK 省下额。
    combined_saved = total.combined_saved_usd
    combined_counterfactual = counterfactual_cost + total.tool_saved_usd
    combined_share = (combined_saved / combined_counterfactual
                      if combined_counterfactual else 0.0)

    # RTK 状态：把「$0」拆开——RTK 没启用 vs 启用了但没省，是两回事。
    rtk_status = total.rtk_status
    if rtk_status == "disabled":
        rtk_hero = '<b class="muted">RTK 未启用</b>'
        rtk_kpi_value = '<span class="muted" style="font-size:16px">not enabled</span>'
        rtk_kpi_sub = "proxy mode 未含 rtk —— 用 --mode both 启用"
    elif rtk_status == "idle":
        rtk_hero = f'RTK <b class="gold">{_fmt_usd(total.tool_saved_usd)}</b>'
        rtk_kpi_value = f'<span class="gold">{_fmt_usd(total.tool_saved_usd)}</span>'
        rtk_kpi_sub = "已启用 · 暂无工具输出可过滤"
    else:  # nosave / active
        rtk_hero = f'RTK <b class="gold">{_fmt_usd(total.tool_saved_usd)}</b>'
        rtk_kpi_value = f'<span class="gold">{_fmt_usd(total.tool_saved_usd)}</span>'
        rtk_kpi_sub = ("工具输出过滤 · 命中率加权计价" if rtk_status == "active"
                       else "已跑 · 本批无可省内容")

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

    # —— "with TELOS" 视角的 token mix（实际值） ——
    seg_bar_with = _render_seg_bar([
        ("raw_input",   total.raw_input,   "raw_input"),
        ("cache_read",  total.cache_read,  "cache_read"),
        ("cache_write", total.cache_write, "cache_write"),
        ("output",      total.output,      "output"),
    ])
    # —— "without TELOS" 视角：所有 prompt token 都落 raw_input 桶 ——
    seg_bar_without = _render_seg_bar([
        ("raw_input",   prompt_tokens_total, "raw_input (counterfactual)"),
        ("output",      total.output,        "output"),
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
    by_mode = _render_mode_table(summary.by_mode)
    compare_section = _render_compare_section(summary.compare_groups,
                                              summary.replay_groups)

    timeline_svg = _render_timeline_svg(summary.timeline)

    # 5m / 1h cache_write 拆分（仅在 raw_usage.cache_creation.* 拆分可用时
    # 非零；否则全部计入 5m 兜底）
    w5_total = total.cache_write_5m
    w1_total = total.cache_write_1h
    write_breakdown_note = (
        f"cache_write 拆分：5m <b class='gold'>{_fmt_tokens(w5_total)}</b>"
        f" · 1h <b class='gold'>{_fmt_tokens(w1_total)}</b>"
    ) if (w5_total + w1_total) else (
        "cache_write 缺 5m/1h 拆分 → 按 5m 价兜底估算"
    )

    refresh_tag = (
        f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'
        if refresh_seconds and refresh_seconds > 0 else ""
    )
    refresh_note = (
        f' · auto-refresh {int(refresh_seconds)}s'
        if refresh_seconds and refresh_seconds > 0 else ""
    )

    # 对照模式下需要的数字（关 TELOS 时，所有 token 走 input 价）
    without_input_value = _fmt_tokens(prompt_tokens_total)
    without_cost = _fmt_usd(counterfactual_cost)
    with_cost = _fmt_usd(total.cost_usd)
    delta_cost = _fmt_usd(total.saved_usd)
    delta_share = _fmt_pct(saved_share)

    toggle_css = """
.toggle-wrap { display: inline-flex; gap: 0; background: #161b22; border-radius: 999px;
  padding: 4px; margin: 0 0 14px 0; border: 1px solid #30363d; }
.toggle-wrap button { all: unset; cursor: pointer; padding: 7px 18px; border-radius: 999px;
  font-size: 12px; font-weight: 600; color: #8b949e; transition: all .15s ease; }
.toggle-wrap button.active { background: linear-gradient(120deg, #d2a8ff 0%, #79c0ff 100%);
  color: #0a0d12; }
.toggle-wrap button:hover:not(.active) { color: #e6edf3; }
[data-mode='without'] .with-only { display: none; }
[data-mode='with'] .without-only { display: none; }
[data-mode='_compare'] .with-only,
[data-mode='_compare'] .without-only { display: none; }
.compare-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-bottom: 18px; }
.compare-cell { background: #0f141c; border: 1px solid #21262d; border-radius: 10px;
  padding: 18px 20px; }
.compare-cell h3 { margin: 0 0 10px 0; font-size: 12px; text-transform: uppercase;
  letter-spacing: 0.06em; color: #7d8590; font-weight: 600; }
.compare-cell h3 .pill { display: inline-block; margin-left: 8px; padding: 2px 8px;
  border-radius: 999px; font-size: 10px; vertical-align: middle; }
.compare-cell.actual h3 .pill   { background: #1a4d2e; color: #56d364; }
.compare-cell.counter h3 .pill  { background: #4d2a1a; color: #f0883e; }
.compare-cell .row { font-size: 13px; color: #c9d1d9; margin: 4px 0; display: flex;
  justify-content: space-between; }
.compare-cell .row b { font-variant-numeric: tabular-nums; }
.compare-cell .big { font-size: 24px; font-weight: 700; margin: 4px 0 8px 0;
  letter-spacing: -0.01em; font-variant-numeric: tabular-nums; }
.compare-cell.actual .big   { color: #56d364; }
.compare-cell.counter .big  { color: #f0883e; }
.compare-cell .small { font-size: 11px; color: #7d8590; margin-top: 6px; }
.savings-arrow { font-size: 13px; color: #d2a8ff; margin: 10px 0 0 0; text-align: center; }
.savings-arrow b { font-size: 18px; }
"""

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
{refresh_tag}
<title>TELOS · Token Savings Dashboard</title>
<style>{CSS}</style>
<style>{toggle_css}</style>
</head><body data-mode="with">
<div class="wrap">

<header>
  <h1>TELOS · Token Savings</h1>
  <div class="sub">
    {n_calls:,} calls · {n_sessions:,} sessions · span {span_s}
    · generated {ts_now}{refresh_note}
  </div>
</header>

<!-- ===== 视角切换：with TELOS vs. without TELOS ===== -->
<div class="toggle-wrap" role="tablist">
  <button id="btn-with"    class="active" onclick="setMode('with')">实际（开 TELOS）</button>
  <button id="btn-without" onclick="setMode('without')">反事实（不开 TELOS）</button>
  <button id="btn-compare" onclick="setMode('compare')">并排对比</button>
</div>

<!-- compare 视角下的并排面板 -->
<section id="compare-view" style="display:none; margin-bottom: 18px;">
  <div class="compare-grid">
    <div class="compare-cell counter">
      <h3>without TELOS <span class="pill">counterfactual</span></h3>
      <div class="big">{without_cost}</div>
      <div class="row"><span>prompt tokens (input 价)</span><b>{without_input_value}</b></div>
      <div class="row"><span>output tokens</span><b>{_fmt_tokens(total.output)}</b></div>
      <div class="small">所有 prompt token 按基础 input 价计费；无 cache_read 折扣、无 cache_write 溢价。</div>
    </div>
    <div class="compare-cell actual">
      <h3>with TELOS <span class="pill">actual</span></h3>
      <div class="big">{with_cost}</div>
      <div class="row"><span>raw_input · @input</span><b>{_fmt_tokens(total.raw_input)}</b></div>
      <div class="row"><span>cache_read · @0.1×input</span><b class="green">{_fmt_tokens(total.cache_read)}</b></div>
      <div class="row"><span>cache_write · @1.25–2×input</span><b class="gold">{_fmt_tokens(total.cache_write)}</b></div>
      <div class="row"><span>output · @output</span><b>{_fmt_tokens(total.output)}</b></div>
      <div class="small">{write_breakdown_note}</div>
    </div>
  </div>
  <div class="savings-arrow">
    净节省：<b>{delta_cost}</b> &nbsp;·&nbsp; <b>{delta_share}</b> off counterfactual
  </div>
</section>

<section class="hero with-only">
  <div class="hero-card green">
    <div class="label">tokens saved (cache hits)</div>
    <div class="value">{_fmt_tokens(total.cache_read)}</div>
    <div class="sub">
      cache hits 占总 prompt tokens 的 <b class="green">{_fmt_pct(hit_rate)}</b>
      ·  绝对量 <code>{_fmt_int(total.cache_read)}</code> tokens
    </div>
  </div>
  <div class="hero-card purple">
    <div class="label">total cost saved (estimated)</div>
    <div class="value">{_fmt_usd(combined_saved)}</div>
    <div class="sub">
      TELOS <b class="green">{_fmt_usd(total.saved_usd)}</b>
      &nbsp;·&nbsp; {rtk_hero}
      &nbsp;·&nbsp; 占反事实总成本 <b class="lilac">{_fmt_pct(combined_share)}</b>
    </div>
  </div>
</section>

<section class="hero without-only">
  <div class="hero-card green" style="opacity:.65">
    <div class="label">prompt tokens (no cache)</div>
    <div class="value">{without_input_value}</div>
    <div class="sub">
      反事实视角：所有 raw_input + cache_read + cache_write 都以
      base input 价计费 ·
      output <code>{_fmt_int(total.output)}</code>
    </div>
  </div>
  <div class="hero-card purple" style="opacity:.85">
    <div class="label">cost (no TELOS)</div>
    <div class="value">{without_cost}</div>
    <div class="sub">
      启用 TELOS 后实际只付 <b>{with_cost}</b>
      · 节省 <b class="lilac">{delta_cost}</b>（<b class="lilac">{delta_share}</b>）
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
    <div class="sub">5m {_fmt_tokens(w5_total)} · 1h {_fmt_tokens(w1_total)}</div></div>
  <div class="kpi"><div class="label">output</div>
    <div class="value blue">{_fmt_tokens(total.output)}</div>
    <div class="sub">{_fmt_int(total.output)}</div></div>
  <div class="kpi"><div class="label">RTK tool output removed</div>
    <div class="value gold">{_fmt_tokens(total.tool_saved_tokens)}</div>
    <div class="sub">{_fmt_int(total.tool_blocks_filtered)} blocks · ~{_fmt_usd(total.tool_saved_usd)}</div></div>
  <div class="kpi"><div class="label">TELOS saved $</div>
    <div class="value green">{_fmt_usd(total.saved_usd)}</div>
    <div class="sub">前缀缓存 vs 不开 cache_control</div></div>
  <div class="kpi"><div class="label">RTK saved $</div>
    <div class="value gold">{rtk_kpi_value}</div>
    <div class="sub">{rtk_kpi_sub}</div></div>
</div>

<div class="card with-only">
  <h2>Token mix（with TELOS · 实际）</h2>
  {seg_bar_with}
  <div class="seg-legend">
    <span><span class="sw" style="background:#f0883e"></span>raw_input · {_fmt_tokens(total.raw_input)}</span>
    <span><span class="sw" style="background:#3fb950"></span>cache_read · {_fmt_tokens(total.cache_read)}</span>
    <span><span class="sw" style="background:#d29922"></span>cache_write · {_fmt_tokens(total.cache_write)}</span>
    <span><span class="sw" style="background:#79c0ff"></span>output · {_fmt_tokens(total.output)}</span>
  </div>
</div>

<div class="card without-only">
  <h2>Token mix（without TELOS · 反事实）</h2>
  {seg_bar_without}
  <div class="seg-legend">
    <span><span class="sw" style="background:#f0883e"></span>raw_input · {without_input_value}</span>
    <span><span class="sw" style="background:#79c0ff"></span>output · {_fmt_tokens(total.output)}</span>
  </div>
  <p class="muted" style="font-size:11px;margin-top:8px">
    反事实假设：保持 prompt 内容不变，但移除 ``cache_control``，全量 prompt token
    按 base input 价计费 → 计费视角下所有 cache_read 与 cache_write 都"塌"成 raw_input。
  </p>
</div>

<div class="card timeline">
  <h2>Activity over time（按小时聚合）</h2>
  {timeline_svg}
</div>

{compare_section}
{by_mode}
{by_harness}
{by_model}
{by_session}

<div class="footer">
  数据源 · {sources_html}<br>
  价格表 = Anthropic / OpenAI / DeepSeek 2026 公开报价（USD / 1M tokens）；未识别 model 走 Sonnet 价位估算。
  <br>cache_write 拆分按 raw_usage.cache_creation.ephemeral_{{5m,1h}}_input_tokens；缺失则全计 5m 价（保守）。
</div>

</div>

<script>
function setMode(m) {{
  var body = document.body;
  var compareView = document.getElementById('compare-view');
  ['with','without','compare'].forEach(function(k){{
    var b = document.getElementById('btn-' + k);
    if (b) b.classList.toggle('active', k === m);
  }});
  if (m === 'compare') {{
    // compare 模式：隐藏 with/without 专属面板，只显示并排
    body.setAttribute('data-mode', '_compare');
    compareView.style.display = 'block';
  }} else {{
    body.setAttribute('data-mode', m);
    compareView.style.display = 'none';
  }}
  try {{ localStorage.setItem('telos.dashboard.mode', m); }} catch(e) {{}}
}}
// 恢复上次选择
try {{
  var saved = localStorage.getItem('telos.dashboard.mode');
  if (saved && ['with','without','compare'].indexOf(saved) >= 0) setMode(saved);
}} catch(e) {{}}
</script>
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
<title>TELOS · Token Savings</title>
<style>{CSS}</style>
</head><body><div class="wrap">
<header><h1>TELOS · Token Savings</h1></header>
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
        prog="telos.scripts.build_savings_dashboard",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--usage-log", action="append", required=True,
                    help="usage_log jsonl 路径或 glob，可重复")
    ap.add_argument("--out", default="telos_savings.html",
                    help="输出 HTML 路径（默认 ./telos_savings.html）")
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
    print(f"  saved: {_fmt_tokens(summary.total.cache_read)} cache-read tokens "
          f"· TELOS {_fmt_usd(summary.total.saved_usd)}"
          f" + RTK {_fmt_usd(summary.total.tool_saved_usd)}"
          f" = {_fmt_usd(summary.total.combined_saved_usd)}")
    print(f"  open with:  open {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
