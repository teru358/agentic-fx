"""AC-42: 隔離された worker の内側から鍵 dir の全 file が読めない。

実 process で、実際の鍵 dir (``keys.key_dir(root)`` に ``keys.initialize`` で作った
鍵・``.ready``・revoke の tombstone・rotate 途中で落ちて残った一時 file) を作ってから、
plugin worker / 改善 worker (local・claude) / gate pytest worker の内側で開く。
対照として、利用者 shell (素の子 process) と Landlock のない trade worker からは読める
ことも確かめる (信頼境界の記述どおり)。
"""
from __future__ import annotations

import errno
import json
import os
import resource
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.landlock import is_available
from agentic_fx.ops import keys
from agentic_fx.ops.contracts import Principal
from agentic_fx.runners.worker_runner import _mission_worker_env
from tests.plugin._worker_isolation_support import (
    cfg_for, copy_plugin, needs_sandbox, run_child)

REPO = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).with_name("_keyprobe.py")
EACCES = errno.EACCES

pytestmark = pytest.mark.skipif(not is_available(), reason="Landlock not available")


def _no_core() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _crash_rotate_after_temp_written(directory: Path) -> None:
    # 一時 file を書いた直後に process ごと落とす (unwind で一時 file が消えない)。
    code = ("import os, sys\n"
            "from pathlib import Path\n"
            "from agentic_fx.ops import keys\n"
            "from agentic_fx.ops.contracts import Principal\n"
            "def hook(step):\n"
            "    if step == 'key:written':\n"
            "        os._exit(17)\n"
            "keys.rotate(Path(sys.argv[1]), Principal.OPERATOR, hook=hook)\n")
    result = subprocess.run([sys.executable, "-c", code, str(directory)], timeout=60,
                            capture_output=True, text=True, preexec_fn=_no_core)
    assert result.returncode == 17, result.stderr


@pytest.fixture
def key_dir(tmp_path) -> Path:
    """実 lifecycle で作った鍵 dir (HOME は conftest が tmp に向けている)。"""
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    directory = keys.key_dir(root)
    assert str(directory).startswith(os.environ["HOME"])
    keys.initialize(directory, root=root)
    keys.revoke(directory, Principal.APPROVER)
    _crash_rotate_after_temp_written(directory)
    names = sorted(p.name for p in directory.iterdir())
    temps = [n for n in names if n.startswith(".operator.token.") and n.endswith(".tmp")]
    assert {"operator.token", "approver.token", keys.READY_NAME} <= set(names)
    assert len(temps) == 1, names
    assert (directory / "approver.token").read_bytes() != b""  # tombstone
    return directory


def _targets(directory: Path) -> tuple[list[str], list[str]]:
    files = sorted(str(p) for p in directory.iterdir())
    dirs = [str(directory), str(directory.parent), str(directory.parent.parent)]
    return files, dirs


def _all_eacces(result: dict, files: list[str], dirs: list[str]) -> None:
    assert result["files"] == {p: EACCES for p in files}
    assert result["dirs"] == {p: EACCES for p in dirs}


def _all_readable(result: dict, files: list[str], dirs: list[str]) -> None:
    assert result["files"] == {p: "ok" for p in files}
    assert result["dirs"] == {p: "ok" for p in dirs}


def _run_probe(cfg: dict, *, env: dict, cwd: Path) -> dict:
    result = subprocess.run([sys.executable, str(PROBE), json.dumps(cfg)], env=env,
                            cwd=str(cwd), capture_output=True, text=True, timeout=60,
                            stdin=subprocess.DEVNULL, preexec_fn=_no_core)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------ 隔離される 3 worker

