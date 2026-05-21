"""Harness plugin abstract base class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping

from telos.ir import TelosIR


class HarnessPlugin(ABC):
    """A harness plugin = a pure function that translates an upstream agent's raw request into a TelosIR.

    *Stateless*: the same input always produces the same IR.
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
    ) -> TelosIR: ...
