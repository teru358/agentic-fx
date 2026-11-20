"""/performance の戦略別タブ: 表・累積損益の図・固定文・予算。"""
from __future__ import annotations

import re

import pytest

from afx_ops.web import perf_pages, render, svg, tabs
from tests.web.test_performance_chart import (ALLOWED_ATTRS, ALLOWED_TAGS, iso, parse, polylines,
                                              vertices)
from tests.web.test_performance_page import get, page, payload
from tests.web.test_render import PAGES

SENTENCE = ("その戦略のシグナルで起きた判断の成績です。建てる・決済する判断は LLM が行うので、"
            "戦略そのものの成績 (backtest) とは違います")


def row(origin="signal:rsi", *, closed=12, known=None, wins=7, losses=4, flats=1, rate=0.583,
        pf=1.4, avg_r=0.21, r_known=None, total=123.45, reasons=None, holding=1, pending=0,
        cumulative=None, pf_state=None, **over):
    known = closed if known is None else known
    base = {"origin": origin, "origin_count": 1, "closed_count": closed, "pnl_known_count": known,
            "r_known_count": known if r_known is None else r_known,
            "pnl_unknown_count": closed - known,
            "r_unknown_count": closed - (known if r_known is None else r_known),
            "open_count": holding, "close_pending_count": pending, "wins": wins, "losses": losses,
            "flats": flats, "win_rate": rate, "pf": pf,
            "pf_state": ("computed" if isinstance(pf, float) and pf == pf and abs(pf) != float("inf")
                         else "unknown") if pf_state is None else pf_state, "avg_r": avg_r, "total_pnl": total,
            "close_reasons": reasons or {"llm_close": 8, "sl": 3, "tp": 1, "other": 0},
            "cumulative": [{"t": iso(0), "pnl": 0.0}, {"t": iso(2000), "pnl": 50.0},
                           {"t": iso(3000), "pnl": total}, {"t": iso(10000), "pnl": total}]
            if cumulative is None else cumulative}
    base.update(over)
    return base


def data_of(*rows, **over):
    base = payload(strategies=list(rows))
    base["from"], base["to"] = iso(0), iso(10000)
    base.update(over)
    return base


def strategy_page(data=None, query=None):
    return page(data, {"tab": "strategy", **(query or {})})


def table_cells(html: str) -> list[list[str]]:
    table = html.split("<h2>起動理由ごとの表</h2>", 1)[1].split("</section>", 1)[0]
    return [[re.sub(r"<[^>]*>", "|", c).replace("&lt;", "<") for c in
             re.findall(r"<td>(.*?)</td>", r)] for r in re.findall(r"<tr>(.*?)</tr>", table)
            if "<td>" in r]


def figure_part(html: str) -> str:
    return html.split("<h2>累積損益の図</h2>", 1)[1].split("</section>", 1)[0]


# --- tab・query ---------------------------------------------------------------------------

def test_the_performance_tab_table_has_the_equity_tab_first_and_the_strategy_tab():
    assert tabs.tab_names("/performance") == ("equity", "strategy")
    assert tabs.default_tab("/performance") == "equity"


def test_the_strategy_tab_keeps_the_range_and_the_range_links_keep_the_tab():
    html = strategy_page(data_of(row()), {"range": "7d"})
    assert re.findall(r'<a href="([^"]*)" aria-current="page">', html.split("<h1>")[1]) \
        == ["/performance?tab=strategy&amp;range=7d", perf_pages.range_href("strategy", "7d")]
    assert perf_pages.range_href("strategy", "90d") == "/performance?tab=strategy&amp;range=90d"
    assert perf_pages.range_href("strategy", "30d") == "/performance?tab=strategy"
    assert 'href="/performance?range=7d"' in html


def test_an_unknown_tab_value_is_still_400_and_the_daemon_is_not_called():
    response, client = get(query={"tab": "strategies"})
    assert response.status == 400 and client.calls == []


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_the_strategy_tab_answers_get_and_head(method):
    response, client = get(data_of(row()), {"tab": "strategy"}, method=method)
    assert response.status == 200 and client.calls == ["30d"]
    assert (response.body == b"") is (method == "HEAD")


# --- 固定文 ---------------------------------------------------------------------------------

