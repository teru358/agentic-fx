"""[indicator-consumption-wiring] T2: indicator 戻り値の共通 validator (V1)。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from agentic_fx.core.plugin_contract import (
    IndicatorResultError, validate_indicator_result, validate_ohlcv_passthrough,
)


def _idx(n: int = 5) -> pd.DatetimeIndex:
    return pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")


def test_scalar_and_series_accepted():
    idx = _idx()
    out = validate_indicator_result(
        {"a": 1.5, "b": pd.Series([1.0] * 5, index=idx)},
        df_index=idx, outputs=("a", "b"))
    assert out["a"] == 1.5
    assert list(out["b"]) == [1.0] * 5


def test_nan_is_allowed_in_scalar_and_series():
    idx = _idx()
    out = validate_indicator_result(
        {"a": float("nan"), "b": pd.Series([float("nan")] * 5, index=idx)},
        df_index=idx, outputs=("a", "b"))
    assert np.isnan(out["a"])
    assert bool(pd.isna(out["b"]).all())


def test_list_and_ndarray_series_accepted_by_length():
    idx = _idx()
    out = validate_indicator_result(
        {"a": [1.0, 2.0, 3.0, 4.0, 5.0],
         "b": np.array([1.0, 2.0, 3.0, 4.0, 5.0])},
        df_index=idx, outputs=("a", "b"))
    assert len(out["a"]) == 5 and len(out["b"]) == 5


@pytest.mark.parametrize("value", [
    True,                               # bool は数値として拒否
    float("inf"),
    float("-inf"),
])
def test_scalar_rejects(value):
    idx = _idx()
    with pytest.raises(IndicatorResultError):
        validate_indicator_result({"a": value}, df_index=idx, outputs=("a",))


def test_numpy_scalars_are_accepted_at_the_scalar_position():
    """1 周目 ローカル LLM (c03 qwen): `_check_number` の numpy allowlist。

    既存テストはスカラー位置に Python の `int` / `float` しか置かない
    (`np.array(...)` は ndarray 分岐へ行き、`tolist()` で Python の数値に
    落ちてから `_check_number` に渡る)。そのため
    `isinstance(value, (int, float, np.integer, np.floating))` から
    **numpy の 2 型を外す**変異が 156 passed で生存した。実 indicator は
    `df["close"].iloc[-1]` のような `np.float64` をそのまま返すのが普通なので、
    この allowlist が落ちると正常な indicator が軒並み
    `IndicatorResultError` になる (fail closed 側への退行)。
    `np.bool_` が拒否される側も同時に pin する。
    """
    idx = _idx()
    out = validate_indicator_result(
        {"a": np.float64(1.5), "b": np.int32(2)},
        df_index=idx, outputs=("a", "b"))
    assert out["a"] == 1.5 and out["b"] == 2.0
    with pytest.raises(IndicatorResultError, match="bool"):
        validate_indicator_result({"a": np.bool_(True)}, df_index=idx,
                                  outputs=("a",))


def test_series_index_mismatch_rejected():
    idx = _idx()
    other = pd.date_range("2027-01-01", periods=5, freq="1h", tz="UTC")
    with pytest.raises(IndicatorResultError, match="index"):
        validate_indicator_result({"a": pd.Series([1.0] * 5, index=other)},
                                  df_index=idx, outputs=("a",))


def test_series_length_mismatch_rejected():
    idx = _idx()
    with pytest.raises(IndicatorResultError, match="length"):
        validate_indicator_result({"a": [1.0, 2.0]}, df_index=idx, outputs=("a",))


def test_length_one_series_is_not_accepted_as_a_scalar():
    """段 0 束 1 M14: 長さ 1 の系列を「スカラー相当」として通さない。

    既存の長さ検査の負例は長さ 2 だけだったため、`len(seq) != expected_len`
    を `len(seq) not in (1, expected_len)` に緩める変異 (= 1 点しか返さない
    indicator を全バー系列として受理する) が 判定 suite 丸ごと green のまま
    生存した。スカラーを返したい indicator は `float` を返す契約であり
    (設計書 §2.5 (a))、長さ 1 の list/ndarray/Series は df と長さが違う
    以上つねに不合格でなければならない。
    """
    idx = _idx()
    with pytest.raises(IndicatorResultError, match="length"):
        validate_indicator_result({"a": [1.0]}, df_index=idx, outputs=("a",))
    with pytest.raises(IndicatorResultError, match="length"):
        validate_indicator_result({"a": np.array([1.0])}, df_index=idx,
                                  outputs=("a",))


def test_series_longer_than_df_is_rejected():
    """1 周目 ローカル LLM (c03 ornith): 長さ不一致の**長すぎる側**。

    既存の負例は長さ 2 (`test_series_length_mismatch_rejected`) と長さ 1
    (段 0 M14) で、どちらも df より**短い**。そのため
    `len(seq) != expected_len` を `len(seq) < expected_len` に緩める変異が
    生存した (実測: tests/core + tests/plugin/test_sandbox.py +
    tests/tools/test_plugin_loader.py で 149 passed)。この変異下で長い系列は
    長さ検査を通過し、直後の `pd.Series(values, index=df_index)` が
    **pandas の生の ValueError** を送出する — 段 0 M14 と同じく
    `except IndicatorResultError` を素通りして上位へ抜ける故障形になる。
    長さ違いは必ず `IndicatorResultError` へ写像される、という側を pin する。
    """
    idx = _idx()
    with pytest.raises(IndicatorResultError, match="length"):
        validate_indicator_result({"a": [1.0] * 8}, df_index=idx, outputs=("a",))


def test_pd_series_elements_reject_bool_and_inf():
    """1 周目 ローカル LLM (c03 ornith): `pd.Series` 経路の要素検査。

    `test_series_with_inf_or_bool_rejected` は `list` を渡すので
    `isinstance(value, pd.Series)` 分岐には入らず、`_nan_or_number` 経由の
    list 経路しか踏まない。そのため Series 分岐の
    `None if pd.isna(v) else _check_number(v, where)` を
    `None if pd.isna(v) else float(v)` に緩める変異が生存した
    (実測: tests/core + tests/plugin/test_sandbox.py +
    tests/tools/test_plugin_loader.py で 149 passed)。この変異下では
    bool が 1.0 / 0.0 へ、±Inf と数値文字列がそのまま通り、契約 7 の
    「bool・±Inf・数値文字列は拒否」が Series 返却の indicator に対して
    まるごと外れる (実 indicator は `pd.Series` を返すのが本線)。
    """
    idx = _idx()
    for bad in (float("inf"), float("-inf"), True, "1.5"):
        with pytest.raises(IndicatorResultError):
            validate_indicator_result(
                {"a": pd.Series([1.0, 2.0, bad, 4.0, 5.0], index=idx,
                                dtype="object")},
                df_index=idx, outputs=("a",))


def test_series_with_inf_or_bool_rejected():
    idx = _idx()
    with pytest.raises(IndicatorResultError):
        validate_indicator_result({"a": [1.0, 2.0, float("inf"), 4.0, 5.0]},
                                  df_index=idx, outputs=("a",))
    with pytest.raises(IndicatorResultError):
        validate_indicator_result({"a": [True] * 5}, df_index=idx, outputs=("a",))


def test_outputs_set_must_match_exactly():
    idx = _idx()
    with pytest.raises(IndicatorResultError, match="outputs"):
        validate_indicator_result({"a": 1.0}, df_index=idx, outputs=("a", "b"))
    with pytest.raises(IndicatorResultError, match="outputs"):
        validate_indicator_result({"a": 1.0, "x": 1.0}, df_index=idx,
                                  outputs=("a",))


def test_outputs_none_allows_empty_dict_for_standalone():
    idx = _idx()
    assert validate_indicator_result({}, df_index=idx, outputs=None) == {}
    assert validate_indicator_result({"whatever": 1.0}, df_index=idx,
                                     outputs=None) == {"whatever": 1.0}


def test_non_dict_and_non_str_keys_rejected():
    idx = _idx()
    with pytest.raises(IndicatorResultError):
        validate_indicator_result([1, 2], df_index=idx, outputs=None)
    with pytest.raises(IndicatorResultError):
        validate_indicator_result({1: 2.0}, df_index=idx, outputs=None)


def test_numeric_string_is_rejected():
    """opus r1 M3: `float("1.5")` が通るため素の `float()` では数値文字列を
    受理してしまう。スカラー経路・系列経路の両方を pin する。"""
    idx = _idx()
    with pytest.raises(IndicatorResultError, match="got str"):
        validate_indicator_result({"a": "1.5"}, df_index=idx, outputs=("a",))
    with pytest.raises(IndicatorResultError, match="got str"):
        validate_indicator_result({"a": ["1.5"] * 5}, df_index=idx,
                                  outputs=("a",))


def test_nested_container_element_raises_indicator_result_error():
    """opus r1 M4: 要素が list / dict のとき `pd.isna(v)` は配列を返し、
    素の `ValueError`(truth value ambiguous) が漏れていた。必ず
    `IndicatorResultError` に写像されること。"""
    idx = _idx()
    with pytest.raises(IndicatorResultError):
        validate_indicator_result({"a": [[1.0], [2.0], [3.0], [4.0], [5.0]]},
                                  df_index=idx, outputs=("a",))
    with pytest.raises(IndicatorResultError):
        validate_indicator_result({"a": [{"x": 1}] * 5}, df_index=idx,
                                  outputs=("a",))


def test_ohlcv_passthrough_preserves_order_values_index_and_all_prefixes():
    idx = _idx()
    df = pd.DataFrame({"open": [1.0, 2.0, 3.0, 4.0, 5.0],
                       "high": [2.0, 3.0, 4.0, 5.0, 6.0],
                       "low": [0.0, 1.0, 2.0, 3.0, 4.0],
                       "close": [1.5, 2.5, 3.5, 4.5, 5.5],
                       "volume": [10.0, 20.0, 30.0, 40.0, 50.0]}, index=idx)
    before = df.copy(deep=True)
    result = {column: df[column].copy() for column in
              ("open", "high", "low", "close", "volume")}

    got = validate_ohlcv_passthrough(result, df=df, original_df=before)

    assert list(got) == ["open", "high", "low", "close", "volume"]
    for end in range(1, len(df) + 1):
        for column in got:
            pd.testing.assert_series_equal(got[column].iloc[:end],
                                            before[column].iloc[:end], check_names=False)
    pd.testing.assert_frame_equal(df, before)
    assert not any(series.isna().any() for series in got.values())


@pytest.mark.parametrize("broken", [
    lambda df: {"open": df["open"].copy(), "volume": df["volume"].copy(),
                "high": df["high"].copy(), "low": df["low"].copy(),
                "close": df["close"].copy()},
    lambda df: {c: (df[c].copy() + 1 if c == "close" else df[c].copy())
                for c in ("open", "high", "low", "close", "volume")},
])
def test_ohlcv_passthrough_rejects_wrong_output_order_or_value(broken):
    idx = _idx()
    df = pd.DataFrame({c: [float(i) for i in range(5)]
                       for c in ("open", "high", "low", "close", "volume")}, index=idx)
    result = broken(df)
    if list(result) == ["open", "high", "low", "close", "volume"]:
        with pytest.raises(IndicatorResultError, match="equal input"):
            validate_ohlcv_passthrough(result, df=df, original_df=df.copy(deep=True))
    else:
        with pytest.raises(IndicatorResultError, match="order"):
            validate_ohlcv_passthrough(result, df=df, original_df=df.copy(deep=True))


def test_ohlcv_passthrough_rejects_input_mutation_and_new_nan():
    idx = _idx()
    before = pd.DataFrame({c: [1.0] * 5 for c in
                           ("open", "high", "low", "close", "volume")}, index=idx)
    changed = before.copy(deep=True)
    changed.loc[idx[0], "close"] = 9.0
    result = {c: changed[c].copy() for c in changed}
    with pytest.raises(IndicatorResultError, match="mutated"):
        validate_ohlcv_passthrough(result, df=changed, original_df=before)

    result = {c: before[c].copy() for c in before}
    result["close"].iloc[2] = float("nan")
    with pytest.raises(IndicatorResultError, match="equal input"):
        validate_ohlcv_passthrough(result, df=before, original_df=before.copy(deep=True))


def test_ohlcv_passthrough_rejects_a_list_even_when_its_values_match():
    idx = _idx()
    df = pd.DataFrame({c: [1.0] * 5 for c in
                       ("open", "high", "low", "close", "volume")}, index=idx)
    result = {c: df[c].tolist() for c in df}
    with pytest.raises(IndicatorResultError, match="must be a Series"):
        validate_ohlcv_passthrough(result, df=df, original_df=df.copy(deep=True))
