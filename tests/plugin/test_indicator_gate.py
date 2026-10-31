from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import yaml

from agentic_fx.plugin import indicator_gate as gate

NAN = float("nan")


def _series(values, start=0):
    idx = pd.date_range("2026-01-01", periods=len(values) + start, freq="h", tz="UTC")[start:]
    return pd.Series(np.asarray(values, dtype=float), index=idx)


def _walk(seed, n=100):
    return _series(np.cumsum(np.random.default_rng(seed).normal(size=n)))


def _spiky(spike_seed, noise_seed, noise, *, n=200, sign=1.0):
    """Cumulated series whose changes are dominated by a few shared spikes."""
    spikes = np.zeros(n)
    pos = np.random.default_rng(spike_seed).choice(n, 6, replace=False)
    spikes[pos] = 10 * (np.arange(6) % 2 * 2 - 1)
    noise_part = np.random.default_rng(noise_seed).normal(0, noise, n)
    return _series(sign * np.cumsum(spikes + noise_part))


def _rising(seed, n=60):
    return _series(np.cumsum(np.random.default_rng(seed).uniform(0.5, 1.5, n)))


def _compare(candidate, deployed, **kwargs):
    kwargs.setdefault("mode", "warn")
    return gate.compare_outputs(candidate, deployed, **kwargs)


def _fake_stats(monkeypatch, *, level=None, diff=None, pearson=None, n=40):
    if level is not None or diff is not None:
        monkeypatch.setattr(gate, "_spearman",
                            lambda a, b: level if len(a) == n else diff)
    if pearson is not None:
        monkeypatch.setattr(gate, "_pearson", lambda a, b: pearson)


# ---------------------------------------------------------------- symbols


def test_symbol_tokens_exact_set_and_suffix_cut():
    tokens = gate.symbol_tokens(["XAUUSD.m", "USDJPY", "US500.cash", "USOIL"])
    assert tokens == frozenset({
        "xauusd", "xau", "usd", "gold", "dollar", "usdjpy", "jpy", "yen",
        "us500", "spx", "sp500", "usoil", "oil", "wti", "crude",
    })
    assert "xauusd.m" not in tokens and "us500.cash" not in tokens


def test_symbol_tokens_split_only_when_both_halves_are_currencies():
    assert gate.symbol_tokens(["copper"]) == frozenset({"copper"})
    assert gate.symbol_tokens(["natgas"]) == frozenset({"natgas"})
    assert gate.symbol_tokens(["usdxxx"]) == frozenset({"usdxxx"})
    assert gate.symbol_tokens(["xxxusd"]) == frozenset({"xxxusd"})
    assert gate.symbol_tokens(["eurusd"]) == frozenset({"eurusd", "eur", "usd", "euro", "dollar"})
    assert gate.symbol_tokens(["xagusd"]) == frozenset({"xagusd", "xag", "usd", "silver", "dollar"})


def test_alias_table_is_pinned_row_by_row():
    assert gate.ALIASES == {
        "xau": ("gold",), "xag": ("silver",), "usoil": ("oil", "wti", "crude"),
        "us500": ("spx", "sp500"), "jp225": ("nikkei",), "jpy": ("yen",),
        "usd": ("dollar",), "eur": ("euro",),
    }
    assert gate.symbol_tokens(["JP225"]) == frozenset({"jp225", "nikkei"})
    assert gate.symbol_tokens(["USDJPY"]) >= {"yen"}
    assert gate.symbol_tokens(["EURUSD"]) >= {"euro"}
    assert gate.symbol_tokens(["USOIL"]) >= {"wti"}
    assert gate.symbol_tokens([]) == frozenset()


# ---------------------------------------------------------------- tokenizer


@pytest.mark.parametrize("value, expected", [
    ("us500", ["us500", "us", "500"]),
    ("500us", ["500us", "500", "us"]),
    ("xauUsd", ["xauusd", "xau", "usd"]),
    ("XAUUSD", ["xauusd"]),
    ("xau_usd", ["xau", "usd"]),
    ("xau_xau", ["xau"]),
    ("x_a_u", ["x", "a", "u", "xau"]),
    ("x_a", ["x", "a", "xa"]),
    ("a_bc_d", ["a", "bc", "d"]),
    ("a_b_cd_e_f", ["a", "b", "cd", "e", "f", "ab", "ef"]),
    ("abc1", ["abc1", "abc", "1"]),
    ("1abc", ["1abc", "1", "abc"]),
    ("a1b", ["a1b", "a", "1", "b"]),
    ("", []),
    ("  ,;. ", []),
    ("Volume Per-Bar", ["volume", "per", "bar"]),
])
def test_tokenizer_cases(value, expected):
    assert gate._tokens(value) == list(dict.fromkeys(expected))


def test_tokenizer_digit_to_letter_and_letter_to_digit_boundaries_both_split():
    assert {"xau", "100"} <= set(gate._tokens("100xau"))
    assert {"xau", "100"} <= set(gate._tokens("xau100"))


# ---------------------------------------------------------------- candidate_tokens


def test_candidate_tokens_exact_hits_for_every_location():
    meta = {"name": "goldFast", "params": {"nestedKey": {"xauUsd": ["yen", ("oil", 3)]}, "n": 5}}
    src = ('"""module sea"""\n'
           'VALUE = "const wti"\n'
           'class K:\n    """class cad"""\n    X = "x"\n'
           'def f():\n    """func chf"""\n    y = "inner nok"\n'
           'async def g():\n    """async sek"""\n')
    hits = gate.candidate_tokens(meta, src, "alpha:\n  - beta\n  - {k: gamma}\nlevel: 7\n")
    got = {(h.place, h.token) for h in hits}
    assert got == {
        ("name", "goldfast"), ("name", "gold"), ("name", "fast"),
        ("param_key", "nestedkey"), ("param_key", "nested"), ("param_key", "key"),
        ("param_key", "xauusd"), ("param_key", "xau"), ("param_key", "usd"),
        ("param_value", "yen"), ("param_value", "oil"), ("param_key", "n"),
        ("docstring", "module"), ("docstring", "sea"),
        ("string_constant", "const"), ("string_constant", "wti"),
        ("docstring", "class"), ("docstring", "cad"),
        ("string_constant", "x"),
        ("docstring", "func"), ("docstring", "chf"),
        ("string_constant", "inner"), ("string_constant", "nok"),
        ("docstring", "async"), ("docstring", "sek"),
        ("config_value", "beta"), ("config_value", "gamma"),
    }
    assert len(hits) == len(set(hits))


