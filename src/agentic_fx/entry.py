"""main.py と [project.scripts] afx の共通エントリ。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentic_fx import service
from agentic_fx.backtest import cli as backtest_cli
from agentic_fx.ops.contracts import Principal

_BACKTEST_COMMANDS = ("history", "backtest", "analyze", "plugin", "improve")


def main(argv: list[str] | None = None) -> int:
    args_list = sys.argv[1:] if argv is None else list(argv)
    # 全体 flag の後ろの ctl も argparse に通さない (REMAINDER は `--as` 始まりを
    # 受け取れない)。全体 flag は client には関係ないので読み捨て、daemon も起動しない。
    head = 0
    while args_list[head:head + 1] == ["--daemon"]:
        head += 1
    if args_list[head:head + 1] == ["ctl"]:
        # client は .env も設定の dotenv も読まない。--as を含む残りの引数は client が読む。
        from agentic_fx.ops.client import main as ctl_main
        return ctl_main(args_list[head + 1:], root=Path.cwd())
    # 略記 (--dae など) を許すと、上の完全一致の前置きをすり抜けて ctl が argparse に届く。
    parser = argparse.ArgumentParser(prog="afx", description="agentic-fx", allow_abbrev=False)
    parser.add_argument("--daemon", action="store_true",
                        help="systemd 用 (コマンド受付なし)")
    sub = parser.add_subparsers(dest="command")
    init_parser = sub.add_parser("init", help="初期設定 (対話。`--non-interactive` で従来の非対話)")
    init_parser.add_argument("--non-interactive", action="store_true",
                             help="従来の非対話初期化を実行する")
    init_parser.add_argument("--config", type=Path, metavar="FILE",
                             help="settings.yaml として配置する検証済み設定ファイル")
    check_parser = sub.add_parser("check", help="設定と実行環境の起動前検査 (読み取りのみ。fatal があると非 0)")
    check_parser.add_argument("--json", action="store_true", dest="as_json",
                              help="結果を JSON ({\"fatal\": [...], \"warning\": [...]}) で出す")
    check_parser.add_argument("--unit-env", action="store_true", dest="unit_env",
                              help="unit の最小環境で呼ばれたときの検査 (秘密らしい env を fatal に数える)")
    keys_parser = sub.add_parser("keys", help="操作 API の鍵管理 (サービス停止中だけ)")
    keys_sub = keys_parser.add_subparsers(dest="keys_action", required=True)
    keys_sub.add_parser("init", help="初回の鍵を作る (作成済みなら何もしない)")
    for action, text in (("enroll", "未登録の principal の鍵を作って登録する"),
                         ("rotate", "principal の鍵を rotate する"),
                         ("revoke", "principal の鍵を revoke する")):
        p = keys_sub.add_parser(action, help=text)
        p.add_argument("principal", choices=tuple(member.value for member in Principal))
    ops_parser = sub.add_parser("ops", help="外部 operations 機能の有効化・無効化・状態表示")
    ops_sub = ops_parser.add_subparsers(dest="ops_action", required=True)
    for action, text in (("enable", "機能を停止中に有効化する"),
                         ("disable", "機能を停止せずに無効化する"),
                         ("status", "機能の durable stage を表示する")):
        p = ops_sub.add_parser(action, help=text)
        p.add_argument("feature", choices=("bot", "web", "notify"))
    # ctl は上の分岐で client へ渡す。ここは help の一覧に載せるためだけに置く。
    sub.add_parser("ctl", add_help=False,
                   help="稼働中の daemon を操作 API で操作する (afx ctl help)")
    backtest_cli.register_subparsers(sub)
    args = parser.parse_args(args_list)

    root = Path.cwd()
    if args.command == "init":
        if not args.non_interactive:
            from agentic_fx.setup import wizard
            if args.config is not None:
                print("`--config` は `--non-interactive` と一緒に指定してください", file=sys.stderr)
                return 2
            if not sys.stdin.isatty():
                print(wizard.NON_TTY_MESSAGE, file=sys.stderr)
                return 2
            return wizard.run_wizard(root, wizard.ConsoleWizardIO())
        return service.run_init(root, config_file=args.config)
    if args.command == "check":
        from agentic_fx.setup import preflight
        return preflight.main(root, as_json=args.as_json, unit_env=args.unit_env)
    if args.command == "keys":
        # 鍵管理は .env を読まない (鍵を env に載せない・設定にも依存しない)。
        from agentic_fx.ops.keys import run_keys_command
        return run_keys_command(root, args.keys_action, getattr(args, "principal", None))
    if args.command == "ops":
        if args.ops_action == "enable":
            from agentic_fx.ops_setup.interactive import ConsoleIO, interactive_enable
            return interactive_enable(root, args.feature, io=ConsoleIO()).rc
        from agentic_fx.ops_setup.stages import run_ops_command
        return run_ops_command(root, args.ops_action, args.feature)
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
