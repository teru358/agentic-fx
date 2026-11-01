"""承認ページの byte 配分と上限の端、節ごとの表示 allowlist、縮小の段階。"""
from __future__ import annotations

import re

import pytest

from afx_ops.web import render

KIB = 1024
HEADER = "@@ -1 +1 @@"
DETAIL = {"id": 7, "kind": "plugin", "status": "pending", "payload_sha256": "cd" * 32,
          "in_sample": {"USDJPY": {"trades": 40}}, "holdout": {"USDJPY": {"trades": 20}}}


def make_change(items=(), performance=None, **over):
    change = {"name": "sma_cross", "kind": "strategy", "classification": "updated",
              "candidate_hash": "a1" * 32, "baseline_hash": "b2" * 32,
              "computed_at": "2026-10-19T01:02:03+00:00", "reason": None,
              "params": {"items": list(items), "omitted": 0, "invalid_keys": 0,
                         "flatten_omitted": 0, "reason": None},
              "performance": performance or {"in_sample": [], "holdout": []}, "summary": None}
    change.update(over)
    return change


def entry(path, lines, status="changed", header=HEADER, omitted=0):
    return {"path": path, "status": status, "reason": None, "omitted_lines": omitted,
            "hunks": [{"header": header, "lines": lines}]}


def change_data(change=None, files=None, claims=None):
    return {"id": 7, "change": change or make_change(),
            "claims": claims or {"summary": None, "selection_rationale": None},
            "diff": {"files": files if files is not None else [entry("plugin.py", ["+x"])],
                     "omitted_lines": 0}}


def compose(data, detail=None):
    return render.compose_approval_page(detail if detail is not None else dict(DETAIL),
                                        data["change"], data)


def overview_of(page: bytes) -> str:
    return re.search(r"<h2>概要</h2>(.*?)</section>", page.decode(), re.S).group(1)


# ---------------------------------------------------------------- 1 ファイルの行の詰め方

def _single_file_plan(cost, count=14000):
    lines = ["+" + "a" * (cost - 2)] * count
    (plan,) = render.plan_diff({"files": [entry("plugin.py", lines)], "omitted_lines": 0})
    return plan


def _room_after_header(cost, count=14000):
    total = count + 1
    head = len(render.FilePlan(entry("plugin.py", ["+x"])).head.encode())
    fixed = head + len("</pre>") + len(render._omitted_mark(total).encode()) + 1
    return render.DIFF_BUDGET_BYTES - fixed - (len(HEADER) + 1)


def _cost_with_remainder(wanted):
    for cost in range(4, 40):
        room = _room_after_header(cost)
        if room % cost == wanted(cost):
            return cost, room
    raise AssertionError("no line cost gives the requested remainder")


@pytest.mark.parametrize("wanted", [lambda c: 0, lambda c: c - 1], ids=["exact_fit", "one_short"])
def test_a_file_shows_the_most_lines_that_fit_and_never_exceeds_its_allocation(wanted):
    cost, room = _cost_with_remainder(wanted)
    plan = _single_file_plan(cost)
    assert plan.alloc == render.DIFF_BUDGET_BYTES
    assert plan.shown == 1 + room // cost
    assert len(plan.html.encode()) <= plan.alloc


# ---------------------------------------------------------------- 他の節の 15 KiB

def _claims_and_params(rows, summary_chars, rationale_chars):
    items = [{"key": f"params.k{i:02d}", "old": "o" * 80, "new": "n" * 80} for i in range(rows)]
    return (make_change(items),
            {"summary": "s" * summary_chars, "selection_rationale": "r" * rationale_chars})


def _other_bytes(rows, summary_chars, rationale_chars=2000):
    change, claims = _claims_and_params(rows, summary_chars, rationale_chars)
    return compose(change_data(change, claims=claims)).other_bytes


def _exact_fit():
    for rows in range(1, 65):
        need = render.OTHER_BUDGET_BYTES - _other_bytes(rows, 1) + 1
        if 1 <= need <= 2000:
            return rows, need
    raise AssertionError("no exact fit")


def test_other_sections_may_use_exactly_15_kib_and_shrink_from_one_byte_more():
    rows, summary = _exact_fit()
    change, claims = _claims_and_params(rows, summary, 2000)
    fit = compose(change_data(change, claims=claims))
    assert fit.other_bytes == render.OTHER_BUDGET_BYTES
    assert fit.page.decode().count("<dt>params.k") == rows
    change, claims = _claims_and_params(rows, summary + 1, 2000)
    over = compose(change_data(change, claims=claims))
    assert over.other_bytes <= render.OTHER_BUDGET_BYTES
    assert over.page.decode().count("<dt>params.k") < rows


# ---------------------------------------------------------------- ページ全体の 64 KiB

