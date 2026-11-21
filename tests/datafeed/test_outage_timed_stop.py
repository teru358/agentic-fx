"""停止の通知の時間条件: 開場時間で 10 分たっても待ち・復帰にならない degraded を一度だけ記録する。

時刻はすべて UTC の実時刻の例 (2026-09/10 は夏時間で日次境界は 21:00 UTC)。
"""
import logging
from datetime import datetime, timedelta, timezone

from agentic_fx.datafeed.outage import OutageStateMachine

from .test_outage_notices import KEY, WED, WIDTHS, _rig, _set, _states, _tick

UTC = timezone.utc
FRI_CLOSE = datetime(2026, 10, 2, 20, 56, tzinfo=UTC)   # 金曜、閉場の 4 分前
SUN_OPEN = datetime(2026, 10, 4, 21, 0, tzinfo=UTC)     # 日曜の開場


def _alternate(conn, machine, start, count, *, first_failed=True):
    """1 分ごとに失敗と健全を交互に観測する。失敗が 2 連続せず、健全も 3 連続しないので、
    confirmed にも自動復帰にもならない。各 tick の後の通知の記録を返す。"""
    seen = []
    for index in range(count):
        failed = (index % 2 == 0) == first_failed
        _tick(conn, machine, start + timedelta(minutes=index), failed=failed)
        seen.append(_states(conn))
    return seen


def _fresh_machine(conn):
    machine = OutageStateMachine(conn, hard_keys=frozenset({KEY}), interval_widths=WIDTHS,
                                 grace=timedelta(seconds=30), storage_source="mt5-live")
    machine._reset_ready_streak = False
    return machine


def test_an_unconfirmed_stop_is_recorded_once_at_ten_open_minutes(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, entered_degraded_at=WED.isoformat())
    seen = _alternate(conn, machine, WED + timedelta(minutes=1), 14)
    # 経過 1〜9 分の tick (index 0〜8) では何も無く、10 分の tick (index 9) で一度だけ
    assert seen[:9] == [[]] * 9
    assert seen[9:] == [["stopped"]] * 5
    row = conn.execute("SELECT epoch, occurred_at FROM datafeed_outage_notices").fetchone()
    assert row["epoch"] == 3 and row["occurred_at"] == (WED + timedelta(minutes=10)).isoformat()


def test_the_time_condition_counts_open_minutes_and_skips_the_weekend(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, entered_degraded_at=FRI_CLOSE.isoformat())
    _tick(conn, machine, datetime(2026, 10, 3, 12, 0, tzinfo=UTC), failed=True)  # 土曜: 休場
    assert _states(conn) == []
    seen = _alternate(conn, machine, SUN_OPEN + timedelta(minutes=1), 6)
    # 金曜の開場 4 分 + 日曜 21:01〜 の開場分。日曜 21:05 までは 9 分、21:06 で 10 分
    assert seen[:5] == [[]] * 5
    assert seen[5] == ["stopped"]


def test_a_restart_does_not_restart_the_open_time(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, entered_degraded_at=WED.isoformat())
    assert _alternate(conn, machine, WED + timedelta(minutes=1), 5)[-1] == []
    restarted = _fresh_machine(conn)
    seen = _alternate(conn, restarted, WED + timedelta(minutes=6), 5, first_failed=False)
    assert seen[:4] == [[]] * 4 and seen[4] == ["stopped"]


def test_no_stop_is_recorded_after_the_wait_was(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, entered_degraded_at=WED.isoformat(),
         pending_human_confirmation=1)
    _tick(conn, machine, WED + timedelta(minutes=1))                 # 健全: 待ちを記録
    _tick(conn, machine, WED + timedelta(minutes=2), failed=True)
    _tick(conn, machine, WED + timedelta(minutes=3), failed=True)    # 2 連続: confirmed
    for minute in range(4, 14):
        _tick(conn, machine, WED + timedelta(minutes=minute))
    assert _states(conn) == ["awaiting_resume"]


def test_no_stop_is_recorded_when_the_state_came_back_to_ready_first(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, entered_degraded_at=WED.isoformat())
    for minute in range(1, 16):
        _tick(conn, machine, WED + timedelta(minutes=minute))
    assert machine.state == "ready"
    assert _states(conn) == []


def test_a_recovery_after_the_time_stop_records_the_resumption(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, entered_degraded_at=WED.isoformat())
    _alternate(conn, machine, WED + timedelta(minutes=1), 10)
    assert _states(conn) == ["stopped"]
    for minute in range(11, 14):
        _tick(conn, machine, WED + timedelta(minutes=minute))
    assert machine.state == "ready"
    assert _states(conn) == ["stopped", "resumed"]


def test_the_confirmed_stop_after_the_time_stop_adds_no_second_record(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, entered_degraded_at=WED.isoformat())
    _alternate(conn, machine, WED + timedelta(minutes=1), 10)
    _tick(conn, machine, WED + timedelta(minutes=11), failed=True)
    _tick(conn, machine, WED + timedelta(minutes=12), failed=True)   # 2 連続で confirmed
    assert machine.status()["confirmed"] == 1
    assert _states(conn) == ["stopped"]


def test_a_recovery_without_any_stop_or_wait_record_stays_silent(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, entered_degraded_at=WED.isoformat())
    for minute in range(1, 5):
        _tick(conn, machine, WED + timedelta(minutes=minute))
    assert machine.state == "ready" and _states(conn) == []


