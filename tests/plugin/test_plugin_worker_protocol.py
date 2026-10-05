"""plugin worker の二段 protocol と、load 前後の失敗の分類を実 worker で確かめる。

隔離の効き (Landlock / seccomp) は kernel の実応答で見る。worker は必ず
`PluginSession` が `Popen` で起こす子 process で、pytest の process には何も掛けない。
attestation の各違反は、本物の隔離段を通したうえで 1 か所だけ壊した応答を返す
制御 worker (`_control_worker.py`) で起こす。
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pandas as pd
import pytest

from agentic_fx.config import load_settings
from agentic_fx.core import runtime_fingerprint, seccomp
from agentic_fx.plugin import sandbox
from agentic_fx.plugin.loader import PluginMeta, content_hash
from agentic_fx.plugin.sandbox import PluginSession, SandboxError, run_plugin

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
CONTROL = Path(__file__).with_name("_control_worker.py")

pytestmark = pytest.mark.skipif(sandbox.host_preflight()[0] is not None,
                                reason="this host cannot isolate plugin workers")


@pytest.fixture(scope="module")
def plugin_settings():
    return load_settings(EXAMPLE).plugin


@pytest.fixture(autouse=True)
def _fresh_orphans(monkeypatch):
    monkeypatch.setattr(sandbox, "_ORPHANS", [])
    monkeypatch.setattr(sandbox, "_ORPHAN_OVERFLOW_LOGGED", False)


INDICATOR_OK = "def compute(df, params):\n    return {'x': 1.0}\n"


def _meta(base: Path, name: str, plugin_py: str, kind: str = "indicator") -> PluginMeta:
    d = base / name
    d.mkdir()
    (d / "plugin.py").write_text(plugin_py)
    (d / "config.yaml").write_text(f"kind: {kind}\n")
    (d / "test_plugin.py").write_text("def test_placeholder():\n    pass\n")
    return PluginMeta(name=name, kind=kind, path=d, params={}, timeframe=None, pairs=(),
                      max_bars=200, content_hash=content_hash(d))


def _df(n: int = 30) -> pd.DataFrame:
    idx = pd.date_range("2026-01-05", periods=n, freq="1h", tz="UTC")
    close = [100.0 + i for i in range(n)]
    return pd.DataFrame({"open": close, "high": close, "low": close, "close": close,
                         "volume": [1.0] * n}, index=idx)


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _diagnostics(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records
                     if r.name == "agentic_fx.plugin.sandbox")


def _route_popen(monkeypatch, argv_for, spawned: list):
    """`PluginSession` の Popen を、同じ引数 (env・cwd・pipe) のまま別 argv で起こす。"""
    real = subprocess.Popen

    def popen(argv, **kwargs):
        proc = real(argv_for(argv), **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(sandbox.subprocess, "Popen", popen)


def _route_to_control(monkeypatch, mode: str, spawned: list):
    _route_popen(monkeypatch, lambda argv: [argv[0], "-B", str(CONTROL), argv[-1], mode],
                 spawned)


# --- 正常な二段 protocol --------------------------------------------------

def test_real_worker_two_stage_startup_is_verified_and_isolated(tmp_path, plugin_settings):
    settings = plugin_settings.model_copy(update={"sandbox_session_cpu_sec": 7})
    meta = _meta(tmp_path, "ok", INDICATOR_OK)
    spawned: list = []
    real = subprocess.Popen

    with pytest.MonkeyPatch.context() as mp:
        def popen(argv, **kwargs):
            spawned.append(real(argv, **kwargs))
            return spawned[-1]
        mp.setattr(sandbox.subprocess, "Popen", popen)
        with PluginSession(meta, settings=settings) as session:
            (proc,) = spawned
            assert proc.args[:4] == [sys.executable, "-B", "-m", "agentic_fx.plugin.worker"]
            assert session.pid == proc.pid
            # 全 task が NoNewPrivs 1 / Seccomp 2 / Seccomp_filters 1
            assert sandbox.check_worker_tasks(proc.pid) is None
            limits = Path(f"/proc/{proc.pid}/limits").read_text()
            assert any(line.startswith("Max cpu time") and line.split()[3:5] == ["7", "7"]
                       for line in limits.splitlines())
            assert any(line.startswith("Max core file size")
                       and line.split()[4:6] == ["0", "0"] for line in limits.splitlines())
            assert session.call({"df": _df(), "params": {}}) == {"x": 1.0}
    assert session.worker_returncode == 0
    assert session.worker_signal is None
    assert not (meta.path / "__pycache__").exists()


# --- attestation の検査順 ---------------------------------------------------

_REFUSALS = [
    ("old_ready", "attestation_missing"),
    ("plugin_ready_first", "attestation_order"),
    ("unknown_phase", "attestation_unexpected_message"),
    ("extra_line", "attestation_unexpected_message"),
    ("bad_nonce", "attestation_mismatch:nonce"),
    ("bad_pid", "attestation_mismatch:pid"),
    ("missing_pid", "attestation_missing:pid"),
    ("extra_field", "attestation_unexpected_field"),
    ("bad_abi_type", "attestation_mismatch:landlock_fs_abi"),
    ("missing_scope", "attestation_missing:scope"),
    ("reordered", "attestation_mismatch:sandbox_profile_version"),
    ("okfalse_unknown_reason", "sandbox_setup_failed:unknown"),
    # 複数の違反は、先に検査される項目の reason になる
    ("okfalse_with_extra_field", "sandbox_setup_failed:rlimit_failed"),
    ("extra_field_and_bad_pid", "attestation_unexpected_field"),
    ("bad_pid_and_bad_nonce", "attestation_mismatch:pid"),
    ("bad_nonce_and_extra_line", "attestation_mismatch:nonce"),
    # 隔離せずに形だけ完全な attestation を返す worker は /proc の検査で止まる
    ("unisolated_forgery", "proc_status_mismatch:NoNewPrivs"),
    # 継承した filter は worker が自分の filter と Landlock の前に拒否する
    ("inherited_filter", "inherited_seccomp_filter"),
    ("exit_before_ready", "worker_bootstrap_failed"),
    ("sigsys_before_ready", "startup_sigsys_unattributed"),
]


@pytest.mark.parametrize("mode,reason", _REFUSALS)
def test_control_worker_refusals_never_send_load(monkeypatch, tmp_path, plugin_settings,
                                                 caplog, mode, reason):
    caplog.set_level(logging.INFO, logger="agentic_fx.plugin.sandbox")
    spawned: list = []
    _route_to_control(monkeypatch, mode, spawned)
    session = PluginSession(_meta(tmp_path, "ctl", INDICATOR_OK), settings=plugin_settings)
    with pytest.raises(SandboxError) as error:
        session.__enter__()
    assert error.value.code == session.error_code == "sandbox_unavailable"
    assert error.value.sandbox_reason == session.sandbox_reason == reason
    # reason は例外文字列に出さない (技術ログと activity だけ)
    assert reason not in str(error.value)
    assert f"sandbox_reason={reason}" in _diagnostics(caplog)
    assert "CONTROL_LOAD_RECEIVED" not in _diagnostics(caplog)
    assert _gone(spawned[0].pid)


@pytest.mark.parametrize("reason",
                         ["plugin_file_invalid", "allowlist_not_leaf", "allowlist_guarded"])
def test_candidate_isolation_failures_are_plugin_error_not_sandbox_unavailable(
        monkeypatch, tmp_path, plugin_settings, reason):
    """候補の plugin.py / config.yaml が原因の隔離失敗 (symlink・非通常ファイル・
    guarded dir) は候補の責任。環境障害 (sandbox_unavailable) ではなく plugin_error
    に分類し、agent に修正を促せるようにする (load は送らない)。"""
    spawned: list = []
    _route_to_control(monkeypatch, f"okfalse_candidate:{reason}", spawned)
    session = PluginSession(_meta(tmp_path, "ctl", INDICATOR_OK), settings=plugin_settings)
    with pytest.raises(SandboxError) as error:
        session.__enter__()
    assert error.value.code == session.error_code == "plugin_error"
    assert session.sandbox_reason != "sandbox_unavailable"
    # reason は例外文字列に出さない (技術ログだけ)
    assert reason not in str(error.value)
    assert _gone(spawned[0].pid)


def test_plugin_ready_okfalse_from_a_worker_that_already_exited_is_plugin_error(
        monkeypatch, tmp_path, plugin_settings):
    """候補が plugin_ready ok:false を書いてすぐ死に、親が遅れて死を先に観測しても、
    load の応答 (plugin_ready) は正規の 1 応答として読んで plugin_error にする
    (crashed に化けさせない)。plugin_ready の読取でだけ worker の死を先に待つことで、
    reap が先に成功する経路を決定的に通す。"""
    spawned: list = []
    _route_to_control(monkeypatch, "plugin_error_then_die", spawned)
    session = PluginSession(_meta(tmp_path, "ctl", INDICATOR_OK), settings=plugin_settings)
    real_read = session._read_response

    def read_after_exit_for_load(timeout_sec, max_bytes):
        if session._reading_load_response:
            os.waitid(os.P_PID, session._proc.pid, os.WEXITED | os.WNOWAIT)
        return real_read(timeout_sec, max_bytes)

    monkeypatch.setattr(session, "_read_response", read_after_exit_for_load)
    with pytest.raises(SandboxError) as error:
        session.__enter__()
    assert error.value.code == session.error_code == "plugin_error"
    assert _gone(spawned[0].pid)


def test_graceful_close_of_a_worker_that_exits_before_the_parent_reads_is_not_crashed(
        monkeypatch, tmp_path, plugin_settings):
    """close op に応答して正常終了した worker を、親が遅れて死を先に観測しても
    crashed に汚染しない (死後の close 応答行は読まず、終了状態だけで判断する)。"""
    session = PluginSession(_meta(tmp_path, "gc", INDICATOR_OK), settings=plugin_settings)
    session.__enter__()
    session.call({"df": _df(), "params": {}})
    real_read = session._read_response

    def read_after_exit_for_close(timeout_sec, max_bytes):
        if session._closing:
            os.waitid(os.P_PID, session._proc.pid, os.WEXITED | os.WNOWAIT)
        return real_read(timeout_sec, max_bytes)

    monkeypatch.setattr(session, "_read_response", read_after_exit_for_close)
    session.close()
    assert session.worker_returncode == 0
    assert session.error_code != "crashed"


def test_transient_admission_failure_is_not_process_cached(monkeypatch):
    """一時要因 (timeout 等) の selftest 失敗は process 内に恒久 cache しない。間隔を
    超えたら再判定できる (service 再起動なしで環境の回復に追従する)。"""
    calls = {"n": 0}

    def selftest():
        calls["n"] += 1
        return runtime_fingerprint.SelftestOutcome(ok=False, detail="timeout")

    clk = {"t": 0.0}
    monkeypatch.setattr(sandbox, "_RUNTIME_ADMISSION",
                        runtime_fingerprint.RuntimeAdmission(
                            selftest=selftest, supported=(),
                            retry_interval_sec=60.0, clock=lambda: clk["t"]))
    monkeypatch.setattr(sandbox, "_ADMISSION_RESULT", None)
    r1 = sandbox.runtime_admission()
    assert r1.admitted is False
    assert r1.reason == "runtime_fingerprint_selftest_failed"
    assert sandbox._ADMISSION_RESULT is None   # 恒久 cache しない
    clk["t"] = 61.0
    sandbox.runtime_admission()
    assert calls["n"] == 2   # 間隔を超えたら再試行


def test_permanent_admission_failure_is_process_cached(monkeypatch):
    """恒久要因 (計算不一致等) の失敗は process 内に cache し、再試行しない。"""
    calls = {"n": 0}

    def selftest():
        calls["n"] += 1
        return runtime_fingerprint.SelftestOutcome(ok=False, detail="workload_failed")

    monkeypatch.setattr(sandbox, "_RUNTIME_ADMISSION",
                        runtime_fingerprint.RuntimeAdmission(selftest=selftest, supported=()))
    monkeypatch.setattr(sandbox, "_ADMISSION_RESULT", None)
    sandbox.runtime_admission()
    assert sandbox._ADMISSION_RESULT is not None
    sandbox.runtime_admission()
    assert calls["n"] == 1


def test_sigsys_before_load_logs_the_fixed_kernel_log_hint(monkeypatch, tmp_path,
                                                          plugin_settings, caplog):
    caplog.set_level(logging.INFO, logger="agentic_fx.plugin.sandbox")
    spawned: list = []
    _route_to_control(monkeypatch, "sigsys_before_ready", spawned)
    with pytest.raises(SandboxError):
        PluginSession(_meta(tmp_path, "ctl", INDICATOR_OK),
                      settings=plugin_settings).__enter__()
    assert seccomp.sigsys_diagnostic_line(spawned[0].pid) in _diagnostics(caplog)


# --- 単一 deadline と load 前後の分類 ---------------------------------------

def test_sleeping_before_sandbox_ready_is_a_startup_timeout_without_load(
        monkeypatch, tmp_path, plugin_settings, caplog):
    caplog.set_level(logging.INFO, logger="agentic_fx.plugin.sandbox")
    monkeypatch.setattr(sandbox, "_STARTUP_TIMEOUT_SEC", 1.5)
    spawned: list = []
    _route_to_control(monkeypatch, "sleep_before_ready", spawned)
    session = PluginSession(_meta(tmp_path, "ctl", INDICATOR_OK), settings=plugin_settings)
    started = time.monotonic()
    with pytest.raises(SandboxError) as error:
        session.__enter__()
    assert time.monotonic() - started < 1.5 + 2.0
    assert (error.value.code, error.value.sandbox_reason) == (
        "sandbox_unavailable", "sandbox_startup_timeout")
    assert "CONTROL_LOAD_RECEIVED" not in _diagnostics(caplog)


def test_sleeping_after_load_shares_the_deadline_and_is_a_timeout(
        monkeypatch, tmp_path, plugin_settings, caplog):
    caplog.set_level(logging.INFO, logger="agentic_fx.plugin.sandbox")
    monkeypatch.setattr(sandbox, "_STARTUP_TIMEOUT_SEC", 4.0)
    spawned: list = []
    _route_to_control(monkeypatch, "sleep_after_load", spawned)
    session = PluginSession(_meta(tmp_path, "ctl", INDICATOR_OK), settings=plugin_settings)
    started = time.monotonic()
    with pytest.raises(SandboxError) as error:
        session.__enter__()
    assert time.monotonic() - started < 4.0 + 2.0
    assert error.value.code == "timeout"
    assert error.value.sandbox_reason is None
    assert "CONTROL_LOAD_RECEIVED" in _diagnostics(caplog)


def test_sigsys_after_load_is_an_unattributed_crash(monkeypatch, tmp_path, plugin_settings):
    spawned: list = []
    _route_to_control(monkeypatch, "sigsys_after_load", spawned)
    session = PluginSession(_meta(tmp_path, "ctl", INDICATOR_OK), settings=plugin_settings)
    with pytest.raises(SandboxError) as error:
        session.__enter__()
    assert (error.value.code, error.value.sandbox_reason) == ("crashed", "sigsys_unattributed")


# --- worker bootstrap の失敗 ------------------------------------------------

@pytest.mark.parametrize("argv_for", [
    pytest.param(lambda a: [a[0], "-B", "-m", "agentic_fx.plugin.no_such_worker", a[-1]],
                 id="module-missing"),
    pytest.param(lambda a: [a[0], "-B", "-c", "def broken(:\n", a[-1]], id="syntax-error"),
    pytest.param(lambda a: [a[0], "-B", "-c",
                            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)", a[-1]],
                 id="signal-before-first-line"),
    pytest.param(lambda a: [a[0], "-B", "-c", "raise SystemExit(3)", a[-1]],
                 id="exit-before-first-line"),
    pytest.param(lambda a: ["/nonexistent/python3", *a[1:]], id="interpreter-missing"),
])
def test_worker_that_never_reports_its_sandbox_is_bootstrap_failed(
        monkeypatch, tmp_path, plugin_settings, argv_for):
    spawned: list = []
    _route_popen(monkeypatch, argv_for, spawned)
    session = PluginSession(_meta(tmp_path, "boot", INDICATOR_OK), settings=plugin_settings)
    with pytest.raises(SandboxError) as error:
        session.__enter__()
    assert (error.value.code, error.value.sandbox_reason) == (
        "sandbox_unavailable", "worker_bootstrap_failed")
    assert session._proc is None


# --- load 後の SIGSYS: 禁止 syscall と外部 signal を区別しない --------------

_GETCWD_PLUGIN = textwrap.dedent("""
    import pandas as pd

    def compute(df, params):
        pd.io.common.os.getcwd()
        return {"x": 1.0}
    """)

_SPIN_PLUGIN = textwrap.dedent("""
    def compute(df, params):
        n = 0
        while True:
            n += 1
    """)


def test_forbidden_syscall_after_load_is_an_unattributed_crash(tmp_path, plugin_settings,
                                                               caplog):
    caplog.set_level(logging.INFO, logger="agentic_fx.plugin.sandbox")
    meta = _meta(tmp_path, "cwd", _GETCWD_PLUGIN)
    session = PluginSession(meta, settings=plugin_settings)
    with pytest.raises(SandboxError) as error:
        with session:
            session.call({"df": _df(), "params": {}})
    assert (error.value.code, error.value.sandbox_reason) == ("crashed", "sigsys_unattributed")
    assert session.worker_signal == signal.SIGSYS
    assert session.parent_kill_sent is False
    assert seccomp.sigsys_diagnostic_line(session.pid) in _diagnostics(caplog)
    assert "sandbox_reason=sigsys_unattributed" in _diagnostics(caplog)


@pytest.mark.parametrize("kernel_log", [seccomp.KERNEL_LOG_READABLE,
                                        seccomp.KERNEL_LOG_EMPTY,
                                        seccomp.KERNEL_LOG_PERMISSION])
def test_external_sigsys_after_load_is_the_same_crash_whatever_the_kernel_log(
        monkeypatch, tmp_path, plugin_settings, kernel_log):
    # 分類は wait4 の status・親 kill・load の印だけで決まり、kernel log を読まない
    monkeypatch.setattr(seccomp, "check_kernel_log", lambda **_k: kernel_log)
    monkeypatch.setattr(sandbox.seccomp, "check_kernel_log", lambda **_k: kernel_log)
    settings = plugin_settings.model_copy(update={"sandbox_timeout_sec": 20.0})
    session = PluginSession(_meta(tmp_path, "spin", _SPIN_PLUGIN), settings=settings)
    with pytest.raises(SandboxError) as error:
        with session:
            timer = threading.Timer(0.5, os.kill, (session.pid, signal.SIGSYS))
            timer.start()
            try:
                session.call({"df": _df(), "params": {}})
            finally:
                timer.cancel()
    assert (error.value.code, error.value.sandbox_reason) == ("crashed", "sigsys_unattributed")
    assert str(error.value) == sandbox.SIGSYS_CRASH_MESSAGE
    assert session.parent_kill_sent is False


# --- Popen 直前の継承 filter 検査 ------------------------------------------

_FILTERED_PARENT = textwrap.dedent("""
    import json, sys
    from pathlib import Path
    from agentic_fx.config import load_settings
    from agentic_fx.core import seccomp
    from agentic_fx.plugin import sandbox
    from agentic_fx.plugin.loader import PluginMeta, content_hash

    seccomp.install_program([(seccomp.BPF_RET_K, 0, 0, seccomp.RET_ALLOW)], log=False)
    calls = []
    sandbox.subprocess.Popen = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(
        AssertionError("Popen must not be called"))
    d = Path(sys.argv[1])
    meta = PluginMeta(name="p", kind="indicator", path=d, params={}, timeframe=None,
                      pairs=(), max_bars=200, content_hash=content_hash(d))
    settings = load_settings(Path(sys.argv[2])).plugin
    try:
        sandbox.PluginSession(meta, settings=settings).__enter__()
        out = {"code": None}
    except sandbox.SandboxError as exc:
        out = {"code": exc.code, "reason": exc.sandbox_reason}
    out["popen_calls"] = len(calls)
    print(json.dumps(out))
    """)


def test_parent_with_a_real_inherited_filter_never_calls_popen(tmp_path):
    meta = _meta(tmp_path, "p", INDICATOR_OK)
    done = subprocess.run([sys.executable, "-c", _FILTERED_PARENT, str(meta.path),
                           str(EXAMPLE)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.splitlines()[-1]) == {
        "code": "sandbox_unavailable", "reason": "inherited_seccomp_filter",
        "popen_calls": 0}


@pytest.mark.parametrize("status", [None, b"Name:\tpython\nSeccomp:\t0\n",
                                    b"Seccomp_filters:\t2\n"])
def test_missing_unreadable_or_nonzero_thread_filter_field_blocks_popen(
        monkeypatch, tmp_path, plugin_settings, status):
    path = tmp_path / "status"
    if status is not None:
        path.write_bytes(status)
    monkeypatch.setattr(sandbox, "_THREAD_STATUS_PATH", str(path))
    calls: list = []
    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *a, **k: calls.append(a))
    session = PluginSession(_meta(tmp_path, "p", INDICATOR_OK), settings=plugin_settings)
    with pytest.raises(SandboxError) as error:
        session.__enter__()
    assert (error.value.code, error.value.sandbox_reason) == (
        "sandbox_unavailable", "inherited_seccomp_filter")
    assert calls == []


def test_current_thread_status_is_read_right_before_popen(monkeypatch, tmp_path,
                                                          plugin_settings):
    order: list = []
    real = sandbox.inherited_filter_preflight
    monkeypatch.setattr(sandbox, "inherited_filter_preflight",
                        lambda: order.append("preflight") or real())
    monkeypatch.setattr(sandbox.subprocess, "Popen",
                        lambda *a, **k: order.append("popen") or (_ for _ in ()).throw(
                            OSError("stop")))
    with pytest.raises(SandboxError):
        PluginSession(_meta(tmp_path, "p", INDICATOR_OK),
                      settings=plugin_settings).__enter__()
    assert order == ["preflight", "popen"]


# --- 共通 admission gate --------------------------------------------------

@pytest.fixture
def failing_admission(monkeypatch):
    """集合外 fingerprint として、代表自己試験が失敗する process を作る。"""
    runs: list = []

    def selftest():
        runs.append(1)
        return runtime_fingerprint.SelftestOutcome(False, "workload_failed")

    monkeypatch.setattr(sandbox, "_RUNTIME_ADMISSION",
                        runtime_fingerprint.RuntimeAdmission(selftest=selftest, supported=()))
    monkeypatch.setattr(sandbox, "_ADMISSION_RESULT", None)
    popen_calls: list = []
    real = subprocess.Popen

    def popen(argv, **kwargs):
        if "agentic_fx.plugin.worker" in argv:
            popen_calls.append(argv)
        return real(argv, **kwargs)

    monkeypatch.setattr(sandbox.subprocess, "Popen", popen)
    return runs, popen_calls


def test_out_of_set_fingerprint_selftest_runs_once_and_fails_every_session(
        tmp_path, plugin_settings, failing_admission):
    runs, popen_calls = failing_admission
    assert any("unavailable sandbox_reason=runtime_fingerprint_selftest_failed" in line
               for line in sandbox.startup_diagnostics())
    for i in range(3):
        with pytest.raises(SandboxError) as error:
            run_plugin(_meta(tmp_path, f"p{i}", INDICATOR_OK),
                       {"df": _df(), "params": {}}, settings=plugin_settings)
        assert (error.value.code, error.value.sandbox_reason) == (
            "sandbox_unavailable", "runtime_fingerprint_selftest_failed")
    assert runs == [1]
    assert popen_calls == []


def test_out_of_set_fingerprint_with_a_passing_selftest_is_admitted_with_a_warning(
        monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="agentic_fx.plugin.sandbox")
    runs: list = []
    monkeypatch.setattr(sandbox, "_RUNTIME_ADMISSION", runtime_fingerprint.RuntimeAdmission(
        selftest=lambda: runs.append(1) or runtime_fingerprint.SelftestOutcome(True, "ok"),
        supported=()))
    monkeypatch.setattr(sandbox, "_ADMISSION_RESULT", None)
    first = sandbox.runtime_admission()
    assert first.admitted and not first.supported
    assert sandbox.runtime_admission() is first
    assert runs == [1]
    assert runtime_fingerprint.OUT_OF_SET_WARNING in _diagnostics(caplog)


def test_admission_selftest_harness_does_not_recurse_into_plugin_session(monkeypatch):
    monkeypatch.setattr(PluginSession, "__enter__",
                        lambda self: pytest.fail("selftest must not use PluginSession"))
    outcome = sandbox._run_selftest()
    assert outcome.ok, outcome.detail


@pytest.mark.parametrize("probe,reason", [
    ("arch", "arch_unsupported"), ("landlock0", "landlock_unavailable"),
    ("landlock2", "landlock_abi_too_old"), ("seccomp", "seccomp_unavailable"),
    ("zoneinfo", "missing_system_dir:zoneinfo"),
    ("seccompfield", "seccomp_status_field_unavailable"),
])
def test_host_without_the_required_isolation_never_starts_a_worker(
        monkeypatch, tmp_path, plugin_settings, probe, reason):
    if probe == "arch":
        monkeypatch.setattr(sandbox.platform, "machine", lambda: "aarch64")
    elif probe == "landlock0":
        monkeypatch.setattr(sandbox.landlock, "landlock_abi", lambda: 0)
    elif probe == "landlock2":
        monkeypatch.setattr(sandbox.landlock, "landlock_abi", lambda: 2)
    elif probe == "zoneinfo":
        # zoneinfo の無い minimal 環境。worker が fd_open_failed になる前に検知
        monkeypatch.setattr(sandbox.landlock, "missing_required_system_dirs",
                            lambda: [Path("/usr/share/zoneinfo")])
    elif probe == "seccompfield":
        # Seccomp_filters 欄の無い kernel。全 session が inherited_seccomp_filter に
        # なる前に検知
        monkeypatch.setattr(sandbox.seccomp, "seccomp_filters_field", lambda data: None)
    else:
        monkeypatch.setattr(sandbox.seccomp, "probe_support", lambda: seccomp.SeccompSupport(
            "seccomp_unavailable", False, False, False, False))
    monkeypatch.setattr(sandbox, "_ADMISSION_RESULT", None)
    calls: list = []
    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *a, **k: calls.append(a))
    with pytest.raises(SandboxError) as error:
        PluginSession(_meta(tmp_path, "p", INDICATOR_OK),
                      settings=plugin_settings).__enter__()
    assert (error.value.code, error.value.sandbox_reason) == ("sandbox_unavailable", reason)
    assert calls == []


# --- 檻の中から何に届くか (ライブラリ属性経由で os に届く plugin) ----------

_PROBE_PLUGIN = textwrap.dedent("""
    import pandas as pd

    _os = pd.io.common.os
    _open = _os.__dict__["open"]


    def _code(fn):
        try:
            fn()
        except PermissionError:
            return 1.0
        except OSError as exc:
            return 100.0 + exc.errno
        return 0.0


    def _open_close(path):
        def run():
            _os.close(_open(path, _os.O_RDONLY))
        return run


    def _listdir(path):
        def run():
            _os.listdir(path)
        return run


    def _socket():
        imp = _os.sys.modules["builtins"].__dict__["__import__"]
        s = imp("_socket")
        sock = s.socket(s.AF_INET, s.SOCK_STREAM)
        sock.close()


    def _fork():
        if _os.fork() == 0:
            _os._exit(0)


    def _exec():
        _os.execv("/bin/true", ["true"])


    def compute(df, params):
        p = params["paths"]
        return {
            "own_plugin": _code(_open_close(p["own_plugin"])),
            "own_test_plugin": _code(_open_close(p["own_test_plugin"])),
            "own_dir_listing": _code(_listdir(p["own_dir"])),
            "sibling_plugin": _code(_open_close(p["sibling_plugin"])),
            "repo_root_listing": _code(_listdir(p["repo_root"])),
            "home_config_listing": _code(_listdir(p["home_config"])),
            "etc_passwd": _code(_open_close("/etc/passwd")),
            "create_file": _code(lambda: _os.close(_open(p["new_file"],
                                                        _os.O_WRONLY | _os.O_CREAT))),
            "tcp_socket": _code(_socket),
            "fork": _code(_fork),
            "exec": _code(_exec),
        }
    """)


def test_plugin_reaching_os_through_a_library_attribute_stays_in_the_cage(
        tmp_path, plugin_settings):
    """AST 検査をライブラリの属性経由で回避した plugin が、檻の中で何に届くか。"""
    from agentic_fx.core import landlock

    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (sibling / "plugin.py").write_text("x = 1\n")
    meta = _meta(tmp_path, "probe", _PROBE_PLUGIN)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.setblocking(False)
    paths = {"own_plugin": str(meta.path / "plugin.py"),
             "own_test_plugin": str(meta.path / "test_plugin.py"),
             "own_dir": str(meta.path), "sibling_plugin": str(sibling / "plugin.py"),
             "repo_root": str(landlock.default_repo_root()),
             "home_config": str(Path.home() / ".config"),
             "new_file": str(tmp_path / "created")}
    try:
        out = run_plugin(meta, {"df": _df(), "params": {"paths": paths}},
                         settings=plugin_settings)
        with pytest.raises(BlockingIOError):
            server.accept()
    finally:
        server.close()
    assert out["own_plugin"] == 0.0           # 選んだ plugin.py だけは読める
    for key in ("own_test_plugin", "own_dir_listing", "sibling_plugin",
                "repo_root_listing", "home_config_listing", "etc_passwd",
                "create_file", "tcp_socket", "fork", "exec"):
        assert out[key] == 1.0, (key, out)
    assert not (tmp_path / "created").exists()


# --- 隔離段の失敗が親で読めること -----------------------------------------

def test_isolation_failure_reported_by_a_worker_that_already_exited_keeps_its_reason(
        monkeypatch, tmp_path, plugin_settings, caplog):
    """隔離段の失敗応答は書いた直後に `_exit(0)` する。死を先に観測しても、固定 reason
    は例外と技術ログの両方に残る。"""
    caplog.set_level(logging.INFO, logger="agentic_fx.plugin.sandbox")
    settings = plugin_settings.model_copy(update={"sandbox_nofile": 10 ** 9})
    session = PluginSession(_meta(tmp_path, "rl", INDICATOR_OK), settings=settings)
    real_read = session._read_response

    def read_after_exit(timeout_sec, max_bytes):
        os.waitid(os.P_PID, session._proc.pid, os.WEXITED | os.WNOWAIT)
        return real_read(timeout_sec, max_bytes)

    monkeypatch.setattr(session, "_read_response", read_after_exit)
    with pytest.raises(SandboxError) as error:
        session.__enter__()
    assert error.value.sandbox_reason == "sandbox_setup_failed:rlimit_failed"
    log = _diagnostics(caplog)
    assert "code=sandbox_unavailable" in log
    assert "sandbox_reason=sandbox_setup_failed:rlimit_failed" in log
    assert "rlimit_failed" in log and "Traceback" in log


def test_plugin_grown_past_the_limit_after_sandbox_ready_is_file_too_large(
        monkeypatch, tmp_path, plugin_settings):
    """sandbox_ready の後・load の前に同じ inode を上限超過へ伸ばしても、worker の
    有界読みが 1 秒以内に `plugin_error / file_too_large` にする。"""
    from agentic_fx.core.plugin_files import MAX_PLUGIN_FILE_BYTES

    meta = _meta(tmp_path, "grow", INDICATOR_OK)
    real_check = sandbox.check_worker_tasks
    marks: dict = {}

    def check_then_grow(pid, **kwargs):
        reason = real_check(pid, **kwargs)
        with open(meta.path / "plugin.py", "ab") as f:
            f.write(b"#" * MAX_PLUGIN_FILE_BYTES)
        marks["t"] = time.monotonic()
        return reason

    monkeypatch.setattr(sandbox, "check_worker_tasks", check_then_grow)
    with pytest.raises(SandboxError) as error:
        PluginSession(meta, settings=plugin_settings).__enter__()
    assert time.monotonic() - marks["t"] < 1.0
    assert error.value.code == "plugin_error"
    assert "file_too_large" in str(error.value)


# --- bytecode cache を書かない -------------------------------------------------

def _bytecode_files(*roots: Path) -> set[Path]:
    return {p for root in roots for p in root.rglob("*.pyc")}


def test_real_worker_writes_no_bytecode_cache_anywhere(tmp_path, monkeypatch,
                                                     plugin_settings):
    import numpy
    import pandas
    roots = (Path(sandbox._SRC_DIR) / "agentic_fx", Path(numpy.__file__).parent,
             Path(pandas.__file__).parent)
    meta = _meta(tmp_path, "nocache", INDICATOR_OK)
    spawned: list = []
    real = subprocess.Popen

    def popen(argv, **kwargs):
        spawned.append(real(argv, **kwargs))
        return spawned[-1]

    monkeypatch.setattr(sandbox.subprocess, "Popen", popen)
    before = _bytecode_files(*roots)
    with PluginSession(meta, settings=plugin_settings) as session:
        (proc,) = spawned
        assert proc.args[1] == "-B"
        assert session.call({"df": _df(), "params": {}}) == {"x": 1.0}
    assert session.worker_returncode == 0
    # 起動した worker が import しても、package・venv・候補 dir に cache は増えない
    assert _bytecode_files(*roots) == before
    assert not list(meta.path.rglob("__pycache__"))


# --- 親から worker への書き込みも deadline の中にある --------------------------

def _bounded(fn, limit: float, spawned: list) -> dict:
    """`fn` を別 thread で走らせ、`limit` 秒で終わらなければ失敗にする。

    書き込みが止まったまま pytest 全体を止めないため、時間切れのときは起こした
    worker を殺して詰まった write を解いてから失敗させる。"""
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(limit)
    if thread.is_alive():
        for proc in spawned:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        thread.join(10)
        pytest.fail(f"session did not finish within {limit}s (blocked on a write?)")
    return box


def _large_resolved_set(root: Path):
    """loader と resolve の上限に収まる正当な indicator 依存で、pipe 容量 (64 KiB)
    を超える handshake を作る。"""
    from agentic_fx.plugin.loader import MAX_INDICATOR_DEPS, _check_json_safe
    from agentic_fx.plugin.resolve import (MAX_HANDSHAKE_BYTES, ResolvedIndicator,
                                           ResolvedIndicatorSet, freeze_params)

    params = {"w": [0.5] * 2000}
    assert _check_json_safe(params, "params") is None
    items = []
    for i in range(MAX_INDICATOR_DEPS):
        d = _meta(root, f"ind{i}", INDICATOR_OK).path
        items.append(ResolvedIndicator(
            alias=f"ind{i}", plugin_name=d.name, plugin_py=d / "plugin.py",
            content_hash=content_hash(d), params=freeze_params(params), max_bars=200,
            outputs=("x",), pinned=True))
    resolved = ResolvedIndicatorSet(inventory_root=root.resolve(), items=tuple(items),
                                    all_pinned=True)
    canonical = json.dumps(resolved.handshake_items(), separators=(",", ":"),
                           sort_keys=True, ensure_ascii=False, allow_nan=False)
    assert len(canonical.encode()) <= MAX_HANDSHAKE_BYTES
    assert len(json.dumps(resolved.handshake_items()).encode()) > 65536
    return resolved


def test_handshake_larger_than_the_pipe_to_a_worker_that_never_reads_hits_the_startup_deadline(
        monkeypatch, tmp_path, plugin_settings):
    monkeypatch.setattr(sandbox, "_STARTUP_TIMEOUT_SEC", 2.0)
    spawned: list = []
    _route_popen(monkeypatch, lambda a: [a[0], "-c", "import time; time.sleep(60)"], spawned)
    root = tmp_path / "plugins"
    root.mkdir()
    resolved = _large_resolved_set(root)
    meta = _meta(tmp_path, "strat", "def decide(df, params):\n    return None\n",
                 kind="strategy")

    def enter():
        started = time.monotonic()
        try:
            PluginSession(meta, settings=plugin_settings, resolved=resolved).__enter__()
        finally:
            elapsed = time.monotonic() - started
        return elapsed

    box = _bounded(enter, 2.0 + 8.0, spawned)
    error = box.get("error")
    assert isinstance(error, SandboxError), box
    assert (error.code, error.sandbox_reason) == ("sandbox_unavailable",
                                                  "sandbox_startup_timeout")
    (proc,) = spawned
    assert _gone(proc.pid)


def test_call_larger_than_the_pipe_to_a_worker_that_stopped_reading_is_a_timeout(
        monkeypatch, tmp_path, plugin_settings):
    settings = plugin_settings.model_copy(update={"sandbox_timeout_sec": 1.5})
    spawned: list = []
    # 隔離段と load は正しく通り、plugin_ready の後は stdin を読まずに眠る
    _route_to_control(monkeypatch, "stops_reading_after_load", spawned)
    df = _df(4000)
    assert len(json.dumps(sandbox._df_to_wire(df)).encode()) > 2 * 65536
    meta = _meta(tmp_path, "ctl", INDICATOR_OK)

    def run():
        session = PluginSession(meta, settings=settings)
        with session:
            started = time.monotonic()
            try:
                session.call({"df": df, "params": {}})
            finally:
                run.elapsed = time.monotonic() - started
        return session

    box = _bounded(run, 1.5 + 15.0, spawned)
    error = box.get("error")
    assert isinstance(error, SandboxError), box
    assert error.code == "timeout"
    assert run.elapsed < 1.5 + 3.0
    (proc,) = spawned
    assert _gone(proc.pid)


def test_call_shares_one_deadline_between_the_write_and_the_read(
        monkeypatch, tmp_path, plugin_settings):
    """要求の書き込みで時間を使ったら、応答の読み取りは残り時間だけ待つ。call 全体の
    上限は `sandbox_timeout_sec`。"""
    timeout = 2.0
    settings = plugin_settings.model_copy(update={"sandbox_timeout_sec": timeout})
    spawned: list = []
    # 要求を約 160 KiB/s でしか読まないので、書き込みだけで 1 秒あまりかかる
    _route_to_control(monkeypatch, "reads_slowly_after_load", spawned)
    df = _df(4000)
    meta = _meta(tmp_path, "ctl", INDICATOR_OK)

    def run():
        session = PluginSession(meta, settings=settings)
        with session:
            started = time.monotonic()
            try:
                session.call({"df": df, "params": {}})
            finally:
                run.elapsed = time.monotonic() - started
        return session

    box = _bounded(run, timeout * 2 + 15.0, spawned)
    error = box.get("error")
    assert isinstance(error, SandboxError), box
    assert error.code == "timeout"
    assert run.elapsed < timeout + 0.7
    (proc,) = spawned
    assert _gone(proc.pid)


# --- 親の起動前の判定: 読めない status、fingerprint の失敗、ABI の下限 ---------------

def test_thread_status_that_cannot_be_read_blocks_popen(monkeypatch, tmp_path,
                                                         plugin_settings):
    # 存在はするが読めない (dir を open すると kernel が EISDIR を返す) も field 不在と同じ
    unreadable = tmp_path / "status_dir"
    unreadable.mkdir()
    monkeypatch.setattr(sandbox, "_THREAD_STATUS_PATH", str(unreadable))
    calls: list = []
    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *a, **k: calls.append(a))
    session = PluginSession(_meta(tmp_path, "p", INDICATOR_OK), settings=plugin_settings)
    with pytest.raises(SandboxError) as error:
        session.__enter__()
    assert (error.value.code, error.value.sandbox_reason) == (
        "sandbox_unavailable", "inherited_seccomp_filter")
    assert calls == []


def test_fingerprint_that_cannot_be_computed_fails_closed_without_a_worker(
        monkeypatch, tmp_path, plugin_settings):
    def broken(**_k):
        raise OSError("RECORD unreadable")

    monkeypatch.setattr(sandbox.runtime_fingerprint, "compute_fingerprint", broken)
    monkeypatch.setattr(sandbox, "_ADMISSION_RESULT", None)
    result = sandbox.runtime_admission()
    assert (result.admitted, result.reason) == (False, "runtime_fingerprint_selftest_failed")
    monkeypatch.setattr(sandbox, "_ADMISSION_RESULT", None)
    calls: list = []
    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *a, **k: calls.append(a))
    with pytest.raises(SandboxError) as error:
        PluginSession(_meta(tmp_path, "p", INDICATOR_OK),
                      settings=plugin_settings).__enter__()
    assert (error.value.code, error.value.sandbox_reason) == (
        "sandbox_unavailable", "runtime_fingerprint_selftest_failed")
    assert calls == []


def test_landlock_abi_exactly_at_the_minimum_is_accepted(monkeypatch):
    # 下限 ABI 3 ちょうどは隔離できる host (3 未満の拒否は上の landlock2 が見る)
    monkeypatch.setattr(sandbox.landlock, "landlock_abi", lambda: 3)
    assert sandbox.host_preflight() == (None, 3)


def test_refusal_before_popen_closes_the_stderr_file_and_logs_the_reason(
        monkeypatch, tmp_path, plugin_settings, caplog):
    caplog.set_level(logging.INFO, logger="agentic_fx.plugin.sandbox")
    monkeypatch.setattr(sandbox, "_THREAD_STATUS_PATH", str(tmp_path / "missing"))
    monkeypatch.setattr(sandbox.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("Popen must not be called"))
    opened: list = []
    real_tempfile = sandbox.tempfile.TemporaryFile

    def tracked(*a, **k):
        opened.append(real_tempfile(*a, **k))
        return opened[-1]

    monkeypatch.setattr(sandbox.tempfile, "TemporaryFile", tracked)
    session = PluginSession(_meta(tmp_path, "p", INDICATOR_OK), settings=plugin_settings)
    with pytest.raises(SandboxError):
        session.__enter__()
    (stderr_file,) = opened
    assert stderr_file.closed
    assert ("plugin_worker_diagnostic plugin=p code=sandbox_unavailable"
            in _diagnostics(caplog))
    assert "sandbox_reason=inherited_seccomp_filter" in _diagnostics(caplog)


# --- load の後に socket 系 module を塞ぐ配線 -----------------------------------

_POISON_PROBE_PLUGIN = textwrap.dedent("""
    import pandas as pd

    _imp = pd.io.common.os.sys.modules["builtins"].__dict__["__import__"]


    def _blocked(name):
        try:
            _imp(name, fromlist=["_"])
        except ImportError:
            return 1.0
        return 0.0


    def compute(df, params):
        return {"socket": _blocked("socket"), "urllib_request": _blocked("urllib.request"),
                "http_client": _blocked("http.client")}
    """)


def test_real_worker_poisons_the_network_modules_before_loading_the_plugin(
        tmp_path, plugin_settings):
    meta = _meta(tmp_path, "poison", _POISON_PROBE_PLUGIN)
    out = run_plugin(meta, {"df": _df(), "params": {}}, settings=plugin_settings)
    assert out == {"socket": 1.0, "urllib_request": 1.0, "http_client": 1.0}
