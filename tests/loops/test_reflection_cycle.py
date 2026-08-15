import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call
import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock, OrderStatus
from agentic_fx.loops.reflection_cycle import ReflectionCycle
from agentic_fx.loops.mission_watch import MissionWatch
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.store import missions, orders, reflections
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _cycle(tmp_path, results, watch=None):
    """Helper to create ReflectionCycle."""
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    rag = MagicMock()
    cyc = ReflectionCycle(
        conn=conn, runner=FakeRunner(results), rag=rag,
        settings=SETTINGS,
        activity=ActivityLog(tmp_path / "a.log"),
        clock=FixedClock(NOW), core_lock=threading.RLock(),
        watch=watch)
    return conn, rag, cyc


def _closed_order(conn):
    """Helper to create a closed order."""
    return orders.insert(
        conn, pair="USDJPY", direction="long",
        entry_type="market", horizon="day",
        status=OrderStatus.CLOSED, now=NOW, quantity=0.1,
        realized_pnl=-1500.0, close_reason="sl",
        avg_fill_price=148.5, close_price=148.0)


def test_creates_reflection_for_closed(tmp_path):
    """Closed order without reflection gets reflection created."""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "SL 幅が狭すぎた"}, [])])
    oid = _closed_order(conn)
    assert cyc.run_pending() == 1
    assert reflections.get(conn, oid)["content"] == "SL 幅が狭すぎた"
    rag.add_reflection.assert_called_once_with(oid, "SL 幅が狭すぎた", "USDJPY")


def test_skips_already_reflected(tmp_path):
    """Closed order with reflection is skipped."""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "x"}, [])])
    oid = _closed_order(conn)
    reflections.save(conn, oid, "既存", NOW)
    assert cyc.run_pending() == 0
    rag.add_reflection.assert_not_called()


def test_runner_failure_skips_for_retry(tmp_path):
    """Runner failure (timeout) leaves no SQLite row — retry next run."""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult("timeout", None, [])])
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    assert reflections.get(conn, oid) is None


def test_rag_failure_leaves_no_sqlite_row(tmp_path):
    """ChromaDB failure before SQLite save leaves no row — retry next run."""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "x"}, [])])
    rag.add_reflection.side_effect = RuntimeError("chroma down")
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    assert reflections.get(conn, oid) is None


def test_watch_begin_end_called(tmp_path):
    """MissionWatch.begin/end are called even on runner exception."""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult("failed", None, [])])
    watch = MagicMock(spec=MissionWatch)
    cyc.watch = watch
    oid = _closed_order(conn)
    cyc.run_pending()
    # Should have called begin with the mid, loop name, and timeout
    watch.begin.assert_called_once()
    watch.end.assert_called_once()
    # end should be called even if runner fails
    args, kwargs = watch.end.call_args
    assert args[0] == watch.begin.call_args[0][0]  # same mission_id


def test_runner_non_mission_result_normalized_to_failed(tmp_path):
    """Runner returning non-MissionResult is normalized to failed (F2)."""
    class NonMissionResultRunner:
        def run(self, mission):
            return {"status": "completed", "output": {"content": "x"}}  # dict, not MissionResult

    conn = connect(tmp_path / "t.db")
    init_db(conn)
    rag = MagicMock()
    cyc = ReflectionCycle(
        conn=conn, runner=NonMissionResultRunner(), rag=rag,
        settings=SETTINGS,
        activity=ActivityLog(tmp_path / "a.log"),
        clock=FixedClock(NOW), core_lock=threading.RLock())

    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    # Should be normalized to failed, no rag call, no sqlite row
    assert reflections.get(conn, oid) is None
    rag.add_reflection.assert_not_called()
    # Verify missions row was finalized as failed
    mid_row = conn.execute("SELECT status FROM missions ORDER BY id DESC LIMIT 1").fetchone()
    assert mid_row["status"] == "failed"


