import contextlib
import dataclasses
import json
import math
import os
import random
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

    def _write(self, op, **kwargs):
        result = super()._write(op, **kwargs)
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


# --- 停止要求・時刻・走査・文の境界 ------------------------------------------------------------

class _HoldWriterAfterGeneration(EconSliceWorker):
    """世代を登録した直後に別接続が書込み lock を握る。"""
    holder = None

    def _write(self, *args, **kwargs):
        result = super()._write(*args, **kwargs)
        if (self.holder is not None and getattr(result, "committed", False)):
            holder, self.holder = self.holder, None
            holder.execute("BEGIN IMMEDIATE")
        return result


def _run_in_thread(w, stop):
    thread = threading.Thread(target=w.run, args=(stop,))
    thread.start()
    return thread


def _stop_and_join(thread, stop):
    stop.set()
    thread.join(timeout=10)
    return not thread.is_alive()


def test_a_stop_request_ends_the_startup_while_another_connection_holds_the_writer(world):
    busy_seen = threading.Event()

    def sleep(seconds):
        busy_seen.set()
        time.sleep(0.005)

    read, write = world.pair()
    w = _worker(world, _Clock(LATE), rw=(read, write), sleep=sleep, start=False)
    holder = world.open(busy=0)
    holder.execute("BEGIN IMMEDIATE")
    stop = threading.Event()
    thread = _run_in_thread(w, stop)
    try:
        assert busy_seen.wait(10)
        assert _stop_and_join(thread, stop)
    finally:
        stop.set()
        thread.join(timeout=10)
        holder.rollback()
    assert w.generation is None


def test_a_stop_request_ends_the_old_generation_reclaim_while_the_writer_is_held(world):
    _seed(world.db)
    set_id = _event_set(world.db)
    os_.record_event_spread(world.db, set_id, spread_pips=1.7,
                            spread_source="quote")
    first = _worker(world, _Clock(LATE), cls=_CrashingWorker)
    first.crash_after = 1
    with pytest.raises(_Crash):
        first.run_cycle()
    assert os_.get_set(world.db, set_id)["state"] == "staging"
    busy_seen = threading.Event()

    def sleep(seconds):
        busy_seen.set()
        time.sleep(0.005)

    holder = world.open(busy=0)
    read, write = world.pair()
    w = _worker(world, _Clock(LATE), rw=(read, write), sleep=sleep, start=False,
                cls=_HoldWriterAfterGeneration)
    w.holder = holder
    stop = threading.Event()
    thread = _run_in_thread(w, stop)
    try:
        assert busy_seen.wait(10)
        assert _stop_and_join(thread, stop)
    finally:
        stop.set()
        thread.join(timeout=10)
        if holder.in_transaction:
            holder.rollback()
    assert w.generation is not None
    assert os_.get_set(world.db, set_id)["state"] == "staging"


def test_a_later_candidate_after_ts_is_recorded_as_assumed_even_if_the_cycle_began_before(world):
    upcoming = [_event_set(world.db, SPREAD_TS, name=f"NFP{i}") for i in range(2)]
    clock = _Clock(SPREAD_TS - timedelta(seconds=1))
    calls = []

    def quote(pair):
        calls.append(pair)
        return _quote()

    read, write = world.pair()

    def trace(statement):
        if statement.startswith("UPDATE ohlcv_slice_sets SET spread_pips"):
            clock.now = SPREAD_TS + timedelta(seconds=1)

    write.set_trace_callback(trace)
    w = _worker(world, clock, rw=(read, write), quote_fn=quote)
    clock.now = SPREAD_TS - timedelta(seconds=1)
    w.run_cycle()
    sources = sorted(os_.get_set(world.db, i)["spread_source"] for i in upcoming)
    assert sources == ["assumed", "quote"]
    assert calls == ["USDJPY"]


def test_a_quote_taken_across_ts_is_dropped_and_assumed_is_recorded(world):
    upcoming = _event_set(world.db, SPREAD_TS, name="NFP")
    clock = _Clock(SPREAD_TS - timedelta(seconds=1))

    def quote(pair):
        clock.now = SPREAD_TS + timedelta(milliseconds=1)
        return _quote(150.0, 150.05)

    w = _worker(world, clock, quote_fn=quote)
    clock.now = SPREAD_TS - timedelta(seconds=1)
    w.run_cycle()
    row = os_.get_set(world.db, upcoming)
    assert (row["spread_source"], row["spread_pips"]) == ("assumed", 1.5)


@pytest.mark.parametrize("outcome", ["none", "raise"])
def test_a_quote_that_fails_after_ts_still_records_assumed(world, outcome):
    upcoming = _event_set(world.db, SPREAD_TS, name="NFP")
    clock = _Clock(SPREAD_TS - timedelta(seconds=1))

    def quote(pair):
        clock.now = SPREAD_TS + timedelta(milliseconds=1)
        if outcome == "raise":
            raise RuntimeError("quote unavailable")
        return None

    w = _worker(world, clock, quote_fn=quote)
    clock.now = SPREAD_TS - timedelta(seconds=1)
    w.run_cycle()
    row = os_.get_set(world.db, upcoming)
    assert (row["spread_source"], row["spread_pips"]) == ("assumed", 1.5)


def test_a_staged_shadow_whose_source_bars_leave_retention_ends_as_data_insufficient(world):
    _seed(world.db)
    shadow = _shadow(world.db, 0)
    w = _worker(world, _Clock(LATE), cls=_CrashingWorker)
    w.crash_after = 1
    with pytest.raises(_Crash):
        w.run_cycle()
    w.crash_after = None
    staged = [s for s in _sets(world.db) if s["shadow_id"] == shadow]
    assert staged and staged[0]["state"] == "staging"
    assert _rows_in(world.db, staged[0]["id"]) > 0
    world.db.execute("DELETE FROM ohlcv_cache WHERE bar_time < ?",
                     (datetime(2026, 11, 2, 13, 0, tzinfo=UTC).isoformat(),))
    world.db.commit()
    w = _worker(world, _Clock(LATE))
    for _ in range(3):
        w.run_cycle()
        if ss.get(world.db, shadow)["state"] != "pending_entry":
            break
    row = ss.get(world.db, shadow)
    assert row["result_code"] == "data_insufficient"
    sets = [s for s in _sets(world.db) if s["shadow_id"] == shadow]
    assert [s["state"] for s in sets] == ["unrecoverable"]
    assert world.db.execute(
        "SELECT COUNT(*) FROM ohlcv_slices").fetchone()[0] == 0


def test_candidates_after_a_key_continue_in_the_same_order(world):
    ids = [_shadow(world.db, i, decided=DECIDED + timedelta(seconds=i))
           for i in range(5)]
    everything = ss.unresolved_candidates(world.db, decided_before=LATE, limit=10)
    assert [r["id"] for r in everything] == ids
    key = (everything[1]["decided_at"], everything[1]["id"])
    rest = ss.unresolved_candidates(world.db, decided_before=LATE, limit=10,
                                    after=key)
    assert [r["id"] for r in rest] == ids[2:]


def test_broken_candidates_filling_the_window_do_not_hide_a_later_mature_one(world):
    _seed(world.db)
    broken = [_shadow(world.db, i) for i in range(worker_mod.SHADOW_CANDIDATE_LIMIT)]
    good = _shadow(world.db, worker_mod.SHADOW_CANDIDATE_LIMIT)
    world.db.execute(
        "UPDATE signals SET payload_json='not json' WHERE id IN "
        "(SELECT signal_id FROM signal_shadow WHERE id IN (%s))"
        % ",".join("?" * len(broken)), broken)
    world.db.commit()
    w = _worker(world, _Clock(LATE))
    for _ in range(3):
        w.run_cycle()
        if ss.get(world.db, good)["state"] != "pending_entry":
            break
    assert ss.get(world.db, good)["state"] == "resolved"
    assert all(ss.get(world.db, i)["state"] == "pending_entry" for i in broken)
    w.run_cycle()
    assert ss.get(world.db, good)["state"] == "resolved"


