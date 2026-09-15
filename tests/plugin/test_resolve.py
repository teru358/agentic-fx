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


def _inv(root: Path, *metas: PluginMeta) -> ApprovedInventory:
    return ApprovedInventory(root=root.resolve(), metas=tuple(metas))


def test_resolve_not_found(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    s = _strategy(tmp_path / "c", "s", "indicators:\n  rsi: {plugin: rsi}\n")
    with pytest.raises(IndicatorResolutionError) as ei:
        resolve_indicator_deps(s, _inv(root), settings=SETTINGS, pin_mode="check")
    assert (ei.value.alias, ei.value.reason) == ("rsi", "not_found")


def test_resolve_not_indicator(tmp_path):
    root = tmp_path / "plugins"
    other = _strategy(root, "rsi", "")
    s = _strategy(tmp_path / "c", "s", "indicators:\n  rsi: {plugin: rsi}\n")
    with pytest.raises(IndicatorResolutionError) as ei:
        resolve_indicator_deps(s, _inv(root, other), settings=SETTINGS,
                               pin_mode="check")
    assert (ei.value.alias, ei.value.reason) == ("rsi", "not_indicator")


def test_resolve_outputs_undeclared(tmp_path):
    """U4b: outputs 宣言なしの配備済 indicator は依存先にできない。"""
    root = tmp_path / "plugins"
    legacy = _indicator(root, "rsi", outputs="")
    assert legacy.outputs is None
    s = _strategy(tmp_path / "c", "s", "indicators:\n  rsi: {plugin: rsi}\n")
    for mode in ("require", "check"):
        with pytest.raises(IndicatorResolutionError) as ei:
            resolve_indicator_deps(s, _inv(root, legacy), settings=SETTINGS,
                                   pin_mode=mode)
        assert (ei.value.alias, ei.value.reason) == ("rsi", "outputs_undeclared")


def test_resolve_over_max_bars_limit(tmp_path):
    root = tmp_path / "plugins"
    big = _indicator(root, "rsi", max_bars=f"max_bars: {SETTINGS.plugin.max_bars_limit + 1}\n")
    s = _strategy(tmp_path / "c", "s", "indicators:\n  rsi: {plugin: rsi}\n")
    with pytest.raises(IndicatorResolutionError) as ei:
        resolve_indicator_deps(s, _inv(root, big), settings=SETTINGS,
                               pin_mode="check")
    assert (ei.value.alias, ei.value.reason) == ("rsi", "over_max_bars_limit")


def test_resolve_params_not_json_safe_after_merge(tmp_path):
    root = tmp_path / "plugins"
    ind = _indicator(root, "rsi")
    # strategy 側の上書きに非 JSON-safe 値を差し込む (loader を通さずに
    # meta を組み替えて merge 後検証だけを突く — loader は既に L1 で pin 済み)
    from agentic_fx.plugin.loader import IndicatorRef
    s = _strategy(tmp_path / "c", "s", "indicators:\n  rsi: {plugin: rsi}\n")
    broken = PluginMeta(
        name=s.name, kind=s.kind, path=s.path, params=s.params,
        timeframe=s.timeframe, pairs=s.pairs, max_bars=s.max_bars,
        content_hash=s.content_hash, artifact_hash=s.artifact_hash,
        indicators=(IndicatorRef(alias="rsi", plugin="rsi",
                                 params={"period": float("inf")}, pin=None),),
        outputs=None)
    with pytest.raises(IndicatorResolutionError) as ei:
        resolve_indicator_deps(broken, _inv(root, ind), settings=SETTINGS,
                               pin_mode="check")
    assert (ei.value.alias, ei.value.reason) == ("rsi", "params_not_json_safe")


def test_resolve_unpinned_only_fails_under_require(tmp_path):
    root = tmp_path / "plugins"
    ind = _indicator(root, "rsi")
    s = _strategy(tmp_path / "c", "s", "indicators:\n  rsi: {plugin: rsi}\n")
    with pytest.raises(IndicatorResolutionError) as ei:
        resolve_indicator_deps(s, _inv(root, ind), settings=SETTINGS,
                               pin_mode="require")
    assert (ei.value.alias, ei.value.reason) == ("rsi", "unpinned")
    got = resolve_indicator_deps(s, _inv(root, ind), settings=SETTINGS,
                                 pin_mode="check")
    assert got.all_pinned is False and got.items[0].pinned is False


def test_resolve_pin_mismatch_under_require_and_check_but_ignored_under_ignore(tmp_path):
    root = tmp_path / "plugins"
    ind = _indicator(root, "rsi")
    stale = "a" * 64
    s = _strategy(tmp_path / "c", "s",
                  f"indicators:\n  rsi: {{plugin: rsi, pin: '{stale}'}}\n")
    for mode in ("require", "check"):
        with pytest.raises(IndicatorResolutionError) as ei:
            resolve_indicator_deps(s, _inv(root, ind), settings=SETTINGS,
                                   pin_mode=mode)
        assert (ei.value.alias, ei.value.reason) == ("rsi", "pin_mismatch")
    got = resolve_indicator_deps(s, _inv(root, ind), settings=SETTINGS,
                                 pin_mode="ignore")
    assert got.items[0].content_hash == ind.content_hash   # 名前で解決し直す
    assert got.items[0].pinned is True and got.all_pinned is True


def test_resolve_pin_match_under_require(tmp_path):
    root = tmp_path / "plugins"
    ind = _indicator(root, "rsi")
    s = _strategy(tmp_path / "c", "s",
                  f"indicators:\n  rsi: {{plugin: rsi, pin: '{ind.content_hash}'}}\n")
    got = resolve_indicator_deps(s, _inv(root, ind), settings=SETTINGS,
                                 pin_mode="require")
    assert got.all_pinned is True
    assert got.items[0].plugin_py == ind.path / "plugin.py"
    assert got.items[0].outputs == ("v",)
    assert got.inventory_root == root.resolve()


def test_resolve_merges_strategy_params_over_indicator_defaults(tmp_path):
    root = tmp_path / "plugins"
    ind = _indicator(root, "rsi", params="params:\n  period: 14\n  src: close\n")
    s = _strategy(tmp_path / "c", "s",
                  "indicators:\n  rsi: {plugin: rsi, params: {period: 21}}\n")
    got = resolve_indicator_deps(s, _inv(root, ind), settings=SETTINGS,
                                 pin_mode="check")
    assert thaw(got.items[0].params) == {"period": 21, "src": "close"}
    # indicator 側の params dict は書き換えられない (deep copy)
    assert ind.params == {"period": 14, "src": "close"}


def test_resolve_handshake_too_large_boundary(tmp_path, monkeypatch):
    """R1 上限境界: `>` 判定を実測サイズの前後 1 byte で挟む。

    注: loader が通す入力 (deps <= 8 / params <= 8 KiB) では handshake は
    最大でも ~136 KB にしかならず、既定の 256 KiB には到達しない。よって
    定数を monkeypatch して境界そのものを pin する (設計 R1 の申し送り参照)。
    """
    import json
    root = tmp_path / "plugins"
    from agentic_fx.plugin.loader import MAX_PARAMS_BYTES
    blob = "x" * (MAX_PARAMS_BYTES - 64)
    ind = _indicator(root, "rsi", params=f"params:\n  big: '{blob}'\n")
    body = "indicators:\n" + "".join(
        f"  a{i}: {{plugin: rsi}}\n" for i in range(8))
    s = _strategy(tmp_path / "c", "s", body)

    got = resolve_indicator_deps(s, _inv(root, ind), settings=SETTINGS,
                                 pin_mode="check")
    assert len(got.items) == 8
    size = len(json.dumps(got.handshake_items(), separators=(",", ":"),
                          sort_keys=True, ensure_ascii=False).encode("utf-8"))

    # ちょうど上限は通る (実装は `>` 判定)
    monkeypatch.setattr(resolve, "MAX_HANDSHAKE_BYTES", size)
    assert len(resolve_indicator_deps(
        s, _inv(root, ind), settings=SETTINGS, pin_mode="check").items) == 8

    # 1 byte 下げると落ちる
    monkeypatch.setattr(resolve, "MAX_HANDSHAKE_BYTES", size - 1)
    with pytest.raises(IndicatorResolutionError) as ei:
        resolve_indicator_deps(s, _inv(root, ind), settings=SETTINGS,
                               pin_mode="check")
    assert (ei.value.alias, ei.value.reason) == (None, "handshake_too_large")


def test_resolver_failure_spawns_no_worker(tmp_path, monkeypatch):
    """R1: 拒否時に worker spawn 0 回。"""
    import subprocess
    calls = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(
                            AssertionError("worker spawned")))
    root = tmp_path / "plugins"
    root.mkdir()
    s = _strategy(tmp_path / "c", "s", "indicators:\n  rsi: {plugin: rsi}\n")
    with pytest.raises(IndicatorResolutionError):
        resolve_indicator_deps(s, _inv(root), settings=SETTINGS, pin_mode="check")
    assert calls == []


