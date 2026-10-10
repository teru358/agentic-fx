"""connect supervisor を実 seccomp user-notif、実子 process、実 kernel で確かめる。

- tracee は `_notif_tracee.py` の factory が fork する子で、connect だけを USER_NOTIF に
  する最小 filter の下にいる (宛先・errno・race の切り分けのため)。bind 無し listen と
  結合試験 1 本は、本物の launcher が最終 filter `improve-cli/1` を掛けた CLI で確かめる。
- 宛先の port は全部 127.0.0.1 の一時 port。proxy 役と opencode の LLM 上流役
  (本番 8080) は一時 port に置き換える。
- 観測点の停止・遅延・例外は supervisor の constructor に渡す `Faults` だけで入れる。
"""
from __future__ import annotations

import ast
import errno
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from agentic_fx.core import cli_launch as L
from agentic_fx.core import cli_launcher
from agentic_fx.core import connect_supervisor as CS
from agentic_fx.core import landlock_nested
from tests.core._notif_tracee import Factory, Gate, run_worker

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine != "x86_64",
    reason="connect supervisor は x86_64 Linux 専用")

PY = os.path.realpath(sys.executable)
EPERM, EAGAIN, EINVAL = -errno.EPERM, -errno.EAGAIN, -errno.EINVAL


def _kernel_unready(why: str) -> None:
    if os.environ.get("AFX_REQUIRE_SANDBOX_PROBES") == "1":
        pytest.fail(why)
    pytest.skip(why)


def require_kernel() -> None:
    missing = CS.probe_supervisor_capabilities()
    if CS.PTRACE_SCOPE_UNSUPPORTED in missing:
        _kernel_unready("Yama ptrace_scope >= 2: 代行の試験は行えない")
    if missing:
        _kernel_unready(f"supervisor の kernel 能力が足りない: {missing}")
    if cli_launcher.probe_seccomp_capabilities():
        _kernel_unready("seccomp の能力が足りない")


# --- 共通の道具 ---------------------------------------------------------------

def fd_table() -> dict[int, str]:
    out = {}
    for name in os.listdir("/proc/self/fd"):
        try:
            out[int(name)] = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            pass
    return out


def leaked(before: dict[int, str], after: dict[int, str]) -> dict[int, str]:
    return {fd: link for fd, link in after.items() if before.get(fd) != link}


def special_fds() -> list[str]:
    return [link for link in fd_table().values()
            if link == L.SECCOMP_NOTIFY_LINK or link == "anon_inode:[pidfd]"]


class Listener:
    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0

    def drain(self) -> int:
        while True:
            try:
                c, _ = self.sock.accept()
            except BlockingIOError:
                return self.accepted
            c.close()
            self.accepted += 1

    def close(self) -> None:
        self.sock.close()


