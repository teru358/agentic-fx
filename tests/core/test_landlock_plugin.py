"""plugin worker 用 Landlock ruleset のテスト。

純関数 (ABI ごとの構造、guarded root、runtime subtree) はそのまま検査し、
kernel の応答は実子 process で確かめる (Landlock は不可逆なので pytest 本体には
掛けない。子は `sys.executable -c` で起こし、SIGSYS 等で死んでも core を
残さないよう RLIMIT_CORE=0 を掛ける)。
"""
from __future__ import annotations

import dataclasses
import errno
import json
import os
import resource
import socket
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from agentic_fx.core import landlock as L

HOST_ABI = L.landlock_abi()
needs_abi8 = pytest.mark.skipif(HOST_ABI < 8, reason="Landlock ABI 8 の host だけで実行")
needs_landlock = pytest.mark.skipif(HOST_ABI < 3, reason="Landlock ABI 3 以上の host だけで実行")

EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples" / "plugins"


# --------------------------------------------------------------------------
# 子 process の harness
# --------------------------------------------------------------------------

PRELUDE = textwrap.dedent("""
    import json, os, sys, errno, socket, threading, signal
    from pathlib import Path
    from agentic_fx.core import landlock as L
    cfg = json.loads(sys.argv[1])
    def probe(fn):
        try:
            return {"ok": fn()}
        except OSError as e:
            return {"errno": e.errno}
    def rd(p):
        return probe(lambda: len(open(p, "rb").read()))
    def ls(p):
        return probe(lambda: len(os.listdir(p)))
    def create(p):
        def f():
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            return 1
        return probe(f)
    def apply():
        kw = dict(repo_root=Path(cfg["repo"]) if cfg.get("repo") else None,
                  home=Path(cfg["home"]) if cfg.get("home") else None)
        dirs = [Path(d) for d in cfg["plugin_dirs"]]
        if cfg.get("assume_abi") is None:
            return L.apply_plugin_ruleset(dirs, **kw)
        return L._apply_plugin_ruleset_for_abi(dirs, cfg["assume_abi"], **kw)
    def emit(obj):
        sys.stdout.write("\\n" + json.dumps(obj) + "\\n")
        sys.stdout.flush()
""")


def _no_core():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run_child(script: str, cfg: dict, *, cwd: Path, env: dict | None = None,
              timeout: float = 120) -> tuple[subprocess.CompletedProcess, dict | None]:
    proc = subprocess.run(
        [sys.executable, "-c", PRELUDE + textwrap.dedent(script), json.dumps(cfg)],
        capture_output=True, text=True, cwd=cwd, timeout=timeout,
        preexec_fn=_no_core, env=env)
    out = None
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("{"):
            out = json.loads(line)
            break
    return proc, out


def run_ok(script: str, cfg: dict, *, cwd: Path, env: dict | None = None) -> dict:
    proc, out = run_child(script, cfg, cwd=cwd, env=env)
    assert proc.returncode == 0 and out is not None, (proc.returncode, proc.stderr[-3000:])
    return out


EACCES = errno.EACCES


# --------------------------------------------------------------------------
# 作業領域
# --------------------------------------------------------------------------

def _write_plugin_dir(d: Path, marker: str) -> Path:
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(f"VALUE = {marker!r}\n")
    (d / "config.yaml").write_text("kind: indicator\noutputs: [x]\n")
    (d / "extra.py").write_text("SECRET = 1\n")
    (d / "plugin.pyc").write_bytes(b"\x00pyc")
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "plugin.cpython-313.pyc").write_bytes(b"\x00pyc")
    return d


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "repo"
    for name in ("data", "config", "logs", "plugins/_staging/cand"):
        (repo / name).mkdir(parents=True)
    (repo / "data" / "agentic.db").write_bytes(b"db")
    (repo / "config" / "settings.yaml").write_text("secret: 1\n")
    (repo / "logs" / "a.log").write_text("log\n")
    (repo / "plugins" / "_staging" / "cand" / "plugin.py").write_text("x = 1\n")
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "token").write_text("token\n")
    deployed = tmp_path / "deployed"
    strat = _write_plugin_dir(deployed / "strat", "strat")
    ind = _write_plugin_dir(deployed / "ind", "ind")
    other = _write_plugin_dir(deployed / "other", "other")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    return dict(root=tmp_path, repo=repo, home=home, strat=strat, ind=ind,
                other=other, outside=outside, cwd=cwd)


def _cfg(world, dirs, **extra):
    return dict(plugin_dirs=[str(world[d]) for d in dirs], repo=str(world["repo"]),
                home=str(world["home"]), outside=str(world["outside"]),
                strat=str(world["strat"]), ind=str(world["ind"]),
                other=str(world["other"]), **extra)


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


# --------------------------------------------------------------------------
# ABI 別の構造 (純関数)
# --------------------------------------------------------------------------

V1_FS = (1 << 13) - 1


@pytest.mark.parametrize("abi,size,net,scope,flags,tsync", [
    (3, 8, False, False, 0, "single_task"),
    (4, 16, True, False, 0, "single_task"),
    (5, 16, True, False, 0, "single_task"),
    (6, 24, True, True, 0, "single_task"),
    (7, 24, True, True, 0, "single_task"),
    (8, 24, True, True, L.RESTRICT_SELF_TSYNC, "applied"),
    (9, 24, True, True, L.RESTRICT_SELF_TSYNC, "applied"),
])
def test_plan_for_abi_matches_the_abi_table(abi, size, net, scope, flags, tsync):
    plan = L.plan_for_abi(abi)
    assert plan.attr_size == size
    assert plan.restrict_flags == flags
    assert plan.tsync == tsync
    assert plan.abi == min(abi, 8)
    assert plan.network == ("applied" if net else "unsupported")
    assert plan.scope == ("applied" if scope else "unsupported")
    assert plan.handled_net == ((1 | 2) if net else 0)
    assert plan.handled_scope == ((1 | 2) if scope else 0)
    # fs は ABI によらず v1 の 13 種 + TRUNCATE。EXECUTE と TRUNCATE を必ず含む
    assert plan.handled_fs == V1_FS | (1 << 14)
    assert plan.handled_fs & 1 and plan.handled_fs & (1 << 14)


@pytest.mark.parametrize("abi", [-1, 0, 1, 2])
def test_plan_for_abi_rejects_abi_below_3(abi):
    with pytest.raises(L.LandlockSetupError) as ei:
        L.plan_for_abi(abi)
    assert ei.value.reason == "landlock_abi_too_old"


