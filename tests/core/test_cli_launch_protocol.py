"""status protocol の frame の encode / decode と固定値。"""
from __future__ import annotations

import socket
import struct

import pytest

from agentic_fx.core import cli_launch_protocol as P

NONCE = bytes(range(16))


def _prep() -> P.Prepared:
    return P.Prepared("improve-cli/1", "ab" * 32, "cd" * 32, 57, 8)


def test_frame_layout_is_fixed():
    assert P.HEADER_LEN == 24
    assert P.FRAME_LEN == {P.HELLO: 28, P.JOINED: 24, P.PREPARED: 132, P.GO: 24,
                           P.EXEC_FAILED: 28, P.SETUP_FAILED: 32}
    assert (P.HELLO, P.JOINED, P.PREPARED, P.GO, P.EXEC_FAILED, P.SETUP_FAILED) == (
        1, 2, 3, 4, 5, 6)
    hello = P.encode_hello(NONCE, 1234)
    assert hello == b"AFXL" + struct.pack("<HH", 2, 1) + NONCE + struct.pack("<i", 1234)


@pytest.mark.parametrize("data, frame_type", [
    (P.encode_hello(NONCE, 77), P.HELLO),
    (P.encode_joined(NONCE), P.JOINED),
    (P.encode_go(NONCE), P.GO),
    (P.encode_exec_failed(NONCE, 2), P.EXEC_FAILED),
    (P.encode_setup_failed(NONCE, "credentials_unexpected", 1), P.SETUP_FAILED),
    (P.encode_prepared(NONCE, _prep()), P.PREPARED),
])
def test_round_trip(data, frame_type):
    f = P.decode(data, NONCE)
    assert f.type == frame_type
    if frame_type == P.HELLO:
        assert f.pid == 77
    if frame_type == P.EXEC_FAILED:
        assert f.errno == 2
    if frame_type == P.SETUP_FAILED:
        assert (f.reason, f.errno) == ("credentials_unexpected", 1)
    if frame_type == P.PREPARED:
        assert f.prepared == _prep()


def _replace(data: bytes, offset: int, raw: bytes) -> bytes:
    return data[:offset] + raw + data[offset + len(raw):]


@pytest.mark.parametrize("name, data", [
    ("empty", b""),
    ("short", P.encode_hello(NONCE, 1)[:23]),
    ("header_only_hello", P.encode_hello(NONCE, 1)[:24]),
    ("long", P.encode_hello(NONCE, 1) + b"\0"),
    ("magic", _replace(P.encode_go(NONCE), 0, b"AFXM")),
    ("version", _replace(P.encode_go(NONCE), 4, struct.pack("<H", 1))),
    ("type", _replace(P.encode_go(NONCE), 6, struct.pack("<H", 7))),
    ("nonce", P.encode_go(bytes(16))),
    ("nonce_last_byte", P.encode_go(NONCE[:15] + bytes([NONCE[15] ^ 1]))),
    ("setup_reason", _replace(P.encode_setup_failed(NONCE, "rlimit_failed"), 24,
                              struct.pack("<H", 999))),
    ("setup_reserved", _replace(P.encode_setup_failed(NONCE, "rlimit_failed"), 26,
                                struct.pack("<H", 1))),
    ("profile_id_nul", _replace(P.encode_prepared(NONCE, _prep()), 24, b"a\0b")),
    ("profile_id_non_ascii", _replace(P.encode_prepared(NONCE, _prep()), 24, b"\xff")),
])
def test_malformed_frames(name, data):
    with pytest.raises(P.MalformedFrame):
        P.decode(data, NONCE)


def test_take_fds_separates_other_ancillary_kinds():
    creds = struct.pack("<iII", 1, 2, 3)
    fds, other = P.take_fds([
        (socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("<ii", 5, 6)),
        (socket.SOL_SOCKET, socket.SCM_CREDENTIALS, creds),
    ])
    assert fds == [5, 6]
    assert other == [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS)]


def test_setup_reasons_cover_nested_landlock_reasons():
    from agentic_fx.core.landlock_nested import NESTED_REASONS

    assert set(NESTED_REASONS) <= set(P.SETUP_REASONS.values())
    assert {"inherited_seccomp_filter", "credentials_unexpected", "rlimit_failed",
            "subreaper_failed"} <= set(P.SETUP_REASONS.values())
    # 番号は wire 上の値なので並びを固定する
    assert P.SETUP_REASONS[1] == "inherited_seccomp_filter"
    assert P.SETUP_REASONS[2] == "credentials_unexpected"