class ImmediateAck:
    """proxy の同期 handoff の代役: client tuple を記録して即時に ack を返す。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.pending: list[object] = []
        self.acked: list[tuple[str, int]] = []
        self.removed = 0

    def add_pending(self, dup_fd: int) -> object:
        entry = object()
        self.pending.append(entry)
        return entry

    def wait_ack(self, entry, client, deadline):
        self.acked.append(client)
        return self.ok

    def remove_pending(self, entry) -> None:
        self.pending.remove(entry)
        self.removed += 1


@pytest.fixture(scope="module")
def factory():
    require_kernel()
    f = Factory()
    yield f
    f.close()


class Env:
    def __init__(self, factory: Factory) -> None:
        self.factory = factory
        self.proxy = Listener()
        self.direct = Listener()
        self.other = Listener()
        self.sups: list[CS.ConnectSupervisor] = []
        self.tracees = []

    def dest(self, direct: bool = True) -> CS.Destinations:
        return CS.Destinations(self.proxy.port, self.direct.port if direct else None)

    def start(self, *, faults: CS.Faults | None = None, handoff=None, direct: bool = True,
              subreaper: bool = False, **kw):
        t = self.factory.spawn(subreaper=subreaper)
        self.tracees.append(t)
        sup = CS.ConnectSupervisor(self.dest(direct), faults=faults,
                                   proxy_handoff=ImmediateAck() if handoff is None else handoff,
                                   **kw)
        self.sups.append(sup)
        sup.handoff(t.take_listener())
        return t, sup

    def close(self) -> None:
        for sup in self.sups:
            sup.stop(time.monotonic() + 5)
            assert sup.join(5), "supervisor の thread が残った"
            sup.release()
        for t in self.tracees:
            t.close()
        for x in (self.proxy, self.direct, self.other):
            x.close()


@pytest.fixture
def env(factory):
    before = fd_table()
    e = Env(factory)
    try:
        yield e
    finally:
        e.close()
    assert not leaked(before, fd_table()), "fd が残った"
    assert special_fds() == []


def connect(t, timeout: float = 5.0, **kw) -> dict:
    return t.call("connect", timeout=timeout, **kw)


# --- 受入 3 -------------------------------------------------------------------

_DEST_CASES = [
    # (名前, connect の引数を作る関数, 期待 rc, 接続されるべき listener)
    ("proxy", lambda e: dict(port=e.proxy.port), 0, "proxy"),
    ("direct", lambda e: dict(port=e.direct.port), 0, "direct"),
    ("other_port", lambda e: dict(port=e.other.port), -errno.ECONNREFUSED, None),
    ("ipv6_loopback", lambda e: dict(family="inet6", host="::1", port=e.proxy.port),
     -errno.EAFNOSUPPORT, None),
    ("ipv4_mapped", lambda e: dict(family="inet6", host="::ffff:127.0.0.1",
                                   port=e.proxy.port), -errno.EAFNOSUPPORT, None),
    ("af_unspec", lambda e: dict(family="unspec"), -errno.EAFNOSUPPORT, None),
    ("ipv6_scope_id", lambda e: dict(family="inet6", host="fe80::1", port=e.proxy.port,
                                     scope_id=1), -errno.EINVAL, None),
    ("loopback_127_0_0_2", lambda e: dict(host="127.0.0.2", port=e.proxy.port),
     -errno.ECONNREFUSED, None),
]


@pytest.mark.parametrize("name, args, want, target", _DEST_CASES,
                         ids=[c[0] for c in _DEST_CASES])
def test_only_allowed_destinations_are_connected(env, name, args, want, target):
    t, sup = env.start()
    r = connect(t, **args(env))
    assert r["rc"] == want, r
    counts = {"proxy": env.proxy.drain(), "direct": env.direct.drain(),
              "other": env.other.drain()}
    expect = {k: (1 if k == target else 0) for k in counts}
    assert counts == expect
    if target is None:
        # kernel でなく supervisor の段 8 が拒否した
        assert [(x["stage"], x["result"]) for x in sup.records if "stage" in x] == [(8, want)]
    assert sup.failure is None and sup.state == CS.RUNNING


def test_direct_port_is_closed_unless_configured(env):
    """LLM 上流の宛先は opencode の構成だけ。他の backend では他の port と同じに拒否。"""
    t, _sup = env.start(direct=False)
    assert connect(t, port=env.direct.port)["rc"] == -errno.ECONNREFUSED
    assert env.direct.drain() == 0
    assert CS.destinations_for("opencode", 4321) == CS.Destinations(4321, 8080)
    assert CS.destinations_for("claude", 4321) == CS.Destinations(4321, None)
    assert CS.destinations_for("codex", 4321) == CS.Destinations(4321, None)


def test_proxy_without_handoff_is_eperm(env, factory):
    """proxy の同期 handoff が無い構成では proxy 宛を接続しない (fail closed)。"""
    t = factory.spawn()
    env.tracees.append(t)
    sup = CS.ConnectSupervisor(env.dest())
    env.sups.append(sup)
    sup.handoff(t.take_listener())
    assert connect(t, port=env.proxy.port)["rc"] == EPERM
    assert env.proxy.drain() == 0
    assert connect(t, port=env.direct.port)["rc"] == 0


def test_proxy_ack_missing_refuses_and_shuts_down(env):
    ack = ImmediateAck(ok=False)
    t, _sup = env.start(handoff=ack)
    r = connect(t, port=env.proxy.port)
    assert r["rc"] == -errno.ECONNREFUSED, r
    assert len(ack.acked) == 1 and ack.acked[0][0] == "127.0.0.1"
    assert ack.removed == 1 and ack.pending == []


def test_proxy_ack_receives_client_tuple(env):
    ack = ImmediateAck()
    t, _sup = env.start(handoff=ack)
    r = connect(t, port=env.proxy.port, keep=True)
    assert r["rc"] == 0
    assert ack.acked and ack.acked[0][0] == "127.0.0.1" and ack.acked[0][1] > 0
    assert ack.removed == 1


def test_other_uid_localhost_listener_is_not_reachable(env):
    """別 uid の localhost listener の代わりに、許可外 port で待つ listener を置く。
    宛先は port の完全一致で閉じているので、待ち手の uid は結果を変えない。"""
    t, _sup = env.start()
    for _ in range(3):
        assert connect(t, port=env.other.port)["rc"] == -errno.ECONNREFUSED
    assert env.other.drain() == 0


# --- 受入 4 -------------------------------------------------------------------

@pytest.mark.parametrize("target", ["proxy", "direct"])
def test_blocking_and_nonblocking_connect(env, target):
    t, _sup = env.start()
    port = getattr(env, target).port
    b = connect(t, port=port)
    assert b["rc"] == 0 and b["nonblock_after"] is False, b
    nb = connect(t, port=port, nonblock=True)
    assert nb["rc"] == -errno.EINPROGRESS, nb
    assert nb["so_error"] == 0
    assert nb["nonblock_after"] is True
    assert getattr(env, target).drain() == 2


def test_reconnect_thread_and_grandchild(env):
    t, sup = env.start()
    r = connect(t, port=env.proxy.port, twice=True)
    assert r["rc"] == 0 and r["second"] == -errno.EISCONN, r
    main = connect(t, port=env.proxy.port, sync=True)
    assert main["rc"] == 0 and main["tid"] == main["pid"]
    th = connect(t, port=env.proxy.port)
    assert th["rc"] == 0 and th["tid"] != th["pid"]
    gc = connect(t, port=env.proxy.port, where="grandchild")
    assert gc["rc"] == 0 and gc["pid"] != t.pid and gc["ppid"] == t.pid
    assert env.proxy.drain() == 4
    tids = {rec["tid"] for rec in sup.records if rec.get("result") == 0}
    assert {th["tid"], gc["tid"], main["tid"]} <= tids


def test_orphan_under_subreaper_is_supervised(env):
    t, _sup = env.start(subreaper=True)
    r = connect(t, port=env.proxy.port, where="orphan", timeout=10)
    assert r["ppid"] == t.pid, r
    assert r["rc"] == 0, r
    assert env.proxy.drain() == 1


@pytest.mark.parametrize("addrlen, want", [(15, EINVAL), (16, 0), (128, 0), (129, EINVAL)])
def test_addrlen_bounds(env, addrlen, want):
    t, sup = env.start()
    r = connect(t, port=env.proxy.port, addrlen=addrlen)
    assert r["rc"] == want, r
    # 範囲外は kernel に渡す前に段 2 で止まる
    stages = [x["stage"] for x in sup.records if "stage" in x]
    assert stages == ([2] if want else [12])
    assert env.proxy.drain() == (1 if want == 0 else 0)


def test_unmapped_sockaddr_is_efault(env):
    t, _sup = env.start()
    assert connect(t, port=env.proxy.port, unmapped=True)["rc"] == -errno.EFAULT


@pytest.mark.parametrize("to, want", [("udp", EPERM), ("pipe", -errno.ENOTSOCK),
                                      ("close", EPERM)])
def test_fd_swap_is_rechecked(env, to, want):
    gate = Gate("pidfd_getfd")
    t, sup = env.start(faults=CS.Faults(at=gate))
    fd = t.call("socket", sync=True)["fd"]
    cid = t.start("connect", fd=fd, port=env.proxy.port, keep=True)
    assert gate.wait()
    t.call("swap", sync=True, fd=fd, to=to)
    gate.release()
    r = t.result(cid)
    assert r["rc"] == want, r
    assert env.proxy.drain() == 0
    assert sup.failure is None
    # 同じ supervisor は続けて代行する
    assert connect(t, port=env.proxy.port)["rc"] == 0


def test_tracee_death_and_signal(env):
    # SIGKILL: SEND は ENOENT、資源は残らない
    gate = Gate("before_send")
    t, sup = env.start(faults=CS.Faults(at=gate))
    cid = t.start("connect", port=env.proxy.port)
    assert gate.wait()
    t.kill()
    assert t.wait_gone()
    gate.release()
    deadline = time.monotonic() + 3
    while sup.in_flight and time.monotonic() < deadline:
        time.sleep(0.01)
    rec = [r for r in sup.records if r.get("id") == gate.ids[0]]
    assert rec and rec[0]["send"] == "enoent", rec
    assert sup.failure is None
    assert t.result(cid, 0.2) is None

    # 非致死 signal: WAIT_KILLABLE_RECV の下で通知は 1 件のまま、結果は実値
    gate2 = Gate("before_send")
    t2, sup2 = env.start(faults=CS.Faults(at=gate2))
    t2.call("sighandler", sync=True)
    cid = t2.start("connect", port=env.proxy.port)
    assert gate2.wait()
    for _ in range(5):
        os.kill(t2.pid, 10)  # SIGUSR1
        time.sleep(0.02)
    gate2.release()
    r = t2.result(cid)
    assert r["rc"] == 0, r
    assert len([x for x in sup2.records if x.get("tid") == r["tid"]]) == 1
    assert t2.call("sigcount", sync=True)["count"] >= 1


def test_slot_saturation_returns_eagain(env):
    gate = Gate("handler", count=CS.CONNECT_SLOTS, hold=5)
    t, sup = env.start(faults=CS.Faults(at=gate))
    cids = [t.start("connect", port=env.proxy.port) for _ in range(CS.CONNECT_SLOTS + 1)]
    assert gate.wait(timeout=5)
    early = []
    end = time.monotonic() + 1.0
    while time.monotonic() < end and not early:
        for c in cids:
            r = t.result(c, 0.02)
            if r is not None:
                early.append((c, r))
                break
    assert len(early) == 1 and early[0][1]["rc"] == EAGAIN, early
    gate.release()
    rest = [t.result(c) for c in cids if c != early[0][0]]
    assert [r["rc"] for r in rest] == [0] * CS.CONNECT_SLOTS
    assert env.proxy.drain() == CS.CONNECT_SLOTS
    assert sup.failure is None


# --- 受入 24 ------------------------------------------------------------------

def test_pidfd_getfd_failure_is_contained(env):
    gate = Gate("pidfd_getfd")
    t, sup = env.start(faults=CS.Faults(at=gate))
    fd = t.call("socket", sync=True)["fd"]
    cid = t.start("connect", fd=fd, port=env.proxy.port, keep=True)
    assert gate.wait()
    t.call("swap", sync=True, fd=fd, to="close")
    gate.release()
    assert t.result(cid)["rc"] in (EPERM, -errno.ENOTSOCK)
    assert sup.failure is None and sup.state == CS.RUNNING
    assert connect(t, port=env.proxy.port)["rc"] == 0


def test_memory_copy_failure_is_efault(env):
    t, sup = env.start()
    assert connect(t, port=env.proxy.port, unmapped=True)["rc"] == -errno.EFAULT
    t2, sup2 = env.start(faults=CS.Faults(vm_read_len=lambda n: n - 1))
    assert connect(t2, port=env.proxy.port)["rc"] == -errno.EFAULT
    assert env.proxy.drain() == 0
    assert sup.failure is None and sup2.failure is None


def test_receiver_stop_failure_marks_supervisor_failed(env):
    seen: list = []

    def boom(point, nid):
        if point == "after_poll":
            raise RuntimeError("injected receiver failure")

    t, sup = env.start(faults=CS.Faults(at=boom), on_fault=seen.append)
    r = connect(t, port=env.proxy.port)
    assert r["rc"] == -errno.ENOSYS
    assert sup.wait_terminal(3) and sup.state == CS.CLOSED
    assert sup.failure.reason == CS.SUPERVISOR_DIED
    assert sup.failure.classification == "sandbox_unavailable"
    assert [f.reason for f in seen] == [CS.SUPERVISOR_DIED]

    def slow(point, nid):
        if point == "handler":
            time.sleep(CS.HANDLER_DEADLINE_SEC + 0.4)

    t2, sup2 = env.start(faults=CS.Faults(at=slow))
    r = connect(t2, port=env.proxy.port, timeout=8)
    assert r["rc"] == EPERM
    assert sup2.wait_terminal(5) and sup2.state == CS.CLOSED
    assert sup2.failure.reason == CS.SUPERVISOR_STALLED
    assert env.proxy.drain() == 0


# --- 受入 29 ------------------------------------------------------------------

@pytest.mark.parametrize("point, stage", [("id_valid_1", 3), ("id_valid_2", 5),
                                          ("id_valid_3", 7), ("id_valid_4", 10)])
def test_id_valid_seams_discard_resources(env, point, stage):
    gate = Gate(point)
    t, sup = env.start(faults=CS.Faults(at=gate))
    before = fd_table()
    cid = t.start("connect", port=env.proxy.port)
    assert gate.wait()
    t.kill()
    assert t.wait_gone()
    gate.release()
    deadline = time.monotonic() + 3
    while sup.in_flight and time.monotonic() < deadline:
        time.sleep(0.01)
    rec = [r for r in sup.records if r.get("id") == gate.ids[0]]
    assert rec and rec[0]["result"] == "silent" and rec[0]["stage"] == stage, rec
    assert "send" not in rec[0]
    assert not leaked(before, fd_table())
    assert special_fds().count("anon_inode:[pidfd]") == 0
    assert env.proxy.drain() == 0
    assert t.result(cid, 0.1) is None
    assert sup.failure is None


def test_pidfd_failure_and_send_enoent(env):
    # 段 4: 通知が有効なら EPERM
    t, sup = env.start(faults=CS.Faults(pidfd_open_errno=lambda tid: errno.ESRCH))
    assert connect(t, port=env.proxy.port)["rc"] == EPERM
    rec = [r for r in sup.records if r.get("stage") == 4]
    assert rec and rec[0]["pidfd_errno"] == errno.ESRCH

    # 段 4: 通知が無効なら無応答
    gate = Gate("pidfd_open")
    t2, sup2 = env.start(faults=CS.Faults(at=gate, pidfd_open_errno=lambda tid: errno.ESRCH))
    t2.start("connect", port=env.proxy.port)
    assert gate.wait()
    t2.kill()
    assert t2.wait_gone()
    gate.release()
    deadline = time.monotonic() + 3
    while sup2.in_flight and time.monotonic() < deadline:
        time.sleep(0.01)
    rec = [r for r in sup2.records if r.get("id") == gate.ids[0]]
    assert rec[0]["result"] == "silent" and rec[0]["stage"] == 4

    # 段 12: SEND の ENOENT は正常な race として記録
    gate3 = Gate("before_send")
    t3, sup3 = env.start(faults=CS.Faults(at=gate3))
    t3.start("connect", port=env.proxy.port)
    assert gate3.wait()
    t3.kill()
    assert t3.wait_gone()
    gate3.release()
    deadline = time.monotonic() + 3
    while sup3.in_flight and time.monotonic() < deadline:
        time.sleep(0.01)
    rec = [r for r in sup3.records if r.get("id") == gate3.ids[0]]
    assert rec[0]["send"] == "enoent" and rec[0]["stage"] == 12
    assert sup3.failure is None
    assert special_fds().count("anon_inode:[pidfd]") == 0


# --- 受入 30 ------------------------------------------------------------------

def _raise_at(where: str):
    def at(point, nid):
        if point == where:
            raise RuntimeError(f"injected at {where}")
    return at


def _sleep_at(where: str, sec: float):
    def at(point, nid):
        if point == where:
            time.sleep(sec)
    return at


_INPROC_FAULTS = [
    # (名前, Faults, tracee の結果, 終状態, reason)。SEND の失敗だけは connect 済み
    ("receiver_exception", CS.Faults(at=_raise_at("receiver_loop")), -errno.ENOSYS,
     CS.CLOSED, CS.SUPERVISOR_DIED),
    ("handler_exception", CS.Faults(at=_raise_at("handler")), EPERM, CS.CLOSED,
     CS.SUPERVISOR_DIED),
    ("handler_deadline", CS.Faults(at=_sleep_at("before_commit",
                                                CS.HANDLER_DEADLINE_SEC + 0.4)),
     EPERM, CS.CLOSED, CS.SUPERVISOR_STALLED),
    ("recv_errno", CS.Faults(recv_errno=lambda: errno.EBADF), -errno.ENOSYS, CS.CLOSED,
     CS.SUPERVISOR_DIED),
    ("send_errno", CS.Faults(send_errno=lambda nid: errno.EINVAL), -errno.ENOSYS, CS.CLOSED,
     CS.SUPERVISOR_DIED),
    ("pre_register_exception", CS.Faults(at=_raise_at("after_recv")), -errno.ENOSYS,
     CS.CLOSED, CS.SUPERVISOR_DIED),
]


@pytest.mark.parametrize("name, faults, want, state, reason", _INPROC_FAULTS,
                         ids=[c[0] for c in _INPROC_FAULTS])
def test_fault_injection_matrix(env, name, faults, want, state, reason):
    t, sup = env.start(faults=faults)
    r = connect(t, port=env.proxy.port, timeout=8)
    assert r["rc"] == want, r
    assert sup.wait_terminal(5)
    assert sup.state == state
    assert sup.failure is not None and sup.failure.reason == reason
    assert env.proxy.drain() == (1 if name == "send_errno" else 0)
    # CLOSED の後の connect は ENOSYS
    assert connect(t, port=env.proxy.port)["rc"] == -errno.ENOSYS


def test_fault_matrix_slot_saturation_is_not_a_fault(env):
    gate = Gate("handler", count=CS.CONNECT_SLOTS, hold=5)
    t, sup = env.start(faults=CS.Faults(at=gate))
    cids = [t.start("connect", port=env.proxy.port) for _ in range(CS.CONNECT_SLOTS + 1)]
    assert gate.wait()
    time.sleep(0.2)
    gate.release()
    rcs = sorted(t.result(c)["rc"] for c in cids)
    assert rcs.count(EAGAIN) == 1 and rcs.count(0) == CS.CONNECT_SLOTS
    assert sup.failure is None and sup.state == CS.RUNNING


def test_fault_matrix_drain_waits_for_handlers(env):
    gate = Gate("after_commit")
    t, sup = env.start(faults=CS.Faults(at=gate))
    cid = t.start("connect", port=env.proxy.port)
    assert gate.wait()
    box = {}
    th = threading.Thread(target=lambda: box.update(r=sup.stop(time.monotonic() + 5)))
    th.start()
    time.sleep(0.2)
    assert sup.state == CS.STOPPING and sup.in_flight == 1
    gate.release()
    th.join(6)
    assert box["r"] == CS.CLOSED
    assert t.result(cid)["rc"] == 0
    assert env.proxy.drain() == 1
    assert sup.failure is None


def test_fault_matrix_stuck_cases():
    require_kernel()
    port_holder = Listener()
    try:
        pre = run_worker("pre_register_stuck", {"port": port_holder.port, "hold": 4.0})
        assert "exception" not in pre, pre
        assert pre["terminal"] and pre["state"] == CS.STUCK, pre
        assert pre["reason"] == CS.SUPERVISOR_DIED
        assert pre["listener_open"] is True
        assert pre["second_result"] is None  # 登録前例外の通知は block のまま

        dl = run_worker("cleanup_deadline", {"port": port_holder.port, "hold": 4.0,
                                             "deadline": 0.3})
        assert "exception" not in dl, dl
        assert dl["stop_result"] == CS.STUCK and dl["state"] == CS.STUCK
        assert dl["stop_ms"] < 600, dl
        assert dl["listener_open"] is True
    finally:
        port_holder.close()


def test_stuck_keeps_listener_open_and_blocks_new_tracee():
    require_kernel()
    out = run_worker("stuck_handler")
    assert "exception" not in out, out
    assert out["terminal_after_watchdog"] is True
    assert out["state"] == CS.STUCK and out["stop_result"] == CS.STUCK
    assert out["reason"] == CS.SUPERVISOR_STALLED
    assert out["on_fault"] == [CS.SUPERVISOR_STALLED]
    assert out["listener_open"] is True
    assert out["first_result"] is None
    assert out["second_result"] is None
    assert out["listener_pending"] is True  # 新しい通知は受信されずに残る
    assert out["stop_ms"] < 100


def test_blocking_connect_to_blackhole_times_out_without_stuck():
    require_kernel()
    out = run_worker("blocking_blackhole")
    assert "exception" not in out, out
    assert out["first_result"] is not None and out["first_result"]["rc"] == -errno.EPERM, out
    assert out["first_result"]["nonblock_after"] is False
    assert out["first_ms"] < (CS.CONNECT_WAIT_SEC + 0.5) * 1e3, out
    assert out["terminal_after_watchdog"] is False
    assert out["state_after_wait"] == CS.RUNNING
    assert out["reason"] is None and out["on_fault"] == []
    assert out["second_result"] is not None and out["second_result"]["rc"] == -errno.EPERM
    assert out["handler_reasons"] == [CS.CONNECT_TIMEOUT, CS.CONNECT_TIMEOUT]
    assert out["stop_result"] == CS.CLOSED
    assert out["listener_open"] is False


# --- 受入 36 ------------------------------------------------------------------

def _close_calls(tree: ast.AST) -> list[tuple[str, str]]:
    """(関数名, 引数の source) の組。`os.close(...)` の呼び出しを全部挙げる。"""
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "close" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "os"):
                out.append((fn.name, ast.unparse(node.args[0])))
    return out


def test_listener_close_only_by_receiver(env):
    src = Path(CS.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = _close_calls(tree)
    # listener を閉じる close は _close_listener の中の 1 箇所だけ
    listener_closes = [c for c in calls if c[0] == "_close_listener"]
    assert listener_closes == [("_close_listener", "fd")]
    assert all("listener" not in arg for _fn, arg in calls)
    # _close_listener を呼ぶのは receiver の終わり方だけ
    callers = {fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
               for n in ast.walk(fn) if isinstance(n, ast.Attribute)
               and n.attr == "_close_listener" and fn.name != "_close_listener"}
    assert callers == {"_receiver_finish"}
    # 全 close 呼び出しの固定 (増えたら見直す)
    assert sorted(set(fn for fn, _ in calls)) == sorted(
        {"probe_pidfd_thread", "release", "_close_listener", "_handle", "_dup_socket"})

    t, sup = env.start()
    with pytest.raises(RuntimeError):
        sup._close_listener()
    assert sup.state == CS.RUNNING
    assert connect(t, port=env.proxy.port)["rc"] == 0
    assert sup.stop(time.monotonic() + 3) == CS.CLOSED
    assert connect(t, port=env.proxy.port)["rc"] == -errno.ENOSYS


def test_handoff_failure_leaves_fd_with_caller(env, factory):
    """handoff が受け取らなかったとき fd は呼び出し側に残る (二重 close を作らない)。"""
    t = factory.spawn()
    env.tracees.append(t)
    sup = CS.ConnectSupervisor(env.dest(), proxy_handoff=ImmediateAck())
    env.sups.append(sup)
    sup.handoff(t.take_listener())
    t2 = factory.spawn()
    env.tracees.append(t2)
    with pytest.raises(RuntimeError):
        sup.handoff(t2.listener)  # 2 度目は受けない
    assert L.is_seccomp_notify_fd(t2.listener)  # 呼び出し側の所有のまま (close で閉じる)


def test_poll_wakeup_and_enoent(env):
    # 保留なしで stop_efd の poll 復帰
    t, sup = env.start()
    t0 = time.monotonic()
    assert sup.stop(time.monotonic() + 3) == CS.CLOSED
    assert time.monotonic() - t0 < 0.5

    # poll 復帰後に通知の主が去ると RECV は ENOENT。その通知だけが終わり receiver は続く
    gate = Gate("after_poll")
    t2, sup2 = env.start(faults=CS.Faults(at=gate))
    cid = t2.start("connect", port=env.proxy.port, where="grandchild")
    assert gate.wait()
    # 孫を殺す (tracee 本体は生きているので filter の利用者は残る)
    pids = _children(t2.pid)
    assert len(pids) == 1
    os.kill(pids[0], 9)
    time.sleep(0.1)
    gate.release()
    r = t2.result(cid)
    assert r is not None
    deadline = time.monotonic() + 2
    while not any(x.get("result") == "recv_enoent" for x in sup2.records) and \
            time.monotonic() < deadline:
        time.sleep(0.01)
    assert any(x.get("result") == "recv_enoent" for x in sup2.records)
    assert sup2.failure is None and sup2.state == CS.RUNNING
    assert connect(t2, port=env.proxy.port)["rc"] == 0

    # filter の利用者が全部去ると (POLLHUP) receiver は障害なしで CLOSED になる
    t2.kill()
    assert t2.wait_gone()
    assert sup2.wait_terminal(3) and sup2.state == CS.CLOSED and sup2.failure is None


def _children(pid: int) -> list[int]:
    out = []
    for tid in os.listdir(f"/proc/{pid}/task"):
        try:
            text = Path(f"/proc/{pid}/task/{tid}/children").read_text().split()
        except OSError:
            continue
        out += [int(x) for x in text]
    return out


# --- probe (受入 23 の PIDFD_THREAD と Yama、段 0) ------------------------------

@pytest.mark.parametrize("value, ok", [("0", True), ("1", True), ("2", False), ("3", False),
                                       ("x", False)])
def test_ptrace_scope_probe(tmp_path, value, ok):
    p = tmp_path / "ptrace_scope"
    p.write_text(value + "\n")
    assert CS.probe_ptrace_scope(str(p)) is ok
    reasons = CS.probe_supervisor_capabilities(yama_path=str(p))
    assert (CS.PTRACE_SCOPE_UNSUPPORTED in reasons) is (not ok)


def test_ptrace_scope_probe_without_yama(tmp_path):
    assert CS.probe_ptrace_scope(str(tmp_path / "absent")) is True


def test_pidfd_thread_probe_fails_closed():
    assert CS.probe_pidfd_thread(lambda tid, flags: -errno.EINVAL) is False
    reasons = CS.probe_supervisor_capabilities(pidfd_open=lambda tid, flags: -errno.EINVAL)
    assert CS.PIDFD_THREAD_UNAVAILABLE in reasons


def test_pidfd_thread_probe_on_this_kernel():
    major, minor = (int(x) for x in os.uname().release.split(".")[:2])
    if (major, minor) < (6, 9):
        _kernel_unready("PIDFD_THREAD は kernel 6.9 以降")
    assert CS.probe_pidfd_thread() is True


def test_notif_sizes_failure_is_fixed_reason():
    def unavailable():
        raise CS.SupervisorUnavailable(CS.SECCOMP_USER_NOTIF_UNAVAILABLE, "injected")

    with pytest.raises(CS.SupervisorUnavailable) as ei:
        CS.ConnectSupervisor(CS.Destinations(1), faults=CS.Faults(notif_sizes=unavailable))
    assert ei.value.reason == CS.SECCOMP_USER_NOTIF_UNAVAILABLE
    with pytest.raises(CS.SupervisorUnavailable):
        CS.ConnectSupervisor(CS.Destinations(1), faults=CS.Faults(notif_sizes=lambda: (40, 24, 64)))
    assert CS.SECCOMP_USER_NOTIF_UNAVAILABLE in CS.probe_supervisor_capabilities(
        notif_sizes=unavailable)


def test_ioctl_numbers_from_kernel_sizes():
    require_kernel()
    sizes = CS.probe_notif_sizes()
    assert CS.notif_ioctls(*sizes[:2]) == (0xC0502100, 0xC0182101, 0x40082102)


def test_module_has_no_settings_or_env_keys():
    """注入点も定数も、環境変数や settings から入れる経路を持たない。CONTINUE も使わない。"""
    tree = ast.parse(Path(CS.__file__).read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    imports = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
               for a in n.names}
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not {"environ", "getenv", "environb"} & (names | attrs)
    assert not any("config" in (m or "") or "settings" in (m or "") for m in mods | imports)
    assert not any("CONTINUE" in x for x in names | attrs)


def test_profile_constants():
    assert CS.CONNECT_SLOTS == 16
    assert CS.HANDLER_DEADLINE_SEC == 2.0
    assert CS.WATCHDOG_INTERVAL_SEC == 0.1
    assert CS.PROXY_ACK_DEADLINE_SEC == 1.0
    assert CS.HANDLER_SPAWN_MAX == 1 << 16
    assert CS.OPENCODE_UPSTREAM_PORT == 8080


# --- 最終 filter での結合 (受入 3 の listen、代行の結合 1 本) -----------------

@pytest.fixture
def final_filter():
    require_kernel()
    if landlock_nested.probe_abi() < landlock_nested.MIN_ABI:
        _kernel_unready("Landlock ABI < 8")


def _launch_python(sup: CS.ConnectSupervisor, code: str, args: list[str]) -> dict:
    import json

    r = L.launch_cli(
        cli_fd=L.open_cli_binary(PY),
        argv=[PY, "-I", "-S", "-c", textwrap.dedent(code), *args],
        env={"PATH": "/usr/bin:/bin", "LANG": "C"}, backend="claude",
        join=lambda pid: None, handoff=sup.handoff, deadline=time.monotonic() + 15,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert r.started, r
    out, _ = r.launcher_proc.communicate(timeout=30)
    return json.loads(out.decode().strip().splitlines()[-1])


_LISTEN_CODE = """
import json, socket
out = {}
for name, fam in (("v4", socket.AF_INET), ("v6", socket.AF_INET6)):
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        s.listen(1)
        out[name] = 0
    except OSError as e:
        out[name] = e.errno
    s.close()
