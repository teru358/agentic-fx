"""Reference frames and strategy-level skips in the live producer."""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Bar
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.reference_frames import (
    REFERENCE_REASONS, RUNTIME_WINDOW_REASON,
)
from agentic_fx.plugin.resolve import (
    ResolvedIndicator, ResolvedIndicatorSet, freeze_params,
)
from agentic_fx.plugin.sandbox import SandboxError
from agentic_fx.plugin.signal_producer import SignalProducer
from agentic_fx.store import ohlcv as ohlcv_store
from agentic_fx.store import signals
from agentic_fx.store.db import connect, init_db

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
BASE = load_settings(EXAMPLE)
SOURCE = "mt5-live"
OPEN = {"action": "open", "rationale": "go", "direction": "long",
        "entry_type": "market", "limit_price": None, "stop_loss": 100.0,
        "take_profit": None}


def _settings(watch=("JP225",), pairs=("USDJPY",)):
    return SimpleNamespace(
        plugin=BASE.plugin, pairs=list(pairs), worker=BASE.worker,
        datafeed=SimpleNamespace(closed_bar_grace_sec=0, primary="mt5",
                                 watch_symbols=list(watch)))


class _Activity:
    def __init__(self):
        self.rows = []

    def write(self, category, event, summary, ref_id=""):
        self.rows.append((event, summary))

    def events(self, name):
        return [summary for event, summary in self.rows if event == name]


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _seed(conn, symbol, interval, start, end, *, price=100.0, skip=()):
    width = {"1h": timedelta(hours=1), "15m": timedelta(minutes=15)}[interval]
    bars, t = [], start
    while t < end:
        if t not in skip:
            bars.append(Bar(symbol, interval, t, price, price, price, price, 5.0))
        t += width
    ohlcv_store.upsert_cache_bars(conn, bars, source=SOURCE)


def _meta(name="ref", timeframe="1h", max_bars=3, pairs=("USDJPY",), hash_="a" * 64):
    return PluginMeta(name=name, kind="strategy", path=Path("/nonexistent"), params={},
                      timeframe=timeframe, pairs=pairs, max_bars=max_bars,
                      content_hash=hash_)


def _item(alias="jp", symbol="JP225", timeframe="1h", max_age_hours=None, max_bars=100,
          mode="literal"):
    from agentic_fx.plugin.reference_time import timeframe_seconds

    seconds = timeframe_seconds(timeframe)
    return ResolvedIndicator(
        alias=alias, plugin_name="ema", plugin_py=Path("/nonexistent/plugin.py"),
        content_hash="c" * 64, params=freeze_params({}), max_bars=max_bars,
        outputs=("value",), pinned=True, symbol_mode=mode,
        literal_symbol=None if mode == "self" else symbol, timeframe=timeframe,
        max_age_seconds=(max_age_hours * 3600 if max_age_hours else 3 * seconds),
        frame_id=f"{'self' if mode == 'self' else symbol}@{timeframe}")


def _resolved(meta, *items):
    return {(meta.name, meta.content_hash): ResolvedIndicatorSet(
        inventory_root=Path("/nonexistent"), items=tuple(items), all_pinned=True)}


class _Sandbox:
    def __init__(self, result=OPEN, error=None):
        self.calls, self.result, self.error = [], result, error

    def __call__(self, meta, payload, *, settings):
        self.calls.append((meta.name, payload))
        if self.error is not None:
            raise self.error
        return self.result


def _run(conn, producer, metas, resolved, now, sandbox, *, activity=None, settings=None):
    return producer.evaluate_due_plugins(
        conn, plugins=metas, now=now, source=SOURCE, sandbox_run=sandbox,
        settings=settings or _settings(), resolved_by_identity=resolved, activity=activity)


T0 = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)   # Wednesday
NOW = datetime(2026, 7, 22, 12, 1, tzinfo=timezone.utc)


def _fixture(tmp_path, *, jp_end=None, items=None, max_bars=3, pairs=("USDJPY",)):
    conn = _conn(tmp_path)
    for pair in pairs:
        _seed(conn, pair, "1h", T0 - timedelta(hours=8), T0 + timedelta(hours=4),
              price=100.0 + pairs.index(pair))
    jp_end = jp_end or T0 + timedelta(hours=4)
    _seed(conn, "JP225", "1h", T0 - timedelta(hours=40), jp_end, price=7.0)
    meta = _meta(max_bars=max_bars, pairs=pairs)
    return conn, meta, _resolved(meta, *(items or [_item()]))


