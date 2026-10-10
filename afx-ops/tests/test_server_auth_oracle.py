"""AC-13: 第三者 oracle を afx-ops の verifier へ別 process で与える。

harness 自身の検査 (組み立て・fake daemon・probe の出力形式) と、oracle ケース本体。
oracle ケース本体は verifier が未実装の間は期待と一致しないので xfail(strict) で置き、
verifier を実装した task がこの印を外す。
"""
from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from _oracle_harness import (TOKEN, build, flock_write_holders, load_cases, no_core,
                             received, run_probe, start_daemon, stop_daemon)

CASES = load_cases()
REALIZABLE = [c for c in CASES if c["realizable"]]
BASELINE = next(c for c in CASES if c["id"] == "baseline_lock_holder")


def _connect_and_send(path: Path, payload: bytes = b"GET / HTTP/1.1\r\n\r\n") -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(str(path))
        sock.sendall(payload)
        data = b""
        try:
            while chunk := sock.recv(65536):
                data += chunk
        except OSError:
            pass
        return data


# ------------------------------------------------ harness 自身

def test_daemon_records_bytes_per_connection(tmp_path):
    record = tmp_path / "record"
    sock_path = tmp_path / "s.sock"
    proc, pid, _ = start_daemon(sock_path, record, lock_dir=None, orphan=False, mode="answer")
    try:
        assert _connect_and_send(sock_path, b"A\r\n\r\n").startswith(b"HTTP/1.1 200")
        assert _connect_and_send(sock_path, b"BB\r\n\r\n").startswith(b"HTTP/1.1 200")
        deadline = time.monotonic() + 5
        while len(received(record, expect_any=False)) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert received(record, expect_any=False) == [b"A\r\n\r\n", b"BB\r\n\r\n"]
    finally:
        stop_daemon(proc, pid)


def test_silent_daemon_sends_nothing_back(tmp_path):
    record = tmp_path / "record"
    sock_path = tmp_path / "s.sock"
    proc, pid, _ = start_daemon(sock_path, record, lock_dir=None, orphan=False, mode="silent")
    try:
        assert _connect_and_send(sock_path, b"A\r\n\r\n") == b""
    finally:
        stop_daemon(proc, pid)


def test_daemon_holds_the_real_flock(tmp_path):
    import fcntl
    record = tmp_path / "record"
    (tmp_path / "d").mkdir()
    proc, pid, _ = start_daemon(tmp_path / "d" / "s.sock", record, lock_dir=tmp_path / "d",
                                orphan=False)
    try:
        lock_path = tmp_path / "d" / "instance.lock"
        assert flock_write_holders(lock_path) == {pid}
        with open(lock_path, "w") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        stop_daemon(proc, pid)
    assert flock_write_holders(lock_path) == set()


