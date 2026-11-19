"""/performance: query の検査・今の資金保護の枠・資金推移の表・形の検査・予算・隔離。"""
from __future__ import annotations

import copy
import re

import pytest

from afx_ops.api_client import Reply, WebClient, WebWriteClient
from afx_ops.web import marks, perf_pages, render, tabs
from afx_ops.web.headers import SECURITY_HEADERS
from afx_ops.web.request import Request, handle_request

NOW = "2026-11-19T03:00:00+00:00"
TOKYO = "Asia/Tokyo"


def current(**over):
    base = {"kill_switch": {"latched": False, "generation": 3, "latched_at": None,
                            "reconcile_required": False},
            "state_fetched_at": NOW, "snapshot_at": "2026-11-19T02:59:30+00:00",
            "snapshot_fetched_at": NOW, "snapshot_age_sec": 30.0, "snapshot_freshness": "fresh",
            "dd_pct": 2.5, "kill_level": 9000.0, "headroom": 500.0, "kill_pct": 10.0,
            "fill_hold": "none"}
    base.update(over)
    return base


def mark(kind="latched", **over):
    base = {"kind": kind, "plot_at": "2026-11-18T01:02:03+00:00",
            "occurred_at": "2026-11-18T01:02:00+00:00",
            "recorded_at": "2026-11-18T01:02:03+00:00", "source_snapshot_ts": None,
            "applied_at": None, "generation": 2, "time_source": "state"}
    base.update(over)
    return base


def payload(**over):
    base = {"range": "30d", "from": "2026-10-20T03:00:00+00:00", "to": NOW, "bucket_sec": 9300,
            "account_currency": "JPY", "display_timezone": TOKYO, "current": current(),
            "points": [], "hwm_steps": [],
            "stats": {"first_at": "2026-10-20T03:01:00+00:00", "last_at": "2026-11-19T02:59:30+00:00",
                      "equity_first": 10000.0, "equity_last": 9500.5, "balance_first": 10000.0,
                      "balance_last": 9400.25, "equity_min": 9100.0, "equity_max": 10400.0,
                      "balance_min": 9000.0, "balance_max": 10300.0, "dd_max": 12.3,
                      "dd_max_at": "2026-11-10T00:00:00+00:00"},
            "marks": [], "marks_omitted": 0, "marks_since": "2026-10-05T00:00:00+00:00",
            "other_sources": 0, "strategies": [],
            "thresholds": {"min_count": 5, "reference_count": 30},
            "omissions": {"equity_points": 0, "hwm_steps": 0, "strategy_rows": 0,
                          "strategy_series": 0},
            "quality": {"partial": False, "invalid_snapshots": 0, "invalid_marks": 0,
                        "invalid_orders": 0, "unknown_hwm_drops": 0, "unplaced_closed_count": 0}}
    base.update(over)
    return base


class Client:
    def __init__(self, data=None, reply=None):
        self.data = payload() if data is None else data
        self.reply = reply
        self.calls = []

    def performance(self, *, range="30d"):
        self.calls.append(range)
        return self.reply or Reply(200, self.data, None)


def get(data=None, query=None, method="GET", client=None):
    client = client or Client(data)
    return handle_request(Request(method, "/performance", query or {}), client), client


def page(data=None, query=None):
    response, _ = get(data, query)
    assert response.status == 200
    return response.body.decode()


def after_bars(html: str) -> str:
    return html.rsplit("</nav>", 1)[1]


def body_text(html: str) -> str:
    return re.sub(r"<[^>]*>", " ", after_bars(html))


# --- route・query ----------------------------------------------------------------

def test_the_route_is_the_performance_read_and_its_tab_table_has_one_equity_tab():
    from afx_ops.web.routes import resolve_route
    assert resolve_route("/performance").route.client_method == "performance"
    assert tabs.tab_names("/performance") == ("equity",)
    assert tabs.default_tab("/performance") == "equity"


@pytest.mark.parametrize("range_key", ["7d", "30d", "90d", "all"])
def test_each_range_is_passed_to_the_daemon_and_marked_current(range_key):
    response, client = get(query={"range": range_key})
    assert response.status == 200 and client.calls == [range_key]
    html = response.body.decode()
    assert re.findall(r'<a href="([^"]*)" aria-current="page">', after_bars(html)) \
        == [perf_pages.range_href("equity", range_key)]


