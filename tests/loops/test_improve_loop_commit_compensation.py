from __future__ import annotations

from agentic_fx.loops.improve_run_context import ImproveRunContext
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store import db as db_mod
from agentic_fx.store import improve_runs as improve_runs_store
from agentic_fx.store import missions as missions_store


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
    # commit() は例外でも finally で PERSISTED 化する現行契約。補償は
    # mark_discarded() の RuntimeError に阻まれず DB 終端を続行する。
    ledger.mark_persisted()
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
    assert not staging_dir.exists()
    activity = (tmp_path / "activity.log").read_text()
    assert "mission_failed" in activity
    assert f"mission={mission_id} status=failed" in activity
    assert "reason=commit_crashed:RuntimeError:boom" in activity
