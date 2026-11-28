"""observe が足の接触検査・起動待ち・移行確認・観測の途切れを、保存した facts から決める。

遷移の規則そのもの (純関数) は test_outage_transition_table.py。ここは DB・注文・足を実際に置いた
observe の配線を、日付と価格を決めた実時刻の例で確かめる (水曜の取引時間中)。損切り・指値は足の価格
(100 付近) から離して置く。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.outage import IngestTickReport, OutageStateMachine
from agentic_fx.store import orders, outage_facts
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.ohlcv import upsert_cache_bars

UTC = timezone.utc
PAIR = "USDJPY"
KEY = (PAIR, "1m")
SRC = "mt5-live"
WIDTHS = {"1m": timedelta(minutes=1)}
GRACE = timedelta(seconds=30)


def _at(hour, minute, second=0):
    return datetime(2026, 9, 30, hour, minute, second, tzinfo=UTC)


def _rig(tmp_path, **kw):
    conn = connect(tmp_path / "inspect.db")
    init_db(conn)
    machine = OutageStateMachine(conn, hard_keys=frozenset({KEY}), interval_widths=WIDTHS,
                                 grace=GRACE, storage_source=SRC, **kw)
    return conn, machine


def _bar(conn, at, *, low=101.0, high=101.0, open_=101.0, close=101.0):
    upsert_cache_bars(conn, [Bar(PAIR, "1m", at, open_, high, low, close, 1)], source=SRC)
    conn.commit()


def _bars(conn, start, end, *, touch=None):
    """[start, end] の 1m 足 (価格 101)。`touch` の時刻の足だけ安値 99・高値 110 まで広げる。"""
    at = start
    while at <= end:
        if at == touch:
            _bar(conn, at, low=99.0, high=110.0)
        else:
            _bar(conn, at)
        at += timedelta(minutes=1)


def _order(conn, *, status="open", direction="long", created=_at(9, 30), stop_loss=100.0,
           take_profit=None, requested_price=None, closed_at=None, quantity=None, pair=PAIR):
    extra = {}
    if status in ("open", "closed"):
        extra["filled_at"] = created.isoformat()
    if closed_at is not None:
        extra["closed_at"] = closed_at.isoformat()
    if quantity is not None:
        extra["quantity"] = quantity
    oid = orders.insert(conn, pair=pair, direction=direction, entry_type="limit",
                        horizon="swing", status=status, now=created, stop_loss=stop_loss,
                        take_profit=take_profit, requested_price=requested_price, **extra)
    conn.commit()
    return oid


def _state(conn, machine, state, *, epoch=3, since=None, observed=None, stall=None,
           updated=_at(10, 0), **cols):
    """永続状態を直接用意する。`observed` があれば、その水位まで observe 済みの facts も置く。"""
    machine._ensure_row(updated)
    cols = dict(state=state, epoch=epoch, updated_at=updated.isoformat(),
                restricted_since=None if since is None else since.isoformat(), **cols)
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE datafeed_outage_state SET {sets} WHERE id=1", tuple(cols.values()))
    if observed is not None or stall is not None:
        outage_facts.save_key_facts(conn, PAIR, "1m", SRC, observed_through=observed,
                                    stall_start=stall, last_attempt_ok=True, now=updated)
    conn.commit()
    machine._reset_ready_streak = False


def _ok():
    return IngestTickReport(attempted=frozenset({KEY}), succeeded=frozenset({KEY}),
                            failed=frozenset(), deferred=frozenset(), empty=frozenset())


def _failed():
    return IngestTickReport(attempted=frozenset({KEY}), succeeded=frozenset(),
                            failed=frozenset({(KEY, "ConnectError")}), deferred=frozenset(),
                            empty=frozenset())


def _contacts(conn):
    return [(r["epoch"], r["order_id"], r["bar_time"], r["kind"]) for r in conn.execute(
        "SELECT epoch, order_id, bar_time, kind FROM datafeed_outage_contacts "
        "ORDER BY bar_time, order_id")]


def _notices(conn):
    return [(r["epoch"], r["notice_state"], r["request_key"]) for r in conn.execute(
        "SELECT epoch, notice_state, request_key FROM datafeed_outage_notices "
        "ORDER BY occurred_at, rowid")]


def _row(conn):
    return dict(conn.execute("SELECT * FROM datafeed_outage_state WHERE id=1").fetchone())


def _tick(conn, machine, now, report=None, *, bar=True):
    """tick ごとに足が 1 本進む観測 (now - 2 分の足を入れる)。"""
    if bar:
        _bar(conn, (now - timedelta(minutes=2)).replace(second=0))
    return machine.observe(now, report or _ok())


# ---- N: non-ready の間に届いた足の検査 (一括到着) ---------------------------------

def test_a_bulk_arrival_after_a_long_outage_is_checked_and_a_touch_stops_the_recovery(tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "degraded", observed=_at(10, 0), confirmed=1,
           pending_human_confirmation=1, updated=_at(10, 41))
    oid = _order(conn)
    _bars(conn, _at(10, 1), _at(10, 40), touch=_at(10, 20))

    assert machine.observe(_at(10, 42), _ok()) == "degraded"

    assert _contacts(conn) == [(3, oid, _at(10, 20).isoformat(), "sl")]
    assert ("stopped", "contact") in [(s, k) for _e, s, k in _notices(conn)]
    row = _row(conn)
    assert row["pending_human_confirmation"] == 1 and row["ready_streak"] == 0
    # 建玉が消えて flat になっても、接触の確認 (ack) が済むまで自動では戻らない
    conn.execute("UPDATE orders SET status='closed', closed_at=?", (_at(10, 43).isoformat(),))
    conn.commit()
    for minute in (43, 44, 45, 46):
        assert _tick(conn, machine, _at(10, minute)) == "degraded"
    assert _row(conn)["ready_streak"] == 0


def test_without_a_touch_the_same_bulk_arrival_recovers_under_the_ordinary_flat_rule(tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "degraded", observed=_at(10, 0), confirmed=1,
           pending_human_confirmation=1, updated=_at(10, 41))
    _order(conn, status="closed", closed_at=_at(10, 5))     # 全 exposure ゼロ
    _bars(conn, _at(10, 1), _at(10, 40))

    assert machine.observe(_at(10, 42), _ok()) == "degraded"
    assert _contacts(conn) == []
    states = [_tick(conn, machine, _at(10, minute)) for minute in (43, 44)]
    assert states == ["degraded", "ready"]                 # 健全 3 tick 目 (10:42 を含む)
    assert _row(conn)["pending_human_confirmation"] == 0


def test_an_acknowledge_is_required_even_when_flat_and_it_clears_the_contact(tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "degraded", observed=_at(10, 0), updated=_at(10, 41))
    oid = _order(conn, status="closed", closed_at=_at(10, 30))   # 接触時すでに flat
    _bars(conn, _at(10, 1), _at(10, 40), touch=_at(10, 20))
    assert machine.observe(_at(10, 42), _ok()) == "degraded"
    assert _contacts(conn)[0][1] == oid

    machine.request_resume(_at(10, 42, 10), acknowledge=False)
    assert _tick(conn, machine, _at(10, 43)) == "degraded"
    assert _row(conn)["resume_last_result"] == "rejected_unprocessed"
    machine.request_resume(_at(10, 43, 10), acknowledge=True)
    assert _tick(conn, machine, _at(10, 44)) == "ready"
    assert _contacts(conn) == []
    assert _row(conn)["resume_last_result"] == "accepted"


def test_a_contact_makes_one_stop_notice_per_epoch_and_extra_contacts_only_add_references(
        tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "degraded", observed=_at(10, 0), recovered_notified_epoch=3,
           confirmed=1, resume_requested_at=_at(10, 41).isoformat(), updated=_at(10, 41))
    first = _order(conn)
    second = _order(conn, direction="short", stop_loss=102.0, created=_at(9, 31))
    _bars(conn, _at(10, 1), _at(10, 40), touch=_at(10, 20))
    # 既存の待ち印・要求中でも、初回の接触は停止の通知を抑止しない
    machine.observe(_at(10, 42), _ok())
    assert [n for n in _notices(conn) if n[1] == "stopped"] == [(3, "stopped", "contact")]
    refs = {c[1] for c in _contacts(conn)}
    assert first in refs and second in refs
    # 追加の接触 (次の tick) は参照が増えるだけで、停止の通知は増えない
    _bar(conn, _at(10, 41), low=98.0, high=104.0)
    _bar(conn, _at(10, 42))
    machine.observe(_at(10, 44), _ok())
    assert [n for n in _notices(conn) if n[1] == "stopped"] == [(3, "stopped", "contact")]
    assert len(_contacts(conn)) > len(refs)


# ---- 最新・鮮度内の足と未約定指値 -------------------------------------------------

def _pending_reach_rig(tmp_path, state):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, state, observed=_at(10, 0), since=_at(9, 58),
           updated=_at(10, 4, 30))
    oid = _order(conn, status="pending_fill", stop_loss=None, requested_price=100.0,
                 created=_at(9, 55))
    _bars(conn, _at(10, 1), _at(10, 3), touch=_at(10, 3))      # 最新の足 10:03 が指値に届く
    return conn, machine, oid


def test_the_latest_bar_limit_reach_stops_when_the_epoch_stays_non_ready(tmp_path):
    conn, machine, oid = _pending_reach_rig(tmp_path, "restricted")
    assert machine.observe(_at(10, 5), _failed()) == "degraded"
    assert _contacts(conn) == [(3, oid, _at(10, 3).isoformat(), "limit")]
    assert orders.get(conn, oid)["status"] == "pending_fill"     # 仮約定はしない


def test_the_latest_bar_limit_reach_is_left_to_the_normal_fill_when_the_tick_recovers(
        tmp_path):
    conn, machine, oid = _pending_reach_rig(tmp_path, "restricted")
    assert machine.observe(_at(10, 5), _ok()) == "ready"
    assert _contacts(conn) == []
    assert orders.get(conn, oid)["status"] == "pending_fill"


def test_a_stale_latest_bar_is_checked_like_any_other_bar(tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "restricted", observed=_at(10, 0), since=_at(9, 58),
           updated=_at(10, 9, 30))
    oid = _order(conn, status="pending_fill", stop_loss=None, requested_price=100.0,
                 created=_at(9, 55))
    _bars(conn, _at(10, 1), _at(10, 3), touch=_at(10, 3))
    # 10:03 の足は最新だが 10:10 には 7 分前で鮮度切れ (通常保護は評価しない)
    assert machine.observe(_at(10, 10), _ok()) == "degraded"
    assert [(c[1], c[3]) for c in _contacts(conn)] == [(oid, "limit")]


def test_an_open_orders_latest_bar_is_left_to_the_normal_protection(tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "restricted", observed=_at(10, 0), since=_at(9, 58),
           updated=_at(10, 4, 30))
    _order(conn)
    _bars(conn, _at(10, 1), _at(10, 3), touch=_at(10, 3))
    assert machine.observe(_at(10, 5), _failed()) == "restricted"
    assert _contacts(conn) == []


def test_a_bar_the_normal_protection_already_closed_on_is_not_checked_again(tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "degraded", observed=_at(10, 0), updated=_at(10, 4, 30))
    oid = _order(conn)
    _bars(conn, _at(10, 1), _at(10, 3), touch=_at(10, 3))
    # tick t: 最新の足 X=10:03 は通常保護が損切りで決済する (observe は決済の前)
    assert machine.observe(_at(10, 5), _ok()) == "degraded"
    assert _contacts(conn) == []
    conn.execute("UPDATE orders SET status='closed', closed_at=?, close_reason='sl' "
                 "WHERE id=?", (_at(10, 5, 5).isoformat(), oid))
    conn.commit()
    # tick t+1: X は古い足になるが、observe 済みの水位以下なので検査しない。新しい足だけを検査する
    assert _tick(conn, machine, _at(10, 6)) == "degraded"
    assert _contacts(conn) == []
    restarted = OutageStateMachine(conn, hard_keys=frozenset({KEY}), interval_widths=WIDTHS,
                                   grace=GRACE, storage_source=SRC)
    assert _tick(conn, restarted, _at(10, 7)) == "degraded"
    assert _contacts(conn) == []


# ---- 週末・鮮度切れ・生存窓 -------------------------------------------------------

def test_a_bulk_arrival_across_the_weekend_is_checked_and_the_closed_hours_are_not_counted(
        tmp_path):
    conn, machine = _rig(tmp_path)
    friday = datetime(2026, 11, 20, 21, 50, tzinfo=UTC)          # 冬時間: 22:00Z に閉場
    sunday_open = datetime(2026, 11, 22, 22, 0, tzinfo=UTC)
    _state(conn, machine, "restricted", observed=friday, since=friday + timedelta(minutes=2),
           updated=friday + timedelta(minutes=5), epoch=8)
    oid = _order(conn, created=friday - timedelta(hours=1))
    _bar(conn, friday + timedelta(minutes=5), low=99.0)          # 金曜の末尾の足
    _bar(conn, sunday_open, low=101.0)
    _bar(conn, sunday_open + timedelta(minutes=1))
    # 日曜の開場後の最初の tick。restricted の連続は開場分 (金曜 8 分 + 日曜 2 分) で 30 分未満
    assert machine.observe(sunday_open + timedelta(minutes=3), _ok()) == "degraded"
    assert [(c[1], c[2]) for c in _contacts(conn)] == [
        (oid, (friday + timedelta(minutes=5)).isoformat())]


def test_orders_outside_the_bars_live_window_are_not_flagged(tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "degraded", observed=_at(10, 0), updated=_at(10, 41))
    _order(conn, status="closed", created=_at(9, 0), closed_at=_at(10, 10))   # 10:20 より前に終了
    _order(conn, created=_at(10, 30))                                          # 10:20 より後に作成
    _bars(conn, _at(10, 1), _at(10, 40), touch=_at(10, 20))
    assert machine.observe(_at(10, 42), _ok()) == "degraded"
    assert _contacts(conn) == []


@pytest.mark.parametrize("kwargs,kind", [
    (dict(stop_loss=100.0), "sl"),
    (dict(stop_loss=90.0, take_profit=105.0), "tp"),
    (dict(stop_loss=100.0, take_profit=105.0), "both"),
    (dict(stop_loss=None), "unknown"),
])
def test_the_contact_kind_reaches_the_reference(tmp_path, kwargs, kind):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "degraded", observed=_at(10, 0), updated=_at(10, 41))
    _order(conn, **kwargs)
    _bars(conn, _at(10, 1), _at(10, 40), touch=_at(10, 20))
    machine.observe(_at(10, 42), _ok())
    assert {c[3] for c in _contacts(conn)} == {kind}


# ---- 後着足 ------------------------------------------------------------------

def _late_bar(conn, at, *, low):
    outage_facts.add_late_bars(
        conn, [Bar(PAIR, "1m", at, 101.0, 101.0, low, 101.0, 1)], SRC, detected_at=_at(10, 5))
    conn.commit()


def test_a_late_bar_touch_in_a_ready_epoch_opens_an_epoch_and_stops(tmp_path):
    conn, machine = _rig(tmp_path)
    _bars(conn, _at(10, 0), _at(10, 3))
    assert machine.observe(_at(10, 5), _ok()) == "ready"
    oid = _order(conn)
    _late_bar(conn, _at(10, 2, 0), low=99.0)
    assert _tick(conn, machine, _at(10, 6)) == "degraded"
    row = _row(conn)
    assert row["epoch"] == 1 and row["pending_human_confirmation"] == 1
    assert _contacts(conn) == [(1, oid, _at(10, 2).isoformat(), "sl")]
    assert outage_facts.list_late_bars(conn, SRC) == []           # 検査した行は消費する


def test_a_late_bar_without_a_touch_is_consumed_and_changes_nothing(tmp_path):
    conn, machine = _rig(tmp_path)
    _bars(conn, _at(10, 0), _at(10, 3))
    machine.observe(_at(10, 5), _ok())
    _order(conn)
    _late_bar(conn, _at(10, 2, 0), low=101.0)
    assert _tick(conn, machine, _at(10, 6)) == "ready"
    assert outage_facts.list_late_bars(conn, SRC) == []
    assert _contacts(conn) == [] and _row(conn)["epoch"] == 0


def test_a_failed_save_keeps_the_late_bar_and_leaves_no_contact_or_state(tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path)
    _bars(conn, _at(10, 0), _at(10, 3))
    machine.observe(_at(10, 5), _ok())
    _order(conn)
    _late_bar(conn, _at(10, 2, 0), low=99.0)

    def crash(*args, **kwargs):
        raise sqlite3.OperationalError("injected crash")

    original = machine._save_facts
    monkeypatch.setattr(machine, "_save_facts", crash)
    with pytest.raises(sqlite3.OperationalError):
        _tick(conn, machine, _at(10, 6))
    assert len(outage_facts.list_late_bars(conn, SRC)) == 1
    assert _contacts(conn) == [] and _row(conn)["state"] == "ready"
    monkeypatch.setattr(machine, "_save_facts", original)
    assert _tick(conn, machine, _at(10, 7)) == "degraded"
    assert outage_facts.list_late_bars(conn, SRC) == []
    assert len(_contacts(conn)) == 1
    assert [n for n in _notices(conn) if n[1] == "stopped"] == [(1, "stopped", "contact")]
    machine.observe(_at(10, 8), _ok())
    assert [n for n in _notices(conn) if n[1] == "stopped"] == [(1, "stopped", "contact")]


def test_an_unreadable_late_bar_check_closes_the_gate_and_keeps_the_rows(tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path)
    _bars(conn, _at(10, 0), _at(10, 3))
    machine.observe(_at(10, 5), _ok())
    _late_bar(conn, _at(10, 2, 0), low=99.0)
    from agentic_fx.datafeed import outage_inspect

    def unreadable(*args, **kwargs):
        raise sqlite3.OperationalError("orders unreadable")

    monkeypatch.setattr(outage_inspect, "late_bar_findings", unreadable)
    machine.request_resume(_at(10, 5, 30), acknowledge=True)
    assert _tick(conn, machine, _at(10, 6)) == "degraded"
    assert len(outage_facts.list_late_bars(conn, SRC)) == 1
    row = _row(conn)
    assert row["state"] == "degraded" and row["resume_requested_at"] is not None  # 要求は次の tick へ
    # 観測済みの水位は進めない (次の tick が同じ範囲を検査し直す)
    kf = outage_facts.load_key_facts(conn, SRC)[KEY]
    assert kf.observed_through == _at(10, 3)


def test_an_unreadable_key_facts_read_is_a_gate_closing_failure(tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path)
    _bars(conn, _at(10, 0), _at(10, 3))

    def unreadable(*args, **kwargs):
        raise sqlite3.OperationalError("facts unreadable")

    real = outage_facts.load_key_facts
    monkeypatch.setattr(outage_facts, "load_key_facts", unreadable)
    assert _tick(conn, machine, _at(10, 5)) == "degraded"
    assert _row(conn)["epoch"] == 1
    monkeypatch.setattr(outage_facts, "load_key_facts", real)
    for minute in (6, 7, 8):
        _tick(conn, machine, _at(10, minute))
    assert machine.state == "ready"


# ---- crash: 保存前は差分を再検査、保存後は同じ足を回収しない ---------------------

def test_a_crash_after_the_cache_commit_leaves_the_bars_to_the_next_start(tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "degraded", observed=_at(10, 0), updated=_at(10, 41))
    oid = _order(conn)
    _bars(conn, _at(10, 1), _at(10, 40), touch=_at(10, 20))
    # observe の前に落ちた: observe 済みの水位は 10:00 のまま。新しいプロセスの最初の observe が回収する
    restarted = OutageStateMachine(conn, hard_keys=frozenset({KEY}), interval_widths=WIDTHS,
                                   grace=GRACE, storage_source=SRC, startup_recovery=True)
    assert restarted.state == "degraded"                          # 最初の開場 observe まで待つ
    assert restarted.observe(_at(10, 42), _ok()) == "degraded"
    assert [c[1] for c in _contacts(conn)] == [oid]
    # 保存後は同じ足を回収しない: 次の tick の差分は空で、参照も通知も増えない
    count = len(_contacts(conn))
    restarted.observe(_at(10, 43), _ok())
    assert len(_contacts(conn)) == count
    assert outage_facts.load_key_facts(conn, SRC)[KEY].observed_through == _at(10, 40)


def test_a_failed_save_does_not_advance_the_observed_watermark(tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "degraded", observed=_at(10, 0), updated=_at(10, 41))
    oid = _order(conn)
    _bars(conn, _at(10, 1), _at(10, 40), touch=_at(10, 20))
    original = machine._save_facts

    def save_then_crash(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.OperationalError("injected crash")

    monkeypatch.setattr(machine, "_save_facts", save_then_crash)
    with pytest.raises(sqlite3.OperationalError):
        machine.observe(_at(10, 42), _ok())
    assert outage_facts.load_key_facts(conn, SRC)[KEY].observed_through == _at(10, 0)
    assert _contacts(conn) == []
    monkeypatch.setattr(machine, "_save_facts", original)
    assert machine.observe(_at(10, 43), _ok()) == "degraded"
    assert [c[1] for c in _contacts(conn)] == [oid]


def test_an_explicit_null_watermark_is_checked_from_the_first_bar(tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "degraded", updated=_at(10, 41))
    outage_facts.save_key_facts(conn, PAIR, "1m", SRC, observed_through=None, stall_start=None,
                                last_attempt_ok=None, now=_at(10, 41))
    conn.commit()
    oid = _order(conn)
    _bars(conn, _at(10, 1), _at(10, 40), touch=_at(10, 1))
    machine.observe(_at(10, 42), _ok())
    assert [(c[1], c[2]) for c in _contacts(conn)] == [(oid, _at(10, 1).isoformat())]


# ---- 起動待ち・移行確認 -------------------------------------------------------

def test_the_first_open_observe_releases_the_startup_wait_and_a_closed_one_does_not(tmp_path):
    conn, machine = _rig(tmp_path, startup_recovery=True)
    assert machine.state == "degraded"                    # 起動直後 (行は無い) は待つ
    saturday = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    machine.observe(saturday, _ok())
    assert machine.state == "degraded" and machine.state_from(conn) == "degraded"
    _bars(conn, _at(10, 0), _at(10, 3))
    assert machine.observe(_at(10, 5), _ok()) == "ready"
    assert machine.state == "ready"


def test_a_manual_request_is_not_consumed_before_the_first_open_observe(tmp_path):
    conn, machine = _rig(tmp_path, startup_recovery=True)
    machine.request_resume(datetime(2026, 9, 26, 11, 0, tzinfo=UTC), acknowledge=True)
    machine.observe(datetime(2026, 9, 26, 12, 0, tzinfo=UTC), _ok())
    assert _row(conn)["resume_requested_at"] is not None


def test_a_machine_without_the_startup_recovery_has_no_startup_wait(tmp_path):
    conn, machine = _rig(tmp_path)
    assert machine.state == "ready"


def test_an_existing_stop_without_saved_watermarks_waits_for_a_human_after_the_deploy(
        tmp_path):
    conn, machine = _rig(tmp_path, startup_recovery=True)
    _state(conn, machine, "degraded", epoch=66, updated=_at(10, 0))
    _bars(conn, _at(9, 50), _at(10, 3))
    states = [_tick(conn, machine, _at(10, minute)) for minute in (5, 6, 7, 8, 9)]
    assert states == ["degraded"] * 5
    assert _row(conn)["migration_confirmation_pending"] == 1
    # 再起動でも ack 前の待ちを保持する
    restarted = OutageStateMachine(conn, hard_keys=frozenset({KEY}), interval_widths=WIDTHS,
                                   grace=GRACE, storage_source=SRC, startup_recovery=True)
    assert _tick(conn, restarted, _at(10, 10)) == "degraded"
    assert _row(conn)["migration_confirmation_pending"] == 1
    restarted.request_resume(_at(10, 10, 20), acknowledge=False)
    assert _tick(conn, restarted, _at(10, 11)) == "degraded"
    assert _row(conn)["resume_last_result"] == "rejected_unprocessed"
    restarted.request_resume(_at(10, 11, 20), acknowledge=True)
    assert _tick(conn, restarted, _at(10, 12)) == "ready"
    assert _row(conn)["migration_confirmation_pending"] == 0


def test_a_healthy_ready_start_has_no_bar_check_and_saves_the_watermark(tmp_path):
    conn, machine = _rig(tmp_path, startup_recovery=True)
    _order(conn)
    _bars(conn, _at(9, 40), _at(10, 3), touch=_at(9, 50))    # 古い足の接触は検査しない
    assert machine.observe(_at(10, 5), _ok()) == "ready"
    assert _contacts(conn) == []
    assert outage_facts.load_key_facts(conn, SRC)[KEY].observed_through == _at(10, 3)
    assert _row(conn)["migration_confirmation_pending"] == 0


# ---- 観測の途切れ (旧停滞の期限超過) -----------------------------------------------

def test_an_old_stall_past_the_exposure_limit_stops_even_when_the_bulk_arrival_is_healthy(
        tmp_path):
    conn, machine = _rig(tmp_path, startup_recovery=True)
    _state(conn, machine, "restricted", observed=_at(10, 0), stall=_at(10, 0),
           since=_at(10, 0), updated=_at(10, 8))
    oid = _order(conn)
    _bars(conn, _at(10, 1), _at(10, 10), touch=_at(10, 5))
    # 再起動後の最初の tick は 10:11:01 (旧 S から 661 秒)。一括到着の足は健全だが F
    assert machine.observe(_at(10, 11, 1), _ok()) == "degraded"
    assert [c[1] for c in _contacts(conn)] == [oid]               # 接触の検査も先に済ませる
    assert _row(conn)["epoch"] == 3                                 # 同じ epoch


def test_an_old_stall_within_the_exposure_limit_recovers_on_the_first_healthy_tick(tmp_path):
    conn, machine = _rig(tmp_path, startup_recovery=True)
    _state(conn, machine, "restricted", observed=_at(10, 0), stall=_at(10, 0),
           since=_at(10, 0), updated=_at(10, 8))
    _order(conn)
    _bars(conn, _at(10, 1), _at(10, 8))
    assert machine.observe(_at(10, 10), _ok()) == "ready"


def test_a_flat_old_stall_uses_the_configured_limit(tmp_path):
    conn, machine = _rig(tmp_path, startup_recovery=True)
    _state(conn, machine, "restricted", observed=_at(9, 0), stall=_at(9, 0),
           since=_at(9, 0), updated=_at(9, 20))
    _bars(conn, _at(9, 1), _at(9, 31))
    assert machine.observe(_at(9, 31, 5), _ok()) == "degraded"      # 1865 秒 > 1800
    row = _row(conn)
    assert row["pending_human_confirmation"] == 0                    # flat は人の確認を立てない


def test_a_ready_first_observe_with_an_old_saved_stall_stops(tmp_path):
    conn, machine = _rig(tmp_path, startup_recovery=True)
    _state(conn, machine, "ready", observed=_at(10, 0), stall=_at(10, 0), updated=_at(10, 8))
    _order(conn)
    _bars(conn, _at(10, 1), _at(10, 10))
    assert machine.observe(_at(10, 12), _ok()) == "degraded"
    assert _row(conn)["epoch"] == 4


# ---- flat の判定 --------------------------------------------------------------

@pytest.mark.parametrize("make", [
    lambda conn: _order(conn, status="open", quantity=0, stop_loss=100.0),    # 数量 0 でも非終端
    lambda conn: _order(conn, status="open", pair="EURUSD", stop_loss=1.0),   # 別 pair も数える
    lambda conn: _order(conn, status="some_new_status"),                      # 未知の status
])
def test_the_flat_release_does_not_start_while_any_order_is_not_provably_terminal(
        tmp_path, make):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "degraded", observed=_at(10, 0), updated=_at(10, 4))
    make(conn)
    _bars(conn, _at(10, 1), _at(10, 4))
    for minute in range(5, 10):
        assert _tick(conn, machine, _at(10, minute)) == "degraded"
    assert _row(conn)["ready_streak"] == 0


def test_a_pending_flag_does_not_stop_the_flat_release_of_an_ordinary_stop(tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "degraded", observed=_at(10, 0), updated=_at(10, 4),
           pending_human_confirmation=1, confirmed=1)
    _bars(conn, _at(10, 1), _at(10, 4))
    states = [_tick(conn, machine, _at(10, minute)) for minute in (5, 6, 7)]
    assert states == ["degraded", "degraded", "ready"]
    assert _row(conn)["pending_human_confirmation"] == 0


def test_an_exposure_recurring_after_two_ticks_resets_the_count(tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "degraded", observed=_at(10, 0), updated=_at(10, 4))
    _bars(conn, _at(10, 1), _at(10, 4))
    assert [_tick(conn, machine, _at(10, minute)) for minute in (5, 6)] == ["degraded"] * 2
    _order(conn, created=_at(10, 6, 30))
    for minute in (7, 8, 9):
        assert _tick(conn, machine, _at(10, minute)) == "degraded"
    assert _row(conn)["ready_streak"] == 0


def test_the_same_bar_and_the_same_now_do_not_add_a_healthy_tick(tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "degraded", observed=_at(10, 0), updated=_at(10, 4))
    _bars(conn, _at(10, 1), _at(10, 5))
    machine.observe(_at(10, 7), _ok())
    streak = _row(conn)["ready_streak"]
    assert streak == 1
    machine.observe(_at(10, 7), _ok())                    # 同じ now・同じ足
    assert _row(conn)["ready_streak"] == streak


# ---- 保存した失敗の記憶・停滞の起点 -------------------------------------------------

def test_the_stall_start_is_fixed_across_restarts_and_exposure_changes(tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "ready", observed=_at(10, 0), updated=_at(10, 3))
    _bar(conn, _at(10, 0))
    assert machine.observe(_at(10, 4), _failed()) == "restricted"     # S=10:02:30 (足 10:00 の次)
    first = outage_facts.load_key_facts(conn, SRC)[KEY].stall_start
    assert first is not None
    _order(conn, created=_at(10, 4))                                   # flat -> 建玉あり
    restarted = OutageStateMachine(conn, hard_keys=frozenset({KEY}), interval_widths=WIDTHS,
                                   grace=GRACE, storage_source=SRC)
    assert restarted.observe(_at(10, 5), _failed()) == "restricted"
    assert outage_facts.load_key_facts(conn, SRC)[KEY].stall_start == first
    # 建玉ありの猶予 600 秒は同じ S から数える: 10:12:31 で超過
    assert restarted.observe(_at(10, 12), _failed()) == "restricted"
    assert restarted.observe(_at(10, 13), _failed()) == "degraded"


def test_a_new_confirmed_bar_ends_the_old_timer_and_a_new_stall_starts_a_new_one(tmp_path):
    conn, machine = _rig(tmp_path)
    _bar(conn, _at(10, 0))
    machine.observe(_at(10, 1), _ok())
    assert machine.observe(_at(10, 4), _failed()) == "restricted"
    first = outage_facts.load_key_facts(conn, SRC)[KEY].stall_start
    _bar(conn, _at(10, 10))
    assert machine.observe(_at(10, 12), _ok()) == "ready"
    assert outage_facts.load_key_facts(conn, SRC)[KEY].stall_start is None
    assert machine.observe(_at(10, 15), _failed()) == "restricted"
    second = outage_facts.load_key_facts(conn, SRC)[KEY].stall_start
    assert second is not None and second > first


def test_an_unattempted_non_stalled_tick_keeps_the_state_the_gaps_and_the_memo(tmp_path):
    conn, machine = _rig(tmp_path)
    _bar(conn, _at(10, 0))
    machine.observe(_at(10, 1), _ok())
    machine.observe(_at(10, 4), _failed())
    memo = dict(machine._last_attempt_ok)
    gaps = conn.execute("SELECT count(*) FROM datafeed_outage_gap").fetchone()[0]
    _bar(conn, _at(10, 2))
    idle = IngestTickReport(attempted=frozenset(), succeeded=frozenset(), failed=frozenset(),
                            deferred=frozenset(), empty=frozenset())
    assert machine.observe(_at(10, 4, 30), idle) == "restricted"
    assert _row(conn)["epoch"] == 1
    assert conn.execute("SELECT count(*) FROM datafeed_outage_gap").fetchone()[0] == gaps
    assert machine._last_attempt_ok == memo
    assert _row(conn)["ready_streak"] == 0


def test_an_upper_failure_is_remembered_across_a_restart_and_blocks_the_automatic_recovery(
        tmp_path):
    conn = connect(tmp_path / "upper.db")
    init_db(conn)
    keys = frozenset({KEY, (PAIR, "1h")})
    widths = {"1m": timedelta(minutes=1), "1h": timedelta(minutes=60)}

    def make():
        return OutageStateMachine(conn, hard_keys=keys, interval_widths=widths, grace=GRACE,
                                  storage_source=SRC)

    upper_failed = IngestTickReport(
        attempted=frozenset(keys), succeeded=frozenset({KEY}),
        failed=frozenset({((PAIR, "1h"), "x")}), deferred=frozenset(), empty=frozenset())
    _bars(conn, _at(9, 50), _at(10, 3))
    first = make()
    assert first.observe(_at(10, 5), upper_failed) == "degraded"
    second = make()
    one_minute_only = IngestTickReport(
        attempted=frozenset({KEY}), succeeded=frozenset({KEY}), failed=frozenset(),
        deferred=frozenset(), empty=frozenset())
    for minute in (6, 7, 8, 9):
        _bar(conn, _at(10, minute) - timedelta(minutes=2))
        assert second.observe(_at(10, minute), one_minute_only) == "degraded"
    # 上位の未取得を True にしない: 手動再開も拒否する
    second.request_resume(_at(10, 9, 20), acknowledge=True)
    _bar(conn, _at(10, 8))
    assert second.observe(_at(10, 10), one_minute_only) == "degraded"
    assert _row(conn)["resume_last_result"] == "rejected_watermark"


def test_a_soft_failure_never_escalates(tmp_path):
    conn, machine = _rig(tmp_path)
    _bars(conn, _at(10, 0), _at(10, 3))
    report = IngestTickReport(
        attempted=frozenset({KEY}), succeeded=frozenset({KEY}), failed=frozenset(),
        deferred=frozenset(), empty=frozenset(),
        soft_attempted=frozenset({("EURUSD", "1m")}),
        soft_failed=frozenset({(("EURUSD", "1m"), "x")}))
    assert machine.observe(_at(10, 5), report) == "ready"
    assert _row(conn)["epoch"] == 0


# ---- gap の起点 ----------------------------------------------------------------

def test_an_epoch_opened_with_a_bulk_arrival_starts_at_the_watermark_before_the_arrival(
        tmp_path):
    conn, machine = _rig(tmp_path, startup_recovery=True)
    _state(conn, machine, "ready", observed=_at(10, 0), stall=_at(10, 0), updated=_at(10, 8))
    _order(conn)                                  # 建玉ありの猶予 (600 秒) を旧 S から超える
    _bars(conn, _at(10, 1), _at(10, 10))
    machine.observe(_at(10, 12), _ok())
    gap = conn.execute("SELECT gap_start, source FROM datafeed_outage_gap WHERE epoch=4"
                       ).fetchone()
    assert gap["gap_start"] == _at(10, 0).isoformat() and gap["source"] == SRC


def test_a_key_without_any_bar_opens_the_gap_with_a_null_start(tmp_path):
    conn, machine = _rig(tmp_path, flat_stall_max_sec=0)
    assert machine.observe(_at(10, 5), _failed()) == "degraded"
    gap = conn.execute("SELECT gap_start FROM datafeed_outage_gap").fetchone()
    assert gap["gap_start"] is None
    assert machine.gap_summary(_at(10, 6))["gap_starts"] == {}


# ---- 閉場 -----------------------------------------------------------------------

def test_a_closed_market_observation_resets_the_healthy_count_and_keeps_the_starts(tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "degraded", observed=_at(10, 0), updated=_at(10, 4))
    _bars(conn, _at(10, 1), _at(10, 4))
    for minute in (5, 6):
        _tick(conn, machine, _at(10, minute))
    before = _row(conn)
    assert before["ready_streak"] == 2
    facts_before = outage_facts.load_key_facts(conn, SRC)[KEY]
    saturday = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    assert machine.observe(saturday, _failed()) == "degraded"
    after = _row(conn)
    assert after["ready_streak"] == 0
    assert (after["epoch"], after["updated_at"], after["entered_degraded_at"]) == (
        before["epoch"], before["updated_at"], before["entered_degraded_at"])
    assert outage_facts.load_key_facts(conn, SRC)[KEY] == facts_before


# ---- 通知の再照合 ----------------------------------------------------------------

def test_a_contact_without_its_notice_record_is_recorded_at_the_first_observe_after_a_restart(
        tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "degraded", observed=_at(10, 0), confirmed=1, updated=_at(10, 4))
    outage_facts.add_contact(conn, epoch=3, order_id=5, pair=PAIR, bar_time=_at(10, 2),
                             kind="sl", detected_at=_at(10, 3))
    conn.commit()
    assert _notices(conn) == []                      # 状態を保存した後、通知の保存の前に落ちた
    restarted = OutageStateMachine(conn, hard_keys=frozenset({KEY}), interval_widths=WIDTHS,
                                   grace=GRACE, storage_source=SRC)
    _bars(conn, _at(10, 1), _at(10, 4))
    restarted.observe(_at(10, 6), _ok())
    assert [n for n in _notices(conn) if n[1] == "stopped"] == [(3, "stopped", "contact")]
    restarted.observe(_at(10, 7), _ok())
    assert [n for n in _notices(conn) if n[1] == "stopped"] == [(3, "stopped", "contact")]
