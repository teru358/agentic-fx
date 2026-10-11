"""隔離済みの実 process (canary) で、拒否すべき操作が拒否され、必要な操作だけが通ること。"""
from __future__ import annotations

import errno
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from _afx_isolation_support import (
    FEATURES,
    PRINCIPAL,
    Interactive,
    make_root,
    needs_landlock,
    needs_strace,
    needs_x86_64,
    run_canary,
)
from afx_ops.isolation import profiles as P
from afx_ops.isolation.policy import AUDIT_ARCH_X86_64, evaluate
from afx_ops.isolation.syscalls_x86_64 import SNAPSHOT_NUMBERS as NR

pytestmark = [needs_x86_64, needs_landlock]

NO_SUCH_PID = 0x7FFFFFFF  # pid_max より大きい: seccomp が無ければ kernel は ESRCH を返す


class _Echo:
    """127.0.0.1 / pathname UDS の echo server (thread)。"""

    def __init__(self, family: int, kind: int, address) -> None:
        self.sock = socket.socket(family, kind)
        self.sock.bind(address)
        if kind == socket.SOCK_STREAM:
            self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.sock.getsockname()[1]

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                if self.sock.type == socket.SOCK_STREAM:
                    conn, _ = self.sock.accept()
                    with conn:
                        conn.settimeout(5)
                        conn.sendall(conn.recv(64))
                else:
                    data, addr = self.sock.recvfrom(64)
                    self.sock.sendto(data, addr)
            except (TimeoutError, OSError):
                continue

    def close(self) -> None:
        self.stop.set()
        self.thread.join(5)
        self.sock.close()


@pytest.fixture
def root(tmp_path) -> Path:
    return make_root(tmp_path)


@pytest.fixture
def sibling():
    """隔離されていない同 uid の別 process (他 pid の /proc の観測点)。"""
    proc = subprocess.Popen(["sleep", "120"])
    yield proc.pid
    os.kill(proc.pid, 9)
    proc.wait(10)


@pytest.fixture
def servers(tmp_path):
    made = []

    def make(family, kind, address):
        echo = _Echo(family, kind, address)
        made.append(echo)
        return echo
    yield make
    for echo in made:
        echo.close()


def _spec(root: Path, feature: str, op: str, **params) -> dict:
    return {"feature": feature, "root": str(root), "op": op, "params": params}


def _errno_of(feature: str, name: str, *args: int) -> int:
    value = evaluate(P.feature_filter(feature).table, AUDIT_ARCH_X86_64, NR[name], args)
    assert value & 0xFFFF0000 == 0x00050000, (feature, name, hex(value))
    return value & 0xFFFF


EPERM, EACCES, EBADF = errno.EPERM, errno.EACCES, errno.EBADF

