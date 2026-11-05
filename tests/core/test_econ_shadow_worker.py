import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core import econ_slice_worker as worker_mod
from agentic_fx.core.contracts import Bar, Quote
from agentic_fx.core.econ_slice_worker import EconSliceWorker
from agentic_fx.core.paper_fills import resolve_shadow, select_limit_entry
from agentic_fx.core.writer_admission import WriterAdmission
from agentic_fx.store import ohlcv_slices as os_
from agentic_fx.store import signal_decisions as sd
from agentic_fx.store import signal_shadow as ss
from agentic_fx.store.db import connect, connect_readonly, init_db
from agentic_fx.store.ohlcv_slices import open_minutes, SliceRow, QUALITY_OUTAGE
from agentic_fx.store.short_tx import Conflict, short_tx
from agentic_fx.datafeed.econ_bars import NormalizedBars, normalize_1m


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


def test_limit_with_day_close_before_four_hours_resolves_from_its_own_range():
    t0 = datetime(2026, 11, 2, 19, 0, tzinfo=timezone.utc)
    end = datetime(2026, 11, 2, 21, 55, tzinfo=timezone.utc)
    fill = _bar(t0, o=150.0, h=150.1, l=149.85, c=150.0)
    rows = _span(t0, end, replace={t0: fill}, l=149.95)
    normalized = NormalizedBars(tuple(rows), len(rows), False,
                                range_start=t0, range_end=end)
    for given in (rows, normalized):
        result = resolve_shadow(given, direction="long", kind="limit",
                                requested_price=149.9, stop_loss=149.0,
                                take_profit=None, spread=0, t0=t0, pip_size=.01)
        assert result.result_code == "day_close"
        assert result.entry_at == t0
        assert result.range_end == end
        assert result.exit_at == end


def test_limit_selection_needs_minutes_only_up_to_the_fill_bar():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    fill_at = t0 + timedelta(minutes=3)
    rows = _span(t0, fill_at + timedelta(minutes=1),
                 replace={fill_at: _bar(fill_at, l=149.85)}, l=150.0)
    selection = select_limit_entry(rows, t0=t0, requested_price=149.9,
                                   direction="long", spread=0)
    assert selection.fill_bar.ts == fill_at
    assert selection.range_end == datetime(2026, 11, 2, 21, 55,
                                           tzinfo=timezone.utc)
    gap = [row for row in rows if row.ts != t0 + timedelta(minutes=1)]
    with pytest.raises(ValueError, match="missing open minute"):
        select_limit_entry(gap, t0=t0, requested_price=149.9,
                           direction="long", spread=0)
    no_fill = _span(t0, fill_at + timedelta(minutes=1), l=150.0)
    with pytest.raises(ValueError, match="missing open minute"):
        select_limit_entry(no_fill, t0=t0, requested_price=149.9,
                           direction="long", spread=0)


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


def test_market_entry_bar_take_profit_resolves_on_that_bar():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    bars = _span(t0, datetime(2026, 11, 2, 21, 55, tzinfo=timezone.utc),
                 {t0: _bar(t0, h=151, l=149.5, c=150)}, l=150.0)
    result = resolve_shadow(bars, direction="long", kind="market",
                            requested_price=None, stop_loss=149,
                            take_profit=150.5, spread=0, t0=t0, pip_size=.01)
    assert result.result_code == "tp"
    assert result.exit_at == t0 + timedelta(minutes=1)


def test_market_entry_bar_touching_both_stop_and_target_is_a_stop():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    bars = _span(t0, datetime(2026, 11, 2, 21, 55, tzinfo=timezone.utc),
                 {t0: _bar(t0, h=151, l=148.5, c=150)}, l=150.0)
    result = resolve_shadow(bars, direction="long", kind="market",
                            requested_price=None, stop_loss=149,
                            take_profit=150.5, spread=0, t0=t0, pip_size=.01)
    assert result.result_code == "sl"
    assert result.exit_at == t0 + timedelta(minutes=1)


def test_duplicate_minute_in_plain_rows_is_rejected():
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    end = datetime(2026, 11, 2, 21, 55, tzinfo=timezone.utc)
    rows = [_bar(ts) for ts in open_minutes(t0, end)]
    rows.append(_bar(t0 + timedelta(minutes=5)))
    with pytest.raises(ValueError, match="duplicate minute"):
        resolve_shadow(rows, direction="long", kind="market",
                       requested_price=None, stop_loss=149, take_profit=None,
                       spread=0, t0=t0, pip_size=.01)
    with pytest.raises(ValueError, match="duplicate minute"):
        resolve_shadow(rows, direction="long", kind="limit",
                       requested_price=140, stop_loss=139, take_profit=None,
                       spread=0, t0=t0, pip_size=.01)


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


# --- slice worker ---------------------------------------------------------------

UTC = timezone.utc
SEED_START = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
SEED_END = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)
EVENT_TS = datetime(2026, 11, 2, 12, 30, tzinfo=UTC)
DECIDED = datetime(2026, 11, 2, 12, 0, 30, tzinfo=UTC)
LATE = datetime(2026, 11, 3, 22, 30, tzinfo=UTC)
PLAIN = (150.1, 150.2, 150.0, 150.1)


