"""``python -m stela.proxy`` 入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from stela.proxy.server import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stela.proxy",
        description="STELA Anthropic 反向代理。"
                    " agent 设 ANTHROPIC_BASE_URL=http://<host>:<port> 即可零侵入接入。",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=7171,
                        help="监听端口（默认 7171）")
    parser.add_argument("--upstream", default="https://api.anthropic.com",
                        help="真实 Anthropic API endpoint")
    parser.add_argument("--usage-log", type=Path, default=None,
                        help="每次调用写一行 jsonl 到此路径")
    parser.add_argument("--harness", default=None,
                        choices=["openclaw", "hermes"],
                        help="强制使用某个 harness（默认自动检测）")
    args = parser.parse_args(argv)

    run(
        host=args.host,
        port=args.port,
        upstream=args.upstream,
        usage_log=args.usage_log,
        harness_override=args.harness,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
