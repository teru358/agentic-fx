"""refresh 後の冪等 pass (supersede・set の作成と cancelled 化)・window_view・鮮度。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.core.econ_window import WindowSettings
from agentic_fx.datafeed.econ_calendar import EconCalendar
from agentic_fx.store import econ_events, fetch_attempts, ohlcv_slices
from agentic_fx.store.db import connect, init_db

UTC = timezone.utc
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)         # 水曜
T1 = datetime(2026, 10, 30, 12, 30, tzinfo=UTC)         # 金曜 12:30 UTC
PAIRS = {"USDJPY": ("USD", "JPY"), "EURUSD": ("EUR", "USD")}
US = timedelta(microseconds=1)


@dataclass
class Clock:
    at: datetime

    def now(self) -> datetime:
        return self.at


def ff(title, country, when, impact="High"):
    return {"title": title, "country": country, "impact": impact,
            "date": when.isoformat(), "forecast": "", "previous": ""}


def resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status = MagicMock()
    return r


def make(tmp_path, *, clock=None, pairs=PAIRS, window=None, name="t.db"):
    conn = connect(tmp_path / name)
    init_db(conn)
    clock = clock or Clock(NOW)
    cal = EconCalendar(conn, ActivityLog(tmp_path / "a.log"), clock,
                       timeout_sec=10, window=window,
                       pair_currencies=pairs)
    return cal, clock


def refresh(cal, payload):
    with patch("httpx.get", return_value=resp(payload)):
        return cal.refresh()


def sets(cal):
    return [tuple(r) for r in cal.conn.execute(
        "SELECT e.name, s.pair, s.state FROM ohlcv_slice_sets s "
        "JOIN econ_events e ON e.id=s.event_id ORDER BY s.id")]


def event_id(cal, name, ts=None):
    sql = "SELECT id FROM econ_events WHERE name=?"
    args = [name]
    if ts is not None:
        sql += " AND ts=?"
        args.append(ts.isoformat())
    return cal.conn.execute(sql, args).fetchone()[0]


# --- refresh 後 pass: set の作成 -------------------------------------------

def test_refresh_creates_one_awaiting_set_per_target_event_and_pair(tmp_path):
    cal, _ = make(tmp_path)
    payload = [
        ff("US CPI", "USD", T1),
        ff("JP CPI", "JPY", T1 + timedelta(hours=1)),
        ff("EU CPI", "EUR", T1 + timedelta(hours=2)),
        ff("CA CPI", "CAD", T1 + timedelta(hours=3)),
        ff("US Retail", "USD", T1 + timedelta(hours=4), impact="Medium"),
        ff("Holiday", "All", T1 + timedelta(hours=5)),
    ]
    assert refresh(cal, payload) == 6
    assert sorted(sets(cal)) == sorted([
        ("US CPI", "USDJPY", "awaiting"), ("US CPI", "EURUSD", "awaiting"),
        ("JP CPI", "USDJPY", "awaiting"), ("EU CPI", "EURUSD", "awaiting"),
        ("Holiday", "USDJPY", "awaiting"), ("Holiday", "EURUSD", "awaiting")])
    assert cal.last_pass is not None and len(cal.last_pass.sync.created) == 6


def test_the_pass_is_idempotent_a_second_refresh_adds_nothing(tmp_path):
    cal, _ = make(tmp_path)
    payload = [ff("US CPI", "USD", T1)]
    refresh(cal, payload)
    first = sets(cal)
    assert refresh(cal, payload) == 1
    assert sets(cal) == first
    assert cal.last_pass.sync.created == ()
    assert cal.last_pass.sync.cancelled == () and cal.last_pass.superseded == ()
    cal.sync_sets()
    assert sets(cal) == first


def test_sets_follow_the_importance_min_setting(tmp_path):
    cal, _ = make(tmp_path, window=WindowSettings(2, 30, 15))
    refresh(cal, [ff("US Retail", "USD", T1, impact="Medium"),
                  ff("US Low", "USD", T1 + timedelta(hours=1), impact="Low")])
    assert {n for n, _, _ in sets(cal)} == {"US Retail"}


def test_past_events_in_the_store_also_get_sets(tmp_path):
    # 元足が保持範囲に残る過去 event も同じ規則で退避する
    cal, _ = make(tmp_path)
    refresh(cal, [ff("Old CPI", "USD", NOW - timedelta(days=3))])
    assert ("Old CPI", "USDJPY", "awaiting") in sets(cal)


# --- supersede → cancelled -------------------------------------------------

def test_a_moved_event_supersedes_the_old_row_and_cancels_its_set(tmp_path):
    cal, _ = make(tmp_path)
    refresh(cal, [ff("US CPI", "USD", T1)])
    old = event_id(cal, "US CPI", T1)
    assert sets(cal) == [("US CPI", "USDJPY", "awaiting"),
                         ("US CPI", "EURUSD", "awaiting")]
    moved = T1 + timedelta(hours=2)
    refresh(cal, [ff("US CPI", "USD", moved)])
    new = event_id(cal, "US CPI", moved)
    assert cal.conn.execute("SELECT superseded_by FROM econ_events WHERE id=?",
                            (old,)).fetchone()[0] == new
    assert cal.last_pass.superseded == ((old, new),)
    assert len(cal.last_pass.sync.cancelled) == 2
    assert sorted(sets(cal)) == sorted([
        ("US CPI", "USDJPY", "cancelled"), ("US CPI", "EURUSD", "cancelled"),
        ("US CPI", "USDJPY", "awaiting"), ("US CPI", "EURUSD", "awaiting")])
    states = {r[0]: r[1] for r in cal.conn.execute(
        "SELECT event_id, state FROM ohlcv_slice_sets WHERE pair='USDJPY'")}
    assert states == {old: "cancelled", new: "awaiting"}


def test_a_set_that_was_staging_is_cancelled_when_its_event_is_superseded(tmp_path):
    cal, _ = make(tmp_path)
    refresh(cal, [ff("US CPI", "USD", T1)])
    gen = ohlcv_slices.new_generation(cal.conn, now=NOW)
    cal.conn.execute("UPDATE ohlcv_slice_sets SET state='staging', generation=?",
                     (gen,))
    cal.conn.commit()
    refresh(cal, [ff("US CPI", "USD", T1 + timedelta(hours=1))])
    assert [s for n, p, s in sets(cal) if p == "USDJPY"][0] == "cancelled"


def test_a_released_event_is_not_replaced_by_a_next_instance(tmp_path):
    cal, clock = make(tmp_path)
    refresh(cal, [ff("Claims", "USD", NOW - timedelta(hours=1))])
    old = event_id(cal, "Claims")
    refresh(cal, [ff("Claims", "USD", NOW + timedelta(days=6))])
    assert cal.conn.execute("SELECT superseded_by FROM econ_events WHERE id=?",
                            (old,)).fetchone()[0] is None
    assert cal.last_pass.superseded == ()


def test_same_name_instances_in_one_fetch_all_stay(tmp_path):
    cal, _ = make(tmp_path)
    refresh(cal, [ff("Speech", "USD", T1)])
    refresh(cal, [ff("Speech", "USD", T1), ff("Speech", "USD", T1 + timedelta(days=1))])
    assert cal.last_pass.superseded == ()
    assert cal.conn.execute(
        "SELECT COUNT(*) FROM econ_events WHERE superseded_by IS NOT NULL"
    ).fetchone()[0] == 0


def test_feed_flip_back_revives_the_old_row_and_supersedes_the_other(tmp_path):
    cal, _ = make(tmp_path)
    moved = T1 + timedelta(hours=2)
    refresh(cal, [ff("US CPI", "USD", T1)])
    refresh(cal, [ff("US CPI", "USD", moved)])
    refresh(cal, [ff("US CPI", "USD", T1)])
    a, b = event_id(cal, "US CPI", T1), event_id(cal, "US CPI", moved)
    sup = dict(cal.conn.execute("SELECT id, superseded_by FROM econ_events"))
    assert sup == {a: None, b: a}
    assert cal.last_pass.superseded == ((b, a),)
    by_event = {(r[0], r[1]): r[2] for r in cal.conn.execute(
        "SELECT event_id, pair, state FROM ohlcv_slice_sets")}
    assert by_event[(a, "USDJPY")] == "awaiting"      # 再有効化 (slice 行なし)
    assert by_event[(b, "USDJPY")] == "cancelled"
    assert cal.last_pass.sync.reactivated


# --- 設定由来: cancelled 化と再有効化 -------------------------------------

def test_lowering_importance_cancels_and_raising_it_back_reactivates(tmp_path):
    cal, _ = make(tmp_path, window=WindowSettings(2, 30, 15))
    refresh(cal, [ff("US Retail", "USD", T1, impact="Medium")])
    assert {s for _, _, s in sets(cal)} == {"awaiting"}
    strict, _ = make(tmp_path, window=WindowSettings(3, 30, 15), name="t.db")
    result = strict.sync_sets()
    assert len(result.cancelled) == 2 and {s for _, _, s in sets(strict)} == {"cancelled"}
    loose, _ = make(tmp_path, window=WindowSettings(2, 30, 15), name="t.db")
    result = loose.sync_sets()
    assert len(result.reactivated) == 2
    assert {s for _, _, s in sets(loose)} == {"awaiting"}


def test_a_removed_pair_cancels_its_sets_and_a_readded_pair_reactivates_them(tmp_path):
    cal, _ = make(tmp_path)
    refresh(cal, [ff("US CPI", "USD", T1)])
    only_jpy, _ = make(tmp_path, pairs={"USDJPY": ("USD", "JPY")}, name="t.db")
    only_jpy.sync_sets()
    assert dict(((n, p), s) for n, p, s in sets(only_jpy)) == {
        ("US CPI", "USDJPY"): "awaiting", ("US CPI", "EURUSD"): "cancelled"}
    both, _ = make(tmp_path, name="t.db")
    result = both.sync_sets()
    assert len(result.reactivated) == 1
    assert {s for _, _, s in sets(both)} == {"awaiting"}


def test_a_cancelled_set_with_slice_rows_is_deferred_not_reactivated(tmp_path):
    cal, _ = make(tmp_path, window=WindowSettings(2, 30, 15))
    refresh(cal, [ff("US Retail", "USD", T1, impact="Medium")])
    set_id = cal.conn.execute("SELECT id FROM ohlcv_slice_sets LIMIT 1").fetchone()[0]
    strict, _ = make(tmp_path, name="t.db")
    strict.sync_sets()
    cal.conn.execute(
        "INSERT INTO ohlcv_slices (set_id,bar_time,open,high,low,close,volume,"
        "source,quality) VALUES (?,?,?,?,?,?,?,?,?)",
        (set_id, (T1 - timedelta(minutes=15)).isoformat(), 1, 1, 1, 1, 0,
         "mt5-live", "observed"))
    cal.conn.commit()
    loose, _ = make(tmp_path, window=WindowSettings(2, 30, 15), name="t.db")
    result = loose.sync_sets()
    assert set_id in result.deferred and set_id not in result.reactivated


# --- 保存の途中失敗・pass の失敗 -------------------------------------------

def test_a_failed_store_runs_no_pass_and_the_next_refresh_catches_up(tmp_path):
    cal, _ = make(tmp_path)
    payload = [ff("A", "USD", T1), ff("B", "USD", T1 + timedelta(hours=1))]
    real = econ_events.upsert
    calls = {"n": 0}

    def flaky(conn, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite_error()
        return real(conn, **kw)

    with patch("agentic_fx.datafeed.econ_calendar.econ_events.upsert", flaky):
        assert refresh(cal, payload) == 1          # 保存できた件数
    assert sets(cal) == []                          # pass は走らない
    assert cal.last_pass is None
    assert refresh(cal, payload) == 2
    assert len(sets(cal)) == 4                      # A, B × 2 pair


def sqlite_error():
    import sqlite3
    return sqlite3.OperationalError("disk I/O error")


def test_a_failing_pass_does_not_fail_the_refresh_and_is_recorded(tmp_path, caplog):
    cal, _ = make(tmp_path)
    with patch("agentic_fx.datafeed.econ_calendar.ohlcv_slices.sync_event_sets",
               side_effect=RuntimeError("boom")), \
         caplog.at_level("WARNING", logger="agentic_fx.econ"):
        assert refresh(cal, [ff("A", "USD", T1)]) == 1
    tail = "\n".join(cal.activity.tail(10))
    assert "\tNEWS\tecon_refresh_failed\tsets: RuntimeError: boom\t" in tail
    assert "econ_refreshed" in tail
    assert "refresh pass failed" in caplog.text
    assert cal.freshness() == "fresh"             # 取得と保存は成功している
    assert cal.last_pass is None
    refresh(cal, [ff("A", "USD", T1)])             # 次の refresh で補われる
    assert len(sets(cal)) == 2


def test_begin_that_yields_skips_the_pass_without_failing(tmp_path):
    cal, _ = make(tmp_path)
    cal.begin = lambda conn: False
    assert refresh(cal, [ff("A", "USD", T1)]) == 1
    assert sets(cal) == [] and cal.last_pass is None
    cal.begin = ohlcv_slices.begin_immediate
    refresh(cal, [ff("A", "USD", T1)])
    assert len(sets(cal)) == 2


def _moved(cal):
    """A を保存して set を作り、B へ動かした refresh の前まで進める。"""
    refresh(cal, [ff("US CPI", "USD", T1)])
    old = event_id(cal, "US CPI", T1)
    return old, T1 + timedelta(hours=2)


def _superseded(cal, row_id):
    return cal.conn.execute("SELECT superseded_by FROM econ_events WHERE id=?",
                            (row_id,)).fetchone()[0]


def _cancelled_for(cal, row_id):
    return [r[0] for r in cal.conn.execute(
        "SELECT state FROM ohlcv_slice_sets WHERE event_id=?", (row_id,))]


def _pending(cal):
    return cal.conn.execute(
        "SELECT COUNT(*) FROM alert_state WHERE key=?",
        (econ_events.PENDING_SUPERSEDE_KEY,)).fetchone()[0]


def test_a_supersede_that_yielded_is_retried_by_the_next_refresh(tmp_path):
    cal, _ = make(tmp_path)
    old, moved = _moved(cal)
    cal.begin = lambda conn: False
    refresh(cal, [ff("US CPI", "USD", moved)])
    new = event_id(cal, "US CPI", moved)
    assert _superseded(cal, old) is None and _pending(cal) == 1
    assert cal.last_pass is None
    cal.begin = ohlcv_slices.begin_immediate
    refresh(cal, [ff("US CPI", "USD", moved)])      # B はもう新規ではない
    assert _superseded(cal, old) == new and _pending(cal) == 0
    assert cal.last_pass.superseded == ((old, new),)
    assert _cancelled_for(cal, old) == ["cancelled", "cancelled"]
    assert _cancelled_for(cal, new) == ["awaiting", "awaiting"]


def test_a_supersede_that_raised_is_retried_by_the_next_refresh(tmp_path):
    cal, _ = make(tmp_path)
    old, moved = _moved(cal)
    with patch("agentic_fx.datafeed.econ_calendar.econ_events."
               "supersede_pending_tx", side_effect=sqlite_error()):
        assert refresh(cal, [ff("US CPI", "USD", moved)]) == 1
    new = event_id(cal, "US CPI", moved)
    assert _superseded(cal, old) is None and _pending(cal) == 1
    refresh(cal, [ff("US CPI", "USD", moved)])
    assert _superseded(cal, old) == new and _pending(cal) == 0
    assert _cancelled_for(cal, old) == ["cancelled", "cancelled"]


def test_a_failure_inside_the_supersede_transaction_keeps_the_pending(tmp_path):
    cal, _ = make(tmp_path)
    old, moved = _moved(cal)
    with patch("agentic_fx.store.econ_events.supersede_in_tx",
               side_effect=sqlite_error()):
        refresh(cal, [ff("US CPI", "USD", moved)])
    assert _superseded(cal, old) is None and _pending(cal) == 1
    refresh(cal, [ff("US CPI", "USD", moved)])
    assert _superseded(cal, old) is not None and _pending(cal) == 0


def test_the_startup_top_up_retries_a_pending_supersede(tmp_path):
    cal, _ = make(tmp_path)
    old, moved = _moved(cal)
    cal.begin = lambda conn: False
    refresh(cal, [ff("US CPI", "USD", moved)])
    assert _superseded(cal, old) is None
    # 再起動: 新しい EconCalendar が同じ DB で sync_sets (起動時補完) を呼ぶ
    cal2 = EconCalendar(cal.conn, cal.activity, cal.clock, timeout_sec=10,
                        pair_currencies=PAIRS)
    sync = cal2.sync_sets()
    new = event_id(cal2, "US CPI", moved)
    assert _superseded(cal2, old) == new and _pending(cal2) == 0
    assert len(sync.cancelled) == 2
    assert _cancelled_for(cal2, old) == ["cancelled", "cancelled"]


def test_top_up_that_cannot_supersede_keeps_the_pending_and_makes_no_sets(tmp_path):
    cal, _ = make(tmp_path)
    old, moved = _moved(cal)
    cal.begin = lambda conn: False
    refresh(cal, [ff("US CPI", "USD", moved)])
    assert cal.sync_sets() is None
    assert _pending(cal) == 1 and _superseded(cal, old) is None


def test_pending_work_accumulates_across_unfinished_refreshes(tmp_path):
    cal, _ = make(tmp_path)
    refresh(cal, [ff("A", "USD", T1), ff("B", "EUR", T1)])
    a, b = event_id(cal, "A"), event_id(cal, "B")
    cal.begin = lambda conn: False
    refresh(cal, [ff("A", "USD", T1 + timedelta(hours=1)), ff("B", "EUR", T1)])
    refresh(cal, [ff("A", "USD", T1 + timedelta(hours=1)),
                  ff("B", "EUR", T1 + timedelta(hours=2))])
    cal.begin = ohlcv_slices.begin_immediate
    cal.sync_sets()
    assert _superseded(cal, a) == event_id(cal, "A", T1 + timedelta(hours=1))
    assert _superseded(cal, b) == event_id(cal, "B", T1 + timedelta(hours=2))
    assert _pending(cal) == 0


def test_an_unreadable_pending_is_dropped_with_a_warning(tmp_path, caplog):
    cal, _ = make(tmp_path)
    cal.conn.execute(
        "INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?)",
        (econ_events.PENDING_SUPERSEDE_KEY, "{broken", NOW.isoformat()))
    cal.conn.commit()
    with caplog.at_level("WARNING", logger="agentic_fx.econ"):
        refresh(cal, [ff("A", "USD", T1)])
    assert "pending supersede unreadable" in caplog.text
    assert _pending(cal) == 0 and len(sets(cal)) == 2


def test_fetch_failure_runs_no_pass(tmp_path):
    cal, _ = make(tmp_path)
    with patch("httpx.get", side_effect=OSError("down")):
        assert cal.refresh() == 0
    assert cal.last_pass is None and sets(cal) == []


def test_without_pair_currencies_refresh_still_supersedes_but_makes_no_sets(tmp_path):
    cal = EconCalendar(*_bare(tmp_path), timeout_sec=10)
    refresh(cal, [ff("US CPI", "USD", T1)])
    moved = T1 + timedelta(hours=1)
    refresh(cal, [ff("US CPI", "USD", moved)])
    assert cal.conn.execute("SELECT COUNT(*) FROM ohlcv_slice_sets").fetchone()[0] == 0
    assert cal.conn.execute(
        "SELECT COUNT(*) FROM econ_events WHERE superseded_by IS NOT NULL"
    ).fetchone()[0] == 1
    assert cal.sync_sets() is None
    assert cal.last_pass.sync is None
    with pytest.raises(ValueError):
        cal.window_view("USDJPY")


def _bare(tmp_path):
    conn = connect(tmp_path / "bare.db")
    init_db(conn)
    return conn, ActivityLog(tmp_path / "a.log"), Clock(NOW)


# --- window_view ------------------------------------------------------------

def test_window_view_shows_only_the_usd_importance_3_event_for_usdjpy(tmp_path):
    cal, clock = make(tmp_path)
    refresh(cal, [ff("BoC Rate Statement", "CAD", T1),
                  ff("Retail Sales", "USD", T1, impact="Medium"),
                  ff("Core CPI", "USD", T1)])
    clock.at = T1
    view = cal.window_view("USDJPY")
    payload = json.dumps(view.to_dict())
    assert view.in_window and [e.name for e in view.active] == ["Core CPI"]
    assert "BoC Rate Statement" not in payload and "Retail Sales" not in payload
    assert "CAD" not in payload
    assert view.currencies == ("JPY", "USD") and view.pair == "USDJPY"


def test_window_view_boundaries_come_from_the_settings(tmp_path):
    cal, _ = make(tmp_path)
    refresh(cal, [ff("Core CPI", "USD", T1)])
    for offset, expected in [(-30, True), (15, True)]:
        at = T1 + timedelta(minutes=offset)
        assert cal.window_view("USDJPY", at).in_window is expected
    assert not cal.window_view("USDJPY", T1 - timedelta(minutes=30) - US).in_window
    assert not cal.window_view("USDJPY", T1 + timedelta(minutes=15) + US).in_window
    wide, _ = make(tmp_path, window=WindowSettings(3, 60, 45), name="t.db")
    assert wide.window_view("USDJPY", T1 - timedelta(minutes=60)).in_window
    assert wide.window_view("USDJPY", T1 + timedelta(minutes=45)).in_window
    assert not wide.window_view("USDJPY", T1 + timedelta(minutes=45) + US).in_window


def test_window_view_uses_the_pair_currencies_and_all(tmp_path):
    cal, _ = make(tmp_path)
    refresh(cal, [ff("EU CPI", "EUR", T1), ff("Holiday", "All", T1)])
    usdjpy = cal.window_view("USDJPY", T1)
    eurusd = cal.window_view("EURUSD", T1)
    assert [e.name for e in usdjpy.active] == ["Holiday"]
    assert sorted(e.name for e in eurusd.active) == ["EU CPI", "Holiday"]


def test_window_view_next_event_is_within_seven_days(tmp_path):
    cal, _ = make(tmp_path)
    edge = NOW + timedelta(days=7)
    refresh(cal, [ff("Edge", "USD", edge),
                  ff("Over", "USD", edge + US),
                  ff("Other", "CAD", NOW + timedelta(hours=1))])
    view = cal.window_view("USDJPY", NOW)
    assert view.next_event is not None and view.next_event.name == "Edge"
    assert not view.in_window
    only_over, _ = make(tmp_path, name="o.db")
    refresh(only_over, [ff("Over", "USD", edge + timedelta(minutes=1))])
    assert only_over.window_view("USDJPY", NOW).next_event is None


def test_window_view_unknown_pair_and_naive_now_are_refused(tmp_path):
    cal, _ = make(tmp_path)
    with pytest.raises(ValueError):
        cal.window_view("GBPJPY", NOW)
    with pytest.raises(ValueError):
        cal.window_view("USDJPY", datetime(2026, 10, 28, 12, 0))


def test_window_view_uses_the_clock_by_default_and_converts_offsets(tmp_path):
    cal, clock = make(tmp_path)
    refresh(cal, [ff("Core CPI", "USD", T1)])
    clock.at = T1
    assert cal.window_view("USDJPY").in_window
    jst = timezone(timedelta(hours=9))
    clock.at = NOW
    assert cal.window_view("USDJPY", T1.astimezone(jst)).in_window


def test_a_stale_calendar_still_builds_windows_from_stored_events(tmp_path):
    cal, clock = make(tmp_path)
    refresh(cal, [ff("Core CPI", "USD", T1)])
    clock.at = T1 + timedelta(hours=40)
    stale = cal.window_view("USDJPY", T1)
    assert stale.freshness == "stale" and stale.in_window


def test_a_window_view_for_an_unknown_calendar_still_marks_stored_events(tmp_path):
    cal, _ = make(tmp_path)
    cal.conn.execute("INSERT INTO econ_events (ts,country,name,importance) "
                     "VALUES (?,?,?,3)", (T1.isoformat(), "USD", "Seeded"))
    cal.conn.commit()
    view = cal.window_view("USDJPY", T1)
    assert view.freshness == "unknown" and view.in_window


def test_upcoming_is_unchanged(tmp_path):
    cal, _ = make(tmp_path)
    refresh(cal, [ff("A", "USD", NOW + timedelta(hours=1)),
                  ff("B", "CAD", NOW + timedelta(hours=2), impact="Low")])
    assert [r["name"] for r in cal.upcoming(24)] == ["A", "B"]


# --- 鮮度 §5-13 --------------------------------------------------------------

def test_freshness_is_unknown_before_any_success(tmp_path):
    cal, _ = make(tmp_path)
    assert cal.freshness() == "unknown"
    fetch_attempts.record_attempt(cal.conn, fetch_attempts.ECON_KEY, NOW)
    assert cal.freshness() == "unknown"               # 試行だけでは成功ではない
    with patch("httpx.get", side_effect=OSError("down")):
        cal.refresh()
    assert cal.freshness() == "unknown"               # 初回成功なしの失敗も unknown


def test_freshness_is_fresh_after_a_success_and_a_later_attempt_does_not_hide_it(tmp_path):
    cal, clock = make(tmp_path)
    refresh(cal, [ff("A", "USD", T1)])
    assert cal.freshness() == "fresh"
    clock.at = NOW + timedelta(hours=6)
    fetch_attempts.record_attempt(cal.conn, fetch_attempts.ECON_KEY, clock.at)
    assert cal.freshness() == "fresh"                 # 試行の記録は成功を消さない


def test_a_failure_after_a_success_is_stale_and_a_later_success_recovers(tmp_path):
    cal, clock = make(tmp_path)
    refresh(cal, [ff("A", "USD", T1)])
    clock.at = NOW + timedelta(hours=6)
    with patch("httpx.get", side_effect=OSError("down")):
        cal.refresh()
    assert cal.freshness() == "stale"
    clock.at = NOW + timedelta(hours=12)
    refresh(cal, [ff("A", "USD", T1)])
    assert cal.freshness() == "fresh"


def test_twelve_hours_exactly_is_fresh_and_one_microsecond_more_is_stale(tmp_path):
    cal, clock = make(tmp_path)
    refresh(cal, [ff("A", "USD", T1)])
    assert cal.freshness(NOW + timedelta(hours=12)) == "fresh"
    assert cal.freshness(NOW + timedelta(hours=12) + US) == "stale"
    clock.at = NOW + timedelta(hours=12) + US
    assert cal.freshness() == "stale"


def test_the_threshold_is_twice_the_refresh_interval(tmp_path):
    cal, _ = make(tmp_path)
    cal.refresh_interval = timedelta(hours=1)
    refresh(cal, [ff("A", "USD", T1)])
    assert cal.freshness(NOW + timedelta(hours=2)) == "fresh"
    assert cal.freshness(NOW + timedelta(hours=2) + US) == "stale"


def test_a_partial_store_failure_counts_as_a_failure(tmp_path):
    cal, _ = make(tmp_path)
    refresh(cal, [ff("A", "USD", T1)])
    with patch("agentic_fx.datafeed.econ_calendar.econ_events.upsert",
               side_effect=sqlite_error()):
        assert refresh(cal, [ff("A", "USD", T1)]) == 0
    assert cal.freshness() == "stale"


def test_a_429_after_a_success_keeps_the_outcome_and_the_retry_after(tmp_path):
    cal, clock = make(tmp_path)
    refresh(cal, [ff("A", "USD", T1)])
    clock.at = NOW + timedelta(hours=1)
    err = httpx.HTTPStatusError(
        "err", request=httpx.Request("GET", "https://x.invalid/"),
        response=httpx.Response(429, headers={"Retry-After": "7200"},
                                request=httpx.Request("GET", "https://x.invalid/")))
    with patch("httpx.get", side_effect=err):
        cal.refresh()
    assert cal.freshness() == "stale"
    loaded = fetch_attempts.load(cal.conn, fetch_attempts.ECON_KEY)
    assert loaded[1] == timedelta(hours=2)
    raw = json.loads(cal.conn.execute(
        "SELECT value FROM alert_state WHERE key=?",
        (fetch_attempts.ECON_KEY,)).fetchone()[0])
    assert raw["last_success_at"] == NOW.isoformat()
    assert raw["last_outcome"] == "failure"


def test_freshness_that_cannot_be_read_is_unknown_and_never_raises(tmp_path):
    cal, _ = make(tmp_path)
    cal.conn.execute("INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?)",
                     (fetch_attempts.ECON_KEY, "{not json", NOW.isoformat()))
    cal.conn.commit()
    assert cal.freshness() == "unknown"


def test_the_refresh_interval_default_matches_the_scheduler():
    from agentic_fx.core import scheduler
    assert fetch_attempts.ECON_REFRESH_INTERVAL == scheduler._ECON_INTERVAL
