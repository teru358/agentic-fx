"""始端が holdout 境界 B のとき、検査尾は B より前の既存 key から選ぶ。尾は比較・監査だけに使う。"""
from __future__ import annotations

from datetime import datetime, timedelta

from agentic_fx.backtest import history_sync as hs
from agentic_fx.core.market_hours import is_market_open
from agentic_fx.store import history_sync as store
from agentic_fx.store.ohlcv import history_epoch
from tests.backtest.history_sync_support import (
    SYMBOL, UTC, WIDTH, FakeBridge, conn_at, count, grid, make_settings, seed)
from tests.backtest.test_history_sync import B1, E, T, _day_bounds, _run

PRE = B1 - timedelta(days=2)          # B より前に置く履歴の始端
FRIDAY = datetime(2026, 10, 9, tzinfo=UTC)   # B (日曜 12:00) の直前の開場日。21:00Z に閉場 (夏時間)


def _boundary_fixture(tmp_path, *, lookback=31, pre=PRE, **seed_kw):
    conn = conn_at(tmp_path)
    seed(conn, SYMBOL, pre, E, **seed_kw)
    return conn, make_settings(holdout_months=1, lookback_days=lookback)


def _before_b(conn):
    return conn.execute("SELECT bar_time, open, high, low, close FROM ohlcv_history "
                        "WHERE bar_time < ? ORDER BY bar_time", (B1.isoformat(),)).fetchall()


def _recent(result):
    return next(o for o in result.ops if o.operation == "recent")


def test_a_recent_whose_start_is_b_is_fetched_with_a_tail_before_b(tmp_path):
    conn, settings = _boundary_fixture(tmp_path)
    before = [tuple(r) for r in _before_b(conn)]
    bridge = FakeBridge()
    result = _run(conn, settings, bridge)
    recent = _recent(result)
    assert recent.start == B1 and recent.status == "ok" and recent.requests > 0
    assert min(c[1] for c in bridge.calls) < B1                # 尾は B より前から取る
    assert result.cursors[SYMBOL] == E.isoformat() and result.rc == 0
    assert [tuple(r) for r in _before_b(conn)] == before


def test_the_example_settings_boundary_day_gap_progresses(tmp_path):
    conn, settings = _boundary_fixture(tmp_path, lookback=2)
    lo, hi = _day_bounds(B1.date())
    conn.execute("DELETE FROM ohlcv_history WHERE bar_time >= ? AND bar_time < ?",
                 (B1.isoformat(), hi.isoformat()))
    conn.commit()
    before = [tuple(r) for r in _before_b(conn)]
    bridge = FakeBridge()
    _run(conn, settings, bridge, until_done=True)
    refilled = conn.execute("SELECT COUNT(*) FROM ohlcv_history WHERE bar_time >= ? "
                            "AND bar_time < ?", (B1.isoformat(), hi.isoformat())).fetchone()[0]
    assert refilled > 0                                        # 境界日の B 以後は埋まる
    assert [tuple(r) for r in _before_b(conn)] == before
    assert any(c[1] < B1 for c in bridge.calls)


def test_no_history_before_b_is_still_inspection_insufficient_with_zero_requests(tmp_path):
    conn, settings = _boundary_fixture(tmp_path, pre=B1)
    bridge = FakeBridge()
    result = _run(conn, settings, bridge)
    recent = _recent(result)
    assert (recent.status, recent.reason, recent.requests) == (
        "deferred", "inspection_insufficient", 0)
    assert bridge.calls == [] and result.cursors[SYMBOL] is None


def test_a_missing_bar_in_the_tail_range_is_not_inserted(tmp_path):
    hole = FRIDAY + timedelta(hours=20)
    assert is_market_open(hole)
    conn, settings = _boundary_fixture(tmp_path, skip=lambda t: t == hole)
    before = [tuple(r) for r in _before_b(conn)]
    result = _run(conn, settings, FakeBridge())
    assert _recent(result).status == "ok"
    assert [tuple(r) for r in _before_b(conn)] == before
    assert conn.execute("SELECT 1 FROM ohlcv_history WHERE bar_time=?",
                        (hole.isoformat(),)).fetchone() is None


