"""``python -m stela.proxy`` 入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from stela.corpus import DEFAULT_CORPUS_DIR
from stela.output_filter import MODE_LABELS, StelaMode
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
    parser.add_argument("--mode", default="stela", choices=list(MODE_LABELS),
                        help="默认优化开关：none=纯透传 / stela=只前缀缓存 / "
                             "rtk=只过滤工具输出 / both=两者都开（默认 stela）。"
                             "单条请求可用 X-Stela-Mode header 覆盖。")
    parser.add_argument("--strict", action="store_true",
                        help="STELA 失败时返回 500（默认降级到 passthrough）")
    parser.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS_DIR,
                        help=f"会话语料目录，录原始请求供 stela replay 重放"
                             f"（默认 {DEFAULT_CORPUS_DIR}）")
    parser.add_argument("--no-record", action="store_true",
                        help="关闭会话录制（默认开启）。录的是原始请求 body，"
                             "含你的 prompt / 代码 —— 介意落盘可用此开关。")
    parser.add_argument("--dashboard-refresh", type=int, default=5,
                        metavar="SECONDS",
                        help="GET /__stela/dashboard 的 meta-refresh 间隔，"
                             "0 = 关闭 auto-refresh（默认 5 秒）")
    args = parser.parse_args(argv)

    run(
        host=args.host,
        port=args.port,
        upstream=args.upstream,
        usage_log=args.usage_log,
        harness_override=args.harness,
        strict=args.strict,
        dashboard_refresh=args.dashboard_refresh,
        mode=StelaMode.from_label(args.mode),
        corpus_dir=args.corpus_dir,
        record=not args.no_record,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
