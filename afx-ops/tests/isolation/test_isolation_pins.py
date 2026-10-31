"""隔離の各段で、他の層や後段の検査に隠れて単体では観測されにくい判定を固定する。"""
from __future__ import annotations

import ast
import errno
import functools
import inspect
import operator
import os
import socket
import time
from pathlib import Path

import pytest

from _afx_isolation_support import make_root, needs_landlock, needs_x86_64, run_canary
from _test_plans import test_plan
from afx_ops.isolation import bpf, landlock, startup
from afx_ops.isolation import profiles as P
from afx_ops.isolation.accept_loop import AcceptLoop, ListenerError, check_listener, prebind_listener
from afx_ops.isolation.policy import AUDIT_ARCH_X86_64, PolicyBuildError, evaluate, ret_errno
from afx_ops.isolation.startup import StartupError, StartupPlan
from afx_ops.isolation.syscalls_x86_64 import SNAPSHOT_NUMBERS as NR


# --- seccomp 表と hash pin ------------------------------------------------------

def test_feature_filter_refuses_a_table_whose_hash_differs_from_the_pin(monkeypatch):
    P.feature_filter.cache_clear()
    monkeypatch.setitem(P.PINNED_HASHES, "bot", ("0" * 64, "0" * 64))
    try:
        with pytest.raises(PolicyBuildError) as info:
            P.feature_filter("bot")
        assert info.value.reason == "hash_mismatch"
    finally:
        P.feature_filter.cache_clear()


def test_verify_hashes_needs_both_the_table_and_the_bpf_hash_to_match():
    compiled = P.feature_filter("bot").compiled
    for policy_hash, bpf_hash in ((compiled.policy_hash, "0" * 64),
                                  ("0" * 64, compiled.bpf_hash)):
        with pytest.raises(PolicyBuildError) as info:
            bpf.verify_hashes(compiled, policy_hash=policy_hash, bpf_hash=bpf_hash)
        assert info.value.reason == "hash_mismatch"


def test_web_recvfrom_and_sendto_refuse_an_address_even_without_its_length():
    table = P.feature_filter("web").table

    def ev(name, *args):
        return evaluate(table, AUDIT_ARCH_X86_64, NR[name], args)
    # 送信元 / 宛先 address だけが非 NULL で長さ (arg5) が 0 でも、接続済みとは見なさない
    assert ev("recvfrom", 3, 0, 1, 0, 0x1000, 0) == ret_errno(errno.EPERM)
    assert ev("sendto", 3, 0, 1, 0, 0x1000, 0) == ret_errno(errno.EPERM)


# --- 起動順 --------------------------------------------------------------------

def _first_call_lines(func) -> dict[str, int]:
    tree = ast.parse(inspect.getsource(func))
    seen: dict[str, int] = {}
    for node in sorted((n for n in ast.walk(tree) if isinstance(n, ast.Call)),
                       key=lambda n: (n.lineno, n.col_offset)):
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        if name is not None:
            seen.setdefault(name, node.lineno)
    return seen


def test_isolate_runs_the_stages_in_the_fixed_order():
    lines = _first_call_lines(startup.isolate)
    order = ["plan_check", "check_preconditions", "connect_notify", "feature_filter",
             "prepare_dirs", "read_api_token", "read_secrets", "prebind_listener",
             "pin_proc", "close_unlisted_fds", "apply_landlock", "apply_seccomp", "set_dumpable_zero",
             "self_check"]
    assert [lines[name] for name in order] == sorted(lines[name] for name in order)
    # READY=1 は isolate では送らない (server 認証の後に run が送る)
    assert "send_ready" not in lines and "announce_ready" not in lines


def test_run_announces_ready_between_isolation_and_body():
    lines = _first_call_lines(startup.run)
    order = ["isolate", "announce_ready", "body"]
    assert [lines[name] for name in order] == sorted(lines[name] for name in order)
    assert len({lines[name] for name in order}) == 3


