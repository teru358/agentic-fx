"""通知の記録 (datafeed_outage_notices)・要求の契約・終端結果の永続化。

時刻はすべて実時刻 (UTC) の例で固定する。期待値は設計の表から手で書いた値。
"""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.outage import IngestTickReport, OutageStateMachine
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.ohlcv import upsert_cache_bars

PAIR = "USDJPY"
KEY = (PAIR, "1m")
UTC = timezone.utc
WED = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
WIDTHS = {"1m": timedelta(minutes=1)}


def _rig(tmp_path, **kw):
    conn = connect(tmp_path / "notices.db")
    init_db(conn)
    machine = OutageStateMachine(conn, hard_keys=frozenset({KEY}), interval_widths=WIDTHS,
                                 grace=timedelta(seconds=30), storage_source="mt5-live", **kw)
    return conn, machine


def _bar(conn, at):
    upsert_cache_bars(conn, [Bar(PAIR, "1m", at, 1, 1, 1, 1, 1)], source="mt5-live")
    conn.commit()


def _report(*, failed=False, empty=False):
    if failed:
        return IngestTickReport(attempted=frozenset({KEY}), succeeded=frozenset(),
                                failed=frozenset({(KEY, "ConnectError")}),
                                deferred=frozenset(), empty=frozenset())
    return IngestTickReport(attempted=frozenset({KEY}), succeeded=frozenset({KEY}),
                            failed=frozenset(), deferred=frozenset(),
                            empty=frozenset({KEY}) if empty else frozenset())


def _tick(conn, machine, now, *, failed=False, empty=False, fresh=True):
    if fresh:
        _bar(conn, now - timedelta(minutes=2))
    return machine.observe(now, _report(failed=failed, empty=empty))


def _open_position(conn, at):
    orders.insert(conn, pair=PAIR, direction="buy", entry_type="market", horizon="swing",
                  status="open", now=at, filled_at=at.isoformat())
    conn.commit()


def _notices(conn):
    return [(r["epoch"], r["notice_state"], r["request_key"]) for r in conn.execute(
        "SELECT epoch, notice_state, request_key FROM datafeed_outage_notices "
        "ORDER BY occurred_at, rowid")]


def _states(conn):
    return [n[1] for n in _notices(conn)]


def _set(conn, machine, **cols):
    machine._ensure_row(WED)
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE datafeed_outage_state SET {sets} WHERE id=1", tuple(cols.values()))
    conn.commit()
    machine._reset_ready_streak = False


# ---- 要求の契約 --------------------------------------------------------

def test_a_request_without_an_unconsumed_one_is_recorded_as_new_with_its_epoch(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=7)
    reply = machine.request_resume(WED, acknowledge=False)
    assert reply == {"outcome": "new", "epoch": 7, "acknowledge": False}
    row = machine.status()
    assert row["resume_requested_at"] == WED.isoformat()
    assert row["resume_acknowledge"] == 0


@pytest.mark.parametrize(("first", "second", "outcome", "ack"), [
    (False, False, "existing", False),
    (True, True, "existing", True),
    (True, False, "existing", True),
])
def test_a_repeated_request_inquires_and_keeps_the_time_and_the_acknowledge(
        tmp_path, first, second, outcome, ack):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=7)
    machine.request_resume(WED, acknowledge=first)
    reply = machine.request_resume(WED + timedelta(minutes=5), acknowledge=second)
    assert reply == {"outcome": outcome, "epoch": 7, "acknowledge": ack}
    row = machine.status()
    assert row["resume_requested_at"] == WED.isoformat()
    assert bool(row["resume_acknowledge"]) is ack


def test_an_acknowledge_request_replaces_one_without_it_once(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=7)
    machine.request_resume(WED, acknowledge=False)
    later = WED + timedelta(minutes=5)
    reply = machine.request_resume(later, acknowledge=True)
    assert reply == {"outcome": "replaced", "epoch": 7, "acknowledge": True}
    row = machine.status()
    assert row["resume_requested_at"] == later.isoformat() and row["resume_acknowledge"] == 1
    assert machine.request_resume(later + timedelta(minutes=1), acknowledge=True)["outcome"] == "existing"


def test_a_request_key_never_repeats_a_consumed_request_time(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=7)
    machine.request_resume(WED, acknowledge=False)
    _tick(conn, machine, WED)  # ready なので already_ready として消費される
    reply = machine.request_resume(WED, acknowledge=False)
    assert reply["outcome"] == "new"
    assert machine.status()["resume_requested_at"] == (WED + timedelta(microseconds=1)).isoformat()


