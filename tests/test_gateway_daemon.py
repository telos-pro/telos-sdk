"""``telos.gateway.daemon`` tests: background start/stop / idempotency / stale-PID cleanup."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from pathlib import Path

import telos.config as cfgmod  # noqa: F401 — triggers the TELOS_HOME activation path
from telos.gateway import daemon


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _isolated_home() -> Path:
    home = Path(tempfile.mkdtemp(prefix="telos-daemon-"))
    os.environ["TELOS_HOME"] = str(home)
    return home


def test_start_status_stop() -> None:
    _isolated_home()
    port = _free_port()
    state = daemon.start_detached(port=port)
    try:
        assert state.port == port
        assert daemon.is_running() is True
        assert "running" in daemon.status_text()
        # idempotency: starting again returns the same process.
        again = daemon.start_detached(port=port)
        assert again.pid == state.pid
    finally:
        assert daemon.stop() is True
    assert daemon.is_running() is False
    # stopping again after already stopped returns False.
    assert daemon.stop() is False
    print("✓ test_start_status_stop")


def test_stale_pid_cleaned() -> None:
    home = _isolated_home()
    home.mkdir(parents=True, exist_ok=True)
    state_file = home / "gateway.json"
    state_file.write_text(json.dumps({
        "pid": 999_999, "host": "127.0.0.1", "port": 1234,
        "mode": "telos", "usage_log": "", "started_at": time.time(),
    }))
    # pid does not exist → read_state returns None and cleans up the state file.
    assert daemon.read_state() is None
    assert not state_file.exists()
    print("✓ test_stale_pid_cleaned")


def test_restart() -> None:
    _isolated_home()
    port = _free_port()
    s1 = daemon.start_detached(port=port)
    try:
        s2 = daemon.restart(port=port)
        assert s2.pid != s1.pid
        assert daemon.is_running() is True
    finally:
        daemon.stop()
    print("✓ test_restart")


def main() -> None:
    test_start_status_stop()
    test_stale_pid_cleaned()
    test_restart()
    print("\nall gateway daemon tests passed.")


if __name__ == "__main__":
    main()
