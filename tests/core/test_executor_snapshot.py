"""Executor snapshot API (プラン8, 設計書 §3.1 / §12 申し送り①N4-2)。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import (
    ConversionRate, FixedClock, InstrumentSpec, Origin,
    OrderStatus as S, Quote, TradeIntent,
)
from agentic_fx.core.executor import (
    ExecutionSnapshot, Executor, SnapshotCoverageError, _intent_payload,
    open_risk_and_notional_from_snapshot,
)
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.store import intents as intents_store
from agentic_fx.store import missions, orders
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")

# **pair ごとに異なる spec を返す** — 既存 test_executor.py の
# `spec_fn=lambda p: SPEC` (常に USDJPY spec) を流用すると、USDJPY だけで
# USD/JPY の両通貨が揃ってしまい、`gather_open_snapshot` の
# exposure_pairs 展開ループを丸ごと消しても `rates` の assert が緑になる。
# EURUSD に別の base/quote 通貨 (EUR/USD) を持たせて初めて
# 「EUR が rates に入るか」が展開ループを pin する assert になる。
SPECS = {
    "USDJPY": InstrumentSpec(symbol="USDJPY", pip_size=0.01, min_lot=0.01,
                             max_lot=50.0, lot_step=0.01,
                             contract_size=100_000,
                             base_currency="USD", quote_currency="JPY"),
    "EURUSD": InstrumentSpec(symbol="EURUSD", pip_size=0.01, min_lot=0.01,
                             max_lot=50.0, lot_step=0.01,
                             contract_size=100_000,
                             base_currency="EUR", quote_currency="USD"),
}
QUOTE = Quote("USDJPY", 148.49, 148.51, NOW, "test")


def _rate_fn(ccy, account_ccy, now):
    """JPY 恒等 / USD・EUR → JPY のみ供給する最小スタブ。"""
    if ccy == account_ccy:
        return ConversionRate(1.0, ccy, account_ccy, (now,))
    if account_ccy == "JPY" and ccy in ("USD", "EUR"):
        return ConversionRate(QUOTE.ask, ccy, "JPY", (now,))
    raise DataUnhealthy(f"no rate for {ccy}->{account_ccy}")


def _make_executor(tmp_path, *, quote_fn=None, spec_fn=None, rate_fn=None,
                   broker=None) -> Executor:
    """本ファイル専用の Executor 構築ヘルパー。

    既存 `tests/core/test_executor.py::_setup` は `quote_fn`/`spec_fn` を
    固定注入しており差し替えられないため、こちらを新設する。
    `tmp_path / "a"` のようなサブディレクトリを渡せるよう mkdir する。
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    record_snapshot(conn, now=NOW, balance=1_000_000, equity=1_000_000)
    return Executor(
        conn=conn,
        broker=broker or PaperBroker(conn, SETTINGS, FixedClock(NOW)),
        settings=SETTINGS, state_store=StateStore(tmp_path / "state.json"),
        activity=ActivityLog(tmp_path / "activity.log"),
        notifier=Notifier(enabled=False, webhook_url=None),
        clock=FixedClock(NOW),
        quote_fn=quote_fn or (lambda p: QUOTE),
        spec_fn=spec_fn or (lambda p: SPECS[p]),
        rate_fn=rate_fn or _rate_fn)


def _start_trade_mission(conn) -> int:
    return missions.start(conn, "trade", "local", "m", NOW)


def _insert_intent(conn, mid: int, intent: TradeIntent) -> int:
    return intents_store.insert(conn, mid, _intent_payload(intent), NOW)


def _open_intent(pair="USDJPY", origin=Origin.SCHEDULER, **over) -> TradeIntent:
    """既定は **market** (即 OPEN になる) — 既存 test_executor.py の
    `_open_intent` は limit 既定 (結果は "pending") なので同名だが別物。"""
    d = {"action": "open", "pair": pair, "direction": "long",
         "entry_type": "market", "horizon": "day", "limit_price": None,
         "expires_in": None, "stop_loss": 148.00, "take_profit": 149.60,
         "reasoning": "t"}
    d.update(over)
    return TradeIntent.from_llm_dict(d, origin=origin)