def test_tsync_flag_is_only_set_where_the_kernel_accepts_it():
    assert L.RESTRICT_SELF_TSYNC == 1 << 3
    assert [a for a in range(3, 10) if L.plan_for_abi(a).restrict_flags] == [8, 9]


def test_landlock_abi_is_zero_off_x86_64(monkeypatch):
    monkeypatch.setattr(L.platform, "machine", lambda: "aarch64")
    assert L.landlock_abi() == 0


@needs_landlock
def test_landlock_abi_reports_the_kernel_version():
    assert HOST_ABI >= 3


def test_check_single_task_reports_inspection_failure(tmp_path):
    with pytest.raises(L.LandlockSetupError) as ei:
        L.check_single_task(str(tmp_path / "no-such-task-dir"))
    assert ei.value.reason == "landlock_task_inspection_failed"


def test_check_single_task_rejects_more_than_one_task(tmp_path):
    (tmp_path / "1").mkdir()
    (tmp_path / "2").mkdir()
    with pytest.raises(L.LandlockSetupError) as ei:
        L.check_single_task(str(tmp_path))
    assert ei.value.reason == "landlock_multithreaded"
    (tmp_path / "2").rmdir()
    L.check_single_task(str(tmp_path))


# --------------------------------------------------------------------------
# guarded root の判定 (純関数)
# --------------------------------------------------------------------------

REPO = Path("/srv/afx-repo")
HOME = Path("/home/someone")
GUARDED = L.GuardedRoots(
    full=(REPO / "data", REPO / "config", REPO / "logs", HOME / ".config"),
    cover=(REPO, REPO / "plugins", REPO / "plugins" / "_staging", HOME,
           Path("/tmp"), Path("/")))


def test_guarded_roots_lists_the_documented_roots(tmp_path):
    g = L.guarded_roots(tmp_path / "r", tmp_path / "h")
    r, h = (tmp_path / "r").resolve(), (tmp_path / "h").resolve()
    assert set(g.full) == {r / "data", r / "config", r / "logs", h / ".config"}
    assert set(g.cover) == {r, r / "plugins", r / "plugins" / "_staging", h,
                            Path("/tmp").resolve(), Path("/")}


@pytest.mark.parametrize("root", [REPO / "data", REPO / "config", REPO / "logs",
                                  HOME / ".config"])
@pytest.mark.parametrize("rel", ["ancestor", "equal", "descendant"])
@pytest.mark.parametrize("kind", ["runtime", "system", "plugin", "device"])
def test_full_deny_roots_reject_all_three_directions(root, rel, kind):
    path = {"ancestor": root.parent, "equal": root,
            "descendant": root / "sub" / "x"}[rel]
    with pytest.raises(L.LandlockSetupError) as ei:
        L.check_allowlist_path(kind, path, GUARDED)
    assert ei.value.reason == "allowlist_guarded"


@pytest.mark.parametrize("root", [REPO, REPO / "plugins",
                                  REPO / "plugins" / "_staging", HOME,
                                  Path("/tmp"), Path("/")])
@pytest.mark.parametrize("rel", ["ancestor", "equal"])
def test_cover_roots_reject_ancestor_and_equal(root, rel):
    if rel == "ancestor" and root == Path("/"):
        pytest.skip("/ に祖先は無い")
    path = root if rel == "equal" else root.parent
    for kind in ("runtime", "system", "plugin"):
        with pytest.raises(L.LandlockSetupError) as ei:
            L.check_allowlist_path(kind, path, GUARDED)
        assert ei.value.reason == "allowlist_guarded"


@pytest.mark.parametrize("path", [REPO / "src" / "agentic_fx",
                                  REPO / "plugins" / "_staging" / "cand",
                                  HOME / "project" / "x",
                                  Path("/usr/lib")])
def test_cover_roots_allow_descendants_that_are_not_full_deny(path):
    L.check_allowlist_path("plugin", path, GUARDED)


def test_runtime_and_system_under_tmp_are_rejected_but_plugin_leaf_is_allowed():
    for kind in ("runtime", "system"):
        with pytest.raises(L.LandlockSetupError) as ei:
            L.check_allowlist_path(kind, Path("/tmp/venv/lib"), GUARDED)
        assert ei.value.reason == "allowlist_guarded"
    L.check_allowlist_path("plugin", Path("/tmp/deployed/strat"), GUARDED)


def test_plugin_file_entries_are_represented_by_their_parent_dir():
    L.check_allowlist_path("plugin_file", REPO / "data" / "x.py", GUARDED)


@pytest.mark.parametrize("wide", ["/", "/usr", "/usr/local", "/opt", "/var", "/home"])
def test_runtime_root_too_wide_rejects_fixed_roots(wide):
    with pytest.raises(L.LandlockSetupError) as ei:
        L.check_runtime_root(Path(wide), repo_root=REPO, home=HOME)
    assert ei.value.reason == "runtime_root_too_wide"


@pytest.mark.parametrize("path", [HOME, HOME.parent, REPO, REPO.parent])
def test_runtime_root_too_wide_rejects_home_and_repo_root_and_ancestors(path):
    with pytest.raises(L.LandlockSetupError) as ei:
        L.check_runtime_root(path, repo_root=REPO, home=HOME)
    assert ei.value.reason == "runtime_root_too_wide"


@pytest.mark.parametrize("path", [
    "/usr/lib/python3.13", "/usr/local/lib/python3.13", "/opt/py/lib/python3.13",
    str(REPO / "src" / "agentic_fx"), str(REPO / ".venv" / "lib" / "python3.13"),
    str(HOME / ".local" / "share" / "uv" / "python" / "cpython-3.13" / "lib"),
])
def test_runtime_root_too_wide_accepts_subtrees(path):
    L.check_runtime_root(Path(path), repo_root=REPO, home=HOME)


# --------------------------------------------------------------------------
# runtime subtree の算出
# --------------------------------------------------------------------------

def test_runtime_subtree_uses_only_code_root_sysconfig_dirs_and_lib_dynload(tmp_path):
    code = tmp_path / "src" / "agentic_fx"
    stdlib = tmp_path / "py" / "lib" / "python3.13"
    dyn = stdlib / "lib-dynload"
    venv_lib = tmp_path / "venv" / "lib" / "python3.13"
    site = venv_lib / "site-packages"
    unrelated = tmp_path / "unrelated"
    for d in (code, stdlib, dyn, site, unrelated):
        d.mkdir(parents=True)
    paths = {"stdlib": str(stdlib), "platstdlib": str(venv_lib),
             "purelib": str(site), "platlib": str(site),
             "include": str(tmp_path / "include")}
    out = L.runtime_subtree(code, paths=paths, sys_path=[
        str(unrelated), str(dyn), str(tmp_path / "src"), ""])
    assert set(out) == {code.resolve(), stdlib.resolve(), venv_lib.resolve()}
    assert tmp_path.resolve() not in out and (tmp_path / "src").resolve() not in out


