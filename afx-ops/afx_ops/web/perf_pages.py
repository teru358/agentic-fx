"""資金推移の画面 (/performance)。今の資金保護の状態・数字・印の表だけを出し、図は出さない。

daemon の応答のうち、ここで名指しした field だけを表示する。上位の型は request の
`_check_reply_shape` が見ており、個々の値の異常はここで「不明」にする (正常には倒さない)。
"""
from __future__ import annotations

import html
import math
import re
from collections.abc import Mapping

from afx_ops import display_time
from afx_ops.display_zone import zone_of
from afx_ops.web import marks, render, tabs

PATH = "/performance"
RANGES = ("7d", "30d", "90d", "all")
DEFAULT_RANGE = "30d"
RANGE_LABELS = {"7d": "7 日", "30d": "30 日", "90d": "90 日", "all": "全期間"}

MAX_MARKS = 20
# 資金推移の画面の合計と、印の表の部品の予算 (UTF-8 byte)
PAGE_BUDGET_BYTES = 60 * 1024
MARKS_BUDGET_BYTES = 7 * 1024

UNKNOWN = "不明"
NONE_TEXT = "–"
BIG = 1e12
COUNT_LIMIT = 10 ** 15
_CURRENCY = re.compile(r"[A-Za-z0-9_.-]{1,16}\Z")

FILL_HOLD_NOTE = {
    "kill_switch": "kill switch が作動中、または解除の途中です",
    "unknown": "状態を読めないため、止まっていないとは言えません",
    "stale": "最新の記録が古いため止まっています",
    "drawdown": "DD が作動の水準に届いています",
    "none": "止める理由は見つかっていません",
}
MARK_KINDS = {"latched": ("✕", "作動"), "released": ("↑", "解除"),
              "rebase": ("▽", "HWM の付け直し"), "cashflow": ("◇", "入出金"),
              "unknown_hwm_drop": ("△", "不明な HWM 下降")}
TIME_SOURCES = {"state": "state", "ledger": "台帳", "event_ts": "event の記録",
                "snapshot": "snapshot"}