def test_candidate_tokens_params_recursion_into_lists_tuples_and_values():
    hits = gate.candidate_tokens({"name": "n", "params": {
        "a": [{"keyone": "valone"}, ("valtwo", {"keytwo": ["valthree"]})]}}, "", "")
    assert {(h.place, h.token) for h in hits} == {
        ("name", "n"), ("param_key", "a"), ("param_key", "keyone"), ("param_value", "valone"),
        ("param_value", "valtwo"), ("param_key", "keytwo"), ("param_value", "valthree"),
    }


def test_candidate_tokens_accepts_attribute_meta_and_missing_fields():
    meta = SimpleNamespace(name="xau", params={"k": "v"})
    assert {(h.place, h.token) for h in gate.candidate_tokens(meta, "", "")} == {
        ("name", "xau"), ("param_key", "k"), ("param_value", "v")}


def test_candidate_tokens_empty_meta_has_no_param_hits():
    hits = gate.candidate_tokens({}, "", "")
    assert all(h.place == "name" for h in hits)
    assert gate.candidate_tokens({"name": "", "params": {}}, "", "") == []


def test_docstring_is_decided_by_position_not_by_value():
    src = 'def f():\n    "same words"\n    x = "same words"\n'
    hits = gate.candidate_tokens({"name": ""}, src, "")
    assert {(h.place, h.token) for h in hits} == {
        ("docstring", "same"), ("docstring", "words"),
        ("string_constant", "same"), ("string_constant", "words")}
    only_second = 'x = "same words"\n"same words"\n'
    assert {h.place for h in gate.candidate_tokens({"name": ""}, only_second, "")} == {"string_constant"}


def test_only_the_first_statement_string_is_a_docstring():
    src = '"""doc one"""\n"second"\ndef f():\n    pass\n    "late"\n'
    got = {(h.place, h.token) for h in gate.candidate_tokens({"name": ""}, src, "")}
    assert got == {("docstring", "doc"), ("docstring", "one"),
                   ("string_constant", "second"), ("string_constant", "late")}


def test_grandfather_cannot_be_bypassed_by_a_docstring_of_the_same_text():
    symbols = gate.symbol_tokens(["XAUUSD"])
    legacy = gate.candidate_tokens({"name": "n"}, '"""xau"""\n', "")
    now = gate.candidate_tokens({"name": "n"}, '"""xau"""\nX = "xau"\n', "")
    assert gate.check_symbol_in_name(now, symbols, grandfather_hits=legacy).code == "symbol_in_name"
    assert gate.check_symbol_in_name(legacy, symbols, grandfather_hits=legacy).warnings == (
        "legacy_symbol_name",)


def test_source_that_does_not_parse_raises():
    with pytest.raises(SyntaxError):
        gate.candidate_tokens({"name": "n"}, "def broken(:\n", "")


def test_config_checks_parsed_string_values_not_keys():
    assert [h for h in gate.candidate_tokens({"name": ""}, "", "gold: 1\nxau: 2\n")] == []
    hits = gate.candidate_tokens({"name": ""}, "", "label: xau\nnested:\n  - [yen]\n  - {k: crude}\nn: 3\nflag: true\n")
    assert {(h.place, h.token) for h in hits} == {
        ("config_value", "xau"), ("config_value", "yen"), ("config_value", "crude")}
    assert gate.candidate_tokens({"name": ""}, "", "") == []
    assert gate.candidate_tokens({"name": ""}, "", "# only a comment\n") == []
    assert gate.candidate_tokens({"name": ""}, "", "- xau\n- 5\n")[0] == gate.Hit("config_value", "xau")


def test_config_that_is_not_yaml_raises():
    with pytest.raises(yaml.YAMLError):
        gate.candidate_tokens({"name": ""}, "", "a: [unclosed\n")


# ---------------------------------------------------------------- check_symbol_in_name


def test_check_symbol_in_name_without_a_hit_is_ok():
    symbols = gate.symbol_tokens(["XAUUSD", "USDJPY", "US500"])
    for meta in ({"name": "volume_per_bar", "params": {"period": 14, "window": 8, "alpha": 0.2}},
                 {"name": "us", "params": {}}, {"name": "copper_basis"}):
        verdict = gate.check_symbol_in_name(gate.candidate_tokens(meta, "", ""), symbols)
        assert verdict == gate.Verdict(True, "", ())


@pytest.mark.parametrize("meta, src, config", [
    ({"name": "xauusd_x"}, "", ""),
    ({"name": "x", "params": {"xauUsd": 1}}, "", ""),
    ({"name": "x", "params": {"k": "gold"}}, "", ""),
    ({"name": "x"}, 'V = "usdjpy"', ""),
    ({"name": "x"}, '"""us500 commentary"""', ""),
    ({"name": "x"}, "", "label: wti"),
    ({"name": "x_a_u"}, "", ""),
    ({"name": "x"}, 'V = "x_a_u"', ""),
])
def test_check_symbol_in_name_rejects_each_location(meta, src, config):
    symbols = gate.symbol_tokens(["XAUUSD", "USDJPY", "US500", "USOIL"])
    verdict = gate.check_symbol_in_name(gate.candidate_tokens(meta, src, config), symbols)
    assert (verdict.ok, verdict.code, verdict.warnings) == (False, "symbol_in_name", ())


