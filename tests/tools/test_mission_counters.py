from concurrent.futures import ThreadPoolExecutor

from agentic_fx.tools.mission_counters import MissionToolCounters


def test_mission_tool_counters_track_calls_writes_and_backtests():
    counters = MissionToolCounters()
    counters.record_call()
    counters.record_write()
    counters.record_backtest("candidate", ok=False)
    counters.record_backtest("candidate", ok=True)
    assert counters.total_calls == 1
    assert counters.writes == 1
    assert counters.backtest_calls["candidate"] == 2
    assert counters.successful_backtests["candidate"] == 1


def test_record_self_test_counts_consecutive_equal_signatures():
    counters = MissionToolCounters()
    assert counters.record_self_test("a", ("x",)) == 1
    assert counters.record_self_test("a", ("x",)) == 2
    assert counters.record_self_test("a", ("y",)) == 1
    assert counters.self_test_runs == 3


def test_atomic_budget_reservations_do_not_exceed_limits():
    counters = MissionToolCounters()
    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = list(pool.map(lambda _: counters.reserve_write(30), range(100)))
    assert sum(admitted) == 30
    assert counters.writes == 30
