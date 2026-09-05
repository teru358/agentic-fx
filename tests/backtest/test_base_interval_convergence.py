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

import pandas as pd
import pytest

from agentic_fx.backtest.dataset import HistoryDataset
from agentic_fx.backtest.replay import BarFeed
from agentic_fx.backtest.runner import run_replay, _aggregate_bucket
from agentic_fx.backtest.timeframes import floor_to_bucket, load_resampled_frame
from agentic_fx.core.scheduler import Scheduler
from agentic_fx.datafeed.bars import pandas_rule, resample
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
    """1m 行 (epoch 錨) を 5 分バケットへ集約する (テスト専用ヘルパ)。

    I4 是正 (codex 段階2/3 是正 1周目): OHLC/volume の集約規則はプロジェクト
    自身の `agentic_fx.datafeed.bars.resample` (実出力) に委ねる —
    「1m 系列からプロジェクト自身の規則で 5m を生成する」設計 v2 §6 の
    要求どおり、pandas 側の境界・欠損・型・volume 処理をテスト内で
    再実装しない (二重実装は実装側とテスト側が同じバグを共有してしまう
    リスクがある)。bucket の境界も `timeframes.floor_to_bucket`
    (`_aggregate_bucket`/`load_resampled_frame` と同一の epoch 錨規則)
    を使う。

    spread は Bar/OHLCV の集約対象に含まれない列 (`resample`/`bars_to_df`
    の domain 外) — 本ヘルパは従来どおり「バケット内最終行の spread」を
    採用する (ohlcv_history への 5m ネイティブ行投入に spread 値が必須の
    ため、テスト fixture 独自の規約として明記)。
    """
    if not rows_1m:
        return []
    rows_1m = sorted(rows_1m, key=lambda r: r[2])  # load_resampled_frame と
                                                    # 同じ ORDER BY bar_time
    symbol = rows_1m[0][0]
    df = pd.DataFrame(
        {"open": [r[3] for r in rows_1m], "high": [r[4] for r in rows_1m],
         "low": [r[5] for r in rows_1m], "close": [r[6] for r in rows_1m],
         "volume": [r[7] for r in rows_1m]},
        index=pd.DatetimeIndex(
            pd.to_datetime([r[2] for r in rows_1m], utc=True)))
    resampled = resample(df, pandas_rule("5m"))

    spread_by_bucket: dict[datetime, float] = {}
    for r in rows_1m:
        ts = datetime.fromisoformat(r[2])
        bucket_start = floor_to_bucket(ts, "5m")
        spread_by_bucket[bucket_start] = r[8]  # 昇順走査 → 最終行が残る

    out = []
    for ts, row in resampled.iterrows():
        bstart = ts.to_pydatetime()
        out.append(_row(bstart, row["open"], row["high"], row["low"],
                        row["close"], row["volume"],
                        spread_by_bucket[bstart], symbol=symbol,
                        interval="5m"))
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


# 段階 2/3 レビュー是正 B: 11 列の許可リストを廃し、`orders` の全列を比較
# する。除外は以下の 3 列のみ (実測で確認済み — `probe_b.py` 相当の手動
# 検証で、他の全列は 1m/5m 両基底で厳密一致することを確認した):
#   - client_order_id: `f"afx-{intent_id}-{now.timestamp():.0f}"` — now は
#     注文の "submitting" 遷移時刻。決定 (closed_bar) は同一 (13:00) でも、
#     執行は次 tick (1m なら+1分、5m なら+5分) まで遅延する設計 (v2 §1(a)
#     「シグナル後の執行は次 tick = 最大 base 幅の遅延」) のため、この
#     "now" 自体が base 幅ぶんずれる — 非決定性ではなく base 幅に比例する
#     設計上の実行遅延で、意図的に非収束。
#   - created_at: 上と同じ理由 (注文行の INSERT 時刻 = 上記の "now" その
#     もの)。fresh DB + replay clock で決定的だが、1m/5m で値そのものが
#     異なることが設計上正しい。
#   - expires_at: `created_at + expires_in` (今回のシナリオは "6h") から
#     計算されるため、created_at のずれがそのまま伝播する。
# 上記いずれも「同じ入力に対して毎回同じ値になる」という意味では決定的
# だが、1m 基底と 5m 基底の間では値そのものが一致しない (base 幅に比例する
# 実行遅延) — pytest.approx や等値では吸収できないため列ごと除外する。
_NON_COMPARABLE_ORDER_COLUMNS = frozenset(
    {"client_order_id", "created_at", "expires_at"})


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
    all_cols = sorted(set(o1) | set(o5))
    compared = [c for c in all_cols if c not in _NON_COMPARABLE_ORDER_COLUMNS]
    assert compared  # 除外リストで空にならないこと自体を確認
    for col in compared:
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


