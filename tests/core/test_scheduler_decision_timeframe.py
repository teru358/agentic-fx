"""判断足 cron の見送り・合流・資金保護順序 (B-2)。"""
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core import market_hours
from agentic_fx.core.contracts import Bar
from agentic_fx.core.supervisor import SubmitResult
from agentic_fx.store import cron_cursor
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db
from tests.core.test_scheduler import SETTINGS, WED, Env, _seed_decision_bar

THU = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)   # 木曜


def _settings(interval="15m", pairs=("USDJPY",)):
    datafeed = SETTINGS.datafeed.model_copy(update={
        "decision_timeframes": [interval], "primary_intervals": [interval]})
    return SETTINGS.model_copy(update={"datafeed": datafeed, "pairs": list(pairs)})


def _at(base, hour, minute, second=0, *, day=None):
    return base.replace(day=day or base.day, hour=hour, minute=minute,
                        second=second)


def _seed_bar(env, bar_time, *, interval="15m", pair="USDJPY"):
    env.conn.execute(
        "INSERT INTO ohlcv_cache (symbol, interval, bar_time, open, high, "
        "low, close, volume, source) VALUES (?,?,?,?,?,?,?,?,?)",
        (pair, interval, bar_time.isoformat(), 148.0, 148.1, 147.9, 148.0,
         1.0, "yfinance"))
    env.conn.commit()


class _SlotFake:
    """supervisor の単一スロットを tick 時刻で模す (実時間は使わない)。
    busy_until より前の呼び出しは running で拒否する。"""

    def __init__(self):
        self.now = None
        self.busy_from = None
        self.busy_until = None
        self.accepted_at = []
        self.accepted_kwargs = []

    def __call__(self, reason, **kwargs):
        if self.busy_until is not None and self.now < self.busy_until:
            return SubmitResult.rejected(
                "running", busy_since=self.busy_from.timestamp(),
                checked_at=self.now.timestamp())
        self.busy_from = self.now
        self.accepted_at.append(self.now)
        self.accepted_kwargs.append(dict(kwargs))
        return SubmitResult.accepted_with(None, checked_at=0.0)


def _env(tmp_path, base, *, interval="15m", pairs=("USDJPY",)):
    env = Env(tmp_path, base=base, seed_cron_bar=False)
    env.sched.settings = _settings(interval, pairs)
    slot = _SlotFake()
    env.sched.on_trade_mission = slot
    return env, slot


def _tick(env, slot, now):
    slot.now = now
    return env.sched.tick(now)


def _activity(env, event):
    path = env.tmp_path / "a.log"
    if not path.exists():
        return []
    rows = [line.split("\t") for line in
            path.read_text(encoding="utf-8").splitlines()]
    return [row[3] for row in rows if row[2] == event]


def test_busy_defers_once_with_elapsed_and_accepts_latest_closed_bar(tmp_path):
    env, slot = _env(tmp_path, THU)
    _seed_bar(env, _at(THU, 12, 15))
    _tick(env, slot, _at(THU, 12, 30, 31))
    assert slot.accepted_at == [_at(THU, 12, 30, 31)]
    slot.busy_until = _at(THU, 12, 47)
    # 12:30 足の確定は bar_time + 足幅 15m + grace 30 秒 = 12:45:30
    _seed_bar(env, _at(THU, 12, 30))
    _tick(env, slot, _at(THU, 12, 45, 30))
    _tick(env, slot, _at(THU, 12, 46, 30))
    assert _activity(env, "cron_mission_deferred") == [
        "USDJPY 15m bar=2026-09-24T12:30 busy (running 15.0 min)"]
    assert env.sched._cron_watermarks[("USDJPY", "15m")] == _at(THU, 12, 15)
    # 12:45 足 (確定 13:00:30) が DB に先に在っても 12:47:30 では採らない
    _seed_bar(env, _at(THU, 12, 45))
    _tick(env, slot, _at(THU, 12, 47, 30))
    assert slot.accepted_at == [_at(THU, 12, 30, 31), _at(THU, 12, 47, 30)]
    assert env.sched._cron_watermarks[("USDJPY", "15m")] == _at(THU, 12, 30)
    assert len(_activity(env, "cron_mission_deferred")) == 1


def test_busy_across_two_bars_coalesces_to_latest_counting_db_bars(tmp_path):
    env, slot = _env(tmp_path, THU)
    _seed_bar(env, _at(THU, 12, 15))
    _tick(env, slot, _at(THU, 12, 30, 31))
    slot.busy_until = _at(THU, 13, 2)
    _seed_bar(env, _at(THU, 12, 30))
    _tick(env, slot, _at(THU, 12, 45, 30))
    _seed_bar(env, _at(THU, 12, 45))
    _tick(env, slot, _at(THU, 13, 0, 30))
    _tick(env, slot, _at(THU, 13, 2, 30))
    assert slot.accepted_at == [_at(THU, 12, 30, 31), _at(THU, 13, 2, 30)]
    assert env.sched._cron_watermarks[("USDJPY", "15m")] == _at(THU, 12, 45)
    assert _activity(env, "cron_mission_coalesced") == [
        "USDJPY 15m accepted=2026-09-24T12:45 skipped=1 (2026-09-24T12:30)"]
    assert [s.split(" busy")[0] for s in _activity(env, "cron_mission_deferred")] == [
        "USDJPY 15m bar=2026-09-24T12:30", "USDJPY 15m bar=2026-09-24T12:45"]


