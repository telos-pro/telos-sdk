"""``python -m telos.scripts.demo_capture`` — prep-time capture for the showcase.

Run this ONCE during demo prep, while you have network + ``ANTHROPIC_API_KEY``.
It replays the multi-turn demo corpus through Anthropic for all four modes
(none / rtk / telos / both) and writes the **real** per-turn usage Anthropic
reported into ``showcase/replay_responses.json``.

After that, ``telos showcase`` runs fully offline and Scene 3 shows real numbers.

    # real capture (needs ANTHROPIC_API_KEY)
    python -m telos.scripts.demo_capture

    # offline fallback — deterministic synthetic estimates, no API
    python -m telos.scripts.demo_capture --synthetic

``--synthetic`` is also how the repo ships an initial ``replay_responses.json``
so the showcase works before a real capture has been run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Mapping

from telos.output_filter import TelosMode
from telos.replay import replay_session
from telos.scripts.showcase import (
    COMPARE_GROUP,
    REPLAY_MODES,
    RESPONSES_PATH,
    SHOWCASE_DIR,
    build_demo_corpus,
    synthetic_sender,
    write_corpus,
)


def _capturing_sender(inner, store: list[dict], mode_label: str):
    """Wrap a replay sender so every returned raw_usage is also recorded."""
    fallback = synthetic_sender(mode_label)
    counter = {"i": 0}

    def send(wire: Mapping[str, Any]) -> dict | None:
        counter["i"] += 1
        raw = inner(wire)
        if raw is None:
            # keep the capture file usable even if one turn failed
            raw = fallback(wire)
            print(f"  [warn] {mode_label} turn {counter['i']}: upstream failed, "
                  f"stored a synthetic estimate", file=sys.stderr)
        store.append(dict(raw))
        return raw

    return send


def _synthetic_inner(mode_label: str):
    return synthetic_sender(mode_label)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="telos.scripts.demo_capture",
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--synthetic", action="store_true",
                    help="skip the API; write deterministic synthetic estimates")
    ap.add_argument("--api-key", help="Anthropic API key (else ANTHROPIC_API_KEY)")
    ap.add_argument("--out", default=str(RESPONSES_PATH),
                    help=f"output JSON path (default: {RESPONSES_PATH})")
    args = ap.parse_args(argv)

    turns = build_demo_corpus()
    write_corpus(turns)
    print(f"[demo-capture] corpus: {len(turns)} turns")

    if args.synthetic:
        print("[demo-capture] mode: SYNTHETIC (no network)")
    else:
        key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            print("error: no ANTHROPIC_API_KEY — pass --api-key or use --synthetic",
                  file=sys.stderr)
            return 2
        print("[demo-capture] mode: REAL Anthropic API")

    responses: dict[str, Any] = {
        "_meta": {"source": "synthetic" if args.synthetic else "real",
                  "modes": list(REPLAY_MODES), "turns": len(turns)},
    }
    for mode_label in REPLAY_MODES:
        store: list[dict] = []
        if args.synthetic:
            inner = _synthetic_inner(mode_label)
        else:
            from telos.replay import anthropic_sender
            inner = anthropic_sender(api_key=args.api_key
                                     or os.environ.get("ANTHROPIC_API_KEY"))
        sender = _capturing_sender(inner, store, mode_label)
        result = replay_session(
            turns, TelosMode.from_label(mode_label),
            session_id="showcase", compare_group=COMPARE_GROUP,
            sender=sender, cache_isolation=True,
        )
        responses[mode_label] = store
        cr = sum(int(r.get("cache_read_input_tokens", 0) or 0) for r in store)
        print(f"  {mode_label:<6} {result.turns_ok} ok / {result.turns_failed} failed "
              f"· {cr:,} cache_read tokens captured")

    SHOWCASE_DIR.mkdir(parents=True, exist_ok=True)
    out = args.out
    with open(out, "w", encoding="utf-8") as f:
        json.dump(responses, f, ensure_ascii=False, indent=2)
    print(f"[demo-capture] wrote {out}")
    print("[demo-capture] `telos showcase` will now use these numbers offline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
