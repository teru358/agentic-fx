"""Mission worker child process tests (プラン10 Task 5)."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from agentic_fx.tools.mission_counters import MissionToolCounters

_REPO_ROOT = Path(__file__).resolve().parents[1]


# --- A4 10 回目 #71 観測 B (2026-09-11): _improve_result_tool_calls -----

def test_improve_result_tool_calls_none_for_local_backend():
    """local backend では終端 frame に tool_calls を載せない (既存の
    local 終端 activity の文面を変えない)。"""
    from agentic_fx.mission_worker import _improve_result_tool_calls
    from agentic_fx.tools.mission_counters import MissionToolCounters

    counters = MissionToolCounters(budget=100)
    for _ in range(11):
        counters.total_calls += 1
    assert _improve_result_tool_calls(counters, "local") is None


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
def test_improve_result_tool_calls_reflects_counters_for_cli_backends(backend):
    """非 local backend では counters.total_calls をそのまま返す — 0 も
    欠落させない。"""
    from agentic_fx.mission_worker import _improve_result_tool_calls
    from agentic_fx.tools.mission_counters import MissionToolCounters

    zero_counters = MissionToolCounters(budget=100)
    assert _improve_result_tool_calls(zero_counters, backend) == 0

    eleven_counters = MissionToolCounters(budget=100)
    for _ in range(11):
        eleven_counters.total_calls += 1
    assert _improve_result_tool_calls(eleven_counters, backend) == 11


# --- Task 5 Section 5-C: _exec_closure_for -----

def test_exec_closure_local_has_no_shell_or_cli_dirs(tmp_path):
    from agentic_fx.mission_worker import _exec_closure_for
    venv = tmp_path / "venv"; venv.mkdir()
    closure = _exec_closure_for("local", claude_bin=None, codex_bin=None,
                                venv_root=venv)
    assert Path("/usr/bin") not in closure.dirs
    assert venv in closure.dirs


def test_exec_closure_claude_includes_usr_bin_and_bin_parent(tmp_path):
    from agentic_fx.mission_worker import _exec_closure_for
    venv = tmp_path / "venv"; venv.mkdir()
    fake_claude = tmp_path / "versions" / "2.1.233" / "claude"
    fake_claude.parent.mkdir(parents=True)
    fake_claude.write_text("")
    closure = _exec_closure_for("claude", claude_bin=fake_claude,
                               codex_bin=None, venv_root=venv)
    assert Path("/usr/bin") in closure.dirs
    assert fake_claude.parent in closure.dirs


def test_exec_closure_codex_includes_usr_bin_and_bin_parent(tmp_path):
    from agentic_fx.mission_worker import _exec_closure_for
    venv = tmp_path / "venv"; venv.mkdir()
    fake_codex = tmp_path / "vendor" / "codex"
    fake_codex.parent.mkdir(parents=True)
    fake_codex.write_text("")
    closure = _exec_closure_for("codex", claude_bin=None, codex_bin=fake_codex,
                               venv_root=venv)
    assert Path("/usr/bin") in closure.dirs
    assert fake_codex.parent in closure.dirs


def test_exec_closure_opencode_includes_cli_binary(tmp_path):
    """opencode 実行ファイルを closure から落とすと Landlock 後の exec が
    PermissionError になるため、target と親ディレクトリを pin する。"""
    from agentic_fx.mission_worker import _exec_closure_for
    venv = tmp_path / "venv"; venv.mkdir()
    binary = tmp_path / "vendor" / "opencode"; binary.parent.mkdir(); binary.write_text("")
    closure = _exec_closure_for("opencode", claude_bin=None, codex_bin=None,
                                opencode_bin=binary, venv_root=venv)
    assert binary.resolve() in closure.targets
    assert binary.resolve().parent in closure.dirs


def test_exec_closure_local_excludes_claude_and_codex_bin_dirs(tmp_path):
    """local backend に claude_bin/codex_bin を渡しても無視される
    (LocalRunner は subprocess を起こさない — §2.2)。"""
    from agentic_fx.mission_worker import _exec_closure_for
    venv = tmp_path / "venv"; venv.mkdir()
    fake_claude = tmp_path / "cbin" / "claude"
    fake_claude.parent.mkdir(parents=True); fake_claude.write_text("")
    closure = _exec_closure_for("local", claude_bin=fake_claude,
                               codex_bin=None, venv_root=venv)
    assert fake_claude.parent not in closure.dirs


def test_exec_closure_local_does_not_grant_usr_lib_as_an_exec_dir():
    """5-C 改訂 (2026-08-22, 裁定 A): 旧
    `test_exec_closure_includes_usr_lib_family_for_all_backends` の置き換え。
    `/usr/lib` をディレクトリ単位で exec 許可すると、実体が `/usr/lib` 配下
    にある実行ファイル (uutils coreutils 等) が芋づるで exec 可能になり
    local backend の shell 遮断が壊れる (probe-execute-closure.md §4)。
    **local に限った主張である** — claude/codex は §2.1-5 により `/usr/bin`
    + shell を意図的に許可しており、`/usr/lib` を落とすと `git submodule`
    (`/usr/lib/git-core/`) 等が壊れる (裁定 4、claude/codex は現行どおり
    `/usr/lib` を含む)。"""
    from agentic_fx.mission_worker import _exec_closure_for
    closure = _exec_closure_for("local", claude_bin=None, codex_bin=None,
                                venv_root=Path("/nonexistent-venv"))
    assert Path("/usr/lib") not in closure.dirs
    assert Path("/usr/lib64") not in closure.dirs


def test_exec_closure_targets_include_the_running_python_for_all_backends():
    """PT_INTERP 解決の入力 (`targets`) に必ず実行中の python が入る —
    ここが空だとローダのファイルルールが 1 本も張られず、bootstrap 後の
    自己 exec が `PermissionError` になる (probe 3-i-control)。"""
    from agentic_fx.mission_worker import _exec_closure_for
    for backend in ("local", "claude", "codex"):
        closure = _exec_closure_for(backend, claude_bin=None, codex_bin=None,
                                    venv_root=Path("/nonexistent-venv"))
        assert Path(sys.executable).resolve() in closure.targets


# --- Task 5 Section 5-D: _bootstrap_improve_profile expansion -----

def _run_bootstrap_probe(script: str, *, staging_dir: Path, mission_id: str,
                         source_snapshot_dir: Path, workdir: Path,
                         backend: str = "local",
                         extra_env: dict | None = None,
                         isolate_transcripts_dir: bool = True,
                         ) -> subprocess.CompletedProcess:
    """`_bootstrap_improve_profile` を子プロセスで呼び、続けて `script` を
    実行する。子プロセス 1 個 = 検査 1 件 (Landlock 不可逆のため)。
    **`cwd` は明示的に `workdir` を渡す** — `staging_dir` の祖先 (`repo/
    plugins/`) を cwd にすると、`_bootstrap_improve_profile` が
    `Path.cwd()` を rw allowlist に加える際にその祖先ごと書込可能になり、
    `plugins/` 全体が書ける事故を自己生産してしまう (5-E で顕在化する
    穴と同じ — ここでは fixture 側で作らない)。

    T1(a) 是正 (test-hygiene 設計書 2026-09-12): `isolate_transcripts_dir`
    (既定 True) のとき `AGENTIC_FX_MISSION_TRANSCRIPTS_DIR` を
    `workdir` 配下 (tmp_path 由来) の隔離先に向けて子プロセスの env に
    渡す — `_bootstrap_improve_profile` は別プロセスなので親プロセスの
    `cli_runner._TRANSCRIPT_DIR_DEFAULT` monkeypatch (`tests/conftest.py`)
    が届かず、環境変数だけがこの別プロセスを隔離できる唯一の経路。
    `test_bootstrap_improve_profile_mission_transcript_dir_is_writable`
    は実リポジトリの `logs/mission-transcripts/` との座標一致を検証する
    のが主旨のため `isolate_transcripts_dir=False` を渡し、隔離しない。"""
    # ローカル backend-fix 1 周目 #L1 (2026-09-11): `PYTHONPATH` が与えられて
    # いるときは子の sys.path 先頭に実 src を差し込まない — 段 0 の変異
    # overlay (`PYTHONPATH=overlay/src`) をこの probe にも効かせるため
    # (従来は実 src を必ず先頭に挿すので overlay が無視され、Landlock
    # allowlist の変異が測れなかった)。通常実行では `PYTHONPATH` は
    # 与えられないので挙動は変わらない。
    full_script = textwrap.dedent(f"""
        import os, sys
        if not os.environ.get("PYTHONPATH"):
            sys.path.insert(0, {str(_REPO_ROOT / "src")!r})
        from agentic_fx.mission_worker import _bootstrap_improve_profile
        _bootstrap_improve_profile(
            backend={backend!r}, mission_id={mission_id!r},
            staging_dir={str(staging_dir)!r},
            source_snapshot_dir={str(source_snapshot_dir)!r},
            claude_bin=None, codex_bin=None)
    """) + "\n" + textwrap.dedent(script)
    env = {"PATH": "/usr/bin:/bin"}
    # 同上 (#L1): 親に `PYTHONPATH` があれば子へ渡す (段 0 overlay)。子は
    # `cwd=workdir` (tmp_path 配下) で走るため、相対パスのままでは解決
    # できない — **親の cwd で絶対化してから**渡す。
    parent_pythonpath = os.environ.get("PYTHONPATH")
    if parent_pythonpath:
        env["PYTHONPATH"] = os.pathsep.join(
            str(Path(entry).resolve()) for entry in
            parent_pythonpath.split(os.pathsep) if entry)
    if isolate_transcripts_dir:
        isolated_transcripts_dir = workdir / "mission-transcripts-isolated"
        env["AGENTIC_FX_MISSION_TRANSCRIPTS_DIR"] = str(isolated_transcripts_dir)
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


def test_bootstrap_improve_profile_mission_transcript_dir_is_writable(
        improve_worker_layout):
    """段A 是正 (2026-08-30): `CliRunner` の transcript 既定保存先
    (`cli_runner._TRANSCRIPT_DIR_DEFAULT` == `<repo>/logs/mission-
    transcripts`) が improve profile の Landlock rw allowlist に無いと、
    improve/opencode mission の transcript 保存が `PermissionError` →
    `logging.warning` (`stderr=subprocess.DEVNULL` 越しに握り潰される) で
    silent に無効化される。ここでは `_bootstrap_improve_profile` 後に
    実際にその dir へ書けることを子プロセスで実測する。

    書込先は実リポジトリの `logs/mission-transcripts/` (`CliRunner` 本体
    と Landlock allowlist が同じ座標に一致していることを検証するのが
    このテストの主旨のため、テスト専用ディレクトリへ差し替えない) —
    一意なマーカーファイル名で書き、assert 後に必ず削除して実リポジトリを
    汚さない (`tests-touching-real-repo-resources` の再演防止)。

    **`real_dir` は `_REPO_ROOT` から独立に導出する** — 子プロセスの
    `_TRANSCRIPT_DIR_DEFAULT` を親プロセス側で直接 import して比較する
    と、`tests/conftest.py::_isolate_mission_transcripts_default_dir`
    (セッション全体で親プロセスのその属性を隔離先へ monkeypatch する
    fixture) の影響を受けてしまい、子 (別プロセス、monkeypatch 不到達)
    が実際に書いた実リポジトリの座標と食い違う。

    T1(b) 是正 (test-hygiene 設計書 2026-09-12, fresh worktree では
    `logs/mission-transcripts/` がそもそも存在しない): `_bootstrap_
    improve_profile` はこのディレクトリが無ければ `mkdir(parents=True)`
    で新規作成する。fresh worktree でこのテストを実行すると、マーカー
    ファイルを消すだけではディレクトリ自体が新規に残ってしまい、
    `tests/conftest.py::_guard_real_mission_transcripts_dir_is_never_
    touched` (session 前後のスナップショット比較) が ERROR になる。
    このテストが**このディレクトリを新規作成した側**である場合は、
    マーカー削除後にディレクトリごと rmdir して session 開始時点の
    状態 (無い) へ戻す。"""
    l = improve_worker_layout
    marker_name = f"landlock-probe-{uuid.uuid4().hex}.txt"
    script = f"""
    from agentic_fx.runners.cli_runner import _TRANSCRIPT_DIR_DEFAULT
    p = _TRANSCRIPT_DIR_DEFAULT / {marker_name!r}
    p.write_text("ok")
    print("TRANSCRIPT_DIR_WRITABLE")
    """
    real_dir = _REPO_ROOT / "logs" / "mission-transcripts"
    marker_path = real_dir / marker_name
    # フルスイート是正 (test-hygiene 2026-09-12): 従来は `real_dir`
    # (`logs/mission-transcripts`) 自身の pre-existence だけを記録して
    # rmdir していたが、`_bootstrap_improve_profile` の `mkdir(parents=
    # True)` は祖先 `logs/` もまとめて作る。fresh worktree (`logs/` 自体
    # が無い状態) でこのテストを実行すると、`mission-transcripts` は
    # rmdir されても親 `logs/` が空のまま残り続けた (二分探索で特定された
    # 実測の残骸)。session 開始前に**存在しなかった祖先**を repo root を
    # 越えない範囲で列挙し (深い方から順、`missing_ancestors[0]` が
    # `real_dir` 自身)、teardown で子から順に rmdir する — 空でなければ
    # `OSError` を握って諦める (他の何かがまだ使っている可能性があるので
    # 無理に消さない)。
    missing_ancestors: list[Path] = []
    _p = real_dir
    while _p != _REPO_ROOT and not _p.exists():
        missing_ancestors.append(_p)
        _p = _p.parent
    try:
        result = _run_bootstrap_probe(
            script, staging_dir=l["staging_dir"], mission_id=l["mission_id"],
            source_snapshot_dir=l["source_snapshot_dir"], workdir=l["workdir"],
            isolate_transcripts_dir=False)
        assert result.returncode == 0, result.stderr
        assert "TRANSCRIPT_DIR_WRITABLE" in result.stdout
        assert marker_path.is_file()
        assert marker_path.read_text() == "ok"
    finally:
        marker_path.unlink(missing_ok=True)
        for _ancestor in missing_ancestors:
            try:
                _ancestor.rmdir()
            except OSError:
                pass


def test_bootstrap_improve_profile_does_not_mkdir_transcript_dir_under_guarded_data_dir(
        improve_worker_layout):
    """検収是正 C4 (codex 1 周目 Important, test-hygiene 2026-09-12):
    `AGENTIC_FX_MISSION_TRANSCRIPTS_DIR` が `_guarded_data_dir()`
    (`<repo>/data`) 配下を指す場合、是正前は `_assert_allowlist_
    excludes_data_dir` (fail closed の assert) が最終的に拒否する**前**
    に `transcript_dir.mkdir(parents=True)` が実行され、`data/` 配下に
    ディレクトリが実際に作られてしまっていた (assert 自体は空振りしない
    が、mkdir という副作用は防げていなかった)。ここでは
    `<repo>/data/<マーカー>/mission-transcripts` を狙い、bootstrap 後に
    そのディレクトリが**作られていない**ことを確認する (mkdir 前の
    早期 skip が効いていることの直接証拠)。実リポジトリの `data/` 直下に
    残骸を作らないよう、常に teardown で掃除する。"""
    l = improve_worker_layout
    marker = f"c4-probe-{uuid.uuid4().hex}"
    malicious_dir = _REPO_ROOT / "data" / marker / "mission-transcripts"
    data_marker_dir = _REPO_ROOT / "data" / marker
    script = "print('BOOTSTRAP_OK')"
    try:
        result = _run_bootstrap_probe(
            script, staging_dir=l["staging_dir"], mission_id=l["mission_id"],
            source_snapshot_dir=l["source_snapshot_dir"], workdir=l["workdir"],
            extra_env={"AGENTIC_FX_MISSION_TRANSCRIPTS_DIR": str(malicious_dir)})
        assert result.returncode == 0, result.stderr
        assert "BOOTSTRAP_OK" in result.stdout
        assert not malicious_dir.exists(), (
            f"{malicious_dir} が data/ 配下にもかかわらず mkdir された "
            "(C4 是正が効いていない)")
    finally:
        if data_marker_dir.exists():
            import shutil as _shutil
            _shutil.rmtree(data_marker_dir, ignore_errors=True)


def test_bootstrap_improve_profile_data_dir_skip_warning_does_not_read_as_startup_refusal(
        improve_worker_layout):
    """codex 2 周目 Minor M2 (test-hygiene 2026-09-12): C4 是正の warning
    文言に含まれていた「(fail closed)」は improve worker の起動そのものを
    拒否しているように読める。実際にはこの分岐は transcript 保存だけを
    諦めて bootstrap 自体は継続するため、「起動は継続する」ことを明示する
    文言 (`continuing without transcript persistence`) に変更し、
    誤解を招く「fail closed」の語をこの警告からは外した。`logging` の
    既定 (ハンドラ未設定時の `lastResort`) は stderr へ出るため、子
    プロセスの `result.stderr` で文言を確認する。"""
    l = improve_worker_layout
    marker = f"c4-wording-probe-{uuid.uuid4().hex}"
    malicious_dir = _REPO_ROOT / "data" / marker / "mission-transcripts"
    data_marker_dir = _REPO_ROOT / "data" / marker
    script = "print('BOOTSTRAP_OK')"
    try:
        result = _run_bootstrap_probe(
            script, staging_dir=l["staging_dir"], mission_id=l["mission_id"],
            source_snapshot_dir=l["source_snapshot_dir"], workdir=l["workdir"],
            extra_env={"AGENTIC_FX_MISSION_TRANSCRIPTS_DIR": str(malicious_dir)})
        assert result.returncode == 0, result.stderr
        assert "continuing without transcript persistence" in result.stderr, (
            f"是正後の文言が stderr に出ていない: {result.stderr!r}")
        assert "fail closed" not in result.stderr, (
            "起動拒否と誤読されうる旧文言 'fail closed' がまだ warning に "
            f"残っている: {result.stderr!r}")
    finally:
        if data_marker_dir.exists():
            import shutil as _shutil
            _shutil.rmtree(data_marker_dir, ignore_errors=True)


def test_bootstrap_improve_profile_skips_rw_allowlist_when_transcript_mkdir_raises_oserror(
        improve_worker_layout):
    """ローカル 1 周目 pin (P4, test-hygiene 2026-09-12): C4 是正後の形で
    「mkdir が `OSError` (例: 対象パスに既に通常ファイルがある) のとき
    `extra_rw_paths` に追加されない」ことを、Landlock の実際の
    read_write_paths (`landlock.restrict_to` の呼び出し引数) を捕まえて
    直接確認する。`_run_bootstrap_probe` はそのプリアンブルで
    `_bootstrap_improve_profile` を呼んでしまう (monkeypatch を挟む余地が
    無い) ため、ここでは
    `test_bootstrap_improve_profile_fails_closed_when_exec_closure_dirs_
    reach_data` と同じ形で subprocess スクリプトを自前で組み立てる。"""
    l = improve_worker_layout
    blocked_path = l["workdir"] / "blocked-transcripts-file"
    blocked_path.write_text("this is a regular file, not a directory")
    full_script = textwrap.dedent(f"""
        import json, sys
        sys.path.insert(0, {str(_REPO_ROOT / "src")!r})
        from agentic_fx import mission_worker
        from agentic_fx.core import landlock

        captured = {{}}

        def fake_restrict_to(**kw):
            captured.update(kw)

        landlock.restrict_to = fake_restrict_to
        mission_worker._bootstrap_improve_profile(
            backend={"local"!r}, mission_id={l["mission_id"]!r},
            staging_dir={str(l["staging_dir"])!r},
            source_snapshot_dir={str(l["source_snapshot_dir"])!r},
            claude_bin=None, codex_bin=None)
        rw = [str(p) for p in captured.get("read_write_paths", [])]
        print(json.dumps(rw))
    """)
    env = {"PATH": "/usr/bin:/bin",
          "AGENTIC_FX_MISSION_TRANSCRIPTS_DIR": str(blocked_path)}
    result = subprocess.run([sys.executable, "-c", full_script],
                            cwd=str(l["workdir"]), env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    read_write_paths = json.loads(result.stdout.strip().splitlines()[-1])
    assert str(blocked_path) not in read_write_paths, (
        f"mkdir が OSError (対象が通常ファイル) で失敗したのに "
        f"{blocked_path} が read_write_paths (extra_rw_paths) に "
        f"追加されている: {read_write_paths}")
    # mkdir 失敗の副作用でファイル自体が壊れていないことも確認する。
    assert blocked_path.is_file()
    assert not blocked_path.is_dir()


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


def test_bootstrap_improve_profile_rejects_staging_dir_forward_matching_mission_id(
        improve_worker_layout, tmp_path):
    """N1 (`stage0-bundle-B.md` Important): 既存の mismatch pin
    (`..._mismatch`) は「完全不一致」の 1 ケースのみで、`staging_path.name
    != mission_id` を `not staging_path.name.startswith(mission_id)` に
    緩める変異 (前方一致を許す) を殺せない。`mission_id="m-001"` に対し
    `staging_dir` の末尾成分を `"m-0011"` (前方一致するが不一致) にした
    ケースを 1 本足す — fail closed (非 0 終了) することを assert する。"""
    l = improve_worker_layout
    repo_plugins = tmp_path / "repo2" / "plugins"
    repo_plugins.mkdir(parents=True)
    forward_matching_staging = repo_plugins / "_staging" / "m-0011"
    forward_matching_staging.mkdir(parents=True, mode=0o700)
    result = _run_bootstrap_probe(
        "print('SHOULD_NOT_REACH')",
        staging_dir=forward_matching_staging, mission_id=l["mission_id"],
        source_snapshot_dir=l["source_snapshot_dir"], workdir=l["workdir"])
    assert result.returncode != 0
    assert "SHOULD_NOT_REACH" not in result.stdout


def test_bootstrap_improve_profile_fails_closed_when_exec_closure_dirs_reach_data(
        improve_worker_layout):
    """A1 (`stage0-bundle-B.md` Important): `_assert_allowlist_excludes_
    data_dir(read_only + [workdir, staging_path] + execute_dirs +
    execute_files, ...)` から `+ execute_dirs + execute_files` を落として
    も既存テストは全緑 — 「どの allowlist 群を検査に掛けるか」自体が
    無検証だった。`_exec_closure_for` を monkeypatch して `dirs` に
    `_guarded_data_dir().parent` を返させ、`_bootstrap_improve_profile`
    が fail closed (非 0 終了) することを子プロセスで確認する。"""
    l = improve_worker_layout
    full_script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(_REPO_ROOT / "src")!r})
        from agentic_fx import mission_worker
        from agentic_fx.mission_worker import (
            ExecClosure, _guarded_data_dir)

        def _fake_exec_closure_for(backend, *, claude_bin, codex_bin, venv_root):
            return ExecClosure(dirs=[_guarded_data_dir().parent], targets=[])

        mission_worker._exec_closure_for = _fake_exec_closure_for
        mission_worker._bootstrap_improve_profile(
            backend={"local"!r}, mission_id={l["mission_id"]!r},
            staging_dir={str(l["staging_dir"])!r},
            source_snapshot_dir={str(l["source_snapshot_dir"])!r},
            claude_bin=None, codex_bin=None)
        print('SHOULD_NOT_REACH')
    """)
    # T1(a)/(b) 是正 (test-hygiene 設計書 2026-09-12): このテストは
    # `_run_bootstrap_probe` を経由せず直接 `subprocess.run` を組み立てる
    # ため、隔離用の `AGENTIC_FX_MISSION_TRANSCRIPTS_DIR` 環境変数が付か
    # ないまま `_bootstrap_improve_profile` の mkdir (fail closed の
    # `_assert_allowlist_excludes_data_dir` より前で実行される) が実
    # `logs/mission-transcripts/` を毎回作っていた (repo root 直下の
    # untracked 残骸 — `_guard_repo_root_has_no_new_untracked_files` が
    # 検出する対象そのもの)。他の probe と同じ隔離先を明示する。
    env = {"PATH": "/usr/bin:/bin",
           "AGENTIC_FX_MISSION_TRANSCRIPTS_DIR":
               str(l["workdir"] / "mission-transcripts-isolated")}
    result = subprocess.run([sys.executable, "-c", full_script],
                            cwd=str(l["workdir"]), env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "SHOULD_NOT_REACH" not in result.stdout


def test_bootstrap_improve_profile_local_backend_has_no_shell_execute(
        improve_worker_layout):
    """local backend の exec closure に `/usr/bin` が無い — **PATH 経由・
    直接 exec のどちらでも** `/usr/bin/env` は `PermissionError`。

    5-C 改訂 (2026-08-22, probe-execute-closure.md §5.2): **明示的な動的
    ローダ起動 (`ld.so <path>`) による残余経路は本テストの対象外**
    (§5.1 のとおり FS allowlist は execve を跨いで継承されるため
    `data/` 到達不能は別途保たれる — この残余は FS 境界の脱出ではない)。
    このテストは「PATH 経由・直接 exec のどちらでも shell/CLI を起動
    できない」という水準の主張であり、「local backend は shell execute を
    一切持たない」という、より強い主張の証拠として引用してはならない。"""
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


def test_bootstrap_improve_profile_proc_blocked_for_codex(
        improve_worker_layout):
    """反証 (2026-09-12): A4 10 回目 #71 が SIGTRAP の引き金だとした
    「codex の code-mode host (V8) が /proc/self/maps を読む」という説は
    単体切り分けの実測で否定された (`tmp/codex-host-probe/findings.md`)
    — 引き金は RLIMIT_AS 4096MB (Landlock + env + RLIMIT_AS のみでも同じ
    SIGTRAP が再現し、/proc なしの対照でも再現する)。ユーザー裁定
    (2026-09-12) により、codex には /proc を付与しない — local と同じく
    listdir が拒否されることを pin する (`838b09d` で足した readable pin
    を反転)。**codex に /proc が不要と証明されたわけではない**
    (findings.md 未証明の点 2 — /proc を外した対照は未実施)。"""
    l = improve_worker_layout
    script = """
    try:
        import os
        os.listdir("/proc")
        print("PROC_READABLE")
    except PermissionError:
        print("PROC_BLOCKED")
    """
    layout3_staging = (l["staging_dir"].parent.parent.parent / "_staging3"
                       / l["mission_id"])
    layout3_staging.mkdir(parents=True, mode=0o700)
    result_codex = _run_bootstrap_probe(
        script, staging_dir=layout3_staging, mission_id=l["mission_id"],
        source_snapshot_dir=l["source_snapshot_dir"], workdir=l["workdir"],
        backend="codex")
    assert "PROC_BLOCKED" in result_codex.stdout, result_codex.stderr


def test_bootstrap_improve_profile_proc_readable_for_opencode(
        improve_worker_layout):
    """ローカル backend-fix 1 周目 #L1 (2026-09-11): opencode backend でも
    `/proc` が read_only に入る。

    `backend in ("claude", "opencode", "codex")` のタプルから `opencode`
    だけを落とす変異は、claude 側と codex 側の probe しか無かった材料
    時点では生存した (opencode = bun/JSC は `/proc/self/maps` を読めないと
    SIGABRT するので、落ちれば improve mission が起動ごと死ぬ)。"""
    l = improve_worker_layout
    script = """
    try:
        import os
        os.listdir("/proc")
        print("PROC_READABLE")
    except PermissionError:
        print("PROC_BLOCKED")
    """
    layout4_staging = (l["staging_dir"].parent.parent.parent / "_staging4"
                       / l["mission_id"])
    layout4_staging.mkdir(parents=True, mode=0o700)
    result_opencode = _run_bootstrap_probe(
        script, staging_dir=layout4_staging, mission_id=l["mission_id"],
        source_snapshot_dir=l["source_snapshot_dir"], workdir=l["workdir"],
        backend="opencode")
    assert "PROC_READABLE" in result_opencode.stdout, result_opencode.stderr


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


# --- Task 5 Section 5-G: exec closure 1 要素 drop 表 -----

import shutil


def _skip_unless_cli_installed(bin_name: str) -> str:
    path = shutil.which(bin_name)
    if path is None:
        pytest.skip(f"{bin_name} not installed on this host — drop table "
                    "row cannot be measured here (see probe report §7)")
    return path


def _resolve_codex_native_bin() -> str:
    """(裁定 R5) `codex` 用の CLI 解決。`shutil.which("codex")` の結果を
    ELF マジックバイトで判定し、Node シェバンラッパ (ELF でない) なら
    同じ npm パッケージ配下の `codex-linux-x64/vendor/*/bin/codex`
    (native, static-pie musl) を探索する。どちらも見つからなければ
    skip する (`_skip_unless_cli_installed` と同じ規律)。Task 1 が
    `runner.codex.bin` の config 経路を確定させたら、本関数は設定値の
    検証 (ELF でなければ fail closed) に差し替えること (申し送り④)。"""
    which_path = shutil.which("codex")
    if which_path is None:
        pytest.skip("codex not installed on this host — drop table row "
                    "cannot be measured here (see probe report §7)")
    resolved = Path(which_path).resolve()
    with open(resolved, "rb") as f:
        magic = f.read(4)
    if magic == b"\x7fELF":
        return str(resolved)
    search_root = resolved
    for _ in range(6):
        search_root = search_root.parent
        candidates = sorted(search_root.glob(
            "**/codex-linux-x64/vendor/*/bin/codex"))
        if candidates:
            return str(candidates[0])
    pytest.skip(f"codex vendor native binary not found by walking up from "
                f"{resolved} — drop table row cannot be measured here "
                "(裁定 R5, see probe report §2.1)")


def _run_version_under_closure(bin_path: str, *, execute_paths: list[Path],
                               read_only_paths: list[Path],
                               read_write_paths: list[Path] = (),
                               env: dict[str, str] | None = None
                               ) -> subprocess.CompletedProcess:
    """`<bin_path> --version` を、指定した exec closure だけを許可した
    Landlock 下の子プロセスで実行する。rlimit は probe 実測の本番相当形
    (`as_mb=4096, nofile=128, fsize_mb=8`) を使う。**`env` を明示しない
    と実 `$HOME` を継承する** — claude の `--version` が `$HOME`/
    `~/.claude` に触れる場合、それがどの allowlist にも入っていないため
    無関係な理由で red になる (advisor 指摘)。呼び出し側は claude を
    測るときは必ず scratch `HOME`/`CLAUDE_CONFIG_DIR` を `env=` で渡し、
    その scratch dir を `read_write_paths`(または `read_only_paths`)に
    含めること。"""
    script = textwrap.dedent(f"""
        import resource, sys
        sys.path.insert(0, {str(_REPO_ROOT / "src")!r})
        from pathlib import Path
        from agentic_fx.core import landlock
        resource.setrlimit(resource.RLIMIT_AS, (4096*1024*1024,)*2)
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        resource.setrlimit(resource.RLIMIT_FSIZE, (8*1024*1024,)*2)
        landlock.restrict_to(
            read_only_paths=[Path(p) for p in {[str(p) for p in read_only_paths]!r}],
            read_write_paths=[Path(p) for p in {[str(p) for p in read_write_paths]!r}],
            execute_paths=[Path(p) for p in {[str(p) for p in execute_paths]!r}])
        import os
        try:
            os.execv({bin_path!r}, [{bin_path!r}, "--version"])
        except PermissionError as e:
            print(f"EXEC_PERMISSION_ERROR errno={{e.errno}}")
            raise SystemExit(0)
    """)
    run_env = {"PATH": "/usr/bin:/bin"}
    run_env.update(env or {})
    return subprocess.run([sys.executable, "-c", script], env=run_env,
                          capture_output=True, text=True, timeout=15)


def test_drop_codex_bin_parent_denies_exec():
    codex_bin = _resolve_codex_native_bin()
    result = _run_version_under_closure(
        codex_bin, execute_paths=[], read_only_paths=[Path("/etc")])
    assert "EXEC_PERMISSION_ERROR errno=13" in result.stdout


def test_codex_bin_parent_alone_allows_exec():
    """positive control: 親ディレクトリ 1 つを execute_paths に足すだけで
    `--version` が通る (static-pie musl — ローダ不要、probe §2.1)。
    codex は `$CODEX_HOME` が無くても `--version` が通ることを probe
    §2.1 が前提にしている (認証を要さない経路)。**(着手前検証
    Blocking 9)** `codex_bin` は `_resolve_codex_native_bin()` で解決した
    native バイナリ (`which codex` の Node ラッパではない) — このホスト
    ではローダ不要のため `execute_paths` は親ディレクトリ 1 つで足りる。"""
    codex_bin = _resolve_codex_native_bin()
    parent = Path(codex_bin).resolve().parent
    result = _run_version_under_closure(
        codex_bin, execute_paths=[parent], read_only_paths=[Path("/etc")])
    assert "EXEC_PERMISSION_ERROR" not in result.stdout
    assert result.returncode == 0, result.stderr


def _claude_scratch_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """claude 系テスト共通: 実 `$HOME`/`~/.claude` に触れさせない scratch
    env を作る (probe §4 と同じ最小構成)。戻り値はそのまま
    `_run_version_under_closure(..., env=env, read_write_paths=[scratch])`
    に渡す。"""
    scratch = tmp_path / "claude_home"
    scratch.mkdir()
    (scratch / "cfg").mkdir()
    env = {"HOME": str(scratch), "CLAUDE_CONFIG_DIR": str(scratch / "cfg")}
    return env, scratch


def test_drop_usr_lib_denies_claude_exec(tmp_path):
    claude_bin = _skip_unless_cli_installed("claude")
    parent = Path(claude_bin).resolve().parent
    env, scratch = _claude_scratch_env(tmp_path)
    result = _run_version_under_closure(
        claude_bin, execute_paths=[parent], read_only_paths=[Path("/etc")],
        read_write_paths=[scratch], env=env)
    assert "EXEC_PERMISSION_ERROR errno=13" in result.stdout


def test_usr_lib64_alone_is_insufficient_for_claude_exec(tmp_path):
    """probe `logs/evi_claude_exec_lib64only.json` の回帰 pin — `/usr/lib64`
    だけでは不十分、`/usr/lib` の併記が必須。"""
    claude_bin = _skip_unless_cli_installed("claude")
    parent = Path(claude_bin).resolve().parent
    env, scratch = _claude_scratch_env(tmp_path)
    result = _run_version_under_closure(
        claude_bin, execute_paths=[parent, Path("/usr/lib64")],
        read_only_paths=[Path("/etc")], read_write_paths=[scratch], env=env)
    assert "EXEC_PERMISSION_ERROR errno=13" in result.stdout


def test_usr_lib_and_bin_parent_together_allow_claude_exec(tmp_path):
    """positive control: `/usr/lib` を併記すれば通る。**`env=` に scratch
    `HOME`/`CLAUDE_CONFIG_DIR` を明示する** — 実 `$HOME` を継承すると、
    `--version` が `~/.claude` に触れた場合 (未確認) allowlist 外への
    アクセスで無関係な理由で red になり得るため (advisor 指摘)。もし
    `--version` が `$HOME`/`CLAUDE_CONFIG_DIR` に一切触れないことが実装
    時の実測で確認できれば、`read_write_paths=[scratch]` は
    `read_only_paths` へ落としてよい — 実装者が実測して確定すること。

    **(実装時の実測で追加、逸脱として申告)** `/dev` が allowlist に無いと
    `claude --version` は `/dev/urandom` の open で `EACCES` になり、Bun
    ランタイムが `SIGABRT` で panic する (`strace` で実測確認 —
    `openat(AT_FDCWD, "/dev/urandom", O_RDONLY) = -1 EACCES`)。プラン記載の
    コードブロックには `Path("/dev")` が無かった — 5-D の
    `_bootstrap_improve_profile` が `/dev` を常に `read_write_paths` に
    含めているのと同じ扱いをここでも行う。"""
    claude_bin = _skip_unless_cli_installed("claude")
    parent = Path(claude_bin).resolve().parent
    env, scratch = _claude_scratch_env(tmp_path)
    result = _run_version_under_closure(
        claude_bin, execute_paths=[parent, Path("/usr/lib"), Path("/usr/lib64")],
        read_only_paths=[Path("/etc"), Path("/proc")],
        read_write_paths=[scratch, Path("/dev")], env=env)
    assert "EXEC_PERMISSION_ERROR" not in result.stdout
    assert result.returncode == 0, result.stderr


# --- Task 4 Step 7d: factory.build_runner via dispatcher -----

def _settings_with_improve_backend(backend):
    """プラン10 Task4, Step 7d の test helper: improve backend を
    指定した Settings を返す。"""
    from agentic_fx.config import load_settings
    settings = load_settings(
        Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example")
    return settings.model_copy(update={
        "runner": settings.runner.model_copy(update={
            "improve": settings.runner.improve.model_copy(
                update={"backend": backend}),
            "opencode": settings.runner.opencode.model_copy(
                update={"context_limit": 65536}),
        }),
    })


def test_mission_worker_builds_runner_via_factory_for_all_improve_backends(
        monkeypatch, tmp_path):
    """レビュー1周目 C4: improve backend が claude/codex でも
    `factory.build_runner` 経由で runner が構築されること (fake CLI、
    実 LLM 不要)。旧ガード (`backend != "local"` で RuntimeError) が
    残っていれば claude/codex パラメータで red になる。"""
    import agentic_fx.mission_worker as mw_mod

    captured = {}

    def spy(profile, settings, registry, *, workdir, on_message=None,
            cli_started_sink=None, abort_event=None, abort_reason_fn=None,
            after_tool_call=None):
        captured["profile"] = profile
        captured["backend"] = getattr(
            getattr(settings.runner, profile, None), "backend", None)
        captured["on_message"] = on_message
        # precheck 2026-08-22 pass2: RB3 — cli_started_sink= が
        # build_runner まで届いていることを pin する。
        captured["cli_started_sink"] = cli_started_sink
        # 段 0 pin A16 (2026-09-08): abort の reason は `tool_budget_abort:`
        # prefix で始まる (親の Tier D' / activity がこの prefix で判定する)。
        captured["abort_event"] = abort_event
        captured["abort_reason_fn"] = abort_reason_fn
        # 実 CLI/実 LLM を起動しない fake を返す — 構築経路の到達のみ確認する。
        class _Fake:
            def run(self, mission):
                from agentic_fx.runners.base import MissionResult
                return MissionResult("completed", {}, [])
            def close(self):
                pass
        return _Fake()

    monkeypatch.setattr(mw_mod.runner_factory, "build_runner", spy)

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    source_snapshot_dir = tmp_path / "source"
    source_snapshot_dir.mkdir()

    for backend in ["local", "claude", "codex", "opencode"]:
        captured.clear()
        settings = _settings_with_improve_backend(backend)
        mw_mod._run_improve_mission(
            settings=settings, workdir=tmp_path, staging_dir=str(staging_dir),
            source_snapshot_dir=str(source_snapshot_dir),
            protocol_out=None, out_seq=None, in_seq=None)
        assert captured["profile"] == "improve", f"backend={backend}"
        assert captured["backend"] == backend, f"backend={backend}"
        # 3 周目レビュー Important-1: on_message が callable として配線されている
        # ことを assert する — 落とすと transcript/event 転送が全 backend で失われる。
        assert callable(captured["on_message"]), f"backend={backend}"
        assert captured["abort_event"] is not None, f"backend={backend}"
        assert captured["abort_reason_fn"]().startswith("tool_budget_abort:"), \
            f"backend={backend}"
        # precheck 2026-08-22 pass2: RB3 — cli_started_sink も callable として
        # 配線されていることを assert する (§7.1-2 の受入条件、裁定 R1)。
        assert callable(captured["cli_started_sink"]), f"backend={backend}"


def test_build_improve_registry_requires_rpc_client(tmp_path):
    """A14 裁定 (2026-08-28、束D検収 verified-local-round1.md §7):
    `_build_improve_registry` は `rpc_client` を必須引数化した — 旧実装は
    `rpc_client=None` の既定値で空 `ToolRegistry()` を返す fail-open
    経路を持っていたが (`tools/mission_registry.py::build_mission_registry`
    は `rpc_handlers is None` で `ValueError` を送出する fail closed との
    非対称)、既定値を消して呼び出し元に明示させる。呼び出し側が
    `rpc_client` を渡し忘れると `TypeError` (missing required keyword
    argument) になることを pin する。"""
    import agentic_fx.mission_worker as mw_mod

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    source_snapshot_dir = tmp_path / "source"
    source_snapshot_dir.mkdir()
    settings = _settings_with_improve_backend("local")

    with pytest.raises(TypeError, match="rpc_client"):
        mw_mod._build_improve_registry(
            settings=settings, workdir=tmp_path, staging_dir=staging_dir,
            source_snapshot_dir=source_snapshot_dir)


# --- A-4 検収是正 (2026-08-22, B1): Step 7 Unix socket dispatcher -----

def test_run_improve_mission_binds_mcp_dispatcher_and_serves_registry_tool(
        monkeypatch, tmp_path):
    """B1 是正: mission_worker の improve 分岐が `workdir/afx.sock` を bind
    し、`mcp_shim` からの `tools/call` を注入された registry の実 tool へ
    配線すること (`ToolRegistry.execute` まで到達すること) を、
    `python -m agentic_fx.tools.mcp_shim <sock>` の**実プロセス**を fake
    mcp_shim クライアントとして起動し確認する (プランの Step 7 文言
    「fake mcp_shim クライアントが socket 経由で tools/call を投げ、
    registry の fake tool が実行される」に合わせる — advisor 指摘 #4)。
    `_build_improve_registry` を monkeypatch して非空 registry を注入
    できることも同時に pin する (「空でない registry を注入できる seam」
    — A-4 是正の要求)。"""
    import json
    import subprocess as subprocess_mod

    import agentic_fx.mission_worker as mw_mod
    from agentic_fx.tools.registry import ToolDef, ToolRegistry

    def _echo(x: int) -> dict:
        return {"echo": x}

    fake_registry = ToolRegistry()
    fake_registry.register(ToolDef(
        name="echo_tool", description="d",
        parameters={"type": "object", "properties": {"x": {"type": "integer"}}},
        func=_echo))

    monkeypatch.setattr(
        mw_mod, "_build_improve_registry",
        lambda *, settings, workdir, staging_dir, source_snapshot_dir, rpc_client,
               inventory_view=None:
        (fake_registry, MissionToolCounters(budget=settings.improve.tool_budget)))

    class _Fake:
        def run(self, mission):
            from agentic_fx.runners.base import MissionResult
            return MissionResult("completed", {}, [])

        def close(self):
            pass

    monkeypatch.setattr(mw_mod.runner_factory, "build_runner",
                        lambda *a, **kw: _Fake())

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    source_snapshot_dir = tmp_path / "source"
    source_snapshot_dir.mkdir()

    settings = _settings_with_improve_backend("local")
    mw_mod._run_improve_mission(
        settings=settings, workdir=tmp_path, staging_dir=str(staging_dir),
        source_snapshot_dir=str(source_snapshot_dir),
        protocol_out=None, out_seq=None, in_seq=None)

    sock_path = tmp_path / "afx.sock"
    assert sock_path.exists(), "afx.sock が bind されていない (B1)"

    # fake mcp_shim クライアント = `run_mcp_shim` の実プロセス (CLI 側の
    # 子プロセスエントリと同じ起動形)。stdin へ 1 行の JSON-RPC を書き、
    # stdout から応答を読む (`test_run_mcp_shim_forwards_stdio_to_unix_socket`
    # と同じパターン、Step 1 の契約済み実装をそのまま流用する)。
    proc = subprocess_mod.Popen(
        [sys.executable, "-m", "agentic_fx.tools.mcp_shim", str(sock_path)],
        stdin=subprocess_mod.PIPE, stdout=subprocess_mod.PIPE, text=True)
    try:
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "echo_tool", "arguments": {"x": 9}}}
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        resp = json.loads(line)
    finally:
        proc.kill()
        proc.wait(timeout=5)
    content = resp["result"]["content"]
    payload = json.loads(content[0]["text"])
    assert payload == {"echo": 9}