def test_coalesced_is_not_written_when_intermediate_bar_is_missing(tmp_path):
    """足幅からの格子計算ではなく DB に実在する閉じた足だけを数える。"""
    env, slot = _env(tmp_path, THU)
    _seed_bar(env, _at(THU, 12, 15))
    _tick(env, slot, _at(THU, 12, 30, 31))
    slot.busy_until = _at(THU, 13, 2)
    _seed_bar(env, _at(THU, 12, 45))           # 12:30 足は欠けている
    _tick(env, slot, _at(THU, 13, 0, 30))
    _tick(env, slot, _at(THU, 13, 2, 30))
    assert slot.accepted_at[-1] == _at(THU, 13, 2, 30)
    assert env.sched._cron_watermarks[("USDJPY", "15m")] == _at(THU, 12, 45)
    assert _activity(env, "cron_mission_coalesced") == []


def test_cursor_advance_exception_leaves_day_close_and_sl_already_done(
        tmp_path, monkeypatch):
    """既存の実行順序を固定する: tick 内の資金保護 (day 強制決済・OPEN の
    SL 監視) は cron 受理・cursor 前進より前に実行される。cron 受理後の
    cursor 前進で例外が出ても、資金保護は例外より前に 1 回実行済みのまま
    残ることを確認する (受理ブロックの位置を変える変異でしか red に
    ならない)。"""
    env = Env(tmp_path, seed_cron_bar=False)
    day_oid = env.place_limit()
    swing_oid = orders.insert(
        env.conn, pair="USDJPY", direction="long", entry_type="market",
        horizon="swing", status="open", now=WED, quantity=0.1,
        avg_fill_price=148.20, stop_loss=147.80, take_profit=149.00)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED.replace(minute=1))
    assert orders.get(env.conn, day_oid)["status"] == "open"
    assert orders.get(env.conn, swing_oid)["status"] == "open"

    near_close = WED.replace(hour=20, minute=57)
    _seed_decision_bar(env, WED.replace(hour=19))
    env.bars["USDJPY"] = Bar("USDJPY", "1m", near_close, 147.70, 147.75,
                             147.50, 147.55, 100)

    def boom(*_args, **_kwargs):
        raise RuntimeError("injected cursor advance failure")

    monkeypatch.setattr(env.sched, "_advance_cron_watermarks", boom)
    with pytest.raises(RuntimeError, match="injected cursor advance failure"):
        env.sched.tick(near_close)
    assert env.trade_reasons == ["cron"]
    day = orders.get(env.conn, day_oid)
    assert (day["status"], day["close_reason"]) == ("closed", "day_rollover")
    swing = orders.get(env.conn, swing_oid)
    assert (swing["status"], swing["close_reason"]) == ("closed", "sl")


FRI = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)   # 金曜
SUN = datetime(2026, 9, 27, 21, 0, tzinfo=timezone.utc)   # 日曜 開場
KEY = ("USDJPY", "15m")


