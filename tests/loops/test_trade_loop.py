"""取引判断 loop テスト — fail closed・全記録・ask 回答専用・二層境界・trigger 記録。"""
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

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
from agentic_fx.store import signals
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
        clock=clock, core_lock=threading.RLock(), conn_supervisor=conn,
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


def test_run_once_mission_tools_include_get_signals(tmp_path):
    """⑧プラン 7 Task 9: _TRADE_TOOLS への get_signals 追加が実際に
    Mission.tools まで届くことのピン (registry 登録だけでは Mission から
    使えない — codex R3 I3)。"""
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "w"}, [])])
    loop.run_once()
    assert "get_signals" in runner.missions[0].tools


def test_ask_once_mission_tools_include_get_signals(tmp_path):
    """⑧ask Mission にも get_signals が露出する (brief どおり意図的)。"""
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"answer": "test"}, [])])
    loop.ask_once("今どう見てる？")
    assert "get_signals" in runner.missions[0].tools


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
    """executor.record_and_validate_intent が例外 (プラン8 五相再構成:
    commit-core の dispatch 入口) → intent_execution_failed で記録・None。"""
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "test"}, [])])
    loop.executor.record_and_validate_intent = MagicMock(
        side_effect=RuntimeError("executor_boom"))
    result = loop.run_once()
    assert result is None
    act_text = (tp / "a.log").read_text(encoding="utf-8")
    assert "intent_execution_failed" in act_text


def test_run_once_boundary_exception_from_activity_write(tmp_path):
    """activity.write が例外 → catch・None・notifier も試行・いずれも漏らない。"""
    conn, loop, _, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "test"}, [])])
    loop.activity.write = MagicMock(side_effect=RuntimeError("activity_boom"))
    result = loop.run_once()
    assert result is None


def test_ask_once_boundary_exception_returns_error_string(tmp_path):
    """ask_once の境界例外 (missions.start) → 'internal_error' 返却 (正確一致)。"""
    conn, loop, _, _ = _loop(tmp_path, [])
    with patch("agentic_fx.loops.trade_loop.missions.start") as mock_start:
        mock_start.side_effect = RuntimeError("missions_boom")
        result = loop.ask_once("test?")
        # 公開境界を通る例外は内部エラーとして返される
        assert result == "(Mission 失敗: internal_error)"


def test_ask_once_invalid_output_dict_missing_answer(tmp_path):
    """ask output が dict だが answer キーなし → 'completed' error 返却 (正確一致)。"""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"wrong_key": "value"}, [])])
    result = loop.ask_once("test?")
    assert result == "(Mission 失敗: completed)"


def test_ask_once_invalid_output_not_dict(tmp_path):
    """ask output が文字列 (dict ではない) → 'completed' error 返却 (正確一致)。"""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", "not a dict", [])])
    result = loop.ask_once("test?")
    assert result == "(Mission 失敗: completed)"


def test_ask_once_invalid_output_answer_not_string(tmp_path):
    """ask output.answer が数値 (str ではない) → 'completed' error 返却 (正確一致)。"""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"answer": 123}, [])])
    result = loop.ask_once("test?")
    assert result == "(Mission 失敗: completed)"


def test_missions_finish_failure_logged_not_blocking(tmp_path):
    """missions.finish が例外 → mission_finalize_failed を記録するが、hold の
    結果はそのまま返る (巻き戻さない)。

    設計上の注記 (プラン8 Task 15): trade 経路の `missions.finish(CAS)` は
    commit-core の末尾 (paper broker 執行の後) に置く設計であり、finalize
    失敗は「無警告のまま実行し続ける」ことを防ぐために可視化されるだけで、
    intent の記録・実行自体 (`orders.insert`/`trade_intents` への書込み) は
    finalize と独立している。旧 W1 の「completed 相当の結果を巻き戻す」は
    trade 経路では意図的に撤回された (旧テストの意図とは逆転する変更)。
    """
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "test"}, [])])
    with patch("agentic_fx.loops.trade_loop.missions.finish") as mock_finish:
        mock_finish.side_effect = RuntimeError("finish_boom")
        result = loop.run_once()
        # hold の結果は finalize 失敗で巻き戻されない (設計上の注記)
        assert result == {"result": "hold", "order_id": None, "reasons": []}
        act_text = (tp / "a.log").read_text(encoding="utf-8")
        assert "mission_finalize_failed" in act_text


