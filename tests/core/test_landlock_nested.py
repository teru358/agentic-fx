"""改善 mission の CLI 部分木に掛ける 2 枚目の Landlock domain のテスト。

効き目は使い捨ての実子 process に実 kernel の domain を掛けて観測する
(kernel の応答はモックしない)。Landlock は不可逆なので pytest 本体には掛けない。
子は `sys.executable -P -B` で helper を起こし、RLIMIT_CORE=0 を掛ける。
signal の的・listener・abstract socket はこの test が作り、終わりに pid 指定で
止める。
"""
from __future__ import annotations

import ctypes
import dataclasses
import errno
import json
import os
import resource
import secrets
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_fx.core import landlock as L
from agentic_fx.core import landlock_nested as N

HELPER = Path(__file__).resolve().parent / "_nested_landlock_child.py"
SLEEP = "/usr/bin/sleep"


def _host_has_tsync() -> bool:
    return N.probe_abi() >= 8


needs_tsync = pytest.mark.skipif(not _host_has_tsync(),
                                 reason="Landlock ABI 8 (TSYNC) が要る")


def _no_core() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _cmd(cfg: dict) -> list[str]:
    return [sys.executable, "-P", "-B", str(HELPER), json.dumps(cfg)]


def run_child(cfg: dict, timeout: float = 60) -> dict:
    proc = subprocess.run(_cmd(cfg), capture_output=True, text=True,
                          timeout=timeout, preexec_fn=_no_core)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    assert proc.returncode == 0 and lines, (proc.returncode, proc.stdout, proc.stderr)
    out = json.loads(lines[-1])
    assert "result" in out, out
    return out["result"]


@pytest.fixture
def sleeper():
    """domain の外にいる的。自分の session に置き、pgid 宛の signal が
    pytest 自身の process group に届かないようにする。"""
    procs: list[subprocess.Popen] = []

    def make() -> subprocess.Popen:
        p = subprocess.Popen([SLEEP, "60"], start_new_session=True,
                             preexec_fn=None)
        procs.append(p)
        return p

    yield make
    for p in procs:
        if p.poll() is None:
            os.kill(p.pid, signal.SIGKILL)
        p.wait(timeout=10)


@pytest.fixture
def tcp_listener():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    s.setblocking(False)
    yield s
    s.close()


def _accept_count(listener: socket.socket) -> int:
    n = 0
    while True:
        try:
            c, _ = listener.accept()
        except BlockingIOError:
            return n
        c.close()
        n += 1


@pytest.fixture
def abstract_listener():
    name = f"afx-test-{secrets.token_hex(8)}"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind("\0" + name)
    s.listen(8)
    s.setblocking(False)
    yield name, s
    s.close()


# --------------------------------------------------------------------------
# 純関数: ABI 判定、正規形と digest
# --------------------------------------------------------------------------

@pytest.mark.parametrize("abi", [0, 3, 5, 6, 7])
def test_abi_below_eight_is_rejected_without_fallback(abi):
    with pytest.raises(N.NestedLandlockError) as ei:
        N.ruleset_for_abi(abi)
    assert ei.value.reason == "landlock_abi_too_old"


@pytest.mark.parametrize("abi", [8, 9])
def test_abi_eight_or_newer_builds_the_same_ruleset(abi):
    rs = N.ruleset_for_abi(abi)
    assert rs == N.NESTED_RULESET
    assert rs.handled_fs == 0
    assert rs.handled_net == (L._ACCESS_NET_BIND_TCP | L._ACCESS_NET_CONNECT_TCP)
    assert rs.handled_scope == (L._SCOPE_ABSTRACT_UNIX_SOCKET | L._SCOPE_SIGNAL)
    assert rs.rules == ()
    assert rs.restrict_flags == L.RESTRICT_SELF_TSYNC
    assert rs.min_abi == 8


def test_abi_probe_reports_host_abi():
    # 実 kernel の値。Landlock の無い host では 0 (fail closed 側)
    assert N.probe_abi() == L.landlock_abi()


def test_reasons_reuse_the_existing_sandbox_vocabulary():
    assert set(N.NESTED_REASONS) <= set(L.SANDBOX_REASONS)


