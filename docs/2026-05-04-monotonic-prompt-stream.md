# 单调 Prompt 流:Agent 侧与推理侧协同的 Token-Efficient 架构

> **状态**:草案 v0.1 / 2026-05-04
> **目标读者**:agent-janus / Hermes / 推理引擎方向的工程同学
> **一句话立场**:agent 侧和推理侧共享一个 **append-only 的 BlockStream + 一组以 `block_id` 为抓手的最小操作原语**;推理侧能配合就走快路径,不能配合就走"等价字节流"的降级路径。**任一场景下 agent 侧代码不变。**

---

## 0. 背景与动机

agent-janus 当前的 Strategy A(prefix normalization)+ E(result externalization)在 SWE-bench 双轮汇总(53 个共同任务)上拿到的最强结论是:

- 不掉解决率
- raw 输入 token **−21%**
- cache_read 占比 **40.5% → 52.7%(+12.2pp)**

这是**事后修补 prompt 字节**能拿到的天花板。再往上走,需要从**数据结构本身**强制前缀稳定,而不是每次请求前再做一遍 normalize。本文是这条路径的设计草案。

> 设计灵感来源:Telos 项目内部"单调前缀 Prompt 架构"讨论,以及 agent-janus 在 4 轮 review 之后沉淀出的不变量。

---

## 1. 核心抽象:`BlockStream`

### 1.1 Block 的最小定义

```text
Block {
  id:         BlockId           // 见 §1.3
  seq:        u64               // 单调递增,gap 即非法
  parent:     BlockId           // 上一个 block 的 id,形成 hash chain
  kind:       BlockKind         // 见下表
  body:       Bytes | Reference // tool_result 默认 Reference,其他默认 Bytes
  taint:      Set<TaintTag>     // 安全 / 合规标签
  visibility: { llm, detector_only, audit_only }
  ttl_hint:   Option<Duration>  // 给推理侧的 cache TTL 暗示
}
```

| BlockKind | 用途 |
|---|---|
| `system_static` | 冷:身份 / playbook / 工具集,跨 session 可共享 |
| `system_dynamic` | 热:时间 / cwd / pid,**永远在尾部** |
| `user_input` | 用户输入 |
| `assistant_output` | 模型输出 |
| `tool_call` | 模型发出的工具调用 |
| `tool_result` | 工具返回,**默认是 reference,不是字节** |
| `redact` | 前向遮蔽 |
| `summary` | prefix rotation 产生 |
| `edit` | 修改语义,见 §4 |

### 1.2 两个不可妥协的不变量

1. **append-only**:`commit()` 后 bytes 永不可变。修改 = 追加新 block,而不是改老 block。
2. **canonical bytes**:同一 block 在任何机器、任何时刻序列化出的字节必须**完全一致**。这是 prefix cache 命中的物理前提。

### 1.3 BlockId 设计(双层寻址)

为了既能让 stream 内有序,又能让**跨 stream / 跨 agent 共享 cold prefix 的 KV**:

```text
content_id = sha256(canonical_bytes)         // 跨 stream 的内容寻址
block_id   = (stream_id, seq, content_id)    // stream 内的位置 + 内容
```

- 推理侧按 `content_id` 索引 KV cache 池(同样字节,任何 stream 都能复用)
- agent / detector 按 `block_id` 索引 stream 内顺序

这一条解决了"团队多 agent 共享 system_static 的 KV"的天然路径。

### 1.4 Stream 的分区结构

```text
BlockStream = [
  ─── Cold Prefix ─── (跨 session 可共享,极强 cache 候选)
  system_static …

  ─── Warm Prefix ─── (本 session 不变,session 级 cache)
  user_input(turn_1.intent), assistant_output(turn_1.plan) …

  ─── Hot Tail ─── (每轮在变,基本不指望复用)
  system_dynamic(time/cwd), tool_call/result(turn_N), …
]
```

> **关键修正**:`system_dynamic` 必须在**尾部**,不是头部。否则时间戳每秒打掉冷/暖前缀的字节序列。这一点 agent-janus Strategy A 的实验已经验证。

---

## 2. Tool Result Reference —— 杠杆最大的一条

### 2.1 三种 reference 形态