def test_the_default_range_is_30_days():
    response, client = get()
    assert response.status == 200 and client.calls == ["30d"]


@pytest.mark.parametrize("query", [
    {"range": "1d"}, {"range": "30D"}, {"range": ""}, {"range": "30d "}, {"range": "all,7d"},
    {"tab": "strategy"}, {"tab": "Equity"}, {"tab": ""}, {"x": "1"}, {"range": "7d", "n": "5"},
    {"range": "7d", "limit": "5"}])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_an_unknown_key_or_value_is_400_without_calling_the_daemon(query, method):
    response, client = get(query=query, method=method)
    assert response.status == 400 and client.calls == []
    assert (response.body == b"") is (method == "HEAD")


def test_the_range_links_swap_range_and_keep_the_tab_and_the_tab_bar_keeps_the_range():
    html = page(query={"range": "7d", "tab": "equity"})
    links = re.findall(r'<a href="([^"]*)"[^>]*>([^<]*)</a>', after_bars(html))
    assert [text for _href, text in links if text in {"7 日", "30 日", "90 日", "全期間"}] \
        == ["7 日", "30 日", "90 日", "全期間"]
    hrefs = {text: href for href, text in links}
    assert hrefs["7 日"] == "/performance?range=7d"
    assert hrefs["30 日"] == "/performance"
    assert hrefs["全期間"] == "/performance?range=all"
    bar = re.search(r'<nav class="tabs">(.*?)</nav>', html, re.S).group(1)
    assert re.findall(r'<a href="([^"]*)"', bar) == ["/performance?tab=equity&amp;range=7d"]
    assert "range=" not in re.search(r'<nav class="tabs">(.*?)</nav>', page(), re.S).group(1)


def test_the_href_carry_takes_only_short_lowercase_words_and_integers():
    item = tabs.TABS["/performance"][0]
    assert tabs._href("/performance", item, {"range": "7d"}) == "/performance?tab=equity&amp;range=7d"
    for bad in ("7d&x=1", "7D", "", "a" * 9, "7 d", "<b>"):
        assert tabs._href("/performance", item, {"range": bad}) == "/performance?tab=equity"


# --- 枠・表示の規則 ------------------------------------------------------------------

def test_the_page_has_the_paper_sentence_the_zone_line_the_nav_mark_and_no_script_or_style_attribute():
    html = page()
    assert "<h1>Performance</h1>" in html and "paper の記録です" in html
    assert "<p>表示 tz: Asia/Tokyo (+09:00)</p>" in html
    nav = re.search(r"<nav>(.*?)</nav>", html, re.S).group(1)
    assert re.findall(r'<a href="([^"]*)" aria-current="page">', nav) == ["/performance"]
    assert "<script" not in html and " style=" not in html and "<svg" not in html
    assert html.count('<meta http-equiv="refresh" content="30">') == 1
    assert re.findall(r"<style>(.*?)</style>", html, re.S) == [render.STYLE]


def test_a_get_and_a_head_have_the_same_headers_and_the_head_has_no_body():
    got, _ = get()
    head, _ = get(method="HEAD")
    assert head.status == 200 and head.body == b""
    assert head.headers == got.headers and int(got.headers["Content-Length"]) == len(got.body)
    for name, value in SECURITY_HEADERS.items():
        assert got.headers[name] == value


def test_times_are_shown_in_the_display_zone_with_the_fixed_format():
    html = page()
    assert "2026-11-19 11:59:30 +09:00" in html      # snapshot_at
    assert "2026-11-19 12:00:00 +09:00" in html      # state_fetched_at
    assert "2026-11-10 09:00:00 +09:00" in html      # dd_max_at
    assert "2026-10-05 09:00:00 +09:00" in html      # marks_since
    assert "T02:59:30" not in html


def test_the_post_methods_are_still_refused():
    response = handle_request(Request("POST", "/performance", {}), Client())
    assert response.status == 405 and response.headers["Allow"] == "GET, HEAD"


