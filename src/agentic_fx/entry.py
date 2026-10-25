"""main.py と [project.scripts] afx の共通エントリ。"""
from __future__ import annotations

import argparse
import importlib.metadata
import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

from agentic_fx import service
from agentic_fx.backtest import cli as backtest_cli
from agentic_fx.cli_surface import (COMMANDS, COMPATIBILITY_RECORDS, HelpFormatter, leaf_help,
                                    parser_description, parser_help, render_epilog,
                                    render_top_epilog, top_description, version_tuple)
from agentic_fx.ops.contracts import Principal

_BACKTEST_COMMANDS = ("history", "backtest", "analyze", "plugin", "news")


class _OpsOptionError(Exception):
    """`afx ops web` の option の誤り。argparse の usage 付きの文面を持つ。"""


class _OpsOptionParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs) -> None:
        if sys.version_info >= (3, 14):
            kwargs.setdefault("color", False)
        super().__init__(*args, **kwargs)

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


def _reconfigurable_features() -> frozenset[str]:
    """`afx ops <feature> reconfigure` を持つ feature。parser と旧綴りの変換が同じ表を見る。"""
    return frozenset(
        record.command.split()[1] for record in COMMANDS
        if record.command.startswith("ops ") and record.command.endswith(" reconfigure")
        and record.implemented)


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
    reconfigurable = _reconfigurable_features()
    feature = args_list[head + 1] if len(args_list) > head + 1 else None
    if feature in reconfigurable and args_list[head + 2:head + 3] == ["reconf"]:
        normalized = list(args_list)
        normalized[head + 2] = "reconfigure"
        return normalized, None
    return args_list, None


class RootError(Exception):
    """root の指定が directory でない。文面に指定された値をそのまま含める。"""


def _split_prefix(args_list: list[str]) -> tuple[int, str | None]:
    """subcommand より前の `--daemon` と `--root <dir>` を数える。head と最後の `--root` の値を返す。"""
    head = 0
    root: str | None = None
    while head < len(args_list):
        token = args_list[head]
        if token == "--daemon":
            head += 1
        elif token == "--root" and head + 1 < len(args_list):
            root = args_list[head + 1]
            head += 2
        elif token.startswith("--root="):
            root = token.partition("=")[2]
            head += 1
        else:
            break
    return head, root


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def _is_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.stat(path).st_mode)
    except OSError:
        return False


def _owned_by_current_user(path: Path) -> bool:
    try:
        return os.stat(path).st_uid == os.getuid()
    except OSError:
        return False


def resolve_root(argv: list[str], environ: Mapping[str, str], cwd: Path) -> Path:
    """root を `--root`、`AFX_ROOT`、cwd と親の `config/settings.yaml`、cwd の順に決める。

    上方探索は既存 root を対象にする操作だけに適用する。`init` と `--daemon` は探索せず cwd に落とす。
    探索は、所有者が実行 uid と異なる directory に達した時点で止めて cwd に落とす。
    """
    head, option = _split_prefix(argv)
    if option is not None:
        path = Path(os.path.abspath(Path(cwd) / option))
        if not path.is_dir():
            raise RootError(f"afx: --root {option} は directory ではありません")
        return path
    env_value = environ.get("AFX_ROOT")
    if env_value:
        path = Path(os.path.abspath(Path(cwd) / env_value))
        if not path.is_dir():
            raise RootError(f"afx: AFX_ROOT={env_value} は directory ではありません")
        return path
    here = Path(os.path.abspath(cwd))
    if "--daemon" in argv[:head] or argv[head:head + 1] == ["init"]:
        return here
    for candidate in (here, *here.parents):
        if not _owned_by_current_user(candidate):
            return here
        if (_is_directory(candidate / "config")
                and _is_regular_file(candidate / "config" / "settings.yaml")):
            return candidate
    return here