| 形态 | 何时用 | body 内容 | LLM 看到 |
|---|---|---|---|
| `inline` | result < 阈值(如 1KB) | 原文 | 原文 |
| `digest` | result 在阈值以上 | `{ref_id, sha256, size, content_type, head_preview, schema_hint}` | 简短摘要 + `ref:abc123` |
| `external` | result 巨大 / 可能重复读 | 仅 content-addressed handle | `tool_result://abc123 (24KB, json)` |

### 2.2 显式 `materialize` 操作原语

模型若决定"要看",发一个新的 tool_call:

```text
tool_call { name: "materialize", input: { ref: "abc123", range?: "lines 100-200" } }
  → tool_result { kind: tool_result, body: <实际字节>, parent: <reference block> }
```

### 2.3 收益

- 默认状态下 prompt 里只有 reference 的 ~100 字节,而不是 24KB 全文
- 真要看,**也是追加在尾部**,前缀仍稳定
- `ref_id = sha256(content)`,**重读自动命中**(比 agent-janus 现在的事后 md5 dedup 更彻底,前置到生成时)
- ref 自带 `schema_hint`,模型可"不看就推理"——大量决策只需要"长度 N、有 status 字段"这种元信息

### 2.4 推理侧配套(可选但强推)

推理服务暴露 sidecar:

```http
GET /v1/blob/{ref_id}     → 字节
PUT /v1/blob              → 返回 ref_id
```

agent 直接 PUT 巨型 tool result,prompt 里只放 ref_id;推理侧装配 prompt 时按需 fetch。这把"巨型字节进 KV cache 池"的问题彻底从 LLM 路径里剔除。

---

## 3. 推理侧契约:以 block_id 为一等键的 KV cache

### 3.1 为什么需要换索引

今天 vLLM/SGLang 用**滚动 token-block hash**(每 16/32 token 一个)。后果:

- agent 的"逻辑 block"和 cache 的"物理 block"不对齐 → 改 1 个 token 让后续 N 个物理 block 全 miss
- agent 想"指定 evict 这个 block"做不到——没有句柄

### 3.2 新协议(Janus 快路径)

```http
POST /v1/messages
  X-Janus-Stream-Id: stream_xxx
  X-Janus-Block-Chain: bid_1,bid_2,...,bid_N    ← 前缀 block id 序列
  body: { messages: [<只发新 block 的 bytes>] }
```

推理侧逻辑:

1. 用 `(stream_id, block_chain)` 在 KV index 里查最长已命中前缀
2. 命中部分 KV 直接复用,不命中 + 新 block 才走 forward
3. 响应头返回 `X-Janus-Cached-Blocks: bid_1,bid_2,...` 让 agent 知道命中到哪

### 3.3 最小操作原语集(只 7 个)

| 原语 | 语义 | 用例 |
|---|---|---|
| `attach(stream_id, block)` | 把新 block 的 KV 算出并锁定到 stream | 普通生成 |
| `detach(stream_id, [bid])` | 释放某些 block 的 KV(可重新计算) | 节省内存,非破坏 |
| `evict(stream_id, [bid])` | 强删 KV + cache index | GDPR forget |
| `pin(stream_id, [bid], ttl)` | 强制驻留,LRU 也不踢 | cold prefix / playbook |
| `fork(stream_id) → new_id` | 复制前缀作为新 stream(零拷贝,refcount) | 多 agent 协作 / 分支 |
| `truncate_after(stream_id, bid)` | 保留 ≤bid 的 KV,丢弃后续 | 错误回滚 / 重试 |
| `materialize_blob(ref_id)` | 装配阶段把 sidecar blob 拼进 prompt | tool result reference |

> **设计纪律**:只暴露这 7 个。少了不够用,多了会重蹈 cache_edits 的碎片化。

### 3.4 stream 生命周期

```text
create(cold_prefix_blocks)
   → fork() (per session)
      → attach() × N   (turns)
      → truncate_after() (revert)
      → close()  (release session refcount;cold prefix 仍 pin 着)
```

---

## 4. 修改语义:不是"覆盖",是"显式失效"

直接覆盖会破坏 append-only。改成:

```text
EditOp:
  invalidate(target_bid, reason)
    → 追加一个 redact 或 edit block
    → 推理侧自动 truncate_after(parent_of(target_bid))
    → 新内容作为新 block 追加
```

性质:

- chain 仍单调,审计 / 检测的"已扫描 block 永不变"不变
- KV cache 失效**精确**(只丢 target 之后),不是 microcompact 的"全部失效"
- 失效自带审计痕迹(redact block 本身就是 audit 记录)

