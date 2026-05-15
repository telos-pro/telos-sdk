"""``python -m stela.replay`` / ``stela replay`` 入口。

把语料库里某个真实会话，按多种 mode 各重放一遍，结果 append 到 usage_log，
dashboard 的「A/B 对比」面板会自动并排展示（compare_group = 原会话 id）。

用法::

    stela replay --list                       # 列出语料库里的会话
    stela replay --session stela-ab12cd34      # 默认 4 mode 全跑
    stela replay --session <id> --modes none,both
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from stela.corpus import DEFAULT_CORPUS_DIR, list_sessions, load_session
from stela.output_filter import MODE_LABELS, StelaMode, build_filter
from stela.replay import ReplayResult, anthropic_sender, replay_session

_DEFAULT_USAGE_LOG = Path.home() / ".stela" / "usage.jsonl"


def _print_sessions(corpus_dir: Path) -> int:
    infos = list_sessions(corpus_dir)
    if not infos:
        print(f"语料库为空：{corpus_dir}")
        print("（先用 `stela proxy` 跑几个真实会话，默认就会录进去）")
        return 0
    print(f"语料库 {corpus_dir} —— {len(infos)} 个会话：\n")
    print(f"  {'session_id':<40} {'calls':>6}  last_seen")
    for i in infos:
        last = datetime.fromtimestamp(i.last_ts).strftime("%Y-%m-%d %H:%M") \
            if i.last_ts else "—"
        print(f"  {i.session_id:<40} {i.n_calls:>6}  {last}")
    print("\n重放：stela replay --session <session_id>")
    return 0


def _append_records(usage_log: Path, results: list[ReplayResult]) -> int:
    usage_log.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with usage_log.open("a", encoding="utf-8") as f:
        for r in results:
            for rec in r.records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
    return n


def _print_summary(results: list[ReplayResult]) -> None:
    print(f"\n{'mode':<10} {'turns':>7} {'raw_input':>12} "
          f"{'cache_read':>12} {'cache_write':>12}")
    print("  " + "-" * 56)
    for r in results:
        print(f"{r.mode:<10} {r.turns_ok:>7} {r.total_raw_input:>12,} "
              f"{r.total_cache_read:>12,} {r.total_cache_write:>12,}")
        if r.turns_failed:
            print(f"  ⚠ {r.turns_failed} 轮上游调用失败（已跳过）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="stela.replay",
        description="录制 → 重放对照：同一会话按多种 mode 各跑一遍，"
                    "结果进 dashboard 的 A/B 对比面板。",
    )
    ap.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS_DIR,
                    help=f"会话语料目录（默认 {DEFAULT_CORPUS_DIR}）")
    ap.add_argument("--list", action="store_true",
                    help="列出语料库里的会话后退出")
    ap.add_argument("--session", default=None,
                    help="要重放的会话 id（见 --list）")
    ap.add_argument("--modes", default="none,stela,rtk,both",
                    help="逗号分隔的 mode 列表（默认 none,stela,rtk,both）")
    ap.add_argument("--usage-log", type=Path, default=_DEFAULT_USAGE_LOG,
                    help=f"重放结果 append 到此 jsonl（默认 {_DEFAULT_USAGE_LOG}）")
    ap.add_argument("--compare-group", default=None,
                    help="对比分组键（默认用会话 id）")
    ap.add_argument("--upstream", default="https://api.anthropic.com",
                    help="上游 Anthropic endpoint")
    ap.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY"),
                    help="Anthropic API key（默认读 ANTHROPIC_API_KEY）")
    ap.add_argument("--no-cache-isolation", action="store_true",
                    help="不给每个 mode 注入唯一 system 前缀（默认注入，"
                         "避免跨 mode 缓存互相污染）")
    args = ap.parse_args(argv)

    if args.list:
        return _print_sessions(args.corpus_dir)

    if not args.session:
        print("需要 --session <id>（或用 --list 查看可用会话）", file=sys.stderr)
        return 2

    try:
        turns = load_session(args.corpus_dir, args.session)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1
    if not turns:
        print(f"会话 {args.session} 没有可重放的轮次", file=sys.stderr)
        return 1

    if not args.api_key:
        print("缺少 API key：设 ANTHROPIC_API_KEY 或传 --api-key", file=sys.stderr)
        return 2

    modes = [StelaMode.from_label(m.strip()) for m in args.modes.split(",")
             if m.strip()]
    if not modes:
        print(f"--modes 解析为空；合法值：{', '.join(MODE_LABELS)}", file=sys.stderr)
        return 2

    compare_group = args.compare_group or args.session
    sender = anthropic_sender(api_key=args.api_key, upstream=args.upstream)
    flt = build_filter()

    print(f"重放会话 {args.session}（{len(turns)} 轮）"
          f"× {len(modes)} mode → {args.usage_log}")
    t0 = time.time()
    results: list[ReplayResult] = []
    for mode in modes:
        print(f"  · mode={mode.label} …", flush=True)
        results.append(replay_session(
            turns, mode,
            session_id=args.session,
            compare_group=compare_group,
            sender=sender,
            flt=flt,
            cache_isolation=not args.no_cache_isolation,
        ))

    n = _append_records(args.usage_log, results)
    _print_summary(results)
    print(f"\n写入 {n} 条记录 · 用时 {time.time() - t0:.1f}s")
    print(f"看对比：stela dashboard --usage-log {args.usage_log}")
    print(f"  （compare_group = {compare_group}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
