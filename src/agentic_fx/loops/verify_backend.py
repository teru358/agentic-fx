"""検証専用入口 `afx improve verify-backend` (§8.1-45)。

scheduler・wave・backlog・improve_waves/improve_wave_slots・improvement_runs
のいずれにも触れない one-shot 接続性プローブ。`improve.llama_swap_verified=
false` のまま実行できる唯一の経路 — 通常入口 (`ImproveSupervisor`) は
§7.1-2 の pin により通常入口の opencode backend を拒否したままにする (本モジュ
ールはその pin を変更しない。バイパスするのはこの検証専用入口だけであり、
その旨を WARNING ログへ残す)。

<!-- precheck 2026-08-23 wave3: T13-B1 T13-B2 T13-B3 -->
本番経路 (`WorkerRunner(worker_profile="improve", run_context=..., on_ready=...,
rpc_handlers=...)`) をそのまま流用する — 独自に `build_runner`/
`build_mission_registry` を呼ばない (呼ぶと workdir の 0700 作成・認証
コピー・MCP dispatcher bind という `WorkerRunner`/`mission_worker.py` の
親側責務を全部飛ばしてしまう — 着手前検証 report-task13.md B-3)。Mission
は候補を実装させるのではなく、固定 nonce の往復を要求するだけの 1 ターン
接続性プローブに単純化する (report-task13.md B-1 の裁定)。親ゲートは
(a) `ready` フレームの run_context echo 一致 (b) nonce 往復 schema 一致
(c) `backend != "local"` のとき実 CLI 子プロセスの起動観測 (d) 同じく
run() 完了後の子孫プロセス 0 件 (pgid 回収) — の 4 点。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, TypeAlias

from agentic_fx.runners.base import Mission
from agentic_fx.runners.worker_runner import WorkerRunner
from agentic_fx.store.rag import Rag
from agentic_fx.tools.improve_rpc_tools import build_ledger_wrapped_rpc_handlers

if TYPE_CHECKING:
    from agentic_fx.config import Settings
    from agentic_fx.core.contracts import Clock

_log = logging.getLogger("agentic_fx.loops.verify_backend")


WorkerIsolationOptions: TypeAlias = dict[str, object]


class _OperatorShellNoIsolation:
    """運用者 shell の非隔離実行を表す、比較専用の値。"""

    __slots__ = ()

    def __repr__(self) -> str:
        return "OPERATOR_SHELL_NO_ISOLATION"


#: service 外の運用者 shell からの接続性確認だけが明示的に選ぶ、非隔離の runner 引数。
OPERATOR_SHELL_NO_ISOLATION = _OperatorShellNoIsolation()


class VerifyBackendGateError(Exception):
    """親ゲート (a) (`on_ready` の run_context echo 照合) の mismatch 専用
    例外 (束F検収 L-F1/L-F14 是正、裁定 C 案)。`RuntimeError` は
    `_check_cli_backend`/`_validate_startup` 等、本リポジトリで「配線
    ミスを起動時に殺す」用途にも広く使われているため、CLI の統一エラー
    境界 (`backtest/cli.py::dispatch`) で一律 catch すると診断が落ちる
    懸念があった。この専用型だけを `dispatch` の except に足すことで、
    握り潰す範囲を広げずに他ゲート (b)(c)(d) と同じ `エラー: ...` + rc=1
    の UX に揃える (fail-closed 性は維持 — mismatch のまま mission を
    続行させない)。"""


@dataclass(frozen=True)
class VerifyBackendResult:
    ok: bool
    backend: Literal["local", "claude", "codex", "opencode"]
    provider: Literal["chatgpt"] | None
    fingerprint: str | None
    detail: str


def _reject_rpc(method: str) -> Callable[[dict], dict]:
    def _handler(args: dict) -> dict:
        raise RuntimeError(
            f"verify-backend: RPC method {method!r} は許可されません "
            "(この入口は DB を経由する run_backtest/analyze_corr を "
            "一切実行しない)")
    return _handler


def _descendant_pids(pid: int) -> set[int]:
    """`/proc` を 1 回スキャンして `pid` の子孫 PID 集合を返す。
    `WorkerRunner`/`mission_worker.py` を改変せずに外側から「実 CLI 子
    プロセスが起動し、回収されたか」を観測するための black-box プローブ
    (report-task13.md B-1 の親ゲート (c)(d))。"""
    children: dict[int, set[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text()
        except OSError:
            continue
        after = raw.rsplit(")", 1)[1].split()
        ppid = int(after[1])
        children.setdefault(ppid, set()).add(int(entry.name))
    result: set[int] = set()
    frontier = [pid]
    while frontier:
        p = frontier.pop()
        for c in children.get(p, ()):
            if c not in result:
                result.add(c)
                frontier.append(c)
    return result


class _DescendantWatcher:
    """`runner.run(mission)` がブロックしている間、`root_pid` の子孫 PID の
    和集合を 0.05 秒間隔でポーリングして記録する daemon thread。"""

    def __init__(self, root_pid: int) -> None:
        self._root_pid = root_pid
        self._seen: set[int] = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._seen |= _descendant_pids(self._root_pid)
            time.sleep(0.05)

    def __enter__(self) -> "_DescendantWatcher":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def saw_any_descendant(self) -> bool:
        return bool(self._seen)


#: 進行の目安として数える event の type (opencode の step_finish、claude の assistant、
#: codex の turn.completed)
_STEP_EVENT_TYPES = frozenset({"step_finish", "step-finish", "assistant", "turn.completed"})


#: echo に届いてから MCP の観測 (initialize と tools/list) が揃うまで待つ上限 (秒)
MCP_GRACE_SEC = 60.0

_FENCE = re.compile(r"\A```(?:json)?[ \t]*\n([^`]*?)\n?```\Z")


def _is_echo_text(text: object, nonce: str) -> bool:
    """文字列の全体が `{"echo": <nonce>, "count": <整数>}` だけ (`count` は list_staging の
    結果から数えた値で必須。完全な 1 つの code fence に収まる形は許す)。
    prompt の文章に埋まった nonce や、他のキーを足した JSON は一致と見ない。"""
    if not isinstance(text, str):
        return False
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced is not None:
        body = fenced.group(1).strip()
    if not body.startswith("{"):
        return False
    try:
        parsed = json.loads(body)
    except ValueError:
        return False
    if not isinstance(parsed, dict) or parsed.get("echo") != nonce:
        return False
    if set(parsed) != {"echo", "count"}:
        return False
    return type(parsed["count"]) is int


def _assistant_texts(backend: str, event: dict) -> list[object]:
    """backend の stdout の event のうち、assistant の本文 (出力 text) だけを返す。
    tool の結果・thinking・回復の記録 (stderr 末尾を含む) は含めない。各 runner が出力と
    見なす経路 (opencode の text part、claude の assistant の text block と最終 result、
    codex の agent_message) に対応する。"""
    kind = event.get("type")
    if backend == "opencode":
        part = event.get("part")
        if kind == "text" and isinstance(part, dict):
            return [part.get("text")]
    elif backend == "claude":
        if kind == "assistant":
            message = event.get("message")
            blocks = message.get("content") if isinstance(message, dict) else None
            if isinstance(blocks, list):
                return [b.get("text") for b in blocks
                        if isinstance(b, dict) and b.get("type") == "text"]
        elif kind == "result":
            structured = event.get("structured_output")
            return [event.get("result"),
                    json.dumps(structured) if isinstance(structured, dict) else None]
    elif backend == "codex":
        item = event.get("item")
        if kind == "item.completed" and isinstance(item, dict) \
                and item.get("type") == "agent_message":
            return [item.get("text")]
    return []


class _EchoWatch:
    """子が流す event を見て、(1) assistant の本文が echo と一致した、(2) worker の MCP が
    `tools/list` に tool を 1 つ以上返し、list_staging を 1 回呼んだ時点で `stop` を立てる。
    どちらかが欠けたまま終わった場合に読めるよう、event 数・step 数・MCP の観測も持つ。"""

    def __init__(self, nonce: str, stop: threading.Event, backend: str, *,
                 mcp_grace_sec: float = MCP_GRACE_SEC,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._mcp_grace_sec = mcp_grace_sec
        self._monotonic = monotonic
        self._echo_at: float | None = None
        self.grace_expired = False
        # grace thread と event reader の両方が状態を更新するので、observe / check_grace /
        # summary は同じ lock の下で原子的に動く
        self._lock = threading.Lock()
        self._nonce = nonce
        self._stop = stop
        self._backend = backend
        self.seen = False
        self.events = 0
        self.steps = 0
        self.initialized = False
        self.listed: list[str] | None = None
        self.calls = 0

    # tools/list は server 側の名前 (web_search 等) を返す。`afx_` 付きは CLI 側の呼び名
    @property
    def mcp_ok(self) -> bool:
        return (self.initialized and self.listed is not None and len(self.listed) > 0
                and self.calls >= 1)

    @property
    def ok(self) -> bool:
        return self.seen and self.mcp_ok and not self.grace_expired

    def observe(self, frame: dict) -> None:
        with self._lock:
            # 期限優先: 期限に達していたら、同時に届いた frame より先に失敗を確定する
            self._expire_if_due()
            self._observe_locked(frame)

    def _expire_if_due(self) -> None:
        if (self._echo_at is not None and not self.ok and not self.grace_expired
                and self._monotonic() - self._echo_at >= self._mcp_grace_sec):
            self.grace_expired = True
            self._stop.set()

    def _observe_locked(self, frame: dict) -> None:
        self.events += 1
        msg = frame.get("message") or {}
        cli_output = msg.get("type") == "event" and isinstance(msg.get("message"), dict)
        if cli_output:
            # CliRunner が流す {"type": "event", "message": {...}} は worker がもう一度包む
            msg = msg["message"]
        content = msg.get("content")
        try:
            event = json.loads(content) if isinstance(content, str) else None
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        if not cli_output:
            # 包みが 1 重の event は worker 自身が送ったもの (CLI の出力は 2 重)
            observed = event.get("_mcp_observed")
            if isinstance(observed, dict):
                if observed.get("kind") == "initialize":
                    self.initialized = True
                elif observed.get("kind") == "tools/list" \
                        and isinstance(observed.get("tools"), list):
                    self.listed = [str(n) for n in observed["tools"]]
                elif observed.get("kind") == "tools/call" \
                        and "list_staging" in observed.get("tools", []):
                    self.calls += 1
        if event.get("type") in _STEP_EVENT_TYPES:
            self.steps += 1
        if not self.seen and any(_is_echo_text(t, self._nonce)
                                 for t in _assistant_texts(self._backend, event)):
            self.seen = True
            self._echo_at = self._monotonic()
        if self.ok:
            self._stop.set()

    def check_grace(self) -> bool:
        """echo に届いたのに MCP の観測が揃わないまま猶予 (`mcp_grace_sec`) を過ぎたら、
        失敗として `stop` を立てる。echo 未到達の間は何もしない (mission の上限に従う)。"""
        with self._lock:
            self._expire_if_due()
            return self.grace_expired

    def summary(self) -> str:
        with self._lock:
            return self._summary_locked()

    def _summary_locked(self) -> str:
        if self.listed is not None:
            suffix = (" (empty)" if not self.listed else
                      " (uninitialized)" if not self.initialized else "")
            mcp = f"tools={len(self.listed)}{suffix}"
        else:
            mcp = "init_only" if self.initialized else "none"
        grace = " grace_expired" if self.grace_expired else ""
        return (f"echo_reached={'yes' if self.seen else 'no'} mcp={mcp} call={self.calls} "
                f"steps={self.steps} events={self.events}{grace}")


def verify_backend(
    root: Path, settings: "Settings", *,
    backend: Literal["local", "claude", "codex", "opencode"],
    provider: Literal["chatgpt"] | None,
    clock: "Clock",
    isolation_options: WorkerIsolationOptions | _OperatorShellNoIsolation | None = None,
    stop_on_echo: bool = False,
) -> VerifyBackendResult:
    """`isolation_options` は CLI 隔離の WorkerRunner の引数。
    省略時は改善 mission と同じ admission を通り、通らなければ
    `sandbox_unavailable` を返す。運用者の CLI は
    `OPERATOR_SHELL_NO_ISOLATION` で非隔離を明示する。

    `stop_on_echo` (隔離の互換性自己試験が立てる): assistant の本文の echo 一致と、MCP の
    `tools/list` が tool を 1 つ以上返した観測の両方が揃った時点を成功とし、mission は通常の停止経路 (stop_event) で終える。その後に CLI が tool を呼び続けても
    成功は覆らない。失敗したときは echo 未到達と step 数を detail に残す。"""
    now = clock.now()
    nonce = secrets.token_hex(16)

    # backend/provider の実行時上書き (report-task13.md B-4)。個人設定
    # ファイル (settings.yaml) は一切書き換えない — model_copy は pydantic
    # の非破壊コピー。
    runner_update: dict = {
        "improve": settings.runner.improve.model_copy(update={"backend": backend})}
    if backend == "codex" and provider is not None:
        runner_update["codex"] = settings.runner.codex.model_copy(
            update={"provider": provider})
    scoped_settings = settings.model_copy(
        update={"runner": settings.runner.model_copy(update=runner_update)})

    # I1 是正 (codex 1周目 verified-codex-round1.md): `--provider` 省略時は
    # `settings.runner.codex.provider` がそのまま `scoped_settings` に残り、
    # 実際に走る provider になる (runners/factory.py:49,
    # worker_runner.py:158 も scoped_settings 経由)。以降はこの 1 値
    # (`effective_provider`) だけを使い、引数 `provider` を直接見ない —
    # 結果 (`VerifyBackendResult.provider`)・バイパス WARNING 判定・
    # fingerprint 原像・detail の 4 箇所すべてが実効値と一致する。
    # `backend == "codex"` で条件付けないと、`backend=local`/`claude` の
    # 結果に settings 由来の provider が漏れる (回帰点、負のテストで pin)。
    effective_provider = (
        scoped_settings.runner.codex.provider if backend == "codex" else None)

    if (backend == "opencode"
            and not settings.improve.llama_swap_verified):
        # 通常入口 (ImproveSupervisor) はこの組み合わせを拒否したまま
        # (§7.1-2 の pin は変更しない) — verify-backend だけが唯一の
        # バイパス経路であることを WARNING で残す。
        _log.warning(
            "verify-backend: bypassing improve.llama_swap_verified=false "
            "gate for backend=opencode (this is the only entry point "
            "allowed to do so)")

    mission_id = -1
    scratch_root = Path(tempfile.mkdtemp(prefix="afx-verify-backend-"))
    staging_dir = scratch_root / "staging" / str(mission_id)
    source_snapshot_dir = scratch_root / "source_snapshot"
    try:
        staging_dir.mkdir(parents=True, mode=0o700)
        source_snapshot_dir.mkdir(mode=0o500)

        from agentic_fx.loops.improve_run_context import ImproveRunContext
        from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger

        rpc_timeout_sec_by_kind = {
            "run_backtest": scoped_settings.improve.backtest_rpc_timeout_sec,
            "analyze_corr": scoped_settings.improve.backtest_rpc_timeout_sec}
        ledger = ImproveRpcLedger(
            rpc_timeout_sec_by_kind=rpc_timeout_sec_by_kind)
        ctx = ImproveRunContext(
            mission_id=mission_id, run_id=-1,
            staging_dir=staging_dir, source_snapshot_dir=source_snapshot_dir,
            allowed_backlog_ids=None, slot_key=None, ledger=ledger,
            rpc_handlers={"run_backtest": _reject_rpc("run_backtest"),
                         "analyze_corr": _reject_rpc("analyze_corr")})

        ready_frames: list[dict] = []

        def on_ready(frame: dict) -> None:
            ready_frames.append(frame)
            echoed = frame.get("run_context") or {}
            # <!-- precheck 2026-08-23 R-D3 -->
            # 裁定 R-D3: source_snapshot_dir は WorkerRunner が
            # workdir/"source" (この関数からは見えない使い捨て workdir 配下)
            # へ実体化した後の値を返す — 渡した出所 (source_snapshot_dir
            # 変数) そのものではない。mission_id/staging_dir は完全一致を、
            # source_snapshot_dir は basename が "source" であることのみを
            # 照合する (実体化そのものの裏取りは Task 1 Step 33 のテストが
            # 担う)。
            echoed_source = echoed.get("source_snapshot_dir")
            mismatch = (echoed.get("mission_id") != str(mission_id)
                       or echoed.get("staging_dir") != str(staging_dir)
                       or echoed_source is None
                       or Path(echoed_source).name != "source")
            if mismatch:
                raise VerifyBackendGateError(
                    f"verify-backend: ready frame run_context mismatch "
                    f"(expected mission_id={mission_id!r} "
                    f"staging_dir={str(staging_dir)!r} and a "
                    f"source_snapshot_dir whose basename is 'source', "
                    f"got {echoed})")

        if isolation_options is None:
            from agentic_fx.core import improve_admission

            choice = improve_admission.choose_for_mission(scoped_settings, root)
            if choice.refusal is not None:
                return VerifyBackendResult(
                    ok=False, backend=backend, provider=effective_provider,
                    fingerprint=None,
                    detail=f"{improve_admission.SANDBOX_UNAVAILABLE}: {choice.refusal}")
            isolation_options = choice.worker_options
        elif isolation_options is OPERATOR_SHELL_NO_ISOLATION:
            isolation_options = {}
        elif isinstance(isolation_options, dict):
            required = {"cleanup_protocol", "isolation"}
            if not required <= isolation_options.keys():
                raise ValueError(
                    "isolation_options must contain cleanup_protocol and isolation")
        else:
            raise ValueError("isolation_options must be admission options or "
                             "OPERATOR_SHELL_NO_ISOLATION")
        rag = Rag(scratch_root / "rag")
        stop = threading.Event()
        watch = _EchoWatch(nonce, stop, backend) if stop_on_echo else None
        extra = {"stop_event": stop, "on_event": watch.observe} if watch is not None else {}
        runner = WorkerRunner(
            root=root, settings=scoped_settings, clock=clock, rag=rag,
            worker_profile="improve", run_context=ctx, on_ready=on_ready,
            rpc_handlers=build_ledger_wrapped_rpc_handlers(
                ledger=ctx.ledger, rpc_handlers=ctx.rpc_handlers,
                staging_dir=ctx.staging_dir),
            rpc_timeout_sec_by_kind=rpc_timeout_sec_by_kind, **isolation_options, **extra)

        mission = Mission(
            prompt=(
                "You are a one-shot backend connectivity probe. Step 1: call the MCP tool "
                "`list_staging` exactly once and count the entries it returns. Step 2: reply "
                f'with exactly the JSON object {{"echo": "{nonce}", "count": <that number>}} '
                "and nothing else. Do not call any other tool."),
            tools=["list_staging"],
            output_schema={
                "type": "object", "required": ["echo", "count"],
                # "type" は必須 — ChatGPT backend は type 無しの property を
                # 400 (Invalid schema for response_format) で拒否する (実機実測)
                "properties": {"echo": {"type": "string", "const": nonce},
                               "count": {"type": "integer"}},
                "additionalProperties": False},
            max_turns=4,
            timeout_sec=scoped_settings.improve.mission_timeout_sec)

        grace_done = threading.Event()
        grace_thread = None
        if watch is not None:
            def _grace_loop() -> None:
                while not grace_done.wait(0.2):
                    if watch.check_grace():
                        return
            grace_thread = threading.Thread(target=_grace_loop, daemon=True)
            grace_thread.start()
        try:
            with _DescendantWatcher(os.getpid()) as watcher:
                result = runner.run(mission)
        finally:
            grace_done.set()
            if grace_thread is not None:
                # _grace_loop は grace_done.wait(0.2) で必ず抜けるので、join は有限時間で終わる
                grace_thread.join()
        after_descendants = _descendant_pids(os.getpid())

        if not ready_frames:
            return VerifyBackendResult(
                ok=False, backend=backend, provider=effective_provider, fingerprint=None,
                detail=("mission never reached ready (handshake failed): "
                        f"status={result.status} reason={result.reason}"))
        progress = f" [{watch.summary()}]" if watch is not None else ""
        echoed = watch is not None and watch.ok
        if watch is not None and not watch.ok:
            # 自己試験は echo と MCP の両方が揃って初めて成功 (CLI の最終出力だけでは足りない)
            return VerifyBackendResult(
                ok=False, backend=backend, provider=effective_provider, fingerprint=None,
                detail=f"mission did not complete: status={result.status} "
                      f"reason={result.reason}{progress}")
        if not echoed and (result.status != "completed" or not result.output):
            return VerifyBackendResult(
                ok=False, backend=backend, provider=effective_provider, fingerprint=None,
                detail=f"mission did not complete: status={result.status} "
                      f"reason={result.reason}{progress}")
        if not echoed and result.output.get("echo") != nonce:
            return VerifyBackendResult(
                ok=False, backend=backend, provider=effective_provider, fingerprint=None,
                detail="mission output failed the nonce round-trip "
                      f"(schema/nonce gate){progress}")
        if backend != "local" and not watcher.saw_any_descendant():
            return VerifyBackendResult(
                ok=False, backend=backend, provider=effective_provider, fingerprint=None,
                detail="no CLI child process was observed during the run "
                      "(cli_started gate)")
        if backend != "local" and after_descendants:
            return VerifyBackendResult(
                ok=False, backend=backend, provider=effective_provider, fingerprint=None,
                detail="CLI child process(es) were not reaped after the "
                      f"mission completed (pgid recovery gate): "
                      f"{sorted(after_descendants)}")

        model = scoped_settings.runner.improve.model
        fingerprint = hashlib.sha256(
            f"{backend}:{effective_provider}:{model}:{nonce}".encode("utf-8")
        ).hexdigest()
        return VerifyBackendResult(
            ok=True, backend=backend, provider=effective_provider,
            fingerprint=fingerprint,
            detail=f"backend={backend} provider={effective_provider} "
                  f"model={model} fingerprint={fingerprint}{progress}")
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)
