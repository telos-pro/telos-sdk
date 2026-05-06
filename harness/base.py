"""Harness plugin 抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping

from stela.ir import StelaIR


class HarnessPlugin(ABC):
    """harness plugin = 把上游 agent 的原始请求翻译成 StelaIR 的纯函数。

    *无状态*：相同输入永远输出相同 IR。
    """

    @abstractmethod
    def parse(
        self,
        raw_request: Mapping[str, Any],
        *,
        session_id: str,
        engine: str,
        model: str = "",
        expected_turns: int = 0,
    ) -> StelaIR: ...
