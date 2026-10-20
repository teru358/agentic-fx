"""ロジック hash と仮説族 key の最小実装。"""
from __future__ import annotations

from pathlib import Path

from agentic_fx.plugin.logic_identity import (
    LOGIC_HASH_PREFIX, family_key_of, identity_of_dir, logic_hash_of)

EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples" / "plugins"
CONFIG = "kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\nparams: {x: 1}\n"

BASE = '''
"""説明"""
def evaluate(df, indicators, signals, params):
    if df["close"].iloc[-1] > 100:
        return {"action": "buy", "rationale": "above", "sl": 30}
    return {"action": "hold", "rationale": "flat"}
'''


def test_numeric_literals_docstrings_and_rationale_do_not_change_the_hash():
    changed = BASE.replace("100", "150").replace("30", "45").replace(
        "説明", "別の説明").replace('"above"', '"さらに別の理由"')
    assert logic_hash_of(BASE, CONFIG) == logic_hash_of(changed, CONFIG)


def test_signed_numeric_literals_are_dropped_like_unsigned_ones():
    negative = BASE.replace("> 100", "> -1").replace('"sl": 30', '"sl": -30')
    positive = BASE.replace("> 100", "> 2").replace('"sl": 30', '"sl": +7')
    assert logic_hash_of(negative, CONFIG) == logic_hash_of(positive, CONFIG)
    assert logic_hash_of(negative, CONFIG) == logic_hash_of(BASE, CONFIG)


def test_description_and_note_strings_do_not_change_the_hash():
    with_text = BASE.replace(
        '"rationale": "above"',
        '"rationale": "above", "description": "d1", "note": "n1"')
    other_text = BASE.replace(
        '"rationale": "above"',
        '"rationale": "above", "description": "d2", "note": "n2"')
    assert logic_hash_of(with_text, CONFIG) == logic_hash_of(other_text, CONFIG)


def test_hash_carries_the_algorithm_version_prefix():
    value = logic_hash_of(BASE, CONFIG)
    assert value.startswith(LOGIC_HASH_PREFIX) and LOGIC_HASH_PREFIX == "v1:"
    assert len(value) == len(LOGIC_HASH_PREFIX) + 64


def test_decisive_strings_and_structure_change_the_hash():
    assert logic_hash_of(BASE, CONFIG) != logic_hash_of(
        BASE.replace('"close"', '"open"'), CONFIG)
    assert logic_hash_of(BASE, CONFIG) != logic_hash_of(
        BASE.replace("return {\"action\": \"hold\"", "pass\n    return {\"action\": \"hold\""),
        CONFIG)


def test_config_values_do_not_but_config_keys_and_indicator_names_do():
    other_values = CONFIG.replace("x: 1", "x: 9")
    assert logic_hash_of(BASE, CONFIG) == logic_hash_of(BASE, other_values)
    with_key = CONFIG.replace("x: 1", "x: 1, y: 2")
    assert logic_hash_of(BASE, CONFIG) != logic_hash_of(BASE, with_key)
    dep_a = CONFIG + "indicators:\n  f: {plugin: rsi}\n"
    dep_b = CONFIG + "indicators:\n  f: {plugin: sma}\n"
    assert logic_hash_of(BASE, dep_a) != logic_hash_of(BASE, dep_b)


def test_unparsable_input_is_none():
    assert logic_hash_of("def (:", CONFIG) is None
    assert logic_hash_of(BASE, "- a\n- b\n") is None
    assert family_key_of(": : :", name="x") is None


def test_strategy_without_indicator_dependencies_has_no_family():
    assert family_key_of(CONFIG, name="sma_cross") is None
    _, family = identity_of_dir(EXAMPLES / "sma_cross", name="sma_cross")
    assert family is None


def test_strategy_family_is_indicator_set_timeframe_and_pairs():
    config = ("kind: strategy\ntimeframe: 1h\npairs: [EURUSD, USDJPY]\n"
              "indicators:\n  a: {plugin: rsi}\n  b: {plugin: bollinger}\n"
              "  c: {plugin: rsi}\n")
    assert family_key_of(config, name="x") == (
        "strategy:bollinger,rsi@1h@EURUSD,USDJPY")
