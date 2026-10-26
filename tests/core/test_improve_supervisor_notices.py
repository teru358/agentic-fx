"""ImproveSupervisor の開始・終了 event の発行点。

loop は DB に実際の行(mission / run / snapshot / slot)を書く差し替えで、supervisor は
本物。event の中身は snapshot だけから作られることを、終端状態を直接組んで確かめる。
"""
from __future__ import annotations

import ast
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_fx import improve_notice_codes as codes
from agentic_fx.core import improve_supervisor as sup_mod
from agentic_fx.core.improve_supervisor import ImproveSupervisor
from agentic_fx.store import db as db_mod
from agentic_fx.store import improve_runs, improve_waves, missions
from tests.ops._improve_seed import seed

NOW = datetime(2026, 10, 25, 3, 0, tzinfo=timezone.utc)
_REAL_CONNECT = db_mod.connect
PERIOD = "2026-W43"


class _Clock:
    def now(self):
        return NOW


class _Activity:
    def __init__(self):
        self.rows = []

    def write(self, category, event, detail="", *args):
        self.rows.append((event, detail))


def _settings():
    return SimpleNamespace(
        schedule=SimpleNamespace(improve="weekly", improve_at="Sat 03:00"),
        improve=SimpleNamespace(parallel=1, accept_drain_sec=0.0),
        display_timezone="UTC")


class _Runner:
    def __init__(self, loop, *, result=None, error=None, call_on_ready=False):
        self.loop, self.result, self.error = loop, result, error
        self.call_on_ready = call_on_ready
        self.on_ready = None

    def run(self, mission):
        if self.call_on_ready and self.on_ready is not None:
            self.on_ready({"type": "ready", "ok": True})
        if self.error is not None:
            raise self.error
        return self.result


def _result(status="completed", tool_calls=7):
    return SimpleNamespace(status=status, tool_calls=tool_calls, refused=False)


class FakeLoop:
    """DB に実際の行を書く。`script` は prepare ごとの振る舞いの列。"""

    def __init__(self, db_path, script):
        self.db_path = db_path
        self.script = list(script)
        self.calls = []
        self.prepared = []

    def _conn(self):
        return _REAL_CONNECT(self.db_path)

    def _finish(self, ctx, notice, *, status, tool_calls=None, slot=True):
        conn = self._conn()
        try:
            missions.finish_improve_mission(
                conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
                slot_key=ctx.slot_key if slot else None, mission_status=status,
                run_result=None, backlog_transition=None, now=NOW, notice=notice,
                tool_calls=tool_calls)
        finally:
            conn.close()

    def prepare(self, *, slot_key, now, on_ready=None):
        step = self.script.pop(0)
        conn = self._conn()
        try:
            mission_id = missions.start(conn, "improve", "local", "m", NOW)
            run_id = improve_runs.start(
                conn, None, NOW, mission_id=mission_id,
                trigger="manual" if slot_key is None else "scheduled")
            if slot_key is not None:
                assert improve_waves.claim_slot(
                    conn, period_key=slot_key[0], k=slot_key[1],
                    mission_id=mission_id, now=NOW)
        finally:
            conn.close()
        ctx = SimpleNamespace(mission_id=mission_id, run_id=run_id, slot_key=slot_key)
        self.prepared.append(ctx)
        if step.get("prepare_fails") is not None:
            self._finish(ctx, codes.NOTICE_PREPARE_FAILED, status="failed")
            kind = step["prepare_fails"]
            if kind == "exception":
                from agentic_fx.loops.improve_loop import ImprovePrepareFailed
                raise ImprovePrepareFailed(mission_id, slot_key) from OSError("cause")
            raise kind()
        runner = _Runner(self, result=step.get("result", _result()),
                         error=step.get("run_error"),
                         call_on_ready=step.get("ready", slot_key is not None))
        runner.on_ready = on_ready
        ctx.step = step
        return "mission", ctx, runner

    def commit(self, *, mission, ctx, result, now, slot_terminalize=True):
        self.calls.append(("commit", ctx.mission_id))
        step = ctx.step
        if step.get("commit_error") is not None:
            raise step["commit_error"]
        self._finish(ctx, step.get("notice", codes.NOTICE_UNPROFITABLE),
                     status="completed" if result.status == "completed" else "failed",
                     tool_calls=result.tool_calls, slot=slot_terminalize)

    def compensate_commit_failure(self, *, ctx, now, exc, slot_terminalize,
                                  tool_calls=None):
        self.calls.append(("compensate_commit", ctx.mission_id, tool_calls))
        if ctx.step.get("compensation_error") is not None:
            raise ctx.step["compensation_error"]
        self._finish(ctx, codes.NOTICE_COMMIT_CRASHED, status="failed",
                     tool_calls=tool_calls, slot=slot_terminalize)

    def compensate_launch_failure(self, *, ctx, now, tool_calls=None):
        self.calls.append(("compensate_launch", ctx.mission_id, tool_calls))
        if ctx.step.get("compensation_error") is not None:
            raise ctx.step["compensation_error"]
        self._finish(ctx, codes.NOTICE_LAUNCH_CRASHED, status="failed",
                     tool_calls=tool_calls)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "agentic.db"
    conn = db_mod.connect(path)
    db_mod.init_db(conn)
    improve_waves.create_wave_and_slots(
        conn, period_key=PERIOD, now=NOW, expected=1, commit=True)
    conn.close()
    return path


