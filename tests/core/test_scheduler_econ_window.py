"""limit 約定の指標回避帯による抑止 (足区間基準) と、親 report の hook。

帯は処理する 1m 足の閉区間 `[b, b+1m]` と inclusive な帯の交差で決め、`now` は使わない。
抑止は約定だけで、注文は cancel せず期限も延ばさない。SL / TP / cancel は常に通る。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core import band_notice
from agentic_fx.core.contracts import Bar
from agentic_fx.core.econ_window import WindowSettings
from agentic_fx.core.scheduler import Scheduler
from agentic_fx.core.writer_admission import WriterAdmission
from agentic_fx.store import alert_state, econ_events, orders

from tests.core.test_scheduler import SETTINGS, WED, Env

EVENT = "limit_fill_suppressed_econ_window"
FILL_BAR = dict(open=148.30, high=148.35, low=148.15, close=148.25, volume=100)


def _env(tmp_path, *, before=30, after=15, importance_min=3, writer=None):
    env = Env(tmp_path)
    env.sched.econ_window = WindowSettings(
        importance_min=importance_min, before_min=before, after_min=after)
    env.sched.pair_currencies = {"USDJPY": ("USD", "JPY")}
    if writer is not None:
        env.sched.writer_admission = writer
    return env


def _event(env, ts, *, country="USD", name="CPI", importance=3):
    return econ_events.upsert(env.conn, ts=ts, country=country, name=name,
                              importance=importance).id


def _bar(ts, **kw):
    values = {**FILL_BAR, **kw}
    return Bar("USDJPY", "1m", ts, values["open"], values["high"], values["low"],
               values["close"], values["volume"])


def _log(env) -> str:
    path = env.tmp_path / "a.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _tick(env, bar_ts, *, delay=1):
    env.bars["USDJPY"] = _bar(bar_ts)
    env.sched.tick(bar_ts + timedelta(minutes=delay))


def _status(env, oid):
    return orders.get(env.conn, oid)["status"]


# --- 足区間と帯の交差 ------------------------------------------------------

def test_a_bar_inside_the_band_does_not_fill_and_the_order_stays_untouched(tmp_path):
    env = _env(tmp_path)
    oid = env.place_limit()
    expires = orders.get(env.conn, oid)["expires_at"]
    _event(env, WED + timedelta(minutes=20))          # 帯 [WED-10m, WED+35m]
    _tick(env, WED)
    row = orders.get(env.conn, oid)
    assert row["status"] == "pending_fill"
    assert row["expires_at"] == expires               # 期限は延ばさない
    assert _log(env).count(EVENT) == 1


def test_a_band_without_a_price_touch_suppresses_nothing_and_notifies_nothing(tmp_path):
    env = _env(tmp_path)
    oid = env.place_limit(price=148.20)
    _event(env, WED + timedelta(minutes=20))
    env.bars["USDJPY"] = _bar(WED, open=148.60, high=148.70, low=148.50, close=148.55)
    env.sched.tick(WED + timedelta(minutes=1))          # limit 148.20 に届かない足
    assert _status(env, oid) == "pending_fill"
    assert EVENT not in _log(env)
    assert _record(env, oid) is None


def test_without_a_band_the_same_bar_fills(tmp_path):
    env = _env(tmp_path)
    oid = env.place_limit()
    _tick(env, WED)
    assert _status(env, oid) == "open"
    assert EVENT not in _log(env)


@pytest.mark.parametrize("event_offset_min, suppressed", [
    (-15, True),    # 帯の後端 (ts+15m) が足の開始 b にちょうど触れる
    (-16, False),   # 後端が b の 1 分前
    (31, True),     # 帯の前端 (ts-30m) が足の終端 b+1m にちょうど触れる
    (32, False),    # 前端が b+1m の 1 分後
])
def test_the_band_and_the_bar_interval_are_both_closed(tmp_path, event_offset_min,
                                                       suppressed):
    env = _env(tmp_path)
    oid = env.place_limit()
    _event(env, WED + timedelta(minutes=event_offset_min))
    _tick(env, WED)
    assert (_status(env, oid) == "pending_fill") is suppressed


def test_a_bar_processed_late_still_overlaps_a_band_that_ended_before_now(tmp_path):
    """帯の後端 (ts+15m = WED+1m) は足 [WED, WED+1m] に触れる。処理が 4 分遅れて
    now が帯の外に出ていても、足で判定するので抑止する。"""
    env = _env(tmp_path)
    oid = env.place_limit()
    _event(env, WED - timedelta(minutes=14))
    _tick(env, WED, delay=4)
    assert _status(env, oid) == "pending_fill"
    assert _log(env).count(EVENT) == 1


def test_events_of_other_currencies_or_below_importance_do_not_suppress(tmp_path):
    env = _env(tmp_path)
    oid = env.place_limit()
    _event(env, WED + timedelta(minutes=10), country="EUR", name="ECB")
    _event(env, WED + timedelta(minutes=10), country="USD", name="Low",
           importance=2)
    _tick(env, WED)
    assert _status(env, oid) == "open"


def test_a_superseded_event_makes_no_band(tmp_path):
    env = _env(tmp_path)
    oid = env.place_limit()
    old = _event(env, WED + timedelta(minutes=10))
    new = _event(env, WED + timedelta(hours=3), name="CPI")
    env.conn.execute("UPDATE econ_events SET superseded_by=? WHERE id=?", (new, old))
    env.conn.commit()
    _tick(env, WED)
    assert _status(env, oid) == "open"


def test_all_country_events_suppress_every_pair(tmp_path):
    env = _env(tmp_path)
    oid = env.place_limit()
    _event(env, WED + timedelta(minutes=10), country="All", name="Holiday")
    _tick(env, WED)
    assert _status(env, oid) == "pending_fill"


def test_without_window_settings_nothing_is_suppressed(tmp_path):
    env = Env(tmp_path)                                 # econ_window / currencies なし
    oid = env.place_limit()
    _event(env, WED + timedelta(minutes=10))
    _tick(env, WED)
    assert _status(env, oid) == "open"


def test_a_pair_without_known_currencies_is_not_suppressed(tmp_path):
    env = _env(tmp_path)
    env.sched.pair_currencies = {"EURUSD": ("EUR", "USD")}
    oid = env.place_limit()
    _event(env, WED + timedelta(minutes=10))
    _tick(env, WED)
    assert _status(env, oid) == "open"


# --- 抑止してよいのは約定だけ --------------------------------------------------

def test_a_suppressed_bar_is_not_reprocessed_and_the_next_bar_can_fill(tmp_path):
    env = _env(tmp_path)
    oid = env.place_limit()
    _event(env, WED + timedelta(minutes=20))
    _tick(env, WED)
    # 同じ足を次 tick で再入力しても、判定も activity も繰り返さない
    env.sched.tick(WED + timedelta(minutes=2))
    assert _status(env, oid) == "pending_fill"
    assert _log(env).count(EVENT) == 1
    # 帯の外に出た新しい足だけが約定できる
    _tick(env, WED + timedelta(minutes=40))
    assert _status(env, oid) == "open"


def test_sl_and_tp_of_open_positions_run_during_the_band(tmp_path):
    env = _env(tmp_path)
    sl = orders.insert(env.conn, pair="USDJPY", direction="long",
                       entry_type="market", horizon="swing", status="open",
                       now=WED, quantity=0.1, avg_fill_price=148.20,
                       stop_loss=147.80, take_profit=149.00)
    tp = orders.insert(env.conn, pair="USDJPY", direction="short",
                       entry_type="market", horizon="swing", status="open",
                       now=WED, quantity=0.1, avg_fill_price=148.20,
                       stop_loss=150.00, take_profit=148.00)
    _event(env, WED + timedelta(minutes=10))
    env.bars["USDJPY"] = _bar(WED, open=148.20, high=148.30, low=147.70,
                              close=147.75)
    env.sched.tick(WED + timedelta(minutes=1))
    assert orders.get(env.conn, sl)["close_reason"] == "sl"
    env.bars["USDJPY"] = _bar(WED + timedelta(minutes=1), open=148.20, high=148.30,
                              low=147.90, close=147.95)
    env.sched.tick(WED + timedelta(minutes=2))
    assert orders.get(env.conn, tp)["close_reason"] == "tp"


def test_cancel_of_a_pending_order_is_allowed_during_the_band(tmp_path):
    env = _env(tmp_path)
    oid = env.place_limit()
    _event(env, WED + timedelta(minutes=10))
    _tick(env, WED)
    assert _status(env, oid) == "pending_fill"
    env.executor.cancel_order(orders.get(env.conn, oid), reason="manual")
    assert _status(env, oid) == "cancelled"


def test_an_order_expires_on_schedule_during_the_band(tmp_path):
    env = _env(tmp_path)
    oid = env.place_limit(hours=1)
    _event(env, WED + timedelta(minutes=10))
    _tick(env, WED)
    assert _status(env, oid) == "pending_fill"
    env.bars["USDJPY"] = _bar(WED + timedelta(hours=1, minutes=1))
    env.sched.tick(WED + timedelta(hours=1, minutes=2))
    assert _status(env, oid) in ("expired", "cancelled")


# --- 通知は order × 連結成分で 1 回 ----------------------------------------------

def _record(env, oid):
    raw = alert_state.get(env.conn, alert_state.limit_suppress_key(oid))
    return json.loads(raw) if raw else None


def test_the_notice_is_written_once_per_order_and_survives_a_restart(tmp_path):
    env = _env(tmp_path)
    oid = env.place_limit()
    _event(env, WED + timedelta(minutes=30))
    for minute in (0, 1, 2):
        _tick(env, WED + timedelta(minutes=minute))
    assert _log(env).count(EVENT) == 1
    # 同じ DB を引き継ぐ新しい scheduler (再起動) でも、同じ成分では書かない
    fresh = Scheduler(
        conn=env.conn, executor=env.executor, settings=SETTINGS,
        state_store=env.state, activity=env.sched.activity,
        bars_fn=lambda p: env.bars.get(p), on_trade_mission=env._trade,
        on_news_cycle=env._news, on_econ_cycle=env._econ,
        econ_window=env.sched.econ_window, pair_currencies=env.sched.pair_currencies)
    env.bars["USDJPY"] = _bar(WED + timedelta(minutes=3))
    fresh.tick(WED + timedelta(minutes=4))
    assert _status(env, oid) == "pending_fill"
    assert _log(env).count(EVENT) == 1


def test_the_notice_names_the_order_and_the_component_id(tmp_path):
    env = _env(tmp_path)
    oid = env.place_limit()
    eid = _event(env, WED + timedelta(minutes=20))
    _tick(env, WED)
    line = [l for l in _log(env).splitlines() if EVENT in l][0]
    assert f"#{oid}" in line and f"成分 {eid}" in line
    assert _record(env, oid) == [{"id": eid, "events": [eid]}]


def _three_bands(env, base):
    """A と B、B と C は帯が重なり、A と C は重ならない (before 30 / after 15)。"""
    a = _event(env, base, name="A")
    b = _event(env, base + timedelta(minutes=40), name="B")
    c = _event(env, base + timedelta(minutes=80), name="C")
    return a, b, c


def test_overlapping_bands_are_one_component_whose_id_is_the_smallest_event_id(tmp_path):
    env = _env(tmp_path)
    oid = env.place_limit(hours=8)
    a, b, c = _three_bands(env, WED + timedelta(minutes=30))
    _tick(env, WED)                                      # A の帯の中
    assert _record(env, oid) == [{"id": a, "events": [a, b, c]}]
    _tick(env, WED + timedelta(minutes=100))             # C の帯の中 (同じ成分)
    _tick(env, WED + timedelta(minutes=120))
    assert _status(env, oid) == "pending_fill"
    assert _log(env).count(EVENT) == 1


def test_a_new_event_joining_the_component_keeps_its_id_and_does_not_renotify(tmp_path):
    env = _env(tmp_path)
    oid = env.place_limit(hours=8)
    a = _event(env, WED + timedelta(minutes=30), name="A")
    _tick(env, WED)
    assert _record(env, oid) == [{"id": a, "events": [a]}]
    late = _event(env, WED + timedelta(minutes=60), name="late")   # A と重なる
    _tick(env, WED + timedelta(minutes=50))
    assert _log(env).count(EVENT) == 1
    assert _record(env, oid) == [{"id": a, "events": [a, late]}]


def test_the_id_is_fixed_when_the_smallest_event_is_superseded(tmp_path):
    env = _env(tmp_path)
    oid = env.place_limit(hours=8)
    a, b, c = _three_bands(env, WED + timedelta(minutes=30))
    _tick(env, WED)
    moved = _event(env, WED + timedelta(hours=6), name="A")        # A の新しい時刻
    env.conn.execute("UPDATE econ_events SET superseded_by=? WHERE id=?", (moved, a))
    env.conn.commit()
    _tick(env, WED + timedelta(minutes=50))                        # 残る B の帯の中
    assert _log(env).count(EVENT) == 1
    assert [r["id"] for r in _record(env, oid)] == [a]


def test_a_separate_band_notifies_again_once(tmp_path):
    env = _env(tmp_path)
    oid = env.place_limit(hours=8)
    first = _event(env, WED + timedelta(minutes=30), name="first")
    second = _event(env, WED + timedelta(hours=4), name="second")
    _tick(env, WED)
    _tick(env, WED + timedelta(hours=3, minutes=40))
    _tick(env, WED + timedelta(hours=3, minutes=41))
    assert _log(env).count(EVENT) == 2
    assert [r["id"] for r in _record(env, oid)] == [first, second]


def test_two_orders_in_the_same_band_notify_separately(tmp_path):
    env = _env(tmp_path)
    one, two = env.place_limit(), env.place_limit()
    _event(env, WED + timedelta(minutes=20))
    _tick(env, WED)
    assert _log(env).count(EVENT) == 2
    assert _record(env, one) is not None and _record(env, two) is not None


def test_the_state_is_pruned_when_the_order_is_no_longer_pending(tmp_path):
    env = _env(tmp_path)
    oid = env.place_limit()
    _event(env, WED + timedelta(minutes=20))
    _tick(env, WED)
    assert env.sched.suppressed_limit_count == 1
    env.executor.cancel_order(orders.get(env.conn, oid), reason="manual")
    env.sched.tick(WED + timedelta(minutes=2))
    assert _record(env, oid) is None
    assert env.sched.suppressed_limit_count == 0


def test_a_failure_to_judge_the_band_skips_the_fill_for_that_bar(tmp_path, monkeypatch):
    """帯を判定できなかった足は約定させない (fail-closed)。例外は吸収する。"""
    env = _env(tmp_path)
    oid = env.place_limit()

    def boom(*args, **kwargs):
        raise RuntimeError("calendar unreadable")

    with monkeypatch.context() as patch:
        patch.setattr(econ_events, "bar_components", boom)
        _tick(env, WED)
    assert _status(env, oid) == "pending_fill"
    assert "limit_fill_error" not in _log(env)            # 吸収する。毎足の activity は出さない
    _tick(env, WED + timedelta(minutes=1))               # 次の足は判定できて約定
    assert _status(env, oid) == "open"


def _bar_error_warnings(caplog):
    return [r for r in caplog.records
            if "band judgement failed" in r.getMessage()]


def test_a_repeated_band_failure_warns_once_per_order_and_operating_day(
        tmp_path, monkeypatch, caplog):
    """毎足の例外で limit が黙って約定しなくならないよう、技術ログに warning を
    (order, 運用日) で 1 回出す。activity の語は増えず、SL / TP と期限は続く。"""
    env = _env(tmp_path)
    oid = env.place_limit(hours=20)
    other = env.place_limit(hours=20)
    sl = orders.insert(env.conn, pair="USDJPY", direction="long",
                       entry_type="market", horizon="swing", status="open",
                       now=WED, quantity=0.1, avg_fill_price=148.20,
                       stop_loss=147.80, take_profit=149.00)

    def boom(*args, **kwargs):
        raise RuntimeError("calendar unreadable")

    with monkeypatch.context() as patch, caplog.at_level("WARNING"):
        patch.setattr(econ_events, "bar_components", boom)
        _tick(env, WED)
        _tick(env, WED + timedelta(minutes=1))
        _tick(env, WED + timedelta(minutes=2))
        same_day = _bar_error_warnings(caplog)
        env.bars["USDJPY"] = _bar(WED + timedelta(minutes=3), low=147.70)
        env.sched.tick(WED + timedelta(minutes=4))
        # 次の運用日 (21:00 UTC の rollover の後) は同じ order でも 1 回出す
        next_day = WED + timedelta(hours=10)
        _tick(env, next_day)
        _tick(env, next_day + timedelta(minutes=1))
    assert len(same_day) == 2                            # order 2 本 × 1 回
    assert {r.getMessage().split()[6] for r in same_day} == {f"#{oid}", f"#{other}"}
    assert _status(env, oid) == "pending_fill" and _status(env, other) == "pending_fill"
    assert orders.get(env.conn, sl)["close_reason"] == "sl"      # 資金保護は続く
    assert len(_bar_error_warnings(caplog)) == 4          # 運用日が変われば 1 回ずつ
    assert "limit_fill_error" not in _log(env)            # 毎足の activity は出さない
    assert "band_judgement" not in _log(env)             # 新しい固定語は無い
    env.bars["USDJPY"] = _bar(WED + timedelta(hours=21))
    env.sched.tick(WED + timedelta(hours=21, minutes=1))
    assert _status(env, oid) in ("expired", "cancelled")  # 自然期限は止まらない


@pytest.mark.parametrize("target", ["alert_state_get", "alert_state_set", "activity"])
def test_a_failure_in_the_notice_path_holds_the_fill_and_warns_once(
        tmp_path, monkeypatch, caplog, target):
    """alert_state や通知の例外も吸収する。足は約定させず、limit_fill_error は出さず、
    warning は (order, 運用日) で 1 回。"""
    env = _env(tmp_path)
    oid = env.place_limit(hours=20)
    _event(env, WED + timedelta(minutes=20))

    def boom(*args, **kwargs):
        raise RuntimeError("storage unavailable")

    with monkeypatch.context() as patch, caplog.at_level("WARNING"):
        if target == "alert_state_get":
            real = alert_state.get
            patch.setattr(alert_state, "get", lambda conn, key, *a, **k: (
                boom() if key.startswith(alert_state.LIMIT_SUPPRESS_PREFIX)
                else real(conn, key, *a, **k)))
        elif target == "alert_state_set":
            real_set = alert_state.set
            patch.setattr(alert_state, "set", lambda conn, key, *a, **k: (
                boom() if key.startswith(alert_state.LIMIT_SUPPRESS_PREFIX)
                else real_set(conn, key, *a, **k)))
        else:
            patch.setattr(env.sched.activity, "write", boom)
        for minute in range(3):
            _tick(env, WED + timedelta(minutes=minute))
    assert _status(env, oid) == "pending_fill"
    assert "limit_fill_error" not in _log(env)
    assert len(_bar_error_warnings(caplog)) == 1


# --- 書込み要求フラグ ---------------------------------------------------------

def test_the_writer_flag_is_up_during_tick_and_down_afterwards(tmp_path):
    writer = WriterAdmission()
    env = _env(tmp_path, writer=writer)
    seen = []
    env.sched.bars_fn = lambda p: (seen.append(writer.requested()), None)[1]
    env.sched.tick(WED)
    assert seen and all(seen)
    assert writer.requested() is False
    assert writer.stats().requests >= 1


def test_the_writer_flag_is_down_during_the_network_hooks_and_cache_prune(tmp_path):
    """news / econ の取得と cache prune、mission の起動はフラグの外。決定論ブロックと
    cron cursor の永続化 (DB に書く範囲) はフラグの中。"""
    writer = WriterAdmission()
    env = _env(tmp_path, writer=writer)
    seen: dict[str, list] = {"news": [], "econ": [], "cache": [], "bars": [],
                             "mission": []}
    env._news_fn = lambda: seen["news"].append(writer.requested())
    env._econ_fn = lambda: seen["econ"].append(writer.requested())
    env.sched.on_cache_maintenance = lambda now: seen["cache"].append(writer.requested())
    env.sched.bars_fn = lambda p: (seen["bars"].append(writer.requested()), None)[1]
    env.sched._trade_mission_due = lambda now: (
        seen["mission"].append(writer.requested()), None)[1]
    env.sched.tick(WED)
    assert seen["news"] == [False] and seen["econ"] == [False]
    assert seen["cache"] == [False]
    assert seen["bars"] and all(seen["bars"])
    assert seen["mission"] == [False]                   # 起動は transaction ではない
    assert writer.requested() is False
    assert writer.stats().requests >= 2                 # 決定論ブロックと後段は別の保持


def test_a_slow_news_fetch_does_not_hold_the_flag(tmp_path):
    writer = WriterAdmission()
    env = _env(tmp_path, writer=writer)
    held = []

    def slow_news():
        held.append(writer.requested())
        time.sleep(0.05)

    env._news_fn = slow_news
    env.sched.tick(WED)
    assert held == [False]
    assert writer.stats().max_hold_sec < 0.05


def test_the_writer_flag_is_released_when_tick_raises(tmp_path):
    writer = WriterAdmission()
    env = _env(tmp_path, writer=writer)

    def boom(_now):
        raise RuntimeError("mtm")

    env.sched._mark_to_market = boom
    with pytest.raises(RuntimeError):
        env.sched.tick(WED)
    assert writer.requested() is False


# --- 親 report の hook --------------------------------------------------------

MON = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)        # 月曜
TUE_CLOSE = datetime(2026, 7, 21, 21, 0, tzinfo=timezone.utc)  # 火曜の取引日の終端
WED_CLOSE = datetime(2026, 7, 22, 21, 0, tzinfo=timezone.utc)


def _report_env(tmp_path, calls, *, base=WED):
    env = Env(tmp_path, base=base)
    env.sched.register_daily_report(lambda close_at: calls.append(close_at))
    return env


def _run(env, pending):
    """遅延 callable を順に走らせ、report の専用 thread が空になるまで待つ。"""
    for hook in pending:
        hook()
    assert env.sched.join_daily_report(5)


def test_the_latest_close_is_the_rollover_after_which_the_market_was_open():
    close = Scheduler.latest_trading_close
    assert close(WED) == TUE_CLOSE                   # 水曜昼 → 火曜 21:00 UTC
    fri_night = datetime(2026, 7, 24, 22, 0, tzinfo=timezone.utc)
    assert close(fri_night) == datetime(2026, 7, 24, 21, 0, tzinfo=timezone.utc)
    sat = datetime(2026, 7, 25, 22, 0, tzinfo=timezone.utc)
    assert close(sat) == datetime(2026, 7, 24, 21, 0, tzinfo=timezone.utc)
    sun_open = datetime(2026, 7, 26, 22, 0, tzinfo=timezone.utc)
    assert close(sun_open) == datetime(2026, 7, 24, 21, 0, tzinfo=timezone.utc)


def test_the_first_tick_catches_up_once_and_the_callback_runs_after_the_tick(tmp_path):
    calls: list = []
    env = _report_env(tmp_path, calls)
    pending = env.sched.tick(WED)
    assert calls == []                               # tick の中では呼ばない
    _run(env, pending)
    assert calls == [TUE_CLOSE]
    _run(env, env.sched.tick(WED + timedelta(minutes=1)))
    _run(env, env.sched.tick(WED + timedelta(hours=3)))
    assert calls == [TUE_CLOSE]                      # 同じ取引日では 1 回だけ


def test_the_next_trading_day_close_fires_once_more(tmp_path):
    calls: list = []
    env = _report_env(tmp_path, calls)
    _run(env, env.sched.tick(WED))
    _run(env, env.sched.tick(WED_CLOSE + timedelta(minutes=1)))
    assert calls == [TUE_CLOSE, WED_CLOSE]


def test_a_restart_does_not_repeat_the_same_trading_day(tmp_path):
    calls: list = []
    env = _report_env(tmp_path, calls)
    _run(env, env.sched.tick(WED))
    _run(env, env.sched.tick(WED + timedelta(minutes=1)))     # 終わった日の記録を書く
    again = Scheduler(
        conn=env.conn, executor=env.executor, settings=SETTINGS,
        state_store=env.state, activity=env.sched.activity,
        bars_fn=lambda p: None, on_trade_mission=env._trade,
        on_news_cycle=env._news, on_econ_cycle=env._econ)
    again.register_daily_report(lambda close_at: calls.append(close_at))
    _run(env, again.tick(WED + timedelta(minutes=5)))
    assert again.join_daily_report(5)
    assert calls == [TUE_CLOSE]


def test_missed_days_are_caught_up_as_one_call(tmp_path):
    calls: list = []
    env = _report_env(tmp_path, calls, base=MON)
    _run(env, env.sched.tick(MON))
    assert len(calls) == 1
    _run(env, env.sched.tick(MON + timedelta(days=4)))     # 数日止まっていた
    assert len(calls) == 2


def test_weekend_boundaries_do_not_fire(tmp_path):
    calls: list = []
    sat = datetime(2026, 7, 25, 22, 0, tzinfo=timezone.utc)
    env = _report_env(tmp_path, calls, base=sat)
    _run(env, env.sched.tick(sat))
    assert calls == [datetime(2026, 7, 24, 21, 0, tzinfo=timezone.utc)]
    _run(env, env.sched.tick(sat + timedelta(hours=24)))   # 日曜 22:00 (閉場の日の終端)
    assert len(calls) == 1


def test_a_slow_report_callback_does_not_delay_the_next_tick(tmp_path):
    """callback は scheduler thread で走らない。遅い callback (ここでは止めたまま) を
    登録しても、hook 自体も次 tick も待たされず、SL / TP の判定も通る。"""
    env = Env(tmp_path)
    release = threading.Event()
    started = threading.Event()
    threads: list = []

    def slow(close_at):
        threads.append(threading.current_thread())
        started.set()
        release.wait(10)

    env.sched.register_daily_report(slow)
    sl = orders.insert(env.conn, pair="USDJPY", direction="long",
                       entry_type="market", horizon="swing", status="open",
                       now=WED, quantity=0.1, avg_fill_price=148.20,
                       stop_loss=147.80, take_profit=149.00)
    env.bars["USDJPY"] = _bar(WED, open=148.20, high=148.30, low=147.70, close=147.75)
    try:
        for hook in env.sched.tick(WED + timedelta(minutes=1)):
            hook()                       # 戻ってくる = callback を待っていない
        assert started.wait(5)
        assert threads[0] is not threading.current_thread()
        assert orders.get(env.conn, sl)["close_reason"] == "sl"
        before = time.monotonic()
        env.sched.tick(WED + timedelta(minutes=2))           # callback が走っている最中
        assert time.monotonic() - before < 2
        assert env.sched.join_daily_report(0.05) is False     # まだ走っている
    finally:
        release.set()
    assert env.sched.join_daily_report(5)


def test_queued_reports_run_in_order_on_one_thread(tmp_path):
    env = Env(tmp_path)
    gate = threading.Event()
    seen: list = []

    def cb(close_at):
        seen.append((close_at, threading.current_thread().name))
        gate.wait(10)

    first, second = datetime(2026, 7, 21, 21, 0, tzinfo=timezone.utc), WED_CLOSE
    try:
        env.sched._enqueue_daily_report(cb, first)
        env.sched._enqueue_daily_report(cb, second)
        time.sleep(0.05)
        assert [c for c, _ in seen] == [first]               # 2 本目は待つ
    finally:
        gate.set()
    assert env.sched.join_daily_report(5)
    assert [c for c, _ in seen] == [first, second]
    assert len({name for _, name in seen}) == 1


@pytest.mark.parametrize("holiday_eve, holiday_night, monday, monday_close", [
    # 12/24 の取引日が閉じた後、12/25 は休場。月曜 12/28 の終端で次に動く
    (datetime(2026, 12, 24, 22, 0, tzinfo=timezone.utc),
     datetime(2026, 12, 25, 22, 0, tzinfo=timezone.utc),
     datetime(2026, 12, 28, 12, 0, tzinfo=timezone.utc),
     datetime(2026, 12, 28, 21, 0, tzinfo=timezone.utc)),
    (datetime(2026, 12, 31, 22, 0, tzinfo=timezone.utc),
     datetime(2027, 1, 1, 22, 0, tzinfo=timezone.utc),
     datetime(2027, 1, 4, 12, 0, tzinfo=timezone.utc),
     datetime(2027, 1, 4, 21, 0, tzinfo=timezone.utc)),
])
def test_holiday_boundaries_do_not_fire_and_survive_a_restart(
        tmp_path, holiday_eve, holiday_night, monday, monday_close):
    """12/25 と 1/1 の 21:00 UTC の境界 (直前の 1 分は休場) では起動しない。
    休場日をまたぐ再起動でも、閉じた取引日の分を 1 回しか呼ばない。"""
    calls: list = []
    env = _report_env(tmp_path, calls, base=holiday_eve)
    _run(env, env.sched.tick(holiday_eve))
    closed = Scheduler.latest_trading_close(holiday_eve)
    assert calls == [closed]
    assert closed.date() == holiday_eve.date() and closed.hour == 21
    _run(env, env.sched.tick(holiday_eve + timedelta(minutes=1)))   # 記録を書く
    _run(env, env.sched.tick(holiday_night))                # 休場日の境界は数えない
    assert calls == [closed]
    again = Scheduler(
        conn=env.conn, executor=env.executor, settings=SETTINGS,
        state_store=env.state, activity=env.sched.activity,
        bars_fn=lambda p: None, on_trade_mission=env._trade,
        on_news_cycle=env._news, on_econ_cycle=env._econ)
    again.register_daily_report(lambda close_at: calls.append(close_at))
    _run(env, again.tick(monday))
    assert again.join_daily_report(5)
    assert calls == [closed]                                # 再起動後も増えない
    _run(env, again.tick(monday_close + timedelta(minutes=1)))
    assert again.join_daily_report(5)
    assert calls == [closed, monday_close]


def test_the_marker_is_written_after_the_callback_finished_not_before(tmp_path):
    env = Env(tmp_path)
    gate = threading.Event()
    started = threading.Event()

    def slow(close_at):
        started.set()
        gate.wait(10)

    env.sched.register_daily_report(slow)
    try:
        for hook in env.sched.tick(WED):
            hook()
        assert started.wait(5)
        env.sched.tick(WED + timedelta(minutes=1))        # 実行中の tick
        assert alert_state.get(env.conn, alert_state.DAILY_REPORT_KEY) is None
    finally:
        gate.set()
    assert env.sched.join_daily_report(5)
    assert alert_state.get(env.conn, alert_state.DAILY_REPORT_KEY) is None  # 次の tick が書く
    env.sched.tick(WED + timedelta(minutes=2))
    assert json.loads(alert_state.get(env.conn, alert_state.DAILY_REPORT_KEY)) == {
        "day": TUE_CLOSE.isoformat()}


def test_a_report_is_queued_at_most_once_while_it_runs_and_before_the_marker(tmp_path):
    """実行中・実行後で記録がまだでも、同じ取引日を 2 回 queue に渡さない。"""
    calls: list = []
    gate = threading.Event()

    def cb(close_at):
        calls.append(close_at)
        gate.wait(10)

    env = Env(tmp_path)
    env.sched.register_daily_report(cb)
    try:
        _run_pending_without_join(env, WED)
        for minute in (1, 2, 3):
            _run_pending_without_join(env, WED + timedelta(minutes=minute))
    finally:
        gate.set()
    assert env.sched.join_daily_report(5)
    _run(env, env.sched.tick(WED + timedelta(minutes=30)))
    assert calls == [TUE_CLOSE]


def _run_pending_without_join(env, now):
    for hook in env.sched.tick(now):
        hook()


def test_a_tick_that_raises_loses_no_report_and_the_next_tick_retries(tmp_path):
    """tick が例外で抜けて遅延 callable を失っても、記録は書かれず次 tick で再試行する。"""
    calls: list = []
    env = _report_env(tmp_path, calls)
    original = env.sched._trade_mission_due

    def boom(now):
        raise RuntimeError("tick failed after the report was scheduled")

    env.sched._trade_mission_due = boom
    with pytest.raises(RuntimeError):
        env.sched.tick(WED)                              # pending は呼び出し元へ渡らない
    assert alert_state.get(env.conn, alert_state.DAILY_REPORT_KEY) is None
    assert calls == []
    env.sched._trade_mission_due = original
    _run(env, env.sched.tick(WED + timedelta(minutes=1)))
    assert calls == [TUE_CLOSE]                          # 最低 1 回
    _run(env, env.sched.tick(WED + timedelta(minutes=2)))
    _run(env, env.sched.tick(WED + timedelta(minutes=3)))
    assert calls == [TUE_CLOSE]                          # 最大 1 回


def test_a_restart_before_the_marker_is_written_calls_the_day_again(tmp_path):
    """実行が終わってから記録までの間に再起動すれば、その日はもう一度呼ぶ (最低 1 回)。"""
    calls: list = []
    env = _report_env(tmp_path, calls)
    _run(env, env.sched.tick(WED))
    assert alert_state.get(env.conn, alert_state.DAILY_REPORT_KEY) is None
    again = Scheduler(
        conn=env.conn, executor=env.executor, settings=SETTINGS,
        state_store=env.state, activity=env.sched.activity,
        bars_fn=lambda p: None, on_trade_mission=env._trade,
        on_news_cycle=env._news, on_econ_cycle=env._econ)
    again.register_daily_report(lambda close_at: calls.append(close_at))
    _run(env, again.tick(WED + timedelta(minutes=5)))
    assert again.join_daily_report(5)
    assert calls == [TUE_CLOSE, TUE_CLOSE]


def test_a_failing_callback_is_logged_and_retried_without_a_marker(tmp_path):
    env = Env(tmp_path)
    attempts = []

    def boom(close_at):
        attempts.append(close_at)
        raise RuntimeError("report failed")

    env.sched.register_daily_report(boom)
    _run(env, env.sched.tick(WED))
    _run(env, env.sched.tick(WED + timedelta(minutes=1)))
    assert attempts == [TUE_CLOSE, TUE_CLOSE]
    assert "daily_report_cycle_error" in _log(env)
    assert alert_state.get(env.conn, alert_state.DAILY_REPORT_KEY) is None


def test_a_report_that_succeeds_on_retry_gets_its_marker(tmp_path):
    env = Env(tmp_path)
    attempts = []

    def flaky(close_at):
        attempts.append(close_at)
        if len(attempts) == 1:
            raise RuntimeError("transient")

    env.sched.register_daily_report(flaky)
    for minute in range(4):
        _run(env, env.sched.tick(WED + timedelta(minutes=minute)))
    assert attempts == [TUE_CLOSE, TUE_CLOSE]
    raw = alert_state.get(env.conn, alert_state.DAILY_REPORT_KEY)
    assert json.loads(raw)["day"] == TUE_CLOSE.isoformat()


def test_a_report_gives_up_after_three_attempts_with_a_warning(tmp_path, caplog):
    env = Env(tmp_path)
    attempts = []

    def boom(close_at):
        attempts.append(close_at)
        raise RuntimeError("report failed")

    env.sched.register_daily_report(boom)
    with caplog.at_level("WARNING"):
        for minute in range(6):
            _run(env, env.sched.tick(WED + timedelta(minutes=minute)))
    assert len(attempts) == 3
    assert alert_state.get(env.conn, alert_state.DAILY_REPORT_KEY) is None
    assert sum("gave up" in r.getMessage() for r in caplog.records) == 1


def test_no_callback_means_no_state_is_written(tmp_path):
    env = Env(tmp_path)
    _run(env, env.sched.tick(WED))
    assert alert_state.get(env.conn, alert_state.DAILY_REPORT_KEY) is None


def test_the_hook_is_not_run_while_stopping(tmp_path):
    calls: list = []
    env = _report_env(tmp_path, calls)
    stop = threading.Event()
    stop.set()
    env.sched._stop_event = stop
    _run(env, env.sched.tick(WED))
    assert calls == []


# --- dedupe の純粋部分 --------------------------------------------------------

def test_band_notice_load_ignores_unreadable_values():
    assert band_notice.load(None) == []
    assert band_notice.load("not json") == []
    assert band_notice.load('{"id": 1}') == []
    assert band_notice.load('[{"id": "x", "events": [1]}]') == []
    assert band_notice.load('[{"id": 1, "events": [2, 1, 1]}]') == [
        {"id": 1, "events": [1, 2]}]


def test_band_notice_merges_every_record_the_component_touches_into_the_first():
    records = [{"id": 1, "events": [1, 2]}, {"id": 5, "events": [5]},
               {"id": 9, "events": [9]}]
    out, notify = band_notice.record_suppression(records, 2, [2, 5, 7])
    assert notify is False
    assert out == [{"id": 1, "events": [1, 2, 5, 7]}, {"id": 9, "events": [9]}]
    out, notify = band_notice.record_suppression(out, 20, [20])
    assert notify is True and out[-1] == {"id": 20, "events": [20]}


def test_band_notice_matches_by_shared_event_not_by_component_id():
    records = [{"id": 1, "events": [1, 2]}]
    _, notify = band_notice.record_suppression(records, 2, [2, 3])   # 最小 id は変わった
    assert notify is False
    _, notify = band_notice.record_suppression(records, 1, [4])      # id が同じでも別の帯
    assert notify is True


# --- calendar の鮮度観測 ------------------------------------------------------

def test_the_calendar_observation_hook_runs_on_every_tick(tmp_path):
    env = Env(tmp_path)
    seen: list = []
    env.sched.on_calendar_observe = seen.append
    env.sched.tick(WED)
    env.sched.tick(WED + timedelta(minutes=1))
    assert seen == [WED, WED + timedelta(minutes=1)]


def test_a_failing_calendar_observation_does_not_stop_the_tick(tmp_path):
    env = Env(tmp_path)

    def boom(now):
        raise RuntimeError("freshness unreadable")

    env.sched.on_calendar_observe = boom
    oid = env.place_limit()
    _tick(env, WED)
    assert _status(env, oid) == "open"                       # 約定処理は止まらない
    assert "calendar_observe_cycle_error" in _log(env)
