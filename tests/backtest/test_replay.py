from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest
from agentic_fx.backtest.replay import ReplayClock, BarFeed, quote_from_bar
from agentic_fx.core.contracts import Bar
from agentic_fx.store import ohlcv
from tests.backtest.factories import _conn, _row_at, H


# === ReplayClock tests ===

def test_replay_clock_advances_continuously():
    """Clock advances exactly 1 minute per call."""
    c = ReplayClock(H)
    assert c.now() == H
    assert c.advance() == H + timedelta(minutes=1)
    assert c.now() == H + timedelta(minutes=1)


def test_replay_clock_rejects_naive_start():
    """ReplayClock requires aware UTC start."""
    naive_start = datetime(2026, 7, 22, 12, 0)  # naive
    with pytest.raises(ValueError, match="aware"):
        ReplayClock(naive_start)


def test_replay_clock_accepts_non_utc_start_and_normalizes():
    """ReplayClock accepts non-UTC aware start and normalizes to UTC (F5)."""
    utc9 = timezone(timedelta(hours=9))
    # 21:00:00+09:00 = 12:00:00 UTC
    ts_utc9 = datetime(2026, 7, 22, 21, 0, tzinfo=utc9)
    c = ReplayClock(ts_utc9)
    expected_utc = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    assert c.now() == expected_utc


def test_replay_clock_rejects_off_grid_start():
    """ReplayClock requires minute boundary (second=0, microsecond=0, F4)."""
    off_grid = H.replace(second=30)  # 12:00:30 instead of 12:00:00
    with pytest.raises(ValueError, match="minute boundary"):
        ReplayClock(off_grid)


def test_replay_clock_accepts_zoneinfo_aware_and_normalizes_to_utc():
    """ReplayClock accepts ZoneInfo aware datetime and normalizes to UTC (F5)."""
    utc9_tz = ZoneInfo("Asia/Tokyo")  # UTC+9
    # 21:00:00 JST = 12:00:00 UTC
    ts_utc9 = datetime(2026, 7, 22, 21, 0, tzinfo=utc9_tz)
    c = ReplayClock(ts_utc9)
    expected_utc = H  # 12:00:00 UTC
    assert c.now() == expected_utc


def test_replay_clock_accepts_utc9_datetime():
    """ReplayClock accepts UTC+9 datetime and normalizes (F5)."""
    utc9 = timezone(timedelta(hours=9))
    ts_utc9 = datetime(2026, 7, 22, 21, 0, tzinfo=utc9)  # 21:00:00+09:00 = 12:00:00 UTC
    c = ReplayClock(ts_utc9)
    expected_utc = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    assert c.now() == expected_utc


# === BarFeed tests ===

def test_bar_feed_returns_none_for_gap(tmp_path):
    """BarFeed.bar_at returns None for missing bars (no look-back)."""
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1),
         _row_at(H + timedelta(minutes=2), o=148.1, h=148.3, l=148.0, c=148.2)],
        source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=H + timedelta(minutes=5))
    assert feed.bar_at(H) is not None
    assert feed.bar_at(H + timedelta(minutes=1)) is None  # gap
    assert feed.bar_at(H + timedelta(minutes=2)) is not None


def test_bar_feed_respects_start_boundary(tmp_path):
    """BarFeed.bar_at returns None for bars before start."""
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(
        conn,
        [_row_at(H - timedelta(minutes=2), o=147.0, h=147.2, l=146.9, c=147.1),
         _row_at(H, o=148.0, h=148.2, l=147.9, c=148.1)],
        source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=H + timedelta(minutes=5))
    assert feed.bar_at(H - timedelta(minutes=2)) is None  # before start
    assert feed.bar_at(H) is not None


def test_bar_feed_respects_end_boundary(tmp_path):
    """BarFeed.bar_at returns None for bars after end."""
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1),
         _row_at(H + timedelta(minutes=10), o=148.1, h=148.3, l=148.0, c=148.2)],
        source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=H + timedelta(minutes=5))
    assert feed.bar_at(H) is not None
    assert feed.bar_at(H + timedelta(minutes=10)) is None  # after end


def test_bar_feed_bar_at_end_boundary_included(tmp_path):
    """BarFeed should include bars exactly at the end boundary."""
    conn = _conn(tmp_path)
    end_time = H + timedelta(minutes=5)
    ohlcv.import_history_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1),
         _row_at(end_time, o=148.1, h=148.3, l=148.0, c=148.2)],
        source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=end_time)
    assert feed.bar_at(end_time) is not None  # bar at boundary should be included


