"""Offline status: per-source output, fault isolation and the zero-write contract."""
from __future__ import annotations

import os
import sqlite3
from types import SimpleNamespace

import pytest

from agentic_fx import status
from agentic_fx.ops.contracts import Principal
from agentic_fx.setup.pending_facts import collect_pending_facts
from agentic_fx.setup.pending_steps import pending_steps, render_pending_step
from agentic_fx.store.setup_record import SetupRecord
from tests.status_support import (
    NOTE, NOW, VALID_STAGE, add_sidecars, held_by_other_process, make_db, make_run, manifest, setup_data,
    state_json, touch_lock, write_activity, write_enroll_marker, write_key, write_settings, write_setup,
    write_setup_marker, write_stage, write_state,
)


@pytest.fixture(autouse=True)
def unit_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "default_unit_dir", lambda: tmp_path / "unit")
    return tmp_path / "unit"


def full_root(tmp_path, *, setup="ok", settings="ok", db=True):
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    touch_lock(root)
    write_setup(root, setup)
    write_settings(root, settings)
    write_state(root, state_json())
    write_activity(root, "2026-10-25T00:00:00Z\tmission\tstarted\tsummary text")
    if db:
        make_db(root, approval=2)
    return root


def run_status(root, capsys, run=None):
    rc = status.main(root, run=run or make_run())
    captured = capsys.readouterr()
    assert captured.err == ""
    return rc, captured.out


def fields(out):
    result = {}
    for line in out.splitlines():
        if ": " in line and not line.startswith("  "):
            key, value = line.split(": ", 1)
            result[key] = value
    return result


# setup x settings ---------------------------------------------------------

@pytest.mark.parametrize("settings", ["ok", "missing", "invalid"])
@pytest.mark.parametrize("setup", ["ok", "missing", "corrupt"])
def test_setup_and_settings_vocabulary_do_not_disturb_other_sources(tmp_path, capsys, setup, settings):
    root = full_root(tmp_path, setup=setup, settings=settings)
    rc, out = run_status(root, capsys)
    values = fields(out)

    assert rc == 0
    assert values["setup"] == setup
    assert values["settings"] == settings
    assert values["instance"] == "free"
    assert values["state"] == "mode=learning kill_switch=clear"
    assert values["database"] == "ok"
    assert values["recent mission"].startswith("#1 ")
    assert values["pending approvals"] == "2"
    assert values["plugin deploy ops"] == "none"
    assert values["recent activity"] == "2026-10-25T00:00:00Z mission started"
    assert values["ops"] == "bot=missing web=missing notify=missing marker=none"
    steps = [line for line in out.splitlines() if line.startswith("  - ")]
    assert values["pending steps"] == str(len(steps))
    if setup != "ok":
        assert steps
    assert out.splitlines().count(NOTE) == 1
    expected_next = ("afx init" if setup != "ok" else "afx check" if settings != "ok" else "afx ctl status")
    assert values["next"] == expected_next


def test_next_prefers_start_only_for_an_installed_unit_on_a_free_instance(tmp_path, capsys, unit_dir):
    root = full_root(tmp_path)
    unit_dir.mkdir()
    (unit_dir / "afx.service").write_text("[Unit]\n", encoding="utf-8")
    assert fields(run_status(root, capsys)[1])["next"] == "systemctl --user start afx.service"
    os.unlink(root / "data" / "instance.lock")
    assert fields(run_status(root, capsys)[1])["next"] == "afx ctl status"


def test_a_missing_everything_root_shows_every_fixed_line(tmp_path, capsys):
    root = tmp_path / "root"
    root.mkdir()
    touch_lock(root)

    rc, out = run_status(root, capsys)
    values = fields(out)

    assert rc == 0
    assert values["setup"] == "missing"
    assert values["settings"] == "missing"
    assert values["unit"] == "afx.service=missing active=inactive linger=enabled"
    assert values["ops"] == "bot=missing web=missing notify=missing marker=none"
    assert values["state"] == "mode=learning kill_switch=clear"
    assert values["database"] == "missing"
    assert values["recent mission"] == "none"
    assert values["pending approvals"] == "0"
    assert values["plugin deploy ops"] == "none"
    assert values["recent activity"] == "none"
    assert values["next"] == "afx init"


# database ---------------------------------------------------------------

def _db_bytes(root):
    return (root / "data" / "agentic.db").read_bytes()


def _user_version(root):
    conn = sqlite3.connect(f"file:{root / 'data' / 'agentic.db'}?mode=ro&immutable=1", uri=True)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def test_database_ok_reports_mission_approvals_and_deploy_ops(tmp_path, capsys):
    root = full_root(tmp_path)
    values = fields(run_status(root, capsys)[1])
    assert values["database"] == "ok"
    assert values["recent mission"] == "#1 running at 2026-10-25T00:00:00+00:00"
    assert values["pending approvals"] == "2"
    assert values["plugin deploy ops"] == "none"