def test_run_improve_mission_claude_backend_workdir_matches_dispatcher_socket(
        monkeypatch, tmp_path):
    """Step 7b + B2-r2 是正: `CliRunner.run()` が実際に子プロセスへ渡す
    `--mcp-config` の socket パスと、mission_worker が bind したパス
    (`_start_mcp_dispatcher` → `mcp_socket_path(workdir)`) が一致することを
    **実プロセス**で pin する。

    前回是正 (r2 以前) は `runner._workdir == tmp_path` と
    `(tmp_path / "afx.sock").exists()` の 2 本の assert しかなく、
    `cli_runner.py:88` の `mcp_socket = self._workdir / "afx.sock"` を
    別名 (`afx_MUTATED.sock`) へ変異させても検出できなかった
    (検収 B2-r2: 全スイートで Survived) — その式は `_build_argv` へ渡す
    値の由来を検査しておらず、2 つの独立したリテラルの偶然の一致に
    頼っていたため。

    ここでは `settings.runner.claude.bin` を fake claude CLI (Python
    script) に差し替え、`ClaudeRunner.run()` を実際に呼び出す —
    `cli_runner.py:88` (現在は `mcp_socket_path(self._workdir)` 呼び出し)
    がその実行経路に必ず含まれる。fake CLI は `--mcp-config` の JSON から
    `mcpServers.afx.command`/`args` (= `[sys.executable, "-m",
    "agentic_fx.tools.mcp_shim", <mcp_socket>]`) を読み、実際にそれを
    Popen して `tools/list` を JSON-RPC で投げる — fake mcp_shim が argv の
    socket パスへ接続でき、mission_worker が bind した dispatcher から
    登録済みツールの一覧が返ってくることを確認する。"""
    import agentic_fx.mission_worker as mw_mod
    from agentic_fx.runners.base import Mission
    from agentic_fx.tools.registry import ToolDef, ToolRegistry

    def _echo(x: int) -> dict:
        return {"echo": x}

    fake_registry = ToolRegistry()
    fake_registry.register(ToolDef(
        name="probe_tool", description="d",
        parameters={"type": "object", "properties": {"x": {"type": "integer"}}},
        func=_echo))

    fake_bin = tmp_path / "fake_claude.py"
    fake_bin.write_text(
        f"#!{sys.executable}\n"
        "import json, subprocess, sys\n"
        "argv = sys.argv[1:]\n"
        "mcp_config_path = argv[argv.index('--mcp-config') + 1]\n"
        "cfg = json.loads(open(mcp_config_path).read())\n"
        "server = cfg['mcpServers']['afx']\n"
        "cmd = [server['command']] + server['args']\n"
        "proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, "
        "stdout=subprocess.PIPE, text=True)\n"
        "req = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}}\n"
        "proc.stdin.write(json.dumps(req) + chr(10))\n"
        "proc.stdin.flush()\n"
        "line = proc.stdout.readline()\n"
        "proc.kill()\n"
        "proc.wait(timeout=5)\n"
        "with open(mcp_config_path + '.probe_response', 'w') as f:\n"
        "    f.write(line)\n"
        "print(json.dumps({'type': 'result', 'result': '{}'}))\n"
        "sys.exit(0)\n"
    )
    fake_bin.chmod(0o755)

    settings = _settings_with_improve_backend("claude")
    settings = settings.model_copy(update={
        "runner": settings.runner.model_copy(update={
            "claude": settings.runner.claude.model_copy(
                update={"bin": str(fake_bin)}),
        }),
    })

    monkeypatch.setattr(
        mw_mod, "_build_improve_registry",
        lambda *, settings, workdir, staging_dir, source_snapshot_dir, rpc_client,
               inventory_view=None:
        (fake_registry, MissionToolCounters(budget=settings.improve.tool_budget)))
    # `ClaudeRunner.run()` は `cli_started_sink` 経由で実際に `cli_started`
    # フレームを送出する (`_make_on_message`/`_send_frame` 配線) — 実プロセス
    # を起動するこのテストではその配線を素通りさせるため、
    # protocol_out/out_seq に実物 (in-memory stream + SeqTracker) を渡す。
    from agentic_fx.core.mission_protocol import SeqTracker
    protocol_out = io.BytesIO()
    out_seq = SeqTracker()
    in_seq = SeqTracker()
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    source_snapshot_dir = tmp_path / "source"
    source_snapshot_dir.mkdir()
    runner = mw_mod._run_improve_mission(
        settings=settings, workdir=tmp_path, staging_dir=str(staging_dir),
        source_snapshot_dir=str(source_snapshot_dir),
        protocol_out=protocol_out, out_seq=out_seq, in_seq=in_seq)

    sock_path = mw_mod.mcp_socket_path(tmp_path)
    assert runner._workdir == tmp_path
    assert sock_path.exists(), "dispatcher が bind されていない"

    mission = Mission(prompt="probe", tools=[], output_schema={"type": "object"},
                      max_turns=1, timeout_sec=10.0)
    runner.run(mission)

    probe_path = tmp_path / "mcp.json.probe_response"
    # fake claude CLI は接続の成否に関わらず probe ファイルを作る (接続
    # 失敗時は `readline()` が `''` を返し、空ファイルが書かれる) —
    # `exists()` だけでは「空ファイルが書かれた」ケースを「接続できた」と
    # 誤読しうる (`json.loads("")` の `JSONDecodeError` に落ちるだけの、
    # 意図と無関係な理由での fail)。中身が非空であることを明示的に
    # assert してから decode する。
    raw = probe_path.read_text() if probe_path.exists() else ""
    assert raw.strip(), (
        "fake claude CLI が argv の --mcp-config が指す socket パスへ "
        "接続できなかった (bind パスと argv パスの不一致、B2-r2)")
    resp = json.loads(raw)
    tool_names = {t["name"] for t in resp["result"]["tools"]}
    assert tool_names == {"probe_tool"}, (
        "argv の socket パス経由で mission_worker が bind した dispatcher "
        "に到達できなかった")