def test_bar_feed_filters_by_source(tmp_path):
    """BarFeed must filter by source (catch source parameter mutations)."""
    conn = _conn(tmp_path)
    # Import same bar_time under two different sources
    ohlcv.import_history_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1)],
        source="dukascopy")
    ohlcv.import_history_bars(
        conn,
        [_row_at(H, o=149.0, h=149.2, l=148.9, c=149.1)],
        source="mt5")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=H + timedelta(minutes=5))
    bar = feed.bar_at(H)
    assert bar is not None
    assert bar.close == 148.1  # dukascopy, not mt5


def test_bar_feed_latest_completed_1m_returns_previous_bar(tmp_path):
    """BarFeed.latest_completed_1m(ts) returns bar at ts-1m (completed bar)."""
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1),
         _row_at(H + timedelta(minutes=1), o=148.1, h=148.3, l=148.0, c=148.2)],
        source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=H + timedelta(minutes=5))
    # latest_completed_1m(H+1m) should return bar at H
    completed = feed.latest_completed_1m(H + timedelta(minutes=1))
    assert completed is not None
    assert completed.ts == H


def test_bar_feed_latest_completed_1m_gap_returns_none(tmp_path):
    """BarFeed.latest_completed_1m returns None if previous bar missing (gap)."""
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1),
         _row_at(H + timedelta(minutes=2), o=148.1, h=148.3, l=148.0, c=148.2)],
        source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=H + timedelta(minutes=5))
    # latest_completed_1m(H+2m) looks for bar at H+1m which is missing
    assert feed.latest_completed_1m(H + timedelta(minutes=2)) is None


def test_bar_feed_latest_completed_1m_before_start_returns_none(tmp_path):
    """BarFeed.latest_completed_1m(H) returns None (no bar at H-1m)."""
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1)],
        source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=H + timedelta(minutes=5))
    # latest_completed_1m(H) looks for bar at H-1m which doesn't exist
    assert feed.latest_completed_1m(H) is None


def test_bar_feed_rejects_naive_start(tmp_path):
    """BarFeed requires aware UTC start."""
    conn = _conn(tmp_path)
    naive_start = datetime(2026, 7, 22, 12, 0)  # naive
    with pytest.raises(ValueError, match="aware"):
        BarFeed(conn, "USDJPY", source="dukascopy", start=naive_start, end=H)


def test_bar_feed_rejects_naive_end(tmp_path):
    """BarFeed requires aware UTC end."""
    conn = _conn(tmp_path)
    naive_end = datetime(2026, 7, 22, 13, 0)  # naive
    with pytest.raises(ValueError, match="aware"):
        BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=naive_end)


def test_bar_feed_accepts_non_utc_start_and_normalizes(tmp_path):
    """BarFeed accepts non-UTC aware start and normalizes to UTC (F5)."""
    conn = _conn(tmp_path)
    utc9 = timezone(timedelta(hours=9))
    # 21:00:00+09:00 = 12:00:00 UTC
    start_utc9 = datetime(2026, 7, 22, 21, 0, tzinfo=utc9)
    ohlcv.import_history_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1)],
        source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=start_utc9, end=H + timedelta(minutes=5))
    # Should find the bar at H even though start was given in UTC+9
    assert feed.bar_at(H) is not None


def test_bar_feed_accepts_non_utc_end_and_normalizes(tmp_path):
    """BarFeed accepts non-UTC aware end and normalizes to UTC (F5)."""
    conn = _conn(tmp_path)
    utc9 = timezone(timedelta(hours=9))
    # 21:05:00+09:00 = 12:05:00 UTC
    end_utc9 = datetime(2026, 7, 22, 21, 5, tzinfo=utc9)
    ohlcv.import_history_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1),
         _row_at(H + timedelta(minutes=5), o=148.1, h=148.3, l=148.0, c=148.2)],
        source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=end_utc9)
    # Should find bar at H+5m even though end was given in UTC+9
    assert feed.bar_at(H + timedelta(minutes=5)) is not None


def test_bar_feed_rejects_start_greater_than_end(tmp_path):
    """BarFeed rejects start > end."""
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="start > end"):
        BarFeed(conn, "USDJPY", source="dukascopy",
                start=H + timedelta(minutes=5), end=H)


def test_bar_feed_bar_at_rejects_naive_ts(tmp_path):
    """BarFeed.bar_at rejects naive datetime."""
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1)],
        source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=H + timedelta(minutes=5))
    naive_ts = datetime(2026, 7, 22, 12, 0)
    with pytest.raises(ValueError, match="aware"):
        feed.bar_at(naive_ts)