class _Clock:
    """now_fn と monotonic を兼ねる。"""

    def __init__(self, now):
        self.now = now
        self.t = 0.0

    def __call__(self):
        return self.now

    def monotonic(self):
        return self.t


class _World:
    def __init__(self, path):
        self.path = path
        self.conns = []
        self.db = self.open()
        init_db(self.db)
        self.db.commit()

    def open(self, *, read_only=False, busy=None):
        conn = connect_readonly(self.path) if read_only else connect(self.path)
        if busy is not None:
            conn.execute(f"PRAGMA busy_timeout={busy}")
        self.conns.append(conn)
        return conn

    def pair(self):
        return self.open(read_only=True), self.open(busy=0)

    def close(self):
        for conn in self.conns:
            conn.close()


@pytest.fixture
def worlds(tmp_path):
    made = []

    def make(name="worker.db"):
        made.append(_World(tmp_path / name))
        return made[-1]

    yield make
    for w in made:
        w.close()


@pytest.fixture
def world(worlds):
    return worlds()


def _seed(db, overrides=None, start=SEED_START, end=SEED_END):
    overrides = overrides or {}
    rows = []
    for ts in open_minutes(start, end):
        o, h, l, c = overrides.get(ts, PLAIN)
        rows.append(("USDJPY", "1m", ts.isoformat(), o, h, l, c, 1.0, "mt5-live"))
    db.executemany(
        "INSERT INTO ohlcv_cache (symbol,interval,bar_time,open,high,low,close,"
        "volume,source) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    db.commit()


def _unavailable(pair):
    raise RuntimeError("no quote")


def _worker(world, clock, *, rw=None, quote_fn=_unavailable, admission=None,
            lock=None, cls=EconSliceWorker, sleep=None, monotonic=None,
            on_complete=None, start=True):
    read, write = rw or world.pair()
    w = cls(read_conn=read, write_conn=write, core_lock=lock or threading.RLock(),
            admission=admission or WriterAdmission(), quote_fn=quote_fn,
            pip_size_fn=lambda pair: 0.01, assumed_spread_pips_fn=lambda pair: 1.5,
            now_fn=clock, monotonic=monotonic or (lambda: 0.0),
            sleep=sleep or (lambda seconds: None), on_complete=on_complete)
    if start:
        assert w.start()
    return w


def _event_set(db, ts=EVENT_TS, *, pair="USDJPY", name="CPI"):
    event_id = db.execute(
        "INSERT INTO econ_events (ts,country,name,importance) VALUES (?,?,?,3)",
        (ts.isoformat(), "USD", name)).lastrowid
    set_id = os_.create_event_set_in_tx(db, event_id=event_id, pair=pair,
                                        now=ts - timedelta(days=1))
    db.commit()
    return set_id


def _shadow(db, n, *, decided=DECIDED, direction="long", entry_type="market",
            limit_price=None, stop_loss=149.0, take_profit=None,
            spread_pips=2.0, source="assumed", accept=False):
    payload = {"action": "open", "direction": direction,
               "entry_type": entry_type, "limit_price": limit_price,
               "stop_loss": stop_loss, "take_profit": take_profit}
    mission = db.execute(
        "INSERT INTO missions (loop,runner,model,status,started_at) "
        "VALUES ('trade','local','m','running',?)",
        (decided.isoformat(),)).lastrowid
    signal = db.execute(
        "INSERT INTO signals (plugin,content_hash,pair,timeframe,bar_ts,kind,"
        "payload_json,status,claimed_by_mission_id,claimed_at,created_at,"
        "contract_version) VALUES ('p',?,'USDJPY','15m',?,'strategy',?,"
        "'claimed',?,?,?,'executable_v1')",
        (f"h{n}", (SEED_START + timedelta(minutes=15 * n)).isoformat(),
         json.dumps(payload), mission, decided.isoformat(),
         decided.isoformat())).lastrowid
    db.commit()
    spread = ss.ShadowSpread(pip_size=0.01, spread_pips=spread_pips, source=source)
    if accept:
        return sd.commit_accept(
            db, mission_id=mission, signal_id=signal,
            intent_payload={"pair": "USDJPY", "action": "open"}, spread=spread,
            output=None, transcript=[], now=decided).shadow_id
    return sd.commit_reject(
        db, mission_id=mission, signal_id=signal, actor="system",
        reject_code="econ_window", spread=spread, output=None, transcript=[],
        now=decided).shadow_id


def _sets(db):
    return [dict(r) for r in db.execute(
        "SELECT * FROM ohlcv_slice_sets ORDER BY id")]


def _rows_in(db, set_id):
    return db.execute("SELECT COUNT(*) FROM ohlcv_slices WHERE set_id=?",
                      (set_id,)).fetchone()[0]


def _at(stamp):
    return datetime.fromisoformat(stamp)


def _normalized(db, start, end):
    return list(normalize_1m(db, "USDJPY", start, end).rows)


class _Crash(BaseException):
    pass


class _CrashingWorker(EconSliceWorker):
    """n 本目の書込みの commit 直後に落ちる (処理の途中で止まる)。"""
    crash_after = None

    def _write(self, op, *, locked=False):
        result = super()._write(op, locked=locked)
        if self.crash_after is not None:
            self.crash_after -= 1
            if self.crash_after <= 0:
                raise _Crash
        return result


class _TamperingWorker(EconSliceWorker):
    """最後の batch の後、読戻しの前に 1 行を別接続から書き換える。"""
    tamper = None

    def _before_readback(self, set_id):
        if self.tamper is None:
            return
        conn = self.tamper
        self.tamper = None
        conn.execute(
            "UPDATE ohlcv_slices SET volume = volume + 1 WHERE set_id=? AND "
            "bar_time=(SELECT MIN(bar_time) FROM ohlcv_slices WHERE set_id=?)",
            (set_id, set_id))
        conn.commit()


def test_accept_and_reject_shadows_end_with_their_stored_spread(world):
    _seed(world.db)
    accepted = _shadow(world.db, 0, accept=True, take_profit=150.15,
                       spread_pips=2.0, source="quote")
    rejected = _shadow(world.db, 1, direction="short", stop_loss=151.0,
                       spread_pips=4.0, source="assumed")
    done = []
    w = _worker(world, _Clock(LATE), on_complete=done.append)
    report = w.run_cycle()
    a = ss.get(world.db, accepted)
    assert (a["state"], a["result_code"]) == ("resolved", "tp")
    assert a["entry_price"] == pytest.approx(150.11)
    assert a["exit_price"] == pytest.approx(150.14)
    assert a["pnl_pips"] == pytest.approx(3.0)
    assert _at(a["resolved_at"]) == datetime(2026, 11, 2, 12, 2, tzinfo=UTC)
    r = ss.get(world.db, rejected)
    assert (r["state"], r["result_code"]) == ("resolved", "day_close")
    assert r["entry_price"] == pytest.approx(150.08)
    assert r["exit_price"] == pytest.approx(150.12)
    assert r["pnl_pips"] == pytest.approx(-4.0)
    assert _at(r["resolved_at"]) == datetime(2026, 11, 2, 21, 55, tzinfo=UTC)
    assert [s["state"] for s in _sets(world.db)] == ["complete", "complete"]
    assert sorted(s["id"] for s in done) == sorted(report.completed)
    assert {s["state"] for s in done} == {"complete"}


def test_a_shadow_does_not_end_before_its_final_point(world):
    _seed(world.db)
    shadow = _shadow(world.db, 0)
    clock = _Clock(datetime(2026, 11, 3, 21, 54, tzinfo=UTC))
    w = _worker(world, clock)
    w.run_cycle()
    assert ss.get(world.db, shadow)["state"] == "pending_entry"
    assert _sets(world.db) == []
    clock.now = datetime(2026, 11, 3, 21, 55, tzinfo=UTC)
    w.run_cycle()
    assert ss.get(world.db, shadow)["state"] == "resolved"


def test_limit_shadows_end_in_two_stages_and_stage_only_their_own_range(world):
    fill = (150.0, 150.05, 149.8, 150.0)
    early = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)
    _seed(world.db, {datetime(2026, 11, 2, 12, 30, tzinfo=UTC): fill,
                     early: fill})
    filled = _shadow(world.db, 0, entry_type="limit", limit_price=149.9)
    unfilled = _shadow(world.db, 1, entry_type="limit", limit_price=140.0)
    near_close = _shadow(world.db, 2, entry_type="limit", limit_price=149.9,
                         decided=early)
    clock = _Clock(datetime(2026, 11, 3, 17, 0, tzinfo=UTC))
    w = _worker(world, clock)
    w.run_cycle()
    u = ss.get(world.db, unfilled)
    assert (u["state"], u["result_code"]) == ("expired", "no_fill")
    sets = {s["shadow_id"]: s for s in _sets(world.db)}
    assert set(sets) == {unfilled}
    assert _rows_in(world.db, sets[unfilled]["id"]) == 240
    assert _at(sets[unfilled]["range_end"]) == datetime(2026, 11, 2, 16, 1,
                                                        tzinfo=UTC)
    assert ss.get(world.db, filled)["state"] == "pending_entry"
    assert ss.get(world.db, near_close)["state"] == "pending_entry"
    clock.now = datetime(2026, 11, 3, 22, 0, tzinfo=UTC)
    w.run_cycle()
    sets = {s["shadow_id"]: s for s in _sets(world.db)}
    for shadow_id, rows, entry_at in (
            (filled, 594, datetime(2026, 11, 2, 12, 30, tzinfo=UTC)),
            (near_close, 235, early)):
        row = ss.get(world.db, shadow_id)
        assert (row["state"], row["result_code"]) == ("resolved", "day_close")
        assert row["entry_price"] == pytest.approx(149.9)
        assert _at(row["entry_at"]) == entry_at
        assert row["exit_price"] == pytest.approx(150.09)
        assert row["pnl_pips"] == pytest.approx(19.0)
        assert sets[shadow_id]["state"] == "complete"
        assert _rows_in(world.db, sets[shadow_id]["id"]) == rows
    assert _at(sets[near_close]["range_end"]) == datetime(2026, 11, 2, 21, 55,
                                                          tzinfo=UTC)


