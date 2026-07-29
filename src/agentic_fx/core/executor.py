"""Executor — intent の全件記録・origin 検証・gate・発注経路 (設計書 §5)。"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Callable

from agentic_fx._safe_error import safe_error_text
from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import Settings
from agentic_fx.core import accounting, transitions
from agentic_fx.core.contracts import (
    Action, BrokerResult, Clock, InstrumentSpec, Origin, OrderStatus as S,
    Quote, TradeIntent,
)
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker, compute_pnl
from agentic_fx.core.risk_gate import GateContext, evaluate
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


def open_risk_and_notional(conn: sqlite3.Connection,
                           spec_fn: Callable[[str], InstrumentSpec],
                           risk) -> tuple[float, float, int]:
    total_risk = total_notional = 0.0
    rows = orders.list_by_status(conn, *_EXPOSURE)
    for r in rows:
        spec = spec_fn(r["pair"])
        rule = risk.pair_rules.get(r["pair"])
        spread = rule.assumed_spread_pips * spec.pip_size if rule else 0.0
        entry = r["avg_fill_price"] or r["requested_price"] or 0.0
        qty = r["quantity"] or 0.0
        total_risk += (abs(entry - (r["stop_loss"] or entry)) + spread) \
            * spec.contract_size * qty + risk.commission_per_lot * qty
        total_notional += qty * spec.contract_size * entry
    return total_risk, total_notional, len(rows)


def has_unresolved_unknown(conn: sqlite3.Connection) -> bool:
    return bool(orders.list_by_status(conn, *_UNKNOWN))


class Executor:
    def __init__(self, *, conn: sqlite3.Connection, broker: PaperBroker,
                 settings: Settings, state_store: StateStore,
                 activity: ActivityLog, notifier: Notifier, clock: Clock,
                 quote_fn: Callable[[str], Quote],
                 spec_fn: Callable[[str], InstrumentSpec]) -> None:
        self.conn = conn
        self.broker = broker
        self.settings = settings
        self.state = state_store
        self.activity = activity
        self.notifier = notifier
        self.clock = clock
        self.quote_fn = quote_fn
        self.spec_fn = spec_fn

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
        risk_total, notional, count = open_risk_and_notional(
            self.conn, self.spec_fn, self.settings.risk)
        ctx = GateContext(
            quote=quote, spec=spec, equity=equity, hwm=hwm,
            daily_start_equity=accounting.daily_start_equity(self.conn, now),
            open_position_count=count, existing_risk_total=risk_total,
            existing_notional=notional,
            kill_switch_latched=self.state.load().kill_switch_latched,
            has_unresolved_unknown=has_unresolved_unknown(self.conn),
            account_currency=self.settings.account_currency,
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

    # ---- close / cancel -------------------------------------------------

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
        """裁量クローズ・SL/TP・強制クローズ共通の決定論的クローズ経路。
        結果不明は closed 扱いにしない (設計書 §12)。snapshot は scheduler の
        mark-to-market が記録する。戻り値は遷移後の状態
        (S.CLOSED / S.CLOSE_UNKNOWN) — cancel_order と対称。"""
        now = self.clock.now()
        spec = self.spec_fn(row["pair"])
        transitions.transition(self.conn, row["id"], S.CLOSING, now,
                               close_reason=reason)
        try:
            br = self.broker.close(row, price, reason)
        except Exception as e:  # noqa: BLE001 — 結果不明として扱う (codex 2)
            br = BrokerResult(status="unknown",
                              message=safe_error_text(e))
        if br.status != "ok":
            transitions.transition(self.conn, row["id"], S.CLOSE_UNKNOWN, now)
            self.activity.write(Category.TRADE, "close_unknown",
                                f"{row['pair']} — reconcile 待ち",
                                ref_id=str(row["id"]))
            self.notifier.send(f"[agentic-fx] クローズ結果不明 #{row['id']}")
            return S.CLOSE_UNKNOWN
        pnl = compute_pnl(row, price, contract_size=spec.contract_size,
                          commission_per_lot=self.settings.risk.commission_per_lot)
        transitions.transition(self.conn, row["id"], S.CLOSED, now,
                               close_price=price, realized_pnl=pnl,
                               closed_at=now.isoformat())
        self.activity.write(Category.TRADE, "order_closed",
                            f"{row['pair']} pnl={pnl:.0f} reason={reason}",
                            ref_id=str(row["id"]))
        return S.CLOSED

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
