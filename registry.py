"""Plugin / adapter 注册表（按名加载，避免顶层模块强依赖）。

harness 和 engine 都通过这里实例化，保证 bridge 不直接 import 任何
具体实现 —— 这就是"三层只往下传值，不反向引用"的代码层落地。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telos.harness.base import HarnessPlugin
    from telos.engine.base import EngineAdapter


_HARNESS_ALIASES: dict[str, str] = {
    "claude-code": "hermes",
    "deepseek-cli": "telos",
}


def canonical_harness(name: str) -> str:
    """把 harness 别名解析成 canonical 名（``claude-code`` → ``hermes``）。

    非别名原样返回。用于让 usage log / dashboard 不论调用方传别名还是
    canonical 名都显示一致的 harness。
    """
    return _HARNESS_ALIASES.get(name, name)


def load_harness(name: str) -> "HarnessPlugin":
    """按名加载 harness plugin。

    支持：``openclaw``, ``hermes``, ``telos``
    别名：``claude-code`` → hermes, ``deepseek-cli`` → telos
    """
    canonical = canonical_harness(name)
    if canonical == "openclaw":
        from telos.harness.openclaw import OpenClawPlugin
        return OpenClawPlugin()
    if canonical == "hermes":
        from telos.harness.hermes import HermesPlugin
        return HermesPlugin()
    if canonical == "telos":
        from telos.harness.telos import TelosPlugin
        return TelosPlugin()
    raise ValueError(f"Unknown harness plugin: {name!r}")


def load_engine(name: str) -> "EngineAdapter":
    """按名加载 engine adapter。

    支持：
    - 闭源 API：``anthropic``, ``openai``, ``deepseek``
    - 开源推理（双向感知）：``vllm``, ``sglang``
    """
    if name == "anthropic":
        from telos.engine.anthropic import AnthropicAdapter
        return AnthropicAdapter()
    if name == "openai":
        from telos.engine.openai import OpenAIAdapter
        return OpenAIAdapter()
    if name == "deepseek":
        from telos.engine.deepseek import DeepSeekAdapter
        return DeepSeekAdapter()
    if name == "vllm":
        from telos.engine.vllm import VLLMAdapter
        return VLLMAdapter()
    if name == "sglang":
        from telos.engine.sglang import SGLangAdapter
        return SGLangAdapter()
    raise ValueError(f"Unknown engine adapter: {name!r}")
