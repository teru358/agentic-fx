"""afx-ops の seccomp 番号表の model (x86_64 専用)。

x86_64 の syscall 番号 0〜CEILING-1 の一つ一つに action を一つ与える。未割当の番号
(hole) も ENOSYS として明示する。評価順:

1. arch が x86_64 でない、または x32 (`nr >= 0x40000000`) → KILL_PROCESS
2. `nr >= CEILING` → ERRNO(ENOSYS)
3. 0〜CEILING-1 → 表の action (ALLOW / COND / EPERM / EACCES / ENOSYS)

COND は引数の判定をデータ (`Condition`) で持ち、data evaluator と BPF 生成器
(`bpf`) の両方がこのデータだけに従う。afx-ops には supervisor がいないので、
USER_NOTIF の action と listener は持たない。

profile は「syscall 名 → action」と「条件付き syscall 名 → 条件 ID」を持ち、条件の
中身は条件 ID ごとの library から引く。表の hash は canonical text (行
`nr\\tname\\taction\\tcondition_id` と条件定義の行) の SHA-256。
"""
from __future__ import annotations

import errno as _errno
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from afx_ops.isolation.syscalls_x86_64 import CEILING, SNAPSHOT_NUMBERS

AUDIT_ARCH_X86_64 = 0xC000003E
X32_SYSCALL_BIT = 0x40000000

RET_KILL_PROCESS = 0x80000000
RET_ALLOW = 0x7FFF0000
_RET_ERRNO_BASE = 0x00050000


def ret_errno(err: int) -> int:
    """`SECCOMP_RET_ERRNO` の戻り値。"""
    return _RET_ERRNO_BASE | (err & 0xFFFF)


ACTIONS = ("ALLOW", "COND", "EPERM", "EACCES", "ENOSYS")
# 条件の結果に使う名前と戻り値
RESULT_ERRNO = {
    "EPERM": _errno.EPERM, "EACCES": _errno.EACCES, "ENOSYS": _errno.ENOSYS,
    "EINVAL": _errno.EINVAL, "ENOTTY": _errno.ENOTTY, "EAFNOSUPPORT": _errno.EAFNOSUPPORT,
}


def result_value(result: str) -> int:
    """action / 条件結果の名前 → seccomp の戻り値。"""
    if result == "ALLOW":
        return RET_ALLOW
    return ret_errno(RESULT_ERRNO[result])


class PolicyBuildError(ValueError):
    """表または BPF を作れない。`reason` は固定の失敗名。"""

    REASONS = frozenset({
        "unclassified", "duplicate", "unknown_name", "number_collision", "ceiling_mismatch",
        "condition_mismatch", "unknown_condition", "bad_condition", "arch_mismatch",
        "too_many_instructions", "jump_out_of_range", "invalid_name", "empty_program",
        "bad_opcode", "bad_field", "bad_load_offset", "no_final_return",
        "unreachable_instruction", "number_out_of_range", "hash_mismatch",
    })

    def __init__(self, reason: str, detail: str = "") -> None:
        assert reason in self.REASONS, reason
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason


# --- 条件のデータ -------------------------------------------------------------

_U32 = 0xFFFFFFFF
_U64 = 0xFFFFFFFFFFFFFFFF
_OPS = ("in", "masked_in", "no_bits")
_WIDTHS = ("lo32", "u64")


@dataclass(frozen=True)
class ArgTest:
    """引数 1 個の判定。

    width: `lo32` は下位 32 bit だけを見る (int 型の引数。kernel も切り詰める)。
    `u64` は上位 32 bit が 0 かつ下位が値に一致 (flags の 64 bit 全体一致、pid と pointer)。
    op: `in` は値の集合、`masked_in` は mask との AND が集合、`no_bits` は mask の bit が
    一つも立っていない。
    """

    arg: int
    op: str
    width: str = "lo32"
    values: tuple[tuple[str, int], ...] = ()
    mask: tuple[str, int] | None = None

    def holds(self, args: Sequence[int]) -> bool:
        v = args[self.arg] & _U64
        lo = v & _U32
        if self.width == "u64" and v >> 32:
            return False
        if self.op == "in":
            return any(lo == x for _n, x in self.values)
        assert self.mask is not None
        if self.op == "masked_in":
            return any(lo & self.mask[1] == x for _n, x in self.values)
        return lo & self.mask[1] == 0

    def canonical(self) -> dict:
        return {"arg": self.arg, "op": self.op, "width": self.width,
                "values": [[n, x] for n, x in self.values],
                "mask": None if self.mask is None else [self.mask[0], self.mask[1]]}


