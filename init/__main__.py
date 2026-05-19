"""``python -m telos.init`` / ``telos init`` entry point.

Default (no arguments) flow —— this is the headline feature:

1. Detect which harness CLIs are installed locally (claude-code / codex /
   openclaw / hermes);
2. Inject config pointing at the gateway for each detected harness;
3. Start the gateway in the background;
4. Print the gateway and dashboard addresses.

Single-point operations are also supported: ``--harness <name>``,
``--uninstall``, ``--status``.
"""

from __future__ import annotations

import argparse
import sys

from telos.config import load_config
from telos.harnesses import HARNESS_NAMES, detect_installed
from telos.init import INSTALLERS
from telos.init.base import InstallResult


def _render(result: InstallResult) -> str:
    lines = [f"[{result.agent}] {result.action}"]
    if result.changed_files:
        lines.append("  changed:")
        for p in result.changed_files:
            lines.append(f"    - {p}")
    if result.backups:
        lines.append("  backups:")
        for p in result.backups:
            lines.append(f"    - {p}")
    for note in result.notes:
        lines.append(f"  ▸ {note}")
    return "\n".join(lines)


def _make_installer(name: str, gateway_url: str):
    factory = INSTALLERS[name]
    return factory(proxy_url=gateway_url)


def _run_one(name: str, gateway_url: str, *, uninstall: bool, status: bool) -> int:
    try:
        installer = _make_installer(name, gateway_url)
        if status:
            result = installer.status()
        elif uninstall:
            result = installer.uninstall()
        else:
            result = installer.install()
    except Exception as e:  # noqa: BLE001
        print(f"[{name}] error: {e}", file=sys.stderr)
        return 1
    print(_render(result))
    return 0


def _start_gateway() -> None:
    """Start the gateway in the background after injection completes, and print the address."""
    from telos.gateway import daemon

    try:
        state = daemon.start_detached()
    except RuntimeError as e:
        print(f"warning: gateway failed to start: {e}", file=sys.stderr)
        print("        you can run it manually later: telos gateway start")
        return
    print()
    print(f"✓ gateway running → {state.base_url()}  (mode={state.mode})")
    print(f"  dashboard     → {state.dashboard_url()}")
    print("  open the dashboard: telos dashboard")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="telos init",
        description="Automatically detect harnesses, inject gateway config, start the gateway.",
    )
    # --harness is primary; --agent is a hidden alias for backward compatibility.
    parser.add_argument("--harness", "--agent", dest="harness", default=None,
                        choices=sorted(INSTALLERS.keys()),
                        help="operate only on the specified harness (default: auto-detect all)")
    parser.add_argument("--gateway-url", "--proxy-url", dest="gateway_url",
                        default=None,
                        help="gateway address (default: taken from ~/.telos/config.json)")
    parser.add_argument("--uninstall", action="store_true", help="undo the injection")
    parser.add_argument("--status", action="store_true", help="view only, do not change files")
    parser.add_argument("--no-gateway", action="store_true",
                        help="only inject config, do not auto-start the gateway")
    args = parser.parse_args(argv)

    cfg = load_config()
    gateway_url = args.gateway_url or cfg.gateway.base_url()

    # ---- Determine the target harness list ----
    if args.harness:
        targets = [args.harness]
    elif args.status or args.uninstall:
        # When unspecified, status/uninstall apply to all known harnesses.
        targets = [n for n in HARNESS_NAMES if n in INSTALLERS]
    else:
        detected = detect_installed(cfg.harness_executables)
        targets = [s.name for s in detected if s.name in INSTALLERS]
        if not targets:
            print("No installed harness CLI detected.")
            print(f"telos supports: {', '.join(HARNESS_NAMES)}")
            print("Install one of them and re-run telos init, "
                  "or use telos init --harness <name> to specify one.")
            return 1
        print(f"Detected harnesses: {', '.join(targets)}\n")

    # ---- Execute one by one ----
    rc = 0
    for name in targets:
        rc |= _run_one(name, gateway_url, uninstall=args.uninstall,
                       status=args.status)

    # ---- Start the gateway after a successful install ----
    if not args.uninstall and not args.status and not args.no_gateway and rc == 0:
        _start_gateway()

    return rc


if __name__ == "__main__":
    sys.exit(main())