def test_runtime_subtree_drops_missing_dirs_and_collapses_descendants(tmp_path):
    code = tmp_path / "code"
    base = tmp_path / "base"
    child = base / "site-packages"
    for d in (code, base, child):
        d.mkdir()
    out = L.runtime_subtree(code, paths={
        "stdlib": str(base), "platstdlib": str(tmp_path / "missing"),
        "purelib": str(child), "platlib": str(child)}, sys_path=[])
    assert set(out) == {code.resolve(), base.resolve()}


def test_runtime_subtree_resolves_symlinks_to_real_dirs(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    code = tmp_path / "code"
    code.mkdir()
    out = L.runtime_subtree(code, paths={"stdlib": str(link)}, sys_path=[])
    assert real.resolve() in out and link not in out


def test_real_runtime_subtree_passes_the_too_wide_check_on_this_host():
    code = Path(L.__file__).resolve().parents[1]
    out = L.runtime_subtree(code)
    assert code.resolve() in out
    for p in out:
        L.check_runtime_root(p, repo_root=L.default_repo_root(), home=L.default_home())
        assert p != Path("/usr") and p != Path("/")


# --------------------------------------------------------------------------
# allowlist の組み立て (fd 系統)
# --------------------------------------------------------------------------

def test_build_allowlist_only_has_read_rules_on_expected_kinds(world):
    fds = _open_fds()
    allow = L.build_allowlist([world["strat"], world["ind"]],
                              repo_root=world["repo"], home=world["home"])
    try:
        kinds = [r.kind for r in allow.rules]
        assert set(kinds) <= {"runtime", "system", "plugin_file", "device"}
        assert kinds.count("plugin_file") == 4
        write_exec = (L._ACCESS_FS_EXECUTE | L._ACCESS_FS_WRITE_FILE
                      | L._ACCESS_FS_TRUNCATE | L._ACCESS_FS_MAKE_REG
                      | L._ACCESS_FS_MAKE_DIR | L._ACCESS_FS_REMOVE_FILE
                      | L._ACCESS_FS_REMOVE_DIR | L._ACCESS_FS_MAKE_SOCK
                      | L._ACCESS_FS_REFER)
        assert all(r.access & write_exec == 0 for r in allow.rules)
        for r in allow.rules:
            if r.kind in ("plugin_file", "device"):
                assert r.access == L._ACCESS_FS_READ_FILE
            else:
                assert r.access == L._ACCESS_FS_READ_FILE | L._ACCESS_FS_READ_DIR
        plugin_files = sorted(str(r.path) for r in allow.rules if r.kind == "plugin_file")
        assert plugin_files == sorted(
            str(world[d].resolve() / n) for d in ("strat", "ind")
            for n in ("plugin.py", "config.yaml"))
        # plugin dir 自体には規則を張らない。dir fd だけを別に保持する
        plugin_dirs = {str(world["strat"].resolve()), str(world["ind"].resolve())}
        assert set(allow.plugin_dir_fds) == plugin_dirs
        assert not any(str(r.path) in plugin_dirs for r in allow.rules)
        assert [str(r.path) for r in allow.rules if r.kind == "device"] == ["/dev/urandom"]
    finally:
        allow.close_all()
    assert _open_fds() == fds


def test_build_allowlist_runtime_and_system_entries_are_the_documented_ones(world):
    allow = L.build_allowlist([world["strat"]], repo_root=world["repo"], home=world["home"])
    try:
        runtime = {r.path for r in allow.rules if r.kind == "runtime"}
        assert runtime == set(L.runtime_subtree(Path(L.__file__).resolve().parents[1]))
        system = {r.path for r in allow.rules if r.kind == "system"}
        assert Path("/usr/share/zoneinfo").resolve() in system
        assert Path("/usr/lib").resolve() in system
        assert all(str(p).startswith(("/usr", "/lib")) for p in system)
    finally:
        allow.close_all()


def test_build_allowlist_resolves_a_symlinked_plugin_dir(world):
    link = world["root"] / "deployed" / "ind_link"
    link.symlink_to(world["ind"])
    allow = L.build_allowlist([link], repo_root=world["repo"], home=world["home"])
    try:
        assert list(allow.plugin_dir_fds) == [str(world["ind"].resolve())]
    finally:
        allow.close_all()


def _expect_build_failure(world, plugin_dir, reason):
    fds = _open_fds()
    with pytest.raises(L.LandlockSetupError) as ei:
        L.build_allowlist([plugin_dir], repo_root=world["repo"], home=world["home"])
    assert ei.value.reason == reason, ei.value
    assert _open_fds() == fds, "失敗しても開いた fd を残さない"


def test_build_allowlist_rejects_plugin_dir_without_plugin_py(world):
    d = world["root"] / "deployed" / "empty"
    d.mkdir()
    _expect_build_failure(world, d, "allowlist_not_leaf")


def test_build_allowlist_rejects_symlinked_plugin_py(world):
    d = world["root"] / "deployed" / "linked_main"
    d.mkdir()
    (d / "plugin.py").symlink_to(world["outside"])
    (d / "config.yaml").write_text("kind: indicator\n")
    _expect_build_failure(world, d, "allowlist_not_leaf")


def test_build_allowlist_rejects_plugin_py_that_is_a_directory(world):
    d = world["root"] / "deployed" / "dir_main"
    (d / "plugin.py").mkdir(parents=True)
    (d / "config.yaml").write_text("kind: indicator\n")
    _expect_build_failure(world, d, "allowlist_not_leaf")


def test_build_allowlist_rejects_symlinked_config_yaml(world):
    d = world["root"] / "deployed" / "linked_cfg"
    d.mkdir()
    (d / "plugin.py").write_text("x = 1\n")
    (d / "config.yaml").symlink_to(world["outside"])
    _expect_build_failure(world, d, "plugin_file_invalid")


def test_build_allowlist_rejects_missing_config_yaml(world):
    d = world["root"] / "deployed" / "no_cfg"
    d.mkdir()
    (d / "plugin.py").write_text("x = 1\n")
    _expect_build_failure(world, d, "plugin_file_invalid")


def test_build_allowlist_rejects_config_yaml_that_is_a_fifo(world):
    d = world["root"] / "deployed" / "fifo_cfg"
    d.mkdir()
    (d / "plugin.py").write_text("x = 1\n")
    os.mkfifo(d / "config.yaml")
    _expect_build_failure(world, d, "plugin_file_invalid")


def test_build_allowlist_reports_fd_exhaustion_at_plugin_file_open_as_fd_open_failed(world):
    # plugin ファイルを開く段で fd 上限に達したとき、候補の不正ではなく fd の問題として返す
    script = """
        real_open = os.open
        def limited(path, flags, *a, **k):
            if path == "plugin.py" and flags & os.O_PATH:
                free = os.dup(0)
                os.close(free)
                resource.setrlimit(resource.RLIMIT_NOFILE, (free, free))
            return real_open(path, flags, *a, **k)
        import resource
        os.open = limited
        try:
            L.build_allowlist([Path(cfg["strat"])], repo_root=Path(cfg["repo"]),
                              home=Path(cfg["home"]))
            err = None
        except L.LandlockSetupError as e:
            err = e.reason
        emit({"err": err})
    """
    out = run_ok(script, _cfg(world, ["strat"]), cwd=world["cwd"])
    assert out == {"err": "fd_open_failed"}


def test_build_allowlist_rejects_missing_plugin_dir(world):
    _expect_build_failure(world, world["root"] / "deployed" / "nope", "fd_open_failed")


@pytest.mark.parametrize("where", ["data", "config", "logs", ".config", "repo", "plugins", "staging", "home"])
def test_build_allowlist_rejects_plugin_dir_on_guarded_roots(world, where):
    target = {"data": world["repo"] / "data", "config": world["repo"] / "config",
              "logs": world["repo"] / "logs", ".config": world["home"] / ".config",
              "repo": world["repo"], "plugins": world["repo"] / "plugins",
              "staging": world["repo"] / "plugins" / "_staging",
              "home": world["home"]}[where]
    if where in ("data", "config", "logs", ".config"):
        # 子孫に plugin.py を置いても拒否される
        sub = target / "p"
        sub.mkdir()
        (sub / "plugin.py").write_text("x = 1\n")
        (sub / "config.yaml").write_text("k: 1\n")
        target = sub
    _expect_build_failure(world, target, "allowlist_guarded")


def test_build_allowlist_rejects_symlink_that_points_into_guarded_data(world):
    sub = world["repo"] / "data" / "p"
    sub.mkdir()
    (sub / "plugin.py").write_text("x = 1\n")
    (sub / "config.yaml").write_text("k: 1\n")
    link = world["root"] / "deployed" / "sneaky"
    link.symlink_to(sub)
    _expect_build_failure(world, link, "allowlist_guarded")


def test_build_allowlist_rejects_tmp_root_as_plugin_dir(world):
    _expect_build_failure(world, Path("/tmp"), "allowlist_guarded")


# --------------------------------------------------------------------------
# 実 kernel: 子 process で Landlock を掛ける
# --------------------------------------------------------------------------

PROBE_SCRIPT = """
    ap = apply()
    res = {"fields": ap.attestation_fields(), "dirs": {}}
    for name, d in (("strat", cfg["strat"]), ("ind", cfg["ind"]), ("other", cfg["other"])):
        selected = os.path.realpath(d) in ap.plugin_dir_fds
        r = {"selected": selected,
             "plugin_py": rd(d + "/plugin.py"), "config": rd(d + "/config.yaml"),
             "extra": rd(d + "/extra.py"), "pyc": rd(d + "/plugin.pyc"),
             "pycache_ls": ls(d + "/__pycache__"), "pycache_rd": rd(d + "/__pycache__/plugin.cpython-313.pyc"),
             "ls": ls(d), "create": create(d + "/new.py"),
             "write_open": probe(lambda: os.close(os.open(d + "/plugin.py", os.O_WRONLY))),
             "truncate": probe(lambda: os.truncate(d + "/plugin.py", 0)),
             "rename": probe(lambda: os.rename(d + "/plugin.py", d + "/moved.py")),
             "unlink": probe(lambda: os.unlink(d + "/plugin.py"))}
        if selected:
            fd = ap.plugin_dir_fds[os.path.realpath(d)]
            def via_fd(n):
                def f():
                    x = os.open(n, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                    try:
                        return len(os.read(x, 1 << 20))
                    finally:
                        os.close(x)
                return probe(f)
            r["fd_plugin_py"] = via_fd("plugin.py")
            r["fd_config"] = via_fd("config.yaml")
            r["fd_extra"] = via_fd("extra.py")
            r["fd_create"] = probe(lambda: os.close(os.open("new.py", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd)))
        res["dirs"][name] = r
    res["outside"] = {"rd": rd(cfg["outside"]), "ls": ls(os.path.dirname(cfg["outside"])),
                      "create": create(os.path.dirname(cfg["outside"]) + "/new.txt"),
                      "truncate": probe(lambda: os.truncate(cfg["outside"], 0))}
    repo, home = cfg["repo"], cfg["home"]
    res["guarded"] = {
        "data_db": rd(repo + "/data/agentic.db"), "data_ls": ls(repo + "/data"),
        "config_file": rd(repo + "/config/settings.yaml"), "config_ls": ls(repo + "/config"),
        "logs_file": rd(repo + "/logs/a.log"), "logs_ls": ls(repo + "/logs"),
        "staging": rd(repo + "/plugins/_staging/cand/plugin.py"),
        "staging_ls": ls(repo + "/plugins/_staging"), "plugins_ls": ls(repo + "/plugins"),
        "repo_ls": ls(repo), "home_token": rd(home + "/.config/token"), "home_ls": ls(home + "/.config"),
        "home_root_ls": ls(home)}
    res["sys"] = {"urandom": probe(lambda: len(open("/dev/urandom", "rb").read(16))),
                  "dev_null": probe(lambda: len(open("/dev/null", "rb").read(1))),
                  "dev_ls": ls("/dev"), "etc_passwd": rd("/etc/passwd"), "etc_ls": ls("/etc"),
                  "proc": rd("/proc/self/status"), "tmp_ls": ls("/tmp"), "run_ls": ls("/run")}
    emit(res)
"""

LAYOUTS = {
    "main_and_indicator": ["strat", "ind"],
    "main_only": ["strat"],
    "indicator_only": ["ind"],
}


@needs_abi8
@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_applied_child_reads_only_the_selected_plugin_files(world, layout):
    selected = LAYOUTS[layout]
    res = run_ok(PROBE_SCRIPT, _cfg(world, selected), cwd=world["cwd"])
    for name, r in res["dirs"].items():
        if name in selected:
            assert r["selected"]
            assert r["plugin_py"]["ok"] > 0 and r["config"]["ok"] > 0
            assert r["fd_plugin_py"]["ok"] > 0 and r["fd_config"]["ok"] > 0
            assert r["fd_extra"] == {"errno": EACCES}
            assert r["fd_create"] == {"errno": EACCES}
        else:
            assert not r["selected"]
            assert r["plugin_py"] == {"errno": EACCES}
            assert r["config"] == {"errno": EACCES}
        for key in ("extra", "pyc", "pycache_ls", "pycache_rd", "ls", "create",
                    "write_open", "truncate", "rename", "unlink"):
            assert r[key] == {"errno": EACCES}, (name, key, r[key])


@needs_abi8
def test_applied_child_cannot_reach_guarded_paths_or_system_places(world):
    res = run_ok(PROBE_SCRIPT, _cfg(world, ["strat", "ind"]), cwd=world["cwd"])
    assert all(v == {"errno": EACCES} for v in res["guarded"].values()), res["guarded"]
    assert all(v == {"errno": EACCES} for v in res["outside"].values()), res["outside"]
    sysr = res["sys"]
    assert sysr["urandom"] == {"ok": 16}
    assert all(v == {"errno": EACCES} for k, v in sysr.items() if k != "urandom"), sysr
    assert (world["outside"].read_text(), (world["repo"] / "data" / "agentic.db").read_bytes()) \
        == ("outside\n", b"db")


@needs_abi8
def test_applied_child_attests_abi8_fields(world):
    res = run_ok(PROBE_SCRIPT, _cfg(world, ["strat"]), cwd=world["cwd"])
    assert res["fields"] == {"landlock_fs_abi": 8, "landlock_tsync": "applied",
                             "network": "applied", "scope": "applied"}


@needs_abi8
def test_real_guarded_paths_of_this_repo_and_home_are_unreachable(world):
    """実 repo (この module の位置から導く) と実 $HOME の guarded path を
    O_RDONLY で開く / 列挙するだけ (読み出しと変更はしない)。"""
    repo, home = L.default_repo_root(), L.default_home()
    targets = [p for p in (repo / "data", repo / "config", repo / "logs",
                           home / ".config") if p.is_dir()]
    assert targets, "config/ は repo に必ずある"
    cfg = dict(plugin_dirs=[str(world["strat"])], targets=[str(t) for t in targets])
    script = """
        ap = L.apply_plugin_ruleset([Path(cfg["plugin_dirs"][0])])
        out = {}
        for t in cfg["targets"]:
            out[t] = {"open": probe(lambda: os.close(os.open(t, os.O_RDONLY))),
                      "ls": ls(t)}
        emit(out)
    """
    out = run_ok(script, cfg, cwd=world["cwd"])
    for t, r in out.items():
        assert r == {"open": {"errno": EACCES}, "ls": {"errno": EACCES}}, t


@needs_abi8
def test_applied_child_cannot_exec_anything(world):
    script = """
        L.apply_plugin_ruleset([Path(d) for d in cfg["plugin_dirs"]],
                               repo_root=Path(cfg["repo"]), home=Path(cfg["home"]))
        ld = [p for p in ("/usr/lib64/ld-linux-x86-64.so.2",
                          "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
                          "/lib64/ld-linux-x86-64.so.2") if os.path.exists(p)][0]
        res = {"ld": probe(lambda: os.execv(ld, [ld, "--version"])),
               "python": probe(lambda: os.execv(sys.executable, [sys.executable, "-c", "0"])),
               "bin_true": probe(lambda: os.execv("/usr/bin/true", ["true"]))}
        emit(res)
    """
    out = run_ok(script, _cfg(world, ["strat"]), cwd=world["cwd"])
    assert out == {k: {"errno": EACCES} for k in ("ld", "python", "bin_true")}


class _Listener:
    """親側の待受。child が届いたら hits が増える。"""

    def __init__(self, family: int, addr):
        self.sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock.bind(addr)
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.hits = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                c, _ = self.sock.accept()
            except (TimeoutError, OSError):
                continue
            self.hits += 1
            c.close()

    def close(self):
        self._stop.set()
        self._t.join(2)
        self.sock.close()


@needs_abi8
def test_applied_child_cannot_reach_tcp_or_abstract_uds_or_signal_the_parent(world):
    tcp = _Listener(socket.AF_INET, ("127.0.0.1", 0))
    abstract = _Listener(socket.AF_UNIX, f"\0afx-ll-test-{os.getpid()}")
    try:
        script = """
            L.apply_plugin_ruleset([Path(d) for d in cfg["plugin_dirs"]],
                                   repo_root=Path(cfg["repo"]), home=Path(cfg["home"]))
            def tcp():
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(2)
                s.connect(("127.0.0.1", cfg["port"]))
            def bind():
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(("127.0.0.1", 0))
            def uds():
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(2)
                s.connect("\\0" + cfg["abstract"])
            emit({"connect": probe(tcp), "bind": probe(bind), "abstract": probe(uds),
                  "signal": probe(lambda: os.kill(os.getppid(), signal.SIGWINCH))})
        """
        cfg = _cfg(world, ["strat"], port=tcp.sock.getsockname()[1],
                   abstract=f"afx-ll-test-{os.getpid()}")
        out = run_ok(script, cfg, cwd=world["cwd"])
        assert out["connect"] == {"errno": EACCES}
        assert out["bind"] == {"errno": EACCES}
        assert out["abstract"] == {"errno": errno.EPERM}   # scope は EPERM を返す
        assert out["signal"] == {"errno": errno.EPERM}
        assert (tcp.hits, abstract.hits) == (0, 0)
    finally:
        tcp.close()
        abstract.close()


@needs_abi8
def test_abstract_uds_is_reachable_without_the_scope_control(world):
    """scope が効いていることの対照: ABI 5 の分岐 (scope なし) では届く。"""
    abstract = _Listener(socket.AF_UNIX, f"\0afx-ll-ctl-{os.getpid()}")
    try:
        script = """
            ap = apply()
            def uds():
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(2)
                s.connect("\\0" + cfg["abstract"])
            emit({"fields": ap.attestation_fields(), "abstract": probe(uds)})
        """
        out = run_ok(script, _cfg(world, ["strat"], assume_abi=5,
                                  abstract=f"afx-ll-ctl-{os.getpid()}"), cwd=world["cwd"])
        assert out["fields"]["scope"] == "unsupported"
        assert out["abstract"] == {"ok": None}
    finally:
        abstract.close()
    assert abstract.hits == 1


# --------------------------------------------------------------------------
# 正常 workload (Landlock だけを掛けた直接子)
# --------------------------------------------------------------------------

WORKLOAD = """
    ap = apply()
    import numpy as np
    import pandas as pd
    from agentic_fx.core import plugin_contract
    rng = np.random.default_rng(7)
    idx = pd.date_range("2026-01-01", periods=400, freq="1h", tz="UTC")
    close = 100 + np.cumsum(rng.normal(size=400))
    df = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1,
                       "close": close, "volume": 1.0}, index=idx)
    roll = df["close"].rolling(20).mean().iloc[-1]
    tokyo = df.tz_convert("Asia/Tokyo").index[-1].isoformat()
    ny = df.tz_convert("America/New_York").index[-1].isoformat()
    a = np.ones((300, 300))
    blas = float((a @ a)[0, 0])
    box = {}
    def work():
        box["t"] = float((a @ a)[1, 1])
    th = threading.Thread(target=work)
    th.start(); th.join()
    # loader と同じ経路: 検査済み dir fd から読んだ bytes を exec する
    fd = ap.plugin_dir_fds[os.path.realpath(cfg["ind"])]
    def read(n):
        x = os.open(n, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        try:
            return os.read(x, 1 << 20)
        finally:
            os.close(x)
    src = read("plugin.py")
    mod = {"__name__": "indicator_rsi"}
    exec(compile(src, cfg["ind"] + "/plugin.py", "exec", dont_inherit=True), mod)
    out = mod["compute"](df, {"period": 14})
    plugin_contract.validate_indicator_result(out, df_index=df.index, outputs=["rsi"])
    emit({"roll": roll, "tokyo": tokyo, "ny": ny, "blas": blas, "thread": box["t"],
          "rsi": [float(v) for v in out["rsi"].iloc[-5:]],
          "rsi_nan": int(out["rsi"].isna().sum())})
"""


def _expected_workload(ind_dir: Path) -> dict:
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(7)
    idx = pd.date_range("2026-01-01", periods=400, freq="1h", tz="UTC")
    close = 100 + np.cumsum(rng.normal(size=400))
    df = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1,
                       "close": close, "volume": 1.0}, index=idx)
    ns: dict = {"__name__": "indicator_rsi"}
    exec(compile((ind_dir / "plugin.py").read_bytes(), "x", "exec"), ns)
    rsi = ns["compute"](df, {"period": 14})["rsi"]
    return {"roll": float(df["close"].rolling(20).mean().iloc[-1]),
            "tokyo": df.tz_convert("Asia/Tokyo").index[-1].isoformat(),
            "ny": df.tz_convert("America/New_York").index[-1].isoformat(),
            "rsi": [float(v) for v in rsi.iloc[-5:]], "rsi_nan": int(rsi.isna().sum())}