def _slow_statement_clock(write, clock, trigger):
    issued = []

    def trace(statement):
        issued.append(statement)
        if trigger(statement):
            clock.t += 0.06

    write.set_trace_callback(trace)
    return issued


@pytest.mark.parametrize("trigger", [
    lambda s: s.startswith("BEGIN"),
    lambda s: s.startswith("UPDATE ohlcv_slice_sets SET state="),
])
def test_no_statement_is_issued_after_the_transaction_ran_over_50ms(world, trigger):
    _seed(world.db)
    set_id = _event_set(world.db)
    clock = _Clock(LATE)
    read, write = world.pair()
    w = _worker(world, clock, rw=(read, write), monotonic=clock.monotonic)
    issued = _slow_statement_clock(write, clock, trigger)
    report = w.run_cycle()
    assert not any(s.startswith("INSERT INTO ohlcv_slices") for s in issued)
    assert not any(s.startswith("SELECT range_start") for s in issued)
    assert _rows_in(world.db, set_id) == 0
    assert os_.get_set(world.db, set_id)["state"] == "awaiting"
    assert report.completed == []


def test_the_complete_step_issues_nothing_after_the_transaction_ran_over_50ms(world):
    _seed(world.db)
    shadow = _shadow(world.db, 0)
    clock = _Clock(LATE)
    read, write = world.pair()
    w = _worker(world, clock, rw=(read, write), monotonic=clock.monotonic)
    issued = _slow_statement_clock(
        write, clock, lambda s: "generation=NULL" in s and s.startswith("UPDATE"))
    w.run_cycle()
    assert not any(s.startswith("UPDATE signal_shadow") for s in issued)
    assert ss.get(world.db, shadow)["state"] == "pending_entry"
    assert [s["state"] for s in _sets(world.db)] == ["staging"]


def test_a_store_operation_gives_up_between_statements_when_the_checkpoint_raises(world):
    from agentic_fx.store.short_tx import OverBudget
    set_id = _event_set(world.db)
    gen = os_.new_generation(world.db, now=LATE)
    seen = []

    def checkpoint():
        seen.append(1)
        if len(seen) == 1:
            raise OverBudget

    start = EVENT_TS - timedelta(minutes=15)
    rows = [SliceRow(ts, 150.0, 150.1, 149.9, 150.0, 1.0, "mt5-live", "observed")
            for ts in open_minutes(start, start + timedelta(minutes=3))]
    issued = []
    world.db.set_trace_callback(issued.append)
    result = os_.stage_event_rows(world.db, set_id=set_id, generation=gen,
                                  rows=rows, assumed_spread_pips=1.0,
                                  checkpoint=checkpoint)
    world.db.set_trace_callback(None)
    assert result.status == "over_budget"
    assert not any(s.startswith("INSERT INTO ohlcv_slices") for s in issued)
    assert _rows_in(world.db, set_id) == 0
    assert os_.get_set(world.db, set_id)["state"] == "awaiting"


# --- 競合 ------------------------------------------------------------------------------------

class _RivalFirst(EconSliceWorker):
    """候補を取得した後、自分の処理の前に相手の worker に cycle を回させる。"""
    rival = None

    def _shadow_owner(self, row, now):
        if self.rival is not None:
            rival, self.rival = self.rival, None
            rival.run_cycle()
        super()._shadow_owner(row, now)


def test_a_worker_holding_a_candidate_another_worker_finished_ends_nothing_twice(world):
    _seed(world.db)
    shadow = _shadow(world.db, 0)
    done = []
    clock = _Clock(LATE)
    rival = _worker(world, clock, on_complete=done.append)
    mine = _worker(world, clock, cls=_RivalFirst, on_complete=done.append)
    mine.rival = rival
    report = mine.run_cycle()
    assert ss.get(world.db, shadow)["state"] == "resolved"
    assert report.completed == []
    assert len(done) == 1
    assert [s["state"] for s in _sets(world.db)] == ["complete"]
    first = _shadow_fields(world.db, shadow)
    mine.run_cycle()
    rival.run_cycle()
    assert _shadow_fields(world.db, shadow) == first
    assert len(done) == 1


def test_the_shadow_ended_elsewhere_after_staging_is_not_ended_again(world):
    _seed(world.db)
    shadow = _shadow(world.db, 0)
    other = world.open(busy=5000)

    class _Racing(EconSliceWorker):
        def _before_readback(self, set_id):
            row = ss.get(other, shadow)
            assert ss.transition(
                other, shadow, expected_state=row["state"],
                expected_updated_at=row["updated_at"],
                outcome=ss.ShadowOutcome.no_fill(), now=LATE)

    done = []
    w = _worker(world, _Clock(LATE), cls=_Racing, on_complete=done.append)
    report = w.run_cycle()
    assert report.completed == [] and done == []
    row = ss.get(world.db, shadow)
    assert (row["state"], row["result_code"]) == ("expired", "no_fill")
    assert [s["state"] for s in _sets(world.db)] == ["staging"]


class _UpdateBetweenOwners(EconSliceWorker):
    other = None
    owners = 0

    def _event_owner(self, row, now):
        super()._event_owner(row, now)
        self.owners += 1
        if self.owners == 1:
            self.other.execute(
                "UPDATE ohlcv_cache SET open=155.0, high=155.2, low=154.8, "
                "close=155.0 WHERE symbol='USDJPY' AND interval='1m' "
                "AND bar_time=?", ("2026-11-03T07:00:00+00:00",))
            self.other.commit()


def test_the_next_owner_reads_what_another_connection_wrote_between_owners(world):
    _seed(world.db)
    first = _event_set(world.db)
    second = _event_set(world.db, datetime(2026, 11, 3, 6, 0, tzinfo=UTC),
                        name="NFP")
    w = _worker(world, _Clock(datetime(2026, 11, 4, 12, 0, tzinfo=UTC)),
                cls=_UpdateBetweenOwners)
    w.other = world.open(busy=5000)
    w.run_cycle()
    assert os_.get_set(world.db, first)["state"] == "complete"
    assert os_.get_set(world.db, second)["state"] == "complete"
    read = world.open(read_only=True)
    rows = {r.bar_time: r for r in os_.complete_rows(read, second)}
    assert rows[datetime(2026, 11, 3, 7, 0, tzinfo=UTC)].close == 155.0


# --- 保持・再試行・巡回・読込みの量 ------------------------------------------------------------

def _count_normalize(monkeypatch, clock=None, cost=0.0, fail_before=None):
    calls = []
    real = worker_mod.normalize_1m

    def counting(conn, pair, start, end):
        calls.append((start, end))
        if fail_before is not None and start < fail_before:
            raise RuntimeError("normalize failed")
        if clock is not None:
            clock.t += cost
        return real(conn, pair, start, end)

    monkeypatch.setattr(worker_mod, "normalize_1m", counting)
    return calls


def test_an_event_whose_read_uses_the_whole_cycle_budget_still_completes(world, monkeypatch):
    _seed(world.db)
    set_id = _event_set(world.db)
    clock = _Clock(LATE)
    calls = _count_normalize(monkeypatch, clock, cost=0.3)
    w = _worker(world, clock, monotonic=clock.monotonic)
    for _ in range(5):
        w.run_cycle()
        if os_.get_set(world.db, set_id)["state"] == "complete":
            break
    assert os_.get_set(world.db, set_id)["state"] == "complete"
    assert len(calls) == 1


