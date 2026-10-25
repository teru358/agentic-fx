"""main.py と [project.scripts] afx の共通エントリ。"""
from __future__ import annotations

import argparse
import importlib.metadata
import sys
from pathlib import Path

from agentic_fx import service
from agentic_fx.backtest import cli as backtest_cli
from agentic_fx.cli_surface import COMPATIBILITY_RECORDS, version_tuple
from agentic_fx.ops.contracts import Principal

_BACKTEST_COMMANDS = ("history", "backtest", "analyze", "plugin")


class _OpsOptionError(Exception):
    """`afx ops web` の option の誤り。argparse の usage 付きの文面を持つ。"""


class _OpsOptionParser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        raise _OpsOptionError(self.format_usage() + f"{self.prog}: error: {message}\n")


def _web_parser_kw(feature: str) -> dict:
    """web の subparser は option の略記を受けない。"""
    return {"allow_abbrev": False} if feature == "web" else {}


def _add_web_setup_options(parser: argparse.ArgumentParser) -> None:
    """enable / reconfigure が受ける option。重複は数えて表外として扱うため append / count にする。"""
    parser.add_argument("--ask", action="count", default=0,
                        help="既存の値を既定として全項目を質問する")
    parser.add_argument("--non-interactive", action="count", default=0, dest="non_interactive",
                        help="質問せず --config-file と --secret-action だけで進める")
    parser.add_argument("--config-file", action="append", metavar="FILE", dest="config_file",
                        help="目標の web 設定 (port・bind・allow・auth だけの TOML)")
    parser.add_argument("--password-file", action="append", metavar="FILE", dest="password_file",
                        help="password を読む file (mode 0600、--secret-action install のときだけ)")
    parser.add_argument("--secret-action", action="append", metavar="ACTION", dest="secret_action",
                        help="remove | keep | install | absent")


def _web_options(args: argparse.Namespace):
    from agentic_fx.ops_setup.web_setup import WebOptions
    lists = {name: getattr(args, name) or [] for name in ("config_file", "password_file", "secret_action")}
    return WebOptions(
        ask=args.ask == 1, non_interactive=args.non_interactive == 1,
        config_file=lists["config_file"][0] if lists["config_file"] else None,
        password_file=lists["password_file"][0] if lists["password_file"] else None,
        secret_action=lists["secret_action"][0] if lists["secret_action"] else None,
        duplicated=args.ask > 1 or args.non_interactive > 1 or any(len(v) > 1 for v in lists.values()))


def _compatibility_active(record) -> bool:
    """期限前なら真。version を引けない環境 (未 install) は期限判定不能として有効のまま。"""
    try:
        current = importlib.metadata.version("agentic-fx")
    except importlib.metadata.PackageNotFoundError:
        return True
    return version_tuple(current) < version_tuple(record.remove_in)


def _shim_argv(args_list: list[str], head: int) -> tuple[list[str], int | None]:
    """Normalize local compatibility spellings before argparse sees them."""
    if args_list[head:head + 1] == ["ctl"]:
        return args_list, None
    improve, ops = COMPATIBILITY_RECORDS
    if args_list[head:head + 1] == ["improve"] and _compatibility_active(improve):
        print(improve.message, file=sys.stderr)
        return args_list, 2
    if args_list[head:head + 1] != ["ops"]:
        return args_list, None
    verb = args_list[head + 1] if len(args_list) > head + 1 else None
    if verb in {"enable", "disable", "status"} and _compatibility_active(ops):
        feature = args_list[head + 2] if len(args_list) > head + 2 else None
        display_feature = feature if feature in {"bot", "web", "notify"} else "<bot|web|notify>"
        message = ops.message.replace("<verb>", verb).replace("<feature>", display_feature)
        if feature not in {"bot", "web", "notify"}:
            print(message, file=sys.stderr)
            return args_list, 2
        normalized = args_list[:head + 1] + [feature, verb] + args_list[head + 3:]
        print(message, file=sys.stderr)
        return normalized, None
    if args_list[head + 1:head + 2] == ["web"] and args_list[head + 2:head + 3] == ["reconf"]:
        normalized = list(args_list)
        normalized[head + 2] = "reconfigure"
        return normalized, None
    return args_list, None


