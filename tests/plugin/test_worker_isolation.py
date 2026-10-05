"""worker の隔離段 (`worker_isolation.prepare_isolation` / `isolate`) を実子 process で確かめる。

protocol は通さない。子が隔離段の関数を直接呼び、結果を退避した protocol fd に書く。
隔離は pytest の process に掛けない (不可逆)。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from agentic_fx.core import landlock
from agentic_fx.core import runtime_fingerprint as F
from agentic_fx.plugin import worker_isolation as wi

from ._worker_isolation_support import (
    NONCE,
    SIGSYS,
    cfg_for,
    copy_plugin,
    failure_line,
    handshake,
    needs_sandbox,
    proc_task_status,
    run_child,
)


# --- 純粋な契約 (隔離しない) ------------------------------------------------------

def test_module_import_does_not_load_numpy_or_pandas():
    code = ("import sys; import agentic_fx.plugin.worker_isolation; "
            "print(sorted(m for m in ('numpy', 'pandas', 'yaml') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-P", "-B", "-c", code], capture_output=True,
                         text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_max_file_bytes_is_the_shared_constant_of_core_plugin_files():
    from agentic_fx.core import plugin_files
    from agentic_fx.plugin import loader
    assert wi.MAX_PLUGIN_FILE_BYTES == plugin_files.MAX_PLUGIN_FILE_BYTES
    assert loader._MAX_FILE_BYTES == plugin_files.MAX_PLUGIN_FILE_BYTES


def test_worker_reasons_cover_the_landlock_reasons_and_the_spec_list():
    assert set(landlock.SANDBOX_REASONS) <= set(wi.WORKER_SANDBOX_REASONS)
    assert len(set(wi.WORKER_SANDBOX_REASONS)) == len(wi.WORKER_SANDBOX_REASONS) == 18


def test_attested_fields_are_in_the_spec_table_order():
    assert wi.ATTESTED_FIELDS == ("sandbox_profile_version", "landlock_fs_abi", "landlock_tsync",
                                  "seccomp", "keyring", "network", "scope", "nonce")


# --- 成功時の attestation と /proc ---------------------------------------------------

_PRE_THREAD = """
import threading
probe_go = threading.Event()
probe_result = {}
def _probe():
    probe_go.wait()
    try:
        os.close(os.open("/etc/passwd", os.O_RDONLY))
        probe_result["etc_passwd"] = "readable"
    except OSError as e:
        probe_result["etc_passwd"] = e.errno
