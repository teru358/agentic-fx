"""成績行の追加 3 項目 (勝率・max DD・kill switch 回数) の語と桁、afx-ops の写しとの一致。"""
from __future__ import annotations

import ast
from pathlib import Path

from agentic_fx import metric_words as words
from agentic_fx.commands import Commands

AFX_OPS = Path(__file__).resolve().parents[1] / "afx-ops" / "afx_ops" / "metric_words.py"
_NAMES = ("WIN_RATE_LABEL", "MAX_DD_LABEL", "KILL_SWITCH_LABEL", "KILL_SWITCH_UNIT",
          "PERCENT_FORMAT")

FULL = {"pf": 1.002, "trades": 864, "avg_r": 0.004158, "max_drawdown": 0.263,
        "win_rate": 0.429, "kill_switch_latches": 55}


def _afx_ops_literals() -> dict:
    found = {}
    for node in ast.parse(AFX_OPS.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id in _NAMES:
            found[node.targets[0].id] = ast.literal_eval(node.value)
    return found


def test_afx_ops_words_match_the_daemon_words():
    found = _afx_ops_literals()
    assert set(found) == set(_NAMES)
    for name in _NAMES:
        assert found[name] == getattr(words, name), name


def test_items_are_percent_one_decimal_and_integer_count():
    assert words.metric_items(FULL) == ["勝率 42.9%", "max DD 26.3%", "kill switch 55回"]


def test_missing_or_malformed_keys_are_omitted_without_error():
    assert words.metric_items({"pf": 1.0}) == []
    assert words.metric_items(None) == []
    assert words.metric_items({"win_rate": "x", "max_drawdown": float("nan"),
                               "kill_switch_latches": True}) == []
    assert words.metric_items({"kill_switch_latches": 0}) == ["kill switch 0回"]


def test_cli_line_carries_the_items_for_a_full_and_an_old_payload():
    full = Commands._metrics_lines("in_sample", FULL)
    assert full == ["in_sample: pf=1.002 trades=864 avg_r=0.004158 max_drawdown=0.263 "
                    "勝率 42.9% max DD 26.3% kill switch 55回"]
    old = Commands._metrics_lines("holdout", {"pf": 1.0, "trades": 3, "avg_r": 0.1,
                                              "max_drawdown": 0.5})
    assert old == ["holdout: pf=1.0 trades=3 avg_r=0.1 max_drawdown=0.5 max DD 50.0%"]
    per_pair = Commands._metrics_lines("holdout", {"USDJPY": FULL})
    assert per_pair[0].endswith("勝率 42.9% max DD 26.3% kill switch 55回")