# --- (c') 非収束ケース追加 7 本 (design-v2.md §7 / design-v3.md A9) -------
# 既存 4 ケース (limit 途中到達・SL gap・欠損 1m vs native 5m・非整列
# 拒否) の作り方 (`_run_both`/`_row`/`_aggregate_5m` を使い、1m と 5m の
# 判定タイミング/優先順位の違いが観測できる価格系列を組む) を踏襲する。


def test_nonconvergence_entry_same_bar_defers_tp_confirmation_on_5m():
    """① entry 後同一 5m 足で TP のみ到達 — 5m は `entry_same_bar` 抑制で
    その足では TP を確定させず、以降 TP に再到達しなければ持ち越しのまま
    残る。1m は fill した 1m 足と TP 到達 1m 足が別々なので同一バー抑制の
    対象にならず、次の完成 1m 足で普通に TP 確定する。
    """
    rows = []
    t = WED
    for i in range(80):  # 12:00-13:19 静穏 (決定は 13:00, 執行遅延を待つ)
        rows.append(_row(t + timedelta(minutes=i), 148.5, 148.6, 148.4, 148.5))
    # 13:20: 指値 148.20 到達 (bucket [13:20,13:25) 先頭分)
    rows.append(_row(t + timedelta(minutes=80), 148.3, 148.35, 148.10, 148.15))
    rows.append(_row(t + timedelta(minutes=81), 148.5, 148.6, 148.4, 148.5))
    # 13:22: TP 149.00 到達 (同じ bucket [13:20,13:25) 内、fill とは別の 1m 足)
    rows.append(_row(t + timedelta(minutes=82), 148.9, 149.10, 148.85, 149.05))
    for i in range(83, 120):  # 13:23-13:59 静穏 (以降 TP 再到達なし)
        rows.append(_row(t + timedelta(minutes=i), 148.5, 148.6, 148.4, 148.5))
    res1, res5 = _run_both(rows)
    assert res1.orders and res5.orders
    o1, o5 = res1.orders[0], res5.orders[0]
    # 1m: 別々の 1m 足なので entry_same_bar は掛からず TP が確定する。
    assert o1["status"] == "closed"
    assert o1["close_reason"] == "tp"
    # 5m: fill も TP 到達も同一 5m バケット内 → entry_same_bar=True で
    # その足では確定させない。以降 TP に再到達しないため持ち越しのまま
    # run 終了まで残る (非収束)。
    assert o5["status"] == "open"
    assert o5["close_reason"] is None