def test_symbol_name_grandfather_requires_hit_set_containment():
    symbols = gate.symbol_tokens(["XAUUSD", "USDJPY", "US500"])
    hits = gate.candidate_tokens({"name": "xau_signal"}, "", "")
    legacy = gate.check_symbol_in_name(hits, symbols, grandfather_hits=hits)
    assert legacy == gate.Verdict(True, "", ("legacy_symbol_name",))
    extra = gate.candidate_tokens({"name": "xau_usdjpy_signal"}, "", "")
    assert gate.check_symbol_in_name(extra, symbols, grandfather_hits=hits).code == "symbol_in_name"
    assert gate.check_symbol_in_name(hits, symbols, grandfather_hits=[]).code == "symbol_in_name"
    assert gate.check_symbol_in_name(hits, symbols).code == "symbol_in_name"
    # a subset of the old hits stays grandfathered
    wide = gate.candidate_tokens({"name": "xau_usdjpy_signal"}, "", "")
    assert gate.check_symbol_in_name(hits, symbols, grandfather_hits=wide).warnings == ("legacy_symbol_name",)


def test_grandfather_is_place_sensitive_and_ignores_non_symbol_tokens():
    symbols = gate.symbol_tokens(["XAUUSD"])
    in_name = gate.candidate_tokens({"name": "xau"}, "", "")
    old_elsewhere = gate.candidate_tokens({"name": "other"}, '"""xau"""', "")
    assert gate.check_symbol_in_name(in_name, symbols, grandfather_hits=old_elsewhere).code == "symbol_in_name"
    noisy = gate.candidate_tokens({"name": "xau_signal"}, "", "")
    plain = gate.candidate_tokens({"name": "xau"}, "", "")
    assert gate.check_symbol_in_name(plain, symbols, grandfather_hits=noisy).ok


def test_hits_with_foreign_tokens_are_ignored_by_the_check():
    symbols = gate.symbol_tokens(["XAUUSD"])
    assert gate.check_symbol_in_name([gate.Hit("name", "momentum")], symbols).ok


# ---------------------------------------------------------------- price unit


@pytest.mark.parametrize("key", [
    "pip", "pips", "pipsize", "pipSize", "pip_size", "point", "points", "ticksize", "tickSize",
    "tick_size", "point_size", "pip_value", "price_level", "absolute_price", "priceThreshold",
    "price_", "lastPrice", "pricelevel", "pricey",
])
def test_param_price_unit_rejects_each_vocabulary_word_as_key(key):
    assert gate.check_param_price_unit({key: 1}) == gate.Verdict(False, "param_price_unit", ())
    assert gate.check_param_price_unit({"outer": [{"inner": {key: 1}}]}).code == "param_price_unit"


def test_param_price_unit_accepts_neutral_keys_and_ignores_values():
    assert gate.check_param_price_unit({"period": 14, "window": 8, "alpha": "pip", "k": "price",
                                        "pricing": 1, "tick": 2, "size": 3, "t": ["points"]}) == gate.Verdict()
    assert gate.check_param_price_unit({}).ok
    assert gate.check_param_price_unit(None).ok


# ---------------------------------------------------------------- classify_scale


def _scale(base, k100, k001, vol=None):
    frame = lambda v: pd.DataFrame({"c": v})  # noqa: E731
    return gate.classify_scale(frame(base), frame(k100), frame(k001), frame(vol if vol is not None else base))["c"]


def test_classify_scale_needs_both_scales_to_agree():
    b = [1., 2., 3.]
    assert _scale(b, b, b) == ("invariant", "invariant")
    assert _scale(b, [100., 200., 300.], [.01, .02, .03]) == ("proportional", "invariant")
    assert _scale(b, b, [.01, .02, .03])[0] == "neither"
    assert _scale(b, [100., 200., 300.], b)[0] == "neither"
    assert _scale(b, [100., 200., 300.], [.02, .04, .06])[0] == "neither"
    assert _scale(b, [200., 400., 600.], [.01, .02, .03])[0] == "neither"
    assert _scale(b, [5., 5., 5.], [5., 5., 5.])[0] == "neither"


def test_classify_scale_zero_column_is_invariant_first():
    assert _scale([0., 0.], [0., 0.], [0., 0.])[0] == "invariant"


def test_classify_scale_volume_is_independent_of_price():
    b = [1., 2., 3.]
    assert _scale(b, b, b, vol=[100., 200., 300.]) == ("invariant", "neither")
    assert _scale(b, [100., 200., 300.], [.01, .02, .03], vol=[3., 2., 1.]) == ("proportional", "neither")
    assert _scale(b, [1., 2., 4.], b, vol=b) == ("neither", "invariant")


def test_classify_scale_tolerance_and_nan_and_shape_rules():
    b = [1000., 2000.]
    assert _scale(b, [1000.0005, 2000.], b)[0] == "invariant"
    assert _scale(b, [1000.002, 2000.], b)[0] == "neither"
    assert _scale(b, [1000.002, 2000.], b)[0] == "neither"
    assert gate.classify_scale(pd.DataFrame({"c": b}), pd.DataFrame({"c": [1000.002, 2000.]}),
                               pd.DataFrame({"c": b}), pd.DataFrame({"c": b}), rtol=1e-5)["c"][0] == "invariant"
    assert _scale([0.], [5e-10], [0.])[0] == "invariant"
    assert _scale([0.], [2e-9], [0.])[0] == "neither"
    assert _scale([NAN, 1.], [NAN, 1.], [NAN, 1.])[0] == "invariant"
    assert _scale([NAN, 1.], [1., NAN], [NAN, 1.])[0] == "neither"
    assert _scale([1., 2.], [NAN, 2.], [1., 2.])[0] == "neither"
    assert _scale([1., 2.], [1., 2.], [1., NAN])[0] == "neither"
    shorter = gate.classify_scale(pd.DataFrame({"c": [1., 2.]}), pd.DataFrame({"c": [1.]}),
                                  pd.DataFrame({"c": [1.]}), pd.DataFrame({"c": [1., 2.]}))
    assert shorter["c"] == ("neither", "invariant")


