from __future__ import annotations

import json
import sqlite3
import stat

import pytest

from agentic_fx.backtest.plugin_deploy_cli import plugin_list
from agentic_fx.store.db import connect, connect_observer_readonly, init_db
from tests.fixtures.fs_tree import tree_snapshot

STRATEGY_CONFIG = "kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\nexit_mode: levels\n"
INDICATOR_CONFIG = "kind: indicator\ntimeframe: 4h\npairs: [USDJPY]\noutputs: [v]\n"
H_PENDING = "8b0e6d5a2c91" + "0" * 52
H_TARGET = "3f9a1c2b7d4e" + "1" * 52
NOW = "2026-10-23T00:00:00+00:00"


def _write_plugin(root, name, kind="strategy", config=None):
    directory = root / "plugins" / name
    directory.mkdir(parents=True)
    if kind == "strategy":
        (directory / "plugin.py").write_text(
            "def evaluate(df, indicators, signals, params):\n    return {'action': 'hold'}\n")
    else:
        (directory / "plugin.py").write_text("def compute(df, params):\n    return df\n")
    (directory / "config.yaml").write_text(
        config or (STRATEGY_CONFIG if kind == "strategy" else INDICATOR_CONFIG))
    (directory / "test_plugin.py").write_text("def test_plugin():\n    pass\n")
    return directory


def _new_db(root):
    db = root / "data" / "agentic.db"
    conn = connect(db)
    init_db(conn)
    return db, conn


def _pending(conn, payload):
    conn.execute("INSERT INTO approval_requests (kind,payload_json,status,created_at) VALUES (?,?,?,?)",
                 ("plugin", payload if isinstance(payload, str) else json.dumps(payload), "pending", NOW))


def _deploy_op(conn, op_id, name, target=H_TARGET):
    conn.execute(
        "INSERT INTO plugin_deploy_ops (op_id,op_kind,name,from_kind,to_kind,"
        "from_target,to_target,retired_path,subject_content_hash,subject_artifact_hash,"
        "target_content_hash,target_artifact_hash,approval_id,revoke,revoke_row_id,force,"
        "phase,fail_reason,notify_attempted,actor,created_at,updated_at) "
        "VALUES (?,'rollback_symlink',?,'absent','symlink',NULL,?,NULL,NULL,NULL,?,"
        "?,NULL,0,NULL,0,'intent',NULL,0,'test',?,?)",
        (op_id, name, ".versions/x/x", target, "f" * 64, NOW, NOW))


def _switch_journal(conn, op_id, name, phase="recorded"):
    conn.execute(
        "INSERT INTO plugin_switch_journal (op_id,kind,approval_id,name,old_kind,old_target,"
        "temp_path,new_target,switch_required,phase,actor,updated_at) "
        "VALUES (?,'approve',1,?,'absent',NULL,'t','.versions/x/cccccccccccc',1,?,'test',?)",
        (op_id, name, phase, NOW))


def _lines(capsys):
    return capsys.readouterr().out.splitlines()


def test_plugin_list_golden_for_live_pending_and_open_rollback_without_writing(tmp_path, capsys):
    _write_plugin(tmp_path, "momentum")
    broken = tmp_path / "plugins" / "rsi"
    broken.mkdir()
    (broken / "config.yaml").write_text("kind: [unterminated\n")
    db, conn = _new_db(tmp_path)
    live_hash = _live_hash(tmp_path, "momentum")
    _pending(conn, {"name": "momentum", "content_hash": H_PENDING, "kind": "strategy"})
    _deploy_op(conn, 17, "momentum")
    conn.commit()
    conn.close()
    before = tree_snapshot(tmp_path)

    assert plugin_list(tmp_path) == 1

    out = capsys.readouterr().out
    assert out == (
        "source live: ok\n"
        "source pending: ok\n"
        "source journal: ok\n"
        "NAME      KIND      LIVE          PENDING       OPEN_OP                   ISSUES\n"
        f"momentum  strategy  {live_hash[:12]}  {H_PENDING[:12]}  17:rollback:{H_TARGET[:12]}  -\n"
        "rsi       unknown   -             -             -                         broken:invalid_manifest\n")
    assert tree_snapshot(tmp_path) == before


def _live_hash(root, name):
    from agentic_fx.plugin import loader
    meta, _ = loader.discover_one_with_reason(root / "plugins" / name, name)
    return meta.content_hash