def test_missions_finish_failure_recorded_in_activity(tmp_path):
    """missions.finish failure writes activity SYSTEM mission_finalize_failed (F3).

    W1-b: finish 失敗時は監査未確定 (missions 行が running のまま) のため
    reflection も保存しない (以前は保存していた — 意図の変更・レビュー指摘
    W1-b)。SQLite マーカー (reflections 行) が無いので次周期の run_pending
    が同じ order を再試行対象のまま残す。
    """
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "x"}, [])])
    activity = ActivityLog(tmp_path / "a.log")
    cyc.activity = activity

    # Mock missions.finish to raise
    orig_finish = missions.finish
    finish_call_count = 0

    def failing_finish(*args, **kwargs):
        nonlocal finish_call_count
        finish_call_count += 1
        raise RuntimeError("db error")

    try:
        missions.finish = failing_finish
        oid = _closed_order(conn)
        # missions.finish failure → fail closed: reflection は保存されない
        result = cyc.run_pending()
        assert result == 0
        assert reflections.get(conn, oid) is None
        rag.add_reflection.assert_not_called()

        # F3: Verify missions.finish called exactly once and activity recorded
        assert finish_call_count == 1
        activity_entries = [line for line in
                            (tmp_path / "a.log").read_text().split("\n")
                            if "mission_finalize_failed" in line]
        assert len(activity_entries) >= 1
    finally:
        missions.finish = orig_finish


def test_finish_failure_retried_next_run_pending(tmp_path):
    """W1-b: finish 失敗の周期後、次の run_pending で同じ order が再試行される。"""
    conn, rag, cyc = _cycle(tmp_path, [
        MissionResult("completed", {"content": "x"}, []),
        MissionResult("completed", {"content": "y"}, []),
    ])
    orig_finish = missions.finish
    calls = {"n": 0}

    def flaky_finish(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db error")
        return orig_finish(*args, **kwargs)

    try:
        missions.finish = flaky_finish
        oid = _closed_order(conn)
        assert cyc.run_pending() == 0
        assert reflections.get(conn, oid) is None
        # 次回 run_pending は SQLite マーカーが無いので同じ order を再試行
        assert cyc.run_pending() == 1
        assert reflections.get(conn, oid)["content"] == "y"
    finally:
        missions.finish = orig_finish


def test_per_item_isolation_two_orders(tmp_path):
    """One order's save failure doesn't block second order processing."""
    # Create 2 results for 2 orders
    conn, rag, cyc = _cycle(tmp_path, [
        MissionResult("completed", {"content": "order1"}, []),
        MissionResult("completed", {"content": "order2"}, []),
    ])

    oid1 = _closed_order(conn)
    oid2 = _closed_order(conn)

    # Make save fail for oid1 only
    orig_save = reflections.save
    def selective_save(c, oid, content, now):
        if oid == oid1:
            raise RuntimeError("save failed")
        return orig_save(c, oid, content, now)

    try:
        reflections.save = selective_save
        result = cyc.run_pending()
        # Should process both, but only oid2 succeeds
        # Since per-item try/except catches oid1 failure, oid2 should be processed
        # rag.add_reflection should be called for both before save fails
        assert result == 1  # only oid2 counted
        assert reflections.get(conn, oid1) is None
        assert reflections.get(conn, oid2)["content"] == "order2"
        # rag should be called twice (once per order, before save)
        assert rag.add_reflection.call_count == 2
    finally:
        reflections.save = orig_save


def test_runner_raises_normalized_to_failed(tmp_path):
    """Runner raising exception is normalized to failed, no rag call."""
    class FailRunner:
        def run(self, mission):
            raise RuntimeError("runner crashed")

    conn = connect(tmp_path / "t.db")
    init_db(conn)
    rag = MagicMock()
    cyc = ReflectionCycle(
        conn=conn, runner=FailRunner(), rag=rag,
        settings=SETTINGS,
        activity=ActivityLog(tmp_path / "a.log"),
        clock=FixedClock(NOW), core_lock=threading.RLock())

    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    assert reflections.get(conn, oid) is None
    rag.add_reflection.assert_not_called()


def test_activity_failure_does_not_block_reflection(tmp_path):
    """activity.write failure is isolated from reflection save (F1)."""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "test"}, [])])
    activity = ActivityLog(tmp_path / "a.log")
    cyc.activity = activity

    # Mock activity.write to raise
    orig_write = activity.write
    def failing_write(*args, **kwargs):
        raise RuntimeError("activity failed")

    try:
        activity.write = failing_write
        oid = _closed_order(conn)
        # Activity failure should not block reflection creation
        result = cyc.run_pending()
        assert result == 1  # reflection created despite activity failure
        assert reflections.get(conn, oid)["content"] == "test"
        rag.add_reflection.assert_called_once_with(oid, "test", "USDJPY")
        # Exception should be logged, not raised
    finally:
        activity.write = orig_write


