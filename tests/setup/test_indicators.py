from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.ops_setup.interactive import IO, ConsoleIO
from agentic_fx.plugin import switch as plugin_switch
from agentic_fx.plugin.history_git import HistoryGitError
from agentic_fx.plugin.sandbox import SandboxError
from agentic_fx.setup import indicators as ind
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import plugin_switch_journal as journal_store
from agentic_fx.store import setup_record
from agentic_fx.store.db import init_db
from agentic_fx.store.setup_record import SetupRecord

NOW = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)
NAMES = [n for n, _ in ind.STANDARD_INDICATORS]


class ScriptIO:
    def __init__(self, answers):
        self.answers = list(answers)
        self.questions = []
        self.out = []

    def ask(self, label, default=None):
        self.questions.append(label)
        assert self.answers, f"unexpected question: {label}"
        answer = self.answers.pop(0)
        return answer if answer else default

    def stdout(self, text):
        self.out.append(text)

    @property
    def text(self):
        return "\n".join(self.out)


class Spy:
    """Stands in for the bless path; records approvals like the real one does."""

    def __init__(self, failures=None, status="approved"):
        self.calls = []
        self.failures = failures or {}
        self.seen_dirs = []
        self.status = status

    def __call__(self, conn, *, name, human_dir, settings, now, decided_by, **kw):
        self.calls.append((name, decided_by))
        self.seen_dirs.append((human_dir, (human_dir / "plugin.py").exists()))
        if name in self.failures:
            raise self.failures[name]
        approval_id = approvals_store.create(conn, "plugin", {"name": name}, now)
        conn.execute("UPDATE approval_requests SET status=? WHERE id=?", (self.status, approval_id))
        conn.commit()
        return approval_id


def open_journal(conn, name, approval_id):
    approval_id = approvals_store.create(conn, "plugin", {"name": name}, NOW) if approval_id is None else approval_id
    journal_store.insert(conn, kind="bless", approval_id=approval_id, name=name, old_kind="absent",
                         old_target=None, temp_path="t", new_target="x", switch_required=True, actor="human_cli", now=NOW)
    return approval_id


@pytest.fixture
def root(tmp_path):
    for name in NAMES:
        d = tmp_path / "docs" / "examples" / "plugins" / name
        d.mkdir(parents=True)
        (d / "plugin.py").write_text("x = 1\n")
        (d / "config.yaml").write_text("kind: indicator\n")
    (tmp_path / "plugins").mkdir()
    return tmp_path


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db(c)
    yield c
    c.close()


_EXAMPLES = {}


@pytest.fixture(autouse=True)
def _examples_dir(tmp_path_factory):
    path = tmp_path_factory.mktemp("examples")
    for name in NAMES:
        (path / name).mkdir()
    _EXAMPLES["dir"] = path


def rows(deployed=()):
    return ind.list_standard_indicators(_EXAMPLES["dir"], deployed)


def test_nine_rows_fixed_order_and_deployed_flag():
    result = rows({"rsi"})
    assert [r.name for r in result] == ["sma", "ema", "rsi", "macd", "bollinger", "atr", "adx", "stochastic", "ichimoku"]
    assert all(r.summary for r in result)
    assert [r.name for r in result if r.deployed] == ["rsi"]


def test_declining_standard_registration_selects_nothing():
    io = ScriptIO(["n"])
    assert ind.ask_selection(io, rows()) == []
    assert "[Y/n]" in io.questions[-1] and "human_cli" not in io.questions[-1]


def test_empty_standard_registration_answer_selects_all():
    assert ind.ask_selection(ScriptIO([""]), rows()) == NAMES


def test_yes_selects_all_standard_indicators():
    assert ind.ask_selection(ScriptIO(["y"]), rows()) == NAMES


def test_invalid_confirmation_answers_repeat_without_recursion_then_accept_yes():
    io = ScriptIO(["maybe", "?", "y"])

    assert ind.ask_selection(io, rows()) == NAMES
    assert len(io.questions) == 3
    assert inspect.getsource(ind.ask_selection).count("ask_selection(") == 1


