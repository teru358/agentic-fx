"""Mission worker child process tests (プラン10 Task 5)."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


# --- Task 5 Section 5-C: _exec_closure_for -----

def test_exec_closure_local_has_no_shell_or_cli_dirs(tmp_path):
    from agentic_fx.mission_worker import _exec_closure_for
    venv = tmp_path / "venv"; venv.mkdir()
    closure = _exec_closure_for("local", claude_bin=None, codex_bin=None,
                                venv_root=venv)
    assert Path("/usr/bin") not in closure
    assert venv in closure


def test_exec_closure_claude_includes_usr_bin_and_bin_parent(tmp_path):
    from agentic_fx.mission_worker import _exec_closure_for
    venv = tmp_path / "venv"; venv.mkdir()
    fake_claude = tmp_path / "versions" / "2.1.233" / "claude"
    fake_claude.parent.mkdir(parents=True)
    fake_claude.write_text("")
    closure = _exec_closure_for("claude", claude_bin=fake_claude,
                               codex_bin=None, venv_root=venv)
    assert Path("/usr/bin") in closure
    assert fake_claude.parent in closure


def test_exec_closure_codex_includes_usr_bin_and_bin_parent(tmp_path):
    from agentic_fx.mission_worker import _exec_closure_for
    venv = tmp_path / "venv"; venv.mkdir()
    fake_codex = tmp_path / "vendor" / "codex"
    fake_codex.parent.mkdir(parents=True)
    fake_codex.write_text("")
    closure = _exec_closure_for("codex", claude_bin=None, codex_bin=fake_codex,
                               venv_root=venv)
    assert Path("/usr/bin") in closure
    assert fake_codex.parent in closure


def test_exec_closure_local_excludes_claude_and_codex_bin_dirs(tmp_path):
    """local backend に claude_bin/codex_bin を渡しても無視される
    (LocalRunner は subprocess を起こさない — §2.2)。"""
    from agentic_fx.mission_worker import _exec_closure_for
    venv = tmp_path / "venv"; venv.mkdir()
    fake_claude = tmp_path / "cbin" / "claude"
    fake_claude.parent.mkdir(parents=True); fake_claude.write_text("")
    closure = _exec_closure_for("local", claude_bin=fake_claude,
                               codex_bin=None, venv_root=venv)
    assert fake_claude.parent not in closure


def test_exec_closure_includes_usr_lib_family_for_all_backends():
    from agentic_fx.mission_worker import _exec_closure_for
    from pathlib import Path
    for backend in ("local", "claude", "codex"):
        closure = _exec_closure_for(backend, claude_bin=None, codex_bin=None,
                                    venv_root=Path("/nonexistent-venv"))
        assert Path("/usr/lib") in closure


# --- Task 5 Section 5-D: _bootstrap_improve_profile expansion -----

def _run_bootstrap_probe(script: str, *, staging_dir: Path, mission_id: str,
                         source_snapshot_dir: Path, workdir: Path,
                         backend: str = "local",
                         extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """`_bootstrap_improve_profile` を子プロセスで呼び、続けて `script` を
    実行する。子プロセス 1 個 = 検査 1 件 (Landlock 不可逆のため)。
    **`cwd` は明示的に `workdir` を渡す** — `staging_dir` の祖先 (`repo/
    plugins/`) を cwd にすると、`_bootstrap_improve_profile` が
    `Path.cwd()` を rw allowlist に加える際にその祖先ごと書込可能になり、
    `plugins/` 全体が書ける事故を自己生産してしまう (5-E で顕在化する
    穴と同じ — ここでは fixture 側で作らない)。"""
    full_script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(_REPO_ROOT / "src")!r})
        from agentic_fx.mission_worker import _bootstrap_improve_profile
        _bootstrap_improve_profile(
            backend={backend!r}, mission_id={mission_id!r},
            staging_dir={str(staging_dir)!r},
            source_snapshot_dir={str(source_snapshot_dir)!r},
            claude_bin=None, codex_bin=None)
    """) + "\n" + textwrap.dedent(script)
    env = {"PATH": "/usr/bin:/bin"}
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-c", full_script],
                          cwd=str(workdir), env=env,
                          capture_output=True, text=True, timeout=30)


