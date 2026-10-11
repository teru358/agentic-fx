"""隔離の試験で使う共通部品 (実子 process の起動、一時 root、skip 条件)。"""
from __future__ import annotations

import json
import os
import platform
import resource
import ctypes
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
# 鍵 dir と fake daemon の部品は tests 直下にある
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from _ops_harness import Layout, make_layout  # noqa: E402
from afx_ops.isolation import landlock  # noqa: E402
CANARY = HERE / "afx_isolation_canary.py"
FEATURES = ("bot", "web", "notify")
PRINCIPAL = {"bot": "ops-bot", "web": "ops-web", "notify": "ops-notify"}

needs_x86_64 = pytest.mark.skipif(
    sys.platform != "linux" or platform.machine() != "x86_64",
    reason="afx-ops の隔離は x86_64 Linux 専用")
needs_landlock = pytest.mark.skipif(
    landlock.abi_version() < landlock.REQUIRED_ABI,
    reason=f"Landlock ABI {landlock.REQUIRED_ABI} 以上が要る (この host は "
           f"{landlock.abi_version()})")
needs_strace = pytest.mark.skipif(shutil.which("strace") is None, reason="strace が無い")


def no_core() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


# 機能の secret file と中身 (web は secret を持たない)。中身は repr・出力に出ないことの目印
SECRET_FILES = {"bot": "discord.token", "notify": "webhook.url"}
SECRET_TEXT = {"bot": "discord-secret-of-bot-7f3a", "notify": "https://hooks.invalid/secret-9c1e"}


def make_root(base: Path) -> Path:
    """機能ごとの設定・secret・状態領域、実 manifest の鍵 dir、instance root を置いた一時 root。

    <root>/<feature>/{config,secret,state}、<root>/keys (全 principal が active)、
    <root>/root (instance root、data/run は 0700)。
    """
    root = base / "afx-root"
    root.mkdir(parents=True)
    make_layout(root)
    for feature in FEATURES:
        (root / feature / "config").mkdir(parents=True)
        # 起動は他者が書ける config・0700 でない state を拒むので、umask に依らず固定する
        (root / feature / "config").chmod(0o755)
        (root / feature / "state").mkdir(parents=True)
        (root / feature / "state").chmod(0o700)
        (root / feature / "config" / "settings.txt").write_text(f"{feature}\n")
        secret = root / feature / "secret"
        secret.mkdir(mode=0o700)
        secret.chmod(0o700)
        if feature in SECRET_FILES:
            path = secret / SECRET_FILES[feature]
            path.write_text(SECRET_TEXT[feature] + "\n")
            path.chmod(0o600)
    return root


def layout_of(root: Path) -> Layout:
    """make_root の root に対応する fake daemon 用の配置。"""
    data = root / "root" / "data"
    return Layout(root, data, data / "run", data / "run" / "api.sock", data / "instance.lock",
                  root / "keys", root / "record")


def child_env(home: Path) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config")}


@dataclass
class CanaryResult:
    rc: int
    stdout: str
    stderr: str

    @property
    def isolated(self) -> bool:
        return "ISOLATED" in self.stdout.splitlines()

    @property
    def token_sent(self) -> bool:
        return "TOKEN-SENT" in self.stdout.splitlines()

    @property
    def result(self) -> list[int]:
        for line in self.stdout.splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        raise AssertionError(f"no RESULT line: rc={self.rc} out={self.stdout!r} "
                             f"err={self.stderr[-2000:]!r}")


def canary_argv(spec: dict, *, prefix: tuple[str, ...] = ()) -> list[str]:
    return [*prefix, sys.executable, "-I", str(CANARY), json.dumps(spec)]


def run_canary(spec: dict, env: dict[str, str], *, pass_fds: tuple[int, ...] = (),
               timeout: float = 60, prefix: tuple[str, ...] = ()) -> CanaryResult:
    done = subprocess.run(canary_argv(spec, prefix=prefix), capture_output=True, text=True,
                          timeout=timeout, env=env, pass_fds=pass_fds, preexec_fn=no_core,
                          stdin=subprocess.DEVNULL)
    return CanaryResult(done.returncode, done.stdout, done.stderr)


_SYS_PIDFD_SEND_SIGNAL = 424
_SYS_PIDFD_OPEN = 434


def _syscall(number: int, *args: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(number, *[ctypes.c_long(arg) for arg in args])
    if result < 0:
        raise OSError(ctypes.get_errno(), "syscall")
    return result


def _pidfd_open(pid: int) -> int:
    """標準の os に無い build でも pidfd を開く (x86_64 の syscall 番号)。"""
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    return _syscall(_SYS_PIDFD_OPEN, pid, 0)


def _pidfd_kill(pidfd: int) -> None:
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
    else:
        _syscall(_SYS_PIDFD_SEND_SIGNAL, pidfd, int(signal.SIGKILL), 0, 0)


class Interactive:
    """stdin で段を進める canary。止めるときは pid を指定して kill し、回収する。"""

    def __init__(self, spec: dict, env: dict[str, str], *,
                 prefix: tuple[str, ...] = ()) -> None:
        self.proc = subprocess.Popen(canary_argv(spec, prefix=prefix), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env=env, preexec_fn=no_core,
                                     start_new_session=True)
        self.lines: list[str] = []
        self._pgid = self.proc.pid

    @property
    def pid(self) -> int:
        return self.proc.pid

    def wait_line(self, prefix: str, timeout: float = 60.0) -> str:
        """prefix で始まる行を待つ。期限までに来なければ子を止めて TimeoutError にする。"""
        assert self.proc.stdout is not None
        expired = threading.Event()
        # 子は start_new_session で自分の group の leader なので、group id は子の pid。
        # 起動時の値を使う (親が先に終わっても、stdout を握る子孫の group を指せる)
        group = getattr(self, "_pgid", None)
        # pidfd を握っている間は子の pid が再利用されない。group を先に撃ち、その後で直接の子を撃つ
        # 既に回収された子 (returncode が確定) の pid は別の process のものかもしれないので開かない
        pidfd = -1
        if self.proc.returncode is None:
            try:
                pidfd = _pidfd_open(self.proc.pid)
            except OSError:
                pidfd = -1

        def expire() -> None:
            expired.set()
            if group is not None:
                try:
                    os.killpg(group, signal.SIGKILL)
                except OSError:
                    pass
            if pidfd >= 0:
                try:
                    _pidfd_kill(pidfd)
                except OSError:
                    pass

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    if expired.is_set():
                        raise TimeoutError(f"no {prefix!r} within {timeout}s: {self.lines}")
                    err = self.proc.stderr.read() if self.proc.stderr else ""
                    raise AssertionError(f"canary ended before {prefix!r}: {self.lines} {err}")
                line = line.rstrip("\n")
                self.lines.append(line)
                if line.startswith(prefix):
                    return line
        finally:
            timer.cancel()
            if pidfd >= 0:
                os.close(pidfd)

    def send(self, text: str = "go") -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    def finish(self, timeout: float = 30) -> CanaryResult:
        out, err = self.proc.communicate(timeout=timeout)
        self.lines += out.splitlines()
        return CanaryResult(self.proc.returncode, "\n".join(self.lines), err)

    def kill(self) -> None:
        if self.proc.poll() is None:
            group = getattr(self, "_pgid", None)
            if group is not None:
                try:
                    os.killpg(group, signal.SIGKILL)
                except OSError:
                    pass
            os.kill(self.proc.pid, 9)
        try:
            self.proc.communicate(timeout=10)
        except (ValueError, subprocess.TimeoutExpired):
            pass