@pytest.mark.parametrize("answer,removed", [
    ("2", {"ema"}), ("1,3", {"sma", "rsi"}), ("1 3  9", {"sma", "rsi", "ichimoku"}),
    ("2-4", {"ema", "rsi", "macd"}),
])
def test_exclusion_numbers_are_interpreted(answer, removed):
    assert ind.ask_selection(ScriptIO(["e", answer]), rows()) == [n for n in NAMES if n not in removed]


@pytest.mark.parametrize("bad", ["0", "10", "x", "3-2", "1,,x", "-1"])
def test_out_of_range_or_garbage_asks_again(bad):
    io = ScriptIO(["e", bad, "2"])
    assert ind.ask_selection(io, rows()) == [n for n in NAMES if n != "ema"]
    assert "1〜9" in io.text


def test_deployed_is_excluded_by_default_and_warned():
    io = ScriptIO(["y"])
    chosen = ind.ask_selection(io, rows({"rsi"}))
    assert "rsi" not in chosen and len(chosen) == 8
    assert "配備済み・既定で外れています" in io.text
    assert "標準指標 8 本を登録" in io.questions[-1]


def test_deployed_can_be_brought_back_by_number():
    chosen = ind.ask_selection(ScriptIO(["e", "3"]), rows({"rsi"}))
    assert "rsi" in chosen and len(chosen) == 9


def test_everything_deployed_emits_one_line_without_a_question():
    io = ScriptIO([])
    assert ind.ask_selection(io, rows(NAMES)) == []
    assert io.out == [ind._INTRO_TEXT, "標準指標 9 本は配備済みです (変更なし)"]
    assert io.questions == []


def test_everything_excluded_asks_no_confirmation():
    io = ScriptIO(["e", "1-9"])
    assert ind.ask_selection(io, rows()) == []


def test_adopt_calls_bless_once_per_name_with_human_path_and_cleans_up(root, conn):
    spy = Spy()
    io = ScriptIO([])
    out = ind.adopt(io, root, conn, ["sma", "ema"], settings=object(), bless=spy, now=NOW)
    assert spy.calls == [("sma", "human_cli"), ("ema", "human_cli")]
    assert all(d == root / "plugins" / "_human" / n and existed
               for (d, existed), n in zip(spy.seen_dirs, ["sma", "ema"]))
    assert [(o.name, o.result) for o in out] == [("sma", "adopted"), ("ema", "adopted")]
    assert not (root / "plugins" / "_human" / "sma").exists()
    assert not (root / "plugins" / "sma").exists()  # nothing copied directly
    assert "1/2 sma: 審査中 (pytest、最長 300 秒)" in io.text
    assert "2/2 ema: 審査中 (pytest、最長 300 秒)" in io.text
    assert "次回のサービス起動" in io.text


def test_gate_failure_does_not_stop_the_rest_and_text_is_fixed(root, conn):
    spy = Spy({"ema": plugin_switch.GateCheckError("pytest failed: secret detail", kind="pytest")})
    io = ScriptIO([])
    out = ind.adopt(io, root, conn, ["sma", "ema", "rsi"], settings=object(), bless=spy, now=NOW)
    assert [o.result for o in out] == ["adopted", "gate_failed", "adopted"]
    assert [c[0] for c in spy.calls] == ["sma", "ema", "rsi"]
    assert ("ema: 審査に通りませんでした (pytest 不合格)。何も配備されていません。"
            "後で `uv run afx plugin bless ema --from _human` でやり直せます。") in io.text
    assert "secret detail" not in io.text
    assert not (root / "plugins" / "_human" / "ema").exists()
    assert not (root / "plugins" / "_human" / "rsi").exists()


def test_ast_gate_failure_names_ast(root, conn):
    cause = SandboxError("plugin.py: use of name 'open' is not allowed")
    error = ValueError("plugin 'ema': detail")
    error.__cause__ = cause
    io = ScriptIO([])
    ind.adopt(io, root, conn, ["ema"], settings=object(), bless=Spy({"ema": error}), now=NOW)
    assert "ema: 審査に通りませんでした (ast 不合格)。" in io.text
    assert "open" not in io.text


