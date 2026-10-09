"""改善 mission の CLI 部分木に掛ける seccomp `improve-cli/1` の表 (データ側)。

表は 0〜471 の全番号に action を一つずつ与え、条件つき syscall の引数判定もデータで持つ。
ここでは表そのもの、生成時の検査、data evaluator、host の header との照合を見る。
"""
from __future__ import annotations

import dataclasses
import errno
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine != "x86_64",
    reason="improve-cli/1 は x86_64 Linux 専用")

from agentic_fx.core import seccomp_cli_policy as P  # noqa: E402

HEADER = Path("/usr/include/x86_64-linux-gnu/asm/unistd_64.h")

ALLOW = 0x7FFF0000
KILL = 0x80000000
NOTIF = 0x7FC00000


def E(err: int) -> int:
    return 0x00050000 | err


def nr_of(name: str) -> int:
    return P.SNAPSHOT_NUMBERS[name]


def ev(name_or_nr, *args, arch=0xC000003E):
    nr = nr_of(name_or_nr) if isinstance(name_or_nr, str) else name_or_nr
    full = tuple(args) + (0,) * (6 - len(args))
    return P.evaluate(arch, nr, full)


# --- 件数、ceiling、hash ------------------------------------------------------

POLICY_HASH = "411c5239c362429890312987d9cb0f534f2a5c826123a6125931a6d788466108"


def test_counts_ceiling_and_policy_hash_are_pinned():
    t = P.policy_table()
    assert P.CEILING == 472
    assert len(t.rows) == 472
    assert [r.nr for r in t.rows] == list(range(472))
    assert t.counts() == {"ALLOW": 202, "COND": 12, "NOTIF": 1, "EPERM": 150,
                          "EACCES": 1, "ENOSYS": 106}
    holes = [r for r in t.rows if r.name is None]
    assert len(holes) == 87
    assert all(r.action == "ENOSYS" for r in holes)
    assert t.policy_hash == POLICY_HASH
    assert P.PROFILE_ID == "improve-cli/1"


def test_canonical_text_is_ascii_rows_then_condition_definitions():
    text = P.policy_table().canonical_text
    text.encode("ascii")
    lines = text.splitlines()
    assert lines[0] == "0\tread\tALLOW\t-"
    assert lines[41] == "41\tsocket\tCOND\tsocket-stream-v1"
    assert lines[42] == "42\tconnect\tNOTIF\t-"
    cond_lines = lines[472:]
    assert len(cond_lines) == 12
    assert all(c.startswith("cond\t") for c in cond_lines)


def _with_condition(name: str, **changes):
    conds = dict(P.CONDITIONS)
    conds[name] = dataclasses.replace(conds[name], **changes)
    return P.build_table(conditions=conds)


def test_policy_hash_covers_condition_definitions():
    base = P.policy_table().policy_hash
    assert P.build_table().policy_hash == base
    pr = P.CONDITIONS["personality"]
    root = pr.decision
    # 条件の判定値を 1 つ変える
    test = dataclasses.replace(root.test, values=(("PER_QUERY", 0xFFFFFFFE),))
    assert _with_condition(
        "personality", decision=dataclasses.replace(root, test=test)).policy_hash != base
    # 定数の名前だけを変える
    test = dataclasses.replace(root.test, values=(("PER_QUERx", 0xFFFFFFFF),))
    assert _with_condition(
        "personality", decision=dataclasses.replace(root, test=test)).policy_hash != base
    # 条件外の errno
    assert _with_condition(
        "personality", decision=dataclasses.replace(root, otherwise="EINVAL")).policy_hash != base
    # then と otherwise の入れ替え
    assert _with_condition(
        "personality",
        decision=dataclasses.replace(root, then="EPERM", otherwise="ALLOW")).policy_hash != base
    # 条件 id の 1 文字
    assert _with_condition("personality", condition_id="personality-query-v2").policy_hash != base
    # 引数の位置と幅
    for change in ({"arg": 1}, {"width": "u64"}):
        test = dataclasses.replace(root.test, **change)
        assert _with_condition(
            "personality", decision=dataclasses.replace(root, test=test)).policy_hash != base
    # mask の 1 bit
    cl = P.CONDITIONS["clone"].decision
    test = dataclasses.replace(cl.test, mask=("CLONE_FORBIDDEN_MASK", 0x7E028080 ^ 0x8000))
    assert _with_condition(
        "clone", decision=dataclasses.replace(cl, test=test)).policy_hash != base


