"""mission の後始末: spool dispatcher の absolute deadline での停止、§4.2.3 の cleanup 系列。

cleanup 系列の試験は使い捨ての user service の中で `_mission_cgroup_service_helper.py` を走らせ、
実 launcher・実 mission cgroup・実 `ConnectSupervisor` で観測する (cgroup.kill / populated /
wait をモックしない)。
"""
from __future__ import annotations

import inspect
import os
import threading
import time
from pathlib import Path

import pytest

from agentic_fx.core import mission_protocol as MP
from agentic_fx.tools import mcp_spool
from agentic_fx.tools import mcp_spool_client as client


def _place(spool: Path, data: bytes) -> str:
    rid = client.new_request_id()
    dirfd = os.open(spool, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = os.open("tmp-" + rid, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dirfd)
        os.write(fd, data)
        os.close(fd)
        client.renameat2_noreplace(dirfd, "tmp-" + rid, "req-" + rid)
    finally:
        os.close(dirfd)
    return rid


_REQ = (b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", '
        b'"params": {"name": "x", "arguments": {}}}')


def test_dispatcher_stop_honours_absolute_deadline(tmp_path):
    root = mcp_spool.SpoolRoot.create(tmp_path, os.urandom(16).hex())
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def handler(request, publish):
        calls.append(request.get("method"))
        started.set()
        release.wait(10)
        publish({"jsonrpc": "2.0", "id": request.get("id"), "result": {}})

    d = mcp_spool.SpoolDispatcher(root, handler)
    d.start()
    try:
        first = _place(root.path, _REQ)
        assert started.wait(5)
        t0 = time.monotonic()
        deadline = t0 + 0.3
        assert d.stop(deadline=deadline) is False  # 実行中の tool が残る
        took = time.monotonic() - t0
        assert took < 0.3 + 0.1, took
        # 停止後は rescan しない: 新しい要求は rescan 周期を越えても claim されない
        second = _place(root.path, _REQ)
        release.set()
        time.sleep(mcp_spool.RESCAN_INTERVAL_SEC + 0.5)
        names = set(os.listdir(root.path))
        assert "req-" + second in names
        assert "claim-" + first in names
        assert calls == ["tools/call"]
        assert d.thread is not None
        d.thread.join(5)
        assert not d.thread.is_alive()
    finally:
        release.set()
        d.stop(1.0)


def test_dispatcher_stop_keeps_relative_form(tmp_path):
    root = mcp_spool.SpoolRoot.create(tmp_path, os.urandom(16).hex())
    d = mcp_spool.SpoolDispatcher(root, lambda req, publish: None)
    d.start()
    assert d.stop(1.0) is True
    sig = inspect.signature(mcp_spool.SpoolDispatcher.stop)
    assert list(sig.parameters)[:2] == ["self", "timeout_sec"]
    assert sig.parameters["timeout_sec"].default == 5.0
    assert sig.parameters["deadline"].kind is inspect.Parameter.KEYWORD_ONLY
    # 互換名も同じ関数 (deadline を受ける)
    assert mcp_spool.SpoolDispatcher.close is mcp_spool.SpoolDispatcher.stop


def test_dispatcher_stop_past_deadline_does_not_wait(tmp_path):
    root = mcp_spool.SpoolRoot.create(tmp_path, os.urandom(16).hex())
    gate = threading.Event()
    d = mcp_spool.SpoolDispatcher(root, lambda req, publish: gate.wait(10))
    d.start()
    _place(root.path, _REQ)
    time.sleep(0.3)
    t0 = time.monotonic()
    assert d.stop(deadline=t0 - 1) is False
    assert time.monotonic() - t0 < 0.05
    gate.set()
    d.stop(2.0)


# --- launcher の停止: kill() の後に必ず pid 宛 SIGKILL -----------------------------

def _sleeper():
    import resource
    import subprocess
    import sys

    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))


@pytest.mark.parametrize("kind", ["noop", "raises", "esrch"])
def test_reap_launcher_always_sigkills_pid_after_kill(kind):
    """cgroup.kill は JOINED より前の launcher (mission cgroup に居ない) に届かない。kill() が
    何もしなくても、例外でも、launcher pid への SIGKILL で deadline 内に reap する。"""
    import signal

    from agentic_fx.core import cli_launch as L

    calls: list[str] = []

    def kill() -> None:
        calls.append("kill")
        if kind == "raises":
            raise OSError(9, "EBADF")
        if kind == "esrch":
            raise ProcessLookupError()

    p = _sleeper()
    try:
        t0 = time.monotonic()
        assert L.reap_launcher(p, deadline=t0 + 3, kill=kill) is True
        assert time.monotonic() - t0 < 1.0
        assert calls == ["kill"]
        assert p.returncode == -signal.SIGKILL
    finally:
        if p.returncode is None:
            p.kill()
            p.wait(5)


