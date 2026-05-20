# TELOS · 开发者大会 Showcase 策划方案

> **受众**：Agent / LLM 应用开发者（在 Anthropic / OpenAI / DeepSeek / vLLM / SGLang 上做生产 Agent 的工程师）
> **核心价值锚点**：① **省美元** — KV-cache 绝对美元节省 ② **跨厂商可移植** — 一份 IR 跑五家
> **展示形式**：主舞台 Live Demo + 展位 Hands-on 体验区
> **核心 take-away**：*Context 是你的——不是租来的。把稳定的钉死，让缓存真的命中你。*

---

## 0. 一页纸——读这一节就够

| 维度 | 内容 |
|---|---|
| **主标语** | **`$0.36 → $0.03` · 同一段对话，省 92.7%。**（按 Claude Opus 4.7 标价估算，源数据 `showcase/usage.jsonl`） |
| **副标语** | One IR, Five Engines. Stop being a tenant in someone else's agent. |
| **展位主视觉** | 三色石碑（PIN / FOLD / DROP）+ 实时美元节省计数器 |
| **展位面积建议** | ≥ 3m × 2m，能摆 2 台演示机 + 1 块大屏 |
| **现场人员** | 2 人轮班（1 名讲解 + 1 名 hands-on 答疑），高峰期补第 3 人 |
| **核心产出物** | 5 个 showcase（A–E）、1 块仪表盘大屏、1 张 cheatsheet 一页纸、1 个 USB 体验包 |
| **断网兜底** | 所有 Live Demo 全程离线可跑（`telos showcase` 自带 replay 数据） |

---

## 1. 五个 Showcase 总览

| ID | 名称 | 时长 | 形式 | 受众密度 | 核心价值 |
|---|---|---|---|---|---|
| **A** | One IR, Five Engines | 5' | 主舞台 Live | 50–200 人 | 跨厂商可移植 |
| **B** | $0.36 → $0.03 钞票计数器 | 3' | 展位大屏 Live + 循环播放 | 持续吸睛 | 省美元 |
| **C** | Break the Stele · 砸石碑 | 5–10' 自助 | 展位 Hands-on | 1–2 人/台 | 协议规范 + 上手感 |
| **D** | Claude Code 5 分钟极速接入 | 5' | 主舞台 Live | 50–200 人 | 零侵入 + 省美元 |
| **E** | Cooperative Fold（进阶） | 3' | 主舞台/小会议室 | 30–50 人 | 双向引擎能力 |

> 主舞台时段建议：**A → D → B 闪电秀 → E**；展位 24h 循环：**B 大屏 + C 自助台**。

---

## 2. Showcase A · One IR, Five Engines

### 2.1 一句话

*同一份 OpenClaw 请求只解析一次，得到一个 `TelosIR`，原封不动喂给 5 个引擎适配器——Anthropic / OpenAI / DeepSeek / vLLM / SGLang，能力差异由适配器**确定性降级**——不是静默丢失，语义不变。*

### 2.2 目标

让台下相信：**今天写在 Anthropic 上的 Agent，明天能搬到 vLLM 自部署不丢东西。**

### 2.3 现场脚本（5 分钟）

| 时点 | 屏幕 | 讲解 |
|---|---|---|
| 0:00 | 打开终端，黑底白字 | "我们做的不是 prompt 框架，是 Agent 上下文的可移植协议。" |
| 0:20 | `python -m telos.demo` 回车 | "一份请求，五个引擎，零修改。" |
| 0:40 | 屏幕显示 `engine = anthropic` 段，指 BP slots | "Anthropic 拿到显式 breakpoint，4 个 BP 全用上。" |
| 1:30 | 翻到 `engine = openai` 段 | "OpenAI 没有 BP，**确定性降级**到 `prompt_cache_key`——同一个 IR 自动 fall through 到这条能力。" |
| 2:30 | 翻到 `engine = deepseek` 段 | "DeepSeek 两者皆无——但 IR 仍然合法，前缀仍然字节稳定，缓存照样命中。" |
| 3:20 | 翻到 `engine = vllm` / `sglang` 段，指 `cache_policy` | "自部署引擎能享受到比闭源 API 更多的能力：probe / fold / cache hierarchy。" |
| 4:00 | 高亮 `cooperative_fold` 输出 | "vLLM/SGLang 上前缀 KV 不重算——只重算 summary tail。这是闭源 API 做不到的。" |
| 4:40 | 收尾 | "**Context 是你的资产，不是租来的。**" |

