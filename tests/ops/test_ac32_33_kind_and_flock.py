"""AC-32: plugin 以外の kind は受理時・実行時とも fail closed。
AC-33: plugin flock の待機は min(設定値, job deadline 残り) で plugin_busy、停止で即抜ける。"""
from __future__ import annotations

import os
import threading
import time

import pytest

from agentic_fx.ops import ErrorCode, OpsError
from agentic_fx.ops.service import OpsLimits
from agentic_fx.plugin.switch import PluginBusyError, _plugin_locks

from .conftest import APPROVER, digest_of, hold_plugin_lock, wait_job
from .test_ac09_25_26_decision_lane import _shell


@pytest.mark.parametrize("kind", ["live_trade", "tech_plugin", "unknown"])
def test_ac32_non_plugin_kind_is_refused_at_acceptance_for_api_and_shell(ops_env, kind):
    service = ops_env.service()
    approval_id = ops_env.raw_approval(kind)
    digest = digest_of(ops_env, approval_id)
    for call in (lambda: service.approve(APPROVER, approval_id, digest),
                 lambda: service.retry(APPROVER, approval_id, digest),
                 lambda: service.reject(APPROVER, approval_id, "x")):
        with pytest.raises(OpsError) as raised:
            call()
        assert raised.value.code is ErrorCode.UNSUPPORTED_KIND
    shell = _shell(ops_env, service)
    for line in (f"approve {approval_id}", f"reject {approval_id}",
                 f"approval retry {approval_id}"):
        assert "plugin 以外の kind" in shell.dispatch(line)
    assert ops_env.approval(approval_id)["status"] == "pending"
    assert service.jobs._jobs == {}


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_ac32_kind_changed_after_acceptance_fails_closed_at_execution(ops_env, decision):
    service = ops_env.service()
    approval_id = ops_env.make_plugin_approval()
    digest = digest_of(ops_env, approval_id)
    release = hold_plugin_lock(ops_env.plugins_root, "sma")
    try:
        if decision == "approve":
            reply = service.approve(APPROVER, approval_id, digest)
        else:
            reply = service.reject(APPROVER, approval_id, "x")
        time.sleep(0.2)  # worker が lock 待ちに入る
        ops_env.conn.execute("UPDATE approval_requests SET kind='live_trade' WHERE id=?",
                             (approval_id,))
        ops_env.conn.commit()
    finally:
        release()
    job = wait_job(service, reply["job_id"])
    assert (job["state"], job["result_code"]) == ("failed", "unsupported_kind")
    assert ops_env.approval(approval_id)["status"] == "pending"
    assert not (ops_env.plugins_root / "sma").exists()


def _snapshot(env, approval_id):
    versions = env.plugins_root / "_versions"
    return (tuple(env.approval(approval_id)),
            tuple(tuple(r) for r in env.conn.execute(
                "SELECT * FROM plugin_switch_journal").fetchall()),
            sorted(str(p) for p in versions.rglob("*")) if versions.exists() else [],
            os.path.lexists(env.plugins_root / "sma"))


def test_ac33_plugin_busy_within_setting_plus_half_second_without_changes(ops_env):
    service = ops_env.service(limits=OpsLimits(plugin_lock_wait_seconds=0.5))
    approval_id = ops_env.make_plugin_approval()
    before = _snapshot(ops_env, approval_id)
    release = hold_plugin_lock(ops_env.plugins_root, "sma")
    try:
        started = time.monotonic()
        reply = service.approve(APPROVER, approval_id, digest_of(ops_env, approval_id))
        job = wait_job(service, reply["job_id"])
        elapsed = time.monotonic() - started
    finally:
        release()
    assert (job["state"], job["result_code"]) == ("failed", "plugin_busy")
    assert 0.5 <= elapsed <= 1.0
    assert _snapshot(ops_env, approval_id) == before


def test_ac33_job_deadline_shorter_than_setting_bounds_the_wait(ops_env):
    service = ops_env.service(limits=OpsLimits(plugin_lock_wait_seconds=30.0,
                                               decision_deadline_seconds=0.6))
    approval_id = ops_env.make_plugin_approval()
    release = hold_plugin_lock(ops_env.plugins_root, "sma")
    try:
        started = time.monotonic()
        reply = service.reject(APPROVER, approval_id, "x")
        job = wait_job(service, reply["job_id"])
        elapsed = time.monotonic() - started
    finally:
        release()
    assert job["result_code"] == "plugin_busy"
    assert elapsed <= 0.6 + 0.5
    assert ops_env.approval(approval_id)["status"] == "pending"


def test_ac33_stop_releases_the_wait_within_half_second(ops_env):
    service = ops_env.service()
    approval_id = ops_env.make_plugin_approval()
    release = hold_plugin_lock(ops_env.plugins_root, "sma")
    try:
        reply = service.reject(APPROVER, approval_id, "x")
        time.sleep(0.2)
        started = time.monotonic()
        service.shutdown(join_timeout=2.0)
        elapsed = time.monotonic() - started
    finally:
        release()
    job = service.jobs.lookup(reply["job_id"])
    assert job.state.value == "shutdown"
    assert elapsed <= 0.5
    assert ops_env.approval(approval_id)["status"] == "pending"


def test_ac33_second_dependency_timeout_releases_the_first_lock(tmp_path):
    root = tmp_path / "plugins"
    (root / ".locks").mkdir(parents=True)
    release = hold_plugin_lock(root, "zeta")
    try:
        with pytest.raises(PluginBusyError):
            with _plugin_locks(root, ["alpha", "zeta"], deadline=time.monotonic() + 0.2):
                pass
        # alpha は解放済みなので別の取得がすぐ通る。
        with _plugin_locks(root, ["alpha"], deadline=time.monotonic() + 0.2):
            pass
    finally:
        release()


def test_ac33_bless_and_submit_path_keeps_blocking_lock(tmp_path):
    """deadline を渡さない既存の呼び出し元は従来どおり待ち続ける。"""
    root = tmp_path / "plugins"
    (root / ".locks").mkdir(parents=True)
    release = hold_plugin_lock(root, "alpha")
    acquired = threading.Event()

    def blocking():
        with _plugin_locks(root, ["alpha"]):
            acquired.set()

    thread = threading.Thread(target=blocking, daemon=True)
    thread.start()
    assert not acquired.wait(0.5)
    release()
    assert acquired.wait(5)
    thread.join(5)