def _evaluated(producer, last_bucket=datetime(2026, 7, 22, 11, tzinfo=timezone.utc)):
    """Whether the cursor moved onto the bucket that is under evaluation."""
    return [b for b in producer._cursor.values() if b >= last_bucket]


def _bar_ts(meta_name, conn):
    return [row["bar_ts"] for row in conn.execute(
        "SELECT bar_ts FROM signals WHERE plugin=?", (meta_name,))]


def test_a_literal_frame_reaches_the_call_with_the_span_the_window_covers(tmp_path):
    conn, meta, resolved = _fixture(tmp_path)
    sandbox = _Sandbox()
    inserted = _run(conn, SignalProducer(), [meta], resolved, NOW, sandbox)
    payload = next(p for _, p in sandbox.calls)
    assert set(payload["frames"]) == {"JP225@1h"}
    frame = payload["frames"]["JP225@1h"]
    # main df = 09:00..11:00 -> d_first 10:00, d_last 12:00 -> K = 3, and the
    # default max_age (3h) plus one bar reaches back to 08:00
    assert list(frame.index) == [T0 + timedelta(hours=h) for h in (0, 1, 2, 3)]
    assert set(frame["close"]) == {7.0}
    assert inserted >= 1
    conn.close()


def test_a_strategy_without_reference_dependencies_has_the_unchanged_payload(tmp_path):
    conn, meta, _ = _fixture(tmp_path)
    resolved = _resolved(meta, replace(_item(), frame_id=None, symbol_mode="self"))
    sandbox = _Sandbox()
    _run(conn, SignalProducer(), [meta], resolved, NOW, sandbox)
    assert sandbox.calls and all(set(p) == {"df", "params"} for _, p in sandbox.calls)
    empty = _resolved(meta)
    sandbox = _Sandbox()
    _run(conn, SignalProducer(), [meta], empty, NOW + timedelta(hours=1), sandbox)
    assert all(set(p) == {"df", "params"} for _, p in sandbox.calls)
    conn.close()


def test_aliases_sharing_one_coordinate_send_one_frame(tmp_path):
    conn, meta, resolved = _fixture(
        tmp_path, items=[_item("a"), _item("b"), _item("c", timeframe="1h")])
    sandbox = _Sandbox()
    _run(conn, SignalProducer(), [meta], resolved, NOW, sandbox)
    assert len(sandbox.calls[0][1]["frames"]) == 1
    conn.close()


def test_self_with_another_timeframe_is_the_evaluated_pairs_own_frame(tmp_path):
    conn, meta, resolved = _fixture(
        tmp_path, pairs=("USDJPY", "EURUSD"),
        items=[_item("r", symbol="self", timeframe="4h", mode="self")])
    meta = replace(meta, pairs=("USDJPY", "EURUSD"))
    resolved = _resolved(meta, _item("r", symbol="self", timeframe="4h", mode="self"))
    sandbox = _Sandbox()
    producer = SignalProducer()
    _run(conn, producer, [meta], resolved, NOW, sandbox,
         settings=_settings(pairs=("USDJPY", "EURUSD")))
    by_close = {}
    for _, payload in sandbox.calls:
        frame = payload["frames"]["self@4h"]
        by_close[float(payload["df"]["close"].iloc[-1])] = set(frame["close"])
    assert by_close == {100.0: {100.0}, 101.0: {101.0}}
    conn.close()


def test_a_literal_outside_the_watch_list_sends_no_frame_and_skips(tmp_path):
    conn, meta, resolved = _fixture(tmp_path)
    sandbox, activity = _Sandbox(), _Activity()
    _run(conn, SignalProducer(), [meta], resolved, NOW, sandbox, activity=activity,
         settings=_settings(watch=()))
    assert sandbox.calls == []
    assert any("reason=missing" in text for text in activity.events("reference_unavailable"))
    conn.close()