# --- 今の状態 ------------------------------------------------------------------------

@pytest.mark.parametrize("kill_switch", [
    {"latched": True, "generation": 4, "reconcile_required": False},
    {"latched": False, "generation": 4, "reconcile_required": True},
    {"latched": False, "generation": 4, "reconcile_required": False},
    {"latched": "yes"}, {"latched": False}, None, "x", [], 7])
def test_the_kill_switch_uses_the_status_screen_word_and_mark(kill_switch):
    html = page(payload(current=current(kill_switch=kill_switch)))
    expected = marks.render_verdict(marks.classify_kill_switch(kill_switch))
    block = html.split("今の状態", 1)[1]
    assert expected in block


def test_the_generation_and_the_state_time_are_shown_beside_the_kill_switch():
    html = page()
    assert re.search(r"<strong>世代</strong>3</div>", html)


@pytest.mark.parametrize("hold,word,level", [
    ("kill_switch", "kill_switch", "danger"), ("unknown", "unknown", None),
    ("stale", "stale", "warn"), ("drawdown", "drawdown", "danger"), ("none", "none", "ok")])
def test_the_fill_hold_word_is_the_daemon_value_with_its_own_mark(hold, word, level):
    html = page(payload(current=current(fill_hold=hold)))
    card = re.search(r"<strong>新規の約定</strong>(.*?)</div>", html, re.S).group(1)
    assert f"{word}" in card
    if level is None:
        assert 'class="st"' in card and "? " in card
    else:
        assert f'class="st {level}"' in card


@pytest.mark.parametrize("hold", [None, "", "KILL_SWITCH", "ok", "none ", 0, True, ["none"],
                                  {"none": 1}, "<b>none</b>"])
def test_a_fill_hold_outside_the_table_is_unknown_and_never_normal(hold):
    html = page(payload(current=current(fill_hold=hold)))
    card = re.search(r"<strong>新規の約定</strong>(.*?)</div>", html, re.S).group(1)
    assert "? 不明" in card and "○" not in card and "<b>" not in card


def test_a_missing_key_in_current_is_unknown_for_the_fill_hold():
    data = payload()
    del data["current"]["fill_hold"]
    html = page(data)
    assert "? 不明" in re.search(r"<strong>新規の約定</strong>(.*?)</div>", html, re.S).group(1)


@pytest.mark.parametrize("value", [None, "x", [], 5])
def test_current_that_is_null_missing_or_of_the_wrong_type_is_unknown_everywhere(value):
    data = payload()
    data["current"] = value
    html = page(data)
    block = html.split("今の状態", 1)[1].split("</section>", 1)[0]
    assert "○" not in block
    assert block.count("? 不明") >= 2
    assert "kill switch" in block and "新規の約定" in block


def test_current_missing_entirely_is_the_same_as_null():
    data = payload()
    del data["current"]
    block = page(data).split("今の状態", 1)[1].split("</section>", 1)[0]
    assert "○" not in block and "? 不明" in block


def test_the_account_numbers_are_formatted_and_a_stale_or_unknown_freshness_is_not_fresh():
    html = page(payload(current=current(snapshot_freshness="stale", snapshot_age_sec=700.0,
                                        fill_hold="stale")))
    assert "9,000.00" not in html and "9000.00" in html and "500.00" in html
    assert "2.5%" in html and "10.0%" in html
    assert re.search(r"<strong>最新記録の鮮度</strong><span class=\"st warn\">△ stale", html)
    odd = page(payload(current=current(snapshot_freshness="fresh!", snapshot_age_sec=True,
                                       dd_pct=float("nan"), headroom="x", kill_level=None)))
    card = re.search(r"<strong>最新記録の鮮度</strong>(.*?)</div>", odd, re.S).group(1)
    assert "? 不明" in card
    assert odd.count("不明") >= 5


def test_the_fixed_sentences_of_the_current_frame_and_the_notes_are_all_there():
    text = body_text(page())
    for sentence in ("paper の記録です",
                     "余裕が正でも解除されたとは限りません",
                     "期間内にも履歴の欠けがあり得ます",
                     "kill switch は、equity が作動の水準以下のときに建てる判断が出ると作動します。"
                     "線を割っただけでは印は付きません"):
        assert sentence in text, sentence
    assert "event の記録は 2026-10-05 09:00:00 +09:00 以降です" in text


