"""Tests for ``telos.cast`` and ``telos replay --cast`` dashboard recording."""

from __future__ import annotations

import json

from telos.cast import CastRecorder
from telos.output_filter import TelosMode
from telos.replay import replay_session
from telos.replay.__main__ import _render_dashboard_frame
from telos.scripts.build_savings_dashboard import aggregate


def _turns(n: int = 4) -> list[dict]:
    return [{"call_index": i, "request": {
        "model": "claude-opus-4-7", "max_tokens": 100,
        "system": [{"type": "text", "text": "agent"}],
        "messages": [{"role": "user",
                      "content": [{"type": "text", "text": f"turn {i}"}]}],
    }} for i in range(1, n + 1)]


def _fake_sender(wire):
    n = len(json.dumps(wire))
    return {"input_tokens": n // 4, "output_tokens": 1,
            "cache_read_input_tokens": n // 8,
            "cache_creation_input_tokens": n // 16}


def test_cast_recorder_writes_valid_asciinema_v2(tmp_path) -> None:
    cast = tmp_path / "out.cast"
    with CastRecorder(cast, title="t") as rec:
        rec.emit("hello\n")
        rec.frame("panel-1", dt=0.1)
        rec.frame("panel-2", dt=0.1)
    lines = cast.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["version"] == 2 and header["title"] == "t"
    events = [json.loads(ln) for ln in lines[1:]]
    assert all(len(e) == 3 and e[1] == "o" for e in events)
    # the virtual clock advances monotonically with dt
    times = [e[0] for e in events]
    assert times == sorted(times)
    assert times[-1] >= 0.2
    # frame() clears the screen so panels replace each other on playback
    assert "\x1b[2J" in events[1][2]


def test_render_dashboard_frame_shows_modes_and_progress() -> None:
    recs = []
    for label in ("none", "telos"):
        r = replay_session(_turns(), TelosMode.from_label(label),
                           session_id="s", compare_group="s",
                           sender=_fake_sender)
        recs.extend(r.records)
    frame = _render_dashboard_frame("sess-abc", ["none", "telos", "rtk", "both"],
                                    "telos", 3, 4, aggregate(recs))
    assert "dashboard cast" in frame
    assert "sess-abc" in frame
    for m in ("none", "telos", "rtk", "both"):
        assert m in frame
    assert "75%" in frame  # 3 / 4 progress


def test_replay_with_cast_produces_one_frame_per_turn(tmp_path) -> None:
    """End-to-end offline: drive replay_session's on_turn into a CastRecorder."""
    cast = tmp_path / "replay.cast"
    turns = _turns(4)
    done: list[dict] = []
    with CastRecorder(cast) as rec:
        for label in ("none", "both"):
            def on_turn(result, idx, total, _m=label):
                rec.frame(_render_dashboard_frame(
                    "sess", ["none", "both"], _m, idx, total,
                    aggregate(done + result.records)), dt=0.1)
            r = replay_session(turns, TelosMode.from_label(label),
                               session_id="sess", compare_group="sess",
                               sender=_fake_sender, on_turn=on_turn)
            done.extend(r.records)
    events = [json.loads(ln) for ln in
              cast.read_text(encoding="utf-8").splitlines()[1:]]
    # 2 modes × 4 turns = 8 dashboard frames
    assert len(events) == 8
    assert "cumulative saved" in events[-1][2]