def test_classify_scale_covers_every_column_in_order():
    base = pd.DataFrame({"a": [1., 2.], "b": [2., 4.], "c": [1., 2.]})
    k100 = pd.DataFrame({"a": [1., 2.], "b": [200., 400.], "c": [100., 200.]})
    k001 = pd.DataFrame({"a": [1., 2.], "b": [.02, .04], "c": [1., 2.]})
    vol = pd.DataFrame({"a": [1., 2.], "b": [2., 4.], "c": [100., 200.]})
    result = gate.classify_scale(base, k100, k001, vol)
    assert list(result) == ["a", "b", "c"]
    assert result == {"a": ("invariant", "invariant"), "b": ("proportional", "invariant"),
                      "c": ("neither", "neither")}


# ---------------------------------------------------------------- finite report


def test_finite_report_warmup_nunique_inf_and_coercion():
    df = pd.DataFrame({"w": [5., 5., 1., 2.], "d": [1., 1., 2., np.nan], "i": [np.inf, -np.inf, 1., 2.],
                       "s": ["x", "3", "4", "4"]})
    assert gate.finite_report(df, 2) == {"w": (2, 2), "d": (1, 1), "i": (2, 2), "s": (2, 1)}
    assert gate.finite_report(df, 0) == {"w": (4, 3), "d": (3, 2), "i": (2, 2), "s": (3, 2)}
    assert gate.finite_report(df, 10) == {"w": (0, 0), "d": (0, 0), "i": (0, 0), "s": (0, 0)}


def test_finite_report_marks_sparse_nan_and_constant_columns():
    report = gate.finite_report(pd.DataFrame({"s": [NAN, 1.], "n": [NAN, NAN], "c": [1., 1.]}), 0)
    assert report == {"s": (1, 1), "n": (0, 0), "c": (2, 1)}


def test_sparse_and_all_nan_boundaries():
    assert [gate.is_sparse((n, 5)) for n in (0, 1, 29, 30, 31)] == [False, True, True, False, False]
    assert [gate.is_all_nan((n, 5)) for n in (0, 1, 2)] == [True, False, False]


# ---------------------------------------------------------------- helpers


def test_pearson_rejects_constant_short_and_scores_lines():
    line = np.arange(5.)
    assert np.isnan(gate._pearson(np.ones(5), line))
    assert np.isnan(gate._pearson(line, np.ones(5)))
    assert np.isnan(gate._pearson(np.array([1.]), np.array([2.])))
    assert np.isnan(gate._pearson(np.array([]), np.array([])))
    assert gate._pearson(line, 2 * line + 1) == pytest.approx(1.0)
    assert gate._pearson(line, -line) == pytest.approx(-1.0)
    assert gate._pearson(np.array([0., 1.]), np.array([0., 1.])) == pytest.approx(1.0)


def test_pearson_does_not_evaluate_degenerate_input():
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert np.isnan(gate._pearson(np.ones(5), np.arange(5.)))
        assert np.isnan(gate._pearson(np.arange(5.), np.ones(5)))


def test_allclose_nan_and_inf_positions():
    assert gate._allclose([1., np.inf], [1., np.inf])
    assert not gate._allclose([np.inf], [-np.inf])
    assert not gate._allclose([np.inf], [1.])
    assert not gate._allclose([NAN, 1.], [2., 1.])
    assert not gate._allclose([2., 1.], [NAN, 1.])
    assert gate._allclose([NAN, 1.], [NAN, 1.])
    assert not gate._allclose([1., 2.], [1.])


def test_spearman_uses_average_ranks_for_ties():
    assert gate._spearman(np.array([1., 1, 2, 2]), np.array([1., 2, 3, 4])) == pytest.approx(0.894427191)
    assert gate._spearman(np.arange(5.) ** 3, np.arange(5.)) == pytest.approx(1.0)
    assert gate._spearman(np.arange(5.), -np.arange(5.)) == pytest.approx(-1.0)


def test_common_aligns_on_index_and_drops_non_finite_on_either_side():
    a = _series(np.arange(40, dtype=float))
    b = _series(np.arange(40, dtype=float) * 2, start=10)
    ca, cb = gate._common(a, b)
    assert ca.tolist() == list(range(10, 40)) and cb.tolist() == [2. * i for i in range(0, 30)]
    a2, b2 = a.copy(), _series(np.arange(40, dtype=float))
    a2.iloc[3] = NAN
    b2.iloc[7] = NAN
    ca, cb = gate._common(a2, b2)
    assert len(ca) == 38 and not np.isnan(ca).any() and not np.isnan(cb).any()
    assert 3.0 not in ca and 7.0 not in ca


def test_exact_series_alignment_and_mask_rules():
    a = _series(np.arange(40, dtype=float))
    assert gate._exact(a, a.copy())
    assert not gate._exact(a, _series(np.arange(50, dtype=float)))
    assert not gate._exact(_series(np.arange(50, dtype=float)), a)
    assert not gate._exact(a, _series(np.arange(40, dtype=float), start=1))
    hole = a.copy(); hole.iloc[5] = NAN
    assert not gate._exact(hole, a) and not gate._exact(a, hole)
    both_hole = a.copy(); both_hole.iloc[5] = NAN
    assert gate._exact(hole, both_hole)
    other_hole = a.copy(); other_hole.iloc[6] = NAN
    assert not gate._exact(hole, other_hole)


