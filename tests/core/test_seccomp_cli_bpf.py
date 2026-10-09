"""`improve-cli/1` の表から作った BPF と data evaluator の一致。

BPF は生成器を import しない参照 interpreter (`_bpf_reference`) で走らせる。
条件つき 12 個の引数の分割表と期待値は、仕様の文言からこのファイルで独立に書く。
"""
from __future__ import annotations

import errno
import hashlib
import os
import struct
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine != "x86_64",
    reason="improve-cli/1 は x86_64 Linux 専用")

from agentic_fx.core import seccomp_cli_bpf as B  # noqa: E402
from agentic_fx.core import seccomp_cli_policy as P  # noqa: E402
from tests.core import _bpf_reference as R  # noqa: E402

X86_64 = R.AUDIT_ARCH_X86_64
HI = 1 << 32
MAX_U64 = (1 << 64) - 1

RESULTS = {
    "ALLOW": R.RET_ALLOW,
    "EPERM": R.ret_errno(errno.EPERM),
    "EACCES": R.ret_errno(errno.EACCES),
    "EAFNOSUPPORT": R.ret_errno(errno.EAFNOSUPPORT),
    "EINVAL": R.ret_errno(errno.EINVAL),
    "ENOTTY": R.ret_errno(errno.ENOTTY),
}


def a(**kw) -> tuple[int, ...]:
    out = [0] * 6
    for k, v in kw.items():
        out[int(k[1:])] = v
    return tuple(out)


def _single_bits():
    return [1 << b for b in range(32)]


# --- 条件つき 12 個の分割表 (引数, 期待) ---------------------------------------

AF_UNIX, AF_INET, AF_INET6 = 1, 2, 10
SOCK_NONBLOCK, SOCK_CLOEXEC = 0x800, 0x80000


def _socket():
    p = []
    for fam in (AF_INET, AF_INET6):
        for proto in (0, 6):
            p.append((a(a0=fam, a1=1, a2=proto), "ALLOW"))
        p.append((a(a0=fam, a1=1 | SOCK_NONBLOCK | SOCK_CLOEXEC, a2=0), "ALLOW"))
        for t in range(16):
            p.append((a(a0=fam, a1=t), "ALLOW" if t == 1 else "EACCES"))
        for proto in (1, 5, 7, 17, 0xFFFFFFFF, HI | 17):
            p.append((a(a0=fam, a1=1, a2=proto), "EACCES"))
        # int 引数は下位 32 bit で判定する
        p.append((a(a0=fam | (0xDEAD * HI), a1=1 | HI, a2=6 | HI), "ALLOW"))
        p.append((a(a0=fam, a1=1, a2=HI), "ALLOW"))
    for b in range(4, 32):
        p.append((a(a0=AF_INET, a1=1 | (1 << b)), "ALLOW"))
    for t in (1, 2, 5, 3):
        p.append((a(a0=AF_UNIX, a1=t), "EACCES"))
    p.append((a(a0=AF_UNIX | HI, a1=1), "EACCES"))
    for fam in (0, 3, 9, 11, 16, 17, 40, 0xFFFFFFFF, HI, HI | 0xFFFF):
        p.append((a(a0=fam, a1=1, a2=6), "EAFNOSUPPORT"))
    return p


def _socketpair():
    p = [(a(a0=AF_UNIX, a1=1), "ALLOW"), (a(a0=AF_UNIX | HI, a1=2), "ALLOW")]
    for fam in (0, 2, 10, 16, HI):
        p.append((a(a0=fam, a1=1), "EAFNOSUPPORT"))
    return p


CLONE_FORBIDDEN = 0x7E028080


