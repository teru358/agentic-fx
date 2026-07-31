"""取引判断 loop テスト — fail closed・全記録・ask 回答専用・二層境界・trigger 記録。"""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import (
    ConversionRate, FixedClock, InstrumentSpec, Quote,
)
from agentic_fx.core.executor import Executor
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.loops.mission_watch import MissionWatch
from agentic_fx.loops.trade_loop import TradeLoop
from agentic_fx.policy import Policy
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
SPEC = InstrumentSpec("USDJPY", 0.01, 0.01, 50.0, 0.01, 100_000, "USD", "JPY")
QUOTE = Quote("USDJPY", 148.49, 148.51, NOW, "test")


def _loop(tmp_path, results, healthy=True):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    record_snapshot(conn, now=NOW, balance=1_000_000, equity=1_000_000)
    clock = FixedClock(NOW)
    broker = PaperBroker(conn, SETTINGS, clock)
    def rate_fn(ccy: str, account_ccy: str, now) -> ConversionRate:
        return ConversionRate(value=1.0, from_ccy=ccy, to_ccy=account_ccy,
                              leg_ts=(now,))

    executor = Executor(
        conn=conn, broker=broker, settings=SETTINGS,
        state_store=StateStore(tmp_path / "s.json"),
        activity=ActivityLog(tmp_path / "a.log"),
        notifier=Notifier(enabled=False, webhook_url=None),
        clock=clock,
        quote_fn=lambda p: QUOTE, spec_fn=lambda p: SPEC,
        rate_fn=rate_fn)
    provider = MagicMock()
    if healthy:
        provider.healthcheck.return_value = "yfinance"
    else:
        provider.healthcheck.side_effect = DataUnhealthy("all down")
    econ = MagicMock()
    econ.upcoming.return_value = []
    runner = FakeRunner(results)
    policy_path = tmp_path / "directives.md"
    policy_path.write_text("USDJPY は月末は控えめに", encoding="utf-8")
    activity_log = ActivityLog(tmp_path / "a.log")
    watch = MissionWatch()
    loop = TradeLoop(
        conn=conn, runner=runner, settings=SETTINGS,
        executor=executor, provider=provider, econ=econ,
        policy=Policy(policy_path),
        activity=activity_log,
        notifier=Notifier(enabled=False, webhook_url=None),
        clock=clock,
        watch=watch)
    return conn, loop, runner, tmp_path


def test_hold_mission_recorded(tmp_path):
    """hold 判断が記録される: missions テーブル + trade_intents."""
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "様子見"}, [])])
    out = loop.run_once()
    assert out["result"] == "hold"
    m = conn.execute("SELECT * FROM missions").fetchone()
    assert m["loop"] == "trade" and m["status"] == "completed"
    assert conn.execute("SELECT COUNT(*) c FROM trade_intents").fetchone()["c"] == 1
    # prompt に policy とサマリが注入されている
    prompt = runner.missions[0].prompt
    assert "月末は控えめに" in prompt
    assert "現在の状態" in prompt


def test_fail_closed_on_unhealthy_data(tmp_path):
    """healthcheck が DataUnhealthy → Mission 実行なし・activity 記録・None 返却。"""
    conn, loop, runner, tp = _loop(tmp_path, [], healthy=False)
    assert loop.run_once() is None
    assert runner.missions == []  # Mission を実行していない
    act = (tp / "a.log").read_text(encoding="utf-8")
    assert "data_unhealthy" in act


def test_runner_failure_recorded(tmp_path):
    """runner が timeout → missions に記録・activity に mission_failed・None 返却。"""
    conn, loop, _, tp = _loop(tmp_path,
                              [MissionResult("timeout", None, [])])
    assert loop.run_once() is None
    m = conn.execute("SELECT * FROM missions").fetchone()
    assert m["status"] == "timeout"
    assert "mission_failed" in (tp / "a.log").read_text(encoding="utf-8")


def test_runner_exception_normalized_to_failed(tmp_path):
    """runner.run() が例外 → 正規化 + 必ず missions.finish → status=failed。"""
    conn, loop, _, _ = _loop(tmp_path, [])

    class Boom:
        def run(self, mission):
            raise RuntimeError("crash")

    loop.runner = Boom()
    assert loop.run_once() is None  # 例外が漏れない
    m = conn.execute("SELECT * FROM missions").fetchone()
    assert m["status"] == "failed"  # 必ず finish される


def test_unparsable_intent_recorded(tmp_path):
    """LLM 出力が TradeIntent に変換不可 → activity intent_parse_failed・None。"""
    conn, loop, _, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "buy!"}, [])])
    assert loop.run_once() is None
    assert "intent_parse_failed" in (tp / "a.log").read_text(encoding="utf-8")


def test_open_intent_executes(tmp_path):
    """open 判断が executor に渡される → result は executor の結果。"""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed",
        {"action": "open", "pair": "USDJPY", "direction": "long",
         "entry_type": "limit", "horizon": "day", "limit_price": 148.20,
         "expires_in": "4h", "stop_loss": 147.80, "take_profit": 149.00,
         "reasoning": "test"}, [])])
    out = loop.run_once()
    assert out["result"] == "pending"


def test_ask_is_answer_only(tmp_path):
    """ask_once: ANSWER_SCHEMA (answer 専用) で Mission 実行・loop=ask・intent なし。"""
    conn, loop, runner, _ = _loop(tmp_path, [MissionResult(
        "completed", {"answer": "現在は様子見が妥当です"}, [])])
    ans = loop.ask_once("今どう見てる？")
    assert "様子見" in ans
    # ask Mission のスキーマは answer 専用 (TradeIntent 不可)
    schema = runner.missions[0].output_schema
    assert "answer" in schema["properties"]
    assert "action" not in schema["properties"]
    m = conn.execute("SELECT * FROM missions").fetchone()
    assert m["loop"] == "ask"
    # intent は生成されない
    assert conn.execute("SELECT COUNT(*) c FROM trade_intents").fetchone()["c"] == 0


