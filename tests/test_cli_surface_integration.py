"""Public CLI help is rendered from the command-surface metadata."""
from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
import unicodedata

import pytest

from agentic_fx import cli_surface
from agentic_fx.entry import main as entry_main


TOP_HELP = """usage: afx [-h] [--daemon] {init,check,keys,ops,ctl,status,history,backtest,analyze,plugin} ...

agentic-fx

positional arguments:
  {init,check,keys,ops,ctl,status,history,backtest,analyze,plugin}
    init                [変更] 初期設定 (対話。`--non-interactive` で従来の非対話)
    check               [下位操作ごと] 設定と実行環境の検査 (`backend` は非隔離・課金ありの接続確認)
    keys                [変更] 操作 API の鍵管理 (停止中のみ)
    ops                 [下位操作ごと] bot / web / notify の有効化・無効化・再設定・状態表示
    ctl                 稼働中の daemon を操作 API で操作する (afx ctl help)
    status              [読取] オフライン状態を書き込まずに表示する
    history             [下位操作ごと] 履歴データ操作
    backtest            [変更] バックテスト
    analyze             [変更] 履歴分析
    plugin              [下位操作ごと] plugin の採否・配備・表示

options:
  -h, --help            show this help message and exit
  --daemon              systemd 用 (コマンド受付なし)

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
    assert "  backlog list [--all] [--limit <n>]  バックログを表示する" in shell_help
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
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {
                    "ArgumentParser", "add_parser"}:
                for keyword in node.keywords:
                    if keyword.arg in {"help", "description", "epilog"}:
                        assert not isinstance(keyword.value, (ast.Constant, ast.JoinedStr)), path
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in metadata_copy, (path, node.value)
