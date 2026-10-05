from __future__ import annotations


def test_validator_folds_list_to_series_before_wire():
    """validator は list を df_index 付きで必ず pd.Series に畳むので、
    wire 変換には畳まれた後の形で入り、NaN は null になる。"""
    import pandas as pd

    from agentic_fx.core.plugin_contract import validate_indicator_result
    from agentic_fx.plugin.worker import _indicator_result_to_wire

    idx = pd.RangeIndex(3)
    validated = validate_indicator_result(
        {"a": [1.0, float("nan"), 3.0]}, df_index=idx, outputs=("a",))
    assert isinstance(validated["a"], pd.Series)
    assert _indicator_result_to_wire(validated) == {
        "a": {"series": [1.0, None, 3.0]}}
