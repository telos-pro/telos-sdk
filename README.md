<div align="center">

<img src="branding/logo.svg" alt="TELOS — Portable Agent Context" width="460"/>

### Context is yours &nbsp;·&nbsp; Agents are hired

**No rewrite. No compression. 90% token billing saving.**

<sub>One canonical IR — tools, system, turns, and memory — runs unchanged across Anthropic · OpenAI · DeepSeek · vLLM · SGLang<br/>Real 6-turn session −92.3% · Cost reported in absolute $/query-resolved — ratios can be gamed; dollars can't</sub>

<br/>

[![Core](https://img.shields.io/badge/core-Apache%202.0-2C5F66?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-4FB3BF?style=flat-square)](pyproject.toml)
[![Status](https://img.shields.io/badge/status-Beta-d8851f?style=flat-square)](CHANGELOG.md)
[![Protocol](https://img.shields.io/badge/protocol-TELOS%20IR-7FD8E0?style=flat-square)](docs/2026-05-06-telos-protocol.md)

[**Quickstart**](#-3-step-start) · [**Why**](#-the-problem--two-broken-legs) · [**Protocol**](#-the-protocol-not-compression-but-never-breaking-the-prefix) · [**Engines**](#-engines--one-ir-five-backends) · [**Docs**](#-going-deeper)

<sub>📖 &nbsp;**English** · [Simplified Chinese](README.zh-CN.md)</sub>

</div>

---

## ⬢ &nbsp;2 a.m. — where did all the money go?

2 a.m., agent still running. The counter in the bottom-right corner climbs to 2,847,103 — you convert it to dollars and your stomach drops. Worse: the line above reads `cache_read: 0`. All night long, every turn fed the same 4,000-token system prompt **from scratch** to the model, billed at full price.

Take the exact same **6-turn** real conversation, drop it into openclaw, flip two switches:

| Mode | raw input tokens | cache_read | Cost for 6 turns |
|---|:--:|:--:|:--:|
| passthrough (today's default) | 24,151 | 0 | **$0.3623** |
| with TELOS | 0 | 18,701 | **$0.0281 (−92.3%)** |

Scale to 1,000 sessions: **$362 → $26**. In a controlled A/B/C/D run (`showcase/dashboard.html`, 2026-05-19) — 48 calls across 4 sessions, counterfactual bill **$5.90**, actual **$3.74** — net saved **$2.16 (−36.6%)**. One dev machine, one afternoon. Multiply by team scale, and that's a real server invoice every month.

**Stop measuring in "X× fewer tokens."** In 2026, the pricing gap between tiers of the same model family already spans **80×–150×**. Anyone can inflate a ratio by stuffing the cheapest tier in the denominator — absolute dollars are the only number that doesn't lie.

<p align="center">
  <img src="promo-assets/01-waste.svg" alt="Today's agent token efficiency is only 25%" width="100%"/>
</p>

---

## ⬢ &nbsp;The problem — two broken legs

**Leg one: Token burn, fast and needless.** In a 20-turn session, that 4,000-token system prompt gets read 20 times verbatim. Anthropic's cache hit costs 10%, a miss costs 100% — and all it takes to invalidate a full-session PIN is a single `currentDate: 2026-05-20` slipped into the system prompt. That gate has never been closed for you.

**Leg two: Your context isn't yours.** The persona you spent a day tuning, the tool-chain you wired up, the 20-turn thread you're mid-way through — all of it is trapped on someone else's server. Want to try DeepSeek instead? Their first response: *"Tell me about your project."* Want to hand a task segment to a model that's better at it? Can't be done. The bill arrives as a percentage diluted by whatever denominator they chose. **You're not the agent's owner — you're a tenant inside someone else's agent.**

<p align="center">
  <img src="promo-assets/02-pain-points.svg" alt="Four walls" width="100%"/>
</p>

---

## ⬢ &nbsp;TELOS solves exactly two things

**① Push token efficiency to the limit.** 6-turn real session **−92.3%**; controlled 48-call run **−36.6% (net −$2.16)**. Every cent accounted for in absolute $/query-resolved — ratios can be faked; dollars can't.

**② Return context sovereignty to you.** `TelosIR` is an engine-agnostic, serializable, portable context representation. Your persona, your tools, your 20-turn mid-session thread — everything packed into the same **stone tablet**. Hand it to Claude today, move it to DeepSeek tomorrow, run it on a local vLLM tonight. **Your context; agents are just hired help.**

---

## ⬢ &nbsp;The protocol: not compression, but never breaking the prefix

Most agent frameworks treat KV-cache as a runtime gift the inference engine may or may not give you. TELOS inverts this:

> **Cache reuse is a structural property of the prompt itself, not a matter of runtime luck. If you never touch bytes already submitted, the cache *cannot* be invalidated.**

That principle materializes in three interlocking ideas.

### Three-color bands

<p align="center">
  <img src="promo-assets/03-banding.en.svg" alt="PIN / FOLD / DROP bands" width="100%"/>
</p>

Every content block declares its cache lifetime **at birth** — not post-hoc heuristics, not LLM guessing, but first-class structural annotation:

| Band | Color | Semantics | Cache behavior |
|---|:---:|---|---|
| **PIN** | 🟢 | Tool defs · system prompt · current question | Permanent. Never evicted. The immutable base of every request's prefix hash |
| **FOLD** | 🟡 | Conversation history · tool results · large docs | Cacheable, compactable. Under pressure, replaced by a summary — PIN prefix bytes stay untouched |
| **DROP** | 🔴 | Timestamps · CWD · git status · PIDs | Ephemeral. **Excluded entirely from the prefix hash.** Must follow all BPs; never contaminates upstream bytes |

The ordering invariant is absolute: **PIN\* → FOLD\* → DROP\*** — within each message, across the full prompt, at every layer. This is the **only** structural rule that wins the cache — everything else is implementation detail.

### Monotonic append

The prompt is an **append-only stream**. New turns only add blocks to the tail — **no mutation of already-submitted bytes**. A "modification" is expressed as a new block (summary, redaction), never an in-place rewrite.

<p align="center">
  <img src="promo-assets/04-append.en.svg" alt="Monotonic append: cache hit rate is monotonically non-decreasing with session length" width="100%"/>
</p>

Because earlier blocks are immutable and bytes are identical across turns, the inference engine's prefix-matching algorithm finds the longest common prefix on **every** request — not by luck, but by construction. **Cache hit rate is therefore a monotonically non-decreasing function of session length: longer sessions, more reuse, never regression.**

### One IR, five backends

The same `TelosIR` lands on different engines via **deterministic capability degradation**: Anthropic's explicit BPs, OpenAI's `prompt_cache_key`, DeepSeek's byte-stable prefix, vLLM's cooperative eviction, SGLang's RadixAttention — each engine pushed to its actual cache ceiling, while your agent code changes nothing.

<p align="center">
  <img src="promo-assets/05-dashboard.png" alt="TELOS savings dashboard — absolute dollars broken down by harness / model / session" width="100%"/>
</p>

<p align="center"><sub><strong>Every saving pinned to an absolute dollar figure</strong> · No cloud server required · Opens offline · <code>~/.telos/usage.jsonl</code> fed directly into a single-file HTML page</sub></p>

---

## ⬢ &nbsp;3-step start

#### ❶ &nbsp;Install

```bash
pip install telos-sdk
```

#### ❷ &nbsp;Connect

```bash
telos init
```

Auto-detects **claude-code / codex / openclaw / hermes** on this machine, injects config into each, and starts the local gateway in the background (state written to `~/.telos/gateway.json`). No changes to your agent code.

#### ❸ &nbsp;Observe

```bash
telos dashboard
```

Opens an offline HTML dashboard in your browser showing savings per call in absolute dollars. Every invocation is automatically appended to `~/.telos/usage.jsonl` and aggregated in real time.

**TELOS is open source. Run it on your own workflow — see whether that 92% is real, or just another "X× tokens" claim.**

---

## ⬢ &nbsp;Engines — one IR, five backends

TELOS is normative: it defines how context *should* be represented, and engines align by capability. One `TelosIR` on different engines is degraded **deterministically** by the adapter — never silently, never lossily in meaning.

| Capability | Anthropic 4.6+ | OpenAI 4+/5.x | DeepSeek V3+ | vLLM | SGLang |
|---|:---:|:---:|:---:|:---:|:---:|
| explicit BP / anchors | ✓ (≤4) | ✗ | ✗ | ✓ | ✓ |
| explicit prewarm | ✓ | ✗ | ✗ | ✓ | ✓ |
| routing key | ✗ | `prompt_cache_key` | ✗ | `cache_salt` | `affinity_key` |
| cache probe / segment evict | ✗ | ✗ | ✗ | ✓ | ✓ |
| fork-and-replace | ✗ | ✗ | ✗ | partial | ✓ |

> **Bidirectional capability** (`BidirectionalEngineAdapter`, open-source inference engines only): `cooperative_fold()` lets the server keep the prefix KV untouched and recompute only the summary tail — a closed API's `fold` is a client-side rewrite that forces re-prefill of the whole span every time. Full matrix in [protocol §6](docs/2026-05-06-telos-protocol.md).

---

## ⬢ &nbsp;Architecture

```
agent harness ──► TELOS Bridge ──► engine adapter ──► LLM service
   (parse)          (5 primitives)   (capability-aware)
```

| Layer | Files | Responsibility |
|---|---|---|
| harness | [`harness/openclaw.py`](harness/) `hermes.py` | split envelope, large docs into ref-pool, produce `TelosIR` |
| bridge | [`bridge.py`](bridge.py) [`ir.py`](ir.py) [`refpool.py`](refpool.py) | 5 primitives, invariant checks, frozen ref-pool slugs, canonicalize |
| engine | [`engine/anthropic.py`](engine/) `openai.py` `deepseek.py` | capability-aware Mark, wire serialization, usage parsing |

The bridge is pure Python with no LLM SDK dependency. `TelosIR` is the single data structure passing between all three layers — frozen, narrow-fielded, engine-agnostic.

---

## ⬢ &nbsp;One invariant

The whole protocol has exactly one hard constraint. Within each segment (`tools` / `system` / a single `message`), blocks must be in physical order:

```
PIN*  →  FOLD*  →  DROP*
```

<sub>(In a `message`, `tool_result` blocks always come first — required by the Anthropic protocol.)</sub>

| Band | Meaning | Typical content |
|---|---|---|
| **PIN** | long-lived stable segment | tool definitions, system prompt, current question |
| **FOLD** | cacheable but droppable on compact | assistant replies, tool_result, large ref-pool docs |
| **DROP** | never enters the cache hash | timestamp, cwd, git status, envelope |

Violate it and `TelosInvariantError` is raised. Everything else is a soft suggestion.

### Five primitives &nbsp;<sub>(`Bridge` methods)</sub>

| Primitive | Purpose |
|---|---|
| `place(segment, blocks)` | put blocks into tools / system / the current message |
| `pin(slug, payload)` | write a PIN block into the system segment |
| `mark()` | let the engine produce this turn's BP / routing-key plan |
| `fold(slugs= / message_range=, summary=)` | fold old turns into ref-pool references |
| `refresh(plan)` | once throttling allows, send a `max_tokens=0` prewarm (Anthropic only) |

### ref-pool — a "pointer table" for context

A slug is **frozen** the moment `register()` is called: content can change (`fold()`), the slug cannot. `fold()` swaps the payload, not the slug → every `[ref:slug]` reference stays byte-identical → BPs still hit after a fold. **Stable pointers, flowing content** — portable context realized in the protocol.

---

## ⬢ &nbsp;Appendix: R1–R8 protocol-hazard fixes

Review surfaced 8 design hazards in the protocol; the Python implementation fixes all of them:

| ID | Problem | Fix location |
|---|---|---|
| R1 | OpenAI `prompt_cache_key` only widens slots at ≥15 RPM per key | `engine/openai.py :: KEY_RPM_SOFT_CAP = 12` + `shard()` |
| R2 | Anthropic's 4 BPs cover only head + tail, leaving mid turns uncached | `engine/anthropic.py :: _MID_ANCHOR_STRIDE = 19` |
| R3 | sub-agent IR and parent IR sharing a `session_id` | `harness/hermes.py` — sub-IR parsed independently |
| R4 | after `fold()`, a Mark slot can land in a folded span | `bridge.py :: fold()` — re-run `mark()` |
| R5 | tool field / array order not stably canonicalized | `bridge.py :: _canonicalize_ir()` |
| R6 | thinking blocks lost across non-tool_result calls | `engine/base.py :: thinking_preserved_across_non_tool_result` |
| R7 | no explicit priority when Anthropic BP candidates > 4 | `engine/anthropic.py :: plan_marks` priority + truncation |
| R8 | refresh unthrottled, can saturate quota in reverse | `bridge.py :: REFRESH_THRESHOLD = 11` adaptive gate |

---

## ⬢ &nbsp;Going deeper

| What you want | Where |
|---|---|
| Get started (install, integration, CLI, troubleshooting) | [`docs/User-guide.md`](docs/User-guide.md) |
| Understand the protocol | [`docs/2026-05-06-telos-protocol.md`](docs/2026-05-06-telos-protocol.md) |
| See the architecture | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| See the change history | [`CHANGELOG.md`](CHANGELOG.md) |

---

## ⬢ &nbsp;License

Apache-2.0 — the protocol core is open source, forever. See [LICENSE](LICENSE).

---

<div align="center">
<a href="https://github.com/telos-pro/telos-sdk"><img src="https://img.shields.io/badge/⭐%20Star%20on%20GitHub-telos--pro%2Ftelos--sdk-1F4A50?style=for-the-badge&logo=github&logoColor=white" alt="Star on GitHub"/></a>

**[github.com/telos-pro/telos-sdk](https://github.com/telos-pro/telos-sdk)** &nbsp;·&nbsp; Apache 2.0

<sub>Token efficiency is not about compression — it's about never breaking the prefix.</sub>

</div>