def _close_intent(order_id: int) -> TradeIntent:
    return TradeIntent.from_llm_dict({"action": "close", "order_id": order_id},
                                     origin=Origin.SCHEDULER)


def _insert_open_order(conn, pair: str) -> dict:
    """Risk Gate を通さず OPEN の建玉行を直接作る (exposure の下ごしらえ
    専用)。gate 経由にすると EURUSD の pip_size/価格の組合せでサイズ計算が
    絡み、テストの意図 (exposure が存在すること) がぶれるため。"""
    oid = orders.insert(
        conn, pair=pair, direction="long", entry_type="market",
        horizon="day", status=S.OPEN, now=NOW,
        quantity=0.1, remaining_quantity=0.0, requested_price=148.20,
        avg_fill_price=148.20, filled_quantity=0.1, stop_loss=147.80,
        take_profit=149.00, filled_at=NOW.isoformat())
    return orders.get(conn, oid)


def test_gather_open_snapshot_covers_intent_pair_and_exposure_pairs(tmp_path):
    """gather_open_snapshot は intent.pair と exposure_pairs の両方の
    spec/通貨レートを含む。"""
    ex = _make_executor(tmp_path)
    intent = _open_intent(pair="USDJPY")
    snapshot = ex.gather_open_snapshot(intent, exposure_pairs=["EURUSD"])

    assert "USDJPY" in snapshot.specs_by_pair
    assert "EURUSD" in snapshot.specs_by_pair
    assert "JPY" in snapshot.rates  # USDJPY の quote_currency
    assert "USD" in snapshot.rates  # USDJPY の base / EURUSD の quote
    # ↓ この 1 本だけが exposure_pairs 展開ループを pin する
    #   (EUR は EURUSD の base_currency からしか入らない)
    assert "EUR" in snapshot.rates