`edit` block 的可见性:

- 给 LLM:渲染成 `⚠ turn 3 的内容已更正,以下是新版本`,防模型沿用旧信息
- 给 detector:全可见,用于追溯
- 给 cache:**不参与 KV**(zero token cost on inference)

---

## 5. Prefix Rotation —— 替代 microcompact 的协议化

```text
agent 侧:
  当 estimate_tokens(stream) > rotate_threshold:
    summary_text = await llm.summarize(blocks_to_compact)
    new_stream_id = await inference.fork(stream_id)
    await inference.attach(new_stream_id, [
      cold_prefix...,           // 共享,零开销
      Block(kind=summary, body=summary_text),
      hot_tail_recent_K...      // 保留最近 K 个不压缩
    ])
    schedule_close(stream_id, after=grace_period)

推理侧:
  fork() 是 O(1) refcount 操作,不复制 KV
  summary block 走一次 prefill —— 这次的 cache miss 不可避免
  但之后整个新 stream 的 cold + summary 都可以 ≥95% hit
```

**关键约束**:`fork` + `attach summary` 必须**原子**。否则中途崩溃会出现"老 stream 已 detach、新 stream 没 attach 完"的 split-brain。

---

## 6. 降级路径(必须设计 —— 因为今天就是这样)

OpenAI / Claude / DeepSeek 现在都不会按 block_id 给你返回 cache 状态。**降级路径不是 fallback,是 default;快路径反而是 opt-in。**

### 6.1 降级矩阵

| 推理侧能力 | 走法 |
|---|---|
| 完全不感知(任何 OpenAI 兼容代理) | agent 侧把 BlockStream **序列化为传统 messages 数组**,但保证字节稳定。等价于 agent-janus Strategy A + E。 |
| 支持 `cache_control`(Anthropic) | 在 cold/warm 边界自动打 `cache_control: ephemeral`。block_id chain 不发出去。 |
| 支持 `previous_response_id`(OpenAI Responses) | stream_id 映射成 previous_response_id。每次只发 hot tail 的新 block。 |
| 支持 vLLM/SGLang 的 automatic prefix caching | 字节稳定即可命中,什么 hint 都不发。 |
| **支持 Janus block 协议**(自家 patch 的 vLLM/SGLang) | 走 §3 快路径:发 `X-Janus-Block-Chain`,7 原语全开。 |

> agent 代码看不见这个矩阵,只对 BlockStream + 7 个原语编程。底下由 Janus bridge 按 `EngineCapabilities` 自动选路。**这是"不写两套 SDK,写一个 SDK + N 个适配器"的实现方式。**

### 6.2 降级时怎么模拟"按 block 操作 cache"

显然做不到(只读 API),但可以**功能等价地不做**:

| 原语 | 降级行为 |
|---|---|
| `evict` | agent 侧追加 redact + 重发整段(承担一次 cache miss) |
| `pin` | no-op(寄希望于上游 LRU 自然保留高频前缀) |
| `fork` | agent 侧复制 stream 对象,新 stream 第一次请求承担一次 cache miss |
| `truncate_after` | agent 侧砍 stream tail,下次请求等价于"短一截的 prompt" |

**契约**:所有降级**功能等价,只丢性能,不丢正确性**。

---

## 7. 与 agent-janus 现状的关系

| agent-janus 现状 | 单调流方案下变成 |
|---|---|
| Strategy A: prefix normalization | **不再需要** —— BlockStream 在数据结构上就保证字节稳定 |
| Strategy E: result-externalization (md5 dedup) | **前置化** —— tool result 默认 reference,生成时就避免重复 |
| `UpstreamHints` 透传 `cache_control / previous_response_id` | 仍存在,作为降级路径的实现细节 |
| `EngineCapabilities` | 扩展 4 个 bool:`supportsBlockChain / supportsBlockOps / supportsBlobSidecar / supportsAtomicFork` |
| Phase 6 engine-patches | **变成单调流的 reference 实现**:vLLM/SGLang patch 暴露 7 原语 |

---

## 8. 落地次序

