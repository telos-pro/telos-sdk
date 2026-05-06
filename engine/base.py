"""Engine adapter 抽象基类与能力矩阵。

每个 adapter 都实现这个接口；bridge 永远只看接口，不分支判断 engine 名。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from stela.ir import StelaIR, UsageReport


# ---------------------------------------------------------------------------
# 能力矩阵
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EngineCapabilities:
    """声明 engine 支持哪些 cache 控制原语。

    bridge 用这些 bool 决定是否调对应的 adapter 方法；adapter 自己
    *绝不能* 在不支持时偷偷做 no-op，必须显式声明 ``False``。
    """

    explicit_breakpoints: bool       #: 仅 Anthropic
    ttl_control: Literal["none", "presets", "seconds"]
    prewarmable: bool                #: ``max_tokens:0`` 风格的 keep-alive
    routing_key: bool                #: OpenAI ``prompt_cache_key``
    retention_policy: Literal["fixed", "configurable"]
    max_breakpoints: int             #: 0 = 无显式 BP
    thinking_preserved_across_non_tool_result: bool = False
    """修复 R6：Opus 4.5+/Sonnet 4.6+ 才为 True；早期模型与 Haiku 全为 False。"""

    # —— 双向能力（vLLM / SGLang 才有；闭源 API 全部 False）————————————
    cache_probe: bool = False        #: 客户端可以读 server 端缓存命中状态
    span_eviction: bool = False      #: 客户端可以显式释放某段 KV 块
    fork_and_replace: bool = False   #: SGLang radix fork：在保留前缀的前提下替换尾段
    tier_hint: bool = False          #: HiCache 三级（GPU/CPU/disk）显式提示
    pin_unpin: bool = False          #: 显式 pin / unpin 防 LRU 淘汰


# ---------------------------------------------------------------------------
# Mark slot 抽象 —— bridge 只看到 slot 列表，不知道 cache_control 长啥样
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MarkSlot:
    """单个 cache 锚位的位置 + 期望 TTL。

    "位置"是逻辑指针：``segment`` ∈ {``"tools"``, ``"system"``, ``"message"``}
    + ``index`` 表示在该段内的第几个 block。adapter 在 ``emit`` 时把它翻译
    成 engine 私有字段（Anthropic 的 ``cache_control``、OpenAI 的
    ``prompt_cache_key`` 衍生 hash 等）。
    """

    name: str                        #: 诊断用：BP-T / BP-S / BP-R / BP-X / BP-mid
    segment: Literal["tools", "system", "message"]
    index: int                       #: segment 内 block index；message 段还需 message_index
    message_index: int | None = None
    ttl_class: Literal["short", "long", "none"] = "long"


@dataclass(frozen=True)
class EmitPlan:
    """``Mark()`` 的返回值；engine 私有的 emit 决策。"""

    slots: tuple[MarkSlot, ...] = ()
    routing_key: str | None = None
    extras: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Adapter 基类
# ---------------------------------------------------------------------------

class EngineAdapter(ABC):
    """三个方法 + 一个属性，engine adapter 的全部接口。"""

    @property
    @abstractmethod
    def capabilities(self) -> EngineCapabilities: ...

    @abstractmethod
    def plan_marks(self, ir: StelaIR) -> EmitPlan:
        """根据 IR 决定本次 emit 把锚位放在哪。"""

    @abstractmethod
    def emit(self, ir: StelaIR, plan: EmitPlan) -> Mapping[str, Any]:
        """把 IR + plan 翻译成 wire request（dict 形态，调用方自己 POST）。"""

    @abstractmethod
    def parse_usage(self, response: Mapping[str, Any]) -> UsageReport:
        """从 engine response 提取 usage，归一为 ``UsageReport``。"""

    def refresh(self, ir: StelaIR, plan: EmitPlan) -> None:
        """可选：发起 keep-alive 请求；默认 no-op。"""
        return None


# ---------------------------------------------------------------------------
# 双向 mixin —— 仅 vLLM / SGLang 实现；bridge 用 isinstance 判断
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProbeResult:
    """server 端 prefix-cache 命中查询结果。"""

    hit: bool
    cached_token_count: int = 0
    tier: Literal["gpu", "cpu", "disk", "none"] = "none"


class BidirectionalEngineAdapter(EngineAdapter):
    """开源推理引擎独有的"读 + 写"控制面。

    实现这个抽象类的 adapter，必须在 ``capabilities`` 里把对应的能力位
    设成 True；bridge 在调用前会 ``isinstance`` 判断，闭源 API 不实现这个类
    所以 bridge 不会误调。
    """

    def probe(self, ir: StelaIR, plan: EmitPlan) -> ProbeResult:
        """问 server："你那边还缓存着这个前缀吗？"

        默认返回 miss；具体 adapter 覆盖。返回 ``hit=True`` 时 bridge 可以
        跳过即将发起的 ``refresh`` 请求，省一次 RTT。
        """
        return ProbeResult(hit=False)

    def evict_span(self, ir: StelaIR, start_block: int, end_block: int) -> Mapping[str, Any]:
        """显式淘汰一段 KV 块；返回随下次 emit 一起带的 ``cache_policy`` 片段。

        bridge 在做 ``Fold`` 时调用：服务端释放旧 span 的 KV，下次请求只
        重算 summary 这段短得多的尾部。"""
        return {}

    def fork_and_replace(
        self,
        ir: StelaIR,
        path_hash: str,
        replace_suffix: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """radix fork + suffix 替换；SGLang 专属，vLLM 仅部分支持。

        作用：保留 ``path_hash`` 对应的前缀 KV 不变，把它后面那段换成
        ``replace_suffix``（通常是一段短摘要）。这是 ``Fold`` 真正的"零重算"
        实现——闭源 API 完全做不到。
        """
        return {}