import shutil

import yaml

from agentic_fx.plugin.resolve import (
    is_relock_transition, lock_config, same_modulo_pins, strip_pins,
)


def test_lock_config_writes_pins_and_keeps_discoverable(tmp_path):
    root = tmp_path / "plugins"
    rsi = _indicator(root, "rsi")
    adx = _indicator(root, "adx")
    cand = tmp_path / "cand"
    s = _strategy(cand, "s", "indicators:\n"
                             "  rsi: {plugin: rsi}\n  adx: {plugin: adx}\n")
    before_hash = s.content_hash
    resolved = resolve_indicator_deps(s, _inv(root, rsi, adx), settings=SETTINGS,
                                      pin_mode="ignore")
    before_text, after_text, new_hash = lock_config(cand / "s", resolved.pins())
    assert "pin:" not in before_text
    written = yaml.safe_load((cand / "s" / "config.yaml").read_text())
    assert written["indicators"]["rsi"]["pin"] == rsi.content_hash
    assert written["indicators"]["adx"]["pin"] == adx.content_hash
    assert after_text == (cand / "s" / "config.yaml").read_text()
    # ユーザー裁定 2026-09-14 ⑥: lock 後は必ず snapshot (content_hash) を
    # disk から取り直す。返り値の new_hash が、書き込み後の config.yaml を
    # 独立に再読して計算した content_hash() と一致すること (呼び出し元が
    # resolve 時点の値を使い回していないことの pin)。
    assert new_hash == content_hash(cand / "s")
    relocked, reason = discover_one_with_reason(cand / "s", "s")
    assert reason is None                       # P1: 書き換え後も discover を通る
    assert relocked.content_hash != before_hash  # pin は content_hash の署名対象
    assert relocked.content_hash == new_hash     # snapshot 再取得の値と一致
    # 既に同じ pin なら no-op (2 回目の lock で内容が変わらない)
    resolved2 = resolve_indicator_deps(relocked, _inv(root, rsi, adx),
                                       settings=SETTINGS, pin_mode="ignore")
    b2, a2, h2 = lock_config(cand / "s", resolved2.pins())
    assert b2 == a2
    assert h2 == new_hash                        # no-op でも snapshot は再計算される