NEWS_USAGE_ERROR = "使い方が誤っています。afx news --help を参照してください。"
NEWS_INTERRUPTED = "中断しました。"


def _is_news(args_list: list[str]) -> bool:
    return args_list[_split_prefix(args_list)[0]:][:1] == ["news"]


def main(argv: list[str] | None = None) -> int:
    args_list = sys.argv[1:] if argv is None else list(argv)
    if not _is_news(args_list):
        return _main(args_list)
    try:
        return _main(args_list)
    except KeyboardInterrupt:
        print(NEWS_INTERRUPTED, file=sys.stderr)
        return 130


def _main(args_list: list[str]) -> int:
    # 全体 flag の後ろの ctl も argparse に通さない (REMAINDER は `--as` 始まりを
    # 受け取れない)。全体 flag は client には関係ないので読み捨て、daemon も起動しない。
    head, _ = _split_prefix(args_list)
    args_list, shim_rc = _shim_argv(args_list, head)
    if shim_rc is not None:
        return shim_rc
    if args_list[head:head + 1] == ["ctl"]:
        # client は .env も設定の dotenv も読まない。--as を含む残りの引数は client が読む。
        from agentic_fx.ops.client import main as ctl_main
        try:
            ctl_root = resolve_root(args_list, os.environ, Path.cwd())
        except RootError as error:
            print(error, file=sys.stderr)
            return 2
        return ctl_main(args_list[head + 1:], root=ctl_root)
    if args_list[head:head + 1] == ["completion"]:
        shell = args_list[head + 1] if len(args_list) > head + 1 else None
        if shell is not None and not shell.startswith("-") and shell != "bash":
            from agentic_fx import completion
            return completion.main(shell)
    # 略記 (--dae など) を許すと、上の完全一致の前置きをすり抜けて ctl が argparse に届く。
    parser = _OpsOptionParser(prog="afx", description=top_description(), epilog=render_top_epilog(),
                              formatter_class=HelpFormatter, allow_abbrev=False)
    parser.add_argument("--daemon", action="store_true",
                        help="systemd 用 (コマンド受付なし)")
    parser.add_argument("--root", metavar="DIR",
                        help="作業 root (未指定なら AFX_ROOT、無ければ上位の config/settings.yaml)")
    sub = parser.add_subparsers(dest="command")
    init_parser = sub.add_parser("init", help=parser_help("init"),
                                 description=parser_description("init"), epilog=render_epilog("init"),
                                 formatter_class=HelpFormatter)
    init_parser.add_argument("--non-interactive", action="store_true",
                             help="従来の非対話初期化を実行する")
    init_parser.add_argument("--config", type=Path, metavar="FILE",
                             help="settings.yaml として配置する検証済み設定ファイル")
    check_parser = sub.add_parser("check", help=parser_help("check"),
                                  description=parser_description("check"), epilog=render_epilog("check"),
                                  formatter_class=HelpFormatter)
    check_parser.add_argument("--json", action="store_true", dest="as_json",
                              help="結果を JSON ({\"fatal\": [...], \"warning\": [...]}) で出す")
    check_parser.add_argument("--unit-env", action="store_true", dest="unit_env",
                              help="unit の最小環境で呼ばれたときの検査 (秘密らしい env を fatal に数える)")
    check_sub = check_parser.add_subparsers(dest="check_action")
    backend_parser = check_sub.add_parser("backend", help=leaf_help("check backend"),
                                          description=parser_description("check backend"),
                                          epilog=render_epilog("check backend"), formatter_class=HelpFormatter)
    backend_parser.add_argument("--backend", choices=("local", "claude", "codex", "opencode"),
                                required=True)
    backend_parser.add_argument("--provider", choices=("chatgpt",), default=None)
    keys_parser = sub.add_parser("keys", help=parser_help("keys"),
                                 description=parser_description("keys"), epilog=render_epilog("keys"),
                                 formatter_class=HelpFormatter)
    keys_sub = keys_parser.add_subparsers(dest="keys_action", required=True)
    keys_sub.add_parser("init", help=leaf_help("keys init"),
                        description=parser_description("keys init"), epilog=render_epilog("keys init"),
                        formatter_class=HelpFormatter)
    for action in ("enroll", "rotate", "revoke"):
        command = f"keys {action}"
        p = keys_sub.add_parser(action, help=leaf_help(command),
                                description=parser_description(command), epilog=render_epilog(command),
                                formatter_class=HelpFormatter)
        p.add_argument("principal", choices=tuple(member.value for member in Principal))
    ops_parser = sub.add_parser("ops", help=parser_help("ops"),
                                description=parser_description("ops"), epilog=render_epilog("ops"),
                                formatter_class=HelpFormatter)
    ops_sub = ops_parser.add_subparsers(dest="feature", required=True)
    reconfigurable = _reconfigurable_features()
    for feature in ("bot", "web", "notify"):
        feature_command = f"ops {feature}"
        feature_parser = ops_sub.add_parser(feature, help=parser_help(feature_command),
                                             description=parser_description(feature_command),
                                             epilog=render_epilog(feature_command), formatter_class=HelpFormatter)
        feature_sub = feature_parser.add_subparsers(
            dest="ops_action", required=True,
            **({"parser_class": _OpsOptionParser} if feature == "web" else {}))
        actions = (("enable", "disable", "reconfigure", "status")
                   if feature in reconfigurable else ("enable", "disable", "status"))
        for action in actions:
            command = f"ops {feature} {action}"
            if action == "reconfigure":
                action_parser = feature_sub.add_parser(
                    action, help=leaf_help(command), description=parser_description(command),
                    epilog=render_epilog(command), formatter_class=HelpFormatter, allow_abbrev=False)
                if feature == "web":
                    _add_web_setup_options(action_parser)
                continue
            action_parser = feature_sub.add_parser(
                action, help=leaf_help(command), description=parser_description(command),
                epilog=render_epilog(command), formatter_class=HelpFormatter, **_web_parser_kw(feature))
            if feature == "web" and action == "enable":
                _add_web_setup_options(action_parser)
    completion_parser = sub.add_parser("completion", help=parser_help("completion"),
                                       description=parser_description("completion"),
                                       epilog=render_epilog("completion"), formatter_class=HelpFormatter)
    completion_sub = completion_parser.add_subparsers(dest="completion_action", required=True)
    completion_sub.add_parser("bash", help=leaf_help("completion bash"),
                              description=parser_description("completion bash"),
                              epilog=render_epilog("completion bash"), formatter_class=HelpFormatter)
    # ctl は上の分岐で client へ渡す。ここは help の一覧に載せるためだけに置く。
    sub.add_parser("ctl", add_help=False, help=parser_help("ctl"),
                   description=parser_description("ctl"), epilog=render_epilog("ctl"),
                   formatter_class=HelpFormatter)
    sub.add_parser("status", help=parser_help("status"), description=parser_description("status"),
                   epilog=render_epilog("status"), formatter_class=HelpFormatter)
    backtest_cli.register_subparsers(sub)
    try:
        args = parser.parse_args(args_list)
    except _OpsOptionError as error:
        if _is_news(args_list):
            print(NEWS_USAGE_ERROR, file=sys.stderr)
            raise SystemExit(2) from None
        if args_list[head:head + 2] == ["ops", "web"] and args_list[head + 2:head + 3] in (
                ["enable"], ["reconfigure"]):
            print("afx ops: setup_input_required", file=sys.stderr)
            return 2
        sys.stderr.write(str(error))
        raise SystemExit(2) from None

    if args.command == "completion":
        from agentic_fx import completion
        return completion.main(args.completion_action)
    try:
        root = resolve_root(args_list, os.environ, Path.cwd())
    except RootError as error:
        print(error, file=sys.stderr)
        return 2
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
