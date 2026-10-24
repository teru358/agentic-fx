"""仮説族 key の生成の一本化 (`family_key_from_parts`)、字句文法と再生成照合
による既知族の判定、meta 由来と `read_identity` 由来の key の一致。"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agentic_fx.loops.improve_context import deployed_families
from agentic_fx.plugin.loader import discover_one_with_reason
from agentic_fx.plugin.logic_identity import (
    ParsedFamilyKey, checked_family_key, family_key_from_parts,
    family_key_of, parse_family_key, read_identity)

EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples" / "plugins"
PAIRS = {"USDJPY", "EURUSD"}


# --- 生成: 保存値と byte 互換 ---------------------------------------------

@pytest.mark.parametrize("config,expected", [
    ("kind: indicator\ntimeframe: 4h\n", "indicator:x@4h"),
    ("kind: indicator\n", "indicator:x@1h"),
    ("kind: strategy\ntimeframe: 1h\npairs: [EURUSD, USDJPY]\n"
     "indicators:\n  a: {plugin: rsi}\n  b: {plugin: bollinger}\n"
     "  c: {plugin: rsi}\n", "strategy:bollinger,rsi@1h@EURUSD,USDJPY"),
    ("kind: strategy\ntimeframe: 15m\npairs: [USDJPY, EURUSD, USDJPY]\n"
     "indicators:\n  a: {plugin: zeta}\n  b: {plugin: alpha}\n",
     "strategy:alpha,zeta@15m@EURUSD,USDJPY"),
])
def test_stored_key_bytes_are_unchanged(config, expected):
    assert family_key_of(config, name="x") == expected


def test_explicit_null_timeframe_is_the_default_timeframe():
    assert family_key_of("kind: indicator\ntimeframe: null\n",
                         name="x") == "indicator:x@1h"
    assert family_key_from_parts("indicator", "x", None, (), ()) == (
        "indicator:x@1h")


def test_parts_sorts_and_dedupes_and_has_no_key_without_dependencies():
    assert family_key_from_parts(
        "strategy", "s", "1h", ["USDJPY", "EURUSD", "USDJPY"],
        ["b", "a", "b"]) == "strategy:a,b@1h@EURUSD,USDJPY"
    assert family_key_from_parts("strategy", "s", "1h", ["USDJPY"], []) is None
    assert family_key_from_parts("signal", "s", "1h", ["USDJPY"], ["a"]) is None


# --- 分解: 字句文法 + 所属 + 再生成の byte 一致 --------------------------------

def _parse(key, pairs=PAIRS):
    return parse_family_key(key, allowed_pairs=pairs)


def test_valid_keys_parse_to_their_parts():
    assert _parse("indicator:rsi@4h") == ParsedFamilyKey(
        "indicator", ("rsi",), "4h", ())
    assert _parse("strategy:rsi,sma@1h@EURUSD,USDJPY") == ParsedFamilyKey(
        "strategy", ("rsi", "sma"), "1h", ("EURUSD", "USDJPY"))


_REJECTED = [
    None, 7, b"indicator:rsi@1h", "", "rsi@1h", "other:rsi@1h",
    "indicator:rsi", "indicator:rsi@1h@USDJPY", "indicator:rsi@2h",
    "indicator:rsi@None", "indicator:Rsi@1h", "indicator:1rsi@1h",
    "indicator:" + "a" * 65 + "@1h", "indicator:rsi\n@1h",
    "indicator:rsi@1h\n", "indicator:@1h",
    "strategy:rsi@1h", "strategy:rsi@1h@", "strategy:rsi@None@USDJPY",
    "strategy:@1h@USDJPY", "strategy:rsi,@1h@USDJPY",
    "strategy:rsi@1h@USDJPY@EURUSD", "strategy:rsi@1h@usdjpy",
    "strategy:rsi@1h@USDJP", "strategy:rsi@1h@USDJPYX",
    "strategy:rsi@1h@GBPJPY",          # 字句は通るが settings.pairs に無い
    "strategy:rsi@1h@USDJPY\nEURUSD", "strategy:rsi@1h@USDJPY EURUSD",
    "strategy:rsi@1h@USDJPY|EURUSD", "strategy:rsi@1h@円円円円円円",
    "strategy:rsi@1h@123456",
    # 直接検査は通るが、再生成値と一致しない
    "strategy:sma,rsi@1h@USDJPY", "strategy:rsi,rsi@1h@USDJPY",
    "strategy:rsi@1h@USDJPY,EURUSD", "strategy:rsi@1h@USDJPY,USDJPY",
]


@pytest.mark.parametrize("key", _REJECTED)
def test_rejected_keys_are_not_known_families(key):
    assert _parse(key) is None


def test_membership_alone_does_not_make_free_text_a_pair():
    free = {"USDJPY", "EURUSD", "USDJPY| pf1.42 を優先", "A,B", "A", "B"}
    for key in ("strategy:rsi@1h@USDJPY| pf1.42 を優先",
                "strategy:rsi@1h@A,B", "strategy:rsi@1h@A"):
        assert _parse(key, free) is None


def test_ordering_and_duplicates_are_decided_only_by_regeneration():
    # name / pair の字句と所属はすべて満たすので、落とすのは再生成の不一致だけ
    assert _parse("strategy:rsi,sma@1h@EURUSD,USDJPY") is not None
    for key in ("strategy:sma,rsi@1h@USDJPY", "strategy:rsi,rsi@1h@USDJPY",
                "strategy:rsi@1h@USDJPY,EURUSD"):
        assert _parse(key) is None


def test_key_longer_than_512_bytes_is_rejected_even_if_otherwise_valid():
    pairs = {f"{chr(65 + i // 26)}{chr(65 + i % 26)}CDEF" for i in range(80)}
    ok = "strategy:rsi@1h@" + ",".join(sorted(pairs)[:50])
    assert len(ok.encode()) <= 512
    assert _parse(ok, pairs) is not None
    too_long = "strategy:rsi@1h@" + ",".join(sorted(pairs)[:80])
    assert len(too_long.encode()) > 512
    assert _parse(too_long, pairs) is None


# --- 配備側: 要素を先に検査し、生成した文字列を分解し直さない --------------------

def test_deployed_key_checks_elements_before_joining():
    kwargs = {"allowed_pairs": {"USDJPY", "EURUSD", "A,B", "A", "B"}}
    assert checked_family_key("strategy", "s", "1h", ["USDJPY"], ["rsi"],
                              **kwargs) == "strategy:rsi@1h@USDJPY"
    assert checked_family_key("strategy", "s", "1h", ["A,B"], ["rsi"],
                              **kwargs) is None
    assert checked_family_key("strategy", "s", "1h", ["USDJPY"], ["Rsi"],
                              **kwargs) is None
    assert checked_family_key("strategy", "s", "2h", ["USDJPY"], ["rsi"],
                              **kwargs) is None
    assert checked_family_key("strategy", "s", "1h", [], ["rsi"],
                              **kwargs) is None
    assert checked_family_key("indicator", "rsi", None, [], [],
                              **kwargs) == "indicator:rsi@1h"
    assert checked_family_key("indicator", "Rsi", "1h", [], [],
                              **kwargs) is None


# --- meta 由来と read_identity 由来の key の一致 ---------------------------------

_CASES = {
    "ind_default": ("indicator", "kind: indicator\n"),
    "ind_null_tf": ("indicator", "kind: indicator\ntimeframe: null\n"),
    "ind_4h": ("indicator", "kind: indicator\ntimeframe: 4h\n"),
    "strat_unordered": ("strategy", (
        "kind: strategy\ntimeframe: 1h\npairs: [USDJPY, EURUSD, USDJPY]\n"
        "exit_mode: levels\nmax_bars: 200\nindicators:\n"
        "  a: {plugin: zeta_ind}\n  b: {plugin: alpha_ind}\n"
        "  c: {plugin: zeta_ind}\n")),
    "strat_no_dependency": ("strategy", (
        "kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\n"
        "exit_mode: levels\nmax_bars: 200\n")),
}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_meta_key_equals_read_identity_key(tmp_path, case):
    kind, config = _CASES[case]
    source = EXAMPLES / ("rsi" if kind == "indicator" else "rsi_pullback")
    plugin_dir = tmp_path / "cand"
    shutil.copytree(source, plugin_dir)
    (plugin_dir / "config.yaml").write_text(config)
    meta, reason = discover_one_with_reason(plugin_dir, "cand")
    assert meta is not None, reason
    family = deployed_families([meta])[0]
    from_meta = checked_family_key(
        family["kind"], family["name"], family["timeframe"], family["pairs"],
        family["indicators"], allowed_pairs=PAIRS)
    _, from_identity, _ = read_identity(plugin_dir, name="cand")
    assert from_meta == from_identity
    assert (from_identity is None) == (case == "strat_no_dependency")
