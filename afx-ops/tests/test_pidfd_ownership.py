"""要求 object が pidfd を取得から要求終了まで持ち、一回だけ閉じる。

観測は client 子 process の外から行う: ``/proc/<client-pid>/fd`` の readlink で
``anon_inode:[pidfd]`` の有無を見る。close の回数は strace の記録で数える。
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest
from _ops_harness import (client_command, daemon, finish, make_layout, pidfd_supported,
                          run_client, spawn_client, wait_until)

pytestmark = pytest.mark.skipif(not pidfd_supported(),
                                reason="SO_PEERPIDFD の無い kernel (< 6.5)")


def _pidfds(pid: int) -> list[str]:
    found = []
    try:
        names = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return found
    for name in names:
        try:
            if os.readlink(f"/proc/{pid}/fd/{name}") == "anon_inode:[pidfd]":
                found.append(name)
        except OSError:
            continue
    return found


def test_pidfd_stays_open_until_reply_and_closed_once(tmp_path, child_env):
    layout = make_layout(tmp_path)
    barrier, replied, leave = tmp_path / "barrier", tmp_path / "replied", tmp_path / "leave"
    steps = [{"op": "call", "call": "status"}, {"op": "touch", "path": str(replied)},
             {"op": "wait", "path": str(leave)}]
    with daemon(layout, f"barrier={barrier}"):
        proc = spawn_client(layout, layout.spec("ops-web", steps), child_env)
        try:
            # daemon が応答を止めている間 (鍵は送信済み、応答待ち) は pidfd が開いている。
            assert wait_until(lambda: layout.record.exists() or _pidfds(proc.pid))
            assert wait_until(lambda: len(_pidfds(proc.pid)) == 1)
            # 認証の途中ではなく応答待ちに入った後も開いたままであること。
            assert not wait_until(lambda: len(_pidfds(proc.pid)) != 1, limit=0.5)
            barrier.touch()
            assert wait_until(replied.exists)
            # 応答を受け取った後、process が生きている間に pidfd はもう無い。
            assert _pidfds(proc.pid) == []
            leave.touch()
        finally:
            rc, lines, err = finish(proc)
    assert rc == 0, err
    assert lines == [{"op": "call", "ok": True, "status": 200}]


def test_pidfd_is_closed_after_a_failed_request(tmp_path, child_env):
    layout = make_layout(tmp_path)
    failed, leave = tmp_path / "failed", tmp_path / "leave"
    steps = [{"op": "call", "call": "status", "expect_error": True},
             {"op": "touch", "path": str(failed)}, {"op": "wait", "path": str(leave)}]
    with daemon(layout, "kill_parent_before_reply"):
        proc = spawn_client(layout, layout.spec("ops-web", steps), child_env)
        try:
            assert wait_until(failed.exists)
            assert _pidfds(proc.pid) == []
            leave.touch()
        finally:
            rc, lines, err = finish(proc)
    assert rc == 0, err
    assert lines == [{"op": "call", "ok": False, "reason": "response_lost",
                      "token_sent": True}]


_PIDFD = re.compile(r"^(\d+) +getsockopt\(\d+, SOL_SOCKET, (?:SO_PEERPIDFD|0x4d|77), "
                    r"\[(\d+)\]")


def _close_counts(trace: Path) -> list[int]:
    """各 SO_PEERPIDFD の fd について、取得後に同じ pid が close した回数。"""
    lines = trace.read_text().splitlines()
    counts = []
    for index, line in enumerate(lines):
        match = _PIDFD.match(line)
        if not match:
            continue
        pid, fd = match.group(1), match.group(2)
        closed = 0
        for later in lines[index + 1:]:
            if re.match(rf"^{pid} +close\({fd}\)", later):
                closed += 1
            # 同じ番号が別の fd として作り直されたら、そこから先は数えない。
            if re.match(rf"^{pid} +\w+\(.*\) = {fd}$", later) and "close" not in later:
                break
        counts.append(closed)
    return counts


@pytest.mark.skipif(shutil.which("strace") is None, reason="strace が無い")
@pytest.mark.parametrize("flags,steps,expect_rc", [
    ((), [{"op": "call", "call": "status"}], 0),
    (("kill_parent_before_reply",), [{"op": "call", "call": "status"}], 3),
    (("parent_waits",), [{"op": "call", "call": "status"}], 0),
], ids=["reply", "peer_exit_during_request", "parent_alive"])
def test_pidfd_close_is_counted_once_by_strace(tmp_path, child_env, flags, steps, expect_rc):
    import subprocess
    from _oracle_harness import no_core
    layout = make_layout(tmp_path)
    trace = tmp_path / "trace"
    with daemon(layout, *flags):
        command = client_command(layout, layout.spec("ops-web", steps),
                                 prefix=["strace", "-f", "-o", str(trace), "-e",
                                         "trace=getsockopt,close,socket,openat,pidfd_open"])
        done = subprocess.run(command, capture_output=True, text=True, timeout=60,
                              env=child_env, preexec_fn=no_core)
    assert done.returncode == expect_rc, done.stderr
    assert _close_counts(trace) == [1]
