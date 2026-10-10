"""AC-30: client は接続先が同 uid の instance lock 保持者だと確かめてから鍵を送る。

偽 server は別 process で、受け取った bytes を接続ごとに記録する。鍵 (Authorization)
が届いていないことを server 側の記録で確かめる。
"""
from __future__ import annotations

import json
import os
import resource
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentic_fx.ops import client, keys
from agentic_fx.store.instance_lock import acquire_instance_lock

from ._ctl import FakeServer, ctl_api, run_ctl  # noqa: F401


@pytest.fixture
def fake_env(ops_env, tmp_path):
    keys.ensure_initialized(keys.key_dir(ops_env.root), root=ops_env.root)
    servers = []

    def start(*, hold_lock: bool) -> FakeServer:
        server = FakeServer(ops_env.root / "data" / "run" / "api.sock",
                            tmp_path / f"record{len(servers)}",
                            hold_lock_dir=ops_env.root / "data" if hold_lock else None)
        servers.append(server)
        return server

    yield ops_env, start
    for server in servers:
        server.stop()


def _token(env) -> bytes:
    return keys.read_token(keys.key_dir(env.root), keys.Principal.OPERATOR).encode()


def test_real_lock_holder_receives_the_request(ctl_api):
    rc, out, err = run_ctl(ctl_api.env.root, ["whoami"])
    assert rc == 0, err
    assert "operator" in out


def test_fake_server_that_holds_the_lock_does_receive_the_key(fake_env):
    """記録の仕組みの対照: 保持者を装えば鍵は届く (= 残余リスクとして既知)。"""
    env, start = fake_env
    server = start(hold_lock=True)
    rc, _, _ = run_ctl(env.root, ["whoami"])
    assert rc == 3
    received = server.connections()
    assert received and b"Authorization: Bearer " + _token(env) in received[0]


def test_fake_server_while_the_real_holder_is_another_process(fake_env):
    env, start = fake_env
    lock = acquire_instance_lock(env.root / "data")
    try:
        server = start(hold_lock=False)
        rc, _, err = run_ctl(env.root, ["whoami"])
    finally:
        lock.close()
    assert rc == 3
    assert "鍵を送りませんでした" in err
    assert server.connections() == [b""]


def test_fake_server_when_nobody_holds_the_lock(fake_env):
    env, start = fake_env
    server = start(hold_lock=False)
    rc, _, err = run_ctl(env.root, ["whoami"])
    assert rc == 3
    assert "鍵を送りませんでした" in err
    assert server.connections() == [b""]


def test_socket_dir_0755_is_refused_before_connecting(fake_env):
    env, start = fake_env
    server = start(hold_lock=True)
    os.chmod(env.root / "data" / "run", 0o755)
    rc, _, err = run_ctl(env.root, ["whoami"])
    assert rc == 3
    assert "鍵を送りませんでした" in err
    assert server.connections(expected=1, timeout=0.5) == []


def test_owner_uid_mismatch_is_refused(fake_env):
    """別 uid は root なしでは作れないので、期待 uid を変えて同じ判定を通す。"""
    env, start = fake_env
    server = start(hold_lock=True)
    rc, _, err = run_ctl(env.root, ["whoami"], uid=os.getuid() + 1)
    assert rc == 3
    assert "鍵を送りませんでした" in err
    assert server.connections(expected=1, timeout=0.5) == []


def test_peer_uid_mismatch_is_refused_on_a_connected_socket(fake_env):
    env, start = fake_env
    start(hold_lock=True)
    import socket
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(env.root / "data" / "run" / "api.sock"))
    try:
        client.verify_peer(sock, uid=os.getuid(), lock_path=env.root / "data" / "instance.lock")
        with pytest.raises(client.ServerAuthError):
            client.verify_peer(sock, uid=os.getuid() + 1,
                               lock_path=env.root / "data" / "instance.lock")
    finally:
        sock.close()


def test_symlinked_socket_path_is_refused(fake_env, tmp_path):
    env, start = fake_env
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    server = FakeServer(elsewhere / "real.sock", tmp_path / "sym-record",
                        hold_lock_dir=env.root / "data")
    try:
        run_dir = env.root / "data" / "run"
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (run_dir / "api.sock").symlink_to(elsewhere / "real.sock")
        rc, _, err = run_ctl(env.root, ["whoami"])
        assert rc == 3
        assert "鍵を送りませんでした" in err
        assert server.connections(expected=1, timeout=0.5) == []
    finally:
        server.stop()


def test_lock_holders_reads_the_real_flock_holder(tmp_path):
    lock_path = tmp_path / "instance.lock"
    lock_path.touch()
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl,sys,time; f=open(sys.argv[1],'w'); "
         "fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB); print('held', flush=True); "
         "sys.stdin.read()", str(lock_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert client.lock_holders(lock_path) == {holder.pid}
    finally:
        holder.stdin.close()
        holder.wait(5)
        holder.stdout.close()
    assert client.lock_holders(lock_path) == set()


def test_lock_holders_ignores_waiters_and_other_files(tmp_path):
    lock_path = tmp_path / "instance.lock"
    lock_path.touch()
    st = os.stat(lock_path)
    dev = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    other = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino + 1}"
    proc = tmp_path / "locks"
    proc.write_text(
        f"1: FLOCK  ADVISORY  WRITE 100 {dev} 0 EOF\n"
        f"1: -> FLOCK  ADVISORY  WRITE 200 {dev} 0 EOF\n"
        f"2: FLOCK  ADVISORY  WRITE 300 {other} 0 EOF\n"
        f"3: POSIX  ADVISORY  WRITE 400 {dev} 0 EOF\n"
        f"4: FLOCK  ADVISORY  READ 500 {dev} 0 EOF\n")
    assert client.lock_holders(lock_path, proc_locks=proc) == {100}