def test_malformed_condition_fails_generation():
    pr = P.CONDITIONS["personality"]
    bad = [
        dataclasses.replace(pr, condition_id="personality query"),
        dataclasses.replace(pr, decision=dataclasses.replace(pr.decision, otherwise="EBOGUS")),
        dataclasses.replace(pr, decision=dataclasses.replace(pr.decision, otherwise="ALLOW")),
        dataclasses.replace(pr, decision=dataclasses.replace(
            pr.decision, test=dataclasses.replace(pr.decision.test, arg=6))),
        dataclasses.replace(pr, decision=dataclasses.replace(
            pr.decision, test=dataclasses.replace(pr.decision.test, values=(("x", 1 << 32),)))),
    ]
    for cond in bad:
        conds = dict(P.CONDITIONS)
        conds["personality"] = cond
        with pytest.raises(P.PolicyBuildError) as ei:
            P.build_table(conditions=conds)
        assert ei.value.reason == "bad_condition"


def test_policy_hash_covers_row_actions():
    base = P.policy_table().policy_hash
    cls = {k: set(v) for k, v in P.CLASSIFICATION.items()}
    cls["ALLOW"].discard("getpid")
    cls["EPERM"].add("getpid")
    assert P.build_table(classification=cls).policy_hash != base


# --- 生成時の検査 --------------------------------------------------------------

def _cls():
    return {k: set(v) for k, v in P.CLASSIFICATION.items()}


def test_unclassified_name_fails_generation():
    cls = _cls()
    cls["ALLOW"].discard("read")
    with pytest.raises(P.PolicyBuildError) as ei:
        P.build_table(classification=cls)
    assert ei.value.reason == "unclassified"
    assert "read" in str(ei.value)


def test_duplicate_classification_fails_generation():
    cls = _cls()
    cls["ALLOW"].add("bpf")
    with pytest.raises(P.PolicyBuildError) as ei:
        P.build_table(classification=cls)
    assert ei.value.reason == "duplicate"
    assert "bpf" in str(ei.value)


def _snapshot_header_text() -> str:
    return "".join(f"#define __NR_{n} {v}\n" for n, v in sorted(P.SNAPSHOT_NUMBERS.items(),
                                                                  key=lambda x: x[1]))


def test_unknown_header_name_fails_generation():
    holes = sorted(set(range(472)) - set(P.SNAPSHOT_NUMBERS.values()))
    # header に分類の無い名前が足された (hole の番号を使い ceiling は変えない)
    text = _snapshot_header_text() + f"#define __NR_afx_unlisted_call {holes[-1]}\n"
    numbers = P.numbers_from_header(text)
    with pytest.raises(P.PolicyBuildError) as ei:
        P.build_table(numbers=numbers)
    assert ei.value.reason == "unclassified"
    assert "afx_unlisted_call" in str(ei.value)
    # 分類にあるが header に無い名前
    cls = _cls()
    cls["EPERM"].add("afx_no_such_call")
    with pytest.raises(P.PolicyBuildError) as ei:
        P.build_table(classification=cls)
    assert ei.value.reason == "unknown_name"
    assert "afx_no_such_call" in str(ei.value)
    # 2 つの名前が同じ番号
    text = _snapshot_header_text() + "#define __NR_afx_alias 0\n"
    with pytest.raises(P.PolicyBuildError) as ei:
        P.build_table(numbers=P.numbers_from_header(text))
    assert ei.value.reason == "number_collision"


