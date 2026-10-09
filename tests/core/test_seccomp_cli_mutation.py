"""生成 BPF と filter flag に変異を入れ、比較のどれかが必ず落ちることを示す。

検出器は 4 段: flag の pin、kernel 相当の命令列検査、全番号 (引数 0) の比較、
条件つき syscall の分割表と、分割表の引数を全番号に流した比較。期待値は data evaluator
(表) から作り、BPF は参照 interpreter で走らせる。
"""
from __future__ import annotations

import errno
import os
import sys
from collections import Counter

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine != "x86_64",
    reason="improve-cli/1 は x86_64 Linux 専用")

from agentic_fx.core import seccomp_cli_bpf as B  # noqa: E402
from agentic_fx.core import seccomp_cli_policy as P  # noqa: E402
from tests.core import _bpf_reference as R  # noqa: E402
from tests.core.test_seccomp_cli_bpf import COND_NR, PARTITIONS  # noqa: E402

X86_64 = R.AUDIT_ARCH_X86_64
EXPECTED_FLAGS = 0x1 | 0x8 | 0x10 | 0x20  # TSYNC | NEW_LISTENER | TSYNC_ESRCH | WAIT_KILLABLE_RECV
LOG = 0x2

_JUMPS = (R.JEQ_K, R.JGE_K, R.JGT_K, R.JSET_K)
_ERRNOS = (errno.EPERM, errno.EACCES, errno.ENOSYS, errno.EINVAL, errno.ENOTTY,
           errno.EAFNOSUPPORT, errno.ESRCH)
_ZERO = (0,) * 6


class Oracle:
    """表から作った期待値。変異体ごとに安い比較から順に当てる。"""

    def __init__(self) -> None:
        self.sweep = []
        for nr in range(472):
            self.sweep.append((nr, X86_64, _ZERO))
        for nr in (472, 473, 0x3FFFFFFF, 0x40000000, 0x40000000 | 39, 0xFFFFFFFF):
            self.sweep.append((nr, X86_64, _ZERO))
        for arch in (R.AUDIT_ARCH_I386, R.AUDIT_ARCH_AARCH64, X86_64 - 1, X86_64 + 1):
            self.sweep.append((39, arch, _ZERO))
        self.sweep = [(c, P.evaluate(c[1], c[0], c[2])) for c in self.sweep]
        self.own = []
        vectors = set()
        for name, rows in PARTITIONS.items():
            for args, _w in rows:
                self.own.append(((COND_NR[name], X86_64, args),
                                 P.evaluate(X86_64, COND_NR[name], args)))
                vectors.add(args)
        self.vectors = sorted(vectors)
        self._cross = None

    def cross(self):
        # 分割表の引数を全番号に流す。条件 syscall の隣の行へ迷い込む分岐を取る
        if self._cross is None:
            self._cross = [((nr, X86_64, v), P.evaluate(X86_64, nr, v))
                           for v in self.vectors for nr in range(472)]
        return self._cross

    def detect(self, program, flags) -> str | None:
        if flags != EXPECTED_FLAGS:
            return "flag"
        try:
            R.validate(program)
        except R.InvalidProgram:
            return "invalid"
        for stage, cases in (("sweep", self.sweep), ("partition", self.own)):
            for (nr, arch, args), want in cases:
                try:
                    got = R.run(program, nr=nr, arch=arch, args=args)
                except R.RuntimeFault:
                    return "invalid"
                if got != want:
                    return stage
        for (nr, arch, args), want in self.cross():
            if R.run(program, nr=nr, arch=arch, args=args) != want:
                return "cross"
        return None


def _set(prog, i, *, code=None, jt=None, jf=None, k=None):
    c, t, f, kk = prog[i]
    p = list(prog)
    p[i] = (c if code is None else code, t if jt is None else jt,
            f if jf is None else jf, kk if k is None else k)
    return tuple(p)


def _is_errno(k):
    return k & 0xFFFF0000 == 0x00050000


