"""``telos gateway`` —— gateway start/stop management.

Subverbs:

- ``start``    start the gateway in the background (``--foreground`` runs it
  blocking in the foreground instead)
- ``stop``     stop the background gateway
- ``status``   view the running status
- ``restart``  restart

Without a subverb, this is equivalent to ``start``. ``host`` / ``port`` /
``mode`` / ``usage-log`` default to values from ``~/.telos/config.json``; values
passed explicitly on the CLI are written back as the new defaults.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from telos.config import load_config, save_config
from telos.gateway import daemon
from telos.output_filter import MODE_LABELS


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="telos gateway",
        description="Start / stop / view the local TELOS gateway.",
    )
    p.add_argument("verb", nargs="?", default="start",
                   choices=["start", "stop", "status", "restart"],
                   help="operation (default: start)")
    p.add_argument("--host", default=None, help="listen address")
    p.add_argument("--port", type=int, default=None, help="listen port")
    p.add_argument("--mode", default=None, choices=list(MODE_LABELS),
                   help="default optimization mode")
    p.add_argument("--usage-log", type=Path, default=None, help="usage jsonl path")
    p.add_argument("--foreground", action="store_true",
                   help="run blocking in the foreground (no daemonizing), for debugging / containers")
    return p


def _persist_overrides(args: argparse.Namespace) -> None:
    """Write the explicitly passed CLI host/port/mode/usage-log back into the config."""
    cfg = load_config()
    changed = False
    if args.host is not None:
        cfg.gateway.host = args.host
        changed = True
    if args.port is not None:
        cfg.gateway.port = args.port
        changed = True
    if args.mode is not None:
        cfg.mode = args.mode
        changed = True
    if args.usage_log is not None:
        cfg.gateway.usage_log = str(args.usage_log)
        changed = True
    if changed:
        save_config(cfg)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _persist_overrides(args)
    cfg = load_config()

    if args.verb == "stop":
        if daemon.stop():
            print("gateway stopped.")
        else:
            print("gateway is not running.")
        return 0

    if args.verb == "status":
        print(daemon.status_text())
        return 0

    host = args.host or cfg.gateway.host
    port = args.port or cfg.gateway.port
    mode = args.mode or cfg.mode
    usage_log = args.usage_log or cfg.gateway.resolved_usage_log()

    # Foreground run: directly call the blocking run().
    if args.foreground and args.verb in ("start", "restart"):
        from telos.output_filter import TelosMode
        from telos.proxy.server import run
        if args.verb == "restart":
            daemon.stop()
        run(host=host, port=int(port), usage_log=Path(usage_log),
            mode=TelosMode.from_label(mode))
        return 0

    try:
        if args.verb == "restart":
            state = daemon.restart(host=host, port=int(port), mode=mode,
                                   usage_log=Path(usage_log), config=cfg)
        else:  # start
            state = daemon.start_detached(host=host, port=int(port), mode=mode,
                                          usage_log=Path(usage_log), config=cfg)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"gateway running → {state.base_url()}  (mode={state.mode})")
    print(f"dashboard      → {state.dashboard_url()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
