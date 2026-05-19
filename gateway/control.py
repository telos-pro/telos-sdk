"""gateway control plane: hot-update a running gateway over localhost HTTP.

Uses only the standard-library ``urllib`` —— no extra dependencies. The control
endpoint listens on loopback only, and the gateway side also accepts only
loopback origins (see ``proxy/server.py``).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

_CONTROL_PATH = "/__telos/control/mode"
_TIMEOUT_S = 3.0


def _control_url(host: str, port: int) -> str:
    return f"http://{host}:{port}{_CONTROL_PATH}"


def dashboard_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/__telos/dashboard"


def get_mode(host: str, port: int) -> str:
    """Read the current default mode of a running gateway."""
    req = urllib.request.Request(_control_url(host, port), method="GET")
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
        data = json.loads(resp.read().decode("utf-8"))
    return str(data.get("mode", ""))


def post_mode(host: str, port: int, label: str) -> str:
    """Hot-switch the default mode of a running gateway; return the mode the
    gateway confirmed.

    Raises ``RuntimeError`` on failure (gateway not running / rejected / invalid
    label).
    """
    body = json.dumps({"mode": label}).encode("utf-8")
    req = urllib.request.Request(
        _control_url(host, port), data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"gateway rejected the mode switch (HTTP {e.code}): {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot connect to gateway: {e.reason}") from e
    return str(data.get("mode", label))
