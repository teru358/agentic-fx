"""同名更新の候補を配備中の版と比べる gate が commit に配線されていること。

gate 本体 (`evaluate_strategy_adoption_gate`) は差し替え、commit が verdict を
どう扱うか (mode の snapshot、却下 / 継続の分岐、行・payload・activity・
レポート・通知の出し方) を実 DB・実 prepare / commit で確かめる。"""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest

from agentic_fx import improve_notice_codes as codes
from agentic_fx.loops.improve_loop import ImproveLoop
from agentic_fx.plugin.sandbox import SandboxError
from agentic_fx.plugin.strategy_gate import (
    StrategyGateVerdict, _decide_comparison)
from tests.loops.test_improve_candidate_ledger import (
    _indicator, _run, _strategy)
from tests.loops.test_improve_e2e import (
    NOW, _fake_record_kwargs, _no_real_network, _install, _plugin_artifact,
    improve_env)  # noqa: F401
from tests.loops.test_improve_quality_gate import (
    PY_X, PY_Y, _artifact, _files)

BASE_HASH = "b" * 64
PAIR = "USDJPY"
CAND_IN = {"trades": 40, "pf": 1.5, "win_rate": 0.5, "avg_r": 0.2,
           "max_drawdown": 0.1, "total_pnl": 100.0,
           "kill_switch_latches": 0, "evaluable": True}
CAND_HO = {**CAND_IN, "pf": 1.4, "avg_r": 0.15}
DEPLOYED_IN = {**CAND_IN, "pf": 1.2, "avg_r": 0.1}
DEPLOYED_HO_WORSE = {**CAND_IN, "pf": 1.1, "avg_r": 0.05}
DEPLOYED_HO_BETTER = {**CAND_IN, "pf": 2.5, "avg_r": 0.4}
DEPLOYED_NOT_EVALUABLE = {**CAND_IN, "trades": 3, "evaluable": False}


def _candidate_row(settings, scope, content_hash, metrics):
    row = _fake_record_kwargs(
        settings=settings, trades=metrics["trades"], scope=scope,
        content_hash=content_hash, plugin_ref="plugins/cand",
        source=settings.backtest.eval_source,
        base_interval=settings.backtest.dataset().base_interval)
    row["metrics"] = dict(metrics)
    return row


def _baseline_rows(settings, candidate_hash, in_sample, holdout):
    rows = []
    for scope, metrics in (("in_sample", in_sample),
                           ("holdout_gate", holdout)):
        row = _candidate_row(settings, scope, BASE_HASH, metrics)
        row.update(variant="baseline", ref_plugin_ref="plugins/cand",
                   ref_content_hash=candidate_hash)
        rows.append(row)
    return tuple(rows)


class FakeGate:
    """`ImproveLoop._run_strategy_gate` の差し替え。呼び出しの kwargs を
    積み、`kind` に応じた verdict を返す。"""

    def __init__(self, settings, kind, *, metrics=None):
        self.settings = settings
        self.kind = kind
        self.calls: list[dict] = []
        self.m = metrics or {
            "cand_in": CAND_IN, "cand_ho": CAND_HO, "dep_in": DEPLOYED_IN,
            "dep_ho_worse": DEPLOYED_HO_WORSE,
            "dep_ho_better": DEPLOYED_HO_BETTER}

    def __call__(self, conn, **kw):
        self.calls.append(kw)
        mode = kw["compare_mode"]
        content_hash = kw["content_hash"]
        record = kw["record_fn"]
        if self.kind == "violation":
            return StrategyGateVerdict(
                evaluable=False, baseline_variant="baseline",
                baseline_row={"ref_plugin_ref": "plugins/cand",
                              "variant": "baseline"},
                floor_reason="same_name_contract_changed",
                contract_violation="same_name_contract_changed")
        if self.kind == "unprofitable_in_sample":
            record(_candidate_row(self.settings, "in_sample", content_hash,
                                  {**CAND_IN, "pf": 0.4}))
            return StrategyGateVerdict(
                evaluable=True, floor_reason="in_sample pair=USDJPY pf",
                candidate_metrics={PAIR: {**CAND_IN, "pf": 0.4}})
        m = self.m
        in_row = _candidate_row(self.settings, "in_sample", content_hash,
                                m["cand_in"])
        ho_row = _candidate_row(self.settings, "holdout_gate", content_hash,
                                m["cand_ho"])
        record(in_row)
        record(ho_row)
        base = {"ref_plugin_ref": "plugins/cand", "variant": "baseline"}
        comparison, rows = None, ()
        if mode != "off" and self.kind != "new_name":
            base["base_content_hash"] = BASE_HASH
            if self.kind in ("improved", "not_improved"):
                holdout = (m["dep_ho_better"] if self.kind == "not_improved"
                           else m["dep_ho_worse"])
                comparison = _decide_comparison(
                    mode=mode, pairs=[PAIR], stages={
                        "in_sample": {"candidate": {PAIR: m["cand_in"]},
                                      "deployed": {PAIR: m["dep_in"]}},
                        "holdout": {"candidate": {PAIR: m["cand_ho"]},
                                    "deployed": {PAIR: holdout}}})
                rows = _baseline_rows(self.settings, content_hash,
                                      m["dep_in"], holdout)
            elif self.kind == "unavailable_unhealthy":
                comparison = {"mode": mode, "verdict": "unavailable",
                              "reason": "baseline_unhealthy",
                              "stages": {"holdout": {}, "in_sample": {}}}
            elif self.kind == "unavailable_not_evaluable":
                comparison = _decide_comparison(
                    mode=mode, pairs=[PAIR], stages={
                        "in_sample": {"candidate": {PAIR: CAND_IN},
                                      "deployed": {PAIR: DEPLOYED_NOT_EVALUABLE}},
                        "holdout": {"candidate": {PAIR: CAND_HO},
                                    "deployed": {PAIR: DEPLOYED_NOT_EVALUABLE}}})
                rows = _baseline_rows(self.settings, content_hash,
                                      DEPLOYED_NOT_EVALUABLE,
                                      DEPLOYED_NOT_EVALUABLE)
        return StrategyGateVerdict(
            evaluable=True, baseline_variant="baseline", baseline_row=base,
            candidate_metrics={PAIR: m["cand_in"]},
            holdout_metrics={PAIR: m["cand_ho"]},
            comparison=comparison,
            not_improved=bool(
                comparison and comparison["verdict"] == "not_improved"),
            baseline_rows=rows)


