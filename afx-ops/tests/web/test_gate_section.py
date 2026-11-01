"""承認詳細の web 面: indicator gate の警告の節。daemon が描画した行 (`gate_lines`) を
表示するだけで、語・文の表は持たない。"""
from __future__ import annotations

import pytest

from afx_ops.web.render import render_approval

SCALE = ("警告: 価格または出来高のスケールに依存する出力 "
         "(別銘柄で挙動が変わる可能性)")


def _row(name="ema", column="value", kind="similar"):
    return {"name": name, "column": column, "kind": kind}


def _data(**fields):
    data = {"id": 7, "kind": "plugin", "status": "pending", "payload_sha256": "a" * 64}
    data.update(fields)
    return data


def _page(data) -> str:
    return render_approval(data, None).decode("utf-8")


LINES = [SCALE + " (列: gap, vol_move)", "警告: 配備済み `ema` と類似 (列: value)",
         "警告: 承認待ち #12 と一部重複 (列: v)", "判読できない警告 2 件"]


def test_the_lines_the_daemon_rendered_are_shown_as_list_items_in_order():
    page = _page(_data(gate_lines=LINES))
    positions = [page.index("<li>" + line + "</li>") for line in LINES]
    assert positions == sorted(positions)


def test_the_web_package_has_no_word_table_so_it_shows_only_what_it_is_given():
    page = _page(_data(gate_warnings=["scale_dependent"], similar_to=[_row()],
                       scale_columns=["gap"]))
    assert "警告" not in page and "インジケータ" not in page


def test_the_section_sits_below_the_baseline_section():
    comparison = {"verdict": "improved", "stages": {}}
    page = _page(_data(baseline={"comparison": comparison},
                       gate_lines=["警告: 有限値が少ない"]))
    assert page.index("配備中の版 (同条件で再評価)") < page.index("有限値が少ない")


def test_with_a_change_section_the_warnings_are_still_shown():
    change = {"name": "cand", "kind": "indicator", "classification": "new",
              "candidate_hash": "a" * 64, "baseline_hash": None,
              "computed_at": "2026-10-19T01:02:03+00:00", "reason": None,
              "params": {"items": [], "omitted": 0, "invalid_keys": 0,
                         "flatten_omitted": 0, "reason": None},
              "performance": {"in_sample": [], "holdout": []}, "summary": None}
    page = render_approval(_data(gate_lines=[SCALE]),
                           {"id": 7, "change": change, "claims": {},
                            "diff": {"files": [], "omitted_lines": 0}}).decode("utf-8")
    assert SCALE in page


@pytest.mark.parametrize("data", [
    {}, {"gate_lines": None}, {"gate_lines": []}, {"gate_lines": "x"},
    {"gate_lines": [None, 3, ""]}])
def test_no_lines_means_no_section(data):
    assert "indicator gate" not in _page(_data(**data))


def test_line_text_is_escaped_and_only_strings_are_shown():
    page = _page(_data(gate_lines=["<b>x</b>", 5, None, "ok"]))
    assert "&lt;b&gt;x&lt;/b&gt;" in page and "<b>" not in page and "<li>ok</li>" in page


def test_the_number_of_lines_is_bounded():
    page = _page(_data(gate_lines=[f"line{i}" for i in range(100)]))
    assert "line15" in page and "line16" not in page


@pytest.mark.parametrize("value", [None, "x", 3, [], [1]])
def test_a_non_list_value_cannot_break_the_page(value):
    assert render_approval(_data(gate_lines=value), None)


def test_the_page_stays_under_the_response_limit_with_the_most_lines():
    from afx_ops.web.render import MAX_RESPONSE_BYTES
    page = render_approval(_data(gate_lines=["警告: " + "あ" * 200] * 40), None)
    assert len(page) < MAX_RESPONSE_BYTES


def test_a_malformed_change_response_still_shows_the_lines():
    page = render_approval(_data(gate_lines=[SCALE]),
                           {"id": 7, "change": {"classification": 3}}).decode("utf-8")
    assert SCALE in page


def test_a_missing_change_response_still_shows_the_lines():
    assert SCALE in _page(_data(gate_lines=[SCALE]))
