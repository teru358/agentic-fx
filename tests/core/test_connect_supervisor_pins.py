"""connect supervisor の防御を、既存の試験が見ていない向きから固定する。

宛先・errno・資源は実 seccomp user-notif と実 kernel で見る。分岐を直接叩く試験
(`_classify`、`_dup_socket`) は同じ process の中で本物の socket と pidfd を使う。
"""
from __future__ import annotations

import errno
import os
import socket
import struct
import sys
import threading
import time

import pytest

from agentic_fx.core import connect_supervisor as CS
from tests.core._notif_tracee import Gate
from tests.core.test_connect_supervisor import (  # noqa: F401  fixture を共有する
    ImmediateAck, connect, env, factory, fd_table, leaked, require_kernel)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine != "x86_64",
    reason="connect supervisor は x86_64 Linux 専用")

EPERM = -errno.EPERM


def _inet(host: str, port: int) -> bytes:
    return struct.pack("<H", socket.AF_INET) + struct.pack("!H", port) + \
        socket.inet_aton(host) + bytes(8)


@pytest.fixture
def bare():
    """handoff していない supervisor (thread 無し)。終わりに CLOSED にして efd を閉じる。"""
    require_kernel()
    made = []

    def make(dest, **kw):
        s = CS.ConnectSupervisor(dest, **kw)
        made.append(s)
        return s

    before = fd_table()
    yield make
    for s in made:
        s.stop(time.monotonic() + 1)
        s.release()
    assert not leaked(before, fd_table())


# --- 固定 reason は文字列そのもので見る -----------------------------------------

def test_fixed_reason_literals():
    assert CS.SECCOMP_USER_NOTIF_UNAVAILABLE == "seccomp_user_notif_unavailable"
    assert CS.PIDFD_THREAD_UNAVAILABLE == "pidfd_thread_unavailable"
    assert CS.PTRACE_SCOPE_UNSUPPORTED == "ptrace_scope_unsupported"
    assert CS.SUPERVISOR_DIED == "supervisor_died"
    assert CS.SUPERVISOR_STALLED == "supervisor_stalled"
    assert CS.SUPERVISOR_RESOURCE == "supervisor_resource"
    assert CS.CONNECT_TIMEOUT == "connect_timeout"
    assert CS.SupervisorFailure("x", "y").classification == "sandbox_unavailable"


# --- 段 8: 8080 は opencode の構成だけ -------------------------------------------

def test_upstream_port_is_refused_without_direct_destination(bare):
    sup = bare(CS.Destinations(40000, None), proxy_handoff=ImmediateAck())
    assert sup._classify(_inet("127.0.0.1", 8080)) == ("", errno.ECONNREFUSED)
    sup2 = bare(CS.Destinations(40000, 8080), proxy_handoff=ImmediateAck())
    assert sup2._classify(_inet("127.0.0.1", 8080)) == ("direct", 0)
    assert sup2._classify(_inet("127.0.0.1", 40000)) == ("proxy", 0)
    assert sup2._classify(_inet("127.0.0.1", 40001)) == ("", errno.ECONNREFUSED)


# --- 段 9: 複製の種別は 3 項目とも見る -------------------------------------------

def _self_pidfd() -> int:
    fd = CS._pidfd_open(os.getpid(), 0)
    assert fd >= 0, fd
    return fd


@pytest.mark.parametrize("family, proto", [
    (socket.AF_INET6, socket.IPPROTO_TCP),   # SO_DOMAIN だけが違う
    (socket.AF_INET, 262),                   # IPPROTO_MPTCP: SO_PROTOCOL だけが違う
])
def test_dup_socket_rejects_other_domain_or_protocol(bare, family, proto):
    sup = bare(CS.Destinations(40000, None))
    try:
        s = socket.socket(family, socket.SOCK_STREAM, proto)
    except OSError as e:
        pytest.skip(f"socket({family}, STREAM, {proto}) を作れない: {e}")
    pidfd = _self_pidfd()
    try:
        before = fd_table()
        assert sup._dup_socket(pidfd, s.fileno()) == (-1, errno.EPERM)
        assert not leaked(before, fd_table())
        ok = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            dup, err = sup._dup_socket(pidfd, ok.fileno())
            assert err == 0 and dup >= 0
            os.close(dup)
        finally:
            ok.close()
    finally:
        os.close(pidfd)
        s.close()


# --- 段 11b: ack が無いときの複製の shutdown、EINPROGRESS の ack、期限 ----------