def test_the_fixed_sentence_sits_under_the_heading_and_the_two_notes_are_always_shown():
    for data in (data_of(row()), data_of(), payload(strategies=[])):
        html = strategy_page(data)
        assert f"<h2>戦略別の成績</h2><p>{SENTENCE}</p>" in html
        for note in ("累積損益の合計は equity の変化と一致しません (含み損益、期間の外で決済した取引、"
                     "スワップを含まないため)", "同じ名前の plugin の版が変わっても、同じ行に数えます"):
            assert note in html.split("<h2>注記</h2>")[1]
    assert "paper の記録です" in strategy_page(data_of(row()))


def test_the_strategy_tab_shows_no_current_state_block_nor_the_equity_figure():
    html = strategy_page(data_of(row()))
    assert "今の状態" not in html and "資金推移の図" not in html and "<h2>印</h2>" not in html
    assert re.findall(r"<svg[^>]*>", html) and "equity" not in figure_part(html)


# --- 表 -------------------------------------------------------------------------------------------

def test_the_table_has_the_nine_columns_with_the_unit_and_a_right_aligned_class():
    html = strategy_page(data_of(row()))
    table = html.split("<h2>起動理由ごとの表</h2>", 1)[1]
    assert re.findall(r"<th>(.*?)</th>", table) == [
        "起動理由", "件数", "勝 / 負 / 分", "勝率", "PF", "平均 R (手数料込み)",
        "損益の合計 (JPY)", "決済の内訳", "保有中"]
    assert '<div class="scroll"><table class="num">' in table


def test_a_row_with_twelve_closed_shows_the_rates_as_reference_and_the_counts():
    cells = table_cells(strategy_page(data_of(row())))[0]
    assert cells == ["signal:rsi", "12 (損益既知 12 / 不明 0)", "7 / 4 / 1", "58.3% (参考)", "1.40 (参考)",
                     "+0.21 (参考)|R 算出 12 / 決済 12 件", "+123.45", "判断 8 / SL 3 / TP 1", "1"]


def test_fewer_than_five_known_hides_the_rates_and_thirty_or_more_drops_the_reference_mark():
    few = table_cells(strategy_page(data_of(row(closed=4, wins=2, losses=1, flats=1))))[0]
    assert few[3] == "–" and few[4] == "–" and few[5].startswith("–|")
    exact = table_cells(strategy_page(data_of(row(closed=5, wins=3, losses=1, flats=1))))[0]
    assert exact[3] == "58.3% (参考)"
    edge = table_cells(strategy_page(data_of(row(closed=30, wins=15, losses=10, flats=5))))[0]
    assert "参考" not in edge[3] and "参考" not in edge[4] and "参考" not in edge[5]
    below = table_cells(strategy_page(data_of(row(closed=29))))[0]
    assert "参考" in below[3]


def test_each_rate_uses_its_own_denominator():
    cells = table_cells(strategy_page(data_of(row(closed=40, known=40, r_known=4))))[0]
    assert cells[3] == "58.3%" and cells[4] == "1.40" and cells[5].startswith("–|") \
        and "R 算出 4 / 決済 40 件" in cells[5]
    unknown = table_cells(strategy_page(data_of(row(closed=40, known=3, r_known=40))))[0]
    assert unknown[1] == "40 (損益既知 3 / 不明 37)" and unknown[3] == "–" and unknown[5].startswith("+0.21|")


def test_the_thresholds_come_from_the_daemon_and_unreadable_ones_make_the_rates_unknown():
    data = data_of(row(closed=6, wins=3, losses=2, flats=1))
    data["thresholds"] = {"min_count": 3, "reference_count": 6}
    cells = table_cells(strategy_page(data))[0]
    assert cells[3] == "58.3%" and "参考" not in cells[4]
    for bad in ({}, {"min_count": "5", "reference_count": 30}, {"min_count": 9, "reference_count": 3},
                {"min_count": True, "reference_count": 30}, None):
        data["thresholds"] = bad
        cells = table_cells(strategy_page(data))[0]
        assert cells[3] == cells[4] == "不明" and cells[5].startswith("不明|")


def test_a_profit_factor_without_losses_is_told_so_and_not_shown_as_a_number():
    cells = table_cells(strategy_page(data_of(row(closed=40, wins=40, losses=0, flats=0, pf=None,
                                                      pf_state="no_losses"))))[0]
    assert cells[4] == "損失なし"
    assert table_cells(strategy_page(data_of(row(closed=40, pf=None))))[0][4] == "不明"


