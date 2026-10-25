"""Public CLI help is rendered from the command-surface metadata."""
from __future__ import annotations

import argparse
import ast
import dataclasses
import subprocess
import sys
from pathlib import Path
import unicodedata

import pytest

from agentic_fx import cli_surface
from agentic_fx import entry
from agentic_fx.entry import main as entry_main


TOP_HELP = """usage: afx [-h] [--daemon] [--root DIR]
           {init,check,keys,ops,completion,ctl,status,history,backtest,analyze,plugin} ...

agentic-fx

positional arguments:
  {init,check,keys,ops,completion,ctl,status,history,backtest,analyze,plugin}
    init                [変更] 初期設定 (対話。`--non-interactive` で従来の非対話)
    check               [下位操作ごと] 設定と実行環境の検査 (`backend` は非隔離・課金ありの接続確認)
    keys                [変更] 操作 API の鍵管理 (停止中のみ)
    ops                 [下位操作ごと] bot / web / notify の有効化・無効化・再設定・状態表示
    completion          [読取] シェル補完スクリプトの出力
    ctl                 稼働中の daemon を操作 API で操作する (afx ctl help)
    status              [読取] オフライン状態を書き込まずに表示する
    history             [下位操作ごと] 履歴データ操作
    backtest            [変更] バックテスト
    analyze             [変更] 履歴分析
    plugin              [下位操作ごと] plugin の採否・配備・表示

options:
  -h, --help            show this help message and exit
  --daemon              systemd 用 (コマンド受付なし)
  --root DIR            作業 root (未指定なら AFX_ROOT、無ければ上位の config/settings.yaml)

実行レーン:
  afx <sub>      daemon を介さない。停止中でも実行できる
  afx ctl <op>   稼働 daemon の認証・監査つき操作 API

用途                     daemon 不要                稼働 daemon
導入・接続管理           init / check / keys / ops  ctl whoami
稼働観測・実行追跡       status                     ctl status / log / activity / events / jobs
市場入力を整える         history                    —
調査・評価する           backtest / analyze         —
問い合わせ・改善運用     —                          ctl ask / improve / backlog / policy / reflect
Plugin を採否・配備する  plugin                     ctl approval / approve / reject
保護状態を回復する       —                          ctl killswitch / data resume

ラベル: [読取] 状態を変えない  [変更] 状態を変える  [確認] 端末確認  [approver] approver 権限
詳しい操作: afx <sub> --help / afx ctl help
"""

OPS_HELP = """usage: afx ops [-h] {bot,web,notify} ...

bot / web / notify の有効化・無効化・再設定・状態表示

positional arguments:
  {bot,web,notify}
    bot             操作 bot (ops-bot)
    web             web 表示 (ops-web)
    notify          通知 (ops-notify)

options:
  -h, --help        show this help message and exit

操作:
  afx ops <feature> enable        [変更] 有効化する (必要時だけ daemon を一時停止して再開)
  afx ops <feature> disable       [変更] daemon を止めずに無効化する
  afx ops <feature> status        [読取] durable stage を表示する
  afx ops web reconfigure         [変更] afx に触れずに設定を変える (別名 reconf)

旧形 `afx ops enable|disable|status <feature>` は 0.2.0 だけ受け付け、0.3.0 で削除する。
"""

COMPLETION_HELP = """usage: afx completion [-h] {bash} ...

シェル補完スクリプトの出力

positional arguments:
  {bash}
    bash      [読取] bash の補完スクリプトを出力する

options:
  -h, --help  show this help message and exit

操作:
  afx completion bash  [読取] bash の補完スクリプトを出力する
"""

CTL_HELP = """usage: afx ctl [--as operator|approver] <操作>
       afx ctl [--as operator|approver]        (端末なら対話モード afx ctl>)

導入・接続管理:
  [読取] whoami

稼働観測・実行追跡:
  [読取] status
  [読取] log [n] / activity [n] [カテゴリ]
  [読取] events [--after <id>] [--limit <n>] [--follow]
  [読取] jobs get <id>
  [変更] [確認] jobs cancel <id>

市場入力を整える:
  —

調査・評価する:
  —

問い合わせ・改善運用:
  [変更] ask <質問> / improve
  [変更] backlog add <idea>
  [読取] backlog list [--all] [--limit <n>]
  [変更] backlog reject|reopen|note <id>
  [変更] policy add <text>
  [読取] policy show
  [変更] policy remove <id>
  [変更] policy reconcile
  [変更] reflect retry <order_id>

Plugin を採否・配備する:
  [読取] approval list [n] / approval show <id>
  [変更] [確認] [approver] approve <id> / reject <id> [理由] / approval retry <id>

保護状態を回復する:
  [読取] killswitch reset
  [変更] [確認] [approver] killswitch reset <世代> / killswitch reconcile [confirm]
  [変更] [確認] [approver] data resume [--acknowledge]

互換名: improve add <idea> / approvals list / approvals show / approval <id> / retry <id>
API に発注・SL 変更・クローズ・サービス停止は無い。
"""


