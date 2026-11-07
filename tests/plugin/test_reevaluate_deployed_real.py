"""``reevaluate_deployed`` against the real gate: a real worker replays a real plugin over a
small synthetic history (a deterministic 24 hour wave) in ``tmp_path``.
"""
from __future__ import annotations

import json

import pytest

from agentic_fx.plugin import strategy_gate, switch
from agentic_fx.tools import plugin_loader
from tests.plugin.reevaluation_world import (
    HOLD_PY, STARTED, RealWorld, real_settings, strategy_py)
from tests.plugin.test_reevaluate_deployed import NAME, Recorder


@pytest.fixture
def world(tmp_path):
    return RealWorld(tmp_path)


def _reevaluate(world: RealWorld, name: str = NAME, **overrides):
    activity = overrides.pop("activity", Recorder())
    result = switch.reevaluate_deployed(
        world.conn, name=name, plugins_root=world.root,
        settings=overrides.pop("settings", world.settings), now=STARTED, activity=activity)
    return result, activity


def _payload(world: RealWorld, approval_id: int) -> dict:
    return json.loads(world.conn.execute(
        "SELECT payload_json FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()[0])


def test_the_request_holds_exactly_what_the_shared_gate_gives_for_the_same_now(world):
    version = world.strategy("a", strategy_py())
    world.bless_like(version, 1)

    result, _activity = _reevaluate(world)

    assert result.status == "created"
    payload = _payload(world, result.approval_id)
    # an independent run of the same gate at the same instant
    inventory = plugin_loader.approved_plugins(world.conn, world.root, settings=world.settings)
    meta = next(m for m in inventory.inventory.metas if m.name == NAME)
    rows: list[dict] = []
    verdict = strategy_gate.evaluate_strategy_adoption_gate(
        world.conn, meta=meta, settings=world.settings, now=STARTED,
        resolved=inventory.resolved[(NAME, meta.content_hash)], inventory=inventory,
        floor_mode="warn", compare_mode="off", record_fn=rows.append)
    assert verdict.evaluable
    assert payload["in_sample"] == verdict.candidate_metrics
    assert payload["holdout"] == verdict.holdout_metrics
    assert payload["in_sample"]["USDJPY"]["trades"] >= 30
    # the run ids name the rows the gate wrote
    runs = {r["id"]: r for r in world.conn.execute("SELECT * FROM backtest_runs")}
    for scope, key in (("in_sample", "in_sample"), ("holdout_gate", "holdout")):
        row = runs[payload["run_ids"][key]["USDJPY"]]
        assert (row["scope"], row["mission_outcome"]) == (scope, "reevaluation")
        assert json.loads(row["metrics_json"]) == payload[key]["USDJPY"]
    assert payload["eval_now"] == "2026-11-08T03:15:00+00:00"
    # the deployment is the same version, still live
    assert (world.root / NAME).is_symlink()


def test_a_strategy_with_too_few_trades_keeps_its_generated_row_and_files_no_request(world):
    version = world.strategy("hold", HOLD_PY)
    world.bless_like(version, 1)

    result, activity = _reevaluate(world)

    assert result.status == "unevaluable" and result.reason == "insufficient_trades:0"
    assert world.conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE status='pending'").fetchone()[0] == 0
    rows = [tuple(r) for r in world.conn.execute(
        "SELECT scope, mission_outcome FROM backtest_runs")]
    assert rows == [("in_sample", "reevaluation")]
    assert activity.names() == ["reevaluation_unevaluable"]
    assert (world.root / NAME).is_symlink()


def test_an_unconfigured_history_source_stops_before_any_replay_and_leaves_no_row(world):
    version = world.strategy("a", strategy_py())
    world.bless_like(version, 1)

    result, activity = _reevaluate(world, settings=real_settings(eval_source="none"))

    assert result.status == "unevaluable"
    assert "history_source_unconfigured" in result.reason
    assert world.conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 0
    assert world.conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE status='pending'").fetchone()[0] == 0
    assert activity.names() == ["reevaluation_unevaluable"]


def test_several_strategies_are_evaluated_one_by_one_at_the_same_instant(world):
    first = world.strategy("a", strategy_py())
    world.bless_like(first, 1)
    second = world.strategy("b", strategy_py(take_profit=0.8, stop_loss=0.4, note="b"),
                            name="other")
    world.bless_like(second, 3, name="other")

    results = [_reevaluate(world, name)[0] for name in ("other", NAME)]

    assert [r.status for r in results] == ["created", "created"]
    pending = world.conn.execute(
        "SELECT json_extract(payload_json,'$.name'), json_extract(payload_json,'$.eval_now') "
        "FROM approval_requests WHERE status='pending' ORDER BY id").fetchall()
    assert [tuple(r) for r in pending] == [
        ("other", "2026-11-08T03:15:00+00:00"), (NAME, "2026-11-08T03:15:00+00:00")]
