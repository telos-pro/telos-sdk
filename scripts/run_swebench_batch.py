#!/usr/bin/env python3
"""Batch-run SWE-bench Verified (telos + TELOS + OpenRouter).

Randomly samples N instances from the dataset, runs them to completion
concurrently through ``run_swebench_one``, optionally runs the evaluator
automatically, and aggregates ``result_5_2.md``-style metrics.

Usage::

    export OPENROUTER_API_KEY=sk-or-...
    export PYTHONPATH=/Users/george/Code

    # 5 random instances, 4-way concurrency, evaluate immediately after finishing
    python -m telos.scripts.run_swebench_batch \\
        -n 5 --seed 42 --workers 4 \\
        --model deepseek/deepseek-v4-flash \\
        --results-dir /tmp/telos-telos-runs \\
        --evaluate

    # Run only the specified instances (bypassing random sampling)
    python -m telos.scripts.run_swebench_batch \\
        --instances pallets__flask-5014 django__django-14373 \\
        --workers 2

Output:

* Each instance is still the 4-file set from ``run_swebench_one``
  (``.patch / .trajectory.json / .result.json / .usage.jsonl``)
* After evaluation there is a ``.eval.json``
* The batch root directory gets a new ``batch-<timestamp>.json``: full state + aggregated metrics
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any


TELOS_ROOT = Path("/Users/george/Code/tokenpilot-ai/telos")
TEF_ROOT = Path("/Users/george/Code/token-efficient-framework")
DEFAULT_DATASET = TEF_ROOT / "benchmark" / "datasets" / "swe-bench-verified.jsonl"
DEFAULT_RESULTS = Path("/tmp/telos-telos-runs")


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------

def load_all_instance_ids(dataset_path: Path,
                          repo_filter: list[str] | None = None) -> list[str]:
    if not dataset_path.exists():
        sys.exit(f"dataset missing: {dataset_path}\n"
                 "tip: run `run_swebench_one --instance ... ` once to "
                 "auto-download from HuggingFace.")
    ids: list[str] = []
    repo_set = set(repo_filter) if repo_filter else None
    with dataset_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if repo_set and d.get("repo") not in repo_set:
                continue
            ids.append(d["instance_id"])
    return ids


def pick_instances(args: argparse.Namespace) -> list[str]:
    if args.instances:
        return list(args.instances)
    pool = load_all_instance_ids(Path(args.dataset),
                                 repo_filter=args.repo)
    if not pool:
        sys.exit("no instances matched the filters.")
    if args.n is None or args.n >= len(pool):
        return pool
    rng = random.Random(args.seed)
    return rng.sample(pool, args.n)


# ---------------------------------------------------------------------------
# Single-task subprocess
# ---------------------------------------------------------------------------

def run_one(instance_id: str, args: argparse.Namespace,
            log_dir: Path) -> dict[str, Any]:
    """Invoke run_swebench_one in a subprocess; return that task's metadata."""
    cmd = [
        sys.executable, "-m", "telos.scripts.run_swebench_one",
        "--instance", instance_id,
        "--model", args.model,
        "--dataset", args.dataset,
        "--results-dir", args.results_dir,
        "--max-iterations", str(args.max_iterations),
        "--command-timeout", str(args.command_timeout),
    ]
    if args.keep_worktree:
        cmd.append("--keep-worktree")
    if args.no_telos:
        cmd.append("--no-telos")

    tag_prefix = "vanilla" if args.no_telos else "telos"
    log_path = log_dir / f"{tag_prefix}-{instance_id}.runner.log"
    t0 = time.time()
    with log_path.open("w") as logf:
        logf.write(f"$ {' '.join(cmd)}\n\n")
        logf.flush()
        env = os.environ.copy()
        try:
            rc = subprocess.run(
                cmd, stdout=logf, stderr=subprocess.STDOUT,
                env=env, timeout=args.task_timeout,
            ).returncode
            err: str | None = None
        except subprocess.TimeoutExpired:
            rc = -1
            err = f"task_timeout({args.task_timeout}s)"
        except Exception as e:  # noqa: BLE001
            rc = -1
            err = f"{type(e).__name__}: {e}"
    duration = int(time.time() - t0)

    # Read back the result.json that run_swebench_one wrote itself (if any)
    result_path = Path(args.results_dir) / f"{tag_prefix}-{instance_id}.result.json"
    summary: dict[str, Any] = {}
    if result_path.exists():
        try:
            summary = json.loads(result_path.read_text())
        except Exception:  # noqa: BLE001
            pass

    return {
        "instance_id": instance_id,
        "returncode": rc,
        "duration_s": duration,
        "error": err,
        "log": str(log_path),
        "result": summary,
    }