def _dt(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def test_friday_last_bar_committed_at_sunday_open_is_skipped_not_fired(tmp_path):
    env, slot = _env(tmp_path, FRI)
    _seed_bar(env, _at(FRI, 20, 15))
    _tick(env, slot, _at(FRI, 20, 30, 30))
    _seed_bar(env, _at(FRI, 20, 30))
    _tick(env, slot, _at(FRI, 20, 45, 30))
    assert len(slot.accepted_at) == 2
    _tick(env, slot, _at(FRI, 20, 59, 59))
    assert env.sched._was_open is True and len(slot.accepted_at) == 2
    _tick(env, slot, _at(FRI, 21, 0, 0))
    assert env.sched._was_open is False
    _tick(env, slot, _at(FRI, 21, 0, 30))
    assert len(slot.accepted_at) == 2
    # 金 20:45 足 (確定 21:00:30) は閉場で取り込まれず、日曜の開場 tick で commit される
    _seed_bar(env, _at(FRI, 20, 45))
    _tick(env, slot, _at(SUN, 21, 0, 30))
    assert len(slot.accepted_at) == 2
    assert env.sched._cron_watermarks[KEY] == _at(FRI, 20, 45)
    assert cron_cursor.load_all(env.conn) == {KEY: _at(FRI, 20, 45)}
    assert _activity(env, "cron_previous_session_bar_skipped") == [
        "USDJPY 15m bar=2026-09-25T20:45 session_start=2026-09-27T21:00"]
    _seed_bar(env, _at(SUN, 21, 0))
    _tick(env, slot, _at(SUN, 21, 15, 30))
    assert slot.accepted_at[-1] == _at(SUN, 21, 15, 30) and len(slot.accepted_at) == 3
    assert env.sched._cron_watermarks[KEY] == _at(SUN, 21, 0)


def test_bar_ending_exactly_at_session_start_is_previous_session_boundary(
        tmp_path):
    """`_drop_previous_session_bars` の
    `bar + width == session_start` 等号境界。合成データでのみ踏める境界
    (実データでは閉場時間帯に足がなく到達しにくい) — 前セッションの足として
    見送り、mission は起動せず、cursor はその足まで進む。"""
    env, slot = _env(tmp_path, SUN)
    bar = SUN - timedelta(minutes=15)      # SUN 20:45 = session_start - 足幅
    _seed_bar(env, bar)
    _tick(env, slot, _at(SUN, 21, 0, 30))
    assert slot.accepted_at == []
    assert _activity(env, "cron_previous_session_bar_skipped") == [
        "USDJPY 15m bar=2026-09-27T20:45 session_start=2026-09-27T21:00"]
    assert env.sched._cron_watermarks[KEY] == bar
    assert cron_cursor.load_all(env.conn) == {KEY: bar}


def test_holiday_not_joined_to_weekend_skips_pre_holiday_bar(tmp_path):
    base = _dt(2025, 12, 24, 20, 0)                        # 水曜
    env, slot = _env(tmp_path, base)
    _seed_bar(env, _dt(2025, 12, 24, 20, 30))
    _tick(env, slot, _dt(2025, 12, 24, 20, 45, 30))
    assert len(slot.accepted_at) == 1
    _tick(env, slot, _dt(2025, 12, 24, 21, 0, 30))         # 取引日 12/25 (祝日) へ
    _tick(env, slot, _dt(2025, 12, 25, 12, 0))
    assert env.sched._was_open is False
    _seed_bar(env, _dt(2025, 12, 24, 20, 45))
    _tick(env, slot, _dt(2025, 12, 25, 21, 0, 30))
    assert env.sched._was_open is True and len(slot.accepted_at) == 1
    assert _activity(env, "cron_previous_session_bar_skipped") == [
        "USDJPY 15m bar=2025-12-24T20:45 session_start=2025-12-25T21:00"]
    _seed_bar(env, _dt(2025, 12, 25, 21, 0))
    _tick(env, slot, _dt(2025, 12, 25, 21, 15, 30))
    assert len(slot.accepted_at) == 2
    assert env.sched._cron_watermarks[KEY] == _dt(2025, 12, 25, 21, 0)


def test_new_year_joined_to_weekend_skips_until_sunday_open(tmp_path):
    base = _dt(2026, 12, 31, 20, 0)                        # 木曜
    env, slot = _env(tmp_path, base)
    _seed_bar(env, _dt(2026, 12, 31, 20, 30))
    _tick(env, slot, _dt(2026, 12, 31, 20, 45, 30))
    assert env.sched._cron_watermarks[KEY] == _dt(2026, 12, 31, 20, 30)
    for closed in (_dt(2026, 12, 31, 21, 0, 30), _dt(2027, 1, 1, 21, 0, 30),
                   _dt(2027, 1, 2, 12, 0)):
        _tick(env, slot, closed)
        assert env.sched._was_open is False
    assert len(slot.accepted_at) == 1
    _seed_bar(env, _dt(2026, 12, 31, 20, 45))
    _tick(env, slot, _dt(2027, 1, 3, 21, 0, 30))
    assert len(slot.accepted_at) == 1
    assert env.sched._cron_watermarks[KEY] == _dt(2026, 12, 31, 20, 45)
    assert _activity(env, "cron_previous_session_bar_skipped") == [
        "USDJPY 15m bar=2026-12-31T20:45 session_start=2027-01-03T21:00"]
    _seed_bar(env, _dt(2027, 1, 3, 21, 0))
    _tick(env, slot, _dt(2027, 1, 3, 21, 15, 30))
    assert len(slot.accepted_at) == 2


def test_christmas_friday_joined_to_weekend_is_not_a_session_start(tmp_path):
    base = _dt(2026, 12, 24, 20, 0)                        # 木曜
    env, slot = _env(tmp_path, base)
    _seed_bar(env, _dt(2026, 12, 24, 20, 30))
    _tick(env, slot, _dt(2026, 12, 24, 20, 45, 30))
    for closed in (_dt(2026, 12, 24, 21, 0, 30), _dt(2026, 12, 25, 21, 0, 30),
                   _dt(2026, 12, 26, 12, 0)):
        _tick(env, slot, closed)
        assert env.sched._was_open is False
    assert len(slot.accepted_at) == 1
    _seed_bar(env, _dt(2026, 12, 24, 20, 45))
    _tick(env, slot, _dt(2026, 12, 27, 21, 0, 30))
    assert len(slot.accepted_at) == 1
    assert env.sched._cron_watermarks[KEY] == _dt(2026, 12, 24, 20, 45)
    assert _activity(env, "cron_previous_session_bar_skipped") == [
        "USDJPY 15m bar=2026-12-24T20:45 session_start=2026-12-27T21:00"]
    _seed_bar(env, _dt(2026, 12, 27, 21, 0))
    _tick(env, slot, _dt(2026, 12, 27, 21, 15, 30))
    assert len(slot.accepted_at) == 2


def test_default_1h_friday_last_bar_is_skipped_at_sunday_open(tmp_path):
    """既定 (1h) でも意図的な差分 (b): 日曜開場 tick に金曜最終足で起動しない。"""
    env = Env(tmp_path, base=_at(FRI, 19, 0), seed_cron_bar=False)
    _seed_decision_bar(env, _at(FRI, 19, 0))
    env.sched.tick(_at(FRI, 20, 0, 30))
    assert env.trade_reasons == ["cron"]
    env.sched.tick(_at(FRI, 21, 0, 30))
    _seed_decision_bar(env, _at(FRI, 20, 0))              # 確定 21:00:30 = 閉場後
    env.sched.tick(_at(SUN, 21, 0, 30))
    assert env.trade_reasons == ["cron"]
    assert _activity(env, "cron_previous_session_bar_skipped") == [
        "USDJPY 1h bar=2026-09-25T20:00 session_start=2026-09-27T21:00"]
    _seed_decision_bar(env, SUN)
    env.sched.tick(SUN + timedelta(hours=1, seconds=30))
    assert env.trade_reasons == ["cron", "cron"]


def test_session_start_exception_leaves_day_close_and_sl_already_done(
        tmp_path, monkeypatch):
    env = Env(tmp_path, seed_cron_bar=False)
    day_oid = env.place_limit()
    swing_oid = orders.insert(
        env.conn, pair="USDJPY", direction="long", entry_type="market",
        horizon="swing", status="open", now=WED, quantity=0.1,
        avg_fill_price=148.20, stop_loss=147.80, take_profit=149.00)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED.replace(minute=1))
    near_close = WED.replace(hour=20, minute=57)
    _seed_decision_bar(env, WED.replace(hour=19))
    env.bars["USDJPY"] = Bar("USDJPY", "1m", near_close, 147.70, 147.75,
                             147.50, 147.55, 100)

    def boom(_now):
        raise RuntimeError("injected session_start failure")

    monkeypatch.setattr(market_hours, "session_start", boom)
    with pytest.raises(RuntimeError, match="injected session_start failure"):
        env.sched.tick(near_close)
    assert env.trade_reasons == []
    day = orders.get(env.conn, day_oid)
    assert (day["status"], day["close_reason"]) == ("closed", "day_rollover")
    swing = orders.get(env.conn, swing_oid)
    assert (swing["status"], swing["close_reason"]) == ("closed", "sl")