def test_two_workers_finish_every_owner_exactly_once(world):
    _seed(world.db)
    shadows = [_shadow(world.db, i, spread_pips=1.0 + i) for i in range(4)]
    events = [_event_set(world.db, EVENT_TS + timedelta(minutes=i),
                         name=f"E{i}") for i in range(3)]
    done, guard = [], threading.Lock()

    def record(row):
        with guard:
            done.append(row["id"])

    clock = _Clock(LATE)
    workers = [_worker(world, clock, on_complete=record, sleep=time.sleep,
                       monotonic=time.monotonic) for _ in range(2)]
    assert workers[1].generation > workers[0].generation
    barrier = threading.Barrier(2)
    errors = []

    def drive(w):
        try:
            barrier.wait(timeout=10)
            for _ in range(200):
                with guard:
                    if len(done) >= len(shadows) + len(events):
                        return
                w.run_cycle()
                time.sleep(0.002)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=drive, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    assert len(done) == len(set(done)) == len(shadows) + len(events)
    assert all(ss.get(world.db, i)["state"] == "resolved" for i in shadows)
    assert {s["state"] for s in _sets(world.db)} == {"complete"}
    assert {s["id"] for s in _sets(world.db)} == set(done)
    assert events


def test_recorded_commit_times_are_kept(world):
    _seed(world.db)
    _shadow(world.db, 0)
    w = _worker(world, _Clock(LATE))
    assert w.commit_seconds == (w.commit_seconds[0],)  # 世代登録の COMMIT
    w.run_cycle()
    assert len(w.commit_seconds) > 1
    assert all(seconds >= 0 for seconds in w.commit_seconds)


