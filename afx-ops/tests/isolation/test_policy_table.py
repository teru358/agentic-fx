"""機能別 seccomp 番号表の生成時検査、header との照合、hash の pin、分類の中身。"""
from __future__ import annotations

import dataclasses
import errno
from pathlib import Path

import pytest

from afx_ops.isolation import bpf
from afx_ops.isolation import profiles as P
from afx_ops.isolation.policy import (
    AUDIT_ARCH_X86_64,
    RET_ALLOW,
    RET_KILL_PROCESS,
    ArgTest,
    Branch,
    Condition,
    PolicyBuildError,
    build_table,
    evaluate,
    ret_errno,
)
from afx_ops.isolation.syscalls_x86_64 import CEILING, SNAPSHOT_NUMBERS, numbers_from_header

HEADERS = (Path("/usr/include/x86_64-linux-gnu/asm/unistd_64.h"),
           Path("/usr/include/asm/unistd_64.h"))
NR = SNAPSHOT_NUMBERS


def _table(feature: str):
    return build_table(P.make_profile(feature), P.CONDITION_LIBRARY)


def _with(profile, **changes):
    return dataclasses.replace(profile, **changes)


def _move(classification, name, src, dst):
    out = {a: set(v) for a, v in classification.items()}
    out[src].discard(name)
    out.setdefault(dst, set()).add(name)
    return {a: frozenset(v) for a, v in out.items()}


def _mutations():
    def unclassified(p):
        c = {a: set(v) for a, v in p.classification.items()}
        c["ALLOW"].discard("getpid")
        return _with(p, classification={a: frozenset(v) for a, v in c.items()}), {}, None

    def duplicate(p):
        c = {a: set(v) for a, v in p.classification.items()}
        c["EPERM"].add("getpid")
        return _with(p, classification={a: frozenset(v) for a, v in c.items()}), {}, None

    def beyond_ceiling(p):
        return p, {}, {**NR, "future_call": CEILING}

    def ceiling_hole(p):
        numbers = dict(NR)
        top = max(numbers, key=numbers.get)
        del numbers[top]
        c = {a: set(v) - {top} for a, v in p.classification.items()}
        return _with(p, classification={a: frozenset(v) for a, v in c.items()}), {}, numbers

    def unknown_condition_id(p):
        return _with(p, conditions={**p.conditions, "clone": "afx-ops-clone-any-v1"}), {}, None

    def cond_without_condition(p):
        conditions = dict(p.conditions)
        del conditions["ioctl"]
        return _with(p, conditions=conditions), {}, None

    def bad_condition_result(p):
        lib = {"afx-ops-ioctl-v1": Condition("afx-ops-ioctl-v1", Branch(
            ArgTest(arg=1, op="in", values=(("X", 1),)), "ALLOW", "EBOGUS"))}
        return p, lib, None

    def bad_condition_arg(p):
        lib = {"afx-ops-ioctl-v1": Condition("afx-ops-ioctl-v1", Branch(
            ArgTest(arg=6, op="in", values=(("X", 1),)), "ALLOW", "ENOTTY"))}
        return p, lib, None

    def condition_only_allows(p):
        lib = {"afx-ops-ioctl-v1": Condition("afx-ops-ioctl-v1", Branch(
            ArgTest(arg=1, op="in", values=(("X", 1),)), "ALLOW", "ALLOW"))}
        return p, lib, None

    def library_key_differs_from_id(p):
        lib = {"afx-ops-ioctl-v1": dataclasses.replace(
            P.CONDITION_LIBRARY["afx-ops-ioctl-v1"], condition_id="afx-ops-ioctl-v2")}
        return p, lib, None

    def arch_mismatch(p):
        return _with(p, arch=0x40000003), {}, None

    def unknown_action(p):
        return _with(p, classification={**p.classification, "NOTIF": frozenset()}), {}, None

    return {
        "unclassified": (unclassified, "unclassified"),
        "duplicate": (duplicate, "duplicate"),
        "beyond_ceiling": (beyond_ceiling, "number_out_of_range"),
        "ceiling_hole": (ceiling_hole, "ceiling_mismatch"),
        "unknown_condition_id": (unknown_condition_id, "unknown_condition"),
        "cond_without_condition": (cond_without_condition, "condition_mismatch"),
        "bad_condition_result": (bad_condition_result, "bad_condition"),
        "bad_condition_arg": (bad_condition_arg, "bad_condition"),
        "condition_only_allows": (condition_only_allows, "bad_condition"),
        "library_key_differs_from_id": (library_key_differs_from_id, "unknown_condition"),
        "arch_mismatch": (arch_mismatch, "arch_mismatch"),
        "unknown_action": (unknown_action, "unknown_name"),
    }


MUTATIONS = _mutations()


