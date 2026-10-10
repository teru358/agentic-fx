"""worker 側の mission cgroup: 作成・上限・保持 fd・join・cgroup.kill・populated。

cgroup の実体 (mkdir・cgroup.procs・cgroup.kill・cgroup.events・rmdir) はモックしない。
使い捨ての user service (`afxtest-<id>-*`) の中で `_mission_cgroup_service_helper.py` を
走らせて観測する。触るのはその unit の cgroup だけ。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from agentic_fx.core import cli_launch_protocol as CLP
from agentic_fx.core import landlock_nested
from agentic_fx.core import mission_cgroup as MCG
from agentic_fx.core import unit_admission as ua
from tests.core import _disposable_unit as du
from tests.core._disposable_unit import disposable_units  # noqa: F401 (fixture)

SERVICE_HELPER = Path(__file__).with_name("_mission_cgroup_service_helper.py")
_DENIED = {"EACCES", "EPERM"}


@pytest.fixture(autouse=True)
def _landlock_abi():
    if landlock_nested.probe_abi() < landlock_nested.MIN_ABI:
        pytest.skip("Landlock ABI < 8")


def run_service(units: du.DisposableUnits, case: str, scenario: str,
                timeout: float = 120) -> dict:
    """使い捨て service の main として helper を 1 scenario 走らせ、結果 JSON を返す。"""
    out = units.tmp_path / f"{case}.json"
    command = [sys.executable, str(SERVICE_HELPER), scenario, str(out)]
    units.started.append(units.unit_name(case))
    r = du._run(units._argv(case, {}, command, wait=True), timeout=timeout)
    if not out.exists():
        raise AssertionError(
            f"helper が結果を書かなかった (rc={r.returncode}): {r.stdout!r} {r.stderr!r}")
    res = json.loads(out.read_text())
    assert "exception" not in res, res.get("traceback")
    return res


def assert_cleaned(res: dict) -> None:
    assert res["cleanup_mission"] is None, res
    assert res["cgroup_exists_after"] is False
    assert res.get("blocking_reasons", []) == []


# --- 定数と固定 reason --------------------------------------------------------------

def test_profile_constants_and_reasons():
    assert MCG.PIDS_MAX == 4096
    assert MCG.MEMORY_MAX == 2 * 1024 ** 3
    assert MCG.REASON_CREATE_FAILED == "cgroup_create_failed"
    assert MCG.REASON_TOPOLOGY_UNSUPPORTED == ua.REASON_TOPOLOGY_UNSUPPORTED
    assert MCG.REASON_JOIN_FAILED == CLP.CGROUP_JOIN_FAILED == "cgroup_join_failed"
    assert MCG.cgroup_name("ab" * 16) == "m-" + "ab" * 16


def test_mission_cgroup_name_has_one_derivation(monkeypatch):
    """nonce・mission cgroup・host の掃除が使う名前は同じ接頭辞 1 個から導く。"""
    from agentic_fx.core import cgroup_host as CH
    from agentic_fx.core import mission_protocol as MP

    hex_ = "0123456789abcdef" * 2
    nonce = MP.MissionNonce(hex_)
    assert nonce.cgroup_name == MCG.cgroup_name(hex_) == CH.MISSION_PREFIX + hex_
    # 接頭辞を変えると nonce 側の名前も追従する (直書きの別定義を持たない)
    monkeypatch.setattr(CH, "MISSION_PREFIX", "q-")
    assert nonce.cgroup_name == "q-" + hex_


def test_self_position_is_parsed_from_supervisor_only():
    with pytest.raises(MCG.MissionCgroupError) as e:
        MCG.MissionCgroup.create("ab" * 16, proc_self_cgroup="0::/user.slice/x.service\n")
    assert e.value.reason == MCG.REASON_TOPOLOGY_UNSUPPORTED
    with pytest.raises(MCG.MissionCgroupError) as e:
        MCG.MissionCgroup.create("ab" * 16, proc_self_cgroup="0::/a\n1:name=x:/b\n")
    assert e.value.reason == MCG.REASON_TOPOLOGY_UNSUPPORTED


# --- 受入 18 (cgroup) ----------------------------------------------------------------

def test_setsid_and_double_fork_are_emptied_by_cgroup_kill(disposable_units):
    res = run_service(disposable_units, "orphans", "orphans")
    orphans = [ln for ln in res["lines"] if ln["role"] == "orphan"]
    cli = [ln for ln in res["lines"] if ln["role"] == "cli"][0]
    assert sorted(o["kind"] for o in orphans) == ["plain", "setsid"]
    # 孤児の親は CLI (subreaper)。setsid の孤児は別 session
    assert all(o["ppid"] == cli["pid"] for o in orphans)
    setsid = [o for o in orphans if o["kind"] == "setsid"][0]
    assert setsid["sid"] == setsid["pid"]
    assert {cli["pid"], *(o["pid"] for o in orphans)} <= set(res["procs_before_kill"])
    # 上限は kernel の interface file から読み戻し、保持 fd は全部 CLOEXEC
    assert res["limits"] == {"pids.max": str(MCG.PIDS_MAX), "memory.max": str(MCG.MEMORY_MAX),
                             "memory.swap.max": "0"}
    assert res["held_cloexec"] == [True, True, True, True]
    s = res["series"]
    assert s["names"][:4] == ["supervisor_stop", "cgroup_kill", "populated_zero",
                              "launcher_reap"]
    assert {x["step"]: x for x in s["steps"]}["populated_zero"]["ok"] is True
    assert res["procs_after"] == []
    assert res["orphans_alive_after"] == []
    assert_cleaned(res)


# --- 受入 19 -------------------------------------------------------------------------

@pytest.fixture(scope="module")
def escape_result(tmp_path_factory):
    reason = du.environment_skip_reason()
    if reason is not None:
        pytest.skip(reason)
    units = du.DisposableUnits(tmp_path_factory.mktemp("escape"))
    try:
        res = run_service(units, "escape", "escape")
    finally:
        units.cleanup()
    assert units.listed() == []
    w = res["worker"]
    assert "exception" not in w, w.get("traceback")
    return res


def test_cli_cannot_escape_mission_cgroup(escape_result):
    w = escape_result["worker"]
    probe = w["probe"]
    for key in ("parent_procs", "root_procs", "own_procs", "subtree_control",
                "root_subtree_control", "own_kill", "parent_kill"):
        assert probe[key] in _DENIED, (key, probe[key])
    assert probe["cgroup_before"] == w["rel"]
    assert probe["cgroup_after"] == w["rel"]
    assert w["rel"].endswith("/" + w["name"])
    assert escape_result["cleanup_mission"] is None
    assert escape_result["cgroup_exists_after"] is False


def test_cgroup_fds_absent_after_exec(escape_result):
    probe = escape_result["worker"]["probe"]
    # listdir 自身が開く /proc/<pid>/fd の dirfd を除けば 0/1/2 だけ
    fds = {k: v for k, v in probe["fds"].items() if not v.endswith("/fd")}
    assert set(fds) == {"0", "1", "2"}, probe["fds"]
    assert not any("cgroup" in v for v in probe["fds"].values())


def test_worker_uses_held_fds_after_isolation(escape_result):
    w = escape_result["worker"]
    # outer Landlock の後は path による open が EACCES
    assert w["path_cgroup.procs"] == "EACCES"
    assert w["path_cgroup.kill"] == "EACCES"
    assert w["path_cgroup.events"] == "EACCES"
    # 保持 fd の join で JOINED まで進み、kill と populated の読みも保持 fd で動く
    assert w["launch"]["started"] is True
    assert w["held_populated_before"] is False
    assert w["held_populated_running"] is True
    steps = {s["step"]: s for s in w["series"]["steps"]}
    assert steps["cgroup_kill"]["ok"] is True
    assert steps["populated_zero"]["ok"] is True
    assert w["held_populated_after"] is False


# --- 受入 20 (worker 側) --------------------------------------------------------------

@pytest.mark.parametrize("kind,reason,detail", [
    ("eexist", MCG.REASON_CREATE_FAILED, "File exists"),
    ("not_supervisor", MCG.REASON_TOPOLOGY_UNSUPPORTED, "supervisor"),
    ("no_memory", MCG.REASON_CREATE_FAILED, "memory.max"),
])
def test_mkdir_and_limit_failures_are_fixed_reasons(disposable_units, kind, reason, detail):
    res = run_service(disposable_units, f"cf-{kind}", f"create_fail:{kind}")
    assert res.get("created") is not True, res
    assert res["reason"] == reason
    assert res["classification"] == "sandbox_unavailable"
    assert detail in res["detail"]
    if kind == "eexist":
        # 既にある cgroup は使い回さない (その 1 個だけが残っている)
        assert len(res["m_dirs"]) == 1
    else:
        # 作りかけを残さない
        assert res["m_dirs"] == []


def test_clear_inheritable_caps_lets_launcher_credentials_pass(disposable_units):
    """user service が継承する CapInh を空にした後の launch_cli は資格検査を通る。
    effective / permitted には触れない。"""
    res = run_service(disposable_units, "capinh", "launch_after_clear")
    (inh_before, caps_before), (inh_after, caps_after) = res["caps_before"], res["caps_after"]
    assert int(inh_after, 16) == 0 and caps_after[2] == 0
    assert caps_after[:2] == caps_before[:2]
    assert res["launch"]["started"] is True
    assert_cleaned(res)
    if int(inh_before, 16) == 0:
        pytest.skip("この unit は CapInh を継承していない (空にする前から 0)")


def test_join_reads_back_through_held_fd_without_proc(disposable_units):
    """outer Landlock が /proc を許さなくても、join は保持した cgroup.procs fd の読み戻しで
    成立する (launcher pid が cgroup.procs に居ることを見る)。"""
    res = run_service(disposable_units, "jnp", "join_no_proc")
    w = res["worker"]
    assert "exception" not in w, w.get("traceback")
    assert w["proc_read"] == "EACCES"
    assert w["joined"] is True and w["unpopulated"] is True
    assert_cleaned(res)


@pytest.mark.parametrize("kind", ["esrch", "mismatch"])
def test_join_failure_is_cgroup_join_failed(disposable_units, kind):
    res = run_service(disposable_units, f"jf-{kind}", f"join_fail:{kind}")
    launch = res["launch"]
    assert launch["started"] is False
    assert launch["reason"] == CLP.CGROUP_JOIN_FAILED
    assert launch["series"] == "before_handoff"
    assert launch["reaped"] is True
    if kind == "esrch":
        assert res["join"]["zombie"] is True
    else:
        assert "読み戻し" in res["join"]["detail"]
    assert res["series"]["names"][:4] == ["close_untransferred", "cgroup_kill",
                                          "populated_zero", "launcher_reap"]
    assert_cleaned(res)


# --- 単一階層・保持 fd の振る舞い (cgroup の外の偽 mount で、kernel に依らない分岐だけ) -------

def test_self_position_requires_single_v2_line():
    with pytest.raises(MCG.MissionCgroupError) as e:
        MCG.MissionCgroup.create("ab" * 16,
                                 proc_self_cgroup="0::/x.service/supervisor\n1:name=x:/b\n")
    assert e.value.reason == MCG.REASON_TOPOLOGY_UNSUPPORTED


def test_launcher_kill_writes_cgroup_kill():
    calls: list[str] = []

    class _Cg:
        def kill_quietly(self) -> bool:
            calls.append("kill_quietly")
            return True

    MCG.launcher_kill(_Cg())()
    assert calls == ["kill_quietly"]


def _held(tmp_path: Path, events: str) -> MCG.MissionCgroup:
    ev = tmp_path / "cgroup.events"
    ev.write_text(events)
    ro = os.open(os.devnull, os.O_RDONLY)  # 書き込めない fd: cgroup.kill の書き込みは失敗する
    return MCG.MissionCgroup("m-" + "ab" * 16, "/x/m-" + "ab" * 16, os.open(os.devnull, os.O_RDONLY),
                             ro, os.open(ev, os.O_RDONLY), os.open(os.devnull, os.O_RDONLY))


def test_kill_quietly_reports_write_failure(tmp_path):
    cg = _held(tmp_path, "populated 0\nfrozen 0\n")
    try:
        assert cg.kill_quietly() is False
    finally:
        cg.close()


def test_unreadable_populated_is_not_empty(tmp_path):
    cg = _held(tmp_path, "frozen 0\n")  # populated の行が無い
    try:
        assert cg.wait_unpopulated(time.monotonic() + 0.2) is False
    finally:
        cg.close()


def test_create_failure_after_opening_fds_closes_them(tmp_path, monkeypatch):
    """interface fd の途中 (cgroup.events) で open に失敗したら、開いた fd を閉じてから
    `cgroup_create_failed` を送出する。"""
    mount = tmp_path / "mnt"
    (mount / "u" / "supervisor").mkdir(parents=True)
    real_mkdir = os.mkdir

    def mkdir_with_files(path, mode=0o777):
        real_mkdir(path, mode)
        for key in ("pids.max", "memory.max", "memory.swap.max", "cgroup.procs", "cgroup.kill"):
            (Path(path) / key).write_text("")

    monkeypatch.setattr(MCG.os, "mkdir", mkdir_with_files)
    before = set(os.listdir("/proc/self/fd"))
    with pytest.raises(MCG.MissionCgroupError) as e:
        MCG.MissionCgroup.create("ab" * 16, proc_self_cgroup="0::/u/supervisor\n",
                                 mount=str(mount))
    monkeypatch.undo()
    assert e.value.reason == MCG.REASON_CREATE_FAILED and "cgroup.events" in e.value.detail
    assert set(os.listdir("/proc/self/fd")) == before