def test_ruleset_digest_is_stable_and_changes_with_any_bit():
    base = N.NESTED_RULESET
    d0 = N.ruleset_digest(base)
    assert d0 == N.ruleset_digest(N.ruleset_for_abi(8))
    assert len(d0) == 64 and int(d0, 16) >= 0
    # 別 process でも同じ値になる
    out = subprocess.run(
        [sys.executable, "-P", "-B", "-c",
         "from agentic_fx.core import landlock_nested as N;"
         "print(N.ruleset_digest(N.NESTED_RULESET))"],
        capture_output=True, text=True, timeout=60, check=True)
    assert out.stdout.strip() == d0
    seen = {d0}
    variants = []
    for name in ("handled_fs", "handled_net", "handled_scope", "restrict_flags"):
        for bit in range(4):
            v = getattr(base, name) ^ (1 << bit)
            variants.append(dataclasses.replace(base, **{name: v}))
    variants.append(dataclasses.replace(base, min_abi=9))
    variants.append(dataclasses.replace(base, rules=(("net_port", 3, 443),)))
    for v in variants:
        d = N.ruleset_digest(v)
        assert d not in seen, v
        seen.add(d)
    assert json.loads(N.canonical_form(base)) == {
        "handled_fs": 0, "handled_net": 3, "handled_scope": 3,
        "min_abi": 8, "restrict_flags": 8, "rules": [],
    }


def test_module_imports_only_the_standard_library():
    code = ("import sys;"
            "before=set(sys.modules);"
            "from agentic_fx.core import landlock_nested;"
            "print('\\n'.join(sorted(set(sys.modules)-before)))")
    out = subprocess.run([sys.executable, "-P", "-B", "-c", code],
                         capture_output=True, text=True, timeout=60, check=True)
    added = set(out.stdout.split())
    afx = {m for m in added if m.split(".")[0] == "agentic_fx"}
    assert afx <= {"agentic_fx", "agentic_fx.core", "agentic_fx.core.landlock_nested"}
    for heavy in ("numpy", "pandas", "yaml", "pydantic"):
        assert heavy not in added


def test_module_runs_from_its_source_file_alone(tmp_path):
    # launcher が source を埋め込む使い方でも動くこと (package 無しで exec)
    src = Path(N.__file__).read_text()
    code = src + "\nprint(ruleset_digest(NESTED_RULESET))\n"
    out = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path,
                         capture_output=True, text=True, timeout=60, check=True)
    assert out.stdout.strip() == N.ruleset_digest(N.NESTED_RULESET)


def test_source_runs_when_exec_into_a_namespace_not_in_sys_modules(tmp_path):
    # source 文字列を module 登録の無い namespace に exec する埋込みでも import できること
    code = (
        "import sys\n"
        f"src = open({str(N.__file__)!r}).read()\n"
        "ns = {'__name__': 'embedded_unregistered'}\n"
        "exec(compile(src, '<embedded>', 'exec'), ns)\n"
        "print(ns['ruleset_digest'](ns['NESTED_RULESET']))\n")
    out = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == N.ruleset_digest(N.NESTED_RULESET)


def test_libc_handle_is_created_once_and_typed():
    assert N._libc() is N._libc()
    libc = N._libc()
    assert libc.syscall.restype is ctypes.c_long
    assert libc.prctl.restype is ctypes.c_int
    assert libc.prctl.argtypes == [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                                   ctypes.c_ulong, ctypes.c_ulong]


# --------------------------------------------------------------------------
# 実子 process: network
# --------------------------------------------------------------------------

@needs_tsync
def test_explicit_bind_is_eacces_for_ipv4_and_ipv6():
    r = run_child({"scenario": "probes"})
    assert r["applied"] is not None
    assert r["bind4"] == {"errno": errno.EACCES}
    assert r["bind6"] == {"errno": errno.EACCES}


@needs_tsync
def test_direct_tcp_connect_is_eacces_inside_nested_domain(tcp_listener):
    port = tcp_listener.getsockname()[1]
    r = run_child({"scenario": "probes", "port": port})
    assert r["connect"] == {"errno": errno.EACCES}
    assert _accept_count(tcp_listener) == 0
    # 対照: nested 無しの同じ子は接続できる
    c = run_child({"scenario": "probes", "port": port, "nested": False})
    assert c["connect"] == {"ok": 1}
    assert _accept_count(tcp_listener) == 1


@needs_tsync
def test_listen_without_bind_is_not_closed_by_landlock_alone():
    p = subprocess.Popen(_cmd({"scenario": "listen_nobind"}), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, preexec_fn=_no_core)
    try:
        first = json.loads(p.stdout.readline())
        assert "port" in first, first
        with socket.create_connection(("127.0.0.1", first["port"]), timeout=5):
            pass
        rest = p.stdout.read()
        p.wait(timeout=30)
    finally:
        if p.poll() is None:
            os.kill(p.pid, signal.SIGKILL)
            p.wait(timeout=10)
    out = json.loads(rest.strip().splitlines()[-1])["result"]
    assert out["applied"] is not None
    assert out["listen"] == {"ok": first["port"]}
    assert out["accepted"] == 1