# --- 受入 22 (cleanup): §4.2.3 の系列 (使い捨て service の中、実 cgroup・実 launcher) ----

from agentic_fx.core import cli_launch_protocol as CLP  # noqa: E402
from agentic_fx.core import connect_supervisor as CS  # noqa: E402
from agentic_fx.core import mission_cleanup as MC  # noqa: E402
from tests.core._disposable_unit import disposable_units  # noqa: E402,F401 (fixture)
from tests.core.test_mission_cgroup import assert_cleaned, run_service  # noqa: E402

_AFTER = ["supervisor_stop", "cgroup_kill", "populated_zero", "launcher_reap",
          "dispatcher_stop"]
_BEFORE = ["close_untransferred", "cgroup_kill", "populated_zero", "launcher_reap",
           "dispatcher_stop", "supervisor_stop"]


def _steps(res: dict) -> dict:
    return {s["step"]: s for s in res["series"]["steps"]}


@pytest.mark.parametrize("row", ["normal", "receiver_anomaly", "stuck", "before_handoff",
                                 "after_handoff_failure"])
def test_cleanup_rows_follow_canonical_order(disposable_units, row):
    res = run_service(disposable_units, f"row-{row}", f"series:{row}")
    s = res["series"]
    names = s["names"]
    steps = _steps(res)
    if row == "before_handoff":
        assert names == _BEFORE + ["supervisor_join", "supervisor_release"]
        assert res["launch"]["series"] == "before_handoff"
    elif row == "stuck":
        assert names == _AFTER + ["release_skipped"]
    else:
        assert names == _AFTER + ["supervisor_join", "supervisor_release"]
    # dispatcher の停止は populated 0 の後
    assert names.index("populated_zero") < names.index("dispatcher_stop")
    assert steps["populated_zero"]["ok"] is True
    assert s["reason"] is None
    assert s["launcher_reaped"] is True
    if row == "normal":
        # CLI の正常終了を先に wait で検出していれば reap の段は記録だけ
        assert steps["launcher_reap"]["result"] == "already_reaped"
        assert s["supervisor_state"] == CS.CLOSED
    if row == "receiver_anomaly":
        assert s["supervisor_state"] == CS.CLOSED
        assert s["supervisor_failure"] == CS.SUPERVISOR_DIED
    if row == "stuck":
        assert res["state_before_cleanup"] == CS.STUCK
        assert s["supervisor_state"] == CS.STUCK
        # listener は閉じない (receiver だけが閉じる)。release も呼ばない
        assert res["listener_open_after"] is True
        assert res["efd_after"] >= 0
    else:
        assert res["listener_open_after"] is False
        assert res["efd_after"] == -1
    if row == "after_handoff_failure":
        assert res["launch"]["reason"] == CLP.ATTESTATION_DEADLINE
        assert res["launch"]["series"] == "after_handoff"
        assert steps["launcher_reap"]["result"] == "reaped"
    assert res["dispatcher_stopped"] is True
    assert s["deadline_left_ms"] > (MC.WATCHDOG_LEAD_SEC * 1e3)
    assert_cleaned(res)


@pytest.mark.parametrize("when", ["pre_joined_stop", "pre_joined_death", "after_joined"])
def test_handoff_before_failure_closes_untransferred_fds(disposable_units, when):
    res = run_service(disposable_units, f"bh-{when}", f"before_handoff:{when}")
    launch = res["launch"]
    assert launch["started"] is False and launch["series"] == "before_handoff"
    assert launch["reaped"] is True
    if when == "pre_joined_stop":
        # JOINED より前に止まった launcher は cgroup に居ない。pid 宛 SIGKILL で reap
        assert launch["reason"] == CLP.ATTESTATION_DEADLINE
        assert launch["returncode"] == -9
        assert res["launch_ms"] < 1500 + 1000
    elif when == "pre_joined_death":
        assert launch["reason"] == CLP.LAUNCHER_DIED
    else:
        assert launch["reason"] == CLP.ATTESTATION_DEADLINE
        assert launch["returncode"] == -9
    # worker の fd 表に listener も status 親端も残らない
    assert res["new_fds"] == {}, res["new_fds"]
    assert res["series"]["names"][:4] == ["close_untransferred", "cgroup_kill",
                                          "populated_zero", "launcher_reap"]
    assert _steps(res)["populated_zero"]["ok"] is True
    assert_cleaned(res)