def test_bar_feed_bar_at_accepts_non_utc_aware_and_normalizes(tmp_path):
    """BarFeed.bar_at accepts non-UTC aware datetime and normalizes (F5)."""
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1)],
        source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=H + timedelta(minutes=5))
    utc9 = timezone(timedelta(hours=9))
    # 21:00:00+09:00 = 12:00:00 UTC
    ts_utc9 = datetime(2026, 7, 22, 21, 0, tzinfo=utc9)
    bar = feed.bar_at(ts_utc9)
    assert bar is not None
    assert bar.ts == H  # Bar.ts normalized to UTC


def test_bar_feed_spread_at_returns_none_for_gap(tmp_path):
    """BarFeed.spread_at returns None for missing bars."""
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1, spread=0.02),
         _row_at(H + timedelta(minutes=2), o=148.1, h=148.3, l=148.0, c=148.2, spread=0.03)],
        source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=H + timedelta(minutes=5))
    assert feed.spread_at(H) == 0.02
    assert feed.spread_at(H + timedelta(minutes=1)) is None  # gap


def test_bar_feed_spread_at_uses_actual_value(tmp_path):
    """BarFeed.spread_at must read actual spread, not hardcoded value."""
    conn = _conn(tmp_path)
    # Use non-default spread value to catch hardcoded mutations
    ohlcv.import_history_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1, spread=0.03)],
        source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=H + timedelta(minutes=5))
    assert feed.spread_at(H) == 0.03


def test_bar_feed_spread_at_rejects_naive_ts(tmp_path):
    """BarFeed.spread_at rejects naive datetime (F3)."""
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1, spread=0.02)],
        source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=H + timedelta(minutes=5))
    naive_ts = datetime(2026, 7, 22, 12, 0)
    with pytest.raises(ValueError, match="aware"):
        feed.spread_at(naive_ts)


def test_bar_feed_spread_at_accepts_non_utc_ts_and_normalizes(tmp_path):
    """BarFeed.spread_at accepts non-UTC aware datetime and normalizes (F5)."""
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1, spread=0.02)],
        source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=H + timedelta(minutes=5))
    utc9 = timezone(timedelta(hours=9))
    # 21:00:00+09:00 = 12:00:00 UTC
    ts_utc9 = datetime(2026, 7, 22, 21, 0, tzinfo=utc9)
    spread = feed.spread_at(ts_utc9)
    assert spread == 0.02


# === quote_from_bar tests ===

def test_quote_from_bar_half_spread():
    """quote_from_bar computes bid = close - spread/2, ask = close + spread/2."""
    bar = Bar("USDJPY", "1m", H, 148, 148.2, 147.9, 148.1, 5)
    q = quote_from_bar(bar, spread=0.02)
    # Use approx for float equality (floating point precision)
    assert q.bid == pytest.approx(148.09, abs=1e-9)
    assert q.ask == pytest.approx(148.11, abs=1e-9)


def test_quote_from_bar_sets_fields():
    """quote_from_bar sets symbol, ts, source correctly."""
    bar = Bar("USDJPY", "1m", H, 148, 148.2, 147.9, 148.1, 5)
    q = quote_from_bar(bar, spread=0.02)
    assert q.symbol == "USDJPY"
    assert q.ts == H
    assert q.source == "backtest"


# === quote_from_bar validation tests (F1) ===

def test_quote_from_bar_rejects_none_spread():
    """quote_from_bar rejects None spread (fail-closed)."""
    bar = Bar("USDJPY", "1m", H, 148, 148.2, 147.9, 148.1, 5)
    with pytest.raises(ValueError, match="spread must not be None"):
        quote_from_bar(bar, spread=None)


def test_quote_from_bar_rejects_nan_spread():
    """quote_from_bar rejects NaN spread."""
    bar = Bar("USDJPY", "1m", H, 148, 148.2, 147.9, 148.1, 5)
    with pytest.raises(ValueError, match="spread must be finite"):
        quote_from_bar(bar, spread=float('nan'))


def test_quote_from_bar_rejects_inf_spread():
    """quote_from_bar rejects infinite spread."""
    bar = Bar("USDJPY", "1m", H, 148, 148.2, 147.9, 148.1, 5)
    with pytest.raises(ValueError, match="spread must be finite"):
        quote_from_bar(bar, spread=float('inf'))


def test_quote_from_bar_rejects_negative_spread():
    """quote_from_bar rejects negative spread (would create bid > ask)."""
    bar = Bar("USDJPY", "1m", H, 148, 148.2, 147.9, 148.1, 5)
    with pytest.raises(ValueError, match="spread must not be negative"):
        quote_from_bar(bar, spread=-0.01)
