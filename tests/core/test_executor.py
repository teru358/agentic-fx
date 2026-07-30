from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import (
    Action, FixedClock, InstrumentSpec, Origin, Quote, TradeIntent,
)
from agentic_fx.core.executor import Executor
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.store import intents as intents_store
from agentic_fx.store import missions, orders
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
SPEC = InstrumentSpec(symbol="USDJPY", pip_size=0.01, min_lot=0.01,
                      max_lot=50.0, lot_step=0.01, contract_size=100_000,
                      base_currency="USD", quote_currency="JPY")
QUOTE = Quote("USDJPY", 148.49, 148.51, NOW, "test")


def _setup(tmp_path, broker=None):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    record_snapshot(conn, now=NOW, balance=1_000_000, equity=1_000_000)
    state = StateStore(tmp_path / "state.json")
    from agentic_fx.core.notifier import Notifier
    ex = Executor(conn=conn,
                  broker=broker or PaperBroker(conn, SETTINGS, FixedClock(NOW)),
                  settings=SETTINGS, state_store=state,
                  activity=ActivityLog(tmp_path / "activity.log"),
                  notifier=Notifier(enabled=False, webhook_url=None),
                  clock=FixedClock(NOW), quote_fn=lambda p: QUOTE,
                  spec_fn=lambda p: SPEC)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    return conn, ex, state, mid


class StubBroker:
    """BrokerResult 分岐テスト用 (Phase 3 Mt5Broker の failure モード再現)。"""

    def __init__(self, submit_status="ok", cancel_status="ok",
                 close_status="ok"):
        from agentic_fx.core.contracts import BrokerResult
        self._r = BrokerResult
        self.submit_status = submit_status
        self.cancel_status = cancel_status
        self.close_status = close_status

    def equity(self):
        return 1_000_000, 1_000_000

    def submit(self, order_row, entry_price):
        if self.submit_status == "raise":
            raise RuntimeError("submit timeout")
        return self._r(status=self.submit_status, broker_order_id="s1")

    def cancel(self, order_row):
        if self.cancel_status == "raise":
            raise RuntimeError("cancel timeout")
        return self._r(status=self.cancel_status)

    def close(self, order_row, price, reason):
        if self.close_status == "raise":
            raise RuntimeError("close timeout")
        return self._r(status=self.close_status)


def _open_intent(origin=Origin.SCHEDULER, **over):
    d = {"action": "open", "pair": "USDJPY", "direction": "long",
         "entry_type": "limit", "horizon": "day", "limit_price": 148.20,
         "expires_in": "4h", "stop_loss": 147.80, "take_profit": 149.00,
         "reasoning": "t"}
    d.update(over)
    return TradeIntent.from_llm_dict(d, origin=origin)