class RecordingAck(ImmediateAck):
    def __init__(self, ok: bool = True, sup_box: dict | None = None) -> None:
        super().__init__(ok)
        self.deadlines: list[float] = []
        self.locked_at_add: list[bool] = []
        self.sup_box = sup_box if sup_box is not None else {}

    def add_pending(self, dup_fd):
        sup = self.sup_box.get("sup")
        self.locked_at_add.append(sup is not None and sup._lock.locked())
        return super().add_pending(dup_fd)

    def wait_ack(self, entry, client, deadline):
        self.deadlines.append(deadline - time.monotonic())
        return super().wait_ack(entry, client, deadline)


def test_missing_ack_shuts_down_the_tracee_socket(env):
    ack = ImmediateAck(ok=False)
    t, _sup = env.start(handoff=ack)
    r = connect(t, port=env.proxy.port, keep=True)
    assert r["rc"] == -errno.ECONNREFUSED, r
    # 接続自体は kernel で成立している。proxy 側から見て相手は既に閉じている
    env.proxy.sock.setblocking(True)
    env.proxy.sock.settimeout(2)
    c, _ = env.proxy.sock.accept()
    try:
        c.settimeout(2)
        assert c.recv(16) == b""
    finally:
        c.close()
        env.proxy.sock.setblocking(False)


def test_nonblocking_proxy_connect_waits_for_ack(env):
    ack = ImmediateAck(ok=False)
    t, _sup = env.start(handoff=ack)
    r = connect(t, port=env.proxy.port, nonblock=True)
    assert r["rc"] == -errno.ECONNREFUSED, r
    assert len(ack.acked) == 1


def test_ack_deadline_is_one_second_and_pending_is_added_under_lock(env):
    box: dict = {}
    ack = RecordingAck(sup_box=box)
    t, sup = env.start(handoff=ack)
    box["sup"] = sup
    assert connect(t, port=env.proxy.port)["rc"] == 0
    assert len(ack.deadlines) == 1
    assert 0.8 < ack.deadlines[0] <= CS.PROXY_ACK_DEADLINE_SEC
    assert ack.locked_at_add == [True]


def test_pending_is_not_added_when_commit_is_refused(env):
    gate = Gate("before_commit")
    ack = ImmediateAck()
    t, sup = env.start(handoff=ack, faults=CS.Faults(at=gate))
    cid = t.start("connect", port=env.proxy.port)
    assert gate.wait()
    th = threading.Thread(target=lambda: sup.stop(time.monotonic() + 5))
    th.start()
    end = time.monotonic() + 2
    while sup.state == CS.RUNNING and time.monotonic() < end:
        time.sleep(0.001)
    gate.release()
    th.join(6)
    assert t.result(cid)["rc"] == EPERM
    assert ack.pending == [] and ack.removed == 0


# --- 拒否の経路でも slot を返す -------------------------------------------------

def test_refused_connects_do_not_consume_slots(env):
    t, sup = env.start()
    for _ in range(CS.CONNECT_SLOTS + 2):
        assert connect(t, port=env.other.port)["rc"] == -errno.ECONNREFUSED
    assert connect(t, port=env.proxy.port)["rc"] == 0
    assert sup.failure is None


# --- probe --------------------------------------------------------------------

def test_unreadable_yama_is_unsupported(tmp_path):
    # 読めない (ディレクトリ) は不可
    assert CS.probe_ptrace_scope(str(tmp_path)) is False


def test_pidfd_thread_probe_closes_its_pidfd():
    require_kernel()
    before = fd_table()
    assert CS.probe_pidfd_thread() is True
    assert not leaked(before, fd_table())


# --- 段 2: arch / nr の不一致 (filter が通さない値を通知として直接渡す) -----------

@pytest.mark.parametrize("arch, nr", [(0x40000003, 42),        # i386 の arch
                                      (0xC000003E, 41)])       # socket の nr
def test_filter_mismatch_is_eperm_and_fault(bare, arch, nr):
    seen: list = []
    sup = bare(CS.Destinations(40000, None), on_fault=seen.append)
    sent: list = []
    sup._send = lambda nid, rc: sent.append((nid, rc)) or "sent"
    sup._id_valid = lambda nid: pytest.fail("段 2 の後へ進んだ")
    rec: dict = {}
    notif = CS._Notif(7, os.getpid(), nr, arch, (3, 0, 16, 0, 0, 0))
    with pytest.raises(CS._Fault):
        sup._handle(notif, rec)
    assert sent == [(7, EPERM)]
    assert rec["stage"] == 2 and rec["result"] == EPERM


# --- 段 1: RECV の EINTR は再試行 -----------------------------------------------

def test_recv_eintr_is_retried(env):
    calls = {"n": 0}

    def recv_errno():
        calls["n"] += 1
        return errno.EINTR if calls["n"] == 1 else 0

    t, sup = env.start(faults=CS.Faults(recv_errno=recv_errno))
    assert connect(t, port=env.proxy.port)["rc"] == 0
    assert calls["n"] >= 2
    assert sup.failure is None and sup.state == CS.RUNNING