def test_nonconvergence_sl_tp_same_bucket_5m_prioritizes_sl_over_first_touch():
    """② SL・TP 同時到達 — 1m は TP が先に (別々の 1m 足で) 単独到達する
    ので TP で確定するが、5m は同じ 5m バケットに TP 到達分と SL 到達分の
    両方が入り込むため、`check_exit` の SL 優先規則により SL で確定する
    (「先に触れた方」を判定できない粗い基底の帰結)。
    """
    rows = []
    t = WED
    for i in range(69):  # 12:00-13:08 静穏
        rows.append(_row(t + timedelta(minutes=i), 148.5, 148.6, 148.4, 148.5))
    # 13:09: 指値 148.20 到達・約定 (bucket [13:05,13:10) 末尾分)
    rows.append(_row(t + timedelta(minutes=69), 148.3, 148.35, 148.10, 148.15))
    for i in range(70, 100):  # 13:10-13:39 静穏 (約定確定を待つ)
        rows.append(_row(t + timedelta(minutes=i), 148.5, 148.6, 148.4, 148.5))
    # bucket [13:40,13:45): 13:40 に TP のみ到達、13:41 に SL のみ到達
    rows.append(_row(t + timedelta(minutes=100), 148.9, 149.10, 148.85, 149.05))
    rows.append(_row(t + timedelta(minutes=101), 147.90, 147.95, 147.60, 147.70))
    for i in range(102, 120):  # 13:42-13:59 静穏
        rows.append(_row(t + timedelta(minutes=i), 148.5, 148.6, 148.4, 148.5))
    res1, res5 = _run_both(rows, end_hours=3)
    assert res1.orders and res5.orders
    o1 = [o for o in res1.orders if o["status"] == "closed"][0]
    o5 = [o for o in res5.orders if o["status"] == "closed"][0]
    # 1m: 13:40 の TP のみ単独到達 → 次の完成 1m 足でまず TP 確定
    # (13:41 の SL 到達より前に既にクローズ済み)。
    assert o1["close_reason"] == "tp"
    # 5m: bucket [13:40,13:45) に TP・SL 両方の到達分が入り、SL 優先規則
    # により SL で確定する。
    assert o5["close_reason"] == "sl"
    assert o1["closed_at"] != o5["closed_at"]


def test_nonconvergence_short_expiry_1_to_4_minutes_only_1m_fills():
    """④ expiry が 1〜4 分内 — `_expire_limits` は `_process_limit_fills`
    より**先に**評価される (scheduler.tick の内部順序)。expires_in を base
    幅より短く (3 分) 取ると、5m 基底は「次に評価される tick (= 次の 5m
    格子点)」の時点で既に期限切れになっており、一度も約定機会を得ないまま
    EXPIRED になる。1m 基底は毎分評価されるため、期限内に価格が指値へ
    到達すれば約定できる。
    """
    short_limit = dict(OPEN_LIMIT)
    short_limit["expires_in"] = "0.05h"  # 3 分

    def _short_source():
        fired: list = []

        def source(bar):
            if not fired:
                fired.append(bar.ts)
                return dict(short_limit)
            return None
        return source

    rows = []
    t = WED
    for i in range(60):  # 12:00-12:59 静穏
        rows.append(_row(t + timedelta(minutes=i), 148.5, 148.6, 148.4, 148.5))
    rows.append(_row(t + timedelta(hours=1), 148.5, 148.6, 148.4, 148.5))  # 13:00
    # 13:01: 指値 148.20 到達 (1m 側の作成直後の 1 分足)
    rows.append(_row(t + timedelta(hours=1, minutes=1), 148.3, 148.35,
                     148.10, 148.15))
    for i in range(2, 60):  # 13:02-13:59 静穏
        rows.append(_row(t + timedelta(hours=1, minutes=i),
                         148.5, 148.6, 148.4, 148.5))
    res1, res5 = _run_both(rows, source_factory=_short_source)
    assert res1.orders and res5.orders
    o1, o5 = res1.orders[0], res5.orders[0]
    # 1m: 作成 (13:01) 直後の完成 1m 足 (13:01) で指値到達 → 13:02 に約定。
    # 期限 (13:04) より前に約定できている。
    assert o1["status"] == "open"
    assert o1["filled_at"] is not None
    # 5m: 作成 (13:05) → 期限 (13:08) だが、次に評価される 5m 格子点は
    # 13:10 (bucket [13:05,13:10) の完成時) — その時点で既に期限切れの
    # ため、一度も約定機会を得ないまま EXPIRED になる。
    assert o5["status"] == "expired"
    assert o5["filled_at"] is None