def _clone():
    p = [(a(a0=0), "ALLOW"), (a(a0=17), "ALLOW"), (a(a0=0x100000 | 17), "ALLOW"),
         (a(a0=0x100 | 0x800 | 0x10000 | 0x100000, a1=5, a2=6, a3=7, a4=8), "ALLOW"),
         (a(a0=0x8000 | 17), "EPERM"), (a(a0=0x10000000 | 0x20000), "EPERM"),
         (a(a0=HI | 0x10000000), "EPERM"), (a(a0=1 << 40), "ALLOW"), (a(a0=1 << 63), "ALLOW")]
    for bit in _single_bits():
        p.append((a(a0=bit), "EPERM" if bit & CLONE_FORBIDDEN else "ALLOW"))
        p.append((a(a0=bit | 17 | HI), "EPERM" if bit & CLONE_FORBIDDEN else "ALLOW"))
    return p


def _prlimit64():
    p = [(a(a0=0, a1=7, a2=0x1000, a3=0x2000), "ALLOW"), (a(a0=0), "ALLOW")]
    for pid in (1, 2, 0xFFFFFFFF, HI, HI | 1, MAX_U64, 1 << 63):
        p.append((a(a0=pid, a1=7), "EPERM"))
    return p


MEMBARRIER_OK = {0, 8, 16, 32, 64, 128, 256}


def _membarrier():
    p = []
    for cmd in sorted(MEMBARRIER_OK | {1, 2, 4, 512, 7, 9, 15, 17, 24, 0xFFFFFFFF}):
        p.append((a(a0=cmd), "ALLOW" if cmd in MEMBARRIER_OK else "EPERM"))
    for bit in _single_bits():
        p.append((a(a0=bit, a1=1), "ALLOW" if bit in MEMBARRIER_OK else "EPERM"))
    p.append((a(a0=8 | HI), "ALLOW"))
    p.append((a(a0=1 | HI), "EPERM"))
    for ok in sorted(MEMBARRIER_OK):
        for d in (-1, 1):
            v = (ok + d) & 0xFFFFFFFF
            p.append((a(a0=v), "ALLOW" if v in MEMBARRIER_OK else "EPERM"))
    return p


def _personality():
    p = [(a(a0=0xFFFFFFFF), "ALLOW"), (a(a0=0xFFFFFFFF | 5 * HI), "ALLOW")]
    for v in (0, 0xFFFFFFFE, 0x0400000, 8, HI, 0x7FFFFFFF):
        p.append((a(a0=v), "EPERM"))
    for bit in _single_bits():
        p.append((a(a0=0xFFFFFFFF ^ bit), "EPERM"))
    return p


MSG_FASTOPEN = 0x20000000


def _flags_partition(flag_arg: int, extra: dict | None = None):
    extra = extra or {}
    p = []
    for bit in _single_bits():
        p.append((a(**{f"a{flag_arg}": bit}, **extra), "EPERM" if bit == MSG_FASTOPEN else "ALLOW"))
        p.append((a(**{f"a{flag_arg}": bit | HI}, **extra),
                  "EPERM" if bit == MSG_FASTOPEN else "ALLOW"))
    for v in (0, 0x4000, 0x1FFFFFFF, 0xDFFFFFFF, HI * 0x20000000):
        p.append((a(**{f"a{flag_arg}": v}, **extra), "ALLOW"))
    for v in (MSG_FASTOPEN | 0x4000, 0x20000001, 0xFFFFFFFF, MSG_FASTOPEN | HI):
        p.append((a(**{f"a{flag_arg}": v}, **extra), "EPERM"))
    return p


def _sendto():
    p = _flags_partition(3, {"a1": 0x1000, "a2": 10})
    p.append((a(a1=0x1000, a2=10, a3=0, a4=0, a5=16), "ALLOW"))
    for dest in (1, 0x1000, HI, HI | 0x1000, MAX_U64, 1 << 63):
        p.append((a(a1=0x1000, a2=10, a4=dest, a5=16), "EPERM"))
        p.append((a(a3=0x4000, a4=dest), "EPERM"))
    p.append((a(a3=MSG_FASTOPEN, a4=0x1000), "EPERM"))
    return p