def test_a_shadow_whose_read_uses_the_whole_cycle_budget_still_completes(world, monkeypatch):
    _seed(world.db)
    shadow = _shadow(world.db, 0)
    clock = _Clock(LATE)
    calls = _count_normalize(monkeypatch, clock, cost=0.3)
    w = _worker(world, clock, monotonic=clock.monotonic)
    for _ in range(5):
        w.run_cycle()
        if ss.get(world.db, shadow)["state"] != "pending_entry":
            break
    assert ss.get(world.db, shadow)["state"] == "resolved"
    assert len(calls) == 1


def test_a_limit_shadow_waiting_for_its_final_point_is_not_read_again(world, monkeypatch):
    fill = (150.0, 150.05, 149.8, 150.0)
    late_fill = datetime(2026, 11, 2, 22, 1, tzinfo=UTC)
    decided = datetime(2026, 11, 2, 18, 5, tzinfo=UTC)
    _seed(world.db, {late_fill: fill})
    shadow = _shadow(world.db, 0, entry_type="limit", limit_price=149.9,
                     decided=decided)
    waiting = datetime(2026, 11, 3, 22, 30, tzinfo=UTC)
    clock = _Clock(waiting)
    calls = _count_normalize(monkeypatch)
    w = _worker(world, clock)
    for minutes in (0, 1, 2):
        clock.now = waiting + timedelta(minutes=minutes)
        w.run_cycle()
        assert ss.get(world.db, shadow)["state"] == "pending_entry"
    assert len(calls) == 1
    clock.now = datetime(2026, 11, 4, 22, 0, tzinfo=UTC)
    w.run_cycle()
    assert ss.get(world.db, shadow)["state"] == "resolved"
    assert len(calls) == 2


class _FailStartOnce(EconSliceWorker):
    failures = 1

    def _write(self, *args, **kwargs):
        if self.failures:
            self.failures -= 1
            raise sqlite3.OperationalError("disk I/O error")
        return super()._write(*args, **kwargs)


def test_a_non_busy_error_at_startup_is_logged_and_retried(world, caplog):
    _seed(world.db)
    set_id = _event_set(world.db)
    read, write = world.pair()
    w = _FailStartOnce(
        read_conn=read, write_conn=write, core_lock=threading.RLock(),
        admission=WriterAdmission(), quote_fn=_unavailable,
        pip_size_fn=lambda pair: 0.01, assumed_spread_pips_fn=lambda pair: 1.5,
        now_fn=_Clock(LATE), interval_sec=0.01)
    stop = threading.Event()
    thread = _run_in_thread(w, stop)
    try:
        deadline = time.monotonic() + 20
        while (os_.get_set(world.db, set_id)["state"] != "complete"
               and time.monotonic() < deadline):
            time.sleep(0.01)
    finally:
        assert _stop_and_join(thread, stop)
    assert w.failures == 0
    assert os_.get_set(world.db, set_id)["state"] == "complete"
    assert "econ slice worker start failed" in caplog.text


def test_one_quote_is_taken_per_pair_in_a_cycle(world):
    ids = [_event_set(world.db, SPREAD_TS, name=f"NFP{i}") for i in range(3)]
    clock = _Clock(SPREAD_TS - timedelta(minutes=4))
    calls = []

    def quote(pair):
        calls.append(pair)
        return _quote()

    w = _worker(world, clock, quote_fn=quote)
    clock.now = SPREAD_TS - timedelta(minutes=4)
    report = w.run_cycle()
    assert calls == ["USDJPY"] and report.spreads == 3
    rows = [os_.get_set(world.db, i) for i in ids]
    assert {r["spread_source"] for r in rows} == {"quote"}
    assert [r["spread_pips"] for r in rows] == [pytest.approx(2.0)] * 3


def test_a_failed_quote_is_not_retried_within_the_cycle(world):
    ids = [_event_set(world.db, SPREAD_TS, name=f"NFP{i}") for i in range(3)]
    clock = _Clock(SPREAD_TS - timedelta(minutes=4))
    calls = []

    def quote(pair):
        calls.append(pair)
        raise RuntimeError("no quote")

    w = _worker(world, clock, quote_fn=quote)
    clock.now = SPREAD_TS - timedelta(minutes=4)
    report = w.run_cycle()
    assert calls == ["USDJPY"] and report.spreads == 0
    assert all(os_.get_set(world.db, i)["spread_source"] is None for i in ids)
    w.run_cycle()
    assert calls == ["USDJPY", "USDJPY"]


def test_events_that_cannot_complete_do_not_hold_back_a_later_mature_one(world, monkeypatch):
    _seed(world.db)
    ids = [_event_set(world.db, EVENT_TS + timedelta(minutes=i), name=f"E{i}")
           for i in range(worker_mod.MAX_OWNERS_PER_CYCLE)]
    good = _event_set(world.db, EVENT_TS + timedelta(minutes=30), name="late")
    boundary = _at(os_.get_set(world.db, good)["range_start"])
    _count_normalize(monkeypatch, fail_before=boundary)
    w = _worker(world, _Clock(LATE))
    for _ in range(4):
        w.run_cycle()
        if os_.get_set(world.db, good)["state"] == "complete":
            break
    assert os_.get_set(world.db, good)["state"] == "complete"
    assert all(os_.get_set(world.db, i)["state"] == "awaiting" for i in ids)


def test_staging_a_long_range_does_not_reread_every_row_per_batch(world, monkeypatch):
    fill = (150.0, 150.05, 149.8, 150.0)
    late_fill = datetime(2026, 11, 2, 22, 1, tzinfo=UTC)
    decided = datetime(2026, 11, 2, 18, 5, tzinfo=UTC)
    _seed(world.db, {late_fill: fill})
    shadow = _shadow(world.db, 0, entry_type="limit", limit_price=149.9,
                     decided=decided)
    reads, writes = [], []
    real_read, real_write = os_.staging_rows, os_.stage_shadow_rows
    monkeypatch.setattr(os_, "staging_rows",
                        lambda *a, **k: reads.append(1) or real_read(*a, **k))
    monkeypatch.setattr(os_, "stage_shadow_rows",
                        lambda *a, **k: writes.append(1) or real_write(*a, **k))
    w = _worker(world, _Clock(datetime(2026, 11, 4, 22, 0, tzinfo=UTC)))
    w.run_cycle()
    assert ss.get(world.db, shadow)["state"] == "resolved"
    assert len(writes) >= 6
    assert len(reads) <= 2


def test_kept_state_is_bounded_and_dropped_when_an_owner_ends(world):
    _seed(world.db)
    memo = worker_mod._Memo(worker_mod.OWNER_MEMO_MAX)
    for i in range(worker_mod.OWNER_MEMO_MAX + 5):
        memo.put(i, i)
    assert len(memo) == worker_mod.OWNER_MEMO_MAX
    assert memo.get(0) is None and memo.get(worker_mod.OWNER_MEMO_MAX + 4) is not None
    set_id = _event_set(world.db)
    w = _worker(world, _Clock(LATE))
    w.run_cycle()
    assert os_.get_set(world.db, set_id)["state"] == "complete"
    assert len(w._event_norms) == 0


# --- 保持期間を過ぎた set の削除 ---------------------------------------------------------

def _completed_world(world):
    """event set と shadow を 1 つずつ完備まで進めた状態。"""
    _seed(world.db)
    event_set = _event_set(world.db)
    shadow = _shadow(world.db, 0)
    w = _worker(world, _Clock(LATE))
    w.run_cycle()
    assert os_.get_set(world.db, event_set)["state"] == "complete"
    assert ss.get(world.db, shadow)["state"] == "resolved"
    return event_set, shadow


def _retention_end(db, set_id):
    created = _at(os_.get_set(db, set_id)["created_at"])
    return created + timedelta(days=os_.RETENTION_DAYS)


