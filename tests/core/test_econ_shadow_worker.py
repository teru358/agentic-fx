from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core.contracts import Bar
from agentic_fx.core.paper_fills import resolve_shadow, select_limit_entry
from agentic_fx.store.ohlcv_slices import open_minutes, SliceRow, QUALITY_OUTAGE
from agentic_fx.datafeed.econ_bars import NormalizedBars


def _bar(ts, o=150.0, h=150.2, l=149.8, c=150.0):
    return Bar("USDJPY", "1m", ts, o, h, l, c, 1)


def _marker(ts):
    return SliceRow(ts, None, None, None, None, None, "econ-normalizer", QUALITY_OUTAGE)


def _span(start, end, replace=None, markers=(), **defaults):
    """[start,end) の開場 minute 全部の足。replace の minute は差し替え、markers の minute は欠損の印にする。"""
    replace = replace or {}
    rows = []
    for ts in open_minutes(start, end):
        if ts in markers:
            rows.append(_marker(ts))
        elif ts in replace:
            rows.append(replace[ts])
        else:
            rows.append(_bar(ts, **defaults))
    return rows


def test_market_entry_uses_half_spread_and_tp_after_entry_bar():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=1)
    bars = _span(t0, datetime(2026, 11, 2, 21, 55, tzinfo=timezone.utc), {t1: _bar(t1, h=151, c=150.8)})
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
    bars = _span(t0, datetime(2026, 11, 2, 21, 55, tzinfo=timezone.utc), markers={t0 + timedelta(minutes=1)})
    result = resolve_shadow(bars, direction="long", kind="market",
                            requested_price=None, stop_loss=149,
                            take_profit=None, spread=0, t0=t0, pip_size=.01)
    assert result.result_code == "data_insufficient"


def test_fill_bar_sl_wins_when_fill_bar_also_touches_take_profit():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    bars = _span(t0, datetime(2026, 11, 2, 21, 55, tzinfo=timezone.utc), {t0: _bar(t0, h=151, l=148.5, c=150)}, l=150.0)
    result = resolve_shadow(bars, direction="long", kind="limit",
                            requested_price=150, stop_loss=149,
                            take_profit=150.5, spread=0, t0=t0, pip_size=.01)
    assert result.result_code == "sl"


def test_fill_bar_tp_only_is_not_resolved_until_following_bar():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=1)
    bars = _span(t0, datetime(2026, 11, 2, 21, 55, tzinfo=timezone.utc), {t0: _bar(t0, h=151, l=149.5, c=150),
                          t1: _bar(t1, h=151, l=149.5, c=150.7)}, l=150.0)
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
    bars = _span(t0, datetime(2026, 11, 2, 21, 55, tzinfo=timezone.utc),
                 {t0: _bar(t0, o=open_price, h=open_price + .1,
                           l=open_price - .1, c=open_price)}, l=150.0)
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
    last = deadline - timedelta(minutes=1)
    bars = _span(t0, t0 + timedelta(hours=4), {last: _bar(last, l=149.8)}, l=150.0)
    result = resolve_shadow(bars, direction="long", kind="limit",
                            requested_price=149.9, stop_loss=149,
                            take_profit=None, spread=0, t0=t0, pip_size=.01)
    assert result.result_code == "day_close"
    assert result.entry_at == deadline - timedelta(minutes=1)
    assert result.exit_at == deadline


def test_limit_fill_after_rollover_uses_next_rollover_deadline():
    t0 = datetime(2026, 11, 1, 21, 50, tzinfo=timezone.utc)
    deadline = datetime(2026, 11, 2, 21, 55, tzinfo=timezone.utc)
    fill = datetime(2026, 11, 1, 22, 1, tzinfo=timezone.utc)
    bars = [_bar(ts, l=149.8 if ts == fill else 150.0)
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
    result = resolve_shadow([], direction="long", kind="market",
                            requested_price=None, stop_loss=149,
                            take_profit=None, spread=0, t0=t0, pip_size=.01)
    assert result.result_code == "no_fill"


def test_marker_after_take_profit_does_not_change_resolution():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=1)
    bars = _span(t0, datetime(2026, 11, 2, 21, 55, tzinfo=timezone.utc), {t1: _bar(t1, h=151, c=150.8)},
                 markers={t0 + timedelta(minutes=2)})
    result = resolve_shadow(bars, direction="long", kind="market",
                            requested_price=None, stop_loss=149,
                            take_profit=150.5, spread=0, t0=t0, pip_size=.01)
    assert result.result_code == "tp"


def test_limit_marker_before_fill_makes_entry_indeterminate():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    bars = _span(t0, t0 + timedelta(hours=4), markers={t0})
    result = resolve_shadow(bars, direction="long", kind="limit",
                            requested_price=149, stop_loss=148,
                            take_profit=None, spread=0, t0=t0, pip_size=.01)
    assert result.result_code == "data_insufficient"


def test_unrecoverable_normalization_resolves_as_data_insufficient():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    normalized = NormalizedBars((), 1440, False, unrecoverable=True)
    result = resolve_shadow(normalized, direction="long", kind="market",
                            requested_price=None, stop_loss=149,
                            take_profit=None, spread=0, t0=t0, pip_size=.01)
    assert result.result_code == "data_insufficient"
    assert result.entry_price is None