def _drive(app, root, kind, mode, *, name="cand", files=None, artifact=None,
           **gate_kw):
    app.settings.improve.baseline_gate = mode
    gate = FakeGate(app.settings, kind, **gate_kw)
    outcomes: list[str] = []
    real = ImproveLoop._persist_ledger_in_tx

    def spy(self, conn, **kw):
        outcomes.append(kw["mission_outcome"])
        return real(self, conn, **kw)

    loop, ctx, _ = _run(
        app, root, artifact or _artifact(name), files=files or _files(name),
        extra=[patch.object(ImproveLoop, "_run_strategy_gate", gate),
               patch.object(ImproveLoop, "_persist_ledger_in_tx", spy)],
        seed=False)
    gate.ledger_outcomes = outcomes
    return gate, loop, ctx


def _backtest_rows(conn, mission_id):
    return [dict(r) for r in conn.execute(
        "SELECT variant, scope, mission_outcome, content_hash, metrics_json "
        "FROM backtest_runs WHERE mission_id=? ORDER BY id", (mission_id,))]


def _variants(conn, mission_id):
    return [(r["variant"], r["scope"], r["mission_outcome"])
            for r in _backtest_rows(conn, mission_id)]


def _notice(conn, mission_id):
    row = conn.execute(
        "SELECT result_code, reason_code FROM improve_run_notices "
        "WHERE mission_id=?", (mission_id,)).fetchone()
    return (row[0], row[1])


def _activity(root):
    return (root / "logs" / "activity.log").read_text()


def _approvals(conn):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload_json FROM approval_requests WHERE kind='plugin' "
        "ORDER BY id")]


def _last_result(conn):
    return conn.execute(
        "SELECT last_result FROM improvement_backlog ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]


# --- 却下 / 継続の分岐 -------------------------------------------------------

def test_enforce_not_improved_rejects(improve_env):
    app, root = improve_env
    conn = app.conn_core
    gate, loop, ctx = _drive(app, root, "not_improved", "enforce")
    mid = ctx.mission_id
    assert _approvals(conn) == []
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (mid,)).fetchone()[0] == "completed"
    assert _last_result(conn) == "observation:not_submitted"
    assert _notice(conn, mid) == ("gate_failed", "not_improved")
    rows = _variants(conn, mid)
    assert ("baseline", "in_sample", "not_improved") in rows
    assert ("baseline", "holdout_gate", "not_improved") in rows
    assert ("candidate", "in_sample", "not_improved") in rows
    assert {r[2] for r in rows} == {"not_improved"}
    assert gate.ledger_outcomes == ["not_improved"]
    text = _activity(root)
    assert f"gate_failed\tmission={mid} reason=not_improved" in text
    line = next(ln for ln in text.splitlines() if "baseline_compare" in ln)
    cand12 = gate.calls[0]["content_hash"][:12]
    assert (f"baseline_compare\tmission={mid} plugin=cand mode=enforce "
            f"result=not_improved detail=holdout_not_exceeds "
            f"candidate={cand12} base={BASE_HASH[:12]}") in line
    assert not any(ch in line.split("baseline_compare", 1)[1]
                   for ch in ("pf=", "1.5", "1.4"))
    report = next((root / "data" / "improve_reports").glob("improve-*.md"))
    body = report.read_text()
    assert "## Baseline comparison detail" in body
    assert cand12 in body and BASE_HASH[:12] in body
    assert "## Profitability floor detail" not in body