def test_a_broken_notice_table_never_fails_the_observation(tmp_path, caplog):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, entered_degraded_at=WED.isoformat())
    conn.execute("DROP TABLE datafeed_outage_notices")
    conn.commit()
    with caplog.at_level(logging.WARNING):
        for index in range(12):
            _tick(conn, machine, WED + timedelta(minutes=1 + index), failed=index % 2 == 0)
    assert machine.state == "degraded"
    assert machine._observe_failed is False
    assert any(r.getMessage() == "datafeed_notice_deferred" for r in caplog.records)


def test_the_time_condition_does_not_touch_the_stop_decision_itself(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, entered_degraded_at=WED.isoformat(),
         pending_human_confirmation=1)
    before = machine.status()
    _alternate(conn, machine, WED + timedelta(minutes=1), 12)
    after = machine.status()
    assert after["state"] == "degraded" and after["epoch"] == 3
    assert after["pending_human_confirmation"] == before["pending_human_confirmation"] == 1


def test_no_stop_is_recorded_when_a_resume_request_is_waiting_at_the_tenth_minute(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, entered_degraded_at=WED.isoformat())
    assert _alternate(conn, machine, WED + timedelta(minutes=1), 8)[-1] == []
    machine.request_resume(WED + timedelta(minutes=9, seconds=30), acknowledge=False)
    _tick(conn, machine, WED + timedelta(minutes=10), failed=True)
    assert _states(conn) == ["resume_rejected"]


def test_the_open_time_restarts_from_the_result_of_a_request_and_the_stop_is_then_recorded(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, entered_degraded_at=WED.isoformat())
    machine.request_resume(WED, acknowledge=False)
    _tick(conn, machine, WED + timedelta(minutes=1), failed=True)   # 取得不健全のため拒否
    assert machine.status()["resume_last_result"] == "rejected_watermark"
    seen = []
    for index in range(2, 14):
        _tick(conn, machine, WED + timedelta(minutes=index), failed=index % 2 == 1)
        seen.append(_states(conn))
    # 判定 (+1 分) から開場時間 10 分の tick (+11 分) まで停止は無く、そこで一度だけ記録する
    assert seen[:9] == [["resume_rejected"]] * 9
    assert seen[9:] == [["resume_rejected", "stopped"]] * 3
    row = conn.execute("SELECT occurred_at FROM datafeed_outage_notices "
                       "WHERE notice_state='stopped'").fetchone()
    assert row["occurred_at"] == (WED + timedelta(minutes=11)).isoformat()


def test_a_confirmed_stop_after_a_request_is_recorded_as_usual(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, entered_degraded_at=WED.isoformat())
    machine.request_resume(WED, acknowledge=False)
    _tick(conn, machine, WED + timedelta(minutes=1), failed=True)   # 拒否
    _tick(conn, machine, WED + timedelta(minutes=2), failed=True)   # 2 連続: confirmed
    assert machine.status()["confirmed"] == 1
    assert _states(conn) == ["resume_rejected", "stopped"]


def _block_notice_writes(conn):
    conn.execute("CREATE TRIGGER block_notice BEFORE INSERT ON datafeed_outage_notices "
                 "BEGIN SELECT RAISE(ABORT,'blocked'); END")
    conn.commit()


def _unblock_notice_writes(conn):
    conn.execute("DROP TRIGGER block_notice")
    conn.commit()


def test_a_stop_whose_write_keeps_failing_is_queued_once_and_never_pushes_out_a_result(
        tmp_path, caplog):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, entered_degraded_at=WED.isoformat())
    _block_notice_writes(conn)
    machine.request_resume(WED, acknowledge=False)
    _tick(conn, machine, WED + timedelta(minutes=1), failed=True)   # 拒否の記録も失敗して保留
    with caplog.at_level(logging.WARNING):
        for index in range(2, 100):
            _tick(conn, machine, WED + timedelta(minutes=index), failed=index % 2 == 1)
    queued = [(state, key != "") for _e, _t, entries in machine._pending_notices
              for state, key in entries]
    assert sorted(queued) == [("resume_rejected", True), ("stopped", False)]
    assert not any(r.getMessage() == "datafeed_notice_dropped" for r in caplog.records)
    _unblock_notice_writes(conn)
    _tick(conn, machine, WED + timedelta(minutes=100), failed=False)
    assert sorted(_states(conn)) == ["resume_rejected", "stopped"]


def test_a_recovery_counts_a_stop_that_is_still_queued_and_records_the_resumption(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, entered_degraded_at=WED.isoformat())
    _block_notice_writes(conn)
    _alternate(conn, machine, WED + timedelta(minutes=1), 10)       # 停止の記録が失敗して保留
    assert _pending_states(machine) == ["stopped"]
    for minute in range(11, 14):
        _tick(conn, machine, WED + timedelta(minutes=minute))       # 自動復帰
    assert machine.state == "ready"
    _unblock_notice_writes(conn)
    _tick(conn, machine, WED + timedelta(minutes=14))
    assert _states(conn) == ["stopped", "resumed"]


def _pending_states(machine):
    return [state for _e, _t, entries in machine._pending_notices for state, _k in entries]


def test_a_stop_already_announced_is_not_announced_again_after_a_rejection(tmp_path):
    conn, machine = _rig(tmp_path)
    _set(conn, machine, state="degraded", epoch=3, entered_degraded_at=WED.isoformat())
    _tick(conn, machine, WED + timedelta(minutes=1), failed=True)
    _tick(conn, machine, WED + timedelta(minutes=2), failed=True)   # confirmed: 停止を記録
    machine.request_resume(WED + timedelta(minutes=3), acknowledge=False)
    _tick(conn, machine, WED + timedelta(minutes=4), failed=True)   # 拒否
    for index in range(5, 40):
        _tick(conn, machine, WED + timedelta(minutes=index), failed=index % 2 == 0)
    assert _states(conn) == ["stopped", "resume_rejected"]
