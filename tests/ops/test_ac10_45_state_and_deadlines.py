"""AC-10: 既存世代 CAS を API から使う。AC-45: 全待機の deadline / 停止合図と停止順序。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentic_fx.ops import ErrorCode, OpsError
from agentic_fx.ops.jobs import JobState
from agentic_fx.ops.service import OpsLimits
from agentic_fx.store.state import StateStore

from .conftest import APPROVER, OPERATOR, hold_plugin_lock, wait_job

CHILD = Path(__file__).with_name("_child.py")


def _spawn(*args):
    return subprocess.Popen([sys.executable, str(CHILD), *map(str, args)],
                            stdin=subprocess.DEVNULL)


def _wait_file(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"child did not signal {path}"
        time.sleep(0.01)


def _stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait(10)


def test_ac10_stale_generation_is_409_and_new_latch_remains(ops_env):
    service = ops_env.service()
    ops_env.state.update(kill_switch_latched=True)
    old = ops_env.state.load().kill_switch_generation
    service.reset_kill_switch(APPROVER, old)
    ops_env.state.update(kill_switch_latched=True)
    with pytest.raises(OpsError) as raised:
        service.reset_kill_switch(APPROVER, old)
    assert raised.value.code is ErrorCode.GENERATION_MISMATCH
    state = ops_env.state.load()
    assert state.kill_switch_latched and state.kill_switch_generation == old + 1


def test_ac10_repeated_latch_keeps_generation_and_unlatched_reset_writes_nothing(ops_env):
    service = ops_env.service()
    ops_env.state.update(kill_switch_latched=True)
    generation = ops_env.state.load().kill_switch_generation
    for _ in range(100):
        ops_env.state.update(kill_switch_latched=True)
    assert ops_env.state.load().kill_switch_generation == generation
    service.reset_kill_switch(APPROVER, generation)
    path = Path(os.path.realpath(ops_env.state._path))
    before = path.stat().st_mtime_ns
    time.sleep(0.01)
    with pytest.raises(OpsError) as raised:
        service.reset_kill_switch(APPROVER, generation)
    assert raised.value.code is ErrorCode.NOT_LATCHED
    assert path.stat().st_mtime_ns == before


def test_ac10_threads_and_another_process_never_break_the_generation(ops_env):
    service = ops_env.service()
    state_path = Path(os.path.realpath(ops_env.state._path))
    ops_env.state.update(kill_switch_latched=True)
    start_generation = ops_env.state.load().kill_switch_generation
    resets, errors, seen = [0], [], []
    lock = threading.Lock()

    def latcher():
        store = StateStore(state_path)
        for _ in range(250):
            store.update(kill_switch_latched=True)

    def resetter():
        store = StateStore(state_path)
        for _ in range(250):
            try:
                current = store.load()
                with lock:
                    seen.append(current.kill_switch_generation)
                if current.kill_switch_latched:
                    service.reset_kill_switch(APPROVER, current.kill_switch_generation,
                                              deadline=time.monotonic() + 30)
                    with lock:
                        resets[0] += 1
            except OpsError as exc:
                if exc.code not in (ErrorCode.GENERATION_MISMATCH, ErrorCode.NOT_LATCHED):
                    errors.append(exc.code)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

    child = _spawn("latch_loop", state_path, 200)
    threads = [threading.Thread(target=fn) for fn in (latcher, latcher, resetter, resetter)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert child.wait(120) == 0
    assert errors == []
    final = json.loads(state_path.read_text(encoding="utf-8"))
    state = ops_env.state.load()
    transitions = state.kill_switch_generation - start_generation
    assert transitions == resets[0] - (0 if state.kill_switch_latched else 1)
    assert final["kill_switch_generation"] == state.kill_switch_generation
    # 観測した世代は単調に増える側にしか動かない (別 thread の観測順なので最大値で比べる)。
    assert max(seen) <= state.kill_switch_generation


@pytest.mark.parametrize("role", ["hold_state_lock", "hold_state_lock_shared"])
def test_ac45_state_flock_held_past_request_deadline_times_out(ops_env, role):
    service = ops_env.service()
    ops_env.state.update(kill_switch_latched=True)
    generation = ops_env.state.load().kill_switch_generation
    ready = ops_env.root / "state-ready"
    child = _spawn(role, os.path.realpath(ops_env.state._path), ready, 30)
    try:
        _wait_file(ready)
        started = time.monotonic()
        with pytest.raises(OpsError) as raised:
            service.reset_kill_switch(APPROVER, generation)
        elapsed = time.monotonic() - started
    finally:
        _stop(child)
    assert raised.value.code is ErrorCode.REQUEST_TIMEOUT
    assert 4.5 <= elapsed <= 5.5
    rows = ops_env.ops_rows()
    assert [r["result_code"] for r in rows if r["phase"] != "accepted"] == ["request_timeout"]
    assert ops_env.state.load().kill_switch_latched


def test_ac45_ops_lock_held_past_request_deadline_times_out(ops_env):
    service = ops_env.service()
    holding, release = threading.Event(), threading.Event()

    def hold():
        with service._ops_lane(time.monotonic() + 60):
            holding.set()
            release.wait(30)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert holding.wait(5)
    try:
        started = time.monotonic()
        with pytest.raises(OpsError) as raised:
            service.add_backlog(OPERATOR, "k", "idea")
        elapsed = time.monotonic() - started
    finally:
        release.set()
        thread.join(5)
    assert raised.value.code is ErrorCode.REQUEST_TIMEOUT
    assert 4.5 <= elapsed <= 5.5
    assert ops_env.ops_rows() == []


def test_ac45_stop_signal_releases_state_and_lane_waits_within_half_second(ops_env):
    service = ops_env.service()
    ops_env.state.update(kill_switch_latched=True)
    generation = ops_env.state.load().kill_switch_generation
    ready = ops_env.root / "state-ready"
    child = _spawn("hold_state_lock", os.path.realpath(ops_env.state._path), ready, 30)
    outcome = {}

    def call():
        started = time.monotonic()
        try:
            service.reset_kill_switch(APPROVER, generation)
        except OpsError as exc:
            outcome["code"] = exc.code
        outcome["stopped_after"] = time.monotonic() - started

    try:
        _wait_file(ready)
        thread = threading.Thread(target=call)
        thread.start()
        time.sleep(0.3)
        signalled = time.monotonic()
        service.stop_event.set()
        thread.join(5)
        latency = time.monotonic() - signalled
    finally:
        _stop(child)
    assert outcome["code"] is ErrorCode.UNAVAILABLE
    assert latency <= 0.5


def test_ac45_stop_signal_releases_ops_lane_wait_within_half_second(ops_env):
    service = ops_env.service()
    holding, release = threading.Event(), threading.Event()

    def hold():
        with service._ops_lane(time.monotonic() + 60):
            holding.set()
            release.wait(30)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert holding.wait(5)
    outcome = {}

    def call():
        try:
            service.add_backlog(OPERATOR, "k", "idea")
        except OpsError as exc:
            outcome["code"] = exc.code

    try:
        thread = threading.Thread(target=call)
        thread.start()
        time.sleep(0.3)
        signalled = time.monotonic()
        service.stop_event.set()
        thread.join(5)
        latency = time.monotonic() - signalled
    finally:
        release.set()
        holder.join(5)
    assert outcome["code"] is ErrorCode.UNAVAILABLE
    assert latency <= 0.5
    assert ops_env.ops_rows() == []


def test_ac45_wall_clock_jump_does_not_move_request_deadline(ops_env):
    """wall clock を 1 日進めても monotonic の期限は動かない (逆も同様)。"""
    service = ops_env.service()
    ops_env.state.update(kill_switch_latched=True)
    generation = ops_env.state.load().kill_switch_generation
    ready = ops_env.root / "state-ready"
    child = _spawn("hold_state_lock", os.path.realpath(ops_env.state._path), ready, 30)
    try:
        _wait_file(ready)
        ops_env.wall.advance(days=1)
        started = time.monotonic()
        with pytest.raises(OpsError):
            service.reset_kill_switch(APPROVER, generation,
                                      deadline=ops_env.mono() + 0.5)
        assert 0.4 <= time.monotonic() - started <= 1.0
        ops_env.mono.advance(10)
        started = time.monotonic()
        with pytest.raises(OpsError) as raised:
            service.reset_kill_switch(APPROVER, generation,
                                      deadline=ops_env.mono() + 0.5)
        assert raised.value.code is ErrorCode.REQUEST_TIMEOUT
        assert time.monotonic() - started <= 1.0
    finally:
        _stop(child)


def test_ac45_shutdown_order_is_stop_accepting_then_queued_then_running_then_join(ops_env):
    service = ops_env.service()
    first = ops_env.make_plugin_approval(name="aaa", mission_id=1)
    second = ops_env.make_plugin_approval(name="bbb", mission_id=2)
    trace = []
    original = service._decision_action

    def observed(kind, approval_id, digest, reason, decided_by):
        run = original(kind, approval_id, digest, reason, decided_by)
        if approval_id != first:
            return run

        def wrapped(job):
            started.set()
            # running job は停止合図を見るまで待つ (未取得 lock 待ちと同じ扱い)。
            service.stop_event.wait(10)
            queued_state = service.jobs.lookup(queued["job_id"]).state
            try:
                service.add_backlog(OPERATOR, "late", "idea")
                accepted_after_stop = True
            except OpsError as exc:
                accepted_after_stop = exc.code
            trace.append(("running_saw_stop", queued_state, accepted_after_stop))
            raise OpsError(ErrorCode.UNAVAILABLE)
        return wrapped

    started = threading.Event()
    service._decision_action = observed
    running = service.reject(APPROVER, first, "x")
    assert started.wait(5)
    queued = service.reject(APPROVER, second, "x")
    service.shutdown(join_timeout=5)
    trace.append(("joined", service._decision_thread.is_alive()))
    assert trace == [("running_saw_stop", JobState.SHUTDOWN, ErrorCode.UNAVAILABLE),
                     ("joined", False)]
    assert service.jobs.lookup(running["job_id"]).state is JobState.SHUTDOWN
    terminal = {r["target_ref"]: r["result_code"] for r in ops_env.ops_rows()
                if r["phase"] != "accepted"}
    assert sorted(terminal.values()) == ["shutdown", "unavailable"]
    assert ops_env.approval(second)["status"] == "pending"


def test_ac45_shutdown_join_is_bounded_even_if_a_job_does_not_converge(ops_env):
    class Stuck:
        def submit_manual(self, *, on_prepared=None):
            time.sleep(3)
            return 1

    service = ops_env.service(improve_supervisor=Stuck())
    service.improve(OPERATOR, "k")
    time.sleep(0.1)
    started = time.monotonic()
    service.shutdown(join_timeout=0.5)
    assert time.monotonic() - started <= 0.8
    # 後片付け: 接続を閉じる前に、収束しなかった job の thread を待つ。
    for thread in list(service._operation_threads):
        thread.join(10)


def test_ac45_lane_wait_setting_shorter_than_the_request_deadline_bounds_the_wait(ops_env):
    service = ops_env.service(limits=OpsLimits(ops_lock_wait_seconds=0.3))
    holding, release = threading.Event(), threading.Event()

    def hold():
        with service._ops_lane(time.monotonic() + 60):
            holding.set()
            release.wait(30)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert holding.wait(5)
    try:
        started = time.monotonic()
        with pytest.raises(OpsError) as raised:
            service.add_backlog(OPERATOR, "k", "idea")
        elapsed = time.monotonic() - started
    finally:
        release.set()
        thread.join(5)
    assert raised.value.code is ErrorCode.REQUEST_TIMEOUT
    assert 0.3 <= elapsed <= 1.0
    assert ops_env.ops_rows() == []
