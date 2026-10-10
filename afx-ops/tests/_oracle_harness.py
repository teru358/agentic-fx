"""oracle の setup を実 UDS・実 instance lock・実 fake daemon で組み立てる装置。

verifier は呼ばない (組み立てだけ)。probe を別 process で起こす入口 (`run_probe`) は
test_server_auth_oracle.py が使う。
"""
from __future__ import annotations

import fcntl
import json
import os
import resource
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ORACLE = HERE / "fixtures" / "server_auth_oracle.json"
DAEMON = HERE / "_oracle_daemon.py"
TOKEN = "oracle-token-0123456789"


def load_cases() -> list[dict]:
    return json.loads(ORACLE.read_text(encoding="utf-8"))["cases"]


def no_core() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


@dataclass
class Built:
    setup: dict
    data: Path
    run_dir: Path
    real_dir: Path
    socket_path: Path       # probe へ渡す path (client が見る path)
    bound_path: Path | None  # daemon が bind した実体の path
    lock_path: Path
    record: Path
    token_file: Path
    uid: int
    daemon_pid: int | None = None
    daemon_parent_pid: int | None = None
    other_lock: object | None = None
    extra: dict = field(default_factory=dict)


def start_daemon(socket_path: Path, record: Path, *, lock_dir: Path | None, orphan: bool,
                 mode: str = "answer") -> tuple[subprocess.Popen, int, int]:
    """(Popen, 受け付ける pid, listen した pid)。orphan は親を回収してから返す。"""
    args = [sys.executable, str(DAEMON), str(socket_path), str(record),
            str(lock_dir) if lock_dir is not None else "-", mode]
    if orphan:
        args.append("orphan")
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, text=True, preexec_fn=no_core)
    word, serve_pid, listen_pid = proc.stdout.readline().split()
    assert word == "ready"
    if orphan:
        assert proc.wait(10) == 0
    return proc, int(serve_pid), int(listen_pid)


def stop_daemon(proc: subprocess.Popen | None, pid: int | None) -> None:
    if pid is not None:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc is not None:
        if proc.poll() is None:
            proc.wait(5)
        proc.stdout.close()


@contextmanager
def build(setup: dict, tmp: Path, *, mode: str = "answer"):
    """setup を組み立て、終わったら daemon を pid 指定で止めて lock を放す。"""
    root = tmp / "root"
    data = root / "data"
    run_dir = data / "run"
    data.mkdir(parents=True)
    if setup["dir"] == "symlink":
        real_dir = tmp / "real-run"
        real_dir.mkdir(mode=0o700)
        run_dir.symlink_to(real_dir)
    else:
        run_dir.mkdir(mode=0o700)
        real_dir = run_dir
    os.chmod(real_dir, 0o700)
    bound = real_dir / "api.sock"
    if setup["socket"] == "symlink_to_socket":
        elsewhere = tmp / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        bound = elsewhere / "real.sock"
        (run_dir / "api.sock").symlink_to(bound)
    token_file = tmp / "token"
    token_file.write_text(TOKEN + "\n", encoding="ascii")
    built = Built(setup=setup, data=data, run_dir=run_dir, real_dir=real_dir,
                  socket_path=run_dir / "api.sock", bound_path=None,
                  lock_path=data / "instance.lock", record=tmp / "record",
                  token_file=token_file,
                  uid=os.getuid() if setup["client_uid"] == "self" else os.getuid() + 1)
    proc = None
    try:
        if setup["socket"] == "regular_file":
            (run_dir / "api.sock").write_text("")
            os.chmod(run_dir / "api.sock", 0o600)
        else:
            if setup["server_lock"] == "other_process":
                # daemon とは別 process (この process) が保持者になる。
                built.other_lock = open(data / "instance.lock", "w")
                fcntl.flock(built.other_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_dir = data if setup["server_lock"] in ("held", "replaced_inode") else None
            proc, built.daemon_pid, built.daemon_parent_pid = start_daemon(
                bound, built.record, lock_dir=lock_dir, mode=mode,
                orphan=setup["server_process"] == "exited_after_listen")
            built.bound_path = bound
            if setup["server_lock"] == "replaced_inode":
                fresh = data / "instance.lock.fresh"
                fresh.write_text("")
                os.replace(fresh, data / "instance.lock")
        os.chmod(real_dir, int(setup["dir_mode"], 8))
        yield built
    finally:
        if built.other_lock is not None:
            built.other_lock.close()
        stop_daemon(proc, built.daemon_pid)


def received(record: Path, *, expect_any: bool) -> list[bytes]:
    deadline = time.monotonic() + (5.0 if expect_any else 0.5)
    while True:
        lines = record.read_text().splitlines() if record.exists() else []
        if lines or time.monotonic() >= deadline:
            return [bytes.fromhex(line) for line in lines]
        time.sleep(0.05)


def flock_write_holders(lock_path: Path) -> set[int]:
    """/proc/locks の、この file の FLOCK WRITE 保持者 pid (待ち行は数えない)。"""
    st = lock_path.stat()
    holders = set()
    for line in Path("/proc/locks").read_text().splitlines():
        parts = line.split()
        if len(parts) < 6 or "->" in parts or parts[1] != "FLOCK" or parts[3] != "WRITE":
            continue
        major, minor, inode = parts[5].split(":")
        if int(inode) == st.st_ino and (int(major, 16), int(minor, 16)) == (
                os.major(st.st_dev), os.minor(st.st_dev)):
            holders.add(int(parts[4]))
    return holders


def run_probe(built: Built, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-m", "afx_ops.server_auth_probe", str(built.socket_path),
         str(built.lock_path), str(built.uid), str(built.token_file)],
        capture_output=True, text=True, timeout=30, env=env, preexec_fn=no_core)