def test_database_with_no_missions_says_none(tmp_path, capsys):
    root = full_root(tmp_path, db=False)
    make_db(root, mission=False, approval=0)
    values = fields(run_status(root, capsys)[1])
    assert values["recent mission"] == "none"
    assert values["pending approvals"] == "0"


def test_database_missing_does_not_create_the_file_or_its_directory_siblings(tmp_path, capsys):
    root = full_root(tmp_path, db=False)
    before = manifest(root)
    rc, out = run_status(root, capsys)
    values = fields(out)
    assert rc == 0
    assert values["database"] == "missing"
    assert (values["recent mission"], values["pending approvals"], values["plugin deploy ops"]) == (
        "none", "0", "none")
    assert manifest(root) == before
    assert not (root / "data" / "agentic.db").exists()


def test_corrupt_database_is_unavailable_and_the_other_sections_survive(tmp_path, capsys):
    root = full_root(tmp_path)
    (root / "data" / "agentic.db").write_bytes(b"\x00garbage" * 600)
    before = manifest(root)

    rc, out = run_status(root, capsys)
    values = fields(out)

    assert rc == 1
    assert values["database"] == "unavailable"
    assert values["recent mission"] == "unavailable"
    assert values["pending approvals"] == "unavailable"
    assert values["plugin deploy ops"] == "unavailable"
    assert values["setup"] == "ok" and values["settings"] == "ok"
    assert values["recent activity"] == "2026-10-25T00:00:00Z mission started"
    assert values["state"] == "mode=learning kill_switch=clear"
    assert values["unit"].startswith("afx.service=")
    assert manifest(root) == before


@pytest.mark.parametrize("dropped,affected", [
    ("missions", "recent mission"),
    ("approval_requests", "pending approvals"),
    ("plugin_deploy_ops", "plugin deploy ops"),
])
def test_a_missing_table_only_makes_its_own_line_unavailable(tmp_path, capsys, dropped, affected):
    root = full_root(tmp_path, db=False)
    make_db(root, approval=2, drop=(dropped,))
    bytes_before, version_before, tree_before = _db_bytes(root), _user_version(root), manifest(root)

    rc, out = run_status(root, capsys)
    values = fields(out)

    assert rc == 1
    assert values[affected] == "unavailable"
    assert values["database"] != "missing"
    for line in ("recent mission", "pending approvals", "plugin deploy ops"):
        if line != affected:
            assert values[line] != "unavailable", line
    assert values["setup"] == "ok"
    assert values["recent activity"] == "2026-10-25T00:00:00Z mission started"
    assert _db_bytes(root) == bytes_before
    assert _user_version(root) == version_before
    assert manifest(root) == tree_before


def test_a_schema_incomplete_database_is_not_migrated(tmp_path, capsys):
    root = full_root(tmp_path, db=False)
    path = root / "data" / "agentic.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE only_one (x INTEGER)")
    conn.commit()
    conn.close()
    before = manifest(root)

    rc, out = run_status(root, capsys)

    assert rc == 1
    assert fields(out)["recent mission"] == "unavailable"
    assert manifest(root) == before
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert tables == {"only_one"}


# unit / linger / ops / marker / activity ---------------------------------

@pytest.mark.parametrize("installed", [True, False])
@pytest.mark.parametrize("case,run_kwargs,active", [
    ("active", dict(active="active", active_rc=0), "active"),
    ("inactive", dict(active="inactive", active_rc=3), "inactive"),
    ("failed", dict(active="failed", active_rc=3), "failed"),
    ("no systemctl", dict(fail=FileNotFoundError("systemctl")), "unknown"),
])
def test_unit_line_follows_unit_file_and_is_active(tmp_path, capsys, unit_dir, installed, case, run_kwargs,
                                                    active):
    root = full_root(tmp_path)
    if installed:
        unit_dir.mkdir()
        (unit_dir / "afx.service").write_text("[Unit]\n", encoding="utf-8")
    rc, out = run_status(root, capsys, run=make_run(**run_kwargs))
    values = fields(out)
    word = "installed" if installed else "missing"
    linger = "unknown" if case == "no systemctl" else "enabled"
    assert values["unit"] == f"afx.service={word} active={active} linger={linger}"
    assert values["setup"] == "ok" and values["database"] == "ok"
    assert values["state"] == "mode=learning kill_switch=clear"


@pytest.mark.parametrize("linger,word", [("Linger=yes\n", "enabled"), ("Linger=no\n", "disabled"),
                                         ("garbage\n", "unknown")])
def test_linger_word(tmp_path, capsys, linger, word):
    root = full_root(tmp_path)
    values = fields(run_status(root, capsys, run=make_run(linger=linger))[1])
    assert values["unit"].endswith(f"linger={word}")


