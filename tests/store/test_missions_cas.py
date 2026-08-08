"""missions.finish の CAS 化 + 起動時回収 (プラン8, 設計書 §4.7/§4.8)。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from agentic_fx.store import missions, signals
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    conn = connect(tmp_path / "x.db")
    init_db(conn)
    return conn


def test_finish_returns_true_on_first_call(tmp_path):
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    assert missions.finish(conn, mid, "completed", {"a": 1}, [], NOW) is True
    row = conn.execute("SELECT status FROM missions WHERE id=?", (mid,)).fetchone()
    assert row["status"] == "completed"


def test_finish_returns_false_on_second_call_and_does_not_overwrite(tmp_path):
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    assert missions.finish(conn, mid, "completed", {"a": 1}, [], NOW) is True
    assert missions.finish(conn, mid, "failed", None, [], NOW) is False
    row = conn.execute(
        "SELECT status, output_json FROM missions WHERE id=?", (mid,)).fetchone()
    assert row["status"] == "completed"  # 上書きされていない
    assert row["output_json"] == '{"a": 1}'


def test_recover_interrupted_finalizes_running_missions(tmp_path):
    conn = _conn(tmp_path)
    mid1 = missions.start(conn, "trade", "local", "m", NOW)
    mid2 = missions.start(conn, "trade", "local", "m", NOW)
    missions.finish(conn, mid2, "completed", None, [], NOW)  # 既に終端済み

    result = missions.recover_interrupted(conn, now=NOW, max_requeue=2)

    assert result["missions_recovered"] == 1
    row1 = conn.execute("SELECT status FROM missions WHERE id=?", (mid1,)).fetchone()
    assert row1["status"] == "interrupted"
    row2 = conn.execute("SELECT status FROM missions WHERE id=?", (mid2,)).fetchone()
    assert row2["status"] == "completed"  # 触られない


def test_recover_interrupted_requeues_claimed_signals_same_transaction(tmp_path):
    """running のまま残った mission が claim していた signal は、
    lease_min の経過を待たず同一トランザクションで requeue される
    (codex C-5 — 分離すると mission は終端済みなのに signal は lease 満了
    まで不可視、という不整合窓が生じる)。"""
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    signals.add(conn, plugin="p", content_hash="h", pair="USDJPY",
               timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
               payload={}, now=NOW)
    claimed = signals.claim_oldest(conn, mission_id=mid, now=NOW,
                                   freshness_bars=None)
    assert claimed is not None

    result = missions.recover_interrupted(conn, now=NOW, max_requeue=2)

    assert result["signals_requeued"] == 1
    row = conn.execute(
        "SELECT status, claimed_by_mission_id FROM signals WHERE id=?",
        (claimed["id"],)).fetchone()
    assert row["status"] == "pending"
    assert row["claimed_by_mission_id"] is None


def test_recover_interrupted_abandons_signal_over_requeue_limit(tmp_path):
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    signals.add(conn, plugin="p", content_hash="h", pair="USDJPY",
               timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
               payload={}, now=NOW)
    claimed = signals.claim_oldest(conn, mission_id=mid, now=NOW,
                                   freshness_bars=None)
    conn.execute("UPDATE signals SET requeue_count=2 WHERE id=?", (claimed["id"],))
    conn.commit()

    result = missions.recover_interrupted(conn, now=NOW, max_requeue=2)

    assert result["signals_abandoned"] == 1
    row = conn.execute(
        "SELECT status FROM signals WHERE id=?", (claimed["id"],)).fetchone()
    assert row["status"] == "abandoned"


def test_recover_interrupted_is_atomic_no_partial_state_on_failure(tmp_path):
    """途中で例外が起きても missions/signals どちらも変更されない
    (単一トランザクション — codex C-5)。

    I11 対応: `sqlite3.Connection.execute` は C 拡張型の read-only
    属性であり `monkeypatch.setattr(conn, "execute", ...)` は
    `AttributeError: 'sqlite3.Connection' object attribute 'execute'
    is read-only` で失敗し、テストがそもそも実行できない (レビュー
    I11、実測で確認済み)。DB 側の決定論的な failure point として
    SQLite trigger を使う — `signals` の `status` を `'pending'` に
    更新する UPDATE (`recover_interrupted` の requeue 分岐) だけを
    確実に失敗させ、`recover_interrupted` 自身の `except BaseException:
    conn.rollback(); raise` 経路を実際に通す。
    """
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    signals.add(conn, plugin="p", content_hash="h", pair="USDJPY",
               timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
               payload={}, now=NOW)
    claimed = signals.claim_oldest(conn, mission_id=mid, now=NOW,
                                   freshness_bars=None)
    assert claimed is not None

    conn.execute("""
        CREATE TRIGGER fail_on_signal_requeue
        BEFORE UPDATE OF status ON signals
        WHEN NEW.status = 'pending'
        BEGIN
            SELECT RAISE(ABORT, 'simulated failure');
        END;
    """)
    conn.commit()

    with pytest.raises(sqlite3.Error):
        missions.recover_interrupted(conn, now=NOW, max_requeue=2)

    row = conn.execute("SELECT status FROM missions WHERE id=?", (mid,)).fetchone()
    assert row["status"] == "running"  # ロールバック済み (missions 側も巻き戻る)
    sig_row = conn.execute(
        "SELECT status FROM signals WHERE id=?", (claimed["id"],)).fetchone()
    assert sig_row["status"] == "claimed"  # signals 側も巻き戻る


# ---- I2: recover_interrupted 末尾の conn.commit() が pin されていない -------

def test_i2_conn_commit_required_in_recover_interrupted(tmp_path):
    """回収後、別の接続から見えることを確認 (conn.commit() がないと未コミット)。

    ファイル DB で recover_interrupted を呼び、別の接続を開いて
    mission が 'interrupted'、signal が 'pending' になっていることを
    確認する (file DB 必須 — :memory: では別接続から見えない)。
    """
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    signals.add(conn, plugin="p", content_hash="h", pair="USDJPY",
               timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
               payload={}, now=NOW)
    claimed = signals.claim_oldest(conn, mission_id=mid, now=NOW,
                                   freshness_bars=None)
    assert claimed is not None

    result = missions.recover_interrupted(conn, now=NOW, max_requeue=2)

    assert result["missions_recovered"] == 1
    assert result["signals_requeued"] == 1

    # 別の接続を開いて確認 (commit されていなければ見えない)
    conn2 = connect(tmp_path / "x.db")
    row = conn2.execute("SELECT status FROM missions WHERE id=?", (mid,)).fetchone()
    assert row["status"] == "interrupted"
    sig_row = conn2.execute(
        "SELECT status FROM signals WHERE id=?", (claimed["id"],)).fetchone()
    assert sig_row["status"] == "pending"
    conn2.close()


# ---- I1: BEGIN IMMEDIATE が pin されていない (最難) ----------------------

def test_i1_begin_immediate_locks_writes(tmp_path):
    """BEGIN IMMEDIATE の本来の仕事は「最初に write lock を取る」ことを確認。

    conn1 で recover_interrupted を実行。その途中で trace callback から
    conn2 で BEGIN IMMEDIATE を試みる。callback は SELECT 実行時に発火する
    ため、その時点で既に BEGIN IMMEDIATE が済んでいれば conn2 は BUSY になる。
    callback の中で release (rollback) して conn1 が進めるようにする。
    """
    conn1 = _conn(tmp_path)
    mid = missions.start(conn1, "trade", "local", "m", NOW)
    signals.add(conn1, plugin="p", content_hash="h", pair="USDJPY",
               timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
               payload={}, now=NOW)
    claimed = signals.claim_oldest(conn1, mission_id=mid, now=NOW,
                                   freshness_bars=None)
    assert claimed is not None

    # conn2 は timeout=0 で即エラー化
    conn2 = sqlite3.connect(tmp_path / "x.db", timeout=0)

    outcome: list[str] = []

    def trace_callback(stmt: str):
        # "FROM missions WHERE status='running'" を含む文が来たときだけ試みる
        if "FROM missions WHERE status='running'" not in stmt or outcome:
            return
        try:
            conn2.execute("BEGIN IMMEDIATE")
            outcome.append("acquired")
            conn2.rollback()  # release して conn1 が進む
        except sqlite3.OperationalError:
            outcome.append("busy")

    conn1.set_trace_callback(trace_callback)
    missions.recover_interrupted(conn1, now=NOW, max_requeue=2)

    # conn2 は write lock を取れなかった
    assert outcome == ["busy"], \
        f"Expected BEGIN IMMEDIATE to be blocked (busy), got {outcome}"

    conn2.close()


# ---- I4: 原子性テストが SQLite trigger 依存 (Python-side failure) ---------

class _ConnProxy:
    """conn.execute を intercept するプロキシ (read-only execute をバイパスする)。"""
    def __init__(self, conn, intercept_fn=None):
        self._conn = conn
        self._intercept_fn = intercept_fn

    def execute(self, sql, *args, **kwargs):
        if self._intercept_fn:
            self._intercept_fn(sql)
        return self._conn.execute(sql, *args, **kwargs)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_i4_atomicity_with_python_exception_on_signals_update(tmp_path):
    """Python 側の失敗注入で復旧テスト。

    conn.execute をプロキシでラップして「signals の UPDATE が失敗する」
    ようにし、recover_interrupted が例外を出した後で missions がまだ
    'running' のまま巻き戻っていることを確認する (DB trigger に依存しない)。
    """
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    signals.add(conn, plugin="p", content_hash="h", pair="USDJPY",
               timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
               payload={}, now=NOW)
    claimed = signals.claim_oldest(conn, mission_id=mid, now=NOW,
                                   freshness_bars=None)
    assert claimed is not None

    # プロキシで execute をインターセプト
    def intercept_fn(sql):
        if "UPDATE signals SET status='pending'" in sql:
            raise RuntimeError("simulated signals update failure")

    conn_proxy = _ConnProxy(conn, intercept_fn)

    # recover_interrupted は例外を出す
    with pytest.raises(RuntimeError, match="simulated signals update failure"):
        missions.recover_interrupted(conn_proxy, now=NOW, max_requeue=2)

    # missions は rollback で巻き戻っている (実際の conn で確認)
    row = conn.execute("SELECT status FROM missions WHERE id=?", (mid,)).fetchone()
    assert row["status"] == "running"


# ---- m1: rollback の失敗注入が requeue 分岐しか踏んでいない -----------

def test_m1_abandon_branch_failure_injection(tmp_path):
    """abandon 分岐 (requeue_count >= max_requeue) で例外が起きるケース。

    abandon UPDATE を失敗させて recover_interrupted が例外を出した後,
    missions がまだ 'running' のまま巻き戻っていることを確認。
    """
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    signals.add(conn, plugin="p", content_hash="h", pair="USDJPY",
               timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
               payload={}, now=NOW)
    claimed = signals.claim_oldest(conn, mission_id=mid, now=NOW,
                                   freshness_bars=None)
    # abandon させるために requeue_count を max_requeue 以上にする
    conn.execute("UPDATE signals SET requeue_count=2 WHERE id=?",
                (claimed["id"],))
    conn.commit()

    # プロキシで execute をインターセプト
    def intercept_fn(sql):
        if "UPDATE signals SET status='abandoned'" in sql:
            raise RuntimeError("simulated signals abandon failure")

    conn_proxy = _ConnProxy(conn, intercept_fn)

    with pytest.raises(RuntimeError, match="simulated signals abandon failure"):
        missions.recover_interrupted(conn_proxy, now=NOW, max_requeue=2)

    # missions は rollback で巻き戻っている (実際の conn で確認)
    row = conn.execute("SELECT status FROM missions WHERE id=?", (mid,)).fetchone()
    assert row["status"] == "running"


# ---- m2: max_requeue の境界が片側しか踏まれていない ----------------------

def test_m2_requeue_count_eq_max_requeue_becomes_abandoned(tmp_path):
    """requeue_count=1, max_requeue=1 → abandoned になること。

    >= の条件を > に変えたときに red になるケースの確認。
    """
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    signals.add(conn, plugin="p", content_hash="h", pair="USDJPY",
               timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
               payload={}, now=NOW)
    claimed = signals.claim_oldest(conn, mission_id=mid, now=NOW,
                                   freshness_bars=None)
    # requeue_count を 1 にセット
    conn.execute("UPDATE signals SET requeue_count=1 WHERE id=?",
                (claimed["id"],))
    conn.commit()

    # max_requeue=1 で recover_interrupted を呼ぶ
    result = missions.recover_interrupted(conn, now=NOW, max_requeue=1)

    # requeue_count=1 >= max_requeue=1 だから abandoned になるはず
    assert result["signals_abandoned"] == 1
    assert result["signals_requeued"] == 0
    row = conn.execute(
        "SELECT status FROM signals WHERE id=?", (claimed["id"],)).fetchone()
    assert row["status"] == "abandoned"


def test_m2_requeue_count_lt_max_requeue_becomes_requeued(tmp_path):
    """requeue_count=0, max_requeue=2 → requeued (count が 1 になる) こと。

    >= の条件を > に変えたときに取り落とされるケースの確認。
    """
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    signals.add(conn, plugin="p", content_hash="h", pair="USDJPY",
               timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
               payload={}, now=NOW)
    claimed = signals.claim_oldest(conn, mission_id=mid, now=NOW,
                                   freshness_bars=None)
    # requeue_count は既定値 0

    # max_requeue=2 で recover_interrupted を呼ぶ
    result = missions.recover_interrupted(conn, now=NOW, max_requeue=2)

    # requeue_count=0 < max_requeue=2 だから requeued になり count++されるはず
    assert result["signals_requeued"] == 1
    assert result["signals_abandoned"] == 0
    row = conn.execute(
        "SELECT requeue_count, status FROM signals WHERE id=?",
        (claimed["id"],)).fetchone()
    assert row["status"] == "pending"
    assert row["requeue_count"] == 1  # +1 されている
