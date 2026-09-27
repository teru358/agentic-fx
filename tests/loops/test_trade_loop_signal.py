"""TradeLoop の signal-aware lifecycle テスト (プラン 7 Task 8)。

`_loop`/`NOW`/`SETTINGS` は `tests/loops/test_trade_loop.py` のものを再利用
する (FakeRunner・Executor 配線は既存と同一)。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from unittest.mock import MagicMock, patch

import pytest
import yaml

from agentic_fx.config import Settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import mission_decision_bars, signals
from agentic_fx.store.db import connect

from tests.loops.test_trade_loop import NOW, SETTINGS, _loop

_EXAMPLE_PATH = (Path(__file__).resolve().parents[2] / "config"
                / "settings.yaml.example")


def _settings_with_eurusd() -> Settings:
    """USDJPY に加え EURUSD を設定内 pair にした Settings。signal は
    approved plugin の実行結果としてしか生まれず、plugin の対象 pair は
    settings.pairs の範囲に限られる — テスト fixture の pair も
    settings.pairs 内でなければ本番では起こり得ない入力になる。"""
    raw = yaml.safe_load(_EXAMPLE_PATH.read_text(encoding="utf-8"))
    raw["pairs"] = ["USDJPY", "EURUSD"]  # risk.pair_rules は EXAMPLE に両方揃っている
    return Settings.model_validate(raw)


def test_settings_with_eurusd_fixture_includes_eurusd_pair():
    """`_settings_with_eurusd` の settings.pairs が実際に EURUSD を含む
    ことをピンする (既定 EXAMPLE は USDJPY のみ — settings.yaml.example
    L5)。"""
    settings = _settings_with_eurusd()
    assert settings.pairs == ["USDJPY", "EURUSD"]


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
# ⑧ consume 成功後の executor 執行例外でも consumed のまま
# (requeue されない — 二重発注ピン)
# ---------------------------------------------------------------------
def test_signal_stays_consumed_when_executor_raises(tmp_path):
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "cancel", "order_id": 999999,
                      "reasoning": "ok"}, [])])
    sid = _add_signal(conn)
    # record → consume の後にある執行分岐を失敗させる。記録自体の失敗は
    # consume 前なので requeue されるのが新しい順序契約。
    loop.executor.cancel_intent = MagicMock(
        side_effect=RuntimeError("executor_boom"))

    out = loop.run_once("signal")

    assert out is None
    row = conn.execute(
        "SELECT status FROM signals WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "consumed"  # requeue されない


def test_signal_requeues_when_intent_recording_raises(tmp_path):
    """intent 記録失敗は consume 前なので、claimed signal を requeue する。"""
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "ok"}, [])])
    sid = _add_signal(conn)
    loop.executor.record_and_validate_intent = MagicMock(
        side_effect=RuntimeError("record boom"))

    with patch("agentic_fx.loops.trade_loop.signals.consume",
               wraps=signals.consume) as consume:
        out = loop.run_once("signal")

    assert out is None
    consume.assert_not_called()
    row = conn.execute(
        "SELECT status, requeue_count FROM signals WHERE id=?", (sid,)).fetchone()
    assert dict(row) == {"status": "pending", "requeue_count": 1}


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


# ---------------------------------------------------------------------
# cron mission の signal 購読: 判断足の cron mission も既存の
# claim_oldest で最古の pending を 1 件だけ自分へ claim し、prompt の
# 「この判断で扱う signal」節に載せ、intent のパース成功で consume する。
# ---------------------------------------------------------------------
THU_1400 = datetime(2026, 9, 24, 14, 0, 30, tzinfo=timezone.utc)
_HOLD = MissionResult("completed", {"action": "hold", "reasoning": "ok"}, [])


def _loop_at(tmp_path, results, now):
    conn, loop, runner, tp = _loop(tmp_path, results)
    loop.clock = FixedClock(now)
    loop.executor.clock = FixedClock(now)
    return conn, loop, runner, tp


def _add_at(conn, *, pair, bar_ts, plugin="sma_cross_10_30", timeframe="1h",
            direction="long"):
    return signals.add(
        conn, plugin=plugin, content_hash=f"h-{pair}-{bar_ts.isoformat()}",
        pair=pair, timeframe=timeframe, bar_ts=bar_ts.isoformat(),
        kind="strategy",
        payload={"direction": direction, "stop_loss": 147.0,
                 "take_profit": 150.0},
        now=bar_ts)


def _signal_row(conn, sid):
    return dict(conn.execute(
        "SELECT status, requeue_count, claimed_by_mission_id FROM signals "
        "WHERE id=?", (sid,)).fetchone())


def _cron_bars(now, pair="USDJPY"):
    return MappingProxyType({(pair, "15m"): now - timedelta(minutes=15, seconds=30)})


def test_cron_mission_claims_signal_after_commit_and_consumes_on_hold(tmp_path):
    conn, loop, runner, _ = _loop_at(tmp_path, [_HOLD], THU_1400)
    sid = _add_at(conn, pair="USDJPY",
                  bar_ts=datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc))

    out = loop.run_once("cron", decision_bars=_cron_bars(THU_1400))

    assert out["result"] == "hold"
    m = conn.execute("SELECT id, trigger, status FROM missions").fetchone()
    assert (m["trigger"], m["status"]) == ("cron", "completed")
    assert _signal_row(conn, sid) == {
        "status": "consumed", "requeue_count": 0,
        "claimed_by_mission_id": m["id"]}
    assert mission_decision_bars.for_mission(conn, m["id"]) == dict(
        _cron_bars(THU_1400))
    prompt = runner.missions[0].prompt
    section = prompt.split("## この判断で扱う signal", 1)[1]
    assert f"- signal_id: {sid}\n" in section
    assert "- pair: USDJPY\n" in section
    assert "- direction: long\n" in section
    assert "- strategy_timeframe: 1h\n" in section
    assert "- bar_ts: 2026-09-24T13:00:00+00:00\n" in section
    assert "- bar_close: 2026-09-24T14:00:00+00:00\n" in section
    assert "- fresh_until: 2026-09-24T15:00:00+00:00\n" in section


def test_cron_mission_without_pending_signal_runs_without_signal_section(tmp_path):
    conn, loop, runner, _ = _loop_at(tmp_path, [_HOLD], THU_1400)

    out = loop.run_once("cron", decision_bars=_cron_bars(THU_1400))

    assert out["result"] == "hold"
    assert len(runner.missions) == 1
    assert "この判断で扱う signal" not in runner.missions[0].prompt
    m = conn.execute("SELECT trigger, status FROM missions").fetchone()
    assert (m["trigger"], m["status"]) == ("cron", "completed")


def test_cron_claim_happens_after_mission_rows_are_committed(tmp_path):
    conn, loop, runner, tp = _loop_at(tmp_path, [_HOLD], THU_1400)
    _add_at(conn, pair="USDJPY",
            bar_ts=datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc))
    seen = []
    real_claim = signals.claim_oldest

    def spy(c, *, mission_id, now, freshness_bars):
        other = connect(tp / "t.db")
        try:
            seen.append((
                c.in_transaction, now, freshness_bars,
                other.execute("SELECT COUNT(*) c FROM missions WHERE id=?",
                              (mission_id,)).fetchone()["c"],
                mission_decision_bars.for_mission(other, mission_id)))
        finally:
            other.close()
        return real_claim(c, mission_id=mission_id, now=now,
                          freshness_bars=freshness_bars)

    with patch("agentic_fx.loops.trade_loop.signals.claim_oldest", spy):
        loop.run_once("cron", decision_bars=_cron_bars(THU_1400))

    assert seen == [(False, THU_1400, SETTINGS.plugin.signal_freshness_bars,
                     1, dict(_cron_bars(THU_1400)))]


def test_cron_mission_claims_only_the_oldest_of_three_pending(tmp_path):
    conn, loop, runner, _ = _loop_at(tmp_path, [_HOLD, _HOLD], THU_1400)
    loop.settings = _settings_with_eurusd()  # EURUSD を設定内 pair にする
    usd_1245 = _add_at(conn, pair="USDJPY",
                       bar_ts=datetime(2026, 9, 24, 12, 45, tzinfo=timezone.utc))
    eur_1230 = _add_at(conn, pair="EURUSD",
                       bar_ts=datetime(2026, 9, 24, 12, 30, tzinfo=timezone.utc))
    usd_1300 = _add_at(conn, pair="USDJPY",
                       bar_ts=datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc))

    loop.run_once("cron", decision_bars=_cron_bars(THU_1400))

    first = conn.execute("SELECT id FROM missions").fetchone()["id"]
    assert _signal_row(conn, eur_1230) == {
        "status": "consumed", "requeue_count": 0,
        "claimed_by_mission_id": first}
    for sid in (usd_1245, usd_1300):
        assert _signal_row(conn, sid) == {
            "status": "pending", "requeue_count": 0,
            "claimed_by_mission_id": None}
    section = runner.missions[0].prompt.split("## この判断で扱う signal", 1)[1]
    assert "- pair: EURUSD\n" in section
    assert "USDJPY" not in section

    later = THU_1400 + timedelta(minutes=15)
    loop.clock = FixedClock(later)
    loop.executor.clock = FixedClock(later)
    loop.run_once("cron", decision_bars=_cron_bars(later))

    second = conn.execute(
        "SELECT id FROM missions ORDER BY id DESC LIMIT 1").fetchone()["id"]
    assert _signal_row(conn, usd_1245)["claimed_by_mission_id"] == second
    assert _signal_row(conn, usd_1245)["status"] == "consumed"
    assert _signal_row(conn, usd_1300)["status"] == "pending"


def test_cron_prepare_exception_after_claim_fails_mission_and_requeues(tmp_path):
    conn, loop, runner, tp = _loop_at(tmp_path, [_HOLD], THU_1400)
    sid = _add_at(conn, pair="USDJPY",
                  bar_ts=datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc))
    real_build = loop._build_mission
    calls = []

    def boom_once(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError("injected after claim")
        return real_build(prompt)

    loop._build_mission = boom_once

    assert loop.run_once("cron", decision_bars=_cron_bars(THU_1400)) is None

    first = conn.execute("SELECT id, status FROM missions").fetchone()
    assert first["status"] == "failed"
    assert _signal_row(conn, sid) == {
        "status": "pending", "requeue_count": 1, "claimed_by_mission_id": None}
    assert runner.missions == []
    log = (tp / "a.log").read_text(encoding="utf-8")
    assert "mission_finalize_conflict" not in log

    later = THU_1400 + timedelta(minutes=15)
    loop.clock = FixedClock(later)
    loop.executor.clock = FixedClock(later)
    out = loop.run_once("cron", decision_bars=_cron_bars(later))

    assert out["result"] == "hold"
    second = conn.execute(
        "SELECT id FROM missions ORDER BY id DESC LIMIT 1").fetchone()["id"]
    assert second != first["id"]
    assert _signal_row(conn, sid) == {
        "status": "consumed", "requeue_count": 1,
        "claimed_by_mission_id": second}


def test_cron_claim_exception_finalizes_committed_mission_as_failed(tmp_path):
    conn, loop, runner, _ = _loop_at(tmp_path, [_HOLD], THU_1400)
    _add_at(conn, pair="USDJPY",
            bar_ts=datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc))

    with patch("agentic_fx.loops.trade_loop.signals.claim_oldest",
               side_effect=RuntimeError("db boom")):
        assert loop.run_once("cron", decision_bars=_cron_bars(THU_1400)) is None

    assert runner.missions == []
    m = conn.execute("SELECT trigger, status FROM missions").fetchone()
    assert (m["trigger"], m["status"]) == ("cron", "failed")


def test_cron_mission_keeps_cron_trigger_so_signal_rate_limit_is_unchanged(tmp_path):
    conn, loop, runner, _ = _loop_at(tmp_path, [_HOLD], THU_1400)
    _add_at(conn, pair="USDJPY",
            bar_ts=datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc))

    loop.run_once("cron", decision_bars=_cron_bars(THU_1400))

    from agentic_fx.store import missions as missions_store
    assert conn.execute("SELECT trigger FROM missions").fetchone()["trigger"] == "cron"
    assert missions_store.signals_rate_ok(conn, THU_1400, SETTINGS) is True


def test_cron_timeout_requeues_claimed_signal_and_third_requeue_abandons(tmp_path):
    """requeue 上限 (既定 2): timeout の cron mission が claim した signal は
    pending・requeue_count=1 に戻り、3 回目の requeue (count=2) で abandoned。"""
    assert SETTINGS.plugin.signal_requeue_max == 2  # 前提
    timeout = MissionResult("timeout", None, [])
    conn, loop, runner, _ = _loop_at(tmp_path, [timeout, timeout, timeout],
                                     THU_1400)
    sid = _add_at(conn, pair="USDJPY",
                  bar_ts=datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc))
    notify_calls: list[str] = []
    loop.notifier.send = lambda msg: notify_calls.append(msg)

    seen = []
    for minutes in (0, 15, 30):
        now = THU_1400 + timedelta(minutes=minutes)
        loop.clock = FixedClock(now)
        loop.executor.clock = FixedClock(now)
        assert loop.run_once("cron", decision_bars=_cron_bars(now)) is None
        row = _signal_row(conn, sid)
        seen.append((row["status"], row["requeue_count"]))

    assert seen == [("pending", 1), ("pending", 2), ("abandoned", 2)]
    assert len(runner.missions) == 3
    assert [r["status"] for r in conn.execute(
        "SELECT status FROM missions ORDER BY id")] == ["timeout"] * 3
    assert any(f"signal #{sid}" in m and "abandoned" in m for m in notify_calls)


def test_cron_max_turns_requeues_claimed_signal(tmp_path):
    """timeout と同じく status != "completed" 系の失敗 (max_turns) でも
    claim 済み signal は pending・requeue_count=1 に戻る。"""
    max_turns = MissionResult("max_turns", None, [])
    conn, loop, runner, _ = _loop_at(tmp_path, [max_turns], THU_1400)
    sid = _add_at(conn, pair="USDJPY",
                  bar_ts=datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc))

    assert loop.run_once("cron", decision_bars=_cron_bars(THU_1400)) is None

    assert _signal_row(conn, sid) == {
        "status": "pending", "requeue_count": 1, "claimed_by_mission_id": None}
    m = conn.execute("SELECT status FROM missions").fetchone()
    assert m["status"] == "max_turns"


def test_cron_intent_parse_failure_requeues_claimed_signal(tmp_path):
    """runner status="completed" でも output が intent としてパースできな
    ければ claim 済み signal は requeue される (timeout/max_turns と同じ
    finally の _requeue_signal 経路)。"""
    unparseable = MissionResult("completed", {"action": "buy!"}, [])
    conn, loop, runner, _ = _loop_at(tmp_path, [unparseable], THU_1400)
    sid = _add_at(conn, pair="USDJPY",
                  bar_ts=datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc))

    assert loop.run_once("cron", decision_bars=_cron_bars(THU_1400)) is None

    assert _signal_row(conn, sid) == {
        "status": "pending", "requeue_count": 1, "claimed_by_mission_id": None}
    m = conn.execute("SELECT status FROM missions").fetchone()
    assert m["status"] == "completed"


def test_cron_lease_reclaimed_while_running_makes_consume_fail_closed(tmp_path):
    """mission 実行中 (claim から consume までの間) に signal_lease_min が
    切れて `reclaim_expired` が claim 済み signal を先に pending へ戻すと、
    `signals.consume` の CAS (claimed_by_mission_id 一致) が不一致になり
    ValueError を送出する。判断は signal 拒否として fail closed になり
    (発注しない)、signal は reclaim_expired 自身の判定どおり
    pending・requeue_count=1 のまま (finally の重複 requeue 試行は対象が
    既に claimed でないため無害に失敗する)。"""
    conn, loop, runner, _ = _loop_at(tmp_path, [_HOLD], THU_1400)
    sid = _add_at(conn, pair="USDJPY",
                  bar_ts=datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc))

    real_consume = signals.consume

    def consume_after_lease_expiry(conn_, signal_id, *, mission_id, now):
        # commit-core (consume 直前) で lease が切れて reclaim_expired が
        # 先に走った状態を模す (lease_min=0 で claimed_at <= now は即回収)。
        signals.reclaim_expired(conn_, now=now, lease_min=0,
                                max_requeue=SETTINGS.plugin.signal_requeue_max)
        return real_consume(conn_, signal_id, mission_id=mission_id, now=now)

    with patch("agentic_fx.loops.trade_loop.signals.consume",
              consume_after_lease_expiry):
        out = loop.run_once("cron", decision_bars=_cron_bars(THU_1400))

    assert out is not None
    assert out["result"] == "rejected"
    assert "signal not claimed by this mission" in out["reasons"][0]
    assert _signal_row(conn, sid) == {
        "status": "pending", "requeue_count": 1, "claimed_by_mission_id": None}
    m = conn.execute("SELECT status FROM missions").fetchone()
    assert m["status"] == "completed"


# ---------------------------------------------------------------------
# signal 節の全文・足幅と鮮度本数・claim 例外の終端・consume 後の扱い
# ---------------------------------------------------------------------
def _signal_section(prompt):
    return prompt.split("\n\n## この判断で扱う signal\n", 1)[1]


def test_cron_signal_section_is_complete_and_says_signal_is_not_an_order(tmp_path):
    """cron mission の signal 節は、signal が発注指示ではなく判断の確定で
    consumed になる旨の前書きと、行の全フィールドをこの順で載せる。"""
    conn, loop, runner, _ = _loop_at(tmp_path, [_HOLD], THU_1400)
    sid = _add_at(conn, pair="USDJPY",
                  bar_ts=datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc))

    loop.run_once("cron", decision_bars=_cron_bars(THU_1400))

    assert _signal_section(runner.missions[0].prompt) == (
        "この判断足の Mission は以下の signal を引き受けています。"
        "signal は strategy の足の確定に対する plugin の判定で、"
        "発注指示ではありません。判断が確定すると (hold を含む) "
        "この signal は consumed になり、他の Mission には渡りません。\n"
        f"- signal_id: {sid}\n"
        "- plugin: sma_cross_10_30\n"
        "- kind: strategy\n"
        "- pair: USDJPY\n"
        "- direction: long\n"
        "- strategy_timeframe: 1h\n"
        "- bar_ts: 2026-09-24T13:00:00+00:00\n"
        "- bar_close: 2026-09-24T14:00:00+00:00\n"
        "- fresh_until: 2026-09-24T15:00:00+00:00\n"
        '- payload: {"direction": "long", "stop_loss": 147.0, '
        '"take_profit": 150.0}\n')


def test_signal_trigger_section_keeps_trigger_lead(tmp_path):
    """signal 起動の mission は「signal をトリガーに起動された」前書きの
    まま (cron の前書きには置き換わらない)。"""
    conn, loop, runner, _ = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "ok"}, [])])
    _add_signal(conn)

    loop.run_once("signal")

    section = _signal_section(runner.missions[0].prompt)
    assert section.startswith(
        "このMissionは以下の signal/strategy plugin の出力をトリガーに"
        "起動されました。判断の参考にしてください。\n- signal_id: ")
    assert "発注指示ではありません" not in section


def test_signal_section_uses_strategy_bar_width_and_configured_freshness(tmp_path):
    """bar_close・fresh_until は signal を作った strategy の足幅と設定の
    鮮度本数から計算する。15m・鮮度 3 本・bar_ts 13:45 なら bar_close
    14:00・fresh_until 14:30。"""
    conn, loop, runner, _ = _loop_at(tmp_path, [_HOLD], THU_1400)
    settings3 = SETTINGS.model_copy(deep=True)
    settings3.plugin.signal_freshness_bars = 3
    loop.settings = settings3
    _add_at(conn, pair="USDJPY", timeframe="15m",
            bar_ts=datetime(2026, 9, 24, 13, 45, tzinfo=timezone.utc))

    loop.run_once("cron", decision_bars=_cron_bars(THU_1400))

    section = _signal_section(runner.missions[0].prompt)
    assert ("- strategy_timeframe: 15m\n"
            "- bar_ts: 2026-09-24T13:45:00+00:00\n"
            "- bar_close: 2026-09-24T14:00:00+00:00\n"
            "- fresh_until: 2026-09-24T14:30:00+00:00\n") in section


def test_consumed_signal_is_not_sent_back_to_requeue(tmp_path):
    """パース成功で consume した signal は finally の requeue に回さない。"""
    conn, loop, runner, _ = _loop_at(tmp_path, [_HOLD], THU_1400)
    sid = _add_at(conn, pair="USDJPY",
                  bar_ts=datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc))

    with patch("agentic_fx.loops.trade_loop.signals.requeue",
               wraps=signals.requeue) as requeue:
        loop.run_once("cron", decision_bars=_cron_bars(THU_1400))

    assert _signal_row(conn, sid)["status"] == "consumed"
    assert requeue.call_count == 0


@pytest.mark.parametrize("trigger", ["cron", "signal"])
def test_claim_exception_finalizes_mission_once_without_conflict(tmp_path, trigger):
    """claim_oldest の例外で mission は failed に 1 回だけ終端し、finally
    からの二重 finalize (mission_finalize_conflict) を起こさない。"""
    conn, loop, runner, tp = _loop_at(tmp_path, [_HOLD], THU_1400)
    _add_at(conn, pair="USDJPY",
            bar_ts=datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc))
    bars = _cron_bars(THU_1400) if trigger == "cron" else None

    with patch("agentic_fx.loops.trade_loop.signals.claim_oldest",
               side_effect=RuntimeError("db boom")):
        assert loop.run_once(trigger, decision_bars=bars) is None

    assert runner.missions == []
    assert conn.execute("SELECT status FROM missions").fetchone()["status"] == "failed"
    log = (tp / "a.log").read_text(encoding="utf-8")
    assert "mission_boundary_failed" in log
    assert "mission_finalize_conflict" not in log


# ---------------------------------------------------------------------
# 未知 timeframe の claimed 行でも `_format_signal_injection` は例外を
# 出さない: `freshness_window` が `KeyError` を送出する
# 行を `_TRADE_TOOLS` 経由で claim してしまうと、以前は例外がそのまま
# `run_once` の外側 finally まで抜け、requeue → 次 cron が同じ最古行を
# 再 claim → 再度例外、を繰り返した (claim_oldest 自身の鮮度条件は NULL
# 幅で常に偽になるため通常は claim され得ないが、鮮度ゲート実装が将来
# 変わった場合や移行後の残存データに対する防御として、formatter 自体は
# 未知 timeframe でも例外を出さない契約にする)。
# ---------------------------------------------------------------------
def test_format_signal_injection_unknown_timeframe_shows_placeholder(tmp_path):
    conn, loop, runner, _ = _loop_at(tmp_path, [_HOLD], THU_1400)
    claimed = {"id": 1, "plugin": "sma_cross_10_30", "kind": "strategy",
              "pair": "USDJPY", "timeframe": "9x",
              "bar_ts": "2026-09-24T13:00:00+00:00",
              "payload_json": '{"direction": "long"}'}

    section = loop._format_signal_injection(claimed, "cron")

    assert "- strategy_timeframe: 9x\n" in section
    assert "- bar_close: 不明\n" in section
    assert "- fresh_until: 不明\n" in section


def test_maintenance_abandons_oldest_unknown_timeframe_signal_then_cron_claims_valid_one(
        tmp_path):
    """最古 pending が未知 timeframe でも、maintenance
    (`service._run_signal_maintenance` 経由の `signals.expire_stale`) が先に
    それを abandoned へ落とし理由を activity に記録するので、同じ tick の
    cron mission は failed にならず、その次に古い正常な pending 行を claim
    して completed になる。"""
    import agentic_fx.service as service_mod

    conn, loop, runner, tp = _loop_at(tmp_path, [_HOLD], THU_1400)
    invalid_id = _add_at(conn, pair="USDJPY",
                         bar_ts=datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc))
    conn.execute("UPDATE signals SET timeframe='9x' WHERE id=?", (invalid_id,))
    conn.commit()
    valid_id = _add_at(conn, pair="USDJPY",
                       bar_ts=datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc))

    class _NoOpProducer:
        def evaluate_due_plugins(self, **k):
            pass

    service_mod._run_signal_maintenance(
        conn=conn, signal_producer=_NoOpProducer(), approved=[],
        settings=SETTINGS, now=THU_1400, resolved_by_identity={},
        activity=loop.activity)

    assert _signal_row(conn, invalid_id)["status"] == "abandoned"
    log_text = (tp / "a.log").read_text(encoding="utf-8")
    assert "signal_abandoned_invalid_timeframe" in log_text
    assert f"signal #{invalid_id}" in log_text

    out = loop.run_once("cron", decision_bars=_cron_bars(THU_1400))

    assert out["result"] == "hold"
    m = conn.execute("SELECT trigger, status FROM missions").fetchone()
    assert (m["trigger"], m["status"]) == ("cron", "completed")
    assert _signal_row(conn, invalid_id)["status"] == "abandoned"
    assert _signal_row(conn, valid_id)["status"] == "consumed"