@pytest.mark.parametrize("low, high, expected", [
    (1000.0, 1000.0005, True),
    (1000.0, 1000.0010000015, True),
    (1000.0, 1000.0010000030, False),
    (1000.0, 1000.0011, False),
    (0.0, 5e-10, True),
    (0.0, 1.5e-9, False),
    (0.0, 1e-6, False),
    (1.0, 1.0 + 5e-7, True),
    (1.0, 1.0001, False),
    (-1000.0, -1000.0010000015, True),
    (-1000.0, -1000.0011, False),
])
def test_exact_tolerance_is_symmetric_for_scalars_and_series(low, high, expected):
    for a, b in ((low, high), (high, low)):
        assert gate._exact(a, b) is expected
        assert gate._exact(_series([a, a, a]), _series([b, b, b])) is expected
        assert gate._exact(_series([0., a, 5.]), _series([0., b, 5.])) is expected


def test_exact_scalar_rules():
    assert not gate._exact(NAN, NAN) and not gate._exact(np.inf, np.inf)
    assert not gate._exact(1.0, NAN)
    assert gate._exact(np.float64(2.0), 2.0)
    assert not gate._exact(1.0, _series([1.0] * 40)) and not gate._exact(_series([1.0] * 40), 1.0)


# ---------------------------------------------------------------- duplicate / overlap


def test_compare_exact_mask_duplicate_and_overlap():
    a = _series(np.arange(40, dtype=float))
    exact = _compare({"x": a}, {"one": {"y": a}})
    assert exact.code == "indicator_duplicate" and exact.similar_to == (
        {"name": "one", "column": "y", "kind": "duplicate"},)
    assert not exact.ok and exact.warnings == ()
    masked = a.copy(); masked.iloc[0] = np.nan
    assert _compare({"x": masked}, {"one": {"y": a}}).code != "indicator_duplicate"
    overlap = _compare({"x": a, "other": a + 3}, {"one": {"y": a}})
    assert overlap.ok and overlap.warnings == ("indicator_output_overlap",)


def test_duplicate_does_not_depend_on_mode_and_is_not_followed_by_other_relations():
    a, b = _walk(1), _walk(2)
    for mode in ("warn", "enforce"):
        res = _compare({"x": a}, {"one": {"y": a}}, mode=mode)
        assert not res.ok and res.code == "indicator_duplicate"
        assert res.similar_to == ({"name": "one", "column": "y", "kind": "duplicate"},)
    res = _compare({"x": a, "z": b}, {"one": {"q": b, "p": a}})
    assert res.similar_to == (
        {"name": "one", "column": "p", "kind": "duplicate"}, {"name": "one", "column": "q", "kind": "duplicate"})


def test_duplicate_needs_every_candidate_column_and_every_deployed_column():
    a, b, c = _walk(1), _walk(2), _walk(3)
    partial = _compare({"x": a, "y": b}, {"one": {"p": a, "q": c}})
    assert partial.ok and partial.code == "" and partial.warnings == ("indicator_output_overlap",)
    off = {"x": ("neither", "neither")}
    extra = _compare({"x": a}, {"one": {"p": a, "q": c}}, classifications=off)
    assert extra.ok and extra.code == "" and extra.warnings == ("indicator_output_overlap",)
    assert extra.similar_to == ({"name": "one", "column": "p", "kind": "overlap"},)
    same = _compare({"x": a, "y": c}, {"one": {"p": a, "q": c}}, classifications=off)
    assert same.code == "indicator_duplicate"
    unclassified = _compare({"x": a}, {"one": {"p": a, "q": c}})
    assert unclassified.code == "" and unclassified.warnings == ("indicator_monotone_duplicate",)


def test_duplicate_matching_is_one_to_one():
    a, c = _walk(1), _walk(3)
    twin = _compare({"x": a, "y": a.copy()}, {"one": {"p": a, "q": c}})
    assert twin.ok and twin.warnings == ("indicator_output_overlap",)
    fewer = _compare({"x": a, "y": a.copy()}, {"one": {"p": a}})
    assert fewer.ok and fewer.code == ""
    assert _compare({"x": a, "y": c}, {"one": {"p": c, "q": a}}).code == "indicator_duplicate"


def test_compare_exact_uses_the_specification_tolerance():
    a = _series(np.arange(40, dtype=float) + 1)
    assert _compare({"x": a}, {"one": {"y": a + 1e-4}}).code != "indicator_duplicate"
    assert _compare({"x": a}, {"one": {"y": a + 1e-8}}).code == "indicator_duplicate"
    big = _series(np.full(40, 1000.0))
    assert _compare({"x": big}, {"one": {"y": big + 0.0005}}).code == "indicator_duplicate"
    assert _compare({"x": big + 0.0005}, {"one": {"y": big}}).code == "indicator_duplicate"
    assert _compare({"x": big}, {"one": {"y": big + 0.0011}}).code != "indicator_duplicate"
    assert _compare({"x": big + 0.0011}, {"one": {"y": big}}).code != "indicator_duplicate"


def test_scalars_are_compared_exactly_and_mixed_with_series():
    s = _walk(1)
    assert _compare({"x": 1.0}, {"one": {"y": 1.0}}).code == "indicator_duplicate"
    assert _compare({"x": 1.0}, {"one": {"y": 1.0 + 5e-7}}).code == "indicator_duplicate"
    assert _compare({"x": 1.0}, {"one": {"y": 1.001}}).code == ""
    assert _compare({"x": NAN}, {"one": {"y": NAN}}).code == ""
    assert _compare({"x": 1.0}, {"one": {"y": s}}).code == ""
    assert _compare({"x": s}, {"one": {"y": 1.0}}).code == ""
    part = _compare({"x": s, "k": 2.0}, {"one": {"y": s, "z": 3.0}},
                    classifications={"x": ("neither", "neither")})
    assert part.ok and part.warnings == ("indicator_output_overlap",)
    both = _compare({"x": s, "k": 2.0}, {"one": {"z": 2.0, "y": s}})
    assert both.code == "indicator_duplicate"


def test_sparse_and_neither_columns_still_take_part_in_exact_comparison():
    sparse = _series([NAN] * 48 + [1., 2.])
    assert _compare({"x": sparse}, {"one": {"y": sparse}}).code == "indicator_duplicate"
    x = _walk(1)
    res = _compare({"x": x}, {"one": {"y": x}}, classifications={"x": ("neither", "neither")})
    assert res.code == "indicator_duplicate"