@pytest.mark.parametrize("kind", ["LaunchError", "LaunchInterrupted"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_launch_error_and_interrupt_are_cleaned_by_series(disposable_units, kind, when):
    res = run_service(disposable_units, f"le-{kind[6:].lower()}-{when}",
                      f"launch_exc:{kind}:{when}")
    assert res["launch"]["kind"] == kind
    series = "before_handoff" if when == "before" else "after_handoff"
    assert res["launch"]["series"] == series
    # launcher (または CLI) は reap 済み、cgroup は空
    assert res["launcher_returncode"] is not None
    assert res["procs_after"] == []
    if kind == "LaunchInterrupted":
        assert res["reraised"] == "LaunchInterrupted"
    else:
        assert res["reraised"] is None
        names = res["series"]["names"]
        assert names[:4] == (_BEFORE[:4] if when == "before" else _AFTER[:4])
    assert not any(v == "anon_inode:seccomp notify" for v in res["new_fds"].values())
    assert_cleaned(res)


@pytest.mark.parametrize("state", ["closed", "stuck"])
def test_supervisor_stop_join_release_order(disposable_units, state):
    res = run_service(disposable_units, f"sjr-{state}", f"stop_join_release:{state}")
    w = res["worker"]
    names = w["series"]["names"]
    if state == "closed":
        assert names[-2:] == ["supervisor_join", "supervisor_release"]
        assert names.index("supervisor_stop") < names.index("supervisor_join")
        assert w["release_called"] is True
        # stop_efd まで閉じる
        assert w["efd_after"] == -1 and w["efd_fd_open"] is False
        assert w["listener_open_after_series"] is False
        assert res["listener_in_worker_before_exit"] != "anon_inode:seccomp notify"
    else:
        assert w["series"]["supervisor_state"] == CS.STUCK
        assert names[-1] == "release_skipped"
        assert w["release_called"] is False
        assert w["efd_fd_open"] is True
        # listener は worker の exit まで worker の fd 表に残る
        assert w["listener_open_after_series"] is True
        assert res["listener_in_worker_before_exit"] == "anon_inode:seccomp notify"
    assert res["worker_rc"] == 0
    assert res["worker_fd_dir_after_exit"] is False
    assert res["cleanup_mission"] is None and res["cgroup_exists_after"] is False


# --- 受入 43 -----------------------------------------------------------------------

def _ev(res: dict, name: str) -> list[dict]:
    return [e for e in res["events"] if e["event"] == name]


def _notes(res: dict, name: str) -> list[dict]:
    return [n for n in res["worker_notes"] if n["event"] == name]


def test_stuck_blocking_connect_in_real_cgroup(disposable_units):
    res = run_service(disposable_units, "stuck-real", "runner:stuck", timeout=150)
    stuck = _notes(res, "stuck")[0]
    assert stuck["state"] == CS.STUCK and stuck["failure"] == CS.SUPERVISOR_STALLED
    assert _notes(res, "listener")[0]["open"] is True
    lines = _notes(res, "cli_lines")[0]["lines"]
    # 新しい tracee (B) の connect は STUCK の後 block のまま (done が来ない)
    assert [ln for ln in lines if ln.get("role") == "B" and ln.get("event") == "start"]
    assert not [ln for ln in lines if ln.get("event") == "done"]
    series = _notes(res, "series")[0]
    assert series["supervisor_state"] == CS.STUCK
    assert series["names"] == _AFTER[:4] + ["release_skipped"]
    assert {s["step"]: s for s in series["steps"]}["populated_zero"]["ok"] is True
    # STUCK でも listener は系列の後も開いたまま (worker の exit で消える)
    assert _notes(res, "listener_after")[0]["open"] is True
    # WorkerRunner: 要求で 1 回発行、worker は自分で exit、wait で reap
    assert [e["trigger"] for e in _ev(res, "issued")] == [MC.TRIGGER_WORKER_REQUEST]
    assert _ev(res, "reaped") and not _ev(res, "worker_sigkill")
    hook = res["hook"][0]
    assert hook["reaped"] is True and hook["cleanup_mission"] is None
    assert hook["exists_after"] is False and hook["left_after_ms"] > 0
    assert res["blocking_reasons"] == []
    assert res["m_dirs_left"] == []


@pytest.mark.parametrize("mode", ["hang", "hang_unreaped"])
def test_worker_hang_after_stuck_is_sigkilled_and_rewaited(disposable_units, mode):
    res = run_service(disposable_units, f"stuck-{mode.replace('_', '-')}", f"runner:{mode}",
                      timeout=150)
    assert _notes(res, "hang"), "worker は ACK の後、系列の前に止まった"
    assert not _notes(res, "series")
    issued = _ev(res, "issued")[0]
    kill = _ev(res, "worker_sigkill")[0]
    assert kill["t"] == pytest.approx(issued["deadline"] - MC.WORKER_KILL_LEAD_SEC, abs=0.3)
    assert _ev(res, "worker_rewait")
    hook = res["hook"][0]
    # CLI は worker の死後も cgroup に残っている。afx の保険 kill が回収する
    assert hook["procs_before"] != []
    if mode == "hang":
        assert res["worker_returncode"] == -9
        assert hook["reaped"] is True
        assert hook["cleanup_mission"] is None and hook["exists_after"] is False
        assert hook["left_after_ms"] > 0
        assert res["blocking_reasons"] == []
    else:
        # 再 wait の結果を timeout と判定させる: gate が閉じ、rmdir しない
        assert hook["reaped"] is False
        assert hook["exists_after"] is True
        assert res["blocking_reasons"] == ["worker_not_reaped"]
    assert res["m_dirs_left"] == []


def test_stop_after_commit_returns_actual_result(disposable_units):
    res = run_service(disposable_units, "commit-stop", "stop_after_commit")
    assert res["reached"] is True
    assert res["state_when_released"] == CS.STOPPING
    assert res["cli"]["rc"] == 0
    assert res["proxy_accepted"] == 1 and res["acked"] == 1
    assert res["stop_result"] == [CS.CLOSED]
    assert res["series"]["supervisor_state"] == CS.CLOSED
    assert_cleaned(res)


# --- worker 側の ACK 予約 -----------------------------------------------------------

def test_worker_acks_once_even_for_concurrent_waiters():
    """`wait_deadline` が並行に呼ばれても ACK は 1 回だけ。2 回目以降は同じ deadline を
    返し、ACK が送り終わる前には返らない。"""
    sent: list[dict] = []
    gate = threading.Event()

    def send(frame: dict) -> None:
        gate.wait(5)  # 1 回目の送信を止めて、2 回目を待たせる
        sent.append(frame)

    ch = MC.WorkerCleanupChannel(send, exit_fn=lambda code: None, clock=lambda: 0.0)
    ch.deliver({"type": MP.CLEANUP_DEADLINE, "seq": 1, "deadline": 10.0})
    results: list = []
    ts = [threading.Thread(target=lambda: results.append(ch.wait_deadline(5))) for _ in range(2)]
    for t in ts:
        t.start()
    time.sleep(0.3)
    assert results == [], "ACK を送り終えるまでどちらも返らない"
    gate.set()
    for t in ts:
        t.join(5)
    assert len(sent) == 1 and sent[0]["type"] == MP.CLEANUP_ACK
    assert [r.value for r in results] == [10.0, 10.0]
    assert ch.wait_deadline(0).value == 10.0 and len(sent) == 1


def test_worker_ack_send_failure_releases_reservation():
    """ACK の送信が失敗したら予約を解いて例外を伝える。呼び直すと再送し、成功した ACK の
    deadline を返す。watchdog は送信の前に 1 回だけ起こす。"""
    attempts: list[dict] = []

    def send(frame: dict) -> None:
        attempts.append(frame)
        if len(attempts) == 1:
            raise BrokenPipeError("first send fails")

    ch_ = MC.WorkerCleanupChannel(send, exit_fn=lambda code: None, clock=lambda: 0.0)
    ch_.deliver({"type": MP.CLEANUP_DEADLINE, "seq": 1, "deadline": 10.0})
    with pytest.raises(BrokenPipeError):
        ch_.wait_deadline(1)
    first_watchdog = ch_.watchdog
    assert first_watchdog is not None
    got = ch_.wait_deadline(1)
    assert got is not None and got.value == 10.0
    assert [a["type"] for a in attempts] == [MP.CLEANUP_ACK, MP.CLEANUP_ACK]
    assert ch_.watchdog is first_watchdog
    # 以後は送らずに同じ値
    assert ch_.wait_deadline(0).value == 10.0 and len(attempts) == 2


def test_worker_watchdog_runs_even_if_ack_cannot_be_sent():
    now = [100.0]
    exits: list[int] = []

    def send(frame: dict) -> None:
        raise BrokenPipeError("parent gone")

    ch_ = MC.WorkerCleanupChannel(send, exit_fn=exits.append, clock=lambda: now[0])
    ch_.deliver({"type": MP.CLEANUP_DEADLINE, "seq": 1,
                 "deadline": 100.0 + MC.WATCHDOG_LEAD_SEC + 0.5})
    with pytest.raises(BrokenPipeError):
        ch_.wait_deadline(1)
    now[0] = 101.0
    ch_.watchdog.join(5)
    assert exits == [MC.WATCHDOG_EXIT_CODE]


# --- 系列の判定 (cgroup / supervisor / dispatcher / launcher は呼び出しを記録するだけの代役) ----

class _RecordingCgroup:
    """`cgroup.kill` と `populated 0` の待ちの呼び出しを記録し、待ちの結果を返す代役。"""

    def __init__(self, unpopulated: bool = True) -> None:
        self.calls: list = []
        self._unpopulated = unpopulated

    def kill_quietly(self) -> bool:
        self.calls.append("kill")
        return True

    def wait_unpopulated(self, deadline: float) -> bool:
        self.calls.append(("wait_unpopulated", deadline))
        return self._unpopulated


class _RecordingSupervisor:
    def __init__(self) -> None:
        self.drains: list[tuple[float, float]] = []
        self.failure = None

    def stop(self, deadline: float) -> str:
        self.drains.append((time.monotonic(), deadline))
        return CS.CLOSED

    def join(self, timeout: float) -> bool:
        return True

    def release(self) -> None:
        pass


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.deadlines: list = []

    def stop(self, timeout_sec: float = 5.0, *, deadline: float | None = None) -> bool:
        self.deadlines.append(deadline)
        return True


class _UnreapableLauncher:
    """wait が必ず timeout する launcher の代役 (pid は存在しない番号)。"""

    pid = 2 ** 22 + 12345
    returncode = None

    def kill(self) -> None:
        pass

    def wait(self, timeout=None):
        import subprocess
        raise subprocess.TimeoutExpired("launcher", timeout)


def _far_deadline() -> MC.AckedDeadline:
    return MC.AckedDeadline(time.monotonic() + 60.0)


def test_series_without_populated_zero_reports_cleanup_timeout():
    deadline = MC.AckedDeadline(time.monotonic() + MC.CLEANUP_BUDGET_SEC)
    cg = _RecordingCgroup(unpopulated=False)
    rep = MC.run_cleanup_series(deadline=deadline, series=MC.AFTER_HANDOFF, cgroup=cg)
    assert cg.calls == ["kill", ("wait_unpopulated", deadline.value - MC.WATCHDOG_LEAD_SEC)]
    assert {s["step"]: s for s in rep.steps}["populated_zero"]["ok"] is False
    assert rep.reason == MC.REASON_CGROUP_CLEANUP_TIMEOUT


def test_series_bounds_supervisor_drain_and_passes_deadline_to_dispatcher():
    deadline = _far_deadline()
    sup, disp = _RecordingSupervisor(), _RecordingDispatcher()
    rep = MC.run_cleanup_series(deadline=deadline, series=MC.AFTER_HANDOFF,
                                cgroup=_RecordingCgroup(), supervisor=sup, dispatcher=disp)
    limit = deadline.value - MC.WATCHDOG_LEAD_SEC
    (called_at, drain), = sup.drains
    # drain は handler の期限 (今から HANDLER_DEADLINE_SEC) を越えず、worker の上限も越えない
    assert drain <= called_at + CS.HANDLER_DEADLINE_SEC + 0.05
    assert drain <= limit
    assert disp.deadlines == [limit]
    assert rep.reason is None


def test_series_with_unreaped_launcher_reports_deadline_exceeded():
    rep = MC.run_cleanup_series(deadline=MC.AckedDeadline(time.monotonic() + 3.5),
                                series=MC.BEFORE_HANDOFF, cgroup=_RecordingCgroup(),
                                launcher_proc=_UnreapableLauncher())
    assert rep.launcher_reaped is False
    assert {s["step"]: s for s in rep.steps}["launcher_reap"]["result"] == "not_reaped"
    assert rep.reason == MC.REASON_CLEANUP_DEADLINE_EXCEEDED


def test_series_before_handoff_closes_untransferred_fds():
    r, w = os.pipe()
    try:
        rep = MC.run_cleanup_series(deadline=_far_deadline(), series=MC.BEFORE_HANDOFF,
                                    cgroup=_RecordingCgroup(), untransferred_fds=(r, w))
        assert {s["step"]: s for s in rep.steps}["close_untransferred"]["fds"] == [r, w]
        for fd in (r, w):
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        for fd in (r, w):
            try:
                os.close(fd)
            except OSError:
                pass


def test_series_requires_acked_deadline():
    cg = _RecordingCgroup()
    with pytest.raises(TypeError):
        MC.run_cleanup_series(deadline=time.monotonic() + 10, series=MC.AFTER_HANDOFF, cgroup=cg)
    assert cg.calls == []


def test_cleanup_after_launch_without_ack_starts_nothing():
    from agentic_fx.core import cli_launch as L

    cg = _RecordingCgroup()
    rep = MC.cleanup_after_launch(L.LaunchError(_UnreapableLauncher(), MC.BEFORE_HANDOFF),
                                  acquire=lambda: None, cgroup=cg)
    assert cg.calls == []
    assert rep.steps == []
    assert rep.reason == MC.REASON_CLEANUP_DEADLINE_EXCEEDED


def test_worker_rejects_second_cleanup_deadline():
    ch_ = MC.WorkerCleanupChannel(lambda frame: None, exit_fn=lambda code: None,
                                  clock=lambda: 100.0)
    ch_.deliver({"type": MP.CLEANUP_DEADLINE, "seq": 1, "deadline": 105.0})
    with pytest.raises(MP.ProtocolError):
        ch_.deliver({"type": MP.CLEANUP_DEADLINE, "seq": 2, "deadline": 106.0})
    assert ch_.wait_deadline(0).value == 105.0


def test_reap_worker_rewait_timeout_is_not_reaped():
    protocol = MC.CleanupProtocol()
    reaped = MC.reap_worker(_UnreapableLauncher(), time.monotonic() + 2.5, protocol)
    assert reaped is False
    names = [e["event"] for e in protocol.events]
    assert names == ["worker_wait", "worker_sigkill", "worker_rewait", "not_reaped"]


def test_cleanup_frame_names_are_fixed():
    assert (MP.CLEANUP_REQUEST, MP.CLEANUP_DEADLINE, MP.CLEANUP_ACK) == (
        "cleanup_request", "cleanup_deadline", "cleanup_ack")


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), "1.0", None])
def test_cleanup_deadline_must_be_a_finite_number(bad):
    with pytest.raises(MP.ProtocolError):
        MP.parse_cleanup_deadline({"type": MP.CLEANUP_DEADLINE, "deadline": bad},
                                  now=0.0, max_ahead=MC.DEADLINE_MAX_AHEAD_SEC)
    assert MP.parse_cleanup_deadline({"type": MP.CLEANUP_DEADLINE, "deadline": 3},
                                     now=0.0, max_ahead=MC.DEADLINE_MAX_AHEAD_SEC) == 3.0


