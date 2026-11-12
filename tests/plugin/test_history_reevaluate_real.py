"""配備中の再評価が共有 gate へ渡す引数を、履歴検査を差し替えず実 sqlite の履歴で確かめる。
実 backtest だけは差し替える。`_store_reevaluation` まで通す。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from agentic_fx.backtest import history_evidence as ev
from agentic_fx.plugin import switch
from tests.history_support import fill_history, hole_day, write_no_bars
from tests.plugin.reevaluation_world import CONFIG, RealWorld, strategy_py
from tests.plugin.test_reevaluate_deployed import NAME, Recorder
from tests.plugin.test_strategy_gate import _metrics

UTC = timezone.utc
TUESDAY = datetime(2026, 11, 10, 12, 0, 20, tzinfo=UTC)
WEEKEND = datetime(2026, 11, 8, 3, 17, 42, tzinfo=UTC)
NOWS = [pytest.param(TUESDAY, id="weekday"), pytest.param(WEEKEND, id="across-weekend")]
SOURCE = "dukascopy"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
        lambda *a, **kw: _metrics(trades=40, pf=1.5))
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_holdout_gate",
        lambda *a, **kw: _metrics(trades=40, pf=1.5))
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.strategy_adapter.build_intent_source",
        lambda meta, **kw: MagicMock(close=lambda: None, cpu_sec=0.0))
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.require_reference_history",
        lambda *a, **kw: None)
    return RealWorld(tmp_path, history=False)


def _deploy(world, *, config=CONFIG):
    version = world.strategy("a", strategy_py(), config=config)
    world.bless_like(version, 1)


def _reevaluate(world, now):
    activity = Recorder()
    result = switch.reevaluate_deployed(
        world.conn, name=NAME, plugins_root=world.root, settings=world.settings, now=now,
        activity=activity)
    return result, activity


def _pending(world):
    return world.conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE status='pending'").fetchone()[0]


@pytest.mark.real_history_guard
@pytest.mark.parametrize("now", NOWS)
def test_a_healthy_history_creates_the_request_with_the_evidence(world, now):
    _deploy(world)
    fill_history(world.conn, source=SOURCE, pairs=["USDJPY"], now=now)
    result, _ = _reevaluate(world, now)
    assert result.status == "created"
    payload = json.loads(world.conn.execute(
        "SELECT payload_json FROM approval_requests WHERE id=?",
        (result.approval_id,)).fetchone()[0])
    evidence = payload["history_guard"]
    assert evidence["verdict"] == "ready" and evidence["pairs"] == ["USDJPY"]
    assert evidence["checked_at"] == now.isoformat()
    assert evidence["dataset"] == {"source": SOURCE, "base_interval": "5m"}
    assert ev.verify_evidence(evidence, eval_source=SOURCE, base_interval="5m",
                              pairs=["USDJPY"], no_bars_path=ev.no_bars_path(world.root.parent)).ok


@pytest.mark.real_history_guard
@pytest.mark.parametrize("now", NOWS)
@pytest.mark.parametrize("scenario,kind", [("stale", "history_stale"), ("gap", "history_gap")])
def test_a_stale_or_gapped_history_files_no_request(world, scenario, kind, now):
    _deploy(world)
    fill_history(world.conn, source=SOURCE, pairs=["USDJPY"], now=now, scenario=scenario)
    result, activity = _reevaluate(world, now)
    assert result.status == "unevaluable" and result.reason == kind
    assert _pending(world) == 0
    assert activity.names() == ["reevaluation_unevaluable"]


@pytest.mark.real_history_guard
def test_the_primary_pair_comes_from_the_deployed_strategys_pairs(world):
    _deploy(world, config=CONFIG.replace("USDJPY", "EURUSD"))
    fill_history(world.conn, source=SOURCE, pairs=["USDJPY"], now=TUESDAY)
    fill_history(world.conn, source=SOURCE, pairs=["EURUSD"], now=TUESDAY, scenario="stale")
    result, _ = _reevaluate(world, TUESDAY)
    assert result.status == "unevaluable" and result.reason == "history_stale"
    assert _pending(world) == 0


@pytest.mark.real_history_guard
def test_the_no_bars_list_is_read_from_the_plugins_root_parent(world):
    _deploy(world)
    fill_history(world.conn, source=SOURCE, pairs=["USDJPY"], now=TUESDAY, scenario="gap")
    day = hole_day(TUESDAY)[0].date()
    write_no_bars(world.root.parent, source=SOURCE, symbol="USDJPY", day=day)
    result, _ = _reevaluate(world, TUESDAY)
    assert result.status == "created"
    payload = json.loads(world.conn.execute(
        "SELECT payload_json FROM approval_requests WHERE id=?",
        (result.approval_id,)).fetchone()[0])
    entries = payload["history_guard"]["no_bars_entries"]
    assert [(e["symbol"], e["date_utc"]) for e in entries] == [("USDJPY", day.isoformat())]
