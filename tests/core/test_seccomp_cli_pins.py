"""`improve-cli/1` の生成器と表の検査の境界を、製品の表とは別の入力で確かめる。

製品の表・条件はそのまま使い、合成した条件や番号だけを `build_table` に渡す。
BPF は生成器を import しない参照 interpreter (`_bpf_reference`) で走らせる。
"""
from __future__ import annotations

import dataclasses
import os
import random
import signal
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
PERSONALITY = 135


def _cls():
    return {k: set(v) for k, v in P.CLASSIFICATION.items()}


def _with_personality(decision: P.Branch) -> P.PolicyTable:
    conds = dict(P.CONDITIONS)
    conds["personality"] = P.Condition("personality-synthetic-v1", decision)
    return P.build_table(conditions=conds)


def _agree(table: P.PolicyTable, vectors) -> None:
    prog = B.compile_policy(table).program
    R.validate(prog)
    for args in vectors:
        full = tuple(args) + (0,) * (6 - len(args))
        want = P.evaluate(X86_64, PERSONALITY, full, table)
        assert R.run(prog, nr=PERSONALITY, args=full) == want, [hex(x) for x in full]


def _test(arg, *values, op="in", width="lo32", mask=None):
    return P.ArgTest(arg=arg, op=op, width=width,
                     values=tuple((f"v{v:x}", v) for v in values), mask=mask)


# --- 同じ引数を続けて調べる条件で、累算器の中身の追跡が正しい ------------------------

def test_lo32_then_u64_on_same_argument_reloads_low_word():
    # 下位 32 bit を見た直後に同じ引数の u64 判定: 上位を load した後は下位を load し直す
    t = _with_personality(P.Branch(
        _test(0, 7), P.Branch(_test(0, 7, width="u64"), "ALLOW", "EPERM"), "EINVAL"))
    _agree(t, [(7,), (7 | HI,), (8,), (0,), (7 | 7 * HI,)])
    assert P.evaluate(X86_64, PERSONALITY, (7, 0, 0, 0, 0, 0), t) == R.RET_ALLOW


def test_failed_u64_test_leaves_high_word_in_accumulator():
    # u64 判定が上位 32 bit で外れた後、同じ引数の下位を見る判定は load し直す
    t = _with_personality(P.Branch(
        _test(0, 3, width="u64"), "ALLOW", P.Branch(_test(0, 1), "ALLOW", "EPERM")))
    _agree(t, [(3,), (1,), (1 | 5 * HI,), (3 | HI,), (1 | HI,), (2,), (5 * HI,)])
    assert P.evaluate(X86_64, PERSONALITY, (1 | 5 * HI, 0, 0, 0, 0, 0), t) == R.RET_ALLOW


def test_masked_test_then_plain_test_on_same_argument_reloads():
    # mask した値の判定の後に同じ引数の素の値を見るときは、mask 前の値を load し直す
    t = _with_personality(P.Branch(
        _test(1, 1, op="masked_in", mask=("m", 0xF)),
        P.Branch(_test(1, 0x801), "ALLOW", "EACCES"),
        "EPERM"))
    _agree(t, [(0, 1), (0, 0x801), (0, 0x811), (0, 2), (0, 0x1), (0, 0x8001)])
    assert P.evaluate(X86_64, PERSONALITY, (0, 0x801, 0, 0, 0, 0), t) == R.RET_ALLOW


# --- 命令列の検査の境界 ------------------------------------------------------------

def test_check_program_rejects_jump_landing_just_past_the_end():
    ret = (R.RET_K, 0, 0, R.RET_ALLOW)
    # 飛び先 = 命令数 (末尾の 1 つ先) は範囲外
    with pytest.raises(P.PolicyBuildError) as ei:
        B.check_program([(R.JEQ_K, 1, 0, 1), ret])
    assert ei.value.reason == "jump_out_of_range"
    with pytest.raises(P.PolicyBuildError) as ei:
        B.check_program([(B.BPF_JA, 0, 0, 1), ret])
    assert ei.value.reason == "jump_out_of_range"
    with pytest.raises(P.PolicyBuildError) as ei:
        B.check_program([(B.BPF_JA, 0, 0, 5), ret])
    assert ei.value.reason == "jump_out_of_range"
    # 末尾ちょうどへの飛びは受け付ける
    B.check_program([(R.JEQ_K, 0, 0, 1), ret])
    B.check_program([(B.BPF_JA, 0, 0, 0), ret])