NOW_W = 1000.0


@pytest.mark.parametrize("value", [
    NOW_W - 0.001,                                   # 過去
    NOW_W + MC.CLEANUP_BUDGET_SEC + 1.0 + 0.001,      # 過大 (watchdog が実質無効になる)
    -1.0,                                            # 負
    1e18,
])
def test_worker_rejects_cleanup_deadline_out_of_range(value):
    """worker は受け取った deadline を自分の時計で範囲検査し、外れたら protocol 違反にする
    (即 `os._exit` する watchdog も、効かない watchdog も起こさない)。"""
    exits: list[int] = []
    ch_ = MC.WorkerCleanupChannel(lambda frame: None, exit_fn=exits.append,
                                  clock=lambda: NOW_W)
    with pytest.raises(MP.ProtocolError):
        ch_.deliver({"type": MP.CLEANUP_DEADLINE, "seq": 1, "deadline": value})
    assert ch_.wait_deadline(0) is None
    assert ch_.watchdog is None and exits == []


@pytest.mark.parametrize("value", [NOW_W, NOW_W + MC.CLEANUP_BUDGET_SEC + 1.0])
def test_worker_accepts_cleanup_deadline_at_range_edges(value):
    ch_ = MC.WorkerCleanupChannel(lambda frame: None, exit_fn=lambda code: None,
                                  clock=lambda: NOW_W)
    ch_.deliver({"type": MP.CLEANUP_DEADLINE, "seq": 1, "deadline": value})
    assert ch_.wait_deadline(0).value == value
