"""afx 本体側の cgroup topology、前回 mission cgroup の回収、mission 後の rmdir と次 mission の gate。

cgroup v2 の kernel 応答はモックしない。topology と自己移動は pytest process (login session の
scope) では成立しないので、正の経路は使い捨ての user service の中で helper を走らせて観測する。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from agentic_fx.core import cgroup_host as ch
from agentic_fx.core import unit_admission as ua
from tests.core._disposable_unit import disposable_units  # noqa: F401 (fixture)

REPO = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------ 純粋な部分


def test_unit_cgroup_root_from_proc_self_cgroup():
    base = "/user.slice/user-1000.slice/user@1000.service/app.slice/afx.service"
    assert ch.unit_cgroup_root("afx.service", f"0::{base}\n") == Path("/sys/fs/cgroup" + base)
    # 自己移動後の同じ process
    assert ch.unit_cgroup_root("afx.service", f"0::{base}/supervisor\n") == Path(
        "/sys/fs/cgroup" + base)
    for bad in (
        "0::/user.slice/user-1000.slice/session-3.scope\n",       # unit の外
        f"0::{base}/m-abc\n",                                       # supervisor 以外の子
        f"0::{base}/supervisor/deeper\n",
        f"1:name=systemd:{base}\n0::{base}\n",                      # v1 の行が混じる
        "",
        f"0::{base}\n0::{base}\n",
    ):
        with pytest.raises(ch.CgroupHostError) as ei:
            ch.unit_cgroup_root("afx.service", bad)
        assert ei.value.reason == ua.REASON_TOPOLOGY_UNSUPPORTED


def test_gate_reasons_are_fixed():
    assert ch.GATE_REASONS == frozenset({
        "cgroup_cleanup_timeout", "cgroup_not_empty", "cgroup_rmdir_failed", "worker_not_reaped"})
    host = ch.MissionCgroupHost()
    with pytest.raises(ValueError):
        host.close_gate("anything_else")


def test_unstarted_host_does_not_allow_missions():
    host = ch.MissionCgroupHost()
    assert host.mission_allowed() is False


def test_delegation_or_topology_failure_keeps_backend_closed():
    if "/session-" not in Path("/proc/self/cgroup").read_text():
        pytest.skip("pytest が login session の scope の中で走っていない")
    host = ch.MissionCgroupHost()
    host.start(deadline=time.monotonic() + 5)
    assert host.mission_allowed() is False
    assert host.blocking_reasons() == (ua.REASON_DELEGATION_MISSING,)
    # process 内で開く経路は無い
    with pytest.raises(RuntimeError):
        host.start(deadline=time.monotonic() + 5)
    host.report_worker_not_reaped()
    assert host.mission_allowed() is False
    assert host.blocking_reasons() == (ua.REASON_DELEGATION_MISSING, ch.REASON_WORKER_NOT_REAPED)
    public = {n for n in dir(host) if not n.startswith("_")}
    assert not public & {"open", "reopen", "reset", "clear", "open_gate", "reset_gate"}

    # 隔離を緩める設定 key が無い (config schema と example の両方)
    relaxing = re.compile(
        r"cgroup|delegat|topology|stop_policy|unit_admission|systemctl|mission_gate|sandbox_unsafe",
        re.IGNORECASE)
    for rel in ("src/agentic_fx/config.py", "config/settings.yaml.example"):
        text = (REPO / rel).read_text()
        assert relaxing.search(text) is None, rel
    # 部品自体も設定を読まない
    for mod in (ch, ua):
        src = Path(mod.__file__).read_text()
        assert "agentic_fx.config" not in src and "settings" not in src


def test_topology_failure_in_wrapper_service_keeps_backend_closed(disposable_units):
    res = disposable_units.run_helper("wrapstart", "host_start", wrapper=True)
    assert res.get("error") is None, res
    assert res["mission_allowed"] is False
    assert res["reasons"] == [ua.REASON_TOPOLOGY_UNSUPPORTED]


def test_leftover_empty_supervisor_is_reused(disposable_units):
    res = disposable_units.run_helper("supempty", "host_leftover_supervisor_empty")
    assert res.get("error") is None, res
    assert res["mission_allowed"] is True, res
    assert res["self_cgroup_after"].endswith("/supervisor")


def test_leftover_occupied_supervisor_is_topology_unsupported(disposable_units):
    res = disposable_units.run_helper("supbusy", "host_leftover_supervisor_occupied")
    assert res.get("error") is None, res
    assert res["mission_allowed"] is False
    assert res["reasons"] == [ua.REASON_TOPOLOGY_UNSUPPORTED]
    assert not res["self_cgroup_after"].endswith("/supervisor"), "他の process の居る所へは移らない"


# ------------------------------------------------------------------ mission cgroup の後始末と gate


def test_stale_mission_cgroup_is_reclaimed_at_startup(disposable_units):
    res = disposable_units.run_helper("stale", "host_stale")
    assert res.get("error") is None, res
    assert res["stale_before"]["joined"] is True
    assert res["stale_before"]["populated"] == "populated 1"
    assert res["reasons"] == []
    assert res["mission_allowed"] is True
    assert res["sleeper_signal"] == 9, "前回 mission の process は cgroup.kill で死ぬ"
    assert res["stale_exists_after"] is False
    assert res["reclaimed"] == ["m-stale"]


def test_unremovable_mission_cgroup_closes_gate(disposable_units):
    res = disposable_units.run_helper("busy", "host_busy")
    assert res.get("error") is None, res
    assert res["reasons"] == [ch.REASON_RMDIR_FAILED]
    assert res["mission_allowed"] is False
    assert res["busy_exists_after"] is True
    assert res["mission_allowed_later"] is False
    assert res["restart_in_process"] == "RuntimeError"
    # 後始末からの rmdir も同じ reason で gate を閉じる
    assert res["cleanup_after_start"] == ch.REASON_RMDIR_FAILED


def test_cleanup_deadline_passed_closes_gate(disposable_units):
    res = disposable_units.run_helper("late", "host_deadline_passed")
    assert res.get("error") is None, res
    assert res["allowed_before"] is True
    assert res["cleanup_reason"] == ch.REASON_CLEANUP_TIMEOUT
    assert res["cleanup_elapsed"] < 0.5, "過ぎた deadline では populated の待ちに入らない"
    assert res["mission_allowed"] is False
    assert res["reasons"] == [ch.REASON_CLEANUP_TIMEOUT]
    # 保険の cgroup.kill は出す
    assert res["sleeper_signal"] == 9


def test_worker_not_reaped_reported_by_caller_closes_gate(disposable_units):
    res = disposable_units.run_helper("notreaped", "host_worker_not_reaped")
    assert res.get("error") is None, res
    assert res["allowed_before"] is True
    assert res["mission_allowed"] is False
    assert res["reasons"] == [ch.REASON_WORKER_NOT_REAPED]
    # 閉じた後の成功した cleanup は gate を開かない
    assert res["allowed_after_successful_cleanup"] is False


def test_invalid_mission_cgroup_name_fails_closed(disposable_units):
    res = disposable_units.run_helper("badname", "host_bad_name")
    assert res.get("error") is None, res
    assert res["cleanup_reason"] == ch.REASON_RMDIR_FAILED
    assert res["mission_allowed"] is False
    assert res["supervisor_exists"] is True, "名前の外の cgroup には触れない"


def test_no_disposable_unit_or_cgroup_left(disposable_units):
    res = disposable_units.run_helper("leftover", "host_start")
    assert res["mission_allowed"] is True, res
    root = Path(res["root"])
    deadline = time.monotonic() + 10
    while root.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not root.exists(), "unit が消えたら cgroup も消える"
    assert disposable_units.listed() == []
