"""ImproveLoop — 改善 Mission の三相 (prepare/run/commit)
(設計書 §4、プラン §8.1-6/11/16/23/24/40/42/43)。"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import logging
import os
import re
import sqlite3
import stat
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import json
import jsonschema

# F-6 是正 (検収 task12): renameat2(2) の AT_FDCWD / RENAME_NOREPLACE。
# x86_64 Linux の値 (Linux 3.15+ の ABI、glibc 2.28+ が libc wrapper を持つ)。
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1

from agentic_fx._safe_error import safe_error_text
from agentic_fx.activity import Category
from agentic_fx.backtest import holdout  # precheck 2026-08-22 wave2: 型6#5 —
    # module-level で import する (関数内 import だと monkeypatch.setattr
    # ("agentic_fx.loops.improve_loop.holdout.run_in_sample", ...) が
    # 属性未定義で ImportError になり、足しても sys.modules から取り直す
    # ため効かない)
from agentic_fx.loops.improve_context import build_improve_context
from agentic_fx.loops.improve_run_context import ImproveRunContext
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.runners.base import is_tool_budget_abort
from agentic_fx.loops.summary import IMPROVE_OUTPUT_SCHEMA  # precheck 2026-08-22 wave2: T10-B12
from agentic_fx.plugin.gate_pytest import (
    CandidateSnapshotError, check_candidate_snapshot, hashes_of,
    run_gate_pytest,
)
from agentic_fx.plugin import loader as plugin_loader
from agentic_fx.plugin.noop_gate import count_self_test_functions, find_noop_copy
from agentic_fx.plugin.resolve import InventoryBuildResult, is_relock_transition
from agentic_fx.plugin.sandbox import SandboxError, check_source
from agentic_fx.plugin.strategy_gate import (
    _check_profitability_floor,
    _eval_timeframe as _strategy_gate_eval_timeframe,
    _floor_fail_items,
    evaluate_strategy_adoption_gate,
    floor_rule_text as _strategy_gate_floor_rule_text,
    floor_settings_kv,
)
from agentic_fx.runners.base import Mission
from agentic_fx.tools.plugin_loader import approved_plugins
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store import improve_runs as improve_runs_store
from agentic_fx.store import improve_waves
from agentic_fx.store import missions as missions_store
from agentic_fx.tools.improve_rpc_tools import RpcOutcome, build_rpc_handlers

if TYPE_CHECKING:
    from agentic_fx.activity import ActivityLog
    from agentic_fx.config import Settings
    from agentic_fx.core.contracts import Clock
    from agentic_fx.runners.worker_runner import WorkerRunner
    from agentic_fx.store.rag import Rag  # precheck 2026-08-22 wave2: T10-B10

_log = logging.getLogger("agentic_fx.improve_loop")

_PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def accepted_entries(entries):
    """Return ledger entries whose RPC result was accepted successfully."""
    return [e for e in entries if "error" not in e["result_summary"]]


def _is_numeric(value) -> bool:
    """T4 §1 L3: `bool` は `int` の subclass なので明示的に除く。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def best_candidate(rows: list[dict]) -> dict | None:
    """`candidate_archives.list_by_mission` の行から best を 1 件選ぶ
    (ブリーフ「変更点」1)。順序 = `metrics.evaluable` (True 先) →
    `trades` desc → `pf` desc (None/非数値は最下) → `max_drawdown` asc
    (None は最下)。同点は id 昇順で先頭 (決定的)。行 0 件は `None`。"""
    if not rows:
        return None

    def sort_key(row: dict):
        metrics = row.get("metrics") or {}
        evaluable_rank = 0 if metrics.get("evaluable") else 1
        trades = metrics.get("trades")
        trades_rank = -trades if _is_numeric(trades) else float("inf")
        pf = metrics.get("pf")
        pf_rank = -pf if _is_numeric(pf) else float("inf")
        max_drawdown = metrics.get("max_drawdown")
        dd_rank = max_drawdown if _is_numeric(max_drawdown) else float("inf")
        return (evaluable_rank, trades_rank, pf_rank, dd_rank, row["id"])

    return min(rows, key=sort_key)


def _format_metric(value) -> str:
    return f"{value:.3f}" if _is_numeric(value) else "-"


def _best_label(best: dict | None) -> str | None:
    """`best_candidate()` の 1 行から `<name>@<hash8> pf=.. dd=..` を作る。
    `best` が `None` なら `None` (呼び出し側は「best 無し」を意味する)。"""
    if best is None:
        return None
    metrics = best.get("metrics") or {}
    hash8 = str(best.get("artifact_hash", ""))[:8]
    return (f"{best.get('name')}@{hash8} pf={_format_metric(metrics.get('pf'))} "
            f"dd={_format_metric(metrics.get('max_drawdown'))}")


class _BacktestReply(dict):
    """公開応答とは別に、同一プロセスのTx-2用元データを保持する。"""

    def __init__(self, reply: dict, save_kwargs: dict) -> None:
        super().__init__(reply)
        self.save_kwargs = save_kwargs


def _current_rpc_abandoned() -> bool:
    """codex 2 周目 Important (2026-09-11): `run_backtest_handler` が
    `ledger.state() != "OPEN"` を読んだ**直後**に dispatcher の RPC
    timeout が予約を release し、freeze が完了しても、この state 読み取り
    と snapshot mkdir は原子的でないため handler は続行してしまう
    (check-then-act 競合)。`worker_runner.dispatcher_loop` は timeout
    分岐で自分が起動した worker thread に `RPC_ABANDONED_ATTR` を立てる
    — この handler は snapshot を書く前後の両方でこれを読み、abandoned
    ならその場で打ち切る/後始末する。`worker_runner` の import は関数内に
    留める (モジュール先頭import は `store.rag` 経由の chromadb を
    improve_loop の import 時に強制する — 既存の遅延方針 (`_build_worker_
    runner` 参照) を踏襲する)。"""
    from agentic_fx.runners.worker_runner import RPC_ABANDONED_ATTR
    return getattr(threading.current_thread(), RPC_ABANDONED_ATTR, False)


def _backtest_reply_from_save_kwargs(
        save_kwargs: dict, *, submission_blocked: dict | None = None,
        started: bool = True) -> dict:
    """永続化用 save kwargs を JSON-safe な RPC 応答へ限定投影する。
    期間端点 (`period`) と保存時刻 (`now`) は agent に見せない (遮断 7、
    `improve_rpc_tools._FORBIDDEN_KEYS`) — 台帳用の元データは属性で運ぶ。

    [profitability-floor] T2 Step 2-1 (2026-09-13): `submission_blocked`
    は**agent へ返る公開面にだけ**足す — `save_kwargs` (= 台帳/Tx-2 が
    読む非公開の元データ) には一切混ぜない (`improve_rpc_tools.py:150-154`
    の `save_kwargs` 契約を痩せさせない)。in_sample 段が合格のときは
    キー自体を作らない (`None` を渡さない呼び出し元の既定)。

    [indicator-consumption-wiring] T5b Step 5-4c: `started` は F4 の裏
    (成功応答にも `started` キーを載せる) — 子が解放しない判定を
    キーの有無ではなく値で行えるようにする。"""
    reply = {
        "scope": save_kwargs["scope"],
        "pair": save_kwargs["pair"],
        "timeframe": save_kwargs["timeframe"],
        "source": save_kwargs["source"],
        "metrics": save_kwargs["metrics"],
        "trial_count": 1,
    }
    if submission_blocked is not None:
        reply["submission_blocked"] = submission_blocked
    reply["started"] = started
    return _BacktestReply(reply, save_kwargs)


@dataclass(frozen=True)
class _InspectionVerdict:  # 新規命名
    ok: bool
    reason: str = ""
    risk_gate_unsupported: bool = False
    # A36 裁定 (2026-08-28、束D検収 verified-local-round1.md §7):
    # `out_of_partition` field を撤去した — `commit()` はこの値を一度も
    # 読まなかった (dead field、`grep -n "\.out_of_partition" src/` で
    # `_inspect_output` 内の代入以外にヒット無し)。実質の産物は
    # activity ログ 1 行 (`_inspect_output` 内、下記) のみで、それは
    # 維持する。


@dataclass(frozen=True)
class _SelectionOutcome:  # 新規命名
    won: bool
    backlog_id: int | None
    # [unprofitable-note-hygiene] 設計書 v2.0 §2 (専用列方式への作り直し):
    # `inserted_ids` は撤回。起票時の系譜は INSERT 時点で
    # `improvement_backlog.origin_mission_id` へ直接書く (`commit()` 側で
    # 後から絞り込む必要がなくなった)。


def _empty_inventory_result(plugins_root: Path):
    """[indicator-consumption-wiring] T4b: `inventory` が渡されない直接
    テスト呼び出し (conn を持たない経路) 用のフォールバック — 空
    `InventoryBuildResult`。`is_relock_transition` の再ロック例外は
    「現在 inventory」を引けないため常に False になる (安全側)。"""
    from agentic_fx.plugin.resolve import ApprovedInventory, InventoryBuildResult
    inv = ApprovedInventory(root=plugins_root.resolve(), metas=())
    return InventoryBuildResult(inventory=inv, phase1_metas=(), resolved={},
                                rejected_strategies=())


@dataclass(frozen=True)
class _PluginGateVerdict:  # 新規命名
    passed: bool
    reason: str = ""
    content_hash: str | None = None
    artifact_hash: str | None = None


@dataclass(frozen=True)
class _DuplicateDemotion:
    """/code-review 2 周目 CR1 是正 (2026-09-12): `_finalize_success` の
    質検査 (§A) が既承認候補との metrics 一致を見つけたときに返す降格通知。
    `_finalize_success` は自分では observation 経路を呼ばない (内側 tx を
    rollback した直後は呼び出し元の `commit()` が持つ `report_path`/
    `artifact` 変数を書き換える立場にないため) — `commit()` が戻り値で
    受け取り、`_finalize_report_or_observation` へルーティングする。"""
    content_hash: str
    pair: str


def _artifact_hash_of(plugin_py: bytes, config_yaml: bytes,
                      test_plugin: bytes) -> str:
    h = hashlib.sha256()
    h.update(b"plugin.py\0" + plugin_py + b"\0config.yaml\0" + config_yaml
             + b"\0test_plugin.py\0" + test_plugin)
    return h.hexdigest()


def copy_source_snapshot(metas: list, *, dest_root: Path,
                         plugin_lock: threading.Lock) -> list[str]:
    """稼働中 registry の固定 `PluginMeta.path` (版ディレクトリ実体) から
    承認済み plugin 3 本を読取専用スナップショットへコピーする
    (設計書 §3.4/§4 冒頭、プラン §8.1-11)。**live symlink `plugins/<name>`
    は一切参照しない** — `meta.path` は discover 時点で symlink 解決済みの
    実体パスとして registry が既に保持している (Task 5 の産物)。

    C2 是正 (束D検収, verified-local-round1.md §11 #20): **読み取ったバイト
    列に対して** hash を照合してから書き込む (`read_bytes()` → hash 再計算
    → 一致確認 → その後 `write_bytes()` の順)。以前の docstring は
    「コピー完了後に再計算」と書いていたが実装は逆順 — ただし防御の実効
    (混成版/版切替の検出) は変わらない: 書き込むバイト列は hash を取った
    バイト列そのものなので、`meta.artifact_hash` との不一致は書き込み前に
    確実に検出される。
    """
    dest_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    copied_names: list[str] = []
    with plugin_lock:
        for meta in metas:
            plugin_py = (meta.path / "plugin.py").read_bytes()
            config_yaml = (meta.path / "config.yaml").read_bytes()
            test_plugin = (meta.path / "test_plugin.py").read_bytes()
            recomputed = _artifact_hash_of(plugin_py, config_yaml, test_plugin)
            if recomputed != meta.artifact_hash:
                raise ValueError(
                    f"plugin {meta.name!r}: artifact_hash mismatch after "
                    "copy (expected "
                    f"{meta.artifact_hash}, got {recomputed}) — live version "
                    "may have switched mid-copy or files came from mixed "
                    "versions")
            target = dest_root / meta.name
            target.mkdir(parents=True, exist_ok=True)
            (target / "plugin.py").write_bytes(plugin_py)
            (target / "config.yaml").write_bytes(config_yaml)
            (target / "test_plugin.py").write_bytes(test_plugin)
            copied_names.append(meta.name)
    _chmod_tree_readonly(dest_root)
    return copied_names


def copy_examples_snapshot(examples_root: Path, *, dest_root: Path) -> None:
    """`docs/examples/plugins/*` を `source/_examples/<name>/` へ読取専用
    コピーする (worker は repo の `docs/` を Landlock で読めない — Task 5)。
    """
    dest_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not examples_root.exists():
        return
    for entry in sorted(examples_root.iterdir()):
        if not entry.is_dir():
            continue
        target = dest_root / entry.name
        for fname in ("plugin.py", "config.yaml", "test_plugin.py"):
            src_file = entry / fname
            if src_file.exists():
                target.mkdir(parents=True, exist_ok=True)
                (target / fname).write_bytes(src_file.read_bytes())
    _chmod_tree_readonly(dest_root)