def test_intent_reasoning_in_prompt(tmp_path):
    """Intent reasoning is included in prompt (F4)."""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "x"}, [])])

    # Create closed order with intent
    oid = _closed_order(conn)

    # Create trade_intent with reasoning
    import json
    intent_payload = {"reasoning": "市場が弱気だから売り"}
    mid = conn.execute(
        "INSERT INTO missions (loop, runner, model, status, started_at) "
        "VALUES ('trade', 'fake', 'fake', 'completed', ?) RETURNING id",
        (NOW.isoformat(),)).fetchone()["id"]
    intent_id = conn.execute(
        "INSERT INTO trade_intents (mission_id, payload_json, created_at) "
        "VALUES (?, ?, ?) RETURNING id",
        (mid, json.dumps(intent_payload), NOW.isoformat())).fetchone()["id"]
    conn.commit()

    # Update order to link intent
    conn.execute("UPDATE orders SET intent_id=? WHERE id=?", (intent_id, oid))
    conn.commit()

    # Run reflection
    cyc.run_pending()

    # Capture the mission prompt that was sent to runner
    from agentic_fx.runners.fake_runner import FakeRunner
    runner = cyc.runner
    assert isinstance(runner, FakeRunner)
    assert len(runner.missions) > 0
    mission_prompt = runner.missions[0].prompt

    # Verify reasoning is in the prompt
    assert "市場が弱気だから売り" in mission_prompt


# ---- W2: completed 出力の検証 ----

def _row_dict(conn, oid):
    return dict(conn.execute(
        "SELECT * FROM orders WHERE id=?", (oid,)).fetchone())


def test_completed_output_none_is_normalized_not_raised(tmp_path):
    """W2: output=None の completed → _reflect_one が例外を出さず False を返す。

    result.output["content"] を無条件参照すると TypeError が発生する
    (`_reflect_one` を直接呼ぶことで、`run_pending` の per-item isolation
    (`except Exception`) に隠されずに区別する — caplog はロガーの
    propagate=False 設定に依存するため信頼できる判別に使わない)。
    """
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", None, [])])
    oid = _closed_order(conn)
    assert cyc._reflect_one(_row_dict(conn, oid)) is False
    assert reflections.get(conn, oid) is None
    rag.add_reflection.assert_not_called()
    # run_pending 経由でも同じ (per-item isolation に落ちない = 0 件で正常終了)
    assert cyc.run_pending() == 0


def test_completed_output_missing_content_is_normalized_not_raised(tmp_path):
    """W2: output dict だが content キー欠落 → _reflect_one が例外を出さず False。"""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"wrong_key": "value"}, [])])
    oid = _closed_order(conn)
    assert cyc._reflect_one(_row_dict(conn, oid)) is False
    assert reflections.get(conn, oid) is None
    rag.add_reflection.assert_not_called()


def test_completed_output_content_not_string_is_normalized(tmp_path):
    """W2: output.content が str でない (int) → _reflect_one が例外を出さず False。"""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": 123}, [])])
    oid = _closed_order(conn)
    assert cyc._reflect_one(_row_dict(conn, oid)) is False
    assert reflections.get(conn, oid) is None
    rag.add_reflection.assert_not_called()


# ---- W3: run_pending の件数 cap ----

def test_run_pending_caps_at_max_items(tmp_path):
    """W3: 5 件の closed order → 1 回目は 3 件のみ処理・残り 2 件は次周期。"""
    conn, rag, cyc = _cycle(tmp_path, [
        MissionResult("completed", {"content": f"r{i}"}, [])
        for i in range(5)])
    oids = [_closed_order(conn) for _ in range(5)]
    first = cyc.run_pending()
    assert first == 3
    for oid in oids[:3]:
        assert reflections.get(conn, oid) is not None
    for oid in oids[3:]:
        assert reflections.get(conn, oid) is None
    second = cyc.run_pending()
    assert second == 2
    for oid in oids[3:]:
        assert reflections.get(conn, oid) is not None