def _four_hour_fixture(tmp_path, *, skip=(), now=NOW, timeframe="4h"):
    conn = _conn(tmp_path)
    _seed(conn, "USDJPY", "1h", T0 - timedelta(days=3), T0 + timedelta(hours=4))
    _seed(conn, "JP225", "1h", T0 - timedelta(days=3), T0 + timedelta(hours=4),
          price=7.0, skip=skip)
    meta = _meta(max_bars=3)
    resolved = _resolved(meta, _item(timeframe=timeframe, max_age_hours=4))
    sandbox = _Sandbox()
    _run(conn, SignalProducer(), [meta], resolved, now, sandbox)
    frames = [p["frames"][f"JP225@{timeframe}"] for _, p in sandbox.calls]
    conn.close()
    return frames


def test_a_four_hour_reference_is_built_from_complete_hours_only(tmp_path):
    frames = _four_hour_fixture(tmp_path)
    frame = frames[-1]
    # main 09:00..11:00 -> K_ref_actual = 2 four-hour rows = 8 native hours from 04:00
    assert list(frame.index) == [T0 - timedelta(hours=4), T0]
    assert set(frame["volume"]) == {4 * 5.0}


def test_a_missing_leading_hour_drops_the_first_four_hour_row(tmp_path):
    frames = _four_hour_fixture(tmp_path, skip={T0 - timedelta(hours=4)})
    frame = frames[-1]
    assert list(frame.index) == [T0]


def test_a_partial_trailing_bucket_is_not_offered(tmp_path):
    frames = _four_hour_fixture(tmp_path, skip={T0 + timedelta(hours=3)})
    frame = frames[-1]
    assert T0 not in list(frame.index)
    assert list(frame.index)[-1] == T0 - timedelta(hours=4)


def test_a_daily_reference_needs_complete_utc_days(tmp_path):
    conn = _conn(tmp_path)
    day = datetime(2026, 7, 20, tzinfo=timezone.utc)
    _seed(conn, "USDJPY", "1h", day, T0 + timedelta(hours=4))
    _seed(conn, "JP225", "1h", day, T0 + timedelta(hours=4), price=7.0)
    meta = _meta(max_bars=3)
    resolved = _resolved(meta, _item(timeframe="1d", max_age_hours=24))
    sandbox = _Sandbox()
    _run(conn, SignalProducer(), [meta], resolved, NOW, sandbox)
    frame = sandbox.calls[-1][1]["frames"]["JP225@1d"]
    assert list(frame.index) == [day, day + timedelta(days=1)]
    assert set(frame["volume"]) == {24 * 5.0}
    conn.close()


def _weekend_conn(tmp_path):
    conn = _conn(tmp_path)
    thu = datetime(2026, 7, 16, tzinfo=timezone.utc)
    for symbol in ("USDJPY", "JP225"):
        _seed(conn, symbol, "1h", thu, datetime(2026, 7, 17, 22, tzinfo=timezone.utc))
        _seed(conn, symbol, "1h", datetime(2026, 7, 19, 22, tzinfo=timezone.utc),
              datetime(2026, 7, 21, 12, tzinfo=timezone.utc))
    return conn


def test_a_span_stretched_by_a_weekend_exceeds_the_window_and_skips_at_runtime(tmp_path):
    conn = _weekend_conn(tmp_path)
    meta = _meta(max_bars=60)
    resolved = _resolved(meta, _item(max_age_hours=200, max_bars=60))
    sandbox, activity = _Sandbox(), _Activity()
    now = datetime(2026, 7, 21, 12, 1, tzinfo=timezone.utc)
    producer = SignalProducer()
    _run(conn, producer, [meta], resolved, now, sandbox, activity=activity)
    assert sandbox.calls == []
    notices = activity.events("reference_unavailable")
    assert notices and all(f"reason={RUNTIME_WINDOW_REASON}" in n for n in notices)
    assert activity.events("reference_stale") == []
    assert activity.events("plugin_eval_failed") == []
    assert not _evaluated(producer, datetime(2026, 7, 21, 11, tzinfo=timezone.utc))
    # the same data with a window that covers the stretched span evaluates
    roomy = _resolved(meta, _item(max_age_hours=200, max_bars=400))
    sandbox = _Sandbox()
    _run(conn, SignalProducer(), [meta], roomy, now, sandbox)
    assert sandbox.calls
    conn.close()


