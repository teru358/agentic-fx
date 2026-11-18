"""承認詳細のタブ: 節の振り分け、query の検査、再読込、決定のブロックと form の条件。"""
from __future__ import annotations

import collections
import dataclasses
import re

import pytest

from afx_ops.api_client import Reply
from afx_ops.web import render, tabs
from afx_ops.web.request import Request, handle_request
from tests.gate.test_reevaluation_display import CHANGE as REEVAL_CHANGE, VIEW as REEVAL_VIEW
from tests.web.test_approval_change import (
    Client, DETAIL, file_entry, make_change, make_change_data,
)
from _write_support import DIGEST, Rig

TABS = tabs.tab_names(tabs.APPROVAL_DETAIL)
REFRESH = '<meta http-equiv="refresh"'
BLOCK_MARK = "見ていないタブの規模"
LONG = {"id": 7, "kind": "plugin", "status": "pending", "payload_sha256": DIGEST}


def get(client, tab=None, method="GET", query=None):
    query = dict(query or {})
    if tab is not None:
        query["tab"] = tab
    return handle_request(Request(method, "/approvals/7", query), client)


def text_of(client, tab=None) -> str:
    response = get(client, tab)
    assert response.status == 200
    return response.body.decode()


def h2s(page: str) -> list[str]:
    return re.findall(r"<h2>(.*?)</h2>", page)


def h3s(page: str) -> list[str]:
    return re.findall(r"<h3>(.*?)</h3>", page)


def full_detail(**over):
    detail = dict(DETAIL, change=make_change(), gate_lines=["警告: 類似あり"],
                  candidate_origin="reevaluation", reevaluation=REEVAL_VIEW, deploy_phase=None,
                  base_check={"state": "mismatch", "base": "a" * 12, "live": "b" * 12},
                  reason="history_check_unavailable",
                  baseline={"base_content_hash": "b" * 64, "comparison": {
                      "mode": "enforce", "verdict": "improved", "reason": "x",
                      "stages": {"holdout": {"USDJPY": {"candidate": {"pf": 1.3},
                                                        "deployed": {"pf": 1.1},
                                                        "verdict": "improved"}}}}})
    detail.update(over)
    return detail


# ---------------------------------------------------------------- 節の振り分け

def _sections_by_tab(client):
    return {tab: h2s(text_of(client, tab)) for tab in TABS}


def test_the_tab_table_lists_the_four_approval_tabs_with_the_default_first():
    assert TABS == ("overview", "result", "change", "claims")
    assert tabs.default_tab(tabs.APPROVAL_DETAIL) == "overview"
    assert [item.label for item in tabs.TABS[tabs.APPROVAL_DETAIL]] == ["概要", "成績", "変更", "申告"]


def test_every_section_goes_to_exactly_one_tab_in_the_order_of_the_table():
    client = Client(detail=full_detail(), change_data=make_change_data())
    found = _sections_by_tab(client)
    reevaluation = next(h for h in found["overview"] if h.startswith("再評価"))
    assert found == {
        "overview": ["概要", "indicator gate の警告", reevaluation, "土台", "履歴証跡"],
        "result": ["成績", "配備中の版 (同条件で再評価)"],
        "change": ["params の差分", "変更 diff"],
        "claims": ["申告 (agent の申告・未検証)"],
    }
    counts = collections.Counter(h for heads in found.values() for h in heads)
    assert set(counts.values()) == {1}


def test_an_approval_without_a_change_puts_its_sections_in_the_same_tabs_and_says_none():
    client = Client(detail=full_detail(change=None))
    found = _sections_by_tab(client)
    assert found["overview"][:2] == ["概要", "変更内容"]
    assert "indicator gate の警告" in found["overview"] and "土台" in found["overview"]
    assert found["result"] == ["配備中の版 (同条件で再評価)"]
    assert found["change"] == [] and found["claims"] == []
    for tab in ("change", "claims"):
        assert "<p>なし</p>" in text_of(client, tab)
    assert "変更内容を取得できません" in text_of(client, "overview")


