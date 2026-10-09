"""実 daemon process (``afx --daemon`` 相当) を tmp root で起動・停止する部品。"""
from __future__ import annotations

import os
import resource
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml

CHILD = Path(__file__).with_name("_daemon_child.py")
REPO = Path(__file__).resolve().parents[2]


def init_root(root: Path, **api) -> Path:
    """``afx init`` 済みの root を作り、settings の api 節を上書きする。"""
    from tests.test_service_app import _init
    root.mkdir(parents=True, exist_ok=True)
    _init(root)
    (root / "plugins" / ".locks").mkdir(parents=True, exist_ok=True)
    set_settings(root, api={"enabled": True, **api})
    return root


def set_settings(root: Path, **sections) -> None:
    """settings.yaml を深くマージして上書きする (指定しない既存キーは残す)。"""
    path = root / "config" / "settings.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    def merge(into: dict, values: dict) -> None:
        for key, value in values.items():
            if isinstance(value, dict) and isinstance(into.get(key), dict):
                merge(into[key], value)
            else:
                into[key] = value

    merge(data, sections)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def socket_path(root: Path) -> Path:
    return root / "data" / "run" / "api.sock"


def activity(root: Path) -> str:
    path = root / "logs" / "activity.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def base_env() -> dict[str, str]:
    # HOME / XDG は conftest が tmp に向けている。鍵 dir もそこに作られる。
    env = {k: os.environ[k] for k in ("PATH", "LANG") if k in os.environ}
    env["HOME"] = os.environ["HOME"]
    env["XDG_CONFIG_HOME"] = os.environ.get("XDG_CONFIG_HOME",
                                            os.path.join(os.environ["HOME"], ".config"))
    env["PYTHONPATH"] = str(REPO)
    return env


def _no_core() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class Daemon:
    def __init__(self, root: Path, *, extra_env: dict[str, str] | None = None,
                 peer_probe: Path | None = None) -> None:
        self.root = root
        env = base_env()
        env.update(extra_env or {})
        self.output = root / f"daemon-{time.monotonic_ns()}.out"
        args = [sys.executable, str(CHILD), str(root)]
        if peer_probe is not None:
            args += ["--peer-probe", str(peer_probe)]
        self._out = self.output.open("wb")
        # この起動より前の行を数えておく (同じ root で再起動しても前回の行に反応しない)
        existing = activity(root)
        self._baseline = {event: existing.count(f"\t{event}\t")
                          for event in ("api_started", "api_start_failed")}
        self.proc = subprocess.Popen(args, cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                                     stdout=self._out, stderr=subprocess.STDOUT,
                                     preexec_fn=_no_core)

    @property
    def pid(self) -> int:
        return self.proc.pid

    def text(self) -> str:
        return self.output.read_text(encoding="utf-8", errors="replace")

    def wait_activity(self, event: str, *, count: int = 1, timeout: float = 60.0) -> None:
        """activity に event が count 回現れるまで待つ。daemon が先に死んだら失敗。"""
        end = time.monotonic() + timeout
        while activity(self.root).count(f"\t{event}\t") < count:
            if self.proc.poll() is not None:
                raise AssertionError(f"daemon exited rc={self.proc.returncode}:\n{self.text()}")
            if time.monotonic() > end:
                raise AssertionError(f"{event} did not appear:\n{activity(self.root)}")
            time.sleep(0.05)

    def wait_first(self, *events: str, timeout: float = 60.0) -> str:
        """この起動の後に events のどれかが activity に現れるまで待ち、現れたものを返す。"""
        end = time.monotonic() + timeout
        while True:
            text = activity(self.root)
            for event in events:
                if text.count(f"\t{event}\t") > self._baseline.get(event, 0):
                    return event
            if self.proc.poll() is not None:
                raise AssertionError(f"daemon exited rc={self.proc.returncode}:\n{self.text()}")
            if time.monotonic() > end:
                raise AssertionError(f"none of {events} appeared:\n{text}")
            time.sleep(0.05)

    def wait_listening(self, timeout: float = 60.0) -> None:
        self.wait_activity("api_started", count=self._baseline["api_started"] + 1,
                           timeout=timeout)

    def stop(self, timeout: float = 90.0) -> int:
        """SIGTERM で止める (systemd の停止と同じ合図)。終了コードを返す。"""
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            # 止まらない daemon を残さない
            self.proc.kill()
            self.proc.wait(30)
            raise
        finally:
            self._out.close()

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(30)
        self._out.close()
