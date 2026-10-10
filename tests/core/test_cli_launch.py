"""launcher と worker 側の status protocol を、実子 process と実 kernel で確かめる。

- 本物の launcher (`python -I -m agentic_fx.core.cli_launcher`) は pytest process の
  子として起こし、seccomp と nested Landlock はその子だけに掛かる。
- worker 側の判定の異常系は、protocol を話す偽 launcher (`_cli_launch_peer.py`) を
  相手にする。listener が要る場面では偽 launcher も本物の seccomp notify fd を作る。
- exec される CLI 役は、実 python に小さな script を渡したもの。`/usr/bin/sleep` は
  uutils の multicall binary で、fd 経由の exec では呼ばれた名前を判定できずすぐ
  終わるので使わない。
"""
from __future__ import annotations

import ctypes
import dataclasses
import errno
import fcntl
import gc
import json
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import textwrap
import threading
import time
import warnings
from pathlib import Path

import pytest

from agentic_fx.core import cli_launch as L
from agentic_fx.core import cli_launch_protocol as P
from agentic_fx.core import cli_launcher
from agentic_fx.core import landlock_nested
from agentic_fx.core import seccomp_cli_policy as SP

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine != "x86_64",
    reason="improve-cli の launcher は x86_64 Linux 専用")

PEER = str(Path(__file__).with_name("_cli_launch_peer.py"))
PY = os.path.realpath(sys.executable)
# CLI 役の既定: 30 秒眠るだけの python
PY_SLEEP_ARGV = [PY, "-I", "-S", "-c", "import time; time.sleep(30)"]
SRC = str(Path(L.__file__).resolve().parents[2])


def _env() -> dict:
    return {"PATH": "/usr/bin:/bin", "LANG": "C"}


def _kernel_unready(why: str) -> None:
    """kernel 能力の不足は既定で skip。`AFX_REQUIRE_SANDBOX_PROBES=1` なら fail にして、
    probe の失敗で module 全体が黙って skip になるのを判定の場で見逃さない。"""
    if os.environ.get("AFX_REQUIRE_SANDBOX_PROBES") == "1":
        pytest.fail(why)
    pytest.skip(why)


@pytest.fixture(scope="module", autouse=True)
def _kernel_ready():
    if landlock_nested.probe_abi() < landlock_nested.MIN_ABI:
        _kernel_unready("Landlock ABI < 8")
    missing = cli_launcher.probe_seccomp_capabilities()
    if missing:
        _kernel_unready(f"seccomp の能力が足りない: {missing}")


class _Harness:
    def __init__(self) -> None:
        self.procs: list[subprocess.Popen] = []
        self.listeners: list[int] = []
        self.joined: list[int] = []

    def handoff(self, fd: int) -> None:
        self.listeners.append(fd)

    def join(self, pid: int) -> None:
        self.joined.append(pid)

    def launch(self, *, peer: str | None = None, cli: str = PY,
               argv: list[str] | None = None, timeout: float = 10.0,
               backend: str = "claude", env: dict | None = None, **kw) -> L.LaunchResult:
        kw.setdefault("join", self.join)
        kw.setdefault("handoff", self.handoff)
        if peer is not None:
            kw.setdefault("launcher_cmd", [sys.executable, PEER, peer])
        kw.setdefault("stdout", subprocess.DEVNULL)
        kw.setdefault("stderr", subprocess.DEVNULL)
        r = L.launch_cli(
            cli_fd=L.open_cli_binary(cli),
            argv=argv or (list(PY_SLEEP_ARGV) if cli == PY else [cli]),
            env=_env() if env is None else env,
            backend=backend, deadline=time.monotonic() + timeout, **kw)
        if r.launcher_proc is not None:
            self.procs.append(r.launcher_proc)
        return r

    def close(self) -> None:
        for p in self.procs:
            if p.returncode is None:
                p.kill()
                p.wait(10)
            for s in (p.stdout, p.stderr):
                if s is not None:
                    s.close()
        for fd in self.listeners:
            os.close(fd)


@pytest.fixture
def h():
    harness = _Harness()
    yield harness
    harness.close()


def _fds() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


def _notify_fds(pid: int | str = "self") -> list[int]:
    out = []
    for name in os.listdir(f"/proc/{pid}/fd"):
        try:
            if os.readlink(f"/proc/{pid}/fd/{name}") == L.SECCOMP_NOTIFY_LINK:
                out.append(int(name))
        except OSError:
            pass
    return out


def _status(pid: int) -> dict:
    out = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        k, _, v = line.partition(":")
        out[k] = v.strip()
    return out


def _python_cli(h: _Harness, code: str, **kw) -> tuple[L.LaunchResult, dict]:
    """実 python を CLI 役として launcher 経由で exec し、stdout の JSON 1 行を読む。"""
    r = h.launch(cli=PY, argv=[PY, "-I", "-S", "-c", textwrap.dedent(code)],
                 stdout=subprocess.PIPE, **kw)
    assert r.started, r
    out, _ = r.launcher_proc.communicate(timeout=30)
    return r, json.loads(out.decode().strip().splitlines()[-1])


# --- 受入 22 -------------------------------------------------------------------------

def test_real_launcher_reaches_cli_started(h):
    r = h.launch()
    assert r.started and r.reason is None and r.series is None, r
    assert h.joined == [r.launcher_proc.pid]
    pid = r.launcher_proc.pid
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        fds = sorted(int(x) for x in os.listdir(f"/proc/{pid}/fd"))
        if fds == [0, 1, 2]:
            break
        time.sleep(0.05)
    assert fds == [0, 1, 2]
    st = _status(pid)
    assert st["Seccomp"] == "2" and st["NoNewPrivs"] == "1"
    exe = os.stat(f"/proc/{pid}/exe")
    want = os.stat(PY)
    assert (exe.st_dev, exe.st_ino) == (want.st_dev, want.st_ino)
    assert len(h.listeners) == 1 and L.is_seccomp_notify_fd(h.listeners[0])


_MALFORMED = [
    ("short", P.ATTESTATION_MALFORMED),
    ("long", P.ATTESTATION_MALFORMED),
    ("magic", P.ATTESTATION_MALFORMED),
    ("version", P.ATTESTATION_MALFORMED),
    ("nonce", P.ATTESTATION_MALFORMED),
    ("unknown_type", P.ATTESTATION_MALFORMED),
    ("zero_length", P.ATTESTATION_MALFORMED),
    ("hello_pid", P.ATTESTATION_MISMATCH),
    ("joined_first", P.ATTESTATION_ORDER),
    ("prepared_first", P.ATTESTATION_MALFORMED),
    ("hello_twice", P.ATTESTATION_ORDER),
    ("go_before_prepared", P.ATTESTATION_ORDER),
]


@pytest.mark.parametrize("scenario, reason", _MALFORMED)
def test_malformed_frames_are_rejected(h, scenario, reason):
    before = _fds()
    r = h.launch(peer=scenario)
    assert not r.started
    assert (r.reason, r.series) == (reason, L.BEFORE_HANDOFF), r
    assert r.classification == L.SANDBOX_UNAVAILABLE
    assert h.listeners == []
    assert _fds() == before


