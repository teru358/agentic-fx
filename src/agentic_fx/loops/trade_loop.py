"""取引判断 loop — Mission 組み立て・実行・記録・executor 連携 (設計書 §5)。

構造 (プラン8 五相/三相再構成 — 設計書 §3.1):
- 公開 run_once/ask_once: サービス境界 (never-raise)
- _run_once_impl: prepare/run/commit-pre/commit-core/commit-post の五相
- _ask_once_impl: prepare/run/commit の三相
- finalize_mission (agentic_fx.loops.mission_finalize): missions.finish の
  CAS 化された共通呼び出し (core_lock 保持中に呼ぶこと。TradeLoop/
  ReflectionCycle 共有 — プラン8 Task 16)
"""
from __future__ import annotations

import json
import sqlite3
import threading
import contextlib
from pathlib import Path
from time import monotonic
from collections.abc import Mapping
from datetime import datetime

from agentic_fx._safe_error import safe_error_text
from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import Settings
from agentic_fx.core import market_hours, time_cutover
from agentic_fx.core.contracts import (
    Action, Clock, IntentParseError, Origin, OrderStatus as S, TradeIntent,
)
from agentic_fx.core.executor import (EXPOSURE_NOT_TRADABLE_REASON, Executor,
                                      ExposureNotTradableSymbol, NotTradableSymbol,
                                      intent_payload, snapshot_age_exceeded)
from agentic_fx.core.notifier import Notifier
from agentic_fx.datafeed.econ_calendar import EconCalendar
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.loops.mission_finalize import finalize_mission
from agentic_fx.loops.signal_decision_contract import (
    Accept, Invalid, Reject, DECISION_SCHEMA, SIGNAL_DECISION_TOOLS,
    parse_decision, verify_opposite_position,
)
from agentic_fx.loops.mission_watch import MissionWatch
from agentic_fx.loops.prompts_loader import load_prompt
from agentic_fx.loops.summary import (
    ANSWER_SCHEMA, build_state_summary, trade_intent_schema,
)
from agentic_fx.policy import Policy
from agentic_fx.runners.base import AgentRunner, Mission, MissionResult
from agentic_fx.store import alert_state
from agentic_fx.store import signal_decisions
from agentic_fx.store.signal_shadow import spread_from_quote
from agentic_fx.store import intents as intents_store
from agentic_fx.store import orders as orders_store
from agentic_fx.store import mission_decision_bars, missions, signals
from agentic_fx.plugin import switch
from agentic_fx.plugin.deploy_ops import acquire_name_locks
from agentic_fx.plugin.source_state import source_state
from agentic_fx.store import plugin_deploy_ops
from agentic_fx.core.timeutil import as_utc

import logging

_log = logging.getLogger("agentic_fx.trade_loop")

_CRON_TOOLS = ["get_ohlcv", "get_indicators", "search_news",
               "get_econ_calendar", "get_positions", "get_account",
               "get_recent_reflections", "search_reflections"]
_ASK_TOOLS = [*_CRON_TOOLS, "get_signals"]
# Full registry-facing set retained for service validation; missions use the
# explicit cron, ask, or signal list above.
_TRADE_TOOLS = _ASK_TOOLS

_SIGNAL_DECISION_INSTRUCTIONS = (
    "\n\n## Signal 採否\n"
    "この専用 Mission では signal を採用するか拒否するかだけを判断します。"
    "発注内容・方向・価格・SL/TP は signal の固定 payload が正本です。"
    "それらを変更せず、JSON の decision を accept または reject にし、"
    "reject の場合だけ reject_code を news_shock / opposite_position の"
    "どちらかにしてください。ほかの操作や理由コードは返しません。\n"
)


def signal_source_active(conn, plugins_root: Path, plugin: str, content_hash: str,
                         resolved=None) -> bool:
    """Check a claimed OPEN signal's source while its name locks are held."""
    identity = plugin_deploy_ops.live_identity(plugins_root, plugin)
    artifact_hash = (identity.artifact_hash
                     if identity is not None and identity.kind == "symlink"
                     else None)
    return source_state(
        conn, plugins_root, name=plugin, content_hash=content_hash,
        artifact_hash=artifact_hash,
        indicators=resolved.items if resolved is not None else None) is None


def gate_reject_streak(conn: sqlite3.Connection) -> tuple[int, int, str | None]:
    """(streak_id, rejected_count, dominant_category) — 設計書 D3 の SQL ①②③。
    **`conn_supervisor` (lock 外の読取専用接続) で呼ぶこと。**"""
    streak_id = int(conn.execute(
        "SELECT COALESCE(MAX(id),0) FROM trade_intents "
        "WHERE action='open' AND gate_result='accepted'").fetchone()[0])
    count = int(conn.execute(
        "SELECT COUNT(*) FROM trade_intents WHERE action='open' "
        "AND gate_result='rejected' AND id > ?", (streak_id,)).fetchone()[0])
    row = conn.execute(
        "SELECT reject_category,COUNT(*) c FROM trade_intents "
        "WHERE action='open' AND gate_result='rejected' AND id > ? "
        "GROUP BY reject_category ORDER BY c DESC,reject_category ASC LIMIT 1",
        (streak_id,)).fetchone()
    return streak_id, count, row["reject_category"] if row else None