def test_the_cron_unknown_and_merged_rows_are_written_as_designed_and_names_are_not_cut():
    long_name = "signal:" + "a" * 64
    rows = [row(long_name, closed=9), row("cron", closed=4, wins=2, losses=2, flats=0),
            row("unknown", closed=2, wins=1, losses=1, flats=0),
            row("other", closed=1, wins=1, losses=0, flats=0, origin_count=7)]
    cells = table_cells(strategy_page(data_of(*rows)))
    assert [c[0] for c in cells] == [long_name, "定期の判断 (cron、戦略に属さない)", "帰属不明",
                                     "ほか 7 件"]


def test_an_origin_outside_the_grammar_is_never_written_into_the_page():
    hostile = ['signal:<script>x</script>', "signal:Rsi", 'x" onload="y', "signal:" + "a" * 65, 5, None]
    html = strategy_page(data_of(*[row(o) for o in hostile]))
    assert "<script" not in html and "onload" not in html and "Rsi" not in html
    assert [c[0] for c in table_cells(html)] == ["不明"] * len(hostile)
    assert not re.findall(r"<svg[^>]*>", html)


def test_the_close_reasons_list_the_nonzero_ones_and_a_holding_only_row_still_appears():
    held = row("signal:macd", closed=0, known=0, wins=0, losses=0, flats=0, rate=None, pf=None,
               avg_r=None, total=None, reasons={"llm_close": 0, "sl": 0, "tp": 0, "other": 0},
               holding=2, pending=1, cumulative=[])
    html = strategy_page(data_of(held))
    cells = table_cells(html)[0]
    assert cells[1:] == ["0 (損益既知 0 / 不明 0)", "0 / 0 / 0", "–", "–", "–|R 算出 0 / 決済 0 件", "–", "–", "2 (決済未確定 1)"]
    assert "この期間に決済した取引はありません" in html and "<svg" not in html
    assert svg.NOTE_NO_SERIES in figure_part(html)


def test_no_row_at_all_says_so_and_keeps_the_sentences():
    html = strategy_page(data_of())
    assert "この期間に決済した取引も、保有中の取引もありません" in html and "<table" not in html
    assert SENTENCE in html and svg.NOTE_NO_SERIES in figure_part(html)


def test_unreadable_values_are_unknown_never_a_normal_looking_number():
    bad = row(closed=40, rate="x", pf=float("nan"), avg_r=True, total=float("inf"),
              wins=-1, losses=None, flats=1.5, reasons={"llm_close": -2, "sl": "1"}, holding="3")
    cells = table_cells(strategy_page(data_of(bad)))[0]
    assert cells[3] == cells[4] == "不明" and cells[5].startswith("不明|") and cells[6] == "不明"
    assert cells[2] == "不明 / 不明 / 不明" and cells[8] == "不明"
    assert cells[7] == "判断 不明 / SL 不明 / TP 不明 / その他 不明"


def test_a_missing_close_reasons_table_is_unknown_and_the_page_never_raises():
    cells = table_cells(strategy_page(data_of(row(close_reasons=None))))[0]
    assert cells[7] == "判断 不明 / SL 不明 / TP 不明 / その他 不明"
    for junk in ([{}], [{"origin": "cron"}], [{"origin": "cron", "cumulative": "x"}]):
        assert strategy_page(data_of() | {"strategies": junk})


def test_the_merged_row_count_and_quality_warnings_are_shown():
    data = data_of(row())
    data["omissions"]["strategy_rows"] = 5
    data["quality"].update(partial=True, invalid_orders=2, unplaced_closed_count=3)
    html = strategy_page(data)
    assert "5 件の起動理由を「ほか」の行にまとめました" in html
    assert "一部の記録を読めていません" in html and "読めない注文があります" in html \
        and "決済の時刻を読めない取引があり" in html
    assert "paper 以外の記録" not in html


# --- 累積損益の図 -------------------------------------------------------------------------------

ALLOWED_STRATEGY_CLASSES = {"ax", "c1", "c2", "c3", "c4", "cr"}


def test_each_origin_is_a_step_line_that_moves_horizontally_then_vertically_at_each_close():
    html = strategy_page(data_of(row(cumulative=[{"t": iso(0), "pnl": 0.0},
                                                 {"t": iso(5000), "pnl": 40.0},
                                                 {"t": iso(10000), "pnl": 40.0}])))
    (line,) = polylines(html, "c1")
    assert len(line) == 5
    (x0, y0), (x1, ya), (x2, yb), (x3, yc), (x4, yd) = line
    assert ya == y0 and x1 == x2 and yb < ya and yc == yb and x3 == x4 and yd == yc
    assert x0 < x1 < x3 and x4 == 952


