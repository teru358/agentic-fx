from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

from agentic_fx.tools.mission_counters import MissionToolCounters
from agentic_fx.config import ImproveToolBudgetSettings


def test_mission_tool_counters_track_calls_writes_and_backtests():
    counters = MissionToolCounters()
    counters.record_call()
    assert counters.reserve_write(limit=1) is True
    assert counters.reserve_backtest("candidate", limit=2) is True
    counters.record_backtest_result("candidate", ok=False)
    assert counters.reserve_backtest("candidate", limit=2) is True
    counters.record_backtest_result("candidate", ok=True)
    assert counters.total_calls == 1
    assert counters.writes == 1
    assert counters.backtest_calls["candidate"] == 2
    assert counters.successful_backtests["candidate"] == 1


def test_record_self_test_counts_consecutive_equal_signatures():
    counters = MissionToolCounters()
    for _ in range(3):
        assert counters.reserve_self_test(
            "a", max_runs=10, max_before_backtest=10, is_strategy=False) is None
    assert counters.record_self_test_result("a", ("x",)) == 1
    assert counters.record_self_test_result("a", ("x",)) == 2
    assert counters.record_self_test_result("a", ("y",)) == 1
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


def _budget(**changes):
    return ImproveToolBudgetSettings(max_refusal_streak=3, max_tool_calls=5,
                                     **changes)


def test_terminal_refusal_fires_only_after_response_delivery_hook():
    counters = MissionToolCounters(budget=_budget())
    counters.record_terminal_refusal()
    counters.record_terminal_refusal()
    assert not counters.abort_pending
    counters.record_terminal_refusal()
    assert counters.abort_pending
    assert counters.abort_trigger == "terminal_refusals"
    assert not counters.abort_event.is_set()
    counters.fire_if_pending()
    assert counters.abort_event.is_set()


def test_progress_resets_only_the_relevant_refusal_streaks():
    counters = MissionToolCounters(budget=_budget())
    counters.record_terminal_refusal()
    counters.record_recoverable_refusal("a", "run_backtest_required_first")
    counters.record_recoverable_refusal("b", "run_backtest_required_first")
    counters.record_progress("a", "self_test_ran")
    assert counters.terminal_refusal_streak == 0
    assert counters.recoverable_refusal_streak[("a", "run_backtest_required_first")] == 1
    counters.record_progress("a", "backtest_ok")
    assert counters.recoverable_refusal_streak[("a", "run_backtest_required_first")] == 0
    assert counters.recoverable_refusal_streak[("b", "run_backtest_required_first")] == 1


def test_max_calls_sets_pending_and_summary_is_single_line():
    counters = MissionToolCounters(budget=_budget())
    for _ in range(5):
        counters.record_call()
    assert counters.abort_pending
    assert counters.abort_trigger == "max_tool_calls"
    assert counters.summary() == "calls=5 refused=0 errors=0 self_test=0 backtest=0"


def test_run6_real_refusal_sequence_reaches_abort_threshold():
    rows = json.loads((Path(__file__).resolve().parents[1] / "fixtures" /
                       "mission_abort_run6_refusals.json").read_text())
    counters = MissionToolCounters(
        budget=ImproveToolBudgetSettings(max_refusal_streak=10))
    for row in rows:
        assert row["output"]["error"] == "budget exhausted"
        counters.record_terminal_refusal()
    assert counters.abort_pending
    assert counters.abort_trigger == "terminal_refusals"


# --- ローカル 1 周目 pin (2026-09-08、tmp/review-20260908-ma/verified-round1-local.md) ---

# P6 / P3b
def test_first_abort_trigger_is_not_overwritten_by_a_later_one():
    """ローカル 1 周目 #6 (muse c1 / qwen c1): `_mark_abort` の
    `if not self.abort_pending:` は「最初の到達だけが trigger を決める」契約。
    ガード削除の変異が全スイート green で生存していた — 上書きされると
    reason 文字列と Tier D' の申し送りが誤った死因を報告する。"""
    budget = ImproveToolBudgetSettings(max_refusal_streak=1, max_tool_calls=1)
    counters = MissionToolCounters(budget=budget)
    counters.record_terminal_refusal()
    assert counters.abort_trigger == "terminal_refusals"
    counters.record_call()
    assert counters.abort_trigger == "terminal_refusals"


def test_recoverable_refusal_streak_aborts_exactly_at_threshold():
    """ローカル 1 周目 #7 (muse c1): 3 本ある閾値のうち recoverable だけ
    境界が未検証だった (`>=` を `>` に緩める変異が生存)。手前で立たない /
    到達で立つ を対で踏み、trigger 名 `recoverable_refusals:<name>` も見る。"""
    budget = ImproveToolBudgetSettings(max_refusal_streak=3)
    counters = MissionToolCounters(budget=budget)
    for _ in range(2):
        counters.record_recoverable_refusal("cand", "run_backtest_required_first")
    assert not counters.abort_pending
    counters.record_recoverable_refusal("cand", "run_backtest_required_first")
    assert counters.abort_pending
    assert counters.abort_trigger == "recoverable_refusals:cand"


# --- ローカル 2 周目 pin (2026-09-08、tmp/review-20260908-ma-r2/verified-round2-local.md #23) ---
def test_max_tool_calls_aborts_exactly_at_threshold_not_before():
    """ローカル 2 周目 #23 (qwen c2): 閾値 3 本のうち `max_tool_calls` だけ
    「手前で立たない」側が未検証だった (terminal は
    `test_terminal_refusal_fires_only_after_response_delivery_hook`、
    recoverable は 1 周目 P6 が対で押さえている)。定数を 1 減らす変異
    (`>= max_tool_calls - 1`) が全スイート green で生存していた —
    1 手前で abort すると mission が予算を使い切る前に殺され、Tier D' の
    申し送りが実際には残っていた予算を「使い切った」と偽る。"""
    budget = ImproveToolBudgetSettings(max_tool_calls=3, max_refusal_streak=10)
    counters = MissionToolCounters(budget=budget)
    for _ in range(2):
        counters.record_call()
        assert not counters.abort_pending
        assert counters.abort_trigger is None
    counters.record_call()
    assert counters.abort_pending
    assert counters.abort_trigger == "max_tool_calls"



