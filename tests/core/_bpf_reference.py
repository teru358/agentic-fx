"""seccomp 用 classic BPF の小さな参照 interpreter (テスト専用)。

生成器 (`agentic_fx.core.seccomp_cli_bpf`) を import しない。生成器と同じ誤解を
共有しないよう、opcode・offset・戻り値の定数もここで独立に書く。
seccomp_data の配置 (x86_64、little endian):
nr = 0 (u32)、arch = 4 (u32)、instruction_pointer = 8 (u64)、args[i] = 16 + 8*i (u64)。
"""
from __future__ import annotations

import struct
from collections.abc import Sequence

LD_W_ABS = 0x20
ALU_AND_K = 0x54
JA = 0x05
JEQ_K = 0x15
JGT_K = 0x25
JGE_K = 0x35
JSET_K = 0x45
RET_K = 0x06

AUDIT_ARCH_X86_64 = 0xC000003E
AUDIT_ARCH_I386 = 0x40000003
AUDIT_ARCH_AARCH64 = 0xC00000B7

RET_KILL_PROCESS = 0x80000000
RET_ALLOW = 0x7FFF0000
RET_USER_NOTIF = 0x7FC00000


def ret_errno(err: int) -> int:
    return 0x00050000 | (err & 0xFFFF)


# kernel が受け付ける命令数の上限 (BPF_MAXINSNS)
MAX_INSNS = 4096
_DATA_SIZE = 64


class InvalidProgram(Exception):
    """kernel の filter 検査 (命令・飛び先・offset・末尾 RET) を通らない命令列。"""


class RuntimeFault(Exception):
    """解釈中に命令列の外へ出た。"""


class StepLimitExceeded(Exception):
    """命令数を超えて実行した (検査を通った前方 jump だけの列では起きない)。"""


def seccomp_data(nr: int, arch: int, args: Sequence[int] = (0,) * 6, ip: int = 0) -> bytes:
    a = [x & 0xFFFFFFFFFFFFFFFF for x in args]
    a += [0] * (6 - len(a))
    return struct.pack("<IIQ6Q", nr & 0xFFFFFFFF, arch & 0xFFFFFFFF, ip, *a)


def validate(program: Sequence[tuple[int, int, int, int]]) -> None:
    """kernel の `bpf_check_classic` + `seccomp_check_filter` と同じ観点で検査する。

    加えて、どの入力でも通らない命令 (到達不能) があれば拒む。
    """
    n = len(program)
    if n == 0 or n > MAX_INSNS:
        raise InvalidProgram(f"length {n}")
    succ: list[list[int]] = []
    for pc, (code, jt, jf, k) in enumerate(program):
        if not (0 <= code <= 0xFFFF and 0 <= jt <= 255 and 0 <= jf <= 255
                and 0 <= k <= 0xFFFFFFFF):
            raise InvalidProgram(f"field range at {pc}")
        if code == LD_W_ABS:
            if k % 4 or k >= _DATA_SIZE:
                raise InvalidProgram(f"bad load offset {k} at {pc}")
            succ.append([pc + 1])
        elif code == ALU_AND_K:
            succ.append([pc + 1])
        elif code == RET_K:
            succ.append([])
        elif code == JA:
            if pc + 1 + k >= n:
                raise InvalidProgram(f"ja out of range at {pc}")
            succ.append([pc + 1 + k])
        elif code in (JEQ_K, JGT_K, JGE_K, JSET_K):
            if pc + 1 + max(jt, jf) >= n:
                raise InvalidProgram(f"jump out of range at {pc}")
            succ.append([pc + 1 + jt, pc + 1 + jf])
        else:
            raise InvalidProgram(f"opcode {code:#x} at {pc}")
    if program[-1][0] != RET_K:
        raise InvalidProgram("last instruction is not RET")
    seen = {0}
    todo = [0]
    while todo:
        for nxt in succ[todo.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    if len(seen) != n:
        raise InvalidProgram(f"unreachable instruction {min(set(range(n)) - seen)}")


def run(program: Sequence[tuple[int, int, int, int]], *, nr: int,
        arch: int = AUDIT_ARCH_X86_64, args: Sequence[int] = (0,) * 6, ip: int = 0) -> int:
    data = seccomp_data(nr, arch, args, ip)
    acc = 0
    pc = 0
    n = len(program)
    # 前方 jump だけの列は命令数以内で必ず終わる。超えたら検査漏れの命令列
    for _step in range(n + 1):
        if not 0 <= pc < n:
            raise RuntimeFault(f"pc {pc} outside program")
        code, jt, jf, k = program[pc]
        if code == LD_W_ABS:
            acc = struct.unpack_from("<I", data, k)[0]
            pc += 1
        elif code == ALU_AND_K:
            acc &= k
            pc += 1
        elif code == JA:
            pc += 1 + k
        elif code == JEQ_K:
            pc += 1 + (jt if acc == k else jf)
        elif code == JGT_K:
            pc += 1 + (jt if acc > k else jf)
        elif code == JGE_K:
            pc += 1 + (jt if acc >= k else jf)
        elif code == JSET_K:
            pc += 1 + (jt if acc & k else jf)
        elif code == RET_K:
            return k
        else:
            raise RuntimeFault(f"opcode {code:#x} at {pc}")
    raise StepLimitExceeded(f"more than {n + 1} steps")
