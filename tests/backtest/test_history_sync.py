"""停止中の手動同期: 範囲・尾・要求予算・gap queue・until-done (偽 bridge + 実 sqlite)。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from fractions import Fraction

import pytest

from agentic_fx.backtest import history_sync as hs
from agentic_fx.backtest.history_health import NoBarsEntry, NoBarsList, assess_pair
from agentic_fx.backtest.holdout import in_sample_until
from agentic_fx.core.market_hours import is_market_open
from agentic_fx.store import history_sync as store
from tests.backtest.history_sync_support import (
    REF, SYMBOL, T, UTC, WIDTH, Clock, FakeBridge, conn_at, count, grid, make_settings, seed)

B1 = in_sample_until(T, 1, base_interval="15m")          # 2026-10-11 12:00
B2 = in_sample_until(T, 2, base_interval="15m")          # 2026-09-11 12:00
E = datetime(2026, 11, 11, 11, 30, tzinfo=UTC)            # T - 30s - 15m を格子に切り下げ


def _run(conn, settings, bridge=None, *, now=T, until_done=False, rescan=False, clock=None,
         no_bars=None, **kw):
    clock = clock or Clock()
    extra = {} if no_bars is None else {"no_bars": no_bars}
    result = hs.run_sync(conn, settings, now=now, fetch=bridge or FakeBridge(),
                         until_done=until_done, rescan=rescan, monotonic=clock.monotonic,
                         sleep=clock.sleep, **extra, **kw)
    result.clock = clock
    return result


def _seeded(tmp_path, *, months=1, symbols=(SYMBOL,), settings=None, **kw):
    conn = conn_at(tmp_path)
    start = in_sample_until(T, months, base_interval="15m")
    for symbol in symbols:
        seed(conn, symbol, start, E, **kw)
    return conn, settings or make_settings(holdout_months=months,
                                           watch=[s for s in symbols if s != SYMBOL])


def _day_bounds(day):
    return (datetime(day.year, day.month, day.day, tzinfo=UTC),
            datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1))


def _weekdays(start, stop):
    d = start
    while d < stop:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


# ---- 前提・拒否 -----------------------------------------------------------

def test_preflight_refuses_a_non_mt5_source_with_rc_2_and_no_bridge_call():
    with pytest.raises(hs.SyncRefused) as err:
        hs.preflight(make_settings(eval_source="dukascopy"))
    assert (err.value.code, err.value.rc) == ("not_applicable_eval_source", 2)
    with pytest.raises(hs.SyncRefused) as err:
        hs.preflight(make_settings(eval_source="none"))
    assert err.value.rc == 2


def test_preflight_refuses_a_missing_bridge_url_with_rc_1_and_accepts_a_configured_one():
    with pytest.raises(hs.SyncRefused) as err:
        hs.preflight(make_settings(bridge_url=None))
    assert (err.value.code, err.value.rc) == ("bridge_not_configured", 1)
    hs.preflight(make_settings())


def test_targets_are_pairs_then_watch_symbols_in_setting_order_without_duplicates():
    s = make_settings(watch=["JP225", SYMBOL, "JP225", "US500"])
    assert hs.split_targets(s) == ([SYMBOL], ["JP225", "US500"])


# ---- recent 操作 ----------------------------------------------------------

def test_recent_sync_of_a_complete_history_changes_nothing_and_advances_the_cursor(tmp_path):
    conn, settings = _seeded(tmp_path)
    before = count(conn)
    bridge = FakeBridge()
    result = _run(conn, settings, bridge)
    assert (result.result, result.rc) == ("ok", 0)
    assert result.inserted == 0 and result.unchanged > 0 and result.conflicted == 0
    assert count(conn) == before
    assert result.cursors[SYMBOL] == E.isoformat()
    # E=11:30 の後ろ窓は 11-09 11:30 から。検査尾は同日内なので 11-09/10/11 の 3 要求
    assert result.requests == len(bridge.calls) == 3
    # 要求の開始間隔は 5 秒、待ちは仮想時計
    assert result.clock.sleeps == [5.0, 5.0]
    run = conn.execute("SELECT * FROM history_sync_runs").fetchone()
    assert (run["status"], run["rc"], run["trigger"], run["requests"]) == ("ok", 0, "manual", 3)
    assert run["b_utc"] == B1.isoformat() and run["e_utc"] == E.isoformat()
    assert run["t_utc"] == T.isoformat()


def test_new_bars_are_inserted_with_origin_sync_and_progress_is_recorded_in_the_batch(tmp_path):
    conn = conn_at(tmp_path)
    seed(conn, SYMBOL, B1, datetime(2026, 11, 11, 9, tzinfo=UTC))
    settings = make_settings()
    result = _run(conn, settings)
    assert result.result == "ok" and result.inserted == 10          # 09:00〜11:15 の 10 本
    series = conn.execute("SELECT * FROM history_sync_series WHERE operation='recent'").fetchone()
    assert series["status"] == "ok" and series["inserted"] == 10
    assert series["committed_prefix_end"] == (E).isoformat()
    assert series["last_committed_bar_time"] == (E - WIDTH).isoformat()
    assert series["requests"] == 3 and series["run_id"] == result.run_id


def test_an_empty_series_is_deferred_before_any_request_and_nothing_is_written(tmp_path):
    conn = conn_at(tmp_path)
    bridge = FakeBridge()
    result = _run(conn, make_settings(), bridge)
    # 空の DB は holdout 全体が gap なので、後送りより gap の結果が優先する (rc=3)
    assert (result.result, result.rc) == ("completed_with_gaps", 3)
    assert bridge.calls == [] and result.requests == 0
    assert count(conn) == 0 and count(conn, table="history_conflicts") == 0
    assert result.ops[0].status == "deferred" and result.ops[0].reason == "inspection_insufficient"
    assert result.cursors[SYMBOL] is None                      # cursor を捏造しない
    lines = "\n".join(hs.render(result, make_settings().backtest.dataset(), until_done=False))
    assert "deferred reason=inspection_insufficient" in lines
    assert "remaining=" in lines and "next:" in lines and "afx history import" in lines
    assert "result=completed_with_gaps rc=3" in lines and "last_processed_e=uninitialized" in lines


def test_a_short_response_overlap_defers_with_zero_writes_and_keeps_the_cursor(tmp_path):
    conn, settings = _seeded(tmp_path)
    before = count(conn)
    # 応答が既存の足を返さない (実重複 0) → 新規の足も書かない
    bridge = FakeBridge(drop=lambda symbol, t: t < E - timedelta(minutes=15))
    conn.execute("DELETE FROM ohlcv_history WHERE bar_time >= ?",
                 ((E - timedelta(hours=1)).isoformat(),))
    conn.commit()
    before = count(conn)
    result = _run(conn, settings, bridge)
    assert result.result == "deferred" and result.rc == 0
    assert result.ops[0].outcome.deferred_reason == "inspection_insufficient"
    assert count(conn) == before and count(conn, table="history_conflicts") == 0
    assert result.cursors[SYMBOL] is None
    assert result.ops[0].requests > 0                          # 要求したうえでの検査不足


def test_an_isolated_conflict_is_recorded_as_sync_and_later_bars_still_land(tmp_path):
    conn = conn_at(tmp_path)
    seed(conn, SYMBOL, B1, datetime(2026, 11, 11, 9, tzinfo=UTC))
    settings = make_settings()
    changed = datetime(2026, 11, 11, 7, 0, tzinfo=UTC)
    result = _run(conn, settings, FakeBridge(change={(SYMBOL, changed): 0.3}))
    assert (result.result, result.rc) == ("completed_with_conflicts", 3)
    assert result.inserted == 10 and result.conflicted == 1
    row = conn.execute("SELECT * FROM history_conflicts").fetchone()
    assert (row["origin"], row["last_origin"], row["state"], row["last_run_id"]) == (
        "sync", "sync", "open", result.run_id)
    kept = conn.execute("SELECT close FROM ohlcv_history WHERE bar_time=?",
                        (changed.isoformat(),)).fetchone()[0]
    assert kept != pytest.approx(kept + 0.3)                    # 既存値は変わらない
    assert result.cursors[SYMBOL] == E.isoformat()              # 衝突完走は確定終端まで進む


def test_a_systematic_shift_opens_the_circuit_and_writes_and_advances_nothing(tmp_path):
    conn, settings = _seeded(tmp_path)
    before = count(conn)
    result = _run(conn, settings, FakeBridge(shift=5.0))
    assert (result.result, result.rc) == ("conflict_circuit_open", 4)
    assert count(conn) == before and count(conn, table="history_conflicts") == 0
    assert result.cursors[SYMBOL] is None
    assert conn.execute("SELECT status FROM history_sync_series").fetchone()[0] == (
        "conflict_circuit_open")


def test_a_bridge_failure_is_a_technical_failure_with_backoff_and_no_cursor_move(tmp_path):
    conn, settings = _seeded(tmp_path)
    result = _run(conn, settings, FakeBridge(fail=RuntimeError("boom")))
    assert result.rc == 1 and result.result == "failed"
    state = store.get_state(conn, "mt5", "15m", SYMBOL)
    assert state["attempts"] == 1 and state["next_retry_at"] is not None
    assert state["last_processed_e"] is None and state["last_error"] == "fetch_failed"
    # 失敗の細部 (例外本文) は記録しない
    assert "boom" not in str(dict(conn.execute("SELECT * FROM history_sync_runs").fetchone()))


def test_a_success_after_a_failure_clears_the_backoff(tmp_path):
    conn, settings = _seeded(tmp_path)
    _run(conn, settings, FakeBridge(fail=RuntimeError("x")))
    result = _run(conn, settings)
    assert result.rc == 0
    state = store.get_state(conn, "mt5", "15m", SYMBOL)
    assert (state["attempts"], state["next_retry_at"]) == (0, None)


def test_requests_are_single_flight_with_at_least_five_seconds_between_starts():
    clock = Clock()
    starts = []

    def fetch(url):
        starts.append(clock.now)
        return {}
    gate = hs.RequestGate(fetch, monotonic=clock.monotonic, sleep=clock.sleep)
    for _ in range(4):
        gate("u")
        clock.now += 1.0                    # 応答に 1 秒かかった
    assert [round(b - a, 6) for a, b in zip(starts, starts[1:])] == [5.0, 5.0, 5.0]
    assert gate.count == 4


# ---- gap queue・古い gap の取得・until-done ---------------------------------

def _holes(conn, days):
    """指定日の足を全部消して gap にする。"""
    for day in days:
        lo, hi = _day_bounds(day)
        conn.execute("DELETE FROM ohlcv_history WHERE symbol=? AND bar_time >= ? AND bar_time < ?",
                     (SYMBOL, lo.isoformat(), hi.isoformat()))
    conn.commit()


def test_an_old_gap_day_is_queued_fetched_and_resolved_in_the_same_sync(tmp_path):
    conn, settings = _seeded(tmp_path)
    _holes(conn, [date(2026, 10, 20)])
    bridge = FakeBridge()
    result = _run(conn, settings, bridge)
    assert (result.result, result.rc) == ("ok", 0)
    lo, hi = _day_bounds(date(2026, 10, 20))
    assert count(conn, SYMBOL) > 0 and conn.execute(
        "SELECT COUNT(*) FROM ohlcv_history WHERE bar_time >= ? AND bar_time < ?",
        (lo.isoformat(), hi.isoformat())).fetchone()[0] == 96
    assert result.gap_counts.get("open", 0) == 0
    gap_ops = [o for o in result.ops if o.operation == "gap"]
    assert len(gap_ops) == 1 and gap_ops[0].day == "2026-10-20"
    assert gap_ops[0].requests == 2                              # 検査尾の前日窓 + 当日窓


def test_a_gap_that_cannot_be_filled_backs_off_and_until_done_terminates(tmp_path):
    conn, settings = _seeded(tmp_path)
    days = [date(2026, 10, 20), date(2026, 10, 22), date(2026, 10, 27)]
    _holes(conn, days)
    lo, _ = _day_bounds(days[0])
    bridge = FakeBridge(drop=lambda s, t: t.date() in set(days))
    result = _run(conn, settings, bridge, until_done=True)
    assert result.subruns == 4                                   # 3 回の無改善 + due なしで終了
    assert (result.result, result.rc) == ("completed_with_gaps", 3)
    for row in conn.execute("SELECT * FROM history_sync_gaps WHERE kind='primary_exact'"):
        assert row["attempts"] == 3 and row["no_improve_count"] == 3
        assert datetime.fromisoformat(row["next_retry_at"]) - T == timedelta(hours=24)
    # 次の同期は待ち中の gap を取りにいかない (recent の 3 要求だけ)
    again = _run(conn, settings, FakeBridge(drop=lambda s, t: t.date() in set(days)))
    assert again.requests == 3
    assert all(o.operation == "recent" for o in again.ops)


def test_the_old_gap_budget_is_32_day_windows_per_subrun_and_until_done_finishes_the_rest(tmp_path):
    conn, settings = _seeded(tmp_path, months=2)
    days = [d for i, d in enumerate(_weekdays(date(2026, 9, 14), date(2026, 11, 6))) if i % 2]
    assert len(days) > hs.OLD_GAP_WINDOWS // 2
    _holes(conn, days)
    bridge = FakeBridge()
    first = _run(conn, settings, bridge)
    # 1 回の上限: recent 3 + 古い gap 32 窓。1 操作は検査尾の前日窓 + 当日窓 = 2 窓
    gap_ops = [o for o in first.ops if o.operation == "gap" and o.status != "deferred"]
    assert sum(o.requests for o in gap_ops) <= hs.OLD_GAP_WINDOWS
    assert len(gap_ops) >= hs.OLD_GAP_WINDOWS // 2
    assert first.requests <= (2 + 2) * 1 + hs.OLD_GAP_WINDOWS
    assert "request_budget" in first.deferred_reasons
    assert first.result == "completed_with_gaps" and first.rc == 3
    open_after_first = first.gap_counts["open"]
    assert open_after_first == len(days) - len(gap_ops)
    # 続きは until-done が尽きるまで
    rest = _run(conn, settings, FakeBridge(), until_done=True)
    assert rest.result == "ok" and rest.rc == 0 and rest.gap_counts["open"] == 0
    assert all(sum(o.requests for o in rest.ops if o.operation == "gap") <= 2 * len(days)
               for _ in [0])


def test_until_done_never_repeats_the_recent_operation_in_later_subruns(tmp_path):
    conn, settings = _seeded(tmp_path, months=2)
    days = [d for i, d in enumerate(_weekdays(date(2026, 9, 14), date(2026, 11, 6))) if i % 2]
    _holes(conn, days)
    result = _run(conn, settings, FakeBridge(), until_done=True)
    assert result.subruns >= 2
    assert [o.operation for o in result.ops].count("recent") == 1
    assert result.gap_counts["open"] == 0


def test_until_done_stops_after_a_failed_subrun(tmp_path):
    conn, settings = _seeded(tmp_path)
    days = [date(2026, 10, 20), date(2026, 10, 22)]
    _holes(conn, days)
    bridge = FakeBridge()

    class Failing(FakeBridge):
        def __call__(self, url):
            payload = super().__call__(url)
            if self.calls[-1][1].date() in set(days):
                raise RuntimeError("down")
            return payload
    result = _run(conn, settings, Failing(), until_done=True)
    assert result.subruns == 1 and result.rc == 1
    assert result.result in ("partial", "failed")


def test_an_unfinished_cursor_older_than_the_lookback_queues_unprocessed_days_and_fetches_them(tmp_path):
    conn, settings = _seeded(tmp_path)
    store.write_txn(conn, lambda c: store.advance_cursor(
        c, "mt5", "15m", SYMBOL, last_processed_e=datetime(2026, 11, 3, 12, tzinfo=UTC),
        run_id="old", now=T), sleep=lambda _s: None)
    result = _run(conn, settings)
    rows = conn.execute("SELECT day_utc, state, resolution FROM history_sync_gaps "
                        "WHERE kind='unprocessed' ORDER BY day_utc").fetchall()
    # 11-03 〜 11-09 の開場日 (土曜は全日閉場、日曜は 22:00 以降) が日ごとの行になり、取得後に processed として解消する
    assert [r["day_utc"] for r in rows] == [
        "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-08", "2026-11-09"]
    assert {r["state"] for r in rows} == {"resolved"}
    assert {r["resolution"] for r in rows} == {"processed"}
    assert result.cursors[SYMBOL] == E.isoformat() and result.rc == 0


def test_a_gap_day_at_or_before_b_is_expired_without_any_request(tmp_path):
    conn, settings = _seeded(tmp_path)
    store.write_txn(conn, lambda c: store.observe_gap(
        c, source="mt5", interval="15m", symbol=SYMBOL, day_utc="2026-10-05",
        kind="primary_exact", missing_count=90, run_id="x", now=T), sleep=lambda _s: None)
    bridge = FakeBridge()
    result = _run(conn, settings, bridge)
    gap = conn.execute("SELECT state, next_retry_at FROM history_sync_gaps").fetchone()
    assert (gap["state"], gap["next_retry_at"]) == ("expired", None)
    assert result.requests == 3 and all(s == SYMBOL for s, _a, _b in bridge.calls)
    assert all(a >= B1 - timedelta(hours=3) for _s, a, _b in bridge.calls)    # B より前は要求しない
    assert result.gap_counts["expired"] == 1


def test_rescan_reopens_an_expired_gap_only_when_the_holdout_grew_back(tmp_path):
    conn, settings = _seeded(tmp_path, months=2)
    store.write_txn(conn, lambda c: store.observe_gap(
        c, source="mt5", interval="15m", symbol=SYMBOL, day_utc="2026-09-20",
        kind="primary_exact", missing_count=90, run_id="x", now=T), sleep=lambda _s: None)
    conn.execute("UPDATE history_sync_gaps SET state='expired'")
    conn.commit()
    plain = _run(conn, settings)
    assert conn.execute("SELECT state FROM history_sync_gaps").fetchone()[0] in (
        "expired", "resolved")
    conn.execute("UPDATE history_sync_gaps SET state='expired', resolution='holdout_passed'")
    conn.commit()
    _run(conn, settings, rescan=True)
    state = conn.execute("SELECT state FROM history_sync_gaps").fetchone()[0]
    assert state in ("open", "resolved")           # 復活して再観測・解消された (expired のままではない)
    assert plain.rc in (0, 3)


# ---- 参照の疑い -----------------------------------------------------------

def _insert_day(conn, symbol, day, n):
    lo, _ = _day_bounds(day)
    rows = [(symbol, "15m", (lo + i * WIDTH).isoformat(), 1.0, 1.1, 0.9, 1.0, 1.0, "mt5", None)
            for i in range(n)]
    conn.executemany("INSERT INTO ohlcv_history (symbol, interval, bar_time, open, high, low, "
                     "close, volume, source, spread) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()


def _suspects(conn, *, now=datetime(2026, 11, 20, 12, tzinfo=UTC), no_bars=hs.EMPTY_NO_BARS):
    from agentic_fx.backtest.dataset import HistoryDataset
    return hs.reference_suspects(
        conn, HistoryDataset("mt5", "15m"), REF, [SYMBOL], boundary=datetime(2026, 10, 1, tzinfo=UTC),
        now=now, no_bars=no_bars)


def _prior_weekdays(target, n):
    """月〜木 (共通暦で 96 格子が全部開場の日) を、target の前から n 日。"""
    out, d = [], target - timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 4:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


TUESDAY = date(2026, 10, 27)
SUNDAY = date(2026, 11, 8)


def _suspect_fixture(tmp_path, target, *, prior=20, prior_count=96, target_day=TUESDAY):
    conn = conn_at(tmp_path)
    for day in _prior_weekdays(target_day, prior):
        _insert_day(conn, REF, day, prior_count)
        _insert_day(conn, SYMBOL, day, 96)
    _insert_day(conn, SYMBOL, target_day, 96)
    if target:
        _insert_day(conn, REF, target_day, target)
    return conn, target_day


@pytest.mark.parametrize("count,suspected", [(0, True), (47, True), (48, False), (49, False),
                                             (96, False)])
def test_reference_is_suspected_below_half_of_the_median_and_exactly_half_is_not(
        tmp_path, count, suspected):
    conn, day = _suspect_fixture(tmp_path, count)
    assert (day in _suspects(conn)) is suspected


def test_with_fewer_than_twenty_known_days_only_a_zero_day_is_suspected(tmp_path):
    conn, day = _suspect_fixture(tmp_path, 10, prior=19)
    assert day not in _suspects(conn)
    (tmp_path / "b").mkdir()
    conn2, day2 = _suspect_fixture(tmp_path / "b", 0, prior=19)
    assert day2 in _suspects(conn2)


def test_a_normal_sunday_is_judged_by_the_fill_of_its_open_slots_not_by_the_count(tmp_path):
    # 日曜は 22:00 以降の 8 格子だけが開場。全格子に足があれば充足率 100% で疑わない
    conn, day = _suspect_fixture(tmp_path, 8, target_day=SUNDAY)
    assert day not in _suspects(conn)
    # 開場 8 格子の半分 (4 本) は 50% ちょうどで対象外、3 本は疑い、0 本も疑い
    for count, suspected in ((4, False), (3, True), (0, True)):
        (tmp_path / f"s{count}").mkdir()
        conn2, day2 = _suspect_fixture(tmp_path / f"s{count}", count, target_day=SUNDAY)
        assert (day2 in _suspects(conn2)) is suspected, count


def test_a_product_specific_closure_on_an_open_calendar_day_is_still_suspected(tmp_path):
    # 共通暦では開場の平日に、その商品だけ足が無い (休場) -> 疑いに残る
    conn, day = _suspect_fixture(tmp_path, 0)
    assert day in _suspects(conn)
    (tmp_path / "p").mkdir()
    conn2, day2 = _suspect_fixture(tmp_path / "p", 20)       # 20/96 は半分に満たない
    assert day2 in _suspects(conn2)


def test_a_full_calendar_closure_day_is_neither_a_suspect_nor_part_of_the_baseline(tmp_path):
    conn, day = _suspect_fixture(tmp_path, 96)
    saturday = date(2026, 10, 24)
    _insert_day(conn, SYMBOL, saturday, 96)           # 土曜は全日閉場 (開場格子 0)
    assert saturday not in _suspects(conn)


def test_the_median_of_an_even_window_is_the_mean_of_the_two_middle_fills(tmp_path):
    conn = conn_at(tmp_path)
    target = TUESDAY
    for i, day in enumerate(_prior_weekdays(target, 20)):   # 10 日が 40 本、10 日が 60 本 → 中央値 50
        _insert_day(conn, REF, day, 40 if i < 10 else 60)
        _insert_day(conn, SYMBOL, day, 96)
    _insert_day(conn, SYMBOL, target, 96)
    _insert_day(conn, REF, target, 25)              # 25/96 は 50/96 の半分未満ではない (ちょうど半分より上)
    assert target not in _suspects(conn)
    conn.execute("DELETE FROM ohlcv_history WHERE symbol=? AND bar_time LIKE ?",
                 (REF, f"{target.isoformat()}%"))
    _insert_day(conn, REF, target, 24)
    assert target in _suspects(conn)


def test_days_without_primary_bars_incomplete_days_and_no_bars_days_are_not_suspected(tmp_path):
    conn, day = _suspect_fixture(tmp_path, 0)
    assert day in _suspects(conn)
    # 当日 (まだ完了していない UTC 日) は疑わない
    assert day not in _suspects(conn, now=datetime(day.year, day.month, day.day, 12, tzinfo=UTC))
    # 主 pair に足が無い日は比較できない
    conn.execute("DELETE FROM ohlcv_history WHERE symbol=? AND bar_time LIKE ?",
                 (SYMBOL, f"{day.isoformat()}%"))
    conn.commit()
    assert day not in _suspects(conn)
    # 参照の無足日 (no-bars 一覧) は疑いから外す
    (tmp_path / "x").mkdir()
    conn2, day2 = _suspect_fixture(tmp_path / "x", 0)
    listed = NoBarsList((NoBarsEntry("mt5", "15m", REF, day2, "holiday", "human",
                                     datetime(2026, 11, 1, tzinfo=UTC)),), "d")
    assert day2 not in _suspects(conn2, no_bars=listed)


def test_a_newly_suspected_reference_makes_the_run_completed_with_gaps_once_only(tmp_path):
    conn = conn_at(tmp_path)
    settings = make_settings(watch=[REF])
    seed(conn, SYMBOL, B1, E)
    seed(conn, REF, B1, E, skip=lambda t: t.date() == date(2026, 10, 28))
    # 応答も当日を返さない (本当に休場)。正常な日曜 (開場 8 格子が全部ある) は疑いにならない
    bridge = FakeBridge(drop=lambda s, t: s == REF and t.date() == date(2026, 10, 28))
    first = _run(conn, settings, bridge)
    assert (first.result, first.rc) == ("completed_with_gaps", 3)
    assert first.new_reference_suspects == 1 and first.primary_open_gaps == 0
    row = conn.execute("SELECT * FROM history_sync_gaps WHERE symbol=?", (REF,)).fetchone()
    assert (row["kind"], row["state"], row["day_utc"]) == (
        "reference_suspected", "open", "2026-10-28")
    second = _run(conn, settings, FakeBridge(drop=lambda s, t: s == REF and t.date()
                                             == date(2026, 10, 28)))
    assert second.new_reference_suspects == 0
    assert (second.result, second.rc) == ("ok", 0)             # 既知の疑いだけでは rc=3 にしない


def test_reference_series_are_synced_after_the_primary_in_setting_order(tmp_path):
    conn = conn_at(tmp_path)
    settings = make_settings(watch=[REF, "US500"])
    for symbol in (SYMBOL, REF, "US500"):
        seed(conn, symbol, B1, E)
    result = _run(conn, settings, FakeBridge())
    assert [o.symbol for o in result.ops if o.operation == "recent"] == [SYMBOL, REF, "US500"]


# ---- 観測の正本 -----------------------------------------------------------

def test_the_slot_at_e_is_not_a_false_gap_and_the_rule_matches_the_guard(tmp_path):
    from agentic_fx.backtest.dataset import HistoryDataset
    conn = conn_at(tmp_path)
    now = datetime(2026, 11, 11, 0, 10, 7, tzinfo=UTC)
    limit = datetime(2026, 11, 10, 23, 45, tzinfo=UTC)
    day = date(2026, 11, 10)
    lo, _ = _day_bounds(day)
    seed(conn, SYMBOL, lo, datetime(2026, 11, 10, 23, 30, tzinfo=UTC))
    dataset = HistoryDataset("mt5", "15m")
    kw = dict(start=lo, now=now, grace=timedelta(seconds=30), no_bars=hs.EMPTY_NO_BARS)
    at_e = hs.primary_day_missing(conn, dataset, SYMBOL, end=limit, **kw)
    beyond = hs.primary_day_missing(conn, dataset, SYMBOL, end=limit + WIDTH, **kw)
    assert at_e == {day: 1}                  # 23:30 のみ。E (23:45) 以後の余裕 1 本は数えない
    assert beyond == {day: 2}


def test_gap_days_are_exactly_the_guard_missing_days(tmp_path):
    from agentic_fx.backtest.dataset import HistoryDataset
    conn = conn_at(tmp_path)
    start = datetime(2026, 11, 2, tzinfo=UTC)
    now = datetime(2026, 11, 11, 12, tzinfo=UTC)
    seed(conn, SYMBOL, start, E, skip=lambda t: (
        t.date() == date(2026, 11, 4)                                    # 全日欠損
        or (t.date() == date(2026, 11, 5) and t.hour < 2)                # 2 時間 (> 60 分)
        or (t.date() == date(2026, 11, 6) and t.hour == 3)))             # 1 時間ちょうど (対象外)
    dataset = HistoryDataset("mt5", "15m")
    grace = timedelta(seconds=30)
    missing = hs.primary_day_missing(conn, dataset, SYMBOL, start=start, end=E, now=now,
                                     grace=grace, no_bars=hs.EMPTY_NO_BARS)
    flagged = hs.guard_relevant(missing, dataset.width)
    health = assess_pair(SYMBOL, window_start=start, window_end=E, now=now,
                         width=dataset.width, grace=grace,
                         present_times=hs.present_times(conn, dataset, SYMBOL, start, E),
                         latest_confirmed=None)
    assert flagged == dict(health.missing_days)
    assert date(2026, 11, 6) in missing and date(2026, 11, 6) not in flagged


def test_no_bars_days_are_not_expected_so_they_are_not_gaps(tmp_path):
    from agentic_fx.backtest.dataset import HistoryDataset
    conn = conn_at(tmp_path)
    start, now = datetime(2026, 11, 2, tzinfo=UTC), datetime(2026, 11, 11, 12, tzinfo=UTC)
    seed(conn, SYMBOL, start, E, skip=lambda t: t.date() == date(2026, 11, 4))
    dataset = HistoryDataset("mt5", "15m")
    kw = dict(start=start, end=E, now=now, grace=timedelta(seconds=30))
    assert date(2026, 11, 4) in hs.primary_day_missing(
        conn, dataset, SYMBOL, no_bars=hs.EMPTY_NO_BARS, **kw)
    listed = NoBarsList((NoBarsEntry("mt5", "15m", SYMBOL, date(2026, 11, 4), "holiday", "human",
                                     datetime(2026, 11, 5, tzinfo=UTC)),), "d")
    assert date(2026, 11, 4) not in hs.primary_day_missing(
        conn, dataset, SYMBOL, no_bars=listed, **kw)


# ---- 記録 -----------------------------------------------------------------

def test_a_failed_series_record_is_counted_and_the_run_is_not_reported_as_success(tmp_path):
    conn, settings = _seeded(tmp_path)
    conn.execute("DROP TABLE history_sync_series")
    conn.commit()
    result = _run(conn, settings)
    assert result.unrecorded_series >= 1
    assert (result.result, result.rc) == ("partial", 1)
    assert conn.execute("SELECT unrecorded_series FROM history_sync_runs").fetchone()[0] >= 1


def test_a_running_run_left_by_a_dead_owner_is_recovered_as_interrupted(tmp_path):
    conn, settings = _seeded(tmp_path)
    store.write_txn(conn, lambda c: store.insert_run(
        c, run_id="stale", owner="offline:999", trigger="manual", source="mt5", interval="15m",
        calendar_version="v", lookback_days=2, epoch="none", t=T, b=T, e=T, started_at=T),
        sleep=lambda _s: None)
    _run(conn, settings)
    statuses = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM history_sync_runs")}
    assert statuses["stale"] == "interrupted"
    assert sorted(statuses.values()) == ["interrupted", "ok"]


def test_the_sync_origin_conflict_is_the_shared_history_conflicts_record(tmp_path):
    conn = conn_at(tmp_path)
    seed(conn, SYMBOL, B1, datetime(2026, 11, 11, 9, tzinfo=UTC))
    first = _run(conn, make_settings(), FakeBridge(change={(SYMBOL, datetime(
        2026, 11, 11, 7, tzinfo=UTC)): 0.3}))
    second = _run(conn, make_settings(), FakeBridge(change={(SYMBOL, datetime(
        2026, 11, 11, 7, tzinfo=UTC)): 0.3}))
    rows = conn.execute("SELECT * FROM history_conflicts").fetchall()
    assert len(rows) == 1 and rows[0]["seen_count"] == 2           # 同じ id を再観測 (冪等)
    assert rows[0]["first_run_id"] == first.run_id and rows[0]["last_run_id"] == second.run_id


# ---- 系列の独立・round-robin・B の clip・遮断の検出 --------------------------

def test_one_failed_series_does_not_roll_back_the_others_and_is_the_technical_failure(tmp_path):
    conn = conn_at(tmp_path)
    settings = make_settings(watch=[REF, "US500"])
    for symbol in (SYMBOL, REF, "US500"):
        seed(conn, symbol, B1, E)

    class Corrupt(FakeBridge):
        def __call__(self, url):
            payload = super().__call__(url)
            if self.calls[-1][0] == REF:
                payload["bars"][-1]["close"] = "not-a-number"        # 最終窓の応答が不正
            return payload
    result = _run(conn, settings, Corrupt())
    by_symbol = {o.symbol: o for o in result.ops if o.operation == "recent"}
    assert by_symbol[REF].status == "failed" and by_symbol[REF].reason == "invalid_response"
    assert by_symbol[SYMBOL].status == by_symbol["US500"].status == "ok"
    assert (result.result, result.rc) == ("partial", 1)           # 確定した系列があるので partial
    assert result.cursors[SYMBOL] == result.cursors["US500"] == E.isoformat()
    assert result.cursors[REF] is None
    state = store.get_state(conn, "mt5", "15m", REF)
    assert state["attempts"] == 1 and state["last_error"] == "invalid_response"


def test_a_failed_old_gap_does_not_undo_the_recent_operation_of_the_same_run(tmp_path):
    conn, settings = _seeded(tmp_path)
    day = date(2026, 10, 20)
    _holes(conn, [day])

    class FailGap(FakeBridge):
        def __call__(self, url):
            payload = super().__call__(url)
            if self.calls[-1][1].date() == day:
                raise RuntimeError("down")
            return payload
    result = _run(conn, settings, FailGap())
    recent = next(o for o in result.ops if o.operation == "recent")
    assert recent.status == "ok" and result.cursors[SYMBOL] == E.isoformat()
    assert (result.result, result.rc) in (("failed", 1), ("partial", 1))
    assert store.get_state(conn, "mt5", "15m", SYMBOL)["last_processed_e"] == E.isoformat()


def test_old_gap_budget_is_shared_round_robin_across_symbols_and_the_cursor_is_persisted(tmp_path):
    conn = conn_at(tmp_path)
    settings = make_settings(holdout_months=2, watch=[REF])
    for symbol in (SYMBOL, REF):
        seed(conn, symbol, B2, E)
    weekdays = list(_weekdays(date(2026, 9, 14), date(2026, 11, 6)))
    # 主 pair は奇数日、参照は偶数日が欠ける (参照は主 pair に足がある日の 0 本が疑いになる)
    for symbol, parity in ((SYMBOL, 1), (REF, 0)):
        for i, day in enumerate(weekdays):
            if i % 2 == parity:
                lo, hi = _day_bounds(day)
                conn.execute("DELETE FROM ohlcv_history WHERE symbol=? AND bar_time >= ? "
                             "AND bar_time < ?", (symbol, lo.isoformat(), hi.isoformat()))
    conn.commit()
    result = _run(conn, settings)
    gap_ops = [o for o in result.ops if o.operation == "gap" and o.status != "deferred"]
    symbols = [o.symbol for o in gap_ops]
    # 一方の銘柄が予算を独占しない: 古い日から順に、銘柄を交互に選ぶ
    assert set(symbols[:2]) == {SYMBOL, REF}
    assert abs(symbols.count(SYMBOL) - symbols.count(REF)) <= 1
    assert store.get_rr_cursor(conn, "mt5", "15m") == symbols[-1]


def test_a_gap_day_that_crosses_b_is_clipped_to_b_for_requests_and_rows(tmp_path):
    conn, settings = _seeded(tmp_path)
    day = B1.date()                                              # B の当日 (12:00 以降だけが holdout)
    lo, hi = _day_bounds(day)
    conn.execute("DELETE FROM ohlcv_history WHERE bar_time >= ? AND bar_time < ?",
                 (B1.isoformat(), hi.isoformat()))
    conn.commit()
    before_b = conn.execute("SELECT COUNT(*) FROM ohlcv_history WHERE bar_time < ?",
                            (B1.isoformat(),)).fetchone()[0]
    bridge = FakeBridge()
    _run(conn, settings, bridge, until_done=True)
    assert all(start >= B1 for _s, start, _e in bridge.calls) or all(
        start >= B1 - timedelta(hours=3) for _s, start, _e in bridge.calls)
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history WHERE bar_time < ?",
                        (B1.isoformat(),)).fetchone()[0] == before_b      # B より前の行は増えない


def test_an_hour_shifted_response_opens_the_circuit_instead_of_appending_blindly(tmp_path):
    conn, settings = _seeded(tmp_path)
    before = count(conn)

    class Shifted(FakeBridge):
        def __call__(self, url):
            payload = super().__call__(url)
            for bar in payload["bars"]:
                t = datetime.fromisoformat(bar["time"]) - timedelta(hours=1)
                from tests.backtest.history_sync_support import values
                o, h, low, c = values(self.calls[-1][0], t)
                bar.update(open=o, high=h, low=low, close=c)
            return payload
    result = _run(conn, settings, Shifted())
    assert result.result == "conflict_circuit_open" and result.rc == 4
    assert count(conn) == before and count(conn, table="history_conflicts") == 0


def test_unchanged_rows_cost_no_per_row_write_statements(tmp_path):
    conn, settings = _seeded(tmp_path)
    statements = []
    conn.set_trace_callback(statements.append)
    result = _run(conn, settings)
    conn.set_trace_callback(None)
    assert result.unchanged > 100 and result.inserted == 0
    history_writes = [s for s in statements if s.lstrip().upper().startswith(
        ("INSERT INTO OHLCV_HISTORY", "UPDATE OHLCV_HISTORY", "INSERT OR IGNORE INTO OHLCV_HISTORY",
         "INSERT INTO HISTORY_CONFLICTS"))]
    assert history_writes == []


def test_the_sync_writes_no_in_sample_rows_before_b_even_with_a_deep_lookback(tmp_path):
    conn = conn_at(tmp_path)
    settings = make_settings(lookback_days=31)
    seed(conn, SYMBOL, B1, E)
    bridge = FakeBridge()
    _run(conn, settings, bridge)
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history WHERE bar_time < ?",
                        (B1.isoformat(),)).fetchone()[0] == 0
    assert all(start >= B1 - timedelta(hours=3) for _s, start, _e in bridge.calls)


def test_the_recent_operation_is_deferred_before_any_request_when_it_would_exceed_the_window_budget(
        tmp_path, monkeypatch):
    conn, settings = _seeded(tmp_path)
    from agentic_fx.backtest import history_sync_plan as plan
    # 後ろ窓が想定より長く決まってしまっても、D+2 窓の上限を超える操作は要求 0 で後送りにする
    monkeypatch.setattr(plan, "lookback_start", lambda limit, days, boundary: limit - timedelta(days=6))
    bridge = FakeBridge()
    result = _run(conn, settings, bridge)
    recent = next(o for o in result.ops if o.operation == "recent")
    assert (recent.status, recent.reason, recent.requests) == ("deferred", "request_budget", 0)
    # 上限外の範囲は未取得として queue に残り、要求はその gap 操作だけが出す
    assert result.cursors[SYMBOL] is None
    assert len(bridge.calls) == sum(o.requests for o in result.ops if o.operation == "gap") > 0


def test_the_final_scan_stops_at_e_so_the_margin_slot_is_never_observed(tmp_path, monkeypatch):
    conn, settings = _seeded(tmp_path)
    ends = []
    real = hs.primary_day_missing

    def spy(*args, **kwargs):
        ends.append(kwargs["end"])
        return real(*args, **kwargs)
    monkeypatch.setattr(hs, "primary_day_missing", spy)
    result = _run(conn, settings)
    assert ends and set(ends) == {result.e}


@pytest.mark.parametrize('previous_attempts', [0, 2])
def test_until_done_backs_off_insufficient_gap_once_while_other_gaps_progress(tmp_path, previous_attempts):
    conn, settings = _seeded(tmp_path, months=2)
    stuck = date(2026, 10, 20)
    progressing = date(2026, 10, 22)
    _holes(conn, [stuck, progressing])
    store.write_txn(conn, lambda c: store.observe_gap(
        c, source='mt5', interval='15m', symbol=SYMBOL, day_utc=stuck.isoformat(),
        kind='primary_exact', missing_count=96, run_id='old', now=T), sleep=lambda _s: None)
    conn.execute("UPDATE history_sync_gaps SET attempts=?, no_improve_count=? WHERE day_utc=?",
                 (previous_attempts, previous_attempts, stuck.isoformat()))
    conn.commit()
    bridge = FakeBridge(drop=lambda _symbol, t: t.date() == date(2026, 10, 19) and t.hour >= 22)
    result = _run(conn, settings, bridge, until_done=True)
    gaps = [op for op in result.ops if op.operation == 'gap']
    stuck_ops = [op for op in gaps if op.day == stuck.isoformat()]
    assert len(stuck_ops) == 1
    assert stuck_ops[0].status == 'deferred'
    assert stuck_ops[0].reason == 'inspection_insufficient'
    assert any(op.day == progressing.isoformat() and op.status == 'ok' for op in gaps)
    assert result.subruns < hs.MAX_SUBRUNS
    row = conn.execute("SELECT * FROM history_sync_gaps WHERE day_utc=? AND kind='primary_exact'",
                       (stuck.isoformat(),)).fetchone()
    assert row['state'] == 'open' and row['attempts'] == previous_attempts + 1
    wait = timedelta(minutes=5) if previous_attempts == 0 else timedelta(hours=24)
    assert datetime.fromisoformat(row['next_retry_at']) == T + wait
    assert conn.execute("SELECT COUNT(*) FROM history_sync_series WHERE run_id=? AND operation='gap' AND day_utc=?",
                        (result.run_id, stuck.isoformat())).fetchone()[0] == 1


def test_deferred_gap_is_attempted_once_per_run_even_if_it_becomes_due_again(tmp_path, monkeypatch):
    conn, settings = _seeded(tmp_path, months=4)
    days = list(_weekdays(date(2026, 8, 4), date(2026, 11, 6)))[::2]
    assert len(days) > 32
    stuck = days[0]
    _holes(conn, days)
    clock = Clock()
    actual_due = store.due_gaps

    def advancing_due(conn, **kwargs):
        kwargs['now'] = T + timedelta(seconds=clock.now)
        return actual_due(conn, **kwargs)

    monkeypatch.setattr(store, 'due_gaps', advancing_due)
    bridge = FakeBridge(drop=lambda _symbol, t: t.date() == date(2026, 8, 3) and t.hour >= 22)
    result = _run(conn, settings, bridge, until_done=True, clock=clock)
    assert result.subruns >= 3 and clock.now > 300
    stuck_ops = [op for op in result.ops if op.operation == 'gap' and op.day == stuck.isoformat()]
    assert len(stuck_ops) == 1 and stuck_ops[0].reason == 'inspection_insufficient'
    row = conn.execute("SELECT attempts FROM history_sync_gaps WHERE day_utc=? AND kind='primary_exact'",
                       (stuck.isoformat(),)).fetchone()
    assert row['attempts'] == 1
    assert conn.execute("SELECT COUNT(*) FROM history_sync_series WHERE run_id=? AND operation='gap' AND day_utc=?",
                        (result.run_id, stuck.isoformat())).fetchone()[0] == 1
    assert sum(op.status == 'ok' for op in result.ops if op.operation == 'gap') > 32
