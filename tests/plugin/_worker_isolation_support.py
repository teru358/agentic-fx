"""worker_isolation の実子 process テストが共有する helper。

隔離 (Landlock・seccomp・rlimit・keyring) は不可逆なので、pytest の process には掛けない。
子は `_worker_isolation_child.py` を `python -P` で起こし、`RLIMIT_CORE=0` を子自身が掛ける。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agentic_fx.core import landlock, seccomp
from agentic_fx.plugin import version_store

REPO = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).with_name("_worker_isolation_child.py")
EXAMPLES = REPO / "docs" / "examples" / "plugins"
NONCE = "0123456789abcdef0123456789abcdef"
SIGSYS = 31


def _sandbox_reason() -> str | None:
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        return "x86_64 Linux only"
    if landlock.landlock_abi() < 3:
        return "Landlock ABI 3+ required"
    if seccomp.probe_support().reason is not None:
        return "seccomp TSYNC/LOG required"
    return None


needs_sandbox = pytest.mark.skipif(_sandbox_reason() is not None,
                                   reason=_sandbox_reason() or "")


def copy_plugin(name: str, dest_root: Path, *, as_name: str | None = None) -> Path:
    """examples の plugin を `dest_root` に複製する (plugin.py・config.yaml・test_plugin.py)。"""
    dest = dest_root / (as_name or name)
    dest.mkdir(parents=True)
    for f in ("plugin.py", "config.yaml", "test_plugin.py"):
        src = EXAMPLES / name / f
        if src.exists():
            shutil.copyfile(src, dest / f)
    return dest


def write_plugin(dest: Path, source: str | bytes, config: str = "kind: indicator\n") -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    data = source.encode("utf-8") if isinstance(source, str) else source
    (dest / "plugin.py").write_bytes(data)
    (dest / "config.yaml").write_text(config, encoding="utf-8")
    return dest


def chash(plugin_dir: Path) -> str:
    return version_store.content_hash_bytes((plugin_dir / "plugin.py").read_bytes(),
                                            (plugin_dir / "config.yaml").read_bytes())


def handshake(main_dir: Path, indicators: tuple[tuple[str, Path], ...] = (), **over) -> dict:
    hs = {"cpu_sec": 60, "memory_mb": 512, "nofile": 128, "fsize_mb": 8,
          "attest_nonce": NONCE, "content_hash": chash(main_dir),
          "indicators": [{"alias": a, "plugin_py": str(d / "plugin.py"), "content_hash": chash(d)}
                         for a, d in indicators]}
    hs.update(over)
    return hs


def cfg_for(plugin_dir: Path, hs: dict | None = None, **extra) -> dict:
    cfg = {"main_dir": str(plugin_dir),
           "handshake": hs if hs is not None else handshake(plugin_dir)}
    cfg.update(extra)
    return cfg


@dataclass
class ChildResult:
    rc: int
    lines: list[dict]
    stderr: str
    raw: bytes
    pid: int = 0
    interact: dict = field(default_factory=dict)

    def phase(self, name: str) -> dict | None:
        return next((ln for ln in self.lines if ln.get("phase") == name), None)

    @property
    def out(self) -> dict:
        done = self.phase("done")
        assert done is not None, f"child did not finish: rc={self.rc} lines={self.lines}\n{self.stderr}"
        return done["out"]


def _parse(raw: bytes) -> list[dict]:
    lines = []
    for chunk in raw.splitlines():
        if chunk.strip():
            lines.append(json.loads(chunk))
    return lines


def run_child(cfg: dict, *, write_bytecode: bool = False, env: dict | None = None,
              timeout: float = 120.0,
              interact: Callable[[int, dict], dict | None] | None = None) -> ChildResult:
    """子を起こして結果を集める。`interact` は子が `isolated` 行を書いた後 (子は
    `wait_parent()` で待つ) に親側で呼ばれ、戻ると子へ 1 行送る。"""
    argv = [sys.executable, "-P"] + ([] if write_bytecode else ["-B"]) + [str(CHILD), json.dumps(cfg)]
    full_env = dict(os.environ)
    full_env["OPENBLAS_NUM_THREADS"] = "1"
    full_env.pop("PYTHONPYCACHEPREFIX", None)
    full_env.update(env or {})
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=full_env)
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    try:
        head = b""
        info: dict = {}
        if interact is not None:
            line = proc.stdout.readline()
            head = line
            first = json.loads(line) if line.strip() else {}
            if first.get("phase") == "isolated":
                info = interact(proc.pid, first) or {}
            try:
                proc.stdin.write(b"go\n")
                proc.stdin.flush()
            except BrokenPipeError:
                pass
        stdout, stderr = proc.communicate()
    finally:
        timer.cancel()
    raw = head + stdout
    return ChildResult(proc.returncode, _parse(raw), stderr.decode("utf-8", "replace"), raw,
                       pid=proc.pid, interact=info)


def failure_line(reason: str, pid: int) -> dict:
    return {"phase": "sandbox_ready", "ok": False, "stage": "sandbox", "reason": reason,
            "pid": pid}


def proc_task_status(pid: int) -> list[tuple[str, str, str]]:
    """`/proc/<pid>/task/*/status` の (NoNewPrivs, Seccomp, Seccomp_filters)。"""
    out = []
    for tid in sorted(os.listdir(f"/proc/{pid}/task")):
        with open(f"/proc/{pid}/task/{tid}/status", encoding="ascii") as f:
            fields = dict(line.split(":", 1) for line in f if ":" in line)
        out.append(tuple(fields.get(k, "").strip()
                         for k in ("NoNewPrivs", "Seccomp", "Seccomp_filters")))
    return out
