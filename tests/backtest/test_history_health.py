"""共通の履歴検査: 鮮度・充足率・日別の穴・判定不能・無足日一覧。"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction

import pytest

from agentic_fx.backtest import history_health as hh
from agentic_fx.backtest.dataset import HistoryDataset
from agentic_fx.backtest.history_health import (
    HISTORY_GAP, HISTORY_READY, HISTORY_STALE, HISTORY_UNAVAILABLE,
    NoBarsConfigError, assess_pair, check_history, evaluate_history,
    expected_grid, load_no_bars, open_duration)
from agentic_fx.core.market_hours import is_market_open
from tests.backtest.factories import _conn

UTC = timezone.utc
W = timedelta(minutes=5)
GRACE = timedelta(seconds=30)
DS = HistoryDataset("mt5", "5m")
MON = datetime(2026, 11, 2, tzinfo=UTC)               # 冬時間 (NY は 11-01 に戻る)
T_END = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)      # window_end = 11-10 00:00 (完了日のみ)
WIN = (MON, datetime(2026, 11, 10, tzinfo=UTC))


def _times(start, end, *, skip=lambda t: False, width=W):
    out, t = [], start
    while t < end:
        if is_market_open(t) and not skip(t):
            out.append(t)
        t += width
    return out


def _pair(present, *, latest=None, now=T_END, window=WIN, excluded=frozenset(), name="USDJPY",
          width=W):
    present = list(present)
    return assess_pair(
        name, window_start=window[0], window_end=window[1], now=now, width=width,
        grace=GRACE, present_times=present,
        latest_confirmed=latest if latest is not None else (max(present) if present else None),
        excluded_dates=excluded)


# ---- 純関数: 暦 ----------------------------------------------------------

def test_expected_grid_skips_weekend_in_winter_and_summer_closes():
    # 冬: 金 22:00Z 閉場 / 日 22:00Z 開場。夏: 21:00Z。
    winter = expected_grid(datetime(2026, 11, 6, 21, 0, tzinfo=UTC),
                           datetime(2026, 11, 8, 23, 0, tzinfo=UTC),
                           width=W, now=T_END + timedelta(days=9), grace=GRACE)
    assert winter[0] == datetime(2026, 11, 6, 21, 0, tzinfo=UTC)
    assert winter[11] == datetime(2026, 11, 6, 21, 55, tzinfo=UTC)
    assert winter[12] == datetime(2026, 11, 8, 22, 0, tzinfo=UTC)
    assert len(winter) == 12 + 12
    summer = expected_grid(datetime(2026, 7, 24, 20, 0, tzinfo=UTC),
                           datetime(2026, 7, 26, 22, 0, tzinfo=UTC),
                           width=W, now=datetime(2026, 8, 30, tzinfo=UTC), grace=GRACE)
    assert summer[11] == datetime(2026, 7, 24, 20, 55, tzinfo=UTC)
    assert summer[12] == datetime(2026, 7, 26, 21, 0, tzinfo=UTC)
    assert len(summer) == 12 + 12


def test_expected_grid_skips_christmas_and_new_year_holidays():
    grid = expected_grid(datetime(2026, 12, 24, 0, 0, tzinfo=UTC),
                         datetime(2026, 12, 27, 0, 0, tzinfo=UTC),
                         width=W, now=datetime(2027, 1, 30, tzinfo=UTC), grace=GRACE)
    assert all(is_market_open(t) for t in grid)
    assert not any(datetime(2026, 12, 25, 3, 0, tzinfo=UTC) <= t < datetime(2026, 12, 25, 21, 0, tzinfo=UTC)
                   for t in grid)
    assert datetime(2026, 12, 24, 12, 0, tzinfo=UTC) in grid


def test_expected_grid_drops_bars_not_confirmed_at_the_clock():
    now = datetime(2026, 11, 4, 12, 5, 20, tzinfo=UTC)
    end = datetime(2026, 11, 4, 12, 5, tzinfo=UTC)
    grid = expected_grid(datetime(2026, 11, 4, 11, 0, tzinfo=UTC), end,
                         width=W, now=now, grace=GRACE)
    # 12:00 足は 12:05:30 に確定するので T=12:05:20 ではまだ数えない
    assert grid[-1] == datetime(2026, 11, 4, 11, 55, tzinfo=UTC)
    grid2 = expected_grid(datetime(2026, 11, 4, 11, 0, tzinfo=UTC), end,
                          width=W, now=now + timedelta(seconds=10), grace=GRACE)
    assert grid2[-1] == datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def test_expected_grid_excludes_a_no_bars_utc_day_only():
    grid = expected_grid(datetime(2026, 11, 3, 23, 0, tzinfo=UTC),
                         datetime(2026, 11, 5, 1, 0, tzinfo=UTC), width=W,
                         now=T_END, grace=GRACE, excluded_dates=frozenset({date(2026, 11, 4)}))
    assert len(grid) == 12 + 12
    assert all(t.date() != date(2026, 11, 4) for t in grid)


def test_open_duration_counts_open_time_across_weekend_and_wall_gap():
    start = datetime(2026, 11, 6, 20, 0, tzinfo=UTC)          # 金
    end = datetime(2026, 11, 9, 1, 0, tzinfo=UTC)             # 月
    # 金 20:00-22:00 (2h) + 日 22:00 - 月 01:00 (3h)
    assert open_duration(start, end) == timedelta(hours=5)


def test_open_duration_excludes_no_bars_days_and_keeps_seconds():
    start = datetime(2026, 11, 3, 23, 0, 0, tzinfo=UTC)
    end = datetime(2026, 11, 5, 0, 0, 30, tzinfo=UTC)
    assert open_duration(start, end) == timedelta(hours=25, seconds=30)
    assert open_duration(start, end, excluded_dates=frozenset({date(2026, 11, 4)})) \
        == timedelta(hours=1, seconds=30)


# ---- 純関数: 閾値の境界 ---------------------------------------------------

def test_a_full_series_is_ready_with_exact_counts():
    present = _times(*WIN)
    health = _pair(present)
    assert health.reasons == ()
    assert health.market_expected == health.market_present == len(present)
    assert health.missing_days == () and health.worst_day[1] == 0


def test_stale_is_open_time_since_the_latest_confirmed_bar_beyond_24h():
    present = _times(*WIN)
    # 24 開場時間ちょうど (T = latest 足の終端 + 24h、平日内) は合格、1 秒超過で stale
    latest = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
    now = latest + W + timedelta(hours=24)
    exact = _pair(present, latest=latest, now=now + timedelta(0), window=WIN)
    assert HISTORY_STALE not in exact.reasons
    over = _pair(present, latest=latest, now=now + timedelta(seconds=1))
    assert HISTORY_STALE in over.reasons
    assert over.open_gap > timedelta(hours=24)
    assert over.wall_gap == over.open_gap


def test_stale_ignores_weekend_and_known_holiday_hours():
    present = _times(*WIN)
    latest = datetime(2026, 11, 6, 21, 55, tzinfo=UTC)            # 金の最終足
    now = datetime(2026, 11, 8, 23, 0, tzinfo=UTC)                # 日 開場 1h 後
    h = _pair(present, latest=latest, now=now, window=(MON, datetime(2026, 11, 8, 22, 0, tzinfo=UTC)))
    assert HISTORY_STALE not in h.reasons
    assert h.wall_gap > timedelta(hours=48) and h.open_gap == timedelta(hours=1)


def test_fill_ratio_is_exact_at_099():
    start = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
    end = start + 100 * W                                          # 100 本
    now = end + timedelta(hours=1)
    allp = _times(start, end)
    assert len(allp) == 100
    ok = _pair(allp[1:], latest=allp[-1], now=now, window=(start, end))   # 99/100
    assert HISTORY_GAP not in ok.reasons
    assert ok.fill == Fraction(99, 100)
    bad = _pair(allp[2:], latest=allp[-1], now=now, window=(start, end))  # 98/100
    assert HISTORY_GAP in bad.reasons


@pytest.mark.parametrize("width, per_day, limit", [
    (timedelta(minutes=1), 1440, 60), (timedelta(minutes=5), 288, 12),
    (timedelta(minutes=15), 96, 4)])
def test_a_day_missing_more_than_60_minutes_is_a_hole_at_every_base_interval(width, per_day, limit):
    full = _times(*WIN, width=width)
    in_day = [t for t in full if t.date() == date(2026, 11, 4)]
    assert len(in_day) == per_day
    ok = _pair([t for t in full if t not in set(in_day[:limit])], width=width)
    assert ok.reasons == () and ok.worst_day == (date(2026, 11, 4), limit)
    bad = _pair([t for t in full if t not in set(in_day[:limit + 1])], width=width)
    assert bad.reasons == (HISTORY_GAP,)
    assert bad.missing_days == ((date(2026, 11, 4), limit + 1),)
    assert bad.fill > Fraction(99, 100)         # 日別の穴だけで止まる


def test_the_in_progress_day_is_not_judged_by_day_but_counts_in_the_ratio():
    start = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
    end = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    now = end + timedelta(minutes=2)
    full = _times(start, end)
    h = _pair([t for t in full if t not in set(full[:20])], latest=full[-1], now=now,
              window=(start, end))
    assert h.missing_days == ()                           # 進行中の日は日別判定から除く
    assert h.reasons == (HISTORY_GAP,)                     # 比率には反映される


def test_empty_series_is_both_stale_and_gap_in_that_order():
    h = _pair([], latest=None)
    assert h.latest_confirmed is None
    assert h.reasons == (HISTORY_STALE, HISTORY_GAP)


def test_zero_expected_is_unavailable_not_ready():
    start = datetime(2026, 11, 7, 3, 0, tzinfo=UTC)               # 土曜の閉場中
    h = _pair([], latest=None, window=(start, start + timedelta(hours=3)),
              now=start + timedelta(days=1))
    assert h.reasons == (HISTORY_UNAVAILABLE,)


def test_no_bars_day_removes_its_expected_bars_and_its_hole():
    full = _times(*WIN)
    present = [t for t in full if t.date() != date(2026, 11, 4)]
    assert _pair(present).reasons == (HISTORY_GAP,)
    h = _pair(present, excluded=frozenset({date(2026, 11, 4)}))
    assert h.reasons == () and h.market_expected == len(full) - 288


def test_percentages_are_not_rounded():
    # 0.99 に丸まる 0.9895 は不合格
    start = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
    end = start + 190 * W
    allp = _times(start, end)
    assert len(allp) == 190
    h = _pair(allp[2:], latest=allp[-1], now=end + timedelta(hours=1), window=(start, end))
    assert h.fill == Fraction(188, 190)
    assert HISTORY_GAP in h.reasons


# ---- DB 層 -----------------------------------------------------------------

def _insert(conn, symbol, times, *, source="mt5", interval="5m"):
    conn.executemany(
        "INSERT OR IGNORE INTO ohlcv_history (symbol, interval, bar_time, open, high, low, "
        "close, volume, source, spread) VALUES (?,?,?,?,?,?,?,?,?,NULL)",
        [(symbol, interval, t.isoformat(), 148.0, 148.2, 147.9, 148.1, 1.0, source) for t in times])
    conn.commit()


NOW = datetime(2026, 11, 10, 6, 2, 20, tzinfo=UTC)


def _check(conn, pairs=("USDJPY",), *, now=NOW, months=1, no_bars=None, dataset=DS):
    kw = {} if no_bars is None else {"no_bars": no_bars}
    return check_history(conn, dataset=dataset, pairs=pairs, now=now,
                         holdout_months=months, grace=GRACE, **kw)


def _fill_db(conn, symbol="USDJPY", *, until=None, skip=lambda t: False):
    start = datetime(2026, 10, 1, tzinfo=UTC)
    _insert(conn, symbol, _times(start, until or datetime(2026, 11, 10, 6, 0, tzinfo=UTC), skip=skip))


def test_healthy_db_is_ready_and_window_follows_holdout_boundary(tmp_path):
    conn = _conn(tmp_path)
    _fill_db(conn)
    r = _check(conn)
    assert r.verdict == HISTORY_READY and r.reasons == ()
    assert r.holdout_end == datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
    assert r.holdout_start == datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
    p = r.pairs[0]
    assert p.latest_confirmed == datetime(2026, 11, 10, 5, 55, tzinfo=UTC)
    assert p.market_present == p.market_expected > 1000


def test_a_stopped_update_is_stale_and_gap(tmp_path):
    conn = _conn(tmp_path)
    _fill_db(conn, until=datetime(2026, 11, 4, tzinfo=UTC))
    r = _check(conn)
    assert r.verdict == HISTORY_STALE
    assert r.reasons == (HISTORY_STALE, HISTORY_GAP)


def test_rows_of_other_source_interval_offgrid_and_unconfirmed_are_not_counted(tmp_path):
    conn = _conn(tmp_path)
    _fill_db(conn)
    _insert(conn, "USDJPY", [NOW - timedelta(minutes=2)])                         # 形成中 (格子外)
    _insert(conn, "USDJPY", [datetime(2026, 11, 10, 6, 0, tzinfo=UTC)])            # 未確定の足
    _insert(conn, "USDJPY", _times(datetime(2026, 11, 5, tzinfo=UTC),
                                    datetime(2026, 11, 20, tzinfo=UTC)), source="dukascopy")
    _insert(conn, "USDJPY", _times(datetime(2026, 11, 5, tzinfo=UTC),
                                    datetime(2026, 11, 20, tzinfo=UTC)), interval="1m")
    r = _check(conn)
    p = r.pairs[0]
    assert p.latest_confirmed == datetime(2026, 11, 10, 5, 55, tzinfo=UTC)
    assert p.market_present == p.market_expected
    assert p.latest_stored >= p.latest_confirmed


def test_other_source_and_interval_rows_fill_no_hole_and_make_nothing_fresh(tmp_path):
    conn = _conn(tmp_path)
    _fill_db(conn, until=datetime(2026, 11, 4, tzinfo=UTC))
    later = _times(datetime(2026, 11, 4, tzinfo=UTC), datetime(2026, 11, 10, 6, 0, tzinfo=UTC))
    _insert(conn, "USDJPY", later, source="dukascopy")
    _insert(conn, "USDJPY", later, interval="1m")
    r = _check(conn)
    p = r.pairs[0]
    assert HISTORY_STALE in r.reasons and HISTORY_GAP in r.reasons
    assert p.latest_confirmed < datetime(2026, 11, 5, tzinfo=UTC)
    assert p.market_present < p.market_expected - 1000


def test_stale_series_with_newer_off_grid_row_is_not_fresh(tmp_path):
    conn = _conn(tmp_path)
    _fill_db(conn, until=datetime(2026, 11, 4, tzinfo=UTC))
    conn.execute("INSERT INTO ohlcv_history VALUES ('USDJPY','5m',?,1,1,1,1,1,'mt5',NULL)",
                 (datetime(2026, 11, 10, 5, 52, 30, tzinfo=UTC).isoformat(),))
    conn.commit()
    r = _check(conn)
    assert HISTORY_STALE in r.reasons
    assert r.pairs[0].latest_confirmed < datetime(2026, 11, 5, tzinfo=UTC)


def test_every_primary_pair_must_pass_and_reasons_are_kept_per_pair(tmp_path):
    conn = _conn(tmp_path)
    _fill_db(conn, "USDJPY")
    _fill_db(conn, "EURUSD", until=datetime(2026, 11, 4, tzinfo=UTC))
    r = _check(conn, ("USDJPY", "EURUSD"))
    assert r.verdict == HISTORY_STALE
    by = {p.symbol: p for p in r.pairs}
    assert by["USDJPY"].reasons == () and HISTORY_STALE in by["EURUSD"].reasons


def test_a_missing_pair_series_is_not_averaged_away(tmp_path):
    conn = _conn(tmp_path)
    _fill_db(conn, "USDJPY")
    r = _check(conn, ("USDJPY", "GBPUSD"))
    assert r.verdict == HISTORY_STALE and r.reasons == (HISTORY_STALE, HISTORY_GAP)


@pytest.mark.parametrize("kwargs", [
    {"pairs": ()}, {"dataset": HistoryDataset("none", "5m")}, {"months": 0}])
def test_unusable_settings_are_unavailable(tmp_path, kwargs):
    conn = _conn(tmp_path)
    _fill_db(conn)
    r = _check(conn, **kwargs)
    assert r.verdict == HISTORY_UNAVAILABLE and r.reasons == (HISTORY_UNAVAILABLE,)


def test_db_read_failure_is_unavailable(tmp_path):
    conn = _conn(tmp_path)
    _fill_db(conn)
    conn.close()
    r = _check(conn)
    assert r.verdict == HISTORY_UNAVAILABLE


def test_calendar_failure_is_unavailable(tmp_path):
    conn = _conn(tmp_path)
    _fill_db(conn)

    def broken(_t):
        raise ValueError("calendar")
    r = check_history(conn, dataset=DS, pairs=("USDJPY",), now=NOW, holdout_months=1,
                      grace=GRACE, is_open=broken)
    assert r.verdict == HISTORY_UNAVAILABLE


@pytest.mark.parametrize("error", [RuntimeError("zoneinfo"), KeyError("tz"), OSError("io"),
                                   ZeroDivisionError(), AttributeError("x")])
def test_any_calendar_failure_is_unavailable(tmp_path, error):
    conn = _conn(tmp_path)
    _fill_db(conn)

    def broken(_t):
        raise error
    r = check_history(conn, dataset=DS, pairs=("USDJPY",), now=NOW, holdout_months=1,
                      grace=GRACE, is_open=broken)
    assert r.verdict == HISTORY_UNAVAILABLE
    assert r.pairs[0].reasons == (HISTORY_UNAVAILABLE,)


def test_unavailable_outranks_stale_and_gap_in_the_representative_verdict(tmp_path):
    conn = _conn(tmp_path)
    _fill_db(conn, "EURUSD", until=datetime(2026, 11, 4, tzinfo=UTC))
    conn.close()
    assert _check(conn, ("EURUSD",)).verdict == HISTORY_UNAVAILABLE


def test_the_clock_must_be_aware(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="aware"):
        _check(conn, now=NOW.replace(tzinfo=None))


def test_judgement_depends_only_on_the_given_clock(tmp_path):
    conn = _conn(tmp_path)
    now = datetime(2024, 11, 12, 6, 2, 20, tzinfo=UTC)
    _insert(conn, "USDJPY", _times(datetime(2024, 10, 1, tzinfo=UTC),
                                    datetime(2024, 11, 12, 6, 0, tzinfo=UTC)))
    assert _check(conn, now=now).verdict == HISTORY_READY


# ---- 読取 transaction ------------------------------------------------------

def test_reads_use_one_short_snapshot_and_restore_autocommit(tmp_path, monkeypatch):
    path = tmp_path / "s.db"
    from agentic_fx.store.db import connect, init_db
    conn = connect(path)
    init_db(conn)
    _fill_db(conn, "USDJPY")
    _fill_db(conn, "EURUSD")
    other = connect(path)
    real = hh._read_series
    calls = []

    def spy(*a, **k):
        out = real(*a, **k)
        if not calls:
            other.execute("DELETE FROM ohlcv_history WHERE symbol='EURUSD'")
            other.commit()
        calls.append(1)
        return out
    monkeypatch.setattr(hh, "_read_series", spy)
    r = _check(conn, ("USDJPY", "EURUSD"))
    assert r.verdict == HISTORY_READY                    # 2 本目は同じ snapshot を読む
    assert conn.in_transaction is False


def test_a_callers_open_transaction_is_left_open(tmp_path):
    conn = _conn(tmp_path)
    _fill_db(conn)
    conn.execute("BEGIN")
    _check(conn)
    assert conn.in_transaction is True
    conn.rollback()


# ---- 無足日一覧 -------------------------------------------------------------

def _write(tmp_path, text):
    p = tmp_path / "history-no-bars.yaml"
    p.write_text(text, encoding="utf-8")
    return p


ENTRY = """\
entries:
  - source: mt5
    base_interval: 5m
    symbol: USDJPY
    date_utc: 2026-11-04
    reason: broker closed
    approved_by: operator
    approved_at: 2026-11-10T01:02:03Z
