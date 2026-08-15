"""プラン9 束B Task 6/7: gather deadline (executor.py)。
設計の正: docs/superpowers/specs/2026-08-11-gather-deadline-design.md の
「確定仕様」節。検査点ごとのテーブル駆動 + 全 stub の呼び出し列を assert
する (境界は直前・等号・直後の 3 点)。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import load_settings
from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import (
    ConversionRate, FixedClock, InstrumentSpec, Origin, OrderStatus as S,
    Quote, TradeIntent,
)
from agentic_fx.core.executor import Executor
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
BUDGET = SETTINGS.worker.snapshot_max_age_sec  # 10.0 (既定 — 新キーは作らない)

SPECS = {
    "USDJPY": InstrumentSpec(symbol="USDJPY", pip_size=0.01, min_lot=0.01,
                             max_lot=50.0, lot_step=0.01, contract_size=100_000,
                             base_currency="USD", quote_currency="JPY"),
    "EURUSD": InstrumentSpec(symbol="EURUSD", pip_size=0.01, min_lot=0.01,
                             max_lot=50.0, lot_step=0.01, contract_size=100_000,
                             base_currency="EUR", quote_currency="USD"),
    "GBPUSD": InstrumentSpec(symbol="GBPUSD", pip_size=0.01, min_lot=0.01,
                             max_lot=50.0, lot_step=0.01, contract_size=100_000,
                             base_currency="GBP", quote_currency="USD"),
}
QUOTE = Quote("USDJPY", 148.49, 148.51, NOW, "test")


class _Mono:
    """テスト用可変 monotonic フェイク。缶詰の呼び出し順シーケンスにしない
    (呼び出し回数が実装の一部を変えるだけで壊れるため) — leg スタブ自身が
    `t` を進める。

    **基準時刻は 0.0 にしない (着手前検証 M6-10)。** 本番の
    `time.monotonic()` は起動時間ベースの大きな値を返す。0 起点のフェイクを
    使うと `start = self.monotonic_fn()` を `start = 0.0` に潰す変異が
    `elapsed = t - 0.0` と区別できず**全テストを生き延びる** (実測: 13 件
    全 PASS)。この変異は本番では最初の `check` が必ず発火する = 一度も
    取引しないシステムになる、Task 6 が防ごうとしているまさにその故障。
    以降のテストは絶対値代入 (`mono.t = ...`) ではなく**必ず相対加算**
    (`mono.t += ...`) を使うこと。"""

    def __init__(self) -> None:
        self.t = 12_345.0  # 0 起点にしない (上記 M6-10)

    def __call__(self) -> float:
        return self.t


def _make_executor(tmp_path, *, monotonic_fn, quote_fn, spec_fn,
                   rate_fn) -> Executor:
    tmp_path.mkdir(parents=True, exist_ok=True)
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    record_snapshot(conn, now=NOW, balance=1_000_000, equity=1_000_000)
    return Executor(
        conn=conn, broker=PaperBroker(conn, SETTINGS, FixedClock(NOW)),
        settings=SETTINGS, state_store=StateStore(tmp_path / "state.json"),
        activity=ActivityLog(tmp_path / "activity.log"),
        notifier=Notifier(enabled=False, webhook_url=None),
        clock=FixedClock(NOW), monotonic_fn=monotonic_fn,
        quote_fn=quote_fn, spec_fn=spec_fn, rate_fn=rate_fn)


def _recording_stubs(mono: _Mono, leg_costs: dict[str, float]):
    """quote_fn/spec_fn/rate_fn の記録型スタブ一式。各呼び出しは `calls` に
    追記し、`leg_costs` (呼び出しキー→消費秒数) ぶんだけ `mono.t` を進めて
    から返る (= その脚の取得に要した時間を模す)。"""
    calls: list[str] = []

    def quote_fn(pair: str) -> Quote:
        calls.append(f"quote:{pair}")
        mono.t += leg_costs.get(f"quote:{pair}", 0.0)
        return QUOTE

    def spec_fn(pair: str) -> InstrumentSpec:
        calls.append(f"spec:{pair}")
        mono.t += leg_costs.get(f"spec:{pair}", 0.0)
        return SPECS[pair]

    def rate_fn(ccy: str, account_ccy: str, now, **_ignored) -> ConversionRate:
        # **_ignored: Task 7 が rate_fn に keyword-only `deadline_check` を
        # 追加する (cycle_rate_fn/resolve_close_rate 経由)。Task 6 の
        # 呼び出しは kwarg を渡さないためここでは常に空。
        calls.append(f"rate:{ccy}")
        mono.t += leg_costs.get(f"rate:{ccy}", 0.0)
        return ConversionRate(1.0, ccy, account_ccy, (now,))

    return calls, quote_fn, spec_fn, rate_fn


def _open_intent(pair: str = "USDJPY") -> TradeIntent:
    return TradeIntent.from_llm_dict(
        {"action": "open", "pair": pair, "direction": "long",
         "entry_type": "market", "horizon": "day", "limit_price": None,
         "expires_in": None, "stop_loss": 148.00, "take_profit": 149.60,
         "reasoning": "t"}, origin=Origin.SCHEDULER)


def _insert_open_order(conn, pair: str = "USDJPY", direction: str = "long") -> dict:
    oid = orders.insert(
        conn, pair=pair, direction=direction, entry_type="market",
        horizon="day", status=S.OPEN, now=NOW,
        quantity=0.1, remaining_quantity=0.0, requested_price=148.20,
        avg_fill_price=148.20, filled_quantity=0.1, stop_loss=147.80,
        take_profit=149.00, filled_at=NOW.isoformat())
    return orders.get(conn, oid)


# ---- Task 6: _make_deadline_checker (低レベル) -----------------------------

def test_deadline_checker_accepts_exact_budget_boundary(tmp_path):
    """比較は `>` (等号は受理側) — 予算ちょうど消費しても発火しない。"""
    mono = _Mono()
    ex = _make_executor(tmp_path, monotonic_fn=mono,
                        quote_fn=lambda p: QUOTE, spec_fn=lambda p: SPECS[p],
                        rate_fn=lambda c, a, n, **k: ConversionRate(1.0, c, a, (n,)))
    check = ex._make_deadline_checker(BUDGET)
    mono.t += BUDGET  # ちょうど予算を使い切った (相対加算 — M6-10 参照)
    check("leg")  # raise しないこと


def test_deadline_checker_raises_just_over_budget(tmp_path):
    mono = _Mono()
    ex = _make_executor(tmp_path, monotonic_fn=mono,
                        quote_fn=lambda p: QUOTE, spec_fn=lambda p: SPECS[p],
                        rate_fn=lambda c, a, n, **k: ConversionRate(1.0, c, a, (n,)))
    check = ex._make_deadline_checker(BUDGET)
    mono.t += BUDGET + 0.1  # 相対加算 — M6-10 参照
    with pytest.raises(DataUnhealthy, match="deadline exceeded"):
        check("leg:x")


# ---- Task 6: gather_open_snapshot (OPEN 側) --------------------------------

def test_gather_open_snapshot_completes_when_within_budget(tmp_path):
    """非損失性のピン (確定仕様テスト観点 1): 全脚が予算内なら deadline は
    発火せず、全 stub が呼ばれて snapshot が完成する。"""
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(mono, leg_costs={})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    intent = _open_intent("USDJPY")

    snapshot = ex.gather_open_snapshot(intent, exposure_pairs=[])

    assert calls == ["quote:USDJPY", "spec:USDJPY", "rate:JPY", "rate:USD"]
    assert snapshot.specs_by_pair == {"USDJPY": SPECS["USDJPY"]}
    assert set(snapshot.rates) == {"JPY", "USD"}


def test_gather_open_snapshot_boundary_exact_budget_still_succeeds(tmp_path):
    """境界 (等号): quote_fn の取得に予算ちょうど掛かっても、比較は `>`
    なので次の脚 (spec_fn) は打ち切られず全体が完成する
    (`>=` へ変異すると red になる)。"""
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"quote:USDJPY": BUDGET})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    intent = _open_intent("USDJPY")

    snapshot = ex.gather_open_snapshot(intent, exposure_pairs=[])

    assert calls == ["quote:USDJPY", "spec:USDJPY", "rate:JPY", "rate:USD"]
    assert snapshot is not None


def test_gather_open_snapshot_aborts_before_spec_of_intent_pair(tmp_path):
    """打ち切りのピン (OPEN-1, 検査点「直後」): quote_fn だけで予算超過
    → spec_fn(intent.pair) が呼ばれない。"""
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"quote:USDJPY": BUDGET + 0.1})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    intent = _open_intent("USDJPY")

    with pytest.raises(DataUnhealthy, match="deadline exceeded"):
        ex.gather_open_snapshot(intent, exposure_pairs=[])

    assert calls == ["quote:USDJPY"]


def test_gather_open_snapshot_aborts_before_second_exposure_pair_spec(tmp_path):
    """打ち切りのピン (OPEN-2): exposure_pairs 2 件で、1 件目 (EURUSD) の
    spec_fn 取得で予算超過 → 2 件目 (GBPUSD) の spec_fn が呼ばれない。
    1 件だけだと「ループの前で 1 回だけ検査する」誤実装でも green に
    なってしまうため 2 件必須 (advisor 指摘)。"""
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"spec:EURUSD": BUDGET + 0.1})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    intent = _open_intent("USDJPY")

    with pytest.raises(DataUnhealthy, match="deadline exceeded"):
        ex.gather_open_snapshot(intent, exposure_pairs=["EURUSD", "GBPUSD"])

    assert calls == ["quote:USDJPY", "spec:USDJPY", "spec:EURUSD"]


def test_gather_open_snapshot_aborts_before_second_currency_rate(tmp_path):
    """打ち切りのピン (OPEN-3): 通貨 2 件 (JPY/USD, USDJPY 単体の
    quote/base から自然に出る) で、1 件目 (JPY) の rate_fn 取得で予算超過
    → 2 件目 (USD) の rate_fn が呼ばれない。"""
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"rate:JPY": BUDGET + 0.1})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    intent = _open_intent("USDJPY")

    with pytest.raises(DataUnhealthy, match="deadline exceeded"):
        ex.gather_open_snapshot(intent, exposure_pairs=[])

    assert calls == ["quote:USDJPY", "spec:USDJPY", "rate:JPY"]


def test_gather_open_snapshot_deadline_independent_of_fixed_clock(tmp_path):
    """論理時計からの独立のピン (確定仕様テスト観点 3): `clock` は
    FixedClock (captured_at は常に NOW で不変) のまま、monotonic フェイク
    だけを進めて deadline が発火することを確認する。経過測定が誤って
    `self.clock` に戻る変異が入ると、captured_at が動かないため elapsed
    は恒等的に 0 になり、このテストは raise を観測できず red になる。"""
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"quote:USDJPY": BUDGET + 0.1})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    intent = _open_intent("USDJPY")

    with pytest.raises(DataUnhealthy):
        ex.gather_open_snapshot(intent, exposure_pairs=[])

    # clock 自体は 1 秒たりとも進んでいない (captured_at 由来の deadline
    # なら絶対に発火しないはずの状況で発火したことの反証材料)
    assert ex.clock.now() == NOW


# ---- Task 6: gather_close_snapshot (CLOSE 側 — OPEN と対称) -----------------

def test_gather_close_snapshot_completes_when_within_budget(tmp_path):
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(mono, leg_costs={})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    row = _insert_open_order(ex.conn, pair="USDJPY")

    snapshot = ex.gather_close_snapshot(row)

    assert calls == ["quote:USDJPY", "spec:USDJPY", "rate:JPY"]
    assert snapshot.price == QUOTE.bid


def test_gather_close_snapshot_boundary_exact_budget_still_succeeds(tmp_path):
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"quote:USDJPY": BUDGET})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    row = _insert_open_order(ex.conn, pair="USDJPY")

    snapshot = ex.gather_close_snapshot(row)

    assert calls == ["quote:USDJPY", "spec:USDJPY", "rate:JPY"]
    assert snapshot is not None


def test_gather_close_snapshot_aborts_before_spec(tmp_path):
    """CLOSE 対称のピン (CLOSE-1, 確定仕様テスト観点 5): quote_fn で予算
    超過 → spec_fn(pair) が呼ばれない。初稿の「CLOSE は打ち切らない」
    (非対称) は改稿 I1 で誤りと判明し撤回された — ここは初稿を反転させる。
    """
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"quote:USDJPY": BUDGET + 0.1})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    row = _insert_open_order(ex.conn, pair="USDJPY")

    with pytest.raises(DataUnhealthy, match="deadline exceeded"):
        ex.gather_close_snapshot(row)

    assert calls == ["quote:USDJPY"]


def test_gather_close_snapshot_aborts_before_resolve_close_rate(tmp_path):
    """CLOSE 対称のピン (CLOSE-2): spec_fn で予算超過 →
    resolve_close_rate 経由の rate_fn が呼ばれない。"""
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"spec:USDJPY": BUDGET + 0.1})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    row = _insert_open_order(ex.conn, pair="USDJPY")

    with pytest.raises(DataUnhealthy, match="deadline exceeded"):
        ex.gather_close_snapshot(row)

    assert calls == ["quote:USDJPY", "spec:USDJPY"]


# ---- Task 7: 脚伝播 (Executor → rate_fn への deadline_check 配線) ----------

def test_cycle_rate_fn_propagates_deadline_check_to_rate_fn(tmp_path):
    """Task 7: `cycle_rate_fn` が `deadline_check` を `rate_fn` へ転送する
    こと (`to_account_rate` 自体の脚検査は price_provider 側のテストが
    担うので、ここでは「渡ったこと」だけを見る configuration test)。"""
    mono = _Mono()
    received: list[object] = []

    def rate_fn(ccy, account_ccy, now, *, deadline_check=None):
        received.append(deadline_check)
        return ConversionRate(1.0, ccy, account_ccy, (now,))

    ex = _make_executor(tmp_path, monotonic_fn=mono,
                        quote_fn=lambda p: QUOTE, spec_fn=lambda p: SPECS[p],
                        rate_fn=rate_fn)
    intent = _open_intent("USDJPY")

    ex.gather_open_snapshot(intent, exposure_pairs=[])

    assert len(received) == 2  # JPY, USD の 2 通貨
    assert all(cb is not None for cb in received)
    # 1 回の gather = 1 つの deadline (通貨ごとに別オブジェクトを渡さない)
    assert len(set(id(cb) for cb in received)) == 1
    # 受け取った callable が「生きた予算つき checker」であることの pin
    # (no-op ラムダに差し替える変異を殺す — 着手前検証)
    mono.t += BUDGET + 0.1
    with pytest.raises(DataUnhealthy):
        received[0]("probe")


def test_resolve_close_rate_propagates_deadline_check_to_rate_fn(tmp_path):
    """Task 7: `gather_close_snapshot` → `resolve_close_rate` →
    `rate_fn` への転送 (CLOSE 側)。"""
    mono = _Mono()
    received: list[object] = []

    def rate_fn(ccy, account_ccy, now, *, deadline_check=None):
        received.append(deadline_check)
        return ConversionRate(1.0, ccy, account_ccy, (now,))

    ex = _make_executor(tmp_path, monotonic_fn=mono,
                        quote_fn=lambda p: QUOTE, spec_fn=lambda p: SPECS[p],
                        rate_fn=rate_fn)
    row = _insert_open_order(ex.conn, pair="USDJPY")

    ex.gather_close_snapshot(row)

    assert len(received) == 1
    assert received[0] is not None


def test_gather_close_snapshot_absorbs_cross_leg_deadline_into_degraded_rate(
        tmp_path):
    """CLOSE の吸収契約の pin (advisor 指摘): 実際の `PriceProvider.
    to_account_rate` を `rate_fn` に束縛し、EUR→JPY の USD クロス
    (2 脚) の 1 脚目取得後に deadline を超過させる。`resolve_close_rate`
    は例外を握りつぶし degraded フォールバックにする既存契約 (設計書
    §5「クローズはレート欠損でも妨げない」) があるため、
    `gather_close_snapshot` 自体は例外を投げずに完了する。ただし 2 脚目
    (USDJPY) の quote は一度も取得されていないこと (=打ち切りが実際に
    効いていること) まで確認する — 「degraded になった」だけでは
    (rate_fn が別の理由で単純に失敗しても同じ結果になるため) 偶然の
    一致と区別できない。

    本番の `_SPECS` (USDJPY/EURUSD のみ) には quote_currency=EUR を持つ
    銘柄が無い (account_currency=JPY からは常に直接ペアで解決できる) ため、
    このテストだけ架空の EURJPY spec を注入して EUR→JPY クロスを踏ませる。
    """
    mono = _Mono()
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    record_snapshot(conn, now=NOW, balance=1_000_000, equity=1_000_000)
    from agentic_fx.datafeed.price_provider import PriceProvider
    provider = PriceProvider(conn, SETTINGS, FixedClock(NOW))

    def rate_fn(ccy, account_ccy, now, **kw):
        return provider.to_account_rate(
            ccy, account_ccy, reference_ts=now,
            max_skew_min=SETTINGS.datafeed.conversion_skew_max_min, **kw)

    eurjpy_spec = InstrumentSpec(
        symbol="EURJPY", pip_size=0.01, min_lot=0.01, max_lot=50.0,
        lot_step=0.01, contract_size=100_000,
        base_currency="EUR", quote_currency="EUR")

    def quote_fn(pair):
        return Quote("EURJPY", 160.0, 160.2, NOW, "test")

    def spec_fn(pair):
        return eurjpy_spec

    ex = Executor(
        conn=conn, broker=PaperBroker(conn, SETTINGS, FixedClock(NOW)),
        settings=SETTINGS, state_store=StateStore(tmp_path / "state.json"),
        activity=ActivityLog(tmp_path / "activity.log"),
        notifier=Notifier(enabled=False, webhook_url=None),
        clock=FixedClock(NOW), monotonic_fn=mono,
        quote_fn=quote_fn, spec_fn=spec_fn, rate_fn=rate_fn)
    row = _insert_open_order(ex.conn, pair="EURJPY")

    def eurusd_quote(pair):
        assert pair == "EURUSD"
        mono.t += BUDGET + 0.1  # 1 脚目取得に予算を使い切ったことを模す (相対加算 — `_Mono` の基準は 12_345.0)
        return Quote("EURUSD", 1.08, 1.09, NOW, "yfinance")

    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               side_effect=eurusd_quote) as yq:
        snapshot = ex.gather_close_snapshot(row)

    assert snapshot.rate_degraded is True
    assert snapshot.rate is None
    # 2 脚目 (USDJPY) は一度も取得されていない (打ち切りが効いている)
    assert yq.call_count == 1
    assert yq.call_args.args[0] == "EURUSD"

    # ---- 観測性の pin (/code-review 2 周目) -------------------------------
    # 吸収経路を通っても degraded activity に原因が残ること。ここまで
    # 駆動しないと `CloseSnapshot.rate_degraded_reason` の受け渡しを
    # 一切踏まない (activity 行を書くのは `_finish_close` だけ)。
    ex.close_order_from_snapshot(row, snapshot, reason="llm_close")

    degraded_lines = [
        line for line in ex.activity.tail(n=100, category=Category.TRADE)
        if "close_pnl_rate_degraded" in line]
    assert len(degraded_lines) == 1, degraded_lines
    # 「ハング打ち切り」と「ベンダ障害」がログ上で識別できること。
    # `_make_deadline_checker` が確保した文言がここまで届く。
    assert "deadline exceeded" in degraded_lines[0], degraded_lines[0]
    assert "DataUnhealthy" in degraded_lines[0], degraded_lines[0]


# ---- codex 1 周目 指摘 2: gather ごとに新しい checker になること ----------

def test_each_gather_open_gets_a_fresh_budget(tmp_path):
    """1 回の gather ごとに新しい checker が作られる (予算は gather 単位で
    リセットされる) ことのピン。checker を Executor にキャッシュして使い
    回す退行では、2 回目の gather の最初の検査が 1 回目の経過を引き継いで
    予算超過になり red になる。"""
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"quote:USDJPY": BUDGET * 0.9})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    intent = _open_intent("USDJPY")

    ex.gather_open_snapshot(intent, exposure_pairs=[])
    ex.gather_open_snapshot(intent, exposure_pairs=[])

    assert calls == ["quote:USDJPY", "spec:USDJPY", "rate:JPY", "rate:USD"] * 2


def test_each_gather_close_gets_a_fresh_budget(tmp_path):
    """CLOSE 側の対称ピン (同上)。"""
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"quote:USDJPY": BUDGET * 0.9})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    row = _insert_open_order(ex.conn)

    ex.gather_close_snapshot(row)
    ex.gather_close_snapshot(row)

    assert calls == ["quote:USDJPY", "spec:USDJPY", "rate:JPY"] * 2


# ---- 束B 1周目 ローカル LLM レビュー: 検査は「脚の前」かの判別 -------------

def test_gather_open_snapshot_aborts_before_first_exposure_pair_spec(tmp_path):
    """打ち切りのピン (OPEN-2', 検査位置の判別): intent pair の spec_fn で
    予算超過 → exposure ループの **1 件目** (EURUSD) の spec_fn が呼ばれない。

    既存の OPEN-2 は超過コストを「そのループ反復の脚自身」(spec:EURUSD) に
    載せているため、`check(f"spec:{pair}")` を `self.spec_fn(pair)` の
    **後ろ**へ移す変異でも呼び出し列と raise が完全に一致し green のまま
    生き延びる (実測 1923 passed)。超過を「1 つ前の脚」に載せて初めて
    前後が区別できる。"""
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"spec:USDJPY": BUDGET + 0.1})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    intent = _open_intent("USDJPY")

    with pytest.raises(DataUnhealthy, match="deadline exceeded"):
        ex.gather_open_snapshot(intent, exposure_pairs=["EURUSD", "GBPUSD"])

    assert calls == ["quote:USDJPY", "spec:USDJPY"]


def test_gather_open_snapshot_aborts_before_first_currency_rate(tmp_path):
    """打ち切りのピン (OPEN-3', 検査位置の判別): intent pair の spec_fn で
    予算超過 → rates ループの **1 件目** (JPY) の rate_fn が呼ばれない。
    既存の OPEN-3 は `check(f"rate:{ccy}")` を `cycle_rate(ccy)` の後ろへ
    移す変異を検出できない (同上、実測 1923 passed)。"""
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"spec:USDJPY": BUDGET + 0.1})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    intent = _open_intent("USDJPY")

    with pytest.raises(DataUnhealthy, match="deadline exceeded"):
        ex.gather_open_snapshot(intent, exposure_pairs=[])

    assert calls == ["quote:USDJPY", "spec:USDJPY"]