def test_orphan_daemon_peer_pid_is_dead(tmp_path):
    (tmp_path / "d").mkdir()
    sock_path = tmp_path / "d" / "s.sock"
    proc, pid, parent = start_daemon(sock_path, tmp_path / "record", lock_dir=tmp_path / "d",
                                     orphan=True)
    try:
        assert pid != parent
        assert not os.path.isdir(f"/proc/{parent}")
        assert os.path.isdir(f"/proc/{pid}")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(str(sock_path))
            import struct
            peer_pid, _uid, _gid = struct.unpack(
                "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        assert peer_pid == parent
        assert flock_write_holders(tmp_path / "d" / "instance.lock") == {parent}
    finally:
        stop_daemon(proc, pid)


def _check_built(case: dict, built) -> None:
    setup = case["setup"]
    run_st = os.lstat(built.run_dir)
    if setup["dir"] == "symlink":
        assert stat.S_ISLNK(run_st.st_mode)
        assert stat.S_IMODE(os.stat(built.run_dir).st_mode) == int(setup["dir_mode"], 8)
    else:
        assert stat.S_ISDIR(run_st.st_mode)
        assert stat.S_IMODE(run_st.st_mode) == int(setup["dir_mode"], 8)
    api = os.lstat(built.run_dir / "api.sock")
    if setup["socket"] == "regular_file":
        assert stat.S_ISREG(api.st_mode)
        assert built.daemon_pid is None
    elif setup["socket"] == "symlink_to_socket":
        assert stat.S_ISLNK(api.st_mode)
        assert stat.S_ISSOCK(os.stat(built.run_dir / "api.sock").st_mode)
    else:
        assert stat.S_ISSOCK(api.st_mode)
    assert built.uid == (os.getuid() if setup["client_uid"] == "self" else os.getuid() + 1)
    if setup["socket"] == "regular_file":
        return
    lock = built.lock_path
    holders = flock_write_holders(lock) if lock.exists() else set()
    kind = setup["server_lock"]
    if kind == "held":
        expected = built.daemon_parent_pid if setup["server_process"] == "exited_after_listen" \
            else built.daemon_pid
        assert holders == {expected}
    elif kind == "none":
        assert holders == set()
    elif kind == "other_process":
        assert holders == {os.getpid()}
        assert built.daemon_pid not in holders
    elif kind == "replaced_inode":
        # daemon の lock は別 inode に残り、いまの instance.lock には保持者が居ない。
        assert holders == set()
        fds = [os.readlink(f"/proc/{built.daemon_pid}/fd/{n}")
               for n in os.listdir(f"/proc/{built.daemon_pid}/fd")]
        assert any(f.endswith("instance.lock (deleted)") for f in fds)
    else:
        raise AssertionError(f"組み立てを検算できない server_lock: {kind}")
    if setup["server_process"] == "exited_after_listen":
        assert not os.path.isdir(f"/proc/{built.daemon_parent_pid}")
        assert os.path.isdir(f"/proc/{built.daemon_pid}")
    else:
        assert os.path.isdir(f"/proc/{built.daemon_pid}")


@pytest.mark.parametrize("case", REALIZABLE, ids=[c["id"] for c in REALIZABLE])
def test_every_realizable_setup_axis_can_be_built(tmp_path, case):
    with build(case["setup"], tmp_path) as built:
        _check_built(case, built)


def test_the_realizable_cases_cover_every_buildable_axis_value():
    seen = {key: {c["setup"][key] for c in REALIZABLE}
            for key in ("socket", "dir", "dir_mode", "client_uid", "server_lock",
                        "server_process")}
    assert seen["socket"] == {"socket", "regular_file", "symlink_to_socket"}
    assert seen["dir"] == {"real", "symlink"}
    assert seen["dir_mode"] == {"0700", "0755", "0750"}
    assert seen["client_uid"] == {"self", "other"}
    assert seen["server_lock"] == {"held", "none", "other_process", "replaced_inode"}
    assert seen["server_process"] == {"alive", "exited_after_listen"}


def test_probe_prints_rc_and_reason_line(tmp_path, child_env):
    with build(BASELINE["setup"], tmp_path) as built:
        done = run_probe(built, child_env)
        assert done.stdout == "rc=3 reason=peer_check\n"
        assert done.returncode == 3
        assert done.stderr == ""
        assert received(built.record, expect_any=False) == []


# ------------------------------------------------ oracle ケース本体

def _param(case: dict):
    if not case["realizable"]:
        return pytest.param(case, id=case["id"],
                            marks=pytest.mark.skip(reason=case["unrealizable_because"]))
    return pytest.param(case, id=case["id"], marks=pytest.mark.xfail(
        strict=True, raises=AssertionError, reason="verifier 未実装"))


@pytest.mark.parametrize("case", [_param(c) for c in CASES])
def test_server_auth_oracle_case(tmp_path, child_env, case):
    expect = case["expect"]
    with build(case["setup"], tmp_path) as built:
        done = run_probe(built, child_env)
        got = received(built.record, expect_any=expect["key_sent"])
    line = done.stdout.strip()
    assert done.returncode == expect["rc"], (line, done.stderr)
    if expect["key_sent"]:
        assert line == "rc=0"
        assert got and b"Authorization: Bearer " + TOKEN.encode() in got[0]
    else:
        assert line == f"rc=3 reason={expect['reason']}"
        assert sum(len(chunk) for chunk in got) == expect["pre_auth_bytes"]