def test_deployed_empty_and_mode_validation():
    x = _walk(1)
    res = _compare({"x": x}, {})
    assert res == gate.CompareResult(True, "", ()) and res.similar_to == ()
    assert _compare({}, {"one": {"y": x}}).ok
    for bad in ("block", "WARN", "", "Enforce"):
        with pytest.raises(ValueError):
            gate.compare_outputs({"x": x}, {}, mode=bad)
        with pytest.raises(ValueError):
            gate.compare_outputs({"x": x}, {"one": {"y": x}}, mode=bad)


# ---------------------------------------------------------------- monotone


def test_compare_monotone_requires_level_and_difference_ranks():
    x = _series(np.linspace(1, 10, 50) ** 2)
    monotone = _compare({"x": x}, {"one": {"y": x ** 3}})
    assert monotone.ok and monotone.code == "" and monotone.warnings == ("indicator_monotone_duplicate",)
    assert monotone.similar_to == ({"name": "one", "column": "y", "kind": "monotone"},)
    enforced = _compare({"x": x}, {"one": {"y": x ** 3}}, mode="enforce")
    assert not enforced.ok and enforced.code == "indicator_monotone_duplicate" and enforced.warnings == ()
    noisy = _series(np.linspace(1, 10, 50) ** 2 + np.where(np.arange(50) % 2, 5, -5))
    assert _compare({"x": x}, {"one": {"y": noisy}}).code != "indicator_monotone_duplicate"
    assert "indicator_monotone_duplicate" not in _compare({"x": x}, {"one": {"y": noisy}}).warnings


def test_monotone_uses_rank_correlation_not_linear_correlation_of_levels():
    x = _series(np.linspace(1, 10, 50) ** 2)
    a, b = gate._common(x, x ** 3)
    assert gate._pearson(a, b) < 0.95
    assert "indicator_monotone_duplicate" in _compare({"x": x}, {"one": {"y": x ** 3}}).warnings


def test_monotone_needs_the_level_condition_alone_is_not_enough():
    x, y = _rising(1), _rising(2)
    assert gate._spearman(x.to_numpy(), y.to_numpy()) == pytest.approx(1.0)
    res = _compare({"x": x}, {"one": {"y": y}})
    assert res.warnings == () and res.similar_to == ()


def test_monotone_needs_the_difference_condition_alone_is_not_enough():
    x = _walk(5, 120)
    y = x + _series(np.linspace(0, 60, 120))
    a, b = gate._common(x, y)
    assert gate._spearman(np.diff(a), np.diff(b)) >= 0.98 and gate._spearman(a, b) < 0.999
    res = _compare({"x": x}, {"one": {"y": y}})
    assert "indicator_monotone_duplicate" not in res.warnings and res.ok


def test_monotone_accepts_decreasing_transforms():
    x = _series(np.linspace(1, 10, 50) ** 2)
    res = _compare({"x": x}, {"one": {"y": -(x ** 3)}})
    assert res.warnings == ("indicator_monotone_duplicate",)
    assert res.similar_to[0]["kind"] == "monotone"


def test_monotone_thresholds_are_inclusive_and_exact(monkeypatch):
    x, y = _walk(1, 40), _walk(2, 40)
    cases = [
        (0.999, 0.98, True), (-0.999, -0.98, True), (1.0, 1.0, True),
        (0.9989999, 0.98, False), (0.999, 0.9799999, False),
        (0.995, 0.99, False), (1.0, 0.95, False), (0.985, 0.979, False),
        (NAN, 1.0, False), (1.0, NAN, False),
    ]
    for level, diff, expected in cases:
        _fake_stats(monkeypatch, level=level, diff=diff, pearson=0.0)
        res = _compare({"x": x}, {"one": {"y": y}})
        assert ("indicator_monotone_duplicate" in res.warnings) is expected, (level, diff)


def test_monotone_row_count_boundary():
    for n, expected in ((28, False), (29, False), (30, True), (31, True)):
        x = _walk(1, n)
        res = _compare({"x": x}, {"one": {"y": 2 * x + 1}})
        assert ("indicator_monotone_duplicate" in res.warnings) is expected, n
    assert _compare({"x": _walk(1, 29)}, {"one": {"y": 2 * _walk(1, 29) + 1}}).warnings == ()


def test_monotone_uses_only_the_common_finite_rows():
    x = _walk(1, 45)
    y = 2 * x + 1
    y.iloc[:15] = NAN
    assert "indicator_monotone_duplicate" in _compare({"x": x}, {"one": {"y": y}}).warnings
    y.iloc[:16] = NAN
    assert _compare({"x": x}, {"one": {"y": y}}).warnings == ()
    shifted = _series(2 * x.to_numpy() + 1, start=16)
    assert _compare({"x": x}, {"one": {"y": shifted}}).warnings == ()
    shifted = _series(2 * x.to_numpy()[:45] + 1, start=15)
    assert _compare({"x": x}, {"one": {"y": shifted}}).warnings == ()


def test_monotone_needs_every_series_column_matched_one_to_one():
    x1, x2, junk = _walk(1), _walk(2), _walk(9)
    both = _compare({"u": x1, "v": x2}, {"one": {"p": 2 * x1 + 1, "q": 3 * x2 + 2}})
    assert both.warnings == ("indicator_monotone_duplicate",)
    assert {r["column"] for r in both.similar_to} == {"p", "q"}
    one_only = _compare({"u": x1, "v": x2}, {"one": {"p": 2 * x1 + 1, "q": junk}})
    assert one_only.warnings == ()
    same_target = _compare({"u": x1, "v": 3 * x1 + 5}, {"one": {"p": 2 * x1 + 1, "q": junk}})
    assert same_target.warnings == ()
    extra_deployed = _compare({"u": x1}, {"one": {"p": 2 * x1 + 1, "q": junk}})
    assert extra_deployed.warnings == ("indicator_monotone_duplicate",)


