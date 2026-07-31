"""取引判断 loop — Mission 組み立て・実行・記録・executor 連携 (設計書 §5)。

構造:
- 公開 run_once/ask_once: サービス境界 (never-raise)
- _run_once_impl/_ask_once_impl: 実装本体
- _run_recorded: runner 実行の汎用ラッパ (例外正規化・missions.finish・watch 計測)
"""
from __future__ import annotations

import sqlite3

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import Settings
from agentic_fx.core.contracts import Clock, IntentParseError, Origin, TradeIntent
from agentic_fx.core.executor import Executor
from agentic_fx.core.notifier import Notifier
from agentic_fx.datafeed.econ_calendar import EconCalendar
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.loops.mission_watch import MissionWatch
from agentic_fx.loops.prompts_loader import load_prompt
from agentic_fx.loops.summary import (
    ANSWER_SCHEMA, build_state_summary, trade_intent_schema,
)
from agentic_fx.policy import Policy
from agentic_fx.runners.base import AgentRunner, Mission, MissionResult
from agentic_fx.store import missions

import logging

_log = logging.getLogger("agentic_fx.trade_loop")

_TRADE_TOOLS = ["get_ohlcv", "get_indicators", "search_news",
                "get_econ_calendar", "get_positions", "get_account",
                "get_recent_reflections", "search_reflections"]


