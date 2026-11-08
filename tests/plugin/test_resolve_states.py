"""resolve_indicator_deps の判定を、例外を投げずに alias ごとに列挙する関数の test。"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentic_fx.plugin import resolve
from agentic_fx.plugin.loader import PluginMeta, discover_one_with_reason
from agentic_fx.plugin.resolve import (
    ApprovedInventory, IndicatorResolutionError, enumerate_indicator_states,
    resolve_indicator_deps,
)
from tests.backtest.factories import SETTINGS

_TEST_PY = "def test_placeholder():\n    pass\n"
_INDICATOR_PY = "def compute(df, params):\n    return {}\n"
_STRATEGY_PY = ("def evaluate(df, indicators, signals, params):\n"
                "    return {'action': 'hold', 'rationale': 'x'}\n")
_STRATEGY_HEAD = ("kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\n"
                  "exit_mode: levels\nmax_bars: 200\n")


def _plugin(base: Path, name: str, config_yaml: str, plugin_py: str) -> PluginMeta:
    directory = base / name
    directory.mkdir(parents=True)
    (directory / "plugin.py").write_text(plugin_py)
    (directory / "config.yaml").write_text(config_yaml)
    (directory / "test_plugin.py").write_text(_TEST_PY)
    meta, reason = discover_one_with_reason(directory, name)
    assert reason is None, reason
    return meta


def _indicator(base: Path, name: str, *, extra: str = "") -> PluginMeta:
    return _plugin(base, name, "kind: indicator\noutputs: [v]\nparams:\n  period: 14\n" + extra,
                   _INDICATOR_PY)


def _strategy(base: Path, name: str, indicators: str) -> PluginMeta:
    return _plugin(base, name, _STRATEGY_HEAD + indicators, _STRATEGY_PY)


def _inventory(root: Path, *metas: PluginMeta) -> ApprovedInventory:
    return ApprovedInventory(root=root.resolve(), metas=tuple(metas))


def _states(meta, inventory, settings=SETTINGS):
    result = enumerate_indicator_states(meta, inventory, settings=settings)
    return {item.alias: item.state for item in result.aliases}, result.set_state


def test_every_alias_gets_a_state_and_a_good_pin_is_a_match(tmp_path):
    root = tmp_path / "plugins"
    rsi, adx = _indicator(root, "rsi"), _indicator(root, "adx")
    strategy = _strategy(
        tmp_path / "c", "s",
        f"indicators:\n  fast: {{plugin: rsi, pin: '{rsi.content_hash}'}}\n"
        f"  slow: {{plugin: adx, pin: '{'a' * 64}'}}\n"
        "  loose: {plugin: adx}\n  gone: {plugin: macd, pin: '" + "b" * 64 + "'}\n")
    states, set_state = _states(strategy, _inventory(root, rsi, adx))
    assert states == {"fast": "match", "slow": "pin_mismatch", "loose": "unpinned",
                      "gone": "not_found"}
    assert set_state is None
    result = enumerate_indicator_states(strategy, _inventory(root, rsi, adx), settings=SETTINGS)
    assert [item.alias for item in result.aliases] == ["fast", "slow", "loose", "gone"]
    assert {item.alias: (item.plugin, item.pin) for item in result.aliases}["slow"] == (
        "adx", "a" * 64)


def test_a_non_indicator_dependency_is_not_indicator(tmp_path):
    root = tmp_path / "plugins"
    other = _strategy(root, "other", "")
    strategy = _strategy(tmp_path / "c", "s",
                         f"indicators:\n  x: {{plugin: other, pin: '{other.content_hash}'}}\n")
    assert _states(strategy, _inventory(root, other))[0] == {"x": "not_indicator"}


def test_it_never_raises_and_every_state_is_in_the_closed_vocabulary(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    rsi = _indicator(root, "rsi")
    strategy = _strategy(tmp_path / "c", "s",
                         f"indicators:\n  a: {{plugin: rsi, pin: '{rsi.content_hash}'}}\n")

    def boom(*args, **kwargs):
        raise RuntimeError("a path /secret/location")

    monkeypatch.setattr(resolve, "resolve_indicator_deps", boom)
    states, set_state = _states(strategy, _inventory(root, rsi))
    assert states == {"a": "unknown"} and set_state is None


def test_a_reason_outside_the_vocabulary_is_unknown_never_a_match(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    rsi = _indicator(root, "rsi")
    strategy = _strategy(tmp_path / "c", "s",
                         f"indicators:\n  a: {{plugin: rsi, pin: '{rsi.content_hash}'}}\n")

    def odd(*args, **kwargs):
        raise IndicatorResolutionError("a", "some_future_reason")

    monkeypatch.setattr(resolve, "resolve_indicator_deps", odd)
    assert _states(strategy, _inventory(root, rsi))[0] == {"a": "unknown"}


def test_set_level_failure_is_reported_apart_from_the_aliases(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    rsi = _indicator(root, "rsi")
    body = "indicators:\n" + "".join(
        f"  a{i}: {{plugin: rsi, pin: '{rsi.content_hash}'}}\n" for i in range(3))
    strategy = _strategy(tmp_path / "c", "s", body)
    inventory = _inventory(root, rsi)
    import json
    one_size = len(json.dumps(resolve_indicator_deps(_one(strategy), inventory, settings=SETTINGS,
                                                     pin_mode="require").handshake_items(),
                              separators=(",", ":"), sort_keys=True,
                              ensure_ascii=False).encode("utf-8"))
    monkeypatch.setattr(resolve, "MAX_HANDSHAKE_BYTES", one_size + 10)
    states, set_state = _states(strategy, inventory)
    assert states == {"a0": "match", "a1": "match", "a2": "match"}
    assert set_state == "handshake_too_large"


def _one(meta):
    import dataclasses
    return dataclasses.replace(meta, indicators=meta.indicators[:1])


def test_a_strategy_without_dependencies_has_no_states(tmp_path):
    strategy = _strategy(tmp_path / "c", "s", "")
    result = enumerate_indicator_states(strategy, _inventory(tmp_path), settings=SETTINGS)
    assert result.aliases == () and result.set_state is None


def test_the_enumeration_agrees_with_the_resolver_on_who_fails_first(tmp_path):
    root = tmp_path / "plugins"
    rsi = _indicator(root, "rsi")
    strategy = _strategy(
        tmp_path / "c", "s",
        f"indicators:\n  a: {{plugin: rsi, pin: '{rsi.content_hash}'}}\n"
        "  b: {plugin: rsi}\n")
    inventory = _inventory(root, rsi)
    with pytest.raises(IndicatorResolutionError) as error:
        resolve_indicator_deps(strategy, inventory, settings=SETTINGS, pin_mode="require")
    states, _ = _states(strategy, inventory)
    assert states[error.value.alias] == error.value.reason


def test_the_resolver_still_raises_the_same_way(tmp_path):
    root = tmp_path / "plugins"
    rsi = _indicator(root, "rsi")
    strategy = _strategy(tmp_path / "c", "s", "indicators:\n  a: {plugin: rsi}\n")
    with pytest.raises(IndicatorResolutionError) as error:
        resolve_indicator_deps(strategy, _inventory(root, rsi), settings=SETTINGS,
                               pin_mode="require")
    assert (error.value.alias, error.value.reason, str(error.value)) == (
        "a", "unpinned", "indicator_unresolved:a:unpinned")
