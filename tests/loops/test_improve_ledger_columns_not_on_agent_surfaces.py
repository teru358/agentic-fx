"""候補台帳の新列 (logic_hash / family_key / outcome_class / quality_flags) が
agent の見る面へ流れないこと。

面ごとに、台帳へ入れた canary が現れないことを確認する。各面の入力側に
canary が実在すること (positive control) を先に確認する。"""
from __future__ import annotations

import inspect
import json
from unittest.mock import patch

import pytest

from agentic_fx.loops import improve_loop
from agentic_fx.loops.improve_context import build_improve_context
from agentic_fx.runners.base import MissionResult

from tests.loops.test_improve_candidate_ledger import (
    NOW, FakeImproveWorkerRunner, _plugin_artifact, _rows, _run, _strategy,
    _fake_in_sample_metrics, improve_env)

LOGIC_CANARY = "v1:logiccanary7f3a91d2"
FAMILY_CANARY = "indicator:familycanary_c2e84b@1h"
OUTCOME_CANARY = "family_redirect"
FLAG_CANARY = "legacy_unlinked"
CANARIES = {
    "logic_hash": LOGIC_CANARY, "family_key": FAMILY_CANARY,
    "outcome_class": OUTCOME_CANARY, "quality_flags": FLAG_CANARY}


def _plant(loop, ctx):
    """run_backtest が作った行へ、新列の canary を直接置く。"""
    conn = loop._db_write_conn_factory()
    try:
        conn.execute(
            "UPDATE candidate_archives SET logic_hash=?, family_key=?, "
            "outcome_class=?, quality_flags=? WHERE mission_id=?",
            (LOGIC_CANARY, FAMILY_CANARY, OUTCOME_CANARY,
             json.dumps([FLAG_CANARY]), ctx.mission_id))
        conn.commit()
    finally:
        conn.close()


def _surfaces(app, root) -> dict[str, str]:
    """canary つきの台帳を持つ mission を終端させ、agent が見うる面の本文を集める。"""
    conn = app.conn_core

    def plant_after_seed(loop, ctx):
        _plant(loop, ctx)

    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(), in_sample=_fake_in_sample_metrics(29),
        before_commit=plant_after_seed)
    rows = _rows(conn, ctx.mission_id)
    seeded_rows = [r for r in rows if r["id"] in seeded]
    # positive control: 入力側 (台帳) に 4 つの canary が実在する
    assert seeded_rows
    for row in seeded_rows:
        assert row["logic_hash"] == LOGIC_CANARY
        assert row["family_key"] == FAMILY_CANARY
        assert row["outcome_class"] == OUTCOME_CANARY
        assert row["quality_flags"] == [FLAG_CANARY]

    result = MissionResult(status="completed", output={}, transcript=[])
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        next_mission, next_ctx, _ = loop.prepare(slot_key=None, now=NOW)
    ctx_data = build_improve_context(
        conn, settings=app.settings, now=NOW, root=root,
        allowed_backlog_ids=None)
    backlog = [dict(r) for r in conn.execute(
        "SELECT * FROM improvement_backlog")]
    runs = [dict(r) for r in conn.execute("SELECT * FROM improvement_runs")]
    index = root / "plugins" / "_archive" / "INDEX.md"
    return {
        "generated_prompt": next_mission.prompt,
        "improve_context": json.dumps(ctx_data, ensure_ascii=False,
                                      default=str),
        "rendered_prompt": loop._render_improve_mission_prompt(
            ctx_data, ctx=next_ctx),
        "last_result_and_backlog_notes": json.dumps(
            backlog, ensure_ascii=False, default=str),
        "improvement_runs": json.dumps(runs, ensure_ascii=False, default=str),
        "activity": "\n".join(app.activity.tail(10_000)),
        "archive_index": index.read_text(encoding="utf-8")
        if index.exists() else "",
        "best_note_suffix": improve_loop.ImproveLoop._best_note_suffix(
            improve_loop.best_candidate(seeded_rows)),
    }


@pytest.mark.parametrize("surface", [
    "generated_prompt", "improve_context", "rendered_prompt",
    "last_result_and_backlog_notes", "improvement_runs", "activity",
    "archive_index", "best_note_suffix"])
def test_new_ledger_columns_do_not_reach_the_agent_surface(improve_env,
                                                           surface):
    app, root = improve_env
    surfaces = _surfaces(app, root)
    text = surfaces[surface]
    assert text, f"{surface} is empty; the surface was not exercised"
    for column, canary in CANARIES.items():
        assert canary not in text, f"{column} leaked to {surface}"


def test_surfaces_that_read_the_ledger_actually_ran(improve_env):
    """note と INDEX は台帳の行から best を引く面。実際にその経路を通って
    いること (空振りで canary が無いのを漏れなしと誤認しない)。"""
    app, root = improve_env
    surfaces = _surfaces(app, root)
    assert "best=" in surfaces["best_note_suffix"]
    assert "best=" in surfaces["archive_index"]


@pytest.mark.parametrize("function", [
    improve_loop.best_candidate, improve_loop._best_label,
    improve_loop.ImproveLoop._best_note_suffix,
    improve_loop.ImproveLoop._append_archive_index])
def test_best_note_and_index_read_paths_do_not_reference_the_new_columns(
        function):
    source = inspect.getsource(function)
    for column in CANARIES:
        assert column not in source, (
            f"{function.__qualname__} references {column}")
