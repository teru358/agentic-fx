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
from tests.loops.test_improve_candidate_ledger import _run, _strategy
from tests.loops.test_improve_e2e import (
    NOW, _fake_record_kwargs, _no_real_network, _install, improve_env)  # noqa: F401
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

    def __init__(self, settings, kind):
        self.settings = settings
        self.kind = kind
        self.calls: list[dict] = []

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
        in_row = _candidate_row(self.settings, "in_sample", content_hash,
                                CAND_IN)
        ho_row = _candidate_row(self.settings, "holdout_gate", content_hash,
                                CAND_HO)
        record(in_row)
        record(ho_row)
        base = {"ref_plugin_ref": "plugins/cand", "variant": "baseline"}
        comparison, rows = None, ()
        if mode != "off" and self.kind != "new_name":
            base["base_content_hash"] = BASE_HASH
            if self.kind in ("improved", "not_improved"):
                holdout = (DEPLOYED_HO_BETTER if self.kind == "not_improved"
                           else DEPLOYED_HO_WORSE)
                comparison = _decide_comparison(
                    mode=mode, pairs=[PAIR], stages={
                        "in_sample": {"candidate": {PAIR: CAND_IN},
                                      "deployed": {PAIR: DEPLOYED_IN}},
                        "holdout": {"candidate": {PAIR: CAND_HO},
                                    "deployed": {PAIR: holdout}}})
                rows = _baseline_rows(self.settings, content_hash,
                                      DEPLOYED_IN, holdout)
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
            candidate_metrics={PAIR: CAND_IN}, holdout_metrics={PAIR: CAND_HO},
            comparison=comparison,
            not_improved=bool(
                comparison and comparison["verdict"] == "not_improved"),
            baseline_rows=rows)


def _drive(app, root, kind, mode, *, name="cand", files=None, **gate_kw):
    app.settings.improve.baseline_gate = mode
    gate = FakeGate(app.settings, kind, **gate_kw)
    outcomes: list[str] = []
    real = ImproveLoop._persist_ledger_in_tx

    def spy(self, conn, **kw):
        outcomes.append(kw["mission_outcome"])
        return real(self, conn, **kw)

    loop, ctx, _ = _run(
        app, root, _artifact(name), files=files or _files(name),
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


# --- 3 入口 ---------------------------------------------------------------

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


# --- off / 新名 / 再ロックの出力が mode に依らない ---------------------------------

def _fingerprint(app, root, ctx):
    conn = app.conn_core
    mid = ctx.mission_id
    activity = [ln.split(" ", 3)[-1] for ln in _activity(root).splitlines()]
    reports = sorted(p.read_text() for p in
                     (root / "data" / "improve_reports").glob("improve-*.md")) \
        if (root / "data" / "improve_reports").exists() else []
    rows = [(r["variant"], r["scope"], r["mission_outcome"], r["metrics_json"])
            for r in _backtest_rows(conn, mid)]
    return {
        "rows": rows, "payload": _approvals(conn), "last": _last_result(conn),
        "notice": _notice(conn, mid), "reports": reports,
        "activity": [a for a in activity if "baseline_compare" not in a],
        "no_compare": not any("baseline_compare" in a for a in activity)}


@pytest.mark.parametrize("kind", ["new_name"])
def test_new_name_is_identical_across_modes(tmp_path_factory, kind):
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
                gate, loop, ctx = _drive(app, root, kind, mode)
                print_ = _fingerprint(app, root, ctx)
            finally:
                app.close()
        if mode == "off":
            assert [c["compare_mode"] for c in gate.calls] == ["off"]
        prints[mode] = print_
    for mode in ("shadow", "enforce"):
        a, b = dict(prints["off"]), dict(prints[mode])
        for key in ("rows", "payload", "last", "notice", "no_compare"):
            assert a[key] == b[key], key
        # activity は mission の id と時刻以外が同一
        assert a["activity"] == b["activity"]
        assert a["reports"] == b["reports"]
    assert prints["off"]["no_compare"]