def test_a_cycle_keeps_every_set_younger_than_the_retention_days(world):
    event_set, shadow = _completed_world(world)
    before = [_rows_in(world.db, s["id"]) for s in _sets(world.db)]
    assert before and all(before)
    clock = _Clock(_retention_end(world.db, event_set))
    w = _worker(world, clock)
    report = w.run_cycle()
    assert report.purged == 0
    assert [_rows_in(world.db, s["id"]) for s in _sets(world.db)] == before
    assert all(s["purged_at"] is None for s in _sets(world.db))


def test_a_cycle_deletes_only_the_rows_of_a_set_past_the_retention_days(world):
    event_set, shadow = _completed_world(world)
    sets_before = {s["id"]: s for s in _sets(world.db)}
    shadow_before = ss.get(world.db, shadow)
    world.db.execute(
        "INSERT INTO econ_reaction_values (set_id,window_min,status,range_pips,"
        "close_change_pips,abs_close_change_pips,net_abs_close_change_pips,"
        "rule_version,computed_at) VALUES (?,15,'resolved',1,1,1,0,1,?)",
        (event_set, LATE.isoformat()))
    world.db.execute(
        "INSERT INTO econ_reaction_keys (event_id,currency,set_id,decided_at) "
        "SELECT event_id,'USD',id,? FROM ohlcv_slice_sets WHERE id=?",
        (LATE.isoformat(), event_set))
    world.db.commit()
    values_before = world.db.execute(
        "SELECT * FROM econ_reaction_values ORDER BY 1,2").fetchall()
    keys_before = world.db.execute(
        "SELECT * FROM econ_reaction_keys ORDER BY 1,2").fetchall()
    assert values_before and keys_before
    clock = _Clock(_retention_end(world.db, event_set) + timedelta(seconds=1))
    w = _worker(world, clock)
    report = w.run_cycle()
    assert report.purged == 255
    row = os_.get_set(world.db, event_set)
    assert (row["state"], row["purged_at"]) == (
        "complete", clock.now.isoformat())
    assert _rows_in(world.db, event_set) == 0
    for set_id, before in sets_before.items():
        after = os_.get_set(world.db, set_id)
        assert after["state"] == before["state"]
        assert after["expected_rows"] == before["expected_rows"]
    assert ss.get(world.db, shadow) == shadow_before
    assert world.db.execute(
        "SELECT * FROM econ_reaction_values ORDER BY 1,2").fetchall() == values_before
    assert world.db.execute(
        "SELECT * FROM econ_reaction_keys ORDER BY 1,2").fetchall() == keys_before
    assert w.run_cycle().purged == 0


def test_purge_does_not_take_the_core_lock(world):
    event_set, shadow = _completed_world(world)
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
        clock = _Clock(_retention_end(world.db, event_set)
                       + timedelta(seconds=1))
        w = _worker(world, clock, lock=lock)
        w.run_cycle()
        assert _rows_in(world.db, event_set) == 0
        assert os_.get_set(world.db, event_set)["purged_at"] is not None
    finally:
        release.set()
        thread.join(timeout=30)
    assert not thread.is_alive()


# --- 停止要求と purge の巡回 ------------------------------------------------------------

def test_a_stop_request_during_a_quote_fetch_ends_the_cycle_before_the_next_quote(world):
    first = _event_set(world.db, SPREAD_TS, name="NFP")
    second = _event_set(world.db, SPREAD_TS, pair="EURUSD", name="NFP2")
    clock = _Clock(SPREAD_TS - timedelta(minutes=4))
    stop = threading.Event()
    calls = []

    def quote(pair):
        calls.append(pair)
        stop.set()
        return _quote()

    w = _worker(world, clock, quote_fn=quote)
    w.run_cycle(stop)
    assert len(calls) == 1
    sources = {i: os_.get_set(world.db, i)["spread_source"] for i in (first, second)}
    assert sorted(v for v in sources.values() if v) == ["quote"]


def test_a_cycle_started_with_a_stop_request_already_set_fetches_no_quote(world):
    _event_set(world.db, SPREAD_TS, name="NFP")
    clock = _Clock(SPREAD_TS - timedelta(minutes=4))
    stop = threading.Event()
    stop.set()
    calls = []
    w = _worker(world, clock, quote_fn=lambda pair: calls.append(pair))
    w.run_cycle(stop)
    assert calls == []


def _two_expired_sets(world):
    _seed(world.db)
    first = _event_set(world.db, EVENT_TS, name="A")
    second = _event_set(world.db, EVENT_TS + timedelta(minutes=1), name="B")
    _worker(world, _Clock(LATE)).run_cycle()
    assert {os_.get_set(world.db, i)["state"] for i in (first, second)} == {"complete"}
    return first, second


def test_purge_advances_every_cycle_even_when_owners_use_the_whole_budget(world):
    _seed(world.db)
    old = _event_set(world.db, EVENT_TS, name="OLD")
    _worker(world, _Clock(LATE)).run_cycle()
    assert os_.get_set(world.db, old)["state"] == "complete"
    for i in range(4):
        _event_set(world.db, EVENT_TS + timedelta(minutes=i + 1), name=f"E{i}")
    clock = _Clock(_retention_end(world.db, old) + timedelta(seconds=1))
    read, write = world.pair()
    _step_clock(write, clock, lambda statement: 0.045)
    w = _worker(world, clock, rw=(read, write), monotonic=clock.monotonic)
    first = w.run_cycle()
    assert first.budget_exhausted and first.purged == 240
    assert os_.get_set(world.db, old)["purged_at"] is None
    clock.t = 0.0
    second = w.run_cycle()
    assert second.purged == 15
    assert os_.get_set(world.db, old)["purged_at"] is not None


def test_a_set_that_keeps_going_over_budget_does_not_hold_back_later_sets(world, monkeypatch):
    first, second = _two_expired_sets(world)
    clock = _Clock(_retention_end(world.db, second) + timedelta(seconds=1))
    real = os_.purge_step
    calls = []

    def purge_step(conn, *, set_id, **kwargs):
        calls.append((set_id, kwargs["max_rows"]))
        if set_id == first:
            return worker_mod.TxResult(status="over_budget")
        return real(conn, set_id=set_id, **kwargs)

    monkeypatch.setattr(os_, "purge_step", purge_step)
    w = _worker(world, clock)
    w.run_cycle()
    assert calls[:3] == [(first, 240), (second, 240), (second, 240)]
    assert os_.get_set(world.db, second)["purged_at"] is not None
    assert os_.get_set(world.db, first)["purged_at"] is None
    calls.clear()
    w.run_cycle()
    assert calls and calls[0] == (first, 240)


# --- 実ディスクの bench (writer 優先と容量) ------------------------------------------
#
# 本番の data/ と同じ filesystem の実 SQLite file で測る。`AFX_BENCH_DATA_DIR` に本番の
# data/ の場所を渡す (stat だけで、中身には触れない)。通常 suite では bench marker で除外される。

DISK_OUT = Path(__file__).resolve().parents[2] / "tmp" / "econ-window-acceptance" / "disk"
FIXED_CAPACITY_ROWS = 64_693_085
JOB_LIMIT_SEC = 4 * 3600
JOB_LIMIT_BYTES = 4 * 1024 ** 3
# first-statement がこれを超えたら待ちが起きたと数える。worker が BEGIN を出さなかったことは
# 別に、BEGIN を出した時刻とフラグの立っていた区間の突き合わせで決定的に確かめる。
WAIT_EPSILON_SEC = 0.005
BENCH_FILL_AT = datetime(2026, 11, 2, 22, 0, tzinfo=UTC)
BENCH_DECIDED = datetime(2026, 11, 2, 18, 0, 30, tzinfo=UTC)
BENCH_NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
BENCH_FILL_BAR = (150.0, 150.05, 149.8, 150.0)