def test_the_zero_line_is_drawn_and_the_start_of_every_line_sits_on_it():
    html = strategy_page(data_of(row(), row("signal:macd", closed=3)))
    elements = parse(figure_part(html)).elements
    zero = [a for t, a in elements if t == "line" and a.get("class") == "ax"
            and a["y1"] == a["y2"] and a["x1"] == "72" and a["x2"] == "952"
            and a["y1"] != str(svg.Y1)]
    assert len(zero) == 1
    for css in ("c1", "c2"):
        (line,) = polylines(html, css)
        assert str(line[0][1]) == zero[0]["y1"]


def test_series_are_told_apart_by_color_class_and_the_cron_line_is_the_neutral_long_dash():
    rows = [row("signal:a"), row("signal:b"), row("signal:c"), row("cron", closed=4),
            row("signal:d")]
    figure = figure_part(strategy_page(data_of(*rows)))
    classes = [a["class"] for t, a in parse(figure).elements if t == "polyline"]
    assert classes == ["c1", "c2", "c3", "cr", "c4"]
    assert re.search(r"\n\.cr\{[^}]*stroke-dasharray:10 4", render.STYLE)
    css = re.findall(r"\n\.(c[1-4])\{[^}]*stroke-dasharray:([^;}]*)", render.STYLE)
    assert len({v for _, v in css} | {"10 4"}) == 4


def test_the_legend_lists_only_the_drawn_series_with_the_end_value_the_table_shows():
    rows = [row("signal:a", total=-12.5, cumulative=[{"t": iso(0), "pnl": 0.0},
                                                     {"t": iso(9000), "pnl": -12.5},
                                                     {"t": iso(10000), "pnl": -12.5}]),
            row("signal:b", total=88.0, closed=0 + 9, known=9)]
    html = strategy_page(data_of(*rows))
    figure = figure_part(html)
    texts = [t.strip() for t in parse(figure).texts if "終点" in t]
    cells = table_cells(html)
    assert texts == [f"{r['origin']} 終点 {cells[i][6]}" for i, r in enumerate(rows)]
    assert cells[0][6] == "-12.50" and cells[1][6] == "+88.00"
    assert [a["class"] for t, a in parse(figure).elements if t == "line"
            and a["y1"] == a["y2"] and a["x2"] == str(int(a["x1"]) + 28)] == ["c1", "c2"]


def test_the_end_of_every_line_matches_the_table_total_for_every_drawn_row():
    rows = [row("signal:a", total=33.33), row("cron", closed=7, total=-1.0),
            row("signal:b", total=0.0, wins=0, losses=0, flats=12)]
    html = strategy_page(data_of(*rows))
    cells = table_cells(html)
    ends = re.findall(r"終点 ([^<]*)</text>", figure_part(html))
    assert ends == [c[6] for c in cells]


def test_a_series_with_no_known_pnl_is_a_zero_line_told_as_unknown():
    unknown = row("signal:x", closed=3, known=0, wins=0, losses=0, flats=0, rate=None, pf=None,
                  avg_r=None, total=None,
                  cumulative=[{"t": iso(0), "pnl": 0.0}, {"t": iso(10000), "pnl": 0.0}])
    html = strategy_page(data_of(unknown))
    figure = figure_part(html)
    assert "signal:x 損益不明" in figure and "終点" not in figure
    assert "signal:x: 損益が不明のため、0 の線です" in figure
    assert table_cells(html)[0][6] == "–"


def test_the_series_with_more_than_the_history_says_the_old_closes_are_folded_into_the_start():
    cumulative = [{"t": iso(4000), "pnl": 500.0}] + [{"t": iso(4000 + i), "pnl": 500.0 + i}
                                                      for i in range(1, 6)]
    html = strategy_page(data_of(row(closed=125, cumulative=cumulative)))
    start = svg._full(svg._epoch(iso(4000)), "Asia/Tokyo")
    assert f"signal:rsi: 古い決済 5 件は {start} の始点 1 点にまとめています" in figure_part(html)
    assert "古い決済" not in strategy_page(data_of(row(closed=120)))


