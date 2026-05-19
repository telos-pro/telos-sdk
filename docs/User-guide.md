# TELOS User Guide

> An end-to-end manual: from installation to integrating an agent, to multi-turn observability and tuning.
>
> For the protocol level see [`2026-05-06-telos-protocol.md`](2026-05-06-telos-protocol.md);
> for the change history see [`CHANGELOG.md`](../CHANGELOG.md) in the root.

---

## 1. Decision tree: which integration path should you use?

```
Can you modify the agent's source code / import site?
│
├─ Yes (a self-built Python agent / vendored code like mini_swe_runner)
│      ↓
│   Path A — SDK Transport
│   import TelosAnthropicTransport / TelosOpenAITransport
│   Pros: full typed responses, same lifecycle as the agent process, direct to debug
│   Cons: each agent needs its own import change; streaming not yet wrapped
│
└─ No (npm-globally-installed Claude Code, closed-source binary, multiple agents on a shared host)
       ↓
    Path B — HTTP reverse proxy (gateway)
    telos gateway brings up 7171 locally; the agent sets ANTHROPIC_BASE_URL=http://127.0.0.1:7171
    Pros: zero intrusion, survives agent upgrades, multiple agents share one proxy
    Cons: one extra process layer; headers outside the allowlist are dropped
```

The two paths are **functionally equivalent**:
- the same TELOS pipeline (`process_anthropic_request` / `bridge.emit_with_plan`)
- the same multi-turn state accumulation (`BridgeSessionState`)
- the same `cache_control` injection / canonical ordering
- the same usage accumulation fields in the logs

The differences are only at the operational level: process boundary / error handling / streaming.

---

## 2. Installation

One-line install:

```bash
pip install telos-sdk
# or Homebrew (see packaging/, available after the tap is published):
# brew install telos-sdk
```

Development install from source:

```bash
cd /Users/george/Code/telos-sdk
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

[`pyproject.toml`](../pyproject.toml) maps the project root directory to the `telos` package. After install:

```bash
python -c "import telos; print(telos.__file__)"
# .../telos-sdk/__init__.py

telos --help
# usage: telos [<subcommand>] [...]
```

Requires Python ≥ 3.10. Depends on `anthropic ≥ 0.49`, `openai ≥ 1.72`, `aiohttp ≥ 3.10`.

### 2.1 One-command integration (recommended)

```bash
telos init
```

`telos init` with no arguments will: auto-detect which harness CLIs are installed locally
(claude-code / codex / openclaw / hermes) → inject config pointing at the local gateway into each
→ start the gateway in the background → print the gateway and dashboard addresses.

Afterward:

```bash
telos              # pick a harness and enter its CLI (telos alias <harness> sets the default)
telos dashboard    # open the live dashboard in the browser
telos mode both    # switch the optimization gear, hot-reloading the running gateway
telos gateway status   # check the gateway's run status
```

---

## 3. Path A: SDK Transport (in-code integration)

### 3.1 Anthropic client — Claude Code / Openclaw / Hermes / self-built agents

Replace `anthropic.Anthropic()` with `TelosAnthropicTransport`; all other `.messages.create()` calls stay the same:

```python
# before
import anthropic
client = anthropic.Anthropic()

# after
from telos.scripts.telos_anthropic_transport import TelosAnthropicTransport
client = TelosAnthropicTransport(
    session_id="my-agent-session",       # use the same id for the same conversation
    usage_log="logs/usage.jsonl",
    prompt_trace_log="logs/trace.jsonl",
    # harness_name="hermes",             # leave unset for auto-detect
)

# the call is completely unchanged
response = client.messages.create(
    model="claude-opus-4-7",
    max_tokens=8192,
    system=[{"type": "text", "text": "You are an engineer."}],
    tools=[...],
    messages=[...],
)
print(response.content[0].text)
```

Constructor parameters:

| Parameter | Default | Description |
|---|---|---|
| `api_key` | `$ANTHROPIC_API_KEY` | Anthropic API key |
| `base_url` | `None` (uses the SDK default) | can point at a local proxy for debugging |
| `session_id` | `"telos-session"` | keep the same id for the same conversation; the key for multi-turn cache accumulation |
| `harness_name` | `None` (auto-detect) | force `"openclaw"` / `"hermes"` |
| `engine_name` | `"anthropic"` | usually left alone |
| `usage_log` | `None` | jsonl path; one line appended per call (normalized usage) |
| `prompt_trace_log` | `None` | jsonl path; records IR layout / plan / accumulation state and other diagnostics |
| `session_state` | `None` (new'd internally) | pass explicitly when multiple transports share one conversation |

Harness auto-detection: system contains `<system-reminder>` or `<command-message>`, or a message has a `thinking` block → picks `hermes`; otherwise `openclaw`.

### 3.2 OpenAI client — telos / mini_swe_runner / self-built OpenAI-shape agents

```python
# before
from openai import OpenAI
client = OpenAI(base_url="https://openrouter.ai/api/v1")