def test_a_tail_conflict_is_audited_without_touching_history_and_counts_in_the_denominator(
        tmp_path):
    bad = FRIDAY + timedelta(hours=19, minutes=30)
    conn, settings = _boundary_fixture(tmp_path)
    epoch = history_epoch(conn)
    before = [tuple(r) for r in _before_b(conn)]
    result = _run(conn, settings, FakeBridge(change={(SYMBOL, bad): 0.4}))
    recent = _recent(result)
    assert recent.status == "completed_with_conflicts" and recent.conflicted_new == 1
    rows = conn.execute("SELECT bar_time FROM history_conflicts").fetchall()
    assert [r["bar_time"] for r in rows] == [bad.isoformat()]
    assert [tuple(r) for r in _before_b(conn)] == before      # 既存値は上書きしない
    assert history_epoch(conn) == epoch
    second = _run(conn, settings, FakeBridge(change={(SYMBOL, bad): 0.4}))
    again = _recent(second)
    assert again.conflicted_known == 1 and again.conflicted_new == 0
    assert history_epoch(conn) == epoch


def test_tail_conflicts_take_priority_and_block_everything(tmp_path):
    conn, settings = _boundary_fixture(tmp_path)
    count_before = count(conn)
    # 尾の 8 本 + 取得範囲の 13 本を異値にして合計 21 件 (> 20)。尾の衝突も分母・件数に入り遮断が優先
    tail = [t for t in grid(FRIDAY + timedelta(hours=14), B1) if is_market_open(t)]
    inside = [t for t in grid(datetime(2026, 10, 12, 1, tzinfo=UTC), E)][:13]
    change = {(SYMBOL, t): 0.4 for t in [*tail[-8:], *inside]}
    assert len(change) == 21
    result = _run(conn, settings, FakeBridge(change=change))
    assert _recent(result).status == "conflict_circuit_open" and result.rc == 4
    assert count(conn) == count_before and count(conn, table="history_conflicts") == 0
    assert result.cursors[SYMBOL] is None


def test_the_holdout_content_is_unchanged_by_a_boundary_start_run(tmp_path):
    conn, settings = _boundary_fixture(tmp_path)

    def holdout():
        return [tuple(r) for r in conn.execute(
            "SELECT bar_time, open, high, low, close, volume FROM ohlcv_history "
            "WHERE bar_time >= ? ORDER BY bar_time", (B1.isoformat(),))]
    before = holdout()
    epoch = history_epoch(conn)
    _run(conn, settings, FakeBridge())
    assert holdout() == before and history_epoch(conn) == epoch


def _save_resume(conn, *, range_start, tail_start, prefix_end):
    store.write_txn(conn, lambda c: store.save_resume(
        c, source="mt5", interval="15m", symbol=SYMBOL, op_key="recent",
        range_start=range_start, range_end=E, tail_start=tail_start, prefix_end=prefix_end,
        now=T), sleep=lambda _s: None)


def test_a_resume_point_whose_tail_starts_before_b_is_valid_while_its_range_is_not(tmp_path):
    conn, settings = _boundary_fixture(tmp_path)
    tail_start = B1 - timedelta(hours=2)
    _save_resume(conn, range_start=B1, tail_start=tail_start, prefix_end=B1)
    bridge = FakeBridge()
    result = _run(conn, settings, bridge)
    first = result.ops[0]
    assert first.resumed and first.status == "ok"
    assert min(c[1] for c in bridge.calls) <= tail_start
    assert conn.execute("SELECT COUNT(*) FROM history_sync_resume").fetchone()[0] == 0


def test_a_resume_point_whose_range_starts_before_b_is_stale_after_b_moved(tmp_path):
    conn, settings = _boundary_fixture(tmp_path)
    _save_resume(conn, range_start=B1 - timedelta(hours=1), tail_start=B1 - timedelta(hours=3),
                 prefix_end=B1 - timedelta(hours=1))
    result = _run(conn, settings, FakeBridge())
    assert not result.ops[0].resumed
