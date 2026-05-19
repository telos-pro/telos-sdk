"""TELOS Anthropic reverse-proxy.

Zero-intrusion integration path: an agent sets ``ANTHROPIC_BASE_URL=http://localhost:7171``
to route all ``messages.create()`` calls through the TELOS pipeline. See ``telos.proxy.server``.

The ``server`` submodule depends on aiohttp; ``pipeline`` / ``inspector`` are pure Python.
So that unit tests can run in environments without aiohttp, ``server`` is now a *lazy import*:
the top-level namespace still exposes ``make_app`` / ``ProxyApp``, but a failed access no longer
breaks the import of ``telos.proxy`` outright.
"""

from telos.proxy.pipeline import process_anthropic_request

__all__ = ["ProxyApp", "make_app", "process_anthropic_request"]


def __getattr__(name: str):
    """PEP 562 lazy-import: only import server when ``telos.proxy.make_app`` is accessed."""
    if name in ("ProxyApp", "make_app"):
        from telos.proxy import server as _s
        return getattr(_s, name)
    raise AttributeError(name)