@dataclass(frozen=True)
class Branch:
    """`test` が成り立てば `then`、成り立たなければ `otherwise`。葉は結果の名前
    (ALLOW か errno 名)。"""

    test: ArgTest
    then: Branch | str
    otherwise: Branch | str


def _walk(node: Branch | str, args: Sequence[int]) -> str:
    while isinstance(node, Branch):
        node = node.then if node.test.holds(args) else node.otherwise
    return node


def _canonical_node(node: Branch | str) -> object:
    if isinstance(node, str):
        return node
    return {"test": node.test.canonical(), "then": _canonical_node(node.then),
            "else": _canonical_node(node.otherwise)}


@dataclass(frozen=True)
class Condition:
    """条件つき syscall 1 個の引数判定。"""

    condition_id: str
    decision: Branch

    def evaluate(self, args: Sequence[int]) -> str:
        return _walk(self.decision, args)

    def canonical(self) -> str:
        return json.dumps(_canonical_node(self.decision), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Profile:
    """機能 1 個の seccomp profile の元データ。

    classification: action → syscall 名の集合 (COND を含む)。
    conditions: COND の syscall 名 → 条件 ID。
    """

    name: str
    version: int
    classification: Mapping[str, frozenset[str]]
    conditions: Mapping[str, str]
    arch: int = AUDIT_ARCH_X86_64

    @property
    def profile_id(self) -> str:
        return f"{self.name}/{self.version}"


# --- 表 ----------------------------------------------------------------------

@dataclass(frozen=True)
class Row:
    nr: int
    name: str | None  # None は未割当 (hole)
    action: str
    condition_id: str | None


@dataclass(frozen=True)
class PolicyTable:
    profile_id: str
    rows: tuple[Row, ...]
    conditions: Mapping[str, Condition] = field(repr=False)  # syscall 名 → 条件
    ceiling: int = CEILING
    canonical_text: str = field(default="", repr=False)
    policy_hash: str = ""

    def counts(self) -> dict[str, int]:
        c = Counter(r.action for r in self.rows)
        return {a: c.get(a, 0) for a in ACTIONS}

    def action_of(self, name: str) -> str:
        for row in self.rows:
            if row.name == name:
                return row.action
        raise KeyError(name)


def _canonical_text(profile_id: str, rows: Iterable[Row],
                    conditions: Mapping[str, Condition]) -> str:
    lines = [f"profile\t{profile_id}"]
    lines += [f"{r.nr}\t{r.name or '-'}\t{r.action}\t{r.condition_id or '-'}" for r in rows]
    lines += [f"cond\t{name}\t{conditions[name].condition_id}\t{conditions[name].canonical()}"
              for name in sorted(conditions)]
    return "".join(line + "\n" for line in lines)


def _check_condition(name: str, cond: Condition) -> None:
    def bad(msg: str) -> PolicyBuildError:
        return PolicyBuildError("bad_condition", f"{name}: {msg}")

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*-v\d+", cond.condition_id):
        raise bad(f"condition id {cond.condition_id!r}")
    if not isinstance(cond.decision, Branch):
        raise bad("decision must start with a test")
    results: set[str] = set()

    def visit(node: Branch | str) -> None:
        if isinstance(node, str):
            if node != "ALLOW" and node not in RESULT_ERRNO:
                raise bad(f"result {node!r}")
            results.add(node)
            return
        t = node.test
        if not 0 <= t.arg <= 5 or t.op not in _OPS or t.width not in _WIDTHS:
            raise bad(f"test {t}")
        if t.op == "in" and (not t.values or t.mask is not None):
            raise bad(f"test {t}")
        if t.op == "masked_in" and (not t.values or t.mask is None or t.width != "lo32"):
            raise bad(f"test {t}")
        if t.op == "no_bits" and (t.values or t.mask is None or t.width != "lo32"):
            raise bad(f"test {t}")
        if any(not 0 <= x <= _U32 for _n, x in t.values):
            raise bad(f"value out of 32 bit range in {t}")
        if t.mask is not None and not 0 < t.mask[1] <= _U32:
            raise bad(f"mask out of range in {t}")
        visit(node.then)
        visit(node.otherwise)

    visit(cond.decision)
    if "ALLOW" not in results or len(results) < 2:
        raise bad("a condition must both allow and deny")


def build_table(profile: Profile, library: Mapping[str, Condition], *,
                numbers: Mapping[str, int] = SNAPSHOT_NUMBERS,
                ceiling: int = CEILING) -> PolicyTable:
    """profile と番号から全番号表を作る。

    arch 不一致・未分類・重複・未知の名前・番号の範囲外・ceiling 不一致・条件 ID 不明・
    条件式の誤りは生成失敗 (`PolicyBuildError`)。
    """
    if profile.arch != AUDIT_ARCH_X86_64:
        raise PolicyBuildError("arch_mismatch", f"{profile.arch:#x}")
    classification = profile.classification
    # 正準 text は ASCII でしか符号化できない。符号化の例外を外に出さず生成失敗にする
    names = set(numbers) | {n for members in classification.values() for n in members}
    bad_names = sorted(n for n in names if not n.isascii())
    if bad_names:
        raise PolicyBuildError("invalid_name", ", ".join(ascii(n) for n in bad_names))
    out_of_range = sorted(f"{n}={nr}" for n, nr in numbers.items() if not 0 <= nr < ceiling)
    if out_of_range:
        raise PolicyBuildError("number_out_of_range", ", ".join(out_of_range))
    by_nr: dict[int, str] = {}
    for name, nr in sorted(numbers.items()):
        if nr in by_nr:
            raise PolicyBuildError("number_collision", f"{by_nr[nr]} / {name} = {nr}")
        by_nr[nr] = name
    extra = set(classification) - set(ACTIONS)
    if extra:
        raise PolicyBuildError("unknown_name", f"actions {sorted(extra)}")
    sets = {a: frozenset(classification.get(a, ())) for a in ACTIONS}
    owner: dict[str, str] = {}
    dups = []
    for action in ACTIONS:
        for name in sets[action]:
            if name in owner:
                dups.append(f"{name} ({owner[name]}/{action})")
            owner[name] = action
    if dups:
        raise PolicyBuildError("duplicate", ", ".join(sorted(dups)))
    unknown = set(owner) - set(numbers)
    if unknown:
        raise PolicyBuildError("unknown_name", ", ".join(sorted(unknown)))
    unclassified = set(numbers) - set(owner)
    if unclassified:
        raise PolicyBuildError("unclassified", ", ".join(sorted(unclassified)))
    if not numbers or max(numbers.values()) + 1 != ceiling:
        top = max(numbers.values()) if numbers else None
        raise PolicyBuildError("ceiling_mismatch", f"header max {top}, ceiling {ceiling}")
    if set(profile.conditions) != sets["COND"]:
        raise PolicyBuildError(
            "condition_mismatch", f"{sorted(set(profile.conditions) ^ sets['COND'])}")
    conditions: dict[str, Condition] = {}
    for name, cond_id in profile.conditions.items():
        cond = library.get(cond_id)
        if cond is None or cond.condition_id != cond_id:
            raise PolicyBuildError("unknown_condition", f"{name}: {cond_id!r}")
        _check_condition(name, cond)
        conditions[name] = cond

    rows = []
    for nr in range(ceiling):
        name = by_nr.get(nr)
        if name is None:
            rows.append(Row(nr, None, "ENOSYS", None))
            continue
        action = owner[name]
        rows.append(Row(nr, name, action,
                        conditions[name].condition_id if action == "COND" else None))
    text = _canonical_text(profile.profile_id, rows, conditions)
    return PolicyTable(profile_id=profile.profile_id, rows=tuple(rows), conditions=conditions,
                       ceiling=ceiling, canonical_text=text,
                       policy_hash=hashlib.sha256(text.encode("ascii")).hexdigest())


def evaluate(table: PolicyTable, arch: int, nr: int, args: Sequence[int]) -> int:
    """`(arch, nr, args)` に対し filter が返すべき seccomp 戻り値。BPF を見ない。"""
    nr &= _U32  # seccomp_data.nr は 32 bit
    if arch & _U32 != AUDIT_ARCH_X86_64 or nr >= X32_SYSCALL_BIT:
        return RET_KILL_PROCESS
    if nr >= table.ceiling:
        return ret_errno(_errno.ENOSYS)
    row = table.rows[nr]
    if row.action == "COND":
        assert row.name is not None
        full = tuple(a & _U64 for a in args) + (0,) * (6 - len(args))
        return result_value(table.conditions[row.name].evaluate(full))
    return result_value(row.action)
