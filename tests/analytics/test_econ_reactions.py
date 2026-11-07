from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.analytics import econ_reactions as reactions
from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.econ_bars import normalize_1m
from agentic_fx.store import ohlcv as ohlcv_store
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.ohlcv_slices import (
    QUALITY_OBSERVED,
    SliceRow,
    open_minutes,
)

UTC = timezone.utc
PIP = 0.01
PAIR_CURRENCIES = {"EURUSD": ("EUR", "USD"), "USDJPY": ("USD", "JPY")}


def _rows(ts: datetime, minutes: int, *, marker_at: datetime | None = None,
          source_change_at: datetime | None = None) -> list[SliceRow]:
    out = [SliceRow(ts - timedelta(minutes=1), 150.0, 150.01, 149.99,
                    150.0, 1.0, "mt5-live", QUALITY_OBSERVED)]
    for i in range(minutes):
        at = ts + timedelta(minutes=i)
        if at == marker_at:
            out.append(SliceRow(at, None, None, None, None, None,
                                "econ-normalizer", "datafeed_outage_gap"))
            continue
        step = i + 1
        out.append(SliceRow(
            at, 150.0, 150.0 + step * PIP, 150.0 - step * PIP,
            150.0 + step * PIP, 1.0,
            "mt5-live" if source_change_at is None or at < source_change_at else "yfinance",
            QUALITY_OBSERVED,
        ))
    return out


def _bar(ts: datetime, *, close: float = 150.0) -> Bar:
    return Bar("USDJPY", "1m", ts, close, close + 0.01, close - 0.01,
               close, 1.0)


def _conn(tmp_path):
    conn = connect(tmp_path / "reaction-bars.db")
    init_db(conn)
    return conn


def test_window_values_uses_baseline_terminal_range_and_full_spread():
    ts = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    got = reactions.window_values(_rows(ts, 240), event_ts=ts, pip_size=PIP,
                                  spread_pips=300.0, set_state="complete")
    assert got == (
        reactions.WindowValue(15, "resolved", 30.0, 15.0, 15.0, -285.0),
        reactions.WindowValue(60, "resolved", 120.0, 60.0, 60.0, -240.0),
        reactions.WindowValue(240, "resolved", 480.0, 240.0, 240.0, -60.0),
    )


def test_window_status_is_independent_and_baseline_marker_invalidates_each_window():
    ts = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    marker_rows = _rows(ts, 240, marker_at=ts + timedelta(minutes=30))
    got = reactions.window_values(marker_rows, event_ts=ts, pip_size=PIP,
                                  spread_pips=1.0, set_state="complete")
    assert [value.status for value in got] == ["resolved", "unresolvable", "unresolvable"]
    baseline = _rows(ts, 240)
    baseline[0] = SliceRow(ts - timedelta(minutes=1), None, None, None, None,
                           None, "econ-normalizer", "datafeed_outage_gap")
    got = reactions.window_values(baseline, event_ts=ts, pip_size=PIP,
                                  spread_pips=1.0, set_state="complete")
    assert [value.status for value in got] == ["unresolvable"] * 3
    assert [value.range_pips for value in got] == [None] * 3


def test_closed_market_invalidates_only_windows_crossing_friday_close():
    ts = datetime(2026, 11, 13, 21, 30, tzinfo=UTC)
    rows = [SliceRow(at, 150.0, 150.01, 149.99, 150.0, 1.0,
                     "mt5-live", QUALITY_OBSERVED)
            for at in open_minutes(ts - timedelta(minutes=1), ts + timedelta(minutes=240))]
    got = reactions.window_values(rows, event_ts=ts, pip_size=PIP,
                                  spread_pips=1.0, set_state="complete")
    assert [value.status for value in got] == ["resolved", "unresolvable", "unresolvable"]


def test_weekday_ny_rollover_is_not_a_market_closure():
    ts = datetime(2026, 11, 10, 21, 50, tzinfo=UTC)
    rows = _rows(ts, 60)
    got = reactions.window_values(rows, event_ts=ts, pip_size=PIP,
                                  spread_pips=1.0, set_state="complete")
    assert got[1].status == "resolved"


