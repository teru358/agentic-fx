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
import shutil
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

_DATA_PROVIDER_ENV_ALLOWLIST = ("TWELVEDATA_API_KEY", "MT5_BRIDGE_API_KEY")
_MAX_CREDENTIALS_FILE_BYTES = 64 * 1024


def _mission_worker_env(worker_profile: str) -> dict[str, str]:
    """R10-①: trade 資格情報も env では渡さない。全 profile 共通で
    `plugin/sandbox.py:_build_env()` の最小 env のみ返す。"""
    return _build_env()


class _CredentialsCopyError(Exception):
    pass


def _copy_credentials_file(src_path: str, dest: Path) -> None:
    """認証原本を検査してから `dest` へコピーする。通常ファイル
    (symlink 不可)・所有者 == 実 uid・group/other に権限なし・
    サイズ ≤ 64 KiB を満たさなければ `_CredentialsCopyError`。"""
    src = Path(src_path).expanduser()
    try:
        fd = os.open(str(src), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        raise _CredentialsCopyError(f"cannot open credentials file: {e}") from e
    try:
        st = os.fstat(fd)
        if not _stat_is_regular(st):
            raise _CredentialsCopyError("credentials file is not a regular file")
        if st.st_uid != os.getuid():
            raise _CredentialsCopyError("credentials file is not owned by this uid")
        if st.st_mode & 0o077:
            raise _CredentialsCopyError(
                "credentials file is readable/writable by group or other")
        if st.st_size > _MAX_CREDENTIALS_FILE_BYTES:
            raise _CredentialsCopyError("credentials file exceeds size limit")
        data = os.read(fd, st.st_size + 1)
    finally:
        os.close(fd)
    dest.write_bytes(data)
    dest.chmod(0o600)


def _stat_is_regular(st: os.stat_result) -> bool:
    import stat as _stat
    return _stat.S_ISREG(st.st_mode)


class WorkerRunner(AgentRunner):
    def __init__(self, *, root: Path, settings, clock: Clock, rag: Rag,
                worker_profile: str = "trade",
                run_context: object | None = None,
                on_rpc_leak: Callable[[], None] | None = None,
                on_ready: Callable[[dict], None] | None = None,
                rpc_handlers: dict[str, Callable] | None = None,
                stop_event: threading.Event | None = None) -> None:
        self._root = root
        self._settings = settings
        self._clock = clock
        self._rag = rag
        self._worker_profile = worker_profile
        self._run_context = run_context
        self._on_rpc_leak = on_rpc_leak
        self._on_ready = on_ready
        self._rpc_handlers = rpc_handlers
        self._stop_event = stop_event

    def close(self) -> None:
        """no-op — 各 Mission が自分の子プロセスを spawn/reap するため
        永続資源を持たない (LocalRunner との対称性のための空実装)。"""

    def run(self, mission: Mission) -> MissionResult:
        w = self._settings.worker
        with tempfile.TemporaryDirectory(prefix="afx-mission-") as base:
            workdir = Path(base)
            (workdir / "home").mkdir(mode=0o700)
            (workdir / "tmp").mkdir(mode=0o700)
            (workdir / "cfg").mkdir(mode=0o700)

            # 裁定 R-D3: `run_context.source_snapshot_dir` は「スナップ
            # ショットの出所」を指すに留まる (Task 10 が用意する)。子側
            # (`_bootstrap_improve_profile`, R7) は「workdir 配下の実在
            # dir」を要求するため、出所をそのまま handshake へ載せると
            # 本物の run_context では常に fail closed する。workdir 作成
            # 直後に出所を `workdir/source` へ実体化し、handshake には
            # この実体化先を載せる (`ready` の `run_context` 反射も同値)。
            materialized_source: Path | None = None
            if self._run_context is not None:
                origin = Path(str(self._run_context.source_snapshot_dir))
                # `Path.is_dir()` は symlink を辿って解決するため、
                # symlink かどうかは先に独立して検査する (辿った先が実在
                # ディレクトリだと `is_dir()` 単独では symlink を見逃す)。
                if origin.is_symlink() or not origin.is_dir():
                    return MissionResult(
                        "failed", None, [],
                        reason="source_snapshot_dir origin is missing or "
                               "is a symlink (R-D3 fail closed)")
                materialized_source = workdir / "source"
                shutil.copytree(origin, materialized_source, symlinks=False)

            credentials: dict[str, str] = {}
            if self._worker_profile == "trade":
                for key in _DATA_PROVIDER_ENV_ALLOWLIST:
                    value = os.environ.get(key)
                    if value is not None:
                        credentials[key] = value

            # I-1 是正: 認証ファイルのコピーは worker_profile ではなく
            # backend で条件化する (profile 不問)。trade+claude/codex も
            # improve と同じ CLI 認証を必要とするため。
            choice = getattr(self._settings.runner, self._worker_profile, None)
            if choice is not None and choice.backend == "claude":
                try:
                    _copy_credentials_file(
                        self._settings.runner.claude.credentials_file,
                        workdir / "cfg" / ".credentials.json")
                except _CredentialsCopyError:
                    return MissionResult(
                        "failed", None, [],
                        reason="claude credentials copy failed "
                               "(参照: 起動時検査/認証原本の要件)")
            elif (choice is not None and choice.backend == "codex"
                  and self._settings.runner.codex.provider == "chatgpt"):
                try:
                    _copy_credentials_file(
                        self._settings.runner.codex.auth_file,
                        workdir / "cfg" / "auth.json")
                except _CredentialsCopyError:
                    return MissionResult(
                        "failed", None, [],
                        reason="codex auth copy failed")
            elif choice is not None and choice.backend == "opencode":
                try:
                    source_config = Path("~/.config/opencode").expanduser()
                    destination = workdir / "home" / ".config" / "opencode"
                    destination.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(source_config / "node_modules",
                                    destination / "node_modules", symlinks=False)
                    shutil.copy2(source_config / "package.json", destination / "package.json")
                    shutil.copytree(Path("~/.cache/opencode").expanduser(),
                                    workdir / "home" / ".cache" / "opencode",
                                    symlinks=False)
                except (OSError, shutil.Error):
                    return MissionResult("failed", None, [],
                                         reason="opencode runtime assets copy failed")

            run_context_fields: dict[str, object] = {}
            if self._run_context is not None:
                run_context_fields = {
                    # precheck 2026-08-22 pass2: RB2 — mission_id は
                    # ImproveRunContext 上は int が正 (missions_store.start
                    # の戻り値)。子側 (mission_worker.py) は str 前提
                    # (staging_dir の末尾成分と Path.name で比較するため、
                    # Path.name は必ず str) なので、handshake 組み立てで
                    # ここだけ str() を掛ける。
                    "mission_id": str(self._run_context.mission_id),
                    "staging_dir": str(self._run_context.staging_dir),
                    # 裁定 R-D3: 出所ではなく実体化先 (workdir/source) を
                    # 載せる。
                    "source_snapshot_dir": str(materialized_source),
                }

            proc = subprocess.Popen(
                [sys.executable, "-m", "agentic_fx.mission_worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=str(workdir),
                env=_mission_worker_env(self._worker_profile),
                start_new_session=True)
            return self._run_with_child(
                proc, mission, w, credentials=credentials,
                run_context_fields=run_context_fields)

    # ---- 内部 -------------------------------------------------------

    def _run_with_child(self, proc, mission: Mission, w,
                        credentials: dict[str, str] | None = None,
                        run_context_fields: dict[str, object] | None = None) -> MissionResult:
        stdin_lock = threading.Lock()
        stdin_state = {"closed": False}
        # 裁定 RW1: `go` フレームの送出 (on_ready 完了後) と
        # `tool_rpc_result` の送出 (dispatcher_loop) は同じ親→子 seq 系列
        # (handshake=1 起点) に相乗りするため、カウンタを共有する。
        out_seq_holder = {"n": 0}
        in_seq = SeqTracker()  # 子→親方向の受信検証
        ready_queue: "queue.Queue[dict]" = queue.Queue(maxsize=1)
        done_queue: "queue.Queue[tuple[str, object]]" = queue.Queue(maxsize=1)
        dispatch_queue: "queue.Queue[dict]" = queue.Queue()
        transcript: list[dict] = []
        state = {"bytes": 0, "truncated": False}
        cli_pgid_holder: dict[str, int] = {}

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
                    elif ftype == "cli_started":
                        pgid = frame.get("pgid")
                        if isinstance(pgid, int):
                            cli_pgid_holder["pgid"] = pgid
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
                        result_queue.put((True, self._dispatch_rpc(name, args)))
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
            "credentials": credentials or {},
            **(run_context_fields or {}),
        }
        with stdin_lock:
            write_frame(proc.stdin, handshake)

        status = "failed"
        output = None
        try:
            try:
                ready = self._wait_with_stop(
                    ready_queue, timeout=w.worker_startup_timeout_sec)
            except queue.Empty:
                self._escalate_kill(proc, w)
                return MissionResult("failed", None, transcript)

            # I1 是正 (プラン10 束D round1、verified-codex-round1.md、
            # 2026-08-28): `ok` の検査を `on_ready`/`go` より**前**へ移す。
            # `ok=false` は子が Mission を一度も開始していないことの明示
            # (mission_worker.py の bootstrap 失敗 — Landlock bind /
            # registry 構築 / handshake 検証の失敗) — これは pre-ready
            # 失敗そのものなので、`_on_ready` を呼んで
            # `ImproveSupervisor` に running commit させてはならない
            # (呼ぶと pre-ready 失敗が post-ready 失敗として誤分類され、
            # 設計 §3.1⑥/§8.1-19 の spawn_attempts retry 分岐に到達しなく
            # なる)。子を明示的に `_escalate_kill` する (旧来は `finally`
            # の `_ensure_dead` に依存していたが、意図を明示する)。
            if not ready.get("ok", False):
                self._escalate_kill(proc, w)
                return MissionResult(
                    "failed", None, transcript,
                    reason=f"child ready ok=false: {ready.get('error')}")

            if self._on_ready is not None:
                try:
                    self._on_ready(ready)
                except Exception as e:  # noqa: BLE001 — 裁定 R-D1: pre-ready
                    # 失敗として扱う。呼び出し元 (ImproveSupervisor) が
                    # commit 前 (mark_running 前) の失敗と区別できるよう、
                    # 子を SIGTERM→SIGKILL で kill して即 failed を返す
                    # (子は ready 送出直後に実行を継続する実装のままなので、
                    # ここで kill しない限り commit していない slot に紐付く
                    # 実行が走り続けてしまう — 裁定 R-D1 は「on_ready が
                    # 例外を投げたら子を kill して failed を返す」ことだけを
                    # 要求し、新規プロトコルフレームの追加は明示的に不要と
                    # している。設計書 プラン 9.3 節「実装時改訂 R-D1」参照)。
                    _log.exception("on_ready callback failed")
                    self._escalate_kill(proc, w)
                    return MissionResult(
                        "failed", None, transcript,
                        reason=f"on_ready failed: {type(e).__name__}: {e}")

            # 裁定 RW1/RW6: `on_ready` が例外なく戻った直後、
            # `worker_profile == "improve"` のときだけ `go` 実フレームを
            # 子へ書く (`on_ready is not None` ではなく profile 判定のみに
            # 依存させる — `submit_manual` (on_ready=None) の子も `go` を
            # 待つため、この非対称を避ける。trade profile は `go` を
            # 待たない旧来のプロトコルを維持するため一切送らない)。
            if self._worker_profile == "improve":
                out_seq_holder["n"] += 1
                try:
                    with stdin_lock:
                        if not stdin_state["closed"]:
                            write_frame(proc.stdin, {
                                "type": "go",
                                "seq": out_seq_holder["n"] + 1,
                            })
                except (BrokenPipeError, OSError):
                    pass  # 子が既に死んでいる — 以降の done_queue 待ちが検知する

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
                # [fail-observability]: worker 内例外経路
                # (mission_worker.py の `except Exception` ハンドラ) は
                # `reason` ではなく `error` キーで送るため、`reason` キー
                # が無ければ (None) `error` を拾う (死因を落とさない)。
                # `reason` キーが明示的に空文字で来た場合はそのまま
                # 空文字を通す (`or` ではなく `is None` で判定 — 既存の
                # test_worker_runner_preserves_empty_string_reason の
                # 契約「キーが無い (None) とキーはあるが空 (\"\") は別の
                # 事象」を壊さない)。
                reason = payload.get("reason")
                if reason is None:
                    reason = payload.get("error")
                # M4: 旧フレーム (`recovered` キー無し) は既定 False —
                # `MissionResult.recovered` の既定と一致させる後方互換。
                recovered = payload.get("recovered", False)
            else:  # eof / protocol_error / error — すべて failed に正規化
                status = "failed"
                output = None
                # [fail-observability]: kind (eof/protocol_error/error) を
                # そのまま残し、result フレームを受け取れなかった終端でも
                # 死因の手がかりを一切残さないことを避ける。
                reason = f"worker {kind}"
                recovered = False
            return MissionResult(status, output, transcript, reason=reason,
                                 recovered=recovered)
        finally:
            dispatch_queue.put(None)
            self._ensure_dead(proc, w)
            if "pgid" in cli_pgid_holder:
                self._terminate_cli_pgid(cli_pgid_holder["pgid"])
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

    def _dispatch_rpc(self, name: str, args: dict):
        """裁定 R-D2: `worker_profile="improve"` 用に構築時へ渡された
        `rpc_handlers` (run_backtest/analyze_corr) をここで初めて消費する。
        `__init__` は受け取って `self._rpc_handlers` へ格納するだけで、
        以前の `dispatcher_loop` は `tool_rpc` フレームを常に
        `_call_rag` (trade profile の RAG 呼び出し専用) へルーティング
        しており、improve 側の RPC は握り潰されて `self._rag` への
        `AttributeError` に化けていた (実プロセス回帰ピン:
        `tests/runners/test_worker_runner.py::
        test_worker_runner_dispatches_improve_tool_rpc_via_rpc_handlers_not_rag`)。

        `rpc_handlers` が構築時に渡されていれば (None ではなく dict なら、
        空 dict も含む — `verify_backend.py` は全 RPC を明示的に拒否する
        ための `_reject_rpc` ハンドラだけの dict を渡す) 名前で dispatch
        し、未登録名は `KeyError` で fail closed する。`rpc_handlers` が
        渡されていない (None、trade profile の既定) 場合のみ、既存の
        RAG 呼び出し経路 (`self._rag` への `getattr`) へフォールバック
        する — trade profile の挙動は変えない。"""
        if self._rpc_handlers is not None:
            handler = self._rpc_handlers.get(name)
            if handler is None:
                raise KeyError(f"no rpc handler registered for {name!r}")
            return handler(args)
        return self._call_rag(name, args)

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

    def _terminate_cli_pgid(self, pgid: int) -> None:
        """§7.1-2 の blocking 受入条件: worker が EOF/異常終了した後、
        `cli_started` で得た CLI の pgid を `CliRunner._terminate_pgid` と
        同じ規律 (SIGTERM→grace→SIGKILL) で回収する。mission_worker 自身の
        pgid (`proc.pid`) とは別グループのため `_ensure_dead` では届かない。"""
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            return
        grace = getattr(self._settings.runner, "cli_terminate_grace_sec", 10.0)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