def test_hash_gate_failure_names_hash(root, conn):
    io = ScriptIO([])
    ind.adopt(io, root, conn, ["ema"], settings=object(),
              bless=Spy({"ema": plugin_switch.GateCheckError("x", kind="hash")}), now=NOW)
    assert "(hash 不合格)" in io.text


def test_unknown_gate_kind_has_no_parenthesis(root, conn):
    io = ScriptIO([])
    ind.adopt(io, root, conn, ["ema"], settings=object(),
              bless=Spy({"ema": plugin_switch.GateCheckError("x", kind="other")}), now=NOW)
    assert "ema: 審査に通りませんでした。何も配備されていません。" in io.text


def test_gate_failure_removes_candidate_so_a_rerun_blesses_again(root, conn):
    ind.adopt(ScriptIO([]), root, conn, ["ema"], settings=object(),
              bless=Spy({"ema": plugin_switch.GateCheckError("x", kind="pytest")}), now=NOW)
    spy = Spy()
    out = ind.adopt(ScriptIO([]), root, conn, ["ema"], settings=object(), bless=spy, now=NOW)
    assert spy.calls == [("ema", "human_cli")] and out[0].result == "adopted"


@pytest.mark.parametrize("error", [OSError("disk"), sqlite3.OperationalError("locked"), RuntimeError("boom")])
def test_non_gate_exception_is_an_error_keeping_the_candidate_and_the_loop_continues(root, conn, error):
    spy = Spy({"sma": error})
    io = ScriptIO([])
    out = ind.adopt(io, root, conn, ["sma", "ema"], settings=object(), bless=spy, now=NOW)
    assert [(o.name, o.result) for o in out] == [("sma", "error"), ("ema", "adopted")]
    assert out[0].error_type == type(error).__name__
    assert f"sma: 採用処理でエラー ({type(error).__name__})。候補は plugins/_human に残っています" in io.text
    assert "何も配備されていません" not in io.text
    assert (root / "plugins" / "_human" / "sma").exists()


def test_lookup_failure_after_a_failed_bless_is_an_error_not_gate_failed(root, conn, monkeypatch):
    def broken(conn, name):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ind.journal_store, "get_open_by_name", broken)
    io = ScriptIO([])
    out = ind.adopt(io, root, conn, ["sma"], settings=object(),
                    bless=Spy({"sma": plugin_switch.GateCheckError("x", kind="pytest")}), now=NOW)
    assert out[0].result == "error"
    assert "何も配備されていません" not in io.text
    assert (root / "plugins" / "_human" / "sma").exists()


def test_keyboard_interrupt_reports_what_is_left_and_propagates(root, conn):
    io = ScriptIO([])
    with pytest.raises(KeyboardInterrupt):
        ind.adopt(io, root, conn, ["sma"], settings=object(),
                  bless=Spy({"sma": KeyboardInterrupt()}), now=NOW)
    assert "sma: 中断しました。候補は plugins/_human/sma に残っています。" in io.text
    assert (root / "plugins" / "_human" / "sma").exists()


def test_progress_seconds_follow_the_gate_timeout_setting(root, conn):
    from types import SimpleNamespace
    settings = SimpleNamespace(plugin=SimpleNamespace(pytest_timeout_sec=45.0))
    io = ScriptIO([])
    ind.adopt(io, root, conn, ["sma"], settings=settings, bless=Spy(), now=NOW)
    assert "最長 45 秒" in io.text


def test_legacy_plain_present_is_reported_with_the_manual_procedure(root, conn):
    class Legacy(Spy):
        def __call__(self, conn, *, name, **kw):
            approval_id = approvals_store.create(conn, "plugin", {"name": name}, NOW)
            approvals_store.set_reason(conn, approval_id, "legacy_plain_present", commit=True)
            return approval_id

    io = ScriptIO([])
    out = ind.adopt(io, root, conn, ["sma"], settings=object(), bless=Legacy(), now=NOW)
    assert out[0].result == "legacy_present" and out[0].reason_code == "legacy_plain_present"
    assert "sma は旧形式で配備済みです。切り替えは手順書 (0) を参照" in io.text
    assert "approval retry" not in io.text
    assert ind.indicators_record_values(out) == {"installed": 0}