def _sendmsg():
    return _flags_partition(2, {"a1": 0x1000})


def _sendmmsg():
    return _flags_partition(3, {"a1": 0x1000, "a2": 4})


PRCTL_OK = {15, 16, 41, 42, 1, 2, 38, 39, 21, 23, 3, 37}
PR_SET_VMA = 0x53564D41


def _prctl():
    p = []
    for v in sorted(PRCTL_OK):
        p.append((a(a0=v, a1=1), "ALLOW"))
        p.append((a(a0=v | HI), "ALLOW"))
        for d in (-1, 1):
            w = (v + d) & 0xFFFFFFFF
            p.append((a(a0=w), "ALLOW" if w in PRCTL_OK else "EINVAL"))
    p.append((a(a0=PR_SET_VMA, a1=0, a2=0x1000), "ALLOW"))
    p.append((a(a0=PR_SET_VMA | HI, a1=0), "ALLOW"))
    for sub in (1, 2, HI, HI | 0, MAX_U64):
        p.append((a(a0=PR_SET_VMA, a1=sub), "EINVAL" if sub else "ALLOW"))
    for v in (PR_SET_VMA - 1, PR_SET_VMA + 1):
        p.append((a(a0=v), "EINVAL"))
    for v in (62, 0x59616D61, 35, 4, 36, 28, 47, 53, 0, 0xFFFFFFFF):
        p.append((a(a0=v), "EINVAL"))
    return p


FCNTL_OK = {0, 1030, 1, 2, 3, 4, 5, 6, 1034, 1033, 1032}


def _fcntl():
    p = []
    for v in sorted(FCNTL_OK):
        p.append((a(a0=3, a1=v), "ALLOW"))
        p.append((a(a0=3, a1=v | HI), "ALLOW"))
        for d in (-1, 1):
            w = (v + d) & 0xFFFFFFFF
            p.append((a(a0=3, a1=w), "ALLOW" if w in FCNTL_OK else "EINVAL"))
    for v in (7, 36, 37, 38, 8, 9, 10, 15, 16, 1024, 1026, 1031, 0xFFFFFFFF):
        p.append((a(a0=3, a1=v), "EINVAL"))
    return p


IOCTL_OK = {0x5401, 0x802C542A, 0x5413, 0x540F, 0x80045430, 0x5421, 0x541B, 0x5451, 0x5450,
            0x40049409, 0x4020940D}


def _ioctl():
    p = []
    for v in sorted(IOCTL_OK):
        p.append((a(a0=1, a1=v), "ALLOW"))
        p.append((a(a0=1, a1=v | HI, a2=0x1000), "ALLOW"))
        for d in (-1, 1):
            w = (v + d) & 0xFFFFFFFF
            p.append((a(a0=1, a1=w), "ALLOW" if w in IOCTL_OK else "ENOTTY"))
    for v in (0x5412, 0x540E, 0x8901, 0x40086602, 0x5423, 0x541C, 0xAE01, 0, 0xFFFFFFFF):
        p.append((a(a0=1, a1=v), "ENOTTY"))
    return p


PARTITIONS = {
    "socket": _socket(), "socketpair": _socketpair(), "clone": _clone(),
    "prlimit64": _prlimit64(), "membarrier": _membarrier(), "personality": _personality(),
    "sendto": _sendto(), "sendmsg": _sendmsg(), "sendmmsg": _sendmmsg(), "prctl": _prctl(),
    "fcntl": _fcntl(), "ioctl": _ioctl(),
}

# x86_64 の番号 (仕様の表から独立に書く)
COND_NR = {"socket": 41, "socketpair": 53, "clone": 56, "prlimit64": 302, "membarrier": 324,
           "personality": 135, "sendto": 44, "sendmsg": 46, "sendmmsg": 307, "prctl": 157,
           "fcntl": 72, "ioctl": 16}