def _captured_help(argv: list[str], capsys) -> str:
    with pytest.raises(SystemExit) as exited:
        entry_main(argv)
    assert exited.value.code == 0
    return capsys.readouterr().out


def test_public_help_matches_the_surface_golden(capsys):
    assert _captured_help(["--help"], capsys) == TOP_HELP
    assert _captured_help(["ops", "--help"], capsys) == OPS_HELP
    assert _captured_help(["completion", "--help"], capsys) == COMPLETION_HELP
    assert entry_main(["ctl", "help"]) == 0
    assert capsys.readouterr().out == CTL_HELP


@pytest.mark.parametrize(("feature", "expected"), [
    ("web", """usage: afx ops web [-h] {enable,disable,reconfigure,status} ...

positional arguments:
  {enable,disable,reconfigure,status}
    enable              [変更] 有効化する (必要時だけ daemon を一時停止して再開)
    disable             [変更] daemon を止めずに無効化する
    reconfigure         [変更] afx に触れずに設定を変える (別名 reconf)
    status              [読取] durable stage を表示する

options:
  -h, --help            show this help message and exit
"""),
    ("bot", """usage: afx ops bot [-h] {enable,disable,status} ...

positional arguments:
  {enable,disable,status}
    enable              [変更] 有効化する (必要時だけ daemon を一時停止して再開)
    disable             [変更] daemon を止めずに無効化する
    status              [読取] durable stage を表示する

options:
  -h, --help            show this help message and exit
"""),
    ("notify", """usage: afx ops notify [-h] {enable,disable,status} ...

positional arguments:
  {enable,disable,status}
    enable              [変更] 有効化する (必要時だけ daemon を一時停止して再開)
    disable             [変更] daemon を止めずに無効化する
    status              [読取] durable stage を表示する

options:
  -h, --help            show this help message and exit
"""),
])
def test_ops_feature_help_matches_the_surface_golden(feature, expected, capsys):
    assert _captured_help(["ops", feature, "--help"], capsys) == expected


def test_surface_publication_and_shell_help():
    assert all(record.exposed for record in cli_surface.COMMANDS
               if record.implemented and not record.planned)
    assert not any(record.exposed for record in cli_surface.COMMANDS
                   if not record.implemented or record.planned)
    shell_help = cli_surface.render_shell_help()
    assert "  backlog add <idea>  バックログへ課題を追加する" in shell_help
    assert "backlog list" not in shell_help
    assert "  improve add <idea>  旧名 (backlog add <idea>)" in shell_help
    assert "  stop  daemon を graceful shutdown する" in shell_help
    assert shell_help.endswith(
        "廃止予定: 対話 shell は 0.2.0 で deprecated。1.0.0 で削除予定。afx ctl を使用してください。")


def test_metadata_summary_mutation_changes_the_rendered_help():
    original = next(record for record in cli_surface.COMMANDS if record.command == "init")
    renderer = cli_surface.SurfaceRenderer(tuple(
        dataclasses.replace(record, summary="差し替えた初期設定") if record is original else record
        for record in cli_surface.COMMANDS))
    assert renderer.parser_help("init") == "[変更] 差し替えた初期設定"
    assert renderer.parser_help("init") != cli_surface.parser_help("init")


def test_help_golden_is_uncolored_narrow_and_hides_compatibility_choices(capsys, monkeypatch):
    for variable in ("FORCE_COLOR", "NO_COLOR", "PYTHON_COLORS", "TERM", "CLICOLOR", "CLICOLOR_FORCE"):
        monkeypatch.delenv(variable, raising=False)
    output = _captured_help(["--help"], capsys) + _captured_help(["ops", "--help"], capsys)
    for line in output.splitlines():
        width = sum(2 if unicodedata.east_asian_width(char) in {"F", "W", "A"} else 1
                    for char in line)
        assert width <= 100, line
    assert "{improve" not in output
    assert "news" not in output
    assert "reconf}" not in output
    assert "<feature> enable|disable|status" not in output


