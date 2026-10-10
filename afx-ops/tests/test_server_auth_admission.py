"""afx-ops の要求経路: 固有 admission + 共有 server 認証を通るまで鍵を 1 byte も送らない。

client は別 process、fake daemon も別 process。鍵が届いたかは fake daemon の受信 bytes で
判定する。kernel の応答はモックせず、pidfd 不可は client 子の seccomp、/proc/locks 不可は
client 子の Landlock、dev 不一致は /proc/locks の入力 file の差し替えで作る。
"""
from __future__ import annotations

import os
import platform
import re
import signal
from pathlib import Path

import pytest
from _keydir import build_key_dir
from _ops_harness import (daemon, make_layout, pidfd_supported, run_client, spawn_client,
                          finish, token_in, wait_until)
from _oracle_harness import build, load_cases, received

AFX_OPS = Path(__file__).resolve().parents[1]
CASES = {c["id"]: c for c in load_cases()}
needs_pidfd = pytest.mark.skipif(not pidfd_supported(),
                                 reason="SO_PEERPIDFD の無い kernel (< 6.5)")
WHOAMI = [{"op": "call", "call": "status"}]


def _layout_spec(built, keys: Path, principal: str = "ops-web", steps=WHOAMI, **extra):
    return {"principal": principal, "socket": str(built.socket_path),
            "lock": str(built.lock_path), "uid": built.uid,
            "key_file": str(keys / f"{principal}.token"), "ready_file": str(keys / ".ready"),
            "steps": steps, **extra}


def _rejected(rc: int, stderr: str) -> str:
    """固定 reason で非零終了したことを確かめて reason を返す。"""
    assert rc == 3, stderr
    match = re.fullmatch(r"afx-ops: ([a-z_]+)\n", stderr)
    assert match, stderr
    return match.group(1)


@needs_pidfd
def test_baseline_sends_token_after_every_check(tmp_path, child_env):
    keys = build_key_dir(tmp_path / "keys")
    with build(CASES["baseline_lock_holder"]["setup"], tmp_path) as built:
        rc, lines, err = run_client(tmp_path, _layout_spec(built, keys), child_env)
        got = received(built.record, expect_any=True)
    assert rc == 0, err
    assert lines == [{"op": "call", "ok": True, "status": 200}]
    assert token_in(got, "ops-web")


FORGERIES = ["lock_not_held", "lock_held_by_another_process", "lock_inode_replaced",
             "parent_dir_mode_0755", "socket_path_is_symlink", "owner_differs_from_client_uid",
             "parent_dir_is_symlink", "socket_is_regular_file"]


@needs_pidfd
@pytest.mark.parametrize("case_id", FORGERIES)
def test_each_single_axis_forgery_sends_nothing(tmp_path, child_env, case_id):
    case = CASES[case_id]
    keys = build_key_dir(tmp_path / "keys")
    with build(case["setup"], tmp_path) as built:
        rc, lines, err = run_client(tmp_path, _layout_spec(built, keys), child_env)
        got = received(built.record, expect_any=False)
    assert _rejected(rc, err) == case["expect"]["reason"]
    assert lines == []
    assert sum(map(len, got)) == 0


@needs_pidfd
def test_admission_requires_pidfd_pid_match(tmp_path, child_env):
    # listen と lock 取得をした親は exit 済みで、子が accept を継いでいる。
    # peer の pidfd は終了済み (fdinfo の Pid が -1) で peer pid と一致しない。
    layout = make_layout(tmp_path)
    with daemon(layout, "orphan"):
        rc, lines, err = run_client(layout, layout.spec("ops-web", WHOAMI), child_env)
    assert _rejected(rc, err) == "peer_gone"
    assert layout.bytes_received() == 0