### 2.4 关键数字

- **1 份 IR** → **5 个引擎** → **0 行 prompt 改写**
- 能力对比矩阵（截图自 README）：BP / prewarm / routing_key / probe / fold 五行五列

### 2.5 风险与兜底

- **断网风险**：demo.py 全程纯本地，无任何 API 调用 ✅
- **输出过长翻车**：终端字号调到 18pt，预先 `python -m telos.demo > /tmp/demo.out` 备份，可滚动展示
- **Q&A 备料**：R1–R8（见 §7），尤其 R1（OpenAI 配额）和 R2（中段锚点）

---

## 3. Showcase B · `$0.36 → $0.03` 钞票计数器

### 3.1 一句话

*同一段 6 轮对话，4 种开关组合各跑一次——字节完全相同的请求序列，唯一变量是开关本身。none 烧 $0.36，both 只花 $0.03。**省下的是绝对美元，比率可以靠缩小分母作弊，绝对美元不行。***

### 3.2 形式

**展位大屏循环 + 主舞台 3 分钟闪电秀**。大屏内容：

```
┌─────────────────────────────────────────────────────────┐
│  TELOS · Live Savings Counter                            │
│  ───────────────────────────────────────────────────     │
│  Mode    raw_in   cache_read   est. cost    saved        │
│  none    24,151        0       $0.3623     baseline     │
│  rtk     22,841        0       $0.3426     -5.4%        │
│  telos        0   18,701       $0.0281     -92.3%       │
│  both         0   17,719       $0.0266     -92.7%       │
│  ───────────────────────────────────────────────────     │
│  💸  Saved per 6-turn session:   $0.336                 │
│  💸  Saved per 1k sessions:      $336                   │
└─────────────────────────────────────────────────────────┘
                  (numbers refresh every 30s from live replay)
```

### 3.3 现场命令（3 分钟）

```bash
# 走真实采集版（已含在仓库 showcase/replay_responses.json 里，标记 source=real）
telos showcase --pace 1.5

# 或者展位上一直跑（自动循环）：
while true; do telos showcase --pace 2 --quiet; sleep 30; done

# 或者更"工程师向"——直接打开 dashboard：
open showcase/dashboard.html
```

### 3.4 讲解节奏

| 时点 | 讲什么 |
|---|---|
| 0:00–0:30 | "怎么证明省钱？不是估算——录真实会话，回放 4 次，字节完全相同。" |
| 0:30–1:30 | 屏幕滚动 4 种模式 usage 输出，指 `cache_read` 从 0 涨到 17,719 |
| 1:30–2:30 | 打开 dashboard.html，"saved $" 榜上四种模式并排，指着 \$0.336 那行 |
| 2:30–3:00 | "Replay 是受控实验——CI 友好、可复现。比率不能作弊，绝对美元不能。" |

### 3.5 真实数字来源（避免被质疑）

数字来自 `showcase/usage.jsonl`，6 轮真实采集的 Anthropic 上报值聚合：

| Mode | raw_input | cache_read | 估算 \$（$15/M input + $1.5/M cache_read） |
|---|---|---|---|
| none | 24,151 | 0 | **$0.3623** |
| rtk | 22,841 | 0 | $0.3426 |
| telos | 0 | 18,701 | $0.0281 |
| both | 0 | 17,719 | **$0.0266** |

> 数据来自 `showcase/usage.jsonl`（6 轮真实采集，按 mode 字段聚合）。
> **省 $0.336 / 6 轮会话 · $336 / 1k 会话 · -92.7%**。彩排前如重新采集，记得重算这张表。

