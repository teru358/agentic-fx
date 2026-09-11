"""ローカル 2 周目 pin (2026-09-11): `dispatcher_loop` の Thread.start 失敗
経路 — 「予約解放 + rpc_start_failed」だけでは守れていなかった 2 点
(#W2 dispatcher の継続、#W3 応答 frame の seq) を別ファイルに固定する。

別ファイルに置く理由: 同時刻に `test_worker_runner.py` を編集中の agent が
いるため末尾追記を避けた。helper は同ディレクトリの本体から import する。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.mission_protocol import write_frame
from agentic_fx.runners import worker_runner as wr_mod
from agentic_fx.runners.base import Mission
from agentic_fx.runners.worker_runner import WorkerRunner

from tests.runners.test_worker_runner import (  # noqa: E402
    _fake_improve_proc, _rag, _root, _tiny_worker_settings,
)

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _boom_thread(monkeypatch):
    """`afx-rag-rpc` スレッドの start だけを失敗させる。"""
    real_thread = wr_mod.threading.Thread

    class _Boom(real_thread):
        def start(self):
            if self.name == "afx-rag-rpc":
                raise RuntimeError("can't start new thread")
            return super().start()

    monkeypatch.setattr(wr_mod.threading, "Thread", _Boom)


def test_rpc_thread_start_failure_keeps_dispatcher_alive_for_next_rpc(
        tmp_path, monkeypatch):
    """ローカル 2 周目 #W2 (2026-09-11): Thread.start 失敗の枝は `continue`
    でなければならない (`return` にすると dispatcher が死に、子は次の RPC
    応答を永遠に待って mission が timeout する)。既存の
    `test_rpc_thread_start_failure_releases_reservation` は frame を 1 本しか
    送らないため、`continue` → `return` の変異を緑で通していた (実測
    SURVIVED)。2 本目の RPC にも応答が返り mission が completed することで
    「dispatcher が生きている」を観測する。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()
    responses: list[dict] = []

    def child() -> None:
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())  # handshake
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        json.loads(child_in.readline())  # go frame (improve profile、親→子 seq=2)
        for seq, rpc_id in ((2, "r1"), (3, "r2")):
            write_frame(child_out, {
                "type": "tool_rpc", "seq": seq, "rpc_id": rpc_id,
                "name": "run_backtest", "args": {"name": "x",
                                                 "pair": "USDJPY"}})
            line = child_in.readline()
            if not line:  # dispatcher が死んだ (変異時)
                break
            responses.append(json.loads(line))
        write_frame(child_out, {"type": "result", "seq": 4,
                                "status": "completed", "output": {}})
        child_out.close()

    fake_proc = _fake_improve_proc(r, w, w2, r2)
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)
    _boom_thread(monkeypatch)
    runner = WorkerRunner(
        root=_root(tmp_path), settings=_tiny_worker_settings(),
        clock=FixedClock(NOW), rag=_rag(tmp_path), worker_profile="improve",
        rpc_handlers={"run_backtest": lambda args: {"v": 1}},
        on_rpc_begin=lambda name: True, on_rpc_released=lambda name: None,
    )
    thread = threading.Thread(target=child, daemon=True)
    thread.start()
    result = runner.run(Mission(prompt="hi", tools=[],
                                output_schema={"type": "object"},
                                max_turns=1, timeout_sec=5.0))
    thread.join(3.0)

    # 2 本目にも応答が返る = dispatcher が continue した
    assert [f["rpc_id"] for f in responses] == ["r1", "r2"]
    assert all(f["ok"] is False and f["error"] == "rpc_start_failed"
               for f in responses)
    # 親→子方向の seq は handshake(1) → go(2) の次から連番 (#W3 と同じ契約)
    assert [f["seq"] for f in responses] == [3, 4]
    assert result.status == "completed"


def test_rpc_start_failed_frame_seq_follows_out_seq(tmp_path, monkeypatch):
    """ローカル 2 周目 #W3 (2026-09-11): `rpc_start_failed` 応答の `seq` も
    親の `out_seq_holder` を進めた値でなければならない。子側は
    `mission_worker._make_rpc_client` (:455、`run_backtest`/`analyze_corr`
    の経路 = この枝が実際に返す相手) と `_RagRpcProxy._call` (:374) の
    両方で `in_seq.check(response.get("seq"))` を通すため、ここを定数や
    据え置きにすると本番の子は `ProtocolError` で mission を落とす。既存テストは `ok`/`error` しか見て
    おらず、seq を 99 に固定する変異が緑で通っていた (実測 SURVIVED)。
    improve profile の親→子方向は handshake(seq=1) → go(seq=2) を消費済み
    なので、初回 RPC 応答は seq=3 (連番の次) でなければならない。"""
    _boom_thread(monkeypatch)
    from tests.runners.test_worker_runner import _run_fake_tool_rpc
    _, response = _run_fake_tool_rpc(
        tmp_path, monkeypatch, handler=lambda args: {"v": 1},
        on_rpc_begin=lambda name: True, on_rpc_released=lambda name: None)
    assert response["error"] == "rpc_start_failed"
    assert response["seq"] == 3
