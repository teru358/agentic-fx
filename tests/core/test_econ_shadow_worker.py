from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core.contracts import Bar
from agentic_fx.core.paper_fills import resolve_shadow, select_limit_entry
from agentic_fx.store.ohlcv_slices import open_minutes
from agentic_fx.datafeed.econ_bars import NormalizedBars


def _bar(ts, o=150.0, h=150.2, l=149.8, c=150.0):
    return Bar("USDJPY", "1m", ts, o, h, l, c, 1)


def test_market_entry_uses_half_spread_and_tp_after_entry_bar():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    bars = [_bar(t0), _bar(t0 + timedelta(minutes=1), h=151, c=150.8)]
    result = resolve_shadow(bars, direction="long", kind="market",
                            requested_price=None, stop_loss=149,
                            take_profit=150.5, spread=.02, t0=t0, pip_size=.01)
    assert result.state == "resolved"
    assert result.result_code == "tp"
    assert result.entry_price == 150.01
    assert result.entry_at == t0


def test_limit_expiry_and_fill_selection():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    bars = [_bar(t0 + timedelta(minutes=i), l=150.1) for i in range(240)]
    selection = select_limit_entry(bars, t0=t0, requested_price=149.9,
                                   direction="long", spread=0)
    assert selection.fill_bar is None
    assert selection.range_end == t0 + timedelta(hours=4)
    result = resolve_shadow(bars, direction="long", kind="limit",
                            requested_price=149.9, stop_loss=149,
                            take_profit=None, spread=0, t0=t0, pip_size=.01)
    assert result.state == "expired"
    assert result.result_code == "no_fill"


def test_open_minute_marker_before_resolution_makes_result_data_insufficient():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    bars = [_bar(t0), _bar(t0 + timedelta(minutes=1))]
    result = resolve_shadow(bars, direction="long", kind="market",
                            requested_price=None, stop_loss=149,
                            take_profit=None, spread=0, t0=t0, pip_size=.01,
                            markers={t0 + timedelta(minutes=1)})
    assert result.result_code == "data_insufficient"


def test_fill_bar_sl_wins_when_fill_bar_also_touches_take_profit():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    bars = [_bar(t0, h=151, l=148.5, c=150)]
    result = resolve_shadow(bars, direction="long", kind="limit",
                            requested_price=150, stop_loss=149,
                            take_profit=150.5, spread=0, t0=t0, pip_size=.01)
    assert result.result_code == "sl"


def test_fill_bar_tp_only_is_not_resolved_until_following_bar():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    bars = [_bar(t0, h=151, l=149.5, c=150),
            _bar(t0 + timedelta(minutes=1), h=151, l=149.5, c=150.7)]
    result = resolve_shadow(bars, direction="long", kind="limit",
                            requested_price=150, stop_loss=149,
                            take_profit=150.5, spread=0, t0=t0, pip_size=.01)
    assert result.result_code == "tp"
    assert result.exit_at == t0 + timedelta(minutes=2)


@pytest.mark.parametrize("direction, open_price, expected", [
    ("long", 149.8, 149.8), ("short", 150.2, 150.2),
])
def test_limit_gap_entry_uses_conservative_open(direction, open_price, expected):
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    bars = [_bar(t0, o=open_price, h=open_price + .1,
                 l=open_price - .1, c=open_price)]
    requested = 150.0
    result = resolve_shadow(bars, direction=direction, kind="limit",
                            requested_price=requested, stop_loss=149,
                            take_profit=None, spread=0, t0=t0, pip_size=.01)
    assert result.entry_price == expected


@pytest.mark.parametrize("t0, expected_deadline", [
    (datetime(2026, 7, 6, 19, 0, tzinfo=timezone.utc),
     datetime(2026, 7, 6, 20, 55, tzinfo=timezone.utc)),
    (datetime(2026, 11, 2, 20, 0, tzinfo=timezone.utc),
     datetime(2026, 11, 2, 21, 55, tzinfo=timezone.utc)),
])
def test_market_day_close_uses_last_complete_bar_and_dst_deadline(t0,
                                                                    expected_deadline):
    bars = [_bar(ts, c=150.2) for ts in open_minutes(t0, expected_deadline)]
    result = resolve_shadow(bars, direction="long", kind="market",
                            requested_price=None, stop_loss=149,
                            take_profit=None, spread=.02, t0=t0, pip_size=.01)
    assert result.result_code == "day_close"
    assert result.range_end == expected_deadline
    assert result.exit_at == expected_deadline
    assert result.exit_price == pytest.approx(150.19)


