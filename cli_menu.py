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
