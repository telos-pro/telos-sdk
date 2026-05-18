"""TELOS — Prompt Reuse via Indexed Span Marking.

三层 cache-友好 prompt 协议的 Python 参考实现。

公开入口：
    from telos import (
        # IR
        Band, TelosBlock, TelosMessage, TelosIR,
        # Bridge
        Bridge,
        # 工厂
        load_harness, load_engine,
    )

详见 telos/README.zh.md。
"""

from telos.ir import (
    Band,
    TelosBlock,
    TelosHints,
    TelosIR,
    TelosInvariantError,
    TelosMessage,
    UsageReport,
)
from telos.bridge import Bridge
from telos.engine.base import (
    BidirectionalEngineAdapter,
    EngineAdapter,
    EngineCapabilities,
    ProbeResult,
)
from telos.registry import load_engine, load_harness
from telos.scripts.transport import TelosTransport, HarnessPreset, PRESETS

__all__ = [
    "Band",
    "TelosBlock",
    "TelosHints",
    "TelosMessage",
    "TelosIR",
    "TelosInvariantError",
    "UsageReport",
    "Bridge",
    "EngineAdapter",
    "EngineCapabilities",
    "BidirectionalEngineAdapter",
    "ProbeResult",
    "load_harness",
    "load_engine",
    "TelosTransport",
    "HarnessPreset",
    "PRESETS",
]
