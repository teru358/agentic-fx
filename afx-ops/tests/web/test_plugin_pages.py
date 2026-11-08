"""/plugins と /plugins/{name}: 実際の応答の見本から画面を作り、語・allowlist・escape・上限を確かめる。"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from afx_ops import plugin_text
from afx_ops.api_client import Reply
from afx_ops.web import plugin_pages, render
from afx_ops.web.request import Request, handle_request
from afx_ops.web.routes import resolve_route

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MATCH = re.compile("(?<!不)一致")
NAMES = ("rsi", "mom", "brk", "bol", "junk", "ghost")


def sample(key: str) -> dict:
    return json.loads((FIXTURES / f"{key}.json").read_text())


class Client:
    def __init__(self, listing=None, details=None, plugin_reply=None):
        self.listing = listing if listing is not None else sample("plugins_list")
        self.details = details or {name: sample(f"plugin_{name}") for name in NAMES}
        self.plugin_reply = plugin_reply

    def plugins(self):
        return Reply(200, self.listing, None)

    def plugin(self, name):
        if self.plugin_reply is not None:
            return self.plugin_reply
        return Reply(200, self.details[name], None)


def page(path: str, client=None) -> str:
    response = handle_request(Request("GET", path, {}), client or Client())
    assert response.status == 200, (path, response.status)
    return response.body.decode()


def body_of(html: str) -> str:
    return html.split("</nav>", 1)[1]


def cells(html: str, name: str) -> list[str]:
    row = re.search(rf'<tr><td><a href="/plugins/{name}">{name}</a></td>(.*?)</tr>', html, re.S)
    assert row, name
    return re.findall(r"<td>(.*?)</td>", row.group(1), re.S)


def every_dict(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from every_dict(item)
    elif isinstance(value, list):
        for item in value:
            yield from every_dict(item)


# ---------------------------------------------------------------- route

def test_the_plugin_route_matches_only_the_loader_name_grammar_and_does_not_decode():
    match = resolve_route("/plugins/rsi_14")
    assert match.route.client_method == "plugin" and match.values == {"name": "rsi_14"}
    assert resolve_route("/plugins").route.client_method == "plugins"
    assert resolve_route("/plugins/" + "a" * 64) is not None
    for bad in ("/plugins/", "/plugins/" + "a" * 65, "/plugins/Rsi", "/plugins/1rsi",
                "/plugins/r-s", "/plugins/rsi/", "/plugins/rsi/x", "/plugins/%2e%2e",
                "/plugins/rsi%2Fx", "/plugins/rsi\n", "/plugin/rsi"):
        assert resolve_route(bad) is None, bad
        assert handle_request(Request("GET", bad, {}), Client()).status in (400, 404), bad


def test_a_query_on_the_plugin_routes_is_refused_and_writes_are_405():
    for path in ("/plugins", "/plugins/rsi"):
        assert handle_request(Request("GET", path, {"x": "1"}), Client()).status == 400
        for method in ("POST", "PUT", "DELETE"):
            assert handle_request(Request(method, path, {}), Client()).status == 405


def test_an_unknown_plugin_is_404_and_other_failures_are_502():
    unknown = Client(plugin_reply=Reply(404, None, "not_found"))
    assert handle_request(Request("GET", "/plugins/nope", {}), unknown).status == 404
    for reply in (Reply(500, None, "internal"), Reply(404, None, "other"),
                  Reply(200, {"plugin": []}, None), Reply(200, {"plugin": {"name": 5}}, None),
                  Reply(200, {"unrelated": 1}, None)):
        assert handle_request(Request("GET", "/plugins/nope", {}),
                              Client(plugin_reply=reply)).status == 502, reply
    for listing in ({"plugins": "x"}, {"plugins": [1]}, {"sources": {}}):
        assert handle_request(Request("GET", "/plugins", {}),
                              Client(listing=listing)).status == 502, listing


# ---------------------------------------------------------------- 一覧

def test_the_list_shows_every_name_with_kind_axes_hash_pending_and_usefulness():
    html = page("/plugins")
    for name in NAMES + ("old",):
        assert f'<a href="/plugins/{name}">{name}</a>' in html
    rsi = cells(html, "rsi")
    assert rsi[0] == "indicator"
    assert rsi[1] == "配備中 (版を指す)" and rsi[2] == "読込可" and rsi[3] == "1"
    assert rsi[4] == sample("plugin_rsi")["plugin"]["entity"]["content_hash"][:12]
    assert '<a href="/plugins/brk">brk</a> (pin 不一致)' in rsi[6]
    assert '<a href="/plugins/mom">mom</a>' in rsi[6] and "mom</a> (" not in rsi[6]
    assert rsi[7] == "提出 2 / 配備成功 2"
    brk = cells(html, "brk")
    assert brk[2] == "読込除外 (indicator を解決できない) [a: pin 不一致]"
    assert brk[6:8] == ["-", "-"]
    assert cells(html, "bol")[1] == "配備なし" and cells(html, "bol")[3] == "1"
    assert cells(html, "bol")[7] == "提出 0 / 配備成功 0"
    assert cells(html, "junk")[0] == "不明"
    assert cells(html, "junk")[1] == "読めない (必要なファイルが無い)"
    assert cells(html, "junk")[8] == "読めない (必要なファイルが無い)"
    assert cells(html, "ghost")[0] == "signal"


def test_the_list_shows_unknown_submissions_and_a_way_to_the_approvals():
    html = body_of(page("/plugins"))
    assert '<a href="/approvals">' in html
    assert "古い strategy の提出: 1 件" in html
    listing = sample("plugins_list")
    listing["unknown_submissions"] = 0
    assert "古い strategy の提出" not in page("/plugins", Client(listing=listing))
    del listing["unknown_submissions"]
    assert "古い strategy の提出" not in page("/plugins", Client(listing=listing))


def test_a_source_that_could_not_be_read_is_shown_as_a_warning():
    listing = sample("plugins_list")
    assert 'class="warn"' not in page("/plugins")
    listing["sources"] = {"live": "ok", "pending": "unavailable", "journal": "missing"}
    html = page("/plugins", Client(listing=listing))
    assert "承認待ち: 読めない" in html and "切替の記録: なし" in html and "配備:" not in html


def test_invalid_pending_requests_are_counted_and_linked():
    listing = sample("plugins_list")
    listing["invalid_pending"] = [3, 4]
    html = page("/plugins", Client(listing=listing))
    assert "読めない承認待ち: 2 件" in html
    assert '<a href="/approvals/3">#3</a>' in html and '<a href="/approvals/4">#4</a>' in html


def test_an_empty_list_says_so():
    listing = {"sources": {"live": "ok"}, "plugins": [], "invalid_pending": [],
               "unknown_submissions": 0}
    assert "plugin はありません" in page("/plugins", Client(listing=listing))


def test_open_operations_are_shown_with_words():
    listing = sample("plugins_list")
    row = next(item for item in listing["plugins"] if item["name"] == "mom")
    row["open_ops"] = [{"source": "deploy", "op_id": 12, "kind": "disable", "target": None},
                       {"source": "switch", "op_id": 13, "kind": "approve", "target": "a" * 64}]
    assert "配備操作 停止 #12 / 切替 承認 #13" in page("/plugins", Client(listing=listing))


# ---------------------------------------------------------------- 詳細

def test_the_detail_of_a_strategy_shows_dependencies_with_the_state_words():
    html = page("/plugins/brk")
    section = re.search(r"<h2>使う indicator</h2>(.*?)</section>", html, re.S).group(1)
    assert re.findall(r"<td>(.*?)</td>", section) == [
        "a", '<a href="/plugins/rsi">rsi</a>', "a" * 12, "pin 不一致"]
    assert "pin_mismatch" not in html
    ok = re.search(r"<h2>使う indicator</h2>(.*?)</section>", page("/plugins/mom"), re.S).group(1)
    assert re.findall(r"<td>(.*?)</td>", ok)[-1] == "一致"


def test_the_detail_of_an_indicator_shows_dependents_usefulness_and_pins():
    html = page("/plugins/rsi")
    dependents = re.search(r"<h2>参照する strategy</h2>(.*?)</section>", html, re.S).group(1)
    assert re.findall(r"<td>(.*?)</td>", dependents) == [
        '<a href="/plugins/brk">brk</a>', "a", "a" * 12, "pin 不一致",
        '<a href="/plugins/mom">mom</a>', "fast", sample("plugin_rsi")["plugin"]["dependents"][1]["pin"][:12],
        "一致"]
    useful = re.search(r"<h2>有用性</h2>(.*?)</section>", html, re.S).group(1)
    assert "<dt>提出</dt><dd>2</dd>" in useful and "<dt>配備成功</dt><dd>2</dd>" in useful
    assert "1 件は、上の数に含めていません" in useful


def test_the_detail_lists_versions_and_the_approval_history_with_links_and_user_tz():
    html = page("/plugins/rsi")
    versions = re.search(r"<h2>版の履歴</h2>(.*?)</section>", html, re.S).group(1)
    assert '配備中</td><td><a href="/approvals/1">#1</a></td><td>承認済み</td>' in versions
    history = re.search(r"<h2>承認の履歴</h2>(.*?)</section>", html, re.S).group(1)
    rows = re.findall(r"<tr><td>(.*?)</tr>", history, re.S)
    assert len(rows) == 2
    newest = re.findall(r"<td>(.*?)</td>", "<td>" + rows[0])
    assert newest == ['<a href="/approvals/4">#4</a>', "承認待ち", "indicator", "改善の提出", "9",
                      "e" * 12, "2026-11-01 09:00:09 +09:00", "-"]
    oldest = re.findall(r"<td>(.*?)</td>", "<td>" + rows[1])
    assert oldest[3:5] == ["人の提出", "-"] and oldest[-1] == "2026-11-01 09:00:02 +09:00"
    pending = re.search(r"<h2>承認待ち</h2>(.*?)</section>", html, re.S).group(1)
    assert '<a href="/approvals/4">#4</a>' in pending
    assert '<a href="/approvals">' in body_of(html)


def test_a_history_cut_by_the_daemon_says_how_many_there_are():
    data = sample("plugin_rsi")
    shown = len(data["plugin"]["history"])
    data["plugin"]["history_total"] = shown + 120
    html = page("/plugins/rsi", Client(details={"rsi": data}))
    history = re.search(r"<h2>承認の履歴</h2>(.*?)</section>", html, re.S).group(1)
    assert f"最新 {shown} 件 (全 {shown + 120} 件)" in history
    whole = page("/plugins/rsi")
    assert "最新" not in re.search(r"<h2>承認の履歴</h2>(.*?)</section>", whole, re.S).group(1)
    data["plugin"]["history_total"] = "many"
    odd = page("/plugins/rsi", Client(details={"rsi": data}))
    assert "最新" not in re.search(r"<h2>承認の履歴</h2>(.*?)</section>", odd, re.S).group(1)


def test_without_display_timezone_the_times_are_utc():
    data = sample("plugin_rsi")
    del data["display_timezone"]
    html = page("/plugins/rsi", Client(details={"rsi": data}))
    assert "2026-11-01 00:00:09 +00:00" in html


def test_the_detail_of_unreadable_and_absent_names_renders_without_missing_parts():
    junk = page("/plugins/junk")
    assert "読めない (必要なファイルが無い)" in junk and "config.yaml が不正" not in junk
    assert "<h2>使う indicator</h2>" not in junk and "<h2>参照する strategy</h2>" not in junk
    ghost = page("/plugins/ghost")
    assert "却下" in ghost and "配備なし" in ghost


# ---------------------------------------------------------------- 語の写し表

def test_a_word_the_table_does_not_know_is_shown_as_unknown_with_the_word_never_as_match():
    listing = sample("plugins_list")
    row = next(item for item in listing["plugins"] if item["name"] == "rsi")
    row["dependents"][0]["state"] = "frobnicated"
    other = next(item for item in listing["plugins"] if item["name"] == "mom")
    other["entity"]["state"] = "weird_state"
    other["admission"]["state"] = "odd"
    other["kind"] = "gizmo"
    html = page("/plugins", Client(listing=listing))
    cell = cells(html, "mom")
    assert cell[0] == "不明 (gizmo)" and cell[1] == "不明 (weird_state)" and cell[2] == "不明 (odd)"
    assert "不明 (frobnicated)" in cells(html, "rsi")[6]
    assert not MATCH.search(html)


def test_unknown_state_words_in_the_detail_are_unknown_not_match():
    data = sample("plugin_brk")
    data["plugin"]["dependencies"][0]["state"] = "frobnicated"
    data["plugin"]["admission"]["detail"] = "frobnicated"
    html = page("/plugins/brk", Client(details={"brk": data}))
    assert "不明 (frobnicated)" in html
    assert not MATCH.search(html)
    data["plugin"]["dependencies"][0]["state"] = "unknown"
    assert "<td>不明</td>" in page("/plugins/brk", Client(details={"brk": data}))


def test_a_word_that_is_not_a_plain_word_is_unknown_without_echoing_it():
    for bad in ("<b>x</b>", "PIN", "a" * 41, "", 5, None, ["match"], {"match": 1}):
        assert plugin_text.label(plugin_text.DEPENDENCY, bad) == "不明", bad
    assert plugin_text.label(plugin_text.DEPENDENCY, "match") == "一致"
    assert plugin_text.label(plugin_text.DEPENDENCY, "pin_mismatch") == "pin 不一致"
    assert plugin_text.label(plugin_text.DEPENDENCY, "new_word") == "不明 (new_word)"


def test_every_word_in_the_real_samples_has_a_label():
    for key in ["plugins_list"] + [f"plugin_{name}" for name in NAMES]:
        html = page("/plugins" if key == "plugins_list" else f"/plugins/{key[7:]}")
        assert "不明 (" not in html, key


def test_the_tables_hold_no_empty_label_and_no_word_maps_to_match_by_accident():
    for table in (plugin_text.KIND, plugin_text.ENTITY, plugin_text.ENTITY_REASON,
                  plugin_text.ADMISSION, plugin_text.ADMISSION_REASON, plugin_text.DEPENDENCY,
                  plugin_text.ISSUE, plugin_text.APPROVAL_STATUS, plugin_text.ORIGIN,
                  plugin_text.OPEN_OP_SOURCE, plugin_text.OPEN_OP_KIND, plugin_text.SOURCE_STATE):
        assert all(value for value in table.values())
    assert [word for word, text in plugin_text.DEPENDENCY.items() if text == "一致"] == ["match"]


# ---------------------------------------------------------------- allowlist

def test_a_new_server_field_is_not_shown_on_either_page():
    marker = "ZZ-NEW-FIELD-VALUE"
    for path, key in (("/plugins", "plugins_list"), ("/plugins/rsi", "plugin_rsi"),
                      ("/plugins/brk", "plugin_brk"), ("/plugins/ghost", "plugin_ghost")):
        data = sample(key)
        for node in every_dict(data):
            node["zz_new_field"] = marker
        if key == "plugins_list":
            client = Client(listing=data)
        else:
            client = Client(details={key[7:]: data})
        assert marker not in page(path, client), path


def test_the_allowlists_name_exactly_the_fields_of_the_real_samples():
    listing = sample("plugins_list")
    assert set(listing) - {"display_timezone"} == set(plugin_pages.LISTING_FIELDS)
    row = next(item for item in listing["plugins"] if item["name"] == "rsi")
    assert set(row) == set(plugin_pages.ROW_FIELDS) - {"history", "history_total", "versions"}
    detail = sample("plugin_rsi")["plugin"]
    assert set(detail) == set(plugin_pages.ROW_FIELDS)
    assert set(detail["history"][0]) - {"artifact_hash"} == set(plugin_pages.HISTORY_FIELDS)
    assert set(detail["versions"][0]) == set(plugin_pages.VERSION_FIELDS)
    assert set(row["entity"]) == set(plugin_pages.ENTITY_FIELDS)
    assert set(row["admission"]) == set(plugin_pages.ADMISSION_FIELDS)
    assert set(row["pending"][0]) == set(plugin_pages.PENDING_FIELDS)
    assert set(row["dependents"][0]) == set(plugin_pages.DEPENDENT_FIELDS)
    assert set(row["usefulness"]) == set(plugin_pages.USEFULNESS_FIELDS)
    assert set(row["usefulness"]["by_pin"][0]) == set(plugin_pages.BY_PIN_FIELDS)
    brk = sample("plugin_brk")["plugin"]
    assert set(brk["dependencies"][0]) == set(plugin_pages.DEPENDENCY_FIELDS)


def test_a_field_with_the_wrong_type_does_not_break_the_page():
    for path, key in (("/plugins", "plugins_list"), ("/plugins/rsi", "plugin_rsi")):
        data = sample(key)
        for node in every_dict(data):
            for name in list(node):
                if name not in ("plugins", "plugin"):
                    node[name] = {"x": [1]} if isinstance(node[name], str) else "bad"
        client = Client(listing=data) if key == "plugins_list" else Client(details={"rsi": data})
        response = handle_request(Request("GET", path, {}), client)
        assert response.status in (200, 502), (path, response.status)


# ---------------------------------------------------------------- escape

HOSTILE = '<script>alert(1)</script>"&\''


def test_hostile_strings_are_escaped_on_both_pages():
    listing = sample("plugins_list")
    for node in every_dict(listing):
        for name, value in list(node.items()):
            if isinstance(value, str) and name not in ("display_timezone", "state", "kind",
                                                       "status", "reason", "detail", "origin",
                                                       "live", "pending", "journal"):
                node[name] = HOSTILE
    detail = sample("plugin_brk")
    for node in every_dict(detail):
        for name, value in list(node.items()):
            if isinstance(value, str) and name not in ("display_timezone", "state", "kind",
                                                       "status", "reason", "detail", "origin"):
                node[name] = HOSTILE
    htmls = [page("/plugins", Client(listing=listing)),
             page("/plugins/brk", Client(details={"brk": detail}))]
    for html in htmls:
        assert "<script" not in html.replace("<style>", "")
        assert HOSTILE not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert " onerror" not in html


def test_a_hostile_word_in_a_state_is_never_echoed():
    data = sample("plugin_brk")
    data["plugin"]["dependencies"][0]["state"] = HOSTILE
    data["plugin"]["entity"]["state"] = HOSTILE
    html = page("/plugins/brk", Client(details={"brk": data}))
    assert "<script" not in html and "alert(1)" not in html


def test_a_hostile_alias_and_name_are_escaped_not_linked():
    data = sample("plugin_brk")
    data["plugin"]["dependencies"][0]["alias"] = HOSTILE
    data["plugin"]["dependencies"][0]["plugin"] = "../x"
    html = page("/plugins/brk", Client(details={"brk": data}))
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert 'href="/plugins/../x"' not in html and "../x" in html


# ---------------------------------------------------------------- 上限

def test_the_list_is_cut_by_whole_rows_in_name_order_and_says_how_many_were_left_out():
    listing = sample("plugins_list")
    base = next(item for item in listing["plugins"] if item["name"] == "rsi")
    rows = []
    for index in range(600):
        row = copy.deepcopy(base)
        row["name"] = f"p{index:04d}"
        rows.append(row)
    listing["plugins"] = rows
    response = handle_request(Request("GET", "/plugins", {}), Client(listing=listing))
    assert response.status == 200 and len(response.body) <= render.MAX_RESPONSE_BYTES
    html = response.body.decode()
    shown = re.findall(r'<tr><td><a href="/plugins/(p\d{4})">', html)
    assert shown == [f"p{index:04d}" for index in range(len(shown))]
    assert 0 < len(shown) < 600
    omitted = re.search(r"…\((\d+) 件省略", html)
    assert omitted and int(omitted.group(1)) == 600 - len(shown)
    assert "Output clipped" not in html
    # もう 1 行は入らない (切り方が早すぎない)
    row_bytes = len(("<tr>" + html.split("<tr>")[2].split("</tr>")[0] + "</tr>").encode())
    assert len(response.body) + row_bytes > render.MAX_RESPONSE_BYTES


def test_a_list_that_fits_is_not_cut():
    html = page("/plugins")
    assert "件省略" not in html


def test_the_cut_leaves_room_for_the_header_notes():
    listing = sample("plugins_list")
    listing["invalid_pending"] = list(range(1, 400))
    base = listing["plugins"][-1]
    listing["plugins"] = [dict(copy.deepcopy(base), name=f"p{index:04d}") for index in range(600)]
    response = handle_request(Request("GET", "/plugins", {}), Client(listing=listing))
    assert response.status == 200 and len(response.body) <= render.MAX_RESPONSE_BYTES
    assert "Output clipped" not in response.body.decode()


def test_a_detail_with_the_largest_lists_and_longest_values_stays_in_the_limit():
    data = sample("plugin_brk")
    plugin = data["plugin"]
    long = "x" * 600
    plugin["dependencies"] = [dict(plugin["dependencies"][0], alias=long, plugin=f"d{i}")
                              for i in range(300)]
    plugin["dependents"] = [{"strategy": f"s{i}", "alias": long, "pin": "a" * 64, "state": "match"}
                            for i in range(300)]
    plugin["history"] = [dict(plugin["history"][0], id=i + 1, mission_id=10 ** 12) for i in range(300)]
    plugin["versions"] = [dict(plugin["versions"][0], approval_id=i + 1) for i in range(300)]
    plugin["pending"] = [{"id": i + 1, "content_hash": "b" * 64, "kind": "strategy"} for i in range(300)]
    plugin["usefulness"] = {"submitted": 1, "deployed": 1,
                            "by_pin": [{"content_hash": "c" * 64, "submitted": 1, "deployed": 1}] * 300}
    response = handle_request(Request("GET", "/plugins/brk", {}),
                              Client(details={"brk": data}))
    assert response.status == 200 and len(response.body) <= render.MAX_RESPONSE_BYTES
    html = response.body.decode()
    assert "Output clipped" not in html
    for title in ("承認待ち", "使う indicator", "参照する strategy", "版の履歴", "承認の履歴"):
        section = re.search(rf"<h2>{title}</h2>(.*?)</section>", html, re.S).group(1)
        assert "件省略" in section, title
        shown = section.count("<tr><td>")
        omitted = int(re.search(r"…\((\d+) 件省略", section).group(1))
        assert shown + omitted == 300, title
    assert len(re.findall(r'<a href="/approvals/\d+">', html)) <= 5 * render.MAX_ITEMS + 2
