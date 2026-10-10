"""CLI の subreaper: 孤児と zombie の引取り先が CLI であり、worker でも 1 でもないこと。

全部、実 launcher で exec した実 process と実 kernel で観測する。CLI 役の
`_subreaper_probe.py` が作った孤児・zombie の PPid を /proc で読む。
"""
from __future__ import annotations

import ctypes
import dataclasses
import json
import os
import select
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentic_fx.core import cli_launch as L
from agentic_fx.core import cli_launch_protocol as P
from tests.core import test_cli_launch as base
from tests.core.test_cli_launch import PY, _wrapped_launcher

_kernel_ready = base._kernel_ready

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine != "x86_64",
    reason="improve-cli の launcher は x86_64 Linux 専用")

PROBE = Path(__file__).with_name("_subreaper_probe.py")
YAMA = Path("/proc/sys/kernel/yama/ptrace_scope")


@pytest.fixture
def h():
    harness = base._Harness()
    yield harness
    harness.close()


def ppid_of(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def children_of(ppid: int) -> list[int]:
    out = []
    for d in os.listdir("/proc"):
        if d.isdigit() and ppid_of(int(d)) == ppid:
            out.append(int(d))
    return sorted(out)


class Lines:
    """probe の stdout を 1 行 1 JSON で読む。孤児が pipe を持ち続けるので EOF は待たない。"""

    def __init__(self, fd: int) -> None:
        self.fd, self.buf = fd, b""

    def read(self, want: int, timeout: float = 10.0) -> list[dict]:
        out, end = [], time.monotonic() + timeout
        while len(out) < want and time.monotonic() < end:
            while b"\n" in self.buf and len(out) < want:
                line, self.buf = self.buf.split(b"\n", 1)
                out.append(json.loads(line))
            if len(out) >= want:
                break
            if select.select([self.fd], [], [], 0.1)[0]:
                data = os.read(self.fd, 4096)
                if not data:
                    break
                self.buf += data
        assert len(out) == want, out
        return out


class Probe:
    """CLI 役として probe を exec し、終了時に孤児を含めて pid 指定で止める。"""

    def __init__(self, h, mode: str, *args: str, **kw) -> None:
        self.h = h
        self.pids: list[int] = []
        kw.setdefault("stdout", subprocess.PIPE)
        self.result = h.launch(
            cli=PY, argv=[PY, "-I", "-S", "-c", PROBE.read_text(), mode, *args],
            handoff=os.close, **kw)
        assert self.result.started, self.result
        self.proc = self.result.launcher_proc
        self.lines = Lines(self.proc.stdout.fileno())

    def track(self, *pids: int) -> None:
        self.pids.extend(pids)

    def stop(self) -> None:
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)


@pytest.fixture
def probe(h):
    made: list[Probe] = []

    def make(mode, *args, **kw):
        p = Probe(h, mode, *args, **kw)
        made.append(p)
        return p

    yield make
    for p in made:
        p.stop()


def _orphans(p: Probe, count: int) -> tuple[int, list[dict]]:
    first = p.lines.read(1)[0]
    assert first["role"] == "probe"
    p.track(first["pid"])
    rows = p.lines.read(count + 1)
    orphans = [r for r in rows if r["role"] == "orphan"]
    assert len(orphans) == count and any(r["role"] == "probe_done" for r in rows)
    p.track(*[o["pid"] for o in orphans])
    return first["pid"], orphans


def test_launcher_becomes_cli_with_same_pid(probe):
    p = probe("prctl")
    assert p.lines.read(1)[0]["role"] == "prctl"
    assert p.proc.pid == p.result.launcher_proc.pid


def test_double_fork_orphan_is_adopted_by_cli(probe):
    p = probe("orphans", "double_fork", "1")
    cli, (o,) = _orphans(p, 1)
    assert ppid_of(o["pid"]) == cli != os.getpid()


def test_setsid_orphan_is_adopted_by_cli(probe):
    p = probe("orphans", "setsid", "1")
    cli, (o,) = _orphans(p, 1)
    assert ppid_of(o["pid"]) == cli != os.getpid()
    assert os.getsid(o["pid"]) == o["pid"]


