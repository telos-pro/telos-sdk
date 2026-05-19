"""Hermes (Claude Code) harness plugin.

Input contract: same shape as OpenClaw (Anthropic ``/v1/messages``
compatible), but the envelope pattern differs — ``<system-reminder>`` /
``<command-message>`` are Hermes's hallmarks.

Differences (vs the item-by-item comparison with OpenClaw) see
[TELOS protocol §7.2]:
- a large ``<file>...</file>`` block (>2KB) → ref-pool, with the slug being the file path
- the result of a sub-agent (the Agent tool) goes through FOLD in the parent
  IR; the sub-IR is parsed separately by the caller through this plugin
  again, with a different session_id
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from telos.harness._user_split import split_user_text
from telos.harness.base import HarnessPlugin
from telos.harness.openclaw import _classify_anthropic_tool
from telos.ir import (
    Band,
    TelosBlock,
    TelosHints,
    TelosIR,
    TelosMessage,
    enforce_band_order,
)


_REFPOOL_THRESHOLD = 2048
_FILE_BLOCK_RE = re.compile(r'<file path="([^"]+)">(.*?)</file>', re.DOTALL)


class HermesPlugin(HarnessPlugin):
    def parse(
        self,
        raw_request: Mapping[str, Any],
        *,
        session_id: str,
        engine: str,
        model: str = "",
        expected_turns: int = 0,
    ) -> TelosIR:
        ref_pool: dict[str, TelosBlock] = {}

        # ---- tools ----
        def _build_tool(i: int, t: Mapping[str, Any]) -> TelosBlock:
            source, mcp_server = _classify_anthropic_tool(t)
            extra: dict[str, Any] = {"source": source}
            if mcp_server:
                extra["mcp_server"] = mcp_server
            return TelosBlock(
                id=f"tool:{t.get('name', i)}",
                band=Band.PIN,
                kind="tool_def",
                payload=t,
                source_tag="hermes/tools",
                extra=extra,
            )

        tools = tuple(
            _build_tool(i, t)
            for i, t in enumerate(raw_request.get("tools", []))
        )

        # ---- system ----
        system_blocks: list[TelosBlock] = []
        for i, item in enumerate(raw_request.get("system", [])):
            text = item.get("text", "") if isinstance(item, dict) else str(item)
            # Hermes's system may also contain <file> blocks; extract them first and put them in the ref-pool
            stripped = text
            for m in _FILE_BLOCK_RE.finditer(text):
                path, body = m.group(1), m.group(2)
                if len(body) > _REFPOOL_THRESHOLD:
                    slug = _slug_from_path(path)
                    if slug not in ref_pool:
                        ref_pool[slug] = TelosBlock(
                            id=f"ref:{slug}",
                            band=Band.FOLD,
                            kind="text",
                            payload=body,
                            ref_slug=slug,
                            source_tag="hermes/file-block",
                        )
                    stripped = stripped.replace(m.group(0), f"[ref:{slug}]")
            system_blocks.append(TelosBlock(
                id=f"system/{i}",
                band=Band.PIN,
                kind="text",
                payload=stripped,
                source_tag="hermes/system",
            ))

        # ---- messages ----
        messages: list[TelosMessage] = []
        for mi, msg in enumerate(raw_request.get("messages", [])):
            role = msg.get("role")
            content = msg.get("content", [])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            blocks: list[TelosBlock] = []
            for ci, item in enumerate(content):
                t = item.get("type")
                if role == "user" and t == "text":
                    blocks.extend(split_user_text(
                        item.get("text", ""), base_id=f"msg{mi}/blk{ci}",
                    ))
                elif role == "user" and t == "tool_result":
                    blocks.append(TelosBlock(
                        id=f"msg{mi}/tr{ci}",
                        band=Band.FOLD,
                        kind="tool_result",
                        payload=item,
                        source_tag="hermes/tool-result",
                    ))
                elif role == "assistant" and t == "text":
                    blocks.append(TelosBlock(
                        id=f"msg{mi}/at{ci}",
                        band=Band.FOLD,
                        kind="text",
                        payload=item.get("text", ""),
                        source_tag="hermes/assistant-text",
                    ))
                elif role == "assistant" and t == "tool_use":
                    blocks.append(TelosBlock(
                        id=f"msg{mi}/au{ci}",
                        band=Band.FOLD,
                        kind="tool_use",
                        payload=item,
                        source_tag="hermes/assistant-tool-use",
                    ))
                elif role == "assistant" and t == "thinking":
                    # Fix R6: thinking blocks default to FOLD and cannot have cache_control attached directly
                    blocks.append(TelosBlock(
                        id=f"msg{mi}/th{ci}",
                        band=Band.FOLD,
                        kind="thinking",
                        payload=item,
                        source_tag="hermes/thinking",
                    ))
                else:
                    blocks.append(TelosBlock(
                        id=f"msg{mi}/x{ci}",
                        band=Band.FOLD,
                        kind=t or "text",
                        payload=item,
                        source_tag="hermes/other",
                    ))
            # Fix: concatenating multiple content blocks would make (PIN,DROP,PIN,DROP,...) violate §5.
            messages.append(TelosMessage(role=role, blocks=enforce_band_order(blocks)))

        return TelosIR(
            session_id=session_id,
            tools=tools,
            system=tuple(system_blocks),
            messages=tuple(messages),
            ref_pool=ref_pool,
            hints=TelosHints(
                engine=engine,  # type: ignore[arg-type]
                model=model,
                expected_turns=expected_turns,
            ),
        )


def _slug_from_path(path: str) -> str:
    """``src/auth/login.py`` → ``src.auth.login.py``; stays human-readable and stable across sessions."""
    return path.replace("/", ".")
