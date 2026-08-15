from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import (
    Action, ConversionRate, FixedClock, InstrumentSpec, Origin, Quote,
    TradeIntent,
)
from agentic_fx.core.executor import Executor
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.datafeed.health import DataUnhealthy
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


def _rate_fn(ccy, account_ccy, now):
    """USDJPY 専用の最小限のレート供給スタブ (JPY 恒等 / USD→JPY のみ)。"""
    if ccy == account_ccy:
        return ConversionRate(1.0, ccy, account_ccy, (now,))
    if ccy == "USD" and account_ccy == "JPY":
        return ConversionRate(QUOTE.ask, "USD", "JPY", (now,))
    raise DataUnhealthy(f"no rate for {ccy}->{account_ccy}")


def _setup(tmp_path, broker=None, rate_fn=None):
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
                  spec_fn=lambda p: SPEC, rate_fn=rate_fn or _rate_fn)
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


def test_close_order_degraded_activity_carries_absorbed_cause(tmp_path):
    """観測性の pin (/code-review 2 周目): 非 snapshot 経路 (`close_order`
    — SL/TP・day rollover・裁量クローズが使う本番で最も回る経路) でも、
    `resolve_close_rate` が吸収した例外の要旨が
    `close_pnl_rate_degraded` activity に残ること。

    これが無いと degraded の行から原因が消え、運用ログ上で
    「gather deadline による打ち切り」と「ベンダ障害」が区別できない。
    `close_order` への `degraded_reason` 受け渡しを落とす退行変異は
    (既定値 None のため) 無音で通るので、ここで固定する。"""
    from agentic_fx.activity import Category
    from agentic_fx.core.contracts import OrderStatus as S

    def failing_rate_fn(ccy, account_ccy, now, **_ignored):
        raise DataUnhealthy(f"vendor outage for {ccy}")

    conn, ex, _, mid = _setup(tmp_path)
    it = _open_intent(entry_type="market", limit_price=None, expires_in=None,
                      stop_loss=148.00, take_profit=149.60)
    oid = ex.handle_intent(it, mid)["order_id"]
    ex.rate_fn = failing_rate_fn  # commit 後にレート供給が壊れた

    final = ex.close_order(orders.get(conn, oid), 148.60, reason="sl_hit")

    assert final == S.CLOSED  # fail-soft 契約は不変 (クローズは妨げない)
    degraded = [l for l in ex.activity.tail(n=100, category=Category.TRADE)
                if "close_pnl_rate_degraded" in l]
    assert len(degraded) == 1, degraded
    assert "vendor outage for JPY" in degraded[0], degraded[0]
    assert "DataUnhealthy" in degraded[0], degraded[0]


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


# --- 口座通貨換算層 (設計書 §5、改訂第16版) --------------------------------

EUR_SPEC = InstrumentSpec("EURUSD", 0.0001, 0.01, 10.0, 0.01, 100_000,
                          base_currency="EUR", quote_currency="USD")