@pytest.mark.parametrize("feature", ["bot", "web", "notify"])
@pytest.mark.parametrize("case,text,word", [
    ("present", None, "enabled"),
    ("invalid", "{not json", "invalid"),
    ("wrong feature", '{"operation":"enable","stage":"enabled","feature":"other"}', "invalid"),
    ("missing", "", "missing"),
])
def test_ops_stage_per_feature(tmp_path, capsys, feature, case, text, word):
    root = full_root(tmp_path)
    if case != "missing":
        write_stage(root, feature, text if text is not None else VALID_STAGE % feature)
    values = fields(run_status(root, capsys)[1])
    expected = {name: "missing" for name in ("bot", "web", "notify")}
    expected[feature] = word
    assert values["ops"] == "bot=%s web=%s notify=%s marker=none" % tuple(expected[n] for n in
                                                                          ("bot", "web", "notify"))
    assert values["database"] == "ok" and values["recent activity"] != "unavailable"


@pytest.mark.parametrize("text,word", [
    ('{"feature":"web","started_at":"2026-10-25T00:00:00+00:00"}', "web"),
    ("not json", "invalid"),
])
def test_enroll_marker_none_feature_invalid(tmp_path, capsys, text, word):
    root = full_root(tmp_path)
    assert fields(run_status(root, capsys)[1])["ops"].endswith("marker=none")
    write_enroll_marker(root, text)
    assert fields(run_status(root, capsys)[1])["ops"].endswith(f"marker={word}")


@pytest.mark.parametrize("case,lines,expected,rc", [
    ("none", None, "none", 0),
    ("empty", [], "none", 0),
    ("one", ["2026-10-25T01:02:03Z\tpolicy\tpolicy_added\tfree text"], "2026-10-25T01:02:03Z policy policy_added", 0),
    ("broken", ["no tabs here"], "unavailable", 1),
])
def test_activity_line(tmp_path, capsys, case, lines, expected, rc):
    root = full_root(tmp_path)
    log = root / "logs" / "activity.log"
    if lines is None:
        os.unlink(log)
    else:
        write_activity(root, *lines)
    code, out = run_status(root, capsys)
    values = fields(out)
    assert values["recent activity"] == expected
    assert code == rc
    assert values["database"] == "ok" and values["setup"] == "ok"


def test_activity_longer_than_two_hundred_characters_is_cut(tmp_path, capsys):
    root = full_root(tmp_path)
    write_activity(root, "2026-10-25T01:02:03Z\tpolicy\t" + "word " * 120 + "\tsummary")
    value = fields(run_status(root, capsys)[1])["recent activity"]
    assert 0 < len(value) <= 200
    assert value.startswith("2026-10-25T01:02:03Z policy word word")


def test_activity_secrets_are_redacted_and_the_summary_field_is_not_shown(tmp_path, capsys):
    root = full_root(tmp_path)
    write_activity(root, "2026-10-25T01:02:03Z\tpolicy\tevent token=abcdef0123456789abcdef0123456789\tSUMMARYTEXT")
    out = run_status(root, capsys)[1]
    assert "abcdef0123456789" not in out
    assert "SUMMARYTEXT" not in out
    assert "[redacted]" in fields(out)["recent activity"]


def test_only_the_last_activity_line_is_shown(tmp_path, capsys):
    root = full_root(tmp_path)
    write_activity(root, "2026-10-25T00:00:00Z\tpolicy\told\tx", "2026-10-25T00:00:05Z\tpolicy\tnew\ty")
    assert fields(run_status(root, capsys)[1])["recent activity"] == "2026-10-25T00:00:05Z policy new"


# pending facts ---------------------------------------------------------------

class _Clock:
    def now(self):
        return NOW


def _start_pending_record():
    data = setup_data()
    data["start"] = {"result": "pending"}
    return SetupRecord(data)


def _write_pending_setup(root):
    from agentic_fx.store.setup_record import write_setup_record

    write_setup_record(root, _start_pending_record())


@pytest.mark.parametrize("value,present", [(True, False), (False, True), (None, True)])
def test_start_pending_depends_on_service_active(tmp_path, value, present):
    _write_pending_setup(tmp_path)
    facts = collect_pending_facts(tmp_path, None, None, deployed_plugins=(), outage=None, clock=_Clock(),
                                  service_active=value)
    assert facts.service_active is value
    steps = pending_steps(facts, for_status=True)
    rendered = [render_pending_step(step) for step in steps]
    assert all(isinstance(text, str) and text for text in rendered)
    assert any("systemctl --user start afx" in text for text in rendered) is present
    assert any(step.code == "start_pending" for step in steps) is present