def _env_int(name, default):
    raw = os.environ.get(name)
    return default if raw in (None, "") else int(raw)


def _dist(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p50": None, "p99": None, "max": None}
    n = len(ordered)
    return {"count": n,
            "p50": ordered[max(0, math.ceil(0.50 * n) - 1)],
            "p99": ordered[max(0, math.ceil(0.99 * n) - 1)],
            "max": ordered[-1]}


def _dir_bytes(path):
    return sum(e.stat().st_size for e in os.scandir(path) if e.is_file())


class _Peak:
    def __init__(self, work):
        self.work = work
        self.bytes = 0

    def sample(self):
        total = _dir_bytes(self.work)
        if DISK_OUT.is_dir():
            total += sum(e.stat().st_size for e in os.scandir(DISK_OUT)
                         if e.is_file())
        self.bytes = max(self.bytes, total)
        return self.bytes


@pytest.fixture(scope="module")
def disk_job(tmp_path_factory):
    raw = os.environ.get("AFX_BENCH_DATA_DIR")
    if not raw:
        pytest.fail("AFX_BENCH_DATA_DIR is not set (the production data/ location)")
    data = Path(raw)
    if not data.is_dir():
        pytest.fail(f"AFX_BENCH_DATA_DIR does not exist: {data}")
    base = tmp_path_factory.getbasetemp()
    if os.stat(data).st_dev != os.stat(base).st_dev:
        pytest.fail(f"basetemp {base} is not on the same filesystem as {data}")
    DISK_OUT.mkdir(parents=True, exist_ok=True)
    work = tmp_path_factory.mktemp("disk")
    return {"work": work, "started": time.monotonic(), "peak": _Peak(work),
            "data_dev": os.stat(data).st_dev}


def _write_json(name, payload):
    target = DISK_OUT / name
    target.write_text(json.dumps(payload, indent=2, sort_keys=True))


class _ProbeConn:
    """worker の write 接続を包み、transaction ごとの BEGIN から COMMIT 直前までの経過・
    COMMIT の所要・挿入行数を記録する。他の属性はそのまま通す。"""

    def __init__(self, conn):
        self._conn = conn
        self.begins = []
        self.busy = []
        self.txs = []
        self._open = None

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args):
        text = sql.lstrip().upper()
        if text.startswith("BEGIN IMMEDIATE"):
            issued = time.perf_counter()
            self.begins.append(issued)
            try:
                cur = self._conn.execute(sql, *args)
            except sqlite3.OperationalError:
                self.busy.append(issued)
                raise
            self._open = {"begin": issued, "rows": 0}
            return cur
        cur = self._conn.execute(sql, *args)
        if self._open is not None and text.startswith("INSERT INTO OHLCV_SLICES ("):
            self._open["rows"] += cur.rowcount
        return cur

    def commit(self):
        before = time.perf_counter()
        self._conn.commit()
        after = time.perf_counter()
        if self._open is not None:
            self.txs.append({"outcome": "commit", "begin": self._open["begin"],
                             "pre": before - self._open["begin"],
                             "commit": after - before, "end": after,
                             "rows": self._open["rows"]})
            self._open = None

    def rollback(self):
        before = time.perf_counter()
        self._conn.rollback()
        if self._open is not None:
            self.txs.append({"outcome": "rollback", "begin": self._open["begin"],
                             "pre": before - self._open["begin"], "commit": 0.0,
                             "end": time.perf_counter(), "rows": self._open["rows"]})
            self._open = None