print(json.dumps(out))
"""

_CONNECT_CODE = """
import json, socket, sys
proxy, other = int(sys.argv[1]), int(sys.argv[2])
out = {}
def tryc(fam, addr):
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        return s.connect_ex(addr)
    finally:
        s.close()
out["proxy"] = tryc(socket.AF_INET, ("127.0.0.1", proxy))
out["other"] = tryc(socket.AF_INET, ("127.0.0.1", other))
out["v6"] = tryc(socket.AF_INET6, ("::1", proxy))
print(json.dumps(out))
"""


def test_listen_without_bind_is_eperm(final_filter):
    before = fd_table()
    sup = CS.ConnectSupervisor(CS.Destinations(None))
    out = _launch_python(sup, _LISTEN_CODE, [])
    assert out == {"v4": errno.EPERM, "v6": errno.EPERM}
    assert sup.stop(time.monotonic() + 3) == CS.CLOSED
    assert sup.join(5)
    sup.release()
    assert not leaked(before, fd_table())


def test_final_filter_connect_is_delegated(final_filter):
    before = fd_table()
    proxy, other = Listener(), Listener()
    ack = ImmediateAck()
    sup = CS.ConnectSupervisor(CS.Destinations(proxy.port), proxy_handoff=ack)
    try:
        out = _launch_python(sup, _CONNECT_CODE, [str(proxy.port), str(other.port)])
        assert out == {"proxy": 0, "other": errno.ECONNREFUSED, "v6": errno.EAFNOSUPPORT}
        assert proxy.drain() == 1 and other.drain() == 0
        assert len(ack.acked) == 1
        assert sup.failure is None
        assert sup.stop(time.monotonic() + 3) == CS.CLOSED
        assert sup.join(5)
        sup.release()
    finally:
        proxy.close()
        other.close()
    assert not leaked(before, fd_table())