def test_missions_finish_failure_does_not_block_order_execution(tmp_path):
    """設計上の注記 (プラン8 Task 15): finish 失敗時も open intent の発注自体は
    実行される — `orders.insert`/`trade_intents` は `missions.finish` と独立
    した書込みであり、finalize 失敗は監査証跡を失わせない
    (旧 W1 「no_order」の期待とは逆転する意図的な変更)。"""
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed",
        {"action": "open", "pair": "USDJPY", "direction": "long",
         "entry_type": "limit", "horizon": "day", "limit_price": 148.20,
         "expires_in": "4h", "stop_loss": 147.80, "take_profit": 149.00,
         "reasoning": "test"}, [])])
    with patch("agentic_fx.loops.trade_loop.missions.finish") as mock_finish:
        mock_finish.side_effect = RuntimeError("finish_boom")
        result = loop.run_once()
        assert result["result"] == "pending"
        assert conn.execute(
            "SELECT COUNT(*) c FROM orders").fetchone()["c"] == 1
        act_text = (tp / "a.log").read_text(encoding="utf-8")
        assert "mission_finalize_failed" in act_text


def test_ask_once_finish_failure_returns_answer_despite_finalize_failure(tmp_path):
    """(⚠ 着手前検証の結果 (4)) ask 経路: `_finalize_mission` は
    `result.status != "completed"` の判定より前に呼ばれるため、finish が
    失敗しても回答文字列がそのまま返る (従来は失敗文字列だった — ask は
    読み取り専用で資金に影響しないため許容する。この判断は実装者が独自に
    変えないこと、と本 task のプランに明記されている)。"""
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"answer": "test answer"}, [])])
    with patch("agentic_fx.loops.trade_loop.missions.finish") as mock_finish:
        mock_finish.side_effect = RuntimeError("finish_boom")
        result = loop.ask_once("質問？")
        assert result == "test answer"
        act_text = (tp / "a.log").read_text(encoding="utf-8")
        assert "mission_finalize_failed" in act_text


def test_ask_once_mismatched_schema_in_output(tmp_path):
    """ask が TRADE_INTENT_SCHEMA を返す (誤配置) → output 検証で error string。"""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed",
        {"action": "hold", "reasoning": "misplaced trade intent"},
        [])])
    result = loop.ask_once("test?")
    assert result == "(Mission 失敗: completed)"


# ---- F1: handle_intent 例外の専用ハンドリング ----

def test_handle_intent_exception_recorded(tmp_path):
    """executor.record_and_validate_intent が例外 (プラン8 五相再構成:
    commit-core の dispatch 入口) → activity intent_execution_failed・
    mid 記録・None."""
    conn, loop, _, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "test"}, [])])
    loop.executor.record_and_validate_intent = MagicMock(
        side_effect=RuntimeError("executor_crash"))
    result = loop.run_once()
    assert result is None
    # activity に記録
    act_text = (tp / "a.log").read_text(encoding="utf-8")
    assert "intent_execution_failed" in act_text
    # mission は finished
    m = conn.execute("SELECT * FROM missions").fetchone()
    assert m["status"] == "completed"


# ---- F2: 欠落している境界例外注入テスト ----