def test_enforce_rejection_marks_origin_rows_and_archive(improve_env):
    app, root = improve_env
    conn = app.conn_core
    gate, loop, ctx = _drive(app, root, "not_improved", "enforce")
    outcomes = {r[0] for r in conn.execute(
        "SELECT outcome_class FROM candidate_archives WHERE mission_id=?",
        (ctx.mission_id,))}
    assert outcomes == {"gate_failed"}
    origin = {r[0] for r in conn.execute(
        "SELECT origin_outcome FROM improvement_backlog "
        "WHERE origin_mission_id=?", (ctx.mission_id,))}
    assert origin <= {"not_submitted"} and origin


def test_shadow_not_improved_goes_to_approval(improve_env):
    app, root = improve_env
    conn = app.conn_core
    gate, loop, ctx = _drive(app, root, "not_improved", "shadow")
    payloads = _approvals(conn)
    assert len(payloads) == 1
    comparison = payloads[0]["baseline"]["comparison"]
    assert comparison["verdict"] == "not_improved"
    assert comparison["mode"] == "shadow"
    rows = _variants(conn, ctx.mission_id)
    assert {r[2] for r in rows} == {"approval"}
    assert sum(1 for r in rows if r[0] == "baseline") == 2
    assert f"mode=shadow result=not_improved" in _activity(root)
    assert _notice(conn, ctx.mission_id) == ("submitted", None)


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_improved_creates_approval_with_comparison_in_both_modes(
        improve_env, mode):
    app, root = improve_env
    conn = app.conn_core
    gate, loop, ctx = _drive(app, root, "improved", mode)
    (payload,) = _approvals(conn)
    json.dumps(payload)
    baseline = payload["baseline"]
    assert set(baseline["comparison"]) == {"mode", "verdict", "reason",
                                           "stages"}
    assert baseline["comparison"]["verdict"] == "improved"
    assert baseline["comparison"]["mode"] == mode
    assert baseline["base_content_hash"] == BASE_HASH
    assert baseline["ref_plugin_ref"] == "plugins/cand"
    assert baseline["variant"] == "baseline"
    entry = baseline["comparison"]["stages"]["holdout"][PAIR]
    assert set(entry) == {"candidate", "deployed", "verdict"}
    assert entry["verdict"] == "improved"
    assert _variants(conn, ctx.mission_id).count(
        ("baseline", "holdout_gate", "approval")) == 1


def test_payload_holdout_is_candidate_not_deployed(improve_env):
    app, root = improve_env
    conn = app.conn_core
    _drive(app, root, "improved", "enforce")
    (payload,) = _approvals(conn)
    assert payload["holdout"]["pf"] == CAND_HO["pf"]
    assert payload["holdout"]["pf"] != 1.0
    assert payload["in_sample"][PAIR]["pf"] == CAND_IN["pf"]


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_unavailable_replay_passes_with_floor_only(improve_env, mode):
    app, root = improve_env
    conn = app.conn_core
    gate, loop, ctx = _drive(app, root, "unavailable_unhealthy", mode)
    (payload,) = _approvals(conn)
    assert payload["baseline"]["comparison"]["verdict"] == "unavailable"
    assert [r for r in _variants(conn, ctx.mission_id)
            if r[0] == "baseline"] == []
    assert f"mode={mode} result=unavailable detail=baseline_unhealthy" \
        in _activity(root)


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_unavailable_not_evaluable_baseline_keeps_its_rows(improve_env, mode):
    app, root = improve_env
    conn = app.conn_core
    gate, loop, ctx = _drive(app, root, "unavailable_not_evaluable", mode)
    (payload,) = _approvals(conn)
    assert payload["baseline"]["comparison"]["reason"] \
        == "baseline_not_evaluable"
    rows = _variants(conn, ctx.mission_id)
    assert rows.count(("baseline", "in_sample", "approval")) == 1
    assert rows.count(("baseline", "holdout_gate", "approval")) == 1


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_contract_violation_is_not_insufficient_trades(improve_env, mode):
    app, root = improve_env
    conn = app.conn_core
    gate, loop, ctx = _drive(app, root, "violation", mode)
    mid = ctx.mission_id
    assert _last_result(conn) == "observation:not_submitted"
    assert _notice(conn, mid) == ("gate_failed", "same_name_contract_changed")
    assert _approvals(conn) == []
    assert _backtest_rows(conn, mid) == []
    assert gate.ledger_outcomes == ["same_name_contract_changed"]
    assert "reason=same_name_contract_changed" in _activity(root)
    assert "baseline_compare" not in _activity(root)
    body = next((root / "data" / "improve_reports").glob("improve-*.md")
                ).read_text()
    assert "## Baseline comparison detail" in body
    assert "新しい名前" in body


def test_floor_failure_has_no_baseline_rows_and_unprofitable(improve_env):
    app, root = improve_env
    conn = app.conn_core
    gate, loop, ctx = _drive(app, root, "unprofitable_in_sample", "enforce")
    assert _notice(conn, ctx.mission_id) == ("gate_failed", "unprofitable")
    assert {r[0] for r in _variants(conn, ctx.mission_id)} == {"candidate"}
    assert {r[2] for r in _variants(conn, ctx.mission_id)} == {"unprofitable"}
    text = _activity(root)
    assert "baseline_compare" not in text
    assert "reason=unprofitable" in text


