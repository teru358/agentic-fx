from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_fx import service
from agentic_fx.setup.wizard import run_wizard
from agentic_fx.store.setup_marker import (
    EXIT_SETUP_IN_PROGRESS, read_setup_marker, setup_marker_path, setup_marker_present,
)
from agentic_fx.store.setup_record import read_setup_record

from tests.setup.test_wizard_rerun import installed_root
from tests.setup.wizard_support import (
    ScriptedWizardIO, fresh_responder, make_deps, make_root,
)


class Crash(Exception):
    pass


def crash_at(point, *, nth=1):
    seen = {"n": 0}

    def hook(name):
        if name == point:
            seen["n"] += 1
            if seen["n"] == nth:
                raise Crash(name)
    return hook


def db_rows(root):
    path = root / "data" / "agentic.db"
    if not path.exists():
        return None
    conn = sqlite3.connect(path)
    try:
        return {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
                for table in ("news_sources", "improve_waves")}
    finally:
        conn.close()


def state_of(root):
    text = lambda p: p.read_text() if p.exists() else None  # noqa: E731
    return {
        "settings": text(root / "config" / "settings.yaml"),
        "record": text(root / "data" / "state" / "setup.json"),
        "app_state": text(root / "data" / "state" / "app_state.json"),
        "db": db_rows(root),
    }


def initialized(root):
    path = root / "data" / "state" / "app_state.json"
    return path.exists() and json.loads(path.read_text()).get("initialized") is True


def one_shot(tmp_path):
    root = make_root(tmp_path / "reference")
    deps, *_ = make_deps(tmp_path / "reference")
    assert run_wizard(root, ScriptedWizardIO(responder=fresh_responder()), deps) == 0
    return state_of(root)


POINTS = [
    # point, marker present, settings written, db exists, news rows, record, initialized
    ("before_commit_marker", False, False, False, None, False, False),
    ("before_settings_replace", True, False, False, None, False, False),
    ("after_settings_replace", True, True, False, None, False, False),
    ("after_init_db", True, True, True, 0, False, False),
    ("before_seed_commit", True, True, True, 0, False, False),
    ("after_setup_record_write", True, True, True, "all", True, False),
    ("after_app_state_write", True, True, True, "all", True, True),
    ("before_setup_marker_remove", True, True, True, "all", True, True),
]


@pytest.mark.parametrize("point,marker,settings,db,news,record,init", POINTS)
def test_a_crash_at_every_commit_point_leaves_the_documented_state_and_the_rerun_converges(
        tmp_path, point, marker, settings, db, news, record, init):
    reference = one_shot(tmp_path)
    expected_news = len(reference["db"]["news_sources"])
    root = make_root(tmp_path / "crashed")
    deps, *_ = make_deps(tmp_path / "crashed", hook=crash_at(point))
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) != 0
    assert "確定の途中で失敗しました" in io.text and "afx init" in io.text
    assert setup_marker_present(root) is marker
    assert (root / "config" / "settings.yaml").exists() is settings
    assert (root / "data" / "agentic.db").exists() is db
    if news is not None:
        rows = db_rows(root)
        assert len(rows["news_sources"]) == (expected_news if news == "all" else news)
        assert rows["improve_waves"] == ([] if point in ("after_init_db", "before_seed_commit")
                                          else reference["db"]["improve_waves"])
    assert (read_setup_record(root).state == "ok") is record
    assert initialized(root) is init
    if marker:
        content = read_setup_marker(root)
        assert content.phase == "commit" and content.settings_sha256 and content.started_at

    # the service refuses to start while the marker stands, before reading any setting
    if marker:
        with patch.object(service, "load_settings") as load, patch.object(service, "ensure_initialized") as ensure:
            assert service.run_service(root, daemon=True) == EXIT_SETUP_IN_PROGRESS
        load.assert_not_called()
        ensure.assert_not_called()

    # the same answers converge
    deps2, *_ = make_deps(tmp_path / "crashed")
    io2 = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io2, deps2) == 0, io2.text
    assert not setup_marker_present(root)
    assert state_of(root) == reference
    assert list((root / "config").glob("settings.yaml.bak-*")) == []
    if point in ("after_app_state_write", "before_setup_marker_remove"):
        assert "消しますか" in io2.asked[0] and io2.asked_with("価格源") == []