def test_open_limit_creates_pending_fill(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    out = ex.handle_intent(_open_intent(), mid)
    assert out["result"] == "pending"
    row = orders.get(conn, out["order_id"])
    assert row["status"] == "pending_fill"
    assert row["quantity"] > 0
    assert row["expires_at"] is not None


def test_open_market_goes_straight_to_open(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    it = _open_intent(entry_type="market", limit_price=None, expires_in=None,
                      stop_loss=148.00, take_profit=149.60)
    out = ex.handle_intent(it, mid)
    assert out["result"] == "opened"
    row = orders.get(conn, out["order_id"])
    assert row["status"] == "open"
    assert row["avg_fill_price"] == QUOTE.ask


def test_ask_origin_rejected_for_open(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    out = ex.handle_intent(_open_intent(origin=Origin.ASK), mid)
    assert out["result"] == "rejected"
    assert any("origin" in r for r in out["reasons"])
    assert orders.list_by_status(conn, "pending_fill") == []


def test_ask_mission_id_rejected_even_with_scheduler_origin(tmp_path):
    """origin を偽装しても、mission の loop が trade でなければ拒否される。

    origin は呼び出し側が渡す enum 値に過ぎず、任意の内部コードが
    Origin.SCHEDULER を構成できる。mission_id は DB で照合できるため、
    executor は両方を独立に検証する (設計書 §5 / codex レビュー 4)。
    """
    conn, ex, _, _ = _setup(tmp_path)
    ask_mid = missions.start(conn, "ask", "local", "m", NOW)
    out = ex.handle_intent(_open_intent(), ask_mid)   # origin は scheduler のまま
    assert out["result"] == "rejected"
    assert any("not a trade mission" in r for r in out["reasons"])
    assert orders.list_by_status(conn, "pending_fill") == []


def test_nonexistent_mission_id_never_creates_an_order(tmp_path):
    """存在しない mission_id では発注に至らない。

    実際には loop 検証より前の trade_intents への記録 (全 intent を記録する
    設計) が FK 制約で弾くため IntegrityError になる。これは呼び出し側の
    プログラミングエラーでしか起きない経路であり、いずれにせよ**建玉は
    生まれない**ことをここで固定する。
    """
    import sqlite3
    conn, ex, _, _ = _setup(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        ex.handle_intent(_open_intent(), 999_999)
    assert orders.list_by_status(conn, "pending_fill") == []
    assert orders.list_by_status(conn, "open") == []


def test_improve_mission_cannot_close_positions(tmp_path):
    """close も同じ検証を通る (open だけの防御にしない)。"""
    conn, ex, _, mid = _setup(tmp_path)
    oid = ex.handle_intent(_open_intent(), mid)["order_id"]
    improve_mid = missions.start(conn, "improve", "local", "m", NOW)
    close = TradeIntent(action=Action.CLOSE, origin=Origin.SCHEDULER,
                        order_id=oid)
    out = ex.handle_intent(close, improve_mid)
    assert out["result"] == "rejected"
    assert any("not a trade mission" in r for r in out["reasons"])


def test_gate_reject_recorded(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    out = ex.handle_intent(_open_intent(take_profit=148.30), mid)  # RR 不足
    assert out["result"] == "rejected"
    row = conn.execute("SELECT * FROM trade_intents").fetchone()
    assert row["gate_result"] == "rejected"


def test_kill_switch_latches(tmp_path):
    conn, ex, state, mid = _setup(tmp_path)
    record_snapshot(conn, now=NOW, balance=970_000, equity=970_000)  # DD 3%
    out = ex.handle_intent(_open_intent(), mid)
    assert out["result"] == "rejected"
    assert state.load().kill_switch_latched is True
    # ラッチ後は equity が回復しても拒否される
    record_snapshot(conn, now=NOW, balance=1_100_000, equity=1_100_000)
    out2 = ex.handle_intent(_open_intent(), mid)
    assert out2["result"] == "rejected"
    assert any("latched" in r for r in out2["reasons"])


def test_close_open_position(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    it = _open_intent(entry_type="market", limit_price=None, expires_in=None,
                      stop_loss=148.00, take_profit=149.60)
    oid = ex.handle_intent(it, mid)["order_id"]
    close = TradeIntent.from_llm_dict({"action": "close", "order_id": oid},
                                      origin=Origin.SCHEDULER)
    out = ex.handle_intent(close, mid)
    assert out["result"] == "closed"
    row = orders.get(conn, oid)
    assert row["status"] == "closed"
    assert row["realized_pnl"] is not None
    assert row["closed_at"] is not None


def test_close_order_returns_final_status(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    it = _open_intent(entry_type="market", limit_price=None, expires_in=None,
                      stop_loss=148.00, take_profit=149.60)
    oid = ex.handle_intent(it, mid)["order_id"]
    row = orders.get(conn, oid)
    from agentic_fx.core.contracts import OrderStatus as S
    final = ex.close_order(row, 148.60, reason="test")
    assert final == S.CLOSED


def test_close_unknown_via_handle_intent_does_not_report_closed(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    it = _open_intent(entry_type="market", limit_price=None, expires_in=None,
                      stop_loss=148.00, take_profit=149.60)
    oid = ex.handle_intent(it, mid)["order_id"]
    ex.broker = StubBroker(close_status="unknown")
    close = TradeIntent.from_llm_dict({"action": "close", "order_id": oid},
                                      origin=Origin.SCHEDULER)
    out = ex.handle_intent(close, mid)
    assert out["result"] != "closed"
    assert out["result"] == "unknown"
    row = orders.get(conn, oid)
    assert row["status"] == "close_unknown"
    assert row["realized_pnl"] is None
    assert row["closed_at"] is None


def test_cancel_pending(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    oid = ex.handle_intent(_open_intent(), mid)["order_id"]
    cancel = TradeIntent.from_llm_dict({"action": "cancel", "order_id": oid},
                                       origin=Origin.SCHEDULER)
    assert ex.handle_intent(cancel, mid)["result"] == "cancelled"


def test_hold_records_only(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    hold = TradeIntent.from_llm_dict({"action": "hold", "reasoning": "wait"},
                                     origin=Origin.SCHEDULER)
    assert ex.handle_intent(hold, mid)["result"] == "hold"
    assert conn.execute("SELECT COUNT(*) c FROM trade_intents").fetchone()["c"] == 1


def test_pending_counts_toward_position_cap(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    ex.handle_intent(_open_intent(), mid)
    ex.handle_intent(_open_intent(limit_price=148.10, stop_loss=147.70,
                                  take_profit=148.90), mid)
    out3 = ex.handle_intent(_open_intent(limit_price=148.00, stop_loss=147.60,
                                         take_profit=148.80), mid)
    assert out3["result"] == "rejected"
    assert any("positions" in r for r in out3["reasons"])


def test_no_fresh_snapshot_fail_closed(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    conn.execute("DELETE FROM account_snapshots")
    conn.commit()
    out = ex.handle_intent(_open_intent(), mid)
    assert out["result"] == "rejected"
    assert any("snapshot" in r for r in out["reasons"])


def test_unresolved_unknown_blocks_new_open(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    orders.insert(conn, pair="USDJPY", direction="long", entry_type="market",
                  horizon="day", status="submit_unknown", now=NOW,
                  quantity=0.1, requested_price=148.5, stop_loss=148.0)
    out = ex.handle_intent(_open_intent(), mid)
    assert out["result"] == "rejected"
    assert any("unknown" in r for r in out["reasons"])


def test_submit_unknown_branches_to_submit_unknown(tmp_path):
    conn, ex, _, mid = _setup(tmp_path, broker=StubBroker(
        submit_status="unknown"))
    it = _open_intent(entry_type="market", limit_price=None, expires_in=None,
                      stop_loss=148.00, take_profit=149.60)
    out = ex.handle_intent(it, mid)
    assert out["result"] == "unknown"
    assert orders.get(conn, out["order_id"])["status"] == "submit_unknown"


def test_submit_rejected_branches_to_rejected(tmp_path):
    conn, ex, _, mid = _setup(tmp_path, broker=StubBroker(
        submit_status="rejected"))
    it = _open_intent(entry_type="market", limit_price=None, expires_in=None,
                      stop_loss=148.00, take_profit=149.60)
    out = ex.handle_intent(it, mid)
    assert out["result"] == "rejected"
    assert orders.get(conn, out["order_id"])["status"] == "rejected"


def test_close_unknown_not_marked_closed(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    it = _open_intent(entry_type="market", limit_price=None, expires_in=None,
                      stop_loss=148.00, take_profit=149.60)
    oid = ex.handle_intent(it, mid)["order_id"]
    ex.broker = StubBroker(close_status="unknown")
    row = orders.get(conn, oid)
    from agentic_fx.core.contracts import OrderStatus as S
    final = ex.close_order(row, 148.60, reason="test")
    assert final == S.CLOSE_UNKNOWN
    row = orders.get(conn, oid)
    assert row["status"] == "close_unknown"
    assert row["realized_pnl"] is None  # closed 扱いにしない
    assert row["closed_at"] is None


def test_cancel_rejected_means_fill_race(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    oid = ex.handle_intent(_open_intent(), mid)["order_id"]
    ex.broker = StubBroker(cancel_status="rejected")
    cancel = TradeIntent.from_llm_dict({"action": "cancel", "order_id": oid},
                                       origin=Origin.SCHEDULER)
    out = ex.handle_intent(cancel, mid)
    assert orders.get(conn, oid)["status"] == "protection_pending"


# --- レビュー修正 (codex 2): broker 例外は「結果不明」として扱う ---

def test_submit_exception_treated_as_submit_unknown(tmp_path):
    conn, ex, _, mid = _setup(tmp_path, broker=StubBroker(submit_status="raise"))
    it = _open_intent(entry_type="market", limit_price=None, expires_in=None,
                      stop_loss=148.00, take_profit=149.60)
    out = ex.handle_intent(it, mid)
    assert out["result"] == "unknown"
    assert orders.get(conn, out["order_id"])["status"] == "submit_unknown"


def test_cancel_exception_treated_as_cancel_unknown(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    oid = ex.handle_intent(_open_intent(), mid)["order_id"]
    ex.broker = StubBroker(cancel_status="raise")
    cancel = TradeIntent.from_llm_dict({"action": "cancel", "order_id": oid},
                                       origin=Origin.SCHEDULER)
    out = ex.handle_intent(cancel, mid)
    assert out["result"] == "unknown"
    assert orders.get(conn, oid)["status"] == "cancel_unknown"


def test_close_exception_treated_as_close_unknown(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    it = _open_intent(entry_type="market", limit_price=None, expires_in=None,
                      stop_loss=148.00, take_profit=149.60)
    oid = ex.handle_intent(it, mid)["order_id"]
    ex.broker = StubBroker(close_status="raise")
    close = TradeIntent.from_llm_dict({"action": "close", "order_id": oid},
                                      origin=Origin.SCHEDULER)
    out = ex.handle_intent(close, mid)
    assert out["result"] == "unknown"
    assert orders.get(conn, oid)["status"] == "close_unknown"


# --- レビュー修正 (codex 5): ref_price が intent payload に記録される ---

def test_ref_price_included_in_intent_payload(tmp_path):
    import json
    conn, ex, _, mid = _setup(tmp_path)
    it = TradeIntent.from_llm_dict(
        {"action": "open", "pair": "USDJPY", "direction": "long",
         "entry_type": "market", "horizon": "day", "stop_loss": 148.00,
         "take_profit": 149.60, "reasoning": "t"},
        origin=Origin.SCHEDULER, ref_price=148.50)
    ex.handle_intent(it, mid)
    row = conn.execute(
        "SELECT payload_json FROM trade_intents ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert json.loads(row["payload_json"])["ref_price"] == 148.50
