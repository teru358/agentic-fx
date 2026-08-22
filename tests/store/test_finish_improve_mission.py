"""finish_improve_mission — slot+mission+run+backlog を単一 tx で終端する
唯一の terminal helper (設計書 §4.1、§8.1-21/22)。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.store import backlog, improve_runs, improve_waves, missions
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)


def _prepare_scheduler_mission(c, *, period_key="2026-W34", k=0):
    improve_waves.create_wave_and_slots(c, period_key=period_key, now=NOW, expected=1)
    mid = missions.start(c, "improve", "local", "m", now=NOW, commit=False)
    rid = improve_runs.start(c, None, now=NOW, mission_id=mid, commit=False)
    improve_waves.claim_slot(c, period_key=period_key, k=k, mission_id=mid,
                             now=NOW, commit=False)
    c.commit()
    return mid, rid


def test_tx0_missions_run_slot_created_in_one_tx(tmp_path):
    """§8.1-23: Tx-0 = missions.start + run INSERT + slot claim の 1 tx。
    3 つとも揃って生まれることを確認 (この関数自体が Tx-0 の逐語実装)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    mid, rid = _prepare_scheduler_mission(c)
    assert c.execute("SELECT status FROM missions WHERE id=?", (mid,)).fetchone()["status"] == "running"
    assert c.execute("SELECT id FROM improvement_runs WHERE id=?", (rid,)).fetchone() is not None
    slot = improve_waves.get_slot(c, period_key="2026-W34", k=0)
    assert slot["status"] == "claimed" and slot["mission_id"] == mid


def test_tx0_rolls_back_atomically_on_mid_failure(tmp_path):
    """§8.1-23 の否定側: Tx-0 の 3 ステップの途中で例外が起きたら
    wave/slot も mission も一切残らないこと (`commit=False` を使わず
    個別に commit していたら、この test は wave/slot だけ残ってしまう)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW,
                                        expected=1, commit=False)
    mid = missions.start(c, "improve", "local", "m", now=NOW, commit=False)
    improve_runs.start(c, None, now=NOW, mission_id=mid, commit=False)
    with pytest.raises(sqlite3.IntegrityError):
        # 同一 mission_id で 2 件目の run を作ろうとする — 部分 UNIQUE
        # (ix_improvement_runs_mission_id) が衝突し、tx を巻き込む。
        improve_runs.start(c, None, now=NOW, mission_id=mid, commit=False)
    c.rollback()
    assert c.execute("SELECT 1 FROM missions WHERE id=?", (mid,)).fetchone() is None
    assert c.execute(
        "SELECT 1 FROM improve_waves WHERE period_key='2026-W34'").fetchone() is None
    assert c.execute(
        "SELECT 1 FROM improve_wave_slots WHERE wave_period_key='2026-W34'"
    ).fetchone() is None


def test_finish_improve_mission_success_path_updates_all_four(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "idea", "user", NOW)
    mid, rid = _prepare_scheduler_mission(c)
    improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    backlog.select_for_mission(c, bid, now=NOW)
    improve_runs.bind_backlog(c, rid, bid)

    missions.finish_improve_mission(
        c, mission_id=mid, run_id=rid, slot_key=("2026-W34", 0),
        mission_status="completed", run_result="report",
        backlog_transition={"backlog_id": bid, "status": "done",
                            "last_result": "report:reports/x.md"},
        now=NOW, output={"ok": True}, transcript=[])

    assert c.execute("SELECT status FROM missions WHERE id=?", (mid,)).fetchone()["status"] == "completed"
    run = c.execute("SELECT finished_at, result FROM improvement_runs WHERE id=?",
                    (rid,)).fetchone()
    assert run["finished_at"] is not None and run["result"] == "report"
    assert improve_waves.get_slot(c, period_key="2026-W34", k=0)["status"] == "done"
    bl = c.execute("SELECT status FROM improvement_backlog WHERE id=?", (bid,)).fetchone()
    assert bl["status"] == "done"


def test_finish_improve_mission_approval_path_persists_approval_id(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "idea", "user", NOW)
    mid, rid = _prepare_scheduler_mission(c)
    improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    backlog.select_for_mission(c, bid, now=NOW)
    improve_runs.bind_backlog(c, rid, bid)

    missions.finish_improve_mission(
        c, mission_id=mid, run_id=rid, slot_key=("2026-W34", 0),
        mission_status="completed", run_result="approval",
        backlog_transition={"backlog_id": bid, "status": "done",
                            "last_result": "approval:42"},
        now=NOW, approval_id=42, output=None, transcript=[])

    run = c.execute("SELECT result, approval_id FROM improvement_runs WHERE id=?",
                    (rid,)).fetchone()
    assert run["result"] == "approval"
    assert run["approval_id"] == 42


@pytest.mark.parametrize("mission_status", ["failed", "timeout", "max_turns"])
def test_finish_improve_mission_non_completed_marks_slot_failed(tmp_path, mission_status):
    """4 runner status のうち非 completed はいずれも slot を failed にする
    (§8.1-21「4 runner status」)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    mid, rid = _prepare_scheduler_mission(c)
    improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    missions.finish_improve_mission(
        c, mission_id=mid, run_id=rid, slot_key=("2026-W34", 0),
        mission_status=mission_status, run_result=None,
        backlog_transition=None, now=NOW, output=None, transcript=[])
    assert improve_waves.get_slot(c, period_key="2026-W34", k=0)["status"] == "failed"
    run = c.execute("SELECT result, finished_at FROM improvement_runs WHERE id=?",
                    (rid,)).fetchone()
    assert run["result"] is None and run["finished_at"] is not None


