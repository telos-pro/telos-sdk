"""Shared user-message splitting logic (used by both OpenClaw and Hermes).

Splits the plain text of a user message into three categories of substrings
by known envelope patterns:
- ``<environment_info>...</environment_info>`` (injected by OpenClaw) → DROP
- ``<system-reminder>...</system-reminder>`` (injected by Hermes) → DROP
- ``<command-message>...</command-message>`` (Hermes command palette) → DROP
- ``Current time: ...`` (commonly injected by both) → DROP
- the body of the user's question → PIN
- a ``[ref:...]`` reference / previous-turn tool_result summary → FOLD (only when wrapped in ``<prev>...</prev>``)

Reuses the regex set from
``agent-janus/bridge/src/efficiency/prefix-normalization/system.ts`` (the
values are already stable within that repo, so they are copied verbatim here).
"""

from __future__ import annotations

import re
from typing import Iterable

from telos.ir import Band, TelosBlock


# DROP patterns: metadata that the harness injects every turn and that changes every turn
_DROP_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("env_info",        re.compile(r"<environment_info>.*?</environment_info>", re.DOTALL)),
    ("system_reminder", re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)),
    ("command_message", re.compile(r"<command-message>.*?</command-message>", re.DOTALL)),
    ("command_name",    re.compile(r"<command-name>.*?</command-name>", re.DOTALL)),
    ("current_time",    re.compile(r"Current time:.*?(?=\n|$)")),
]

# FOLD patterns: the echo of the previous turn's result (if the harness wraps it explicitly)
_FOLD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("prev_result", re.compile(r"<prev>.*?</prev>", re.DOTALL)),
]


def split_user_text(text: str, *, base_id: str) -> tuple[TelosBlock, ...]:
    """Split a span of user text into (PIN: question body) + (FOLD: reference echo)* + (DROP: envelope)*.

    The returned blocks are already ordered per §5, and can be fed directly to ``TelosMessage(blocks=...)``.
    """
    drops: list[tuple[str, str]] = []
    folds: list[tuple[str, str]] = []
    remaining = text

    for tag, pat in _DROP_PATTERNS:
        for m in pat.finditer(remaining):
            drops.append((tag, m.group(0)))
        remaining = pat.sub("", remaining)

    for tag, pat in _FOLD_PATTERNS:
        for m in pat.finditer(remaining):
            folds.append((tag, m.group(0)))
        remaining = pat.sub("", remaining)

    pin_text = remaining.strip()

    blocks: list[TelosBlock] = []
    if pin_text:
        blocks.append(TelosBlock(
            id=f"{base_id}/q",
            band=Band.PIN,
            kind="text",
            payload=pin_text,
            source_tag="harness/user-query",
        ))
    for i, (tag, content) in enumerate(folds):
        blocks.append(TelosBlock(
            id=f"{base_id}/fold-{i}",
            band=Band.FOLD,
            kind="text",
            payload=content,
            source_tag=f"harness/{tag}",
        ))
    for i, (tag, content) in enumerate(drops):
        blocks.append(TelosBlock(
            id=f"{base_id}/drop-{i}",
            band=Band.DROP,
            kind="text",
            payload=content,
            source_tag=f"harness/{tag}",
        ))
    return tuple(blocks)
