"""golden-v2 → v3 の許容差分だけを機械分類する。"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

GOLDEN_DIR = Path(__file__).parent / "golden"
UNCHANGED = {"orders", "equity_curve", "snapshots", "start", "end",
             "first_decision_at", "fallback_spread_used"}


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _load(name: str) -> dict:
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


def _classify_golden_diff(v2: dict, v3: dict) -> dict:
    assert set(v3) == set(v2)
    for key in UNCHANGED:
        assert _canonical(v3[key]) == _canonical(v2[key]), key
    assert set(v3["metrics"]) == set(v2["metrics"]) | {"kill_switch_latches"}
    assert type(v3["metrics"]["kill_switch_latches"]) is int
    assert v3["metrics"]["kill_switch_latches"] == 1
    for key, value in v2["metrics"].items():
        assert _canonical(v3["metrics"][key]) == _canonical(value), key

    expected = [e for e in v2["kill_switch_events"]
                if e["reason"] != "state_store kill_switch_latched transition"]
    assert len(v2["kill_switch_events"]) - len(expected) == 1
    assert _canonical(v3["kill_switch_events"]) == _canonical(expected)
    assert sum(e["kind"] == "latched" for e in v3["kill_switch_events"]) == 1
    assert not any(e["kind"] == "released" for e in v3["kill_switch_events"])
    return {"added_metric_keys": 1,
            "removed_duplicate_activity_events": 1,
            "changed_existing_values": 0, "unexpected_added_rows": 0,
            "unclassified": 0}


def test_v2_to_v3_diff_is_fully_classified():
    assert _classify_golden_diff(_load("golden-v2.json"),
                                 _load("golden-v3.json")) == {
        "added_metric_keys": 1, "removed_duplicate_activity_events": 1,
        "changed_existing_values": 0, "unexpected_added_rows": 0,
        "unclassified": 0}


@pytest.mark.parametrize("mutation", [
    "existing_metric", "snapshot", "released", "wrong_latched",
    "unknown_top_level", "float_latches",
])
def test_classifier_rejects_each_unapproved_difference(mutation):
    v2, v3 = _load("golden-v2.json"), _load("golden-v3.json")
    bad = copy.deepcopy(v3)
    if mutation == "existing_metric": bad["metrics"]["trades"] += 1
    elif mutation == "snapshot": bad["snapshots"].append(copy.deepcopy(bad["snapshots"][-1]))
    elif mutation == "released": bad["kill_switch_events"].append({
        "ts": bad["end"], "kind": "released", "reason": "x", "drawdown_pct": 0})
    elif mutation == "wrong_latched": bad["kill_switch_events"].clear()
    elif mutation == "unknown_top_level": bad["unknown"] = 1
    else: bad["metrics"]["kill_switch_latches"] = 1.0
    with pytest.raises(AssertionError):
        _classify_golden_diff(v2, bad)
