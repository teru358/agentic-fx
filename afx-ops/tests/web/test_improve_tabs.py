"""Improve の一覧・詳細・実行のタブ、一覧の並べ替えと件数の注記、query の検査。"""
from __future__ import annotations

import re

import pytest

from afx_ops.api_client import Reply
from afx_ops.web import improve_pages, render, tabs
from afx_ops.web.request import Request, handle_request

from test_improve_pages import Client, dto, run_row, wide_runs

FULL_SECTIONS = {
    "overview": ["概要"],
    "agent": ["仮説", "agent の説明", "観察文", "判定理由", "試したもの"],
    "result": ["床との差", "配備中の版との比較 (holdout)"],
    "handoff": ["次回への申し送り (系統の試行結果)"],
}
REFRESH = f'<meta http-equiv="refresh" content="{render.REFRESH_SECONDS}">'


def get(path, query=None, client=None, method="GET"):
    client = client if client is not None else Client()
    return handle_request(Request(method, path, query or {}), client), client


def text_of(path, query=None, client=None) -> str:
    response, _ = get(path, query, client)
    assert response.status == 200, (path, query, response.status)
    return response.body.decode()


def headings(page: str) -> list[str]:
    return re.findall(r"<h2>([^<]*)</h2>", page)


def bar_of(page: str) -> str:
    return re.search(r'<nav class="tabs">(.*?)</nav>', page).group(1)


def full_detail() -> dict:
    data = dto("canary_improved")
    items = data["summary"]["items"]
    items["observation"] = "obs"
    items["parent_reason"] = "unprofitable"
    items["handoff"] = {"family": "fam", "attempts": 1, "outcomes": {"not_submitted": 1}}
    return data


# ---------------------------------------------------------------- 帯

def test_the_improve_tab_table_names_the_tabs_and_marks_the_run_tab_write_only():
    assert tabs.tab_names("/improve") == ("runs",)
    assert tabs.tab_names("/improve", writable=True) == ("runs", "run")
    assert tabs.default_tab("/improve") == "runs"
    assert tabs.tab_names(tabs.IMPROVE_DETAIL) == ("overview", "agent", "result", "handoff")
    assert tabs.default_tab(tabs.IMPROVE_DETAIL) == "overview"


def test_the_read_only_list_bar_has_only_the_history_tab_and_carries_no_other_key():
    page = text_of("/improve", {"limit": "100", "sort": "run", "dir": "asc"})
    assert re.findall(r'<a href="([^"]*)"( aria-current="page")?>([^<]*)', bar_of(page)) == [
        ("/improve?tab=runs", ' aria-current="page"', "履歴")]
    assert page.index("<h1>") < page.index('<nav class="tabs">')


def test_the_detail_bar_marks_the_current_tab_and_keeps_the_nav_paragraph_above_it():
    page = text_of("/improve/7", {"tab": "result"}, Client(detail=full_detail()))
    assert re.findall(r'<a href="([^"]*)"( aria-current="page")?>([^<]*)', bar_of(page)) == [
        ("/improve/7?tab=overview", "", "概要"), ("/improve/7?tab=agent", "", "agent"),
        ("/improve/7?tab=result", ' aria-current="page"', "成績"),
        ("/improve/7?tab=handoff", "", "申し送り")]
    assert page.index('<a href="/improve">Improve (一覧)</a>') < page.index('<nav class="tabs">')


def test_a_missing_tab_and_the_default_tab_give_the_same_body():
    assert get("/improve")[0].body.replace(b'aria-current="page"', b"") == get(
        "/improve", {"tab": "runs"})[0].body.replace(b'aria-current="page"', b"")
    assert get("/improve")[0].body == get("/improve", {"tab": "runs"})[0].body
    client = Client(detail=full_detail())
    assert get("/improve/7", client=client)[0].body == get(
        "/improve/7", {"tab": "overview"}, client)[0].body


# ---------------------------------------------------------------- 詳細の振り分け

@pytest.mark.parametrize("tab", list(FULL_SECTIONS))
def test_each_detail_tab_shows_its_sections_in_order(tab):
    page = text_of("/improve/7", {"tab": tab}, Client(detail=full_detail()))
    assert headings(page) == FULL_SECTIONS[tab]