def _fail_cursor_writes(conn):
    conn.executescript(
        "CREATE TRIGGER fail_cc_ins BEFORE INSERT ON cron_cursor "
        "BEGIN SELECT RAISE(ABORT, 'injected cron_cursor failure'); END;"
        "CREATE TRIGGER fail_cc_upd BEFORE UPDATE ON cron_cursor "
        "BEGIN SELECT RAISE(ABORT, 'injected cron_cursor failure'); END;")


def _allow_cursor_writes(conn):
    conn.executescript("DROP TRIGGER fail_cc_ins; DROP TRIGGER fail_cc_upd;")


def _preload_cursor(tmp_path, rows):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    for (pair, interval), bar_time in rows.items():
        cron_cursor.upsert(conn, pair, interval, bar_time, now=THU)
    conn.close()


def test_restart_restores_cursor_and_does_not_rejudge_same_bar(tmp_path):
    env, slot = _env(tmp_path, THU)
    _seed_bar(env, _at(THU, 12, 45))
    _tick(env, slot, _at(THU, 13, 0, 41))
    assert slot.accepted_at == [_at(THU, 13, 0, 41)]
    assert cron_cursor.load_all(env.conn) == {KEY: _at(THU, 12, 45)}
    assert env.conn.execute(
        "SELECT updated_at FROM cron_cursor").fetchone()[0] == (
        "2026-09-24T13:00:41+00:00")
    restarted, slot2 = _env(tmp_path, _at(THU, 13, 8, 43))
    _tick(restarted, slot2, _at(THU, 13, 8, 44))
    assert slot2.accepted_at == []
    _seed_bar(restarted, _at(THU, 13, 0))
    _tick(restarted, slot2, _at(THU, 13, 15, 30))
    assert slot2.accepted_at == [_at(THU, 13, 15, 30)]


def test_cursor_write_failure_is_retried_in_closed_ticks_once_per_tick(tmp_path):
    env, slot = _env(tmp_path, FRI)
    _seed_bar(env, _at(FRI, 20, 15))
    _tick(env, slot, _at(FRI, 20, 30, 30))
    _fail_cursor_writes(env.conn)
    _seed_bar(env, _at(FRI, 20, 30))
    _tick(env, slot, _at(FRI, 20, 45, 41))
    assert len(slot.accepted_at) == 2
    assert env.sched._cron_watermarks[KEY] == _at(FRI, 20, 30)
    assert env.sched._dirty_cron_cursor == {KEY}
    assert not env.conn.in_transaction
    assert len(_activity(env, "cron_cursor_write_failed")) == 1
    for count, minute in ((2, 1), (3, 2)):
        _tick(env, slot, _at(FRI, 21, minute, 30))   # 閉場 tick (早期 return)
        assert env.sched._was_open is False
        assert len(_activity(env, "cron_cursor_write_failed")) == count
        assert not env.conn.in_transaction
    assert cron_cursor.load_all(env.conn)[KEY] == _at(FRI, 20, 15)
    _allow_cursor_writes(env.conn)
    _tick(env, slot, _at(FRI, 21, 3, 30))
    assert env.sched._dirty_cron_cursor == set()
    assert cron_cursor.load_all(env.conn)[KEY] == _at(FRI, 20, 30)
    _tick(env, slot, _at(FRI, 21, 4, 30))
    assert len(_activity(env, "cron_cursor_write_failed")) == 3


def test_cursor_write_failure_for_two_pairs_in_the_same_tick_logs_both(
        tmp_path):
    """`_cron_cursor_failure_logged_this_tick` は (pair, interval) ごとの
    集合であるべき — tick 冒頭でクリアし、同一 tick 内で異なる key の
    失敗はそれぞれ 1 行ずつ記録する (旧実装は tick 単位の bool だったため
    2 件目以降の pair が無音になっていた)。"""
    env, slot = _env(tmp_path, THU, pairs=("USDJPY", "EURUSD"))
    _fail_cursor_writes(env.conn)
    _seed_bar(env, _at(THU, 12, 45), pair="USDJPY")
    _seed_bar(env, _at(THU, 12, 45), pair="EURUSD")
    _tick(env, slot, _at(THU, 13, 0, 41))
    rows = _activity(env, "cron_cursor_write_failed")
    assert len(rows) == 2, (
        f"同一 tick 内の 2 pair 失敗が 2 行になっていない: {rows}")
    assert any("USDJPY" in r for r in rows), rows
    assert any("EURUSD" in r for r in rows), rows
    assert env.sched._dirty_cron_cursor == {
        ("USDJPY", "15m"), ("EURUSD", "15m")}
    # tick ローカルのフラグ (`now` の同一性ではない) なので、同じ `now` で
    # tick が再度呼ばれても無音化されず、また 2 行 (計 4 行) 記録される
    # — 既存の再入テストの契約 (同一 `now` の 2 回目を無音にしない) を
    # 2 pair 構成でも維持する。
    _tick(env, slot, _at(THU, 13, 0, 41))
    assert len(_activity(env, "cron_cursor_write_failed")) == 4


