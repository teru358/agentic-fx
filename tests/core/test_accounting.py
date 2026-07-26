from datetime import datetime, timedelta, timezone

from agentic_fx.core.accounting import (
    daily_start_equity, drawdown_pct, record_snapshot,
)
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)  # 水曜


def _conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    return c


def test_hwm_tracks_peak(tmp_path):
    c = _conn(tmp_path)
    r1 = record_snapshot(c, now=NOW, balance=100.0, equity=100.0)
    assert r1["hwm"] == 100.0
    r2 = record_snapshot(c, now=NOW + timedelta(hours=1), balance=100.0,
                         equity=110.0)
    assert r2["hwm"] == 110.0
    r3 = record_snapshot(c, now=NOW + timedelta(hours=2), balance=100.0,
                         equity=105.0)
    assert r3["hwm"] == 110.0  # 下がっても HWM 維持


def test_hwm_cashflow_adjustment(tmp_path):
    c = _conn(tmp_path)
    record_snapshot(c, now=NOW, balance=100.0, equity=100.0)
    # 50 入金: equity 150 だが利益ではない → HWM は 100+50=150 (DD は 0% のまま)
    r = record_snapshot(c, now=NOW + timedelta(hours=1), balance=150.0,
                        equity=150.0, cashflow=50.0)
    assert r["hwm"] == 150.0
    assert drawdown_pct(150.0, r["hwm"]) == 0.0


def test_drawdown_pct():
    assert drawdown_pct(98.0, 100.0) == 2.0
    assert drawdown_pct(100.0, 100.0) == 0.0
    assert drawdown_pct(1.0, 0.0) == 0.0


def test_daily_start_equity(tmp_path):
    c = _conn(tmp_path)
    # 前日 22:00 (当日境界 21:00 の後) → 当日分
    record_snapshot(c, now=datetime(2026, 7, 21, 22, 0, tzinfo=timezone.utc),
                    balance=100, equity=101.0)
    record_snapshot(c, now=NOW, balance=100, equity=99.0)
    assert daily_start_equity(c, NOW) == 101.0


def test_daily_start_falls_back_to_last_before(tmp_path):
    c = _conn(tmp_path)
    record_snapshot(c, now=datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc),
                    balance=100, equity=98.0)  # 境界より前
    assert daily_start_equity(c, NOW) == 98.0


def test_daily_start_stale_fallback_is_none(tmp_path):
    c = _conn(tmp_path)
    record_snapshot(c, now=NOW - timedelta(days=10), balance=100, equity=98.0)
    assert daily_start_equity(c, NOW) is None  # 陳腐化 → fail closed


def test_daily_start_adjusts_for_cashflow(tmp_path):
    c = _conn(tmp_path)
    record_snapshot(c, now=datetime(2026, 7, 21, 22, 0, tzinfo=timezone.utc),
                    balance=100, equity=100.0)  # 日初 100
    record_snapshot(c, now=NOW - timedelta(hours=1), balance=150,
                    equity=150.0, cashflow=50.0)  # 日中 50 入金
    # 入金は日次損益に含めない: 日初は 100+50=150 として扱う
    assert daily_start_equity(c, NOW) == 150.0


def test_daily_start_none_when_empty(tmp_path):
    c = _conn(tmp_path)
    assert daily_start_equity(c, NOW) is None


def test_current_account(tmp_path):
    from agentic_fx.core.accounting import current_account
    c = _conn(tmp_path)
    assert current_account(c, NOW) is None
    record_snapshot(c, now=NOW - timedelta(minutes=5), balance=100,
                    equity=100.0)
    record_snapshot(c, now=NOW, balance=100, equity=97.0)
    assert current_account(c, NOW) == (97.0, 100.0)  # (equity, hwm)
    # 古い snapshot は None (fail closed)
    assert current_account(c, NOW + timedelta(hours=1)) is None