def test_every_section_of_the_detail_appears_in_exactly_one_tab():
    client = Client(detail=full_detail())
    seen = [title for tab in FULL_SECTIONS
            for title in headings(text_of("/improve/7", {"tab": tab}, client))]
    assert sorted(seen) == sorted(title for titles in FULL_SECTIONS.values() for title in titles)
    assert len(seen) == len(set(seen))


def test_the_renderer_table_and_the_tab_table_name_the_same_tabs():
    names = []
    for tab in tabs.tab_names(tabs.IMPROVE_DETAIL):
        names.append(headings(text_of("/improve/7", {"tab": tab}, Client(detail=full_detail()))))
    assert [bool(item) for item in names] == [True, True, True, True]


def test_a_detail_without_a_summary_puts_the_notice_in_the_overview_and_none_elsewhere():
    data = dto("submitted")
    data["summary"] = None
    client = Client(detail=data)
    overview = text_of("/improve/7", {"tab": "overview"}, client)
    assert headings(overview) == ["概要", "要約"] and improve_pages.NO_SUMMARY_TEXT in overview
    for tab in ("agent", "result", "handoff"):
        page = text_of("/improve/7", {"tab": tab}, client)
        assert "<p>なし</p>" in page and headings(page) == []


def test_a_detail_without_a_terminal_record_has_the_fixed_text_only_in_the_overview():
    data = {"id": 7, "backend": "local", "snapshot": None, "summary": None, "floor": None,
            "report_state": "none", "approval": None, "display_timezone": "UTC"}
    client = Client(detail=data)
    overview = text_of("/improve/7", {"tab": "overview"}, client)
    assert improve_pages.NO_TERMINAL_TEXT in overview and headings(overview) == ["概要"]
    for tab in ("agent", "result", "handoff"):
        page = text_of("/improve/7", {"tab": tab}, client)
        assert improve_pages.NO_TERMINAL_TEXT not in page and "<p>なし</p>" in page
        assert bar_of(page).count("<a ") == 4


def test_the_floor_from_the_report_belongs_to_the_result_tab():
    data = dto("unprofitable_holdout")
    data["summary"] = None
    data["floor"] = {"stages": [{"stage": "holdout", "pairs": [
        {"pair": "USDJPY", "trades": 12, "pf": 0.8, "avg_r": -0.1, "evaluable": True,
         "conditions": ["pf"]}]}]}
    client = Client(detail=data)
    assert "床との差 (レポートから)" in text_of("/improve/7", {"tab": "result"}, client)
    assert "床との差 (レポートから)" not in text_of("/improve/7", {"tab": "overview"}, client)


# ---------------------------------------------------------------- query

LIST_BAD = [
    {"tab": "nope"}, {"tab": ""}, {"tab": "Runs"}, {"x": "1"}, {"tab": "runs", "x": "1"},
    {"tab": "nope", "x": "1"}, {"sort": "nope"}, {"sort": "Run"}, {"sort": ""}, {"dir": "asc"},
    {"sort": "run", "dir": "up"}, {"sort": "run", "dir": ""}, {"limit": "0"}, {"limit": "101"},
    {"limit": "x"}, {"limit": "-1"}, {"after": "3"}, {"limit": "5", "n": "1"},
    {"sort": "name"}, {"category": "NEWS"},
]
DETAIL_BAD = [{"tab": "nope"}, {"tab": ""}, {"tab": "run"}, {"tab": "runs"}, {"x": "1"},
              {"limit": "5"}, {"sort": "run"}, {"tab": "overview", "x": "1"},
              {"tab": "nope", "x": "1"}]


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path,query", [("/improve", q) for q in LIST_BAD]
                         + [("/improve/7", q) for q in DETAIL_BAD])
def test_a_query_outside_the_table_is_400_and_never_reaches_the_daemon(path, query, method):
    client = Client(detail=full_detail())
    response, _ = get(path, query, client, method)
    assert response.status == 400 and client.calls == []
    assert b"nope" not in response.body and b'class="tabs"' not in response.body
    assert response.body == b"" or method == "GET"


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_the_run_tab_is_404_without_write_and_never_reaches_the_daemon(method):
    client = Client()
    response, _ = get("/improve", {"tab": "run"}, client, method)
    assert response.status == 404 and client.calls == []


