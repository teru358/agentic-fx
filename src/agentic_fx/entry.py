"""main.py と [project.scripts] afx の共通エントリ。"""
from __future__ import annotations

import argparse
from pathlib import Path

from agentic_fx import service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="afx", description="agentic-fx")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="初期設定 (非対話・冪等。既存 settings.yaml は上書きしない)")
    args = parser.parse_args(argv)

    root = Path.cwd()
    if args.command == "init":
        return service.run_init(root)
    return service.run_service(root)


if __name__ == "__main__":
    raise SystemExit(main())
