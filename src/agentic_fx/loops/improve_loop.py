"""ImproveLoop — 改善 Mission の三相 (prepare/run/commit)
(設計書 §4、プラン §8.1-6/11/16/23/24/40/42/43)。"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import stat
import threading
from dataclasses import dataclass
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
from agentic_fx.plugin.gate_pytest import (
    CandidateSnapshotError, check_candidate_snapshot, hashes_of,
    run_gate_pytest,
)
from agentic_fx.plugin.sandbox import SandboxError, check_source
from agentic_fx.plugin.strategy_gate import evaluate_strategy_adoption_gate
from agentic_fx.runners.base import Mission
from agentic_fx.store import backlog as backlog_store
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

_PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class _InspectionVerdict:  # 新規命名
    ok: bool
    reason: str = ""
    out_of_partition: bool = False
    risk_gate_unsupported: bool = False


@dataclass(frozen=True)
class _SelectionOutcome:  # 新規命名
    won: bool
    backlog_id: int | None


@dataclass(frozen=True)
class _PluginGateVerdict:  # 新規命名
    passed: bool
    reason: str = ""
    content_hash: str | None = None
    artifact_hash: str | None = None


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

    コピー完了後に 3 本から再計算した artifact_hash を `meta.artifact_hash`
    と照合する — 不一致ならコピー中の版切替 (live 差し替え) か 3 本混成を
    示すので `ValueError` で fail closed にする。
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
            os.chmod(os.path.join(dirpath, fname), 0o400)
        os.chmod(dirpath, 0o500)


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
        staging_dir = self._root / f"improve-staging-{mission_id}"
        staging_dir.mkdir(parents=True, exist_ok=True)

        source_snapshot_root = staging_dir / "_snapshot_src"

        # copy_examples_snapshot を先に実行し、その後 copy_source_snapshot を実行する
        # (R-D3 による権限管理: _snapshot_src が 0o500 になる前に _examples も配置)
        examples_root = Path(self._settings.data_root) / "docs" / "examples" / "plugins"
        copy_examples_snapshot(examples_root, dest_root=source_snapshot_root / "_examples")

        # Discover 済みの plugin metas をここでコピーする実装は後続節に任せる (10.3 節の stub)
        # とりあえず空の出所を用意する
        copy_source_snapshot([], dest_root=source_snapshot_root,
                            plugin_lock=threading.Lock())

        return staging_dir, source_snapshot_root

    _EVAL_SOURCE = "dukascopy"

    def _build_rpc_handlers(self, ledger: "ImproveRpcLedger", *,
                            staging_dir: Path) -> dict:
        """10.9 Step 11: run_backtest/analyze_corr の親側実装。"""
        from agentic_fx.backtest.analysis import analyze_for_agent
        from agentic_fx.plugin import loader as plugin_loader
        from agentic_fx.plugin import strategy_adapter

        def run_backtest_handler(args: dict) -> dict:
            candidate_dir = staging_dir / args["name"]
            meta = plugin_loader._discover_one(candidate_dir, args["name"])
            if meta is None or meta.kind != "strategy":
                raise ValueError(
                    f"run_backtest requires a strategy candidate: "
                    f"{args['name']!r}")
            try:
                conn = self._db_readonly_conn_factory()
                captured: list[dict] = []
                intent_source = strategy_adapter.build_intent_source(
                    meta, conn=conn, pair=args["pair"], source=self._EVAL_SOURCE,
                    settings=self._settings)
                try:
                    holdout.run_in_sample(
                        self._settings, history_conn=conn, symbol=args["pair"],
                        source=self._EVAL_SOURCE, intent_source=intent_source,
                        eval_timeframe=meta.timeframe,
                        plugin_ref=f"plugins/_staging/{staging_dir.name}/"
                                   f"{args['name']}",
                        content_hash=meta.content_hash, kind="strategy",
                        now=self._clock.now(), record_fn=captured.append)
                finally:
                    intent_source.close()
                    conn.close()
            except Exception:
                _log.exception("run_backtest_handler failed for %r",
                               args.get("name"))
                return {"error": "backtest_failed"}
            save_kwargs = captured[0]
            return {**save_kwargs, "trial_count": 1}

        def analyze_corr_handler(args: dict) -> dict:
            try:
                conn = self._db_readonly_conn_factory()
                try:
                    return analyze_for_agent(
                        conn, self._settings, args, now=self._clock.now(),
                        persist=False)
                finally:
                    conn.close()
            except Exception:
                _log.exception("analyze_corr_handler failed")
                return {"error": "analyze_failed"}

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
        from agentic_fx.store.rag import Rag

        return WorkerRunner(
            root=self._root, settings=self._settings, clock=self._clock,
            rag=Rag(self._db_readonly_conn_factory), worker_profile="improve",
            run_context=ctx, on_ready=on_ready)

    def _freeze_ledger(self, ctx: ImproveRunContext) -> None:
        ctx.ledger.freeze()

    def _inspect_output(self, output: dict, ctx: ImproveRunContext,
                        conn=None) -> _InspectionVerdict:
        artifact = output.get("artifact", {})
        atype = artifact.get("type")
        out_of_partition = False
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
                out_of_partition = True
                self._activity.write(
                    Category.IMPROVE, "out_of_partition",
                    f"mission={ctx.mission_id} backlog_id={selected_id}")

        if atype == "plugin":
            name = artifact.get("name", "")
            if not _PLUGIN_NAME_RE.match(name):
                return _InspectionVerdict(
                    ok=False, reason=f"artifact.name {name!r} is not in "
                                    "canonical form", out_of_partition=out_of_partition)
            # staging_dir/<name> の dirfd+lstat 検査は 10.6 節 (plugin ゲート)
            # で実装する — ここでは name 正規形のみ (手順1の範囲)。
            return _InspectionVerdict(ok=True, out_of_partition=out_of_partition)

        if atype == "report" and artifact.get("proposal_kind") == "risk_gate":
            return _InspectionVerdict(
                ok=True, out_of_partition=out_of_partition,
                risk_gate_unsupported=True)

        return _InspectionVerdict(ok=True, out_of_partition=out_of_partition)

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
                # precheck 2026-08-22 wave2: T10-M11 — SQL 側 lower(trim(idea))
                # と同じ「前後空白のみ除去」に揃える (内部空白は畳まない)
                return idea.strip().lower()

            for d in output.get("discoveries", []):
                idea_norm = _norm(d["idea"])
                dup = conn.execute(
                    "SELECT id FROM improvement_backlog WHERE "
                    "lower(trim(idea))=?", (idea_norm,)).fetchone()
                if dup is not None:
                    continue
                if inserted >= limit:
                    dropped += 1
                    continue
                conn.execute(
                    "INSERT INTO improvement_backlog (idea, source, status, "
                    "created_at, updated_at) VALUES (?,?,'open',?,?)",
                    (d["idea"], d.get("source", "agent"), now.isoformat(),
                     now.isoformat()))
                inserted += 1
            if dropped:
                self._activity.write(
                    Category.IMPROVE, "backlog_limit_exceeded",
                    f"mission={ctx.mission_id} dropped={dropped}")

            selected = output.get("selected", {})
            backlog_id = selected.get("backlog_id")
            if backlog_id is None:
                selected_idea = selected.get("idea", "")
                idea_norm = _norm(selected_idea)
                row = conn.execute(
                    "SELECT id FROM improvement_backlog WHERE "
                    "lower(trim(idea))=? AND status IN ('open','observation')",
                    (idea_norm,)).fetchone()
                if row is None:
                    # precheck 2026-08-22 wave2: T10-B5 — discoveries にも
                    # 既存 backlog にも無い「新規 idea を選択」した場合は、
                    # ここで improvement_backlog へ INSERT してから選ぶ
                    # (docstring が謳う契約。test_new_idea_selected_creates_
                    # and_binds_in_same_tx が要求する)。空文字は選択なしとして
                    # 扱う (fail closed — 実在しない idea を勝者にしない)。
                    if not selected_idea.strip():
                        conn.commit()
                        return _SelectionOutcome(won=False, backlog_id=None)
                    cur = conn.execute(
                        "INSERT INTO improvement_backlog (idea, source, status, "
                        "created_at, updated_at) VALUES (?,?,'open',?,?)",
                        (selected_idea, selected.get("source", "agent"),
                         now.isoformat(), now.isoformat()))
                    backlog_id = cur.lastrowid
                else:
                    backlog_id = row["id"]

            won = backlog_store.select_for_mission(
                conn, backlog_id, now=now, commit=False)
            if won:
                improve_runs_store.bind_backlog(
                    conn, ctx.run_id, backlog_id, commit=False)
            conn.commit()
            return _SelectionOutcome(won=won, backlog_id=backlog_id if won else None)
        except BaseException:
            conn.rollback()
            raise

    def _run_plugin_gate(self, plugin_dir: Path, *,
                         name: str) -> _PluginGateVerdict:
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

        try:
            result = run_gate_pytest(plugin_dir, settings=self._settings)
        except RuntimeError as exc:
            # M7: Landlock 不可 (run_gate_pytest の fail-closed RuntimeError,
            # 設計書 §4.2-3d) は commit() 全体を例外で抜けさせず gate 不合格に倒す
            return _PluginGateVerdict(passed=False, reason=str(exc))
        if not result.passed:
            return _PluginGateVerdict(
                passed=False, reason=f"pytest failed: {result.stdout_tail}")

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
                           now, meta, kind="strategy", record_fn=None):
        return evaluate_strategy_adoption_gate(
            conn, name=name, pairs=pairs, timeframe=timeframe,
            content_hash=content_hash, now=now, settings=self._settings,
            meta=meta, kind=kind, record_fn=record_fn)

    def _build_approval_payload(self, conn, *, name, kind, content_hash,
                                artifact_hash, ctx_ledger, mission_id,
                                backlog_id, candidate_origin, candidate_path,
                                gate_metrics, output, now) -> dict:
        entries = ctx_ledger.entries()
        analysis_entries = [e for e in entries if e["kind"] == "analyze_corr"]
        backtest_entries = [e for e in entries if e["kind"] == "run_backtest"]
        trial_count = sum(e["trial_count"] for e in entries)
        return {
            "name": name, "kind": kind,
            "candidate_origin": candidate_origin,
            "candidate_path": candidate_path,
            "content_hash": content_hash, "artifact_hash": artifact_hash,
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
        }

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

    def _publish_report(self, conn, *, run_id: int, part_path: Path,
                        final_path: Path, now: datetime) -> None:
        """report outbox — rename 後に published へ遷移、fsync。"""
        try:
            if final_path.exists():
                raise FileExistsError(final_path)
            os.rename(part_path, final_path)
        except FileExistsError:
            self._fail_report(conn, run_id=run_id, now=now, reason="rename_conflict")
            return
        except OSError as exc:
            self._fail_report(conn, run_id=run_id, now=now,
                              reason=f"rename_failed:{exc}")
            return
        self._fsync_dir(final_path.parent)
        self._fsync_dir(part_path.parent)
        conn.execute(
            "UPDATE improvement_runs SET report_state='published' "
            "WHERE id=?", (run_id,))
        conn.commit()

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
                conn.execute(
                    "UPDATE improvement_backlog SET status='observation', "
                    "last_result=?, updated_at=? WHERE id=? AND status='done'",
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
        "source", "period", "metrics", "settings_hash", "core_commit",
        "initial_balance", "now")
    _ANALYSIS_ROW_KEYS = ("params", "trial_count", "source")

    def _persist_ledger_rows(self, conn, *, ledger_entries, now) -> list[int]:
        """10.10 Step 3: 台帳から analysis_runs/backtest_runs へ永続化。"""
        from agentic_fx.store import analysis_runs as analysis_runs_store
        from agentic_fx.store import backtest_runs as backtest_runs_store

        analysis_run_ids: list[int] = []
        for entry in ledger_entries:
            summary = entry["result_summary"]
            if "error" in summary:
                self._activity.write(
                    Category.IMPROVE, "ledger_entry_skipped_error",
                    f"kind={entry['kind']} error={summary['error']!r}")
                continue
            if entry["kind"] == "run_backtest":
                row_kwargs = {k: summary[k] for k in self._BACKTEST_ROW_KEYS}
                backtest_runs_store.save_harness_run(
                    conn, commit=False, variant="candidate", **row_kwargs)
            elif entry["kind"] == "analyze_corr":
                row_kwargs = {k: summary[k] for k in self._ANALYSIS_ROW_KEYS}
                run_id = analysis_runs_store.save(
                    conn, commit=False, now=now, **row_kwargs)
                analysis_run_ids.append(run_id)
        return analysis_run_ids

    def _persist_gate_rows(self, conn, *, gate_rows, now) -> None:
        """10.10 Step 3: 親ゲート行を永続化。"""
        from agentic_fx.store import backtest_runs as backtest_runs_store

        for row in gate_rows:
            backtest_runs_store.save_harness_run(conn, commit=False, **row)

    def _compensate_tx2_failure(self, conn, *, mission_id, run_id,
                                backlog_id, slot_key, now) -> None:
        """10.10 Step 3: Tx-2 失敗時の補償 tx。"""
        conn.execute("BEGIN IMMEDIATE")
        try:
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
        self._activity.write(
            Category.IMPROVE, "improve_commit_failed",
            f"mission_id={mission_id} run_id={run_id}")

    def _finalize_success(self, conn, *, mission_id, run_id, backlog_id,
                          slot_key, approval_payload, now,
                          ledger_entries=(), gate_rows=()) -> None:
        """10.10 Step 3: Tx-2 本体 (台帳→gate→approval→finish)。"""
        from agentic_fx.store import approvals as approvals_store
        from agentic_fx.store import missions as missions_store

        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                analysis_run_ids = self._persist_ledger_rows(
                    conn, ledger_entries=ledger_entries, now=now)
                self._persist_gate_rows(
                    conn, gate_rows=gate_rows, now=now)
                approval_payload = dict(approval_payload)
                approval_payload["analysis_run_ids"] = analysis_run_ids
                approval_payload["analysis_call_count"] = sum(
                    1 for e in ledger_entries if e["kind"] == "analyze_corr")
                approval_payload["trial_count"] = sum(
                    e["trial_count"] for e in ledger_entries)
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
        except Exception:
            _log.exception("Tx-2 failed for mission_id=%s — running "
                           "compensation", mission_id)
            try:
                self._compensate_tx2_failure(
                    conn, mission_id=mission_id, run_id=run_id,
                    backlog_id=backlog_id, slot_key=slot_key, now=now)
            except Exception:
                _log.exception(
                    "compensation itself failed for mission_id=%s — mission "
                    "stays in a non-terminal state, will be picked up by "
                    "startup reconcile", mission_id)
                self._activity.write(
                    Category.IMPROVE, "tx2_compensation_failed",
                    f"mission_id={mission_id} run_id={run_id}")

    def commit(self, *, mission, ctx, result, now) -> None:
        owns_conn = False
        conn = getattr(self, "_conn_for_test", None)
        if conn is None:
            conn = self._db_write_conn_factory()
            owns_conn = True
        try:
            self._freeze_ledger(ctx)                                  # 手順0

            if result.status != "completed":
                self._finalize_failed_mission(conn, ctx=ctx, result=result, now=now)
                return

            output = result.output or {}
            verdict = self._inspect_output(output, ctx, conn=conn)     # 手順1
            if not verdict.ok:
                self._finalize_output_invalid(conn, ctx=ctx, reason=verdict.reason, now=now)
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

            if atype == "plugin":
                candidate_dir = ctx.staging_dir / artifact["name"]
                gate_verdict = self._run_plugin_gate(                  # 手順3
                    candidate_dir, name=artifact["name"])
                if not gate_verdict.passed:
                    self._finalize_gate_failed(
                        conn, ctx=ctx, backlog_id=selection.backlog_id,
                        reason=gate_verdict.reason, now=now)
                    return

                kind = self._read_candidate_kind(candidate_dir)
                if kind == "strategy":
                    from agentic_fx.plugin import loader as plugin_loader
                    candidate_meta = plugin_loader._discover_one(
                        candidate_dir, artifact["name"])
                    strategy_verdict = self._run_strategy_gate(         # 手順4
                        conn, name=artifact["name"],
                        pairs=self._read_candidate_pairs(candidate_dir),
                        timeframe=self._read_candidate_timeframe(candidate_dir),
                        content_hash=gate_verdict.content_hash, now=now,
                        meta=candidate_meta, kind=kind,
                        record_fn=gate_rows.append)
                    if not strategy_verdict.evaluable:
                        self._finalize_gate_failed(
                            conn, ctx=ctx, backlog_id=selection.backlog_id,
                            reason=strategy_verdict.observation_reason, now=now)
                        return
                    gate_metrics["baseline"] = strategy_verdict.baseline_row

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

            report_path = None
            if approval_payload is None:
                report_path = self._prepare_report_if_applicable(       # 手順6
                    conn, ctx=ctx, artifact=artifact, output=output, now=now)

            if approval_payload is not None:
                self._finalize_success(                                 # 手順7-9
                    conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
                    backlog_id=selection.backlog_id, slot_key=ctx.slot_key,
                    approval_payload=approval_payload, now=now,
                    ledger_entries=tuple(ctx.ledger.entries()),
                    gate_rows=tuple(gate_rows))
            else:
                self._finalize_report_or_observation(
                    conn, ctx=ctx, backlog_id=selection.backlog_id,
                    report_path=report_path, now=now)
        finally:
            if owns_conn:
                conn.close()
            try:
                ctx.ledger.mark_persisted()
            except RuntimeError:
                pass

    def _prepare_report_if_applicable(self, conn, *, ctx, artifact, output,
                                      now) -> str | None:
        """`artifact.type=='report'` かつ `proposal_kind != 'risk_gate'` の
        ときだけ本文を `.tmp/*.part` へ書き、Tx-2 で `report_state=
        'prepared'` + 予定パスを書く。"""
        if artifact.get("type") != "report":
            return None
        if artifact.get("proposal_kind") == "risk_gate":
            return None
        reports_dir = self._root / "data" / "improve_reports"
        (reports_dir / ".tmp").mkdir(parents=True, exist_ok=True)
        body_md = artifact.get("body_md", "")
        part_path = self._write_report_part(
            reports_dir, mission_id=ctx.mission_id, body_md=body_md)
        final_path = reports_dir / f"improve-{ctx.mission_id}.md"
        conn.execute(
            "UPDATE improvement_runs SET report_state='prepared', "
            "report_path=? WHERE id=?", (str(final_path), ctx.run_id))
        return str(final_path)

    def _finalize_report_or_observation(self, conn, *, ctx, backlog_id,
                                        report_path, now) -> None:
        """承認申請を出さない経路の Tx-2 + finish。"""
        from agentic_fx.store import missions as missions_store
        conn.execute("BEGIN IMMEDIATE")
        try:
            missions_store.finish_improve_mission(
                conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
                slot_key=ctx.slot_key, mission_status="completed",
                run_result=("report" if report_path is not None else None),
                now=now,
                report_path=report_path,
                report_state=("prepared" if report_path is not None else "none"),
                backlog_transition=(
                    {"backlog_id": backlog_id, "status": "observation",
                     "last_result": "unsupported_in_plan10"}
                    if report_path is None and backlog_id is not None
                    else ({"backlog_id": backlog_id, "status": "done",
                          "last_result": "reported"}
                          if backlog_id is not None else None)),
                commit=False)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if report_path is not None:
            reports_dir = self._root / "data" / "improve_reports"
            part_path = reports_dir / ".tmp" / f"improve-{ctx.mission_id}.md.part"
            self._publish_report(conn, run_id=ctx.run_id, part_path=part_path,
                                 final_path=Path(report_path), now=now)

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

    def _finalize_failed_mission(self, conn, *, ctx, result, now) -> None:
        """timeout/failed/max_turns 処理。プラン実装時に申し送り条件参照。"""
        from agentic_fx.store import missions as missions_store
        ctx.ledger.mark_discarded()
        missions_store.finish_improve_mission(
            conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
            slot_key=ctx.slot_key, mission_status="failed",
            run_result="failed", now=now, commit=True)

    def _finalize_output_invalid(self, conn, *, ctx, reason, now) -> None:
        """手順1 不合格。プラン実装時に申し送り条件参照。"""
        from agentic_fx.store import missions as missions_store
        ctx.ledger.mark_discarded()
        missions_store.finish_improve_mission(
            conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
            slot_key=ctx.slot_key, mission_status="failed",
            run_result="failed", now=now, commit=True)

    def _finalize_loser(self, conn, *, ctx, output, now) -> None:
        """敗者経路。プラン実装時に申し送り条件参照。"""
        from agentic_fx.store import missions as missions_store
        conn.execute("BEGIN IMMEDIATE")
        try:
            missions_store.finish_improve_mission(
                conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
                slot_key=ctx.slot_key, mission_status="completed",
                run_result="duplicate", now=now, commit=False)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def _finalize_gate_failed(self, conn, *, ctx, backlog_id, reason, now) -> None:
        """ゲート不合格。プラン実装時に申し送り条件参照。"""
        from agentic_fx.store import missions as missions_store
        ctx.ledger.mark_discarded()
        missions_store.finish_improve_mission(
            conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
            slot_key=ctx.slot_key, mission_status="completed",
            run_result="observation", now=now, commit=True)
