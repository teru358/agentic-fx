"""rsi indicator plugin の自己テスト ([indicator-initial-set] 設計書 §6)。

担保する受入条件: **I2** (outputs 完全一致 / index 一致 / Inf 不在)、
**I3** (独立参照実装との全行一致)、**I4** (固定 fixture における全行の
接頭辞一致)、**I9** (入力不変・反復決定性)、warmup 境界の両側 pin、
params の型検証。

このファイルは bless の pytest ゲート (subprocess + Landlock) で毎回走る。
`check_source(..., extra_allowed={"pytest", "plugin"})` を通る範囲で書くこと
(`to_frame` / `df.open` / `getattr` は使えない)。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from plugin import compute

OUTPUTS = ('rsi',)
WARMUP = {'rsi': 14}
EPS = 1e-9


def _mkdf(n: int = 160, *, seed: int = 0, base: float = 150.0) -> pd.DataFrame:
    """設計書 §6 I4 の固定 fixture。160 行は `ichimoku` の `senkou_b` warmup
    51 + 余裕。**極値を中間と末尾側の両方に置く** — 片側だけだと「全体の
    min/max で正規化する」型の未来参照を fixture 次第で見逃す (§6.2)。
    """
    rng = np.random.default_rng(seed)
    sigma = base * 0.002
    close = base + np.cumsum(rng.normal(0.0, sigma, n))
    if n >= 20:
        close[n // 2] += base * 0.05
        close[n - 7] -= base * 0.06
    high = close + np.abs(rng.normal(0.0, sigma / 2.0, n))
    low = close - np.abs(rng.normal(0.0, sigma / 2.0, n))
    if n >= 2:
        # **bar 1 に決定論的な上げを置く** — ここが平坦だと `+DM[0]` を NaN に
        # するか 0.0 にするかが Wilder の seed に効かず、行 0 の扱いを潰す変異
        # (M-adx-4) が fixture 次第で生き残る (変異スイープの実測)。
        high[1] = high[0] + base * 0.01
    # **open は close と別の系列にする** (前バーの終値)。`open == close` の
    # fixture だと「close の代わりに open を読む」型の変異を検出できない
    # (M-sma-4 の実測)。
    open_ = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
          "volume": np.ones(n)}, index=index)


def _same(got, want, *, rel: float = 1e-9, tol: float = 1e-9) -> bool:
    """NaN 同士は一致。数値は相対/絶対のどちらかを満たせば一致。"""
    got_nan = got is None or bool(pd.isna(got))
    want_nan = want is None or bool(pd.isna(want))
    if got_nan or want_nan:
        return got_nan and want_nan
    got = float(got)
    want = float(want)
    return abs(got - want) <= max(tol, rel * abs(want))


# --- 独立参照実装 (plugin.py とは別の書き方。plugin.py から import しない) ---

def _isnan(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))

def _ref_recursive(values: list, alpha: float, period: int) -> list:
    """再帰平滑。**`min_periods` は位置ではなく「非 NaN 観測数」で数える**
    (設計書 §3.3) — 位置で `i < period - 1` と書くと `atr` / `adx` の warmup
    が 1 本早く明け、I3 が落ちる。seed は最初の非 NaN 値 (`y_0 = x_0`)。
    """
    out = []
    prev = None
    seen = 0
    for value in values:
        if _isnan(value):
            out.append(float("nan"))
            continue
        prev = value if prev is None else alpha * value + (1.0 - alpha) * prev
        seen += 1
        out.append(prev if seen >= period else float("nan"))
    return out


def _reference(df, params: dict) -> dict:
    """plugin.py とは別の書き方だが、**ε 規則は同じ閾値・同じ基準
    (その行の |close|)・同じ判定順 (1)->(2)->(3) で書き写す** (設計書 §3.3)。
    ここを素朴な式のままにすると閾値付近の行で I3 が落ちる。
    """
    period = int(params.get("period", 14))
    close = df["close"].tolist()
    gain = [float("nan")]
    loss = [float("nan")]
    for i in range(1, len(close)):
        delta = close[i] - close[i - 1]
        gain.append(delta if delta > 0.0 else 0.0)
        loss.append(-delta if delta < 0.0 else 0.0)
    alpha = 1.0 / period
    avg_gain = _ref_recursive(gain, alpha, period)
    avg_loss = _ref_recursive(loss, alpha, period)
    out = []
    for i in range(len(close)):
        if _isnan(avg_gain[i]) or _isnan(avg_loss[i]):
            out.append(float("nan"))
            continue
        threshold = EPS * abs(close[i])
        if avg_gain[i] + avg_loss[i] <= threshold:
            out.append(50.0)
        elif avg_loss[i] <= threshold:
            out.append(100.0)
        else:
            out.append(100.0 - 100.0 / (1.0 + avg_gain[i] / avg_loss[i]))
    return {"rsi": out}


# --- I2: 宣言キー集合 / index 一致 / Inf 不在 -------------------------------

def test_outputs_match_declared_keys_and_index():
    df = _mkdf()
    out = compute(df, {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        assert isinstance(out[key], pd.Series), key
        assert out[key].index.equals(df.index), key
        assert not bool(np.isinf(out[key].to_numpy(dtype="float64")).any()), key


# --- I3: 独立参照実装との全行一致 -------------------------------------------

def test_matches_reference_implementation_on_every_row():
    df = _mkdf()
    got = compute(df, {})
    want = _reference(df, {})
    for key in OUTPUTS:
        for i in range(len(df)):
            assert _same(got[key].iloc[i], want[key][i]), (key, i)


# --- I4: 固定 fixture における接頭辞一致 (全 160 行) ------------------------

def test_prefix_consistency_on_every_row():
    """`compute(df[:t+1])[key].iloc[-1] == compute(df)[key].iloc[t]` を
    **全行**で。サンプル点だけだと「サンプルされない行だけ未来を読む」実装
    (実測: 行 37 のみ書き換え) を素通りする (設計書 §6.2)。
    """
    df = _mkdf()
    full = compute(df, {})
    for t in range(len(df)):
        sub = compute(df.iloc[:t + 1], {})
        for key in OUTPUTS:
            assert _same(sub[key].iloc[-1], full[key].iloc[t]), (key, t)


# --- I9: 入力不変 / 反復決定性 ----------------------------------------------

def test_input_frame_is_not_mutated():
    """`check_source` は添字代入 `df["x"] = ...` を拒否しない (設計書 §4)。
    入力を壊さない規律の観測点はこのテストだけ。"""
    df = _mkdf()
    before = df.copy(deep=True)
    compute(df, {})
    assert df.equals(before)
    assert list(df.columns) == list(before.columns)
    assert df.index.equals(before.index)
    assert bool((df.dtypes == before.dtypes).all())


def test_repeated_calls_are_deterministic():
    """(1) fresh な df で 2 回 (2) **同じ df オブジェクトで 2 回** —
    (2) が module レベルの状態持ち越しを捕まえる。"""
    first = compute(_mkdf(), {})
    second = compute(_mkdf(), {})
    df = _mkdf()
    third = compute(df, {})
    fourth = compute(df, {})
    for key in OUTPUTS:
        for i in range(len(first[key])):
            assert _same(first[key].iloc[i], second[key].iloc[i]), ("fresh", key, i)
            assert _same(third[key].iloc[i], fourth[key].iloc[i]), ("same", key, i)


# --- warmup 境界 (両側を pin する) ------------------------------------------

def test_warmup_boundary_is_pinned_on_both_sides():
    """「N 行目まで NaN」だけでなく「N+1 行目に値が入る」も見る — 片側だけ
    だと warmup が 1 本早く/遅く明ける変異を検出できない。"""
    out = compute(_mkdf(), {})
    for key, first_valid in WARMUP.items():
        series = out[key]
        if first_valid > 0:
            assert bool(series.iloc[:first_valid].isna().all()), key
        assert not bool(pd.isna(series.iloc[first_valid])), key


def test_short_frame_returns_all_declared_keys_as_all_nan():
    """行が足りなくても「返さない」はできない — 宣言キーは必ず全部返す。"""
    out = compute(_mkdf(n=3), {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        if WARMUP[key] >= 3:
            assert bool(out[key].isna().all()), key


# --- params の型検証 (変換ではなく型の確認) ---------------------------------

@pytest.mark.parametrize("params", [{'period': 14.0}, {'period': '14'}, {'period': True}, {'period': 0}])
def test_invalid_params_raise_value_error(params):
    """**文言まで pin する。** `match` が無いと、検査を外す変異を入れても
    pandas 自身が投げる `ValueError` (`span must satisfy: span >= 1` 等) で
    テストが緑のまま通ってしまう (変異スイープの実測: M-sma-5 / M-ema-4)。
    本 plugin の文言はすべて `params.<キー名> ...` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