# --- 発表前 spread と event の退避 --------------------------------------------------

SPREAD_TS = datetime(2026, 11, 4, 12, 30, tzinfo=UTC)


def _quote(bid=150.000, ask=150.020):
    return Quote("USDJPY", bid, ask, SPREAD_TS, "mt5")


def test_a_spread_candidate_is_recorded_before_matured_slices(world):
    _seed(world.db)
    old = _event_set(world.db, EVENT_TS)
    os_.record_event_spread(world.db, old, spread_pips=1.0,
                            spread_source="assumed")
    upcoming = _event_set(world.db, SPREAD_TS, name="NFP")
    clock = _Clock(SPREAD_TS - timedelta(minutes=4))
    calls = []

    def quote(pair):
        calls.append(pair)
        return _quote()

    read, write = world.pair()
    statements = []
    write.set_trace_callback(statements.append)
    w = _worker(world, clock, rw=(read, write), quote_fn=quote)
    clock.now = SPREAD_TS - timedelta(minutes=4)
    report = w.run_cycle()
    spread_at = next(i for i, s in enumerate(statements)
                     if s.startswith("UPDATE ohlcv_slice_sets SET spread_pips"))
    insert_at = next(i for i, s in enumerate(statements)
                     if s.startswith("INSERT INTO ohlcv_slices"))
    assert spread_at < insert_at
    row = os_.get_set(world.db, upcoming)
    assert row["spread_source"] == "quote"
    assert row["spread_pips"] == pytest.approx(2.0)
    assert report.spreads == 1 and calls == ["USDJPY"]
    w.run_cycle()
    assert calls == ["USDJPY"]
    assert sum(s.startswith("UPDATE ohlcv_slice_sets SET spread_pips")
               for s in statements) == 1
    assert os_.get_set(world.db, old)["state"] == "complete"


def test_a_failed_quote_before_ts_records_nothing_and_a_restart_retries(world):
    upcoming = _event_set(world.db, SPREAD_TS, name="NFP")
    clock = _Clock(SPREAD_TS - timedelta(minutes=3))
    first = _worker(world, clock)
    first.run_cycle()
    first.run_cycle()
    assert os_.get_set(world.db, upcoming)["spread_pips"] is None
    quotes = iter([None])
    again = _worker(world, clock, quote_fn=lambda pair: next(quotes, None))
    again.run_cycle()
    assert os_.get_set(world.db, upcoming)["spread_pips"] is None
    restarted = _worker(world, clock, quote_fn=lambda pair: _quote(150.0, 150.03))
    restarted.run_cycle()
    row = os_.get_set(world.db, upcoming)
    assert row["spread_source"] == "quote"
    assert row["spread_pips"] == pytest.approx(3.0)


def test_an_unusable_quote_is_recorded_as_assumed(world):
    upcoming = _event_set(world.db, SPREAD_TS, name="NFP")
    clock = _Clock(SPREAD_TS - timedelta(minutes=1))
    w = _worker(world, clock, quote_fn=lambda pair: _quote(150.0, 150.0))
    w.run_cycle()
    row = os_.get_set(world.db, upcoming)
    assert (row["spread_source"], row["spread_pips"]) == ("assumed", 1.5)