def test_short_market_day_close_adds_half_spread_to_close():
    t0 = datetime(2026, 11, 2, 20, 0, tzinfo=timezone.utc)
    deadline = datetime(2026, 11, 2, 21, 55, tzinfo=timezone.utc)
    bars = [_bar(ts, c=150.2) for ts in open_minutes(t0, deadline)]
    result = resolve_shadow(bars, direction="short", kind="market",
                            requested_price=None, stop_loss=151,
                            take_profit=None, spread=.02, t0=t0, pip_size=.01)
    assert result.result_code == "day_close"
    assert result.entry_price == pytest.approx(149.99)
    assert result.exit_price == pytest.approx(150.21)


def test_limit_fill_on_last_pre_close_minute_is_checked_then_closed():
    t0 = datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)
    deadline = datetime(2026, 11, 2, 21, 55, tzinfo=timezone.utc)
    bars = [_bar(ts, l=149.8 if ts == deadline - timedelta(minutes=1) else 150.0)
            for ts in open_minutes(t0, deadline)]
    result = resolve_shadow(bars, direction="long", kind="limit",
                            requested_price=149.9, stop_loss=149,
                            take_profit=None, spread=0, t0=t0, pip_size=.01)
    assert result.result_code == "day_close"
    assert result.entry_at == deadline - timedelta(minutes=1)
    assert result.exit_at == deadline


def test_limit_fill_after_rollover_uses_next_rollover_deadline():
    t0 = datetime(2026, 11, 1, 22, 0, tzinfo=timezone.utc)
    deadline = datetime(2026, 11, 2, 21, 55, tzinfo=timezone.utc)
    bars = [_bar(ts, l=149.8 if ts == t0 else 150.0)
            for ts in open_minutes(t0, deadline)]
    result = resolve_shadow(bars, direction="long", kind="limit",
                            requested_price=149.9, stop_loss=149,
                            take_profit=None, spread=0, t0=t0, pip_size=.01)
    assert result.result_code == "day_close"
    assert result.range_end == deadline
    assert len(open_minutes(t0, deadline)) <= 1680


@pytest.mark.parametrize("t0", [
    datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 7, 3, 20, 58, tzinfo=timezone.utc),
])
def test_market_started_when_next_minute_is_past_close_cutoff_expires(t0):
    result = resolve_shadow([], direction="long", kind="market",
                            requested_price=None, stop_loss=149,
                            take_profit=None, spread=0, t0=t0, pip_size=.01)
    assert result.state == "expired"
    assert result.result_code == "no_fill"


def test_market_saturday_expires_before_sunday_open():
    t0 = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    sunday_open = datetime(2026, 7, 5, 21, 0, tzinfo=timezone.utc)
    result = resolve_shadow([_bar(sunday_open)], direction="long", kind="market",
                            requested_price=None, stop_loss=149,
                            take_profit=None, spread=0, t0=t0, pip_size=.01)
    assert result.result_code == "no_fill"


def test_marker_after_take_profit_does_not_change_resolution():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    bars = [_bar(t0), _bar(t0 + timedelta(minutes=1), h=151, c=150.8)]
    result = resolve_shadow(bars, direction="long", kind="market",
                            requested_price=None, stop_loss=149,
                            take_profit=150.5, spread=0, t0=t0, pip_size=.01,
                            markers={t0 + timedelta(minutes=2)})
    assert result.result_code == "tp"


def test_limit_marker_before_fill_makes_entry_indeterminate():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    bars = [_bar(t0), _bar(t0 + timedelta(minutes=1))]
    result = resolve_shadow(bars, direction="long", kind="limit",
                            requested_price=149, stop_loss=148,
                            take_profit=None, spread=0, t0=t0, pip_size=.01,
                            markers={t0})
    assert result.result_code == "data_insufficient"


def test_unrecoverable_normalization_resolves_as_data_insufficient():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    normalized = NormalizedBars((), 1440, False, unrecoverable=True)
    result = resolve_shadow(normalized, direction="long", kind="market",
                            requested_price=None, stop_loss=149,
                            take_profit=None, spread=0, t0=t0, pip_size=.01)
    assert result.result_code == "data_insufficient"
    assert result.entry_price is None