def build(db_path, tmp_path, script, *, emit=True, activity=None, stop=None):
    sup = ImproveSupervisor(
        capacity=1, root=tmp_path, settings=_settings(), clock=_Clock(),
        db_path=db_path, stop_event=stop or threading.Event(), activity=activity,
        emit_events=emit)
    loop = FakeLoop(db_path, script)
    sup._improve_loop = loop
    return sup, loop


def events(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [(r["ref"], json.loads(r["fields_json"])) for r in conn.execute(
            "SELECT ref,fields_json FROM ops_events WHERE code='improve_run_changed' "
            "ORDER BY id")]
    finally:
        conn.close()


def started(trigger):
    return {"state": "started", "trigger": trigger, "result_code": None,
            "reason_code": None, "tool_calls": None}


def finished(trigger, result, reason, tool_calls):
    return {"state": "finished", "trigger": trigger, "result_code": result,
            "reason_code": reason, "tool_calls": tool_calls}


def not_terminal_records(caplog):
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("improve_notice_skipped")]


# ---- 正常系 ----

def test_manual_success_emits_started_then_finished_once_each(db_path, tmp_path):
    sup, loop = build(db_path, tmp_path, [{}])
    mission_id = sup.submit_manual()
    assert events(db_path) == [
        (f"improve:{mission_id}", started("manual")),
        (f"improve:{mission_id}", finished("manual", "gate_failed", "unprofitable", 7))]


def test_weekly_success_emits_started_then_finished_once_each(db_path, tmp_path):
    sup, loop = build(db_path, tmp_path, [{}])
    sup._launch_slot(PERIOD, 0)
    mission_id = loop.prepared[0].mission_id
    assert events(db_path) == [
        (f"improve:{mission_id}", started("scheduled")),
        (f"improve:{mission_id}", finished("scheduled", "gate_failed", "unprofitable", 7))]


def test_started_comes_before_the_on_prepared_callback(db_path, tmp_path):
    sup, loop = build(db_path, tmp_path, [{}])
    seen = []
    sup.submit_manual(on_prepared=lambda mission_id: seen.append(len(events(db_path))))
    assert seen == [1]


def test_approval_creation_failure_snapshot_is_announced_once(db_path, tmp_path):
    sup, loop = build(db_path, tmp_path, [{"notice": codes.NOTICE_APPROVAL_TX_FAILED}])
    sup.submit_manual()
    assert [f["reason_code"] for _, f in events(db_path)[1:]] == ["approval_tx_failed"]
    assert len(events(db_path)) == 2


