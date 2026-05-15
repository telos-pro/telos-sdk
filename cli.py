"""``stela`` 命令行入口（统一 dispatch）。

子命令：
- ``stela proxy``      → ``python -m stela.proxy``
- ``stela init``       → ``python -m stela.init``
- ``stela dashboard``  → ``python -m stela.scripts.build_savings_dashboard``
- ``stela replay``     → ``python -m stela.replay``
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
    if subcommand == "dashboard":
        from stela.scripts.build_savings_dashboard import main as dash_main
        return dash_main(rest)
    if subcommand == "replay":
        from stela.replay.__main__ import main as replay_main
        return replay_main(rest)
    print(f"unknown subcommand: {subcommand}", file=sys.stderr)
    _print_usage()
    return 2


def _print_usage() -> None:
    print(
        "usage: stela <subcommand> [...]\n"
        "\n"
        "subcommands:\n"
        "  proxy       启动 STELA Anthropic 反向代理\n"
        "  init        把代理接入到指定 agent 的配置\n"
        "  dashboard   把 usage_log 聚合成 saved-token / saved-$ HTML 看板\n"
        "  replay      把录下的会话按多种 mode 重放，做受控 A/B 对比\n"
        "\n"
        "examples:\n"
        "  stela proxy --port 7171 --usage-log /tmp/usage.jsonl\n"
        "  stela init --agent claude-code\n"
        "  stela init --agent claude-code --status\n"
        "  stela init --agent claude-code --uninstall\n"
        "  stela dashboard --usage-log ~/.stela/usage.jsonl --out savings.html\n"
        "  stela replay --list\n"
        "  stela replay --session <id> --modes none,stela,rtk,both\n"
    )


if __name__ == "__main__":
    sys.exit(main())