def test_a_page_of_exactly_64_kib_is_clipped_and_one_byte_less_is_served(monkeypatch):
    monkeypatch.setattr(render, "OTHER_BUDGET_BYTES", 10 ** 6)
    monkeypatch.setattr(render, "DIFF_BUDGET_BYTES", 10 ** 6)
    monkeypatch.setattr(render, "FILE_QUOTA_BYTES", 10 ** 6)

    def page_with(chars):
        return compose(change_data(files=[entry("plugin.py", ["+" + "a" * chars])])).page
    fixed = len(page_with(1)) - 1
    served = page_with(render.PAGE_LIMIT_BYTES - 1 - fixed)
    assert len(served) == render.PAGE_LIMIT_BYTES - 1 and b"<h3>plugin.py</h3>" in served
    clipped = page_with(render.PAGE_LIMIT_BYTES - fixed)
    assert len(clipped) < render.PAGE_LIMIT_BYTES
    assert b"Output clipped" in clipped and b"<h3>plugin.py</h3>" not in clipped


def test_a_generic_page_of_exactly_the_response_limit_is_served_and_one_more_is_clipped():
    base = len(render._document("T", ""))
    exact = render._page("T", "x" * (render.MAX_RESPONSE_BYTES - base))
    assert len(exact) == render.MAX_RESPONSE_BYTES and b"Output clipped" not in exact
    assert b"Output clipped" in render._page("T", "x" * (render.MAX_RESPONSE_BYTES - base + 1))


# ---------------------------------------------------------------- 縮小の段階

def _ladder_page(tail_chars, items=21):
    parent = {"result": ["p" * 500] * items + ["q" * tail_chars]}
    detail = dict(DETAIL, parent_facts=parent)
    data = change_data(claims={"summary": "s" * 2000, "selection_rationale": "r" * 2000})
    return compose(data, detail)


def test_claims_shrink_through_each_step_before_the_parent_record_is_given_up():
    seen, omitted_claims = set(), False
    for items in range(14, 32):
        for tail in range(0, 501, 10):
            built = _ladder_page(tail, items)
            assert built.other_bytes <= render.OTHER_BUDGET_BYTES
            match = re.search(r"<h3>summary</h3><pre>(s*)…?</pre>", built.page.decode())
            seen.add(len(match.group(1)) if match else 0)
            omitted_claims = omitted_claims or "<p>…(省略)</p>" in built.page.decode()
    assert {2000, 1000, 500, 250, 100} <= seen
    assert omitted_claims


def test_the_parent_record_is_dropped_last_and_only_when_nothing_else_is_left_to_shrink():
    built = _ladder_page(500, items=50)
    assert built.other_bytes <= render.OTHER_BUDGET_BYTES
    assert b"<dt>parent_facts</dt>" not in built.page
    assert b"<dt>parent_facts</dt>" in _ladder_page(0, items=1).page


# ---------------------------------------------------------------- 概要の allowlist

def test_overview_shows_the_decision_fields_the_parent_record_and_the_hash_of_the_payload():
    detail = dict(DETAIL, decided_by="alice", decided_at="2026-10-19T02:00:00+00:00",
                  parent_facts={"analysis_call_count": 3}, unknown_field="LEAK")
    built = compose(change_data(), detail)
    overview = overview_of(built.page)
    assert "<dt>decided_by</dt><dd>alice</dd>" in overview
    assert "<dt>decided_at</dt>" in overview
    assert "<dt>parent_facts</dt>" in overview and "analysis_call_count" in overview
    assert "<dt>payload_sha256</dt>" in overview
    assert "LEAK" not in built.page.decode()


def test_overview_does_not_repeat_the_metrics_that_have_their_own_section():
    detail = dict(DETAIL, in_sample={"trades": 40, "pf": 1.3}, holdout={"trades": 20, "pf": 1.1})
    built = compose(change_data(make_change(
        performance={"in_sample": [{"pair": None, "trades": 40, "pf": 1.3, "avg_r": 0.0}],
                     "holdout": []})), detail)
    overview = overview_of(built.page)
    assert "in_sample" not in overview and "holdout" not in overview
    assert "<dt>in_sample</dt><dd>trades=40" in built.page.decode()


# ---------------------------------------------------------------- 件数上限・並び・許可された名前

def test_performance_shows_at_most_sixteen_pairs_per_scope():
    rows = [{"pair": f"P{i:02d}", "trades": 1, "pf": 1.0, "avg_r": 0.0} for i in range(20)]
    text = compose(change_data(make_change(performance={"in_sample": rows,
                                                         "holdout": []}))).page.decode()
    assert text.count("<dt>in_sample P") == 16 and "in_sample P16" not in text


def test_params_show_at_most_the_params_limit_rows():
    items = [{"key": f"params.k{i:03d}", "old": "1", "new": "2"} for i in range(render.PARAMS_MAX + 6)]
    text = compose(change_data(make_change(items))).page.decode()
    assert text.count("<dt>params.k") == render.PARAMS_MAX


def test_diff_files_are_listed_in_the_fixed_order_whatever_order_they_arrive_in():
    files = [entry("test_plugin.py", ["+t"]), entry("config.yaml", ["+c"]),
             entry("plugin.py", ["+p"])]
    text = compose(change_data(files=files)).page.decode()
    assert text.index("<h3>plugin.py</h3>") < text.index("<h3>config.yaml</h3>") \
        < text.index("<h3>test_plugin.py</h3>")


