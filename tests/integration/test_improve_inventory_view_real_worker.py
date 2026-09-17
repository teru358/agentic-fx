"""[indicator-consumption-wiring] inventory view の実プロセス透通 E2E。

段 0 束 3 の改善提案 4 / codex r1 束 3 Minor の採用。親が `prepare()` 時に
作った `inventory_view` が、

  handshake (`worker_runner`) → `mission_worker.main()` →
  `_run_improve_mission` → `_build_improve_registry` →
  `improve_staging_tools.list_deployed_plugins`

の 4 層を通って**実 worker プロセスの中の tool 応答**として返ることを 1 本で
観測する。各層は unit / AST で個別に pin 済みだが、handshake の**キー名**・
引数名・registry 登録の接続ずれはどの層の pin にも映らない (段 0 M2 の
構造 pin の限界、段 0 6 節末尾)。

**LLM は使わない** — improve worker は `ready` を送る前に MCP dispatcher を
`workdir/afx.sock` へ同期 bind する (`_start_mcp_dispatcher`) ので、親から
その socket へ JSON-RPC `tools/call` を直接投げれば registry 経由で tool を
実行できる。実 llama-swap にも `go` にも触らない。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_fx.core.landlock import is_available as landlock_available

_REPO_ROOT = Path(__file__).resolve().parents[2]

# 親の `prepare()` が作る形 (`build_inventory_view` の出力と同じ 6 キー)。
_VIEW = {
    "plugins": [
        {"name": "rsi_wilder", "kind": "indicator", "pairs": [],
         "params": {"period": 14}, "outputs": ["rsi"],
         "content_hash": "a" * 64},
        {"name": "legacy", "kind": "indicator", "pairs": [],
         "params": {"period": 9}, "outputs": None, "content_hash": "b" * 64},
    ],
    "pin_broken_strategies": [
        {"name": "s_old", "alias": "rsi", "reason": "pin_mismatch"}],
}


def _rpc(sock_path: Path, payload: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(15.0)
        s.connect(str(sock_path))
        s.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode())


def test_real_improve_worker_serves_the_prepare_time_inventory_view(tmp_path):
    if not landlock_available():
        pytest.skip("Landlock not available on this kernel/architecture")

    from agentic_fx.config import load_settings
    from agentic_fx.core.mission_protocol import read_frame, write_frame
    from agentic_fx.mission_worker import mcp_socket_path
    from tests.conftest import _LLAMA_SWAP_UNREACHABLE_URL

    settings = load_settings(_REPO_ROOT / "config" / "settings.yaml.example")
    # ネットワーク隔離 (実 worker を直に Popen するので tests/conftest.py の
    # `WorkerRunner.run` pin は届かない)。この経路では `go` を送らないので
    # runner.run 自体に到達しないが、静かに漏れる穴を塞いでおく。
    settings = settings.model_copy(update={
        "llama_swap": settings.llama_swap.model_copy(
            update={"base_url": _LLAMA_SWAP_UNREACHABLE_URL}),
    })

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    staging = workdir / "staging" / "iw-view-probe"
    staging.mkdir(parents=True, mode=0o700)
    source_snapshot = workdir / "source"
    source_snapshot.mkdir(mode=0o500)

    # 「prepare 後に live plugins/ を変えても view のまま」の対照。**判別力の
    # 限界を明示しておく**: improve worker は handshake の `plugins_dir` が
    # `None` で、Landlock で plugins/ へ到達もできない。したがって下の ③ は
    # 配線がどう壊れていても落ちない (構造的に不可能な混入の確認)。**本テスト
    # の判別力は ① (`out == _VIEW`) にある** — handshake キー名・引数名・
    # registry 登録のいずれかがずれれば ① だけが落ちる (変異確認済)。
    live_plugins = tmp_path / "plugins"
    (live_plugins / "added_after_prepare").mkdir(parents=True)
    (live_plugins / "added_after_prepare" / "config.yaml").write_text(
        "kind: indicator\noutputs: [x]\n")
    # staging の候補も同様に view へは現れてはならない (U4b / 遮断 8)。
    (staging / "config.yaml").write_text("kind: strategy\n")

    proc = subprocess.Popen(
        [sys.executable, "-m", "agentic_fx.mission_worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=str(workdir), start_new_session=True)
    try:
        write_frame(proc.stdin, {
            "type": "handshake", "seq": 1,
            "expected_parent_pid": os.getpid(),
            "db_path": None, "plugins_dir": None,
            "settings": settings.model_dump(),
            "mission": {"prompt": "test", "tools": [],
                        "output_schema": {"type": "object"},
                        "max_turns": 1, "timeout_sec": 30},
            "worker_profile": "improve",
            "mission_id": "iw-view-probe",
            "staging_dir": str(staging),
            "source_snapshot_dir": str(source_snapshot),
            "inventory_view": _VIEW,
            "now": "2026-09-17T00:00:00+00:00",
        })
        frame = read_frame(proc.stdout)
        assert frame is not None, proc.stderr.read(4096)
        assert frame["type"] == "ready" and frame.get("ok") is True, (
            f"{frame} stderr={proc.stderr.read(4096) if proc.stderr else ''}")

        sock_path = mcp_socket_path(workdir)
        resp = _rpc(sock_path, {
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "list_deployed_plugins", "arguments": {}}})
        assert "error" not in resp, resp
        out = json.loads(resp["result"]["content"][0]["text"])

        # ① handshake の inventory_view が逐語で届いている (キー名・引数名・
        #    registry 登録のどこか 1 つでもずれれば、ここで落ちる)
        assert out == _VIEW, out
        # ② outputs=None は空リストへ潰されず区別が残る (U4 の依存可否)
        by_name = {p["name"]: p for p in out["plugins"]}
        assert by_name["legacy"]["outputs"] is None
        assert by_name["rsi_wilder"]["outputs"] == ["rsi"]
        # ③ live plugins/ と staging 候補は view に現れない (上記のとおり
        #    この 3 行は判別力を持たない — 契約の読み手向けの明示)
        names = set(by_name)
        assert names == {"rsi_wilder", "legacy"}
        assert "added_after_prepare" not in names
        assert "iw-view-probe" not in names
        # ④ 許可キーは 6 種のみ (遮断 8 — holdout 数値・段名の同乗を防ぐ)
        for entry in out["plugins"]:
            assert set(entry) == {"name", "kind", "pairs", "params",
                                  "outputs", "content_hash"}
    finally:
        proc.kill()
        proc.wait(timeout=10)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