def test_finish_improve_mission_output_invalid_treated_as_failed(tmp_path):
    """出力不正 (§4.2-1 検査不合格) は commit 相の呼び出し側が
    mission_status='failed' として渡す規約 — ヘルパ自身は出力検査をしない
    (責務分離)。ここでは failed 経路がそのまま通ることを確認する。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    mid, rid = _prepare_scheduler_mission(c)
    improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    missions.finish_improve_mission(
        c, mission_id=mid, run_id=rid, slot_key=("2026-W34", 0),
        mission_status="failed", run_result=None, backlog_transition=None,
        now=NOW, output=None, transcript=[])
    assert c.execute("SELECT status FROM missions WHERE id=?", (mid,)).fetchone()["status"] == "failed"


def test_finish_improve_mission_shutdown_path_same_helper(tmp_path):
    """shutdown 経路も同じヘルパを通る (別の終端関数を持たない pin)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    mid, rid = _prepare_scheduler_mission(c)
    improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    missions.finish_improve_mission(
        c, mission_id=mid, run_id=rid, slot_key=("2026-W34", 0),
        mission_status="timeout", run_result=None, backlog_transition=None,
        now=NOW, output=None, transcript=[])
    assert improve_waves.get_slot(c, period_key="2026-W34", k=0)["status"] == "failed"


def test_finish_improve_mission_manual_one_shot_slot_key_none(tmp_path):
    """手動 one-shot は slot_key=None — slot テーブルに一切触れない。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    mid = missions.start(c, "improve", "local", "m", now=NOW, commit=False)
    rid = improve_runs.start(c, None, now=NOW, mission_id=mid, commit=False)
    c.commit()
    missions.finish_improve_mission(
        c, mission_id=mid, run_id=rid, slot_key=None,
        mission_status="completed", run_result=None, backlog_transition=None,
        now=NOW, output=None, transcript=[])
    assert c.execute("SELECT status FROM missions WHERE id=?", (mid,)).fetchone()["status"] == "completed"


def test_finish_improve_mission_cas_zero_rowcount_raises_and_rolls_back(tmp_path):
    """mission が既に終端済み (CAS rowcount=0) なら例外 — 呼び出し元が
    ロールバックする。slot・run・backlog も一切変わらない (1 tx の証拠)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "idea", "user", NOW)
    mid, rid = _prepare_scheduler_mission(c)
    improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    backlog.select_for_mission(c, bid, now=NOW)
    improve_runs.bind_backlog(c, rid, bid)
    missions.finish_improve_mission(
        c, mission_id=mid, run_id=rid, slot_key=("2026-W34", 0),
        mission_status="completed", run_result="report",
        backlog_transition={"backlog_id": bid, "status": "done",
                            "last_result": "x"},
        now=NOW, output=None, transcript=[])
    with pytest.raises(Exception):
        missions.finish_improve_mission(
            c, mission_id=mid, run_id=rid, slot_key=("2026-W34", 0),
            mission_status="failed", run_result=None, backlog_transition=None,
            now=NOW, output=None, transcript=[])
    assert improve_waves.get_slot(c, period_key="2026-W34", k=0)["status"] == "done"  # 変化なし


def test_run_lifecycle_created_bound_finished_no_dangling(tmp_path):
    """§8.1-22: 全経路で run は CREATED (start) -> BOUND (bind_backlog、
    任意) -> FINISHED (finish_improve_mission) の一対一。dangling
    (started_at はあるが Mission が無い run) が生まれないことを、
    Tx-0 の 1 tx 性から間接的に確認する — Mission INSERT が無い run は
    そもそも作れない (呼び出しの形自体がそれを強制する)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    mid, rid = _prepare_scheduler_mission(c)
    run = c.execute("SELECT mission_id, started_at, finished_at FROM "
                    "improvement_runs WHERE id=?", (rid,)).fetchone()
    assert run["mission_id"] == mid and run["started_at"] is not None
    assert run["finished_at"] is None  # まだ FINISHED でない (CREATED/BOUND)
    improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    missions.finish_improve_mission(
        c, mission_id=mid, run_id=rid, slot_key=("2026-W34", 0),
        mission_status="completed", run_result=None, backlog_transition=None,
        now=NOW, output=None, transcript=[])
    run2 = c.execute("SELECT finished_at FROM improvement_runs WHERE id=?",
                     (rid,)).fetchone()
    assert run2["finished_at"] is not None  # FINISHED


def test_recover_interrupted_ends_improve_run_and_backlog_observation(tmp_path):
    """§4.1「起動時回収」: `improvement_runs.finished_at IS NULL` かつ
    mission が interrupted になる run だけを対象に、run を終端し
    backlog selected -> observation(interrupted) にする。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "idea", "user", NOW)
    mid, rid = _prepare_scheduler_mission(c)
    improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    backlog.select_for_mission(c, bid, now=NOW)
    improve_runs.bind_backlog(c, rid, bid)
    # ここでプロセスが crash した想定 (missions は running のまま)

    result = missions.recover_interrupted(c, now=NOW, max_requeue=3)
    assert result["missions_recovered"] == 1
    run = c.execute("SELECT finished_at, result FROM improvement_runs WHERE id=?",
                    (rid,)).fetchone()
    assert run["finished_at"] is not None and run["result"] is None
    bl = c.execute("SELECT status, last_result FROM improvement_backlog WHERE id=?",
                   (bid,)).fetchone()
    assert bl["status"] == "observation" and bl["last_result"] == "interrupted"
    slot = improve_waves.get_slot(c, period_key="2026-W34", k=0)
    assert slot["status"] == "failed"