# --- 段 6: 写しの失敗の後に通知が無効なら無応答 -----------------------------------

def test_copy_failure_after_tracee_left_is_silent(env):
    seen: dict[int, int] = {}
    t, sup = env.start(faults=CS.Faults(vm_read_len=lambda n: n - 1))
    real = sup._id_valid

    def id_valid(nid):
        seen[nid] = seen.get(nid, 0) + 1
        # 段 3・5 は本物、段 6 の失敗の後の照会で「去った」ことにする
        return real(nid) if seen[nid] <= 2 else False

    sup._id_valid = id_valid
    cid = t.start("connect", port=env.proxy.port)
    end = time.monotonic() + 3
    while not any(r.get("result") == "silent" for r in sup.records) and time.monotonic() < end:
        time.sleep(0.01)
    rec = [r for r in sup.records if r.get("by") == "handler"]
    assert len(rec) == 1 and rec[0]["result"] == "silent" and rec[0]["stage"] == 6, rec
    assert "send" not in rec[0]
    assert t.result(cid, 0.2) is None
    assert env.proxy.drain() == 0


# --- handler の例外: on_fault が例外を投げても EPERM で応答する --------------------

def test_on_fault_exception_does_not_skip_the_reply(env):
    def boom(_failure):
        raise RuntimeError("on_fault failed")

    def at(point, nid):
        if point == "handler":
            raise RuntimeError("injected at handler")

    t, sup = env.start(faults=CS.Faults(at=at), on_fault=boom)
    assert connect(t, port=env.proxy.port)["rc"] == EPERM
    assert sup.wait_terminal(3) and sup.state == CS.CLOSED
    assert sup.failure.reason == "supervisor_died"


# --- watchdog の起動失敗は handoff の例外と supervisor_died -------------------------

def test_watchdog_start_failure_raises_from_handoff(env, factory, monkeypatch):
    class NoWatchdog(threading.Thread):
        def start(self):
            if self.name == "afx-connect-watchdog":
                raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr(CS.threading, "Thread", NoWatchdog)
    seen: list = []
    t = factory.spawn()
    env.tracees.append(t)
    sup = CS.ConnectSupervisor(env.dest(), proxy_handoff=ImmediateAck(), on_fault=seen.append)
    env.sups.append(sup)
    fd = t.take_listener()
    with pytest.raises(RuntimeError):
        sup.handoff(fd)
    # 呼び出し側は例外で知り、listener は受け取られていない (receiver も起動していない)
    assert sup.wait_terminal(0) and sup.state == CS.CLOSED
    assert sup.failure is not None and sup.failure.reason == "supervisor_died"
    assert [f.reason for f in seen] == ["supervisor_died"]
    assert sup._receiver is None and sup._listener == -1
    from agentic_fx.core import cli_launch as L
    assert L.is_seccomp_notify_fd(fd)
    # 呼び出し側が閉じる (launch_cli の handoff 失敗と同じ扱い)
    t.listener = fd


# --- handoff は stop の後は受けない ---------------------------------------------