def test_open_risk_and_notional_converts_per_pair_no_price_in_notional(
        tmp_path):
    from agentic_fx.core.executor import open_risk_and_notional
    conn, ex, _, _ = _setup(tmp_path)
    orders.insert(conn, pair="USDJPY", direction="long", entry_type="market",
                 horizon="day", status="open", now=NOW, quantity=0.10,
                 avg_fill_price=148.20, stop_loss=147.80)
    orders.insert(conn, pair="EURUSD", direction="long", entry_type="market",
                 horizon="day", status="open", now=NOW, quantity=0.20,
                 avg_fill_price=1.1000, stop_loss=1.0950)

    def spec_fn(pair):
        return SPEC if pair == "USDJPY" else EUR_SPEC

    # EUR の base_to_account を entry 価格 (1.10 付近) とはかけ離れた値
    # (200.0) にする — notional が entry を使っていれば桁が全く合わなくなる。
    rates = {"JPY": ConversionRate(1.0, "JPY", "JPY", (NOW,)),
            "USD": ConversionRate(148.51, "USD", "JPY", (NOW,)),
            "EUR": ConversionRate(200.0, "EUR", "JPY", (NOW,))}

    total_risk, total_notional, count = open_risk_and_notional(
        conn, spec_fn, SETTINGS.risk, lambda ccy: rates[ccy])

    assert count == 2
    rule_usdjpy = SETTINGS.risk.pair_rules["USDJPY"]
    spread_usdjpy = rule_usdjpy.assumed_spread_pips * SPEC.pip_size
    risk_usdjpy = (0.40 + spread_usdjpy) * 100_000 * 0.10 * 1.0 \
        + SETTINGS.risk.commission_per_lot * 0.10
    rule_eur = SETTINGS.risk.pair_rules["EURUSD"]
    spread_eur = rule_eur.assumed_spread_pips * EUR_SPEC.pip_size
    sl_dist_eur = abs(1.1000 - 1.0950)
    risk_eur = (sl_dist_eur + spread_eur) * 100_000 * 0.20 * 148.51 \
        + SETTINGS.risk.commission_per_lot * 0.20
    assert total_risk == pytest.approx(risk_usdjpy + risk_eur)

    notional_usdjpy = 0.10 * 100_000 * 148.51   # base=USD
    notional_eur = 0.20 * 100_000 * 200.0        # base=EUR (rate ≠ entry 価格)
    assert total_notional == pytest.approx(notional_usdjpy + notional_eur)


def test_cycle_rate_fn_fetches_once_per_currency_across_rows(tmp_path):
    """スナップショット固定のピン: 同一サイクル内で同じ通貨の行が複数
    あっても、外部レート取得は通貨ごとに 1 回だけであること (都度取得に
    戻す変異のピン)。"""
    from agentic_fx.core.executor import open_risk_and_notional
    conn, ex, _, _ = _setup(tmp_path)
    for _ in range(3):
        orders.insert(conn, pair="USDJPY", direction="long",
                      entry_type="market", horizon="day", status="open",
                      now=NOW, quantity=0.05, avg_fill_price=148.20,
                      stop_loss=147.80)
    calls: list[str] = []

    def counting_rate_fn(ccy, account_ccy, now):
        calls.append(ccy)
        return _rate_fn(ccy, account_ccy, now)

    ex.rate_fn = counting_rate_fn
    cycle_rate = ex.cycle_rate_fn(NOW)
    open_risk_and_notional(conn, ex.spec_fn, SETTINGS.risk, cycle_rate)
    # 3 行とも quote=JPY (恒等)・base=USD だが、キャッシュにより通貨ごとに
    # 1 回だけ rate_fn が呼ばれる (恒等変換も呼び出し自体はカウントされる)。
    assert calls.count("USD") == 1
    assert calls.count("JPY") == 1


# --- fix round 2 (codex 節目レビュー): 判断内スナップショット全体の
# 時刻差 (設計書 §5 ③)。個々の ConversionRate は自分の脚と reference_ts
# だけを検証するため、複数レートを跨いだ全体スパンは cycle_rate_fn 側で
# 別途検証する必要がある。

def test_cycle_rate_fn_rejects_overall_snapshot_skew_across_currencies(
        tmp_path):
    """①: USD の leg が now+2min、EUR の leg が now-(skew+2)min。どちらも
    それぞれ「個別」には健全 (このテストの rate_fn スタブは個別鮮度検証を
    経ずに直接 ConversionRate を返すので、ここでは cycle_rate_fn 自身の
    集約検証だけを対象にする) だが、2 つの leg を合わせた全体スパンは
    conversion_skew_max_min を超える → fail closed。"""
    conn, ex, _, _ = _setup(tmp_path)
    skew = SETTINGS.datafeed.conversion_skew_max_min

    def rate_fn(ccy, account_ccy, now):
        if ccy == "USD":
            return ConversionRate(148.51, "USD", "JPY",
                                  (now + timedelta(minutes=2),))
        if ccy == "EUR":
            return ConversionRate(1.10, "EUR", "JPY",
                                  (now - timedelta(minutes=skew + 2),))
        raise DataUnhealthy(f"no rate for {ccy}")

    ex.rate_fn = rate_fn
    cycle_rate = ex.cycle_rate_fn(NOW)
    cycle_rate("USD")   # 先に取得・キャッシュされる (この時点では単独)
    with pytest.raises(DataUnhealthy, match="skew"):
        cycle_rate("EUR")   # USD の leg との全体スパンが skew を超える


