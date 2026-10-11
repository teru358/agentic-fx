"""改善 worker の outer Landlock における `/dev` の個別 file 規則と IOCTL_DEV。

`/dev` を directory 単位で許さず、`/dev/null` (読み書き) と乱数・zero (読み
だけ) の 4 file に限ること、device への ioctl を handled にしてどこにも与え
ないことを、実子 process に実 kernel の Landlock domain を掛けて観測する。
pytest 自身には Landlock を掛けない (不可逆で以後のテストが全滅する)。

観測は 2 種の子で行う:
- improve: `_bootstrap_improve_profile` の実配線 (local backend) で掛かった domain
- dir_rule: `/dev` を directory 単位の rw で許す旧来の形 (対照)
"""
from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agentic_fx.core import landlock
from tests.test_mission_worker import (
    _claude_scratch_env,
    _resolve_codex_native_bin,
    _run_version_under_closure,
    _skip_unless_cli_installed,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# 改善 worker の最低 ABI。定数の側が変わったときに気付けるよう literal で持つ。
_IMPROVE_ABI = 8

pytestmark = pytest.mark.skipif(
    landlock.landlock_abi() < _IMPROVE_ABI,
    reason=f"Landlock ABI {_IMPROVE_ABI} 未満 (改善 worker は起動拒否する環境)")

_R = 1 << 2      # READ_FILE
_W = 1 << 1      # WRITE_FILE
_T = 1 << 14     # TRUNCATE
_IOCTL_DEV = 1 << 15
_DIR_ONLY_BITS = (
    (1 << 3)            # READ_DIR
    | (1 << 4)          # REMOVE_DIR
    | (1 << 5)          # REMOVE_FILE
    | (1 << 6) | (1 << 7) | (1 << 8) | (1 << 9) | (1 << 10) | (1 << 11)
    | (1 << 12)         # MAKE_*
    | (1 << 13))        # REFER

_TCGETS = 0x5401
_TCGETS2 = 0x802C542A
_FIONCLEX = 0x5450
_FIOCLEX = 0x5451


# --- 子 process で観測する ------------------------------------------------

# 子の中で回す観測。結果は {名前: "ok" | errno 名}。
_PROBES = r'''
import errno, fcntl, json, os, subprocess, sys

def _probe(fn):
    try:
        fn()
        return "ok"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))

def _open_rw(path, flags, data=None, read=0):
    def run():
        fd = os.open(path, flags)
        try:
            if data is not None:
                os.write(fd, data)
            if read:
                os.read(fd, read)
        finally:
            os.close(fd)
    return run

def _ioctl(path, req):
    def run():
        fd = os.open(path, os.O_RDONLY)
        try:
            fcntl.ioctl(fd, req, bytes(64))
        finally:
            os.close(fd)
    return run

def _plain_ioctl(path, req):
    def run():
        fd = os.open(path, os.O_RDONLY)
        try:
            fcntl.ioctl(fd, req, 0)
        finally:
            os.close(fd)
    return run

def _reopen_pipe(prefix):
    def run():
        r, w = os.pipe()
        try:
            os.write(w, b"pipe-ok")
            fd = os.open(f"{prefix}/{r}", os.O_RDONLY)
            try:
                if os.read(fd, 7) != b"pipe-ok":
                    raise OSError(errno.EIO, "unexpected pipe data")
            finally:
                os.close(fd)
        finally:
            os.close(r)
            os.close(w)
    return run

def _shm_create():
    name = f"/dev/shm/afx-dev-rules-probe-{os.getpid()}"
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    # 拒否されるべき場面で作れてしまった場合も実資源に残さない
    os.unlink(name)

def _subprocess_devnull():
    rc = subprocess.run([sys.executable, "-c", "pass"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL).returncode
    if rc != 0:
        raise OSError(errno.EIO, f"child rc={rc}")

def run_probes(mode):
    res = {}
    res["null_rdonly"] = _probe(_open_rw("/dev/null", os.O_RDONLY, read=1))
    res["null_wronly"] = _probe(_open_rw("/dev/null", os.O_WRONLY, data=b"x"))
    res["null_rdwr_trunc"] = _probe(
        _open_rw("/dev/null", os.O_RDWR | os.O_TRUNC, data=b"x"))
    res["null_subprocess_devnull"] = _probe(_subprocess_devnull)
    for name in ("urandom", "random", "zero"):
        res[f"{name}_read"] = _probe(_open_rw(f"/dev/{name}", os.O_RDONLY, read=16))
        res[f"{name}_wronly"] = _probe(_open_rw(f"/dev/{name}", os.O_WRONLY))
    res["tty"] = _probe(_open_rw("/dev/tty", os.O_RDWR | os.O_NOCTTY))
    res["ptmx"] = _probe(_open_rw("/dev/ptmx", os.O_RDWR | os.O_NOCTTY))
    if os.path.exists("/dev/pts/0"):
        res["pts0"] = _probe(
            _open_rw("/dev/pts/0", os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK))
    res["full"] = _probe(_open_rw("/dev/full", os.O_RDONLY))
    res["list_dev"] = _probe(lambda: os.listdir("/dev"))
    res["list_shm"] = _probe(lambda: os.listdir("/dev/shm"))
    if os.path.isdir("/dev/mqueue"):
        res["list_mqueue"] = _probe(lambda: os.listdir("/dev/mqueue"))
    if mode == "improve":
        res["shm_create"] = _probe(_shm_create)
        if os.path.isdir("/dev/mqueue"):
            res["mqueue_create"] = _probe(
                _open_rw(f"/dev/mqueue/afx-dev-rules-{os.getpid()}",
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    res["ioctl_tcgets"] = _probe(_ioctl("/dev/null", 0x5401))
    res["ioctl_tcgets2"] = _probe(_ioctl("/dev/null", 0x802C542A))
    res["ioctl_tcgets_urandom"] = _probe(_ioctl("/dev/urandom", 0x5401))
    res["ioctl_fioclex"] = _probe(_plain_ioctl("/dev/null", 0x5451))
    res["ioctl_fionclex"] = _probe(_plain_ioctl("/dev/null", 0x5450))
    res["pipe_dev_fd"] = _probe(_reopen_pipe("/dev/fd"))
    res["pipe_proc_self_fd"] = _probe(_reopen_pipe("/proc/self/fd"))
    print("RESULT " + json.dumps(res))
'''


def _child_script(mode: str, layout: dict) -> str:
    prelude = textwrap.dedent(f"""
        import resource, sys
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        sys.path.insert(0, {str(_REPO_ROOT / "src")!r})
        from pathlib import Path
    """)
    if mode == "improve":
        restrict = textwrap.dedent(f"""
            from agentic_fx.mission_worker import _bootstrap_improve_profile
            _bootstrap_improve_profile(
                backend="local", mission_id={layout["mission_id"]!r},
                staging_dir={str(layout["staging_dir"])!r},
                source_snapshot_dir={str(layout["source_snapshot_dir"])!r},
                claude_bin=None, codex_bin=None)
        """)
    else:
        # 旧来の形: `/dev` を directory 単位の rw、IOCTL_DEV は handled 外
        restrict = textwrap.dedent(f"""
            import sysconfig
            from agentic_fx.core import landlock
            ro = [Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
                  Path(sysconfig.get_paths()["stdlib"]).resolve()]
            landlock.restrict_to(
                read_only_paths=ro,
                read_write_paths=[Path({str(layout["workdir"])!r}), Path("/dev")],
                execute_paths=ro)
        """)
    return prelude + _PROBES + restrict + f"\nrun_probes({mode!r})\n"


def _run_child(mode: str, layout: dict) -> dict:
    env = {"PATH": "/usr/bin:/bin",
           "AGENTIC_FX_MISSION_TRANSCRIPTS_DIR":
               str(layout["workdir"] / "mission-transcripts-isolated")}
    # 制御端末を持たない子にする (WorkerRunner と同じく新 session)
    result = subprocess.run(
        [sys.executable, "-P", "-B", "-c", _child_script(mode, layout)],
        cwd=str(layout["workdir"]), env=env, capture_output=True, text=True,
        timeout=60, start_new_session=True, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith("RESULT ")]
    assert len(lines) == 1, result.stdout + result.stderr
    return json.loads(lines[0][len("RESULT "):])


def _make_layout(base: Path) -> dict:
    workdir = base / "workdir"; workdir.mkdir()
    mission_id = "m-dev"
    staging_dir = base / "repo" / "plugins" / "_staging" / mission_id
    staging_dir.mkdir(parents=True, mode=0o700)
    source_snapshot_dir = workdir / "source"
    source_snapshot_dir.mkdir(mode=0o500)
    return {"workdir": workdir, "mission_id": mission_id,
            "staging_dir": staging_dir, "source_snapshot_dir": source_snapshot_dir}


@pytest.fixture(scope="module")
def observed(tmp_path_factory) -> dict:
    """improve domain と対照 domain で同じ観測を 1 回ずつ回した結果。"""
    improve = _run_child("improve", _make_layout(tmp_path_factory.mktemp("improve")))
    dir_rule = _run_child("dir_rule", _make_layout(tmp_path_factory.mktemp("dirrule")))
    return {"improve": improve, "dir_rule": dir_rule}


def test_dev_null_is_readable_writable_and_truncatable(observed):
    got = observed["improve"]
    assert got["null_rdonly"] == "ok"
    assert got["null_wronly"] == "ok"
    # char device では kernel が O_TRUNC を落とすため、この open は TRUNCATE
    # bit の有無を見分けない。bit そのものは表の pin で見る。
    assert got["null_rdwr_trunc"] == "ok"
    assert got["null_subprocess_devnull"] == "ok"


def test_random_devices_are_read_only(observed):
    got = observed["improve"]
    for name in ("urandom", "random", "zero"):
        assert got[f"{name}_read"] == "ok", name
        assert got[f"{name}_wronly"] == "EACCES", name
    # 対照: directory 規則では書込みでも開ける
    for name in ("urandom", "random", "zero"):
        assert observed["dir_rule"][f"{name}_wronly"] == "ok", name


def test_tty_pty_and_shm_are_denied(observed):
    got, old = observed["improve"], observed["dir_rule"]
    for key in ("tty", "ptmx", "full", "list_shm", "shm_create"):
        assert got[key] == "EACCES", (key, got[key])
    if "pts0" in got:
        assert got["pts0"] == "EACCES", got["pts0"]
    if "list_mqueue" in got:
        assert got["list_mqueue"] == "EACCES"
        assert got["mqueue_create"] == "EACCES"
    # 対照: 制御端末の無い子では tty は ENXIO、ptmx・full・shm は通っていた
    assert old["tty"] == "ENXIO"
    for key in ("ptmx", "full", "list_shm"):
        assert old[key] == "ok", (key, old[key])


def test_device_ioctl_is_eacces_on_allowed_device(observed):
    got, old = observed["improve"], observed["dir_rule"]
    assert got["ioctl_tcgets"] == "EACCES"
    assert got["ioctl_tcgets2"] == "EACCES"
    assert got["ioctl_tcgets_urandom"] == "EACCES"
    # Landlock が常に許す汎用 ioctl
    assert got["ioctl_fioclex"] == "ok"
    assert got["ioctl_fionclex"] == "ok"
    # 対照: IOCTL_DEV が handled 外なら driver まで届き端末でないので ENOTTY
    assert old["ioctl_tcgets"] == "ENOTTY"
    assert old["ioctl_tcgets2"] == "ENOTTY"


def test_dev_directory_is_not_listable(observed):
    assert observed["improve"]["list_dev"] == "EACCES"
    assert observed["dir_rule"]["list_dev"] == "ok"


def test_dev_fd_of_pipe_reopens_without_rule(observed):
    got = observed["improve"]
    assert got["pipe_dev_fd"] == "ok"
    assert got["pipe_proc_self_fd"] == "ok"


# --- device 規則の組み立て --------------------------------------------------

def _mk_regular(tmp_path: Path) -> Path:
    p = tmp_path / "regular"; p.write_text("x"); return p


def _mk_directory(tmp_path: Path) -> Path:
    p = tmp_path / "directory"; p.mkdir(); return p


def _mk_symlink_to_null(tmp_path: Path) -> Path:
    p = tmp_path / "null-link"; p.symlink_to("/dev/null"); return p


def _mk_missing(tmp_path: Path) -> Path:
    return tmp_path / "missing"


@pytest.mark.parametrize("make", [_mk_regular, _mk_directory,
                                  _mk_symlink_to_null, _mk_missing])
def test_device_rule_requires_character_device(tmp_path, make):
    path = make(tmp_path)
    with pytest.raises(landlock.LandlockUnavailable):
        landlock._open_character_device(path)


def test_device_rule_rejects_non_device_without_leaking_fd(tmp_path):
    """character device でない path を拒否するとき、開いた fd を閉じる。"""
    path = _mk_regular(tmp_path)
    before = set(os.listdir("/proc/self/fd"))
    with pytest.raises(landlock.LandlockUnavailable):
        landlock._open_character_device(path)
    assert set(os.listdir("/proc/self/fd")) == before


def test_device_rule_fstat_failure_is_normalized_without_leaking_fd(
        monkeypatch):
    """`fstat` 失敗でも、開いた fd を閉じて利用不能エラーへ正規化する。"""
    def fail_fstat(_fd):
        raise OSError(errno.EIO, "injected fstat failure")

    monkeypatch.setattr(landlock.os, "fstat", fail_fstat)
    before = set(os.listdir("/proc/self/fd"))
    with pytest.raises(landlock.LandlockUnavailable,
                       match=r"device file rule: cannot stat /dev/null:"):
        landlock._open_character_device(Path("/dev/null"))
    assert set(os.listdir("/proc/self/fd")) == before


def test_device_rule_fstat_failure_stops_restrict_to_without_leaking_fd(
        monkeypatch, tmp_path):
    """device 規則の準備中の `fstat` 失敗も統一例外で fail closed する。"""
    def fail_fstat(_fd):
        raise OSError(errno.EIO, "injected fstat failure")

    monkeypatch.setattr(landlock, "landlock_abi", lambda: _IMPROVE_ABI)
    monkeypatch.setattr(landlock.os, "fstat", fail_fstat)
    before = set(os.listdir("/proc/self/fd"))
    with pytest.raises(landlock.LandlockUnavailable,
                       match=r"device file rule: cannot stat /dev/null:"):
        landlock.restrict_to(read_only_paths=[], read_write_paths=[tmp_path],
                             device_rules=True)
    assert set(os.listdir("/proc/self/fd")) == before


def test_device_rule_on_fifo_fails_without_opening_it(tmp_path):
    """検査のための open は file を実際には開かない (O_PATH)。FIFO なら
    読み手として開くと書き手が来るまで止まるが、ここではすぐ拒否される。
    止まった場合に pytest ごと固まらないよう子 process で見る。"""
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(_REPO_ROOT / "src")!r})
        from pathlib import Path
        from agentic_fx.core import landlock
        try:
            landlock._open_character_device(Path({str(fifo)!r}))
        except landlock.LandlockUnavailable:
            print("REJECTED")
    """)
    result = subprocess.run([sys.executable, "-P", "-B", "-c", code],
                            capture_output=True, text=True, timeout=30)
    assert result.stdout.strip() == "REJECTED", result.stdout + result.stderr


def _device_table_guard():
    """import 時に表の bit を検査する for 文を、landlock の source から
    取り出して compile したもの。"""
    import ast
    src_path = Path(landlock.__file__)
    tree = ast.parse(src_path.read_text())
    loops = [node for node in tree.body
             if isinstance(node, ast.For) and isinstance(node.iter, ast.Name)
             and node.iter.id == "_DEVICE_FILE_RULES"]
    assert len(loops) == 1
    return compile(ast.Module(body=loops, type_ignores=[]), str(src_path), "exec")


@pytest.mark.parametrize("bit", [
    1 << 3, 1 << 4, 1 << 5, 1 << 6, 1 << 7, 1 << 8, 1 << 9, 1 << 10,
    1 << 11, 1 << 12, 1 << 13, _IOCTL_DEV, 1 << 0])
def test_device_table_guard_rejects_non_file_bits(bit):
    """表に directory 専用 bit・IOCTL_DEV・EXECUTE が混ざれば import 時に
    落ちる。許してよい bit は READ_FILE・WRITE_FILE・TRUNCATE だけ。"""
    assert landlock._DEVICE_FILE_ALLOWED_BITS == _R | _W | _T
    guard = _device_table_guard()
    bad = {"_DEVICE_FILE_ALLOWED_BITS": landlock._DEVICE_FILE_ALLOWED_BITS,
           "_DEVICE_FILE_RULES": ((Path("/dev/null"), _R | bit),)}
    with pytest.raises(AssertionError):
        exec(guard, bad)
    ok = {"_DEVICE_FILE_ALLOWED_BITS": landlock._DEVICE_FILE_ALLOWED_BITS,
          "_DEVICE_FILE_RULES": ((Path("/dev/null"), _R | _W | _T),)}
    exec(guard, ok)


@pytest.mark.parametrize("path", ["/dev/null", "/dev/urandom",
                                  "/dev/random", "/dev/zero"])
def test_device_rule_accepts_real_character_devices(path):
    fd = landlock._open_character_device(Path(path))
    try:
        assert fd >= 0
    finally:
        os.close(fd)


def test_device_rule_never_carries_directory_bits():
    table = {str(p): access for p, access in landlock._DEVICE_FILE_RULES}
    assert table == {
        "/dev/null": _R | _W | _T,
        "/dev/urandom": _R,
        "/dev/random": _R,
        "/dev/zero": _R,
    }
    for path, access in table.items():
        assert access & _DIR_ONLY_BITS == 0, path
        assert access & _IOCTL_DEV == 0, path
    # 改善 worker の handled は plugin 用の 0x5FFF に IOCTL_DEV を足したもの
    assert landlock._IMPROVE_HANDLED_ACCESS_FS == 0x5FFF | _IOCTL_DEV
    assert landlock.IMPROVE_MIN_ABI == _IMPROVE_ABI


def test_plugin_handled_mask_is_unchanged():
    assert landlock._HANDLED_ACCESS_FS == 0x5FFF
    assert landlock.PLUGIN_MIN_ABI == 3
    for abi in range(3, 9):
        assert landlock.plan_for_abi(abi).handled_fs == 0x5FFF, abi


# --- 実 CLI の互換回帰 -------------------------------------------------------

def _cli_case(backend: str, tmp_path: Path) -> dict:
    """本番の closure と rlimit に近い形で `--version` を回す設定。"""
    if backend == "claude":
        bin_path = _skip_unless_cli_installed("claude")
        env, scratch = _claude_scratch_env(tmp_path)
        return dict(bin_path=bin_path, env=env, as_mb=4096,
                    execute_paths=[Path(bin_path).resolve().parent,
                                   Path("/usr/lib"), Path("/usr/lib64")],
                    read_only_paths=[Path("/etc"), Path("/proc")],
                    read_write_paths=[scratch])
    if backend == "codex":
        bin_path = _resolve_codex_native_bin()
        scratch = tmp_path / "codex_home"; scratch.mkdir()
        return dict(bin_path=bin_path, as_mb=262144,
                    env={"HOME": str(scratch), "CODEX_HOME": str(scratch)},
                    execute_paths=[Path(bin_path).resolve().parent],
                    read_only_paths=[Path("/etc")],
                    read_write_paths=[scratch])
    bin_path = _skip_unless_cli_installed_at("opencode",
                                             Path.home() / ".opencode/bin/opencode")
    scratch = tmp_path / "opencode_home"; scratch.mkdir()
    return dict(bin_path=bin_path, as_mb=262144,
                env={"HOME": str(scratch),
                     "XDG_CONFIG_HOME": str(scratch / "config"),
                     "XDG_DATA_HOME": str(scratch / "data"),
                     "XDG_CACHE_HOME": str(scratch / "cache"),
                     "XDG_STATE_HOME": str(scratch / "state")},
                execute_paths=[Path(bin_path).resolve().parent,
                               Path("/usr/lib"), Path("/usr/lib64")],
                read_only_paths=[Path("/etc"), Path("/proc")],
                read_write_paths=[scratch])


def _skip_unless_cli_installed_at(name: str, default: Path) -> str:
    import shutil
    found = shutil.which(name)
    if found:
        return found
    if default.exists():
        return str(default)
    pytest.skip(f"{name} not installed on this host")


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
def test_cli_version_runs_under_individual_dev_rules(tmp_path, backend):
    """`--version` の起動確認。

    prompt の往復・MCP initialize・非空 tools/list まで含む仕事の確認は
    `tests/test_improve_isolated_mission.py::test_real_cli_selftest`
    (`realbackend`) が同じ device 規則で行う (tools/call は呼ばせない)。stdin を Landlock 適用後に
    開いた `/dev/null` にして回し、CLI の端末判定が `/dev/null` への TCGETS
    で EACCES を受けても落ちないこと、Bun が `/dev/urandom` を開けることを
    見る。"""
    case = _cli_case(backend, tmp_path)
    # 本番の runner と同じく TMPDIR を rw の scratch 配下に向ける
    tmpdir = Path(case["read_write_paths"][0]) / "tmp"
    tmpdir.mkdir()
    case["env"] = {**case["env"], "TMPDIR": str(tmpdir), "TERM": "dumb"}
    result = _run_version_under_closure(
        case["bin_path"], execute_paths=case["execute_paths"],
        read_only_paths=case["read_only_paths"],
        read_write_paths=case["read_write_paths"], env=case["env"],
        device_rules=True, stdin=subprocess.DEVNULL, as_mb=case["as_mb"],
        timeout=60)
    assert "EXEC_PERMISSION_ERROR" not in result.stdout
    # CLI に渡す stdin の fd で IOCTL_DEV の拒否が実際に起きている前提の確認
    assert "STDIN_TCGETS=EACCES" in result.stdout, result.stdout
    assert result.returncode == 0, result.stdout + result.stderr
    version = result.stdout.replace("STDIN_TCGETS=EACCES", "").strip()
    assert version, result.stderr