def test_clone_parent_is_eperm_and_parent_settid_allowed(probe):
    """CLONE_PARENT は EPERM (worker を親にする経路は閉じる)。thread 生成に要る
    CLONE_PARENT_SETTID と通常の fork 相当、threading.Thread は通る。"""
    p = probe("clone")
    (row,) = p.lines.read(1)
    assert row["clone_parent"] == "EPERM", row
    assert row["parent_settid"] == "ok" and row["fork_like"] == "ok", row
    assert row["thread"] == "ok", row


def test_cli_cannot_change_subreaper(probe):
    """CLI 側の PR_SET_CHILD_SUBREAPER は 0 にも 1 にも EINVAL。subreaper を外せず、
    孤児が worker へ上がる経路は閉じている。PR_GET は 1 を返す。"""
    p = probe("prctl")
    (row,) = p.lines.read(1)
    assert (row["set0"], row["set1"]) == ("EINVAL", "EINVAL"), row
    assert row["get"] == [0, 1], row


def test_subreaper_self_check_failure_is_fixed_reason(h):
    """自己検査の読み値が 0 なら subreaper_failed (handoff 前)。読み値だけを差し替える。"""
    r = h.launch(launcher_cmd=_wrapped_launcher("m._subreaper_flag = lambda: 0"))
    assert (r.reason, r.series) == ("subreaper_failed", L.BEFORE_HANDOFF), r
    assert h.listeners == [] and not r.started


def test_prepared_claims_subreaper_and_worker_checks_it():
    """PREPARED は subreaper の申告を持ち、worker の期待値は 1。0 の申告は一致しない。"""
    assert L.expected_prepared().subreaper == 1
    import dataclasses
    bad = dataclasses.replace(L.expected_prepared(), subreaper=0)
    assert bad != L.expected_prepared()
    nonce = bytes(16)
    frame = P.decode(P.encode_prepared(nonce, bad), nonce)
    assert frame.prepared == bad
    assert P.decode(P.encode_prepared(nonce, L.expected_prepared()), nonce).prepared \
        == L.expected_prepared()


def test_worker_child_set_is_launcher_only(probe):
    """孤児を 5 本作っても、worker の直接の子は launcher_proc (= CLI) だけ。"""
    me = os.getpid()
    before = set(children_of(me))
    p = probe("orphans", "double_fork", "5")
    cli, orphans = _orphans(p, 5)
    assert set(children_of(me)) - before == {p.proc.pid}
    assert all(ppid_of(o["pid"]) == cli for o in orphans)


def test_unreaped_zombie_stays_under_cli(probe):
    """CLI が回収しない zombie は CLI の下に残る (pids.max を食う自己妨害として受容)。"""
    p = probe("zombie")
    rows = p.lines.read(2)
    info = {r["role"]: r for r in rows}
    cli = info["probe"]["pid"]
    zombie, grand = info["zombie_child"]["zombie"], info["zombie_child"]["grandchild"]
    p.track(grand)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and ppid_of(grand) != cli:
        time.sleep(0.05)
    assert ppid_of(zombie) == cli and ppid_of(grand) == cli
    assert cli != os.getpid()


def test_zombie_and_orphan_move_to_next_subreaper_when_cli_dies():
    """CLI を殺すと zombie も生きている孫も次の subreaper (別 process の worker 役) に
    移り、reap できる。pytest 自身は subreaper にしない。"""
    out = subprocess.run(
        [sys.executable, str(PROBE), "worker"], capture_output=True, timeout=60,
        preexec_fn=lambda: __import__("resource").setrlimit(
            __import__("resource").RLIMIT_CORE, (0, 0)))
    assert out.returncode == 0, out.stderr.decode()[-500:]
    res = json.loads(out.stdout.decode().strip().splitlines()[-1])
    assert res["started"], res
    assert res["before"] == {"zombie_state": "Z", "zombie_ppid": res["cli"],
                             "grand_ppid": res["cli"]}, res
    assert res["after"]["grand_ppid"] == res["me"], res
    assert res["zombie_reaped"] and res["grand_alive_child"], res


