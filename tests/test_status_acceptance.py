"""Offline status: per-source output, fault isolation and the zero-write contract."""
from __future__ import annotations

import os
import sqlite3

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
    assert values["state"] == "mode=paper kill_switch=clear"
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
    assert values["state"] == "mode=paper kill_switch=clear"
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
    assert values["state"] == "mode=paper kill_switch=clear"


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

    assert rc == 1
    assert values["settings"] in {"invalid", "unavailable"}
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


def _shape_state_lock(root):
    _shape_no_db(root)
    write_state(root, state_json(), lock=True, reset_marker=True)
    write_key(root, Principal.OPERATOR)


SHAPES = {
    "empty": _shape_empty, "partial": _shape_partial, "corrupt": _shape_corrupt_settings,
    "no-db": _shape_no_db, "db": _shape_no_keys_db_no_sidecar, "sidecar": _shape_db_sidecar,
    "state-lock": _shape_state_lock,
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
