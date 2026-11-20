"""資金推移の図 (server が描く SVG)。

daemon が束ねた代表点・HWM の段・印だけを、整数座標の要素にする。色・線種は class と埋め込みの
CSS で付け (style 属性・色の直書き・script・event 属性は出さない)、文字は固定語か数値・換算した時刻
だけを escape して入れる。読めない値は描かずに飛ばし、巨大な値や欠けを正常な線に見せない。
図は判定の根拠ではない。daemon の応答以外の値は使わず、値から安全の判断を作り直さない。
"""
from __future__ import annotations

import html
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import NamedTuple

from afx_ops import display_time

VIEW_W, VIEW_H = 960, 260
X0, X1, Y0, Y1 = 72, 952, 12, 186
MAX_POINTS = 280
MAX_STEPS = 320
BIG = 1e12
# 目盛り間隔の下限。これより細かい値幅は広げて描く
MIN_TICK_STEP = 1e-9
# 時刻として読む範囲 (1971〜9998 年。9999-01-01T00:00:00 UTC は含めない)。外の値は表示 tz に直せないことがあるので読めない値にする
EPOCH_MIN = datetime(1971, 1, 1, tzinfo=timezone.utc).timestamp()
EPOCH_MAX_EXCLUSIVE = datetime(9999, 1, 1, tzinfo=timezone.utc).timestamp()
# 図の部品ごとの予算 (UTF-8 byte)。超えた部品は丸ごと説明文に置き換える
LINES_BUDGET_BYTES = 32 * 1024
MARKS_BUDGET_BYTES = 6 * 1024
TICKS_X = 6
OPENING = (f'<svg viewBox="0 0 {VIEW_W} {VIEW_H}" role="img" '
           'aria-label="資金推移の図 (equity・balance・HWM・作動の水準・印)">')

# 印の種別 → (記号, 語, class)。表 (perf_pages.MARK_KINDS) と同じ記号・語
MARK_STYLE = {"latched": ("✕", "作動", "ev"), "released": ("↑", "解除", "rl"),
              "rebase": ("▽", "HWM の付け直し", "rb"), "cashflow": ("◇", "入出金", "cf"),
              "unknown_hwm_drop": ("△", "不明な HWM 下降", "rb")}
LINE_LEGEND = (("c1", "equity"), ("c2", "balance"), ("c3", "HWM"), ("c4", "作動の水準 (今の閾値)"))
BAND_LEGEND = "帯: 束内の最小〜最大"
LEGEND_STEP = 176
# 印の記号 1 つぶんの文字幅 (図の単位)。段の数は図の高さに収まる範囲
MARK_WIDTH = 12
MARK_ROWS = (Y1 - Y0 - 10) // 12
_TITLE_MAX_BYTES = 160

NOTE_LINES_OVER = "図は表示の上限を超えたため省きました。数字は下の表を見てください"
NOTE_MARKS_OVER = "印の図は表示の上限を超えたため省きました。印は下の表にあります"
NOTE_NO_POINTS = "図に描ける記録がありません。数字は下の表を見てください"