def test_cursor_write_failure_is_retried_while_outage_is_degraded(tmp_path):
    state = {"value": "ready"}
    env = Env(tmp_path, base=THU, seed_cron_bar=False,
              state_fn=lambda: state["value"])
    env.sched.settings = _settings()
    slot = _SlotFake()
    env.sched.on_trade_mission = slot
    _fail_cursor_writes(env.conn)
    _seed_bar(env, _at(THU, 12, 45))
    _tick(env, slot, _at(THU, 13, 0, 41))
    assert env.sched._dirty_cron_cursor == {KEY}
    state["value"] = "degraded"
    _allow_cursor_writes(env.conn)
    _tick(env, slot, _at(THU, 13, 1, 41))
    assert env.sched._dirty_cron_cursor == set()
    assert cron_cursor.load_all(env.conn) == {KEY: _at(THU, 12, 45)}
    assert len(slot.accepted_at) == 1


def test_dirty_cursor_retry_runs_even_when_state_fn_raises_in_closed_tick(
        tmp_path):
    """`_run_hooks` が例外を出しても `_retry_dirty_cron_cursor`
    は独立した try/finally で必ず実行される。閉場 tick で state_fn が
    例外を投げると、閉場分岐の呼び出しと `_run_hooks` (signal maintenance
    ゲート) の呼び出しの両方で伝播する — 再試行はどちらの経路でも
    スキップされてはいけない。"""
    state = {"raise": False}

    def state_fn():
        if state["raise"]:
            raise RuntimeError("state probe failed")
        return "ready"

    env = Env(tmp_path, base=FRI, seed_cron_bar=False, state_fn=state_fn,
              on_signal_maintenance=lambda now: None)
    env.sched.settings = _settings()
    slot = _SlotFake()
    env.sched.on_trade_mission = slot
    _fail_cursor_writes(env.conn)
    _seed_bar(env, _at(FRI, 20, 15))
    _tick(env, slot, _at(FRI, 20, 30, 30))
    assert env.sched._dirty_cron_cursor == {KEY}
    before = len(_activity(env, "cron_cursor_write_failed"))

    state["raise"] = True
    with pytest.raises(RuntimeError, match="state probe failed"):
        _tick(env, slot, _at(FRI, 21, 2, 30))   # 閉場 tick
    assert env.sched._was_open is False
    # 再試行は走った (書込みはまだ失敗するので dirty のまま + 失敗ログ +1)
    assert env.sched._dirty_cron_cursor == {KEY}
    assert len(_activity(env, "cron_cursor_write_failed")) == before + 1


def test_cursor_write_failure_is_logged_once_per_tick_even_with_retry_and_accept(
        tmp_path):
    """同じ tick で finally の再試行と新しい受理の upsert が両方失敗しても
    cron_cursor_write_failed は 1 行。"""
    env, slot = _env(tmp_path, THU)
    _fail_cursor_writes(env.conn)
    _seed_bar(env, _at(THU, 12, 45))
    _tick(env, slot, _at(THU, 13, 0, 41))
    assert len(_activity(env, "cron_cursor_write_failed")) == 1
    _seed_bar(env, _at(THU, 13, 0))
    _tick(env, slot, _at(THU, 13, 15, 41))          # 再試行 + 受理の 2 回失敗
    assert len(slot.accepted_at) == 2
    assert len(_activity(env, "cron_cursor_write_failed")) == 2
    assert env.sched._dirty_cron_cursor == {KEY}
    assert env.sched._cron_watermarks[KEY] == _at(THU, 13, 0)


def test_cursor_write_failure_is_logged_on_each_tick_call_even_with_same_now(
        tmp_path):
    """I2: `now` の値が同一でも、tick の呼び出しごとに記録する (tick
    ローカルのフラグは tick() の呼び出し冒頭で必ず False にリセットする —
    `now` 同士の同一性で判定すると、同じ `now` で tick を 2 回呼んだ場合に
    2 回目が無音になる)。"""
    env, slot = _env(tmp_path, THU)
    _fail_cursor_writes(env.conn)
    _seed_bar(env, _at(THU, 12, 45))
    now = _at(THU, 13, 0, 41)
    _tick(env, slot, now)
    assert len(_activity(env, "cron_cursor_write_failed")) == 1
    _tick(env, slot, now)          # 同一 now で tick を再度呼ぶ
    assert len(_activity(env, "cron_cursor_write_failed")) == 2


def test_unpersisted_cursor_is_rejudged_after_restart_at_least_once(tmp_path):
    env, slot = _env(tmp_path, THU)
    _seed_bar(env, _at(THU, 12, 30))
    _tick(env, slot, _at(THU, 12, 45, 41))
    _fail_cursor_writes(env.conn)
    _seed_bar(env, _at(THU, 12, 45))
    _tick(env, slot, _at(THU, 13, 0, 41))
    assert env.sched._dirty_cron_cursor == {KEY}
    restarted, slot2 = _env(tmp_path, _at(THU, 13, 8, 43))
    _allow_cursor_writes(restarted.conn)
    _tick(restarted, slot2, _at(THU, 13, 8, 44))
    assert slot2.accepted_at == [_at(THU, 13, 8, 44)]     # 12:45 を再判断
    assert cron_cursor.load_all(restarted.conn)[KEY] == _at(THU, 12, 45)


