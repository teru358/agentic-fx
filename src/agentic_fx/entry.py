"""main.py と [project.scripts] afx の共通エントリ。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentic_fx import service
from agentic_fx.backtest import cli as backtest_cli

_BACKTEST_COMMANDS = ("history", "backtest", "analyze")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="afx", description="agentic-fx")
    parser.add_argument("--daemon", action="store_true",
                        help="systemd 用 (コマンド受付なし)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="初期設定 (非対話・冪等。既存 settings.yaml は上書きしない)")
    backtest_cli.register_subparsers(sub)
    args = parser.parse_args(argv)

    root = Path.cwd()
    if args.command == "init":
        return service.run_init(root)
    if args.command in _BACKTEST_COMMANDS:
        return backtest_cli.dispatch(args, root)
    daemon = args.daemon or not sys.stdin.isatty()
    return service.run_service(root, daemon=daemon)


if __name__ == "__main__":
    raise SystemExit(main())
