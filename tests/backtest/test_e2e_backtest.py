"""通し E2E — import_history_bars → run_in_sample → in_sample_view (プラン 6 Task 12)。

Task 0〜11 の成果を実際の呼び出し経路で通す唯一のテスト:
- ohlcv.import_history_bars で合成 1 週間 (月曜〜金曜、約 7,200 本の 1m) を投入
- run_in_sample (内部で run_replay + compute_metrics +
  backtest_runs.save_harness_run(scope="in_sample")) を 1 回呼ぶ
- in_sample_view(conn) で保存された行を読み、遮断 1 (期間端点の非露出) を
  確認する

価格経路の設計 (上書き節 B — tests/backtest/test_runner.py の
``_seed_history``/``test_full_cycle_open_fill_tp`` をそのまま週 3 回分
流用):

``TRIGGER_HOURS`` の 3 つの水曜日相当パターン (月・火・水 12:00 UTC) が
それぞれ独立した「指値 → 約定 → TP」の 1 サイクルを起こす。各トリガー
時刻 T について、評価バケット [T, T+1h) は平坦データ (指値・TP どちらにも
届かない) のまま確定し、intent_source が T をキーに提案を返す。実行は
T+1h+1min (指値 148.20 発注、指値未到達の完成バー 2 本を経由)
→ T+1h+3min (完成バー low=148.10 で指値到達 = 約定)
→ T+1h+4min (完成バー high=149.10 で TP 到達 = 決済) という
``test_full_cycle_open_fill_tp`` と同一のタイムラインをたどる。3 トリガー
は 1 日おきに離れているため、ブロック同士が絶対に重ならない
(各ブロックの実働区間は 64 分で、次のブロックまで 23 時間以上ある)。

背景データ (0〜59 分オフセット = 評価対象の 1h バケット自体、および
トリガー時刻を挟まないその他の全時間帯) は指値 (148.20) にも TP (149.00)
にも届かない平坦値 (high=148.6/low=148.4) に統一してあるため、3 トリガー
以外の毎時バケットで intent_source が誤って提案を出しても (実際には
``bar.ts`` が ``TRIGGER_HOURS`` に含まれる場合のみ提案を返す設計なので
起きない) 価格的に約定し得ない — 二重の安全策になっている。

期間の構成 (上書き節 B): in-sample 期間は
``(source の最古バー, holdout_boundary(now, holdout_months=3))``。
``NOW`` (2026-10-25T00:00 UTC) を選ぶと ``holdout_boundary(NOW, 3)`` が
ちょうどデータ末尾 (2026-07-25T00:00 UTC, 土曜 00:00 = 金曜 23:59 の直後)
に一致し、合成データ全体 (7,200 tick) だけを再生する — 無駄な空 tick を
作らない (advisor 指摘の実測)。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.backtest.holdout import run_in_sample
from agentic_fx.backtest.metrics import METRIC_KEYS
from agentic_fx.store import ohlcv
from agentic_fx.store.backtest_runs import in_sample_view

from tests.backtest.factories import SETTINGS, _conn, _row_at

_UTC = timezone.utc

WEEK_START = datetime(2026, 7, 20, 0, 0, tzinfo=_UTC)   # 月曜 00:00 UTC
WEEK_END = datetime(2026, 7, 25, 0, 0, tzinfo=_UTC)      # 土曜 00:00 UTC (排他的上限)
NOW = datetime(2026, 10, 25, 0, 0, tzinfo=_UTC)          # holdout_boundary(NOW,3) == WEEK_END

TRIGGER_HOURS = [
    datetime(2026, 7, 20, 12, 0, tzinfo=_UTC),   # 月
    datetime(2026, 7, 21, 12, 0, tzinfo=_UTC),   # 火
    datetime(2026, 7, 22, 12, 0, tzinfo=_UTC),   # 水 (factories の H と同一時刻)
]

OPEN = {"action": "open", "pair": "USDJPY", "direction": "long",
        "entry_type": "limit", "horizon": "day",
        "limit_price": 148.20, "expires_in": "6h",
        "stop_loss": 147.80, "take_profit": 149.00,
        "reasoning": "e2e"}


def _seed_week(conn):
    """月曜 00:00 〜 土曜 00:00 (排他) の 1m を平坦値で投入し、3 トリガー
    時刻それぞれの直後 4 分 (offset 60/61/62/63 — test_runner.py の
    ``_seed_history`` と同一パターン) だけ「指値未到達 x2 → 指値到達 →
    TP 到達」に差し替える。他の全時間帯は指値・TP どちらにも届かない
    平坦値のまま (二重の安全策)。
    """
    rows_by_ts: dict[datetime, tuple] = {}
    t = WEEK_START
    while t < WEEK_END:
        rows_by_ts[t] = _row_at(t, o=148.5, h=148.6, l=148.4, c=148.5)
        t += timedelta(minutes=1)

    for trig in TRIGGER_HOURS:
        rows_by_ts[trig + timedelta(minutes=60)] = _row_at(
            trig + timedelta(minutes=60), o=148.4, h=148.45, l=148.30,
            c=148.35)
        rows_by_ts[trig + timedelta(minutes=61)] = _row_at(
            trig + timedelta(minutes=61), o=148.4, h=148.45, l=148.30,
            c=148.35)
        rows_by_ts[trig + timedelta(minutes=62)] = _row_at(
            trig + timedelta(minutes=62), o=148.3, h=148.35, l=148.10,
            c=148.15)         # 指値 148.20 到達
        rows_by_ts[trig + timedelta(minutes=63)] = _row_at(
            trig + timedelta(minutes=63), o=148.9, h=149.10, l=148.85,
            c=149.05)         # TP 149.00 到達

    rows = [rows_by_ts[ts] for ts in sorted(rows_by_ts)]
    assert len(rows) == 7_200          # 5 日 x 1,440 分 (上書き節 B — 約 7,000 本)
    ohlcv.import_history_bars(conn, rows, source="dukascopy")


def _make_source():
    fired: list[datetime] = []

    def source(bar):
        if bar.ts in TRIGGER_HOURS:
            fired.append(bar.ts)
            return dict(OPEN)
        return None

    return source, fired


def test_e2e_import_run_in_sample_in_sample_view(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agentic_fx.backtest.holdout.core_commit", lambda: "e2etest")
    conn = _conn(tmp_path)
    _seed_week(conn)
    source, fired = _make_source()

    metrics = run_in_sample(
        SETTINGS, history_conn=conn, symbol="USDJPY", source="dukascopy",
        intent_source=source, eval_timeframe="1h", plugin_ref="e2e-plugin",
        content_hash="e2e-content-hash", kind="e2e", now=NOW)

    # 3 トリガーとも評価は起きた (fired) — 実行はうち発注可能な分のみ。
    assert fired == TRIGGER_HOURS

    # ① closed 3 件 (compute_metrics は status=="closed" のみ数える)。
    assert metrics["trades"] == 3
    # ② metrics 妥当 — 価格経路から手計算できる値のみを固定する。
    #   3 件とも同一の「指値 148.20 → TP 149.00 (ロング)」で負けが無いため:
    #   win_rate=1.0 (3/3 勝ち)、pf=None (gross_loss==0 の compute_metrics
    #   分岐)、evaluable=False (3 < EVALUABLE_MIN_TRADES=30)、
    #   max_drawdown=0.0 (実現損益ベースの equity は勝ちでのみ変化するため
    #   単調非減少)、total_pnl>0 (3 件とも利益方向)、
    #   fallback_spread_used=False (全行 spread=0.01 で投入)。
    assert metrics["win_rate"] == 1.0
    assert metrics["pf"] is None
    assert metrics["evaluable"] is False
    assert metrics["max_drawdown"] == 0.0
    assert metrics["total_pnl"] > 0
    assert metrics["fallback_spread_used"] is False

    # ③ in_sample_view 経由で読める — 期間端点は返却列に無い (holdout 遮断
    #    1)。issued_by/scope は _VIEW_COLUMNS に実在する列であり (Task 8
    #    fix round 1 で確定した契約 — tests/store/test_backtest_runs.py::
    #    test_backtest_runs_issuer_and_view が同じ形で issued_by の存在を
    #    ピンしている)、值を検証する対象として扱う (期間・端点の非露出とは
    #    別の話 — brief 末尾の上書き節の記述はここが誤記と判断した。詳細は
    #    report.md「brief どおりにできなかった点」)。
    rows = in_sample_view(conn, pair="USDJPY")
    assert len(rows) == 1
    row = rows[0]
    assert {"period_start", "period_end"}.isdisjoint(row.keys())
    assert row["scope"] == "in_sample"
    assert row["issued_by"] == "harness"
    # ④ metrics_json 展開キーが METRIC_KEYS と完全一致 (subset だと
    #    白リスト濾過が無検証になる — METRIC_KEYS 自体が compute_metrics の
    #    厳密な返却キー集合なので等価性で固定する)。
    assert set(row["metrics"]) == METRIC_KEYS
    assert row["metrics"]["trades"] == 3