def test_the_first_backup_is_kept_when_a_commit_is_resumed(tmp_path):
    root, before = installed_root(tmp_path)
    rules = fresh_responder(("表示タイムゾーン (例", "UTC"))
    deps, *_ = make_deps(tmp_path, hook=crash_at("after_settings_replace"))
    assert run_wizard(root, ScriptedWizardIO(responder=rules), deps) != 0
    backups = sorted((root / "config").glob("settings.yaml.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == before
    deps2, *_ = make_deps(tmp_path)
    assert run_wizard(root, ScriptedWizardIO(responder=rules), deps2) == 0
    resumed_backups = sorted((root / "config").glob("settings.yaml.bak-*"))
    assert resumed_backups[:1] == backups
    assert backups[0].read_text() == before
    assert "display_timezone: UTC" in (root / "config" / "settings.yaml").read_text()


def test_a_backup_that_cannot_be_written_stops_before_the_settings_are_replaced(tmp_path, monkeypatch):
    root, before = installed_root(tmp_path)
    import agentic_fx.setup.wizard as wizard
    real = wizard.atomic_write_settings

    def refuse_backup(path, text):
        if ".bak-" in path.name:
            raise OSError("no space")
        real(path, text)

    monkeypatch.setattr(wizard, "atomic_write_settings", refuse_backup)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("表示タイムゾーン (例", "UTC")))
    assert run_wizard(root, io, deps) == 1
    assert (root / "config" / "settings.yaml").read_text() == before
    assert setup_marker_present(root)
    assert "確定の途中で失敗しました" in io.text


@pytest.mark.parametrize("content", [b"\xff\xfe", b"phase=commit\n", b"phase=commit\nsettings_sha256=" + b"0" * 64 + b"\n"])
def test_an_unreadable_or_foreign_marker_is_not_treated_as_a_continuation(tmp_path, content):
    root, before = installed_root(tmp_path)
    marker = setup_marker_path(root)
    marker.parent.mkdir(parents=True)
    marker.write_bytes(content)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("表示タイムゾーン (例", "UTC")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert io.asked_with("消しますか") == [] and io.asked_with("表示タイムゾーン (例")
    assert len(list((root / "config").glob("settings.yaml.bak-*"))) == 1
    assert not setup_marker_present(root)


def test_an_incomplete_handoff_restarts_collection_and_keeps_its_marker_when_stopped(tmp_path):
    root = make_root(tmp_path)
    marker = setup_marker_path(root)
    marker.parent.mkdir(parents=True)
    marker.write_text("phase=handoff\n")
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("表示タイムゾーン (例", "q")))

    assert run_wizard(root, io, deps) == 1

    assert io.asked_with("表示タイムゾーン (例")
    assert "前回の引き渡しの記録が不完全です" in io.text
    assert setup_marker_present(root)


def test_a_completed_commit_whose_marker_remains_can_be_cleared_with_zero_questions_before_the_tail(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path, hook=crash_at("before_setup_marker_remove"))
    assert run_wizard(root, ScriptedWizardIO(responder=fresh_responder()), deps) != 0
    deps2, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("消しますか", "n")))
    assert run_wizard(root, io, deps2) == 1
    assert setup_marker_present(root)
    io2 = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io2, deps2) == 0
    assert not setup_marker_present(root)
    assert io2.asked[0].startswith("消しますか")
    assert io2.asked_with("読みました") == [] and io2.asked_with("判断足") == []