def test_non_ascii_name_fails_generation_with_a_named_reason():
    holes = sorted(set(range(P.CEILING)) - set(P.SNAPSHOT_NUMBERS.values()))
    text = _snapshot_header_text() + f"#define __NR_afx_\u00e9call {holes[-1]}\n"
    numbers = P.numbers_from_header(text)
    cls = _cls()
    cls["EPERM"].add("afx_\u00e9call")
    with pytest.raises(P.PolicyBuildError) as ei:
        P.build_table(classification=cls, numbers=numbers)
    assert ei.value.reason in ("unknown_name", "invalid_name")
    cls = _cls()
    cls["EPERM"].add("afx_\u00e9call")
    with pytest.raises(P.PolicyBuildError) as ei:
        P.build_table(classification=cls)
    assert ei.value.reason in ("unknown_name", "invalid_name")


def test_ceiling_mismatch_fails_generation():
    text = _snapshot_header_text() + "#define __NR_afx_future_call 472\n"
    cls = _cls()
    cls["EPERM"].add("afx_future_call")
    with pytest.raises(P.PolicyBuildError) as ei:
        P.build_table(classification=cls, numbers=P.numbers_from_header(text))
    assert ei.value.reason == "ceiling_mismatch"


def test_build_table_has_no_ceiling_parameter():
    with pytest.raises(TypeError):
        P.build_table(ceiling=473)


def test_negative_number_fails_generation_instead_of_dropping_the_row():
    numbers = {**P.SNAPSHOT_NUMBERS, "getpid": -1}
    with pytest.raises(P.PolicyBuildError) as ei:
        P.build_table(numbers=numbers)
    assert ei.value.reason == "number_out_of_range"


def test_condition_set_must_match_cond_classification():
    conds = dict(P.CONDITIONS)
    del conds["personality"]
    with pytest.raises(P.PolicyBuildError) as ei:
        P.build_table(conditions=conds)
    assert ei.value.reason == "condition_mismatch"


def test_snapshot_matches_installed_header():
    if not HEADER.exists():
        pytest.skip("asm/unistd_64.h が無い")
    assert P.numbers_from_header(HEADER.read_text()) == P.SNAPSHOT_NUMBERS
    # 製品の既定表は header でなく同梱 snapshot から作る
    assert P.build_table(numbers=P.numbers_from_header(HEADER.read_text())).policy_hash \
        == P.policy_table().policy_hash


def test_policy_module_does_not_read_headers_at_runtime():
    src = Path(P.__file__).read_text()
    assert "/usr/include" not in src
    assert not re.search(r"(?<![\w.])open\(|read_text|read_bytes", src)


# --- 条件の数値を host の header と照合 -------------------------------------

_C_NAMES = [
    "PR_SET_NAME", "PR_GET_NAME", "PR_SET_VMA", "PR_SET_VMA_ANON_NAME", "PR_GET_THP_DISABLE",
    "PR_SET_THP_DISABLE", "PR_SET_PDEATHSIG", "PR_GET_PDEATHSIG", "PR_SET_NO_NEW_PRIVS",
    "PR_GET_NO_NEW_PRIVS", "PR_GET_SECCOMP", "PR_CAPBSET_READ", "PR_GET_DUMPABLE",
    "PR_GET_CHILD_SUBREAPER",
    "F_DUPFD", "F_DUPFD_CLOEXEC", "F_GETFD", "F_SETFD", "F_GETFL", "F_SETFL", "F_GETLK",
    "F_SETLK", "F_GET_SEALS", "F_ADD_SEALS", "F_GETPIPE_SZ",
    "TCGETS", "TCGETS2", "TIOCGWINSZ", "TIOCGPGRP", "TIOCGPTN", "FIONBIO", "FIONREAD",
    "FIOCLEX", "FIONCLEX", "FICLONE", "FICLONERANGE",
    "MEMBARRIER_CMD_QUERY", "MEMBARRIER_CMD_PRIVATE_EXPEDITED",
    "MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED", "MEMBARRIER_CMD_PRIVATE_EXPEDITED_SYNC_CORE",
    "MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED_SYNC_CORE",
    "MEMBARRIER_CMD_PRIVATE_EXPEDITED_RSEQ", "MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED_RSEQ",
    "MSG_FASTOPEN", "AF_UNIX", "AF_INET", "AF_INET6", "SOCK_STREAM", "IPPROTO_TCP",
    "CLONE_PARENT", "CLONE_NEWTIME", "CLONE_NEWNS", "CLONE_NEWCGROUP", "CLONE_NEWUTS",
    "CLONE_NEWIPC", "CLONE_NEWUSER", "CLONE_NEWPID", "CLONE_NEWNET",
]


