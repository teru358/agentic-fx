"""/plugins の「使われ方」の列と未使用の絞り込み、/plugins/{name} の概要の判定の行。"""
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

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def sample(key: str) -> dict:
    return json.loads((FIXTURES / f"{key}.json").read_text())


def review(state: str, *, proposable: bool = False, origin: str = "staging",
           not_proposable: str | None = None, unknown: dict | None = None) -> dict:
    results = {"live_strategy": ("clear", None), "pending_approval": ("clear", None),
               "recent_candidate": ("clear", None), "recent_deploy": ("clear", None)}
    if state == "used":
        results["live_strategy"] = ("holds", None)
    for cond, reason in (unknown or {}).items():
        results[cond] = ("unknown", reason)
    return {"state": state, "k_weeks": 8, "origin": origin, "proposable": proposable,
            "not_proposable": not_proposable,
            "conditions": [{"id": cond, "result": result, "reason": reason}
                           for cond, (result, reason) in results.items()]}


def indicator_row(name: str, usage: object) -> dict:
    row = copy.deepcopy(next(item for item in sample("plugins_list")["plugins"]
                             if item["name"] == "rsi"))
    row["name"] = name
    row["usage_review"] = usage
    return row


def listing_of(rows: list[dict], **inventory) -> dict:
    data = sample("plugins_list")
    data["plugins"] = rows
    data["usage_inventory"] = {**data["usage_inventory"], **inventory}
    return data


class Client:
    def __init__(self, listing, detail=None):
        self.listing, self.detail = listing, detail

    def plugins(self):
        return Reply(200, self.listing, None)

    def plugin(self, name):
        return Reply(200, self.detail, None)


def get(listing, query=None, path="/plugins"):
    return handle_request(Request("GET", path, query or {}), Client(listing))


def page(listing, query=None) -> str:
    response = get(listing, query)
    assert response.status == 200
    return response.body.decode()


def usage_cell(html: str, name: str) -> str:
    row = re.search(rf'<tr><td><a href="/plugins/{name}">{name}</a></td>(.*?)</tr>', html, re.S)
    assert row, name
    return re.findall(r"<td>(.*?)</td>", row.group(1), re.S)[-1]


# ---------------------------------------------------------------- allowlist と語

def test_the_usage_allowlists_name_exactly_the_fields_of_the_real_sample():
    data = sample("plugins_list")
    assert set(data["usage_inventory"]) == set(plugin_pages.USAGE_INVENTORY_FIELDS)
    assert set(data["usage_inventory"]["excluded"]) == set(plugin_pages.USAGE_EXCLUDED_FIELDS)
    usage = next(row for row in data["plugins"] if row["name"] == "rsi")["usage_review"]
    assert set(usage) == set(plugin_pages.USAGE_FIELDS)
    assert set(usage["conditions"][0]) == set(plugin_pages.USAGE_CONDITION_FIELDS)
    assert [item["id"] for item in usage["conditions"]] == list(plugin_text.USAGE_CONDITION)


def test_every_reason_the_daemon_can_give_has_a_japanese_label():
    for reason in ("scan_failed", "pending_source_unreadable", "pending_request_unreadable",
                   "candidates_unreadable", "key_mentioned", "time_unreadable",
                   "journal_unreadable", "no_generation_record", "generation_mismatch"):
        assert reason in plugin_text.USAGE_REASON


def test_a_field_the_page_does_not_name_is_not_shown():
    marker = "ZZ-NEW-USAGE-FIELD"
    row = indicator_row("aaa", review("unused", proposable=True))
    row["usage_review"]["zz_new"] = marker
    data = listing_of([row])
    data["usage_inventory"]["zz_new"] = marker
    assert marker not in page(data)


# ---------------------------------------------------------------- 一覧の列

def test_the_column_tells_used_unused_and_indeterminate_apart():
    rows = [
        indicator_row("uuu", review("used")),
        indicator_row("nnn", review("unused", proposable=True)),
        indicator_row("hhh", review("unused", origin="human",
                                    not_proposable="human_origin")),
        indicator_row("iii", review("indeterminate", unknown={
            "recent_deploy": "no_generation_record", "pending_approval": "pending_source_unreadable"})),
    ]
    html = page(listing_of(rows))
    assert usage_cell(html, "uuu") == "使用中"
    assert usage_cell(html, "nnn") == "△ 未使用 (参照なし・配備から 8 週以上。停止の提案: 可)"
    assert usage_cell(html, "hhh") == "△ 未使用 (参照なし・配備から 8 週以上。停止の提案の対象外)"
    assert usage_cell(html, "iii") == "? 判定不能 (承認待ちを読めなかった・配備した記録が見つからない)"