def test_plugin_list_without_any_source_creates_nothing_and_is_rc0(tmp_path, capsys):
    before = tree_snapshot(tmp_path)
    assert plugin_list(tmp_path) == 0
    assert capsys.readouterr().out == (
        "source live: missing\nsource pending: missing\nsource journal: missing\n"
        "NAME  KIND  LIVE  PENDING  OPEN_OP  ISSUES\n")
    assert tree_snapshot(tmp_path) == before
    assert not (tmp_path / "plugins").exists() and not (tmp_path / "data").exists()


def test_plugin_list_live_path_that_is_a_file_is_unavailable_but_other_sources_show(tmp_path, capsys):
    (tmp_path / "plugins").write_text("not a directory")
    db, conn = _new_db(tmp_path)
    _pending(conn, {"name": "alpha", "content_hash": H_PENDING, "kind": "indicator"})
    conn.commit()
    conn.close()

    assert plugin_list(tmp_path) == 1

    lines = _lines(capsys)
    assert lines[:3] == ["source live: unavailable", "source pending: ok", "source journal: ok"]
    assert lines[4].split() == ["alpha", "indicator", "-", H_PENDING[:12], "-", "-"]


def test_plugin_list_unreadable_live_directory_is_unavailable_only_for_live(tmp_path, capsys):
    plugins = tmp_path / "plugins"
    _write_plugin(tmp_path, "alpha")
    plugins.chmod(0)
    try:
        if _can_read(plugins):
            pytest.skip("directory permissions are not enforced for this user")
        assert plugin_list(tmp_path) == 1
    finally:
        plugins.chmod(stat.S_IRWXU)
    lines = _lines(capsys)
    assert lines[:3] == ["source live: unavailable", "source pending: missing", "source journal: missing"]
    assert len(lines) == 4


def _can_read(path):
    try:
        next(path.iterdir())
    except PermissionError:
        return False
    except StopIteration:
        return True
    return True


def test_plugin_list_one_broken_plugin_does_not_hide_the_others(tmp_path, capsys, monkeypatch):
    _write_plugin(tmp_path, "alpha")
    _write_plugin(tmp_path, "boom")
    _write_plugin(tmp_path, "gamma", "indicator")
    from agentic_fx.plugin import loader
    real = loader.discover_one_with_reason

    def discover(directory, name):
        if name == "boom":
            raise TypeError("manifest has the wrong shape")
        return real(directory, name)

    monkeypatch.setattr(loader, "discover_one_with_reason", discover)

    assert plugin_list(tmp_path) == 1

    lines = _lines(capsys)
    assert lines[0] == "source live: ok"
    rows = [line.split() for line in lines[4:]]
    assert [row[:2] for row in rows] == [["alpha", "strategy"], ["boom", "unknown"],
                                         ["gamma", "indicator"]]
    assert rows[1][-1] == "broken:invalid_manifest"


@pytest.mark.parametrize("error", [ValueError("x"), KeyError("x"), OSError("x")])
def test_plugin_list_any_per_plugin_failure_is_a_broken_row(tmp_path, capsys, monkeypatch, error):
    _write_plugin(tmp_path, "alpha")
    _write_plugin(tmp_path, "boom")
    from agentic_fx.plugin import loader
    real = loader.discover_one_with_reason

    def discover(directory, name):
        if name == "boom":
            raise error
        return real(directory, name)

    monkeypatch.setattr(loader, "discover_one_with_reason", discover)
    assert plugin_list(tmp_path) == 1
    lines = _lines(capsys)
    assert lines[0] == "source live: ok"
    assert [line.split()[0] for line in lines[4:]] == ["alpha", "boom"]


def test_plugin_list_orders_by_name_then_kind_and_keeps_same_name_kinds_apart(tmp_path, capsys):
    _write_plugin(tmp_path, "zeta", "indicator")
    _write_plugin(tmp_path, "beta")
    db, conn = _new_db(tmp_path)
    _pending(conn, {"name": "beta", "content_hash": H_PENDING, "kind": "indicator"})
    _pending(conn, {"name": "alpha", "content_hash": H_TARGET, "kind": "strategy"})
    conn.commit()
    conn.close()

    plugin_list(tmp_path)

    rows = [line.split()[:2] for line in _lines(capsys)[4:]]
    assert rows == [["alpha", "strategy"], ["beta", "indicator"], ["beta", "strategy"],
                    ["zeta", "indicator"]]


