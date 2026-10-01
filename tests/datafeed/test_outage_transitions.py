"""OutageStateMachine の遷移表 (状態 x exposure x 観測) と期限・連続 healthy の境界。

期待値は spec の遷移表から手で書いた値で、実装の出力を写していない。
時刻はすべて実時刻 (UTC、水曜の取引時間中) の例で固定する。
"""
from datetime import datetime, timedelta, timezone

import sqlite3

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.outage import EMPTY_REPORT, IngestTickReport, OutageStateMachine
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.ohlcv import upsert_cache_bars

PAIR = "USDJPY"
KEY = (PAIR, "1m")
UTC = timezone.utc
NOW = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
GRACE = timedelta(seconds=30)
WIDTHS = {"1m": timedelta(minutes=1)}


def _rig(tmp_path, *, hard_keys=frozenset({KEY}), activity=None, **kw):
    conn = connect(tmp_path / "outage.db")
    init_db(conn)
    machine = OutageStateMachine(conn, hard_keys=hard_keys, interval_widths=WIDTHS,
                                 grace=GRACE, storage_source="mt5-live",
                                 activity=activity, **kw)
    return conn, machine


def _seed_bar(conn, bar_time, pair=PAIR):
    upsert_cache_bars(conn, [Bar(pair, "1m", bar_time, 1, 1, 1, 1, 1)], source="mt5-live")
    conn.commit()


def _fresh(conn, now, pair=PAIR):
    """now の時点で停滞していない (直近の確定足がある) 状態にする。"""
    _seed_bar(conn, now - timedelta(minutes=2), pair)


def _report(*, ok=(KEY,), empty=(), failed=()):
    return IngestTickReport(
        attempted=frozenset(ok) | frozenset(failed), succeeded=frozenset(ok),
        failed=frozenset((k, "ConnectError") for k in failed),
        deferred=frozenset(), empty=frozenset(empty))


def _put_state(conn, machine, state, *, deadline=None, pending=0, streak=0, epoch=5):
    machine._ensure_row(NOW)
    since = (deadline - timedelta(seconds=1800)).isoformat() if deadline else None
    conn.execute(
        "UPDATE datafeed_outage_state SET state=?, epoch=?, restricted_since=?, "
        "restricted_deadline_at=?, pending_human_confirmation=?, ready_streak=? WHERE id=1",
        (state, epoch, since, deadline.isoformat() if deadline else None, pending, streak))
    conn.commit()
    machine._reset_ready_streak = False


def _open_position(conn, now=NOW):
    orders.insert(conn, pair=PAIR, direction="buy", entry_type="market",
                  horizon="swing", status="open", now=now)
    conn.commit()


def _observation(conn, kind):
    """観測の種類ごとに、cache の watermark と ingest の報告を用意する。"""
    if kind == "stalled":
        _seed_bar(conn, NOW - timedelta(minutes=10))
        return _report()
    _fresh(conn, NOW)
    if kind == "healthy":
        return _report()
    if kind == "empty":
        return _report(empty=(KEY,))
    if kind == "failed":
        return _report(ok=(), failed=(KEY,))
    raise AssertionError(kind)


# (現在の state, exposure, 観測) -> (次 state, epoch の増分, pending)
CELLS = [
    ("ready", False, "healthy", "ready", 0, 0),
    ("ready", False, "stalled", "restricted", 1, 0),
    ("ready", False, "empty", "degraded", 1, 0),
    ("ready", False, "failed", "degraded", 1, 0),
    ("ready", True, "healthy", "ready", 0, 0),
    ("ready", True, "stalled", "degraded", 1, 1),
    ("ready", True, "empty", "degraded", 1, 1),
    ("ready", True, "failed", "degraded", 1, 1),
    ("restricted", False, "healthy", "restricted", 0, 0),
    ("restricted", False, "stalled", "restricted", 0, 0),
    ("restricted", False, "empty", "degraded", 0, 0),
    ("restricted", False, "failed", "degraded", 0, 0),
    ("restricted", True, "healthy", "degraded", 0, 1),
    ("restricted", True, "stalled", "degraded", 0, 1),
    ("restricted", True, "empty", "degraded", 0, 1),
    ("restricted", True, "failed", "degraded", 0, 1),
    ("degraded", False, "healthy", "degraded", 0, 0),
    ("degraded", False, "stalled", "degraded", 0, 0),
    ("degraded", False, "empty", "degraded", 0, 0),
    ("degraded", False, "failed", "degraded", 0, 0),
    ("degraded", True, "healthy", "degraded", 0, 0),
    ("degraded", True, "stalled", "degraded", 0, 0),
    ("degraded", True, "empty", "degraded", 0, 0),
    ("degraded", True, "failed", "degraded", 0, 0),
]


