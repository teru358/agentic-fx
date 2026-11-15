"""同期の必須の状態書込みの失敗は、衝突・遮断より優先して技術失敗になる。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agentic_fx.backtest import history_sync as hs
from agentic_fx.store import history_sync as store
from tests.backtest.history_sync_support import SYMBOL, UTC, FakeBridge
from tests.backtest.test_history_sync import E, T, _run, _seeded


def _boom(*_a, **_k):
    raise RuntimeError("write failed")


def _conflict_bridge():
    return FakeBridge(change={(SYMBOL, datetime(2026, 11, 11, 7, tzinfo=UTC)): 0.3})


def test_a_cursor_write_failure_is_a_technical_failure_and_the_cursor_stays(tmp_path, monkeypatch):
    conn, settings = _seeded(tmp_path)
    monkeypatch.setattr(store, "advance_cursor", _boom)
    result = _run(conn, settings)
    assert result.result in ("partial", "failed")
    assert result.rc == 1
    assert result.cursors[SYMBOL] is None
    assert any(w.startswith("series_state") for w in result.required_write_failures)
    assert conn.execute("SELECT rc, failure_code FROM history_sync_runs").fetchone()[0] == 1


def test_a_cursor_write_failure_beats_a_conflict_result(tmp_path, monkeypatch):
    conn, settings = _seeded(tmp_path)
    monkeypatch.setattr(store, "advance_cursor", _boom)
    result = _run(conn, settings, _conflict_bridge())
    assert result.conflicted >= 1
    assert result.rc == 1 and result.result in ("partial", "failed")


def test_a_reconcile_write_failure_is_a_technical_failure(tmp_path, monkeypatch):
    conn, settings = _seeded(tmp_path)
    monkeypatch.setattr(hs._Run, "_reconcile_kind", _boom)
    result = _run(conn, settings)
    assert result.rc == 1 and "reconcile" in result.required_write_failures


def test_a_reconcile_write_failure_beats_the_circuit_open_result(tmp_path, monkeypatch):
    conn, settings = _seeded(tmp_path)
    monkeypatch.setattr(hs._Run, "_reconcile_kind", _boom)
    result = _run(conn, settings, FakeBridge(shift=5.0))
    assert result.rc == 1 and result.result in ("partial", "failed")


def test_an_unprocessed_save_failure_blocks_the_cursor_advance_in_the_same_transaction(
        tmp_path, monkeypatch):
    conn, settings = _seeded(tmp_path)
    old = datetime(2026, 11, 3, 12, tzinfo=UTC)
    store.write_txn(conn, lambda c: store.advance_cursor(
        c, "mt5", "15m", SYMBOL, last_processed_e=old, run_id="old", now=T),
        sleep=lambda _s: None)
    real = store.observe_gap

    def fail_unprocessed(c, **kw):
        if kw["kind"] == "unprocessed":
            raise RuntimeError("write failed")
        return real(c, **kw)
    monkeypatch.setattr(store, "observe_gap", fail_unprocessed)
    result = _run(conn, settings)
    assert result.rc == 1
    assert result.cursors[SYMBOL] == old.isoformat()       # 前進していない
    assert conn.execute("SELECT COUNT(*) FROM history_sync_gaps WHERE kind='unprocessed'"
                        ).fetchone()[0] == 0


def test_a_cursor_failure_after_unprocessed_rolls_back_the_unprocessed_rows(tmp_path, monkeypatch):
    conn, settings = _seeded(tmp_path)
    old = datetime(2026, 11, 3, 12, tzinfo=UTC)
    store.write_txn(conn, lambda c: store.advance_cursor(
        c, "mt5", "15m", SYMBOL, last_processed_e=old, run_id="old", now=T),
        sleep=lambda _s: None)
    real = store.advance_cursor

    def fail_new(c, source, interval, symbol, *, last_processed_e, run_id, now):
        if run_id != "old":
            raise RuntimeError("write failed")
        return real(c, source, interval, symbol, last_processed_e=last_processed_e,
                    run_id=run_id, now=now)
    monkeypatch.setattr(store, "advance_cursor", fail_new)
    result = _run(conn, settings)
    assert result.rc == 1 and result.cursors[SYMBOL] == old.isoformat()
    # 2 つの書込みは 1 transaction: cursor が進まないなら unprocessed 行も残らない
    assert conn.execute("SELECT COUNT(*) FROM history_sync_gaps WHERE kind='unprocessed'"
                        ).fetchone()[0] == 0


def test_a_run_without_write_failures_is_unchanged(tmp_path):
    conn, settings = _seeded(tmp_path)
    result = _run(conn, settings)
    assert result.rc == 0 and result.required_write_failures == []
