"""会话语料库 —— proxy 把每次 ``/v1/messages`` 的**原始请求**录到磁盘，
供 ``telos replay`` 之后重放对照。

为什么只录请求、不录响应：Anthropic ``/v1/messages`` 是无状态的，第 N 轮
的请求体里 ``messages[]`` 已经包含了前 N-1 轮的全部 assistant 回复和
tool_result。所以「请求序列」本身就是完整的可重放轨迹；assistant 响应
不必单独存（也避免把模型输出落盘）。

录的是 client→proxy 的**原始**请求（RTK 过滤前、TELOS 改写前），即「规范
输入」。replay 时每个 mode 各自从同一份规范输入重新推导 wire，对照才公平。

默认目录 ``~/.telos/corpus/``，一个 session 一个 ``<session>.jsonl``。
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
    """把 session_id 变成安全的文件名（非字母数字 . _ - 一律替换成 _）。"""
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
    """把一次调用的原始请求 append 到 ``<corpus_dir>/<session>.jsonl``。

    调用方负责保证 ``request`` 是 client 发来的原始 body（未经 RTK / TELOS
    改写）。本函数从不抛错由调用方决定——这里照常抛，proxy 侧包 try。
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
    """扫描语料目录，返回每个 session 的摘要（按 last_ts 倒序）。"""
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
    """读出某个 session 的全部轮次，按 ``call_index`` 升序。

    ``session_id`` 既可传真实 id，也可传文件名 stem —— 两种都能命中。
    找不到时抛 ``FileNotFoundError``。
    """
    candidates = [corpus_dir / f"{_safe_name(session_id)}.jsonl"]
    # 真实 id 与 safe 文件名不一致时，回退到全目录扫描匹配内部 session_id。
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
        f"corpus 里找不到 session {session_id!r}（目录 {corpus_dir}）")
