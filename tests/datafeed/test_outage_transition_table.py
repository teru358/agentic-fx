"""`outage_transition` の遷移表。入口 (観測・接触・健全) x 現 state x flat/建玉の全行を、
DB・時計なしの純関数として確かめる。

期待値は設計の状態表から手で書いた値で、実装の出力を写していない。時刻は実時刻の例で固定する
(水曜の取引時間中、日曜の開場直後、金曜の閉場をまたぐ restricted)。開場秒は手で数えた値。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.datafeed.outage_transition import (
    Facts, Finding, KeyView, outage_transition)

UTC = timezone.utc
PAIR = "USDJPY"
K1M = (PAIR, "1m")
K1H = (PAIR, "1h")
# 水曜の取引時間中 (閉場をまたがない)
WED = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


def _row(state="ready", **kw):
    row = {"state": state, "epoch": 5, "confirmed": 0, "entered_degraded_at": None,
           "restricted_since": None, "restricted_deadline_at": None,
           "pending_human_confirmation": 0, "recovered_notified_epoch": None,
           "ready_streak": 0, "resume_requested_at": None}
    row.update(kw)
    return row


def _k(key=K1M, status="ok", stalled=False, s=None, one_m=None):
    return KeyView(key, key[1] == "1m" if one_m is None else one_m, status, stalled, s)


def _f(now=WED, *, flat=True, keys=None, **kw):
    base = dict(now=now, flat=flat, keys=(_k(),) if keys is None else tuple(keys),
                healthy=True, advanced=True)
    base.update(kw)
    return Facts(**base)


def _run(row, facts):
    return outage_transition(row, facts)


def _finding(order_id=1, kind="sl", at=WED - timedelta(minutes=3)):
    return Finding(order_id, PAIR, at, kind)


# ---- R / T ---------------------------------------------------------------

def test_ready_healthy_stays_ready_without_a_new_epoch():
    nxt, eff = _run(_row(), _f())
    assert nxt["state"] == "ready" and nxt["epoch"] == 5 and not eff.opened
    assert eff.problem_streak == 0 and eff.notices == () and eff.activities == ()


@pytest.mark.parametrize("state", ["ready", "restricted", "degraded"])
def test_unattempted_non_stalled_key_keeps_the_state_and_adds_no_streak(state):
    row = _row(state, ready_streak=0, restricted_since=(WED - timedelta(minutes=5)).isoformat()
               if state == "restricted" else None)
    nxt, eff = _run(row, _f(keys=[_k(status="idle")], healthy=False))
    assert nxt["state"] == state and nxt["epoch"] == 5 and not eff.opened
    assert nxt["ready_streak"] == 0


# ---- D / F: 1m の停滞・失敗・空 ------------------------------------------

@pytest.mark.parametrize("flat,limit", [(True, 1800), (False, 600)])
def test_stall_inside_the_grace_opens_restricted_with_the_stall_start(flat, limit):
    s = WED - timedelta(seconds=90)
    nxt, eff = _run(_row(), _f(flat=flat, keys=[_k(stalled=True, s=s)], healthy=False))
    assert (nxt["state"], nxt["epoch"], eff.opened) == ("restricted", 6, True)
    assert nxt["restricted_since"] == s.isoformat()
    assert nxt["restricted_deadline_at"] == (s + timedelta(seconds=limit)).isoformat()
    assert nxt["pending_human_confirmation"] == 0
    assert [a[0] for a in eff.activities] == ["datafeed_restricted"]


@pytest.mark.parametrize("status", ["failed", "empty"])
@pytest.mark.parametrize("flat", [True, False])
def test_failure_or_empty_inside_the_grace_is_restricted_for_both_exposures(status, flat):
    nxt, eff = _run(_row(), _f(flat=flat, keys=[_k(status=status, s=WED)], healthy=False))
    assert nxt["state"] == "restricted" and eff.opened


@pytest.mark.parametrize("elapsed,want", [(600, "restricted"), (601, "degraded")])
def test_exposure_grace_is_600_open_seconds_and_exactly_600_is_still_restricted(elapsed, want):
    s = WED - timedelta(seconds=elapsed)
    nxt, _ = _run(_row("restricted", restricted_since=s.isoformat()),
                  _f(flat=False, keys=[_k(stalled=True, s=s)], healthy=False))
    assert nxt["state"] == want
    if want == "degraded":
        assert nxt["pending_human_confirmation"] == 1


@pytest.mark.parametrize("elapsed,want", [(1800, "restricted"), (1801, "degraded")])
def test_flat_grace_is_the_configured_seconds_and_exactly_the_limit_is_still_restricted(
        elapsed, want):
    s = WED - timedelta(seconds=elapsed)
    nxt, _ = _run(_row("restricted", restricted_since=(WED - timedelta(minutes=10)).isoformat()),
                  _f(keys=[_k(stalled=True, s=s)], healthy=False))
    assert nxt["state"] == want


def test_flat_grace_zero_degrades_at_the_first_problem_tick():
    nxt, eff = _run(_row(), _f(keys=[_k(status="failed", s=WED)], healthy=False,
                              flat_stall_max_sec=0))
    assert nxt["state"] == "degraded" and eff.opened and nxt["pending_human_confirmation"] == 0


def test_flat_grace_runs_from_the_stall_start_not_the_tick_that_noticed_it():
    # 前回から 40 分観測が途切れて初めて見た停滞。起点は 40 分前の足なので即 degraded
    s = WED - timedelta(minutes=40)
    nxt, eff = _run(_row(), _f(keys=[_k(stalled=True, s=s)], healthy=False))
    assert nxt["state"] == "degraded" and eff.opened


def test_stall_start_after_the_friday_close_counts_only_open_seconds():
    # 金曜 21:50Z (冬時間の閉場は 22:00Z) に始まった停滞。日曜の開場 22:00Z 後 2 分で
    # 経過した開場秒は 10 分 + 2 分 = 720 秒 (< 1800)。壁時計では 2 日超
    s = datetime(2026, 11, 20, 21, 50, tzinfo=UTC)
    now = datetime(2026, 11, 22, 22, 2, tzinfo=UTC)
    row = _row("restricted", restricted_since=s.isoformat())
    nxt, _ = _run(row, _f(now, keys=[_k(stalled=True, s=s)], healthy=False))
    assert nxt["state"] == "restricted"
    nxt, _ = _run(row, _f(now, flat=False, keys=[_k(stalled=True, s=s)], healthy=False))
    assert nxt["state"] == "degraded"  # 720 秒 > 建玉ありの 600 秒


def test_sunday_open_one_missing_minute_is_a_one_minute_restricted():
    # 日曜の開場直後。11-01 22:07 が欠け、22:06 までが水位。S=22:08:30。22:09 は猶予内
    s = datetime(2026, 11, 1, 22, 8, 30, tzinfo=UTC)
    now = datetime(2026, 11, 1, 22, 9, 0, tzinfo=UTC)
    nxt, eff = _run(_row(), _f(now, flat=False, keys=[_k(stalled=True, s=s)], healthy=False))
    assert nxt["state"] == "restricted" and nxt["pending_human_confirmation"] == 0
    # 後続 (22:08) が届いた tick は健全で、建玉ありでも同 tick で ready に戻る
    later = datetime(2026, 11, 1, 22, 9, 40, tzinfo=UTC)
    back, eff2 = _run(nxt | {"resume_requested_at": None}, _f(later, flat=False))
    assert back["state"] == "ready" and eff2.recovered


# ---- G: 上位 hard の不調 -------------------------------------------------

def test_upper_failure_alone_with_exposure_degrades_at_once():
    nxt, eff = _run(_row(), _f(flat=False, keys=[_k(), _k(K1H, status="failed", s=WED)],
                              healthy=False))
    assert nxt["state"] == "degraded" and nxt["pending_human_confirmation"] == 1 and eff.opened


def test_upper_failure_alone_when_flat_degrades_at_once():
    nxt, _ = _run(_row(), _f(keys=[_k(), _k(K1H, status="empty", s=WED)], healthy=False))
    assert nxt["state"] == "degraded"


def test_upper_stall_alone_when_flat_is_restricted_inside_the_configured_grace():
    s = WED - timedelta(minutes=5)
    nxt, _ = _run(_row(), _f(keys=[_k(), _k(K1H, stalled=True, s=s)], healthy=False))
    assert nxt["state"] == "restricted"


def test_upper_trouble_joins_the_one_minute_grace_when_the_one_minute_key_also_fails():
    nxt, _ = _run(_row(), _f(flat=False, keys=[_k(status="failed", s=WED),
                                              _k(K1H, status="failed", s=WED)], healthy=False))
    assert nxt["state"] == "restricted"  # 1m の猶予 (600 秒) に合流し、同 tick は即停止にしない


def test_upper_trouble_is_judged_by_its_own_start_inside_the_merged_grace():
    old = WED - timedelta(seconds=601)
    nxt, _ = _run(_row(), _f(flat=False, keys=[_k(status="failed", s=WED),
                                              _k(K1H, stalled=True, s=old)], healthy=False))
    assert nxt["state"] == "degraded"


# ---- E / H / I / P -------------------------------------------------------

def test_restricted_recovers_on_the_first_healthy_tick_without_extra_ticks():
    nxt, eff = _run(_row("restricted", restricted_since=(WED - timedelta(minutes=2)).isoformat()),
                    _f())
    assert nxt["state"] == "ready" and nxt["ready_streak"] == 0 and eff.recovered
    assert nxt["restricted_since"] is None and nxt["pending_human_confirmation"] == 0


def test_restricted_with_exposure_recovers_the_same_way():
    nxt, _ = _run(_row("restricted", restricted_since=(WED - timedelta(minutes=2)).isoformat()),
                  _f(flat=False))
    assert nxt["state"] == "ready"


def test_restricted_recovery_is_not_taken_while_the_tick_is_unhealthy():
    nxt, _ = _run(_row("restricted", restricted_since=(WED - timedelta(minutes=2)).isoformat(),
                       ready_streak=2), _f(healthy=False))
    assert nxt["state"] == "restricted" and nxt["ready_streak"] == 0


@pytest.mark.parametrize("blocker", [{"auto_resume": False}, {"migration_pending": True}])
def test_restricted_blocked_from_auto_recovery_waits_three_healthy_ticks_then_asks_a_human(
        blocker):
    since = (WED - timedelta(minutes=2)).isoformat()
    row = _row("restricted", restricted_since=since)
    for expected in (1, 2):
        row, _ = _run(row, _f(**blocker))
        assert row["state"] == "restricted" and row["ready_streak"] == expected
    nxt, eff = _run(row, _f(**blocker))
    assert nxt["state"] == "degraded"
    assert ("awaiting_resume", "") in eff.notices


def test_restricted_over_thirty_open_minutes_stops_even_with_a_healthy_tick():
    since = WED - timedelta(minutes=30, seconds=1)
    nxt, eff = _run(_row("restricted", restricted_since=since.isoformat()), _f())
    assert nxt["state"] == "degraded" and nxt["confirmed"] == 1 and eff.confirmed_stop
    assert ("stopped", "") in eff.notices


def test_restricted_for_exactly_thirty_open_minutes_still_recovers():
    since = WED - timedelta(minutes=30)
    nxt, _ = _run(_row("restricted", restricted_since=since.isoformat()), _f())
    assert nxt["state"] == "ready"


def test_restricted_limit_counts_open_time_across_the_friday_close():
    # 金曜 21:45Z 開始 (閉場 22:00Z まで 15 分)。日曜 22:10Z の開場後 10 分で 25 分: 継続
    since = datetime(2026, 11, 20, 21, 45, tzinfo=UTC).isoformat()
    row = _row("restricted", restricted_since=since)
    nxt, _ = _run(row, _f(datetime(2026, 11, 22, 22, 10, tzinfo=UTC), auto_resume=False))
    assert nxt["state"] == "restricted"
    # 22:16Z で 31 分 > 30 分
    nxt, _ = _run(row, _f(datetime(2026, 11, 22, 22, 16, tzinfo=UTC), auto_resume=False))
    assert nxt["state"] == "degraded"


def test_degraded_flat_auto_healthy_returns_after_three_advancing_ticks_even_with_pending():
    row = _row("degraded", pending_human_confirmation=1, confirmed=1,
               recovered_notified_epoch=5)
    for expected in (1, 2):
        row, eff = _run(row, _f())
        assert row["state"] == "degraded" and row["ready_streak"] == expected
        assert eff.notices == ()
    row, eff = _run(row, _f(noticed=True))
    assert row["state"] == "ready" and row["pending_human_confirmation"] == 0
    assert row["confirmed"] == 0 and ("resumed", "") in eff.notices


def test_degraded_streak_is_not_added_for_the_same_now_and_bar():
    row = _row("degraded", ready_streak=2)
    nxt, _ = _run(row, _f(advanced=False))
    assert nxt["state"] == "degraded" and nxt["ready_streak"] == 2


@pytest.mark.parametrize("facts", [
    dict(flat=False), dict(auto_resume=False), dict(contact_wait=True),
    dict(migration_pending=True), dict(healthy=False)])
def test_degraded_does_not_count_healthy_ticks_when_a_blocker_holds(facts):
    nxt, _ = _run(_row("degraded", ready_streak=2), _f(**facts))
    assert nxt["state"] == "degraded" and nxt["ready_streak"] == 0


@pytest.mark.parametrize("facts", [
    dict(flat=False), dict(auto_resume=False), dict(contact_wait=True),
    dict(migration_pending=True)])
def test_degraded_waits_for_a_human_and_says_so_once_when_healthy(facts):
    nxt, eff = _run(_row("degraded"), _f(**facts))
    assert ("awaiting_resume", "") in eff.notices and nxt["recovered_notified_epoch"] == 5
    again, eff2 = _run(nxt, _f(**facts))
    assert eff2.notices == ()


def test_degraded_does_not_announce_a_human_wait_while_a_request_is_pending():
    _, eff = _run(_row("degraded", resume_requested_at=WED.isoformat()), _f(flat=False))
    assert eff.notices == ()


def test_degraded_does_not_announce_a_human_wait_when_the_auto_recovery_is_running():
    _, eff = _run(_row("degraded"), _f())
    assert eff.notices == ()


@pytest.mark.parametrize("status", ["failed", "empty", "stalled"])
def test_degraded_with_trouble_stays_and_keeps_pending_and_contacts(status):
    key = _k(status="ok" if status == "stalled" else status, stalled=status == "stalled",
             s=WED - timedelta(minutes=3))
    nxt, eff = _run(_row("degraded", pending_human_confirmation=1, ready_streak=2),
                    _f(keys=[key], healthy=False, contact_wait=True))
    assert nxt["state"] == "degraded" and nxt["ready_streak"] == 0
    assert nxt["pending_human_confirmation"] == 1 and not eff.opened


def test_two_problem_ticks_confirm_the_stop_and_notify_once():
    key = _k(status="failed", s=WED)
    row = _row("degraded")
    row, eff = _run(row, _f(keys=[key], healthy=False))
    assert row["confirmed"] == 0 and eff.problem_streak == 1
    row, eff = _run(row, _f(keys=[key], healthy=False, problem_streak=1))
    assert row["confirmed"] == 1 and eff.confirmed_stop and ("stopped", "") in eff.notices


# ---- A / N / O: 接触 ------------------------------------------------------

def test_late_bar_contact_in_ready_opens_an_epoch_and_stops_with_a_human_wait():
    nxt, eff = _run(_row(), _f(late_contacts=(_finding(7, "sl"),)))
    assert nxt["state"] == "degraded" and nxt["epoch"] == 6 and eff.opened
    assert nxt["pending_human_confirmation"] == 1 and nxt["confirmed"] == 1
    # 初回接触の停止通知を先に、健全なら続けて人の待ちを知らせる
    assert list(eff.notices) == [("stopped", "contact"), ("awaiting_resume", "")]
    assert [c.order_id for c in eff.new_contacts] == [7]
    assert [a[0] for a in eff.activities] == [
        "late_bar_protection_contact", "datafeed_recovered_awaiting_resume"]


def test_unknown_contact_is_reported_with_its_own_event_and_also_stops():
    nxt, eff = _run(_row(), _f(late_contacts=(_finding(7, "unknown"),)))
    assert nxt["state"] == "degraded"
    assert eff.activities[0][0] == "late_bar_contact_unknown"


def test_contact_notice_is_not_suppressed_by_an_existing_stop_marker_or_request_or_flat():
    row = _row("degraded", confirmed=1, recovered_notified_epoch=5,
               resume_requested_at=WED.isoformat())
    nxt, eff = _run(row, _f(late_contacts=(_finding(),), noticed=True))
    assert ("stopped", "contact") in eff.notices
    assert nxt["pending_human_confirmation"] == 1 and nxt["ready_streak"] == 0


def test_contact_in_a_restricted_epoch_moves_it_to_degraded_in_the_same_epoch():
    since = (WED - timedelta(minutes=5)).isoformat()
    nxt, eff = _run(_row("restricted", restricted_since=since),
                    _f(n_contacts=(_finding(),), healthy=True))
    assert nxt["state"] == "degraded" and nxt["epoch"] == 5 and not eff.opened


def test_new_bar_contacts_are_ignored_in_ready_unless_the_old_stall_was_overdue():
    nxt, _ = _run(_row(), _f(n_contacts=(_finding(),)))
    assert nxt["state"] == "ready"
    old = WED - timedelta(minutes=40)
    nxt, eff = _run(_row(), _f(n_contacts=(_finding(),), old_stall_starts=(old,)))
    assert nxt["state"] == "degraded" and eff.opened and eff.new_contacts


def test_contact_before_recovery_wins_over_the_restricted_auto_recovery():
    since = (WED - timedelta(minutes=2)).isoformat()
    nxt, _ = _run(_row("restricted", restricted_since=since), _f(n_contacts=(_finding(),)))
    assert nxt["state"] == "degraded"


def test_latest_bar_pending_limit_contact_stops_when_the_epoch_stays_non_ready():
    since = (WED - timedelta(minutes=2)).isoformat()
    keys = [_k(stalled=True, s=WED - timedelta(minutes=1))]
    nxt, eff = _run(_row("restricted", restricted_since=since),
                    _f(keys=keys, healthy=False, n_latest_contacts=(_finding(3, "limit"),)))
    assert nxt["state"] == "degraded" and [c.order_id for c in eff.new_contacts] == [3]


def test_latest_bar_pending_limit_contact_is_left_to_normal_fills_when_the_tick_recovers():
    since = (WED - timedelta(minutes=2)).isoformat()
    nxt, eff = _run(_row("restricted", restricted_since=since),
                    _f(n_latest_contacts=(_finding(3, "limit"),)))
    assert nxt["state"] == "ready" and eff.new_contacts == ()


def test_latest_bar_contact_is_left_alone_when_a_degraded_epoch_completes_its_recovery():
    nxt, eff = _run(_row("degraded", ready_streak=2),
                    _f(n_latest_contacts=(_finding(3, "limit"),)))
    assert nxt["state"] == "ready" and eff.new_contacts == ()


def test_latest_bar_contact_is_not_checked_in_a_ready_tick():
    nxt, _ = _run(_row(), _f(n_latest_contacts=(_finding(3, "limit"),)))
    assert nxt["state"] == "ready"


def test_other_contacts_win_over_the_recovering_tick_and_the_latest_bar_contact_is_added():
    nxt, eff = _run(_row("degraded", ready_streak=2),
                    _f(n_contacts=(_finding(4),), n_latest_contacts=(_finding(3, "limit"),)))
    assert nxt["state"] == "degraded" and nxt["ready_streak"] == 0
    assert [c.order_id for c in eff.new_contacts] == [4, 3]


# ---- B: 保存・必須状態の読取り障害 ----------------------------------------

def test_unreadable_state_in_ready_closes_the_gate_and_never_releases():
    nxt, eff = _run(_row(), _f(read_error=True, flat=False))
    assert nxt["state"] == "degraded" and eff.opened and eff.blocked
    assert nxt["pending_human_confirmation"] == 1


def test_unreadable_state_keeps_a_degraded_epoch_even_after_enough_healthy_ticks():
    nxt, eff = _run(_row("degraded", ready_streak=2), _f(read_error=True))
    assert nxt["state"] == "degraded" and nxt["ready_streak"] == 0 and eff.blocked
    assert nxt["pending_human_confirmation"] == 0


def test_unreadable_state_keeps_an_existing_pending_when_flat():
    nxt, _ = _run(_row("degraded", pending_human_confirmation=1), _f(read_error=True))
    assert nxt["pending_human_confirmation"] == 1


# ---- 観測の途切れ (旧停滞の期限超過) -------------------------------------

@pytest.mark.parametrize("flat,age,want", [
    (False, 600, "ready"), (False, 601, "degraded"),
    (True, 1800, "ready"), (True, 1801, "degraded")])
def test_an_old_stall_past_its_limit_is_failed_even_when_the_tick_arrives_healthy(
        flat, age, want):
    nxt, eff = _run(_row(), _f(flat=flat, old_stall_starts=(WED - timedelta(seconds=age),)))
    assert nxt["state"] == want
    assert eff.opened is (want == "degraded")


def test_an_old_stall_past_its_limit_does_not_let_a_restricted_epoch_recover():
    since = (WED - timedelta(minutes=20)).isoformat()
    nxt, _ = _run(_row("restricted", restricted_since=since),
                  _f(flat=False, old_stall_starts=(WED - timedelta(minutes=15),)))
    assert nxt["state"] == "degraded"


# ---- 問題の連続カウント ----------------------------------------------------

def test_problem_streak_counts_restricted_ticks_and_resets_on_a_clean_tick():
    key = _k(stalled=True, s=WED - timedelta(minutes=1))
    row = _row("restricted", restricted_since=(WED - timedelta(minutes=1)).isoformat())
    _, eff = _run(row, _f(keys=[key], healthy=False, problem_streak=3))
    assert eff.problem_streak == 4
    _, eff = _run(row, _f(problem_streak=3))
    assert eff.problem_streak == 0