def test_check_program_rejects_empty_program():
    with pytest.raises(P.PolicyBuildError) as ei:
        B.check_program([])
    assert ei.value.reason == "empty_program"


def test_oversized_generated_program_fails_generation():
    # 条件 block だけで 4096 命令を超える表は、生成器の最後の検査で生成失敗になる
    conds = dict(P.CONDITIONS)
    for name in conds:
        node: P.Branch | str = "EPERM"
        for base in range(2):
            vals = tuple(range(base * 1000 + 1, base * 1000 + 201))
            node = P.Branch(_test(0, *vals), "ALLOW", node)
        conds[name] = P.Condition(f"{name}-big-v1", node)
    table = P.build_table(conditions=conds)
    with pytest.raises(P.PolicyBuildError) as ei:
        B.compile_policy(table)
    assert ei.value.reason == "too_many_instructions"


# --- 表の生成時の検査 ----------------------------------------------------------------

def test_ceiling_must_equal_profile_ceiling_even_when_header_agrees():
    # header の最大番号 + 1 と ceiling が一致しても、profile の ceiling (472) と違えば失敗
    numbers = {**P.SNAPSHOT_NUMBERS, "afx_future_call": 472}
    cls = _cls()
    cls["EPERM"].add("afx_future_call")
    with pytest.raises(P.PolicyBuildError) as ei:
        P.build_table(classification=cls, numbers=numbers)
    assert ei.value.reason == "ceiling_mismatch"


def test_empty_header_fails_generation():
    with pytest.raises(P.PolicyBuildError) as ei:
        P.build_table(classification={}, numbers={}, conditions={})
    assert ei.value.reason == "ceiling_mismatch"


def test_unknown_action_key_fails_generation():
    cls = _cls()
    cls["KILL"] = set()
    with pytest.raises(P.PolicyBuildError) as ei:
        P.build_table(classification=cls)
    assert ei.value.reason == "unknown_name"
    assert "KILL" in str(ei.value)


@pytest.mark.parametrize("test", [
    # mask が範囲外
    _test(0, op="no_bits", mask=("m", 0)),
    _test(0, op="no_bits", mask=("m", 1 << 32)),
    # masked_in / no_bits は下位 32 bit だけ
    _test(0, 1, op="masked_in", width="u64", mask=("m", 0xF)),
    _test(0, op="no_bits", width="u64", mask=("m", 0xF)),
    # no_bits は値を持たない、in は mask を持たない
    _test(0, 1, op="no_bits", mask=("m", 0xF)),
    _test(0, 1, op="in", mask=("m", 0xF)),
    # 未知の op と幅
    _test(0, 1, op="eq"),
    _test(0, 1, width="u32"),
], ids=["mask0", "mask33", "masked_u64", "nobits_u64", "nobits_values", "in_mask", "op",
        "width"])
def test_malformed_argument_test_fails_generation(test):
    pr = P.CONDITIONS["personality"]
    conds = dict(P.CONDITIONS)
    conds["personality"] = dataclasses.replace(
        pr, decision=dataclasses.replace(pr.decision, test=test))
    with pytest.raises(P.PolicyBuildError) as ei:
        P.build_table(conditions=conds)
    assert ei.value.reason == "bad_condition"


def test_condition_id_must_be_lowercase_versioned():
    pr = P.CONDITIONS["personality"]
    for cid in ("Personality-query-v1", "personality-query", "personality_query-v1",
                "personality-query-v1x"):
        conds = dict(P.CONDITIONS)
        conds["personality"] = dataclasses.replace(pr, condition_id=cid)
        with pytest.raises(P.PolicyBuildError) as ei:
            P.build_table(conditions=conds)
        assert ei.value.reason == "bad_condition", cid


