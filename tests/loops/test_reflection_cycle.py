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


def test_reflection_current_retry_behavior_is_pinned(tmp_path):
    """Task 4 / CP16 (回帰固定): 同一 order で 2 回 run_pending を呼んでも
    starvation せず**毎回同じ order が選ばれ続ける** — missions 行が 2 本
    (どちらも failed)・reflection_mission_failed activity が 2 本・
    reflections 行は 0 本のまま (無制限再試行の現挙動。修正は設計書 §4.5
    codex I3 で既に独立課題として起票済み・本 task では直さない)。"""
    conn, rag, cyc = _cycle(tmp_path, [
        MissionResult("failed", None, [], reason="boom1"),
        MissionResult("failed", None, [], reason="boom2"),
    ])
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    assert cyc.run_pending() == 0

    failed_missions = conn.execute(
        "SELECT COUNT(*) c FROM missions WHERE status='failed'"
    ).fetchone()["c"]
    assert failed_missions == 2

    act = (tmp_path / "a.log").read_text(encoding="utf-8")
    assert act.count("reflection_mission_failed") == 2

    assert reflections.get(conn, oid) is None


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
        tmp_path, caplog):
    """Task 4 / 段 0 の生存変異: `reflection_mission_failed` の書込みが
    例外を投げても reflection 経路は止まらず、**その例外は event 書込みの
    場所で捕まる**。

    ⚠️ **「経路が止まらないこと」だけを見る形では、この防御を測れない。**
    `run_pending` は per-item isolation の `except Exception` を別に持つ
    ため、実装の `try/except` を丸ごと外しても `run_pending() == 0` と
    `reflections.get(...) is None` は**どちらも変わらない**
    (指揮者が実測: フルスイート 1776 passed のまま生存)。防御が二重に
    なっているぶん、外側の観測点では差が出ない。

    そこで **どちらの層が捕まえたかをログで区別する**。event 書込み側の
    ガードが消えると、例外は per-item isolation まで昇格し、
    `"failed to record reflection_mission_failed"` が出なくなる。

    層が縮退する (= 例外が本経路を巻き込んでから捕まる) と、将来 event
    書込みの後ろに処理を足したときに、その処理が黙って飛ばされる。"""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "failed", None, [], reason="context exceeded: prompt 1 tokens "
                                   "> n_ctx 2 (model=m)")])
    oid = _closed_order(conn)

    real_write = cyc.activity.write

    def exploding_write(category, event, message, **kwargs):
        if event == "reflection_mission_failed":
            raise OSError("No space left on device")
        return real_write(category, event, message, **kwargs)

    cyc.activity.write = exploding_write

    with caplog.at_level(logging.ERROR, logger="agentic_fx.reflection"):
        assert cyc.run_pending() == 0

    assert reflections.get(conn, oid) is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("failed to record reflection_mission_failed" in m
               for m in messages), messages
    # per-item isolation まで昇格していないこと (= 本経路を巻き込んでいない)。
    assert not any("per-item reflection failed" in m for m in messages), messages


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