def test_missing_example_is_dropped_from_the_list_and_named(root, conn):
    import shutil
    shutil.rmtree(root / "docs" / "examples" / "plugins" / "atr")
    spy = Spy()
    io = ScriptIO(["", "y"])
    ind.run_indicator_stage(io, root, conn, settings=object(), landlock_available=lambda: True,
                            bless=spy, now=NOW)
    assert "atr: 雛形が見つかりません。" in io.text
    assert "atr" not in [c[0] for c in spy.calls] and len(spy.calls) == 8
    assert "6. atr" not in io.text


def test_failure_after_the_journal_exists_is_stalled_not_gate_failed(root, conn):
    approval_id = open_journal(conn, "macd", None)

    def bless(conn, **kw):
        raise ValueError("journal row is inconsistent")

    io = ScriptIO([])
    out = ind.adopt(io, root, conn, ["macd"], settings=object(), bless=bless, now=NOW)
    assert [(o.result, o.approval_id) for o in out] == [("stalled", approval_id)]
    assert f"approval retry {approval_id}" in io.text
    assert "何も配備されていません" not in io.text
    assert (root / "plugins" / "_human" / "macd").exists()


def test_stalled_after_gate_prints_retry_instruction(root, conn):
    open_journal(conn, "macd", 12)
    err = plugin_switch.UnresolvedJournalError("plugin 'macd': (op_id=3, approval_id=12) blocks")
    spy = Spy({"macd": err})
    io = ScriptIO([])
    out = ind.adopt(io, root, conn, ["macd", "atr"], settings=object(), bless=spy, now=NOW)
    assert [(o.result, o.approval_id) for o in out][0] == ("stalled", 12)
    assert out[1].result == "adopted"
    assert ("承認 #12 (macd) が途中で止まりました。サービス起動後に "
            "`afx ctl --as approver approval retry 12` を実行してください (端末での確認があります)。") in io.text


def test_approval_id_comes_from_the_journal_not_the_error_text(root, conn):
    open_journal(conn, "macd", 12)
    err = HistoryGitError("record failed approval_id=999")
    io = ScriptIO([])
    out = ind.adopt(io, root, conn, ["macd"], settings=object(), bless=Spy({"macd": err}), now=NOW)
    assert out[0].approval_id == 12
    assert "#12" in io.text and "999" not in io.text


def test_error_text_alone_never_yields_an_approval_id(root, conn):
    err = HistoryGitError("record failed approval_id=999")
    out = ind.adopt(ScriptIO([]), root, conn, ["macd"], settings=object(),
                    bless=Spy({"macd": err}), now=NOW)
    assert out[0].approval_id is None and out[0].result == "error"


def test_pending_approval_left_by_bless_is_stalled_and_not_counted(root, conn):
    spy = Spy(status="pending")
    io = ScriptIO([])
    out = ind.adopt(io, root, conn, ["sma"], settings=object(), bless=spy, now=NOW)
    assert out[0].result == "stalled" and out[0].approval_id is not None
    assert f"承認 #{out[0].approval_id} (sma)" in io.text
    assert "採用しました" not in io.text and "次回のサービス起動" not in io.text
    assert (root / "plugins" / "_human" / "sma").exists()
    assert ind.indicators_record_values(out) == {"installed": 0}


