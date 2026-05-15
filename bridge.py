"""Bridge：STELA 的策略核心。五个原语 + 一次 canonicalize。

```
upstream agent → harness.parse() → IR
                                    │
                                    ▼
                          ┌────────────────────┐
                          │     Bridge          │
                          │   place / pin /     │
                          │   mark  / fold /    │
                          │   refresh           │
                          └─────────┬──────────┘
                                    │ IR (改写后)
                                    ▼
                          engine.emit() → wire request
                          engine.parse_usage() → UsageReport
```

Bridge 是**有状态**的（每个 session 一个实例）：
- 维护 ref-pool（slug 一旦注册即冻结）
- 维护"自上次 mark 以来真实请求数"，给 ``refresh`` 自适应门控用（修复 R8）
- 维护一个累计 ``cache_creation`` 计数，达到阈值就提示上游 ``Fold``

Bridge **不**记录任何 engine 私有状态（breakpoint slot 编号、TTL slot 等）—
那些由 engine adapter 在 ``plan_marks`` 时按需重算。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from stela.engine.base import (
    BidirectionalEngineAdapter,
    EmitPlan,
    EngineAdapter,
    ProbeResult,
)
from stela.ir import (
    Band,
    StelaBlock,
    StelaIR,
    StelaInvariantError,
    StelaMessage,
    UsageReport,
    assert_band_order,
    assert_ir_invariants,
)
from stela.refpool import RefPool


# ---------------------------------------------------------------------------
# Bridge 跨 turn 持久化的状态
# ---------------------------------------------------------------------------

@dataclass
class _SessionStats:
    """跟踪自上次 refresh 以来的真实请求数（用于 refresh 自适应门控）。"""

    real_requests_since_refresh: int = 0
    cumulative_cache_creation: int = 0
    last_refresh_at: float = field(default_factory=time.monotonic)


@dataclass
class BridgeSessionState:
    """一个 conversation session 的全部跨 turn 状态。

    设计意图：上游（proxy / SDK transport）可以按 session_id 持有一份
    ``BridgeSessionState``，每轮请求构造新的 ``Bridge`` 时传进来。这样：

    - ref-pool slug 一次注册、全 session 共享（fold 状态跨轮保持）
    - R8 自适应 refresh 的请求计数能真累积
    - cache_creation 累计计数能真累积，触发上游 fold 提示

    缺省（``None``）时 Bridge 自己 new 一个，行为退化到"每轮独立"——与
    之前完全等价，保证不破坏现有调用方。
    """

    refpool: RefPool = field(default_factory=RefPool)
    stats: _SessionStats = field(default_factory=_SessionStats)
    # 一旦某个 session 的 harness 被识别出来（hermes / openclaw），后续
    # 同 session 的请求直接复用，避免每条 call 重新探测带来的"openclaw ↔
    # hermes 翻转 → source_tag 前缀不一致 → ref-pool slug 失配"。显式
    # 传入 ``harness_name`` 始终覆盖这个字段。
    sticky_harness: str | None = None
    # 同理：一个 session 第一次见到的 mode（none/stela/rtk/both）被锁定，
    # 后续同 session 的请求复用，避免对比实验里某个 session 中途换挡。
    # proxy 配置默认值仍可被首个请求的 X-Stela-Mode header 覆盖一次。
    sticky_mode: str | None = None
    # 对比实验分组标签（X-Stela-Compare-Group header）。同一 compare_group
    # 下、mode 不同的多个 session 会在 dashboard 上并排展示。
    compare_group: str | None = None


REFRESH_THRESHOLD = 11  # Janus §6.3.1：每续期间至少 11 次真实请求才回本


# ---------------------------------------------------------------------------
# Canonicalization（修复 R5：跨 engine 通用，必须在 emit 前统一做掉）
# ---------------------------------------------------------------------------

# JSON-Schema 中"集合语义"的数组键：order-insensitive，排序后字节稳定。
# 仅在 tool_def 的 schema 子树里被认作集合；tool_use / tool_result 的 payload
# 是用户数据，绝不动（payload 里碰巧叫 ``required`` 的字段不能被静默重排）。
#
# 故意 *不* 排序的键（与 Janus tools.ts 同步保守）：
#   - ``enum``        : 顺序常被用作 tie-break 偏好
#   - ``examples``    : 文档示例可能是有意排序的
#   - ``anyOf`` / ``oneOf`` / ``allOf`` : spec 无序，但 prompt 里常见
#                       "prefer first matching schema" 这种语义
# 暴露为模块级名字，方便特殊 harness 在 import 后 monkey-patch。
_SCHEMA_SET_ARRAY_KEYS: frozenset[str] = frozenset({"required"})

# 工具来源排序：builtin 先、MCP 次、user 最末；未标 tag 排到最后（safe default）。
_TOOL_SOURCE_RANK: Mapping[str, int] = {"builtin": 0, "mcp": 1, "user": 2}
_TOOL_SOURCE_DEFAULT_RANK = 3


def _canonicalize_payload(payload: Any) -> Any:
    """对 dict 类 payload 做 key 排序；其它类型原样返回。

    Anthropic 文档明确指出 Swift / Go 的 JSON 序列化会随机化 key 顺序，
    导致 cache 失效。DeepSeek 的 prefix 是 exact-match，OpenAI 的 prefix
    是 hash —— 所有 engine 都受影响。所以放在 bridge 而不是 adapter。

    本函数 *不* 触碰集合语义数组（如 ``required``）—— 那只发生在
    ``_canonicalize_schema``，且仅作用于 tool_def 的 schema 子树。
    """
    if isinstance(payload, dict):
        return {k: _canonicalize_payload(payload[k]) for k in sorted(payload.keys())}
    if isinstance(payload, list):
        return [_canonicalize_payload(x) for x in payload]
    return payload


def _canonicalize_schema(node: Any, *, parent_key: str | None = None) -> Any:
    """JSON-Schema 子树专用规范化：dict key 排序 + 集合语义数组排序。

    与 ``_canonicalize_payload`` 的差别仅在于：当一个 list 的父键属于
    ``_SCHEMA_SET_ARRAY_KEYS`` 时，按字符串排序而不是保留原序。这样
    ``required: ["b","a"]`` 与 ``["a","b"]`` 字节相同，杜绝
    ``list(set(...))`` 类的隐式失序破坏 prefix cache。
    """
    if isinstance(node, dict):
        return {
            k: _canonicalize_schema(node[k], parent_key=k)
            for k in sorted(node.keys())
        }
    if isinstance(node, list):
        if parent_key in _SCHEMA_SET_ARRAY_KEYS:
            sorted_items = sorted(node, key=lambda x: str(x))
            return [_canonicalize_schema(x) for x in sorted_items]
        return [_canonicalize_schema(x) for x in node]
    return node


def _canonicalize_tool_def(payload: Any) -> Any:
    """规范化 tool_def payload：识别 Anthropic / OpenAI 两种 schema 字段。

    - Anthropic shape: ``{"name", "description", "input_schema": {...}}``
    - OpenAI   shape: ``{"type": "function", "function": {"name", "description",
                          "parameters": {...}}}``

    schema 子树（``input_schema`` / ``parameters``）走 ``_canonicalize_schema``，
    其余字段走普通 ``_canonicalize_payload``。未识别的形态退化为整体
    ``_canonicalize_payload`` —— 与改动前等价。
    """
    if not isinstance(payload, dict):
        return _canonicalize_payload(payload)

    # Anthropic shape
    if "input_schema" in payload:
        out: dict[str, Any] = {}
        for k in sorted(payload.keys()):
            v = payload[k]
            out[k] = _canonicalize_schema(v) if k == "input_schema" else _canonicalize_payload(v)
        return out

    # OpenAI function-tool shape
    fn = payload.get("function")
    if isinstance(fn, dict) and ("parameters" in fn or "name" in fn):
        out = {}
        for k in sorted(payload.keys()):
            v = payload[k]
            if k == "function":
                inner: dict[str, Any] = {}
                for fk in sorted(v.keys()):
                    fv = v[fk]
                    inner[fk] = _canonicalize_schema(fv) if fk == "parameters" else _canonicalize_payload(fv)
                out[k] = inner
            else:
                out[k] = _canonicalize_payload(v)
        return out

    return _canonicalize_payload(payload)


def _canonicalize_block(blk: StelaBlock) -> StelaBlock:
    """规范化单个 block：tool 定义 / tool_use / tool_result 的字段排序。

    ``tool_def`` 走 schema-aware 路径（额外规范化 ``required`` 等集合数组）；
    ``tool_use`` / ``tool_result`` 只做 dict key 排序 —— 它们的 payload 是
    用户数据，不能改动数组顺序。
    """
    if blk.kind == "tool_def":
        return replace(blk, payload=_canonicalize_tool_def(blk.payload))
    if blk.kind in ("tool_use", "tool_result"):
        return replace(blk, payload=_canonicalize_payload(blk.payload))
    return blk


def _tool_name(blk: StelaBlock) -> str:
    """从 tool_def block 提取工具名（Anthropic / OpenAI 两种 shape 都覆盖）。"""
    p = blk.payload
    if isinstance(p, dict):
        name = p.get("name")
        if isinstance(name, str):
            return name
        fn = p.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str):
                return name
    return blk.id


def _tool_sort_key(blk: StelaBlock) -> tuple[int, str, str]:
    """工具数组的稳定排序键：``(source_rank, mcp_server, name)``。

    - ``source_rank`` : builtin(0) → mcp(1) → user(2) → 未标记(3)
    - ``mcp_server``  : 同为 MCP 时按 server 名稳定，避免多 server 启动竞态
                        在两个 server 之间交替插入而破坏 prefix
    - ``name``        : 组内字典序

    所有 PIN 工具排序后仍是 PIN，不破坏 §5 band 顺序（``assert_band_order``
    在 emit 时再校验一次）。``extra["source"]`` 与 ``extra["mcp_server"]``
    是 harness 约定的 tag —— 缺失时退化为最末位（safe default）。
    """
    extra = blk.extra or {}
    src = extra.get("source") if isinstance(extra, Mapping) else None
    if isinstance(src, str):
        rank = _TOOL_SOURCE_RANK.get(src, _TOOL_SOURCE_DEFAULT_RANK)
    else:
        rank = _TOOL_SOURCE_DEFAULT_RANK
    server = extra.get("mcp_server") if isinstance(extra, Mapping) else None
    return (rank, str(server or ""), _tool_name(blk))


def _canonicalize_ir(ir: StelaIR) -> StelaIR:
    # tools: 每个 block 内部规范化 → 整个数组稳定排序（仍全是 PIN，不破坏 §5）
    canon_tools = sorted(
        (_canonicalize_block(b) for b in ir.tools),
        key=_tool_sort_key,
    )
    new_tools = tuple(canon_tools)

    new_system = tuple(_canonicalize_block(b) for b in ir.system)
    new_messages = tuple(
        StelaMessage(role=m.role, blocks=tuple(_canonicalize_block(b) for b in m.blocks))
        for m in ir.messages
    )
    return replace(ir, tools=new_tools, system=new_system, messages=new_messages)


# ---------------------------------------------------------------------------
# Bridge 主体
# ---------------------------------------------------------------------------

class Bridge:
    """每个 session 一个实例。线程不安全（同一 session 通常顺序处理）。

    跨 turn 状态外置：传入 ``session_state`` 让 ref-pool + R8 计数器跨调用
    累积；不传则每个 Bridge 独立持有状态（与早期版本行为等价）。
    """

    def __init__(
        self,
        ir: StelaIR,
        engine: EngineAdapter,
        *,
        session_state: BridgeSessionState | None = None,
    ):
        self._ir = ir
        self._engine = engine
        self._state = session_state if session_state is not None else BridgeSessionState()
        # 把当前 IR 里的 ref_pool 同步进 state.refpool。
        # 用 register_or_skip：第二次起，已注册的 slug（可能已被 fold 改成
        # 占位符）不会被新一轮的完整 payload 覆盖回去。
        for slug, blk in ir.ref_pool.items():
            self._state.refpool.register_or_skip(slug, blk)
        # 初始 IR 也走一遍 §5 校验，避免 harness plugin 偷懒
        assert_ir_invariants(self._ir)

    @property
    def session_state(self) -> BridgeSessionState:
        """暴露外置状态。上游可读 ``cumulative_cache_creation`` 等做诊断。"""
        return self._state

    # 后向兼容：旧代码可能直接读这些字段。保留 property 读路径。
    @property
    def _refpool(self) -> RefPool:  # type: ignore[override]
        return self._state.refpool

    @property
    def _stats(self) -> _SessionStats:  # type: ignore[override]
        return self._state.stats

    # ------------------------------------------------------------------
    # 五个原语
    # ------------------------------------------------------------------

    def place(self, segment: str, blocks: tuple[StelaBlock, ...]) -> "Bridge":
        """**Place**：替换某个段（``"tools"`` / ``"system"`` / ``"messages"``）的
        全部 blocks，并立即重新跑 §5 校验。

        Place 是显式的"接受新 IR"动作，harness 在每次新 turn 来临时调用。
        """
        if segment == "tools":
            assert_band_order(blocks, "tools")
            if any(b.band is not Band.PIN for b in blocks):
                raise StelaInvariantError("tools blocks must all be band=PIN")
            self._ir = replace(self._ir, tools=blocks)
        elif segment == "system":
            assert_band_order(blocks, "system")
            self._ir = replace(self._ir, system=blocks)
        else:
            raise ValueError(f"Unknown segment for place(): {segment!r}")
        return self

    def append_message(self, msg: StelaMessage) -> "Bridge":
        """**Place** 的 message 专用快捷方式：追加一条新 message。

        每次追加都校验 message 内部的 §5 顺序——这是修复 Janus C6 的
        关键，user message 的 envelope 必须切到 ``DROP`` 子块。
        """
        assert_band_order(msg.blocks, f"new message (role={msg.role})")
        self._ir = replace(self._ir, messages=self._ir.messages + (msg,))
        return self

    def pin(self, slug: str, payload: str, *, source_tag: str | None = None) -> "Bridge":
        """**Pin**：注册一个 ref-pool 条目，slug 立即冻结。

        注意 Pin 注册的是 ``band=FOLD`` 的可折叠条目（与原语名"Pin"看似
        矛盾，但 Pin 这里指的是"把这段大内容固定在 ref-pool 里、给它
        一个稳定的指针"，不是"band=PIN"）。
        """
        blk = StelaBlock(
            id=f"ref:{slug}",
            band=Band.FOLD,
            kind="text",
            payload=payload,
            ref_slug=slug,
            source_tag=source_tag or "ref-pool/registered",
        )
        self._refpool.register(slug, blk)
        # 把 ref-pool 渲染到 system 段尾部（§4 所有大内容都集中在这里）
        self._sync_refpool_into_system()
        return self

    def mark(self) -> EmitPlan:
        """**Mark**：让 engine adapter 决定本次 emit 的 cache 锚位。

        bridge 不知道 cache_control / prompt_cache_key 这些 engine 私有
        概念，把决策完全委托给 adapter。
        """
        return self._engine.plan_marks(self._ir)

    def fold(
        self,
        *,
        slugs: tuple[str, ...] = (),
        message_range: tuple[int, int] | None = None,
        summary: str = "<folded prior turns>",
    ) -> "Bridge":
        """**Fold**：折叠 ref-pool 条目，或把一段历史 message 折叠成摘要。

        修复 R4：Fold 后所有落在 fold 区域之后的 Mark slot 必须由下次
        ``mark()`` 重新规划——bridge 不缓存 plan，所以这是天然成立的。

        参数：
            slugs: 要折叠的 ref-pool slug 列表（仅替换 payload，slug 不动）
            message_range: ``(start, end)`` 半开区间，把这段历史 message
                替换为单个 ``band=FOLD`` 的 summary message
            summary: message 折叠时使用的占位文本
        """
        for slug in slugs:
            self._refpool.fold(slug)
        self._sync_refpool_into_system()

        if message_range is not None:
            start, end = message_range
            if not (0 <= start < end <= len(self._ir.messages)):
                raise StelaInvariantError(
                    f"Invalid message_range {message_range!r} for "
                    f"{len(self._ir.messages)} messages"
                )
            placeholder = StelaMessage(
                role="user",
                blocks=(
                    StelaBlock(
                        id=f"folded:{start}-{end}",
                        band=Band.FOLD,
                        kind="text",
                        payload=summary,
                        source_tag="bridge/fold-history",
                    ),
                ),
            )
            new_msgs = (
                self._ir.messages[:start]
                + (placeholder,)
                + self._ir.messages[end:]
            )
            self._ir = replace(self._ir, messages=new_msgs)
        return self

    def refresh(self, plan: EmitPlan) -> bool:
        """**Refresh**：触发 engine 的 keep-alive；若 engine 不支持则 no-op。

        修复 R8：自适应门控——窗口内真实请求数低于阈值就跳过续期，
        让 cache 自然过期。这避免低活跃 session 续期成本 > 收益。
        """
        if not self._engine.capabilities.prewarmable:
            return False
        if self._stats.real_requests_since_refresh < REFRESH_THRESHOLD:
            return False
        self._engine.refresh(self._ir, plan)
        self._stats.last_refresh_at = time.monotonic()
        self._stats.real_requests_since_refresh = 0
        return True

    # ------------------------------------------------------------------
    # emit / 回流：bridge 还要负责把 engine 返回的 usage 归一化
    # ------------------------------------------------------------------

    def emit(self) -> Mapping[str, Any]:
        """规范化 → 校验 → 委托 engine.emit() 出 wire 请求。"""
        wire, _ = self.emit_with_plan()
        return wire

    def emit_with_plan(self) -> tuple[Mapping[str, Any], EmitPlan]:
        """``emit()`` 的二元返回版：同时拿到 wire 和当次使用的 EmitPlan。

        proxy / transport 想记 plan 诊断（slot 名、routing_key 等）的时候用
        这个，避免它们自己重复跑 canonicalize + plan_marks。
        """
        canon = _canonicalize_ir(self._ir)
        # 渲染前再做一次完整 §5 校验（修改 IR 的入口很多，最后一道防线）
        assert_ir_invariants(canon)
        # ref-pool lint：扫描所有文本 block 内的 [ref:...] 引用
        self._state.refpool.lint_blocks(canon.system, "system")
        for i, m in enumerate(canon.messages):
            self._state.refpool.lint_blocks(m.blocks, f"messages[{i}]")
        plan = self._engine.plan_marks(canon)
        wire = self._engine.emit(canon, plan)
        self._state.stats.real_requests_since_refresh += 1
        return wire, plan

    def absorb_usage(self, raw_response: Mapping[str, Any]) -> UsageReport:
        """解析 engine response，更新 cache_creation 累计计数。"""
        report = self._engine.parse_usage(raw_response)
        self._stats.cumulative_cache_creation += report.cache_write
        return report

    # ------------------------------------------------------------------
    # 诊断 / 调试
    # ------------------------------------------------------------------

    @property
    def cumulative_cache_creation(self) -> int:
        return self._stats.cumulative_cache_creation

    # ------------------------------------------------------------------
    # 双向操作（仅 vLLM / SGLang 等开源推理实现；闭源 API 全 no-op）
    # ------------------------------------------------------------------

    @property
    def is_bidirectional(self) -> bool:
        return isinstance(self._engine, BidirectionalEngineAdapter)

    def probe_cache(self) -> ProbeResult:
        """**Probe**：问 server 端"前缀还在缓存里吗？"

        闭源 API 直接返回 ``hit=False``；vLLM / SGLang 真正发起 lookup。
        bridge 用这个结果决定是否跳过即将发起的 ``refresh``，省一次 RTT。
        """
        if not isinstance(self._engine, BidirectionalEngineAdapter):
            return ProbeResult(hit=False)
        plan = self._engine.plan_marks(self._ir)
        return self._engine.probe(self._ir, plan)

    def cooperative_fold(
        self,
        *,
        slugs: tuple[str, ...] = (),
        message_range: tuple[int, int] | None = None,
        summary: str = "<folded prior turns>",
    ) -> Mapping[str, Any]:
        """**协同 Fold**：客户端折叠 + 服务端 evict-span / fork-and-replace。

        与普通 ``fold()`` 不同：本方法不仅改 IR，还返回一个 ``cache_control``
        / ``cache_policy`` 片段，由 caller 合并进下一次 emit 的 plan extras。
        服务端拿到后真正释放旧 KV 块（vLLM）或 fork radix 路径（SGLang），
        实现"零重算 Fold"——这是闭源 API 完全做不到的。

        闭源 API 上调用本方法等同于 ``fold()`` + 返回 ``{}``。
        """
        # 先做客户端侧的 IR rewrite（与普通 fold 相同）
        self.fold(slugs=slugs, message_range=message_range, summary=summary)
        if not isinstance(self._engine, BidirectionalEngineAdapter):
            return {}

        caps = self._engine.capabilities
        # 优先走 fork_and_replace（SGLang 全支持，vLLM 部分支持）
        if caps.fork_and_replace and message_range is not None:
            plan = self._engine.plan_marks(self._ir)
            path_hash = plan.extras.get("path_hash") or plan.routing_key or ""
            return self._engine.fork_and_replace(
                self._ir,
                path_hash=path_hash,
                replace_suffix={"text": summary},
            )
        # 退而求其次：evict_span（vLLM 主路径）
        if caps.span_eviction and message_range is not None:
            start, end = message_range
            return self._engine.evict_span(self._ir, start, end)
        return {}

    def emit_with_extras(self, extras: Mapping[str, Any]) -> Mapping[str, Any]:
        """``emit()`` 的扩展版：允许 caller 把双向操作返回的 cache_control
        片段合并进 plan.extras。

        典型用法：

            ctrl = bridge.cooperative_fold(message_range=(2, 8), summary="…")
            wire = bridge.emit_with_extras(ctrl)
        """
        canon = _canonicalize_ir(self._ir)
        assert_ir_invariants(canon)
        self._refpool.lint_blocks(canon.system, "system")
        for i, m in enumerate(canon.messages):
            self._refpool.lint_blocks(m.blocks, f"messages[{i}]")
        plan = self._engine.plan_marks(canon)
        merged = EmitPlan(
            slots=plan.slots,
            routing_key=plan.routing_key,
            extras={**dict(plan.extras), **dict(extras)},
        )
        wire = self._engine.emit(canon, merged)
        self._stats.real_requests_since_refresh += 1
        return wire

    def snapshot_ir(self) -> StelaIR:
        """返回当前 IR 的快照（用于序列化 / 测试）。"""
        return self._ir

    def dump_layout(self) -> str:
        """打印当前 IR 的 band 分布；调试用。"""
        lines: list[str] = [f"-- session {self._ir.session_id} --"]

        def fmt(blocks: tuple[StelaBlock, ...]) -> str:
            return " | ".join(f"{b.band.value}:{b.id}" for b in blocks)

        lines.append(f"tools  : {fmt(self._ir.tools)}")
        lines.append(f"system : {fmt(self._ir.system)}")
        for i, m in enumerate(self._ir.messages):
            lines.append(f"msg[{i}] {m.role:9s}: {fmt(m.blocks)}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部：把 ref-pool 同步进 system 段（pin* → fold(ref-pool)*）
    # ------------------------------------------------------------------

    def _sync_refpool_into_system(self) -> None:
        # 取 system 中所有非 ref-pool 来源的 block（保留 harness 注入的 system pin / drop）
        non_pool = tuple(b for b in self._ir.system if b.ref_slug is None)
        # 重新组合：保留原有 pin → 加 ref-pool fold → 保留原有 drop
        pins = tuple(b for b in non_pool if b.band is Band.PIN)
        drops = tuple(b for b in non_pool if b.band is Band.DROP)
        # ref-pool 字典序渲染（保证多次 emit 字节稳定）
        pool_blocks = self._refpool.render_blocks()
        new_system = pins + pool_blocks + drops
        assert_band_order(new_system, "system (after refpool sync)")
        self._ir = replace(
            self._ir,
            system=new_system,
            ref_pool=self._refpool.to_mapping(),
        )