# ---------------------------------------------------------------------------
# Evaluate + aggregate
# ---------------------------------------------------------------------------

def run_evaluator(args: argparse.Namespace) -> int:
    evaluator = TEF_ROOT / "benchmark" / "scripts" / "evaluate-patches.py"
    if not evaluator.exists():
        print(f"[eval] skip: {evaluator} not found", flush=True)
        return 0
    cmd = [
        sys.executable, str(evaluator),
        "--results-dir", args.results_dir,
        "--dataset", args.dataset,
        "--filter-agent", "vanilla" if args.no_telos else "telos",
        "--max-parallel", str(args.eval_workers),
        "--python-bin", sys.executable,
    ]
    if args.force_eval:
        cmd.append("--force")
    print(f"[eval] $ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, check=False).returncode


def aggregate(instance_ids: list[str], results_dir: Path, *,
              tag_prefix: str = "telos") -> dict[str, Any]:
    agg = {"raw_input": 0, "cache_read": 0, "output": 0, "calls": 0}
    per_inst: list[dict[str, Any]] = []
    resolved = evaluated = 0
    for inst in instance_ids:
        tag = f"{tag_prefix}-{inst}"
        usage = results_dir / f"{tag}.usage.jsonl"
        ev = results_dir / f"{tag}.eval.json"
        rec: dict[str, Any] = {"instance_id": inst}
        ti = {"raw_input": 0, "cache_read": 0, "output": 0, "calls": 0}
        if usage.exists():
            for line in usage.open():
                line = line.strip()
                if not line:
                    continue
                try:
                    n = json.loads(line)["normalized"]
                except Exception:  # noqa: BLE001
                    continue
                ti["calls"] += 1
                for k in ("raw_input", "cache_read", "output"):
                    ti[k] += int(n.get(k, 0))
        for k, v in ti.items():
            agg[k] += v
        rec.update(ti)
        if ev.exists():
            try:
                e = json.loads(ev.read_text())
                rec["resolved"] = bool(e.get("resolved"))
                rec["reason"] = e.get("reason")
                evaluated += 1
                resolved += int(rec["resolved"])
            except Exception:  # noqa: BLE001
                rec["resolved"] = None
        per_inst.append(rec)

    inp_total = agg["raw_input"] + agg["cache_read"]
    n = len(instance_ids) or 1
    return {
        "n_instances": len(instance_ids),
        "n_evaluated": evaluated,
        "n_resolved": resolved,
        "resolved_rate": (resolved / evaluated) if evaluated else None,
        "totals": agg,
        "per_task_avg": {
            "raw_input": agg["raw_input"] / n,
            "cache_read": agg["cache_read"] / n,
            "input_total": inp_total / n,
            "output": agg["output"] / n,
            "calls": agg["calls"] / n,
        },
        "cache_share": (agg["cache_read"] / inp_total) if inp_total else 0.0,
        "per_instance": per_inst,
    }


def print_report(report: dict[str, Any]) -> None:
    t = report["per_task_avg"]
    print("\n" + "=" * 68)
    print(f"  batch summary  ({report['n_instances']} instances)")
    print("=" * 68)
    if report["n_evaluated"]:
        rate = 100 * report["resolved_rate"]
        print(f"  resolved: {report['n_resolved']}/{report['n_evaluated']} "
              f"({rate:.1f}%)")
    else:
        print("  resolved: (evaluator not run)")
    print(f"  cache_share: {100 * report['cache_share']:.1f}%")
    print("  per task (avg):")
    print(f"    raw_input   = {t['raw_input']:>10,.0f}")
    print(f"    cache_read  = {t['cache_read']:>10,.0f}")
    print(f"    input_total = {t['input_total']:>10,.0f}")
    print(f"    output      = {t['output']:>10,.0f}")
    print(f"    api_calls   = {t['calls']:>10,.1f}")
    print("=" * 68)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)

    sel = ap.add_argument_group("instance selection")
    sel.add_argument("-n", type=int, default=None,
                     help="number of random instances to sample "
                          "(default: all matching)")
    sel.add_argument("--seed", type=int, default=42,
                     help="random seed for sampling (default: 42)")
    sel.add_argument("--instances", nargs="+",
                     help="explicit instance_ids; overrides -n / --seed")
    sel.add_argument("--repo", action="append",
                     help="filter by repo (e.g. --repo pallets/flask); "
                          "may repeat")

    run = ap.add_argument_group("run")
    run.add_argument("--model", default="deepseek/deepseek-v4-flash")
    run.add_argument("--dataset", default=str(DEFAULT_DATASET))
    run.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    run.add_argument("--max-iterations", type=int, default=25)
    run.add_argument("--command-timeout", type=int, default=60)
    run.add_argument("--task-timeout", type=int, default=1800,
                     help="hard timeout per instance subprocess (s)")
    run.add_argument("--workers", type=int, default=4,
                     help="concurrent runner workers (default: 4); "
                          "lower if OpenRouter 429s")
    run.add_argument("--keep-worktree", action="store_true")
    run.add_argument("--no-telos", action="store_true",
                     help="bypass TELOS — plain OpenAI client (A/B baseline)")

    ev = ap.add_argument_group("evaluate")
    ev.add_argument("--evaluate", action="store_true",
                    help="run evaluator after all instances finish")
    ev.add_argument("--eval-workers", type=int, default=2)
    ev.add_argument("--force-eval", action="store_true",
                    help="pass --force to evaluator (re-eval existing)")

    ap.add_argument("--dry-run", action="store_true",
                    help="just print the sampled instance ids and exit")

    args = ap.parse_args()

    if not os.environ.get("OPENROUTER_API_KEY"):
        sys.exit("OPENROUTER_API_KEY not set in environment.")

    instances = pick_instances(args)
    print(f"[batch] selected {len(instances)} instances "
          f"(seed={args.seed}, workers={args.workers})", flush=True)
    for i in instances:
        print(f"  - {i}")
    if args.dry_run:
        return

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    log_dir = results_dir / "logs"
    log_dir.mkdir(exist_ok=True)

    t0 = time.time()
    per_run: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(run_one, inst, args, log_dir): inst
                   for inst in instances}
        for i, fut in enumerate(as_completed(futures), 1):
            inst = futures[fut]
            try:
                rec = fut.result()
            except Exception as e:  # noqa: BLE001
                rec = {"instance_id": inst, "returncode": -1,
                       "error": f"{type(e).__name__}: {e}"}
            per_run.append(rec)
            ok = rec.get("returncode") == 0
            r = rec.get("result", {}) or {}
            print(
                f"[batch {i}/{len(instances)}] {inst} "
                f"rc={rec.get('returncode')} "
                f"dur={rec.get('duration_s')}s "
                f"patch={r.get('patch_bytes', '?')}B "
                f"calls={r.get('api_calls', '?')} "
                f"completed={r.get('completed', '?')}"
                + ("" if ok else f"  ERR: {rec.get('error')}"),
                flush=True,
            )
    batch_duration = int(time.time() - t0)

    if args.evaluate:
        run_evaluator(args)

    report = aggregate(instances, results_dir,
                       tag_prefix="vanilla" if args.no_telos else "telos")
    report.update({
        "model": args.model,
        "seed": args.seed,
        "n_workers": args.workers,
        "batch_duration_s": batch_duration,
        "started_at": datetime.utcfromtimestamp(t0).isoformat() + "Z",
        "runs": per_run,
    })
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    bench_dir = results_dir / "benchmark"
    bench_dir.mkdir(parents=True, exist_ok=True)
    out = bench_dir / f"batch-{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    # also keep a stable "latest" pointer for convenience
    latest = bench_dir / "latest.json"
    latest.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n[batch] wrote {out}", flush=True)
    print(f"[batch] latest -> {latest}", flush=True)
    print_report(report)


if __name__ == "__main__":
    main()