def test_without_any_event_record_the_note_says_so():
    text = body_text(page(payload(marks_since=None)))
    assert "event の記録はありません" in text and "以降です" not in text


# --- 資金推移の表 -----------------------------------------------------------------------

def test_the_stats_table_shows_the_start_end_extremes_and_the_worst_drawdown_with_its_time():
    html = page()
    section = html.split("<h2>数字で読む</h2>", 1)[1].split("</section>", 1)[0]
    for text in ("10000.00", "9500.50", "9400.25", "9100.00", "10400.00", "9000.00", "10300.00",
                 "12.3%", "2026-11-10 09:00:00 +09:00", "JPY"):
        assert text in section, text
    assert "2026-10-20 12:01:00 +09:00" in section and "2026-11-19 11:59:30 +09:00" in section


def test_an_empty_period_says_there_is_no_record_and_keeps_the_marks_notes_and_state():
    data = payload(stats={k: None for k in payload()["stats"]})
    html = page(data)
    assert "この期間の記録はありません" in html
    assert "今の状態" in html and "期間内にも履歴の欠けがあり得ます" in html


@pytest.mark.parametrize("bad", [{"first_at": "x", "last_at": "y", "equity_first": "1"},
                                 {"first_at": True, "last_at": [], "dd_max": {}}])
def test_a_stats_object_with_wrong_typed_values_renders_unknown_values(bad):
    data = payload()
    data["stats"] = bad
    html = page(data)
    assert "この期間の記録はありません" not in html
    assert html.split("<h2>数字で読む</h2>", 1)[1].split("</section>", 1)[0].count("不明") >= 5


def test_the_marks_table_has_the_kind_words_the_times_and_the_two_rebase_columns():
    marks_ = [mark("latched"), mark("released", occurred_at=None, time_source="event_ts"),
              mark("rebase", occurred_at=None, recorded_at=None, generation=None,
                   source_snapshot_ts="2026-11-17T00:00:00+00:00",
                   applied_at="2026-11-18T12:00:00+00:00", time_source="ledger"),
              mark("cashflow", generation=None, time_source="snapshot"),
              mark("unknown_hwm_drop", generation=None, time_source="snapshot")]
    html = page(payload(marks=marks_, marks_omitted=3))
    section = html.split("<h2>印</h2>", 1)[1].split("</section>", 1)[0]
    head = re.findall(r"<th>([^<]*)</th>", section)
    assert head == ["種別", "位置", "実時刻", "記録時刻", "帳簿の位置", "操作時刻", "世代", "出所"]
    rows = re.findall(r"<tr><td>.*?</tr>", section, re.S)
    assert len(rows) == 5
    assert "✕ 作動" in rows[0] and "↑ 解除" in rows[1] and "▽ HWM の付け直し" in rows[2]
    assert "◇ 入出金" in rows[3] and "△ 不明な HWM 下降" in rows[4]
    assert "2026-11-17 09:00:00 +09:00" in rows[2] and "2026-11-18 21:00:00 +09:00" in rows[2]
    assert "ほか 3 件" in section
    assert "2026-11-18 10:02:03 +09:00" in rows[0]


def test_an_empty_marks_table_says_so_and_an_unknown_kind_is_shown_as_unknown():
    assert "印はありません" in page()
    html = page(payload(marks=[mark("zzz"), mark(5), mark(None)]))
    assert html.count("? 不明") >= 3


def test_at_most_20_marks_are_shown_and_the_rest_are_counted_with_the_daemon_omission():
    html = page(payload(marks=[mark() for _ in range(25)], marks_omitted=2))
    section = html.split("<h2>印</h2>", 1)[1].split("</section>", 1)[0]
    assert len(re.findall(r"<tr><td>", section)) == 20
    assert "ほか 7 件" in section


