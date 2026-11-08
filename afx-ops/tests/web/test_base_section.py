"""承認詳細の web 面: 比較成績と独立した「土台」の節 (警告が要る 3 状態だけ)。"""
from __future__ import annotations

import pytest

from afx_ops.web.render import render_approval

MISMATCH = {"state": "mismatch", "base": "a" * 12, "live": "b" * 12}
NONE_BUT_LIVE = {"state": "none_but_live", "base": "none", "live": "c" * 12}
UNREADABLE = {"state": "unreadable", "base": "d" * 12, "live": "unreadable"}
CHANGE = {"name": "sma_cross", "kind": "strategy", "classification": "updated",
          "candidate_hash": "b" * 64, "baseline_hash": "c" * 64, "reason": None,
          "params": {"items": [], "omitted": 0, "invalid_keys": 0, "flatten_omitted": 0,
                     "reason": None}, "performance": {"in_sample": [], "holdout": []},
          "summary": None}


def _data(base_check=None, **fields):
    data = {"id": 7, "kind": "plugin", "status": "pending", "payload_sha256": "a" * 64,
            "change": dict(CHANGE), "base_check": base_check}
    data.update(fields)
    return data


def _page(data, change_data=None) -> str:
    return render_approval(data, change_data).decode("utf-8")


def test_ac14_a_mismatch_shows_both_short_hashes():
    page = _page(_data(MISMATCH))
    assert "<h2>土台</h2>" in page
    assert ("警告: この候補の土台は配備中の版と違います (土台 " + "a" * 12 + " / 配備中 "
            + "b" * 12 + ")。このままでは配備できません") in page


@pytest.mark.parametrize("view,line", [
    ({**MISMATCH, "live": "none"}, "配備中 無し)"),
    ({**MISMATCH, "live": "plain"}, "配備中 plain)"),
    (NONE_BUT_LIVE, "警告: この候補は同名の配備中の版を見ずに作られました。"
                    "このままでは配備できません"),
    (UNREADABLE, "警告: 配備中の版を確かめられません"),
])
def test_ac14_each_warning_state_shows_its_line(view, line):
    assert line in _page(_data(view))


def test_ac14_the_section_does_not_need_a_comparison():
    page = _page(_data(NONE_BUT_LIVE))
    assert "配備中の版 (同条件で再評価)" not in page and "<h2>土台</h2>" in page


def test_ac14_the_section_and_the_content_hash_warning_coexist():
    data = _data(MISMATCH, baseline={
        "base_content_hash": "b" * 64,
        "comparison": {"mode": "enforce", "verdict": "improved", "reason": "x", "stages": {}}},
        baseline_hash_warning={"expected": "e" * 12, "actual": "f" * 12})
    page = _page(data)
    assert "配備中の版の hash が比較時と異なります" in page
    assert "<h2>土台</h2>" in page


@pytest.mark.parametrize("view", [
    {"state": "match", "base": "a" * 12, "live": "a" * 12},
    {"state": "legacy", "base": None, "live": None},
    {"state": "unchecked", "base": None, "live": None},
    None, "mismatch", 5, [], {}, {"state": 5}, {"state": ["mismatch"]}, {"state": "nonsense"},
])
def test_ac14_no_section_otherwise(view):
    page = _page(_data(view))
    assert "<h2>土台</h2>" not in page and "警告" not in page


def test_ac14_a_response_without_base_check_renders_as_before():
    data = _data()
    del data["base_check"]
    assert "<h2>土台</h2>" not in _page(data)


def test_ac14_a_malformed_value_is_shown_as_unknown_and_never_echoed():
    page = _page(_data({"state": "mismatch", "base": "<script>x</script>", "live": {"a": 1}}))
    assert "土台 不明 / 配備中 不明" in page
    assert "<script>" not in page


def test_ac14_the_section_is_shown_when_the_change_is_missing_or_malformed():
    data = _data(MISMATCH)
    del data["change"]
    assert "<h2>土台</h2>" in _page(data)
    assert "<h2>土台</h2>" in _page(_data(MISMATCH, change={"classification": 5}))


def test_ac14_the_section_is_kept_last_when_extra_sections_do_not_fit():
    lines = ["x" * 400] * 40
    page = _page(_data(UNREADABLE, gate_lines=lines))
    assert "<h2>土台</h2>" in page