def test_run_pending_max_items_override(tmp_path):
    """W3: max_items を明示指定すると SQL LIMIT に反映される。"""
    conn, rag, cyc = _cycle(tmp_path, [
        MissionResult("completed", {"content": f"r{i}"}, [])
        for i in range(3)])
    oids = [_closed_order(conn) for _ in range(3)]
    result = cyc.run_pending(max_items=1)
    assert result == 1
    assert reflections.get(conn, oids[0]) is not None
    assert reflections.get(conn, oids[1]) is None
    assert reflections.get(conn, oids[2]) is None


def test_null_intent_id_handled(tmp_path):
    """Null intent_id is handled without exception (F4)."""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "no intent"}, [])])

    # Create order without intent
    oid = orders.insert(
        conn, pair="USDJPY", direction="long",
        entry_type="market", horizon="day",
        status=OrderStatus.CLOSED, now=NOW, quantity=0.1,
        realized_pnl=-1500.0, close_reason="sl",
        avg_fill_price=148.5, close_price=148.0,
        intent_id=None)  # Explicitly null

    # Should not raise
    result = cyc.run_pending()
    assert result == 1
    assert reflections.get(conn, oid)["content"] == "no intent"

    # Verify prompt was generated with null entry_reasoning
    from agentic_fx.runners.fake_runner import FakeRunner
    runner = cyc.runner
    mission_prompt = runner.missions[0].prompt
    assert '"entry_reasoning": null' in mission_prompt


def test_reflection_mission_failed_activity_written_with_reason(tmp_path):
    """Task 4 / CP15: reflection Mission 失敗時に reflection_mission_failed
    activity が reason 付きで書かれる (通知は出さない — cyc は Notifier を
    持たないため、通知しないことは構造的に保証されている)。"""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "failed", None, [], reason="context exceeded: prompt 1 tokens "
                                    "> n_ctx 2 (model=m)")])
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    act = (tmp_path / "a.log").read_text(encoding="utf-8")
    assert "reflection_mission_failed" in act
    assert "context exceeded: prompt 1 tokens > n_ctx 2" in act
    assert f"order_id={oid}" in act


def test_reflection_retries_to_limit_then_stops(tmp_path):
    conn, rag, cyc = _cycle(tmp_path, [
        MissionResult("failed", None, [], reason="boom1"),
        MissionResult("failed", None, [], reason="boom2"),
        MissionResult("failed", None, [], reason="must-not-run"),
    ])
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    assert cyc.run_pending() == 0
    assert cyc.run_pending() == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM missions WHERE loop='reflection' "
        "AND status='failed'").fetchone()[0] == 2
    assert conn.execute(
        "SELECT attempts FROM reflection_attempts WHERE order_id=?", (oid,)
    ).fetchone()[0] == 2


def test_reflection_abandoned_activity_written_once_at_limit(tmp_path):
    conn, rag, cyc = _cycle(tmp_path, [
        MissionResult("failed", None, [], reason="one"),
        MissionResult("failed", None, [], reason="two"),
    ])
    _closed_order(conn)
    cyc.run_pending()
    cyc.run_pending()
    cyc.run_pending()
    text = (tmp_path / "a.log").read_text(encoding="utf-8")
    assert text.count("reflection_abandoned") == 1


def test_failed_old_order_does_not_starve_later_order(tmp_path):
    conn, rag, cyc = _cycle(tmp_path, [
        MissionResult("failed", None, [], reason="one"),
        MissionResult("failed", None, [], reason="two"),
        MissionResult("completed", {"content": "later"}, []),
    ])
    first = _closed_order(conn)
    second = _closed_order(conn)
    cyc.run_pending(max_items=1)
    cyc.run_pending(max_items=1)
    assert cyc.run_pending(max_items=1) == 1
    assert reflections.get(conn, first) is None
    assert reflections.get(conn, second)["content"] == "later"


def test_success_clears_prior_attempt_row(tmp_path):
    conn, rag, cyc = _cycle(tmp_path, [
        MissionResult("completed", {"content": "ok"}, [])])
    oid = _closed_order(conn)
    from agentic_fx.store import reflection_attempts
    reflection_attempts.bump(conn, oid, now=NOW, reason="old")
    assert cyc.run_pending() == 1
    assert reflection_attempts.attempts_of(conn, oid) == 0