def test_service_active_defaults_to_true_when_omitted(tmp_path):
    _write_pending_setup(tmp_path)
    facts = collect_pending_facts(tmp_path, None, None, deployed_plugins=(), outage=None, clock=_Clock())
    assert facts.service_active is True
    assert not [s for s in pending_steps(facts, for_status=True) if s.code == "start_pending"]


@pytest.mark.parametrize("instance,present", [("free", True), ("unknown", True)])
def test_status_shows_the_start_step_when_the_service_is_not_known_to_run(tmp_path, capsys, instance, present):
    root = full_root(tmp_path)
    _write_pending_setup(root)
    if instance == "unknown":
        os.unlink(root / "data" / "instance.lock")
    out = run_status(root, capsys)[1]
    assert "instance: " + instance in out.splitlines()
    assert ("systemctl --user start afx" in out) is present


def test_settings_none_drops_history_and_env_facts_even_with_a_connection(tmp_path):
    make_db(tmp_path)
    from agentic_fx.store.db import connect_observer_readonly

    conn = connect_observer_readonly(tmp_path / "data" / "agentic.db")
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        facts = collect_pending_facts(tmp_path, None, conn, deployed_plugins=(), outage=None, clock=_Clock())
    finally:
        conn.close()
    assert facts.settings is None
    assert facts.history_counts == {}
    assert facts.required_env_keys == ()
    assert facts.env_present_keys == frozenset()
    assert not [sql for sql in statements if "ohlcv_history" in sql]


def test_conn_none_drops_history_and_approvals_with_settings_present(tmp_path):
    from agentic_fx.config import load_settings_text
    from tests.status_support import REPO

    settings = load_settings_text((REPO / "config" / "settings.yaml.example").read_text(encoding="utf-8"))
    facts = collect_pending_facts(tmp_path, settings, None, deployed_plugins=(), outage=None, clock=_Clock())
    assert facts.settings is settings
    assert facts.history_counts == {}
    assert facts.approval_ids == ()