> 旁白时必须说明：「按 Anthropic 公开标价估算，**实际账单可能略有差异**。dashboard 显示的是 token 而非美元——美元数字由公开 pricing × token 推算。」

### 3.6 风险与兜底

- **被问"这数字真假"**：现场打开 `showcase/replay_responses.json`，指 `_meta.source = "real"`
- **被问"为什么 rtk 单跑没省钱"**：rtk 只压 tool_result 体积，不稳前缀；前缀仍然每轮重算 → 见 playbook §5
- **大屏挂掉**：备 1 份 PDF / 单文件 HTML，U 盘里随时拷出来打开

---

## 4. Showcase C · Break the Stele · 砸石碑（Hands-on 体验区）

### 4.1 一句话

*石碑只有一条硬规则：每段内必须 `PIN → FOLD → DROP`。你来试试——把 FOLD 排到 PIN 前面，看协议怎么咬你。*

### 4.2 形式

**展位 2 台演示机**（MacBook Air 即可），每台贴二维码 + 一页 cheatsheet。游客随时上手。

### 4.3 主导动线（5–10 分钟自助）

引导话术（贴在屏幕侧边）：

```
┌─────────────────────────────────────┐
│  3 分钟试试看：                       │
│                                     │
│  ① 终端里输入：telos showcase --interactive
│  ② 选 [3] —— 故意把 FOLD 排到 PIN 前
│     看协议怎么抛 TelosInvariantError
│  ③ 选 [2] —— 改 expected_turns（2/20/60）
│     看 mark plan 怎么自适应
│  ④ 选 [4] —— 4 模式 replay A/B
│  ⑤ 选 [5] —— 生成并打开 dashboard
│                                     │
│  📛 不会卡，全程离线。                │
└─────────────────────────────────────┘
```

### 4.4 旁站讲解关键点（每个 hands-on 玩家平均 2 分钟）

- **菜单 [3] 是最直观的**：协议只有一条硬约束，违反会精确告诉你哪个 block 错了
- **菜单 [2] 展示自适应**：expected_turns=2 时 mark plan 只放 2 个锚点，60 时启用中段滚动锚点（R2）
- **菜单 [4] 是 B 的小屏版**：玩家自己看到 4 种模式数字差异

### 4.5 物料清单

- [ ] 2 台演示机，仓库已 `pip install -e .`，已断网验证
- [ ] 屏幕侧贴 cheatsheet（4.3 那个框）
- [ ] 桌上摆 USB 体验包（含仓库 zip + cheatsheet PDF + 视频链接二维码）
- [ ] 二维码：① GitHub 仓库 ② Quickstart 文档 ③ 留资入口
- [ ] 备一份 asciinema cast `showcase/demo.cast`，演示机离线也能放

### 4.6 KPI

- 上手人数：≥ 30 / 天
- 平均停留：≥ 4 分钟
- 留资率：≥ 25%

---

## 5. Showcase D · Claude Code 5 分钟极速接入

### 5.1 一句话

*npm-global 装的 Claude Code，**不改一行代码**，5 分钟内让它走 TELOS 缓存——再看 dashboard 上 cache_read 实时上涨。*

### 5.2 目标

把"省钱"从抽象数字变成"我现在就能装"的可操作演示。受众是手里正在用 Claude Code / Cursor / Gemini CLI 的人。

### 5.3 现场脚本（5 分钟）

| 时点 | 命令 / 屏幕 | 讲解 |
|---|---|---|
| 0:00 | 打开两个终端 | "左边装，右边看缓存。" |
| 0:20 | `telos gateway start --usage-log ~/.telos/usage.jsonl` | "起一个 127.0.0.1 上的反向代理。" |
| 0:50 | `telos init --agent claude-code` | "一行命令，patch `~/.claude/settings.json` 的 env 字段——**不改 npm 包，npm update 不丢配置。**" |
| 1:30 | `telos init --agent claude-code --status` | "确认状态：✓ enabled。" |
| 2:00 | 左边：`claude` 启动一段编码任务（预录或现场） | "用法完全不变。" |
| 3:00 | 右边：`jq -c '{call: .call_index, cache_read: .normalized.cache_read}' < ~/.telos/usage.jsonl` | "看 cache_read 一行行涨。第 4 轮起每轮命中 6000+ token。" |
| 4:00 | 浏览器打开 `http://127.0.0.1:7171/__telos/dashboard` | "实时 dashboard，PIN/FOLD/DROP 分布一目了然。" |
| 4:40 | `telos init --agent claude-code --uninstall` | "随时可以精确卸载，恢复到装前的样子。" |

