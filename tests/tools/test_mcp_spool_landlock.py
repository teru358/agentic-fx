"""spool の rename を improve の Landlock rw マスク (REFER 無し) の下の
実子 process で観測する。Landlock は不可逆なので必ず子 process で掛ける。
"""
from __future__ import annotations

import json
import os
import resource
import subprocess
import sys

import pytest

from agentic_fx.core import landlock
from agentic_fx.tools import mcp_spool
from agentic_fx.tools import mcp_spool_client as client
from agentic_fx.tools.mcp_shim import McpShimDispatcher
from agentic_fx.tools.registry import ToolDef, ToolRegistry

pytestmark = pytest.mark.skipif(
    not landlock.is_available(), reason="Landlock が無い kernel")


def _no_core() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


_RENAME_PROBE = r"""
import json, os, sys
from pathlib import Path
from agentic_fx.core import landlock
from agentic_fx.tools import mcp_spool_client as client

spool = Path(sys.argv[1])
landlock.restrict_to(read_only_paths=[], read_write_paths=[spool])
out = {}
dfd = os.open(spool, os.O_RDONLY | os.O_DIRECTORY)
sub = os.open(spool / "sub", os.O_RDONLY | os.O_DIRECTORY)

def attempt(key, fn):
    try:
        fn()
        out[key] = "ok"
    except OSError as e:
        out[key] = os.strerror(e.errno)

def create(name):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=dfd)
    os.write(fd, b"{}")
    os.fsync(fd)
    os.close(fd)

attempt("create_tmp", lambda: create("tmp-a"))
attempt("publish_noreplace",
        lambda: client.renameat2_noreplace(dfd, "tmp-a", "req-a"))
attempt("claim_noreplace",
        lambda: client.renameat2_noreplace(dfd, "req-a", "claim-a"))
attempt("create_resp_once", lambda: create("resp-a"))
attempt("create_resp_twice", lambda: create("resp-a"))
attempt("create_x", lambda: create("x"))
attempt("cross_directory_rename",
        lambda: os.rename("x", "x", src_dir_fd=dfd, dst_dir_fd=sub))
sys.stdout.write(json.dumps(out))
"""


def _run_probe(spool) -> dict:
    out = subprocess.run(
        [sys.executable, "-c", _RENAME_PROBE, str(spool)],
        capture_output=True, text=True, timeout=30, preexec_fn=_no_core)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_same_directory_noreplace_works_under_landlock(tmp_path):
    spool = tmp_path / "spool"
    (spool / "sub").mkdir(parents=True)
    r = _run_probe(spool)
    assert r["create_tmp"] == "ok"
    assert r["publish_noreplace"] == "ok"
    assert r["claim_noreplace"] == "ok"
    assert r["create_resp_once"] == "ok"
    assert r["create_resp_twice"] == os.strerror(17)  # EEXIST
    assert sorted(os.listdir(spool)) == ["claim-a", "resp-a", "sub", "x"]


def test_cross_directory_rename_is_exdev_under_landlock(tmp_path):
    spool = tmp_path / "spool"
    (spool / "sub").mkdir(parents=True)
    r = _run_probe(spool)
    assert r["create_x"] == "ok"
    assert r["cross_directory_rename"] == os.strerror(18)  # EXDEV
    assert (spool / "x").exists()
    assert not (spool / "sub" / "x").exists()


_SHIM_UNDER_LANDLOCK = r"""
import sys
from pathlib import Path
from agentic_fx.core import landlock
from agentic_fx.tools.mcp_shim import run_mcp_shim

spool = Path(sys.argv[1])
landlock.restrict_to(read_only_paths=[], read_write_paths=[spool])
run_mcp_shim(spool)
"""


def test_shim_round_trips_under_landlock(tmp_path):
    """spool directory だけに rw を持つ Landlock の下で、実 shim の要求が
    worker 側 dispatcher で実行される。"""
    calls: list = []
    reg = ToolRegistry()
    reg.register(ToolDef(
        "count", "", {"type": "object", "properties": {"x": {"type": "integer"}}},
        lambda x=0: calls.append(x) or {"n": len(calls)}))
    root = mcp_spool.SpoolRoot.create(tmp_path, client.new_request_id())
    d = mcp_spool.SpoolDispatcher(
        root, McpShimDispatcher(registry=reg, allowed=["count"]).respond)
    d.start()
    proc = subprocess.Popen(
        [sys.executable, "-c", _SHIM_UNDER_LANDLOCK, str(root.path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, preexec_fn=_no_core)
    try:
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "count", "arguments": {"x": 6}}}) + "\n")
        proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        proc.stdin.close()
        assert proc.wait(timeout=5) == 0, proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        d.stop()
    assert json.loads(resp["result"]["content"][0]["text"]) == {"n": 1}
    assert calls == [6]
