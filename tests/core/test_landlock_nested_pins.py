"""nested Landlock の組み立て・失敗経路・正規形を、kernel 呼び出しを記録する
偽の libc で固定する。

実 kernel での効き目は test_landlock_nested.py が子 process で見る。ここでは
「kernel に何を渡すか」「失敗をどう返すか」を見る。偽の libc は syscall を
記録するだけで実際には何も掛けないので、pytest 本体で apply を呼んでよい。
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import os

import pytest

from agentic_fx.core import landlock_nested as N

_CREATE = 444
_RESTRICT = 446
_PR_SET_NO_NEW_PRIVS = 38

GOLDEN_CANONICAL = (b'{"handled_fs":0,"handled_net":3,"handled_scope":3,'
                    b'"min_abi":8,"restrict_flags":8,"rules":[]}')
GOLDEN_DIGEST = "c818477092c89ae4224d64551ae3fe14e25b35c66c91ed5cb10312e651fba3a5"


class FakeLibc:
    """landlock の syscall と prctl を記録する。失敗させたい段は errno を渡す。"""

    def __init__(self, abi: int = 8, version_errno: int | None = None,
                 create_errno: int | None = None, nnp_errno: int | None = None,
                 restrict_errno: int | None = None) -> None:
        self.abi = abi
        self.version_errno = version_errno
        self.create_errno = create_errno
        self.nnp_errno = nnp_errno
        self.restrict_errno = restrict_errno
        self.calls: list[tuple] = []
        self.fds: list[int] = []

    def _fail(self, e: int) -> int:
        ctypes.set_errno(e)
        return -1

    def syscall(self, nr, *args):
        nr = nr.value
        if nr == _CREATE and args[0] is None:
            self.calls.append(("version", args[1].value, args[2].value))
            if self.version_errno is not None:
                return self._fail(self.version_errno)
            return self.abi
        if nr == _CREATE:
            a = args[0]._obj
            self.calls.append(("create", (a.handled_access_fs, a.handled_access_net,
                                          a.scoped), args[1].value, args[2].value))
            if self.create_errno is not None:
                return self._fail(self.create_errno)
            fd = os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC)
            self.fds.append(fd)
            return fd
        if nr == _RESTRICT:
            self.calls.append(("restrict", args[0].value, args[1].value))
            if self.restrict_errno is not None:
                return self._fail(self.restrict_errno)
            return 0
        raise AssertionError(f"unexpected syscall {nr}")

    def prctl(self, option, *args):
        self.calls.append(("prctl", option, *args))
        if self.nnp_errno is not None:
            return self._fail(self.nnp_errno)
        return 0

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]

    def all_fds_closed(self) -> bool:
        for fd in self.fds:
            try:
                os.fstat(fd)
            except OSError:
                continue
            os.close(fd)
            return False
        return True


@pytest.fixture
def fake(monkeypatch):
    def make(**kw) -> FakeLibc:
        f = FakeLibc(**kw)
        monkeypatch.setattr(N, "_libc", lambda: f)
        return f
    return make


# --------------------------------------------------------------------------
# 成功経路: kernel に渡す値と順序
# --------------------------------------------------------------------------

def test_apply_passes_exactly_the_canonical_bits_in_order(fake):
    f = fake()
    rs = N.apply_nested_landlock()
    assert rs == N.NESTED_RULESET
    assert f.kinds() == ["version", "create", "prctl", "restrict"]
    _, attr, size, flags = f.calls[1]
    # FS は handled にしない。net は BIND と CONNECT、scope は ABSTRACT と SIGNAL
    assert attr == (0, 0b11, 0b11)
    assert (size, flags) == (24, 0)
    assert f.calls[2] == ("prctl", _PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    assert f.calls[3] == ("restrict", f.fds[0], 1 << 3)
    assert f.all_fds_closed()


# --------------------------------------------------------------------------
# 失敗経路: 固定 reason、detail、再試行しない、fd を閉じる
# --------------------------------------------------------------------------

@pytest.mark.parametrize("abi", [0, 6, 7])
def test_apply_on_old_abi_fails_before_touching_the_kernel(fake, abi):
    f = fake(abi=abi)
    with pytest.raises(N.NestedLandlockError) as ei:
        N.apply_nested_landlock()
    assert ei.value.reason == "landlock_abi_too_old"
    assert f"abi={abi}" in ei.value.detail
    assert f.kinds() == ["version"]


def test_create_failure_is_reported_and_stops(fake):
    f = fake(create_errno=errno.EINVAL)
    with pytest.raises(N.NestedLandlockError) as ei:
        N.apply_nested_landlock()
    assert ei.value.reason == "landlock_create_failed"
    assert f"errno={errno.EINVAL}" in ei.value.detail
    assert f.kinds() == ["version", "create"]


def test_no_new_privs_failure_is_reported_and_restrict_is_not_called(fake):
    f = fake(nnp_errno=errno.EPERM)
    with pytest.raises(N.NestedLandlockError) as ei:
        N.apply_nested_landlock()
    assert ei.value.reason == "no_new_privs_failed"
    assert f"errno={errno.EPERM}" in ei.value.detail
    assert f.kinds() == ["version", "create", "prctl"]
    assert f.all_fds_closed()


@pytest.mark.parametrize("err", [errno.EINVAL, errno.EOPNOTSUPP, errno.EPERM])
def test_restrict_failure_is_not_retried_without_tsync(fake, err):
    f = fake(restrict_errno=err)
    with pytest.raises(N.NestedLandlockError) as ei:
        N.apply_nested_landlock()
    assert ei.value.reason == "landlock_restrict_failed"
    assert f"errno={err}" in ei.value.detail
    restricts = [c for c in f.calls if c[0] == "restrict"]
    assert restricts == [("restrict", f.fds[0], 1 << 3)]
    assert f.all_fds_closed()


def test_reasons_are_exactly_the_four_fixed_words():
    assert N.NESTED_REASONS == (
        "landlock_abi_too_old", "landlock_create_failed",
        "no_new_privs_failed", "landlock_restrict_failed")


# --------------------------------------------------------------------------
# ABI probe: 失敗と非 x86_64 は 0
# --------------------------------------------------------------------------

@pytest.mark.parametrize("err", [errno.ENOSYS, errno.EOPNOTSUPP])
def test_probe_failure_reports_abi_zero(fake, err):
    fake(version_errno=err)
    assert N.probe_abi() == 0


@pytest.mark.parametrize("machine", ["aarch64", "i686", "riscv64", ""])
def test_probe_on_other_arch_is_zero_without_syscall(fake, monkeypatch, machine):
    f = fake(abi=8)
    monkeypatch.setattr(N.platform, "machine", lambda: machine)
    assert N.probe_abi() == 0
    assert f.calls == []


@pytest.mark.parametrize("abi", [10, 16, 2**31 - 1])
def test_newer_abi_keeps_the_same_ruleset(abi):
    assert N.ruleset_for_abi(abi) == N.NESTED_RULESET


# --------------------------------------------------------------------------
# 正規形と digest の固定値
# --------------------------------------------------------------------------

def test_canonical_form_and_digest_are_fixed_bytes():
    assert N.canonical_form(N.NESTED_RULESET) == GOLDEN_CANONICAL
    assert N.ruleset_digest(N.NESTED_RULESET) == GOLDEN_DIGEST
    assert hashlib.sha256(GOLDEN_CANONICAL).hexdigest() == GOLDEN_DIGEST
