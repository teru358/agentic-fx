"""cgroup host の契約のうち、既存のテストが値で固定していない部分。

gate の reason の表出、mission cgroup 名の文法、想定外の I/O 失敗、unit root の同定、
既存 supervisor/ の子 cgroup、cgroup.kill が書けないときの deadline。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from agentic_fx.core import cgroup_host as ch
from agentic_fx.core import unit_admission as ua
from tests.core._disposable_unit import disposable_units  # noqa: F401 (fixture)

BASE = "/user.slice/user-1000.slice/user@1000.service/app.slice/afx.service"


def test_supervisor_of_another_unit_is_not_this_unit_root():
    other = "/user.slice/user-1000.slice/user@1000.service/app.slice/other.service"
    with pytest.raises(ch.CgroupHostError) as ei:
        ch.unit_cgroup_root("afx.service", f"0::{other}/supervisor\n")
    assert ei.value.reason == ua.REASON_TOPOLOGY_UNSUPPORTED


@pytest.mark.parametrize("reason", sorted(ch.GATE_REASONS))
def test_every_gate_reason_is_reported_and_blocks(reason):
    host = ch.MissionCgroupHost()
    host.close_gate(reason)
    assert host.blocking_reasons() == (reason,)
    assert host.mission_allowed() is False


@pytest.mark.parametrize("reason", sorted(ua.ADMISSION_REASONS))
def test_every_admission_reason_is_reported_by_start(reason):
    def admit():
        raise ua.AdmissionError(reason, "x")

    host = ch.MissionCgroupHost()
    host.start(deadline=time.monotonic() + 5, admit=admit)
    assert host.blocking_reasons() == (reason,)
    assert host.mission_allowed() is False


@pytest.mark.parametrize("name", [
    "m-",
    "m-" + "a" * 65,
    "m-a/b",
    "m-a.b",
    "m-../x",
    "m-a\n",
    "x-abc",
])
def test_mission_name_outside_grammar_is_not_touched_and_closes_gate(tmp_path, name):
    host = ch.MissionCgroupHost()
    host.root = tmp_path
    before = sorted(p.name for p in tmp_path.iterdir())
    assert host.cleanup_mission(name, time.monotonic() + 5) == ch.REASON_RMDIR_FAILED
    assert host.blocking_reasons() == (ch.REASON_RMDIR_FAILED,)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_mission_name_at_grammar_limit_is_accepted(tmp_path):
    host = ch.MissionCgroupHost()
    host.root = tmp_path
    # 存在しない cgroup の後始末は成功 (既に無い)
    assert host.cleanup_mission("m-" + "a" * 64, time.monotonic() + 5) is None
    assert host.blocking_reasons() == ()


def test_unexpected_io_failure_at_start_closes_gate_without_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "adopt_topology", lambda unit: tmp_path / "absent")
    host = ch.MissionCgroupHost()
    host.start(deadline=time.monotonic() + 5,
               admit=lambda: ua.UnitAdmission(unit="afx.service"))
    assert host.mission_allowed() is False
    assert host.blocking_reasons() == (ua.REASON_TOPOLOGY_UNSUPPORTED,)


def test_leftover_supervisor_with_child_cgroup_is_topology_unsupported(disposable_units):
    res = disposable_units.run_helper("supchild", "host_leftover_supervisor_with_child")
    assert res.get("error") is None, res
    assert res["mission_allowed"] is False
    assert res["reasons"] == [ua.REASON_TOPOLOGY_UNSUPPORTED]
    assert not res["self_cgroup_after"].endswith("/supervisor")


def test_unwritable_cgroup_kill_times_out_at_deadline(disposable_units):
    res = disposable_units.run_helper("killro", "host_kill_unwritable")
    assert res.get("error") is None, res
    assert res["kill_writable"] is False
    assert res["cleanup_reason"] == ch.REASON_CLEANUP_TIMEOUT
    assert 0.4 <= res["cleanup_elapsed"] < 3, res["cleanup_elapsed"]
    assert res["mission_allowed"] is False
    assert res["reasons"] == [ch.REASON_CLEANUP_TIMEOUT]


def test_process_joining_root_during_self_move_is_topology_unsupported(disposable_units):
    res = disposable_units.run_helper("movejoin", "host_root_joined_during_move")
    assert res.get("error") is None, res
    assert res["joined"] is True
    assert res["mission_allowed"] is False
    assert res["reasons"] == [ua.REASON_TOPOLOGY_UNSUPPORTED]


def test_adopt_again_reenables_disabled_controllers(disposable_units):
    res = disposable_units.run_helper("reenable", "host_controllers_disabled_then_adopt_again")
    assert res.get("error") is None, res
    assert res["allowed_before"] is True
    assert not {"pids", "memory"} & set(res["disabled"].split())
    assert res["again"] is None
    assert {"pids", "memory"} <= set(res["subtree_control_after"].split())


def test_passed_deadline_closes_gate_even_for_empty_mission_cgroup(disposable_units):
    res = disposable_units.run_helper("lateempty", "host_deadline_passed_empty")
    assert res.get("error") is None, res
    assert res["cleanup_reason"] == ch.REASON_CLEANUP_TIMEOUT
    assert res["reasons"] == [ch.REASON_CLEANUP_TIMEOUT]


def test_unwritable_subtree_control_is_delegation_missing(disposable_units):
    res = disposable_units.run_helper("subro", "host_subtree_control_unwritable")
    assert res.get("error") is None, res
    assert res["mission_allowed"] is False
    assert res["reasons"] == [ua.REASON_DELEGATION_MISSING]


def test_missing_cgroup_kill_on_existing_cgroup_still_checks_emptiness(tmp_path, monkeypatch):
    """cgroup.kill が ENOENT でも cgroup 自体が在れば、空の確認と rmdir まで進む。"""
    cg = tmp_path / "m-x"
    cg.mkdir()
    (cg / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    (cg / "cgroup.procs").write_text("")
    original = ch._write

    def write(path, value):
        if path.name == "cgroup.kill":
            raise FileNotFoundError(2, "No such file or directory")
        original(path, value)

    monkeypatch.setattr(ch, "_write", write)
    with pytest.raises(ch.CgroupHostError) as ei:
        ch.remove_mission_cgroup(cg, time.monotonic() + 5)
    # 通常の dir は file を含むので rmdir が失敗する。ENOENT を「既に無い」と読まないこと
    assert ei.value.reason == ch.REASON_RMDIR_FAILED
    assert cg.exists()


@pytest.mark.parametrize("exc", [RuntimeError("x"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")])
def test_unexpected_exception_in_start_closes_gate_without_raising(exc, caplog):
    def admit():
        raise exc

    host = ch.MissionCgroupHost()
    host.start(deadline=time.monotonic() + 5, admit=admit)
    assert host.mission_allowed() is False
    assert host.blocking_reasons() == (ua.REASON_TOPOLOGY_UNSUPPORTED,)
    assert type(exc).__name__ in caplog.text
