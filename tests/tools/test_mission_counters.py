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


# --- ローカル 1 周目 pin (2026-09-07、tmp/review-20260907-st/verified-round1-local.md) ---

# 追加 1: 挿入先: モジュール末尾 (新規関数 `test_reserve_self_test_rejection_does_not_consume_budget`)。
def test_reserve_self_test_rejection_does_not_consume_budget():
    """ローカル 1 周目 (ornith c1/c4, qwen c4): `run_backtest_required_first`
    で拒否した経路がカウンタを進めない契約の pin。拒否側の分岐で
    `self_test_runs` / `_self_tests_by_name` を加算する変異が緑で生存していた
    (max_self_test_runs 側の拒否は test_run_plugin_tests_rejects_exactly_at_
    max_self_test_runs が観測しているが、順序制約側は誰も見ていなかった)。"""
    counters = MissionToolCounters()
    kwargs = dict(max_runs=12, max_before_backtest=3, is_strategy=True)
    assert counters.reserve_self_test("a", **kwargs) is None
    before_runs = counters.self_test_runs
    before_pre = counters.self_tests_before_backtest
    for _ in range(3):
        assert counters.reserve_self_test("a", **kwargs) == "run_backtest_required_first"
        assert counters.self_test_runs == before_runs
        assert counters.self_tests_before_backtest == before_pre
    # 拒否が予算を食っていないので、backtest 成功後は残枠がそのまま使える
    counters.record_backtest_result("a", ok=True)
    assert counters.reserve_self_test("a", **kwargs) is None
    assert counters.self_test_runs == before_runs + 1