def test_rag_failure_consumes_attempt_and_stops_at_limit(tmp_path):
    from unittest.mock import Mock
    from agentic_fx.store import reflection_attempts
    conn, rag, cyc = _cycle(tmp_path, [
        MissionResult("completed", {"content": "ok"}, []),
        MissionResult("completed", {"content": "ok"}, [])])
    oid = _closed_order(conn)
    rag.add_reflection = Mock(side_effect=RuntimeError("chroma down"))
    assert cyc.run_pending() == 0
    assert cyc.run_pending() == 0
    assert reflection_attempts.attempts_of(conn, oid) == 2
    before = conn.execute("SELECT COUNT(*) FROM missions").fetchone()[0]
    assert cyc.run_pending() == 0
    assert conn.execute("SELECT COUNT(*) FROM missions").fetchone()[0] == before


def test_finalize_failure_does_not_consume_an_attempt(tmp_path, monkeypatch):
    """Task 15 (設計書 D1) の契約: `finalize_ok=False` は **mission 監査
    書込みの失敗**であって reflection 自体の失敗ではない。試行回数を
    消費させると、`missions.finish` が継続的に失敗する環境で
    **振り返りが max_attempts 回で恒久 abandon される** —
    「監査が書けない」という別の故障が、学習材料の永久喪失に化ける。

    段 0 実測: このピンが無いと `not finalize_ok` 経路で bump する変異が
    フルスイート green のまま生存する。"""
    from agentic_fx.store import reflection_attempts
    conn, rag, cyc = _cycle(tmp_path, [
        MissionResult("completed", {"content": "ok"}, []),
        MissionResult("completed", {"content": "ok"}, [])])
    oid = _closed_order(conn)
    monkeypatch.setattr(
        "agentic_fx.loops.reflection_cycle.finalize_mission",
        lambda *a, **k: False)

    assert cyc.run_pending() == 0
    assert cyc.run_pending() == 0
    assert reflection_attempts.attempts_of(conn, oid) == 0
    assert reflections.get(conn, oid) is None
    # 上限を消費していないので、3 周期目も同じ order が選ばれ続ける。
    assert conn.execute(
        "SELECT COUNT(*) c FROM missions WHERE loop='reflection'"
    ).fetchone()["c"] == 2


def test_bump_is_committed_immediately(tmp_path):
    """Task 15 (設計書 D1): 失敗台帳は **その場で commit** する。
    `bump` の `conn.commit()` を落とすと、未コミットのまま次周期を待つ
    ことになり、プロセス障害 (kill switch 後の再起動・OOM) で試行回数が
    巻き戻って無制限再試行が復活する。別接続から見えることで固定する。"""
    from agentic_fx.store import reflection_attempts
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "failed", None, [], reason="boom")])
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    other = connect(tmp_path / "t.db")
    assert reflection_attempts.attempts_of(other, oid) == 1


def test_bump_records_the_mission_failure_reason(tmp_path):
    """Task 15 (設計書 D1): 台帳の `last_reason` には **spec ② の安全化済み
    `reason` をそのまま**入れる。`reflection_abandoned` の activity は
    件数と order_id しか持たないので、**なぜ恒久失敗したか**を残す唯一の
    場所がこの列である。

    段 0 実測: `reason=result.reason` を `reason=None` にする変異は
    フルスイート green のまま生存する。"""
    reason = "context exceeded: prompt 1 tokens > n_ctx 2 (model=m)"
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "failed", None, [], reason=reason)])
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    row = conn.execute(
        "SELECT attempts, last_reason FROM reflection_attempts "
        "WHERE order_id=?", (oid,)).fetchone()
    assert row["attempts"] == 1
    assert row["last_reason"] == reason


def test_reflection_mission_failed_omits_separator_for_empty_reason(tmp_path):
    """Task 4 mutation pin: 空 reason は activity に区切りを残さない。"""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "failed", None, [], reason="")])
    _closed_order(conn)
    assert cyc.run_pending() == 0
    act = (tmp_path / "a.log").read_text(encoding="utf-8")
    assert "reflection_mission_failed" in act
    assert " —" not in act


def test_reflection_failed_status_with_content_does_not_persist(tmp_path):
    """Task 4 mutation pin: failed の output は保存経路へ流さない。"""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "failed", {"content": "must not persist"}, [], reason="boom")])
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    assert reflections.get(conn, oid) is None
    rag.add_reflection.assert_not_called()


