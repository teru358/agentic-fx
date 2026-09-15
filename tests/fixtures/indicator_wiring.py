"""[indicator-consumption-wiring] 受入 fixture (設計書 §6 逐語)。

**実 DB・実 `plugins/` には一切触れない** — 呼び出し元が渡す `tmp_path`
配下の sqlite と plugins root にだけ書く。

bar 生成式 (決定論、設計書 §6):
    ts_k    = 2025-11-03T00:00:00Z (月曜) + k*5min   (k = 0 .. 33983)
    close_k = 150.0 + 2.0*sin(2*pi*k/288) + 0.5*sin(2*pi*k/2016)
    open_k  = close_{k-1}   (open_0 = 150.0)
    high_k  = max(open_k, close_k) + 0.05
    low_k   = min(open_k, close_k) - 0.05
    volume_k = 100
週末も生成する — runner が `is_market_open` で評価を止めるだけ (codex r7 I1)。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

NOW = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
BARS_START = datetime(2025, 11, 3, 0, 0, tzinfo=timezone.utc)   # 月曜
BARS_COUNT = 33_984
HOLDOUT_MONTHS = 1
PAIR = "USDJPY"
SOURCE = "dukascopy"
BASE_INTERVAL = "5m"
EVAL_TIMEFRAME = "1h"
PIP_SIZE = 0.01
RSI_PERIOD = 14
OVERSOLD = 30.0
OVERBOUGHT = 70.0
STOP_LOSS_PIPS = 30.0
TAKE_PROFIT_PIPS = 60.0
MAX_BARS = 200

_TEST_PY = "def test_placeholder():\n    pass\n"

_SMA_PY = """
from __future__ import annotations

import pandas as pd


def compute(df, params):
    period = int(params.get("period", 20))
    return {"sma": df["close"].astype(float).rolling(window=period).mean()}
"""

_RSI_PY = """
from __future__ import annotations

import pandas as pd


def compute(df, params):
    period = int(params.get("period", 14))
    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    out = out.where(avg_loss != 0.0, 100.0)
    out = out.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
    out = out.where(~(avg_gain.isna() | avg_loss.isna()))
    return {"rsi": out}
"""

_ADX_PY = """
from __future__ import annotations

import pandas as pd


def _wilder(series, period):
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def compute(df, params):
    period = int(params.get("period", 14))
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0.0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0.0), 0.0)
    atr = _wilder(tr, period)
    plus_di = 100.0 * _wilder(plus_dm, period) / atr
    minus_di = 100.0 * _wilder(minus_dm, period) / atr
    denom = (plus_di + minus_di).replace(0.0, float("nan"))
    dx = 100.0 * (plus_di - minus_di).abs() / denom
    return {"adx": _wilder(dx.fillna(0.0), period)}
