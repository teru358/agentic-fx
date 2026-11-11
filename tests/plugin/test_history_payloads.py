"""gate が作った履歴証跡は、改善の提出・人間 submit・bless の payload にそのまま載る。
履歴の評価不能と未知の判別子は、submit・bless のどちらも既定で拒否する。"""
from __future__ import annotations

import json

import pytest

from agentic_fx.plugin import approval, strategy_gate, switch
from agentic_fx.plugin.strategy_gate import StrategyGateVerdict
from tests.history_support import ready_evidence
from tests.plugin.test_approval_payload_common_contract import (
    NOW, SETTINGS, _conn, _fake_pytest_ok, _write_plugin)

EVIDENCE = ready_evidence(source="mt5", base_interval="5m", pairs=["USDJPY"])


def _verdict(**kw):
    base = dict(evaluable=True, baseline_variant="no_strategy",
                baseline_row={"plugin_ref": "no_strategy:x", "variant": "no_strategy"},
                candidate_metrics={"USDJPY": {"trades": 40, "pf": 1.2}})
    base.update(kw)
    return StrategyGateVerdict(**base)


def _install(monkeypatch, verdict):
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.evaluate_strategy_adoption_gate",
        lambda conn, **kw: verdict)


def _submit(tmp_path, monkeypatch, verdict):
    root = tmp_path / "s"
    plugins = root / "plugins"
    (plugins / ".locks").mkdir(parents=True)
    conn = _conn(root)
    staging = plugins / "_staging" / "1"
    _write_plugin(staging, "strat_b", kind="strategy")
    _install(monkeypatch, verdict)
    return conn, lambda: switch.submit_candidate(
        conn, name="strat_b", staging_dir=staging, candidate_origin="staging", mission_id=1,
        backlog_id=None, settings=SETTINGS, now=NOW)


def _bless(tmp_path, monkeypatch, verdict):
    root = tmp_path / "b"
    plugins = root / "plugins"
    (plugins / ".locks").mkdir(parents=True)
    conn = _conn(root)
    _write_plugin(plugins / "_human", "strat_c", kind="strategy")
    _install(monkeypatch, verdict)
    return conn, lambda: switch.bless_candidate(
        conn, name="strat_c", human_dir=plugins / "_human" / "strat_c", settings=SETTINGS,
        now=NOW, decided_by="human_cli")


def _rows(conn):
    return conn.execute("SELECT payload_json FROM approval_requests").fetchall()


def test_submit_and_bless_carry_the_evidence_unchanged(tmp_path, monkeypatch):
    for make in (_submit, _bless):
        conn, call = make(tmp_path / make.__name__, monkeypatch, _verdict(history_guard=EVIDENCE))
        approval_id = call()
        payload = json.loads(conn.execute("SELECT payload_json FROM approval_requests WHERE id=?",
                                          (approval_id,)).fetchone()[0])
        assert payload["history_guard"] == EVIDENCE




@pytest.mark.parametrize("kind", ["history_stale", "history_gap", "history_check_unavailable"])
@pytest.mark.parametrize("make", [_submit, _bless])
def test_a_history_failure_creates_no_request_and_names_the_next_step(
        tmp_path, monkeypatch, kind, make):
    conn, call = make(tmp_path, monkeypatch, _verdict(
        evaluable=False, observation_reason=kind, unevaluable_kind=kind,
        candidate_metrics=None, baseline_row=None))
    with pytest.raises(ValueError) as caught:
        call()
    assert kind in str(caught.value) and "afx status" in str(caught.value)
    assert _rows(conn) == []


def _kind_outcome(kind, evaluable, sink):
    return approval.GateOutcome(metrics={}, evaluable=evaluable, verdict_kind=kind,
                                gate_rows=sink)


@pytest.mark.parametrize("kind,evaluable", [("a_kind_added_later", True), ("ok", False)])
@pytest.mark.parametrize("make", [_submit, _bless])
def test_an_unknown_or_unevaluable_outcome_is_refused_by_default(
        tmp_path, monkeypatch, kind, evaluable, make):
    conn, call = make(tmp_path, monkeypatch, _verdict())
    monkeypatch.setattr(switch.approval, "run_kind_gate",
                        lambda *a, sink=None, **k: _kind_outcome(kind, evaluable, sink))
    with pytest.raises(ValueError, match="not evaluable"):
        call()
    assert _rows(conn) == []


def test_a_warned_floor_still_blesses_but_an_enforced_one_still_refuses(tmp_path, monkeypatch):
    conn, call = _bless(tmp_path / "w", monkeypatch, _verdict(
        floor_reason="unprofitable", floor_detail="d", history_guard=EVIDENCE))
    assert call() > 0
    conn, call = _submit(tmp_path / "e", monkeypatch, _verdict(
        floor_reason="unprofitable", floor_detail="d", history_guard=EVIDENCE))
    with pytest.raises(ValueError, match="unprofitable"):
        call()
    assert _rows(conn) == []