def test_valid_param_override_changes_the_result():
    df = _mkdf()
    base = compute(df, {})
    other = compute(df, {'period': 5})
    key = OUTPUTS[0]
    assert not _same(base[key].iloc[-1], other[key].iloc[-1])


def _decayed_flat_df(n_flat: int = 260, price: float = 150.0,
                     move: float = 1.0) -> pd.DataFrame:
    """1 本だけ上げたあと `n_flat` 本の完全横ばい。

    平滑量は指数的に減衰して**実質ゼロ**になるが、**厳密なゼロにはならない**
    — 厳密 `== 0` 判定では捕まらず、比を取る指標が「値動きが無いのに強い
    トレンド」という値を返す構成 (設計書 §3.2 (i-b) の反例)。相対 ε 規則が
    効いているかはこの fixture でしか観測できない (変異スイープの実測:
    完全横ばいだけの fixture では EPS=0 への変異が生き残る)。
    """
    closes = [price] * 5 + [price + move] * (n_flat + 1)
    values = np.array(closes, dtype="float64")
    index = pd.date_range("2026-01-01", periods=len(values), freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": values, "high": values, "low": values, "close": values,
         "volume": np.ones(len(values))}, index=index)


def test_eps_rule_fires_after_the_averages_decay():
    """`avg_loss` が厳密 0 でも `avg_gain` が実質ゼロまで減衰した区間では
    **50 (中立)** を返す。EPS を 0 にすると (1) の分岐に到達せず 100 を返す
    ので、この fixture が ε 規則の唯一の観測点になる。"""
    rsi = compute(_decayed_flat_df(), {})["rsi"]
    assert float(rsi.iloc[-1]) == 50.0