def test_attestation_reason_words():
    assert P.ATTESTATION_REASONS == (
        "attestation_malformed", "attestation_order", "attestation_deadline",
        "attestation_mismatch", "attestation_go_send_failed", "attestation_exec_failed")


def test_rlimit_profile_constants():
    mib = 1024 * 1024
    claude = P.rlimit_profile("claude")
    assert claude["RLIMIT_AS"] == (4096 * mib, 4096 * mib)
    assert P.rlimit_profile("codex")["RLIMIT_AS"] == (262144 * mib, 262144 * mib)
    assert P.rlimit_profile("opencode")["RLIMIT_AS"] == (262144 * mib, 262144 * mib)
    assert claude["RLIMIT_NOFILE"] == (128, 128)
    assert claude["RLIMIT_FSIZE"] == (8 * mib, 8 * mib)
    for name in ("RLIMIT_CORE", "RLIMIT_NICE", "RLIMIT_RTPRIO", "RLIMIT_RTTIME",
                 "RLIMIT_MEMLOCK", "RLIMIT_MSGQUEUE"):
        assert claude[name] == (0, 0)
    assert "RLIMIT_SIGPENDING" not in claude
    with pytest.raises(KeyError):
        P.rlimit_profile("local")


def test_nonce_is_128_bit_and_fresh():
    a, b = P.new_launch_nonce(), P.new_launch_nonce()
    assert len(a) == 16 and a != b


def test_protocol_module_imports_only_stdlib():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(P))
    mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree)
             if isinstance(n, ast.ImportFrom) and n.module}
    assert mods <= {"os", "secrets", "socket", "struct", "dataclasses"}


def test_nonce_every_byte_is_random():
    nonces = [P.new_launch_nonce() for _ in range(64)]
    for i in range(P.NONCE_LEN):
        assert len({n[i] for n in nonces}) > 1, f"byte {i} が固定"


def test_fixed_reason_words():
    """reason の語と wire 番号は呼び出し側・記録側が文字列で読むので、値そのものを固定する。"""
    assert P.SETUP_REASONS == {
        1: "inherited_seccomp_filter", 2: "credentials_unexpected", 3: "rlimit_failed",
        4: "subreaper_failed", 5: "landlock_abi_too_old", 6: "landlock_create_failed",
        7: "no_new_privs_failed", 8: "landlock_restrict_failed", 9: "seccomp_install_failed",
        10: "seccomp_self_check_failed", 11: "policy_compile_failed"}
    assert (P.LAUNCHER_DIED, P.LAUNCHER_SPAWN_FAILED, P.CGROUP_JOIN_FAILED,
            P.RECEIVER_HANDOFF_FAILED, P.CLI_EXEC_UNPROVEN) == (
        "launcher_died", "launcher_spawn_failed", "cgroup_join_failed",
        "receiver_handoff_failed", "cli_exec_unproven")
    assert (P.SECCOMP_USER_NOTIF_UNAVAILABLE, P.SECCOMP_WAIT_KILLABLE_UNAVAILABLE,
            P.SECCOMP_TSYNC_ESRCH_UNAVAILABLE) == (
        "seccomp_user_notif_unavailable", "seccomp_wait_killable_unavailable",
        "seccomp_tsync_esrch_unavailable")


def test_probe_asks_the_kernel_for_each_flag(monkeypatch):
    """probe は install と同じ flag の組を kernel に問う。flag を落とした問い方は、
    この host では同じ答えになっても別 kernel で能力を見誤る。"""
    from agentic_fx.core import cli_launcher
    from agentic_fx.core import seccomp_cli_policy as SP

    asked = []

    def spy(flags):
        asked.append(flags)
        return (-1, 14)  # EFAULT = flag は受け付けた

    monkeypatch.setattr(cli_launcher, "raw_flags_probe", spy)
    monkeypatch.setattr(cli_launcher, "raw_user_notif_probe", lambda: (0, 0))
    monkeypatch.setattr(cli_launcher, "raw_notif_sizes_probe", lambda: (0, 0))
    assert cli_launcher.probe_seccomp_capabilities() == ()
    assert sorted(asked) == sorted([
        SP.SECCOMP_FILTER_FLAG_NEW_LISTENER,
        SP.SECCOMP_FILTER_FLAG_NEW_LISTENER | SP.SECCOMP_FILTER_FLAG_WAIT_KILLABLE_RECV,
        SP.SECCOMP_FILTER_FLAG_TSYNC | SP.SECCOMP_FILTER_FLAG_NEW_LISTENER
        | SP.SECCOMP_FILTER_FLAG_TSYNC_ESRCH])
    assert all(f & SP.FILTER_FLAGS == f for f in asked)