def test_start_mcp_dispatcher_fails_closed_when_bind_fails(tmp_path):
    """`_start_mcp_dispatcher` は `McpShimDispatcher.bind()` を呼び出し
    スレッドで同期実行する (B1-r2 是正) — bind の例外はそのまま伝播し、
    `_start_mcp_dispatcher` は `RuntimeError` を送出して fail closed に
    なる。黙って戻ると、CLI へ存在しない socket path を渡し続け B1
    (ツール 0 個) を再発させる。ここでは workdir の親ディレクトリが
    存在しない状態を作り、bind (`ENOENT`) を確実に失敗させて
    red/green を確認する。"""
    import agentic_fx.mission_worker as mw_mod
    from agentic_fx.tools.registry import ToolRegistry

    nonexistent_workdir = tmp_path / "does" / "not" / "exist"
    with pytest.raises(RuntimeError, match="failed to bind"):
        mw_mod._start_mcp_dispatcher(
            workdir=nonexistent_workdir, registry=ToolRegistry())


def test_start_mcp_dispatcher_fails_closed_when_socket_path_is_masked_by_directory(
        tmp_path):
    """B1-r2 是正の回帰テスト (検収 B1-r2 (a) の決定的 masking probe)。

    旧実装 (`sock_path.exists()` を 3 秒ポーリングして bind 成否を判定) は
    「そのパスに何か在るか」という代理観測でしかなく、bind 前から
    `workdir/afx.sock` が (例えばディレクトリとして) 既に存在していると
    `unlink()` が `IsADirectoryError` (OSError のサブクラス) で失敗して
    bind が絶対に成功しないにもかかわらず、`ready: ok=True` を返していた
    — B1 (ツール 0 個) と区別のつかない症状を黙って再導入する。
    ここでは `afx.sock` を先にディレクトリとして作っておき、
    `_start_mcp_dispatcher` が `RuntimeError` で fail closed することを
    pin する (旧 `exists()` ポーリング実装ではこのケースを検出できない
    — ENOENT だけを見るテストが偶然正しい代理になっていた唯一のケース
    だったため)。"""
    import agentic_fx.mission_worker as mw_mod
    from agentic_fx.tools.registry import ToolRegistry

    (tmp_path / "afx.sock").mkdir()
    with pytest.raises(RuntimeError, match="failed to bind"):
        mw_mod._start_mcp_dispatcher(workdir=tmp_path, registry=ToolRegistry())