def test_future_cursor_holds_only_that_pair_and_resumes_when_w_catches_up(tmp_path):
    _preload_cursor(tmp_path, {("USDJPY", "15m"): _at(THU, 13, 15),
                               ("EURUSD", "15m"): _at(THU, 12, 45)})
    env, slot = _env(tmp_path, THU, pairs=("USDJPY", "EURUSD"))
    for pair in ("USDJPY", "EURUSD"):
        _seed_bar(env, _at(THU, 13, 0), pair=pair)
    _tick(env, slot, _at(THU, 13, 15, 30))
    assert slot.accepted_at == [_at(THU, 13, 15, 30)]
    assert env.sched._due_cron_watermarks == {("EURUSD", "15m"): _at(THU, 13, 0)}
    assert env.sched._cron_watermarks[("USDJPY", "15m")] == _at(THU, 13, 15)
    assert _activity(env, "cron_cursor_future_watermark") == [
        "USDJPY 15m L=2026-09-24T13:15 W=2026-09-24T13:00 "
        "(この pair の cron 判定を保留、W が L に追いつくと自動で再開)"]
    _tick(env, slot, _at(THU, 13, 16, 30))
    assert len(_activity(env, "cron_cursor_future_watermark")) == 1
    for pair in ("USDJPY", "EURUSD"):
        _seed_bar(env, _at(THU, 13, 15), pair=pair)
    _tick(env, slot, _at(THU, 13, 30, 30))
    assert _activity(env, "cron_cursor_future_watermark_resolved") == [
        "USDJPY 15m L=2026-09-24T13:15 W=2026-09-24T13:15 (cron 判定を再開)"]
    assert env.sched._due_cron_watermarks == {("EURUSD", "15m"): _at(THU, 13, 15)}
    _seed_bar(env, _at(THU, 13, 30), pair="USDJPY")
    _tick(env, slot, _at(THU, 13, 45, 30))
    assert env.sched._due_cron_watermarks == {("USDJPY", "15m"): _at(THU, 13, 30)}
    assert len(slot.accepted_at) == 3
    assert len(_activity(env, "cron_cursor_future_watermark_resolved")) == 1


def test_cursor_written_by_another_connection_while_running_is_picked_up_next_tick(
        tmp_path):
    """I3: 稼働中の DB cursor 変更 (別接続・手動編集等) を、毎 tick
    (受理判定の前) に読み直して拾う。プロセス自身が受理して進めた cursor
    だけでは検出できない (別接続からの書き込みはメモリに一切反映されない
    ため) — 起動後、別接続で `cron_cursor` を未来値へ upsert すると、
    次 tick でその pair が hold され、W が追いつくと自動復帰する。"""
    env, slot = _env(tmp_path, THU, pairs=("USDJPY", "EURUSD"))
    for pair in ("USDJPY", "EURUSD"):
        _seed_bar(env, _at(THU, 12, 45), pair=pair)
    _tick(env, slot, _at(THU, 13, 0, 30))
    assert slot.accepted_at == [_at(THU, 13, 0, 30)]
    assert env.sched._cron_watermarks[("USDJPY", "15m")] == _at(THU, 12, 45)

    # 別接続で USDJPY の cron_cursor を未来値へ書き換える (稼働中の手動更新等)。
    other = connect(tmp_path / "t.db")
    cron_cursor.upsert(other, "USDJPY", "15m", _at(THU, 13, 15),
                       now=_at(THU, 13, 0, 30))
    other.close()

    for pair in ("USDJPY", "EURUSD"):
        _seed_bar(env, _at(THU, 13, 0), pair=pair)
    _tick(env, slot, _at(THU, 13, 15, 30))
    assert env.sched._due_cron_watermarks == {("EURUSD", "15m"): _at(THU, 13, 0)}
    assert env.sched._cron_watermarks[("USDJPY", "15m")] == _at(THU, 13, 15)
    assert _activity(env, "cron_cursor_future_watermark") == [
        "USDJPY 15m L=2026-09-24T13:15 W=2026-09-24T13:00 "
        "(この pair の cron 判定を保留、W が L に追いつくと自動で再開)"]
    _tick(env, slot, _at(THU, 13, 16, 30))
    assert len(_activity(env, "cron_cursor_future_watermark")) == 1

    for pair in ("USDJPY", "EURUSD"):
        _seed_bar(env, _at(THU, 13, 15), pair=pair)
    _tick(env, slot, _at(THU, 13, 30, 30))
    assert _activity(env, "cron_cursor_future_watermark_resolved") == [
        "USDJPY 15m L=2026-09-24T13:15 W=2026-09-24T13:15 (cron 判定を再開)"]
    assert env.sched._due_cron_watermarks == {("EURUSD", "15m"): _at(THU, 13, 15)}


def test_restored_cursor_without_observed_w_is_left_unverified(tmp_path):
    _preload_cursor(tmp_path, {("USDJPY", "15m"): _at(THU, 12, 45),
                               ("EURUSD", "15m"): _at(THU, 12, 45)})
    state = {"value": "ready"}
    env = Env(tmp_path, base=THU, seed_cron_bar=False,
              state_fn=lambda: state["value"])
    env.sched.settings = _settings(pairs=("USDJPY", "EURUSD"))
    slot = _SlotFake()
    env.sched.on_trade_mission = slot
    _seed_bar(env, _at(THU, 13, 0), pair="EURUSD")        # USDJPY の W は未観測
    _tick(env, slot, _at(THU, 13, 15, 30))
    assert slot.accepted_at == [_at(THU, 13, 15, 30)]
    assert env.sched._due_cron_watermarks == {("EURUSD", "15m"): _at(THU, 13, 0)}
    assert env.sched._cron_watermarks[("USDJPY", "15m")] == _at(THU, 12, 45)
    assert _activity(env, "cron_cursor_future_watermark") == []
    state["value"] = "degraded"
    _seed_bar(env, _at(THU, 13, 0), pair="USDJPY")
    _tick(env, slot, _at(THU, 13, 16, 30))
    assert len(slot.accepted_at) == 1
    state["value"] = "ready"
    _tick(env, slot, _at(THU, 13, 17, 30))
    assert slot.accepted_at[-1] == _at(THU, 13, 17, 30)
    assert env.sched._due_cron_watermarks == {("USDJPY", "15m"): _at(THU, 13, 0)}