### 5.4 关键数字（现场可见）

- **接入耗时**：≤ 60 秒（1 行 init 命令）
- **侵入面**：仅 `~/.claude/settings.json` 的 `env` 字段
- **可逆性**：`--uninstall` 精确还原

### 5.5 风险与兜底

- **现场网络不可用**：预录一段 30 秒视频 "claude 跑 6 轮 → cache_read 上涨"，无网时直接放
- **`claude` 真要请求 Anthropic**：用一个低成本短 prompt（"列出当前目录"）；账单可控
- **被问"代理挂掉怎么办"**：non-strict 模式（默认）会自动 passthrough，agent 不受影响

---

## 6. Showcase E · Cooperative Fold（进阶专场）

### 6.1 一句话

*闭源 API 的 fold 是客户端改写，服务器每次都得 re-prefill 整段；vLLM/SGLang 上 `cooperative_fold` 让服务器**保留前缀 KV 不动**，只重算 summary tail。*

### 6.2 受众

vLLM / SGLang 用户、做推理基础设施的工程师。预期听众体量小（30–50 人），但精准。

### 6.3 现场脚本（3 分钟）

```bash
# 终端里跑 demo.py 的 vLLM / SGLang 部分
python -c "
from telos import Bridge, load_engine, load_harness
from telos.demo import RAW_REQUEST

ir = load_harness('openclaw').parse(RAW_REQUEST, session_id='fold-demo', engine='sglang', model='deepseek-ai/DeepSeek-V3', expected_turns=20)
b = Bridge(ir, load_engine('sglang'))

# 1) probe
probe = b.probe_cache()
print(f'probe → hit={probe.hit} cached={probe.cached_token_count} tier={probe.tier}')

# 2) cooperative_fold
ctrl = b.cooperative_fold(message_range=(1,3), summary='<prev turns folded>')
print(f'server-side cache_control fragment:')
import json; print(json.dumps(ctrl, indent=2))
"
```

### 6.4 讲解节奏

- **0:00** "闭源 API 让你做 fold——但服务器并不知道你 fold 了。下一轮请求里那段前缀对服务器是新内容，KV 全部重算。"
- **0:40** 屏幕：`probe_cache` 输出，"先看服务器侧缓存有什么，再决定怎么改"
- **1:30** 屏幕：`cooperative_fold` 输出，指 `cache_policy`/`cache_control` 片段，"客户端告诉服务器：前缀别动，只把这一段折叠"
- **2:30** 收尾："这是**双向引擎能力**——只有自部署引擎才有。**自部署引擎用 TELOS 是高配，不是降级。**"

### 6.5 何时跑

- 选项 1：作为 A 的延伸专场（A 跑完邀请有兴趣的人去小会议室）
- 选项 2：单独安排在第二天下午（推理工程师专场时段）

---

## 7. Q&A 备料 · 协议风险 R1–R8

>  全部已在 `docs/showcase-runbook.md §5` 落地。现场讲解员每人备一份打印版。

