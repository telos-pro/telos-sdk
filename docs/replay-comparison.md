# Replay 对照 —— 录制 / 重放

> 把一个真实会话录下来，按多种开关组合各重放一遍，得到**受控**的成本对照。

STELA 提供两种对照方式。本文讲 replay；另一种「双 session」见文末对比。

---

## 1. 为什么要 replay

要回答「开 STELA / RTK 到底省多少钱」，最直觉的做法是同一个任务跑两遍、
换不同开关——但两次跑出来的 agent 轨迹会分叉（采样随机性、工具结果不同
导致后续决策不同），成本差里混进了与优化无关的噪声。样本量 1 时，这个
delta 基本不可信。

replay 把**轨迹钉死**：录一次真实会话，得到一串「请求序列」；之后每个
mode 都重放**逐字节相同**的这串请求。唯一的变量就是开关本身——这是受控
实验，对照数字干净、可复现、可进 CI。

代价也低：一次完整真实会话 + 每个 mode 一串廉价 prefill 调用，比「N 个
mode 各跑一整个 agent 会话 × K 次取平均」便宜一两个数量级。

---

## 2. 原理

### 2.1 只录请求，不录响应

Anthropic `/v1/messages` 是无状态的：第 N 轮的请求体里，`messages[]` 已经
包含了前 N−1 轮的全部 assistant 回复和 tool_result。所以「请求序列」本身
就是完整可重放的轨迹，assistant 响应不必单独存（也避免把模型输出落盘）。

proxy 默认把每次调用的**原始请求**（client→proxy，RTK 过滤前、STELA 改写
前）录进语料库 `~/.stela/corpus/<session>.jsonl`。录的是「规范输入」——
replay 时每个 mode 各自从同一份规范输入重新推导 wire，对照才公平。

> RTK 的过滤只发生在 proxy→upstream 这一段，不改 agent 的本地对话状态。
> 所以 agent 下一轮仍会发来未过滤的完整 tool_result——语料库里录到的
> 永远是未过滤的规范输入。

### 2.2 重放只测 prefill 成本

`stela replay` 对每个 mode：

1. 取语料里每一轮的原始请求；
2. （可选）注入缓存隔离前缀（见 2.3）；
3. `mode.rtk` 开 → 跑 RTK 工具结果过滤；
4. `mode.stela` 开 → 跑 STELA 管线打 cache_control / ref-pool；
5. 把 `max_tokens` 强制设成 `1`，去掉 `stream` / `tool_choice` / `thinking`，
   发到上游；
6. 只取响应的 `usage`，写一条 usage_log 记录。

强制 `max_tokens=1` 是因为我们只关心 prompt / prefill 侧的
`cache_read` / `cache_write` 计费——输出生成被刻意阉割，几乎不产生 output
成本。拿到的是 Anthropic **实报**的缓存数字，不是估算值。

### 2.3 跨 mode 缓存隔离

Anthropic 的前缀缓存按「前缀内容 + 组织」keyed，没有路由 key。如果先重放
`stela`、再重放 `both`，而两者的可缓存前缀恰好一致，后者会白蹭前者暖好的
缓存，对照数字就被污染了。

默认对策：给每个 mode 在 `system` 段最前面注入一个唯一前缀块
`[stela-replay ns=<session>/<mode>]`。各 mode 前缀因此互不相同 → 缓存各自
独立。这个块只有 ~10 个 token、各 mode 等长，不影响相对对照。

`--no-cache-isolation` 可关闭注入。

---

## 3. 用法

```bash
# 1. 跑几个真实会话（proxy 默认就会录进语料库）
stela proxy --usage-log ~/.stela/usage.jsonl
#    ... 用 agent 干活 ...

# 2. 看语料库里有哪些会话
stela replay --list

# 3. 重放：默认 4 个 mode 全跑
stela replay --session stela-ab12cd34
#    或挑 mode：
stela replay --session stela-ab12cd34 --modes none,both

# 4. 看对比（dashboard「A/B 对比」面板，卡片标 `replay` 徽章）
stela dashboard --usage-log ~/.stela/usage.jsonl --out savings.html
```

重放需要 `ANTHROPIC_API_KEY`（或 `--api-key`）。结果 append 到 `--usage-log`
（默认 `~/.stela/usage.jsonl`），`compare_group` = 原会话 id，dashboard 据此
把同会话的各 mode 并排展示。

---

## 4. 边界 —— replay 测不到什么

replay 是受控实验，受控的代价是它**只在轨迹固定时成立**：

- **测的是「同一段对话在不同编码下的成本」，不是「同一个任务在不同配置
  下的成本」。** 它捕捉不到二阶效应——比如 RTK 缩短了工具结果后，真实
  运行里 agent 的下一步可能因为上下文不同而做出不同（更好或更坏）的决策，
  进而改变后续轮数和总成本。replay 把这条岔路堵死了。
- **测的是 prefill / 缓存计费，不是端到端任务成本。** `max_tokens=1`
  意味着 output 成本不计入；真实任务的 output 开销要另算。
- **缓存隔离前缀是一个刻意引入的人为产物。** 它对相对对照无害（各 mode
  等长），但绝对 token 数会比真实多出那 ~10 token/轮。
- **替代不了真实运行。** 要证明「用 STELA 后 agent 整体更便宜」这种端到端
  论断，只能跑独立 session。

把 replay 当成「现有 dashboard 那个*计算出来*的『不开 STELA』反事实」的
升级版——把它换成*实测出来*的反事实。要测机制（缓存标记 + 过滤是否降低
计费 token），这正好是对的范围；要测任务结果，用下面的双 session。

---

## 5. 对比：replay vs 双 session

| | 成本 | 控制变量 | 适合论断 |
|---|---|---|---|
| **replay** | 1 次真实会话 + 廉价 prefill | 好（轮次钉死） | 「对给定工作负载，token 账单降 X」 |
| **双 session** | N×K 个完整 agent 会话 | 差（trajectory 分叉） | 「用了 STELA，agent 整体更便宜」 |

双 session 做法：起两个独立 agent 会话、用户输入相同，各自带不同的
`X-Stela-Mode` + 相同的 `X-Stela-Compare-Group` header；dashboard 的同一个
「A/B 对比」面板会把它们并排（卡片标 `live A/B` 徽章）。

日常对照、回归基准用 replay；偶尔做端到端校验用双 session。

---

## 6. 隐私

proxy 默认开启会话录制，录的是**原始请求 body**——里面有你的 prompt、
代码、文件内容。语料库落在 `~/.stela/corpus/`。

- 不想落盘：`stela proxy --no-record`。
- 改目录：`stela proxy --corpus-dir <path>`。
- 语料库会随会话增长，目前不自动清理，自行管理。
