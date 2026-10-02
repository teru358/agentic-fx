"""失敗した backtest の情報が、次回以降の agent が読める面へ流れないこと。

agent が読める面は、backlog / note の `last_result`・実行履歴・次回 mission の
prompt・提案レポート。流れてよいのは公開分類と固定 hint だけで、例外文字列・
stderr・CPU 値・returncode・signal は出ない。実 worker を殺して確かめる。
"""
from __future__ import annotations

import json
import re

import pytest

from agentic_fx.loops.improve_context import build_improve_context
from agentic_fx.runners.base import MissionResult
from agentic_fx.store.db import connect

from tests.fixtures import indicator_wiring as fx
from tests.fixtures.wiring_envs import activity_text as _activity_text
from tests.loops.test_improve_backtest_real_worker import (
    _CASES, _MARKER, _arrange, _call_through_registry, _commit_with)

_CASE_PARAMS = [(k, u, p) for k, u, p, _r in _CASES]

# plugin / worker の死因を直接語る文字列。agent が読める面には出ない。
_FORBIDDEN = (_MARKER, "plugin says no", "timed out", "exited unexpectedly",
              "returncode", "cpu_sec", "signal=", "parent_wait4")


def _table_text(db_path, table: str) -> str:
    c = connect(db_path)
    try:
        rows = c.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        c.close()
    return json.dumps([list(map(str, r)) for r in rows], ensure_ascii=False)


def _agent_readable(loop, root) -> dict[str, str]:
    db = root / "data" / "agentic.db"
    reports = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted((root / "data" / "improve_reports").rglob("*"))
        if p.is_file())
    # 次回 mission の prompt は、本物の prepare が backlog / 履歴から組む。
    mission, ctx, _runner = loop.prepare(slot_key=None, now=fx.NOW)
    # prompt 表は note の last_result を載せないが、prompt の元になる context
    # 辞書には載る。両方を読み手の面として扱う。
    c = connect(db)
    try:
        context = json.dumps(build_improve_context(
            c, settings=loop._settings, now=fx.NOW, root=root,
            allowed_backlog_ids=ctx.allowed_backlog_ids,
            inventory_view=ctx.inventory_view), default=str,
            ensure_ascii=False)
    finally:
        c.close()
    return {
        "context": context,
        "backlog": _table_text(db, "improvement_backlog"),
        "runs": _table_text(db, "improvement_runs"),
        "missions": _table_text(db, "missions"),
        "reports": reports,
        "next_prompt": mission.prompt,
    }


def _assert_clean(sinks: dict[str, str]) -> None:
    for name, text in sinks.items():
        for needle in _FORBIDDEN:
            assert needle not in text, (name, needle)


@pytest.mark.parametrize("kind,updates,public", _CASE_PARAMS)
def test_failed_backtest_does_not_reach_notes_history_reports_or_next_prompt(
        tmp_path, kind, updates, public):
    loop, ctx, activity = _arrange(tmp_path, kind, updates)
    _call_through_registry(ctx)
    conn = loop._db_write_conn_factory()
    try:
        loop._finalize_failed_mission(
            conn, ctx=ctx, now=fx.NOW,
            result=MissionResult(
                "failed", None, [],
                reason="tool_budget_abort:tool_errors:run_backtest"))
    finally:
        conn.close()

    sinks = _agent_readable(loop, tmp_path)
    _assert_clean(sinks)
    # 流れる経路が実在することの確認: note は呼び出し回数だけを載せる。
    assert "run_backtest=1" in sinks["backlog"]
    assert "run_backtest=1" in sinks["context"]
    assert sinks["next_prompt"]
    # 失敗した評価は成績として保存されない。
    assert json.loads(_table_text(tmp_path / "data" / "agentic.db",
                                  "backtest_runs")) == []
    # 台帳から飛ばした行の activity は公開分類だけを載せる。
    skipped = [l.split("\t")[3] for l in _activity_text(activity).splitlines()
               if "ledger_entry_skipped_error" in l]
    assert skipped == [f"mission=1 kind=run_backtest error={public!r}"]


@pytest.mark.parametrize("kind,updates,public", _CASE_PARAMS)
def test_commit_gate_worker_failure_does_not_reach_agent_readable_surfaces(
        tmp_path, kind, updates, public):
    loop, conn, root, activity, error, ctx = _commit_with(
        tmp_path, kind, updates)
    assert error is not None
    loop.compensate_commit_failure(
        ctx=ctx, now=fx.NOW, exc=error, slot_terminalize=True)

    sinks = _agent_readable(loop, root)
    _assert_clean(sinks)
    # 補償は固定文言だけを残す。例外の中身は note に入らない。
    assert not re.search(r"commit_crashed", sinks["backlog"] + sinks["next_prompt"])
    # stderr は activity にも出ない。
    assert _MARKER not in _activity_text(activity)
