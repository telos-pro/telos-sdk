"""``telos.output_filter`` —— RTK 风格的工具结果过滤层。

公开入口::

    from telos.output_filter import TelosMode, build_filter, apply_filter

    mode = TelosMode.from_label("both")     # none|telos|rtk|both
    if mode.rtk:
        flt = build_filter()
        new_raw, stats = apply_filter(raw_request, flt)

这一层与 TELOS 管线正交：TELOS 稳「请求前缀」拿 KV cache，本层缩
「工具结果尾巴」减少每轮新增 token。两者可独立开关，见 ``TelosMode``。
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