def main(argv: list[str] | None = None) -> int:
    args_list = sys.argv[1:] if argv is None else list(argv)
    # 全体 flag の後ろの ctl も argparse に通さない (REMAINDER は `--as` 始まりを
    # 受け取れない)。全体 flag は client には関係ないので読み捨て、daemon も起動しない。
    head = 0
    while args_list[head:head + 1] == ["--daemon"]:
        head += 1
    args_list, shim_rc = _shim_argv(args_list, head)
    if shim_rc is not None:
        return shim_rc
    if args_list[head:head + 1] == ["ctl"]:
        # client は .env も設定の dotenv も読まない。--as を含む残りの引数は client が読む。
        from agentic_fx.ops.client import main as ctl_main
        return ctl_main(args_list[head + 1:], root=Path.cwd())
    # 略記 (--dae など) を許すと、上の完全一致の前置きをすり抜けて ctl が argparse に届く。
    parser = _OpsOptionParser(prog="afx", description="agentic-fx", allow_abbrev=False)
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
    check_sub = check_parser.add_subparsers(dest="check_action")
    backend_parser = check_sub.add_parser("backend", help="backend への接続を検査する")
    backend_parser.add_argument("--backend", choices=("local", "claude", "codex", "opencode"),
                                required=True)
    backend_parser.add_argument("--provider", choices=("chatgpt",), default=None)
    keys_parser = sub.add_parser("keys", help="操作 API の鍵管理 (サービス停止中だけ)")
    keys_sub = keys_parser.add_subparsers(dest="keys_action", required=True)
    keys_sub.add_parser("init", help="初回の鍵を作る (作成済みなら何もしない)")
    for action, text in (("enroll", "未登録の principal の鍵を作って登録する"),
                         ("rotate", "principal の鍵を rotate する"),
                         ("revoke", "principal の鍵を revoke する")):
        p = keys_sub.add_parser(action, help=text)
        p.add_argument("principal", choices=tuple(member.value for member in Principal))
    ops_parser = sub.add_parser("ops", help="外部 operations 機能の有効化・無効化・状態表示")
    ops_sub = ops_parser.add_subparsers(dest="feature", required=True)
    for feature in ("bot", "web", "notify"):
        feature_parser = ops_sub.add_parser(feature, help=f"{feature} の操作")
        feature_sub = feature_parser.add_subparsers(
            dest="ops_action", required=True,
            **({"parser_class": _OpsOptionParser} if feature == "web" else {}))
        for action, text in (("enable", "機能を停止中に有効化する"),
                             ("disable", "機能を停止せずに無効化する"),
                             ("status", "機能の durable stage を表示する")):
            action_parser = feature_sub.add_parser(action, help=text, **_web_parser_kw(feature))
            if feature == "web" and action == "enable":
                _add_web_setup_options(action_parser)
        if feature == "web":
            reconfigure_parser = feature_sub.add_parser(
                "reconfigure", help="有効化済みの web の設定を変更する (ops-web だけを短時間止める)",
                allow_abbrev=False)
            _add_web_setup_options(reconfigure_parser)
    # ctl は上の分岐で client へ渡す。ここは help の一覧に載せるためだけに置く。
    sub.add_parser("ctl", add_help=False,
                   help="稼働中の daemon を操作 API で操作する (afx ctl help)")
    sub.add_parser("status", help="オフライン状態を書き込まずに表示する")
    backtest_cli.register_subparsers(sub)
    try:
        args = parser.parse_args(args_list)
    except _OpsOptionError as error:
        if args_list[head:head + 2] == ["ops", "web"] and args_list[head + 2:head + 3] in (
                ["enable"], ["reconfigure"]):
            print("afx ops: setup_input_required", file=sys.stderr)
            return 2
        sys.stderr.write(str(error))
        raise SystemExit(2) from None

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
        if args.check_action == "backend":
            if args.as_json or args.unit_env:
                print("`--json` / `--unit-env` は `afx check backend` と併用できません。", file=sys.stderr)
                return 2
            return backtest_cli.verify_backend_command(root, args)
        from agentic_fx.setup import preflight
        return preflight.main(root, as_json=args.as_json, unit_env=args.unit_env)
    if args.command == "keys":
        # 鍵管理は .env を読まない (鍵を env に載せない・設定にも依存しない)。
        from agentic_fx.ops.keys import run_keys_command
        return run_keys_command(root, args.keys_action, getattr(args, "principal", None))
    if args.command == "ops":
        if args.ops_action == "reconfigure":
            from agentic_fx.ops_setup.reconfigure import run_ops_reconfigure
            return run_ops_reconfigure(root, args.feature, options=_web_options(args))
        if args.ops_action == "enable":
            from agentic_fx.ops_setup.interactive import ConsoleIO, interactive_enable
            options = _web_options(args) if args.feature == "web" else None
            return interactive_enable(root, args.feature, io=ConsoleIO(), options=options).rc
        if args.ops_action == "disable":
            from agentic_fx.ops_setup.interactive import run_ops_disable
            return run_ops_disable(root, args.feature)
        from agentic_fx.ops_setup.stages import run_ops_command
        return run_ops_command(root, args.ops_action, args.feature)
    if args.command == "status":
        from agentic_fx.status import main as status_main
        return status_main(root)
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
