"""Tool output token estimation — a heuristic with no third-party dependencies.

The dashboard historically used a fixed ``chars / 4`` to convert characters
saved by filtering into tokens, which has large errors for code / logs / CJK.
This module provides a heuristic estimate that chunks text by
"word / digit / punctuation / whitespace run", closer to BPE (cl100k-scale)
granularity:

- ASCII letter words: roughly 1 subword token per 4 characters (long words
  get split by BPE).
- Single characters such as digits / punctuation / CJK: 1 token each (code /
  logs are dense with punctuation, and a fixed chars/4 would severely
  underestimate).
- Whitespace: a single space is folded into the adjacent token (counts 0),
  a long whitespace run (indentation / many newlines) is roughly 1 token per
  3 characters.

A pure function that never raises; any exception falls back to ``len(text) // 4``.
"""

from __future__ import annotations

import re

# letter word | whitespace run | any single character (digit / punctuation / CJK …)
_CHUNK_RE = re.compile(r"[A-Za-z]+|\s+|.", re.DOTALL)


def estimate_tokens(text: str) -> int:
    """Estimate the token count of ``text`` (heuristic, not an exact tokenizer)."""
    if not text:
        return 0
    try:
        total = 0.0
        for m in _CHUNK_RE.finditer(text):
            chunk = m.group()
            c0 = chunk[0]
            if c0.isspace():
                # a single space is folded into the adjacent token; a long whitespace run is roughly 3 chars / token
                total += 0 if len(chunk) == 1 else max(1, len(chunk) // 3)
            elif c0.isascii() and c0.isalpha():
                # ASCII word: BPE subwords roughly 1 per 4 chars, at least 1
                total += max(1, round(len(chunk) / 4))
            else:
                # single digit / punctuation / CJK character: 1 token each
                total += 1
        return max(1, int(round(total)))
    except Exception:  # noqa: BLE001 — the estimator never raises
        return max(1, len(text) // 4)