@pytest.fixture
def example_indicator(world):
    import shutil
    d = world["root"] / "deployed" / "rsi_indicator"
    shutil.copytree(EXAMPLES / "rsi_indicator", d)
    world["ind"] = d
    return d


@needs_abi8
def test_normal_workload_runs_to_completion_under_the_applied_ruleset(world, example_indicator):
    env = dict(os.environ, OPENBLAS_NUM_THREADS="4")
    out = run_ok(WORKLOAD, _cfg(world, ["strat", "ind"]), cwd=world["cwd"], env=env)
    exp = _expected_workload(example_indicator)
    assert out["blas"] == 300.0 and out["thread"] == 300.0
    for k in ("roll", "tokyo", "ny", "rsi", "rsi_nan"):
        assert out[k] == exp[k], k


@needs_abi8
def test_normal_workload_runs_with_the_indicator_alone(world, example_indicator):
    out = run_ok(WORKLOAD, _cfg(world, ["ind"]), cwd=world["cwd"])
    assert out["rsi"] == _expected_workload(example_indicator)["rsi"]


@needs_landlock
@pytest.mark.parametrize("assume", [3, 4, 5, 6, 7])
def test_older_abi_branches_apply_and_run_the_workload_on_this_host(world, example_indicator, assume):
    """旧 ABI の分岐 (struct 大きさ・flags 0・single-task) を新しい kernel 上で
    実際に通す。kernel の応答は本物 (ABI の実測そのものではない)。"""
    if assume > HOST_ABI:
        pytest.skip("host の ABI を超える")
    script = WORKLOAD.replace('emit({"roll"', 'emit({"fields": ap.attestation_fields(), "roll"')
    out = run_ok(script, _cfg(world, ["ind"], assume_abi=assume), cwd=world["cwd"])
    plan = L.plan_for_abi(assume)
    assert out["fields"] == {"landlock_fs_abi": assume, "landlock_tsync": "single_task",
                             "network": plan.network, "scope": plan.scope}
    assert out["rsi"] == _expected_workload(example_indicator)["rsi"]


