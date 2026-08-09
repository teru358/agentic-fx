"""WorkerRunner — Mission 実行を preemption 可能な使い捨て子プロセスへ
隔離する AgentRunner 実装 (プラン8, 設計書 §3.1/§4.1/§4.3/§4.7)。

親→子の spawn/handshake、子→親の event/tool_rpc/result 受信、
timeout エスカレーション (SIGTERM→grace→SIGKILL) をすべてこのクラスの
`run()` 呼び出し 1 回に閉じる。preemption 後も `MissionResult(status=
"timeout")` を返し、既存の 4 終端契約 (completed/failed/timeout/
max_turns) に in-band で乗る (呼び出し元は他の AgentRunner 実装と
区別なく扱える)。
"""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from agentic_fx.core.contracts import Clock
from agentic_fx.core.mission_protocol import (
    ProtocolError, SeqTracker, read_frame, write_frame,
)
from agentic_fx.plugin.sandbox import _build_env
from agentic_fx.runners.base import AgentRunner, Mission, MissionResult
from agentic_fx.store.rag import Rag

import logging

_log = logging.getLogger("agentic_fx.worker_runner")

# IM-3/P8-03 対応 (裁定書 F-9): trade profile の子だけに渡すデータ
# プロバイダ資格情報の明示 allowlist。
_DATA_PROVIDER_ENV_ALLOWLIST = ("TWELVEDATA_API_KEY", "MT5_BRIDGE_API_KEY")


def _mission_worker_env(worker_profile: str) -> dict[str, str]:
    """mission worker 専用の env builder (IM-3/P8-03 対応、裁定書 F-9)。

    `plugin/sandbox.py:_build_env()` (`PATH`/`PYTHONPATH`/
    `PYTHONSAFEPATH`/`_SINGLE_THREAD_ENV` のみの最小 env、`AFX_*` 等は
    継承しない) をそのまま mission worker にも流用すると、
    `price_provider.py`/`sources.py` が読む `TWELVEDATA_API_KEY`/
    `MT5_BRIDGE_API_KEY` が子に渡らず、MT5/TwelveData を有効化した
    構成で trade worker の市場データ取得ツールが恒常的に認証失敗する
    (レビュー IM-3/P8-03)。`worker_profile == "trade"` のときだけ、
    この 2 キーを親プロセスの環境から明示 allowlist で追加する。
    `AFX_*`/`ANTHROPIC_*` 等は引き続き除外 (継承しない)。improve
    profile では資格情報も渡さない (裁定書 F-9 — 遮断維持)。
    """
    env = _build_env()
    if worker_profile == "trade":
        for key in _DATA_PROVIDER_ENV_ALLOWLIST:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
    return env


