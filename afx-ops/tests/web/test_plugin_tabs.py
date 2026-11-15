"""Plugins・承認一覧・plugin 詳細のタブの帯、plugin 一覧の並べ替えと query の検査。"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from afx_ops.api_client import Reply
from afx_ops.web import plugin_pages, render, tabs
from afx_ops.web.request import Request, handle_request

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NAMES = ("rsi", "mom", "brk", "bol", "junk", "ghost")


def sample(key: str) -> dict:
    return json.loads((FIXTURES / f"{key}.json").read_text())


class Client:
    def __init__(self, listing=None, details=None):
        self.listing = listing if listing is not None else sample("plugins_list")
        self.details = details or {name: sample(f"plugin_{name}") for name in NAMES}
        self.calls = []

    def plugins(self):
        self.calls.append("plugins")
        return Reply(200, self.listing, None)

    def plugin(self, name):
        self.calls.append(("plugin", name))
        return Reply(200, self.details[name], None)

    def approvals(self, *, limit):
        self.calls.append(("approvals", limit))
        return Reply(200, {"approvals": [], "open_journals": []}, None)


def get(path, query=None, client=None, method="GET"):
    client = client or Client()
    return handle_request(Request(method, path, query or {}), client), client


def headings(page, level="h2"):
    return re.findall(rf"<{level}>([^<]*)</{level}>", page)


def bar_of(page):
    return re.search(r'<nav class="tabs">(.*?)</nav>', page).group(1)


# ---------------------------------------------------------------- 帯

def test_the_plugins_bar_has_the_list_and_a_link_to_the_approvals():
    page = get("/plugins")[0].body.decode()
    assert re.findall(r'<a href="([^"]*)"( aria-current="page")?>([^<]*)', bar_of(page)) == [
        ("/plugins?tab=list", ' aria-current="page"', "一覧"), ("/approvals", "", "承認待ち")]


def test_the_approvals_page_shows_the_same_bar_with_the_approvals_as_current():
    page = get("/approvals")[0].body.decode()
    assert re.findall(r'<a href="([^"]*)"( aria-current="page")?>([^<]*)', bar_of(page)) == [
        ("/plugins?tab=list", "", "一覧"), ("/approvals", ' aria-current="page"', "承認待ち")]
    assert '<a href="/plugins" aria-current="page">Plugins</a>' in page


def test_a_missing_tab_and_the_default_tab_give_the_same_list():
    assert get("/plugins")[0].body == get("/plugins", {"tab": "list"})[0].body


def test_the_tab_table_of_plugins_writes_only_the_list_as_a_tab_value():
    assert tabs.tab_names("/plugins") == ("list",)
    assert tabs.default_tab("/plugins") == "list"
    assert tabs.tab_names("/plugins/{name}") == (
        "overview", "approvals", "deps", "usefulness", "versions")


DETAIL_SECTIONS = {
    "overview": ["概要"],
    "approvals": ["承認待ち", "承認の履歴"],
    "deps": ["参照する strategy"],
    "usefulness": ["有用性"],
    "versions": ["版の履歴"],
}


@pytest.mark.parametrize("tab,expected", DETAIL_SECTIONS.items())
def test_each_indicator_detail_tab_shows_only_its_sections_in_order(tab, expected):
    response, _ = get("/plugins/rsi", {"tab": tab})
    assert response.status == 200
    assert headings(response.body.decode()) == expected


def test_the_dependency_tab_of_a_strategy_and_of_an_indicator():
    assert headings(get("/plugins/brk", {"tab": "deps"})[0].body.decode()) == ["使う indicator"]
    assert headings(get("/plugins/rsi", {"tab": "deps"})[0].body.decode()) == ["参照する strategy"]


def test_every_current_section_appears_in_exactly_one_tab():
    seen = []
    for tab in tabs.tab_names("/plugins/{name}"):
        seen += headings(get("/plugins/rsi", {"tab": tab})[0].body.decode())
    assert sorted(seen) == sorted(["概要", "承認待ち", "参照する strategy", "有用性", "版の履歴",
                                   "承認の履歴"])
    seen = []
    for tab in tabs.tab_names("/plugins/{name}"):
        seen += headings(get("/plugins/brk", {"tab": tab})[0].body.decode())
    assert sorted(seen) == sorted(["概要", "使う indicator", "承認の履歴"] + (
        ["版の履歴"] if sample("plugin_brk")["plugin"]["versions"] else []))


def test_an_empty_detail_tab_says_none_and_the_bar_is_still_complete():
    response, _ = get("/plugins/ghost", {"tab": "usefulness"})
    page = response.body.decode()
    assert "<p>なし</p>" in page
    assert re.findall(r">([^<]*)</a>", bar_of(page)) == ["概要", "承認", "依存", "有用性", "版の履歴"]


def test_the_detail_bar_uses_the_plugin_path_and_the_fixed_table():
    page = get("/plugins/rsi", {"tab": "deps"})[0].body.decode()
    assert re.findall(r'<a href="([^"]*)"( aria-current="page")?>([^<]*)', bar_of(page)) == [
        ("/plugins/rsi?tab=overview", "", "概要"), ("/plugins/rsi?tab=approvals", "", "承認"),
        ("/plugins/rsi?tab=deps", ' aria-current="page"', "依存"),
        ("/plugins/rsi?tab=usefulness", "", "有用性"),
        ("/plugins/rsi?tab=versions", "", "版の履歴")]


def test_the_page_nav_paragraph_stays_above_the_bar():
    page = get("/plugins/rsi")[0].body.decode()
    nav = '<p><a href="/plugins">Plugins</a> / <a href="/approvals">Pending approvals</a></p>'
    assert page.index(nav) < page.index('<nav class="tabs">')


def test_a_missing_detail_tab_and_the_default_tab_give_the_same_body():
    assert get("/plugins/rsi")[0].body == get("/plugins/rsi", {"tab": "overview"})[0].body


def test_the_detail_pages_keep_the_auto_refresh():
    for tab in tabs.tab_names("/plugins/{name}"):
        assert 'http-equiv="refresh"' in get("/plugins/rsi", {"tab": tab})[0].body.decode()


def test_every_tab_of_the_largest_detail_stays_in_the_limit():
    data = sample("plugin_brk")
    plugin = data["plugin"]
    long = "x" * 600
    plugin["dependencies"] = [dict(plugin["dependencies"][0], alias=long, plugin=f"d{i}")
                              for i in range(300)]
    plugin["history"] = [dict(plugin["history"][0], id=i + 1, mission_id=10 ** 12) for i in range(300)]
    plugin["versions"] = [dict(plugin["versions"][0], approval_id=i + 1) for i in range(300)]
    plugin["pending"] = [{"id": i + 1, "content_hash": "b" * 64, "kind": "strategy"} for i in range(300)]
    for tab in tabs.tab_names("/plugins/{name}"):
        response, _ = get("/plugins/brk", {"tab": tab}, Client(details={"brk": data}))
        assert response.status == 200 and len(response.body) <= render.MAX_RESPONSE_BYTES
        assert "Output clipped" not in response.body.decode(), tab


# ---------------------------------------------------------------- query の検査

BAD_QUERIES = [
    ("/plugins", {"tab": "approvals"}),
    ("/plugins", {"tab": "bogus"}),
    ("/plugins", {"extra": "1"}),
    ("/plugins", {"sort": "bogus"}),
    ("/plugins", {"sort": ""}),
    ("/plugins", {"dir": "asc"}),
    ("/plugins", {"sort": "name", "dir": "up"}),
    ("/plugins", {"sort": "name", "dir": ""}),
    ("/plugins", {"tab": "list", "sort": "name", "extra": "1"}),
    ("/plugins", {"sort": "NAME"}),
    ("/plugins/rsi", {"tab": "bogus"}),
    ("/plugins/rsi", {"tab": "list"}),
    ("/plugins/rsi", {"sort": "name"}),
    ("/plugins/rsi", {"extra": "1"}),
    ("/plugins/rsi", {"tab": "deps", "extra": "1"}),
    ("/plugins/rsi", {"tab": "bogus", "extra": "1"}),
    ("/approvals", {"tab": "list"}),
    ("/approvals", {"sort": "name"}),
]


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path,query", BAD_QUERIES)
def test_a_query_outside_the_table_is_400_without_a_daemon_call(path, query, method):
    response, client = get(path, query, method=method)
    assert response.status == 400
    assert client.calls == []


@pytest.mark.parametrize("path,query", [
    ("/plugins", {"tab": "list", "sort": "pending", "dir": "asc"}),
    ("/plugins", {"sort": "submitted"}),
    ("/plugins/rsi", {"tab": "versions"}),
])
def test_head_returns_the_same_status_and_length_as_get(path, query):
    got, _ = get(path, query)
    head, _ = get(path, query, method="HEAD")
    assert head.status == got.status == 200 and head.body == b""
    assert head.headers["Content-Length"] == got.headers["Content-Length"] == str(len(got.body))


# ---------------------------------------------------------------- 並べ替え

def listing_of(rows):
    listing = sample("plugins_list")
    base = copy.deepcopy(next(item for item in listing["plugins"] if item["name"] == "rsi"))
    listing["plugins"] = [dict(copy.deepcopy(base), **row) for row in rows]
    return listing


def names_in(page):
    return re.findall(r'<tr><td><a href="/plugins/([^"]*)">', page)


def order(query, rows):
    response, _ = get("/plugins", query, Client(listing=listing_of(rows)))
    assert response.status == 200
    return names_in(response.body.decode())


def entity(state):
    return {"state": state, "artifact_hash": None, "content_hash": None, "reason": None}


def useful(submitted, deployed):
    return {"submitted": submitted, "deployed": deployed, "by_pin": []}


def test_sort_by_name_and_kind_and_state_compare_the_code_strings():
    rows = [{"name": "b", "kind": "strategy", "entity": entity("symlink")},
            {"name": "a", "kind": "indicator", "entity": entity("absent")},
            {"name": "c", "kind": "indicator", "entity": entity("unreadable")}]
    assert order({"sort": "name"}, rows) == ["a", "b", "c"]
    assert order({"sort": "name", "dir": "desc"}, rows) == ["c", "b", "a"]
    assert order({"sort": "kind"}, rows) == ["a", "c", "b"]
    assert order({"sort": "kind", "dir": "desc"}, rows) == ["b", "a", "c"]
    assert order({"sort": "state"}, rows) == ["a", "b", "c"]
    assert order({"sort": "state", "dir": "desc"}, rows) == ["c", "b", "a"]


def test_sort_by_the_integer_columns_starts_descending_and_zero_is_a_value():
    rows = [{"name": "a", "pending_count": 0, "usefulness": useful(0, 5)},
            {"name": "b", "pending_count": 3, "usefulness": useful(2, 0)},
            {"name": "c", "pending_count": 1, "usefulness": useful(7, 1)}]
    assert order({"sort": "pending"}, rows) == ["b", "c", "a"]
    assert order({"sort": "pending", "dir": "desc"}, rows) == ["b", "c", "a"]
    assert order({"sort": "pending", "dir": "asc"}, rows) == ["a", "c", "b"]
    assert order({"sort": "submitted"}, rows) == ["c", "b", "a"]
    assert order({"sort": "submitted", "dir": "asc"}, rows) == ["a", "b", "c"]
    assert order({"sort": "deployed"}, rows) == ["a", "c", "b"]
    assert order({"sort": "deployed", "dir": "asc"}, rows) == ["b", "c", "a"]


def test_equal_values_keep_the_given_order_in_both_directions():
    rows = [{"name": n, "pending_count": 1} for n in ("q", "b", "z", "m")]
    assert order({"sort": "pending", "dir": "asc"}, rows) == ["q", "b", "z", "m"]
    assert order({"sort": "pending", "dir": "desc"}, rows) == ["q", "b", "z", "m"]
    assert order({"sort": "kind", "dir": "desc"}, rows) == ["q", "b", "z", "m"]


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_missing_and_wrong_typed_values_go_last_in_both_directions(direction):
    rows = [{"name": "t", "pending_count": True},
            {"name": "a", "pending_count": 2},
            {"name": "n", "pending_count": None},
            {"name": "s", "pending_count": "9"},
            {"name": "z", "pending_count": 0}]
    got = order({"sort": "pending", "dir": direction}, rows)
    assert got[:2] == (["a", "z"] if direction == "desc" else ["z", "a"])
    assert got[2:] == ["t", "n", "s"]


def test_usefulness_columns_have_no_value_for_a_non_indicator_or_a_bool():
    rows = [{"name": "s", "kind": "strategy", "usefulness": useful(9, 9)},
            {"name": "i", "kind": "indicator", "usefulness": useful(1, 1)},
            {"name": "b", "kind": "indicator", "usefulness": useful(True, True)},
            {"name": "m", "kind": "indicator", "usefulness": None}]
    for key in ("submitted", "deployed"):
        for direction in ("asc", "desc"):
            assert order({"sort": key, "dir": direction}, rows) == ["i", "s", "b", "m"]


def test_sorting_applies_before_the_budget_so_the_cut_takes_the_tail():
    base = [{"name": f"p{index:04d}", "pending_count": index % 7} for index in range(600)]
    response, _ = get("/plugins", {"sort": "pending"}, Client(listing=listing_of(base)))
    html = response.body.decode()
    shown = names_in(html)
    assert 0 < len(shown) < 600
    expected = [row["name"] for row in sorted(base, key=lambda r: -r["pending_count"])]
    assert shown == expected[:len(shown)]
    assert int(re.search(r"…\((\d+) 件省略", html).group(1)) == 600 - len(shown)


def head_cells(page):
    thead = re.search(r"<thead><tr>(.*?)</tr></thead>", page, re.S).group(1)
    return re.findall(r"<th>(.*?)</th>", thead, re.S)


def test_the_header_links_start_in_the_first_direction_and_carry_no_other_key():
    page = get("/plugins")[0].body.decode()
    cells = head_cells(page)
    assert cells[0] == '<a href="/plugins?sort=name&amp;dir=asc">名前</a>'
    assert cells[1] == '<a href="/plugins?sort=kind&amp;dir=asc">kind</a>'
    assert cells[2] == '<a href="/plugins?sort=state&amp;dir=asc">配備</a>'
    assert cells[4] == '<a href="/plugins?sort=pending&amp;dir=desc">承認待ち</a>'
    assert "▲" not in page and "▼" not in page
    useful_cell = cells[8]
    assert '<a href="/plugins?sort=submitted&amp;dir=desc">提出</a>' in useful_cell
    assert '<a href="/plugins?sort=deployed&amp;dir=desc">配備成功</a>' in useful_cell
    assert cells[3] == "承認と読込"


def test_the_current_column_is_marked_and_its_link_reverses_the_direction():
    page = get("/plugins", {"sort": "name"})[0].body.decode()
    assert head_cells(page)[0] == '<a href="/plugins?sort=name&amp;dir=desc">名前▲</a>'
    assert page.count("▲") == 1 and "▼" not in page
    page = get("/plugins", {"sort": "pending", "dir": "asc"})[0].body.decode()
    assert head_cells(page)[4] == '<a href="/plugins?sort=pending&amp;dir=desc">承認待ち▲</a>'
    page = get("/plugins", {"sort": "pending"})[0].body.decode()
    assert head_cells(page)[4] == '<a href="/plugins?sort=pending&amp;dir=asc">承認待ち▼</a>'
    page = get("/plugins", {"sort": "deployed", "dir": "asc"})[0].body.decode()
    cell = head_cells(page)[8]
    assert '<a href="/plugins?sort=deployed&amp;dir=desc">配備成功▲</a>' in cell
    assert '<a href="/plugins?sort=submitted&amp;dir=desc">提出</a>' in cell


def test_no_header_href_has_a_raw_ampersand():
    page = get("/plugins", {"sort": "name"})[0].body.decode()
    for href in re.findall(r'href="([^"]*)"', page):
        assert "&" not in href.replace("&amp;", "")


def test_only_the_list_table_is_wrapped_in_a_scroll_box():
    page = get("/plugins")[0].body.decode()
    assert page.count('<div class="scroll">') == 1
    assert re.search(r'<div class="scroll"><table>.*</table>.*</div></body>', page, re.S)
    empty = {"sources": {"live": "ok"}, "plugins": [], "invalid_pending": [],
             "unknown_submissions": 0}
    assert '<div class="scroll">' not in get("/plugins", client=Client(listing=empty))[0].body.decode()
    assert '<div class="scroll">' not in get("/approvals")[0].body.decode()
    assert '<div class="scroll">' not in get("/plugins/rsi", {"tab": "approvals"})[0].body.decode()


@pytest.mark.parametrize("query", [{}, {"sort": "name"}, {"sort": "submitted", "dir": "asc"}])
def test_a_full_list_fills_the_budget_and_is_not_clipped(query):
    base = [{"name": f"p{index:04d}", "pending_count": index % 5} for index in range(900)]
    listing = listing_of(base)
    listing["invalid_pending"] = list(range(1, 400))
    response, _ = get("/plugins", query, Client(listing=listing))
    html = response.body.decode()
    assert response.status == 200 and len(response.body) <= render.MAX_RESPONSE_BYTES
    assert "Output clipped" not in html and "件省略" in html
    row = re.search(r"<tr><td><a.*?</tr>", html, re.S).group(0)
    assert len(response.body) + len(row.encode()) > render.MAX_RESPONSE_BYTES


def test_the_sort_columns_are_a_fixed_table():
    assert set(plugin_pages.SORT_COLUMNS) == {"name", "kind", "state", "pending", "submitted",
                                              "deployed"}
    assert {key: column.first for key, column in plugin_pages.SORT_COLUMNS.items()} == {
        "name": "asc", "kind": "asc", "state": "asc", "pending": "desc", "submitted": "desc",
        "deployed": "desc"}
