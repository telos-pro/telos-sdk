"""``telos replay`` —— 录制 → 重放对照引擎。

原理
----
从语料库取出某个真实会话录下的「请求序列」，对每一种 mode
（none / telos / rtk / both）把**逐字节相同**的轮次重新跑一遍管线、发到
上游，只取 usage。因为每个 mode 看到的输入完全一致，唯一变量就是优化
开关本身 —— 这是受控实验，比「跑两个独立 session」少了 trajectory 分叉
的混杂。

为压低成本，重放时把 ``max_tokens`` 强制设成 1（并去掉 ``stream`` /
``tool_choice`` / ``thinking``）：我们只关心 prompt / prefill 侧的
``cache_read`` / ``cache_write`` 计费，输出生成被刻意阉割。一次完整真实
会话 + 每 mode 一串廉价 prefill，比「N 个 mode 各跑完整 agent 会话」便宜
一两个数量级。

边界
----
- replay 把 trajectory **钉死**了。它测的是「同一段对话在不同编码下的
  成本」，不是「同一个任务在不同配置下的成本」。捕捉不到二阶效应——比如
  RTK 缩短工具结果后，真实运行里 agent 下一步可能做出不同决策。
- 跨 mode 的缓存隔离：默认给每个 mode 注入一个唯一的 system 前缀块
  （``[telos-replay ns=...]``），让 Anthropic 端的前缀缓存各自独立，
  避免「先重放的 mode 把缓存暖好、后重放的 mode 白蹭命中」。这块前缀
  本身只有几个 token、各 mode 等长，不影响相对对照；``cache_isolation=
  False`` 可关。
- 测的是 prefill / 缓存计费，不是端到端任务成本。要后者得跑独立 session。
"""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from telos.bridge import BridgeSessionState
from telos.output_filter import TelosMode, ToolResultFilter, apply_filter, build_filter
from telos.proxy.pipeline import process_anthropic_request

_log = logging.getLogger("telos.replay")

# 上游 sender：吃一个 wire dict，返回 Anthropic 风格的 raw usage dict
# （或 None 表示该轮调用失败）。注入式设计——测试传假 sender，不打网络。
Sender = Callable[[Mapping[str, Any]], "dict[str, Any] | None"]


# ---------------------------------------------------------------------------
# usage 归一化
# ---------------------------------------------------------------------------

def _normalize(raw: Mapping[str, Any]) -> dict[str, int]:
    return {
        "raw_input": int(raw.get("input_tokens", 0) or 0),
        "cache_read": int(raw.get("cache_read_input_tokens", 0) or 0),
        "cache_write": int(raw.get("cache_creation_input_tokens", 0) or 0),
        "output": int(raw.get("output_tokens", 0) or 0),
    }


def _usage_obj_to_raw(usage: Any) -> dict[str, Any]:
    """把 Anthropic SDK 的 ``Usage`` 对象转成 dashboard 认的 raw_usage dict。"""
    raw: dict[str, Any] = {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens":
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }
    cc = getattr(usage, "cache_creation", None)
    if cc is not None:
        raw["cache_creation"] = {
            "ephemeral_5m_input_tokens":
                getattr(cc, "ephemeral_5m_input_tokens", 0) or 0,
            "ephemeral_1h_input_tokens":
                getattr(cc, "ephemeral_1h_input_tokens", 0) or 0,
        }
    return raw


# ---------------------------------------------------------------------------
# 上游 sender 工厂
# ---------------------------------------------------------------------------