def test_open_from_snapshot_rejects_stale_snapshot(tmp_path):
    ex = _make_executor(tmp_path)
    intent = _open_intent(pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    iid = _insert_intent(ex.conn, mid, intent)
    snapshot = ex.gather_open_snapshot(intent, exposure_pairs=[])
    stale_snapshot = snapshot.__class__(
        quote=snapshot.quote, spec=snapshot.spec,
        specs_by_pair=snapshot.specs_by_pair, rates=snapshot.rates,
        captured_at=snapshot.captured_at - timedelta(seconds=999))

    out = ex.open_from_snapshot(intent, iid, stale_snapshot,
                                max_snapshot_age_sec=5.0)
    assert out["result"] == "rejected"
    assert "stale" in out["reasons"][0]
    # 拒否は「発注しない」まで意味する — 行が 1 本も生まれていないこと
    assert orders.list_by_status(ex.conn, S.SUBMITTING, S.OPEN,
                                 S.PENDING_FILL) == []


def test_open_from_snapshot_rejects_when_exposure_grew_after_commit_pre(tmp_path):
    """N4-2: commit-pre と commit-core の間に新規 exposure が確定し、
    スナップショットに必要通貨が無い場合は lock 内取得せず intent 拒否。"""
    ex = _make_executor(tmp_path)
    intent = _open_intent(pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    iid = _insert_intent(ex.conn, mid, intent)
    snapshot = ex.gather_open_snapshot(intent, exposure_pairs=[])  # EURUSD 未カバー

    # commit-pre 後・commit-core 前に EURUSD の建玉が確定した状況を模す
    _insert_open_order(ex.conn, pair="EURUSD")

    out = ex.open_from_snapshot(intent, iid, snapshot, max_snapshot_age_sec=999.0)
    assert out["result"] == "rejected"
    assert "snapshot" in out["reasons"][0].lower()


def test_open_from_snapshot_matches_handle_intent(tmp_path):
    """judgment ロジック不変の確認: 同じ intent/状況で handle_intent (ライブ
    経路) と open_from_snapshot (Mission 経路) が同じ結果になる。"""
    ex1 = _make_executor(tmp_path / "a")
    ex2 = _make_executor(tmp_path / "b")  # 同一初期状態の別 DB
    intent = _open_intent(pair="USDJPY")   # market → "opened" を期待

    mid1 = _start_trade_mission(ex1.conn)
    out1 = ex1.handle_intent(intent, mid1)

    mid2 = _start_trade_mission(ex2.conn)
    iid2 = _insert_intent(ex2.conn, mid2, intent)
    snapshot = ex2.gather_open_snapshot(intent, exposure_pairs=[])
    out2 = ex2.open_from_snapshot(intent, iid2, snapshot,
                                  max_snapshot_age_sec=999.0)

    assert out1["result"] == out2["result"] == "opened"
    row1 = orders.get(ex1.conn, out1["order_id"])
    row2 = orders.get(ex2.conn, out2["order_id"])
    # サイズ・約定価格まで一致すること (「同じ result 文字列」だけでは
    # 判定ロジック共有の証明にならない)
    assert row1["quantity"] == row2["quantity"]
    assert row1["avg_fill_price"] == row2["avg_fill_price"]


def test_open_from_snapshot_matches_handle_intent_on_gate_rejection(tmp_path):
    """却下側も一致すること — kill switch ラッチ等の副作用込みで
    `_evaluate_and_execute_open` を両経路が共有していることの pin。"""
    ex1 = _make_executor(tmp_path / "a")
    ex2 = _make_executor(tmp_path / "b")
    # gate が必ず落とす intent (stop_loss を極端に離してリスク超過にする)
    intent = _open_intent(pair="USDJPY", stop_loss=100.00)

    mid1 = _start_trade_mission(ex1.conn)
    out1 = ex1.handle_intent(intent, mid1)

    mid2 = _start_trade_mission(ex2.conn)
    iid2 = _insert_intent(ex2.conn, mid2, intent)
    snapshot = ex2.gather_open_snapshot(intent, exposure_pairs=[])
    out2 = ex2.open_from_snapshot(intent, iid2, snapshot,
                                  max_snapshot_age_sec=999.0)

    assert out1["result"] == out2["result"] == "rejected"
    assert out1["reasons"] == out2["reasons"]


def test_open_risk_and_notional_from_snapshot_raises_on_uncovered_pair(tmp_path):
    ex = _make_executor(tmp_path)
    _insert_open_order(ex.conn, pair="EURUSD")
    empty_snapshot = ExecutionSnapshot(
        quote=None, spec=None, specs_by_pair={}, rates={},
        captured_at=datetime(2026, 8, 4, tzinfo=timezone.utc))
    with pytest.raises(SnapshotCoverageError):
        open_risk_and_notional_from_snapshot(ex.conn, ex.settings.risk,
                                             empty_snapshot)


def test_open_risk_and_notional_from_snapshot_raises_on_uncovered_currency(
        tmp_path):
    """spec はあるが通貨レートが欠けている場合も N4-2 として拒否する
    (`if spec is None` だけを見て通貨チェックを削っても落ちるように)。"""
    ex = _make_executor(tmp_path)
    _insert_open_order(ex.conn, pair="EURUSD")
    snapshot = ExecutionSnapshot(
        quote=QUOTE, spec=SPECS["USDJPY"],
        specs_by_pair={"EURUSD": SPECS["EURUSD"]},
        rates={"USD": ConversionRate(1.0, "USD", "JPY", (NOW,))},  # EUR 欠落
        captured_at=NOW)
    with pytest.raises(SnapshotCoverageError):
        open_risk_and_notional_from_snapshot(ex.conn, ex.settings.risk,
                                             snapshot)


def test_open_risk_and_notional_from_snapshot_matches_live_version(tmp_path):
    """DB-only 版が既存 open_risk_and_notional と同じ数値を返すこと
    (集計ロジックの逐語移植の pin)。"""
    from agentic_fx.core.executor import open_risk_and_notional
    ex = _make_executor(tmp_path)
    _insert_open_order(ex.conn, pair="USDJPY")
    _insert_open_order(ex.conn, pair="EURUSD")
    cycle_rate = ex.cycle_rate_fn(NOW)
    live = open_risk_and_notional(ex.conn, ex.spec_fn, ex.settings.risk,
                                  cycle_rate)
    intent = _open_intent(pair="USDJPY")
    snapshot = ex.gather_open_snapshot(intent,
                                       exposure_pairs=["USDJPY", "EURUSD"])
    snap = open_risk_and_notional_from_snapshot(ex.conn, ex.settings.risk,
                                                snapshot)
    assert live == snap


# ---- CLOSE snapshot (裁定書 F-1 / CR-2 / P8-01 — 独自裁定「OPEN のみ」を
# 破棄した反映) ---------------------------------------------------------

def test_gather_close_snapshot_captures_price_spec_and_rate(tmp_path):
    """gather_close_snapshot は quote_fn/spec_fn/resolve_close_rate を
    呼び、CloseSnapshot に price/spec/rate を確定する。"""
    ex = _make_executor(tmp_path)
    row = {"pair": "USDJPY", "direction": "long"}
    snapshot = ex.gather_close_snapshot(row)
    assert snapshot.price == QUOTE.bid       # long → bid
    assert snapshot.spec.symbol == "USDJPY"  # InstrumentSpec のフィールドは symbol
    assert snapshot.rate is not None
    assert snapshot.rate_degraded is False


def test_gather_close_snapshot_uses_ask_for_short(tmp_path):
    ex = _make_executor(tmp_path)
    snapshot = ex.gather_close_snapshot({"pair": "USDJPY",
                                         "direction": "short"})
    assert snapshot.price == QUOTE.ask


def test_close_order_from_snapshot_performs_no_external_io(tmp_path):
    """裁定書 F-1 の核心: close_order_from_snapshot は quote_fn/spec_fn/
    rate_fn を一切呼ばない (commit-core は取得済み値のみ使用)。

    **スタブは「呼ばれたら raise」ではなく「記録して正常値を返す」にする** —
    `resolve_close_rate` (executor.py:206-211) も broker 呼び出し
    (executor.py:395-399) も `except Exception` で握り潰すため、
    AssertionError を投げるスタブは静かに飲まれてテストが緑になる。
    """
    calls: list[str] = []

    def rec_quote_fn(pair):
        calls.append(f"quote_fn:{pair}")
        return QUOTE

    def rec_spec_fn(pair):
        calls.append(f"spec_fn:{pair}")
        return SPECS[pair]

    def rec_rate_fn(ccy, account_ccy, now):
        calls.append(f"rate_fn:{ccy}")
        return _rate_fn(ccy, account_ccy, now)

    # snapshot は commit-pre 相を模す健全な Executor で取得する
    healthy_ex = _make_executor(tmp_path / "pre")
    row = _insert_open_order(healthy_ex.conn, pair="USDJPY")
    snapshot = healthy_ex.gather_close_snapshot(row)

    # commit-core 相を模す Executor — 外部取得が起きたら calls に残る
    ex = _make_executor(tmp_path / "core", quote_fn=rec_quote_fn,
                        spec_fn=rec_spec_fn, rate_fn=rec_rate_fn)
    row2 = _insert_open_order(ex.conn, pair="USDJPY")
    ex.close_order_from_snapshot(row2, snapshot, reason="llm_close")

    assert calls == [], f"commit-core で外部取得が発生した: {calls}"
    assert orders.get(ex.conn, row2["id"])["status"] == S.CLOSED.value


def test_close_order_from_snapshot_matches_close_order_result(tmp_path):
    """判定・記録ロジック不変の確認: 同一 price/spec/rate を使えば
    close_order (lock保持中に自前で取得) と close_order_from_snapshot
    (取得済みスナップショットを使用) が同じ最終状態・pnl になる。"""
    ex1 = _make_executor(tmp_path / "a")
    ex2 = _make_executor(tmp_path / "b")
    row1 = _insert_open_order(ex1.conn, pair="USDJPY")
    row2 = _insert_open_order(ex2.conn, pair="USDJPY")

    final1 = ex1.close_order(row1, price=QUOTE.bid, reason="llm_close")
    snapshot = ex2.gather_close_snapshot(row2)
    final2 = ex2.close_order_from_snapshot(row2, snapshot, reason="llm_close")

    assert final1 == final2 == S.CLOSED
    r1 = orders.get(ex1.conn, row1["id"])
    r2 = orders.get(ex2.conn, row2["id"])
    assert r1["realized_pnl"] == r2["realized_pnl"]
    assert r1["close_price"] == r2["close_price"]


def test_close_from_snapshot_rejects_stale_snapshot(tmp_path):
    ex = _make_executor(tmp_path)
    row = _insert_open_order(ex.conn, pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    intent = _close_intent(order_id=row["id"])
    iid = _insert_intent(ex.conn, mid, intent)
    snapshot = ex.gather_close_snapshot(row)
    stale = snapshot.__class__(
        price=snapshot.price, spec=snapshot.spec, rate=snapshot.rate,
        rate_degraded=snapshot.rate_degraded,
        captured_at=snapshot.captured_at - timedelta(seconds=999))

    out = ex.close_from_snapshot(intent, iid, stale, max_snapshot_age_sec=5.0)
    assert out["result"] == "rejected"
    assert "stale" in out["reasons"][0]
    # 拒否は「クローズしない」まで意味する
    assert orders.get(ex.conn, row["id"])["status"] == S.OPEN.value


def test_close_from_snapshot_rejects_when_snapshot_is_none(tmp_path):
    """裁定書 F-1: commit-pre 時点で row が未 OPEN (snapshot 取得をスキップ)
    だった場合、commit-core は lock 内で取得し直さず reject する。"""
    ex = _make_executor(tmp_path)
    row = _insert_open_order(ex.conn, pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    intent = _close_intent(order_id=row["id"])
    iid = _insert_intent(ex.conn, mid, intent)

    out = ex.close_from_snapshot(intent, iid, None, max_snapshot_age_sec=999.0)
    assert out["result"] == "rejected"
    assert "snapshot" in out["reasons"][0].lower()
    assert orders.get(ex.conn, row["id"])["status"] == S.OPEN.value


def test_close_from_snapshot_closes_when_fresh(tmp_path):
    ex = _make_executor(tmp_path)
    row = _insert_open_order(ex.conn, pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    intent = _close_intent(order_id=row["id"])
    iid = _insert_intent(ex.conn, mid, intent)
    snapshot = ex.gather_close_snapshot(row)

    out = ex.close_from_snapshot(intent, iid, snapshot,
                                 max_snapshot_age_sec=999.0)
    assert out["result"] == "closed"
    assert orders.get(ex.conn, row["id"])["status"] == S.CLOSED.value


def test_open_from_snapshot_performs_no_external_io(tmp_path):
    """裁定書 F-1 の核心: open_from_snapshot は quote_fn/spec_fn/
    rate_fn を一切呼ばない (commit-core は取得済み値のみ使用)。

    **スタブは「呼ばれたら raise」ではなく「記録して正常値を返す」にする** —
    `cycle_rate_fn` が返すクロージャが rate_fn を呼び出すため、
    率 fn の記録型スタブで検出できる。
    """
    calls: list[str] = []

    def rec_quote_fn(pair):
        calls.append(f"quote_fn:{pair}")
        return QUOTE

    def rec_spec_fn(pair):
        calls.append(f"spec_fn:{pair}")
        return SPECS[pair]

    def rec_rate_fn(ccy, account_ccy, now):
        calls.append(f"rate_fn:{ccy}")
        return _rate_fn(ccy, account_ccy, now)

    # snapshot は commit-pre 相を模す健全な Executor で取得する
    healthy_ex = _make_executor(tmp_path / "pre")
    intent = _open_intent(pair="USDJPY")
    snapshot = healthy_ex.gather_open_snapshot(intent, exposure_pairs=[])

    # commit-core 相を模す Executor — 外部取得が起きたら calls に残る
    ex = _make_executor(tmp_path / "core", quote_fn=rec_quote_fn,
                        spec_fn=rec_spec_fn, rate_fn=rec_rate_fn)
    mid = _start_trade_mission(ex.conn)
    iid = _insert_intent(ex.conn, mid, intent)
    ex.open_from_snapshot(intent, iid, snapshot, max_snapshot_age_sec=999.0)

    assert calls == [], f"commit-core で外部取得が発生した: {calls}"