@needs_landlock
@pytest.mark.parametrize("assume", [3, 5, 7])
def test_older_abi_branches_still_deny_files(world, assume):
    if assume > HOST_ABI:
        pytest.skip("host の ABI を超える")
    res = run_ok(PROBE_SCRIPT, _cfg(world, ["strat"], assume_abi=assume), cwd=world["cwd"])
    assert res["dirs"]["strat"]["plugin_py"]["ok"] > 0
    assert res["dirs"]["strat"]["extra"] == {"errno": EACCES}
    assert res["outside"]["rd"] == {"errno": EACCES}
    assert res["outside"]["truncate"] == {"errno": EACCES}
    assert res["guarded"]["data_db"] == {"errno": EACCES}


def test_internal_abi_override_cannot_raise_above_the_kernel(world):
    script = """
        ap = apply()
        emit({"abi": ap.plan.abi})
    """
    out = run_ok(script, _cfg(world, ["strat"], assume_abi=99), cwd=world["cwd"])
    assert out["abi"] == min(HOST_ABI, 8)


# --------------------------------------------------------------------------
# TSYNC / single-task
# --------------------------------------------------------------------------

THREAD_BEFORE_APPLY = """
    started, go, done = threading.Event(), threading.Event(), threading.Event()
    box = {}
    def worker():
        started.set(); go.wait(10)
        box["read"] = rd(cfg["outside"])
        done.set()
    t = threading.Thread(target=worker); t.start(); started.wait(5)
    ntasks = threading.active_count()
    mode = cfg["mode"]
    err = None
    try:
        if mode == "tsync":
            apply()
        elif mode == "no_tsync_control":
            allow = L.build_allowlist([Path(d) for d in cfg["plugin_dirs"]],
                                      repo_root=Path(cfg["repo"]), home=Path(cfg["home"]))
            import dataclasses
            L.restrict_with_allowlist(allow, dataclasses.replace(L.plan_for_abi(8), restrict_flags=0))
        elif mode == "old_abi":
            apply()
    except L.LandlockSetupError as e:
        err = e.reason
    go.set(); done.wait(10)
    emit({"err": err, "thread_read": box["read"], "main_read": rd(cfg["outside"]),
          "tasks": ntasks})
"""


