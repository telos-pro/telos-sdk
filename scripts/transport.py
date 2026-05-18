"""统一 SDK Transport 入口 —— 一键接入 openclaw / hermes / claude-code / deepseek-cli。

用法：

    from telos.scripts.transport import TelosTransport

    # 方式 1：直接选 harness（推荐）
    transport = TelosTransport.for_harness("claude-code")

    # Anthropic-shape harness（openclaw / hermes / claude-code）→ 鸭子 Anthropic 接口
    resp = transport.messages.create(model="claude-opus-4-7", system=[...], messages=[...], tools=[...])

    # OpenAI-shape harness（deepseek-cli）→ 鸭子 OpenAI 接口
    resp = transport.chat.completions.create(model="deepseek-chat", messages=[...], tools=[...])

    # 方式 2：自动检测（从请求内容猜 harness）
    transport = TelosTransport.auto(session_id="my-session")

每个 harness preset 预配好了：
- 对应的 harness plugin（请求解析）
- 默认 engine adapter（wire 生成 + cache 策略）
- 默认 base_url 和 API key 环境变量
- wire 协议（Anthropic /v1/messages 或 OpenAI chat/completions）

也可以通过 kwargs 覆盖任何默认值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

from telos.bridge import BridgeSessionState


# ---------------------------------------------------------------------------
# Harness preset 定义
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HarnessPreset:
    """某个 harness 的完整默认配置。"""
    harness_name: str
    engine_name: str
    wire_protocol: Literal["anthropic", "openai"]
    default_base_url: str | None = None
    api_key_env: str = "ANTHROPIC_API_KEY"
    description: str = ""


PRESETS: dict[str, HarnessPreset] = {
    "openclaw": HarnessPreset(
        harness_name="openclaw",
        engine_name="anthropic",
        wire_protocol="anthropic",
        default_base_url=None,
        api_key_env="ANTHROPIC_API_KEY",
        description="OpenClaw agent — Anthropic /v1/messages wire, explicit cache breakpoints",
    ),
    "hermes": HarnessPreset(
        harness_name="hermes",
        engine_name="anthropic",
        wire_protocol="anthropic",
        default_base_url=None,
        api_key_env="ANTHROPIC_API_KEY",
        description="Claude Code (Hermes) — Anthropic /v1/messages wire with envelope tags",
    ),
    "claude-code": HarnessPreset(
        harness_name="hermes",
        engine_name="anthropic",
        wire_protocol="anthropic",
        default_base_url=None,
        api_key_env="ANTHROPIC_API_KEY",
        description="Alias for hermes — Claude Code harness",
    ),
    "deepseek-cli": HarnessPreset(
        harness_name="telos",
        engine_name="deepseek",
        wire_protocol="openai",
        default_base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        description="DeepSeek CLI agent — OpenAI chat/completions wire, prefix cache",
    ),
}


# ---------------------------------------------------------------------------
# 统一 Transport
# ---------------------------------------------------------------------------

class TelosTransport:
    """统一 SDK Transport —— 根据 harness preset 自动选择底层 wire 协议。

    ``for_harness("claude-code")`` 返回的实例带 ``messages.create()`` 接口；
    ``for_harness("deepseek-cli")`` 返回的实例带 ``chat.completions.create()``
    接口。两者也统一暴露 ``create(**kwargs)`` 方法，不关心 wire 协议时直接用这个。
    """

    def __init__(
        self,
        *,
        preset: HarnessPreset,
        api_key: str | None = None,
        base_url: str | None = None,
        session_id: str = "telos-session",
        engine_name: str | None = None,
        usage_log: str | None = None,
        prompt_trace_log: str | None = None,
        session_state: BridgeSessionState | None = None,
    ):
        self._preset = preset
        self._wire = preset.wire_protocol
        effective_engine = engine_name or preset.engine_name
        effective_base_url = base_url or preset.default_base_url
        effective_api_key = api_key or os.environ.get(preset.api_key_env, "")

        common = dict(
            session_id=session_id,
            usage_log=usage_log,
            prompt_trace_log=prompt_trace_log,
            session_state=session_state,
        )

        if self._wire == "anthropic":
            from telos.scripts.telos_anthropic_transport import TelosAnthropicTransport
            kwargs: dict[str, Any] = {
                "api_key": effective_api_key,
                "harness_name": preset.harness_name,
                "engine_name": effective_engine,
                **common,
            }
            if effective_base_url is not None:
                kwargs["base_url"] = effective_base_url
            self._inner = TelosAnthropicTransport(**kwargs)
            self.messages = self._inner.messages
        else:
            from telos.scripts.telos_transport import TelosOpenAITransport
            self._inner = TelosOpenAITransport(
                base_url=effective_base_url or "https://api.deepseek.com",
                api_key=effective_api_key,
                engine_name=effective_engine,
                harness_name=preset.harness_name,
                **common,
            )
            self.chat = self._inner.chat

    @property
    def session_state(self) -> BridgeSessionState:
        return self._inner.session_state

    @property
    def preset(self) -> HarnessPreset:
        return self._preset

    def create(self, **kwargs) -> Any:
        """统一调用入口 —— 自动路由到对应 wire 协议的 create 方法。"""
        if self._wire == "anthropic":
            return self.messages.create(**kwargs)
        return self.chat.completions.create(**kwargs)

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def for_harness(
        cls,
        name: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        session_id: str = "telos-session",
        engine_name: str | None = None,
        usage_log: str | None = None,
        prompt_trace_log: str | None = None,
        session_state: BridgeSessionState | None = None,
    ) -> "TelosTransport":
        """按 harness 名一键创建 transport。

        支持的 harness 名：
        - ``"openclaw"``    — OpenClaw agent（Anthropic wire）
        - ``"hermes"``      — Claude Code / Hermes（Anthropic wire）
        - ``"claude-code"`` — 同 hermes
        - ``"deepseek-cli"``— DeepSeek CLI agent（OpenAI wire）

        所有配置项都有合理默认值；传 kwargs 覆盖。

        Example::

            transport = TelosTransport.for_harness("claude-code")
            resp = transport.messages.create(
                model="claude-opus-4-7",
                system=[{"type": "text", "text": "You are helpful."}],
                messages=[{"role": "user", "content": "Hello"}],
                max_tokens=1024,
            )
        """
        preset = PRESETS.get(name)
        if preset is None:
            available = ", ".join(sorted(PRESETS))
            raise ValueError(
                f"Unknown harness {name!r}. Available: {available}"
            )
        return cls(
            preset=preset,
            api_key=api_key,
            base_url=base_url,
            session_id=session_id,
            engine_name=engine_name,
            usage_log=usage_log,
            prompt_trace_log=prompt_trace_log,
            session_state=session_state,
        )

    @classmethod
    def auto(
        cls,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        session_id: str = "telos-session",
        usage_log: str | None = None,
        prompt_trace_log: str | None = None,
        session_state: BridgeSessionState | None = None,
    ) -> "TelosTransport":
        """自动检测模式 —— harness_name=None，首次请求时从内容推断。

        默认走 Anthropic wire（openclaw / hermes 自动切换）。如果需要
        OpenAI wire 请显式用 ``for_harness("deepseek-cli")``。
        """
        from telos.scripts.telos_anthropic_transport import TelosAnthropicTransport
        preset = HarnessPreset(
            harness_name="",
            engine_name="anthropic",
            wire_protocol="anthropic",
            api_key_env="ANTHROPIC_API_KEY",
            description="Auto-detect harness from request content",
        )
        transport = cls.__new__(cls)
        transport._preset = preset
        transport._wire = "anthropic"

        kwargs: dict[str, Any] = {
            "session_id": session_id,
            "harness_name": None,
            "engine_name": "anthropic",
            "usage_log": usage_log,
            "prompt_trace_log": prompt_trace_log,
            "session_state": session_state,
        }
        if api_key is not None:
            kwargs["api_key"] = api_key
        if base_url is not None:
            kwargs["base_url"] = base_url
        transport._inner = TelosAnthropicTransport(**kwargs)
        transport.messages = transport._inner.messages
        return transport

    @staticmethod
    def available_presets() -> dict[str, str]:
        """返回所有可用 preset 名 → 描述的映射。"""
        return {name: p.description for name, p in PRESETS.items()}