def test_monotone_ignores_neither_columns_and_scalars_when_counting_required_columns():
    x1, junk = _walk(1), _walk(9)
    cls = {"u": ("invariant", "invariant"), "w": ("neither", "invariant")}
    skip_neither = _compare({"u": x1, "w": junk}, {"one": {"p": 2 * x1 + 1}}, classifications=cls)
    assert skip_neither.warnings == ("indicator_monotone_duplicate",)
    also_series = _compare({"u": x1, "w": junk}, {"one": {"p": 2 * x1 + 1}})
    assert also_series.warnings == ()
    scalar = _compare({"u": x1, "s": 5.0}, {"one": {"p": 2 * x1 + 1, "t": 3.0}},
                      classifications={"u": ("invariant", "invariant")})
    assert scalar.warnings == ("indicator_monotone_duplicate",)
    only_neither = _compare({"w": x1}, {"one": {"p": 2 * x1 + 1}}, classifications={"w": ("neither", "neither")})
    assert only_neither.warnings == () and only_neither.similar_to == ()


def test_neither_only_looks_at_the_price_scale_class():
    x1 = _walk(1)
    deployed = {"one": {"p": 2 * x1 + 1}}
    volume_neither = _compare({"u": x1}, deployed, classifications={"u": ("invariant", "neither")})
    assert volume_neither.warnings == ("indicator_monotone_duplicate",)
    proportional = _compare({"u": x1}, deployed, classifications={"u": ("proportional", "invariant")})
    assert proportional.warnings == ("indicator_monotone_duplicate",)
    neither = _compare({"u": x1}, deployed, classifications={"u": ("neither", "invariant")})
    assert neither.warnings == ()
    unknown_col = _compare({"u": x1}, deployed, classifications={"other": ("neither", "neither")})
    assert unknown_col.warnings == ("indicator_monotone_duplicate",)


# ---------------------------------------------------------------- similar


def test_similar_threshold_uses_difference_pearson_and_is_inclusive(monkeypatch):
    x, y = _walk(1, 40), _walk(2, 40)
    cases = [
        (0.98, {}, True), (-0.98, {}, True), (0.9799999, {}, False), (-0.9799999, {}, False),
        (0.95, {}, False), (0.99, {}, True), (0.95, {"similar_corr": 0.9}, True),
        (0.95, {"similar_corr": 0.95}, True), (0.949999, {"similar_corr": 0.95}, False),
        (0.98, {"similar_corr": 0.995}, False), (NAN, {}, False),
    ]
    for value, kwargs, expected in cases:
        _fake_stats(monkeypatch, level=0.0, diff=0.0, pearson=value)
        res = _compare({"x": x}, {"one": {"y": y}}, **kwargs)
        assert ("indicator_similar" in res.warnings) is expected, (value, kwargs)


def test_similar_on_real_series_and_default_threshold():
    x, y = _spiky(1, 2, 0.05), _spiky(1, 3, 0.05)
    res = _compare({"x": x}, {"one": {"y": y}})
    assert res.ok and res.code == "" and res.warnings == ("indicator_similar",)
    assert res.similar_to == ({"name": "one", "column": "y", "kind": "similar"},)
    loose = _compare({"x": _spiky(1, 2, 0.3)}, {"one": {"y": _spiky(1, 3, 0.3)}})
    assert loose.warnings == ()
    assert _compare({"x": _spiky(1, 2, 0.3)}, {"one": {"y": _spiky(1, 3, 0.3)}}, similar_corr=0.95).warnings == (
        "indicator_similar",)


def test_similar_ignores_level_correlation_and_uses_linear_not_rank_correlation():
    x = _walk(1, 200)
    smooth_noise = _series(np.random.default_rng(4).normal(0, 1, 200))
    y = x + smooth_noise
    a, b = gate._common(x, y)
    assert gate._pearson(a, b) > 0.9 and abs(gate._pearson(np.diff(a), np.diff(b))) < 0.98
    assert _compare({"x": x}, {"one": {"y": y}}).warnings == ()
    sx, sy = _spiky(1, 2, 0.05), _spiky(1, 3, 0.05)
    a, b = gate._common(sx, sy)
    assert gate._spearman(np.diff(a), np.diff(b)) < 0.5
    assert "indicator_similar" in _compare({"x": sx}, {"one": {"y": sy}}).warnings


def test_similar_accepts_inverted_series():
    res = _compare({"x": _spiky(1, 2, 0.05)}, {"one": {"y": _spiky(1, 3, 0.05, sign=-1.0)}})
    assert res.warnings == ("indicator_similar",)


def test_similar_row_count_boundary():
    for n, expected in ((29, False), (30, True)):
        x = _spiky(1, 2, 0.05, n=n)
        y = x + 100.0
        res = _compare({"x": x}, {"one": {"y": y}}, similar_corr=0.98)
        assert ("indicator_similar" in res.warnings or "indicator_monotone_duplicate" in res.warnings) is expected, n


def test_similar_is_not_evaluated_when_the_plugin_is_a_monotone_duplicate():
    x = _walk(1)
    res = _compare({"x": x}, {"one": {"y": 2 * x + 1}})
    assert res.warnings == ("indicator_monotone_duplicate",)
    assert [r["kind"] for r in res.similar_to] == ["monotone"]