def test_an_empty_root_issues_no_sql_and_raises_nothing(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    root.mkdir()
    touch_lock(root)
    connects = []
    seen = []
    real = status.collect_pending_facts

    def spy(*args, **kwargs):
        seen.append((args[1], args[2]))
        return real(*args, **kwargs)

    with monkeypatch.context() as patch:
        real_connect = sqlite3.connect
        patch.setattr(sqlite3, "connect", lambda *a, **k: connects.append(a) or real_connect(*a, **k))
        patch.setattr(status, "collect_pending_facts", spy)
        rc, _out = run_status(root, capsys)

    assert rc == 0
    assert connects == []
    assert seen == [(None, None)]


def test_existing_call_sites_keep_the_default_of_true():
    import inspect

    parameter = inspect.signature(collect_pending_facts).parameters["service_active"]
    assert parameter.default is True
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


# fault injection -------------------------------------------------------------

def _boom(*_args, **_kwargs):
    raise RuntimeError("injected failure")


def _ok_output(tmp_path, capsys):
    root = full_root(tmp_path)
    rc, out = run_status(root, capsys)
    assert rc == 0
    return root, fields(out)


def test_all_sources_readable_returns_zero(tmp_path, capsys, unit_dir):
    root = full_root(tmp_path)
    unit_dir.mkdir()
    (unit_dir / "afx.service").write_text("[Unit]\n", encoding="utf-8")
    write_stage(root, "bot", VALID_STAGE % "bot")
    rc, _out = run_status(root, capsys, run=make_run(active="active", active_rc=0))
    assert rc == 0


FAULTS = {
    "setup": ("read_setup_record", "setup", "unavailable"),
    "unit": ("run", "unit", None),
    "state": ("read_state", "state", "mode=unavailable kill_switch=unavailable"),
    "database": ("connect_observer_readonly", "database", "unavailable"),
    "activity": ("tail_lines", "recent activity", "unavailable"),
    "pending": ("collect_pending_facts", "pending steps", "unavailable"),
    "ops-marker": ("read_marker", "ops", None),
    "ops-stage": ("read_stage", "ops", None),
}


@pytest.mark.parametrize("name", sorted(FAULTS))
def test_one_failing_source_only_degrades_its_own_section(tmp_path, monkeypatch, capsys, name):
    root, healthy = _ok_output(tmp_path, capsys)
    target, key, expected = FAULTS[name]

    with monkeypatch.context() as patch:
        if target == "run":
            rc, out = run_status(root, capsys, run=_boom)
        else:
            patch.setattr(status, target, _boom)
            rc, out = run_status(root, capsys)
    values = fields(out)

    assert rc == 1
    assert "Traceback" not in out
    if expected is not None:
        assert values[key] == expected
    elif name == "unit":
        assert values["unit"] == "afx.service=missing active=unknown linger=unknown"
    elif name == "ops-marker":
        assert values["ops"].endswith("marker=invalid") or values["ops"].endswith("marker=unavailable")
    else:
        assert "unavailable" in values["ops"] or "invalid" in values["ops"]
    skipped = {key} | ({"recent mission", "pending approvals", "plugin deploy ops"} if name == "database" else set())
    if name == "pending":
        skipped.add("next")
    for other in ("setup", "settings", "unit", "ops", "state", "database", "recent mission",
                  "pending approvals", "plugin deploy ops", "recent activity"):
        if other not in skipped:
            assert values[other] == healthy[other], other
    assert out.splitlines()[-2] == NOTE
    assert out.splitlines()[0] == "agentic-fx offline status"


def test_an_unreadable_settings_file_degrades_only_the_settings_line(tmp_path, capsys):
    root, healthy = _ok_output(tmp_path, capsys)
    os.unlink(root / "config" / "settings.yaml")
    (root / "config" / "settings.yaml").mkdir()

    rc, out = run_status(root, capsys)
    values = fields(out)

    assert rc == 0
    assert values["settings"] == "invalid"
    for other in ("setup", "unit", "ops", "state", "database", "recent mission", "pending approvals",
                  "plugin deploy ops", "recent activity"):
        assert values[other] == healthy[other]


def test_a_failing_pending_collector_keeps_the_pending_steps_line_present(tmp_path, monkeypatch, capsys):
    root, _healthy = _ok_output(tmp_path, capsys)
    monkeypatch.setattr(status, "collect_pending_facts", _boom)
    _rc, out = run_status(root, capsys)
    assert any(line.startswith("pending steps:") for line in out.splitlines())
    assert not [line for line in out.splitlines() if line.startswith("  - ")]


# zero-write -------------------------------------------------------------------

def _shape_empty(root):
    pass


def _shape_partial(root):
    write_settings(root, "ok")


def _shape_corrupt_settings(root):
    write_settings(root, "invalid")
    write_setup(root, "corrupt")
    write_state(root, "{broken")


def _shape_no_db(root):
    write_settings(root, "ok")
    write_setup(root, "ok")
    write_state(root, state_json())
    write_activity(root, "2026-10-25T00:00:00Z\tmission\tstarted\tx")
    write_stage(root, "web", VALID_STAGE % "web")
    write_setup_marker(root)


def _shape_no_keys_db_no_sidecar(root):
    _shape_no_db(root)
    make_db(root, approval=2)


def _shape_db_sidecar(root):
    _shape_no_db(root)
    add_sidecars(make_db(root, approval=2))


def _shape_plugins_dir(root):
    _shape_no_db(root)
    make_db(root, approval=2)
    (root / "plugins" / "stray").mkdir(parents=True)
    (root / "plugins" / "stray" / "note.txt").write_text("not a plugin", encoding="utf-8")


def _shape_state_lock(root):
    _shape_no_db(root)
    write_state(root, state_json(), lock=True, reset_marker=True)
    write_key(root, Principal.OPERATOR)


SHAPES = {
    "empty": _shape_empty, "partial": _shape_partial, "corrupt": _shape_corrupt_settings,
    "no-db": _shape_no_db, "db": _shape_no_keys_db_no_sidecar, "sidecar": _shape_db_sidecar,
    "state-lock": _shape_state_lock, "plugins": _shape_plugins_dir,
}


@pytest.mark.parametrize("instance", ["locked", "free", "unknown"])
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_tree_is_identical_before_and_after(tmp_path, capsys, shape, instance):
    root = tmp_path / "root"
    root.mkdir()
    SHAPES[shape](root)
    lock = root / "data" / "instance.lock"
    if instance != "unknown":
        touch_lock(root)
    before = manifest(root)
    sidecar = shape == "sidecar"
    ignored = {"data/agentic.db-wal", "data/agentic.db-shm"} if sidecar else set()

    if instance == "locked":
        with held_by_other_process(lock):
            rc, out = run_status(root, capsys)
    else:
        rc, out = run_status(root, capsys)

    assert f"instance: {instance}" in out.splitlines()
    after = manifest(root)
    keep = lambda tree: {path: value for path, value in tree.items() if path not in ignored}  # noqa: E731
    assert keep(after) == keep(before)
    if not sidecar:
        assert after == before
    assert set(after) == set(before)


def test_a_sidecar_database_main_file_is_byte_identical_and_still_readable(tmp_path, capsys):
    root = tmp_path / "root"
    root.mkdir()
    _shape_db_sidecar(root)
    touch_lock(root)
    main_before = (root / "data" / "agentic.db").read_bytes()

    rc, out = run_status(root, capsys)

    assert (root / "data" / "agentic.db").read_bytes() == main_before
    assert fields(out)["database"] == "ok"
    assert fields(out)["pending approvals"] == "3"
    assert rc == 0


def test_a_database_without_sidecars_gets_none_from_a_read(tmp_path, capsys):
    root = tmp_path / "root"
    root.mkdir()
    _shape_no_keys_db_no_sidecar(root)
    touch_lock(root)
    for _ in range(2):
        run_status(root, capsys)
    names = sorted(os.listdir(root / "data"))
    assert "agentic.db-wal" not in names and "agentic.db-shm" not in names


def test_home_key_directory_is_not_created_or_touched_when_no_key_exists(tmp_path, capsys):
    from agentic_fx.ops.keys import key_dir

    root = tmp_path / "root"
    root.mkdir()
    touch_lock(root)
    status.main(root, run=make_run())
    capsys.readouterr()
    assert not key_dir(root).exists()


# state schema ---------------------------------------------------------------

@pytest.mark.parametrize("text", [
    '{"initialized":true,"mode":"paper","autopilot":false,"kill_switch_latched":false}',
    '{"initialized":true,"mode":"live","autopilot":false,"kill_switch_latched":false}',
    '{"mode":"learning","kill_switch_latched":false}',
    '{"initialized":true,"mode":"learning","autopilot":false}',
    '{"initialized":1,"mode":"learning","autopilot":false,"kill_switch_latched":false}',
    '{"initialized":true,"mode":"learning","autopilot":false,"kill_switch_latched":false,'
    '"kill_switch_generation":"1"}',
    '{"initialized":true,"mode":"learning","autopilot":false,"kill_switch_latched":false,'
    '"kill_switch_latched_at":5}',
])
def test_state_outside_the_state_store_schema_is_unavailable(tmp_path, text):
    root = tmp_path / "root"
    write_state(root, text)
    assert status.read_state(root) == ("unavailable", "unavailable")


def test_state_with_the_optional_keys_is_read_like_the_state_store(tmp_path):
    root = tmp_path / "root"
    write_state(root, '{"initialized":true,"mode":"trading","autopilot":true,"kill_switch_latched":false,'
                      '"kill_switch_generation":3,"kill_switch_latched_at":"2026-10-25T00:00:00+00:00"}')
    assert status.read_state(root) == ("trading", "clear")


def test_reset_marker_without_a_state_file_is_latched(tmp_path):
    root = tmp_path / "root"
    state_dir = root / "data" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "app_state.json.reset-in-progress").write_text("{}", encoding="utf-8")
    assert status.read_state(root) == ("learning", "latched")


