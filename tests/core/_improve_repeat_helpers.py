"""改善 mission の繰り返し・admission gate の test 用の差し替え。

loop は DB に実際の行 (mission / run / slot) を書く差し替えで、supervisor は本物。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from types import SimpleNamespace

from agentic_fx import improve_notice_codes as codes
from agentic_fx.core.improve_supervisor import ImproveSupervisor
from agentic_fx.store import db as db_mod
from agentic_fx.store import improve_runs, improve_waves, missions

NOW = datetime(2026, 10, 25, 3, 0, tzinfo=timezone.utc)
PERIOD = "2026-W43"
REAL_CONNECT = db_mod.connect


class Clock:
    def __init__(self, now=NOW):
        self.value = now

    def now(self):
        return self.value


class Activity:
    def __init__(self):
        self.rows = []
        self._lock = threading.Lock()

    def write(self, category, event, detail="", *args):
        with self._lock:
            self.rows.append((event, detail))


class Mono:
    """進める単調時計。"""

    def __init__(self, start=1000.0):
        self.value = start
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self.value

    def advance(self, seconds):
        with self._lock:
            self.value += seconds


def make_settings():
    return SimpleNamespace(
        schedule=SimpleNamespace(improve="weekly", improve_at="Sat 03:00"),
        improve=SimpleNamespace(parallel=1, accept_drain_sec=0.0),
        display_timezone="UTC")


def result(status="completed", *, failure_class=None, tool_calls=3):
    return SimpleNamespace(status=status, tool_calls=tool_calls, refused=False,
                           failure_class=failure_class)


class Runner:
    def __init__(self, loop, ctx, step):
        self.loop, self.ctx, self.step = loop, ctx, step
        self.on_ready = None

    def run(self, mission):
        hook = self.step.get("run")
        if hook is not None:
            hook(self.ctx)
        if self.step.get("ready", self.ctx.slot_key is not None) \
                and self.on_ready is not None:
            self.on_ready({"type": "ready", "ok": True})
        if self.step.get("run_error") is not None:
            raise self.step["run_error"]
        return self.step.get("result", result())


class FakeLoop:
    """`script` は prepare ごとの振る舞い。尽きたら最後の step を繰り返す。"""

    def __init__(self, db_path, script, *, default=None):
        self.db_path = db_path
        self.script = list(script)
        self.default = default if default is not None else {}
        self.calls = []
        self.prepared = []
        self.comments = []
        self.prepare_hook = None
        self.claimed_hook = None
        self._lock = threading.Lock()

    def _conn(self):
        return REAL_CONNECT(self.db_path)

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

    def prepare(self, *, slot_key, now, on_ready=None, on_claimed=None,
                oneoff_comment=None):
        with self._lock:
            step = self.script.pop(0) if self.script else dict(self.default)
            self.comments.append(oneoff_comment)
        if self.prepare_hook is not None:
            self.prepare_hook(slot_key)
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
        if on_claimed is not None and slot_key is not None:
            on_claimed()
        if self.claimed_hook is not None:
            self.claimed_hook(slot_key)
        ctx = SimpleNamespace(mission_id=mission_id, run_id=run_id,
                              slot_key=slot_key, step=step)
        with self._lock:
            self.prepared.append(ctx)
        if step.get("prepare_fails") is not None:
            self._finish(ctx, codes.NOTICE_PREPARE_FAILED, status="failed")
            kind = step["prepare_fails"]
            if kind == "exception":
                from agentic_fx.loops.improve_loop import ImprovePrepareFailed
                raise ImprovePrepareFailed(mission_id, slot_key) from OSError("cause")
            raise kind()
        runner = Runner(self, ctx, step)
        runner.on_ready = on_ready
        return "mission", ctx, runner

    def commit(self, *, mission, ctx, result, now, slot_terminalize=True):
        self.calls.append(("commit", ctx.mission_id))
        step = ctx.step
        if step.get("commit_error") is not None:
            raise step["commit_error"]
        status = step.get("db_status") or (
            "completed" if result.status == "completed" else "failed")
        self._finish(ctx, step.get("notice", codes.NOTICE_UNPROFITABLE),
                     status=status, tool_calls=result.tool_calls,
                     slot=slot_terminalize)
        output = step.get("output")
        if output is not None:
            conn = self._conn()
            try:
                conn.execute("UPDATE missions SET output_json=? WHERE id=?",
                             (output(), ctx.mission_id))
                conn.commit()
            finally:
                conn.close()

    def compensate_commit_failure(self, *, ctx, now, exc, slot_terminalize,
                                  tool_calls=None):
        self.calls.append(("compensate_commit", ctx.mission_id))
        if ctx.step.get("compensation_error") is not None:
            raise ctx.step["compensation_error"]
        self._finish(ctx, codes.NOTICE_COMMIT_CRASHED, status="failed",
                     tool_calls=tool_calls, slot=slot_terminalize)

    def compensate_launch_failure(self, *, ctx, now, tool_calls=None):
        self.calls.append(("compensate_launch", ctx.mission_id))
        self._finish(ctx, codes.NOTICE_LAUNCH_CRASHED, status="failed",
                     tool_calls=tool_calls)


def init_db(tmp_path, *, slots=0):
    path = tmp_path / "agentic.db"
    conn = db_mod.connect(path)
    db_mod.init_db(conn)
    if slots:
        improve_waves.create_wave_and_slots(
            conn, period_key=PERIOD, now=NOW, expected=slots, commit=True)
    conn.close()
    return path


def build(db_path, tmp_path, script=(), *, default=None, stop=None,
          activity=None, emit=False, clock=None):
    sup = ImproveSupervisor(
        capacity=1, root=tmp_path, settings=make_settings(),
        clock=clock or Clock(), db_path=db_path,
        stop_event=stop or threading.Event(), activity=activity,
        emit_events=emit)
    loop = FakeLoop(db_path, script, default=default)
    sup._improve_loop = loop
    return sup, loop


class Repeat:
    """`submit_repeat` の呼び出しをまとめる。"""

    def __init__(self, sup, *, mono=None, duration_sec=None, max_runs=None,
                 holder=None, autopilot=lambda: False, cancel=None):
        self.sup = sup
        self.mono = mono or Mono()
        self.duration_sec = duration_sec
        self.max_runs = max_runs
        self.holder = holder
        self.autopilot = autopilot
        self.cancel = cancel or threading.Event()
        self.patches = []

    def on_progress(self, patch):
        self.patches.append(dict(patch))

    def run(self):
        return self.sup.submit_repeat(
            duration_sec=self.duration_sec, max_runs=self.max_runs,
            comment_holder=self.holder, on_progress=self.on_progress,
            cancel_event=self.cancel, autopilot_check=lambda: self.autopilot(),
            monotonic=self.mono)


def fake_waits(sup, mono):
    """待ちを単調時計の前進に置き換える。呼ばれた (event, timeout) を返す列。"""
    waits = []

    def wait(event, timeout):
        waits.append(timeout)
        if not event.is_set():
            mono.advance(timeout)

    sup._wait_fn = wait
    return waits