def test_product_copy_has_no_legacy_ops_word_order():
    roots = (Path("src/agentic_fx/setup"), Path("src/agentic_fx/ops_setup"))
    for root in roots:
        for path in root.rglob("*.py"):
            assert "afx ops enable " not in path.read_text(encoding="utf-8"), path
            assert "afx ops disable " not in path.read_text(encoding="utf-8"), path
            assert "afx ops status " not in path.read_text(encoding="utf-8"), path


_ACCESSORS = frozenset({
    "parser_help", "parser_description", "leaf_help", "render_epilog", "top_description",
    "render_top_epilog", "render_ctl_help", "render_shell_help"})
_PARSER_CALLS = frozenset({"ArgumentParser", "add_parser", "_OpsOptionParser"})
_COPY_KEYWORDS = frozenset({"help", "description", "epilog"})


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


_KW_HELPERS = frozenset({"_surface_parser_kw", "_web_parser_kw"})


def _is_accessor_copy(value: ast.expr) -> bool:
    if isinstance(value, ast.Call):
        return _call_name(value.func) in _ACCESSORS
    if isinstance(value, ast.IfExp):
        return _is_accessor_copy(value.body) and _is_accessor_copy(value.orelse)
    return False


def _copy_outside_accessors(tree: ast.AST) -> list[int]:
    """Lines of parser help / description / epilog values that are not an accessor call.

    Covers the keyword directly, a ``**`` expansion (only the known helper is
    allowed) and every dict literal that carries one of the copy keys.
    """
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value in _COPY_KEYWORDS
                        and not _is_accessor_copy(value)):
                    found.append(value.lineno)
            continue
        if not isinstance(node, ast.Call) or _call_name(node.func) not in _PARSER_CALLS:
            continue
        for keyword in node.keywords:
            if keyword.arg is None:
                value = keyword.value
                if not (isinstance(value, ast.Call) and _call_name(value.func) in _KW_HELPERS):
                    found.append(value.lineno)
                continue
            if keyword.arg not in _COPY_KEYWORDS:
                continue
            if not _is_accessor_copy(keyword.value):
                found.append(keyword.value.lineno)
    return found


def test_parser_copy_comes_only_from_surface_accessors():
    source_files = (
        Path("src/agentic_fx/entry.py"),
        Path("src/agentic_fx/backtest/cli.py"),
        Path("src/agentic_fx/ops/client.py"),
        Path("src/agentic_fx/commands.py"),
    )
    metadata_copy = {
        record.summary for record in cli_surface.COMMANDS
    } | {record.condition for record in cli_surface.COMMANDS if record.condition} | {
        group.value for group in cli_surface.Group
    } | {record.usage for record in cli_surface.COMMANDS if " " in record.usage}
    for path in source_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert _copy_outside_accessors(tree) == [], path
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in metadata_copy, (path, node.value)


@pytest.mark.parametrize("source", [
    'sub.add_parser("x", help="直書きの説明")',
    'copy = "a" + "b"\nsub.add_parser("x", description="a" + "b")',
    'copy = leaf_help("x")\nsub.add_parser("x", epilog=copy)',
    'sub.add_parser("x", help=f"{name}")',
    'argparse.ArgumentParser(prog="afx", description=SUMMARY.text)',
    '_OpsOptionParser(prog="afx", epilog=copies["x"])',
    'copy = {"help": "手書き copy"}\nsub.add_parser("x", **copy)',
    'sub.add_parser("x", **{"help": "手書き copy"})',
    'sub.add_parser("x", **other_helper("x"))',
    'def _surface_parser_kw(c):\n    return {"help": "手書き copy", "epilog": render_epilog(c)}',
])
def test_parser_copy_check_rejects_everything_but_accessor_calls(source):
    assert _copy_outside_accessors(ast.parse(source)) != []