@pytest.mark.parametrize(("set_state", "spread"), [
    ("unrecoverable", 1.0),
    ("complete", None),
])
def test_unrecoverable_set_or_missing_spread_makes_all_windows_null(set_state, spread):
    ts = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    got = reactions.window_values(_rows(ts, 240), event_ts=ts, pip_size=PIP,
                                  spread_pips=spread, set_state=set_state)
    assert [value.status for value in got] == ["unresolvable"] * 3
    assert [(value.range_pips, value.close_change_pips,
             value.abs_close_change_pips, value.net_abs_close_change_pips)
            for value in got] == [(None, None, None, None)] * 3


def test_source_name_change_within_window_does_not_break_resolution():
    ts = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    got = reactions.window_values(
        _rows(ts, 15, source_change_at=ts + timedelta(minutes=7)),
        event_ts=ts, pip_size=PIP, spread_pips=1.0, set_state="complete")
    assert got[0].status == "resolved"


def test_normalize_duplicate_equal_sources_selects_live_once(tmp_path):
    conn = _conn(tmp_path)
    ts = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    bars = [_bar(ts - timedelta(minutes=1)), *(_bar(ts + timedelta(minutes=i)) for i in range(15))]
    ohlcv_store.upsert_cache_bars(conn, bars, source="mt5-live")
    history_rows = [
        (bar.symbol, bar.interval, bar.ts.isoformat(), bar.open, bar.high,
         bar.low, bar.close, bar.volume, None)
        for bar in bars
    ]
    ohlcv_store.import_history_bars(conn, history_rows, source="dukascopy")
    normalized = normalize_1m(conn, "USDJPY", ts - timedelta(minutes=1),
                              ts + timedelta(minutes=15))
    assert len(normalized.rows) == 16
    assert [row.source for row in normalized.rows] == ["mt5-live"] * 16
    assert not normalized.has_markers
    result = reactions.window_values(normalized.rows, event_ts=ts,
                                     pip_size=PIP, spread_pips=1.0,
                                     set_state="complete")
    assert result[0].status == "resolved"


def test_normalize_conflicting_sources_yields_marker_and_unresolvable_window(tmp_path):
    conn = _conn(tmp_path)
    ts = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    live = [_bar(ts - timedelta(minutes=1)), *(_bar(ts + timedelta(minutes=i)) for i in range(15))]
    history = [_bar(ts - timedelta(minutes=1)), *(_bar(ts + timedelta(minutes=i)) for i in range(15))]
    history[5] = _bar(ts + timedelta(minutes=4), close=151.0)
    ohlcv_store.upsert_cache_bars(conn, live, source="mt5-live")
    history_rows = [
        (bar.symbol, bar.interval, bar.ts.isoformat(), bar.open, bar.high,
         bar.low, bar.close, bar.volume, None)
        for bar in history
    ]
    ohlcv_store.import_history_bars(conn, history_rows, source="dukascopy")
    normalized = normalize_1m(conn, "USDJPY", ts - timedelta(minutes=1),
                              ts + timedelta(minutes=15))
    assert normalized.conflicting_minutes == (ts + timedelta(minutes=4),)
    assert normalized.rows[5].quality == "datafeed_outage_gap"
    result = reactions.window_values(normalized.rows, event_ts=ts,
                                     pip_size=PIP, spread_pips=1.0,
                                     set_state="complete")
    assert result[0].status == "unresolvable"


def test_event_currencies_and_canonical_pair_follow_settings_order():
    settings = {"EURUSD": ("EUR", "USD"), "USDJPY": ("USD", "JPY")}
    assert reactions.canonical_pair("USD", settings) == "EURUSD"
    assert reactions.canonical_pair("JPY", settings) == "USDJPY"
    assert reactions.event_currencies("All", settings) == ("EUR", "USD", "JPY")
    assert reactions.event_currencies("GBP", settings) == ()
    assert reactions.event_currencies("JPY", settings) == ("JPY",)


