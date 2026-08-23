"""Mission worker child process tests (プラン10 Task 5)."""
from __future__ import annotations

import io
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
            cli_started_sink=None):
        captured["profile"] = profile
        captured["backend"] = getattr(
            getattr(settings.runner, profile, None), "backend", None)
        captured["on_message"] = on_message
        # precheck 2026-08-22 pass2: RB3 — cli_started_sink= が
        # build_runner まで届いていることを pin する。
        captured["cli_started_sink"] = cli_started_sink
        # 実 CLI/実 LLM を起動しない fake を返す — 構築経路の到達のみ確認する。
        class _Fake:
            def run(self, mission):
                from agentic_fx.runners.base import MissionResult
                return MissionResult("completed", {}, [])
            def close(self):
                pass
        return _Fake()

    monkeypatch.setattr(mw_mod.runner_factory, "build_runner", spy)
    
    for backend in ["local", "claude", "codex"]:
        captured.clear()
        settings = _settings_with_improve_backend(backend)
        mw_mod._run_improve_mission(
            settings=settings, workdir=tmp_path, protocol_out=None, out_seq=None)
        assert captured["profile"] == "improve", f"backend={backend}"
        assert captured["backend"] == backend, f"backend={backend}"
        # 3 周目レビュー Important-1: on_message が callable として配線されている
        # ことを assert する — 落とすと transcript/event 転送が全 backend で失われる。
        assert callable(captured["on_message"]), f"backend={backend}"
        # precheck 2026-08-22 pass2: RB3 — cli_started_sink も callable として
        # 配線されていることを assert する (§7.1-2 の受入条件、裁定 R1)。
        assert callable(captured["cli_started_sink"]), f"backend={backend}"


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
        lambda *, settings, workdir: fake_registry)

    class _Fake:
        def run(self, mission):
            from agentic_fx.runners.base import MissionResult
            return MissionResult("completed", {}, [])

        def close(self):
            pass

    monkeypatch.setattr(mw_mod.runner_factory, "build_runner",
                        lambda *a, **kw: _Fake())

    settings = _settings_with_improve_backend("local")
    mw_mod._run_improve_mission(
        settings=settings, workdir=tmp_path, protocol_out=None, out_seq=None)

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
        lambda *, settings, workdir: fake_registry)
    # `ClaudeRunner.run()` は `cli_started_sink` 経由で実際に `cli_started`
    # フレームを送出する (`_make_on_message`/`_send_frame` 配線) — 実プロセス
    # を起動するこのテストではその配線を素通りさせるため、
    # protocol_out/out_seq に実物 (in-memory stream + SeqTracker) を渡す。
    from agentic_fx.core.mission_protocol import SeqTracker
    protocol_out = io.BytesIO()
    out_seq = SeqTracker()
    runner = mw_mod._run_improve_mission(
        settings=settings, workdir=tmp_path,
        protocol_out=protocol_out, out_seq=out_seq)

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