class _TimedLock:
    """core lock の代わり。worker の保持時間 (スレッド名ごと) を記録する。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._since = {}
        self.holds = []

    def acquire(self, blocking=True, timeout=-1):
        ok = self._lock.acquire(blocking, timeout)
        if ok:
            self._since[threading.get_ident()] = time.perf_counter()
        return ok

    def release(self):
        began = self._since.pop(threading.get_ident(), None)
        if began is not None:
            self.holds.append((threading.current_thread().name,
                               time.perf_counter() - began))
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def _bench_world(work, name, owners):
    world = _World(work / f"{name}.db")
    _seed(world.db, {BENCH_FILL_AT: BENCH_FILL_BAR},
          start=datetime(2026, 11, 2, 17, 0, tzinfo=UTC),
          end=datetime(2026, 11, 4, 0, 0, tzinfo=UTC))
    for n in range(owners):
        _shadow(world.db, n, decided=BENCH_DECIDED, entry_type="limit",
                limit_price=149.9)
    return world


def _long_writer(conn, path, *, holds, hold_sec, gap_sec, out, stop):
    """フラグを持たない長い writer (BEGIN IMMEDIATE の中で file I/O をして保持する)。"""
    chunk = os.urandom(1 << 16)
    with open(path, "wb") as fh:
        for _ in range(holds):
            if stop.is_set():
                break
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                out["busy"] += 1
                continue
            began = time.perf_counter()
            conn.execute(
                "INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("bench_long_writer", "held", "t"))
            while time.perf_counter() - began < hold_sec:
                fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
                time.sleep(0.01)
            conn.commit()
            out["holds"].append((began, time.perf_counter()))
            time.sleep(gap_sec)
    out["done"] = True


def _scheduler_txn(conn, label, serial, record):
    started = time.perf_counter()
    try:
        conn.execute(
            "INSERT INTO account_snapshots (ts,balance,equity,hwm,cashflow,source) "
            "VALUES (?,?,?,?,?,?)",
            (f"2026-11-05T00:00:{serial % 60:02d}+00:00", 1e6, 1e6, 1e6, 0.0, "paper"))
        first = time.perf_counter() - started
        conn.execute(
            "INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"bench_{label}", str(serial), "t"))
        conn.commit()
        record.append({"label": label, "start": started, "first": first,
                       "busy": False})
    except sqlite3.OperationalError:
        if conn.in_transaction:
            conn.rollback()
        record.append({"label": label, "start": started,
                       "first": time.perf_counter() - started, "busy": True})


def _signal_txn(conn, serial, record):
    started = time.perf_counter()
    try:
        conn.execute(
            "INSERT INTO signals (plugin,content_hash,pair,timeframe,bar_ts,kind,"
            "payload_json,status,created_at) VALUES ('bench',?,'USDJPY','15m',?,"
            "'strategy','{}','pending','2026-11-05T00:00:00+00:00')",
            (f"m{serial}", f"2026-11-05T00:00:{serial % 60:02d}+00:00"))
        first = time.perf_counter() - started
        conn.execute("UPDATE signals SET requeue_count=requeue_count+1 "
                     "WHERE plugin='bench' AND content_hash=?", (f"m{serial}",))
        conn.commit()
        record.append({"label": "signal", "start": started, "first": first,
                       "busy": False})
    except sqlite3.OperationalError:
        if conn.in_transaction:
            conn.rollback()
        record.append({"label": "signal", "start": started,
                       "first": time.perf_counter() - started, "busy": True})


def _run_round(work, label, *, with_worker, long_writer, signal_writes, owners,
               seed, duration=None):
    world = _bench_world(work, label, owners)
    core = world.open()
    signal = world.open()
    holder_conn = world.open()
    admission = WriterAdmission()
    lock = _TimedLock()
    rng = random.Random(seed)
    stop = threading.Event()
    ticks = []
    cycles = []
    holder = {"holds": [], "busy": 0, "done": not long_writer}
    probe = worker = None
    if with_worker:
        read, write = world.pair()
        probe = _ProbeConn(write)
        worker = _worker(world, _Clock(BENCH_NOW), rw=(read, probe), lock=lock,
                         admission=admission, monotonic=time.monotonic,
                         sleep=time.sleep)
    worker_done = threading.Event()
    if not with_worker:
        worker_done.set()
    errors = []

    def worker_main():
        try:
            finished = 0
            while not stop.is_set():
                report = worker.run_cycle(stop)
                cycles.append(report)
                finished += len(report.completed)
                if finished >= owners:
                    break
                stop.wait(0.002)
        except BaseException as exc:
            errors.append(exc)
        finally:
            worker_done.set()

    def tick_once(serial, aim):
        began = time.perf_counter()
        record = []
        with contextlib.ExitStack() as stack:
            before = time.perf_counter()
            lock.acquire()
            lock_wait = time.perf_counter() - before
            stack.callback(lock.release)
            before = time.perf_counter()
            stack.enter_context(admission.scheduler_write())
            register = time.perf_counter() - before
            up = time.perf_counter()
            for k in range(3):
                _scheduler_txn(core, f"core{k}", serial * 4 + k, record)
            if signal_writes:
                time.sleep(0.003)
                _signal_txn(signal, serial, record)
            down = time.perf_counter()
        ticks.append({"start": began, "duration": time.perf_counter() - began,
                      "lock_wait": lock_wait, "register": register, "up": up,
                      "down": down, "aimed": aim, "txns": record})

    def scheduler_main():
        serial = 0
        try:
            while not stop.is_set():
                aim = with_worker and rng.random() < 0.5
                if aim:
                    spin = time.perf_counter() + 0.05
                    while (time.perf_counter() < spin and not stop.is_set()
                           and admission.stats().beginning == 0):
                        time.sleep(0.0002)
                else:
                    time.sleep(rng.uniform(0.002, 0.03))
                serial += 1
                tick_once(serial, aim)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=scheduler_main, name="bench-scheduler")]
    if with_worker:
        threads.append(threading.Thread(target=worker_main, name="bench-worker"))
    if long_writer:
        threads.append(threading.Thread(
            target=_long_writer, name="bench-long-writer",
            kwargs=dict(conn=holder_conn, path=work / f"{label}.hold", holds=3,
                        hold_sec=2.0, gap_sec=0.3, out=holder, stop=stop)))
    started = time.perf_counter()
    for t in threads:
        t.start()
    try:
        deadline = started + 600
        while time.perf_counter() < deadline:
            if errors:
                break
            if worker_done.is_set() and holder["done"] and (
                    with_worker or duration is None
                    or time.perf_counter() - started >= duration):
                break
            time.sleep(0.01)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=120)
    elapsed = time.perf_counter() - started
    assert not errors, errors
    assert not any(t.is_alive() for t in threads)
    result = {"elapsed": elapsed, "ticks": ticks, "cycles": cycles,
              "holder": holder, "probe": probe, "lock": lock,
              "admission": admission.stats(),
              "recorded_commits": worker.commit_seconds if worker else ()}
    if with_worker:
        check = connect_readonly(world.path)
        try:
            sets = check.execute(
                "SELECT s.state, (SELECT COUNT(*) FROM ohlcv_slices x "
                "WHERE x.set_id=s.id) FROM ohlcv_slice_sets s").fetchall()
        finally:
            check.close()
        result["sets"] = [(r[0], r[1]) for r in sets]
    world.close()
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(f"{world.path}{suffix}")
    with contextlib.suppress(FileNotFoundError):
        os.unlink(work / f"{label}.hold")
    return result


def _round_waits(result):
    """tick 内の各 write transaction の待ち。長い writer の保持と重なった時間は引く。"""
    holds = result["holder"]["holds"]
    waits = []
    for tick in result["ticks"]:
        per_tick = []
        for tx in tick["txns"]:
            end = tx["start"] + tx["first"]
            overlap = sum(max(0.0, min(end, h1) - max(tx["start"], h0))
                          for h0, h1 in holds)
            per_tick.append({"raw": tx["first"],
                             "residual": max(0.0, tx["first"] - overlap),
                             "busy": tx["busy"]})
        waits.append(per_tick)
    return waits


def _summarize(rounds):
    waits = [w for r in rounds for tick in _round_waits(r) for w in tick]
    ticks = [t for r in rounds for t in r["ticks"]]
    per_tick_waits = [sum(1 for w in tick if w["residual"] > WAIT_EPSILON_SEC)
                      for r in rounds for tick in _round_waits(r)]
    out = {
        "rounds": len(rounds),
        "ticks": len(ticks),
        "aimed_ticks": sum(1 for t in ticks if t["aimed"]),
        "write_transactions": len(waits),
        "sqlite_busy": sum(1 for w in waits if w["busy"]),
        "wait_residual_sec": _dist([w["residual"] for w in waits]),
        "wait_raw_sec": _dist([w["raw"] for w in waits]),
        "waits_per_tick_max": max(per_tick_waits, default=0),
        "core_lock_wait_sec": _dist([t["lock_wait"] for t in ticks]),
        "register_sec": _dist([t["register"] for t in ticks]),
        "ticks_that_drained_a_lease": sum(1 for t in ticks if t["register"] > 0.001),
        "tick_duration_sec": _dist([t["duration"] for t in ticks]),
        "elapsed_sec": [r["elapsed"] for r in rounds],
    }
    return out


def _worker_summary(rounds, owners):
    txs = [tx for r in rounds for tx in r["probe"].txs]
    commits = [tx for tx in txs if tx["outcome"] == "commit"]
    cycles = [c for r in rounds for c in r["cycles"]]
    holds = [h for r in rounds for name, h in r["lock"].holds
             if name == "bench-worker"]
    flag_violations = 0
    held_commits = 0
    busy_during_hold = 0
    for r in rounds:
        ups = [(t["up"], t["down"]) for t in r["ticks"]]
        for issued in r["probe"].begins:
            if any(a < issued < b for a, b in ups):
                flag_violations += 1
        for tx in commits_of(r):
            if any(h0 < tx["end"] < h1 for h0, h1 in r["holder"]["holds"]):
                held_commits += 1
        busy_during_hold += sum(
            1 for issued in r["probe"].busy
            if any(h0 < issued < h1 for h0, h1 in r["holder"]["holds"]))
    stats = [r["admission"] for r in rounds]
    return {
        "transactions": len(txs),
        "committed": len(commits),
        "rolled_back": len(txs) - len(commits),
        "rows_per_transaction_max": max((tx["rows"] for tx in commits), default=0),
        "rows_staged": sum(tx["rows"] for tx in commits),
        "before_commit_sec": _dist([tx["pre"] for tx in commits]),
        "rolled_back_before_commit_sec": _dist(
            [tx["pre"] for tx in txs if tx["outcome"] == "rollback"]),
        "commit_sec": _dist([tx["commit"] for tx in commits]),
        "recorded_commit_sec": _dist([x for r in rounds
                                      for x in r["recorded_commits"]]),
        "core_lock_hold_sec": _dist(holds),
        "cycles": len(cycles),
        "cycle_owners_max": max((c.owners for c in cycles), default=0),
        "cycle_spent_sec": _dist([c.spent_sec for c in cycles]),
        "cycle_spent_bound_sec": 0.25 + 0.05 + max(
            (tx["pre"] + tx["commit"] for tx in txs), default=0.0),
        "begin_inside_flag_interval": flag_violations,
        "commits_inside_long_writer_hold": held_commits,
        "busy_begins": sum(len(r["probe"].busy) for r in rounds),
        "busy_begins_during_long_writer": busy_during_hold,
        "yielded": sum(s.yielded for s in stats),
        "admitted": sum(s.admitted for s in stats),
        "drain_overruns": sum(s.wait_overruns for s in stats),
        "drain_wait_max_sec": max((s.max_wait_sec or 0.0 for s in stats),
                                  default=0.0),
        "owners_complete": [sum(1 for st, _ in r["sets"] if st == "complete")
                            for r in rounds],
        "rows_per_owner": sorted({n for r in rounds for st, n in r["sets"]}),
    }


def commits_of(result):
    return [tx for tx in result["probe"].txs if tx["outcome"] == "commit"]


LATENCY_SCENARIOS = (
    ("worker_and_scheduler", dict(long_writer=False, signal_writes=False)),
    ("with_long_writer", dict(long_writer=True, signal_writes=False)),
    ("signal_connection_tick", dict(long_writer=False, signal_writes=True)),
)


@pytest.mark.bench
def test_scheduler_writes_stay_prompt_while_the_worker_stages_the_largest_load(disk_job):
    owners = _env_int("AFX_BENCH_OWNERS", 25)
    rounds_n = _env_int("AFX_BENCH_ROUNDS", 3)
    report = {"filesystem_dev": disk_job["data_dev"], "owners": owners,
              "rounds_per_scenario": rounds_n, "wait_epsilon_sec": WAIT_EPSILON_SEC,
              "scenarios": {}}
    problems = []
    # 待ちを DB の待ちとして数えるため、interpreter の GIL 切替 (既定 5ms) による
    # 遅延が first-statement の時間に混ざらないよう切替間隔を縮める。
    interval = sys.getswitchinterval()
    sys.setswitchinterval(0.0005)
    report["gil_switch_interval_sec"] = 0.0005
    try:
        _latency_scenarios(disk_job, owners, rounds_n, report, problems)
    finally:
        sys.setswitchinterval(interval)
    report["peak_bytes"] = disk_job["peak"].sample()
    report["problems"] = problems
    _write_json("latency.json", report)
    assert not problems, problems


def _latency_scenarios(disk_job, owners, rounds_n, report, problems):
    work, peak = disk_job["work"], disk_job["peak"]
    for name, opts in LATENCY_SCENARIOS:
        with_worker, control = [], []
        for i in range(rounds_n):
            seed = 1000 * (i + 1) + len(name)
            got = _run_round(work, f"{name}-w{i}", with_worker=True, owners=owners,
                             seed=seed, **opts)
            peak.sample()
            with_worker.append(got)
            control.append(_run_round(work, f"{name}-c{i}", with_worker=False,
                                      owners=owners, seed=seed,
                                      duration=got["elapsed"], **opts))
        sched, base = _summarize(with_worker), _summarize(control)
        worker = _worker_summary(with_worker, owners)
        verdict = {
            "wait_p99_le_50ms": (sched["wait_residual_sec"]["p99"] or 0) <= 0.050,
            "wait_max_le_100ms": (sched["wait_residual_sec"]["max"] or 0) <= 0.100,
            "waits_per_tick_le_1": sched["waits_per_tick_max"] <= 1,
            "sqlite_busy_zero": sched["sqlite_busy"] == 0 and base["sqlite_busy"] == 0,
            "core_lock_wait_p99_le_10ms":
                (sched["core_lock_wait_sec"]["p99"] or 0) <= 0.010,
            "tick_max_le_control_plus_100ms":
                (sched["tick_duration_sec"]["max"] or 0)
                <= (base["tick_duration_sec"]["max"] or 0) + 0.100,
            "rows_per_transaction_le_240": worker["rows_per_transaction_max"] <= 240,
            "before_commit_le_50ms": (worker["before_commit_sec"]["max"] or 0) <= 0.050,
            "worker_core_lock_hold_le_50ms":
                (worker["core_lock_hold_sec"]["max"] or 0) <= 0.050,
            "cycle_owners_le_25": worker["cycle_owners_max"] <= 25,
            "cycle_work_within_budget_and_last_transaction":
                (worker["cycle_spent_sec"]["max"] or 0)
                <= worker["cycle_spent_bound_sec"],
            "commit_seconds_recorded": worker["recorded_commit_sec"]["count"] > 0,
            "no_begin_while_flag_up": worker["begin_inside_flag_interval"] == 0,
            "no_commit_during_long_writer": worker["commits_inside_long_writer_hold"] == 0,
            "all_owners_complete": all(n == owners for n in worker["owners_complete"]),
        }
        report["scenarios"][name] = {"scheduler": sched, "control": base,
                                     "worker": worker, "verdict": verdict}
        problems += [f"{name}: {k}" for k, ok in verdict.items() if not ok]


# --- 容量 (byte 容量 B = B0 + C × β の実測) -------------------------------------------

SLICE_RELATED_TABLES = (
    "signal_decisions", "signal_shadow", "ohlcv_slice_sets", "ohlcv_slices",
    "ohlcv_slice_generations", "econ_reaction_keys", "econ_reaction_values")
SHADOW_ROWS_FIXED = 400 * 96 * 1680
SHADOW_RANGE_START = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
EVENT_FIRST_TS = datetime(2026, 11, 3, 12, 30, tzinfo=UTC)


def _bar_template(start, count, seed):
    rng = random.Random(seed)
    rows, price = [], 150.0
    for i in range(count):
        o = round(price, 3)
        c = round(o + rng.uniform(-0.03, 0.03), 3)
        h = round(max(o, c) + rng.uniform(0, 0.02), 3)
        low = round(min(o, c) - rng.uniform(0, 0.02), 3)
        rows.append(SliceRow(start + timedelta(minutes=i), o, h, low, c,
                             float(rng.randint(5, 400)), "mt5-live", "observed"))
        price = c
    return rows


def _file_sizes(path):
    sizes = {}
    for key, suffix in (("db", ""), ("wal", "-wal")):
        try:
            sizes[key] = os.stat(f"{path}{suffix}").st_size
        except FileNotFoundError:
            sizes[key] = 0
    sizes["total"] = sizes["db"] + sizes["wal"]
    return sizes


def _slice_related_bytes(conn):
    """slice 関連 7 表とその index の bytes (dbstat)。"""
    owner = {r[0]: r[1] for r in conn.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type IN ('table','index')")}
    total = 0
    for name, size in conn.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"):
        if owner.get(name) in SLICE_RELATED_TABLES:
            total += size
    return total


def _fresh_empty_db(work, label):
    path = work / f"{label}.db"
    conn = connect(path)
    init_db(conn)
    conn.commit()
    return path, conn


def _remove_db(path):
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(f"{path}{suffix}")


def _measure_empty(work, index):
    path, conn = _fresh_empty_db(work, f"base{index}")
    try:
        truncate = tuple(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
        out = _file_sizes(path)
        out["wal_checkpoint_truncate"] = list(truncate)
        out["slice_related_bytes"] = _slice_related_bytes(conn)
        out["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
        out["synchronous"] = conn.execute("PRAGMA synchronous").fetchone()[0]
        out["wal_autocheckpoint"] = conn.execute(
            "PRAGMA wal_autocheckpoint").fetchone()[0]
    finally:
        conn.close()
        _remove_db(path)
    return out


def _fill_run(work, label, rows_target, shadow_fraction, peak):
    """fresh DB に signal / event 比で `rows_target` 行を、240 行以下の transaction に
    分けて投入し、最後の COMMIT 直後・明示 checkpoint 前の大きさを測る。"""
    path, conn = _fresh_empty_db(work, label)
    counts = {"commits": 0}

    def trace(statement):
        if statement == "COMMIT":
            counts["commits"] += 1
            if counts["commits"] % 200 == 0:
                peak.sample()

    started = time.monotonic()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        conn.set_trace_callback(trace)
        generation = os_.new_generation(conn, now=BENCH_NOW)
        shadow_rows = round(rows_target * shadow_fraction)
        event_rows = rows_target - shadow_rows
        shadow_template = _bar_template(SHADOW_RANGE_START, 1680, 11)
        event_template = _bar_template(EVENT_FIRST_TS - timedelta(minutes=15), 255, 12)
        inserted = shadow_sets = event_sets = 0
        remaining, n = shadow_rows, 0
        while remaining > 0:
            take = min(1680, remaining)
            remaining -= take
            shadow_id = _shadow(conn, n, decided=BENCH_DECIDED,
                                entry_type="limit", limit_price=149.9)
            n += 1
            end = SHADOW_RANGE_START + timedelta(minutes=take)
            set_id = None
            for i in range(0, take, MAX_BATCH):
                out = os_.stage_shadow_rows(
                    conn, shadow_id=shadow_id, pair="USDJPY",
                    range_start=SHADOW_RANGE_START, range_end=end,
                    generation=generation, rows=shadow_template[i:min(i + MAX_BATCH, take)],
                    now=BENCH_NOW)
                assert out.committed
                set_id = out.set_id
            stamp = ss.get(conn, shadow_id)["updated_at"]
            assert os_.complete_set(
                conn, set_id=set_id, generation=generation, now=BENCH_NOW,
                shadow=os_.ShadowCas(shadow_id, "pending_entry", stamp,
                                     ss.ShadowOutcome.no_fill())).committed
            inserted += take
            shadow_sets += 1
        remaining, k = event_rows, 0
        while remaining > 0:
            take = min(255, remaining)
            remaining -= take
            ts = EVENT_FIRST_TS + timedelta(days=7 * k)
            k += 1
            event_id = conn.execute(
                "INSERT INTO econ_events (ts,country,name,importance) "
                "VALUES (?,?,?,3)", (ts.isoformat(), "USD", f"bench{k}")).lastrowid
            set_id = os_.create_event_set_in_tx(conn, event_id=event_id,
                                                pair="USDJPY", now=BENCH_NOW)
            conn.commit()
            start = ts - timedelta(minutes=15)
            rows = [dataclasses.replace(r, bar_time=start + timedelta(minutes=j))
                    for j, r in enumerate(event_template[:take])]
            for i in range(0, take, MAX_BATCH):
                assert os_.stage_event_rows(
                    conn, set_id=set_id, generation=generation,
                    rows=rows[i:i + MAX_BATCH], assumed_spread_pips=1.0).committed
            if take == 255:
                assert os_.complete_set(conn, set_id=set_id, generation=generation,
                                        now=BENCH_NOW).committed
                for window in (15, 60, 240):
                    conn.execute(
                        "INSERT INTO econ_reaction_values (set_id,window_min,status,"
                        "range_pips,close_change_pips,abs_close_change_pips,"
                        "net_abs_close_change_pips,rule_version,computed_at) "
                        "VALUES (?,?,'resolved',12.5,-3.25,3.25,1.75,1,?)",
                        (set_id, window, BENCH_NOW.isoformat()))
                for currency in ("USD", "JPY"):
                    conn.execute(
                        "INSERT INTO econ_reaction_keys (event_id,currency,set_id,"
                        "decided_at) VALUES (?,?,?,?)",
                        (event_id, currency, set_id, BENCH_NOW.isoformat()))
                conn.commit()
                event_sets += 1
            inserted += take
        stored = conn.execute("SELECT COUNT(*) FROM ohlcv_slices").fetchone()[0]
        assert stored == inserted == rows_target
        sizes = _file_sizes(path)
        sizes["slice_related_bytes"] = _slice_related_bytes(conn)
        sizes["rows"] = stored
        sizes["commits"] = counts["commits"]
        sizes["shadow_sets"] = shadow_sets
        sizes["complete_event_sets"] = event_sets
        sizes["elapsed_sec"] = time.monotonic() - started
        peak.sample()
    finally:
        conn.set_trace_callback(None)
        conn.close()
        _remove_db(path)
    return sizes


MAX_BATCH = os_.MAX_BATCH_ROWS


@pytest.mark.bench
def test_byte_capacity_constants_cover_the_measured_sizes_on_the_real_disk(disk_job):
    work, peak = disk_job["work"], disk_job["peak"]
    os.sync()
    fixed_rows = os_.row_capacity(daily_signals=96, importance_min=3, pair_count=1)
    assert fixed_rows == FIXED_CAPACITY_ROWS
    capacity_rows = _env_int("AFX_BENCH_CAPACITY_C", FIXED_CAPACITY_ROWS)
    runs_each = _env_int("AFX_BENCH_CAPACITY_RUNS", 3)
    points = sorted({math.ceil(capacity_rows / 1000), math.ceil(capacity_rows / 100),
                     math.ceil(capacity_rows / 10)})
    fraction = SHADOW_ROWS_FIXED / FIXED_CAPACITY_ROWS
    started = time.monotonic()

    bases = [_measure_empty(work, i) for i in range(runs_each)]
    base_bytes = max(b["total"] for b in bases)
    measured = {}
    for rows in points:
        measured[rows] = [_fill_run(work, f"fill{rows}-{i}", rows, fraction, peak)
                          for i in range(runs_each)]
    beta = max(math.ceil((m["total"] - base_bytes) / m["rows"])
               for runs in measured.values() for m in runs)
    envelope = all(base_bytes + m["rows"] * beta >= m["total"]
                   for runs in measured.values() for m in runs)
    top = max(points)
    low = max(p for p in points if p < top) if len(points) > 1 else None
    size_of = {p: max(m["total"] for m in measured[p]) for p in points}
    if low is not None:
        slope = (size_of[top] - size_of[low]) / (top - low)
        trend = size_of[top] + slope * (capacity_rows - top)
    else:
        slope, trend = None, None
    extrapolated = base_bytes + capacity_rows * beta
    elapsed = time.monotonic() - disk_job["started"]
    code = {"SLICE_BASE_BYTES": os_.SLICE_BASE_BYTES,
            "SLICE_BYTES_PER_ROW": os_.SLICE_BYTES_PER_ROW}
    code_covers = (
        os_.SLICE_BASE_BYTES >= base_bytes
        and all(os_.byte_capacity(m["rows"]) >= m["total"]
                for runs in measured.values() for m in runs)
        and (trend is None or os_.byte_capacity(capacity_rows) >= trend))
    full_scale = capacity_rows == FIXED_CAPACITY_ROWS and runs_each >= 3
    peak_bytes = peak.sample()
    report = {
        "full_scale": full_scale,
        "capacity_rows": capacity_rows,
        "checkpoint": {"before_empty_measure": "wal_checkpoint(TRUNCATE) after the DDL commit",
                       "before_fill_measure": "none (production auto-checkpoint only)",
                       "journal_mode": bases[0]["journal_mode"],
                       "synchronous": bases[0]["synchronous"],
                       "wal_autocheckpoint": bases[0]["wal_autocheckpoint"]},
        "shadow_row_fraction": fraction,
        "empty_measurements": bases,
        "B0": base_bytes,
        "points": {str(p): measured[p] for p in points},
        "beta": beta,
        "upper_envelope_holds": envelope,
        "trend_slope_bytes_per_row": slope,
        "trend_prediction_at_C": trend,
        "extrapolated_at_C": extrapolated,
        "extrapolation_covers_trend": trend is None or extrapolated >= trend,
        "code_constants": code,
        "code_constants_cover": code_covers,
        "job_elapsed_sec": elapsed,
        "capacity_elapsed_sec": time.monotonic() - started,
        "peak_bytes": peak_bytes,
        "limits": {"seconds": JOB_LIMIT_SEC, "bytes": JOB_LIMIT_BYTES},
    }
    _write_json("capacity.json", report)
    assert envelope
    assert report["extrapolation_covers_trend"]
    assert elapsed <= JOB_LIMIT_SEC
    assert peak_bytes <= JOB_LIMIT_BYTES
    if full_scale:
        assert code_covers, (code, base_bytes, beta)
