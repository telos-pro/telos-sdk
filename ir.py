"""STELA IR：三层之间唯一通过的数据结构。

设计原则
--------
1. **不可变** —— 所有 dataclass 都是 frozen；bridge 的"修改"动作返回新 IR，
   不在原对象上改字节。这样上游 agent 即使把同一个 IR 发给多个 bridge，
   也不会出现共享状态写竞争。
2. **窄字段** —— 字段越少越难用错。任何 engine-specific 的旋钮都不放进
   IR，由 engine adapter 在 emit 时自己决定。
3. **三色一刀切** —— 每个 block 必须落在 ``pin / fold / drop`` 之一；
   没有"既可缓存又不可缓存"的灰色态。

不变量（由 bridge 在每次原语调用前后再校验，详见 ``bridge.py``）
--------------------------------------------------------------
**§5 顺序不变量**：在每个段（``tools`` / ``system`` / 每条 ``message``）
内部，blocks 必须按 ``pin* → fold* → drop*`` 物理顺序排列。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Literal, Mapping


# ---------------------------------------------------------------------------
# Band（三色带）
# ---------------------------------------------------------------------------

class Band(str, Enum):
    """每个 block 的缓存生命周期类别。

    - ``PIN``：长寿稳定段。tools 定义、system prompt、用户当下提问。
      默认请求 1h TTL（Anthropic）/ 24h retention（OpenAI）。
    - ``FOLD``：可缓存但 compact 时可丢弃。assistant 回答、tool_result、
      ref-pool 大文档。默认请求 5m TTL。
    - ``DROP``：永远不进 cache hash。timestamp、cwd、git status、
      ``<system-reminder>`` envelope。必须出现在所属段的最末。
    """

    PIN = "pin"
    FOLD = "fold"
    DROP = "drop"


_BAND_RANK: Mapping[Band, int] = {Band.PIN: 0, Band.FOLD: 1, Band.DROP: 2}


# ---------------------------------------------------------------------------
# Block / Message / IR
# ---------------------------------------------------------------------------

BlockKind = Literal[
    "text",
    "tool_def",
    "tool_use",
    "tool_result",
    "image",
    "thinking",
]


@dataclass(frozen=True)
class StelaBlock:
    """STELA 中最小的内容单元。

    一个 block 等价于一段 "engine 看作不可拆的 cache 计算粒度"。这与
    Anthropic 的 content block、OpenAI 的 message-piece 在大多数情况下
    一一对应。
    """

    id: str                       #: 会话内稳定标识（用于诊断 / 引用）
    band: Band
    kind: BlockKind
    payload: Any                  #: engine-agnostic 内容；emit 时由 adapter 翻译
    ref_slug: str | None = None   #: 若非空，此 block 来自 ref-pool（详见 §4）
    source_tag: str | None = None #: 诊断字段：哪条 harness 规则把它分到此 band
    extra: Mapping[str, Any] = field(default_factory=dict)
    """放 engine 可能需要的稳定旁信息，例如 image 的 ``detail`` 字段。

    *必须* 进 cache hash 的字段都写到 ``extra`` 里、由 adapter 在 emit
    时一并序列化；harness 决不能在 emit 时刻才注入这类字段，否则 §5
    保证的字节稳定性被破坏。
    """


@dataclass(frozen=True)
class StelaMessage:
    """一条对话 message（user / assistant）。

    与一个 OpenAI / Anthropic 的 message 对应，但内部 blocks **必须**
    按 §5 顺序排列。最常见的场景是 user message 被强制切成
    ``(pin: 用户提问) + (fold: 历史回声 / [ref:...] 引用) + (drop: harness envelope)``。
    """

    role: Literal["system", "user", "assistant"]
    blocks: tuple[StelaBlock, ...]


@dataclass(frozen=True)
class StelaIR:
    """harness → bridge → engine 之间唯一的传输对象。"""

    session_id: str
    tools: tuple[StelaBlock, ...]                  #: 全部 band=PIN（schema 不变）
    system: tuple[StelaBlock, ...]                 #: pin* → fold*（fold 部分含 ref-pool）→ drop*
    messages: tuple[StelaMessage, ...]
    ref_pool: Mapping[str, StelaBlock]             #: slug → block
    hints: "StelaHints" = field(default_factory=lambda: StelaHints())


@dataclass(frozen=True)
class StelaHints:
    """非强制的元信息，engine adapter 用来做 plan 决策。"""

    engine: Literal["anthropic", "openai", "deepseek"] = "anthropic"
    model: str = ""
    expected_turns: int = 0       #: harness 预估的总轮数；影响 mid-rolling 锚的开关


# ---------------------------------------------------------------------------
# Engine 输出后再回流的 usage 报告（§9）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UsageReport:
    """统一 usage 返回格式。

    与 ``benchmark/scripts/compute-metrics.py`` 的 ``raw_input / cache_read /
    cache_write`` 三元 schema 对齐——任何 engine 的原始 ``usage`` 字段都
    需归一到这三个数，否则 north-star 指标算不出来。
    """

    raw_input: int                 #: 未命中且未写缓存的 input token
    cache_read: int                #: 从缓存读入
    cache_write: int               #: 本次请求写入缓存的新增 token
    output: int
    raw: Mapping[str, Any] = field(default_factory=dict)  #: 原始 usage 字段，留作诊断


# ---------------------------------------------------------------------------
# §5 顺序不变量校验
# ---------------------------------------------------------------------------

class StelaInvariantError(ValueError):
    """§5 / canonical 化 / ref-pool 注册任一不变量被破坏时抛出。"""


def assert_band_order(blocks: tuple[StelaBlock, ...], where: str) -> None:
    """断言 blocks 满足 ``pin* → fold* → drop*`` 严格顺序。

    复杂度 O(n)，单次扫描；bridge 在每次原语调用前后都会跑一次，开销
    可以忽略——这是整个协议的"安全门"。
    """
    last_rank = -1
    for blk in blocks:
        rank = _BAND_RANK[blk.band]
        if rank < last_rank:
            raise StelaInvariantError(
                f"Band order violated in {where}: block {blk.id!r} has band "
                f"{blk.band.value!r} after a higher-band block. Required order "
                f"is pin* -> fold* -> drop*."
            )
        last_rank = rank


def assert_ir_invariants(ir: StelaIR) -> None:
    """对整份 IR 跑一次完整的 §5 校验。"""
    assert_band_order(ir.tools, "tools")
    if any(b.band is not Band.PIN for b in ir.tools):
        raise StelaInvariantError("All blocks in `tools` must have band=PIN")
    assert_band_order(ir.system, "system")
    for i, msg in enumerate(ir.messages):
        assert_band_order(msg.blocks, f"messages[{i}] (role={msg.role})")


# ---------------------------------------------------------------------------
# 便利构造器（harness plugin 常用，所以放进 IR 模块本体）
# ---------------------------------------------------------------------------

def with_messages(ir: StelaIR, messages: tuple[StelaMessage, ...]) -> StelaIR:
    """返回替换 ``messages`` 后的新 IR；方便函数式风格的 bridge 操作。"""
    return replace(ir, messages=messages)


def with_ref_pool(ir: StelaIR, ref_pool: Mapping[str, StelaBlock]) -> StelaIR:
    return replace(ir, ref_pool=dict(ref_pool))
