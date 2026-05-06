# 用 Telos + STELA 跑 SWE-bench Benchmark

> 把 [Telos](https://github.com/tokenpilot-ai/telos)（vendored Hermes Agent）作为
> harness，通过 STELA 这条 cache-friendly 管道，调 OpenRouter 上的 DeepSeek-V4
> 跑 SWE-bench Verified 任务，并复现 [`result_5_2.md`][r52] 的指标格式。
>
> [r52]: https://github.com/.../token-efficient-framework/blob/main/result_5_2.md

---

## 总览

```
┌────────────────────────────┐
│ telos.MiniSWERunner        │  ← vendored Hermes（telos/vendor/hermes/）
│   self.client.chat...      │
└──────────────┬─────────────┘
               │ OpenAI ChatCompletions shape
               ▼
┌────────────────────────────┐
│ StelaOpenAITransport       │  ← stela/scripts/stela_transport.py
│   harness=telos            │
│   engine=deepseek          │
│   ┌──────────────────────┐ │
│   │ telos harness plugin │ │  ← stela/harness/telos.py
│   │   parse → StelaIR    │ │
│   ├──────────────────────┤ │
│   │ Bridge.mark()        │ │  ← stela/bridge.py
│   │   canonicalize +     │ │
│   │   §5 band layout     │ │
│   └──────────────────────┘ │
└──────────────┬─────────────┘
               │ chat-completions wire（DROP 段下沉、tool_calls 保结构）
               ▼
       OpenRouter /v1/chat/completions
        (deepseek/deepseek-v4-flash)
               │
               ▼
       patch ─► evaluate-patches.py ─► resolved? + token 指标
```

---

## 0. 一次性环境准备

### 路径速记

```bash
export STELA=/Users/george/Code/stela
export TELOS=/Users/george/Code/tokenpilot-ai/telos
export TEF=/Users/george/Code/token-efficient-framework
export PY=$TELOS/.venv/bin/python    # ← 全程用 telos venv
```

### 必需的环境变量

```bash
# OpenRouter API key
export OPENROUTER_API_KEY=sk-or-v1-...

# stela 必须能被 import（仓库根在 /Users/george/Code）
# ⚠️ 新开 shell 必须重新 export，否则 -m stela.scripts.run_swebench_one 会报
#    ModuleNotFoundError: No module named 'stela'
export PYTHONPATH=/Users/george/Code
```

> 想免 export，可以一次性 `cd $TELOS && uv pip install -e $STELA` 把 stela 装进
> telos venv；之后任何 shell 直接 `$PY -m stela.scripts.run_swebench_one ...` 即可。

### 子模块 / 数据集

```bash
# vendored Hermes 必须就位
ls $TELOS/vendor/hermes/mini_swe_runner.py >/dev/null \
  || (cd $TELOS && git submodule update --init)

# SWE-bench Verified 数据集（首次运行 runner 会自动从 HF 下载）
ls $TEF/benchmark/datasets/swe-bench-verified.jsonl
```

---

## 1. 跑一个任务

```bash
cd $STELA
PYTHONPATH=/Users/george/Code \
$PY -m stela.scripts.run_swebench_one \
    --instance pallets__flask-5014 \
    --model deepseek/deepseek-v4-flash \
    --max-iterations 25 \
    --command-timeout 60 \
    --results-dir /tmp/stela-telos-runs
```

> 把 `PYTHONPATH=...` 内联进命令是最稳的写法；如果当前 shell 已经
> `export PYTHONPATH=/Users/george/Code` 可以省略前缀。

跑完后 `/tmp/stela-telos-runs/` 下生成 4 个文件：

| 文件 | 内容 |
|---|---|
| `telos-<inst>.patch`           | `git diff HEAD`，喂给 evaluator |
| `telos-<inst>.trajectory.json` | Hermes 风格 conversations |
| `telos-<inst>.result.json`     | duration / api_calls / completed |
| `telos-<inst>.usage.jsonl`     | 每次 LLM 调用一行：raw + 归一化 token 计数 |

### 挑实例

`result_5_2.md` 用的是 50 个采样。从全集随机挑几个：

```bash
$PY -c "
import json, random
random.seed(42)
ids = [json.loads(l)['instance_id']
       for l in open('$TEF/benchmark/datasets/swe-bench-verified.jsonl')]
print('\n'.join(random.sample(ids, 5)))
"
```

---

## 2. 评测 patch 是否真的解题

### 2.1 先克隆被测仓库

evaluator 期望仓库在 `/tmp/swebench-repos/<owner>__<repo>/.git`：

```bash
mkdir -p /tmp/swebench-repos
git clone https://github.com/django/django.git /tmp/swebench-repos/django__django
# 或 mirror 也行：git clone --mirror ... .git
```

### 2.2 跑 evaluator（用 telos venv）

```bash
$PY $TEF/benchmark/scripts/evaluate-patches.py \
    --results-dir /tmp/stela-telos-runs \
    --dataset $TEF/benchmark/datasets/swe-bench-verified.jsonl \
    --filter-agent telos \
    --max-parallel 1 \
    --python-bin $PY \
    --force          # 已有 .eval.json 时强制重测
```

### 2.3 看结果

```bash
cat /tmp/stela-telos-runs/telos-pallets__flask-5014.eval.json | $PY -m json.tool
```

关键字段：

- `resolved`: 总成败
- `model_patch_applied`: patch 是否 git apply 成功
- `fail_to_pass_passed / fail_to_pass_total`: FAIL_TO_PASS 测试通过比

> evaluator 会在被测仓库里 `pip install -e .` 到 `$PY` 这个 venv。
> telos venv 已经有 `flask`；其它仓库（django / sympy / sphinx…）首次跑会自己装。

---

## 3. 算单任务的 token 指标

四个北极星指标（与 `result_5_2.md` 列对齐）：

```bash
$PY -c "
import json
path = '/tmp/stela-telos-runs/telos-pallets__flask-5014.usage.jsonl'
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

输出对应 `result_5_2.md` 表格的：

| 字段 | 来源 |
|---|---|
| **raw_input / task**   | `raw_input` 求和 |
| **cache_read / task**  | `cache_read` 求和 |
| **input_total / task** | `raw_input + cache_read` |
| **output / task**      | `output` 求和 |
| **resolved**           | `.eval.json` 的 `resolved` 字段 |

---

## 4. 批量跑

`run_swebench_one.py` 没内置并发，最简方式是 shell 并行（OpenRouter 会限速，
**先 `-P4` 起步**，遇到 429 调小到 `-P2`）：

```bash
INSTANCES=(
  pallets__flask-5014
  django__django-14373
  psf__requests-1142
  sympy__sympy-13615
)

mkdir -p /tmp/stela-telos-runs
printf '%s\n' "${INSTANCES[@]}" | xargs -n1 -P4 -I{} \
  $PY -m stela.scripts.run_swebench_one \
    --instance {} \
    --model deepseek/deepseek-v4-flash \
    --max-iterations 25 \
    --results-dir /tmp/stela-telos-runs

# 一次性评测全部
$PY $TEF/benchmark/scripts/evaluate-patches.py \
    --results-dir /tmp/stela-telos-runs \
    --dataset $TEF/benchmark/datasets/swe-bench-verified.jsonl \
    --filter-agent telos --max-parallel 2 \
    --python-bin $PY --force
```

聚合所有 instance 的指标：

```bash
$PY -c "
import json, glob
agg = {'raw_input':0, 'cache_read':0, 'output':0, 'calls':0}
resolved = total = 0
for usage in glob.glob('/tmp/stela-telos-runs/telos-*.usage.jsonl'):
    inst = usage.replace('.usage.jsonl','')
    for l in open(usage):
        d = json.loads(l)['normalized']; agg['calls'] += 1
        for k in ('raw_input','cache_read','output'): agg[k] += d[k]
    try:
        ev = json.load(open(inst + '.eval.json'))
        total += 1; resolved += int(ev.get('resolved', False))
    except FileNotFoundError:
        pass
inp = agg['raw_input'] + agg['cache_read']
print(f'tasks evaluated: {total}  resolved: {resolved} ({100*resolved/max(total,1):.1f}%)')
print(f'calls: {agg[\"calls\"]}  raw_input: {agg[\"raw_input\"]}  '
      f'cache_read: {agg[\"cache_read\"]}  output: {agg[\"output\"]}  '
      f'cache_share: {100*agg[\"cache_read\"]/max(inp,1):.1f}%')
"
```

---

## 5. 常见坑

| 现象 | 修法 |
|---|---|
| `OPENROUTER_API_KEY not set` | `export OPENROUTER_API_KEY=...` |
| `ModuleNotFoundError: stela` | `export PYTHONPATH=/Users/george/Code` |
| `mini_swe_runner` 找不到 | `cd $TELOS && git submodule update --init` |
| evaluator 报 `repo_not_cloned` | `git clone https://github.com/<owner>/<repo>.git /tmp/swebench-repos/<owner>__<repo>` |
| evaluator 报 `ModuleNotFoundError: <pkg>` | `cd $TELOS && uv pip install <pkg>` |
| `Reached max iterations` 但 patch 非空 | 可接受，`resolved` 仍会被正确判定 |
| OpenRouter 429 限速 | xargs `-P4` → `-P2` |
| 想保留 worktree 排查 | runner 加 `--keep-worktree` |

---

## 6. `run_swebench_one.py` 参数速查

| 参数 | 默认 | 说明 |
|---|---|---|
| `--instance`         | `pallets__flask-5014` | SWE-bench Verified `instance_id` |
| `--model`            | `deepseek/deepseek-chat` | OpenRouter model id；`result_5_2.md` 用 `deepseek/deepseek-v4-flash` |
| `--dataset`          | `$TEF/benchmark/datasets/swe-bench-verified.jsonl` | 数据集路径（不存在会自动从 HF 下） |
| `--results-dir`      | `/tmp/stela-telos-runs` | 输出目录 |
| `--max-iterations`   | 25 | agent 循环上限 |
| `--command-timeout`  | 60 | 单次 bash 超时（秒） |
| `--evaluate`         | off | 任务跑完直接调 evaluator（需先克隆仓库） |
| `--keep-worktree`    | off | 调试：不删 `/tmp/stela-swebench/<tag>` |

---

## 7. 已验证的端到端样例

| 项 | 值 |
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

复现命令：

```bash
$PY -m stela.scripts.run_swebench_one \
    --instance pallets__flask-5014 \
    --model deepseek/deepseek-v4-flash \
    --max-iterations 12 \
    --results-dir /tmp/stela-telos-runs

$PY $TEF/benchmark/scripts/evaluate-patches.py \
    --results-dir /tmp/stela-telos-runs \
    --dataset $TEF/benchmark/datasets/swe-bench-verified.jsonl \
    --filter-agent telos --max-parallel 1 \
    --python-bin $PY --force
```

---

## 8. 相关文件索引

| 文件 | 作用 |
|---|---|
| [stela/harness/telos.py](../harness/telos.py) | telos harness 插件（OpenAI ChatCompletions → StelaIR） |
| [stela/scripts/stela_transport.py](../scripts/stela_transport.py) | OpenAI 鸭子接口 client，内部走 STELA Bridge |
| [stela/scripts/run_swebench_one.py](../scripts/run_swebench_one.py) | 单任务端到端 runner |
| [stela/tests/test_telos_harness.py](../tests/test_telos_harness.py) | telos 插件 smoke 测试 |
| [token-efficient-framework/benchmark/scripts/evaluate-patches.py][evp] | SWE-bench patch 评测器（无 docker，git worktree 隔离） |

[evp]: file:///Users/george/Code/token-efficient-framework/benchmark/scripts/evaluate-patches.py