def test_series_past_the_four_colors_and_the_daemons_hidden_series_are_counted_as_table_only():
    rows = [row(f"signal:s{i}") for i in range(6)]
    data = data_of(*rows)
    data["omissions"]["strategy_series"] = 3
    figure = figure_part(strategy_page(data))
    assert len(re.findall(r"<polyline", figure)) == 4
    assert "ほか 5 行は表のみです" in figure
    assert "表のみ" not in figure_part(strategy_page(data_of(row())))


def test_an_unreadable_point_cuts_the_line_and_never_joins_across_it():
    cumulative = [{"t": iso(0), "pnl": 0.0}, {"t": iso(2000), "pnl": 10.0}, {"t": "bad", "pnl": 5.0},
                  {"t": iso(8000), "pnl": 20.0}, {"t": iso(10000), "pnl": float("nan")},
                  {"t": iso(10001), "pnl": 20.0}]
    html = strategy_page(data_of(row(cumulative=cumulative)))
    lines = polylines(html, "c1")
    assert [len(v) for v in lines] == [3, 2, 2]
    for before, after in zip(lines, lines[1:]):
        assert max(x for x, _ in before) < min(x for x, _ in after)


def test_values_that_are_unreadable_or_huge_are_not_drawn_as_lines():
    for bad in ({"t": iso(0), "pnl": 1e30}, {"t": iso(0), "pnl": True}, {"t": 5, "pnl": 1.0}, "x",
                {"t": "9999-12-31T00:00:00+00:00", "pnl": 1.0}):
        html = strategy_page(data_of(row(cumulative=[bad])))
        assert "<polyline" not in html and svg.NOTE_NO_SERIES in figure_part(html)


def test_the_figure_has_only_allowlisted_elements_with_integer_coordinates_inside_its_viewbox():
    rows = [row("signal:a"), row("signal:b"), row("signal:c"), row("signal:d"), row("cron", closed=4)]
    figure = figure_part(strategy_page(data_of(*rows)))
    markup = re.search(r"<svg.*</svg>", figure, re.S).group(0)
    width, height = map(int, re.search(r'viewBox="0 0 (\d+) (\d+)"', markup).groups())
    assert width == 960 and height > 260
    for tag, attrs in parse(markup).elements:
        assert tag in ALLOWED_TAGS - {"title"} and set(attrs) <= ALLOWED_ATTRS[tag], (tag, attrs)
        assert set(attrs.get("class", "").split()) <= ALLOWED_STRATEGY_CLASSES
        for name in ("x", "y", "x1", "y1", "x2", "y2"):
            if name in attrs:
                assert re.fullmatch(r"\d+", attrs[name])
                assert 0 <= int(attrs[name]) <= (width if "x" in name else height)
        if "points" in attrs:
            assert all(0 <= x <= width and 0 <= y <= height for x, y in vertices(attrs["points"]))
    for forbidden in ("<script", "<foreignObject", "<image", "<use", "<a ", "<style", "style=",
                      " on", "fill=", "stroke=", "#"):
        assert forbidden not in markup, forbidden
    used = {c for _, a in parse(markup).elements for c in a.get("class", "").split()}
    for css in used:
        assert re.search(r"(^|\n)\." + css + r"\{", render.STYLE), css


def test_the_axes_are_in_the_display_zone_and_the_legend_text_is_escaped():
    figure = figure_part(strategy_page(data_of(row())))
    assert "横軸: 表示 tz の時刻 (2026-10-20 09:00:00 +09:00 〜 " in figure
    assert "縦軸: 期間の初めを 0 とした累積損益 (JPY)" in figure
    data = data_of(row())
    data["account_currency"] = "<svg onload=x>"
    data["display_timezone"] = "<script>"
    html = strategy_page(data)
    assert re.findall(r"<svg[^>]*>", html) == [re.search(r"<svg[^>]*>", html).group(0)]
    assert "<script" not in html and "onload" not in html.replace("&lt;", "")


def test_no_page_other_than_performance_has_an_svg():
    for path in PAGES:
        if path != "/performance":
            body = page_of(path)
            assert "<svg" not in body, path


def page_of(path):
    from afx_ops.web.request import Request, handle_request
    from tests.web.test_render import ReadClient
    return handle_request(Request("GET", path, {}), ReadClient()).body.decode()


# --- 予算 -----------------------------------------------------------------------------------------

BIG_COUNT = 10 ** 15 - 1
HUGE = -999999999999.99


