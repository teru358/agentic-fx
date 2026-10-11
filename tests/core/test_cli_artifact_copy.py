"""CLI の実行 artifact を封印した memfd に写す部品 (launcher が mission cgroup の中で使う)。"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import os

import pytest

from agentic_fx.core import cli_artifact_copy as AC


def _memfds() -> list[str]:
    out = []
    for n in os.listdir("/proc/self/fd"):
        try:
            link = os.readlink(f"/proc/self/fd/{n}")
        except OSError:
            continue
        if link.startswith("/memfd:afx-cli"):
            out.append(link)
    return out


def test_sealed_copy_fixes_the_bytes(tmp_path):
    """写した後に元の file を同じ inode のまま書き換えても、memfd の内容は写した時のまま。
    封印は書込み・伸縮・封印の追加を拒否する。"""
    p = tmp_path / "cli"
    original = b"\x7fELF" + bytes(range(256)) * 9000
    p.write_bytes(original)
    st = os.stat(p)
    fd = os.open(p, os.O_RDONLY | os.O_CLOEXEC)
    try:
        memfd = AC.sealed_copy(fd)
    finally:
        os.close(fd)
    try:
        assert AC.sha256_fd(memfd) == hashlib.sha256(original).hexdigest()
        assert fcntl.fcntl(memfd, AC.F_GET_SEALS) & 0xF == 0xF
        assert os.get_inheritable(memfd) is False
        assert os.readlink(f"/proc/self/fd/{memfd}").startswith("/memfd:afx-cli")
        with open(p, "r+b") as f:
            f.seek(100)
            f.write(b"\xff" * 64)
        assert os.stat(p).st_ino == st.st_ino and p.read_bytes() != original
        assert os.pread(memfd, len(original) + 1, 0) == original
        for op in (lambda: os.pwrite(memfd, b"x", 0), lambda: os.ftruncate(memfd, 1),
                   lambda: os.ftruncate(memfd, len(original) + 1),
                   lambda: fcntl.fcntl(memfd, AC.F_ADD_SEALS, 0x10)):
            with pytest.raises(OSError):
                op()
    finally:
        os.close(memfd)


def test_sealed_copy_refuses_a_changing_file(tmp_path, monkeypatch):
    p = tmp_path / "cli"
    p.write_bytes(b"\x7fELF" + b"A" * 100)
    fd = os.open(p, os.O_RDONLY | os.O_CLOEXEC)
    real_fstat = os.fstat
    calls = {"n": 0}

    def fstat(f):
        st = real_fstat(f)
        if f == fd:
            calls["n"] += 1
            if calls["n"] > 1:
                os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1))
                return real_fstat(f)
        return st

    monkeypatch.setattr(AC.os, "fstat", fstat)
    try:
        with pytest.raises(AC.ArtifactCopyChanged):
            AC.sealed_copy(fd)
    finally:
        os.close(fd)
    assert _memfds() == []


def test_sha256_fd_does_not_move_the_offset(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(b"abc" * 1000)
    fd = os.open(p, os.O_RDONLY)
    try:
        os.lseek(fd, 7, os.SEEK_SET)
        assert AC.sha256_fd(fd) == hashlib.sha256(b"abc" * 1000).hexdigest()
        assert os.lseek(fd, 0, os.SEEK_CUR) == 7
    finally:
        os.close(fd)


# --- memfd_create: os の関数が無い build では syscall を直接呼ぶ -------------------------

def test_memfd_create_uses_os_when_available(monkeypatch):
    seen = []

    def fake(name, flags):
        seen.append((name, flags))
        return 1234

    monkeypatch.setattr(AC.os, "memfd_create", fake, raising=False)
    monkeypatch.setattr(AC, "_libc", lambda: pytest.fail("libc must not be used"))
    assert AC.memfd_create(b"afx-cli", 3) == 1234
    assert seen == [("afx-cli", 3)]


def test_memfd_create_by_syscall_on_this_host(monkeypatch):
    monkeypatch.delattr(AC.os, "memfd_create", raising=False)
    fd = AC.memfd_create(b"afx-cli", AC.MFD_CLOEXEC | AC.MFD_ALLOW_SEALING)
    try:
        assert os.readlink(f"/proc/self/fd/{fd}").startswith("/memfd:afx-cli")
        assert os.get_inheritable(fd) is False
    finally:
        os.close(fd)


def test_memfd_create_without_libc_syscall_is_oserror(monkeypatch):
    """libc に syscall の属性が無くても AttributeError は漏れず OSError になる。"""
    monkeypatch.delattr(AC.os, "memfd_create", raising=False)
    monkeypatch.setattr(AC, "_libc", lambda: object())
    with pytest.raises(OSError) as ei:
        AC.memfd_create(b"afx-cli", 0)
    assert ei.value.errno == errno.ENOSYS
    with pytest.raises(OSError):
        AC.exec_memfd()


def test_memfd_create_on_an_unknown_arch_is_oserror(monkeypatch):
    monkeypatch.delattr(AC.os, "memfd_create", raising=False)
    monkeypatch.setattr(AC, "_machine", lambda: "riscv64")
    monkeypatch.setattr(AC, "_libc", lambda: pytest.fail("no syscall on unknown arch"))
    with pytest.raises(OSError) as ei:
        AC.memfd_create(b"afx-cli", 0)
    assert ei.value.errno == errno.ENOSYS


@pytest.mark.parametrize("machine,nr", [("x86_64", 319), ("aarch64", 279)])
def test_memfd_create_syscall_number_and_errno(monkeypatch, machine, nr):
    """arch ごとの固定番号で呼び、失敗の errno を OSError にする。"""
    calls = []

    class Syscall:
        restype = None

        def __call__(self, *args):
            calls.append(args[0].value)
            ctypes.set_errno(errno.EMFILE)
            return -1

    class FakeLibc:
        syscall = Syscall()

    monkeypatch.delattr(AC.os, "memfd_create", raising=False)
    monkeypatch.setattr(AC, "_machine", lambda: machine)
    fake = FakeLibc()
    monkeypatch.setattr(AC, "_libc", lambda: fake)
    with pytest.raises(OSError) as ei:
        AC.memfd_create(b"afx-cli", 0)
    assert ei.value.errno == errno.EMFILE and calls == [nr]


def test_exec_memfd_retries_without_mfd_exec_on_einval(monkeypatch):
    seen = []

    def fake(name, flags):
        seen.append(flags)
        if flags & AC.MFD_EXEC:
            raise OSError(errno.EINVAL, "old kernel")
        return 99

    monkeypatch.setattr(AC, "memfd_create", fake)
    assert AC.exec_memfd() == 99
    base = AC.MFD_CLOEXEC | AC.MFD_ALLOW_SEALING
    assert seen == [base | AC.MFD_EXEC, base]