@pytest.mark.parametrize("state,exposure,kind,want_state,want_epoch_step,want_pending", CELLS)
def test_transition_table_cell(tmp_path, state, exposure, kind, want_state,
                               want_epoch_step, want_pending):
    conn, machine = _rig(tmp_path)
    deadline = NOW + timedelta(minutes=10) if state == "restricted" else None
    _put_state(conn, machine, state, deadline=deadline)
    if exposure:
        _open_position(conn, NOW - timedelta(minutes=30))
    report = _observation(conn, kind)

    assert machine.observe(NOW, report) == want_state
    row = machine.status()
    assert row["state"] == want_state
    assert row["epoch"] == 5 + want_epoch_step
    assert row["pending_human_confirmation"] == want_pending
    if want_state == "restricted":
        # 期限は後発の観測で動かない (新規なら起点 + 1800 秒)
        if state == "restricted":
            assert row["restricted_deadline_at"] == deadline.isoformat()
        else:
            assert row["restricted_deadline_at"] == "2026-09-30T10:22:30+00:00"


def test_new_restricted_episode_records_origin_and_deadline_from_the_stalled_key(tmp_path):
    conn, machine = _rig(tmp_path)
    _put_state(conn, machine, "ready")
    _seed_bar(conn, datetime(2026, 9, 30, 9, 50, tzinfo=UTC))

    assert machine.observe(NOW, _report()) == "restricted"
    row = machine.status()
    assert row["restricted_since"] == "2026-09-30T09:52:30+00:00"
    assert row["restricted_deadline_at"] == "2026-09-30T10:22:30+00:00"


def test_healthy_exactly_at_the_deadline_stays_restricted(tmp_path):
    conn, machine = _rig(tmp_path)
    deadline = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
    _put_state(conn, machine, "restricted", deadline=deadline)
    _fresh(conn, NOW)

    assert machine.observe(deadline, _report()) == "restricted"


def test_healthy_one_second_after_the_deadline_is_degraded_without_human_confirmation(tmp_path):
    conn, machine = _rig(tmp_path)
    deadline = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
    _put_state(conn, machine, "restricted", deadline=deadline, streak=2)
    now = deadline + timedelta(seconds=1)
    _fresh(conn, now)

    assert machine.observe(now, _report()) == "degraded"
    row = machine.status()
    assert row["pending_human_confirmation"] == 0
    assert row["epoch"] == 5


def test_deadline_passed_with_auto_resume_disabled_requires_human_confirmation(tmp_path):
    conn, machine = _rig(tmp_path, auto_resume_when_flat=False)
    deadline = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
    _put_state(conn, machine, "restricted", deadline=deadline)
    now = deadline + timedelta(seconds=1)
    _fresh(conn, now)

    assert machine.observe(now, _report()) == "degraded"
    assert machine.status()["pending_human_confirmation"] == 1