@pytest.fixture(scope="module")
def header_values(tmp_path_factory):
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("C compiler が無い")
    d = tmp_path_factory.mktemp("hdr")
    src = d / "v.c"
    body = "\n".join(f'  printf("%s %llu\\n", "{n}", (unsigned long long)({n}));' for n in _C_NAMES)
    src.write_text(
        "#define _GNU_SOURCE\n#include <stdio.h>\n#include <sched.h>\n#include <sys/socket.h>\n"
        "#include <netinet/in.h>\n#include <sys/prctl.h>\n#include <linux/prctl.h>\n"
        "#include <fcntl.h>\n#include <sys/ioctl.h>\n#include <asm/termbits.h>\n"
        "#include <linux/fs.h>\n#include <linux/membarrier.h>\n"
        "int main(void) {\n" + body + "\n  return 0;\n}\n")
    exe = d / "v"
    r = subprocess.run([cc, "-o", str(exe), str(src)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"header 照合用の C が通らない: {r.stderr[:300]}")
    out = subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout
    return {k: int(v) for k, v in (line.split() for line in out.splitlines())}


def test_condition_constants_match_host_headers(header_values):
    values = P.named_condition_values()
    missing = set(_C_NAMES) - set(values)
    assert not missing, missing
    for name in _C_NAMES:
        assert values[name] == header_values[name], name


def test_clone_mask_is_namespaces_and_parent(header_values):
    want = 0
    for n in ("CLONE_PARENT", "CLONE_NEWTIME", "CLONE_NEWNS", "CLONE_NEWCGROUP",
              "CLONE_NEWUTS", "CLONE_NEWIPC", "CLONE_NEWUSER", "CLONE_NEWPID",
              "CLONE_NEWNET"):
        want |= header_values[n]
    assert want == 0x7E028080
    assert P.named_condition_values()["CLONE_FORBIDDEN_MASK"] == want


# --- data evaluator の 3 段 ---------------------------------------------------

def test_foreign_arch_and_x32_kill():
    assert ev(0, arch=0x40000003) == KILL
    assert ev(0, arch=0xC00000B7) == KILL
    assert ev(0x40000000) == KILL
    assert ev(0x40000000 | 39) == KILL
    assert ev(0xFFFFFFFF) == KILL


def test_ceiling_and_above_are_enosys():
    for nr in (472, 473, 1000, 0x3FFFFFFF):
        assert ev(nr) == E(errno.ENOSYS)
    # seccomp_data.nr は 32 bit。上位 bit は落ちる
    assert ev((1 << 32) | 39) == ev(39) == ALLOW


def test_representative_rows():
    assert ev("getpid") == ALLOW
    assert ev("ptrace") == E(errno.EPERM)
    assert ev("perf_event_open") == E(errno.EACCES)
    assert ev("keyctl") == E(errno.ENOSYS)
    assert ev("connect") == NOTIF
    assert ev("listen") == E(errno.EPERM)
    holes = sorted(set(range(P.CEILING)) - set(P.SNAPSHOT_NUMBERS.values()))
    assert holes and 335 not in holes
    assert all(ev(nr) == E(errno.ENOSYS) for nr in holes)


# --- 12b の表の側 --------------------------------------------------------------

def test_clone3_is_enosys_and_clone_parent_is_eperm():
    assert ev("clone3") == E(errno.ENOSYS)
    assert ev("clone", 0x8000 | 17) == E(errno.EPERM)
    assert ev("clone", 0x100000 | 17) == ALLOW
    assert ev("clone", 0x100 | 0x800 | 0x10000 | 0x100000) == ALLOW


def test_posix_timer_syscalls_are_eperm():
    for name in ("timer_create", "timer_settime", "timer_gettime", "timer_getoverrun",
                 "timer_delete"):
        assert ev(name) == E(errno.EPERM), name


def test_filter_flags_exclude_log():
    assert P.FILTER_FLAGS == 1 | 8 | 16 | 32
    assert P.FILTER_FLAGS & 2 == 0
