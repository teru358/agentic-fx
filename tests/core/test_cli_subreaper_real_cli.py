"""実 CLI での subreaper と worker の子集合 (起動までの観測だけ。API は呼ばない)。

CLI には prompt を与えず stdin を開いたまま待たせ、外向き connect の listener は
launcher の PREPARED を受けた直後に閉じる (seccomp の user-notif が ENOSYS を返す)。
観測の後は pid 指定で止める。
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentic_fx.core import cli_launch as L
from agentic_fx.core import cli_launch_protocol as P
from tests.core import test_cli_launch as base
from tests.core.test_cli_launch_pins import _codex_native_elf
from tests.core.test_cli_subreaper import children_of, ppid_of

_kernel_ready = base._kernel_ready

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine != "x86_64",
    reason="improve-cli の launcher は x86_64 Linux 専用")

HOME = Path.home()


def _claude() -> str | None:
    p = shutil.which("claude") or str(HOME / ".local/bin/claude")
    return os.path.realpath(p) if os.path.exists(p) else None


def _opencode() -> str | None:
    p = shutil.which("opencode") or str(HOME / ".opencode/bin/opencode")
    return os.path.realpath(p) if os.path.exists(p) else None


def _codex_wrapper() -> str | None:
    return shutil.which("codex")


def descendants(root: int) -> dict[int, int]:
    """root の子孫 (pid → ppid)。"""
    table = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            pp = ppid_of(int(d))
            if pp is not None:
                table[int(d)] = pp
    out, frontier = {}, {root}
    while frontier:
        nxt = {pid for pid, pp in table.items() if pp in frontier and pid not in out}
        out.update({pid: table[pid] for pid in nxt})
        frontier = nxt
    return out


CASES = {
    "claude": (_claude, "claude", ["-p"]),
    "codex": (_codex_native_elf, "codex", ["exec"]),
    "opencode": (_opencode, "opencode", ["run"]),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_real_cli_subreaper_and_orphans(name, tmp_path):
    """実 CLI を実 launcher で exec し、CLI の下の PPid 鎖が CLI で閉じ、worker の
    直接の子が CLI 1 本だけであること。"""
    finder, backend, extra = CASES[name]
    binary = finder()
    if binary is None:
        pytest.skip(f"{name} の binary が見つからない")
    me = os.getpid()
    before = set(children_of(me))
    env = {"PATH": "/usr/bin:/bin", "LANG": "C", "HOME": str(tmp_path)}
    h = base._Harness()
    r = None
    try:
        r = h.launch(cli=binary, argv=[binary, *extra], backend=backend, env=env,
                     handoff=os.close, stdin=subprocess.PIPE, cwd=str(tmp_path),
                     timeout=20.0)
        assert r.started, r
        cli = r.launcher_proc.pid
        time.sleep(3.0)
        alive = r.launcher_proc.poll() is None
        desc = descendants(cli)
        # 子孫の ppid 鎖は必ず CLI で閉じる (worker や 1 に上がらない)
        for pid in desc:
            cur, hops = pid, 0
            while cur != cli:
                cur = ppid_of(cur)
                hops += 1
                assert cur is not None and cur not in (me, 1) and hops < 64, (pid, desc)
        assert set(children_of(me)) - before == {cli}
        if not alive:
            pytest.skip(f"{name} は prompt 無しで終了した (観測は終了前まで)")
    finally:
        if r is not None and r.launcher_proc is not None:
            for pid in list(descendants(r.launcher_proc.pid)):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            if r.launcher_proc.stdin is not None:
                r.launcher_proc.stdin.close()
        h.close()


def test_real_codex_js_wrapper_is_rejected():
    """npm の codex wrapper (`#!` の JS script) は artifact として拒否される。"""
    wrapper = _codex_wrapper()
    if wrapper is None:
        pytest.skip("npm の codex wrapper が見つからない")
    h = base._Harness()
    try:
        r = h.launch(cli=wrapper, argv=[wrapper, "exec"], backend="codex")
        assert (r.started, r.reason, r.series) == (
            False, P.CLI_ARTIFACT_UNSUPPORTED, L.BEFORE_HANDOFF), r
        assert r.launcher_proc is None
    finally:
        h.close()
