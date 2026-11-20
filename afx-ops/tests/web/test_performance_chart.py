"""/performance の資金推移の図: SVG の形・安全・描画の規則・予算・CSS の配色。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import pytest

from afx_ops.web import perf_pages, render, svg
from tests.web.test_performance_page import mark, page, payload
from tests.web.test_render import (PAGES, STYLE_MAX_BYTES, ReadClient, _contrast)
from afx_ops.web.request import Request, handle_request

BASE = datetime(2026, 10, 20, tzinfo=timezone.utc)
TOKYO = "Asia/Tokyo"


def iso(minutes: float) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat()


def point(start, end, low, high, last, *, seg=0, balance=None, at=None):
    return {"start_at": iso(start), "end_at": iso(end), "last_at": iso(end if at is None else at),
            "equity_min": low, "equity_max": high, "equity_last": last,
            "balance_last": last - 10 if balance is None else balance, "seg": seg}


def step(at, hwm, seg=0):
    return {"t": iso(at), "hwm": hwm, "seg": seg}


def data_with(points, steps=(), marks=(), kill_pct=10.0, **over):
    base = payload(points=list(points), hwm_steps=list(steps), marks=list(marks))
    base["current"]["kill_pct"] = kill_pct
    base["from"], base["to"] = iso(0), iso(10000)
    base.update(over)
    return base


def draw(data, marks=None):
    shown = data["marks"] if marks is None else marks
    return svg.draw_equity(data, TOKYO, shown)


SIMPLE = [point(1000, 1100, 9000, 9100, 9050), point(2000, 2100, 9200, 9400, 9300),
          point(3000, 3100, 9500, 9600, 9550)]
STEPS = [step(1000, 9500), step(2000, 9500), step(2500, 9800), step(3100, 9800)]


class Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements: list[tuple[str, dict[str, str]]] = []
        self.texts: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    handle_startendtag = handle_starttag

    def handle_data(self, data):
        self.texts.append(data)


def parse(markup: str) -> Collector:
    collector = Collector()
    collector.feed(markup)
    collector.close()
    return collector


def vertices(attr: str) -> list[tuple[int, int]]:
    return [tuple(int(v) for v in pair.split(",")) for pair in attr.split()]


def polylines(markup: str, css: str):
    return [vertices(a["points"]) for tag, a in parse(markup).elements
            if tag == "polyline" and a.get("class") == css]


ALLOWED_TAGS = {"svg", "polyline", "polygon", "line", "text", "title"}
ALLOWED_ATTRS = {"svg": {"viewbox", "role", "aria-label"},
                 "polyline": {"class", "points"}, "polygon": {"class", "points"},
                 "line": {"class", "x1", "y1", "x2", "y2"},
                 "text": {"class", "x", "y", "text-anchor"}, "title": set()}
ALLOWED_CLASSES = {"ax", "c1", "c2", "c3", "c4", "bd", "ev", "rb", "rl", "cf"}


def full_data():
    marks = [mark(kind=k, plot_at=iso(1500 + 500 * i)) for i, k in enumerate(
        ["latched", "released", "rebase", "cashflow", "unknown_hwm_drop"])]
    return data_with(SIMPLE, STEPS, marks)


# --- 要素・属性の allowlist ---------------------------------------------------------------

def test_only_allowlisted_elements_attributes_and_classes_are_drawn_with_integer_coordinates():
    markup = draw(full_data()).svg
    assert markup.startswith(svg.OPENING) and markup.endswith("</svg>")
    for tag, attrs in parse(markup).elements:
        assert tag in ALLOWED_TAGS, tag
        assert set(attrs) <= ALLOWED_ATTRS[tag], (tag, attrs)
        assert set(attrs.get("class", "").split()) <= ALLOWED_CLASSES
        for name in ("x", "y", "x1", "y1", "x2", "y2"):
            if name in attrs:
                assert re.fullmatch(r"\d+", attrs[name]), (name, attrs)
                assert 0 <= int(attrs[name]) <= (960 if "x" in name else 260)
        if "points" in attrs:
            assert re.fullmatch(r"\d+,\d+( \d+,\d+)*", attrs["points"])
            assert all(0 <= x <= 960 and 0 <= y <= 260 for x, y in vertices(attrs["points"]))
    for forbidden in ("<script", "<foreignObject", "<image", "<use", "<a ", "<style", "style=",
                      " on", "fill=", "stroke=", "#"):
        assert forbidden not in markup, forbidden
    assert markup.count("<svg") == 1


def test_every_class_the_figure_uses_has_a_rule_in_the_embedded_css():
    used = {c for _, a in parse(draw(full_data()).svg).elements for c in a.get("class", "").split()}
    assert used == ALLOWED_CLASSES
    for css in used:
        assert re.search(r"(^|\n)(text)?\." + css + r"\{", render.STYLE), css
    for selector in ("svg{", "svg *{", "svg text{", ".num td:not(:first-child)"):
        assert selector in render.STYLE, selector


# --- 線・帯・水準 ---------------------------------------------------------------------------

def test_equity_is_a_solid_polyline_with_higher_values_higher_on_the_screen():
    (line,) = polylines(draw(full_data()).svg, "c1")
    assert len(line) == 3
    xs, ys = zip(*line)
    assert list(xs) == sorted(xs) and ys[0] > ys[1] > ys[2]


def test_balance_is_its_own_polyline_and_the_hwm_and_kill_level_are_staircases():
    markup = draw(full_data()).svg
    (balance,) = polylines(markup, "c2")
    assert len(balance) == 3
    (hwm,) = polylines(markup, "c3")
    (kill,) = polylines(markup, "c4")
    # 段の変化点は同じ x で縦に動く: 4 段 = 先頭 1 + 3 × 2 点
    assert len(hwm) == 1 + 2 * 3 == len(kill)
    assert hwm[3][0] == hwm[4][0] and hwm[3][1] > hwm[4][1]
    # 作動の水準は HWM の 90% = 常に HWM より下 (y が大きい)
    assert all(k[1] > h[1] for h, k in zip(hwm, kill))


@pytest.mark.parametrize("kill_pct", [None, 0, -1, 100, 150, True, "10", float("nan")])
def test_the_kill_level_is_not_drawn_without_a_usable_threshold(kill_pct):
    markup = draw(data_with(SIMPLE, STEPS, kill_pct=kill_pct)).svg
    assert polylines(markup, "c4") == [] and len(polylines(markup, "c3")) == 1
    assert "作動の水準 (今の閾値)" not in markup


def test_the_band_is_one_polygon_per_run_spanning_the_real_bucket_width():
    parts = [point(1000, 1500, 9000, 9100, 9050), point(2000, 2500, 9200, 9400, 9300)]
    markup = draw(data_with(parts)).svg
    polygons = [vertices(a["points"]) for t, a in parse(markup).elements
                if t == "polygon" and a["class"] == "bd" and len(a["points"].split()) == 8]
    assert len(polygons) == 1
    (a, b, c, d, e, f, g, h) = polygons[0]
    # 上辺は時刻順 (各束の始め・終わり)、下辺は逆順で閉じる
    assert a[0] < b[0] < c[0] < d[0] and e == (d[0], e[1]) and h == (a[0], h[1])
    assert a[1] == b[1] and c[1] == d[1] and e[1] == f[1] and g[1] == h[1]
    assert c[1] < a[1] and h[1] > a[1] and f[0] == c[0] and g[0] == b[0]


def test_a_recording_gap_cuts_the_lines_and_the_bands_and_joins_nothing():
    parts = [point(1000, 1100, 9000, 9100, 9050, seg=0), point(1200, 1300, 9100, 9200, 9150, seg=0),
             point(5000, 5100, 9300, 9400, 9350, seg=1), point(5200, 5300, 9400, 9500, 9450, seg=1)]
    steps = [step(1000, 9500, 0), step(1300, 9500, 0), step(5000, 9600, 1), step(5300, 9600, 1)]
    markup = draw(data_with(parts, steps)).svg
    for css in ("c1", "c2", "c3", "c4"):
        lines = polylines(markup, css)
        assert len(lines) == 2, css
        assert max(x for x, _ in lines[0]) < min(x for x, _ in lines[1]), css
    bands = [a for t, a in parse(markup).elements if t == "polygon" and len(a["points"].split()) == 8]
    assert len(bands) == 2


def test_an_unreadable_row_between_readable_ones_cuts_every_series_and_joins_nothing():
    good = [point(1000, 1100, 9000, 9100, 9050), point(3000, 3100, 9300, 9400, 9350)]
    steps = [step(1000, 9500), step(3000, 9600)]
    junk_row = {**point(2000, 2100, 9100, 9200, 9150), "equity_last": "bad"}
    junk_step = {"t": iso(2000), "hwm": "bad", "seg": 0}
    for css in ("c1", "c2", "c3", "c4"):
        markup = draw(data_with([good[0], junk_row, good[1]], [steps[0], junk_step, steps[1]])).svg
        lines = polylines(markup, css)
        assert len(lines) == 2, css
        assert max(x for x, _ in lines[0]) < min(x for x, _ in lines[1]), css
    markup = draw(data_with([good[0], junk_row, good[1]])).svg
    bands = [a for t, a in parse(markup).elements if t == "polygon" and a["class"] == "bd"
             and "216" not in a["points"]]
    assert len(bands) == 2
    # 数値でない行も境界になる
    assert len(polylines(draw(data_with([good[0], None, good[1]])).svg, "c1")) == 2


def test_an_unreadable_balance_alone_cuts_the_balance_line_and_not_the_equity_line():
    rows = [point(1000, 1100, 9000, 9100, 9050), {**point(2000, 2100, 9100, 9200, 9150), "balance_last": "x"},
            point(3000, 3100, 9300, 9400, 9350)]
    markup = draw(data_with(rows)).svg
    assert len(polylines(markup, "c1")) == 1 and len(polylines(markup, "c2")) == 2


def test_a_non_positive_hwm_ends_the_kill_level_and_a_later_positive_one_starts_a_new_line():
    steps = [step(1000, 100), step(2000, 0), step(3000, 80), step(3100, 80)]
    markup = draw(data_with(SIMPLE, steps)).svg
    kill = polylines(markup, "c4")
    assert len(kill) == 2 and max(x for x, _ in kill[0]) < min(x for x, _ in kill[1])
    assert len(polylines(markup, "c3")) == 1


def test_a_run_of_one_point_is_drawn_as_a_short_visible_line_not_dropped():
    parts = [point(1000, 1100, 9000, 9100, 9050, seg=0), point(5000, 5100, 9300, 9400, 9350, seg=1)]
    lines = polylines(draw(data_with(parts)).svg, "c1")
    assert len(lines) == 2 and all(len(line) == 2 and line[0][1] == line[1][1] for line in lines)


def test_the_axis_ticks_the_dates_and_the_two_ends_are_in_the_display_zone():
    chart = draw(full_data())
    assert chart.span[0] == "2026-10-20 09:00:00 +09:00"
    labels = re.findall(r'<text x="\d+" y="\d+" text-anchor="(?:start|middle|end)">'
                        r'(\d{2}-\d{2})</text>', chart.svg)
    assert 4 <= len(labels) <= 8 and labels[0] == "10-20"
    long = draw(data_with(SIMPLE, STEPS, to=iso(60 * 24 * 400))).svg
    assert re.search(r"<text[^>]*>2027-\d{2}-\d{2}</text>", long)
    short = draw(data_with([point(100, 110, 9000, 9100, 9050)], to=iso(300))).svg
    assert re.search(r"<text[^>]*>10-20 \d{2}:\d{2}</text>", short)


def test_a_flat_series_still_has_a_scale_and_ticks():
    parts = [point(1000, 1100, 9000, 9000, 9000), point(2000, 2100, 9000, 9000, 9000)]
    markup = draw(data_with(parts)).svg
    assert markup and re.search(r"<text[^>]*text-anchor=\"end\">[0-9.-]+</text>", markup)


# --- 印 -------------------------------------------------------------------------------------

def test_each_mark_kind_has_its_symbol_color_class_and_a_title_with_the_display_time():
    markup = draw(full_data()).svg
    expected = {"✕": "ev", "↑": "rl", "▽": "rb", "◇": "cf", "△": "rb"}
    titled = re.findall(r'<text class="(\w+)" x="\d+" y="\d+" text-anchor="middle">'
                        r'<title>([^<]*)</title>(.)</text>', markup)
    assert [(c, s) for c, _, s in titled] == [(expected[s], s) for _, _, s in titled]
    assert sorted(s for _, _, s in titled) == sorted(expected)
    for _, title, symbol in titled:
        assert title.startswith(symbol) and re.search(r"2026-10-2\d \d\d:\d\d:\d\d \+09:00", title)
    assert 'x1="' in markup and re.search(r'<line class="ev" x1="(\d+)" y1="12" x2="\1" y2="186"/>', markup)


def test_the_legend_lists_every_line_and_every_symbol_with_its_color_class():
    markup = draw(full_data()).svg
    for word in ("equity", "balance", "HWM", "作動の水準", "束内の最小〜最大"):
        assert word in markup, word
    for symbol, word, css in svg.MARK_STYLE.values():
        assert f'<text class="{css}" x=' in markup and f">{symbol} {word}</text>" in markup


def test_a_mark_with_an_unknown_kind_or_an_unreadable_time_is_left_out_of_the_figure():
    marks = [mark(kind="latched", plot_at=iso(1500)), mark(kind="??", plot_at=iso(1600)),
             mark(kind="released", plot_at="bad"), mark(kind="rebase", plot_at=None),
             mark(kind=5, plot_at=iso(1700)), "x", None]
    markup = draw(data_with(SIMPLE, STEPS, marks)).svg
    assert len(re.findall(r"<title>", markup)) == 1


def test_an_equal_position_mark_is_staggered_so_the_symbols_do_not_overlap():
    marks = [mark(kind="rebase", plot_at=iso(1500)), mark(kind="cashflow", plot_at=iso(1500)),
             mark(kind="unknown_hwm_drop", plot_at=iso(1500))]
    ys = re.findall(r'<text class="\w+" x="\d+" y="(\d+)" text-anchor="middle">',
                    draw(data_with(SIMPLE, STEPS, marks)).svg)
    assert len(set(ys)) == 3


def test_four_or_more_marks_at_one_position_are_each_identifiable():
    kinds = ["latched", "released", "rebase", "cashflow", "unknown_hwm_drop"]
    marks = [mark(kind=k, plot_at=iso(1500)) for k in kinds]
    markup = draw(data_with(SIMPLE, STEPS, marks)).svg
    ys = re.findall(r'<text class="\w+" x="\d+" y="(\d+)" text-anchor="middle">', markup)
    assert len(ys) == 5 and len(set(ys)) == 5
    # 同じ種別は 1 つの記号に件数をまとめる
    many = [mark(kind="latched", plot_at=iso(1500)) for _ in range(4)] + [mark(kind="cashflow", plot_at=iso(1500))]
    markup = draw(data_with(SIMPLE, STEPS, many)).svg
    assert markup.count("<title>") == 2 and "✕×4" in markup and "ほか 3 件" in markup
    assert ">◇</text>" in markup


def test_only_the_marks_the_table_shows_are_drawn_in_the_same_order_and_number():
    marks = [mark(kind="latched", plot_at=iso(1000 + 100 * i)) for i in range(30)]
    html = page(data_with(SIMPLE, STEPS, marks))
    assert len(re.findall(r"<title>[✕↑▽◇△] ", html)) == perf_pages.MAX_MARKS
    assert len(re.findall(r"<tr><td>", html.split("<h2>印</h2>")[1])) == perf_pages.MAX_MARKS


# --- 敵対的な値・escape -------------------------------------------------------------------------

HOSTILE_WORD = '<b onload="x">&\'"'


def test_text_inside_the_figure_is_escaped_so_it_cannot_become_an_element_or_attribute(monkeypatch):
    monkeypatch.setitem(svg.MARK_STYLE, "latched", ("✕", HOSTILE_WORD, "ev"))
    monkeypatch.setattr(svg, "LINE_LEGEND", (("c1", HOSTILE_WORD), ("c4", HOSTILE_WORD)))
    monkeypatch.setattr(svg, "BAND_LEGEND", HOSTILE_WORD)
    markup = draw(full_data()).svg
    assert "<b" not in markup and 'onload="x"' not in markup
    assert "&lt;b onload=&quot;x&quot;&gt;&amp;&#x27;&quot;" in markup
    assert {t for t, _ in parse(markup).elements} <= ALLOWED_TAGS


@pytest.mark.parametrize("bad", ["<script>", None, True, float("nan"), float("inf"), -1e308, 1e308,
                                 10 ** 400, [], {}, "9000"])
def test_an_unreadable_or_huge_value_is_skipped_never_drawn_as_a_line(bad):
    good = point(1000, 1100, 9000, 9100, 9050)
    for key in ("equity_min", "equity_max", "equity_last", "start_at", "end_at", "last_at", "seg"):
        broken = {**good, key: bad}
        chart = draw(data_with([broken]))
        assert chart.svg == "" and chart.notes == (svg.NOTE_NO_POINTS,), (key, bad)
    chart = draw(data_with([good, {**good, "balance_last": bad, "seg": 0}]))
    assert len(polylines(chart.svg, "c1")[0]) == 2
    assert len(polylines(chart.svg, "c2")[0]) == 2  # 1 点だけの線は短い横線になる
    chart = draw(data_with([good], [{"t": iso(1000), "hwm": bad, "seg": 0}]))
    assert polylines(chart.svg, "c3") == [] and polylines(chart.svg, "c4") == []


@pytest.mark.parametrize("bad", [None, "x", 5, [], {"x": 1}, [None], [[]]])
def test_a_non_list_or_junk_points_and_steps_never_raise(bad):
    assert draw(data_with(SIMPLE) | {"points": bad}).svg == ""
    assert draw(data_with(SIMPLE) | {"hwm_steps": bad}).svg.startswith(svg.OPENING)


def test_a_negative_nonfinite_or_zero_hwm_has_no_kill_level_and_a_bad_mark_list_never_raises():
    steps = [step(1000, 0), step(2000, -5), {"t": iso(3000), "hwm": float("nan"), "seg": 0}]
    markup = draw(data_with(SIMPLE, steps)).svg
    assert polylines(markup, "c4") == []
    assert draw(data_with(SIMPLE), marks=[None, 3, "x", {"kind": []}]).svg.startswith(svg.OPENING)


# --- 予算 -------------------------------------------------------------------------------------------

def worst_data():
    """全上限 (280 点・320 段・32 区間・20 印) で、座標が 3 桁になる入力。"""
    parts, steps = [], []
    for i in range(280):
        seg = i * 32 // 280
        parts.append(point(2000 + i * 100, 2000 + i * 100 + 60, 9000 + i % 7, 9010 + i % 5, 9005,
                           seg=seg))
    parts[0]["equity_max"] = 19000.0
    for i in range(320):
        steps.append(step(2000 + i * 90, 9005 + i % 3, seg=i * 32 // 320))
    marks = [mark(kind=k, plot_at=iso(3000 + 1300 * j)) for j, k in enumerate(
        ["latched", "released", "rebase", "cashflow", "unknown_hwm_drop"] * 4)]
    return data_with(parts, steps, marks, **{"to": iso(2000 + 320 * 100)})


# 頂点 1 つの最大 (3 桁,3 桁 + 区切り) と、1 区間の要素の包み (polygon・c1〜c4 の 5 本) の最大
VERTEX_MAX = len("960,260 ")
RUN_WRAPPERS_MAX = len('<polygon class="bd" points=""/>') + 4 * len('<polyline class="c1" points=""/>')


def static_worst_lines() -> int:
    vertices_max = 6 * 280 + 4 * (320 + 32) + 2 * 2 * 32  # 1 点だけの区間の補いを含む
    sample = svg._Scale(1.7e9, 1.7e9 + 86400 * 400, -1.2345e11, 1.2e11)
    legend = svg._legend(["c1", "c2", "c3", "c4"], list(svg.MARK_STYLE))
    fixed = len((svg._frame(sample, TOKYO) + legend).encode("utf-8"))
    return VERTEX_MAX * vertices_max + RUN_WRAPPERS_MAX * 32 + fixed


def test_the_static_worst_case_of_the_lines_and_the_marks_fits_their_part_budgets():
    assert static_worst_lines() <= svg.LINES_BUDGET_BYTES
    # 印 1 つ: 固定属性 + escape 後 160 byte の title (全印が最大長・最大の桁)
    one = len(svg._marks_part(svg._Scale(0, 1, 0, 1), [svg._Marks("unknown_hwm_drop", 1, "x" * 160)])
              .encode("utf-8"))
    assert one * 20 <= svg.MARKS_BUDGET_BYTES
    assert svg.LINES_BUDGET_BYTES == 32 * 1024 and svg.MARKS_BUDGET_BYTES == 6 * 1024


def test_the_real_figure_for_the_largest_input_is_within_the_static_worst_and_the_budgets():
    chart = draw(worst_data())
    assert chart.notes == () and chart.svg
    marks_at = chart.svg.index('<line class="ev"')
    lines, marks = chart.svg[:marks_at], chart.svg[marks_at:]
    assert len(lines.encode("utf-8")) <= static_worst_lines()
    assert len(marks.encode("utf-8")) <= svg.MARKS_BUDGET_BYTES
    assert len(re.findall(r"<polyline", chart.svg)) <= 32 * 4
    assert chart.svg.count("<title>") == 20
    assert max(len(m.encode("utf-8")) for m in re.findall(r"<title>([^<]*)</title>", chart.svg)) \
        <= 160


def test_the_largest_input_as_a_page_fits_the_total_budget_and_keeps_every_fixed_part():
    html = page(worst_data())
    assert len(html.encode("utf-8")) <= perf_pages.PAGE_BUDGET_BYTES
    assert "Output clipped" not in html and html.count("<svg") == 1
    for word in ("今の状態", "資金推移の図", "数字で読む", "判定の根拠ではありません"):
        assert word in html
    section = html.split("<h2>資金推移の図</h2>", 1)[1].split("</section>", 1)[0]
    assert len(section.encode("utf-8")) <= svg.LINES_BUDGET_BYTES + svg.MARKS_BUDGET_BYTES + 2048


def test_a_lines_part_over_its_budget_is_replaced_whole_by_a_note_and_the_rest_stays(monkeypatch):
    monkeypatch.setattr(svg, "LINES_BUDGET_BYTES", 100)
    html = page(full_data())
    assert "<svg" not in html and svg.NOTE_LINES_OVER in html
    for word in ("今の状態", "数字で読む", "判定の根拠ではありません", "paper の記録です", "<h2>印</h2>"):
        assert word in html


def test_a_marks_part_over_its_budget_is_replaced_whole_and_the_lines_stay(monkeypatch):
    monkeypatch.setattr(svg, "MARKS_BUDGET_BYTES", 100)
    html = page(full_data())
    assert html.count("<svg") == 1 and not re.search(r"<title>[✕↑▽◇△] ", html) \
        and svg.NOTE_MARKS_OVER in html
    assert "<polyline" in html and "数字で読む" in html


def test_a_page_over_the_total_budget_drops_the_marks_table_first_then_the_figure(monkeypatch):
    data = full_data()
    full = len(page(data).encode("utf-8"))
    no_table = len(page(data | {"marks": []}).encode("utf-8"))
    assert no_table < full
    monkeypatch.setattr(perf_pages, "PAGE_BUDGET_BYTES", full - 1)
    html = page(data)
    assert html.count("<svg") == 1 and "<table" in html.split("<h2>数字で読む</h2>")[1] and \
        "表示の上限を超えたため省きました" in html.split("<h2>印</h2>")[1]
    stage_two = len(html.encode("utf-8"))
    monkeypatch.setattr(perf_pages, "PAGE_BUDGET_BYTES", stage_two - 1)
    html = page(data)
    assert "<svg" not in html and svg.NOTE_LINES_OVER in html and "Output clipped" not in html
    assert len(html.encode("utf-8")) <= stage_two - 1 and "判定の根拠ではありません" in html


# --- 画面の枠 ---------------------------------------------------------------------------------------

def test_the_figure_section_sits_between_the_current_state_and_the_numbers_with_its_sentences():
    html = page(full_data())
    assert html.index("今の状態") < html.index("資金推移の図") < html.index("数字で読む")
    section = html.split("<h2>資金推移の図</h2>", 1)[1].split("</section>", 1)[0]
    assert "線は時間で束ねた表示で、判定の根拠ではありません" in section
    assert "kill switch の印は記録がある分だけです" in section
    assert "横軸: 表示 tz の時刻 (2026-10-20 09:00:00 +09:00 〜 " in section and "(JPY)" in section
    assert "束内の equity と作動の水準の厳密な交差は再現していません" in html


def test_omitted_points_and_steps_are_counted_beside_the_figure():
    data = full_data()
    data["omissions"] = {"equity_points": 3, "hwm_steps": 4, "strategy_rows": 0, "strategy_series": 0}
    section = page(data).split("<h2>資金推移の図</h2>", 1)[1].split("</section>", 1)[0]
    assert "代表点 3" in section and "階段の段 4" in section
    assert "省いたもの" not in page(full_data())


def test_a_period_with_no_record_says_so_instead_of_a_figure_and_keeps_the_notes():
    data = payload(stats={"first_at": None, "last_at": None})
    html = page(data)
    assert "<svg" not in html
    section = html.split("<h2>資金推移の図</h2>", 1)[1].split("</section>", 1)[0]
    assert "この期間の記録はありません" in section and "判定の根拠ではありません" in section
    assert "今の状態" in html and "注記" in html


def test_points_that_cannot_be_drawn_with_numbers_present_say_so_without_a_figure():
    section = page(payload(points=[{"x": 1}])).split("<h2>資金推移の図</h2>", 1)[1] \
        .split("</section>", 1)[0]
    assert svg.NOTE_NO_POINTS in section and "<svg" not in section


def test_the_only_svg_in_the_whole_web_is_the_one_this_route_draws_with_a_fixed_opening():
    for path in PAGES:
        body = handle_request(Request("GET", path, {}), ReadClient()).body.decode()
        assert "<svg" not in body, path
    html = page(full_data())
    assert re.findall(r"<svg[^>]*>", html) == [svg.OPENING]
    assert "<script" not in html and " style=" not in html
    assert re.findall(r"<style>(.*?)</style>", html, re.S) == [render.STYLE]


def test_hostile_text_around_a_real_figure_never_becomes_an_element():
    data = full_data()
    data["account_currency"] = '<svg onload=alert(1)>'
    data["display_timezone"] = "<script>x</script>"
    data["marks"][0]["kind"] = "<img src=x onerror=y>"
    html = page(data)
    assert re.findall(r"<svg[^>]*>", html) == [svg.OPENING]
    assert "<script" not in html and "<img" not in html and "onerror" not in html.replace("&lt;", "")
    assert not re.findall(r"<[a-z]+[^>]* (?:on[a-z]+|style)=", html)


def test_the_stats_table_is_right_aligned_by_a_class_and_the_marks_table_is_not():
    html = page(full_data())
    assert re.findall(r"<table[^>]*>", html.split("<h2>数字で読む</h2>")[1].split("</section>")[0]) \
        == ['<table class="num">']
    assert "<table>" in html.split("<h2>印</h2>")[1]


# --- CSS: token・配色 ---------------------------------------------------------------------------------

SERIES = ["--c1", "--c2", "--c3", "--c4"]


def _full_tokens():
    css = render.STYLE
    light = re.match(r"\s*:root \{([^}]*)\}", css).group(1)
    dark = re.search(r"@media \(prefers-color-scheme: dark\) \{ :root \{([^}]*)\}", css).group(1)
    return [dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{3,6})", block)) for block in (light, dark)]


def test_the_four_series_tokens_exist_in_both_themes_and_are_written_only_there():
    for tokens in _full_tokens():
        assert all(name in tokens for name in SERIES)
    light, dark = _full_tokens()
    assert [light[n] for n in SERIES] != [dark[n] for n in SERIES]
    assert not re.search(r"(?<!:root \{)#[0-9a-fA-F]{6}\b", re.sub(r":root \{[^}]*\}", "", render.STYLE))


def test_lines_marks_and_axes_have_contrast_3_and_text_4_5_on_the_page_background_in_both_themes():
    for name, tokens in zip(("light", "dark"), _full_tokens()):
        for background in ("--bg", "--soft"):
            for line in SERIES + ["--mut", "--bad", "--warn"]:
                assert _contrast(tokens[line], tokens[background]) >= 3.0, (name, line, background)
            for text in ("--fg", "--mut", "--bad", "--warn", "--c2"):
                assert _contrast(tokens[text], tokens[background]) >= 4.5, (name, text, background)


def test_the_series_are_told_apart_by_dash_pattern_as_well_as_color():
    patterns = {css: re.search(r"\n\." + css + r"\{[^}]*stroke-dasharray:([^;}]*)", render.STYLE)
                for css in ("c2", "c3", "c4")}
    values = {css: m.group(1) for css, m in patterns.items() if m}
    assert len(values) == 3 and len(set(values.values())) == 3
    assert "stroke-dasharray" not in re.search(r"\n\.c1\{[^}]*\}", render.STYLE).group(0)


def test_the_style_with_the_figure_rules_stays_within_the_byte_limit():
    assert len(render.STYLE.encode("utf-8")) <= STYLE_MAX_BYTES
    assert "vector-effect" in render.STYLE and "<" not in render.STYLE


@pytest.mark.parametrize("bad", ["0001-01-01T00:00:00+09:00", "9999-12-31T23:59:59-05:00",
                                 "1970-06-01T00:00:00+00:00", "9999-01-01T00:00:00+00:00"])
def test_a_time_outside_the_readable_years_is_skipped_everywhere_and_never_raises(bad):
    assert svg._epoch(bad) is None
    data = data_with(SIMPLE, STEPS, [mark(kind="latched", plot_at=bad)], **{"from": bad, "to": bad})
    data["points"] = data["points"] + [{**point(4000, 4100, 9500, 9600, 9550), "last_at": bad}]
    data["hwm_steps"] = data["hwm_steps"] + [{"t": bad, "hwm": 9800, "seg": 0}]
    chart = draw(data)
    assert chart.svg and chart.span is not None and all(chart.span)
    assert [len(line) for line in polylines(chart.svg, "c1")] == [3]
    assert page(data)


def test_the_readable_year_range_edges_are_accepted():
    assert svg._epoch("1971-01-01T00:00:00+00:00") is not None
    assert svg._epoch("9998-12-30T00:00:00+00:00") is not None
    assert svg._epoch("9998-12-31T12:00:00+00:00") is not None
    assert svg._epoch("9998-12-31T23:59:59+00:00") is not None
    assert svg._epoch("9999-01-01T00:00:00+00:00") is None
    assert svg._epoch("9999-01-01T08:59:59+09:00") is not None
    for tz_label in ("full", "day", "time"):
        assert svg._date_label(svg.EPOCH_MAX_EXCLUSIVE - 1, TOKYO, tz_label) is not None
    assert svg._date_label(1e18, TOKYO, "full") == "" and svg._full(1e18, TOKYO) == ""


def test_a_point_on_the_last_day_of_the_last_readable_year_is_drawn():
    late = [{**point(0, 1, 9000, 9100, 9050), "start_at": "9998-12-31T12:00:00+00:00",
             "end_at": "9998-12-31T13:00:00+00:00", "last_at": "9998-12-31T13:00:00+00:00"}]
    chart = draw(data_with(late, **{"from": "9998-12-31T12:00:00+00:00",
                                    "to": "9998-12-31T13:00:00+00:00"}))
    assert chart.svg and len(polylines(chart.svg, "c1")[0]) == 2


@pytest.mark.parametrize("low,high", [(0.0, 5e-324), (0.0, 1e-323), (5e-324, 5e-324), (0.0, 0.0),
                                      (1e10, 1e10 + 1e-5), (0.0, 1e-300)])
def test_a_value_range_too_narrow_to_tick_still_draws_with_a_widened_axis(low, high):
    rows = [point(1000, 1100, low, high, low), point(2000, 2100, low, high, high)]
    chart = draw(data_with(rows, []))
    assert chart.svg and polylines(chart.svg, "c1")
    ticks = re.findall(r'<text x="\d+" y="\d+" text-anchor="end">([0-9.-]+)</text>', chart.svg)
    assert ticks and all(t == t for t in ticks)


def test_the_tick_step_is_always_positive_and_finite():
    for low, high in [(0.0, 5e-324), (0.0, 0.0), (1.0, 1.0), (0.0, 1e-300), (-1e12, 1e12)]:
        ticks, step = svg._nice_ticks(low, high)
        assert step > 0 and step == step and step != float("inf") and ticks


def test_marks_at_neighbouring_positions_do_not_overlap_their_symbols():
    # 作動と解除が隣り合う x (1 目盛りだけ違う) に落ちても、同じ高さに文字が重ならない
    span = 10000 / 880
    marks = [mark(kind="latched", plot_at=iso(1500)), mark(kind="released", plot_at=iso(1500 + span)),
             mark(kind="cashflow", plot_at=iso(1500 + 2 * span))]
    texts = re.findall(r'<text class="\w+" x="(\d+)" y="(\d+)" text-anchor="middle">',
                       draw(data_with(SIMPLE, STEPS, marks)).svg)
    assert len(texts) == 3
    for i, (xa, ya) in enumerate(texts):
        for xb, yb in texts[i + 1:]:
            assert ya != yb or abs(int(xa) - int(xb)) >= svg.MARK_WIDTH, texts


def test_far_apart_marks_share_a_row_and_a_neighbouring_counted_group_keeps_clear():
    far = [mark(kind="latched", plot_at=iso(1000)), mark(kind="released", plot_at=iso(5000))]
    ys = re.findall(r'<text class="\w+" x="\d+" y="(\d+)" text-anchor="middle">',
                    draw(data_with(SIMPLE, STEPS, far)).svg)
    assert len(set(ys)) == 1
    many = [mark(kind="latched", plot_at=iso(1500)) for _ in range(4)] \
        + [mark(kind="released", plot_at=iso(1500 + 10000 / 880 * 3))]
    texts = re.findall(r'<text class="\w+" x="(\d+)" y="(\d+)" text-anchor="middle">'
                       r'<title>[^<]*</title>([^<]*)</text>', draw(data_with(SIMPLE, STEPS, many)).svg)
    (xa, ya, la), (xb, yb, lb) = texts
    assert ya != yb or abs(int(xa) - int(xb)) * 2 >= svg.MARK_WIDTH * (len(la) + len(lb))


def test_the_crowded_marks_stay_inside_the_figure_and_the_part_budget():
    marks = [mark(kind=k, plot_at=iso(3000 + 5 * j)) for j, k in enumerate(
        ["latched", "released", "rebase", "cashflow", "unknown_hwm_drop"] * 4)]
    chart = draw(data_with(SIMPLE, STEPS, marks))
    ys = [int(y) for y in re.findall(r'<text class="\w+" x="\d+" y="(\d+)" text-anchor="middle">', chart.svg)]
    assert len(ys) == 20 and max(ys) < svg.Y1


def test_marks_dropped_for_the_budget_do_not_stretch_the_time_axis(monkeypatch):
    far = [mark(kind="latched", plot_at=iso(500000))]
    data = data_with(SIMPLE, STEPS, far, **{"from": iso(1000), "to": iso(3100)})
    with_marks = draw(data)
    assert with_marks.svg and with_marks.span[1] != draw(data_with(SIMPLE, STEPS, **{"from": iso(1000), "to": iso(3100)})).span[1]
    monkeypatch.setattr(svg, "MARKS_BUDGET_BYTES", 100)
    dropped = draw(data)
    plain = draw(data_with(SIMPLE, STEPS, [], **{"from": iso(1000), "to": iso(3100)}))
    assert dropped.notes == (svg.NOTE_MARKS_OVER,) and dropped.span == plain.span
    assert polylines(dropped.svg, "c1") == polylines(plain.svg, "c1")


def test_the_legend_lists_only_the_series_and_marks_that_were_drawn(monkeypatch):
    rows = [{**p, "balance_last": None} for p in SIMPLE]
    bare = draw(data_with(rows, [], [mark(kind="latched", plot_at=iso(1500))])).svg
    bare = bare[len(svg.OPENING):]
    for absent in ("balance", "HWM", "作動の水準", "↑ 解除", "▽", "◇ 入出金", "△"):
        assert f">{absent}" not in bare and absent not in bare.replace("HWM の付け直し", ""), absent
    for present in (">equity<", "束内の最小〜最大", ">✕ 作動<"):
        assert present in bare, present
    monkeypatch.setattr(svg, "MARKS_BUDGET_BYTES", 100)
    dropped = draw(full_data()).svg
    assert ">✕ 作動<" not in dropped and ">equity<" in dropped and ">HWM<" in dropped


def test_the_figure_scrolls_sideways_in_its_own_box_and_keeps_a_minimum_width_for_the_text():
    html = page(full_data())
    section = html.split("<h2>資金推移の図</h2>", 1)[1]
    assert re.search(r'<div class="scroll"><svg ', section)
    assert re.search(r"\nsvg\{[^}]*min-width:55rem", render.STYLE)
    assert 11 / 960 * 55 * 16 >= 10
    assert '<div class="scroll"><svg' not in page(payload(points=[]))