# --- ローカル 1 周目 pin (2026-09-08、tmp/review-20260908-ma/verified-round1-local.md) ---

# P2
def test_run_improve_mission_shares_one_counters_across_registry_and_runner(
        monkeypatch, tmp_path):
    """ローカル 1 周目 #2 (muse c5 / ornith c5 / qwen c5): registry・
    dispatcher・runner が**同一の** counters を見ていること。(a)
    `after_send=` に別インスタンスの `fire_if_pending` を渡す変異、(b)
    `build_mission_registry(..., counters=None)` に落とす変異、いずれも全
    スイート green で生存していた — どちらが起きても abort_event は永久に
    set されず、run6 (拒否 455 回・48 分) がそのまま再現する。
    tool の拒否は registry 経由の公開挙動で作り、発火は dispatcher が
    実際に配線したフック経由で起こす。"""
    import agentic_fx.mission_worker as mw_mod
    from agentic_fx.config import ImproveToolBudgetSettings

    captured: dict = {}

    def spy(profile, settings, registry, *, workdir, on_message=None,
            cli_started_sink=None, abort_event=None, abort_reason_fn=None,
            after_tool_call=None):
        captured["registry"] = registry
        captured["abort_event"] = abort_event

        class _Fake:
            def run(self, mission):
                raise AssertionError("not used")

            def close(self):
                pass
        return _Fake()

    monkeypatch.setattr(mw_mod.runner_factory, "build_runner", spy)
    staging_dir = tmp_path / "staging"; staging_dir.mkdir()
    source_snapshot_dir = tmp_path / "source"; source_snapshot_dir.mkdir()

    settings = _settings_with_improve_backend("local")
    settings = settings.model_copy(update={
        "improve": settings.improve.model_copy(update={
            "tool_budget": ImproveToolBudgetSettings(
                max_writes=1, max_refusal_streak=1)})})

    runner = mw_mod._run_improve_mission(
        settings=settings, workdir=tmp_path, staging_dir=str(staging_dir),
        source_snapshot_dir=str(source_snapshot_dir),
        protocol_out=None, out_seq=None, in_seq=None)

    # codex 2 周目 Minor: private seam (`_afx_mcp_dispatcher._after_send`) を
    # 直叩きせず、実 Unix socket に tools/call を送って dispatcher 自身に
    # フックを呼ばせる (公開挙動)。dispatcher は finally で必ず close する。
    import socket

    def _call(name, arguments):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(5)
            c.connect(str(mw_mod.mcp_socket_path(tmp_path)))
            c.sendall((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": name, "arguments": arguments}})
                       + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = c.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(json.loads(buf)["result"]["content"][0]["text"])

    try:
        assert _call("write_staging_file",
                     {"name": "a", "rel": "plugin.py", "content": "x = 1\n"}) == {"ok": True}
        assert not captured["abort_event"].is_set()
        refused = _call("write_staging_file",
                        {"name": "a", "rel": "plugin.py", "content": "y = 1\n"})
        assert refused["error"] == "budget exhausted"
        # dispatcher が握るフックと runner に渡った event が同じ counters を
        # 指していなければ、ここで event は立たない。
        assert captured["abort_event"].wait(5)
    finally:
        runner._afx_mcp_dispatcher.close()


def test_run_improve_mission_wires_after_tool_call_for_local_backend(monkeypatch, tmp_path):
    """/code-review 2 周目 #1: worker は factory に `after_tool_call=counters.fire_if_pending`
    を渡す。これが無いと local backend で abort_event が永久に set されない (run6 再現)。"""
    import agentic_fx.mission_worker as mw_mod
    captured = {}

    def spy(profile, settings, registry, *, workdir, on_message=None,
            cli_started_sink=None, abort_event=None, abort_reason_fn=None,
            after_tool_call=None):
        captured["after_tool_call"] = after_tool_call
        captured["abort_event"] = abort_event

        class _Fake:
            def run(self, mission):
                raise AssertionError("not used")
            def close(self):
                pass
        return _Fake()

    monkeypatch.setattr(mw_mod.runner_factory, "build_runner", spy)
    staging_dir = tmp_path / "staging"; staging_dir.mkdir()
    source_snapshot_dir = tmp_path / "source"; source_snapshot_dir.mkdir()
    runner = mw_mod._run_improve_mission(
        settings=_settings_with_improve_backend("local"), workdir=tmp_path,
        staging_dir=str(staging_dir), source_snapshot_dir=str(source_snapshot_dir),
        protocol_out=None, out_seq=None, in_seq=None)
    try:
        counters = runner._afx_mission_counters
        assert captured["after_tool_call"] is not None
        counters.abort_pending = True            # 閾値到達を模す
        assert not captured["abort_event"].is_set()
        captured["after_tool_call"]()            # LocalRunner が tool 実行後に呼ぶフック
        assert captured["abort_event"].is_set()
    finally:
        runner._afx_mcp_dispatcher.close()


def test_local_backend_end_to_end_abort_without_hand_made_state(monkeypatch, tmp_path):
    """3 周目 sonnet #2 (2026-09-08): 多段配線を**手で中間状態を作らず**に 1 本で通す。
    実 counters → 実 registry (staging tool の拒否) → 実 factory (backend=local) →
    実 LocalRunner (HTTP だけ MockTransport) → after_tool_call → event → runner 終端。
    fake LLM は write_staging_file を毎 turn 3 回呼ぶ。max_writes=1 /
    max_refusal_streak=2 なので 2 回目の拒否で pending → フックで event → 3 回目は
    実行されず `failed` + prefix。手で set / 代入する箇所は無い。"""
    import json as _json

    import httpx

    import agentic_fx.mission_worker as mw_mod
    import agentic_fx.runners.local_runner as lr
    from agentic_fx.config import ImproveToolBudgetSettings
    from agentic_fx.runners.base import Mission, is_tool_budget_abort

    calls = []
    msg = {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"c{n}", "type": "function",
         "function": {"name": "write_staging_file", "arguments": _json.dumps(
             {"name": "a", "rel": "plugin.py", "content": f"x = {n}\n"})}}
        for n in (1, 2, 3)]}

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": msg}]})

    real_init = lr.LocalRunner.__init__

    def init_with_mock_transport(self, **kw):   # HTTP だけ差し替え、配線は実物
        real_init(self, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(lr.LocalRunner, "__init__", init_with_mock_transport)
    monkeypatch.setattr(mw_mod, "_bootstrap_improve_profile",
                        lambda *a, **k: None, raising=False)

    staging_dir = tmp_path / "staging"; staging_dir.mkdir()
    source_snapshot_dir = tmp_path / "source"; source_snapshot_dir.mkdir()
    settings = _settings_with_improve_backend("local")
    settings = settings.model_copy(update={
        "improve": settings.improve.model_copy(update={
            "tool_budget": ImproveToolBudgetSettings(
                max_writes=1, max_refusal_streak=2)})})
    # on_message は event フレームを protocol_out へ書けないと os._exit(1) する
    # (fail closed) ので、実物の出力先と seq tracker を渡す。
    import io
    from agentic_fx.core.mission_protocol import SeqTracker
    protocol_out = io.BytesIO()
    runner = mw_mod._run_improve_mission(
        settings=settings, workdir=tmp_path, staging_dir=str(staging_dir),
        source_snapshot_dir=str(source_snapshot_dir),
        protocol_out=protocol_out, out_seq=SeqTracker(), in_seq=SeqTracker())
    try:
        assert isinstance(runner, lr.LocalRunner)
        result = runner.run(Mission(
            prompt="p", tools=["write_staging_file"], output_schema={"type": "object"},
            max_turns=5, timeout_sec=30))
    finally:
        runner._afx_mcp_dispatcher.close()

    assert result.status == "failed"
    assert is_tool_budget_abort(result.reason)
    assert "terminal_refusals" in result.reason
    counters = runner._afx_mission_counters
    assert counters.writes == 1                 # 1 回受理
    assert counters.terminal_refusal_streak == 2  # 2 回目の拒否で閾値 → 3 回目は未実行
    assert counters.abort_event.is_set()
    assert len(calls) == 1                      # 1 turn で終わる



# --- run8 是正 [analyze-corr-rpc-double-unwrap] (2026-09-09) ---

def test_every_improve_rpc_crosses_child_frame_and_parent_wrapper(monkeypatch, tmp_path):
    """run8 欠陥 A: 子 `analyze_corr(request)` は request の中身を RPC に送り、親 wrapper
    `build_ledger_wrapped_rpc_handlers` は `func(**args)` で再 splat するため、本番で
    100% TypeError だった (run_backtest はフラット dict で偶然一致)。契約 = `tool_rpc`
    フレームの `args` は親 tooldef の**キーワード引数 dict**。子 registry.execute →
    子 rpc lambda (フレーム捕捉) → 親 wrapper → 親 raw handler を **全 RPC 種別**で通す。"""
    import json as _json

    import agentic_fx.mission_worker as mw_mod
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
    from agentic_fx.tools.improve_rpc_tools import build_ledger_wrapped_rpc_handlers

    frames = []

    def fake_rpc_client(name, args):
        frames.append((name, args))
        return {"metrics": {"trades": 1, "evaluable": False}} if name == "run_backtest" else {"rows": []}

    monkeypatch.setattr(mw_mod, "_make_rpc_client", lambda *a, **k: fake_rpc_client)
    staging = tmp_path / "staging"; staging.mkdir(); (tmp_path / "source").mkdir()
    settings = _settings_with_improve_backend("local")
    registry, _ = mw_mod._build_improve_registry(
        settings=settings, workdir=tmp_path, staging_dir=staging,
        source_snapshot_dir=tmp_path / "source", rpc_client=fake_rpc_client)
    names = list(registry.names())
    request = {"pairs": ["USDJPY", "EURUSD"], "window": 90}
    out = _json.loads(registry.execute("analyze_corr", {"request": request}, names))
    assert "error" not in out, out
    # run_backtest も子 registry 経由で (loader が通る strategy 候補を staging に置く)
    import shutil
    from pathlib import Path as _P
    shutil.copytree(_P(__file__).resolve().parents[1] / "docs/examples/plugins/sma_cross",
                    staging / "cand")
    out = _json.loads(registry.execute("run_backtest", {"name": "cand", "pair": "USDJPY"}, names))
    assert "error" not in out, out
    # 親側: raw handler を spy に差し替えた wrapper にフレームをそのまま渡す
    received = {}
    parent = build_ledger_wrapped_rpc_handlers(
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 1, "analyze_corr": 1}),
        rpc_handlers={"run_backtest": lambda a: received.setdefault("run_backtest", a) or {"metrics": {}},
                      "analyze_corr": lambda a: received.setdefault("analyze_corr", a) or {"rows": []}},
        staging_dir=None)
    # tripwire (codex Minor 2): 親 handler の集合 = 子 registry が公開する RPC tool の
    # 集合 = このテストが通したフレームの集合。どれかに追加して他を忘れると落ちる。
    rpc_names = {n for n, _ in frames}
    child_rpc_tools = {n for n in names if n in parent}
    assert rpc_names == set(parent) == child_rpc_tools == {"run_backtest", "analyze_corr"}, \
        "RPC 種別を増やしたらこのテストに足す (全種別を子→親で通す)"
    for name, args in frames:
        parent[name](args)                      # ここが本番で TypeError だった
    assert received["analyze_corr"] == request
    assert received["run_backtest"] == {"name": "cand", "pair": "USDJPY"}