def test_good_queries_reach_the_daemon_once_and_head_matches_get():
    for path, query in (("/improve", {"tab": "runs", "limit": "20", "sort": "started",
                                      "dir": "asc"}),
                        ("/improve", {"sort": "target"}),
                        ("/improve/7", {"tab": "handoff"})):
        got, client = get(path, query, Client(detail=full_detail()))
        head, _ = get(path, query, Client(detail=full_detail()), "HEAD")
        assert got.status == head.status == 200 and len(client.calls) == 1
        assert head.headers["Content-Length"] == got.headers["Content-Length"] == str(len(got.body))
        assert head.body == b""


# ---------------------------------------------------------------- 再読込

def test_every_reading_tab_keeps_the_refresh():
    assert REFRESH in text_of("/improve")
    client = Client(detail=full_detail())
    for tab in tabs.tab_names(tabs.IMPROVE_DETAIL):
        assert REFRESH in text_of("/improve/7", {"tab": tab}, client)


# ---------------------------------------------------------------- 並べ替え

def sortable_runs() -> list[dict]:
    def row(tag, **fields):
        base = {"started_at": None, "finished_at": None, "trigger": None, "result_code": None,
                "reason_code": None, "plugin": None, "failed_stage": None,
                "failed_conditions": None, "failed_pair_count": None, "approval_id": tag}
        base.update(fields)
        return base

    return [
        row(1, id=6, started_at="2026-11-01T09:00:00+09:00", finished_at="2026-11-01T00:30:00+00:00",
            trigger="scheduled", result_code="gate_failed", reason_code="unprofitable", plugin="m"),
        row(2, id=5, started_at="2026-11-01T00:00:00+00:00", finished_at="2026-11-01T00:10:00+00:00",
            trigger="manual", result_code="gate_failed", plugin="c"),
        row(3, id=4, started_at="2026-10-31T23:00:00+00:00", trigger="manual",
            result_code="failed", reason_code="timeout"),
        row(4, id=0, started_at="2026-11-03T00:00:00", finished_at="garbage",
            result_code="submitted", plugin="a"),
        row(5, id=True, started_at="2026-11-02T00:00:00+00:00",
            finished_at="2026-11-02T00:10:00+00:00", trigger="scheduled",
            result_code="gate_failed", reason_code="duplicate_logic", plugin="z"),
        row(6, id=3, started_at="2026-11-01T00:00:00Z", finished_at="2026-11-01T00:30:00+00:00",
            trigger="manual", result_code="gate_failed", reason_code="unprofitable", plugin="m"),
    ]


def order_of(page: str) -> list[int]:
    table = page.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    return [int(tag) for tag in re.findall(r'href="/approvals/(\d+)"', table)]


def sorted_order(sort=None, direction=None, runs=None) -> list[int]:
    query = {}
    if sort is not None:
        query["sort"] = sort
    if direction is not None:
        query["dir"] = direction
    listing = {"runs": runs if runs is not None else sortable_runs(), "has_more": False}
    return order_of(text_of("/improve", query, Client(listing=listing)))


EXPECTED = {
    ("run", "desc"): [1, 2, 3, 6, 4, 5], ("run", "asc"): [4, 6, 3, 2, 1, 5],
    ("started", "desc"): [5, 1, 2, 6, 3, 4], ("started", "asc"): [3, 1, 2, 6, 5, 4],
    ("finished", "desc"): [5, 1, 6, 2, 3, 4], ("finished", "asc"): [2, 1, 6, 5, 3, 4],
    ("trigger", "asc"): [2, 3, 6, 1, 5, 4], ("trigger", "desc"): [1, 5, 2, 3, 6, 4],
    ("result", "asc"): [3, 5, 1, 6, 2, 4], ("result", "desc"): [4, 1, 6, 5, 2, 3],
    ("target", "asc"): [4, 2, 1, 6, 5, 3], ("target", "desc"): [5, 1, 6, 2, 4, 3],
}


def test_the_expected_table_covers_every_column_and_direction():
    assert set(EXPECTED) == {(key, d) for key in improve_pages.SORT_COLUMNS for d in ("asc", "desc")}


@pytest.mark.parametrize("sort,direction", sorted(EXPECTED))
def test_a_column_sorts_in_both_directions_with_stable_ties_and_missing_last(sort, direction):
    assert sorted_order(sort, direction) == EXPECTED[(sort, direction)]