def test_cron_passes_only_advanced_pairs_as_immutable_snapshot(tmp_path):
    env, slot = _env(tmp_path, THU, pairs=("USDJPY", "EURUSD"))
    _seed_bar(env, _at(THU, 12, 30), pair="USDJPY")
    _seed_bar(env, _at(THU, 12, 45), pair="EURUSD")
    _tick(env, slot, _at(THU, 13, 0, 41))
    first = slot.accepted_kwargs[0]["decision_bars"]
    assert dict(first) == {("USDJPY", "15m"): _at(THU, 12, 30),
                           ("EURUSD", "15m"): _at(THU, 12, 45)}
    with pytest.raises(TypeError):
        first[("USDJPY", "15m")] = _at(THU, 13, 0)
    _seed_bar(env, _at(THU, 12, 45), pair="USDJPY")        # EURUSD は進まない
    _tick(env, slot, _at(THU, 13, 1, 41))
    assert dict(slot.accepted_kwargs[1]["decision_bars"]) == {
        ("USDJPY", "15m"): _at(THU, 12, 45)}
    assert dict(first) == {("USDJPY", "15m"): _at(THU, 12, 30),
                           ("EURUSD", "15m"): _at(THU, 12, 45)}


def test_signal_mission_gets_empty_decision_bars(tmp_path):
    env = Env(tmp_path, base=THU, seed_cron_bar=False,
              signal_due_fn=lambda now: True)
    slot = _SlotFake()
    env.sched.on_trade_mission = slot
    _tick(env, slot, _at(THU, 12, 1))
    assert len(slot.accepted_kwargs) == 1
    assert dict(slot.accepted_kwargs[0]["decision_bars"]) == {}


def _seed_source_bar(env, bar_time, source, *, interval="15m", pair="USDJPY"):
    env.conn.execute(
        "INSERT INTO ohlcv_cache (symbol, interval, bar_time, open, high, "
        "low, close, volume, source) VALUES (?,?,?,?,?,?,?,?,?)",
        (pair, interval, bar_time.isoformat(), 148.0, 148.1, 147.9, 148.0,
         1.0, source))
    env.conn.commit()


def test_busy_defers_every_advanced_pair_once(tmp_path):
    env, slot = _env(tmp_path, THU, pairs=("USDJPY", "EURUSD"))
    for pair in ("USDJPY", "EURUSD"):
        _seed_bar(env, _at(THU, 12, 15), pair=pair)
    _tick(env, slot, _at(THU, 12, 30, 31))
    slot.busy_until = _at(THU, 12, 47)
    for pair in ("USDJPY", "EURUSD"):
        _seed_bar(env, _at(THU, 12, 30), pair=pair)
    _tick(env, slot, _at(THU, 12, 45, 30))
    _tick(env, slot, _at(THU, 12, 46, 30))
    assert _activity(env, "cron_mission_deferred") == [
        "EURUSD 15m bar=2026-09-24T12:30 busy (running 15.0 min)",
        "USDJPY 15m bar=2026-09-24T12:30 busy (running 15.0 min)"]


def test_mt5_primary_uses_mt5_live_rows_for_due_and_coalesced(tmp_path):
    """primary=mt5 では storage source "mt5-live" の行だけで due と合流本数を
    判定する。別 source (yfinance) の中間足は skipped に数えない。"""
    env, slot = _env(tmp_path, THU)
    datafeed = env.sched.settings.datafeed.model_copy(update={"primary": "mt5"})
    env.sched.settings = env.sched.settings.model_copy(update={"datafeed": datafeed})
    _seed_source_bar(env, _at(THU, 12, 15), "mt5-live")
    _tick(env, slot, _at(THU, 12, 30, 31))
    assert slot.accepted_at == [_at(THU, 12, 30, 31)]
    slot.busy_until = _at(THU, 13, 2)
    _seed_source_bar(env, _at(THU, 12, 30), "yfinance")    # 別 source の中間足
    _seed_source_bar(env, _at(THU, 12, 45), "mt5-live")
    _tick(env, slot, _at(THU, 13, 0, 30))
    _tick(env, slot, _at(THU, 13, 2, 30))
    assert slot.accepted_at == [_at(THU, 12, 30, 31), _at(THU, 13, 2, 30)]
    assert env.sched._cron_watermarks[KEY] == _at(THU, 12, 45)
    assert _activity(env, "cron_mission_coalesced") == []


def test_reload_keeps_newer_in_memory_cursor_when_persist_failed(tmp_path):
    """永続化に失敗して表の L がメモリ L より古い間も、毎 tick の読み直しで
    メモリ L を巻き戻さない (同じ足を同一プロセス内で再判断しない)。"""
    env, slot = _env(tmp_path, THU)
    _seed_bar(env, _at(THU, 12, 15))
    _tick(env, slot, _at(THU, 12, 30, 41))
    assert cron_cursor.load_all(env.conn) == {KEY: _at(THU, 12, 15)}
    _fail_cursor_writes(env.conn)
    _seed_bar(env, _at(THU, 12, 30))
    _tick(env, slot, _at(THU, 12, 45, 41))
    assert env.sched._dirty_cron_cursor == {KEY}
    _tick(env, slot, _at(THU, 12, 46, 41))
    _tick(env, slot, _at(THU, 12, 47, 41))
    assert slot.accepted_at == [_at(THU, 12, 30, 41), _at(THU, 12, 45, 41)]
    assert env.sched._cron_watermarks[KEY] == _at(THU, 12, 30)
    assert cron_cursor.load_all(env.conn) == {KEY: _at(THU, 12, 15)}


