"""STELA Anthropic reverse-proxy。

零侵入式接入路径：agent 设 ``ANTHROPIC_BASE_URL=http://localhost:7171``
即可让所有 ``messages.create()`` 走 STELA 管线。详见 ``stela.proxy.server``。
"""

from stela.proxy.pipeline import process_anthropic_request
from stela.proxy.server import ProxyApp, make_app

__all__ = ["ProxyApp", "make_app", "process_anthropic_request"]
