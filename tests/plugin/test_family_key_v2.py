"""Family identity for strategies that read other symbols: grammar, escaping, limits."""
from __future__ import annotations

from dataclasses import replace

import pytest

from agentic_fx.plugin.logic_identity import (
    FAMILY_KEY_MAX_BYTES, family_key_of, parse_family_key, reference_family_key,
)
from agentic_fx.plugin.resolve import IndicatorResolutionError, resolve_indicator_deps
from tests.backtest.factories import SETTINGS
from tests.plugin.test_resolve import _indicator, _inv, _plugin, _STRATEGY_PY

PAIRS = {"USDJPY", "EURUSD"}


def _config(deps, *, tf="1h", pairs="[USDJPY]"):
    lines = "".join(f"  {alias}: {{plugin: {plugin}, symbol: '{symbol}', "
                    f"timeframe: {ref_tf}}}\n" for alias, plugin, symbol, ref_tf in deps)
    return f"kind: strategy\ntimeframe: {tf}\npairs: {pairs}\nindicators:\n{lines}"


def _parse(key):
    return parse_family_key(key, allowed_pairs=PAIRS)


def test_literal_symbols_and_aliases_and_timeframes_are_all_part_of_the_identity():
    base = family_key_of(_config([("a", "ema", "JP225", "1h")]), name="x")
    assert family_key_of(_config([("a", "ema", "US500", "1h")]), name="x") != base
    assert family_key_of(_config([("b", "ema", "JP225", "1h")]), name="x") != base
    assert family_key_of(_config([("a", "ema", "JP225", "4h")]), name="x") != base
    assert family_key_of(_config([("a", "rsi", "JP225", "1h")]), name="x") != base
    two = [("a", "ema", "JP225", "1h"), ("b", "ema", "US500", "1h")]
    assert (family_key_of(_config(two), name="x")
            != family_key_of(_config(list(reversed(two))), name="x"))


def test_a_strategy_without_a_literal_symbol_keeps_the_legacy_key_byte_for_byte():
    config = ("kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\nindicators:\n"
              "  a: {plugin: ema}\n  b: {plugin: rsi, symbol: self, timeframe: 4h}\n")
    assert family_key_of(config, name="x") == "strategy:ema,rsi@1h@USDJPY"


def test_pairs_order_does_not_change_the_v2_identity():
    one = family_key_of(_config([("a", "ema", "JP225", "1h")], pairs="[USDJPY, EURUSD]"), name="x")
    two = family_key_of(_config([("a", "ema", "JP225", "1h")], pairs="[EURUSD, USDJPY]"), name="x")
    assert one == two and "@EURUSD,USDJPY@" in one


@pytest.mark.parametrize("symbol", [
    "JP225", "US500.cash", "A@B", "A;B", "A%B", "A,B", "日経225", "ÜS500.m", "X" * 32,
    "%41", "A%2", "a b"])
def test_special_symbols_round_trip_through_the_utf8_percent_escape(symbol):
    key = family_key_of(_config([("a", "ema", symbol, "4h")]), name="x")
    parsed = _parse(key)
    assert parsed is not None and parsed.version == "v2"
    assert parsed.dependencies[0].literal_symbol == symbol
    assert parsed.dependencies[0].timeframe == "4h"
    # the key itself is plain ASCII with fixed separators only
    assert key.isascii() and key.count("@") == 2


def test_the_escape_uses_uppercase_hex_and_keeps_only_unreserved_characters():
    key = family_key_of(_config([("a", "ema", "é;~-._", "1h")]), name="x")
    assert "%C3%A9%3B~-._" in key


def test_eight_dependencies_with_escapes_round_trip_in_declared_order():
    deps = [(f"d{i}", "ema", f"S{i};%@日", ["1h", "4h"][i % 2]) for i in range(8)]
    key = family_key_of(_config(deps, tf="4h"), name="x")
    parsed = _parse(key)
    assert [d.alias for d in parsed.dependencies] == [f"d{i}" for i in range(8)]
    assert [d.literal_symbol for d in parsed.dependencies] == [d[2] for d in deps]
    assert [d.timeframe for d in parsed.dependencies] == [d[3] for d in deps]
    assert parsed.timeframe == "4h"