probe_thread = threading.Thread(target=_probe, daemon=True)
probe_thread.start()
"""

_POST_THREAD = """
print("stray print must not reach the protocol fd")
sys.stdout.flush()
wait_parent()
probe_go.set()
probe_thread.join(10)
out["thread"] = probe_result
"""


@needs_sandbox
def test_isolation_succeeds_with_exact_attestation_and_filter_on_every_task(tmp_path):
    main = copy_plugin("rsi", tmp_path)
    seen = {}

    def interact(pid, first):
        seen["tasks"] = proc_task_status(pid)

    res = run_child(cfg_for(main, pre=_PRE_THREAD, post=_POST_THREAD), interact=interact)
    assert res.rc == 0, res.stderr
    ready = json.loads(res.phase("isolated")["line"])
    abi = landlock.landlock_abi()
    assert list(ready) == ["phase", "ok", "pid", *wi.ATTESTED_FIELDS]
    assert ready == {
        "phase": "sandbox_ready", "ok": True, "pid": res.pid,
        "sandbox_profile_version": "plugin-worker/6", "landlock_fs_abi": min(abi, 8),
        "landlock_tsync": "applied" if abi >= 8 else "single_task", "seccomp": "allow/6",
        "keyring": "anonymous", "network": "applied", "scope": "applied", "nonce": NONCE}
    # 隔離前から居た thread を含む全 task に NoNewPrivs 1 / Seccomp 2 / filter 1 本
    assert len(seen["tasks"]) >= 2
    assert set(seen["tasks"]) == {("1", "2", "1")}
    # 隔離前に作った thread も Landlock の domain に入っている (TSYNC)
    assert res.out["thread"] == {"etc_passwd": 13}
    # plugin の print は protocol fd に混ざらず stderr へ行く
    assert all(set(ln) <= {"phase", "line", "out"} for ln in res.lines)
    assert "stray print" in res.stderr


@needs_sandbox
def test_without_landlock_tsync_a_pre_isolation_thread_escapes_the_domain(tmp_path):
    # 対照: TSYNC flag を外すと、隔離前から居た thread は Landlock の domain に入らない
    if landlock.landlock_abi() < 8:
        pytest.skip("TSYNC needs ABI 8")
    main = copy_plugin("rsi", tmp_path)
    mid = "from agentic_fx.core import landlock\nlandlock.RESTRICT_SELF_TSYNC = 0\n"
    res = run_child(cfg_for(main, pre=_PRE_THREAD, mid=mid, post=_POST_THREAD),
                    interact=lambda pid, first: None)
    assert res.out["thread"] == {"etc_passwd": "readable"}


@needs_sandbox
@pytest.mark.parametrize("abi, network, scope", [(3, "unsupported", "unsupported"),
                                                 (5, "applied", "unsupported"),
                                                 (7, "applied", "applied")])
def test_forced_old_abi_uses_single_task_and_reports_its_attestation(tmp_path, abi, network, scope):
    if landlock.landlock_abi() < abi:
        pytest.skip("kernel ABI is lower than the forced branch")
    main = copy_plugin("rsi", tmp_path)
    mid = f"from agentic_fx.core import landlock\nlandlock.landlock_abi = lambda: {abi}\n"
    res = run_child(cfg_for(main, mid=mid))
    assert res.rc == 0, res.stderr
    ready = json.loads(res.phase("isolated")["line"])
    assert (ready["landlock_fs_abi"], ready["landlock_tsync"], ready["network"],
            ready["scope"]) == (abi, "single_task", network, scope)


# --- 失敗段の固定 reason ----------------------------------------------------------

def _fail_cases(tmp_path: Path) -> dict:
    main = copy_plugin("rsi", tmp_path / "p")
    not_leaf = tmp_path / "empty_leaf"
    not_leaf.mkdir()
    (not_leaf / "config.yaml").write_text("kind: indicator\n")
    sym = copy_plugin("rsi", tmp_path / "s")
    (sym / "config.yaml").unlink()
    (sym / "config.yaml").symlink_to(main / "config.yaml")
    purelib = str(Path(sysconfig.get_paths()["purelib"]).resolve())
    lowest_free_fd = ("_fd = os.open('/dev/null', os.O_RDONLY)\nos.close(_fd)\n"
                      "cfg['handshake']['nofile'] = _fd\n")
    return {
        "rlimit_failed": cfg_for(
            main, pre="import resource\nresource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))\n"),
        "inherited_seccomp_filter": cfg_for(
            main, mid="from agentic_fx.core import seccomp as S\n"
                      "S.install_program([(0x06, 0, 0, S.RET_ALLOW)], log=False)\n"),
        "landlock_abi_too_old": cfg_for(
            main, mid="from agentic_fx.core import landlock\nlandlock.landlock_abi = lambda: 2\n"),
        "landlock_multithreaded": cfg_for(
            main, mid="from agentic_fx.core import landlock\nlandlock.landlock_abi = lambda: 7\n"
                      "import threading\n_ev = threading.Event()\n"
                      "threading.Thread(target=_ev.wait, daemon=True).start()\n"),
        "allowlist_guarded": cfg_for(main, hs=handshake(main), main_dir="/tmp"),
        "allowlist_not_leaf": cfg_for(not_leaf, hs=handshake(main), main_dir=str(not_leaf)),
        "plugin_file_invalid": cfg_for(sym, hs=handshake(main), main_dir=str(sym)),
        "runtime_root_too_wide": cfg_for(main, home=purelib),
        "fd_open_failed": cfg_for(main, mid=lowest_free_fd),
        "seccomp_failed": cfg_for(
            main, mid="import dataclasses\nfrom agentic_fx.core import seccomp as S\n"
                      "S.ALLOW6_TABLE = dataclasses.replace(S.ALLOW6_TABLE, table_end=1)\n"),
        "isolation_unexpected_error": cfg_for(
            main, mid="from agentic_fx.core import landlock\n"
                      "def _boom(*a, **k):\n    raise RuntimeError('boom inside the stage')\n"
                      "landlock.build_allowlist = _boom\n"),
        "runtime_import_failed": cfg_for(main, mid="sys.modules['pandas'] = None\n"),
    }


_FAIL_REASONS = ["rlimit_failed", "inherited_seccomp_filter", "landlock_abi_too_old",
                 "landlock_multithreaded", "allowlist_guarded", "allowlist_not_leaf",
                 "plugin_file_invalid", "runtime_root_too_wide", "fd_open_failed",
                 "seccomp_failed", "isolation_unexpected_error", "runtime_import_failed"]


@needs_sandbox
@pytest.mark.parametrize("reason", _FAIL_REASONS)
def test_each_failing_stage_writes_its_prebuilt_line_and_a_traceback(tmp_path, reason):
    if reason in ("landlock_multithreaded",) and landlock.landlock_abi() < 7:
        pytest.skip("needs ABI 7+ to force the single-task branch")
    res = run_child(_fail_cases(tmp_path)[reason])
    assert res.rc == 0, res.stderr
    # protocol fd には事前生成の 1 行だけ。plugin を読む段 (isolated) へ進まない
    assert res.lines == [failure_line(reason, res.pid)]
    assert res.raw == (json.dumps(failure_line(reason, res.pid), separators=(",", ":"))
                       + "\n").encode()
    assert "Traceback (most recent call last)" in res.stderr
    assert f"plugin worker: sandbox setup failed reason={reason} step=" in res.stderr
    assert "runtime=[" in res.stderr


@needs_sandbox
def test_unreadable_proc_status_is_an_inherited_filter_refusal(tmp_path):
    # 同 uid では /proc/self/status を読めなくできないので、準備段の open だけを失敗させる。
    # 読めない status は filter 0 を確かめられないので、未知の失敗でなく継承 filter の拒否
    main = copy_plugin("rsi", tmp_path)
    pre = ("_real_open = os.open\n"
           "def _open(path, *a, **k):\n"
           "    if path == '/proc/self/status':\n"
           "        raise PermissionError(13, 'injected')\n"
           "    return _real_open(path, *a, **k)\n"
           "os.open = _open\n")
    res = run_child(cfg_for(main, pre=pre))
    assert res.rc == 0, res.stderr
    assert res.lines == [failure_line("inherited_seccomp_filter", res.pid)]
    assert "reason=inherited_seccomp_filter step=inherited_seccomp_filter" in res.stderr


@needs_sandbox
def test_memory_error_during_the_runtime_import_is_runtime_import_failed(tmp_path):
    main = copy_plugin("rsi", tmp_path)
    mid = ("class _Oom:\n"
           "    def find_spec(self, name, path=None, target=None):\n"
           "        if name == 'pandas':\n            raise MemoryError('injected')\n"
           "sys.meta_path.insert(0, _Oom())\n")
    res = run_child(cfg_for(main, mid=mid))
    assert res.lines == [failure_line("runtime_import_failed", res.pid)]
    assert "MemoryError" in res.stderr


@needs_sandbox
def test_foreign_exception_carrying_a_reason_attribute_is_still_unexpected(tmp_path):
    # reason 属性を持つだけの他所の例外を、隔離段の既知 reason と取り違えない
    main = copy_plugin("rsi", tmp_path)
    mid = ("from agentic_fx.core import landlock\n"
           "class _Foreign(Exception):\n    reason = 'allowlist_guarded'\n"
           "def _boom(*a, **k):\n    raise _Foreign('not from the stage')\n"
           "landlock.build_allowlist = _boom\n")
    res = run_child(cfg_for(main, mid=mid))
    assert res.lines == [failure_line("isolation_unexpected_error", res.pid)]


@needs_sandbox
def test_malformed_handshake_is_an_unexpected_isolation_error(tmp_path):
    main = copy_plugin("rsi", tmp_path)
    res = run_child(cfg_for(main, hs=handshake(main, attest_nonce="short")))
    assert res.lines == [failure_line("isolation_unexpected_error", res.pid)]


@needs_sandbox
def test_handshake_rlimits_are_applied_before_landlock(tmp_path):
    main = copy_plugin("rsi", tmp_path)
    post = ("import resource\n"
            "out['lim'] = {n: list(resource.getrlimit(getattr(resource, 'RLIMIT_' + n)))"
            " for n in ('CPU', 'CORE', 'AS', 'NOFILE', 'FSIZE', 'NPROC')}\n")
    # core の soft を hard まで戻してから渡し、isolate() 自身が 0 にすることを見る
    pre = ("import resource\n_h = resource.getrlimit(resource.RLIMIT_CORE)[1]\n"
           "resource.setrlimit(resource.RLIMIT_CORE, (_h, _h))\n")
    res = run_child(cfg_for(main, hs=handshake(main, cpu_sec=50, memory_mb=700, nofile=100,
                                               fsize_mb=3), pre=pre, post=post))
    mb = 1024 * 1024
    assert res.out["lim"] == {"CPU": [50, 50], "CORE": [0, 0], "AS": [700 * mb, 700 * mb],
                              "NOFILE": [100, 100], "FSIZE": [3 * mb, 3 * mb],
                              "NPROC": [512, 512]}


# --- RLIMIT_AS を振る ---------------------------------------------------------------

def _classify(res) -> tuple:
    if res.phase("isolated") is not None:
        return ("isolated",)
    if res.lines:
        assert len(res.lines) == 1, res.lines
        line = res.lines[0]
        assert line == failure_line(line.get("reason"), res.pid)
        return ("line", line["reason"])
    return ("native", res.rc)


@needs_sandbox
def test_address_space_sweep_never_looks_like_a_plugin_failure_and_is_stable(tmp_path):
    main = copy_plugin("rsi", tmp_path)
    seen = {}
    for mb in (1, 2, 4, 8, 16, 32, 48, 64, 96, 128, 256, 512):
        outcomes = [_classify(run_child(cfg_for(main, hs=handshake(main, memory_mb=mb))))
                    for _ in range(2)]
        assert outcomes[0] == outcomes[1], (mb, outcomes)
        seen[mb] = outcomes[0]
    for mb, o in seen.items():
        if o[0] == "line":
            assert o[1] in wi.WORKER_SANDBOX_REASONS, (mb, o)
        if o[0] == "native":
            assert o[1] != -SIGSYS, (mb, o)
    assert seen[512] == ("isolated",)
    # Python に制御が戻る帯は固定 reason になる
    assert any(o[0] == "line" for o in seen.values()), seen


# --- 匿名 session keyring --------------------------------------------------------

@needs_sandbox
def test_joined_session_keyring_is_new_and_differs_from_the_parent(tmp_path):
    main = copy_plugin("rsi", tmp_path)
    post = ("before = wi.session_keyring_id()\n"
            "joined = wi.join_anonymous_session_keyring()\n"
            "out['ids'] = [before, joined, wi.session_keyring_id()]\n")
    res = run_child(cfg_for(main, isolate=False, post=post))
    before, joined, after = res.out["ids"]
    assert joined == after
    assert before != after
    assert wi.session_keyring_id() == before   # 親 (pytest) の keyring は変わらない


@needs_sandbox
def test_isolate_switches_the_session_keyring(tmp_path):
    # seccomp だけを外した対照子で、isolate() の後の session keyring が前と違うことを見る
    # (最終 profile では keyctl 自体が SIGSYS になり観測できない)
    main = copy_plugin("rsi", tmp_path)
    mid = ("from agentic_fx.core import seccomp as S\nS.apply_allow6 = lambda: None\n"
           "keyring_before = wi.session_keyring_id()\n")
    post = "out['ids'] = [keyring_before, wi.session_keyring_id()]\n"
    res = run_child(cfg_for(main, mid=mid, post=post))
    before, after = res.out["ids"]
    assert before != after
    assert before == wi.session_keyring_id()


# --- 代表自己試験の hook --------------------------------------------------------

@needs_sandbox
def test_representative_selftest_passes_with_the_production_isolation_hook():
    out = F.run_representative_selftest(
        isolation_hook="agentic_fx.plugin.worker_isolation:isolate_for_selftest")
    assert out == F.SelftestOutcome(True, "ok")


@needs_sandbox
def test_selftest_hook_rejects_an_inherited_filter():
    code = ("from agentic_fx.core import seccomp as S\n"
            "S.install_program([(0x06, 0, 0, S.RET_ALLOW)], log=False)\n"
            "from agentic_fx.plugin import worker_isolation as wi\n"
            "try:\n    wi.isolate_for_selftest()\nexcept wi.IsolationStageError as e:\n"
            "    print(e.reason)\n")
    out = subprocess.run([sys.executable, "-P", "-B", "-c", code], capture_output=True,
                         text=True, timeout=60)
    assert out.stdout.strip() == "inherited_seccomp_filter", out.stderr


def test_run_isolation_stage_returns_none_on_handshake_eof(tmp_path):
    # EOF なら隔離せずに戻る (plugin を読まない)。protocol fd は退避されたまま
    code = ("import os\nfrom agentic_fx.plugin import worker_isolation as wi\n"
            f"r = wi.run_isolation_stage({str(tmp_path)!r}, lambda: None)\n"
            "os.write(2, repr(r).encode())\nos._exit(0)\n")
    out = subprocess.run([sys.executable, "-P", "-B", "-c", code], capture_output=True,
                         timeout=60)
    assert out.stderr == b"None"
    assert out.stdout == b""


# --- 準備段の失敗 ---------------------------------------------------------------------

def _run_prepare(seam: str) -> subprocess.CompletedProcess:
    """子で seam を実行してから `prepare_isolation()` を呼ぶ。stdout が protocol 側になる。"""
    code = ("import resource, os, sys\n"
            "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
            "from agentic_fx.plugin import worker_isolation as wi\n"
            + seam +
            "wi.prepare_isolation()\n"
            "os._exit(0)\n")
    return subprocess.run([sys.executable, "-P", "-B", "-c", code], capture_output=True,
                          stdin=subprocess.DEVNULL, timeout=60)


@pytest.mark.parametrize("seam", [
    "def _dup2(*a, **k):\n    raise OSError(9, 'injected dup2 failure')\nos.dup2 = _dup2\n",
    "def _boom():\n    raise KeyboardInterrupt()\nwi._allowlist_summary = _boom\n",
    "wi._failure_line_orig = wi._failure_line\n"
    "_n = [0]\n"
    "def _fl(reason, pid):\n    _n[0] += 1\n"
    "    if _n[0] > 1:\n        raise SystemExit(3)\n    return wi._failure_line_orig(reason, pid)\n"
    "wi._failure_line = _fl\n",
], ids=["dup2_oserror", "keyboard_interrupt", "system_exit"])
def test_a_failure_while_preparing_writes_the_unexpected_error_line_and_exits_zero(seam):
    res = _run_prepare(seam)
    assert res.returncode == 0, res.stderr
    lines = [json.loads(ln) for ln in res.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    line = lines[0]
    assert line["reason"] == "isolation_unexpected_error"
    assert {k: line[k] for k in ("phase", "ok", "stage")} == {
        "phase": "sandbox_ready", "ok": False, "stage": "sandbox"}
    assert isinstance(line["pid"], int)


def test_failing_to_duplicate_the_protocol_fd_exits_nonzero_with_one_stderr_line():
    seam = ("_dup = os.dup\n"
            "def _d(fd):\n    if fd == 1:\n        raise OSError(24, 'injected dup failure')\n"
            "    return _dup(fd)\n"
            "os.dup = _d\n")
    res = _run_prepare(seam)
    assert res.returncode != 0
    assert res.stdout == b""
    assert len(res.stderr.decode().strip().splitlines()) == 1