def test_completed_reflection_does_not_write_failure_activity(tmp_path):
    """Task 4 mutation pin: completed を failure event として記録しない。"""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "ok"}, [])])
    _closed_order(conn)
    assert cyc.run_pending() == 1
    assert not any("reflection_mission_failed" in line
                   for line in cyc.activity.tail(10))


def test_reflection_mission_failed_ref_id_is_order_id(tmp_path):
    """Task 4 mutation pin: failure event の ref_id は order ID。"""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "failed", None, [], reason="boom")])
    seed_mid = missions.start(
        conn, "reflection", SETTINGS.runner.trade.backend,
        SETTINGS.runner.trade.model, NOW)
    assert missions.finish(conn, seed_mid, "completed", {}, [], NOW)
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0

    event_line = next(
        line for line in cyc.activity.tail(10)
        if "\treflection_mission_failed\t" in line)
    assert event_line.split("\t")[-1] == str(oid)


def test_reflection_failure_event_write_error_is_caught_at_the_event_site(
        tmp_path):
    """Task 4 / 段 0 の生存変異: `reflection_mission_failed` の書込みが
    例外を投げても、その例外は **event 書込みの場所で捕まる**。

    ⚠️ **`run_pending()` 越しに「経路が止まらないこと」だけを見る形では、
    この防御を測れない。** `run_pending` は per-item isolation の
    `except Exception` を別に持つため、実装の `try/except` を丸ごと外しても
    戻り値も `reflections` 行も変わらない (指揮者が実測: フルスイート
    1776 passed のまま生存)。防御が二重になっているぶん、外側の観測点では
    差が出ない。

    そこで **per-item isolation を経由しない `_reflect_one` を直接呼ぶ**。
    event 書込み側のガードが消えれば、例外はここまで漏れてくる。

    (最初は「どちらの層が捕まえたか」をログ文言で区別する形にしたが、
    `caplog.at_level(logger=...)` は指定 logger のレベルを変えるだけで
    handler は root に付くため、`setup_technical_logging()` が親
    `agentic_fx` を `propagate=False` にした後に走るとログが届かない —
    テスト順序に依存する pin になっていた。1 周目 codex 指摘 M5。)

    層が縮退する (= 例外が本経路を巻き込んでから捕まる) と、将来 event
    書込みの後ろに処理を足したときに、その処理が黙って飛ばされる。"""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "failed", None, [], reason="context exceeded: prompt 1 tokens "
                                   "> n_ctx 2 (model=m)")])
    oid = _closed_order(conn)
    row = dict(conn.execute(
        "SELECT * FROM orders WHERE id=?", (oid,)).fetchone())

    real_write = cyc.activity.write

    def exploding_write(category, event, message, **kwargs):
        if event == "reflection_mission_failed":
            raise OSError("No space left on device")
        return real_write(category, event, message, **kwargs)

    cyc.activity.write = exploding_write

    # ガードが消えていればここで OSError が漏れる (per-item isolation は
    # run_pending 側にあり、この呼び出しでは効かない)。
    assert cyc._reflect_one(row) is False
    assert reflections.get(conn, oid) is None
    rag.add_reflection.assert_not_called()


@pytest.mark.parametrize("status", ["failed", "timeout", "max_turns"])
def test_reflection_failure_event_covers_every_non_completed_status(
        tmp_path, status):
    """Task 4 / 1 周目 codex 指摘 I2: **`completed` 以外のすべての status**
    で failure event が 1 件残り、status が本文に入る。reason は `None`。

    Task 4 の event テストはどれも `status="failed"` かつ reason 付き
    だったため、ガードを `if result.status == "failed":` に狭める変異も、
    `if result.reason is not None:` に置き換える変異も**フルスイート
    1777 passed のまま生存する** (指揮者が実測)。

    `timeout` は llama-swap の応答が `llama_swap.timeout_sec` を超えた
    ときに出る**最も起きやすい失敗**であり、ここが記録されないと
    reflection が黙って進まなくなる。"""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(status, None, [])])
    oid = _closed_order(conn)

    assert cyc.run_pending() == 0

    act = (tmp_path / "a.log").read_text(encoding="utf-8")
    assert act.count("reflection_mission_failed") == 1
    assert f"status={status}" in act
    assert f"order_id={oid}" in act
    # reason が None なら区切りも "None" も出さない。
    assert "—" not in act
    assert "None" not in act


