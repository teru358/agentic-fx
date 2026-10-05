"""二段 protocol の検証を試すための制御 worker (テスト専用)。

`python -B _control_worker.py <plugin_dir> <mode>` で起動される。本物の隔離段
(`worker_isolation.run_isolation_stage`) を掛けたうえで、`mode` に応じて
`sandbox_ready` を 1 か所だけ壊して返す。`load` を受け取ったら stderr に
`CONTROL_LOAD_RECEIVED` を書く (親が load を送ったかを技術ログで確かめるため)。
下の一覧に無い mode は正しい `sandbox_ready` と `plugin_ready` を返し、その後は
stdin を二度と読まずに眠る。
"""
from __future__ import annotations

import json
import os
import resource
import sys
import time

from agentic_fx.core import seccomp
from agentic_fx.plugin import worker_isolation

MODE = sys.argv[2]
PLUGIN_DIR = sys.argv[1]

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _write(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data):]


def _install_allow_all_filter() -> None:
    seccomp.install_program([(seccomp.BPF_RET_K, 0, 0, seccomp.RET_ALLOW)], log=False)


def _await_load() -> None:
    line = sys.stdin.buffer.readline()
    if line.strip() == b'{"op": "load"}':
        sys.stderr.write("CONTROL_LOAD_RECEIVED\n")
        sys.stderr.flush()
        if MODE == "plugin_error_then_die":
            # 候補の import 失敗を模す: plugin_ready ok:false を書いてすぐ死ぬ
            _write(protocol_fd, b'{"phase": "plugin_ready", "ok": false, "error": "boom"}\n')
            os._exit(0)
        if MODE == "sleep_after_load":
            time.sleep(60)
        if MODE == "sigsys_after_load":
            os.getcwd()
        _write(protocol_fd, b'{"phase": "plugin_ready", "ok": true}\n')
        if MODE == "reads_slowly_after_load":
            # 要求をゆっくり読み、応答は返さない
            while sys.stdin.buffer.read1(16384):
                time.sleep(0.1)
        time.sleep(60)


captured: dict = {}


def _read_handshake():
    captured["hs"] = json.loads(sys.stdin.buffer.readline())
    if MODE == "sleep_before_ready":
        time.sleep(60)
    if MODE == "exit_before_ready":
        os._exit(7)
    if MODE == "inherited_filter":
        _install_allow_all_filter()
    return captured["hs"]


if MODE == "unisolated_forgery":
    # 隔離を掛けずに、形だけは完全な attestation を返す
    from agentic_fx.core import landlock
    from agentic_fx.plugin import sandbox

    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    hs = json.loads(sys.stdin.buffer.readline())
    abi = landlock.plan_for_abi(landlock.landlock_abi()).abi
    ready = {"phase": "sandbox_ready", "ok": True, "pid": os.getpid(),
             **sandbox.expected_attestation(abi, hs["attest_nonce"])}
    _write(protocol_fd, (json.dumps(ready) + "\n").encode())
    _await_load()
    os._exit(0)

isolated = worker_isolation.run_isolation_stage(PLUGIN_DIR, _read_handshake)
assert isolated is not None
protocol_fd = isolated.protocol_fd
pid = os.getpid()
ready = json.loads(isolated.sandbox_ready_line(pid))

if MODE == "sigsys_before_ready":
    os.getcwd()

lines: list[dict | str] = [ready]
if MODE == "old_ready":
    lines = [{"ok": True, "ready": True, "pid": pid}]
elif MODE == "plugin_ready_first":
    lines = [{"phase": "plugin_ready", "ok": True}]
elif MODE == "unknown_phase":
    lines = [dict(ready, phase="hello")]
elif MODE == "extra_line":
    lines = [ready, {"phase": "plugin_ready", "ok": True}]
elif MODE == "bad_nonce":
    ready["nonce"] = "0" * 32
elif MODE == "bad_pid":
    ready["pid"] = pid + 1
elif MODE == "missing_pid":
    del ready["pid"]
elif MODE == "extra_field":
    ready["extra"] = 1
elif MODE == "bad_abi_type":
    ready["landlock_fs_abi"] = str(ready["landlock_fs_abi"])
elif MODE == "missing_scope":
    del ready["scope"]
elif MODE == "reordered":
    ready = {k: ready[k] for k in ("phase", "ok", "pid", "landlock_fs_abi",
                                   "sandbox_profile_version", "landlock_tsync", "seccomp",
                                   "keyring", "network", "scope", "nonce")}
    lines = [ready]
elif MODE == "okfalse_with_extra_field":
    lines = [{"phase": "sandbox_ready", "ok": False, "stage": "sandbox",
              "reason": "rlimit_failed", "pid": pid, "extra": 1}]
elif MODE == "okfalse_unknown_reason":
    lines = [{"phase": "sandbox_ready", "ok": False, "stage": "sandbox",
              "reason": "made_up", "pid": pid}]
elif MODE.startswith("okfalse_candidate:"):
    lines = [{"phase": "sandbox_ready", "ok": False, "stage": "sandbox",
              "reason": MODE.split(":", 1)[1], "pid": pid}]
elif MODE == "extra_field_and_bad_pid":
    ready["extra"] = 1
    ready["pid"] = pid + 1
elif MODE == "bad_pid_and_bad_nonce":
    ready["pid"] = pid + 1
    ready["nonce"] = "0" * 32
elif MODE == "bad_nonce_and_extra_line":
    ready["nonce"] = "0" * 32
    lines = [ready, {"phase": "plugin_ready", "ok": True}]

_write(protocol_fd, "".join(json.dumps(x) + "\n" for x in lines).encode())
_await_load()
os._exit(0)
