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


# ---- approval-quality 設計書 §C: find_by_mission_content ------------------

def test_find_by_mission_content_matches_on_content_hash_despite_artifact_hash_drift(
        tmp_path):
    """run12 観測 C の実データ形: backtest 時点の archive 行は
    `artifact_hash` (self-test 込み) を持つが、提出時点で agent が
    self-test を書き直すと提出後の `artifact_hash` はずれる。
    `content_hash` (plugin.py/config.yaml 相当) は両時点で不変 — 引き当て
    キーには `content_hash` だけを使うため、`artifact_hash` が食い違って
    いても見つかる。"""
    conn = _conn(tmp_path)
    _insert(conn, mission_id=7, content_hash="stable-content",
           artifact_hash="artifact-at-backtest-time",
           archive_path="plugins/_archive/7/artifact-at-backtest-time")
    row = candidate_archives.find_by_mission_content(
        conn, mission_id=7, content_hash="stable-content")
    assert row is not None
    assert row["archive_path"] == "plugins/_archive/7/artifact-at-backtest-time"
    assert row["artifact_hash"] == "artifact-at-backtest-time"


def test_find_by_mission_content_returns_none_when_no_archive(tmp_path):
    """GC 済み・失敗終端等で該当 mission に archive が無い場合。"""
    conn = _conn(tmp_path)
    assert candidate_archives.find_by_mission_content(
        conn, mission_id=42, content_hash="nope") is None


def test_find_by_mission_content_returns_only_matching_content_hash_row(
        tmp_path):
    """同一 mission に複数候補があるとき、指定した `content_hash` の 1 件
    だけを返す (別候補の行を誤って返さない)。"""
    conn = _conn(tmp_path)
    _insert(conn, mission_id=8, content_hash="hash-a", artifact_hash="a",
           archive_path="plugins/_archive/8/a")
    _insert(conn, mission_id=8, content_hash="hash-b", artifact_hash="b",
           archive_path="plugins/_archive/8/b")
    row = candidate_archives.find_by_mission_content(
        conn, mission_id=8, content_hash="hash-b")
    assert row["archive_path"] == "plugins/_archive/8/b"


def test_find_by_mission_content_does_not_match_other_mission(tmp_path):
    conn = _conn(tmp_path)
    _insert(conn, mission_id=9, content_hash="shared-hash", artifact_hash="a")
    assert candidate_archives.find_by_mission_content(
        conn, mission_id=10, content_hash="shared-hash") is None


def test_find_by_mission_content_does_not_use_artifact_hash_as_key(tmp_path):
    """段 0 変異狙い: 検索キーを `artifact_hash` に戻す変異が red になる
    ための pin。content_hash 一致・artifact_hash 不一致でも見つかること
    自体が上の主 pin と同じ内容だが、こちらは `artifact_hash` を検索引数
    に一切渡さずとも解決できることを明示する。"""
    conn = _conn(tmp_path)
    _insert(conn, mission_id=11, content_hash="c11", artifact_hash="drifted")
    row = candidate_archives.find_by_mission_content(
        conn, mission_id=11, content_hash="c11")
    assert row is not None


def test_insert_does_not_commit_by_default(tmp_path):
    """ローカル 1 周目 #17 (2026-09-10): `commit` の既定が False
    (caller-owned tx)。既存の caller-owned tx テストは `commit=False` を明示
    して呼び、ヘルパ `_insert` も `commit=True` を既定で埋めているので、
    ライブラリ既定値は一度も踏まれず True へ変えても緑のままだった。
    T3 が SAVEPOINT の中で insert する前提そのもの。ヘルパを通さず直に呼ぶ。"""
    conn = _conn(tmp_path)
    conn.execute("BEGIN IMMEDIATE")
    candidate_archives.insert(          # commit を渡さない = ライブラリ既定
        conn, mission_id=3, name="rsi_v2", content_hash="content-a",
        artifact_hash="artifact-a", archive_path="plugins/_archive/3/a",
        pair="USDJPY", metrics={"pf": 1.25}, now=NOW)
    conn.rollback()
    assert candidate_archives.list_by_mission(conn, 3) == []