def test_mission_trigger_is_recorded_as_cron(tmp_path):
    """run_once() 既定引数 → missions.trigger == 'cron'."""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "test"}, [])])
    loop.run_once()  # trigger 未指定
    m = conn.execute("SELECT * FROM missions").fetchone()
    assert m["trigger"] == "cron"


def test_signal_trigger_is_recorded_verbatim(tmp_path):
    """run_once('signal:demo') → missions.trigger == 'signal:demo'."""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "test"}, [])])
    loop.run_once("signal:demo")
    m = conn.execute("SELECT * FROM missions").fetchone()
    assert m["trigger"] == "signal:demo"


def test_non_trade_missions_have_null_trigger(tmp_path):
    """ask_once 後 → missions.trigger IS NULL。"""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"answer": "test"}, [])])
    loop.ask_once("test?")
    m = conn.execute("SELECT * FROM missions WHERE loop='ask'").fetchone()
    assert m["trigger"] is None


# ---- Boundary tests (override 2) ----

def test_run_once_boundary_exception_from_healthcheck(tmp_path):
    """healthcheck の RuntimeError → 公開境界で catch・None 返却・exception 漏れない。"""
    conn, loop, _, tp = _loop(tmp_path, [])
    loop.provider.healthcheck.side_effect = RuntimeError("boom")
    result = loop.run_once()
    assert result is None
    assert "mission_boundary_failed" in (tp / "a.log").read_text(encoding="utf-8")


def test_run_once_boundary_exception_from_executor(tmp_path):
    """executor.handle_intent が例外 → 公開境界で catch・None・activity 記録。"""
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "test"}, [])])
    loop.executor.handle_intent = MagicMock(side_effect=RuntimeError("executor_boom"))
    result = loop.run_once()
    assert result is None
    assert "mission_boundary_failed" in (tp / "a.log").read_text(encoding="utf-8")


def test_run_once_boundary_exception_from_activity_write(tmp_path):
    """activity.write が例外 → catch・None・notifier も試行・いずれも漏らない。"""
    conn, loop, _, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "test"}, [])])
    loop.activity.write = MagicMock(side_effect=RuntimeError("activity_boom"))
    result = loop.run_once()
    assert result is None


def test_ask_once_boundary_exception_returns_error_string(tmp_path):
    """ask_once の例外 → error string 返却 (None ではなく文字列)。"""
    conn, loop, _, _ = _loop(tmp_path, [])
    loop.runner.run = MagicMock(side_effect=RuntimeError("runner_boom"))
    result = loop.ask_once("test?")
    assert isinstance(result, str)
    assert "失敗" in result or "error" in result.lower()


def test_ask_once_invalid_output_dict_missing_answer(tmp_path):
    """ask output が dict だが answer キーなし → error string 返却。"""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"wrong_key": "value"}, [])])
    result = loop.ask_once("test?")
    assert isinstance(result, str)
    assert "失敗" in result or "error" in result.lower()


def test_ask_once_invalid_output_not_dict(tmp_path):
    """ask output が文字列 (dict ではない) → error string 返却。"""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", "not a dict", [])])
    result = loop.ask_once("test?")
    assert isinstance(result, str)
    assert "失敗" in result or "error" in result.lower()


def test_ask_once_invalid_output_answer_not_string(tmp_path):
    """ask output.answer が数値 (str ではない) → error string 返却。"""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"answer": 123}, [])])
    result = loop.ask_once("test?")
    assert isinstance(result, str)
    assert "失敗" in result or "error" in result.lower()


def test_missions_finish_failure_logged_not_blocking(tmp_path):
    """missions.finish が例外 → 記録は試行・例外は catch・結果は返る。"""
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "test"}, [])])
    # missions.finish を mock で失敗させる
    with patch("agentic_fx.loops.trade_loop.missions.finish") as mock_finish:
        mock_finish.side_effect = RuntimeError("finish_boom")
        result = loop.run_once()
        # 例外は catch されるが結果は返される (hold が成功したため)
        assert result["result"] == "hold"
        # 失敗はログに記録されている
        # (activity.write も試行が行われる)


def test_ask_once_mismatched_schema_in_output(tmp_path):
    """ask が TRADE_INTENT_SCHEMA を返す (誤配置) → output 検証で error string。"""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed",
        {"action": "hold", "reasoning": "misplaced trade intent"},
        [])])
    result = loop.ask_once("test?")
    assert isinstance(result, str)
    assert "失敗" in result or "error" in result.lower()


def test_mission_watch_begin_end_called(tmp_path):
    """_run_recorded が watch.begin/end を呼ぶ。"""
    conn, loop, runner, _ = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "test"}, [])])
    mock_watch = MagicMock()
    loop.watch = mock_watch
    loop.run_once()
    # begin が呼ばれたことを確認
    assert mock_watch.begin.called
    # end が呼ばれたことを確認
    assert mock_watch.end.called


def test_mission_watch_end_called_even_on_exception(tmp_path):
    """runner 例外時も watch.end が呼ばれる (finally)。"""
    conn, loop, _, _ = _loop(tmp_path, [])

    class Boom:
        def run(self, mission):
            raise RuntimeError("crash")

    loop.runner = Boom()
    mock_watch = MagicMock()
    loop.watch = mock_watch
    loop.run_once()
    # end が呼ばれたことを確認
    assert mock_watch.end.called