| ID | 工程师可能问 | 一句话回答 | 代码出处 |
|---|---|---|---|
| R1 | OpenAI `prompt_cache_key` 不是要 ≥15 RPM 才扩槽？低流量怎么办？ | 我们设 `KEY_RPM_SOFT_CAP=12`，自动 shard | `engine/openai.py` |
| R2 | Anthropic 只 4 个 BP，几十轮怎么覆盖？ | 中段滚动锚点 `_MID_ANCHOR_STRIDE=19`，自适应 | `engine/anthropic.py` |
| R3 | 子 Agent 和父 Agent 共用 session_id 会串吗？ | 子 IR 独立解析（hermes） | `harness/hermes.py` |
| R4 | fold 之后 mark 槽落进被折叠区怎么办？ | `fold()` 后强制重跑 `mark()` | `bridge.py` |
| R5 | 工具字段顺序不稳定会不会破坏缓存？ | `_canonicalize_ir()` 把 key/tool 排序固化 | `bridge.py` |
| R6 | thinking block 跨非 tool_result 调用会丢吗？ | 适配器有 `thinking_preserved_across_non_tool_result` 标志 | `engine/base.py` |
| R7 | BP 候选超过 4 个时优先保留谁？ | `plan_marks` 优先级 + 截断 | `engine/anthropic.py` |
| R8 | refresh 会不会反向把配额打满？ | `REFRESH_THRESHOLD=11`，自适应门控 | `bridge.py` |

**两个最常被问、但答案不在 R1–R8 的**：

- **"replay 能测端到端任务成本吗？"** → 不能，replay 钉死轨迹，测的是"同一对话不同编码的成本"。要测端到端用 dual session（见 `docs/replay-comparison.md §4`）。
- **"数字是真的吗？"** → 翻 `showcase/replay_responses.json`，`_meta.source` 是 `real` 或 `synthetic`。今天我们用的是 `real`。

---

## 8. 现场动线（建议）

```
                    主舞台 (每天 2 场 25 分钟主题秀)
                     ┌─────────────────────────┐
                     │   25min Keynote          │
                     │   = A (5) + D (5) + B    │
                     │     闪电秀 (3) + E (5)   │
                     │     + Q&A (7)            │
                     └─────────────────────────┘
                              │
              (会后引流) ↓
                     ┌─────────────────────────┐
                     │  TELOS 展位 3m × 2m       │
                     │                          │
                     │  [大屏 · B 钞票计数器]    │ ← 持续吸睛
                     │  [桌 · C Hands-on 1]      │ ← 工程师上手
                     │  [桌 · C Hands-on 2]      │
                     │  [立牌 · 三色石碑视觉]    │
                     │  [桌脚 · USB 体验包堆]    │
                     └─────────────────────────┘
```

### 8.1 人员排班

| 角色 | 数量 | 职责 |
|---|---|---|
| 主舞台讲解 | 1 | A / D / B / E 主讲，备 R1–R8 |
| 展位讲解 | 1 | B 大屏旁讲解 + 引流到 Hands-on |
| Hands-on 答疑 | 1 | C 旁站陪玩、解答现场问题 |
| 救火 / 录制 | 1（高峰期） | 处理设备故障、记录精彩问答 |

### 8.2 一天的节奏

| 时段 | 主舞台 | 展位 |
|---|---|---|
| 09:00–10:00 | — | C 自助开放 + B 大屏循环 |
| 10:00–10:25 | **主题秀 1**（A+D+B+E） | — |
| 10:25–12:00 | — | 高峰期：3 人轮班 |
| 12:00–13:30 | 午休 | B 大屏继续循环（无人值守 OK） |
| 13:30–14:00 | — | C 自助开放 |
| 14:00–14:25 | **主题秀 2** | — |
| 14:25–18:00 | — | 高峰期 + 收尾 |

---

## 9. 物料清单（采购 / 准备）

### 9.1 硬件

- [ ] 2 台演示笔记本（macOS / Linux 均可），16GB+ 内存
- [ ] 1 块 ≥ 32" 大屏 + HDMI 转接头
- [ ] 投屏遥控笔 ×2
- [ ] 充电插线板 ×2
- [ ] 备用电源 / UPS（防展会临时停电）

### 9.2 软件 / 数据

- [ ] 仓库 `pip install -e .` 在每台演示机上验证可跑
- [ ] `telos showcase` / `telos showcase --interactive` / `telos showcase --cast` 三种入口都跑通
- [ ] `showcase/replay_responses.json` 已是 `_meta.source = "real"` 版本
- [ ] `showcase/dashboard.html` 双击离线可开
- [ ] 备一份 `python -m telos.demo` 的 ASCII 文本输出（万一终端翻车滚动展示）
- [ ] 录一段 30s 视频 "claude + telos gateway → cache_read 上涨"（Showcase D 兜底）