def test_handoff_after_stop_is_refused(bare):
    sup = bare(CS.Destinations(40000, None))
    assert sup.stop(time.monotonic() + 1) == CS.CLOSED
    fd = os.eventfd(0, os.EFD_CLOEXEC)
    try:
        with pytest.raises(RuntimeError):
            sup.handoff(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    assert sup._receiver is None


# --- release: STUCK と thread 生存中は stop_efd を閉じない ------------------------

def test_release_keeps_efd_while_stuck_or_threads_alive(bare):
    sup = bare(CS.Destinations(40000, None))
    efd = sup._efd
    with sup._lock:
        sup._state = CS.STUCK
    sup.release()
    assert sup._efd == efd and os.fstat(efd)
    with sup._lock:
        sup._state = CS.CLOSED
    hold = threading.Event()
    th = threading.Thread(target=hold.wait, args=(5,))
    th.start()
    try:
        with sup._lock:
            sup._handlers.add(th)
        sup.release()
        assert sup._efd == efd
    finally:
        hold.set()
        th.join(5)
        with sup._lock:
            sup._handlers.discard(th)
    sup.release()
    assert sup._efd == -1


# --- stop の期限切れと receiver の確定の交差 --------------------------------------

def _slow_close(sup, sec: float) -> None:
    real = sup._close_listener

    def slow():
        time.sleep(sec)
        real()

    sup._close_listener = slow


def test_slow_close_does_not_block_stop_past_deadline(env):
    """receiver の close が遅れても、stop は deadline + 猶予で STUCK を返し、listener は閉じない。"""
    t, sup = env.start()
    _slow_close(sup, 0.3)
    t0 = time.monotonic()
    r = sup.stop(t0 + 0.02)
    assert time.monotonic() - t0 < 0.02 + 0.1 + 0.05
    assert r == CS.STUCK and sup.state == CS.STUCK
    assert sup.wait_terminal(0)
    assert sup.join(2)
    # 遅れて来た receiver は STUCK を CLOSED に戻さず、listener も閉じない
    assert sup.state == CS.STUCK
    assert L_is_notify(sup._listener)
    os.close(sup._listener)
    sup._listener = -1
    os.close(sup._efd)
    sup._efd = -1
    env.sups.remove(sup)


def test_close_and_closed_are_one_step_when_close_is_prompt(env):
    t, sup = env.start()
    assert sup.stop(time.monotonic() + 1) == CS.CLOSED
    assert sup.state == CS.CLOSED and sup._listener == -1
    assert connect(t, port=env.proxy.port)["rc"] == -errno.ENOSYS


def test_blocked_handler_stop_returns_within_deadline_and_acks(env):
    """handler が止まったままでも stop は deadline + 0.2 秒以内に STUCK を返し、ack を立てる。"""
    gate = Gate("after_commit")
    t, sup = env.start(faults=CS.Faults(at=gate))
    cid = t.start("connect", port=env.proxy.port)
    assert gate.wait()
    t0 = time.monotonic()
    assert sup.stop(t0 + 0.1) == CS.STUCK
    assert time.monotonic() - t0 < 0.1 + 0.2
    assert sup.state == CS.STUCK
    assert sup.wait_terminal(0)
    gate.release()
    t.result(cid)
    assert sup.join(5)
    assert L_is_notify(sup._listener)
    os.close(sup._listener)
    sup._listener = -1
    os.close(sup._efd)
    sup._efd = -1
    env.sups.remove(sup)


def test_handler_spawn_cap_refuses_and_records_failure(env, monkeypatch):
    monkeypatch.setattr(CS, "HANDLER_SPAWN_MAX", 3)
    t, sup = env.start()
    for _ in range(3):
        assert connect(t, port=env.proxy.port)["rc"] == 0
    assert sup.failure is None
    assert connect(t, port=env.proxy.port)["rc"] == EPERM
    assert sup.failure is not None and sup.failure.reason == CS.SUPERVISOR_STALLED
    assert sup.wait_terminal(5)
    assert sup.state == CS.CLOSED


def test_receiver_closing_within_grace_is_reported_closed(env):
    """stop の期限が切れた後、猶予の間に receiver が CLOSED にしたら CLOSED を返す。"""
    reached = threading.Event()
    go = threading.Event()

    def at(point, nid):
        if point == "after_poll" and not reached.is_set():
            reached.set()
            go.wait(5)

    t, sup = env.start(faults=CS.Faults(at=at))
    cid = t.start("connect", port=env.proxy.port)
    assert reached.wait(5)
    timer = threading.Timer(0.07, go.set)
    timer.start()
    try:
        r = sup.stop(time.monotonic() + 0.02)
    finally:
        timer.join()
    assert r == CS.CLOSED and sup.state == CS.CLOSED
    assert t.result(cid)["rc"] == -errno.ENOSYS


def test_stuck_stop_returns_at_deadline_and_receiver_exits(env):
    """応答待ちが残ったまま期限が来たら stop は期限で STUCK を返し、後から来た短い期限で
    receiver も待ちを終える。"""
    gate = Gate("after_commit")
    t, sup = env.start(faults=CS.Faults(at=gate))
    cid = t.start("connect", port=env.proxy.port)
    assert gate.wait()
    long_stop = threading.Thread(target=lambda: sup.stop(time.monotonic() + 5))
    long_stop.start()
    end = time.monotonic() + 2
    while sup.state == CS.RUNNING and time.monotonic() < end:
        time.sleep(0.001)
    t0 = time.monotonic()
    assert sup.stop(t0 + 0.2) == CS.STUCK
    assert time.monotonic() - t0 < 0.28
    sup._receiver.join(0.5)
    assert not sup._receiver.is_alive()
    gate.release()
    long_stop.join(6)
    assert t.result(cid)["rc"] == 0
    assert sup.join(5)
    # STUCK は listener と stop_efd を閉じない。試験の後始末としてここで閉じる
    assert L_is_notify(sup._listener)
    os.close(sup._listener)
    sup._listener = -1
    os.close(sup._efd)
    sup._efd = -1
    env.sups.remove(sup)


def L_is_notify(fd: int) -> bool:
    from agentic_fx.core import cli_launch as L
    return fd >= 0 and L.is_seccomp_notify_fd(fd)


# --- probe の段 0 と thread 待ち ------------------------------------------------

class _FakeLibc:
    def __init__(self, rc: int, sizes: tuple[int, int, int]) -> None:
        self.rc = rc
        self.sizes = sizes

    def syscall(self, _nr, _op, _flags, buf):
        for i, v in enumerate(self.sizes):
            buf[i] = v
        return self.rc


@pytest.mark.parametrize("rc, sizes", [(-1, (80, 24, 64)), (0, (40, 24, 64)),
                                       (0, (80, 16, 64))])
def test_notif_sizes_probe_fails_closed(monkeypatch, rc, sizes):
    monkeypatch.setattr(CS, "_libc", lambda: _FakeLibc(rc, sizes))
    with pytest.raises(CS.SupervisorUnavailable) as ei:
        CS.probe_notif_sizes()
    assert ei.value.reason == "seccomp_user_notif_unavailable"



# --- 複製を socket で包む処理: 包みの構築に失敗しても複製を閉じる -------------------

class _Shim:
    """module の一部の関数だけを差し替え、残りは本物へ委ねる。"""

    def __init__(self, real, **over) -> None:
        self._real = real
        self.__dict__.update(over)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _raise_oserror(code: int):
    def f(*_a, **_k):
        raise OSError(code, os.strerror(code))
    return f


@pytest.fixture
def default_timeout():
    socket.setdefaulttimeout(1.0)
    try:
        yield
    finally:
        socket.setdefaulttimeout(None)


def test_getsockname_and_shutdown_keep_blocking_under_default_timeout(default_timeout):
    # 既定 timeout があっても、複製 fd の flag を変えず blocking のまま扱う
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    cli = socket.create_connection(srv.getsockname())
    fd = cli.fileno()
    os.set_blocking(fd, True)
    try:
        assert CS._getsockname4(fd) == cli.getsockname()
        assert os.get_blocking(fd) is True
        CS._shutdown(fd)
        assert os.get_blocking(fd) is True
        conn, _ = srv.accept()
        conn.settimeout(2)
        assert conn.recv(16) == b""
        conn.close()
    finally:
        cli.close()
        srv.close()


def test_getsockname_and_shutdown_do_not_close_the_fd_on_failure():
    # 渡された fd の所有権は呼び出し側にある。失敗しても閉じない
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError):
            CS._getsockname4(s.fileno())
        os.fstat(s.fileno())
    finally:
        s.close()
    r, w = os.pipe()
    try:
        with pytest.raises(OSError):
            CS._shutdown(r)
        os.fstat(r)
    finally:
        os.close(r)
        os.close(w)