class Chart(NamedTuple):
    svg: str                 # 描かなかったときは空
    span: tuple[str, str] | None  # 図の横軸の両端 (表示 tz の時刻)
    notes: tuple[str, ...]   # 図の代わり・省いた部品の説明 (固定文)


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _number(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and abs(number) < BIG else None


def _epoch(value: object) -> float | None:
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        stamp = parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None
    return stamp if EPOCH_MIN <= stamp < EPOCH_MAX_EXCLUSIVE else None


def _integer(value: object) -> int | None:
    return value if type(value) is int and 0 <= value < 10 ** 9 else None


class _Part(NamedTuple):
    seg: int
    gap: int
    start: float
    end: float
    last: float
    low: float
    high: float
    equity: float
    balance: float | None


class _Step(NamedTuple):
    seg: int
    gap: int
    at: float
    hwm: float


def _parts(raw: object) -> list[_Part]:
    parts = []
    gap = 0  # 読めない行を飛ばした回数。前後の行を線でつながない境界に使う
    for item in raw if type(raw) is list else []:
        if type(item) is not dict:
            gap += 1
            continue
        seg = _integer(item.get("seg"))
        start, end, last = (_epoch(item.get(k)) for k in ("start_at", "end_at", "last_at"))
        low, high, equity = (_number(item.get(k)) for k in
                             ("equity_min", "equity_max", "equity_last"))
        if seg is None or None in (start, end, last, low, high, equity) or start > end \
                or low > high:
            gap += 1
            continue
        parts.append(_Part(seg, gap, start, end, last, low, high, equity,
                           _number(item.get("balance_last"))))
    return parts[-MAX_POINTS:]


def _steps(raw: object) -> list[_Step]:
    steps = []
    gap = 0
    for item in raw if type(raw) is list else []:
        if type(item) is not dict:
            gap += 1
            continue
        seg, at, hwm = _integer(item.get("seg")), _epoch(item.get("t")), _number(item.get("hwm"))
        if seg is not None and at is not None and hwm is not None:
            steps.append(_Step(seg, gap, at, hwm))
        else:
            gap += 1
    return steps[-MAX_STEPS:]


def _kill_ratio(current: object) -> float | None:
    """作動の水準 = HWM × (1 − kill_pct / 100)。閾値が読めない・範囲外なら線を描かない。"""
    value = _number(current.get("kill_pct")) if type(current) is dict else None
    return None if value is None or not 0 < value < 100 else 1 - value / 100


class _Marks(NamedTuple):
    kind: str
    at: float
    title: str


def _marks(raw: Sequence[object], tz: str) -> list[_Marks]:
    found = []
    for item in raw:
        if type(item) is not dict:
            continue
        kind, at = item.get("kind"), _epoch(item.get("plot_at"))
        if type(kind) is not str or kind not in MARK_STYLE or at is None:
            continue
        converted = display_time.convert_time(item.get("plot_at"), tz,
                                              display_time.PRECISION_SECOND)
        if not converted.ok:
            continue
        symbol, word, _ = MARK_STYLE[kind]
        found.append(_Marks(kind, at, f"{symbol} {word} {converted.text}"))
    return found


# --- 軸 --------------------------------------------------------------------------------

def _nice_ticks(low: float, high: float) -> tuple[list[float], float]:
    raw = (high - low) / 4
    if not (math.isfinite(raw) and raw >= MIN_TICK_STEP):
        raw = 1.0
        high = low + 4.0
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw)
    first = math.ceil(low / step)
    ticks = []
    index = first
    while index * step <= high and len(ticks) < 6:
        ticks.append(index * step)
        index += 1
    return ticks, step


def _axis_text(value: float, step: float) -> str:
    digits = 0 if step >= 1 else min(6, math.ceil(-math.log10(step)))
    text = f"{value:.{digits}f}"
    if text.startswith("-") and not any(c in "123456789" for c in text):
        text = text[1:]
    return text


def _date_label(at: float, tz: str, mode: str) -> str:
    try:
        converted = display_time.convert_time(datetime.fromtimestamp(at, tz=timezone.utc), tz,
                                              display_time.PRECISION_MINUTE)
    except (ValueError, OverflowError, OSError):
        return ""
    if not converted.ok:
        return ""
    text = converted.text
    return {"full": text[:10], "day": text[5:10], "time": text[5:16]}[mode]


class _Scale(NamedTuple):
    t_low: float
    t_high: float
    v_low: float
    v_high: float

    def x(self, at: float) -> int:
        ratio = (at - self.t_low) / (self.t_high - self.t_low)
        return X0 + round(min(1.0, max(0.0, ratio)) * (X1 - X0))

    def y(self, value: float) -> int:
        ratio = (value - self.v_low) / (self.v_high - self.v_low)
        return Y1 - round(min(1.0, max(0.0, ratio)) * (Y1 - Y0))


def _vertices(pairs: Sequence[tuple[int, int]]) -> str:
    return " ".join(f"{x},{y}" for x, y in pairs)


def _line(css: str, pairs: Sequence[tuple[int, int]]) -> str:
    if len(pairs) == 1:  # 1 点だけの区間は短い横線にする (点は描かれない)
        pairs = [pairs[0], (pairs[0][0] + 2, pairs[0][1])]
    return f'<polyline class="{css}" points="{_vertices(pairs)}"/>'