def test_lock_config_overwrites_stale_pin(tmp_path):
    root = tmp_path / "plugins"
    rsi = _indicator(root, "rsi")
    cand = tmp_path / "cand"
    s = _strategy(cand, "s",
                  "indicators:\n  rsi: {plugin: rsi, pin: '" + "b" * 64 + "'}\n")
    resolved = resolve_indicator_deps(s, _inv(root, rsi), settings=SETTINGS,
                                      pin_mode="ignore")
    lock_config(cand / "s", resolved.pins())
    written = yaml.safe_load((cand / "s" / "config.yaml").read_text())
    assert written["indicators"]["rsi"]["pin"] == rsi.content_hash


def test_strip_pins_removes_only_pins():
    cfg = {"kind": "strategy", "indicators": {
        "rsi": {"plugin": "rsi", "pin": "a" * 64, "params": {"p": 1}}}}
    out = strip_pins(cfg)
    assert out["indicators"]["rsi"] == {"plugin": "rsi", "params": {"p": 1}}
    assert cfg["indicators"]["rsi"]["pin"] == "a" * 64   # 入力は不変


def test_same_modulo_pins(tmp_path):
    root = tmp_path / "plugins"
    rsi = _indicator(root, "rsi")
    a = tmp_path / "a"
    _strategy(a, "s", f"indicators:\n  rsi: {{plugin: rsi, pin: '{'a' * 64}'}}\n")
    b = tmp_path / "b"
    _strategy(b, "s", f"indicators:\n  rsi: {{plugin: rsi, pin: '{rsi.content_hash}'}}\n")
    c = tmp_path / "c"
    _strategy(c, "s", "indicators:\n  rsi: {plugin: rsi, params: {p: 1}}\n")
    assert same_modulo_pins(a / "s", b / "s") is True
    assert same_modulo_pins(a / "s", c / "s") is False


def test_is_relock_transition(tmp_path):
    root = tmp_path / "plugins"
    rsi = _indicator(root, "rsi")          # 現在 inventory の hash
    inv = _inv(root, rsi)
    deployed = tmp_path / "dep"
    _strategy(deployed, "s",
              f"indicators:\n  rsi: {{plugin: rsi, pin: '{'a' * 64}'}}\n")  # 破れ
    cand = tmp_path / "cand"
    _strategy(cand, "s",
              f"indicators:\n  rsi: {{plugin: rsi, pin: '{rsi.content_hash}'}}\n")
    assert is_relock_transition(cand / "s", deployed / "s", inv) is True
    # 候補が古い pin のまま = 再ロックではない
    stale_cand = tmp_path / "stale"
    _strategy(stale_cand, "s",
              f"indicators:\n  rsi: {{plugin: rsi, pin: '{'a' * 64}'}}\n")
    assert is_relock_transition(stale_cand / "s", deployed / "s", inv) is False
    # deployed の pin が破れていない = 再ロックではない
    fresh_dep = tmp_path / "fresh"
    _strategy(fresh_dep, "s",
              f"indicators:\n  rsi: {{plugin: rsi, pin: '{rsi.content_hash}'}}\n")
    assert is_relock_transition(cand / "s", fresh_dep / "s", inv) is False
    # 依存なしの strategy 同士は再ロックではない
    nodep_a, nodep_b = tmp_path / "na", tmp_path / "nb"
    _strategy(nodep_a, "s", "")
    _strategy(nodep_b, "s", "")
    assert is_relock_transition(nodep_a / "s", nodep_b / "s", inv) is False
