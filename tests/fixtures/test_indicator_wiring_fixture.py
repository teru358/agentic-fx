"""[indicator-consumption-wiring] T6: 受入 fixture の自己整合 (A1 の前提)。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core import market_hours
from agentic_fx.plugin.loader import discover_one_with_reason
from tests.backtest.factories import _conn
from tests.fixtures import indicator_wiring as fx


def test_bar_generation_matches_the_spec_formula():
    rows = fx.synthetic_rows()
    assert len(rows) == fx.BARS_COUNT == 33_984
    symbol, interval, ts, o, h, l, c, v, _spread = rows[0]
    assert (symbol, interval) == (fx.PAIR, fx.BASE_INTERVAL)
    assert ts == fx.BARS_START.isoformat()
    assert o == 150.0 and c == 150.0 and v == 100
    assert h == pytest.approx(150.05) and l == pytest.approx(149.95)
    last_ts = datetime.fromisoformat(rows[-1][2])
    assert last_ts == datetime(2026, 2, 28, 23, 55, tzinfo=timezone.utc)
    # 欠損なし: 隣接行はちょうど 5 分差
    for a, b in zip(rows, rows[1:]):
        assert (datetime.fromisoformat(b[2])
                - datetime.fromisoformat(a[2])) == timedelta(minutes=5)


def test_open_is_previous_close():
    rows = fx.synthetic_rows()
    for a, b in zip(rows[:200], rows[1:201]):
        assert b[3] == pytest.approx(a[6])


@pytest.mark.slow   # opus r1 M11: `seed_history` が 33,984 行を投入する
def test_expected_eval_timestamps_follow_market_hours(tmp_path):
    conn = _conn(tmp_path)
    fx.seed_history(conn)
    stamps = fx.expected_eval_timestamps()
    assert stamps, "no evaluation timestamps derived"
    assert all(market_hours.is_market_open(ts) for ts in stamps)
    assert all(ts.minute == 0 and ts.second == 0 for ts in stamps)
    # opus r1 M6 是正: 旧案は `grid = (fx.NOW - fx.BARS_START) // 1h` と
    # **holdout 期間まで含んだ暦格子**と比べていたので、市場時間を完全に
    # 無視する実装でも常に真になる空振り assert だった (`stamps` は
    # `in_sample_until` = 2026-02-01 までしか無い)。同じ期間の暦格子と
    # 比べ、かつ「週末の timestamp が 1 件も含まれない」を直接 pin する。
    from agentic_fx.backtest.holdout import in_sample_until
    end = in_sample_until(fx.NOW, fx.HOLDOUT_MONTHS,
                          base_interval=fx.BASE_INTERVAL)
    grid = (end - (fx.BARS_START + timedelta(hours=1))) // timedelta(hours=1)
    assert 0 < len(stamps) < grid
    # 土曜 00:00Z〜日曜 21:00Z は市場休止 (サーバ UTC+3 固定、週末境界
    # 21:00 UTC) — 1 件も含まれないことを直接見る
    assert not [ts for ts in stamps
                if ts.weekday() == 5], "土曜の評価時点が混ざっている"


def test_fixture_plugins_discover_and_pin(tmp_path):
    root = tmp_path / "plugins"
    for name in ("sma", "rsi", "adx"):
        d = fx.write_indicator(root, name)
        meta, reason = discover_one_with_reason(d, name)
        assert reason is None, reason
        assert meta.kind == "indicator" and meta.outputs == (name,)
    conn = _conn(tmp_path)
    hashes = fx.deploy_approved(conn, root, ["sma", "rsi", "adx"], now=fx.NOW)
    d = fx.write_rsi_pullback(root, pins={"rsi": hashes["rsi"]})
    meta, reason = discover_one_with_reason(d, "rsi_pullback")
    assert reason is None, reason
    assert meta.max_bars == 200
    assert meta.indicators[0].pin == hashes["rsi"]


@pytest.mark.slow   # opus r1 M11: in_sample の全 bucket (≒2,100 点) で
                    # `load_resampled_frame` を回すため。A1 (Step 4-3) にも
                    # 既に `slow` が付いている
def test_oracle_produces_hold_during_warmup_then_values(tmp_path):
    conn = _conn(tmp_path)
    fx.seed_history(conn)
    stamps = fx.expected_eval_timestamps()
    decisions = fx.oracle_decisions(conn)
    assert set(decisions) == set(stamps)
    # 最初の 14 評価は RSI warmup で必ず hold
    for ts in stamps[:14]:
        assert decisions[ts] == {"action": "hold", "direction": None,
                                 "entry_type": None, "stop_loss": None,
                                 "take_profit": None}
    opens = sum(1 for d in decisions.values() if d["action"] == "open")
    # A1 の gate は `total_trades >= EVALUABLE_MIN_TRADES` (= 30、
    # `backtest/metrics.py:20`) を満たさないと `insufficient_trades` で
    # 早期 return する。fixture の生成式・閾値は設計書 §6 の逐語なので
    # 調整できない — ここで下限を先に pin して、A1 の偽陰性を T6a の段階で
    # 検出する (open 数 >= 成立 trade 数 なので必要条件)。
    assert opens >= 30, f"fixture produces only {opens} entries — A1 would fail "\
                        "with insufficient_trades (escalate to 指揮者)"
