"""最終 artifact の材料と提出 tx まわりの境界を固定する。"""
from __future__ import annotations

from unittest.mock import patch

from tests.loops.test_improve_candidate_ledger import (
    _PASSING_INDICATOR_CONFIG, _PASSING_INDICATOR_PY,
    _PASSING_INDICATOR_TEST, _expect_rows, _plugin_artifact,
    _run, improve_env)


def test_deep_expression_candidate_does_not_fail_the_mission(improve_env):
    """ロジック hash を計算できない候補は、hash なしの行として通常の終端に進む。"""
    app, root = improve_env
    deep = "_deep = " + " + ".join(["1"] * 3000) + "\n"
    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand"),
        files=("cand", _PASSING_INDICATOR_PY + deep, _PASSING_INDICATOR_CONFIG,
               _PASSING_INDICATOR_TEST))
    status = app.conn_core.execute(
        "SELECT status FROM missions WHERE id=?", (ctx.mission_id,)
    ).fetchone()[0]
    assert status == "completed"
    extra = _expect_rows(app.conn_core, ctx, seeded, "pending")
    assert extra["logic_hash"] is None