def test_quality_demotion_keeps_baseline_rows_with_observation_outcome(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    _drive(app, root, "improved", "shadow", name="conv_a",
           files=_files("conv_a", PY_X))
    gate, loop, ctx = _drive(app, root, "improved", "shadow", name="conv_b",
                             files=_files("conv_b", PY_Y))
    assert _notice(conn, ctx.mission_id) == ("observation", "duplicate_metrics")
    rows = _variants(conn, ctx.mission_id)
    assert {r[2] for r in rows} == {"observation"}
    assert sum(1 for r in rows if r[0] == "baseline") == 2
    assert sum(1 for r in rows if r[0] == "candidate") == 2


def _compare_lines(root):
    return [ln for ln in _activity(root).splitlines()
            if "\tbaseline_compare\t" in ln]


@pytest.mark.parametrize("kind,mode,result,detail,outcome", [
    ("not_improved", "shadow", "not_improved", "holdout_not_exceeds",
     "approval"),
    ("improved", "shadow", "improved", None, "approval"),
    ("improved", "enforce", "improved", None, "approval"),
    ("unavailable_unhealthy", "shadow", "unavailable", "baseline_unhealthy",
     "approval"),
    ("unavailable_unhealthy", "enforce", "unavailable", "baseline_unhealthy",
     "approval"),
    ("unavailable_not_evaluable", "shadow", "unavailable",
     "baseline_not_evaluable", "approval"),
    ("not_improved", "enforce", "not_improved", "holdout_not_exceeds",
     "not_improved")])
def test_baseline_compare_line_is_emitted_on_every_terminal_path(
        improve_env, kind, mode, result, detail, outcome):
    app, root = improve_env
    gate, loop, ctx = _drive(app, root, kind, mode)
    (line,) = _compare_lines(root)
    assert f"mission={ctx.mission_id} " in line
    assert f"mode={mode} " in line
    assert f"result={result} " in line
    assert " detail=" in line
    if detail is not None:
        assert f"detail={detail} " in line
    assert {r[2] for r in _variants(app.conn_core, ctx.mission_id)} \
        == {outcome}


def test_baseline_compare_line_is_emitted_on_quality_demotion(improve_env):
    app, root = improve_env
    _drive(app, root, "improved", "shadow", name="conv_a",
           files=_files("conv_a", PY_X))
    gate, loop, ctx = _drive(app, root, "improved", "shadow", name="conv_b",
                             files=_files("conv_b", PY_Y))
    mid = ctx.mission_id
    assert {r[2] for r in _variants(app.conn_core, mid)} == {"observation"}
    mine = [ln for ln in _compare_lines(root) if f"mission={mid} " in ln]
    assert len(mine) == 1
    assert "mode=shadow " in mine[0] and "result=improved " in mine[0]


def test_baseline_compare_line_survives_a_post_commit_publish_failure(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    with patch.object(ImproveLoop, "_publish_report",
                      side_effect=OSError("injected")):
        with pytest.raises(OSError):
            _drive(app, root, "not_improved", "enforce")
    mid = conn.execute("SELECT id FROM missions ORDER BY id DESC LIMIT 1"
                       ).fetchone()[0]
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (mid,)).fetchone()[0] == "completed"
    assert conn.execute(
        "SELECT COUNT(*) FROM backtest_runs WHERE mission_id=? AND "
        "variant='baseline'", (mid,)).fetchone()[0] == 2
    assert len([ln for ln in _compare_lines(root)
                if f"mission={mid} " in ln]) == 1


def test_activity_write_failure_does_not_leak_the_connection(improve_env):
    app, root = improve_env
    from agentic_fx.store.db import connect
    opened = []

    def tracking_connect(*a, **kw):
        c = connect(*a, **kw)
        opened.append(c)
        return c

    real_write = app.activity.write

    def boom(category, event, *a, **kw):
        if event == "baseline_compare":
            raise RuntimeError("activity down")
        return real_write(category, event, *a, **kw)

    with patch("tests.loops.test_improve_candidate_ledger.connect",
               tracking_connect), \
            patch.object(app.activity, "write", boom):
        gate, loop, ctx = _drive(app, root, "improved", "shadow")
    assert opened
    for c in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            c.execute("SELECT 1")


def test_baseline_rows_roll_back_with_the_terminal_tx(improve_env):
    app, root = improve_env
    conn = app.conn_core
    from agentic_fx.store import backtest_runs as store
    real = store.save_harness_run
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if kw.get("variant") == "baseline":
            raise sqlite3.OperationalError("injected")
        return real(*a, **kw)

    with patch.object(store, "save_harness_run", flaky):
        gate, loop, ctx = _drive(app, root, "improved", "enforce")
    assert calls["n"] >= 3
    assert _backtest_rows(conn, ctx.mission_id) == []
    assert _approvals(conn) == []
    assert "baseline_compare" not in _activity(root)


def test_unknown_sandbox_code_propagates(improve_env):
    app, root = improve_env
    conn = app.conn_core

    def mystery(self, conn, **kw):
        raise SandboxError("x", code="mystery_code")

    app.settings.improve.baseline_gate = "shadow"
    with pytest.raises(SandboxError):
        _run(app, root, _artifact("cand"), files=_files("cand"),
             extra=[patch.object(ImproveLoop, "_run_strategy_gate", mystery)],
             seed=False)
    assert _approvals(conn) == []
    assert conn.execute(
        "SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 0


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_sandbox_unavailable_from_replay_is_retryable_failed(
        improve_env, mode):
    app, root = improve_env
    conn = app.conn_core

    def unavailable(self, conn, **kw):
        kw["record_fn"](_candidate_row(
            app.settings, "in_sample", kw["content_hash"], CAND_IN))
        raise SandboxError("x", code="sandbox_unavailable",
                           sandbox_reason="host_preflight")

    app.settings.improve.baseline_gate = mode
    loop, ctx, _ = _run(
        app, root, _artifact("cand"), files=_files("cand"),
        extra=[patch.object(ImproveLoop, "_run_strategy_gate", unavailable)],
        seed=False)
    assert _notice(conn, ctx.mission_id) == ("failed", "sandbox_unavailable")
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (ctx.mission_id,)).fetchone()[0] == "failed"
    assert _approvals(conn) == []


@pytest.mark.parametrize("kind", ["improved", "not_improved", "new_name"])
def test_backtest_call_count_unchanged_by_comparison(improve_env, kind):
    app, root = improve_env
    conn = app.conn_core
    counts = {}
    for mode in ("off", "shadow"):
        # 台帳を使う run_backtest RPC は呼ばない = 候補側の消費は 0 のまま。
        # payload の数が比較の有無で動かないことを同じ入力で確かめる。
        app.settings.improve.baseline_gate = mode
        gate = FakeGate(app.settings, kind)
        _run(app, root, _artifact("cand_" + mode), files=_files("cand_" + mode,
                                                                 PY_X if mode == "off" else PY_Y),
             extra=[patch.object(ImproveLoop, "_run_strategy_gate", gate)],
             seed=False)
        payload = _approvals(conn)[-1]
        counts[mode] = (payload["backtest_call_count"], payload["trial_count"],
                        payload["analysis_call_count"])
    assert counts["off"] == counts["shadow"]


# --- mode の snapshot と再ロック ----------------------------------------------

def test_mode_is_snapshotted_at_prepare(improve_env):
    app, root = improve_env
    app.settings.improve.baseline_gate = "shadow"
    gate = FakeGate(app.settings, "improved")

    def flip(loop, ctx):
        loop._settings.improve.baseline_gate = "enforce"

    _run(app, root, _artifact("cand"), files=_files("cand"),
         extra=[patch.object(ImproveLoop, "_run_strategy_gate", gate)],
         before_commit=flip, seed=False)
    assert [c["compare_mode"] for c in gate.calls] == ["shadow"]
    app.settings.improve.baseline_gate = "enforce"
    gate2 = FakeGate(app.settings, "improved")

    def flip_back(loop, ctx):
        loop._settings.improve.baseline_gate = "shadow"

    _run(app, root, _artifact("cand2"), files=_files("cand2", PY_Y),
         extra=[patch.object(ImproveLoop, "_run_strategy_gate", gate2)],
         before_commit=flip_back, seed=False)
    assert [c["compare_mode"] for c in gate2.calls] == ["enforce"]


def test_replay_wall_clock_is_snapshotted_and_forwarded(improve_env):
    app, root = improve_env
    app.settings.improve.baseline_gate = "shadow"
    app.settings.improve.baseline_replay_wall_clock_max_sec = 123.0
    gate = FakeGate(app.settings, "improved")

    def flip(loop, ctx):
        loop._settings.improve.baseline_replay_wall_clock_max_sec = 7.0

    _run(app, root, _artifact("cand"), files=_files("cand"),
         extra=[patch.object(ImproveLoop, "_run_strategy_gate", gate)],
         before_commit=flip, seed=False)
    assert gate.calls[0]["baseline_replay_wall_clock_max_sec"] == 123.0


def test_ctx_default_is_off_for_direct_construction(tmp_path):
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
    from agentic_fx.loops.improve_run_context import ImproveRunContext
    ctx = ImproveRunContext(
        mission_id=1, run_id=1, staging_dir=tmp_path, source_snapshot_dir=tmp_path,
        allowed_backlog_ids=None, slot_key=None,
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={}), rpc_handlers={})
    assert ctx.baseline_gate_mode == "off"
    assert ctx.baseline_replay_wall_clock_max_sec == 300.0