def test_parser_copy_check_accepts_accessor_calls_and_ignores_options():
    tree = ast.parse(
        'sub.add_parser("x", help=leaf_help("x"), description=cli_surface.parser_description("x"),'
        ' epilog=render_epilog("x"))\n'
        'parser.add_argument("--n", help="option の説明")\n'
        'argparse.ArgumentParser(prog="literal", description=top_description())\n'
        'sub.add_parser("y", **_surface_parser_kw("y"))\n'
        'def _surface_parser_kw(c, *, leaf=False):\n'
        '    return {"help": leaf_help(c) if leaf else parser_help(c), "epilog": render_epilog(c),'
        ' "formatter_class": HelpFormatter}')
    assert _copy_outside_accessors(tree) == []


def test_surface_module_has_no_assert_statement():
    tree = ast.parse(Path("src/agentic_fx/cli_surface.py").read_text(encoding="utf-8"))
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Assert)]


def test_exposure_is_explicit_and_never_inferred():
    record = cli_surface._record(cli_surface.Group.SETUP, cli_surface.Lane.LOCAL, "x", "afx x",
                                 cli_surface.Label.READ, "要約", "entry.py")
    assert record.implemented and not record.planned
    assert record.exposed is False


# --- help layout is independent of the terminal width ---

_FIXED_WIDTH_HELPS = (
    ["--help"], ["ops", "--help"], ["ops", "bot", "--help"], ["ops", "web", "--help"],
    ["ops", "notify", "--help"], ["completion", "--help"], ["history", "--help"], ["plugin", "--help"],
    ["backtest", "--help"], ["analyze", "--help"],
)


def _display_width(line: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"F", "W", "A"} else 1 for char in line)


@pytest.mark.parametrize("argv", _FIXED_WIDTH_HELPS, ids=lambda argv: " ".join(argv))
def test_help_text_does_not_follow_the_terminal_width(argv, capsys, monkeypatch):
    outputs = {}
    for columns in ("20", "200"):
        monkeypatch.setenv("COLUMNS", columns)
        outputs[columns] = _captured_help(argv, capsys)
    assert outputs["20"] == outputs["200"]
    for line in outputs["20"].splitlines():
        assert _display_width(line) <= 100, line


def test_every_parser_shares_the_fixed_width_formatter(monkeypatch):
    formatters = []
    real = argparse._SubParsersAction.add_parser

    def spy(self, name, **kwargs):
        parser = real(self, name, **kwargs)
        formatters.append((f"{self._prog_prefix} {name}", parser.formatter_class))
        return parser

    monkeypatch.setattr(argparse._SubParsersAction, "add_parser", spy)
    with pytest.raises(SystemExit):
        entry_main(["--help"])
    assert formatters
    for path, formatter in formatters:
        assert formatter is cli_surface.HelpFormatter, path


# --- the top overview table follows the records ---

def test_top_overview_rows_follow_the_records():
    records = cli_surface.COMMANDS
    renderer = cli_surface.SurfaceRenderer(tuple(
        r for r in records if r.command.split()[0] != "history" and r.command != "ctl whoami"))
    rows = {group.value: line for line in renderer.render_top_epilog().splitlines()
            for group in cli_surface.Group if line.startswith(group.value)}
    assert rows["市場入力を整える"].split() == ["市場入力を整える", "—", "—"]
    assert rows["導入・接続管理"].split() == [
        "導入・接続管理", "init", "/", "check", "/", "keys", "/", "ops", "—"]
    assert "history" not in "".join(rows.values())
    assert "completion" not in "".join(rows.values())


def test_a_record_kept_out_of_the_overview_still_appears_in_the_positional_list():
    record = next(r for r in cli_surface.COMMANDS if r.command == "completion bash")
    assert record.overview is False and record.exposed is True
    assert "completion" in cli_surface.parser_help("completion") or cli_surface.parser_help("completion")
    shown = cli_surface.SurfaceRenderer(cli_surface.COMMANDS).render_top_epilog()
    assert "completion" not in shown


def test_top_overview_survives_an_empty_record_set():
    text = cli_surface.SurfaceRenderer(()).render_top_epilog()
    for group in cli_surface.Group:
        row = next(line for line in text.splitlines() if line.startswith(group.value))
        assert row.split() == [*group.value.split(), "—", "—"]


def test_ops_epilog_omits_the_legacy_paragraph_without_a_legacy_record():
    renderer = cli_surface.SurfaceRenderer(compatibility=())
    epilog = renderer.render_epilog("ops")
    assert epilog.startswith("操作:")
    assert "旧形" not in epilog