# plugin inventory -------------------------------------------------------------

def _spy_pending(monkeypatch):
    seen = {}
    real = status.collect_pending_facts

    def spy(*args, **kwargs):
        seen["deployed"] = kwargs.get("deployed_plugins")
        seen["args"] = args
        return real(*args, **kwargs)

    monkeypatch.setattr(status, "collect_pending_facts", spy)
    return seen


class _Inventory:
    def __init__(self, metas):
        self.inventory = type("Inv", (), {"metas": metas})()


def test_the_approved_inventory_is_passed_when_settings_database_and_plugins_exist(tmp_path, monkeypatch, capsys):
    root = full_root(tmp_path)
    (root / "plugins").mkdir()
    seen = _spy_pending(monkeypatch)
    calls = []

    def fake_approved(conn, plugins_dir, *, settings):
        calls.append((conn, plugins_dir, settings))
        return _Inventory(("meta-a", "meta-b"))

    monkeypatch.setattr(status.plugin_loader, "approved_plugins", fake_approved)
    run_status(root, capsys)

    assert seen["deployed"] == ("meta-a", "meta-b")
    assert len(calls) == 1 and calls[0][1] == root / "plugins" and calls[0][2] is not None


@pytest.mark.parametrize("missing", ["plugins", "settings", "database"])
def test_the_inventory_is_empty_when_a_source_is_missing(tmp_path, monkeypatch, capsys, missing):
    root = full_root(tmp_path, settings="missing" if missing == "settings" else "ok",
                     db=missing != "database")
    if missing != "plugins":
        (root / "plugins").mkdir()
    seen = _spy_pending(monkeypatch)
    calls = []
    monkeypatch.setattr(status.plugin_loader, "approved_plugins",
                        lambda *a, **k: calls.append(1) or _Inventory(("x",)))
    run_status(root, capsys)

    assert seen["deployed"] == ()
    assert calls == []


def test_a_plugins_file_instead_of_a_directory_is_not_inventoried(tmp_path, monkeypatch, capsys):
    root = full_root(tmp_path)
    (root / "plugins").write_text("file", encoding="utf-8")
    seen = _spy_pending(monkeypatch)
    run_status(root, capsys)
    assert seen["deployed"] == ()


# database robustness ----------------------------------------------------------

