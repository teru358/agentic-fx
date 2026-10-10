"""MCP の spool (shim と dispatcher) の受け渡しを実 file system で観測する。

rename・inotify・openat2 は実 kernel に当て、shim は実 process
(`python -m agentic_fx.tools.mcp_shim <spool dir>`) で起こす。
"""
from __future__ import annotations

import json
import os
import resource
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentic_fx.tools import mcp_spool
from agentic_fx.tools import mcp_spool_client as client
from agentic_fx.tools.mcp_shim import McpShimDispatcher
from agentic_fx.tools.registry import ToolDef, ToolRegistry


def _no_core() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _registry(calls: list) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ToolDef(
        "count", "records x",
        {"type": "object", "properties": {"x": {"type": "integer"}}},
        lambda x=0: calls.append(x) or {"n": len(calls)}))
    reg.register(ToolDef(
        "never_allowed", "registered but not allowed", {"type": "object"},
        lambda: calls.append("never_allowed") or {}))
    return reg


@pytest.fixture
def dispatchers():
    started: list[mcp_spool.SpoolDispatcher] = []
    yield started
    for d in started:
        d.stop(timeout_sec=5)


def _make(dispatchers, tmp_path, *, calls=None, registry=None,
          allowed=("count",), after_send=None, start=True, **kw):
    root = mcp_spool.SpoolRoot.create(tmp_path, client.new_request_id())
    if registry is None:
        registry = _registry(calls if calls is not None else [])
    mcp = McpShimDispatcher(registry=registry, allowed=list(allowed),
                            after_send=after_send)
    d = mcp_spool.SpoolDispatcher(root, mcp.respond, **kw)
    mcp.record = d.record_current
    dispatchers.append(d)
    if start:
        d.start()
    return d


def _call_req(rpc_id=1, x=1, name="count") -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
            "params": {"name": name, "arguments": {"x": x}}}