def test_run_once_boundary_exception_from_build_state_summary(tmp_path):
    """build_state_summary が OSError → 公開境界で catch・None・mission_boundary_failed。"""
    conn, loop, _, tp = _loop(tmp_path, [])
    with patch("agentic_fx.loops.trade_loop.build_state_summary") as mock_summary:
        mock_summary.side_effect = RuntimeError("summary_boom")
        result = loop.run_once()
        assert result is None
        assert "mission_boundary_failed" in (tp / "a.log").read_text(encoding="utf-8")


def test_run_once_boundary_exception_from_missions_start(tmp_path):
    """missions.start が例外 → 公開境界で catch・None・mission_boundary_failed。"""
    conn, loop, _, tp = _loop(tmp_path, [])
    with patch("agentic_fx.loops.trade_loop.missions.start") as mock_start:
        mock_start.side_effect = RuntimeError("start_boom")
        result = loop.run_once()
        assert result is None
        assert "mission_boundary_failed" in (tp / "a.log").read_text(encoding="utf-8")


def test_run_once_boundary_exception_from_trade_intent_parsing_type_error(tmp_path):
    """TradeIntent.from_llm_dict が想定外 TypeError → 公開境界で catch・None。"""
    conn, loop, _, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "test"}, [])])
    with patch("agentic_fx.loops.trade_loop.TradeIntent.from_llm_dict") as mock_parse:
        mock_parse.side_effect = TypeError("unexpected_type_error")
        result = loop.run_once()
        assert result is None
        assert "mission_boundary_failed" in (tp / "a.log").read_text(encoding="utf-8")


def test_run_once_boundary_exception_from_notifier_in_fail_closed(tmp_path):
    """healthcheck 分岐の notifier.send が例外 → 例外漏れない・None。"""
    conn, loop, _, tp = _loop(tmp_path, [], healthy=False)
    loop.notifier.send = MagicMock(side_effect=RuntimeError("notifier_boom"))
    result = loop.run_once()
    assert result is None  # 例外は catch される


# ---- F4: missions.finish 失敗テストの強化 ----

def test_missions_finish_failure_activity_recorded_call_count(tmp_path):
    """missions.finish が例外 → activity mission_finalize_failed 記録・finish は 1 回。

    設計上の注記 (プラン8 Task 15): hold の結果は finalize 失敗で巻き戻さない
    (旧 W1 の「failed に降格」は trade 経路では意図的に撤回された)。
    """
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "test"}, [])])
    with patch("agentic_fx.loops.trade_loop.missions.finish") as mock_finish:
        mock_finish.side_effect = RuntimeError("finish_boom")
        result = loop.run_once()
        # hold の結果は finalize 失敗で巻き戻されない (設計上の注記)
        assert result == {"result": "hold", "order_id": None, "reasons": []}
        # finish は呼ばれたがちょうど 1 回
        assert mock_finish.call_count == 1
        # activity に記録の試み
        act_text = (tp / "a.log").read_text(encoding="utf-8")
        assert "mission_finalize_failed" in act_text


# ---- F5: origin 固定と executor 不呼び出し ----

def test_trade_intent_origin_is_scheduler(tmp_path):
    """trade 経路の TradeIntent.from_llm_dict は Origin.SCHEDULER で呼ばれる。"""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "test"}, [])])
    with patch("agentic_fx.loops.trade_loop.TradeIntent.from_llm_dict") as mock_parse:
        mock_parse.return_value = MagicMock(action=MagicMock(value="hold"))
        loop.executor.handle_intent = MagicMock()
        loop.run_once()
        # from_llm_dict が呼ばれたことを確認
        assert mock_parse.called
        # origin 引数が Origin.SCHEDULER であること (キーワード引数チェック)
        call_kwargs = mock_parse.call_args.kwargs
        from agentic_fx.core.contracts import Origin
        assert call_kwargs.get("origin") == Origin.SCHEDULER


def test_ask_once_executor_not_called(tmp_path):
    """ask 経路では executor.handle_intent が呼ばれない。"""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"answer": "test"}, [])])
    loop.executor.handle_intent = MagicMock()
    loop.ask_once("test?")
    # executor は呼ばれない
    loop.executor.handle_intent.assert_not_called()


