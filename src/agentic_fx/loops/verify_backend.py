"""検証専用入口 `afx improve verify-backend` (§8.1-45)。

scheduler・wave・backlog・improve_waves/improve_wave_slots・improvement_runs
のいずれにも触れない one-shot 接続性プローブ。`improve.llama_swap_verified=
false` のまま実行できる唯一の経路 — 通常入口 (`ImproveSupervisor`) は
§7.1-2 の pin により `provider=llama_swap` を拒否したままにする (本モジュ
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
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

from agentic_fx.runners.base import Mission
from agentic_fx.runners.worker_runner import WorkerRunner
from agentic_fx.store.rag import Rag

if TYPE_CHECKING:
    from agentic_fx.config import Settings
    from agentic_fx.core.contracts import Clock

_log = logging.getLogger("agentic_fx.loops.verify_backend")


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
    backend: Literal["local", "claude", "codex"]
    provider: Literal["chatgpt", "llama_swap"] | None
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


def verify_backend(
    root: Path, settings: "Settings", *,
    backend: Literal["local", "claude", "codex"],
    provider: Literal["chatgpt", "llama_swap"] | None,
    clock: "Clock",
) -> VerifyBackendResult:
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

    if (backend == "codex" and effective_provider == "llama_swap"
            and not settings.improve.llama_swap_verified):
        # 通常入口 (ImproveSupervisor) はこの組み合わせを拒否したまま
        # (§7.1-2 の pin は変更しない) — verify-backend だけが唯一の
        # バイパス経路であることを WARNING で残す。
        _log.warning(
            "verify-backend: bypassing improve.llama_swap_verified=false "
            "gate for provider=llama_swap (this is the only entry point "
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

        ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={
            "run_backtest": scoped_settings.improve.backtest_rpc_timeout_sec,
            "analyze_corr": scoped_settings.improve.backtest_rpc_timeout_sec})
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

        rag = Rag(scratch_root / "rag")
        runner = WorkerRunner(
            root=root, settings=scoped_settings, clock=clock, rag=rag,
            worker_profile="improve", run_context=ctx, on_ready=on_ready,
            rpc_handlers=ctx.rpc_handlers)

        mission = Mission(
            prompt=(
                "You are a one-shot backend connectivity probe. Reply "
                f'with exactly the JSON object {{"echo": "{nonce}"}} and '
                "nothing else — do not create files, do not call any "
                "tools."),
            tools=[],
            output_schema={
                "type": "object", "required": ["echo"],
                # "type" は必須 — ChatGPT backend は type 無しの property を
                # 400 (Invalid schema for response_format) で拒否する (実機実測)
                "properties": {"echo": {"type": "string", "const": nonce}},
                "additionalProperties": False},
            max_turns=1,
            timeout_sec=scoped_settings.improve.mission_timeout_sec)

        with _DescendantWatcher(os.getpid()) as watcher:
            result = runner.run(mission)
        after_descendants = _descendant_pids(os.getpid())

        if not ready_frames:
            return VerifyBackendResult(
                ok=False, backend=backend, provider=effective_provider, fingerprint=None,
                detail="mission never reached ready (handshake failed)")
        if result.status != "completed" or not result.output:
            return VerifyBackendResult(
                ok=False, backend=backend, provider=effective_provider, fingerprint=None,
                detail=f"mission did not complete: status={result.status} "
                      f"reason={result.reason}")
        if result.output.get("echo") != nonce:
            return VerifyBackendResult(
                ok=False, backend=backend, provider=effective_provider, fingerprint=None,
                detail="mission output failed the nonce round-trip "
                      "(schema/nonce gate)")
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
                  f"model={model} fingerprint={fingerprint}")
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)
