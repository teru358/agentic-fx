from datetime import datetime, timedelta, timezone

from agentic_fx.datafeed.econ_bars import normalize_1m
from agentic_fx.store import ohlcv_slices
from agentic_fx.store.db import connect, init_db


def _conn(tmp_path, name="econ.db"):
    conn = connect(tmp_path / name)
    init_db(conn)
    return conn


def _put(conn, table, source, at, price):
    conn.execute(
        f"INSERT INTO {table} (symbol,interval,bar_time,open,high,low,close,volume,source) "
        "VALUES ('USDJPY','1m',?,?,?,?,?,?,?)",
        (at.isoformat(), price, price + .1, price - .1, price, 1, source),
    )
    conn.commit()


def test_open_minutes_counts_only_open_minutes_and_skips_closed_data(tmp_path):
    start = datetime(2026, 11, 1, 21, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    assert ohlcv_slices.open_minutes(start, end) == []
    conn = _conn(tmp_path)
    _put(conn, "ohlcv_cache", "mt5-live", start + timedelta(minutes=1), 150)
    result = normalize_1m(conn, "USDJPY", start, end)
    assert result.expected_rows == 0
    assert result.rows == ()


def test_missing_open_minute_is_an_outage_marker(tmp_path):
    start = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    conn = _conn(tmp_path)
    _put(conn, "ohlcv_cache", "mt5-live", start, 150)
    _put(conn, "ohlcv_cache", "mt5-live", start + timedelta(minutes=2), 150)
    result = normalize_1m(conn, "USDJPY", start, start + timedelta(minutes=3))
    assert result.expected_rows == 3
    assert len(result.rows) == result.expected_rows
    assert result.rows[1].quality == ohlcv_slices.QUALITY_OUTAGE
    assert result.has_markers


def test_duplicate_sources_choose_cache_before_history_and_conflicts_mark(tmp_path):
    start = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    conn = _conn(tmp_path)
    _put(conn, "ohlcv_history", "dukascopy", start, 150)
    _put(conn, "ohlcv_cache", "mt5-live", start, 150)
    result = normalize_1m(conn, "USDJPY", start, start + timedelta(minutes=1))
    assert result.rows[0].source == "mt5-live"
    _put(conn, "ohlcv_cache", "yfinance", start, 150 + 5e-10)
    matching = normalize_1m(conn, "USDJPY", start, start + timedelta(minutes=1))
    assert matching.rows[0].source == "mt5-live"
    conn.execute("DELETE FROM ohlcv_cache WHERE symbol='USDJPY' AND interval='1m' "
                 "AND bar_time=? AND source='yfinance'", (start.isoformat(),))
    conn.commit()
    _put(conn, "ohlcv_cache", "yfinance", start, 152)
    conflict = normalize_1m(conn, "USDJPY", start, start + timedelta(minutes=1))
    assert conflict.rows[0].quality == ohlcv_slices.QUALITY_OUTAGE
    assert conflict.conflicting_minutes == (start,)


def test_invalid_source_ohlc_becomes_an_invalid_data_marker(tmp_path):
    start = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    conn = _conn(tmp_path)
    _put(conn, "ohlcv_cache", "mt5-live", start - timedelta(minutes=1), 150)
    conn.execute(
        "INSERT INTO ohlcv_cache (symbol,interval,bar_time,open,high,low,close,volume,source) "
        "VALUES ('USDJPY','1m',?,?,?,?,?,?,?)",
        (start.isoformat(), 150, 149, 151, 150, 1, "mt5-live"))
    conn.commit()
    result = normalize_1m(conn, "USDJPY", start, start + timedelta(minutes=1))
    assert result.rows[0].quality == ohlcv_slices.QUALITY_OUTAGE
    assert result.invalid_minutes == (start,)


def test_ohlc_at_database_price_limit_becomes_invalid_marker(tmp_path):
    start = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    conn = _conn(tmp_path)
    _put(conn, "ohlcv_cache", "mt5-live", start - timedelta(minutes=1), 150)
    conn.execute(
        "INSERT INTO ohlcv_cache (symbol,interval,bar_time,open,high,low,close,volume,source) "
        "VALUES ('USDJPY','1m',?,?,?,?,?,?,?)",
        (start.isoformat(), 1e308, 1e308, 1e308, 1e308, 1, "mt5-live"))
    conn.commit()
    result = normalize_1m(conn, "USDJPY", start, start + timedelta(minutes=1))
    assert result.rows[0].quality == ohlcv_slices.QUALITY_OUTAGE
    assert result.invalid_minutes == (start,)


def test_normalized_row_count_matches_open_minute_count(tmp_path):
    start = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=255)
    conn = _conn(tmp_path)
    for minute in ohlcv_slices.open_minutes(start, end):
        _put(conn, "ohlcv_cache", "mt5-live", minute, 150)
    result = normalize_1m(conn, "USDJPY", start, end)
    assert result.expected_rows == len(ohlcv_slices.open_minutes(start, end)) == 255
    assert len(result.rows) == result.expected_rows