def test_proxy_connect_through_handler_under_default_timeout(env, default_timeout):
    ack = ImmediateAck()
    t, sup = env.start(handoff=ack)
    r = connect(t, port=env.proxy.port)
    assert r["rc"] == 0, r
    assert sup.failure is None


def test_supervisor_does_not_wrap_duplicate_fds_in_socket_objects():
    import inspect
    src = inspect.getsource(CS)
    assert "socket.socket(" not in src
    assert "_borrowed_socket" not in src


# --- handler の後始末: pending の削除が失敗しても複製と pidfd は閉じる -------------

class FailingRemove(ImmediateAck):
    def remove_pending(self, entry) -> None:
        raise RuntimeError("remove_pending failed")


def test_remove_pending_failure_still_closes_duplicate_and_pidfd(env):
    t, sup = env.start(handoff=FailingRemove())
    before = fd_table()
    r = connect(t, port=env.proxy.port)
    assert r["rc"] == 0, r
    end = time.monotonic() + 3
    while sup.in_flight and time.monotonic() < end:
        time.sleep(0.01)
    assert sup.in_flight == 0
    # pending の削除の失敗は supervisor の障害として記録する
    assert sup.failure is not None and sup.failure.reason == CS.SUPERVISOR_DIED
    assert not leaked(before, fd_table())


# --- ack 待ちの前の getsockname が資源不足で失敗: その通知だけ EPERM ----------------

def test_getsockname_failure_is_eperm_for_that_notification(env, monkeypatch):
    ack = ImmediateAck()
    t, sup = env.start(handoff=ack)
    before = fd_table()
    # getsockname は追加の fd を使わないので、資源不足の失敗はその関数の失敗として作る
    monkeypatch.setattr(CS, "_getsockname4", _raise_oserror(errno.ENOBUFS))
    try:
        r = connect(t, port=env.proxy.port)
    finally:
        monkeypatch.undo()
    assert r["rc"] == EPERM, r
    rec = [x for x in sup.records if x.get("by") == "handler"]
    assert rec[-1]["reason"] == CS.SUPERVISOR_RESOURCE and rec[-1]["stage"] == 12, rec
    assert ack.acked == [] and ack.removed == 1
    end = time.monotonic() + 3
    while sup.in_flight and time.monotonic() < end:
        time.sleep(0.01)
    assert not leaked(before, fd_table())
    # supervisor は続けて代行する
    assert sup.failure is None and sup.state == CS.RUNNING
    assert connect(t, port=env.proxy.port)["rc"] == 0


