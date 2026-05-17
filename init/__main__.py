"""``python -m telos.init`` 入口。"""

from __future__ import annotations

import argparse
import sys

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="telos.init",
        description="把 TELOS 代理接入到指定 agent 的配置。",
    )
    parser.add_argument("--agent", required=True, choices=sorted(INSTALLERS.keys()),
                        help="目标 agent")
    parser.add_argument("--proxy-url", default="http://127.0.0.1:7171",
                        help="代理地址（默认 http://127.0.0.1:7171）")
    parser.add_argument("--uninstall", action="store_true",
                        help="还原（撤销 install 的修改）")
    parser.add_argument("--status", action="store_true",
                        help="只查看接入状态，不改文件")
    args = parser.parse_args(argv)

    installer_cls = INSTALLERS[args.agent]
    installer = installer_cls(proxy_url=args.proxy_url)

    try:
        if args.status:
            result = installer.status()
        elif args.uninstall:
            result = installer.uninstall()
        else:
            result = installer.install()
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(_render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