class TradeLoop:
    def __init__(self, *, conn: sqlite3.Connection, runner: AgentRunner,
                 settings: Settings, executor: Executor,
                 provider: PriceProvider, econ: EconCalendar, policy: Policy,
                 activity: ActivityLog, notifier: Notifier,
                 clock: Clock,
                 watch: MissionWatch | None = None) -> None:
        self.conn = conn
        self.runner = runner
        self.settings = settings
        self.executor = executor
        self.provider = provider
        self.econ = econ
        self.policy = policy
        self.activity = activity
        self.notifier = notifier
        self.clock = clock
        self.watch = watch if watch is not None else MissionWatch()

    # ---- 公開 API (never-raise サービス境界) -----------------------------------

    def run_once(self, trigger: str = "cron") -> dict | None:
        try:
            return self._run_once_impl(trigger)
        except Exception:  # noqa: BLE001 — サービス境界: 周期を殺さない
            _log.exception("trade mission service boundary failed")
            self._safe_report_boundary_failure("trade")
            return None

    def ask_once(self, question: str) -> str:
        try:
            return self._ask_once_impl(question)
        except Exception:  # noqa: BLE001
            _log.exception("ask mission service boundary failed")
            self._safe_report_boundary_failure("ask")
            return "(Mission 失敗: internal_error)"

    # ---- 実装本体 ---------------------------------------------------

    def _run_once_impl(self, trigger: str = "cron") -> dict | None:
        now = self.clock.now()
        try:
            self.provider.healthcheck(self.settings.pairs[0])
        except DataUnhealthy as e:
            self.activity.write(Category.SYSTEM, "data_unhealthy", str(e))
            self.notifier.send(f"[agentic-fx] データ不健全のため判断をスキップ: {e}")
            return None

        prompt = self._build_prompt(load_prompt("trade_mission"))
        mission = Mission(
            prompt=prompt, tools=_TRADE_TOOLS,
            output_schema=trade_intent_schema(self.settings.pairs),
            max_turns=self.settings.llama_swap.max_turns,
            timeout_sec=self.settings.llama_swap.timeout_sec)
        mid = missions.start(self.conn, "trade",
                             self.settings.runner.trade.backend,
                             self.settings.runner.trade.model, now,
                             trigger=trigger)
        result = self._run_recorded(mid, mission, loop="trade")
        if result.status != "completed":
            self.activity.write(Category.AGGREGATE, "mission_failed",
                                f"runner status={result.status}",
                                ref_id=str(mid))
            self.notifier.send(f"[agentic-fx] 判断 Mission 失敗: {result.status}")
            return None
        try:
            intent = TradeIntent.from_llm_dict(result.output,
                                               origin=Origin.SCHEDULER)
        except IntentParseError as e:
            self.activity.write(Category.AGGREGATE, "intent_parse_failed",
                                str(e), ref_id=str(mid))
            return None
        out = self.executor.handle_intent(intent, mid)
        self.activity.write(Category.AGGREGATE, "decision",
                            f"{intent.action.value} -> {out['result']}",
                            ref_id=str(mid))
        return out

    def _ask_once_impl(self, question: str) -> str:
        now = self.clock.now()
        prompt = self._build_prompt(load_prompt("ask_mission")) \
            + f"\n\n## ユーザーの質問\n{question}"
        mission = Mission(
            prompt=prompt, tools=_TRADE_TOOLS,
            output_schema=ANSWER_SCHEMA,
            max_turns=self.settings.llama_swap.max_turns,
            timeout_sec=self.settings.llama_swap.timeout_sec)
        mid = missions.start(self.conn, "ask",
                             self.settings.runner.trade.backend,
                             self.settings.runner.trade.model, now)
        result = self._run_recorded(mid, mission, loop="ask")
        if result.status != "completed":
            return f"(Mission 失敗: {result.status})"
        # output 検証: dict であり、answer キーが存在し、値が str であること
        if not isinstance(result.output, dict):
            return "(Mission 失敗: invalid_output)"
        answer = result.output.get("answer")
        if not isinstance(answer, str):
            return "(Mission 失敗: invalid_answer)"
        self.activity.write(Category.AGGREGATE, "ask_answered",
                            question[:80], ref_id=str(mid))
        return answer

    # ---- Internal -------------------------------------------------------

    def _run_recorded(self, mid: int, mission: Mission, *,
                      loop: str) -> MissionResult:
        """runner を実行し、例外を failed に正規化して必ず missions.finish する。

        MissionWatch の begin/end も呼ぶ。
        """
        result: MissionResult | None = None
        try:
            self.watch.begin(mid, loop, mission.timeout_sec)
            result = self.runner.run(mission)
            # 戻り値が MissionResult でない場合も failed に正規化
            if not isinstance(result, MissionResult):
                result = MissionResult("failed", None, [])
        except Exception:  # noqa: BLE001 — runner 例外で周期を殺さない
            _log.exception("runner raised")
            result = MissionResult("failed", None, [])
        finally:
            # result が None の場合も failed に正規化 (begin が例外を出さなくても
            # 予期しない状況を守る)
            if result is None:
                result = MissionResult("failed", None, [])
            try:
                missions.finish(self.conn, mid, result.status, result.output,
                                result.transcript, self.clock.now())
            except Exception:  # noqa: BLE001
                _log.exception("missions.finish failed for %s", mid)
                try:
                    self.activity.write(Category.SYSTEM, "mission_finalize_failed",
                                        f"mid={mid}")
                except Exception:  # noqa: BLE001
                    _log.exception("failed to record mission_finalize_failed")
            self.watch.end(mid)
        return result

    def _build_prompt(self, system: str) -> str:
        """システムプロンプト + policy + サマリを結合する。"""
        parts = [system]
        tail = self.policy.tail(4000)
        if tail:
            parts.append(f"## ユーザー方針 (policy)\n{tail}")
        parts.append(build_state_summary(
            self.conn, self.executor.broker, self.econ, self.clock,
            self.settings.paper.starting_balance))
        return "\n\n".join(parts)

    def _safe_report_boundary_failure(self, loop: str) -> None:
        """公開境界での例外を activity/notifier に記録する (いずれも例外を吸収)。"""
        try:
            self.activity.write(Category.SYSTEM, "mission_boundary_failed",
                                f"loop={loop}")
        except Exception:  # noqa: BLE001
            _log.exception("failed to record mission boundary failure")
        try:
            self.notifier.send(f"[agentic-fx] {loop} Mission が内部エラーで失敗しました")
        except Exception:  # noqa: BLE001
            _log.exception("failed to notify mission boundary failure")
