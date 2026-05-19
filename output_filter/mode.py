"""``TelosMode`` — the 4-state combination of two independent switches (TELOS / RTK).

Four-state table:

| label   | telos | rtk  | meaning                                       |
|---------|-------|------|-----------------------------------------------|
| ``none``  | ✗   | ✗  | pure passthrough, the proxy rewrites no bytes  |
| ``telos`` | ✓   | ✗  | run the TELOS pipeline only (cache_control / ref-pool) |
| ``rtk``   | ✗   | ✓  | only run RTK tool-output filtering, apply no cache marks |
| ``both``  | ✓   | ✓  | RTK shrinks tool results first, then TELOS stabilizes the prefix (recommended default) |

Designed as "two booleans" rather than a single enum: when the proxy /
dashboard each only care about one dimension, they do not have to repeatedly
match all 4 branches — ``mode.telos`` / ``mode.rtk`` can be read directly.
"""

from __future__ import annotations

from dataclasses import dataclass

_LABEL_TO_FLAGS: dict[str, tuple[bool, bool]] = {
    "none": (False, False),
    "telos": (True, False),
    "rtk": (False, True),
    "both": (True, True),
}

# all valid labels, used for CLI / argparse choices.
MODE_LABELS: tuple[str, ...] = ("none", "telos", "rtk", "both")


@dataclass(frozen=True)
class TelosMode:
    """Which optimization layers a request enables. Immutable, safe to reuse across sessions."""

    telos: bool = True
    rtk: bool = False

    @property
    def label(self) -> str:
        for name, flags in _LABEL_TO_FLAGS.items():
            if flags == (self.telos, self.rtk):
                return name
        # In theory unreachable: two booleans must fall into one of the 4 states.
        return f"telos={self.telos},rtk={self.rtk}"

    @classmethod
    def from_label(cls, label: str | None) -> "TelosMode":
        """Parse ``none|telos|rtk|both`` into a ``TelosMode``.

        ``None`` / an empty string / an unknown value all degrade to the
        default ``telos`` (preserving the historical behavior: before the
        switch was introduced, the proxy was equivalent to "TELOS only").
        """
        if not label:
            return cls()
        flags = _LABEL_TO_FLAGS.get(label.strip().lower())
        if flags is None:
            return cls()
        return cls(telos=flags[0], rtk=flags[1])


DEFAULT_MODE = TelosMode()
