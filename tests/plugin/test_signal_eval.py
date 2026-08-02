"""signal_eval.evaluate_detection のテスト (プラン 7 Task 4)。

sandbox サブプロセスは一切起動しない — 全テストで `sandbox_run` に fake
を注入する (ウォークフォワードのセッション償却経路のみ、実サブプロセスの
代わりに `PluginSession` を monkeypatch した fake クラスで検証する)。
実 HTTP/git/乱数/実時刻取得も使わない。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from agentic_fx.config import load_settings
from agentic_fx.plugin import signal_eval
from agentic_fx.plugin.loader import PluginMeta, content_hash as _real_content_hash
from agentic_fx.plugin.sandbox import SandboxError

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"

DETECT_PY = """
def detect(df, params):
    return []
"""


def _plugin_dir(base: Path, name: str, *, kind: str = "signal") -> Path:
    d = base / name
    d.mkdir()
    (d / "plugin.py").write_text(DETECT_PY)
    (d / "config.yaml").write_text(f"kind: {kind}\n")
    (d / "test_plugin.py").write_text("def test_placeholder():\n    pass\n")
    return d


def _meta(d: Path, name: str, *, kind: str = "signal", max_bars: int = 200,
         params: dict | None = None) -> PluginMeta:
    return PluginMeta(name=name, kind=kind, path=d, params=params or {},
                      timeframe="1h", pairs=("USDJPY",), max_bars=max_bars,
                      content_hash=_real_content_hash(d))


def _bars(n: int, *, start: str = "2026-01-01T00:00:00Z",
         freq: str = "1h") -> list:
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return [[ts.isoformat(), 100.0 + i, 100.5 + i, 99.5 + i, 100.2 + i, 10.0 + i]
            for i, ts in enumerate(idx)]


def _write_labels(d: Path, bars: list, expected: list) -> None:
    (d / "labels.json").write_text(json.dumps({"bars": bars, "expected": expected}))


@pytest.fixture(scope="module")
def settings():
    return load_settings(EXAMPLE)


# --- brief 逐語の precision/recall 期待値 --------------------------------

def test_two_matches_one_extra_gives_precision_recall_two_thirds(tmp_path, settings):
    d = _plugin_dir(tmp_path, "acc_ind")
    bars = _bars(4)
    idx = pd.date_range("2026-01-01T00:00:00Z", periods=4, freq="1h", tz="UTC")
    t0, t1, t2, t3 = idx

    _write_labels(d, bars, [
        {"bar_ts": t0.isoformat(), "direction": "long"},
        {"bar_ts": t1.isoformat(), "direction": "short"},
        {"bar_ts": t2.isoformat(), "direction": "long"},
    ])
    meta = _meta(d, "acc_ind")

    def fake_sandbox_run(meta_arg, payload, *, settings):
        latest = payload["df"].index[-1]
        if latest == t0:
            return {"signals": [{"direction": "long", "strength": 0.8, "rationale": "r"}]}
        if latest == t1:
            return {"signals": [{"direction": "short", "strength": 0.7, "rationale": "r"}]}
        if latest == t3:
            return {"signals": [{"direction": "long", "strength": 0.6, "rationale": "r"}]}
        return {"signals": []}

    result = signal_eval.evaluate_detection(
        meta, sandbox_run=fake_sandbox_run, settings=settings)

    assert result == {
        "precision": pytest.approx(2 / 3), "recall": pytest.approx(2 / 3),
        "tp": 2, "fp": 1, "fn": 1, "n_labels": 3,
    }


def test_missing_labels_json_raises_value_error(tmp_path, settings):
    d = _plugin_dir(tmp_path, "no_labels_ind")
    meta = _meta(d, "no_labels_ind")

    with pytest.raises(ValueError, match="labels.json"):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


# --- labels.json スキーマ fail closed -----------------------------------

def test_labels_json_invalid_json_raises_value_error(tmp_path, settings):
    d = _plugin_dir(tmp_path, "bad_json_ind")
    (d / "labels.json").write_text("{not json")
    meta = _meta(d, "bad_json_ind")

    with pytest.raises(ValueError):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_labels_json_top_level_not_dict_raises_value_error(tmp_path, settings):
    d = _plugin_dir(tmp_path, "list_top_ind")
    (d / "labels.json").write_text("[]")
    meta = _meta(d, "list_top_ind")

    with pytest.raises(ValueError):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_labels_json_missing_bars_key_raises_value_error(tmp_path, settings):
    d = _plugin_dir(tmp_path, "no_bars_ind")
    (d / "labels.json").write_text(json.dumps({"expected": [
        {"bar_ts": "2026-01-01T00:00:00Z", "direction": "long"}]}))
    meta = _meta(d, "no_bars_ind")

    with pytest.raises(ValueError, match="bars"):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_labels_json_missing_expected_key_raises_value_error(tmp_path, settings):
    d = _plugin_dir(tmp_path, "no_expected_ind")
    (d / "labels.json").write_text(json.dumps({"bars": _bars(2)}))
    meta = _meta(d, "no_expected_ind")

    with pytest.raises(ValueError, match="expected"):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_labels_json_bars_not_list_raises_value_error(tmp_path, settings):
    d = _plugin_dir(tmp_path, "bars_not_list_ind")
    (d / "labels.json").write_text(json.dumps({"bars": "nope", "expected": [
        {"bar_ts": "2026-01-01T00:00:00Z", "direction": "long"}]}))
    meta = _meta(d, "bars_not_list_ind")

    with pytest.raises(ValueError):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_labels_json_expected_not_list_raises_value_error(tmp_path, settings):
    d = _plugin_dir(tmp_path, "expected_not_list_ind")
    (d / "labels.json").write_text(json.dumps({"bars": _bars(2), "expected": "nope"}))
    meta = _meta(d, "expected_not_list_ind")

    with pytest.raises(ValueError):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_bars_row_wrong_length_raises_value_error(tmp_path, settings):
    d = _plugin_dir(tmp_path, "bad_row_len_ind")
    bars = _bars(2)
    bars[0] = bars[0][:5]  # 5 要素に削る
    _write_labels(d, bars, [{"bar_ts": "2026-01-01T00:00:00Z", "direction": "long"}])
    meta = _meta(d, "bad_row_len_ind")

    with pytest.raises(ValueError):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_bars_row_non_numeric_ohlcv_raises_value_error(tmp_path, settings):
    d = _plugin_dir(tmp_path, "bad_row_type_ind")
    bars = _bars(2)
    bars[0][1] = "not-a-number"
    _write_labels(d, bars, [{"bar_ts": "2026-01-01T00:00:00Z", "direction": "long"}])
    meta = _meta(d, "bad_row_type_ind")

    with pytest.raises(ValueError):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_bars_out_of_order_raises_value_error(tmp_path, settings):
    """昇順前提のウォークフォワードを崩す入力は fail closed で拒否する
    (brief に明示は無いが、順序が崩れると bar_ts の対応がずれて評価結果
    が無意味になるため — 自己判断で追加した検証)。"""
    d = _plugin_dir(tmp_path, "out_of_order_ind")
    bars = _bars(3)
    bars[0], bars[1] = bars[1], bars[0]
    _write_labels(d, bars, [{"bar_ts": "2026-01-01T00:00:00Z", "direction": "long"}])
    meta = _meta(d, "out_of_order_ind")

    with pytest.raises(ValueError):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_expected_empty_list_raises_value_error(tmp_path, settings):
    d = _plugin_dir(tmp_path, "empty_expected_ind")
    _write_labels(d, _bars(2), [])
    meta = _meta(d, "empty_expected_ind")

    with pytest.raises(ValueError, match="expected"):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_expected_invalid_direction_raises_value_error(tmp_path, settings):
    d = _plugin_dir(tmp_path, "bad_dir_ind")
    _write_labels(d, _bars(2),
                 [{"bar_ts": "2026-01-01T00:00:00Z", "direction": "up"}])
    meta = _meta(d, "bad_dir_ind")

    with pytest.raises(ValueError):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_expected_extra_key_raises_value_error(tmp_path, settings):
    d = _plugin_dir(tmp_path, "extra_key_ind")
    _write_labels(d, _bars(2), [
        {"bar_ts": "2026-01-01T00:00:00Z", "direction": "long", "strength": 0.5}])
    meta = _meta(d, "extra_key_ind")

    with pytest.raises(ValueError):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_expected_missing_bar_ts_raises_value_error(tmp_path, settings):
    d = _plugin_dir(tmp_path, "missing_bar_ts_ind")
    _write_labels(d, _bars(2), [{"direction": "long"}])
    meta = _meta(d, "missing_bar_ts_ind")

    with pytest.raises(ValueError):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_kind_not_signal_raises_value_error(tmp_path, settings):
    d = _plugin_dir(tmp_path, "wrong_kind_ind", kind="indicator")
    meta = _meta(d, "wrong_kind_ind", kind="indicator")

    with pytest.raises(ValueError, match="signal"):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


# --- 時刻正規化 ------------------------------------------------------------

def test_naive_and_aware_iso_normalize_to_same_utc_instant(tmp_path, settings):
    d = _plugin_dir(tmp_path, "tz_ind")
    idx = pd.date_range("2026-01-01T00:00:00Z", periods=2, freq="1h", tz="UTC")
    t0, t1 = idx
    bars = [[t0.isoformat(), 1.0, 1.0, 1.0, 1.0, 1.0],
           [t1.isoformat(), 1.0, 1.0, 1.0, 1.0, 1.0]]
    # naive iso (tz 情報なし) — UTC として正規化されることを検証する
    _write_labels(d, bars, [{"bar_ts": "2026-01-01T00:00:00", "direction": "long"}])
    meta = _meta(d, "tz_ind")

    def fake_sandbox_run(meta_arg, payload, *, settings):
        if payload["df"].index[-1] == t0:
            return {"signals": [{"direction": "long", "strength": 0.5, "rationale": "r"}]}
        return {"signals": []}

    result = signal_eval.evaluate_detection(
        meta, sandbox_run=fake_sandbox_run, settings=settings)

    assert result["tp"] == 1
    assert result["fp"] == 0
    assert result["fn"] == 0
    assert result["precision"] == pytest.approx(1.0)
    assert result["recall"] == pytest.approx(1.0)


# --- ウォークフォワード呼び出しパターン -----------------------------------

def test_walk_forward_clamps_window_to_max_bars_and_grows(tmp_path, settings):
    d = _plugin_dir(tmp_path, "window_ind")
    bars = _bars(4)
    _write_labels(d, bars, [{"bar_ts": bars[0][0], "direction": "long"}])
    meta = _meta(d, "window_ind", max_bars=2)

    seen_lengths = []
    seen_payload_keys = []

    def fake_sandbox_run(meta_arg, payload, *, settings):
        seen_lengths.append(len(payload["df"]))
        seen_payload_keys.append(set(payload))
        return {"signals": []}

    signal_eval.evaluate_detection(meta, sandbox_run=fake_sandbox_run, settings=settings)

    # i=0,1,2,3 の 4 回呼ばれ、max_bars=2 でクランプされる
    assert seen_lengths == [1, 2, 2, 2]
    assert all(keys == {"df", "params"} for keys in seen_payload_keys)


def test_duplicate_signals_at_same_bar_collapse_to_one_predicted(tmp_path, settings):
    d = _plugin_dir(tmp_path, "dup_ind")
    bars = _bars(1)
    _write_labels(d, bars, [{"bar_ts": bars[0][0], "direction": "long"}])
    meta = _meta(d, "dup_ind")

    def fake_sandbox_run(meta_arg, payload, *, settings):
        return {"signals": [
            {"direction": "long", "strength": 0.5, "rationale": "r1"},
            {"direction": "long", "strength": 0.9, "rationale": "r2"},
        ]}

    result = signal_eval.evaluate_detection(
        meta, sandbox_run=fake_sandbox_run, settings=settings)

    assert result["tp"] == 1
    assert result["fp"] == 0


# --- SandboxError の伝播 (fail closed、握りつぶさない) --------------------

def test_sandbox_error_propagates_uncaught(tmp_path, settings):
    d = _plugin_dir(tmp_path, "boom_ind")
    _write_labels(d, _bars(2), [{"bar_ts": "2026-01-01T00:00:00Z", "direction": "long"}])
    meta = _meta(d, "boom_ind")

    def failing_sandbox_run(meta_arg, payload, *, settings):
        raise SandboxError("simulated plugin crash")

    with pytest.raises(SandboxError, match="simulated plugin crash"):
        signal_eval.evaluate_detection(meta, sandbox_run=failing_sandbox_run,
                                       settings=settings)


# --- 既定実行のセッション償却 (PluginSession を 1 個だけ開く) -------------

def test_default_sandbox_run_opens_single_plugin_session(tmp_path, settings, monkeypatch):
    d = _plugin_dir(tmp_path, "sess_ind")
    bars = _bars(3)
    _write_labels(d, bars, [{"bar_ts": bars[0][0], "direction": "long"}])
    meta = _meta(d, "sess_ind")

    session_opens = []
    calls = []

    class FakeSession:
        def __init__(self, meta_arg, *, settings):
            session_opens.append((meta_arg, settings))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def call(self, payload):
            calls.append(payload)
            return {"signals": []}

    monkeypatch.setattr(signal_eval.plugin_sandbox, "PluginSession", FakeSession)

    signal_eval.evaluate_detection(meta, settings=settings)

    assert len(session_opens) == 1
    assert session_opens[0] == (meta, settings.plugin)
    assert len(calls) == 3