def test_every_durable_write_syncs_the_file_and_its_directory(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    synced: list[str] = []
    real = os.fsync

    def spy(fd):
        synced.append(os.readlink(f"/proc/self/fd/{fd}"))
        real(fd)

    monkeypatch.setattr(os, "fsync", spy)
    deps, *_ = make_deps(tmp_path)
    assert run_wizard(root, ScriptedWizardIO(responder=fresh_responder()), deps) == 0
    resolved = root.resolve()
    names = [Path(p).name for p in synced]
    assert any(n.startswith(".settings.yaml.") for n in names)
    assert str(resolved / "config") in synced
    assert any(n.startswith(".setup.json.") for n in names)
    assert any(n.startswith(".setup_in_progress.") for n in names)
    assert synced.count(str(resolved / "data" / "state")) >= 3


def test_the_marker_is_placed_before_anything_else_is_written_and_removed_last(tmp_path):
    root = make_root(tmp_path)
    order: list[str] = []

    def hook(name):
        if name in ("before_commit_marker", "before_settings_replace", "after_init_db",
                    "after_setup_record_write", "after_app_state_write", "before_setup_marker_remove"):
            order.append(f"{name}:marker={setup_marker_present(root)}:settings={(root/'config'/'settings.yaml').exists()}")

    deps, *_ = make_deps(tmp_path, hook=hook)
    assert run_wizard(root, ScriptedWizardIO(responder=fresh_responder()), deps) == 0
    assert order == [
        "before_commit_marker:marker=False:settings=False",
        "before_settings_replace:marker=True:settings=False",
        "after_init_db:marker=True:settings=True",
        "after_setup_record_write:marker=True:settings=True",
        "after_app_state_write:marker=True:settings=True",
        "before_setup_marker_remove:marker=True:settings=True"]
    assert not setup_marker_present(root)


def test_a_rerun_does_not_reset_mode_autopilot_or_the_kill_switch(tmp_path):
    root, _ = installed_root(tmp_path)
    store = service._state_store(root)
    store.update(initialized=True, mode=service.Mode.TRADING, autopilot=True)
    store.update(kill_switch_latched=True)
    deps, *_ = make_deps(tmp_path)
    assert run_wizard(root, ScriptedWizardIO(responder=fresh_responder()), deps) == 0
    state = store.load()
    assert state.mode is service.Mode.TRADING and state.autopilot is True
    assert state.kill_switch_latched is True


def test_a_first_run_starts_in_learning_mode_with_autopilot_off(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    assert run_wizard(root, ScriptedWizardIO(responder=fresh_responder()), deps) == 0
    state = service._state_store(root).load()
    assert state.initialized and state.mode is service.Mode.LEARNING and state.autopilot is False


def test_the_marker_is_left_when_the_database_transaction_fails(tmp_path, monkeypatch):
    import agentic_fx.setup.wizard as wizard

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(wizard, "seed_default_sources", broken)
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 1
    assert setup_marker_present(root)
    assert db_rows(root)["news_sources"] == []
    assert "確定の途中で失敗しました" in io.text


def test_a_resumed_commit_with_different_answers_still_keeps_only_the_first_backup(tmp_path):
    root, before = installed_root(tmp_path)
    deps, *_ = make_deps(tmp_path, hook=crash_at("after_settings_replace"))
    assert run_wizard(root, ScriptedWizardIO(responder=fresh_responder(("表示タイムゾーン (例", "UTC"))), deps) != 0
    backups = sorted((root / "config").glob("settings.yaml.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == before
    deps2, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("表示タイムゾーン (例", "America/New_York")))
    assert run_wizard(root, io, deps2) == 0, io.text
    assert sorted((root / "config").glob("settings.yaml.bak-*"))[:1] == backups
    assert backups[0].read_text() == before
    assert "display_timezone: America/New_York" in (root / "config" / "settings.yaml").read_text()


def test_a_changed_file_that_is_not_the_one_the_marker_names_gets_its_own_backup(tmp_path):
    root, before = installed_root(tmp_path)
    deps, *_ = make_deps(tmp_path, hook=crash_at("after_settings_replace"))
    assert run_wizard(root, ScriptedWizardIO(responder=fresh_responder(("表示タイムゾーン (例", "UTC"))), deps) != 0
    edited = (root / "config" / "settings.yaml").read_text().replace("display_timezone: UTC", "display_timezone: Europe/London")
    (root / "config" / "settings.yaml").write_text(edited)
    deps2, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("表示タイムゾーン (例", "America/New_York")))
    assert run_wizard(root, io, deps2) == 0, io.text
    backups = sorted((root / "config").glob("settings.yaml.bak-*"))
    assert len(backups) == 2 and edited in [path.read_text() for path in backups]


@pytest.mark.parametrize("point,fragment", [
    ("before_settings_replace", "確定の途中で止まりました"),
    ("after_seed_commit", "確定の途中で止まりました")])
def test_ctrl_c_while_committing_reports_what_remains_and_leaves_the_marker(tmp_path, point, fragment):
    class Interrupt(KeyboardInterrupt):
        pass

    def hook(name):
        if name == point:
            raise Interrupt()

    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path, hook=hook)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 130
    assert fragment in io.text and "afx init" in io.text
    assert setup_marker_present(root)
    deps2, *_ = make_deps(tmp_path)
    assert run_wizard(root, ScriptedWizardIO(responder=fresh_responder()), deps2) == 0
    assert not setup_marker_present(root)


def test_ctrl_c_after_the_commit_keeps_what_was_written(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)

    def responder(prompt):
        if "方針文" in prompt:
            raise KeyboardInterrupt
        return fresh_responder()(prompt)

    io = ScriptedWizardIO(responder=responder)
    assert run_wizard(root, io, deps) == 130
    assert "ここまでの確定は残っています" in io.text
    assert not setup_marker_present(root) and (root / "config" / "settings.yaml").exists()
