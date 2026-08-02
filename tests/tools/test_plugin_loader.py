"""plugin_loader.approved_plugins + market_tools.build の indicator plugin
合成テスト (プラン 7 Task 3)。

実 subprocess サンドボックス (Task 2 の対象) は使わない — `sandbox_run` は
すべて fake で差し替える。DB は tmp_path 上の sqlite のみ。実 HTTP/git/
乱数/実時計は使わない。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.sources import INTERVAL_MIN
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.sandbox import SandboxError
from agentic_fx.store import approvals
from agentic_fx.store.db import connect, init_db
from agentic_fx.tools import market_tools, plugin_loader
from agentic_fx.tools.registry import ToolRegistry

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

INDICATOR_PY = """
def compute(df, params):
    return {"custom": 1.0}
"""

INDICATOR_CONFIG = """
kind: indicator
max_bars: 50
"""

TEST_PY = """
def test_placeholder():
    pass
"""


def _conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    return c


def _write_plugin(base: Path, name: str, *, plugin_py: str = INDICATOR_PY,
                  config_yaml: str = INDICATOR_CONFIG) -> Path:
    d = base / name
    d.mkdir()
    (d / "plugin.py").write_text(plugin_py)
    (d / "config.yaml").write_text(config_yaml)
    (d / "test_plugin.py").write_text(TEST_PY)
    return d


def _approve(conn, name: str, content_hash: str) -> int:
    aid = approvals.create(conn, "plugin",
                           {"name": name, "content_hash": content_hash}, NOW)
    approvals.decide(conn, aid, status="approved", decided_by="shell", now=NOW)
    return aid


def _bars(n=120, interval="1h"):
    step = timedelta(minutes=INTERVAL_MIN[interval])
    return [Bar("USDJPY", interval, NOW - step * (n - i),
                148.0, 148.2, 147.8, 148.1, 100) for i in range(n)]


# ① 未承認は出ない -----------------------------------------------------

def test_approved_plugins_excludes_unapproved(tmp_path, caplog):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(plugins_dir, "unapproved_ind")
    conn = _conn(tmp_path)

    with caplog.at_level(logging.WARNING):
        metas = plugin_loader.approved_plugins(conn, plugins_dir)

    assert metas == []
    assert "unapproved_ind" in caplog.text


# ② 承認 + hash 一致は出る ----------------------------------------------

def test_approved_plugins_includes_approved_hash_match(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    d = _write_plugin(plugins_dir, "good_ind")
    conn = _conn(tmp_path)
    from agentic_fx.plugin.loader import content_hash
    _approve(conn, "good_ind", content_hash(d))

    metas = plugin_loader.approved_plugins(conn, plugins_dir)

    assert len(metas) == 1
    assert metas[0].name == "good_ind"
    assert isinstance(metas[0], PluginMeta)


# ③ 承認後の編集で出ない (ハッシュ不一致) --------------------------------

def test_approved_plugins_excludes_after_post_approval_edit(tmp_path, caplog):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    d = _write_plugin(plugins_dir, "edited_ind")
    conn = _conn(tmp_path)
    from agentic_fx.plugin.loader import content_hash
    _approve(conn, "edited_ind", content_hash(d))

    # 承認後に plugin.py を書き換える → 現在のハッシュが承認時と食い違う
    (d / "plugin.py").write_text(INDICATOR_PY + "\n# edited after approval\n")

    with caplog.at_level(logging.WARNING):
        metas = plugin_loader.approved_plugins(conn, plugins_dir)

    assert metas == []
    assert "edited_ind" in caplog.text


def test_approved_plugins_tolerates_missing_dir(tmp_path):
    conn = _conn(tmp_path)
    metas = plugin_loader.approved_plugins(conn, tmp_path / "no_such_dir")
    assert metas == []


# ④ get_indicators 合成 (fake sandbox_run) -------------------------------

def test_get_indicators_composes_plugin_output(tmp_path):
    d = _write_plugin(tmp_path, "my_ind")
    from agentic_fx.plugin.loader import content_hash as ch
    meta = PluginMeta(name="my_ind", kind="indicator", path=d, params={"x": 1},
                      timeframe=None, pairs=(), max_bars=50,
                      content_hash=ch(d))

    received = {}

    def fake_sandbox_run(meta_arg, payload, *, settings):
        received["meta"] = meta_arg
        received["payload"] = payload
        received["settings"] = settings
        return {"custom": 42.0}

    provider = MagicMock()
    provider.get_bars.return_value = _bars(n=120, interval="1h")
    econ = MagicMock()
    from agentic_fx.config import load_settings
    settings = load_settings(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")

    reg = ToolRegistry()
    reg.register_all(market_tools.build(
        provider, econ, settings, indicator_plugins=[meta],
        sandbox_run=fake_sandbox_run))

    result = reg.func("get_indicators")(pair="USDJPY", timeframe="1h")

    assert result["plugin:my_ind"] == {"custom": 42.0}
    # 組み込み指標も引き続き返る
    assert "rsi_14" in result

    # fake が受け取った payload は max_bars=50 でクランプされた df
    df = received["payload"]["df"]
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 50
    assert received["payload"]["params"] == {"x": 1}
    assert received["meta"] is meta
    assert received["settings"] is settings.plugin


def test_get_indicators_skips_plugin_over_max_bars_limit(tmp_path, caplog):
    d = _write_plugin(tmp_path, "huge_ind")
    from agentic_fx.plugin.loader import content_hash as ch
    from agentic_fx.config import load_settings
    settings = load_settings(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
    meta = PluginMeta(name="huge_ind", kind="indicator", path=d, params={},
                      timeframe=None, pairs=(),
                      max_bars=settings.plugin.max_bars_limit + 1,
                      content_hash=ch(d))

    called = []

    def fake_sandbox_run(meta_arg, payload, *, settings):
        called.append(meta_arg)
        return {"x": 1.0}

    provider = MagicMock()
    provider.get_bars.return_value = _bars(n=120, interval="1h")

    reg = ToolRegistry()
    reg.register_all(market_tools.build(
        provider, MagicMock(), settings, indicator_plugins=[meta],
        sandbox_run=fake_sandbox_run))

    with caplog.at_level(logging.WARNING):
        result = reg.func("get_indicators")(pair="USDJPY", timeframe="1h")

    assert "plugin:huge_ind" not in result
    assert called == []
    assert "huge_ind" in caplog.text


# ⑤ SandboxError でも組み込みは返る --------------------------------------

def test_get_indicators_fail_open_on_sandbox_error(tmp_path, caplog):
    d = _write_plugin(tmp_path, "broken_ind")
    from agentic_fx.plugin.loader import content_hash as ch
    meta = PluginMeta(name="broken_ind", kind="indicator", path=d, params={},
                      timeframe=None, pairs=(), max_bars=50,
                      content_hash=ch(d))

    def failing_sandbox_run(meta_arg, payload, *, settings):
        raise SandboxError("simulated timeout")

    provider = MagicMock()
    provider.get_bars.return_value = _bars(n=120, interval="1h")
    from agentic_fx.config import load_settings
    settings = load_settings(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")

    reg = ToolRegistry()
    reg.register_all(market_tools.build(
        provider, MagicMock(), settings, indicator_plugins=[meta],
        sandbox_run=failing_sandbox_run))

    with caplog.at_level(logging.WARNING):
        result = reg.func("get_indicators")(pair="USDJPY", timeframe="1h")

    assert "plugin:broken_ind" not in result
    assert "rsi_14" in result  # 組み込みは必ず返る (fail-open)
    assert "broken_ind" in caplog.text


def test_get_indicators_default_sandbox_run_is_plugin_run_plugin():
    """indicator_plugins/sandbox_run 未指定時は市場ツールが (プラグイン
    無しで) 従来どおり動く — 呼び出し元 ~10 箇所の後方互換確認を兼ねる。"""
    provider = MagicMock()
    provider.get_bars.return_value = _bars(n=120, interval="1h")
    econ = MagicMock()
    from agentic_fx.config import load_settings
    settings = load_settings(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")

    reg = ToolRegistry()
    reg.register_all(market_tools.build(provider, econ, settings))

    result = reg.func("get_indicators")(pair="USDJPY", timeframe="1h")
    assert "rsi_14" in result
    assert not any(k.startswith("plugin:") for k in result)


def test_get_indicators_default_sandbox_run_binds_to_plugin_run_plugin(
        tmp_path, monkeypatch):
    """`sandbox_run` 省略時、実際に `plugin.sandbox.run_plugin` が (meta,
    payload, settings=settings.plugin) の呼び出し規約で束縛されることの
    ピン — この束縛が壊れると本番で `TypeError` になり、`except
    plugin_sandbox.SandboxError` では捕まらない (fail-open が効かず
    get_indicators 全体が例外で落ちる) ため、default=None 経路も明示的に
    テストする。束縛は `build()` 呼び出し時点で行われるため、monkeypatch は
    `build()` を呼ぶ**前**に当てる。
    """
    from agentic_fx.plugin import sandbox as plugin_sandbox
    d = _write_plugin(tmp_path, "default_ind")
    from agentic_fx.plugin.loader import content_hash as ch
    meta = PluginMeta(name="default_ind", kind="indicator", path=d, params={"a": 1},
                      timeframe=None, pairs=(), max_bars=50, content_hash=ch(d))

    received = {}

    def fake_run_plugin(meta_arg, payload, *, settings, timeout_sec=None):
        received["meta"] = meta_arg
        received["payload"] = payload
        received["settings"] = settings
        return {"custom": 7.0}

    monkeypatch.setattr(plugin_sandbox, "run_plugin", fake_run_plugin)

    provider = MagicMock()
    provider.get_bars.return_value = _bars(n=120, interval="1h")
    from agentic_fx.config import load_settings
    settings = load_settings(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")

    reg = ToolRegistry()
    reg.register_all(market_tools.build(
        provider, MagicMock(), settings, indicator_plugins=[meta]))

    result = reg.func("get_indicators")(pair="USDJPY", timeframe="1h")

    assert result["plugin:default_ind"] == {"custom": 7.0}
    assert received["meta"] is meta
    assert received["payload"]["params"] == {"a": 1}
    assert received["settings"] is settings.plugin


# ⑥ kind フィルタ (indicator_plugins に非 indicator kind が混じっても無視) ---

def test_get_indicators_ignores_non_indicator_kind_plugins(tmp_path):
    """`indicator_plugins` には `plugin_loader.approved_plugins()` の全 kind
    混在の結果をそのまま渡してよい設計 — `kind="signal"` 等は
    `get_indicators` 側で無視され、`sandbox_run` にも渡らない。"""
    d = _write_plugin(tmp_path, "sig", plugin_py="""
def detect(df, params):
    return []
""", config_yaml="""
kind: signal
timeframe: 1h
pairs: [USDJPY]
""")
    from agentic_fx.plugin.loader import content_hash as ch
    signal_meta = PluginMeta(name="sig", kind="signal", path=d, params={},
                             timeframe="1h", pairs=("USDJPY",), max_bars=200,
                             content_hash=ch(d))

    called = []

    def fake_sandbox_run(meta_arg, payload, *, settings):
        called.append(meta_arg)
        return {"should": "not be reached"}

    provider = MagicMock()
    provider.get_bars.return_value = _bars(n=120, interval="1h")
    from agentic_fx.config import load_settings
    settings = load_settings(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")

    reg = ToolRegistry()
    reg.register_all(market_tools.build(
        provider, MagicMock(), settings, indicator_plugins=[signal_meta],
        sandbox_run=fake_sandbox_run))

    result = reg.func("get_indicators")(pair="USDJPY", timeframe="1h")

    assert called == []
    assert not any(k.startswith("plugin:") for k in result)
