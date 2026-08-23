"""ImproveLoop — 改善 Mission の三相 (prepare/run/commit)
(設計書 §4、プラン §8.1-6/11/16/23/24/40/42/43)。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from agentic_fx.activity import Category
from agentic_fx.backtest import holdout  # precheck 2026-08-22 wave2: 型6#5 —
    # module-level で import する (関数内 import だと monkeypatch.setattr
    # ("agentic_fx.loops.improve_loop.holdout.run_in_sample", ...) が
    # 属性未定義で ImportError になり、足しても sys.modules から取り直す
    # ため効かない)
from agentic_fx.loops.improve_context import build_improve_context
from agentic_fx.loops.improve_run_context import ImproveRunContext
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.loops.summary import IMPROVE_OUTPUT_SCHEMA  # precheck 2026-08-22 wave2: T10-B12
from agentic_fx.runners.base import Mission
from agentic_fx.store import improve_runs as improve_runs_store
from agentic_fx.store import improve_waves
from agentic_fx.store import missions as missions_store

if TYPE_CHECKING:
    from agentic_fx.activity import ActivityLog
    from agentic_fx.config import Settings
    from agentic_fx.core.contracts import Clock
    from agentic_fx.runners.worker_runner import WorkerRunner
    from agentic_fx.store.rag import Rag  # precheck 2026-08-22 wave2: T10-B10

_log = logging.getLogger("agentic_fx.improve_loop")


class ImproveLoop:
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
        finally:
            pass  # write 接続は commit 相 (10.11 節) の同一接続を使い回す —
                   # ここで close しない (呼び出し元が prepare→run→commit を
                   # 通して所有する。10.9 節で最終形に確定する)

        allowed_ids = self._compute_partition_hint(conn, slot_key)
        staging_dir, source_snapshot_dir = self._materialize_workspace(
            mission_id, allowed_ids)
        ledger = ImproveRpcLedger(
            rpc_timeout_sec_by_kind={
                "run_backtest": self._settings.improve.backtest_rpc_timeout_sec,
                "analyze_corr": self._settings.improve.backtest_rpc_timeout_sec})
        rpc_handlers = self._build_rpc_handlers(ledger, staging_dir=staging_dir)
        ctx = ImproveRunContext(
            mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
            source_snapshot_dir=source_snapshot_dir,
            allowed_backlog_ids=allowed_ids, slot_key=slot_key, ledger=ledger,
            rpc_handlers=rpc_handlers)

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
        conn.close()
        return mission, ctx, runner

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
            if not items:
                return "(バックログなし)"
            header = ("| id | idea | status | attempts | assigned |\n"
                      "|---|---|---|---|---|\n")
            return header + "\n".join(
                f"| {i.get('id')} | {i.get('idea')} | {i.get('status')} | "
                f"{i.get('attempts')} | {i.get('assigned', '')} |"
                for i in items)

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
            "backlog_table": _backlog_table(bl["items"]),
            "user_policy_tail": policy["tail"],
            "plugin_name_pattern": refs["plugin_name_pattern"],
            "plugin_contract_summary": refs["plugin_contract_summary"],
            "staging_dir": str(ctx.staging_dir),
            "source_snapshot_dir": str(ctx.source_snapshot_dir),
        }
        template_path = (Path(__file__).resolve().parent / "prompts"
                         / "improve_mission.md")
        return template_path.read_text().format(**render_map)

    def _compute_partition_hint(self, conn, slot_key) -> frozenset[int] | None:
        raise NotImplementedError  # 10.9 節

    def _materialize_workspace(self, mission_id, allowed_ids):
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
        raise NotImplementedError  # 10.3 節

    def _build_rpc_handlers(self, ledger, *, staging_dir: Path):
        raise NotImplementedError  # Task 7 依存分。10.9 節 Step 11 で完全実装する

    def _build_mission_tools(self, *, staging_dir, source_snapshot_dir,
                             ledger, rpc_handlers):
        raise NotImplementedError  # precheck 2026-08-22 wave2: T10-B12/B17
                                    # 10.9 節 Step 12 で完全実装する

    def _build_worker_runner(self, ctx, *,
                             on_ready: Callable[[dict], None] | None = None):
        raise NotImplementedError  # Task 1 (C3) 依存。10.9 節 Step 7 で
                                    # WorkerRunner(..., run_context=ctx,
                                    # on_ready=on_ready) として完成
                                    # (precheck 2026-08-22 wave2: T10-B2/R-D1)

    def commit(self, *, mission, ctx, result, now):
        raise NotImplementedError  # 10.4〜10.11 節
