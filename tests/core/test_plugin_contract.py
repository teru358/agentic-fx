"""[indicator-consumption-wiring] T2: indicator 戻り値の共通 validator (V1)。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from agentic_fx.core.plugin_contract import (
    IndicatorResultError, validate_indicator_result,
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
