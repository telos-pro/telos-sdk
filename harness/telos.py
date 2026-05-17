"""Telos harness plugin。

输入约定：**OpenAI ChatCompletions** 形态的 ``raw_request``：

::

    {
      "model": "...",
      "messages": [
        {"role": "system",    "content": "..."},
        {"role": "user",      "content": "..."},
        {"role": "assistant", "content": "...", "tool_calls": [...]},
        {"role": "tool",      "content": "...", "tool_call_id": "..."},
      ],
      "tools": [{"type": "function", "function": {...}}, ...],
    }

这是 telos 的 vendored Hermes ``mini_swe_runner`` 与上层 chat 循环
共同发出的格式（通过 ``openai.OpenAI().chat.completions.create``）。

与 ``hermes.py``（解析 Anthropic ``/v1/messages`` 形态）的区别：

- ``messages[].content`` 通常是字符串，而非 content-block 数组；
- 子调用通过 ``role="tool"`` 单独成 message，而不是嵌在 user 里的
  ``tool_result`` 块；
- ``tools[].function.parameters`` 而不是 ``tools[].input_schema``。

ref-pool 触发同样依靠 ``<file path="...">...</file>`` 块（>2KB）—— Telos
agent 在 SWE-bench prompt 里不一定会塞这种 envelope，但保留兼容。
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from telos.harness._user_split import split_user_text
from telos.harness.base import HarnessPlugin
from telos.ir import (
    Band,
    TelosBlock,
    TelosHints,
    TelosIR,
    TelosMessage,
)


_REFPOOL_THRESHOLD = 2048
_FILE_BLOCK_RE = re.compile(r'<file path="([^"]+)">(.*?)</file>', re.DOTALL)


def _classify_openai_tool(t: Mapping[str, Any]) -> tuple[str, str | None]:
    """OpenAI function-tool 的分类：仅靠上游 metadata。

    OpenAI 的 ``tools`` 数组没有原生的 builtin/mcp 划分；harness 如果要
    进一步区分，需在上举的 ``raw_request`` 里为每个 tool 塑 ``metadata.source``
    （可选加 ``metadata.mcp_server``）。否则默认 ``user``。
    """
    meta = t.get("metadata") if isinstance(t, Mapping) else None
    if isinstance(meta, Mapping):
        src = meta.get("source")
        server = meta.get("mcp_server")
        if isinstance(src, str):
            return src, server if isinstance(server, str) else None
    return "user", None


def _slug_from_path(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", path).strip("-").lower() or "file"


def _coerce_content_to_text(content: Any) -> str:
    """OpenAI message ``content`` 可能是 str 或 list[{type,text}]；统一成字符串。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


class TelosPlugin(HarnessPlugin):
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

        # ---- tools (OpenAI function-tool schema) ----
        def _build_tool(i: int, t: Mapping[str, Any]) -> TelosBlock:
            source, mcp_server = _classify_openai_tool(t)
            extra: dict[str, Any] = {"source": source}
            if mcp_server:
                extra["mcp_server"] = mcp_server
            return TelosBlock(
                id=f"tool:{(t.get('function') or {}).get('name', i)}",
                band=Band.PIN,
                kind="tool_def",
                payload=t,
                source_tag="telos/tools",
                extra=extra,
            )

        tools = tuple(
            _build_tool(i, t)
            for i, t in enumerate(raw_request.get("tools") or [])
        )

        # ---- system + messages 在 OpenAI shape 下混在 messages[] 里。
        # 切：开头连续若干条 role=system 进 system 段；其余进 messages。
        raw_messages = list(raw_request.get("messages") or [])
        system_blocks: list[TelosBlock] = []
        cursor = 0
        for cursor, msg in enumerate(raw_messages):
            if msg.get("role") != "system":
                break
            text = _coerce_content_to_text(msg.get("content"))
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
                            source_tag="telos/file-block",
                        )
                    stripped = stripped.replace(m.group(0), f"[ref:{slug}]")
            system_blocks.append(TelosBlock(
                id=f"system/{len(system_blocks)}",
                band=Band.PIN,
                kind="text",
                payload=stripped,
                source_tag="telos/system",
            ))
        else:
            # 全是 system；cursor 落在末尾，需手动推进
            cursor = len(raw_messages)

        # ---- 其余 message（user / assistant / tool）----
        messages: list[TelosMessage] = []
        for mi, msg in enumerate(raw_messages[cursor:], start=cursor):
            role = msg.get("role")
            text_content = _coerce_content_to_text(msg.get("content"))

            if role == "user":
                blocks = list(split_user_text(text_content, base_id=f"msg{mi}"))
                messages.append(TelosMessage(role="user", blocks=tuple(blocks)))

            elif role == "assistant":
                blocks: list[TelosBlock] = []
                if text_content:
                    blocks.append(TelosBlock(
                        id=f"msg{mi}/at",
                        band=Band.FOLD,
                        kind="text",
                        payload=text_content,
                        source_tag="telos/assistant-text",
                    ))
                for ti, tc in enumerate(msg.get("tool_calls") or []):
                    blocks.append(TelosBlock(
                        id=f"msg{mi}/au{ti}",
                        band=Band.FOLD,
                        kind="tool_use",
                        payload=tc,
                        source_tag="telos/assistant-tool-use",
                    ))
                if not blocks:
                    # 空 assistant message：跳过，避免 IR 出现空 message
                    continue
                messages.append(TelosMessage(role="assistant", blocks=tuple(blocks)))

            elif role == "tool":
                # OpenAI: 独立 role=tool message。TELOS 协议里 tool_result
                # 必须挂在 user message 内（与 Anthropic 对齐），所以包成 user。
                payload = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": text_content,
                }
                blocks = (TelosBlock(
                    id=f"msg{mi}/tr",
                    band=Band.FOLD,
                    kind="tool_result",
                    payload=payload,
                    source_tag="telos/tool-result",
                ),)
                messages.append(TelosMessage(role="user", blocks=blocks))

            elif role == "system":
                # 中途出现的 system（不常见）：当 PIN 文本插进 user 段。
                blocks = (TelosBlock(
                    id=f"msg{mi}/sys",
                    band=Band.PIN,
                    kind="text",
                    payload=text_content,
                    source_tag="telos/inline-system",
                ),)
                messages.append(TelosMessage(role="user", blocks=blocks))

        return TelosIR(
            session_id=session_id,
            tools=tools,
            system=tuple(system_blocks),
            messages=tuple(messages),
            ref_pool=ref_pool,
            hints=TelosHints(
                engine=engine if engine in ("anthropic", "openai", "deepseek") else "openai",  # type: ignore[arg-type]
                model=model or raw_request.get("model", ""),
                expected_turns=expected_turns,
            ),
        )