def test_the_quality_and_other_source_warnings_are_shown_only_when_there_is_something():
    quiet = body_text(page())
    for word in ("paper 以外の記録", "不明な HWM 下降", "一部の記録を読めていません", "除外しました"):
        assert word not in quiet
    html = page(payload(other_sources=4, quality={
        "partial": True, "invalid_snapshots": 2, "invalid_marks": 1, "invalid_orders": 0,
        "unknown_hwm_drops": 3, "unplaced_closed_count": 0}))
    text = body_text(html)
    assert "paper 以外の記録が 4 行あります。この画面は paper だけを描いています" in text
    assert 'class="st warn">△ 注意</span> paper 以外' in html
    assert "不明な HWM 下降が 3 件あります" in text
    assert "一部の記録を読めていません" in text


# --- 形の検査と「不明」 ---------------------------------------------------------------------

@pytest.mark.parametrize("key,value", [
    ("points", "x"), ("points", None), ("points", {}), ("points", [1]),
    ("hwm_steps", "x"), ("hwm_steps", [None]), ("marks", "x"), ("marks", [1, 2]),
    ("strategies", "x"), ("strategies", None), ("strategies", [3]), ("stats", "x"),
    ("stats", None), ("stats", [])])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_an_upper_level_type_mismatch_is_502_for_get_and_head(key, value, method):
    data = payload()
    data[key] = value
    response, _ = get(data, method=method)
    assert response.status == 502
    assert (response.body == b"") is (method == "HEAD")


@pytest.mark.parametrize("key", ["points", "hwm_steps", "marks", "strategies", "stats"])
def test_a_missing_upper_level_key_is_502(key):
    data = payload()
    del data[key]
    assert get(data)[0].status == 502


@pytest.mark.parametrize("reply", [Reply(404, None, "not_found"), Reply(503, None, "unavailable"),
                                   Reply(200, None, None), Reply(200, [], None),
                                   Reply(500, {}, "internal")])
def test_a_failed_reply_is_502(reply):
    assert get(client=Client(reply=reply))[0].status == 502


def test_a_daemon_that_does_not_have_the_endpoint_is_502():
    class Old:
        def performance(self, *, range="30d"):
            return Reply(404, None, "not_found")

    assert handle_request(Request("GET", "/performance", {}), Old()).status == 502


@pytest.mark.parametrize("value", [None, True, "x", float("nan"), float("inf"), [], {}, -1e999])
def test_a_bad_individual_value_is_unknown_not_a_failure(value):
    data = payload()
    data["stats"].update({"equity_first": value, "equity_last": value, "dd_max": value,
                          "dd_max_at": value, "first_at": value})
    data["current"].update({"snapshot_at": value, "dd_pct": value, "kill_level": value,
                            "headroom": value, "snapshot_age_sec": value, "state_fetched_at": value})
    data["marks"] = [mark(plot_at=value, occurred_at=value, recorded_at=value, generation=value)]
    data["marks_since"] = value
    response, _ = get(data)
    assert response.status == 200
    assert "不明" in response.body.decode()


@pytest.mark.parametrize("zone", [None, 5, "Not/AZone", "", "<b>"])
def test_a_bad_display_timezone_falls_back_to_utc(zone):
    data = payload()
    data["display_timezone"] = zone
    html = page(data)
    assert "表示 tz: UTC (+00:00)" in html and "2026-11-19 02:59:30 +00:00" in html


# --- 隔離・予算 ------------------------------------------------------------------------------

HOSTILE = '<script>alert(1)</script><img src=x onerror=y>"\'&'


def test_hostile_text_anywhere_in_the_reply_never_becomes_an_element():
    data = payload(account_currency=HOSTILE)
    data["range"] = HOSTILE
    data["current"]["kill_switch"]["detail"] = HOSTILE
    data["current"]["fill_hold"] = HOSTILE
    data["current"]["snapshot_freshness"] = HOSTILE
    data["marks"] = [mark(kind=HOSTILE, time_source=HOSTILE, plot_at=HOSTILE, occurred_at=HOSTILE)]
    data["strategies"] = [{"origin": HOSTILE, "detail": HOSTILE, "cumulative": [{"t": HOSTILE}]}]
    data["omissions"] = {"strategy_rows": HOSTILE}
    data["quality"]["partial"] = HOSTILE
    data["extra"] = {"note": HOSTILE}
    html = page(data)
    for raw in ("<script", "<img", "onerror", "<svg", "<b>"):
        assert raw not in html.lower().replace("&lt;", ""), raw
    assert "alert(1)" not in html
    assert not re.findall(r"<[a-z]+[^>]* (?:on[a-z]+|style)=", html)
    # 値は表示に載らない (allowlist の field だけ)
    assert html.count("<script") == 0


