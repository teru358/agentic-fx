"""改善 mission の履歴検査: 非提出で終端し、agent 面には固定の `not_submitted` だけを返す。"""
from __future__ import annotations

import json

import pytest

from agentic_fx.backtest import history_health as hh
from agentic_fx.loops.improve_context import NOT_SUBMITTED_LAST_RESULT
from tests.loops.test_improve_agent_surface import _render, _strict_settings
from tests.loops.test_improve_candidate_ledger import (
    _expect_rows, _fake_holdout_metrics, _fake_in_sample_metrics, _plugin_artifact, _run,
    _strategy, improve_env)

CANARY = "CANARY-8c41-2026-11-04T05:55"


@pytest.mark.parametrize("verdict", [hh.HISTORY_STALE, hh.HISTORY_GAP, hh.HISTORY_UNAVAILABLE])
def test_a_history_failure_files_nothing_and_the_agent_sees_only_not_submitted(
        improve_env, history_guard_fake, verdict, tmp_path):
    history_guard_fake.verdict = verdict
    app, root = improve_env
    holdout_calls = []
    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand", kind="strategy"), files=_strategy(),
        in_sample=_fake_in_sample_metrics(40),
        holdout_fn=lambda *a, **kw: holdout_calls.append(1) or _fake_holdout_metrics()(*a, **kw))
    conn = app.conn_core
    _expect_rows(conn, ctx, seeded, "gate_failed")
    assert holdout_calls == []
    assert conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0
    backlog_id = conn.execute("SELECT backlog_id FROM improvement_runs WHERE id=?",
                              (ctx.run_id,)).fetchone()[0]
    assert conn.execute("SELECT last_result FROM improvement_backlog WHERE id=?",
                        (backlog_id,)).fetchone()[0] == NOT_SUBMITTED_LAST_RESULT
    notice = conn.execute(
        "SELECT result_code, reason_code FROM improve_run_notices WHERE mission_id=?",
        (ctx.mission_id,)).fetchone()
    assert tuple(notice) == ("gate_failed", "history_not_ready")
    rendered, _data = _render(conn, _strict_settings(), root)
    for word in ("history_stale", "history_gap", "history_check_unavailable",
                 "history_not_ready", "history_guard"):
        assert word not in rendered, word
    assert NOT_SUBMITTED_LAST_RESULT in rendered


def test_nothing_of_the_check_reaches_the_agent_surfaces(improve_env, history_guard_fake):
    """検査の詳細 (日時・本数・pair・理由) は人間専用。agent が読める面に出ない。"""
    from tests.history_support import ready_health
    original = history_guard_fake.verdict
    history_guard_fake.verdict = hh.HISTORY_STALE
    app, root = improve_env
    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand", kind="strategy"), files=_strategy(),
        in_sample=_fake_in_sample_metrics(40), holdout_fn=_fake_holdout_metrics())
    conn = app.conn_core
    rendered, data = _render(conn, _strict_settings(), root)
    agent_view = rendered + json.dumps(data, default=str)
    backlog = [tuple(r) for r in conn.execute(
        "SELECT last_result, origin_outcome FROM improvement_backlog")]
    index = root / "plugins" / "_archive" / "INDEX.md"
    surfaces = [agent_view, json.dumps(backlog)]
    if index.exists():
        surfaces.append(index.read_text(encoding="utf-8"))
    for text in surfaces:
        assert CANARY not in text and "2026-11" not in text.replace("2026-11-0", "")
        assert "history_stale" not in text and "history_gap" not in text
    assert original == hh.HISTORY_READY


def test_a_submitted_candidate_carries_the_gates_evidence_in_its_payload(
        improve_env, history_guard_fake):
    from agentic_fx.backtest import history_evidence as ev
    from tests.loops.test_improve_candidate_ledger import _payload

    app, root = improve_env
    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand", kind="strategy"), files=_strategy(),
        in_sample=_fake_in_sample_metrics(40), holdout_fn=_fake_holdout_metrics())
    _expect_rows(app.conn_core, ctx, seeded, "pending")
    payload = _payload(app.conn_core)
    assert len(history_guard_fake.calls) == 1
    evidence = payload["history_guard"]
    assert evidence["verdict"] == "ready"
    assert history_guard_fake.calls[0]["no_bars_path"] == ev.no_bars_path(root)
    check = ev.verify_evidence(
        evidence, eval_source=payload["eval_source"], base_interval=payload["base_interval"],
        no_bars_path=ev.no_bars_path(root))
    assert check.ok, check
