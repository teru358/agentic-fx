from __future__ import annotations

import pytest

from agentic_fx.loops.improve_run_context import ImproveRunContext
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store import db as db_mod
from agentic_fx.store import improve_runs as improve_runs_store
from agentic_fx.store import missions as missions_store


def test_commit_exception_leaves_ledger_discardable(loop_full, monkeypatch):
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ctx = ImproveRunContext(
        mission_id=1, run_id=1, staging_dir=loop_full._root / "staging",
        source_snapshot_dir=loop_full._root / "source",
        allowed_backlog_ids=None, slot_key=None, ledger=ledger,
        rpc_handlers={})
    monkeypatch.setattr(
        loop_full, "_inspect_output",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("commit boom")))

    from agentic_fx.runners.base import Mission, MissionResult
    mission = Mission(prompt="x", tools=[], output_schema={}, max_turns=1,
                      timeout_sec=1)
    result = MissionResult(status="completed", output={}, transcript=[])
    with pytest.raises(RuntimeError, match="commit boom"):
        loop_full.commit(mission=mission, ctx=ctx, result=result,
                         now=loop_full._clock.now())

    ledger.mark_discarded()
    assert ledger.state() == "DISCARDED"


def test_compensate_commit_failure_terminalizes_and_is_idempotent(
        loop_no_seam, tmp_path, clock):
    loop, db_path = loop_no_seam
    conn = db_mod.connect(db_path)
    backlog_id = backlog_store.add(conn, "selected idea", "user", clock.now())
    mission_id = missions_store.start(
        conn, "improve", "local", "model", clock.now(), commit=False)
    run_id = improve_runs_store.start(
        conn, backlog_id, clock.now(), mission_id=mission_id, commit=False)
    assert backlog_store.select_for_mission(
        conn, backlog_id, now=clock.now(), commit=False)
    conn.commit()
    conn.close()

    staging_dir = tmp_path / "plugins" / "_staging" / str(mission_id)
    staging_dir.mkdir(parents=True)
    (staging_dir / "candidate.py").write_text("candidate = True\n")
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ledger.freeze()
    # commit() の例外時と同じ FROZEN 状態から補償を開始する。補償は台帳を
    # 永続化してから DB と staging の pre-commit state を収束させる。
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=staging_dir / "_snapshot_src",
        allowed_backlog_ids=None, slot_key=None, ledger=ledger,
        rpc_handlers={})

    loop.compensate_commit_failure(
        ctx=ctx, now=clock.now(), exc=RuntimeError("boom"))
    loop.compensate_commit_failure(
        ctx=ctx, now=clock.now(), exc=RuntimeError("boom"))

    check = db_mod.connect(db_path)
    mission = check.execute(
        "SELECT status, finished_at FROM missions WHERE id=?", (mission_id,)
    ).fetchone()
    run = check.execute(
        "SELECT finished_at FROM improvement_runs WHERE id=?", (run_id,)
    ).fetchone()
    backlog = check.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?",
        (backlog_id,),
    ).fetchone()
    check.close()

    assert mission["status"] == "failed"
    assert mission["finished_at"] is not None
    assert run["finished_at"] is not None
    assert backlog["status"] == "observation"
    assert backlog["last_result"] == "interrupted"
    assert ledger.state() == "PERSISTED"
    assert not staging_dir.exists()
    activity = (tmp_path / "activity.log").read_text()
    assert "mission_failed" in activity
    assert f"mission={mission_id} status=failed" in activity
    assert "reason=commit_crashed:RuntimeError:boom" in activity


def test_commit_exception_marks_ledger_persist_failed(loop_full, monkeypatch):
    """ローカル T3 1 周目 #Y9 (2026-09-10): `commit()` が例外で抜けるとき、
    FROZEN の台帳は `PERSIST_FAILED` へ遷移する (設計 §1 L1)。

    既存 `test_commit_exception_leaves_ledger_discardable` はテスト側で
    `mark_discarded()` を呼ぶだけで、`mark_discarded` は FROZEN からも
    PERSIST_FAILED からも合法なため、`except BaseException` 内の
    `if ctx.ledger.state() == "FROZEN": ctx.ledger.mark_persist_failed()`
    を削る変異がフルスイートでも生存していた。
    """
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ctx = ImproveRunContext(
        mission_id=1, run_id=1, staging_dir=loop_full._root / "staging",
        source_snapshot_dir=loop_full._root / "source",
        allowed_backlog_ids=None, slot_key=None, ledger=ledger,
        rpc_handlers={})
    monkeypatch.setattr(
        loop_full, "_inspect_output",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("commit boom")))

    from agentic_fx.runners.base import Mission, MissionResult
    mission = Mission(prompt="x", tools=[], output_schema={}, max_turns=1,
                      timeout_sec=1)
    result = MissionResult(status="completed", output={}, transcript=[])
    with pytest.raises(RuntimeError, match="commit boom"):
        loop_full.commit(mission=mission, ctx=ctx, result=result,
                         now=loop_full._clock.now())

    assert ledger.state() == "PERSIST_FAILED"