def test_canonical_text_must_be_ascii():
    # 番号表に ASCII 以外の名前が混ざった表は作れない (policy hash は ASCII の SHA-256)
    numbers = {**P.SNAPSHOT_NUMBERS, "afx_caf\u00e9": 337}
    cls = _cls()
    cls["ENOSYS"].add("afx_caf\u00e9")
    with pytest.raises(P.PolicyBuildError) as ei:
        P.build_table(classification=cls, numbers=numbers)
    assert ei.value.reason == "invalid_name"


# --- data evaluator の入力の幅 ---------------------------------------------------------

def test_arch_is_compared_as_32_bit_like_seccomp_data():
    prog = B.compile_policy().program
    for arch in (X86_64 | HI, X86_64 | (0xFFFF << 40)):
        assert P.evaluate(arch, 39, (0,) * 6) == R.run(prog, nr=39, arch=arch) == R.RET_ALLOW


def test_compiled_policy_carries_hash_of_the_given_table():
    cls = _cls()
    cls["ALLOW"].discard("getpid")
    cls["EPERM"].add("getpid")
    table = P.build_table(classification=cls)
    assert table.policy_hash != P.policy_table().policy_hash
    assert B.compile_policy(table).policy_hash == table.policy_hash


# --- 参照 interpreter 自身の命令列検査 ---------------------------------------------------

def test_reference_validator_rejects_kernel_invalid_programs():
    ret = (R.RET_K, 0, 0, R.RET_ALLOW)
    for prog in (
        [(R.JEQ_K, 1, 0, 1), ret],            # 末尾の 1 つ先へ飛ぶ
        [(R.JA, 0, 0, 1), ret],
        [(R.JEQ_K, 256, 0, 1)] + [ret] * 300,  # 8 bit に収まらない飛び幅
        [(R.JEQ_K, -1, 0, 1), ret],
        [ret, (R.LD_W_ABS, 0, 0, 0)],          # 末尾が RET でない
        [ret] * 4097,                          # 命令数の上限超え
        [],
        [(R.LD_W_ABS, 0, 0, 2), ret],          # 4 の倍数でない offset
        [(R.LD_W_ABS, 0, 0, 64), ret],         # seccomp_data の外
        [(0x24, 0, 0, 2), ret],                # 対象外の opcode (ALU MUL)
    ):
        with pytest.raises(R.InvalidProgram):
            R.validate(prog)
    R.validate([(R.JEQ_K, 0, 0, 1), ret])
    # 全命令が到達できる最大長 (JA 0 を連ねて末尾の RET へ)
    R.validate([(R.JA, 0, 0, 0)] * 4095 + [ret])


# --- 命令列検査: kernel の規則と、参照側の独立実装との一致 ------------------------------------

_RET = (R.RET_K, 0, 0, R.RET_ALLOW)

_REJECTED = {
    "bad_opcode": [
        [(0x24, 0, 0, 2), _RET],               # ALU MUL
        [(0x00, 0, 0, 0), _RET],               # LD IMM
        [(0x61, 0, 0, 0), _RET],               # LDX
        [(R.JGT_K | 0x08, 0, 0, 0), _RET],     # JGT の X 形
        [(R.LD_W_ABS | 0x10000, 0, 0, 0), _RET],
    ],
    "bad_load_offset": [
        [(R.LD_W_ABS, 0, 0, 2), _RET],
        [(R.LD_W_ABS, 0, 0, 64), _RET],
        [(R.LD_W_ABS, 0, 0, 60 + 4), _RET],
        [(R.LD_W_ABS, 0, 0, 0xFFFFFFFC), _RET],
    ],
    "no_final_return": [
        [_RET, (R.LD_W_ABS, 0, 0, 0)],
        [(R.LD_W_ABS, 0, 0, 0)],
    ],
    "unreachable_instruction": [
        [_RET, _RET],
        [(R.JA, 0, 0, 1), _RET, _RET],
        [(R.JEQ_K, 1, 1, 0), _RET, _RET, _RET],
    ],
    "jump_out_of_range": [
        [(R.JEQ_K, -1, 0, 1), _RET],
        [(R.JEQ_K, 0, 256, 1), _RET],
        [(R.JA, 0, 0, 0xFFFFFFFF), _RET],
        [(R.JA, 0, 0, -1), _RET],
    ],
    "bad_field": [
        [(R.LD_W_ABS, 0, 0, -4), _RET],
        [(R.RET_K, 0, 0, 1 << 32)],
    ],
}


