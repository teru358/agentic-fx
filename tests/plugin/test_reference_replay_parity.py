"""A replay hands a strategy the same reference input a live tick would.

The same UTC fixture is stored twice: as native 1h rows in the live cache (what the
producer reads) and as 1m rows in the replay dataset (what the backtest adapter reads).
Both paths must pick the same reference rows, or skip together.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from agentic_fx.backtest.dataset import HistoryDataset
from agentic_fx.core.contracts import Bar
from agentic_fx.plugin import strategy_adapter
from agentic_fx.plugin.reference_frames import (
    RUNTIME_WINDOW_REASON, load_history_reference_frame, load_reference_frame,
)
from agentic_fx.plugin.resolve import ResolvedIndicatorSet
from agentic_fx.plugin.signal_producer import SignalProducer
from agentic_fx.store import ohlcv as ohlcv_store
from tests.backtest.factories import SETTINGS
from tests.plugin.test_signal_producer_reference import (
    SOURCE, _Sandbox, _conn, _item, _meta, _resolved, _run, _settings,
)

HISTORY = HistoryDataset("dukascopy", "1m")
FRIDAY_LAST = datetime(2026, 10, 23, 15, tzinfo=timezone.utc)   # JP225 last end
MONDAY_FIRST = datetime(2026, 10, 26, 1, tzinfo=timezone.utc)   # JP225 first row start
FIRST = datetime(2026, 10, 18, 0, tzinfo=timezone.utc)
LAST = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
SETTINGS_WITH_WATCH = SETTINGS.model_copy(update={
    "datafeed": SETTINGS.datafeed.model_copy(update={"watch_symbols": ["JP225"]})})


def _hours(start, end, *, skip=lambda t: False):
    t = start
    while t < end:
        if not skip(t):
            yield t
        t += timedelta(hours=1)


def _seed(conn, symbol, hours):
    """Each hour is 60 one-minute rows and the one native 1h row they aggregate to."""
    minute_rows, hour_bars = [], []
    for t in hours:
        price = 100.0 + (t - FIRST).total_seconds() / 3600
        for m in range(60):
            minute_rows.append((symbol, "1m", (t + timedelta(minutes=m)).isoformat(),
                                price, price + 1, price - 1, price, 1.0, 0.01))
        hour_bars.append(Bar(symbol, "1h", t, price, price + 1, price - 1, price, 60.0))
    ohlcv_store.import_history_bars(conn, minute_rows, source="dukascopy")
    ohlcv_store.upsert_cache_bars(conn, hour_bars, source=SOURCE)


def _world(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "USDJPY", _hours(FIRST, LAST))
    _seed(conn, "JP225", _hours(
        FIRST, LAST, skip=lambda t: FRIDAY_LAST <= t < MONDAY_FIRST))
    return conn


class _Session:
    def __init__(self):
        self.calls = []

    def call(self, payload):
        self.calls.append(payload)
        return {"action": "hold", "rationale": "x", "direction": None,
                "entry_type": None, "limit_price": None, "stop_loss": None,
                "take_profit": None}


def _live(conn, meta, resolved, bucket_start):
    sandbox = _Sandbox(result={"action": "hold", "rationale": "x", "direction": None,
                               "entry_type": None, "limit_price": None,
                               "stop_loss": None, "take_profit": None})
    width = timedelta(hours=4 if meta.timeframe == "4h" else 1)
    _run(conn, SignalProducer(), [meta], resolved, bucket_start + width + timedelta(minutes=1),
         sandbox, settings=_settings(watch=("JP225",)))
    mine = [p for _, p in sandbox.calls if p["df"].index[-1] == bucket_start]
    return mine[-1] if mine else None


def _replay(conn, meta, resolved_set, bucket_start):
    session = _Session()
    source = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=HISTORY, settings=SETTINGS_WITH_WATCH,
        resolved=resolved_set, session=session)
    # replay drives 1h closed bars; the bar that closes the strategy bucket
    last_hour = bucket_start + (timedelta(hours=3) if meta.timeframe == "4h" else timedelta(0))
    source(Bar("USDJPY", "1h", last_hour, 1.0, 1.0, 1.0, 1.0, 1.0))
    return session.calls[-1] if session.calls else None


CASES = [
    # (name, strategy tf, ref tf, max_age_hours, bucket_start, usable)
    ("friday end, fresh", "1h", "1h", None, datetime(2026, 10, 23, 15, tzinfo=timezone.utc), True),
    ("friday end, stale by default age", "1h", "1h", None,
     datetime(2026, 10, 23, 19, tzinfo=timezone.utc), False),
    ("friday end, weekend age allowed", "1h", "1h", 84,
     datetime(2026, 10, 23, 19, tzinfo=timezone.utc), True),
    ("monday reopen before the reference", "1h", "1h", 84,
     datetime(2026, 10, 26, 0, tzinfo=timezone.utc), True),
    ("monday reopen, stale by default age", "1h", "1h", None,
     datetime(2026, 10, 26, 0, tzinfo=timezone.utc), False),
    ("monday reference resumed", "1h", "1h", None,
     datetime(2026, 10, 26, 1, tzinfo=timezone.utc), True),
    ("four hour reference derived from hours", "4h", "4h", 100,
     datetime(2026, 10, 26, 0, tzinfo=timezone.utc), True),
    ("four hour reference stale by default age", "4h", "4h", None,
     datetime(2026, 10, 26, 0, tzinfo=timezone.utc), False),
]


@pytest.mark.parametrize("label,strategy_tf,ref_tf,max_age,bucket,usable", CASES,
                         ids=[c[0] for c in CASES])
def test_replay_and_live_choose_the_same_reference_input(
        tmp_path, label, strategy_tf, ref_tf, max_age, bucket, usable):
    conn = _world(tmp_path)
    meta = _meta(timeframe=strategy_tf, max_bars=3)
    item = _item(timeframe=ref_tf, max_age_hours=max_age)
    live = _live(conn, meta, _resolved(meta, item), bucket)
    replay = _replay(conn, meta, ResolvedIndicatorSet(
        inventory_root=Path("/nonexistent"), items=(item,), all_pinned=True), bucket)

    if not usable:
        assert live is None and replay is None
        return
    assert live is not None and replay is not None
    assert list(replay["frames"]) == list(live["frames"]) == [f"JP225@{ref_tf}"]
    pd.testing.assert_frame_equal(replay["df"], live["df"], check_freq=False)
    pd.testing.assert_frame_equal(replay["frames"][f"JP225@{ref_tf}"],
                                  live["frames"][f"JP225@{ref_tf}"], check_freq=False)
    conn.close()


def test_replay_never_uses_a_reference_row_that_ends_after_the_decision(tmp_path):
    conn = _world(tmp_path)
    meta = _meta(timeframe="1h", max_bars=3)
    item = _item()
    bucket = datetime(2026, 10, 26, 3, tzinfo=timezone.utc)
    call = _replay(conn, meta, ResolvedIndicatorSet(
        inventory_root=Path("/nonexistent"), items=(item,), all_pinned=True), bucket)
    frame = call["frames"]["JP225@1h"]
    assert frame.index[-1] + timedelta(hours=1) <= bucket + timedelta(hours=1)
    conn.close()


def test_history_loader_matches_the_live_loader_row_for_row(tmp_path):
    conn = _world(tmp_path)
    d_last = pd.Timestamp(datetime(2026, 10, 26, 6, tzinfo=timezone.utc))
    for ref_tf in ("1h", "4h"):
        live = load_reference_frame(
            conn, "JP225", reference_timeframe=ref_tf, source=SOURCE, d_last=d_last,
            now=datetime(2026, 10, 27, tzinfo=timezone.utc), grace=timedelta(0),
            actual_bars=4, max_age_seconds=100 * 3600)
        replay = load_history_reference_frame(
            conn, "JP225", reference_timeframe=ref_tf, source="dukascopy",
            base_interval="1m", d_last=d_last, actual_bars=4,
            max_age_seconds=100 * 3600)
        pd.testing.assert_frame_equal(replay, live, check_freq=False)
    conn.close()


def test_a_window_the_replay_stretches_past_max_bars_skips_like_live(tmp_path):
    conn = _world(tmp_path)
    meta = _meta(timeframe="1h", max_bars=3)
    item = _item(max_bars=1)   # a reference indicator that can hold one bar only
    bucket = datetime(2026, 10, 26, 3, tzinfo=timezone.utc)
    assert _replay(conn, meta, ResolvedIndicatorSet(
        inventory_root=Path("/nonexistent"), items=(item,), all_pinned=True), bucket) is None
    assert RUNTIME_WINDOW_REASON == "reference_window_exceeded_at_runtime"
    conn.close()


def test_a_replay_without_reference_dependencies_keeps_the_two_key_payload(tmp_path):
    conn = _world(tmp_path)
    meta = _meta(timeframe="1h", max_bars=3)
    call = _replay(conn, meta, ResolvedIndicatorSet.empty(Path("/nonexistent")),
                   datetime(2026, 10, 26, 3, tzinfo=timezone.utc))
    assert set(call) == {"df", "params"}
    conn.close()


def test_a_literal_outside_the_watch_list_skips_the_replay(tmp_path):
    conn = _world(tmp_path)
    meta = _meta(timeframe="1h", max_bars=3)
    session = _Session()
    source = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=HISTORY, settings=SETTINGS,
        resolved=ResolvedIndicatorSet(inventory_root=Path("/nonexistent"),
                                      items=(_item(),), all_pinned=True),
        session=session)
    source(Bar("USDJPY", "1h", datetime(2026, 10, 26, 3, tzinfo=timezone.utc),
               1.0, 1.0, 1.0, 1.0, 1.0))
    assert session.calls == []
    conn.close()


def test_every_call_of_a_replay_without_references_has_the_two_key_payload(tmp_path):
    conn = _world(tmp_path)
    meta = _meta(timeframe="1h", max_bars=3)
    session = _Session()
    source = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=HISTORY, settings=SETTINGS_WITH_WATCH,
        resolved=ResolvedIndicatorSet.empty(Path("/nonexistent")), session=session)
    t = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
    for hour in range(30):
        source(Bar("USDJPY", "1h", t + timedelta(hours=hour), 1.0, 1.0, 1.0, 1.0, 1.0))
    assert len(session.calls) == 30
    assert all(set(call) == {"df", "params"} for call in session.calls)
    conn.close()


def test_a_stale_reference_is_refused_even_when_a_long_strategy_window_reaches_it(tmp_path):
    """100 strategy bars look back far enough to contain the last Friday row, so only
    the freshness rule can keep it out."""
    conn = _world(tmp_path)
    meta = _meta(timeframe="1h", max_bars=100)
    item = _item(max_bars=200)
    bucket = datetime(2026, 10, 23, 19, tzinfo=timezone.utc)
    assert _live(conn, meta, _resolved(meta, item), bucket) is None
    assert _replay(conn, meta, ResolvedIndicatorSet(
        inventory_root=Path("/nonexistent"), items=(item,), all_pinned=True), bucket) is None
    # the row is in the window the replay loads
    frame = load_history_reference_frame(
        conn, "JP225", reference_timeframe="1h", source="dukascopy", base_interval="1m",
        d_last=pd.Timestamp(bucket + timedelta(hours=1)), actual_bars=100,
        max_age_seconds=3 * 3600)
    assert frame.index[-1] == pd.Timestamp(FRIDAY_LAST - timedelta(hours=1))
    conn.close()


def test_consecutive_buckets_share_one_read_of_the_reference_and_match_uncached_frames(
        tmp_path, monkeypatch):
    conn = _world(tmp_path)
    meta = _meta(timeframe="1h", max_bars=3)
    item = _item(max_age_hours=84)
    session = _Session()
    source = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=HISTORY, settings=SETTINGS_WITH_WATCH,
        resolved=ResolvedIndicatorSet(inventory_root=Path("/nonexistent"),
                                      items=(item,), all_pinned=True), session=session)
    reads = []
    real = strategy_adapter.load_history_native_frame
    monkeypatch.setattr(strategy_adapter, "load_history_native_frame",
                        lambda *a, **k: reads.append(1) or real(*a, **k))
    starts = [datetime(2026, 10, 23, 15, tzinfo=timezone.utc),
              datetime(2026, 10, 26, 0, tzinfo=timezone.utc),
              datetime(2026, 10, 26, 5, tzinfo=timezone.utc)]
    for start in starts:
        source(Bar("USDJPY", "1h", start, 1.0, 1.0, 1.0, 1.0, 1.0))
    assert len(reads) == 1
    assert len(session.calls) == 3
    for start, call in zip(starts, session.calls):
        expected = load_history_reference_frame(
            conn, "JP225", reference_timeframe="1h", source="dukascopy",
            base_interval="1m", d_last=pd.Timestamp(start + timedelta(hours=1)),
            actual_bars=3, max_age_seconds=84 * 3600)
        pd.testing.assert_frame_equal(call["frames"]["JP225@1h"], expected,
                                      check_freq=False)
    conn.close()