def test_a_diff_entry_for_any_other_path_is_not_shown():
    files = [entry("../../etc/passwd", ["+LEAK"]), entry("evil.py", ["+LEAK"]),
             entry("plugin.py", ["+p"])]
    text = compose(change_data(files=files)).page.decode()
    assert "LEAK" not in text and "evil.py" not in text and "<h3>plugin.py</h3>" in text


def test_a_changed_file_without_any_line_or_omission_is_shown_as_unchanged():
    text = compose(change_data(files=[{"path": "plugin.py", "status": "changed", "reason": None,
                                       "omitted_lines": 0, "hunks": []}])).page.decode()
    assert "<h3>plugin.py</h3><p>差分なし</p>" in text and "<pre>@@" not in text


# ---------------------------------------------------------------- 値の整形

def test_diff_header_and_body_are_escaped_including_quotes():
    files = [entry("plugin.py", ['+say "hi" \'x\' <b>&'], header='@@ <i>"&" @@')]
    text = compose(change_data(files=files)).page.decode()
    assert "@@ &lt;i&gt;&quot;&amp;&quot; @@\n" in text
    assert "+say &quot;hi&quot; &#x27;x&#x27; &lt;b&gt;&amp;\n" in text
    assert "<i>" not in text and "<b>" not in text


def test_tabs_in_a_claim_and_in_a_diff_line_become_four_spaces():
    data = change_data(files=[entry("plugin.py", ["+a\tb"])],
                       claims={"summary": "x\ty", "selection_rationale": None})
    text = compose(data).page.decode()
    assert "<pre>x    y</pre>" in text and "+a    b\n" in text
    assert "\t" not in text


# ---------------------------------------------------------------- indicator gate の警告節の予約

def _largest_gate_lines():
    return ["警告: " + "あ" * 80] * render.MAX_GATE_LINES


def _full_budget_change_data():
    # diff も、diff 以外の節 (申告の長文と params の行) も、それぞれの予算いっぱいまで使うページ
    rows, need = _exact_fit()
    change, claims = _claims_and_params(rows, 1 + need, 2000)
    lines = ["+" + "x" * 78] * 2000
    return change_data(change, claims=claims, files=[entry("plugin.py", lines)])


def _baseline_big_section():
    return "<section><h2>配備中の版</h2>" + "".join(
        "<p>" + "b" * 200 + "</p>" for _ in range(200)) + "</section>"


def test_the_largest_gate_section_never_clips_a_page_whose_other_sections_fill_their_budget():
    data = _full_budget_change_data()
    detail = dict(DETAIL)
    plain = render.compose_approval_page(detail, data["change"], data).page
    assert len(plain) < render.PAGE_LIMIT_BYTES and b"Output clipped" not in plain
    detail["gate_lines"] = _largest_gate_lines()
    page = render.render_approval(detail, data)
    assert b"Output clipped" not in page and len(page) < render.PAGE_LIMIT_BYTES
    text = page.decode()
    assert text.count("警告: " + "あ" * 80) == render.MAX_GATE_LINES
    assert "<h3>plugin.py</h3>" in text


def test_the_extra_sections_are_counted_in_the_budget_of_the_other_sections():
    data = _full_budget_change_data()
    detail = dict(DETAIL)
    built = render.compose_approval_page(detail, data["change"], data,
                                         extra_sections=["<section>" + "g" * 3000 + "</section>"])
    assert b"Output clipped" not in built.page
    assert built.other_bytes <= render.OTHER_BUDGET_BYTES


def test_other_sections_shrink_before_the_extra_sections_are_clipped():
    data = _full_budget_change_data()
    built = render.compose_approval_page(
        dict(DETAIL), data["change"], data,
        extra_sections=["<section>" + "g" * 1000 + "</section>"])
    assert b"g" * 1000 in built.page
    assert render.EXTRA_OMITTED_SECTION.encode() not in built.page


def test_an_oversized_baseline_section_is_clipped_whole_with_an_omission_line():
    data = _full_budget_change_data()
    detail = dict(DETAIL)
    gate_section = "<section><h2>indicator gate の警告</h2><ul><li>GATE-LINE</li></ul></section>"
    built = render.compose_approval_page(
        detail, data["change"], data,
        extra_sections=[_baseline_big_section(), gate_section])
    text = built.page.decode()
    assert "GATE-LINE" in text and "bbbbbbbbbb" not in text
    assert render.EXTRA_OMITTED_SECTION in text
    assert built.other_bytes <= render.OTHER_BUDGET_BYTES
    assert b"Output clipped" not in built.page


def test_other_bytes_stay_within_budget_for_the_largest_baseline_and_gate_together():
    data = _full_budget_change_data()
    detail = dict(DETAIL)
    detail["gate_lines"] = _largest_gate_lines()
    built = render.compose_approval_page(
        detail, data["change"], data,
        extra_sections=[_baseline_big_section(), render._gate_section(detail)])
    assert built.other_bytes <= render.OTHER_BUDGET_BYTES
    assert "警告: " in built.page.decode()
