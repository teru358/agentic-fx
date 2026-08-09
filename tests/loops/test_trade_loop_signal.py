"""TradeLoop の signal-aware lifecycle テスト (プラン 7 Task 8)。

`_loop`/`NOW`/`SETTINGS` は `tests/loops/test_trade_loop.py` のものを再利用
する (FakeRunner・Executor 配線は既存と同一)。
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from agentic_fx.runners.base import MissionResult
from agentic_fx.store import signals

from tests.loops.test_trade_loop import NOW, SETTINGS, _loop


def _add_signal(conn, *, plugin="sig1", pair="USDJPY", timeframe="1h",
                bar_ts=None):
    bar_ts = bar_ts or (NOW - timedelta(hours=1))
    return signals.add(
        conn, plugin=plugin, content_hash="h1", pair=pair, timeframe=timeframe,
        bar_ts=bar_ts.isoformat(), kind="signal",
        payload={"direction": "long", "strength": 0.7, "rationale": "up"},
        now=bar_ts)


# ---------------------------------------------------------------------
# ⑥ claim → set_trigger → プロンプト搭載 → パース成功時 consumed
# ---------------------------------------------------------------------
def test_signal_claim_set_trigger_prompt_injection_and_consume(tmp_path):
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "ok"}, [])])
    sid = _add_signal(conn)

    out = loop.run_once("signal")

    assert out is not None
    m = conn.execute("SELECT * FROM missions").fetchone()
    assert m["trigger"] == "signal:sig1"  # ④set_trigger で plugin 名が確定
    prompt = runner.missions[0].prompt  # ⑤FakeRunner が受信したプロンプト
    assert "sig1" in prompt
    assert "USDJPY" in prompt
    assert "1h" in prompt
    row = conn.execute(
        "SELECT status FROM signals WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "consumed"  # パース成功時点で確定


# ---------------------------------------------------------------------
# ⑦ runner/パース失敗で requeue
# ---------------------------------------------------------------------
def test_signal_requeue_on_runner_failure(tmp_path):
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult("timeout", None, [])])
    sid = _add_signal(conn)

    out = loop.run_once("signal")

    assert out is None
    row = conn.execute(
        "SELECT status, requeue_count FROM signals WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "pending"
    assert row["requeue_count"] == 1


def test_signal_requeue_on_parse_failure(tmp_path):
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "buy!"}, [])])
    sid = _add_signal(conn)

    out = loop.run_once("signal")

    assert out is None
    row = conn.execute(
        "SELECT status FROM signals WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "pending"


def test_signal_abandoned_after_requeue_limit_notifies(tmp_path):
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult("timeout", None, [])])
    max_requeue = SETTINGS.plugin.signal_requeue_max
    sid = _add_signal(conn)
    notify_calls: list[str] = []
    loop.notifier.send = lambda msg: notify_calls.append(msg)

    for _ in range(max_requeue + 1):
        loop.run_once("signal")

    row = conn.execute(
        "SELECT status FROM signals WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "abandoned"
    assert any("abandoned" in m for m in notify_calls)


# ---------------------------------------------------------------------
# ⑧ executor.handle_intent が例外でも consumed のまま (requeue されない — 二重発注ピン)
# ---------------------------------------------------------------------
def test_signal_stays_consumed_when_executor_raises(tmp_path):
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "ok"}, [])])
    sid = _add_signal(conn)
    # プラン8 五相再構成: commit-core の dispatch 入口は
    # executor.record_and_validate_intent (handle_intent はもう呼ばれない)
    loop.executor.record_and_validate_intent = MagicMock(
        side_effect=RuntimeError("executor_boom"))

    out = loop.run_once("signal")

    assert out is None
    row = conn.execute(
        "SELECT status FROM signals WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "consumed"  # requeue されない


# ---------------------------------------------------------------------
# 鮮度: claim は settings.plugin.signal_freshness_bars を鮮度ゲートとして
# claim_oldest に渡す (freshness_bars を渡し忘れる/None にする変異の killer
# — advisor 指摘)。1h plugin・既定 signal_freshness_bars=2 で鮮度窓は 2h。
# 窓を大きく超える古い pending だけが存在する場合、claim は鮮度ゲートで
# 何も拾えず (None) skipped で終端する。
# ---------------------------------------------------------------------
def test_signal_claim_respects_freshness_gate(tmp_path):
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "ok"}, [])])
    stale_ts = NOW - timedelta(hours=10)  # 鮮度窓 (2h) を大きく超える古さ
    sid = _add_signal(conn, bar_ts=stale_ts)

    out = loop.run_once("signal")

    assert out is None
    assert runner.missions == []  # 鮮度ゲートで claim 対象にすら入らない
    row = conn.execute(
        "SELECT status FROM signals WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "pending"  # claim されていない (abandoned でもない)
    m = conn.execute("SELECT status FROM missions").fetchone()
    assert m["status"] == "skipped"


# ---------------------------------------------------------------------
# fix round 1 F2 (codex): claim_oldest 自体が例外を出しても mission が
# running のまま残らない (missions.finish("failed") で終端し、例外は
# 公開境界 (run_once) まで伝播して never-raise 契約どおり None になる)。
# ---------------------------------------------------------------------
def test_signal_claim_oldest_exception_finalizes_mission_as_failed(tmp_path):
    conn, loop, runner, tp = _loop(tmp_path, [])
    _add_signal(conn)

    with patch("agentic_fx.loops.trade_loop.signals.claim_oldest",
              side_effect=RuntimeError("db boom")):
        out = loop.run_once("signal")

    assert out is None
    assert runner.missions == []  # LLM は起動されていない
    m = conn.execute("SELECT status FROM missions").fetchone()
    assert m["status"] == "failed"  # running のまま残らない


# ---------------------------------------------------------------------
# ⑨ claim 失敗で skipped・LLM 不起動
# ---------------------------------------------------------------------
def test_signal_claim_failure_finalizes_skipped_without_running_llm(tmp_path):
    conn, loop, runner, tp = _loop(tmp_path, [])  # pending signal 無し

    out = loop.run_once("signal")

    assert out is None
    assert runner.missions == []  # LLM (runner.run) は一切呼ばれていない
    m = conn.execute("SELECT * FROM missions").fetchone()
    assert m["status"] == "skipped"
    assert m["trigger"] == "signal"


# ---------------------------------------------------------------------
# ⑩ healthcheck 失敗は claim 前離脱
# ---------------------------------------------------------------------
def test_signal_healthcheck_failure_skips_before_claim(tmp_path):
    conn, loop, runner, tp = _loop(tmp_path, [], healthy=False)
    sid = _add_signal(conn)

    out = loop.run_once("signal")

    assert out is None
    assert runner.missions == []
    # claim が一切試みられていない (signal は pending のまま・missions 行も無い)
    row = conn.execute(
        "SELECT status FROM signals WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "pending"
    assert conn.execute("SELECT COUNT(*) c FROM missions").fetchone()["c"] == 0
