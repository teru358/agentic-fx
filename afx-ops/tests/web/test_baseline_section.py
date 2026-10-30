"""承認詳細の web 面: 配備中の版 (同条件で再評価) の表、判定、hash、警告、旧 payload。"""
from __future__ import annotations

import pytest

from afx_ops.web.render import DETAIL_FIELDS, render_approval

SECTION = "配備中の版 (同条件で再評価)"
BASE = "b" * 64


def _metrics(pf, trades=40):
    return {"pf": pf, "trades": trades, "win_rate": 0.55, "avg_r": 0.12, "max_drawdown": 0.07,
            "total_pnl": 12.5, "kill_switch_latches": 0, "evaluable": True}


def _comparison(verdict="improved", mode="enforce", reason="holdout_exceeds"):
    stage = {"USDJPY": {"candidate": _metrics(1.31), "deployed": _metrics(1.17),
                        "verdict": verdict}}
    return {"mode": mode, "verdict": verdict, "reason": reason,
            "stages": {"in_sample": stage, "holdout": stage}}


def _data(**fields):
    data = {"id": 7, "kind": "plugin", "status": "pending", "payload_sha256": "a" * 64,
            "baseline": {"base_content_hash": BASE, "comparison": _comparison()}}
    data.update(fields)
    return data


def _page(data) -> str:
    return render_approval(data, None).decode("utf-8")


def test_the_baseline_field_is_a_detail_field():
    assert "baseline" in DETAIL_FIELDS


def test_improved_comparison_shows_both_versions_eight_metrics_verdict_and_hash():
    page = _page(_data())
    assert SECTION in page
    assert "判定: 改善" in page
    assert "base_content_hash: " + "b" * 12 in page and "b" * 13 not in page
    for column in ("pf", "trades", "win_rate", "avg_r", "max_drawdown", "total_pnl",
                   "kill_switch_latches", "evaluable", "verdict"):
        assert f"<th>{column}</th>" in page
    assert page.count("<td>候補</td>") == 2 and page.count("<td>配備中の版</td>") == 2
    assert "<td>1.31</td>" in page and "<td>1.17</td>" in page


def test_the_section_is_below_the_in_sample_and_holdout_tables():
    change = {"name": "sma_cross", "kind": "strategy", "classification": "updated",
              "candidate_hash": "a" * 64, "baseline_hash": "b" * 64,
              "computed_at": "2026-10-19T01:02:03+00:00", "reason": None,
              "params": {"items": [{"key": "params.period", "old": "14", "new": "10"}],
                         "omitted": 0, "invalid_keys": 0, "flatten_omitted": 0, "reason": None},
              "performance": {"in_sample": [{"pair": "USDJPY", "trades": 40, "pf": 1.31,
                                              "avg_r": 0.12}],
                              "holdout": [{"pair": "USDJPY", "trades": 20, "pf": 1.18,
                                           "avg_r": 0.08}]},
              "summary": "要約"}
    change_data = {"id": 7, "change": change, "claims": {"summary": "S", "selection_rationale": "R"},
                   "diff": {"files": [], "omitted_lines": 0}}
    page = render_approval(_data(), change_data).decode("utf-8")
    assert SECTION in page
    assert page.index("holdout") < page.index(SECTION)


@pytest.mark.parametrize("verdict,mode,reason,label", [
    ("not_improved", "shadow", "holdout_not_exceeds", "未改善 (観測のみ)"),
    ("unavailable", "enforce", "baseline_unhealthy", "比較不能 (baseline_unhealthy)"),
])
def test_the_verdict_label_follows_the_stored_verdict(verdict, mode, reason, label):
    page = _page(_data(baseline={"base_content_hash": BASE,
                                 "comparison": _comparison(verdict, mode, reason)}))
    assert "判定: " + label in page


def test_a_hash_mismatch_warning_is_shown_and_absent_otherwise():
    warned = _page(_data(baseline_hash_warning={"expected": "b" * 12, "actual": "c" * 12}))
    assert "警告: 配備中の版の hash が比較時と異なります" in warned
    assert "現在=" + "c" * 12 in warned
    assert "警告" not in _page(_data())


@pytest.mark.parametrize("fields", [
    {"baseline": None},
    {"baseline": {"ref_plugin_ref": "plugins/s", "variant": "baseline"}},
    {"baseline": {"base_content_hash": BASE}},
    {"baseline": {"base_content_hash": BASE, "comparison": "x"}},
    {"baseline": {"base_content_hash": BASE, "comparison": {"verdict": "improved"}}},
])
def test_a_payload_without_a_usable_comparison_renders_no_table_and_does_not_fail(fields):
    data = _data(**fields)
    assert SECTION not in _page(data)
    assert SECTION not in render_approval(data, {"change": "broken"}).decode("utf-8")


def test_the_page_stays_static_without_script():
    assert "<script" not in _page(_data()).lower()


def test_each_row_carries_its_pair_verdict():
    page = _page(_data(baseline={"base_content_hash": BASE,
                                 "comparison": _comparison("improved")}))
    assert page.count("<td>improved</td></tr>") == 4