# ---- F6: MissionWatch 統合テストの強化 ----

def test_mission_watch_begin_end_called_with_correct_args(tmp_path):
    """_run_recorded が watch.begin(mid, loop, timeout_sec)・end(mid) を呼ぶ。"""
    conn, loop, runner, _ = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "test"}, [])])
    mock_watch = MagicMock()
    loop.watch = mock_watch
    loop.run_once()
    # begin が呼ばれたことを確認・引数チェック (mid, "trade", timeout_sec)
    begin_calls = mock_watch.begin.call_args_list
    assert len(begin_calls) == 1
    mid, loop_name, timeout_sec = begin_calls[0][0]
    assert loop_name == "trade"
    assert timeout_sec == 300.0  # SETTINGS.llama_swap.timeout_sec
    # end が同じ mid で呼ばれたことを確認
    end_calls = mock_watch.end.call_args_list
    assert len(end_calls) == 1
    assert end_calls[0][0][0] == mid  # 同じ mid


def test_mission_watch_begin_end_order_on_exception(tmp_path):
    """runner 例外時も begin → end の順で呼ぶ (finally で end)。"""
    conn, loop, _, _ = _loop(tmp_path, [])

    class Boom:
        def run(self, mission):
            raise RuntimeError("crash")

    loop.runner = Boom()
    mock_watch = MagicMock()
    loop.watch = mock_watch
    loop.run_once()
    # begin が呼ばれたことを確認
    assert mock_watch.begin.called
    # end が呼ばれたことを確認
    assert mock_watch.end.called
    # begin が end より前に呼ばれたことを確認 (call_args_list)
    all_calls = mock_watch.method_calls
    begin_idx = next(i for i, call in enumerate(all_calls) if call[0] == "begin")
    end_idx = next(i for i, call in enumerate(all_calls) if call[0] == "end")
    assert begin_idx < end_idx


def test_ask_once_watch_loop_name(tmp_path):
    """ask 経路の watch.begin は loop="ask" で呼ぶ。"""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"answer": "test"}, [])])
    mock_watch = MagicMock()
    loop.watch = mock_watch
    loop.ask_once("test?")
    # begin が呼ばれたことを確認・loop 引数が "ask"
    begin_calls = mock_watch.begin.call_args_list
    assert len(begin_calls) == 1
    _, loop_name, _ = begin_calls[0][0]
    assert loop_name == "ask"


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


def test_requeue_signal_happens_under_core_lock(tmp_path):
    """裁定書 F-6 (CR-5) / レビュー反映 2 回目 R2-CX-02: finally 節の
    `_requeue_signal` は core_lock 保持中に呼ばれる — 呼び出し中は他
    スレッドから core_lock を取得できないこと、かつ呼び出しがちょうど
    1 回であることを実際に検証する (骨格・恒真テストではない)。"""
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "timeout", None, [])])  # runner 失敗 → requeue 経路 (consume 前)
    sid = signals.add(
        conn, plugin="sig1", content_hash="h1", pair="USDJPY",
        timeframe="1h", bar_ts=NOW.isoformat(), kind="signal",
        payload={"direction": "long", "strength": 0.7, "rationale": "up"},
        now=NOW)

    entered = threading.Event()
    proceed = threading.Event()
    calls: list[object] = []
    original_requeue_signal = loop._requeue_signal  # bound method (self 済み)

    def spy_requeue_signal(*args, **kwargs):
        calls.append(args)
        entered.set()
        # requeue 呼び出しの「最中」を維持したまま、別スレッドに
        # core_lock.acquire(blocking=False) を試させる猶予を作る。
        assert proceed.wait(5.0), "checker スレッドが確認を完了しなかった"
        return original_requeue_signal(*args, **kwargs)

    loop._requeue_signal = spy_requeue_signal

    t = threading.Thread(target=lambda: loop.run_once("signal"), daemon=True)
    t.start()
    assert entered.wait(5.0), "_requeue_signal が呼ばれなかった"

    # RLock は同一スレッドからの acquire(blocking=False) は常に成功して
    # しまう (再入可能) ため、必ず別スレッド (checker) から確認する。
    acquired: list[bool] = []
    checker = threading.Thread(
        target=lambda: acquired.append(
            loop._core_lock.acquire(blocking=False)))
    checker.start()
    checker.join(timeout=5.0)
    if acquired and acquired[0]:
        loop._core_lock.release()  # 誤って取れてしまった場合の後始末
    assert acquired == [False], (
        "_requeue_signal 実行中は他スレッドから core_lock を取得できない"
        "はず (finally 節が with self._core_lock: で包んでいることの検証)")

    proceed.set()
    t.join(timeout=5.0)
    assert not t.is_alive()

    assert len(calls) == 1  # requeue はちょうど 1 回だけ呼ばれる
    row = conn.execute(
        "SELECT status, requeue_count FROM signals WHERE id=?",
        (sid,)).fetchone()
    assert row["status"] == "pending"
    assert row["requeue_count"] == 1