# --- 退化規則 (ε) と既存 `rsi_indicator` との差異 ---------------------------

def _flat_df(n: int = 60, price: float = 150.0) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    values = np.full(n, price)
    return pd.DataFrame(
        {"open": values, "high": values, "low": values, "close": values,
         "volume": np.ones(n)}, index=index)


def test_flat_market_is_neutral_not_one_hundred():
    """完全横ばい = 値動きなし -> **50** (中立)。配備済 `rsi_indicator` は
    同じ場面で 100 を返す (厳密 `== 0` 判定で「下げが無い」に倒すため) —
    両者を同じ strategy で混ぜて使わないこと (設計書 §0 R5a)。"""
    rsi = compute(_flat_df(), {})["rsi"]
    assert float(rsi.iloc[-1]) == 50.0


def test_monotonic_rise_is_one_hundred():
    """下げが一度も無く上げが有意 -> 100 (判定順 (2))。"""
    closes = [100.0 + i for i in range(40)]
    index = pd.date_range("2026-01-01", periods=len(closes), freq="1h", tz="UTC")
    df = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes,
         "volume": [1.0] * len(closes)}, index=index)
    assert float(compute(df, {})["rsi"].iloc[-1]) == 100.0


def test_eps_rule_does_not_fire_on_ordinary_data():
    """通常データで ε 規則が誤発火しないこと (設計書 §3.2 (i-b) の余裕 7e+03 倍)。"""
    rsi = compute(_mkdf(), {})["rsi"]
    assert 0.0 < float(rsi.iloc[-1]) < 100.0
    assert float(rsi.iloc[-1]) != 50.0