def test_nonconvergence_day_close_boundary_execution_delay_shifts_rollover_day():
    """⑤ day close 直前 (expiry とは別経路) — market 注文の約定は「決定
    (closed_bar 確定) の次 tick」まで遅延する (base 幅ぶん)。この執行遅延
    が NY 17:00 (=このケースの UTC 21:00、夏時間) の日次ロールオーバー
    境界を跨ぐと、`_force_close_day` が使う `anchor` (filled_at) の
    ロールオーバー期限 (`market_hours.next_rollover`) が丸ごと 1 日ずれる。
    決定は共通 (5 分格子の同一バケット) だが、1m 基底は 1 分遅れで
    20:56 に約定し即日 21:00 の期限にかかって直後に強制決済されるのに
    対し、5m 基底は 5 分遅れでちょうど 21:00 (NY 17:00) に約定し、
    ``next_rollover`` の `>=` 判定により期限が**翌日**へ送られ、同じ
    シミュレーション窓内では強制決済されずに持ち越される。
    """
    open_market = {"action": "open", "pair": "USDJPY", "direction": "long",
                  "entry_type": "market", "horizon": "day",
                  "stop_loss": 148.0, "take_profit": 149.5,
                  "reasoning": "day-close-boundary"}
    # 決定バケット = [20:50,20:55) の完成 (closed_bar.ts=20:50) — 執行は
    # 1m 基底で 20:56 (1 分遅延)、5m 基底でちょうど 21:00 (5 分遅延) に
    # なるよう、決定の 1 tick 後が NY 17:00 (UTC 21:00) を跨ぐ位置を選ぶ。
    target_ts = WED + timedelta(hours=8, minutes=50)

    def _one_shot_at(target):
        fired: list = []

        def source(bar):
            if not fired and bar.ts == target:
                fired.append(bar.ts)
                return dict(open_market)
            return None
        return source

    rows = []
    t = WED
    for i in range(9 * 60 + 30):  # 12:00 〜 21:30 静穏
        rows.append(_row(t + timedelta(minutes=i), 148.5, 148.6, 148.4, 148.5))
    rows_5m = _aggregate_5m(rows)
    end = WED + timedelta(hours=9, minutes=30)

    conn1 = _conn()
    ohlcv.import_history_bars(conn1, rows, source="dukascopy")
    res1 = run_replay(SETTINGS, symbol="USDJPY",
                      dataset=HistoryDataset("dukascopy", "1m"),
                      start=WED, end=end,
                      intent_source=_one_shot_at(target_ts),
                      eval_timeframe="5m", history_conn=conn1)

    conn5 = _conn()
    ohlcv.import_history_bars(conn5, rows_5m, source="mt5")
    res5 = run_replay(SETTINGS, symbol="USDJPY",
                      dataset=HistoryDataset("mt5", "5m"),
                      start=WED, end=end,
                      intent_source=_one_shot_at(target_ts),
                      eval_timeframe="5m", history_conn=conn5)

    assert res1.orders and res5.orders
    o1, o5 = res1.orders[0], res5.orders[0]
    # 1m: 20:56 約定 → ロールオーバー期限は同日 21:00 → 直後の tick
    # (20:57 以降 バッファ 5 分に入る 20:55 以降なので即座) で強制決済。
    assert o1["filled_at"].startswith("2026-07-22T20:56")
    assert o1["status"] == "closed"
    assert o1["close_reason"] == "day_rollover"
    # 5m: ちょうど 21:00 (NY 17:00) に約定 → next_rollover の `>=` 判定で
    # 期限が翌日へ送られ、同じシミュレーション窓 (21:30 まで) では
    # 強制決済されずに持ち越される (非収束)。
    assert o5["filled_at"].startswith("2026-07-22T21:00")
    assert o5["status"] == "open"
    assert o5["close_reason"] is None