def test_the_column_is_a_plain_mark_not_a_styled_status():
    rows = [indicator_row("nnn", review("unused", proposable=True))]
    assert 'class="st' not in usage_cell(page(listing_of(rows)), "nnn")


def test_the_column_is_dash_for_a_row_without_a_review():
    plain = indicator_row("aaa", None)
    signal = indicator_row("sss", review("unused", proposable=True))
    signal["kind"] = "signal"
    html = page(listing_of([plain, signal]))
    assert usage_cell(html, "aaa") == "-" and usage_cell(html, "sss") == "-"


@pytest.mark.parametrize("bad", ["unused", 7, ["unused"], {"state": ["unused"]},
                                 {"state": "weird"}, {"state": None}])
def test_a_malformed_review_never_reads_as_unused(bad):
    html = page(listing_of([indicator_row("aaa", bad)]))
    assert "未使用" not in usage_cell(html, "aaa")
    assert "△" not in usage_cell(html, "aaa")
    filtered = page(listing_of([indicator_row("aaa", bad)]), {"unused": "1"})
    assert '<a href="/plugins/aaa">' not in filtered


def test_raw_words_of_the_mechanism_are_not_shown():
    rows = [indicator_row("iii", review("indeterminate", unknown={
        "recent_candidate": "key_mentioned", "recent_deploy": "generation_mismatch"})),
        indicator_row("nnn", review("unused", proposable=True))]
    html = page(listing_of(rows))
    for word in ("usage_review", "key_mentioned", "generation_mismatch", "recent_candidate",
                 "proposable", "indeterminate"):
        assert word not in html


def test_the_header_explains_unused_and_counts_the_candidates_in_the_window():
    html = page(listing_of([indicator_row("nnn", review("unused", proposable=True))],
                           window_candidates=0))
    assert "配備から 8 週以上たち" in html and "直近 8 週の戦略候補: 0 件" in html
    excluded = {"null_key": 2, "unreadable_key": 0, "unreadable_time": 1}
    html = page(listing_of([], window_candidates=5, excluded=excluded))
    assert "直近 8 週の戦略候補: 5 件 (除外した行: key なし 2、作成時刻を読めない 1)" in html


def test_an_inventory_that_could_not_be_judged_is_a_warning():
    html = page(listing_of([], status="indeterminate", reason="journal_unreadable"))
    assert ('<p class="warn">棚卸し全体を判定できませんでした: '
            "切替の記録を読めなかったため、配備時期を確かめきれません</p>") in html
    html = page(listing_of([], candidates_readable=False))
    assert "戦略候補の記録を読めなかったため、判定できない指標があります" in html


def test_a_daemon_without_the_review_shows_no_header_and_no_crash():
    data = sample("plugins_list")
    del data["usage_inventory"]
    for row in data["plugins"]:
        del row["usage_review"]
    html = page(data)
    assert "未使用" not in html
    assert "未使用かどうかの判定を受け取れませんでした" in page(data, {"unused": "1"})


# ---------------------------------------------------------------- 絞り込み

def test_unused_only_shows_the_unused_indicators():
    rows = [indicator_row("uuu", review("used")),
            indicator_row("nnn", review("unused", proposable=True)),
            indicator_row("iii", review("indeterminate", unknown={"recent_deploy": "journal_unreadable"}))]
    html = page(listing_of(rows), {"unused": "1"})
    assert '<a href="/plugins/nnn">' in html
    assert '<a href="/plugins/uuu">' not in html and '<a href="/plugins/iii">' not in html
    assert '<a href="/plugins">すべて表示</a>' in html
    assert '<a href="/plugins?unused=1">未使用だけ表示</a>' in page(listing_of(rows))