def test_run_strategy_gate_defaults_to_off(improve_env):
    app, root = improve_env
    loop = ImproveLoop(
        root=root, settings=app.settings, clock=None,
        db_write_conn_factory=lambda: app.conn_core,
        db_readonly_conn_factory=lambda: app.conn_core,
        activity=app.activity, rag=app.rag)
    seen = {}

    def spy(conn, **kw):
        seen.update(kw)
        return None

    with patch("agentic_fx.loops.improve_loop.evaluate_strategy_adoption_gate",
               spy):
        loop._run_strategy_gate(app.conn_core, name="n", pairs=[PAIR],
                                timeframe="1h", content_hash="h", now=NOW,
                                meta=None, resolved=None)
        assert seen["compare_mode"] == "off"
        assert seen["baseline_replay_wall_clock_max_sec"] == 300.0
        loop._run_strategy_gate(app.conn_core, name="n", pairs=[PAIR],
                                timeframe="1h", content_hash="h", now=NOW,
                                meta=None, resolved=None,
                                compare_mode="enforce",
                                baseline_replay_wall_clock_max_sec=9.0)
        assert seen["compare_mode"] == "enforce"
        assert seen["baseline_replay_wall_clock_max_sec"] == 9.0


def test_relock_runs_gate_with_off(improve_env):
    app, root = improve_env
    app.settings.improve.baseline_gate = "enforce"
    gate = FakeGate(app.settings, "new_name")
    with patch.object(ImproveLoop, "_deployed_dir_for",
                      lambda self, name, *, inventory: root), \
         patch("agentic_fx.loops.improve_loop.is_relock_transition",
               lambda *a, **k: True):
        _run(app, root, _artifact("cand"), files=_files("cand"),
             extra=[patch.object(ImproveLoop, "_run_strategy_gate", gate)],
             seed=False)
    assert [c["compare_mode"] for c in gate.calls] == ["off"]
    assert [r for r in _variants(app.conn_core, 1) if r[0] == "baseline"] == []


