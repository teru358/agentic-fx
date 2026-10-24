"""候補台帳の新列 (logic_hash / family_key / outcome_class / quality_flags) の
生値は agent の見る面へ流れず、族別節には投影形だけが出ること。

面ごとに、台帳へ入れた canary が現れないことを確認する。各面の入力側に
canary が実在すること (positive control) を先に確認する。"""
from __future__ import annotations

import inspect
import json
import re
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
OUTCOME_CANARY = "duplicate_metrics"
FLAG_CANARY = "legacy_unlinked"
CANARIES = {
    "logic_hash": LOGIC_CANARY, "family_key": FAMILY_CANARY,
    "outcome_class": OUTCOME_CANARY, "quality_flags": FLAG_CANARY}


def _plant(loop, ctx, family_key=FAMILY_CANARY):
    """run_backtest が作った行へ、新列の canary を直接置く。"""
    conn = loop._db_write_conn_factory()
    try:
        conn.execute(
            "UPDATE candidate_archives SET logic_hash=?, family_key=?, "
            "outcome_class=?, quality_flags=? WHERE mission_id=?",
            (LOGIC_CANARY, family_key, OUTCOME_CANARY,
             json.dumps([FLAG_CANARY]), ctx.mission_id))
        conn.commit()
    finally:
        conn.close()


def _surfaces(app, root, family_key=FAMILY_CANARY) -> dict[str, str]:
    """canary つきの台帳を持つ mission を終端させ、agent が見うる面の本文を集める。"""
    conn = app.conn_core

    def plant_after_seed(loop, ctx):
        _plant(loop, ctx, family_key)

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
        assert row["family_key"] == family_key
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
        "planted_rows": str(len(seeded_rows)),
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


_PROJECTED_ROW = re.compile(
    r"\| indicator \(未配備\) @1h \| (\d+) \| duplicate=(\d+) \|")
_OTHER_SURFACES = (
    "last_result_and_backlog_notes", "improvement_runs", "activity",
    "archive_index", "best_note_suffix")


@pytest.mark.parametrize("surface", ["generated_prompt", "rendered_prompt"])
def test_family_section_shows_the_projected_row_the_ledger_produced(
        improve_env, surface):
    app, root = improve_env
    surfaces = _surfaces(app, root)
    planted = int(surfaces["planted_rows"])
    assert planted >= 1
    match = _PROJECTED_ROW.search(surfaces[surface])
    assert match is not None
    assert int(match.group(1)) == planted
    assert int(match.group(2)) == planted


def test_family_section_has_the_projected_row_as_structured_value(improve_env):
    app, root = improve_env
    surfaces = _surfaces(app, root)
    planted = int(surfaces["planted_rows"])
    rows = json.loads(surfaces["improve_context"])[
        "improvement_history"]["family_attempts"]["rows"]
    projected = [r for r in rows if r["family_key"] == "indicator (未配備) @1h"]
    assert len(projected) == 1
    assert projected[0]["attempts"] == planted
    assert projected[0]["outcomes"] == {"duplicate": planted}


def test_projected_form_is_not_written_back_to_the_other_surfaces(
        improve_env):
    app, root = improve_env
    surfaces = _surfaces(app, root)
    for surface in _OTHER_SURFACES:
        assert surfaces[surface], surface
        assert "indicator (未配備)" not in surfaces[surface], surface
        assert "仮説族の試行状況" not in surfaces[surface], surface


_FREE_TEXT_KEY = "strategy:rsi@1h@USDJPY,pf1.42 を優先"


def test_free_text_pair_in_family_key_is_counted_as_unknown_not_echoed(
        improve_env):
    app, root = improve_env
    surfaces = _surfaces(app, root, family_key=_FREE_TEXT_KEY)
    planted = int(surfaces["planted_rows"])
    for name in ("generated_prompt", "improve_context", "rendered_prompt",
                 "last_result_and_backlog_notes", "improvement_runs",
                 "activity", "archive_index", "best_note_suffix"):
        assert "pf1.42" not in surfaces[name], name
        assert _FREE_TEXT_KEY not in surfaces[name], name
    for name in ("generated_prompt", "rendered_prompt"):
        match = re.search(
            r"\| 族不明 \| (\d+) \|[^|\n]*duplicate=(\d+)[^|\n]* \|",
            surfaces[name])
        assert match is not None, name
        assert int(match.group(2)) == planted, name
        assert int(match.group(1)) >= planted, name