# --- proxy 宛と判定されたのに差し込み口が無い: assert でなく EPERM ------------------

def test_proxy_kind_without_handoff_is_eperm_not_a_crash(env, factory):
    t = factory.spawn()
    env.tracees.append(t)
    sup = CS.ConnectSupervisor(env.dest())
    env.sups.append(sup)
    # 段 8 の判定と差し込み口の有無が食い違った場合を作る
    sup._classify = lambda copy: ("proxy", 0)
    sup.handoff(t.take_listener())
    assert connect(t, port=env.proxy.port)["rc"] == EPERM
    assert env.proxy.drain() == 0
    assert sup.failure is None and sup.state == CS.RUNNING
    rec = [x for x in sup.records if x.get("by") == "handler"]
    assert rec[-1]["stage"] == 10 and "committed" not in rec[-1], rec


# --- 生涯の生成数は実際に起動した handler だけを数える --------------------------------

def test_spawn_cap_counts_only_started_handlers(env, monkeypatch):
    monkeypatch.setattr(CS, "HANDLER_SPAWN_MAX", CS.CONNECT_SLOTS + 1)
    gate = Gate("handler", count=CS.CONNECT_SLOTS, hold=5)
    t, sup = env.start(faults=CS.Faults(at=gate))
    cids = [t.start("connect", port=env.proxy.port) for _ in range(CS.CONNECT_SLOTS + 1)]
    assert gate.wait(timeout=5)
    time.sleep(0.2)
    gate.release()
    rcs = sorted(t.result(c)["rc"] for c in cids)
    assert rcs.count(-errno.EAGAIN) == 1 and rcs.count(0) == CS.CONNECT_SLOTS
    # slot で拒否した通知は数えていないので、上限までもう 1 件起動できる
    assert connect(t, port=env.proxy.port)["rc"] == 0
    assert sup.failure is None
    assert connect(t, port=env.proxy.port)["rc"] == EPERM
    assert sup.failure is not None and sup.failure.reason == CS.SUPERVISOR_STALLED


# --- listener は O_NONBLOCK、RECV の EAGAIN は poll へ戻る ---------------------------

def test_listener_is_nonblocking_after_handoff(env):
    t, sup = env.start()
    assert os.get_blocking(sup._listener) is False
    assert connect(t, port=env.proxy.port)["rc"] == 0


def test_recv_eagain_returns_to_poll(env):
    calls = {"n": 0}

    def recv_errno():
        calls["n"] += 1
        return errno.EAGAIN if calls["n"] == 1 else 0

    t, sup = env.start(faults=CS.Faults(recv_errno=recv_errno))
    assert connect(t, port=env.proxy.port)["rc"] == 0
    assert any(x.get("result") == "recv_eagain" for x in sup.records)
    assert sup.failure is None and sup.state == CS.RUNNING


def test_notification_withdrawn_after_pollin_does_not_block_receiver(env):
    """POLLIN を見た後、RECV の前に tracee を SIGKILL して通知を取り消す。receiver は
    RECV で止まらずに次の poll へ戻り、利用者が居なくなった listener を閉じる。"""
    gate = Gate("after_poll")
    t, sup = env.start(faults=CS.Faults(at=gate))
    t.start("connect", port=env.proxy.port)
    assert gate.wait()
    t.kill()
    assert t.wait_gone()
    gate.release()
    assert sup.wait_terminal(3) and sup.state == CS.CLOSED
    assert sup.failure is None
    assert any(x.get("result") in ("recv_enoent", "recv_eagain") for x in sup.records)
    assert env.proxy.drain() == 0


# --- 複製への connect は handler の期限で打ち切る -----------------------------------

def test_connect_wait_fits_in_handler_deadline():
    assert CS.CONNECT_TIMEOUT == "connect_timeout"
    assert CS.CONNECT_WAIT_SEC == 0.75
    # connect の待ちと proxy の ack の待ちを足しても watchdog の期限に届かない
    assert CS.CONNECT_WAIT_SEC + CS.PROXY_ACK_DEADLINE_SEC < CS.HANDLER_DEADLINE_SEC


