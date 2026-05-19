"""``telos.gateway`` —— gateway daemon management + control plane.

The ``proxy/`` package handles the actual reverse-proxy logic (``ProxyApp`` /
``run()``); this package is its user-friendly wrapper:

- ``daemon``  —— start/stop the gateway in the background, write PID / state
  files, idempotently.
- ``control`` —— hot-update a running gateway over localhost HTTP (e.g. switch
  the mode).
- ``__main__``—— the ``telos gateway start|stop|status|restart`` subcommands.
"""

from telos.gateway.daemon import (
    GatewayState,
    is_running,
    read_state,
    restart,
    start_detached,
    status_text,
    stop,
)

__all__ = [
    "GatewayState",
    "is_running",
    "read_state",
    "restart",
    "start_detached",
    "status_text",
    "stop",
]