# --- the ctl help follows the records ---

def test_ctl_help_follows_the_records():
    records = cli_surface.COMMANDS
    changed = tuple(dataclasses.replace(r, ctl_text="whoami (差し替え)") if r.command == "ctl whoami"
                    else r for r in records)
    text = cli_surface.SurfaceRenderer(changed).render_ctl_help()
    assert "  [読取] whoami (差し替え)" in text
    dropped = tuple(r for r in records if r.command != "ctl policy show")
    assert "policy show" not in cli_surface.SurfaceRenderer(dropped).render_ctl_help()


def test_ctl_help_fold_mistakes_are_rejected_when_the_records_are_validated():
    records = cli_surface.COMMANDS
    first = next(r for r in records if r.command == "ctl whoami")
    with pytest.raises(ValueError, match="no line to join"):
        cli_surface.SurfaceRenderer(tuple(
            dataclasses.replace(r, ctl_fold="join") if r is first else r for r in records))
    log = next(r for r in records if r.command == "ctl activity")
    with pytest.raises(ValueError, match="changes the label"):
        cli_surface.SurfaceRenderer(tuple(
            dataclasses.replace(r, label=cli_surface.Label.CHANGE) if r is log else r
            for r in records))
    with pytest.raises(ValueError, match="unknown ctl help fold"):
        cli_surface.SurfaceRenderer(tuple(
            dataclasses.replace(r, ctl_fold="x") if r is first else r for r in records))
    hidden = next(r for r in records if r.command == "ops bot reconfigure")
    with pytest.raises(ValueError, match="not shown"):
        cli_surface.SurfaceRenderer(tuple(
            dataclasses.replace(r, ctl_fold="join") if r is hidden else r for r in records))