def test_the_observer_connections_are_always_closed(tmp_path, monkeypatch, capsys):
    root = full_root(tmp_path)
    opened = []
    real = status.connect_observer_readonly

    class Tracked:
        def __init__(self, conn):
            self.conn = conn
            self.closed = False
            opened.append(self)

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def close(self):
            self.closed = True
            self.conn.close()

    monkeypatch.setattr(status, "connect_observer_readonly", lambda path: Tracked(real(path)))
    monkeypatch.setattr(status.missions, "recent", _boom)
    run_status(root, capsys)

    assert opened and all(item.closed for item in opened)


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_a_database_path_that_is_not_a_regular_file_is_unavailable(tmp_path, capsys, kind):
    root = full_root(tmp_path, db=False)
    target = root / "data" / "agentic.db"
    if kind == "directory":
        target.mkdir()
    else:
        os.mkfifo(target)

    rc, out = run_status(root, capsys)
    values = fields(out)

    assert rc == 1
    assert values["database"] == "unavailable"
    assert (values["recent mission"], values["pending approvals"], values["plugin deploy ops"]) == (
        "unavailable",) * 3
    assert values["setup"] == "ok"


def test_a_database_symlink_to_nowhere_is_unavailable(tmp_path, capsys):
    root = full_root(tmp_path, db=False)
    (root / "data" / "agentic.db").symlink_to(tmp_path / "nowhere.db")
    rc, out = run_status(root, capsys)
    assert rc == 1 and fields(out)["database"] == "unavailable"


# ops reader exceptions and rc rules ----------------------------------------------

def test_an_unreadable_stage_file_is_unavailable_and_exits_one(tmp_path, capsys):
    root = full_root(tmp_path)
    (root / "data" / "ops").mkdir(parents=True, exist_ok=True)
    (root / "data" / "ops" / "bot.stage.json").mkdir()
    rc, out = run_status(root, capsys)
    assert rc == 1
    assert fields(out)["ops"] == "bot=unavailable web=missing notify=missing marker=none"


def test_a_malformed_stage_file_is_invalid_and_exits_zero(tmp_path, capsys):
    root = full_root(tmp_path)
    write_stage(root, "web", "{not json")
    rc, out = run_status(root, capsys)
    assert rc == 0
    assert fields(out)["ops"] == "bot=missing web=invalid notify=missing marker=none"


def test_a_failing_pending_collector_says_unavailable_without_step_lines(tmp_path, monkeypatch, capsys):
    root = full_root(tmp_path)
    monkeypatch.setattr(status, "collect_pending_facts", _boom)
    rc, out = run_status(root, capsys)
    assert rc == 1
    assert fields(out)["pending steps"] == "unavailable"
    assert not [line for line in out.splitlines() if line.startswith("  - ")]


def test_a_failing_step_builder_says_unavailable(tmp_path, monkeypatch, capsys):
    root = full_root(tmp_path)
    monkeypatch.setattr(status, "pending_steps", _boom)
    rc, out = run_status(root, capsys)
    assert rc == 1
    assert fields(out)["pending steps"] == "unavailable"


def test_a_missing_systemd_is_unknown_and_exits_zero(tmp_path, capsys):
    root = full_root(tmp_path)
    rc, out = run_status(root, capsys, run=make_run(fail=FileNotFoundError("systemctl")))
    assert rc == 0
    assert fields(out)["unit"] == "afx.service=missing active=unknown linger=unknown"


def test_invalid_and_missing_values_alone_never_exit_one(tmp_path, capsys):
    root = full_root(tmp_path, setup="corrupt", settings="invalid")
    write_enroll_marker(root, "not json")
    rc, out = run_status(root, capsys)
    values = fields(out)
    assert (values["setup"], values["settings"]) == ("corrupt", "invalid")
    assert rc == 0


# field-wise activity, unit words, symlinked database, shared connection ----------

def test_a_percent_encoded_non_ascii_field_is_redacted_alone(tmp_path, capsys):
    root = full_root(tmp_path)
    write_activity(root, "2026-10-25T01:02:03Z\tpolicy\ttoken=%E3%81%82\tsummary")
    rc, out = run_status(root, capsys)
    assert fields(out)["recent activity"] == "2026-10-25T01:02:03Z policy [redacted]"
    assert rc == 0


def test_a_redacted_category_does_not_hide_the_other_fields(tmp_path, capsys):
    root = full_root(tmp_path)
    write_activity(root, "2026-10-25T01:02:03Z\ttoken=%E3%81%82\tevent\tsummary")
    assert fields(run_status(root, capsys)[1])["recent activity"] == "2026-10-25T01:02:03Z [redacted] event"