def test_blocking_connect_to_unreachable_listener_times_out(env, factory):
    """accept しない listener の backlog を埋め、SYN を捨てさせる。blocking の connect
    でも handler は期限内に EPERM を返し、tracee の socket は blocking のまま戻る。"""
    from tests.core._notif_tracee import blackhole
    bl, fillers = blackhole()
    try:
        port = bl.getsockname()[1]
        t = factory.spawn()
        env.tracees.append(t)
        sup = CS.ConnectSupervisor(CS.Destinations(None, port))
        env.sups.append(sup)
        sup.handoff(t.take_listener())
        r = connect(t, port=port, timeout=CS.HANDLER_DEADLINE_SEC + 1)
        assert r["rc"] == EPERM, r
        assert r["ms"] < (CS.CONNECT_WAIT_SEC + 0.5) * 1000, r
        assert r["nonblock_after"] is False
        rec = [x for x in sup.records if x.get("by") == "handler"]
        assert rec[-1]["reason"] == CS.CONNECT_TIMEOUT and rec[-1]["stage"] == 11, rec
        assert sup.failure is None and sup.state == CS.RUNNING
        # 同じ supervisor は続けて代行する
        ok = socket.socket()
        ok.bind(("127.0.0.1", 0))
        ok.listen(4)
        try:
            sup2_port = ok.getsockname()[1]
            sup._dest = CS.Destinations(None, sup2_port)
            assert connect(t, port=sup2_port)["rc"] == 0
        finally:
            ok.close()
    finally:
        for f in fillers:
            f.close()
        bl.close()


# --- fd が枯渇していても、拒否した proxy 接続は shutdown で切る ---------------------

def test_refused_proxy_connection_is_cut_even_when_dup_fails(env, monkeypatch):
    ack = ImmediateAck(ok=False)
    t, sup = env.start(handoff=ack)
    monkeypatch.setattr(CS, "os", _Shim(os, dup=_raise_oserror(errno.EMFILE)))
    try:
        r = connect(t, port=env.proxy.port, keep=True)
    finally:
        monkeypatch.undo()
    # 追加の fd が要らないので getsockname も成功し、ack の不在で拒否する
    assert r["rc"] in (EPERM, -errno.ECONNREFUSED), r
    env.proxy.sock.setblocking(True)
    env.proxy.sock.settimeout(2)
    c, _ = env.proxy.sock.accept()
    try:
        c.settimeout(2)
        assert c.recv(16) == b""
    finally:
        c.close()
        env.proxy.sock.setblocking(False)
    assert r["rc"] == -errno.ECONNREFUSED, r
    assert sup.failure is None and sup.state == CS.RUNNING


# --- blocking な複製の完了は再 connect の結果で決める ---------------------------------

@pytest.mark.parametrize("seq,want", [
    # 継続 (EINPROGRESS / EALREADY) の後に成功 0
    ([errno.EINPROGRESS, errno.EALREADY, 0], 0),
    # 確定済みの再 connect は EISCONN。元の blocking connect の成功として 0 を返す
    ([errno.EINPROGRESS, errno.EISCONN], 0),
    # 失敗の確定はその errno のまま返す
    ([errno.EINPROGRESS, errno.ECONNREFUSED], -errno.ECONNREFUSED),
])
def test_blocking_reconnect_outcomes(monkeypatch, seq, want):
    # poll が直ちに POLLOUT を返す本物の接続済み socket を使い、connect の結果だけを並べる
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    cli = socket.create_connection(srv.getsockname())
    try:
        calls = []

        def fake(fd, sa):
            calls.append(fd)
            v = seq[len(calls) - 1]
            return -v if v else 0

        monkeypatch.setattr(CS, "_raw_connect", fake)
        rc, timed_out = CS._bounded_connect(cli.fileno(), b"", time.monotonic() + 1)
        assert (rc, timed_out) == (want, False)
        assert len(calls) == len(seq)
        assert os.get_blocking(cli.fileno()) is True
    finally:
        cli.close()
        srv.close()


def test_first_connect_eisconn_is_passed_through(monkeypatch):
    # 最初から接続済みの socket への connect は EISCONN のまま返す
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    cli = socket.create_connection(srv.getsockname())
    try:
        sa = _inet(*srv.getsockname())
        assert CS._bounded_connect(cli.fileno(), sa, time.monotonic() + 1) == \
            (-errno.EISCONN, False)
    finally:
        cli.close()
        srv.close()


# --- ack の待ちは handler の期限 (起動から HANDLER_DEADLINE_SEC) を越えない ----------------

def _sleep_at(where: str, sec: float):
    def at(point: str, nid: int) -> None:
        if point == where:
            time.sleep(sec)
    return at