def test_reload_failure_is_fail_safe_and_does_not_drop_the_tick(
        tmp_path, monkeypatch):
    """`_reload_cron_cursor` の読み直し (`cron_cursor.load_all`) が失敗しても
    例外は tick から漏れず、メモリ L はそのまま維持される。W > L の pair は
    その tick で通常どおり受理される (`_restore_cron_cursor` と同じ
    fail-safe 方針)。"""
    env, slot = _env(tmp_path, THU)
    _seed_bar(env, _at(THU, 12, 15))
    _tick(env, slot, _at(THU, 12, 30, 41))
    assert slot.accepted_at == [_at(THU, 12, 30, 41)]
    assert env.sched._cron_watermarks[KEY] == _at(THU, 12, 15)

    from agentic_fx.store import cron_cursor as cron_cursor_mod

    def boom(conn):
        raise RuntimeError("injected cron cursor reload failure")

    monkeypatch.setattr(cron_cursor_mod, "load_all", boom)
    _seed_bar(env, _at(THU, 12, 30))
    _tick(env, slot, _at(THU, 12, 45, 41))  # 例外が漏れないこと
    assert env.sched._cron_watermarks[KEY] == _at(THU, 12, 30)
    assert slot.accepted_at == [_at(THU, 12, 30, 41), _at(THU, 12, 45, 41)]


def test_coalesced_count_failure_does_not_lose_acceptance_or_persist(
        tmp_path, monkeypatch):
    """`_record_cron_coalesced` の観測 (`ohlcv.load_cache_bars`) が失敗しても
    受理・cursor 前進・永続化は成立し、例外は tick から漏れない。
    `cron_mission_coalesced` は書かれない (観測できなかった分だけの縮退)。"""
    env, slot = _env(tmp_path, THU)
    _seed_bar(env, _at(THU, 12, 15))
    _tick(env, slot, _at(THU, 12, 30, 31))
    slot.busy_until = _at(THU, 13, 2)
    _seed_bar(env, _at(THU, 12, 30))
    _tick(env, slot, _at(THU, 12, 45, 30))
    _seed_bar(env, _at(THU, 12, 45))

    from agentic_fx.store import ohlcv as ohlcv_mod

    def boom(*args, **kwargs):
        raise RuntimeError("injected coalesced count failure")

    monkeypatch.setattr(ohlcv_mod, "load_cache_bars", boom)
    _tick(env, slot, _at(THU, 13, 0, 30))
    _tick(env, slot, _at(THU, 13, 2, 30))  # 例外が漏れないこと
    assert slot.accepted_at == [_at(THU, 12, 30, 31), _at(THU, 13, 2, 30)]
    assert env.sched._cron_watermarks[KEY] == _at(THU, 12, 45)
    assert cron_cursor.load_all(env.conn) == {KEY: _at(THU, 12, 45)}
    assert _activity(env, "cron_mission_coalesced") == []


def test_closed_baseline_persists_advanced_cursor(tmp_path):
    """閉場中の baseline が前進させた cursor は同じ tick で表に書く。"""
    env, slot = _env(tmp_path, FRI)
    _seed_bar(env, _at(FRI, 20, 15))
    _tick(env, slot, _at(FRI, 20, 30, 30))
    _tick(env, slot, _at(FRI, 21, 0, 30))
    assert env.sched._was_open is False
    assert cron_cursor.load_all(env.conn) == {KEY: _at(FRI, 20, 15)}
    _seed_bar(env, _at(FRI, 20, 45))           # 閉場後に遅れて commit された足
    _tick(env, slot, _at(FRI, 21, 5, 30))
    assert env.sched._cron_watermarks[KEY] == _at(FRI, 20, 45)
    assert cron_cursor.load_all(env.conn) == {KEY: _at(FRI, 20, 45)}
    assert len(slot.accepted_at) == 1


def test_cursor_restore_failure_is_recorded_and_starts_empty(tmp_path, monkeypatch):
    def broken(conn):
        raise RuntimeError("cursor table unreadable")

    monkeypatch.setattr(cron_cursor, "load_all", broken)
    env, _slot = _env(tmp_path, THU)
    assert env.sched._cron_watermarks == {}
    assert _activity(env, "cron_cursor_restore_failed") == [
        "RuntimeError: cursor table unreadable — 空の cursor で起動 "
        "(最新の確定足 1 本で発火し得る)"]


def test_previous_session_bar_of_one_pair_does_not_hold_back_other_pair(tmp_path):
    """同じ tick で USDJPY は前セッションの足、EURUSD は当セッションの足が
    due のとき、USDJPY だけ skip して EURUSD の足で起動する。"""
    env, slot = _env(tmp_path, FRI, pairs=("USDJPY", "EURUSD"))
    for pair in ("USDJPY", "EURUSD"):
        _seed_bar(env, _at(FRI, 20, 15), pair=pair)
    _tick(env, slot, _at(FRI, 20, 30, 30))
    assert len(slot.accepted_at) == 1
    _seed_bar(env, _at(FRI, 20, 45), pair="USDJPY")   # 遅れて commit された金曜足
    _seed_bar(env, _at(SUN, 21, 0), pair="EURUSD")
    _tick(env, slot, _at(SUN, 21, 15, 30))
    assert slot.accepted_at[-1] == _at(SUN, 21, 15, 30)
    assert dict(slot.accepted_kwargs[-1]["decision_bars"]) == {
        ("EURUSD", "15m"): _at(SUN, 21, 0)}
    assert env.sched._cron_watermarks == {
        ("USDJPY", "15m"): _at(FRI, 20, 45), ("EURUSD", "15m"): _at(SUN, 21, 0)}
    assert _activity(env, "cron_previous_session_bar_skipped") == [
        "USDJPY 15m bar=2026-09-25T20:45 session_start=2026-09-27T21:00"]