@needs_pidfd
def test_zombie_peer_is_rejected_by_pidfd_poll(tmp_path, child_env):
    # 親が終了して回収前 (zombie)。fdinfo の pid は一致し、/proc/<pid> も残るが、
    # pidfd の POLLIN で保持者の終了と分かる。
    layout = make_layout(tmp_path)
    with daemon(layout, "parent_waits") as started:
        os.kill(started.listen_pid, signal.SIGKILL)
        assert wait_until(lambda: _state(started.listen_pid) == "Z")
        assert os.path.isdir(f"/proc/{started.listen_pid}")
        rc, lines, err = run_client(layout, layout.spec("ops-web", WHOAMI), child_env)
    assert _rejected(rc, err) == "holder_gone"
    assert layout.bytes_received() == 0


def _state(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat") as file:
            return file.read().rsplit(") ", 1)[1][0]
    except OSError:
        return None


@needs_pidfd
def test_pidfd_pollin_before_send_sends_nothing(tmp_path, child_env):
    layout = make_layout(tmp_path)
    opened, go = tmp_path / "opened", tmp_path / "go"
    steps = [{"op": "open_wait_send", "opened": str(opened), "go": str(go),
              "expect_error": True}]
    with daemon(layout, "parent_waits") as started:
        proc = spawn_client(layout, layout.spec("ops-web", steps), child_env)
        try:
            # 認証を全部通した後、送信の直前で止まっている間に peer (親) を終了させる。
            assert wait_until(opened.exists)
            os.kill(started.listen_pid, signal.SIGKILL)
            assert wait_until(lambda: _state(started.listen_pid) == "Z")
            go.touch()
        finally:
            rc, lines, err = finish(proc)
    assert rc == 0, err
    assert lines == [{"op": "open_wait_send", "ok": False, "reason": "peer_gone",
                      "token_sent": False}]
    assert layout.bytes_received() == 0


@needs_pidfd
def test_pidfd_pollin_during_request_fails_without_success(tmp_path, child_env):
    # daemon の子は要求を読んだ後、親 (peer) を殺してから 200 を返す。
    layout = make_layout(tmp_path)
    steps = [{"op": "call", "call": "status"}]
    with daemon(layout, "kill_parent_before_reply"):
        rc, lines, err = run_client(layout, layout.spec("ops-web", steps), child_env)
        got = layout.received(expect_any=True)
    assert _rejected(rc, err) == "response_lost"
    assert lines == []
    # 鍵は届いていて (送信時は相手が生きていた)、応答は成功として扱われていない。
    assert token_in(got, "ops-web")


def _fake_proc_locks(tmp_path: Path, lock_path: Path, *, dev: str) -> Path:
    """実 /proc/locks の写しで、instance lock の行だけ装置番号を差し替えた入力 file。"""
    st = os.stat(lock_path)
    real = (f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}")
    text = Path("/proc/locks").read_text()
    assert f" {real} " in text
    path = tmp_path / "proc-locks"
    path.write_text(text.replace(f" {real} ", f" {dev}:{st.st_ino} "))
    return path


@needs_pidfd
def test_dev_mismatch_is_rejected_by_admission_even_if_shared_oracle_allows(
        tmp_path, child_env):
    layout = make_layout(tmp_path)
    with daemon(layout):
        fake = _fake_proc_locks(tmp_path, layout.lock_path, dev="fe:fe")
        rc, lines, err = run_client(layout, layout.spec("ops-web", WHOAMI,
                                                        proc_locks=str(fake)), child_env)
        assert _rejected(rc, err) == "instance_lock_dev_mismatch"
        assert layout.bytes_received() == 0
        # 同じ入力で共有 verifier だけなら readlink の救済で通り、鍵を送る。
        rc, lines, err = run_client(layout, layout.spec(
            "ops-web", [{"op": "shared"}], proc_locks=str(fake)), child_env)
        got = layout.received(expect_any=True)
    assert rc == 0, err
    assert lines == [{"op": "shared", "ok": True}]
    assert token_in(got, "ops-web")


def _other_fs_row_with_same_inode(tmp_path: Path, lock_path: Path,
                                  shm_file: Path) -> Path:
    """実 /proc/locks に、別 fs (tmpfs) の file の実 flock 行を instance lock と同じ inode
    番号に書き換えて足した入力 file。inode 番号は fs をまたぐと重複し得る。"""
    st, shm = os.stat(lock_path), os.stat(shm_file)
    text = Path("/proc/locks").read_text()
    shm_key = f"{os.major(shm.st_dev):02x}:{os.minor(shm.st_dev):02x}:{shm.st_ino}"
    rows = [line for line in text.splitlines() if f" {shm_key} " in line]
    assert len(rows) == 1, rows
    row = rows[0].replace(f" {shm_key} ", f" {shm_key.rsplit(':', 1)[0]}:{st.st_ino} ")
    path = tmp_path / "proc-locks-other-fs"
    path.write_text(text + row + "\n")
    return path


@needs_pidfd
def test_a_same_inode_lock_on_another_filesystem_does_not_block_the_real_daemon(
        tmp_path, child_env):
    import fcntl
    import tempfile
    shm_dir = Path("/dev/shm")
    if not shm_dir.is_dir():
        pytest.skip("/dev/shm が無い")
    layout = make_layout(tmp_path)
    with daemon(layout), tempfile.NamedTemporaryFile(dir=shm_dir) as other:
        if os.stat(other.name).st_dev == os.stat(layout.lock_path).st_dev:
            pytest.skip("一時領域と /dev/shm が同じ fs")
        fcntl.flock(other.fileno(), fcntl.LOCK_EX)
        fake = _other_fs_row_with_same_inode(tmp_path, layout.lock_path, Path(other.name))
        rc, lines, err = run_client(layout, layout.spec("ops-web", WHOAMI,
                                                        proc_locks=str(fake)), child_env)
        got = layout.received(expect_any=True)
    assert rc == 0, err
    assert lines == [{"op": "call", "ok": True, "status": 200}]
    assert token_in(got, "ops-web")


x86_only = pytest.mark.skipif(platform.machine() != "x86_64",
                              reason="試験用 seccomp filter は x86_64 の番号で書いている")


@needs_pidfd
@x86_only
def test_pidfd_unavailable_is_rejected_with_fixed_reason(tmp_path, child_env):
    layout = make_layout(tmp_path)
    with daemon(layout):
        rc, lines, err = run_client(layout, layout.spec("ops-web", WHOAMI,
                                                        seccomp_no_pidfd=True), child_env)
        assert _rejected(rc, err) == "ops_requires_so_peerpidfd"
        assert layout.bytes_received() == 0
        # 同じ kernel 応答で共有 verifier だけなら pidfd を省略して通る。
        rc, lines, err = run_client(layout, layout.spec("ops-web", [{"op": "shared"}],
                                                        seccomp_no_pidfd=True), child_env)
        got = layout.received(expect_any=True)
    assert rc == 0, err
    assert lines == [{"op": "shared", "ok": True}]
    assert token_in(got, "ops-web")


@pytest.mark.skipif(pidfd_supported(), reason="SO_PEERPIDFD のある kernel では起きない")
def test_kernel_without_pidfd_is_refused_by_the_ops_path(tmp_path, child_env):
    layout = make_layout(tmp_path)
    with daemon(layout):
        rc, lines, err = run_client(layout, layout.spec("ops-web", WHOAMI), child_env)
    assert _rejected(rc, err) == "ops_requires_so_peerpidfd"
    assert layout.bytes_received() == 0


def _landlock_abi() -> int:
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc.syscall(444, None, ctypes.c_size_t(0), ctypes.c_uint32(1))


@needs_pidfd
@pytest.mark.skipif(_landlock_abi() < 1, reason="Landlock が使えない")
def test_proc_locks_unreadable_sends_nothing(tmp_path, child_env):
    layout = make_layout(tmp_path)
    steps = [{"op": "proc_locks_readable"}, {"op": "call", "call": "status"}]
    with daemon(layout):
        rc, lines, err = run_client(layout, layout.spec(
            "ops-web", steps, landlock_no_proc_locks=True,
            landlock_roots=[str(AFX_OPS)]), child_env)
    # Landlock が実際に /proc/locks を拒んでいること (空振りの緑でないこと)。
    assert lines == [{"op": "proc_locks_readable", "readable": False}]
    assert _rejected(rc, err) == "not_lock_holder"
    assert layout.bytes_received() == 0