def _chmod_tree_readonly(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        for fname in filenames:
            p = Path(dirpath) / fname
            if p.is_symlink():
                continue
            p.chmod(0o400)
        p = Path(dirpath)
        if p.is_symlink():
            continue
        p.chmod(0o500)


def build_inventory_view(result: "InventoryBuildResult") -> dict:
    """[indicator-consumption-wiring] §2.9(b): 子 worker の tool が読む
    JSON-safe な inventory。**snapshot ディレクトリを列挙しない** —
    phase 2 で落ちた strategy が inventory に混ざらないようにするため、
    最終 admit 済 plugin だけを載せる。遮断 8 との照合: 載るのは
    name / kind / pairs / params / outputs / content_hash のみ
    (成績・期間・段名は一切含まない)。"""
    return {
        "plugins": [
            {"name": m.name, "kind": m.kind, "pairs": list(m.pairs),
             "params": m.params,
             "outputs": (list(m.outputs) if m.outputs is not None else None),
             "content_hash": m.content_hash}
            for m in result.inventory.metas],
        "pin_broken_strategies": [
            {"name": r.name, "alias": r.alias, "reason": r.reason}
            for r in result.rejected_strategies],
    }


# Task 12 Step3: `_prepare_report_if_applicable` が OSError を捕まえて
# その場で Mission を終端させたことを `commit()` に伝えるための sentinel
# (`None` は「report 対象外 (indicator/plugin や risk_gate)」の既存の
# 正常値なので流用できない)。
_REPORT_WRITE_FAILED = object()


def _tool_calls_suffix(tool_calls: int | None) -> str:
    """A4 10 回目 #71 観測 B (2026-09-11): registry 経由の tool 呼び出しが
    0 件で終端した improve mission は「agent が自主的に観察を選んだ」
    状態と同じ記録になり区別がつかない。`MissionResult.tool_calls`
    (`WorkerRunner` が improve/非 local backend の終端でだけ埋める) を
    終端 activity 行に ` tool_calls=<N>` として明示する。`None`
    (local backend、または counters 未取得) では何も付けない — 既存の
    local backend 終端 activity の文面を変えない。"""
    if tool_calls is None:
        return ""
    return f" tool_calls={tool_calls}"


class ImproveLoop:
    # INDEX.md の追記を直列化する (プロセス内、slot スレッド間で共有)。
    _archive_index_lock = threading.Lock()

    # precheck 2026-08-22 wave2: T10-B10 — Rag はここで注入を受ける
    # (`_build_worker_runner` が独自シグネチャで new しない)
    def __init__(self, *, root: Path, settings: "Settings", clock: "Clock",
                 db_write_conn_factory: Callable[[], sqlite3.Connection],
                 db_readonly_conn_factory: Callable[[], sqlite3.Connection],
                 activity: "ActivityLog", rag: "Rag") -> None:
        self._root = root
        self._settings = settings
        self._clock = clock
        self._db_write_conn_factory = db_write_conn_factory
        self._db_readonly_conn_factory = db_readonly_conn_factory
        self._activity = activity
        self._rag = rag

    # precheck 2026-08-22 wave2: T10-B2/R-D1 — on_ready は
    # `_build_worker_runner` を経由して `WorkerRunner(on_ready=...)` へ渡す。
    # `ImproveSupervisor._launch_slot` (Task 9, on_ready で mark_running を
    # 行う) が指定する。省略時は None (WorkerRunner 既定と同じ、手動
    # one-shot=`submit_manual` は on_ready 不要なので省略する)
    def prepare(self, *, slot_key: tuple[str, int] | None, now: datetime,
               on_ready: Callable[[dict], None] | None = None,
               ) -> tuple[Mission, ImproveRunContext, "WorkerRunner"]:
        conn = self._db_write_conn_factory()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                mission_id = missions_store.start(
                    conn, "improve", self._settings.runner.improve.backend,
                    self._settings.runner.improve.model, now=now, commit=False)
                run_id = improve_runs_store.start(
                    conn, backlog_id=None, mission_id=mission_id, now=now,
                    commit=False)
                if slot_key is not None:
                    period_key, k = slot_key
                    claimed = improve_waves.claim_slot(
                        conn, period_key=period_key, k=k, mission_id=mission_id,
                        now=now, commit=False)
                    if not claimed:
                        raise RuntimeError(
                            f"slot claim failed for {slot_key!r} — "
                            "already claimed by a concurrent process")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        except BaseException:
            # ACC-B1 是正 (束D検収、verified-local-round1.md §11 #1):
            # Tx-0 *自体*の失敗 (`claim_slot` の CAS 失敗を含む) は
            # rollback 済みでもこの経路のまま prepare() を抜けると、
            # post-Tx0 失敗経路 (下の except BaseException: @227-)
            # の対称 close には到達しない — write conn が漏れる
            # (`probe_leakrate.py` 実測: 1 回につき厳密に 2 fd、retry
            # loop 導入で `_launch_slot` 1 回あたりの露出が 2 倍)。
            # 成功時は下の finally 相当の close 判断 (line ~283) が
            # 別途処理するので、ここでは失敗時のみ close する。
            if getattr(self, "_conn_for_test", None) is None:
                conn.close()
            raise
        # write 接続は commit 相 (10.11 節) の同一接続を使い回す — Tx-0
        # 成功時はここで close しない (呼び出し元が prepare→run→commit
        # を通して所有する。10.9 節で最終形に確定する)

        # I3 是正 (プラン10 束D round1、ユーザー裁定 D②、2026-08-28):
        # Tx-0 は上で durable commit 済み。ここから先 (workspace/context/
        # tools/runner 構築) は try/except を持たなかったため、例外が
        # 起きると Tx-0 で作った mission(running)/run(未終端)/slot(claimed)
        # が dangling のまま残っていた。主害は dangling そのものより
        # `ImproveSupervisor._running_slot_count`
        # (`WHERE status IN ('claimed','running')`) が容量を恒久的に
        # 食い潰すこと — 既定 `improve.parallel=1` では改善ループが
        # 再起動まで完全停止する (`service.py:946` の
        # `capacity=settings.improve.parallel`、`config.py:187` の
        # `default=1`)。`prepare()` 内部で自作の mission/run/slot を
        # 1 tx で終端化してから re-raise する — mission_id/run_id を
        # 知る唯一の点が `prepare()` 内部であるため (`_compensate_tx2_
        # failure`、10.10 節と同型)。
        try:
            allowed_ids = self._compute_partition_hint(conn, slot_key)
            staging_dir, source_snapshot_dir, inventory_result = \
                self._materialize_workspace(conn, mission_id, allowed_ids)
            ledger = ImproveRpcLedger(
                rpc_timeout_sec_by_kind=self._improve_rpc_timeout_sec_by_kind())
            rpc_handlers = self._build_rpc_handlers(
                ledger, staging_dir=staging_dir, inventory=inventory_result)
            ctx = ImproveRunContext(
                mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
                source_snapshot_dir=source_snapshot_dir,
                allowed_backlog_ids=allowed_ids, slot_key=slot_key, ledger=ledger,
                rpc_handlers=rpc_handlers,
                inventory=inventory_result,
                inventory_view=build_inventory_view(inventory_result))

            # precheck 2026-08-22: T8-B8 追随 -- root= が欠落し戻り値キー
            # `prompt_text` も非実在だった (build_improve_context は
            # performance_report/improvement_history/current_inventory/backlog/
            # user_policy/references の 6 キーの生データ辞書を返すのみ)。
            # レンダリング (テンプレート .format()) は本メソッドの責務 —
            # 8-I の対応表 (プレースホルダ⇔戻り値キー) に従って組み立てる。
            ctx_data = build_improve_context(
                conn, settings=self._settings, now=now, root=self._root,
                allowed_backlog_ids=allowed_ids)
            prompt_text = self._render_improve_mission_prompt(ctx_data, ctx=ctx)
            # precheck 2026-08-22 wave2: T10-B12/B17 — tools/output_schema は
            # 10.9 節 Step 12 (`_build_mission_tools`) で完成させる。ここでは
            # その呼び出しに置き換えるだけ (完全実装は 10.9 節参照)
            mission_tools = self._build_mission_tools(
                staging_dir=staging_dir, source_snapshot_dir=source_snapshot_dir,
                ledger=ledger, rpc_handlers=rpc_handlers)
            mission = Mission(prompt=prompt_text, tools=mission_tools,
                              output_schema=IMPROVE_OUTPUT_SCHEMA,
                              max_turns=self._settings.improve.mission_max_turns,
                              timeout_sec=self._settings.improve.mission_timeout_sec)
            # precheck 2026-08-22 wave2: T10-B2/R-D1
            runner = self._build_worker_runner(ctx, on_ready=on_ready)
        except BaseException:
            _log.exception(
                "improve prepare failed after Tx-0 for mission_id=%s "
                "slot_key=%r", mission_id, slot_key)
            self._compensate_prepare_failure(
                conn, mission_id=mission_id, run_id=run_id,
                slot_key=slot_key, now=now)
            if getattr(self, "_conn_for_test", None) is None:
                conn.close()
            raise
        # <!-- precheck 2026-08-24 D-10 是正: 逸脱申告 -->
        # `commit()` (10.11 節) は `_conn_for_test` シームがあれば close
        # しない (テスト共有 conn を守る)。`prepare()` にはこの対称が無く
        # 無条件 close だった — production では factory が呼び出しごとに
        # 新規接続を返すため実害は無いが、テスト用に同一 conn を返す
        # factory (tests/loops/conftest.py) では `prepare()` が返した後に
        # 呼び出し元が同じ `conn` で状態確認できなくなる。commit() と同じ
        # シームで対称化する (プラン L16312-16314 のコメント「ここで
        # close しない」の意図とも整合)。
        if getattr(self, "_conn_for_test", None) is None:
            conn.close()
        return mission, ctx, runner

    def _compensate_prepare_failure(self, conn, *, mission_id, run_id,
                                    slot_key, now) -> None:
        """I3 是正 (プラン10 束D round1、ユーザー裁定 D②、2026-08-28):
        Tx-0 commit 後の `prepare()` 例外を、自作の mission/run/(あれば)
        slot に限って 1 tx で終端化する (`_compensate_tx2_failure`、
        10.10 節と同型)。`slot_key=None` (手動 one-shot、`submit_manual`)
        のときは slot が存在しないので `finish_improve_mission
        (slot_key=None, ...)` で mission/run だけ終端化する。"""
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                missions_store.finish_improve_mission(
                    conn, mission_id=mission_id, run_id=run_id,
                    slot_key=slot_key, mission_status="failed",
                    run_result=None, backlog_transition=None, now=now,
                    commit=False)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        # C6 裁定 (2026-08-28、束D検収 verified-local-round1.md §7、
        # 現状維持): 内側 tx は `except BaseException:` だが、この外側
        # (補償 tx 自体の失敗を握る経路) は意図的に `except Exception:` —
        # `KeyboardInterrupt`/`SystemExit` (shutdown) はここを通らず、
        # 補償を試みずに呼び出し元 (`prepare()`) へそのまま伝播する。
        # shutdown 時に「元例外の後始末」としての補償 tx を新規に開始
        # するより、即座に終了する方が安全という判断 (mission は
        # 非終端のまま残るが、起動時 reconcile が拾う設計)。
        except Exception:
            _log.exception(
                "compensation itself failed for mission_id=%s — mission "
                "stays in a non-terminal state, will be picked up by "
                "startup reconcile", mission_id)
            # ACC-B3 是正 (束D検収, verified-local-round1.md §11 #14):
            # `activity=None` で構築された `ImproveLoop` では、無条件の
            # `self._activity.write(...)` が `AttributeError` を送出し、
            # 呼び出し元 (`prepare()`) が re-raise しようとしていた元例外
            # (Tx-0 後の実失敗) を置換した上、`conn.close()` も飛ばして
            # いた (fd 漏れが復活する)。本番 (`service.py:967`) は実
            # `ActivityLog` を渡すため到達しない経路だが、テスト/将来の
            # 呼び出し元での既定引数省略に備えてガードする。
            if self._activity is None:
                return
            self._activity.write(
                Category.IMPROVE, "prepare_compensation_failed",
                f"mission_id={mission_id} run_id={run_id}")

    # round2 #5 是正 (2026-08-29): `prepare()` は Tx-0 を完走・close 済みで
    # 呼び出し元 (`ImproveSupervisor._launch_slot`) に mission/run/slot を
    # 渡す。その後 `runner.run(mission)` 自体が例外を投げる窓は I3
    # (`_compensate_prepare_failure`、prepare() 内部の例外) にも I2(b)
    # (pre-ready 失敗を runner が値で返す窓) にも属さない第 4 の窓 —
    # verified-round2.md #5。`_compensate_prepare_failure` と同型の
    # 1 tx 終端を、`_launch_slot` から呼べる形で公開する (新規 write conn
    # を自前で開く — prepare() が close した後の呼び出しのため)。
    def compensate_launch_failure(self, *, ctx: "ImproveRunContext",
                                  now: datetime) -> None:
        # R5 是正 (2026-08-30): launch failure で DB 行 (mission/run/slot) を
        # 終端化するだけでなく、staging ディレクトリ
        # (`plugins/_staging/<mission_id>/`) も削除する。`_snapshot_src` 配下は
        # `_chmod_tree_readonly` (§4/10.3 節) でディレクトリ 0500・ファイル
        # 0400 の readonly tree になっているため、後段の `sweep_orphans` の
        # `rmtree(ignore_errors=True)` では消えず恒久的に残る (D-15 是正と
        # 同一の欠陥を launch failure 経路が抱えていた)。既存の
        # `_delete_staging` (readonly を書込可に戻してから削除する実装) を
        # 呼ぶ。staging 削除は DB 終端化より先に、例外を握って行う —
        # staging 削除の失敗 (OSError 等) が DB 終端化 (`_compensate_prepare_
        # failure`) を妨げると、mission/run/slot が dangling のまま残る
        # (I3 と同じ主害: `_running_slot_count` が容量を恒久的に食い潰す)。
        try:
            self._delete_staging(ctx)
        except Exception:
            _log.exception(
                "staging deletion failed during compensate_launch_failure "
                "for mission_id=%s — continuing with DB terminalization",
                ctx.mission_id)
        conn = self._db_write_conn_factory()
        try:
            self._compensate_prepare_failure(
                conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
                slot_key=ctx.slot_key, now=now)
        finally:
            if getattr(self, "_conn_for_test", None) is None:
                conn.close()

    def compensate_commit_failure(
            self, *, ctx: "ImproveRunContext", now: datetime,
            exc: BaseException, slot_terminalize: bool = True) -> None:
        """`commit()` 例外後に mission/run/(必要なら slot) を終端する。

        失敗した `commit()` の接続は transaction 状態が不明なため再利用せず、
        新しい write 接続上の `BEGIN IMMEDIATE` で補償する。mission の終端は
        `finish_improve_mission()` の ``WHERE status='running'`` CAS に守られ、
        既に終端済みなら補償は状態を上書きしないため冪等である。
        補償自体の失敗は元の `commit()` 例外を置換しないよう握りつぶす。
        """
        conn = None
        try:
            ctx.ledger.freeze_if_open(
                drain_timeout_sec=self._settings.improve.accept_drain_sec,
                on_timeout=lambda dropped: self._activity.write(
                    Category.IMPROVE, "ledger_freeze_timeout",
                    f"mission={ctx.mission_id} dropped={dropped}"))
            self._activity.write(
                Category.IMPROVE, "mission_failed",
                f"mission={ctx.mission_id} status=failed "
                f"reason=commit_crashed:{type(exc).__name__}:{str(exc)[:300]}")
            try:
                self._delete_staging(ctx)
            except Exception:
                _log.exception(
                    "staging deletion failed during commit compensation for "
                    "mission_id=%s — continuing with DB terminalization",
                    ctx.mission_id)

            conn = self._db_write_conn_factory()
            state = ctx.ledger.state()
            if state == "DISCARDED":
                return
            conn.execute("BEGIN IMMEDIATE")
            ledger_ids = [] if state == "PERSISTED" else None
            try:
                mission_row = conn.execute(
                    "SELECT status FROM missions WHERE id=?",
                    (ctx.mission_id,)).fetchone()
                terminal = mission_row is not None and mission_row["status"] != "running"
                if state in ("FROZEN", "PERSIST_FAILED"):
                    already_saved = conn.execute(
                        "SELECT 1 FROM backtest_runs WHERE mission_id=? "
                        "AND mission_outcome='commit_failed' LIMIT 1",
                        (ctx.mission_id,)).fetchone()
                    if not already_saved:
                        ledger_ids = self._persist_ledger_in_tx(
                            conn, ctx=ctx, now=now,
                            mission_outcome="commit_failed")
                    else:
                        ledger_ids = []
                # `missions.recover_interrupted` と同じ selected backlog の
                # 収束。関数自体は全 running mission を対象に別 tx を開始
                # するため再利用せず、この mission の run だけに限定する。
                if not terminal:
                    conn.execute(
                        "UPDATE improvement_backlog SET status='observation', "
                        "last_result='interrupted', updated_at=? WHERE id=("
                        "SELECT backlog_id FROM improvement_runs WHERE id=?"
                        ") AND status='selected'",
                        (now.isoformat(), ctx.run_id))
                    missions_store.finish_improve_mission(
                        conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
                        slot_key=ctx.slot_key if slot_terminalize else None,
                        mission_status="failed", run_result=None, now=now,
                        backlog_transition=None, commit=False)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            if state in ("FROZEN", "PERSIST_FAILED"):
                if ledger_ids is None:
                    if state == "FROZEN":
                        ctx.ledger.mark_persist_failed()
                else:
                    ctx.ledger.mark_persisted()
            self._write_archive_index_safe(
                conn, ctx=ctx, status="commit_failed", now=now)
        except Exception:
            _log.exception(
                "commit failure compensation itself failed for mission_id=%s "
                "— preserving original commit exception", ctx.mission_id)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    _log.exception(
                        "write connection close failed during commit "
                        "compensation for mission_id=%s", ctx.mission_id)

    # precheck 2026-08-22 pass2: RB4 — Step 2a で定義した失敗するテストへの
    # 最小実装。8-I 節「プレースホルダ⇔戻り値キーの対応表 (B8)」の 17 項目
    # をそのまま埋める。self の属性には依存しない (Step 2a のテストが
    # ImproveLoop.__new__ で検証できる理由)。
    def _render_improve_mission_prompt(self, ctx_data: dict, *, ctx) -> str:
        perf = ctx_data["performance_report"]
        hist = ctx_data["improvement_history"]
        inv = ctx_data["current_inventory"]
        bl = ctx_data["backlog"]
        policy = ctx_data["user_policy"]
        refs = ctx_data["references"]

        def _history_table(rows: list[dict]) -> str:
            if not rows:
                return "(履歴なし)"
            header = ("| id | backlog | idea | result | attempts | "
                      "last_result |\n|---|---|---|---|---|---|\n")
            return header + "\n".join(
                f"| {r.get('id')} | {r.get('backlog_id')} | {r.get('idea')} | "
                f"{r.get('result')} | {r.get('attempts')} | "
                f"{r.get('last_result')} |" for r in rows)

        def _backlog_table(items: list[dict]) -> str:
            # [unprofitable-note-hygiene] 設計書 v2.0 §2-3: `origin` 列。
            # 専用列 `origin_outcome` が `'unprofitable'` の行だけ
            # `unprofitable` を出す — 他行は空文字。`last_result` には
            # 一切触れない (`origin_outcome` は他の書き手が触らない
            # 専用列なので、判定は完全一致でも部分一致でもなく単純な
            # 値比較で足りる)。
            if not items:
                return "(バックログなし)"
            header = ("| id | idea | status | attempts | assigned | "
                      "origin |\n|---|---|---|---|---|---|\n")
            return header + "\n".join(
                f"| {i.get('id')} | {i.get('idea')} | {i.get('status')} | "
                f"{i.get('attempts')} | {i.get('assigned', '')} | "
                f"{'unprofitable' if i.get('origin_outcome') == 'unprofitable' else ''} |"
                for i in items)

        # [indicator-consumption-wiring] T5a Step 5-1c (P1'): pin 破れで
        # 配備から外れている strategy の件数・名前を prompt に出す。
        # `ctx` は `synthetic_ctx` / テスト直組みでは `None` もありうる
        # ため `ctx.inventory_view` を防御的に既定 `{}` へ落とす。
        pin_broken = (ctx.inventory_view.get("pin_broken_strategies", [])
                     if ctx is not None else [])
        pin_broken_line = (
            f"pin 破れで配備から外れている strategy: {len(pin_broken)} 本 "
            f"({', '.join(p['name'] for p in pin_broken)})"
            if pin_broken else "pin 破れで配備から外れている strategy: 0 本")

        def _dict_to_line(d: dict) -> str:
            # dict の repr は `{`/`}` を含み、テンプレートの `.format()`
            # 展開結果に生の波括弧が残ってしまう (未展開プレースホルダと
            # 区別できなくなる)。カンマ区切りの key: value 行へ変換する。
            if not d:
                return "(データなし)"
            return ", ".join(f"{k}: {v}" for k, v in d.items())

        render_map = {
            "performance_window_days": perf["window_days"],
            "win_rate": perf["win_rate"],
            "profit_factor": perf["profit_factor"],
            "by_pair": _dict_to_line(perf["by_pair"]),
            "by_hour": _dict_to_line(perf["by_hour"]),
            "reject_breakdown": _dict_to_line(perf["reject_breakdown"]),
            "hold_rate": perf["hold_rate"],
            "improvement_history_table": _history_table(hist["recent_runs"]),
            # precheck 2026-08-22 wave2: T10-B4 — improve_context.py:86-92 は
            # list[dict] (name/kind/pairs, name/enabled) を返す。list[str]
            # 前提の `", ".join(list)` は本番で TypeError になるため、
            # 辞書から name を取り出して結合する
            "approved_plugins": (", ".join(
                f"{p['name']}({p['kind']})" for p in inv["approved_plugins"])
                or "(なし)"),
            "news_sources": (", ".join(
                f"{s['name']}({'on' if s['enabled'] else 'off'})"
                for s in inv["news_sources"]) or "(なし)"),
            "risk_gate_summary": _dict_to_line(inv["risk_gate"]),
            "pin_broken_strategies": pin_broken_line,
            "backlog_table": (
                "## 選べる課題 (backlog_id を selected に書けるのはここだけ)\n\n"
                + _backlog_table(bl["items"])
                + "\n\n## 既知の事実 (note、選択不可、context として参照)\n\n"
                + (_backlog_table(bl["notes"]) if bl["notes"] else "(なし)")
                + (f"\n\n(他 {bl.get('notes_omitted', 0)} 件)"
                   if bl.get("notes_omitted", 0) else "")),
            "min_test_functions": self._settings.improve.gate.min_test_functions,
            "user_policy_tail": policy["tail"],
            "plugin_name_pattern": refs["plugin_name_pattern"],
            "plugin_contract_summary": refs["plugin_contract_summary"],
            "staging_dir": str(ctx.staging_dir),
            "source_snapshot_dir": str(ctx.source_snapshot_dir),
            # [profitability-floor] T2 Step 2-2 (2026-09-13、codex I8):
            # 文言は settings からレンダする (ハードコードすると設定変更後
            # にゲートと説明が食い違う)。
            "profitability_floor_rule": self._floor_rule_text(),
        }
        template_path = (Path(__file__).resolve().parent / "prompts"
                         / "improve_mission.md")
        rendered = template_path.read_text().format(**render_map)
        # [indicator-consumption-wiring] T5a Step 5-1c: テスト専用の観測面
        # (`tests/loops/test_improve_loop_source_snapshot.py::
        # test_prompt_shows_the_number_of_pin_broken_strategies`)。本番挙動
        # は変わらない。
        self._last_rendered_prompt = rendered
        return rendered

    def _floor_rule_text(self) -> str:
        """[profitability-floor] T2 Step 2-2 (2026-09-13、設計書 §6 T2、
        codex I8): 収益性フロアの規律文言を `improve.gate` の実値から
        組み立てる。`.format()` は条件分岐できないため、文そのものを
        ここで組み立ててプレースホルダへ渡す —
        `require_positive_avg_r=False` のときは avg_r の条件を文から
        省く (ハードコードした固定文言にしない)。

        codex 2 周目レビュー CR3/CR1 (2026-09-13): 条件節の組み立て
        自体は共有 helper `strategy_gate.floor_rule_text` に委譲する
        (CLI・`switch._run_full_gate` と同じ実装を通す)。
        `audience="agent"` を渡すため、`require_holdout_evaluable` は
        **この文言に一切現れない** (遮断 8、設計書 §4 のただし書き —
        holdout の閾値・条件を agent に見せない)。"""
        g = self._settings.improve.gate
        condition = _strategy_gate_floor_rule_text(g, audience="agent")
        return (
            f"**{condition} の候補は提出しても承認申請になりません**"
            "(親の決定論ゲートが `unprofitable` として observation に"
            "落とします)。予算内でパラメータ・フィルタ・エントリ条件を"
            f"変えて `run_backtest` をやり直し、`pf >= {g.min_pf}`"
            + (f" かつ `avg_r > 0`" if g.require_positive_avg_r else "")
            + "を満たした候補だけを提出してください。予算を使い切っても"
            "満たせなければ**提出せず** `observation` として、試した"
            "パラメータ群とそれぞれの pf / avg_r を理由に書いてください。")

    def _compute_partition_hint(self, conn, slot_key) -> frozenset[int] | None:
        # <!-- precheck 2026-08-24 D-10 是正: 逸脱申告 -->
        # プラン L15634-20876 に `_compute_partition_hint` の具体形が無い
        # (grep 4 ヒット全て骨格スタブ/xfail 注記のみ、検収 D-1 参照)。
        # 設計書 §6「注入コンテキスト」/ まとめ表の「wave が parallel=N で
        # N partition を全て担当する」(L24685) から最小実装する:
        # `slot_key=None` (手動 one-shot) は全バックログ担当 = None (印なし、
        # プラン L16077 の既存 pin と一致)。scheduler wave は
        # `improve_wave_slots` の実 slot 数 (= wave の `expected`) を N とし、
        # open|observation な backlog id を `id % N == k` で互いに素な
        # N 分割にする — `list_open` は `select_for_mission` の CAS 対象
        # (open|observation) と同じ集合なので、ヒントと実際に選べる範囲が
        # 一致する。
        if slot_key is None:
            return None
        period_key, k = slot_key
        slots = improve_waves.list_slots(conn, period_key=period_key)
        expected = len(slots)
        if expected <= 0:
            # fail-open (None = 全担当) にしない — 呼び出し元は
            # claim_slot 済みのはずで、対応する wave が無いのは矛盾。
            raise RuntimeError(
                f"_compute_partition_hint: no wave slots for "
                f"period_key={period_key!r} — cannot derive a partition")
        open_ids = sorted(row["id"] for row in backlog_store.list_open(conn))
        partition = frozenset(i for i in open_ids if i % expected == k)
        if not partition:
            # L-B13 裁定 (2026-08-28、束D検収 verified-local-round1.md §7):
            # 空集合 (`frozenset()`) を「担当ゼロ」として黙って返すと、
            # `None` (印なし = 全担当) との区別が呼び出し元に伝わらない
            # (`_inspect_output` は空集合下で全 selected が
            # out_of_partition になる — fail-open ではないが無音)。
            # 明示的に raise し、呼び出し元 (`prepare()`) の post-Tx0
            # 失敗補償 (`_compensate_prepare_failure`) へ委ねる。
            raise RuntimeError(
                f"_compute_partition_hint: empty partition for "
                f"period_key={period_key!r} k={k} expected={expected} "
                "(no open backlog id maps to this slot)")
        return partition

    def _materialize_workspace(self, conn, mission_id, allowed_ids):
        # <!-- precheck 2026-08-23 R-D3 -->
        # 裁定 R-D3: 本メソッドの責務は「出所を用意する」ことに縮小された。
        # `staging_dir/_snapshot_src/` へ `copy_source_snapshot`/
        # `copy_examples_snapshot` (10.3 節) を使って plugins/<name> 版実体
        # + `_examples` をコピーし、その `staging_dir/_snapshot_src/` パスを
        # 返す (これが ImproveRunContext.source_snapshot_dir に入る「出所」)。
        # `workdir/source` への配置・実体化は本メソッドの責務ではない —
        # `WorkerRunner.run()` が workdir 作成直後に
        # `shutil.copytree(source_snapshot_dir, workdir/"source")` で行う
        # (Task 1 Step 33 実装時追記)。詳細は 10.3 節参照。
        # D-15 是正 (着手前検証): 設計書 §2.2/§2.3/§4.2-5 の正規形は
        # `<root>/plugins/_staging/<mission_id>/` (worker が rw で書ける
        # 唯一の場所、`switch.py::_CANDIDATE_PATH_RE["staging"]` の regex、
        # 起動時孤児 reconcile (`switch.py:215` の `plugins_root/"_staging"`
        # 走査)、`mission_worker.py:155-159` の dirfd 再検証 (`staging_path.
        # name == mission_id` かつ mode 0700) の 3 者が収束してこの形を
        # 前提にする。旧実装は `<root>/improve-staging-<mission_id>` という
        # 別系統の名前・default mode を使っており、`commit()` が記録する
        # `candidate_path` (`plugins/_staging/<mission_id>/<name>`) と実体
        # パスが一致せず承認時に恒久 `CandidateMissingError` pending になる
        # 上、`mission_worker.py` の名前照合も `"improve-staging-5" != "5"`
        # で必ず fail closed する実バグだった。実 subprocess 経路のテスト
        # (`tests/test_improve_profile_isolation.py`) は `prepare()` を
        # 経由せず自前で正規形の `staging_dir` を組むため、この不一致は
        # これまで一度も実行されたことがなかった。
        staging_dir = self._root / "plugins" / "_staging" / str(mission_id)
        staging_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(staging_dir, 0o700)  # mkdir は umask で masked、exist_ok
                                       # だと既存 dir を re-mode しないため
                                       # 明示 chmod する (mission_worker.py
                                       # の dirfd 再検証が mode 0700 を要求)

        source_snapshot_root = staging_dir / "_snapshot_src"

        # copy_examples_snapshot を先に実行し、その後 copy_source_snapshot を実行する
        # (R-D3 による権限管理: _snapshot_src が 0o500 になる前に _examples も配置)
        # <!-- precheck 2026-08-24 D-10 是正: 逸脱申告 -->
        # 元コードは `self._settings.data_root` を参照していたが
        # `Settings` に `data_root` フィールドは存在しない
        # (`AttributeError`、prepare() が一度も最後まで到達しなかった
        # ため未検出だった)。`service.py:956` の `ImproveLoop(root=root, ...)`
        # と同じ `root` (リポジトリ root) を使う — `docs/examples/plugins`
        # は設計書 §6 の記載どおりリポジトリ直下に存在する。
        examples_root = self._root / "docs" / "examples" / "plugins"
        copy_examples_snapshot(examples_root, dest_root=source_snapshot_root / "_examples")

        # <!-- precheck 2026-08-24 D-10 是正: 逸脱申告 -->
        # R-D3「discover 済みの plugin metas をここでコピーする」の実装。
        # 取得元は `build_improve_context`/`_current_inventory` と同一の
        # `tools.plugin_loader.approved_plugins` (承認済みのみ、二重実装
        # しない)。`conn` は prepare() の Tx-0 完了後 (COMMIT 済み) の
        # 同一接続を再利用する — 新規に readonly factory を開いて close
        # すると、テスト fixture (tests/loops/conftest.py) では
        # write/readonly が同一 conn オブジェクトを指すため、この中で
        # close すると呼び出し元 prepare() が続けて使う conn まで壊れる
        # (D-3 が突き止めた「テスト factory が共有 conn を返すと閉じた
        # 接続を掴む」と同じ罠を作らない)。
        plugins_dir = self._root / "plugins"
        inventory_result = approved_plugins(conn, plugins_dir,
                                            settings=self._settings)
        metas = list(inventory_result.phase1_metas)   # snapshot 材料は第 1 相
        copy_source_snapshot(metas, dest_root=source_snapshot_root,
                            plugin_lock=threading.Lock())

        # [indicator-consumption-wiring] T5a Step 5-1c: `prepare()` が
        # `ImproveRunContext.inventory` / `.inventory_view` の唯一の生成元。
        # ここで一度だけ構築した `inventory_result` を呼び出し元へ返す
        # (第 2 相フィルタ済み — snapshot 材料の第 1 相 `metas` とは別)。
        return staging_dir, source_snapshot_root, inventory_result

    def _build_rpc_handlers(self, ledger: "ImproveRpcLedger", *,
                            staging_dir: Path,
                            inventory: "InventoryBuildResult | None" = None,
                            ) -> dict:
        """10.9 Step 11: run_backtest/analyze_corr の親側実装。

        A34 裁定 (2026-08-28、束D検収 verified-local-round1.md §7、
        規約として明文化): 戻り値の `period`/`now` 除去 (遮断7) は**この
        handler の責務ではない** — `tools/improve_rpc_tools.py::
        build_improve_rpc_tooldefs` の `_strip_forbidden` (tooldef 層)
        が担う。`run_backtest_handler`/`analyze_corr_handler` は
        save_kwargs をそのまま返す (`_persist_ledger_rows` が読む契約、
        10.10 節) — ここで剥がすと台帳の記録内容まで痩せてしまう
        (`_strip_forbidden` の docstring 「台帳は痩せない」参照)。
        **この handler をここで返す辞書以外の経路 (tooldef を経由しない
        直接呼び出し) で agent へ晒してはならない** — 遮断7 が抜ける。
        `_build_rpc_handlers` の戻り値は必ず
        `build_improve_rpc_tooldefs(run_backtest_handler=...,
        analyze_corr_handler=...)` を経由させること (設計書 §6 注記)。"""
        from agentic_fx.backtest.analysis import analyze_for_agent
        from agentic_fx.plugin import loader as plugin_loader
        from agentic_fx.plugin import strategy_adapter
        from agentic_fx.plugin.version_store import (
            _fsync_dir, _write_ro_file, artifact_hash_bytes, content_hash_bytes,
        )

        def run_backtest_handler(args: dict) -> dict:
            candidate_dir = staging_dir / args["name"]
            meta = plugin_loader._discover_one(candidate_dir, args["name"])
            if meta is None or meta.kind != "strategy":
                from agentic_fx.tools.improve_rpc_tools import _RUN_BACKTEST_KIND_HINT
                raise RuntimeError(
                    f"run_backtest requires a strategy candidate: "
                    f"{args['name']!r}. {_RUN_BACKTEST_KIND_HINT}")
            try:
                plugin_py = (candidate_dir / "plugin.py").read_bytes()
                config_yaml = (candidate_dir / "config.yaml").read_bytes()
                test_plugin = (candidate_dir / "test_plugin.py").read_bytes()
            except OSError:
                return {"error": "loader_rejected: content changed during backtest"}
            if content_hash_bytes(plugin_py, config_yaml) != meta.content_hash:
                return {"error": "loader_rejected: content changed during backtest"}
            # [indicator-consumption-wiring] §2.9(c): 解決は**親でしか
            # できない** (`ImproveRunContext.inventory` は親にしかない)。
            # 予約は子で先に起きているので、ここで未解決なら backtest を
            # 走らせずに `started: false` を返し、**子が予約を戻す**。
            # 探索中なので `pin_mode="check"` (pin があれば一致を要求、
            # 無ければ通す)。`available` は inventory の indicator 名だけ
            # (staging・examples は含まない)。
            from agentic_fx.plugin.resolve import (
                IndicatorResolutionError, resolve_indicator_deps,
            )
            # [indicator-consumption-wiring] T5b 逸脱是正: `inventory` は
            # `prepare()` 経由の本番経路では常に非 None だが、
            # `tests/loops/test_improve_loop_rpc_handlers.py` は
            # `_build_rpc_handlers(ledger, staging_dir=...)` を `inventory=`
            # 省略で直接呼ぶ既存テストを多数持つ (T4b の `_run_plugin_gate`
            # と同じ「直接呼び出しテスト用フォールバック」— `_empty_
            # inventory_result` を流用する)。
            inv = (inventory if inventory is not None
                  else _empty_inventory_result(self._root / "plugins"))
            try:
                resolved = resolve_indicator_deps(
                    meta, inv.inventory, settings=self._settings,
                    pin_mode="check")
            except IndicatorResolutionError as exc:
                return {
                    "started": False, "error": "indicator_unresolved",
                    "alias": exc.alias, "reason": exc.reason,
                    "available": sorted(
                        m.name for m in inv.inventory.metas
                        if m.kind == "indicator" and m.outputs is not None),
                }
            try:
                conn = self._db_readonly_conn_factory()
                captured: list[dict] = []
                dataset = self._settings.backtest.dataset()
                intent_source = strategy_adapter.build_intent_source(
                    meta, conn=conn, pair=args["pair"], dataset=dataset,
                    settings=self._settings, resolved=resolved)
                try:
                    holdout.run_in_sample(
                        self._settings, history_conn=conn, symbol=args["pair"],
                        dataset=dataset,
                        intent_source=intent_source,
                        eval_timeframe=meta.timeframe,
                        plugin_ref=f"plugins/_staging/{staging_dir.name}/"
                                   f"{args['name']}",
                        content_hash=meta.content_hash, kind="strategy",
                        now=self._clock.now(), record_fn=captured.append)
                finally:
                    intent_source.close()
                    conn.close()
            except ValueError as exc:
                message = str(exc)
                if isinstance(exc, holdout.NoHistoryError):
                    # settings.pairs (設定 pair) を「available」と言っては
                    # いけない — pair_rules に居てもデータが無い pair が
                    # あり得る (m47 の EURUSD がまさにそれで、モデルを
                    # 死路に再誘導する)。実際に 1m 履歴が存在する symbol
                    # を DB から出す。
                    symbols: list[str] = []
                    try:
                        hist_conn = self._db_readonly_conn_factory()
                        try:
                            symbols = sorted(
                                row[0] for row in hist_conn.execute(
                                    "SELECT DISTINCT symbol FROM ohlcv_history"
                                    " WHERE interval=? AND source=?",
                                    (dataset.base_interval, dataset.source)))
                        finally:
                            hist_conn.close()
                    except Exception:
                        pass  # hint 構築の失敗で error 応答自体を壊さない
                    if symbols:
                        hint = (f"No {dataset.base_interval} history for {args['pair']}. Pairs "
                                "with local backtest history: "
                                f"{', '.join(symbols)}.")
                    else:
                        hint = (f"No {dataset.base_interval} history for {args['pair']}, and no "
                                "pair has local backtest history yet — "
                                "run_backtest cannot succeed until history "
                                "data is imported. Report this as a "
                                "discovery instead of retrying other pairs.")
                    return {
                        "error": "no_history_for_symbol",
                        "message": message,
                        "hint": hint,
                    }
                if " is not in plugin " in message and "declared pairs" in message:
                    declared = ", ".join(meta.pairs)
                    return {
                        "error": "pair_not_declared_by_plugin",
                        "message": message,
                        "hint": ("Request one of the plugin's declared pairs: "
                                 f"{declared}."),
                    }
                _log.exception("run_backtest_handler failed for %r",
                               args.get("name"))
                return {"error": "backtest_failed"}
            except Exception:
                _log.exception("run_backtest_handler failed for %r",
                               args.get("name"))
                return {"error": "backtest_failed"}
            save_kwargs = captured[0]
            # [profitability-floor] T2 Step 2-1 (2026-09-13、設計書 §6
            # T2、codex I7): `submission_blocked` は親 handler (ここ) の
            # 1 箇所だけで導出する — `self._settings` を既に持っている
            # ため、`build_improve_rpc_tooldefs`/`build_rpc_handlers`/
            # `tools/mission_registry.py` のシグネチャは変えない (子側
            # tooldef の `_strip_forbidden` は禁止キーのみを剥がすので
            # このキーはそのまま agent へ通る)。**in_sample 段のみ判定
            # (holdout は一切参照しない)**。判定は
            # `_check_profitability_floor` を再利用する (閾値のハード
            # コード禁止、T1 と同じ関数)。
            floor_label, floor_detail_text = _check_profitability_floor(
                {args["pair"]: save_kwargs["metrics"]},
                settings=self._settings, scope="in_sample")
            submission_blocked = (
                {"reason": "unprofitable",
                 "detail": f"{floor_detail_text} — この成績では親ゲートが"
                           "承認申請を出さず observation になります"}
                if floor_label else None)
            if ledger.state() != "OPEN" or _current_rpc_abandoned():
                # /code-review 2 周目 CR4 (2026-09-11): timeout 後に完了した
                # handler は記録されない (受理境界) — snapshot も書かない
                # (書くと commit 後の plugins/_archive に孤立 tmp が残る)。
                # codex 2 周目 Important: state の読み取りと release/freeze
                # は原子的でないため、release 済み worker thread かどうか
                # (`_current_rpc_abandoned`) も合わせて見る。
                self._activity.write(
                    Category.IMPROVE, "archive_skipped_late",
                    f"mission={staging_dir.name} name={args.get('name')}")
                return _backtest_reply_from_save_kwargs(
                    save_kwargs, submission_blocked=submission_blocked)
            artifact_hash = artifact_hash_bytes(
                plugin_py, config_yaml, test_plugin)
            archive_tmp = (self._root / "plugins" / "_archive" /
                           staging_dir.name /
                           f".tmp-{artifact_hash}-{uuid.uuid4()}")
            try:
                archive_tmp.mkdir(parents=True, mode=0o700)
                _write_ro_file(archive_tmp / "plugin.py", plugin_py)
                _write_ro_file(archive_tmp / "config.yaml", config_yaml)
                _write_ro_file(archive_tmp / "test_plugin.py", test_plugin)
                metadata = {
                    "name": meta.name, "content_hash": meta.content_hash,
                    "artifact_hash": artifact_hash, "pair": args["pair"],
                    "metrics": save_kwargs["metrics"],
                    "created_at": self._clock.now().isoformat(),
                }
                _write_ro_file(
                    archive_tmp / "meta.json",
                    json.dumps(metadata, sort_keys=True).encode())
                _fsync_dir(archive_tmp)
                os.chmod(archive_tmp, 0o500)
                _fsync_dir(archive_tmp.parent)
            except Exception as exc:
                _log.exception("archive snapshot failed for %r", args.get("name"))
                self._activity.write(
                    Category.IMPROVE, "archive_failed",
                    f"mission={staging_dir.name} name={args.get('name')} "
                    f"reason={type(exc).__name__}:{str(exc)[:300]}")
            else:
                # codex 2 周目 Important (2026-09-11): 書き込み中に
                # release/freeze が割り込んだ場合、今書いた tmp を残さず
                # 消す (post-write の再確認 — 事前確認だけでは check-then
                # -act 競合窓を閉じ切れない)。
                if ledger.state() != "OPEN" or _current_rpc_abandoned():
                    self._remove_archive_tmp(archive_tmp)
                    self._activity.write(
                        Category.IMPROVE, "archive_skipped_late",
                        f"mission={staging_dir.name} name={args.get('name')} "
                        f"reason=post_write")
                else:
                    save_kwargs["artifact_hash"] = artifact_hash
                    save_kwargs["archive_tmp"] = str(archive_tmp)
            return _backtest_reply_from_save_kwargs(
                save_kwargs, submission_blocked=submission_blocked)

        def analyze_corr_handler(args: dict) -> dict:
            try:
                conn = self._db_readonly_conn_factory()
                try:
                    result = analyze_for_agent(
                        conn, self._settings, args, now=self._clock.now(),
                        persist=False)
                finally:
                    conn.close()
            except Exception:
                _log.exception("analyze_corr_handler failed")
                return {"error": "analyze_failed"}
            # /code-review 2 周目 CR3 (2026-09-11): 台帳は痩せない — 親
            # wrapper が private (= .save_kwargs) を記録するので、公開 dict
            # (遮断 7 で in_sample_until 等が剥がれる) とは別に全量を載せる。
            return _BacktestReply(result, dict(result))

        return {"run_backtest": run_backtest_handler,
                "analyze_corr": analyze_corr_handler}

    def _build_mission_tools(self, *, staging_dir, source_snapshot_dir,
                             ledger, rpc_handlers) -> list[dict]:
        """10.9b Step 12-1: Mission.tools を名前列挙専用の registry から取得。"""
        from agentic_fx.tools.mission_registry import build_mission_registry

        registry = build_mission_registry(
            "improve", None, self._settings, None, None, activity=None,
            staging_dir=staging_dir, source_snapshot_dir=source_snapshot_dir,
            ledger=ledger, rpc_handlers=rpc_handlers)
        names = registry.names()
        return registry.openai_tools(names)

    def _build_worker_runner(self, ctx: ImproveRunContext, *,
                             on_ready: Callable[[dict], None] | None = None) -> "WorkerRunner":
        """10.9 Step 7: ImproveRunContext を WorkerRunner へ渡す。"""
        from agentic_fx.runners.worker_runner import WorkerRunner

        # <!-- precheck 2026-08-24 D-10 是正: 逸脱申告 -->
        # 元コードは `Rag(self._db_readonly_conn_factory)` — `Rag.__init__`
        # の第 1 引数は `data_dir: Path` であり、conn factory (callable) を
        # 渡すのは型不一致 (str(factory) が chroma の永続化先パスに解釈され、
        # `<function ...>/` という実在しないディレクトリを cwd に作ってしまう
        # 実害を確認)。クラス docstring 冒頭のコメント (T10-B10:
        # 「Rag はここで注入を受ける (`_build_worker_runner` が独自
        # シグネチャで new しない)」) が既に意図を明記しており、
        # `__init__` で受け取り済みの `self._rag` を使うのが正しい実装。
        # precheck 2026-08-27 Task13 Step0 是正 (R-D2 dead code): 子
        # (mission_worker.py) の run_backtest/analyze_corr RPC を親側で
        # 消費するには WorkerRunner に rpc_handlers を渡す必要がある。
        # 以前はここで省略されており (rpc_handlers=None のまま)、
        # WorkerRunner.__init__ が受け取って格納するだけで
        # dispatcher_loop から一度も参照されなかった。
        reservations: dict[str, tuple[int, int]] = {}
        reservation_lock = threading.Lock()

        def on_rpc_begin(name: str) -> bool:
            reservation = ctx.ledger.begin_accept()
            if reservation is None:
                return False
            with reservation_lock:
                reservations[name] = reservation
            return True

        def release(name: str) -> None:
            with reservation_lock:
                reservation = reservations.pop(name, None)
            ctx.ledger.end_accept(reservation)

        def on_rpc_accepted(name: str, args: dict, outcome: RpcOutcome) -> None:
            # 前処理 (summary / opaque_ref / params) も finally の中 — ここで
            # 落ちると予約が残り freeze が drain 全時間を待つ (codex 1 周目)。
            try:
                summary = outcome.private or outcome.public
                if name == "run_backtest":
                    opaque_ref = f"run_backtest:{args['name']}:{args['pair']}"
                    params = {"name": args["name"], "pair": args["pair"]}
                else:
                    request = args.get("request", args)
                    opaque_ref = f"analyze_corr:{id(request)}"
                    params = request
                ctx.ledger.record(
                    opaque_ref=opaque_ref, kind=name, params=params,
                    result_summary=summary,
                    trial_count=summary.get("trial_count", 1))
            finally:
                release(name)

        return WorkerRunner(
            root=self._root, settings=self._settings, clock=self._clock,
            rag=self._rag, worker_profile="improve",
            run_context=ctx, on_ready=on_ready,
            rpc_handlers=build_rpc_handlers(ctx.rpc_handlers, ctx.staging_dir),
            rpc_timeout_sec_by_kind=self._improve_rpc_timeout_sec_by_kind(),
            on_rpc_begin=on_rpc_begin, on_rpc_accepted=on_rpc_accepted,
            on_rpc_released=release)

    def _improve_rpc_timeout_sec_by_kind(self) -> dict[str, float]:
        timeout = self._settings.improve.backtest_rpc_timeout_sec
        return {"run_backtest": timeout, "analyze_corr": timeout}

    def _freeze_ledger(self, ctx: ImproveRunContext) -> None:
        ctx.ledger.freeze(
            drain_timeout_sec=self._settings.improve.accept_drain_sec,
            on_timeout=lambda dropped: self._activity.write(
                Category.IMPROVE, "ledger_freeze_timeout",
                f"mission={ctx.mission_id} dropped={dropped}"))

    def _inspect_output(self, output: dict, ctx: ImproveRunContext,
                        conn=None) -> _InspectionVerdict:
        artifact = output.get("artifact", {})
        if isinstance(artifact, dict) and artifact.get("type") == "plugin":
            name = artifact.get("name", "")
            # round2 M1 是正 (2026-08-29、verified-round2.md M1): `.match()`
            # + `$` は末尾改行を受理する穴がある (`'foo\n'` が match する —
            # probe 実測)。`fullmatch` に揃える。schema validate より前に置く
            # のは、agent 由来の巨大 name を activity へ 240 文字も echo
            # させないため (120 文字 cap、test_noncanonical_artifact_name_*)。
            if not _PLUGIN_NAME_RE.fullmatch(name):
                display_name = name[:120] + ("…" if len(name) > 120 else "")
                return _InspectionVerdict(
                    ok=False, reason=f"artifact.name {display_name!r} is not in "
                                    "canonical form")
        # 親側の二重防御: worker (local_runner/cli_runner) も validate するが、
        # resume 回収経路や将来の runner 追加で抜けても kind 欠落等を
        # output_invalid で止める。
        try:
            jsonschema.validate(output, IMPROVE_OUTPUT_SCHEMA)
        except jsonschema.ValidationError as exc:
            path = ".".join(str(part) for part in exc.absolute_path)
            location = f" at {path}" if path else ""
            message = exc.message[:240]
            return _InspectionVerdict(
                ok=False,
                reason=f"output schema invalid{location}: {message}")
        atype = artifact.get("type")
        selected_id = output.get("selected", {}).get("backlog_id")
        if selected_id is not None:
            if conn is not None:
                row = conn.execute(
                    "SELECT id FROM improvement_backlog WHERE id=?",
                    (selected_id,)).fetchone()
                if row is None:
                    return _InspectionVerdict(
                        ok=False, reason=f"selected.backlog_id {selected_id} "
                                        "does not exist")
            if (ctx.allowed_backlog_ids is not None
                    and selected_id not in ctx.allowed_backlog_ids):
                # A36 裁定: out_of_partition の選択は拒否せず、activity
                # ログ 1 行だけ残して続行する (CAS が正)。field としては
                # 撤去済み — この activity 行が唯一の観測点。
                self._activity.write(
                    Category.IMPROVE, "out_of_partition",
                    f"mission={ctx.mission_id} backlog_id={selected_id}")

        if atype == "plugin":
            name = artifact.get("name", "")  # 正規形は冒頭で検査済み
            # staging_dir/<name> の dirfd+lstat 検査は 10.6 節 (plugin ゲート)
            # で実装する — ここでは name 正規形のみ (手順1の範囲)。
            return _InspectionVerdict(ok=True)

        if atype == "report" and artifact.get("proposal_kind") == "risk_gate":
            return _InspectionVerdict(ok=True, risk_gate_unsupported=True)

        return _InspectionVerdict(ok=True)

    def _upsert_backlog_idea(self, conn, idea: str, source: str, kind: str,
                             now: datetime, *,
                             mission_id: int | None = None) -> tuple[int, str]:
        """Insert, reuse, or promote one normalized backlog idea.

        [unprofitable-note-hygiene] 設計書 v2.0 §3: v1.1 以前の挙動に
        戻す (昇格は無条件 `promoted_from_note`。`last_result` の CAS 相当
        分岐は撤回 — 系譜は専用列 `origin_mission_id`/`origin_outcome` が
        持つため、この関数はもう `last_result` を読む必要がない)。
        `mission_id` は新規 INSERT (`"inserted"`) のときだけ
        `origin_mission_id` へ書く — 既存行の再利用 (`existing`/
        `promoted`) は最初の起票者のまま変えない (§2-1)。"""
        idea_norm = idea.strip().lower()
        row = conn.execute(
            "SELECT id,status FROM improvement_backlog "
            "WHERE idea_norm=? ORDER BY id LIMIT 1", (idea_norm,)).fetchone()
        if row is not None:
            if row["status"] == "note" and kind == "task":
                conn.execute(
                    "UPDATE improvement_backlog SET status='open', "
                    "last_result='promoted_from_note', updated_at=? WHERE id=?",
                    (now.isoformat(), row["id"]))
                if self._activity is not None:
                    self._activity.write(Category.IMPROVE, "backlog_promoted",
                                         f"#{row['id']} from note", str(row["id"]))
                return row["id"], "promoted"
            return row["id"], "existing"
        status = "note" if kind == "fact" else "open"
        cur = conn.execute(
            "INSERT INTO improvement_backlog "
            "(idea,source,status,created_at,updated_at,idea_norm,"
            "origin_mission_id) VALUES (?,?,?,?,?,?,?)",
            (idea, source, status, now.isoformat(), now.isoformat(), idea_norm,
             mission_id))
        return cur.lastrowid, "inserted"

    # precheck 2026-08-22 wave2: T10-B5 T10-M10 T10-M11
    def _select_and_bind(self, conn, output: dict, ctx: ImproveRunContext,
                         *, now: datetime) -> _SelectionOutcome:
        conn.execute("BEGIN IMMEDIATE")
        try:
            limit = self._settings.improve.max_new_backlog_per_mission
            inserted = 0  # precheck 2026-08-22 wave2: T10-M10 — 上限は挿入件数
                          # で数える (enumerate インデックスは重複 skip の分だけ
                          # ずれる)
            dropped = 0

            def _norm(idea: str) -> str:
                # round2 #8 是正 (2026-08-29、verified-round2.md #8): 正規化
                # は Python 側のこの 1 箇所に閉じる。SQL 側は INSERT 時に
                # ここで作った正規形を `idea_norm` 列へ書き、以降の重複検出
                # は `idea_norm=?` の等値比較のみで行う (SQL の
                # lower(trim(idea)) は SQLite 既定で ASCII 空白の trim・
                # ASCII のみの lower のため、`'improve X\n'`/`'IMPROVE Ä'`
                # のような idea で Python 側正規形と食い違い、重複行が
                # 生まれていた — probe 実測)。
                return idea.strip().lower()

            for d in output.get("discoveries", []):
                idea_norm = _norm(d["idea"])
                dup = conn.execute(
                    "SELECT id,status FROM improvement_backlog WHERE "
                    "idea_norm=?", (idea_norm,)).fetchone()
                if dup is not None and not (
                        dup["status"] == "note" and d.get("kind", "task") == "task"):
                    continue
                if inserted >= limit:
                    if dup is None:
                        dropped += 1
                        continue
                _row_id, action = self._upsert_backlog_idea(
                    conn, d["idea"], d.get("source", "agent"),
                    d.get("kind", "task"), now, mission_id=ctx.mission_id)
                inserted += action == "inserted"
            if dropped:
                self._activity.write(
                    Category.IMPROVE, "backlog_limit_exceeded",
                    f"mission={ctx.mission_id} dropped={dropped}")

            selected = output.get("selected", {})
            backlog_id = selected.get("backlog_id")
            if backlog_id is None:
                selected_idea = selected.get("idea", "")
                if not selected_idea.strip():
                    conn.commit()
                    return _SelectionOutcome(won=False, backlog_id=None)
                # precheck 2026-08-22 wave2: T10-B5 — discoveries にも既存
                # backlog にも無い「新規 idea を選択」は INSERT してから選ぶ
                # (test_new_idea_selected_creates_and_binds_in_same_tx)。空文字
                # は上で選択なし扱い (fail closed)。2 周目 CR4: 同文の行が
                # 既にあれば INSERT せずその行を使い、note なら task として
                # open に昇格する (`_upsert_backlog_idea`)。`selected` の新規
                # idea は選んだ本人が task と判断したもの → kind=task 固定。
                backlog_id, _selected_action = self._upsert_backlog_idea(
                    conn, selected_idea, selected.get("source", "agent"),
                    "task", now, mission_id=ctx.mission_id)

            won = backlog_store.select_for_mission(
                conn, backlog_id, now=now, commit=False)
            if won:
                improve_runs_store.bind_backlog(
                    conn, ctx.run_id, backlog_id, commit=False)
            conn.commit()
            return _SelectionOutcome(
                won=won, backlog_id=backlog_id if won else None)
        except BaseException:
            conn.rollback()
            raise

    def _inventory_for_gate(self, conn):
        """[indicator-consumption-wiring] T4b Step 4-6c (codex plan r2 束3
        Critical): `ctx.inventory` が (本 task の時点では常に) `None` の
        ときの暫定フォールバック — `approved_plugins` を都度呼ぶだけ。
        `_run_plugin_gate` の呼び出し元 (`commit()`) と
        `_check_duplicate_metrics_for_approval` の呼び出し元
        (`_finalize_success`) の両方が共有する。T5a Step 5-1 で
        `ctx.inventory` が実配線された後は使われなくなる。"""
        return approved_plugins(conn, self._root / "plugins",
                                settings=self._settings)

    def _run_plugin_gate(self, plugin_dir: Path, *, name: str,
                         source_snapshot_dir: Path,
                         inventory=None) -> _PluginGateVerdict:
        # precheck 2026-08-22 wave2: T10-B14 T10-M5 T10-M6 T10-M7
        try:
            check_candidate_snapshot(plugin_dir)
        except CandidateSnapshotError as exc:
            return _PluginGateVerdict(passed=False, reason=str(exc))

        content_hash_before, artifact_hash_before = hashes_of(plugin_dir)

        try:
            check_source(plugin_dir / "plugin.py")
            check_source(plugin_dir / "test_plugin.py",
                        extra_allowed=frozenset({"pytest", "plugin"}))
        except SandboxError as exc:
            return _PluginGateVerdict(passed=False, reason=str(exc))

        meta, reason = plugin_loader.discover_one_with_reason(plugin_dir, name)
        if meta is None:
            return _PluginGateVerdict(
                passed=False, reason=f"loader_rejected: {reason}")

        try:
            noop_copy = find_noop_copy(
                plugin_dir, source_snapshot_dir=source_snapshot_dir,
                examples_dir=source_snapshot_dir / "_examples", name=name,
                inventory=(inventory if inventory is not None
                          else _empty_inventory_result(self._root / "plugins")))
        except SyntaxError:
            return _PluginGateVerdict(passed=False, reason="plugin.py syntax error")
        if noop_copy is not None:
            return _PluginGateVerdict(
                passed=False, reason=f"noop_copy_of:{noop_copy}")

        test_count = count_self_test_functions(plugin_dir / "test_plugin.py")
        minimum = self._settings.improve.gate.min_test_functions
        if test_count == 0:
            return _PluginGateVerdict(passed=False, reason="self_test_missing")

        try:
            result = run_gate_pytest(plugin_dir, settings=self._settings)
        except RuntimeError as exc:
            # M7: Landlock 不可 (run_gate_pytest の fail-closed RuntimeError,
            # 設計書 §4.2-3d) は commit() 全体を例外で抜けさせず gate 不合格に倒す
            return _PluginGateVerdict(passed=False, reason=str(exc))
        if not result.passed:
            return _PluginGateVerdict(
                passed=False, reason=f"pytest failed: {result.stdout_tail}")

        # 1 周目 I1: AST の事前カウントは名前規則の近似でしかない (pytest が
        # 収集しない class 内でも数える等の水増しが可能)。真の下限は pytest の
        # summary から取る。pytest -q の summary は `3 passed in 0.1s` のほか
        # `3 passed, 2 skipped, 1 warning in 0.1s` の形も取る (カンマ続き) —
        # 末尾を `\b` にしないと後者が 0 扱いで誤 fail closed する。
        # 解析不能 (summary 無し) は 0 = fail closed。
        # pytest summary is trusted only as the last summary-shaped line. Tests
        # must not emit forged summary-shaped output after pytest's real summary.
        summary = None
        for line in reversed(result.stdout_tail.splitlines()):
            cleaned = line.strip().strip("=").strip()
            if re.fullmatch(r"(?:\d+ [a-z]+(?:, )?)+ in [\d.]+s", cleaned):
                summary = cleaned
                break
        match = re.search(r"(?:^|, )(\d+) passed\b", summary or "")
        collected_count = int(match.group(1)) if match is not None else 0
        if collected_count < minimum:
            return _PluginGateVerdict(
                passed=False,
                reason=(f"self_test_too_thin_collected:{collected_count}"
                        f"<{minimum}"))

        content_hash_after, artifact_hash_after = hashes_of(plugin_dir)
        if (content_hash_after != content_hash_before
                or artifact_hash_after != artifact_hash_before):
            return _PluginGateVerdict(
                passed=False, reason="hash changed after pytest execution "
                                     "(candidate was mutated by its own test)")

        return _PluginGateVerdict(
            passed=True, content_hash=content_hash_before,
            artifact_hash=artifact_hash_before)

    def _run_strategy_gate(self, conn, *, name, pairs, timeframe, content_hash,
                           now, meta, resolved, kind="strategy",
                           record_fn=None):
        # [profitability-floor] T1 Step 1-3 (2026-09-12、設計書 §3 T1-b):
        # 改善ループは常に `floor_mode="enforce"` (in_sample 段の不合格で
        # holdout を回さず即終端 — evaluator の既定と同じ値だが、この
        # 呼び出し元が「常に enforce」であることを明示するために渡す)。
        #
        # [indicator-consumption-wiring] T3 Step 3-1 (5 番目): `resolved`
        # は呼び出し元が composition root で解決したものをそのまま中継する
        # (再解決しない)。
        return evaluate_strategy_adoption_gate(
            conn, name=name, pairs=pairs, timeframe=timeframe,
            content_hash=content_hash, now=now, settings=self._settings,
            meta=meta, kind=kind, record_fn=record_fn, floor_mode="enforce",
            resolved=resolved)

    def _build_approval_payload(self, conn, *, name, kind, content_hash,
                                artifact_hash, ctx_ledger, mission_id,
                                backlog_id, candidate_origin, candidate_path,
                                gate_metrics, output, now,
                                resolved: "ResolvedIndicatorSet | None" = None,
                                ) -> dict:
        entries = accepted_entries(ctx_ledger.entries())
        analysis_entries = [e for e in entries if e["kind"] == "analyze_corr"]
        backtest_entries = [e for e in entries if e["kind"] == "run_backtest"]
        trial_count = sum(e["trial_count"] for e in entries)
        return {
            "name": name, "kind": kind,
            "candidate_origin": candidate_origin,
            "candidate_path": candidate_path,
            "content_hash": content_hash, "artifact_hash": artifact_hash,
            "eval_source": (self._settings.backtest.eval_source
                            if kind == "strategy" else None),
            "base_interval": (self._settings.backtest.dataset().base_interval
                              if kind == "strategy" else None),
            "eval_timeframe": (_strategy_gate_eval_timeframe(
                                   getattr(gate_metrics.get("meta"), "timeframe", None))
                               if kind == "strategy" else None),
            "live_source": self._settings.plugin.producer_source,
            "mission_id": mission_id, "backlog_id": backlog_id,
            "in_sample": gate_metrics.get("in_sample"),
            "holdout": gate_metrics.get("holdout"),
            "baseline": gate_metrics.get("baseline"),
            "analysis_run_ids": [],  # Tx-2 で実 id を解決してから埋める
                                     # (10.10 節 `_finalize_success` — 直前
                                     # 修正の申し送り③)
            "trial_count": trial_count,
            "analysis_call_count": len(analysis_entries),
            "backtest_call_count": len(backtest_entries),
            "selection_rationale": output.get("selection_rationale", ""),
            "summary": output.get("artifact", {}).get("summary", ""),
            "audit_note": "RPC timeout した呼出しは数えていない",
            # [profitability-floor] T1 Step 1-8 (2026-09-13、codex I3):
            # 適用した閾値 snapshot (approval 行を作る 3 箇所すべてに載せる
            # — switch.submit_candidate / switch.bless_candidate / ここ)。
            # CR4 (2026-09-13): `ImproveGateSettings.snapshot()` に一本化。
            "profitability_floor": self._settings.improve.gate.snapshot(),
            # [indicator-consumption-wiring] §2.7: switch.py の
            # submit / bless と**同形**。呼び出し元が解決済み集合を
            # 渡す (ここで再解決しない — P3')。
            "indicator_deps": (resolved.pin_object()
                               if resolved is not None else {}),
        }

    def _check_duplicate_metrics(self, conn, *, content_hash, pair, variant,
                                 source, base_interval, metrics) -> str | None:
        """approval-quality 設計書 §A: `_build_approval_payload` の直後
        (gate 通過後・approval 提出前) に挟む質検査。候補の
        `(trades, pf, avg_r)` が既存の承認済み candidate 行と一致すれば、
        一致した行の `content_hash` を返す (呼び出し元はこれを
        `duplicate_metrics_of:<hash>` として観測降格の note に使う)。
        一致が無ければ `None`。

        `content_hash` (候補自身の hash) はこの検査の比較には使わない —
        `_check_duplicate_metrics` の呼び出し規約を
        `_build_approval_payload` と揃え、将来 activity ログに候補自身の
        hash も残すときに引数を増やさず済むようにするための保持。"""
        from agentic_fx.store import backtest_runs as backtest_runs_store
        return backtest_runs_store.find_matching_approved_metrics(
            conn, pair=pair, variant=variant, source=source,
            base_interval=base_interval, trades=metrics.get("trades"),
            pf=metrics.get("pf"), avg_r=metrics.get("avg_r"))

    def _final_report_path(self, reports_dir: Path, *, mission_id: int,
                           now: datetime) -> Path:
        """F-3 是正 (検収 task12、設計書 §4.2 改訂): 最終レポート名は
        `improve-YYYY-MM-DD-<mission_id>.md`。日付を省くと mission_id が
        DB 再構築後などに再利用された場合、最終名が衝突しうる
        (acceptance-task12.md F-3)。`now` は各終端メソッドが Mission の
        決定論的時刻としてすでに受け取っている引数を使う — 壁時計を
        直接読まない現行流儀 (`Clock` 注入) に従う。一時 `.tmp/*.part`
        側は `_write_report_part` が `mission_id` のみで命名する
        (同時に 1 mission につき 1 tmp ファイルしか存在せず、公開後は
        消えるため衝突しない)。"""
        return reports_dir / f"improve-{now:%Y-%m-%d}-{mission_id}.md"

    def _write_report_part(self, reports_dir: Path, *, mission_id: int,
                           body_md: str) -> Path:
        """report outbox — 一時ファイルを O_EXCL + fsync で書く。"""
        part_path = reports_dir / ".tmp" / f"improve-{mission_id}.md.part"
        fd = os.open(str(part_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, body_md.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return part_path

    def _fsync_dir(self, dirpath: Path) -> None:
        """report outbox — ディレクトリの fsync。"""
        fd = os.open(str(dirpath), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _rename_no_replace(self, src: Path, dst: Path) -> bool:
        """F-6 是正 (検収 task12、設計書 §4.2 L389 の `RENAME_NOREPLACE`
        要求): `renameat2(2)` (Linux 3.15+, glibc 2.28+ に libc wrapper
        あり) で `RENAME_NOREPLACE` フラグを使い、rename を「既存 dst が
        無いときだけ」カーネルに原子的に行わせる。旧実装は
        `if final_path.exists(): raise ... ; os.rename(...)` という
        check-then-act (TOCTOU) — exists() チェックと os.rename の間に
        別プロセスが同名ファイルを作ると `os.rename` は POSIX 上それを
        黙って上書きする (exists+rename 版は「renameat2 の近似」に過ぎず、
        本物の排他ではなかった)。

        戻り値: `renameat2` で原子的にリネームできたら True。**非対応
        環境 (非 Linux / 古いカーネルの ENOSYS・一部 FS の EINVAL) では
        False を返し**、呼び出し元 (`_publish_report`) が旧来の
        exists+rename (TOCTOU 近似) にフォールバックする。**dst が既に
        存在する衝突 (`EEXIST`) は非対応の合図ではない** — `FileExistsError`
        をそのまま送出し、呼び出し元の `_fail_report` 補償 tx へ倒す
        (fallback は行わない — フォールバックしてしまうと `RENAME_NOREPLACE`
        が検出した衝突を exists+rename が再チェックする間に別プロセスが
        入れ替わる余地を新たに作ってしまう)。"""
        if not sys.platform.startswith("linux"):
            return False
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            renameat2 = libc.renameat2
        except AttributeError:
            return False
        renameat2.restype = ctypes.c_long
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                              ctypes.c_char_p, ctypes.c_uint]
        ctypes.set_errno(0)
        rc = renameat2(_AT_FDCWD, os.fsencode(str(src)), _AT_FDCWD,
                       os.fsencode(str(dst)), _RENAME_NOREPLACE)
        if rc == 0:
            return True
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(errno.EEXIST, os.strerror(err), str(dst))
        if err in (errno.ENOSYS, errno.EINVAL):
            return False
        raise OSError(err, os.strerror(err), str(dst))

    def _publish_report(self, conn, *, run_id: int, part_path: Path,
                        final_path: Path, now: datetime) -> bool:
        """report outbox — rename 後に published へ遷移、fsync。

        F-6 是正 (検収 task12、設計書 §4.2 L389): 最終名への rename は
        `renameat2(RENAME_NOREPLACE)` で行う (`_rename_no_replace` 参照)。
        非対応環境 (非 Linux・古いカーネル) のみ、旧来の
        exists+rename (TOCTOU 近似) にフォールバックする。

        戻り値: published に遷移したら True、rename 失敗で `_fail_report`
        に落ちたら False (2 周目 CR3: 呼び出し元は True のときだけ
        `report_published` activity を書く — 失敗時に「published」の痕跡を
        残さない)。"""
        try:
            if not self._rename_no_replace(part_path, final_path):
                # renameat2 非対応環境向けフォールバック (TOCTOU 近似 —
                # `_rename_no_replace` 自身が返す False はこの経路でしか
                # 起きない。EEXIST は `_rename_no_replace` が直接
                # FileExistsError を送出する — ここでは再チェックしない)。
                if final_path.exists():
                    raise FileExistsError(final_path)
                os.rename(part_path, final_path)
        except FileExistsError:
            self._fail_report(conn, run_id=run_id, now=now, reason="rename_conflict")
            return False
        except OSError as exc:
            self._fail_report(conn, run_id=run_id, now=now,
                              reason=f"rename_failed:{exc}")
            return False
        self._fsync_dir(final_path.parent)
        self._fsync_dir(part_path.parent)
        conn.execute(
            "UPDATE improvement_runs SET report_state='published' "
            "WHERE id=?", (run_id,))
        conn.commit()
        return True

    def _fail_report(self, conn, *, run_id: int, now: datetime,
                     reason: str) -> None:
        """report outbox — 報告失敗の補償 tx。"""
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE improvement_runs SET report_state='failed', "
                "result=NULL, report_path=NULL WHERE id=?", (run_id,))
            run = conn.execute(
                "SELECT backlog_id FROM improvement_runs WHERE id=?",
                (run_id,)).fetchone()
            if run is not None and run["backlog_id"] is not None:
                # D-15 是正: 元の CAS guard は `status='done'` のみ許可して
                # おり、「report artifact 成功 → done」の分岐しか想定して
                # いなかった。`_finalize_gate_failed` (D-15 是正で report
                # 生成を追加) は Tx-2 の時点で既に backlog を `observation`
                # へ遷移させてから report を publish するため、公開失敗時の
                # 補償も `observation` からの上書きを許す必要がある
                # (`tests/loops/test_improve_e2e.py::
                # test_report_outbox_state_transitions_published_then_rename_failure`
                # の 2 本目、gate_failed 経路での rename 衝突)。backlog_id
                # は Tx-1 CAS で単一 mission だけが占有するため、
                # `done`/`observation` 以外に居るのは無関係な mission
                # (`selected`/`open` 等) のときだけであり、それを誤って
                # 上書きする心配はない。
                conn.execute(
                    "UPDATE improvement_backlog SET status='observation', "
                    "last_result=?, updated_at=? WHERE id=? "
                    "AND status IN ('done', 'observation')",
                    (f"report_failed:{reason}", now.isoformat(),
                     run["backlog_id"]))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def reconcile_report_outbox(self, conn, *, reports_dir: Path,
                                now: datetime) -> None:
        """report outbox — 起動時 reconcile。"""
        rows = conn.execute(
            "SELECT id, mission_id, report_state, report_path "
            "FROM improvement_runs "
            "WHERE report_state IN ('prepared','published')").fetchall()
        for row in rows:
            report_path = Path(row["report_path"]) if row["report_path"] else None
            if row["report_state"] == "prepared":
                part_path = (reports_dir / ".tmp"
                            / f"improve-{row['mission_id']}.md.part")
                if part_path.exists() and report_path is not None:
                    self._publish_report(conn, run_id=row["id"],
                                        part_path=part_path,
                                        final_path=report_path, now=now)
                elif report_path is not None and report_path.exists():
                    conn.execute(
                        "UPDATE improvement_runs SET report_state='published' "
                        "WHERE id=?", (row["id"],))
                    conn.commit()
                else:
                    self._fail_report(conn, run_id=row["id"], now=now,
                                      reason="missing_temp_and_final")
            elif row["report_state"] == "published":
                if report_path is None or not report_path.exists():
                    self._fail_report(conn, run_id=row["id"], now=now,
                                      reason="missing:published_final_absent")

    # 10.10 節キーホワイトリスト
    _BACKTEST_ROW_KEYS = (
        "scope", "plugin_ref", "content_hash", "kind", "pair", "timeframe",
        "source", "base_interval", "params", "period", "metrics", "settings_hash", "core_commit",
        "initial_balance", "now")
    _ANALYSIS_ROW_KEYS = ("params", "trial_count", "source")

    def _persist_ledger_rows(self, conn, *, ledger_entries, now,
                             mission_id: int | None = None,
                             mission_outcome: str) -> list[int]:
        """10.10 Step 3: 台帳から analysis_runs/backtest_runs へ永続化。"""
        from agentic_fx.store import analysis_runs as analysis_runs_store
        from agentic_fx.store import backtest_runs as backtest_runs_store

        analysis_run_ids: list[int] = []
        for entry in ledger_entries:
            summary = entry["result_summary"]
            if "error" in summary:
                self._activity.write(
                    Category.IMPROVE, "ledger_entry_skipped_error",
                    f"mission={mission_id} kind={entry['kind']} "
                    f"error={summary['error']!r}")
        for entry in accepted_entries(ledger_entries):
            summary = entry["result_summary"]
            if entry["kind"] == "run_backtest":
                row_kwargs = {k: summary[k] for k in self._BACKTEST_ROW_KEYS}
                backtest_runs_store.save_harness_run(
                    conn, commit=False, variant="candidate",
                    mission_id=mission_id, mission_outcome=mission_outcome,
                    **row_kwargs)
            elif entry["kind"] == "analyze_corr":
                row_kwargs = {k: summary[k] for k in self._ANALYSIS_ROW_KEYS}
                run_id = analysis_runs_store.save(
                    conn, commit=False, now=now, mission_id=mission_id,
                    **row_kwargs)
                analysis_run_ids.append(run_id)
        return analysis_run_ids

    def _archive_failed(self, *, mission_id, artifact_hash, reason) -> None:
        if self._activity is not None:
            self._activity.write(
                Category.IMPROVE, "archive_failed",
                f"mission={mission_id} hash={artifact_hash} reason={reason}")

    def _archive_rows_for_note(self, conn, *, mission_id: int) -> list[dict]:
        """T4 変更点2: note 用の best を、`_persist_ledger_in_tx` の後・
        `finish_improve_mission` の前、**同じ tx 内**で読む。例外は活動記録
        のみで終端を止めない (ブリーフ「テスト」節)。"""
        from agentic_fx.store import candidate_archives as candidate_archives_store
        try:
            return candidate_archives_store.list_by_mission(conn, mission_id)
        except Exception as exc:
            if self._activity is not None:
                self._activity.write(
                    Category.IMPROVE, "best_candidate_lookup_failed",
                    f"mission={mission_id} reason="
                    f"{type(exc).__name__}:{str(exc)[:300]}")
            return []

    @staticmethod
    def _best_note_suffix(best: dict | None) -> str:
        """T4 変更点2: note の `last_result` 末尾に付ける
        ` best=<name>@<hash8> pf=<pf> dd=<dd> archive=<archive_path>`。
        `best` が `None` なら空文字 (何も付けない)。"""
        label = _best_label(best)
        if label is None:
            return ""
        archive = best.get("archive_path") or "-"
        return f" best={label} archive={archive}"

    def _append_archive_index(self, ctx, *, status: str, rows: list[dict],
                              now: datetime) -> None:
        """T4 変更点3: `plugins/_archive/INDEX.md` に 1 mission 1 行を
        追記する。呼び出し元が `rows` (0 件でないことを確認済み) を渡す。"""
        index_path = self._root / "plugins" / "_archive" / "INDEX.md"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        best = best_candidate(rows)
        label = _best_label(best)
        best_cell = f"best={label}" if label is not None else "-"
        archive_cell = "-"
        if best is not None:
            archive_cell = best.get("archive_path") or "-"
        line = (f"| {now.isoformat()} | mission {ctx.mission_id} | {status} "
                f"| candidates={len(rows)} | {best_cell} | {archive_cell} |\n")
        marker = f"| mission {ctx.mission_id} |"
        # codex 1 周目 (T3+T4) Important 1 (2026-09-11): 「1 mission 1 行」を
        # プロセス内 lock で守る — 外部補償の二重呼び出しは同 mission を
        # 2 回通り、並行 slot の初回終端は双方が「ファイル無し」を観測して
        # ヘッダを重複させていた。lock 内で既出 mission を確認して skip。
        with self._archive_index_lock:
            is_new = not index_path.exists()
            if not is_new:
                with open(index_path, "r") as fh:
                    if any(marker in existing for existing in fh):
                        return
            with open(index_path, "a") as fh:
                if is_new:
                    # approval-quality 設計書 §B (2026-09-12、
                    # [archive-index-naming]): この見出しは「終端ログ」で
                    # あって承認可否の目録ではない旨を明記する — approval
                    # 終端 (T-B で追加) も 1 行だけ載るため、この一覧に
                    # 無いことは「未承認」を意味しない。GC はこのファイルを
                    # 更新しない。承認済み候補の正 (authoritative source)
                    # は `candidate_archives` 表と `approval_requests` 表。
                    fh.write(
                        "終端ログ (GC 非対象)。承認可否の目録ではない。"
                        "承認済み候補の正は `candidate_archives` 表と "
                        "`approval_requests` 表である。\n\n")
                    fh.write(
                        "| date | mission | status | candidates | best | "
                        "archive |\n")
                    fh.write("| --- | --- | --- | --- | --- | --- |\n")
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())

    def _settle_ledger_after_commit(self, conn, *, ctx, ledger_ids,
                                    outcome: str, now: datetime) -> None:
        """外側 commit 成功後の共通処理: ledger の終端状態 (保存結果で決める)
        と INDEX.md (`status` = `mission_outcome` と同じ語)。10 箇所の複製を
        1 本に (/code-review 2 周目 CR9、2026-09-11)。"""
        if ledger_ids is None:
            ctx.ledger.mark_persist_failed()
        else:
            ctx.ledger.mark_persisted()
        self._write_archive_index_safe(conn, ctx=ctx, status=outcome, now=now)

    def _write_archive_index_safe(self, conn, *, ctx, status: str,
                                  now: datetime) -> None:
        """T4 変更点3: 終端 tx の**外側 commit 成功後**に呼ぶ。行 0 件の
        mission は書かない。`OSError` (readonly dir 等) を含むあらゆる
        例外は `archive_index_failed` activity にして終端を壊さない
        (ブリーフ「変更点」3・「テスト」節)。"""
        from agentic_fx.store import candidate_archives as candidate_archives_store
        try:
            rows = candidate_archives_store.list_by_mission(conn, ctx.mission_id)
            if not rows:
                return
            self._append_archive_index(ctx, status=status, rows=rows, now=now)
        except Exception as exc:
            if self._activity is not None:
                self._activity.write(
                    Category.IMPROVE, "archive_index_failed",
                    f"mission={ctx.mission_id} reason="
                    f"{type(exc).__name__}:{str(exc)[:300]}")

    @staticmethod
    def _remove_archive_tmp(path: Path) -> None:
        import shutil
        from agentic_fx.plugin.version_store import chmod_tree_writable
        chmod_tree_writable(path)
        shutil.rmtree(path, ignore_errors=True)

    def _publish_archives(self, conn, *, ctx, now,
                          mission_outcome: str) -> None:
        from agentic_fx.plugin.version_store import artifact_hash_bytes, _fsync_dir
        from agentic_fx.store import candidate_archives

        for entry in accepted_entries(ctx.ledger.entries()):
            if entry["kind"] != "run_backtest":
                continue
            summary = entry["result_summary"]
            archive_tmp = summary.get("archive_tmp")
            artifact_hash = summary.get("artifact_hash")
            if not archive_tmp or not artifact_hash:
                continue
            tmp_path = Path(archive_tmp)
            final_path = self._root / "plugins" / "_archive" / str(
                ctx.mission_id) / artifact_hash
            published = False
            if tmp_path.exists():
                final_path.parent.mkdir(parents=True, exist_ok=True)
                # sonnet 3 周目 Minor (2026-09-11): os.rename は宛先が
                # 既存の空ディレクトリだと例外なしで成功し (先着を消費して
                # 置換してしまう)、EEXIST/ENOTEMPTY 経路の先着扱い
                # (hash 再検証) をバイパスする。rename 直前に final_path を
                # mkdir して既存判定を正規化する: FileExistsError なら
                # 既存扱い (tmp 削除 → 下の hash 再検証へ)。mkdir が成功
                # したら自分が作った空 dir を rmdir してから rename する
                # (空 dir を残したまま rename すると同じ問題が再現する)。
                try:
                    final_path.mkdir()
                except FileExistsError:
                    self._remove_archive_tmp(tmp_path)
                except OSError as exc:
                    self._archive_failed(
                        mission_id=ctx.mission_id,
                        artifact_hash=artifact_hash,
                        reason=f"{type(exc).__name__}:{str(exc)[:300]}")
                    continue
                else:
                    final_path.rmdir()
                    try:
                        os.rename(tmp_path, final_path)
                        _fsync_dir(final_path.parent)
                        published = True
                    except OSError as exc:
                        if isinstance(exc, FileExistsError) or exc.errno in (
                                errno.EEXIST, errno.ENOTEMPTY):
                            self._remove_archive_tmp(tmp_path)
                        else:
                            self._archive_failed(
                                mission_id=ctx.mission_id,
                                artifact_hash=artifact_hash,
                                reason=f"{type(exc).__name__}:{str(exc)[:300]}")
                            continue
            if not published:
                try:
                    if final_path.is_symlink() or not final_path.is_dir():
                        raise ValueError("final_not_regular_directory")
                    actual = artifact_hash_bytes(
                        (final_path / "plugin.py").read_bytes(),
                        (final_path / "config.yaml").read_bytes(),
                        (final_path / "test_plugin.py").read_bytes())
                    if actual != artifact_hash:
                        raise ValueError(f"hash_mismatch:{actual}")
                    published = True
                except (OSError, ValueError) as exc:
                    self._archive_failed(
                        mission_id=ctx.mission_id, artifact_hash=artifact_hash,
                        reason=f"{type(exc).__name__}:{str(exc)[:300]}")
                    continue
            relative_path = final_path.relative_to(self._root).as_posix()
            candidate_archives.insert(
                conn, mission_id=ctx.mission_id,
                name=entry["params"]["name"],
                content_hash=summary["content_hash"],
                artifact_hash=artifact_hash, archive_path=relative_path,
                pair=summary["pair"], metrics=summary["metrics"], now=now,
                commit=False)

    def _persist_ledger_in_tx(self, conn, *, ctx, now,
                              mission_outcome: str) -> list[int] | None:
        conn.execute("SAVEPOINT ledger")
        try:
            # 全 entry を渡す (error entry の `ledger_entry_skipped_error`
            # activity は _persist_ledger_rows 側で維持、行は accepted のみ)。
            ids = self._persist_ledger_rows(
                conn, ledger_entries=ctx.ledger.entries(),
                now=now, mission_id=ctx.mission_id,
                mission_outcome=mission_outcome)
            self._publish_archives(
                conn, ctx=ctx, now=now, mission_outcome=mission_outcome)
            conn.execute("RELEASE ledger")
            return ids
        except Exception as exc:
            conn.execute("ROLLBACK TO ledger")
            conn.execute("RELEASE ledger")
            self._activity.write(
                Category.IMPROVE, "ledger_persist_failed",
                f"mission={ctx.mission_id} reason="
                f"{type(exc).__name__}:{str(exc)[:300]}")
            return None

    def _persist_gate_rows(self, conn, *, gate_rows, now,
                           mission_id: int | None = None,
                           mission_outcome: str) -> None:
        """10.10 Step 3: 親ゲート行を永続化。`mission_outcome` は必須 —
        NULL のまま保存すると `latest_in_sample_metrics` の live 絞り
        (`IS NULL OR 'approval'`) を通り、gate 不合格候補の成績が live 表示
        に出る (/code-review 2 周目 CR1、2026-09-11)。"""
        from agentic_fx.store import backtest_runs as backtest_runs_store

        for row in gate_rows:
            backtest_runs_store.save_harness_run(
                conn, commit=False, mission_id=mission_id,
                mission_outcome=mission_outcome, **row)

    def _compensate_tx2_failure(self, conn, *, ctx, mission_id, run_id,
                                backlog_id, slot_key, now) -> None:
        """10.10 Step 3: Tx-2 失敗時の補償 tx。"""
        conn.execute("BEGIN IMMEDIATE")
        ledger_ids = None
        try:
            ledger_ids = self._persist_ledger_in_tx(
                conn, ctx=ctx, now=now, mission_outcome="commit_failed")
            missions_store.finish_improve_mission(
                conn, mission_id=mission_id, run_id=run_id,
                slot_key=slot_key, mission_status="failed", run_result=None,
                now=now,
                backlog_transition=(
                    {"backlog_id": backlog_id, "status": "observation",
                     "last_result": "commit_failed"}
                    if backlog_id is not None else None),
                commit=False)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if ledger_ids is None:
            ctx.ledger.mark_persist_failed()
        else:
            ctx.ledger.mark_persisted()
        self._activity.write(
            Category.IMPROVE, "improve_commit_failed",
            f"mission_id={mission_id} run_id={run_id}")
        self._write_archive_index_safe(
            conn, ctx=ctx, status="commit_failed", now=now)

    def _check_duplicate_metrics_for_approval(
            self, conn, approval_payload: dict, *,
            inventory: "InventoryBuildResult", staging_dir: Path,
            ) -> _DuplicateDemotion | None:
        """/code-review 2 周目 CR1+CR7 是正 (2026-09-12): `_finalize_success`
        の `BEGIN IMMEDIATE` 内側から呼ぶ質検査 (approval-quality 設計書
        §A)。CR7: `eval_source`/`base_interval`/`content_hash`/`in_sample`
        は `approval_payload` (`_build_approval_payload` が数手前に書いた
        もの) からそのまま読む — `commit()` 側で `self._settings` から
        再導出した throwaway `HistoryDataset` を別途作らない (2 つの導出が
        将来ズレて質検査の絞り込みキーが提出される行と食い違うことを
        構造的に防ぐ)。indicator (`kind != 'strategy'`) は成績
        (trades/pf/avg_r) を持たないため対象外。複数 pair の候補は pair
        ごとに検査し、いずれか 1 pair でも既承認候補と一致すれば候補全体を
        observation へ倒す (実装時点の未決事項 — 設計書は単一 pair を
        前提にした記述のみで多 pair の合成方針を明示していない)。

        [indicator-consumption-wiring] §2.7 (opus I5): 再ロックのみの
        再提出は母集団から除外する — pin はハーネスの派生値であり、
        「同じ戦略を新しい indicator 版に貼り直しただけ」の再提出が
        成績一致で降格されると正式な再ロック経路が塞がる。"""
        if approval_payload.get("kind") != "strategy":
            return None
        deployed_dir = self._deployed_dir_for(approval_payload.get("name"),
                                              inventory=inventory)
        candidate_dir = self._candidate_dir_for(approval_payload,
                                                staging_dir=staging_dir)
        if (deployed_dir is not None and candidate_dir is not None
                and is_relock_transition(candidate_dir, deployed_dir,
                                        inventory.inventory)):
            return None
        in_sample = approval_payload.get("in_sample") or {}
        eval_source = approval_payload.get("eval_source")
        eval_base_interval = approval_payload.get("base_interval")
        for pair, pair_metrics in in_sample.items():
            duplicate_of = self._check_duplicate_metrics(
                conn, content_hash=approval_payload.get("content_hash"),
                pair=pair, variant="candidate", source=eval_source,
                base_interval=eval_base_interval, metrics=pair_metrics)
            if duplicate_of is not None:
                return _DuplicateDemotion(content_hash=duplicate_of, pair=pair)
        return None

    def _deployed_dir_for(self, name, *, inventory) -> "Path | None":
        """`inventory.phase1_metas` のうち同名 strategy の `meta.path`。
        見つからなければ `None`。"""
        for meta in inventory.phase1_metas:
            if meta.name == name and meta.kind == "strategy":
                return meta.path
        return None

    def _candidate_dir_for(self, approval_payload, *, staging_dir) -> "Path | None":
        """`staging_dir / approval_payload["name"]`。存在しなければ `None`。"""
        if staging_dir is None:
            return None
        candidate_dir = staging_dir / approval_payload.get("name", "")
        return candidate_dir if candidate_dir.is_dir() else None

    def _finalize_success(self, conn, *, mission_id, run_id, backlog_id,
                          slot_key, approval_payload, now,
                          ledger_entries=(), gate_rows=(), ctx=None,
                          tool_calls: int | None = None,
                          ) -> _DuplicateDemotion | None:
        """10.10 Step 3: Tx-2 本体 (台帳→gate→approval→finish)。

        戻り値: 質検査 (CR1) が既承認候補との一致を検出したときは
        `_DuplicateDemotion` を返す (この場合、内側 tx は既に rollback 済み
        — 呼び出し元 `commit()` が observation 経路へ再ルートする)。通常の
        approval 終端では `None` を返す。"""
        from agentic_fx.store import approvals as approvals_store
        from agentic_fx.store import missions as missions_store

        # persist_ctx は `BEGIN IMMEDIATE` の**前**に束縛する — 中で BEGIN が
        # 例外 (ロック競合) を出すと外側 except の `_compensate_tx2_failure(
        # ctx=persist_ctx)` が UnboundLocalError になり補償が静かに飛ぶ
        # (ローカル T3 1 周目 申し送り、2026-09-10)。
        if ctx is None:
            class _EntriesView:
                def entries(self):
                    return list(ledger_entries)
                def mark_persisted(self):
                    return None
                def mark_persist_failed(self):
                    return None
                def mark_discarded(self):
                    return None
            from types import SimpleNamespace
            persist_ctx = SimpleNamespace(
                mission_id=mission_id, ledger=_EntriesView())
        else:
            persist_ctx = ctx
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # /code-review 2 周目 CR1 是正 (2026-09-12): 質検査は
                # `_persist_ledger_in_tx` (→ `_publish_archives` が
                # `os.rename` でファイルシステムへ副作用を及ぼす) より前、
                # かつ `BEGIN IMMEDIATE` の内側で行う。ファイルシステムの
                # 変更はトランザクショナルでない (rollback で戻せない) ため、
                # 何か書く前でなければ「ヒットしたら安全に降格して戻る」が
                # 成立しない。WAL の `BEGIN IMMEDIATE` は書き込みロックの
                # 直列化なので、並行 slot の 2 本目はここで待たされ、1 本目
                # の commit 後のスナップショットを読む — bare SELECT だった
                # 旧位置 (`commit()` 内、gate 通過直後) の check-then-insert
                # レースを閉じる。
                # [indicator-consumption-wiring] codex plan r2 束3
                # Critical: `ctx.inventory` は本 task の時点では常に
                # 既定値 `None` (実配線は T5a Step 5-1) — 直渡しすると
                # `_deployed_dir_for(name, inventory=None)` が
                # `AttributeError` になる。`None` フォールバックを挟む。
                # `ctx is None` はテスト専用の直接呼び出し経路
                # (`approval_payload["kind"] != "strategy"` のケースしか
                # 使っていないため `staging_dir=None` でも安全)。
                demotion = self._check_duplicate_metrics_for_approval(
                    conn, approval_payload,
                    inventory=(ctx.inventory
                              if ctx is not None and ctx.inventory is not None
                              else self._inventory_for_gate(conn)),
                    staging_dir=(ctx.staging_dir if ctx is not None else None))
                if demotion is not None:
                    conn.rollback()
                    return demotion
                # precheck 2026-08-23 wave3: RW4 — mission_id (= ctx.mission_id、
                # commit() が渡すローカル引数) を台帳/親ゲート両方の永続化へ
                # 転送する。Task 12 の `WHERE mission_id=?` assert が読む列。
                analysis_run_ids = self._persist_ledger_in_tx(
                    conn, ctx=persist_ctx, now=now,
                    mission_outcome="approval")
                if analysis_run_ids is None:
                    raise RuntimeError("approval ledger persistence failed")
                self._persist_gate_rows(
                    conn, gate_rows=gate_rows, now=now, mission_id=mission_id,
                    mission_outcome="approval")
                approval_payload = dict(approval_payload)
                approval_payload["analysis_run_ids"] = analysis_run_ids
                accepted = accepted_entries(ledger_entries)
                approval_payload["analysis_call_count"] = sum(
                    1 for e in accepted if e["kind"] == "analyze_corr")
                approval_payload["trial_count"] = sum(
                    e["trial_count"] for e in accepted)
                approval_id = approvals_store.create(
                    conn, kind="plugin", payload=approval_payload, now=now,
                    commit=False)
                missions_store.finish_improve_mission(
                    conn, mission_id=mission_id, run_id=run_id,
                    slot_key=slot_key, mission_status="completed",
                    run_result="approval", now=now, approval_id=approval_id,
                    backlog_transition={
                        "backlog_id": backlog_id, "status": "selected",
                        "last_result": f"approval_pending:{approval_id}"},
                    commit=False)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            # 成功系 activity は commit 成功後に書く (1 周目 F2、3 モデル
            # 一致): tx 前に書くと rollback しても「成功」が activity に残る。
            # 失敗系が tx 前に書くのは痕跡を残す意図で、成功系とは逆。
            # 2 周目 R2-1: try の外に置き `_finalize_report_or_observation` と
            # 対称にする (write は非送出契約だが、万一の例外で commit 済みの
            # 成功を補償経路が failed に上書きしないよう構造で保証)。
            if self._activity is not None:
                self._activity.write(
                    Category.IMPROVE, "approval_requested",
                    f"mission={mission_id} backlog={backlog_id} "
                    f"plugin={approval_payload['name']} "
                    f"approval={approval_id}{_tool_calls_suffix(tool_calls)}",
                    str(mission_id))
            # /code-review 2 周目 CR8 是正 (2026-09-12): `mark_persisted` +
            # `_write_archive_index_safe` の対を素の呼び出しで並べていたが、
            # `_settle_ledger_after_commit` はこの対を 1 本化するために
            # 導入された helper (docstring 参照) — この呼び出し元だけ独自に
            # インライン化していた。`analysis_run_ids` はこの行に到達する
            # 時点で non-None 確定 (数行上の `if analysis_run_ids is None:
            # raise` を通過済み) なので drop-in で置き換えられる。`ctx` は
            # 直接呼び出し (単体テスト) で `None` になりうるため、常に
            # `mission_id`/ledger を持つ `persist_ctx` を渡す
            # (`persist_ctx.ledger` は `ctx is None` のとき no-op
            # `_EntriesView` — 従来の `if ctx is not None:` ガードと等価)。
            self._settle_ledger_after_commit(
                conn, ctx=persist_ctx, ledger_ids=analysis_run_ids,
                outcome="approval", now=now)
        # C6 裁定 (2026-08-28、束D検収 verified-local-round1.md §7、
        # 現状維持): 内側 tx は `except BaseException:` (rollback して
        # 必ず re-raise) だが、この外側の補償トリガーは意図的に
        # `except Exception:` — `KeyboardInterrupt`/`SystemExit`
        # (shutdown) はここを通らず補償 tx を走らせずに呼び出し元へ
        # 素通りする。shutdown 中に新たな tx (補償) を開始するより、
        # 補償を諦めて即座に終了する方が安全という判断 (起動時
        # reconcile が非終端 mission を拾う)。`_compensate_prepare_
        # failure` の外側にも同じ非対称がある (下記参照)。
        except Exception:
            _log.exception("Tx-2 failed for mission_id=%s — running "
                           "compensation", mission_id)
            try:
                self._compensate_tx2_failure(
                    conn, ctx=persist_ctx, mission_id=mission_id, run_id=run_id,
                    backlog_id=backlog_id, slot_key=slot_key, now=now)
            except Exception:
                _log.exception(
                    "compensation itself failed for mission_id=%s — mission "
                    "stays in a non-terminal state, will be picked up by "
                    "startup reconcile", mission_id)
                self._activity.write(
                    Category.IMPROVE, "tx2_compensation_failed",
                    f"mission_id={mission_id} run_id={run_id}")
                if ctx is not None:
                    ctx.ledger.mark_discarded()

    def commit(self, *, mission, ctx, result, now,
              slot_terminalize: bool = True) -> None:
        """`slot_terminalize` (I2b 是正、プラン10 束D round1、ユーザー裁定
        D①、2026-08-28): pre-ready 失敗の巻き戻し (`_handle_pre_ready_
        failure` の `revert_to_reserved`) は `commit()` が呼ばれる**前**に
        完了している。既定 `True` (従来どおり無条件に slot も終端) の
        まま `finish_improve_mission(slot_key=ctx.slot_key, ...)` を呼ぶと、
        `mark_terminal` が status ガード無しの無条件 UPDATE なので、直前
        に revert した `reserved` slot を即座に `failed` へ潰してしまう
        (プラン 9.5 節 RW3 が指した「commit() 内部の reached_running 分岐」
        は実在しなかった — 訂正はプラン文書側で行う)。呼び出し元
        (`ImproveSupervisor._launch_slot`) は pre-ready 失敗を再試行する
        ときだけ `slot_terminalize=False` を渡し、mission/run のみを
        終端して slot には触れない。"""
        owns_conn = False
        conn = getattr(self, "_conn_for_test", None)
        if conn is None:
            conn = self._db_write_conn_factory()
            owns_conn = True
        commit_raised = False
        try:
            self._freeze_ledger(ctx)                                  # 手順0

            # A4 10 回目 #71 観測 B (2026-09-11): `result.tool_calls`/
            # `.stderr_fatal` は `MissionResult` の新設フィールド
            # (2026-09-11 追加)。`getattr` で読む — 一部テストは duck-typed
            # な独自 Result スタブ (これらの属性を持たない) を渡すため、
            # 直接属性アクセスだと `AttributeError` で既存テストを壊す。
            tool_calls = getattr(result, "tool_calls", None)
            stderr_fatal = getattr(result, "stderr_fatal", None)

            # 終端種別より前 (どの経路へ分岐しても漏れなく書く) に、CLI
            # backend の stderr 既知致命パターン検知を activity へ出す。
            # `stderr_fatal` は CliRunner (claude/codex/opencode) だけが
            # 埋め、LocalRunner の結果では常に None — local backend では
            # 一切書かれない。
            if self._activity is not None and stderr_fatal:
                self._activity.write(
                    Category.IMPROVE, "cli_stderr_fatal",
                    f"mission={ctx.mission_id} "
                    f"backend={self._settings.runner.improve.backend} "
                    f"{stderr_fatal}")

            if result.status != "completed":
                self._finalize_failed_mission(
                    conn, ctx=ctx, result=result, now=now,
                    slot_terminalize=slot_terminalize)
                return

            if is_tool_budget_abort(result.reason):
                # /code-review 2 周目 #2: abort 後の追撃で回収できた completed。
                # 成果は通常どおり処理するが、拒否の嵐があったことを activity に残す。
                self._activity.write(
                    Category.IMPROVE, "tool_budget_abort_recovered",
                    f"mission={ctx.mission_id} reason={str(result.reason)[:500]}")
            output = result.output or {}
            verdict = self._inspect_output(output, ctx, conn=conn)     # 手順1
            if not verdict.ok:
                self._finalize_output_invalid(
                    conn, ctx=ctx, reason=verdict.reason, now=now,
                    tool_calls=tool_calls)
                return

            selection = self._select_and_bind(conn, output, ctx, now=now)  # 手順2
            if not selection.won:
                self._finalize_loser(conn, ctx=ctx, output=output, now=now)
                return

            artifact = output.get("artifact", {})
            atype = artifact.get("type")
            gate_metrics: dict = {}
            approval_payload = None
            gate_rows: list[dict] = []

            # M4 (2026-08-30、codex レビュー Major 4): plugin artifact は
            # 既存の決定論 gate (staging 実体・pytest・hash) が虚偽
            # completed を防げるが、report artifact はモデルの body_md を
            # 実体突合なしでファイル公開し得る。`result.recovered` (段B の
            # timeout/no-output からの session resume 追撃で回収した出力)
            # のときは report を公開せず、既存の observation 終端経路
            # (`_finalize_report_or_observation`) を再利用して降格する。
            # plugin/observation 形は従来どおり (plugin は既存 gate が防衛線)。
            # `proposal_kind == "risk_gate"` は対象外 — このケースは
            # `_prepare_report_if_applicable` が本文書込み前に None を返す
            # ため元々ファイルを公開しない (降格の動機である「実体突合
            # なしでファイル公開し得る」が成立しない) — 対象にすると設計書
            # §4.3 状態表の逐語ラベル `unsupported_in_plan10:risk_gate` を
            # 無条件で書き換えてしまう (実装者裁定 — 挙動差分を実際に
            # ファイル公開しうる経路だけに絞る)。
            if (getattr(result, "recovered", False) and atype == "report"
                    and artifact.get("proposal_kind") != "risk_gate"):
                from agentic_fx._safe_error import safe_text
                title = safe_text(str(artifact.get("title", "")))
                # activity は終端 tx より前に書く (`_finalize_failed_
                # mission`/`_finalize_output_invalid` と同じ規律 —
                # [fail-observability] 是正: tx が例外を送出しても降格の
                # 痕跡が activity.log に残るようにする)。
                self._activity.write(
                    Category.IMPROVE, "report_demoted_recovered",
                    f"mission={ctx.mission_id} title={title}")
                self._finalize_report_or_observation(
                    conn, ctx=ctx, backlog_id=selection.backlog_id,
                    report_path=None,
                    artifact={
                        "type": "observation",
                        "reason": (f"title={title} — "
                                  "resume 回収経由のため降格"),
                    },
                    now=now, tool_calls=tool_calls)
                return

            if atype == "plugin":
                candidate_dir = ctx.staging_dir / artifact["name"]
                gate_verdict = self._run_plugin_gate(                  # 手順3
                    candidate_dir, name=artifact["name"],
                    source_snapshot_dir=ctx.source_snapshot_dir,
                    inventory=(ctx.inventory if ctx.inventory is not None
                              else self._inventory_for_gate(conn)))
                if not gate_verdict.passed:
                    self._finalize_gate_failed(
                        conn, ctx=ctx, backlog_id=selection.backlog_id,
                        reason=f"gate_failed:{gate_verdict.reason}", now=now,
                        tool_calls=tool_calls)
                    return

                from agentic_fx.plugin import approval
                from agentic_fx.plugin import loader as plugin_loader
                candidate_meta = plugin_loader._discover_one(
                    candidate_dir, artifact["name"])
                if candidate_meta is None:
                    self._finalize_gate_failed(
                        conn, ctx=ctx, backlog_id=selection.backlog_id,
                        reason="gate_failed:loader_rejected", now=now,
                        tool_calls=tool_calls)
                    return
                try:
                    approval.assert_max_bars_within_limit(
                        candidate_meta, settings=self._settings)
                except ValueError as exc:
                    self._finalize_gate_failed(
                        conn, ctx=ctx, backlog_id=selection.backlog_id,
                        reason=f"gate_failed:max_bars_limit:{exc}", now=now,
                        tool_calls=tool_calls)
                    return

                kind = self._read_candidate_kind(candidate_dir)
                if kind not in {"indicator", "strategy"}:
                    self._finalize_gate_failed(
                        conn, ctx=ctx, backlog_id=selection.backlog_id,
                        reason=f"gate_failed:kind_unsupported:{kind}", now=now,
                        tool_calls=tool_calls)
                    return
                if kind == "strategy":
                    try:
                        from agentic_fx.plugin.resolve import ResolvedIndicatorSet
                        strategy_verdict = self._run_strategy_gate(     # 手順4
                            conn, name=artifact["name"],
                            pairs=self._read_candidate_pairs(candidate_dir),
                            timeframe=self._read_candidate_timeframe(candidate_dir),
                            content_hash=gate_verdict.content_hash, now=now,
                            meta=candidate_meta, kind=kind,
                            record_fn=gate_rows.append,
                            # [indicator-consumption-wiring] T3 暫定
                            # (T5b Step 5-5 で `ImproveRunContext.inventory`
                            # からの `require` 解決に置き換える)。
                            resolved=ResolvedIndicatorSet.empty(
                                self._root / "plugins"))
                    except holdout.NoHistoryError as exc:
                        # Missing market history is an expected gate verdict;
                        # unrelated ValueErrors still abort the commit.
                        self._finalize_gate_failed(
                            conn, ctx=ctx, backlog_id=selection.backlog_id,
                            reason=("gate_failed:backtest_data_unavailable:"
                                    f"{exc}"), now=now,
                            gate_rows=tuple(gate_rows), tool_calls=tool_calls)
                        return
                    if not strategy_verdict.evaluable:
                        self._finalize_gate_failed(
                            conn, ctx=ctx, backlog_id=selection.backlog_id,
                            reason=strategy_verdict.observation_reason, now=now,
                            gate_rows=tuple(gate_rows), tool_calls=tool_calls)
                        return
                    # [profitability-floor] T1 Step 1-3 (2026-09-12、設計書
                    # §3 T1-b): 収益性フロア不合格の再ルート。`reason` は
                    # 固定文言 `unprofitable` (last_result にそのまま流れる
                    # — 遮断 8 の 1 bit 例外)。`report_detail` は親専有
                    # レポートにのみ渡り、activity/last_result には流れない。
                    if strategy_verdict.floor_reason:
                        # [profitability-floor-fix] G2 (2026-09-13): holdout
                        # 段の per-pair 成績は `StrategyGateVerdict.
                        # holdout_metrics` (evaluator が判定に使った実際の
                        # dict、`_run_strategy_gate` が enforce で in_sample
                        # 段を落とし holdout を回さなかったときは `None`)。
                        # `gate_rows` (record_fn sink) 経由では取らない —
                        # sink の `metrics` は永続化用に呼び出し元が別途
                        # 組み立てる値であり、判定に使った pf/avg_r と
                        # 一致する保証がない (fixture 実測で乖離を確認)。
                        report_detail = self._render_floor_report_detail(
                            in_sample_metrics=(
                                strategy_verdict.candidate_metrics or {}),
                            holdout_metrics=(
                                strategy_verdict.holdout_metrics or {}))
                        self._finalize_gate_failed(
                            conn, ctx=ctx, backlog_id=selection.backlog_id,
                            reason="unprofitable", now=now,
                            gate_rows=tuple(gate_rows), tool_calls=tool_calls,
                            mission_outcome="unprofitable",
                            report_detail=report_detail)
                        return
                    gate_metrics["baseline"] = strategy_verdict.baseline_row
                    # [approval-payload-missing-gate-metrics] 是正 (A4 10
                    # 回目 claude #69 観測 A、2026-09-11): ゲートが実際に
                    # 測った in-sample/holdout の成績を承認 payload へ載せる。
                    # 従来はここで代入されるのが baseline (メタ情報のみ) だけ
                    # だったため、holdout で負けている候補でも人間の承認
                    # 材料には agent 自身の自己申告しか出てこなかった。
                    # in_sample は pair→metrics の dict (StrategyGateVerdict.
                    # candidate_metrics をそのまま)。holdout は gate_rows の
                    # scope=='holdout_gate' 行の metrics から組む — pair が
                    # 1 つなら metrics dict そのもの、複数なら pair→metrics
                    # の dict (`_build_approval_payload` が期待する形、
                    # ブリーフの明示指定どおり)。
                    gate_metrics["in_sample"] = strategy_verdict.candidate_metrics
                    holdout_by_pair = {
                        row["pair"]: row["metrics"] for row in gate_rows
                        if row.get("scope") == "holdout_gate"}
                    if len(holdout_by_pair) == 1:
                        gate_metrics["holdout"] = next(iter(holdout_by_pair.values()))
                    elif holdout_by_pair:
                        gate_metrics["holdout"] = holdout_by_pair
                    gate_metrics["meta"] = candidate_meta

                candidate_path = (f"plugins/_staging/{ctx.mission_id}/"
                                  f"{artifact['name']}")
                approval_payload = self._build_approval_payload(        # 手順5
                    conn, name=artifact["name"], kind=kind,
                    content_hash=gate_verdict.content_hash,
                    artifact_hash=gate_verdict.artifact_hash,
                    ctx_ledger=ctx.ledger, mission_id=ctx.mission_id,
                    backlog_id=selection.backlog_id,
                    candidate_origin="staging", candidate_path=candidate_path,
                    gate_metrics=gate_metrics, output=output, now=now)
                # /code-review 2 周目 CR1 是正 (2026-09-12): 質検査
                # (approval-quality 設計書 §A) は check-then-insert のまま
                # ここ (bare `SELECT` が `_finalize_success` の
                # `BEGIN IMMEDIATE` より前に実行される autocommit) に置くと
                # 並行 slot 間でレースする — `settings.improve.parallel>1`
                # では 2 スレッドが同時にここを通過し両方とも一致無しと
                # 判定しうる。質検査本体は `_finalize_success` の
                # `BEGIN IMMEDIATE` 内側 (`_check_duplicate_metrics_for_
                # approval`) へ移した。ここではもう呼ばない。

            report_path = None
            if approval_payload is None:
                report_path = self._prepare_report_if_applicable(       # 手順6
                    conn, ctx=ctx, artifact=artifact, output=output, now=now,
                    backlog_id=selection.backlog_id)
                if report_path is _REPORT_WRITE_FAILED:
                    # OSError を _prepare_report_if_applicable 自身が Tx-2
                    # で終端済み — 二重終端を避けてここで抜ける。
                    return

            if approval_payload is not None:
                demotion = self._finalize_success(                      # 手順7-9
                    conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
                    backlog_id=selection.backlog_id, slot_key=ctx.slot_key,
                    approval_payload=approval_payload, now=now,
                    ledger_entries=tuple(ctx.ledger.entries()),
                    gate_rows=tuple(gate_rows), ctx=ctx,
                    tool_calls=tool_calls)
                if demotion is not None:
                    # CR1 是正: `_finalize_success` が質検査ヒットを検出し
                    # 内側 tx を rollback 済み (何も永続化していない) —
                    # observation 経路へ再ルートする。CR3: 全 pair 分の
                    # gate 行 (in_sample/holdout とも) を
                    # mission_outcome='observation' で残す — 一致した pair
                    # は reason に埋め込む (どの pair が降格を引いたかを
                    # 人間が追跡できるようにする)。
                    self._finalize_report_or_observation(
                        conn, ctx=ctx, backlog_id=selection.backlog_id,
                        report_path=None,
                        artifact={
                            "type": "observation",
                            "reason": (
                                f"duplicate_metrics_of:{demotion.content_hash} "
                                f"pair={demotion.pair}"),
                        },
                        now=now, gate_rows=tuple(gate_rows),
                        tool_calls=tool_calls)
            else:
                self._finalize_report_or_observation(
                    conn, ctx=ctx, backlog_id=selection.backlog_id,
                    report_path=report_path, artifact=artifact, now=now,
                    tool_calls=tool_calls)
        except BaseException:
            commit_raised = True
            if ctx.ledger.state() == "FROZEN":
                ctx.ledger.mark_persist_failed()
            raise
        finally:
            if owns_conn:
                conn.close()

    def _prepare_report_if_applicable(self, conn, *, ctx, artifact, output,
                                      now, backlog_id=None) -> str | None:
        """`artifact.type=='report'` かつ `proposal_kind != 'risk_gate'` の
        ときだけ本文を `.tmp/*.part` へ書き、Tx-2 で `report_state=
        'prepared'` + 予定パスを書く。

        Task 12 Step3: `_finalize_gate_failed` と同じ扱いに揃える —
        `_write_report_part` の OSError (symlink 罠等) を捕まえず突き抜けると
        Mission が非終端のまま落ちる。ここで捕まえたときは、この関数自身が
        Tx-2 で Mission を `report_state='failed'`/`run_result=None` に
        終端し、呼び出し元 (`commit()`) には `_REPORT_WRITE_FAILED`
        sentinel を返して以降の `_finalize_report_or_observation` 呼び出し
        (= 二重終端) を止めさせる。"""
        if artifact.get("type") != "report":
            return None
        if artifact.get("proposal_kind") == "risk_gate":
            return None
        reports_dir = self._root / "data" / "improve_reports"
        (reports_dir / ".tmp").mkdir(parents=True, exist_ok=True)
        body_md = artifact.get("body_md", "")
        try:
            part_path = self._write_report_part(
                reports_dir, mission_id=ctx.mission_id, body_md=body_md)
        except OSError as exc:
            conn.execute("BEGIN IMMEDIATE")
            ledger_ids = None
            try:
                ledger_ids = self._persist_ledger_in_tx(
                    conn, ctx=ctx, now=now,
                    mission_outcome="report_failed")
                missions_store.finish_improve_mission(
                    conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
                    slot_key=ctx.slot_key, mission_status="completed",
                    run_result=None, now=now,
                    report_state="failed",
                    backlog_transition=(
                        {"backlog_id": backlog_id, "status": "observation",
                         "last_result":
                             f"report_failed:{safe_error_text(exc)}"}
                        if backlog_id is not None else None),
                    commit=False)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            self._settle_ledger_after_commit(
                conn, ctx=ctx, ledger_ids=ledger_ids, outcome="report_failed", now=now)
            return _REPORT_WRITE_FAILED
        final_path = self._final_report_path(
            reports_dir, mission_id=ctx.mission_id, now=now)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE improvement_runs SET report_state='prepared', "
                "report_path=? WHERE id=?", (str(final_path), ctx.run_id))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return str(final_path)

    def _finalize_report_or_observation(self, conn, *, ctx, backlog_id,
                                        report_path, artifact, now,
                                        gate_rows=(),
                                        tool_calls: int | None = None) -> None:
        """承認申請を出さない経路の Tx-2 + finish。

        /code-review 2 周目 CR3 是正 (2026-09-12): `gate_rows` (既定 ()) —
        質検査 (CR1) で observation へ降格した strategy 候補は、gate が
        実際に計測した in_sample/holdout の成績行 (全 pair 分、一致しな
        かった pair も含む) を失わずに残す。`_finalize_gate_failed` と
        同じく `_persist_gate_rows(..., mission_outcome='observation')` を
        この tx の内側で呼ぶ。他の呼び出し元 (report/observation
        (質検査以外の理由)/`unsupported_in_plan10:risk_gate`) は
        `gate_rows=()` のまま — `_persist_gate_rows` は空リストで何もしない
        ため無害。

        round2 #6 是正 (2026-08-29、verified-round2.md #6、設計逐語違反):
        `report_path is None` になる分岐は 2 つある —
        `artifact.type != 'report'` (= observation、`IMPROVE_OUTPUT_SCHEMA`
        の正規出力) と `proposal_kind == 'risk_gate'` (本プラン未対応)。
        従来はどちらも `unsupported_in_plan10:risk_gate` を無条件に書いて
        いたが、設計書 §4.3 状態機械表は `artifact=observation →
        observation:<reason>` の逐語行を持つ。`artifact["reason"]` は
        LLM 由来 (次 Mission の context に注入される backlog history) の
        ため `safe_text` でサニタイズする (`safe_error_text` は
        `BaseException` 前提で `type(e).__name__: ` 接頭辞を足すため、
        素の文字列である `reason` には型名の無い `safe_text` を使う —
        どちらも同じ `_URL_RE`/`_SECRET_RE` 抑止を共有する)。"""
        from agentic_fx.store import missions as missions_store
        if report_path is None and artifact.get("type") == "observation":
            from agentic_fx._safe_error import safe_text
            observation_last_result = (
                f"observation:{safe_text(str(artifact.get('reason', '')))}")
        else:
            # D-15 是正: 設計書 §4.3 状態表の逐語は
            # `unsupported_in_plan10:risk_gate` — `proposal_kind=='risk_gate'`
            # (本プラン未対応) のときの正規ラベル。
            observation_last_result = "unsupported_in_plan10:risk_gate"
        outcome = "report" if report_path is not None else "observation"
        conn.execute("BEGIN IMMEDIATE")
        ledger_ids = None
        try:
            ledger_ids = self._persist_ledger_in_tx(
                conn, ctx=ctx, now=now, mission_outcome=outcome)
            self._persist_gate_rows(
                conn, gate_rows=gate_rows, now=now, mission_id=ctx.mission_id,
                mission_outcome=outcome)
            missions_store.finish_improve_mission(
                conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
                slot_key=ctx.slot_key, mission_status="completed",
                run_result=("report" if report_path is not None else None),
                now=now,
                report_path=report_path,
                report_state=("prepared" if report_path is not None else "none"),
                backlog_transition=(
                    {"backlog_id": backlog_id, "status": "observation",
                     "last_result": observation_last_result}
                    if report_path is None and backlog_id is not None
                    else ({"backlog_id": backlog_id, "status": "done",
                          "last_result": "reported"}
                          if backlog_id is not None else None)),
                commit=False)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        self._settle_ledger_after_commit(
            conn, ctx=ctx, ledger_ids=ledger_ids, outcome=outcome, now=now)
        # 成功系 activity は commit 成功後 (1 周目 F2。`_finalize_success` と同じ理由)。
        if self._activity is not None and report_path is None:
                self._activity.write(
                    Category.IMPROVE, "mission_observation",
                    f"mission={ctx.mission_id} backlog="
                    f"{backlog_id if backlog_id is not None else '-'} "
                    f"reason={observation_last_result}"
                    f"{_tool_calls_suffix(tool_calls)}", str(ctx.mission_id))
        if report_path is not None:
            reports_dir = self._root / "data" / "improve_reports"
            part_path = reports_dir / ".tmp" / f"improve-{ctx.mission_id}.md.part"
            published = self._publish_report(
                conn, run_id=ctx.run_id, part_path=part_path,
                final_path=Path(report_path), now=now)
            # 2 周目 CR3: rename 失敗 (`_fail_report` → report_state=failed) は
            # 例外を出さず False で返るので、True のときだけ「published」を書く。
            if published and self._activity is not None:
                self._activity.write(
                    Category.IMPROVE, "report_published",
                    f"mission={ctx.mission_id} backlog="
                    f"{backlog_id if backlog_id is not None else '-'} "
                    f"path={report_path}"
                    f"{_tool_calls_suffix(tool_calls)}", str(ctx.mission_id))
        # round2 #7 是正 (2026-08-29、verified-round2.md #7、設計逐語違反):
        # 設計 §4.2 手順9「承認申請を出した staging は残す。それ以外は
        # 削除。」— report/observation 経路は承認申請を出さないため、
        # 他 4 経路 (gate_failed/output_invalid/failed/loser) と同じく
        # staging を削除する (公開の後)。従来は漏れており、`_snapshot_src`
        # が 0500/0400 の readonly tree のまま `sweep_orphans` の
        # rmtree(ignore_errors=True) では消えず、report を出す Mission
        # 1 本につき永久に積んでいた。`mark_discarded()` を先に呼ぶことで
        # `finally` 節の無条件 `mark_persisted()` は (既に DISCARDED のため)
        # RuntimeError → 既存の `except RuntimeError: pass` に吸われる
        # (この経路の台帳エントリは実際には永続化されていないため、
        # mark_persisted は元々誤り — 副次的に解消する)。
        self._delete_staging(ctx)

    def _read_candidate_kind(self, candidate_dir: Path) -> str:
        """candidate の config.yaml から kind (indicator/strategy) を読む。"""
        import yaml
        config_path = candidate_dir / "config.yaml"
        if not config_path.exists():
            raise FileNotFoundError(f"config.yaml not found: {config_path}")
        config = yaml.safe_load(config_path.read_text())
        return config.get("kind", "indicator")

    def _read_candidate_pairs(self, candidate_dir: Path) -> list[str]:
        """candidate の config.yaml から pairs リストを読む。"""
        import yaml
        config_path = candidate_dir / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        return config.get("pairs", [])

    def _read_candidate_timeframe(self, candidate_dir: Path) -> str:
        """candidate の config.yaml から timeframe (1h/4h/1d など) を読む。"""
        import yaml
        config_path = candidate_dir / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        return config.get("timeframe", "1h")

    def _delete_staging(self, ctx: ImproveRunContext) -> None:
        """§4.2 手順9: 「承認申請を出した staging は残す。それ以外は削除」
        — 非承認の全終端経路 (failed/output_invalid/loser/gate_failed) が
        呼ぶ。存在しない/既に消えている場合も無害 (`ignore_errors=True`)。

        D-15 是正 (着手前検証): `staging_dir/_snapshot_src/` は
        `_chmod_tree_readonly` (§4/10.3 節) でディレクトリ 0500・ファイル
        0400 に固めてある。`shutil.rmtree` は削除対象の各ディレクトリに
        書込権限が要る (`unlink`/`rmdir` は親エントリの書込権限で決まる)
        ため、`ignore_errors=True` だけでは読取専用ツリーの削除に失敗して
        `_snapshot_src` 配下がディスクに残り続ける (実測: `improve-staging-
        <mission_id>` 命名時代は削除先が `plugins/_staging/<mission_id>`
        と別パスだったため検査対象自体が常に存在せずこの欠陥が検出
        されなかった)。削除前にツリー全体を書込可能に戻す。"""
        import shutil
        from agentic_fx.plugin.version_store import chmod_tree_writable
        chmod_tree_writable(ctx.staging_dir)
        shutil.rmtree(ctx.staging_dir, ignore_errors=True)

    def _finalize_failed_mission(self, conn, *, ctx, result, now,
                                 slot_terminalize: bool = True) -> None:
        """§3.6: timeout/failed/max_turns → missions を `failed` で終端。
        `improvement_runs.result` は CHECK (`approval`/`report` のみ) の
        制約上 NULL のまま (`result.status` の文字列をそのまま渡すと
        IntegrityError になる)。backlog は未選択 (Tx-1 未到達) のため
        遷移なし。staging を削除し、台帳は `DISCARDED` (呼び出し元
        `commit()` の手順0で既に FROZEN、ここで確定させる)。

        `slot_terminalize=False` (I2b 是正、ユーザー裁定 D①、2026-08-28):
        pre-ready 失敗の巻き戻し (呼び出し元が既に `revert_to_reserved`
        済み) では `slot_key=None` を `finish_improve_mission` へ渡し、
        mission/run のみ終端して slot には触れない (`mark_terminal` の
        無条件 UPDATE が巻き戻した `reserved` を潰すのを防ぐ)。

        [fail-observability] 是正 (2026-08-30): `_finalize_output_invalid`
        は activity に `output_invalid` を書くのに、ここは何も書いておらず
        非対称だった — improve mission が failed のとき死因が一切残らない
        原因の一つ。`result.reason` (runner が診断した失敗理由、無ければ
        `-`) を添えて `mission_failed` を書く。"""
        reason_text = "-" if result.reason is None else str(result.reason)[:500]
        # duck-typed な独自 Result スタブ (`tool_calls` 属性を持たない) を
        # 渡すテストがあるため getattr で読む。
        self._activity.write(
            Category.IMPROVE, "mission_failed",
            f"mission={ctx.mission_id} status={result.status} "
            f"reason={reason_text}"
            f"{_tool_calls_suffix(getattr(result, 'tool_calls', None))}")
        self._delete_staging(ctx)
        conn.execute("BEGIN IMMEDIATE")
        ledger_ids = None
        try:
            ledger_ids = self._persist_ledger_in_tx(
                conn, ctx=ctx, now=now, mission_outcome="failed")
            if (result.status in ("timeout", "max_turns") or
                    (result.status == "failed" and
                     is_tool_budget_abort(result.reason))):
                entries = ctx.ledger.entries()
                n_bt = sum(e["kind"] == "run_backtest" for e in entries)
                n_corr = sum(e["kind"] == "analyze_corr" for e in entries)
                # [system-note-type-by-cause] (run7 欠陥 B、2026-09-09): 型は
                # 死因で決める。旧実装は backtest 件数だけで型 B を選び、枯渇して
                # いない timeout に「予算が尽きたら…」を申し送っていた。
                if n_bt == 0:
                    idea = ("improve mission が最終出力なしで終了した "  # 型 A
                            "(timeout/max_turns)。strategy はまず run_backtest "
                            "を呼び、self-test の修正を繰り返さないこと")
                elif is_tool_budget_abort(result.reason):
                    idea = (                                        # 型 B: 予算枯渇 abort
                        "improve mission が backtest 完走後に最終出力を出さずに終了した。"
                        "予算が尽きたら (`budget exhausted` を受けたら) 直ちに現状の"
                        "候補で提出するか observation を出すこと。拒否された tool を"
                        "繰り返し呼ばないこと")
                else:
                    idea = (                                        # 型 C: 時間切れ
                        "improve mission が backtest 完走後に時間切れで終了した "
                        "(最終出力なし)。backtest の結果を得たら self-test の修正に"
                        "戻らず、早めに提出か observation を出すこと")
                # T4 変更点2: note の best は `_persist_ledger_in_tx` の後・
                # `finish_improve_mission` の前、同じ tx 内で読む。
                archive_rows = self._archive_rows_for_note(
                    conn, mission_id=ctx.mission_id)
                last_result = (
                    f"mission #{ctx.mission_id} status={result.status} "
                    f"reason={result.reason or '-'} "
                    f"run_backtest={n_bt} analyze_corr={n_corr}")
                last_result += self._best_note_suffix(
                    best_candidate(archive_rows))
                try:
                    backlog_store.upsert_system_note(
                        conn, idea=idea, last_result=last_result, now=now)
                except sqlite3.Error as e:
                    self._activity.write(
                        Category.IMPROVE, "backlog_note_failed",
                        f"mission={ctx.mission_id} {safe_error_text(e)}")
            missions_store.finish_improve_mission(
                conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
                slot_key=ctx.slot_key if slot_terminalize else None,
                mission_status="failed",
                run_result=None, now=now, backlog_transition=None,
                commit=False)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        self._settle_ledger_after_commit(
            conn, ctx=ctx, ledger_ids=ledger_ids, outcome="failed", now=now)

    def _finalize_output_invalid(self, conn, *, ctx, reason, now,
                                 tool_calls: int | None = None) -> None:
        """§4.2 手順1 不合格 (schema 不整合・`artifact.name` 非正規形・
        `staging_dir/<name>` 異常等) → Mission `failed`、
        `improvement_runs.result` は NULL のまま、staging 削除、
        台帳 `DISCARDED`。backlog は Tx-1 未到達のため遷移なし。

        T4 変更点2: `_finalize_failed_mission` の型 A/B/C と対の system
        note を新設する (型 A の「最終出力なし」の流用ではなく、
        schema/名前検査不合格専用の文面)。best があれば末尾に付く。"""
        self._delete_staging(ctx)
        self._activity.write(
            Category.IMPROVE, "output_invalid",
            f"mission={ctx.mission_id} reason={reason}"
            f"{_tool_calls_suffix(tool_calls)}")
        conn.execute("BEGIN IMMEDIATE")
        ledger_ids = None
        try:
            ledger_ids = self._persist_ledger_in_tx(
                conn, ctx=ctx, now=now, mission_outcome="output_invalid")
            archive_rows = self._archive_rows_for_note(
                conn, mission_id=ctx.mission_id)
            idea = (
                "improve mission の最終出力が schema/名前検査に不合格。"
                "出力 schema を守り、candidate 名は "
                "`^[a-z][a-z0-9_]{0,63}$` に従うこと")
            last_result = f"mission #{ctx.mission_id} reason={reason}"
            last_result += self._best_note_suffix(best_candidate(archive_rows))
            try:
                backlog_store.upsert_system_note(
                    conn, idea=idea, last_result=last_result, now=now)
            except sqlite3.Error as e:
                self._activity.write(
                    Category.IMPROVE, "backlog_note_failed",
                    f"mission={ctx.mission_id} {safe_error_text(e)}")
            missions_store.finish_improve_mission(
                conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
                slot_key=ctx.slot_key, mission_status="failed",
                run_result=None, now=now, backlog_transition=None,
                commit=False)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        self._settle_ledger_after_commit(
            conn, ctx=ctx, ledger_ids=ledger_ids, outcome="output_invalid", now=now)

    def _finalize_loser(self, conn, *, ctx, output, now) -> None:
        """§4.2 手順2 敗者経路 (Tx-1 の選択 CAS で rowcount=0):
        backlog は一切遷移しない (自分は選択に負けただけで、勝者側の状態を
        壊さない — §4.3「遷移は起きない」)。親が「重複のため見送り」の
        自動レポートを 10.9 節の outbox ヘルパ (`_write_report_part`/
        `_publish_report`) で書き、run を `result='report'` で終端する
        (プラン10 Task10-11 申し送り)。staging は残す候補が無いため削除、
        台帳はこの Mission の RPC 結果を一切永続化しないため `DISCARDED`。"""
        self._delete_staging(ctx)
        idea = output.get("selected", {}).get("idea", "")
        body_md = (
            f"# Improve Mission {ctx.mission_id} — skipped (duplicate)\n\n"
            f"Selected backlog idea `{idea!r}` was already claimed by a "
            "concurrent mission before this mission's Tx-1 CAS. No "
            "candidate was produced by this mission.\n")
        reports_dir = self._root / "data" / "improve_reports"
        (reports_dir / ".tmp").mkdir(parents=True, exist_ok=True)
        # Task 12 Step3: `_finalize_gate_failed` と同じ扱いに揃える —
        # `_write_report_part` の O_EXCL|O_NOFOLLOW 書込失敗 (symlink 罠等)
        # を捕まえず OSError が commit() を突き抜けると Mission が非終端の
        # まま落ちる。ここで捕まえて fail closed の終端 tx へ倒す
        # (backlog は敗者経路なので触らない — 本メソッドの docstring 参照)。
        try:
            part_path = self._write_report_part(
                reports_dir, mission_id=ctx.mission_id, body_md=body_md)
        except OSError:
            conn.execute("BEGIN IMMEDIATE")
            ledger_ids = None
            try:
                ledger_ids = self._persist_ledger_in_tx(
                    conn, ctx=ctx, now=now, mission_outcome="report_failed")
                missions_store.finish_improve_mission(
                    conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
                    slot_key=ctx.slot_key, mission_status="completed",
                    run_result=None, now=now,
                    report_state="failed",
                    backlog_transition=None, commit=False)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            self._settle_ledger_after_commit(
                conn, ctx=ctx, ledger_ids=ledger_ids, outcome="report_failed", now=now)
            return
        final_path = self._final_report_path(
            reports_dir, mission_id=ctx.mission_id, now=now)

        conn.execute("BEGIN IMMEDIATE")
        ledger_ids = None
        try:
            ledger_ids = self._persist_ledger_in_tx(
                conn, ctx=ctx, now=now, mission_outcome="loser")
            missions_store.finish_improve_mission(
                conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
                slot_key=ctx.slot_key, mission_status="completed",
                run_result="report", now=now,
                report_path=str(final_path), report_state="prepared",
                backlog_transition=None, commit=False)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        self._settle_ledger_after_commit(
            conn, ctx=ctx, ledger_ids=ledger_ids, outcome="loser", now=now)
        self._publish_report(conn, run_id=ctx.run_id, part_path=part_path,
                             final_path=final_path, now=now)

    _FLOOR_REPORT_METRIC_KEYS = (
        "trades", "pf", "win_rate", "avg_r", "max_drawdown", "total_pnl",
        "kill_switch_latches", "evaluable")

    def _render_floor_report_detail(self, *, in_sample_metrics: dict,
                                     holdout_metrics: dict) -> str:
        """[profitability-floor-fix] G2 (2026-09-13、plan `:258-263`):
        `_finalize_gate_failed` のフロア不合格レポート本文専用の
        Markdown を組み立てる。in_sample/holdout の pair ごとに
        `trades/pf/win_rate/avg_r/max_drawdown/total_pnl/
        kill_switch_latches/evaluable` の 8 指標 + 落ちた段 + 落ちた
        pair + 適用閾値を含める。`_floor_fail_items` (`_check_
        profitability_floor` と同じ判定条件) を再利用し、判定条件を
        二重実装しない。

        `holdout_metrics` が空 (enforce モードで in_sample 段が落ち、
        holdout を回さなかった場合) は holdout 表を出さない — plan pin
        「in_sample で落ちた場合は holdout 表無し / holdout で落ちた場合
        は両方」。戻り値は `_finalize_gate_failed` の `body_md` に
        そのまま追記される文字列であり、`last_result`/`activity`/
        INDEX には一切流れない (呼び出し元がそこへ渡さない契約)。"""
        stages: list[tuple[str, dict]] = [("in_sample", in_sample_metrics)]
        if holdout_metrics:
            stages.append(("holdout", holdout_metrics))

        failed_scopes: list[str] = []
        failed_pairs: list[str] = []
        tables: list[str] = []
        for scope, per_pair in stages:
            fail_items = _floor_fail_items(
                per_pair, settings=self._settings, scope=scope)
            if fail_items:
                failed_scopes.append(scope)
                for pair, _msg in fail_items:
                    if pair not in failed_pairs:
                        failed_pairs.append(pair)
            header = "| pair | " + " | ".join(
                self._FLOOR_REPORT_METRIC_KEYS) + " |"
            separator = "|---" * (len(self._FLOOR_REPORT_METRIC_KEYS) + 1) + "|"
            rows = [header, separator]
            for pair, m in per_pair.items():
                values = [str(m.get(key, "")) for key
                          in self._FLOOR_REPORT_METRIC_KEYS]
                rows.append("| " + pair + " | " + " | ".join(values) + " |")
            tables.append(f"### {scope}\n\n" + "\n".join(rows))

        lines = [
            f"failed_stage: {', '.join(failed_scopes)}",
            f"failed_pairs: {', '.join(failed_pairs)}",
            f"thresholds: {floor_settings_kv(self._settings.improve.gate)}",
            "",
        ]
        return "\n".join(lines) + "\n\n".join(tables)

    def _finalize_gate_failed(self, conn, *, ctx, backlog_id, reason, now,
                              gate_rows=(), tool_calls: int | None = None,
                              mission_outcome: str = "gate_failed",
                              report_detail: str = "") -> None:
        """§4.2 手順3/4 不合格・評価不能 → §4.3: backlog を `observation`
        (`last_result` は呼び出し元が組み立てた `reason` そのまま —
        `commit()` が `gate_failed:<...>`/`insufficient_trades:<n>` の形で
        渡す)。承認申請は出さないため mission は `completed` で終端
        (取引は止めない — R8)。staging を削除し、台帳は `DISCARDED`。

        [profitability-floor] T1 Step 1-3 (2026-09-12、設計書 §3 T1-b、
        codex I6): `mission_outcome` は正常分岐 (report 作成成功) の
        gate 行・ledger 行・settle の 3 箇所すべてへ渡す branch-local な
        1 つの値 (既定 `"gate_failed"`、フロア経路は呼び出し元が
        `"unprofitable"` を渡す)。**report 作成失敗分岐は
        `outcome="report_failed"` に固定し、この引数で上書きしない**
        (既存の `report_failed` 意味論を壊さない)。`report_detail` は
        `body_md` の後段に追記する人間向けレポート専用の文字列 —
        `last_result`/`activity` には一切流さない (pin F2-7/F5-2 の
        「ラベルと数値の分離」契約)。

        [profitability-floor-fix] G1 (2026-09-13、plan `:258-263`):
        フロア経路 (`mission_outcome=="unprofitable"`) だけ、`activity`
        の `gate_failed` 行に `reason=unprofitable` と同じ行で適用閾値
        3 値 (`floor_settings_kv` — `ImproveGateSettings.snapshot()`
        由来) を足す。**`report_detail` の数値 (per-pair 8 指標) は
        ここには一切書かない** — 通常の `gate_failed` (標本不足など)
        には閾値も出ない。

        D-15 是正 (着手前検証): 設計書 §4.2 手順6「ゲート不合格・評価不能・
        observation・敗者のとき、reports に書く」に従い、`_finalize_loser`
        と**全く同じ既存の outbox 規約** (`data/improve_reports/
        improve-YYYY-MM-DD-<mission_id>.md` — F-3 是正 (検収 task12
        2026-08-27) で日付を追加) でレポートを書く。旧実装は report を
        一切書かず `run_result=None`/
        `report_state='none'` のまま終端していた
        (`tests/loops/test_improve_e2e.py::
        test_gate_failure_stops_at_report_no_approval_request` が期待する
        レポートファイルに届かなかった)。"""
        self._delete_staging(ctx)
        # [profitability-floor-fix] G1: フロア不合格 (mission_outcome=
        # "unprofitable") のときだけ同じ行に閾値 3 値を足す。他の
        # gate_failed 経路 (標本不足・pytest 不合格等) には出さない。
        floor_suffix = (
            f" {floor_settings_kv(self._settings.improve.gate)}"
            if mission_outcome == "unprofitable" else "")
        self._activity.write(
            Category.IMPROVE, "gate_failed",
            f"mission={ctx.mission_id} reason={reason}{floor_suffix}"
            f"{_tool_calls_suffix(tool_calls)}")
        body_md = (
            f"# Improve Mission {ctx.mission_id} — gate failed\n\n"
            f"reason: `{reason}`\n\nNo approval request was produced by "
            "this mission.\n")
        if report_detail:
            # [profitability-floor] T1 Step 1-3: 親専有レポート本文の
            # 後段にのみ全数値を書く。activity/last_result には流れない
            # (report_detail の呼び出し元は `reason` と混ぜていない)。
            body_md += f"\n## Profitability floor detail\n\n{report_detail}\n"
        reports_dir = self._root / "data" / "improve_reports"
        (reports_dir / ".tmp").mkdir(parents=True, exist_ok=True)
        # D-15 是正: 設計書 §4.2 手順6/7「一時ファイル作成に失敗していたら
        # result=NULL, report_state='failed'」/ §4.3 状態表「selected →
        # レポートの一時ファイル作成に失敗 → observation
        # (report_failed:<safe_reason>)」。旧実装 (3 箇所とも) は
        # `_write_report_part` の `O_EXCL|O_NOFOLLOW` 書込失敗 (symlink 罠
        # 等) を一切捕まえておらず、`OSError` が `commit()` を突き抜けて
        # Mission が非終端のまま落ちていた
        # (`tests/loops/test_improve_e2e.py::
        # test_report_tmp_symlink_fails_closed`)。ここで捕まえて fail
        # closed の終端 tx へ倒す。
        try:
            part_path = self._write_report_part(
                reports_dir, mission_id=ctx.mission_id, body_md=body_md)
        except OSError as exc:
            conn.execute("BEGIN IMMEDIATE")
            ledger_ids = None
            try:
                ledger_ids = self._persist_ledger_in_tx(
                    conn, ctx=ctx, now=now, mission_outcome="report_failed")
                self._persist_gate_rows(
                    conn, gate_rows=gate_rows, now=now,
                    mission_id=ctx.mission_id, mission_outcome="report_failed")
                missions_store.finish_improve_mission(
                    conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
                    slot_key=ctx.slot_key, mission_status="completed",
                    run_result=None, now=now,
                    report_state="failed",
                    backlog_transition=(
                        {"backlog_id": backlog_id, "status": "observation",
                         "last_result":
                             f"report_failed:{safe_error_text(exc)}"}
                        if backlog_id is not None else None),
                    commit=False)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            self._settle_ledger_after_commit(
                conn, ctx=ctx, ledger_ids=ledger_ids, outcome="report_failed", now=now)
            return
        final_path = self._final_report_path(
            reports_dir, mission_id=ctx.mission_id, now=now)

        # [profitability-floor] T1 Step 1-3 (codex I6): outcome を
        # branch-local に 1 つ決めて gate 行・ledger 行・settle の 3 箇所
        # 全てへ同じ値を渡す (分裂させない)。
        outcome = mission_outcome
        conn.execute("BEGIN IMMEDIATE")
        ledger_ids = None
        try:
            ledger_ids = self._persist_ledger_in_tx(
                conn, ctx=ctx, now=now, mission_outcome=outcome)
            self._persist_gate_rows(
                conn, gate_rows=gate_rows, now=now,
                mission_id=ctx.mission_id, mission_outcome=outcome)
            # [unprofitable-note-hygiene] 設計書 v2.0 §2-2/§3: フロア不合格
            # (`mission_outcome=="unprofitable"`) のときだけ、この mission
            # が起票した全行 (`origin_mission_id=ctx.mission_id` — INSERT
            # 時に書かれた専用列、既存行の再利用は含まれない) の
            # `origin_outcome` を `'unprofitable'` にする。`last_result`/
            # `idea`/`idea_norm`/`status` には一切触れない — 専用列なので
            # 他の書き手 (終端・人間コマンド・note 昇格) と衝突しない
            # (CAS 不要、v1.x の `AND last_result IS NULL` は撤回)。同じ
            # tx 内 — 後段 (`finish_improve_mission` の rollback を含む)
            # が失敗すればこの UPDATE も一緒に rollback する。当該 mission
            # の選択行 (`backlog_id`) 自身が起票行でも、`origin_outcome`
            # と `last_result` は別列なので衝突しない (N11)。
            if mission_outcome == "unprofitable":
                conn.execute(
                    "UPDATE improvement_backlog SET origin_outcome='unprofitable' "
                    "WHERE origin_mission_id=?", (ctx.mission_id,))
            missions_store.finish_improve_mission(
                conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
                slot_key=ctx.slot_key, mission_status="completed",
                run_result="report", now=now,
                report_path=str(final_path), report_state="prepared",
                backlog_transition=(
                    {"backlog_id": backlog_id, "status": "observation",
                     "last_result": reason}
                    if backlog_id is not None else None),
                commit=False)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        self._settle_ledger_after_commit(
            conn, ctx=ctx, ledger_ids=ledger_ids, outcome=outcome, now=now)
        self._publish_report(conn, run_id=ctx.run_id, part_path=part_path,
                             final_path=final_path, now=now)
