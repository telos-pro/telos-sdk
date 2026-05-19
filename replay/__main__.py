"""``python -m telos.replay`` / ``telos replay`` entry point.

Replays a real session from the corpus once for each of several modes; the
results are appended to usage_log, and the dashboard's "A/B comparison" panel
shows them side by side automatically (compare_group = the original session id).

Usage::

    telos replay --list                       # list the sessions in the corpus
    telos replay --session telos-ab12cd34      # run all 4 modes by default
    telos replay --session <id> --modes none,both
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from telos.corpus import DEFAULT_CORPUS_DIR, list_sessions, load_session
from telos.output_filter import MODE_LABELS, TelosMode, build_filter
from telos.replay import ReplayResult, anthropic_sender, replay_session

_DEFAULT_USAGE_LOG = Path.home() / ".telos" / "usage.jsonl"


def _print_sessions(corpus_dir: Path) -> int:
    infos = list_sessions(corpus_dir)
    if not infos:
        print(f"corpus is empty: {corpus_dir}")
        print("(run a few real sessions with `telos proxy` first; they are recorded by default)")
        return 0
    print(f"corpus {corpus_dir} —— {len(infos)} sessions:\n")
    print(f"  {'session_id':<40} {'calls':>6}  last_seen")
    for i in infos:
        last = datetime.fromtimestamp(i.last_ts).strftime("%Y-%m-%d %H:%M") \
            if i.last_ts else "—"
        print(f"  {i.session_id:<40} {i.n_calls:>6}  {last}")
    print("\nreplay: telos replay --session <session_id>")
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
            print(f"  ⚠ {r.turns_failed} turns failed the upstream call (skipped)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="telos.replay",
        description="Record → replay comparison: run the same session once for each of "
                    "several modes; the results go into the dashboard's A/B comparison panel.",
    )
    ap.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS_DIR,
                    help=f"session corpus directory (default {DEFAULT_CORPUS_DIR})")
    ap.add_argument("--list", action="store_true",
                    help="list the sessions in the corpus and exit")
    ap.add_argument("--session", default=None,
                    help="the session id to replay (see --list)")
    ap.add_argument("--modes", default="none,telos,rtk,both",
                    help="comma-separated list of modes (default none,telos,rtk,both)")
    ap.add_argument("--usage-log", type=Path, default=_DEFAULT_USAGE_LOG,
                    help=f"replay results are appended to this jsonl (default {_DEFAULT_USAGE_LOG})")
    ap.add_argument("--compare-group", default=None,
                    help="comparison group key (defaults to the session id)")
    ap.add_argument("--upstream", default="https://api.anthropic.com",
                    help="upstream Anthropic endpoint")
    ap.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY"),
                    help="Anthropic API key (defaults to reading ANTHROPIC_API_KEY)")
    ap.add_argument("--no-cache-isolation", action="store_true",
                    help="do not inject a unique system prefix per mode (injected by default, "
                         "to avoid cross-mode cache pollution)")
    args = ap.parse_args(argv)

    if args.list:
        return _print_sessions(args.corpus_dir)

    if not args.session:
        print("--session <id> is required (or use --list to view available sessions)", file=sys.stderr)
        return 2

    try:
        turns = load_session(args.corpus_dir, args.session)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1
    if not turns:
        print(f"session {args.session} has no replayable turns", file=sys.stderr)
        return 1

    if not args.api_key:
        print("missing API key: set ANTHROPIC_API_KEY or pass --api-key", file=sys.stderr)
        return 2

    modes = [TelosMode.from_label(m.strip()) for m in args.modes.split(",")
             if m.strip()]
    if not modes:
        print(f"--modes parsed to empty; valid values: {', '.join(MODE_LABELS)}", file=sys.stderr)
        return 2

    compare_group = args.compare_group or args.session
    sender = anthropic_sender(api_key=args.api_key, upstream=args.upstream)
    flt = build_filter()

    print(f"replaying session {args.session} ({len(turns)} turns)"
          f" × {len(modes)} modes → {args.usage_log}")
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
    print(f"\nwrote {n} records · took {time.time() - t0:.1f}s")
    print(f"view the comparison: telos dashboard --usage-log {args.usage_log}")
    print(f"  (compare_group = {compare_group})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
