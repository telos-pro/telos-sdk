"""把工具结果过滤器应用到原始 ``/v1/messages`` 请求体。

在 STELA 管线**之前**跑：rtk 开关打开时，proxy 收到请求后先用这里的
``apply_filter`` 把 ``messages[].content[].tool_result`` 里的大段 bash
输出缩短，再（可选地）交给 STELA 管线打 cache 标记。

纯函数：不改入参，返回新的 request dict + ``FilterStats`` 计量。
"""

from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Mapping

from stela.output_filter.filters import ToolResultFilter


@dataclass
class FilterStats:
    """一次请求里所有 tool_result 过滤的汇总，写进 usage_log。"""

    original_chars: int = 0
    filtered_chars: int = 0
    blocks_seen: int = 0      # 扫描到的 tool_result 文本块数
    blocks_filtered: int = 0  # 实际省下字节的块数
    by_rule: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def saved_chars(self) -> int:
        return max(0, self.original_chars - self.filtered_chars)

    def as_dict(self) -> dict[str, Any]:
        return {
            "original_chars": self.original_chars,
            "filtered_chars": self.filtered_chars,
            "saved_chars": self.saved_chars,
            "blocks_seen": self.blocks_seen,
            "blocks_filtered": self.blocks_filtered,
            "by_rule": dict(self.by_rule),
        }


def _command_index(messages: list[Any]) -> dict[str, str]:
    """tool_use_id → shell 命令串（仅 Bash 类工具有 ``input.command``）。"""
    out: dict[str, str] = {}
    for msg in messages:
        if not isinstance(msg, Mapping) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, Mapping) or item.get("type") != "tool_use":
                continue
            tid = item.get("id")
            inp = item.get("input")
            if isinstance(tid, str) and isinstance(inp, Mapping):
                cmd = inp.get("command")
                if isinstance(cmd, str):
                    out[tid] = cmd
    return out


def _tool_name_index(messages: list[Any]) -> dict[str, str]:
    """tool_use_id → 工具名。"""
    out: dict[str, str] = {}
    for msg in messages:
        if not isinstance(msg, Mapping) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, Mapping) and item.get("type") == "tool_use":
                tid, name = item.get("id"), item.get("name")
                if isinstance(tid, str) and isinstance(name, str):
                    out[tid] = name
    return out


def apply_filter(
    raw: Mapping[str, Any], flt: ToolResultFilter,
) -> tuple[dict[str, Any], FilterStats]:
    """对 ``raw`` 里所有 tool_result 文本跑 ``flt``，返回新请求 + 计量。

    支持 tool_result 的两种 content 形态：
    - 字符串
    - content block 列表（仅过滤 ``type=="text"`` 的块，image 等原样保留）
    """
    new = copy.deepcopy(dict(raw))
    stats = FilterStats()
    messages = new.get("messages")
    if not isinstance(messages, list):
        return new, stats

    cmd_idx = _command_index(messages)
    name_idx = _tool_name_index(messages)

    for msg in messages:
        if not isinstance(msg, Mapping) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_result":
                continue
            tid = item.get("tool_use_id", "")
            cmd = cmd_idx.get(tid, "")
            tool_name = name_idx.get(tid, "")
            _filter_tool_result(item, flt, cmd, tool_name, stats)
    return new, stats


def _filter_tool_result(
    item: dict[str, Any],
    flt: ToolResultFilter,
    command: str,
    tool_name: str,
    stats: FilterStats,
) -> None:
    """就地改写单个 tool_result block 的 content。"""
    content = item.get("content")

    if isinstance(content, str):
        rec = flt.filter_text(content, tool_name=tool_name, command=command)
        stats.blocks_seen += 1
        stats.original_chars += rec.original_chars
        stats.filtered_chars += rec.filtered_chars
        stats.by_rule[rec.rule] += 1
        if rec.saved_chars > 0:
            item["content"] = rec.text
            stats.blocks_filtered += 1
        return

    if isinstance(content, list):
        for blk in content:
            if not isinstance(blk, dict) or blk.get("type") != "text":
                continue
            text = blk.get("text")
            if not isinstance(text, str):
                continue
            rec = flt.filter_text(text, tool_name=tool_name, command=command)
            stats.blocks_seen += 1
            stats.original_chars += rec.original_chars
            stats.filtered_chars += rec.filtered_chars
            stats.by_rule[rec.rule] += 1
            if rec.saved_chars > 0:
                blk["text"] = rec.text
                stats.blocks_filtered += 1
