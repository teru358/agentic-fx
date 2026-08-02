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


def test_fp_and_fn_are_not_symmetric_tp_fp_fn_distinguishable(tmp_path, settings):
    """fp != fn な入力で tp/fp/fn の取り違え (交換) を検出する — brief の
    2 一致+1 余分シナリオは fp == fn == 1 のため、fp/fn を丸ごと入れ替える
    変異が生存してしまう (自己レビューで実際に確認した)。"""
    d = _plugin_dir(tmp_path, "asym_ind")
    idx = pd.date_range("2026-01-01T00:00:00Z", periods=3, freq="1h", tz="UTC")
    t0, t1, t2 = idx
    bars = _bars(3)

    # expected は t0 の long のみ (1 件)
    _write_labels(d, bars, [{"bar_ts": t0.isoformat(), "direction": "long"}])
    meta = _meta(d, "asym_ind")

    def fake_sandbox_run(meta_arg, payload, *, settings):
        latest = payload["df"].index[-1]
        if latest == t0:
            return {"signals": [{"direction": "long", "strength": 0.8, "rationale": "r"}]}
        if latest == t1:
            return {"signals": [{"direction": "short", "strength": 0.7, "rationale": "r"}]}
        if latest == t2:
            return {"signals": [{"direction": "long", "strength": 0.6, "rationale": "r"}]}
        return {"signals": []}

    result = signal_eval.evaluate_detection(
        meta, sandbox_run=fake_sandbox_run, settings=settings)

    # predicted = {t0 long, t1 short, t2 long} (3件), expected = {t0 long} (1件)
    # tp=1 (t0 一致), fp=2 (t1, t2 は expected に無い余分), fn=0 (missed 無し)
    assert result["tp"] == 1
    assert result["fp"] == 2
    assert result["fn"] == 0
    assert result["precision"] == pytest.approx(1 / 3)
    assert result["recall"] == pytest.approx(1.0)


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
    """`match` はエラー文言固有の部分文字列にする — plugin 名
    `no_bars_ind` 自体が 'bars' を含むため、`match="bars"` はメッセージ
    本文から 'bars' を削っても plugin 名経由で偽陽性になり得る (レビュー
    fix round 1 F6、変異で実証済み。kind ガードで踏んだ tmp_path 由来の
    偽陽性と同種、キャリアが plugin 名なだけ)。"""
    d = _plugin_dir(tmp_path, "no_bars_ind")
    (d / "labels.json").write_text(json.dumps({"expected": [
        {"bar_ts": "2026-01-01T00:00:00Z", "direction": "long"}]}))
    meta = _meta(d, "no_bars_ind")

    with pytest.raises(ValueError, match="missing 'bars' key"):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_labels_json_missing_expected_key_raises_value_error(tmp_path, settings):
    """plugin 名 `no_expected_ind` が 'expected' を含むための偽陽性回避
    (F6、上記 test_labels_json_missing_bars_key と同種)。"""
    d = _plugin_dir(tmp_path, "no_expected_ind")
    (d / "labels.json").write_text(json.dumps({"bars": _bars(2)}))
    meta = _meta(d, "no_expected_ind")

    with pytest.raises(ValueError, match="missing 'expected' key"):
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