def test_a_result_tab_without_a_baseline_comparison_says_none_but_still_has_its_table_of_metrics():
    client = Client(detail=dict(DETAIL, change=None))
    assert "<p>なし</p>" in text_of(client, "result")


def test_the_overview_dl_is_split_in_three_groups_and_keeps_the_dt_wording():
    detail = dict(DETAIL, change=make_change(), decided_by="alice",
                  decided_at="2026-10-19T02:00:00+00:00", asserted_actor="a", parent_facts={"n": 1})
    page = text_of(Client(detail=detail, change_data=make_change_data(change=make_change())))
    assert h3s(page) == ["承認", "対象", "内部値"]
    keys = re.findall(r"<dt>(.*?)</dt>", page)
    assert keys == ["id", "kind", "status", "decided_by", "decided_at",
                    "plugin", "kind", "分類", "候補", "比較対象", "計算時刻",
                    "payload_sha256", "parent_facts", "n", "asserted_actor"]


def test_an_approval_without_a_change_has_two_groups_with_the_metrics_at_the_end_of_the_internal_values():
    detail = dict(DETAIL, change=None, decided_by="alice", parent_facts={"n": 1},
                  in_sample={"trades": 40}, holdout={"trades": 20})
    page = text_of(Client(detail=detail))
    assert h3s(page) == ["承認", "内部値"]
    assert re.findall(r"<dt>(.*?)</dt>", page) == [
        "id", "kind", "status", "decided_by", "payload_sha256", "parent_facts", "n", "in_sample", "trades",
        "holdout", "trades"]
    assert "<h2>変更内容</h2>" in page


def test_the_group_table_has_the_same_keys_as_the_allowlists():
    keys = [key for _title, group in render.APPROVAL_GROUPS for key in group]
    assert len(keys) == len(set(keys))
    assert set(keys) == render.DETAIL_FIELDS
    assert set(keys) - {"in_sample", "holdout"} == render.OVERVIEW_FIELDS
    assert keys[-2:] == ["in_sample", "holdout"]


def test_the_virtual_page_sections_match_the_tab_table():
    built = render.compose_approval_page(dict(DETAIL), make_change(), make_change_data())
    assert set(built.sections) == set(TABS)


def test_a_group_with_no_value_prints_no_heading():
    detail = {"id": 7, "payload_sha256": DIGEST}
    page = text_of(Client(detail=dict(detail, change=make_change())))
    assert "承認" in h3s(page) and "内部値" in h3s(page)
    empty = render._approval_groups({}, "UTC")
    assert empty == ""


# ---------------------------------------------------------------- 帯

def test_the_bar_marks_the_current_tab_and_links_use_the_fixed_table():
    client = Client(change_data=make_change_data())
    for tab in TABS:
        page = text_of(client, tab)
        bar = re.search(r'<nav class="tabs">(.*?)</nav>', page).group(1)
        assert re.findall(r'<a href="([^"]*)"( aria-current="page")?>([^<]*)', bar) == [
            (f"/approvals/7?tab={name}", ' aria-current="page"' if name == tab else "", label)
            for name, label in zip(TABS, ("概要", "成績", "変更", "申告"))]
        assert '<a href="/plugins" aria-current="page">Plugins</a>' in page


# ---------------------------------------------------------------- query

@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("query", [
    {"tab": "deps"}, {"tab": ""}, {"tab": "OVERVIEW"}, {"tab": "overview", "x": "1"},
    {"x": "1"}, {"limit": "5"}, {"sort": "name"}, {"category": "NEWS"}])
def test_a_value_or_key_outside_the_table_is_400_and_the_daemon_is_not_called(method, query):
    client = Client(change_data=make_change_data())
    response = handle_request(Request(method, "/approvals/7", query), client)
    assert response.status == 400 and client.calls == []
    assert b"deps" not in response.body and b"OVERVIEW" not in response.body


def test_a_missing_tab_and_the_default_tab_return_the_same_body():
    client = Client(change_data=make_change_data())
    assert get(client).body == get(client, "overview").body