def _place(spool: Path, data: bytes, *, rid: str | None = None,
           mode: int = 0o600) -> str:
    """shim と同じ手順 (tmp- に書いて req- に NOREPLACE rename) で任意の
    中身を置く。"""
    rid = rid or client.new_request_id()
    dirfd = os.open(spool, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = os.open("tmp-" + rid, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                     0o600, dir_fd=dirfd)
        os.write(fd, data)
        os.close(fd)
        os.chmod("tmp-" + rid, mode, dir_fd=dirfd)
        client.renameat2_noreplace(dirfd, "tmp-" + rid, "req-" + rid)
    finally:
        os.close(dirfd)
    return rid


def _wait(pred, timeout=5.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def _events(d, rid, kind=None):
    return [e for e in d.ledger.events(kind) if e.request_id == rid]


def _wait_rejected(d, rid) -> str:
    _wait(lambda: _events(d, rid, "rejected"), what=f"rejection of {rid}")
    return _events(d, rid, "rejected")[0].detail


def _read_resp(spool: Path, rid: str) -> dict:
    return json.loads((spool / ("resp-" + rid)).read_text())


def _spawn_shim(spool: Path, *, audit_sockets: bool = False):
    if audit_sockets:
        # socket の生成を audit hook で数え、終了時に stderr へ出す。
        code = (
            "import sys\n"
            "made = []\n"
            "sys.addaudithook(lambda ev, args: made.append(ev) "
            "if ev == 'socket.__new__' else None)\n"
            "from agentic_fx.tools.mcp_shim import run_mcp_shim\n"
            "try:\n"
            "    run_mcp_shim(sys.argv[1])\n"
            "finally:\n"
            "    sys.stderr.write('SOCKETS=%d\\n' % len(made))\n")
        argv = [sys.executable, "-c", code, str(spool)]
    else:
        argv = [sys.executable, "-m", "agentic_fx.tools.mcp_shim", str(spool)]
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            preexec_fn=_no_core)


def _shim_rpc(proc, req: dict) -> dict:
    proc.stdin.write(json.dumps(req) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def _names_by_prefix(spool: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in os.listdir(spool):
        prefix = name.split("-", 1)[0] + "-"
        out[prefix] = out.get(prefix, 0) + 1
    return out


# --- 往復 ---------------------------------------------------------------

def test_shim_request_round_trips_through_spool(tmp_path, dispatchers):
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    proc = _spawn_shim(d.spool_dir, audit_sockets=True)
    try:
        listed = _shim_rpc(proc, {"jsonrpc": "2.0", "id": 1,
                                  "method": "tools/list", "params": {}})
        assert [t["name"] for t in listed["result"]["tools"]] == ["count"]
        called = _shim_rpc(proc, _call_req(rpc_id=2, x=5))
        assert called["id"] == 2
        assert json.loads(called["result"]["content"][0]["text"]) == {"n": 1}
        proc.stdin.close()
        assert proc.wait(timeout=5) == 0
        assert "SOCKETS=0" in proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    assert calls == [5]
    assert _names_by_prefix(d.spool_dir) == {"claim-": 2, "resp-": 2}
    # tools/list は処理したが tool は実行していない。
    assert len(d.ledger.events("dispatched")) == 2
    assert len(d.ledger.tool_executed_ids()) == 1


def test_shim_entry_does_not_import_worker_side_modules(tmp_path):
    """shim は部分木内で動くので、入口の import は spool の client と標準
    ライブラリの軽いものに限る。"""
    code = ("import sys, agentic_fx.tools.mcp_shim\n"
            "bad = [m for m in ('socket', 'agentic_fx.tools.registry', "
            "'agentic_fx.tools.mcp_spool', 'agentic_fx.mission_worker', "
            "'pydantic', 'jsonschema') if m in sys.modules]\n"
            "print(bad)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, check=True, preexec_fn=_no_core).stdout
    assert out.strip() == "[]"


# --- 検証 (受入 16) -------------------------------------------------------

def test_symlinked_request_is_not_executed(tmp_path, dispatchers):
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    outside = tmp_path / "outside.json"
    body = json.dumps(_call_req()).encode()
    outside.write_bytes(body)
    outside.chmod(0o600)
    before = os.stat(outside)
    rid = client.new_request_id()
    os.symlink(outside, d.spool_dir / ("req-" + rid))
    assert _wait_rejected(d, rid).startswith("open_failed")
    assert calls == []
    assert rid not in d.ledger.tool_executed_ids()
    after = os.stat(outside)
    assert outside.read_bytes() == body
    assert (after.st_mode, after.st_mtime_ns, after.st_nlink) == (
        before.st_mode, before.st_mtime_ns, before.st_nlink)


def test_hardlinked_request_is_not_executed(tmp_path, dispatchers):
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    other = tmp_path / "other.json"
    other.write_bytes(json.dumps(_call_req()).encode())
    other.chmod(0o600)
    rid = client.new_request_id()
    os.link(other, d.spool_dir / ("req-" + rid))
    assert _wait_rejected(d, rid) == "link_count"
    assert calls == []
    assert rid not in d.ledger.tool_executed_ids()


_GROWER = """
import os, sys
spool, rid = sys.argv[1], sys.argv[2]
dfd = os.open(spool, os.O_RDONLY | os.O_DIRECTORY)
fd = os.open("tmp-" + rid, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dfd)
# どの長さで切っても JSON にならない (文字列が閉じない) 前置き。
os.write(fd, b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", '
             b'"params": {"name": "count", "arguments": {"x": 1}}, "pad": "')
os.write(fd, b"a" * (int(sys.argv[3]) - 200))
os.rename("tmp-" + rid, "req-" + rid, src_dir_fd=dfd, dst_dir_fd=dfd)
sys.stdout.write("placed\\n"); sys.stdout.flush()
chunk = b"a" * (1 << 20)
for _ in range(64):
    os.write(fd, chunk)
sys.stdin.read()
"""


def test_growing_or_oversized_request_is_rejected(tmp_path, dispatchers):
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    limit = mcp_spool.MAX_REQUEST_BYTES

    # 置いた時点で上限を 1 byte 超える file。
    big = json.dumps(_call_req()).encode()
    big = big[:-1] + b', "pad": "' + b"a" * (limit - len(big)) + b'"}'
    assert len(big) > limit
    big_rid = _place(d.spool_dir, big)
    assert _wait_rejected(d, big_rid) == "oversized"

    # 置いた後も writer が追記し続ける file。
    grow_rid = client.new_request_id()
    grower = subprocess.Popen(
        [sys.executable, "-c", _GROWER, str(d.spool_dir), grow_rid,
         str(limit - 4096)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        preexec_fn=_no_core)
    try:
        assert grower.stdout.readline().strip() == "placed"
        reason = _wait_rejected(d, grow_rid)
    finally:
        grower.stdin.close()
        grower.wait(timeout=10)
    assert reason in ("oversized", "not_json")
    assert calls == []

    # dispatcher は止まらず次を処理する。
    ok_rid = _place(d.spool_dir, json.dumps(_call_req(x=3)).encode())
    _wait(lambda: ok_rid in d.ledger.tool_executed_ids(), what="next request")
    _wait(lambda: calls == [3], what="tool execution")


def test_non_private_mode_fifo_and_directory_are_rejected(tmp_path, dispatchers):
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    body = json.dumps(_call_req()).encode()

    world_rid = _place(d.spool_dir, body, mode=0o644)
    assert _wait_rejected(d, world_rid) == "not_private_mode"

    fifo_rid = client.new_request_id()
    os.mkfifo(d.spool_dir / ("req-" + fifo_rid), 0o600)
    assert _wait_rejected(d, fifo_rid) == "not_regular_file"

    dir_rid = client.new_request_id()
    os.mkdir(d.spool_dir / ("req-" + dir_rid), 0o700)
    assert _wait_rejected(d, dir_rid) == "not_regular_file"

    assert calls == []
    ok_rid = _place(d.spool_dir, json.dumps(_call_req(x=4)).encode())
    _wait(lambda: ok_rid in d.ledger.tool_executed_ids(),
          what="request after FIFO (dispatcher must not block)")
    assert calls == [4]


def test_foreign_uid_request_is_rejected(tmp_path, dispatchers):
    if os.getuid() != 0:
        pytest.skip("別 uid の file を作れない (非 root)")
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    rid = client.new_request_id()
    tmp = d.spool_dir / ("tmp-" + rid)
    tmp.write_bytes(json.dumps(_call_req()).encode())
    tmp.chmod(0o600)
    os.chown(tmp, 65534, 65534)
    os.rename(tmp, d.spool_dir / ("req-" + rid))
    assert _wait_rejected(d, rid) == "foreign_uid"
    assert calls == []


def test_same_id_is_executed_once(tmp_path, dispatchers):
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    body = json.dumps(_call_req(x=7)).encode()
    rid = _place(d.spool_dir, body)
    _wait(lambda: rid in d.ledger.tool_executed_ids(), what="first execution")

    # claim- が残っている状態で同じ id を置き直す。
    _place(d.spool_dir, body, rid=rid)
    _wait(lambda: any(e.detail == "claim_exists"
                      for e in _events(d, rid, "replay")),
          what="replay via existing claim")

    # claim- を消すと残っていた req- の claim は通るが、処理済み id は
    # 実行しない。
    os.unlink(d.spool_dir / ("claim-" + rid))
    _wait(lambda: any(e.detail == "id_already_processed"
                      for e in _events(d, rid, "replay")),
          what="replay via processed id set")
    assert calls == [7]
    assert d.ledger.tool_executed_ids().count(rid) == 1


def test_preplaced_response_tmp_blocks_execution(tmp_path, dispatchers):
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls, start=False)
    rid = client.new_request_id()
    fd = os.open(d.spool_dir / ("rtmp-" + rid), os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    _place(d.spool_dir, json.dumps(_call_req()).encode(), rid=rid)
    d.start()
    _wait(lambda: _events(d, rid, "response_tmp_preempted"),
          what="rtmp preemption")
    assert calls == []
    assert rid not in d.ledger.tool_executed_ids()


def test_preplaced_response_is_recorded_as_preempted(tmp_path, dispatchers):
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls, start=False)
    rid = client.new_request_id()
    forged = b'{"forged": true}\n'
    (d.spool_dir / ("resp-" + rid)).write_bytes(forged)
    _place(d.spool_dir, json.dumps(_call_req(x=2)).encode(), rid=rid)
    d.start()
    _wait(lambda: _events(d, rid, "response_preempted"),
          what="resp preemption")
    # 実行はした (正本は ledger)。先置きの応答は書き換えない。
    assert calls == [2]
    assert rid in d.ledger.tool_executed_ids()
    assert (d.spool_dir / ("resp-" + rid)).read_bytes() == forged
    assert not (d.spool_dir / ("rtmp-" + rid)).exists()


def test_concurrent_claims_execute_once(tmp_path, dispatchers):
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls, start=False)
    rid = _place(d.spool_dir, json.dumps(_call_req(x=1)).encode())
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def claim():
        barrier.wait()
        results.append(d.claim_and_process(rid))

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert sorted(results) == [False, True]
    assert calls == [1]
    assert d.ledger.tool_executed_ids() == [rid]

    # 並行する 8 本の shim の要求が全部 1 回ずつ実行される。
    calls.clear()
    d.start()
    procs = [_spawn_shim(d.spool_dir) for _ in range(8)]
    try:
        for i, p in enumerate(procs):
            p.stdin.write(json.dumps(_call_req(rpc_id=i, x=i)) + "\n")
            p.stdin.flush()
        resps = [json.loads(p.stdout.readline()) for p in procs]
    finally:
        for p in procs:
            p.kill()
            p.wait(timeout=5)
    assert [r["id"] for r in resps] == list(range(8))
    assert sorted(calls) == list(range(8))
    assert len(d.ledger.tool_executed_ids()) == 9


def test_rescan_finds_requests_without_inotify_wake(tmp_path, dispatchers):
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls, use_inotify=False)
    time.sleep(0.1)  # 起動時の scan を済ませる
    rids = [_place(d.spool_dir, json.dumps(_call_req(x=i)).encode())
            for i in range(5)]
    _wait(lambda: all(r in d.ledger.tool_executed_ids() for r in rids),
          timeout=3.5, what="rescan without inotify")
    assert sorted(calls) == list(range(5))


def test_inotify_queue_overflow_is_followed_by_rescan(tmp_path, dispatchers):
    max_events = int(Path("/proc/sys/fs/inotify/max_queued_events").read_text())
    if max_events > 100_000:
        pytest.skip(f"max_queued_events={max_events} は溢れさせるのに大き過ぎる")
    calls: list = []
    # 起床処理を止めた状態 (thread 未起動) で、watch の queue を溢れさせる。
    d = _make(dispatchers, tmp_path, calls=calls, start=False,
              rescan_interval_sec=3600)
    dirfd = os.open(d.spool_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for i in range(max_events + 100):
            # 同じ名前の連続 event は kernel が 1 件にまとめるので名前を変える。
            fd = os.open(f"j{i}", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=dirfd)
            os.close(fd)
            os.unlink(f"j{i}", dir_fd=dirfd)
    finally:
        os.close(dirfd)
    d.start()
    _wait(lambda: d.overflow_count >= 1, what="IN_Q_OVERFLOW")
    rid = _place(d.spool_dir, json.dumps(_call_req(x=9)).encode())
    _wait(lambda: rid in d.ledger.tool_executed_ids(), what="request after overflow")
    assert calls == [9]


def test_entry_flood_is_spool_flood(tmp_path, dispatchers):
    calls: list = []
    fatal: list[str] = []
    d = _make(dispatchers, tmp_path, calls=calls, max_entries=50,
              on_fatal=fatal.append)
    for i in range(60):
        (d.spool_dir / f"junk-{i}").write_bytes(b"")
    _wait(lambda: d.failure_reason == mcp_spool.SPOOL_FLOOD, what="flood")
    assert fatal == [mcp_spool.SPOOL_FLOOD]
    d.thread.join(5)
    assert not d.thread.is_alive()
    for i in range(60):
        (d.spool_dir / f"junk-{i}").unlink()
    rid = _place(d.spool_dir, json.dumps(_call_req()).encode())
    time.sleep(1.5)
    assert (d.spool_dir / ("req-" + rid)).exists()
    assert calls == []


def test_default_flood_limit_is_the_module_constant(tmp_path, dispatchers):
    d = _make(dispatchers, tmp_path, start=False)
    assert d._max_entries == mcp_spool.MAX_SPOOL_ENTRIES  # noqa: SLF001


_BASE = ('{"jsonrpc": "2.0", "id": 1, "method": "tools/call", '
         '"params": {"name": "count", "arguments": {"x": 1}}')


# depth_far_beyond_limit は decoder の再帰上限 (RecursionError) に届く深さ。
# 深さ上限 (32) の検査が先に効き、json_too_deep として拒否されること。
@pytest.mark.parametrize("raw,reason", [
    ((_BASE + "}").encode(), None),
    ((_BASE + ', "pad": ' + "[" * 40 + "]" * 40 + "}").encode(), "json_too_deep"),
    ((_BASE + ', "pad": ' + "[" * 100000 + "]" * 100000 + "}").encode(),
     "json_too_deep"),
    ((_BASE + ', "pad": {' + ", ".join(f'"k{i}": 0' for i in range(5000))
      + "}}").encode(), "json_too_many_keys"),
    ((_BASE + ', "method": "tools/call"}').encode(), "json_duplicate_key"),
    ((_BASE + ', "pad": "').encode() + b"\xff" + b'"}', "invalid_utf8"),
    (b"not json", "not_json"),
    (b"[1, 2]", "not_object"),
    (b'{"jsonrpc": "2.0", "method": "tools/call", "params": '
     b'{"name": "count", "arguments": {"x": 1}}}', "notification"),
], ids=["control", "depth", "depth_far_beyond_limit", "keys", "duplicate", "utf8",
        "not_json", "not_object", "no_id"])
def test_json_limits(tmp_path, dispatchers, raw, reason):
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    rid = _place(d.spool_dir, raw)
    if reason is None:
        _wait(lambda: rid in d.ledger.tool_executed_ids(), what="control execution")
        assert calls == [1]
        return
    assert _wait_rejected(d, rid) == reason
    assert calls == []
    # shim が待ち続けないよう、拒否にも応答を返す。
    _wait(lambda: (d.spool_dir / ("resp-" + rid)).exists(), what="error response")
    assert _read_resp(d.spool_dir, rid)["error"]["code"] == -32600


_SHIM_WITH_TIMEOUT = (
    "import sys\n"
    "from agentic_fx.tools import mcp_spool_client as c\n"
    "fixed = sys.argv[3] if len(sys.argv) > 3 else None\n"
    "if fixed:\n"
    "    c.new_request_id = lambda: fixed\n"
    "from agentic_fx.tools.mcp_shim import run_mcp_shim\n"
    "run_mcp_shim(sys.argv[1], response_timeout_sec=float(sys.argv[2]))\n")


def _spawn_shim_with_timeout(spool: Path, timeout_sec: float,
                             fixed_rid: str | None = None):
    argv = [sys.executable, "-c", _SHIM_WITH_TIMEOUT, str(spool),
            str(timeout_sec)]
    if fixed_rid:
        argv.append(fixed_rid)
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            preexec_fn=_no_core)


def _assert_dispatcher_unavailable(proc, rpc_id: int) -> None:
    started = time.monotonic()
    resp = _shim_rpc(proc, _call_req(rpc_id=rpc_id))
    assert time.monotonic() - started < 10
    assert resp["id"] == rpc_id
    assert resp["error"]["code"] == -32000
    assert resp["error"]["message"] == "dispatcher_unavailable"


def test_shim_returns_error_when_no_response_is_ever_published(
        tmp_path, dispatchers):
    """応答の一時 file が先置きされた要求は実行されず応答も置かれない
    (自己妨害として扱う)。shim は期限で error を返し、次の要求に進める。"""
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    rid = client.new_request_id()
    fd = os.open(d.spool_dir / ("rtmp-" + rid), os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    proc = _spawn_shim_with_timeout(d.spool_dir, 0.5, fixed_rid=rid)
    try:
        _assert_dispatcher_unavailable(proc, 1)
        assert calls == []
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_shim_returns_error_after_dispatcher_stops(tmp_path, dispatchers):
    d = _make(dispatchers, tmp_path)
    proc = _spawn_shim_with_timeout(d.spool_dir, 0.5)
    try:
        listed = _shim_rpc(proc, {"jsonrpc": "2.0", "id": 1,
                                  "method": "tools/list", "params": {}})
        assert "result" in listed
        assert d.stop(5)
        _assert_dispatcher_unavailable(proc, 2)
        # 期限切れの後も shim は次の要求を受け付け続ける。
        _assert_dispatcher_unavailable(proc, 3)
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_spool_root_is_private_and_fresh(tmp_path):
    nonce = client.new_request_id()
    root = mcp_spool.SpoolRoot.create(tmp_path, nonce)
    try:
        assert root.path == mcp_spool.mcp_spool_dir(tmp_path, nonce)
        assert root.path == tmp_path / "afx-spool" / nonce
        st = os.lstat(root.path)
        assert stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o700
        assert stat.S_IMODE(os.lstat(root.path.parent).st_mode) == 0o700
    finally:
        root.close()

    # 同じ nonce の path が既に在れば (directory、file、symlink) 作らない。
    with pytest.raises(mcp_spool.SpoolCreateFailed, match="spool_create_failed"):
        mcp_spool.SpoolRoot.create(tmp_path, nonce)
    file_nonce = client.new_request_id()
    (tmp_path / "afx-spool" / file_nonce).write_text("x")
    with pytest.raises(mcp_spool.SpoolCreateFailed, match="spool_create_failed"):
        mcp_spool.SpoolRoot.create(tmp_path, file_nonce)
    link_nonce = client.new_request_id()
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    os.symlink(target, tmp_path / "afx-spool" / link_nonce)
    with pytest.raises(mcp_spool.SpoolCreateFailed, match="spool_create_failed"):
        mcp_spool.SpoolRoot.create(tmp_path, link_nonce)
    assert os.listdir(target) == []

    # 親 afx-spool が symlink、または共有 mode でも作らない。
    other = tmp_path / "w2"
    other.mkdir()
    os.symlink(target, other / "afx-spool")
    with pytest.raises(mcp_spool.SpoolCreateFailed, match="spool_create_failed"):
        mcp_spool.SpoolRoot.create(other, client.new_request_id())
    shared = tmp_path / "w3"
    (shared / "afx-spool").mkdir(parents=True)
    (shared / "afx-spool").chmod(0o755)
    with pytest.raises(mcp_spool.SpoolCreateFailed, match="spool_create_failed"):
        mcp_spool.SpoolRoot.create(shared, client.new_request_id())
    # nonce は 128 bit 以上の hex。
    with pytest.raises(mcp_spool.SpoolCreateFailed, match="spool_create_failed"):
        mcp_spool.SpoolRoot.create(tmp_path / "w4", "ab" * 8)


# --- allowed と budget (受入 17) -----------------------------------------

def test_tool_outside_allowed_is_rejected(tmp_path, dispatchers):
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    resp = client.call(d.spool_dir, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "never_allowed", "arguments": {}}}, timeout_sec=5)
    assert resp["error"]["code"] == -32601
    assert "result" not in resp
    assert calls == []


def _improve_registry(tmp_path, **budget):
    import agentic_fx.mission_worker as mw
    from agentic_fx.config import ImproveToolBudgetSettings, load_settings
    settings = load_settings(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
    settings = settings.model_copy(update={
        "improve": settings.improve.model_copy(update={
            "tool_budget": ImproveToolBudgetSettings(**budget)})})
    staging = tmp_path / "staging"
    staging.mkdir()
    source = tmp_path / "source"
    source.mkdir()

    def no_rpc(name, args):
        raise AssertionError(f"unexpected rpc {name}")

    registry, counters = mw._build_improve_registry(
        settings=settings, workdir=tmp_path, staging_dir=staging,
        source_snapshot_dir=source, rpc_client=no_rpc)
    return mw, registry, counters, staging


def _write_call(i: int) -> dict:
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call",
            "params": {"name": "write_staging_file",
                       "arguments": {"name": f"c{i}", "rel": "plugin.py",
                                     "content": f"x = {i}\n"}}}


def _payload(resp: dict) -> dict:
    return json.loads(resp["result"]["content"][0]["text"])


def test_budget_boundary_is_enforced_through_spool(tmp_path):
    mw, registry, counters, staging = _improve_registry(
        tmp_path, max_writes=2, max_refusal_streak=1)
    d = mw._start_mcp_dispatcher(workdir=tmp_path, registry=registry,
                                 after_send=counters.fire_if_pending)
    try:
        assert _payload(client.call(d.spool_dir, _write_call(0), 5)) == {"ok": True}
        assert _payload(client.call(d.spool_dir, _write_call(1), 5)) == {"ok": True}
        assert not counters.abort_event.is_set()
        refused = _payload(client.call(d.spool_dir, _write_call(2), 5))
        assert refused["error"] == "budget exhausted"
        assert counters.abort_event.wait(5)
    finally:
        d.stop()
    assert sorted(p.name for p in staging.iterdir()) == ["c0", "c1"]


def test_allowed_budget_execute_and_counter_are_serialized(tmp_path):
    mw, registry, counters, staging = _improve_registry(
        tmp_path, max_writes=5, max_refusal_streak=100)
    d = mw._start_mcp_dispatcher(workdir=tmp_path, registry=registry,
                                 after_send=counters.fire_if_pending)
    procs = [_spawn_shim(d.spool_dir) for _ in range(12)]
    try:
        for i, p in enumerate(procs):
            p.stdin.write(json.dumps(_write_call(i)) + "\n")
            p.stdin.flush()
        payloads = [_payload(json.loads(p.stdout.readline())) for p in procs]
    finally:
        for p in procs:
            p.kill()
            p.wait(timeout=5)
        d.stop()
    accepted = [p for p in payloads if p == {"ok": True}]
    refused = [p for p in payloads if p.get("error") == "budget exhausted"]
    assert (len(accepted), len(refused)) == (5, 7)
    assert counters.writes == 5
    assert len(list(staging.iterdir())) == 5
    assert len(d.ledger.tool_executed_ids()) == 12


# --- stop ---------------------------------------------------------------

def test_stop_ends_rescan_and_execution(tmp_path, dispatchers):
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    first = _place(d.spool_dir, json.dumps(_call_req(x=1)).encode())
    _wait(lambda: first in d.ledger.tool_executed_ids(), what="first request")
    started = time.monotonic()
    assert d.stop(timeout_sec=3) is True
    assert time.monotonic() - started < 3
    assert not d.thread.is_alive()
    late = _place(d.spool_dir, json.dumps(_call_req(x=2)).encode())
    time.sleep(1.5)
    assert (d.spool_dir / ("req-" + late)).exists()
    assert calls == [1]
    assert d.claim_and_process(late) is False
    assert calls == [1]


# --- UDS の廃止 ----------------------------------------------------------

def _sockets_under(root: Path) -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            if stat.S_ISSOCK(os.lstat(p).st_mode):
                found.append(p)
    return found


class _ShimProbeRunner:
    """worker が渡した spool directory に実 shim を繋いで tools/list を
    投げ、実行中の workdir に socket file が無いことを記録する。"""

    observed: dict = {}

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self._afx_mcp_dispatcher = None

    def run(self, mission):
        from agentic_fx.runners.base import MissionResult
        proc = _spawn_shim(self.mcp_spool_dir, audit_sockets=True)
        try:
            resp = _shim_rpc(proc, {"jsonrpc": "2.0", "id": 1,
                                    "method": "tools/list", "params": {}})
            _ShimProbeRunner.observed["tools_list_ok"] = "result" in resp
            _ShimProbeRunner.observed["sockets_during"] = _sockets_under(
                self._workdir)
            proc.stdin.close()
            proc.wait(timeout=5)
            _ShimProbeRunner.observed["shim_stderr"] = proc.stderr.read()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        return MissionResult("completed", {"action": "no_trade"}, [],
                             reason=None)


@pytest.mark.parametrize("profile", ["improve", "trade"])
def test_no_unix_socket_is_created_for_mcp(monkeypatch, tmp_path, profile):
    from tests.test_mission_worker_protocol import _drive_main
    import agentic_fx.mission_worker as mw

    _ShimProbeRunner.observed = {}
    monkeypatch.setattr(mw.runner_factory, "build_runner",
                        lambda *a, workdir, **kw: _ShimProbeRunner(workdir))
    if profile == "improve":
        monkeypatch.setattr(mw, "_bootstrap_improve_profile",
                            lambda *a, **kw: None)
        overrides = {"worker_profile": "improve", "db_path": None,
                     "plugins_dir": None, "mission_id": "m-uds",
                     "staging_dir": str(tmp_path / "staging" / "m-uds"),
                     "source_snapshot_dir": str(tmp_path / "source")}

        def mutate(d):
            d["runner"]["improve"]["backend"] = "claude"
    else:
        overrides = None

        def mutate(d):
            d["runner"]["trade"]["backend"] = "claude"

    frames, _, _ = _drive_main(monkeypatch, tmp_path,
                               handshake_overrides=overrides,
                               settings_mutator=mutate)
    assert frames[-1]["type"] == "result"
    assert frames[-1]["status"] == "completed", frames[-1]
    assert _ShimProbeRunner.observed["tools_list_ok"] is True
    assert _ShimProbeRunner.observed["sockets_during"] == []
    assert "SOCKETS=0" in _ShimProbeRunner.observed["shim_stderr"]
    assert _sockets_under(tmp_path) == []
    assert len(list((tmp_path / "afx-spool").iterdir())) == 1


class _FloodingRunner:
    """spool を上限超まで埋め、dispatcher が flood を検出するのを待って
    completed を返す (worker が mission failure に置き換えることを見る)。"""

    observed: dict = {}

    def __init__(self, kw: dict) -> None:
        self._kw = kw
        self._afx_mcp_dispatcher = None

    def run(self, mission):
        from agentic_fx.runners.base import MissionResult
        for i in range(mcp_spool.MAX_SPOOL_ENTRIES + 1):
            (self.mcp_spool_dir / f"junk-{i}").write_bytes(b"")
        _wait(lambda: self._afx_mcp_dispatcher.failure_reason is not None,
              what="flood detection")
        abort_event = self._kw.get("abort_event")
        _FloodingRunner.observed["abort_set"] = (
            None if abort_event is None else abort_event.wait(5))
        reason_fn = self._kw.get("abort_reason_fn")
        _FloodingRunner.observed["abort_reason"] = (
            None if reason_fn is None else reason_fn())
        return MissionResult("completed", {"action": "no_trade"}, [],
                             reason=None)


@pytest.mark.parametrize("profile", ["improve", "trade"])
def test_spool_flood_fails_the_mission(monkeypatch, tmp_path, profile):
    from tests.test_mission_worker_protocol import _drive_main
    import agentic_fx.mission_worker as mw

    _FloodingRunner.observed = {}
    monkeypatch.setattr(mw.runner_factory, "build_runner",
                        lambda *a, **kw: _FloodingRunner(kw))
    if profile == "improve":
        monkeypatch.setattr(mw, "_bootstrap_improve_profile",
                            lambda *a, **kw: None)
        overrides = {"worker_profile": "improve", "db_path": None,
                     "plugins_dir": None, "mission_id": "m-flood",
                     "staging_dir": str(tmp_path / "staging" / "m-flood"),
                     "source_snapshot_dir": str(tmp_path / "source")}

        def mutate(d):
            d["runner"]["improve"]["backend"] = "claude"
    else:
        overrides = None

        def mutate(d):
            d["runner"]["trade"]["backend"] = "claude"

    frames, _, _ = _drive_main(monkeypatch, tmp_path,
                               handshake_overrides=overrides,
                               settings_mutator=mutate)
    result = frames[-1]
    assert result["type"] == "result"
    assert result["status"] == "failed"
    assert result["reason"] == mcp_spool.SPOOL_FLOOD
    assert result["output"] is None
    # CLI を止めるため abort が立ち、理由は budget ではなく spool 側。
    assert _FloodingRunner.observed["abort_set"] is True
    assert _FloodingRunner.observed["abort_reason"] == mcp_spool.SPOOL_FLOOD


_FLOODING_CLI = """#!{python}
import os, sys, time
from pathlib import Path
spool = next((Path.cwd() / "afx-spool").iterdir())
Path("cli_pid").write_text(str(os.getpid()))
for i in range({n}):
    (spool / ("junk-%d" % i)).write_bytes(b"")
time.sleep(120)
"""


def test_spool_flood_stops_the_real_trade_cli(monkeypatch, tmp_path):
    """実 process の CLI (fake claude) が spool を溢れさせたら、mission の
    timeout を待たず CLI が止まり、result は failed / spool_flood になる。"""
    from tests.test_mission_worker_protocol import _drive_main

    cli = tmp_path / "fake-claude"
    cli.write_text(_FLOODING_CLI.format(
        python=sys.executable, n=mcp_spool.MAX_SPOOL_ENTRIES + 1))
    cli.chmod(0o700)
    creds = tmp_path / ".credentials.json"
    creds.write_text('{"token":"x"}')
    creds.chmod(0o600)

    def mutate(d):
        d["runner"]["trade"]["backend"] = "claude"
        d["runner"]["claude"]["bin"] = str(cli)
        d["runner"]["claude"]["credentials_file"] = str(creds)
        d["runner"]["cli_terminate_grace_sec"] = 2.0

    started = time.monotonic()
    mission = {"prompt": "decide the next trade", "tools": ["get_quote"],
               "output_schema": {}, "max_turns": 3, "timeout_sec": 60.0}
    frames, _, _ = _drive_main(monkeypatch, tmp_path, settings_mutator=mutate,
                               handshake_overrides={"mission": mission})
    elapsed = time.monotonic() - started

    pid = int((tmp_path / "cli_pid").read_text())
    try:
        os.killpg(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    finally:
        try:
            os.killpg(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
    result = frames[-1]
    assert result["type"] == "result"
    assert result["status"] == "failed"
    assert result["reason"] == mcp_spool.SPOOL_FLOOD
    assert not alive
    assert elapsed < 25


@pytest.mark.parametrize("profile", ["improve", "trade"])
def test_spool_create_failure_refuses_to_start_the_mission(
        monkeypatch, tmp_path, profile):
    """spool root を作れなければ `ready: ok=False` (`spool_create_failed`)
    で止まり、runner は組み立てられない (mission を始めない)。"""
    from tests.test_mission_worker_protocol import _drive_main
    import agentic_fx.mission_worker as mw

    built: list = []
    bootstrapped: list = []
    monkeypatch.setattr(mw.runner_factory, "build_runner",
                        lambda *a, **kw: built.append(kw) or _ShimProbeRunner(
                            kw["workdir"]))
    (tmp_path / "afx-spool").write_text("masks the spool parent")
    if profile == "improve":
        monkeypatch.setattr(mw, "_bootstrap_improve_profile",
                            lambda *a, **kw: bootstrapped.append(True))
        overrides = {"worker_profile": "improve", "db_path": None,
                     "plugins_dir": None, "mission_id": "m-nospool",
                     "staging_dir": str(tmp_path / "staging" / "m-nospool"),
                     "source_snapshot_dir": str(tmp_path / "source")}

        def mutate(d):
            d["runner"]["improve"]["backend"] = "claude"
    else:
        overrides = None

        def mutate(d):
            d["runner"]["trade"]["backend"] = "claude"

    frames, _, _ = _drive_main(monkeypatch, tmp_path,
                               handshake_overrides=overrides,
                               settings_mutator=mutate)
    assert len(frames) == 1, frames
    assert frames[0]["type"] == "ready" and frames[0]["ok"] is False
    assert mcp_spool.SPOOL_CREATE_FAILED in frames[0]["error"]
    assert built == []
    # improve では Landlock (bootstrap) の前に spool を作るので、そこで止まる。
    assert bootstrapped == []


@pytest.mark.parametrize("body", [
    b"[" * 100_000 + b"]" * 100_000,
    b'{"pad": "' + b"x" * (mcp_spool.MAX_REQUEST_BYTES - 64) + b'"}',
    b"\xff\xfe not utf-8",
], ids=["deep", "oversized", "not_utf8"])
def test_preplaced_response_tmp_stops_before_any_json_decode(
        tmp_path, dispatchers, monkeypatch, body):
    """応答の一時 file を先置きされた要求は、巨大・深い・壊れた中身でも
    parser にも decoder にも届かない。"""
    parsed: list[int] = []
    decoded: list[int] = []
    real_parse = mcp_spool.parse_request
    real_loads = json.loads
    monkeypatch.setattr(mcp_spool, "parse_request",
                        lambda raw: parsed.append(1) or real_parse(raw))
    monkeypatch.setattr(json, "loads",
                        lambda *a, **k: decoded.append(1) or real_loads(*a, **k))
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls, start=False)
    rid = client.new_request_id()
    fd = os.open(d.spool_dir / ("rtmp-" + rid), os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    _place(d.spool_dir, body, rid=rid)
    d.start()
    _wait(lambda: _events(d, rid, "response_tmp_preempted"),
          what="rtmp preemption")
    assert parsed == [] and decoded == []
    assert calls == []
    assert _events(d, rid, "rejected") == []


def test_ledger_distinguishes_tool_execution_from_other_requests(
        tmp_path, dispatchers):
    """tool を実行したことの正本は `tool_executed` だけ。一覧・拒否・
    未知 method は実行に数えない。"""
    calls: list = []
    d = _make(dispatchers, tmp_path, calls=calls)
    cases = {
        "list": ({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                 "served"),
        "init": ({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18"}}, "served"),
        "bad_init": ({"jsonrpc": "2.0", "id": 3, "method": "initialize",
                      "params": {"protocolVersion": "1999-01-01"}},
                     "rejected"),
        "denied": (_call_req(rpc_id=4, name="never_allowed"), "rejected"),
        "unknown": ({"jsonrpc": "2.0", "id": 5, "method": "nope"},
                    "rejected"),
        "call": (_call_req(rpc_id=6, x=9), "tool_executed"),
    }
    rids = {k: _place(d.spool_dir, json.dumps(req).encode())
            for k, (req, _) in cases.items()}
    for k, rid in rids.items():
        _wait(lambda rid=rid: (d.spool_dir / ("resp-" + rid)).exists(),
              what=f"response for {k}")
    for k, (_, expected) in cases.items():
        kinds = {e.kind for e in _events(d, rids[k])}
        assert expected in kinds, k
        assert ("tool_executed" in kinds) == (expected == "tool_executed"), k
    assert d.ledger.tool_executed_ids() == [rids["call"]]
    assert calls == [9]


# --- dispatcher thread の異常終了 / 拒否応答の id / publish の失敗 -----------


def test_unexpected_scan_error_fails_the_dispatcher(tmp_path, dispatchers):
    fatal: list[str] = []
    d = _make(dispatchers, tmp_path, start=False, on_fatal=fatal.append)

    def boom() -> None:
        raise OSError(24, "Too many open files")

    d.scan_once = boom
    d.start()
    _wait(lambda: d.failure_reason is not None, what="dispatcher failure")
    assert d.failure_reason == mcp_spool.SPOOL_DISPATCHER_DIED
    assert fatal == [mcp_spool.SPOOL_DISPATCHER_DIED]
    d.thread.join(5)
    assert not d.thread.is_alive()


def test_unexpected_claim_error_fails_the_dispatcher(
        monkeypatch, tmp_path, dispatchers):
    fatal: list[str] = []
    d = _make(dispatchers, tmp_path, start=False, on_fatal=fatal.append)

    def boom(rid: str) -> bool:
        raise RuntimeError("claim exploded")

    monkeypatch.setattr(d, "claim_and_process", boom)
    d.start()
    _place(d.spool_dir, json.dumps(_call_req()).encode())
    _wait(lambda: fatal == [mcp_spool.SPOOL_DISPATCHER_DIED],
          what="dispatcher failure")


class _DyingDispatcherRunner(_FloodingRunner):
    """dispatcher の scan を壊して thread を落とし、worker が mission
    failure に置き換えることを見る。"""

    def run(self, mission):
        from agentic_fx.runners.base import MissionResult

        def boom() -> None:
            raise OSError(5, "I/O error")

        self._afx_mcp_dispatcher.scan_once = boom
        _wait(lambda: self._afx_mcp_dispatcher.failure_reason is not None,
              what="dispatcher failure")
        abort_event = self._kw.get("abort_event")
        _FloodingRunner.observed["abort_set"] = (
            None if abort_event is None else abort_event.wait(5))
        reason_fn = self._kw.get("abort_reason_fn")
        _FloodingRunner.observed["abort_reason"] = (
            None if reason_fn is None else reason_fn())
        return MissionResult("completed", {"action": "no_trade"}, [],
                             reason=None)


@pytest.mark.parametrize("profile", ["improve", "trade"])
def test_dispatcher_death_fails_the_mission(monkeypatch, tmp_path, profile):
    from tests.test_mission_worker_protocol import _drive_main
    import agentic_fx.mission_worker as mw

    _FloodingRunner.observed = {}
    monkeypatch.setattr(mw.runner_factory, "build_runner",
                        lambda *a, **kw: _DyingDispatcherRunner(kw))
    if profile == "improve":
        monkeypatch.setattr(mw, "_bootstrap_improve_profile",
                            lambda *a, **kw: None)
        overrides = {"worker_profile": "improve", "db_path": None,
                     "plugins_dir": None, "mission_id": "m-died",
                     "staging_dir": str(tmp_path / "staging" / "m-died"),
                     "source_snapshot_dir": str(tmp_path / "source")}

        def mutate(d):
            d["runner"]["improve"]["backend"] = "claude"
    else:
        overrides = None

        def mutate(d):
            d["runner"]["trade"]["backend"] = "claude"

    frames, _, _ = _drive_main(monkeypatch, tmp_path,
                               handshake_overrides=overrides,
                               settings_mutator=mutate)
    result = frames[-1]
    assert result["type"] == "result"
    assert result["status"] == "failed"
    assert result["reason"] == mcp_spool.SPOOL_DISPATCHER_DIED
    assert _FloodingRunner.observed["abort_set"] is True
    assert (_FloodingRunner.observed["abort_reason"]
            == mcp_spool.SPOOL_DISPATCHER_DIED)


def test_rejected_response_carries_the_request_id_through_shim(
        tmp_path, dispatchers):
    """dispatcher は拒否した要求の id を読んでいないので、shim が自分の
    要求の id を応答に補う。"""
    d = _make(dispatchers, tmp_path)
    proc = _spawn_shim(d.spool_dir)
    try:
        # 4 MiB 超。
        huge = _call_req(rpc_id=7)
        huge["pad"] = "a" * (mcp_spool.MAX_REQUEST_BYTES + 10)
        resp = _shim_rpc(proc, huge)
        assert resp["id"] == 7
        assert "request rejected: oversized" in resp["error"]["message"]
        # 深さ上限超 (shim 自身の json.loads は通る深さ)。
        deep = _call_req(rpc_id="abc")
        deep["pad"] = json.loads(("[" * 40) + ("]" * 40))
        resp = _shim_rpc(proc, deep)
        assert resp["id"] == "abc"
        assert "json_too_deep" in resp["error"]["message"]
        # 通常の応答は影響を受けない。
        resp = _shim_rpc(proc, _call_req(rpc_id=9))
        assert resp["id"] == 9 and "result" in resp
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_failed_publish_cleans_up_and_is_recorded(
        monkeypatch, tmp_path, dispatchers):
    """応答の rename が失敗したら rtmp を残さず publish_failed を記録し、
    次の要求を処理する。"""
    calls: list = []
    real = mcp_spool.renameat2_noreplace
    failed: list[str] = []

    def flaky(dirfd, old, new):
        if old.startswith(client.RTMP_PREFIX) and not failed:
            failed.append(old)
            raise OSError(5, "I/O error", old)
        return real(dirfd, old, new)

    monkeypatch.setattr(mcp_spool, "renameat2_noreplace", flaky)
    d = _make(dispatchers, tmp_path, calls=calls)
    rid = _place(d.spool_dir, json.dumps(_call_req(x=1)).encode())
    _wait(lambda: _events(d, rid, "publish_failed"), what="publish_failed")
    assert not (d.spool_dir / ("rtmp-" + rid)).exists()
    assert not (d.spool_dir / ("resp-" + rid)).exists()
    nxt = _place(d.spool_dir, json.dumps(_call_req(x=2)).encode())
    _wait(lambda: (d.spool_dir / ("resp-" + nxt)).exists(), what="next response")


def test_unserializable_response_cleans_up_and_is_recorded(
        tmp_path, dispatchers):
    root = mcp_spool.SpoolRoot.create(tmp_path, client.new_request_id())

    def handler(request, publish):
        publish({"jsonrpc": "2.0", "id": request["id"], "result": {1, 2}})

    d = mcp_spool.SpoolDispatcher(root, handler)
    dispatchers.append(d)
    d.start()
    rid = _place(d.spool_dir, json.dumps(_call_req()).encode())
    _wait(lambda: _events(d, rid, "publish_failed"), what="publish_failed")
    assert not (d.spool_dir / ("rtmp-" + rid)).exists()
    assert not (d.spool_dir / ("resp-" + rid)).exists()