def test_after_ts_the_spread_is_fixed_as_assumed_and_never_replaced(world):
    upcoming = _event_set(world.db, SPREAD_TS, name="NFP")
    clock = _Clock(SPREAD_TS + timedelta(minutes=1))
    calls = []

    def quote(pair):
        calls.append(pair)
        return _quote()

    w = _worker(world, clock, quote_fn=quote)
    w.run_cycle()
    row = os_.get_set(world.db, upcoming)
    assert (row["spread_source"], row["spread_pips"]) == ("assumed", 1.5)
    clock.now = SPREAD_TS + timedelta(minutes=2)
    w.run_cycle()
    assert os_.get_set(world.db, upcoming)["spread_source"] == "assumed"
    assert calls == []


def test_a_candidate_outside_the_lead_window_is_left_alone(world):
    upcoming = _event_set(world.db, SPREAD_TS, name="NFP")
    calls = []
    w = _worker(world, _Clock(SPREAD_TS - timedelta(minutes=6)),
                quote_fn=lambda pair: calls.append(pair))
    w.run_cycle()
    assert calls == [] and os_.get_set(world.db, upcoming)["spread_pips"] is None


def test_event_and_shadow_rows_survive_loss_of_the_cache(world):
    _seed(world.db)
    set_id = _event_set(world.db)
    shadow = _shadow(world.db, 0)
    w = _worker(world, _Clock(LATE))
    w.run_cycle()
    shadow_set = next(s["id"] for s in _sets(world.db) if s["shadow_id"] == shadow)
    read = world.open(read_only=True)
    event_rows = os_.complete_rows(read, set_id)
    shadow_rows = os_.complete_rows(read, shadow_set)
    assert len(event_rows) == 255 and len(shadow_rows) == 594
    world.db.execute("DELETE FROM ohlcv_cache")
    world.db.commit()
    assert os_.complete_rows(read, set_id) == event_rows
    assert os_.complete_rows(read, shadow_set) == shadow_rows
    assert event_rows == [
        SliceRow(ts, *PLAIN, 1.0, "mt5-live", "observed") for ts in
        open_minutes(EVENT_TS - timedelta(minutes=15),
                     EVENT_TS + timedelta(hours=4))]


def test_a_worker_restart_reclaims_old_staging_and_converges(world):
    _seed(world.db)
    set_id = _event_set(world.db)
    os_.record_event_spread(world.db, set_id, spread_pips=1.7,
                            spread_source="quote")
    shadow = _shadow(world.db, 0)
    clock = _Clock(LATE)
    first = _worker(world, clock, cls=_CrashingWorker)
    first.crash_after = 1
    with pytest.raises(_Crash):
        first.run_cycle()
    staged = os_.get_set(world.db, set_id)
    assert staged["state"] == "staging" and _rows_in(world.db, set_id) == 240
    second = _worker(world, clock, cls=_CrashingWorker)
    assert second.generation > first.generation
    reclaimed = os_.get_set(world.db, set_id)
    assert reclaimed["state"] == "awaiting" and reclaimed["generation"] is None
    assert (reclaimed["spread_pips"], reclaimed["spread_source"]) == (1.7, "quote")
    assert _rows_in(world.db, set_id) == 0
    second.crash_after = 4
    with pytest.raises(_Crash):
        second.run_cycle()
    assert [s for s in _sets(world.db) if s["shadow_id"] == shadow]
    third = _worker(world, clock)
    assert third.generation > second.generation
    assert [s for s in _sets(world.db) if s["shadow_id"] == shadow] == []
    assert os_.get_set(world.db, set_id)["state"] == "complete"
    third.run_cycle()
    final = os_.get_set(world.db, set_id)
    assert final["state"] == "complete" and final["spread_pips"] == 1.7
    read = world.open(read_only=True)
    assert os_.complete_rows(read, set_id) == _normalized(
        world.db, EVENT_TS - timedelta(minutes=15), EVENT_TS + timedelta(hours=4))
    assert ss.get(world.db, shadow)["state"] == "resolved"


def test_a_row_changed_after_staging_is_never_completed(world):
    _seed(world.db)
    set_id = _event_set(world.db)
    os_.record_event_spread(world.db, set_id, spread_pips=1.7,
                            spread_source="quote")
    w = _worker(world, _Clock(LATE), cls=_TamperingWorker)
    w.tamper = world.open(busy=5000)
    report = w.run_cycle()
    row = os_.get_set(world.db, set_id)
    assert row["state"] == "awaiting" and row["generation"] is None
    assert (row["spread_pips"], row["spread_source"]) == (1.7, "quote")
    assert _rows_in(world.db, set_id) == 0
    assert report.completed == [] and report.discarded == 1
    w.run_cycle()
    assert os_.get_set(world.db, set_id)["state"] == "complete"
    read = world.open(read_only=True)
    assert os_.complete_rows(read, set_id) == _normalized(
        world.db, EVENT_TS - timedelta(minutes=15), EVENT_TS + timedelta(hours=4))