def test_stale_reference_skips_that_strategy_only_and_opens_nothing(tmp_path):
    conn = _conn(tmp_path)
    now = datetime(2026, 10, 27, 12, 1, tzinfo=timezone.utc)
    start = datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)
    _seed(conn, "USDJPY", "1h", start, datetime(2026, 10, 27, 12, tzinfo=timezone.utc))
    _seed(conn, "JP225", "1h", start, datetime(2026, 10, 27, 8, tzinfo=timezone.utc), price=7.0)
    stale = _meta("needs-ref", timeframe="4h", max_bars=2, hash_="b" * 64)
    plain = _meta("plain", timeframe="4h", max_bars=2, hash_="d" * 64)
    resolved = {**_resolved(stale, _item(max_age_hours=3)), **_resolved(plain)}
    sandbox, activity = _Sandbox(), _Activity()
    _run(conn, SignalProducer(), [stale, plain], resolved, now, sandbox, activity=activity)
    last_bucket = datetime(2026, 10, 27, 8, tzinfo=timezone.utc).isoformat()
    assert last_bucket not in _bar_ts("needs-ref", conn)
    assert last_bucket in _bar_ts("plain", conn)
    assert any(f"bucket={last_bucket}" in n and "reason=age_exceeded" in n
               for n in activity.events("reference_unavailable"))
    conn.close()


@pytest.mark.parametrize("reason", ["source_unavailable", "fetch_failed", "budget_deferred"])
def test_ingest_fetch_state_names_the_reason_when_the_frame_is_stale(tmp_path, reason):
    conn, meta, resolved = _fixture(tmp_path, jp_end=T0 - timedelta(hours=6))
    producer = SignalProducer(reference_state=lambda key: reason)
    activity = _Activity()
    _run(conn, producer, [meta], resolved, NOW, _Sandbox(), activity=activity)
    assert all(f"reason={reason}" in n for n in activity.events("reference_unavailable"))
    assert activity.events("reference_unavailable")
    conn.close()


def test_a_fresh_frame_is_used_even_while_ingest_reports_a_failure(tmp_path):
    conn, meta, resolved = _fixture(tmp_path)
    sandbox = _Sandbox()
    producer = SignalProducer(reference_state=lambda key: "source_unavailable")
    _run(conn, producer, [meta], resolved, NOW, sandbox)
    assert sandbox.calls
    conn.close()


def test_an_empty_cache_is_missing_and_a_stale_one_is_age_exceeded(tmp_path):
    conn, meta, resolved = _fixture(tmp_path)
    conn.execute("DELETE FROM ohlcv_cache WHERE symbol='JP225'")
    conn.commit()
    activity = _Activity()
    _run(conn, SignalProducer(), [meta], resolved, NOW, _Sandbox(), activity=activity)
    assert all("reason=missing" in n for n in activity.events("reference_unavailable"))
    conn.close()
    sub = tmp_path / "stale"
    sub.mkdir()
    conn2, meta2, resolved2 = _fixture(sub, jp_end=T0 - timedelta(hours=6))
    activity = _Activity()
    _run(conn2, SignalProducer(), [meta2], resolved2, NOW, _Sandbox(), activity=activity)
    assert all("reason=age_exceeded" in n for n in activity.events("reference_unavailable"))
    conn2.close()


def test_indicator_unavailable_from_the_worker_skips_without_a_plugin_failure(tmp_path):
    conn, meta, resolved = _fixture(tmp_path)
    error = SandboxError("indicator unavailable", code="indicator_unavailable")
    activity = _Activity()
    producer = SignalProducer()
    for _ in range(3):
        _run(conn, producer, [meta], resolved, NOW, _Sandbox(error=error), activity=activity)
    assert activity.events("plugin_eval_failed") == []
    assert all("reason=indicator_unavailable" in n
               for n in activity.events("reference_unavailable"))
    assert not _evaluated(producer)
    assert len(activity.events("reference_unavailable")) == 1, "once per bucket"
    stale = activity.events("reference_stale")
    assert len(stale) == 1 and "reason=indicator_unavailable" in stale[0]
    conn.close()


def test_other_sandbox_errors_still_count_as_plugin_failures(tmp_path):
    conn, meta, resolved = _fixture(tmp_path)
    activity = _Activity()
    _run(conn, SignalProducer(), [meta], resolved, NOW,
         _Sandbox(error=SandboxError("boom", code="plugin_error")), activity=activity)
    assert len(activity.events("plugin_eval_failed")) == 1
    assert activity.events("reference_unavailable") == []
    conn.close()


