"""The human-supplied ``ohlcv`` example plugin and its approval path."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_fx.plugin.loader import content_hash, discover, discover_one_with_reason
from agentic_fx.plugin.resolve import IndicatorResolutionError, resolve_indicator_deps
from agentic_fx.setup.indicators import STANDARD_INDICATORS
from agentic_fx.tools import plugin_loader
from tests.tools.test_plugin_loader import _SETTINGS, _approve, _conn, _write_plugin

EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples" / "plugins"
OHLCV = EXAMPLES / "ohlcv"


def _copy(dest: Path) -> Path:
    shutil.copytree(OHLCV, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def test_the_examples_own_tests_pass_under_pytest(tmp_path):
    work = _copy(tmp_path / "ohlcv")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--basetemp", str(tmp_path / "bt"), str(work / "test_plugin.py")],
        cwd=work, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_example_is_a_three_file_indicator_declaring_the_ohlcv_outputs():
    assert sorted(p.name for p in OHLCV.iterdir() if p.name != "__pycache__") == [
        "config.yaml", "plugin.py", "test_plugin.py"]
    metas = {meta.name: meta for meta in discover(EXAMPLES)}
    assert metas["ohlcv"].kind == "indicator"
    assert metas["ohlcv"].outputs == ("open", "high", "low", "close", "volume")


def test_the_example_is_found_by_the_resolver_only_after_approval(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    ohlcv = _copy(plugins_dir / "ohlcv")
    strategy_dir = _write_plugin(
        tmp_path, "strategy",
        plugin_py=("def evaluate(df, indicators, signals, params):\n"
                   "    return {'action': 'hold', 'rationale': 'test'}\n"),
        config_yaml=("kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\n"
                     "exit_mode: levels\nindicators:\n  bars: {plugin: ohlcv}\n"))
    strategy, reason = discover_one_with_reason(strategy_dir, "strategy")
    assert reason is None
    conn = _conn(tmp_path)

    before = plugin_loader.approved_plugins(conn, plugins_dir, settings=_SETTINGS)
    with pytest.raises(IndicatorResolutionError, match="not_found"):
        resolve_indicator_deps(strategy, before.inventory, settings=_SETTINGS,
                               pin_mode="ignore")
    _approve(conn, "ohlcv", content_hash(ohlcv))
    after = plugin_loader.approved_plugins(conn, plugins_dir, settings=_SETTINGS)
    resolved = resolve_indicator_deps(strategy, after.inventory, settings=_SETTINGS,
                                      pin_mode="ignore")
    assert resolved.items[0].outputs == ("open", "high", "low", "close", "volume")


def test_the_wizard_standard_list_does_not_offer_ohlcv_yet():
    assert [name for name, _ in STANDARD_INDICATORS] == [
        "sma", "ema", "rsi", "macd", "bollinger", "atr", "adx", "stochastic", "ichimoku"]