"""

_PULLBACK_PY = (Path(__file__).resolve().parents[2] / "docs" / "examples"
                / "plugins" / "rsi_pullback" / "plugin.py")

_INDICATOR_SOURCES = {"sma": _SMA_PY, "rsi": _RSI_PY, "adx": _ADX_PY}
_INDICATOR_PARAMS = {"sma": {"period": 20}, "rsi": {"period": RSI_PERIOD},
                     "adx": {"period": 14}}


def _closes() -> list[float]:
    return [150.0 + 2.0 * math.sin(2 * math.pi * k / 288)
            + 0.5 * math.sin(2 * math.pi * k / 2016) for k in range(BARS_COUNT)]


def synthetic_rows() -> list[tuple]:
    closes = _closes()
    rows = []
    prev_close = 150.0
    for k, close in enumerate(closes):
        ts = BARS_START + timedelta(minutes=5 * k)
        open_ = 150.0 if k == 0 else prev_close
        rows.append((PAIR, BASE_INTERVAL, ts.isoformat(), open_,
                     max(open_, close) + 0.05, min(open_, close) - 0.05,
                     close, 100, 0.01))
        prev_close = close
    return rows


def seed_history(conn) -> None:
    from agentic_fx.store import ohlcv
    ohlcv.import_history_bars(conn, synthetic_rows(), source=SOURCE)


def write_indicator(base: Path, name: str) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.py").write_text(_INDICATOR_SOURCES[name])
    (d / "config.yaml").write_text(yaml.safe_dump(
        {"kind": "indicator", "outputs": [name],
         "params": _INDICATOR_PARAMS[name], "max_bars": MAX_BARS},
        sort_keys=False))
    (d / "test_plugin.py").write_text(_TEST_PY)
    return d


def write_rsi_pullback(base: Path, *, pins: dict[str, str] | None = None) -> Path:
    d = base / "rsi_pullback"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.py").write_text(_PULLBACK_PY.read_text(encoding="utf-8"))
    ref = {"plugin": "rsi", "params": {"period": RSI_PERIOD}}
    if pins and "rsi" in pins:
        ref["pin"] = pins["rsi"]
    (d / "config.yaml").write_text(yaml.safe_dump(
        {"kind": "strategy", "timeframe": EVAL_TIMEFRAME, "pairs": [PAIR],
         "exit_mode": "levels", "max_bars": MAX_BARS,
         "indicators": {"rsi": ref},
         "params": {"oversold": OVERSOLD, "overbought": OVERBOUGHT,
                    "stop_loss_pips": STOP_LOSS_PIPS,
                    "take_profit_pips": TAKE_PROFIT_PIPS,
                    "pip_size": PIP_SIZE}},
        sort_keys=False))
    (d / "test_plugin.py").write_text(_TEST_PY)
    return d


def deploy_approved(conn, plugins_root: Path, names, *, now) -> dict[str, str]:
    """`plugins_root/<name>` を `.versions/<name>/<artifact_hash>` への
    正規形 symlink にして approval 行を approved で作る (配備の最小形)。"""
    from agentic_fx.plugin.loader import artifact_hash_bytes, content_hash
    from agentic_fx.store import approvals
    hashes: dict[str, str] = {}
    for name in names:
        src = plugins_root / name
        chash = content_hash(src)
        ahash = artifact_hash_bytes((src / "plugin.py").read_bytes(),
                                    (src / "config.yaml").read_bytes(),
                                    (src / "test_plugin.py").read_bytes())
        version_dir = plugins_root / ".versions" / name / ahash
        version_dir.mkdir(parents=True, exist_ok=True)
        for rel in ("plugin.py", "config.yaml", "test_plugin.py"):
            (version_dir / rel).write_bytes((src / rel).read_bytes())
        for rel in ("plugin.py", "config.yaml", "test_plugin.py"):
            (src / rel).unlink()
        src.rmdir()
        src.symlink_to(Path(".versions") / name / ahash)
        kind = yaml.safe_load(
            (version_dir / "config.yaml").read_text(encoding="utf-8"))["kind"]
        aid = approvals.create(conn, "plugin",
                               {"name": name, "kind": kind,
                                "content_hash": chash}, now)
        approvals.apply_decision(conn, aid, status="approved",
                                 decided_by="fixture", now=now)
        hashes[name] = chash
    return hashes


_ORACLE_CACHE: dict[int, dict] = {}


def expected_eval_timestamps() -> list[datetime]:
    """`backtest/runner.run_replay` が `intent_source(closed_bar)` を呼ぶ
    時点と同じ集合を、ハーネスのコード (`market_hours.is_market_open`) から
    導出する。plugin コードは一切 import しない。

    **runner の実挙動との対応 (opus r1 I9 で実測確認済み)**: 着手時に
    `rg -n 'bucket_start|is_market_open|first_decision_at' src/agentic_fx/backtest/runner.py`
    で再取得して照合すること。v1 執筆時点の実測では
    `bucket_start = now - tf` → `closed_bar = _aggregate_bucket(..., bucket_start, tf)`
    → `if closed_bar is not None and market_hours.is_market_open(now)` で
    `intent_source(closed_bar)` を呼ぶ。この `now` は **bucket_end** なので
    本関数の `is_market_open(ts)` (ts = bucket_end) と**同じ述語**。開始点も
    `first_decision_at = ceil_to_bucket(start, eval_timeframe) + tf`
    = `BARS_START + 1h` で一致する。**週末境界のズレは発生しない**。

    `conn` は受けない (opus r1 I9 #3: 旧案は受けていたが本文で使っていな
    かった = 死に引数)。"""
    from agentic_fx.backtest.holdout import in_sample_until
    from agentic_fx.core import market_hours
    end = in_sample_until(NOW, HOLDOUT_MONTHS, base_interval=BASE_INTERVAL)
    step = timedelta(hours=1)
    first = BARS_START + step          # 最初の完成 1h バケットの終端
    stamps = []
    ts = first
    while ts < end:
        if market_hours.is_market_open(ts):
            stamps.append(ts)
        ts += step
    return stamps


def oracle_decisions(conn) -> dict[datetime, dict]:
    """**plugin コードを import しない独立参照実装** (設計書 §6)。
    各評価時点で、ハーネスが渡すのと同じ df (`load_resampled_frame(...,
    until=bucket_end, max_bars=200)`) に対して Wilder RSI を計算し、
    `rsi_pullback` の判定式を pandas で転写する。

    `ewm(alpha=1/period, adjust=False)` は窓の先頭から経路依存なので、
    **必ず同じ 200 本の tail に対して**計算すること (全期間の系列から
    切り出すと期待値が実行結果と乖離する)。"""
    import pandas as pd

    from agentic_fx.backtest.timeframes import load_resampled_frame

    # opus r1 M11: 同一 conn に対する再計算を避ける (T6a の自己整合テストと
    # T4a の A1 で計 3 回走る)。`functools.lru_cache` は conn を hashable と
    # して保持し続けるので使わず、`id(conn)` キーの module-level dict で
    # 明示的にメモ化する (テストは関数スコープの conn なので衝突しない)。
    cached = _ORACLE_CACHE.get(id(conn))
    if cached is not None:
        return cached

    out: dict[datetime, dict] = {}
    for bucket_end in expected_eval_timestamps():
        df = load_resampled_frame(conn, PAIR, EVAL_TIMEFRAME, source=SOURCE,
                                  base_interval=BASE_INTERVAL,
                                  until=bucket_end, max_bars=MAX_BARS)
        # opus r1 I9 #1 是正: 旧案はここに `if df.empty: continue` があり、
        # `expected_eval_timestamps` (df を見ない) と集合が食い違う可能性が
        # あった (`test_oracle_produces_hold_during_warmup_then_values` は
        # `set(decisions) == set(stamps)` を要求する)。fixture は欠損なしの
        # 33,984 本なのでここへは到達しない — **到達したら fixture が
        # 壊れているので落ちる方が正しい**。continue せず素通しする
        # (`close.diff()` が空 Series になり `len(rsi) < 2` で hold に落ちる
        # のではなく、`df["close"]` の KeyError / IndexError で落ちること
        # 自体が検出になる)。
        assert not df.empty, (
            f"fixture has a gap at {bucket_end.isoformat()} — "
            "expected_eval_timestamps と oracle の集合が食い違う")
        close = df["close"].astype(float)
        delta = close.diff()
        gain = delta.clip(lower=0.0)
        loss = -delta.clip(upper=0.0)
        avg_gain = gain.ewm(alpha=1.0 / RSI_PERIOD, adjust=False,
                            min_periods=RSI_PERIOD).mean()
        avg_loss = loss.ewm(alpha=1.0 / RSI_PERIOD, adjust=False,
                            min_periods=RSI_PERIOD).mean()
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi = rsi.where(avg_loss != 0.0, 100.0)
        rsi = rsi.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
        rsi = rsi.where(~(avg_gain.isna() | avg_loss.isna()))
        hold = {"action": "hold", "direction": None, "entry_type": None,
                "stop_loss": None, "take_profit": None}
        if len(rsi) < 2 or pd.isna(rsi.iloc[-2]) or pd.isna(rsi.iloc[-1]):
            out[bucket_end] = hold
            continue
        prev, curr = float(rsi.iloc[-2]), float(rsi.iloc[-1])
        last_close = float(close.iloc[-1])
        stop = STOP_LOSS_PIPS * PIP_SIZE
        target = TAKE_PROFIT_PIPS * PIP_SIZE
        if prev <= OVERSOLD and curr > OVERSOLD:
            out[bucket_end] = {"action": "open", "direction": "long",
                               "entry_type": "market",
                               "stop_loss": last_close - stop,
                               "take_profit": last_close + target}
        elif prev >= OVERBOUGHT and curr < OVERBOUGHT:
            out[bucket_end] = {"action": "open", "direction": "short",
                               "entry_type": "market",
                               "stop_loss": last_close + stop,
                               "take_profit": last_close - target}
        else:
            out[bucket_end] = hold
    _ORACLE_CACHE[id(conn)] = out
    return out


def assert_decisions_match(recorded, conn) -> None:
    """`StrategyAdapter(decision_sink=...)` が記録した
    `[(bucket_end, decision_dict), ...]` を oracle と逐次比較する。
    比較対象 (codex r8 I3): `(action, direction)`、open のときは
    `entry_type == "market"` と `stop_loss` / `take_profit` を ±1e-9。
    `rationale` は比較しない。

    **adapter 側の逐語契約 (opus r1 I9 #2)**: `StrategyAdapter.__call__` は
    `load_resampled_frame(...)` の結果が `df.empty` のとき **`decision_sink` を
    呼ばずに `None` を返す** (Step 3-1c の実装を参照)。一方この関数は
    `[ts for ts, _ in recorded] == sorted(expected)` の**完全一致**を要求する。
    欠損のない本 fixture では `df.empty` に到達しないので両者は一致するが、
    **fixture を欠損ありに変えた瞬間に片側だけが落ちる非対称**なので、
    fixture の bar 生成式を変更するときはこの 2 つを同時に見直すこと。"""
    expected = oracle_decisions(conn)
    assert [ts for ts, _ in recorded] == sorted(expected), \
        "evaluated timestamps do not match the market-hours grid"
    for ts, decision in recorded:
        want = expected[ts]
        assert (decision["action"], decision["direction"]) == \
            (want["action"], want["direction"]), f"mismatch at {ts.isoformat()}"
        if want["action"] == "open":
            assert decision["entry_type"] == "market"
            assert abs(decision["stop_loss"] - want["stop_loss"]) < 1e-9
            assert abs(decision["take_profit"] - want["take_profit"]) < 1e-9