@needs_abi8
def test_tsync_restricts_a_thread_started_before_apply(world):
    out = run_ok(THREAD_BEFORE_APPLY, _cfg(world, ["strat"], mode="tsync"), cwd=world["cwd"])
    assert out["err"] is None
    assert out["thread_read"] == {"errno": EACCES}
    assert out["main_read"] == {"errno": EACCES}


@needs_abi8
def test_without_tsync_a_pre_existing_thread_keeps_reading_files(world):
    """TSYNC が効いている証拠の対照: flags 0 では別 thread が外部 file を読める。"""
    out = run_ok(THREAD_BEFORE_APPLY, _cfg(world, ["strat"], mode="no_tsync_control"),
                 cwd=world["cwd"])
    assert out["main_read"] == {"errno": EACCES}
    assert out["thread_read"]["ok"] > 0


@needs_landlock
@pytest.mark.parametrize("assume", [3, 5, 7])
def test_single_task_branch_rejects_a_multithreaded_process_and_applies_nothing(world, assume):
    if assume > HOST_ABI:
        pytest.skip("host の ABI を超える")
    out = run_ok(THREAD_BEFORE_APPLY, _cfg(world, ["strat"], mode="old_abi", assume_abi=assume),
                 cwd=world["cwd"])
    assert out["err"] == "landlock_multithreaded"
    assert out["tasks"] == 2
    assert out["thread_read"]["ok"] > 0 and out["main_read"]["ok"] > 0