class TradeLoop:
    def __init__(self, *, conn: sqlite3.Connection, runner: AgentRunner,
                 settings: Settings, executor: Executor,
                 provider: PriceProvider, econ: EconCalendar, policy: Policy,
                 activity: ActivityLog, notifier: Notifier,
                 clock: Clock, core_lock: threading.RLock,
                 conn_supervisor: sqlite3.Connection,
                 watch: MissionWatch | None = None,
                 plugins_root: Path | None = None,
                 resolved_by_identity=None) -> None:
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
        self._core_lock = core_lock
        self._conn_supervisor = conn_supervisor
        self._plugins_root = plugins_root
        self._resolved_by_identity = resolved_by_identity or {}
        self.watch = watch if watch is not None else MissionWatch()
        self._market_tool_daily: dict = {}

    # ---- 公開 API (never-raise サービス境界) -----------------------------------

    def run_once(self, trigger: str = "cron",
                 decision_bars: Mapping[tuple[str, str], datetime] | None = None,
                 signal_max_id: int | None = None) -> dict | None:
        try:
            return self._run_once_impl(trigger, decision_bars, signal_max_id)
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

    def _run_once_impl(self, trigger: str = "cron",
                       decision_bars: Mapping[tuple[str, str], datetime] | None = None,
                       signal_max_id: int | None = None) -> dict | None:
        """取引判断 Mission 1 回分 (プラン8 五相再構成 — 設計書 §3.1)。

        prepare (lock 保持) → run (lock 非保持) → commit-pre (lock 非保持:
        結果検証・intent 構築・snapshot 取得) → commit-core (lock 保持:
        consume・Risk Gate・執行・finish) → commit-post (lock 非保持:
        aggregate 記録)。`healthcheck` は prepare よりさらに前 (lock 外)
        — claim 済みで healthcheck 死亡 → requeue 漏れ、を構造的に防ぐ
        (既存の設計方針を維持)。**(裁定書 F-6 / CR-5) `self.provider` は
        `conn_supervisor` (RO 接続) で構築した `readonly=True` の
        `PriceProvider` — `healthcheck` 内部の `get_bars` が呼ぶ
        `ohlcv.upsert_cache_bars` は `readonly=True` によりスキップされるため、
        lock 外のこの呼び出しが `conn_core` を無保護で書き込むことはない
        (Global Constraints 違反の解消)。**

        **`finalize_mission` (共有ヘルパー — Task 16) が `False` を返した
        場合 (`missions.finish` 自体が例外で失敗) でも、直前の執行結果
        (発注等) は巻き戻さない** — 設計書 §3.1 が finalize を
        commit-core の**末尾** (paper broker 執行の後) に置くため。
        Task 15 節「⚠ 着手前検証の結果 (4)」で明示的に許容された意図的な
        挙動 (旧 `_run_recorded` の「finish 失敗時は result を failed に
        差し替え、呼び出し元を completed 系の分岐に進ませない」契約は
        もう維持していない)。監査未確定は fail closed にはせず、
        `mission_finalize_failed` の activity 記録で可視化したうえで、
        mission 行は `running` のまま残し、次回起動時の
        `recover_interrupted` による `interrupted` 回収に委ねる。
        `_ask_once_impl` も同じ契約 (finish 失敗時に回答文字列を巻き戻さ
        ない) を採用している — ask は読み取り専用で資金に影響しないため
        許容される。
        """
        now = self.clock.now()
        try:
            # `now` はここで 1 回だけ採り、healthcheck へ明示的に渡す
            # (healthcheck 側が独自に時刻を読み直さない)。
            self.provider.healthcheck(self.settings.pairs[0], now=now)
        except DataUnhealthy as e:
            self.activity.write(Category.SYSTEM, "data_unhealthy", str(e))
            self.notifier.send(f"[agentic-fx] データ不健全のため判断をスキップ: {e}")
            return None

        if trigger == "signal":
            return self._run_signal_mission(signal_max_id)

        mid: int | None = None
        finalized = False
        try:
            # ---- prepare (core_lock 保持) ----
            with self._core_lock:
                if trigger == "cron" and decision_bars:
                    mid = self._start_cron_mission(now, decision_bars)
                else:
                    mid = missions.start(self.conn, "trade",
                                         self.settings.runner.trade.backend,
                                         self.settings.runner.trade.model, now,
                                         trigger=trigger)
                prompt = self._build_prompt(load_prompt("trade_mission"), now)
                mission = self._build_mission(prompt)

            # ---- run (core_lock 非保持) ----
            self.watch.begin(mid, "trade", mission.timeout_sec)
            try:
                try:
                    result = self.runner.run(mission)
                    if not isinstance(result, MissionResult):
                        result = MissionResult("failed", None, [])
                except Exception:  # noqa: BLE001 — runner 例外で周期を殺さない
                    _log.exception("runner raised")
                    result = MissionResult("failed", None, [])
                self._record_market_tool_calls(result, now)
            finally:
                self.watch.end(mid)

            # ---- commit-pre (core_lock 非保持) ----
            if result.status != "completed":
                with self._core_lock:
                    finalize_mission(self.conn, self.activity, self.clock,
                                     mid, result)
                finalized = True
                self.activity.write(
                    Category.AGGREGATE, "mission_failed",
                    f"runner status={result.status}"
                    + (f" — {result.reason}" if result.reason else "")
                    + self._market_tool_calls_suffix(result),
                    ref_id=str(mid))
                self.notifier.send(
                    f"[agentic-fx] 判断 Mission 失敗: {result.status}"
                    + (f" — {result.reason}" if result.reason else ""))
                return None
            try:
                intent = TradeIntent.from_llm_dict(result.output,
                                                   origin=Origin.SCHEDULER)
            except IntentParseError as e:
                with self._core_lock:
                    finalize_mission(self.conn, self.activity, self.clock,
                                     mid, result)
                finalized = True
                self.activity.write(Category.AGGREGATE, "intent_parse_failed",
                                    str(e) + self._market_tool_calls_suffix(result),
                                    ref_id=str(mid))
                return None

            open_snapshot = None
            close_snapshot = None
            snapshot_error: Exception | None = None
            if intent.action is Action.OPEN:
                try:
                    # **(レビュー 3 周目 codex E5)** `_read_exposure_pairs`
                    # (conn_supervisor の読取) も `gather_open_snapshot` と
                    # 同じ try で包む。以前は 1 行上にあり、conn_supervisor
                    # の読取失敗だけが `_run_once_impl` を丸ごと脱出して
                    # `mission_boundary_failed` になっていた — 同じインフラ
                    # 障害でも `gather_open_snapshot` の失敗は
                    # `trade_intents` に記録済み gate 拒否として残るのに、
                    # こちらは `trade_intents` の行が 1 つも残らない非対称
                    # (D3 で塞いだのと同じ欠陥クラス) だった。
                    exposure_pairs = self._read_exposure_pairs()
                    open_snapshot = self.executor.gather_open_snapshot(
                        intent, exposure_pairs=exposure_pairs)
                except Exception as e:  # noqa: BLE001 — commit-core で
                    # 執行失敗として扱う (fail closed、consume より前に
                    # 確定させておき commit-core 側の分岐を単純にする)。
                    snapshot_error = e
            elif intent.action is Action.CLOSE:
                try:
                    # 裁定書 F-1 (CR-2/P8-01): CLOSE の quote/spec/
                    # close-rate も commit-pre (lock 非保持) で取得する。
                    # row が commit-pre 時点で OPEN でなければ snapshot
                    # 取得自体をスキップし (無駄な外部 I/O を避ける)、
                    # commit-core の close_from_snapshot が fresh な状態を
                    # 読み直して reject する (snapshot=None のときの契約 —
                    # lock 内では取得し直さない)。
                    #
                    # **(レビュー 3 周目 codex E5)** `_read_close_row` も
                    # 同じ try で包む (OPEN 側と同じ理由)。
                    row = self._read_close_row(intent.order_id)
                    if row is not None and row["status"] == S.OPEN.value:
                        close_snapshot = self.executor.gather_close_snapshot(row)
                except Exception as e:  # noqa: BLE001
                    snapshot_error = e

            # ---- commit-core (core_lock 保持) ----
            # **通知は commit-post まで遅延させる (Step 3.5)** — Notifier.send は
            # urlopen(timeout=10) の同期実行なので、ここで送ると core_lock を
            # 握ったまま最大 10 秒ブロックし SL/TP 監視が止まる。
            with self._core_lock, \
                    self.executor.defer_notifications() as deferred:
                try:
                    # intent は執行前に必ず記録し、以降の拒否理由を iid に
                    # 結び付ける。record_and_validate_intent 自体は発注しない。
                    iid, early = self.executor.record_and_validate_intent(
                        intent, mid)
                    if isinstance(snapshot_error,
                                    (NotTradableSymbol, ExposureNotTradableSymbol)):
                        reason = (EXPOSURE_NOT_TRADABLE_REASON
                                  if isinstance(snapshot_error, ExposureNotTradableSymbol)
                                  else "not_tradable_symbol")
                        intents_store.set_gate_result(
                            self.conn, iid, accepted=False, reject_reason=reason,
                            reject_category="execution")
                        self.activity.write(Category.TRADE, "gate_rejected", reason,
                                            ref_id=str(iid))
                        out = {"result": "rejected", "order_id": None,
                               "reasons": [reason]}
                    elif snapshot_error is not None:
                        # **(Task 14 レビュー 2 周からの申し送りの回収)**
                        # commit-pre の外部取得失敗を、ライブ経路 (`_open` の
                        # `except DataUnhealthy`) と**同じ形の「記録済み gate
                        # 拒否」**に変換する。ここを汎用の
                        # `intent_execution_failed` にすると
                        # `trade_intents.gate_result` が NULL のまま残り、
                        # 「なぜ発注されなかったか」を DB から追えない。
                        #
                        # intent の記録自体は常に行い、`iid` に拒否を結び付ける。
                        # early (hold/origin/mission 拒否) が確定している
                        # ケースはそちらを優先する — snapshot は使わない。
                        if early is not None:
                            out = early
                        else:
                            reasons = ["execution snapshot unavailable: "
                                       + safe_error_text(snapshot_error)]
                            intents_store.set_gate_result(
                                self.conn, iid, accepted=False,
                                reject_reason=reasons[0],
                                reject_category="execution")
                            self.activity.write(Category.TRADE, "gate_rejected",
                                                reasons[0], ref_id=str(iid))
                            out = {"result": "rejected", "order_id": None,
                                   "reasons": reasons}
                    elif early is not None:
                        out = early
                    elif intent.action is Action.OPEN:
                        out = self.executor.open_from_snapshot(
                            intent, iid, open_snapshot,
                            max_snapshot_age_sec=self.settings.worker.snapshot_max_age_sec)
                    elif intent.action is Action.CLOSE:
                        out = self.executor.close_from_snapshot(
                            intent, iid, close_snapshot,
                            max_snapshot_age_sec=self.settings.worker.snapshot_max_age_sec)
                    else:
                        out = self.executor.cancel_intent(intent, iid)
                except Exception as e:  # noqa: BLE001
                    _log.exception("executor intent handling raised")
                    finalize_mission(self.conn, self.activity, self.clock,
                                     mid, result)
                    finalized = True
                    self.activity.write(Category.AGGREGATE,
                                        "intent_execution_failed",
                                        safe_error_text(e)
                                        + self._market_tool_calls_suffix(result),
                                        ref_id=str(mid))
                    # **`self.notifier.send` をここで呼んではならない** —
                    # まだ core_lock 保持中であり、urlopen(timeout=10) で
                    # 最大 10 秒ブロックする。commit-post まで遅延させ、
                    # `return` せずに with を抜けてから送る (Step 3.5)。
                    deferred.append(
                        f"[agentic-fx] 注文処理失敗: {safe_error_text(e)}")
                    out = None
                else:
                    finalize_mission(self.conn, self.activity, self.clock,
                                     mid, result)
                    finalized = True

            # ---- commit-post (core_lock 非保持) ----
            # commit-core で溜めた通知をここで送る (Step 3.5)。送信失敗で
            # 本流を殺さない — 通知は保守処理であり、発注結果は既に確定済み。
            # **失敗経路 (out is None) もここを必ず通る** — commit-core から
            # 直接 return すると通知が送られないまま捨てられる。
            for _text in deferred:
                try:
                    self.notifier.send(_text)
                except Exception:  # noqa: BLE001 — 通知失敗で本流を止めない
                    _log.exception("deferred notification failed")
            self._notify_gate_reject_streak()        # ← Task 17 が足す 1 行
            if out is None:
                return None
            self.activity.write(Category.AGGREGATE, "decision",
                                f"{intent.action.value} -> {out['result']}"
                                + self._market_tool_calls_suffix(result),
                                ref_id=str(mid))
            return out
        finally:
            # レビュー 1 周目 codex A3: run/commit-pre 相 (watch.begin/end・
            # TradeIntent 解析想定外例外・_read_exposure_pairs/
            # _read_close_row 等) で想定外の例外が出ても、mid が既に
            # 発行されている限り mission を running のまま残さない
            # (fail closed)。`missions.finish` は CAS 化されているため、
            # 既に (正常経路で) finalize 済みなら二重呼び出しは無害だが、
            # 無用なログを避けるため `finalized` で一度限りにする。
            if mid is not None and not finalized:
                with self._core_lock:
                    finalize_mission(self.conn, self.activity, self.clock,
                                     mid, MissionResult("failed", None, []))

    def _run_signal_mission(self, signal_max_id: int | None) -> dict | None:
        """専用 signal 採否経路。採否 transaction が確定するまで発注しない。"""
        now = self.clock.now()
        mid: int | None = None
        claimed: dict | None = None
        finalized = False
        consumed = False
        deferred_notifications: list[str] = []
        result = MissionResult("failed", None, [])
        try:
            with self._core_lock:
                mid = missions.start(
                    self.conn, "trade", self.settings.runner.trade.backend,
                    self.settings.runner.trade.model, now, trigger="signal")
                kwargs = {"mission_id": mid, "now": now,
                          "freshness_bars": self.settings.plugin.signal_freshness_bars}
                if signal_max_id is not None:
                    kwargs["max_id"] = signal_max_id
                claimed = signals.claim_oldest(self.conn, **kwargs)
                if claimed is None:
                    missions.finish(self.conn, mid, "skipped", None, [], now)
                    finalized = True
                    return None
                missions.set_trigger(self.conn, mid, f"signal:{claimed['plugin']}")
                prompt = (self._build_prompt(load_prompt("trade_mission"), now)
                          + _SIGNAL_DECISION_INSTRUCTIONS
                          + self._format_signal_injection(claimed))
                mission = Mission(
                    prompt=prompt, tools=list(SIGNAL_DECISION_TOOLS),
                    output_schema=DECISION_SCHEMA,
                    max_turns=self.settings.llama_swap.max_turns,
                    timeout_sec=self.settings.llama_swap.timeout_sec)

            self.watch.begin(mid, "trade", mission.timeout_sec)
            try:
                run_result = self.runner.run(mission)
                result = (run_result if isinstance(run_result, MissionResult)
                          else MissionResult("failed", None, []))
            except Exception:  # noqa: BLE001
                _log.exception("signal decision runner raised")
            finally:
                self.watch.end(mid)

            if result.status != "completed":
                with self._core_lock:
                    finalize_mission(self.conn, self.activity, self.clock, mid, result)
                finalized = True
                self._record_signal_mission_failure(mid, result)
                return None

            parsed = parse_decision(result.output if isinstance(result.output, dict)
                                    else "")
            if isinstance(parsed, Invalid):
                failed = MissionResult("failed", {"detail": parsed.detail},
                                       result.transcript)
                with self._core_lock:
                    finalize_mission(self.conn, self.activity, self.clock, mid, failed)
                finalized = True
                self._record_signal_mission_failure(mid, failed)
                return None

            signal_id = int(claimed["id"])

            payload = json.loads(claimed["payload_json"])
            if not isinstance(payload, dict) or claimed.get("contract_version") != "executable_v1":
                raise ValueError("claimed signal is not executable_v1")
            executable = {**payload, "action": "open", "origin": Origin.SCHEDULER.value}
            intent = TradeIntent.from_llm_dict(executable, origin=Origin.SCHEDULER)
            if (intent.action is not Action.OPEN or intent.pair != claimed["pair"]
                    or intent.origin is not Origin.SCHEDULER):
                raise ValueError("signal intent failed origin/loop validation")

            reject_code = parsed.code if isinstance(parsed, Reject) else None

            try:
                spec = self.executor.spec_fn(intent.pair)
            except (NotTradableSymbol, ExposureNotTradableSymbol, DataUnhealthy) as exc:
                # decision は書かず requeue する (上限で終わる)。理由は固定語で残す。
                self.activity.write(
                    Category.AGGREGATE, "signal_snapshot_requeue",
                    type(exc).__name__, ref_id=str(mid))
                raise
            pair_rule = self.settings.risk.pair_rules.get(intent.pair)
            assumed_spread = pair_rule.assumed_spread_pips if pair_rule else 0.0
            spread = spread_from_quote(None, None, pip_size=spec.pip_size,
                                       assumed_spread_pips=assumed_spread)
            output = result.output if isinstance(result.output, dict) else None
            snapshot = None
            if isinstance(parsed, Accept):
                exposure_pairs = self._read_exposure_pairs()
                try:
                    snapshot = self.executor.gather_open_snapshot(
                        intent, exposure_pairs=exposure_pairs)
                except (NotTradableSymbol, ExposureNotTradableSymbol, DataUnhealthy) as exc:
                    # decision は書かず requeue する (上限で終わる)。理由は固定語で残す。
                    self.activity.write(
                        Category.AGGREGATE, "signal_snapshot_requeue",
                        type(exc).__name__, ref_id=str(mid))
                    raise
                if self.executor.state_fn() != "ready":
                    return None
            payload_for_commit = intent_payload(intent)
            with contextlib.ExitStack() as locks:
                resolved = self._resolved_by_identity.get(
                    (claimed["plugin"], claimed["content_hash"]))
                if self._plugins_root is not None:
                    source_names = {claimed["plugin"]}
                    if resolved is not None:
                        source_names.update(item.plugin_name
                                            for item in resolved.items)
                    try:
                        locks.enter_context(acquire_name_locks(
                            self._plugins_root, source_names,
                            deadline=monotonic()
                            + self.settings.worker.plugin_lock_timeout_sec))
                    except switch.PluginBusyError:
                        self.activity.write(
                            Category.AGGREGATE, "signal_source_lock_requeue",
                            "signal_source_lock_timeout", ref_id=str(mid))
                        raise
                    except Exception:
                        self.activity.write(
                            Category.AGGREGATE, "signal_source_lock_requeue",
                            "signal_source_lock_error", ref_id=str(mid))
                        raise
                with self._core_lock, self.executor.defer_notifications() as deferred:
                    deferred_notifications = deferred
                    gate_now = self.clock.now()
                    _, fresh_until = signals.freshness_window(
                        claimed["timeframe"], claimed["bar_ts"],
                        self.settings.plugin.signal_freshness_bars)
                    signal_stale = gate_now > datetime.fromisoformat(fresh_until)
                    snapshot_reason = (snapshot_age_exceeded(
                        snapshot, gate_now,
                        self.settings.worker.snapshot_max_age_sec)
                        if snapshot is not None else None)
                    if signal_stale:
                        signals.abandon_claimed(
                            self.conn, signal_id, now=gate_now,
                            reason=signals.freshness_abandon_reason(claimed))
                        missions.finish(self.conn, mid, "failed", None, [], gate_now)
                        consumed = finalized = True
                        return None
                    if snapshot_reason is not None:
                        return None
                    if self.executor.state_fn() != "ready":
                        return None
                    if (self._plugins_root is not None
                            and not signal_source_active(
                                self.conn, self._plugins_root, claimed["plugin"],
                                claimed["content_hash"], resolved)):
                        signals.abandon_claimed(
                            self.conn, signal_id, now=gate_now,
                            reason="source_inactive")
                        missions.finish(self.conn, mid, "failed", None, [], gate_now)
                        consumed = finalized = True
                        return None
                    if reject_code == "opposite_position":
                        active_orders = orders_store.list_by_status(
                            self._conn_supervisor, S.OPEN, S.PENDING_FILL,
                            S.PROTECTION_PENDING)
                        if not verify_opposite_position(
                                parsed, active_orders, pair=intent.pair,
                                direction=intent.direction):
                            failed = MissionResult(
                                "failed", {"detail": "invalid_reject_code"},
                                result.transcript)
                            finalize_mission(self.conn, self.activity, self.clock,
                                             mid, failed)
                            finalized = True
                            return None
                    if isinstance(parsed, Accept):
                        if self.econ.window_view(intent.pair, gate_now).in_window:
                            signal_decisions.commit_reject(
                                self.conn, mission_id=mid, signal_id=signal_id,
                                actor=signal_decisions.ACTOR_SYSTEM,
                                reject_code=signal_decisions.SYSTEM_REJECT_CODE,
                                spread=spread, output=output,
                                transcript=result.transcript, now=gate_now)
                            consumed = finalized = True
                            return {"result": "rejected", "order_id": None,
                                    "reasons": ["econ_window"]}
                        committed = signal_decisions.commit_accept(
                            self.conn, mission_id=mid, signal_id=signal_id,
                            intent_payload=payload_for_commit, spread=spread,
                            output=output, transcript=result.transcript,
                            now=gate_now)
                        consumed = finalized = True
                        try:
                            out = self.executor.open_from_snapshot(
                                intent, committed.trade_intent_id, snapshot,
                                max_snapshot_age_sec=(
                                    self.settings.worker.snapshot_max_age_sec))
                        except Exception as e:  # noqa: BLE001
                            reason = safe_error_text(e)
                            intents_store.set_gate_result(
                                self.conn, committed.trade_intent_id,
                                accepted=False, reject_reason=reason,
                                reject_category="execution")
                            self.activity.write(
                                Category.AGGREGATE, "intent_execution_failed",
                                reason, ref_id=str(mid))
                            deferred_notifications.append(
                                f"[agentic-fx] 注文処理失敗: {reason}")
                            return None
                        return out
                    signal_decisions.commit_reject(
                        self.conn, mission_id=mid, signal_id=signal_id,
                        actor=signal_decisions.ACTOR_LLM, reject_code=reject_code,
                        spread=spread, output=output, transcript=result.transcript,
                        now=gate_now)
                    consumed = finalized = True
                    return {"result": "rejected", "order_id": None,
                            "reasons": [reject_code]}
        except Exception:  # noqa: BLE001
            _log.exception("signal decision flow failed")
            return None
        finally:
            for message in deferred_notifications:
                try:
                    self.notifier.send(message)
                except Exception:  # noqa: BLE001 — notification failure is non-fatal
                    _log.exception("signal notification failed")
            if claimed is not None and not consumed:
                with self._core_lock:
                    message = self._requeue_signal(
                        claimed, abandon_if_stale=True)
                if message:
                    try:
                        self.notifier.send(message)
                    except Exception:  # noqa: BLE001
                        _log.exception("signal requeue notification failed")
            if mid is not None and not finalized:
                with self._core_lock:
                    finalize_mission(self.conn, self.activity, self.clock, mid,
                                     MissionResult("failed", None, []))

    def _record_signal_mission_failure(self, mission_id: int,
                                       result: MissionResult) -> None:
        status = result.status
        reason = result.reason
        if reason is None and isinstance(result.output, dict):
            reason = result.output.get("detail")
        self.activity.write(
            Category.AGGREGATE, "mission_failed",
            f"runner status={status}" + (f" — {reason}" if reason else ""),
            ref_id=str(mission_id))
        try:
            self.notifier.send(
                f"[agentic-fx] 判断 Mission 失敗: {status}"
                + (f" — {reason}" if reason else ""))
        except Exception:  # noqa: BLE001 — 通知失敗で再試行処理を妨げない
            _log.exception("signal mission failure notification failed")

    def _start_cron_mission(self, now, decision_bars) -> int:
        """missions 行と mission_decision_bars 行を 1 transaction で確定する
        (core_lock 保持中に呼ぶ)。例外時は両方を rollback して再送出する —
        呼び出し元の mid には何も代入されないので、外側 finally の
        finalize (commit されていない mission の終端) は走らない。"""
        try:
            mid = missions.start(self.conn, "trade",
                                 self.settings.runner.trade.backend,
                                 self.settings.runner.trade.model, now,
                                 trigger="cron", commit=False)
            mission_decision_bars.insert_many(self.conn, mid, decision_bars)
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return mid

    def _notify_gate_reject_streak(self) -> None:
        """commit-post 専用 (設計書 D3 / D3')。判定は `_conn_supervisor`、
        通知は lock 非保持、ラッチ更新だけ短い `core_lock` + `self.conn`。
        scheduler / maintenance からは決して呼ばない。

        **`self.notifier.send` を `core_lock` の中へ入れてはならない** —
        `send` は `urlopen(timeout=10)` の同期実行であり、SL/TP 監視を最大
        10 秒止める (Global Constraints)。
        `test_gate_alert_notification_is_sent_without_holding_core_lock` が守る。
        """
        try:
            streak_id, count, dominant = gate_reject_streak(self._conn_supervisor)
            if count < self.settings.alert.consecutive_gate_reject:
                return
            with self._core_lock:
                last = alert_state.get(
                    self.conn, alert_state.GATE_REJECT_STREAK_KEY)
            if last == str(streak_id):
                return
            self.notifier.send(
                f"[agentic-fx] open intent が {count} 件連続で却下されています"
                f" (最多カテゴリ: {dominant or 'unknown'})")
            with self._core_lock:
                alert_state.set(
                    self.conn, alert_state.GATE_REJECT_STREAK_KEY,
                    str(streak_id), now=self.clock.now())
        except Exception:  # noqa: BLE001 — finalize 済み Mission を失敗に見せない
            _log.exception("gate rejection alert evaluation failed")

    def _read_exposure_pairs(self) -> list[str]:
        """commit-pre 専用: `conn_supervisor` (lock 外の読取専用接続) から
        既存 exposure (`executor._EXPOSURE` の全状態) の pair 一覧を読む。
        `gather_open_snapshot` の `exposure_pairs` に渡す。"""
        from agentic_fx.core.executor import _EXPOSURE
        from agentic_fx.store import orders as orders_store
        rows = orders_store.list_by_status(self._conn_supervisor, *_EXPOSURE)
        return sorted({r["pair"] for r in rows})

    def _read_close_row(self, order_id: int) -> dict | None:
        """commit-pre 専用 (裁定書 F-1 / CR-2): `conn_supervisor` (lock 外の
        読取専用接続) から CLOSE 対象の order 行を読む。`gather_close_snapshot`
        の入力にするだけで、commit-core は改めて `self.conn` (conn_core) から
        読み直す (commit-pre 後に状態が変わっていないかの再確認 — 設計書
        §3.1 の鮮度契約と同じ精神)。"""
        from agentic_fx.store import orders as orders_store
        return orders_store.get(self._conn_supervisor, order_id)

    def _build_mission(self, prompt: str) -> Mission:
        return Mission(
            prompt=prompt, tools=_CRON_TOOLS,
            output_schema=trade_intent_schema(self.settings.pairs),
            max_turns=self.settings.llama_swap.max_turns,
            timeout_sec=self.settings.llama_swap.timeout_sec)

    def _format_signal_injection(self, claimed: dict) -> str:
        """claim した signals 行を「この判断で扱う signal」節としてプロンプトに
        追記する。strategy_timeframe は signal を作った strategy の足 (判断足
        とは限らない)、bar_close は strategy 足の確定時刻、fresh_until は
        pending のまま引き受けられる最後の時刻 (これを過ぎると abandoned)。"""
        payload = json.loads(claimed["payload_json"])
        direction = payload.get("direction") if isinstance(payload, dict) else None
        try:
            bar_close, fresh_until = signals.freshness_window(
                claimed["timeframe"], claimed["bar_ts"],
                self.settings.plugin.signal_freshness_bars)
        except KeyError:
            # fail-open: 未知 timeframe (通常は claim_oldest 自身の鮮度
            # ゲートが構造的に弾く — signal_tools.get_signals と同じ
            # 規律の防御的複製) で例外を出すと、この Mission は failed →
            # requeue → 次 cron が同じ最古行を再 claim → 再度失敗、を
            # 繰り返す (`_run_signal_maintenance` の abandoned 化は
            # pending にしか効かず、claimed 中の同じ 1 tick は救えない)。
            # プロンプトへは「不明」で埋めて判断そのものは続行させる。
            bar_close = fresh_until = "不明"
        lead = ("このMissionは以下の signal/strategy plugin の出力をトリガーに"
                "起動されました。判断の参考にしてください。\n")
        return (
            "\n\n## この判断で扱う signal\n"
            + lead
            + f"- signal_id: {claimed['id']}\n"
            f"- plugin: {claimed['plugin']}\n"
            f"- kind: {claimed['kind']}\n"
            f"- pair: {claimed['pair']}\n"
            f"- direction: {direction}\n"
            f"- strategy_timeframe: {claimed['timeframe']}\n"
            f"- bar_ts: {claimed['bar_ts']}\n"
            f"- bar_close: {bar_close}\n"
            f"- fresh_until: {fresh_until}\n"
            f"- payload: {json.dumps(payload, ensure_ascii=False)}\n")

    def _requeue_signal(self, claimed: dict, *,
                        abandon_if_stale: bool = False) -> str | None:
        """claim した signal を pending へ戻す (**core_lock 保持中に呼ぶ** —
        signals.requeue は conn_core を書き込むため)。

        **通知はここで送らない** (レビュー 1 周目 codex A1)。Notifier.send は
        urlopen(timeout=10) の同期実行なので、lock 保持中に呼ぶと SL/TP
        監視が最大 10 秒止まる。送るべき文面を返すだけにし、呼び出し元が
        lock 解放後に送る。

        保守処理そのものの失敗で `_run_once_impl` の本流 (既に確定した
        結果/例外) を上書きしないよう、例外は握りつぶしログのみに残す。
        """
        try:
            if abandon_if_stale and self._claimed_signal_is_stale(claimed):
                # 新規リスクが止まっている間に鮮度が切れた signal は、
                # requeue 回数を使って pending に戻さず、その場で終端する。
                signals.abandon_claimed(
                    self.conn, claimed["id"], now=self.clock.now(),
                    reason=signals.freshness_abandon_reason(claimed))
                return (f"[agentic-fx] signal #{claimed['id']} "
                        f"({claimed['plugin']}) は鮮度切れのため "
                        "abandoned になりました")
            status = signals.requeue(
                self.conn, claimed["id"], now=self.clock.now(),
                max_requeue=self.settings.plugin.signal_requeue_max)
            if status == "abandoned":
                return (f"[agentic-fx] signal #{claimed['id']} "
                        f"({claimed['plugin']}) は requeue 上限超過のため "
                        "abandoned になりました")
        except Exception:  # noqa: BLE001 — 保守処理の失敗で本流を止めない
            _log.exception("signal requeue failed for signal_id=%s",
                           claimed["id"])
        return None

    def _claimed_signal_is_stale(self, claimed: dict) -> bool:
        """鮮度の境界は `expire_stale` と同じ (`freshness_window` の
        fresh_until を now が超えたら切れ)。未知の timeframe も切れ扱い。"""
        try:
            _, fresh_until = signals.freshness_window(
                claimed["timeframe"], claimed["bar_ts"],
                self.settings.plugin.signal_freshness_bars)
        except KeyError:
            return True
        return self.clock.now() > datetime.fromisoformat(fresh_until)

    def _ask_once_impl(self, question: str) -> str:
        """ask Mission (プラン8 三相再構成 — trade と同じ理由: WorkerRunner
        呼び出しが長時間ブロックしうるため lock を保持しない)。

        **(レビュー 2 周目 codex D4)** `finalize_mission` (finish) が
        失敗しても回答文字列は巻き戻さない。ask は読み取り専用で資金に
        影響しないため許容する (`mission_finalize_failed` の activity
        記録で無警告にはならない)。"""
        mid: int | None = None
        finalized = False
        try:
            with self._core_lock:
                now = self.clock.now()
                prompt = self._build_prompt(load_prompt("ask_mission"), now) \
                    + f"\n\n## ユーザーの質問\n{question}"
                mission = Mission(
                    prompt=prompt, tools=_ASK_TOOLS,
                    output_schema=ANSWER_SCHEMA,
                    max_turns=self.settings.llama_swap.max_turns,
                    timeout_sec=self.settings.llama_swap.timeout_sec)
                mid = missions.start(self.conn, "ask",
                                     self.settings.runner.trade.backend,
                                     self.settings.runner.trade.model, now)

            self.watch.begin(mid, "ask", mission.timeout_sec)
            try:
                try:
                    result = self.runner.run(mission)
                    if not isinstance(result, MissionResult):
                        result = MissionResult("failed", None, [])
                except Exception:  # noqa: BLE001
                    _log.exception("runner raised")
                    result = MissionResult("failed", None, [])
                self._record_market_tool_calls(result, now)
            finally:
                self.watch.end(mid)

            with self._core_lock:
                finalize_mission(self.conn, self.activity, self.clock,
                                 mid, result)
            finalized = True

            if result.status != "completed":
                return f"(Mission 失敗: {result.status})"
            if not isinstance(result.output, dict):
                return "(Mission 失敗: completed)"
            answer = result.output.get("answer")
            if not isinstance(answer, str):
                return "(Mission 失敗: completed)"
            self.activity.write(Category.AGGREGATE, "ask_answered",
                                question[:80]
                                + self._market_tool_calls_suffix(result),
                                ref_id=str(mid))
            return answer
        finally:
            # レビュー 1 周目 codex A3: prepare/run 相の想定外例外
            # (watch.begin/end 等) で mission が running のまま残らない
            # ようにする (trade 経路の finally と同じ形)。
            if mid is not None and not finalized:
                with self._core_lock:
                    finalize_mission(self.conn, self.activity, self.clock,
                                     mid, MissionResult("failed", None, []))

    # ---- Internal -------------------------------------------------------

    def _record_market_tool_calls(self, result: MissionResult,
                                  now: datetime) -> None:
        """Accumulate market tool starts for warning-only observability.

        A start is an allowed registry execution of ``get_ohlcv`` or
        ``get_indicators``. Invalid arguments and failures before provider I/O,
        including planner refusal, still count; this is not an HTTP-request
        counter. UTC calendar date is only an aggregation convention, not a
        measured provider quota boundary. A mission crossing midnight is fully
        attributed to its start date. State is process memory and is discarded
        on restart, so a warning may be emitted again on the same UTC day after
        restart.
        """
        try:
            day = as_utc(now).date()
            state = self._market_tool_daily.get(day)
            if state is None:
                state = {
                    "get_ohlcv": 0, "get_indicators": 0,
                    "unknown_missions": 0, "warned": False,
                }
            self._market_tool_daily = {day: state}
            calls = result.market_tool_calls
            if calls is None:
                state["unknown_missions"] += 1
            else:
                state["get_ohlcv"] += calls.get("get_ohlcv", 0)
                state["get_indicators"] += calls.get("get_indicators", 0)
            total = state["get_ohlcv"] + state["get_indicators"]
            budget = self.settings.datafeed.context_daily_call_budget
            if budget is None or state["warned"] or total <= budget:
                return
            self.activity.write(
                Category.SYSTEM, "market_tool_daily_budget_exceeded",
                f"source={self.settings.datafeed.primary} date={day} "
                f"calls={total} budget={budget} "
                f"unknown_missions={state['unknown_missions']} "
                "(WARNING のみ、tool 呼出しは拒否しない。"
                "プロセス再起動で当日カウンタは 0 に戻る)")
            state["warned"] = True
        except Exception:  # noqa: BLE001 — 観測障害で mission を失敗させない
            _log.exception("market tool call observation failed")

    @staticmethod
    def _market_tool_calls_suffix(result: MissionResult) -> str:
        calls = result.market_tool_calls
        if (not isinstance(calls, dict)
                or any(not isinstance(value, int) or isinstance(value, bool)
                       for value in calls.values())):
            return " market_tool_calls=unknown"
        total = calls.get("get_ohlcv", 0) + calls.get("get_indicators", 0)
        return f" market_tool_calls={total}"

    def _build_prompt(self, system: str, now: datetime) -> str:
        """システムプロンプト + policy + サマリを結合する。"""
        timeframe_section = (
            "## 判断足\n"
            f"decision_timeframe: {self.settings.datafeed.decision_timeframe}\n"
        )
        if self.settings.datafeed.context_timeframes:
            timeframe_section += (
                "context_timeframes: "
                f"{', '.join(self.settings.datafeed.context_timeframes)} "
                "(参考足。発注判断の足ではない。確定足だけ)\n"
            )
        timeframe_section += (
            "signal は strategy の足（例: 1h）でしか更新されず、get_signals は "
            "status を問わず 24h 分を返す。consumed / abandoned の行は再提案しない。"
        )
        utc_now = as_utc(now)
        if not market_hours.is_market_open(utc_now):
            day_horizon = (
                "市場は閉場中。次の day 期限は開始時点では表示しない。"
                "実際の期限は約定時刻で決まる。"
            )
        else:
            rollover = time_cutover.day_close_rollover(
                time_cutover.active_window(self.conn, utc_now), utc_now)
            deadline = rollover - market_hours.DAY_CLOSE_BUFFER
            if utc_now < deadline:
                remaining = int((deadline - utc_now).total_seconds() // 60)
                relative = "翌日 " if deadline.date() > utc_now.date() else ""
                day_horizon = (
                    f"day 建玉の強制決済 {relative}{deadline:%H:%M:%S} UTC "
                    f"(残り {remaining} 分、開始時点の見込み)。"
                    "実際の期限は約定時刻で決まる。"
                )
            else:
                day_horizon = (
                    f"当日の day 期限 {deadline:%H:%M:%S} UTC は経過。"
                    f"いま建てる day 建玉は {rollover:%H:%M} UTC より前に"
                    "約定すれば"
                    "次 tick で強制決済の対象 (quote が取れた時点で実行)、"
                    f"{rollover:%H:%M} 以降の約定は翌営業日の期限。"
                    "開始時点の見込みであり、実際の期限は約定時刻で決まる。"
                )
        parts = [
            system,
            timeframe_section,
            f"## day 建玉期限\n{day_horizon}",
        ]
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
