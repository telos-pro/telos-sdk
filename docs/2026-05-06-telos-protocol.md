# TELOS: A Three-Layer Protocol for Cache-Friendly Agent Prompts

**Name**: **TELOS** — *Stable prefix · Tiered bands · Ephemeral tail · Layered adapters · Anchored marks*
**Status**: Design v1 (shipping protocol)
**Date**: 2026-05-06
**Audience**: agent framework integrators (OpenClaw, Hermes / Claude Code), inference-side adapter authors
**Related**: [ARCHITECTURE.md](ARCHITECTURE.md) (code-level reference), [playbook.md](playbook.md) (operator guide)

---

## 0. Why "TELOS"

A *telos* is an upright stone slab carved with a durable inscription at the base and ephemeral notes added above over time. The metaphor maps exactly:

1. **A stable inscription at the base.** The durable prefix (tools + system pin + ref-pool) is carved once and reused across every turn — like the base inscription on a telos that outlives generations of additions.
2. **Ephemeral marks layered above.** Per-turn content (drop band) is added on top, never disturbing the base. Foldable history (fold band) sits in the middle: it can be erased and replaced with a shorter summary without re-cutting the base.
3. **Anchors at fixed depths.** Cache breakpoints (the "Marks" in *Indexed Span Marking*) are placed at well-known depths that every engine adapter can find — the way a telos's inscription bands are read at fixed rows.

Compared to Janus-Prompt: same physics, ⅓ the surface area. TELOS keeps the *ordering invariant* (the only thing that actually wins cache) and drops the parts that mostly explain themselves (TTL philosophy, FAQs, second-round-review-of-second-round-review). Bridge logic that Janus splits across 6 sub-modules collapses into 5 composable primitives.

---

## 1. Goals & Non-Goals

| | Goal | Non-Goal |
|---|---|---|
| G1 | One IR that wins cache on *any* engine that has any caching at all | A new wire format engines must adopt |
| G2 | Five primitives, total. Bridge code fits in one file. | Generic "prompt orchestration" framework |
| G3 | Harness plugins are <300 LOC each, mechanical translation | Replacing the upstream agents' planners |
| G4 | Engine adapters degrade gracefully — same IR, weaker guarantees on weaker engines | Pretending DeepSeek has TTL knobs it doesn't |
| G5 | Verifiable against [CLAUDE.md](../CLAUDE.md)'s four north-star metrics | Cache-hit-ratio as a headline (it's a diagnostic, see §1.1 of Janus doc) |

**Success criteria** (per CLAUDE.md): on the same SWE-bench / Polyglot dataset, with TELOS enabled vs. baseline:

- `tokens_per_resolved_task` ↓ by ≥ X%
- `resolved_rate` does not drop
- `cache_read_total` ↑ in absolute terms

---

## 2. Architecture (Three Layers)

```
                ┌───────────────────────────────────────────────┐
   upstream     │  OpenClaw    Hermes (Claude Code)    others   │
   agents       └───────┬───────────────┬───────────────────────┘
                        │               │
                        ▼               ▼
                ┌──────────────────────────────┐
   Layer 1      │       HARNESS PLUGIN          │   adapter per upstream
   (parse)      │  raw request  →  TELOS IR     │   stateless, mechanical
                └──────────────┬────────────────┘
                               │  TELOS IR (tri-banded blocks +
                               │             ref-pool + session ctx)
                               ▼
                ┌──────────────────────────────┐
   Layer 2      │           BRIDGE              │   the policy core
   (policy)     │  5 primitives:                │   single IR in / single IR out
                │   Place · Pin · Mark ·        │   stateful (per session)
                │   Fold  · Refresh             │
                └──────────────┬────────────────┘
                               │  TELOS IR (rewritten,
                               │             with Mark/Pin annotations)
                               ▼
                ┌──────────────────────────────┐
   Layer 3      │       ENGINE ADAPTER          │   one per inference API
   (emit)       │  TELOS IR → wire request      │   stateless, capability-driven
                └──────┬─────────────┬───────┬──┘
                       │             │       │
                       ▼             ▼       ▼
                  Anthropic       OpenAI   DeepSeek
                  (Claude 4.6+)   (gpt-5+) (V3+)
```

### Why exactly three layers

