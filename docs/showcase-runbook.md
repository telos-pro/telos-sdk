# TELOS Show Case — 对外展示运行手册

> 面向工程团队 / 潜在用户的 15–20 分钟展示。两个交付物都由
> `telos showcase` 这一个命令驱动，**全程离线**，不依赖 API key 与网络。

---

## 0. 两个交付物

| 交付物 | 命令 | 用途 |
|---|---|---|
| 5 分钟预录 demo | `telos showcase` | 节奏化自动运行，屏幕录制成视频 |
| 现场交互环境 | `telos showcase --interactive` | 菜单 playground，工程师自己动手 |
| asciinema cast | `telos showcase --cast showcase/demo.cast` | 产出可回放的终端录像，无需屏录软件 |

四个 scene 两种模式共用：① 一份 IR 五个引擎 ② 唯一一条不变式 ③ replay A/B ④ 看得见的成本。

---

## 1. 准备阶段（务必提前做，需联网一次）

```bash
# 在仓库根目录，用项目 venv
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python -m telos.scripts.demo_capture        # 真实采集 4 模式 replay 数字
```

这会把 Anthropic 真实上报的 per-turn usage 写进 `showcase/replay_responses.json`
（带 `_meta.source = "real"`）。之后 scene 3 的旁白就能说「Anthropic 真实上报」。

> 无 API 访问时的兜底：`demo_capture --synthetic` —— 用确定性估算器生成，
> scene 3 旁白会自动显示「deterministic synthetic estimate」，此时旁白须明说是估算值。
> 仓库默认已带一份 synthetic 版本，保证开箱即跑。

彩排（断网下走一遍）：

```bash
telos showcase --pace 1.5            # 完整预录流程
telos showcase --interactive         # 交互流程，逐项点一遍
telos showcase --cast showcase/demo.cast --pace 1.5   # 产出 cast
asciinema play showcase/demo.cast    # 确认 cast 可回放
```

录制成视频：用 QuickTime「新建屏幕录制」框选终端窗口，运行
`telos showcase --pace 3`（或 `--step` 手动按键对旁白），录完导出 MP4。

---

## 2. 逐 Scene 旁白脚本（约 5 分钟）

### 开场（15s）
> 「TELOS 是一个**可移植的 Agent 上下文协议**。一句话：上下文是 Agent 栈里唯一持久的资产，
> TELOS 把它变成一个引擎无关、可序列化、可携带的标准表示。下面 4 个场景，全程离线。」

### Scene 1 — 一份 IR，五个引擎（~75s）
> 「一份 OpenClaw 风格的请求，**只解析一次**得到一个 `TelosIR`。同一个 IR 交给 5 个引擎适配器——
> Anthropic、OpenAI、DeepSeek、vLLM、SGLang。看 band 布局和 mark plan：
> Anthropic 拿到显式 breakpoint，OpenAI 没有 BP、降级到一个 routing_key，DeepSeek 两者皆无。
> **关键点：能力差异由适配器确定性降级——不是静默丢失，语义不变。** 这就是可移植的上下文。」

### Scene 2 — 唯一一条不变式（~60s）
> 「整个协议只有**一条**硬约束：每个段内 block 必须物理有序 `PIN → FOLD → DROP`。
> 合法的 IR 通过；把 FOLD 排到 PIN 前面，`assert_ir_invariants` 当场抛 `TelosInvariantError`，
> 错误信息精确指出是哪个 block。其他一切都是软建议——只有这一条会咬人。」

### Scene 3 — replay A/B（~75s）
> 「怎么证明省钱？录一个真实的 12 轮 Agent 会话，把**字节完全相同**的请求序列在 4 种模式下
> 各重放一次：none / rtk / telos / both。唯一变量就是开关本身——这是受控实验，不是跑两次取差值的带噪对比。
> 看表：passthrough 模式 cache hit 为 0，每一轮都把整个前缀重新 prefill；
> 开 TELOS 后 **~90% 的 prompt token 直接从缓存读出**，token 成本砍掉 **~78%**；
> 再叠加 RTK 裁剪工具输出 → **~82%**。省下的是**绝对美元**——比率可以靠缩小分母作弊，绝对美元不行。」
> （若是 synthetic：「这里的数字是确定性估算，准备阶段联网采集后会换成 Anthropic 真实上报值——
> 真实数字通常更高，因为真实 Claude Code 的 system prompt 更大、会话更长。」）

