"""``stela`` 命令行入口（统一 dispatch）。

子命令：
- ``stela proxy``  → ``python -m stela.proxy``
- ``stela init``   → ``python -m stela.init``
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        _print_usage()
        return 0 if argv else 1

    subcommand, rest = argv[0], argv[1:]
    if subcommand == "proxy":
        from stela.proxy.__main__ import main as proxy_main
        return proxy_main(rest)
    if subcommand == "init":
        from stela.init.__main__ import main as init_main
        return init_main(rest)
    print(f"unknown subcommand: {subcommand}", file=sys.stderr)
    _print_usage()
    return 2


def _print_usage() -> None:
    print(
        "usage: stela <subcommand> [...]\n"
        "\n"
        "subcommands:\n"
        "  proxy   启动 STELA Anthropic 反向代理\n"
        "  init    把代理接入到指定 agent 的配置\n"
        "\n"
        "examples:\n"
        "  stela proxy --port 7171 --usage-log /tmp/usage.jsonl\n"
        "  stela init --agent claude-code\n"
        "  stela init --agent claude-code --status\n"
        "  stela init --agent claude-code --uninstall\n"
    )


if __name__ == "__main__":
    sys.exit(main())
