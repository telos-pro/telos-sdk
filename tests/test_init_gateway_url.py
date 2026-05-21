"""``telos init`` URL resolution: prefer the running daemon over config defaults.

Phase 2.5 added a protection so installers patch user configs with the URL
the daemon is **actually listening on**, not just whatever ``~/.telos/config.json``
records as a default. This keeps the patched ``baseUrl`` aligned with reality
when someone manually started the gateway on a different port.
"""

from __future__ import annotations

from unittest.mock import patch

from telos.config import GatewayConfig, TelosConfig
from telos.init.__main__ import _resolve_gateway_url


class _FakeState:
    def __init__(self, url: str) -> None:
        self._url = url

    def base_url(self) -> str:
        return self._url


def _cfg(host: str = "127.0.0.1", port: int = 7171) -> TelosConfig:
    return TelosConfig(gateway=GatewayConfig(host=host, port=port))


def test_explicit_flag_wins() -> None:
    cfg = _cfg(port=7171)
    with patch("telos.gateway.daemon.read_state",
               return_value=_FakeState("http://127.0.0.1:7392")):
        url, src = _resolve_gateway_url("http://override:9000", cfg)
    assert url == "http://override:9000"
    assert src == "--gateway-url"
    print("✓ test_explicit_flag_wins")


def test_running_daemon_wins_over_config() -> None:
    """The user's daemon is on 7392 but cfg says 7171; installer should
    use 7392 so the patched baseUrl actually reaches the listener."""
    cfg = _cfg(port=7171)
    with patch("telos.gateway.daemon.read_state",
               return_value=_FakeState("http://127.0.0.1:7392")):
        url, src = _resolve_gateway_url(None, cfg)
    assert url == "http://127.0.0.1:7392"
    assert src == "running daemon"
    print("✓ test_running_daemon_wins_over_config")


def test_config_default_when_no_daemon() -> None:
    cfg = _cfg(port=7171)
    with patch("telos.gateway.daemon.read_state", return_value=None):
        url, src = _resolve_gateway_url(None, cfg)
    assert url == "http://127.0.0.1:7171"
    assert src == "config default"
    print("✓ test_config_default_when_no_daemon")


def test_daemon_read_failure_falls_back_to_config() -> None:
    """If daemon.read_state() raises (e.g. import error in a stripped env),
    URL resolution should not crash — fall back to config."""
    cfg = _cfg(port=7171)
    with patch("telos.gateway.daemon.read_state",
               side_effect=RuntimeError("boom")):
        url, src = _resolve_gateway_url(None, cfg)
    assert url == "http://127.0.0.1:7171"
    assert src == "config default"
    print("✓ test_daemon_read_failure_falls_back_to_config")


def main() -> None:
    test_explicit_flag_wins()
    test_running_daemon_wins_over_config()
    test_config_default_when_no_daemon()
    test_daemon_read_failure_falls_back_to_config()
    print("\nall init gateway-URL resolution tests passed.")


if __name__ == "__main__":
    main()