@pytest.mark.parametrize("tab", TABS)
def test_head_returns_the_same_status_and_length_with_an_empty_body(tab):
    client = Client(change_data=make_change_data())
    full, head = get(client, tab), get(client, tab, "HEAD")
    assert head.status == full.status == 200 and head.body == b""
    assert head.headers["Content-Length"] == str(len(full.body))


@pytest.mark.parametrize("tab", TABS)
def test_every_tab_reads_the_detail_and_the_change_once_each(tab):
    client = Client(change_data=make_change_data())
    get(client, tab)
    assert client.calls == [("approval", 7), ("change", 7)]


# ---------------------------------------------------------------- 再読込 (読む画面)

@pytest.mark.parametrize("tab,refresh", [("overview", True), ("result", True),
                                         ("change", False), ("claims", False)])
def test_the_refresh_of_a_read_only_page_follows_the_table(tab, refresh):
    assert (REFRESH in text_of(Client(change_data=make_change_data()), tab)) is refresh


@pytest.mark.parametrize("tab", ["change", "claims"])
def test_the_change_and_claims_tabs_have_no_refresh_even_when_clipped_or_without_a_change(tab):
    assert REFRESH not in text_of(Client(detail=dict(DETAIL, change=None)), tab)


# ---------------------------------------------------------------- 書込みが使えるとき

def _rig(detail=None, change_data="default", **kwargs) -> Rig:
    rig = Rig(**kwargs)
    rig.client.detail = Reply(200, dict(LONG, change=make_change()) if detail is None else detail, None)
    data = make_change_data() if change_data == "default" else change_data
    if data is not None:
        rig.client.approval_change = lambda approval_id, data=data: Reply(200, data, None)
    return rig


def _page(rig, tab=None, writable=True, method="GET"):
    request = rig.request(method, "/approvals/7")
    if tab is not None:
        request = dataclasses.replace(request, query={"tab": tab})
    response = rig.send(request, writable=writable)
    assert response.status == 200
    return response.body.decode("utf-8")


def test_the_form_and_the_block_are_only_in_the_overview_and_the_refresh_follows_the_table():
    rig = _rig()
    expected = {"overview": (True, False), "result": (False, True),
                "change": (False, False), "claims": (False, False)}
    for tab, (form, refresh) in expected.items():
        page = _page(rig, tab)
        assert ("<form" in page) is form and (BLOCK_MARK in page) is form, tab
        assert (REFRESH in page) is refresh, tab


def _without(key, value):
    detail = dict(LONG, change=make_change())
    if value is None:
        del detail[key]
    else:
        detail[key] = value
    return detail


@pytest.mark.parametrize("detail", [
    _without("status", "approved"), _without("kind", "disable"), _without("payload_sha256", "xyz"),
    _without("payload_sha256", None)],
    ids=["not-pending", "not-plugin", "bad-digest", "no-digest"])
def test_each_condition_of_the_form_removed_gives_neither_block_nor_form_and_keeps_the_refresh(detail):
    rig = _rig(detail=detail)
    page = _page(rig)
    assert "<form" not in page and BLOCK_MARK not in page and "<h2>決定</h2>" not in page
    assert REFRESH in page


def test_with_write_off_there_is_no_block_no_form_and_the_refresh_stays():
    page = _page(_rig(), writable=False)
    assert "<form" not in page and BLOCK_MARK not in page and REFRESH in page


def test_a_path_that_does_not_allow_writes_gets_no_block_no_form_and_the_refresh_stays():
    rig = _rig(loopback=False, tls=False)
    page = _page(rig)
    assert "<form" not in page and BLOCK_MARK not in page and "承認の内容" not in page
    assert REFRESH in page


def test_tls_alone_is_enough_for_the_form():
    assert "<form" in _page(_rig(loopback=False, tls=True))


def _clipping(monkeypatch):
    monkeypatch.setattr(render, "OTHER_BUDGET_BYTES", 10 ** 6)
    monkeypatch.setattr(render, "DIFF_BUDGET_BYTES", 10 ** 6)
    monkeypatch.setattr(render, "FILE_QUOTA_BYTES", 10 ** 6)
    lines = ["+" + "a" * 70] * 1000
    return make_change_data(files=[file_entry("plugin.py", lines)])