@pytest.mark.parametrize("exc", [OSError("x"), UnicodeError("x"),
                                 SyntaxError("x")])
def test_relock_check_failure_still_compares(improve_env, exc):
    app, root = improve_env
    app.settings.improve.baseline_gate = "enforce"
    gate = FakeGate(app.settings, "improved")

    def boom(*a, **k):
        raise exc

    with patch.object(ImproveLoop, "_deployed_dir_for",
                      lambda self, name, *, inventory: root), \
         patch("agentic_fx.loops.improve_loop.is_relock_transition", boom):
        _run(app, root, _artifact("cand"), files=_files("cand"),
             extra=[patch.object(ImproveLoop, "_run_strategy_gate", gate)],
             seed=False)
    assert [c["compare_mode"] for c in gate.calls] == ["enforce"]


def test_no_relock_probe_when_mode_is_off(improve_env):
    app, root = improve_env
    app.settings.improve.baseline_gate = "shadow"
    gate = FakeGate(app.settings, "new_name")
    probed = []
    with patch("agentic_fx.loops.improve_loop.is_relock_transition",
               lambda *a, **k: probed.append(1) or False):
        _run(app, root, _artifact("cand"), files=_files("cand"),
             extra=[patch.object(ImproveLoop, "_run_strategy_gate", gate)],
             seed=False)
    assert probed == []
    assert [c["compare_mode"] for c in gate.calls] == ["shadow"]


# --- 3 入口と repeat ---------------------------------------------------------

def test_every_entrance_snapshots_the_mode_in_prepare(
        loop_full, conn, clock):
    from tests.loops.conftest import _prepare_wave_slot
    from agentic_fx.store import backlog as backlog_store
    loop_full._settings.improve.baseline_gate = "enforce"
    loop_full._settings.improve.baseline_replay_wall_clock_max_sec = 42.0
    now = clock.now()
    _prepare_wave_slot(conn, period_key="2026-W34", k=0, now=now)
    backlog_store.add(conn, "idea-for-partition", "user", now)
    conn.commit()
    manual = loop_full.prepare(slot_key=None, now=now)[1]
    with_comment = loop_full.prepare(slot_key=None, now=now,
                                     oneoff_comment="x")[1]
    weekly = loop_full.prepare(slot_key=("2026-W34", 0), now=now)[1]
    for ctx in (manual, with_comment, weekly):
        assert ctx.baseline_gate_mode == "enforce"
        assert ctx.baseline_replay_wall_clock_max_sec == 42.0


def test_repeat_rounds_each_take_their_own_snapshot(loop_full, clock):
    """repeat の各 mission は prepare() で settings を読み直す。1 周目の途中で
    settings を書き換えても 1 周目は変わらず、2 周目は書き換え後の値になる。"""
    settings = loop_full._settings.improve
    now = clock.now()
    settings.baseline_gate = "shadow"
    settings.baseline_replay_wall_clock_max_sec = 11.0
    first = loop_full.prepare(slot_key=None, now=now)[1]
    settings.baseline_gate = "enforce"
    settings.baseline_replay_wall_clock_max_sec = 22.0
    assert first.baseline_gate_mode == "shadow"
    assert first.baseline_replay_wall_clock_max_sec == 11.0
    second = loop_full.prepare(slot_key=None, now=now)[1]
    settings.baseline_gate = "off"
    assert second.baseline_gate_mode == "enforce"
    assert second.baseline_replay_wall_clock_max_sec == 22.0
    assert first.baseline_gate_mode == "shadow"
    assert first is not second


