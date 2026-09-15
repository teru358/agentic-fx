"""[indicator-consumption-wiring] plugin/resolve.py の単体テスト (R1)。

実 subprocess は使わない — resolver は純粋にファイル読取と辞書操作のみ。
DB は使わない (approved_plugins 二相のテストは tests/tools/test_plugin_loader.py)。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentic_fx.plugin import resolve
from agentic_fx.plugin.loader import PluginMeta, content_hash, discover_one_with_reason
from agentic_fx.plugin.resolve import (
    ApprovedInventory, IndicatorResolutionError, ResolvedIndicatorSet,
    freeze_params, resolve_indicator_deps, thaw,
)

from tests.backtest.factories import SETTINGS

_TEST_PY = "def test_placeholder():\n    pass\n"
_INDICATOR_PY = "def compute(df, params):\n    return {}\n"
_STRATEGY_PY = ("def evaluate(df, indicators, signals, params):\n"
                "    return {'action': 'hold', 'rationale': 'x'}\n")


def _plugin(base: Path, name: str, config_yaml: str, plugin_py: str) -> PluginMeta:
    d = base / name
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(plugin_py)
    (d / "config.yaml").write_text(config_yaml)
    (d / "test_plugin.py").write_text(_TEST_PY)
    meta, reason = discover_one_with_reason(d, name)
    assert reason is None, reason
    return meta


def _indicator(base: Path, name: str, *, outputs: str = "outputs: [v]\n",
               params: str = "params:\n  period: 14\n",
               max_bars: str = "") -> PluginMeta:
    return _plugin(base, name, "kind: indicator\n" + outputs + params + max_bars,
                   _INDICATOR_PY)


def _strategy(base: Path, name: str, body: str) -> PluginMeta:
    head = ("kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\n"
            "exit_mode: levels\nmax_bars: 200\n")
    return _plugin(base, name, head + body, _STRATEGY_PY)


def test_freeze_then_thaw_roundtrips_and_is_independent():
    src = {"a": [1, {"b": 2}], "c": None, "d": True}
    frozen = freeze_params(src)
    first = thaw(frozen)
    second = thaw(frozen)
    assert first == src
    assert second == src
    first["a"][1]["b"] = 999
    assert second["a"][1]["b"] == 2      # call ごとに完全独立 (deep)
    assert thaw(frozen)["a"][1]["b"] == 2


def test_frozen_params_is_hashable():
    hash(freeze_params({"a": [1, 2], "b": {"c": 3}}))


def test_empty_set_has_no_items_and_is_all_pinned():
    empty = ResolvedIndicatorSet.empty(Path("/plugins"))
    assert empty.items == ()
    assert empty.all_pinned is True
    assert empty.pin_object() == {}
    assert empty.handshake_items() == []


def test_pin_object_is_alias_sorted_plain_json(tmp_path):
    root = tmp_path / "plugins"
    rsi = _indicator(root, "rsi")
    adx = _indicator(root, "adx")
    inv = ApprovedInventory(root=root.resolve(), metas=(rsi, adx))
    s = _strategy(tmp_path / "cand", "s",
                  "indicators:\n"
                  "  zeta: {plugin: rsi, params: {period: 21}}\n"
                  "  alpha: {plugin: adx}\n")
    resolved = resolve_indicator_deps(s, inv, settings=SETTINGS, pin_mode="ignore")
    assert [i.alias for i in resolved.items] == ["alpha", "zeta"]   # alias 昇順
    obj = resolved.pin_object()
    assert list(obj) == ["alpha", "zeta"]
    assert obj["zeta"] == {"plugin": "rsi", "content_hash": rsi.content_hash,
                           "params": {"period": 21}}
    assert obj["alpha"] == {"plugin": "adx", "content_hash": adx.content_hash,
                            "params": {"period": 14}}
    import json
    json.dumps(obj)  # plain JSON であること


def test_indicator_resolution_error_str_is_fixed_text():
    exc = IndicatorResolutionError("rsi", "pin_mismatch")
    assert exc.alias == "rsi" and exc.reason == "pin_mismatch"
    assert str(exc) == "indicator_unresolved:rsi:pin_mismatch"
    assert str(IndicatorResolutionError(None, "handshake_too_large")) == \
        "indicator_unresolved:-:handshake_too_large"