def test_third_healthy_tick_returns_a_restricted_state_to_ready(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    conn, machine = _rig(tmp_path, activity=log)
    deadline = NOW + timedelta(minutes=20)
    _put_state(conn, machine, "restricted", deadline=deadline)
    states = []
    for minute in (0, 1, 2):
        now = NOW + timedelta(minutes=minute)
        _fresh(conn, now)
        states.append(machine.observe(now, _report()))

    assert states == ["restricted", "restricted", "ready"]
    row = machine.status()
    assert row["restricted_deadline_at"] is None
    assert row["restricted_since"] is None
    assert row["epoch"] == 5
    assert [line.split("\t")[2] for line in log.tail(10)] == ["datafeed_recovered_auto"]


def test_exposure_appearing_on_the_third_healthy_tick_degrades_instead_of_ready(tmp_path):
    conn, machine = _rig(tmp_path)
    _put_state(conn, machine, "restricted", deadline=NOW + timedelta(minutes=20))
    for minute in (0, 1):
        now = NOW + timedelta(minutes=minute)
        _fresh(conn, now)
        assert machine.observe(now, _report()) == "restricted"
    now = NOW + timedelta(minutes=2)
    _fresh(conn, now)
    _open_position(conn, now)

    assert machine.observe(now, _report()) == "degraded"
    assert machine.status()["pending_human_confirmation"] == 1


def test_auto_resume_disabled_third_healthy_tick_degrades_with_human_confirmation(tmp_path):
    conn, machine = _rig(tmp_path, auto_resume_when_flat=False)
    _put_state(conn, machine, "restricted", deadline=NOW + timedelta(minutes=20))
    states = []
    for minute in (0, 1, 2):
        now = NOW + timedelta(minutes=minute)
        _fresh(conn, now)
        states.append(machine.observe(now, _report()))

    assert states == ["restricted", "restricted", "degraded"]
    assert machine.status()["pending_human_confirmation"] == 1


@pytest.mark.parametrize("not_healthy", [
    IngestTickReport(attempted=frozenset(), succeeded=frozenset(), failed=frozenset(),
                     deferred=frozenset({KEY}), empty=frozenset()),
    EMPTY_REPORT,
])
def test_deferred_or_not_attempted_tick_does_not_advance_the_healthy_streak(
        tmp_path, not_healthy):
    conn, machine = _rig(tmp_path)
    _put_state(conn, machine, "restricted", deadline=NOW + timedelta(minutes=30))
    reports = [_report(), _report(), not_healthy, _report(), _report()]
    states = []
    for minute, report in enumerate(reports):
        now = NOW + timedelta(minutes=minute)
        _fresh(conn, now)
        states.append(machine.observe(now, report))

    # 5 tick のうち連続 healthy は 2 本ずつ。3 本そろわないので ready に戻らない。
    assert states == ["restricted"] * 5
    now = NOW + timedelta(minutes=5)
    _fresh(conn, now)
    assert machine.observe(now, _report()) == "ready"


def test_healthy_streak_restarts_from_zero_after_a_process_restart(tmp_path):
    conn, machine = _rig(tmp_path)
    deadline = NOW + timedelta(minutes=30)
    _put_state(conn, machine, "restricted", deadline=deadline)
    for minute in (0, 1):
        now = NOW + timedelta(minutes=minute)
        _fresh(conn, now)
        machine.observe(now, _report())
    restarted = OutageStateMachine(conn, hard_keys=frozenset({KEY}), interval_widths=WIDTHS,
                                   grace=GRACE, storage_source="mt5-live")
    states = []
    for minute in (2, 3, 4):
        now = NOW + timedelta(minutes=minute)
        _fresh(conn, now)
        states.append(restarted.observe(now, _report()))

    # 再起動後は streak を 0 から数え直すが、期限と state は DB の値を引き継ぐ。
    assert states == ["restricted", "restricted", "ready"]


def test_restart_keeps_the_deadline_stored_in_the_database(tmp_path):
    conn, machine = _rig(tmp_path)
    _put_state(conn, machine, "restricted", deadline=datetime(2026, 9, 30, 10, 5, tzinfo=UTC))
    restarted = OutageStateMachine(conn, hard_keys=frozenset({KEY}), interval_widths=WIDTHS,
                                   grace=GRACE, storage_source="mt5-live")
    now = datetime(2026, 9, 30, 10, 5, 1, tzinfo=UTC)
    _fresh(conn, now)

    assert restarted.observe(now, _report()) == "degraded"


def test_pending_confirmation_keeps_a_restricted_state_from_returning_to_ready(tmp_path):
    conn, machine = _rig(tmp_path)
    _put_state(conn, machine, "restricted", deadline=NOW + timedelta(minutes=20), pending=1)
    states = []
    for minute in (0, 1, 2):
        now = NOW + timedelta(minutes=minute)
        _fresh(conn, now)
        states.append(machine.observe(now, _report()))

    assert states == ["restricted", "restricted", "degraded"]
    assert machine.status()["pending_human_confirmation"] == 1


def test_promotion_from_restricted_keeps_the_recorded_origin_and_deadline(tmp_path):
    conn, machine = _rig(tmp_path)
    deadline = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
    _put_state(conn, machine, "restricted", deadline=deadline)
    now = deadline + timedelta(seconds=1)
    _fresh(conn, now)

    assert machine.observe(now, _report()) == "degraded"
    row = machine.status()
    assert row["restricted_deadline_at"] == deadline.isoformat()
    assert row["restricted_since"] == (deadline - timedelta(seconds=1800)).isoformat()


def test_awaiting_resume_is_announced_only_after_a_healthy_tick(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    conn, machine = _rig(tmp_path, activity=log, auto_resume_when_flat=False)
    _put_state(conn, machine, "degraded", pending=1)
    deferred = IngestTickReport(attempted=frozenset(), succeeded=frozenset(),
                                failed=frozenset(), deferred=frozenset({KEY}),
                                empty=frozenset())
    _fresh(conn, NOW)

    machine.observe(NOW, deferred)
    assert "datafeed_recovered_awaiting_resume" not in [
        line.split("\t")[2] for line in log.tail(10)]

    now = NOW + timedelta(minutes=1)
    _fresh(conn, now)
    machine.observe(now, _report())
    assert [line.split("\t")[2] for line in log.tail(10)].count(
        "datafeed_recovered_awaiting_resume") == 1


def test_restricted_stalled_ticks_do_not_mark_the_episode_confirmed(tmp_path):
    conn, machine = _rig(tmp_path)
    _put_state(conn, machine, "ready")
    _seed_bar(conn, datetime(2026, 9, 30, 9, 50, tzinfo=UTC))
    states = [machine.observe(NOW + timedelta(seconds=30 * i), _report()) for i in range(4)]

    assert states == ["restricted"] * 4
    assert machine.status()["confirmed"] == 0


def test_second_stalled_tick_with_an_exposure_confirms_the_outage_and_notifies(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    conn, machine = _rig(tmp_path, activity=log)
    _put_state(conn, machine, "ready")
    _open_position(conn, NOW - timedelta(minutes=30))
    _seed_bar(conn, NOW - timedelta(minutes=10))

    assert machine.observe(NOW, _report()) == "degraded"
    assert machine.status()["confirmed"] == 0
    assert machine.observe(NOW + timedelta(seconds=30), _report()) == "degraded"
    assert machine.status()["confirmed"] == 1
    assert "data_outage_degraded" in [line.split("\t")[2] for line in log.tail(20)]


def test_accepted_resume_clears_the_pending_human_confirmation(tmp_path):
    conn, machine = _rig(tmp_path)
    _put_state(conn, machine, "degraded", pending=1)
    _fresh(conn, NOW)
    machine.request_resume(NOW, acknowledge=True)

    assert machine.observe(NOW, _report()) == "ready"
    assert machine.status()["pending_human_confirmation"] == 0


KEY_15M = (PAIR, "15m")
WIDTHS_WITH_15M = {"1m": timedelta(minutes=1), "15m": timedelta(minutes=15)}


def _rig_with_15m(tmp_path, **kw):
    conn = connect(tmp_path / "outage.db")
    init_db(conn)
    machine = OutageStateMachine(
        conn, hard_keys=frozenset({KEY, KEY_15M}), interval_widths=WIDTHS_WITH_15M,
        grace=GRACE, storage_source="mt5-live", **kw)
    return conn, machine


def _fresh_both(conn, now):
    _fresh(conn, now)
    upsert_cache_bars(conn, [Bar(PAIR, "15m", now - timedelta(minutes=20), 1, 1, 1, 1, 1)],
                      source="mt5-live")
    conn.commit()


def _only_1m():
    """15m は取りに行かない tick の報告 (attempted にも deferred にも入らない)。"""
    return _report(ok=(KEY,))


def _both():
    return _report(ok=(KEY, KEY_15M))


@pytest.mark.parametrize("start_state", ["restricted", "degraded"])
def test_a_higher_timeframe_key_probed_once_in_a_while_does_not_block_auto_recovery(
        tmp_path, start_state):
    conn, machine = _rig_with_15m(tmp_path)
    _put_state(conn, machine, start_state,
               deadline=NOW + timedelta(minutes=30) if start_state == "restricted" else None)
    states = []
    for minute, report in enumerate([_both(), _only_1m(), _only_1m()]):
        now = NOW + timedelta(minutes=minute)
        _fresh_both(conn, now)
        states.append(machine.observe(now, report))

    assert states == [start_state, start_state, "ready"]


def test_a_failed_higher_timeframe_key_degrades_immediately(tmp_path):
    conn, machine = _rig_with_15m(tmp_path)
    _put_state(conn, machine, "restricted", deadline=NOW + timedelta(minutes=30))
    _fresh_both(conn, NOW)

    assert machine.observe(NOW, _report(ok=(KEY,), failed=(KEY_15M,))) == "degraded"


def test_a_failed_higher_timeframe_key_from_ready_opens_a_degraded_episode(tmp_path):
    conn, machine = _rig_with_15m(tmp_path)
    _fresh_both(conn, NOW)

    assert machine.observe(NOW, _report(ok=(KEY,), failed=(KEY_15M,))) == "degraded"


def test_a_higher_timeframe_failure_blocks_recovery_until_that_key_succeeds_again(tmp_path):
    conn, machine = _rig_with_15m(tmp_path)
    _put_state(conn, machine, "degraded")
    states = []
    reports = [_report(ok=(KEY,), failed=(KEY_15M,)), _only_1m(), _only_1m(), _only_1m(),
               _both(), _only_1m(), _only_1m()]
    for minute, report in enumerate(reports):
        now = NOW + timedelta(minutes=minute)
        _fresh_both(conn, now)
        states.append(machine.observe(now, report))

    assert states == ["degraded"] * 6 + ["ready"]


@pytest.mark.parametrize("skipped", [
    IngestTickReport(attempted=frozenset(), succeeded=frozenset({KEY_15M}),
                     failed=frozenset(), deferred=frozenset({KEY}), empty=frozenset()),
    IngestTickReport(attempted=frozenset(), succeeded=frozenset({KEY_15M}),
                     failed=frozenset(), deferred=frozenset(), empty=frozenset()),
])
def test_a_1m_key_that_is_deferred_or_not_attempted_does_not_advance_the_streak(
        tmp_path, skipped):
    conn, machine = _rig_with_15m(tmp_path)
    _put_state(conn, machine, "restricted", deadline=NOW + timedelta(minutes=30))
    states = []
    for minute, report in enumerate([_both(), _only_1m(), skipped, _only_1m(), _only_1m()]):
        now = NOW + timedelta(minutes=minute)
        _fresh_both(conn, now)
        states.append(machine.observe(now, report))
    assert states == ["restricted"] * 5
    now = NOW + timedelta(minutes=5)
    _fresh_both(conn, now)
    assert machine.observe(now, _only_1m()) == "ready"


def test_degrading_from_restricted_discards_the_healthy_streak_collected_so_far(tmp_path):
    conn, machine = _rig(tmp_path)
    deadline = NOW + timedelta(minutes=2, seconds=30)
    _put_state(conn, machine, "restricted", deadline=deadline)
    states = []
    # 10:00 / 10:01 は健全 (streak=2)、10:03 は期限 10:02:30 を過ぎて degraded
    for minute in (0, 1, 3):
        now = NOW + timedelta(minutes=minute)
        _fresh(conn, now)
        states.append(machine.observe(now, _report()))
    assert states == ["restricted", "restricted", "degraded"]
    assert machine.status()["ready_streak"] == 0

    # degraded に上がってからは健全 tick が 3 回要る (1 回で戻らない)
    for minute in (4, 5, 6):
        now = NOW + timedelta(minutes=minute)
        _fresh(conn, now)
        states.append(machine.observe(now, _report()))
    assert states[3:] == ["degraded", "degraded", "ready"]


# ---- episode を開く tick の証拠 / 昇格 / 期限切れ後の初観測 -----------------


def _events_of(log):
    return [line.split("\t")[2] for line in log.tail(50)]


def test_resume_requested_while_ready_is_not_accepted_on_the_tick_a_stall_opens_an_episode(
        tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    conn, machine = _rig(tmp_path, activity=log)
    _fresh(conn, NOW)
    assert machine.observe(NOW, _report()) == "ready"
    machine.request_resume(NOW)
    now = NOW + timedelta(minutes=10)
    _seed_bar(conn, NOW - timedelta(minutes=2))

    assert machine.observe(now, _report()) == "restricted"
    assert "data_resume_rejected" in _events_of(log)
    assert "data_resume_accepted" not in _events_of(log)
    assert machine.status()["resume_requested_at"] is None


def test_higher_timeframe_success_on_the_episode_opening_tick_counts_as_confirmed_for_resume(
        tmp_path):
    conn, machine = _rig_with_15m(tmp_path, flat_stall_max_sec=0)
    _fresh_both(conn, NOW)
    assert machine.observe(NOW, _both()) == "ready"
    # 1m だけが止まる tick (15m は成功) で episode が開く
    now = NOW + timedelta(minutes=10)
    upsert_cache_bars(conn, [Bar(PAIR, "15m", now - timedelta(minutes=20), 1, 1, 1, 1, 1)],
                      source="mt5-live")
    conn.commit()
    assert machine.observe(now, _both()) == "degraded"
    # 1m が回復、15m は取りに行かない tick に resume を要求する
    later = now + timedelta(minutes=1)
    _fresh_both(conn, later)
    machine.request_resume(later)

    assert machine.observe(later, _only_1m()) == "ready"


def test_restricted_to_degraded_by_healthy_streak_announces_degraded_and_awaiting_resume(
        tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    conn, machine = _rig(tmp_path, activity=log, auto_resume_when_flat=False)
    _put_state(conn, machine, "restricted", deadline=NOW + timedelta(minutes=20))
    for minute in (0, 1, 2):
        now = NOW + timedelta(minutes=minute)
        _fresh(conn, now)
        machine.observe(now, _report())

    assert _events_of(log) == ["datafeed_degraded", "datafeed_recovered_awaiting_resume"]
    now = NOW + timedelta(minutes=3)
    _fresh(conn, now)
    machine.observe(now, _report())
    assert _events_of(log).count("datafeed_recovered_awaiting_resume") == 1


def test_first_sighting_of_a_stall_already_past_its_deadline_degrades_directly(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    conn, machine = _rig(tmp_path, activity=log, flat_stall_max_sec=1800)
    _seed_bar(conn, NOW - timedelta(hours=3))

    assert machine.observe(NOW, _report()) == "degraded"
    row = machine.status()
    assert row["epoch"] == 1
    assert row["restricted_since"] is None and row["restricted_deadline_at"] is None
    assert row["pending_human_confirmation"] == 0
    assert conn.execute("SELECT count(*) FROM datafeed_outage_gap WHERE epoch=1"
                        ).fetchone()[0] == 1
    assert _events_of(log) == ["datafeed_degraded"]


def test_first_sighting_of_a_stall_exactly_at_its_deadline_enters_restricted(tmp_path):
    conn, machine = _rig(tmp_path, flat_stall_max_sec=1800)
    watermark = NOW - timedelta(hours=3)
    _seed_bar(conn, watermark)
    expected = machine._expected("1m", machine._read_watermarks(NOW)[KEY])
    now = expected + timedelta(seconds=1800)

    assert machine.observe(now, _report()) == "restricted"


def test_first_sighting_past_its_deadline_with_auto_resume_disabled_needs_human_confirmation(
        tmp_path):
    conn, machine = _rig(tmp_path, auto_resume_when_flat=False)
    _seed_bar(conn, NOW - timedelta(hours=3))

    assert machine.observe(NOW, _report()) == "degraded"
    assert machine.status()["pending_human_confirmation"] == 1


def test_failure_while_accepting_a_resume_keeps_both_the_state_and_the_request(
        tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path)
    _put_state(conn, machine, "degraded", pending=1)
    _fresh(conn, NOW)
    machine.request_resume(NOW, acknowledge=True)
    original = machine._clear_resume_request

    def clear_then_crash():
        original()
        raise sqlite3.OperationalError("injected crash")

    monkeypatch.setattr(machine, "_clear_resume_request", clear_then_crash)
    with pytest.raises(sqlite3.OperationalError):
        machine.observe(NOW, _report())

    row = machine.status()
    assert row["state"] == "degraded"
    assert row["pending_human_confirmation"] == 1
    assert row["resume_requested_at"] is not None
