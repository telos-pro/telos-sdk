"""``telos.output_filter`` — the RTK-style tool-result filtering layer.

Public entry points::

    from telos.output_filter import TelosMode, build_filter, apply_filter

    mode = TelosMode.from_label("both")     # none|telos|rtk|both
    if mode.rtk:
        flt = build_filter()
        new_raw, stats = apply_filter(raw_request, flt)

This layer is orthogonal to the TELOS pipeline: TELOS stabilizes the "request
prefix" to obtain KV cache, while this layer shrinks the "tool-result tail"
to reduce the new tokens added each turn. The two can be switched
independently — see ``TelosMode``.
"""

from telos.output_filter.filters import (
    CompositeFilter,
    FallbackFilter,
    FilterRecord,
    RtkFilter,
    ToolResultFilter,
    build_filter,
)
from telos.output_filter.mode import DEFAULT_MODE, MODE_LABELS, TelosMode
from telos.output_filter.preprocess import FilterStats, apply_filter

__all__ = [
    "TelosMode",
    "DEFAULT_MODE",
    "MODE_LABELS",
    "ToolResultFilter",
    "FallbackFilter",
    "RtkFilter",
    "CompositeFilter",
    "FilterRecord",
    "build_filter",
    "FilterStats",
    "apply_filter",
]