# after
from telos.scripts.telos_transport import TelosOpenAITransport
client = TelosOpenAITransport(
    base_url="https://openrouter.ai/api/v1",
    session_id="telos-session",
    usage_log="logs/usage.jsonl",
    engine_name="deepseek",              # or "openai"
    harness_name="telos",                # fixed
)

response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[...],
    tools=[...],
)
```

### 3.3 Sharing one conversation across transports

If a conversation is handled by multiple transport instances (e.g. the client is rebuilt after a retry), pass `BridgeSessionState` in explicitly:

```python
from telos.bridge import BridgeSessionState
from telos.scripts.telos_anthropic_transport import TelosAnthropicTransport

shared = BridgeSessionState()
t1 = TelosAnthropicTransport(session_id="conv-1", session_state=shared)
# ... t1 errors and is destroyed ...
t2 = TelosAnthropicTransport(session_id="conv-1", session_state=shared)
# t2 can see the ref-pool and R8 counts accumulated by t1
```

---

## 4. Path B: HTTP reverse proxy (zero-intrusion integration)

### 4.1 Starting the gateway

`telos init` already starts the gateway in the background automatically. You can also manage it manually:

```bash
telos gateway start                       # background start (host/port/mode taken from ~/.telos/config.json)
telos gateway start --port 7171           # specify the port (and write it back as the new default)
telos gateway status                      # check the run status
telos gateway restart                     # restart
telos gateway stop                        # stop
telos gateway start --foreground          # run in the foreground, blocking (debugging / containers)
```

After a background start, the state is recorded in `~/.telos/gateway.json` and the log in `~/.telos/gateway.log`.

> The old `telos proxy ...` (foreground blocking) is still kept as a hidden alias, fully flag-compatible.

After starting, the log (`~/.telos/gateway.log`) outputs:

```
TELOS gateway listening on http://127.0.0.1:7171 → https://api.anthropic.com
usage log → /Users/.../usage.jsonl
```

The gateway accepts all Anthropic protocol paths; `/v1/messages` is rewritten by TELOS, everything else is passed through unchanged.

> **harness auto-detection**: the gateway automatically determines which harness each request belongs to, with no manual specification needed.
> Besides the main conversation, Claude Code also sends auxiliary requests via Haiku (conversation-title generation, new-topic detection, etc.).
> These requests have no tools and no `<system-reminder>` tag — the gateway identifies them via the HTTP `User-Agent`
> header (`claude-cli/...`) and remembers the identification result per client, so auxiliary requests are also
> correctly attributed to Claude Code, and won't be mistakenly shown as `openclaw` on the dashboard.

### 4.2 Integrating Claude Code

`telos init` automatically integrates the harnesses it detects. You can also integrate Claude Code only:

```bash
telos init --harness claude-code
```

It writes into the `env` field of `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:7171",
    "__telos_installed": true
  }
}
```

Afterward any process that starts Claude Code automatically uses the local gateway. **No change to the npm package**, **no change to PATH**, and **`npm update` won't lose it**.

To undo / check status:

```bash
telos init --harness claude-code --uninstall   # restore the state before install
telos init --harness claude-code --status      # view only, don't modify files
```

### 4.3 Integrating other clients

codex / openclaw / hermes are injected via environment variables (set automatically when `telos <harness>` starts the child process).
Any client that respects `ANTHROPIC_BASE_URL` can also use `telos init --harness generic` to get a set of
manual export instructions.

### 4.4 Full CLI reference

```
telos                        bare command: pick a harness and enter its CLI
telos <harness>              enter a harness directly (claude-code / codex / openclaw / hermes)
telos alias <harness>        set the harness that bare telos enters by default

telos init [options]
  --harness {claude-code,codex,openclaw,hermes,generic}
                       operate on the specified harness only (default: auto-detect all)
  --gateway-url URL    gateway address (default taken from ~/.telos/config.json)
  --uninstall          restore the state before install
  --status             view only, don't modify files
  --no-gateway         only inject config, don't start the gateway automatically

telos gateway [start|stop|status|restart] [options]
  --host HOST          listen address (default 127.0.0.1)
  --port PORT          listen port (default 7171)
  --mode {none,telos,rtk,both}   default optimization gear
  --usage-log PATH     append one jsonl line per call
  --foreground         run in the foreground, blocking (no backgrounding)

