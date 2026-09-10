from datetime import datetime, timezone

from agentic_fx.store import candidate_archives
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


def _conn(tmp_path):
    conn = connect(tmp_path / "candidate.db")
    init_db(conn)
    return conn


def _insert(conn, **overrides):
    values = dict(mission_id=3, name="rsi_v2", content_hash="content-a",
                  artifact_hash="artifact-a", archive_path="plugins/_archive/3/a",
                  pair="USDJPY", metrics={"pf": 1.25, "trades": 40}, now=NOW,
                  commit=True)
    values.update(overrides)
    return candidate_archives.insert(conn, **values)


def test_insert_and_list_by_mission_returns_decoded_metrics_in_id_order(tmp_path):
    conn = _conn(tmp_path)
    first = _insert(conn)
    second = _insert(conn, artifact_hash="artifact-b", content_hash="content-b",
                     archive_path="plugins/_archive/3/b", metrics={"pf": 1.5})
    _insert(conn, mission_id=4, artifact_hash="artifact-c")
    rows = candidate_archives.list_by_mission(conn, 3)
    assert [r["id"] for r in rows] == [first, second]
    assert rows[0]["metrics"] == {"pf": 1.25, "trades": 40}
    assert "metrics_json" not in rows[0]


def test_insert_is_idempotent_for_mission_and_artifact_hash(tmp_path):
    conn = _conn(tmp_path)
    first = _insert(conn)
    duplicate = _insert(conn, name="changed", content_hash="changed",
                        archive_path="changed", pair="EURUSD", metrics={"pf": 9})
    assert duplicate == first
    assert len(candidate_archives.list_by_mission(conn, 3)) == 1
    # 段 0 pin (2026-09-10): 接続の last_insert_rowid が別行を指していても
    # 既存行の id を返す (INSERT OR IGNORE 後の lastrowid は前回挿入行を指す)。
    other = _insert(conn, mission_id=9, artifact_hash="artifact-z")
    assert other != first
    assert _insert(conn) == first


def test_clear_path_and_find_without_path(tmp_path):
    conn = _conn(tmp_path)
    cleared = _insert(conn)
    _insert(conn, mission_id=4, artifact_hash="artifact-b",
            archive_path=None)
    _insert(conn, mission_id=5, artifact_hash="artifact-c",
            archive_path="plugins/_archive/5/c")
    candidate_archives.clear_path(conn, cleared)
    rows = candidate_archives.find_without_path(conn)
    assert [(r["mission_id"], r["archive_path"]) for r in rows] == [
        (3, None), (4, None)]


def test_insert_and_clear_path_honor_caller_owned_transaction(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("BEGIN IMMEDIATE")
    archive_id = _insert(conn, commit=False)
    conn.rollback()
    assert candidate_archives.list_by_mission(conn, 3) == []

    archive_id = _insert(conn)
    conn.execute("BEGIN IMMEDIATE")
    candidate_archives.clear_path(conn, archive_id, commit=False)
    conn.rollback()
    assert candidate_archives.list_by_mission(conn, 3)[0]["archive_path"] is not None
