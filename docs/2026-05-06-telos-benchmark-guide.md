# Running the SWE-bench Benchmark with Telos + TELOS

> Use [Telos](https://github.com/tokenpilot-ai/telos) (the vendored Hermes Agent) as the
> harness, run SWE-bench Verified tasks against DeepSeek-V4 on OpenRouter through the
> cache-friendly TELOS pipeline, and reproduce the metrics format of [`result_5_2.md`][r52].
>
> [r52]: https://github.com/.../token-efficient-framework/blob/main/result_5_2.md

---

## Overview

```
┌────────────────────────────┐
│ telos.MiniSWERunner        │  ← vendored Hermes (telos/vendor/hermes/)
│   self.client.chat...      │
└──────────────┬─────────────┘
               │ OpenAI ChatCompletions shape
               ▼
┌────────────────────────────┐
│ TelosOpenAITransport       │  ← telos/scripts/telos_transport.py
│   harness=telos            │
│   engine=deepseek          │
│   ┌──────────────────────┐ │
│   │ telos harness plugin │ │  ← telos/harness/telos.py
│   │   parse → TelosIR    │ │
│   ├──────────────────────┤ │
│   │ Bridge.mark()        │ │  ← telos/bridge.py
│   │   canonicalize +     │ │
│   │   §5 band layout     │ │
│   └──────────────────────┘ │
└──────────────┬─────────────┘
               │ chat-completions wire (DROP segment sunk down, tool_calls keep structure)
               ▼
       OpenRouter /v1/chat/completions
        (deepseek/deepseek-v4-flash)
               │
               ▼
       patch ─► evaluate-patches.py ─► resolved? + token metrics
```

---

## 0. One-time environment setup

### Path cheat sheet

```bash
export TELOS=/Users/george/Code/telos
export TELOS=/Users/george/Code/tokenpilot-ai/telos
export TEF=/Users/george/Code/token-efficient-framework
export PY=$TELOS/.venv/bin/python    # ← use the telos venv throughout
```

### Required environment variables

```bash
# OpenRouter API key
export OPENROUTER_API_KEY=sk-or-v1-...

# telos must be importable (the repo root is /Users/george/Code)
# ⚠️ a new shell must re-export, otherwise -m telos.scripts.run_swebench_one reports
#    ModuleNotFoundError: No module named 'telos'
export PYTHONPATH=/Users/george/Code
```

> To skip the export, you can do a one-time `cd $TELOS && uv pip install -e $TELOS` to install telos into
> the telos venv; afterward any shell can directly run `$PY -m telos.scripts.run_swebench_one ...`.

### Submodule / dataset

```bash
# vendored Hermes must be in place
ls $TELOS/vendor/hermes/mini_swe_runner.py >/dev/null \
  || (cd $TELOS && git submodule update --init)

# SWE-bench Verified dataset (the runner auto-downloads it from HF on first run)
ls $TEF/benchmark/datasets/swe-bench-verified.jsonl
```

---

## 1. Running one task

```bash
cd $TELOS
PYTHONPATH=/Users/george/Code \
$PY -m telos.scripts.run_swebench_one \
    --instance pallets__flask-5014 \
    --model deepseek/deepseek-v4-flash \
    --max-iterations 25 \
    --command-timeout 60 \
    --results-dir /tmp/telos-telos-runs
```

> Inlining `PYTHONPATH=...` into the command is the most robust form; if the current shell already has
> `export PYTHONPATH=/Users/george/Code` you can omit the prefix.

After the run, 5 files are generated under `/tmp/telos-telos-runs/`:

| File | Contents |
|---|---|
| `telos-<inst>.patch`             | `git diff HEAD`, fed to the evaluator |
| `telos-<inst>.trajectory.json`   | Hermes-style conversations |
| `telos-<inst>.result.json`       | duration / api_calls / completed |
| `telos-<inst>.usage.jsonl`       | one line per LLM call: raw + normalized token counts |
| `telos-<inst>.prompt_trace.jsonl`| **one line per call**: prompt-build snapshot + prefix stability + cache hits |

### Picking instances

`result_5_2.md` uses 50 samples. To pick a few at random from the full set:

```bash
$PY -c "
import json, random
random.seed(42)
ids = [json.loads(l)['instance_id']
       for l in open('$TEF/benchmark/datasets/swe-bench-verified.jsonl')]
print('\n'.join(random.sample(ids, 5)))
"
```

Or just use the batch runner directly (see §4) — it has built-in `-n N --seed S` sampling.

---

## 1.5 Viewing the prompt-build & cache-hit trace

Each run produces an extra `<tag>.prompt_trace.jsonl`, recording 6 snapshots for each LLM call:

| Field | Meaning |
|---|---|
| `input`                  | stats of the original messages / tools the caller passed in |
| `ir_after_parse`         | the IR after the telos harness parses it (blocks/chars counted per band) |
| `ir_after_canonicalize`  | after Bridge canonicalize + band-reorder |
| `plan`                   | the mark slot names and routing_key |
| `wire`                   | the chat-completions structure actually sent |
| `prefix.prefix_stability`| the common prefix with the previous call's wire / the previous call's total length (0~1) |
| `cache.{raw_input,cache_read,output,cache_share}` | normalized token counts |

### Reading on the command line

```bash
$PY -m telos.scripts.show_prompt_trace \
    /tmp/telos-telos-runs/telos-pallets__flask-5014.prompt_trace.jsonl
```

Example output (1 row = 1 LLM call):

```
  #  role-counts          wire chars  prefix%   raw_in    cache    out  cache%  plan
  1  s=1 u=1                     983      -        251      512     88   67.1%   -
  5  s=1 u=1 a=4 t=4           5,747   84.4%       274    2,048    138   88.2%   -
 12  s=1 u=1 a=11 t=11         7,579   90.1%       795    2,816     87   78.0%   -
TOTAL  raw_input=18,743  cache_read=9,728  output=1,603  cache_share=34.2%
```

Watch three things:

- **`prefix%` rising monotonically** → TELOS has settled system/tools/conversation history (changes happen only at the tail).
- **`cache%` with intermittent peaks** → DeepSeek's 512-token block boundaries at work; two or three consecutive calls skipping the same boundary will all hit.
- **`TOTAL cache_share`** is the global metric aligned with `result_5_2.md`.

### jq the jsonl yourself

```bash
jq -c '{i: .call_index, prefix: .prefix.prefix_stability, cache: .cache.cache_share}' \
    /tmp/telos-telos-runs/telos-pallets__flask-5014.prompt_trace.jsonl
```

---

## 2. Evaluating whether the patch actually solves the task

### 2.1 First clone the repo under test

The evaluator expects the repo at `/tmp/swebench-repos/<owner>__<repo>/.git`:

```bash
mkdir -p /tmp/swebench-repos
git clone https://github.com/django/django.git /tmp/swebench-repos/django__django
# a mirror also works: git clone --mirror ... .git
```

### 2.2 Run the evaluator (with the telos venv)

```bash
$PY $TEF/benchmark/scripts/evaluate-patches.py \
    --results-dir /tmp/telos-telos-runs \
    --dataset $TEF/benchmark/datasets/swe-bench-verified.jsonl \
    --filter-agent telos \
    --max-parallel 1 \
    --python-bin $PY \
    --force          # force re-evaluation when a .eval.json already exists
```

### 2.3 Viewing the results

```bash
cat /tmp/telos-telos-runs/telos-pallets__flask-5014.eval.json | $PY -m json.tool
```

Key fields:

- `resolved`: overall success/failure
- `model_patch_applied`: whether the patch applied with git apply successfully
- `fail_to_pass_passed / fail_to_pass_total`: the pass ratio of FAIL_TO_PASS tests

> The evaluator runs `pip install -e .` of the repo under test into the `$PY` venv.
> The telos venv already has `flask`; other repos (django / sympy / sphinx…) install themselves on first run.

---

## 3. Computing per-task token metrics

The four north-star metrics (aligned with the `result_5_2.md` columns):

```bash
$PY -c "
import json
path = '/tmp/telos-telos-runs/telos-pallets__flask-5014.usage.jsonl'
tot = {'raw_input': 0, 'cache_read': 0, 'output': 0}
n = 0
for l in open(path):
    d = json.loads(l)['normalized']; n += 1
    for k in tot: tot[k] += d[k]
inp = tot['raw_input'] + tot['cache_read']
print(f'calls={n}  raw_input={tot[\"raw_input\"]}  '
      f'cache_read={tot[\"cache_read\"]}  output={tot[\"output\"]}  '
      f'cache_share={100*tot[\"cache_read\"]/max(inp,1):.1f}%')
"
```

The output corresponds to the `result_5_2.md` table's:

| Field | Source |
|---|---|
| **raw_input / task**   | sum of `raw_input` |
| **cache_read / task**  | sum of `cache_read` |
| **input_total / task** | `raw_input + cache_read` |
| **output / task**      | sum of `output` |
| **resolved**           | the `resolved` field of `.eval.json` |

---

## 4. Batch runs: `run_swebench_batch`

Built-in random sampling, concurrency, automatic evaluation, and a batch report. **The recommended
entry point — no need to hand-write `xargs`.**

```bash
# dry run: see which it will sample
PYTHONPATH=/Users/george/Code $PY -m telos.scripts.run_swebench_batch \
    -n 5 --seed 42 --dry-run

# real run: 5 at random, 4-way concurrency, evaluate right after the run
PYTHONPATH=/Users/george/Code $PY -m telos.scripts.run_swebench_batch \
    -n 5 --seed 42 --workers 4 \
    --model deepseek/deepseek-v4-flash \
    --results-dir /tmp/telos-telos-runs \
    --evaluate

# pick only certain repos
PYTHONPATH=/Users/george/Code $PY -m telos.scripts.run_swebench_batch \
    -n 10 --repo pallets/flask --repo psf/requests \
    --workers 2 --evaluate

# specify concrete instances (overrides random sampling)
PYTHONPATH=/Users/george/Code $PY -m telos.scripts.run_swebench_batch \
    --instances pallets__flask-5014 django__django-14373 \
    --workers 2 --evaluate
```

### Key parameters

| Parameter | Default | Description |
|---|---|---|
| `-n N`             | full set | number of random samples |
| `--seed`           | 42   | determines reproducibility |
| `--instances ...`  | —    | explicit list, overrides random |
| `--repo`           | —    | can be specified multiple times to add up (e.g. `--repo pallets/flask`) |
| `--workers`        | 4    | number of concurrent tasks (drop to 2 on OpenRouter 429) |
| `--task-timeout`   | 1800 | hard timeout for a single instance's subprocess (seconds) |
| `--evaluate`       | off  | run the evaluator after the batch |
| `--eval-workers`   | 2    | evaluator concurrency |
| `--force-eval`     | off  | re-evaluate an existing `.eval.json` |
| `--dry-run`        | off  | print the sample list only |

### Output

- each instance is still the 5-file set from §1 + the post-evaluation `.eval.json`
- the runner subprocess log per instance: `logs/telos-<inst>.runner.log`
- the **batch report** is written to `<results-dir>/benchmark/`:
  - `batch-<UTC timestamp>.json` — a timestamped archive
  - `latest.json` — always points to the latest

The end of the console prints an aggregate table: resolved rate + the 4 north-star metrics (per-task average) + cache_share.

---

## 4.5 Dashboard: `build_dashboard`

Combines all `prompt_trace.jsonl + result.json + eval.json` under `<results-dir>/`
into a **single-file HTML dashboard** (zero JS dependencies, can be opened offline / emailed).

```bash
# all instances
PYTHONPATH=/Users/george/Code $PY -m telos.scripts.build_dashboard \
    --results-dir /tmp/telos-telos-runs

# just one
PYTHONPATH=/Users/george/Code $PY -m telos.scripts.build_dashboard \
    --instance pallets__flask-5014 \
    --out /tmp/dashboard-flask.html

open /tmp/telos-telos-runs/benchmark/dashboard.html
```

### Page structure

- **top KPI bar**: instances · resolved ratio · total api calls · raw / cache /
  output sums · **overall cache_share** (colored green/yellow/orange/gray by the 60% / 30% / 0 thresholds)
- **one card per instance**:
  - title: `instance_id` + resolved badge + F2P ratio + completed
  - 6-column metrics: calls / raw_in / cache_read / output / cache_share /
    prefix_avg
  - **per-call trace table** (the core visualization):
    - each row = one API call
    - **stacked bar**: red raw + green cache + blue output; the bar's total width is normalized
      to the largest input within the instance (see at a glance which call is heaviest)
    - cache% color-coded and bold
    - prefix% number + a yellow bar (see at a glance the monotonic-convergence trend of prefix)
    - plan slots (mark anchor positions / routing_key)
- sorting: unresolved first, to make problems easier to triage first

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `--results-dir` | `/tmp/telos-telos-runs` | scans `telos-*.prompt_trace.jsonl` |
| `--instance`    | —   | include only the specified instance (can repeat) |
| `--out`         | `<results-dir>/benchmark/dashboard.html` | output HTML path |

---

## 5. Common pitfalls

| Symptom | Fix |
|---|---|
| `OPENROUTER_API_KEY not set` | `export OPENROUTER_API_KEY=...` |
| `ModuleNotFoundError: telos` | `export PYTHONPATH=/Users/george/Code` |
| `mini_swe_runner` not found | `cd $TELOS && git submodule update --init` |
| evaluator reports `repo_not_cloned` | `git clone https://github.com/<owner>/<repo>.git /tmp/swebench-repos/<owner>__<repo>` |
| evaluator reports `ModuleNotFoundError: <pkg>` | `cd $TELOS && uv pip install <pkg>` |
| `Reached max iterations` but the patch is non-empty | acceptable, `resolved` will still be judged correctly |
| OpenRouter 429 rate limiting | xargs `-P4` → `-P2` |
| want to keep the worktree to investigate | add `--keep-worktree` to the runner |

---

## 6. `run_swebench_one.py` parameter quick reference

| Parameter | Default | Description |
|---|---|---|
| `--instance`         | `pallets__flask-5014` | SWE-bench Verified `instance_id` |
| `--model`            | `deepseek/deepseek-chat` | OpenRouter model id; `result_5_2.md` uses `deepseek/deepseek-v4-flash` |
| `--dataset`          | `$TEF/benchmark/datasets/swe-bench-verified.jsonl` | dataset path (auto-downloaded from HF if absent) |
| `--results-dir`      | `/tmp/telos-telos-runs` | output directory |
| `--max-iterations`   | 25 | agent loop cap |
| `--command-timeout`  | 60 | single bash timeout (seconds) |
| `--evaluate`         | off | call the evaluator right after the task finishes (clone the repo first) |
| `--keep-worktree`    | off | debug: don't delete `/tmp/telos-swebench/<tag>` |

---

## 7. A verified end-to-end example

| Item | Value |
|---|---|
| instance              | `pallets__flask-5014` |
| model                 | `deepseek/deepseek-v4-flash` |
| api calls             | 12 |
| duration              | 110 s |
| patch_bytes           | 436 |
| raw_input / cache_read / output | 29,058 / 7,680 / 1,838 |
| cache_share           | 20.9% |
| evaluator             | `model_patch_applied=true`, `1/1 fail_to_pass passed` |
| **resolved**          | **true** |

Reproduction commands:

```bash
$PY -m telos.scripts.run_swebench_one \
    --instance pallets__flask-5014 \
    --model deepseek/deepseek-v4-flash \
    --max-iterations 12 \
    --results-dir /tmp/telos-telos-runs

$PY $TEF/benchmark/scripts/evaluate-patches.py \
    --results-dir /tmp/telos-telos-runs \
    --dataset $TEF/benchmark/datasets/swe-bench-verified.jsonl \
    --filter-agent telos --max-parallel 1 \
    --python-bin $PY --force
```

---

## 8. Index of related files

| File | Purpose |
|---|---|
| [telos/harness/telos.py](../harness/telos.py) | the telos harness plugin (OpenAI ChatCompletions → TelosIR) |
| [telos/scripts/telos_transport.py](../scripts/telos_transport.py) | OpenAI duck-typed client, internally going through the TELOS Bridge; writes `usage.jsonl` + `prompt_trace.jsonl` |
| [telos/scripts/run_swebench_one.py](../scripts/run_swebench_one.py) | the **single-task** end-to-end runner |
| [telos/scripts/run_swebench_batch.py](../scripts/run_swebench_batch.py) | the **batch** runner (sampling + concurrency + automatic evaluation + batch report) |
| [telos/scripts/show_prompt_trace.py](../scripts/show_prompt_trace.py) | command-line viewer for `prompt_trace.jsonl` |
| [telos/scripts/build_dashboard.py](../scripts/build_dashboard.py) | generates the single-file HTML dashboard |
| [telos/tests/test_telos_harness.py](../tests/test_telos_harness.py) | smoke test for the telos plugin |
| [token-efficient-framework/benchmark/scripts/evaluate-patches.py][evp] | the SWE-bench patch evaluator (no docker, git worktree isolation) |

[evp]: file:///Users/george/Code/token-efficient-framework/benchmark/scripts/evaluate-patches.py