def test_plugin_list_pending_with_other_kind_than_live_gets_its_own_row(tmp_path, capsys):
    _write_plugin(tmp_path, "beta")
    db, conn = _new_db(tmp_path)
    _pending(conn, {"name": "beta", "content_hash": H_PENDING, "kind": "indicator"})
    conn.commit()
    conn.close()

    assert plugin_list(tmp_path) == 1

    rows = [line.split() for line in _lines(capsys)[4:]]
    assert [row[:2] for row in rows] == [["beta", "indicator"], ["beta", "strategy"]]
    assert rows[0][3] == H_PENDING[:12] and rows[0][-1] == "kind_mismatch"
    assert H_PENDING[:12] not in rows[1] and "kind_mismatch" not in rows[1]

    assert plugin_list(tmp_path, kind="indicator") == 1
    rows = [line.split() for line in _lines(capsys)[4:]]
    assert [row[:2] for row in rows] == [["beta", "indicator"]] and rows[0][3] == H_PENDING[:12]


def test_plugin_list_pending_without_kind_is_kind_missing_and_survives_the_kind_filter(tmp_path, capsys):
    _write_plugin(tmp_path, "beta")
    db, conn = _new_db(tmp_path)
    _pending(conn, {"name": "beta", "content_hash": H_PENDING})
    conn.commit()
    conn.close()

    assert plugin_list(tmp_path, kind="strategy") == 1

    rows = [line.split() for line in _lines(capsys)[4:]]
    assert len(rows) == 1 and rows[0][3] == H_PENDING[:12] and rows[0][-1] == "kind_missing"


def test_plugin_list_kind_filter_hides_unknown_rows_but_keeps_source_status(tmp_path, capsys):
    _write_plugin(tmp_path, "alpha")
    _write_plugin(tmp_path, "gamma", "indicator")
    broken = tmp_path / "plugins" / "rsi"
    broken.mkdir()
    (broken / "config.yaml").write_text("kind: [x\n")

    assert plugin_list(tmp_path, kind="indicator") == 0

    lines = _lines(capsys)
    assert lines[:3] == ["source live: ok", "source pending: missing", "source journal: missing"]
    assert [line.split()[:2] for line in lines[4:]] == [["gamma", "indicator"]]


@pytest.mark.parametrize("content", ["", "abc", "g" * 20, 5, None])
def test_plugin_list_malformed_pending_hash_is_a_broken_row_not_an_error(tmp_path, capsys, content):
    _write_plugin(tmp_path, "alpha")
    db, conn = _new_db(tmp_path)
    _pending(conn, {"name": "alpha", "content_hash": content, "kind": "strategy"})
    conn.commit()
    conn.close()

    assert plugin_list(tmp_path) == 1

    lines = _lines(capsys)
    assert lines[1] == "source pending: ok"
    rows = [line.split() for line in lines[4:]]
    assert [row[0] for row in rows] == ["alpha", "invalid-pending-1"]
    assert rows[0][3] == "-" and rows[1][-1] == "broken:invalid_pending"


def test_plugin_list_unparseable_pending_payload_is_a_broken_row(tmp_path, capsys):
    db, conn = _new_db(tmp_path)
    _pending(conn, "not json")
    _pending(conn, json.dumps(["list"]))
    conn.commit()
    conn.close()
    assert plugin_list(tmp_path) == 1
    names = [line.split()[0] for line in _lines(capsys)[4:]]
    assert names == ["invalid-pending-1", "invalid-pending-2"]


def test_plugin_list_short_hash_prefixes_stay_at_least_twelve_and_unique(tmp_path, capsys):
    shared = "abcdef012345"
    first, second = shared + "aa", shared + "bb"
    db, conn = _new_db(tmp_path)
    _pending(conn, {"name": "alpha", "content_hash": first, "kind": "strategy"})
    _pending(conn, {"name": "alpha", "content_hash": second, "kind": "strategy"})
    conn.commit()
    conn.close()
    plugin_list(tmp_path)
    row = _lines(capsys)[4].split()
    pending = row[3].split(",")
    assert pending == [first[:13], second[:13]]
    assert all(len(value) >= 12 for value in pending)


def test_plugin_list_duplicate_pending_and_open_ops_are_flagged(tmp_path, capsys):
    db, conn = _new_db(tmp_path)
    _pending(conn, {"name": "alpha", "content_hash": H_PENDING, "kind": "strategy"})
    _pending(conn, {"name": "alpha", "content_hash": H_PENDING, "kind": "strategy"})
    _deploy_op(conn, 1, "alpha")
    _switch_journal(conn, 2, "alpha")
    conn.commit()
    conn.close()

    assert plugin_list(tmp_path) == 1

    row = _lines(capsys)[4].split()
    assert row[-1] == "duplicate_open_op,duplicate_pending"


