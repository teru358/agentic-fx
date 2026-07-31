"""reflection cycle — クローズ済みトレードの振り返り生成 (SQLite + Chroma 二重保存)。"""
from __future__ import annotations

import json
import logging
import sqlite3

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import Settings
from agentic_fx.core.contracts import Clock
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
                 clock: Clock,
                 watch: MissionWatch | None = None) -> None:
        self.conn = conn
        self.runner = runner
        self.rag = rag
        self.settings = settings
        self.activity = activity
        self.clock = clock
        self.watch = watch if watch is not None else MissionWatch()

    def run_pending(self) -> int:
        """Reflect on closed orders without reflection. Per-item isolation."""
        rows = self.conn.execute(
            "SELECT o.* FROM orders o LEFT JOIN reflections r "
            "ON r.order_id = o.id WHERE o.status='closed' "
            "AND r.order_id IS NULL ORDER BY o.id").fetchall()
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
        """Run reflection on one closed order. Returns True if created."""
        now = self.clock.now()

        # Build prompt with order details + intent reasoning
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

        # Run with watch and exception normalization (_run_recorded pattern)
        result: MissionResult | None = None
        try:
            self.watch.begin(mid, "reflection", mission.timeout_sec)
            result = self.runner.run(mission)
            # Normalize non-MissionResult to failed
            if not isinstance(result, MissionResult):
                result = MissionResult("failed", None, [])
        except Exception:  # noqa: BLE001
            _log.exception("reflection runner raised")
            result = MissionResult("failed", None, [])
        finally:
            # Ensure result is always MissionResult
            if result is None:
                result = MissionResult("failed", None, [])
            try:
                missions.finish(
                    self.conn, mid, result.status, result.output,
                    result.transcript, self.clock.now())
            except Exception:  # noqa: BLE001
                _log.exception("missions.finish failed for %s", mid)
                try:
                    self.activity.write(
                        Category.SYSTEM, "mission_finalize_failed",
                        f"mid={mid}")
                except Exception:  # noqa: BLE001
                    _log.exception("failed to record mission_finalize_failed")
            self.watch.end(mid)

        if result.status != "completed":
            return False

        content = result.output["content"]

        # RAG → SQLite order (SQLite row is completion marker)
        # If RAG fails, no SQLite row → next run upsert (order_id idempotent)
        try:
            self.rag.add_reflection(row["id"], content, row["pair"])
        except Exception:  # noqa: BLE001
            _log.exception("rag.add_reflection failed for #%s — retry next run",
                           row["id"])
            return False

        reflections.save(self.conn, row["id"], content, now)
        self.activity.write(
            Category.AGGREGATE, "reflection_created",
            f"#{row['id']} {row['pair']}",
            ref_id=str(row["id"]))
        return True
