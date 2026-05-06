"""OpenClaw harness plugin。

输入约定：OpenClaw 的请求体形如 Anthropic ``/v1/messages``，外加可选的
``metadata.openclaw`` 字段。

带类规则（与 STELA 协议 §7.1 一致）：
- ``tools[]``                                 → PIN
- ``system[]`` 元素                            → PIN
- 大文档 / 大文件内容（>2KB 文本）               → FOLD，搬到 ref-pool
- ``messages[i].role=user`` 文本               → 走 ``_user_split.split_user_text``
- ``messages[i].role=assistant``                → FOLD（整条）
- ``role=user`` 的 ``tool_result`` 内容         → FOLD
"""

from __future__ import annotations

from typing import Any, Mapping

from stela.harness._user_split import split_user_text
from stela.harness.base import HarnessPlugin
from stela.ir import (
    Band,
    StelaBlock,
    StelaHints,
    StelaIR,
    StelaMessage,
)


_REFPOOL_THRESHOLD = 2048  # 字节阈值；超过此长度的文本搬到 ref-pool


class OpenClawPlugin(HarnessPlugin):
    def parse(
        self,
        raw_request: Mapping[str, Any],
        *,
        session_id: str,
        engine: str,
        model: str = "",
        expected_turns: int = 0,
    ) -> StelaIR:
        ref_pool: dict[str, StelaBlock] = {}

        # ---- tools ----
        tools = tuple(
            StelaBlock(
                id=f"tool:{t.get('name', i)}",
                band=Band.PIN,
                kind="tool_def",
                payload=t,
                source_tag="openclaw/tools",
            )
            for i, t in enumerate(raw_request.get("tools", []))
        )

        # ---- system ----
        system_blocks: list[StelaBlock] = []
        raw_system = raw_request.get("system", [])
        if isinstance(raw_system, str):
            raw_system = [{"type": "text", "text": raw_system}]
        for i, item in enumerate(raw_system):
            text = item.get("text", "") if isinstance(item, dict) else str(item)
            if len(text) > _REFPOOL_THRESHOLD:
                slug = f"system-doc-{i}"
                ref_pool[slug] = StelaBlock(
                    id=f"ref:{slug}",
                    band=Band.FOLD,
                    kind="text",
                    payload=text,
                    ref_slug=slug,
                    source_tag="openclaw/system-large",
                )
                # 在 system 段留一个 PIN 引用
                system_blocks.append(StelaBlock(
                    id=f"system/{i}-ref",
                    band=Band.PIN,
                    kind="text",
                    payload=f"See [ref:{slug}] for the full document.",
                    source_tag="openclaw/system-ref-stub",
                ))
            else:
                system_blocks.append(StelaBlock(
                    id=f"system/{i}",
                    band=Band.PIN,
                    kind="text",
                    payload=text,
                    source_tag="openclaw/system",
                ))

        # ---- messages ----
        messages: list[StelaMessage] = []
        for mi, msg in enumerate(raw_request.get("messages", [])):
            role = msg.get("role")
            content = msg.get("content", [])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            blocks: list[StelaBlock] = []
            for ci, item in enumerate(content):
                t = item.get("type")
                if role == "user" and t == "text":
                    blocks.extend(split_user_text(
                        item.get("text", ""), base_id=f"msg{mi}/blk{ci}",
                    ))
                elif role == "user" and t == "tool_result":
                    blocks.append(StelaBlock(
                        id=f"msg{mi}/tr{ci}",
                        band=Band.FOLD,
                        kind="tool_result",
                        payload=item,
                        source_tag="openclaw/tool-result",
                    ))
                elif role == "assistant" and t == "text":
                    blocks.append(StelaBlock(
                        id=f"msg{mi}/at{ci}",
                        band=Band.FOLD,
                        kind="text",
                        payload=item.get("text", ""),
                        source_tag="openclaw/assistant-text",
                    ))
                elif role == "assistant" and t == "tool_use":
                    blocks.append(StelaBlock(
                        id=f"msg{mi}/au{ci}",
                        band=Band.FOLD,
                        kind="tool_use",
                        payload=item,
                        source_tag="openclaw/assistant-tool-use",
                    ))
                else:
                    blocks.append(StelaBlock(
                        id=f"msg{mi}/x{ci}",
                        band=Band.FOLD,
                        kind=t or "text",
                        payload=item,
                        source_tag="openclaw/other",
                    ))
            messages.append(StelaMessage(role=role, blocks=tuple(blocks)))

        return StelaIR(
            session_id=session_id,
            tools=tools,
            system=tuple(system_blocks),
            messages=tuple(messages),
            ref_pool=ref_pool,
            hints=StelaHints(
                engine=engine,  # type: ignore[arg-type]
                model=model,
                expected_turns=expected_turns,
            ),
        )