# --- 起動前 / 自己検査の判定 (偽の /proc/<pid> で各判定だけを見る) -------------------

def _fake_proc(tmp_path, *, status: str, tasks: int = 1, cgroup: str = "0::/\n"):
    proc = tmp_path / "proc"
    (proc / "task").mkdir(parents=True)
    for i in range(tasks):
        (proc / "task" / str(100 + i)).mkdir()
    (proc / "fd").mkdir()
    for fd in (0, 1, 2):
        (proc / "fd" / str(fd)).write_text("")
    (proc / "status").write_text(status)
    (proc / "cgroup").write_text(cgroup)
    return str(proc)


def test_preconditions_refuse_a_second_thread(tmp_path):
    proc = _fake_proc(tmp_path, status="NoNewPrivs:\t1\n", tasks=2)
    plan = test_plan("bot")
    with pytest.raises(StartupError) as info:
        startup.check_preconditions(plan, proc)
    assert info.value.reason == "multithreaded"


def test_preconditions_refuse_a_parent_other_than_systemd(tmp_path, monkeypatch):
    proc = _fake_proc(tmp_path, status="NoNewPrivs:\t1\n",
                      cgroup="0::/user.slice/app.slice/afx-ops@bot.service\n")
    plan = test_plan("bot", check_unit=True)
    # 親を自分自身にする (comm は python 系で systemd ではない)
    monkeypatch.setattr(startup.os, "getppid", os.getpid)
    with pytest.raises(StartupError) as info:
        startup.check_preconditions(plan, proc)
    assert info.value.reason == "unit_parent"


def test_self_check_needs_exactly_one_more_seccomp_filter(tmp_path):
    unchanged = _fake_proc(tmp_path / "a", status="Seccomp:\t2\nSeccomp_filters:\t1\nNoNewPrivs:\t1\n")
    with pytest.raises(StartupError) as info:
        startup.self_check(unchanged, filters_before=1, keep={0, 1, 2})
    assert info.value.reason == "self_check_seccomp"
    # 1 本増えていれば seccomp の判定は通り、次の判定 (この test process は dumpable) で止まる
    added = _fake_proc(tmp_path / "b", status="Seccomp:\t2\nSeccomp_filters:\t2\nNoNewPrivs:\t1\n")
    with pytest.raises(StartupError) as info:
        startup.self_check(added, filters_before=1, keep={0, 1, 2})
    assert info.value.reason == "self_check_dumpable"


# --- Landlock の規則 -------------------------------------------------------------

def test_landlock_handles_every_fs_access_it_defines():
    defined = functools.reduce(operator.or_, (v for k, v in vars(landlock).items()
                                              if k.startswith("ACCESS_FS_")))
    assert landlock.HANDLED_FS == defined
    assert landlock.HANDLED_FS & landlock.ACCESS_FS_IOCTL_DEV


def test_web_gets_no_tcp_connect_port_from_landlock(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xc"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xs"))
    web = StartupPlan.for_feature("web", web_port=0)
    bot = StartupPlan.for_feature("bot")
    dirs = startup.PreparedDirs(config_fd=-1, state_fd=-1)
    assert startup.landlock_plan(web, "/proc/1", dirs).connect_ports == ()
    assert startup.landlock_plan(bot, "/proc/1", dirs).connect_ports == (443,)