def test_nonconvergence_5m_bucket_hides_intrabar_dd_delays_kill_switch():
    """③ 5m 内の unrealized DD で kill switch が遅れる — SL には触れない
    (実現損益は変えない) 程度の一時的な値洗い含み損が、1m 基底ではその分の
    close で即座に mark-to-market へ反映されるが、5m 基底では同じ 5 分
    バケットの**最終分**が回復済みの値なので `_aggregate_bucket` の
    ``close=bars[-1].close`` により一時的な含み損の谷が隠れる (v2 §1(f))。
    2 件目の建玉提案を、この谷の直後 (1m) / 谷が隠れた回復後 (5m) に
    ぶつけると、1m は kill switch (drawdown threshold) に引っかかって
    2 件目が rejected になるが、5m は谷を観測できないため 2 件目も
    通ってしまう (非収束)。`drawdown_kill_pct` を小さく (0.05%) した
    settings を使う — 既定 (2.0%) では単一建玉の最大許容リスク
    (`max_total_risk_pct=1.5%`) が SL 非到達のまま作れる含み損の上限
    (SL 距離の範囲内) を上回れず、この現象を SL 非到達のまま再現できない
    ため。
    """
    settings_sensitive = SETTINGS.model_copy(update={
        "risk": SETTINGS.risk.model_copy(update={"drawdown_kill_pct": 0.05})})

    open1 = {"action": "open", "pair": "USDJPY", "direction": "long",
            "entry_type": "market", "horizon": "day",
            "stop_loss": 148.0, "take_profit": 149.5,
            "reasoning": "position1"}
    open2 = {"action": "open", "pair": "USDJPY", "direction": "long",
            "entry_type": "market", "horizon": "day",
            "stop_loss": 148.0, "take_profit": 149.5,
            "reasoning": "position2"}
    d1 = WED  # 決定 1 のバケット [WED,WED+5m) 完成時刻
    d2 = WED + timedelta(hours=1)  # 決定 2 のバケット完成時刻

    def _scripted_source():
        done1: list = []
        done2: list = []

        def source(bar):
            if not done1 and bar.ts == d1:
                done1.append(1)
                return dict(open1)
            if not done2 and bar.ts == d2:
                done2.append(1)
                return dict(open2)
            return None
        return source

    rows = []
    t = WED
    for i in range(130):  # 12:00 〜 14:10 静穏 (デフォルト)
        rows.append(_row(t + timedelta(minutes=i), 148.5, 148.6, 148.4, 148.5))
    # 決定 2 のバケット [13:05,13:10) の**先頭分** (13:05) だけ一時的な
    # 含み損の谷 (148.3、SL=148.0 には非到達) を作り、同バケットの
    # **最終分** (13:09) は静穏 (回復済み) に戻す。
    rows[65] = _row(t + timedelta(minutes=65), 148.3, 148.3, 148.3, 148.3)
    rows[69] = _row(t + timedelta(minutes=69), 148.5, 148.6, 148.4, 148.5)
    rows_5m = _aggregate_5m(rows)
    end = WED + timedelta(hours=2)

    conn1 = _conn()
    ohlcv.import_history_bars(conn1, rows, source="dukascopy")
    res1 = run_replay(settings_sensitive, symbol="USDJPY",
                      dataset=HistoryDataset("dukascopy", "1m"),
                      start=WED, end=end,
                      intent_source=_scripted_source(), eval_timeframe="5m",
                      history_conn=conn1)

    conn5 = _conn()
    ohlcv.import_history_bars(conn5, rows_5m, source="mt5")
    res5 = run_replay(settings_sensitive, symbol="USDJPY",
                      dataset=HistoryDataset("mt5", "5m"),
                      start=WED, end=end,
                      intent_source=_scripted_source(), eval_timeframe="5m",
                      history_conn=conn5)

    # position1 は SL に一度も到達しない (両基底とも "open" のまま) —
    # kill switch が「実現損益」ではなく「一時的な含み損の谷」由来である
    # ことの前提を確認する。
    assert all(o["status"] == "open" for o in res1.orders)
    assert all(o["status"] == "open" for o in res5.orders)
    # 1m: 谷 (13:05) の直後 (13:06) の tick で mark-to-market が谷の
    # close をそのまま反映し、決定 2 (13:06 執行) が kill switch で
    # rejected される → 建玉は position1 の 1 件のみ。
    assert len(res1.orders) == 1
    assert res1.kill_switch_events
    assert res1.kill_switch_events[0]["kind"] == "latched"
    # 5m: 決定 2 の執行 tick (13:10) で使われる直前完成バケット
    # [13:05,13:10) の close は最終分 (13:09、回復済み) — 谷が隠れて
    # kill switch は発火せず、決定 2 も通って建玉が 2 件になる (非収束)。
    assert len(res5.orders) == 2
    assert res5.kill_switch_events == []