@needs_sandbox
def test_ac42_plugin_worker_cannot_read_any_file_of_the_key_dir(tmp_path, key_dir):
    files, dirs = _targets(key_dir)
    main = copy_plugin("rsi", tmp_path / "plugins")
    post = ("def probe(path, listing=False):\n"
            "    try:\n"
            "        if listing:\n"
            "            os.listdir(path)\n"
            "        else:\n"
            "            os.close(os.open(path, os.O_RDONLY | os.O_CLOEXEC))\n"
            "        return 'ok'\n"
            "    except OSError as e:\n"
            "        return e.errno\n"
            "out['files'] = {p: probe(p) for p in cfg['probe_files']}\n"
            "out['dirs'] = {p: probe(p, True) for p in cfg['probe_dirs']}\n")
    res = run_child(cfg_for(main, post=post, probe_files=files, probe_dirs=dirs,
                            home=os.environ["HOME"]))
    assert res.rc == 0, res.stderr
    _all_eacces(res.out, files, dirs)


@pytest.mark.parametrize("backend", ["local", "claude"])
def test_ac42_improve_worker_cannot_read_any_file_of_the_key_dir(tmp_path, key_dir, backend):
    files, dirs = _targets(key_dir)
    # 改善 worker の cwd と staging は鍵 dir と無関係な兄弟に置く (cwd は rw に入る)。
    workdir = tmp_path / "improve-workdir"
    source = workdir / "source"
    source.mkdir(parents=True)
    staging = tmp_path / "plugins" / "_staging" / "m-001"
    staging.mkdir(parents=True, mode=0o700)
    os.chmod(staging, 0o700)
    env = _mission_worker_env("improve")
    env["AGENTIC_FX_MISSION_TRANSCRIPTS_DIR"] = str(tmp_path / "transcripts")
    cfg = {"profile": "improve", "backend": backend, "mission_id": "m-001",
           "staging_dir": str(staging), "source_snapshot_dir": str(source),
           "files": files, "dirs": dirs}
    _all_eacces(_run_probe(cfg, env=env, cwd=workdir), files, dirs)


def test_ac42_gate_pytest_worker_cannot_read_any_file_of_the_key_dir(tmp_path, key_dir):
    from agentic_fx.plugin.gate_pytest import run_gate_pytest
    files, dirs = _targets(key_dir)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "plugin.py").write_text("def compute(df, params):\n    return {}\n")
    (candidate / "config.yaml").write_text("kind: indicator\n")
    (candidate / "test_plugin.py").write_text(
        "import errno, os\n"
        f"FILES = {files!r}\n"
        f"DIRS = {dirs!r}\n"
        "def _errno(fn, path):\n"
        "    try:\n"
        "        fn(path)\n"
        "    except OSError as e:\n"
        "        return e.errno\n"
        "    return 'ok'\n"
        "def test_key_dir_is_eacces():\n"
        "    seen = {p: _errno(lambda q: os.close(os.open(q, os.O_RDONLY)), p) for p in FILES}\n"
        "    seen.update({p: _errno(os.listdir, p) for p in DIRS})\n"
        "    assert seen == {p: errno.EACCES for p in FILES + DIRS}, seen\n")
    settings = load_settings(REPO / "config" / "settings.yaml.example")
    result = run_gate_pytest(candidate, settings=settings)
    assert result.passed is True, result.stdout_tail
    assert "1 passed" in result.stdout_tail


# ------------------------------------------------------------ 信頼境界の内側 (対照)

def test_ac42_user_shell_can_read_the_key_dir(tmp_path, key_dir):
    files, dirs = _targets(key_dir)
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
           "PYTHONPATH": str(REPO / "src")}
    _all_readable(_run_probe({"profile": "plain", "files": files, "dirs": dirs},
                             env=env, cwd=tmp_path), files, dirs)


def test_ac42_trade_worker_without_landlock_can_read_the_key_dir(tmp_path, key_dir):
    files, dirs = _targets(key_dir)
    settings = load_settings(REPO / "config" / "settings.yaml.example")
    limits = {"as_mb": settings.worker.child_as_mb, "nofile": settings.worker.child_nofile,
              "fsize_mb": settings.worker.child_fsize_mb}
    workdir = tmp_path / "trade-workdir"
    workdir.mkdir()
    _all_readable(_run_probe({"profile": "trade", "limits": limits, "files": files,
                              "dirs": dirs},
                             env=_mission_worker_env("trade"), cwd=workdir), files, dirs)