@pytest.mark.parametrize("scenario", [
    "prepared_no_fd", "prepared_two_fds", "prepared_pipe", "hello_with_fd"])
def test_prepared_ancillary_must_be_exactly_one_listener(h, scenario):
    before = _fds()
    r = h.launch(peer=scenario)
    assert (r.reason, r.series) == (P.ATTESTATION_MALFORMED, L.BEFORE_HANDOFF), r
    assert h.listeners == []
    assert _fds() == before


def test_scm_credentials_never_reach_the_worker(h, monkeypatch):
    """worker の端は SO_PASSCRED を立てないので、偽 launcher が付けた SCM_CREDENTIALS は
    kernel が落とす (受信側に届いたら decoder が malformed にすることは protocol の
    test が見る)。frame 本体と listener は届き、正常に進む。"""
    seen = {}
    real_socketpair = socket.socketpair

    def spy(*a, **kw):
        p, c = real_socketpair(*a, **kw)
        seen["passcred"] = p.getsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED)
        return p, c

    monkeypatch.setattr(socket, "socketpair", spy)
    r = h.launch(peer="prepared_creds", timeout=2.0)
    assert seen["passcred"] == 0
    # 偽 launcher は GO の後に何もしないので、handoff 後の期限切れで終わる
    assert (r.reason, r.series) == (P.ATTESTATION_DEADLINE, L.AFTER_HANDOFF), r
    assert len(h.listeners) == 1


@pytest.mark.parametrize("scenario, reason, series", [
    ("die_before_hello", P.LAUNCHER_DIED, L.BEFORE_HANDOFF),
    ("silent", P.ATTESTATION_DEADLINE, L.BEFORE_HANDOFF),
    ("no_prepared", P.ATTESTATION_DEADLINE, L.BEFORE_HANDOFF),
    ("prepared_bad_hash", P.ATTESTATION_MISMATCH, L.BEFORE_HANDOFF),
    ("go_send_fails", P.ATTESTATION_GO_SEND_FAILED, L.AFTER_HANDOFF),
    ("exec_failed", P.ATTESTATION_EXEC_FAILED, L.AFTER_HANDOFF),
    ("exec_timeout", P.ATTESTATION_DEADLINE, L.AFTER_HANDOFF),
    ("exe_mismatch", P.CLI_EXEC_UNPROVEN, L.AFTER_HANDOFF),
])
def test_early_and_late_setup_failure_paths(h, tmp_path, scenario, reason, series):
    # 偽 launcher は python で動くので、CLI 役は python と別 inode の写しにする
    # (同じ inode だと exe_mismatch が一致してしまう)
    cli = tmp_path / "python"
    copy_python(cli)
    r = h.launch(peer=scenario, cli=str(cli), argv=[str(cli), *COPIED_PY_SLEEP_ARGS],
                 timeout=2.0)
    assert not r.started
    assert (r.reason, r.series) == (reason, series), r
    want = L.FAILED if reason == P.CLI_EXEC_UNPROVEN else L.SANDBOX_UNAVAILABLE
    assert r.classification == want
    if series == L.BEFORE_HANDOFF:
        assert r.reaped and r.launcher_proc.returncode is not None
        assert h.listeners == []
    else:
        # handoff 後は receiver の停止が先なので、worker は reap せずに返す
        assert not r.reaped and r.launcher_proc.returncode is None
        assert len(h.listeners) == 1
        assert L.reap_launcher(r.launcher_proc, deadline=time.monotonic() + 5)


def test_receiver_handoff_failure_closes_the_listener(h):
    before = _fds()

    def refuse(fd: int) -> None:
        raise RuntimeError("receiver not running")

    r = h.launch(handoff=refuse)
    assert (r.reason, r.series) == (P.RECEIVER_HANDOFF_FAILED, L.BEFORE_HANDOFF), r
    assert r.reaped
    assert _fds() == before


@pytest.mark.parametrize("err", [errno.EINTR, errno.EBADF])
def test_listener_close_error_in_handoff_failure_is_not_retried(h, monkeypatch, err):
    """handoff 失敗で listener を閉じる close が EINTR / EBADF を返しても、同じ番号をもう
    一度閉じない (Linux の close は EINTR でも fd を解放しており、再 close は再利用された
    別の fd を閉じ得る)。結果は handoff 前の固定 reason のまま。"""
    real_close = os.close
    target: list[int] = []
    calls: list[int] = []

    def refuse(fd: int) -> None:
        target.append(fd)
        raise RuntimeError("receiver not running")

    def flaky_close(fd: int) -> None:
        if target and fd == target[0]:
            calls.append(fd)
            if len(calls) == 1:
                if err == errno.EINTR:
                    real_close(fd)
                raise OSError(err, os.strerror(err))
        real_close(fd)

    monkeypatch.setattr(os, "close", flaky_close)
    try:
        r = h.launch(handoff=refuse)
    finally:
        monkeypatch.setattr(os, "close", real_close)
        if err == errno.EBADF and target:
            try:
                real_close(target[0])
            except OSError:
                pass
    assert (r.reason, r.series) == (P.RECEIVER_HANDOFF_FAILED, L.BEFORE_HANDOFF), r
    assert calls == [target[0]]


def test_cgroup_join_failure_is_fixed_reason(h):
    def no_join(pid: int) -> None:
        raise OSError(errno.EACCES, "cgroup.procs")

    r = h.launch(join=no_join)
    assert (r.reason, r.series) == (P.CGROUP_JOIN_FAILED, L.BEFORE_HANDOFF), r
    assert r.reaped


def test_worker_holds_no_listener_after_handoff(h):
    base = len(_notify_fds())
    r = h.launch()
    assert r.started, r
    assert len(_notify_fds()) == base + 1
    assert h.listeners[0] in _notify_fds()

    h2 = _Harness()
    try:
        r2 = h2.launch(peer="prepared_bad_hash")
        assert r2.reason == P.ATTESTATION_MISMATCH
        assert len(_notify_fds()) == base + 1
    finally:
        h2.close()


def test_launcher_proc_is_reaped_by_worker_only(h):
    creds = L.current_credentials()
    bad = L.ExpectedCredentials(creds.uid + 1, creds.gid, creds.groups)
    r = h.launch(credentials=bad)
    assert r.reason == "credentials_unexpected"
    assert r.reaped and r.launcher_proc.returncode is not None
    assert not Path(f"/proc/{r.launcher_proc.pid}").exists()


# --- 受入 21 -------------------------------------------------------------------------

def test_stderr_and_exit_code_do_not_decide_setup(h):
    r = h.launch(peer="stderr_success_rc0", stderr=subprocess.PIPE)
    assert (r.reason, r.series) == (P.LAUNCHER_DIED, L.BEFORE_HANDOFF), r
    assert r.launcher_proc.returncode == 0
    assert b"setup ok" in r.launcher_proc.stderr.read()

    r = h.launch(peer="stderr_error_but_valid", stderr=subprocess.PIPE)
    assert r.started, r


# --- 受入 23 -------------------------------------------------------------------------

