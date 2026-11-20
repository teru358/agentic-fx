"""route ごとのタブの帯・query の検査・Status と Activity の振り分け。"""
from __future__ import annotations

import re

import pytest

from afx_ops.api_client import Reply
from afx_ops.web import render, tabs
from afx_ops.web.request import Request, handle_request

STATUS = {
    "mode": "paper", "kill_switch": {"latched": False}, "autopilot": False,
    "health": "ok", "equity": 100, "balance": 90, "data": {"state": "ok"},
    "orders": 3, "mission": {"state": "idle"},
    "bot_health": {"state": "healthy", "alternate_push": "configured"},
    "window": {"freshness": "fresh", "in_window": False},
    "econ_signal": {"slice_worker": "alive", "reaction": {"floor": 20, "bundles": []}},
}


class Client:
    def __init__(self, status=STATUS, lines=("a",)):
        self.status_data, self.lines, self.calls = status, list(lines), []

    def status(self):
        self.calls.append("status")
        return Reply(200, self.status_data, None)

    def activity(self, *, n, category):
        self.calls.append(("activity", n, category))
        return Reply(200, {"lines": self.lines}, None)


def get(path, query=None, client=None, method="GET"):
    client = client or Client()
    return handle_request(Request(method, path, query or {}), client), client


def headings(page, level="h2"):
    return re.findall(rf"<{level}>([^<]*)</{level}>", page)


STATUS_SECTIONS = {
    "overview": ["安全", "稼働"],
    "bot": ["Bot"],
    "econ": ["Economic calendar", "Economic signal"],
    "reaction": ["Economic reactions"],
}


@pytest.mark.parametrize("tab,expected", STATUS_SECTIONS.items())
def test_each_status_tab_shows_only_its_sections_in_order(tab, expected):
    response, _ = get("/", {"tab": tab})
    assert response.status == 200
    assert headings(response.body.decode()) == expected


def test_the_status_sections_table_matches_the_tab_table():
    assert set(render.STATUS_TAB_SECTIONS) == set(tabs.tab_names("/"))
    assert list(STATUS_SECTIONS) == list(tabs.tab_names("/"))


def test_a_missing_tab_and_the_default_tab_give_the_same_body():
    assert get("/")[0].body == get("/", {"tab": "overview"})[0].body


def test_every_tab_of_the_bar_is_always_present_and_an_empty_tab_says_none():
    response, _ = get("/", {"tab": "bot"}, Client({"mode": "paper"}))
    page = response.body.decode()
    bar = re.search(r'<nav class="tabs">(.*?)</nav>', page).group(1)
    assert re.findall(r">([^<]*)</a>", bar) == ["概要", "Bot", "経済指標", "反応統計"]
    assert "<p>なし</p>" in page


def test_the_current_tab_alone_is_marked_and_links_use_the_fixed_table():
    page = get("/", {"tab": "econ"})[0].body.decode()
    bar = re.search(r'<nav class="tabs">(.*?)</nav>', page).group(1)
    assert re.findall(r'<a href="([^"]*)"( aria-current="page")?>([^<]*)', bar) == [
        ("/?tab=overview", "", "概要"), ("/?tab=bot", "", "Bot"),
        ("/?tab=econ", ' aria-current="page"', "経済指標"), ("/?tab=reaction", "", "反応統計")]
    assert page.index("<h1>") < page.index('<nav class="tabs">')


def test_the_status_overview_orders_rows_by_group_not_alphabetically():
    page = get("/")[0].body.decode()
    keys = re.findall(r"<dt[^>]*>([a-z_]+)</dt>", page)
    top = [key for key in keys if key in render.STATUS_FIELDS]
    # 平常のカードが写す autopilot・equity・balance・mode の行は出ない。健全と読めない値の行は残る
    assert top == ["kill_switch", "health", "mission", "data", "orders"]


def test_the_group_table_and_the_allowlist_hold_the_same_keys():
    keys = [key for _title, group in render.STATUS_GROUPS for key in group]
    assert len(keys) == len(set(keys))
    assert set(keys) == render.STATUS_FIELDS


def test_a_group_without_values_has_no_heading_and_empty_values_make_no_row():
    page = get("/", {}, Client({"mode": "paper", "mission": {"loop": "x"}, "pnl": None,
                                "latency": ""}))[0].body.decode()
    assert headings(page) == ["稼働"]
    assert "pnl" not in page and "latency" not in page


def test_the_emphasis_is_kept_in_the_overview():
    status = {"kill_switch": {"latched": True}, "autopilot": True}
    page = get("/", {}, Client(status))[0].body.decode()
    assert '<dt class="danger">kill_switch</dt><dd class="danger">' in page
    # autopilot の注意は印 (カード) に置き換わり、行は出ない
    assert '<dt class="warn">autopilot</dt>' not in page and "<dt>autopilot</dt>" not in page
    assert '<span class="st warn">△ ON</span>' in page


