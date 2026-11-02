"""signal 専用 mission の起動条件・静的保証・容量比較・calendar 鮮度・status の配線。"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.econ_window import WindowSettings
from agentic_fx.core.scheduler import TICK_INTERVAL_SEC
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import (
    STARTUP_CHECKS, StartupContext, _run_signal_maintenance, _scheduler_tick_once,
    _startup_signal_capacity, build_app,
)
from agentic_fx.setup.preflight import FATAL_CODES, PreflightFailure
from agentic_fx.signal_admission import (
    CalendarFreshnessNotice, CapacityMonitor, DeployedStrategies, static_guarantee,
    signal_due,
)
from agentic_fx.store import alert_state, econ_events, fetch_attempts, missions, orders, signals
from agentic_fx.store.db import connect, init_db
from tests.store.test_rag import FakeEmbedding
from tests.test_service_app import _init

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example")


def _with(settings, section=None, **updates):
    if section is None:
        return settings.model_copy(update=updates)
    inner = getattr(settings, section).model_copy(update=updates)
    return settings.model_copy(update={section: inner})


def _strategy(timeframe="15m", name="s", pairs=("USDJPY",), content_hash="h"):
    return SimpleNamespace(kind="strategy", name=name, timeframe=timeframe,
                           pairs=tuple(pairs), content_hash=content_hash)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    yield c
    c.close()


def _signal(conn, *, bar_ts=NOW - timedelta(hours=1), kind="strategy",
            contract_version="executable_v1", timeframe="1h", plugin="p"):
    return signals.add(
        conn, plugin=plugin, content_hash="h1", pair="USDJPY", timeframe=timeframe,
        bar_ts=bar_ts.isoformat(), kind=kind,
        payload={"action": "open", "direction": "long", "entry_type": "market",
                 "stop_loss": 147.8, "take_profit": 149.0, "horizon": "day"},
        now=bar_ts, contract_version=contract_version)


def _position(conn, status="open"):
    return orders.insert(conn, pair="USDJPY", direction="long", entry_type="market",
                         horizon="swing", status=status, now=NOW, quantity=0.1,
                         avg_fill_price=148.2, stop_loss=147.8, take_profit=149.0)


# --- 起動条件 ------------------------------------------------------------

def test_a_flat_book_with_an_eligible_signal_is_due(conn):
    _signal(conn)
    assert orders.list_by_status(conn, "open", "pending_fill") == []
    assert signal_due(conn, SETTINGS, NOW) is True


@pytest.mark.parametrize("status", ["open", "pending_fill"])
def test_an_open_position_or_pending_order_does_not_change_the_answer(conn, status):
    _signal(conn)
    _position(conn, status)
    assert signal_due(conn, SETTINGS, NOW) is True


def test_nothing_pending_is_not_due(conn):
    assert signal_due(conn, SETTINGS, NOW) is False


def test_only_ineligible_rows_are_not_due(conn):
    _signal(conn, kind="signal", contract_version=None)
    _signal(conn, kind="strategy", contract_version=None, plugin="legacy")
    assert signal_due(conn, SETTINGS, NOW) is False


def test_a_mix_is_due_for_the_eligible_row(conn):
    _signal(conn, kind="signal", contract_version=None)
    _signal(conn, plugin="good")
    assert signal_due(conn, SETTINGS, NOW) is True


def test_a_signal_past_its_freshness_end_is_not_due(conn):
    # 1h 足 × freshness 2 本: bar_ts + 2h が鮮度の終点
    _signal(conn, bar_ts=NOW - timedelta(hours=2, minutes=1))
    assert signal_due(conn, SETTINGS, NOW) is False
    _signal(conn, bar_ts=NOW - timedelta(hours=1, minutes=59), plugin="fresh")
    assert signal_due(conn, SETTINGS, NOW) is True


def test_a_claimed_signal_is_not_due(conn):
    sid = _signal(conn)
    mid = missions.start(conn, "trade", "local", "m", NOW, trigger="signal")
    signals.claim_oldest(conn, mission_id=mid, now=NOW,
                         freshness_bars=SETTINGS.plugin.signal_freshness_bars)
    assert signals.pending_exists(conn, now=NOW, freshness_bars=2) is False
    assert signal_due(conn, SETTINGS, NOW) is False
    assert sid is not None


def _signal_mission(conn, started, *, status="completed", trigger="signal:p"):
    mid = missions.start(conn, "trade", "local", "m", started, trigger=trigger)
    if status == "system_filtered":
        missions.finish_system_filtered_in_tx(conn, mid, None, [], started)
        conn.commit()
    else:
        missions.finish(conn, mid, status, None, [], started)
    return mid


def test_the_minimum_interval_applies_to_llm_and_skipped_missions(conn):
    _signal(conn)
    _signal_mission(conn, NOW - timedelta(minutes=5))
    assert signal_due(conn, SETTINGS, NOW) is False
    assert signal_due(conn, SETTINGS, NOW + timedelta(minutes=6)) is True


def test_a_skipped_mission_counts_toward_the_rate(conn):
    _signal(conn)
    _signal_mission(conn, NOW - timedelta(minutes=1), status="skipped", trigger="signal")
    assert signal_due(conn, SETTINGS, NOW) is False


def test_a_system_filtered_mission_is_not_counted_so_the_next_signal_starts_at_once(conn):
    _signal(conn)
    _signal_mission(conn, NOW - timedelta(minutes=1), status="system_filtered")
    assert signal_due(conn, SETTINGS, NOW) is True


def test_the_daily_limit_counts_llm_missions_only(conn):
    settings = _with(SETTINGS, "plugin", signal_daily_max=2)
    _signal(conn)
    day = NOW.replace(hour=1)
    _signal_mission(conn, day, status="system_filtered")
    _signal_mission(conn, day + timedelta(minutes=20), status="system_filtered")
    _signal_mission(conn, day + timedelta(minutes=40))
    assert signal_due(conn, settings, NOW) is True
    _signal_mission(conn, day + timedelta(minutes=60))
    assert signal_due(conn, settings, NOW) is False


# --- 静的保証 ------------------------------------------------------------

def test_the_example_settings_cannot_guarantee_a_15m_signal():
    g = static_guarantee(SETTINGS, [_strategy("15m")])
    assert g.unavailable is True and g.status is True
    assert g.dispatch_sec == 1630 and g.tick_sec == TICK_INTERVAL_SEC == 60
    assert g.rate_sec == 600 and g.worker_sec == 405 and g.plugin_lock_sec == 2
    assert g.total_sec == 1630 + 3 * 60 + 600 + 405 + 2 == 2817
    assert g.budget_sec == 900 and g.min_width_sec == 900
    assert (g.by_budget, g.by_decision_width) == (True, False)


def test_no_approved_strategy_is_not_applicable():
    g = static_guarantee(SETTINGS, [])
    assert g.unavailable is None and g.status == "n/a"
    assert g.to_dict()["worst_case_guarantee_unavailable"] == "n/a"
    # strategy でない plugin だけでも n/a
    assert static_guarantee(SETTINGS, [SimpleNamespace(
        kind="indicator", timeframe="15m", name="i", pairs=(), content_hash="x")]
    ).status == "n/a"


def test_a_1h_strategy_fits_the_example_worst_case():
    g = static_guarantee(SETTINGS, [_strategy("1h")])
    assert g.budget_sec == 3600 and g.unavailable is False
    assert (g.by_budget, g.by_decision_width) == (False, False)


def test_the_smallest_width_of_the_approved_strategies_is_used():
    g = static_guarantee(SETTINGS, [_strategy("1h"), _strategy("15m", name="b")])
    assert g.min_width_sec == 900 and g.unavailable is True


def test_the_budget_comparison_includes_the_boundary():
    # 2817 + 783 = 3600 = 1h × (k-1): ちょうど等しければ保証不能
    on = _with(SETTINGS, "worker", plugin_lock_timeout_sec=785.0)
    off = _with(SETTINGS, "worker", plugin_lock_timeout_sec=784.5)
    assert static_guarantee(on, [_strategy("1h")]).unavailable is True
    assert static_guarantee(off, [_strategy("1h")]).unavailable is False


@pytest.mark.parametrize("section, field, value", [
    ("plugin", "signal_min_interval_min", 23),     # R +780 → 3597 < 3600
    ("worker", "plugin_lock_timeout_sec", 784.5),  # Pl +782.5
])
def test_each_term_is_added_once(section, field, value):
    settings = _with(SETTINGS, section, **{field: value})
    g = static_guarantee(settings, [_strategy("1h")])
    assert g.unavailable is False and 3597 <= g.total_sec < 3600


def test_the_freshness_bar_count_scales_the_budget():
    three = _with(SETTINGS, "plugin", signal_freshness_bars=3)
    g = static_guarantee(three, [_strategy("1h")])
    assert g.budget_sec == 7200 and g.unavailable is False
    one = _with(SETTINGS, "plugin", signal_freshness_bars=1)
    assert static_guarantee(one, [_strategy("1h")]).budget_sec == 0


def test_the_decision_width_branch_applies_without_the_budget_branch():
    # Cd + T = 1690 秒。判断足 15m (900 秒) 以下なら、strategy が 1h でも保証不能
    quarter = SETTINGS.model_copy(update={"datafeed": SETTINGS.datafeed.model_copy(
        update={"decision_timeframes": ["15m"]})})
    g = static_guarantee(quarter, [_strategy("1h")])
    assert (g.by_budget, g.by_decision_width, g.unavailable) == (False, True, True)
    assert g.decision_width_sec == 900


def test_the_decision_width_comparison_includes_the_boundary():
    # 判断足 1h = 3600 秒。Cd + T がちょうど 3600 になる timeout_sec を探して前後を比べる
    base = static_guarantee(SETTINGS, [_strategy("1d")])
    assert base.by_decision_width is False
    gap = 3600 - (base.dispatch_sec + base.tick_sec)       # 4 × Cw の増分で埋める
    on = SETTINGS.model_copy(update={"llama_swap": SETTINGS.llama_swap.model_copy(
        update={"timeout_sec": SETTINGS.llama_swap.timeout_sec + gap / 4})})
    off = SETTINGS.model_copy(update={"llama_swap": SETTINGS.llama_swap.model_copy(
        update={"timeout_sec": SETTINGS.llama_swap.timeout_sec + gap / 4 - 0.25})})
    g_on, g_off = static_guarantee(on, [_strategy("1d")]), static_guarantee(
        off, [_strategy("1d")])
    assert g_on.dispatch_sec + g_on.tick_sec == 3600
    assert (g_on.by_decision_width, g_off.by_decision_width) == (True, False)
    assert (g_on.by_budget, g_off.by_budget) == (False, False)   # 1d × 1 = 86400 秒


# --- S と S_producer -------------------------------------------------------

def _ctx(plugins):
    return StartupContext(SimpleNamespace(write=lambda *a, **k: None), list(plugins))


def test_the_capacity_check_is_part_of_the_startup_checks_after_plugin_retention():
    names = [c.__name__ for c in STARTUP_CHECKS]
    assert names.index("_startup_signal_capacity") > names.index(
        "_startup_plugin_retention")


def test_an_override_below_the_producer_maximum_refuses_to_start():
    # 15m × 1 pair = 96 件 / 日
    low = SETTINGS.model_copy(update={"econ": SETTINGS.econ.model_copy(
        update={"signal_capacity_daily_max": 95})})
    with pytest.raises(PreflightFailure) as caught:
        _startup_signal_capacity(low, _ctx([_strategy("15m")]))
    assert caught.value.code == "signal_capacity_override_below_producer"
    assert "signal_capacity_override_below_producer" in FATAL_CODES


def test_an_override_equal_to_or_above_the_producer_maximum_starts():
    for value in (96, 500):
        ok = SETTINGS.model_copy(update={"econ": SETTINGS.econ.model_copy(
            update={"signal_capacity_daily_max": value})})
        assert _startup_signal_capacity(ok, _ctx([_strategy("15m")])) is ok
    assert _startup_signal_capacity(SETTINGS, _ctx([_strategy("15m")])) is SETTINGS


def test_the_capacity_monitor_reads_the_deployed_set_each_time():
    approved = [_strategy("1h")]                       # 24 件 / 日
    settings = SETTINGS.model_copy(update={"econ": SETTINGS.econ.model_copy(
        update={"signal_capacity_daily_max": 30})})
    monitor = CapacityMonitor(settings, lambda: approved)
    monitor.check_startup()                            # 30 ≥ 24: 起動できる
    assert monitor.snapshot() == {
        "capacity_override_below_producer": False, "signal_daily": 30,
        "producer_daily": 24, "override": 30}
    approved.append(_strategy("15m", name="more"))     # 24 + 96 = 120 件 / 日
    snap = monitor.snapshot()
    assert snap["capacity_override_below_producer"] is True
    assert snap["signal_daily"] == 120 and snap["producer_daily"] == 120
    approved.clear()
    assert monitor.snapshot()["capacity_override_below_producer"] is False


class _Loader:
    def __init__(self, *sets):
        self.sets = list(sets)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        value = self.sets[min(self.calls, len(self.sets)) - 1]
        if isinstance(value, Exception):
            raise value
        return value


def test_the_deployed_set_is_reread_only_at_a_new_day_or_a_changed_version():
    loader, version = _Loader([_strategy("15m")]), ["v1"]
    deployed = DeployedStrategies([_strategy("1h")], load_fn=loader,
                                  version_fn=lambda: version[0])
    assert [m.timeframe for m in deployed.current()] == ["1h"]      # 起動時の集合
    assert loader.calls == 0
    assert deployed.refresh(NOW) is True and loader.calls == 1       # 初回
    assert [m.timeframe for m in deployed.current()] == ["15m"]
    assert deployed.refresh(NOW + timedelta(minutes=5)) is False
    assert loader.calls == 1                                         # 同じ日・同じ版
    version[0] = "v2"                                                # deploy op が動いた
    assert deployed.refresh(NOW + timedelta(minutes=6)) is True
    assert loader.calls == 2
    assert deployed.refresh(NOW + timedelta(hours=9, minutes=1)) is True   # 運用日境界
    assert loader.calls == 3


def test_a_failed_reread_keeps_the_previous_set_and_is_retried():
    loader = _Loader(RuntimeError("inventory unreadable"), [_strategy("15m")])
    deployed = DeployedStrategies([_strategy("1h")], load_fn=loader,
                                  version_fn=lambda: "v1")
    assert deployed.refresh(NOW) is False
    assert [m.timeframe for m in deployed.current()] == ["1h"]
    assert deployed.refresh(NOW + timedelta(minutes=1)) is True
    assert [m.timeframe for m in deployed.current()] == ["15m"]


def test_a_persistent_failure_backs_off_exponentially_up_to_fifteen_minutes():
    loader = _Loader(RuntimeError("hash mismatch"))
    deployed = DeployedStrategies([_strategy("1h")], load_fn=loader,
                                  version_fn=lambda: "v1")
    at, waits = NOW, []
    for _ in range(7):
        calls = loader.calls
        assert deployed.refresh(at) is False                 # 失敗する
        assert loader.calls == calls + 1
        wait = (deployed._retry_at - at).total_seconds()
        waits.append(wait)
        # 間隔の手前では読み直さず、間隔ちょうどで読み直す
        assert deployed.refresh(at + timedelta(seconds=wait - 1)) is False
        assert loader.calls == calls + 1
        at += timedelta(seconds=wait)
    assert waits == [60, 120, 240, 480, 900, 900, 900]


def test_the_refresh_warning_is_not_repeated_for_the_same_error(caplog):
    loader = _Loader(RuntimeError("hash mismatch"))
    deployed = DeployedStrategies([_strategy("1h")], load_fn=loader,
                                  version_fn=lambda: "v1")
    at = NOW
    with caplog.at_level("WARNING"):
        for _ in range(4):
            deployed.refresh(at)
            at += timedelta(minutes=20)
    assert loader.calls == 4
    assert len([r for r in caplog.records
                if "deployed strategies refresh failed" in r.getMessage()]) == 1


def test_a_success_resets_the_backoff():
    loader = _Loader(RuntimeError("busy"), [_strategy("15m")], RuntimeError("again"))
    version = ["v1"]
    deployed = DeployedStrategies([_strategy("1h")], load_fn=loader,
                                  version_fn=lambda: version[0])
    assert deployed.refresh(NOW) is False
    assert deployed.refresh(NOW + timedelta(seconds=59)) is False
    assert loader.calls == 1                                  # 間隔の手前
    assert deployed.refresh(NOW + timedelta(seconds=60)) is True
    version[0] = "v2"
    t = NOW + timedelta(seconds=61)
    assert deployed.refresh(t) is False                       # 失敗。間隔は 60 秒から
    assert (deployed._retry_at - t).total_seconds() == 60


def test_a_failing_version_probe_leaves_the_set_alone():
    def broken():
        raise RuntimeError("db busy")

    deployed = DeployedStrategies([_strategy("1h")], load_fn=lambda: [],
                                  version_fn=broken)
    assert deployed.refresh(NOW) is False
    assert len(deployed.current()) == 1


def test_a_deploy_op_while_running_lifts_s_to_the_producer_through_the_real_path(
        tmp_path, monkeypatch):
    """承認集合の正本を実行時に読む: deploy op の行が増えると評価器の tick が配備済み
    集合を取り直し、status の S と `capacity_override_below_producer` が切り替わる。"""
    from agentic_fx import service
    from tests.store.test_plugin_deploy_ops import _insert_intent
    _init(tmp_path)
    cfg = tmp_path / "config" / "settings.yaml"
    text = cfg.read_text(encoding="utf-8")
    assert "  # signal_capacity_daily_max:" in text
    cfg.write_text(text.replace("  # signal_capacity_daily_max:",
                                "  signal_capacity_daily_max: 30  #", 1),
                   encoding="utf-8")
    (tmp_path / "plugins").mkdir()
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                    embedding_fn=FakeEmbedding())
    app.scheduler.on_improve_tick = None
    deployed_now = [_strategy("1h", name="a")]            # 24 件 / 日
    loads = []

    def fake_inventory(conn, root, *, settings):
        # status の組立も同じ関数を呼ぶ。配備済み集合の取り直しだけを数える
        if sys._getframe(1).f_code.co_name == "_load_deployed":
            loads.append(1)
        return SimpleNamespace(inventory=SimpleNamespace(metas=list(deployed_now)))

    try:
        with monkeypatch.context() as patch:
            patch.setattr(service.plugin_loader, "approved_plugins", fake_inventory)
            app.scheduler.state_fn = lambda: "ready"
            hook = app.scheduler.on_signal_maintenance
            hook(NOW + timedelta(minutes=1))                  # 初回: 運用日の読み
            assert loads == [1]
            status = app.ops._status_provider()
            assert status["capacity_override_below_producer"] is False
            assert status["signal_capacity"]["producer"] == 24
            deployed_now.append(_strategy("15m", name="b"))   # 24 + 96 = 120 件 / 日
            hook(NOW + timedelta(minutes=2))                  # op の行は未追加: 読まない
            assert loads == [1]
            assert app.ops._status_provider()[
                "capacity_override_below_producer"] is False
            op_id = _insert_intent(app.conn_core)             # deploy op が動いた
            hook(NOW + timedelta(minutes=3))
            assert loads == [1, 1]
            status = app.ops._status_provider()
            assert status["capacity_override_below_producer"] is True
            assert status["signal_capacity"] == {
                "daily": 120, "producer": 120, "override": 30}
            deployed_now.pop()                                # op の完了で集合が戻る
            from agentic_fx.store import plugin_deploy_ops
            plugin_deploy_ops.set_phase(app.conn_core, op_id, "done",
                                        now=NOW + timedelta(minutes=4),
                                        fail_reason=None)
            hook(NOW + timedelta(minutes=5))
            assert app.ops._status_provider()[
                "capacity_override_below_producer"] is False
    finally:
        app.close()


# --- calendar の鮮度 activity ------------------------------------------------

class _Seq:
    def __init__(self, values):
        self.values = list(values)

    def __call__(self, now):
        return self.values.pop(0)


def _notice(conn, tmp_path, states):
    log = tmp_path / "act.log"
    notice = CalendarFreshnessNotice(
        conn=conn, freshness_fn=_Seq(states), activity=ActivityLog(log))
    return notice, log


def _lines(log, word):
    return [l for l in log.read_text(encoding="utf-8").splitlines() if word in l] \
        if log.exists() else []


def test_calendar_activity_is_written_only_when_the_state_changes(conn, tmp_path):
    notice, log = _notice(conn, tmp_path, [
        "unknown", "fresh", "fresh", "stale", "stale", "stale", "fresh", "fresh"])
    results = [notice.observe(NOW + timedelta(minutes=i)) for i in range(8)]
    assert results == [None, None, None, "econ_calendar_stale", None, None,
                       "econ_calendar_fresh", None]
    assert len(_lines(log, "econ_calendar_stale")) == 1
    assert len(_lines(log, "econ_calendar_fresh")) == 1


def test_unknown_is_never_announced_and_does_not_reset_the_state(conn, tmp_path):
    notice, log = _notice(conn, tmp_path, ["stale", "unknown", "stale", "fresh"])
    for i in range(4):
        notice.observe(NOW + timedelta(minutes=i))
    assert len(_lines(log, "econ_calendar_stale")) == 1
    assert len(_lines(log, "econ_calendar_fresh")) == 1


def test_a_restart_does_not_repeat_the_stale_line(conn, tmp_path):
    first, log = _notice(conn, tmp_path, ["stale"])
    first.observe(NOW)
    second = CalendarFreshnessNotice(
        conn=conn, freshness_fn=_Seq(["stale", "fresh"]), activity=ActivityLog(log))
    assert second.observe(NOW + timedelta(minutes=1)) is None
    assert second.observe(NOW + timedelta(minutes=2)) == "econ_calendar_fresh"
    assert len(_lines(log, "econ_calendar_stale")) == 1


def test_fresh_without_a_stale_history_is_silent(conn, tmp_path):
    notice, log = _notice(conn, tmp_path, ["fresh", "fresh"])
    assert notice.observe(NOW) is None and notice.observe(NOW) is None
    assert not _lines(log, "econ_calendar")


def test_the_real_freshness_drives_stale_and_recovery(conn, tmp_path):
    log = tmp_path / "act.log"
    notice = CalendarFreshnessNotice(
        conn=conn, activity=ActivityLog(log),
        freshness_fn=lambda now: fetch_attempts.freshness(
            conn, fetch_attempts.ECON_KEY, now))
    fetch_attempts.record_outcome(conn, fetch_attempts.ECON_KEY, NOW, ok=True)
    assert notice.observe(NOW + timedelta(hours=1)) is None            # fresh
    assert notice.observe(NOW + timedelta(hours=12)) is None           # ちょうど 12h は fresh
    assert notice.observe(NOW + timedelta(hours=12, seconds=1)) == "econ_calendar_stale"
    fetch_attempts.record_outcome(conn, fetch_attempts.ECON_KEY,
                                  NOW + timedelta(hours=13), ok=True)
    assert notice.observe(NOW + timedelta(hours=13)) == "econ_calendar_fresh"


# --- build_app の配線 -------------------------------------------------------

def _app(tmp_path):
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                    embedding_fn=FakeEmbedding())
    app.scheduler.on_improve_tick = None
    return app


def test_build_app_hands_the_window_and_the_writer_flag_to_the_scheduler(tmp_path):
    app = _app(tmp_path)
    try:
        sched = app.scheduler
        assert sched.econ_window == WindowSettings.from_config(app.settings.econ.window)
        assert sched.pair_currencies == {"USDJPY": ("USD", "JPY")}
        assert app.writer_admission is sched.writer_admission is not None
        assert app.admission is not None
        assert sched.on_calendar_observe is not None
    finally:
        app.close()


def test_the_wired_signal_due_fn_ignores_the_book(tmp_path):
    app = _app(tmp_path)
    try:
        _signal(app.conn_core, bar_ts=NOW - timedelta(hours=1))
        assert orders.list_by_status(app.conn_core, "open", "pending_fill") == []
        assert app.scheduler.signal_due_fn(NOW) is True
        _position(app.conn_core)
        assert app.scheduler.signal_due_fn(NOW) is True
    finally:
        app.close()


def test_status_carries_the_admission_keys(tmp_path):
    app = _app(tmp_path)
    try:
        status = app.ops._status_provider()
        assert status["worst_case_guarantee_unavailable"] == "n/a"
        assert status["capacity_override_below_producer"] is False
        assert status["window"]["in_window"] is False
        assert status["window"]["active_events"] == 0
        assert status["window"]["pairs"]["USDJPY"] == {
            "in_window": False, "active_events": 0}
        assert status["suppressed_limits"] == 0
    finally:
        app.close()


def test_status_shows_the_current_window_and_the_suppressed_limit_count(tmp_path):
    app = _app(tmp_path)
    try:
        econ_events.upsert(app.conn_core, ts=NOW + timedelta(minutes=10),
                           country="USD", name="CPI", importance=3)
        econ_events.upsert(app.conn_core, ts=NOW + timedelta(minutes=10),
                           country="EUR", name="ECB", importance=3)
        oid = _position(app.conn_core, "pending_fill")
        alert_state.set(app.conn_core, alert_state.limit_suppress_key(oid),
                        '[{"id":1,"events":[1]}]', now=NOW)
        alert_state.set(app.conn_core, alert_state.limit_suppress_key(oid + 99),
                        '[{"id":1,"events":[1]}]', now=NOW)    # 約定待ちでない order
        app.scheduler._refresh_suppressed_count()              # 件数は scheduler の snapshot
        status = app.ops._status_provider()
        assert status["window"]["in_window"] is True
        assert status["window"]["active_events"] == 1          # EUR は USDJPY の対象外
        assert status["suppressed_limits"] == 1
    finally:
        app.close()


def test_the_suppressed_count_in_status_is_the_schedulers_snapshot(tmp_path):
    """件数の実装は scheduler の 1 か所。status は snapshot をそのまま読む。"""
    app = _app(tmp_path)
    try:
        app.scheduler.suppressed_limit_count = 7
        assert app.ops._status_provider()["suppressed_limits"] == 7
    finally:
        app.close()


def test_a_failing_observation_does_not_stop_the_signal_pipeline(tmp_path):
    """配備集合の取り直しと待機上限の評価の例外で、expire / reclaim / producer を止めない。"""
    app = _app(tmp_path)
    try:
        app.scheduler.state_fn = lambda: "ready"

        def boom(*args, **kwargs):
            raise RuntimeError("observation broke")

        app.admission.wait_budget.tick = boom
        sid = _signal(app.conn_signal, bar_ts=NOW - timedelta(hours=3))
        app.scheduler.on_signal_maintenance(NOW)
        row = app.conn_core.execute(
            "SELECT status FROM signals WHERE id=?", (sid,)).fetchone()
        assert row[0] == "abandoned"                     # expire は走った
    finally:
        app.close()


def test_a_failing_deployed_refresh_does_not_stop_the_signal_pipeline(
        tmp_path, monkeypatch):
    import agentic_fx.signal_admission as sa
    app = _app(tmp_path)
    try:
        app.scheduler.state_fn = lambda: "ready"
        sid = _signal(app.conn_signal, bar_ts=NOW - timedelta(hours=3))

        def boom(self, now):
            raise RuntimeError("refresh broke")

        with monkeypatch.context() as patch:
            patch.setattr(sa.DeployedStrategies, "refresh", boom)
            app.scheduler.on_signal_maintenance(NOW)
        row = app.conn_core.execute(
            "SELECT status FROM signals WHERE id=?", (sid,)).fetchone()
        assert row[0] == "abandoned"
    finally:
        app.close()


def test_the_admission_connection_does_not_wait_for_another_writer(tmp_path):
    """評価器の接続は busy_timeout 0。他の writer の BEGIN を待たず BUSY で戻る。"""
    app = _app(tmp_path)
    try:
        assert app.conn_admission.execute("PRAGMA busy_timeout").fetchone()[0] == 0
    finally:
        app.close()


def test_the_status_line_is_formatted_with_the_minimal_keys(tmp_path):
    from agentic_fx.ops import client
    app = _app(tmp_path)
    try:
        text = client.format_status(app.ops._status_provider())
        assert "worst_case_guarantee_unavailable=n/a" in text
        assert "capacity_override_below_producer=false" in text
        assert "in_window=false" in text and "抑止中 limit=0" in text
    finally:
        app.close()
    assert "signal/指標回避帯" not in client.format_status({})


def test_the_writer_flag_covers_only_the_transactions_a_tick_opens(tmp_path, monkeypatch):
    """フラグは ingest の commit、tick 本体、signal maintenance の書込みだけを覆う。
    ingest の取得、improve の発火、評価境界 (自前の接続) は覆わない。"""
    app = _app(tmp_path)
    try:
        writer = app.writer_admission
        seen: dict[str, list[bool]] = {}

        def note(name):
            seen.setdefault(name, []).append(writer.requested())

        app.ingest = SimpleNamespace(
            prepare=lambda now: (note("prepare"), (None, "report"))[1],
            commit=lambda conn: note("commit"))
        app.outage = None
        app.scheduler.state_fn = lambda: "ready"
        app.scheduler.on_improve_tick = lambda now: note("improve")
        real_bars = app.scheduler.bars_fn
        app.scheduler.bars_fn = lambda p: (note("tick"), real_bars(p))[1]
        _signal(app.conn_core, bar_ts=NOW - timedelta(hours=3))      # 鮮度切れ
        wait_budget = app.admission.wait_budget
        real_eval = wait_budget.evaluate_wait_budget

        def eval_spy(*args, **kwargs):
            note("evaluate")
            return real_eval(*args, **kwargs)

        wait_budget.evaluate_wait_budget = eval_spy
        for name in ("reclaim_expired", "expire_stale"):
            real = getattr(signals, name)

            def spy(*args, __real=real, __name=name, **kwargs):
                note(__name)
                return __real(*args, **kwargs)

            monkeypatch.setattr(signals, name, spy)
        _scheduler_tick_once(app)
        assert seen["prepare"] == [False]
        assert seen["commit"] == [True]
        assert seen["tick"] and all(seen["tick"])
        assert seen["reclaim_expired"] == [True] and seen["expire_stale"] == [True]
        assert seen["improve"] == [False]
        assert seen["evaluate"] and not any(seen["evaluate"])
        assert app.writer_admission.requested() is False
        stats = app.writer_admission.stats()
        assert stats.requests >= 3 and stats.last_hold_sec is not None
        assert stats.tick_active is False
    finally:
        app.close()


def _core_lock_is_free_for_others(app) -> bool:
    """別 thread から core lock を non-blocking で取れるか (呼んだ thread が握っていれば取れない)。"""
    import threading
    got: list[bool] = []

    def probe():
        ok = app.core_lock.acquire(blocking=False)
        got.append(ok)
        if ok:
            app.core_lock.release()

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join(5)
    return got == [True]


def test_the_terminal_evaluation_runs_after_the_commit_and_outside_the_core_lock(tmp_path):
    """実 app の tick が freshness 系の終端を commit したあと、実 core_lock を握らずに
    評価境界を呼ぶ (呼出し時点で lock と DB の状態を見る)。"""
    app = _app(tmp_path)
    try:
        sid = _signal(app.conn_core, bar_ts=NOW - timedelta(hours=3))
        app.scheduler.state_fn = lambda: "ready"
        calls = []

        def spy(signal_id, kind):
            calls.append((signal_id, kind, _core_lock_is_free_for_others(app),
                          app.conn_core.execute(
                              "SELECT status FROM signals WHERE id=?",
                              (signal_id,)).fetchone()[0]))

        app.admission.wait_budget.on_signal_terminal = spy
        _scheduler_tick_once(app)
        assert calls == [(sid, "freshness_abandoned", True, "abandoned")]
        assert not _core_lock_is_free_for_others_when_held(app)
    finally:
        app.close()


def _core_lock_is_free_for_others_when_held(app) -> bool:
    """検出器の自己確認: 自分が握っている間は『空き』と判定されない。"""
    with app.core_lock:
        return _core_lock_is_free_for_others(app)


def test_a_slow_daily_report_does_not_hold_the_writer_flag(tmp_path):
    import threading
    app = _app(tmp_path)
    release = threading.Event()
    held: list[bool] = []
    try:
        def slow(close_at):
            held.append(app.writer_admission.requested())
            release.wait(10)

        app.scheduler.register_daily_report(slow)
        _scheduler_tick_once(app)                        # 取りこぼしの catch-up が積まれる
        assert app.writer_admission.requested() is False
        deadline = time.monotonic() + 5
        while not held and time.monotonic() < deadline:
            time.sleep(0.01)
        assert held == [False]
        _scheduler_tick_once(app)                        # report が走っていても次 tick は通る
    finally:
        release.set()
        assert app.scheduler.join_daily_report(5)
        app.close()


def test_the_flag_is_released_when_the_tick_fails(tmp_path):
    app = _app(tmp_path)
    try:
        def boom(now):
            raise RuntimeError("tick failed")

        app.scheduler.tick = boom
        with pytest.raises(RuntimeError):
            _scheduler_tick_once(app)
        assert app.writer_admission.requested() is False
        assert app.writer_admission.stats().tick_active is False
    finally:
        app.close()


def test_startup_syncs_the_event_sets_for_saved_events(tmp_path):
    _init(tmp_path)
    seed = connect(tmp_path / "data" / "agentic.db")
    init_db(seed)
    econ_events.upsert(seed, ts=NOW + timedelta(days=1), country="USD", name="NFP",
                       importance=3)
    seed.close()
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                    embedding_fn=FakeEmbedding())
    try:
        rows = app.conn_core.execute(
            "SELECT pair, state FROM ohlcv_slice_sets").fetchall()
        assert [tuple(r) for r in rows] == [("USDJPY", "awaiting")]
    finally:
        app.close()


def test_a_failing_startup_sync_does_not_stop_the_start(tmp_path, monkeypatch):
    _init(tmp_path)
    from agentic_fx.datafeed.econ_calendar import EconCalendar

    def boom(self, *a, **k):
        raise RuntimeError("sync failed")

    with monkeypatch.context() as patch:
        patch.setattr(EconCalendar, "sync_sets", boom)
        app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                        embedding_fn=FakeEmbedding())
    try:
        text = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
        assert "econ_refresh_failed" in text
    finally:
        app.close()


def test_the_wired_maintenance_passes_freshness_terminals_to_the_evaluator(tmp_path):
    """build_app の on_signal_maintenance が、鮮度切れの終端を評価境界へ渡す。"""
    app = _app(tmp_path)
    try:
        _signal(app.conn_signal, bar_ts=NOW - timedelta(hours=3))
        app.scheduler.state_fn = lambda: "ready"
        app.scheduler.on_signal_maintenance(NOW)
        snap = app.ops._status_provider()["wait_budget"]
        assert snap["trigger"] == "freshness_abandoned"
        row = app.conn_core.execute(
            "SELECT status, abandon_reason FROM signals").fetchone()
        assert tuple(row) == ("abandoned", "freshness_expired")
        seen = []
        app.admission.wait_budget.on_signal_terminal = lambda sid, kind: seen.append(kind)
        _signal(app.conn_signal, bar_ts=NOW - timedelta(hours=4), plugin="again")
        app.scheduler.on_signal_maintenance(NOW + timedelta(minutes=1))
        assert seen == ["freshness_abandoned"]
    finally:
        app.close()


def test_the_wired_maintenance_reevaluates_at_a_new_operating_day(tmp_path):
    app = _app(tmp_path)
    try:
        before = app.ops._status_provider()["wait_budget"]
        assert before["trigger"] == "startup"
        app.scheduler.state_fn = lambda: "ready"
        app.scheduler.on_signal_maintenance(NOW + timedelta(minutes=5))
        assert app.ops._status_provider()["wait_budget"]["trigger"] == "startup"
        app.scheduler.on_signal_maintenance(NOW + timedelta(days=2))
        assert app.ops._status_provider()["wait_budget"]["trigger"] == "boundary"
    finally:
        app.close()


def test_maintenance_reports_freshness_terminals_to_the_evaluation_boundary(conn):
    stale = _signal(conn, bar_ts=NOW - timedelta(hours=3))
    calls = []
    producer = SimpleNamespace(evaluate_due_plugins=lambda **kw: None)
    _run_signal_maintenance(
        conn=conn, signal_producer=producer, approved=[], settings=SETTINGS,
        now=NOW, resolved_by_identity={}, activity=None,
        on_terminal=lambda sid, kind: calls.append((sid, kind)))
    assert calls == [(stale, "freshness_abandoned")]


def test_maintenance_does_not_report_cleanup_only_passes(conn):
    _signal(conn, bar_ts=NOW - timedelta(hours=3), kind="signal", contract_version=None)
    calls = []
    producer = SimpleNamespace(evaluate_due_plugins=lambda **kw: None)
    _run_signal_maintenance(
        conn=conn, signal_producer=producer, approved=[], settings=SETTINGS,
        now=NOW, resolved_by_identity={}, activity=None,
        on_terminal=lambda sid, kind: calls.append((sid, kind)))
    assert calls == []


def test_a_failing_evaluation_does_not_stop_the_producer(conn):
    _signal(conn, bar_ts=NOW - timedelta(hours=3))
    ran = []
    producer = SimpleNamespace(evaluate_due_plugins=lambda **kw: ran.append(1))

    def boom(sid, kind):
        raise RuntimeError("evaluation failed")

    _run_signal_maintenance(
        conn=conn, signal_producer=producer, approved=[], settings=SETTINGS,
        now=NOW, resolved_by_identity={}, activity=None, on_terminal=boom)
    assert ran == [1]
