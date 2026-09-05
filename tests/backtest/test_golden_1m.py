"""golden-v1 は 1m replay の観測面を逐語固定する。"""
from __future__ import annotations

import difflib
import json
from pathlib import Path

from tests.backtest.golden.generate import build_golden


GOLDEN_PATH = Path(__file__).parent / "golden" / "golden-v1.json"

# generate.py の main() が golden-v1.json を書く際の canonical serialization
# (段階 1 レビュー是正 6b): sort_keys / 固定 separators (indent=2 の既定
# separators と揃える) / 末尾改行。dict の Python 等価比較 (`==`) は
# `1 == 1.0` を True にしてしまい、int/float の表現差 (json では "1" vs
# "1.0") を検出できない — byte-for-byte 文字列比較に切り替える。
def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def test_golden_v1_replay_observations_are_verbatim():
    """orders / curve / metrics / snapshots / kill-switch を消す変異を検出する。

    byte-for-byte 比較 (段階 1 レビュー是正 6b): dict 等価では int/float の
    表現差 (1 vs 1.0) を見逃す (`1 == 1.0` は True) ため、generator と同じ
    canonical serialization で文字列として突き合わせる。
    """
    actual = build_golden()
    actual_text = _canonical(actual)
    expected_text = GOLDEN_PATH.read_text(encoding="utf-8")
    if actual_text != expected_text:
        diff = "\n".join(difflib.unified_diff(
            expected_text.splitlines(), actual_text.splitlines(),
            fromfile="golden-v1.json", tofile="actual", lineterm=""))
        raise AssertionError(f"golden-v1 mismatch:\n{diff}")

    close_reasons = [row["close_reason"] for row in actual["orders"]]
    assert "sl" in close_reasons
    assert "tp" in close_reasons
    assert actual["snapshots"]


def test_golden_v1_byte_comparison_catches_int_vs_float_representation():
    """1 (int) と 1.0 (float) は Python の `==` では等しいが JSON 表現は
    "1" と "1.0" で異なる — byte-for-byte 比較でなければこの差を見逃す
    (段階 1 レビュー是正 6b の pin)。実 golden とは無関係な最小フィクスチャ
    で検証する。"""
    same_value_different_repr = {"a": 1}
    same_value_as_float = {"a": 1.0}
    assert same_value_different_repr == same_value_as_float  # dict 等価は通る
    assert _canonical(same_value_different_repr) != _canonical(same_value_as_float)