def test_a_clipped_virtual_page_clips_every_tab_and_gets_no_form(monkeypatch):
    data = _clipping(monkeypatch)
    assert render.compose_approval_page(dict(DETAIL), data["change"], data).clipped
    rig = _rig(change_data=data)
    for tab in TABS:
        page = _page(rig, tab)
        assert "Output clipped" in page and "<form" not in page and BLOCK_MARK not in page, tab
        assert ("<h3>plugin.py</h3>" in page) is False
        assert (REFRESH in page) is (tab in ("overview", "result")), tab


def test_the_same_page_one_byte_under_the_limit_is_served_with_the_form(monkeypatch):
    data = _clipping(monkeypatch)
    built = render.compose_approval_page(dict(DETAIL), data["change"], data)
    # 仮想ページが 64 KiB 未満に収まるまで 1 行ずつ減らす
    lines = len(data["diff"]["files"][0]["hunks"][0]["lines"])
    while built.clipped:
        lines -= 100
        assert lines > 0
        data = make_change_data(files=[file_entry("plugin.py", ["+" + "a" * 70] * lines)])
        built = render.compose_approval_page(dict(DETAIL), data["change"], data)
    page = _page(_rig(change_data=data))
    assert "Output clipped" not in page and "<form" in page and REFRESH not in page


def test_the_block_counts_toward_the_limit_with_the_form(monkeypatch):
    rig = _rig()
    with_form = _page(rig)
    assert "<form" in with_form
    plain = _page(rig, writable=False)
    assert REFRESH in plain
    # 同じ概要で、form を足すと limit を超えるときは、足さずに再読込を残す
    refresh_bytes = len(f'<meta http-equiv="refresh" content="{render.REFRESH_SECONDS}">')
    form_bytes = len(with_form.encode("utf-8")) - (len(plain.encode("utf-8")) - refresh_bytes)
    assert form_bytes > 1000  # ブロックを含む
    monkeypatch.setattr(render, "MAX_RESPONSE_BYTES", len(with_form.encode("utf-8")) - 1)
    blocked = _page(rig)
    assert "<form" not in blocked and BLOCK_MARK not in blocked and REFRESH in blocked
    monkeypatch.setattr(render, "MAX_RESPONSE_BYTES", len(with_form.encode("utf-8")))
    assert "<form" in _page(rig)


def test_the_omission_note_comes_before_the_remaining_cautions_and_the_form(monkeypatch):
    big = "<section><h2>大</h2>" + "g" * 20000 + "</section>"
    small = "<section><h2>残った注意</h2><p>x</p></section>"
    monkeypatch.setattr(render, "_EXTRA_BUILDERS", (("overview", lambda data: big),
                                                    ("overview", lambda data: small)))
    page = _page(_rig())
    assert "g" * 100 not in page
    note = page.index(render.EXTRA_OMITTED_SECTION)
    assert note < page.index("残った注意") < page.index("<h2>決定</h2>") < page.index("<form")


# ---------------------------------------------------------------- 決定のブロック

def _block(page: str) -> str:
    start = page.index("<h2>決定</h2>")
    return page[start:page.index("<form", start)]


def test_the_block_is_in_the_order_of_the_design_and_ends_with_the_forms():
    page = _page(_rig())
    block = _block(page)
    marks = ["承認の内容 (payload_sha256)", "候補 (artifact)", "比較対象", "分類", "計算時刻",
             BLOCK_MARK, "params の差分", "diff 表示", "申告: あり", "?tab=result", "?tab=change",
             "?tab=claims", "承認が結び付くのは承認の内容"]
    positions = [block.index(mark) for mark in marks]
    assert positions == sorted(positions)
    assert page.index("<h2>決定</h2>") > page.index("<h2>概要</h2>")
    assert page.count("<form") == 2 and page.endswith("</body></html>")
    assert "比較対象は含まない" in block and "配備中の版が変わると変わる" in block