def mutants(prog, flags):
    """(演算子, 説明, program, flags) を列挙する。"""
    out = []
    n = len(prog)
    arch_idx = [i for i, ins in enumerate(prog) if ins[0] == R.JEQ_K and ins[3] == X86_64]
    ceiling_idx = [i for i, ins in enumerate(prog) if ins[0] == R.JGE_K and ins[3] == 472]
    for i, (code, jt, jf, k) in enumerate(prog):
        if code in _JUMPS:
            # 8 bit に収まらない値 (0 - 1、255 + 1) は命令列検査で落ちる
            for d in (-1, 1):
                out.append(("jump", f"{i} jt{d:+}", _set(prog, i, jt=jt + d), flags))
                out.append(("jump", f"{i} jf{d:+}", _set(prog, i, jf=jf + d), flags))
            if code != R.JSET_K and i not in arch_idx:
                for d in (-1, 1):
                    out.append(("dispatch", f"{i} k{d:+}",
                                _set(prog, i, k=(k + d) & 0xFFFFFFFF), flags))
        if code == R.JA:
            for d in (-1, 1):
                out.append(("jump", f"{i} ja{d:+}", _set(prog, i, k=(k + d) & 0xFFFFFFFF), flags))
        if code == R.LD_W_ABS:
            out.append(("width", f"{i} off {k}->{k ^ 4}", _set(prog, i, k=k ^ 4), flags))
        if code in (R.JSET_K, R.ALU_AND_K):
            for b in range(32):
                out.append(("mask", f"{i} bit{b}", _set(prog, i, k=k ^ (1 << b)), flags))
        if code == R.RET_K:
            if _is_errno(k):
                for e in _ERRNOS:
                    if e != k & 0xFFFF:
                        out.append(("errno", f"{i} errno {e}",
                                    _set(prog, i, k=R.ret_errno(e)), flags))
                out.append(("action", f"{i} errno->allow", _set(prog, i, k=R.RET_ALLOW), flags))
            elif k == R.RET_ALLOW:
                out.append(("action", f"{i} allow->eperm",
                            _set(prog, i, k=R.ret_errno(errno.EPERM)), flags))
            elif k == R.RET_USER_NOTIF:
                out.append(("action", f"{i} notif->eperm",
                            _set(prog, i, k=R.ret_errno(errno.EPERM)), flags))
                out.append(("action", f"{i} notif->allow", _set(prog, i, k=R.RET_ALLOW), flags))
            elif k == R.RET_KILL_PROCESS:
                out.append(("action", f"{i} kill->allow", _set(prog, i, k=R.RET_ALLOW), flags))
                out.append(("action", f"{i} kill->enosys",
                            _set(prog, i, k=R.ret_errno(errno.ENOSYS)), flags))
            out.append(("fallthrough", f"drop RET {i}", prog[:i] + prog[i + 1:], flags))
    for i in arch_idx:
        for v in (R.AUDIT_ARCH_I386, R.AUDIT_ARCH_AARCH64, X86_64 - 1, X86_64 + 1, 0):
            out.append(("arch", f"{i} arch {v:#x}", _set(prog, i, k=v), flags))
    for i in ceiling_idx:
        # 既定 (表の外) の行き先と ceiling
        for v in (471, 473, 0x40000000):
            out.append(("ceiling", f"{i} ceiling {v}", _set(prog, i, k=v), flags))
        target = i + 1 + prog[i][1]
        if prog[target][0] == R.JA:
            target = target + 1 + prog[target][3]
        for v in (R.RET_ALLOW, R.ret_errno(errno.EPERM), R.RET_KILL_PROCESS):
            out.append(("default", f"ceiling target {target} -> {v:#x}",
                        _set(prog, target, k=v), flags))
    # 引数の lo/hi を全体で取り違える (nr / arch の load は除く)
    swapped = tuple((c, t, f, k ^ 4) if c == R.LD_W_ABS and k >= 16 else (c, t, f, k)
                    for c, t, f, k in prog)
    out.append(("endianness", "all args lo<->hi", swapped, flags))
    for drop, label in ((0x10, "no TSYNC_ESRCH"), (0x20, "no WAIT_KILLABLE_RECV"),
                        (0x8, "no NEW_LISTENER"), (0x1, "no TSYNC")):
        out.append(("flag", label, prog, flags & ~drop))
    out.append(("flag", "with LOG", prog, flags | LOG))
    assert n == len(prog)
    return out


OPERATORS = ("jump", "width", "errno", "arch", "mask", "dispatch", "action", "default",
             "ceiling", "fallthrough", "endianness", "flag")


@pytest.fixture(scope="module")
def results():
    compiled = B.compile_policy()
    oracle = Oracle()
    assert oracle.detect(compiled.program, compiled.filter_flags) is None
    injected = Counter()
    detected = Counter()
    by_stage = Counter()
    survivors = []
    for op, desc, prog, flags in mutants(compiled.program, compiled.filter_flags):
        injected[op] += 1
        stage = oracle.detect(prog, flags)
        if stage is None:
            survivors.append((op, desc))
        else:
            detected[op] += 1
            by_stage[(op, stage)] += 1
            if stage == "invalid":
                # 参照 interpreter が拒んだ命令列は、製品の生成器の検査でも生成失敗になる
                with pytest.raises(P.PolicyBuildError):
                    B.check_program(prog)
            else:
                # 参照側の検査を通った命令列は、製品の検査も通す (規則が一致している)
                B.check_program(prog)
    return injected, detected, by_stage, survivors


@pytest.mark.parametrize("op", OPERATORS)
def test_every_mutant_is_detected(results, op):
    injected, detected, by_stage, survivors = results
    assert injected[op] > 0, op
    assert detected[op] == injected[op], [s for s in survivors if s[0] == op][:20]
    print(f"{op}: injected={injected[op]} detected={detected[op]} "
          + " ".join(f"{s}={c}" for (o, s), c in sorted(by_stage.items()) if o == op))


def test_unmutated_program_passes_every_check():
    compiled = B.compile_policy()
    assert Oracle().detect(compiled.program, compiled.filter_flags) is None