def test_bars_empty_list_raises_value_error(tmp_path, settings):
    """F7c (レビュー fix round 1、変異生存を実測) — `_bars_to_df` の空
    bars ガードを外しても green だった (`pd.DataFrame([], columns=...)`
    は空 DataFrame を素通しし、後段のウォークフォワードが 0 回のループで
    黙って完走してしまう) ため killer テストを追加。"""
    d = _plugin_dir(tmp_path, "empty_bars_ind")
    _write_labels(d, [], [{"bar_ts": "2026-01-01T00:00:00Z", "direction": "long"}])
    meta = _meta(d, "empty_bars_ind")

    with pytest.raises(ValueError, match="must be a non-empty list"):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_bars_duplicate_timestamp_raises_value_error(tmp_path, settings):
    """F7d (レビュー fix round 1) — 同時刻の重複 bar_ts 単体の killer
    テスト (これまでは非昇順の入れ替えテストしか無く、`<=` を `<` に緩め
    て重複だけ許容する変異が生存し得た)。"""
    d = _plugin_dir(tmp_path, "dup_ts_ind")
    bars = _bars(2)
    bars[1][0] = bars[0][0]  # 2 本目を 1 本目と同時刻に重複させる
    _write_labels(d, bars, [{"bar_ts": bars[0][0], "direction": "long"}])
    meta = _meta(d, "dup_ts_ind")

    with pytest.raises(ValueError, match="strictly ascending"):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_bars_timestamp_nat_raises_value_error(tmp_path, settings):
    """F2 (レビュー fix round 1) — `pd.Timestamp("NaT")` は例外を出さず
    NaT になり、tz 変換・昇順比較 (`<=`) を素通りしていた。bars 側の NaT
    を拒否する。`match` は `_parse_utc_timestamp` のエラー文言固有の
    'is NaT' にする (`{ts}` の repr が 'NaT' になる他のエラー経路 — 例:
    F1 の '... bar_ts NaT is not present in ...' — との偶然一致を避ける
    ため。実際に下記 test_expected_bar_ts_nat_raises_value_error で
    この種の偽陽性を変異注入により発見した)。"""
    d = _plugin_dir(tmp_path, "nat_bars_ind")
    bars = _bars(2)
    bars[0][0] = "NaT"
    _write_labels(d, bars, [{"bar_ts": bars[1][0], "direction": "long"}])
    meta = _meta(d, "nat_bars_ind")

    with pytest.raises(ValueError, match="is NaT"):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_bars_row_non_finite_value_raises_value_error(tmp_path, settings):
    """F3 (レビュー fix round 1) — `json.loads` は既定で NaN/Infinity を
    受理する。OHLCV の非有限値を拒否する。"""
    d = _plugin_dir(tmp_path, "nonfinite_ind")
    bars = _bars(2)
    bars[0][1] = float("nan")  # open を NaN にする
    _write_labels(d, bars, [{"bar_ts": bars[0][0], "direction": "long"}])
    meta = _meta(d, "nonfinite_ind")

    with pytest.raises(ValueError, match="finite"):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_bars_row_integer_overflow_raises_value_error(tmp_path, settings):
    """F3 (レビュー fix round 1) — JSON の巨大整数は Python の任意精度
    int として読める (json.loads はそのまま受理) が、`float()` へのキャ
    ストで `OverflowError` になり ValueError 契約から外れていた。"""
    d = _plugin_dir(tmp_path, "overflow_ind")
    bars = _bars(2)
    bars[0][1] = 10 ** 400  # float の表現範囲を超える巨大整数
    _write_labels(d, bars, [{"bar_ts": bars[0][0], "direction": "long"}])
    meta = _meta(d, "overflow_ind")

    with pytest.raises(ValueError, match="out of float range"):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_labels_json_exceeds_size_limit_raises_value_error(tmp_path, settings, monkeypatch):
    """F4 (レビュー fix round 1) — loader.py の 1MiB 上限
    (`_MAX_FILE_BYTES`) と同じ値を labels.json にも適用する。実ファイル
    を 1MiB 書くのは無駄なので上限自体を monkeypatch で小さくする
    (loader からの単一定義 import を signal_eval モジュール属性として
    差し替える)。"""
    d = _plugin_dir(tmp_path, "huge_labels_ind")
    _write_labels(d, _bars(2), [{"bar_ts": "2026-01-01T00:00:00Z", "direction": "long"}])
    meta = _meta(d, "huge_labels_ind")
    monkeypatch.setattr(signal_eval, "_MAX_FILE_BYTES", 10)

    with pytest.raises(ValueError, match="exceeds size limit"):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_expected_empty_list_raises_value_error(tmp_path, settings):
    """plugin 名 `empty_expected_ind` が 'expected' を含むための偽陽性
    回避 (F6 と同種の予防的修正 — レビューでの明示指摘対象ではないが同じ
    リスクパターンのため合わせて締める)。"""
    d = _plugin_dir(tmp_path, "empty_expected_ind")
    _write_labels(d, _bars(2), [])
    meta = _meta(d, "empty_expected_ind")

    with pytest.raises(ValueError, match="must be a non-empty list"):
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


