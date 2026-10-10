"""launcher 経路の境界を、実子 process と実 kernel で直接固定する。

harness と kernel 能力の確認は `test_cli_launch` のものを使う。
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentic_fx.core import cli_launch as L
from agentic_fx.core import cli_launch_protocol as P
from tests.core import test_cli_launch as base
from tests.core.test_cli_launch import (
    COPIED_PY_SLEEP_ARGS,
    PY,
    _fds,
    _kernel_unready,
    _wrapped_launcher,
    copied_python_env,
    copy_python,
)

# kernel 能力の確認 (module 共通、autouse) を同じ判定で掛ける
_kernel_ready = base._kernel_ready


@pytest.fixture
def h():
    harness = base._Harness()
    yield harness
    harness.close()


pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine != "x86_64",
    reason="improve-cli の launcher は x86_64 Linux 専用")


# --- 実行 artifact の ELF 検査 -------------------------------------------------------

def _elf_header(*, ei_class: int = 2, ei_data: int = 1, e_type: int = 2,
                e_machine: int = 62) -> bytes:
    ident = b"\x7fELF" + bytes([ei_class, ei_data, 1]) + bytes(9)
    return ident + struct.pack("<HHI", e_type, e_machine, 1) + bytes(40)


_UNSUPPORTED = {
    # npm が置く codex の入口 (Node の #! script)
    "npm_wrapper": b"#!/usr/bin/env node\nimport('../dist/cli.js');\n",
    "shebang_sh": b"#!/bin/sh\nexec sleep 30\n",
    "pe": b"MZ" + bytes(126) + b"PE\0\0" + bytes(64),
    "empty": b"",
    "short_elf": b"\x7fELF\x02\x01",
    "elf32": _elf_header(ei_class=1),
    "elf_big_endian": _elf_header(ei_data=2),
    "elf_aarch64": _elf_header(e_machine=183),
    "elf_relocatable": _elf_header(e_type=1),
    "elf_core": _elf_header(e_type=4),
}


@pytest.mark.parametrize("name", sorted(_UNSUPPORTED))
def test_unsupported_artifact_is_rejected_before_launcher(h, tmp_path, name):
    """ELF 64 bit x86_64 の EXEC / DYN 以外は launcher を起こさずに固定 reason。"""
    artifact = tmp_path / "codex"
    artifact.write_bytes(_UNSUPPORTED[name])
    artifact.chmod(0o700)
    marker = tmp_path / "launcher-ran"
    before = _fds()
    r = h.launch(cli=str(artifact), argv=[str(artifact), "--version"],
                 launcher_cmd=[sys.executable, "-c", f"open({str(marker)!r}, 'w')"])
    assert (r.started, r.reason, r.series, r.classification) == (
        False, P.CLI_ARTIFACT_UNSUPPORTED, L.BEFORE_HANDOFF, L.SANDBOX_UNAVAILABLE), r
    assert r.launcher_proc is None and r.reaped
    assert h.joined == [] and h.listeners == []
    assert not marker.exists()
    # 渡した artifact fd も閉じている
    assert _fds() == before


def test_directory_artifact_is_rejected(h, tmp_path):
    marker = tmp_path / "launcher-ran"
    r = h.launch(cli=str(tmp_path), argv=[str(tmp_path)],
                 launcher_cmd=[sys.executable, "-c", f"open({str(marker)!r}, 'w')"])
    assert (r.reason, r.series) == (P.CLI_ARTIFACT_UNSUPPORTED, L.BEFORE_HANDOFF), r
    assert r.detail == "not a regular file"
    assert r.launcher_proc is None and not marker.exists()


def test_cli_artifact_unsupported_is_admission_reason():
    assert P.CLI_ARTIFACT_UNSUPPORTED == "cli_artifact_unsupported"
    assert P.CLI_ARTIFACT_UNSUPPORTED not in P.SETUP_REASONS.values()
    assert P.CLI_ARTIFACT_UNSUPPORTED not in P.ATTESTATION_REASONS


def test_artifact_is_inspected_on_the_same_fd(h, tmp_path):
    """検査は開いた fd で行い、path を開き直さない。open 後に path を #! script へ
    差し替えても、fd の指す ELF が exec される。"""
    cli = tmp_path / "cli"
    cli.write_bytes(Path(PY).read_bytes())
    cli.chmod(0o700)
    fd = L.open_cli_binary(str(cli))
    os.unlink(cli)
    cli.write_bytes(b"#!/bin/sh\nexit 0\n")
    cli.chmod(0o700)
    r = L.launch_cli(
        cli_fd=fd, argv=[str(cli), "-S", "-c", "import time; time.sleep(30)"],
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "PYTHONHOME": sys.base_prefix},
        backend="claude", join=h.join, handoff=h.handoff,
        deadline=time.monotonic() + 10)
    if r.launcher_proc is not None:
        h.procs.append(r.launcher_proc)
    assert r.started, r
    time.sleep(0.3)
    assert r.launcher_proc.poll() is None


# --- codex の native ELF -------------------------------------------------------------

_CODEX_NATIVE_REL = ("node_modules/@openai/codex-linux-x64/vendor/"
                     "x86_64-unknown-linux-musl/bin/codex")


def _codex_native_elf() -> str | None:
    """npm で入れた codex の wrapper (`bin/codex.js`) から、同じ package が持つ
    native ELF の path を辿る。settings の `runner.codex.bin` に書く値に当たる。"""
    wrapper = shutil.which("codex")
    if wrapper is None:
        return None
    pkg = Path(os.path.realpath(wrapper)).parent.parent
    native = pkg / _CODEX_NATIVE_REL
    return str(native) if native.is_file() else None


def test_codex_native_elf_reaches_cli_started(h):
    """codex の native ELF を artifact にすると launcher 経由で exec され、
    `--version` が完走する。managed 環境変数 (`CODEX_MANAGED_*`) は渡さない。"""
    codex = _codex_native_elf()
    if codex is None:
        pytest.skip("npm の codex (native ELF 同梱) が見つからない")
    env = {"PATH": "/usr/bin:/bin", "LANG": "C"}
    assert not [k for k in env if k.startswith("CODEX_MANAGED_")]
    r = h.launch(cli=codex, argv=[codex, "--version"], backend="codex", env=env,
                 stdout=subprocess.PIPE)
    assert (r.started, r.reason) == (True, None), r
    out, _ = r.launcher_proc.communicate(timeout=30)
    assert r.launcher_proc.returncode == 0
    assert out.decode().startswith("codex"), out


def test_codex_npm_wrapper_is_unsupported(h):
    """npm の wrapper (Node の #! script) そのものを artifact にすると落ちる。"""
    wrapper = shutil.which("codex")
    if wrapper is None:
        pytest.skip("npm の codex が見つからない")
    r = h.launch(cli=wrapper, argv=[wrapper, "--version"], backend="codex")
    assert (r.reason, r.series) == (P.CLI_ARTIFACT_UNSUPPORTED, L.BEFORE_HANDOFF), r
    assert r.launcher_proc is None


# --- probe 失敗の扱い ---------------------------------------------------------------

@pytest.mark.parametrize("env, outcome", [
    (None, pytest.skip.Exception), ("0", pytest.skip.Exception),
    ("1", pytest.fail.Exception)])
def test_probe_failure_fails_when_probes_are_required(monkeypatch, env, outcome):
    """`AFX_REQUIRE_SANDBOX_PROBES=1` の下では kernel 能力の不足が skip でなく fail。"""
    if env is None:
        monkeypatch.delenv("AFX_REQUIRE_SANDBOX_PROBES", raising=False)
    else:
        monkeypatch.setenv("AFX_REQUIRE_SANDBOX_PROBES", env)
    with pytest.raises(outcome):
        _kernel_unready("probe failed")


# --- fd 経由の exec と path 経由の exec の区別 ---------------------------------------

_PATH_EXEC_PATCH = """
def path_exec(cli_fd, argv, envp):
    env = dict(e.split(b"=", 1) for e in envp)
    try:
        os.execve(argv[0], argv, env)
    except OSError as e:
        return e.errno
m._execveat = path_exec
"""


def _replaced_after_open(tmp_path: Path) -> tuple[int, Path, int, int]:
    """CLI を開いた後で、同じ path を別 inode の file に置き換える。"""
    cli, other = tmp_path / "python", tmp_path / "python.new"
    copy_python(cli)
    copy_python(other)
    fd = L.open_cli_binary(str(cli))
    opened_ino = os.fstat(fd).st_ino
    new_ino = os.stat(other).st_ino
    os.rename(other, cli)
    return fd, cli, opened_ino, new_ino


def _launch_fd(h, fd: int, cli: Path, **kw) -> L.LaunchResult:
    r = L.launch_cli(cli_fd=fd, argv=[str(cli), *COPIED_PY_SLEEP_ARGS],
                     env=copied_python_env(), backend="claude", join=h.join,
                     handoff=h.handoff, deadline=time.monotonic() + 10,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    if r.launcher_proc is not None:
        h.procs.append(r.launcher_proc)
    return r


def test_fd_exec_runs_the_opened_inode_after_path_replacement(h, tmp_path):
    """本物の launcher は開いた fd を exec するので、path を置き換えても開いた inode が
    動き `cli_started`。"""
    fd, _cli, opened_ino, _new_ino = _replaced_after_open(tmp_path)
    r = _launch_fd(h, fd, _cli)
    assert r.started, r
    time.sleep(0.2)
    assert r.launcher_proc.poll() is None
    assert os.stat(f"/proc/{r.launcher_proc.pid}/exe").st_ino == opened_ino


def test_path_exec_is_detected_as_unproven(h, tmp_path):
    """launcher が fd でなく path を exec すると、置き換え後の inode が動くので
    worker は `cli_exec_unproven` にする。"""
    fd, cli, opened_ino, new_ino = _replaced_after_open(tmp_path)
    r = _launch_fd(h, fd, cli, launcher_cmd=_wrapped_launcher(_PATH_EXEC_PATCH))
    assert (r.reason, r.series, r.classification) == (
        P.CLI_EXEC_UNPROVEN, L.AFTER_HANDOFF, L.FAILED), r
    # 置き換え後の file が生きて動いている (exec 失敗や即死による unproven ではない)
    assert r.launcher_proc.poll() is None
    assert os.stat(f"/proc/{r.launcher_proc.pid}/exe").st_ino == new_ino != opened_ino
    assert L.reap_launcher(r.launcher_proc, deadline=time.monotonic() + 5)
