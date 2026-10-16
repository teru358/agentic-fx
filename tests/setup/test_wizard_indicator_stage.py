from __future__ import annotations

from datetime import datetime, timezone

from agentic_fx.setup import indicators, wizard
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store.db import connect, init_db

from tests.setup.wizard_support import ScriptedWizardIO


NOW = datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc)


def test_indicator_io_adapter_preserves_defaults_and_forwards_output():
    io = ScriptedWizardIO(["", "chosen"])
    adapter = wizard._IndicatorIOAdapter(io)

    assert isinstance(adapter, indicators.IndicatorIO)
    assert adapter.ask("first", "default") == "default"
    assert adapter.ask("second", "default") == "chosen"
    adapter.stdout("output")

    assert io.asked == ["first [default]: ", "second [default]: "]
    assert io.out == ["output"]


def test_default_indicator_stage_counts_adopted_and_already_deployed_names(tmp_path, monkeypatch):
    root = tmp_path
    source = root / "docs" / "examples" / "plugins" / "sma"
    source.mkdir(parents=True)
    (source / "plugin.py").write_text("x = 1\n", encoding="utf-8")
    (source / "config.yaml").write_text("kind: indicator\n", encoding="utf-8")
    (root / "plugins" / "rsi").mkdir(parents=True)
    db_path = root / "data" / "agentic.db"
    conn = connect(db_path)
    init_db(conn)
    conn.close()
    calls = []

    def bless(conn, *, name, human_dir, settings, now, decided_by):
        calls.append((name, human_dir, settings, now, decided_by))
        approval_id = approvals_store.create(conn, "plugin", {"name": name}, now)
        conn.execute("UPDATE approval_requests SET status=? WHERE id=?", ("approved", approval_id))
        conn.commit()
        return approval_id

    def run_stage(io, root, conn, *, settings, landlock_available, now):
        return indicators.run_indicator_stage(
            io, root, conn, settings=settings, landlock_available=landlock_available,
            bless=bless, now=now)

    monkeypatch.setattr(wizard, "run_indicator_stage", run_stage)
    deps = wizard.WizardDeps(landlock_available=lambda: True)
    context = wizard.WizardContext(
        root, object(), ScriptedWizardIO(["", "y"]), deps, None, NOW)

    assert wizard.default_indicator_stage(context) == wizard.IndicatorOutcome(2)
    assert [(name, decided_by) for name, _, _, _, decided_by in calls] == [("sma", "human_cli")]


def test_default_indicator_stage_skips_a_missing_database(tmp_path):
    context = wizard.WizardContext(
        tmp_path, object(), ScriptedWizardIO(), wizard.WizardDeps(), None, NOW)

    assert wizard.default_indicator_stage(context) == wizard.IndicatorOutcome(0)


def _asking_stage(ctx):
    wizard._IndicatorIOAdapter(ctx.io).ask("標準指標を登録します。よろしいですか [y/N]: ")
    return wizard.IndicatorOutcome(1)


def _run_interrupted_at_the_indicator_question(tmp_path, error):
    from tests.setup.wizard_support import fresh_responder, make_deps, make_root

    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path, indicator_stage=_asking_stage)
    rules = fresh_responder()

    def responder(prompt):
        if "標準指標を登録します" in prompt:
            raise error
        return rules(prompt)

    io = ScriptedWizardIO(responder=responder)
    return wizard.run_wizard(root, io, deps), io, root


def test_eof_at_the_indicator_question_stops_with_the_after_commit_guidance(tmp_path):
    rc, io, root = _run_interrupted_at_the_indicator_question(tmp_path, EOFError())

    assert rc == 1
    assert "afx init" in io.out[-1] and "確定は残っています" in io.out[-1]
    assert "Traceback" not in io.text


def test_ctrl_c_at_the_indicator_question_stops_with_130(tmp_path):
    rc, io, root = _run_interrupted_at_the_indicator_question(tmp_path, KeyboardInterrupt())

    assert rc == 130
    assert "確定は残っています" in io.out[-1]