def _sample(event_id: int, *, ts: datetime, value: reactions.WindowValue,
            importance: int = 3, name: str = "CPI", currency: str = "USD"):
    return reactions.ReactionSample(event_id, currency, importance, name, ts, value)


def test_aggregate_builds_both_bundle_kinds_and_interpolates_percentiles():
    ts = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    samples = [
        _sample(1, ts=ts, value=reactions.WindowValue(15, "resolved", 2.0, -2.0, 2.0, 1.0)),
        _sample(2, ts=ts, value=reactions.WindowValue(15, "resolved", 4.0, 4.0, 4.0, 3.0)),
        _sample(3, ts=ts + timedelta(days=1),
                value=reactions.WindowValue(15, "unresolvable", None, None, None, None)),
    ]
    got = reactions.aggregate(samples)
    assert [(row.kind, row.key, row.window_min, row.n, row.unique_ts_n,
             row.unresolvable_n, row.range_median, row.range_p75,
             row.abs_median, row.abs_p75, row.net_median, row.net_p75,
             row.close_change_median) for row in got] == [
        ("importance", "3", 15, 2, 1, 1, 3.0, 3.5, 3.0, 3.5, 2.0, 2.5, 1.0),
        ("name", "CPI", 15, 2, 1, 1, 3.0, 3.5, 3.0, 3.5, 2.0, 2.5, 1.0),
    ]


def test_aggregate_odd_count_and_empty_resolved_bundle_stats_are_none():
    ts = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    samples = [
        _sample(i, ts=ts + timedelta(days=i), value=reactions.WindowValue(
            60, "resolved", float(i), float(i), float(i), float(i)))
        for i in (1, 2, 3)
    ]
    samples.append(_sample(4, ts=ts, value=reactions.WindowValue(
        60, "unresolvable", None, None, None, None)))
    got = reactions.aggregate(samples)
    row = got[0]
    assert (row.n, row.unique_ts_n, row.unresolvable_n, row.range_median,
            row.range_p75, row.abs_median, row.abs_p75, row.net_median,
            row.net_p75, row.close_change_median) == (3, 3, 1, 2.0, 2.5,
                                                       2.0, 2.5, 2.0, 2.5, 2.0)
    empty = reactions.aggregate([_sample(
        5, ts=ts, value=reactions.WindowValue(240, "unresolvable", None, None, None, None))])[0]
    assert (empty.n, empty.unresolvable_n, empty.range_median,
            empty.range_p75, empty.abs_median, empty.abs_p75, empty.net_median,
            empty.net_p75, empty.close_change_median) == (0, 1, None, None, None,
                                                         None, None, None, None)


def _bundle(currency: str, n: int) -> reactions.BundleStats:
    return reactions.BundleStats(currency, "importance", "3", 15, n, n, 0,
                                 30.0, 25.0, 15.0, 12.0, 14.0, 11.0, -15.0)


def test_agent_view_hides_below_floor_and_exposes_at_floor_for_pair_currencies():
    stats = [_bundle("USD", 19), _bundle("USD", 20), _bundle("JPY", 20),
             _bundle("EUR", 20)]
    got = reactions.agent_view(stats, pair="USDJPY", pair_currencies=PAIR_CURRENCIES)
    insufficient = next(item for item in got if item["currency"] == "USD" and
                        item["status"] == "insufficient")
    assert insufficient == {"currency": "USD", "kind": "importance", "key": "3",
                             "window_min": 15, "status": "insufficient"}
    assert all("n" not in item for item in got if item["status"] == "insufficient")
    adequate = [item for item in got if item["status"] == "ok"]
    assert {item["currency"] for item in adequate} == {"USD", "JPY"}
    assert all(item["n"] == 20 for item in adequate)
    assert all(item["range_median"] == 30.0 and item["range_p75"] == 25.0
               for item in adequate)
    with pytest.raises(ValueError):
        reactions.agent_view(stats, pair="GBPJPY", pair_currencies=PAIR_CURRENCIES)
