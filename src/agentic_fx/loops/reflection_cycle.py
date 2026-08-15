"""reflection cycle — クローズ済みトレードの振り返り生成 (SQLite + Chroma 二重保存)。

構造 (プラン8 三相再構成 — 設計書 §3.1):
- prepare (core_lock 保持) → run (core_lock 非保持) → commit-core
  (core_lock 保持: finalize) → commit-post。commit-post の内訳は一様
  ではない — **RAG 書込 (`Rag.add_reflection`) と activity 記録は
  core_lock 非保持** (RAG は Rag 自身の内部 lock に委ねる — Task 9)。

  **ただし委譲はタダではない (レビュー 2 周目)。** Task 16 で
  `_reflection_fn` の外側 lock を外した結果、**RAG の書き手が 2 つ並行
  しうる**ようになった: この `add_reflection` と、scheduler tick 配下の
  `NewsCollector.collect` (`rag.add_news`/`cleanup_news`) である。
  従来は両方とも core_lock 下だったので構造的に排他されていた。
  `Rag._locked()` の待ちは `lock_timeout_sec` (既定 10 秒) で打ち切られ
  `RagUnavailable` になるため、**news の埋め込みが 10 秒を超えると、
  直前に最大 300 秒かけた LLM の出力を捨てて次周期に丸ごとやり直す**
  (自己修復はするが高価)。逆向きには、`collect` が RAG lock 待ちで
  最大 10 秒 **core_lock を保持したまま**止まりうる。
  **実測と対策 (bounded retry か timeout 調整) は Task 20 へ申し送り。**
  **SQLite 書込 (`reflections.save`) のみ core_lock 保持** (conn_core
  への書込のため — Global Constraints)。この非対称を崩して
  `reflections.save` を lock 外へ出すと Global Constraints 違反になる
  (レビュー 1 周目 F3 で指摘・修正)。
- finalize_mission (agentic_fx.loops.mission_finalize): missions.finish の
  CAS 化された共通呼び出し (TradeLoop と共有 — プラン8 Task 16)。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import Settings
from agentic_fx.core.contracts import Clock
from agentic_fx.loops.mission_finalize import finalize_mission
from agentic_fx.loops.mission_watch import MissionWatch
from agentic_fx.loops.prompts_loader import load_prompt
from agentic_fx.runners.base import AgentRunner, Mission, MissionResult
from agentic_fx.store import missions, reflection_attempts, reflections
from agentic_fx.store.rag import Rag

_log = logging.getLogger("agentic_fx.reflection")

_SCHEMA = {"type": "object",
           "properties": {"content": {"type": "string"}},
           "required": ["content"]}


class ReflectionCycle:
    def __init__(self, *, conn: sqlite3.Connection, runner: AgentRunner,
                 rag: Rag, settings: Settings, activity: ActivityLog,
                 clock: Clock, core_lock: threading.RLock,
                 watch: MissionWatch | None = None) -> None:
        self.conn = conn
        self.runner = runner
        self.rag = rag
        self.settings = settings
        self.activity = activity
        self.clock = clock
        self._core_lock = core_lock
        self.watch = watch if watch is not None else MissionWatch()

    def _consume_attempt(self, order_id: int, reason: str | None) -> None:
        """失敗台帳を 1 消費し、上限到達なら abandon activity を書く
        (レビュー 1 周目 F1)。挙動は Task 15 導入時の bump+abandon 3 箇所
        と完全に同じ (この private メソッドはそれを 1 箇所へ集約しただけ)。
        `core_lock` は呼び出し側で保持済みであること前提にはしない —
        ここで取得する (呼び出し site はどこも既に lock 外から呼ぶため)。"""
        with self._core_lock:
            attempts = reflection_attempts.bump(
                self.conn, order_id, now=self.clock.now(), reason=reason)
        if attempts == self.settings.reflection.max_attempts:
            try:
                self.activity.write(
                    Category.AGGREGATE, "reflection_abandoned",
                    f"order_id={order_id} attempts={attempts}",
                    ref_id=str(order_id))
            except Exception:  # noqa: BLE001
                _log.exception(
                    "failed to record reflection_abandoned for #%s", order_id)

    def run_pending(self, max_items: int = 3) -> int:
        """Reflect on closed orders without reflection. Per-item isolation.

        1 回の呼び出しで処理する件数を `max_items` に制限する — core_lock
        保持中の Mission 合成時間を抑え、SL/TP 監視の停止窓を制限するため
        (この docstring は既存のまま — プラン8 三相再構成後もこの制約の
        意図は変わらない。実際の lock 保持範囲は各相ごとに Task 15/16 の
        方針で細分化されている)。
        """
        with self._core_lock:
            rows = self.conn.execute(
                "SELECT o.* FROM orders o "
                "LEFT JOIN reflections r ON r.order_id=o.id "
                # `ORDER BY o.id` は設計書 D1 が明示的に据え置くと決めた条件。
                # 変えないこと (着手前検証 2026-08-15: この削除変異は黒箱テストでは
                # 殺せない — 順序は実行計画依存。規約としてここに残す)。
                "LEFT JOIN reflection_attempts a ON a.order_id=o.id "
                "WHERE o.status='closed' AND r.order_id IS NULL "
                "AND (a.attempts IS NULL OR a.attempts < :max) "
                "ORDER BY o.id LIMIT :limit",
                {"max": self.settings.reflection.max_attempts, "limit": max_items},
            ).fetchall()
        created = 0
        for row in rows:
            try:
                if self._reflect_one(dict(row)):
                    created += 1
            except Exception:  # noqa: BLE001 — per-item isolation
                _log.exception("per-item reflection failed for order #%s",
                               row["id"])
        return created

    def _reflect_one(self, row: dict) -> bool:
        """Run reflection on one closed order (プラン8 三相再構成)。

        prepare (lock) → run (lock 非保持) → commit-core (lock: finalize)
        → commit-post (RAG 書込・activity 記録は lock 不要 — Rag 自身が
        内部 lock を持つ (Task 9)。SQLite 書込 (`reflections.save`) のみ
        lock 保持)。

        **(レビュー 1 周目 F1)** メソッド全体を `try/finally` で覆い、
        `finalized` フラグが立たないまま (= `mid` 発行済みなのに
        `finalize_mission` に一度も到達しないまま) 抜けそうになったら
        fail-closed で finalize する (`TradeLoop._run_once_impl`/
        `_ask_once_impl` と同じ形 — Task 15)。`watch.begin`/`watch.end`
        (計測用の副作用) が例外を出しても mission が `running` のまま
        残らない。"""
        mid: int | None = None
        finalized = False
        try:
            with self._core_lock:
                now = self.clock.now()
                intent = None
                if row["intent_id"]:
                    ir = self.conn.execute(
                        "SELECT payload_json FROM trade_intents WHERE id=?",
                        (row["intent_id"],)).fetchone()
                    intent = json.loads(ir["payload_json"]) if ir else None

                prompt = (load_prompt("reflection") + "\n\n## トレード詳細\n"
                          + json.dumps({"order": {k: row[k] for k in (
                              "id", "pair", "direction", "horizon", "quantity",
                              "avg_fill_price", "close_price", "realized_pnl",
                              "close_reason")},
                              "entry_reasoning": (intent or {}).get("reasoning")},
                              ensure_ascii=False, indent=1))

                mission = Mission(
                    prompt=prompt, tools=[], output_schema=_SCHEMA,
                    max_turns=2,
                    timeout_sec=self.settings.llama_swap.timeout_sec)

                mid = missions.start(
                    self.conn, "reflection",
                    self.settings.runner.trade.backend,
                    self.settings.runner.trade.model, now)

            self.watch.begin(mid, "reflection", mission.timeout_sec)
            try:
                result = self.runner.run(mission)
                if not isinstance(result, MissionResult):
                    result = MissionResult("failed", None, [])
            except Exception:  # noqa: BLE001
                _log.exception("reflection runner raised")
                result = MissionResult("failed", None, [])
            finally:
                # **(レビュー 1 周目 F1)** watch.end 自体の障害はログで
                # 隔離しつつ再送出する。ここで飲み込んで正常系続行すると
                # 「実際は completed した Mission」が下の finalize_mission
                # に real result のまま渡ってしまい、watch 計測の不具合
                # (本質的には無害) が reflection の正常保存を妨げない、
                # という誤った安全側判定になる。再送出することで外側の
                # `finally` (fail-closed: result="failed" 固定) に必ず
                # 落とし、mission を確実に終端させる。
                try:
                    self.watch.end(mid)
                except Exception:  # noqa: BLE001
                    _log.exception("watch.end failed for %s", mid)
                    raise

            with self._core_lock:
                finalize_ok = finalize_mission(self.conn, self.activity,
                                               self.clock, mid, result)
            # **(レビュー 1 周目 F1 の副作用への対処)** `finalized` は
            # 「finalize_mission を試みたか」を表す (TradeLoop と同じ意味 —
            # 外側 finally の二重 finalize 抑止用)。`finalize_ok` (戻り値)
            # とは別物 — `finalize_mission` が `False` (書込み自体が例外で
            # 失敗) を返しても、それは「試みて失敗した」のであって「試みて
            # いない」ではない。ここで区別しないと、外側 finally が
            # 「まだ finalize していない」と誤認して `missions.finish` を
            # 二重に呼んでしまう (実測で発見・修正)。
            finalized = True

            if result.status != "completed":
                # プラン 9 Task 4: reflection Mission の失敗を可視化する。
                # 通知は出さない (資金に直結しないため activity で足りる —
                # 設計書 2026-08-10-context-overflow-diagnosis-design.md
                # §4.5)。
                try:
                    self.activity.write(
                        Category.AGGREGATE, "reflection_mission_failed",
                        f"order_id={row['id']} mission_id={mid} "
                        f"status={result.status}"
                        + (f" — {result.reason}" if result.reason else ""),
                        ref_id=str(row["id"]))
                except Exception:  # noqa: BLE001 — 記録の失敗で reflection 経路を止めない
                    _log.exception(
                        "failed to record reflection_mission_failed for #%s",
                        row["id"])
                # ↓ここから挿入 (プラン 9 Task 15、設計書 D1)
                # 失敗を台帳へ刻む — `_consume_attempt` (レビュー 1 周目 F1)。
                # ここは **Mission 自体の失敗**であり、`finalize_ok` の成否に
                # 関係なく消費する (F2: completed かつ finalize 失敗の場合の
                # み消費しない — 下の複合ガードはそのまま温存する)。
                self._consume_attempt(row["id"], result.reason)
                # ↑ここまで挿入

            if result.status != "completed" or not finalize_ok:
                # finish 失敗時は監査未確定 (missions 行が running のまま) なので
                # reflection も保存しない。SQLite マーカー (reflections 行) が
                # 無いので次周期の run_pending が同じ order を再試行する
                return False

            # ↓Task 15 検証 ③ (2026-08-15): 不正 output も試行を消費する。
            # `completed` だが output が dict でない / content が str でない
            # のは「使えない出力」であり、恒久的に同じ出力を返す runner /
            # model の故障では無制限再試行が残る。上の 2 ガードを 1 本へ
            # 併合し、bump site を 1 箇所に保つ。
            content = (result.output.get("content")
                       if isinstance(result.output, dict) else None)
            if not isinstance(content, str):
                self._consume_attempt(row["id"], "malformed output")
                return False

            # RAG → SQLite order (SQLite row is completion marker)。RAG 書込は
            # lock 不要 (Rag 自身が内部 lock を持つ — Task 9)。失敗時は SQLite
            # 行が無いので次回 run_pending が同じ order を再試行する (idempotent)。
            try:
                self.rag.add_reflection(row["id"], content, row["pair"])
            except Exception:  # noqa: BLE001
                _log.exception("rag.add_reflection failed for #%s — retry next run",
                               row["id"])
                # **(レビュー 1 周目 F6)** 次周期で自己修復するとはいえ、
                # 障害が続いても静かなままだと運用者が activity から
                # 追えない — SYSTEM カテゴリで記録する (activity 書込
                # 自体の例外は握り潰す — 既存の作法に合わせる)。
                try:
                    self.activity.write(
                        Category.SYSTEM, "reflection_rag_failed",
                        f"#{row['id']} {row['pair']}", ref_id=str(row["id"]))
                except Exception:  # noqa: BLE001
                    _log.exception(
                        "failed to record reflection_rag_failed for #%s",
                        row["id"])
                # ↓ここから挿入 (プラン 9 Task 15、設計書 D1)
                # RAG 書込失敗も再試行を消費する (`_consume_attempt` —
                # レビュー 1 周目 F1) — 「completed だから無料」にすると
                # 恒久的な RAG 障害で無制限再試行が復活する。
                self._consume_attempt(row["id"], "rag.add_reflection failed")
                # ↑ここまで挿入
                return False

            with self._core_lock:
                reflections.save(self.conn, row["id"], content, now)
                reflection_attempts.clear(self.conn, row["id"])
            try:
                self.activity.write(
                    Category.AGGREGATE, "reflection_created",
                    f"#{row['id']} {row['pair']}",
                    ref_id=str(row["id"]))
            except Exception:  # noqa: BLE001
                _log.exception("activity write failed for reflection #%s",
                               row["id"])
            return True
        finally:
            # **(レビュー 1 周目 F1)** prepare 成功後 (`mid` 発行済み) の
            # 想定外例外 (watch.begin/end 等) で mission が running のまま
            # 残らないようにする (TradeLoop の finally と同じ形 — Task 15)。
            if mid is not None and not finalized:
                with self._core_lock:
                    finalize_mission(self.conn, self.activity, self.clock,
                                     mid, MissionResult("failed", None, []))
                # (Task 15) 台帳へ刻む — `_consume_attempt`。想定外例外で
                # finalize_mission に一度も到達しなかった経路 (watch.end が
                # 例外を投げ続ける等) は、上の 3 箇所の bump がどれも通ら
                # ないまま fail-closed で mission を終端するだけだった。
                # 台帳が進まないと LLM 呼出しを伴う Mission が無制限に
                # 再試行され続ける — 例外中なのでこの消費自体の失敗も
                # 隔離する。
                try:
                    self._consume_attempt(row["id"], "aborted before finalize")
                except Exception:  # noqa: BLE001
                    _log.exception(
                        "failed to consume attempt in outer finally for #%s",
                        row["id"])