def worst_row(index: int, *, closed=BIG_COUNT, points=122):
    origin = "signal:" + chr(97 + index % 26) * 30 + f"{index:02d}" + "z" * 32
    assert len(origin) == 71 or len(origin.encode()) <= 71
    cumulative = [{"t": iso(100 + i * 80), "pnl": (HUGE if i % 2 else -HUGE) / 1.0}
                  for i in range(points)]
    return row(origin, closed=closed, known=closed, wins=BIG_COUNT, losses=BIG_COUNT,
               flats=BIG_COUNT, rate=0.3333, pf=HUGE, avg_r=HUGE, r_known=closed, total=HUGE,
               reasons={"llm_close": BIG_COUNT, "sl": BIG_COUNT, "tp": BIG_COUNT,
                        "other": BIG_COUNT},
               holding=BIG_COUNT, pending=BIG_COUNT, cumulative=cumulative)


def worst_data():
    rows = [worst_row(i) for i in range(32)]
    rows[0]["origin"] = "cron"
    data = data_of(*rows)
    data["from"], data["to"] = iso(0), iso(100 + 122 * 80)
    return data


def test_the_static_worst_case_of_the_strategy_table_and_figure_fits_their_part_budgets():
    assert svg.STRATEGY_BUDGET_BYTES == 14 * 1024 and perf_pages.STRATEGY_TABLE_BUDGET_BYTES == 22 * 1024
    assert perf_pages.STRATEGY_PAGE_BUDGET_BYTES == 48 * 1024
    vertex_max = len("960,260 ")
    stair = 2 * (svg.MAX_SERIES_POINTS - 1) + 1
    wrapper = len('<polyline class="c1" points=""/>')
    sample = svg._Scale(1.7e9, 1.7e9 + 86400 * 400, -1.2345e11, 1.2e11)
    legend_cell = len(svg._strategy_legend([("c1", "signal:" + "a" * 64 + " 終点 " + "-999999999999.99")])
                      .encode("utf-8"))
    fixed = len(svg._frame(sample, "Asia/Tokyo").encode("utf-8")) + 5 * legend_cell + 600
    figure_worst = 5 * (stair * vertex_max + wrapper) + fixed
    assert figure_worst <= svg.STRATEGY_BUDGET_BYTES, figure_worst
    cells = perf_pages._strategy_row(worst_row(1), (5, 30))
    table_worst = 32 * len(cells.encode("utf-8")) + 2048
    assert table_worst <= perf_pages.STRATEGY_TABLE_BUDGET_BYTES, table_worst


def test_the_real_figure_and_table_for_the_largest_input_stay_within_their_budgets_and_the_page():
    data = worst_data()
    chart = svg.draw_strategies(data, "Asia/Tokyo", perf_pages._signed_amount)
    assert chart.svg and chart.notes == ()
    assert len(chart.svg.encode("utf-8")) <= svg.STRATEGY_BUDGET_BYTES
    assert len(re.findall(r"<polyline", chart.svg)) == 5
    table = perf_pages._strategy_table(data)
    assert "<table" in table and len(table.encode("utf-8")) <= perf_pages.STRATEGY_TABLE_BUDGET_BYTES
    assert len(table_cells(strategy_page(data))) == 32
    html = strategy_page(data)
    assert len(html.encode("utf-8")) <= perf_pages.STRATEGY_PAGE_BUDGET_BYTES
    assert "Output clipped" not in html and html.count("<svg") == 1
    for word in (SENTENCE, "起動理由ごとの表", "累積損益の図", "同じ名前の plugin の版が変わっても"):
        assert word in html


def test_a_figure_over_its_budget_is_replaced_whole_by_a_note_and_the_table_stays(monkeypatch):
    monkeypatch.setattr(svg, "STRATEGY_BUDGET_BYTES", 100)
    html = strategy_page(data_of(row()))
    assert "<svg" not in html and svg.NOTE_STRATEGY_OVER in html
    assert table_cells(html) and SENTENCE in html and "累積損益の合計は" in html


def test_a_table_over_its_budget_is_replaced_whole_by_a_note_and_the_figure_stays(monkeypatch):
    monkeypatch.setattr(perf_pages, "STRATEGY_TABLE_BUDGET_BYTES", 100)
    html = strategy_page(data_of(row(), row("cron", closed=4)))
    assert "<table" not in html and perf_pages.NOTE_TABLE_OVER in html
    assert html.count("<svg") == 1 and SENTENCE in html and "累積損益の合計は" in html


