"""Apply the tool-result filter to the raw ``/v1/messages`` request body.

Runs **before** the TELOS pipeline: when the rtk switch is on, after the
proxy receives a request it first uses ``apply_filter`` here to shorten the
large bash output inside ``messages[].content[].tool_result``, then
(optionally) hands it to the TELOS pipeline to apply cache marks.

A pure function: does not mutate its input, returns a new request dict +
``FilterStats`` metering.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Mapping

from telos.output_filter.filters import ToolResultFilter


@dataclass
class FilterStats:
    """The summary of all tool_result filtering in one request, written into usage_log.

    ``original_tokens`` / ``filtered_tokens`` are estimated by the filter on
    the real text (see ``filters.FilterRecord``); the dashboard prefers them
    over ``chars/4``.
    """

    original_chars: int = 0
    filtered_chars: int = 0
    original_tokens: int = 0
    filtered_tokens: int = 0
    blocks_seen: int = 0      # number of tool_result text blocks scanned
    blocks_filtered: int = 0  # number of blocks that actually saved bytes
    by_rule: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def saved_chars(self) -> int:
        return max(0, self.original_chars - self.filtered_chars)

    @property
    def saved_tokens(self) -> int:
        return max(0, self.original_tokens - self.filtered_tokens)

    def as_dict(self) -> dict[str, Any]:
        return {
            "original_chars": self.original_chars,
            "filtered_chars": self.filtered_chars,
            "saved_chars": self.saved_chars,
            "original_tokens": self.original_tokens,
            "filtered_tokens": self.filtered_tokens,
            "saved_tokens": self.saved_tokens,
            "blocks_seen": self.blocks_seen,
            "blocks_filtered": self.blocks_filtered,
            "by_rule": dict(self.by_rule),
        }


def _command_index(messages: list[Any]) -> dict[str, str]:
    """tool_use_id → shell command string (only Bash-type tools have ``input.command``)."""
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
    """tool_use_id → tool name."""
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
    """Run ``flt`` over all tool_result text in ``raw``, returning a new request + metering.

    Supports both content shapes of tool_result:
    - a string
    - a list of content blocks (only ``type=="text"`` blocks are filtered;
      images and the like are kept as-is)
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
    """Rewrite the content of a single tool_result block in place."""
    content = item.get("content")

    if isinstance(content, str):
        rec = flt.filter_text(content, tool_name=tool_name, command=command)
        stats.blocks_seen += 1
        stats.original_chars += rec.original_chars
        stats.filtered_chars += rec.filtered_chars
        stats.original_tokens += rec.original_tokens
        stats.filtered_tokens += rec.filtered_tokens
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
            stats.original_tokens += rec.original_tokens
            stats.filtered_tokens += rec.filtered_tokens
            stats.by_rule[rec.rule] += 1
            if rec.saved_chars > 0:
                blk["text"] = rec.text
                stats.blocks_filtered += 1
