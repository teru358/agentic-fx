"""実測 warning (`signal_wait_budget_warning`) の評価。

注入時計で、直近 14 運用日の actor=llm の final decision から `eligible→claim` と
`claim→decision commit` の p95 を直接測る場面 (100 件以上) と、標本不足のときの代用式
(trade dispatch の p95 + R + Cw)、freshness 系 abandoned、設定 fingerprint × 運用日の
dedupe、評価時点 (起動時・運用日境界・承認集合変更・終端確定時) を通す。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core import mission_ceiling
from agentic_fx.signal_admission import (
    DIRECT_MIN_SAMPLES, WaitBudgetEvaluator, percentile95, read_wait_budget,
    settings_fingerprint, window_start,
)
from agentic_fx.store import alert_state, signals
from agentic_fx.store.db import connect, init_db

# 水曜 12:00 UTC (取引日の開始は火曜 21:00 UTC)
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example")
WARNING = "signal_wait_budget_warning"


def _strategy(timeframe="15m", name="s", content_hash="h"):
    return SimpleNamespace(kind="strategy", name=name, timeframe=timeframe,
                           pairs=("USDJPY",), content_hash=content_hash)


class Harness:
    def __init__(self, tmp_path, *, strategies=None, settings=SETTINGS, now=NOW):
        self.conn = connect(tmp_path / "w.db")
        init_db(self.conn)
        self.log = tmp_path / "act.log"
        self.strategies = [_strategy()] if strategies is None else strategies
        self.settings = settings
        self.now = now
        self.counter = 0
        self.evaluator = self.make()

    def make(self, **overrides):
        return WaitBudgetEvaluator(
            conn=overrides.get("conn", self.conn),
            settings=overrides.get("settings", self.settings),
            strategies_fn=lambda: self.strategies, activity=ActivityLog(self.log),
            now_fn=lambda: self.now,
            writer_admission=overrides.get("writer_admission"))

    def lines(self) -> list[str]:
        if not self.log.exists():
            return []
        return [l for l in self.log.read_text(encoding="utf-8").splitlines()
                if WARNING in l]

    def decision(self, claim_sec, decide_sec, *, actor="llm", decided=None):
        """created→claimed が claim_sec、claimed→decided が decide_sec の最終 decision。"""
        self.counter += 1
        created = (decided or self.now) - timedelta(
            seconds=claim_sec + decide_sec + 60)
        claimed = created + timedelta(seconds=claim_sec)
        decided_at = claimed + timedelta(seconds=decide_sec)
        bar = created.replace(microsecond=0) - timedelta(minutes=self.counter)
        sid = signals.add(
            self.conn, plugin="p", content_hash="h", pair="USDJPY", timeframe="15m",
            bar_ts=bar.isoformat(), kind="strategy",
            payload={"action": "open"}, now=created,
            contract_version="executable_v1")
        mid = self.conn.execute(
            "INSERT INTO missions (loop, runner, model, status, trigger, started_at, "
            "finished_at) VALUES ('trade','local','m','completed','signal:p',?,?)",
            (claimed.isoformat(), decided_at.isoformat())).lastrowid
        self.conn.execute(
            "UPDATE signals SET status='consumed', claimed_by_mission_id=?, "
            "claimed_at=? WHERE id=?", (mid, claimed.isoformat(), sid))
        reject = "news_shock" if actor == "llm" else "econ_window"
        self.conn.execute(
            "INSERT INTO signal_decisions (mission_id, signal_id, decision, actor, "
            "reject_code, decided_at) VALUES (?,?,'reject',?,?,?)",
            (mid, sid, actor, reject, decided_at.isoformat()))
        self.conn.commit()
        return sid

    def many(self, n, claim_sec, decide_sec, **kw):
        for _ in range(n):
            self.decision(claim_sec, decide_sec, **kw)

    def abandon(self, reason, *, at=None, n=1):
        for _ in range(n):
            self.counter += 1
            bar = NOW - timedelta(days=1, minutes=self.counter)
            sid = signals.add(
                self.conn, plugin="a", content_hash="h", pair="USDJPY",
                timeframe="15m", bar_ts=bar.isoformat(), kind="strategy",
                payload={"action": "open"}, now=bar, contract_version="executable_v1")
            self.conn.execute(
                "UPDATE signals SET status='abandoned', abandon_reason=?, "
                "abandoned_at=? WHERE id=?",
                (reason, (at or self.now).isoformat(), sid))
        self.conn.commit()

    def trade(self, start, end, *, status="completed", reflections=()):
        mid = self.conn.execute(
            "INSERT INTO missions (loop, runner, model, status, trigger, started_at, "
            "finished_at) VALUES ('trade','local','m',?, 'cron', ?, ?)",
            (status, start.isoformat(), end.isoformat() if end else None)).lastrowid
        for r_start, r_end in reflections:
            self.conn.execute(
                "INSERT INTO missions (loop, runner, model, status, started_at, "
                "finished_at) VALUES ('reflection','local','m','completed',?,?)",
                (r_start.isoformat(), r_end.isoformat()))
        self.conn.commit()
        return mid

    def evaluate(self, **kw):
        return self.evaluator.evaluate_wait_budget(self.now, **kw)


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.conn.close()


def _hours(base, h, m=0, s=0):
    return base.replace(hour=0, minute=0, second=0) + timedelta(hours=h, minutes=m, seconds=s)


# --- p95 と窓 ------------------------------------------------------------

def test_percentile_is_nearest_rank():
    assert percentile95([]) is None
    assert percentile95([7]) == 7
    ninety_four_low = [10.0] * 94 + [1000.0] * 6
    assert percentile95(ninety_four_low) == 1000.0       # 95 番目は大きい側
    ninety_five_low = [10.0] * 95 + [1000.0] * 5
    assert percentile95(ninety_five_low) == 10.0
    assert percentile95([3, 1, 2]) == 3                  # ceil(0.95 × 3) = 3 番目


def test_the_window_is_the_latest_14_operating_days_including_today():
    assert window_start(NOW) == datetime(2026, 7, 21, 21, 0, tzinfo=timezone.utc) \
        - timedelta(days=13)


# --- 直接観測 (100 件以上) ----------------------------------------------------

def test_no_approved_strategy_is_inactive_and_silent(tmp_path):
    h = Harness(tmp_path, strategies=[])
    snap = h.evaluate()
    assert snap.state == "inactive" and snap.basis is None
    assert h.lines() == []
    h.conn.close()


def test_direct_samples_below_the_budget_stay_ok(h):
    h.many(DIRECT_MIN_SAMPLES, 300, 200)                 # 500 < 余地 900
    snap = h.evaluate()
    assert (snap.state, snap.basis, snap.samples) == ("ok", "direct", 100)
    assert (snap.claim_p95_sec, snap.decision_p95_sec) == (300, 200)
    assert snap.estimate_sec == 500 and snap.budget_sec == 900
    assert h.lines() == []


@pytest.mark.parametrize("claim, decide, active", [
    (500, 400, True),        # 合計 900 = 余地: 以上で warning
    (500, 399, False),       # 899
    (899, 0, False),
    (0, 901, True),
])
def test_the_sum_of_the_two_p95_is_compared_with_the_freshness_room(
        tmp_path, claim, decide, active):
    h = Harness(tmp_path)
    h.many(DIRECT_MIN_SAMPLES, claim, decide)
    assert (h.evaluate().state == "active") is active
    assert len(h.lines()) == (1 if active else 0)
    h.conn.close()


def test_99_llm_decisions_use_the_proxy_and_100_use_the_direct_measurement(tmp_path):
    h = Harness(tmp_path)
    h.many(99, 100, 100)
    snap = h.evaluate()
    assert snap.basis == "proxy" and snap.direct_samples == 99
    h.decision(100, 100)
    assert h.evaluate().basis == "direct"
    h.conn.close()


def test_system_decisions_are_not_samples(tmp_path):
    h = Harness(tmp_path)
    h.many(99, 100, 100)
    h.many(50, 5000, 5000, actor="system")
    snap = h.evaluate()
    assert snap.basis == "proxy" and snap.direct_samples == 99
    h.decision(100, 100)
    snap = h.evaluate()
    assert snap.basis == "direct" and snap.estimate_sec == 200   # system の 10000 は無関係
    h.conn.close()


def test_decisions_older_than_14_operating_days_are_ignored(tmp_path):
    h = Harness(tmp_path)
    old = window_start(NOW) - timedelta(minutes=1)
    h.many(DIRECT_MIN_SAMPLES, 5000, 5000, decided=old)
    assert h.evaluate().direct_samples == 0
    h.many(DIRECT_MIN_SAMPLES, 10, 10, decided=window_start(NOW) + timedelta(minutes=1))
    assert h.evaluate().direct_samples == 100
    h.conn.close()


def test_the_start_of_the_window_is_included_for_decisions_too(tmp_path):
    h = Harness(tmp_path)
    start = window_start(NOW)
    margin = timedelta(seconds=60)            # decision() は decided_at を `decided` の 60 秒前に置く
    h.many(DIRECT_MIN_SAMPLES, 10, 10, decided=start + margin)
    assert h.evaluate().direct_samples == 100
    h.conn.close()
    (tmp_path / "late").mkdir()
    h2 = Harness(tmp_path / "late")
    h2.many(DIRECT_MIN_SAMPLES, 10, 10,
            decided=start + margin - timedelta(microseconds=1))
    assert h2.evaluate().direct_samples == 0
    h2.conn.close()


def test_the_budget_is_the_smallest_width_times_k_minus_one(tmp_path):
    h = Harness(tmp_path, strategies=[_strategy("1h"), _strategy("15m", name="b")])
    assert h.evaluate().budget_sec == 900
    h.strategies = [_strategy("1h")]
    assert h.evaluate().budget_sec == 3600
    h.settings = SETTINGS.model_copy(update={"plugin": SETTINGS.plugin.model_copy(
        update={"signal_freshness_bars": 3})})
    h.evaluator = h.make()
    assert h.evaluate().budget_sec == 7200
    h.conn.close()


# --- freshness 系 abandoned --------------------------------------------------

@pytest.mark.parametrize("reason, counts", [
    ("freshness_expired", True), ("requeued_expired", True),
    ("retry_exhausted", False), ("ineligible_cleanup", False),
    ("source_inactive", False), ("deploy_op", False)])
def test_only_freshness_class_abandoned_rows_activate_the_warning(tmp_path, reason, counts):
    h = Harness(tmp_path)
    h.many(DIRECT_MIN_SAMPLES, 10, 10)
    h.abandon(reason)
    snap = h.evaluate()
    assert (snap.state == "active") is counts
    assert snap.freshness_abandoned == (1 if counts else 0)
    assert snap.reasons == (("freshness_abandoned",) if counts else ())
    h.conn.close()


def test_a_freshness_abandon_outside_the_window_is_not_counted(tmp_path):
    h = Harness(tmp_path)
    h.many(DIRECT_MIN_SAMPLES, 10, 10)
    h.abandon("freshness_expired", at=window_start(NOW) - timedelta(seconds=1))
    assert h.evaluate().freshness_abandoned == 0
    h.abandon("freshness_expired", at=window_start(NOW))
    assert h.evaluate().freshness_abandoned == 1
    h.conn.close()


def test_freshness_abandoned_activates_even_with_too_few_decisions(tmp_path):
    h = Harness(tmp_path, strategies=[_strategy("1h")])
    h.abandon("freshness_expired")
    snap = h.evaluate()
    assert snap.state == "active" and snap.reasons == ("freshness_abandoned",)
    h.conn.close()


# --- 標本不足の代用式 ---------------------------------------------------------

def _cd_cw():
    return mission_ceiling.worker_ceiling(SETTINGS).total


def test_the_proxy_uses_trade_start_to_the_last_adjacent_reflection(tmp_path):
    h = Harness(tmp_path, strategies=[_strategy("1h")])
    day = NOW.replace(hour=0, minute=0)
    # 12:00-12:05 の trade に reflection 2 本が続く。dispatch は 12:00 から 12:12 まで
    h.trade(_hours(day, 8), _hours(day, 8, 5),
            reflections=[(_hours(day, 8, 5, 30), _hours(day, 8, 8)),
                         (_hours(day, 8, 8, 30), _hours(day, 8, 12))])
    # reflection の無い trade は自身の終了まで (3 分)
    h.trade(_hours(day, 9), _hours(day, 9, 3))
    snap = h.evaluate()
    assert snap.basis == "proxy" and snap.samples == 2
    assert snap.dispatch_p95_sec == 720                    # 2 件の p95 は大きい側
    rate = SETTINGS.plugin.signal_min_interval_min * 60
    assert snap.estimate_sec == 720 + rate + _cd_cw()
    assert snap.state == "ok" and snap.budget_sec == 3600
    h.conn.close()


def test_a_reflection_belongs_to_the_trade_it_follows_and_not_to_the_previous_one(tmp_path):
    h = Harness(tmp_path, strategies=[_strategy("1h")])
    day = NOW.replace(hour=0, minute=0)
    h.trade(_hours(day, 8), _hours(day, 8, 5))
    # 9:06 の reflection は 9:00 の trade の終了後に始まり、次の trade (9:30) より前
    h.trade(_hours(day, 9), _hours(day, 9, 5),
            reflections=[(_hours(day, 9, 6), _hours(day, 9, 20))])
    h.trade(_hours(day, 9, 30), _hours(day, 9, 33))
    seconds = h.evaluator._dispatch_seconds(window_start(NOW))
    assert seconds == [300.0, 1200.0, 180.0]               # 8:00 の trade は 5 分のまま
    h.conn.close()


def test_a_reflection_that_started_before_the_trade_finished_is_not_attached(tmp_path):
    h = Harness(tmp_path, strategies=[_strategy("1h")])
    day = NOW.replace(hour=0, minute=0)
    h.trade(_hours(day, 8), _hours(day, 8, 5),
            reflections=[(_hours(day, 7, 59), _hours(day, 9, 59))])
    assert h.evaluate().dispatch_p95_sec == 300
    h.conn.close()


def test_skipped_and_running_trades_are_not_samples_but_skipped_closes_the_previous_dispatch(
        tmp_path):
    h = Harness(tmp_path, strategies=[_strategy("1h")])
    day = NOW.replace(hour=0, minute=0)
    h.trade(_hours(day, 8), _hours(day, 8, 5))
    h.trade(_hours(day, 8, 10), _hours(day, 8, 10, 1), status="skipped",
            reflections=[(_hours(day, 8, 20), _hours(day, 8, 40))])
    h.trade(_hours(day, 9), None, status="running")
    snap = h.evaluate()
    assert snap.samples == 1 and snap.dispatch_p95_sec == 300
    h.conn.close()


def test_a_long_dispatch_activates_the_proxy_warning(tmp_path):
    h = Harness(tmp_path, strategies=[_strategy("1h")])
    day = NOW.replace(hour=0, minute=0)
    rate = SETTINGS.plugin.signal_min_interval_min * 60
    need = 3600 - rate - _cd_cw()                         # ちょうどこの dispatch で余地に届く
    h.trade(_hours(day, 8), _hours(day, 8) + timedelta(seconds=need))
    snap = h.evaluate()
    assert snap.estimate_sec == 3600 and snap.state == "active"
    assert snap.reasons == ("latency",)
    h.conn.close()


def test_the_proxy_just_below_the_room_is_ok(tmp_path):
    h = Harness(tmp_path, strategies=[_strategy("1h")])
    day = NOW.replace(hour=0, minute=0)
    rate = SETTINGS.plugin.signal_min_interval_min * 60
    need = 3600 - rate - _cd_cw()
    h.trade(_hours(day, 8), _hours(day, 8) + timedelta(seconds=need - 1))
    assert h.evaluate().state == "ok"
    h.conn.close()


def test_without_any_trade_the_proxy_has_no_estimate_and_stays_ok(tmp_path):
    h = Harness(tmp_path, strategies=[_strategy("1h")])
    snap = h.evaluate()
    assert (snap.state, snap.basis, snap.samples, snap.estimate_sec) == (
        "ok", "proxy", 0, None)
    h.conn.close()


# --- dedupe -----------------------------------------------------------------

def _active(tmp_path, **kw):
    h = Harness(tmp_path, **kw)
    h.many(DIRECT_MIN_SAMPLES, 600, 600)
    return h


def test_the_warning_is_written_once_per_fingerprint_and_operating_day(tmp_path):
    h = _active(tmp_path)
    for _ in range(3):
        assert h.evaluate().state == "active"
    assert len(h.lines()) == 1
    h.conn.close()


def test_a_restart_does_not_repeat_the_warning(tmp_path):
    h = _active(tmp_path)
    h.evaluate()
    h.evaluator = h.make()                                  # 新しい評価器 (再起動)
    h.evaluate()
    assert len(h.lines()) == 1
    h.conn.close()


def test_the_next_operating_day_notifies_once_more(tmp_path):
    h = _active(tmp_path)
    h.evaluate()
    h.now = NOW + timedelta(hours=5)                        # 次の境界は水曜 21:00 UTC
    h.evaluate()
    assert len(h.lines()) == 1                              # 同じ運用日
    h.now = NOW + timedelta(hours=9, minutes=1)             # 水曜 21:01 UTC
    h.evaluate()
    assert len(h.lines()) == 2
    h.evaluate()
    assert len(h.lines()) == 2
    h.conn.close()


def test_a_changed_setting_fingerprint_notifies_again_within_the_day(tmp_path):
    h = _active(tmp_path)
    h.evaluate()
    h.settings = SETTINGS.model_copy(update={"plugin": SETTINGS.plugin.model_copy(
        update={"signal_min_interval_min": 5})})
    h.evaluator = h.make()
    h.evaluate()
    assert len(h.lines()) == 2
    h.conn.close()


def _with_rate(minutes):
    return SETTINGS.model_copy(update={"plugin": SETTINGS.plugin.model_copy(
        update={"signal_min_interval_min": minutes})})


def test_returning_to_an_earlier_setting_within_the_day_does_not_notify_again(tmp_path):
    """A → B → A: 当日に通知済みの (fingerprint, 運用日) の組は、戻っても再通知しない。"""
    h = _active(tmp_path)
    h.evaluate()
    assert len(h.lines()) == 1                              # A
    h.settings = _with_rate(5)
    h.evaluator = h.make()
    h.evaluate()
    assert len(h.lines()) == 2                              # B
    h.settings = SETTINGS
    h.evaluator = h.make()
    h.evaluate()
    h.evaluate()
    assert len(h.lines()) == 2                              # A に戻っても増えない
    h.conn.close()


def test_each_notified_pair_is_its_own_durable_key_and_old_days_are_dropped(tmp_path):
    h = _active(tmp_path)
    h.evaluate()
    h.settings = _with_rate(5)
    h.evaluator = h.make()
    h.evaluate()
    prefix = alert_state.WAIT_BUDGET_NOTIFIED_PREFIX
    keys = alert_state.keys_with_prefix(h.conn, prefix)
    assert len(keys) == 2 and all("|" in k for k in keys)
    h.now = NOW + timedelta(hours=9, minutes=1)             # 次の運用日
    h.evaluate()
    after = alert_state.keys_with_prefix(h.conn, prefix)
    assert len(after) == 1 and after[0] not in keys         # 古い日の key は消える
    h.conn.close()


def test_the_fingerprint_depends_on_each_value_the_judgement_uses():
    base = dict(width_sec=900, freshness_bars=2, rate_sec=600.0, worker_sec=405.0)
    seen = {settings_fingerprint(**base)}
    for key, value in (("width_sec", 3600), ("freshness_bars", 3),
                       ("rate_sec", 300.0), ("worker_sec", 500.0)):
        seen.add(settings_fingerprint(**{**base, key: value}))
    assert len(seen) == 5
    assert settings_fingerprint(**base) == settings_fingerprint(**base)


def test_a_change_of_the_smallest_strategy_width_changes_the_key(tmp_path):
    h = _active(tmp_path, strategies=[_strategy("15m")])
    h.evaluate()
    h.strategies = [_strategy("15m"), _strategy("5m", name="fast")]
    h.evaluate()
    assert len(h.lines()) == 2
    h.conn.close()


def test_the_note_is_not_written_when_the_state_cannot_be_saved(tmp_path):
    h = _active(tmp_path)
    h.conn.execute("DROP TABLE alert_state")
    h.conn.commit()
    assert h.evaluate().state == "active"                   # 保存に失敗しても評価は返す
    assert h.lines() == []                                  # 記録できなければ書かない
    h.conn.close()


# --- 書込み要求との調整 -------------------------------------------------------

def test_an_evaluation_does_not_begin_a_transaction_while_the_scheduler_has_requested(
        tmp_path):
    """評価器の書込み (conn_admission) も admit を通る。要求中は BEGIN を出さず、
    何も記録せず通知もしない。次の tick で取り直す。"""
    from agentic_fx.core.writer_admission import WriterAdmission
    writer = WriterAdmission()
    h = _active(tmp_path)
    h.evaluator = h.make(writer_admission=writer)
    began = []
    h.conn.set_trace_callback(lambda sql: began.append(sql) if "BEGIN" in sql else None)
    writer.request()
    try:
        snap = h.evaluate()
        assert snap.state == "active"                       # 評価結果は返す
        assert began == []                                  # BEGIN を出していない
        assert alert_state.get(h.conn, alert_state.WAIT_BUDGET_SNAPSHOT_KEY) is None
        assert h.lines() == []
        h.evaluator.tick(NOW)                               # 要求中の tick も何も書かない
        assert h.lines() == []
    finally:
        writer.release()
    again = h.evaluator.tick(NOW)                           # 要求が下りたあとの tick
    assert again is not None and again.state == "active"
    assert len(h.lines()) == 1
    assert [q for q in began if "BEGIN IMMEDIATE" in q]     # 今度は admit を通って始まる
    assert writer.stats().yielded >= 1 and writer.stats().admitted == 1
    h.conn.close()


def test_a_tick_does_not_reevaluate_after_a_successful_admitted_write(tmp_path):
    from agentic_fx.core.writer_admission import WriterAdmission
    writer = WriterAdmission()
    h = _active(tmp_path)
    h.evaluator = h.make(writer_admission=writer)
    assert h.evaluator.tick(NOW) is not None
    assert h.evaluator.tick(NOW) is None
    assert writer.stats().admitted == 1
    h.conn.close()


# --- 評価時点 ----------------------------------------------------------------

def test_a_final_decision_or_a_freshness_abandon_triggers_an_evaluation(tmp_path):
    h = _active(tmp_path)
    assert h.evaluator.on_signal_terminal(1, "decision").trigger == "decision"
    assert h.evaluator.on_signal_terminal(None, "freshness_abandoned").trigger == (
        "freshness_abandoned")
    assert h.evaluator.on_signal_terminal(1, "consumed") is None
    assert h.evaluator.on_signal_terminal(1, "") is None
    assert len(h.lines()) == 1
    h.conn.close()


def test_tick_evaluates_only_at_a_new_operating_day_or_a_changed_strategy_set(tmp_path):
    h = _active(tmp_path)
    h.evaluate(trigger="startup")
    assert h.evaluator.tick(h.now + timedelta(minutes=1)) is None
    snap = h.evaluator.tick(h.now + timedelta(hours=9, minutes=1))
    assert snap is not None and snap.trigger == "boundary"
    h.now = h.now + timedelta(hours=9, minutes=1)
    assert h.evaluator.tick(h.now + timedelta(minutes=1)) is None
    h.strategies = [_strategy("15m"), _strategy("1h", name="b")]
    assert h.evaluator.tick(h.now + timedelta(minutes=2)).trigger == "boundary"
    assert h.evaluator.tick(h.now + timedelta(minutes=3)) is None
    h.conn.close()


def test_the_first_tick_of_a_fresh_evaluator_evaluates(tmp_path):
    h = _active(tmp_path)
    assert h.evaluator.tick(NOW) is not None
    h.conn.close()


def test_evaluation_failures_are_swallowed(tmp_path):
    h = _active(tmp_path)
    broken = sqlite3.connect(":memory:")
    evaluator = h.make(conn=broken)
    assert evaluator.evaluate_wait_budget(NOW) is None
    assert evaluator.on_signal_terminal(1, "decision") is None
    assert evaluator.tick(NOW) is None
    h.conn.close()


def test_evaluation_runs_on_another_thread_while_the_core_lock_is_held(tmp_path):
    """評価は自分の接続と lock だけを使う。core lock を持つ thread がいても待たない。"""
    h = _active(tmp_path)
    core_lock = threading.RLock()
    out = {}
    with core_lock:
        worker = threading.Thread(target=lambda: out.setdefault(
            "snap", h.evaluator.on_signal_terminal(1, "decision")))
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive()
    assert out["snap"].state == "active"
    h.conn.close()


# --- status 用の保存 ----------------------------------------------------------

def test_the_snapshot_and_the_last_notice_time_are_saved_for_status(tmp_path):
    h = _active(tmp_path)
    assert read_wait_budget(h.conn) is None
    h.evaluate()
    data = read_wait_budget(h.conn)
    assert data["state"] == "active" and data["basis"] == "direct"
    assert data["samples"] == 100 and data["estimate_sec"] == 1200
    assert data["freshness_abandoned"] == 0 and data["reasons"] == ["latency"]
    assert data["last_notified_at"] == NOW.isoformat()
    assert json.loads(alert_state.get(h.conn, alert_state.WAIT_BUDGET_SNAPSHOT_KEY))[
        "fingerprint"] == data["fingerprint"]
    h.conn.close()


def test_an_ok_evaluation_has_no_last_notice(tmp_path):
    h = Harness(tmp_path)
    h.many(DIRECT_MIN_SAMPLES, 1, 1)
    h.evaluate()
    assert read_wait_budget(h.conn)["last_notified_at"] is None
    h.conn.close()


def test_the_warning_line_does_not_leak_decision_content(tmp_path):
    h = _active(tmp_path)
    h.evaluate()
    line = h.lines()[0]
    assert "news_shock" not in line and "pnl" not in line
