"""Executor snapshot API (プラン8, 設計書 §3.1 / §12 申し送り①N4-2)。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import load_settings
from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import (
    BrokerResult, ConversionRate, FixedClock, InstrumentSpec, Origin,
    OrderStatus as S, Quote, TradeIntent,
)
from agentic_fx.core.executor import (
    CloseSnapshot, ExecutionSnapshot, Executor, SnapshotCoverageError,
    _intent_payload, open_risk_and_notional_from_snapshot,
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


def _insert_open_order(conn, pair: str, direction: str = "long") -> dict:
    """Risk Gate を通さず OPEN の建玉行を直接作る (exposure の下ごしらえ
    専用)。gate 経由にすると EURUSD の pip_size/価格の組合せでサイズ計算が
    絡み、テストの意図 (exposure が存在すること) がぶれるため。"""
    oid = orders.insert(
        conn, pair=pair, direction=direction, entry_type="market",
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
    # F3: 拒否は「発注しない」まで含む
    assert orders.list_by_status(ex.conn, S.SUBMITTING, S.PENDING_FILL) == []


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
    # F4: 副作用 (kill_switch_latched) も一致すること
    assert (ex1.state.load().kill_switch_latched
            == ex2.state.load().kill_switch_latched)


def test_open_from_snapshot_rejects_when_intent_currency_not_in_rates(tmp_path):
    """intent の pair の spec はあるが換算レートが欠けている場合の fail-closed。

    レビュー 3 周目: ここは `snapshot.rates[...]` の裸の添字アクセスで、
    直上の spec ガードと非対称だった。exposure 行が 1 件も無いと
    `open_risk_and_notional_from_snapshot` のループが空回りするため、
    N4-2 検査を素通りしてこの行に到達する。裸の KeyError を
    core_lock 保持中に飛ばさないことを pin する。
    """
    ex = _make_executor(tmp_path)
    intent = _open_intent(pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    iid = _insert_intent(ex.conn, mid, intent)
    # spec はあるが rates が空 (exposure 行は 1 件も作らない)
    broken = ExecutionSnapshot(
        quote=QUOTE, spec=SPECS["USDJPY"],
        specs_by_pair={"USDJPY": SPECS["USDJPY"]}, rates={},
        captured_at=NOW)

    out = ex.open_from_snapshot(intent, iid, broken,
                                max_snapshot_age_sec=999.0)

    assert out["result"] == "rejected"
    assert "not covered" in out["reasons"][0]
    assert orders.list_by_status(ex.conn, S.SUBMITTING, S.OPEN,
                                 S.PENDING_FILL) == []


def test_open_risk_and_notional_from_snapshot_raises_on_uncovered_pair(tmp_path):
    ex = _make_executor(tmp_path)
    _insert_open_order(ex.conn, pair="EURUSD")
    empty_snapshot = ExecutionSnapshot(
        quote=None, spec=None, specs_by_pair={}, rates={},
        captured_at=datetime(2026, 8, 4, tzinfo=timezone.utc))
    with pytest.raises(SnapshotCoverageError):
        open_risk_and_notional_from_snapshot(ex.conn, ex.settings.risk,
                                             empty_snapshot)


def test_open_from_snapshot_rejects_when_intent_pair_not_in_snapshot(tmp_path):
    """intent と snapshot の紐付けが壊れた場合の fail-closed。

    レビュー 3 周目の指摘: `gather_open_snapshot` が必ず intent.pair を
    入れるため、正しい呼び出し契約下では到達不能なガードである。しかし
    紐付けを行うのは Task 15 の五相配線 (intent は DB から読み直し、
    snapshot は別途保持) であり、そこが壊れたときに **core_lock 保持中に
    裸の KeyError を飛ばさない** ことが目的。ガードとテストを対にして
    残し、Task 15 でこの分岐がテスト対象から漏れないようにする。
    CLOSE 側の test_close_order_from_snapshot_rejects_mismatched_pair と対称。
    """
    ex = _make_executor(tmp_path)
    intent = _open_intent(pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    iid = _insert_intent(ex.conn, mid, intent)
    # 別の pair 向けに作られた snapshot を渡す (配線ミスを模す)
    other = _open_intent(pair="EURUSD")
    snapshot = ex.gather_open_snapshot(other, exposure_pairs=[])
    assert intent.pair not in snapshot.specs_by_pair

    out = ex.open_from_snapshot(intent, iid, snapshot,
                                max_snapshot_age_sec=999.0)

    assert out["result"] == "rejected"
    assert "not covered" in out["reasons"][0]
    # 拒否は「発注しない」まで意味する
    assert orders.list_by_status(ex.conn, S.SUBMITTING, S.OPEN,
                                 S.PENDING_FILL) == []


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
    row = {"id": 1, "pair": "USDJPY", "direction": "long"}
    snapshot = ex.gather_close_snapshot(row)
    assert snapshot.price == QUOTE.bid       # long → bid
    assert snapshot.spec.symbol == "USDJPY"  # InstrumentSpec のフィールドは symbol
    assert snapshot.rate is not None
    assert snapshot.rate_degraded is False


def test_gather_close_snapshot_uses_ask_for_short(tmp_path):
    ex = _make_executor(tmp_path)
    snapshot = ex.gather_close_snapshot({"id": 1, "pair": "USDJPY",
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


def test_close_order_from_snapshot_rejects_swapped_same_pair_orders(tmp_path):
    """レビュー 3 周目: pair だけの検査では long/short の取り違えを防げない。

    `gather_close_snapshot` は `row["direction"]` で bid/ask を選び分ける
    ので、**同一 pair の long と short** の snapshot を取り違えると
    pair 検査を素通りし、スプレッドの反対側でクローズされて
    close_price/realized_pnl が恒久的に誤る。order_id まで束縛して防ぐ。
    """
    ex = _make_executor(tmp_path)
    long_row = _insert_open_order(ex.conn, pair="USDJPY")
    short_row = _insert_open_order(ex.conn, pair="USDJPY", direction="short")

    long_snap = ex.gather_close_snapshot(long_row)
    short_snap = ex.gather_close_snapshot(short_row)
    # 前提: pair は同じで price だけが違う (= pair 検査では区別できない)
    assert long_snap.pair == short_snap.pair
    assert long_snap.price != short_snap.price

    # 取り違え (long の row に short の snapshot) は弾かれる
    with pytest.raises(SnapshotCoverageError):
        ex.close_order_from_snapshot(long_row, short_snap, reason="llm_close")

    # 弾かれた側は一切変更されていない
    updated = orders.get(ex.conn, long_row["id"])
    assert updated["status"] == S.OPEN.value
    assert updated["close_price"] is None
    assert updated["realized_pnl"] is None


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
        order_id=snapshot.order_id, pair=snapshot.pair,
        price=snapshot.price, spec=snapshot.spec, rate=snapshot.rate,
        rate_degraded=snapshot.rate_degraded,
        captured_at=snapshot.captured_at - timedelta(seconds=999))

    out = ex.close_from_snapshot(intent, iid, stale, max_snapshot_age_sec=5.0)
    assert out["result"] == "rejected"
    assert "stale" in out["reasons"][0]
    # 拒否は「クローズしない」まで意味する
    assert orders.get(ex.conn, row["id"])["status"] == S.OPEN.value
    # Task 14 レビュー 1 周からの申し送り: 拒否分岐は活動ログにも残す
    assert any("gate_rejected" in l
              for l in ex.activity.tail(n=50, category=Category.TRADE))


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
    # Task 14 レビュー 1 周からの申し送り: 拒否分岐は活動ログにも残す
    assert any("gate_rejected" in l
              for l in ex.activity.tail(n=50, category=Category.TRADE))


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
    # F1: gate_result が受理されたことを記録しているか確認
    gate_result = ex.conn.execute(
        "SELECT gate_result FROM trade_intents WHERE id=?", (iid,)
    ).fetchone()[0]
    assert gate_result == "accepted"


def test_close_order_from_snapshot_records_degraded_rate(tmp_path):
    """F2: rate_degraded フラグが activity 記録に反映されることを確認。"""
    calls: list[str] = []

    def rec_quote_fn(pair):
        return QUOTE

    def rec_spec_fn(pair):
        return SPECS[pair]

    def always_fail_rate_fn(ccy, account_ccy, now):
        # 最初の 1 回だけ失敗させる (resolve_close_rate がフォールバック)
        calls.append(f"rate_fn:{ccy}")
        raise DataUnhealthy(f"rate unavailable for {ccy}")

    # snapshot は commit-pre 相で取得 (rate_fn が失敗 → degraded=True)
    healthy_ex = _make_executor(tmp_path / "pre")
    row = _insert_open_order(healthy_ex.conn, pair="USDJPY")
    # 健全な rate_fn で最初のスナップショットを作る
    snapshot = healthy_ex.gather_close_snapshot(row)
    assert snapshot.rate_degraded is False

    # 次に失敗する rate_fn で再度 snapshot を取得
    ex = _make_executor(tmp_path / "core", quote_fn=rec_quote_fn,
                        spec_fn=rec_spec_fn, rate_fn=always_fail_rate_fn)
    # NOTE: ex の rate_fn は必ず失敗するので、この呼び出しでは
    # _last_good_rate は温まらない。このテストが踏むのは
    # 「rate が一度も取れず realized_pnl 未確定」の分岐である。
    # 「最後の健全レートで pnl を計算する」本命分岐は
    # test_close_order_from_snapshot_computes_pnl_from_degraded_rate が踏む。
    ex.resolve_close_rate("JPY", NOW)
    calls.clear()

    # 失敗する rate_fn 下で snapshot を取得
    row2 = _insert_open_order(ex.conn, pair="USDJPY")
    degraded_snapshot = ex.gather_close_snapshot(row2)
    assert degraded_snapshot.rate_degraded is True

    # このスナップショットで close_order_from_snapshot を実行
    ex.close_order_from_snapshot(row2, degraded_snapshot, reason="llm_close")

    # activity に close_pnl_rate_degraded が記録されているか確認
    from agentic_fx.activity import Category
    activity_log = ex.activity.tail(n=100, category=Category.TRADE)
    degraded_records = [
        l for l in activity_log
        if "close_pnl_rate_degraded" in l
    ]
    assert len(degraded_records) > 0, "close_pnl_rate_degraded が記録されていない"


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


# ---- Review 2 周目反映 (A1, B1-B6) ----

def test_close_order_from_snapshot_rejects_mismatched_pair(tmp_path):
    """A1: row と snapshot の pair が食い違うと拒否される。"""
    ex = _make_executor(tmp_path)
    row = _insert_open_order(ex.conn, pair="USDJPY")
    # EURUSD の snapshot (別銘柄)
    row_eurusd = {"id": row["id"], "pair": "EURUSD", "direction": "long"}
    snapshot = ex.gather_close_snapshot(row_eurusd)
    
    # USDJPY row に EURUSD snapshot で拒否
    with pytest.raises(SnapshotCoverageError):
        ex.close_order_from_snapshot(row, snapshot, reason="llm_close")
    
    # row の状態が OPEN のままであること
    updated = orders.get(ex.conn, row["id"])
    assert updated["status"] == S.OPEN.value
    assert updated["close_price"] is None
    assert updated["realized_pnl"] is None


def test_open_from_snapshot_rejects_when_account_snapshot_missing(tmp_path):
    """B1: commit-core 開始時に account snapshot が無ければ fail-closed。

    `_make_executor` は常に `record_snapshot` を呼ぶため、この分岐は
    レビュー 2 周目まで一度も falsy になっていなかった (変異 SURVIVED)。
    """
    ex = _make_executor(tmp_path)
    intent = _open_intent(pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    iid = _insert_intent(ex.conn, mid, intent)
    snapshot = ex.gather_open_snapshot(intent, exposure_pairs=[])

    # commit-pre 後・commit-core 前に account snapshot が失われた状況を模す
    ex.conn.execute("DELETE FROM account_snapshots")
    ex.conn.commit()

    out = ex.open_from_snapshot(intent, iid, snapshot,
                                max_snapshot_age_sec=999.0)
    assert out["result"] == "rejected"
    assert "no fresh account snapshot" in out["reasons"][0].lower()
    # 拒否は「発注しない」まで意味する
    assert orders.list_by_status(ex.conn, S.SUBMITTING, S.OPEN,
                                 S.PENDING_FILL) == []


def test_close_from_snapshot_rejects_when_row_already_closed(tmp_path):
    """B2: commit-pre 後に SL/TP 監視が既にクローズしていたら拒否する。

    設計書 §3.1 の「commit-core で row の現況を再確認する」意図そのもの。
    レビュー 2 周目まで変異 SURVIVED だった (二重クローズを防いでいる
    ことを誰も確かめていなかった)。
    """
    ex = _make_executor(tmp_path)
    row = _insert_open_order(ex.conn, pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    intent = _close_intent(order_id=row["id"])
    iid = _insert_intent(ex.conn, mid, intent)
    snapshot = ex.gather_close_snapshot(row)

    # commit-pre 後・commit-core 前に SL/TP 監視が閉じてしまった状況を模す
    orders.update_fields(ex.conn, row["id"], now=NOW, status=S.CLOSED,
                         close_price=148.50, realized_pnl=1234.0,
                         closed_at=NOW.isoformat())

    out = ex.close_from_snapshot(intent, iid, snapshot,
                                 max_snapshot_age_sec=999.0)
    assert out["result"] == "rejected"
    assert "not open" in out["reasons"][0].lower()

    # 二重クローズが起きていないこと (先の約定結果が上書きされていない)
    updated = orders.get(ex.conn, row["id"])
    assert updated["status"] == S.CLOSED.value
    assert updated["close_price"] == 148.50
    assert updated["realized_pnl"] == 1234.0


class _StubBroker:
    """BrokerResult 分岐テスト用 (tests/core/test_executor.py と同型)。"""

    def __init__(self, close_status="ok"):
        self.close_status = close_status

    def equity(self):
        return 1_000_000, 1_000_000

    def close(self, order_row, price, reason):
        if self.close_status == "raise":
            raise RuntimeError("close timeout")
        return BrokerResult(status=self.close_status)


def test_close_order_from_snapshot_rejects_when_broker_fails(tmp_path):
    """B3: broker が ok を返さなければ CLOSE_UNKNOWN。closed 扱いにしない。

    レビュー 2 周目まで変異 SURVIVED だった (snapshot 経路の broker 失敗
    分岐を誰も踏んでいなかった)。
    """
    ex = _make_executor(tmp_path, broker=_StubBroker(close_status="unknown"))
    row = _insert_open_order(ex.conn, pair="USDJPY")
    snapshot = CloseSnapshot(
        order_id=row["id"], pair="USDJPY", price=QUOTE.bid,
        spec=SPECS["USDJPY"],
        rate=ConversionRate(1.0, "JPY", "JPY", (NOW,)),
        rate_degraded=False, captured_at=NOW)

    final = ex.close_order_from_snapshot(row, snapshot, reason="llm_close")

    assert final == S.CLOSE_UNKNOWN
    updated = orders.get(ex.conn, row["id"])
    assert updated["status"] == S.CLOSE_UNKNOWN.value
    # 結果不明を closed 扱いにしない (設計書 §12)
    assert updated["realized_pnl"] is None
    assert updated["closed_at"] is None


def test_open_from_snapshot_matches_handle_intent_on_kill_switch_latch(tmp_path):
    """B4: kill switch のラッチという**副作用**が両経路で一致すること。

    レビュー 1 周目で追加した assert は intent が kill switch と無関係
    だったため `False == False` の恒真だった。ここでは drawdown を実際に
    踏ませ、**両経路とも False から True へ遷移する**ことを確かめる。
    """
    ex1 = _make_executor(tmp_path / "a")
    ex2 = _make_executor(tmp_path / "b")
    # DD 3% — tests/core/test_executor.py::test_kill_switch_latches と同じ作り
    record_snapshot(ex1.conn, now=NOW, balance=970_000, equity=970_000)
    record_snapshot(ex2.conn, now=NOW, balance=970_000, equity=970_000)

    # 前提: まだラッチされていない (恒真 assert 防止)
    assert ex1.state.load().kill_switch_latched is False
    assert ex2.state.load().kill_switch_latched is False

    intent = _open_intent(pair="USDJPY")

    mid1 = _start_trade_mission(ex1.conn)
    out1 = ex1.handle_intent(intent, mid1)

    mid2 = _start_trade_mission(ex2.conn)
    iid2 = _insert_intent(ex2.conn, mid2, intent)
    snapshot = ex2.gather_open_snapshot(intent, exposure_pairs=[])
    out2 = ex2.open_from_snapshot(intent, iid2, snapshot,
                                  max_snapshot_age_sec=999.0)

    assert out1["result"] == out2["result"] == "rejected"
    assert out1["reasons"] == out2["reasons"]
    # 本命: 副作用 (ラッチ) が両経路で発生していること
    assert ex1.state.load().kill_switch_latched is True
    assert ex2.state.load().kill_switch_latched is True


def test_close_order_from_snapshot_computes_pnl_from_degraded_rate(tmp_path):
    """B5: degraded でも「最後の健全レート」で pnl を計算する分岐を踏む。

    レビュー 1 周目で追加したテストは、失敗する rate_fn を持つ Executor に
    対して `resolve_close_rate` を呼んでいたため**キャッシュが温まらず**、
    `snapshot.rate is None` の「pnl 計算せず」分岐しか踏んでいなかった。
    ここでは健全な rate_fn でキャッシュを温めてから差し替える。
    """
    def fail_rate_fn(ccy, account_ccy, now):
        raise DataUnhealthy(f"rate unavailable for {ccy}")

    ex = _make_executor(tmp_path)
    row = _insert_open_order(ex.conn, pair="USDJPY")

    # 健全な rate_fn でキャッシュを温める (_last_good_rate に入る)
    healthy_rate, degraded = ex.resolve_close_rate("JPY", NOW)
    assert healthy_rate is not None and degraded is False

    # commit-pre 相: レート取得が失敗するようになった
    ex.rate_fn = fail_rate_fn
    snapshot = ex.gather_close_snapshot(row)

    # 本命の分岐を踏んでいることを明示する — rate は残っていて degraded
    assert snapshot.rate is not None, "degraded フォールバックが効いていない"
    assert snapshot.rate_degraded is True

    ex.close_order_from_snapshot(row, snapshot, reason="llm_close")

    updated = orders.get(ex.conn, row["id"])
    assert updated["status"] == S.CLOSED.value
    # degraded でも pnl は計算される (最後の健全レートを使う — 設計書 §5)
    assert updated["realized_pnl"] is not None
    trade_log = ex.activity.tail(n=100, category=Category.TRADE)
    assert any("close_pnl_rate_degraded" in line for line in trade_log), \
        f"degraded の activity 記録が無い: {trade_log}"


def test_open_from_snapshot_performs_no_external_io_returns_opened_result(tmp_path):
    """B6: 外部 I/O なしで実行できるかつ結果が 'opened'。"""
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

    healthy_ex = _make_executor(tmp_path / "pre")
    intent = _open_intent(pair="USDJPY")
    snapshot = healthy_ex.gather_open_snapshot(intent, exposure_pairs=[])

    ex = _make_executor(tmp_path / "core", quote_fn=rec_quote_fn,
                        spec_fn=rec_spec_fn, rate_fn=rec_rate_fn)
    mid = _start_trade_mission(ex.conn)
    iid = _insert_intent(ex.conn, mid, intent)
    out = ex.open_from_snapshot(intent, iid, snapshot, max_snapshot_age_sec=999.0)

    assert calls == [], f"commit-core で外部取得が発生した: {calls}"
    # B6: 結果が 'opened' であること
    assert out["result"] == "opened"
    assert out["order_id"] is not None


class _RecordingNotifier:
    """Task 14 申し送り (Step 3.5) の pin 用記録型スタブ。「呼ばれたら
    raise」は except Exception に飲まれるので使わない。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


