"""OpenClaw harness plugin.

Input contract: OpenClaw's request body is shaped like Anthropic
``/v1/messages``, plus an optional ``metadata.openclaw`` field.

Band rules (consistent with TELOS protocol §7.1):
- ``tools[]``                                 → PIN
- ``system[]`` elements                       → PIN
- large documents / large file content (>2KB text) → FOLD, moved to the ref-pool
- ``messages[i].role=user`` text               → goes through ``_user_split.split_user_text``
- ``messages[i].role=assistant``                → FOLD (the whole message)
- the ``tool_result`` content of ``role=user``  → FOLD
"""

from __future__ import annotations

from typing import Any, Mapping

from telos.harness._user_split import split_user_text
from telos.harness.base import HarnessPlugin
from telos.ir import (
    Band,
    TelosBlock,
    TelosHints,
    TelosIR,
    TelosMessage,
    enforce_band_order,
)


_REFPOOL_THRESHOLD = 2048  # byte threshold; text longer than this is moved to the ref-pool

# The type prefixes of Anthropic built-in tools (``computer_``/``bash_``/
# ``text_editor_``/``web_search_``). Once recognized they are tagged
# ``source=builtin``, so that in the canonical sort they always come before
# MCP / user tools, protecting the prefix stability of the PIN segment (see
# bridge.py ``_tool_sort_key``).
_ANTHROPIC_BUILTIN_TYPE_PREFIXES = (
    "computer_", "bash_", "text_editor_", "web_search_",
)


def _classify_anthropic_tool(t: Mapping[str, Any]) -> tuple[str, str | None]:
    """Return ``(source, mcp_server)`` — used for ``TelosBlock.extra``.

    Priority:
    1. an explicit override from upstream ``metadata.source`` (``"builtin"|"mcp"|"user"``)
    2. ``type`` matches an Anthropic builtin prefix → ``builtin``
    3. contains a ``server`` / ``mcp_server`` field → ``mcp``
    4. otherwise ``user``
    """
    meta = t.get("metadata") if isinstance(t, Mapping) else None
    if isinstance(meta, Mapping):
        explicit = meta.get("source")
        if isinstance(explicit, str):
            return explicit, meta.get("mcp_server") if isinstance(meta.get("mcp_server"), str) else None
    explicit = t.get("source") if isinstance(t, Mapping) else None
    if isinstance(explicit, str):
        server = t.get("mcp_server") or t.get("server")
        return explicit, server if isinstance(server, str) else None
    typ = t.get("type") if isinstance(t, Mapping) else None
    if isinstance(typ, str) and typ.startswith(_ANTHROPIC_BUILTIN_TYPE_PREFIXES):
        return "builtin", None
    server = t.get("server") or t.get("mcp_server")
    if isinstance(server, str) and server:
        return "mcp", server
    return "user", None


class OpenClawPlugin(HarnessPlugin):
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
                source_tag="openclaw/tools",
                extra=extra,
            )

        tools = tuple(
            _build_tool(i, t)
            for i, t in enumerate(raw_request.get("tools", []))
        )

        # ---- system ----
        system_blocks: list[TelosBlock] = []
        raw_system = raw_request.get("system", [])
        if isinstance(raw_system, str):
            raw_system = [{"type": "text", "text": raw_system}]
        for i, item in enumerate(raw_system):
            text = item.get("text", "") if isinstance(item, dict) else str(item)
            if len(text) > _REFPOOL_THRESHOLD:
                slug = f"system-doc-{i}"
                ref_pool[slug] = TelosBlock(
                    id=f"ref:{slug}",
                    band=Band.FOLD,
                    kind="text",
                    payload=text,
                    ref_slug=slug,
                    source_tag="openclaw/system-large",
                )
                # leave a PIN reference in the system segment
                system_blocks.append(TelosBlock(
                    id=f"system/{i}-ref",
                    band=Band.PIN,
                    kind="text",
                    payload=f"See [ref:{slug}] for the full document.",
                    source_tag="openclaw/system-ref-stub",
                ))
            else:
                system_blocks.append(TelosBlock(
                    id=f"system/{i}",
                    band=Band.PIN,
                    kind="text",
                    payload=text,
                    source_tag="openclaw/system",
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
                        source_tag="openclaw/tool-result",
                    ))
                elif role == "assistant" and t == "text":
                    blocks.append(TelosBlock(
                        id=f"msg{mi}/at{ci}",
                        band=Band.FOLD,
                        kind="text",
                        payload=item.get("text", ""),
                        source_tag="openclaw/assistant-text",
                    ))
                elif role == "assistant" and t == "tool_use":
                    blocks.append(TelosBlock(
                        id=f"msg{mi}/au{ci}",
                        band=Band.FOLD,
                        kind="tool_use",
                        payload=item,
                        source_tag="openclaw/assistant-tool-use",
                    ))
                else:
                    blocks.append(TelosBlock(
                        id=f"msg{mi}/x{ci}",
                        band=Band.FOLD,
                        kind=t or "text",
                        payload=item,
                        source_tag="openclaw/other",
                    ))
            # Fix: concatenating multiple content blocks would make (PIN,DROP,PIN,DROP,...) violate §5.
            # Stably sort by band at the message level, restoring pin* → fold* → drop*.
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