### Scene 4 — 看得见的成本（~60s）
> 「每次调用的归一化 usage 落进 jsonl，聚合成一个**单文件 HTML** 仪表盘——内联 SVG+CSS、
> 零 JavaScript、断网也能打开。token 构成、绝对美元省下多少、按 harness/模型/会话拆分。
> 4 种 replay 模式会作为 4 个会话出现在『saved $』榜上——none / rtk / telos / both 的
> 对比直接在页面上读出来。」

### 收尾（20s）
> 「可移植的上下文 → 看得见的省钱 → 你真正握着控制器。一个任务可以雇任何 harness、
> 任何模型，不被关进笼子。harness 只是雇来的帮手，上下文这块石板，是你的。」

---

## 3. 交互环境（现场让工程师动手）

`telos showcase --interactive` 菜单：

- `[1]` 选引擎 → 看该引擎对样例 IR 的 band 布局 + mark plan
- `[2]` 改 `expected_turns`（试 2 / 20 / 60）→ 看 mark plan 怎么随之变化（中段锚点开关，R2）
- `[3]` 自选 band 顺序 → 看不变式接受 / 抛 `TelosInvariantError`
- `[4]` 跑 4 模式 replay A/B
- `[5]` 生成并打开 dashboard
- `[q]` 退出

建议引导工程师先点 `[3]` 故意选个非法顺序，最直观；再点 `[2]` 看 mark plan 变化。

---

## 4. 断网彩排清单

- [ ] 关闭 Wi-Fi，`telos showcase` 完整跑通，最后 dashboard 在浏览器打开
- [ ] `telos showcase --interactive` 五个菜单项逐一点过
- [ ] `showcase/replay_responses.json` 已是真实采集版本（`_meta.source == "real"`）
- [ ] `showcase/dashboard.html` 双击能离线打开，「saved $」榜显示 4 个模式会话
- [ ] cast 文件 `asciinema play` 回放正常
- [ ] 演示机已 `pip install -e .`（editable），`telos showcase` 命令可用

---

## 5. Q&A 备料 — R1–R8 协议风险

工程师最可能追问「某某情况下会不会出问题」。README 附录的 R1–R8 是现成答案：

| ID | 工程师可能问 | 答案位置 |
|---|---|---|
| R1 | OpenAI 的 `prompt_cache_key` 不是要 ≥15 RPM 才扩槽？ | `engine/openai.py :: KEY_RPM_SOFT_CAP=12` + `shard()` |
| R2 | Anthropic 只有 4 个 BP，中间几十轮怎么覆盖？ | `engine/anthropic.py :: _MID_ANCHOR_STRIDE=19` 中段滚动锚点 |
| R3 | 子 Agent 和父 Agent 共用 session_id 会串吗？ | `harness/hermes.py` 子 IR 独立解析 |
| R4 | fold 之后 mark 槽落进被折叠区怎么办？ | `bridge.py :: fold()` 后强制重跑 `mark()` |
| R5 | 工具字段顺序不稳定会不会破坏缓存？ | `bridge.py :: _canonicalize_ir()` |
| R6 | thinking block 跨非 tool_result 调用会丢吗？ | `engine/base.py :: thinking_preserved_across_non_tool_result` |
| R7 | BP 候选超过 4 个时优先保留谁？ | `engine/anthropic.py :: plan_marks` 优先级 + 截断 |
| R8 | refresh 会不会反向把配额打满？ | `bridge.py :: REFRESH_THRESHOLD=11` 自适应门控 |

其他常见问题：
- **「replay 能测端到端任务成本吗？」** 不能——replay 钉死轨迹，测的是「同一段对话不同编码的成本」，
  不含二阶效应（RTK 缩短 tool_result 后 Agent 下一步可能换决策）。要测端到端须跑独立会话。见 `docs/replay-comparison.md §4`。
- **「数字是真的吗？」** scene 3 旁白会显示来源；真实采集时是 Anthropic 上报的 `usage` 原值。

---

## 6. 推荐 15–20 分钟整体流程

| 段落 | 时长 | 内容 |
|---|---|---|
| 开场 + 架构图 | 2' | harness → Bridge → engine adapter → LLM |
| 播放 5 分钟预录 demo | 5' | `telos showcase` 录制视频 / 现场跑 |
| 协议深挖 | 3' | 现场打开 `ir.py` / `bridge.py`，讲不变式与 5 原语 |
| 现场交互 | 5' | 邀请工程师上手 `telos showcase --interactive` |
| Q&A | 3–5' | R1–R8 备料 |