@pytest.fixture(scope="module")
def compiled():
    return B.compile_policy()


# --- 受入 26 ----------------------------------------------------------------

def test_every_number_agrees_between_table_and_bpf(compiled):
    prog = compiled.program
    R.validate(prog)
    for nr in range(472):
        assert R.run(prog, nr=nr) == P.evaluate(X86_64, nr, (0,) * 6), nr


def test_x32_and_foreign_arch_are_kill_process(compiled):
    prog = compiled.program
    for arch in (R.AUDIT_ARCH_I386, R.AUDIT_ARCH_AARCH64, 0, X86_64 - 1, X86_64 + 1,
                 X86_64 & 0x7FFFFFFF):
        for nr in (0, 39, 41, 56, 435, 471):
            assert R.run(prog, nr=nr, arch=arch) == R.RET_KILL_PROCESS
            assert P.evaluate(arch, nr, (0,) * 6) == R.RET_KILL_PROCESS
    for nr in range(472):
        x = nr | 0x40000000
        assert R.run(prog, nr=x) == R.RET_KILL_PROCESS, nr
        assert P.evaluate(X86_64, x, (0,) * 6) == R.RET_KILL_PROCESS, nr
    for nr in (0x40000000, 0x40000000 + 512, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF):
        assert R.run(prog, nr=nr) == R.RET_KILL_PROCESS
        assert P.evaluate(X86_64, nr, (0,) * 6) == R.RET_KILL_PROCESS


def test_numbers_at_and_above_ceiling_are_enosys(compiled):
    prog = compiled.program
    enosys = R.ret_errno(errno.ENOSYS)
    for nr in (472, 473, 512, 1000, 0x3FFFFFFF):
        assert R.run(prog, nr=nr) == enosys
        assert P.evaluate(X86_64, nr, (0,) * 6) == enosys
    assert R.run(prog, nr=0xFFFFFFFF) == P.evaluate(X86_64, 0xFFFFFFFF, (0,) * 6) \
        == R.RET_KILL_PROCESS
    # seccomp_data.nr は 32 bit。上位 bit を持つ値は両方とも下位 32 bit で判定する
    for nr in ((1 << 32) | 39, (1 << 32) | 472, (1 << 33) | 0x40000000):
        assert R.run(prog, nr=nr) == P.evaluate(X86_64, nr, (0,) * 6)
    assert R.run(prog, nr=471) == P.evaluate(X86_64, 471, (0,) * 6)


@pytest.mark.parametrize("name", sorted(PARTITIONS))
def test_conditional_partitions_agree(compiled, name):
    assert P.SNAPSHOT_NUMBERS[name] == COND_NR[name]
    nr = COND_NR[name]
    rows = PARTITIONS[name]
    assert len(rows) >= 5
    seen = set()
    for args, want in rows:
        exp = RESULTS[want]
        got_data = P.evaluate(X86_64, nr, args)
        got_bpf = R.run(compiled.program, nr=nr, args=args)
        assert got_data == exp, (name, [hex(x) for x in args], want)
        assert got_bpf == exp, (name, [hex(x) for x in args], want)
        seen.add(want)
    assert "ALLOW" in seen and len(seen) >= 2


# --- 生成物の組 --------------------------------------------------------------

def test_compiled_policy_carries_profile_hashes_and_flags(compiled):
    assert compiled.profile_id == "improve-cli/1"
    assert compiled.policy_hash == P.policy_table().policy_hash
    blob = b"".join(struct.pack("<HBBI", *ins) for ins in compiled.program)
    assert B.program_bytes(compiled.program) == blob
    assert compiled.bpf_hash == hashlib.sha256(blob).hexdigest()
    assert compiled.filter_flags == 1 | 8 | 16 | 32
    assert compiled.filter_flags == P.FILTER_FLAGS


