"""Engine adapter abstract base class and capability matrix.

Every adapter implements this interface; the bridge always works against
the interface and never branches on the engine name.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from telos.ir import TelosIR, UsageReport


# ---------------------------------------------------------------------------
# Capability matrix
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EngineCapabilities:
    """Declares which cache control primitives an engine supports.

    The bridge uses these booleans to decide whether to call the
    corresponding adapter method; an adapter must *never* silently turn
    an unsupported operation into a no-op — it must explicitly declare
    ``False``.
    """

    explicit_breakpoints: bool       #: Anthropic only
    ttl_control: Literal["none", "presets", "seconds"]
    prewarmable: bool                #: ``max_tokens:0``-style keep-alive
    routing_key: bool                #: OpenAI ``prompt_cache_key``
    retention_policy: Literal["fixed", "configurable"]
    max_breakpoints: int             #: 0 = no explicit BP
    thinking_preserved_across_non_tool_result: bool = False
    """Fix R6: True only for Opus 4.5+/Sonnet 4.6+; False for all earlier models and Haiku."""

    # —— Bidirectional capabilities (vLLM / SGLang only; all False for closed APIs) ——
    cache_probe: bool = False        #: client can read server-side cache hit status
    span_eviction: bool = False      #: client can explicitly release a span of KV blocks
    fork_and_replace: bool = False   #: SGLang radix fork: replace the tail while keeping the prefix
    tier_hint: bool = False          #: HiCache three-tier (GPU/CPU/disk) explicit hint
    pin_unpin: bool = False          #: explicit pin / unpin to prevent LRU eviction


# ---------------------------------------------------------------------------
# Mark slot abstraction —— the bridge only sees a list of slots, it does not
# know what cache_control looks like
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MarkSlot:
    """A single cache anchor's position + desired TTL.

    "Position" is a logical pointer: ``segment`` ∈ {``"tools"``, ``"system"``,
    ``"message"``} + ``index`` indicates which block within that segment. At
    ``emit`` time the adapter translates it into engine-private fields
    (Anthropic's ``cache_control``, an OpenAI ``prompt_cache_key``-derived
    hash, etc.).
    """

    name: str                        #: diagnostic: BP-T / BP-S / BP-R / BP-X / BP-mid
    segment: Literal["tools", "system", "message"]
    index: int                       #: block index within the segment; the message segment also needs message_index
    message_index: int | None = None
    ttl_class: Literal["short", "long", "none"] = "long"


@dataclass(frozen=True)
class EmitPlan:
    """Return value of ``Mark()``; the engine-private emit decision."""

    slots: tuple[MarkSlot, ...] = ()
    routing_key: str | None = None
    extras: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Adapter base class
# ---------------------------------------------------------------------------

class EngineAdapter(ABC):
    """Three methods + one property — the entire interface of an engine adapter."""

    @property
    @abstractmethod
    def capabilities(self) -> EngineCapabilities: ...

    @abstractmethod
    def plan_marks(self, ir: TelosIR) -> EmitPlan:
        """Decide where to place the anchors for this emit, based on the IR."""

    @abstractmethod
    def emit(self, ir: TelosIR, plan: EmitPlan) -> Mapping[str, Any]:
        """Translate IR + plan into a wire request (dict form; the caller POSTs it itself)."""

    @abstractmethod
    def parse_usage(self, response: Mapping[str, Any]) -> UsageReport:
        """Extract usage from the engine response, normalized into a ``UsageReport``."""

    def refresh(self, ir: TelosIR, plan: EmitPlan) -> None:
        """Optional: issue a keep-alive request; no-op by default."""
        return None


# ---------------------------------------------------------------------------
# Bidirectional mixin —— implemented only by vLLM / SGLang; the bridge uses
# isinstance to detect it
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProbeResult:
    """Result of a server-side prefix-cache hit query."""

    hit: bool
    cached_token_count: int = 0
    tier: Literal["gpu", "cpu", "disk", "none"] = "none"


class BidirectionalEngineAdapter(EngineAdapter):
    """The "read + write" control plane unique to open-source inference engines.

    An adapter implementing this abstract class must set the corresponding
    capability bits to True in ``capabilities``; the bridge runs an
    ``isinstance`` check before calling, and closed APIs do not implement
    this class, so the bridge will not call them by mistake.
    """

    def probe(self, ir: TelosIR, plan: EmitPlan) -> ProbeResult:
        """Ask the server: "Do you still have this prefix cached?"

        Returns a miss by default; concrete adapters override it. When it
        returns ``hit=True`` the bridge can skip the ``refresh`` request it
        was about to issue, saving one RTT.
        """
        return ProbeResult(hit=False)

    def evict_span(self, ir: TelosIR, start_block: int, end_block: int) -> Mapping[str, Any]:
        """Explicitly evict a span of KV blocks; returns the ``cache_policy``
        fragment to carry along with the next emit.

        The bridge calls this during a ``Fold``: the server releases the KV
        of the old span, and the next request only recomputes the much
        shorter summary tail."""
        return {}

    def fork_and_replace(
        self,
        ir: TelosIR,
        path_hash: str,
        replace_suffix: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Radix fork + suffix replacement; SGLang-exclusive, only partially
        supported by vLLM.

        Effect: keep the prefix KV corresponding to ``path_hash`` unchanged,
        and replace the span after it with ``replace_suffix`` (typically a
        short summary). This is ``Fold``'s true "zero recomputation"
        implementation — closed APIs simply cannot do this.
        """
        return {}