### 9.3 印刷品

- [ ] **一页纸 cheatsheet**（A4 双面）—— 正面：三色石碑视觉 + 30s quickstart；反面：5 个 showcase 缩略图 + 二维码
- [ ] **立牌主视觉**：三色石碑 + 主标语 `$0.36 → $0.03 · 同一段对话省 92.7%`
- [ ] **桌签**：每台 Hands-on 机的引导话术（§4.3）
- [ ] **贴纸**：TELOS 三色 logo（PIN-蓝 / FOLD-黄 / DROP-红），≥ 500 张

### 9.4 数字物料

- [ ] USB 体验包（仓库 zip + cheatsheet PDF + 视频）×100
- [ ] 二维码：① GitHub 仓库 ② Quickstart 文档 ③ 留资问卷 ④ 视频回放
- [ ] 收尾邮件模板（48 小时内发给留资观众）

---

## 10. 现场断网彩排清单（出发前 24 小时必须过完）

> 直接照搬 `docs/showcase-runbook.md §4`，加一条 dashboard 大屏检查。

- [ ] **关 Wi-Fi**，`telos showcase` 完整跑通，最后 dashboard 在浏览器打开
- [ ] **关 Wi-Fi**，`telos showcase --interactive` 五个菜单项逐一点过
- [ ] `showcase/replay_responses.json` 已是真实采集版本（`_meta.source == "real"`）
- [ ] `showcase/dashboard.html` 双击离线可开，"saved \$" 榜显示 4 个模式会话
- [ ] cast 文件 `asciinema play showcase/demo.cast` 回放正常
- [ ] 演示机已 `pip install -e .`（editable），`telos` 命令可用
- [ ] **大屏循环**：`while true; do telos showcase --pace 2 --quiet; sleep 30; done` 跑 ≥ 30 分钟不崩
- [ ] **Showcase D 视频兜底**：MP4 在 USB 里可放
- [ ] **PPT 兜底**：万一所有终端都翻车，有一份 PDF 能讲完故事

---

## 11. 成功指标 KPI

| 维度 | 指标 | 目标值 |
|---|---|---|
| **触达** | 主舞台听众累计 | ≥ 300 人 |
| **触达** | 展位停留 >30s 人数 | ≥ 200 / 天 |
| **上手** | Hands-on 完整玩完 5 步的人 | ≥ 30 / 天 |
| **留资** | 留邮箱 / GitHub 关注 | ≥ 100 |
| **质量** | 现场 GitHub star 涨幅 | ≥ +200 |
| **质量** | 现场加入 Discord / 群 | ≥ 50 |
| **媒体** | 听众主动发 X / 朋友圈截图 | ≥ 10 条 |

### 11.1 现场实时打卡

`telos showcase` 跑一次后，dashboard.html 末尾计数器 +1，展位墙上贴一个"今日已展示 N 次"的小白板。

---

## 12. 风险登记册

| 风险 | 概率 | 影响 | 兜底 |
|---|---|---|---|
| 展会 Wi-Fi 不稳 | 高 | 中 | 全部 demo 离线可跑 ✓ |
| Anthropic API 限流 / 不通 | 中 | 高（仅 D） | D 准备 30s 视频兜底 |
| 演示机硬件故障 | 低 | 高 | 备 1 台冷备机，仓库 zip 在 USB 里 |
| 大屏 HDMI 不识别 | 中 | 中 | 备 3 种转接头 + 1 条 USB-C → HDMI 长线 |
| 现场被深度技术追问 | 高 | 低（机会） | R1–R8 备料 + 邀请去小会议室深聊（E） |
| 数字被质疑造假 | 低 | 高 | 当场打开 `replay_responses.json` 的 `_meta.source = "real"` |
| 主讲嗓子哑 | 中 | 中 | 备一份预录视频（A 的 5 分钟版） |