def test_finished_event_is_built_only_from_the_snapshot(db_path, tmp_path):
    # runner の結果の status / tool_calls が snapshot と違っても、event は snapshot の値。
    sup, loop = build(db_path, tmp_path, [{
        "notice": codes.NOTICE_SUBMITTED,
        "result": _result(status="failed", tool_calls=3)}])
    sup.submit_manual()
    assert events(db_path)[1][1] == finished("manual", "submitted", None, 3)


# ---- 補償経路 ----

@pytest.mark.parametrize("entry", ["manual", "slot"])
def test_commit_exception_is_compensated_and_finished_once(db_path, tmp_path, entry):
    sup, loop = build(db_path, tmp_path, [{"commit_error": RuntimeError("boom 9.99")}])
    with pytest.raises(RuntimeError, match="boom"):
        sup.submit_manual() if entry == "manual" else sup._launch_slot(PERIOD, 0)
    mission_id = loop.prepared[0].mission_id
    trigger = "manual" if entry == "manual" else "scheduled"
    assert events(db_path) == [
        (f"improve:{mission_id}", started(trigger)),
        (f"improve:{mission_id}", finished(trigger, "failed", "commit_crashed", 7))]


def test_manual_launch_exception_is_compensated_and_finished_once(db_path, tmp_path):
    sup, loop = build(db_path, tmp_path, [{"run_error": ValueError("run boom")}])
    with pytest.raises(ValueError, match="run boom"):
        sup.submit_manual()
    mission_id = loop.prepared[0].mission_id
    assert events(db_path) == [
        (f"improve:{mission_id}", started("manual")),
        (f"improve:{mission_id}", finished("manual", "failed", "launch_crashed", None))]


def test_weekly_launch_exception_including_base_exception_is_finished_once(
        db_path, tmp_path):
    sup, loop = build(db_path, tmp_path, [{"run_error": KeyboardInterrupt()}])
    with pytest.raises(KeyboardInterrupt):
        sup._launch_slot(PERIOD, 0)
    mission_id = loop.prepared[0].mission_id
    assert events(db_path) == [
        (f"improve:{mission_id}", started("scheduled")),
        (f"improve:{mission_id}", finished("scheduled", "failed", "launch_crashed", None))]


def test_weekly_pre_ready_commit_exception_is_compensated_and_finished_once(
        db_path, tmp_path):
    sup, loop = build(db_path, tmp_path, [{
        "ready": False, "result": _result("failed", 5),
        "commit_error": RuntimeError("boom")}])
    with pytest.raises(RuntimeError):
        sup._launch_slot(PERIOD, 0)
    mission_id = loop.prepared[0].mission_id
    assert events(db_path) == [
        (f"improve:{mission_id}", started("scheduled")),
        (f"improve:{mission_id}", finished("scheduled", "failed", "commit_crashed", 5))]


def test_compensation_gets_the_tool_calls_and_launch_compensation_does_not(
        db_path, tmp_path):
    sup, loop = build(db_path, tmp_path, [
        {"commit_error": RuntimeError("x"), "result": _result(tool_calls=37)}])
    with pytest.raises(RuntimeError):
        sup.submit_manual()
    assert loop.calls[-1][0] == "compensate_commit" and loop.calls[-1][2] == 37
    assert events(db_path)[-1][1]["tool_calls"] == 37


def test_pre_ready_failure_retries_with_one_pair_per_mission_in_order(db_path, tmp_path):
    sup, loop = build(db_path, tmp_path, [
        {"ready": False, "result": _result("failed", 0), "notice": codes.NOTICE_RUNNER_FAILED},
        {"ready": True}])
    sup._launch_slot(PERIOD, 0)
    first, second = (ctx.mission_id for ctx in loop.prepared)
    assert [(ref, f["state"]) for ref, f in events(db_path)] == [
        (f"improve:{first}", "started"), (f"improve:{first}", "finished"),
        (f"improve:{second}", "started"), (f"improve:{second}", "finished")]
    assert events(db_path)[1][1]["reason_code"] == "runner_failed"
    assert first != second