def test_a_shadow_row_changed_after_staging_drops_its_set_and_is_redone(world):
    _seed(world.db)
    shadow = _shadow(world.db, 0)
    w = _worker(world, _Clock(LATE), cls=_TamperingWorker)
    w.tamper = world.open(busy=5000)
    w.run_cycle()
    assert [s for s in _sets(world.db) if s["shadow_id"] == shadow] == []
    assert ss.get(world.db, shadow)["state"] == "pending_entry"
    w.run_cycle()
    assert ss.get(world.db, shadow)["state"] == "resolved"
    assert ss.get(world.db, shadow)["pnl_pips"] == pytest.approx(-2.0)


def test_rows_left_in_a_cancelled_set_are_swept_every_cycle(world):
    set_id = _event_set(world.db)
    gen = os_.new_generation(world.db, now=LATE)
    start = EVENT_TS - timedelta(minutes=15)
    rows = [SliceRow(ts, 150.0, 150.1, 149.9, 150.0, 1.0, "mt5-live", "observed")
            for ts in open_minutes(start, EVENT_TS + timedelta(hours=4))]
    assert os_.stage_event_rows(world.db, set_id=set_id, generation=gen,
                                rows=rows[:240], assumed_spread_pips=1.0,
                                ).committed
    assert os_.cancel_set_in_tx(world.db, set_id, now=LATE)
    world.db.commit()
    w = _worker(world, _Clock(LATE))
    w.run_cycle()
    assert _rows_in(world.db, set_id) == 0
    assert os_.get_set(world.db, set_id)["state"] == "cancelled"
    world.db.execute(
        "INSERT INTO ohlcv_slices (set_id,bar_time,open,high,low,close,volume,"
        "source,quality) VALUES (?,?,?,?,?,?,?,?,?)",
        (set_id, rows[0].bar_time.isoformat(), 150.0, 150.1, 149.9, 150.0, 1.0,
         "mt5-live", "observed"))
    world.db.commit()
    w.run_cycle()
    assert _rows_in(world.db, set_id) == 0
    assert os_.get_set(world.db, set_id)["state"] == "cancelled"


# --- writer admission と BUSY ---------------------------------------------------------

def test_a_requested_scheduler_write_keeps_the_worker_from_beginning(world):
    _seed(world.db)
    _event_set(world.db)
    shadow = _shadow(world.db, 0)
    admission = WriterAdmission()
    w = _worker(world, _Clock(LATE), admission=admission)
    admission.request()
    try:
        report = w.run_cycle()
        assert report.yielded and report.completed == []
        assert [x["state"] for x in _sets(world.db)] == ["awaiting"]
        assert world.db.execute("SELECT COUNT(*) FROM ohlcv_slices"
                                ).fetchone()[0] == 0
        assert admission.stats().yielded >= 1
    finally:
        admission.release()
    assert admission.stats().beginning == 0
    w.run_cycle()
    assert ss.get(world.db, shadow)["state"] == "resolved"