def test_cycle_rate_fn_allows_rates_within_overall_skew(tmp_path):
    """②: 判断内の全レートが近接していれば (全体スパンが
    conversion_skew_max_min 以内) 通常どおり通ること。"""
    conn, ex, _, _ = _setup(tmp_path)
    skew = SETTINGS.datafeed.conversion_skew_max_min
    assert skew >= 2, "このテストは skew >= 2 min を前提にしている"

    def rate_fn(ccy, account_ccy, now):
        if ccy == "USD":
            return ConversionRate(148.51, "USD", "JPY",
                                  (now + timedelta(minutes=1),))
        if ccy == "EUR":
            return ConversionRate(1.10, "EUR", "JPY",
                                  (now - timedelta(minutes=1),))
        raise DataUnhealthy(f"no rate for {ccy}")

    ex.rate_fn = rate_fn
    cycle_rate = ex.cycle_rate_fn(NOW)
    usd = cycle_rate("USD")
    eur = cycle_rate("EUR")   # 全体スパン 2min <= skew (既定 5min) — 通る
    assert usd.value == pytest.approx(148.51)
    assert eur.value == pytest.approx(1.10)


def test_cycle_rate_fn_rejected_rate_does_not_poison_later_currencies(
        tmp_path):
    """fix round 3 (codex 節目レビュー再検証 — 新規 Important): 拒否した
    レートの leg 時刻が running span (確定値) に残ると、以後のキャッシュ
    未登録の通貨の要求までずっと巻き添えで失敗し続ける cascade になる。
    USD (健全) → GBP (外れ値、全体スパンを破る) → CHF (健全、USD と同時刻)
    の順で呼び、GBP だけが拒否され、CHF は cascade に巻き込まれず成功する
    こと。2 通貨だけの構成では GBP の失敗を見て終わり、この cascade は
    見えない — 3 通貨目 (CHF) が本テストの主眼。"""
    conn, ex, _, _ = _setup(tmp_path)
    skew = SETTINGS.datafeed.conversion_skew_max_min

    def rate_fn(ccy, account_ccy, now):
        if ccy == "USD":
            return ConversionRate(148.51, "USD", "JPY", (now,))
        if ccy == "GBP":
            return ConversionRate(190.0, "GBP", "JPY",
                                  (now - timedelta(minutes=skew + 2),))
        if ccy == "CHF":
            return ConversionRate(160.0, "CHF", "JPY", (now,))
        raise DataUnhealthy(f"no rate for {ccy}")

    ex.rate_fn = rate_fn
    cycle_rate = ex.cycle_rate_fn(NOW)
    cycle_rate("USD")   # 健全、確定 span = {NOW, NOW}
    with pytest.raises(DataUnhealthy, match="GBP"):
        cycle_rate("GBP")   # 外れ値、拒否される (エラーは GBP を名指し)
    chf = cycle_rate("CHF")   # cascade していれば GBP の外れ値に巻き込まれ
    # て失敗するが、正しい実装では USD の確定 span だけで判定され成功する。
    assert chf.value == pytest.approx(160.0)


def test_cycle_rate_fn_does_not_cache_rejected_rate(tmp_path):
    """M2 (未ピン不変条件, codex 再レビュー指摘): 全体スパンを破って
    拒否されたレートが cache に入ってしまうと、同一判断内で同じ通貨を
    **再度**要求したとき span 検証を経ずに (拒否されたはずの) レートを
    黙って返す fail-open になる。同じ通貨 (GBP) を 2 回要求し、2 回とも
    再取得され・2 回とも同じ理由で拒否されることを固定する
    (`cache[ccy] = rate` を raise の前に移す変異のピン)。"""
    conn, ex, _, _ = _setup(tmp_path)
    skew = SETTINGS.datafeed.conversion_skew_max_min
    calls: list[str] = []

    def rate_fn(ccy, account_ccy, now):
        calls.append(ccy)
        if ccy == "USD":
            return ConversionRate(148.51, "USD", "JPY", (now,))
        if ccy == "GBP":
            return ConversionRate(190.0, "GBP", "JPY",
                                  (now - timedelta(minutes=skew + 2),))
        raise DataUnhealthy(f"no rate for {ccy}")

    ex.rate_fn = rate_fn
    cycle_rate = ex.cycle_rate_fn(NOW)
    cycle_rate("USD")
    with pytest.raises(DataUnhealthy, match="GBP"):
        cycle_rate("GBP")
    with pytest.raises(DataUnhealthy, match="GBP"):
        # 1 回目の拒否が cache に入っていれば、ここは (span 再検証されず)
        # 例外を投げずに終わってしまう。
        cycle_rate("GBP")
    assert calls.count("GBP") == 2, (
        "1 回目の拒否が cache され、2 回目が再取得すらしていない (fail-open)")