@pytest.mark.parametrize("sort", sorted(improve_pages.SORT_COLUMNS))
def test_a_sort_without_a_direction_uses_the_first_direction_of_the_column(sort):
    first = improve_pages.SORT_COLUMNS[sort].first
    assert sorted_order(sort) == EXPECTED[(sort, first)]


def test_without_a_sort_the_daemon_order_is_kept_and_no_header_is_marked():
    page = text_of("/improve", client=Client(listing={"runs": sortable_runs(), "has_more": False}))
    assert order_of(page) == [1, 2, 3, 4, 5, 6]
    assert "▲" not in page and "▼" not in page
    assert "新しい順に並べています" in page


def test_zero_is_a_value_and_a_bool_or_a_naive_time_is_not():
    runs = [dict(run_row(1), id=True, approval_id=1), dict(run_row(2), id=0, approval_id=2),
            dict(run_row(3), id=7, approval_id=3)]
    assert sorted_order("run", "asc", runs) == [2, 3, 1]
    assert sorted_order("run", "desc", runs) == [3, 2, 1]
    times = [dict(run_row(1), started_at="2026-11-01T00:00:00", approval_id=1),
             dict(run_row(2), started_at="2026-11-01T00:00:00+00:00", approval_id=2),
             dict(run_row(3), started_at=5, approval_id=3)]
    assert sorted_order("started", "asc", times) == [2, 1, 3]
    assert sorted_order("started", "desc", times) == [2, 1, 3]


def test_a_row_without_a_reason_follows_the_rows_with_one_in_both_directions():
    runs = [dict(run_row(1), reason_code=None, approval_id=1),
            dict(run_row(2), reason_code="b_reason", approval_id=2),
            dict(run_row(3), reason_code="a_reason", approval_id=3)]
    assert sorted_order("result", "asc", runs) == [3, 2, 1]
    assert sorted_order("result", "desc", runs) == [2, 3, 1]


def header_links(page: str) -> dict[str, tuple[str, str]]:
    head = page.split("<thead>", 1)[1].split("</thead>", 1)[0]
    return {text.rstrip("▲▼"): (href, text) for href, text in re.findall(
        r'<a href="([^"]*)">([^<]*)</a>', head)}


def test_the_headers_link_to_sorts_mark_the_current_column_and_flip_its_direction():
    listing = {"runs": sortable_runs(), "has_more": False}
    page = text_of("/improve", {"sort": "started", "dir": "desc"}, Client(listing=listing))
    links = header_links(page)
    assert set(links) == {"run", "開始", "終了", "起動", "結果", "対象"}
    assert links["開始"] == ("/improve?sort=started&amp;dir=asc", "開始▼")
    assert links["run"] == ("/improve?sort=run&amp;dir=desc", "run")
    assert links["終了"][0] == "/improve?sort=finished&amp;dir=desc"
    assert links["起動"][0] == "/improve?sort=trigger&amp;dir=asc"
    asc = text_of("/improve", {"sort": "trigger", "dir": "asc"}, Client(listing=listing))
    assert header_links(asc)["起動"] == ("/improve?sort=trigger&amp;dir=desc", "起動▲")


def test_the_sort_links_carry_the_limit_and_the_tab_links_carry_nothing():
    listing = {"runs": sortable_runs(), "has_more": False}
    page = text_of("/improve", {"limit": "100", "sort": "result"}, Client(listing=listing))
    assert header_links(page)["結果"][0] == "/improve?sort=result&amp;dir=desc&amp;limit=100"
    assert header_links(page)["run"][0] == "/improve?sort=run&amp;dir=desc&amp;limit=100"
    assert '<a href="/improve?tab=runs" aria-current="page">履歴</a>' in page
    plain = text_of("/improve", {"sort": "result"}, Client(listing=listing))
    assert "limit=" not in header_links(plain)["run"][0]


def test_the_description_says_how_the_fetched_rows_are_sorted():
    listing = {"runs": sortable_runs(), "has_more": False}
    page = text_of("/improve", {"sort": "finished", "dir": "asc"}, Client(listing=listing))
    assert "取得した最新 6 件を 終了 の 昇順 で並べています" in page
    page = text_of("/improve", {"sort": "run"}, Client(listing=listing))
    assert "取得した最新 6 件を run の 降順 で並べています" in page


