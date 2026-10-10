"""Landlock 下の server 認証の前提: UDS connect と stat は通り、/proc は自 pid と locks だけ。"""
from __future__ import annotations

import ast
import importlib.util
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import afx_ops
from _afx_isolation_support import FEATURES, make_root, needs_landlock, needs_x86_64, run_canary
from afx_ops.isolation.startup import PLANS

pytestmark = [needs_x86_64, needs_landlock]

_TESTS = Path(__file__).resolve().parents[1]


def _load_harness():
    spec = importlib.util.spec_from_file_location("afx_isolation_oracle_harness",
                                                  _TESTS / "_oracle_harness.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass が自 module を引くため
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def root(tmp_path) -> Path:
    return make_root(tmp_path)


@pytest.fixture
def fake_daemon(tmp_path):
    """pathname UDS で受けて 1 byte 返す daemon (thread)。"""
    run = tmp_path / "data" / "run"
    run.mkdir(parents=True)
    run.chmod(0o700)
    path = run / "api.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(4)
    listener.settimeout(0.2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except (TimeoutError, OSError):
                continue
            with conn:
                conn.settimeout(5)
                conn.sendall(conn.recv(16)[:2])
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield path
    stop.set()
    thread.join(5)
    listener.close()


@pytest.fixture
def sibling():
    proc = subprocess.Popen(["sleep", "120"])
    yield proc.pid
    os.kill(proc.pid, 9)
    proc.wait(10)


def _spec(root, feature, op, **params):
    return {"feature": feature, "root": str(root), "op": op, "params": params}


@pytest.mark.parametrize("feature", FEATURES)
def test_landlock_allows_uds_connect_and_stat_but_not_proc(feature, root, fake_daemon,
                                                           sibling, child_env):
    other = next(f for f in FEATURES if f != feature)
    cases = {
        # server 認証が隔離後に使うもの
        "uds_connect": ("unix_connect", {"path": str(fake_daemon)}, lambda r: r == [2, 0]),
        "socket_lstat": ("lstat", {"path": str(fake_daemon)}, lambda r: r == [0, 0]),
        "socket_stat": ("stat", {"path": str(fake_daemon)}, lambda r: r == [0, 0]),
        "dir_lstat": ("lstat", {"path": str(fake_daemon.parent)}, lambda r: r == [0, 0]),
        "dir_stat": ("stat", {"path": str(fake_daemon.parent)}, lambda r: r == [0, 0]),
        "proc_locks": ("read_file", {"path": "/proc/locks"}, lambda r: r[1] == 0),
        "own_pid_fdinfo": ("proc_self_fdinfo", {}, lambda r: r[0] > 0 and r[1] == 0),
        # 自分の設定 (読取り) と状態 (読み書き)
        "own_config": ("read_file", {"path": str(root / feature / "config" / "settings.txt")},
                       lambda r: r[0] > 0 and r[1] == 0),
        "own_state_write": ("write_state", {"dir": str(root / feature / "state")},
                            lambda r: r == [3, 0]),
        # 許可外
        "other_pid_status": ("read_file", {"path": f"/proc/{sibling}/status"},
                             lambda r: r == [-1, 13]),
        "peer_pid_fd": ("open", {"path": f"/proc/{sibling}/fd"}, lambda r: r == [-1, 13]),
        "proc_meminfo": ("read_file", {"path": "/proc/meminfo"}, lambda r: r == [-1, 13]),
        "proc_pid1": ("read_file", {"path": "/proc/1/status"}, lambda r: r == [-1, 13]),
        "proc_sys": ("read_file", {"path": "/proc/sys/kernel/pid_max"},
                     lambda r: r == [-1, 13]),
        "other_config": ("read_file",
                         {"path": str(root / other / "config" / "settings.txt")},
                         lambda r: r == [-1, 13]),
        "other_state": ("write_state", {"dir": str(root / other / "state")},
                        lambda r: r == [-1, 13]),
        "key_dir": ("open", {"path": str(root / "keys")}, lambda r: r == [-1, 13]),
    }
    seen = {}
    for name, (op, params, _ok) in cases.items():
        got = run_canary(_spec(root, feature, op, **params), child_env)
        assert got.rc == 0, (name, got.stderr[-2000:])
        seen[name] = got.result
    bad = {name: seen[name] for name, (_o, _p, ok) in cases.items() if not ok(seen[name])}
    assert bad == {}


@pytest.mark.parametrize("feature", FEATURES)
def test_xdg_layout_reads_config_and_writes_state_but_not_secrets(feature, root, tmp_path,
                                                                  child_env):
    env = {**child_env, "XDG_CONFIG_HOME": str(tmp_path / "xc"),
           "XDG_STATE_HOME": str(tmp_path / "xs")}
    base = tmp_path / "xc" / "agentic-fx" / "ops"
    for f in FEATURES:
        (base / f / "config").mkdir(parents=True)
        (base / f / "config").chmod(0o755)
        (base / f / "config" / "settings.txt").write_text(f"{f}\n")
        (base / f / "secret").mkdir(mode=0o700)
        # 機能の固定名の secret (起動時に読む) と、隔離後に読めないことを見る file
        for name in (*PLANS[f].secret_files, "token"):
            (base / f / "secret" / name).write_text(f"{name}-of-{f}\n")
            (base / f / "secret" / name).chmod(0o600)
        (tmp_path / "xs" / "agentic-fx" / "ops" / f).mkdir(parents=True)
        (tmp_path / "xs" / "agentic-fx" / "ops" / f).chmod(0o700)
    other = next(f for f in FEATURES if f != feature)
    cases = {
        "own_config": ({"op": "read_file", "params": {"path": str(base / feature / "config" /
                                                                "settings.txt")}},
                       lambda r: r[0] > 0 and r[1] == 0),
        "own_secret": ({"op": "read_file", "params": {"path": str(base / feature / "secret" /
                                                                "token")}},
                       lambda r: r == [-1, 13]),
        "own_state": ({"op": "write_state",
                       "params": {"dir": str(tmp_path / "xs" / "agentic-fx" / "ops" / feature)}},
                      lambda r: r == [3, 0]),
        "other_config": ({"op": "read_file", "params": {"path": str(base / other / "config" /
                                                                  "settings.txt")}},
                         lambda r: r == [-1, 13]),
    }
    seen = {}
    for name, (spec, _ok) in cases.items():
        got = run_canary({"feature": feature, "root": str(root), "dirs": "xdg", **spec}, env)
        assert got.rc == 0, (name, got.stderr[-2000:])
        seen[name] = got.result
    assert {n: s for n, s in seen.items() if not cases[n][1](s)} == {}


def test_proc_self_resolves_to_the_own_pid_dir(root, child_env):
    """`/proc/self` は自 pid の dir と同じ inode に解決されるので、Landlock では区別できない。

    自 pid の dir の規則で読める (status) が、dumpable 0 で root 所有になった environ は
    DAC で拒否される。code が `/proc/self` に依存しないことは下の走査で確かめる。
    """
    status = run_canary(_spec(root, "bot", "read_file", path="/proc/self/status"), child_env)
    environ = run_canary(_spec(root, "bot", "read_file", path="/proc/self/environ"), child_env)
    assert status.result[1] == 0 and status.result[0] > 0
    assert environ.result == [-1, 13]


def _code_strings(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]


def test_isolation_code_does_not_use_proc_self():
    package = Path(afx_ops.__file__).resolve().parent / "isolation"
    found = [(p.name, line, s) for p in sorted(package.glob("*.py"))
             for line, s in _code_strings(p) if "/proc/self" in s or "/proc/thread-self" in s]
    assert found == []


def test_the_proc_self_scan_detects_a_use(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text('"""/proc/self in a docstring is fine."""\nPATH = "/proc/self/fdinfo"\n')
    assert [s for _l, s in _code_strings(sample)] == ["/proc/self/fdinfo"]


def test_t4_client_authenticates_under_isolation(root, tmp_path, child_env):
    harness = _load_harness()
    data = tmp_path / "srv" / "data"
    run = data / "run"
    run.mkdir(parents=True)
    run.chmod(0o700)
    record = tmp_path / "record"
    proc, pid, _listen = harness.start_daemon(run / "api.sock", record, lock_dir=data,
                                              orphan=False)
    try:
        good = run_canary(_spec(root, "bot", "t4_connect", socket=str(run / "api.sock"),
                                lock=str(data / "instance.lock"), uid=os.getuid()), child_env)
    finally:
        harness.stop_daemon(proc, pid)
    assert good.rc == 0, good.stderr[-2000:]
    # 偽 daemon (instance lock を持たない) には 1 byte も送らない
    fake_data = tmp_path / "fake" / "data"
    fake_run = fake_data / "run"
    fake_run.mkdir(parents=True)
    fake_run.chmod(0o700)
    (fake_data / "instance.lock").write_text("")
    fake_record = tmp_path / "fake-record"
    proc, pid, _listen = harness.start_daemon(fake_run / "api.sock", fake_record,
                                              lock_dir=None, orphan=False)
    try:
        fake = run_canary(_spec(root, "bot", "t4_connect", socket=str(fake_run / "api.sock"),
                                lock=str(fake_data / "instance.lock"), uid=os.getuid()),
                          child_env)
    finally:
        harness.stop_daemon(proc, pid)
    assert fake.rc == 0, fake.stderr[-2000:]
    assert fake.result[0] == -3
    assert b"".join(harness.received(fake_record, expect_any=False)) == b""
    # 本物 daemon には要求が届き、応答を得た
    assert good.result[0] > 0, good.result
    assert any(harness.received(record, expect_any=True))