def test_pre_ready_failure_without_retry_left_emits_one_pair(db_path, tmp_path):
    sup, loop = build(db_path, tmp_path, [
        {"ready": False, "result": _result("failed", 0), "notice": codes.NOTICE_RUNNER_FAILED},
        {"ready": False, "result": _result("failed", 0), "notice": codes.NOTICE_RUNNER_FAILED}])
    sup._launch_slot(PERIOD, 0)
    assert [f["state"] for _, f in events(db_path)] == [
        "started", "finished", "started", "finished"]
    assert len({ref for ref, _ in events(db_path)}) == 2


# ---- prepare の失敗 ----

@pytest.mark.parametrize("entry", ["manual", "slot"])
def test_prepare_failure_after_tx0_emits_finished_only_and_shows_the_cause(
        db_path, tmp_path, entry):
    sup, loop = build(db_path, tmp_path, [{"prepare_fails": "exception"}])
    with pytest.raises(OSError, match="cause"):
        sup.submit_manual() if entry == "manual" else sup._launch_slot(PERIOD, 0)
    mission_id = loop.prepared[0].mission_id
    trigger = "manual" if entry == "manual" else "scheduled"
    assert events(db_path) == [
        (f"improve:{mission_id}", finished(trigger, "failed", "prepare_failed", None))]


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
def test_base_exception_in_prepare_passes_through_without_events(
        db_path, tmp_path, exc_type):
    sup, loop = build(db_path, tmp_path, [{"prepare_fails": exc_type}])
    with pytest.raises(exc_type):
        sup.submit_manual()
    assert events(db_path) == []
    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT result_code,reason_code FROM improve_run_notices").fetchall() == [
        ("failed", "prepare_failed")]


# ---- not_terminal の確認 ----

def test_failed_commit_compensation_leaves_the_mission_running_and_logs_a_fixed_word(
        db_path, tmp_path, caplog):
    sup, loop = build(db_path, tmp_path, [{
        "commit_error": RuntimeError("commit boom /home/x"),
        "compensation_error": OSError("db locked https://u.example/?k=SECRET")}])
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError, match="commit boom"):
            sup.submit_manual()
    mission_id = loop.prepared[0].mission_id
    assert [f["state"] for _, f in events(db_path)] == ["started"]
    assert not_terminal_records(caplog) == [
        f"improve_notice_skipped reason=not_terminal mission={mission_id}"]


def test_failed_launch_compensation_in_a_weekly_slot_keeps_its_exception(
        db_path, tmp_path, caplog):
    sup, loop = build(db_path, tmp_path, [{
        "run_error": ValueError("run boom"), "compensation_error": OSError("locked")}])
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(OSError, match="locked"):
            sup._launch_slot(PERIOD, 0)
    mission_id = loop.prepared[0].mission_id
    assert [f["state"] for _, f in events(db_path)] == ["started"]
    assert not_terminal_records(caplog) == [
        f"improve_notice_skipped reason=not_terminal mission={mission_id}"]


def _terminal_state(db_path, tmp_path, *, status="completed", run_finished=True,
                    snapshot=True):
    conn = _REAL_CONNECT(db_path)
    env = SimpleNamespace(conn=conn)
    mission_id = seed(env, notice=codes.NOTICE_UNPROFITABLE, tool_calls=3)
    if not snapshot:
        conn.execute("DROP TRIGGER improve_run_notices_no_delete")
        conn.execute("DELETE FROM improve_run_notices WHERE mission_id=?", (mission_id,))
    if status == "running":
        conn.execute("UPDATE missions SET status='running' WHERE id=?", (mission_id,))
    if not run_finished:
        conn.execute("UPDATE improvement_runs SET finished_at=NULL WHERE mission_id=?",
                     (mission_id,))
    conn.commit()
    conn.close()
    return mission_id