def test_a_page_over_the_total_budget_drops_the_figure_first_then_the_table(monkeypatch):
    data = data_of(row(), row("signal:b", closed=9))
    full = len(strategy_page(data).encode("utf-8"))
    monkeypatch.setattr(perf_pages, "STRATEGY_PAGE_BUDGET_BYTES", full - 1)
    html = strategy_page(data)
    assert "<svg" not in html and svg.NOTE_STRATEGY_OVER in html and table_cells(html)
    stage_two = len(html.encode("utf-8"))
    monkeypatch.setattr(perf_pages, "STRATEGY_PAGE_BUDGET_BYTES", stage_two - 1)
    html = strategy_page(data)
    assert "<table" not in html and perf_pages.NOTE_TABLE_OVER in html and "<svg" not in html
    assert "Output clipped" not in html and SENTENCE in html and "累積損益の合計は" in html


def test_the_style_is_unchanged_by_the_strategy_tab_and_stays_within_the_limit():
    assert len(render.STYLE.encode("utf-8")) <= 4096


# --- 異常な応答は正常に倒さない ---------------------------------------------------------------

def test_the_profit_factor_is_told_by_the_daemons_state_and_an_unreadable_pair_is_unknown():
    def cell(**over):
        return table_cells(strategy_page(data_of(row(closed=40, wins=40, losses=0, flats=0,
                                                     **over))))[0][4]
    assert cell(pf=None, pf_state="no_losses") == "損失なし"
    assert cell(pf=1.5, pf_state="computed") == "1.50"
    for pf, state in ((None, "computed"), (None, "unknown"), (None, None), (None, "x"),
                      (1.5, "unknown"), (1.5, "no_losses"), (float("nan"), "computed"),
                      (None, True)):
        assert cell(pf=pf, pf_state=state) == "不明"
    missing = row(closed=40, wins=40, losses=0, flats=0, pf=None)
    del missing["pf_state"]
    assert table_cells(strategy_page(data_of(missing)))[0][4] == "不明"


def test_a_tiny_positive_amount_that_rounds_to_zero_has_no_plus_sign():
    assert perf_pages._signed_amount(0.004) == perf_pages._signed_amount(-0.004) == "0.00"
    assert perf_pages._signed_amount(1.5) == "+1.50"
    assert table_cells(strategy_page(data_of(row(total=0.004))))[0][6] == "0.00"


def test_an_unreadable_rate_in_the_reference_band_is_unknown_without_the_reference_mark():
    cells = table_cells(strategy_page(data_of(row(closed=12, rate=None, pf=None))))[0]
    assert cells[3] == cells[4] == "不明"


def test_the_series_history_limit_comes_from_the_daemon_and_a_missing_one_names_no_count():
    cumulative = [{"t": iso(4000 + i), "pnl": 500.0 + i} for i in range(6)]

    def figure(limits, closed=150):
        data = data_of(row(closed=closed, cumulative=cumulative))
        if limits is None:
            del data["series_limits"]
        else:
            data["series_limits"] = limits
        return figure_part(strategy_page(data))
    assert "古い決済 30 件は" in figure({"events": 120, "series": 4})
    assert "古い決済 50 件は" in figure({"events": 100, "series": 4})
    assert "古い決済" not in figure({"events": 200, "series": 4})
    for bad in (None, {}, {"events": "120", "series": 4}, {"events": True, "series": 4},
                {"events": 0, "series": 4}):
        text = figure(bad)
        assert "古い決済の件数は不明です" in text and "古い決済 " not in text


def test_the_drawn_points_follow_the_daemons_history_limit():
    cumulative = [{"t": iso(100 + i), "pnl": float(i)} for i in range(250)]
    data = data_of(row(closed=250, total=249.0, cumulative=cumulative))
    data["series_limits"] = {"events": 200, "series": 4}
    wide = svg.draw_strategies(data, "Asia/Tokyo", perf_pages._signed_amount)
    data["series_limits"] = {"events": 120, "series": 4}
    narrow = svg.draw_strategies(data, "Asia/Tokyo", perf_pages._signed_amount)
    assert len(polylines(wide.svg, "c1")[0]) > len(polylines(narrow.svg, "c1")[0])


def test_a_response_with_unreadable_rows_among_readable_ones_says_how_many_were_dropped():
    html = perf_pages._strategy_table(data_of(row(), "garbage", row("signal:b"), None))
    assert len(table_cells(html)) == 2
    assert "読めない行が 2 件あります" in html
    assert "読めない行" not in perf_pages._strategy_table(data_of(row(), row("signal:b")))