def test_similar_needs_every_series_column_one_to_one():
    sx1, sx2 = _spiky(1, 2, 0.05), _spiky(7, 8, 0.05)
    sy1, sy2 = _spiky(1, 3, 0.05), _spiky(7, 9, 0.05)
    junk = _walk(9, 200)
    both = _compare({"u": sx1, "v": sx2}, {"one": {"p": sy1, "q": sy2}})
    assert both.warnings == ("indicator_similar",) and len(both.similar_to) == 2
    half = _compare({"u": sx1, "v": sx2}, {"one": {"p": sy1, "q": junk}})
    assert half.warnings == ()
    same_target = _compare({"u": sx1, "v": _spiky(1, 5, 0.05)}, {"one": {"p": sy1, "q": junk}})
    assert same_target.warnings == ()
    skip_neither = _compare({"u": sx1, "w": junk}, {"one": {"p": sy1}},
                            classifications={"w": ("neither", "neither")})
    assert skip_neither.warnings == ("indicator_similar",)
    assert _compare({"u": sx1, "w": junk}, {"one": {"p": sy1}}).warnings == ()
    assert _compare({"u": sx1}, {"one": {"p": sy1}}, classifications={"u": ("neither", "neither")}).warnings == ()
    assert _compare({"u": sx1, "s": 4.0}, {"one": {"p": sy1, "t": 9.0}},
                    classifications={"u": ("invariant", "invariant")}).warnings == ("indicator_similar",)


def test_constant_and_degenerate_series_are_never_similar_or_monotone():
    flat = _series(np.ones(60))
    x = _walk(1, 60)
    res = _compare({"x": flat}, {"one": {"y": x}})
    assert res.warnings == () and res.similar_to == ()
    assert _compare({"x": x}, {"one": {"y": flat}}).warnings == ()
    two_flat = _compare({"x": flat}, {"one": {"y": flat * 3}})
    assert two_flat.warnings == ()


# ---------------------------------------------------------------- ordering and limits


def _plugins():
    s, t = _spiky(1, 2, 0.05), _spiky(7, 8, 0.05)
    return s, t, {
        "zdup": {"p": s, "q": t},
        "amono": {"p": 2 * s + 1, "q": 3 * t + 2},
        "zov": {"p": s, "q": _walk(9, 200)},
        "asim": {"p": _spiky(1, 3, 0.05), "q": _spiky(7, 9, 0.05)},
    }


def test_similar_to_orders_by_kind_strength():
    s, t, plugins = _plugins()
    cand = {"x": s, "y": t}
    top = _compare(cand, {k: plugins[k] for k in ("zdup", "amono")})
    assert [(r["name"], r["kind"]) for r in top.similar_to] == [
        ("zdup", "duplicate"), ("zdup", "duplicate"), ("amono", "monotone")]
    mono = _compare(cand, {k: plugins[k] for k in ("amono", "zov")})
    assert [(r["name"], r["kind"]) for r in mono.similar_to] == [
        ("amono", "monotone"), ("amono", "monotone"), ("zov", "overlap")]
    ov = _compare(cand, {k: plugins[k] for k in ("asim", "zov")})
    assert [(r["name"], r["kind"]) for r in ov.similar_to] == [
        ("zov", "overlap"), ("asim", "similar"), ("asim", "similar")]
    assert ov.warnings == ("indicator_output_overlap", "indicator_similar")


def test_similar_to_orders_by_score_then_name_then_column_and_caps_at_three():
    s = _spiky(1, 2, 0.05)
    cand = {"x": s}
    higher = _spiky(1, 3, 0.05)
    lower = _spiky(1, 4, 0.2)
    a, b = gate._common(s, lower)
    a2, b2 = gate._common(s, higher)
    assert abs(gate._pearson(np.diff(a), np.diff(b))) < abs(gate._pearson(np.diff(a2), np.diff(b2)))
    res = _compare(cand, {"aaa": {"c": lower}, "bbb": {"c": higher}})
    assert [r["name"] for r in res.similar_to] == ["bbb", "aaa"]
    negative_high = _spiky(1, 3, 0.05, sign=-1.0)
    res = _compare(cand, {"aaa": {"c": lower}, "zzz": {"c": negative_high}})
    assert [r["name"] for r in res.similar_to] == ["zzz", "aaa"]
    tie = _compare(cand, {"ccc": {"c": higher}, "bbb": {"c": higher}})
    assert [r["name"] for r in tie.similar_to] == ["bbb", "ccc"]

    x, y = _walk(1), _walk(2)
    off = {"x": ("neither", "neither"), "y": ("neither", "neither")}
    res = _compare({"x": x, "y": y}, {"one": {"q": x, "p": x, "r": y, "w": y}}, classifications=off)
    assert [(r["name"], r["column"], r["kind"]) for r in res.similar_to] == [
        ("one", "p", "overlap"), ("one", "q", "overlap"), ("one", "r", "overlap")]
    short = _compare({"x": x, "y": y}, {"one": {"q": x, "p": x}}, classifications=off)
    assert [r["column"] for r in short.similar_to] == ["p", "q"]


def test_similar_to_scores_the_matched_pair_not_the_first_candidate_edge():
    s = _spiky(1, 2, 0.05)
    twin = _spiky(1, 5, 0.05)
    near = _spiky(1, 3, 0.05)
    far = _spiky(1, 4, 0.2)
    mid = _spiky(1, 6, 0.1)
    res = _compare({"x": s, "y": twin}, {"aaa": {"p": near, "q": far}, "bbb": {"p": mid, "q": mid.copy()}})
    kinds = [(r["name"], r["column"]) for r in res.similar_to]
    assert kinds[0][0] == "bbb" or kinds[0][0] == "aaa"
    assert len(res.similar_to) == 3


def test_warning_order_is_overlap_then_similar_and_each_needs_its_kind():
    s, t, plugins = _plugins()
    cand = {"x": s, "y": t}
    both = _compare(cand, {"asim": plugins["asim"], "zov": plugins["zov"]})
    assert both.warnings == ("indicator_output_overlap", "indicator_similar")
    assert _compare(cand, {"zov": plugins["zov"]}).warnings == ("indicator_output_overlap",)
    assert _compare(cand, {"asim": plugins["asim"]}).warnings == ("indicator_similar",)


def test_new_gate_codes_are_known_to_notice_dictionary():
    from agentic_fx import improve_notice_codes as codes
    for code in gate.REJECT_CODES:
        assert codes.notice_for_plugin_gate(code).reason_code == code
