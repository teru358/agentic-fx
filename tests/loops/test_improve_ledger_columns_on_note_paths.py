"""台帳の新列が、出力不正で終端した mission の backlog note に流れない。"""
from __future__ import annotations

import json

from tests.loops.test_improve_candidate_ledger import _run, improve_env
from tests.loops.test_improve_ledger_columns_not_on_agent_surfaces import (
    CANARIES, _plant)


def test_output_invalid_note_carries_no_new_ledger_column(improve_env):
    app, root = improve_env
    _run(app, root, {"bogus": 1},
         before_commit=lambda loop, ctx: _plant(loop, ctx))
    backlog = json.dumps(
        [dict(r) for r in app.conn_core.execute(
            "SELECT * FROM improvement_backlog")], ensure_ascii=False,
        default=str)
    # 台帳の best を引く経路を実際に通った (空振りを漏れなしと誤認しない)
    assert "best=" in backlog
    for column, canary in CANARIES.items():
        assert canary not in backlog, f"{column} leaked into a backlog note"
