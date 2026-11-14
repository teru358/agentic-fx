"""検査尾の最低条件 (実重複 K 本以上・比較対象 2 以上) と遮断の優先順位 (実 sqlite)。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.store import history_conflicts as hc
from agentic_fx.store import history_import as hi
from agentic_fx.store import ohlcv
from tests.backtest.history_sync_support import conn_at

UTC = timezone.utc
START = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
W = timedelta(minutes=15)
K = 8
SEEN = datetime(2026, 11, 14, tzinfo=UTC)


def _t(i):
    return (START + W * i).isoformat()


def _row(i, close=148.1):
    return ("USDJPY", "15m", _t(i), 148.0, max(148.2, close), 147.9, close, 10.0, None)


def _seed(conn, n):
    ohlcv.import_history_bars(conn, [_row(i) for i in range(n)], source="mt5")


def _run(conn, rows, *, k=K, **kw):
    end = START + W * (max(i for i in range(10_000) if _t(i) <= max(r[2] for r in rows)) + 1)
    spec = hi.SeriesSpec("mt5", "15m", "USDJPY", START, end, end, lambda: list(rows))
    return hi.run_series(conn, spec, now=lambda: SEEN, inspection_tail_bars=k, **kw)


def _counts(conn):
    return (conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM history_conflicts").fetchone()[0])


def test_enough_unchanged_overlap_passes_and_the_new_rows_land(tmp_path):
    conn = conn_at(tmp_path)
    _seed(conn, 20)
    out = _run(conn, [_row(i) for i in range(25)])
    assert (out.status, out.inserted, out.unchanged, out.n_overlap) == ("ok", 5, 20, 20)


def test_overlap_of_k_minus_one_is_deferred_with_zero_writes_and_the_rows_are_not_written(tmp_path):
    conn = conn_at(tmp_path)
    _seed(conn, K - 1)
    out = _run(conn, [_row(i) for i in range(K + 4)])
    assert (out.status, out.deferred_reason, out.n_overlap) == (
        "deferred", "inspection_insufficient", K - 1)
    assert out.resume_from == START.isoformat() and out.inserted == 0
    assert _counts(conn) == (K - 1, 0)


def test_overlap_of_exactly_k_passes(tmp_path):
    conn = conn_at(tmp_path)
    _seed(conn, K)
    out = _run(conn, [_row(i) for i in range(K + 4)])
    assert out.status == "ok" and out.inserted == 4


def test_the_default_call_without_a_tail_requirement_is_unchanged(tmp_path):
    conn = conn_at(tmp_path)
    _seed(conn, 3)
    spec = hi.SeriesSpec("mt5", "15m", "USDJPY", START, START + W * 10, START + W * 10,
                         lambda: [_row(i) for i in range(10)])
    out = hi.run_series(conn, spec, now=lambda: SEEN)
    assert out.status == "ok" and out.inserted == 7


def test_the_circuit_wins_over_insufficient_inspection_even_with_a_short_overlap(tmp_path):
    conn = conn_at(tmp_path)
    _seed(conn, 7)                                   # K-1 本の重複
    rows = [_row(i, close=(150.0 if i < 2 else 148.1)) for i in range(7)] + [_row(8)]
    out = _run(conn, rows)                           # 新規 2 件の異値 / N_test=7 → 1% 超
    assert out.status == "conflict_circuit_open" and out.deferred_reason is None
    assert _counts(conn) == (7, 0)


@pytest.mark.parametrize("n_test,status", [(200, "completed_with_conflicts"), (199, "conflict_circuit_open")])
def test_one_percent_is_the_boundary_for_two_new_conflicts(tmp_path, n_test, status):
    conn = conn_at(tmp_path)
    _seed(conn, n_test)
    rows = [_row(i, close=(150.0 if i in (3, 9) else 148.1)) for i in range(n_test)]
    out = _run(conn, rows)
    assert out.status == status and out.n_test == n_test
    assert (_counts(conn)[1] == 2) is (status == "completed_with_conflicts")


@pytest.mark.parametrize("known,unchanged", [(8, 0), (7, 1)])
def test_a_known_only_overlap_with_fewer_than_two_comparable_rows_is_deferred(tmp_path, known, unchanged):
    conn = conn_at(tmp_path)
    _seed(conn, known + unchanged)
    for i in range(known):
        hc.upsert(conn, source="mt5", interval="15m", symbol="USDJPY", bar_time=_t(i),
                  history_epoch="none", existing=_row(i)[3:9],
                  incoming=_row(i, close=150.0)[3:9], origin="import",
                  seen_at="2026-11-13T00:00:00+00:00")
    conn.commit()
    rows = [_row(i, close=(150.0 if i < known else 148.1)) for i in range(known + unchanged)]
    out = _run(conn, rows)
    assert out.n_overlap == known + unchanged >= K and out.n_test == unchanged < 2
    assert (out.status, out.deferred_reason) == ("deferred", "inspection_insufficient")
    seen = conn.execute("SELECT SUM(seen_count) FROM history_conflicts").fetchone()[0]
    assert seen == known                              # 既知のカウントも更新しない (0 件)


def test_on_batch_runs_inside_the_batch_transaction_and_a_failure_rolls_the_batch_back(tmp_path):
    conn = conn_at(tmp_path)
    _seed(conn, 12)
    seen = []

    def progress(c, p):
        seen.append((c.in_transaction, p.batch_bar_time_end, p.resume_from, p.rows_in_batch))
    out = _run(conn, [_row(i) for i in range(20)], on_batch=progress, max_rows=4)
    assert out.status == "ok" and out.inserted == 8
    assert all(flag for flag, *_ in seen) and len(seen) == 5       # 20 行 / 4 行 batch
    assert seen[-1][2] is None and seen[0][2] == _t(4)

    conn2 = conn_at(tmp_path / "x") if (tmp_path / "x").mkdir() is None else None
    _seed(conn2, 12)

    def boom(c, p):
        raise RuntimeError("progress failed")
    out2 = _run(conn2, [_row(i) for i in range(20)], on_batch=boom, max_rows=4)
    assert out2.status == "failed" and out2.failure == "write_failed"
    assert _counts(conn2) == (12, 0)


def test_summarize_ranks_deferred_below_conflicts_and_above_ok_and_keeps_rc_zero(tmp_path):
    def mk(status):
        return hi.SeriesOutcome("mt5", "15m", "X", status, "a", "b", "c", "op")
    assert hi.summarize([mk("ok"), mk("deferred")]).result == "deferred"
    assert hi.summarize([mk("ok"), mk("deferred")]).rc == 0
    assert hi.summarize([mk("completed_with_conflicts"), mk("deferred")]).result == (
        "completed_with_conflicts")
    assert hi.summarize([mk("failed"), mk("deferred")]).result == "failed"
    assert hi.summarize([mk("conflict_circuit_open"), mk("deferred")]).rc == 4
    assert hi.summarize([mk("ok")]).result == "ok"
