"""1 分足の穴・停滞を実時刻の時系列で再生し、restricted と停止 (degraded) の境界を確かめる。

足は確定直後 (足の開始 + 1 分 30 秒) の tick で cache に入る (通常取得が確定直後に追随する想定)。
`holes` に入れた足は届かない。期待値は設計の表 (全期間の穴の SQL から得た区間) と、開場秒の
手計算から書いた値で、実装の出力を写していない。tick は毎分 0 秒。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core import market_hours
from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.outage import IngestTickReport, OutageStateMachine
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.ohlcv import upsert_cache_bars

UTC = timezone.utc
PAIR = "USDJPY"
KEY = (PAIR, "1m")
SRC = "mt5-live"


def _ok():
    return IngestTickReport(attempted=frozenset({KEY}), succeeded=frozenset({KEY}),
                            failed=frozenset(), deferred=frozenset(), empty=frozenset())


def _rig(tmp_path, *, exposure=False, **kw):
    conn = connect(tmp_path / "holes.db")
    init_db(conn)
    machine = OutageStateMachine(
        conn, hard_keys=frozenset({KEY}), interval_widths={"1m": timedelta(minutes=1)},
        grace=timedelta(seconds=30), storage_source=SRC, **kw)
    if exposure:
        # 損切りが足の価格 (100 付近) から遠い建玉。停止中の足は接触と判定されない
        orders.insert(conn, pair=PAIR, direction="long", entry_type="market", horizon="swing",
                      status="open", now=datetime(2026, 1, 1, tzinfo=UTC),
                      filled_at=datetime(2026, 1, 1, tzinfo=UTC).isoformat(), stop_loss=50.0)
        conn.commit()
    return conn, machine


def _replay(conn, machine, start, end, holes):
    """[start, end] を毎分観測し、{時刻: state} を返す。"""
    states = {}
    now = start
    while now <= end:
        bar = (now - timedelta(seconds=90)).replace(second=0, microsecond=0)
        if bar not in holes and market_hours.is_market_open(bar):
            upsert_cache_bars(conn, [Bar(PAIR, "1m", bar, 100, 100.5, 99.5, 100, 1)], source=SRC)
            conn.commit()
        states[now] = machine.observe(now, _ok())
        now += timedelta(minutes=1)
    return states


def _runs(states, value):
    """state が連続する tick 数の列。"""
    runs, count = [], 0
    for state in states.values():
        if state == value:
            count += 1
        elif count:
            runs.append(count)
            count = 0
    if count:
        runs.append(count)
    return runs


def _times(day, hour, minutes):
    return {datetime(2026, *day, hour, minute, tzinfo=UTC) for minute in minutes}


# 全期間の穴の表 (欠け本数の列。S→R は欠け本数と同じ分数)
NIGHT_1025 = (_times((10, 25), 21, [2, 4, 6, 9, 13, 14, 15, 16, 17, *range(20, 29), 35, 37, 39, 40, 43, 54]),
              datetime(2026, 10, 25, 21, 1, tzinfo=UTC), datetime(2026, 10, 25, 22, 0, tzinfo=UTC),
              [1, 1, 1, 1, 5, 9, 1, 1, 2, 1, 1])
# 冬時間の日曜の開場直後 (22:00Z 開場)
NIGHT_1101 = (_times((11, 1), 22, [7, 12, 34, 37, 38, 39, 40, 53, 57, 58]),
              datetime(2026, 11, 1, 22, 1, tzinfo=UTC), datetime(2026, 11, 1, 23, 5, tzinfo=UTC),
              [1, 1, 1, 4, 1, 2])


@pytest.mark.parametrize("exposure", [False, True], ids=["flat", "exposure"])
@pytest.mark.parametrize("night", [NIGHT_1025, NIGHT_1101], ids=["10-25", "11-01"])
def test_every_hole_of_a_real_night_is_a_short_restricted_that_recovers_and_never_stops(
        tmp_path, night, exposure):
    holes, start, end, lengths = night
    conn, machine = _rig(tmp_path, exposure=exposure)
    states = _replay(conn, machine, start, end, holes)

    assert "degraded" not in states.values()
    assert _runs(states, "restricted") == lengths
    assert states[end] == "ready"
    # 穴ごとに epoch が 1 つずつ。停止通知・停止 epoch の記録は作らない
    assert conn.execute("SELECT epoch FROM datafeed_outage_state").fetchone()[0] == len(lengths)
    assert conn.execute("SELECT count(*) FROM datafeed_outage_notices").fetchone()[0] == 0


def _hole(start_minute, count):
    return {datetime(2026, 9, 30, 10, start_minute + i, tzinfo=UTC) for i in range(count)}


@pytest.mark.parametrize("count", [5, 6, 9, 10])
def test_a_hole_of_up_to_ten_bars_never_stops_even_with_a_position(tmp_path, count):
    conn, machine = _rig(tmp_path, exposure=True)
    states = _replay(conn, machine, datetime(2026, 9, 30, 10, 1, tzinfo=UTC),
                     datetime(2026, 9, 30, 10, 40, tzinfo=UTC), _hole(10, count))
    assert "degraded" not in states.values()
    assert _runs(states, "restricted") == [count]
    assert states[datetime(2026, 9, 30, 10, 40, tzinfo=UTC)] == "ready"


def test_a_hole_of_eleven_bars_stops_a_position_at_the_first_tick_past_ten_open_minutes(
        tmp_path):
    conn, machine = _rig(tmp_path, exposure=True)
    states = _replay(conn, machine, datetime(2026, 9, 30, 10, 1, tzinfo=UTC),
                     datetime(2026, 9, 30, 10, 40, tzinfo=UTC), _hole(10, 11))
    # 欠けは 10:10-10:20、水位 10:09 の次の確認時刻 S=10:11:30。10:21:00 が 570 秒、10:22 は
    # 足 10:21 が届く前の tick で S から 630 秒 > 600
    assert states[datetime(2026, 9, 30, 10, 21, tzinfo=UTC)] == "restricted"
    assert states[datetime(2026, 9, 30, 10, 22, tzinfo=UTC)] == "degraded"


@pytest.mark.parametrize("count", [11, 29])
def test_a_flat_account_rides_out_the_same_holes_in_restricted(tmp_path, count):
    conn, machine = _rig(tmp_path)
    states = _replay(conn, machine, datetime(2026, 9, 30, 10, 1, tzinfo=UTC),
                     datetime(2026, 9, 30, 11, 20, tzinfo=UTC), _hole(10, count))
    assert "degraded" not in states.values()
    assert _runs(states, "restricted") == [count]


def test_a_flat_account_stops_when_the_hole_passes_the_configured_limit(tmp_path):
    conn, machine = _rig(tmp_path)
    states = _replay(conn, machine, datetime(2026, 9, 30, 10, 1, tzinfo=UTC),
                     datetime(2026, 9, 30, 11, 20, tzinfo=UTC), _hole(10, 31))
    # S=10:11:30 から 1800 秒は 10:41:30。10:41:00 までは restricted、10:42 の tick は 1830 秒で停止
    assert states[datetime(2026, 9, 30, 10, 41, tzinfo=UTC)] == "restricted"
    assert states[datetime(2026, 9, 30, 10, 42, tzinfo=UTC)] == "degraded"


def test_the_flat_grace_of_zero_stops_at_the_first_stalled_tick(tmp_path):
    conn, machine = _rig(tmp_path, flat_stall_max_sec=0)
    states = _replay(conn, machine, datetime(2026, 9, 30, 10, 1, tzinfo=UTC),
                     datetime(2026, 9, 30, 10, 20, tzinfo=UTC), _hole(10, 1))
    assert states[datetime(2026, 9, 30, 10, 12, tzinfo=UTC)] == "degraded"


def test_the_same_holes_for_two_pairs_are_judged_together(tmp_path):
    conn = connect(tmp_path / "two.db")
    init_db(conn)
    keys = frozenset({KEY, ("EURUSD", "1m")})
    machine = OutageStateMachine(conn, hard_keys=keys, interval_widths={"1m": timedelta(minutes=1)},
                                 grace=timedelta(seconds=30), storage_source=SRC)
    holes = {"USDJPY": _hole(10, 2), "EURUSD": _hole(10, 1)}
    report = IngestTickReport(attempted=keys, succeeded=keys, failed=frozenset(),
                              deferred=frozenset(), empty=frozenset())
    now, states = datetime(2026, 9, 30, 10, 1, tzinfo=UTC), {}
    while now <= datetime(2026, 9, 30, 10, 20, tzinfo=UTC):
        bar = (now - timedelta(seconds=90)).replace(second=0, microsecond=0)
        for pair, missing in holes.items():
            if bar not in missing:
                upsert_cache_bars(conn, [Bar(pair, "1m", bar, 100, 100.5, 99.5, 100, 1)],
                                  source=SRC)
        conn.commit()
        states[now] = machine.observe(now, report)
        now += timedelta(minutes=1)
    # 長い方の欠け (2 本) に合わせて restricted が続き、短い方で切れない
    assert _runs(states, "restricted") == [2]
    assert "degraded" not in states.values()


# ---- 失敗・空・水位不変の成功 (600 秒の前後) ---------------------------------------

def _failed():
    return IngestTickReport(attempted=frozenset({KEY}), succeeded=frozenset(),
                            failed=frozenset({(KEY, "ConnectError")}), deferred=frozenset(),
                            empty=frozenset())


def _empty():
    return IngestTickReport(attempted=frozenset({KEY}), succeeded=frozenset({KEY}),
                            failed=frozenset(), deferred=frozenset(), empty=frozenset({KEY}))


def _bar(conn, at):
    upsert_cache_bars(conn, [Bar(PAIR, "1m", at, 100, 100.5, 99.5, 100, 1)], source=SRC)
    conn.commit()


@pytest.mark.parametrize("make", [_failed, _empty], ids=["failed", "empty"])
@pytest.mark.parametrize("later,want", [
    (datetime(2026, 9, 30, 10, 14, 59, tzinfo=UTC), "restricted"),     # 599 秒
    (datetime(2026, 9, 30, 10, 15, 0, tzinfo=UTC), "restricted"),      # ちょうど 600 秒
    (datetime(2026, 9, 30, 10, 15, 1, tzinfo=UTC), "degraded"),        # 601 秒
])
def test_a_failure_or_an_empty_answer_is_judged_from_the_same_start_before_at_and_after_600(
        tmp_path, make, later, want):
    conn, machine = _rig(tmp_path, exposure=True)
    _bar(conn, datetime(2026, 9, 30, 10, 3, tzinfo=UTC))
    start = datetime(2026, 9, 30, 10, 5, 0, tzinfo=UTC)
    assert machine.observe(start, make()) == "restricted"          # 起点 S = 観測時刻 10:05:00
    assert machine.observe(later, make()) == want


def test_a_successful_answer_without_a_newer_bar_does_not_recover_and_a_new_bar_ends_the_timer(
        tmp_path):
    conn, machine = _rig(tmp_path, exposure=True)
    _bar(conn, datetime(2026, 9, 30, 10, 3, tzinfo=UTC))
    # 水位 10:03 の次の確認時刻 S=10:05:30。成功しても水位が動かない間は停滞のまま
    assert machine.observe(datetime(2026, 9, 30, 10, 6, tzinfo=UTC), _ok()) == "restricted"
    assert machine.observe(datetime(2026, 9, 30, 10, 8, tzinfo=UTC), _ok()) == "restricted"
    _bar(conn, datetime(2026, 9, 30, 10, 8, tzinfo=UTC))
    assert machine.observe(datetime(2026, 9, 30, 10, 10, tzinfo=UTC), _ok()) == "ready"
    # 回復足で旧計時を切り、次の停滞は新しい起点 S=10:10:30 から数える。旧 S=10:05:30 からなら
    # 10:16 は 630 秒で停止になるが、新しい起点からは 330 秒
    assert machine.observe(datetime(2026, 9, 30, 10, 14, tzinfo=UTC), _ok()) == "restricted"
    assert machine.observe(datetime(2026, 9, 30, 10, 16, tzinfo=UTC), _ok()) == "restricted"
    assert machine.observe(datetime(2026, 9, 30, 10, 20, 30, tzinfo=UTC), _ok()) == "restricted"
    assert machine.observe(datetime(2026, 9, 30, 10, 21, tzinfo=UTC), _ok()) == "degraded"


# ---- 穴の無い約 7 分の停滞 (実際の epoch の形) ---------------------------------------

@pytest.mark.parametrize("exposure", [False, True], ids=["flat", "exposure"])
def test_a_seven_minute_stall_without_a_hole_is_inside_the_grace_for_both_exposures(
        tmp_path, exposure):
    conn, machine = _rig(tmp_path, exposure=exposure)
    _bar(conn, datetime(2026, 11, 13, 12, 31, tzinfo=UTC))
    # 11-13 の epoch 57 の形: 12:31 の足の後 12:38 まで足が届かない (約 7 分の停滞)
    states = {}
    now = datetime(2026, 11, 13, 12, 32, tzinfo=UTC)
    while now <= datetime(2026, 11, 13, 12, 39, tzinfo=UTC):
        states[now] = machine.observe(now, _ok())
        now += timedelta(minutes=1)
    _bar(conn, datetime(2026, 11, 13, 12, 38, tzinfo=UTC))
    states[datetime(2026, 11, 13, 12, 40, tzinfo=UTC)] = machine.observe(
        datetime(2026, 11, 13, 12, 40, tzinfo=UTC), _ok())
    assert "degraded" not in states.values()
    assert states[datetime(2026, 11, 13, 12, 34, tzinfo=UTC)] == "restricted"
    assert states[datetime(2026, 11, 13, 12, 40, tzinfo=UTC)] == "ready"


def _idle():
    return IngestTickReport(attempted=frozenset(), succeeded=frozenset(), failed=frozenset(),
                            deferred=frozenset(), empty=frozenset())


def test_an_unattempted_tick_at_the_recovery_boundary_does_not_recover(tmp_path):
    conn, machine = _rig(tmp_path, exposure=True)
    _bar(conn, datetime(2026, 11, 13, 12, 31, tzinfo=UTC))
    assert machine.observe(datetime(2026, 11, 13, 12, 36, tzinfo=UTC), _ok()) == "restricted"
    _bar(conn, datetime(2026, 11, 13, 12, 38, tzinfo=UTC))
    # 足は届いたが、この tick は 1m を取りに行っていない (backoff 等): 回復と判断しない
    assert machine.observe(datetime(2026, 11, 13, 12, 40, tzinfo=UTC), _idle()) == "restricted"
    _bar(conn, datetime(2026, 11, 13, 12, 39, tzinfo=UTC))
    assert machine.observe(datetime(2026, 11, 13, 12, 41, tzinfo=UTC), _ok()) == "ready"


def test_an_unattempted_tick_past_the_stall_limit_stops(tmp_path):
    conn, machine = _rig(tmp_path, exposure=True)
    _bar(conn, datetime(2026, 11, 13, 12, 31, tzinfo=UTC))
    assert machine.observe(datetime(2026, 11, 13, 12, 36, tzinfo=UTC), _ok()) == "restricted"
    # 起点 S=12:33:30 から 12:44:00 は 630 秒 > 600 秒
    assert machine.observe(datetime(2026, 11, 13, 12, 44, tzinfo=UTC), _idle()) == "degraded"