@pytest.mark.parametrize("broken, kwargs", [
    ("status_running", {"status": "running"}),
    ("run_unfinished", {"run_finished": False}),
    ("no_snapshot", {"snapshot": False}),
])
def test_each_terminal_condition_is_needed_before_finished_is_emitted(
        db_path, tmp_path, caplog, broken, kwargs):
    sup, _ = build(db_path, tmp_path, [])
    mission_id = _terminal_state(db_path, tmp_path, **kwargs)
    with caplog.at_level(logging.DEBUG):
        sup._notices.finished(mission_id)
    assert events(db_path) == []
    assert not_terminal_records(caplog) == [
        f"improve_notice_skipped reason=not_terminal mission={mission_id}"]


def test_all_three_conditions_met_emits(db_path, tmp_path):
    sup, _ = build(db_path, tmp_path, [])
    mission_id = _terminal_state(db_path, tmp_path)
    sup._notices.finished(mission_id)
    assert events(db_path) == [
        (f"improve:{mission_id}", finished("scheduled", "gate_failed", "unprofitable", 3))]


def test_unknown_mission_is_not_terminal(db_path, tmp_path, caplog):
    sup, _ = build(db_path, tmp_path, [])
    with caplog.at_level(logging.DEBUG):
        sup._notices.finished(424242)
    assert events(db_path) == []
    assert not_terminal_records(caplog) == [
        "improve_notice_skipped reason=not_terminal mission=424242"]


