"""候補台帳の migration と引き当ての境界を 1 つずつ固定する。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from agentic_fx.store import candidate_archives as archives
from agentic_fx.store import db as db_mod
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)

_OLD_DDL = """
CREATE TABLE candidate_archives (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  artifact_hash TEXT NOT NULL,
  archive_path TEXT,
  pair TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(mission_id, artifact_hash)
);
"""


def _legacy(tmp_path, name="legacy.db"):
    path = tmp_path / name
    conn = connect(path)
    init_db(conn)
    conn.execute("DROP TABLE candidate_archives")
    conn.executescript(_OLD_DDL)
    conn.commit()
    return conn, path


def _old_row(conn, mission_id, artifact, content):
    conn.execute(
        "INSERT INTO candidate_archives (mission_id,name,content_hash,"
        "artifact_hash,archive_path,pair,metrics_json,created_at) VALUES "
        "(?,?,?,?,NULL,'USDJPY','{}',?)",
        (mission_id, "n", content, artifact, NOW.isoformat()))


def _approval(conn, status, payload):
    return conn.execute(
        "INSERT INTO approval_requests (kind,payload_json,status,reason,"
        "decided_by,decided_at,created_at) VALUES ('plugin',?,?,NULL,'h',?,?)",
        (json.dumps(payload), status, NOW.isoformat(),
         NOW.isoformat())).lastrowid


def _spy_rebuild(monkeypatch):
    calls = []
    real = db_mod._rebuild_candidate_archives

    def spy(conn):
        calls.append(1)
        return real(conn)

    monkeypatch.setattr(db_mod, "_rebuild_candidate_archives", spy)
    return calls


def test_rebuild_runs_once_for_a_legacy_table_and_never_for_a_current_one(
        tmp_path, monkeypatch):
    calls = _spy_rebuild(monkeypatch)
    fresh = connect(tmp_path / "fresh.db")
    init_db(fresh)
    init_db(fresh)
    assert calls == []
    legacy, path = _legacy(tmp_path)
    legacy.close()
    migrated = connect(path)
    init_db(migrated)
    assert calls == [1]
    init_db(migrated)
    assert calls == [1]


def test_rebuild_takes_a_backup_before_touching_the_table(tmp_path):
    legacy, path = _legacy(tmp_path)
    _old_row(legacy, 1, "h1", "c1")
    legacy.commit()
    legacy.close()
    init_db(connect(path))
    backup = path.with_name(path.name + ".bak-candidate-archives")
    assert backup.exists()
    old = sqlite3.connect(backup)
    try:
        assert old.execute("SELECT count(*) FROM candidate_archives"
                           ).fetchone()[0] == 1
    finally:
        old.close()


def test_rebuild_failure_rolls_back_and_leaves_the_old_table(
        tmp_path, monkeypatch):
    legacy, path = _legacy(tmp_path)
    _old_row(legacy, 1, "h1", "c1")
    legacy.commit()
    legacy.close()

    def boom(conn):
        raise RuntimeError("backfill failed")

    monkeypatch.setattr(archives, "backfill_outcome_from_approvals", boom)
    conn = connect(path)
    with pytest.raises(RuntimeError):
        init_db(conn)
    assert not conn.in_transaction
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "candidate_archives_new" not in names
    cols = [r["name"] for r in conn.execute(
        "PRAGMA table_info(candidate_archives)")]
    assert "outcome_class" not in cols
    assert conn.execute("SELECT count(*) FROM candidate_archives"
                        ).fetchone()[0] == 1


def test_rebuild_carries_the_autoincrement_counter(tmp_path):
    legacy, path = _legacy(tmp_path)
    for index in (1, 2, 3):
        _old_row(legacy, 1, f"h{index}", f"c{index}")
    legacy.execute("DELETE FROM candidate_archives WHERE artifact_hash='h3'")
    legacy.commit()
    legacy.close()
    migrated = connect(path)
    init_db(migrated)
    new_id = archives.insert(
        migrated, mission_id=1, name="n", content_hash="cx",
        artifact_hash="hx", archive_path=None, pair="USDJPY", metrics={},
        now=NOW, commit=True)
    assert new_id == 4


def test_resolve_prefers_artifact_hash_over_a_unique_content_hash(tmp_path):
    conn = connect(tmp_path / "r.db")
    init_db(conn)
    first = archives.insert(
        conn, mission_id=3, name="n", content_hash="c1", artifact_hash="a1",
        archive_path=None, pair=None, metrics={}, now=NOW)
    second = archives.insert(
        conn, mission_id=3, name="n", content_hash="c2", artifact_hash="a2",
        archive_path=None, pair=None, metrics={}, now=NOW)
    assert archives.resolve(conn, {
        "mission_id": 3, "artifact_hash": "a2", "content_hash": "c1"}
    ) == second
    assert first != second


def test_identity_backfill_leaves_current_version_values_alone(tmp_path):
    import shutil
    from pathlib import Path
    examples = (Path(__file__).resolve().parents[2] / "docs" / "examples"
                / "plugins" / "sma_cross")
    root = tmp_path / "root"
    shutil.copytree(examples, root / "plugins" / "_archive" / "1" / "h1")
    conn, path = _legacy(tmp_path)
    conn.execute(
        "INSERT INTO candidate_archives (mission_id,name,content_hash,"
        "artifact_hash,archive_path,pair,metrics_json,created_at) VALUES "
        "(1,'sma_cross','c1','h1','plugins/_archive/1/h1','USDJPY','{}',?)",
        (NOW.isoformat(),))
    conn.commit()
    conn.close()
    migrated = connect(path)
    init_db(migrated)  # root なし: NULL のまま
    migrated.execute("UPDATE candidate_archives SET logic_hash='v1:custom', "
                     "family_key='strategy:custom@1h@X'")
    migrated.commit()
    init_db(migrated, root=root)
    row = migrated.execute(
        "SELECT logic_hash, family_key FROM candidate_archives").fetchone()
    assert (row[0], row[1]) == ("v1:custom", "strategy:custom@1h@X")


def test_revocation_only_reaches_rows_the_same_backfill_approved(tmp_path):
    """CHECK 句だけ欠けた台帳に既に approved の行があっても、撤回の承認を
    根拠にその行を revoked へ動かさない。"""
    import re
    conn = connect(tmp_path / "c.db")
    init_db(conn)
    archive_id = archives.insert(
        conn, mission_id=5, name="n", content_hash="c5", artifact_hash="h5",
        archive_path=None, pair=None, metrics={}, now=NOW)
    conn.execute("UPDATE candidate_archives SET outcome_class='approved' "
                 "WHERE id=?", (archive_id,))
    approved = _approval(conn, "approved", {
        "mission_id": 5, "artifact_hash": "h5", "content_hash": "c5"})
    _approval(conn, "rejected", {
        "name": "n", "content_hash": "c5", "artifact_hash": "h5",
        "revokes_approval_id": approved})
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE "
                       "name='candidate_archives'").fetchone()[0]
    stripped = re.sub(r"CHECK \(outcome_class IN \([^)]*\)\)", "", ddl)
    conn.execute("ALTER TABLE candidate_archives RENAME TO ca_old")
    conn.execute(stripped)
    conn.execute("INSERT INTO candidate_archives SELECT * FROM ca_old")
    conn.execute("DROP TABLE ca_old")
    conn.commit()
    conn.close()
    migrated = connect(tmp_path / "c.db")
    init_db(migrated)
    assert migrated.execute(
        "SELECT outcome_class FROM candidate_archives WHERE id=?",
        (archive_id,)).fetchone()[0] == "approved"


def test_a_second_connection_with_a_stale_decision_neither_rebuilds_nor_backs_up(
        tmp_path, monkeypatch):
    """2 接続が同時に旧形を見て rebuild に入っても、ロック取得後の再確認で
    後続は何もしない。backup も backfill も 1 回だけ。"""
    legacy, path = _legacy(tmp_path)
    _old_row(legacy, 1, "h1", "c1")
    legacy.commit()
    legacy.close()
    first = connect(path)
    second = connect(path)
    backfills = []
    real = archives.backfill_outcome_from_approvals

    def counting(conn):
        backfills.append(1)
        return real(conn)

    monkeypatch.setattr(archives, "backfill_outcome_from_approvals", counting)
    db_mod._migrate_candidate_archives(first, root=None)
    first.execute("UPDATE candidate_archives SET outcome_class='revoked'")
    first.commit()
    db_mod._rebuild_candidate_archives(second)
    assert backfills == [1]
    assert first.execute("SELECT outcome_class FROM candidate_archives"
                         ).fetchone()[0] == "revoked"
    backups = [p for p in tmp_path.iterdir()
               if ".bak-candidate-archives" in p.name]
    assert len(backups) == 1
    assert not second.in_transaction
