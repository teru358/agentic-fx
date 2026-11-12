"""改善の提出が共有 gate へ渡す引数 (dataset・pairs・now・root 基準の無足日一覧) を、
履歴検査を差し替えず実 sqlite の履歴で確かめる。実 backtest だけは差し替える。"""
from __future__ import annotations

import pytest

from agentic_fx.backtest import history_evidence as ev
from tests.history_support import fill_history, hole_day, write_no_bars
from tests.loops.test_improve_candidate_ledger import (
    _expect_rows, _fake_holdout_metrics, _fake_in_sample_metrics, _payload,
    _plugin_artifact, _run, _strategy, improve_env)
from tests.loops.test_improve_e2e import NOW

SOURCE = "dukascopy"


def _go(app, root, scenario, holdout_calls):
    app.settings.backtest.base_interval = "5m"
    app.settings.backtest.holdout_months = 1
    fill_history(app.conn_core, source=SOURCE, pairs=["USDJPY"], now=NOW, scenario=scenario)
    return _run(
        app, root, _plugin_artifact("cand", kind="strategy"), files=_strategy(),
        in_sample=_fake_in_sample_metrics(40),
        holdout_fn=lambda *a, **kw: holdout_calls.append(1)
        or _fake_holdout_metrics()(*a, **kw))


def _reasons(conn, ctx):
    return conn.execute(
        "SELECT result_code, reason_code FROM improve_run_notices WHERE mission_id=?",
        (ctx.mission_id,)).fetchone()


@pytest.mark.real_history_guard
def test_a_healthy_history_submits_and_the_payload_carries_the_evidence(improve_env):
    app, root = improve_env
    calls: list = []
    loop, ctx, seeded = _go(app, root, "normal", calls)
    _expect_rows(app.conn_core, ctx, seeded, "pending")
    assert calls == [1]
    evidence = _payload(app.conn_core)["history_guard"]
    assert evidence["verdict"] == "ready" and evidence["pairs"] == ["USDJPY"]
    assert evidence["checked_at"] == NOW.isoformat()
    assert evidence["dataset"] == {"source": SOURCE, "base_interval": "5m"}
    assert ev.verify_evidence(evidence, eval_source=SOURCE, base_interval="5m",
                              pairs=["USDJPY"], no_bars_path=ev.no_bars_path(root)).ok


@pytest.mark.real_history_guard
@pytest.mark.parametrize("scenario,kind", [("stale", "history_stale"), ("gap", "history_gap")])
def test_a_stale_or_gapped_history_files_no_request_and_names_its_reason(
        improve_env, scenario, kind):
    app, root = improve_env
    calls: list = []
    loop, ctx, seeded = _go(app, root, scenario, calls)
    conn = app.conn_core
    _expect_rows(conn, ctx, seeded, "gate_failed")
    assert calls == []
    assert conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0
    assert tuple(_reasons(conn, ctx)) == ("gate_failed", "history_not_ready")
    activity = (root / "logs" / "activity.log").read_text()
    assert f"reason={kind}" in activity


@pytest.mark.real_history_guard
def test_the_no_bars_list_is_read_from_the_loops_root(improve_env):
    app, root = improve_env
    day = hole_day(NOW)[0].date()
    write_no_bars(root, source=SOURCE, symbol="USDJPY", day=day)
    calls: list = []
    loop, ctx, seeded = _go(app, root, "gap", calls)
    _expect_rows(app.conn_core, ctx, seeded, "pending")
    entries = _payload(app.conn_core)["history_guard"]["no_bars_entries"]
    assert [(e["symbol"], e["date_utc"]) for e in entries] == [("USDJPY", day.isoformat())]
