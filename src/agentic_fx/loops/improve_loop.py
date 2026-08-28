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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

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
from agentic_fx.loops.summary import IMPROVE_OUTPUT_SCHEMA  # precheck 2026-08-22 wave2: T10-B12
from agentic_fx.plugin.gate_pytest import (
    CandidateSnapshotError, check_candidate_snapshot, hashes_of,
    run_gate_pytest,
)
from agentic_fx.plugin.sandbox import SandboxError, check_source
from agentic_fx.plugin.strategy_gate import evaluate_strategy_adoption_gate
from agentic_fx.runners.base import Mission
from agentic_fx.tools.plugin_loader import approved_plugins
from agentic_fx.store import approvals as approvals_store
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
            os.chmod(os.path.join(dirpath, fname), 0o400)
        os.chmod(dirpath, 0o500)


# Task 12 Step3: `_prepare_report_if_applicable` が OSError を捕まえて
# その場で Mission を終端させたことを `commit()` に伝えるための sentinel
# (`None` は「report 対象外 (indicator/plugin や risk_gate)」の既存の
# 正常値なので流用できない)。
_REPORT_WRITE_FAILED = object()


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
            staging_dir, source_snapshot_dir = self._materialize_workspace(
                conn, mission_id, allowed_ids)
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
        return frozenset(i for i in open_ids if i % expected == k)

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
        metas = approved_plugins(conn, plugins_dir)
        copy_source_snapshot(metas, dest_root=source_snapshot_root,
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
        return WorkerRunner(
            root=self._root, settings=self._settings, clock=self._clock,
            rag=self._rag, worker_profile="improve",
            run_context=ctx, on_ready=on_ready, rpc_handlers=ctx.rpc_handlers)

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
                        final_path: Path, now: datetime) -> None:
        """report outbox — rename 後に published へ遷移、fsync。

        F-6 是正 (検収 task12、設計書 §4.2 L389): 最終名への rename は
        `renameat2(RENAME_NOREPLACE)` で行う (`_rename_no_replace` 参照)。
        非対応環境 (非 Linux・古いカーネル) のみ、旧来の
        exists+rename (TOCTOU 近似) にフォールバックする。"""
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
        "source", "period", "metrics", "settings_hash", "core_commit",
        "initial_balance", "now")
    _ANALYSIS_ROW_KEYS = ("params", "trial_count", "source")

    def _persist_ledger_rows(self, conn, *, ledger_entries, now,
                             mission_id: int | None = None) -> list[int]:
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
                    conn, commit=False, variant="candidate",
                    mission_id=mission_id, **row_kwargs)
            elif entry["kind"] == "analyze_corr":
                row_kwargs = {k: summary[k] for k in self._ANALYSIS_ROW_KEYS}
                run_id = analysis_runs_store.save(
                    conn, commit=False, now=now, mission_id=mission_id,
                    **row_kwargs)
                analysis_run_ids.append(run_id)
        return analysis_run_ids

    def _persist_gate_rows(self, conn, *, gate_rows, now,
                           mission_id: int | None = None) -> None:
        """10.10 Step 3: 親ゲート行を永続化。"""
        from agentic_fx.store import backtest_runs as backtest_runs_store

        for row in gate_rows:
            backtest_runs_store.save_harness_run(
                conn, commit=False, mission_id=mission_id, **row)

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
                # precheck 2026-08-23 wave3: RW4 — mission_id (= ctx.mission_id、
                # commit() が渡すローカル引数) を台帳/親ゲート両方の永続化へ
                # 転送する。Task 12 の `WHERE mission_id=?` assert が読む列。
                analysis_run_ids = self._persist_ledger_rows(
                    conn, ledger_entries=ledger_entries, now=now,
                    mission_id=mission_id)
                self._persist_gate_rows(
                    conn, gate_rows=gate_rows, now=now, mission_id=mission_id)
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
        try:
            self._freeze_ledger(ctx)                                  # 手順0

            if result.status != "completed":
                self._finalize_failed_mission(
                    conn, ctx=ctx, result=result, now=now,
                    slot_terminalize=slot_terminalize)
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
                        reason=f"gate_failed:{gate_verdict.reason}", now=now)
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
                    conn, ctx=ctx, artifact=artifact, output=output, now=now,
                    backlog_id=selection.backlog_id)
                if report_path is _REPORT_WRITE_FAILED:
                    # OSError を _prepare_report_if_applicable 自身が Tx-2
                    # で終端済み — 二重終端を避けてここで抜ける。
                    return

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
            try:
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
            return _REPORT_WRITE_FAILED
        final_path = self._final_report_path(
            reports_dir, mission_id=ctx.mission_id, now=now)
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
                     # D-15 是正: 設計書 §4.3 状態表の逐語は
                     # `unsupported_in_plan10:risk_gate` (reason 接尾辞を
                     # 含む) — 現状この分岐は risk_gate 提案の未対応にしか
                     # 到達しないため、そのまま付与する。
                     "last_result": "unsupported_in_plan10:risk_gate"}
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
        for dirpath, dirnames, filenames in os.walk(ctx.staging_dir):
            for fname in filenames:
                os.chmod(os.path.join(dirpath, fname), 0o600)
            os.chmod(dirpath, 0o700)
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
        無条件 UPDATE が巻き戻した `reserved` を潰すのを防ぐ)。"""
        ctx.ledger.mark_discarded()
        self._delete_staging(ctx)
        conn.execute("BEGIN IMMEDIATE")
        try:
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

    def _finalize_output_invalid(self, conn, *, ctx, reason, now) -> None:
        """§4.2 手順1 不合格 (schema 不整合・`artifact.name` 非正規形・
        `staging_dir/<name>` 異常等) → Mission `failed`、
        `improvement_runs.result` は NULL のまま、staging 削除、
        台帳 `DISCARDED`。backlog は Tx-1 未到達のため遷移なし。"""
        ctx.ledger.mark_discarded()
        self._delete_staging(ctx)
        self._activity.write(
            Category.IMPROVE, "output_invalid",
            f"mission={ctx.mission_id} reason={reason}")
        conn.execute("BEGIN IMMEDIATE")
        try:
            missions_store.finish_improve_mission(
                conn, mission_id=ctx.mission_id, run_id=ctx.run_id,
                slot_key=ctx.slot_key, mission_status="failed",
                run_result=None, now=now, backlog_transition=None,
                commit=False)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def _finalize_loser(self, conn, *, ctx, output, now) -> None:
        """§4.2 手順2 敗者経路 (Tx-1 の選択 CAS で rowcount=0):
        backlog は一切遷移しない (自分は選択に負けただけで、勝者側の状態を
        壊さない — §4.3「遷移は起きない」)。親が「重複のため見送り」の
        自動レポートを 10.9 節の outbox ヘルパ (`_write_report_part`/
        `_publish_report`) で書き、run を `result='report'` で終端する
        (プラン10 Task10-11 申し送り)。staging は残す候補が無いため削除、
        台帳はこの Mission の RPC 結果を一切永続化しないため `DISCARDED`。"""
        ctx.ledger.mark_discarded()
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
            try:
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
            return
        final_path = self._final_report_path(
            reports_dir, mission_id=ctx.mission_id, now=now)

        conn.execute("BEGIN IMMEDIATE")
        try:
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
        self._publish_report(conn, run_id=ctx.run_id, part_path=part_path,
                             final_path=final_path, now=now)

    def _finalize_gate_failed(self, conn, *, ctx, backlog_id, reason, now) -> None:
        """§4.2 手順3/4 不合格・評価不能 → §4.3: backlog を `observation`
        (`last_result` は呼び出し元が組み立てた `reason` そのまま —
        `commit()` が `gate_failed:<...>`/`insufficient_trades:<n>` の形で
        渡す)。承認申請は出さないため mission は `completed` で終端
        (取引は止めない — R8)。staging を削除し、台帳は `DISCARDED`。

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
        ctx.ledger.mark_discarded()
        self._delete_staging(ctx)
        body_md = (
            f"# Improve Mission {ctx.mission_id} — gate failed\n\n"
            f"reason: `{reason}`\n\nNo approval request was produced by "
            "this mission.\n")
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
            try:
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
            return
        final_path = self._final_report_path(
            reports_dir, mission_id=ctx.mission_id, now=now)

        conn.execute("BEGIN IMMEDIATE")
        try:
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
        self._publish_report(conn, run_id=ctx.run_id, part_path=part_path,
                             final_path=final_path, now=now)
