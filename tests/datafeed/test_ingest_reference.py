"""Reference (soft) keys: ordering, backoff, storage, and isolation from price health."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed import sources
from agentic_fx.datafeed.ingest import (
    SOFT_BACKOFF_BASE_SEC, Ingest, classify_fetch_error, soft_backoff_delay,
)
from agentic_fx.datafeed.planner import PlanResult
from agentic_fx.datafeed.outage import OutageStateMachine
from agentic_fx.plugin.loader import IndicatorRef
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.ohlcv import load_cache_bars

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)  # Wednesday


def _settings(*, watch=("JP225",), budget=10, pairs=("USDJPY",), primary="mt5"):
    return SimpleNamespace(
        pairs=list(pairs),
        datafeed=SimpleNamespace(
            primary=primary, intervals=["1m", "1h"], primary_intervals=["1h"],
            ingest_budget_sec=budget, closed_bar_grace_sec=0,
            watch_symbols=list(watch), mt5=SimpleNamespace(bridge_url="http://bridge")))


def _strategy(*refs, name="ref", timeframe="1h", max_bars=50):
    return SimpleNamespace(name=name, kind="strategy", timeframe=timeframe,
                           pairs=("USDJPY",), max_bars=max_bars, indicators=tuple(refs))


def _ref(symbol="JP225", timeframe="1h", alias="jp"):
    return IndicatorRef(alias, "ema", {}, None, symbol=symbol, timeframe=timeframe)


def _db(tmp_path):
    conn = connect(tmp_path / "bars.db")
    init_db(conn)
    return conn


def _bar(symbol, interval, end, back=1):
    width = timedelta(minutes=sources.INTERVAL_MIN[interval])
    return Bar(symbol, interval, end - width * back, 1, 1, 1, 1, 1)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_hard_keys_are_attempted_before_a_soft_key_even_when_the_soft_one_hangs(tmp_path):
    conn = _db(tmp_path)
    clock = _Clock()
    calls = []

    def fetch(pair, interval, start, end, *, timeout):
        calls.append((pair, interval, timeout))
        if pair == "JP225":
            clock.t += 100  # the soft request eats the whole budget
            raise httpx.ReadTimeout("slow")
        return []

    ingest = Ingest(_settings(), [_strategy(_ref())], fetch=fetch, monotonic=clock)
    _, report = ingest.prepare(NOW, conn)
    assert [c[:2] for c in calls] == [("USDJPY", "1m"), ("USDJPY", "1h"), ("JP225", "1h")]
    assert {("USDJPY", "1m"), ("USDJPY", "1h")} <= report.attempted
    assert ("JP225", "1h") in report.soft_attempted
    conn.close()


def test_soft_timeout_never_exceeds_half_of_the_budget(tmp_path):
    conn = _db(tmp_path)
    seen = {}

    def fetch(pair, interval, start, end, *, timeout):
        seen[(pair, interval)] = timeout
        return []

    ingest = Ingest(_settings(budget=10), [_strategy(_ref())], fetch=fetch,
                    monotonic=_Clock())
    ingest.prepare(NOW, conn)
    assert seen[("USDJPY", "1m")] == 10
    assert seen[("JP225", "1h")] == 5
    conn.close()


def test_soft_deferred_hard_deferred_and_backoff_waiting_are_told_apart(tmp_path):
    conn = _db(tmp_path)
    settings = _settings(budget=1, watch=("JP225", "US500"), pairs=("USDJPY", "EURUSD"))
    clock = _Clock()

    def fetch(pair, interval, start, end, *, timeout):
        clock.t += 2
        return []

    ingest = Ingest(settings, [_strategy(_ref("JP225"), _ref("US500", alias="us"))],
                    fetch=fetch, monotonic=clock)
    _, report = ingest.prepare(NOW, conn)
    assert report.deferred == {("EURUSD", "1h"), ("USDJPY", "1h")}
    assert report.attempted == {("EURUSD", "1m")}
    assert report.soft_deferred == {("JP225", "1h"), ("US500", "1h")}
    assert not report.soft_attempted
    assert not (report.soft_deferred & report.deferred)
    assert not report.soft_backoff_waiting
    conn.close()


def test_a_key_waiting_in_backoff_is_reported_only_as_soft_backoff_waiting(tmp_path):
    conn = _db(tmp_path)

    def fetch(pair, interval, start, end, *, timeout):
        if pair == "JP225":
            raise httpx.ConnectError("down")
        return []

    ingest = Ingest(_settings(), [_strategy(_ref())], fetch=fetch, monotonic=_Clock())
    ingest.prepare(NOW, conn)
    _, report = ingest.prepare(NOW + timedelta(seconds=1), conn)
    assert ("JP225", "1h") in report.soft_backoff_waiting
    assert ("JP225", "1h") not in report.soft_attempted
    assert ("JP225", "1h") not in report.soft_deferred
    conn.close()


def _attempt_gaps(tmp_path, timeframe, rounds, *, raises):
    conn = _db(tmp_path)
    attempts = []

    def fetch(pair, interval, start, end, *, timeout):
        if pair != "JP225":
            return []
        attempts.append(end)
        if raises:
            raise httpx.ConnectError("down")
        return []

    ingest = Ingest(_settings(), [_strategy(_ref(timeframe=timeframe))], fetch=fetch,
                    monotonic=_Clock())
    now = NOW
    for _ in range(rounds):
        before = len(attempts)
        ingest.prepare(now, conn)
        assert len(attempts) == before + 1
        due = ingest.next_probe_at[("JP225", timeframe)]
        early = due - timedelta(seconds=1)
        ingest.prepare(early, conn)
        assert len(attempts) == before + 1, "must not retry before the backoff elapses"
        now = due
    conn.close()
    return [(b - a).total_seconds() for a, b in zip(attempts, attempts[1:])]


@pytest.mark.parametrize("raises", [True, False])
def test_soft_backoff_doubles_from_sixty_seconds_up_to_one_bar_width(tmp_path, raises):
    gaps = _attempt_gaps(tmp_path, "1h", 9, raises=raises)
    assert gaps == [3600] * 8


def test_soft_backoff_cap_is_the_bar_width_of_a_fifteen_minute_key(tmp_path):
    gaps = _attempt_gaps(tmp_path, "15m", 8, raises=True)
    assert gaps == [900] * 7


def test_a_trading_pair_strategy_key_keeps_the_hard_retry_schedule(tmp_path):
    conn = _db(tmp_path)

    def fetch(pair, interval, start, end, *, timeout):
        if (pair, interval) == ("USDJPY", "15m"):
            raise httpx.ConnectError("down")
        return []

    ingest = Ingest(_settings(), [_strategy(timeframe="15m")], fetch=fetch,
                    monotonic=_Clock())
    _, report = ingest.prepare(NOW, conn)
    assert (("USDJPY", "15m"), "down") in report.failed
    assert ("USDJPY", "15m") not in report.soft_attempted
    assert ingest.next_probe_at[("USDJPY", "15m")] == NOW
    conn.close()


def test_a_trading_pair_requested_as_a_reference_keeps_the_hard_retry_schedule(tmp_path):
    conn = _db(tmp_path)

    def fetch(pair, interval, start, end, *, timeout):
        if (pair, interval) == ("USDJPY", "15m"):
            raise httpx.ConnectError("down")
        return []

    ingest = Ingest(_settings(watch=("USDJPY",)),
                    [_strategy(_ref("USDJPY", "15m", "usd"), timeframe="1h")],
                    fetch=fetch, monotonic=_Clock())
    _, report = ingest.prepare(NOW, conn)
    assert (("USDJPY", "15m"), "down") in report.failed
    assert ("USDJPY", "15m") not in report.soft_attempted
    assert ingest.next_probe_at[("USDJPY", "15m")] == NOW
    conn.close()


def test_a_soft_planner_failure_is_visible_once_in_state_and_logs(tmp_path, caplog):
    conn = _db(tmp_path)

    class Planner:
        def plan(self, source, interval, required):
            return PlanResult(1, True) if required == 1 else PlanResult(0, False)

    caplog.set_level("WARNING")
    ingest = Ingest(_settings(), [_strategy(_ref(), max_bars=50)], fetch=lambda *a, **k: [],
                    planner=Planner(), monotonic=_Clock())
    ingest.prepare(NOW, conn)
    ingest.prepare(NOW + timedelta(seconds=1), conn)
    key = ("JP225", "1h")
    assert ingest.reference_fetch_state[key] == "plan_failed"
    assert sum("reference plan failed for JP225 1h" in r.message for r in caplog.records) == 1
    conn.close()


def test_a_rejected_reference_is_recorded_once_and_not_reconsidered(tmp_path, caplog):
    conn = _db(tmp_path)
    settings = _settings()
    ingest = Ingest(settings, [_strategy(_ref())], fetch=lambda *a, **k: [], monotonic=_Clock())
    settings.datafeed.watch_symbols = []
    caplog.set_level("WARNING")
    _, first = ingest.prepare(NOW, conn)
    _, second = ingest.prepare(NOW + timedelta(seconds=1), conn)
    key = ("JP225", "1h")
    assert first.soft_failed == {(key, "symbol_rejected")}
    assert key not in {failed for failed, _ in second.soft_failed}
    assert ingest.reference_fetch_state[key] == "symbol_rejected"
    assert sum("reference symbol rejected for JP225 1h" in r.message for r in caplog.records) == 1
    conn.close()


def test_backoff_delay_function_pins_the_series_and_the_constant():
    assert SOFT_BACKOFF_BASE_SEC == 60
    assert [soft_backoff_delay(n, 3600) for n in range(1, 9)] == [
        60, 120, 240, 480, 960, 1920, 3600, 3600]
    assert soft_backoff_delay(1, 30) == 30


def test_a_non_empty_fetch_resets_the_backoff_and_waits_for_the_next_bar(tmp_path):
    conn = _db(tmp_path)
    state = {"fail": True}
    close = NOW.replace(minute=0)

    def fetch(pair, interval, start, end, *, timeout):
        if pair != "JP225":
            return []
        if state["fail"]:
            raise httpx.ConnectError("down")
        return [Bar("JP225", "1h", end - timedelta(hours=1), 1, 1, 1, 1, 1)]

    ingest = Ingest(_settings(), [_strategy(_ref())], fetch=fetch, monotonic=_Clock())
    ingest.prepare(NOW, conn)
    ingest.prepare(NOW + timedelta(hours=1), conn)
    assert ingest._backoff[("JP225", "1h")] == 2
    state["fail"] = False
    ingest.prepare(NOW + timedelta(hours=2), conn)
    assert ingest._backoff[("JP225", "1h")] == 0
    # The successful 13:00-14:00 bar is reflected immediately; the next probe is 15:00.
    assert ingest.next_probe_at[("JP225", "1h")] == NOW + timedelta(hours=3)
    conn.close()


def test_a_stale_newest_bar_while_open_backs_off_instead_of_using_fx_confirmation(tmp_path):
    conn = _db(tmp_path)

    def fetch(pair, interval, start, end, *, timeout):
        if pair != "JP225":
            return []
        return [Bar("JP225", "1h", NOW - timedelta(hours=5), 1, 1, 1, 1, 1)]

    ingest = Ingest(_settings(), [_strategy(_ref())], fetch=fetch, monotonic=_Clock())
    ingest.prepare(NOW, conn)
    assert ingest.next_probe_at[("JP225", "1h")] == NOW + timedelta(hours=1)
    conn.close()


def test_soft_keys_rotate_so_the_later_one_is_not_deferred_forever(tmp_path):
    conn = _db(tmp_path)
    settings = _settings(budget=1, watch=("JP225", "US500", "DE40"))
    clock = _Clock()
    order = []

    def fetch(pair, interval, start, end, *, timeout):
        if pair != "USDJPY":
            order.append(pair)
        clock.t += 0.6 if pair != "USDJPY" else 0.0
        return []

    refs = [_ref("JP225", alias="a"), _ref("US500", alias="b"), _ref("DE40", alias="c")]
    ingest = Ingest(settings, [_strategy(*refs)], fetch=fetch, monotonic=clock)
    seen = set()
    now = NOW
    for _ in range(8):
        clock.t = 0.0
        ingest.prepare(now, conn)
        seen.update(order)
        now += timedelta(hours=2)
    assert seen == {"JP225", "US500", "DE40"}
    first_round = order[:2]
    assert len(set(first_round)) == 2
    conn.close()


def test_soft_failures_never_move_the_outage_state_or_hard_report_sets(tmp_path):
    def run(with_reference):
        conn = _db(tmp_path / ("a" if with_reference else "b"))
        activity = ActivityLog(tmp_path / ("act-a" if with_reference else "act-b"))

        def fetch(pair, interval, start, end, *, timeout):
            if pair == "JP225":
                raise httpx.ConnectError("down")
            return [_bar(pair, interval, end)]

        strategies = [_strategy(_ref())] if with_reference else []
        ingest = Ingest(_settings(), strategies, fetch=fetch, monotonic=_Clock())
        hard = ingest.registry.hard_keys()
        assert ("JP225", "1h") not in hard
        outage = OutageStateMachine(
            conn, hard_keys=hard,
            interval_widths={i: timedelta(minutes=sources.INTERVAL_MIN[i]) for _, i in hard},
            grace=timedelta(seconds=0), storage_source=ingest.storage_source,
            activity=activity, ready_confirm_ticks=3, auto_resume_when_flat=True,
            flat_stall_max_sec=3600)
        states, reports = [], []
        for minute in range(6):
            now = NOW + timedelta(minutes=minute)
            _, report = ingest.prepare(now, conn)
            ingest.commit(conn)
            states.append(outage.observe(now, report))
            reports.append(report)
        conn.close()
        return states, reports

    (soft_states, soft_reports), (plain_states, plain_reports) = run(True), run(False)
    assert soft_states == plain_states
    assert set(soft_states) == {"ready"}
    for soft, plain in zip(soft_reports, plain_reports):
        assert soft.attempted == plain.attempted and soft.failed == plain.failed
        assert soft.succeeded == plain.succeeded and soft.empty == plain.empty
        assert soft.deferred == plain.deferred
        assert all(key[0] != "JP225" for key in soft.attempted | soft.succeeded | soft.empty)
        assert all(key[0] != "JP225" for key, _ in soft.failed)
    assert any(("JP225", "1h") in r.soft_attempted and
               ("JP225", "1h") in {k for k, _ in r.soft_failed} for r in soft_reports)


def test_reference_bars_are_saved_as_symbol_interval_mt5_live(tmp_path):
    conn = _db(tmp_path)
    four_hour = _ref(timeframe="4h")

    def fetch(pair, interval, start, end, *, timeout):
        return [_bar(pair, interval, end.replace(minute=0, second=0, microsecond=0))]

    ingest = Ingest(_settings(), [_strategy(four_hour)], fetch=fetch, monotonic=_Clock())
    requested = {key for key in ingest.keys if key[0] == "JP225"}
    assert requested == {("JP225", "1h")}
    ingest.prepare(NOW, conn)
    ingest.commit(conn)
    rows = load_cache_bars(conn, "JP225", "1h", source="mt5-live")
    assert [(b.symbol, b.interval) for b in rows] == [("JP225", "1h")]
    assert load_cache_bars(conn, "JP225", "1m", source="mt5-live") == []
    conn.close()


def test_a_four_hour_declaration_asks_the_source_for_one_hour_bars_only(tmp_path):
    conn = _db(tmp_path)
    asked = []

    def fetch(pair, interval, start, end, *, timeout):
        asked.append((pair, interval, start))
        return []

    ingest = Ingest(_settings(), [_strategy(_ref(timeframe="4h"), _ref(timeframe="1d", alias="d"))],
                    fetch=fetch, monotonic=_Clock())
    ingest.prepare(NOW, conn)
    assert {(p, i) for p, i, _ in asked if p == "JP225"} == {("JP225", "1h")}


def test_the_transport_vs_http_classification_of_a_failed_fetch():
    request = httpx.Request("GET", "http://bridge/ohlcv/JP225")
    assert classify_fetch_error(httpx.ConnectError("x")) == "source_unavailable"
    assert classify_fetch_error(httpx.ReadTimeout("x")) == "source_unavailable"
    assert classify_fetch_error(OSError("x")) == "source_unavailable"
    assert classify_fetch_error(httpx.HTTPStatusError(
        "x", request=request, response=httpx.Response(503, request=request))) == "source_unavailable"
    assert classify_fetch_error(httpx.HTTPStatusError(
        "x", request=request, response=httpx.Response(404, request=request))) == "fetch_failed"
    assert classify_fetch_error(ValueError("x")) == "fetch_failed"


def test_the_fetch_state_snapshot_is_immutable_and_names_each_failure_class(tmp_path):
    conn = _db(tmp_path)
    settings = _settings(watch=("JP225", "US500"))
    request = httpx.Request("GET", "http://bridge")

    def fetch(pair, interval, start, end, *, timeout):
        if pair == "JP225":
            raise httpx.ConnectError("down")
        if pair == "US500":
            raise httpx.HTTPStatusError("nf", request=request,
                                        response=httpx.Response(404, request=request))
        return []

    ingest = Ingest(settings, [_strategy(_ref("JP225"), _ref("US500", alias="u"))],
                    fetch=fetch, monotonic=_Clock())
    ingest.prepare(NOW, conn)
    snapshot = ingest.reference_fetch_state
    assert snapshot[("JP225", "1h")] == "source_unavailable"
    assert snapshot[("US500", "1h")] == "fetch_failed"
    with pytest.raises(TypeError):
        snapshot[("X", "1h")] = "ok"
    assert snapshot == dict(snapshot)
    conn.close()


def test_a_symbol_outside_the_watch_list_is_never_fetched(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    ingest = Ingest(_settings(), [_strategy(_ref())], fetch=lambda *a, **k: [], monotonic=_Clock())
    assert ingest._reference_symbol_allowed("JP225")
    assert ingest._reference_symbol_allowed("USDJPY")
    assert not ingest._reference_symbol_allowed("US500")
    assert not ingest._reference_symbol_allowed("X" * 33)
    called = []
    with monkeypatch.context() as patch:
        patch.setattr(sources, "mt5_raw_bars_range", lambda *a, **k: called.append(a) or [])
        patch.setattr(sources, "mt5_bars_range", lambda *a, **k: called.append(a) or [])
        real = Ingest(_settings(), [_strategy(_ref())], monotonic=_Clock())
        with pytest.raises(ValueError):
            real._fetch_primary("US500", "1h", NOW, NOW, timeout=1)
        real._fetch_primary("JP225", "1h", NOW, NOW, timeout=1)
        real._fetch_primary("USDJPY", "1h", NOW, NOW, timeout=1)
    assert len(called) == 2
    assert isinstance(called[0][1], sources.MT5RawSymbol) and called[1][1] == "USDJPY"
    conn.close()


def test_a_rejected_watch_symbol_makes_no_request_and_is_a_soft_failure(tmp_path):
    conn = _db(tmp_path)
    calls = []
    settings = _settings(watch=("JP225",))
    ingest = Ingest(settings, [_strategy(_ref())],
                    fetch=lambda p, *a, **k: calls.append(p) or [], monotonic=_Clock())
    settings.datafeed.watch_symbols = []
    _, report = ingest.prepare(NOW, conn)
    assert "JP225" not in calls
    assert ("JP225", "1h") in {key for key, _ in report.soft_failed}
    conn.close()


# --- status view -----------------------------------------------------------

def _status_rows(tmp_path, *, fetch, bars=None, now=NOW, strategies=None, steps=1,
                 settings=None):
    from agentic_fx.datafeed.reference_status import reference_rows

    conn = _db(tmp_path)
    settings = settings or _settings()
    ingest = Ingest(settings, strategies or [_strategy(_ref())], fetch=fetch,
                    monotonic=_Clock())
    for _ in range(steps):
        ingest.prepare(now, conn)
        ingest.commit(conn)
    rows = reference_rows(conn, ingest, now,
                          watch_symbols=settings.datafeed.watch_symbols,
                          storage_source="mt5-live")
    conn.close()
    return rows


def _fetch_returning(end_offset_hours, *, exc=None):
    def fetch(pair, interval, start, end, *, timeout):
        if pair == "JP225":
            if exc is not None:
                raise exc
            if end_offset_hours is not None:
                return [Bar("JP225", "1h", NOW - timedelta(hours=end_offset_hours), 1, 1, 1, 1, 1)]
        return []
    return fetch


def test_status_rows_cover_the_six_states(tmp_path):
    request = httpx.Request("GET", "http://b")
    cases = {
        "ok": _fetch_returning(1),
        "stale": _fetch_returning(6),
        "missing": _fetch_returning(None),
        "source_unavailable": _fetch_returning(None, exc=httpx.ConnectError("x")),
        "fetch_failed": _fetch_returning(None, exc=httpx.HTTPStatusError(
            "x", request=request, response=httpx.Response(404, request=request))),
    }
    for index, (state, fetch) in enumerate(cases.items()):
        sub = tmp_path / f"s{index}"
        sub.mkdir()
        [row] = _status_rows(sub, fetch=fetch)
        assert row["state"] == state, (state, row)
        assert (row["symbol"], row["interval"], row["source"]) == ("JP225", "1h", "mt5-live")
        assert (row["age_seconds"] is not None) == (state == "stale")
    sub = tmp_path / "deferred"
    sub.mkdir()
    from agentic_fx.datafeed.reference_status import reference_rows

    conn = _db(sub)
    clock = _Clock()
    ingest = Ingest(_settings(budget=1), [_strategy(_ref())],
                    fetch=lambda *a, **k: (setattr(clock, "t", clock.t + 5), [])[1],
                    monotonic=clock)
    ingest.prepare(NOW, conn)
    [row] = reference_rows(conn, ingest, NOW, watch_symbols=["JP225"], storage_source="mt5-live")
    assert row["state"] == "budget_deferred"
    conn.close()


def test_status_only_lists_watched_symbols(tmp_path):
    rows = _status_rows(tmp_path, fetch=_fetch_returning(1))
    assert [r["symbol"] for r in rows] == ["JP225"]


def test_status_rows_are_not_silently_truncated(tmp_path):
    symbols = tuple(f"REF{i}" for i in range(65))
    refs = tuple(_ref(symbol, alias=f"r{i}") for i, symbol in enumerate(symbols))
    rows = _status_rows(tmp_path, fetch=lambda *a, **k: [],
                        strategies=[_strategy(*refs)], now=NOW,
                        settings=_settings(watch=symbols))
    assert [row["symbol"] for row in rows] == sorted(symbols)


def test_status_uses_the_tightest_max_age_of_the_indicators_reading_the_key(tmp_path):
    tight = IndicatorRef("a", "ema", {}, None, symbol="JP225", timeframe="1h", max_age="2h")
    loose = IndicatorRef("b", "ema", {}, None, symbol="JP225", timeframe="1h", max_age="9h")
    [row] = _status_rows(tmp_path, fetch=_fetch_returning(4),
                         strategies=[_strategy(loose, tight)])
    assert row["state"] == "stale"