# --------------------------------------------------------------------------
# 実子 process: signal scope
# --------------------------------------------------------------------------

@needs_tsync
def test_signal_outside_subtree_is_denied(sleeper):
    t = sleeper()
    r = run_child({"scenario": "signal_outside", "target": t.pid,
                   "target_pgid": os.getpgid(t.pid)})
    assert r["applied"] is not None
    for k in ("kill0", "kill_term", "killpg0", "killpg_term", "pidfd_term"):
        assert r[k] == {"errno": errno.EPERM}, (k, r[k])
    assert t.poll() is None


@needs_tsync
def test_signal_inside_subtree_is_allowed():
    p = subprocess.run(_cmd({"scenario": "signal_inside"}), capture_output=True,
                       text=True, timeout=60, preexec_fn=_no_core,
                       start_new_session=True)
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
    assert p.returncode == 0 and lines, (p.returncode, p.stdout, p.stderr)
    r = json.loads(lines[-1])["result"]
    assert r["applied"] is not None
    assert r["kill0"] == {"ok": 0}
    assert r["kill_term"] == {"ok": 0}
    assert r["g1_rc"] == -signal.SIGTERM
    assert r["killpg_term"] == {"ok": 0}
    assert r["g2_rc"] == -signal.SIGTERM


@needs_tsync
def test_parent_domain_can_killpg_nested_subtree():
    p = subprocess.run(_cmd({"scenario": "worker_role"}), capture_output=True,
                       text=True, timeout=60, preexec_fn=_no_core,
                       start_new_session=True)
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
    assert p.returncode == 0 and lines, (p.returncode, p.stdout, p.stderr)
    r = json.loads(lines[-1])["result"]
    rep = r["launcher"]
    assert rep["applied"] is not None
    assert rep["pgid"] == rep["pid"]
    # 逆向き (nested の中から外側 domain の worker 役へ) は拒否
    assert rep["to_worker_kill0"] == {"errno": errno.EPERM}
    assert rep["to_worker_term"] == {"errno": errno.EPERM}
    # 親 domain から子 domain の pgid へは通り、launcher 役と孫が死ぬ
    assert r["killpg_kill"] == {"ok": 0}
    assert r["launcher_rc"] == -signal.SIGKILL
    assert r["grandchild_rc"] == -signal.SIGKILL


# --------------------------------------------------------------------------
# 実子 process: abstract UDS scope
# --------------------------------------------------------------------------

@needs_tsync
def test_abstract_unix_connect_outside_domain_is_denied(abstract_listener):
    name, s = abstract_listener
    own = f"afx-test-{secrets.token_hex(8)}"
    r = run_child({"scenario": "abstract", "abstract": name, "own_abstract": own})
    assert r["applied"] is not None
    assert r["outside"] == {"errno": errno.EPERM}
    assert _accept_count(s) == 0
    # 対照: domain の中で bind した abstract socket へは同じ domain の子が届く
    assert r["own_child_exit"] == 0
    assert r["own_accepted"] == 1


# --------------------------------------------------------------------------
# 実子 process: TSYNC
# --------------------------------------------------------------------------

@needs_tsync
def test_tsync_applies_domain_to_threads_started_before_restrict(sleeper):
    t = sleeper()
    r = run_child({"scenario": "tsync", "target": t.pid})
    assert r["applied"] is not None
    assert r["main_bind4"] == {"errno": errno.EACCES}
    assert r["thread"]["bind4"] == {"errno": errno.EACCES}
    assert r["thread"]["kill0"] == {"errno": errno.EPERM}
    assert t.poll() is None


# --------------------------------------------------------------------------
# 対照: nested を掛けない同じ子
# --------------------------------------------------------------------------

@needs_tsync
def test_contrast_without_nested_domain_signal_bind_and_abstract_connect_succeed(
        sleeper, abstract_listener):
    t = sleeper()
    name, s = abstract_listener
    r = run_child({"scenario": "probes", "nested": False, "target": t.pid,
                   "abstract": name})
    assert r["applied"] is None
    assert "ok" in r["bind4"] and "ok" in r["bind6"]
    assert r["kill0"] == {"ok": 0}
    assert r["abstract"] == {"ok": 1}
    assert _accept_count(s) == 1
    assert t.poll() is None
