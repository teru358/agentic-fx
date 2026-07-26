from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core.accounting import (
    current_account, daily_start_equity, drawdown_pct, record_snapshot,
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


def test_daily_start_equity_consistent_across_timezones(tmp_path):
    # 同じ瞬間を JST / UTC で表現しても daily_start_equity は一致するべき
    jst = timezone(timedelta(hours=9))
    c_utc = _conn(tmp_path / "utc")
    c_jst = _conn(tmp_path / "jst")

    # 前日 22:00 UTC (当日境界 21:00 UTC の後) → 当日分
    record_snapshot(c_utc, now=datetime(2026, 7, 21, 22, 0, tzinfo=timezone.utc),
                    balance=100, equity=101.0)
    record_snapshot(c_jst,
                    now=datetime(2026, 7, 21, 22, 0, tzinfo=timezone.utc).astimezone(jst),
                    balance=100, equity=101.0)

    now_utc = NOW
    now_jst = NOW.astimezone(jst)

    assert daily_start_equity(c_utc, now_utc) == 101.0
    assert daily_start_equity(c_jst, now_jst) == daily_start_equity(c_utc, now_utc)


def test_daily_start_equity_non_utc_offset_misorders_snapshots(tmp_path):
    # trading_day_start(NOW) == 2026-07-21T21:00:00+00:00 (NOW = 2026-07-22T12:00 UTC)
    c = _conn(tmp_path)
    jst = timezone(timedelta(hours=9))

    # X: 真の当日分 (境界 21:00 UTC の 90 分後)。UTC で記録 (equity=200 が正解)。
    record_snapshot(c, now=datetime(2026, 7, 21, 22, 30, tzinfo=timezone.utc),
                    balance=200, equity=200.0)

    # Y: 実時刻は 2026-07-21T12:30:00+00:00 で境界 (21:00) より前 → 前日分。
    # JST (+09:00) で表現すると壁時計は 2026-07-21T21:30:00+09:00 となり、
    # `now` を UTC 正規化せずに ISO 文字列のまま保存すると、境界 21:00+00:00
    # と X の 22:30+00:00 の "間" に辞書式順序で入り込んでしまう。
    record_snapshot(c, now=datetime(2026, 7, 21, 21, 30, tzinfo=jst),
                    balance=77, equity=77.0)

    # 保存された ts は正規 UTC 表記であるべき (辞書式順序の前提)
    from agentic_fx.store import snapshots
    assert snapshots.latest(c)["ts"].endswith("+00:00")

    # 正しくは X (200.0) が当日分の日初エクイティ。
    # 正規化されていないと Y (77.0、実際には前日分) が誤って選ばれる。
    assert daily_start_equity(c, NOW) == 200.0


def test_record_snapshot_rejects_naive_now(tmp_path):
    c = _conn(tmp_path)
    naive = datetime(2026, 7, 22, 12, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        record_snapshot(c, now=naive, balance=100.0, equity=100.0)


def test_daily_start_equity_rejects_naive_now(tmp_path):
    c = _conn(tmp_path)
    record_snapshot(c, now=NOW, balance=100.0, equity=100.0)
    naive = datetime(2026, 7, 22, 12, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        daily_start_equity(c, naive)


def test_current_account_rejects_naive_now(tmp_path):
    c = _conn(tmp_path)
    record_snapshot(c, now=NOW, balance=100.0, equity=100.0)
    naive = datetime(2026, 7, 22, 12, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        current_account(c, naive)


def test_current_account_uses_chronologically_latest_row(tmp_path):
    # out-of-order 挿入 (新しい ts の行の後に、古い ts の行を挿入)
    c = _conn(tmp_path)
    record_snapshot(c, now=NOW - timedelta(minutes=1), balance=100, equity=100.0)
    record_snapshot(c, now=NOW - timedelta(minutes=5), balance=100, equity=80.0)
    # 真に最新なのは equity=100.0 (ts=NOW-1min) の行
    assert current_account(c, NOW) == (100.0, 100.0)