def test_local_backend_end_to_end_abort_on_broken_tool_exceptions(monkeypatch, tmp_path):
    """codex Minor 3 (run8 是正): 壊れた tool (例外) の連打が **実配線** で abort に至る —
    実 counters → 実 registry (on_result) → 実 factory (local) → 実 LocalRunner (HTTP のみ Mock)
    → after_tool_call → event → `failed` + `tool_budget_abort:tool_errors:<name>`。手で
    中間状態を作らない。run8 #65 の実形 (analyze_corr 293 回) を max_refusal_streak=3 で再現。"""
    import io
    import json as _json

    import httpx

    import agentic_fx.mission_worker as mw_mod
    import agentic_fx.runners.local_runner as lr
    from agentic_fx.config import ImproveToolBudgetSettings
    from agentic_fx.core.mission_protocol import SeqTracker
    from agentic_fx.runners.base import Mission, is_tool_budget_abort

    calls = []
    msg = {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"c{n}", "type": "function",
         "function": {"name": "analyze_corr",
                      "arguments": _json.dumps({"request": {"pairs": ["USDJPY"]}})}}
        for n in range(6)]}

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": msg}]})

    real_init = lr.LocalRunner.__init__
    monkeypatch.setattr(lr.LocalRunner, "__init__",
                        lambda self, **kw: real_init(self, transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(mw_mod, "_bootstrap_improve_profile", lambda *a, **k: None, raising=False)

    def broken_rpc_client(name, args):          # 親 RPC が毎回例外 (run8 の親側 TypeError 相当)
        raise RuntimeError("simulated parent rpc failure")

    monkeypatch.setattr(mw_mod, "_make_rpc_client", lambda *a, **k: broken_rpc_client)
    staging_dir = tmp_path / "staging"; staging_dir.mkdir()
    source_snapshot_dir = tmp_path / "source"; source_snapshot_dir.mkdir()
    settings = _settings_with_improve_backend("local")
    settings = settings.model_copy(update={"improve": settings.improve.model_copy(update={
        "tool_budget": ImproveToolBudgetSettings(max_refusal_streak=3)})})
    runner = mw_mod._run_improve_mission(
        settings=settings, workdir=tmp_path, staging_dir=str(staging_dir),
        source_snapshot_dir=str(source_snapshot_dir),
        protocol_out=io.BytesIO(), out_seq=SeqTracker(), in_seq=SeqTracker())
    try:
        result = runner.run(Mission(prompt="p", tools=["analyze_corr"],
                                    output_schema={"type": "object"}, max_turns=5, timeout_sec=30))
    finally:
        runner._afx_mcp_dispatcher.close()
    assert result.status == "failed"
    assert is_tool_budget_abort(result.reason)
    assert "tool_errors:analyze_corr" in result.reason
    counters = runner._afx_mission_counters
    assert counters.errors == 3                 # 3 回目で pending → フックで event → 4 回目は未実行
    assert len(calls) == 1


# --- [indicator-consumption-wiring] T3 Step 3-3: trade worker の別 root ----

def test_trade_worker_builds_its_own_inventory_and_passes_indicator_metas(
        tmp_path, monkeypatch):
    """[indicator-consumption-wiring] §2.3: trade worker は **別 root** で
    `approved_plugins` を再実行する (producer との版ずれは許容する)。
    `market_tools` には indicator kind だけが届く。

    **codex plan r1 I3 是正**: v1.2 の本テストは fake `approved_plugins` を
    テスト自身が直接呼び、その結果をテスト自身が `build_mission_registry`
    に渡していた。`mission_worker.py` のコードは 1 行も実行されないので、
    src を何も直さなくても assert が成立する = 記載どおりの Expected FAIL に
    ならない ([[verify-integration-not-just-units]])。**trade worker の実入口を
    起動し、`plugins_dir` / `settings` / `.inventory.metas` / indicator-only
    filtering を spy で観測する**。

    実入口の形は `mission_worker.py` の `main()` trade 分岐から
    `_build_trade_indicator_metas(conn, plugins_dir, settings)` という純粋
    helper に切り出し済み (Step 3-3c) — テストは helper を実物として呼び、
    `approved_plugins` 側だけを spy する (helper 抽出はふるまいを変えない
    — 切り出し前後で `main()` の `approved` 変数の値は同一)。"""
    from unittest.mock import MagicMock

    from agentic_fx import mission_worker
    from agentic_fx.config import load_settings
    from agentic_fx.plugin.loader import PluginMeta
    from agentic_fx.plugin.resolve import ApprovedInventory, InventoryBuildResult

    SETTINGS = load_settings(_REPO_ROOT / "config" / "settings.yaml.example")

    ind = PluginMeta(name="rsi", kind="indicator", path=tmp_path, params={},
                     timeframe=None, pairs=(), max_bars=200,
                     content_hash="a" * 64, outputs=("rsi",))
    strat = PluginMeta(name="s", kind="strategy", path=tmp_path, params={},
                       timeframe="1h", pairs=("USDJPY",), max_bars=200,
                       content_hash="b" * 64)
    seen_args = {}

    def _spy(conn, plugins_dir, *, settings):
        seen_args.update(conn=conn, plugins_dir=plugins_dir, settings=settings)
        return InventoryBuildResult(
            inventory=ApprovedInventory(root=plugins_dir, metas=(ind, strat)),
            phase1_metas=(ind, strat), resolved={}, rejected_strategies=())

    # `mission_worker` は `from agentic_fx.tools import plugin_loader` で
    # **モジュールを** import している (module-level import、Step 3-3c)。
    monkeypatch.setattr(mission_worker.plugin_loader, "approved_plugins", _spy)

    conn = MagicMock()
    metas = mission_worker._build_trade_indicator_metas(
        conn, tmp_path / "plugins", SETTINGS)

    # 実入口が渡した引数 (別 root / settings 透通)
    assert seen_args["plugins_dir"] == tmp_path / "plugins"
    assert seen_args["settings"] is SETTINGS
    assert seen_args["conn"] is conn
    # `.inventory.metas` から indicator kind だけを取り出している
    assert [m.name for m in metas] == ["rsi"]
    assert all(m.kind == "indicator" for m in metas)


def test_trade_worker_indicator_metas_is_empty_without_a_plugins_dir(tmp_path):
    """`plugins_dir is None` (handshake にキーが無い) なら空リスト。
    現行の `if plugins_dir is not None else []` を helper 側で保つ。"""
    from unittest.mock import MagicMock

    from agentic_fx import mission_worker
    from agentic_fx.config import load_settings

    SETTINGS = load_settings(_REPO_ROOT / "config" / "settings.yaml.example")
    assert mission_worker._build_trade_indicator_metas(
        MagicMock(), None, SETTINGS) == []


def test_main_forwards_the_handshake_inventory_view_to_the_improve_mission():
    """[indicator-consumption-wiring] P3 / 段 0 束 3 M2: `main()` の improve
    分岐は `_run_improve_mission(...)` へ **handshake の `inventory_view`**
    を渡す (親 → 子 tool への唯一の経路)。

    `inventory_view=None` へ固定する変異は判定 suite 全体 (1355 passed) が
    green のままだった (実測) — 既存テストは `_run_improve_mission` を
    直接呼ぶ (= main の呼び出し辺を通らない) か、`_build_improve_registry`
    を `inventory_view=None` 既定の lambda で差し替えているだけで、
    **main が実際に何を渡すか**を誰も見ていない。

    `main()` は bootstrap (resource limit / landlock / backend 起動) を
    伴うためテストから素直に駆動できないので、**呼び出し辺そのものを AST
    で pin する** (同種の構造 pin の前例: 本ファイル群の
    `test_no_caller_uses_legacy_approved_plugins_signature`)。
    構造 pin なので「値が実際に子へ届くか」は
    `tests/runners/test_worker_runner.py::
    test_worker_runner_handshake_carries_the_run_context_inventory_view`
    (親 → handshake) と `tests/tools/test_improve_staging_tools.py`
    (view → tool) が別の層で担保する。"""
    import ast
    from pathlib import Path

    import agentic_fx.mission_worker as mw_mod

    tree = ast.parse(Path(mw_mod.__file__).read_text(encoding="utf-8"))
    main_fn = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [n for n in ast.walk(main_fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "_run_improve_mission"]
    assert len(calls) == 1, "main() の _run_improve_mission 呼び出しが 1 本でない"
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert "inventory_view" in kw, "inventory_view を渡していない"
    src = ast.unparse(kw["inventory_view"])
    assert src in ("handshake.get('inventory_view')",
                   "handshake['inventory_view']"), (
        "main() は handshake の inventory_view をそのまま渡すこと "
        f"(実際: {src})")
