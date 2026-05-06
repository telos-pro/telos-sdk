"""Plugin / adapter 注册表（按名加载，避免顶层模块强依赖）。

harness 和 engine 都通过这里实例化，保证 bridge 不直接 import 任何
具体实现 —— 这就是"三层只往下传值，不反向引用"的代码层落地。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stela.harness.base import HarnessPlugin
    from stela.engine.base import EngineAdapter


def load_harness(name: str) -> "HarnessPlugin":
    """按名加载 harness plugin。当前支持：``openclaw``, ``hermes``, ``telos``。"""
    if name == "openclaw":
        from stela.harness.openclaw import OpenClawPlugin
        return OpenClawPlugin()
    if name == "hermes":
        from stela.harness.hermes import HermesPlugin
        return HermesPlugin()
    if name == "telos":
        from stela.harness.telos import TelosPlugin
        return TelosPlugin()
    raise ValueError(f"Unknown harness plugin: {name!r}")


def load_engine(name: str) -> "EngineAdapter":
    """按名加载 engine adapter。

    支持：
    - 闭源 API：``anthropic``, ``openai``, ``deepseek``
    - 开源推理（双向感知）：``vllm``, ``sglang``
    """
    if name == "anthropic":
        from stela.engine.anthropic import AnthropicAdapter
        return AnthropicAdapter()
    if name == "openai":
        from stela.engine.openai import OpenAIAdapter
        return OpenAIAdapter()
    if name == "deepseek":
        from stela.engine.deepseek import DeepSeekAdapter
        return DeepSeekAdapter()
    if name == "vllm":
        from stela.engine.vllm import VLLMAdapter
        return VLLMAdapter()
    if name == "sglang":
        from stela.engine.sglang import SGLangAdapter
        return SGLangAdapter()
    raise ValueError(f"Unknown engine adapter: {name!r}")