telos mode [none|telos|rtk|both]   switch the optimization gear; hot-reloads the running gateway and persists it
telos dashboard [--static] [--no-open]   open the dashboard in the browser

telos proxy [options]        hidden alias: runs the gateway in the foreground, blocking; fully compatible with the old flags
```

For `telos gateway` / `telos init`, any host/port/mode not passed defaults to `~/.telos/config.json`;
explicitly passed values are written back as the new default.

---

## 5. Multi-turn state accumulation (a key capability)

The ref-pool persistence and the R8 adaptive refresh mentioned in §4 / §6 of the TELOS protocol design doc both depend on **cross-turn state accumulation**. This section explains the mechanism and how to observe it.

### 5.1 Where the state lives

```python
@dataclass
class BridgeSessionState:
    refpool: RefPool          # ref-pool slug registry (kept across turns once frozen, kept across folds too)
    stats: _SessionStats      # cumulative_cache_creation + real_requests_since_refresh
```

### 5.2 Held automatically on Path A

A `TelosAnthropicTransport` / `TelosOpenAITransport` instance = one session. `__init__` creates a `BridgeSessionState` internally; each `_do_create` passes it to `Bridge`, and when the response returns `bridge.absorb_usage(...)` accumulates the cache_creation.

Access: `transport.session_state.stats.cumulative_cache_creation`.

### 5.3 Held automatically on Path B, keyed by session-id

Inside the proxy, `_SessionRegistry` (an OrderedDict LRU, default 10000) holds the state keyed by session_id. session_id derivation priority:

1. the `x-telos-session` HTTP header (explicit override)
2. `metadata.user_id` (an Anthropic SDK built-in field)
3. `blake2b(api_key + system + tools + messages[0])` → `telos-<16 hex>`

The semantics of the derivation rule:
- the N turns of the same conversation (only appending to the tail of `messages[]`) → the same session_id ✓
- a different initial prompt (`messages[0]` changes) → a different session_id ✓
- two users with different API keys → different session_ids ✓

Once the LRU cap is exceeded the oldest session is evicted, with an INFO log emitted.

### 5.4 Observing accumulation

usage_log adds a `cumulative` block per line:

```json
{
  "session_id": "telos-46bbb9d3d3df581e",
  "call_index": 4,
  "harness": "openclaw",
  "normalized": {"raw_input": 50, "cache_read": 6500, "cache_write": 0, "output": 5},
  "cumulative": {
    "cache_creation": 6500,
    "real_requests_since_refresh": 4,
    "refpool_slugs": ["system-doc-1"]
  }
}
```

`cache_creation` increasing monotonically shows accumulation is working; the `refpool_slugs` array should not grow repeatedly across turns (the same document should not be registered again and again).

### 5.5 Disabling accumulation (a fresh Bridge per turn)

Not passing `session_state`, or restarting the proxy, makes the behavior fall back to newing a fresh state per turn. This was the default behavior before 1.0, and it does not break wire bytes.

---

## 6. Troubleshooting

### 6.1 Proxy returns 500 / the SDK retries 10 times

Older TELOS versions threw an exception → the proxy returned 500. It now **degrades to passthrough by default**:
- proxy log on the first failure: full traceback + `"falling back to passthrough"`
- subsequent failures: a single WARNING line
- the wire is a raw passthrough (no `cache_control` rewrite), but the response is normal

To make a TELOS failure blow up explicitly and immediately during the dev stage, start with `--strict`:

```bash
telos proxy --strict
```

### 6.2 `Band order violated`

If you see:

```
TelosInvariantError: Band order violated in messages[0]:
  block 'msg0/blk3/q' has band 'pin' after a higher-band block.
