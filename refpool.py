"""Ref-pool：所有大段内容的"指针表"。

设计要点
--------
- 每个 ref-pool 条目 *绑定一个稳定的 slug*，这个 slug 在整个会话期内
  不能变。emit-time lint 会扫描所有文本 block 内的 ``[ref:<slug>]`` 引用，
  发现未注册的 slug 直接抛 ``TelosInvariantError``。
- compact 时 *只换 payload 不换 slug*：``[ref:login.py]`` 字符串原地不动，
  user/assistant 中所有引用点的字节因此不变 —— 这是 Janus §8 提到的
  "引用天然折叠"的真正落地方式。
- slug 命名规则：``[A-Za-z0-9_\-./]+``，禁止时间戳 / 版本号 / hash，
  规范化时大小写敏感。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from telos.ir import Band, TelosBlock, TelosInvariantError


SLUG_RE = re.compile(r"^[A-Za-z0-9_\-./]+$")
REF_RE = re.compile(r"\[ref:([A-Za-z0-9_\-./]+)\]")


@dataclass
class RefPool:
    """会话级 ref-pool 状态。

    bridge 把它当作"一个段"来管理：注册时 freeze slug，emit 时按字典序
    渲染进 system 段尾部（保证字节稳定）。
    """

    _entries: dict[str, TelosBlock] = field(default_factory=dict)

    # ------ 注册与替换 ------

    def register(self, slug: str, block: TelosBlock) -> None:
        """首次注册 slug；slug 一经注册不可重命名（违反 I3）。"""
        if not SLUG_RE.match(slug):
            raise TelosInvariantError(
                f"Invalid ref slug {slug!r}; must match {SLUG_RE.pattern}"
            )
        if slug in self._entries:
            raise TelosInvariantError(
                f"Slug {slug!r} already registered; ref-pool slugs are frozen"
            )
        if block.ref_slug != slug:
            raise TelosInvariantError(
                f"Block.ref_slug ({block.ref_slug!r}) must equal slug ({slug!r})"
            )
        if block.band is not Band.FOLD:
            raise TelosInvariantError(
                "Ref-pool entries must have band=FOLD (foldable on compact)"
            )
        self._entries[slug] = block

    def register_or_skip(self, slug: str, block: TelosBlock) -> bool:
        """Idempotent register —— 跨 turn 共享 RefPool 时用。

        - slug 未注册 → 走标准 register
        - slug 已注册 → 跳过（保留现有 entry，可能已被 fold 成占位符）

        关键不变量：harness 每轮都生产完整 payload 的 ref_pool；本方法防止
        第二轮把第一轮 fold 过的 entry 覆盖回完整内容。

        返回 True 表示真的注册了，False 表示跳过。
        """
        if slug in self._entries:
            return False
        self.register(slug, block)
        return True

    def fold(self, slug: str, *, summary: str | None = None) -> TelosBlock:
        """把 ref-pool 条目折叠成短占位符；返回新 block。

        slug 不动，``[ref:slug]`` 引用点字节不变 → 后续 BP 仍可命中。
        """
        if slug not in self._entries:
            raise TelosInvariantError(f"Cannot fold unregistered slug {slug!r}")
        old = self._entries[slug]
        placeholder = summary or f"<folded ref:{slug}>"
        new = TelosBlock(
            id=old.id,
            band=Band.FOLD,
            kind="text",
            payload=placeholder,
            ref_slug=slug,
            source_tag="ref-pool/folded",
        )
        self._entries[slug] = new
        return new

    # ------ 查询与遍历 ------

    @property
    def slugs(self) -> frozenset[str]:
        return frozenset(self._entries.keys())

    def render_blocks(self) -> tuple[TelosBlock, ...]:
        """按 slug 字典序渲染条目；保证多次 emit 字节稳定。"""
        return tuple(self._entries[k] for k in sorted(self._entries.keys()))

    def to_mapping(self) -> dict[str, TelosBlock]:
        return dict(self._entries)

    # ------ 引用 lint（§4 / §8.5 L1）------

    def lint_text(self, text: str, where: str) -> None:
        """扫描文本里所有 ``[ref:slug]``，未注册的 slug 直接 fail-fast。"""
        for m in REF_RE.finditer(text):
            slug = m.group(1)
            if slug not in self._entries:
                raise TelosInvariantError(
                    f"Unregistered ref slug {slug!r} in {where}; "
                    f"register via Pin() before emitting."
                )

    def lint_blocks(self, blocks: Iterable[TelosBlock], where: str) -> None:
        for blk in blocks:
            if blk.kind == "text" and isinstance(blk.payload, str):
                self.lint_text(blk.payload, f"{where}/block:{blk.id}")