@pytest.fixture
def improve_worker_layout(tmp_path):
    """**staging_dir の末尾成分は mission_id と一致していなければならない**
    (§2.2 の相互照合 — `_bootstrap_improve_profile` はこれを検証してから
    起動する)。`repo/plugins/_staging/<mission_id>/` の形をそのまま模す。
    `workdir` は `repo`/`plugins` のどちらとも独立させる (cwd がどちらかの
    祖先を兼ねると、その祖先ごと rw allowlist に混入し `plugins/` 全体が
    書けてしまう事故になる — 5-E で顕在化する形と同じ穴をここでも避ける)。"""
    workdir = tmp_path / "workdir"; workdir.mkdir()
    mission_id = "m-001"
    repo_plugins = tmp_path / "repo" / "plugins"
    repo_plugins.mkdir(parents=True)
    staging_dir = repo_plugins / "_staging" / mission_id
    staging_dir.mkdir(parents=True, mode=0o700)
    source_snapshot_dir = workdir / "source"
    source_snapshot_dir.mkdir(mode=0o500)
    return {"workdir": workdir, "mission_id": mission_id,
           "staging_dir": staging_dir, "source_snapshot_dir": source_snapshot_dir}


def test_bootstrap_improve_profile_grants_execute_on_venv(improve_worker_layout):
    """execute_paths に venv_root が入り、python 自体を **exec できる**
    (着手前検証 Blocking 7 修正: 旧稿は「起動できていること自体が exec
    権の証拠」としていたが、python は Landlock 適用の**前**に exec 済み
    であり、bootstrap 後に何も exec しない `print('BOOTSTRAP_OK')` は
    `execute_paths=[]` にしても green のままだった (恒真)。ここでは
    bootstrap **後**に `os.execv(sys.executable, ...)` で venv 内の
    python 自身を明示的に再 exec し、それが通ることを確認する
    (5-G の `_run_version_under_closure` と同型の self-exec probe)。"""
    l = improve_worker_layout
    script = """
    import os, sys
    os.execv(sys.executable, [sys.executable, "-c", "print('VENV_EXEC_OK')"])
    print('SHOULD_NOT_REACH')
    """
    result = _run_bootstrap_probe(
        script, staging_dir=l["staging_dir"], mission_id=l["mission_id"],
        source_snapshot_dir=l["source_snapshot_dir"], workdir=l["workdir"])
    assert result.returncode == 0, result.stderr
    assert "VENV_EXEC_OK" in result.stdout
    assert "SHOULD_NOT_REACH" not in result.stdout


def test_bootstrap_improve_profile_dev_is_read_write(improve_worker_layout):
    """`/dev` が read_write に上がっている — `subprocess.DEVNULL` 相当の
    `/dev/null` 書込オープンが通る (probe §5-③)。"""
    l = improve_worker_layout
    script = """
    import os
    fd = os.open("/dev/null", os.O_WRONLY)
    os.close(fd)
    print("DEVNULL_WRITABLE")
    """
    result = _run_bootstrap_probe(
        script, staging_dir=l["staging_dir"], mission_id=l["mission_id"],
        source_snapshot_dir=l["source_snapshot_dir"], workdir=l["workdir"])
    assert result.returncode == 0, result.stderr
    assert "DEVNULL_WRITABLE" in result.stdout


def test_bootstrap_improve_profile_staging_dir_is_writable(improve_worker_layout):
    l = improve_worker_layout
    script = f"""
    from pathlib import Path
    p = Path({str(l['staging_dir'])!r}) / "x.txt"
    p.write_text("ok")
    print("STAGING_WRITABLE")
    """
    result = _run_bootstrap_probe(
        script, staging_dir=l["staging_dir"], mission_id=l["mission_id"],
        source_snapshot_dir=l["source_snapshot_dir"], workdir=l["workdir"])
    assert result.returncode == 0, result.stderr
    assert "STAGING_WRITABLE" in result.stdout


def test_bootstrap_improve_profile_rejects_staging_dir_mission_id_mismatch(
        improve_worker_layout):
    """§2.2: `staging_dir` の末尾成分が handshake の `mission_id` と
    一致しないと起動拒否 (相互照合)。"""
    l = improve_worker_layout
    result = _run_bootstrap_probe(
        "print('SHOULD_NOT_REACH')",
        staging_dir=l["staging_dir"], mission_id="different-mission-id",
        source_snapshot_dir=l["source_snapshot_dir"], workdir=l["workdir"])
    assert result.returncode != 0
    assert "SHOULD_NOT_REACH" not in result.stdout


def test_bootstrap_improve_profile_local_backend_has_no_shell_execute(
        improve_worker_layout):
    """local backend の exec closure に `/usr/bin` が無い —
    `/usr/bin/env` を exec しようとすると `PermissionError`。"""
    l = improve_worker_layout
    script = """
    import os
    try:
        os.execv("/usr/bin/env", ["/usr/bin/env"])
    except PermissionError:
        print("SHELL_BLOCKED")
        raise SystemExit(0)
    print("SHOULD_NOT_REACH")
    raise SystemExit(1)
    """
    result = _run_bootstrap_probe(
        script, staging_dir=l["staging_dir"], mission_id=l["mission_id"],
        source_snapshot_dir=l["source_snapshot_dir"], workdir=l["workdir"],
        backend="local")
    assert result.returncode == 0, result.stderr
    assert "SHELL_BLOCKED" in result.stdout


