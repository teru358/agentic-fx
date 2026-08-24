"""共通 launcher (`agentic_fx.runners.launcher`) — 設計書 §1.1-1、§8.1-2/3。

`python -c "<launcher source>"` として exec される launcher 本体は、
①`prctl(PR_SET_PDEATHSIG, SIGKILL)` ②`os.getppid()` を expected_parent_pid
と再照合 (不一致なら `os._exit(1)`) ③rlimits があれば適用 ④`os.execv`。
multi-threaded なプロセス (service/mission_worker) では `preexec_fn` を
使わない (CLAUDE.md Global Constraints / 設計書 §1.1-1) — launcher は
`sys.executable -c` を **fork しない単一スレッドの子プロセス**として起動
することでこれを実現する。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentic_fx.runners.launcher import build_launcher_argv


def test_build_launcher_argv_shape():
    """Step 1 (§8.1-2 の前提): argv の形が骨格 Interfaces の docstring どおり。"""
    argv = build_launcher_argv(4242, ["/usr/bin/true"])
    assert argv[0] == sys.executable
    assert argv[1] == "-c"
    assert isinstance(argv[2], str) and len(argv[2]) > 0  # launcher source
    assert argv[3] == "4242"
    assert argv[4] == ""  # rlimits 無し → 空文字列
    assert argv[5:] == ["/usr/bin/true"]


def test_build_launcher_argv_encodes_rlimits_as_json():
    argv = build_launcher_argv(
        1, ["/usr/bin/true"],
        rlimits={"RLIMIT_FSIZE": (8 * 1024 * 1024, 8 * 1024 * 1024)})
    decoded = json.loads(argv[4])
    assert decoded == {"RLIMIT_FSIZE": [8388608, 8388608]}


def test_build_launcher_argv_rejects_empty_argv():
    """#2 (`verified-round1.md` 1-A): `if not argv: raise ValueError` が
    無いと `argv[0]` の直後 IndexError になり、契約 (`ValueError`) が
    変わる。"""
    with pytest.raises(ValueError, match="empty"):
        build_launcher_argv(1, [])


def test_build_launcher_argv_rejects_relative_argv():
    """launcher へ渡す argv は起動時検査が解決した絶対パスのみ
    (§1.1-1「argv は解決済み絶対パスのみ」)。相対パスは呼び出し側の誤りであり
    構築時点で拒否する。"""
    with pytest.raises(ValueError, match="absolute"):
        build_launcher_argv(1, ["relative/bin"])


# ---- 実プロセステスト (§8.1-2): SIGTERM 無視 CLI / kill / 親死 / timeout ----

_TOUCH_AND_SLEEP = (
    "import pathlib, sys, time\n"
    "pathlib.Path(sys.argv[1]).write_text('started')\n"
    "time.sleep(60)\n"
)

_IGNORE_SIGTERM_AND_TOUCH = (
    "import pathlib, signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "pathlib.Path(sys.argv[1]).write_text('started')\n"
    "time.sleep(60)\n"
)


def _wait_for_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(f"{path} did not appear within {timeout}s")


def test_launcher_execs_target_and_target_starts(tmp_path):
    """<!-- precheck 2026-08-22: T1-M9 --> Minor 9 の再発防止 (旧名
    `..._and_target_receives_no_extra_fds` は fd を一切検査していなかった
    — 実態に合わせて改名): launcher が argv[0] を execv する — 素朴な
    `[python, target]` と同じ実行結果になることを実プロセスで確認する。
    fd 継承の検証は別 (`test_cli_runner_does_not_use_subprocess_devnull_for_stdin`
    が `CliRunner` 層で個別に見る)。"""
    marker = tmp_path / "started"
    argv = build_launcher_argv(
        os.getpid(),
        [sys.executable, "-c", _TOUCH_AND_SLEEP, str(marker)])
    proc = subprocess.Popen(argv, start_new_session=True)
    try:
        _wait_for_file(marker)
    finally:
        proc.kill()
        proc.wait(timeout=5)


_TOUCH_AND_EXIT = (
    "import pathlib, sys\n"
    "pathlib.Path(sys.argv[1]).write_text('started')\n"
    "sys.exit(42)\n"
)


def test_launcher_execv_preserves_target_exit_code(tmp_path):
    """#7 (`verified-round1.md` 1-A): marker ファイルの存在のみを見る既存
    テストは launcher プロセスが exit code を伝播するかを見ていない
    (`os.execv` を `subprocess.run` 相当の子プロセス起動に緩める変異でも、
    marker さえ書かれれば通ってしまう)。`os.execv` は現プロセスをターゲット
    で置き換えるため、launcher プロセス自身の終了コードがターゲットの
    `sys.exit(42)` と一致するはずであることを実プロセスで pin する。"""
    marker = tmp_path / "started-exit"
    argv = build_launcher_argv(
        os.getpid(),
        [sys.executable, "-c", _TOUCH_AND_EXIT, str(marker)])
    proc = subprocess.Popen(argv, start_new_session=True)
    proc.wait(timeout=5)
    assert marker.exists()
    assert proc.returncode == 42


def test_launcher_child_dies_when_parent_dies(tmp_path):
    """§8.1-2「worker 親死」: PDEATHSIG により、expected_parent_pid の
    プロセスが死ぬと launcher (と execv した CLI) も自動終了する。

    間接の「親」プロセスを別 subprocess として立て、それを kill して
    観測する (pytest プロセス自身を殺すわけにはいかない)。
    """
    marker = tmp_path / "started"
    died_marker = tmp_path / "child_pid"
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        "import json, os, subprocess, sys, time\n"
        "from agentic_fx.runners.launcher import build_launcher_argv\n"
        f"argv = build_launcher_argv(os.getpid(), [sys.executable, '-c', {_TOUCH_AND_SLEEP!r}, {str(marker)!r}])\n"
        "p = subprocess.Popen(argv, start_new_session=True)\n"
        f"open({str(died_marker)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen([sys.executable, str(parent_script)])
    try:
        _wait_for_file(marker)
        _wait_for_file(died_marker)
        child_pid = int(died_marker.read_text())
        parent.kill()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5.0
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)
        assert not alive, "launcher (and its execv'd child) survived parent death"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)


def test_launcher_child_ignoring_sigterm_still_dies_to_sigkill(tmp_path):
    """§8.1-2「SIGTERM 無視 CLI」: 直接の呼び出し元 (CliRunner、Task 2/3 で
    実装) が SIGKILL へエスカレーションできることの前提 — launcher 経由で
    起動した子が SIGTERM を無視しても、SIGKILL で確実に終了する。"""
    marker = tmp_path / "started"
    argv = build_launcher_argv(
        os.getpid(),
        [sys.executable, "-c", _IGNORE_SIGTERM_AND_TOUCH, str(marker)])
    proc = subprocess.Popen(argv, start_new_session=True)
    try:
        _wait_for_file(marker)
        os.killpg(proc.pid, signal.SIGTERM)
        # #8 (`verified-round1.md` 1-A): 単発の `time.sleep(0.3)` → 1 回
        # だけの `poll()` は負荷時に振れる (非決定)。窓の間ポーリングで
        # 継続的に確認する — 早期に死んだ場合も window 内で検出できる。
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            assert proc.poll() is None, "SIGTERM を無視するはずが死んだ"
            time.sleep(0.02)
        os.killpg(proc.pid, signal.SIGKILL)
        rc = proc.wait(timeout=5)
        # Minor 10 の再発防止: `proc.wait()` は必ず int を返すため
        # `is not None` は恒真。SIGKILL によるシグナル死 (負値) であることを
        # 見る。
        assert rc < 0, f"SIGKILL 後の returncode が負値でない: {rc}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_launcher_exits_immediately_when_expected_parent_pid_mismatches(tmp_path):
    """§1.1-1: `os.getppid()` を expected_parent_pid と再照合し不一致なら
    即終了する (設定前に親が死んで再親付けされた race)。既に死んでいる
    PID (再利用されない値) を expected_parent_pid に渡し、launcher が
    execv せず即終了することを marker file の不在で確認する。"""
    marker = tmp_path / "started"
    dead_pid = _reap_a_dead_pid()
    argv = build_launcher_argv(
        dead_pid, [sys.executable, "-c", _TOUCH_AND_SLEEP, str(marker)])
    proc = subprocess.Popen(argv, start_new_session=True)
    rc = proc.wait(timeout=5)
    assert rc != 0
    assert not marker.exists(), "expected_parent_pid 不一致でも execv してしまった"


def _reap_a_dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait(timeout=5)
    return p.pid


def test_launcher_module_does_not_use_preexec_fn():
    """構造 pin (§8.1-2): launcher モジュール自身、および `CliRunner`
    (Task 1 が土台を produce・Task 2/3 が継承) の `Popen` 呼び出しに
    `preexec_fn=` が出現しない (multi-thread プロセスでの `preexec_fn`
    使用を CLAUDE.md/設計書 §1.1-1 が禁じる)。"""
    import agentic_fx.runners.launcher as launcher_mod
    import agentic_fx.runners.cli_runner as cli_runner_mod
    import ast
    import inspect
    for mod in (launcher_mod, cli_runner_mod):
        src = inspect.getsource(mod)
        tree = ast.parse(src)
        # キーワード引数 preexec_fn= の存在を検査する
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "preexec_fn":
                raise AssertionError(f"{mod.__name__} uses preexec_fn keyword argument")