def test_a_row_always_states_both_the_known_and_the_unknown_pnl_counts():
    cells = table_cells(strategy_page(data_of(row(closed=10, known=10))))[0]
    assert cells[1] == "10 (損益既知 10 / 不明 0)"
    for bad in ("x", None, -1, True):
        cells = table_cells(strategy_page(data_of(row(closed=10, pnl_known_count=bad, pnl_unknown_count=bad))))[0]
        assert cells[1] == "10 (損益既知 不明 / 不明 不明)"


@pytest.mark.parametrize("bad", [None, "x", {}, 5, [5, "x"]])
def test_a_strategies_list_that_cannot_be_read_is_unknown_not_an_empty_period(bad):
    data = data_of()
    data["strategies"] = bad
    section = perf_pages._strategy_table(data)
    assert "取引も、保有中の取引もありません" not in section and "決済した取引はありません" not in section
    assert "戦略別の成績を読めません" in section
    del data["strategies"]
    assert "戦略別の成績を読めません" in perf_pages._strategy_table(data)
    assert "取引も、保有中の取引もありません" in perf_pages._strategy_table(data_of())


@pytest.mark.parametrize("bad", [None, "x", -1, True, 1.5])
def test_an_unreadable_closed_count_never_says_nothing_was_closed(bad):
    html = strategy_page(data_of(row(closed=0, known=0, wins=0, losses=0, flats=0, rate=None,
                                     pf=None, avg_r=None, total=None, closed_count=bad)))
    assert "決済した取引はありません" not in html
    assert "決済の件数を読めない行があります" in html


def test_a_legend_end_that_cannot_be_read_or_disagrees_with_the_table_is_unknown():
    broken = [{"t": iso(0), "pnl": 0.0}, {"t": iso(2000), "pnl": 10.0}, {"t": iso(10000), "pnl": "x"}]
    html = strategy_page(data_of(row(total=20.0, cumulative=broken)))
    figure = figure_part(html)
    assert "signal:rsi 終点 不明" in figure and "+10.00" not in "".join(parse(figure).texts)
    short = [{"t": iso(0), "pnl": 0.0}, {"t": iso(2000), "pnl": 10.0}, {"t": iso(10000), "pnl": 10.0}]
    html = strategy_page(data_of(row(total=20.0, cumulative=short)))
    assert "signal:rsi 終点 不明" in figure_part(html)
    ok = strategy_page(data_of(row(total=10.0, cumulative=short)))
    assert "signal:rsi 終点 +10.00" in figure_part(ok)


def test_a_series_that_starts_after_the_period_start_states_the_start_time_and_the_omitted_count():
    folded = [{"t": iso(4000), "pnl": 500.0}, {"t": iso(5000), "pnl": 510.0},
              {"t": iso(10000), "pnl": 510.0}]
    html = strategy_page(data_of(row(closed=125, cumulative=folded)))
    start = svg._full(svg._epoch(iso(4000)), "Asia/Tokyo")
    assert start and f"古い決済 5 件は {start} の始点 1 点にまとめています" in figure_part(html)
    assert "これより前の線は描いていません" in figure_part(html)
    late = [{"t": iso(4000), "pnl": 0.0}, {"t": iso(10000), "pnl": 0.0}]
    html = strategy_page(data_of(row(closed=3, cumulative=late)))
    assert "これより前の線は描いていません" in figure_part(html)
    assert "古い決済" not in figure_part(html)


@pytest.mark.parametrize("origin", ["cron<script>", "cron\n", "cronx", "cron ", "signal:rsi\n"])
def test_an_origin_that_only_starts_like_the_grammar_is_neither_drawn_nor_written(origin):
    assert svg.series_rows([row(origin)]) == ([], 0)
    html = strategy_page(data_of(row(origin)))
    assert "<script" not in html and not re.findall(r"<svg[^>]*>", html)
    assert [c[0] for c in table_cells(html)] == ["不明"]
    assert svg.series_rows([row("cron")])[0][0].origin == "cron"


def test_a_close_that_may_be_unsettled_is_called_unsettled_not_waiting():
    cells = table_cells(strategy_page(data_of(row(holding=0, pending=1))))[0]
    assert cells[8] == "0 (決済未確定 1)" and "待ち" not in "".join(cells)