@pytest.mark.skipif(not YAMA.exists() or YAMA.read_text().strip() != "1",
                    reason="Yama ptrace_scope が 1 でない host")
def test_orphan_remains_descendant_of_worker_under_yama1(probe):
    """Yama 1 は ptrace を祖先関係で判定する。引き取られた孤児は worker の子孫の
    ままなので pidfd_getfd が通る (1 や別 subreaper の下なら EPERM になる)。"""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    p = probe("orphans", "setsid", "2")
    cli, orphans = _orphans(p, 2)
    for o in orphans:
        assert ppid_of(o["pid"]) == cli
        pidfd = libc.syscall(ctypes.c_long(434), ctypes.c_int(o["pid"]), ctypes.c_uint(0))
        assert pidfd >= 0, os.strerror(ctypes.get_errno())
        try:
            ctypes.set_errno(0)
            fd = libc.syscall(ctypes.c_long(438), ctypes.c_int(pidfd),
                              ctypes.c_int(o["pipe_fd"]), ctypes.c_uint(0))
            assert fd >= 0, os.strerror(ctypes.get_errno())
            try:
                assert os.fstat(fd).st_ino == o["pipe_ino"]
            finally:
                os.close(fd)
        finally:
            os.close(pidfd)


@pytest.mark.parametrize("value", [0, 2])
def test_worker_rejects_prepared_with_other_subreaper_value(h, value):
    """subreaper の申告が 1 以外なら、他の field が合っていても不一致で listener は閉じる。"""
    before = base._notify_fds()
    r = h.launch(expected=dataclasses.replace(L.expected_prepared(), subreaper=value))
    assert (r.reason, r.series) == (P.ATTESTATION_MISMATCH, L.BEFORE_HANDOFF), r
    assert r.reaped and h.listeners == []
    assert base._notify_fds() == before


@pytest.mark.parametrize("value", [0, 1, 2, 3, 0xFFFFFFFF])
def test_prepared_subreaper_value_round_trips_unmodified(value):
    """decode は申告値を加工しない (2 や 3 を 1 に丸めない)。判定は worker の照合が行う。"""
    nonce = bytes(16)
    p = dataclasses.replace(L.expected_prepared(), subreaper=value)
    assert P.decode(P.encode_prepared(nonce, p), nonce).prepared.subreaper == value


def test_prepared_length_and_version_are_strict():
    nonce = bytes(16)
    data = P.encode_prepared(nonce, L.expected_prepared())
    assert len(data) == 24 + 108
    with pytest.raises(P.MalformedFrame):
        P.decode(data[:-4], nonce)       # version 1 の長さ
    with pytest.raises(P.MalformedFrame):
        P.decode(data + b"\0" * 4, nonce)
    for v in (1, P.VERSION + 1):
        with pytest.raises(P.MalformedFrame):
            P.decode(data[:4] + struct.pack("<H", v) + data[6:], nonce)


def test_subreaper_read_error_is_fixed_reason(h):
    """自己検査の prctl が失敗したら読み値 1 とは扱わず subreaper_failed。"""
    patch = ("def _boom():\n    raise OSError(22, 'x')\nm._subreaper_flag = _boom")
    r = h.launch(launcher_cmd=_wrapped_launcher(patch))
    assert (r.reason, r.series) == ("subreaper_failed", L.BEFORE_HANDOFF), r
    assert h.listeners == []


def test_subreaper_is_read_after_seccomp_install(h):
    """flag の読出しが seccomp filter の下で通ること (CLI 側と同じ条件) を launcher 自身が確かめる。"""
    patch = ("real = m._subreaper_flag\n"
             "m._subreaper_flag = lambda: real() if m._seccomp_mode() == 2 else 0")
    r = h.launch(launcher_cmd=_wrapped_launcher(patch))
    assert r.reason is None and r.started, r
