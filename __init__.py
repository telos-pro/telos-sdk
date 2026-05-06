"""STELA — Prompt Reuse via Indexed Span Marking.

三层 cache-友好 prompt 协议的 Python 参考实现。

公开入口：
    from stela import (
        # IR
        Band, StelaBlock, StelaMessage, StelaIR,
        # Bridge
        Bridge,
        # 工厂
        load_harness, load_engine,
    )

详见 stela/README.zh.md。
"""

from stela.ir import (
    Band,
    StelaBlock,
    StelaHints,
    StelaIR,
    StelaInvariantError,
    StelaMessage,
    UsageReport,
)
from stela.bridge import Bridge
from stela.engine.base import (
    BidirectionalEngineAdapter,
    EngineAdapter,
    EngineCapabilities,
    ProbeResult,
)
from stela.registry import load_engine, load_harness

__all__ = [
    "Band",
    "StelaBlock",
    "StelaHints",
    "StelaMessage",
    "StelaIR",
    "StelaInvariantError",
    "UsageReport",
    "Bridge",
    "EngineAdapter",
    "EngineCapabilities",
    "BidirectionalEngineAdapter",
    "ProbeResult",
    "load_harness",
    "load_engine",
]