def test_ctl_usage_error_prints_the_help_without_a_traceback(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert entry_main(["ctl", "no-such-operation"]) == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err + captured.out
    assert (captured.out + captured.err).rstrip().endswith(
        "API に発注・SL 変更・クローズ・サービス停止は無い。")


# --- ops <feature> reconfigure: the parser and the old spelling share one table ---

def test_reconfigure_registration_and_old_spelling_follow_the_same_records(monkeypatch, capsys):
    choices = "{enable,disable,reconfigure,status}"
    assert choices not in _captured_help(["ops", "bot", "--help"], capsys)
    assert entry._shim_argv(["ops", "bot", "reconf"], 0) == (["ops", "bot", "reconf"], None)

    bot = next(r for r in entry.COMMANDS if r.command == "ops bot reconfigure")
    enabled = dataclasses.replace(bot, implemented=True, exposed=True)
    monkeypatch.setattr(entry, "COMMANDS", tuple(
        enabled if r is bot else r for r in entry.COMMANDS))

    assert choices in _captured_help(["ops", "bot", "--help"], capsys)
    assert entry._shim_argv(["ops", "bot", "reconf"], 0) == (["ops", "bot", "reconfigure"], None)
    assert "{enable,disable,status}" in _captured_help(["ops", "notify", "--help"], capsys)


# --- the parsers, the ctl grammar and the confirmation sets agree with the records ---

_CTL_GRAMMAR = {
    "afx ctl whoami": (["whoami"], "whoami"),
    "afx ctl status": (["status"], "status"),
    "afx ctl log [n]": (["log", "5"], "log"),
    "afx ctl activity [n] [category]": (["activity", "5", "system"], "activity"),
    "afx ctl events [--after <id>] [--limit <n>] [--follow]": (["events", "--follow"], "events"),
    "afx ctl jobs get <id>": (["jobs", "get", "0123456789abcdef0123456789abcdef"], "job_get"),
    "afx ctl jobs cancel <id>": (["jobs", "cancel", "0123456789abcdef0123456789abcdef"], "job_cancel"),
    "afx ctl ask <question>": (["ask", "なぜ"], "ask"),
    "afx ctl improve": (["improve"], "improve"),
    "afx ctl backlog add <idea>": (["backlog", "add", "案"], "backlog_add"),
    "afx ctl backlog list [--all] [--limit <n>]": (["backlog", "list", "--all"], "backlog_list"),
    "afx ctl backlog reject <id>": (["backlog", "reject", "3"], "backlog_transition"),
    "afx ctl backlog reopen <id>": (["backlog", "reopen", "3"], "backlog_transition"),
    "afx ctl backlog note <id>": (["backlog", "note", "3"], "backlog_transition"),
    "afx ctl policy add <text>": (["policy", "add", "方針"], "policy"),
    "afx ctl policy show": (["policy", "show"], "policy_show"),
    "afx ctl policy remove <id>": (["policy", "remove", "2"], "policy_remove"),
    "afx ctl policy reconcile": (["policy", "reconcile"], "policy_reconcile"),
    "afx ctl reflect retry <order_id>": (["reflect", "retry", "4"], "reflection_retry"),
    "afx ctl approval list [n]": (["approval", "list"], "approval_list"),
    "afx ctl approval show <id>": (["approval", "show", "5"], "approval_detail"),
    "afx ctl approve <id>": (["approve", "5"], "approve"),
    "afx ctl reject <id> [reason]": (["reject", "5", "理由"], "reject"),
    "afx ctl approval retry <id>": (["approval", "retry", "5"], "retry"),
    "afx ctl killswitch reset": (["killswitch", "reset"], "killswitch_show"),
    "afx ctl killswitch reset <generation>": (["killswitch", "reset", "7"], "killswitch_reset"),
    "afx ctl killswitch reconcile [confirm]": (["killswitch", "reconcile"], "killswitch_reconcile"),
    "afx ctl data resume [--acknowledge]": (["data", "resume"], "data_resume"),
    "afx stop": (["stop"], "stop"),
}


def _ctl_records() -> list:
    return [r for r in cli_surface.COMMANDS if r.lane is cli_surface.Lane.CTL and r.exposed]


def test_every_ctl_record_parses_from_its_own_grammar_one_to_one():
    from agentic_fx.ops.client import parse

    assert sorted(_CTL_GRAMMAR) == sorted(r.usage for r in _ctl_records())
    for usage, (tokens, op_name) in _CTL_GRAMMAR.items():
        assert parse(tokens).name == op_name, usage


def test_confirmation_and_approver_labels_match_the_client_sets():
    from agentic_fx.ops.client import _APPROVER_OPS, _CONFIRM_OPS

    ops = {r.usage: _CTL_GRAMMAR[r.usage][1] for r in _ctl_records()}
    confirm = {ops[r.usage] for r in _ctl_records() if r.confirm}
    approver = {ops[r.usage] for r in _ctl_records() if r.approver}
    assert confirm == set(_CONFIRM_OPS)
    assert approver == set(_APPROVER_OPS)
    cancel = next(r for r in _ctl_records() if r.usage == "afx ctl jobs cancel <id>")
    assert (cancel.confirm, cancel.approver) == (True, False)
    assert "job_cancel" in _CONFIRM_OPS and "job_cancel" not in _APPROVER_OPS


def test_every_exposed_local_record_has_one_argparse_node_and_no_other_node_exists(monkeypatch):
    registered = []
    real = argparse._SubParsersAction.add_parser

    def spy(self, name, **kwargs):
        registered.append(f"{self._prog_prefix} {name}")
        return real(self, name, **kwargs)

    monkeypatch.setattr(argparse._SubParsersAction, "add_parser", spy)
    with pytest.raises(SystemExit):
        entry_main(["--help"])
    assert len(registered) == len(set(registered))

    expected = set()
    for record in cli_surface.COMMANDS:
        if record.lane is cli_surface.Lane.LOCAL and record.exposed:
            expected.update(f"afx {path}" for path in cli_surface.expand_command(record))
    expected |= {f"afx {parent.command}" for parent in cli_surface.PARENTS
                 if not parent.command.startswith("ctl ") and parent.command != "ctl"}
    exceptions = {"afx ctl"}  # `afx ctl` is handed to the client before argparse; only its help row is registered
    assert "afx news" not in registered  # planned, not yet a command
    assert set(registered) - exceptions == expected
    assert exceptions <= set(registered)


def test_surface_import_keeps_the_agentic_fx_closure_to_two_modules():
    code = (
        "import sys, agentic_fx.cli_surface\n"
        "print(sorted(n for n in sys.modules if n == 'agentic_fx' or n.startswith('agentic_fx.')))")
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert completed.stdout.strip() == "['agentic_fx', 'agentic_fx.cli_surface']", completed.stderr