@pytest.mark.parametrize("key", [
    "strategy-v2:1h@USDJPY@a;ema;literal;A%2fB;1h",      # lower-case hex
    "strategy-v2:1h@USDJPY@a;ema;literal;A%41;1h",       # over-escaped safe character
    "strategy-v2:1h@USDJPY@a;ema;literal;%ZZ;1h",        # not hex
    "strategy-v2:1h@USDJPY@a;ema;literal;A%4;1h",        # truncated escape
    "strategy-v2:1h@USDJPY@a;ema;literal;A%;1h",
    "strategy-v2:1h@USDJPY@a;ema;literal;A%C3;1h",       # invalid UTF-8
    "strategy-v2:1h@USDJPY@a;ema;literal;é;1h",          # raw non-ASCII
    "strategy-v2:1h@USDJPY@a;ema;literal;A B;1h",        # raw space
    "strategy-v2:1h@USDJPY@a;ema;literal;-;1h",          # literal without symbol
    "strategy-v2:1h@USDJPY@a;ema;self;JP225;1h",         # self with a symbol
    "strategy-v2:1h@USDJPY@a;ema;both;JP225;1h",         # unknown symbol mode
    "strategy-v2:1h@USDJPY@a;ema;literal;JP225;2h",      # unknown timeframe
    "strategy-v2:1h@USDJPY@a;ema;literal;JP225",         # missing field
    "strategy-v2:1h@USDJPY@a;ema;literal;JP225;1h;x",    # extra field
    "strategy-v2:1h@USDJPY@",                            # no dependencies
    "strategy-v2:1h@GBPJPY@a;ema;literal;JP225;1h",      # pair not configured
    "strategy-v2:1h@USDJPY,EURUSD@a;ema;literal;JP225;1h",   # pairs not canonical order
    "strategy-v2:1h@USDJPY",                             # missing part
    "strategy-v2:1h@USDJPY@a;ema;literal;JP225;1h@x",    # extra part
])
def test_the_decoder_rejects_every_non_canonical_key(key):
    assert _parse(key) is None


def test_the_legacy_grammar_is_still_read_and_is_not_the_v2_family():
    legacy = _parse("strategy:ema@1h@USDJPY")
    assert legacy is not None and legacy.version == "legacy" and legacy.dependencies == ()
    v2 = family_key_of(_config([("a", "ema", "JP225", "1h")]), name="x")
    assert v2 != "strategy:ema@1h@USDJPY" and v2.startswith("strategy-v2:")
    assert _parse("strategy:ema@1h@USDJPY").names == ("ema",)


def _tuned_config(extra: int):
    """Eight literal dependencies whose family key is exactly `512 + extra` bytes."""
    sizes = [2] * 8
    deps = lambda: [(("a%d" % i).ljust(sizes[i], "x"), "ema", "N" * 20, "1h")
                    for i in range(8)]
    while len(family_key_of(_config(deps()), name="x") or "x" * 600) < 512 + extra:
        sizes[sizes.index(min(sizes))] += 1
        if max(sizes) > 32:
            raise AssertionError("cannot reach the boundary")
    return _config(deps())


def test_the_family_key_limit_is_512_bytes():
    assert FAMILY_KEY_MAX_BYTES == 512


def test_a_512_byte_key_is_accepted_and_513_is_refused_by_the_key_builder():
    exact = family_key_of(_tuned_config(0), name="x")
    assert len(exact.encode()) == 512
    assert _parse(exact) is not None
    over = _tuned_config(1)
    assert family_key_of(over, name="x") is None
    parsed = _parse(exact)
    longer = reference_family_key(
        parsed.timeframe, parsed.pairs,
        [replace(parsed.dependencies[0], alias=parsed.dependencies[0].alias + "x"),
         *parsed.dependencies[1:]])
    assert len(longer.encode()) == 513
    assert _parse(longer) is None


def _resolve(tmp_path, config):
    root = tmp_path / "plugins"
    ema = _indicator(root, "ema", max_bars="max_bars: 500\n")
    strategy = _plugin(tmp_path / "candidate", "s", config, _STRATEGY_PY)
    settings = SETTINGS.model_copy(update={
        "datafeed": SETTINGS.datafeed.model_copy(update={"watch_symbols": ["N" * 20]})})
    return resolve_indicator_deps(strategy, _inv(root, ema), settings=settings,
                                  pin_mode="ignore"), strategy


def test_the_resolver_passes_512_bytes_and_refuses_513_with_a_fixed_reason(tmp_path):
    head = "exit_mode: levels\nmax_bars: 200\n"
    exact = _tuned_config(0).replace("indicators:", head + "indicators:", 1)
    resolved, strategy = _resolve(tmp_path / "ok", exact)
    assert len(resolved.items) == 8
    assert len(family_key_of(exact, name="s").encode()) == 512
    over = _tuned_config(1).replace("indicators:", head + "indicators:", 1)
    with pytest.raises(IndicatorResolutionError) as error:
        _resolve(tmp_path / "over", over)
    assert error.value.reason == "family_key_too_long"


def test_the_resolver_and_the_config_derive_the_same_key_for_the_same_strategy(tmp_path):
    from agentic_fx.plugin.logic_identity import ParsedDependency, reference_family_key

    config = ("exit_mode: levels\nmax_bars: 200\n"
              + _config([("jp", "ema", "N" * 20, "1h")], pairs="[USDJPY]"))
    resolved, strategy = _resolve(tmp_path, config)
    item = resolved.items[0]
    from_resolver = reference_family_key(
        strategy.timeframe, strategy.pairs,
        [ParsedDependency(item.alias, item.plugin_name, item.symbol_mode,
                          item.literal_symbol, item.timeframe)])
    assert from_resolver == family_key_of(config, name="s")