@pytest.mark.parametrize("day, expected_end", [
    (datetime(2026, 11, 2, 21, 55, tzinfo=timezone.utc),
     datetime(2026, 11, 2, 21, 56, tzinfo=timezone.utc)),
    (datetime(2026, 11, 2, 21, 56, tzinfo=timezone.utc),
     datetime(2026, 11, 2, 21, 57, tzinfo=timezone.utc)),
    (datetime(2026, 11, 2, 21, 57, tzinfo=timezone.utc),
     datetime(2026, 11, 2, 21, 58, tzinfo=timezone.utc)),
    (datetime(2026, 11, 2, 21, 58, tzinfo=timezone.utc),
     datetime(2026, 11, 2, 21, 59, tzinfo=timezone.utc)),
    (datetime(2026, 11, 2, 21, 59, tzinfo=timezone.utc),
     datetime(2026, 11, 2, 22, 0, tzinfo=timezone.utc)),
])
@pytest.mark.parametrize("stop_loss, expected_code", [(149, "day_close"), (150, "sl")])
def test_limit_fill_in_final_five_minutes_ends_at_fill_bar_end(day, expected_end,
                                                                stop_loss, expected_code):
    t0 = day - timedelta(minutes=5)
    fill_low = 149.5 if stop_loss == 150 else 149.8
    bars = _span(t0, t0 + timedelta(hours=4), {day: _bar(day, l=fill_low, c=150.1)},
                 l=150.0, c=150.1)
    selection = select_limit_entry(bars, t0=t0, requested_price=149.9,
                                   direction="long", spread=0)
    result = resolve_shadow(bars, direction="long", kind="limit",
                            requested_price=149.9, stop_loss=stop_loss,
                            take_profit=None, spread=0, t0=t0, pip_size=.01)
    assert selection.range_end == expected_end
    assert result.range_end == expected_end
    assert result.result_code == expected_code
    assert result.exit_at == expected_end


def test_summer_limit_fill_in_final_five_minutes_ends_at_fill_bar_end():
    t0 = datetime(2026, 7, 6, 20, 50, tzinfo=timezone.utc)
    fill = datetime(2026, 7, 6, 20, 58, tzinfo=timezone.utc)
    bars = _span(t0, t0 + timedelta(hours=4), {fill: _bar(fill, l=149.8, c=150.1)},
                 l=150, c=150.1)
    result = resolve_shadow(bars, direction="long", kind="limit",
                            requested_price=149.9, stop_loss=149,
                            take_profit=None, spread=0, t0=t0, pip_size=.01)
    assert result.range_end == datetime(2026, 7, 6, 20, 59, tzinfo=timezone.utc)
    assert result.exit_at == datetime(2026, 7, 6, 20, 59, tzinfo=timezone.utc)


def test_resolver_rejects_four_hour_selection_range_after_a_fill():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    rows = [_bar(ts) for ts in open_minutes(t0, t0 + timedelta(hours=4))]
    selected = NormalizedBars(tuple(rows), len(rows), False,
                              range_start=t0, range_end=t0 + timedelta(hours=4))
    with pytest.raises(ValueError, match="range"):
        resolve_shadow(selected, direction="long", kind="limit",
                       requested_price=150, stop_loss=149, take_profit=None,
                       spread=0, t0=t0, pip_size=.01)


def test_resolver_rejects_missing_open_minute_in_normalized_input():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    end = datetime(2026, 11, 2, 21, 55, tzinfo=timezone.utc)
    all_minutes = open_minutes(t0, end)
    missing = all_minutes[20]
    rows = [_bar(ts) for ts in all_minutes if ts != missing]
    partial = NormalizedBars(tuple(rows), len(all_minutes), False,
                             range_start=t0, range_end=end)
    with pytest.raises(ValueError, match="missing open minute"):
        resolve_shadow(partial, direction="long", kind="market",
                       requested_price=None, stop_loss=149, take_profit=None,
                       spread=0, t0=t0, pip_size=.01)


def test_closed_only_limit_range_can_be_empty_and_expire():
    t0 = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    end = t0 + timedelta(hours=4)
    closed = NormalizedBars((), 0, False, range_start=t0, range_end=end)
    result = resolve_shadow(closed, direction="long", kind="limit",
                            requested_price=149, stop_loss=148, take_profit=None,
                            spread=0, t0=t0, pip_size=.01)
    assert result.state == "expired"
    assert result.result_code == "no_fill"


def test_closed_only_saturday_market_range_can_be_empty_and_expire():
    t0 = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 4, 20, 55, tzinfo=timezone.utc)
    closed = NormalizedBars((), 0, False, range_start=t0, range_end=end)
    result = resolve_shadow(closed, direction="long", kind="market",
                            requested_price=None, stop_loss=149, take_profit=None,
                            spread=0, t0=t0, pip_size=.01)
    assert result.state == "expired"
    assert result.result_code == "no_fill"


def test_resolver_rejects_plain_rows_missing_an_open_minute():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    end = datetime(2026, 11, 2, 21, 55, tzinfo=timezone.utc)
    all_minutes = open_minutes(t0, end)
    rows = [_bar(ts) for ts in all_minutes if ts != all_minutes[-1]]
    with pytest.raises(ValueError, match="missing open minute"):
        resolve_shadow(rows, direction="long", kind="market",
                       requested_price=None, stop_loss=149, take_profit=None,
                       spread=0, t0=t0, pip_size=.01)
    with pytest.raises(ValueError, match="missing open minute"):
        resolve_shadow(rows[:100], direction="long", kind="limit",
                       requested_price=140, stop_loss=139, take_profit=None,
                       spread=0, t0=t0, pip_size=.01)
