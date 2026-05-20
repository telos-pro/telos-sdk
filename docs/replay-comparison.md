# Replay comparison — record / replay

> Record a real session, then replay it once for each of several switch combinations, to get a **controlled** cost comparison.

TELOS offers two comparison methods. This document covers replay; for the other one, "dual session", see the comparison at the end.

---

## 1. Why replay

To answer "how much does turning on TELOS / RTK actually save", the most intuitive approach is to run the same task twice with different switches — but the two agent trajectories will diverge (sampling randomness, different tool results leading to different downstream decisions), so the cost delta is mixed with noise unrelated to the optimization. With a sample size of 1, this delta is essentially untrustworthy.

replay **pins the trajectory down**: record one real session to get a "request sequence", then for each mode replay this **byte-identical** sequence of requests. The only variable is the switch itself — this is a controlled experiment, and the comparison numbers are clean, reproducible, and CI-ready.

The cost is also low: one complete real session + a string of cheap prefill calls per mode, an order of magnitude or two cheaper than "running a full agent session for each of N modes × K times and averaging".

---

## 2. How it works

### 2.1 Record only requests, not responses

Anthropic's `/v1/messages` is stateless: in the N-th turn's request body, `messages[]` already contains all assistant replies and tool_results from the previous N−1 turns. So the "request sequence" itself is a complete, replayable trajectory, and the assistant responses don't need to be stored separately (which also avoids writing model output to disk).

By default the proxy records each call's **original request** (client→proxy, before RTK filtering, before TELOS rewriting) into the corpus `~/.telos/corpus/<session>.jsonl`. What's recorded is the "canonical input" — on replay each mode re-derives the wire from the same canonical input, so the comparison is fair.

> RTK filtering happens only on the proxy→upstream leg, and does not change the agent's local conversation state.
> So the agent's next turn still sends the unfiltered full tool_result — what the corpus records
> is always the unfiltered canonical input.

### 2.2 Replay measures only prefill cost

For each mode, `telos replay`:

1. takes the original request of each turn from the corpus;
2. (optionally) injects a cache-isolation prefix (see 2.3);
3. if `mode.rtk` is on → runs the RTK tool-result filtering;
4. if `mode.telos` is on → runs the TELOS pipeline to mark cache_control / ref-pool;
5. forces `max_tokens` to `1`, strips `stream` / `tool_choice` / `thinking`,
   and sends to upstream;
6. takes only the response's `usage` and writes one usage_log record.

Forcing `max_tokens=1` is because we only care about the `cache_read` / `cache_write` billing on the prompt / prefill side — output generation is deliberately neutered, producing almost no output cost. What you get is the cache numbers **actually reported** by Anthropic, not an estimate.

### 2.3 Cross-mode cache isolation

Anthropic's prefix cache is keyed by "prefix content + organization", with no routing key. If you replay `telos` first, then `both`, and the two have an identical cacheable prefix, the latter freeloads on the cache the former warmed up, and the comparison numbers get polluted.

The default countermeasure: for each mode, inject a unique prefix block `[telos-replay ns=<session>/<mode>]` at the very front of the `system` segment. The modes' prefixes thus differ from each other → caches are independent. This block is only ~10 tokens, equal-length across modes, and doesn't affect the relative comparison.

`--no-cache-isolation` disables the injection.

---

## 3. Usage

```bash
# 1. run a few real sessions (the proxy records into the corpus by default)
telos gateway start --usage-log ~/.telos/usage.jsonl
#    ... do work with the agent ...

# 2. see which sessions are in the corpus
telos replay --list

# 3. replay: by default all 4 modes run
telos replay --session telos-ab12cd34
#    or pick modes:
telos replay --session telos-ab12cd34 --modes none,both
#    or record an asciinema cast of the savings dashboard updating live:
telos replay --session telos-ab12cd34 --cast

# 4. view the comparison (the dashboard "A/B comparison" panel, cards tagged with the `replay` badge)
telos dashboard --usage-log ~/.telos/usage.jsonl --out savings.html
```

### `--cast` — record the dashboard changing

`telos replay --cast [PATH]` writes an [asciinema](https://asciinema.org) v2
cast (default `~/.telos/replay-cast.cast`) while the replay runs. After every
turn it re-aggregates the usage so far and emits one full-screen frame of the
savings dashboard — so on playback you watch `cache_read`, token cost and saved
dollars fill in per mode, turn by turn:

```bash
telos replay --session <id> --cast              # → ~/.telos/replay-cast.cast
telos replay --session <id> --cast demo.cast    # → ./demo.cast
asciinema play ~/.telos/replay-cast.cast        # play it back
```

The cast runs on a virtual clock (total playback ≈ 40 s regardless of how long
the real replay took), and each frame replaces the previous one in place, so it
plays as a single live-updating panel.

Replay requires `ANTHROPIC_API_KEY` (or `--api-key`). Results are appended to `--usage-log`
(default `~/.telos/usage.jsonl`), with `compare_group` = the original session id, and the dashboard
uses this to display the modes of the same session side by side.

---

## 4. Boundaries — what replay cannot measure

replay is a controlled experiment, and the price of being controlled is that it **only holds when the trajectory is fixed**:

- **It measures "the cost of the same conversation under different encodings", not "the cost of the same task under different configurations".** It cannot capture second-order effects — for example, after RTK shortens the tool results, in a real run the agent's next step might, because the context is different, make a different (better or worse) decision, which in turn changes the subsequent turn count and total cost. replay blocks off this branch.
- **It measures prefill / cache billing, not end-to-end task cost.** `max_tokens=1` means output cost is not counted; the output cost of a real task must be tallied separately.
- **The cache-isolation prefix is a deliberately introduced artifact.** It is harmless to the relative comparison (equal-length across modes), but the absolute token count is ~10 tokens/turn more than reality.
- **It cannot replace a real run.** To prove an end-to-end claim like "with TELOS the agent is cheaper overall", you can only run independent sessions.

Think of replay as an upgrade to "the *computed* 'TELOS off' counterfactual that the current dashboard shows" — replacing it with a *measured* counterfactual. For measuring the mechanism (whether cache markers + filtering reduce billed tokens), this is exactly the right scope; for measuring task outcomes, use the dual session below.

---

## 5. Comparison: replay vs dual session

| | Cost | Controlled variables | Suitable claims |
|---|---|---|---|
| **replay** | 1 real session + cheap prefill | good (turns pinned) | "for a given workload, the token bill drops by X" |
| **dual session** | N×K complete agent sessions | poor (trajectory diverges) | "with TELOS, the agent is cheaper overall" |

The dual-session approach: start two independent agent sessions with the same user input, each carrying a different
`X-Telos-Mode` + the same `X-Telos-Compare-Group` header; the same "A/B comparison" panel of the dashboard
places them side by side (cards tagged with the `live A/B` badge).

Use replay for everyday comparisons and regression baselines; use dual session for occasional end-to-end validation.

---

## 6. Privacy

The proxy enables session recording by default, recording the **original request body** — which contains your prompts,
code, and file contents. The corpus lands in `~/.telos/corpus/`.

- To avoid writing to disk: `telos proxy --no-record`.
- To change the directory: `telos proxy --corpus-dir <path>`.
- The corpus grows with sessions and is currently not auto-cleaned; manage it yourself.
