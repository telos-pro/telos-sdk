"""Session corpus —— the proxy records the **raw request** of every
``/v1/messages`` call to disk, so that ``telos replay`` can replay them for
comparison later.

Why only requests are recorded, not responses: Anthropic ``/v1/messages`` is
stateless; the request body of turn N already contains all assistant replies and
tool_results from the prior N-1 turns. So the "request sequence" itself is a
complete, replayable trace; assistant responses need not be stored separately
(which also avoids writing model output to disk).

What is recorded is the **raw** client→proxy request (before RTK filtering,
before TELOS rewriting), i.e. the "canonical input". On replay each mode
re-derives the wire from the same canonical input, so the comparison is fair.

The default directory is ``~/.telos/corpus/``, one ``<session>.jsonl`` per session.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CORPUS_DIR = Path.home() / ".telos" / "corpus"


def _safe_name(session_id: str) -> str:
    """Turn a session_id into a safe filename (anything not alphanumeric . _ - is replaced with _)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)[:120]
    return cleaned or "anon"


def record_call(
    corpus_dir: Path,
    session_id: str,
    call_index: int,
    request: dict[str, Any],
    *,
    ts: float | None = None,
) -> None:
    """Append the raw request of one call to ``<corpus_dir>/<session>.jsonl``.

    The caller is responsible for ensuring ``request`` is the raw body sent by the
    client (not rewritten by RTK / TELOS). This function leaves it to the caller
    to decide whether to raise —— it raises normally here, and the proxy side
    wraps it in a try.
    """
    corpus_dir.mkdir(parents=True, exist_ok=True)
    path = corpus_dir / f"{_safe_name(session_id)}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": ts if ts is not None else time.time(),
            "session_id": session_id,
            "call_index": call_index,
            "request": request,
        }, ensure_ascii=False) + "\n")


@dataclass
class CorpusSessionInfo:
    session_id: str
    path: Path
    n_calls: int
    first_ts: float
    last_ts: float


def _read_lines(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def list_sessions(corpus_dir: Path) -> list[CorpusSessionInfo]:
    """Scan the corpus directory, return a summary of each session (descending by last_ts)."""
    if not corpus_dir.exists():
        return []
    infos: list[CorpusSessionInfo] = []
    for path in sorted(corpus_dir.glob("*.jsonl")):
        recs = _read_lines(path)
        if not recs:
            continue
        sid = recs[0].get("session_id") or path.stem
        ts_vals = [float(r.get("ts") or 0.0) for r in recs]
        infos.append(CorpusSessionInfo(
            session_id=sid,
            path=path,
            n_calls=len(recs),
            first_ts=min(ts_vals) if ts_vals else 0.0,
            last_ts=max(ts_vals) if ts_vals else 0.0,
        ))
    infos.sort(key=lambda i: i.last_ts, reverse=True)
    return infos


def load_session(corpus_dir: Path, session_id: str) -> list[dict[str, Any]]:
    """Read all turns of a session, in ascending ``call_index`` order.

    ``session_id`` can be either a real id or a filename stem —— both will match.
    Raises ``FileNotFoundError`` if not found.
    """
    candidates = [corpus_dir / f"{_safe_name(session_id)}.jsonl"]
    # When the real id and the safe filename differ, fall back to scanning the whole directory for the internal session_id.
    if not candidates[0].exists() and corpus_dir.exists():
        for path in corpus_dir.glob("*.jsonl"):
            recs = _read_lines(path)
            if recs and recs[0].get("session_id") == session_id:
                candidates = [path]
                break
    for path in candidates:
        if path.exists():
            recs = _read_lines(path)
            recs.sort(key=lambda r: int(r.get("call_index") or 0))
            return recs
    raise FileNotFoundError(
        f"session {session_id!r} not found in corpus (directory {corpus_dir})")