# ------------------------------------------------ 機械可読 oracle (fixtures/server_auth_oracle.json)

ORACLE = Path(__file__).with_name("fixtures") / "server_auth_oracle.json"
ORACLE_SERVER = Path(__file__).with_name("_auth_oracle_server.py")


def _oracle_params():
    cases = json.loads(ORACLE.read_text(encoding="utf-8"))["cases"]
    return [pytest.param(case, id=case["id"], marks=() if case["realizable"] else
                         pytest.mark.skip(reason=case["unrealizable_because"]))
            for case in cases]


def _no_core() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _start_oracle_server(socket_path: Path, record: Path, *, lock_dir: Path | None,
                         orphan: bool, reap: bool = True) -> tuple[subprocess.Popen, int]:
    args = [sys.executable, str(ORACLE_SERVER), str(socket_path), str(record),
            str(lock_dir) if lock_dir is not None else "-", "answer"]
    if orphan:
        args.append("orphan")
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, text=True, preexec_fn=_no_core)
    word, pid = proc.stdout.readline().split()
    assert word == "ready"
    if orphan and reap:
        # listen した親を回収し、peer の pid を「存在しない」にする。
        assert proc.wait(10) == 0
    elif orphan:
        # 回収しない: 親は exit 済みの zombie のまま残り、/proc/<pid> は存在する。
        _wait_zombie(proc.pid)
    return proc, int(pid)


def _wait_zombie(pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0]
        except OSError:
            state = ""
        if state == "Z":
            return
        time.sleep(0.02)
    raise AssertionError("listen した process が zombie にならなかった")


def _received(record: Path, *, expect_any: bool) -> list[bytes]:
    deadline = time.monotonic() + (5.0 if expect_any else 0.5)
    while True:
        lines = record.read_text().splitlines() if record.exists() else []
        if lines or time.monotonic() >= deadline:
            return [bytes.fromhex(line) for line in lines]
        time.sleep(0.05)


@pytest.mark.parametrize("case", _oracle_params())
def test_server_auth_oracle_case(ops_env, tmp_path, case):
    keys.ensure_initialized(keys.key_dir(ops_env.root), root=ops_env.root)
    setup, expect = case["setup"], case["expect"]
    data = ops_env.root / "data"
    run_dir = data / "run"
    assert not run_dir.exists()
    if setup["dir"] == "symlink":
        real_dir = tmp_path / "real-run"
        real_dir.mkdir(mode=0o700)
        run_dir.symlink_to(real_dir)
    else:
        run_dir.mkdir(mode=0o700)
        real_dir = run_dir
    os.chmod(real_dir, 0o700)
    socket_path = real_dir / "api.sock"
    if setup["socket"] == "symlink_to_socket":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        socket_path = elsewhere / "real.sock"
        (run_dir / "api.sock").symlink_to(socket_path)
    record = tmp_path / "record"
    proc, server_pid, other_lock = None, None, None
    try:
        if setup["socket"] == "regular_file":
            (run_dir / "api.sock").write_text("")
            os.chmod(run_dir / "api.sock", 0o600)
        else:
            if setup["server_lock"] == "other_process":
                other_lock = acquire_instance_lock(data)
            lock_dir = data if setup["server_lock"] in ("held", "replaced_inode") else None
            proc, server_pid = _start_oracle_server(
                socket_path, record, lock_dir=lock_dir,
                orphan=setup["server_process"] in ("exited_after_listen",
                                                   "zombie_after_listen"),
                reap=setup["server_process"] != "zombie_after_listen")
            if setup["server_lock"] == "replaced_inode":
                fresh = data / "instance.lock.fresh"
                fresh.write_text("")
                os.replace(fresh, data / "instance.lock")
        os.chmod(real_dir, int(setup["dir_mode"], 8))
        uid = os.getuid() if setup["client_uid"] == "self" else os.getuid() + 1
        rc, out, err = run_ctl(ops_env.root, ["whoami"], uid=uid)
        received = _received(record, expect_any=expect["key_sent"])
    finally:
        if other_lock is not None:
            other_lock.close()
        if server_pid is not None:
            os.kill(server_pid, signal.SIGKILL)
        if proc is not None:
            if proc.poll() is None:
                proc.wait(5)
            proc.stdout.close()
    assert rc == expect["rc"], err
    if expect["key_sent"]:
        assert received and b"Authorization: Bearer " + _token(ops_env) in received[0]
        assert "principal: operator" in out
    else:
        assert sum(len(chunk) for chunk in received) == expect["pre_auth_bytes"]
        assert "鍵を送りませんでした" in err
        assert f"({expect['reason']})" in err