def test_mission_failed_activity_includes_reason_when_present(tmp_path):
    """Task 4 / CP13: reason があれば mission_failed activity 本文に
    含まれる。"""
    conn, loop, _, tp = _loop(tmp_path, [MissionResult(
        "failed", None, [], reason="context exceeded: prompt 90010 tokens "
                                    "> n_ctx 65536 (model=m)")])
    assert loop.run_once() is None
    act = (tp / "a.log").read_text(encoding="utf-8")
    assert "context exceeded: prompt 90010 tokens > n_ctx 65536" in act


def test_mission_failed_notification_includes_reason_when_present(tmp_path):
    """Task 4 / CP14: reason があれば通知本文にも含まれる。"""
    conn, loop, _, tp = _loop(tmp_path, [MissionResult(
        "failed", None, [], reason="context exceeded: prompt 1 tokens "
                                    "> n_ctx 2 (model=m)")])
    loop.notifier.send = MagicMock()
    assert loop.run_once() is None
    sent = loop.notifier.send.call_args[0][0]
    assert "context exceeded: prompt 1 tokens > n_ctx 2" in sent


def test_mission_failed_activity_omits_separator_for_empty_reason(tmp_path):
    """Task 4 mutation pin: 空 reason は activity に区切りを残さない。"""
    conn, loop, _, tp = _loop(tmp_path, [MissionResult(
        "failed", None, [], reason="")])
    assert loop.run_once() is None
    act = (tp / "a.log").read_text(encoding="utf-8")
    assert " —" not in act


def test_mission_failed_notification_omits_separator_for_empty_reason(tmp_path):
    """Task 4 mutation pin: 空 reason は通知に区切りを残さない。"""
    conn, loop, _, tp = _loop(tmp_path, [MissionResult(
        "failed", None, [], reason="")])
    loop.notifier.send = MagicMock()
    assert loop.run_once() is None
    sent = loop.notifier.send.call_args[0][0]
    assert " —" not in sent