def test_cycle_rate_fn_updates_last_good_rate_even_when_overall_skew_rejects(
        tmp_path):
    """M3 (未ピン不変条件, codex 再レビュー指摘): 全体 skew 検証で拒否
    されたレートでも、個別には健全 (`to_account_rate` 自身の検証は通って
    いる) なので `_last_good_rate` (クローズ経路の degraded フォールバック
    専用、設計書 §5「クローズはレート欠損でも妨げない」) は更新される、
    という報告書 fix round 2 の裁定を固定する (更新を span 検証の後に
    移す変異のピン)。"""
    conn, ex, _, _ = _setup(tmp_path)
    skew = SETTINGS.datafeed.conversion_skew_max_min

    def rate_fn(ccy, account_ccy, now):
        if ccy == "USD":
            return ConversionRate(148.51, "USD", "JPY", (now,))
        if ccy == "GBP":
            return ConversionRate(190.0, "GBP", "JPY",
                                  (now - timedelta(minutes=skew + 2),))
        raise DataUnhealthy(f"no rate for {ccy}")

    ex.rate_fn = rate_fn
    cycle_rate = ex.cycle_rate_fn(NOW)
    cycle_rate("USD")
    assert ex._last_good_rate.get(("GBP", "JPY")) is None   # 前提: まだ無い
    with pytest.raises(DataUnhealthy, match="GBP"):
        cycle_rate("GBP")
    cached = ex._last_good_rate.get(("GBP", "JPY"))
    assert cached is not None, (
        "全体 skew で拒否されたレートが _last_good_rate に反映されていない"
        " (クローズの degraded フォールバックが使えなくなる)")
    assert cached.value == pytest.approx(190.0)


def test_open_rejected_when_conversion_rate_unavailable(tmp_path):
    """予約・建玉が無くても、この intent 自身の換算レートが取れなければ
    却下される (設計書 §5: gate → intent 却下、理由に換算不能を明記)。"""
    conn, ex, _, mid = _setup(tmp_path)
    ex.spec_fn = lambda p: EUR_SPEC   # quote=USD, base=EUR
    ex.rate_fn = _rate_fn             # EUR->JPY は解決不可 (スタブの制約)
    it = _open_intent(pair="EURUSD", limit_price=1.1000, stop_loss=1.0950,
                      take_profit=1.1200)
    out = ex.handle_intent(it, mid)
    assert out["result"] == "rejected"
    assert any("conversion rate unavailable" in r for r in out["reasons"])
    row = conn.execute("SELECT * FROM trade_intents").fetchone()
    assert row["gate_result"] == "rejected"


def test_open_rejected_when_overall_snapshot_skew_exceeded(tmp_path):
    """fix round 2 (codex 節目レビュー): gate 評価層。EURUSD の
    quote_to_account (USD) と base_to_account (EUR) はそれぞれ個別には
    健全なレートだが、2 つの leg 時刻を跨いだ全体スパンが
    conversion_skew_max_min を超えれば、既存の
    "conversion rate unavailable" 却下経路にそのまま乗ること (層別動作は
    変えない)。"""
    conn, ex, _, mid = _setup(tmp_path)
    ex.spec_fn = lambda p: EUR_SPEC   # quote=USD, base=EUR
    skew = SETTINGS.datafeed.conversion_skew_max_min

    def rate_fn(ccy, account_ccy, now):
        if ccy == "USD":
            return ConversionRate(148.51, "USD", "JPY",
                                  (now + timedelta(minutes=2),))
        if ccy == "EUR":
            return ConversionRate(1.10, "EUR", "JPY",
                                  (now - timedelta(minutes=skew + 2),))
        raise DataUnhealthy(f"no rate for {ccy}")

    ex.rate_fn = rate_fn
    it = _open_intent(pair="EURUSD", limit_price=1.1000, stop_loss=1.0950,
                      take_profit=1.1200)
    out = ex.handle_intent(it, mid)
    assert out["result"] == "rejected"
    assert any("conversion rate unavailable" in r for r in out["reasons"])