@pytest.mark.parametrize("feature", P.FEATURES)
@pytest.mark.parametrize("kind", sorted(MUTATIONS))
def test_table_rejects_unclassified_duplicate_and_bad_condition(feature, kind):
    mutate, reason = MUTATIONS[kind]
    profile, library_patch, numbers = mutate(P.make_profile(feature))
    library = {**P.CONDITION_LIBRARY, **library_patch}
    kwargs = {} if numbers is None else {"numbers": numbers}
    with pytest.raises(PolicyBuildError) as info:
        build_table(profile, library, **kwargs)
    assert info.value.reason == reason


@pytest.mark.parametrize("feature", P.FEATURES)
def test_unmutated_profiles_build(feature):
    table = _table(feature)
    assert len(table.rows) == CEILING
    assert table.profile_id == f"afx-ops-{feature}/{P.PROFILE_VERSION}"
    assert P.PROFILE_VERSION == 2


def test_snapshot_matches_kernel_header():
    header = next((h for h in HEADERS if h.exists()), None)
    if header is None:
        pytest.skip("asm/unistd_64.h が無い")
    numbers = numbers_from_header(header.read_text())
    assert numbers == dict(SNAPSHOT_NUMBERS)
    assert max(numbers.values()) + 1 == CEILING


# 表 (canonical text) と最終 BPF bytes の SHA-256。表を変えたらここも変える
PINNED = {
    "bot": ("28a0c1af9915d3477ada056e63792a76cd43d92c2fc08fe5d5eb5079b6c38f0c",
            "b562e27e9d71bc818457f4cdd4d0a22606a48f2e62bbf0f8b0bdbddaa9a391d7"),
    "web": ("fbb6ae842a14dfaec77f127c2a8cd0379c29ec77f935506159211b817db4247a",
            "291b89949f8c88d77aa6fd8af3d2321c293d796d56e96638a85c637b24a99efe"),
    "notify": ("cd70ead38ff35e3fa89988399bff9ac0b9962f28c1b78b89d60c94b71d9caac9",
               "b562e27e9d71bc818457f4cdd4d0a22606a48f2e62bbf0f8b0bdbddaa9a391d7"),
}


@pytest.mark.parametrize("feature", P.FEATURES)
def test_profile_hash_is_pinned(feature):
    compiled = bpf.compile_table(_table(feature))
    assert (compiled.policy_hash, compiled.bpf_hash) == PINNED[feature]
    assert P.PINNED_HASHES[feature] == PINNED[feature]
    assert P.feature_filter(feature).compiled.bpf_hash == PINNED[feature][1]


def test_a_changed_table_does_not_match_the_pin():
    profile = P.make_profile("bot")
    c = _move(profile.classification, "getpid", "ALLOW", "EPERM")
    compiled = bpf.compile_table(build_table(_with(profile, classification=c),
                                             P.CONDITION_LIBRARY))
    with pytest.raises(PolicyBuildError) as info:
        bpf.verify_hashes(compiled, policy_hash=PINNED["bot"][0], bpf_hash=PINNED["bot"][1])
    assert info.value.reason == "hash_mismatch"


# --- 分類の中身 (spec の表と共通拒否集合) -----------------------------------------

COMMON_DENY = {
    "ptrace": "EPERM", "process_vm_readv": "EPERM", "process_vm_writev": "EPERM",
    "pidfd_getfd": "EPERM", "kcmp": "EPERM", "process_madvise": "EPERM",
    "perf_event_open": "EACCES", "bpf": "EPERM", "io_uring_setup": "EPERM",
    "io_uring_enter": "EPERM", "io_uring_register": "EPERM", "add_key": "ENOSYS",
    "request_key": "ENOSYS", "keyctl": "ENOSYS", "unshare": "EPERM", "setns": "EPERM",
    "mount": "EPERM", "umount2": "EPERM", "pivot_root": "EPERM", "move_mount": "EPERM",
    "open_tree": "EPERM", "fsopen": "EPERM", "fsmount": "EPERM", "fspick": "EPERM",
    "fsconfig": "EPERM", "mount_setattr": "EPERM", "bind": "EPERM", "listen": "EPERM",
    "clone3": "ENOSYS", "fork": "EPERM", "vfork": "EPERM", "execve": "EPERM",
    "execveat": "EPERM", "kill": "EPERM", "tkill": "EPERM", "pidfd_open": "EPERM",
    "pidfd_send_signal": "EPERM", "wait4": "EPERM", "waitid": "EPERM",
    # 他 process への signal と共有 file への advisory lock (実 trace で未使用)
    "tgkill": "EPERM", "flock": "EPERM",
}


@pytest.mark.parametrize("feature", P.FEATURES)
def test_common_deny_set_and_errnos(feature):
    table = _table(feature)
    assert {name: table.action_of(name) for name in COMMON_DENY} == COMMON_DENY
    assert table.action_of("futex") == "ALLOW"
    for name in ("clone", "prctl", "fcntl", "ioctl", "socket"):
        assert table.action_of(name) == "COND"