@pytest.mark.parametrize("stdout,rc_code,word", [
    ("activating\n", 3, "activating"),
    ("deactivating\n", 3, "deactivating"),
    ("active\n", 3, "active"),
    ("inactive\n", 0, "inactive"),
    ("failed\n", 0, "failed"),
    ("", 3, "unknown"),
    ("\n", 0, "unknown"),
    ("reloading\n", 0, "unknown"),
    ("Failed to connect to bus\n", 1, "unknown"),
])
def test_is_active_stdout_decides_the_word_and_the_exit_code_is_unchanged(tmp_path, capsys, stdout,
                                                                       rc_code, word):
    root = full_root(tmp_path)
    rc, out = run_status(root, capsys, run=make_run(active=stdout.rstrip("\n"), active_rc=rc_code))
    assert fields(out)["unit"].split(" ")[1] == f"active={word}"
    assert rc == 0


@pytest.mark.parametrize("word,rc_code,expected", [
    ("inactive", 3, "systemctl --user start afx.service"),
    ("failed", 3, "systemctl --user start afx.service"),
    ("", 1, "systemctl --user start afx.service"),
    ("reloading", 0, "systemctl --user start afx.service"),
    ("active", 0, "afx ctl status"),
    ("activating", 3, "afx ctl status"),
])
def test_next_starts_the_unit_only_when_it_is_not_running(tmp_path, capsys, unit_dir, word, rc_code, expected):
    root = full_root(tmp_path)
    unit_dir.mkdir()
    (unit_dir / "afx.service").write_text("[Unit]\n", encoding="utf-8")
    out = run_status(root, capsys, run=make_run(active=word, active_rc=rc_code))[1]
    assert fields(out)["next"] == expected


def test_stderr_only_is_active_answer_is_unknown(tmp_path, capsys):
    root = full_root(tmp_path)

    def run(argv, **_kwargs):
        if argv[0] == "loginctl":
            return SimpleNamespace(returncode=0, stdout="Linger=yes\n")
        return SimpleNamespace(returncode=1, stdout="", stderr="inactive\n")

    assert fields(run_status(root, capsys, run=run)[1])["unit"].split(" ")[1] == "active=unknown"


def test_a_symlinked_database_is_read_and_left_untouched(tmp_path, capsys):
    root = full_root(tmp_path, db=False)
    target_root = tmp_path / "elsewhere"
    (target_root / "data").mkdir(parents=True)
    real = make_db(target_root, approval=2)
    (root / "data" / "agentic.db").symlink_to(real)
    before = real.read_bytes()

    rc, out = run_status(root, capsys)
    values = fields(out)

    assert rc == 0
    assert values["database"] == "ok" and values["pending approvals"] == "2"
    assert values["recent mission"].startswith("#")
    assert real.read_bytes() == before
    assert sorted(path.name for path in real.parent.iterdir()) == ["agentic.db"]


def test_a_dangling_symlink_is_unavailable_and_a_plain_absence_is_missing(tmp_path, capsys):
    root = full_root(tmp_path, db=False)
    assert fields(run_status(root, capsys)[1])["database"] == "missing"
    (root / "data" / "agentic.db").symlink_to(tmp_path / "gone.db")
    rc, out = run_status(root, capsys)
    assert rc == 1 and fields(out)["database"] == "unavailable"


def test_a_failing_inventory_is_an_unavailable_pending_section(tmp_path, monkeypatch, capsys):
    root = full_root(tmp_path)
    (root / "plugins").mkdir()
    monkeypatch.setattr(status.plugin_loader, "approved_plugins", _boom)
    rc, out = run_status(root, capsys)
    values = fields(out)
    assert rc == 1 and values["pending steps"] == "unavailable"
    assert values["database"] == "ok" and values["recent activity"] != "unavailable"


def test_the_database_is_opened_once_for_the_shared_connection(tmp_path, monkeypatch, capsys):
    root = full_root(tmp_path)
    opened = []
    real = status.connect_observer_readonly
    monkeypatch.setattr(status, "connect_observer_readonly", lambda path: opened.append(path) or real(path))
    run_status(root, capsys)
    assert len(opened) == 2  # one shared connection plus the deploy-ops counter's own opener


def test_the_state_reader_and_the_state_store_validate_alike(tmp_path):
    from agentic_fx.store.state import StateError, StateStore, parse_state

    assert status.parse_state is parse_state
    good = '{"initialized":true,"mode":"trading","autopilot":false,"kill_switch_latched":true}'
    bad = ['[]', '{}', '{"initialized":true,"mode":"x","autopilot":false,"kill_switch_latched":false}',
           '{"initialized":1,"mode":"learning","autopilot":false,"kill_switch_latched":false}']
    for number, text in enumerate([good, *bad]):
        root = tmp_path / f"r{number}"
        write_state(root, text)
        path = root / "data" / "state" / "app_state.json"
        try:
            StateStore(path).load()
            store_ok = True
        except StateError:
            store_ok = False
        assert (status.read_state(root)[0] != "unavailable") is store_ok