class _RaisingCloseBroker:
    """close() が例外を送出し `_close_unknown` (→ self._notify) を踏ませる
    ためのスタブ broker。"""

    def close(self, row, price, reason):
        raise RuntimeError("broker_close_boom")

    def cancel(self, row):  # pragma: no cover — このテストでは使わない
        raise NotImplementedError

    def submit(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError


def test_defer_notifications_queues_commit_core_notifications(tmp_path):
    """Step 3.5 pin (Task 14 レビュー 1 周からの申し送り): commit-core 相
    (defer_notifications の中) で notifier.send を直接呼ばず、溜めるだけに
    すること。"""
    recording = _RecordingNotifier()
    ex = _make_executor(tmp_path, broker=_RaisingCloseBroker())
    ex.notifier = recording
    row = _insert_open_order(ex.conn, pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    intent = _close_intent(order_id=row["id"])
    iid = _insert_intent(ex.conn, mid, intent)
    snapshot = CloseSnapshot(order_id=row["id"], pair="USDJPY", price=148.49,
                             spec=SPECS["USDJPY"], rate=None,
                             rate_degraded=False, captured_at=NOW)

    with ex.defer_notifications() as pending:
        ex.close_from_snapshot(intent, iid, snapshot, max_snapshot_age_sec=999.0)
        assert recording.sent == []
        assert len(pending) == 1

    # with を抜けた後も遅延リストは変わらない (呼び出し元が commit-post で送る)
    assert recording.sent == []


def test_notify_sends_immediately_outside_defer_notifications(tmp_path):
    """遅延外 (scheduler 経路の非退行): defer_notifications を使わない既存
    close_order 経路は従来どおり即時送信のまま。"""
    recording = _RecordingNotifier()
    ex = _make_executor(tmp_path, broker=_RaisingCloseBroker())
    ex.notifier = recording
    row = _insert_open_order(ex.conn, pair="USDJPY")

    ex.close_order(row, price=148.49, reason="sl_hit")

    assert len(recording.sent) == 1