---

## 13. 一页纸 Cheatsheet（背面印这个，正面印三色石碑视觉）

```
┌───────────────────────────────────────────────────────┐
│  TELOS — Portable Agent Context                       │
│  ─────────────────────────────────────────────────    │
│  ONE problem  你的 Agent 第 20 轮和第 19 轮 95% 字节相同│
│  ONE rule     每段内 PIN → FOLD → DROP                │
│  ONE IR       跑 Anthropic / OpenAI / DeepSeek /      │
│               vLLM / SGLang —— 5 家不改一行 prompt    │
│                                                       │
│  30s quickstart:                                      │
│    pip install -e .                                   │
│    telos gateway start                                │
│    telos init --agent claude-code                     │
│    claude   # 用法不变，缓存自动命中                   │
│                                                       │
│  see for yourself:                                    │
│    telos showcase                  # 5 分钟预录       │
│    telos showcase --interactive    # 你来动手         │
│    telos replay --session <id>     # 4 模式 A/B       │
│                                                       │
│  ───────────────────────────────────────────────────  │
│  ▓▓▓ PIN   tool / system / 当前问题（前缀稳定）       │
│  ▒▒▒ FOLD  history / tool_result / 大文档（可折叠）    │
│  ░░░ DROP  timestamp / cwd / env（每轮重生）          │
│  ───────────────────────────────────────────────────  │
│  GitHub: github.com/<…>/telos-sdk                     │
│  Docs:   docs/playbook.md / docs/User-guide.md        │
└───────────────────────────────────────────────────────┘
```

---

## 13.5 数字复现（被质疑时当场敲）

```bash
cd telos-sdk
python3 - <<'PY'
import json, collections
agg = collections.defaultdict(lambda: {"cache_read":0,"raw_input":0,"calls":0})
for line in open("showcase/usage.jsonl"):
    r = json.loads(line); m = r["mode"]; n = r["normalized"]
    agg[m]["cache_read"] += n["cache_read"]; agg[m]["raw_input"] += n["raw_input"]; agg[m]["calls"] += 1
print(f"{'mode':6s} {'calls':>5s} {'raw_in':>8s} {'cache_read':>11s} {'est_USD':>10s}")
for m in ("none","rtk","telos","both"):
    v = agg[m]; est = (v["raw_input"]*15 + v["cache_read"]*1.5) / 1_000_000
    print(f"{m:6s} {v['calls']:>5d} {v['raw_input']:>8d} {v['cache_read']:>11d}  ${est:>8.4f}")
PY
```

预期输出：

```
mode   calls   raw_in  cache_read    est_USD
none       6    24151           0  $  0.3623
rtk        6    22841           0  $  0.3426
telos      6        0       18701  $  0.0281
both       6        0       17719  $  0.0266
```

源数据合法性：`head -c 200 showcase/replay_responses.json` 应看到 `"_meta": {"source": "real", ...}`。

---

## 14. 行动清单（T-7 → T-0）

| T- | 责任人 | 事项 |
|---|---|---|
| T-7 天 | 主讲 | A / D 现场脚本背 3 遍，录一遍视频自查 |
| T-7 天 | 设计 | 立牌 + cheatsheet + 二维码物料定稿送印 |
| T-5 天 | 工程 | 演示机重装系统 + `pip install -e .` + 离线彩排 |
| T-5 天 | 工程 | `demo_capture` 联网采集真实 replay 数字（如已过期） |
| T-3 天 | 主讲 | 主题秀（A+D+B+E）整段彩排 ×2 |
| T-2 天 | 全员 | 断网彩排清单（§10）跑一遍 |
| T-1 天 | 全员 | USB 体验包 100 个全部烧好 |
| T-0 早 | 全员 | 现场布展 + HDMI 测试 + 大屏 30 分钟稳定性测试 |
| T+1 | 主讲 | 留资邮件发出（含视频回放 + GitHub 链接） |

---

<div align="center">
<sub>—— TELOS —— hold the stable parts stable, drive the unstable parts to the tail ——</sub>
</div>
