"""Executor — intent の全件記録・origin 検証・gate・発注経路 (設計書 §5)。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from agentic_fx._safe_error import safe_error_text
from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import Settings
from agentic_fx.core import accounting, transitions
from agentic_fx.core.contracts import (
    Action, BrokerResult, Clock, ConversionRate, InstrumentSpec, Origin,
    OrderStatus as S, Quote, TradeIntent,
)
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker, compute_pnl
from agentic_fx.core.risk_gate import GateContext, evaluate
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.store import intents as intents_store
from agentic_fx.store import missions as missions_store
from agentic_fx.store import orders
from agentic_fx.store.state import StateStore

# broker 上で存在を否定できない全状態 (保守的にリスク予約へ算入)
_EXPOSURE = (S.OPEN, S.PENDING_FILL, S.PROTECTION_PENDING, S.SUBMITTING,
             S.SUBMITTED, S.CLOSING, S.CANCELLING, S.SUBMIT_UNKNOWN,
             S.CANCEL_UNKNOWN, S.CLOSE_UNKNOWN)
# レビュー修正 (Task 10 fix 2): CLOSING は「broker 側は close 済みかもしれないが
# DB 未確定」の状態であり、quote 障害等で滞留しうる。これを「未解決」扱いから
# 除外すると gate が新規発注を許可し続けてしまうため、SUBMIT/CANCEL/CLOSE の
# unknown と同じ扱いにする (has_unresolved_unknown に含める)。
_UNKNOWN = (S.SUBMIT_UNKNOWN, S.CANCEL_UNKNOWN, S.CLOSE_UNKNOWN, S.CLOSING)


def open_risk_and_notional(
        conn: sqlite3.Connection, spec_fn: Callable[[str], InstrumentSpec],
        risk, rate_fn: Callable[[str], ConversionRate],
) -> tuple[float, float, int]:
    """既存の予約・建玉の総リスク・総 notional (いずれも口座通貨建て)。

    設計書 §5「口座通貨と換算」: 損失・リスク額はクォート通貨建てなので
    `rate_fn(spec.quote_currency)` で口座通貨へ換算する。commission_per_lot
    は口座通貨建て (config で明記) のため換算しない。notional はベース通貨の
    想定元本 (`qty × contract_size`、価格は掛けない) を
    `rate_fn(spec.base_currency)` で換算する。`rate_fn` は呼び出し側が
    1 回の判断 (gate 評価・予約再検証サイクル) 内で固定したスナップショット
    (同一通貨は 1 回だけ取得) を渡すこと — ここでは呼び出し回数を制御しない。
    """
    total_risk = total_notional = 0.0
    rows = orders.list_by_status(conn, *_EXPOSURE)
    for r in rows:
        spec = spec_fn(r["pair"])
        rule = risk.pair_rules.get(r["pair"])
        spread = rule.assumed_spread_pips * spec.pip_size if rule else 0.0
        entry = r["avg_fill_price"] or r["requested_price"] or 0.0
        qty = r["quantity"] or 0.0
        quote_rate = rate_fn(spec.quote_currency)
        total_risk += (abs(entry - (r["stop_loss"] or entry)) + spread) \
            * spec.contract_size * qty * quote_rate.value \
            + risk.commission_per_lot * qty
        base_rate = rate_fn(spec.base_currency)
        total_notional += qty * spec.contract_size * base_rate.value
    return total_risk, total_notional, len(rows)


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    """commit-pre 相が集めた Risk Gate 評価用の外部取得スナップショット
    (設計書 §3.1)。`captured_at` は commit-core の鮮度再検証が使う。"""
    quote: Quote
    spec: InstrumentSpec
    specs_by_pair: dict
    rates: dict
    captured_at: datetime


class SnapshotCoverageError(Exception):
    """commit-core 開始時点の exposure がスナップショットでカバーされて
    いない (設計書 §12 申し送り① N4-2 — commit-pre と commit-core の間に
    新規 exposure が確定した場合)。lock 内で再取得せず intent 拒否する。"""


@dataclass(frozen=True, slots=True)
class CloseSnapshot:
    """commit-pre 相が集めた CLOSE 用の外部取得スナップショット (裁定書
    F-1 / CR-2 / P8-01)。`captured_at` は commit-core の鮮度再検証が使う。"""
    price: float
    spec: InstrumentSpec
    rate: ConversionRate | None
    rate_degraded: bool
    captured_at: datetime


def open_risk_and_notional_from_snapshot(
        conn: sqlite3.Connection, risk,
        snapshot: ExecutionSnapshot) -> tuple[float, float, int]:
    """`open_risk_and_notional` の DB-only 版 (commit-core 専用 — 外部
    I/O を一切行わない)。exposure 行の pair/通貨がスナップショットに
    無ければ `SnapshotCoverageError` (N4-2)。"""
    total_risk = total_notional = 0.0
    rows = orders.list_by_status(conn, *_EXPOSURE)
    for r in rows:
        pair = r["pair"]
        spec = snapshot.specs_by_pair.get(pair)
        if spec is None:
            raise SnapshotCoverageError(
                f"pair {pair!r} is not covered by the execution snapshot "
                "(exposure grew after commit-pre — N4-2)")
        if (spec.quote_currency not in snapshot.rates
                or spec.base_currency not in snapshot.rates):
            raise SnapshotCoverageError(
                f"currency for pair {pair!r} is not covered by the "
                "execution snapshot (exposure grew after commit-pre — N4-2)")
        rule = risk.pair_rules.get(pair)
        spread = rule.assumed_spread_pips * spec.pip_size if rule else 0.0
        entry = r["avg_fill_price"] or r["requested_price"] or 0.0
        qty = r["quantity"] or 0.0
        quote_rate = snapshot.rates[spec.quote_currency]
        total_risk += (abs(entry - (r["stop_loss"] or entry)) + spread) \
            * spec.contract_size * qty * quote_rate.value \
            + risk.commission_per_lot * qty
        base_rate = snapshot.rates[spec.base_currency]
        total_notional += qty * spec.contract_size * base_rate.value
    return total_risk, total_notional, len(rows)


def has_unresolved_unknown(conn: sqlite3.Connection) -> bool:
    return bool(orders.list_by_status(conn, *_UNKNOWN))


class Executor:
    def __init__(self, *, conn: sqlite3.Connection, broker: PaperBroker,
                 settings: Settings, state_store: StateStore,
                 activity: ActivityLog, notifier: Notifier, clock: Clock,
                 quote_fn: Callable[[str], Quote],
                 spec_fn: Callable[[str], InstrumentSpec],
                 rate_fn: Callable[[str, str, datetime], ConversionRate],
                 ) -> None:
        self.conn = conn
        self.broker = broker
        self.settings = settings
        self.state = state_store
        self.activity = activity
        self.notifier = notifier
        self.clock = clock
        self.quote_fn = quote_fn
        self.spec_fn = spec_fn
        # 通貨 1 単位 = 口座通貨いくらか (設計書 §5)。quote_fn/spec_fn と同じ
        # 注入点の作法 (PriceProvider.to_account_rate を呼び出し側が
        # account_currency/max_skew_min を束縛して渡す想定)。
        self.rate_fn = rate_fn
        # 「最後に健全性検証を通ったレート」キャッシュ (クローズ経路専用 —
        # 設計書 §5: クローズはレート欠損でも妨げない。**予約再検証・
        # mark-to-market・gate 評価には使わない** — それらは判断の精度が
        # 目的であり、恒久的な会計精度は定期同期が担う (設計書 §5)。
        # このキャッシュは tick/判断をまたいで永続する意図的な例外。
        self._last_good_rate: dict[tuple[str, str], ConversionRate] = {}

    # ---- 換算レート -------------------------------------------------------

    def cycle_rate_fn(self, now) -> Callable[[str], ConversionRate]:
        """1 回の判断 (gate 評価・予約再検証・mark-to-market サイクル) 内で
        レートを固定するローカルキャッシュを返す (設計書 §5)。呼び出し側は
        `self` に保持せず、その判断の間だけ使い捨てること — 永続させると
        「予約再検証は現在レートで行う」という運用方針に反する。

        成功した取得は `_last_good_rate` (クローズ経路の degraded フォール
        バック専用) も更新する。

        fix round 2 (codex 節目レビュー): 各 `ConversionRate` は自分の脚と
        `reference_ts=now` の差だけを検証する (`to_account_rate` /
        `validate_conversion_skew`) が、`validate_quote` の鮮度窓が
        `[now-freshness, now+2min]` を許すため、**この判断で使う複数の
        レートを跨いだ全体の時刻差**は個別検証だけでは捕まえられない
        (脚 A=now+2min と 脚 B=now-5min はどちらも個別には健全だが、
        両者が同一判断に混在すると全体では 7min 開く)。設計書 §5 ③
        「1 回の判断内のスナップショット全体の時刻差」はこの判断
        (=この `cycle_rate_fn` 呼び出し) で観測した**全レートの leg 時刻の
        running min/max** を見る必要がある。新しいレートを取得するたびに
        その leg 時刻を取り込み、全体 span が `conversion_skew_max_min` を
        超えたら (個々のレート自体は健全でも) `DataUnhealthy` で fail
        closed する。超過を検出したレートはこのサイクルのキャッシュには
        入れない (このサイクル内で確定した「使ってよいレート」ではない)。

        層別動作は変えない: この関数が送出する `DataUnhealthy` は、既存の
        レート不能時の各層の扱い (sizing→SizingError / gate→却下 /
        予約再検証→当該ペアの pending_fill 取消 / mark-to-market→
        snapshot 見送り) にそのまま乗る — 呼び出し側は個別レートの
        鮮度失敗とこの全体 skew 失敗を区別しない (どちらも
        「この判断ではこの通貨の換算が使えない」という同じ意味)。

        予約再検証のペア単位取消における順序依存 (advisor/コードレビュー
        指摘): 複数ペアを跨ぐ判断では、どのペアの取消要求が「span を破った
        瞬間」になるかはペアの処理順に依存する — 先に処理されたペアの
        レートが running span の基準を作り、後から処理されたペアの
        レートがその基準を破れば**後から処理された方**が取消対象になる
        (呼び出し元の `_cancel_pairs_with_unavailable_rate` が
        `sorted(pairs)` で処理順を固定しているため、決定的に再現可能)。
        先に確定した (キャッシュ済みの) ペアは取消されない — 「当該ペアの
        pending_fill のみ取消・他ペアは継続」という既存セマンティクスを
        保つ。報告書 fix round 2 節に根拠を記載。

        fix round 3 (codex 節目レビュー再検証 — 新規 Important): 上記の
        「running span」は**候補値で判定してから確定する**必要がある。
        拒否したレートの leg 時刻を確定済み span (`span_min`/`span_max`)
        に混ぜてしまうと、その外れ値が以後ずっと running span に居座り、
        後続の (本来は健全な) 通貨の要求まで巻き添えで拒否され続ける
        cascade になる (実測: pending 3 ペア = 健全 / 外れ値 / 健全・
        1 ペア目と同時刻、で 3 ペア目まで取消され、エラーメッセージも
        真の外れ値でない通貨を「offending currency」と誤指名した)。
        これは設計書 §5 の「取消はペア単位に限定する (他ペアの予約は
        健全なレートで再検証を継続する)」に反する。修正:
        `span_min`/`span_max` は**このレートを受理する場合の候補値**として
        別変数 (`candidate_min`/`candidate_max`) で計算し、候補が
        `max_skew` 以内のときだけ `span_min`/`span_max`（確定値）と
        `cache[ccy]` を更新する。拒否したレートの leg 時刻は確定値に
        一切混ざらない。
        """
        cache: dict[str, ConversionRate] = {}
        account_ccy = self.settings.account_currency
        max_skew = timedelta(
            minutes=self.settings.datafeed.conversion_skew_max_min)
        span_min: datetime | None = None
        span_max: datetime | None = None

        def fn(ccy: str) -> ConversionRate:
            nonlocal span_min, span_max
            if ccy not in cache:
                rate = self.rate_fn(ccy, account_ccy, now)
                # 個別に健全と確認できたレートなので degraded フォール
                # バック用キャッシュは (全体 skew の成否に関わらず) 更新
                # する — resolve_close_rate はこの全体 skew 検証の対象外
                # (設計書 §5: クローズはレート欠損でも妨げない)。
                self._last_good_rate[(ccy, account_ccy)] = rate
                # fix round 3: 確定値 (span_min/span_max) はまだ書き換え
                # ない。この呼び出しを受理する場合の候補値だけを計算する。
                candidate_min, candidate_max = span_min, span_max
                for ts in rate.leg_ts:
                    candidate_min = (
                        ts if candidate_min is None else min(candidate_min, ts))
                    candidate_max = (
                        ts if candidate_max is None else max(candidate_max, ts))
                if candidate_max - candidate_min > max_skew:
                    raise DataUnhealthy(
                        f"conversion snapshot skew "
                        f"{candidate_max - candidate_min} exceeds "
                        f"{max_skew} across this decision's rates "
                        f"(offending currency: {ccy})")
                # 受理した場合のみ確定値を進める。拒否した呼び出しの leg
                # 時刻はここに一切反映されない (cascade を防ぐ)。
                span_min, span_max = candidate_min, candidate_max
                cache[ccy] = rate
            return cache[ccy]
        return fn

    def resolve_close_rate(self, ccy: str,
                           now) -> tuple[ConversionRate | None, bool]:
        """クローズ専用のレート解決。現在レートが取れなければ最後に健全性
        検証を通ったレートへ degraded フォールバックする (設計書 §5:
        クローズはレート欠損でも妨げない)。戻り値は (rate, degraded)。
        rate が None なのは、一度も健全なレートを観測できていない場合のみ
        (プロセス起動直後の初回クローズ等) — この場合 realized_pnl は
        未確定のまま残し、次回の定期同期で解消する。
        """
        account_ccy = self.settings.account_currency
        try:
            rate = self.rate_fn(ccy, account_ccy, now)
            self._last_good_rate[(ccy, account_ccy)] = rate
            return rate, False
        except Exception:  # noqa: BLE001 — クローズを止めない (設計書 §5)
            return self._last_good_rate.get((ccy, account_ccy)), True

    # ---- public ---------------------------------------------------------

    def handle_intent(self, intent: TradeIntent, mission_id: int) -> dict:
        now = self.clock.now()
        iid = intents_store.insert(self.conn, mission_id,
                                   _intent_payload(intent), now)
        if intent.action is Action.HOLD:
            self.activity.write(Category.AGGREGATE, "hold",
                                intent.reasoning[:120], ref_id=str(iid))
            return {"result": "hold", "order_id": None, "reasons": []}

        # 設計書 §5: origin と mission_id を独立に検証する。origin は呼び出し
        # 側が渡す enum 値に過ぎず、任意の内部コードが Origin.SCHEDULER を
        # 構成できるため、origin 単独では「scheduler が起動した取引判断
        # Mission の出力である」性質を担保できない (codex レビュー 4)。
        # mission_id は DB で照合できるので、loop='trade' を併せて検証する。
        if intent.origin is not Origin.SCHEDULER:
            reasons = ["origin rejected: only scheduler missions may trade"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason="; ".join(reasons))
            self.activity.write(Category.TRADE, "origin_rejected",
                                f"{intent.action.value} from {intent.origin.value}",
                                ref_id=str(iid))
            return {"result": "rejected", "order_id": None, "reasons": reasons}

        loop = missions_store.loop_of(self.conn, mission_id)
        if loop != "trade":
            reasons = [f"mission rejected: loop={loop!r} is not a trade mission"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason="; ".join(reasons))
            self.activity.write(Category.TRADE, "mission_rejected",
                                f"{intent.action.value} from mission "
                                f"#{mission_id} (loop={loop!r})", ref_id=str(iid))
            return {"result": "rejected", "order_id": None, "reasons": reasons}

        if intent.action is Action.OPEN:
            return self._open(intent, iid)
        if intent.action is Action.CLOSE:
            return self._close(intent, iid)
        return self._cancel(intent, iid)

    # ---- open -----------------------------------------------------------

    def _open(self, intent: TradeIntent, iid: int) -> dict:
        now = self.clock.now()
        quote = self.quote_fn(intent.pair)
        spec = self.spec_fn(intent.pair)
        account = accounting.current_account(self.conn, now)
        if account is None:
            reasons = ["no fresh account snapshot (fail closed)"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            self.activity.write(Category.TRADE, "gate_rejected", reasons[0],
                                ref_id=str(iid))
            return {"result": "rejected", "order_id": None, "reasons": reasons}
        equity, hwm = account
        # 1 回の gate 評価内でレートを固定する (設計書 §5)。既存集計
        # (open_risk_and_notional) とこの intent 自身の換算に同じ
        # スナップショットを使う — 途中で取り直さない。
        cycle_rate = self.cycle_rate_fn(now)
        try:
            risk_total, notional, count = open_risk_and_notional(
                self.conn, self.spec_fn, self.settings.risk, cycle_rate)
            quote_to_account = cycle_rate(spec.quote_currency)
            base_to_account = cycle_rate(spec.base_currency)
        except DataUnhealthy as e:
            reasons = [f"conversion rate unavailable: {safe_error_text(e)}"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            self.activity.write(Category.TRADE, "gate_rejected", reasons[0],
                                ref_id=str(iid))
            return {"result": "rejected", "order_id": None, "reasons": reasons}
        ctx = GateContext(
            quote=quote, spec=spec, equity=equity, hwm=hwm,
            daily_start_equity=accounting.daily_start_equity(self.conn, now),
            open_position_count=count, existing_risk_account=risk_total,
            existing_notional_account=notional,
            kill_switch_latched=self.state.load().kill_switch_latched,
            has_unresolved_unknown=has_unresolved_unknown(self.conn),
            account_currency=self.settings.account_currency,
            quote_to_account=quote_to_account,
            base_to_account=base_to_account,
            now=now)
        result = evaluate(intent, ctx, self.settings.risk)
        if not result.accepted:
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason="; ".join(result.reasons))
            if any("kill switch" in r and "latched" not in r
                   for r in result.reasons):
                self.state.update(kill_switch_latched=True)
                self.activity.write(Category.SYSTEM, "kill_switch_latched",
                                    "drawdown threshold hit — 新規停止 (解除は明示操作)")
            self.activity.write(Category.TRADE, "gate_rejected",
                                "; ".join(result.reasons)[:200], ref_id=str(iid))
            return {"result": "rejected", "order_id": None,
                    "reasons": result.reasons}

        intents_store.set_gate_result(self.conn, iid, accepted=True,
                                      reject_reason=None)
        is_market = intent.entry_type.value == "market"
        oid = orders.insert(
            self.conn, pair=intent.pair, direction=intent.direction.value,
            entry_type=intent.entry_type.value, horizon=intent.horizon.value,
            status=S.SUBMITTING, now=now, intent_id=iid,
            client_order_id=f"afx-{iid}-{now.timestamp():.0f}",
            quantity=result.size.quantity,
            remaining_quantity=result.size.quantity,
            requested_price=result.entry_price,
            stop_loss=intent.stop_loss, take_profit=intent.take_profit,
            expires_at=(now + timedelta(hours=intent.expires_in_h)).isoformat()
            if intent.expires_in_h else None)
        row = orders.get(self.conn, oid)
        # レビュー修正 (codex 2): タイムアウト等の broker 例外は「結果不明」
        # として扱う (設計書 §12)。Phase 3 の MT5 実装で必ず起きる経路。
        try:
            br = self.broker.submit(row, entry_price=result.entry_price)
        except Exception as e:  # noqa: BLE001
            br = BrokerResult(status="unknown",
                              message=safe_error_text(e))
        if br.status == "rejected":
            transitions.transition(self.conn, oid, S.REJECTED, now)
            self.activity.write(Category.TRADE, "broker_rejected",
                                f"{intent.pair}", ref_id=str(oid))
            return {"result": "rejected", "order_id": oid,
                    "reasons": ["broker rejected"]}
        if br.status == "unknown":
            transitions.transition(self.conn, oid, S.SUBMIT_UNKNOWN, now)
            self.activity.write(Category.TRADE, "submit_unknown",
                                f"{intent.pair} — reconcile 待ち", ref_id=str(oid))
            self.notifier.send(f"[agentic-fx] 送信結果不明 #{oid} — "
                               "解決まで新規発注停止")
            return {"result": "unknown", "order_id": oid, "reasons": []}
        transitions.transition(self.conn, oid, S.SUBMITTED, now,
                               broker_order_id=br.broker_order_id,
                               broker_position_id=br.broker_position_id)
        if is_market:
            transitions.transition(self.conn, oid, S.PROTECTION_PENDING, now,
                                   avg_fill_price=result.entry_price,
                                   filled_quantity=result.size.quantity,
                                   remaining_quantity=0.0,
                                   filled_at=now.isoformat())
            transitions.transition(self.conn, oid, S.OPEN, now)  # paper: 保護は常に成功
            self.activity.write(Category.TRADE, "order_opened",
                                f"{intent.pair} {intent.direction.value} "
                                f"{result.size.quantity}lot @{result.entry_price}",
                                ref_id=str(oid))
            return {"result": "opened", "order_id": oid, "reasons": []}
        transitions.transition(self.conn, oid, S.PENDING_FILL, now)
        self.activity.write(Category.TRADE, "limit_placed",
                            f"{intent.pair} {intent.direction.value} "
                            f"{result.size.quantity}lot @{result.entry_price}",
                            ref_id=str(oid))
        return {"result": "pending", "order_id": oid, "reasons": []}

    def _evaluate_and_execute_open(self, intent: TradeIntent, iid: int,
                                   ctx: GateContext) -> dict:
        """`_open`/`open_from_snapshot` の共有末尾 (判定ロジック不変—
        Global Constraints: risk_gate は diff ゼロ)。既存 `_open` の
        `result = evaluate(...)` 以降を逐語移動しただけ。"""
        result = evaluate(intent, ctx, self.settings.risk)
        if not result.accepted:
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason="; ".join(result.reasons))
            if any("kill switch" in r and "latched" not in r
                   for r in result.reasons):
                self.state.update(kill_switch_latched=True)
                self.activity.write(Category.SYSTEM, "kill_switch_latched",
                                    "drawdown threshold hit — 新規停止 (解除は明示操作)")
            self.activity.write(Category.TRADE, "gate_rejected",
                                "; ".join(result.reasons)[:200], ref_id=str(iid))
            return {"result": "rejected", "order_id": None,
                    "reasons": result.reasons}

        intents_store.set_gate_result(self.conn, iid, accepted=True,
                                      reject_reason=None)
        is_market = intent.entry_type.value == "market"
        now = ctx.now
        oid = orders.insert(
            self.conn, pair=intent.pair, direction=intent.direction.value,
            entry_type=intent.entry_type.value, horizon=intent.horizon.value,
            status=S.SUBMITTING, now=now, intent_id=iid,
            client_order_id=f"afx-{iid}-{now.timestamp():.0f}",
            quantity=result.size.quantity,
            remaining_quantity=result.size.quantity,
            requested_price=result.entry_price,
            stop_loss=intent.stop_loss, take_profit=intent.take_profit,
            expires_at=(now + timedelta(hours=intent.expires_in_h)).isoformat()
            if intent.expires_in_h else None)
        row = orders.get(self.conn, oid)
        try:
            br = self.broker.submit(row, entry_price=result.entry_price)
        except Exception as e:  # noqa: BLE001
            br = BrokerResult(status="unknown",
                              message=safe_error_text(e))
        if br.status == "rejected":
            transitions.transition(self.conn, oid, S.REJECTED, now)
            self.activity.write(Category.TRADE, "broker_rejected",
                                f"{intent.pair}", ref_id=str(oid))
            return {"result": "rejected", "order_id": oid,
                    "reasons": ["broker rejected"]}
        if br.status == "unknown":
            transitions.transition(self.conn, oid, S.SUBMIT_UNKNOWN, now)
            self.activity.write(Category.TRADE, "submit_unknown",
                                f"{intent.pair} — reconcile 待ち", ref_id=str(oid))
            self.notifier.send(f"[agentic-fx] 送信結果不明 #{oid} — "
                               "解決まで新規発注停止")
            return {"result": "unknown", "order_id": oid, "reasons": []}
        transitions.transition(self.conn, oid, S.SUBMITTED, now,
                               broker_order_id=br.broker_order_id,
                               broker_position_id=br.broker_position_id)
        if is_market:
            transitions.transition(self.conn, oid, S.PROTECTION_PENDING, now,
                                   avg_fill_price=result.entry_price,
                                   filled_quantity=result.size.quantity,
                                   remaining_quantity=0.0,
                                   filled_at=now.isoformat())
            transitions.transition(self.conn, oid, S.OPEN, now)  # paper: 保護は常に成功
            self.activity.write(Category.TRADE, "order_opened",
                                f"{intent.pair} {intent.direction.value} "
                                f"{result.size.quantity}lot @{result.entry_price}",
                                ref_id=str(oid))
            return {"result": "opened", "order_id": oid, "reasons": []}
        transitions.transition(self.conn, oid, S.PENDING_FILL, now)
        self.activity.write(Category.TRADE, "limit_placed",
                            f"{intent.pair} {intent.direction.value} "
                            f"{result.size.quantity}lot @{result.entry_price}",
                            ref_id=str(oid))
        return {"result": "pending", "order_id": oid, "reasons": []}

    def gather_open_snapshot(self, intent: TradeIntent, *,
                             exposure_pairs: list[str]) -> ExecutionSnapshot:
        """commit-pre 相専用 (設計書 §3.1) — **core_lock を保持しない状態
        で呼ぶこと**。Risk Gate 評価に要る全外部取得 (quote + 全 exposure
        pair の instrument spec + 全 exposure 通貨の換算レート) を 1 回で
        完了させ、timestamp 付きスナップショットにする。

        `exposure_pairs` は呼び出し元 (commit-pre 相) が `conn_supervisor`
        (lock 外の読取専用接続) から読んだ既存 exposure の pair 一覧。
        """
        now = self.clock.now()
        quote = self.quote_fn(intent.pair)
        spec = self.spec_fn(intent.pair)
        cycle_rate = self.cycle_rate_fn(now)
        specs_by_pair: dict = {intent.pair: spec}
        currencies: set = {spec.quote_currency, spec.base_currency}
        for pair in exposure_pairs:
            pair_spec = self.spec_fn(pair)
            specs_by_pair[pair] = pair_spec
            currencies.add(pair_spec.quote_currency)
            currencies.add(pair_spec.base_currency)
        rates = {ccy: cycle_rate(ccy) for ccy in currencies}
        return ExecutionSnapshot(quote=quote, spec=spec,
                                 specs_by_pair=specs_by_pair, rates=rates,
                                 captured_at=now)

    def open_from_snapshot(self, intent: TradeIntent, iid: int,
                           snapshot: ExecutionSnapshot, *,
                           max_snapshot_age_sec: float) -> dict:
        """commit-core 相専用 (設計書 §3.1) — **core_lock 保持中に呼ぶ
        こと**。①スナップショットの鮮度再検証 (lock 内での再取得はしない)
        ②DB 状態を読み直して GateContext を確定 ③Risk Gate 判定・paper
        broker 執行は `_evaluate_and_execute_open` へ委譲 (`_open` と
        完全共有 — 判定ロジック不変)。
        """
        now = self.clock.now()
        age_sec = (now - snapshot.captured_at).total_seconds()
        if age_sec > max_snapshot_age_sec:
            reasons = [
                f"execution snapshot is stale ({age_sec:.1f}s > "
                f"{max_snapshot_age_sec}s) — rejecting rather than "
                "re-fetching while holding core_lock (設計書 §3.1)"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            self.activity.write(Category.TRADE, "gate_rejected", reasons[0],
                                ref_id=str(iid))
            return {"result": "rejected", "order_id": None, "reasons": reasons}

        account = accounting.current_account(self.conn, now)
        if account is None:
            reasons = ["no fresh account snapshot (fail closed)"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            self.activity.write(Category.TRADE, "gate_rejected", reasons[0],
                                ref_id=str(iid))
            return {"result": "rejected", "order_id": None, "reasons": reasons}
        equity, hwm = account

        try:
            risk_total, notional, count = open_risk_and_notional_from_snapshot(
                self.conn, self.settings.risk, snapshot)
        except SnapshotCoverageError as e:
            # N4-2: commit-pre と commit-core の間に新規 exposure が確定
            # した。lock 内で再取得せず intent 拒否 (次周期の判断へ送る)。
            reasons = [f"execution snapshot coverage error: {e}"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            self.activity.write(Category.TRADE, "gate_rejected", reasons[0],
                                ref_id=str(iid))
            return {"result": "rejected", "order_id": None, "reasons": reasons}

        spec = snapshot.specs_by_pair[intent.pair]
        ctx = GateContext(
            quote=snapshot.quote, spec=spec, equity=equity, hwm=hwm,
            daily_start_equity=accounting.daily_start_equity(self.conn, now),
            open_position_count=count, existing_risk_account=risk_total,
            existing_notional_account=notional,
            kill_switch_latched=self.state.load().kill_switch_latched,
            has_unresolved_unknown=has_unresolved_unknown(self.conn),
            account_currency=self.settings.account_currency,
            quote_to_account=snapshot.rates[spec.quote_currency],
            base_to_account=snapshot.rates[spec.base_currency],
            now=now)
        return self._evaluate_and_execute_open(intent, iid, ctx)

    # ---- close / cancel -------------------------------------------------

    def _close_unknown(self, row: dict, now: datetime) -> S:
        """close_order/close_order_from_snapshot 共有 (broker 応答が
        'ok' でない場合の後処理)。"""
        transitions.transition(self.conn, row["id"], S.CLOSE_UNKNOWN, now)
        self.activity.write(Category.TRADE, "close_unknown",
                            f"{row['pair']} — reconcile 待ち",
                            ref_id=str(row["id"]))
        self.notifier.send(f"[agentic-fx] クローズ結果不明 #{row['id']}")
        return S.CLOSE_UNKNOWN

    def _finish_close(self, row: dict, price: float, contract_size: float,
                      rate: ConversionRate | None, degraded: bool,
                      reason: str, now: datetime) -> S:
        """close_order/close_order_from_snapshot 共有 (broker 成功後の
        pnl 計算・DB 遷移・activity 記録 — 既存 close_order の当該部分を
        **逐語**移動しただけで判定ロジックは 1 文字も変えない)。"""
        pnl = compute_pnl(
            row, price, contract_size=contract_size,
            commission_per_lot=self.settings.risk.commission_per_lot,
            quote_to_account_rate=rate.value) if rate is not None else None
        transitions.transition(self.conn, row["id"], S.CLOSED, now,
                               close_price=price, realized_pnl=pnl,
                               closed_at=now.isoformat())
        pnl_text = f"{pnl:.0f}" if pnl is not None else "degraded(unresolved)"
        self.activity.write(Category.TRADE, "order_closed",
                            f"{row['pair']} pnl={pnl_text} reason={reason}",
                            ref_id=str(row["id"]))
        if degraded:
            self.activity.write(
                Category.TRADE, "close_pnl_rate_degraded",
                f"{row['pair']}: 換算レート取得不能 — " + (
                    "最後の健全レートで計算 (次回同期で吸収)" if pnl is not None
                    else "realized_pnl 未確定 (次回同期で解消)"),
                ref_id=str(row["id"]))
            self.notifier.send(
                f"[agentic-fx] クローズ換算レート degraded #{row['id']}")
        return S.CLOSED

    def _close(self, intent: TradeIntent, iid: int) -> dict:
        now = self.clock.now()
        row = orders.get(self.conn, intent.order_id)
        if row is None or row["status"] != S.OPEN.value:
            reasons = [f"order {intent.order_id} is not open"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            return {"result": "rejected", "order_id": intent.order_id,
                    "reasons": reasons}
        intents_store.set_gate_result(self.conn, iid, accepted=True,
                                      reject_reason=None)
        quote = self.quote_fn(row["pair"])
        price = quote.bid if row["direction"] == "long" else quote.ask
        final = self.close_order(row, price, reason="llm_close")
        result = "closed" if final == S.CLOSED else "unknown"
        return {"result": result, "order_id": row["id"], "reasons": []}

    def close_order(self, row: dict, price: float, reason: str) -> S:
        """裁量クローズ・SL/TP・強制クローズ共通の決定論的クローズ経路
        (scheduler の SL/TP・day rollover 等が使う — lock 保持中に自前で
        spec_fn/resolve_close_rate を呼ぶ既存動作は不変。**snapshot 版は
        close_order_from_snapshot** — Mission の commit-core から使う)。
        結果不明は closed 扱いにしない (設計書 §12)。snapshot (mark-to-
        market) は scheduler が記録する。戻り値は遷移後の状態
        (S.CLOSED / S.CLOSE_UNKNOWN) — cancel_order と対称。"""
        now = self.clock.now()
        spec = self.spec_fn(row["pair"])
        transitions.transition(self.conn, row["id"], S.CLOSING, now,
                               close_reason=reason)
        try:
            br = self.broker.close(row, price, reason)
        except Exception as e:  # noqa: BLE001 — 結果不明として扱う (codex 2)
            br = BrokerResult(status="unknown", message=safe_error_text(e))
        if br.status != "ok":
            return self._close_unknown(row, now)
        # 設計書 §5: クローズはレート欠損でも妨げない。現在レートが取れなければ
        # 最後に健全性検証を通ったレートへ degraded フォールバックする。
        rate, degraded = self.resolve_close_rate(spec.quote_currency, now)
        return self._finish_close(row, price, spec.contract_size, rate,
                                  degraded, reason, now)

    def gather_close_snapshot(self, row: dict) -> CloseSnapshot:
        """commit-pre 相専用 (裁定書 F-1 / CR-2 / P8-01) — **core_lock
        非保持で呼ぶこと**。CLOSE 実行に要る quote (成行価格) +
        instrument spec + 換算レートを 1 回で取得し timestamp 付き
        スナップショットにする。`row` は呼び出し元 (Task 15 の commit-pre
        相) が `conn_supervisor` (lock 外の読取専用接続) から読んだ現在の
        order 行 (`pair`/`direction` を参照するだけ)。"""
        now = self.clock.now()
        quote = self.quote_fn(row["pair"])
        price = quote.bid if row["direction"] == "long" else quote.ask
        spec = self.spec_fn(row["pair"])
        rate, degraded = self.resolve_close_rate(spec.quote_currency, now)
        return CloseSnapshot(price=price, spec=spec, rate=rate,
                             rate_degraded=degraded, captured_at=now)

    def close_order_from_snapshot(self, row: dict, snapshot: CloseSnapshot,
                                  reason: str) -> S:
        """close_order の commit-core 専用版 (裁定書 F-1 / CR-2 / P8-01)
        — `spec_fn`/`resolve_close_rate` を一切呼ばない (外部 I/O ゼロ)。
        price/spec/rate は commit-pre で取得済みの `CloseSnapshot` を使う
        (`broker.close` は paper broker の DB 書込であり外部 I/O ではない
        ため commit-core に残す)。`_finish_close`/`_close_unknown` を
        `close_order` と共有 — 判定・記録ロジックは `close_order` と完全
        共有 (I/O 位置のみ移動)。"""
        now = self.clock.now()
        transitions.transition(self.conn, row["id"], S.CLOSING, now,
                               close_reason=reason)
        try:
            br = self.broker.close(row, snapshot.price, reason)
        except Exception as e:  # noqa: BLE001
            br = BrokerResult(status="unknown", message=safe_error_text(e))
        if br.status != "ok":
            return self._close_unknown(row, now)
        return self._finish_close(row, snapshot.price,
                                  snapshot.spec.contract_size, snapshot.rate,
                                  snapshot.rate_degraded, reason, now)

    def close_from_snapshot(self, intent: TradeIntent, iid: int,
                            snapshot: "CloseSnapshot | None", *,
                            max_snapshot_age_sec: float) -> dict:
        """commit-core 相専用 (裁定書 F-1 / CR-2 / P8-01) — **core_lock
        保持中に呼ぶこと**。①DB 状態を読み直して row の現況を確定
        (commit-pre 後に状態が変わっていないか確認) ②`snapshot` が None
        (commit-pre 時点で row が OPEN でなく取得をスキップした場合、また
        は commit-pre 後に OPEN へ遷移した稀なケース) なら lock 内で
        取得し直さず reject する ③鮮度再検証 (lock 内での再取得はしない)
        ④`close_order_from_snapshot` へ委譲。"""
        now = self.clock.now()
        row = orders.get(self.conn, intent.order_id)
        if row is None or row["status"] != S.OPEN.value:
            reasons = [f"order {intent.order_id} is not open"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            return {"result": "rejected", "order_id": intent.order_id,
                    "reasons": reasons}
        if snapshot is None:
            reasons = [
                "close snapshot unavailable (order was not open at "
                "commit-pre time) — rejecting rather than re-fetching "
                "while holding core_lock (設計書 §3.1)"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            return {"result": "rejected", "order_id": intent.order_id,
                    "reasons": reasons}
        age_sec = (now - snapshot.captured_at).total_seconds()
        if age_sec > max_snapshot_age_sec:
            reasons = [
                f"close snapshot is stale ({age_sec:.1f}s > "
                f"{max_snapshot_age_sec}s) — rejecting rather than "
                "re-fetching while holding core_lock (設計書 §3.1)"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            return {"result": "rejected", "order_id": intent.order_id,
                    "reasons": reasons}
        intents_store.set_gate_result(self.conn, iid, accepted=True,
                                      reject_reason=None)
        final = self.close_order_from_snapshot(row, snapshot, reason="llm_close")
        result = "closed" if final == S.CLOSED else "unknown"
        return {"result": result, "order_id": row["id"], "reasons": []}

    def cancel_order(self, row: dict, reason: str) -> S:
        """取消の共通経路 (LLM cancel / 期限切れ / 予約維持 / クローズ移行)。
        戻り値は遷移後の状態。"""
        now = self.clock.now()
        transitions.transition(self.conn, row["id"], S.CANCELLING, now)
        try:
            br = self.broker.cancel(row)
        except Exception as e:  # noqa: BLE001 — 結果不明として扱う (codex 2)
            br = BrokerResult(status="unknown",
                              message=safe_error_text(e))
        if br.status == "ok":
            transitions.transition(self.conn, row["id"], S.CANCELLED, now,
                                   close_reason=reason)
            self.activity.write(Category.TRADE, "limit_cancelled",
                                f"{row['pair']} ({reason})",
                                ref_id=str(row["id"]))
            return S.CANCELLED
        if br.status == "rejected":
            # 既に約定済み等 — 取消/約定競合 (設計書 §5)
            transitions.transition(self.conn, row["id"], S.PROTECTION_PENDING,
                                   now)
            return S.PROTECTION_PENDING
        transitions.transition(self.conn, row["id"], S.CANCEL_UNKNOWN, now)
        self.notifier.send(f"[agentic-fx] 取消結果不明 #{row['id']}")
        return S.CANCEL_UNKNOWN

    def _cancel(self, intent: TradeIntent, iid: int) -> dict:
        row = orders.get(self.conn, intent.order_id)
        if row is None or row["status"] != S.PENDING_FILL.value:
            reasons = [f"order {intent.order_id} is not pending_fill"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            return {"result": "rejected", "order_id": intent.order_id,
                    "reasons": reasons}
        intents_store.set_gate_result(self.conn, iid, accepted=True,
                                      reject_reason=None)
        final = self.cancel_order(row, reason="llm_cancel")
        result = "cancelled" if final == S.CANCELLED else "unknown"
        return {"result": result, "order_id": row["id"], "reasons": []}


def _intent_payload(intent: TradeIntent) -> dict:
    return {
        "action": intent.action.value, "origin": intent.origin.value,
        "pair": intent.pair,
        "direction": intent.direction.value if intent.direction else None,
        "entry_type": intent.entry_type.value if intent.entry_type else None,
        "horizon": intent.horizon.value if intent.horizon else None,
        "order_id": intent.order_id, "limit_price": intent.limit_price,
        "expires_in_h": intent.expires_in_h, "stop_loss": intent.stop_loss,
        "take_profit": intent.take_profit, "confidence": intent.confidence,
        "reasoning": intent.reasoning, "ref_price": intent.ref_price,
    }