def test_plain_directory_already_deployed_is_not_reported_as_adopted(tmp_path, monkeypatch):
    from agentic_fx.config import load_settings
    from agentic_fx.store import db as db_store
    from tests.plugin.test_switch_floor_modes import (STRATEGY_PY, TEST_PY_OK, _fake_pytest_ok,
                                                      _floor_fail_gate)

    example = tmp_path / "docs" / "examples" / "plugins" / "sma"
    example.mkdir(parents=True)
    (example / "plugin.py").write_text(STRATEGY_PY)
    (example / "config.yaml").write_text("kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\nexit_mode: levels\n")
    (example / "test_plugin.py").write_text(TEST_PY_OK)
    (tmp_path / "plugins" / ".locks").mkdir(parents=True)
    (tmp_path / "plugins" / "sma").mkdir()  # an old plain directory is already deployed
    real = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(real)
    settings = load_settings(Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    monkeypatch.setattr("agentic_fx.plugin.strategy_gate.evaluate_strategy_adoption_gate", _floor_fail_gate)

    io = ScriptIO([])
    out = ind.adopt(io, tmp_path, real, ["sma"], settings=settings, now=NOW)
    assert out[0].result == "legacy_present" and out[0].approval_id is not None
    assert "採用しました" not in io.text and "approval retry" not in io.text
    assert (tmp_path / "plugins" / "_human" / "sma").exists()
    assert ind.indicators_record_values(out, deployed=["sma"]) == {"installed": 1}
    real.close()


def test_scripted_io_and_console_io_satisfy_the_stage_io():
    members = ("ask", "stdout")
    assert all(name in dir(IO) for name in members)
    assert isinstance(ScriptIO([]), ind.IndicatorIO)
    assert isinstance(ConsoleIO(), ind.IndicatorIO)


def test_existing_candidate_is_not_overwritten(root, conn):
    keep = root / "plugins" / "_human" / "sma"
    keep.mkdir(parents=True)
    (keep / "mine.txt").write_text("hand made")
    spy = Spy()
    out = ind.adopt(ScriptIO([]), root, conn, ["sma"], settings=object(), bless=spy, now=NOW)
    assert spy.calls == [] and out[0].result == "skipped"
    assert (keep / "mine.txt").read_text() == "hand made"


def test_without_landlock_nothing_is_blessed_or_asked(root, conn):
    spy = Spy()
    io = ScriptIO([])
    out = ind.run_indicator_stage(io, root, conn, settings=object(), landlock_available=lambda: False,
                                  bless=spy, now=NOW)
    assert out == [] and spy.calls == [] and io.questions == []
    assert "Landlock" in io.text


def test_stage_end_to_end_with_deployed_default_exclusion(root, conn):
    (root / "plugins" / "rsi").mkdir()
    spy = Spy()
    out = ind.run_indicator_stage(ScriptIO(["e", "1"]), root, conn, settings=object(),
                                  landlock_available=lambda: True, bless=spy, now=NOW)
    assert [c[0] for c in spy.calls] == ["ema", "macd", "bollinger", "atr", "adx", "stochastic", "ichimoku"]
    assert len(out) == 7


def test_stage_decline_blesses_nothing(root, conn):
    spy = Spy()
    out = ind.run_indicator_stage(ScriptIO(["n"]), root, conn, settings=object(),
                                  landlock_available=lambda: True, bless=spy, now=NOW)
    assert out == [] and spy.calls == []


def test_record_values_count_only_adopted_and_pass_validation():
    outcomes = [ind.AdoptOutcome("sma", "adopted", 1), ind.AdoptOutcome("ema", "gate_failed", None, "gate_rejected"),
                ind.AdoptOutcome("rsi", "stalled", 3, "x"), ind.AdoptOutcome("macd", "skipped", None, "y")]
    values = ind.indicators_record_values(outcomes)
    assert values == {"installed": 1}
    assert ind.indicators_record_values(outcomes, deployed=["sma", "rsi", "custom"]) == {"installed": 2}
    assert ind.indicators_record_values([], deployed=NAMES[:5]) == {"installed": 5}
    data = {
        "version": 1, "completed_at": "2026-10-12T00:00:00Z", "mode": "interactive",
        "settings_schema_version": 1,
        "price": {"result": "ok"}, "llm_trade": {"result": "ok"}, "llm_improve": {"result": "ok"},
        "bridge_plan": "connected", "history": {"source": "mt5"}, "unit": "installed",
        "indicators": values, "api": {"enabled": True, "result": "ok"}, "ops": {},
        "isolation": {"admitted": "yes"}, "linger": {"state": "already_enabled"},
        "start": {"result": "started"},
    }
    assert setup_record._valid(data)
    assert setup_record._valid({**data, "indicators": ind.indicators_record_values([])})
