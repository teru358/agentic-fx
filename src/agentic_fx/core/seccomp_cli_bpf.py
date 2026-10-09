"""`improve-cli/1` の表 (`seccomp_cli_policy`) から classic BPF を機械的に作る。

命令の配置:

- 先頭で arch / x32 → KILL_PROCESS、`nr >= CEILING` → ENOSYS を返す。
- 0〜CEILING-1 は、同じ戻り値が続く番号を 1 つの範囲にまとめ、範囲の境界で `JGE` の
  二分木を組む。葉は戻り値の `RET` か、条件つき syscall の判定 block をその場に置く。
- 条件分岐の jt/jf は 8 bit (255 命令先まで) なので、左の部分木が 255 命令を超える
  節では `JGE` の直後に `JA` (32 bit の飛び幅) を置き、右の部分木へはそこから飛ぶ。

飛び先は常に前方で、どの命令も何らかの入力で到達する (共通の return へ遠くから飛ぶ
配置を取らない)。filter の適用はここでは行わない。
"""
from __future__ import annotations

import errno as _errno
import hashlib
import struct
from collections.abc import Sequence
from dataclasses import dataclass

from agentic_fx.core.seccomp import (
    AUDIT_ARCH_X86_64,
    BPF_ALU_AND_K,
    BPF_JEQ_K,
    BPF_JGE_K,
    BPF_JSET_K,
    BPF_LD_W_ABS,
    BPF_RET_K,
    MAX_INSTRUCTIONS,
    RET_KILL_PROCESS,
    X32_SYSCALL_BIT,
    ret_errno,
)
from agentic_fx.core.seccomp_cli_policy import (
    FILTER_FLAGS,
    PROFILE_ID,
    PROFILE_VERSION,
    ArgTest,
    Branch,
    Condition,
    PolicyBuildError,
    PolicyTable,
    policy_table,
    result_value,
)

BPF_JA = 0x05
MAX_JUMP = 255

Instruction = tuple[int, int, int, int]

# seccomp_data のオフセット: nr = 0、arch = 4、args[i] = 16 + 8*i (little endian)
_OFF_NR = 0
_OFF_ARCH = 4


def _arg_lo(i: int) -> int:
    return 16 + 8 * i


def _arg_hi(i: int) -> int:
    return 20 + 8 * i


@dataclass(frozen=True)
class CompiledPolicy:
    """filter を掛ける側と fingerprint に渡す一組。"""

    profile_id: str
    profile_version: int
    policy_hash: str
    program: tuple[Instruction, ...]
    bpf_hash: str
    filter_flags: int


def program_bytes(program: Sequence[Instruction]) -> bytes:
    """`struct sock_filter` の並び (little endian) そのもの。"""
    return b"".join(struct.pack("<HBBI", *ins) for ins in program)


_SECCOMP_DATA_SIZE = 64
_BPF_JGT_K = 0x25  # 生成器は使わないが、kernel が受理する条件分岐として検査には含める

# opcode → 種別。検査と到達集合の伸ばし方はこの種別で決まる
_KIND = {
    BPF_JEQ_K: "cond", _BPF_JGT_K: "cond", BPF_JGE_K: "cond", BPF_JSET_K: "cond",
    BPF_JA: "ja", BPF_RET_K: "ret", BPF_LD_W_ABS: "ld", BPF_ALU_AND_K: "alu",
}


def _check_instruction(pc: int, ins: Instruction, n: int) -> None:
    code, jt, jf, k = ins
    where = f"instruction {pc}"
    kind = _KIND.get(code)
    if kind is None:
        raise PolicyBuildError("bad_opcode", f"{where}: {code:#x}")
    if kind == "cond":
        if not (0 <= jt <= MAX_JUMP and 0 <= jf <= MAX_JUMP) or pc + 1 + max(jt, jf) >= n:
            raise PolicyBuildError("jump_out_of_range", where)
        if not 0 <= k <= 0xFFFFFFFF:
            raise PolicyBuildError("bad_field", where)
        return
    if kind == "ja":
        if not 0 <= k <= 0xFFFFFFFF or pc + 1 + k >= n:
            raise PolicyBuildError("jump_out_of_range", where)
    else:
        if not 0 <= k <= 0xFFFFFFFF:
            raise PolicyBuildError("bad_field", where)
        if kind == "ld" and (k % 4 or k >= _SECCOMP_DATA_SIZE):
            raise PolicyBuildError("bad_load_offset", f"{where}: {k}")
    # 条件分岐以外の jt/jf は kernel が見ないが、8 bit の外は命令として組めない
    if not (0 <= jt <= MAX_JUMP and 0 <= jf <= MAX_JUMP):
        raise PolicyBuildError("bad_field", where)


