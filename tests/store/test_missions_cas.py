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
