"""`_run_full_gate` の失敗の記録先。

省略すると従来どおり gate の行を DB の成績表へ保存し、明示した記録先は全ての失敗
(早い段階の拒否・想定外例外・判別子による不合格) でその 1 つだけが呼ばれ、DB へ倒れない。"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest

from agentic_fx.plugin import gate_pytest, switch
from agentic_fx.plugin.sandbox import SandboxError
from tests.plugin.test_switch_floor_modes import (
    _fake_pytest_ok, _floor_fail_gate, _insufficient_trades_gate, _ok_gate,
    _write_strategy_candidate, env)  # noqa: F401  (env は fixture)

NOW = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)
GATE = "agentic_fx.plugin.strategy_gate.evaluate_strategy_adoption_gate"


@pytest.fixture
def candidate(env, monkeypatch):
    root, plugins_dir, conn, settings = env
    path = plugins_dir / "_staging" / "1" / "st"
    _write_strategy_candidate(path)
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    return path, plugins_dir, conn, settings


class Collector:
    def __init__(self):
        self.failures: list[switch.GateFailure] = []

    def __call__(self, failure):
        self.failures.append(failure)


def _run(candidate, sink, *, floor_mode="warn"):
    path, plugins_dir, conn, settings = candidate
    kwargs = {} if sink is switch._DEFAULT_FAILURE_SINK else {"failure_sink": sink}
    return switch._run_full_gate(
        conn, path, name="st", settings=settings, now=NOW, floor_mode=floor_mode,
        plugins_root=plugins_dir, **kwargs)


def _runs(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0]


# ---- 省略 = 従来の DB 保存 ---------------------------------------------------------

def test_omitting_the_sink_still_saves_the_failed_gate_rows_to_the_database(
        candidate, monkeypatch):
    _, _, conn, _ = candidate
    monkeypatch.setattr(GATE, _insufficient_trades_gate)
    with pytest.raises(ValueError, match="not evaluable"):
        _run(candidate, switch._DEFAULT_FAILURE_SINK)
    assert _runs(conn) == 1
    assert conn.execute("SELECT mission_outcome FROM backtest_runs").fetchone()[0] == "gate_failed"


def test_omitting_the_sink_saves_unprofitable_rows_for_an_enforced_floor(candidate, monkeypatch):
    _, _, conn, _ = candidate
    monkeypatch.setattr(GATE, _floor_fail_gate)
    with pytest.raises(ValueError, match="unprofitable"):
        _run(candidate, switch._DEFAULT_FAILURE_SINK, floor_mode="enforce")
    assert conn.execute("SELECT mission_outcome FROM backtest_runs").fetchone()[0] == "unprofitable"


def test_the_default_sink_resolves_the_persist_function_at_call_time(candidate, monkeypatch):
    seen = []
    monkeypatch.setattr(GATE, _insufficient_trades_gate)
    monkeypatch.setattr(switch, "_persist_human_gate_rows",
                        lambda conn, rows, **kw: seen.append((len(rows), kw["mission_outcome"])))
    with pytest.raises(ValueError):
        _run(candidate, switch._DEFAULT_FAILURE_SINK)
    assert seen == [(1, "gate_failed")]


# ---- 明示した記録先: 全失敗で 1 回、DB は変えない -----------------------------------

def _break_snapshot(path):
    (path / "extra.txt").write_text("x")


def _break_source(path):
    (path / "plugin.py").write_text("import os\n" + (path / "plugin.py").read_text())


def _break_discovery(path):
    (path / "config.yaml").write_text("kind: strategy\nbogus_key: 1\n")


def _break_max_bars(path):
    (path / "config.yaml").write_text(
        "kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\nexit_mode: levels\nmax_bars: 99999999\n")


EARLY = [("snapshot", _break_snapshot), ("source", _break_source),
         ("discovery", _break_discovery), ("contract", _break_max_bars)]


@pytest.mark.parametrize("stage,breaker", EARLY, ids=[s for s, _ in EARLY])
def test_early_rejections_reach_the_sink_with_their_stage_and_touch_no_database(
        candidate, stage, breaker):
    path, _, conn, _ = candidate
    breaker(path)
    before = conn.total_changes
    sink = Collector()
    with pytest.raises(ValueError):
        _run(candidate, sink)
    assert [f.stage for f in sink.failures] == [stage]
    assert sink.failures[0].rows == () and sink.failures[0].mission_outcome is None
    assert conn.total_changes == before and _runs(conn) == 0


def test_a_failed_pytest_is_recorded_with_its_structured_kind_and_return_code(
        candidate, monkeypatch):
    _, _, conn, _ = candidate
    monkeypatch.setattr(
        "agentic_fx.plugin.switch.run_gate_pytest",
        lambda d, *, settings: gate_pytest.GateResult(False, -1, "", 0.1))
    sink = Collector()
    before = conn.total_changes
    with pytest.raises(switch.GateCheckError):
        _run(candidate, sink)
    (failure,) = sink.failures
    assert (failure.stage, failure.detail, failure.returncode) == ("pytest", "pytest", -1)
    assert conn.total_changes == before


def test_a_candidate_changed_during_the_gate_is_recorded_as_a_hash_failure(
        candidate, monkeypatch):
    path, _, conn, _ = candidate

    def tamper(plugin_dir, *, settings):
        (plugin_dir / "test_plugin.py").write_text("def test_other():\n    pass\n")
        return gate_pytest.GateResult(True, 0, "", 0.1)

    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", tamper)
    sink = Collector()
    with pytest.raises(switch.GateCheckError):
        _run(candidate, sink)
    assert [(f.stage, f.detail) for f in sink.failures] == [("hash", "hash")]


def test_a_sandbox_failure_during_the_backtest_keeps_the_rows_collected_so_far(
        candidate, monkeypatch):
    _, _, conn, _ = candidate

    def boom(conn, *, meta, settings, now, floor_mode="enforce", record_fn=None, **_):
        _insufficient_trades_gate(conn, meta=meta, settings=settings, now=now, record_fn=record_fn)
        raise SandboxError("worker died", code="crashed")

    monkeypatch.setattr(GATE, boom)
    sink = Collector()
    before = conn.total_changes
    with pytest.raises(ValueError):
        _run(candidate, sink)
    (failure,) = sink.failures
    assert (failure.stage, failure.detail, failure.error_type) == (
        "kind_gate", "crashed", "SandboxError")
    assert len(failure.rows) == 1 and failure.mission_outcome == "gate_failed"
    assert conn.total_changes == before and _runs(conn) == 0


def test_an_unexpected_exception_reaches_the_sink_and_propagates(candidate, monkeypatch):
    _, _, conn, _ = candidate

    def boom(conn, **_):
        raise ZeroDivisionError("x")

    monkeypatch.setattr(GATE, boom)
    sink = Collector()
    with pytest.raises(ZeroDivisionError):
        _run(candidate, sink)
    (failure,) = sink.failures
    assert (failure.stage, failure.error_type) == ("kind_gate", "ZeroDivisionError")
    assert _runs(conn) == 0


@pytest.mark.parametrize("gate,kind,outcome", [
    (_insufficient_trades_gate, "insufficient_trades", "gate_failed"),
])
def test_a_verdict_failure_is_classified_by_the_discriminator(
        candidate, monkeypatch, gate, kind, outcome):
    _, _, conn, _ = candidate
    monkeypatch.setattr(GATE, gate)
    sink = Collector()
    with pytest.raises(ValueError):
        _run(candidate, sink)
    (failure,) = sink.failures
    assert (failure.stage, failure.verdict_kind, failure.mission_outcome) == (
        "verdict", kind, outcome)
    assert len(failure.rows) == 1 and _runs(conn) == 0


def test_an_enforced_floor_failure_goes_to_the_explicit_sink_too(candidate, monkeypatch):
    _, _, conn, _ = candidate
    monkeypatch.setattr(GATE, _floor_fail_gate)
    sink = Collector()
    with pytest.raises(ValueError, match="unprofitable"):
        _run(candidate, sink, floor_mode="enforce")
    (failure,) = sink.failures
    assert (failure.verdict_kind, failure.mission_outcome) == ("floor", "unprofitable")
    assert _runs(conn) == 0


def test_a_warned_floor_completes_and_the_sink_is_not_called(candidate, monkeypatch):
    _, _, conn, _ = candidate
    monkeypatch.setattr(GATE, _floor_fail_gate)
    sink = Collector()
    meta, content, artifact, outcome = _run(candidate, sink, floor_mode="warn")
    assert outcome.verdict_kind == "floor" and sink.failures == []
    assert len(outcome.gate_rows) == 1 and _runs(conn) == 0


def test_a_successful_gate_keeps_the_four_element_result(candidate, monkeypatch):
    monkeypatch.setattr(GATE, _ok_gate)
    assert len(_run(candidate, Collector(), floor_mode="warn")) == 4


# ---- 記録先の解決: fallback 禁止 ---------------------------------------------------

def test_an_explicit_none_is_refused_before_anything_runs(candidate, monkeypatch):
    path, _, conn, _ = candidate
    called = []
    monkeypatch.setattr(switch, "check_candidate_snapshot", lambda p: called.append(p))
    with pytest.raises(TypeError):
        _run(candidate, None)
    assert called == [] and _runs(conn) == 0


def test_a_failing_sink_propagates_and_never_falls_back_to_the_database(
        candidate, monkeypatch):
    _, _, conn, _ = candidate
    monkeypatch.setattr(GATE, _insufficient_trades_gate)
    saved = []
    monkeypatch.setattr(switch, "_persist_human_gate_rows",
                        lambda *a, **k: saved.append(1))

    def broken(failure):
        raise OSError("sink is down")

    before = conn.total_changes
    with pytest.raises(OSError, match="sink is down"):
        _run(candidate, broken)
    assert saved == [] and _runs(conn) == 0 and conn.total_changes == before


def test_a_failing_sink_on_an_early_rejection_never_writes_to_the_database(candidate):
    path, _, conn, _ = candidate
    _break_snapshot(path)

    def broken(failure):
        raise OSError("sink is down")

    with pytest.raises(OSError):
        _run(candidate, broken)
    assert _runs(conn) == 0


def test_the_public_entry_points_keep_defaulting_to_the_database_sink():
    signature = inspect.signature(switch._run_full_gate)
    assert signature.parameters["failure_sink"].default is switch._DEFAULT_FAILURE_SINK
    for fn in (switch.submit_candidate, switch.bless_candidate):
        assert "failure_sink" not in inspect.signature(fn).parameters


def test_the_gate_pins_the_comparison_off(candidate, monkeypatch):
    seen = {}

    def spy(conn, *, meta, settings, now, floor_mode="enforce", record_fn=None, **kw):
        seen.update(kw)
        return _ok_gate(conn, meta=meta, settings=settings, now=now)

    monkeypatch.setattr(GATE, spy)
    _run(candidate, Collector(), floor_mode="warn")
    assert seen["compare_mode"] == "off"