def test_plugin_list_single_open_op_is_not_a_duplicate(tmp_path, capsys):
    db, conn = _new_db(tmp_path)
    _pending(conn, {"name": "alpha", "content_hash": H_PENDING, "kind": "strategy"})
    _deploy_op(conn, 1, "alpha")
    conn.commit()
    conn.close()
    assert plugin_list(tmp_path) == 0
    assert _lines(capsys)[4].split()[-1] == "-"


def test_plugin_list_duplicate_live_entries_are_flagged(tmp_path, capsys, monkeypatch):
    import dataclasses
    from agentic_fx.plugin import loader
    _write_plugin(tmp_path, "alpha")
    (tmp_path / "plugins" / "alpha_copy").mkdir()
    real = loader.discover_one_with_reason

    def discover(directory, name):
        meta, reason = real(tmp_path / "plugins" / "alpha", "alpha")
        return dataclasses.replace(meta, name="alpha"), reason

    monkeypatch.setattr(loader, "discover_one_with_reason", discover)
    plugin_list(tmp_path)
    rows = [line.split() for line in _lines(capsys)[4:]]
    assert len(rows) == 1 and rows[0][0] == "alpha"
    assert rows[0][-1] == "duplicate_live"


def test_plugin_list_missing_table_is_unavailable_for_that_source_only(tmp_path, capsys):
    db = tmp_path / "data" / "agentic.db"
    db.parent.mkdir()
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE approval_requests (id INTEGER PRIMARY KEY, kind TEXT, "
                 "payload_json TEXT, status TEXT, created_at TEXT)")
    conn.execute("INSERT INTO approval_requests (kind,payload_json,status,created_at) "
                 "VALUES ('plugin',?, 'pending', 'x')",
                 (json.dumps({"name": "alpha", "content_hash": H_PENDING, "kind": "strategy"}),))
    conn.commit()
    conn.close()

    assert plugin_list(tmp_path) == 1

    lines = _lines(capsys)
    assert lines[:3] == ["source live: missing", "source pending: ok", "source journal: unavailable"]
    assert lines[4].split()[:4] == ["alpha", "strategy", "-", H_PENDING[:12]]


def test_plugin_list_corrupt_database_is_unavailable_and_unchanged(tmp_path, capsys):
    _write_plugin(tmp_path, "alpha")
    db = tmp_path / "data" / "agentic.db"
    db.parent.mkdir()
    db.write_bytes(b"this is not a sqlite database" * 20)
    before = tree_snapshot(tmp_path)
    assert plugin_list(tmp_path) == 1
    lines = _lines(capsys)
    assert lines[:3] == ["source live: ok", "source pending: unavailable", "source journal: unavailable"]
    assert tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("db_state", ["no_sidecar", "with_sidecar"])
def test_plugin_list_sidecar_contract(tmp_path, capsys, db_state):
    _write_plugin(tmp_path, "alpha")
    db, conn = _new_db(tmp_path)
    _pending(conn, {"name": "alpha", "content_hash": H_PENDING, "kind": "strategy"})
    conn.commit()
    if db_state == "no_sidecar":
        conn.close()
    holder = conn if db_state == "with_sidecar" else None
    wal, shm = db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")
    try:
        assert wal.exists() == (db_state == "with_sidecar")
        assert shm.exists() == (db_state == "with_sidecar")
        before = tree_snapshot(tmp_path)
        before_db = db.read_bytes()
        assert plugin_list(tmp_path) == 0
        assert db.read_bytes() == before_db
        if db_state == "no_sidecar":
            assert tree_snapshot(tmp_path) == before
            assert not wal.exists() and not shm.exists()
        else:
            ignore = frozenset({"data/agentic.db-wal", "data/agentic.db-shm"})
            assert (tree_snapshot(tmp_path, ignore=ignore)
                    == {k: v for k, v in before.items() if k not in ignore})
    finally:
        if holder is not None:
            holder.close()
    assert H_PENDING[:12] in capsys.readouterr().out


def test_observer_connection_uses_immutable_only_without_sidecars(tmp_path):
    db, conn = _new_db(tmp_path)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    observer = connect_observer_readonly(db)
    try:
        assert observer.execute("PRAGMA query_only").fetchone()[0] in (0, 1)
        with pytest.raises(sqlite3.OperationalError):
            observer.execute("INSERT INTO t VALUES (1)")
    finally:
        observer.close()
    assert not db.with_name(db.name + "-wal").exists()
    assert not db.with_name(db.name + "-shm").exists()


def test_observer_connection_requires_an_existing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        connect_observer_readonly(tmp_path / "nope.db")
    assert list(tmp_path.iterdir()) == []