_HELPER_PRELUDE = r"""
import ctypes, json, os, resource, sys, time
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
sys.path.insert(0, {src!r})
from agentic_fx.core import cli_launch as L
joined, got = [], []
def launch(**kw):
    r = L.launch_cli(cli_fd=L.open_cli_binary({py!r}), argv={argv!r},
                     env={{"PATH": "/usr/bin:/bin"}}, backend="claude", join=joined.append,
                     handoff=got.append, deadline=time.monotonic() + 10,
                     stdout=-3, stderr=-3, **kw)
    if r.launcher_proc is not None and r.launcher_proc.returncode is None:
        r.launcher_proc.kill(); r.launcher_proc.wait()
    print(json.dumps({{"reason": r.reason, "series": r.series, "joined": joined,
                       "detail": r.setup_detail, "got": len(got)}}))
"""


def _run_helper(body: str, prefix: list[str] = ()) -> dict:
    code = _HELPER_PRELUDE.format(src=SRC, py=PY, argv=PY_SLEEP_ARGV) + textwrap.dedent(body)
    proc = subprocess.run([*prefix, sys.executable, "-c", code], capture_output=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr.decode()[-2000:]
    return json.loads(proc.stdout.decode().strip().splitlines()[-1])


def test_inherited_seccomp_filter_fails_closed():
    out = _run_helper(r"""
        class F(ctypes.Structure):
            _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8),
                        ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]
        class Prog(ctypes.Structure):
            _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(F))]
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        arr = (F * 1)(F(0x06, 0, 0, 0x7FFF0000))
        prog = Prog(1, ctypes.cast(arr, ctypes.POINTER(F)))
        assert libc.prctl(38, 1, 0, 0, 0) == 0
        assert libc.syscall(ctypes.c_long(317), ctypes.c_uint(1), ctypes.c_uint(0),
                            ctypes.byref(prog)) == 0
        launch()
    """)
    assert out["reason"] == "inherited_seccomp_filter"
    assert out["series"] == L.BEFORE_HANDOFF
    # HELLO の前に止まるので join も seccomp install も起きない
    assert out["joined"] == [] and out["got"] == 0


def test_listener_sent_and_launcher_copy_closed(h):
    seen = {}

    def inspect(fd: int) -> None:
        pid = h.joined[0]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _notify_fds(pid):
            time.sleep(0.01)
        seen["launcher_notify_fds"] = _notify_fds(pid)
        seen["launcher_status"] = _status(pid)
        seen["received"] = L.is_seccomp_notify_fd(fd)
        seen["link"] = os.readlink(f"/proc/self/fd/{fd}")
        h.listeners.append(fd)

    r = h.launch(handoff=inspect)
    assert r.started, r
    # PREPARED の後・GO の前の launcher は listener を持たない
    assert seen["launcher_notify_fds"] == []
    assert seen["launcher_status"]["Seccomp"] == "2"
    assert seen["received"] and seen["link"] == "anon_inode:seccomp notify"


def test_wait_killable_and_user_notif_probes():
    assert cli_launcher.probe_seccomp_capabilities() == ()
    ok = {"user_notif": (0, 0), "notif_sizes": (0, 0),
          "new_listener": (-1, errno.EFAULT), "wait_killable": (-1, errno.EFAULT),
          "tsync_esrch": (-1, errno.EFAULT)}
    assert cli_launcher.classify_probes(ok) == ()
    assert cli_launcher.classify_probes({**ok, "wait_killable": (-1, errno.EINVAL)}) == (
        P.SECCOMP_WAIT_KILLABLE_UNAVAILABLE,)
    assert cli_launcher.classify_probes({**ok, "new_listener": (-1, errno.EINVAL)}) == (
        P.SECCOMP_USER_NOTIF_UNAVAILABLE,)
    assert cli_launcher.classify_probes({**ok, "user_notif": (-1, errno.EOPNOTSUPP)}) == (
        P.SECCOMP_USER_NOTIF_UNAVAILABLE,)
    assert cli_launcher.classify_probes({**ok, "tsync_esrch": (-1, errno.EINVAL)}) == (
        P.SECCOMP_TSYNC_ESRCH_UNAVAILABLE,)
    # probe は filter を掛けない
    assert _status(os.getpid())["Seccomp"] == "0"


class _Notif(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint64), ("pid", ctypes.c_uint32),
                ("flags", ctypes.c_uint32), ("nr", ctypes.c_int32),
                ("arch", ctypes.c_uint32), ("ip", ctypes.c_uint64),
                ("args", ctypes.c_uint64 * 6)]


class _Resp(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint64), ("val", ctypes.c_int64),
                ("error", ctypes.c_int32), ("flags", ctypes.c_uint32)]


_IOC_RECV, _IOC_SEND, _IOC_ID_VALID = 0xC0502100, 0xC0182101, 0x40082102


def test_wait_killable_recv_keeps_notification_under_signal(h):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ioctl.restype = ctypes.c_int
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    code = f"""
        import errno, json, os, signal, socket, sys
        hits = []
        signal.signal(signal.SIGUSR1, lambda *a: hits.append(1))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("127.0.0.1", {port}))
            res = 0
        except OSError as e:
            res = e.errno
        # syscall から戻った後の bytecode で Python の handler が走る
        for _ in range(1000):
            pass
        print(json.dumps({{"connect": res, "hits": len(hits)}}), flush=True)
    """
    r = h.launch(cli=PY, argv=[PY, "-I", "-S", "-c", textwrap.dedent(code)],
                 stdout=subprocess.PIPE)
    assert r.started, r
    lfd = h.listeners[0]
    p = select.poll()
    p.register(lfd, select.POLLIN)
    assert p.poll(10_000), "connect の notification が来ない"
    n = _Notif()
    assert libc.ioctl(lfd, ctypes.c_ulong(_IOC_RECV), ctypes.byref(n)) == 0
    assert n.nr == SP.SNAPSHOT_NUMBERS["connect"]
    for _ in range(5):
        os.kill(r.launcher_proc.pid, signal.SIGUSR1)
        time.sleep(0.05)
    # 受け取った後の notification は signal で再発行されない
    assert p.poll(500) == []
    assert libc.ioctl(lfd, ctypes.c_ulong(_IOC_ID_VALID),
                      ctypes.byref(ctypes.c_uint64(n.id))) == 0
    resp = _Resp(n.id, 0, -errno.ECONNREFUSED, 0)
    assert libc.ioctl(lfd, ctypes.c_ulong(_IOC_SEND), ctypes.byref(resp)) == 0
    out, _ = r.launcher_proc.communicate(timeout=10)
    srv.close()
    res = json.loads(out.decode().strip().splitlines()[-1])
    assert res["connect"] == errno.ECONNREFUSED
    assert res["hits"] >= 1


# --- 受入 10 -------------------------------------------------------------------------

@pytest.mark.parametrize("field, detail", [
    ("uid", P.CRED_DETAIL_UID), ("gid", P.CRED_DETAIL_GID), ("groups", P.CRED_DETAIL_GROUPS)])
