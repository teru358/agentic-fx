"""通常取得の固定窓 (水位の 10 分前から) と、後着足の判別・保存。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.ingest import LATE_WINDOW, Ingest
from agentic_fx.store import outage_facts
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.ohlcv import load_cache_bars, upsert_cache_bars

NOW = datetime(2026, 9, 16, 12, 5, tzinfo=timezone.utc)  # Wednesday
SRC = "yfinance"
KEY_1M = ("USDJPY", "1m")


def _settings(intervals=("1m", "1h")):
    return SimpleNamespace(
        pairs=["USDJPY"],
        datafeed=SimpleNamespace(primary=SRC, intervals=list(intervals),
                                 primary_intervals=["1h"], ingest_budget_sec=10,
                                 closed_bar_grace_sec=0))


def _m(minute, hour=12):
    return datetime(2026, 9, 16, hour, minute, tzinfo=timezone.utc)


def _bar(ts, interval="1m", close=1.0):
    return Bar("USDJPY", interval, ts, close, close, close, close, 1)


def _seed(conn, times, interval="1m", source=SRC):
    upsert_cache_bars(conn, [_bar(t, interval) for t in times], source=source)


def _db(tmp_path):
    conn = connect(tmp_path / "late.db")
    init_db(conn)
    return conn


def _recording_fetch(responses):
    calls = []

    def fetch(pair, interval, start, end, *, timeout):
        calls.append((interval, start, end))
        return list(responses.get(interval, []))

    return fetch, calls


def test_window_for_hard_1m_starts_ten_minutes_before_the_watermark(tmp_path):
    assert LATE_WINDOW == timedelta(minutes=10)
    conn = _db(tmp_path)
    _seed(conn, [_m(0), _m(1), _m(2), _m(3)])
    _seed(conn, [_m(0, 11)], interval="1h")
    fetch, calls = _recording_fetch({})
    Ingest(_settings(), fetch=fetch).prepare(NOW, conn)
    by_interval = {interval: (start, end) for interval, start, end in calls}
    assert by_interval["1m"] == (_m(3) - timedelta(minutes=10), NOW)
    # 他の key の下限は現行どおり水位の 1 本前
    assert by_interval["1h"][0] == _m(0, 11) - timedelta(hours=1)


def test_cold_start_keeps_the_planner_window_and_reports_no_late_bars(tmp_path):
    conn = _db(tmp_path)
    fetch, calls = _recording_fetch({"1m": [_bar(_m(2)), _bar(_m(3))]})
    ingest = Ingest(_settings(("1m",)), fetch=fetch)
    _, report = ingest.prepare(NOW, conn)
    start = {interval: s for interval, s, _ in calls}["1m"]
    assert start < NOW - timedelta(hours=12)
    assert report.late_bars == ()
    assert dict(report.pre_watermarks)[KEY_1M] is None


def test_prepare_reports_late_bars_without_writing(tmp_path):
    conn = _db(tmp_path)
    _seed(conn, [_m(0), _m(1), _m(3)])
    fetch, _ = _recording_fetch({"1m": [_bar(_m(1)), _bar(_m(2), close=2.0),
                                        _bar(_m(3)), _bar(_m(4))]})
    ingest = Ingest(_settings(("1m",)), fetch=fetch)
    _, report = ingest.prepare(NOW, conn)
    assert dict(report.pre_watermarks)[KEY_1M] == _m(3)
    assert [(b.ts, b.close) for b in report.late_bars] == [(_m(2), 2.0)]
    assert [b.ts for b in load_cache_bars(conn, "USDJPY", "1m", source=SRC)] == [_m(0), _m(1), _m(3)]
    assert outage_facts.list_late_bars(conn, SRC) == []
    assert not conn.in_transaction


def test_a_cache_row_of_another_source_does_not_hide_a_late_bar(tmp_path):
    conn = _db(tmp_path)
    _seed(conn, [_m(0), _m(3)])
    _seed(conn, [_m(2)], source="mt5-live")
    fetch, _ = _recording_fetch({"1m": [_bar(_m(2)), _bar(_m(3))]})
    _, report = Ingest(_settings(("1m",)), fetch=fetch).prepare(NOW, conn)
    assert [b.ts for b in report.late_bars] == [_m(2)]


def test_nothing_new_and_nothing_missing_reports_no_late_bars(tmp_path):
    conn = _db(tmp_path)
    _seed(conn, [_m(1), _m(2), _m(3)])
    fetch, _ = _recording_fetch({"1m": [_bar(_m(1)), _bar(_m(2)), _bar(_m(3))]})
    _, report = Ingest(_settings(("1m",)), fetch=fetch).prepare(NOW, conn)
    assert report.late_bars == ()
    assert KEY_1M in report.succeeded


def test_a_failed_request_reports_no_late_bars(tmp_path):
    conn = _db(tmp_path)
    _seed(conn, [_m(0), _m(3)])

    def fetch(pair, interval, start, end, *, timeout):
        raise OSError("offline")

    _, report = Ingest(_settings(("1m",)), fetch=fetch).prepare(NOW, conn)
    assert report.late_bars == ()
    assert KEY_1M not in dict(report.pre_watermarks)


def test_commit_saves_cache_and_late_bars_in_one_transaction(tmp_path):
    conn = _db(tmp_path)
    _seed(conn, [_m(0), _m(3)])
    fetch, _ = _recording_fetch({"1m": [_bar(_m(2)), _bar(_m(3)), _bar(_m(4))]})
    ingest = Ingest(_settings(("1m",)), fetch=fetch)
    ingest.prepare(NOW, conn)
    assert ingest.commit(conn) == 3
    assert not conn.in_transaction
    assert [b.ts for b in load_cache_bars(conn, "USDJPY", "1m", source=SRC)] == [
        _m(0), _m(2), _m(3), _m(4)]
    assert [(b.ts, at) for b, at in outage_facts.list_late_bars(conn, SRC)] == [(_m(2), NOW)]
    # 同じ窓を読み直しても、保存済みの時刻はもう後着ではない
    ingest.prepare(NOW + timedelta(seconds=61), conn)
    ingest.commit(conn)
    assert len(outage_facts.list_late_bars(conn, SRC)) == 1


def test_a_failure_while_saving_the_late_bars_rolls_the_cache_back_too(tmp_path):
    conn = _db(tmp_path)
    _seed(conn, [_m(0), _m(3)])
    conn.execute("CREATE TRIGGER fail_late BEFORE INSERT ON datafeed_late_bars "
                 "BEGIN SELECT RAISE(ABORT, 'late save failed'); END")
    conn.commit()
    fetch, _ = _recording_fetch({"1m": [_bar(_m(2)), _bar(_m(3)), _bar(_m(4))]})
    ingest = Ingest(_settings(("1m",)), fetch=fetch)
    ingest.prepare(NOW, conn)
    with pytest.raises(Exception, match="late save failed"):
        ingest.commit(conn)
    assert not conn.in_transaction
    assert [b.ts for b in load_cache_bars(conn, "USDJPY", "1m", source=SRC)] == [_m(0), _m(3)]
    assert outage_facts.list_late_bars(conn, SRC) == []


def test_an_uncommitted_late_list_is_replaced_by_the_next_prepare(tmp_path):
    conn = _db(tmp_path)
    _seed(conn, [_m(0), _m(3)])
    fetch, _ = _recording_fetch({"1m": [_bar(_m(2)), _bar(_m(3))]})
    ingest = Ingest(_settings(("1m",)), fetch=fetch)
    ingest.prepare(NOW, conn)
    later = NOW + timedelta(minutes=30)
    ingest.prepare(later, conn)
    ingest.commit(conn)
    assert [(b.ts, at) for b, at in outage_facts.list_late_bars(conn, SRC)] == [(_m(2), later)]


def test_commit_without_a_prepared_late_bar_leaves_the_late_table_empty(tmp_path):
    conn = _db(tmp_path)
    _seed(conn, [_m(1), _m(2), _m(3)])
    fetch, _ = _recording_fetch({"1m": [_bar(_m(2)), _bar(_m(3)), _bar(_m(4))]})
    ingest = Ingest(_settings(("1m",)), fetch=fetch)
    ingest.prepare(NOW, conn)
    ingest.commit(conn)
    assert outage_facts.list_late_bars(conn, SRC) == []