def _runs(parts: Sequence) -> list[list]:
    runs: list[list] = []
    for part in parts:
        if runs and (runs[-1][-1].seg, runs[-1][-1].gap) == (part.seg, part.gap):
            runs[-1].append(part)
        else:
            runs.append([part])
    return runs


def _chunks(items: Sequence, keep) -> list[list]:
    """keep を満たす連続した要素ごとに分ける。満たさない要素の位置で切る。"""
    chunks: list[list] = []
    cut = True
    for item in items:
        if not keep(item):
            cut = True
            continue
        if cut:
            chunks.append([])
            cut = False
        chunks[-1].append(item)
    return chunks


def _plot(scale: _Scale, parts: list[_Part], steps: list[_Step], ratio: float | None) -> str:
    out = []
    for run in _runs(parts):
        top = [pair for p in run for pair in ((scale.x(p.start), scale.y(p.high)),
                                              (scale.x(p.end), scale.y(p.high)))]
        bottom = [pair for p in reversed(run) for pair in ((scale.x(p.end), scale.y(p.low)),
                                                           (scale.x(p.start), scale.y(p.low)))]
        out.append(f'<polygon class="bd" points="{_vertices(top + bottom)}"/>')
    for run in _runs(steps):
        for css, level in (("c4", ratio), ("c3", 1.0)):
            if level is None:
                continue
            for usable in _chunks(run, lambda s: css == "c3" or s.hwm > 0):
                pairs = []
                for index, step in enumerate(usable):
                    if index:
                        pairs.append((scale.x(step.at), scale.y(usable[index - 1].hwm * level)))
                    pairs.append((scale.x(step.at), scale.y(step.hwm * level)))
                out.append(_line(css, pairs))
    for run in _runs(parts):
        for chunk in _chunks(run, lambda p: p.balance is not None):
            out.append(_line("c2", [(scale.x(p.last), scale.y(p.balance)) for p in chunk]))
    for run in _runs(parts):
        out.append(_line("c1", [(scale.x(p.last), scale.y(p.equity)) for p in run]))
    return "".join(out)


def _frame(scale: _Scale, tz: str) -> str:
    ticks, step = _nice_ticks(scale.v_low, scale.v_high)
    out = [f'<line class="ax" x1="{X0}" y1="{Y0}" x2="{X0}" y2="{Y1}"/>',
           f'<line class="ax" x1="{X0}" y1="{Y1}" x2="{X1}" y2="{Y1}"/>']
    for value in ticks:
        y = scale.y(value)
        out.append(f'<line class="ax" x1="{X0 - 4}" y1="{y}" x2="{X0}" y2="{y}"/>'
                   f'<text x="{X0 - 8}" y="{y + 4}" text-anchor="end">'
                   f'{_esc(_axis_text(value, step))}</text>')
    span = scale.t_high - scale.t_low
    first = _date_label(scale.t_low, tz, "full")
    last = _date_label(scale.t_high, tz, "full")
    mode = "time" if span < 2 * 86400 else ("full" if first[:4] != last[:4] else "day")
    for index in range(TICKS_X):
        at = scale.t_low + span * index / (TICKS_X - 1)
        anchor = "start" if index == 0 else "end" if index == TICKS_X - 1 else "middle"
        out.append(f'<text x="{scale.x(at)}" y="{Y1 + 18}" text-anchor="{anchor}">'
                   f'{_esc(_date_label(at, tz, mode))}</text>')
    return "".join(out)


def _legend(drawn: Sequence[str], kinds: Sequence[str]) -> str:
    """描いた線 (class) と印の種別だけを載せる。"""
    out = []
    cells = [item for item in LINE_LEGEND if item[0] in drawn]
    for index, (css, label) in enumerate(cells):
        x = X0 + LEGEND_STEP * index
        out.append(f'<line class="{css}" x1="{x}" y1="222" x2="{x + 28}" y2="222"/>'
                   f'<text x="{x + 34}" y="226">{_esc(label)}</text>')
    x = X0 + LEGEND_STEP * len(cells)
    out.append(f'<polygon class="bd" points="{_vertices([(x, 216), (x + 28, 216), (x + 28, 228), (x, 228)])}"/>'
               f'<text x="{x + 34}" y="226">{_esc(BAND_LEGEND)}</text>')
    shown = [MARK_STYLE[kind] for kind in MARK_STYLE if kind in kinds]
    for index, (symbol, word, css) in enumerate(shown):
        out.append(f'<text class="{css}" x="{X0 + LEGEND_STEP * index}" y="246">'
                   f'{_esc(symbol + " " + word)}</text>')
    return "".join(out)