def test_the_budget_cuts_the_tail_of_the_sorted_order():
    runs = wide_runs(improve_pages.ROW_LIMIT)
    for index, row in enumerate(runs):
        row["approval_id"] = index + 1
    body = text_of("/improve", {"sort": "run", "dir": "asc"},
                   Client(listing={"runs": runs, "has_more": False}))
    shown = order_of(body)
    assert 0 < len(shown) < len(runs)
    assert shown == sorted(shown, key=lambda tag: runs[tag - 1]["id"])
    assert shown[0] == improve_pages.ROW_LIMIT


# ---------------------------------------------------------------- 件数の注記

def notes(page: str) -> str:
    return page.split("</div>", 1)[1]


@pytest.mark.parametrize("limit", [50, 100])
@pytest.mark.parametrize("has_more", [False, True])
@pytest.mark.parametrize("omitted", [False, True])
def test_the_count_note_for_each_limit_and_has_more_and_byte_omission(limit, has_more, omitted):
    runs = wide_runs(improve_pages.ROW_LIMIT if omitted else 3)
    body = improve_pages.render_improve_list({"runs": runs, "has_more": has_more},
                                             limit=limit).decode()
    fetched = len(runs)
    shown = body.split("<tbody>", 1)[1].split("</tbody>", 1)[0].count("<tr>")
    assert (shown < fetched) is omitted
    tail = notes(body)
    cli = "これより古い run は afx ctl improve runs で確認してください"
    if not has_more and not omitted:
        assert "取得した最新" not in tail and "さらに古い" not in tail
    elif omitted:
        assert f"取得した最新 {fetched} 件のうち {shown} 件を表示。{cli}" in tail
        assert "さらに古い" not in tail and "href=" not in tail
    elif limit == 50:
        assert (f"取得した最新 3 件のうち 3 件を表示。"
                f'<a href="/improve?limit=100">さらに古い run を表示 (100 件まで)</a>') in tail
    else:
        assert f"取得した最新 3 件のうち 3 件を表示。{cli}" in tail and "href=" not in tail


def test_the_older_link_carries_the_sort_and_direction_with_escaped_ampersands():
    listing = {"runs": [run_row(3)], "has_more": True}
    page = text_of("/improve", {"limit": "20", "sort": "started"}, Client(listing=listing))
    assert 'href="/improve?limit=40&amp;sort=started&amp;dir=desc"' in page
    page = text_of("/improve", {"sort": "target", "dir": "desc"}, Client(listing=listing))
    assert 'href="/improve?limit=100&amp;sort=target&amp;dir=desc"' in page
    page = text_of("/improve", {}, Client(listing=listing))
    assert 'href="/improve?limit=100"' in page and "sort=" not in notes(page)


# ---------------------------------------------------------------- 予算

@pytest.mark.parametrize("sort,direction", [(None, None), ("run", "asc"), ("result", "desc"),
                                            ("target", "asc")])
@pytest.mark.parametrize("count", [125, 130, 135, 150, 200])
def test_a_list_packed_to_the_budget_never_overflows_and_is_never_clipped(count, sort, direction):
    for has_more in (False, True):
        body = improve_pages.render_improve_list(
            {"runs": wide_runs(count), "has_more": has_more}, limit=100, sort=sort,
            direction=direction, writable=True)
        assert len(body) <= render.MAX_RESPONSE_BYTES and b"Output clipped" not in body
        assert b'class="scroll"' in body and "実行".encode() in body


def test_the_frame_wraps_the_table_and_the_bar_is_counted_in_the_budget():
    runs = wide_runs(improve_pages.ROW_LIMIT)
    plain = improve_pages.render_improve_list({"runs": runs, "has_more": False})
    with_run = improve_pages.render_improve_list({"runs": runs, "has_more": False}, writable=True)
    rows = lambda body: body.decode().split("<tbody>", 1)[1].split("</tbody>", 1)[0].count("<tr>")
    assert rows(plain) >= rows(with_run) > 0
    page = plain.decode()
    assert re.search(r'<div class="scroll"><table>.*</table>(<p>[^<]*</p>)?</div>', page)