@needs_landlock
@pytest.mark.parametrize("assume", [3, 5, 7])
def test_thread_started_while_building_allowlist_is_caught_before_applying(world, assume):
    """早い検査の後、規則を組んでいる最中に増えた thread にも domain は及ばない。
    適用の直前にもう一度検査して、何も掛けずに落とす。"""
    if assume > HOST_ABI:
        pytest.skip("host の ABI を超える")
    script = """
        orig = L.build_allowlist
        keep = threading.Event()
        def building(*a, **k):
            allow = orig(*a, **k)
            threading.Thread(target=keep.wait, daemon=True).start()
            return allow
        L.build_allowlist = building
        try:
            apply()
            err = None
        except L.LandlockSetupError as e:
            err = e.reason
        emit({"err": err, "main_read": rd(cfg["outside"]),
              "threads": threading.active_count()})
    """
    out = run_ok(script, _cfg(world, ["strat"], assume_abi=assume), cwd=world["cwd"])
    assert out["err"] == "landlock_multithreaded"
    assert out["threads"] == 2
    assert out["main_read"]["ok"] > 0


@needs_abi8
def test_abi8_does_not_need_a_single_task_process(world):
    script = """
        t = threading.Thread(target=lambda: threading.Event().wait(1)); t.start()
        ap = apply()
        emit({"tsync": ap.plan.tsync, "tasks": threading.active_count()})
    """
    out = run_ok(script, _cfg(world, ["strat"]), cwd=world["cwd"])
    assert out["tsync"] == "applied" and out["tasks"] >= 2


# --------------------------------------------------------------------------
# plugin dir に READ_DIR を戻す対照 (規則の形が効いている証拠)
# --------------------------------------------------------------------------

@needs_abi8
def test_control_with_read_dir_on_plugin_dir_exposes_siblings(world):
    script = """
        allow = L.build_allowlist([Path(d) for d in cfg["plugin_dirs"]],
                                  repo_root=Path(cfg["repo"]), home=Path(cfg["home"]))
        for p, fd in allow.plugin_dir_fds.items():
            allow.rules.append(L.Rule("plugin", Path(p), fd,
                                      L._ACCESS_FS_READ_FILE | L._ACCESS_FS_READ_DIR))
        L.restrict_with_allowlist(allow, L.plan_for_abi(8))
        d = cfg["strat"]
        emit({"extra": rd(d + "/extra.py"), "ls": ls(d), "pyc": rd(d + "/plugin.pyc")})
    """
    out = run_ok(script, _cfg(world, ["strat"]), cwd=world["cwd"])
    assert out["extra"]["ok"] > 0 and out["pyc"]["ok"] > 0 and out["ls"]["ok"] > 0


# --------------------------------------------------------------------------
# kernel 側の失敗を実際に起こす
# --------------------------------------------------------------------------

@needs_abi8
@pytest.mark.parametrize("what,reason", [
    ("create", "landlock_create_failed"),
    ("add_rule", "landlock_add_rule_failed"),
    ("restrict", "landlock_restrict_failed"),
])
def test_kernel_failures_map_to_fixed_reasons_and_leave_the_process_unrestricted(world, what, reason):
    script = """
        allow = L.build_allowlist([Path(d) for d in cfg["plugin_dirs"]],
                                  repo_root=Path(cfg["repo"]), home=Path(cfg["home"]))
        plan = L.plan_for_abi(8)
        what = cfg["what"]
        if what == "create":
            import dataclasses
            plan = dataclasses.replace(plan, attr_size=3)
        elif what == "add_rule":
            os.close(allow.rules[0].fd)
        elif what == "restrict":
            import dataclasses
            plan = dataclasses.replace(plan, restrict_flags=1 << 20)
        try:
            L.restrict_with_allowlist(allow, plan)
            err = None
        except L.LandlockSetupError as e:
            err = e.reason
        emit({"err": err, "outside": rd(cfg["outside"])})
    """
    out = run_ok(script, _cfg(world, ["strat"], what=what), cwd=world["cwd"])
    assert out["err"] == reason
    assert out["outside"]["ok"] > 0


@needs_abi8
def test_apply_failure_closes_every_fd_it_opened(world):
    d = world["root"] / "deployed" / "no_cfg"
    d.mkdir()
    (d / "plugin.py").write_text("x = 1\n")
    script = """
        before = len(os.listdir("/proc/self/fd"))
        try:
            L.apply_plugin_ruleset([Path(cfg["bad"])], repo_root=Path(cfg["repo"]),
                                   home=Path(cfg["home"]))
            err = None
        except L.LandlockSetupError as e:
            err = e.reason
        emit({"err": err, "leak": len(os.listdir("/proc/self/fd")) - before,
              "outside": rd(cfg["outside"])})
    """
    out = run_ok(script, _cfg(world, ["strat"], bad=str(d)), cwd=world["cwd"])
    assert out == {"err": "plugin_file_invalid", "leak": 0, "outside": out["outside"]}
    assert out["outside"]["ok"] > 0