```

it means the harness output violates §5. **This is a TELOS-side bug, not a problem with your request.**

The most common cause: the harness doesn't know about some content-block type, or a multi-part concatenation didn't sort by band. Currently both openclaw and hermes use `enforce_band_order` as a fallback; if you extend a new harness, remember to run the message blocks through `enforce_band_order(blocks)` at the end.

### 6.3 Multi-turn cache_creation always 0

If `cumulative.cache_creation` in usage_log is always 0, it may be:

| Symptom | Check |
|---|---|
| session_id differs every time for the same conversation | whether the headers are missing `x-api-key`; whether `messages[0]` really stays unchanged |
| `real_requests_since_refresh` is also always 1 | `session_state` wasn't passed (Path A) or the proxy was restarted (Path B) |
| the `cache_read` number is also 0 | the Anthropic model doesn't support prompt caching, or `cache_control` didn't take effect |
| `refpool_slugs` is empty | no large document triggered the ref-pool (default 2KB threshold) |

### 6.4 Headers not passed through

The proxy forwards only an allowlist: `x-api-key` / `authorization` / `anthropic-version` / `anthropic-beta` / `anthropic-dangerous-direct-browser-access` / `user-agent`.

To pass through other headers, currently the only way is to edit `_FORWARD_HEADER_WHITELIST` ([proxy/server.py](../proxy/server.py)). The SDK transport path is not subject to this restriction.

### 6.5 Streaming responses (on by default in Claude Code)

- Path A (SDK transport): currently `messages.create(stream=True)` does no TELOS processing and calls the underlying SDK directly. **Avoid streaming on the SDK transport path.**
- Path B (proxy): full SSE support, side-channel parsing of `message_start` / `message_delta` to extract usage fields.

---

## 7. Observability: a field cross-reference for the two logs

### 7.1 `usage_log` (shared by proxy + SDK transport)

```jsonc
{
  "session_id": "telos-...",          // stable across turns
  "call_index": 1,                     // increments within the process
  "harness": "openclaw" | "hermes" | "telos" | "passthrough",
  "n_slots": 3,                        // the number of slots in the EmitPlan
  "slots": ["BP-T", "BP-S", "BP-X"],
  "latency_s": 1.234,
  "streaming": true | false,
  "status": 200,                       // upstream HTTP status
  "raw_usage": {...},                  // the original wire usage fields
  "normalized": {                      // unified to 4 fields
    "raw_input": 50,
    "cache_read": 6500,
    "cache_write": 0,
    "output": 5
  },
  "cumulative": {
    "cache_creation": 6500,
    "real_requests_since_refresh": 4,
    "refpool_slugs": ["system-doc-1"]
  }
}
```

### 7.2 `prompt_trace_log` (SDK transport only)

Contains IR layout snapshots, plan details, cross-call prefix overlap and other diagnostics — heavier-grained than usage_log, for deep analysis of cache behavior. For the exact fields see [scripts/telos_anthropic_transport.py](../scripts/telos_anthropic_transport.py).

### 7.3 A few common commands for reading the logs

```bash
# view the per-turn cache_read delta (verify multi-turn hits)
jq -c '{call: .call_index, cache_read: .normalized.cache_read, cum: .cumulative.cache_creation}' \
    < ~/.telos/usage.jsonl

# check whether the ref-pool is stable (it should not keep changing)
jq -c '.cumulative.refpool_slugs' < ~/.telos/usage.jsonl | sort -u

# find all requests that degraded to passthrough
jq -c 'select(.harness == "passthrough")' < ~/.telos/usage.jsonl
```

---

## 8. Testing

The full test matrix:

```bash
for t in test_smoke test_harness_multiblock \
         test_proxy_pipeline test_proxy_server test_proxy_session_id \
         test_proxy_accumulation test_bridge_session_state \
         test_sdk_transport_accumulation test_init_claude_code; do
  python -m telos.tests.$t
done
```

Each suite is readable on its own; for the suite-name mapping see the docstrings under [tests/](../tests/).

---

## 9. Known limitations

| Limitation | Explanation | Impact |
|---|---|---|
| SDK transport doesn't wrap `.stream()` | the Anthropic SDK's streaming context manager isn't hooked | avoid `stream=True` when using the SDK transport |
| proxy header allowlist | only 6 headers are passed through | custom headers are silently dropped |
| proxy LRU cap defaults to 10000 | old sessions are evicted past the cap on long runs | tune `max_sessions=` as needed for high-concurrency / long-running scenarios |
| no OpenAI reverse proxy | the proxy only listens on `/v1/messages` | telos-style OpenAI-shape can only go through the SDK transport |
| `R8 refresh` only when the engine supports prewarm | closed-source APIs are all `prewarmable=False` | refresh is always a no-op; only vLLM/SGLang reach it |
| single-process proxy | one aiohttp event loop | to scale out, front it with a load balancer |

---

## 10. Extension points

| What you want to do | Where to change |
|---|---|
| Add a new agent installer (Cursor / Gemini CLI / a local Hermes) | add a `<name>.py` under [init/](../init/), implementing `AgentInstaller` |
| Add a new harness | add a plugin under [harness/](../harness/), registered in [registry.py](../registry.py) |
| Add a new engine adapter | add an `EngineAdapter` subclass under [engine/](../engine/) |
| Add a `/v1/chat/completions` proxy path | add a route in [proxy/server.py](../proxy/server.py) + reuse the same OpenAI pipeline as `process_anthropic_request` |
| Persist session state to Redis / disk | `BridgeSessionState` is a plain dataclass, just serialize it to JSON; change `_SessionRegistry` to use external storage |