def test_failed_mission_output_never_reaches_the_intent_path(tmp_path):
    """Task 4 / 1 周目 codex 指摘 I1: `status != "completed"` の分岐の
    早期 `return None` を固定する。

    ⚠️ **既存の失敗系テストはこの防御を測れない。** すべて `output=None`
    なので、`return None` を削除しても直後の
    `TradeIntent.from_llm_dict(None)` が `IntentParseError` を出し、
    `intent_parse_failed` 経路が同じく `None` を返す — 別の出口が同じ
    見かけの結果を作るため、変異はフルスイート 1776 passed のまま生存
    する (指揮者が実測)。

    **失敗 Mission が構文的に妥当な output を伴った場合に露出する** —
    早期 return が無いと、失敗した LLM 出力がそのまま intent 化され
    執行経路へ進む。CLAUDE.md「発注・SL 変更・クローズ・資金保護は LLM に
    委ねない。決定論的コードで強制」に直接かかる防御であり、
    `status != "completed"` を**唯一の**判断根拠として止めきること自体が
    契約になる。"""
    # ⚠️ **output は「実際に注文が通る」形でなければ意味がない。** 最初に
    # 書いた版は `action="enter"` (存在しない action) を使っており、変異を
    # 入れても `IntentParseError` 側で止まっていた — 早期 return を消した
    # 危険 (執行経路への到達) を一度も踏まないまま KILLED になっていた
    # (指揮者が実測して差し替え)。
    conn, loop, _, tp = _loop(tmp_path, [MissionResult(
        "failed", {"action": "open", "pair": "USDJPY", "direction": "long",
                   "entry_type": "limit", "horizon": "day",
                   "limit_price": 148.20, "expires_in": "4h",
                   "stop_loss": 147.80, "take_profit": 149.00,
                   "reasoning": "壊れた runner の出力"},
        [], reason="context exceeded: prompt 90010 tokens > n_ctx 65536")])

    assert loop.run_once() is None

    # intent は 1 本も作られない (執行経路へ進んでいない)。
    assert conn.execute(
        "SELECT COUNT(*) c FROM trade_intents").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"] == 0

    act = (tp / "a.log").read_text(encoding="utf-8")
    assert "mission_failed" in act
    # 失敗分岐で止まっており、後続の parse 経路へ落ちていない。
    assert "intent_parse_failed" not in act


def test_mission_failed_activity_line_is_pinned_field_by_field(tmp_path):
    """Task 4 / 1 周目 codex 指摘 I3: `mission_failed` の activity 行を
    **フィールド単位の完全一致**で固定する。

    Task 4 のテストは reason の部分文字列しか見ていなかったため、
    ①既存の `runner status=...` を削除する ②category を変える
    ③`ref_id` を mission ID 以外にする ④区切りを `" — "` 以外にする、
    のいずれの変異も素通りしていた (codex 指摘)。activity 行は運用時に
    人が読む唯一の一次記録であり、書式そのものが契約になる。

    行の形は `ts \\t category \\t event \\t summary \\t ref_id`
    (`ActivityLog.write` — summary は空白畳み込み済み)。"""
    reason = "context exceeded: prompt 90010 tokens > n_ctx 65536 (model=m)"
    conn, loop, _, tp = _loop(tmp_path, [MissionResult(
        "failed", None, [], reason=reason)])
    assert loop.run_once() is None

    mid = conn.execute("SELECT id FROM missions").fetchone()["id"]
    lines = [ln for ln in (tp / "a.log").read_text(encoding="utf-8").splitlines()
             if "\tmission_failed\t" in ln]
    assert len(lines) == 1, lines
    _ts, category, event, summary, ref_id = lines[0].split("\t")
    assert category == "AGGREGATE"
    assert event == "mission_failed"
    assert summary == f"runner status=failed — {reason}"
    assert ref_id == str(mid)


def test_mission_failed_notification_body_is_pinned_exactly(tmp_path):
    """Task 4 / 1 周目 codex 指摘 I3: 通知本文を全文一致で固定する
    (既存プレフィックス・status・区切りのいずれを消しても red になる)。"""
    reason = "context exceeded: prompt 1 tokens > n_ctx 2 (model=m)"
    conn, loop, _, tp = _loop(tmp_path, [MissionResult(
        "failed", None, [], reason=reason)])
    loop.notifier.send = MagicMock()
    assert loop.run_once() is None
    assert loop.notifier.send.call_args[0][0] == (
        f"[agentic-fx] 判断 Mission 失敗: failed — {reason}")


