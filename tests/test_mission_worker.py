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
