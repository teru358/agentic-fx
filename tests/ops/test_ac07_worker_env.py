"""AC-7: 実 daemon と、本番の env 構築経路で起こした worker に鍵文字列が無い。

- 実 daemon (``afx --daemon`` 相当、api.enabled=true) を起動し、鍵で要求を通した後の
  ``/proc/<pid>/environ``・``cmdline``・技術 log・activity・標準出力を調べる。
- plugin worker (PluginSession)、改善 worker (WorkerRunner と同じ起動形)、gate pytest
  worker (run_gate_pytest) を起こし、各 worker の env を調べる。起こす側の env に鍵文字列を
  わざと置き、env 構築が親 env を引き継がないことを観測する。
"""
from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from agentic_fx.core.landlock import is_available
from agentic_fx.ops import keys
from agentic_fx.ops.contracts import Principal

from ._daemon import Daemon, base_env, init_root, socket_path

CHILD = Path(__file__).with_name("_worker_env_child.py")


def _client(root: Path, token: str) -> httpx.Client:
    return httpx.Client(transport=httpx.HTTPTransport(uds=str(socket_path(root))),
                        base_url="http://afx", timeout=15,
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Length": "0"})


def test_ac7_real_daemon_env_cmdline_log_activity_and_stdout_hold_no_key(tmp_path):
    root = init_root(tmp_path / "root")
    daemon = Daemon(root)
    try:
        daemon.wait_listening()
        directory = keys.key_dir(root)
        tokens = [keys.read_token(directory, p) for p in Principal]
        statuses = []
        for token in tokens:
            with _client(root, token) as client:
                statuses.append(client.get("/v1/whoami").status_code)
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket_path(root))),
                          base_url="http://afx", timeout=15,
                          headers={"Authorization": f"Bearer {tokens[0]}"}) as client:
            statuses.append(client.post("/v1/policy", json={"text": "keep risk small"},
                                        headers={"Idempotency-Key": "k1"}).status_code)
        environ = Path(f"/proc/{daemon.pid}/environ").read_bytes()
        cmdline = Path(f"/proc/{daemon.pid}/cmdline").read_bytes()
    finally:
        assert daemon.stop() == 0
    # 正しい鍵の要求が実際に通っている (拒否だけを見て緑にしない)
    assert statuses == [200, 200, 200]
    places = {
        "environ": environ, "cmdline": cmdline,
        "agentic.log": (root / "logs" / "agentic.log").read_bytes(),
        "activity.log": (root / "logs" / "activity.log").read_bytes(),
        "stdout": daemon.output.read_bytes(),
    }
    assert all(places.values()), [name for name, blob in places.items() if not blob]
    leaks = sorted(name for name, blob in places.items()
                   if any(token.encode() in blob for token in tokens))
    assert leaks == []


@pytest.mark.skipif(not is_available(), reason="Landlock not available")
def test_ac7_worker_env_builders_drop_a_key_present_in_the_parent_env(tmp_path):
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    tokens = [keys.read_token(directory, p) for p in Principal]
    env = base_env()
    # 起こす側の env に鍵がある最悪の場合を作る (本番では起きない)。
    env["AFX_LEAK_SENTINEL"] = tokens[0]
    env["AFX_LEAK_SENTINEL_2"] = tokens[1]
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "environs.json"
    result = subprocess.run([sys.executable, str(CHILD), str(work), str(out)], env=env,
                            capture_output=True, text=True, timeout=180,
                            stdin=subprocess.DEVNULL,
                            preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE,
                                                                  (0, 0)))
    assert result.returncode == 0, result.stderr[-4000:]
    environs = json.loads(out.read_text())
    # 起こす側には鍵がある (対照)。worker には 1 つも渡っていない。
    assert tokens[0] in environs["self"]
    for name in ("plugin_worker", "improve_worker", "gate_pytest_worker"):
        blob = environs[name]
        assert "PATH=" in blob, name  # worker 自身の env を読めている
        assert not any(token in blob for token in tokens), name
        assert "AFX_LEAK_SENTINEL" not in blob, name
