"""承認詳細の web 面: indicator gate の警告 (`gate_warnings` / `similar_to` /
`scale_columns`) の節。語・文は daemon の表の写しで、数値は出さず、壊れた値は節ごと
出さない。"""
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


def test_scale_dependent_shows_the_sentence_and_the_column_names():
    page = _page(_data(gate_warnings=["scale_dependent"], scale_columns=["gap", "vol_move"]))
    assert SCALE + " (列: gap, vol_move)" in page
    assert "<li>" + SCALE in page


def test_similar_rows_name_the_deployed_indicator_the_kind_and_the_column():
    rows = [_row("ema", "value", "similar"), _row("rsi", "rsi", "duplicate"),
            _row("atr", "a", "monotone"), ]
    page = _page(_data(similar_to=rows))
    assert "警告: 配備済み `ema` と類似 (列: value)" in page
    assert "警告: 配備済み `rsi` と同等 (列: rsi)" in page
    assert "警告: 配備済み `atr` の単調変換 (列: a)" in page


def test_overlap_and_a_pending_partner_are_worded_differently_from_a_deployed_one():
    page = _page(_data(similar_to=[_row("pending-12", "v", "overlap")]))
    assert "警告: 承認待ち #12 と一部重複 (列: v)" in page


def test_every_other_word_has_its_own_sentence_in_payload_order():
    words = ["sparse_output", "duplicate_unavailable", "indicator_redefined"]
    page = _page(_data(gate_warnings=words))
    positions = [page.index(s) for s in (
        "有限値が少ない出力があります", "配備済みの一部と比較できませんでした",
        "同名の配備済み指標から出力の意味が変わっています")]
    assert positions == sorted(positions)


def test_the_section_sits_below_the_baseline_section():
    comparison = {"verdict": "improved", "stages": {}}
    page = _page(_data(baseline={"comparison": comparison},
                       gate_warnings=["sparse_output"]))
    assert page.index("配備中の版 (同条件で再評価)") < page.index("有限値が少ない")


def test_with_a_change_section_the_warnings_are_still_shown():
    change = {"name": "cand", "kind": "indicator", "classification": "new",
              "candidate_hash": "a" * 64, "baseline_hash": None,
              "computed_at": "2026-10-19T01:02:03+00:00", "reason": None,
              "params": {"items": [], "omitted": 0, "invalid_keys": 0,
                         "flatten_omitted": 0, "reason": None},
              "performance": {"in_sample": [], "holdout": []}, "summary": None}
    page = render_approval(_data(gate_warnings=["scale_dependent"]),
                           {"id": 7, "change": change, "claims": {},
                            "diff": {"files": [], "omitted_lines": 0}}).decode("utf-8")
    assert SCALE in page


@pytest.mark.parametrize("data", [
    {}, {"gate_warnings": None, "similar_to": None}, {"gate_warnings": [], "similar_to": []}])
def test_no_findings_means_no_section(data):
    assert "警告" not in _page(_data(**data))


@pytest.mark.parametrize("bad", [
    "scale_dependent", 7, [1], [None], [["scale_dependent"]], ["unknown"],
    ["sparse_output", "sparse_output"], {"sparse_output": 1},
    ["sparse_output"] * 9])
def test_a_broken_warning_list_is_dropped_and_the_rows_survive(bad):
    page = _page(_data(gate_warnings=bad, similar_to=[_row()]))
    assert "警告: 配備済み `ema` と類似 (列: value)" in page
    assert "有限値が少ない" not in page and "unknown" not in page


@pytest.mark.parametrize("bad", [
    "ema", 3, [1], [None], [{"name": "ema", "column": "v"}],
    [{"name": "ema", "column": "v", "kind": "similar", "corr": 0.987}],
    [{"name": "ema", "column": "v", "kind": "other"}],
    [{"name": "<b>", "column": "v", "kind": "similar"}],
    [{"name": "ema", "column": "v w", "kind": "similar"}],
    [{"name": "pending-x", "column": "v", "kind": "similar"}],
    [_row()] * 4])
def test_a_broken_similar_list_is_dropped_and_the_warnings_survive(bad):
    page = _page(_data(gate_warnings=["sparse_output"], similar_to=bad))
    assert "有限値が少ない" in page
    assert "配備済み `" not in page and "0.987" not in page


@pytest.mark.parametrize("bad", [
    "gap", 3, [1], ["Bad"], ["gap", "gap"], [0.5], ["a" * 40], [f"c{i}" for i in range(9)]])
def test_broken_scale_columns_leave_the_plain_sentence(bad):
    page = _page(_data(gate_warnings=["scale_dependent"], scale_columns=bad))
    assert SCALE in page and "(列:" not in page


def test_no_number_from_a_row_reaches_the_page():
    row = {"name": "ema", "column": "value", "kind": "similar", "corr": 0.987654}
    page = _page(_data(gate_warnings=["indicator_similar"], similar_to=[row],
                       scale_columns=[0.123456]))
    assert "0.9876" not in page and "0.1234" not in page


@pytest.mark.parametrize("value", [None, "x", 3, [], [1]])
def test_a_non_mapping_value_cannot_break_the_page(value):
    assert render_approval(_data(gate_warnings=value, similar_to=value,
                                 scale_columns=value), None)


def test_the_page_stays_under_the_response_limit_with_every_word_and_row():
    from afx_ops.web.render import GATE_WARNING_TEXT, MAX_RESPONSE_BYTES
    words = list(GATE_WARNING_TEXT)[:8]
    rows = [_row(f"n{i}", "c", "similar") for i in range(3)]
    page = render_approval(_data(gate_warnings=words, similar_to=rows,
                                 scale_columns=[f"c{i}" for i in range(8)]), None)
    assert len(page) < MAX_RESPONSE_BYTES


def test_a_malformed_change_response_still_shows_the_warnings():
    page = render_approval(_data(gate_warnings=["scale_dependent"]),
                           {"id": 7, "change": {"classification": 3}}).decode("utf-8")
    assert SCALE in page


def test_a_missing_change_response_still_shows_the_warnings():
    assert SCALE in _page(_data(gate_warnings=["scale_dependent"]))