def test_generation_is_deterministic(compiled):
    again = B.compile_policy()
    assert again.program == compiled.program
    assert again.bpf_hash == compiled.bpf_hash


BPF_HASH = "4447e977022180ef5952392a1b76b05b279316da511a87be0d70a0163f4aafbd"


def test_bpf_hash_is_pinned(compiled):
    assert compiled.bpf_hash == BPF_HASH


def test_program_fits_kernel_limits(compiled):
    prog = compiled.program
    assert len(prog) <= 4096
    for code, jt, jf, _k in prog:
        assert 0 <= jt <= 255 and 0 <= jf <= 255
    R.validate(prog)


def test_every_instruction_is_reached(compiled):
    prog = compiled.program
    reached = set()

    def trace(nr, arch=X86_64, args=(0,) * 6):
        data = R.seccomp_data(nr, arch, args)
        acc, pc = 0, 0
        while True:
            reached.add(pc)
            code, jt, jf, k = prog[pc]
            if code == R.LD_W_ABS:
                acc = struct.unpack_from("<I", data, k)[0]
                pc += 1
            elif code == R.ALU_AND_K:
                acc &= k
                pc += 1
            elif code == R.JA:
                pc += 1 + k
            elif code == R.JEQ_K:
                pc += 1 + (jt if acc == k else jf)
            elif code == R.JGE_K:
                pc += 1 + (jt if acc >= k else jf)
            elif code == R.JGT_K:
                pc += 1 + (jt if acc > k else jf)
            elif code == R.JSET_K:
                pc += 1 + (jt if acc & k else jf)
            else:
                return

    trace(0, arch=R.AUDIT_ARCH_I386)
    trace(0x40000000)
    trace(472)
    for nr in range(472):
        trace(nr)
    for name, rows in PARTITIONS.items():
        for args, _w in rows:
            trace(COND_NR[name], args=args)
    assert reached == set(range(len(prog)))


def test_assembler_rejects_out_of_range_jump_and_oversized_program():
    with pytest.raises(P.PolicyBuildError) as ei:
        B.check_program([(R.JEQ_K, 300, 0, 1)] + [(R.RET_K, 0, 0, 0)] * 400)
    assert ei.value.reason == "jump_out_of_range"
    with pytest.raises(P.PolicyBuildError) as ei:
        B.check_program([(R.JEQ_K, 5, 0, 1), (R.RET_K, 0, 0, 0)])
    assert ei.value.reason == "jump_out_of_range"
    with pytest.raises(P.PolicyBuildError) as ei:
        B.check_program([(R.RET_K, 0, 0, 0)] * 4097)
    assert ei.value.reason == "too_many_instructions"


def test_far_branches_are_relayed_through_ja():
    # ALLOW と EPERM を番号ごとに交互にした表は範囲が 400 個を超え、左の部分木が
    # 255 命令を超える節ができる。JA の中継で全番号が表どおりに届くことを見る
    cls = {k: set(v) for k, v in P.CLASSIFICATION.items()}
    by_nr = {nr: name for name, nr in P.SNAPSHOT_NUMBERS.items()}
    for nr, name in by_nr.items():
        if name in cls["ALLOW"] or name in cls["EPERM"]:
            cls["ALLOW"].discard(name)
            cls["EPERM"].discard(name)
            cls["ALLOW" if nr % 2 else "EPERM"].add(name)
    table = P.build_table(classification=cls)
    compiled = B.compile_policy(table)
    prog = compiled.program
    assert any(ins[0] == R.JA for ins in prog)
    assert len(prog) <= 4096
    R.validate(prog)
    for nr in range(472):
        assert R.run(prog, nr=nr) == P.evaluate(X86_64, nr, (0,) * 6, table), nr
    for name, rows in PARTITIONS.items():
        for args, _w in rows:
            nr = COND_NR[name]
            assert R.run(prog, nr=nr, args=args) == P.evaluate(X86_64, nr, args, table)