def test_ack_wait_is_capped_by_handler_deadline(env):
    ack = RecordingAck()
    delay = 1.4
    t, sup = env.start(handoff=ack, faults=CS.Faults(at=_sleep_at("before_ack", delay)))
    assert connect(t, port=env.proxy.port, timeout=5)["rc"] == 0
    assert len(ack.deadlines) == 1
    # 1 秒の固定でなく、handler の期限から応答の余裕を引いた残りまでしか待たない
    assert CS.RESPONSE_MARGIN_SEC == 0.25
    assert CS.CONNECT_WAIT_SEC == \
        CS.HANDLER_DEADLINE_SEC - CS.PROXY_ACK_DEADLINE_SEC - CS.RESPONSE_MARGIN_SEC
    assert ack.deadlines[0] <= CS.HANDLER_DEADLINE_SEC - CS.RESPONSE_MARGIN_SEC - delay + 0.02, \
        ack.deadlines
    assert sup.failure is None


def test_ack_deadline_already_past_refuses_without_waiting(env, monkeypatch):
    # 期限を縮めて、connect の後で handler の期限を使い切った状態を作る
    monkeypatch.setattr(CS, "HANDLER_DEADLINE_SEC", 0.9)
    monkeypatch.setattr(CS, "CONNECT_WAIT_SEC", 0.5)
    ack = RecordingAck()
    t, sup = env.start(handoff=ack, faults=CS.Faults(at=_sleep_at("before_ack", 1.0)))
    r = connect(t, port=env.proxy.port, keep=True, timeout=5)
    assert r["rc"] == EPERM, r
    assert ack.deadlines == []
    rec = [x for x in sup.records if x.get("by") == "handler"]
    assert rec[-1]["reason"] == CS.CONNECT_TIMEOUT and rec[-1]["stage"] == 12, rec
    # 拒否した接続は切る
    env.proxy.sock.setblocking(True)
    env.proxy.sock.settimeout(2)
    c, _ = env.proxy.sock.accept()
    try:
        c.settimeout(2)
        assert c.recv(16) == b""
    finally:
        c.close()
        env.proxy.sock.setblocking(False)


@pytest.mark.parametrize("nonblock", [False, True])
def test_connect_is_not_started_after_connect_deadline(env, nonblock):
    # handler の起動を connect の期限 (CONNECT_WAIT_SEC) より遅らせる
    t, sup = env.start(faults=CS.Faults(at=_sleep_at("handler", CS.CONNECT_WAIT_SEC + 0.15)))
    r = connect(t, port=env.proxy.port, nonblock=nonblock, timeout=5)
    assert r["rc"] == EPERM, r
    assert env.proxy.drain() == 0
    rec = [x for x in sup.records if x.get("by") == "handler"]
    assert rec[-1]["reason"] == CS.CONNECT_TIMEOUT and rec[-1]["stage"] == 11, rec
    assert sup.failure is None and sup.state == CS.RUNNING


# --- handler thread の構築そのものが失敗しても slot と通知を回収する -----------------

def test_handler_thread_construction_failure_returns_slot_and_replies(env, monkeypatch):
    t, sup = env.start()
    real = CS.threading.Thread

    def thread(*a, **k):
        if str(k.get("name", "")).startswith("afx-connect-handler"):
            raise MemoryError("injected")
        return real(*a, **k)

    monkeypatch.setattr(CS, "threading", _Shim(CS.threading, Thread=thread))
    cid = t.start("connect", port=env.proxy.port)
    r = t.result(cid, 3)
    monkeypatch.undo()
    assert r is not None and r["rc"] == EPERM, r
    assert sup.wait_terminal(3) and sup.state == CS.CLOSED
    assert sup.failure is not None and sup.failure.reason == CS.SUPERVISOR_DIED
    assert sup.in_flight == 0
    assert sup._slots._value == CS.CONNECT_SLOTS
    # 起動しなかった handler は生涯の回数に数えない
    assert sup._spawned == 0
    assert any(x.get("reason") == "handler_start" for x in sup.records)


# --- blocking への復元に失敗したら EPERM と障害の記録 ------------------------------

def test_blocking_restore_failure_is_eperm_and_fault(env, monkeypatch):
    t, sup = env.start()
    real = os.set_blocking

    def set_blocking(fd, flag):
        if flag:
            raise OSError(errno.EBADF, "injected")
        return real(fd, flag)

    monkeypatch.setattr(CS, "os", _Shim(os, set_blocking=set_blocking))
    try:
        r = connect(t, port=env.proxy.port, timeout=3)
    finally:
        monkeypatch.undo()
    assert r["rc"] == EPERM, r
    rec = [x for x in sup.records if x.get("by") == "handler"]
    assert rec[-1]["reason"] == CS.SUPERVISOR_RESOURCE and rec[-1]["stage"] == 11, rec
    assert sup.failure is not None and sup.failure.reason == CS.SUPERVISOR_DIED
    assert "blocking restore" in sup.failure.detail