def _marks_part(scale: _Scale, marks: list[_Marks]) -> str:
    """同じ x 位置・同じ種別の印は 1 つの記号にまとめて件数を添える。文字が重なり合う印 (近い位置・別種別) は
    縦に段をずらす。"""
    groups: dict[tuple[int, str], list[_Marks]] = {}
    for mark in marks:
        groups.setdefault((scale.x(mark.at), mark.kind), []).append(mark)
    order = list(MARK_STYLE)
    placed: list[list[tuple[int, int]]] = []  # 段ごとの (中心 x, 文字幅)
    out = []
    for (x, kind), members in sorted(groups.items(), key=lambda g: (g[0][0], order.index(g[0][1]))):
        count = len(members)
        symbol, _, css = MARK_STYLE[kind]
        shown = f"{symbol}×{count}" if count > 1 else symbol
        width = MARK_WIDTH * len(shown)
        row = next((i for i, taken in enumerate(placed)
                    if all(abs(x - cx) * 2 >= width + w for cx, w in taken)), len(placed))
        if row == len(placed):
            placed.append([])
        placed[row].append((x, width))
        base = members[0].title.encode("utf-8")[:_TITLE_MAX_BYTES - 16].decode("utf-8", "ignore")
        title = _esc(base + (f" ほか {count - 1} 件" if count > 1 else ""))
        out.append(f'<line class="{css}" x1="{x}" y1="{Y0}" x2="{x}" y2="{Y1}"/>'
                   f'<text class="{css}" x="{x}" y="{Y0 + 10 + 12 * min(row, MARK_ROWS - 1)}" '
                   f'text-anchor="middle"><title>{title}</title>{shown}</text>')
    return "".join(out)


def _scale(parts: list[_Part], steps: list[_Step], marks: list[_Marks], ratio: float | None,
           data: Mapping[str, object]) -> _Scale:
    times = [t for p in parts for t in (p.start, p.end, p.last)] + [s.at for s in steps] \
        + [m.at for m in marks]
    times += [t for t in (_epoch(data.get("from")), _epoch(data.get("to"))) if t is not None]
    t_low, t_high = min(times), max(times)
    if t_high <= t_low:
        t_high = t_low + 1
    values = [v for p in parts for v in (p.low, p.high, p.equity)]
    values += [p.balance for p in parts if p.balance is not None]
    values += [s.hwm for s in steps]
    if ratio is not None:
        values += [s.hwm * ratio for s in steps if s.hwm > 0]
    v_low, v_high = min(values), max(values)
    pad = (v_high - v_low) * 0.04 or max(abs(v_high) * 0.01, 1.0)
    low, high = v_low - pad, v_high + pad
    if not (high - low) / 4 >= MIN_TICK_STEP:
        low, high = v_low - 1.0, v_high + 1.0
    return _Scale(t_low, t_high, low, high)


def draw_equity(data: Mapping[str, object], tz: str, shown_marks: Sequence[object]) -> Chart:
    """資金推移の図。描ける点が無ければ空の svg と説明文を返す。部品が予算を超えたら丸ごと省く。"""
    parts, steps = _parts(data.get("points")), _steps(data.get("hwm_steps"))
    if not parts:
        return Chart("", None, (NOTE_NO_POINTS,))
    ratio = _kill_ratio(data.get("current"))
    marks = _marks(shown_marks, tz)
    scale = _scale(parts, steps, marks, ratio, data)
    marks_part, notes = _marks_part(scale, marks), ()
    if len(marks_part.encode("utf-8")) > MARKS_BUDGET_BYTES:
        # 描かない印で横軸を広げない
        marks, marks_part, notes = [], "", (NOTE_MARKS_OVER,)
        scale = _scale(parts, steps, marks, ratio, data)
    span = (_full(scale.t_low, tz), _full(scale.t_high, tz))
    drawn = ["c1"] + (["c2"] if any(p.balance is not None for p in parts) else []) \
        + (["c3"] if steps else []) \
        + (["c4"] if ratio is not None and any(s.hwm > 0 for s in steps) else [])
    body = _plot(scale, parts, steps, ratio) + _frame(scale, tz) \
        + _legend(drawn, [m.kind for m in marks])
    if len(body.encode("utf-8")) > LINES_BUDGET_BYTES:
        return Chart("", span, (NOTE_LINES_OVER,))
    return Chart(OPENING + body + marks_part + "</svg>", span, notes)