def test_every_write_outcome_returns_its_lease(world):
    clock = _Clock(LATE)
    admission = WriterAdmission()
    wconn = world.open(busy=0)
    w = _worker(world, clock, admission=admission, monotonic=clock.monotonic,
                rw=(world.open(read_only=True), wconn))

    def run(body, *, begin_first=None):
        return w._write(lambda begin, checkpoint:
                        short_tx(wconn, begin, body, checkpoint))

    assert run(lambda: {}).status == "committed"
    assert admission.stats().beginning == 0

    def conflict():
        raise Conflict

    assert run(conflict).status == "conflict"
    assert admission.stats().beginning == 0

    def slow():
        clock.t += 0.06
        return {}

    assert run(slow).status == "over_budget"
    assert admission.stats().beginning == 0

    admission.request()
    try:
        assert run(lambda: {}) is None
    finally:
        admission.release()
    assert admission.stats().beginning == 0

    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return {}

    w._spent = 0.0
    w._abort = False
    assert run(flaky).status == "committed" and len(attempts) == 2
    assert admission.stats().beginning == 0

    holder = world.open(busy=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        w._spent = 0.0
        assert run(lambda: {}) is None
    finally:
        holder.rollback()
    assert admission.stats().beginning == 0
    assert not wconn.in_transaction


def test_busy_is_retried_with_backoff_inside_the_cycle_budget(world):
    _seed(world.db)
    set_id = _event_set(world.db)
    sleeps = []
    w = _worker(world, _Clock(LATE), sleep=sleeps.append)
    holder = world.open(busy=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        w.run_cycle()
        assert sleeps == [0.01, 0.02, 0.04, 0.05, 0.05, 0.05, 0.05]
        assert _rows_in(world.db, set_id) == 0
    finally:
        holder.rollback()
    w.run_cycle()
    assert os_.get_set(world.db, set_id)["state"] == "complete"
    assert len(sleeps) == 7


# --- 予算 ---------------------------------------------------------------------------------

def _statement_rows(statement):
    return statement.count("),(") + 1


def _step_clock(write, clock, step):
    def trace(statement):
        if statement.startswith("INSERT INTO ohlcv_slices"):
            clock.t += step(statement)
    write.set_trace_callback(trace)


def _shadow_fields(db, shadow_id):
    row = ss.get(db, shadow_id)
    return {k: row[k] for k in ("state", "result_code", "entry_price",
                                "exit_price", "pnl_pips", "entry_at",
                                "resolved_at")}


def test_a_long_shadow_continues_across_cycles_to_the_same_result(worlds):
    fill = (150.0, 150.05, 149.8, 150.0)
    late_fill = datetime(2026, 11, 2, 22, 1, tzinfo=UTC)
    decided = datetime(2026, 11, 2, 18, 5, tzinfo=UTC)
    now = datetime(2026, 11, 4, 22, 0, tzinfo=UTC)
    outcomes = []
    for name, step in (("slow.db", 0.045), ("fast.db", 0.0)):
        w_ = worlds(name)
        _seed(w_.db, {late_fill: fill})
        shadow = _shadow(w_.db, 0, entry_type="limit", limit_price=149.9,
                         decided=decided)
        clock = _Clock(now)
        read, write = w_.pair()
        _step_clock(write, clock, lambda statement, step=step: step)
        w = _worker(w_, clock, rw=(read, write), monotonic=clock.monotonic)
        cycles = 0
        sizes = []
        while ss.get(w_.db, shadow)["state"] == "pending_entry":
            w.run_cycle()
            cycles += 1
            sets = _sets(w_.db)
            sizes.append(_rows_in(w_.db, sets[0]["id"]) if sets else 0)
            assert cycles < 10
        outcomes.append((cycles, sizes, _shadow_fields(w_.db, shadow),
                         os_.complete_rows(read, _sets(w_.db)[0]["id"])))
    (slow_cycles, slow_sizes, slow_fields, slow_rows), (fast_cycles, _, fast_fields,
                                                       fast_rows) = outcomes
    assert fast_cycles == 1 and slow_cycles == 2
    assert slow_sizes[0] == 1440
    assert slow_fields == fast_fields and slow_fields["state"] == "resolved"
    assert slow_rows == fast_rows and 1600 < len(slow_rows) <= 1680


def test_a_transaction_over_its_budget_halves_the_batch(world):
    _seed(world.db)
    set_id = _event_set(world.db)
    clock = _Clock(LATE)
    read, write = world.pair()
    sizes = []

    def step(statement):
        n = _statement_rows(statement)
        sizes.append(n)
        return 0.06 if n > 60 else 0.001

    _step_clock(write, clock, step)
    w = _worker(world, clock, rw=(read, write), monotonic=clock.monotonic)
    w.run_cycle()
    assert sizes == [240, 120, 60, 60, 60, 60, 15]
    assert os_.get_set(world.db, set_id)["state"] == "complete"


def test_the_cycle_stops_beginning_transactions_after_250ms(world):
    _seed(world.db)
    sets = [_event_set(world.db, EVENT_TS + timedelta(minutes=i), name=f"E{i}")
            for i in range(4)]
    clock = _Clock(LATE)
    read, write = world.pair()
    _step_clock(write, clock, lambda statement: 0.045)
    w = _worker(world, clock, rw=(read, write), monotonic=clock.monotonic)
    report = w.run_cycle()
    assert report.budget_exhausted
    assert len(report.completed) == 2
    by_id = {s["id"]: s for s in _sets(world.db)}
    assert [by_id[i]["state"] for i in sets] == [
        "complete", "complete", "staging", "awaiting"]
    assert _rows_in(world.db, sets[2]) == 255
    clock.t = 0.0
    w.run_cycle()
    assert {s["state"] for s in _sets(world.db)} == {"complete"}


def test_a_cycle_handles_at_most_25_owners(world):
    _seed(world.db)
    for i in range(30):
        _event_set(world.db, EVENT_TS + timedelta(minutes=i), name=f"E{i}")
    w = _worker(world, _Clock(LATE))
    first = w.run_cycle()
    assert first.owners == 25 and len(first.completed) == 25
    second = w.run_cycle()
    assert len(second.completed) == 5
    assert {s["state"] for s in _sets(world.db)} == {"complete"}


def test_spread_candidates_do_not_count_toward_the_owner_limit(world):
    _seed(world.db)
    for i in range(25):
        old = _event_set(world.db, EVENT_TS + timedelta(minutes=i), name=f"E{i}")
        os_.record_event_spread(world.db, old, spread_pips=1.0,
                                spread_source="assumed")
    spreads = [_event_set(world.db, SPREAD_TS + timedelta(minutes=i),
                          name=f"S{i}") for i in range(3)]
    clock = _Clock(SPREAD_TS - timedelta(minutes=2))
    w = _worker(world, clock, quote_fn=lambda pair: _quote())
    report = w.run_cycle()
    assert report.spreads == 3 and report.owners == 25
    assert all(os_.get_set(world.db, i)["spread_source"] == "quote"
               for i in spreads)


# --- 区間が空・閉場だけの shadow -----------------------------------------------------------

def test_a_market_shadow_with_no_range_expires_without_a_set(world):
    shadow = _shadow(world.db, 0, decided=datetime(2026, 11, 6, 21, 58, 10,
                                                   tzinfo=UTC))
    w = _worker(world, _Clock(datetime(2026, 11, 6, 22, 10, tzinfo=UTC)))
    w.run_cycle()
    row = ss.get(world.db, shadow)
    assert (row["state"], row["result_code"]) == ("expired", "no_fill")
    assert _sets(world.db) == []


def test_a_closed_only_market_range_completes_with_no_rows(world):
    _seed(world.db)
    shadow = _shadow(world.db, 0, decided=datetime(2026, 11, 7, 12, 0, 10,
                                                   tzinfo=UTC))
    done = []
    w = _worker(world, _Clock(datetime(2026, 11, 8, 22, 0, tzinfo=UTC)),
                on_complete=done.append)
    w.run_cycle()
    row = ss.get(world.db, shadow)
    assert (row["state"], row["result_code"]) == ("expired", "no_fill")
    [only] = _sets(world.db)
    assert (only["state"], only["expected_rows"]) == ("complete", 0)
    assert _rows_in(world.db, only["id"]) == 0 and len(done) == 1


# --- 後処理 / core lock ----------------------------------------------------------------------

def test_on_complete_runs_once_per_set_and_survives_an_exception(world, caplog):
    _seed(world.db)
    ids = [_event_set(world.db, EVENT_TS + timedelta(minutes=i), name=f"E{i}")
           for i in range(2)]
    seen = []

    def hook(row):
        seen.append(row["id"])
        if len(seen) == 1:
            raise RuntimeError("post-processing failed")

    w = _worker(world, _Clock(LATE), on_complete=hook)
    with caplog.at_level("ERROR"):
        report = w.run_cycle()
    assert sorted(seen) == sorted(ids) == sorted(report.completed)
    assert "on_complete failed" in caplog.text
    w.run_cycle()
    assert len(seen) == 2


def test_the_complete_step_waits_while_another_thread_holds_the_core_lock(world):
    _seed(world.db)
    set_id = _event_set(world.db)
    lock = threading.RLock()
    held, release = threading.Event(), threading.Event()

    def hold():
        with lock:
            held.set()
            release.wait(timeout=30)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert held.wait(timeout=10)
        w = _worker(world, _Clock(LATE), lock=lock)
        report = w.run_cycle()
        assert report.completed == []
        row = os_.get_set(world.db, set_id)
        assert row["state"] == "staging" and _rows_in(world.db, set_id) == 255
    finally:
        release.set()
        thread.join(timeout=30)
    assert not thread.is_alive()
    w.run_cycle()
    assert os_.get_set(world.db, set_id)["state"] == "complete"


def test_the_read_connection_holds_no_snapshot_between_owners(world):
    _seed(world.db)
    _event_set(world.db)
    read, write = world.pair()
    w = _worker(world, _Clock(LATE), rw=(read, write))
    w.run_cycle()
    assert not read.in_transaction and not write.in_transaction


def test_a_stoppable_loop_runs_cycles_until_stopped(world):
    _seed(world.db)
    set_id = _event_set(world.db)
    read, write = world.pair()
    w = worker_mod.EconSliceWorker(
        read_conn=read, write_conn=write, core_lock=threading.RLock(),
        admission=WriterAdmission(), quote_fn=_unavailable,
        pip_size_fn=lambda pair: 0.01, assumed_spread_pips_fn=lambda pair: 1.5,
        now_fn=_Clock(LATE), interval_sec=0.01)
    stop = threading.Event()
    thread = threading.Thread(target=w.run, args=(stop,))
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while (os_.get_set(world.db, set_id)["state"] != "complete"
               and time.monotonic() < deadline):
            time.sleep(0.01)
    finally:
        stop.set()
        thread.join(timeout=20)
    assert not thread.is_alive()
    assert os_.get_set(world.db, set_id)["state"] == "complete"


class _CountingLock:
    def __init__(self):
        self._lock = threading.RLock()
        self.attempts = 0

    def acquire(self, blocking=True):
        self.attempts += 1
        return self._lock.acquire(blocking)

    def release(self):
        self._lock.release()


def test_the_complete_step_does_not_touch_the_core_lock_while_a_write_is_requested(world):
    _seed(world.db)
    set_id = _event_set(world.db)
    lock, admission = _CountingLock(), WriterAdmission()
    w = _worker(world, _Clock(LATE), lock=lock, admission=admission)
    rows = _normalized(world.db, EVENT_TS - timedelta(minutes=15),
                       EVENT_TS + timedelta(hours=4))
    for part in (rows[:240], rows[240:]):
        assert os_.stage_event_rows(world.db, set_id=set_id,
                                    generation=w.generation, rows=part,
                                    assumed_spread_pips=1.0).committed
    admission.request()
    try:
        w.run_cycle()
        assert lock.attempts == 0
        assert os_.get_set(world.db, set_id)["state"] == "staging"
    finally:
        admission.release()
    w.run_cycle()
    assert lock.attempts == 1
    assert os_.get_set(world.db, set_id)["state"] == "complete"