def test_a_request_works_without_the_notice_table(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=7)
    conn.execute("DROP TABLE datafeed_outage_notices")
    conn.commit()
    assert machine.request_resume(WED, acknowledge=False)["outcome"] == "new"


# ---- 終端結果 ----------------------------------------------------------

def _degrade(conn, machine, now, *, with_position=False):
    if with_position:
        _open_position(conn, now - timedelta(minutes=30))
    assert _tick(conn, machine, now, failed=True) == "degraded"


def test_an_accepted_request_persists_its_result_and_notifies_acceptance_then_resumption(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    _degrade(conn, machine, WED)
    machine.request_resume(WED + timedelta(minutes=1), acknowledge=False)
    key = machine.status()["resume_requested_at"]
    assert _tick(conn, machine, WED + timedelta(minutes=2)) == "ready"
    row = machine.status()
    assert (row["resume_last_result"], row["resume_last_request_at"]) == ("accepted", key)
    assert row["resume_last_result_at"] == (WED + timedelta(minutes=2)).isoformat()
    assert row["resume_requested_at"] is None
    assert _notices(conn) == [(5, "resume_accepted", key), (5, "resumed", "")]


def test_unprocessed_positions_without_acknowledge_are_rejected_and_can_be_re_requested(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    _degrade(conn, machine, WED, with_position=True)
    _bar(conn, WED + timedelta(minutes=4))
    machine.request_resume(WED + timedelta(minutes=5), acknowledge=False)
    first = machine.status()["resume_requested_at"]
    assert _tick(conn, machine, WED + timedelta(minutes=6)) == "degraded"
    assert machine.status()["resume_last_result"] == "rejected_unprocessed"
    machine.request_resume(WED + timedelta(minutes=7), acknowledge=False)
    second = machine.status()["resume_requested_at"]
    assert _tick(conn, machine, WED + timedelta(minutes=8)) == "degraded"
    assert second != first
    assert _notices(conn) == [(5, "resume_rejected", first), (5, "resume_rejected", second)]
    machine.request_resume(WED + timedelta(minutes=9), acknowledge=True)
    third = machine.status()["resume_requested_at"]
    assert _tick(conn, machine, WED + timedelta(minutes=10)) == "ready"
    assert _states(conn)[-2:] == ["resume_accepted", "resumed"]
    assert _notices(conn)[2] == (5, "resume_accepted", third)


def test_a_watermark_rejection_wins_over_unprocessed_positions_even_with_acknowledge(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    _degrade(conn, machine, WED, with_position=True)
    machine.request_resume(WED + timedelta(minutes=1), acknowledge=False)
    assert _tick(conn, machine, WED + timedelta(minutes=2), failed=True) == "degraded"
    assert machine.status()["resume_last_result"] == "rejected_watermark"
    machine.request_resume(WED + timedelta(minutes=3), acknowledge=True)
    assert _tick(conn, machine, WED + timedelta(minutes=4), failed=True) == "degraded"
    assert machine.status()["resume_last_result"] == "rejected_watermark"
    assert _states(conn) == ["stopped", "resume_rejected", "resume_rejected"]


def test_a_request_on_a_ready_state_is_consumed_as_already_ready(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    machine.request_resume(WED, acknowledge=True)
    key = machine.status()["resume_requested_at"]
    assert _tick(conn, machine, WED + timedelta(minutes=1)) == "ready"
    row = machine.status()
    assert (row["resume_last_result"], row["resume_last_request_at"]) == ("already_ready", key)
    assert row["resume_requested_at"] is None and row["resume_acknowledge"] == 0
    assert _notices(conn) == [(4, "resume_already_ready", key)]


def test_a_restricted_state_that_recovers_by_itself_answers_the_request_as_already_ready(tmp_path):
    conn, machine = _rig(tmp_path)
    deadline = WED + timedelta(minutes=30)
    _set(conn, machine, state="restricted", epoch=4, ready_streak=2,
         restricted_since=WED.isoformat(), restricted_deadline_at=deadline.isoformat())
    machine.request_resume(WED, acknowledge=False)
    key = machine.status()["resume_requested_at"]
    assert _tick(conn, machine, WED + timedelta(minutes=1)) == "ready"
    assert machine.status()["resume_last_result"] == "already_ready"
    assert _notices(conn) == [(4, "resume_already_ready", key)]


def test_a_new_epoch_clears_the_previous_result(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    machine.request_resume(WED, acknowledge=False)
    _tick(conn, machine, WED + timedelta(minutes=1))
    assert machine.status()["resume_last_result"] == "already_ready"
    assert _tick(conn, machine, WED + timedelta(minutes=3), failed=True) == "degraded"
    row = machine.status()
    assert row["epoch"] == 5
    assert (row["resume_last_result"], row["resume_last_result_at"],
            row["resume_last_request_at"]) == (None, None, None)


def test_a_request_recorded_before_a_new_stop_is_judged_against_the_new_epoch(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    machine.request_resume(WED, acknowledge=True)
    key = machine.status()["resume_requested_at"]
    assert _tick(conn, machine, WED + timedelta(minutes=1), failed=True) == "degraded"
    assert machine.status()["resume_last_result"] == "rejected_watermark"
    assert _notices(conn)[-1] == (5, "resume_rejected", key)


# ---- 停止・待ち・復帰の記録 --------------------------------------------

def test_two_consecutive_problem_ticks_record_one_stop_per_episode(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    for minute in range(4):
        _tick(conn, machine, WED + timedelta(minutes=minute), failed=True)
    assert _notices(conn) == [(5, "stopped", "")]


def test_a_healthy_recovery_with_a_retained_pending_flag_records_the_wait_once(tmp_path):
    conn, machine = _rig(tmp_path, activity=ActivityLog(tmp_path / "activity.log"))
    _set(conn, machine, state="degraded", epoch=9, pending_human_confirmation=1)
    for minute in range(4):
        assert _tick(conn, machine, WED + timedelta(minutes=minute)) == "degraded"
    assert _notices(conn) == [(9, "awaiting_resume", "")]
    assert machine.status()["recovered_notified_epoch"] == 9
    awaiting = [line for line in ActivityLog(tmp_path / "activity.log").tail(50)
                if "datafeed_recovered_awaiting_resume" in line]
    assert len(awaiting) == 1
    assert machine.status()["pending_human_confirmation"] == 1


def test_no_wait_is_recorded_while_a_request_is_pending(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=9, pending_human_confirmation=1,
         resume_requested_at=(WED - timedelta(minutes=1)).isoformat(), resume_acknowledge=0)
    _bar(conn, WED - timedelta(minutes=2))
    # 要求は消費されるが、その tick の待ちの判定には要求が見えている
    _tick(conn, machine, WED)
    assert "awaiting_resume" not in _states(conn)
    assert machine.status()["recovered_notified_epoch"] is None


def test_a_flat_healthy_recovery_without_pending_is_not_a_wait(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=9)
    for minute in range(3):
        _tick(conn, machine, WED + timedelta(minutes=minute))
    assert machine.state == "ready"
    assert _notices(conn) == []


def test_no_stop_is_recorded_after_the_wait_of_the_same_epoch(tmp_path):
    conn, machine = _rig(tmp_path)
    _open_position(conn, WED - timedelta(minutes=30))
    _set(conn, machine, state="ready", epoch=4)
    _bar(conn, WED - timedelta(minutes=10))
    assert machine.observe(WED, _report()) == "degraded"       # 建玉あり + 停滞 -> 即 degraded
    _tick(conn, machine, WED + timedelta(minutes=1))              # 健全 -> 待ち
    _tick(conn, machine, WED + timedelta(minutes=2), failed=True)
    _tick(conn, machine, WED + timedelta(minutes=3), failed=True)  # confirmed
    assert machine.status()["confirmed"] == 1
    assert _states(conn) == ["awaiting_resume"]


def test_an_automatic_recovery_after_a_stop_record_adds_the_resumption(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    _tick(conn, machine, WED, failed=True)
    _tick(conn, machine, WED + timedelta(minutes=1), failed=True)
    for minute in range(2, 5):
        _tick(conn, machine, WED + timedelta(minutes=minute))
    assert machine.state == "ready"
    assert _notices(conn) == [(5, "stopped", ""), (5, "resumed", "")]


def test_a_restricted_only_recovery_records_nothing(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    _bar(conn, WED - timedelta(minutes=10))
    assert machine.observe(WED, _report()) == "restricted"
    for minute in range(1, 4):
        _tick(conn, machine, WED + timedelta(minutes=minute))
    assert machine.state == "ready"
    assert _notices(conn) == []


# ---- 記録の失敗の隔離 --------------------------------------------------

def test_a_notice_write_failure_neither_fails_the_observation_nor_stops_the_state(tmp_path, caplog):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    conn.execute("DROP TABLE datafeed_outage_notices")
    conn.commit()
    with caplog.at_level(logging.WARNING):
        for minute in range(3):
            _tick(conn, machine, WED + timedelta(minutes=minute), failed=True)
    assert machine.state == "degraded"
    assert machine.status()["confirmed"] == 1
    assert machine._observe_failed is False
    assert "datafeed_notice_deferred" in caplog.text
    assert not conn.in_transaction


def test_a_failed_notice_write_for_a_request_still_consumes_it(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    _degrade(conn, machine, WED)
    machine.request_resume(WED + timedelta(minutes=1), acknowledge=False)
    conn.execute("DROP TABLE datafeed_outage_notices")
    conn.commit()
    assert _tick(conn, machine, WED + timedelta(minutes=2)) == "ready"
    assert machine.status()["resume_last_result"] == "accepted"


def test_a_duplicate_notice_key_is_a_no_op(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    machine._record_notices(4, WED, [("stopped", ""), ("stopped", "")])
    machine._record_notices(4, WED + timedelta(minutes=1), [("stopped", "")])
    rows = conn.execute("SELECT occurred_at FROM datafeed_outage_notices").fetchall()
    assert [r["occurred_at"] for r in rows] == [WED.isoformat()]


# ---- epoch 66 の実際の時系列 -------------------------------------------

THU = datetime(2026, 11, 19, 22, 40, tzinfo=UTC)


def _epoch66(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=65)
    _open_position(conn, THU - timedelta(hours=3))
    _bar(conn, THU - timedelta(minutes=10))
    assert machine.observe(THU, _report()) == "degraded"                     # 11-19 22:40 建玉あり 1m 停滞
    row = machine.status()
    assert (row["epoch"], row["pending_human_confirmation"]) == (66, 1)
    assert _tick(conn, machine, THU + timedelta(minutes=1)) == "degraded"    # 22:41 健全 -> 待ち
    assert _states(conn) == ["awaiting_resume"]
    conn.execute("UPDATE orders SET status='closed' WHERE status='open'")    # 00:56 建玉を決済
    conn.commit()
    for at in (datetime(2026, 11, 20, 0, 56, tzinfo=UTC), datetime(2026, 11, 20, 0, 57, tzinfo=UTC)):
        assert _tick(conn, machine, at) == "degraded"                        # pending が自動復帰を妨げる
    # 04:19 再起動 (プロセス内の連続カウンタが消える) の後、二連続の問題観測
    machine = OutageStateMachine(conn, hard_keys=frozenset({KEY}), interval_widths=WIDTHS,
                                 grace=timedelta(seconds=30), storage_source="mt5-live")
    for at in (datetime(2026, 11, 20, 4, 19, tzinfo=UTC), datetime(2026, 11, 20, 4, 20, tzinfo=UTC)):
        assert _tick(conn, machine, at, failed=True) == "degraded"
    assert machine.status()["confirmed"] == 1
    assert _states(conn) == ["awaiting_resume"]                              # 待ちの後の停止は出さない
    return conn, machine


def test_epoch_66_request_in_the_closed_market_is_answered_on_the_first_open_tick(tmp_path):
    conn, machine = _epoch66(tmp_path)
    friday_night = datetime(2026, 11, 20, 23, 24, tzinfo=UTC)               # 休場中
    reply = machine.request_resume(friday_night, acknowledge=True)
    assert reply == {"outcome": "new", "epoch": 66, "acknowledge": True}
    key = machine.status()["resume_requested_at"]
    assert _tick(conn, machine, friday_night + timedelta(minutes=1)) == "degraded"
    assert machine.status()["resume_requested_at"] == key                    # 休場中は消費しない
    sunday = datetime(2026, 11, 22, 23, 0, tzinfo=UTC)
    assert _tick(conn, machine, sunday) == "ready"
    assert _notices(conn) == [(66, "awaiting_resume", ""), (66, "resume_accepted", key),
                              (66, "resumed", "")]
    row = machine.status()
    assert (row["resume_last_result"], row["resume_last_request_at"]) == ("accepted", key)


def test_epoch_66_rejection_on_reopen_is_notified_and_a_re_request_is_accepted(tmp_path):
    conn, machine = _epoch66(tmp_path)
    machine.request_resume(datetime(2026, 11, 20, 23, 24, tzinfo=UTC), acknowledge=True)
    first = machine.status()["resume_requested_at"]
    sunday = datetime(2026, 11, 22, 23, 0, tzinfo=UTC)
    assert _tick(conn, machine, sunday, failed=True) == "degraded"
    machine.request_resume(sunday + timedelta(minutes=1), acknowledge=True)
    second = machine.status()["resume_requested_at"]
    assert _tick(conn, machine, sunday + timedelta(minutes=2)) == "ready"
    assert _notices(conn) == [(66, "awaiting_resume", ""), (66, "resume_rejected", first),
                              (66, "resume_accepted", second), (66, "resumed", "")]


# ---- 置換と消費の競合 --------------------------------------------------

def _replace_during_consumption(machine, tmp_path):
    """消費側が要求を読んだ後に、別接続が ack 付きの要求へ置換する。"""
    other = connect(tmp_path / "notices.db")
    original = machine._unprocessed_positions_by_pair
    fired = []
    calls = []

    def hook(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(1)
        # 1 回目は観測の未処理内訳の記録、2 回目が要求の消費での読取り
        if len(calls) == 2 and not fired:
            fired.append(machine.request_resume(WED + timedelta(minutes=30), acknowledge=True,
                                                conn=other))
        return result

    machine._unprocessed_positions_by_pair = hook
    return fired


def test_a_replacement_during_a_rejection_leaves_no_result_for_the_old_request(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    _degrade(conn, machine, WED, with_position=True)
    _bar(conn, WED + timedelta(minutes=4))
    machine.request_resume(WED + timedelta(minutes=5), acknowledge=False)
    fired = _replace_during_consumption(machine, tmp_path)
    notices_before = _states(conn)
    assert _tick(conn, machine, WED + timedelta(minutes=6)) == "degraded"
    assert fired[0]["outcome"] == "replaced"
    row = machine.status()
    assert row["resume_requested_at"] == (WED + timedelta(minutes=30)).isoformat()
    assert row["resume_acknowledge"] == 1
    assert row["resume_last_result"] is None
    assert _states(conn) == notices_before
    new_key = row["resume_requested_at"]
    assert _tick(conn, machine, WED + timedelta(minutes=7)) == "ready"
    assert machine.status()["resume_last_request_at"] == new_key
    assert _notices(conn)[-2:] == [(5, "resume_accepted", new_key), (5, "resumed", "")]


def test_a_replacement_during_an_acceptance_rolls_the_acceptance_back(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    _degrade(conn, machine, WED)
    machine.request_resume(WED + timedelta(minutes=1), acknowledge=False)
    fired = _replace_during_consumption(machine, tmp_path)
    assert _tick(conn, machine, WED + timedelta(minutes=2)) == "degraded"
    assert fired[0]["outcome"] == "replaced"
    row = machine.status()
    assert row["state"] == "degraded"
    assert row["resume_requested_at"] == (WED + timedelta(minutes=30)).isoformat()
    assert row["resume_acknowledge"] == 1 and row["resume_last_result"] is None
    assert "resume_accepted" not in _states(conn)
    assert _tick(conn, machine, WED + timedelta(minutes=3)) == "ready"
    assert machine.status()["resume_last_result"] == "accepted"
    assert not conn.in_transaction


# ---- 記録の取りこぼし ----------------------------------------------------

class _BrokenActivity:
    def write(self, *args, **kwargs):
        raise OSError("activity unavailable")


def test_a_failing_activity_write_does_not_skip_the_notice_record(tmp_path):
    conn, machine = _rig(tmp_path, activity=_BrokenActivity())
    _set(conn, machine, state="ready", epoch=4)
    for minute in range(2):
        with pytest.raises(OSError):
            _tick(conn, machine, WED + timedelta(minutes=minute), failed=True)
    assert machine.status()["confirmed"] == 1
    assert _notices(conn) == [(5, "stopped", "")]


def test_a_failing_activity_write_does_not_skip_the_result_record_of_a_request(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    _degrade(conn, machine, WED)
    machine.request_resume(WED + timedelta(minutes=1), acknowledge=False)
    key = machine.status()["resume_requested_at"]
    machine.activity = _BrokenActivity()
    with pytest.raises(OSError):
        _tick(conn, machine, WED + timedelta(minutes=2))
    assert _notices(conn) == [(5, "resume_accepted", key), (5, "resumed", "")]


def test_a_failed_notice_write_is_retried_on_the_next_tick(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    _tick(conn, machine, WED, failed=True)
    conn.execute("DROP TABLE datafeed_outage_notices")
    conn.commit()
    _tick(conn, machine, WED + timedelta(minutes=1), failed=True)   # confirmed。記録は失敗する
    assert machine.status()["confirmed"] == 1
    init_db(conn)                                                      # 表が戻る
    _tick(conn, machine, WED + timedelta(minutes=2), failed=True)
    rows = conn.execute("SELECT epoch, notice_state, occurred_at FROM datafeed_outage_notices").fetchall()
    assert [(r["epoch"], r["notice_state"], r["occurred_at"]) for r in rows] == [
        (5, "stopped", (WED + timedelta(minutes=1)).isoformat())]


def test_pending_notices_are_bounded_and_the_oldest_are_dropped(tmp_path, caplog):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    conn.execute("DROP TABLE datafeed_outage_notices")
    conn.commit()
    with caplog.at_level(logging.WARNING):
        for epoch in range(100, 170):
            machine._record_notices(epoch, WED, [("stopped", "")])
    assert sum(len(entries) for _e, _t, entries in machine._pending_notices) == 64
    assert machine._pending_notices[0][0] == 106
    assert "datafeed_notice_dropped" in caplog.text


def test_a_lock_failure_of_the_first_write_is_retried(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    conn.execute("PRAGMA busy_timeout=0")
    other = connect(tmp_path / "notices.db")
    other.execute("BEGIN IMMEDIATE")
    machine._record_notices(7, WED, [("stopped", "")])
    other.rollback()
    assert _notices(conn) == []
    _tick(conn, machine, WED + timedelta(minutes=1))
    assert _notices(conn) == [(7, "stopped", "")]


def test_an_unexpected_error_reading_used_request_keys_propagates(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=7)
    conn.execute("DROP TABLE datafeed_outage_notices")
    conn.execute("CREATE VIEW datafeed_outage_notices AS SELECT abs(-9223372036854775808) AS request_key")
    conn.commit()
    with pytest.raises(sqlite3.OperationalError):
        machine.request_resume(WED, acknowledge=False)
    assert machine.status()["resume_requested_at"] is None


def test_a_rolled_back_consumption_keeps_the_unprocessed_memo(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    _degrade(conn, machine, WED)
    machine.request_resume(WED + timedelta(minutes=1), acknowledge=False)
    _replace_during_consumption(machine, tmp_path)
    _tick(conn, machine, WED + timedelta(minutes=2))
    assert machine._last_unprocessed_by_pair is not None


# ---- 保留の再試行の位置と量 ----------------------------------------------

@pytest.mark.parametrize("table_present", [True, False])
def test_a_pending_retry_never_commits_or_rolls_back_the_callers_transaction(tmp_path, table_present):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    conn.execute("DROP TABLE datafeed_outage_notices")
    conn.commit()
    machine._record_notices(9, WED, [("stopped", "")])
    assert len(machine._pending_notices) == 1
    if table_present:
        init_db(conn)
    conn.execute("UPDATE datafeed_outage_state SET updated_at='caller-marker' WHERE id=1")
    with pytest.raises(RuntimeError):
        machine.observe(WED, _report())
    assert conn.in_transaction
    assert conn.execute("SELECT updated_at FROM datafeed_outage_state").fetchone()[0] == "caller-marker"
    other = connect(tmp_path / "notices.db")
    assert other.execute("SELECT updated_at FROM datafeed_outage_state").fetchone()[0] != "caller-marker"
    assert len(machine._pending_notices) == 1
    conn.rollback()


def test_a_pending_retry_stops_at_the_first_failure(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    machine._pending_notices = [(100 + n, WED, [("stopped", "")]) for n in range(64)]
    calls = []

    def failing(epoch, now, entries):
        calls.append(epoch)
        return False

    machine._write_notices = failing
    _tick(conn, machine, WED)
    assert calls == [100]
    assert len(machine._pending_notices) == 64
    assert [item[0] for item in machine._pending_notices] == list(range(100, 164))


def test_a_pending_retry_runs_after_the_closed_market_path_too(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="ready", epoch=4)
    conn.execute("DROP TABLE datafeed_outage_notices")
    conn.commit()
    machine._record_notices(9, WED, [("stopped", "")])
    init_db(conn)
    saturday = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    machine.observe(saturday, _report())
    assert _notices(conn) == [(9, "stopped", "")]