def _full(at: float, tz: str) -> str:
    try:
        converted = display_time.convert_time(datetime.fromtimestamp(at, tz=timezone.utc), tz,
                                              display_time.PRECISION_SECOND)
    except (ValueError, OverflowError, OSError):
        return ""
    return converted.text if converted.ok else ""


# --- 戦略別の累積損益 ----------------------------------------------------------------------

STRATEGY_BUDGET_BYTES = 14 * 1024
MAX_SERIES_POINTS = 122
HISTORY_EXTRA_POINTS = 2
SERIES_CSS = ("c1", "c2", "c3", "c4")
CRON_CSS = "cr"
CRON_ORIGIN = "cron"
_ORIGIN = re.compile(r"cron|signal:[a-z][a-z0-9_]{0,63}")
LEGEND_TOP = 226
LEGEND_ROW = 16
NOTE_STRATEGY_OVER = "累積損益の図は表示の上限を超えたため省きました。数字は下の表を見てください"
NOTE_NO_SERIES = "図に描ける累積損益がありません。数字は下の表を見てください"
NO_PNL = "損益不明"
NO_END = "終点 不明"


class Series(NamedTuple):
    css: str
    origin: str
    row: Mapping[str, object]


def series_limits(data: Mapping[str, object]) -> tuple[int, int] | None:
    """daemon が返す、系列ごとの決済の件数の上限と系列の数の上限。読めなければ None。"""
    raw = data.get("series_limits")
    raw = raw if type(raw) is dict else {}
    events, series = raw.get("events"), raw.get("series")
    if type(events) is int and type(series) is int and events > 0 and series > 0:
        return events, series
    return None


def point_cap(limits: tuple[int, int] | None) -> int:
    """系列 1 本で読む点の数。daemon の決済の上限に始点と終了点を足す。上限が無ければ既定。"""
    return limits[0] + HISTORY_EXTRA_POINTS if limits else MAX_SERIES_POINTS


def series_rows(rows: object, max_series: int | None = None) -> tuple[list[Series], int]:
    """図に描く系列と、色が足りず描かなかった系列の数。

    起動理由が文法に合い、累積の点を持つ行だけ。定期の判断は中立色の長破線、ほかは件数の多い順
    (行の並び順) に 4 色を割り当てる。"""
    chosen: list[Series] = []
    skipped = 0
    colors = iter(SERIES_CSS[:max_series])
    for row in rows if type(rows) is list else []:
        if type(row) is not dict or type(row.get("cumulative")) is not list \
                or not row["cumulative"]:
            continue
        origin = row.get("origin")
        if type(origin) is not str or not _ORIGIN.fullmatch(origin):
            continue
        if origin == CRON_ORIGIN:
            chosen.append(Series(CRON_CSS, origin, row))
            continue
        css = next(colors, None)
        if css is None:
            skipped += 1
        else:
            chosen.append(Series(css, origin, row))
    return chosen, skipped


def _cumulative(raw: object, cap: int = MAX_SERIES_POINTS) -> list[tuple[float, float] | None]:
    """読めない点は None のまま残し、線を切る位置にする。"""
    out: list[tuple[float, float] | None] = []
    for item in (raw if type(raw) is list else [])[-cap:]:
        at = _epoch(item.get("t")) if type(item) is dict else None
        pnl = _number(item.get("pnl")) if type(item) is dict else None
        out.append(None if at is None or pnl is None else (at, pnl))
    return out


