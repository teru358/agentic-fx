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


def test_find_by_mission_content_prefers_row_with_archive_path_over_older_null(
        tmp_path):
    """codex 1周目 I2 是正の pin: 同一 mission/content_hash で古い行の
    `archive_path` が NULL (GC 済み等)、後続の新しい行が有効な場合、
    最古行 (旧実装) ではなく有効な行を返す。"""
    conn = _conn(tmp_path)
    _insert(conn, mission_id=20, content_hash="dup-hash",
            artifact_hash="old-artifact", archive_path=None)
    newer_id = _insert(conn, mission_id=20, content_hash="dup-hash",
                       artifact_hash="new-artifact",
                       archive_path="plugins/_archive/20/new-artifact")
    row = candidate_archives.find_by_mission_content(
        conn, mission_id=20, content_hash="dup-hash")
    assert row["id"] == newer_id
    assert row["archive_path"] == "plugins/_archive/20/new-artifact"


def test_find_by_mission_content_returns_newest_when_both_have_archive_path(
        tmp_path):
    """同一 mission/content_hash で両方とも `archive_path` が有効な場合、
    新しい (id が大きい) 方を返す。"""
    conn = _conn(tmp_path)
    _insert(conn, mission_id=21, content_hash="dup-hash-2",
            artifact_hash="artifact-old",
            archive_path="plugins/_archive/21/old")
    newer_id = _insert(conn, mission_id=21, content_hash="dup-hash-2",
                       artifact_hash="artifact-new",
                       archive_path="plugins/_archive/21/new")
    row = candidate_archives.find_by_mission_content(
        conn, mission_id=21, content_hash="dup-hash-2")
    assert row["id"] == newer_id
    assert row["archive_path"] == "plugins/_archive/21/new"


def test_find_by_mission_content_returns_newest_null_row_when_all_null(
        tmp_path):
    """全行の `archive_path` が NULL のとき、最新 (id 降順) の行を返す
    (「表示は『パス欠落』」の対象行として最新を選ぶ)。"""
    conn = _conn(tmp_path)
    _insert(conn, mission_id=22, content_hash="dup-hash-3",
            artifact_hash="artifact-old-null", archive_path=None)
    newer_id = _insert(conn, mission_id=22, content_hash="dup-hash-3",
                       artifact_hash="artifact-new-null", archive_path=None)
    row = candidate_archives.find_by_mission_content(
        conn, mission_id=22, content_hash="dup-hash-3")
    assert row["id"] == newer_id
    assert row["archive_path"] is None


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


def test_find_by_mission_content_ignores_row_whose_artifact_hash_equals_key(
        tmp_path):
    """ローカル approval-quality 1 周目 #C3x (2026-09-12): 検索キーは
    `content_hash` のみ。`artifact_hash` が検索キーと同値の別候補行 (罠) が
    同一 mission にあっても拾ってはいけない。既存テストは `content_hash`
    側だけを変えていたため、WHERE を
    `(content_hash=? OR artifact_hash=?)` に緩める変異が SURVIVED だった。"""
    conn = _conn(tmp_path)
    _insert(conn, mission_id=30, content_hash="target", artifact_hash="ah-a",
            archive_path="plugins/_archive/30/a")
    _insert(conn, mission_id=30, content_hash="other", artifact_hash="target",
            archive_path="plugins/_archive/30/trap")
    row = candidate_archives.find_by_mission_content(
        conn, mission_id=30, content_hash="target")
    assert row is not None
    assert row["archive_path"] == "plugins/_archive/30/a"
    assert row["artifact_hash"] == "ah-a"