def test_expected_non_hashable_direction_raises_value_error_not_type_error(tmp_path, settings):
    """F5 (レビュー fix round 1) — frozenset 包含判定
    (`direction not in _VALID_DIRECTIONS`) を `isinstance(direction, str)`
    より先に行うと、JSON の配列/オブジェクトのような非 hashable な値を
    direction に渡した際に生の `TypeError` になり ValueError 契約が破れ
    ていた。`isinstance` を先に検証していることを直接ピンする。"""
    d = _plugin_dir(tmp_path, "list_dir_ind")
    _write_labels(d, _bars(2),
                 [{"bar_ts": "2026-01-01T00:00:00Z", "direction": ["long"]}])
    meta = _meta(d, "list_dir_ind")

    with pytest.raises(ValueError, match="direction must be"):
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


def test_expected_bar_ts_not_in_bars_raises_value_error(tmp_path, settings):
    """F1 (レビュー fix round 1、Critical 相当) — expected の bar_ts が
    bars に実在しない場合、その bar_ts はウォークフォワードのどの呼び出
    しでも predicted 側に現れ得ず恒久的に fn となり recall を静かに汚染
    する (Task 6 の承認判断の入力破損)。bars に無い bar_ts は fail closed
    で拒否する。"""
    d = _plugin_dir(tmp_path, "orphan_ind")
    bars = _bars(2)  # 2026-01-01T00:00:00Z, 01:00:00Z の 2 本のみ
    _write_labels(d, bars, [{"bar_ts": "2099-01-01T00:00:00Z", "direction": "long"}])
    meta = _meta(d, "orphan_ind")

    with pytest.raises(ValueError, match="not present in"):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_expected_bar_ts_nat_raises_value_error(tmp_path, settings):
    """F2 (レビュー fix round 1) — expected 側の NaT も同様に拒否する
    (bars 側は test_bars_timestamp_nat_raises_value_error でカバー済み)。
    `match` は 'is NaT' に固定する — `match="NaT"` だと、F2 の isna()
    チェックを外した変異でも F1 の 'bar_ts NaT is not present in ...'
    (NaT は自己非等価のためどの bars 集合にも属せず F1 に必ず捕まる) が
    たまたま部分文字列 'NaT' を含んでしまい green のまま生存する偽陽性を
    変異注入で実際に確認した。"""
    d = _plugin_dir(tmp_path, "nat_expected_ind")
    _write_labels(d, _bars(2), [{"bar_ts": "NaT", "direction": "long"}])
    meta = _meta(d, "nat_expected_ind")

    with pytest.raises(ValueError, match="is NaT"):
        signal_eval.evaluate_detection(meta, sandbox_run=lambda *a, **k: {"signals": []},
                                       settings=settings)


def test_kind_not_signal_raises_value_error(tmp_path, settings):
    """kind ゲートが labels.json の有無より前に効くことを保証するため、
    有効な labels.json を用意したうえで検証する — `tmp_path` (pytest が
    テスト関数名から生成する一時ディレクトリ) がたまたま `match` 文字列
    を部分文字列として含んでしまうケースを避けるため、`match` は kind
    エラー文言に固有の 'requires' を使う (このテスト名自体に 'signal' が
    含まれるため `match="signal"` は tmp_path のパス経由で偽陽性になり
    得ることを自己レビューの変異注入で実際に確認した)。"""
    d = _plugin_dir(tmp_path, "wrong_kind_ind", kind="indicator")
    _write_labels(d, _bars(2), [{"bar_ts": "2026-01-01T00:00:00Z", "direction": "long"}])
    meta = _meta(d, "wrong_kind_ind", kind="indicator")

    with pytest.raises(ValueError, match="requires meta.kind"):
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