# (攻撃名, op, params の作り方, 期待の作り方)。期待の errno は表 (evaluator) と照合する
_ATTACKS = {
    "bind_any4": ("bind", lambda c: {"host": "0.0.0.0"}, lambda f: [-1, _errno_of(f, "bind")]),
    "bind_any6": ("bind", lambda c: {"host": "::"}, lambda f: [-1, _errno_of(f, "bind")]),
    "bind_loopback": ("bind", lambda c: {"host": "127.0.0.1"},
                      lambda f: [-1, _errno_of(f, "bind")]),
    "listen": ("listen", lambda c: {}, lambda f: [-1, _errno_of(f, "listen")]),
    "ptrace_attach": ("ptrace_attach", lambda c: {"pid": NO_SUCH_PID},
                      lambda f: [-1, _errno_of(f, "ptrace")]),
    "process_vm_readv": ("process_vm_readv", lambda c: {"pid": NO_SUCH_PID},
                         lambda f: [-1, _errno_of(f, "process_vm_readv")]),
    "process_vm_writev": ("process_vm_writev", lambda c: {"pid": NO_SUCH_PID},
                          lambda f: [-1, _errno_of(f, "process_vm_writev")]),
    "kill_other": ("kill", lambda c: {"pid": c["sibling"]}, lambda f: [-1, EPERM]),
    "tgkill_other": ("tgkill", lambda c: {"pid": c["sibling"]}, lambda f: [-1, EPERM]),
    "pidfd_getfd": ("pidfd_getfd", lambda c: {"pid": NO_SUCH_PID},
                    lambda f: [-1, _errno_of(f, "pidfd_open"),
                               -1, _errno_of(f, "pidfd_getfd")]),
    "proc_mem_of_other": ("open", lambda c: {"path": f"/proc/{c['sibling']}/mem"},
                          lambda f: [-1, EACCES]),
    "proc_environ_of_other": ("open", lambda c: {"path": f"/proc/{c['sibling']}/environ"},
                              lambda f: [-1, EACCES]),
    "inherited_key_fd": ("fstat_fd", lambda c: {"fd": c["inherited_fd"]},
                         lambda f: [-1, EBADF]),
    "other_principal_key": ("read_file", lambda c: {"path": c["other_key"]},
                            lambda f: [-1, EACCES]),
    "own_key_after_isolation": ("read_file", lambda c: {"path": c["own_key"]},
                                lambda f: [-1, EACCES]),
    "clone_newuser": ("clone", lambda c: {"flags": 0x10000000 | 17},
                      lambda f: [-1, _errno_of(f, "clone", 0x10000000 | 17)]),
    "clone_parent": ("clone", lambda c: {"flags": 0x8000 | 17},
                     lambda f: [-1, _errno_of(f, "clone", 0x8000 | 17)]),
    "clone_fork": ("clone", lambda c: {"flags": 17}, lambda f: [-1, _errno_of(f, "clone", 17)]),
    "unshare": ("unshare", lambda c: {}, lambda f: [-1, _errno_of(f, "unshare")]),
    "setns": ("setns", lambda c: {}, lambda f: [-1, _errno_of(f, "setns")]),
    "io_uring_setup": ("io_uring_setup", lambda c: {},
                       lambda f: [-1, _errno_of(f, "io_uring_setup")]),
    "bpf": ("bpf", lambda c: {}, lambda f: [-1, _errno_of(f, "bpf")]),
}
_WEB_ONLY = {
    "socket_inet_stream": ("socket", lambda c: {"family": 2, "type": 1},
                           lambda f: [-1, _errno_of(f, "socket", 2, 1)]),
    "socket_inet6_dgram": ("socket", lambda c: {"family": 10, "type": 2},
                           lambda f: [-1, _errno_of(f, "socket", 10, 2)]),
    "socket_inet_dgram": ("socket", lambda c: {"family": 2, "type": 2},
                          lambda f: [-1, _errno_of(f, "socket", 2, 2)]),
}

DENIED = [(f, a) for f in FEATURES for a in _ATTACKS] + [("web", a) for a in _WEB_ONLY]


@pytest.mark.parametrize(("feature", "attack"), DENIED, ids=[f"{f}-{a}" for f, a in DENIED])
def test_canary_denied(feature, attack, root, child_env, sibling):
    op, params_of, expect_of = {**_ATTACKS, **_WEB_ONLY}[attack]
    others = [p for f, p in (("bot", "ops-bot"), ("web", "ops-web"), ("notify", "ops-notify"))
              if f != feature]
    other_key = root / "keys" / f"{others[0]}.token"
    own = {"bot": "ops-bot", "web": "ops-web", "notify": "ops-notify"}[feature]
    # 他 principal の鍵を CLOEXEC なしで開いて渡す (隔離の fd 閉鎖で閉じられること)
    leaked = os.open(other_key, os.O_RDONLY)
    try:
        ctx = {"sibling": sibling, "inherited_fd": leaked, "other_key": str(other_key),
               "own_key": str(root / "keys" / f"{own}.token")}
        spec = _spec(root, feature, op, **params_of(ctx))
        got = run_canary(spec, child_env, pass_fds=(leaked,))
    finally:
        os.close(leaked)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.isolated
    assert got.result == expect_of(feature)


