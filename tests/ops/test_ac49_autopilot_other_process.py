"""AC-49: autopilot の切替を別 process (実 flock) で行い、queued job の開始判定との
両順序を観測する (approve / retry / improve)。

- 切替が先: 別 process が StateStore の flock を持ったまま autopilot=true を書く間、job の
  開始判定はその flock で待たされ、放された後に true を読んで autopilot_restricted で終端
  する。approval・journal・symlink・mission は不変。
- 開始判定が先: 判定を通った直後に別 process が切り替える。job は切替前に始まったものとして
  定義済みの終端まで収束し、切替の後に来た job は新しい変更を始めない。
"""
from __future__ import annotations

import resource
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentic_fx.ops import ErrorCode, OpsError
from agentic_fx.ops.jobs import JobState

from .conftest import APPROVER, OPERATOR, digest_of, wait_job
from .test_ac11_12_49_jobs import _BarrierState, _Improve

CHILD = Path(__file__).with_name("_autopilot_child.py")


def _spawn(state_path: Path, *args: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(CHILD), str(state_path), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            stdin=subprocess.DEVNULL,
                            preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE,
                                                                  (0, 0)))


def _finish(proc: subprocess.Popen) -> None:
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    assert out.splitlines()[-1] == "done"


class _AfterCheckState:
    """最初の開始判定が flock を放した直後で止められる StateStore 包み。"""

    def __init__(self, inner):
        self._inner = inner
        self.checked = threading.Event()
        self.proceed = threading.Event()
        self.first_result = None

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def autopilot_at_start(self, **kwargs):
        result = self._inner.autopilot_at_start(**kwargs)
        if not self.checked.is_set():
            self.first_result = result
            self.checked.set()
            assert self.proceed.wait(20)
        return result


def _submit(service, kind: str, env, approval_id: int, key: str = "k"):
    if kind == "improve":
        return service.improve(OPERATOR, key)
    call = service.approve if kind == "approve" else service.retry
    return call(APPROVER, approval_id, digest_of(env, approval_id))


def _journal_rows(env) -> int:
    return env.conn.execute("SELECT COUNT(*) AS n FROM plugin_switch_journal").fetchone()["n"]


@pytest.mark.parametrize("kind", ["approve", "retry", "improve"])
def test_ac49_switch_by_another_process_holding_the_flock_wins(ops_env, kind, tmp_path):
    state = _BarrierState(ops_env.state)
    improve = _Improve()
    improve.release.set()
    service = ops_env.service(state_store=state, improve_supervisor=improve)
    approval_id = ops_env.make_plugin_approval()
    state.proceed.clear()
    reply = _submit(service, kind, ops_env, approval_id)
    assert state.before_check.wait(10)

    release = tmp_path / "release"
    child = _spawn(ops_env.state._path, "hold", str(release))
    try:
        assert child.stdout.readline().strip() == "holding"
        state.proceed.set()
        # 開始判定は別 process の持つ flock で待たされている
        time.sleep(0.5)
        job = service.jobs.lookup(reply["job_id"])
        assert job.state is JobState.RUNNING and not job.done.is_set()
        release.write_text("go")
        _finish(child)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(10)
    job = wait_job(service, reply["job_id"])
    assert (job["state"], job["result_code"]) == ("failed", "autopilot_restricted")
    assert ops_env.state.load().autopilot is True
    assert ops_env.approval(approval_id)["status"] == "pending"
    assert _journal_rows(ops_env) == 0
    assert not (ops_env.plugins_root / "sma").exists()
    assert improve.calls == 0


@pytest.mark.parametrize("kind", ["approve", "retry", "improve"])
def test_ac49_start_check_before_the_other_process_switch_converges_and_nothing_new_starts(
        ops_env, kind):
    state = _AfterCheckState(ops_env.state)
    improve = _Improve()
    improve.release.set()
    service = ops_env.service(state_store=state, improve_supervisor=improve)
    approval_id = ops_env.make_plugin_approval()
    second = ops_env.make_plugin_approval(name="ema", mission_id=2)
    reply = _submit(service, kind, ops_env, approval_id)
    queued = None
    if kind != "improve":
        queued = _submit(service, kind, ops_env, second)
    assert state.checked.wait(10)
    assert state.first_result is False  # 判定は切替の前に通った
    # 判定の後に、別 process が実 flock の下で切り替える
    _finish(_spawn(ops_env.state._path, "now"))
    assert ops_env.state.load().autopilot is True
    state.proceed.set()

    job = wait_job(service, reply["job_id"], timeout=30)
    assert job["state"] == "done", job
    if kind == "improve":
        assert improve.calls == 1
        # 切替の後の要求は受理時に断られ、新しい mission は始まらない
        with pytest.raises(OpsError) as raised:
            service.improve(OPERATOR, "k2")
        assert raised.value.code is ErrorCode.AUTOPILOT_RESTRICTED
        assert improve.calls == 1
        return
    assert ops_env.approval(approval_id)["status"] == "approved"
    assert (ops_env.plugins_root / "sma").is_symlink()
    # 切替前に queued だった 2 件目は開始判定で止まり、何も変えない
    later = wait_job(service, queued["job_id"], timeout=30)
    assert (later["state"], later["result_code"]) == ("failed", "autopilot_restricted")
    assert ops_env.approval(second)["status"] == "pending"
    assert not (ops_env.plugins_root / "ema").exists()
