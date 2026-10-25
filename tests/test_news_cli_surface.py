"""``afx news`` in the command surface: dispatch position, goldens, no route from agents or the API."""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

from agentic_fx import cli_surface, entry, news_cli
from agentic_fx.backtest import cli as backtest_cli
from agentic_fx.ops import client
from agentic_fx.ops.service import ENDPOINTS
from tests.news_cli_support import FakeStdin, make_root, run_news
from tests.test_cli_surface_integration import NEWS_HELP, TOP_HELP

REPO = Path(__file__).resolve().parents[1]
CLI_SURFACE_SPEC = REPO / "docs" / "superpowers" / "specs" / "2026-10-23-cli-surface-design.md"
OPS_API_DOC = REPO / "docs" / "operations" / "ops-api-2026-10-09.md"


# --- dispatch --------------------------------------------------------------------------------------

def test_news_is_dispatched_by_the_local_backtest_family_and_never_starts_the_daemon(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    seen = []
    monkeypatch.setattr(news_cli, "dispatch_news", lambda args, given: (seen.append((args.news_command, given)), 0)[1])
    monkeypatch.setattr(entry.service, "run_service", lambda *a, **k: pytest.fail("daemon started"))
    assert "news" in entry._BACKTEST_COMMANDS
    assert entry.main(["--root", str(root), "news", "list"]) == 0
    assert seen == [("list", root)]


@pytest.mark.parametrize("argv", [("list",), ("enable", "x"), ("disable", "x"), ("test", "x")])
def test_the_news_branch_runs_before_initialisation_settings_and_database_creation(
        tmp_path, monkeypatch, capsys, argv):
    root = make_root(tmp_path, sources=(("x", "feed", "https://example.invalid/x", True),))
    for name in ("ensure_initialized", "load_env_file", "load_settings", "connect", "init_db"):
        monkeypatch.setattr(backtest_cli, name, lambda *a, _n=name, **k: pytest.fail(f"{_n} reached"))
    monkeypatch.setattr(sys, "stdin", FakeStdin())
    assert entry.main(["--root", str(root), "news", *argv]) in (0, 1)


def test_argparse_usage_errors_stay_with_argparse(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    for argv in (("add",), ("add", "only-name"), ("enable",), ("bogus",), ()):
        with pytest.raises(SystemExit) as raised:
            run_news(capsys, monkeypatch, root, *argv)
        assert raised.value.code == 2


# --- records and goldens -----------------------------------------------------------------------------

def test_news_is_a_parent_with_five_classified_leaf_records_and_no_planned_record():
    leaves = {record.command: record for record in cli_surface.COMMANDS if record.command.startswith("news ")}
    assert sorted(leaves) == ["news add", "news disable", "news enable", "news list", "news test"]
    assert {name: record.label.name for name, record in leaves.items()} == {
        "news list": "READ", "news test": "READ", "news add": "CHANGE", "news enable": "CHANGE",
        "news disable": "CHANGE"}
    assert leaves["news add"].condition == "0 件のとき確認" and leaves["news add"].confirm is False
    assert all(record.implemented and record.exposed and not record.planned and record.endpoint_code is None
               for record in leaves.values())
    assert [parent.summary for parent in cli_surface.PARENTS if parent.command == "news"] == ["市場ニュース源を扱う"]
    assert not [record for record in cli_surface.COMMANDS if record.planned]


def _golden_block(heading: str) -> str:
    text = CLI_SURFACE_SPEC.read_text(encoding="utf-8")
    match = re.search(re.escape(heading) + r".*?```text\n(.*?)```", text, re.DOTALL)
    assert match, heading
    return match.group(1)


def test_the_design_document_golden_for_afx_help_is_the_tested_constant_byte_for_byte():
    assert _golden_block("### 3.2 `afx --help` の全文 golden") == TOP_HELP


def test_the_design_document_golden_for_afx_news_help_is_the_tested_constant_byte_for_byte():
    assert _golden_block("### 3.2.3 `afx news --help` の全文 golden") == NEWS_HELP


# --- documents -----------------------------------------------------------------------------------------

def test_the_principle_table_lists_the_news_operations_in_the_market_row():
    row = next(line for line in OPS_API_DOC.read_text(encoding="utf-8").splitlines()
               if line.startswith("| 市場入力を整える |"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[1] == ("`[変更]` `afx history import` / `[読取]` `afx history compare`・`coverage` / "
                        "`[読取]` `afx news list`・`test` / `[変更]` `afx news add`・`enable`・`disable`")
    assert cells[2] == "—"


def test_the_design_document_no_longer_describes_news_as_reserved_or_hidden():
    text = CLI_SURFACE_SPEC.read_text(encoding="utf-8")
    assert "afx news list" in text and "afx news add" in text
    assert "本書は予約行のみ" not in text
    assert "予約 record とし、本 release の choices と help には出さない" not in text
    assert "choices に `improve` / `news` は出ない" not in text


# --- no route from the API or an agent -----------------------------------------------------------------

def test_no_operation_endpoint_exists_for_news_and_ctl_cannot_parse_it(tmp_path, monkeypatch, capsys):
    assert len(ENDPOINTS) == 29
    assert not [code for code in ENDPOINTS if "news" in code]
    assert not [e.route for e in ENDPOINTS.values() if "news" in e.route]
    for tokens in (["news", "list"], ["news", "add", "x", "https://example.invalid/"], ["news"]):
        with pytest.raises(client.ClientError) as raised:
            client.parse(tokens)
        assert raised.value.rc == client.RC_USAGE
    root = tmp_path / "bare"
    root.mkdir()
    assert entry.main(["--root", str(root), "ctl", "news", "list"]) == 2


# Modules that must never change the managed news sources.  Scanning is by path prefix so a new
# module under these trees is covered without editing the test.
FORBIDDEN_TREES = ("src/agentic_fx/tools", "src/agentic_fx/ops", "src/agentic_fx/ops_setup",
                   "src/agentic_fx/loops", "src/agentic_fx/runners", "src/agentic_fx/plugin",
                   "afx-ops/afx_ops")
FORBIDDEN_FILES = ("src/agentic_fx/mission_worker.py", "src/agentic_fx/commands.py", "src/agentic_fx/shell.py")
# The only callers allowed to write the table: the human command, the collector (health and
# bookkeeping around the same rows), the first-run wizard, and the store module itself.
ALLOWED_WRITERS = frozenset({
    "src/agentic_fx/news_cli.py", "src/agentic_fx/datafeed/news_collector.py",
    "src/agentic_fx/setup/wizard.py", "src/agentic_fx/store/news_sources.py"})
_WRITE_SQL = re.compile(r"(?is)\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+news_sources\b")
_WRITE_FUNCTIONS = frozenset({"add", "set_enabled"})
_MODULE_PATH = "news_sources"


def write_paths(source: str) -> list[str]:
    """Every way the given source text could change ``news_sources``: calls, imports, SQL."""
    tree = ast.parse(source)
    module_aliases: set[str] = set()
    function_aliases: set[str] = set()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name == _MODULE_PATH:
                    module_aliases.add(bound)
                if module.endswith(_MODULE_PATH) and (alias.name in _WRITE_FUNCTIONS or alias.name == "*"):
                    function_aliases.add(bound)
                    found.append(f"import {alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith(_MODULE_PATH):
                    module_aliases.add(alias.asname or alias.name.rsplit(".", 1)[-1])
    module_aliases.add(_MODULE_PATH)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr in _WRITE_FUNCTIONS
                    and isinstance(func.value, ast.Name) and func.value.id in module_aliases):
                found.append(f"{func.value.id}.{func.attr}")
            elif isinstance(func, ast.Name) and func.id in function_aliases:
                found.append(func.id)
            elif (isinstance(func, (ast.Name, ast.Attribute))
                  and (func.id if isinstance(func, ast.Name) else func.attr) in {"import_module", "__import__"}
                  and node.args and isinstance(node.args[0], ast.Constant)
                  and isinstance(node.args[0].value, str) and _MODULE_PATH in node.args[0].value):
                found.append("dynamic import")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _WRITE_SQL.search(node.value):
            found.append("sql")
    return found


def _python_files(relative: str) -> list[Path]:
    path = REPO / relative
    return sorted(path.rglob("*.py")) if path.is_dir() else [path]


def test_agents_the_api_and_the_bots_have_no_path_that_changes_news_sources():
    scanned = []
    for relative in (*FORBIDDEN_TREES, *FORBIDDEN_FILES):
        files = _python_files(relative)
        assert files, relative
        for path in files:
            scanned.append(path)
            assert not write_paths(path.read_text(encoding="utf-8")), path.relative_to(REPO)
    assert len(scanned) > 40
    assert not set(FORBIDDEN_TREES + FORBIDDEN_FILES) & ALLOWED_WRITERS


def test_only_the_listed_callers_change_news_sources_anywhere_in_the_source_tree():
    offenders = []
    for root in ("src/agentic_fx", "afx-ops/afx_ops"):
        for path in _python_files(root):
            relative = path.relative_to(REPO).as_posix()
            if relative not in ALLOWED_WRITERS and write_paths(path.read_text(encoding="utf-8")):
                offenders.append(relative)
    assert offenders == []
    assert write_paths((REPO / "src/agentic_fx/news_cli.py").read_text(encoding="utf-8"))


@pytest.mark.parametrize("snippet", [
    "news_sources.add(conn, name='x')",
    "news_sources.set_enabled(conn, 1, True)",
    "from agentic_fx.store import news_sources as ns\nns.add(conn)",
    "import agentic_fx.store.news_sources as ns\nns.set_enabled(conn, 1, True)",
    "from agentic_fx.store.news_sources import add as put\nput(conn)",
    "def f():\n    from agentic_fx.store.news_sources import set_enabled\n    set_enabled(c, 1, True)",
    "import importlib\nimportlib.import_module('agentic_fx.store.news_sources')",
    "conn.execute('INSERT INTO news_sources (name) VALUES (1)')",
    "conn.execute(\"update news_sources set enabled=1\")",
    "conn.execute(f'DELETE FROM news_sources WHERE id={i}')",
    "conn.execute('INSERT OR REPLACE INTO news_sources VALUES (1)')",
])
def test_the_scanner_catches_each_way_of_writing_the_table(snippet):
    assert write_paths(snippet)


@pytest.mark.parametrize("snippet", [
    "news_sources.list_enabled(conn)",
    "conn.execute('SELECT * FROM news_sources')",
    "other.add(conn)",
])
def test_the_scanner_lets_reads_pass(snippet):
    assert write_paths(snippet) == []


@pytest.mark.parametrize("relative", ["src/agentic_fx/tools/news_tools.py", "src/agentic_fx/ops/service.py",
                                      "src/agentic_fx/loops/improve_loop.py", "afx-ops/afx_ops/web/routes.py"])
def test_adding_one_write_call_to_a_guarded_module_would_be_caught(relative):
    source = (REPO / relative).read_text(encoding="utf-8") + "\nnews_sources.add(None)\n"
    assert write_paths(source)


# --- argv echo and interruption boundary -----------------------------------------------------------

_CANARY = "CANARY-9f3a"
_NEWS_USAGE_ERROR = "使い方が誤っています。afx news --help を参照してください。\n"


@pytest.mark.parametrize("argv", [
    (f"https://secret.example/feed?k={_CANARY}",),
    ("add", "x", "https://ok.invalid/", f"https://secret.example/feed?k={_CANARY}"),
    ("add", "x", "https://ok.invalid/", f"--bogus-{_CANARY}"),
    ("list", f"https://secret.example/feed?k={_CANARY}"),
])
def test_argparse_errors_under_news_never_echo_argv(tmp_path, monkeypatch, capsys, argv):
    root = make_root(tmp_path)
    with pytest.raises(SystemExit) as raised:
        run_news(capsys, monkeypatch, root, *argv)
    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert _CANARY not in captured.out + captured.err
    assert captured.out == ""
    assert captured.err == _NEWS_USAGE_ERROR


@pytest.mark.parametrize("target", ["parser", "root"])
def test_interrupt_while_parsing_or_resolving_the_root_is_reported_without_traceback(
        tmp_path, monkeypatch, capsys, target):
    root = make_root(tmp_path)

    def boom(*a, **k):
        raise KeyboardInterrupt

    if target == "parser":
        monkeypatch.setattr(entry._OpsOptionParser, "parse_args", boom)
    else:
        monkeypatch.setattr(entry, "resolve_root", boom)
    rc, out, err = run_news(capsys, monkeypatch, root, "list")
    assert (rc, out, err) == (130, "", "中断しました。\n")


def test_interrupt_outside_news_is_not_swallowed(tmp_path, monkeypatch):
    monkeypatch.setattr(entry, "resolve_root", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        entry.main(["--root", str(tmp_path), "status"])
