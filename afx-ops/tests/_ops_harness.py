"""ops 要求経路の試験装置: 実 UDS の fake daemon と、別 process の ops client。"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from _keydir import build_key_dir, token_for
from _oracle_harness import DAEMON, no_core, received

HERE = Path(__file__).resolve().parent
CLIENT = HERE / "_ops_client.py"


def pidfd_supported() -> bool:
    """この kernel が SO_PEERPIDFD を返すか (自前の socketpair で確かめる)。"""
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        fd = left.getsockopt(socket.SOL_SOCKET, 77)
    except OSError:
        return False
    finally:
        left.close()
        right.close()
    os.close(fd)
    return True


@dataclass
class Layout:
    tmp: Path
    data: Path
    run_dir: Path
    socket_path: Path
    lock_path: Path
    keys: Path
    record: Path

    def spec(self, principal: str, steps: list[dict], **extra) -> dict:
        return {"principal": principal, "socket": str(self.socket_path),
                "lock": str(self.lock_path), "key_file": str(self.keys / f"{principal}.token"),
                "ready_file": str(self.keys / ".ready"), "steps": steps, **extra}

    def received(self, *, expect_any: bool) -> list[bytes]:
        return received(self.record, expect_any=expect_any)

    def received_at_least(self, count: int, limit: float = 5.0) -> list[bytes]:
        """接続 count 本分の記録が揃うまで待つ (daemon は接続を閉じた後に書く)。"""
        wait_until(lambda: len(received(self.record, expect_any=False)) >= count, limit)
        return received(self.record, expect_any=False)

    def bytes_received(self) -> int:
        return sum(len(chunk) for chunk in received(self.record, expect_any=False))


def make_layout(tmp: Path) -> Layout:
    data = tmp / "root" / "data"
    run_dir = data / "run"
    run_dir.mkdir(parents=True)
    os.chmod(run_dir, 0o700)
    keys = build_key_dir(tmp / "keys")
    return Layout(tmp, data, run_dir, run_dir / "api.sock", data / "instance.lock", keys,
                  tmp / "record")


@dataclass
class Daemon:
    proc: subprocess.Popen
    serve_pid: int
    listen_pid: int


def start(layout: Layout, *flags: str, lock: bool = True, mode: str = "answer") -> Daemon:
    args = [sys.executable, str(DAEMON), str(layout.socket_path), str(layout.record),
            str(layout.data) if lock else "-", mode, *flags]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, text=True, preexec_fn=no_core)
    word, serve_pid, listen_pid = proc.stdout.readline().split()
    assert word == "ready"
    if "orphan" in flags and "parent_waits" not in flags:
        assert proc.wait(10) == 0
    return Daemon(proc, int(serve_pid), int(listen_pid))


def stop(daemon: Daemon | None) -> None:
    if daemon is None:
        return
    for pid in {daemon.serve_pid, daemon.listen_pid}:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    daemon.proc.wait(10)
    daemon.proc.stdout.close()
    # orphan の子は init に引き取られる。終了を見届ける。
    deadline = time.monotonic() + 5
    while os.path.exists(f"/proc/{daemon.serve_pid}") and time.monotonic() < deadline:
        try:
            with open(f"/proc/{daemon.serve_pid}/stat") as file:
                if file.read().split(") ")[-1].startswith("Z"):
                    break
        except OSError:
            break
        time.sleep(0.02)


@contextmanager
def daemon(layout: Layout, *flags: str, lock: bool = True):
    started = start(layout, *flags, lock=lock)
    try:
        yield started
    finally:
        stop(started)


def _write_spec(where: Layout | Path, spec: dict) -> Path:
    """spec を一時 file に書く。where は Layout か、書いてよい dir。"""
    path = getattr(where, "tmp", where) / f"spec-{time.monotonic_ns()}.json"
    path.write_text(json.dumps(spec))
    return path


def client_command(layout: Layout | Path, spec: dict, *, prefix: list[str] = ()) -> list[str]:
    return [*prefix, sys.executable, "-I", str(CLIENT), str(_write_spec(layout, spec))]


def run_client(layout: Layout | Path, spec: dict, env: dict, *, prefix: list[str] = (),
               timeout: float = 60) -> tuple[int, list[dict], str]:
    done = subprocess.run(client_command(layout, spec, prefix=prefix), capture_output=True,
                          text=True, timeout=timeout, env=env, preexec_fn=no_core)
    lines = [json.loads(line) for line in done.stdout.splitlines() if line.startswith("{")]
    return done.returncode, lines, done.stderr


def spawn_client(layout: Layout | Path, spec: dict, env: dict) -> subprocess.Popen:
    return subprocess.Popen(client_command(layout, spec), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env, preexec_fn=no_core)


def finish(proc: subprocess.Popen, timeout: float = 30) -> tuple[int, list[dict], str]:
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return proc.returncode, lines, err


def token_in(chunks: list[bytes], principal: str) -> bool:
    needle = b"Authorization: Bearer " + token_for(principal).encode()
    return any(needle in chunk for chunk in chunks)


def wait_until(predicate, limit: float = 10.0) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()