def test_the_block_shows_the_first_twelve_digits_of_the_payload_and_the_candidate():
    rig = _rig()
    block = _block(_page(rig))
    assert f"<dt>承認の内容 (payload_sha256)</dt><dd>{DIGEST[:12]}</dd>" in block
    assert f"<dt>候補 (artifact)</dt><dd>{'a1' * 6}</dd>" in block
    assert DIGEST[:13] not in block and ("a1" * 7) not in block
    confirm = rig.post("/approvals/7/approve", {"payload_sha256": DIGEST}).body.decode("utf-8")
    assert f"<dd>{DIGEST[:12]}</dd>" in confirm


def test_the_block_repeats_the_comparison_and_the_reason():
    change = make_change(reason="hash_mismatch", classification="unknown", baseline_hash=None)
    block = _block(_page(_rig(detail=dict(LONG, change=change),
                              change_data=make_change_data(change=change))))
    assert "<dt>分類</dt><dd>不明</dd>" in block and "<dt>比較対象</dt><dd>-</dd>" in block
    assert render.REASON_TEXT["hash_mismatch"] in block


def test_the_block_of_an_approval_without_a_change_is_the_fixed_sentence():
    rig = _rig(detail=dict(LONG), change_data=None)
    block = _block(_page(rig))
    assert render.UNCHANGED_BLOCK_TEXT in block
    for gone in (BLOCK_MARK, "候補 (artifact)", "?tab=result", "<dt>比較対象"):
        assert gone not in block
    assert f"<dd>{DIGEST[:12]}</dd>" in block and "承認が結び付くのは" in block


def _scale(page: str) -> list[str]:
    return re.findall(r"<li>(.*?)</li>", _block(page))


def test_the_scale_counts_come_from_the_virtual_page_numbers():
    items = [{"key": f"params.k{i}", "old": "1", "new": "2"} for i in range(3)]
    change = make_change(params={"items": items, "omitted": 4, "invalid_keys": 2,
                                 "flatten_omitted": 1, "reason": None})
    files = [file_entry("plugin.py", ["+a", "+b", "+c"], omitted=5),
             file_entry("config.yaml", [" a"], "unchanged"), file_entry("test_plugin.py", ["-y"])]
    data = make_change_data(files, change)
    built = render.compose_approval_page(dict(DETAIL), data["change"], data)
    shown = sum(plan.shown for plan in built.plans)
    omitted = sum(plan.omitted for plan in built.plans)
    assert built.params_total == 3 and omitted == 5
    got = _scale(_page(_rig(detail=dict(LONG, change=change), change_data=data)))
    assert got == [f"params の差分 3 件 (ほか 4 件、形に合わない key 2 件、比較していない key 1 件)",
                   f"diff 表示 {shown} 行・省略 {omitted} 行", "申告: あり"]


def test_when_only_the_change_endpoint_fails_the_diff_and_the_claims_are_not_available():
    rig = _rig(change_data=None)
    got = _scale(_page(rig))
    assert got[1:] == ["diff: 取得できない", "申告: 取得できない"]
    assert "0 行" not in _block(_page(rig))


def test_a_redacted_file_is_counted_as_hidden_and_not_as_zero_lines_or_no_difference():
    only = make_change_data([file_entry("plugin.py", None, "redacted"),
                             file_entry("config.yaml", None, "redacted")])
    block = _block(_page(_rig(change_data=only)))
    assert "diff 非表示 2 ファイル" in block and "0 行" not in block and "差分なし" not in block
    mixed = make_change_data([file_entry("plugin.py", None, "redacted"),
                              file_entry("config.yaml", ["+k: v"])])
    built = render.compose_approval_page(dict(DETAIL), mixed["change"], mixed)
    shown = sum(plan.shown for plan in built.plans if plan.kind == "pre")
    assert shown > 0
    assert f"diff 表示 {shown} 行・省略 0 行・非表示 1 ファイル" in _block(_page(_rig(change_data=mixed)))


