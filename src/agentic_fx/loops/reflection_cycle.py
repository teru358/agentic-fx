"""reflection cycle — クローズ済みトレードの振り返り生成 (SQLite + Chroma 二重保存)。

構造 (プラン8 三相再構成 — 設計書 §3.1):
- prepare (core_lock 保持) → run (core_lock 非保持) → commit-core
  (core_lock 保持: finalize) → commit-post (core_lock 非保持: RAG 書込・
  SQLite reflections 保存・aggregate 記録)。RAG 書込は Rag 自身の内部
  lock に委ねる (Task 9) ため core_lock は取らない。
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
from agentic_fx.store import missions, reflections
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
                "SELECT o.* FROM orders o LEFT JOIN reflections r "
                "ON r.order_id = o.id WHERE o.status='closed' "
                "AND r.order_id IS NULL ORDER BY o.id LIMIT ?",
                (max_items,)).fetchall()
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
        → commit-post (RAG 書込は lock 不要 — Rag 自身が内部 lock を持つ
        (Task 9)。SQLite 書込 (`reflections.save`) のみ lock 保持)。
        """
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
            self.watch.end(mid)

        with self._core_lock:
            finalized = finalize_mission(self.conn, self.activity, self.clock,
                                         mid, result)

        if result.status != "completed" or not finalized:
            # finish 失敗時は監査未確定 (missions 行が running のまま) なので
            # reflection も保存しない。SQLite マーカー (reflections 行) が
            # 無いので次周期の run_pending が同じ order を再試行する
            return False

        if not isinstance(result.output, dict):
            return False
        content = result.output.get("content")
        if not isinstance(content, str):
            return False

        # RAG → SQLite order (SQLite row is completion marker)。RAG 書込は
        # lock 不要 (Rag 自身が内部 lock を持つ — Task 9)。失敗時は SQLite
        # 行が無いので次回 run_pending が同じ order を再試行する (idempotent)。
        try:
            self.rag.add_reflection(row["id"], content, row["pair"])
        except Exception:  # noqa: BLE001
            _log.exception("rag.add_reflection failed for #%s — retry next run",
                           row["id"])
            return False

        with self._core_lock:
            reflections.save(self.conn, row["id"], content, now)
        try:
            self.activity.write(
                Category.AGGREGATE, "reflection_created",
                f"#{row['id']} {row['pair']}",
                ref_id=str(row["id"]))
        except Exception:  # noqa: BLE001
            _log.exception("activity write failed for reflection #%s", row["id"])
        return True
