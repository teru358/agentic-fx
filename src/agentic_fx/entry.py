"""main.py と [project.scripts] afx の共通エントリ。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentic_fx import service
from agentic_fx.backtest import cli as backtest_cli

_BACKTEST_COMMANDS = ("history", "backtest", "analyze", "plugin", "improve")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="afx", description="agentic-fx")
    parser.add_argument("--daemon", action="store_true",
                        help="systemd 用 (コマンド受付なし)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="初期設定 (非対話・冪等。既存 settings.yaml は上書きしない)")
    keys_parser = sub.add_parser("keys", help="操作 API の鍵管理 (サービス停止中だけ)")
    keys_sub = keys_parser.add_subparsers(dest="keys_action", required=True)
    keys_sub.add_parser("init", help="初回の鍵を作る (作成済みなら何もしない)")
    for action in ("rotate", "revoke"):
        p = keys_sub.add_parser(action, help=f"principal の鍵を {action} する")
        p.add_argument("principal", choices=("operator", "approver"))
    backtest_cli.register_subparsers(sub)
    args = parser.parse_args(argv)

    root = Path.cwd()
    if args.command == "init":
        return service.run_init(root)
    if args.command == "keys":
        # 鍵管理は .env を読まない (鍵を env に載せない・設定にも依存しない)。
        from agentic_fx.ops.keys import run_keys_command
        return run_keys_command(root, args.keys_action, getattr(args, "principal", None))
    if args.command in _BACKTEST_COMMANDS:
        return backtest_cli.dispatch(args, root)
    daemon = args.daemon
    if not daemon and not sys.stdin.isatty():
        daemon = True
        print("afx: stdin が端末ではないため daemon モードで起動します "
              "(対話コマンドは受け付けません。明示するには --daemon)",
              file=sys.stderr)
    return service.run_service(root, daemon=daemon)


if __name__ == "__main__":
    raise SystemExit(main())
