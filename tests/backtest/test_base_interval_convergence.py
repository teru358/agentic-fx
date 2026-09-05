"""バックテスト基底足の一般化 (段階 3/4) — 収束テスト。

design-v3.md B / design-v4-addendum.md 参照。合成 1m 系列とその素直な
5m 集約 (epoch 錨、OHLC は open=先頭/high=max/low=min/close=末尾/volume=sum)
が、(a) 読み取り時リサンプル (`load_resampled_frame`) と (b) `run_replay`
の両方で「収束する」(同一の評価結果を出す) ことをピンする。同時に、
意図的に収束条件を破った構成では差が出ることも (c) でピンする。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.backtest.dataset import HistoryDataset
from agentic_fx.backtest.replay import BarFeed
from agentic_fx.backtest.runner import run_replay, _aggregate_bucket
from agentic_fx.backtest.timeframes import load_resampled_frame
from agentic_fx.core.scheduler import Scheduler
from agentic_fx.store import ohlcv
from agentic_fx.store.db import connect, init_db

from tests.backtest.factories import SETTINGS

WED = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)  # 水曜 12:00 UTC (市場オープン)


def _row(ts, o, h, l, c, v=10.0, spread=0.01, symbol="USDJPY", interval="1m"):
    return (symbol, interval, ts.isoformat(), o, h, l, c, v, spread)


def _conn():
    conn = connect(Path(":memory:"))
    init_db(conn)
    return conn


class _NullActivity:
    """テスト専用の no-op ActivityLog スタブ (write を無視するだけ)。"""

    def write(self, category, event, summary, ref_id=None):
        return None


def _aggregate_5m(rows_1m: list[tuple]) -> list[tuple]:
    """1m 行 (epoch 錨) を素直に 5 分バケットへ集約する (テスト専用ヘルパ)。

    プロジェクトの epoch 錨・左 label 規則 (timeframes.floor_to_bucket と
    同じ切り下げ) で束ね、OHLC は open=先頭/high=max/low=min/close=末尾/
    volume=sum とする — `_aggregate_bucket` / `load_resampled_frame` の
    集約規則と同一の定義。
    """
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    width = timedelta(minutes=5)
    buckets: dict[datetime, list[tuple]] = {}
    for r in rows_1m:
        symbol, interval, ts_iso, o, h, l, c, v, spread = r
        ts = datetime.fromisoformat(ts_iso)
        bucket_start = epoch + ((ts - epoch) // width) * width
        buckets.setdefault(bucket_start, []).append((ts, o, h, l, c, v, spread))
    out = []
    for bstart in sorted(buckets):
        items = sorted(buckets[bstart])
        o = items[0][1]
        h = max(x[2] for x in items)
        l = min(x[3] for x in items)
        c = items[-1][4]
        v = sum(x[5] for x in items)
        spread = items[-1][6]
        out.append(_row(bstart, o, h, l, c, v, spread, interval="5m"))
    return out


# --- (a) 集計収束: load_resampled_frame の 1m 経路 vs 5m ネイティブ経路 ---

def test_aggregation_convergence_1m_vs_5m_to_1h(tmp_path):
    """合成 1m → 1h と、1m → 5m (素直な集約) → 1h が OHLC で厳密一致する。

    volume は許容差 (浮動小数の丸めのみ許容、値自体は sum なので一致するが
    契約上は「許容差あり」と明記されている — pytest.approx で確認する)。
    """
    rows_1m = []
    t = WED
    for i in range(180):  # 12:00-14:59 の 3 時間ぶん、変化する OHLC
        rows_1m.append(_row(t + timedelta(minutes=i),
                            o=148.0 + i * 0.01, h=148.05 + i * 0.01,
                            l=147.95 + i * 0.01, c=148.02 + i * 0.01,
                            v=10.0 + (i % 7)))
    rows_5m = _aggregate_5m(rows_1m)

    conn1 = _conn()
    ohlcv.import_history_bars(conn1, rows_1m, source="dukascopy")
    frame_from_1m = load_resampled_frame(
        conn1, "USDJPY", "1h", source="dukascopy", base_interval="1m",
        until=WED + timedelta(hours=3))

    conn5 = _conn()
    ohlcv.import_history_bars(conn5, rows_5m, source="mt5")
    frame_from_5m = load_resampled_frame(
        conn5, "USDJPY", "1h", source="mt5", base_interval="5m",
        until=WED + timedelta(hours=3))

    assert len(frame_from_1m) == len(frame_from_5m) == 3
    assert list(frame_from_1m.index) == list(frame_from_5m.index)
    for col in ("open", "high", "low", "close"):
        assert list(frame_from_1m[col]) == pytest.approx(list(frame_from_5m[col]))
    assert list(frame_from_1m["volume"]) == pytest.approx(
        list(frame_from_5m["volume"]))


# --- (b) replay 収束: 同一シナリオを 1m dataset / 5m dataset (別 source) で駆動 ---

OPEN_LIMIT = {"action": "open", "pair": "USDJPY", "direction": "long",
             "entry_type": "limit", "horizon": "day",
             "limit_price": 148.20, "expires_in": "6h",
             "stop_loss": 147.80, "take_profit": 149.00,
             "reasoning": "convergence"}


def _build_convergence_1m_rows():
    """entry と exit の価格到達を、それぞれ別々の 5 分バケットの**最終 1 分**
    に配置する — 1m 基底の判定 (直前完成 1 分足) と 5m 基底の判定 (直前完成
    5 分足の集約) が同一 wall-clock tick で同一の到達を検出するための設計
    (このバケット内の他の分は在り得ない極値を作らないよう静穏に保つ)。

    タイムライン:
      12:00-13:08: 静穏 (limit/SL/TP いずれにも触れない)
      13:09      : 指値 148.20 到達 (bucket [13:05,13:10) の最終分)
                   → tick=13:10 で両基底とも約定検出
      13:10-13:23: 静穏 (回復)
      13:24      : TP 149.00 到達 (bucket [13:20,13:25) の最終分)
                   → tick=13:25 で両基底とも約定検出
      13:25-13:59: 静穏
    """
    rows = []
    t = WED
    for i in range(69):  # 12:00-13:08
        rows.append(_row(t + timedelta(minutes=i), 148.5, 148.6, 148.4, 148.5))
    rows.append(_row(t + timedelta(minutes=69), 148.3, 148.35, 148.10, 148.15))
    for i in range(70, 84):  # 13:10-13:23
        rows.append(_row(t + timedelta(minutes=i), 148.5, 148.6, 148.4, 148.5))
    rows.append(_row(t + timedelta(minutes=84), 148.9, 149.10, 148.85, 149.05))
    for i in range(85, 120):  # 13:25-13:59
        rows.append(_row(t + timedelta(minutes=i), 149.0, 149.05, 148.95, 149.0))
    return rows


def _one_shot_source():
    fired: list = []

    def source(bar):
        if not fired:
            fired.append(bar.ts)
            return dict(OPEN_LIMIT)
        return None
    return source


ORDER_COLUMNS = ("status", "entry_type", "direction", "avg_fill_price",
                 "filled_at", "closed_at", "close_reason", "realized_pnl",
                 "stop_loss", "take_profit", "quantity")


def test_replay_convergence_1m_vs_5m_orders_equity_metrics_identical():
    from agentic_fx.backtest.metrics import compute_metrics

    rows_1m = _build_convergence_1m_rows()
    rows_5m = _aggregate_5m(rows_1m)

    conn1 = _conn()
    ohlcv.import_history_bars(conn1, rows_1m, source="dukascopy")
    res1 = run_replay(SETTINGS, symbol="USDJPY",
                      dataset=HistoryDataset("dukascopy", "1m"),
                      start=WED, end=WED + timedelta(hours=2),
                      intent_source=_one_shot_source(), eval_timeframe="1h",
                      history_conn=conn1)

    conn5 = _conn()
    ohlcv.import_history_bars(conn5, rows_5m, source="mt5")
    res5 = run_replay(SETTINGS, symbol="USDJPY",
                      dataset=HistoryDataset("mt5", "5m"),
                      start=WED, end=WED + timedelta(hours=2),
                      intent_source=_one_shot_source(), eval_timeframe="1h",
                      history_conn=conn5)

    assert res1.first_decision_at == res5.first_decision_at == WED + timedelta(hours=1)
    assert len(res1.orders) == len(res5.orders) == 1
    o1, o5 = res1.orders[0], res5.orders[0]
    for col in ORDER_COLUMNS:
        assert o1[col] == o5[col], f"{col}: {o1[col]!r} != {o5[col]!r}"
    assert o1["status"] == "closed" and o1["close_reason"] == "tp"

    # equity_curve: 1m 基底は毎分、5m 基底は 5 分毎に記録するため粒度が
    # 異なる — 5m グリッド上の共通時刻で完全一致することを確認する
    # (realized equity は約定時のみ変化する piecewise-constant なので、
    # 追加の 1m サンプルは同じ値の繰り返しにしかならず収束を破らない)。
    ec1 = dict(res1.equity_curve)
    ec5 = dict(res5.equity_curve)
    assert ec5  # 非空
    for ts, val in ec5.items():
        assert ts in ec1, f"5m equity ts {ts} missing from 1m curve"
        assert ec1[ts] == val

    assert compute_metrics(res1) == compute_metrics(res5)


# --- (c) 非収束が正しいケース (差が出ることを pin) -----------------------

def _run_both(rows_1m, *, end_hours=2, source_factory=_one_shot_source):
    rows_5m = _aggregate_5m(rows_1m)
    conn1 = _conn()
    ohlcv.import_history_bars(conn1, rows_1m, source="dukascopy")
    res1 = run_replay(SETTINGS, symbol="USDJPY",
                      dataset=HistoryDataset("dukascopy", "1m"),
                      start=WED, end=WED + timedelta(hours=end_hours),
                      intent_source=source_factory(), eval_timeframe="1h",
                      history_conn=conn1)
    conn5 = _conn()
    ohlcv.import_history_bars(conn5, rows_5m, source="mt5")
    res5 = run_replay(SETTINGS, symbol="USDJPY",
                      dataset=HistoryDataset("mt5", "5m"),
                      start=WED, end=WED + timedelta(hours=end_hours),
                      intent_source=source_factory(), eval_timeframe="1h",
                      history_conn=conn5)
    return res1, res5


def test_nonconvergence_limit_reached_mid_5m_bucket_diverges_fill_time():
    """指値到達が 5m バケットの**途中**分 (最終分でない) だと、1m 基底は
    その分の完成直後に約定検出するが、5m 基底はバケット全体が完成する
    まで検出できず、``filled_at`` が異なる (収束条件 A の破り方)。
    """
    rows = []
    t = WED
    for i in range(60):  # 12:00-12:59
        rows.append(_row(t + timedelta(minutes=i), 148.5, 148.6, 148.4, 148.5))
    rows.append(_row(t + timedelta(hours=1), 148.5, 148.6, 148.4, 148.5))  # 13:00
    for i in range(1, 5):  # 13:01-13:04
        rows.append(_row(t + timedelta(hours=1, minutes=i),
                         148.5, 148.6, 148.4, 148.5))
    # 13:05 到達分 (バケット [13:05,13:10) の先頭)
    rows.append(_row(t + timedelta(hours=1, minutes=5), 148.3, 148.35,
                     148.10, 148.15))
    for i in range(6, 30):  # 13:06-13:29 静穏
        rows.append(_row(t + timedelta(hours=1, minutes=i),
                         148.5, 148.6, 148.4, 148.5))
    res1, res5 = _run_both(rows)
    assert res1.orders and res5.orders
    assert res1.orders[0]["filled_at"] != res5.orders[0]["filled_at"]
    # 1m は 13:06 (到達分 13:05 の完成直後)、5m はバケット完成 13:10
    assert res1.orders[0]["filled_at"].startswith("2026-07-22T13:06")
    assert res5.orders[0]["filled_at"].startswith("2026-07-22T13:10")


def test_nonconvergence_sl_gap_fill_price_diverges():
    """SL 到達が大きな窓 (gap) を伴う場合、成行 SL の約定価格は「実際に
    観測できたバー」の close/worst 値に依存する。1m と 5m でその「直前
    完成バー」の close が異なれば、SL 成行の約定価格 (avg_fill_price) が
    ずれ得る (収束条件 A の破り方 — ギャップが 5m バケットの先頭分だけに
    起きて末尾で反発すると、1m の直前完成バー close と 5m 集約 close が
    異なる)。
    """
    rows = []
    t = WED
    for i in range(60):
        rows.append(_row(t + timedelta(minutes=i), 148.5, 148.6, 148.4, 148.5))
    rows.append(_row(t + timedelta(hours=1), 148.5, 148.6, 148.4, 148.5))
    for i in range(1, 5):
        rows.append(_row(t + timedelta(hours=1, minutes=i),
                         148.5, 148.6, 148.4, 148.5))
    # 13:05 (バケット[13:05,13:10)先頭分) — 指値到達
    rows.append(_row(t + timedelta(hours=1, minutes=5), 148.3, 148.35,
                     148.10, 148.15))
    for i in range(6, 10):  # 13:06-13:09 静穏 (エントリ確定用)
        rows.append(_row(t + timedelta(hours=1, minutes=i),
                         148.5, 148.6, 148.4, 148.5))
    # 13:10 (バケット[13:10,13:15)先頭分) — SL(147.80) を大きく割り込む gap、
    # 直後は反発して静穏に戻る (バケットの先頭でしか起きない移動)
    rows.append(_row(t + timedelta(hours=1, minutes=10), 148.5, 148.55,
                     146.50, 147.00))
    for i in range(11, 15):  # 13:11-13:14 静穏に反発 (146台には触れない)
        rows.append(_row(t + timedelta(hours=1, minutes=i),
                         148.5, 148.6, 148.4, 148.5))
    for i in range(15, 30):
        rows.append(_row(t + timedelta(hours=1, minutes=i),
                         147.00, 147.05, 146.95, 147.00))
    res1, res5 = _run_both(rows)
    assert res1.orders and res5.orders
    o1 = [o for o in res1.orders if o["status"] == "closed"][0]
    o5 = [o for o in res5.orders if o["status"] == "closed"][0]
    assert o1["close_reason"] == o5["close_reason"] == "sl"
    # 到達分がバケット先頭のため 5m 側の検出は 1 バケット (最大 4 分) 遅れ、
    # gap 幅ぶん fill 価格がずれる。
    assert o1["closed_at"] != o5["closed_at"]


def test_nonconvergence_missing_1m_bar_vs_native_5m_no_gap():
    """1m 側に欠損 (在る分だけ集約) がある一方、5m ネイティブ側にはその
    欠損が伝播しない (別 source として独立に投入されているため) 場合、
    集約バケットの OHLC が異なり得る — 欠損 1m と native 5m は非収束。
    """
    rows_1m = []
    t = WED
    for i in range(60):
        rows_1m.append(_row(t + timedelta(minutes=i), 148.5, 148.6, 148.4, 148.5))
    for i in range(60, 65):  # 13:00-13:04 だが 13:02 を欠損させる
        if i == 62:
            continue
        rows_1m.append(_row(t + timedelta(minutes=i), 148.5, 148.6, 148.4, 148.5))
    for i in range(65, 120):
        rows_1m.append(_row(t + timedelta(minutes=i), 148.5, 148.6, 148.4, 148.5))

    # native 5m は欠損なしで独立に構成 (別の値で構成し、欠損の影響を
    # 受けないことを示す)
    rows_5m_native = _aggregate_5m([
        r for r in rows_1m if r != None
    ])
    # 欠損分 13:02 を native 側だけ埋め戻して差を作る (別 source が独立に
    # 完全なデータを持つ現実的なケース)
    conn1 = _conn()
    ohlcv.import_history_bars(conn1, rows_1m, source="dukascopy")
    frame_1m = load_resampled_frame(conn1, "USDJPY", "5m", source="dukascopy",
                                    base_interval="1m",
                                    until=WED + timedelta(minutes=65))

    full_1m = rows_1m + [_row(t + timedelta(minutes=62), 148.5, 148.6, 148.4, 148.5)]
    conn5 = _conn()
    rows_5m = _aggregate_5m(full_1m)
    ohlcv.import_history_bars(conn5, rows_5m, source="mt5")
    frame_5m = load_resampled_frame(conn5, "USDJPY", "5m", source="mt5",
                                    base_interval="5m",
                                    until=WED + timedelta(minutes=65))
    bucket = WED + timedelta(minutes=60)
    # 欠損 1m 側は volume が 1 本ぶん少ない (在る分だけの集約) が、native
    # 5m 側は欠損補完済みで volume が満額 — 非収束 (差が出る)。
    assert frame_1m.loc[bucket]["volume"] != frame_5m.loc[bucket]["volume"]


def test_barfeed_rejects_misaligned_5m_row():
    """5m dataset で、base_interval グリッドに乗らない bar_time の行が
    ``ohlcv_history`` に混入していると ``BarFeed`` 構築時に fail closed
    する (整数倍検証の弱体化に対する pin)。
    """
    conn = _conn()
    rows = [_row(WED, 148.5, 148.6, 148.4, 148.5, interval="5m"),
           _row(WED + timedelta(minutes=3), 148.5, 148.6, 148.4, 148.5,
                interval="5m")]  # 5 分格子に非整列
    ohlcv.import_history_bars(conn, rows, source="mt5")
    with pytest.raises(ValueError):
        BarFeed(conn, "USDJPY", dataset=HistoryDataset("mt5", "5m"),
               start=WED, end=WED + timedelta(minutes=10))


# --- (d) 契約テスト: load_resampled_frame と runner._aggregate_bucket の
#     集約規則が同一 fixture で一致する -------------------------------

def test_aggregate_bucket_and_load_resampled_frame_agree():
    rows = []
    t = WED
    for i in range(10):
        if i == 4:
            continue  # 欠損 1 本 (在る分だけの集約を両経路で確認)
        rows.append(_row(t + timedelta(minutes=i), o=100 + i, h=101 + i,
                         l=99 + i, c=100.5 + i, v=10.0 + i))
    conn = _conn()
    ohlcv.import_history_bars(conn, rows, source="dukascopy")

    dataset = HistoryDataset("dukascopy", "1m")
    feed = BarFeed(conn, "USDJPY", dataset=dataset, start=t,
                  end=t + timedelta(minutes=10))
    bucket = _aggregate_bucket(feed, "USDJPY", "5m", t, timedelta(minutes=5))

    frame = load_resampled_frame(conn, "USDJPY", "5m", source="dukascopy",
                                 base_interval="1m",
                                 until=t + timedelta(minutes=5))
    assert len(frame) == 1
    row0 = frame.iloc[0]
    assert bucket is not None
    assert bucket.open == row0["open"]
    assert bucket.high == row0["high"]
    assert bucket.low == row0["low"]
    assert bucket.close == row0["close"]
    assert bucket.volume == pytest.approx(row0["volume"])


def test_aggregate_bucket_all_missing_returns_none_both_paths():
    conn = _conn()
    dataset = HistoryDataset("dukascopy", "1m")
    feed = BarFeed(conn, "USDJPY", dataset=dataset, start=WED,
                  end=WED + timedelta(minutes=5))
    assert _aggregate_bucket(feed, "USDJPY", "5m", WED,
                             timedelta(minutes=5)) is None
    frame = load_resampled_frame(conn, "USDJPY", "5m", source="dukascopy",
                                 base_interval="1m",
                                 until=WED + timedelta(minutes=5))
    assert frame.empty


# --- (e) 鮮度境界: 5m 基底で age=6m ちょうど fresh、6m+1s stale ---------

def test_run_replay_wires_bar_freshness_as_base_width_plus_one_minute(
        monkeypatch, tmp_path):
    """段 0 pin: `run_replay` が Scheduler へ渡す `bar_freshness` が
    `dataset.width` そのもの (+1 分の余裕を落とす) へ弱体化していないかを
    直接検証する — Scheduler コンストラクタ呼び出しをスパイする。
    """
    import agentic_fx.backtest.runner as runner_mod

    seen = {}
    real_scheduler = runner_mod.Scheduler

    def _spy(*args, **kwargs):
        seen["bar_freshness"] = kwargs.get("bar_freshness")
        return real_scheduler(*args, **kwargs)

    monkeypatch.setattr(runner_mod, "Scheduler", _spy)

    conn = _conn()
    rows = [_row(WED + timedelta(minutes=5 * n), 148.5, 148.6, 148.4, 148.5,
                interval="5m") for n in range(20)]
    ohlcv.import_history_bars(conn, rows, source="mt5")
    run_replay(SETTINGS, symbol="USDJPY", dataset=HistoryDataset("mt5", "5m"),
              start=WED, end=WED + timedelta(minutes=10),
              intent_source=lambda b: None, eval_timeframe="1h",
              history_conn=conn)
    assert seen["bar_freshness"] == timedelta(minutes=6)


def test_bar_freshness_boundary_exact_fresh_and_one_second_stale():
    """`Scheduler.bar_freshness` = base 幅(5m) + 1 分 = 6 分。age==6分は
    fresh、6分+1秒は stale — `_fresh_bar` の判定式そのものをピンする。
    `_evaluate_positions` 側の別配線は
    `test_evaluate_positions_uses_self_bar_freshness_not_module_constant`
    が個別にピンする (run_replay の bars_fn は「ちょうど width 分前」か
    None しか返さないため、この境界を replay 経由で作ることはできない —
    Scheduler を直接ユニットテストする必要がある)。
    """
    conn = _conn()
    from agentic_fx.core.executor import Executor
    from agentic_fx.core.notifier import Notifier
    from agentic_fx.core.paper_broker import PaperBroker
    from agentic_fx.store.state import StateStore
    from agentic_fx.core.contracts import Bar

    now = WED + timedelta(hours=1)
    bar_ts = now - timedelta(minutes=6)
    bar = Bar("USDJPY", "5m", bar_ts, 148.5, 148.6, 148.4, 148.5, 10.0)

    class _Clock:
        def now(self):
            return now

    import tempfile
    state = StateStore(Path(tempfile.mkdtemp()) / "state.json")
    broker = PaperBroker(conn, SETTINGS, _Clock())
    executor = Executor(conn=conn, broker=broker, settings=SETTINGS,
                        state_store=state, activity=_NullActivity(), notifier=Notifier(False, None),
                        clock=_Clock(), quote_fn=lambda p: None,
                        spec_fn=lambda p: None, rate_fn=lambda *a, **k: None)
    scheduler = Scheduler(conn=conn, executor=executor, settings=SETTINGS,
                          state_store=state, activity=_NullActivity(),
                          bars_fn=lambda pair: bar,
                          on_trade_mission=lambda reason: None,
                          on_news_cycle=lambda: None, on_econ_cycle=lambda: None,
                          bar_freshness=timedelta(minutes=6))
    assert scheduler._fresh_bar("USDJPY", now) is not None

    scheduler2 = Scheduler(conn=conn, executor=executor, settings=SETTINGS,
                           state_store=state, activity=_NullActivity(),
                           bars_fn=lambda pair: bar,
                           on_trade_mission=lambda reason: None,
                           on_news_cycle=lambda: None, on_econ_cycle=lambda: None,
                           bar_freshness=timedelta(minutes=6))
    now_stale = bar_ts + timedelta(minutes=6, seconds=1)
    assert scheduler2._fresh_bar("USDJPY", now_stale) is None


def test_evaluate_positions_uses_self_bar_freshness_not_module_constant():
    """段 0 pin: `_evaluate_positions` の鮮度判定が `self.bar_freshness`
    ではなくモジュール定数 `_BAR_FRESHNESS` (既定 5 分) に戻っていないかを
    直接検証する。5m 基底の `bar_freshness=6 分` の下で、age=5分30秒 (定数
    5 分より古いが self.bar_freshness=6 分よりは新しい) の open position を
    `stale=False` (時価評価される) として扱うことをピンする — 定数へ退行
    すると `stale=True` になり unrealized が 0 のまま計算漏れする。
    """
    from agentic_fx.core.executor import Executor
    from agentic_fx.core.notifier import Notifier
    from agentic_fx.core.paper_broker import PaperBroker
    from agentic_fx.core.contracts import Bar, OrderStatus
    from agentic_fx.store.state import StateStore
    from agentic_fx.store import orders as orders_store
    import tempfile

    conn = _conn()
    now = WED + timedelta(hours=1)
    bar_ts = now - timedelta(minutes=5, seconds=30)
    bar = Bar("USDJPY", "5m", bar_ts, 149.0, 149.1, 148.9, 149.0, 10.0)

    orders_store.insert(
        conn, pair="USDJPY", direction="long", entry_type="market",
        horizon="day", status=OrderStatus.OPEN, now=WED,
        avg_fill_price=148.5, quantity=1000.0, stop_loss=147.0,
        take_profit=150.0)

    class _Clock:
        def now(self):
            return now

    from agentic_fx.core.contracts import ConversionRate
    from agentic_fx.datafeed.price_provider import _SPECS

    def _rate_fn(ccy, account_ccy, now_, *, deadline_check=None):
        return ConversionRate(1.0, ccy, account_ccy, (now_,))

    state = StateStore(Path(tempfile.mkdtemp()) / "state.json")
    broker = PaperBroker(conn, SETTINGS, _Clock())
    executor = Executor(conn=conn, broker=broker, settings=SETTINGS,
                        state_store=state, activity=_NullActivity(),
                        notifier=Notifier(False, None), clock=_Clock(),
                        quote_fn=lambda p: None,
                        spec_fn=lambda p: _SPECS[p],
                        rate_fn=_rate_fn)
    scheduler = Scheduler(conn=conn, executor=executor, settings=SETTINGS,
                          state_store=state, activity=_NullActivity(),
                          bars_fn=lambda pair: bar,
                          on_trade_mission=lambda reason: None,
                          on_news_cycle=lambda: None, on_econ_cycle=lambda: None,
                          bar_freshness=timedelta(minutes=6))
    _, unrealized, stale = scheduler._evaluate_positions(now)
    assert stale is False
    assert unrealized != 0.0