def test_positive_offset_iso_actually_shifts_to_utc(tmp_path, settings):
    """F7a (レビュー fix round 1、変異生存を実測) —
    `_parse_utc_timestamp` の `ts.tz_convert("UTC")` を `ts` (no-op) に
    緩める変異は、絶対時刻ベースの `pd.Timestamp` 等価性だけを見るテスト
    では検出できない (+09:00 の 09:00 も UTC の 00:00 も同一 instant の
    ため `==` は変わらず True)。`.tz` 属性と時刻の値そのもの (hour) を
    直接検証することで no-op を検出する。"""
    d = _plugin_dir(tmp_path, "offset_ind")
    # +09:00 の 09:00 = UTC の 00:00 (同一 instant だが tzinfo の表現が違う)
    bars = [["2026-01-01T09:00:00+09:00", 1.0, 1.0, 1.0, 1.0, 1.0]]
    _write_labels(d, bars, [{"bar_ts": "2026-01-01T09:00:00+09:00", "direction": "long"}])
    meta = _meta(d, "offset_ind")

    seen = []

    def fake_sandbox_run(meta_arg, payload, *, settings):
        seen.append(payload["df"].index[-1])
        return {"signals": [{"direction": "long", "strength": 0.5, "rationale": "r"}]}

    result = signal_eval.evaluate_detection(
        meta, sandbox_run=fake_sandbox_run, settings=settings)

    ts = seen[0]
    assert str(ts.tz) == "UTC"
    assert ts.hour == 0  # +09:00 09:00 → UTC 00:00 に実際にシフトしている
    assert result["tp"] == 1


# --- ウォークフォワード呼び出しパターン -----------------------------------

def test_walk_forward_clamps_window_to_max_bars_and_grows(tmp_path, settings):
    d = _plugin_dir(tmp_path, "window_ind")
    bars = _bars(4)
    _write_labels(d, bars, [{"bar_ts": bars[0][0], "direction": "long"}])
    meta = _meta(d, "window_ind", max_bars=2)

    seen_lengths = []
    seen_payload_keys = []
    seen_meta_args = []
    seen_settings = []

    def fake_sandbox_run(meta_arg, payload, **kwargs):
        seen_lengths.append(len(payload["df"]))
        seen_payload_keys.append(set(payload))
        seen_meta_args.append(meta_arg)
        seen_settings.append(kwargs["settings"])
        return {"signals": []}

    signal_eval.evaluate_detection(meta, sandbox_run=fake_sandbox_run, settings=settings)

    # i=0,1,2,3 の 4 回呼ばれ、max_bars=2 でクランプされる
    assert seen_lengths == [1, 2, 2, 2]
    assert all(keys == {"df", "params"} for keys in seen_payload_keys)
    # market_tools.build と同じ呼び出し規約: (meta, payload, *, settings=
    # settings.plugin)。settings が `Settings` 全体のまま渡されると
    # run_plugin/PluginSession 側で `settings.sandbox_timeout_sec` が
    # AttributeError になる (PluginSettings のみが持つ属性) — その取り
    # 違えを検出するため identity で固定する。
    assert all(m is meta for m in seen_meta_args)
    assert all(s is settings.plugin for s in seen_settings)


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


def test_precision_is_zero_when_no_signals_predicted(tmp_path, settings):
    """F7b (レビュー fix round 1、変異生存を実測) — `_score` の precision
    分母 0 分岐 (`0.0`) を `1.0` に緩める変異が、brief 逐語のテスト (常に
    tp>0 で分母が非 0) では検出できなかった。predicted が完全に空
    (tp=fp=0) のシナリオで拒否する。"""
    d = _plugin_dir(tmp_path, "empty_pred_ind")
    bars = _bars(2)
    _write_labels(d, bars, [{"bar_ts": bars[0][0], "direction": "long"}])
    meta = _meta(d, "empty_pred_ind")

    result = signal_eval.evaluate_detection(
        meta, sandbox_run=lambda *a, **k: {"signals": []}, settings=settings)

    assert result["tp"] == 0
    assert result["fp"] == 0
    assert result["fn"] == 1
    assert result["precision"] == 0.0
    assert result["recall"] == 0.0


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