def test_credential_mismatch_fails_before_hello(h, field, detail):
    c = L.current_credentials()
    shifted = {"uid": c.uid, "gid": c.gid, "groups": c.groups}
    shifted[field] = (c.groups + (max(c.groups + (c.gid,)) + 1,)
                      if field == "groups" else shifted[field] + 1)
    r = h.launch(credentials=L.ExpectedCredentials(**shifted))
    assert (r.reason, r.series) == ("credentials_unexpected", L.BEFORE_HANDOFF), r
    assert r.setup_detail == detail
    assert h.joined == [], "HELLO が届いている"


def test_nonzero_capabilities_fail_closed():
    unshare = shutil.which("unshare")
    if unshare is None:
        pytest.skip("unshare が無い")
    probe = subprocess.run([unshare, "-Ur", "true"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip(f"unprivileged user namespace を作れない: {probe.stderr.decode()[-200:]}")
    out = _run_helper("launch()\n", prefix=[unshare, "-Ur"])
    assert out["reason"] == "credentials_unexpected"
    assert out["detail"] == P.CRED_DETAIL_CAPS
    assert out["joined"] == []


def test_credential_setters_are_eperm(h):
    _r, out = _python_cli(h, """
        import ctypes, json, os
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        def raw(nr, *a):
            ctypes.set_errno(0)
            rc = libc.syscall(ctypes.c_long(nr), *[ctypes.c_long(x) for x in a])
            return ctypes.get_errno() if rc == -1 else 0
        uid, gid = os.getuid(), os.getgid()
        hdr = (ctypes.c_uint32 * 2)(0x20080522, 0)
        data = (ctypes.c_uint32 * 6)()
        print(json.dumps({
            "setuid": raw(105, uid), "setgid": raw(106, gid),
            "setresuid": raw(117, uid, uid, uid), "setgroups": raw(116, 0, 0),
            "capset": raw(126, ctypes.addressof(hdr), ctypes.addressof(data)),
        }))
    """)
    assert out == {k: errno.EPERM for k in ("setuid", "setgid", "setresuid", "setgroups",
                                             "capset")}


# --- 受入 12b ------------------------------------------------------------------------

def test_clone3_falls_back_and_spawn_paths_work(h):
    _r, out = _python_cli(h, """
        import ctypes, json, os, subprocess, threading
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        def raw(nr, *a):
            ctypes.set_errno(0)
            rc = libc.syscall(ctypes.c_long(nr), *[ctypes.c_long(x) for x in a])
            return rc, ctypes.get_errno() if rc == -1 else 0
        res = {}
        args = (ctypes.c_uint64 * 11)()
        res["clone3"] = raw(435, ctypes.addressof(args), 88)[1]
        t = threading.Thread(target=lambda: res.__setitem__("thread", True)); t.start(); t.join()
        res["subprocess"] = subprocess.run(["/usr/bin/true"]).returncode
        pid = os.fork()
        if pid == 0:
            os._exit(7)
        res["fork"] = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
        rc, err = raw(56, 0x10000000 | 17, 0, 0, 0, 0)  # CLONE_NEWUSER | SIGCHLD
        if rc == 0:
            os._exit(0)
        if rc > 0:
            os.waitpid(rc, 0)
        res["clone_newuser"] = err
        print(json.dumps(res))
    """)
    assert out == {"clone3": errno.ENOSYS, "thread": True, "subprocess": 0, "fork": 7,
                   "clone_newuser": errno.EPERM}


def test_posix_timer_is_eperm(h):
    _r, out = _python_cli(h, """
        import ctypes, json
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        tid = ctypes.c_int(0)
        ctypes.set_errno(0)
        rc = libc.syscall(ctypes.c_long(222), ctypes.c_long(1), None, ctypes.byref(tid))
        print(json.dumps({"rc": rc, "errno": ctypes.get_errno()}))
    """)
    assert out == {"rc": -1, "errno": errno.EPERM}


def test_installed_filter_flags_match_policy(h):
    # 本物の launcher の install 関数を包み、渡された flag を stderr に書かせる
    # (stderr は setup の判定には使わず、ここで観測するだけ)
    wrap = ("import sys\n"
            "from agentic_fx.core import cli_launcher as m\n"
            "orig = m.install_filter\n"
            "def spy(program, flags):\n"
            "    sys.stderr.write(f'FLAGS {flags} LEN {len(program)}\\n'); sys.stderr.flush()\n"
            "    return orig(program, flags)\n"
            "m.install_filter = spy\n"
            "m.main(sys.argv[1:])\n")
    r = h.launch(launcher_cmd=[sys.executable, "-I", "-c", wrap], stderr=subprocess.PIPE)
    assert r.started, r
    r.launcher_proc.kill()
    r.launcher_proc.wait(10)
    err = r.launcher_proc.stderr.read().decode()
    flags = int(err.split("FLAGS ", 1)[1].split()[0])
    assert flags == SP.FILTER_FLAGS
    assert not flags & SP.SECCOMP_FILTER_FLAG_LOG

    # TSYNC: install の前から居た thread にも filter が掛かる
    code = textwrap.dedent(f"""
        import ctypes, json, os, resource, sys, threading
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        sys.path.insert(0, {SRC!r})
        from agentic_fx.core import cli_launcher, seccomp_cli_bpf
        ev = threading.Event()
        t = threading.Thread(target=ev.wait); t.start()
        c = seccomp_cli_bpf.compile_policy()
        libc = ctypes.CDLL(None, use_errno=True)
        assert libc.prctl(38, 1, 0, 0, 0) == 0
        fd = cli_launcher.install_filter(c.program, c.filter_flags)
        tasks = {{}}
        for tid in os.listdir("/proc/self/task"):
            for line in open(f"/proc/self/task/{{tid}}/status"):
                if line.startswith("Seccomp:"):
                    tasks[tid] = line.split()[1]
        ev.set(); t.join()
        print(json.dumps({{"tasks": tasks, "fd": fd >= 0}}))
    """)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode()[-2000:]
    out = json.loads(proc.stdout.decode().strip().splitlines()[-1])
    assert out["fd"] and len(out["tasks"]) == 2
    assert set(out["tasks"].values()) == {"2"}


# --- rlimit ------------------------------------------------------------------------

def test_rlimits_are_fixed(h):
    r = h.launch()
    assert r.started, r
    pid = r.launcher_proc.pid
    limits = {}
    for line in Path(f"/proc/{pid}/limits").read_text().splitlines()[1:]:
        name = line[:26].strip()
        soft, hard = line[26:47].strip(), line[47:68].strip()
        limits[name] = (soft, hard)
    mine = Path("/proc/self/limits").read_text()
    mib = 1024 * 1024
    assert limits["Max address space"] == (str(4096 * mib),) * 2
    assert limits["Max open files"] == ("128", "128")
    assert limits["Max file size"] == (str(8 * mib),) * 2
    for name in ("Max core file size", "Max nice priority", "Max realtime priority",
                 "Max realtime timeout", "Max locked memory", "Max msgqueue size"):
        assert limits[name] == ("0", "0"), name
    pending = [ln for ln in mine.splitlines() if ln.startswith("Max pending signals")][0]
    assert limits["Max pending signals"] == (pending[26:47].strip(), pending[47:68].strip())


# --- 実装の形 ------------------------------------------------------------------------

def test_launcher_imports_only_stdlib_and_isolation_modules():
    code = ("import sys\n"
            "before = set(sys.modules)\n"
            "import agentic_fx.core.cli_launcher\n"
            "print(sorted(m for m in sys.modules if m.startswith('agentic_fx')))\n")
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode()
    mods = eval(proc.stdout.decode())  # noqa: S307  自分の子の出力
    assert mods == ["agentic_fx", "agentic_fx.core", "agentic_fx.core.cli_launch_protocol",
                    "agentic_fx.core.cli_launcher", "agentic_fx.core.landlock_nested",
                    "agentic_fx.core.seccomp", "agentic_fx.core.seccomp_cli_bpf",
                    "agentic_fx.core.seccomp_cli_policy"]


def test_shebang_script_past_inspection_is_exec_failed_enoent(h, tmp_path, monkeypatch):
    """`#!` の script は worker の artifact 検査で launcher 前に落ちる。検査を
    すり抜けた場合も、exec 時に fd が CLOEXEC で閉じるため interpreter が
    `/dev/fd/N` を開けず kernel が ENOENT を返し、EXEC_FAILED として handoff 後の
    失敗になる (fd を開いたままにする迂回は IC-FD を破るので取らない)。"""
    script = tmp_path / "cli.sh"
    script.write_text("#!/bin/sh\nexec sleep 30\n")
    script.chmod(0o700)
    monkeypatch.setattr(L, "inspect_cli_artifact",
                        lambda fd: ((os.fstat(fd).st_dev, os.fstat(fd).st_ino), ""))
    r = h.launch(cli=str(script), argv=[str(script)])
    assert (r.reason, r.series) == (P.ATTESTATION_EXEC_FAILED, L.AFTER_HANDOFF), r
    assert r.setup_detail == errno.ENOENT
    assert L.reap_launcher(r.launcher_proc, deadline=time.monotonic() + 5)


def test_launcher_works_under_outer_domain_without_proc():
    """outer Landlock が /proc を許さない backend でも、launcher の自己検査
    (seccomp mode、no_new_privs、capability) が通り CLI まで届く。"""
    roots = sorted({os.path.realpath(p) for p in (
        sys.prefix, sys.base_prefix, SRC, "/usr", "/etc", "/lib", "/lib64", "/bin")
        if os.path.exists(p)})
    out = _run_helper(f"""
        import errno, struct
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        handled = (1 << 16) - 1
        attr = struct.pack("QQQ", handled, 0, 0)
        buf = ctypes.create_string_buffer(attr, len(attr))
        rfd = libc.syscall(444, buf, ctypes.c_size_t(len(attr)), ctypes.c_uint32(0))
        assert rfd >= 0, ctypes.get_errno()
        def allow(path, access):
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            rule = struct.pack("=Qi", access, fd)
            rb = ctypes.create_string_buffer(rule, len(rule))
            assert libc.syscall(445, ctypes.c_int(rfd), ctypes.c_int(1), rb,
                                ctypes.c_uint32(0)) == 0, (path, ctypes.get_errno())
            os.close(fd)
        for root in {roots!r}:
            allow(root, (1 << 0) | (1 << 2) | (1 << 3))
        allow("/dev/null", (1 << 1) | (1 << 2))
        for dev in ("/dev/urandom", "/dev/random", "/dev/zero"):
            allow(dev, 1 << 2)
        assert libc.prctl(38, 1, 0, 0, 0) == 0
        assert libc.syscall(446, ctypes.c_int(rfd), ctypes.c_uint32(0)) == 0
        try:
            open("/proc/self/status").read()
            raise SystemExit("/proc が読める")
        except PermissionError:
            pass
        launch()
    """)
    assert (out["reason"], out["series"], out["got"]) == (None, None, 1), out


# --- 判定の境界を 1 つずつ固定する --------------------------------------------------

@pytest.mark.parametrize("field", [
    "profile_id", "policy_hash", "bpf_hash", "filter_flags", "landlock_abi"])
def test_prepared_each_field_must_match(h, field):
    """PREPARED の 5 field はどれか 1 つ違っても不一致で、listener は worker が閉じる。"""
    good = L.expected_prepared()
    bad = {"profile_id": "improve-cli/0", "policy_hash": "00" * 32, "bpf_hash": "00" * 32,
           "filter_flags": good.filter_flags ^ SP.SECCOMP_FILTER_FLAG_WAIT_KILLABLE_RECV,
           "landlock_abi": good.landlock_abi + 1}[field]
    before = _notify_fds()
    r = h.launch(expected=dataclasses.replace(good, **{field: bad}))
    assert (r.reason, r.series) == (P.ATTESTATION_MISMATCH, L.BEFORE_HANDOFF), r
    assert r.reaped and h.listeners == []
    assert _notify_fds() == before


def _raw_launcher():
    """本物の launcher を protocol の相手役なしで起こす (worker 役はテストが手で話す)。
    CLI 役は python の sleep。"""
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    nonce = P.new_launch_nonce()
    creds = L.current_credentials()
    cli_fd = L.open_cli_binary(PY)
    cfg = {"status_fd": child.fileno(), "cli_fd": cli_fd, "nonce": nonce.hex(),
           "expected_ppid": os.getpid(), "uid": creds.uid, "gid": creds.gid,
           "groups": list(creds.groups), "backend": "claude",
           "argv": PY_SLEEP_ARGV}
    proc = subprocess.Popen([sys.executable, "-I", "-m", L.LAUNCHER_MODULE, json.dumps(cfg)],
                            pass_fds=(child.fileno(), cli_fd), env=_env(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child.close()
    os.close(cli_fd)
    parent.settimeout(10)
    return proc, parent, nonce


def _recv_frame(sock: socket.socket, nonce: bytes):
    data, anc, _flags, _ = sock.recvmsg(P.RECV_DATA_LEN, P.RECV_ANC_LEN,
                                        socket.MSG_CMSG_CLOEXEC)
    fds, _other = P.take_fds(anc)
    P.close_fds(fds)
    return P.decode(data, nonce) if data else None


def _bad_frame(kind: str, nonce: bytes, ftype: int):
    good = P.encode_joined(nonce) if ftype == P.JOINED else P.encode_go(nonce)
    if kind == "nonce":
        return [good[:8] + bytes(16) + good[24:]], []
    if kind == "type":
        return [P.encode_go(nonce) if ftype == P.JOINED else P.encode_joined(nonce)], []
    r, w = os.pipe()
    os.close(w)
    return [good], [r]


@pytest.mark.parametrize("stage", ["joined", "go"])
@pytest.mark.parametrize("kind", ["nonce", "type", "fd"])
def test_launcher_rejects_bad_worker_frames(stage, kind):
    """launcher は worker からの JOINED / GO を厳密に検査し、nonce・type・ancillary の
    どれが違っても次の段へ進まずに終わる (PREPARED を送らない / exec しない)。"""
    proc, sock, nonce = _raw_launcher()
    try:
        hello = _recv_frame(sock, nonce)
        assert hello is not None and hello.type == P.HELLO and hello.pid == proc.pid
        if stage == "go":
            sock.sendmsg([P.encode_joined(nonce)])
            prepared = _recv_frame(sock, nonce)
            assert prepared is not None and prepared.type == P.PREPARED
        data, fds = _bad_frame(kind, nonce, P.JOINED if stage == "joined" else P.GO)
        anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("<i", fds[0]))] if fds else []
        sock.sendmsg(data, anc)
        P.close_fds(fds)
        assert _recv_frame(sock, nonce) is None, "launcher が次の段へ進んだ"
        # exec していれば CLI が 30 秒残る。launcher 自身の _exit(1) で終わること
        assert proc.wait(timeout=10) == 1
    finally:
        sock.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)


def test_unexpected_exception_closes_status_end(h, monkeypatch):
    """想定外の例外は `LaunchError` に包み、launcher の handle と系列を呼び出し側に
    渡す。status 親端は閉じている。"""
    before = _fds()

    def bad_joined(nonce: bytes) -> bytes:
        raise RuntimeError("injected")

    monkeypatch.setattr(P, "encode_joined", bad_joined)
    with pytest.raises(L.LaunchError) as excinfo:
        h.launch()
    err = excinfo.value
    try:
        assert isinstance(err.__cause__, RuntimeError)
        assert err.series == L.BEFORE_HANDOFF
        assert err.launcher_proc.pid == h.joined[0]
        # traceback が launch_cli の frame を掴んだままでも、status 親端は閉じている
        assert _fds() == before
    finally:
        assert L.reap_launcher(err.launcher_proc, deadline=time.monotonic() + 5)


def test_unexpected_exception_after_prepared_closes_held_listener(h, monkeypatch):
    """PREPARED を受けた後・handoff 前の想定外例外でも、worker が持つ listener は閉じる
    (呼び出し側にも receiver にも渡っていないので、残ると worker の終了まで生きる)。"""
    before = _fds()

    def bad_probe(fd: int) -> bool:
        raise RuntimeError("injected")

    monkeypatch.setattr(L, "is_seccomp_notify_fd", bad_probe)
    with pytest.raises(L.LaunchError) as excinfo:
        h.launch()
    err = excinfo.value
    try:
        assert err.series == L.BEFORE_HANDOFF and not h.listeners
        assert _notify_fds() == []
        assert _fds() == before
    finally:
        assert L.reap_launcher(err.launcher_proc, deadline=time.monotonic() + 5)


class _NullCgroup:
    def kill_quietly(self) -> bool:
        return True

    def wait_unpopulated(self, deadline: float) -> bool:
        return True


def test_listener_closed_by_launch_cli_is_not_closed_again_by_cleanup(h, monkeypatch):
    """handoff 前に launch_cli が閉じた listener は結果に「未移譲 fd なし」と載る。後始末の
    系列は結果に載った fd だけを閉じるので、番号が別の fd に再利用されていても閉じない。"""
    from agentic_fx.core import mission_cleanup as MC

    seen: list[int] = []

    def bad_probe(fd: int) -> bool:
        seen.append(fd)
        raise RuntimeError("injected")

    monkeypatch.setattr(L, "is_seccomp_notify_fd", bad_probe)
    with pytest.raises(L.LaunchError) as excinfo:
        h.launch()
    err = excinfo.value
    assert err.series == L.BEFORE_HANDOFF and err.untransferred_fds == ()
    assert L.reap_launcher(err.launcher_proc, deadline=time.monotonic() + 5)
    # 閉じられた listener の番号を別の fd に再利用させる
    reused = []
    try:
        while not reused or reused[-1] < seen[0]:
            reused.append(os.open("/dev/null", os.O_RDONLY))
        assert reused[-1] == seen[0]
        rep = MC.cleanup_after_launch(
            err, acquire=lambda: MC.AckedDeadline(time.monotonic() + 30), cgroup=_NullCgroup())
        assert {s["step"]: s for s in rep.steps}["close_untransferred"]["fds"] == []
        os.fstat(seen[0])  # 再利用された fd は開いたまま
    finally:
        for fd in reused:
            os.close(fd)


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_interrupt_is_raised_as_launch_interrupted_with_handle(h, exc):
    """KeyboardInterrupt / SystemExit / GeneratorExit は `LaunchInterrupted` (BaseException)
    で出る。呼び出し側の `except Exception` に捕まらず、原因は元の例外で、launcher の
    handle を渡すので呼び出し側が kill して reap できる。handoff 前の中断なので受け取り
    途中の listener と status 親端は閉じている。"""
    before = _fds()

    def interrupt(fd: int) -> None:
        raise exc()

    with pytest.raises(L.LaunchInterrupted) as excinfo:
        try:
            h.launch(handoff=interrupt)
        except Exception:  # noqa: BLE001  捕まらないことの確認
            pytest.fail("LaunchInterrupted was caught by except Exception")
    err = excinfo.value
    try:
        assert not isinstance(err, Exception)
        assert type(err.__cause__) is exc
        assert err.series == L.BEFORE_HANDOFF
        assert err.launcher_proc.pid == h.joined[0]
        assert _fds() == before
    finally:
        assert L.reap_launcher(err.launcher_proc, deadline=time.monotonic() + 5)


def test_interrupt_after_handoff_keeps_listener_with_receiver(h, monkeypatch):
    """handoff 後の中断は listener を receiver に渡した後なので閉じず、系列は handoff 後。"""
    def bad_go(nonce: bytes) -> bytes:
        raise KeyboardInterrupt()

    monkeypatch.setattr(P, "encode_go", bad_go)
    with pytest.raises(L.LaunchInterrupted) as excinfo:
        h.launch()
    err = excinfo.value
    try:
        assert err.series == L.AFTER_HANDOFF and len(h.listeners) == 1
        assert err.launcher_proc.returncode is None
        assert _notify_fds() == h.listeners
    finally:
        assert L.reap_launcher(err.launcher_proc, deadline=time.monotonic() + 5)


def test_other_thread_is_rejected_before_launcher(h, tmp_path):
    """PDEATHSIG は親の thread に結びつくので、main thread 以外からは launcher を
    起こさず固定 reason で返す。渡した CLI fd も閉じる。"""
    marker = tmp_path / "launcher-ran"
    before = _fds()
    box: list = []

    def run() -> None:
        box.append(h.launch(launcher_cmd=[sys.executable, "-c",
                                          f"open({str(marker)!r}, 'w')"]))

    t = threading.Thread(target=run)
    t.start()
    t.join(30)
    r = box[0]
    assert (r.started, r.reason, r.series, r.classification) == (
        False, P.LAUNCH_THREAD_UNSUPPORTED, L.BEFORE_HANDOFF, L.SANDBOX_UNAVAILABLE), r
    assert P.LAUNCH_THREAD_UNSUPPORTED == "launch_thread_unsupported"
    assert r.launcher_proc is None and r.reaped
    assert h.joined == [] and h.listeners == [] and not marker.exists()
    assert _fds() == before


def test_unexpected_exception_after_handoff_keeps_handle(h, monkeypatch):
    """handoff 後の想定外例外も launcher の handle を失わず、系列は handoff 後。"""
    def bad_go(nonce: bytes) -> bytes:
        raise RuntimeError("injected")

    monkeypatch.setattr(P, "encode_go", bad_go)
    with pytest.raises(L.LaunchError) as excinfo:
        h.launch()
    err = excinfo.value
    assert isinstance(err.__cause__, RuntimeError)
    assert err.series == L.AFTER_HANDOFF and len(h.listeners) == 1
    assert err.launcher_proc.pid == h.joined[0]
    assert err.launcher_proc.returncode is None
    assert L.reap_launcher(err.launcher_proc, deadline=time.monotonic() + 5)


def test_spawn_failure_is_fixed_reason_and_closes_fds(h):
    before = _fds()
    r = h.launch(launcher_cmd=["/nonexistent/afx-launcher"])
    assert (r.reason, r.series, r.classification) == (
        "launcher_spawn_failed", L.BEFORE_HANDOFF, L.SANDBOX_UNAVAILABLE), r
    assert r.launcher_proc is None
    assert _fds() == before


def test_received_listener_is_cloexec(h):
    r = h.launch()
    assert r.started, r
    assert fcntl.fcntl(h.listeners[0], fcntl.F_GETFD) & fcntl.FD_CLOEXEC


def test_setup_deadline_is_absolute(h):
    t0 = time.monotonic()
    r = h.launch(peer="silent", timeout=1.0)
    elapsed = time.monotonic() - t0
    assert (r.reason, r.series) == (P.ATTESTATION_DEADLINE, L.BEFORE_HANDOFF), r
    assert elapsed < 1.0 + 2.0, elapsed


def test_recv_rejects_ancillary_other_than_rights():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        a.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        nonce = P.new_launch_nonce()
        b.sendmsg([P.encode_hello(nonce, 1)])
        ev = L._recv(a, nonce, time.monotonic() + 5)
        assert ev.kind == "malformed", ev
    finally:
        a.close()
        b.close()


def test_cli_runs_as_child_subreaper(h):
    _r, out = _python_cli(h, """
        import ctypes, json
        libc = ctypes.CDLL(None, use_errno=True)
        v = ctypes.c_int(-1)
        rc = libc.prctl(37, ctypes.byref(v), 0, 0, 0)
        print(json.dumps({"rc": rc, "subreaper": v.value}))
    """)
    assert out == {"rc": 0, "subreaper": 1}


def test_cli_cannot_signal_outside_nested_domain(h):
    _r, out = _python_cli(h, f"""
        import json, os
        def k(pid):
            try:
                os.kill(pid, 0)
                return 0
            except OSError as e:
                return e.errno
        print(json.dumps({{"worker": k({os.getpid()}), "self": k(os.getpid())}}))
    """)
    assert out == {"worker": errno.EPERM, "self": 0}


def test_cli_dies_with_worker():
    """worker が死ぬと exec 後の CLI も PDEATHSIG で死ぬ (exec を越えて残る設定)。"""
    out = _run_helper("""
        r = L.launch_cli(cli_fd=L.open_cli_binary(%r),
                         argv=[%r, "-I", "-S", "-c", "import time; time.sleep(30)"],
                         env={"PATH": "/usr/bin:/bin"}, backend="claude",
                         join=joined.append, handoff=got.append,
                         deadline=time.monotonic() + 10, stdout=-3, stderr=-3)
        pid = r.launcher_proc.pid
        time.sleep(0.5)
        print(json.dumps({"reason": r.reason, "pid": pid,
                          "alive": r.launcher_proc.poll() is None}), flush=True)
        os._exit(0)
    """ % (PY, PY))
    assert out["alive"], "worker の生存中に CLI が終わっている"
    assert out["reason"] is None
    pid = out["pid"]
    try:
        deadline = time.monotonic() + 5
        alive = True
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except OSError:
                alive = False
                break
            if state in ("Z", "X"):
                alive = False
                break
            time.sleep(0.05)
        assert not alive, "worker の死後も CLI が残っている"
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_launcher_must_be_direct_child(h):
    """launcher の親が worker でなければ HELLO を送らずに終わる。"""
    r = h.launch(launcher_cmd=[
        "/bin/sh", "-c", f'{sys.executable} -I -m {L.LAUNCHER_MODULE} "$0"; exit 0'])
    assert (r.reason, r.series) == (P.LAUNCHER_DIED, L.BEFORE_HANDOFF), r
    assert h.joined == []


def _wrapped_launcher(patch: str) -> list[str]:
    return [sys.executable, "-I", "-c",
            "import ctypes, os, sys\n"
            "from agentic_fx.core import cli_launcher as m\n"
            + textwrap.dedent(patch) + "\nm.main(sys.argv[1:])\n"]


@pytest.mark.parametrize("patch, reason, detail", [
    ("""
     def bad(program, flags):
         raise OSError(22, "injected")
     m.install_filter = bad
     """, "seccomp_install_failed", errno.EINVAL),
    ("""
     from agentic_fx.core import seccomp_cli_bpf
     def bad():
         raise RuntimeError("injected")
     seccomp_cli_bpf.compile_policy = bad
     """, "policy_compile_failed", 0),
    ("""
     m.install_filter = lambda program, flags: os.open("/dev/null", os.O_RDONLY)
     """, "seccomp_self_check_failed", 0),
    ("""
     m._no_new_privs = lambda: 0
     """, "seccomp_self_check_failed", 0),
    ("""
     real = m._libc()
     class Libc:
         def __getattr__(self, name):
             return getattr(real, name)
         def prctl(self, option, *args):
             if option == 36:
                 ctypes.set_errno(1)
                 return -1
             return real.prctl(option, *args)
     m._LIBC = Libc()
     """, "subreaper_failed", errno.EPERM),
], ids=["install", "compile", "self_check_mode", "self_check_nnp", "subreaper"])
def test_launcher_failure_reasons(h, patch, reason, detail):
    """launcher の段ごとの失敗は、その段の固定 reason で SETUP_FAILED になる。"""
    r = h.launch(launcher_cmd=_wrapped_launcher(patch))
    assert (r.reason, r.series, r.setup_detail) == (reason, L.BEFORE_HANDOFF, detail), r
    assert h.listeners == []


@pytest.mark.parametrize("caps", [(1 << 21, 0, 0), (0, 1 << 21, 0), (0, 0, 1 << 21)],
                         ids=["effective", "permitted", "inheritable"])
def test_any_capability_set_fails_closed(h, caps):
    """effective / permitted / inheritable のどれか 1 つでも非 0 なら HELLO 前に止まる。
    user namespace を作れない host でも判定を通すため、capget の読み値だけを差し替える。"""
    r = h.launch(launcher_cmd=_wrapped_launcher(f"m._capabilities = lambda: {caps!r}"))
    assert (r.reason, r.setup_detail) == ("credentials_unexpected", P.CRED_DETAIL_CAPS), r
    assert h.joined == []


def test_frame_after_go_is_order_violation(h):
    r = h.launch(peer="hello_after_go", timeout=5.0)
    assert (r.reason, r.series) == (P.ATTESTATION_ORDER, L.AFTER_HANDOFF), r
    assert not r.reaped and len(h.listeners) == 1
    assert L.reap_launcher(r.launcher_proc, deadline=time.monotonic() + 5)


def test_malformed_frame_fd_is_closed(h):
    before = _fds()
    r = h.launch(peer="magic_with_fd")
    assert (r.reason, r.series) == (P.ATTESTATION_MALFORMED, L.BEFORE_HANDOFF), r
    assert _fds() == before


def copy_python(dst: Path) -> None:
    """python の実体を別 path に写す (別 inode の CLI 役を作る)。写した python は
    stdlib を自分の場所から探せないので、`copied_python_env` の PYTHONHOME で補う。"""
    shutil.copy(PY, dst)


def copied_python_env(**extra: str) -> dict:
    return {**_env(), "PYTHONHOME": sys.base_prefix, **extra}


# 写した python 用 (-I は PYTHONHOME を無視させるので付けない)
COPIED_PY_SLEEP_ARGS = ["-S", "-c", "import time; time.sleep(30)"]


def test_exe_identity_is_inode_not_path(h, tmp_path):
    """exec された file が渡した fd と同じ inode であることを見る。同じ path に
    置き換えた別の file を exec した場合は `cli_exec_unproven`。"""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    cli, other = tmp_path / "a" / "python", tmp_path / "b" / "python"
    copy_python(cli)
    copy_python(other)
    r = h.launch(peer="exec_replaced_path", cli=str(cli),
                 argv=[str(cli), *COPIED_PY_SLEEP_ARGS],
                 env=copied_python_env(AFX_TEST_REPLACEMENT=str(other)), timeout=5.0)
    assert (r.reason, r.series) == (P.CLI_EXEC_UNPROVEN, L.AFTER_HANDOFF), r
    # 置き換えた側が生きて exec されている (死んで exe が読めないだけの unproven でない)
    assert r.launcher_proc.poll() is None
    assert os.stat(f"/proc/{r.launcher_proc.pid}/exe").st_ino == os.stat(cli).st_ino
    assert L.reap_launcher(r.launcher_proc, deadline=time.monotonic() + 5)


@pytest.mark.parametrize("peer", [None, "die_before_hello"], ids=["started", "failed"])
def test_status_end_is_closed_explicitly(h, peer):
    """status 親端は結果の確定時に明示的に閉じる (GC 任せにしない)。"""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        h.launch(peer=peer)
        gc.collect()
    leaked = [str(w.message) for w in caught
              if issubclass(w.category, ResourceWarning) and "socket" in str(w.message)]
    assert leaked == []


def test_fd_3_is_not_inherited_by_cli():
    """launcher の fd 3 以上は exec で閉じる。CLI fd が 3 番に居る場合も残らない。"""
    out = _run_helper(f"""
        # fd 3 に CLI fd を置く (既存の fd 3 は import 時に開かれた /dev/urandom)
        fd = L.open_cli_binary({PY!r})
        os.dup2(fd, 3, inheritable=False)
        os.close(fd)
        r = L.launch_cli(cli_fd=3, argv=[{PY!r}, "-I", "-S", "-c", "import time; time.sleep(30)"],
                         env={{"PATH": "/usr/bin:/bin"}}, backend="claude",
                         join=joined.append, handoff=got.append,
                         deadline=time.monotonic() + 10, stdout=-3, stderr=-3)
        pid = r.launcher_proc.pid
        end = time.monotonic() + 5
        while time.monotonic() < end:
            fds = sorted(int(x) for x in os.listdir(f"/proc/{{pid}}/fd"))
            if fds == [0, 1, 2]:
                break
            time.sleep(0.05)
        r.launcher_proc.kill(); r.launcher_proc.wait()
        print(json.dumps({{"reason": r.reason, "fds": fds}}))
    """)
    assert out == {"reason": None, "fds": [0, 1, 2]}


def test_launcher_waits_for_go():
    """GO が来るまで launcher は exec しない。"""
    proc, sock, nonce = _raw_launcher()
    try:
        assert _recv_frame(sock, nonce).type == P.HELLO
        sock.sendmsg([P.encode_joined(nonce)])
        assert _recv_frame(sock, nonce).type == P.PREPARED
        time.sleep(1.0)
        assert proc.poll() is None
        cmdline = Path(f"/proc/{proc.pid}/cmdline").read_bytes()
        assert L.LAUNCHER_MODULE.encode() in cmdline, "GO の前に exec した"
    finally:
        sock.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait(10)


class _FakeCapLibc:
    """capget / capset だけを受ける libc の代役。capset で渡された値を以後の capget で返す。
    `drop_capset=True` は capset が成功を返しても値を変えない kernel を模す。"""

    def __init__(self, caps: dict[str, tuple[int, int]], drop_capset: bool = False) -> None:
        self.caps = dict(caps)
        self.drop_capset = drop_capset
        self.capsets: list[dict[str, tuple[int, int]]] = []

    def syscall(self, *args) -> int:
        raise AssertionError("capget / capset は glibc の wrapper で呼ぶ (syscall 番号を持たない)")

    def capget(self, hdr, data) -> int:
        for k, (lo, hi) in self.caps.items():
            setattr(data[0], k, lo)
            setattr(data[1], k, hi)
        return 0

    def capset(self, hdr, data) -> int:
        new = {k: (getattr(data[0], k), getattr(data[1], k)) for k in self.caps}
        self.capsets.append(new)
        if not self.drop_capset:
            self.caps = new
        return 0


_CAPS = {"effective": (0x5, 0x1), "permitted": (0x7, 0x1), "inheritable": (0x30, 0x2)}


def test_clear_inheritable_caps_clears_only_inheritable(monkeypatch):
    libc = _FakeCapLibc(_CAPS)
    monkeypatch.setattr(cli_launcher, "_libc", lambda: libc)
    cli_launcher.clear_inheritable_caps()
    assert libc.capsets == [{**_CAPS, "inheritable": (0, 0)}]


def test_clear_inheritable_caps_verifies_after_capset(monkeypatch):
    libc = _FakeCapLibc(_CAPS, drop_capset=True)
    monkeypatch.setattr(cli_launcher, "_libc", lambda: libc)
    with pytest.raises(OSError) as e:
        cli_launcher.clear_inheritable_caps()
    assert e.value.errno == errno.EPERM
    assert len(libc.capsets) == 1


def test_capability_calls_carry_no_syscall_numbers():
    """capget / capset の syscall 番号は arch ごとに違うので、module は番号を持たない。"""
    assert not [n for n in vars(cli_launcher) if n.startswith("_SYS_CAP")]
    # 実 kernel への呼び出しは子 process で (試験 process の capability を変えない)
    code = ("from agentic_fx.core import cli_launcher as m\n"
            "m.clear_inheritable_caps()\n"
            "print(m._capabilities()[2])\n")
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout.decode().strip() == "0"