def test_bootstrap_improve_profile_claude_backend_has_shell_execute(
        improve_worker_layout):
    """claude backend の exec closure には `/usr/bin` が入り、
    `/usr/bin/env` を exec できる (§2.1-5「shell を許す」の pin)。"""
    l = improve_worker_layout
    script = """
    import os
    os.execv("/usr/bin/env", ["/usr/bin/env", "true"])
    """
    result = _run_bootstrap_probe(
        script, staging_dir=l["staging_dir"], mission_id=l["mission_id"],
        source_snapshot_dir=l["source_snapshot_dir"], workdir=l["workdir"],
        backend="claude")
    assert result.returncode == 0, result.stderr


def test_bootstrap_improve_profile_proc_readable_only_for_claude(
        improve_worker_layout):
    """`/proc` は claude backend のときだけ read_only。local では
    listdir が拒否される。claude 側は同じ mission_id・別 staging_dir
    (`_staging2/<mission_id>/`) を使う — mission_id が両呼出しで一致
    していること自体が相互照合の pin を兼ねる。"""
    l = improve_worker_layout
    script = """
    try:
        import os
        os.listdir("/proc")
        print("PROC_READABLE")
    except PermissionError:
        print("PROC_BLOCKED")
    """
    result_local = _run_bootstrap_probe(
        script, staging_dir=l["staging_dir"], mission_id=l["mission_id"],
        source_snapshot_dir=l["source_snapshot_dir"], workdir=l["workdir"],
        backend="local")
    assert "PROC_BLOCKED" in result_local.stdout

    layout2_staging = (l["staging_dir"].parent.parent.parent / "_staging2"
                       / l["mission_id"])
    layout2_staging.mkdir(parents=True, mode=0o700)
    result_claude = _run_bootstrap_probe(
        script, staging_dir=layout2_staging, mission_id=l["mission_id"],
        source_snapshot_dir=l["source_snapshot_dir"], workdir=l["workdir"],
        backend="claude")
    assert "PROC_READABLE" in result_claude.stdout, result_claude.stderr


def test_bootstrap_improve_profile_home_env_is_scratch_dir(improve_worker_layout):
    """不変条件 4 (§2.1): `HOME` が実ホームでなく workdir/home に固定
    される (env は呼び出し側で設定するため、ここでは
    `_bootstrap_improve_profile` が **env 自体を書き換えない** ことと、
    handshake 側 (worker_runner) が正しい env を渡す契約を pin する —
    本 step の対象は Landlock 配線のみ。env 構築は A-1/A-2 の責務。
    ここでは `_bootstrap_improve_profile` が `HOME` を検査・変更しない
    (env 非依存で Landlock だけを張る) ことだけを pin する。"""
    l = improve_worker_layout
    script = "import os; print('HOME=' + os.environ.get('HOME', '<unset>'))"
    result = _run_bootstrap_probe(
        script, staging_dir=l["staging_dir"], mission_id=l["mission_id"],
        source_snapshot_dir=l["source_snapshot_dir"], workdir=l["workdir"],
        extra_env={"HOME": "/tmp/fake-scratch-home"})
    assert "HOME=/tmp/fake-scratch-home" in result.stdout


def test_bootstrap_improve_profile_rejects_when_staging_dir_mode_is_not_0700(
        improve_worker_layout):
    """staging dirfd 再検証 (uid/mode) — 親が 0700 で作った前提が崩れた
    (例えば 0777 のまま渡された) staging_dir は拒否される。"""
    l = improve_worker_layout
    loose_staging = l["staging_dir"].parent.parent / "loose" / l["mission_id"]
    loose_staging.mkdir(parents=True, mode=0o777)
    result = _run_bootstrap_probe(
        "print('SHOULD_NOT_REACH')", staging_dir=loose_staging,
        mission_id=l["mission_id"], source_snapshot_dir=l["source_snapshot_dir"],
        workdir=l["workdir"])
    assert result.returncode != 0
    assert "SHOULD_NOT_REACH" not in result.stdout


def test_bootstrap_improve_profile_rejects_source_snapshot_dir_outside_workdir(
        improve_worker_layout):
    """(Blocking 10) source_snapshot_dir が workdir の外を指すと拒否される。"""
    l = improve_worker_layout
    outside = l["workdir"].parent / "not-workdir"
    outside.mkdir(mode=0o500)
    result = _run_bootstrap_probe(
        "print('SHOULD_NOT_REACH')", staging_dir=l["staging_dir"],
        mission_id=l["mission_id"], source_snapshot_dir=outside,
        workdir=l["workdir"])
    assert result.returncode != 0
    assert "SHOULD_NOT_REACH" not in result.stdout
