from datetime import datetime, timedelta, timezone
import pytest
from agentic_fx.backtest.replay import ReplayClock, BarFeed, quote_from_bar
from agentic_fx.core.contracts import Bar
from agentic_fx.store import ohlcv
from tests.backtest.conftest import _conn, _row_at, H


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


def test_replay_clock_rejects_non_utc_start():
    """ReplayClock requires UTC (not just aware)."""
    non_utc = H.replace(tzinfo=timezone(timedelta(hours=9)))
    with pytest.raises(ValueError, match="UTC"):
        ReplayClock(non_utc)


# === BarFeed tests ===

def test_bar_feed_returns_none_for_gap(tmp_path):
    """BarFeed.bar_at returns None for missing bars (no look-back)."""
    conn = _conn(tmp_path)
    ohlcv.import_bars(
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
    ohlcv.import_bars(
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
    ohlcv.import_bars(
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
    ohlcv.import_bars(
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
    ohlcv.import_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1)],
        source="dukascopy")
    ohlcv.import_bars(
        conn,
        [_row_at(H, o=149.0, h=149.2, l=148.9, c=149.1)],
        source="mt5")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=H + timedelta(minutes=5))
    bar = feed.bar_at(H)
    assert bar is not None
    assert bar.close == 148.1  # dukascopy, not mt5


def test_bar_feed_latest_1m_gap(tmp_path):
    """BarFeed.latest_1m (alias for bar_at) returns None for gaps."""
    conn = _conn(tmp_path)
    ohlcv.import_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1),
         _row_at(H + timedelta(minutes=2), o=148.1, h=148.3, l=148.0, c=148.2)],
        source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=H + timedelta(minutes=5))
    assert feed.latest_1m(H + timedelta(minutes=1)) is None  # gap


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


def test_bar_feed_rejects_non_utc_start(tmp_path):
    """BarFeed requires UTC start (not just aware)."""
    conn = _conn(tmp_path)
    non_utc = H.replace(tzinfo=timezone(timedelta(hours=9)))
    with pytest.raises(ValueError, match="UTC"):
        BarFeed(conn, "USDJPY", source="dukascopy", start=non_utc, end=H + timedelta(minutes=5))


def test_bar_feed_rejects_non_utc_end(tmp_path):
    """BarFeed requires UTC end (not just aware)."""
    conn = _conn(tmp_path)
    non_utc = (H + timedelta(minutes=5)).replace(tzinfo=timezone(timedelta(hours=9)))
    with pytest.raises(ValueError, match="UTC"):
        BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=non_utc)


def test_bar_feed_rejects_start_greater_than_end(tmp_path):
    """BarFeed rejects start > end."""
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="start > end"):
        BarFeed(conn, "USDJPY", source="dukascopy",
                start=H + timedelta(minutes=5), end=H)


def test_bar_feed_bar_at_rejects_naive_ts(tmp_path):
    """BarFeed.bar_at rejects naive datetime."""
    conn = _conn(tmp_path)
    ohlcv.import_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1)],
        source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=H + timedelta(minutes=5))
    naive_ts = datetime(2026, 7, 22, 12, 0)
    with pytest.raises(ValueError, match="aware"):
        feed.bar_at(naive_ts)


def test_bar_feed_spread_at_returns_none_for_gap(tmp_path):
    """BarFeed.spread_at returns None for missing bars."""
    conn = _conn(tmp_path)
    ohlcv.import_bars(
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
    ohlcv.import_bars(
        conn,
        [_row_at(H, o=148.0, h=148.2, l=147.9, c=148.1, spread=0.03)],
        source="dukascopy")
    feed = BarFeed(conn, "USDJPY", source="dukascopy", start=H, end=H + timedelta(minutes=5))
    assert feed.spread_at(H) == 0.03


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
