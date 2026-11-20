"""資金推移の画面 (/performance)。今の資金保護の状態・資金推移の図・数字・印の表を出す。

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
from afx_ops.web import marks, render, svg, tabs

PATH = "/performance"
RANGES = ("7d", "30d", "90d", "all")
DEFAULT_RANGE = "30d"
RANGE_LABELS = {"7d": "7 日", "30d": "30 日", "90d": "90 日", "all": "全期間"}

MAX_MARKS = 20
# 資金推移の画面の合計と、印の表の部品の予算 (UTF-8 byte)
PAGE_BUDGET_BYTES = 60 * 1024
MARKS_BUDGET_BYTES = 7 * 1024
# 戦略別の画面の合計と、戦略の表の部品の予算 (UTF-8 byte)
STRATEGY_PAGE_BUDGET_BYTES = 48 * 1024
STRATEGY_TABLE_BUDGET_BYTES = 22 * 1024

UNKNOWN = "不明"
EQUITY_EMPTY = ("この期間の記録はありません。期間を広げるか、daemon が動いているかを "
                "Status 画面で確かめてください")
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
TIME_SOURCES = {"state": "今の状態の記録", "ledger": "付け直しの台帳", "event_ts": "通知の記録",
                "snapshot": "口座の記録"}
MARK_HEADERS = ("種別", "位置", "実時刻", "記録時刻", "台帳の位置", "操作時刻", "世代", "出所")


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


def _unsigned_zero(text: str) -> str:
    """書式で 0 になった値 (-0.00 など) から符号を落とす。"""
    return text[1:] if text.startswith("-") and not any(c in "123456789" for c in text) else text


def _amount(value: object) -> str:
    number = _number(value)
    if number is None:
        return UNKNOWN
    return f"{number:.2e}" if abs(number) >= BIG else _unsigned_zero(f"{number:.2f}")


def _percent(value: object) -> str:
    number = _number(value)
    if number is None:
        return UNKNOWN
    return f"{number:.2e}%" if abs(number) >= BIG else _unsigned_zero(f"{number:.1f}") + "%"


def _count(value: object) -> str:
    if type(value) is not int or value < 0:
        return UNKNOWN
    if value < COUNT_LIMIT:
        return str(value)
    try:
        return f"{float(value):.2e}"
    except OverflowError:
        return UNKNOWN


def _elapsed(value: object) -> str:
    """経過の長さ。60 秒以上は大きい単位 (日・時間・分) を先に出し、秒は括弧に残す。"""
    number = _number(value)
    if number is None or number < 0:
        return UNKNOWN
    if number >= BIG:
        return f"{number:.2e} 秒"
    seconds = round(number)
    if seconds < 60:
        return f"{seconds} 秒"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    parts = [f"{n} {unit}" for n, unit in ((days, "日"), (hours, "時間"), (rest // 60, "分")) if n]
    return " ".join(parts) + f" ({seconds} 秒)"


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
    item = next(i for i in tabs.TABS[PATH] if i.name == tab)
    carry = {"range": range_key} if range_key != DEFAULT_RANGE else None
    return tabs._href(PATH, item, carry or {}, omit_default=True)


def _range_links(tab: str, range_key: str) -> str:
    links = []
    for key in RANGES:
        label = RANGE_LABELS[key]
        if key == range_key:
            links.append(f'<a href="{range_href(tab, key)}" aria-current="page">'
                         f"<strong>{label}</strong></a>")
        else:
            links.append(f'<a href="{range_href(tab, key)}">{label}</a>')
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
           (("口座の記録", "invalid_snapshots"), ("印", "invalid_marks"))]
    shown = [f"{label} {_count(value)} 件" for label, value in bad if type(value) is int and value > 0]
    if shown:
        lines.append("読めない記録は除外しました (" + "、".join(shown) + ")")
    return "".join(f'<p><span class="st warn">△ 注意</span> {_esc(line)}</p>' for line in lines)


# --- 今の状態 ---------------------------------------------------------------------------

def _word_card(table: Mapping[str, str], raw: object,
               shown: Mapping[str, str]) -> tuple[str, str | None]:
    """表にある語だけ分類し、表示の語に替えた印を返す。表に無い値は不明 (正常に倒さない)。"""
    known = raw if type(raw) is str and raw in table else None
    return marks.render_verdict(marks.classify_word(table, raw, shown)), known


def _supports_none(current: Mapping[str, object], tz: str) -> bool:
    """「止める理由なし」と出してよいのは、kill switch が解除と読め、最新記録が新鮮で、
    必要な 2 つの時刻が読め、応答の中の経過秒数・DD・equity が停止条件に反しないときだけ。
    欠ける・矛盾するなら不明に倒す。判定の作り直しはここまでで、以上は daemon を正とする。"""
    if not (marks.classify_kill_switch(current.get("kill_switch")) == (marks.OK, "解除")
            and current.get("snapshot_freshness") == "fresh"
            and _time(current.get("snapshot_at"), tz) != UNKNOWN
            and _time(current.get("state_fetched_at"), tz) != UNKNOWN):
        return False
    age = _number(current.get("snapshot_age_sec"))
    limit = _number(current.get("snapshot_max_age_min"))
    dd = _number(current.get("dd_pct"))
    threshold = _number(current.get("kill_pct"))
    level = _number(current.get("kill_level"))
    headroom = _number(current.get("headroom"))
    if None in (age, limit, dd, threshold, level, headroom):
        return False
    return age <= limit * 60 and dd < threshold and level + headroom > 0


def _current(data: Mapping[str, object], tz: str) -> str:
    current = _dict(data.get("current"))
    unit = _currency(data)
    suffix = f" {unit}" if unit else ""
    kill_switch = current.get("kill_switch")
    generation = _dict(kill_switch).get("generation")
    raw_hold = current.get("fill_hold")
    contradicted = raw_hold == "none" and not _supports_none(current, tz)
    hold, hold_word = _word_card(marks.FILL_HOLD, None if contradicted else raw_hold,
                                 marks.FILL_HOLD_TEXT)
    if hold_word is not None:
        hold += " " + _esc(FILL_HOLD_NOTE[hold_word])
    elif contradicted:
        hold += " " + _esc("他の値と合わず、止める理由がないとは言えません")
    fresh, _ = _word_card(marks.FRESHNESS, current.get("snapshot_freshness"), marks.WORD_TEXT)

    def money(value: object) -> str:
        text = _amount(value)
        return text if text == UNKNOWN else _esc(text + suffix)

    items = [
        ("kill switch", marks.render_verdict(marks.classify_kill_switch(kill_switch))),
        ("新規の約定", hold),
        ("DD", _esc(_percent(current.get("dd_pct")))),
        ("閾値 (DD)", _esc(_percent(current.get("kill_pct")))),
        ("作動の水準", money(current.get("kill_level"))),
        ("余裕", money(current.get("headroom"))),
        ("最新記録の鮮度", fresh),
        ("最新記録からの経過", _esc(_elapsed(current.get("snapshot_age_sec")))),
        ("世代", _count(generation)),
    ]
    limit = _number(current.get("snapshot_max_age_min"))
    if type(current.get("snapshot_max_age_min")) is bool or limit is None or limit <= 0:
        stale = "最新記録が古い場合は古い記録として扱います。"
    else:
        stale = f"{limit:g} 分を超えた最新記録は古い記録として扱います。"
    notes = ("<p>余裕が正でも解除されたとは限りません。新規の約定が止まっているかどうかは"
             "「新規の約定」の欄が示します。" + stale + "</p>")
    notice = (f"<p>状態の取得時刻: {_time(current.get('state_fetched_at'), tz)} / "
              f"最新記録の時刻: {_time(current.get('snapshot_at'), tz)}</p>"
              "<p>この画面は自動で更新しません。今の状態は再読み込みか Status 画面 (自動で更新) で"
              '確かめてください (<a href="/">Status 画面を開く</a>)</p>')
    return '<section class="now"><h2>今の状態</h2>' + notice + marks.cards(items) + notes + "</section>"


# --- 数字で読む ------------------------------------------------------------------------

def _stats(data: Mapping[str, object], tz: str) -> str:
    stats = _dict(data.get("stats"))
    if "first_at" in stats and "last_at" in stats and stats["first_at"] is None \
            and stats["last_at"] is None:
        return ""
    unit = _currency(data)
    rows = []
    for label, name in (("equity", "equity"), ("balance", "balance")):
        cells = [_amount(stats.get(f"{name}_{key}")) for key in ("first", "last", "min", "max")]
        rows.append(f"<tr><td>{label}</td>" + "".join(f"<td>{_esc(c)}</td>" for c in cells)
                    + "</tr>")
    table = ('<table class="num"><thead><tr><th>項目</th><th>期間の初め</th><th>期間の終わり</th>'
             "<th>最小</th><th>最大</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")
    first, last = _time(stats.get("first_at"), tz), _time(stats.get("last_at"), tz)
    scope = UNKNOWN if first == last == UNKNOWN else f"{first} 〜 {last}"
    span = (f"<p>記録の範囲: {scope}" + (f" (単位: {_esc(unit)})" if unit else "") + "</p>")
    peak, peak_at = _esc(_percent(stats.get("dd_max"))), _time(stats.get("dd_max_at"), tz)
    if peak == UNKNOWN:
        peak_text = UNKNOWN
    else:
        peak_text = f"{peak} ({peak_at})" if peak_at != UNKNOWN else f"{peak} (時刻は不明)"
    drawdown = f"<p>最大 DD: {peak_text}</p>"
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
             _optional_time(item.get("book_at"), tz),
             _optional_time(item.get("applied_at"), tz), _generation_cell(item.get("generation")),
             _esc(TIME_SOURCES[source]) if type(source) is str and source in TIME_SOURCES
             else UNKNOWN]
    return "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"


def _mark_items(data: Mapping[str, object]) -> list[dict]:
    raw = data.get("marks")
    return [item for item in raw if type(item) is dict] if type(raw) is list else []


def _marks_section(data: Mapping[str, object], tz: str, *, table: bool = True) -> str:
    items = _mark_items(data)
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


# --- 図 ------------------------------------------------------------------------------------

def _figure(data: Mapping[str, object], tz: str, *, draw: bool = True) -> str:
    """資金推移の図の枠。描けない・予算を超えた図は説明文にし、固定文と注記は必ず残す。"""
    stats = _dict(data.get("stats"))
    empty = "first_at" in stats and "last_at" in stats and stats["first_at"] is None \
        and stats["last_at"] is None
    if draw:
        chart = svg.draw_equity(data, tz, _mark_items(data)[:MAX_MARKS])
    else:
        chart = svg.Chart("", None, (svg.NOTE_LINES_OVER,))
    if chart.svg:
        lines = list(chart.notes)
    else:
        lines = [EQUITY_EMPTY if empty and not chart.span else chart.notes[0]]
    body = ('<div class="scroll">' + chart.svg + "</div>" if chart.svg else "") + "".join(f"<p>{_esc(line)}</p>" for line in lines)
    if chart.svg and chart.span is not None:
        unit = _currency(data)
        body += (f"<p>横軸: 表示 tz の時刻 ({_esc(chart.span[0])} 〜 {_esc(chart.span[1])})、"
                 "縦軸: 口座通貨の金額" + (f" ({_esc(unit)})" if unit else "") + "</p>")
    omissions = _dict(data.get("omissions"))
    omitted = [(label, omissions.get(key)) for label, key in (("線の点", "equity_points"),
                                                              ("HWM の段", "hwm_steps"))]
    shown = [f"{label} {_count(value)}" for label, value in omitted
             if type(value) is int and value > 0]
    if shown:
        body += ("<p>図は一部を間引いています (" + _esc("、".join(shown))
                 + ")。下の数字は間引く前の集計です</p>")
    body += "<p>線は時間で束ねた表示で、判定の根拠ではありません。</p>"
    return "<section><h2>資金推移の図</h2>" + body + "</section>"


# --- 注記 ----------------------------------------------------------------------------------

def _notes(data: Mapping[str, object], tz: str) -> str:
    since = data.get("marks_since")
    if since is None:
        history = "kill switch の印は記録がある分だけです。event の記録はありません"
    else:
        history = f"kill switch の印は記録がある分だけです。event の記録は {_time(since, tz)} 以降です"
    items = [
        "kill switch は、equity が作動の水準以下のときに建てる判断が出ると作動します。"
        "線を割っただけでは印は付きません",
        history,
        "期間内にも履歴の欠けがあり得ます",
        "束内の equity と作動の水準の厳密な交差は再現していません",
        "event だけから分かる印は、作動 / 解除を記録した時刻です。通知の時刻を状態が変わった時刻とは"
        "断定しません",
    ]
    return "<section><h2>注記</h2><ul>" + "".join(f"<li>{line}</li>" for line in items) + "</ul></section>"


# --- 画面 --------------------------------------------------------------------------------------

def render_performance(data: Mapping[str, object], tab: str = "equity",
                       range_key: str = DEFAULT_RANGE) -> bytes:
    if tab == "strategy":
        return _render_strategy(data, range_key)
    return _render_equity(data, range_key)


def _render_equity(data: Mapping[str, object], range_key: str) -> bytes:
    """資金推移の画面。合計の予算を超えるときは、印の表、次に図を説明文に置き換える (各 1 段)。"""
    tab = "equity"
    tz = zone_of(data)
    bar = tabs.bar(PATH, tab, carry={"range": range_key} if range_key != DEFAULT_RANGE else None,
                   omit_default=True)
    head = "<p>paper の記録です</p>" + _warnings(data)
    current = _current(data, tz) + _range_links(tab, range_key)
    stats = _stats(data, tz)
    tail = _notes(data, tz)

    def build(figure: str, marks_part: str) -> bytes:
        return render._document("Performance", head + current + figure + stats + marks_part + tail,
                                tz, current=PATH, tabs=bar, refresh=False)
    figure = _figure(data, tz)
    document = build(figure, _marks_section(data, tz))
    if len(document) <= PAGE_BUDGET_BYTES:
        return document
    replaced = _marks_section(data, tz, table=False)
    document = build(figure, replaced)
    if len(document) <= PAGE_BUDGET_BYTES:
        return document
    return render._page("Performance", head + current + _figure(data, tz, draw=False) + stats
                        + replaced + tail, tz, current=PATH, tabs=bar, refresh=False)


# --- 戦略別 ------------------------------------------------------------------------------------

STRATEGY_INTRO = ("その戦略のシグナルで起きた判断の成績です。建てる・決済する判断は LLM が行うので、"
                  "戦略そのものの成績 (backtest) とは違います")
STRATEGY_NOTES = (
    "累積損益の合計は equity の変化と一致しません (含み損益、期間の外で決済した取引、"
    "スワップを含まないため)",
    "同じ名前の plugin の版が変わっても、同じ行に数えます",
)
CRON_LABEL = f"{svg.CRON_NAME} (cron、戦略に属さない)"
UNKNOWN_LABEL = "帰属不明"
REASON_LABELS = (("llm_close", "判断"), ("sl", "SL"), ("tp", "TP"), ("other", "その他"))
STRATEGY_HEADERS = ("起動理由", "件数", "勝 / 負 / 分", "勝率", "PF", "平均 R (手数料込み)",
                    "損益の合計", "決済の内訳", "保有中")
NOTE_STRATEGIES_UNKNOWN = "戦略別の成績を読めません。数字は不明です"
NOTE_ROWS_DROPPED = "読めない行が {} 件あります。表と図には読めた行だけを出しています"
NOTE_CLOSED_UNKNOWN = "決済の件数を読めない行があります"
NO_STRATEGY_FIGURE = "この期間に決済した取引はありません。期間を広げると出ることがあります"
NOTE_TABLE_OVER = "戦略の表は表示の上限を超えたため省きました"
_ORIGIN = re.compile(r"signal:[a-z][a-z0-9_]{0,63}\Z")


def _signed_amount(value: object) -> str:
    text = _amount(value)
    number = _number(value)
    return "+" + text if number is not None and text != UNKNOWN and not text.startswith("-") \
        and number > 0 and any(c in "123456789" for c in text) else text


def _signed_r(value: object) -> str:
    number = _number(value)
    if number is None:
        return UNKNOWN
    if abs(number) >= BIG:
        return f"{number:+.2e}"
    text = f"{number:+.2f}"
    return "0.00" if not any(c in "123456789" for c in text) else text


def _thresholds(data: Mapping[str, object]) -> tuple[int, int] | None:
    """daemon が返す、率を出す最小の件数と「(参考)」が外れる件数。読めなければ None (率は不明)。"""
    raw = _dict(data.get("thresholds"))
    low, high = raw.get("min_count"), raw.get("reference_count")
    if type(low) is int and type(high) is int and 0 < low <= high:
        return low, high
    return None


def _rated(value: str, count: object, limits: tuple[int, int] | None) -> str:
    """母数が少ない率は「–」、基準に満たなければ「(参考)」を添える。"""
    if limits is None or type(count) is not int or count < 0:
        return UNKNOWN
    if count < limits[0]:
        return NONE_TEXT
    return value if count >= limits[1] or value == UNKNOWN else value + " (参考)"


def _rate_note(limits: tuple[int, int] | None) -> str:
    """率が「–」や「(参考)」になる規則。件数の基準は daemon が返した値。"""
    if limits is None:
        return "件数の基準を読めないため、勝率・PF・平均 R は不明にしました"
    return (f"勝率・PF・平均 R は、もとになる件数 (勝率・PF は損益の分かる決済、平均 R は R を算出できた"
            f"決済) が {limits[0]} 件未満だと出しません (–)。{limits[1]} 件未満は参考の値です")


def _origin_cell(row: Mapping[str, object]) -> str:
    origin = row.get("origin")
    if origin == "cron":
        return _esc(CRON_LABEL)
    if origin == "unknown":
        return _esc(UNKNOWN_LABEL)
    if origin == "other":
        return _esc(f"ほか {_count(row.get('origin_count'))} 件")
    if type(origin) is str and _ORIGIN.match(origin):
        return _esc(origin)
    return UNKNOWN


def _ratio_percent(value: object) -> str:
    number = _number(value)
    return UNKNOWN if number is None or not 0 <= number <= 1 else _percent(number * 100)


def _profit_factor(row: Mapping[str, object], limits: tuple[int, int] | None) -> str:
    """PF。null の理由は daemon の `pf_state` で決め、値と状態が噛み合わないものは「不明」。"""
    known = row.get("pnl_known_count")
    state, raw = row.get("pf_state"), row.get("pf")
    value = _number(raw)
    if state == "no_losses" and raw is None:
        text = "損失なし"
    elif state == "computed" and value is not None:
        text = f"{value:.2f}" if value < BIG else f"{value:.2e}"
    else:
        text = UNKNOWN
    return _rated(text, known, limits)


def _average_r(row: Mapping[str, object], limits: tuple[int, int] | None) -> str:
    known = row.get("r_known_count")
    value = "–" if row.get("avg_r") is None and known == 0 else _signed_r(row.get("avg_r"))
    cell = _esc(_rated(value, known, limits))
    closed = row.get("closed_count")
    if type(known) is int and type(closed) is int and known == closed:
        return cell
    return cell + f"<br>R 算出 {_count(known)} / 決済 {_count(closed)} 件"


def _reasons_cell(row: Mapping[str, object]) -> str:
    reasons = _dict(row.get("close_reasons"))
    shown = []
    for key, label in REASON_LABELS:
        value = reasons.get(key)
        if type(value) is int and value == 0:
            continue
        shown.append((label, _count(value)))
    if shown and all(count == UNKNOWN for _, count in shown):
        return UNKNOWN
    return _esc(" / ".join(f"{label} {count}" for label, count in shown)) if shown else NONE_TEXT


def _holding_cell(row: Mapping[str, object]) -> str:
    text = _count(row.get("open_count"))
    pending = row.get("close_pending_count")
    if text == UNKNOWN and _count(pending) == UNKNOWN:
        return UNKNOWN
    if type(pending) is not int or pending != 0:
        text += f" (決済未確定 {_count(pending)})"
    return _esc(text)


def _closed_cell(row: Mapping[str, object]) -> str:
    """件数。損益が不明な決済が無いと分かるときは件数だけ、差があるか読めないときは内訳を添える。"""
    closed = _count(row.get("closed_count"))
    known, unknown = _count(row.get("pnl_known_count")), _count(row.get("pnl_unknown_count"))
    if unknown == "0":
        return closed
    if known == unknown == UNKNOWN:
        return closed if closed == UNKNOWN else f"{closed} (損益の内訳は不明)"
    return f"{closed} (損益既知 {known} / 損益不明 {unknown})"


def _strategy_row(row: Mapping[str, object], limits: tuple[int, int] | None) -> str:
    known = row.get("pnl_known_count")
    closed = _closed_cell(row)
    wins, losses, flats = (_count(row.get(k)) for k in ("wins", "losses", "flats"))
    total = NONE_TEXT if row.get("total_pnl") is None and known == 0 \
        else _signed_amount(row.get("total_pnl"))
    cells = [_origin_cell(row), _esc(closed), _esc(f"{wins} / {losses} / {flats}"),
             _esc(_rated(_ratio_percent(row.get("win_rate")), known, limits)),
             _esc(_profit_factor(row, limits)), _average_r(row, limits), _esc(total),
             _reasons_cell(row), _holding_cell(row)]
    return "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"


def strategy_rows(data: Mapping[str, object]) -> list[dict]:
    raw = data.get("strategies")
    return [item for item in raw if type(item) is dict] if type(raw) is list else []


def _strategy_table(data: Mapping[str, object], *, table: bool = True) -> str:
    rows = strategy_rows(data)
    raw = data.get("strategies")
    unit = _currency(data)
    heads = list(STRATEGY_HEADERS)
    if unit:
        heads[6] += f" ({unit})"
    heading = "<section><h2>起動理由ごとの表</h2>"
    if type(raw) is not list or (raw and not rows):
        return heading + f"<p>{NOTE_STRATEGIES_UNKNOWN}</p></section>"
    dropped = len(raw) - len(rows)
    dropped_note = f"<p>{NOTE_ROWS_DROPPED.format(dropped)}</p>" if dropped else ""
    if not rows:
        return heading + "<p>この期間に決済した取引も、保有中の取引もありません</p></section>"
    counts = [r.get("closed_count") for r in rows]
    if any(type(c) is not int or c < 0 for c in counts):
        empty = f"<p>{NOTE_CLOSED_UNKNOWN}</p>"
    elif not any(counts):
        empty = "<p>この期間に決済した取引はありません</p>"
    else:
        empty = ""
    omitted = _dict(data.get("omissions")).get("strategy_rows")
    merged = (f"<p>行数が多いため、{_count(omitted)} 件の起動理由を「ほか」の行にまとめました</p>"
              if type(omitted) is int and omitted > 0 else "")
    if table:
        limits = _thresholds(data)
        body = ('<div class="scroll"><table class="num"><thead><tr>'
                + "".join(f"<th>{_esc(name)}</th>" for name in heads)
                + "</tr></thead><tbody>" + "".join(_strategy_row(r, limits) for r in rows)
                + "</tbody></table></div>")
        text = heading + empty + dropped_note + body + merged + "</section>"
        if len(text.encode("utf-8")) <= STRATEGY_TABLE_BUDGET_BYTES:
            return text
    return heading + empty + dropped_note + f"<p>{NOTE_TABLE_OVER}</p>" + merged + "</section>"


def _strategy_figure(data: Mapping[str, object], tz: str, *, draw: bool = True) -> str:
    limits = svg.series_limits(data)
    chosen, skipped = svg.series_rows(data.get("strategies"), limits[1] if limits else None)
    if draw:
        chart = svg.draw_strategies(data, tz, _signed_amount)
    else:
        chart = svg.Chart("", None, (svg.NOTE_STRATEGY_OVER,))
    body = '<div class="scroll">' + chart.svg + "</div>" if chart.svg else ""
    notes = chart.notes
    if notes == (svg.NOTE_NO_SERIES,) and type(data.get("strategies")) is list \
            and not data["strategies"]:
        notes = (NO_STRATEGY_FIGURE,)
    body += "".join(f"<p>{_esc(line)}</p>" for line in notes)
    if chart.svg and chart.span is not None:
        unit = _currency(data)
        body += (f"<p>横軸: 表示 tz の時刻 ({_esc(chart.span[0])} 〜 {_esc(chart.span[1])})、"
                 "縦軸: 期間の初めを 0 とした累積損益" + (f" ({_esc(unit)})" if unit else "")
                 + "</p>")
    hidden = skipped
    series_hidden = _dict(data.get("omissions")).get("strategy_series")
    if type(series_hidden) is int and series_hidden > 0:
        hidden += series_hidden
    if hidden:
        body += f"<p>ほか {_count(hidden)} 行は表のみです</p>"
    if chart.svg:
        for item in chosen:
            body += _series_start_note(item, data, tz, limits)
            if item.row.get("pnl_known_count") == 0 and item.row.get("closed_count"):
                body += f"<p>{_esc(svg.origin_name(item.origin))}: 損益が不明のため、0 の線です</p>"
    return "<section><h2>累積損益の図</h2>" + body + "</section>"


def _series_start_note(item: svg.Series, data: Mapping[str, object], tz: str,
                       limits: tuple[int, int] | None) -> str:
    """線が期間の初めから始まらない系列の、描画開始時刻と省いた古い決済の件数。"""
    points = [p for p in svg._cumulative(item.row.get("cumulative")) if p is not None]
    if not points:
        return ""
    closed = item.row.get("closed_count")
    omitted = max(closed - limits[0], 0) if type(closed) is int and limits else None
    period_start = svg._epoch(data.get("from"))
    late = period_start is not None and points[0][0] > period_start
    if not late and not omitted:
        return ""
    start = svg._full(points[0][0], tz) or UNKNOWN
    name = _esc(svg.origin_name(item.origin))
    if omitted is None:
        head = f"古い決済の件数は不明です。{_esc(start)} の始点 1 点にまとめています"
    elif omitted:
        head = f"古い決済 {_count(omitted)} 件は {_esc(start)} の始点 1 点にまとめています"
    else:
        head = f"線は {_esc(start)} から描いています"
    return f"<p>{name}: {head} (これより前の線は描いていません)</p>"


def _strategy_warnings(data: Mapping[str, object]) -> str:
    quality = _dict(data.get("quality"))
    lines = []
    if quality.get("partial") is True:
        lines.append("一部の記録を読めていません。集計は読めた分だけです")
    for key, text in (("invalid_orders", "読めない注文があります。その指標は不明にしました"),
                      ("unplaced_closed_count", "決済の時刻を読めない取引があり、期間に数えていません")):
        value = quality.get(key)
        if type(value) is int and value > 0:
            lines.append(f"{text} ({_count(value)} 件)")
    return "".join(f'<p><span class="st warn">△ 注意</span> {_esc(line)}</p>' for line in lines)


def _render_strategy(data: Mapping[str, object], range_key: str) -> bytes:
    """戦略別の画面。合計の予算を超えるときは、図、次に表を説明文に置き換える (各 1 段)。"""
    tz = zone_of(data)
    bar = tabs.bar(PATH, "strategy", carry={"range": range_key} if range_key != DEFAULT_RANGE
                   else None, omit_default=True)
    head = ("<p>paper の記録です</p>" + _strategy_warnings(data) + "<p>" + _esc(STRATEGY_INTRO)
            + "</p>" + _range_links("strategy", range_key))
    notes = (_rate_note(_thresholds(data)),) + STRATEGY_NOTES
    tail = ("<section><h2>注記</h2><ul>" + "".join(f"<li>{_esc(line)}</li>" for line in notes)
            + "</ul></section>")

    def build(figure: str, table: str) -> bytes:
        return render._document("Performance", head + figure + table + tail, tz, current=PATH,
                                tabs=bar, refresh=False)
    figure, table = _strategy_figure(data, tz), _strategy_table(data)
    document = build(figure, table)
    if len(document) <= STRATEGY_PAGE_BUDGET_BYTES:
        return document
    figure = _strategy_figure(data, tz, draw=False)
    document = build(figure, table)
    if len(document) <= STRATEGY_PAGE_BUDGET_BYTES:
        return document
    table = _strategy_table(data, table=False)
    return render._page("Performance", head + figure + table + tail, tz, current=PATH, tabs=bar,
                        refresh=False)