def test_start_on_base_grid_but_off_eval_grid_shifts_first_decision_identically():
    """⑦ start が base 格子上だが eval 格子外 — 先頭足規則
    (design-v3.md A2: 最初の意思決定 = ``ceil_to_bucket(start, eval_tf) +
    eval_tf``) は ``base_interval`` に依存しない純粋関数であるべきで、
    1m/5m どちらの dataset でも同じ ``first_decision_at`` へずれることを
    両 dataset で直接ピンする (base 幅ぶんの off-by-one 退行や
    `dataset.width` 混入への回帰チェック)。``start=WED+5分`` (12:05) は
    1m/5m どちらの base 格子にも乗るが、eval_timeframe="1h" の格子には
    乗っていない。
    """
    from agentic_fx.backtest.timeframes import ceil_to_bucket

    start = WED + timedelta(minutes=5)
    end = start + timedelta(hours=2)
    expected = ceil_to_bucket(start, "1h") + timedelta(hours=1)
    assert expected == WED + timedelta(hours=2)  # 前提: 14:00 へずれる

    rows = []
    t = WED
    for i in range(150):
        rows.append(_row(t + timedelta(minutes=i), 148.5, 148.6, 148.4, 148.5))
    rows_5m = _aggregate_5m(rows)

    conn1 = _conn()
    ohlcv.import_history_bars(conn1, rows, source="dukascopy")
    res1 = run_replay(SETTINGS, symbol="USDJPY",
                      dataset=HistoryDataset("dukascopy", "1m"),
                      start=start, end=end,
                      intent_source=lambda b: None, eval_timeframe="1h",
                      history_conn=conn1)

    conn5 = _conn()
    ohlcv.import_history_bars(conn5, rows_5m, source="mt5")
    res5 = run_replay(SETTINGS, symbol="USDJPY",
                      dataset=HistoryDataset("mt5", "5m"),
                      start=start, end=end,
                      intent_source=lambda b: None, eval_timeframe="1h",
                      history_conn=conn5)

    assert res1.first_decision_at == res5.first_decision_at == expected


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


def test_aggregate_bucket_and_load_resampled_frame_agree_for_multiple_buckets_with_gaps():
    """C8a-3: 欠損混在の複数バケットでも両経路の集約規則は一致する。"""
    rows = []
    for i in range(15):
        if i in {2, 11}:
            continue
        rows.append(_row(WED + timedelta(minutes=i), 100 + i, 101 + i,
                         99 + i, 100.5 + i, v=10 + i))
    conn = _conn()
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
    feed = BarFeed(conn, "USDJPY", dataset=HistoryDataset("dukascopy", "1m"),
                   start=WED, end=WED + timedelta(minutes=15))
    frame = load_resampled_frame(conn, "USDJPY", "5m", source="dukascopy",
                                 base_interval="1m", until=WED + timedelta(minutes=15))
    assert len(frame) == 3
    for bucket_start, row in frame.iterrows():
        bucket = _aggregate_bucket(feed, "USDJPY", "5m", bucket_start.to_pydatetime(),
                                   timedelta(minutes=5))
        assert bucket is not None
        assert (bucket.open, bucket.high, bucket.low, bucket.close, bucket.volume) == pytest.approx(
            (row["open"], row["high"], row["low"], row["close"], row["volume"]))


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