@pytest.mark.parametrize("reason", sorted(_REJECTED))
def test_check_program_and_reference_validator_reject_the_same_programs(reason):
    for prog in _REJECTED[reason]:
        with pytest.raises(R.InvalidProgram):
            R.validate(prog)
        with pytest.raises(P.PolicyBuildError) as ei:
            B.check_program(prog)
        assert ei.value.reason == reason, prog


def test_check_program_accepts_what_the_generator_emits_and_reference_agrees():
    for prog in (
        [_RET],
        [(R.JA, 0, 0, 0), _RET],
        [(R.LD_W_ABS, 0, 0, 60), (R.ALU_AND_K, 0, 0, 7), (R.JSET_K, 0, 1, 1), _RET, _RET],
        [(R.JGE_K, 1, 0, 3), _RET, _RET],
    ):
        R.validate(prog)
        B.check_program(prog)
    B.check_program(B.compile_policy().program)


def test_check_program_agrees_with_reference_on_random_programs():
    rng = random.Random(20261009)
    codes = [R.LD_W_ABS, R.ALU_AND_K, R.JA, R.JEQ_K, R.JGT_K, R.JGE_K, R.JSET_K, R.RET_K,
             0x24, 0x00, 0x61]
    accepted = rejected = 0
    for _ in range(4000):
        n = rng.randint(1, 8)
        prog = []
        for _i in range(n):
            code = rng.choice(codes)
            if code == R.JA:
                prog.append((code, 0, 0, rng.choice([0, 1, 2, 3, 9])))
            elif code in (R.JEQ_K, R.JGT_K, R.JGE_K, R.JSET_K):
                prog.append((code, rng.choice([0, 1, 2, 3, 9, 300]),
                             rng.choice([0, 1, 2, 3, 9]), rng.randint(0, 5)))
            elif code == R.LD_W_ABS:
                prog.append((code, 0, 0, rng.choice([0, 4, 8, 16, 60, 2, 64])))
            else:
                prog.append((code, 0, 0, rng.randint(0, 5)))
        if rng.random() < 0.7:
            prog[-1] = _RET
        try:
            R.validate(prog)
            ref_ok = True
        except R.InvalidProgram:
            ref_ok = False
        try:
            B.check_program(prog)
            prod_ok = True
        except P.PolicyBuildError:
            prod_ok = False
        assert ref_ok == prod_ok, prog
        accepted += ref_ok
        rejected += not ref_ok
    assert accepted > 100 and rejected > 100


# --- 参照 interpreter の step 上限 ---------------------------------------------------------

def test_reference_run_stops_on_a_program_that_never_terminates():
    # jt = -1 は自分自身へ戻る。検査を通さず走らせても suite が止まらないことはない
    prog = [(R.JEQ_K, -1, -1, 0), _RET]

    def on_alarm(_s, _f):
        raise AssertionError("参照 interpreter が 10 秒以内に止まらなかった")

    old = signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(10)
    try:
        with pytest.raises(R.StepLimitExceeded):
            R.run(prog, nr=0)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def test_reference_run_allows_exactly_program_length_steps():
    # 全命令を 1 度ずつ通る最長の実行 (JA 0 の連鎖) は上限に掛からない
    prog = [(R.JA, 0, 0, 0)] * 50 + [_RET]
    assert R.run(prog, nr=0) == R.RET_ALLOW
