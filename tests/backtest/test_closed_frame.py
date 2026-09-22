from datetime import datetime, timedelta, timezone

from agentic_fx.backtest.timeframes import load_closed_frame, load_resampled_frame
from agentic_fx.core.contracts import Bar
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.ohlcv import upsert_cache_bars


UTC = timezone.utc


def _conn(tmp_path):
    conn = connect(tmp_path / "closed.db")
    init_db(conn)
    return conn


def _bars(interval, start, count):
    width = {"1m": 1, "1h": 60}[interval]
    return [Bar("USDJPY", interval, start + timedelta(minutes=width * i),
                100 + i, 101 + i, 99 + i, 100.5 + i, i + 1)
            for i in range(count)]


def test_closed_frame_reads_native_1h_rows_not_1m_resample(tmp_path):
    conn = _conn(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    upsert_cache_bars(conn, _bars("1m", start, 180), source="yfinance")
    native = _bars("1h", start, 3)
    native[-1] = Bar("USDJPY", "1h", native[-1].ts, 900, 901, 899, 900.5, 1)
    upsert_cache_bars(conn, native, source="yfinance")
    frame = load_closed_frame(conn, "USDJPY", "1h", "yfinance", 2,
                              start + timedelta(hours=3), timedelta())
    assert list(frame["close"]) == [101.5, 900.5]


def test_closed_frame_aggregates_4h_from_1h_with_existing_grid(tmp_path):
    conn = _conn(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    hourly = _bars("1h", start, 8)
    upsert_cache_bars(conn, hourly, source="yfinance")
    cutoff = start + timedelta(hours=8)
    actual = load_closed_frame(conn, "USDJPY", "4h", "yfinance", 2,
                               cutoff, timedelta())
    expected = load_resampled_frame(conn, "USDJPY", "4h", source="yfinance",
                                    base_interval="1h", until=cutoff,
                                    cutoff=cutoff, max_bars=2)
    assert actual.equals(expected)