# --- レビュー指摘 F2: gate_rejected (conversion rate unavailable) の例外
# テキストが safe_error_text を通すこと (codex C-I3 系サイトと同じ規律) ---

_LEAKY = "boom https://bridge.internal:8812/orders?apikey=SECRET_KEY_123"


def _assert_no_url(text: str) -> None:
    assert "bridge.internal" not in text
    assert "/orders" not in text
    assert "SECRET_KEY_123" not in text
    assert "apikey" not in text


def test_open_rejected_rate_error_message_is_sanitized(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    ex.spec_fn = lambda p: EUR_SPEC

    def leaky_rate_fn(ccy, account_ccy, now):
        if ccy == account_ccy:
            return ConversionRate(1.0, ccy, account_ccy, (now,))
        raise DataUnhealthy(_LEAKY)

    ex.rate_fn = leaky_rate_fn
    it = _open_intent(pair="EURUSD", limit_price=1.1000, stop_loss=1.0950,
                      take_profit=1.1200)
    out = ex.handle_intent(it, mid)
    assert out["result"] == "rejected"
    assert any("conversion rate unavailable" in r for r in out["reasons"])
    _assert_no_url("; ".join(out["reasons"]))
    log_text = (tmp_path / "activity.log").read_text(encoding="utf-8")
    assert "gate_rejected" in log_text
    _assert_no_url(log_text)


def test_close_degraded_falls_back_to_last_good_rate(tmp_path):
    """クローズ時にレートが取れなくても、直前に成功したレートで degraded
    換算して完遂すること (設計書 §5: クローズはレート欠損でも妨げない)。"""
    conn, ex, _, mid = _setup(tmp_path)
    it = _open_intent(entry_type="market", limit_price=None, expires_in=None,
                      stop_loss=148.00, take_profit=149.60)
    oid = ex.handle_intent(it, mid)["order_id"]
    # 一度成功させて last_good_rate を温める (open 時に USD->JPY を取得済み)
    row = orders.get(conn, oid)
    assert ex._last_good_rate.get(("USD", "JPY")) is not None

    def failing_rate_fn(ccy, account_ccy, now):
        raise DataUnhealthy("rate feed down")

    ex.rate_fn = failing_rate_fn
    from agentic_fx.core.contracts import OrderStatus as S
    final = ex.close_order(row, 148.60, reason="test")
    assert final == S.CLOSED
    closed = orders.get(conn, oid)
    assert closed["status"] == "closed"
    assert closed["realized_pnl"] is not None   # degraded だが計算できている


def test_close_with_no_rate_ever_leaves_realized_pnl_none(tmp_path):
    """直前に成功したレートも一度も無ければ、クローズ自体は完遂するが
    realized_pnl は未確定のまま残す (次回の定期同期で解消)。"""
    def never_rate_fn(ccy, account_ccy, now):
        raise DataUnhealthy("rate feed down")

    conn, ex, _, mid = _setup(tmp_path, rate_fn=never_rate_fn)
    oid = orders.insert(conn, pair="USDJPY", direction="long",
                        entry_type="market", horizon="day", status="open",
                        now=NOW, quantity=0.10, avg_fill_price=148.20,
                        stop_loss=147.80, take_profit=149.60)
    row = orders.get(conn, oid)
    from agentic_fx.core.contracts import OrderStatus as S
    final = ex.close_order(row, 148.60, reason="test")
    assert final == S.CLOSED
    closed = orders.get(conn, oid)
    assert closed["status"] == "closed"
    assert closed["realized_pnl"] is None