# --- run8 是正 [tool-exception-bypasses-refusal-streak] (2026-09-09) ---

def test_tool_error_streak_trips_abort_and_resets_only_on_same_tool_success():
    """run8 欠陥 B: 壊れた tool の連打 (例外応答 293 回) が refusal streak を素通りし
    max_tool_calls (300) まで止まらなかった。tool 名単位の streak で `max_refusal_streak`
    回目に pending (`tool_errors:<name>`)。同 tool の成功で 0、別 tool の成功では戻らない。"""
    from agentic_fx.config import ImproveToolBudgetSettings
    c = MissionToolCounters(budget=ImproveToolBudgetSettings(max_refusal_streak=3))
    c.record_tool_result("analyze_corr", False)
    c.record_tool_result("analyze_corr", False)
    c.record_tool_result("list_staging", True)       # 別 tool の成功は無関係
    assert c.abort_pending is False
    c.record_tool_result("analyze_corr", True)       # 同 tool の成功でリセット
    c.record_tool_result("analyze_corr", False)
    c.record_tool_result("analyze_corr", False)
    assert c.abort_pending is False
    c.record_tool_result("analyze_corr", False)
    assert c.abort_pending is True
    assert c.abort_trigger == "tool_errors:analyze_corr"
    assert c.errors == 5
    assert "errors=5" in c.summary()



def test_other_tool_success_between_failures_does_not_reset_tool_error_streak():
    """G6 pin: 壊れた tool の失敗の間に別 tool が成功しても streak は続く
    (run8 の実形: analyze_corr 失敗の合間に list_staging 等は成功していた)。"""
    from agentic_fx.config import ImproveToolBudgetSettings
    c = MissionToolCounters(budget=ImproveToolBudgetSettings(max_refusal_streak=3))
    c.record_tool_result("analyze_corr", False)
    c.record_tool_result("analyze_corr", False)
    c.record_tool_result("list_staging", True)
    c.record_tool_result("analyze_corr", False)
    assert c.abort_pending is True
    assert c.abort_trigger == "tool_errors:analyze_corr"



def test_different_business_errors_of_same_tool_do_not_merge_into_one_streak():
    """codex Important 1: run_backtest の no_history / loader_rejected / backtest_failed
    は別 streak。同一種別が閾値まで続いたときだけ abort。"""
    from agentic_fx.config import ImproveToolBudgetSettings
    c = MissionToolCounters(budget=ImproveToolBudgetSettings(max_refusal_streak=3))
    for err in ("no_history_for_symbol", "loader_rejected: x", "backtest_failed",
                "no_history_for_symbol", "loader_rejected: x"):
        c.record_tool_result("run_backtest", False, err)
    assert c.abort_pending is False
    c.record_tool_result("run_backtest", False, "no_history_for_symbol")
    assert c.abort_pending is True and c.abort_trigger == "tool_errors:run_backtest"


# --- ローカルレビュー pin Y1 (2026-09-09、tmp/review-20260909-r8/verified-local.md) ---
def test_error_messages_differing_only_past_60_chars_share_one_streak():
    """L6 派生 pin (ローカル 1 周目 2026-09-09): 失敗種別キーの 60 字切り詰めが未検証だった
    (`[:60]` を外す変異が全スイート緑で生存)。切り詰めが無いと、末尾だけ変わる長い error 文
    (`loader_rejected: invalid YAML (…, line N, column M)` — 先頭 60 字は同一、行番号だけ違う) が
    毎回別 streak になり、閾値に永久に届かない —
    run8 #65 (壊れた tool を 293 回) の再発。対の「切り詰めすぎて別種別を合算しない」側は
    test_different_business_errors_of_same_tool_do_not_merge_into_one_streak が押さえている。"""
    c = MissionToolCounters(budget=ImproveToolBudgetSettings(max_refusal_streak=3))
    base = "loader_rejected: " + "x" * 50          # 67 字 — 先頭 60 字は 3 回とも同一
    for tail in ("/tmp/run-a", "/tmp/run-b", "/tmp/run-c"):
        c.record_tool_result("run_backtest", False, base + tail)
    assert c.abort_pending is True
    assert c.abort_trigger == "tool_errors:run_backtest"


# --- [indicator-consumption-wiring] T5a Step 5-3: release_backtest (F4) ---

def test_release_backtest_decrements_and_never_goes_negative():
    c = MissionToolCounters(budget=_budget(max_backtests_per_candidate=2))
    assert c.reserve_backtest("cand", 2) is True
    assert c.backtest_calls["cand"] == 1
    c.release_backtest("cand")
    assert c.backtest_calls["cand"] == 0
    c.release_backtest("cand")
    assert c.backtest_calls["cand"] == 0      # 0 未満にしない
    c.release_backtest("never_reserved")
    assert c.backtest_calls["never_reserved"] == 0


def test_release_backtest_does_not_touch_successful_backtests():
    c = MissionToolCounters(budget=_budget(max_backtests_per_candidate=2))
    c.reserve_backtest("cand", 2)
    c.record_backtest_result("cand", ok=True)
    before = c.successful_backtests["cand"]
    c.release_backtest("cand")
    assert c.successful_backtests["cand"] == before
