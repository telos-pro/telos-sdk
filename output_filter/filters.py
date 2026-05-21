"""Tool-result filters — compress bash / command output before it enters the prompt.

Three implementations:

- ``RtkFilter``: shells out to the rtk binary (rtk-ai/rtk, Rust, 100+ commands).
- ``FallbackFilter``: a pure-Python lightweight filter covering only the most
  common output types; it guarantees the switch still works and the demo
  still produces numbers when rtk is not installed.
- ``CompositeFilter``: rtk first, falling back to the fallback when rtk misses
  or is unavailable.

All filters follow RTK's "fail → unchanged" principle: no exception is ever
raised, and in the worst case the unmodified original text is returned
(``rule="passthrough"``).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass

from telos.output_filter.tokens import estimate_tokens


@dataclass
class FilterRecord:
    """The result + metering of filtering a single tool_result.

    ``original_tokens`` / ``filtered_tokens`` are the token estimates of the
    pre- / post-filter text (``tokens.estimate_tokens``); filtering computes
    them from the real text, which is more accurate than the dashboard's
    ``chars / 4``.
    """

    text: str
    original_chars: int
    filtered_chars: int
    rule: str  # the name of the matched rule, e.g. "rtk:git", "fallback:dedup", "passthrough"
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
        """Construct from pre- / post-filter text, computing char counts and token estimates automatically."""
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
    """Compress a span of tool output text into a shorter equivalent text."""

    name: str = "abstract"

    @abstractmethod
    def filter_text(
        self, text: str, *, tool_name: str = "", command: str = "",
    ) -> FilterRecord:
        """``tool_name`` / ``command`` are optional hints:

        - ``tool_name``: the name of the tool that issued this call (``Bash`` / ``Read`` …).
        - ``command``: for a Bash call, the corresponding shell command string.
        """
        ...


# ---------------------------------------------------------------------------
# Pure-Python fallback
# ---------------------------------------------------------------------------

_MIN_FILTER_CHARS = 600    # short output is not worth filtering
_TRUNCATE_BUDGET = 4000    # if still over this length after dedup, truncate head and tail
_HEAD_KEEP = 2200
_TAIL_KEEP = 1400

_PYTEST_RE = re.compile(r"\bpytest\b|\bpy\.test\b|python -m pytest")
_PYTEST_SUMMARY_RE = re.compile(
    r"^=+ .*\b(passed|failed|error|skipped)\b.* =+$", re.IGNORECASE,
)


class FallbackFilter(ToolResultFilter):
    """A dependency-free lightweight filter: collapse consecutive duplicate lines + head/tail truncation + pytest summary."""

    name = "fallback"

    def filter_text(
        self, text: str, *, tool_name: str = "", command: str = "",
    ) -> FilterRecord:
        try:
            return self._filter(text, command)
        except Exception:  # noqa: BLE001 — the filter never raises
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
        """Keep FAILED/ERROR lines + the final ``=== N passed ===`` summary line."""
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
            out += f"\n… [pytest: {elided} non-failure output lines omitted] …"
        return out, len(out) < len(text)


def _collapse_repeats(text: str) -> str:
    """Collapse consecutive identical lines into ``<line>  (×N)``."""
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
    return f"{head}\n… [{elided:,} characters omitted from the middle] …\n{tail}"


# ---------------------------------------------------------------------------
# rtk binary adapter
# ---------------------------------------------------------------------------

class RtkFilter(ToolResultFilter):
    """Shells out to the ``rtk`` binary.

    The agreed-upon invocation form (rtk-ai/rtk's offline filtering interface)::

        echo "<raw output>" | rtk filter --command "<shell command>"

    rtk writes the filtered text to stdout. Any failure (binary not found,
    non-zero exit, timeout) degrades to passthrough — this layer never breaks
    correctness.
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
        except Exception:  # noqa: BLE001 — a binary problem should not affect the proxy
            return FilterRecord.passthrough(text)
        if proc.returncode != 0 or not proc.stdout:
            return FilterRecord.passthrough(text)
        out = proc.stdout
        if len(out) >= len(text):
            return FilterRecord.passthrough(text)
        verb = (command.split() or ["?"])[0]
        return FilterRecord.of(text, out, rule=f"rtk:{verb}")


# ---------------------------------------------------------------------------
# Composite: rtk first, fall back to fallback on a miss
# ---------------------------------------------------------------------------

class CompositeFilter(ToolResultFilter):
    """Try ``primary`` first, then try ``secondary`` if it did not save any bytes."""

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
    """Construct the default filter.

    rtk binary available → ``CompositeFilter(rtk, fallback)``;
    unavailable → plain ``FallbackFilter`` (the switch still takes effect, the
    rule on the dashboard is tagged ``fallback:*``, so you can see at a glance
    that rtk is not installed).
    """
    rtk = RtkFilter(binary=rtk_binary)
    fallback = FallbackFilter()
    if rtk.available:
        return CompositeFilter(rtk, fallback)
    return fallback