# ---- 2 周目 (ローカル qwen 指摘・指揮者が実測で確定) ---------------------
# `MissionResult` 契約の強制点である `if not isinstance(result, MissionResult)`
# は 3 箇所にある。**reflection_cycle だけが pin されており
# (test_runner_non_mission_result_normalized_to_failed)、trade_loop の 2 箇所は
# 裸だった** — どちらを消してもフルスイート 1806 passed のまま通る (実測)。
# 経路の非対称であり、pin が無いのは**実注文を作る側**という最悪の組合せ。
#
# **失われるのは資金保護ではなく診断の帰属**である (指揮者が probe で両状態を
# 突き合わせて確定。当初「周期が死ぬ」と書いたのは誤りだった):
#
#   | | ガードあり | ガードなし |
#   |---|---|---|
#   | run_once の戻り値 | None | None       ← 区別できない |
#   | missions 行 | failed | failed         ← 区別できない |
#   | orders | 0 | 0                        ← 区別できない |
#   | activity | AGGREGATE mission_failed (ref_id=<mid>) | SYSTEM mission_boundary_failed (ref_id 無し) |
#   | 通知 | 判断 Mission 失敗: failed | trade Mission が内部エラーで失敗しました |
#
# ガードが無いと AttributeError が `run_once` の never-raise サービス境界に
# 落ち、**どの Mission がなぜ失敗したのか追えない不透明な内部エラー**になる。
# したがって pin は activity の event 名と ref_id、および通知本文に置く
# (戻り値や missions 行を見る pin は**恒真**になる)。

class _NonMissionResultRunner:
    """runner 契約を破って dict を返す (壊れた runner の代理)。"""

    def __init__(self) -> None:
        self.missions: list = []

    def run(self, mission):
        self.missions.append(mission)
        return {"status": "completed",
                "output": {"action": "open", "pair": "USDJPY",
                           "side": "buy", "qty": 0.01}}


def test_run_once_non_mission_result_is_attributed_not_boundary_swallowed(
        tmp_path):
    """runner が MissionResult 以外を返したとき、**Mission の失敗として
    帰属される** (サービス境界の不透明な内部エラーに落ちない)。

    ガードを消すと activity は `mission_boundary_failed` (ref_id 無し) に、
    通知は「内部エラー」に変わる — どの Mission がなぜ失敗したのか追えない。
    """
    conn, loop, _, tp = _loop(tmp_path, [])
    loop.runner = _NonMissionResultRunner()
    loop.notifier.send = MagicMock()

    assert loop.run_once() is None

    mid = conn.execute(
        "SELECT id FROM missions ORDER BY id DESC LIMIT 1").fetchone()["id"]
    lines = [ln.split("\t")
             for ln in (tp / "a.log").read_text(encoding="utf-8").splitlines()]
    # Mission に紐づく失敗として記録されること (event 名と ref_id の両方)
    assert [(c, e, r) for _ts, c, e, _s, r in lines] == [
        ("AGGREGATE", "mission_failed", str(mid))]
    assert loop.notifier.send.call_args_list == [
        call("[agentic-fx] 判断 Mission 失敗: failed")]
    # 併せて: dict の中身は執行経路へ進まない
    assert conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"] == 0


def test_ask_once_non_mission_result_is_attributed_not_boundary_swallowed(
        tmp_path):
    """ask 経路 (`_ask_once_impl`) 側の同じガード。こちらも裸だった。

    ここは戻り値そのものが両者を分ける — ガードありなら実 status を載せた
    `(Mission 失敗: failed)`、無しならサービス境界の
    `(Mission 失敗: internal_error)` になる。
    """
    conn, loop, _, _ = _loop(tmp_path, [])
    loop.runner = _NonMissionResultRunner()

    ans = loop.ask_once("今どう見てる？")

    assert ans == "(Mission 失敗: failed)"       # internal_error ではない
    row = conn.execute(
        "SELECT status FROM missions ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "failed"