def test_unrecoverable_when_range_precedes_oldest_source_bar(tmp_path):
    start = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    conn = _conn(tmp_path)
    _put(conn, "ohlcv_history", "dukascopy", start + timedelta(minutes=1), 150)
    result = normalize_1m(conn, "USDJPY", start, start + timedelta(minutes=2))
    assert result.unrecoverable


def test_open_minute_counts_cover_weekend_holidays_and_dst_edges():
    cases = [
        # Friday 17:00 NY through Sunday 17:00 NY (winter).
        (datetime(2026, 11, 7, 22, 0, tzinfo=timezone.utc),
         datetime(2026, 11, 8, 22, 0, tzinfo=timezone.utc), 0),
        # Christmas and New Year's full NY trading dates.
        (datetime(2026, 12, 24, 22, 0, tzinfo=timezone.utc),
         datetime(2026, 12, 25, 22, 0, tzinfo=timezone.utc), 0),
        (datetime(2026, 12, 31, 22, 0, tzinfo=timezone.utc),
         datetime(2027, 1, 1, 22, 0, tzinfo=timezone.utc), 0),
        # Spring DST Sunday opens at 21:00Z; fall DST Sunday at 22:00Z.
        (datetime(2026, 3, 8, 20, 59, tzinfo=timezone.utc),
         datetime(2026, 3, 8, 22, 1, tzinfo=timezone.utc), 61),
        (datetime(2026, 11, 1, 21, 0, tzinfo=timezone.utc),
         datetime(2026, 11, 1, 22, 1, tzinfo=timezone.utc), 1),
    ]
    for start, end, expected in cases:
        assert len(ohlcv_slices.open_minutes(start, end)) == expected


def test_closed_minute_cache_rows_and_five_minute_rows_are_ignored(tmp_path):
    start = datetime(2026, 11, 1, 21, 0, tzinfo=timezone.utc)
    conn = _conn(tmp_path)
    _put(conn, "ohlcv_cache", "mt5-live", start - timedelta(days=2), 150)
    before = normalize_1m(conn, "USDJPY", start, start + timedelta(hours=1))
    for minute in range(1, 5):
        _put(conn, "ohlcv_cache", "mt5-live",
             start + timedelta(minutes=minute), 150)
    conn.execute(
        "INSERT INTO ohlcv_cache (symbol,interval,bar_time,open,high,low,close,volume,source) "
        "VALUES ('USDJPY','5m',?,?,?,?,?,?,?)",
        ((start + timedelta(minutes=1)).isoformat(), 150, 151, 149, 150, 1,
         "mt5-live"))
    conn.commit()
    after = normalize_1m(conn, "USDJPY", start, start + timedelta(hours=1))
    assert before.expected_rows == after.expected_rows == 0
    assert before.rows == after.rows == ()


def test_missing_minute_is_marked_with_or_without_outage_episode(tmp_path):
    start = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    for with_episode in (False, True):
        conn = _conn(tmp_path, name=f"missing-{with_episode}.db")
        _put(conn, "ohlcv_cache", "mt5-live", start - timedelta(minutes=1), 150)
        _put(conn, "ohlcv_cache", "mt5-live", start, 150)
        _put(conn, "ohlcv_cache", "mt5-live", start + timedelta(minutes=2), 150)
        if with_episode:
            conn.execute(
                "INSERT INTO datafeed_outage_gap "
                "(pair,interval,epoch,gap_start,replay_through) "
                "VALUES ('USDJPY','1m',1,?,NULL)",
                (start.isoformat(),))
            conn.commit()
        result = normalize_1m(conn, "USDJPY", start,
                              start + timedelta(minutes=3))
        assert result.rows[1].quality == ohlcv_slices.QUALITY_OUTAGE
        assert result.rows[1].source
