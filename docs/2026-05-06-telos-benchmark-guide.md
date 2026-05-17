# 用 Telos + TELOS 跑 SWE-bench Benchmark

> 把 [Telos](https://github.com/tokenpilot-ai/telos)（vendored Hermes Agent）作为
> harness，通过 TELOS 这条 cache-friendly 管道，调 OpenRouter 上的 DeepSeek-V4
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
export TELOS=/Users/george/Code/telos
export TELOS=/Users/george/Code/tokenpilot-ai/telos
export TEF=/Users/george/Code/token-efficient-framework
export PY=$TELOS/.venv/bin/python    # ← 全程用 telos venv
```

### 必需的环境变量

```bash
# OpenRouter API key
export OPENROUTER_API_KEY=sk-or-v1-...

# telos 必须能被 import（仓库根在 /Users/george/Code）
# ⚠️ 新开 shell 必须重新 export，否则 -m telos.scripts.run_swebench_one 会报
#    ModuleNotFoundError: No module named 'telos'
export PYTHONPATH=/Users/george/Code
```

> 想免 export，可以一次性 `cd $TELOS && uv pip install -e $TELOS` 把 telos 装进
> telos venv；之后任何 shell 直接 `$PY -m telos.scripts.run_swebench_one ...` 即可。

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
cd $TELOS
PYTHONPATH=/Users/george/Code \
$PY -m telos.scripts.run_swebench_one \
    --instance pallets__flask-5014 \
    --model deepseek/deepseek-v4-flash \
    --max-iterations 25 \
    --command-timeout 60 \
    --results-dir /tmp/telos-telos-runs
```

> 把 `PYTHONPATH=...` 内联进命令是最稳的写法；如果当前 shell 已经
> `export PYTHONPATH=/Users/george/Code` 可以省略前缀。

跑完后 `/tmp/telos-telos-runs/` 下生成 5 个文件：

| 文件 | 内容 |
|---|---|
| `telos-<inst>.patch`             | `git diff HEAD`，嗂给 evaluator |
| `telos-<inst>.trajectory.json`   | Hermes 风格 conversations |
| `telos-<inst>.result.json`       | duration / api_calls / completed |
| `telos-<inst>.usage.jsonl`       | 每次 LLM 调用一行：raw + 归一化 token 计数 |
| `telos-<inst>.prompt_trace.jsonl`| **每次调用一行**：prompt 构建快照 + 前缀稳定性 + cache 命中 |

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

或者直接用批量 runner（见 §4），它内置了 `-n N --seed S` 采样。

---

## 1.5 看 prompt 构建 & cache 命中 trace

每次跑完会多一份 `<tag>.prompt_trace.jsonl`，记录每次 LLM 调用的 6 个快照：

| 字段 | 含义 |
|---|---|
| `input`                  | caller 传入的原始 messages / tools 统计 |
| `ir_after_parse`         | telos harness 解析后的 IR（按 band 计 blocks/chars）|
| `ir_after_canonicalize`  | Bridge canonicalize + band-reorder 后 |
| `plan`                   | mark slot 名与 routing_key |
| `wire`                   | 真正发出去的 chat-completions 结构 |
| `prefix.prefix_stability`| 与上一调用 wire 的公共前缀 / 上一调用总长（0~1）|
| `cache.{raw_input,cache_read,output,cache_share}` | 归一化后的 token 计数 |

### 命令行阅读

```bash
$PY -m telos.scripts.show_prompt_trace \
    /tmp/telos-telos-runs/telos-pallets__flask-5014.prompt_trace.jsonl
```

输出例（1 行 = 1 次 LLM 调用）：

```
  #  role-counts          wire chars  prefix%   raw_in    cache    out  cache%  plan
  1  s=1 u=1                     983      -        251      512     88   67.1%   -
  5  s=1 u=1 a=4 t=4           5,747   84.4%       274    2,048    138   88.2%   -
 12  s=1 u=1 a=11 t=11         7,579   90.1%       795    2,816     87   78.0%   -
TOTAL  raw_input=18,743  cache_read=9,728  output=1,603  cache_share=34.2%
```

看三个点：

- **`prefix%` 单调上升** → TELOS 把 system/tools/历史对话摆稳了（改动仅发生在尾部）。
- **`cache%` 间歇峰值** → DeepSeek 的 512-token 块边界在跑；连续两三调用跳过同一个边界就会都命中。
- **`TOTAL cache_share`** 是与 `result_5_2.md` 对齐的全局指标。

### 拿 jsonl 自己 jq

```bash
jq -c '{i: .call_index, prefix: .prefix.prefix_stability, cache: .cache.cache_share}' \
    /tmp/telos-telos-runs/telos-pallets__flask-5014.prompt_trace.jsonl
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
    --results-dir /tmp/telos-telos-runs \
    --dataset $TEF/benchmark/datasets/swe-bench-verified.jsonl \
    --filter-agent telos \
    --max-parallel 1 \
    --python-bin $PY \
    --force          # 已有 .eval.json 时强制重测
```

### 2.3 看结果

```bash
cat /tmp/telos-telos-runs/telos-pallets__flask-5014.eval.json | $PY -m json.tool
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

输出对应 `result_5_2.md` 表格的：

| 字段 | 来源 |
|---|---|
| **raw_input / task**   | `raw_input` 求和 |
| **cache_read / task**  | `cache_read` 求和 |
| **input_total / task** | `raw_input + cache_read` |
| **output / task**      | `output` 求和 |
| **resolved**           | `.eval.json` 的 `resolved` 字段 |

---

## 4. 批量跑：`run_swebench_batch`

内置随机采样、并发、自动评测、批次报告。**推荐入口，不用再手写
`xargs`。**

```bash
# \u5e72\u8dd1\uff1a\u770b\u4f1a\u91c7\u6837\u54ea\u4e9b
PYTHONPATH=/Users/george/Code $PY -m telos.scripts.run_swebench_batch \
    -n 5 --seed 42 --dry-run

# 真跑：随机 5 个，4 路并发，跑完直接评测
PYTHONPATH=/Users/george/Code $PY -m telos.scripts.run_swebench_batch \
    -n 5 --seed 42 --workers 4 \
    --model deepseek/deepseek-v4-flash \
    --results-dir /tmp/telos-telos-runs \
    --evaluate

# 只挑某些 repo
PYTHONPATH=/Users/george/Code $PY -m telos.scripts.run_swebench_batch \
    -n 10 --repo pallets/flask --repo psf/requests \
    --workers 2 --evaluate

# 指定具体 instance（覆盖随机采样）
PYTHONPATH=/Users/george/Code $PY -m telos.scripts.run_swebench_batch \
    --instances pallets__flask-5014 django__django-14373 \
    --workers 2 --evaluate
```

### 关键参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `-n N`             | 全集 | 随机采样数 |
| `--seed`           | 42   | 决定可复现性 |
| `--instances ...`  | —    | 显式列出，覆盖随机 |
| `--repo`           | —    | 多次指定可累加（如 `--repo pallets/flask`）|
| `--workers`        | 4    | 并发任务数（OpenRouter 429 时调到 2）|
| `--task-timeout`   | 1800 | 单 instance 子进程硬超时（秒）|
| `--evaluate`       | off  | 跑完批次再起 evaluator |
| `--eval-workers`   | 2    | evaluator 并发 |
| `--force-eval`     | off  | 重测已有 `.eval.json` |
| `--dry-run`        | off  | 只打印采样列表 |

### 输出

- 每个 instance 仍是 §1 的 5 件套 + 评测后的 `.eval.json`
- 每个 instance runner 子进程日志：`logs/telos-<inst>.runner.log`
- **批次报告**写到 `<results-dir>/benchmark/`：
  - `batch-<UTC时间戳>.json` — 带时间戳的归档
  - `latest.json` — 始终指向最新

控制台末尾会打印一张聚合表：resolved 率 + 4 个北极星指标（per-task 平均）+ cache_share。

---

## 4.5 看板：`build_dashboard`

把 `<results-dir>/` 下所有 `prompt_trace.jsonl + result.json + eval.json`
合成一个**单文件 HTML 看板**（零 JS 依赖，可离线打开 / 邮件发送）。

```bash
# 全部 instance
PYTHONPATH=/Users/george/Code $PY -m telos.scripts.build_dashboard \
    --results-dir /tmp/telos-telos-runs

# 只看一个
PYTHONPATH=/Users/george/Code $PY -m telos.scripts.build_dashboard \
    --instance pallets__flask-5014 \
    --out /tmp/dashboard-flask.html

open /tmp/telos-telos-runs/benchmark/dashboard.html
```

### 页面结构

- **顶部 KPI 栏**：instances · resolved 比 · total api calls · raw / cache /
  output 求和 · **overall cache_share**（按 60% / 30% / 0 阈值染绿/黄/橙/灰）
- **每个 instance 一个 card**：
  - 标题：`instance_id` + resolved 徽章 + F2P 比 + completed
  - 6 列指标：calls / raw_in / cache_read / output / cache_share /
    prefix_avg
  - **per-call trace 表**（核心可视化）：
    - 每行 = 一次 API 调用
    - **stacked bar**：红 raw + 绿 cache + 蓝 output；条带总宽按
      instance 内最大输入归一化（一眼看哪次调用量最重）
    - cache% 颜色编码加粗
    - prefix% 数字 + 黄色横条（一眼看 prefix 单调收敛趋势）
    - plan slots（mark 锚位 / routing_key）
- 排序：未 resolved 在前，方便先排查问题

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--results-dir` | `/tmp/telos-telos-runs` | 扫描 `telos-*.prompt_trace.jsonl` |
| `--instance`    | —   | 只包含指定 instance（可重复）|
| `--out`         | `<results-dir>/benchmark/dashboard.html` | 输出 HTML 路径 |

---

## 5. 常见坑

| 现象 | 修法 |
|---|---|
| `OPENROUTER_API_KEY not set` | `export OPENROUTER_API_KEY=...` |
| `ModuleNotFoundError: telos` | `export PYTHONPATH=/Users/george/Code` |
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
| `--results-dir`      | `/tmp/telos-telos-runs` | 输出目录 |
| `--max-iterations`   | 25 | agent 循环上限 |
| `--command-timeout`  | 60 | 单次 bash 超时（秒） |
| `--evaluate`         | off | 任务跑完直接调 evaluator（需先克隆仓库） |
| `--keep-worktree`    | off | 调试：不删 `/tmp/telos-swebench/<tag>` |

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

## 8. 相关文件索引

| 文件 | 作用 |
|---|---|
| [telos/harness/telos.py](../harness/telos.py) | telos harness 插件（OpenAI ChatCompletions → TelosIR） |
| [telos/scripts/telos_transport.py](../scripts/telos_transport.py) | OpenAI 鸭子接口 client，内部走 TELOS Bridge；写 `usage.jsonl` + `prompt_trace.jsonl` |
| [telos/scripts/run_swebench_one.py](../scripts/run_swebench_one.py) | **单任务**端到端 runner |
| [telos/scripts/run_swebench_batch.py](../scripts/run_swebench_batch.py) | **批量** runner（采样 + 并发 + 自动评测 + 批次报告） |
| [telos/scripts/show_prompt_trace.py](../scripts/show_prompt_trace.py) | 命令行查看 `prompt_trace.jsonl` |
| [telos/scripts/build_dashboard.py](../scripts/build_dashboard.py) | 生成单文件 HTML 看板 |
| [telos/tests/test_telos_harness.py](../tests/test_telos_harness.py) | telos 插件 smoke 测试 |
| [token-efficient-framework/benchmark/scripts/evaluate-patches.py][evp] | SWE-bench patch 评测器（无 docker，git worktree 隔离） |

[evp]: file:///Users/george/Code/token-efficient-framework/benchmark/scripts/evaluate-patches.py
