"""取引判断 loop — Mission 組み立て・実行・記録・executor 連携 (設計書 §5)。

構造 (プラン8 五相/三相再構成 — 設計書 §3.1):
- 公開 run_once/ask_once: サービス境界 (never-raise)
- _run_once_impl: prepare/run/commit-pre/commit-core/commit-post の五相
- _ask_once_impl: prepare/run/commit の三相
- _finalize_mission: missions.finish の CAS 化された共通呼び出し (core_lock
  保持中に呼ぶこと)
"""
from __future__ import annotations

import json
import sqlite3
import threading

from agentic_fx._safe_error import safe_error_text
from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import Settings
from agentic_fx.core.contracts import (
    Action, Clock, IntentParseError, Origin, OrderStatus as S, TradeIntent,
)
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
from agentic_fx.store import intents as intents_store
from agentic_fx.store import missions, signals

import logging

_log = logging.getLogger("agentic_fx.trade_loop")

_TRADE_TOOLS = ["get_ohlcv", "get_indicators", "search_news",
                "get_econ_calendar", "get_positions", "get_account",
                "get_recent_reflections", "search_reflections",
                "get_signals"]


class TradeLoop:
    def __init__(self, *, conn: sqlite3.Connection, runner: AgentRunner,
                 settings: Settings, executor: Executor,
                 provider: PriceProvider, econ: EconCalendar, policy: Policy,
                 activity: ActivityLog, notifier: Notifier,
                 clock: Clock, core_lock: threading.RLock,
                 conn_supervisor: sqlite3.Connection,
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
        self._core_lock = core_lock
        self._conn_supervisor = conn_supervisor
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
        """取引判断 Mission 1 回分 (プラン8 五相再構成 — 設計書 §3.1)。

        prepare (lock 保持) → run (lock 非保持) → commit-pre (lock 非保持:
        結果検証・intent 構築・snapshot 取得) → commit-core (lock 保持:
        consume・Risk Gate・執行・finish) → commit-post (lock 非保持:
        aggregate 記録)。`healthcheck` は prepare よりさらに前 (lock 外)
        — claim 済みで healthcheck 死亡 → requeue 漏れ、を構造的に防ぐ
        (既存の設計方針を維持)。**(裁定書 F-6 / CR-5) `self.provider` は
        `conn_supervisor` (RO 接続) で構築した `readonly=True` の
        `PriceProvider` — `healthcheck` 内部の `get_bars` が呼ぶ
        `ohlcv.upsert_bars` は `readonly=True` によりスキップされるため、
        lock 外のこの呼び出しが `conn_core` を無保護で書き込むことはない
        (Global Constraints 違反の解消)。**
        """
        try:
            self.provider.healthcheck(self.settings.pairs[0])
        except DataUnhealthy as e:
            self.activity.write(Category.SYSTEM, "data_unhealthy", str(e))
            self.notifier.send(f"[agentic-fx] データ不健全のため判断をスキップ: {e}")
            return None

        # レビュー 1 周目 codex A2/A3: 以下の try/finally は
        # `with self._core_lock:` (prepare) の**中**まで含めてメソッド全体を
        # 覆う。以前は `try:` が prepare の外側 (with ブロックの後) にしか
        # 無かったため、claim 成功後の set_trigger/build_prompt/build_mission
        # で例外が出ると finally が一切実行されず、claimed signal も mid も
        # 回収されないまま呼び出し元 (run_once のサービス境界) まで抜けて
        # いた。`finalized` は「_finalize_mission (または missions.finish) を
        # 既に試みたか」を追跡し、finally での二重 finalize (CAS 上は無害だが
        # 無用な mission_finalize_conflict ログを出す) を避ける。
        claimed: dict | None = None
        mid: int | None = None
        consumed = False
        finalized = False
        try:
            # ---- prepare (core_lock 保持) ----
            with self._core_lock:
                now = self.clock.now()
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
                            finalized = True
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
                        else:
                            finalized = True
                        raise
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

            # ---- run (core_lock 非保持) ----
            self.watch.begin(mid, "trade", mission.timeout_sec)
            try:
                result = self.runner.run(mission)
                if not isinstance(result, MissionResult):
                    result = MissionResult("failed", None, [])
            except Exception:  # noqa: BLE001 — runner 例外で周期を殺さない
                _log.exception("runner raised")
                result = MissionResult("failed", None, [])
            finally:
                self.watch.end(mid)

            # ---- commit-pre (core_lock 非保持) ----
            if result.status != "completed":
                with self._core_lock:
                    self._finalize_mission(mid, result)
                finalized = True
                self.activity.write(Category.AGGREGATE, "mission_failed",
                                    f"runner status={result.status}",
                                    ref_id=str(mid))
                self.notifier.send(f"[agentic-fx] 判断 Mission 失敗: {result.status}")
                return None
            try:
                intent = TradeIntent.from_llm_dict(result.output,
                                                   origin=Origin.SCHEDULER)
            except IntentParseError as e:
                with self._core_lock:
                    self._finalize_mission(mid, result)
                finalized = True
                self.activity.write(Category.AGGREGATE, "intent_parse_failed",
                                    str(e), ref_id=str(mid))
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
                    if claimed is not None:
                        # ⑥consume/requeue の確定規則: パース成功の時点で
                        # consume する (プロンプトに実際に載せた Mission が
                        # 確定できる)。executor 実行後の requeue は二重発注
                        # ハザードになるため、ここで確定させる。
                        #
                        # **(レビュー 2 周目 codex D3)** `signals.consume`
                        # は fail-closed (CAS 不一致で ValueError) であり、
                        # `try:` の**内側**に置かなければならない。外側に
                        # あると、この例外だけが `with self._core_lock,
                        # self.executor.defer_notifications():` ブロック
                        # 全体を素通りし、`record_and_validate_intent` が
                        # 一度も呼ばれないまま (= `trade_intents` に行が
                        # 残らないまま) 汎用の公開境界例外になってしまう
                        # (completed した有効な intent が DB に理由の痕跡を
                        # 一切残さず捨てられる)。consume が失敗したら
                        # `consumed` は False のままにし (下の except で
                        # 設定しない)、finally の requeue に委ねる。
                        signals.consume(self.conn, claimed["id"],
                                        mission_id=mid, now=self.clock.now())
                        consumed = True
                    iid, early = self.executor.record_and_validate_intent(
                        intent, mid)
                    if snapshot_error is not None:
                        # **(Task 14 レビュー 2 周からの申し送りの回収)**
                        # commit-pre の外部取得失敗を、ライブ経路 (`_open` の
                        # `except DataUnhealthy`) と**同じ形の「記録済み gate
                        # 拒否」**に変換する。ここを汎用の
                        # `intent_execution_failed` にすると
                        # `trade_intents.gate_result` が NULL のまま残り、
                        # 「なぜ発注されなかったか」を DB から追えない。
                        #
                        # `record_and_validate_intent` を**先に**呼ぶのは
                        # `iid` を得るため (intent の記録自体は常に行う契約)。
                        # early (hold/origin/mission 拒否) が確定している
                        # ケースはそちらを優先する — snapshot は使わない。
                        if early is not None:
                            out = early
                        else:
                            reasons = ["execution snapshot unavailable: "
                                       + safe_error_text(snapshot_error)]
                            intents_store.set_gate_result(
                                self.conn, iid, accepted=False,
                                reject_reason=reasons[0])
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
                    self._finalize_mission(mid, result)
                    finalized = True
                    self.activity.write(Category.AGGREGATE,
                                        "intent_execution_failed",
                                        safe_error_text(e), ref_id=str(mid))
                    # **`self.notifier.send` をここで呼んではならない** —
                    # まだ core_lock 保持中であり、urlopen(timeout=10) で
                    # 最大 10 秒ブロックする。commit-post まで遅延させ、
                    # `return` せずに with を抜けてから送る (Step 3.5)。
                    deferred.append(
                        f"[agentic-fx] 注文処理失敗: {safe_error_text(e)}")
                    out = None
                else:
                    self._finalize_mission(mid, result)
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
            if out is None:
                return None
            self.activity.write(Category.AGGREGATE, "decision",
                                f"{intent.action.value} -> {out['result']}",
                                ref_id=str(mid))
            return out
        finally:
            # 例外時も claimed のまま残さない (lease 回収を待たず即 requeue)。
            # consume 済みならここでは何もしない (二重発注ハザードを避ける)。
            # 裁定書 F-6 (CR-5): _requeue_signal は signals.requeue 経由で
            # conn_core を書き込むため、core_lock を保持中に呼ぶ (Global
            # Constraints 違反の解消 — この finally はメソッド全体の
            # try に対するものであり、commit-core の with ブロックは
            # 例外伝播時点で既に解放済みなので RLock の再取得は安全)。
            # レビュー 1 周目 codex A1: 通知の送信 (urlopen(timeout=10)) は
            # lock 解放後に行う — _requeue_signal はもう送信しない
            # (文面を返すだけ)。
            if claimed is not None and not consumed:
                with self._core_lock:
                    _requeue_msg = self._requeue_signal(claimed)
                if _requeue_msg:
                    try:
                        self.notifier.send(_requeue_msg)
                    except Exception:  # noqa: BLE001 — 通知失敗で本流を止めない
                        _log.exception("requeue notification failed")
            # レビュー 1 周目 codex A3: run/commit-pre 相 (watch.begin/end・
            # TradeIntent 解析想定外例外・_read_exposure_pairs/
            # _read_close_row 等) で想定外の例外が出ても、mid が既に
            # 発行されている限り mission を running のまま残さない
            # (fail closed)。`missions.finish` は CAS 化されているため、
            # 既に (正常経路で) finalize 済みなら二重呼び出しは無害だが、
            # 無用なログを避けるため `finalized` で一度限りにする。
            if mid is not None and not finalized:
                with self._core_lock:
                    self._finalize_mission(
                        mid, MissionResult("failed", None, []))

    def _finalize_mission(self, mid: int, result: MissionResult) -> None:
        """missions.finish の CAS 化された呼び出し (**core_lock 保持中に
        呼ぶこと**)。設計書 §4.7 codex C-4: 二重終端は上書きせず警告のみ
        残す。

        **(レビュー 2 周目 codex D4 — docstring 訂正)** finish 失敗時も
        執行 (発注等) は巻き戻さない (設計書 §3.1 が finalize を
        commit-core の**末尾** — paper broker 執行の後 — に置くため。
        Task 15 節「⚠ 着手前検証の結果 (4)」で明示的に許容された意図的な
        挙動変更で、旧 `_run_recorded` の「finish 失敗時は result を
        failed に差し替え、呼び出し元を completed 系の分岐に進ませない」
        契約はもう維持していない)。監査未確定は fail closed にはせず、
        `mission_finalize_failed` の activity 記録で可視化したうえで、
        mission 行は `running` のまま残し、次回起動時の
        `recover_interrupted` による `interrupted` 回収に委ねる
        (無警告の再発防止という旧契約の目的そのものは、activity 記録と
        recover_interrupted の組合せで別の形で担保している)。

        `_ask_once_impl` も同じ契約 (finish 失敗時に回答文字列を巻き戻さ
        ない) を採用している — ask は読み取り専用で資金に影響しないため
        許容される (同 Step の判断)。"""
        try:
            finished = missions.finish(self.conn, mid, result.status,
                                       result.output, result.transcript,
                                       self.clock.now())
        except Exception:  # noqa: BLE001
            _log.exception("missions.finish failed for %s", mid)
            try:
                self.activity.write(Category.SYSTEM, "mission_finalize_failed",
                                    f"mid={mid}")
            except Exception:  # noqa: BLE001
                _log.exception("failed to record mission_finalize_failed")
            return
        if not finished:
            try:
                self.activity.write(
                    Category.SYSTEM, "mission_finalize_conflict",
                    f"mid={mid} (already finalized elsewhere)")
            except Exception:  # noqa: BLE001
                _log.exception("failed to record mission_finalize_conflict")

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

    def _requeue_signal(self, claimed: dict) -> str | None:
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

    def _ask_once_impl(self, question: str) -> str:
        """ask Mission (プラン8 三相再構成 — trade と同じ理由: WorkerRunner
        呼び出しが長時間ブロックしうるため lock を保持しない)。

        **(レビュー 2 周目 codex D4)** `_finalize_mission` (finish) が
        失敗しても回答文字列は巻き戻さない — `_finalize_mission` の
        docstring 参照。ask は読み取り専用で資金に影響しないため許容する
        (`mission_finalize_failed` の activity 記録で無警告にはならない)。"""
        mid: int | None = None
        finalized = False
        try:
            with self._core_lock:
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

            self.watch.begin(mid, "ask", mission.timeout_sec)
            try:
                result = self.runner.run(mission)
                if not isinstance(result, MissionResult):
                    result = MissionResult("failed", None, [])
            except Exception:  # noqa: BLE001
                _log.exception("runner raised")
                result = MissionResult("failed", None, [])
            finally:
                self.watch.end(mid)

            with self._core_lock:
                self._finalize_mission(mid, result)
            finalized = True

            if result.status != "completed":
                return f"(Mission 失敗: {result.status})"
            if not isinstance(result.output, dict):
                return "(Mission 失敗: completed)"
            answer = result.output.get("answer")
            if not isinstance(answer, str):
                return "(Mission 失敗: completed)"
            self.activity.write(Category.AGGREGATE, "ask_answered",
                                question[:80], ref_id=str(mid))
            return answer
        finally:
            # レビュー 1 周目 codex A3: prepare/run 相の想定外例外
            # (watch.begin/end 等) で mission が running のまま残らない
            # ようにする (trade 経路の finally と同じ形)。
            if mid is not None and not finalized:
                with self._core_lock:
                    self._finalize_mission(
                        mid, MissionResult("failed", None, []))

    # ---- Internal -------------------------------------------------------

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