@pytest.mark.parametrize("status", ["failed", "timeout", "max_turns"])
def test_non_completed_never_saves_reflection_even_with_valid_content(
        tmp_path, status):
    """Task 4 / 1 周目 codex 指摘 I2: `completed` 以外は、**output が
    保存可能な形をしていても** reflection を保存しない。

    既存の失敗系テストは output=None なので、早期出口
    (`if result.status != "completed" or not finalize_ok:`) を
    `== "failed"` に狭める変異を打っても、後段の型ガードが同じく
    `False` を返して同じ見かけの結果になる — 別の防御に隠れて生存する
    (Task 4 の trade 側 I1 と同じ構造)。

    status が真の判断根拠であることを、**後段のどの防御にも頼らない
    形**で固定する。"""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        status, {"content": "SL 幅が狭すぎた"}, [])])
    oid = _closed_order(conn)

    assert cyc.run_pending() == 0
    assert reflections.get(conn, oid) is None
    rag.add_reflection.assert_not_called()


def test_reflection_mission_failed_activity_line_is_pinned_field_by_field(
        tmp_path):
    """Task 4 / 1 周目 codex 指摘 I3: `reflection_mission_failed` の
    activity 行を**フィールド単位の完全一致**で固定する。

    本文から `mission_id` や `status` を落とす変異、category を
    `AGGREGATE` 以外にする変異が、部分一致の assert では素通りしていた。
    reflection は通知を持たないため、**この 1 行が失敗を知る唯一の
    経路**であり、欠けたフィールドは復元できない。"""
    reason = "context exceeded: prompt 1 tokens > n_ctx 2 (model=m)"
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "failed", None, [], reason=reason)])
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0

    mid = conn.execute(
        "SELECT id FROM missions WHERE loop='reflection'").fetchone()["id"]
    lines = [ln for ln in (tmp_path / "a.log").read_text(
        encoding="utf-8").splitlines()
        if "\treflection_mission_failed\t" in ln]
    assert len(lines) == 1, lines
    _ts, category, event, summary, ref_id = lines[0].split("\t")
    assert category == "AGGREGATE"
    assert event == "reflection_mission_failed"
    assert summary == (
        f"order_id={oid} mission_id={mid} status=failed — {reason}")
    assert ref_id == str(oid)


def test_abandon_releases_starved_later_orders(tmp_path):
    """Task 4 / codex I4 のピンを Task 15 (設計書 D1) の契約へ更新する。

    旧ピンは「失敗し続ける先頭 3 件が `max_items` 枠を永久に占有し、
    4 件目は一度も選ばれない」という**現挙動の固定**だった。D1 は
    `ORDER BY o.id` を変えずに **abandon で枠を空ける**ことで starvation を
    解く。したがってこのテストは「上限到達までは先頭 3 件が占有し、
    到達後は 4 件目が選ばれる」へ書き換える (削除ではなく更新)。"""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult("failed", None, [])])
    oids = [_closed_order(conn) for _ in range(4)]
    assert len(set(oids)) == 4

    for _ in range(3):
        assert cyc.run_pending() == 0

    summaries = [ln.split("\t")[3] for ln in
                 (tmp_path / "a.log").read_text(encoding="utf-8").splitlines()
                 if "\treflection_mission_failed\t" in ln]
    # 先頭 3 件は上限 (2) まで試行され、そこで打ち切られる。
    for oid in oids[:3]:
        assert sum(1 for s in summaries
                   if s.startswith(f"order_id={oid} ")) == 2
    # 3 周期目で枠が空き、4 件目がはじめて選ばれる (starvation の解消)。
    assert sum(1 for s in summaries
               if s.startswith(f"order_id={oids[3]} ")) == 1

    abandoned = [ln for ln in
                 (tmp_path / "a.log").read_text(encoding="utf-8").splitlines()
                 if "\treflection_abandoned\t" in ln]
    assert len(abandoned) == 3

    assert conn.execute(
        "SELECT COUNT(*) c FROM missions WHERE loop='reflection'"
    ).fetchone()["c"] == 7