@needs_x86_64
@needs_landlock
def test_tcp_bind_is_refused_by_landlock_alone(tmp_path, child_env):
    root = make_root(tmp_path)
    spec = {"feature": "bot", "root": str(root), "op": "bind",
            "params": {"host": "127.0.0.1"}, "fault": "skip_seccomp", "fault_self_check": "off"}
    got = run_canary(spec, child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == [-1, errno.EACCES]
    got = run_canary({**spec, "fault": ["skip_seccomp", "skip_landlock"]}, child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == [0, 0]


@needs_x86_64
@needs_landlock
def test_config_dir_is_read_only_after_isolation(tmp_path, child_env):
    root = make_root(tmp_path)
    base = {"feature": "bot", "root": str(root), "op": "write_state"}
    got = run_canary({**base, "params": {"dir": str(root / "bot" / "config")}}, child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == [-1, errno.EACCES]
    got = run_canary({**base, "params": {"dir": str(root / "bot" / "state")}}, child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == [3, 0]


# --- web の listener と accept loop ----------------------------------------------

def test_check_listener_refuses_a_non_inet_socket_by_family(tmp_path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
    try:
        sock.bind(str(tmp_path / "l.sock"))
        sock.listen(1)
        with pytest.raises(ListenerError) as info:
            check_listener(sock, 0)
        assert str(info.value) == "listener_invalid: family"
    finally:
        sock.close()


def test_one_accept_ready_takes_every_queued_connection():
    listener = prebind_listener(0)
    port = listener.getsockname()[1]
    clients = [socket.create_connection(("127.0.0.1", port)) for _ in range(3)]
    got = []
    try:
        time.sleep(0.05)
        loop = AcceptLoop(listener, lambda c, peer: got.append(c))
        assert loop.accept_ready() == 3
        assert len(got) == 3
    finally:
        for c in clients + got:
            c.close()
        listener.close()


class _ScriptedListener:
    def __init__(self, script):
        self._script = list(script)

    def setblocking(self, flag):
        pass

    def fileno(self):
        return -1

    def accept(self):
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def test_an_aborted_connection_does_not_stop_the_accept_loop():
    class _Conn:
        def getpeername(self):
            return ("127.0.0.1", 1)

    conn = _Conn()
    listener = _ScriptedListener([ConnectionAbortedError(), (conn, ("127.0.0.1", 1)),
                                  BlockingIOError()])
    got = []
    assert AcceptLoop(listener, lambda c, peer: got.append(c)).accept_ready() == 1
    assert got == [conn]


# --- libc の ctypes 初期化は 1 箇所 ---------------------------------------------------

_LOADERS = frozenset({"CDLL", "PyDLL", "LoadLibrary", "cdll", "pydll"})


def _libc_loads(tree: ast.AST) -> list[str]:
    """libc (共有 library) の読み込みと restype の設定の箇所。

    `ctypes.CDLL(...)` だけでなく、`from ctypes import CDLL` の名前 import・別名、
    `import ctypes as c` の別名経由、`PyDLL`・`cdll.LoadLibrary`、呼ばずに変数へ移す参照
    (`x = ctypes.CDLL`) も数える。型注釈の中の `ctypes.CDLL` だけは数えない。
    """
    annotations: set[int] = set()
    for node in ast.walk(tree):
        parts = []
        if isinstance(node, ast.arg) and node.annotation is not None:
            parts.append(node.annotation)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns:
            parts.append(node.returns)
        if isinstance(node, ast.AnnAssign):
            parts.append(node.annotation)
        for part in parts:
            annotations |= {id(n) for n in ast.walk(part)}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "ctypes":
            found += [f"import {a.name}" for a in node.names if a.name in _LOADERS]
        if id(node) in annotations:
            continue
        if isinstance(node, ast.Attribute) and node.attr in _LOADERS:
            found.append(node.attr)
        elif isinstance(node, ast.Name) and node.id in _LOADERS:
            found.append(node.id)
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Attribute) and t.attr == "restype" for t in node.targets):
            found.append("restype")
    return found


def test_libc_is_loaded_only_in_one_module():
    import afx_ops
    from pathlib import Path
    package = Path(afx_ops.__file__).resolve().parent
    found = set()
    for path in sorted(package.rglob("*.py")):
        if _libc_loads(ast.parse(path.read_text(encoding="utf-8"))):
            found.add(path.relative_to(package).as_posix())
    assert found == {"isolation/_libc.py"}


@pytest.mark.parametrize("source", [
    "import ctypes\nctypes.CDLL(None)\n",
    "import ctypes\nctypes.PyDLL(None)\n",
    "import ctypes as c\nc.CDLL(None)\n",
    "from ctypes import CDLL\nCDLL(None)\n",
    "from ctypes import CDLL as Loader\nLoader(None)\n",
    "from ctypes import PyDLL\n",
    "from ctypes import cdll\ncdll.LoadLibrary('libc.so.6')\n",
    "import ctypes\nctypes.cdll.LoadLibrary('libc.so.6')\n",
    "h = object()\nh.syscall.restype = int\n",
    "import ctypes\nloader = ctypes.CDLL\nloader(None)\n",
])
def test_the_libc_scan_detects_every_loader_form(source, tmp_path):
    # 一時 module として書き出したものを、package の走査と同じ関数で検査する
    module = tmp_path / "sample.py"
    module.write_text(source, encoding="utf-8")
    assert _libc_loads(ast.parse(module.read_text(encoding="utf-8"))) != []


def test_the_libc_scan_ignores_a_type_annotation(tmp_path):
    module = tmp_path / "sample.py"
    module.write_text("import ctypes\ndef f(h: ctypes.CDLL) -> ctypes.CDLL:\n    return h\n"
                      "x: ctypes.CDLL | None = None\n", encoding="utf-8")
    assert _libc_loads(ast.parse(module.read_text(encoding="utf-8"))) == []


def test_shared_libc_returns_long_from_syscall():
    import ctypes
    from afx_ops.isolation import _libc
    libc = _libc.libc()
    assert libc.syscall.restype is ctypes.c_long
    assert _libc.libc() is libc


# --- procfs の pin ---------------------------------------------------------------

def test_pin_proc_opens_the_proc_dir_and_locks_as_o_path_fds():
    pins = startup.pin_proc(f"/proc/{os.getpid()}")
    try:
        assert pins.fds() == {pins.proc_fd, pins.locks_fd} and len(pins.fds()) == 2
        assert os.readlink(f"/proc/self/fd/{pins.proc_fd}") == f"/proc/{os.getpid()}"
        assert os.readlink(f"/proc/self/fd/{pins.locks_fd}") == "/proc/locks"
        for fd in pins.fds():
            with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as f:
                flags = int(next(l for l in f if l.startswith("flags:")).split()[1], 8)
            assert flags & os.O_PATH and flags & os.O_CLOEXEC
    finally:
        for fd in pins.fds():
            os.close(fd)


def test_pin_proc_failure_closes_what_it_opened_and_is_a_fixed_reason():
    before = startup.open_fds(f"/proc/{os.getpid()}")
    with pytest.raises(StartupError) as info:
        startup.pin_proc("/proc/0/none")
    assert info.value.reason == "proc_pin_failed"
    assert info.value.shape == "step=pin_proc errno=2"
    assert startup.open_fds(f"/proc/{os.getpid()}") == before


def test_pin_proc_failure_on_the_locks_path_names_that_step(monkeypatch):
    before = startup.open_fds(f"/proc/{os.getpid()}")
    monkeypatch.setattr(startup.server_auth, "PROC_LOCKS", Path("/proc/none-locks"))
    with pytest.raises(StartupError) as info:
        startup.pin_proc(f"/proc/{os.getpid()}")
    assert info.value.shape == "step=pin_locks errno=2"
    assert startup.open_fds(f"/proc/{os.getpid()}") == before


def test_landlock_proc_rules_are_bound_to_the_pinned_fds(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xc"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xs"))
    dirs = startup.PreparedDirs(config_fd=-1, state_fd=-1)
    pins = startup.ProcPins(proc_fd=71, locks_fd=72)
    for feature in ("bot", "web", "notify"):
        plan = StartupPlan.for_feature(feature, web_port=0)
        rules = {r.path: r.fd for r in startup.landlock_plan(plan, "/proc/1", dirs, pins).path_rules}
        assert rules["/proc/1"] == 71 and rules["/proc/locks"] == 72