- **Layer 1 = "what did the agent want to say?"** Pure parse, no policy. If OpenClaw sends a `system-reminder` envelope, the OpenClaw plugin knows where to find it; the bridge does not.
- **Layer 2 = "how should this be cached?"** Pure policy, no wire knowledge. Operates on IR only. Every cache decision lives here.
- **Layer 3 = "what does *this* engine accept?"** Pure emit, no policy. Capability matrix drives what each adapter actually wires up; missing capabilities silently degrade, never throw.

Layers communicate by **value** (one IR object passed down). No layer reaches up. No layer holds references into another's internals.

---

## 3. The TELOS IR (Layer 1 ↔ Layer 2 ↔ Layer 3 contract)

```ts
type Band = "pin" | "fold" | "drop";
//          ▲       ▲        ▲
//          │       │        └─ never enters cache hash; emitted last in its segment
//          │       └────────── caches but may be folded on compact (5-min class)
//          └────────────────── caches and stays (1-hour class; the durable prefix)

interface TelosBlock {
  id: string;             // stable within a session
  band: Band;
  kind: "text" | "tool_def" | "tool_use" | "tool_result" | "image" | "thinking";
  payload: unknown;       // engine-agnostic content
  refSlug?: string;       // if set, this block lives in the ref-pool (see §4)
  sourceTag?: string;     // diagnostic: which harness rule produced this band
}

interface TelosMessage {
  role: "system" | "user" | "assistant";
  blocks: TelosBlock[];   // ordered: must satisfy band ordering invariant (see §5)
}

interface TelosIR {
  sessionId: string;
  tools: TelosBlock[];          // all band="pin" by construction
  system: TelosBlock[];
  messages: TelosMessage[];
  refPool: Record<string, TelosBlock>;  // slug → block; rendered into `system` by bridge
  hints: {
    engine: "anthropic" | "openai" | "deepseek";
    model: string;
    expectedTurns?: number;     // helps Mark scheduler
  };
}
```

That's the entire vocabulary between layers. Five fields on `TelosBlock`, three on `TelosMessage`, six on `TelosIR`. No span maps, no fold groups, no out-of-band headers.

---

## 4. The Reference Pool (one idea, kept)

Janus's single most useful construct: every large blob (file content, doc, big tool result) lives in a **ref-pool** keyed by a stable slug. Anywhere else in the prompt, it is referenced by slug only.

```jsonc
// ref-pool entry (lives in TelosIR.refPool, rendered into system)
{ "id": "ref:login.py", "band": "fold", "refSlug": "login.py",
  "kind": "text", "payload": "<4000 lines>" }

// reference site (in user/assistant message)
{ "id": "u1-q", "band": "pin", "kind": "text",
  "payload": "Refactor [ref:login.py] using the new auth module." }
```

Two properties this gives us for free:

1. **Folding the pool entry does not change the byte at the reference site** — so the durable prefix stays valid.
2. **Slug stability is enforced once, at registration time** — not by convention.