# 隔離が無ければ同じ攻撃は kernel まで届いて別の結果になる (canary の観測が隔離の効果であること)
_CONTROLS = [
    ("skip_seccomp", "process_vm_readv", {"pid": NO_SUCH_PID}, [-1, errno.ESRCH]),
    ("skip_seccomp", "ptrace_attach", {"pid": NO_SUCH_PID}, [-1, errno.ESRCH]),
    ("skip_seccomp", "setns", {}, [-1, errno.EINVAL]),
    ("skip_seccomp", "process_vm_writev", {"pid": NO_SUCH_PID}, [-1, errno.ESRCH]),
    ("skip_landlock", "read_file", {"path": "OTHER_KEY"}, "readable"),
]


@pytest.mark.parametrize(("fault", "op", "params", "expect"), _CONTROLS,
                         ids=[f"{c[0]}-{c[1]}" for c in _CONTROLS])
def test_canary_attacks_reach_the_kernel_without_the_layer(fault, op, params, expect, root,
                                                         child_env):
    params = {k: (str(root / "keys" / "ops-web.token") if v == "OTHER_KEY" else v)
              for k, v in params.items()}
    spec = {**_spec(root, "bot", op, **params), "fault": fault}
    got = run_canary(spec, child_env)
    # 層を飛ばすと自己検査で止まる。止まる前の観測は fault 下の単体実行で行う
    assert got.rc != 0 and not got.isolated
    spec["fault_self_check"] = "off"
    got = run_canary(spec, child_env)
    assert got.rc == 0, got.stderr[-2000:]
    if expect == "readable":
        assert got.result[0] > 0
    else:
        assert got.result == expect


ALLOWED = (
    [(f, op) for f in FEATURES for op in ("clone3_enosys", "thread", "futex", "prctl_dumpable0",
                                           "unix_connect", "proc_locks", "random")]
    + [(f, op) for f in ("bot", "notify")
       for op in ("tcp_connect", "udp_echo", "getaddrinfo", "getaddrinfo_addrconfig",
                  "tls_files")])