def test_terminal_check_reads_the_database_at_emit_time_over_the_writer_connection(
        db_path, tmp_path, monkeypatch, caplog):
    sup, _ = build(db_path, tmp_path, [])
    first = _terminal_state(db_path, tmp_path)
    sup._notices.finished(first)  # writer 接続を開く
    mission_id = _terminal_state(db_path, tmp_path)
    other = _REAL_CONNECT(db_path)
    other.execute("UPDATE missions SET status='running' WHERE id=?", (mission_id,))
    other.commit()
    other.close()
    monkeypatch.setattr(sup_mod.db_mod, "connect_readonly",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no read conn")))
    with caplog.at_level(logging.DEBUG):
        sup._notices.finished(mission_id)
    assert [ref for ref, _ in events(db_path)] == [f"improve:{first}"]
    assert not_terminal_records(caplog)


def test_finished_opens_no_connection_besides_the_writer(db_path, tmp_path, monkeypatch):
    sup, _ = build(db_path, tmp_path, [])
    mission_ids = [_terminal_state(db_path, tmp_path) for _ in range(3)]
    opened = []

    def counting(path, *a, **k):
        opened.append(path)
        return _REAL_CONNECT(path, *a, **k)

    monkeypatch.setattr(sup_mod.db_mod, "connect", counting)
    monkeypatch.setattr(sup_mod.db_mod, "connect_readonly",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no read conn")))
    for mission_id in mission_ids:
        sup._notices.finished(mission_id)
    assert len(events(db_path)) == 3 and len(opened) == 1


def _fail_next_emit(monkeypatch, times=1):
    from agentic_fx.ops.events import EventStore
    real = EventStore.emit
    left = [times]

    def flaky(self, code, ref, fields):
        if left[0] > 0:
            left[0] -= 1
            raise sqlite3.OperationalError("disk I/O error /secret/path")
        return real(self, code, ref, fields)

    monkeypatch.setattr(EventStore, "emit", flaky)


def test_a_failed_emit_logs_one_fixed_line_and_the_next_emit_uses_a_new_connection(
        db_path, tmp_path, monkeypatch, caplog):
    sup, _ = build(db_path, tmp_path, [])
    first, second = (_terminal_state(db_path, tmp_path) for _ in range(2))
    opened = []

    def counting(path, *a, **k):
        conn = _REAL_CONNECT(path, *a, **k)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sup_mod.db_mod, "connect", counting)
    _fail_next_emit(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        sup._notices.finished(first)
    assert [r.getMessage() for r in caplog.records
            if "emit_failed" in r.getMessage()] == [
        f"improve_notice_emit_failed mission={first} state=finished"]
    assert "secret" not in caplog.text
    assert events(db_path) == [] and sup._notices._conn is None
    sup._notices.finished(second)
    assert [ref for ref, _ in events(db_path)] == [f"improve:{second}"]
    assert len(opened) == 2
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")  # 失敗した接続は閉じられている


# ---- 設定と構造 ----

def test_emit_events_false_emits_nothing_and_opens_no_connection(
        db_path, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(sup_mod.db_mod, "connect",
                        lambda *a, **k: opened.append(a) or (_ for _ in ()).throw(
                            AssertionError("no connection expected")))
    monkeypatch.setattr(sup_mod.db_mod, "connect_readonly",
                        lambda *a, **k: opened.append(a) or (_ for _ in ()).throw(
                            AssertionError("no connection expected")))
    sup, _ = build(db_path, tmp_path, [{}], emit=False)
    sup.submit_manual()
    sup.join(0.0)
    assert opened == []
    assert sup._notices._conn is None and events(db_path) == []


def test_emit_events_default_is_false(db_path, tmp_path):
    sup = ImproveSupervisor(capacity=1, root=tmp_path, settings=_settings(),
                            clock=_Clock(), db_path=db_path, stop_event=threading.Event())
    loop = FakeLoop(db_path, [{}])
    sup._improve_loop = loop
    sup.submit_manual()
    assert events(db_path) == []


def test_emit_events_true_even_without_any_reader(db_path, tmp_path):
    sup, _ = build(db_path, tmp_path, [{}])
    sup.submit_manual()
    assert len(events(db_path)) == 2


def test_supervisor_uses_its_own_connection_and_event_store_not_the_ops_service():
    source = Path(sup_mod.__file__).read_text()
    tree = ast.parse(source)
    imported = {alias.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                for alias in n.names} | {n.module for n in ast.walk(tree)
                                         if isinstance(n, ast.ImportFrom) and n.module}
    assert "OpsService" not in imported and "agentic_fx.ops.service" not in imported
    assert "OpsService" not in source
    assert "EventStore(self._conn" in source
    assert "db_mod.connect(self._db_path)" in source


def test_event_fields_never_receive_numbers_from_the_report():
    source = Path(sup_mod.__file__).read_text()
    for token in ("holdout", "avg_r", "pf:", "win_rate", "parse_floor_report"):
        assert token not in source


def test_emit_is_serialized_over_one_connection(db_path, tmp_path, monkeypatch):
    sup, _ = build(db_path, tmp_path, [])
    mission_ids = []
    for _ in range(8):
        mission_ids.append(_terminal_state(db_path, tmp_path))
    opened = []

    def counting(path, *a, **k):
        conn = _REAL_CONNECT(path, *a, **k)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sup_mod.db_mod, "connect", counting)
    barrier = threading.Barrier(len(mission_ids))
    errors = []

    def run(mission_id):
        try:
            barrier.wait(5)
            sup._notices.finished(mission_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(m,)) for m in mission_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors
    assert len(events(db_path)) == 8
    assert len(opened) == 1


def test_join_closes_the_connection_and_later_emits_do_nothing(db_path, tmp_path):
    sup, _ = build(db_path, tmp_path, [{}])
    sup.submit_manual()
    assert sup._notices._conn is not None
    sup.join(0.0)
    assert sup._notices._conn is None
    before = len(events(db_path))
    sup._notices.started(99, "manual")
    assert len(events(db_path)) == before
    assert sup._notices._conn is None


# ---- 通知の失敗が mission に戻らない ----

def _final_state(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        def rows(sql):
            return [tuple(r) for r in conn.execute(sql).fetchall()]
        return {
            "missions": rows("SELECT id,loop,status,started_at,finished_at FROM missions"),
            "runs": rows("SELECT id,mission_id,trigger,result,report_state,finished_at "
                         "FROM improvement_runs"),
            "slots": rows("SELECT wave_period_key,k,status,mission_id,spawn_attempts "
                          "FROM improve_wave_slots"),
            "snapshots": rows("SELECT mission_id,trigger,result_code,reason_code,"
                              "tool_calls,observation_text,approval_id,created_at "
                              "FROM improve_run_notices"),
        }
    finally:
        conn.close()


def _break_ops_events(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TRIGGER no_events BEFORE INSERT ON ops_events "
                 "BEGIN SELECT RAISE(ABORT, 'ops_events down'); END")
    conn.commit()
    conn.close()


def _run_entry(entry, db_path, tmp_path, script, *, before=None):
    activity = _Activity()
    sup, loop = build(db_path, tmp_path, script, activity=activity)
    if before is not None:
        before(db_path)
    try:
        sup.submit_manual() if entry == "manual" else sup._launch_slot(PERIOD, 0)
    except Exception:  # noqa: BLE001
        pass
    return _final_state(db_path), activity.rows


@pytest.mark.parametrize("entry", ["manual", "slot"])
@pytest.mark.parametrize("script", [
    [{}], [{"commit_error": RuntimeError("x")}], [{"run_error": ValueError("y")}]])
def test_a_broken_event_store_changes_nothing_about_the_mission(
        tmp_path_factory, entry, script):
    healthy_dir = tmp_path_factory.mktemp("healthy")
    broken_dir = tmp_path_factory.mktemp("broken")
    states = []
    for directory, before in ((healthy_dir, None), (broken_dir, _break_ops_events)):
        path = directory / "agentic.db"
        conn = db_mod.connect(path)
        db_mod.init_db(conn)
        improve_waves.create_wave_and_slots(
            conn, period_key=PERIOD, now=NOW, expected=1, commit=True)
        conn.close()
        states.append(_run_entry(entry, path, directory, [dict(s) for s in script],
                                 before=before))
    assert states[0] == states[1]
    assert events(broken_dir / "agentic.db") == []
    assert len(events(healthy_dir / "agentic.db")) >= 2


def test_schema_error_from_an_invalid_snapshot_changes_nothing_and_logs_a_fixed_word(
        tmp_path_factory, caplog):
    directory = tmp_path_factory.mktemp("schema")
    path = directory / "agentic.db"
    conn = db_mod.connect(path)
    db_mod.init_db(conn)
    conn.close()
    sup, loop = build(path, directory, [{}])

    def bad_commit(*, mission, ctx, result, now, slot_terminalize=True):
        conn = db_mod.connect(path)
        conn.execute("UPDATE missions SET status='completed', finished_at=? WHERE id=?",
                     (NOW.isoformat(), ctx.mission_id))
        conn.execute("UPDATE improvement_runs SET finished_at=? WHERE id=?",
                     (NOW.isoformat(), ctx.run_id))
        conn.execute(
            "INSERT INTO improve_run_notices (mission_id,trigger,result_code,reason_code,"
            "tool_calls,observation_text,approval_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ctx.mission_id, "manual", "gate_failed", "pf:7.7777", 1, None, None,
             NOW.isoformat()))
        conn.commit()
        conn.close()

    loop.commit = bad_commit
    with caplog.at_level(logging.DEBUG):
        sup.submit_manual()
    assert [f["state"] for _, f in events(path)] == ["started"]
    assert [m for m in not_terminal_records(caplog)] == [
        f"improve_notice_skipped reason=schema mission={loop.prepared[0].mission_id}"]
    assert "7.7777" not in caplog.text
    assert _final_state(path)["snapshots"][0][3] == "pf:7.7777"


def test_no_activity_rows_are_written_by_event_emission(db_path, tmp_path):
    activity = _Activity()
    sup, _ = build(db_path, tmp_path, [{}], activity=activity)
    sup.submit_manual()
    assert activity.rows == []
