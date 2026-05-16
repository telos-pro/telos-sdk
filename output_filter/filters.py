"""工具结果过滤器 —— 把 bash / 命令输出在进 prompt 前压缩。

三个实现：

- ``RtkFilter``：shell-out 到 rtk 二进制（rtk-ai/rtk，Rust，100+ 命令）。
- ``FallbackFilter``：纯 Python 的轻量过滤器，只覆盖最常见的几类输出；
  rtk 没装时保证开关仍有效、demo 仍能跑出数。
- ``CompositeFilter``：rtk 优先、未命中或不可用时退回 fallback。

所有过滤器都遵守 RTK 同款「失败 → 原样」原则：任何异常都不会抛出，
最坏情况返回未改动的原文（``rule="passthrough"``）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass

from stela.output_filter.tokens import estimate_tokens


@dataclass
class FilterRecord:
    """单个 tool_result 过滤的结果 + 计量。

    ``original_tokens`` / ``filtered_tokens`` 是过滤前 / 后文本的 token
    估算（``tokens.estimate_tokens``），过滤时按真实文本算，比 dashboard
    端的 ``chars / 4`` 精确。
    """

    text: str
    original_chars: int
    filtered_chars: int
    rule: str  # 命中的规则名，如 "rtk:git", "fallback:dedup", "passthrough"
    original_tokens: int = 0
    filtered_tokens: int = 0

    @property
    def saved_chars(self) -> int:
        return max(0, self.original_chars - self.filtered_chars)

    @property
    def saved_tokens(self) -> int:
        return max(0, self.original_tokens - self.filtered_tokens)

    @classmethod
    def of(cls, before: str, after: str, *, rule: str) -> "FilterRecord":
        """从过滤前 / 后文本构造，自动算字符数与 token 估算。"""
        return cls(
            text=after,
            original_chars=len(before),
            filtered_chars=len(after),
            rule=rule,
            original_tokens=estimate_tokens(before),
            filtered_tokens=estimate_tokens(after),
        )

    @classmethod
    def passthrough(cls, text: str, *, rule: str = "passthrough") -> "FilterRecord":
        n = len(text)
        t = estimate_tokens(text)
        return cls(text=text, original_chars=n, filtered_chars=n, rule=rule,
                   original_tokens=t, filtered_tokens=t)


class ToolResultFilter(ABC):
    """把一段工具输出文本压缩成更短的等价文本。"""

    name: str = "abstract"

    @abstractmethod
    def filter_text(
        self, text: str, *, tool_name: str = "", command: str = "",
    ) -> FilterRecord:
        """``tool_name`` / ``command`` 是可选 hint：

        - ``tool_name``：发起这次工具调用的工具名（``Bash`` / ``Read`` …）。
        - ``command``：若是 Bash 调用，对应的 shell 命令串。
        """
        ...


# ---------------------------------------------------------------------------
# 纯 Python fallback
# ---------------------------------------------------------------------------

_MIN_FILTER_CHARS = 600    # 短输出不值得过滤
_TRUNCATE_BUDGET = 4000    # dedup 后仍超过此长度就头尾截断
_HEAD_KEEP = 2200
_TAIL_KEEP = 1400

_PYTEST_RE = re.compile(r"\bpytest\b|\bpy\.test\b|python -m pytest")
_PYTEST_SUMMARY_RE = re.compile(
    r"^=+ .*\b(passed|failed|error|skipped)\b.* =+$", re.IGNORECASE,
)


class FallbackFilter(ToolResultFilter):
    """无依赖的轻量过滤器：连续重复行折叠 + 头尾截断 + pytest 摘要。"""

    name = "fallback"

    def filter_text(
        self, text: str, *, tool_name: str = "", command: str = "",
    ) -> FilterRecord:
        try:
            return self._filter(text, command)
        except Exception:  # noqa: BLE001 — 过滤器永不抛错
            return FilterRecord.passthrough(text)

    def _filter(self, text: str, command: str) -> FilterRecord:
        original = len(text)
        if original < _MIN_FILTER_CHARS:
            return FilterRecord.passthrough(text)

        if _PYTEST_RE.search(command) or _PYTEST_RE.search(text[:400]):
            out, hit = self._pytest(text)
            if hit:
                return FilterRecord.of(text, out, rule="fallback:pytest")

        deduped = _collapse_repeats(text)
        rule = "fallback:dedup" if len(deduped) < original else "fallback:truncate"
        if len(deduped) > _TRUNCATE_BUDGET:
            deduped = _head_tail(deduped)
            rule = "fallback:truncate"
        if len(deduped) >= original:
            return FilterRecord.passthrough(text)
        return FilterRecord.of(text, deduped, rule=rule)

    @staticmethod
    def _pytest(text: str) -> tuple[str, bool]:
        """保留 FAILED/ERROR 行 + 最后的 ``=== N passed ===`` 摘要行。"""
        lines = text.splitlines()
        kept: list[str] = []
        for ln in lines:
            s = ln.strip()
            if (s.startswith(("FAILED", "ERROR", "PASSED"))
                    or _PYTEST_SUMMARY_RE.match(s)
                    or s.startswith("E   ")
                    or "::" in s and s.endswith(("PASSED", "FAILED", "ERROR"))):
                kept.append(ln)
        if len(kept) < 2:
            return text, False
        out = "\n".join(kept)
        elided = len(lines) - len(kept)
        if elided > 0:
            out += f"\n… [pytest: {elided} 行非失败输出已省略] …"
        return out, len(out) < len(text)


def _collapse_repeats(text: str) -> str:
    """连续相同行折叠成 ``<line>  (×N)``。"""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        j = i
        while j + 1 < len(lines) and lines[j + 1] == lines[i]:
            j += 1
        run = j - i + 1
        if run >= 3:
            out.append(f"{lines[i]}  (×{run})")
        else:
            out.extend(lines[i:j + 1])
        i = j + 1
    return "\n".join(out)


def _head_tail(text: str) -> str:
    head = text[:_HEAD_KEEP]
    tail = text[-_TAIL_KEEP:]
    elided = len(text) - _HEAD_KEEP - _TAIL_KEEP
    return f"{head}\n… [{elided:,} 字符中段已省略] …\n{tail}"


# ---------------------------------------------------------------------------
# rtk 二进制 adapter
# ---------------------------------------------------------------------------

class RtkFilter(ToolResultFilter):
    """shell-out 到 ``rtk`` 二进制。

    约定调用形式（rtk-ai/rtk 的离线过滤接口）::

        echo "<raw output>" | rtk filter --command "<shell command>"

    rtk 把过滤后的文本写 stdout。任何失败（找不到二进制、非零退出、
    超时）都退化为 passthrough —— 这一层永远不破坏正确性。
    """

    name = "rtk"

    def __init__(self, binary: str | None = None, timeout: float = 5.0) -> None:
        self._binary = binary or shutil.which("rtk")
        self._timeout = timeout

    @property
    def available(self) -> bool:
        return self._binary is not None

    def filter_text(
        self, text: str, *, tool_name: str = "", command: str = "",
    ) -> FilterRecord:
        if not self._binary or len(text) < _MIN_FILTER_CHARS:
            return FilterRecord.passthrough(text)
        argv = [self._binary, "filter"]
        if command:
            argv += ["--command", command]
        try:
            proc = subprocess.run(
                argv, input=text, capture_output=True, text=True,
                timeout=self._timeout, check=False,
            )
        except Exception:  # noqa: BLE001 — 二进制问题不应影响代理
            return FilterRecord.passthrough(text)
        if proc.returncode != 0 or not proc.stdout:
            return FilterRecord.passthrough(text)
        out = proc.stdout
        if len(out) >= len(text):
            return FilterRecord.passthrough(text)
        verb = (command.split() or ["?"])[0]
        return FilterRecord.of(text, out, rule=f"rtk:{verb}")


# ---------------------------------------------------------------------------
# 组合：rtk 优先，未命中退回 fallback
# ---------------------------------------------------------------------------

class CompositeFilter(ToolResultFilter):
    """先试 ``primary``，没省下字节再试 ``secondary``。"""

    name = "composite"

    def __init__(self, primary: ToolResultFilter, secondary: ToolResultFilter) -> None:
        self._primary = primary
        self._secondary = secondary

    def filter_text(
        self, text: str, *, tool_name: str = "", command: str = "",
    ) -> FilterRecord:
        rec = self._primary.filter_text(text, tool_name=tool_name, command=command)
        if rec.saved_chars > 0:
            return rec
        return self._secondary.filter_text(
            text, tool_name=tool_name, command=command,
        )


def build_filter(*, rtk_binary: str | None = None) -> ToolResultFilter:
    """构造默认过滤器。

    rtk 二进制可用 → ``CompositeFilter(rtk, fallback)``；
    不可用 → 纯 ``FallbackFilter``（开关仍生效，dashboard 上 rule 标
    ``fallback:*``，一眼能看出 rtk 没装）。
    """
    rtk = RtkFilter(binary=rtk_binary)
    fallback = FallbackFilter()
    if rtk.available:
        return CompositeFilter(rtk, fallback)
    return fallback