def _end_value(points: Sequence[tuple[float, float] | None], total: object) -> float | None:
    """線の終点。最後の点が読めない、または表の合計と食い違うときは None (終点は不明)。"""
    last = points[-1] if points else None
    expected = _number(total)
    if last is None or expected is None:
        return None
    if abs(last[1] - expected) > 1e-6 * max(1.0, abs(expected)):
        return None
    return last[1]


def _stair(scale: _Scale, points: Sequence[tuple[float, float]]) -> list[tuple[int, int]]:
    """決済の時刻で垂直に動く階段 (水平 → 垂直)。"""
    pairs = [(scale.x(points[0][0]), scale.y(points[0][1]))]
    for (_, before), (at, after) in zip(points, points[1:]):
        pairs.append((scale.x(at), scale.y(before)))
        pairs.append((scale.x(at), scale.y(after)))
    return pairs


def _strategy_scale(data: Mapping[str, object],
                    lines: Sequence[Sequence[tuple[float, float]]]) -> _Scale:
    times = [t for line in lines for t, _ in line]
    times += [t for t in (_epoch(data.get("from")), _epoch(data.get("to"))) if t is not None]
    t_low, t_high = min(times), max(times)
    if t_high <= t_low:
        t_high = t_low + 1
    values = [v for line in lines for _, v in line] + [0.0]
    v_low, v_high = min(values), max(values)
    pad = (v_high - v_low) * 0.04 or 1.0
    low, high = v_low - pad, v_high + pad
    if not (high - low) / 4 >= MIN_TICK_STEP:
        low, high = v_low - 1.0, v_high + 1.0
    return _Scale(t_low, t_high, low, high)


def _strategy_legend(items: Sequence[tuple[str, str]]) -> str:
    out = []
    for index, (css, label) in enumerate(items):
        y = LEGEND_TOP + LEGEND_ROW * index
        out.append(f'<line class="{css}" x1="{X0}" y1="{y - 4}" x2="{X0 + 28}" y2="{y - 4}"/>'
                   f'<text x="{X0 + 34}" y="{y}">{_esc(label)}</text>')
    return "".join(out)


def draw_strategies(data: Mapping[str, object], tz: str, amount) -> Chart:
    """起動理由ごとの累積損益の階段線。期間の初めを 0 とする daemon の点をそのまま描く。

    `amount` は金額の書式 (表と同じ関数)。凡例は描いた系列だけで、終点の値を添える。
    予算を超えたら図を丸ごと説明文に置き換える。"""
    limits = series_limits(data)
    chosen, _ = series_rows(data.get("strategies"), limits[1] if limits else None)
    cap = point_cap(limits)
    drawn = [(item, _cumulative(item.row.get("cumulative"), cap)) for item in chosen]
    drawn = [(item, points) for item, points in drawn if any(p is not None for p in points)]
    if not drawn:
        return Chart("", None, (NOTE_NO_SERIES,))
    readable = [[p for p in points if p is not None] for _, points in drawn]
    scale = _strategy_scale(data, readable)
    span = (_full(scale.t_low, tz), _full(scale.t_high, tz))
    zero = scale.y(0.0)
    out = [f'<line class="ax" x1="{X0}" y1="{zero}" x2="{X1}" y2="{zero}"/>']
    legend = []
    for (item, points), line in zip(drawn, readable):
        for chunk in _chunks(points, lambda p: p is not None):
            out.append(_line(item.css, _stair(scale, chunk)))
        known = item.row.get("pnl_known_count")
        if type(known) is int and known == 0:
            end = NO_PNL
        else:
            last = _end_value(points, item.row.get("total_pnl"))
            end = NO_END if last is None else f"終点 {amount(last)}"
        legend.append((item.css, f"{item.origin} {end}"))
    height = LEGEND_TOP + LEGEND_ROW * (len(legend) - 1) + 8
    opening = (f'<svg viewBox="0 0 {VIEW_W} {height}" role="img" '
               'aria-label="戦略別の累積損益の図 (起動理由ごとの階段線)">')
    body = "".join(out) + _frame(scale, tz) + _strategy_legend(legend)
    if len((opening + body + "</svg>").encode("utf-8")) > STRATEGY_BUDGET_BYTES:
        return Chart("", span, (NOTE_STRATEGY_OVER,))
    return Chart(opening + body + "</svg>", span, ())