def test_transitions_are_recorded_once_and_recovery_names_the_previous_reason(tmp_path):
    conn, meta, resolved = _fixture(tmp_path, jp_end=T0 - timedelta(hours=6))
    producer = SignalProducer()
    activity = _Activity()
    for _ in range(3):
        _run(conn, producer, [meta], resolved, NOW, _Sandbox(), activity=activity)
    assert len(activity.events("reference_stale")) == 1
    stale = activity.events("reference_stale")[0]
    assert "symbol=JP225" in stale and "timeframe=1h" in stale and "reason=age_exceeded" in stale
    assert " age=" in stale and "\n" not in stale
    _seed(conn, "JP225", "1h", T0 - timedelta(hours=6), T0 + timedelta(hours=4), price=7.0)
    for _ in range(2):
        _run(conn, producer, [meta], resolved, NOW, _Sandbox(), activity=activity)
    recovered = activity.events("reference_recovered")
    assert len(recovered) == 1
    assert "recovered_from=age_exceeded" in recovered[0] and "reason=" not in recovered[0]
    assert len(activity.events("reference_stale")) == 1
    conn.close()


def test_age_is_its_own_token_and_never_part_of_reason(tmp_path):
    conn, meta, resolved = _fixture(tmp_path, jp_end=T0 - timedelta(hours=6))
    activity = _Activity()
    _run(conn, SignalProducer(), [meta], resolved, NOW, _Sandbox(), activity=activity)
    text = activity.events("reference_stale")[0]
    fields = dict(part.split("=", 1) for part in text.split())
    assert fields["reason"] == "age_exceeded" and fields["age"].isdigit()
    conn.close()


def test_the_reason_vocabulary_is_a_closed_set():
    assert set(REFERENCE_REASONS) == {
        "missing", "age_exceeded", "fetch_failed", "budget_deferred",
        "source_unavailable", "indicator_unavailable"}
    assert RUNTIME_WINDOW_REASON == "reference_window_exceeded_at_runtime"


def test_every_reason_the_producer_emits_is_in_the_closed_set(tmp_path):
    seen = set()
    cases = [("source_unavailable", None), ("fetch_failed", None), ("budget_deferred", None),
             (None, "missing"), (None, "age_exceeded")]
    for index, (state, kind) in enumerate(cases):
        sub = tmp_path / f"c{index}"
        sub.mkdir()
        conn, meta, resolved = _fixture(sub, jp_end=T0 - timedelta(hours=6))
        if kind == "missing":
            conn.execute("DELETE FROM ohlcv_cache WHERE symbol='JP225'")
            conn.commit()
        activity = _Activity()
        _run(conn, SignalProducer(reference_state=lambda key, s=state: s),
             [meta], resolved, NOW, _Sandbox(), activity=activity)
        for text in activity.events("reference_unavailable") + activity.events("reference_stale"):
            seen.add(dict(part.split("=", 1) for part in text.split())["reason"])
        conn.close()
    assert seen == {"source_unavailable", "fetch_failed", "budget_deferred", "missing",
                    "age_exceeded"}
    assert seen <= set(REFERENCE_REASONS)


def test_cursor_is_not_advanced_so_the_bucket_is_retried_when_the_reference_returns(tmp_path):
    conn, meta, resolved = _fixture(tmp_path, jp_end=T0 - timedelta(hours=6))
    producer = SignalProducer()
    sandbox = _Sandbox()
    _run(conn, producer, [meta], resolved, NOW, sandbox)
    assert sandbox.calls == [] and not _evaluated(producer)
    _seed(conn, "JP225", "1h", T0 - timedelta(hours=6), T0 + timedelta(hours=4), price=7.0)
    _run(conn, producer, [meta], resolved, NOW, sandbox)
    assert sandbox.calls
    conn.close()


def test_runtime_skip_logs_are_one_line(tmp_path, caplog):
    conn, meta, resolved = _fixture(tmp_path, jp_end=T0 - timedelta(hours=6))
    caplog.set_level(logging.INFO)
    activity = _Activity()
    _run(conn, SignalProducer(), [meta], resolved, NOW, _Sandbox(), activity=activity)
    assert all("\n" not in text for _, text in activity.rows)
    conn.close()
