"""[indicator-consumption-wiring] T2 V3: 解決後の差し替え拒否と root containment。

実 subprocess を起動する経路だが、`__enter__` の検査は Popen より**前**に
走るため worker は 1 つも起動しない (spawn spy で確認する)。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from agentic_fx.config import load_settings
from agentic_fx.plugin.loader import PluginMeta, content_hash
from agentic_fx.plugin.resolve import (
    ResolvedIndicator, ResolvedIndicatorSet, freeze_params,
)
from agentic_fx.plugin.sandbox import PluginSession, SandboxError

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
_STRATEGY_PY = ("def evaluate(df, indicators, signals, params):\n"
                "    return {'action': 'hold', 'rationale': 'x'}\n")
_INDICATOR_PY = ("import pandas as pd\n"
                 "def compute(df, params):\n"
                 "    return {'v': pd.Series([1.0] * len(df), index=df.index)}\n")


@pytest.fixture(scope="module")
def plugin_settings():
    return load_settings(EXAMPLE).plugin


def _dir(base, name, plugin_py, config):
    d = base / name
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(plugin_py)
    (d / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    (d / "test_plugin.py").write_text("def test_placeholder():\n    pass\n")
    return d


def _strategy_meta(base):
    d = _dir(base, "s", _STRATEGY_PY,
             {"kind": "strategy", "timeframe": "1h", "pairs": ["USDJPY"],
              "exit_mode": "levels", "max_bars": 200})
    return PluginMeta(name="s", kind="strategy", path=d, params={},
                      timeframe="1h", pairs=("USDJPY",), max_bars=200,
                      content_hash=content_hash(d))


def _rset(root, ind_dir):
    return ResolvedIndicatorSet(
        inventory_root=root.resolve(),
        items=(ResolvedIndicator(
            alias="v", plugin_name=ind_dir.name, plugin_py=ind_dir / "plugin.py",
            content_hash=content_hash(ind_dir), params=freeze_params({}),
            max_bars=200, outputs=("v",), pinned=True),),
        all_pinned=True)


def _no_spawn(monkeypatch):
    calls = []
    real = subprocess.Popen

    def _spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _spy)
    return calls


def test_enter_rejects_indicator_content_change(tmp_path, plugin_settings,
                                                monkeypatch):
    root = tmp_path / "plugins"
    ind = _dir(root, "ind", _INDICATOR_PY,
               {"kind": "indicator", "outputs": ["v"]})
    meta = _strategy_meta(tmp_path / "cand")
    resolved = _rset(root, ind)
    (ind / "plugin.py").write_text(_INDICATOR_PY + "\n# tampered\n")
    calls = _no_spawn(monkeypatch)
    with pytest.raises(SandboxError, match="hash"):
        PluginSession(meta, settings=plugin_settings,
                      resolved=resolved).__enter__()
    assert calls == []


@pytest.mark.parametrize("root_name", ["live", "snapshot", "human"])
def test_enter_rejects_indicator_outside_inventory_root(tmp_path, plugin_settings,
                                                        monkeypatch, root_name):
    root = tmp_path / root_name / "plugins"
    root.mkdir(parents=True)
    outside = _dir(tmp_path / "elsewhere", "ind", _INDICATOR_PY,
                   {"kind": "indicator", "outputs": ["v"]})
    meta = _strategy_meta(tmp_path / "cand")
    resolved = _rset(root, outside)
    calls = _no_spawn(monkeypatch)
    with pytest.raises(SandboxError, match="inventory_root"):
        PluginSession(meta, settings=plugin_settings,
                      resolved=resolved).__enter__()
    assert calls == []


def test_enter_rejects_symlinked_indicator_escaping_root(tmp_path,
                                                         plugin_settings,
                                                         monkeypatch):
    root = tmp_path / "plugins"
    root.mkdir()
    outside = _dir(tmp_path / "elsewhere", "ind", _INDICATOR_PY,
                   {"kind": "indicator", "outputs": ["v"]})
    (root / "ind").symlink_to(outside)
    meta = _strategy_meta(tmp_path / "cand")
    resolved = ResolvedIndicatorSet(
        inventory_root=root.resolve(),
        items=(ResolvedIndicator(
            alias="v", plugin_name="ind", plugin_py=root / "ind" / "plugin.py",
            content_hash=content_hash(outside), params=freeze_params({}),
            max_bars=200, outputs=("v",), pinned=True),),
        all_pinned=True)
    calls = _no_spawn(monkeypatch)
    with pytest.raises(SandboxError, match="inventory_root"):
        PluginSession(meta, settings=plugin_settings,
                      resolved=resolved).__enter__()
    assert calls == []


def test_enter_rejects_indicator_failing_check_source(tmp_path, plugin_settings,
                                                      monkeypatch):
    root = tmp_path / "plugins"
    ind = _dir(root, "ind", "def compute(df, params):\n    return eval('{}')\n",
               {"kind": "indicator", "outputs": ["v"]})
    meta = _strategy_meta(tmp_path / "cand")
    resolved = _rset(root, ind)
    calls = _no_spawn(monkeypatch)
    with pytest.raises(SandboxError, match="eval"):
        PluginSession(meta, settings=plugin_settings,
                      resolved=resolved).__enter__()
    assert calls == []


def test_strategy_session_without_resolved_is_rejected(tmp_path, plugin_settings,
                                                       monkeypatch):
    meta = _strategy_meta(tmp_path / "cand")
    calls = _no_spawn(monkeypatch)
    with pytest.raises(SandboxError, match="resolved"):
        PluginSession(meta, settings=plugin_settings).__enter__()
    assert calls == []


def test_indicator_session_with_resolved_is_rejected(tmp_path, plugin_settings,
                                                      monkeypatch):
    """codex plan r2 束1 Minor: 契約は片方向だけでは不十分 — strategy は
    `resolved` 必須 (上のテスト) だが、indicator/signal は `resolved` を
    **受け取ってはいけない**。誤って渡すと依存情報が indicator/signal の
    handshake に混入し得るので `SandboxError` で拒否する。"""
    root = tmp_path / "plugins"
    ind = _dir(root, "ind", _INDICATOR_PY, {"kind": "indicator", "outputs": ["v"]})
    ind_dep = _dir(root, "dep", _INDICATOR_PY, {"kind": "indicator", "outputs": ["v"]})
    meta = PluginMeta(name="ind", kind="indicator", path=ind, params={},
                      timeframe=None, pairs=(), max_bars=200,
                      content_hash=content_hash(ind), outputs=("v",))
    resolved = _rset(root, ind_dep)   # indicator に誤って resolved を渡す
    calls = _no_spawn(monkeypatch)
    with pytest.raises(SandboxError, match="resolved"):
        PluginSession(meta, settings=plugin_settings,
                      resolved=resolved).__enter__()
    assert calls == []