def test_recover_interrupted_leaves_finished_improve_runs_untouched(tmp_path):
    """M6 pin: run が既に `finished_at` を持つ場合、**mission が running の
    まま**残っていても (Tx-2 の run finish と mission finish が同一 tx で
    ないため生じうる部分書き込み障害窓を模す) `recover_interrupted` は
    その run にも紐づく backlog にも触れない — improve 拡張ブロックの
    `AND finished_at IS NULL` フィルタそのものを検査する。

    旧版はここで `finish_improve_mission` を呼んで mission を `completed`
    にしていたため、improve 拡張ブロック (`loop_by_id.get(mid) ==
    'improve'` は running missions のみを対象にする) へ一切到達せず、
    フィルタの有無を区別できていなかった (プラン 10 Task 8 検収 m1)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "idea", "user", NOW)
    mid, rid = _prepare_scheduler_mission(c)
    improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    backlog.select_for_mission(c, bid, now=NOW)
    improve_runs.bind_backlog(c, rid, bid)
    # run だけを直接終端させ、mission は running のまま残す (finish_
    # improve_mission を通す通常経路では起きない、部分書き込み障害窓の模擬)。
    improve_runs.finish(c, rid, result="report", now=NOW)
    assert c.execute("SELECT status FROM missions WHERE id=?",
                     (mid,)).fetchone()["status"] == "running"

    later = NOW + timedelta(hours=1)
    missions.recover_interrupted(c, now=later, max_requeue=3)

    run_row = c.execute("SELECT finished_at FROM improvement_runs WHERE id=?",
                        (rid,)).fetchone()
    assert run_row["finished_at"] == NOW.isoformat()  # `later` で上書きされない
    backlog_row = c.execute(
        "SELECT status FROM improvement_backlog WHERE id=?", (bid,)).fetchone()
    assert backlog_row["status"] == "selected"  # observation へ戻されない


def test_recover_interrupted_trade_missions_unaffected_by_improve_extension(tmp_path):
    """既存の trade レーン回帰: signal claim の requeue 挙動が壊れていない
    ことの pin (Task 8 が改善レーン向けに拡張したことによる回帰無し)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    mid = missions.start(c, "trade", "local", "m", now=NOW)
    result = missions.recover_interrupted(c, now=NOW, max_requeue=3)
    assert result["missions_recovered"] == 1
    assert c.execute("SELECT status FROM missions WHERE id=?",
                     (mid,)).fetchone()["status"] == "interrupted"


def test_recover_interrupted_does_not_reopen_backlog_already_terminal(tmp_path):
    """M7 pin (申し送り⑧の解決): improve 拡張ブロックの
    `AND status='selected'` を落とすと、bind 後に別経路で backlog が
    先に done/rejected へ終端していた稀な競合 (run.finished_at はまだ
    NULL のまま mission が running -> interrupted になるケース) で、
    既に終端済みの backlog を observation へ巻き戻してしまう。ガードが
    あれば selected 以外の backlog には触れない。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "idea", "user", NOW)
    mid, rid = _prepare_scheduler_mission(c)
    improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    backlog.select_for_mission(c, bid, now=NOW)
    improve_runs.bind_backlog(c, rid, bid)
    # 競合の直接再現: run.finished_at を立てる `finish_improve_mission` を
    # 経由せず、backlog だけが先に (例えば人間承認の別経路で) 終端した
    # 状態を作る — run は running のまま。
    backlog.set_status(c, bid, "done", NOW, last_result="report:reports/x.md")

    result = missions.recover_interrupted(c, now=NOW, max_requeue=3)
    assert result["missions_recovered"] == 1
    bl = c.execute("SELECT status, last_result FROM improvement_backlog WHERE id=?",
                   (bid,)).fetchone()
    assert bl["status"] == "done"  # 終端済み backlog は巻き戻されない
    assert bl["last_result"] == "report:reports/x.md"
