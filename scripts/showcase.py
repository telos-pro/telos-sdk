"""``telos showcase`` — offline, narrated demo + interactive playground for TELOS.

Two entry modes, four scenes, **zero network / zero API key** — built for an
external demo where the venue may be offline.

    telos showcase                  paced auto-run (screen-record this → 5-min video)
    telos showcase --interactive    menu-driven hands-on playground
    telos showcase --cast PATH      also emit an asciinema v2 cast while running
    telos showcase --pace SECONDS   pause between scenes (default 2.5)
    telos showcase --step           wait for Enter between scenes instead of timing

The four scenes:

1. **One IR, five engines** — portability / deterministic degradation.
2. **One invariant** — the protocol's single hard constraint (PIN→FOLD→DROP).
3. **Replay A/B** — a controlled cost comparison across none/rtk/telos/both.
4. **Cost you can see** — aggregate into the single-file savings dashboard.

Scenes 3 & 4 replay **pre-captured** upstream usage from ``showcase/
replay_responses.json`` (captured once with a real API by
``telos.scripts.demo_capture``); when that file is absent they fall back to a
deterministic synthetic estimator. Either way the run is fully offline.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable, Mapping

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

# telos package root == repo root (see pyproject `package-dir`); this file is
# telos/scripts/showcase.py, so parents[1] is the package root.
_PKG_ROOT = Path(__file__).resolve().parents[1]
SHOWCASE_DIR = _PKG_ROOT / "showcase"
CORPUS_PATH = SHOWCASE_DIR / "corpus.jsonl"
RESPONSES_PATH = SHOWCASE_DIR / "replay_responses.json"
DASHBOARD_PATH = SHOWCASE_DIR / "dashboard.html"
USAGE_LOG_PATH = SHOWCASE_DIR / "usage.jsonl"

ENGINES = ("anthropic", "openai", "deepseek", "vllm", "sglang")
ENGINE_MODELS = {
    "anthropic": "claude-opus-4-7",
    "openai": "gpt-5.1",
    "deepseek": "deepseek-chat",
    "vllm": "Qwen/Qwen3-32B",
    "sglang": "deepseek-ai/DeepSeek-V3",
}
# replay modes, in the order the dashboard shows them
REPLAY_MODES = ("none", "rtk", "telos", "both")
COMPARE_GROUP = "showcase-demo"
DEMO_MODEL = "claude-opus-4-7"


# ==========================================================================
# Demo corpus — a 12-turn Claude Code refactor session
#
# Shaped like a real agent run: a large, constant system prompt + tool set,
# and a conversation that grows turn by turn. That stable head is exactly what
# TELOS keeps resident in the KV cache instead of re-prefilling every turn.
# ==========================================================================

# A large, realistic, *constant* system prompt — persona + a spec document.
# Real Claude Code system prompts are ~10k+ tokens; this mirrors that scale so
# the cacheable head dominates, as it does in production.
_PERSONA = (
    "You are a senior software engineer agent operating inside a git repository. "
    "Work in small verified steps: read before you edit, run the tests after every "
    "change, and never leave the tree in a broken state.\n"
)
_AUTH_SPEC = (
    "AUTH SPEC v3 — token refresh flow\n"
    + "Every access token is short-lived (15 min). A refresh token (30 days) is\n"
      "exchanged at /oauth/refresh for a new access token. The client MUST send\n"
      "the refresh token in the X-Refresh header, never in the body. On a 401\n"
      "the client retries exactly once after a silent refresh; a second 401 is\n"
      "surfaced to the user. Tokens are validated with validate_token(), which\n"
      "checks signature, expiry, audience and the revocation list. Rotation is\n"
      "mandatory: a used refresh token is revoked the instant a new pair issues.\n"
    * 95
)
_SYSTEM = [
    {"type": "text", "text": _PERSONA},
    {"type": "text", "text": _AUTH_SPEC},
]
_TOOLS = [
    {"name": "Read", "description": "Read a file from the repository.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    {"name": "Bash", "description": "Run a shell command in the repo root.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}},
    {"name": "Edit", "description": "Apply a search/replace edit to a file.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "old": {"type": "string"},
         "new": {"type": "string"}}}},
    {"name": "Grep", "description": "Search the repository for a pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}}},
]


def _src(name: str, body: str, reps: int) -> str:
    return f"# {name}\n" + body * reps


# moderate tool-output payloads — each one a turn's "new content"
_FILES = {
    "auth/login.py": _src("auth/login.py",
        "def login(user, password):\n"
        "    record = db.find_user(user)\n"
        "    if not record or not verify(password, record.hash):\n"
        "        raise AuthError('bad credentials')\n"
        "    return issue_access_token(record.id)\n", 7),
    "auth/tokens.py": _src("auth/tokens.py",
        "def validate_token(tok):\n"
        "    claims = jwt.decode(tok, KEY, audience=AUD)\n"
        "    if claims['exp'] < now(): raise Expired()\n"
        "    if claims['jti'] in revoked(): raise Revoked()\n"
        "    return claims\n", 8),
    "auth/middleware.py": _src("auth/middleware.py",
        "def auth_middleware(req, nxt):\n"
        "    claims = validate_token(header_token(req))\n"
        "    req.user = claims['sub']\n"
        "    return nxt(req)\n", 7),
    "auth/refresh.py": _src("auth/refresh.py",
        "def refresh(refresh_tok):\n"
        "    claims = validate_token(refresh_tok)\n"
        "    revoke(claims['jti'])\n"
        "    return issue_token_pair(claims['sub'])\n", 7),
}
# A long grep result — many distinct call sites. Too big to keep verbatim;
# an ideal RTK head/tail-truncation target.
_GREP_OUT = "".join(
    f"auth/module_{i:02d}.py:{i * 7 + 3}:    claims = validate_token(tok_{i})\n"
    for i in range(140)
)
# A verbose test log — 220 distinct PASSED lines. RTK keeps head + tail,
# elides the repetitive middle; nothing a downstream turn actually needs.
_PYTEST_OUT = "".join(
    f"tests/test_auth.py::test_case_{i:03d} PASSED  [{i * 100 // 220:>3}%]\n"
    for i in range(220)
) + "==== 220 passed, 0 failed in 8.41s ====\n"


def _u(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _u_tool(tool_use_id: str, text: str) -> dict:
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_use_id,
         "content": [{"type": "text", "text": text}]}]}


def _a(text: str, tool: str | None = None, tid: str = "", tinput: dict | None = None) -> dict:
    content: list[dict] = [{"type": "text", "text": text}]
    if tool is not None:
        content.append({"type": "tool_use", "id": tid, "name": tool,
                        "input": tinput or {}})
    return {"role": "assistant", "content": content}


# One step = (assistant narration, tool, tool input, tool result).
_STEPS: list[tuple[str, str, dict, str]] = [
    ("Reading the current login implementation.",
     "Read", {"path": "auth/login.py"}, _FILES["auth/login.py"]),
    ("Finding every caller of validate_token across the repo.",
     "Grep", {"pattern": "validate_token"}, _GREP_OUT),
    ("Reading the token module those callers depend on.",
     "Read", {"path": "auth/tokens.py"}, _FILES["auth/tokens.py"]),
    ("Checking how the middleware validates requests.",
     "Read", {"path": "auth/middleware.py"}, _FILES["auth/middleware.py"]),
    ("Applying the token-pair refactor to login.py.",
     "Edit", {"path": "auth/login.py", "old": "issue_access_token",
              "new": "issue_token_pair"}, "Edit applied to auth/login.py (1 hunk)."),
    ("Running the auth test suite to confirm nothing broke.",
     "Bash", {"command": "pytest tests/test_auth.py -q"}, _PYTEST_OUT),
    ("Reading the refresh endpoint to wire in rotation.",
     "Read", {"path": "auth/refresh.py"}, _FILES["auth/refresh.py"]),
    ("Adding mandatory refresh-token rotation per the spec.",
     "Edit", {"path": "auth/refresh.py", "old": "issue_token_pair",
              "new": "rotate_and_issue"}, "Edit applied to auth/refresh.py (1 hunk)."),
    ("Updating the middleware to surface a second 401.",
     "Edit", {"path": "auth/middleware.py", "old": "return nxt(req)",
              "new": "return nxt(req)  # 401 retry handled upstream"},
     "Edit applied to auth/middleware.py (1 hunk)."),
    ("Re-running the full auth suite after all edits.",
     "Bash", {"command": "pytest tests/test_auth.py -q"}, _PYTEST_OUT),
    ("Grepping once more to confirm no stale call sites remain.",
     "Grep", {"pattern": "issue_access_token"}, "(no matches)\n"),
]


def _build_convo() -> list[dict]:
    convo: list[dict] = [
        _u("Refactor the auth package to the new token refresh flow in the AUTH "
           "SPEC above: token-pair issuance, mandatory refresh rotation, and the "
           "single-retry 401 rule.\n"
           "<environment_info>cwd=/repo branch=main dirty=2</environment_info>\n"
           "Current time: 2026-05-19 09:14:02"),
    ]
    for i, (narration, tool, tinput, result) in enumerate(_STEPS, start=1):
        convo.append(_a(narration, tool, f"t{i}", tinput))
        convo.append(_u_tool(f"t{i}", result))
    convo.append(_a("Done — the auth package now issues token pairs, rotates "
                    "refresh tokens, and all 64 auth tests pass."))
    return convo


_CONVO = _build_convo()
# corpus turn k = the request sent just before assistant message k.
# assistant messages sit at odd indices 1, 3, 5, ... → cuts 1, 3, 5, ...
_CUTS = list(range(1, len(_CONVO), 2))


def build_demo_corpus() -> list[dict[str, Any]]:
    """Return the multi-turn demo corpus as ``[{call_index, request}, ...]``."""
    turns: list[dict[str, Any]] = []
    for i, cut in enumerate(_CUTS, start=1):
        request = {
            "model": DEMO_MODEL,
            "max_tokens": 1024,
            "system": _SYSTEM,
            "tools": _TOOLS,
            "messages": _CONVO[:cut],
        }
        turns.append({"call_index": i, "request": request})
    return turns


def load_or_build_corpus() -> list[dict[str, Any]]:
    """Load ``showcase/corpus.jsonl`` if present, else build it and write it out."""
    try:
        from telos.corpus import _read_lines  # type: ignore
        if CORPUS_PATH.exists():
            recs = _read_lines(CORPUS_PATH)
            if recs:
                recs.sort(key=lambda r: int(r.get("call_index") or 0))
                return recs
    except Exception:  # noqa: BLE001
        pass
    turns = build_demo_corpus()
    write_corpus(turns)
    return turns


def write_corpus(turns: list[dict[str, Any]]) -> None:
    SHOWCASE_DIR.mkdir(parents=True, exist_ok=True)
    with CORPUS_PATH.open("w", encoding="utf-8") as f:
        for t in turns:
            rec = {"ts": 0.0, "session_id": "showcase", **t}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ==========================================================================
# Replay senders — recorded (offline, real numbers) + synthetic fallback
# ==========================================================================

def _prompt_tokens(wire: Mapping[str, Any]) -> int:
    """Rough token estimate of a wire's prompt (system + tools + messages)."""
    parts = [wire.get("system"), wire.get("tools"), wire.get("messages")]
    return max(1, len(json.dumps(parts, ensure_ascii=False)) // 4)


def synthetic_sender(mode_label: str):
    """A stateful offline ``Sender`` that models Anthropic prefix caching.

    The demo session is append-only: turn *k*'s prompt re-uses the **entire**
    prompt of turn *k-1* as a stable prefix. Under TELOS that prefix stays
    resident in the KV cache and is billed as ``cache_read`` (10% of the input
    price); with caching off (``none`` / ``rtk``) the same prefix is
    re-prefilled at full price on every single turn.

    This is the mechanism the showcase is about — so the synthetic estimator
    computes it directly from the wire, rather than guessing a hit rate.
    """
    telos_on = mode_label in ("telos", "both")
    state = {"prev": 0}

    def send(wire: Mapping[str, Any]) -> dict:
        cur = _prompt_tokens(wire)
        prev = state["prev"]
        state["prev"] = cur
        if telos_on:
            cache_read = min(prev, cur)          # prior prefix served from cache
            cache_write = max(0, cur - cache_read)  # this turn's new content, cached
            raw_input = 0
        else:
            cache_read = 0                        # no cache_control → no hits
            cache_write = 0
            raw_input = cur                       # whole prompt re-prefilled
        return {
            "input_tokens": raw_input,
            "output_tokens": 1,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
            "cache_creation": {
                "ephemeral_5m_input_tokens": cache_write,
                "ephemeral_1h_input_tokens": 0,
            },
        }

    return send


def load_responses() -> dict[str, Any] | None:
    """Load pre-captured ``{mode: [raw_usage, ...]}`` (plus ``_meta``) if present."""
    if not RESPONSES_PATH.exists():
        return None
    try:
        data = json.loads(RESPONSES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def responses_source(responses: dict[str, Any] | None) -> str:
    """Human label for where Scene 3's upstream usage came from."""
    if not responses:
        return "synthetic estimate (no capture file yet)"
    meta = responses.get("_meta")
    if isinstance(meta, dict) and meta.get("source") == "real":
        return "pre-captured real Anthropic API usage"
    return "deterministic synthetic estimate"


def recorded_sender(mode_label: str, responses: dict[str, Any] | None):
    """Build a replay ``Sender`` that pops pre-captured usage in turn order.

    Falls back to :func:`synthetic_sender` for any turn without a recording, so
    the showcase never breaks even with a partial / missing capture file.
    """
    recorded = list((responses or {}).get(mode_label, []))
    fallback = synthetic_sender(mode_label)
    counter = {"i": 0}

    def send(wire: Mapping[str, Any]) -> dict:
        counter["i"] += 1
        idx = counter["i"]
        if idx <= len(recorded) and isinstance(recorded[idx - 1], dict):
            return dict(recorded[idx - 1])
        return fallback(wire)

    return send


# ==========================================================================
# Printer — stdout + optional asciinema v2 cast
# ==========================================================================

class Printer:
    """Writes to stdout and, optionally, records an asciinema v2 cast."""

    def __init__(self, cast_path: str | None = None):
        self._cast = None
        self._t0 = time.monotonic()
        if cast_path:
            Path(cast_path).parent.mkdir(parents=True, exist_ok=True)
            self._cast = open(cast_path, "w", encoding="utf-8")
            header = {"version": 2, "width": 100, "height": 36,
                      "timestamp": int(time.time()),
                      "env": {"TERM": "xterm-256color", "SHELL": "/bin/zsh"}}
            self._cast.write(json.dumps(header) + "\n")

    def out(self, line: str = "") -> None:
        text = line + "\n"
        sys.stdout.write(text)
        sys.stdout.flush()
        if self._cast:
            self._cast.write(json.dumps([time.monotonic() - self._t0, "o", text]) + "\n")

    def pause(self, secs: float) -> None:
        if secs > 0:
            time.sleep(secs)

    def close(self) -> None:
        if self._cast:
            self._cast.close()
            self._cast = None


def _box(p: Printer, title: str, lines: list[str], width: int = 78) -> None:
    p.out("┌─ " + title + " " + "─" * max(0, width - 4 - len(title)))
    for ln in lines:
        p.out("│ " + ln)
    p.out("└" + "─" * (width - 1))


def _banner(p: Printer, n: int, title: str, narration: str) -> None:
    p.out("")
    p.out("━" * 78)
    p.out(f"  SCENE {n}  ·  {title}")
    p.out("━" * 78)
    p.out(f"  ▸ {narration}")
    p.out("")


# ==========================================================================
# Scene 1 — one IR, five engines
# ==========================================================================

def _sample_request() -> dict:
    """A single representative request (turn 3 of the corpus)."""
    return build_demo_corpus()[2]["request"]


def scene_portability(p: Printer, *, pace: float = 0.0) -> None:
    from telos import Bridge, load_engine, load_harness

    raw = _sample_request()
    p.out("One OpenClaw-style request → one TelosIR → the wire for five engines.")
    p.out("The IR is parsed ONCE; each engine adapter degrades it deterministically.")
    p.out("")

    summary: list[tuple[str, int, str | None]] = []
    for engine_name in ENGINES:
        harness = load_harness("openclaw")
        engine = load_engine(engine_name)
        ir = harness.parse(raw, session_id=f"showcase-{engine_name}",
                           engine=engine_name, model=ENGINE_MODELS[engine_name],
                           expected_turns=20)
        bridge = Bridge(ir, engine)
        plan = bridge.mark()
        layout = bridge.dump_layout().splitlines()[1:]  # drop the session header
        rk = plan.routing_key
        lines = ["band layout:"]
        lines += ["    " + ln for ln in layout]
        lines.append(f"mark plan : {len(plan.slots)} slot(s)  routing_key={rk!r}")
        for s in plan.slots:
            seg = s.segment if s.message_index is None else f"{s.segment}#{s.message_index}"
            lines.append(f"    {s.name:7s} {seg}[{s.index}]  ttl={s.ttl_class}")
        _box(p, f"{engine_name}  ·  {ENGINE_MODELS[engine_name]}", lines)
        summary.append((engine_name, len(plan.slots), rk))
        p.pause(pace)

    p.out("")
    p.out("Same IR, five wires:")
    for name, n_slots, rk in summary:
        if n_slots and rk:
            note = f"{n_slots} explicit breakpoint(s) + routing_key"
        elif n_slots:
            note = f"{n_slots} explicit breakpoint(s)"
        elif rk:
            note = "no breakpoints — falls back to a routing_key"
        else:
            note = "no breakpoints, no routing_key — caches on raw prefix only"
        p.out(f"  • {name:9s} → {note}")
    p.out("")
    p.out("  Capability gaps are bridged by the adapter — deterministically,")
    p.out("  never silently, never lossily in meaning. That is portable context.")


# ==========================================================================
# Scene 2 — the one invariant
# ==========================================================================

def scene_invariant(p: Printer, *, pace: float = 0.0) -> None:
    from telos.ir import (Band, TelosBlock, TelosIR, TelosInvariantError,
                          TelosMessage, assert_ir_invariants)

    p.out("The whole protocol has exactly ONE hard constraint. Within every")
    p.out("segment, blocks must be physically ordered:  PIN* → FOLD* → DROP*")
    p.out("")

    def msg(*bands: Band) -> TelosMessage:
        blocks = tuple(
            TelosBlock(id=f"b{i}", band=b, kind="text", payload=f"<{b.value}>")
            for i, b in enumerate(bands)
        )
        return TelosMessage(role="user", blocks=blocks)

    # --- valid ---
    good = TelosIR(session_id="ok", tools=(), system=(),
                   messages=(msg(Band.PIN, Band.FOLD, Band.DROP),), ref_pool={})
    _box(p, "a well-formed message", [
        "blocks : pin:b0 | fold:b1 | drop:b2",
        "assert_ir_invariants(ir) ...",
    ])
    assert_ir_invariants(good)
    p.out("  ✓ accepted — order holds.")
    p.out("")
    p.pause(pace)

    # --- invalid ---
    bad = TelosIR(session_id="bad", tools=(), system=(),
                  messages=(msg(Band.FOLD, Band.PIN),), ref_pool={})
    _box(p, "a malformed message (FOLD placed before PIN)", [
        "blocks : fold:b0 | pin:b1",
        "assert_ir_invariants(ir) ...",
    ])
    try:
        assert_ir_invariants(bad)
        p.out("  ?! no error — this should not happen")
    except TelosInvariantError as e:
        p.out("  ✗ TelosInvariantError raised at the gate:")
        for ln in str(e).split(". "):
            p.out(f"      {ln.strip()}")
    p.out("")
    p.out("  One invariant, checked before and after every primitive. Everything")
    p.out("  else in TELOS is a soft suggestion — this is the only thing that bites.")


# ==========================================================================
# Scene 3 — replay A/B
# ==========================================================================

def _run_replays(turns: list[dict], responses: dict[str, Any] | None) -> list[dict]:
    """Replay the corpus under all 4 modes; return all usage_log records."""
    from telos.output_filter import TelosMode
    from telos.replay import replay_session

    records: list[dict] = []
    for mode_label in REPLAY_MODES:
        mode = TelosMode.from_label(mode_label)
        result = replay_session(
            turns, mode,
            session_id="showcase",
            compare_group=COMPARE_GROUP,
            sender=recorded_sender(mode_label, responses),
            cache_isolation=True,
        )
        records.extend(result.records)
    return records


def _aggregate_by_mode(records: list[dict]):
    from telos.scripts.build_savings_dashboard import aggregate
    return aggregate(records).by_mode


def _pct(part: float, whole: float) -> int:
    return round(100 * part / whole) if whole else 0


def scene_replay(p: Printer, *, pace: float = 0.0,
                 records: list[dict] | None = None) -> list[dict]:
    p.out("Record one real session, then replay the BYTE-IDENTICAL request")
    p.out("sequence once per mode. Only the optimization switch varies — a")
    p.out("controlled experiment, not two noisy independent runs.")
    p.out("")

    responses = load_responses()
    n_turns = len(_CUTS)
    p.out(f"  corpus: {n_turns} turns · upstream usage = {responses_source(responses)}")
    p.out("")

    if records is None:
        records = _run_replays(load_or_build_corpus(), responses)
    by_mode = _aggregate_by_mode(records)

    def prompt_tokens(agg) -> int:
        return agg.raw_input + agg.cache_read + agg.cache_write

    none = by_mode.get("none")
    base_cost = none.cost_usd if none is not None else 0.0

    # mode  prompt-tokens  served-from-cache  token-cost  vs. none
    p.out(f"  {'mode':<7}{'prompt tok':>13}{'from cache':>13}"
          f"{'token cost':>13}{'vs none':>10}")
    p.out("  " + "─" * 54)
    for label in REPLAY_MODES:
        agg = by_mode.get(label)
        if agg is None:
            continue
        pt = prompt_tokens(agg)
        hit = _pct(agg.cache_read, pt)
        if label == "none":
            vs = "baseline"
        elif base_cost:
            vs = f"-{_pct(base_cost - agg.cost_usd, base_cost)}%"
        else:
            vs = "—"
        p.out(f"  {label:<7}{pt:>13,}{str(hit) + '%':>13}"
              f"{_usd(agg.cost_usd):>13}{vs:>10}")
    p.out("")

    telos = by_mode.get("telos")
    both = by_mode.get("both")
    if none is not None and telos is not None and base_cost:
        hit = _pct(telos.cache_read, prompt_tokens(telos))
        cut = _pct(base_cost - telos.cost_usd, base_cost)
        p.out(f"  TELOS keeps a stable prefix resident in cache — {hit}% of all")
        p.out(f"  prompt tokens are served from cache instead of re-prefilled,")
        p.out(f"  cutting token cost by {cut}% versus passthrough.")
        if both is not None:
            cut_both = _pct(base_cost - both.cost_usd, base_cost)
            p.out(f"  With RTK trimming tool output on top → {cut_both}% lower.")
    p.out("")
    p.out("  Absolute dollars on a pinned trajectory — not a ratio you can game.")
    return records


def _usd(v: float) -> str:
    if v <= 0:
        return "$0.00"
    if v < 0.01:
        return f"${v:.4f}"
    return f"${v:.2f}"


# ==========================================================================
# Scene 4 — cost you can see
# ==========================================================================

def scene_dashboard(p: Printer, *, records: list[dict], open_browser: bool = True) -> None:
    from telos.scripts.build_savings_dashboard import main as dash_main

    p.out("Every call's normalized usage lands in a jsonl log, aggregated into")
    p.out("a single-file HTML dashboard — inline SVG + CSS, zero JS, opens offline.")
    p.out("")

    SHOWCASE_DIR.mkdir(parents=True, exist_ok=True)
    with USAGE_LOG_PATH.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    dash_main(["--usage-log", str(USAGE_LOG_PATH), "--out", str(DASHBOARD_PATH)])
    p.out("")
    p.out(f"  dashboard written → {DASHBOARD_PATH}")
    p.out("  Token mix · absolute $ saved · breakdown by harness / model / session.")
    p.out("  The four replay modes land as four sessions under \"saved $\" —")
    p.out("  read the none / rtk / telos / both comparison straight off the page.")
    if open_browser:
        try:
            webbrowser.open(DASHBOARD_PATH.as_uri())
        except Exception:  # noqa: BLE001
            pass


# ==========================================================================
# Paced run (for recording)
# ==========================================================================

def _closing(p: Printer) -> None:
    p.out("")
    p.out("━" * 78)
    p.out("  Portable context → visible savings → you hold the controller.")
    p.out("  Harnesses are just hired help. The context — the tablet — is yours.")
    p.out("━" * 78)
    p.out("")


def run_paced(*, pace: float, step: bool, cast: str | None,
              open_browser: bool) -> int:
    p = Printer(cast_path=cast)
    try:
        p.out("")
        p.out("  ╔══════════════════════════════════════════════════════════════╗")
        p.out("  ║   TELOS — portable, cache-friendly agent context             ║")
        p.out("  ║   show case · 4 scenes · fully offline                       ║")
        p.out("  ╚══════════════════════════════════════════════════════════════╝")
        p.pause(pace)

        def gate() -> None:
            if step and not cast:
                try:
                    input("\n  [Enter] for the next scene… ")
                except EOFError:
                    pass
            else:
                p.pause(pace)

        _banner(p, 1, "One IR, five engines",
                "Context is not locked to one vendor.")
        scene_portability(p, pace=min(pace, 1.0))
        gate()

        _banner(p, 2, "One invariant",
                "The whole protocol has a single hard constraint.")
        scene_invariant(p, pace=min(pace, 1.0))
        gate()

        _banner(p, 3, "Replay A/B",
                "Controlled cost comparison — clean, reproducible numbers.")
        records = scene_replay(p, pace=min(pace, 1.0))
        gate()

        _banner(p, 4, "Cost you can see",
                "Absolute dollars, in a single offline HTML file.")
        scene_dashboard(p, records=records, open_browser=open_browser)

        _closing(p)
    finally:
        p.close()
    if cast:
        print(f"\n[showcase] asciinema cast written → {cast}")
        print(f"[showcase] replay with:  asciinema play {cast}")
    return 0


# ==========================================================================
# Interactive playground
# ==========================================================================

def _interactive_engine(p: Printer) -> None:
    from telos import Bridge, load_engine, load_harness
    from telos.cli_menu import select_from

    choice = select_from([(e, f"{e}  ({ENGINE_MODELS[e]})") for e in ENGINES],
                         prompt="Pick an engine:")
    harness = load_harness("openclaw")
    engine = load_engine(choice)
    ir = harness.parse(_sample_request(), session_id=f"play-{choice}",
                       engine=choice, model=ENGINE_MODELS[choice], expected_turns=20)
    bridge = Bridge(ir, engine)
    plan = bridge.mark()
    p.out("")
    p.out(bridge.dump_layout())
    p.out(f"\nmark plan: {len(plan.slots)} slot(s)  routing_key={plan.routing_key!r}")
    for s in plan.slots:
        seg = s.segment if s.message_index is None else f"{s.segment}#{s.message_index}"
        p.out(f"  {s.name:7s} {seg}[{s.index}]  ttl={s.ttl_class}")


def _interactive_turns(p: Printer) -> None:
    from telos import Bridge, load_engine, load_harness

    raw_in = input("expected_turns (e.g. 2, 20, 60) [20]: ").strip() or "20"
    try:
        et = max(0, int(raw_in))
    except ValueError:
        p.out("  not an integer — using 20")
        et = 20
    harness = load_harness("openclaw")
    engine = load_engine("anthropic")
    ir = harness.parse(_sample_request(), session_id="play-turns",
                       engine="anthropic", model=DEMO_MODEL, expected_turns=et)
    plan = Bridge(ir, engine).mark()
    p.out(f"\nexpected_turns={et} → {len(plan.slots)} mark slot(s):")
    for s in plan.slots:
        seg = s.segment if s.message_index is None else f"{s.segment}#{s.message_index}"
        p.out(f"  {s.name:7s} {seg}[{s.index}]  ttl={s.ttl_class}")
    p.out("  (a higher expected_turns toggles the mid-rolling anchor — fix R2.)")


def _interactive_invariant(p: Printer) -> None:
    from telos.cli_menu import select_from
    from telos.ir import (Band, TelosBlock, TelosIR, TelosInvariantError,
                          TelosMessage, assert_ir_invariants)

    orders = {
        "pin, fold, drop  (valid)": (Band.PIN, Band.FOLD, Band.DROP),
        "fold, pin        (FOLD before PIN)": (Band.FOLD, Band.PIN),
        "drop, pin        (DROP before PIN)": (Band.DROP, Band.PIN),
        "pin, drop, fold  (FOLD after DROP)": (Band.PIN, Band.DROP, Band.FOLD),
    }
    pick = select_from([(k, k) for k in orders], prompt="Pick a band order to test:")
    bands = orders[pick]
    blocks = tuple(TelosBlock(id=f"b{i}", band=b, kind="text", payload="x")
                   for i, b in enumerate(bands))
    ir = TelosIR(session_id="play", tools=(), system=(),
                 messages=(TelosMessage(role="user", blocks=blocks),), ref_pool={})
    p.out("  blocks: " + " | ".join(f"{b.band.value}:{b.id}" for b in blocks))
    try:
        assert_ir_invariants(ir)
        p.out("  ✓ accepted — PIN* → FOLD* → DROP* holds.")
    except TelosInvariantError as e:
        p.out(f"  ✗ TelosInvariantError: {e}")


def _interactive_replay(p: Printer) -> list[dict]:
    records = _run_replays(load_or_build_corpus(), load_responses())
    scene_replay(p, records=records)
    return records


def run_interactive(open_browser: bool = True) -> int:
    from telos.cli_menu import select_from

    p = Printer()
    p.out("")
    p.out("  TELOS showcase — interactive playground (offline)")
    p.out("  Pick something to try; pick Quit to leave.")
    records: list[dict] | None = None
    actions = [
        ("engine", "[1] One IR on a chosen engine — band layout + mark plan"),
        ("turns", "[2] Change expected_turns — watch the mark plan shift"),
        ("invariant", "[3] Pick a band order — see the invariant accept/reject it"),
        ("replay", "[4] Run the 4-mode replay A/B comparison"),
        ("dashboard", "[5] Build & open the savings dashboard"),
        ("quit", "[q] Quit"),
    ]
    while True:
        p.out("")
        try:
            choice = select_from(actions, prompt="What would you like to see?")
        except (RuntimeError, EOFError, KeyboardInterrupt):
            break
        if choice == "quit":
            break
        try:
            if choice == "engine":
                _interactive_engine(p)
            elif choice == "turns":
                _interactive_turns(p)
            elif choice == "invariant":
                _interactive_invariant(p)
            elif choice == "replay":
                records = _interactive_replay(p)
            elif choice == "dashboard":
                if records is None:
                    records = _run_replays(load_or_build_corpus(), load_responses())
                scene_dashboard(p, records=records, open_browser=open_browser)
        except (KeyboardInterrupt, EOFError):
            break
        except Exception as e:  # noqa: BLE001
            p.out(f"  error: {e}")
    p.out("\n  Thanks for trying TELOS.")
    return 0


# ==========================================================================
# Entry point
# ==========================================================================

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="telos showcase",
        description="Offline narrated demo + interactive playground for TELOS.")
    ap.add_argument("--interactive", "-i", action="store_true",
                    help="menu-driven hands-on playground")
    ap.add_argument("--cast", metavar="PATH",
                    help="also write an asciinema v2 cast to PATH")
    ap.add_argument("--pace", type=float, default=2.5,
                    help="seconds to pause between scenes (default 2.5)")
    ap.add_argument("--step", action="store_true",
                    help="wait for Enter between scenes instead of a timed pause")
    ap.add_argument("--no-open", action="store_true",
                    help="do not open the dashboard in a browser")
    args = ap.parse_args(argv)

    if args.interactive:
        return run_interactive(open_browser=not args.no_open)
    return run_paced(pace=max(0.0, args.pace), step=args.step,
                     cast=args.cast, open_browser=not args.no_open)


if __name__ == "__main__":
    raise SystemExit(main())
