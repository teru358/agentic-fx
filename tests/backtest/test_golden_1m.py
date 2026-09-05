"""golden-v1 は 1m replay の観測面を逐語固定する。"""
from __future__ import annotations

import difflib
import json
from pathlib import Path

from tests.backtest.golden.generate import build_golden


GOLDEN_PATH = Path(__file__).parent / "golden" / "golden-v1.json"


def test_golden_v1_replay_observations_are_verbatim():
    """orders / curve / metrics / snapshots / kill-switch を消す変異を検出する。"""
    actual = build_golden()
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    if actual != expected:
        expected_text = json.dumps(expected, ensure_ascii=False, indent=2,
                                   sort_keys=True).splitlines()
        actual_text = json.dumps(actual, ensure_ascii=False, indent=2,
                                 sort_keys=True).splitlines()
        diff = "\n".join(difflib.unified_diff(
            expected_text, actual_text, fromfile="golden-v1.json",
            tofile="actual", lineterm=""))
        raise AssertionError(f"golden-v1 mismatch:\n{diff}")

    close_reasons = [row["close_reason"] for row in actual["orders"]]
    assert "sl" in close_reasons
    assert "tp" in close_reasons
    assert actual["snapshots"]