def test_repeat_second_round_commit_uses_its_own_mode(improve_env):
    """commit まで通した 2 周を、1 周目 shadow・2 周目 enforce で回す。"""
    app, root = improve_env
    app.settings.improve.baseline_gate = "shadow"
    gate = FakeGate(app.settings, "not_improved")

    def flip_after_first(loop, ctx):
        loop._settings.improve.baseline_gate = "enforce"

    _run(app, root, _artifact("cand"), files=_files("cand"),
         extra=[patch.object(ImproveLoop, "_run_strategy_gate", gate)],
         before_commit=flip_after_first, seed=False)
    assert len(_approvals(app.conn_core)) == 1
    _run(app, root, _artifact("cand2"), files=_files("cand2", PY_Y),
         extra=[patch.object(ImproveLoop, "_run_strategy_gate", gate)],
         seed=False)
    assert [c["compare_mode"] for c in gate.calls] == ["shadow", "enforce"]
    assert len(_approvals(app.conn_core)) == 1
    assert _last_result(app.conn_core) == "observation:not_submitted"


# --- off / 新名 / indicator の出力が mode に依らない (生の bytes で) --------------

def _raw_activity(root) -> list[str]:
    """activity の行。実時刻の先頭欄だけ落とし、残りは加工しない。"""
    path = root / "logs" / "activity.log"
    lines = path.read_text().splitlines() if path.exists() else []
    return [ln.split("\t", 1)[1] for ln in lines]


def _columns_but_id_and_created_at(conn, table):
    return ", ".join(
        r[1] for r in conn.execute(f"PRAGMA table_info({table})")
        if r[1] not in ("id", "created_at"))


def _fingerprint(app, root, ctx):
    conn = app.conn_core
    mid = ctx.mission_id
    reports_dir = root / "data" / "improve_reports"
    reports = sorted(p.read_bytes() for p in reports_dir.glob("improve-*.md")
                     ) if reports_dir.exists() else []
    tables = {
        table: [tuple(str(v).replace(str(root), "<root>") for v in row)
                for row in conn.execute(
                    f"SELECT {_columns_but_id_and_created_at(conn, table)} "
                    f"FROM {table} ORDER BY id")]
        for table in ("backtest_runs", "missions", "improvement_runs",
                      "candidate_archives")}
    payloads = [r[0] for r in conn.execute(
        "SELECT payload_json FROM approval_requests ORDER BY id")]
    return {
        "tables": tables, "payload": payloads, "last": _last_result(conn),
        "notice": _notice(conn, mid), "reports": reports,
        "activity": _raw_activity(root)}


_NEW_NAME = {}
_INDICATOR = {"artifact": _plugin_artifact("cand", kind="indicator"),
              "files": _indicator()}


@pytest.mark.parametrize("kind,drive_kw", [
    ("new_name", _NEW_NAME), ("new_name", _INDICATOR)],
    ids=["new_name_strategy", "indicator"])
def test_outputs_are_byte_identical_across_modes(
        tmp_path_factory, kind, drive_kw):
    from tests.loops.test_improve_e2e import FakeEmbedding, FakeRunner, FixedClock
    from agentic_fx.service import build_app
    prints = {}
    for index, mode in enumerate(("off", "shadow", "enforce")):
        root = tmp_path_factory.mktemp(f"env{index}")
        _install(root)
        with _no_real_network():
            app = build_app(root, runner=FakeRunner([]), clock=FixedClock(NOW),
                            embedding_fn=FakeEmbedding())
            try:
                gate, loop, ctx = _drive(app, root, kind, mode, **drive_kw)
                print_ = _fingerprint(app, root, ctx)
            finally:
                app.close()
        prints[mode] = print_
    off = prints["off"]
    assert off["payload"], "the candidate must reach approval"
    assert off["reports"] or off["payload"]
    assert off["activity"]
    assert not any("baseline_compare" in ln for ln in off["activity"])
    for mode in ("shadow", "enforce"):
        for key in off:
            assert off[key] == prints[mode][key], (mode, key)


# --- 遮断 8: holdout を含む 8 指標は agent の見る面に出ない ---------------------

def _canary(i, pf, avg_r):
    return {"trades": 600000 + i * 1111, "pf": pf,
            "win_rate": round(0.5113 + i * 0.0371, 4), "avg_r": avg_r,
            "max_drawdown": round(0.0917 + i * 0.0233, 4),
            "total_pnl": round(1000.0 + i * 7.3131, 4),
            "kill_switch_latches": 0, "evaluable": True}


CANARY_METRICS = {
    "cand_in": _canary(1, 3.1716, 0.6137),
    "cand_ho": _canary(2, 2.9473, 0.5281),
    "dep_in": _canary(3, 2.1839, 0.4062),
    "dep_ho_worse": _canary(4, 1.7264, 0.3195),
    "dep_ho_better": _canary(5, 5.2381, 0.8473)}