def test_the_filter_applies_before_the_page_budget_cuts_the_rows():
    rows = [indicator_row(f"u{index:03d}", review("used")) for index in range(400)]
    rows += [indicator_row("zlast", review("unused", proposable=True))]
    full = page(listing_of(rows))
    assert '<a href="/plugins/zlast">' not in full and "件省略" in full
    filtered = get(listing_of(rows), {"unused": "1"}).body.decode()
    assert '<a href="/plugins/zlast">' in filtered and "件省略" not in filtered


def test_the_omission_note_of_a_filtered_page_points_to_the_matching_cli():
    rows = [indicator_row(f"n{index:03d}", review("unused", proposable=True))
            for index in range(400)]
    response = get(listing_of(rows), {"unused": "1"})
    assert response.status == 200 and len(response.body) <= render.MAX_RESPONSE_BYTES
    assert "afx plugin list --unused で確認してください" in response.body.decode()


def test_the_sort_links_keep_the_filter():
    rows = [indicator_row("nnn", review("unused", proposable=True))]
    html = page(listing_of(rows), {"unused": "1", "sort": "name", "dir": "desc"})
    assert 'href="/plugins?sort=name&amp;dir=asc&amp;unused=1"' in html
    unfiltered = page(listing_of(rows), {"sort": "name", "dir": "desc"})
    sort_links = [href for href in re.findall(r'href="([^"]*)"', unfiltered) if "sort=" in href]
    assert sort_links and not any("unused" in href for href in sort_links)


def test_zero_unused_reads_differently_from_could_not_judge():
    used = [indicator_row("uuu", review("used"))]
    assert "<p>未使用の指標はありません</p>" in page(listing_of(used), {"unused": "1"})
    unknown = [indicator_row("iii", review("indeterminate",
                                           unknown={"recent_deploy": "no_generation_record"}))]
    assert ("<p>未使用と決まった指標はありません (判定できなかった指標 1 件)</p>"
            in page(listing_of(unknown), {"unused": "1"}))
    assert ("<p>未使用と決まった指標はありません</p>"
            in page(listing_of(used, status="indeterminate", reason="scan_failed"),
                    {"unused": "1"}))


@pytest.mark.parametrize("query", [{"unused": "0"}, {"unused": "yes"}, {"unused": ""},
                                   {"unused": "1", "extra": "1"}])
def test_the_filter_value_is_validated(query):
    rows = [indicator_row("nnn", review("unused", proposable=True))]
    assert get(listing_of(rows), query).status == 400


# ---------------------------------------------------------------- 詳細

def detail_of(usage: object) -> dict:
    data = sample("plugin_rsi")
    data["plugin"]["usage_review"] = usage
    return data


def overview(usage: object) -> str:
    response = handle_request(Request("GET", "/plugins/rsi", {}),
                              Client(None, detail=detail_of(usage)))
    assert response.status == 200
    return response.body.decode()


def test_the_overview_lists_the_four_conditions_and_the_proposal():
    html = overview(review("unused", proposable=True))
    assert "<dt>使われ方</dt><dd>未使用</dd>" in html
    assert "<dd>参照する配備中の strategy はない</dd>" in html
    assert "<dd>参照する承認待ちはない</dd>" in html
    assert "<dd>直近 8 週の戦略候補には登場しない</dd>" in html
    assert "<dd>配備から 8 週以上たっている</dd>" in html
    assert "<dt>使われ方: 停止の提案</dt><dd>停止の提案にできる</dd>" in html
    assert "<dt>いまの版の出所</dt><dd>改善の提出</dd>" in html


def test_the_overview_says_why_a_human_version_is_out_of_scope():
    html = overview(review("unused", origin="human", not_proposable="human_origin"))
    assert "人が提出した版なので、停止の提案の対象外" in html


def test_the_overview_shows_the_reason_for_an_undecided_condition_and_no_proposal():
    html = overview(review("indeterminate", unknown={"recent_deploy": "no_generation_record"}))
    assert "<dd>判定不能 (配備した記録が見つからない)</dd>" in html
    assert "停止の提案" not in html


def test_the_overview_of_a_used_indicator_has_no_proposal_row():
    html = overview(review("used"))
    assert "<dd>配備中の strategy が参照している</dd>" in html
    assert "停止の提案" not in html


def test_the_overview_without_a_review_is_unchanged():
    assert "使われ方" not in overview(None)