def test_a_strategy_name_or_a_detail_is_not_shown_in_this_view_at_all():
    data = payload()
    data["strategies"] = [{"origin": "signal:secret_name", "detail": "secret-detail-text"}]
    html = page(data)
    assert "secret_name" not in html and "secret-detail-text" not in html


def test_the_currency_is_shown_only_when_it_is_a_short_plain_code():
    assert "JPY" in page()
    for bad in ("<b>", "J" * 17, "jp y", "", None, 5):
        html = page(payload(account_currency=bad))
        assert "<b>" not in html and ("J" * 17) not in html


def maximal() -> dict:
    big = 1.7976931348623157e308
    data = payload()
    data["account_currency"] = "X" * 16
    data["current"] = current(dd_pct=big, kill_level=-big, headroom=big, kill_pct=big,
                              snapshot_age_sec=big,
                              kill_switch={"latched": True, "generation": 10 ** 40,
                                           "reconcile_required": True})
    data["stats"] = {k: (big if "at" not in k else "2026-11-19T02:59:30.123456+00:00")
                     for k in data["stats"]}
    data["marks"] = [mark(kind=kind, generation=10 ** 40, source_snapshot_ts=NOW, applied_at=NOW)
                     for kind in ["unknown_hwm_drop", "rebase", "cashflow", "released"] * 6]
    data["marks_omitted"] = 10 ** 40
    data["other_sources"] = 10 ** 40
    data["quality"] = {"partial": True, "invalid_snapshots": 10 ** 40, "invalid_marks": 10 ** 40,
                       "invalid_orders": 10 ** 40, "unknown_hwm_drops": 10 ** 40,
                       "unplaced_closed_count": 10 ** 40}
    data["points"] = [{"x": "p" * 100} for _ in range(280)]
    data["hwm_steps"] = [{"x": "p" * 100} for _ in range(320)]
    data["strategies"] = [{"origin": "o" * 71, "cumulative": [{"t": "t", "pnl": 1.0}] * 122}] * 32
    return data


def test_the_largest_reply_stays_within_the_page_budget_and_the_part_budgets():
    html = page(maximal())
    assert len(html.encode()) <= render.MAX_RESPONSE_BYTES
    assert len(html.encode()) <= perf_pages.PAGE_BUDGET_BYTES
    assert "Output clipped" not in html
    section = html.split("<h2>印</h2>", 1)[1].split("</section>", 1)[0]
    assert len(section.encode()) <= perf_pages.MARKS_BUDGET_BYTES
    assert len(re.findall(r"<tr><td>", section)) == 20
    for word in ("今の状態", "数字で読む", "paper の記録です", "余裕が正でも解除されたとは限りません"):
        assert word in html
    # 巨大な有限数は指数表記で、1 つの値が 24 文字を超えない
    assert all(len(cell) <= 24 for cell in re.findall(r"<td>([0-9.e+%-]+)</td>", html))


def test_a_marks_table_over_its_budget_is_replaced_whole_by_a_note_and_the_rest_stays(monkeypatch):
    monkeypatch.setattr(perf_pages, "MARKS_BUDGET_BYTES", 100)
    html = page(payload(marks=[mark() for _ in range(5)]))
    section = html.split("<h2>印</h2>", 1)[1].split("</section>", 1)[0]
    assert "<table" not in section and "表示の上限を超えたため" in section and "5 件" in section
    assert "今の状態" in html and "数字で読む" in html and "Output clipped" not in html


def test_a_page_over_the_total_budget_replaces_parts_and_never_clips(monkeypatch):
    monkeypatch.setattr(perf_pages, "PAGE_BUDGET_BYTES", 6000)
    html = page(payload(marks=[mark() for _ in range(20)]))
    assert "Output clipped" not in html and len(html.encode()) <= 6000
    assert "今の状態" in html and "paper の記録です" in html