MARK_HEADERS = ("種別", "位置", "実時刻", "記録時刻", "帳簿の位置", "操作時刻", "世代", "出所")


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _number(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _amount(value: object) -> str:
    number = _number(value)
    if number is None:
        return UNKNOWN
    return f"{number:.2e}" if abs(number) >= BIG else f"{number:.2f}"


def _percent(value: object) -> str:
    number = _number(value)
    if number is None:
        return UNKNOWN
    return f"{number:.2e}%" if abs(number) >= BIG else f"{number:.1f}%"


def _count(value: object) -> str:
    if type(value) is not int or value < 0:
        return UNKNOWN
    if value < COUNT_LIMIT:
        return str(value)
    try:
        return f"{float(value):.2e}"
    except OverflowError:
        return UNKNOWN


def _seconds(value: object) -> str:
    number = _number(value)
    if number is None or number < 0:
        return UNKNOWN
    return f"{number:.0f} 秒" if number < BIG else f"{number:.2e} 秒"


def _time(value: object, tz: str) -> str:
    converted = display_time.convert_time(value, tz, display_time.PRECISION_SECOND)
    return _esc(converted.text) if converted.ok else UNKNOWN


def _optional_time(value: object, tz: str) -> str:
    return NONE_TEXT if value is None else _time(value, tz)


def _currency(data: Mapping[str, object]) -> str:
    value = data.get("account_currency")
    return value if type(value) is str and _CURRENCY.fullmatch(value) else ""


def _dict(value: object) -> dict:
    return value if type(value) is dict else {}


def range_href(tab: str, range_key: str) -> str:
    """期間の link。既定の tab と既定の期間は query に出さない。"""
    params = []
    if tab != tabs.default_tab(PATH):
        params.append(f"tab={tab}")
    if range_key != DEFAULT_RANGE:
        params.append(f"range={range_key}")
    return PATH + ("?" + "&amp;".join(params) if params else "")


def _range_links(tab: str, range_key: str) -> str:
    links = []
    for key in RANGES:
        mark = ' aria-current="page"' if key == range_key else ""
        links.append(f'<a href="{range_href(tab, key)}"{mark}>{RANGE_LABELS[key]}</a>')
    return "<p>期間: " + " / ".join(links) + "</p>"


# --- 注意 -----------------------------------------------------------------------------

def _warnings(data: Mapping[str, object]) -> str:
    lines = []
    other = data.get("other_sources")
    if type(other) is int and other > 0:
        lines.append(f"paper 以外の記録が {_count(other)} 行あります。この画面は paper だけを描いています")
    quality = _dict(data.get("quality"))
    drops = quality.get("unknown_hwm_drops")
    if type(drops) is int and drops > 0:
        lines.append(f"不明な HWM 下降が {_count(drops)} 件あります。台帳にも入出金にも説明できない下降です")
    if quality.get("partial") is True:
        lines.append("一部の記録を読めていません。集計は読めた分だけです")
    bad = [(label, quality.get(key)) for label, key in
           (("snapshot", "invalid_snapshots"), ("印", "invalid_marks"))]
    shown = [f"{label} {_count(value)}" for label, value in bad if type(value) is int and value > 0]
    if shown:
        lines.append("読めない記録は除外しました (" + "、".join(shown) + ")")
    return "".join(f'<p><span class="st warn">△ 注意</span> {_esc(line)}</p>' for line in lines)


# --- 今の状態 ---------------------------------------------------------------------------

def _word_card(table: Mapping[str, str], raw: object) -> tuple[str, str | None]:
    """表にある語だけ分類し、語そのままの印を返す。表に無い値は不明 (正常に倒さない)。"""
    known = raw if type(raw) is str and raw in table else None
    return marks.render_verdict(marks.classify_word(table, raw)), known


def _current(data: Mapping[str, object], tz: str) -> str:
    current = _dict(data.get("current"))
    unit = _currency(data)
    suffix = f" {unit}" if unit else ""
    kill_switch = current.get("kill_switch")
    generation = _dict(kill_switch).get("generation")
    hold, hold_word = _word_card(marks.FILL_HOLD, current.get("fill_hold"))
    if hold_word is not None:
        hold += " " + _esc(FILL_HOLD_NOTE[hold_word])
    fresh, _ = _word_card(marks.FRESHNESS, current.get("snapshot_freshness"))

    def money(value: object) -> str:
        text = _amount(value)
        return text if text == UNKNOWN else _esc(text + suffix)

    items = [
        ("kill switch", marks.render_verdict(marks.classify_kill_switch(kill_switch))),
        ("世代", _count(generation)),
        ("状態の取得時刻", _time(current.get("state_fetched_at"), tz)),
        ("新規の約定", hold),
        ("最新記録", _time(current.get("snapshot_at"), tz)),
        ("最新記録の鮮度", fresh),
        ("最新記録からの経過", _esc(_seconds(current.get("snapshot_age_sec")))),
        ("DD", _esc(_percent(current.get("dd_pct")))),
        ("作動の水準", money(current.get("kill_level"))),
        ("余裕", money(current.get("headroom"))),
        ("閾値 (DD)", _esc(_percent(current.get("kill_pct")))),
    ]
    notes = ("<p>余裕が正でも解除されたとは限りません。新規の約定が止まっているかどうかは"
             "「新規の約定」の欄が示します。10 分を超えた最新記録は古い記録として扱います。</p>")
    return "<section><h2>今の状態</h2>" + marks.cards(items) + notes + "</section>"


# --- 数字で読む ------------------------------------------------------------------------

def _stats(data: Mapping[str, object], tz: str) -> str:
    stats = _dict(data.get("stats"))
    if "first_at" in stats and "last_at" in stats and stats["first_at"] is None \
            and stats["last_at"] is None:
        return "<section><h2>数字で読む</h2><p>この期間の記録はありません</p></section>"
    unit = _currency(data)
    rows = []
    for label, name in (("equity", "equity"), ("balance", "balance")):
        cells = [_amount(stats.get(f"{name}_{key}")) for key in ("first", "last", "min", "max")]
        rows.append(f"<tr><td>{label}</td>" + "".join(f"<td>{_esc(c)}</td>" for c in cells)
                    + "</tr>")
    table = ("<table><thead><tr><th>項目</th><th>期間の初め</th><th>期間の終わり</th>"
             "<th>最小</th><th>最大</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")
    span = (f"<p>記録の範囲: {_time(stats.get('first_at'), tz)} 〜 {_time(stats.get('last_at'), tz)}"
            + (f" (単位: {_esc(unit)})" if unit else "") + "</p>")
    drawdown = (f"<p>最大 DD: {_esc(_percent(stats.get('dd_max')))} "
                f"({_time(stats.get('dd_max_at'), tz)})</p>")
    return "<section><h2>数字で読む</h2>" + table + span + drawdown + "</section>"


# --- 印の表 ------------------------------------------------------------------------------

def _kind_cell(kind: object) -> str:
    if type(kind) is str and kind in MARK_KINDS:
        symbol, word = MARK_KINDS[kind]
        return _esc(f"{symbol} {word}")
    return _esc(f"? {UNKNOWN}")


def _generation_cell(value: object) -> str:
    return NONE_TEXT if value is None else _count(value)


def _mark_row(item: Mapping[str, object], tz: str) -> str:
    source = item.get("time_source")
    cells = [_kind_cell(item.get("kind")), _time(item.get("plot_at"), tz),
             _optional_time(item.get("occurred_at"), tz),
             _optional_time(item.get("recorded_at"), tz),
             _optional_time(item.get("source_snapshot_ts"), tz),
             _optional_time(item.get("applied_at"), tz), _generation_cell(item.get("generation")),
             _esc(TIME_SOURCES[source]) if type(source) is str and source in TIME_SOURCES
             else UNKNOWN]
    return "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"


def _marks_section(data: Mapping[str, object], tz: str, *, table: bool = True) -> str:
    raw = data.get("marks")
    items = [item for item in raw if type(item) is dict] if type(raw) is list else []
    shown = items[:MAX_MARKS]
    omitted = len(items) - len(shown)
    daemon_omitted = data.get("marks_omitted")
    if type(daemon_omitted) is int and daemon_omitted > 0:
        omitted += daemon_omitted
    more = f"<p>ほか {_count(omitted)} 件</p>" if omitted > 0 else ""
    heading = "<section><h2>印</h2>"
    if not shown:
        return heading + "<p>印はありません</p>" + more + "</section>"
    omitted_note = f"<p>印の表 ({len(shown)} 件) は表示の上限を超えたため省きました</p>"
    if table:
        body = ("<table><thead><tr>" + "".join(f"<th>{name}</th>" for name in MARK_HEADERS)
                + "</tr></thead><tbody>" + "".join(_mark_row(item, tz) for item in shown)
                + "</tbody></table>")
        # 表の部品の予算は、見出しと省略数の注記を含めた byte 数で見る
        if len((heading + body + more + "</section>").encode("utf-8")) <= MARKS_BUDGET_BYTES:
            return heading + body + more + "</section>"
    return heading + omitted_note + more + "</section>"


# --- 注記 ----------------------------------------------------------------------------------

def _notes(data: Mapping[str, object], tz: str) -> str:
    since = data.get("marks_since")
    if since is None:
        history = "event の記録はありません"
    else:
        history = f"kill switch の印は記録がある分だけです。event の記録は {_time(since, tz)} 以降です"
    items = [
        "kill switch は、equity が作動の水準以下のときに建てる判断が出ると作動します。"
        "線を割っただけでは印は付きません",
        history,
        "期間内にも履歴の欠けがあり得ます",
        "event だけから分かる印は、作動 / 解除を記録した時刻です。通知の時刻を状態が変わった時刻とは"
        "断定しません",
    ]
    return "<section><h2>注記</h2><ul>" + "".join(f"<li>{line}</li>" for line in items) + "</ul></section>"


# --- 画面 --------------------------------------------------------------------------------------

def render_performance(data: Mapping[str, object], tab: str = "equity",
                       range_key: str = DEFAULT_RANGE) -> bytes:
    """資金推移の画面。合計の予算を超えるときは、印の表を説明文に置き換える 1 段だけ行う。"""
    tz = zone_of(data)
    bar = tabs.bar(PATH, tab, carry={"range": range_key} if range_key != DEFAULT_RANGE else None)
    head = "<p>paper の記録です</p>" + _warnings(data) + _range_links(tab, range_key)
    fixed = _current(data, tz) + _stats(data, tz)
    tail = _notes(data, tz)
    marks_part = _marks_section(data, tz)
    document = render._document("Performance", head + fixed + marks_part + tail, tz,
                                current=PATH, tabs=bar)
    if len(document) <= PAGE_BUDGET_BYTES:
        return document
    replaced = _marks_section(data, tz, table=False)
    return render._page("Performance", head + fixed + replaced + tail, tz, current=PATH, tabs=bar)