def test_a_diff_that_is_really_empty_says_zero_lines():
    empty = make_change_data([file_entry(p, None, "unchanged")
                              for p in ("plugin.py", "config.yaml", "test_plugin.py")])
    assert "diff 表示 0 行・省略 0 行" in _block(_page(_rig(change_data=empty)))


@pytest.mark.parametrize("change,expected", [
    (make_change(classification="new", params=None), "params の差分: 取得できない"),
    (make_change(params={"items": [], "omitted": 0, "invalid_keys": 0, "flatten_omitted": 0,
                         "reason": None}), "params の差分 0 件"),
    (make_change(classification="new", params={"items": [], "omitted": 0, "invalid_keys": 0,
                                               "flatten_omitted": 0, "reason": None}),
     "params の差分: 前版なし"),
    (make_change(classification="unknown", params={"items": [], "omitted": 0, "invalid_keys": 0,
                                                   "flatten_omitted": 0, "reason": None}),
     "params の差分: 取得できない"),
    (make_change(params={"items": [], "omitted": 0, "invalid_keys": 0, "flatten_omitted": 0,
                         "reason": "redacted"}), "params の差分: 非表示"),
    (make_change(), "params の差分 1 件"),
])
def test_the_params_scale_is_a_number_or_unavailable_or_hidden(change, expected):
    data = make_change_data(change=change)
    assert _scale(_page(_rig(detail=dict(LONG, change=change), change_data=data)))[0] == expected


@pytest.mark.parametrize("claims,expected", [
    ({"summary": "S", "selection_rationale": None}, "申告: あり"),
    ({"summary": None, "selection_rationale": "R"}, "申告: あり"),
    ({"summary": None, "selection_rationale": None}, "申告: なし"),
    ({"summary": "", "selection_rationale": ""}, "申告: なし"),
])
def test_the_claims_scale_is_present_or_absent(claims, expected):
    change = make_change(summary=None)
    data = make_change_data(change=change, claims=claims)
    assert _scale(_page(_rig(detail=dict(LONG, change=change), change_data=data)))[2] == expected


def test_the_decision_forms_post_the_digest_the_overview_was_drawn_from():
    page = _page(_rig())
    assert page.count(f'name="payload_sha256" value="{DIGEST}"') == 2


def test_the_reevaluation_change_without_a_diff_still_reads_as_a_block_with_a_form():
    rig = _rig(detail=dict(LONG, change=REEVAL_CHANGE), change_data=None)
    page = _page(rig)
    assert "<form" in page and render.REASON_TEXT["unavailable"] not in _block(page)


# ---------------------------------------------------------------- 状態の印とカード

def _overview_cards(page: str) -> str:
    return re.search(r'<section><h2>概要</h2>(<div class="kpi">.*?</div></div>)', page, re.S).group(1)


@pytest.mark.parametrize(("status", "text"), [
    ("approved", "○ approved"), ("pending", "△ pending"), ("rejected", "– rejected"),
    ("expired", "– expired"), ("invalidated", "– invalidated")])
def test_the_overview_leads_with_the_status_card(status, text):
    detail = full_detail(status=status)
    row = _overview_cards(text_of(Client(detail=detail, change_data=make_change_data(
        change=make_change()))))
    assert "<strong>status</strong>" in row and text in row
    assert "<strong>plugin</strong>" in row and "<strong>分類</strong>" in row


@pytest.mark.parametrize("bad", ["paused", None, 1, [], True, "PENDING"])
def test_an_unrecognised_approval_status_is_an_unknown_mark(bad):
    row = _overview_cards(text_of(Client(detail=full_detail(status=bad))))
    assert '<span class="st">? 不明</span>' in row and "○" not in row


def test_no_performance_verdict_card_is_shown_beside_the_decision_forms():
    page = text_of(Client(detail=full_detail(), change_data=make_change_data(change=make_change())))
    assert "成績" not in _overview_cards(page) and "verdict" not in _overview_cards(page)
    assert _overview_cards(page).count("<div><strong>") <= 3


def test_the_other_tabs_have_no_cards():
    for tab in ("change", "claims", "result"):
        assert 'class="kpi"' not in text_of(Client(detail=full_detail()), tab)