@pytest.mark.parametrize("path,query", [
    ("/", {"tab": "nope"}), ("/", {"tab": ""}), ("/", {"tab": "Overview"}),
    ("/", {"tab": "bot", "x": "1"}), ("/", {"x": "1"}), ("/", {"category": "NEWS"}),
    ("/activity", {"tab": "NEWS"}), ("/activity", {"category": "news"}),
    ("/activity", {"category": "NEWS", "x": "1"}), ("/activity", {"n": "0"}),
    ("/activity", {"n": "501"}), ("/activity", {"n": "01"}),
    ("/plugins", {"tab": "nope"}), ("/approvals", {"tab": "x"}), ("/logs", {"tab": "x"}),
])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_a_query_outside_the_table_is_400_and_never_reaches_the_daemon(path, query, method):
    client = Client()
    response, _ = get(path, query, client, method)
    assert response.status == 400
    assert client.calls == []
    assert response.body == (b"" if method == "HEAD" else response.body)
    assert b"nope" not in response.body and b'class="tabs"' not in response.body


def test_head_has_the_status_and_length_of_get():
    for path, query in (("/", {"tab": "bot"}), ("/activity", {"category": "TRADE", "n": "5"})):
        got, _ = get(path, query)
        head, _ = get(path, query, method="HEAD")
        assert head.status == got.status == 200
        assert head.headers["Content-Length"] == got.headers["Content-Length"] == str(len(got.body))
        assert head.body == b""


ACTIVITY_LABELS = ["全件", *tabs.ACTIVITY_CATEGORIES]


def _activity_bar(page):
    bar = re.search(r'<nav class="tabs">(.*?)</nav>', page).group(1)
    return re.findall(r'<a href="([^"]*)"( aria-current="page")?>([^<]*)', bar)


def test_the_activity_bar_has_all_and_every_category():
    got = _activity_bar(get("/activity")[0].body.decode())
    assert [label for _h, _m, label in got] == ACTIVITY_LABELS
    assert [href for href, _m, _l in got] == ["/activity"] + [
        f"/activity?category={name}" for name in tabs.ACTIVITY_CATEGORIES]
    assert [mark for _h, mark, _l in got] == [' aria-current="page"'] + [""] * 7


def test_the_activity_current_tab_follows_the_category_and_n_is_carried():
    client = Client()
    got = _activity_bar(get("/activity", {"category": "TRADE", "n": "50"}, client)[0].body.decode())
    assert client.calls == [("activity", 50, "TRADE")]
    assert got[0] == ("/activity?n=50", "", "全件")
    assert ("/activity?category=TRADE&amp;n=50", ' aria-current="page"', "TRADE") in got
    assert sum(bool(mark) for _h, mark, _l in got) == 1


def test_the_activity_bar_does_not_invent_n():
    got = _activity_bar(get("/activity", {"category": "NEWS"})[0].body.decode())
    assert all("n=" not in href for href, _m, _l in got)


def test_the_href_is_built_from_the_table_and_escaped():
    link = tabs._href("/activity", tabs.TABS["/activity"][1], {"n": 7})
    assert link == "/activity?category=NEWS&amp;n=7"
    assert tabs._href("/activity", tabs.TABS["/activity"][1], {"n": "7&x"}) == "/activity?category=NEWS"


@pytest.mark.parametrize("path,current", [
    ("/", "/"), ("/plugins", "/plugins"), ("/approvals", "/plugins"),
    ("/approvals/7", "/plugins"), ("/plugins/rsi", "/plugins"), ("/improve", "/improve"),
    ("/improve/3", "/improve"), ("/performance", "/performance"), ("/logs", "/logs"),
    ("/activity", "/activity")])
def test_the_nav_marks_the_screen_it_belongs_to(path, current):
    class Any:
        def __getattr__(self, name):
            def call(*args, **kwargs):
                return Reply(200, {"mode": "paper", "plugins": [], "approvals": [], "runs": [],
                                   "has_more": False, "lines": [], "id": 3, "plugin": {"name": "rsi"},
                                   "points": [], "hwm_steps": [], "marks": [], "strategies": [],
                                   "stats": {}},
                             None)
            return call

    page = handle_request(Request("GET", path, {}), Any()).body.decode()
    nav = re.search(r"<nav>(.*?)</nav>", page, re.S).group(1)
    marked = re.findall(r'<a href="([^"]*)" aria-current="page">', nav)
    assert marked == [current], (path, page[:300])


def test_an_error_page_has_the_nav_but_no_tab_bar_and_no_mark():
    page = re.sub(r"<style>.*?</style>", "", get("/", {"tab": "nope"})[0].body.decode(), flags=re.S)
    assert "<nav>" in page and 'class="tabs"' not in page and "aria-current" not in page


def test_the_css_has_the_tab_bar_the_scroll_frame_and_the_sticky_header():
    assert '.tabs a[aria-current="page"]' in render.STYLE
    assert "max-height: 70vh" in render.STYLE and "overflow: auto" in render.STYLE
    assert re.search(r"\.scroll th \{[^}]*position: sticky;[^}]*top: 0;",
                     render.STYLE)
    assert re.search(r"(^|\n)th \{[^}]*background: var\(--soft\)", render.STYLE)