"""


def test_missing_file_is_an_empty_list(tmp_path):
    nb = load_no_bars(tmp_path / "absent.yaml")
    assert nb.entries == ()
    assert nb.dates_for("mt5", "5m", "USDJPY") == frozenset()


def test_entry_applies_to_its_own_series_and_day_only(tmp_path):
    nb = load_no_bars(_write(tmp_path, ENTRY))
    assert nb.dates_for("mt5", "5m", "USDJPY") == frozenset({date(2026, 11, 4)})
    assert nb.dates_for("mt5", "5m", "EURUSD") == frozenset()
    assert nb.dates_for("dukascopy", "5m", "USDJPY") == frozenset()
    assert nb.dates_for("mt5", "1m", "USDJPY") == frozenset()


def test_digest_ignores_formatting_but_follows_content(tmp_path):
    a = load_no_bars(_write(tmp_path, ENTRY))
    b = load_no_bars(_write(tmp_path, ENTRY.replace("2026-11-04", '"2026-11-04"') + "\n# c\n"))
    c = load_no_bars(_write(tmp_path, ENTRY.replace("broker closed", "holiday")))
    assert a.digest == b.digest and a.digest != c.digest
    assert load_no_bars(tmp_path / "none.yaml").digest not in (a.digest, c.digest)


def test_a_no_bars_day_lets_a_hole_pass_and_ignoring_it_would_not(tmp_path):
    conn = _conn(tmp_path)
    _fill_db(conn, skip=lambda t: t.date() == date(2026, 11, 4))
    assert _check(conn).verdict == HISTORY_GAP
    nb = load_no_bars(_write(tmp_path, ENTRY))
    r = _check(conn, no_bars=nb)
    assert r.verdict == HISTORY_READY
    assert r.no_bars_digest == nb.digest
    assert [e.date_utc for e in r.applied_no_bars] == [date(2026, 11, 4)]
    assert r.applied_no_bars[0].approved_by == "operator"


def test_no_bars_entry_for_another_pair_does_not_help(tmp_path):
    conn = _conn(tmp_path)
    _fill_db(conn, "EURUSD", skip=lambda t: t.date() == date(2026, 11, 4))
    nb = load_no_bars(_write(tmp_path, ENTRY))             # USDJPY 用
    assert _check(conn, ("EURUSD",), no_bars=nb).verdict == HISTORY_GAP


@pytest.mark.parametrize("text", [
    "entries: nope",
    "- just a list",
    ENTRY.replace("date_utc: 2026-11-04", "date_utc: '2026-11-0?'"),
    ENTRY.replace("date_utc: 2026-11-04", "date_utc: 2026-11-04T10:00:00Z"),
    ENTRY.replace("symbol: USDJPY", "symbol: '*'"),
    ENTRY.replace("symbol: USDJPY", "symbol: ''"),
    ENTRY.replace("reason: broker closed", "reason: ''"),
    ENTRY.replace("approved_by: operator", "approved_by: ''"),
    ENTRY.replace("approved_at: 2026-11-10T01:02:03Z", "approved_at: 2026-11-10T01:02:03"),
    ENTRY.replace("base_interval: 5m", "base_interval: 7m"),
    ENTRY.replace("source: mt5", "source: nowhere"),
    ENTRY + "    extra: 1\n",
    ENTRY + ENTRY.split("entries:\n")[1],                       # 同じ系列・日の重複
    ENTRY.replace("date_utc: 2026-11-04", "date_utc: 2026-11-04\n    to: 2026-11-09"),
    "entries: [\n",
])
def test_a_malformed_list_is_a_config_error(tmp_path, text):
    with pytest.raises(NoBarsConfigError):
        load_no_bars(_write(tmp_path, text))


@pytest.mark.parametrize("text", [
    "", "   \n", "null\n", "~", "# only a comment\n", "entries:\n", "entries: null\n",
    ENTRY.replace("source: mt5", "source: [mt5]"),
    ENTRY.replace("source: mt5", "source: {a: b}"),
    ENTRY.replace("base_interval: 5m", "base_interval: [5m]"),
    ENTRY.replace("symbol: USDJPY", "symbol: [USDJPY]"),
    ENTRY.replace("date_utc: 2026-11-04", "date_utc: [2026-11-04]"),
    ENTRY.replace("reason: broker closed", "reason: [x]"),
    ENTRY.replace("approved_by: operator", "approved_by: 7"),
    ENTRY.replace("approved_at: 2026-11-10T01:02:03Z", "approved_at: [1]"),
    ENTRY.replace("source: mt5", "source: 3"),
])
def test_empty_null_and_wrongly_typed_documents_are_unavailable_through_evaluate(tmp_path, text):
    conn = _conn(tmp_path)
    _fill_db(conn)
    p = _write(tmp_path, text)
    with pytest.raises(NoBarsConfigError):
        load_no_bars(p)
    r = evaluate_history(conn, dataset=DS, pairs=("USDJPY",), now=NOW, holdout_months=1,
                         grace=GRACE, no_bars_path=p)
    assert r.verdict == HISTORY_UNAVAILABLE


def test_an_explicit_empty_entries_list_is_a_valid_empty_list(tmp_path):
    assert load_no_bars(_write(tmp_path, "entries: []\n")).entries == ()


@pytest.mark.parametrize("text", [
    "entries: []\nentries: []\n",                                         # トップレベル
    ENTRY.replace("date_utc: 2026-11-04", "date_utc: 2026-11-04\n    date_utc: 2026-11-05"),
    ENTRY.replace("reason: broker closed", "reason: a\n    reason: b"),
    "entries:\n  - {source: mt5, source: mt5}\n",
])
def test_duplicate_yaml_keys_are_rejected_and_unavailable(tmp_path, text):
    conn = _conn(tmp_path)
    _fill_db(conn)
    p = _write(tmp_path, text)
    with pytest.raises(NoBarsConfigError):
        load_no_bars(p)
    r = evaluate_history(conn, dataset=DS, pairs=("USDJPY",), now=NOW, holdout_months=1,
                         grace=GRACE, no_bars_path=p)
    assert r.verdict == HISTORY_UNAVAILABLE


def test_unreadable_list_is_unavailable_not_empty(tmp_path):
    conn = _conn(tmp_path)
    _fill_db(conn)
    bad = _write(tmp_path, "entries: nope")
    r = evaluate_history(conn, dataset=DS, pairs=("USDJPY",), now=NOW, holdout_months=1,
                         grace=GRACE, no_bars_path=bad)
    assert r.verdict == HISTORY_UNAVAILABLE
    ok = evaluate_history(conn, dataset=DS, pairs=("USDJPY",), now=NOW, holdout_months=1,
                          grace=GRACE, no_bars_path=tmp_path / "absent.yaml")
    assert ok.verdict == HISTORY_READY


def test_a_directory_in_place_of_the_list_is_unavailable(tmp_path):
    conn = _conn(tmp_path)
    _fill_db(conn)
    d = tmp_path / "d"
    d.mkdir()
    r = evaluate_history(conn, dataset=DS, pairs=("USDJPY",), now=NOW, holdout_months=1,
                         grace=GRACE, no_bars_path=d)
    assert r.verdict == HISTORY_UNAVAILABLE


# ---- 証跡 -------------------------------------------------------------------

def test_evidence_has_the_agreed_shape_and_is_json_serializable(tmp_path):
    conn = _conn(tmp_path)
    _fill_db(conn)
    nb = load_no_bars(_write(tmp_path, ENTRY))
    ev = _check(conn, no_bars=nb).to_operator_evidence()
    assert json.loads(json.dumps(ev)) == ev
    assert ev["version"] == hh.GUARD_VERSION
    assert ev["verdict"] == "ready"
    assert ev["dataset"] == {"source": "mt5", "base_interval": "5m"}
    assert ev["pairs"] == ["USDJPY"]
    assert ev["checked_at"] == NOW.isoformat()
    assert ev["holdout_start"] == "2026-10-10T06:00:00+00:00"
    assert ev["holdout_end"] == "2026-11-10T06:00:00+00:00"
    assert ev["latest_confirmed_bar"] == {"USDJPY": "2026-11-10T05:55:00+00:00"}
    assert ev["market_present"]["USDJPY"] == ev["market_expected"]["USDJPY"]
    assert ev["missing_days"] == {"USDJPY": []}
    assert ev["worst_day"]["USDJPY"]["missing"] == 0
    assert ev["calendar_version"] == hh.CALENDAR_VERSION
    assert ev["threshold_snapshot"] == {
        "stale_open_hours": 24, "min_fill_ratio": "99/100", "max_day_missing_minutes": 60}
    assert ev["no_bars_digest"] == nb.digest


def test_operator_evidence_keeps_the_applied_no_bars_days_with_reason_and_approver(tmp_path):
    conn = _conn(tmp_path)
    _fill_db(conn, skip=lambda t: t.date() == date(2026, 11, 4))
    nb = load_no_bars(_write(tmp_path, ENTRY))
    ev = _check(conn, no_bars=nb).to_operator_evidence()
    assert ev["verdict"] == "ready"
    assert ev["no_bars_applied"] == [{
        "source": "mt5", "base_interval": "5m", "symbol": "USDJPY", "date_utc": "2026-11-04",
        "reason": "broker closed", "approved_by": "operator",
        "approved_at": "2026-11-10T01:02:03+00:00"}]
    assert json.loads(json.dumps(ev)) == ev


def test_the_evidence_is_only_offered_under_an_operator_name():
    assert not hasattr(hh.HistoryHealth, "to_evidence")
    assert hh.HistoryHealth.to_operator_evidence.__doc__ and "agent" in hh.HistoryHealth.to_operator_evidence.__doc__


def test_thresholds_are_fixed_constants():
    assert hh.STALE_OPEN_HOURS == 24
    assert hh.MIN_FILL_RATIO == Fraction(99, 100)
    assert hh.MAX_DAY_MISSING == timedelta(minutes=60)