@pytest.mark.parametrize(("feature", "op"), ALLOWED, ids=[f"{f}-{o}" for f, o in ALLOWED])
def test_canary_allowed(feature, op, root, child_env, servers, tmp_path):
    if op == "clone3_enosys":
        spec, expect = _spec(root, feature, "clone3"), lambda r: r == [-1, errno.ENOSYS]
    elif op == "thread":
        spec, expect = _spec(root, feature, "thread"), lambda r: r == [1, 0]
    elif op == "futex":
        spec, expect = _spec(root, feature, "futex"), lambda r: r == [0, 0]
    elif op == "prctl_dumpable0":
        spec, expect = _spec(root, feature, "prctl_dumpable", value=0), lambda r: r == [0, 0]
    elif op == "unix_connect":
        path = tmp_path / "nss.sock"
        servers(socket.AF_UNIX, socket.SOCK_STREAM, str(path))
        spec, expect = _spec(root, feature, "unix_connect", path=str(path)), \
            lambda r: r == [2, 0]
    elif op == "proc_locks":
        spec, expect = _spec(root, feature, "read_file", path="/proc/locks"), \
            lambda r: r[0] >= 0 and r[1] == 0
    elif op == "random":
        spec, expect = _spec(root, feature, "random"), lambda r: r == [20, 0]
    elif op == "tcp_connect":
        echo = servers(socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.1", 0))
        spec, expect = _spec(root, feature, "tcp_connect", port=echo.port,
                             connect_ports=[echo.port]), lambda r: r == [2, 0]
    elif op == "udp_echo":
        echo = servers(socket.AF_INET, socket.SOCK_DGRAM, ("127.0.0.1", 0))
        spec, expect = _spec(root, feature, "udp_echo", port=echo.port), lambda r: r == [4, 0]
    elif op == "getaddrinfo":
        spec, expect = _spec(root, feature, "getaddrinfo", host="localhost"), \
            lambda r: r[0] >= 1 and r[1] == 0
    elif op == "getaddrinfo_addrconfig":
        spec, expect = _spec(root, feature, "getaddrinfo", host="localhost", addrconfig=True), \
            lambda r: r[0] >= 1 and r[1] == 0
    elif op == "tls_files":
        spec, expect = _spec(root, feature, "tls_files"), lambda r: r[0] >= 1 and r[1] == 0
    else:
        raise AssertionError(op)
    got = run_canary(spec, child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.isolated
    assert expect(got.result), got.result


@pytest.mark.parametrize("feature", ("bot", "notify"))
def test_tcp_connect_outside_the_allowed_ports_is_denied(feature, root, child_env, servers):
    allowed = servers(socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.1", 0))
    other = servers(socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.1", 0))
    spec = _spec(root, feature, "tcp_connect", port=other.port, connect_ports=[allowed.port])
    got = run_canary(spec, child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == [-1, EACCES]


def test_abstract_unix_socket_outside_the_domain_is_denied(root, child_env):
    name = f"\0afx-ops-isolation-test-{os.getpid()}"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(name)
    listener.listen(1)
    try:
        got = run_canary(_spec(root, "bot", "unix_connect", path=name), child_env)
    finally:
        listener.close()
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == [-1, EPERM]


def test_signal_to_another_process_is_stopped_by_the_landlock_scope_alone(root, child_env,
                                                                          sibling):
    # seccomp を外しても Landlock の signal scope が別 domain の process への kill を拒む
    only_landlock = {**_spec(root, "bot", "kill", pid=sibling), "fault": "skip_seccomp",
                     "fault_self_check": "off"}
    got = run_canary(only_landlock, child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == [-1, EPERM]
    # 両方の層を外すと kernel まで届いて成功する (sig 0 なので sibling には何も起きない)
    neither = {**only_landlock, "fault": ["skip_seccomp", "skip_landlock"]}
    got = run_canary(neither, child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == [0, 0]


@pytest.mark.parametrize("feature", ("bot", "notify"))
def test_tcp_connect_is_limited_to_443_by_the_fixed_plan(feature, root, child_env, servers):
    # connect_ports を上書きしない = 機能の固定 plan の (443,)
    other = servers(socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.1", 0))
    denied = run_canary(_spec(root, feature, "tcp_connect_only", port=other.port), child_env)
    assert denied.rc == 0, denied.stderr[-2000:]
    assert denied.result == [-1, EACCES]
    # 443 は Landlock を通って kernel の接続処理に届く (待受の有無で成功か ECONNREFUSED)。
    # 何も送らない
    allowed = run_canary(_spec(root, feature, "tcp_connect_only", port=443), child_env)
    assert allowed.rc == 0, allowed.stderr[-2000:]
    assert allowed.result in ([0, 0], [-1, errno.ECONNREFUSED])


def test_held_listener_cannot_be_rebound_or_used_outward(root, child_env, servers):
    target = servers(socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.1", 0))
    got = run_canary(_spec(root, "web", "listener_misuse", port=target.port), child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == [-1, EPERM,                      # bind
                          -1, EPERM,                      # listen
                          -1, EACCES,                     # 外向き connect (Landlock)
                          -1, EPERM]                      # 宛先付き sendto


@pytest.mark.parametrize("field", ["connect_ports", "check_unit", "require_notify", "config_dir",
                                   "state_dir", "secret_dir", "secret_files"])
def test_a_widened_production_plan_stops_before_reading_keys(field, root, child_env):
    key = root / "keys" / "ops-bot.token"
    key.chmod(0)  # 鍵を読もうとすれば key_file_invalid になる
    got = run_canary({**_spec(root, "bot", "noop"), "plan": f"widen:{field}"},
                     {**child_env, "XDG_CONFIG_HOME": str(root / "xdg-config"),
                      "XDG_STATE_HOME": str(root / "xdg-state")})
    assert got.rc == 71
    assert got.stderr.strip().splitlines() == ["afx-ops: plan_invalid"]
    assert not got.isolated and not got.token_sent


def test_the_production_plan_refuses_to_run_outside_the_unit(root, child_env):
    got = run_canary({**_spec(root, "bot", "noop"), "plan": "production"},
                     {**child_env, "XDG_CONFIG_HOME": str(root / "xdg-config"),
                      "XDG_STATE_HOME": str(root / "xdg-state")})
    assert got.rc == 71
    assert got.stderr.strip().splitlines() == ["afx-ops: unit_cgroup"]
    assert not got.token_sent


_TRACED = [("bot", "thread", {}), ("bot", "getaddrinfo", {"host": "localhost",
                                                         "addrconfig": True}),
           ("bot", "tls_files", {}), ("notify", "getaddrinfo", {"host": "localhost"}),
           ("web", "thread", {}), ("web", "open_fds", {})]


@needs_strace
@pytest.mark.parametrize(("feature", "op", "params"), _TRACED,
                         ids=[f"{f}-{o}" for f, o, _p in _TRACED])
def test_signal_and_lock_syscalls_denied_by_the_table_are_not_used(feature, op, params, root,
                                                                   child_env, tmp_path):
    # 表で EPERM にした tgkill / flock (と kill / tkill) を、隔離前後のどこでも呼ばないこと
    trace = tmp_path / "calls.trace"
    got = run_canary(_spec(root, feature, op, **params), child_env,
                     prefix=("strace", "-f", "-qq", "-e", "trace=tgkill,tkill,kill,flock",
                             "-o", str(trace)))
    assert got.rc == 0, got.stderr[-2000:]
    assert trace.read_text().strip() == ""
    for name in ("tgkill", "flock"):
        assert P.feature_filter(feature).table.action_of(name) == "EPERM"


# --- resolv.conf は file 単位の規則だけ ----------------------------------------------

def test_resolv_conf_rules_are_leaf_files_only():
    from afx_ops.isolation import landlock, startup
    resolv = startup.RESOLV_CONF
    real = os.path.realpath(resolv)
    rules = {r.path: r for r in startup.dns_tls_rules()}
    assert rules[resolv].access == landlock.ACCESS_FS_READ_FILE
    assert rules[real].access == landlock.ACCESS_FS_READ_FILE
    # 実体を含む dir には規則を与えない
    assert [p for p in rules if os.path.isdir(p) and real.startswith(p.rstrip("/") + "/")] == []


def _resolv_sibling() -> str | None:
    real = os.path.realpath("/etc/resolv.conf")
    if real == "/etc/resolv.conf":
        return None
    parent = os.path.dirname(real)
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        return None
    for name in names:
        path = os.path.join(parent, name)
        if path != real and os.path.isfile(path) and os.access(path, os.R_OK):
            return path
    return None


@pytest.mark.parametrize("feature", ("bot", "notify"))
def test_resolv_conf_is_readable_but_its_siblings_are_not(feature, root, child_env):
    sibling = _resolv_sibling()
    if sibling is None:
        pytest.skip("/etc/resolv.conf が他 file のある dir への symlink ではない")
    own = run_canary(_spec(root, feature, "read_file", path="/etc/resolv.conf"), child_env)
    other = run_canary(_spec(root, feature, "read_file", path=sibling), child_env)
    assert own.rc == 0 and other.rc == 0, (own.stderr[-2000:], other.stderr[-2000:])
    assert own.result[0] > 0 and own.result[1] == 0
    assert other.result == [-1, EACCES]


# --- 自己検査 -----------------------------------------------------------------

FAULTS = {
    "skip_landlock": "self_check_landlock",
    "skip_seccomp": "self_check_seccomp",
    "keep_dumpable": "self_check_dumpable",
    "extra_fd": "self_check_fds",
    "extra_thread": "self_check_threads",
}


@pytest.mark.parametrize("fault", sorted(FAULTS))
@pytest.mark.parametrize("feature", FEATURES)
def test_self_check_rejects_each_missing_layer(feature, fault, root, child_env):
    got = run_canary({**_spec(root, feature, "noop"), "fault": fault}, child_env)
    assert got.rc == 71
    assert got.stderr.strip().splitlines()[-1] == f"afx-ops: {FAULTS[fault]}"
    assert not got.isolated and not got.token_sent


# 各段の想定外の例外 (target, 何回目の呼出しか, 例外) → 固定 reason。traceback で終わらない
_RAISES = [
    ("_read", 1, "OSError", "self_check_unreadable"),        # 起動前提の status
    ("_read", 1, "RuntimeError", "startup_internal_error"),
    ("_task_count", 1, "OSError", "self_check_unreadable"),  # 起動前提の thread 数
    ("_task_count", 2, "OSError", "self_check_unreadable"),  # 自己検査の thread 数
    ("open_fds", 1, "OSError", "fd_close_failed"),           # 継承 fd の閉鎖
    ("open_fds", 2, "OSError", "self_check_unreadable"),     # 自己検査の fd 集合
    ("open_fds", 2, "RuntimeError", "startup_internal_error"),
    ("filter_count", 1, "ValueError", "self_check_unreadable"),
    ("filter_count", 1, "OSError", "self_check_unreadable"),
    ("_read", 2, "OSError", "self_check_unreadable"),        # filter 数の status
    ("_read", 3, "OSError", "self_check_unreadable"),        # 自己検査の status
    ("runtime_rules", 1, "OSError", "landlock_failed"),
    ("runtime_rules", 1, "KeyError", "startup_internal_error"),
    ("feature_filter", 1, "RuntimeError", "startup_internal_error"),
]


@pytest.mark.parametrize(("target", "nth", "exc", "reason"), _RAISES,
                         ids=[f"{t}-{n}-{e}" for t, n, e, _r in _RAISES])
def test_unexpected_failure_in_each_stage_maps_to_a_fixed_reason(target, nth, exc, reason, root,
                                                                 child_env):
    spec = {**_spec(root, "bot", "noop"), "raise": {"target": target, "nth": nth, "exc": exc}}
    got = run_canary(spec, child_env)
    assert got.rc == 71, got.stderr[-2000:]
    assert got.stderr.strip().splitlines() == [f"afx-ops: {reason}"]
    assert "Traceback" not in got.stderr
    assert not got.isolated and not got.token_sent


def test_self_check_passes_without_faults(root, child_env):
    got = run_canary(_spec(root, "bot", "noop"), child_env)
    assert got.rc == 0 and got.isolated and got.token_sent


def test_missing_no_new_privs_stops_before_reading_keys(root, child_env):
    # 鍵 file を読めない状態にしておき、鍵を読む前に止まること (key_file_invalid にならない)
    key = root / "keys" / "ops-bot.token"
    key.chmod(0)
    got = run_canary({**_spec(root, "bot", "noop"), "nnp": False}, child_env)
    assert got.rc == 71
    assert got.stderr.strip().splitlines()[-1] == "afx-ops: unit_no_new_privs"
    assert not got.token_sent


def test_unreadable_key_fails_closed(root, child_env):
    (root / "keys" / "ops-bot.token").unlink()
    got = run_canary(_spec(root, "bot", "noop"), child_env)
    assert got.rc == 71
    assert got.stderr.strip().splitlines()[-1] == "afx-ops: key_missing"


def test_unit_check_rejects_a_non_unit_launch(root, child_env):
    got = run_canary({**_spec(root, "bot", "noop"), "unit": True}, child_env)
    assert got.rc == 71
    assert got.stderr.strip().splitlines()[-1] == "afx-ops: unit_cgroup"


# --- cross-process ------------------------------------------------------------

def test_isolated_peer_cannot_take_token_from_another_ops(root, child_env):
    ptrace_scope = Path("/proc/sys/kernel/yama/ptrace_scope")
    scope = ptrace_scope.read_text().strip() if ptrace_scope.exists() else "absent"
    bot = Interactive({**_spec(root, "bot", "noop"), "wait_before": True, "hold_after": True},
                      child_env)
    try:
        bot.wait_line("READY")
        # 隔離前 (dumpable 1) の bot は、親である非隔離の同 uid process から mem を開ける
        fd = os.open(f"/proc/{bot.pid}/mem", os.O_RDONLY)
        os.close(fd)
        bot.send()
        bot.wait_line("HELD")
        bot_key = str(root / "keys" / "ops-bot.token")
        attacks = {
            "bot_key": ("read_file", {"path": bot_key}, [-1, EACCES]),
            "bot_mem": ("open", {"path": f"/proc/{bot.pid}/mem"}, [-1, EACCES]),
            "bot_environ": ("open", {"path": f"/proc/{bot.pid}/environ"}, [-1, EACCES]),
            "ptrace": ("ptrace_attach", {"pid": bot.pid}, [-1, EPERM]),
            "process_vm_readv": ("process_vm_readv", {"pid": bot.pid}, [-1, EPERM]),
            "process_vm_writev": ("process_vm_writev", {"pid": bot.pid}, [-1, EPERM]),
            "kill": ("kill", {"pid": bot.pid}, [-1, EPERM]),
            "tgkill": ("tgkill", {"pid": bot.pid}, [-1, EPERM]),
            "pidfd_getfd": ("pidfd_getfd", {"pid": bot.pid}, [-1, EPERM, -1, EPERM]),
        }
        seen = {}
        for name, (op, params, _expect) in attacks.items():
            got = run_canary(_spec(root, "notify", op, **params), child_env)
            assert got.rc == 0, got.stderr[-2000:]
            seen[name] = got.result
        assert seen == {name: expect for name, (_o, _p, expect) in attacks.items()}
        # 非隔離の同 uid process (この test process、bot の親) からも dumpable 0 で開けない
        with pytest.raises(PermissionError):
            os.open(f"/proc/{bot.pid}/mem", os.O_RDONLY)
        assert scope in ("0", "1", "2", "3", "absent")
        bot.send()
        done = bot.finish()
        assert done.rc == 0
    finally:
        bot.kill()


# --- 領域の用意: state は隔離前に作り、config が無ければ鍵を読む前に止まる -------------

@pytest.mark.parametrize("feature", FEATURES)
def test_missing_state_dir_is_created_before_isolation(feature, root, child_env):
    state = root / feature / "state"
    state.rmdir()
    got = run_canary(_spec(root, feature, "write_state", dir=str(state)), child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.isolated and got.result == [3, 0]
    assert state.is_dir() and (state.stat().st_mode & 0o777) == 0o700


@pytest.mark.parametrize("feature", FEATURES)
def test_missing_config_dir_stops_before_reading_keys(feature, root, child_env):
    shutil.rmtree(root / feature / "config")
    key = root / "keys" / f"{PRINCIPAL[feature]}.token"
    key.chmod(0)  # 鍵を読もうとすれば key_file_invalid になる
    got = run_canary(_spec(root, feature, "noop"), child_env)
    assert got.rc == 71
    assert got.stderr.strip().splitlines() == ["afx-ops: config_dir_missing"]
    assert not got.isolated and not got.token_sent


def test_a_state_symlink_stops_before_reading_keys_and_isolation(root, child_env):
    state = root / "bot" / "state"
    state.rmdir()
    state.symlink_to(root / "keys")
    key = root / "keys" / "ops-bot.token"
    key.chmod(0)  # 鍵を読もうとすれば key_file_invalid になる
    got = run_canary(_spec(root, "bot", "noop"), child_env)
    assert got.rc == 71
    assert got.stderr.strip().splitlines() == ["afx-ops: state_dir_invalid"]
    assert not got.isolated and not got.token_sent


def test_a_loose_state_dir_stops_before_isolation(root, child_env):
    (root / "bot" / "state").chmod(0o755)
    got = run_canary(_spec(root, "bot", "noop"), child_env)
    assert got.rc == 71
    assert got.stderr.strip().splitlines() == ["afx-ops: state_dir_invalid"]
    assert not got.isolated and not got.token_sent


def test_landlock_follows_the_checked_state_dir_not_a_later_swap(root, child_env):
    # 検査の後で state を他 principal の鍵 dir への symlink に差し替えても、規則は検査した
    # dir (今は state.orig) に付いていて、差し替え先には書けない
    state = root / "bot" / "state"
    before = sorted(p.name for p in (root / "keys").iterdir())
    got = run_canary({**_spec(root, "bot", "write_state", dir=str(state)),
                      "swap_after_prepare": {"path": str(state), "to": str(root / "keys")}},
                     child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.isolated and got.result == [-1, EACCES]
    assert sorted(p.name for p in (root / "keys").iterdir()) == before
    got = run_canary({**_spec(root, "notify", "write_state",
                              dir=str(root / "notify" / "state.orig")),
                      "swap_after_prepare": {"path": str(root / "notify" / "state"),
                                             "to": str(root / "keys")}}, child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == [3, 0]
