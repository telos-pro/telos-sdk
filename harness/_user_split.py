"""共享的 user-message 切分逻辑（OpenClaw / Hermes 都要用）。

把一条 user message 的纯文本按已知 envelope 模式切成三类子串：
- ``<environment_info>...</environment_info>``（OpenClaw 注入）→ DROP
- ``<system-reminder>...</system-reminder>``（Hermes 注入）→ DROP
- ``<command-message>...</command-message>``（Hermes 命令面板）→ DROP
- ``Current time: ...``（两边都常注入）→ DROP
- 用户的提问主体 → PIN
- ``[ref:...]`` 引用 / 上轮 tool_result 摘要 → FOLD（仅当用 ``<prev>...</prev>`` 包裹）

复用 ``agent-janus/bridge/src/efficiency/prefix-normalization/system.ts`` 的
正则集合（值在仓库内已稳定，这里照搬就行）。
"""

from __future__ import annotations

import re
from typing import Iterable

from stela.ir import Band, StelaBlock


# DROP 模式：harness 在每轮注入、每轮变化的元数据
_DROP_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("env_info",        re.compile(r"<environment_info>.*?</environment_info>", re.DOTALL)),
    ("system_reminder", re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)),
    ("command_message", re.compile(r"<command-message>.*?</command-message>", re.DOTALL)),
    ("command_name",    re.compile(r"<command-name>.*?</command-name>", re.DOTALL)),
    ("current_time",    re.compile(r"Current time:.*?(?=\n|$)")),
]

# FOLD 模式：上一轮结果回声（如果 harness 显式包裹）
_FOLD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("prev_result", re.compile(r"<prev>.*?</prev>", re.DOTALL)),
]


def split_user_text(text: str, *, base_id: str) -> tuple[StelaBlock, ...]:
    """把一段 user 文本切成 (PIN: 提问主体) + (FOLD: 引用回声)* + (DROP: envelope)*。

    返回的 blocks 已经按 §5 顺序排列，可以直接喂给 ``StelaMessage(blocks=...)``。
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

    blocks: list[StelaBlock] = []
    if pin_text:
        blocks.append(StelaBlock(
            id=f"{base_id}/q",
            band=Band.PIN,
            kind="text",
            payload=pin_text,
            source_tag="harness/user-query",
        ))
    for i, (tag, content) in enumerate(folds):
        blocks.append(StelaBlock(
            id=f"{base_id}/fold-{i}",
            band=Band.FOLD,
            kind="text",
            payload=content,
            source_tag=f"harness/{tag}",
        ))
    for i, (tag, content) in enumerate(drops):
        blocks.append(StelaBlock(
            id=f"{base_id}/drop-{i}",
            band=Band.DROP,
            kind="text",
            payload=content,
            source_tag=f"harness/{tag}",
        ))
    return tuple(blocks)