class WorkerRunner(AgentRunner):
    def __init__(self, *, root: Path, settings, clock: Clock, rag: Rag,
                worker_profile: str = "trade",
                on_rpc_leak: Callable[[], None] | None = None,
                stop_event: threading.Event | None = None) -> None:
        self._root = root
        self._settings = settings
        self._clock = clock
        self._rag = rag
        self._worker_profile = worker_profile
        self._on_rpc_leak = on_rpc_leak
        self._stop_event = stop_event

    def close(self) -> None:
        """no-op — 各 Mission が自分の子プロセスを spawn/reap するため
        永続資源を持たない (LocalRunner との対称性のための空実装)。"""

    def run(self, mission: Mission) -> MissionResult:
        w = self._settings.worker
        with tempfile.TemporaryDirectory(prefix="afx-mission-") as workdir:
            proc = subprocess.Popen(
                [sys.executable, "-m", "agentic_fx.mission_worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=workdir,
                env=_mission_worker_env(self._worker_profile),
                start_new_session=True)
            return self._run_with_child(proc, mission, w)

    # ---- 内部 -------------------------------------------------------

    def _run_with_child(self, proc, mission: Mission, w) -> MissionResult:
        stdin_lock = threading.Lock()
        stdin_state = {"closed": False}
        in_seq = SeqTracker()  # 子→親方向の受信検証
        ready_queue: "queue.Queue[dict]" = queue.Queue(maxsize=1)
        done_queue: "queue.Queue[tuple[str, object]]" = queue.Queue(maxsize=1)
        dispatch_queue: "queue.Queue[dict]" = queue.Queue()
        transcript: list[dict] = []
        state = {"bytes": 0, "truncated": False}

        def handle_event(frame: dict) -> None:
            if state["truncated"]:
                return
            msg = frame["message"]
            size = len(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
            if state["bytes"] + size > w.transcript_max_bytes:
                transcript.append({"role": "system",
                                   "content": "[transcript truncated: "
                                              "exceeded transcript_max_bytes]"})
                state["truncated"] = True
                return
            transcript.append(msg)
            state["bytes"] += size

        def reader_loop() -> None:
            try:
                while True:
                    frame = read_frame(proc.stdout)
                    if frame is None:
                        done_queue.put(("eof", None))
                        return
                    try:
                        in_seq.check(frame.get("seq"))
                    except ProtocolError as e:
                        done_queue.put(("protocol_error", str(e)))
                        return
                    ftype = frame.get("type")
                    if ftype == "ready":
                        ready_queue.put(frame)
                    elif ftype == "event":
                        handle_event(frame)
                    elif ftype == "tool_rpc":
                        dispatch_queue.put(frame)
                    elif ftype == "result":
                        done_queue.put(("result", frame))
                        return
                    else:
                        done_queue.put(
                            ("protocol_error", f"unknown frame type {ftype!r}"))
                        return
            except Exception as e:  # noqa: BLE001 — reader は死なせない代わりに報告する
                done_queue.put(("error", str(e)))

        def dispatcher_loop() -> None:
            out_seq_holder = {"n": 0}
            while True:
                frame = dispatch_queue.get()
                if frame is None:  # shutdown 合図
                    return
                # FC-1 対応: concurrent.futures.ThreadPoolExecutor は使わない
                # (atexit ハンドラがワーカースレッドの終了を待つため、
                # リークしたまま残ると sys.exit()/非ゼロ終了そのものを
                # 無期限にブロックする — レビュー FC-1、実測で再現確認済み)。
                # RAG 呼び出しごとに使い捨ての daemon スレッドを spawn し、
                # 結果は queue.Queue 経由で受け取る。daemon スレッドは
                # interpreter 終了時に join されないため、リークしても
                # プロセスは正常に (非ゼロ) 終了できる。
                result_queue: "queue.Queue[tuple[bool, object]]" = (
                    queue.Queue(maxsize=1))

                def _rpc_worker(name=frame["name"], args=frame["args"]) -> None:
                    try:
                        result_queue.put((True, self._call_rag(name, args)))
                    except Exception as e:  # noqa: BLE001 — 子へ tool error として返す
                        result_queue.put((False, str(e)))

                threading.Thread(target=_rpc_worker, daemon=True,
                                 name="afx-rag-rpc").start()
                try:
                    ok, payload = result_queue.get(timeout=w.rpc_timeout_sec)
                    response = ({"ok": True, "result": payload} if ok
                               else {"ok": False, "error": payload})
                except queue.Empty:
                    _log.error("RAG RPC leaked past rpc_timeout_sec=%s "
                              "(name=%s) — this thread will never be "
                              "reclaimed but will not block process exit "
                              "(daemon thread — 设計書 §4.3 codex I3-1, "
                              "レビュー FC-1)",
                              w.rpc_timeout_sec, frame["name"])
                    if self._on_rpc_leak is not None:
                        try:
                            self._on_rpc_leak()
                        except Exception:  # noqa: BLE001
                            _log.exception("on_rpc_leak callback failed")
                    response = {"ok": False, "error": "rag rpc timed out"}
                out_seq_holder["n"] += 1
                try:
                    with stdin_lock:
                        if stdin_state["closed"]:
                            return
                        write_frame(proc.stdin, {
                            "type": "tool_rpc_result",
                            "seq": out_seq_holder["n"] + 1,  # handshake=1 済み
                            "rpc_id": frame["rpc_id"], **response})
                except (BrokenPipeError, OSError):
                    return  # 子が既に死んでいる — 応答不能

        reader = threading.Thread(target=reader_loop, daemon=True,
                                  name="afx-worker-reader")
        dispatcher = threading.Thread(target=dispatcher_loop, daemon=True,
                                      name="afx-worker-dispatcher")
        reader.start()
        dispatcher.start()

        now = self._clock.now()
        handshake = {
            "type": "handshake", "seq": 1,
            "expected_parent_pid": os.getpid(),
            "db_path": (str(self._root / "data" / "agentic.db")
                       if self._worker_profile == "trade" else None),
            "plugins_dir": (str(self._root / "plugins")
                            if self._worker_profile == "trade" else None),
            "settings": self._settings.model_dump(),
            "mission": {"prompt": mission.prompt, "tools": mission.tools,
                       "output_schema": mission.output_schema,
                       "max_turns": mission.max_turns,
                       "timeout_sec": mission.timeout_sec},
            "worker_profile": self._worker_profile,
            "now": now.isoformat(),
        }
        with stdin_lock:
            write_frame(proc.stdin, handshake)

        status = "failed"
        output = None
        try:
            try:
                ready = self._wait_with_stop(
                    ready_queue, timeout=w.worker_startup_timeout_sec)
                if not ready.get("ok", False):
                    status = "failed"
                    return MissionResult(status, None, transcript)
            except queue.Empty:
                self._escalate_kill(proc, w)
                return MissionResult("failed", None, transcript)

            deadline_budget = mission.timeout_sec + w.worker_grace_sec
            try:
                kind, payload = self._wait_with_stop(
                    done_queue, timeout=deadline_budget)
            except queue.Empty:
                self._escalate_kill(proc, w)
                return MissionResult("timeout", None, transcript)

            if kind == "result":
                status = payload["status"]
                output = payload.get("output")
            else:  # eof / protocol_error / error — すべて failed に正規化
                status = "failed"
                output = None
            return MissionResult(status, output, transcript)
        finally:
            dispatch_queue.put(None)
            self._ensure_dead(proc, w)
            reader.join(timeout=5.0)
            # IM-7 対応: dispatcher は `with stdin_lock: write_frame(proc.stdin,
            # ...)` を実行し得る (tool_rpc_result 応答の送出中)。ここで
            # stdin_lock の外から proc.stdin.close() すると、dispatcher が
            # まさに書込中の瞬間と競合し得る (設計書 §4.3「writer は
            # 2 時点で排他」違反 — レビュー IM-7)。dispatcher を先に join
            # する (dispatcher 自身は `result_queue.get(timeout=
            # w.rpc_timeout_sec)` で必ず打ち切られるため — FC-1 対応の
            # daemon スレッドがリークしても、dispatcher 自体は有限時間で
            # `dispatch_queue.get()` に戻り、None sentinel を見て return
            # する — 有界待ち)。
            dispatcher.join(timeout=w.rpc_timeout_sec + 5.0)
            with stdin_lock:
                stdin_state["closed"] = True
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.stdout.close()
            except OSError:
                pass

    def _wait_with_stop(self, q: queue.Queue, *, timeout: float,
                        poll_interval: float = 0.2):
        deadline = time.monotonic() + timeout
        while True:
            if self._stop_event is not None and self._stop_event.is_set():
                raise queue.Empty
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise queue.Empty
            try:
                return q.get(timeout=min(poll_interval, remaining))
            except queue.Empty:
                continue

    def _call_rag(self, name: str, args: dict):
        method = getattr(self._rag, name)
        return method(**args)

    def _escalate_kill(self, proc, w) -> None:
        """SIGTERM → `worker_terminate_grace_sec` → SIGKILL (設計書 §4.7)。"""
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        deadline = time.monotonic() + w.worker_terminate_grace_sec
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc.poll() is None:
            self._kill(proc)

    def _ensure_dead(self, proc, w) -> None:
        """finally 節: どの終了経路でも子が生きていれば確実に殺す。
        既に `_escalate_kill` が呼ばれていれば `proc.poll()` は None
        ではないため no-op。"""
        if proc.poll() is None:
            self._kill(proc)

    def _kill(self, proc) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