@needs_abi8
def test_apply_keeps_only_plugin_dir_fds_open(world):
    script = """
        def open_fds():
            out = set()
            for i in range(1024):   # 適用後は /proc を読めないので fstat で数える
                try:
                    os.fstat(i)
                    out.add(i)
                except OSError:
                    pass
            return out
        before = open_fds()
        ap = apply()
        after = open_fds()
        emit({"new": len(after - before), "plugin": len(ap.plugin_dir_fds),
              "closed_back": len(before - after)})
    """
    out = run_ok(script, _cfg(world, ["strat", "ind"]), cwd=world["cwd"])
    assert out["new"] == out["plugin"] == 2


def test_apply_below_min_abi_is_rejected_without_touching_the_process(world):
    script = """
        try:
            L._apply_plugin_ruleset_for_abi([Path(d) for d in cfg["plugin_dirs"]], 2,
                                            repo_root=Path(cfg["repo"]), home=Path(cfg["home"]))
            err = None
        except L.LandlockSetupError as e:
            err = e.reason
        emit({"err": err, "outside": rd(cfg["outside"])})
    """
    out = run_ok(script, _cfg(world, ["strat"]), cwd=world["cwd"])
    assert out["err"] == "landlock_abi_too_old"
    assert out["outside"]["ok"] > 0


def test_public_apply_has_no_argument_that_lowers_the_abi():
    import inspect
    params = inspect.signature(L.apply_plugin_ruleset).parameters
    assert set(params) == {"plugin_dirs", "repo_root", "home"}


def test_existing_restrict_to_api_is_unchanged():
    import inspect
    sig = inspect.signature(L.restrict_to)
    assert list(sig.parameters) == ["read_only_paths", "read_write_paths",
                                    "execute_paths", "execute_file_paths"]
    assert L._HANDLED_ACCESS_FS == V1_FS | (1 << 14)
    assert dataclasses.is_dataclass(L.RulesetPlan)


# --------------------------------------------------------------------------
# 境界の追加検査
# --------------------------------------------------------------------------

def test_check_single_task_rejects_an_empty_task_dir(tmp_path):
    # 「1 本だけ」であって「2 本以上でない」ではない
    with pytest.raises(L.LandlockSetupError) as ei:
        L.check_single_task(str(tmp_path))
    assert ei.value.reason in ("landlock_multithreaded", "landlock_task_inspection_failed")


DEEP_REPO = Path("/srv/a/b/afx-repo")
DEEP_GUARDED = L.GuardedRoots(
    full=(DEEP_REPO / "data", DEEP_REPO / "config", DEEP_REPO / "logs", HOME / ".config"),
    cover=(DEEP_REPO, DEEP_REPO / "plugins", DEEP_REPO / "plugins" / "_staging", HOME,
           Path("/tmp"), Path("/")))


@pytest.mark.parametrize("path", [Path("/srv/a"), Path("/srv")])
@pytest.mark.parametrize("kind", ["runtime", "system", "plugin"])
def test_guarded_roots_reject_ancestors_several_levels_up(path, kind):
    # 1 段上の親が guarded root でない深さの祖先でも、覆う規則は拒否する
    with pytest.raises(L.LandlockSetupError) as ei:
        L.check_allowlist_path(kind, path, DEEP_GUARDED)
    assert ei.value.reason == "allowlist_guarded"


@pytest.mark.parametrize("path", [Path("/srv/a"), Path("/srv")])
def test_runtime_root_too_wide_rejects_ancestors_several_levels_up(path):
    with pytest.raises(L.LandlockSetupError) as ei:
        L.check_runtime_root(path, repo_root=DEEP_REPO, home=HOME)
    assert ei.value.reason == "runtime_root_too_wide"


def test_build_allowlist_rejects_a_device_entry_that_is_not_a_character_device(world, monkeypatch):
    fake = world["root"] / "urandom"
    fake.write_bytes(b"not a device")
    monkeypatch.setattr(L, "_DEVICE_PATH", str(fake))
    _expect_build_failure(world, world["strat"], "fd_open_failed")


def test_build_allowlist_takes_one_dir_fd_for_a_plugin_dir_given_twice(world):
    # 別名 (symlink) で同じ plugin dir が 2 回来ても、dir fd は 1 本で後始末で全部閉じる
    link = world["root"] / "deployed" / "strat_alias"
    link.symlink_to(world["strat"])
    fds = _open_fds()
    allow = L.build_allowlist([world["strat"], link], repo_root=world["repo"], home=world["home"])
    try:
        real = world["strat"].resolve()
        assert list(allow.plugin_dir_fds) == [str(real)]
        assert sorted(str(r.path) for r in allow.rules if r.kind == "plugin_file") == [
            str(real / "config.yaml"), str(real / "plugin.py")]
    finally:
        allow.close_all()
    assert _open_fds() == fds


@needs_landlock
@pytest.mark.parametrize("assume", [3, 7])
def test_failure_right_before_applying_closes_every_fd(world, assume):
    if assume > HOST_ABI:
        pytest.skip("host の ABI を超える")
    script = """
        orig = L.build_allowlist
        keep = threading.Event()
        def building(*a, **k):
            allow = orig(*a, **k)
            threading.Thread(target=keep.wait, daemon=True).start()
            return allow
        L.build_allowlist = building
        before = len(os.listdir("/proc/self/fd"))
        try:
            apply()
            err = None
        except L.LandlockSetupError as e:
            err = e.reason
        emit({"err": err, "leak": len(os.listdir("/proc/self/fd")) - before})
    """
    out = run_ok(script, _cfg(world, ["strat", "ind"], assume_abi=assume), cwd=world["cwd"])
    assert out == {"err": "landlock_multithreaded", "leak": 0}


@needs_abi8
@pytest.mark.parametrize("what,reason", [
    ("add_rule", "landlock_add_rule_failed"),
    ("restrict", "landlock_restrict_failed"),
])
def test_kernel_failures_close_the_ruleset_fd(world, what, reason):
    script = """
        import dataclasses
        allow = L.build_allowlist([Path(d) for d in cfg["plugin_dirs"]],
                                  repo_root=Path(cfg["repo"]), home=Path(cfg["home"]))
        plan = L.plan_for_abi(8)
        if cfg["what"] == "add_rule":
            os.close(allow.rules[0].fd)
        else:
            plan = dataclasses.replace(plan, restrict_flags=1 << 20)
        before = len(os.listdir("/proc/self/fd"))
        try:
            L.restrict_with_allowlist(allow, plan)
            err = None
        except L.LandlockSetupError as e:
            err = e.reason
        emit({"err": err, "leak": len(os.listdir("/proc/self/fd")) - before})
    """
    out = run_ok(script, _cfg(world, ["strat"], what=what), cwd=world["cwd"])
    assert out == {"err": reason, "leak": 0}
