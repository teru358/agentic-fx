"""取引判断 loop — Mission 組み立て・実行・記録・executor 連携 (設計書 §5)。

構造:
- 公開 run_once/ask_once: サービス境界 (never-raise)
- _run_once_impl/_ask_once_impl: 実装本体
- _run_recorded: runner 実行の汎用ラッパ (例外正規化・missions.finish・watch 計測)
"""
from __future__ import annotations

import json
import sqlite3

from agentic_fx._safe_error import safe_error_text
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
from agentic_fx.store import missions, signals

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
        """取引判断 Mission 1 回分。`trigger == "signal"` のときだけ
        signal-aware lifecycle (§5 必須事項 1) を通る — それ以外の値
        (`"cron"` や `run_once("signal:demo")` のような直接指定含む) は
        従来どおり `missions.trigger` にそのまま記録されるだけの経路
        (非退行: `test_signal_trigger_is_recorded_verbatim` 参照)。

        healthcheck は claim より前 (claim 済みで healthcheck 死亡 →
        requeue 漏れ、を構造的に防ぐ)。
        """
        now = self.clock.now()
        try:
            self.provider.healthcheck(self.settings.pairs[0])
        except DataUnhealthy as e:
            self.activity.write(Category.SYSTEM, "data_unhealthy", str(e))
            self.notifier.send(f"[agentic-fx] データ不健全のため判断をスキップ: {e}")
            return None

        claimed: dict | None = None
        mid: int | None = None
        if trigger == "signal":
            # ②missions.start(trigger="signal") — 暫定値。NULL 窓を作らず、
            # signals_rate_ok の LIKE 'signal%' がこの行も数える (§12 不変
            # 条件は維持したまま常に非 NULL trigger を持たせる)。
            mid = missions.start(self.conn, "trade",
                                 self.settings.runner.trade.backend,
                                 self.settings.runner.trade.model, now,
                                 trigger="signal")
            # ③claim_oldest — 失敗なら LLM を起こさず即 finalize (skipped)。
            try:
                claimed = signals.claim_oldest(
                    self.conn, mission_id=mid, now=now,
                    freshness_bars=self.settings.plugin.signal_freshness_bars)
                if claimed is None:
                    missions.finish(self.conn, mid, "skipped", None, [], now)
                    return None
            except Exception:
                # fix round 1 F2 (codex): claim_oldest (または直後の
                # "skipped" finish) が例外を出すと、以前は mission が
                # running のまま永久残留していた。missions.start と
                # claim_oldest の間、または claim 失敗直後の finish の間で
                # 例外が起きても、mission を必ず終端させる。
                #
                # claim_oldest 内部で「DB は commit 済みだが呼び出し直後に
                # Python 例外」という曖昧窓は原理的に残る (claimed 行が
                # claimed のままリークし得る) — この窓を塞ぐのは
                # `signals.reclaim_expired` (lease 回収) の役目であり、
                # `signal_lease_min` が経過するまでは不可視になり得る。
                try:
                    missions.finish(self.conn, mid, "failed", None, [], now)
                except Exception:  # noqa: BLE001 — 元の例外を握りつぶさない
                    _log.exception(
                        "failed to finalize mission %s after claim error", mid)
                raise

        consumed = False
        try:
            if claimed is not None:
                # ④set_trigger で plugin 名を確定 ⑤プロンプトへシグナル行を注入
                missions.set_trigger(self.conn, mid, f"signal:{claimed['plugin']}")
                prompt = (self._build_prompt(load_prompt("trade_mission"))
                         + self._format_signal_injection(claimed))
            else:
                prompt = self._build_prompt(load_prompt("trade_mission"))
            mission = self._build_mission(prompt)
            if mid is None:
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
            # ⑥consume/requeue の確定規則: パース成功の時点で consume する
            # (プロンプトに実際に載せた Mission が確定できる)。executor 実行後
            # の requeue は二重発注ハザードになるため、ここで確定させる —
            # handle_intent の例外は「消費済み」のまま (requeue しない)。
            if claimed is not None:
                signals.consume(self.conn, claimed["id"], mission_id=mid,
                                now=self.clock.now())
                consumed = True
            try:
                out = self.executor.handle_intent(intent, mid)
            except Exception as e:  # noqa: BLE001
                _log.exception("executor.handle_intent raised")
                self.activity.write(Category.AGGREGATE, "intent_execution_failed",
                                    safe_error_text(e), ref_id=str(mid))
                self.notifier.send(
                    f"[agentic-fx] 注文処理失敗: {safe_error_text(e)}")
                return None
            self.activity.write(Category.AGGREGATE, "decision",
                                f"{intent.action.value} -> {out['result']}",
                                ref_id=str(mid))
            return out
        finally:
            # 例外時も claimed のまま残さない (lease 回収を待たず即 requeue)。
            # consume 済みならここでは何もしない (二重発注ハザードを避ける
            # ため — docstring 上の ⑥ 参照)。
            if claimed is not None and not consumed:
                self._requeue_signal(claimed)

    def _build_mission(self, prompt: str) -> Mission:
        return Mission(
            prompt=prompt, tools=_TRADE_TOOLS,
            output_schema=trade_intent_schema(self.settings.pairs),
            max_turns=self.settings.llama_swap.max_turns,
            timeout_sec=self.settings.llama_swap.timeout_sec)

    def _format_signal_injection(self, claimed: dict) -> str:
        """claim した signals 行 (plugin/pair/timeframe/bar_ts/payload) を
        LLM が判断材料にできる形でプロンプトに追記する (brief 上書き 6)。"""
        payload = json.loads(claimed["payload_json"])
        return (
            "\n\n## 起動シグナル\n"
            "このMissionは以下の signal/strategy plugin の出力をトリガーに"
            "起動されました。判断の参考にしてください。\n"
            f"- plugin: {claimed['plugin']}\n"
            f"- kind: {claimed['kind']}\n"
            f"- pair: {claimed['pair']}\n"
            f"- timeframe: {claimed['timeframe']}\n"
            f"- bar_ts: {claimed['bar_ts']}\n"
            f"- payload: {json.dumps(payload, ensure_ascii=False)}\n")

    def _requeue_signal(self, claimed: dict) -> None:
        """claim した signal を pending へ戻す (上限超過なら abandoned +通知)。

        保守処理そのものの失敗で `_run_once_impl` の本流 (既に確定した
        結果/例外) を上書きしないよう、例外は握りつぶしログのみに残す。
        """
        try:
            status = signals.requeue(
                self.conn, claimed["id"], now=self.clock.now(),
                max_requeue=self.settings.plugin.signal_requeue_max)
            if status == "abandoned":
                self.notifier.send(
                    f"[agentic-fx] signal #{claimed['id']} "
                    f"({claimed['plugin']}) は requeue 上限超過のため "
                    "abandoned になりました")
        except Exception:  # noqa: BLE001 — 保守処理の失敗で本流を止めない
            _log.exception("signal requeue failed for signal_id=%s",
                           claimed["id"])

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
            return "(Mission 失敗: completed)"
        answer = result.output.get("answer")
        if not isinstance(answer, str):
            return "(Mission 失敗: completed)"
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
                # 監査未確定 (missions 行が running のまま output/transcript
                # 未保存) で執行させない — fail closed。呼び出し元は
                # completed 系の分岐に進まず、trade は mission_failed
                # (発注なし)・ask は失敗文字列を返す
                result = MissionResult("failed", None, [])
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