def anthropic_sender(*, api_key: str | None = None,
                     upstream: str | None = None) -> Sender:
    """构造一个走 Anthropic SDK 的真实 sender。"""
    from anthropic import Anthropic

    kwargs: dict[str, Any] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if upstream:
        kwargs["base_url"] = upstream
    client = Anthropic(**kwargs)

    def send(wire: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            resp = client.messages.create(**dict(wire))
        except Exception as e:  # noqa: BLE001
            _log.warning("replay 上游调用失败: %s", e)
            return None
        return _usage_obj_to_raw(resp.usage)

    return send


# ---------------------------------------------------------------------------
# 缓存隔离：给每个 mode 注入唯一 system 前缀
# ---------------------------------------------------------------------------

def _inject_namespace(raw: dict[str, Any], session_id: str, mode_label: str) -> None:
    """就地在 ``system`` 段最前面插一个 mode 专属的命名空间块。

    各 mode 的前缀因此互不相同 → Anthropic 端缓存各自独立，重放顺序不再
    污染对照数字。块本身只有 ~10 token、各 mode 等长。
    """
    tag = {"type": "text", "text": f"[telos-replay ns={session_id}/{mode_label}]"}
    system = raw.get("system")
    if system is None:
        raw["system"] = [tag]
    elif isinstance(system, str):
        raw["system"] = [tag, {"type": "text", "text": system}]
    elif isinstance(system, list):
        raw["system"] = [tag, *system]


# ---------------------------------------------------------------------------
# 单 mode 重放
# ---------------------------------------------------------------------------

@dataclass
class ReplayResult:
    """一个 (session, mode) 重放完的汇总。"""

    mode: str
    session_id: str
    compare_group: str
    records: list[dict[str, Any]] = field(default_factory=list)
    turns_ok: int = 0
    turns_failed: int = 0

    @property
    def total_cache_read(self) -> int:
        return sum(r["normalized"]["cache_read"] for r in self.records)

    @property
    def total_cache_write(self) -> int:
        return sum(r["normalized"]["cache_write"] for r in self.records)

    @property
    def total_raw_input(self) -> int:
        return sum(r["normalized"]["raw_input"] for r in self.records)


def replay_session(
    turns: list[Mapping[str, Any]],
    mode: TelosMode,
    *,
    session_id: str,
    compare_group: str,
    sender: Sender,
    flt: ToolResultFilter | None = None,
    cache_isolation: bool = True,
) -> ReplayResult:
    """把一个会话的轮次序列按 ``mode`` 重放一遍。

    Args:
        turns:           语料里的轮次记录列表（每个含 ``request``）。
        mode:            本次重放用的开关组合。
        session_id:      原会话 id（usage_log 里写 ``<id>/<mode>``）。
        compare_group:   对比分组键（dashboard 按此并排）。
        sender:          wire → raw_usage 的可调用；测试可注入假实现。
        flt:             RTK 过滤器；``None`` 时按需 ``build_filter()``。
        cache_isolation: 是否给每个 mode 注入唯一 system 前缀（见模块 docstring）。
    """
    if flt is None:
        flt = build_filter()
    state = BridgeSessionState()
    result = ReplayResult(mode=mode.label, session_id=session_id,
                          compare_group=compare_group)
    replay_sid = f"{session_id}/{mode.label}"

    for turn in turns:
        request = turn.get("request")
        if not isinstance(request, Mapping):
            continue
        raw = copy.deepcopy(dict(request))
        if cache_isolation:
            _inject_namespace(raw, session_id, mode.label)

        reduction: dict[str, Any] = {}
        effective: Mapping[str, Any] = raw
        if mode.rtk:
            effective, fstats = apply_filter(raw, flt)
            reduction = fstats.as_dict()

        if mode.telos:
            try:
                pr = process_anthropic_request(
                    effective, session_id=replay_sid, session_state=state)
                wire = dict(pr.wire)
                harness = pr.harness
            except Exception:  # noqa: BLE001
                _log.exception("replay 管线失败，退回 passthrough")
                wire = dict(effective)
                harness = "passthrough"
        else:
            wire = dict(effective)
            harness = "rtk-only" if mode.rtk else "passthrough"

        # 阉割输出生成：只测 prompt / prefill 侧成本。
        wire["max_tokens"] = 1
        for k in ("stream", "tool_choice", "thinking"):
            wire.pop(k, None)

        raw_usage = sender(wire)
        if raw_usage is None:
            result.turns_failed += 1
            continue

        result.turns_ok += 1
        result.records.append({
            "ts": time.time(),
            "session_id": replay_sid,
            "call_index": int(turn.get("call_index") or len(result.records) + 1),
            "model": wire.get("model") or request.get("model") or "",
            "harness": harness,
            "mode": mode.label,
            "compare_group": compare_group,
            "replay": True,
            "tool_output_reduction": reduction,
            "raw_usage": raw_usage,
            "normalized": _normalize(raw_usage),
        })

    return result