def check_program(program: Sequence[Instruction]) -> None:
    """kernel の seccomp filter 検査に通る命令列であることを確かめる。

    命令数・opcode・`seccomp_data` の load offset・飛び先・末尾の RET に加えて、
    どの入力でも通らない命令 (到達不能) があれば拒む。飛びは常に前方なので、
    先頭から順に到達集合を伸ばせば足りる。
    """
    n = len(program)
    if n == 0:
        raise PolicyBuildError("empty_program")
    if n > MAX_INSTRUCTIONS:
        raise PolicyBuildError("too_many_instructions", f"{n} (> {MAX_INSTRUCTIONS})")
    for pc, ins in enumerate(program):
        _check_instruction(pc, ins, n)
    if program[-1][0] != BPF_RET_K:
        raise PolicyBuildError("no_final_return")
    reached = [False] * n
    reached[0] = True
    for pc, (code, jt, jf, k) in enumerate(program):
        if not reached[pc]:
            raise PolicyBuildError("unreachable_instruction", f"instruction {pc}")
        if code == BPF_RET_K:
            continue
        kind = _KIND[code]
        targets = ((pc + 1 + k,) if kind == "ja"
                   else (pc + 1 + jt, pc + 1 + jf) if kind == "cond"
                   else (pc + 1,))
        for t in targets:
            reached[t] = True


# --- 条件 block -----------------------------------------------------------------

def _assemble_local(items: list) -> list[Instruction]:
    """label 付きの疑似命令列 (block 内の前方 jump だけ) を相対 offset に直す。

    同じ値を返す RET が隣り合うときは 1 つにまとめる。隣の RET へずれた飛び先が
    同じ結果になる (変異として見えない) 配置を残さないため。
    """
    labels: dict[str, int] = {}
    pending: list[str] = []
    body: list = []
    for it in items:
        if isinstance(it, str):
            pending.append(it)
            continue
        if (it[0] == BPF_RET_K and body and body[-1][0] == BPF_RET_K
                and body[-1][3] == it[3]):
            for name in pending:
                labels[name] = len(body) - 1
        else:
            for name in pending:
                labels[name] = len(body)
            body.append(it)
        pending = []
    for name in pending:
        labels[name] = len(body)
    out: list[Instruction] = []
    for i, (code, jt, jf, k) in enumerate(body):
        def rel(t: int | str) -> int:
            return labels[t] - i - 1 if isinstance(t, str) else t
        a, b = rel(jt), rel(jf)
        if not (0 <= a <= MAX_JUMP and 0 <= b <= MAX_JUMP):
            raise PolicyBuildError("jump_out_of_range", f"condition block instruction {i}")
        out.append((code, a, b, k & 0xFFFFFFFF))
    return out


def _test_code(t: ArgTest, state: tuple | None, then: str, otherwise: str
               ) -> tuple[list, tuple | None, tuple | None]:
    """判定 1 個の疑似命令列と、then / otherwise 側の累算器の中身を返す。

    累算器が既に同じ値を持つときは load を省く (同じ値の再 load は消しても結果が
    変わらず、変異で区別できない命令になる)。
    """
    lo = ("lo", t.arg)
    code: list = []
    if t.width == "u64":
        # u64 は `in` だけ (masked_in / no_bits は下位 32 bit 専用で、表の検査が弾く)
        assert t.op == "in", t.op
        code += [(BPF_LD_W_ABS, 0, 0, _arg_hi(t.arg)), (BPF_JEQ_K, 0, otherwise, 0)]
        state = None
    if t.op == "masked_in":
        assert t.mask is not None
        masked = ("and", t.arg, t.mask[1])
        if state != masked:
            if state != lo:
                code.append((BPF_LD_W_ABS, 0, 0, _arg_lo(t.arg)))
            code.append((BPF_ALU_AND_K, 0, 0, t.mask[1]))
        after = masked
    else:
        if state != lo:
            code.append((BPF_LD_W_ABS, 0, 0, _arg_lo(t.arg)))
        after = lo
    if t.op == "no_bits":
        assert t.mask is not None
        code.append((BPF_JSET_K, otherwise, 0, t.mask[1]))
    else:
        values = [x for _n, x in t.values]
        for j, v in enumerate(values):
            last = j == len(values) - 1
            code.append((BPF_JEQ_K, then if not last else 0, otherwise if last else 0, v))
    # u64 の上位 32 bit で落ちた経路は累算器が上位を持つ
    else_state = None if t.width == "u64" else after
    return code, after, else_state