def test_feature_communication_columns():
    bot, web, notify = (_table(f) for f in ("bot", "web", "notify"))
    for t in (bot, notify):
        assert t.action_of("accept") == t.action_of("accept4") == "EPERM"
        assert t.action_of("recvfrom") == "ALLOW"
        assert t.action_of("sendto") == "COND"
    assert web.action_of(P.WEB_ACCEPT_SYSCALL) == "ALLOW"
    assert web.action_of("accept") == "EPERM"
    assert web.action_of("sendto") == web.action_of("recvfrom") == "COND"
    assert web.action_of("sendmsg") == web.action_of("recvmsg") == "EPERM"
    assert bot.canonical_text.split("\n", 1)[1] == notify.canonical_text.split("\n", 1)[1]


def _ev(feature, name, *args):
    return evaluate(_table(feature), AUDIT_ARCH_X86_64, NR[name], args)


E = ret_errno


def test_evaluator_clone_is_thread_flags_exact_match():
    flags = P.CLONE_THREAD_FLAGS
    assert flags == 0x3D0F00
    for f in P.FEATURES:
        assert _ev(f, "clone", flags) == RET_ALLOW
        assert _ev(f, "clone", flags | 1 << 32) == E(errno.EPERM)
        assert _ev(f, "clone", flags | 0x10000000) == E(errno.EPERM)  # CLONE_NEWUSER
        assert _ev(f, "clone", flags | 0x8000) == E(errno.EPERM)  # CLONE_PARENT
        assert _ev(f, "clone", flags | 17) == E(errno.EPERM)  # exit signal
        assert _ev(f, "clone", 17) == E(errno.EPERM)  # fork 相当
        assert _ev(f, "clone", flags & ~0x10000) == E(errno.EPERM)


def test_evaluator_prctl_dumpable_only_zero():
    for f in P.FEATURES:
        assert _ev(f, "prctl", 4, 0) == RET_ALLOW
        assert _ev(f, "prctl", 4, 1) == E(errno.EPERM)
        assert _ev(f, "prctl", 4, 1 << 32) == E(errno.EPERM)
        assert _ev(f, "prctl", 3) == RET_ALLOW
        assert _ev(f, "prctl", 0x59616D61, 0) == E(errno.EPERM)
        assert _ev(f, "prctl", 15) == E(errno.EPERM)


def test_evaluator_socket_by_feature():
    unix_stream_cloexec = (1, 1 | 0o2000000 | 0o4000)
    for f in P.FEATURES:
        assert _ev(f, "socket", *unix_stream_cloexec) == RET_ALLOW
        assert _ev(f, "socket", 1, 2) == E(errno.EACCES)
        assert _ev(f, "socket", 17, 3) == E(errno.EAFNOSUPPORT)
    for f in ("bot", "notify"):
        assert _ev(f, "socket", 2, 1, 0) == RET_ALLOW
        assert _ev(f, "socket", 10, 1, 6) == RET_ALLOW
        assert _ev(f, "socket", 2, 2 | 0o4000, 0) == RET_ALLOW
        assert _ev(f, "socket", 10, 2, 17) == RET_ALLOW
        assert _ev(f, "socket", 2, 3, 1) == E(errno.EACCES)
        assert _ev(f, "socket", 2, 1, 17) == E(errno.EACCES)
        assert _ev(f, "socket", 16, 3, 0) == RET_ALLOW
        assert _ev(f, "socket", 16, 3, 9) == E(errno.EACCES)
    for args in ((2, 1, 0), (10, 1, 0), (2, 2, 0), (10, 2, 0)):
        assert _ev("web", "socket", *args) == E(errno.EACCES)
    assert _ev("web", "socket", 16, 3, 0) == E(errno.EAFNOSUPPORT)


def test_evaluator_web_send_recv_need_null_address():
    assert _ev("web", "sendto", 3, 0, 1, 0, 0, 0) == RET_ALLOW
    assert _ev("web", "sendto", 3, 0, 1, 0, 0x1000, 16) == E(errno.EPERM)
    assert _ev("web", "sendto", 3, 0, 1, 0x20000000, 0, 0) == E(errno.EPERM)
    assert _ev("web", "recvfrom", 3, 0, 1, 0, 0, 0) == RET_ALLOW
    assert _ev("web", "recvfrom", 3, 0, 1, 0, 0x1000, 0x2000) == E(errno.EPERM)
    assert _ev("bot", "sendto", 3, 0, 1, 0, 0x1000, 16) == RET_ALLOW
    assert _ev("bot", "sendto", 3, 0, 1, 0x20000000, 0, 0) == E(errno.EPERM)


def test_evaluator_arch_x32_and_ceiling():
    t = _table("bot")
    assert evaluate(t, 0x40000003, 20, ()) == RET_KILL_PROCESS
    assert evaluate(t, AUDIT_ARCH_X86_64, 0x40000000 | 39, ()) == RET_KILL_PROCESS
    assert evaluate(t, AUDIT_ARCH_X86_64, CEILING, ()) == E(errno.ENOSYS)
    assert evaluate(t, AUDIT_ARCH_X86_64, 400, ()) == E(errno.ENOSYS)  # hole