# --- client ----------------------------------------------------------------------------------

def _client(cls=WebClient):
    calls = []
    client = cls.__new__(cls)
    client._get = lambda path, **kwargs: calls.append((path, kwargs)) or "reply"
    return client, calls


@pytest.mark.parametrize("cls", [WebClient, WebWriteClient])
def test_the_client_reads_the_performance_endpoint_with_the_range_only(cls):
    client, calls = _client(cls)
    assert client.performance() == "reply" and client.performance(range="all") == "reply"
    assert calls == [("/v1/performance", {"range": "30d"}), ("/v1/performance", {"range": "all"})]


@pytest.mark.parametrize("value", ["", "1d", "30D", "7d&x=1", "../", None, 7, "all "])
def test_a_range_outside_the_four_words_is_refused_before_any_request(value):
    client, calls = _client()
    with pytest.raises(ValueError):
        client.performance(range=value)
    assert calls == []


def test_the_response_data_is_not_mutated_by_rendering():
    data = payload(marks=[mark()])
    before = copy.deepcopy(data)
    page(data)
    assert data == before


# --- 異常値・予算の追加の検査 ----------------------------------------------------------------

HUGE_INT = 10 ** 309


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_an_integer_too_large_for_a_float_is_unknown_in_every_numeric_cell(method):
    data = payload()
    data["stats"].update({k: HUGE_INT for k in data["stats"] if "at" not in k})
    data["current"].update({"dd_pct": HUGE_INT, "kill_level": HUGE_INT, "headroom": HUGE_INT,
                            "kill_pct": HUGE_INT, "snapshot_age_sec": HUGE_INT,
                            "kill_switch": {"latched": True, "generation": HUGE_INT,
                                            "reconcile_required": False}})
    data["marks"] = [mark(generation=HUGE_INT)]
    data["marks_omitted"] = HUGE_INT
    data["other_sources"] = HUGE_INT
    data["quality"].update({"invalid_snapshots": HUGE_INT, "invalid_marks": HUGE_INT,
                            "unknown_hwm_drops": HUGE_INT})
    response, _ = get(data, method=method)
    assert response.status == 200
    if method == "GET":
        assert "不明" in response.body.decode()


def test_a_page_over_the_total_budget_keeps_the_shown_and_omitted_mark_counts(monkeypatch):
    monkeypatch.setattr(perf_pages, "PAGE_BUDGET_BYTES", 6000)
    html = page(payload(marks=[mark() for _ in range(25)], marks_omitted=7))
    assert "Output clipped" not in html and len(html.encode()) <= 6000
    assert "<table><thead><tr><th>種別" not in html
    assert "ほか 12 件" in html and "<h2>印</h2>" in html


@pytest.mark.parametrize("stats", [{}, {"equity_first": 1.0}, {"first_at": None},
                                   {"last_at": None}])
def test_a_stats_object_missing_the_empty_period_shape_is_unknown_not_empty(stats):
    data = payload()
    data["stats"] = stats
    html = page(data)
    assert "この期間の記録はありません" not in html
    assert "<h2>数字で読む</h2><table>" in html
    assert html.split("<h2>数字で読む</h2>", 1)[1].split("</section>", 1)[0].count("不明") >= 5


def test_the_marks_budget_counts_the_heading_and_the_omission_note(monkeypatch):
    data = payload(marks=[mark() for _ in range(3)], marks_omitted=5)
    html = page(data)
    section = html.split("<h2>印</h2>", 1)[1].split("</section>", 1)[0]
    table = section[:section.index("</table>") + len("</table>")]
    whole = "<h2>印</h2>" + section
    assert len(table.encode()) < len(whole.encode())
    monkeypatch.setattr(perf_pages, "MARKS_BUDGET_BYTES", len(table.encode()) + 1)
    shrunk = page(data)
    assert "<table><thead><tr><th>種別" not in shrunk
    assert "表示の上限を超えたため" in shrunk and "ほか 5 件" in shrunk