| 阶段 | 内容 | 依赖 |
|---|---|---|
| 1 | BlockStream IR + canonical 序列化 + hash chain | 无 |
| 2 | agent 侧 SDK(Hermes 适配器):消息序列 → BlockStream | (1) |
| 3 | **全降级路径**:序列化为现有协议(Anthropic/OpenAI),功能等价 | (1)(2) |
| 4 | tool result reference + sidecar blob endpoint | (1) |
| 5 | vLLM patch:7 原语 + `X-Janus-Block-Chain` 解析 | (1) |
| 6 | SGLang patch:同上 | (1) |
| 7 | Prefix rotation 协议化(双侧原子 fork) | (5)(6) |

> **(3) 必须先于 (5)(6) 完成** —— 保证任何时候都有可工作的路径,不让"等推理侧 patch"成为 SDK 推广的阻塞。

---

## 9. 待解问题

1. **canonical bytes 的字节级规范**:JSON key 顺序、空白、Unicode normalization。两端实现不一致,跨 agent 共享 cache 就废了。需要一份类似 RFC 8785 JCS 的字节级 spec。
2. **streaming 期间的 block 封装**:模型 streaming 输出时 token 一个一个吐,什么时候封 block?当前提议:`stop_reason` 触发时封;mid-stream 不封。但这需要推理侧支持 "speculative attach + commit on finalize"。
3. **可见性分裂的渲染成本**:`visibility ∈ {llm, detector_only, audit_only}` 让序列化要出三种字节序。需明确**只有 `llm` 可见的 block 参与 KV cache 索引**,否则 cache 状态会和 detector schema 耦合,后者一改前者全 miss。
4. **block 大小下限**:太小(1 token)的 block 让链元数据 > 内容,cache index 也膨胀。建议 enforce 最小 ~64 token 边界,小于的合并;但 user_input / tool_result 等语义边界优先,不能为对齐而合并不同 kind。
5. **多 agent 协作时 cold prefix 的所有权**:多个 stream 共享同一段 cold prefix 的 KV,谁负责 `pin` / `evict`?refcount 显然要,但需要一份明确的 ownership 模型。

---

## 10. 最小可验证里程碑(Spike)

不要直接 RFC + 18 周路线图。先做一个**可量化打脸**的 spike:

> **Spike**:用现成 vLLM,**不打任何 patch**,只在 agent 侧实现 BlockStream + canonical 序列化 + reference 化 tool result,跑 `result_5_2.md` 的 50 个 SWE-bench 任务。
>
> **假设**:即使推理侧不配合,光是 (a) 字节稳定 + (b) tool result 默认 reference,raw_input/task 应该比当前 `111` 模式再降 **≥30%**,cache_read 占比能到 **≥70%**。
>
> **如果跑不出来**,整个协议设计的前提就有问题——因为 §3 的快路径都还没启用,光降级路径就该有这个数量级提升。

跑出来后再投入做 §3 的 vLLM patch,届时就有"基线 / 单调流降级 / 单调流快路径"三档对照,RFC 才有数据撑得起来。

---

## 附录 A:与现有 provider 协议的映射表

| Provider 能力 | 单调流如何利用 |
|---|---|
| Anthropic `cache_control: ephemeral` | 在 cold/warm 边界打 marker,前缀全命中 |
| Anthropic `cache_edits`(私有) | Strategy D 路径,**默认关闭** |
| OpenAI `previous_response_id` | 直接作为 stream_id 的远端表示 |
| OpenAI `prompt_cache_key` | = stream_id |
| vLLM/SGLang automatic prefix caching | 字节稳定即自动命中,无需 hint |
| vLLM/SGLang `cache_salt` | = `sha256(api_key_hash ‖ stream_id)[:16]` |
| Codex `x-codex-turn-state` 增量 | 单调流的 delta 即 Codex 的 input 增量 —— 完美契合 |

---

## 附录 B:与 Telos 单调前缀讨论的差异

| 维度 | Telos 原稿 | 本草案 |
|---|---|---|
| 出发点 | KV cache + 检测引擎共享"前缀稳"约束 | 同上,但额外吸收 agent-janus 4 轮 review 的工程不变量 |
| 数据结构 | MonotonicPromptStream | BlockStream(同物,改名以避免与 telos 内部命名冲突) |
| 核心新增 | tool result reference 默认化、双层 BlockId、7 原语推理侧 ABI、降级路径作为 default | |
| 落地次序 | 6 阶段 18 周 | 7 阶段 + 强制要求降级路径先于快路径 |
| 验证策略 | 起 RFC | 先 spike,跑通再 RFC |