def _condition_block(cond: Condition) -> list[Instruction]:
    items: list = []
    seq = iter(range(1 << 30))

    def emit(node: Branch | str, state: tuple | None) -> None:
        if isinstance(node, str):
            items.append((BPF_RET_K, 0, 0, result_value(node)))
            return
        n = next(seq)
        then_l, else_l = f"then{n}", f"else{n}"
        code, then_state, else_state = _test_code(node.test, state, then_l, else_l)
        items.extend(code)
        items.append(then_l)
        emit(node.then, then_state)
        items.append(else_l)
        emit(node.otherwise, else_state)

    # block の入口では累算器は syscall 番号を持つ
    emit(cond.decision, ("nr",))
    return _assemble_local(items)


# --- 番号の二分木 ---------------------------------------------------------------

def _ranges(table: PolicyTable) -> list[tuple[int, tuple]]:
    """(範囲の先頭番号, 葉) の列。葉は ("ret", 戻り値) か ("cond", 名前)。"""
    out: list[tuple[int, tuple]] = []
    for row in table.rows:
        leaf = (("cond", row.name) if row.action == "COND"
                else ("ret", result_value(row.action)))
        if out and leaf[0] == "ret" and out[-1][1] == leaf:
            continue
        out.append((row.nr, leaf))
    return out


def _tree(ranges: list[tuple[int, tuple]]) -> list:
    """番号の JGE 木。条件つきの葉は block への JA (飛び幅は後で埋める) にする。"""
    if len(ranges) == 1:
        kind, value = ranges[0][1]
        if kind == "ret":
            return [(BPF_RET_K, 0, 0, value)]
        return [("ja_to", value)]
    m = len(ranges) // 2
    left = _tree(ranges[:m])
    right = _tree(ranges[m:])
    k = ranges[m][0]
    if len(left) <= MAX_JUMP:
        return [(BPF_JGE_K, len(left), 0, k)] + left + right
    # 左が遠いときは JA で右へ中継する
    return [(BPF_JGE_K, 0, 1, k), (BPF_JA, 0, 0, len(left))] + left + right


def generate_program(table: PolicyTable) -> tuple[Instruction, ...]:
    prog: list = [
        (BPF_LD_W_ABS, 0, 0, _OFF_ARCH),
        (BPF_JEQ_K, 1, 0, AUDIT_ARCH_X86_64),
        (BPF_RET_K, 0, 0, RET_KILL_PROCESS),
        (BPF_LD_W_ABS, 0, 0, _OFF_NR),
        (BPF_JGE_K, 0, 1, X32_SYSCALL_BIT),
        (BPF_RET_K, 0, 0, RET_KILL_PROCESS),
        (BPF_JGE_K, 0, 1, table.ceiling),
        (BPF_RET_K, 0, 0, ret_errno(_errno.ENOSYS)),
    ]
    prog += _tree(_ranges(table))
    # 条件 block は木の後ろに番号順で並べる。block の隣は別 block の load で始まる
    start: dict[str, int] = {}
    for row in table.rows:
        if row.action == "COND":
            assert row.name is not None
            start[row.name] = len(prog)
            prog += _condition_block(table.conditions[row.name])
    out: list[Instruction] = []
    for pc, ins in enumerate(prog):
        if ins[0] == "ja_to":
            out.append((BPF_JA, 0, 0, start[ins[1]] - pc - 1))
        else:
            out.append(ins)
    check_program(out)
    return tuple(out)


def compile_policy(table: PolicyTable | None = None) -> CompiledPolicy:
    """表 → BPF。profile、policy hash、BPF byte hash、filter flag を一組で返す。"""
    t = policy_table() if table is None else table
    program = generate_program(t)
    return CompiledPolicy(
        profile_id=PROFILE_ID, profile_version=PROFILE_VERSION, policy_hash=t.policy_hash,
        program=program, bpf_hash=hashlib.sha256(program_bytes(program)).hexdigest(),
        filter_flags=FILTER_FLAGS)
