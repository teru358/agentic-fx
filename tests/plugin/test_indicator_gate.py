from __future__ import annotations

import numpy as np
import pandas as pd

from agentic_fx.plugin import indicator_gate as gate


def _series(values):
    return pd.Series(values, index=pd.date_range("2026-01-01", periods=len(values), freq="h", tz="UTC"))


def test_symbol_tokens_only_splits_real_currency_pairs_and_closed_aliases():
    tokens = gate.symbol_tokens(["XAUUSD.m", "USDJPY", "US500", "USOIL"])
    assert {"xauusd", "xau", "usd", "usdjpy", "jpy", "us500", "usoil", "gold", "oil", "wti", "crude", "spx", "sp500", "yen", "dollar"} <= tokens
    assert "per" not in gate.symbol_tokens(["volume_per_bar", "copper"])


def test_candidate_tokens_finds_all_locations_and_avoids_word_fragments():
    meta = {"name": "neutral", "params": {"nested": {"xauUsd": "gold"}}}
    src = '"""us500 commentary"""\nVALUE = "x_a_u"\ndef f():\n    "USDJPY docs"\n'
    hits = gate.candidate_tokens(meta, src, "label: jpy_value")
    places = {(h.place, h.token) for h in hits}
    assert ("param_key", "xau") in places and ("param_value", "gold") in places
    assert ("docstring", "us500") in places and ("string_constant", "xau") in places
    assert ("config_value", "jpy") in places
    assert not gate.check_symbol_in_name(
        gate.candidate_tokens({"name": "volume_per_bar", "params": {"period": 14, "alpha": 0.2}}, "", ""),
        gate.symbol_tokens(["XAUUSD"]),
    ).code


def test_symbol_name_grandfather_requires_hit_set_containment():
    symbols = gate.symbol_tokens(["XAUUSD", "USDJPY", "US500"])
    hits = gate.candidate_tokens({"name": "xau_signal"}, "", "")
    legacy = gate.check_symbol_in_name(hits, symbols, grandfather_hits=hits)
    assert legacy.ok and legacy.warnings == ("legacy_symbol_name",)
    extra = gate.candidate_tokens({"name": "xau_usdjpy_signal"}, "", "")
    assert gate.check_symbol_in_name(extra, symbols, grandfather_hits=hits).code == "symbol_in_name"


def test_param_price_units_reject_closed_price_vocabulary_only_in_keys():
    assert gate.check_param_price_unit({"nested": {"priceThreshold": 1}}).code == "param_price_unit"
    assert gate.check_param_price_unit({"pip_size": 1}).code == "param_price_unit"
    assert gate.check_param_price_unit({"period": 14, "window": 8, "alpha": "pip"}).ok


def test_classify_scale_requires_both_price_scales_to_agree():
    base = pd.DataFrame({"a": [1., 2.], "b": [2., 4.], "c": [1., 2.]})
    k100 = pd.DataFrame({"a": [1., 2.], "b": [200., 400.], "c": [100., 200.]})
    k001 = pd.DataFrame({"a": [1., 2.], "b": [.02, .04], "c": [1., 2.]})
    vol = pd.DataFrame({"a": [1., 2.], "b": [2., 4.], "c": [100., 200.]})
    assert gate.classify_scale(base, k100, k001, vol) == {
        "a": ("invariant", "invariant"), "b": ("proportional", "invariant"), "c": ("neither", "neither"),
    }


def test_finite_report_marks_sparse_nan_and_constant_columns():
    report = gate.finite_report(pd.DataFrame({"s": [np.nan, 1.], "n": [np.nan, np.nan], "c": [1., 1.]}), 0)
    assert report == {"s": (1, 1), "n": (0, 0), "c": (2, 1)}
    assert gate.is_sparse(report["s"]) and gate.is_all_nan(report["n"])


def test_compare_exact_mask_duplicate_and_overlap():
    a = _series(np.arange(40, dtype=float))
    exact = gate.compare_outputs({"x": a}, {"one": {"y": a}}, mode="warn")
    assert exact.code == "indicator_duplicate" and exact.similar_to[0]["kind"] == "duplicate"
    masked = a.copy(); masked.iloc[0] = np.nan
    assert gate.compare_outputs({"x": masked}, {"one": {"y": a}}, mode="warn").code != "indicator_duplicate"
    overlap = gate.compare_outputs({"x": a, "other": a + 3}, {"one": {"y": a}}, mode="warn")
    assert "indicator_output_overlap" in overlap.warnings


def test_compare_exact_uses_the_specification_tolerance():
    a = _series(np.arange(40, dtype=float) + 1)
    close_but_distinct = a + 1e-4
    result = gate.compare_outputs({"x": a}, {"one": {"y": close_but_distinct}}, mode="warn")
    assert result.code != "indicator_duplicate"


def test_compare_monotone_requires_level_and_difference_ranks():
    x = _series(np.linspace(1, 10, 50) ** 2)
    monotone = gate.compare_outputs({"x": x}, {"one": {"y": x ** 3}}, mode="warn")
    assert monotone.warnings == ("indicator_monotone_duplicate",)
    # Similar levels, but alternating changes: the difference-rank condition fails.
    noisy = _series(np.linspace(1, 10, 50) ** 2 + np.where(np.arange(50) % 2, 5, -5))
    assert gate.compare_outputs({"x": x}, {"one": {"y": noisy}}, mode="warn").code != "indicator_monotone_duplicate"
    enforced = gate.compare_outputs({"x": x}, {"one": {"y": x ** 3}}, mode="enforce")
    assert not enforced.ok and enforced.code == "indicator_monotone_duplicate"


def test_compare_similar_sparse_neither_and_scalar_rules():
    rng = np.random.default_rng(1)
    x = _series(np.cumsum(rng.normal(size=100)))
    y = _series(2 * x.to_numpy() + rng.normal(scale=.2, size=100))
    similar = gate.compare_outputs({"x": x}, {"one": {"y": y}}, mode="warn", classifications={"x": ("invariant", "invariant")})
    assert "indicator_similar" in similar.warnings
    sparse = _series([np.nan] * 48 + [1., 2.])
    assert gate.compare_outputs({"x": sparse}, {"one": {"y": sparse}}, mode="warn").code == "indicator_duplicate"
    assert gate.compare_outputs({"x": 1.0}, {"one": {"y": 1.0}}, mode="warn").code == "indicator_duplicate"
    assert gate.compare_outputs({"x": x}, {"one": {"y": y}}, mode="warn", classifications={"x": ("neither", "invariant")}).code != "indicator_similar"


def test_new_gate_codes_are_known_to_notice_dictionary():
    from agentic_fx import improve_notice_codes as codes
    for code in gate.REJECT_CODES:
        assert codes.notice_for_plugin_gate(code).reason_code == code
