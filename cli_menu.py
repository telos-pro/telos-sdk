"""A minimal interactive terminal menu (standard library only).

In non-TTY environments (pipes / CI / subprocesses) it never blocks on
``input()``: it either returns ``default`` or raises ``RuntimeError``.
"""

from __future__ import annotations

import sys
from typing import Sequence, TypeVar

T = TypeVar("T")


def is_interactive() -> bool:
    """An interactive menu can only run when both stdin and stdout are TTYs."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def select_from(
    options: Sequence[tuple[T, str]],
    *,
    prompt: str = "Please select",
    default_index: int | None = None,
) -> T:
    """Numbered menu: ``options`` is a list of ``(value, label)``, returns the selected value.

    In a non-interactive environment, returns the item at ``default_index``;
    raises if there is no default.
    """
    if not options:
        raise RuntimeError("No options available.")

    if not is_interactive():
        if default_index is not None:
            return options[default_index][0]
        raise RuntimeError("Non-interactive terminal, cannot display the menu (please specify arguments explicitly).")

    print(prompt)
    for i, (_, label) in enumerate(options, 1):
        marker = " (default)" if default_index is not None and i - 1 == default_index else ""
        print(f"  {i}. {label}{marker}")

    while True:
        suffix = f" [1-{len(options)}]"
        if default_index is not None:
            suffix += f", Enter={default_index + 1}"
        try:
            raw = input(f"{suffix}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise RuntimeError("Cancelled.") from None
        if not raw and default_index is not None:
            return options[default_index][0]
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx][0]
        print(f"  Invalid input, please enter a number between 1 and {len(options)}.")


def confirm(prompt: str, *, default: bool = True) -> bool:
    """y/n confirmation. Returns ``default`` directly in a non-interactive environment."""
    if not is_interactive():
        return default
    hint = "Y/n" if default else "y/N"
    try:
        raw = input(f"{prompt} [{hint}] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not raw:
        return default
    return raw in ("y", "yes")


def select_many(
    options: Sequence[tuple[T, str]],
    *,
    prompt: str = "Toggle items",
    default_selected: Sequence[T] = (),
) -> list[T]:
    """Multi-select checklist: returns the list of chosen values.

    UX: shows a numbered list with ``[x]`` / ``[ ]`` markers, accepts repeated
    inputs like ``"1 3"`` to toggle, ``"a"`` to select all, ``"n"`` to clear,
    Enter to confirm. No curses; plain stdin/stdout.

    In a non-interactive environment, returns ``default_selected`` as-is
    (deterministic behavior for scripts / CI).

    Selection is tracked by **index** internally so ``T`` need not be hashable.
    Equality of values is checked via ``==`` to seed ``default_selected``.
    """
    if not options:
        return []
    values = [v for v, _ in options]
    # Seed selection: any index whose value matches one in ``default_selected``.
    selected_idx: set[int] = {
        i for i, v in enumerate(values)
        if any(v == dv for dv in default_selected)
    }

    if not is_interactive():
        # Preserve the order from ``options``.
        return [values[i] for i in sorted(selected_idx)]

    print(prompt)

    def _render() -> None:
        for i, (_v, label) in enumerate(options, 1):
            mark = "[x]" if (i - 1) in selected_idx else "[ ]"
            print(f"  {mark} {i}. {label}")

    _render()
    while True:
        try:
            raw = input(
                "Type numbers to toggle (e.g. '1 3'), 'a'=all, 'n'=none, "
                "Enter to confirm > "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            raise RuntimeError("Cancelled.") from None
        if not raw:
            return [values[i] for i in sorted(selected_idx)]
        if raw == "a":
            selected_idx = set(range(len(values)))
        elif raw == "n":
            selected_idx = set()
        else:
            tokens = raw.replace(",", " ").split()
            for tok in tokens:
                if not tok.isdigit():
                    print(f"  Ignored: {tok!r}")
                    continue
                idx = int(tok) - 1
                if not (0 <= idx < len(options)):
                    print(f"  Out of range: {tok}")
                    continue
                if idx in selected_idx:
                    selected_idx.discard(idx)
                else:
                    selected_idx.add(idx)
        _render()
