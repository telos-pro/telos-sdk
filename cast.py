"""asciinema v2 cast recorder — a tiny, dependency-free terminal-recording writer.

A *cast* is the JSON-lines format `asciinema` plays back: a header line, then
``[time, "o", text]`` output events. Because this writer owns the timing, a
cast can be assembled programmatically — no terminal, no screen recorder.

Two emit styles:

- :meth:`CastRecorder.emit` — append text, scrolling like a normal terminal.
- :meth:`CastRecorder.frame` — emit a *full-screen frame* that clears the
  screen first, so playback shows one panel updating in place. This is what
  ``telos replay --cast`` uses to record the savings dashboard changing.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# clear screen + scrollback, move cursor home — makes successive frames
# replace each other instead of scrolling.
_CLEAR = "\x1b[2J\x1b[3J\x1b[H"


class CastRecorder:
    """Writes an asciinema v2 cast on a virtual (caller-driven) clock."""

    def __init__(self, path: str | Path, *, width: int = 100,
                 height: int = 40, title: str = "") -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self._path.open("w", encoding="utf-8")
        header: dict = {"version": 2, "width": width, "height": height,
                        "timestamp": int(time.time()),
                        "env": {"TERM": "xterm-256color", "SHELL": "/bin/zsh"}}
        if title:
            header["title"] = title
        self._f.write(json.dumps(header) + "\n")
        self._clock = 0.0

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, text: str, *, dt: float = 0.0) -> None:
        """Append one output event. ``dt`` advances the playback clock first."""
        self._clock += max(0.0, dt)
        self._f.write(json.dumps([round(self._clock, 3), "o", text]) + "\n")

    def frame(self, text: str, *, dt: float = 0.0, clear: bool = True) -> None:
        """Emit a full-screen frame that replaces the previous one on playback."""
        self.emit((_CLEAR if clear else "") + text, dt=dt)

    def close(self) -> None:
        if self._f is not None and not self._f.closed:
            self._f.close()

    def __enter__(self) -> "CastRecorder":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