def _canary_strings() -> set[str]:
    """値の文字列表現に加え、2 桁・3 桁に丸めた表記も探す。"""
    out = set()
    for metrics in CANARY_METRICS.values():
        for key in ("trades", "pf", "win_rate", "avg_r", "max_drawdown",
                    "total_pnl"):
            value = metrics[key]
            out.add(str(value))
            if isinstance(value, float):
                out.add(f"{value:.2f}")
                out.add(f"{value:.3f}")
    assert len(out) > 70
    return out


def _agent_surfaces(app, root, loop, ctx) -> dict[str, str]:
    """終端させた mission の後、agent や通知先が読みうる面の本文を集める。
    比較節を持つ親専有レポートは別扱い (`_report_text`)。"""
    from agentic_fx.loops.improve_context import build_improve_context
    from agentic_fx.ops.contracts import Principal
    from agentic_fx.runners.base import MissionResult
    from tests.loops.test_improve_candidate_ledger import (
        FakeImproveWorkerRunner)
    conn = app.conn_core
    result = MissionResult(status="completed", output={}, transcript=[])
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        next_mission, next_ctx, _ = loop.prepare(slot_key=None, now=NOW)
    ctx_data = build_improve_context(
        conn, settings=app.settings, now=NOW, root=root,
        allowed_backlog_ids=None)

    def dump(sql):
        return json.dumps([dict(r) for r in conn.execute(sql)],
                          ensure_ascii=False, default=str)

    rpc = app.ops.improve_run(Principal.OPERATOR, ctx.mission_id)
    index = root / "plugins" / "_archive" / "INDEX.md"
    return {
        "generated_prompt": next_mission.prompt,
        "improve_context": json.dumps(ctx_data, ensure_ascii=False,
                                      default=str),
        "family_summary": json.dumps(
            ctx_data["improvement_history"]["family_attempts"],
            ensure_ascii=False, default=str),
        "rendered_prompt": loop._render_improve_mission_prompt(
            ctx_data, ctx=next_ctx),
        "rpc_improve_run": json.dumps(rpc, ensure_ascii=False, default=str),
        "mission_output": dump("SELECT * FROM missions"),
        "backlog_last_result": dump("SELECT * FROM improvement_backlog"),
        "improvement_runs": dump("SELECT * FROM improvement_runs"),
        "notices": dump("SELECT * FROM improve_run_notices"),
        "activity": "\n".join(app.activity.tail(10_000)),
        "archive_index": index.read_text(encoding="utf-8")
        if index.exists() else ""}


def _report_text(root) -> str:
    reports_dir = root / "data" / "improve_reports"
    return "\n".join(p.read_text() for p in sorted(
        reports_dir.glob("improve-*.md"))) if reports_dir.exists() else ""


_COMPARISON_SECTION = "## Baseline comparison detail"


@pytest.mark.parametrize("kind,mode", [
    ("not_improved", "enforce"), ("not_improved", "shadow"),
    ("improved", "shadow"), ("improved", "enforce"),
    ("violation", "enforce")])
def test_holdout_metrics_do_not_reach_agent_or_notice_surfaces(
        improve_env, kind, mode):
    app, root = improve_env
    gate, loop, ctx = _drive(app, root, kind, mode,
                             metrics=CANARY_METRICS)
    canaries = _canary_strings()
    surfaces = _agent_surfaces(app, root, loop, ctx)
    for name in ("generated_prompt", "improve_context", "family_summary",
                 "rpc_improve_run", "mission_output", "backlog_last_result",
                 "notices", "activity"):
        assert surfaces[name], f"{name} is empty; the surface was not exercised"
    for name, text in surfaces.items():
        for canary in canaries:
            assert canary not in text, f"{canary} leaked to {name}"
    report = _report_text(root)
    outside_comparison = report.split(_COMPARISON_SECTION)[0]
    for canary in canaries:
        assert canary not in outside_comparison
    conn = app.conn_core
    if kind == "violation":
        assert _backtest_rows(conn, ctx.mission_id) == []
        for canary in canaries:
            assert canary not in report
        return
    # positive control: 値は親専有の置き場には実在する
    stored = "".join(r["metrics_json"] for r in _backtest_rows(
        conn, ctx.mission_id))
    assert str(CANARY_METRICS["cand_in"]["pf"]) in stored
    assert str(CANARY_METRICS["dep_in"]["pf"]) in stored
    if (kind, mode) == ("not_improved", "enforce"):
        assert _COMPARISON_SECTION in report
        assert str(CANARY_METRICS["cand_ho"]["pf"]) in report
        assert str(CANARY_METRICS["dep_ho_better"]["pf"]) in report
    else:
        assert _COMPARISON_SECTION not in report
        (payload,) = _approvals(conn)
        stages = payload["baseline"]["comparison"]["stages"]
        text = json.dumps(stages)
        assert str(CANARY_METRICS["cand_ho"]["pf"]) in text
        holdout_deployed = ("dep_ho_better" if kind == "not_improved"
                            else "dep_ho_worse")
        assert str(CANARY_METRICS[holdout_deployed]["pf"]) in text
        assert str(CANARY_METRICS["dep_in"]["pf"]) in text
