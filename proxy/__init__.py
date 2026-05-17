"""TELOS Anthropic reverse-proxy。

零侵入式接入路径：agent 设 ``ANTHROPIC_BASE_URL=http://localhost:7171``
即可让所有 ``messages.create()`` 走 TELOS 管线。详见 ``telos.proxy.server``。

``server`` 子模块依赖 aiohttp；``pipeline`` / ``inspector`` 是纯 Python。
为了让单元测试在无 aiohttp 环境也能跑，``server`` 现在是 *lazy import*：
顶层 namespace 仍然暴露 ``make_app`` / ``ProxyApp``，但访问失败不会立即
拉爆 ``telos.proxy`` 的 import。
"""

from telos.proxy.pipeline import process_anthropic_request

__all__ = ["ProxyApp", "make_app", "process_anthropic_request"]


def __getattr__(name: str):
    """PEP 562 lazy-import：访问 ``telos.proxy.make_app`` 时才 import server。"""
    if name in ("ProxyApp", "make_app"):
        from telos.proxy import server as _s
        return getattr(_s, name)
    raise AttributeError(name)