The bridge keeps a frozen `Set<slug>` per session. Emit-time lint scans every text block for `[ref:<slug>]`; unknown slugs are a hard error. (Janus §8.5 L1, kept verbatim because it's cheap and correct.)

---

## 5. The One Invariant That Matters

**Within every segment (tools / system / each message), blocks MUST be physically ordered:**

```
  pin*   →   fold*   →   drop*
```

Every other TELOS rule is a corollary of this one. If you violate it:

- a `drop` block earlier than a `pin` block poisons the prefix hash → no cache hit ever
- a `fold` block earlier than a `pin` block means folding it later breaks the durable prefix

Layer 1 (harness plugin) is responsible for producing IR that satisfies the ordering. Layer 2 (bridge) re-checks on every operation. Layer 3 (engine adapter) re-checks before emitting. Three-line assertion, three places.

The user-message split rule from Janus §3.2 follows directly: a user message is multi-block, with `pin` for the actual user query, `fold` for prior tool-result echoes / `[ref:...]` references, `drop` for the harness envelope (`<system-reminder>`, cwd, git status, timestamp). This is *the same invariant*, applied inside one message.

### 5.1 In-band byte stability (canonicalization)

§5 fixes the *band* order; the bridge additionally fixes the *byte* layout within each band so a re-emit with the same logical IR produces the same wire bytes. Two rules, both applied at `emit` time inside `_canonicalize_ir` (see [`bridge.py`](../bridge.py)):

1. **Tools-array stable order.** `ir.tools` is sorted by `(source_rank, mcp_server, name)` where `source_rank` is `builtin(0) → mcp(1) → user(2) → untagged(3)`. This neutralizes two common cache-prefix breakers:
   - MCP discovery races that re-permute the tools list between sessions
   - harnesses that splice "new" tools to the front of the list and silently invalidate every historical session's PIN prefix
   Source is read from `TelosBlock.extra["source"]` (and optional `extra["mcp_server"]`); harnesses are expected to tag, untagged tools rank last (safe default — costs stability, never correctness).

2. **Set-semantic schema arrays sorted.** Inside a `tool_def` block's schema subtree (Anthropic `input_schema`, OpenAI `function.parameters`), arrays whose key is in `_SCHEMA_SET_ARRAY_KEYS = {"required"}` are sorted as strings. Conservative on purpose — `enum` / `examples` / `anyOf` / `oneOf` / `allOf` are *not* sorted because some prompts rely on their order. The set is module-level on `bridge` so a harness with a special tool can monkey-patch.

`tool_use` / `tool_result` payloads are still only key-sorted at the dict level — their array values are user data, not schema, so we never reorder them even if a key happens to be named `required`.

The wire-side guarantee: shuffling input tool order or shuffling a `required` array yields a byte-equal `Bridge.emit()` result. Regression test: [`tests/test_canonical.py`](../tests/test_canonical.py) `test_emit_byte_stable_under_tool_shuffle`.

---

## 6. The Five Bridge Primitives

The bridge holds session state and exposes exactly five operations. Each is a pure function `TelosIR → TelosIR` (or `→ EmitPlan` for `Mark`).

| # | Primitive | Signature | What it does |
|---|---|---|---|
| 1 | **Place** | `place(block, band)` | Set or change a block's band. Re-asserts §5 ordering. |
| 2 | **Pin** | `pin(slug, payload)` | Register a ref-pool entry; freezes the slug. |
| 3 | **Mark** | `mark(): EmitPlan` | Decide where breakpoints go *for this emit*. Returns up to N positions tagged by intended TTL. Per-engine; bridge calls the adapter's planner. |
| 4 | **Fold** | `fold(slug or messageRange)` | Replace a ref-pool entry's payload with `<folded: N tokens>`, or replace a span of historical messages with a summary block (still `band=fold`). Byte at reference sites unchanged. |
| 5 | **Refresh** | `refresh(slot)` | Issue a keep-alive against a Mark slot, if the engine supports prewarm. No-op on engines that don't. |

That's it. Compact = `Fold` over the whole foldable region. TTL refresh = `Refresh` on a timer. Subagent dispatch = same 5 primitives on a child IR.

### What the bridge does *not* have

- No "compaction strategies" enum. There is one strategy: fold the foldable region into a placeholder. Engine-side cooperative compact (Janus §7.2) is a future capability, exposed as `Fold(coopHint=true)`; current adapters ignore the hint.
- No TTL state machine. TTL is decided by `Mark` at emit time, based on band:
  - `band=pin` → request 1h (or engine equivalent / default)
  - `band=fold` → request 5m (or engine equivalent / default)
  - `band=drop` → never marked
- No per-block hash tracking. The engine does that. We just don't *change* bytes we promised wouldn't change.

---

## 7. Layer 1: Harness Plugins

A harness plugin is a stateless function:

```ts
function parse(rawAgentRequest: unknown, sessionCtx: SessionCtx): TelosIR
```

Two reference plugins ship with TELOS:

### 7.1 OpenClaw plugin

Input: OpenClaw `/v1/messages`-shaped request (Anthropic-compatible) plus OpenClaw-specific metadata in `metadata.openclaw`.

Banding rules (mechanical):

| OpenClaw input | TELOS band |
|---|---|
| `tools[]` | `pin` |
| `system[]` (top of stack) | `pin` |
| Large doc / file content uploaded by tools (>2KB text) | `fold`, moved to ref-pool |
| `messages[i].role=user`, plain text query | `pin` |
| `messages[i].role=user`, `<environment_info>...</environment_info>` envelope | `drop` (split into separate block) |
| `messages[i].role=assistant`, text or tool_use | `fold` |
| `messages[i].role=user`, `tool_result` content | `fold` |
| Per-turn timestamps / `Current time:` injections | `drop` |

The split of user messages into `(pin, fold, drop)` sub-blocks is the only non-trivial step; reuse the regex set from [agent-janus/bridge/src/efficiency/prefix-normalization/system.ts](../agent-janus/bridge/src/efficiency/prefix-normalization/system.ts) (`stripDynamicFields`).

### 7.2 Hermes (Claude Code) plugin

Same shape. Differences from OpenClaw:

| Hermes input | TELOS band |
|---|---|
| `<system-reminder>...</system-reminder>` blocks | `drop` |
| `<command-message>` / `<command-name>` | `drop` |
| Subagent-spawning `Agent` tool result | parent: `fold`; child IR is its own session, parsed by the same plugin |
| `<file>...</file>` blocks > 2KB | `fold`, moved to ref-pool with slug = file path |

That's the entire delta. Both plugins together are < 600 LOC.

---

## 8. Layer 3: Engine Adapters

Each adapter implements:

```ts
interface EngineAdapter {
  capabilities: EngineCapabilities;
  planMarks(ir: TelosIR): EmitPlan;          // bridge.Mark() delegates here
  emit(ir: TelosIR, plan: EmitPlan): WireRequest;
  refresh(plan: EmitPlan, slot: SlotId): Promise<void> | null;
  parseUsage(wireResponse: unknown): UsageReport;
}

interface EngineCapabilities {
  explicitBreakpoints: boolean;     // Anthropic only
  ttlControl: "none" | "presets" | "seconds";
  prewarmable: boolean;
  routingKey: boolean;              // OpenAI prompt_cache_key
  retentionPolicy: "fixed" | "configurable";
  maxBreakpoints: number;           // 0 = no explicit
}
```

Below: how each adapter's `planMarks` and `emit` actually work, grounded in the providers' published behavior.

### 8.1 Anthropic adapter (Claude Opus/Sonnet 4.6+, full caching control)

**Capabilities**: explicit breakpoints (≤4 slots), 5m + 1h TTL, `max_tokens:0` prewarm, 20-block lookback, hierarchy `tools → system → messages`, mixed-TTL ordering rule (1h must precede 5m).

**`planMarks`** — derives 4 breakpoints directly from TELOS bands:

| Slot | Position | TTL | Source band |
|---|---|---|---|
| BP-T | last `pin` block in `tools` (or end of `tools` if all pin) | `1h` | tools |
| BP-S | last `pin` block in `system` | `1h` | system base |
| BP-R | last `fold` block in `system` (i.e., end of ref-pool render) | `1h` initially; on Fold compact, re-emitted as `5m` slot | ref-pool |
| BP-X | last `pin`-or-`fold` block in the latest message (rolling) | `5m` | latest turn |

If `tools` has no boundary distinct from `system`, BP-T is dropped and that slot becomes a mid-history rolling anchor (mirrors Janus §4.3 BP2).

The 1h-before-5m ordering invariant is satisfied automatically by §5 (pin precedes fold, system precedes messages).

**`emit`**: TELOS IR → standard Anthropic request. Each `drop` block is emitted *after* the last block carrying `cache_control` in its segment, so it never enters any prefix hash. Tool input dicts are key-sorted (Anthropic explicitly calls out Swift/Go-style key randomization as a cache breaker; we canonicalize once at emit).

**`refresh`**: fires a `max_tokens: 0` request whose system+tools mirror the live IR up to BP-S (the durable system anchor). Adaptive cadence per Janus §6.3.1: skip the refresh if `recent_real_requests < REFRESH_THRESHOLD`. Refresh requests force `stream=false`, `thinking.type=disabled`, `tool_choice="auto"` (Anthropic's documented `max_tokens:0` constraints).

**`parseUsage`**: reads `usage.cache_creation_input_tokens`, `cache_read_input_tokens`, plus the breakdown `cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`.

### 8.2 OpenAI adapter (gpt-5 / gpt-5.x / gpt-4.1)

**Capabilities**: fully automatic prefix cache (1024-token minimum), `prompt_cache_key` for routing affinity, `prompt_cache_retention: "24h"` opt-in extended retention. **No explicit breakpoints, no TTL knob beyond the retention policy**.

**`planMarks`** returns an empty breakpoint list. The "policy" TELOS exerts on OpenAI is entirely:

1. **Physical ordering** (§5): pin/fold/drop layout means OpenAI's automatic prefix matcher finds the longest stable prefix at the front, exactly as its docs recommend ("static content at the beginning, variable content at the end").
2. **`prompt_cache_key` derivation**: hash of `(toolset_id, system_pin_hash, ref_pool_pinned_slugs)`. This routes requests with the same durable prefix to the same machine, which OpenAI's docs identify as the difference between hitting cache and missing it for any given request. Granularity is chosen so each key sees ≥ 15 RPM (their published overflow threshold).
3. **`prompt_cache_retention`**: set to `"24h"` for any model on the supported list (gpt-5.x, gpt-4.1) when `hints.expectedTurns ≥ 4` or session age suggests it; otherwise leave default `in_memory`. (Pricing is the same per OpenAI's docs, so the only cost is the model-support gate.)

**`emit`**: standard Responses API request. `messages` array has all ref-pool content rendered into the leading `system` message, then turn messages with the §5 ordering preserved per message. Images: ensure `detail` parameter is identical across requests (their docs flag this as a cache breaker).

**`refresh`**: returns `null`. OpenAI has no prewarm primitive; cache warmth is maintained by real traffic, which is why `prompt_cache_key` matters more than anything else here.

**`parseUsage`**: reads `usage.prompt_tokens_details.cached_tokens`. Single number, no creation/read split — we report it as `cache_read` and infer `cache_creation = prompt_tokens − cached_tokens − new_tail_tokens`.

### 8.3 DeepSeek adapter (V3+)

**Capabilities**: fully automatic disk cache, persists at request boundaries / common-prefix detection / fixed-token intervals. **No control surface at all**, except that the prompt structure determines what becomes a cacheable prefix unit.

**`planMarks`** returns empty.

**`emit`**: single OpenAI-compatible chat-completions request. The work is entirely in the *layout*:

1. Render ref-pool into `system`. Per DeepSeek's "Example 2" (long-text Q&A), large stable content placed in the system or at the head of the user message becomes a cache prefix unit on second use. Putting it in `system` (rather than user) means it can be shared across user turns.
2. The `pin → fold → drop` ordering inside each message ensures every emitted prefix unit ends on stable content. Their "request boundary" persistence rule means each user turn boundary becomes a candidate cache unit; we want that boundary to fall right after the durable content.
3. Avoid scattering small dynamic fields (timestamps) into the system; DeepSeek's prefix unit is exact-match, and a single varying token in the system bills you for the whole prefix.

**`refresh`**: `null`.

**`parseUsage`**: reads `usage.prompt_cache_hit_tokens` and `usage.prompt_cache_miss_tokens`. Both directly available; report as `cache_read` and `input` respectively, `cache_creation` not separately reported (DeepSeek's pricing folds the write into the miss tokens).

### 8.4 vLLM adapter (open-source inference, bidirectional)

**Capabilities**: Automatic Prefix Caching (APC) via radix-hashed KV blocks (default 16-token blocks, LRU eviction); enabled by `--enable-prefix-caching` on the server. Exposes prefix-cache hit/miss in responses. Supports `cache_salt` (request-level namespacing) and KV offloading (GPU → CPU → disk) on recent builds.

**Bidirectional ops** (vLLM-specific, beyond what closed APIs allow):

| TELOS op → vLLM action | Mechanism |
|---|---|
| `Mark(pin)` → server-side **pin** | request extension `cache_policy: {"pin_prefix_until": <block_idx>}`; LRU eviction skipped for pinned blocks |
| `Refresh` → **prewarm** | `max_tokens: 1, ignore_eos: false` request that touches the BP-bearing prefix; vLLM materializes the KV blocks without serving real output |
| `Fold` → **co-op compact** | `cache_policy: {"evict_span": [start_block, end_block]}`; the bridge replaces the span with the summary block in the next request; vLLM frees the old blocks and recomputes only the new (shorter) tail |
| `Place(slug, namespace)` → **cache_salt** | `cache_salt: "<sessionId>"`; isolates sessions on shared deployments without hash collisions |
| `Query` (read-only) → **prefix probe** | `HEAD /v1/cache/prefix?hash=<sha256>`; returns `{hit: bool, last_block: int}`; bridge can decide to short-circuit `Refresh` |

**`planMarks`**: returns a `pin_until` index pointing at the last `pin` block in `system` (the durable prefix) and a 5m-equivalent rolling anchor at the last non-`drop` block in the latest message. vLLM has no TTL; "5m" maps to "unpinned, eligible for LRU" and "1h" maps to "pinned".

**`emit`**: OpenAI-compatible `/v1/chat/completions` payload + the `cache_policy` and `cache_salt` extension fields. Ordering rules (§5) still apply because APC is exact-prefix-match at block granularity — a single varying token at the start invalidates the whole prefix.

**`refresh`**: real implementation. Sends the prewarm request and uses the prefix-probe op to verify the materialization landed.

**`parseUsage`**: vLLM returns `usage.prompt_tokens` and (with `--collect-detailed-traces`) `usage.cached_tokens`. Cache creation = `prompt_tokens - cached_tokens` for new tail.

### 8.5 SGLang adapter (open-source inference, bidirectional)

**Capabilities**: RadixAttention — tree-based prefix cache with cache-aware scheduling (requests are reordered to maximize prefix sharing, not just opportunistically matched). HiCache (hierarchical cache) tiers KV across GPU/CPU/disk with explicit hints. `--enable-cache-report` exposes `cached_tokens` per response. Better fork/branch semantics than vLLM, which matters for subagent workloads.

**Bidirectional ops** (a superset of vLLM's, plus cache-aware scheduling):

| TELOS op → SGLang action | Mechanism |
|---|---|
| `Mark(pin)` → **lock_radix** | request extension `cache_control: {"lock_radix_path": true, "path_hash": <sha>}`; pins the radix path against eviction |
| `Refresh` → **prewarm** | `max_tokens: 1` plus `cache_control: {"prewarm_only": true}`; SGLang fills the radix path without scheduling generation |
| `Fold` → **fork-and-replace** | `cache_control: {"fork_from_path": <sha>, "replace_suffix": <summary>}`; SGLang clones the radix prefix, swaps the suffix, and the next request hits the new path with full prefix reuse |
| `Place(slug, tier)` → **HiCache hint** | `cache_control: {"prefer_tier": "gpu"\|"cpu"\|"disk"}`; pin band → `gpu`, fold band → `cpu`, drop band → unset |
| `Query` → **radix lookup** | `POST /v1/cache/lookup` with token-prefix sha; returns `{depth: int, tier: str}` |
| **(SGLang-only)** `Hint(scheduling_affinity)` → **CASS** | `cache_control: {"affinity_key": <sha>}`; cache-aware shortest-job scheduler co-locates same-prefix requests on the same worker |

**`planMarks`**: like vLLM, returns pin/unpin indices. Additionally returns `affinity_key` (hash of toolset + system pin + ref-pool slugs) so the SGLang scheduler can batch sibling requests.

**`emit`**: OpenAI-compatible payload + `cache_control` extension. The `fork_from_path` op for `Fold` is the one place where TELOS emits a *non-OpenAI-compatible* request body — because no closed API has anything resembling it. Adapter falls back to vanilla emit if SGLang version doesn't advertise the capability.

**`refresh`**: real implementation, reuses the prewarm + lookup pair.

**`parseUsage`**: SGLang returns `usage.prompt_tokens`, `usage.cached_tokens`, and (with HiCache) `usage.cache_hierarchy_breakdown: {gpu, cpu, disk}`. We surface the breakdown in `UsageReport.raw` for diagnostics; the headline `cache_read` is the sum.

### 8.6 Why open-source engines unlock the **bidirectional** half of TELOS

The closed APIs (Anthropic / OpenAI / DeepSeek) are **uni-directional**: TELOS emits a request, the engine decides what to cache, TELOS reads the receipt in `usage`. There is no way to ask "do you still have my prefix?" or "please drop this span and accept a summary in its place."

vLLM and SGLang are **bi-directional**: TELOS can both *read* the cache state (probe, hierarchy breakdown) and *write* it (pin, evict-span, fork-and-replace, tier hint). This is what makes the `Fold` primitive a real co-op compact instead of a client-side rewrite — the server keeps the prefix's KV blocks and only re-encodes the (shorter) summary tail.

Concretely, three things that are impossible on closed APIs become trivial here:

1. **Verifiable refresh**: `Refresh` followed by a probe confirms the prefix is hot, instead of blindly burning a `max_tokens:0` round-trip and hoping.
2. **Zero-recompute Fold**: `fork-and-replace` re-uses the cached KV for the durable prefix verbatim; the only new compute is the summary block. On a long agent session this is the difference between a 5× cache hit and a full re-prefill.
3. **Tiered eviction**: `fold` blocks land on CPU/disk instead of competing with `pin` blocks for GPU HBM, so a long-tail of foldable history doesn't push the pin band out of the GPU cache.

### 8.7 Capability matrix at a glance

| Capability | Anthropic | OpenAI | DeepSeek | **vLLM** | **SGLang** |
|---|:---:|:---:|:---:|:---:|:---:|
| Explicit breakpoints | ✓ (≤4) | — | — | ✓ (pin index) | ✓ (radix lock) |
| TTL control | 5m / 1h | `in_memory` / `24h` | — | pin / unpinned | pin + tier |
| Prewarm | `max_tokens:0` | — | — | ✓ | ✓ (`prewarm_only`) |
| Routing affinity | — | `prompt_cache_key` | — | `cache_salt` | `affinity_key` (CASS) |
| **Cache probe (read)** | — | — | — | ✓ | ✓ |
| **Span eviction (write)** | — | — | — | ✓ | ✓ |
| **Fork-and-replace (Fold)** | — | — | — | partial | ✓ |
| **Tier hint (HiCache)** | — | — | — | — | ✓ |
| Lookback window | 20 blocks | prefix-only | prefix unit | block radix | token radix |

Every adapter implements the **same** `EngineAdapter` interface plus an optional `BidirectionalEngineAdapter` mixin (`probe`, `evictSpan`, `forkAndReplace`). Bridge code calls the mixin only when present — closed-API adapters silently no-op.

---

## 9. End-to-End Walkthrough

```
turn 5, OpenClaw on Claude Sonnet 4.6
─────────────────────────────────────

1. OpenClaw POST /v1/messages → harness plugin
     → splits user message into (pin: query, drop: <environment_info>)
     → prior tool_result is fold
     → returns TelosIR

2. Bridge:
     - Place(query, pin) ✓ already
     - Pin("login.py", <4000 lines>) — already in pool
     - Mark() → AnthropicAdapter.planMarks(ir):
           BP-T (tools end, 1h)
           BP-S (system pin end, 1h)
           BP-R (ref-pool end, 1h)
           BP-X (latest assistant block, 5m)
     - no Fold this turn (foldable region < threshold)

3. AnthropicAdapter.emit:
     - tools: [...keysorted...] with cache_control on last
     - system: [pin blocks][fold blocks (ref-pool)] with cache_control at BP-R
     - messages: [..., user{pin: query, drop: env_info}]
                 → drop block emitted last in user msg, no cache_control on it
     - last assistant message gets cache_control (5m) for BP-X

4. Anthropic returns; parseUsage:
     {input: 80, cache_read: 21043, cache_creation: 250,
      breakdown: {ephemeral_1h: 0, ephemeral_5m: 250}}

5. Bridge updates session counters; if cache_creation accumulates past
   threshold, schedule a Fold on next emit.
```

Same IR re-routed to DeepSeek would skip steps 2's Mark and step 3's `cache_control`s, emit the identical block ordering, and rely on DeepSeek's auto-cache to pick up the prefix unit at the user-turn boundary. No harness change, no bridge change.

---

## 10. Verification

TELOS ships with three test suites, all runnable from `agent-janus/bridge`:

| Suite | What it asserts |
|---|---|
| `telos/invariants.spec` | §5 ordering holds after every primitive; ref-pool slugs frozen; `drop` blocks emitted last; tool-input keys sorted |
| `telos/engine-adapters.spec` | Recorded fixtures for each engine: same IR → byte-stable wire request; usage parsing round-trip |
| `telos/efficiency.bench` | On `swe-bench-pure-python-sample`, gates: `tokens_per_resolved` ≤ baseline, `resolved_rate` ≥ baseline, `cache_read_total` ≥ baseline (CLAUDE.md north-star) |

The first suite runs in the type checker for IR-level invariants and in unit tests for runtime checks. The third suite is the only one that gates a release.

---

## 11. Mapping to Existing Code

| TELOS concept | Lives in |
|---|---|
| `TelosIR`, `TelosBlock`, bands | new `agent-janus/bridge/src/telos/ir.ts` |
| 5 primitives | new `agent-janus/bridge/src/telos/bridge.ts` |
| Harness plugins | extends `agent-janus/plugins/harness/{anthropic-messages,openai-responses}` |
| Engine adapters | extends `agent-janus/plugins/engine/{anthropic-passthrough,openai-passthrough}`; new `deepseek-passthrough` |
| Ref-pool registry | new `agent-janus/bridge/src/telos/ref-pool.ts` |
| Anthropic Mark planner | new `agent-janus/bridge/src/telos/marks-anthropic.ts` |
| User-message splitter (drop envelope) | reuse / extend existing `efficiency/prefix-normalization/system.ts :: stripDynamicFields` |

TELOS does **not** require any new files outside `bridge/src/telos/` and the named adapters. The existing `efficiency/` strategy modules from the Janus design (A through F) are not part of TELOS's surface area; they remain available as bridge implementation details, but the contract above does not mention them.

---

## 12. What TELOS Drops From Janus (Deliberately)

| Janus concept | TELOS disposition | Why |
|---|---|---|
| Tri-color names (`need_cache` / `need_cache_foldable` / `no_cache`) | renamed `pin` / `fold` / `drop` | Shorter, action-verb, harder to confuse |
| 4 named breakpoints (BP0..BP3) | implicit, derived per emit by `Mark` | Slot management is engine-specific; not a cross-engine concept |
| `X-Janus-Marking` headers, `X-Janus-Span-Map` | dropped | No engine accepts them. Co-op compact moves to a future capability bit. |
| `/v1/cache/fold` endpoint design | dropped from v1 | Speculative until vLLM/SGLang patches land. Re-added when there's a server to talk to. |
| 11 invariants (I1..I11) | one (§5) plus 3 cheap asserts | The other 10 are corollaries or implementation details, not contract |
| Adaptive refresh formula derivation | one sentence in §8.1 | Detailed derivation is intentionally elided; redo from first principles if needed |
| Two compact modes (fallback / cooperative) | one mode (Fold), with `coopHint` parameter for forward-compat | Cooperative is a future capability, not a parallel architecture |

The Janus-Prompt v2 design notes (cost-model derivation, multi-round review history) are no longer maintained — they have been folded into this document and into [ARCHITECTURE.md](ARCHITECTURE.md). TELOS is the only shipping protocol.

---

## 13. Open Items

| # | Item | Notes |
|---|---|---|
| O1 | DeepSeek's "fixed-token-interval" persistence behavior is undocumented in detail; needs empirical probing to know whether ref-pool placement actually wins | Add to `telos/efficiency.bench` |
| O2 | Anthropic's automatic-caching mode interacts with explicit BPs by consuming a slot; TELOS uses 4 explicit slots and disables automatic. Need to verify no 400 in mixed-mode regressions. | Adapter conformance test |
| O3 | Hermes subagent IRs: child sessions need their own ref-pool, but file content reused across parent+child should ideally share a slug. Cross-session pool not in v1. | Future |
| O4 | OpenAI `prompt_cache_key` overflow at 15 RPM: TELOS's hash currently uses the toolset+system+ref-slug set, which may be too granular for low-traffic agents. Need a coarsening rule. | Adapter heuristic |
| O5 | vLLM/SGLang `cache_policy` / `cache_control` extension fields are not yet upstream-stable; each release may rename keys. Adapters should version-gate via a startup capability fetch (`GET /v1/capabilities`) rather than blind-emit. | Adapter conformance test |
| O6 | SGLang `fork-and-replace` semantics: when the summary length exceeds the original span, the radix path becomes net-longer; bridge should reject `Fold` plans where `len(summary) > 0.5 * len(span)` to keep the op profitable. | Bridge guard |

---

## 14. One-Line Summary

> **TELOS** = three layers (harness · bridge · engine), one IR (tri-banded blocks + ref-pool), one ordering invariant (`pin → fold → drop`), five primitives (`Place · Pin · Mark · Fold · Refresh`), three engine adapters that degrade gracefully from Anthropic's full control to DeepSeek's automatic prefix matching — same IR, same harness code, every engine's cache used to its actual ceiling.
