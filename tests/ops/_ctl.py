"""`afx ctl` を実 UDS + 実 server に向けて動かすための部品。

- ``ctl_api``: 実 server を起動し、試験 process が ``data/instance.lock`` を保持する
  (server の listener と lock 保持者が同じ pid になる、本物の daemon と同じ形)。
- ``run_ctl``: 同じ process で client を呼ぶ (stdin は端末でない)。
- ``run_pty``: 実 process を pty の上で起動し、画面の文字列を待って入力を送る。
"""
from __future__ import annotations

import io
import os
import pty
import resource
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentic_fx.ops import client
from agentic_fx.store.instance_lock import acquire_instance_lock

from ._api import make_api

FAKE_SERVER = Path(__file__).with_name("_fake_uds_server.py")


@pytest.fixture
def ctl_api(ops_env):
    # pty の子は試験 process の子孫なので、service 自身の子孫検査に掛からない pid を渡す。
    api_env = make_api(ops_env, service_pid=0)
    lock = acquire_instance_lock(ops_env.root / "data")
    yield api_env
    for http in api_env.clients:
        http.close()
    api_env.server.stop(5.0)
    lock.close()


def run_ctl(root: Path, argv: list[str], **kwargs) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    rc = client.main(argv, root=root, stdin=io.StringIO(""), stdout=out, stderr=err,
                     **kwargs)
    return rc, out.getvalue(), err.getvalue()


def _no_core() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


@dataclass
class PtyResult:
    rc: int
    output: str


_CHILD_CODE = ("import sys; from agentic_fx.entry import main; "
               "raise SystemExit(main(sys.argv[1:]))")


def run_pty(root: Path, argv: list[str], steps: list[tuple[str, str]], *,
            stdin_pipe: str | None = None, timeout: float = 60.0) -> PtyResult:
    """``afx <argv>`` を pty の上で起動する。

    steps は (待つ文字列, 送る行) の列。stdin_pipe を渡すと、子は制御端末 (/dev/tty)
    を持ったまま stdin だけを pipe に差し替え、その内容を流し込む。
    """
    pid, master = pty.fork()
    if pid == 0:
        try:
            _no_core()
            os.chdir(root)
            if stdin_pipe is not None:
                read_end, write_end = os.pipe()
                os.write(write_end, stdin_pipe.encode())
                os.close(write_end)
                os.dup2(read_end, 0)
                os.close(read_end)
            os.execv(sys.executable, [sys.executable, "-c", _CHILD_CODE, *argv])
        finally:
            os._exit(127)
    output = b""
    deadline = time.monotonic() + timeout
    pending = list(steps)
    cursor = 0
    try:
        while True:
            if pending and pending[0][0].encode() in output[cursor:]:
                _, line = pending.pop(0)
                os.write(master, (line + "\n").encode())
                # 次の待ち文字列は、送った後に出た出力の中だけで探す。
                cursor = len(output)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"pty timeout; output so far:\n{output.decode(errors='replace')}")
            ready, _, _ = select.select([master], [], [], min(0.2, remaining))
            if not ready:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                break
            output += chunk
        _, status = os.waitpid(pid, 0)
        pid = 0
    finally:
        if pid:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(master)
    if pending:
        raise AssertionError(f"unmet steps {pending}; output:\n{output.decode(errors='replace')}")
    return PtyResult(os.waitstatus_to_exitcode(status), output.decode(errors="replace"))


class FakeServer:
    """別 process の偽 server。受信 bytes を接続ごとに記録する。"""

    def __init__(self, socket_path: Path, record: Path, *, hold_lock_dir: Path | None = None):
        self.record = record
        socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(socket_path.parent, 0o700)
        args = [sys.executable, str(FAKE_SERVER), str(socket_path), str(record)]
        if hold_lock_dir is not None:
            args.append(str(hold_lock_dir))
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, text=True,
                                     preexec_fn=_no_core)
        assert self.proc.stdout.readline().strip() == "ready"

    def connections(self, expected: int = 1, timeout: float = 5.0) -> list[bytes]:
        deadline = time.monotonic() + timeout
        while True:
            lines = self.record.read_text().splitlines() if self.record.exists() else []
            if len(lines) >= expected or time.monotonic() >= deadline:
                return [bytes.fromhex(line) for line in lines]
            time.sleep(0.05)

    def stop(self) -> None:
        self.proc.kill()
        self.proc.wait(5)
        self.proc.stdout.close()
