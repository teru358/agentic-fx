"""速度実測ゲート (プラン 6 Task 12 — 上書き節 C)。

``_run_bench(tmp_path, span)`` は共通 helper: 合成データ (決定的 sine
生成、平日分のみ — dukascopy 実データと同じく週末は 1m が存在しない) を
tmp_path 上の sqlite DB に投入し、``run_replay`` の実行時間だけを
``time.monotonic()`` で計測して返す (生成・``import_history_bars`` は計測対象外)。

- ``test_bench_one_year`` (``@pytest.mark.bench`` — 既定 skip、``-m bench``
  で実行): 合成 1 年分 (約 37 万本の平日 1m) を投入し、経過秒と tick 単価を
  print する。assert は「完走」のみ (brief 逐語) — 時間の合否はコントローラ
  が実測してレジャーに記録する。**1 年フル実測 (~28 分見込み) は実装者は
  実行しない** (上書き節 C の分担裁定)。
- ``test_bench_smoke_two_hours`` (mark なし・通常スイート・1 秒未満):
  同じ helper を 2 時間相当で呼び、bench コードパス自体の緑を保証する。
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.backtest.runner import run_replay
from agentic_fx.store import ohlcv

from tests.backtest.factories import SETTINGS, _conn, _row_at

_START = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)   # 月曜


def _weekday_minutes(start: datetime, span: timedelta):
    """``[start, start+span)`` の平日 (月〜金) 分のみを 1 分刻みで列挙する
    (dukascopy 実データと同じく週末は 1m 系列が存在しない前提の合成)。"""
    t = start
    end = start + span
    while t < end:
        if t.weekday() < 5:
            yield t
        t += timedelta(minutes=1)


def _run_bench(tmp_path, span: timedelta) -> tuple[float, int]:
    """合成データを投入し、``run_replay`` の実時間 (秒) と tick 数を返す。

    計測は ``time.monotonic()`` のみ (実時刻 ``datetime.now`` は使わない
    — 上書き節 C の裁定どおり計測用途は「実時刻禁止」の対象外)。合成
    データの生成・``import_history_bars`` は計測開始前に完了させ、計測区間には
    ``run_replay`` の呼び出しのみを含める。
    """
    rows = []
    for i, ts in enumerate(_weekday_minutes(_START, span)):
        base = 148.50 + 0.01 * math.sin(i / 97.0)   # 決定的 (乱数不使用)
        rows.append(_row_at(ts, o=base, h=base + 0.05, l=base - 0.05,
                            c=base, spread=0.01))
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
    end = _START + span

    t0 = time.monotonic()
    run_replay(SETTINGS, symbol="USDJPY", source="dukascopy",
              start=_START, end=end, intent_source=lambda b: None,
              eval_timeframe="1h", history_conn=conn)
    elapsed = time.monotonic() - t0

    ticks = span // timedelta(minutes=1)
    return elapsed, ticks


@pytest.mark.bench
def test_bench_one_year(tmp_path):
    """1 年フル実測 — 実装者は実行しない (コントローラがレジャー・報告書に
    記録する — 上書き節 C)。assert は完走のみ。"""
    elapsed, ticks = _run_bench(tmp_path, timedelta(days=365))
    per_tick_ms = (elapsed / ticks) * 1000 if ticks else 0.0
    print(f"\n[bench] 1-year replay: {elapsed:.2f}s total for {ticks} "
          f"ticks ({per_tick_ms:.4f} ms/tick)")
    assert elapsed >= 0.0   # 完走のみ担保 (上書き節 C — 合否はコントローラ裁定)


def test_bench_smoke_two_hours(tmp_path):
    """mark なし・通常スイート — bench コードパス自体の緑を保証する
    (2 時間相当 = 120 tick、1 秒未満)。"""
    elapsed, ticks = _run_bench(tmp_path, timedelta(hours=2))
    assert ticks == 120
    assert elapsed >= 0.0
