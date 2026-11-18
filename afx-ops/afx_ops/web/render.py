"""DTO の許可された小さな投影だけを HTML にする。"""
from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Mapping, Sequence

from afx_ops import base_text, display_time, history_text, plugin_text, reevaluation_text
from afx_ops.display_zone import zone_of
from afx_ops.metric_words import metric_items
from afx_ops.web import marks
from afx_ops.web import tabs as tab_table
from afx_ops.web.routes import ROUTE_TABLE, resolve_route

MAX_TEXT = 512
MAX_ITEMS = 50
MAX_RESPONSE_BYTES = 64 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# 承認の変更内容の表示上限。値は daemon 側の定義 (agentic_fx/ops/change_limits.py) と同じで、
# 別 package なので写して持ち、test が突き合わせる。数えるのは html.escape 後の UTF-8 byte。
PAGE_LIMIT_BYTES = MAX_RESPONSE_BYTES
DIFF_BUDGET_BYTES = 48 * 1024
OTHER_BUDGET_BYTES = 15 * 1024
FILE_QUOTA_BYTES = 8 * 1024
VALUE_CHARS = 80
CLAIM_CHARS = 2000
PARAMS_MAX = 64
# 読み取り専用の表示なので、開いたままの画面を定期的に読み直させても状態は変わらない
REFRESH_SECONDS = 30

# 埋め込む唯一の CSS。CSP は hash (headers.STYLE_HASH) で これだけを許すので、1 byte でも
# 変えたら hash も変える。強調は値の属性ではなく renderer が付ける class で行う
STYLE = """
:root { color-scheme: light dark; --bg: #fff; --fg: #1f2328; --mut: #57606a; --line: #d0d7de;
  --soft: #f6f8fa; --link: #0969da; --ok: #116329; --ok-bg: #dafbe1; --warn: #7d4e00;
  --warn-bg: #fff8c5; --bad: #cf222e; --bad-bg: #ffebe9; }
@media (prefers-color-scheme: dark) { :root { --bg: #0d1117; --fg: #e6edf3; --mut: #9198a1;
  --line: #3d444d; --soft: #161b22; --link: #4493f8; --ok: #3fb950; --ok-bg: #12261e;
  --warn: #d29922; --warn-bg: #2a2111; --bad: #ff7b72; --bad-bg: #2d1316; } }
body { margin: 0; padding: 0 1rem 2rem; color: var(--fg); background: var(--bg);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", "Hiragino Sans", "Noto Sans JP", sans-serif;
  font-variant-numeric: tabular-nums; }
a { color: var(--link); }
nav { display: flex; flex-wrap: wrap; gap: 1.25rem; padding: 0.6rem 0;
  border-bottom: 1px solid var(--line); margin-bottom: 0.75rem; }
nav a { text-decoration: none; font-weight: 600; }
nav a:hover { text-decoration: underline; }
nav a[aria-current="page"] { color: var(--fg); }
h1 { font-size: 1.25rem; margin: 0.5rem 0 0.75rem; }
h2 { font-size: 1rem; margin: 0 0 0.4rem; }
h3, h4 { font-size: 0.9rem; margin: 0.6rem 0 0.3rem; }
section { border: 1px solid var(--line); border-radius: 6px; padding: 0.6rem 0.75rem;
  margin: 0 0 0.75rem; overflow-x: auto; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 0.15rem 1.25rem; margin: 0; }
dt { font-weight: 600; color: var(--mut); }
dd { margin: 0; overflow-wrap: anywhere; }
dd dl { padding-left: 0.5rem; border-left: 2px solid var(--line); }
ul { margin: 0; padding-left: 1.25rem; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: var(--soft); padding: 0.6rem;
  border-radius: 6px; font-size: 0.85rem; }
.danger { color: var(--bad); }
dd.danger, .st.danger { font-weight: 700; background: var(--bad-bg); }
.warn { color: var(--warn); }
dd.warn, .st.warn { font-weight: 700; background: var(--warn-bg); }
.ok { color: var(--ok); background: var(--ok-bg); }
.st { display: inline-block; padding: 0 0.4rem; border: 1px solid; border-radius: 1rem;
  font-size: 0.85rem; white-space: nowrap; }
.kpi { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0 0 0.75rem; }
.kpi div { border: 1px solid var(--line); border-radius: 6px; padding: 0.4rem 0.75rem;
  background: var(--soft); }
.kpi strong { display: block; font-size: 0.8rem; color: var(--mut); }
table { border-collapse: collapse; font-size: 0.85rem; }
th, td { border: 1px solid var(--line); padding: 0.15rem 0.45rem; text-align: left; }
th { background: var(--soft); }
tbody tr:nth-child(even) { background: var(--soft); }
.tabs a { padding-bottom: 0.25rem; border-bottom: 2px solid transparent; }
.tabs a[aria-current="page"] { border-bottom-color: var(--link); }
.scroll { max-height: 70vh; overflow: auto; }
.scroll th { position: sticky; top: 0; }
form { margin: 0.5rem 0; }
form + form { margin-top: 1.5rem; }
button, input { font: inherit; padding: 0.25rem 0.6rem; }
"""

# nav に出す静的な route (表示順)。/approvals は /plugins から辿り、詳細は一覧の各項目から辿る
_NAV_LABELS = {"/": "Status", "/plugins": "Plugins", "/improve": "Improve", "/logs": "Log", "/activity": "Activity"}
_STATIC_ROUTES = frozenset(route.path for route in ROUTE_TABLE if "{" not in route.path)
if not set(_NAV_LABELS) <= _STATIC_ROUTES:
    raise RuntimeError("nav label without a route")


def _nav(current: str | None) -> str:
    """上部の nav。`current` は今の画面が属する項目の path。"""
    return "<nav>" + "".join(
        f'<a href="{path}"' + (' aria-current="page"' if path == current else "") + f">{label}</a>"
        for path, label in _NAV_LABELS.items()) + "</nav>"

# 真のとき強調する status の field と class
_EMPHASIS = {"kill_switch": "danger", "autopilot": "warn"}

# server DTO の新 field を表示面へ自動的に広げないための allowlist。
STATUS_FIELDS = frozenset({"mode", "autopilot", "kill_switch", "equity", "balance", "orders",
                           "mission", "health", "data"})
# 承認待ちの表の列。表示してよい項目の唯一の定義 (列の並びがそのまま表の並び)
_APPROVAL_COLUMNS = ("id", "kind", "name", "created_at", "content_hash")
APPROVAL_FIELDS = frozenset(_APPROVAL_COLUMNS)
JOURNAL_FIELDS = frozenset({"approval_id", "name", "phase"})
DETAIL_FIELDS = frozenset({"id", "kind", "status", "in_sample", "holdout", "payload_sha256",
                           "decided_by", "decided_at", "asserted_actor", "parent_facts", "baseline"})
# 変更内容の節を出すときは、成績を専用の節に移す
OVERVIEW_FIELDS = DETAIL_FIELDS - {"in_sample", "holdout"}
ACTIVITY_FIELDS = frozenset({"id", "ts", "category", "code", "ref", "state", "result_code",
                             "component_code", "health_code", "job_kind", "guard_kind", "generation"})
NESTED_FIELDS = frozenset({"latched", "generation", "latched_at", "reconcile_required",
                           "requested_generation", "started_at", "file_latched", "active", "loop",
                           "status", "failures", "state", "trades", "pf", "avg_r", "max_drawdown",
                           "win_rate", "total_pnl", "evaluable", "id", "attempts", "n",
                           "content_hash8", "same_hash_as_submitted", "analysis_call_count",
                           "trials_omitted", "run_id", "result", "approval_id"})


def _text(value: object) -> str:
    if type(value) is str:
        value = _CONTROL.sub(" ", value).strip()
        return html.escape(value[:MAX_TEXT], quote=True)
    if type(value) in (int, float, bool):
        return html.escape(str(value), quote=True)
    return ""


def _is_on(value: object) -> bool:
    if value is True:
        return True
    return type(value) is dict and (value.get("latched") is True or value.get("active") is True)


def _time_text(value: object, tz: str) -> str:
    """時刻 field の文字列を表示 tz に換算して escape する。換算できなければ原文 (UTC で再試行)。"""
    return _text(display_time.convert_or_raw(value, tz, display_time.PRECISION_SECOND,
                                             raw=value, utc_fallback=True))


def _pairs(data: object, allowed: frozenset[str], *, nested_allowed: frozenset[str] = frozenset(),
           emphasis: Mapping[str, str] | None = None, tz: str = "UTC",
           order: Sequence[str] | None = None) -> str:
    """allowlist の key を dl にする。`order` を渡すとその順 (既定は key のアルファベット順)。"""
    if type(data) is not dict:
        return ""
    rows = []
    for key in (sorted(set(data) & allowed) if order is None
                else [key for key in order if key in data and key in allowed]):
        value = data[key]
        shown = _value(value, nested_allowed, tz, key)
        if shown:
            mark = (emphasis or {}).get(key) if _is_on(value) else None
            attr = f' class="{mark}"' if mark else ""
            rows.append(f"<dt{attr}>{html.escape(key, quote=True)}</dt><dd{attr}>{shown}</dd>")
    return "<dl>" + "".join(rows) + "</dl>" if rows else ""


def _value(value: object, nested_allowed: frozenset[str], tz: str = "UTC",
           key: str | None = None) -> str:
    if key in display_time.TIME_FIELDS and type(value) is str:
        scalar = _time_text(value, tz)
    else:
        scalar = _text(value)
    if scalar:
        return scalar
    if type(value) is dict and nested_allowed:
        return _pairs(value, nested_allowed, tz=tz)
    if isinstance(value, list):
        values = [_value(item, nested_allowed, tz, key) for item in value[:MAX_ITEMS]]
        return "<ul>" + "".join(f"<li>{item}</li>" for item in values if item) + "</ul>"
    return ""


def _zone_line(tz: str | None) -> str:
    """全ページの上部に出す、換算に使った tz。tz を持たない面 (error) は出さない。"""
    if tz is None:
        return ""
    return "<p>" + html.escape(display_time.display_header(tz), quote=True) + "</p>"


def _document(title: str, body: str, tz: str | None = None, *, refresh: bool = True,
              current: str | None = None, tabs: str = "") -> bytes:
    reload = f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">' if refresh else ""
    return ("<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            + reload + '<title>'
            + html.escape(title, quote=True) + "</title><style>" + STYLE + "</style></head><body>"
            + _nav(current) + "<h1>" + html.escape(title, quote=True) + "</h1>" + tabs
            + _zone_line(tz) + body
            + "</body></html>").encode("utf-8")


def _page(title: str, body: str, tz: str | None = None, *, current: str | None = None,
          tabs: str = "") -> bytes:
    document = _document(title, body, tz, current=current, tabs=tabs)
    if len(document) <= MAX_RESPONSE_BYTES:
        return document
    return _page(title, "<p>Output clipped</p>", tz, current=current, tabs=tabs)


# Status の概要の群と、群の中の並び。allowlist (STATUS_FIELDS) と同じ集合を並べる
STATUS_GROUPS = (
    ("安全", ("kill_switch", "health", "autopilot")),
    ("資金", ("equity", "balance")),
    ("稼働", ("mode", "mission", "data", "orders")),
)


def _scalar_card(data: Mapping[str, object], key: str) -> tuple[str, str]:
    """印を持たない値だけのカード。画面が今も出している scalar の値をそのまま出す。"""
    return key, _text(data.get(key)) if key in data else ""


def _status_cards(data: Mapping[str, object]) -> tuple[str, set[str]]:
    """Status 概要のカードと、カードが同じ内容を出していて dl の行を省いてよい key。

    分類は 1 回で、カードと「行を省く」判定の両方に使う。異常・不明・型違いは省かない。
    """
    covered: set[str] = set()
    items: list[tuple[str, str]] = []

    def marked(key: str, classify, label: str, *, hide_when_ok: bool) -> None:
        if key not in data:
            return
        verdict = classify(data[key])
        items.append((label, marks.render_verdict(verdict)))
        if hide_when_ok and verdict[0] == marks.OK:
            covered.add(key)

    marked("kill_switch", marks.classify_kill_switch, "kill switch", hide_when_ok=True)
    marked("health", marks.classify_health, "health", hide_when_ok=True)
    marked("autopilot", marks.classify_autopilot, "autopilot", hide_when_ok=False)
    if type(data.get("autopilot")) is bool:
        covered.add("autopilot")
    for key in ("mode", "equity", "balance"):
        label, value = _scalar_card(data, key)
        items.append((label, value))
        if value:
            covered.add(key)
    marked("data", marks.classify_data, "data", hide_when_ok=False)
    return marks.cards(items), covered


def _status_overview(data: Mapping[str, object], tz: str) -> str:
    cards, covered = _status_cards(data)
    parts = [cards]
    for title, keys in STATUS_GROUPS:
        rows = _pairs(data, STATUS_FIELDS, nested_allowed=NESTED_FIELDS, emphasis=_EMPHASIS,
                      tz=tz, order=[key for key in keys if key not in covered])
        if rows:
            parts.append(f"<section><h2>{title}</h2>{rows}</section>")
    return "".join(parts)


# Status のタブごとの本文。キーは tabs.TABS["/"] の名前と一致させる
STATUS_TAB_SECTIONS = {
    "overview": lambda data, tz: _status_overview(data, tz),
    "bot": lambda data, tz: _bot_cards(data) + _bot_section(data, tz),
    "econ": lambda data, tz: _econ_cards(data) + _calendar_section(data) + _econ_section(data, tz),
    "reaction": lambda data, tz: _reaction_section(data),
}


def render_status(data: Mapping[str, object], tab: str = "overview") -> bytes:
    tz = zone_of(data)
    body = STATUS_TAB_SECTIONS[tab](data, tz) or "<p>なし</p>"
    return _page("Status", body, tz, current="/", tabs=tab_table.bar("/", tab))


# daemon の `bot_health` の語 (agentic_fx.ops.bot_health の写し。test が突き合わせる)。web は daemon API
# だけを読み、bot の state dir には触れない
BOT_HEALTH_STATES = tuple(marks.BOT_HEALTH)
BOT_ALTERNATE_PUSH = ("none", "configured")
_BOT_CODE = re.compile(r"[a-z_]{1,48}\Z")
INTERACTION_UNAVAILABLE_TEXT = "gateway が健全でない間、Discord の承認操作は使えません"
ALTERNATE_NONE_TEXT = "alternate_push=none (別経路の通知が設定されていません)"


def _bot_cards(data: Mapping[str, object]) -> str:
    health = data.get("bot_health")
    if type(health) is not dict:
        return ""
    return marks.cards([("gateway health", marks.render_verdict(
        marks.classify_word(marks.BOT_HEALTH, health.get("state"))))])


def _bot_section(data: Mapping[str, object], tz: str = "UTC") -> str:
    health = data.get("bot_health")
    if type(health) is not dict:
        return ""
    state = _word(health.get("state"), BOT_HEALTH_STATES)
    if state == "-":
        state = "unknown"
    pairs = [("gateway health", state)]
    gateway, delivery = health.get("gateway_state"), health.get("delivery_state")
    if gateway in ("starting", "healthy", "unhealthy"):
        pairs.append(("gateway", gateway))
    if delivery in ("idle", "healthy", "stalled"):
        pairs.append(("delivery", delivery))
    code = health.get("terminal_code")
    if type(code) is str and _BOT_CODE.fullmatch(code):
        pairs.append(("terminal", html.escape(code, quote=True)))
    observed = health.get("observed_at")
    if type(observed) is str:
        pairs.append(("observed_at", _time_text(observed, tz)))
    push = _word(health.get("alternate_push"), BOT_ALTERNATE_PUSH)
    pairs.append(("alternate_push", push if push != "-" else "none"))
    notes = ""
    if state != "healthy":
        notes += f"<p>{INTERACTION_UNAVAILABLE_TEXT}</p>"
    if push != "configured" and state != "not_configured":
        notes += f'<p class="warn">{html.escape(ALTERNATE_NONE_TEXT, quote=True)}</p>'
    return "<section><h2>Bot</h2>" + _rows(pairs) + notes + "</section>"


# daemon 側 (agentic_fx.loops.econ_signal_report) の定義の写し。test が突き合わせる。
ECON_ABANDON_REASONS = ("freshness_expired", "requeued_expired", "retry_exhausted",
                        "source_inactive", "normalization_failed", "deploy_op",
                        "deploy_linked")
_CALENDAR_STATES = tuple(marks.CALENDAR)
_WORKER_STATES = tuple(marks.WORKER)
_LEVELS = tuple(marks.CAPACITY)
_MISSION_KEYS = ("system_filtered", "failed", "running")
_CAPACITY_KEYS = ("signal_daily", "signal_producer", "signal_override", "rows",
                  "row_capacity", "row_level", "slice_bytes", "slice_bytes_measured_at",
                  "slice_bytes_stale", "measure_outcome", "measure_elapsed_sec",
                  "used_bytes", "byte_capacity", "byte_level", "base_bytes",
                  "bytes_per_row", "db_bytes", "wal_bytes", "free_bytes", "level")
# used_bytes の内訳で、異常の合図にならない行
_CAPACITY_BREAKDOWN = frozenset({"base_bytes", "bytes_per_row", "db_bytes", "wal_bytes"})
_MEASURE_OUTCOMES = ("ok", "aborted", "failed")
_OTHER_KIND = "other"
_ECON_WORD = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")


def _word(value: object, allowed: Sequence[str]) -> str:
    return html.escape(value, quote=True) if type(value) is str and value in allowed else "-"


def _dict(value: object) -> dict:
    return value if type(value) is dict else {}


def _econ_cards(data: Mapping[str, object]) -> str:
    window, econ = data.get("window"), data.get("econ_signal")
    items = []
    if type(window) is dict:
        items.append(("calendar", marks.render_verdict(
            marks.classify_word(marks.CALENDAR, window.get("freshness")))))
    if type(econ) is dict:
        items.append(("worker", marks.render_verdict(
            marks.classify_word(marks.WORKER, econ.get("slice_worker")))))
        capacity = econ.get("slice_capacity")
        if type(capacity) is dict:
            items.append(("slice 容量", marks.render_verdict(
                marks.classify_word(marks.CAPACITY, capacity.get("level")))))
    return marks.cards(items)


def _calendar_section(data: Mapping[str, object]) -> str:
    if type(data.get("window")) is not dict:
        return ""
    window = data["window"]
    state = _word(window.get("freshness"), _CALENDAR_STATES)
    pairs = [("calendar", state if state != "-" else "unknown")]
    if "in_window" in window:
        pairs.append(("in_window", _text(window.get("in_window")) or "-"))
    return "<section><h2>Economic calendar</h2>" + _rows(pairs) + "</section>"


def _is_zero(*values: object) -> bool:
    """全部が整数の 0 のときだけ真。欠損・None・bool・文字列・負は 0 と読まない。"""
    return all(type(value) is int and value == 0 for value in values)


def _count_text(value: object) -> str:
    return _num(value) if type(value) is int else "-"


def _econ_section(data: Mapping[str, object], tz: str = "UTC") -> str:
    econ = data.get("econ_signal")
    if type(econ) is not dict:
        return ""
    pairs: list[tuple[str, str]] = [("worker", _word(econ.get("slice_worker"), _WORKER_STATES))]
    decisions = econ.get("decisions")
    if type(decisions) is dict:
        pairs.append(("decisions", _count_text(decisions.get("total"))))
        by_kind = _dict(decisions.get("by_kind"))
        kinds = sorted(key for key in by_kind if type(key) is str)
        for key in kinds[:MAX_ITEMS]:
            if key == _OTHER_KIND:
                pairs.append(("decision その他", _count_text(by_kind[key])))
            elif _ECON_WORD.match(key):
                pairs.append((f"decision {html.escape(key, quote=True)}", _count_text(by_kind[key])))
        if len(kinds) > MAX_ITEMS:
            pairs.append(("decision 省略", f"{len(kinds) - MAX_ITEMS} 種"))
    else:
        pairs.append(("decisions", "unavailable"))
    abandoned = econ.get("abandoned")
    if type(abandoned) is dict:
        total, day = _dict(abandoned.get("total")), _dict(abandoned.get("latest_day"))
        # 0 と読めた行は出さない。欠損・型違い (「-」)・0 でない値の行は必ず出す
        for reason in ECON_ABANDON_REASONS:
            if not _is_zero(day.get(reason), total.get(reason)):
                pairs.append((f"abandoned {reason} (latest day / total)",
                              f"{_count_text(day.get(reason))} / {_count_text(total.get(reason))}"))
        cleanup = _dict(abandoned.get("cleanup"))
        if not _is_zero(cleanup.get("latest_day"), cleanup.get("total")):
            pairs.append(("abandoned ineligible_cleanup (latest day / total)",
                          f"{_count_text(cleanup.get('latest_day'))} / {_count_text(cleanup.get('total'))}"))
        if not _is_zero(abandoned.get("legacy_unknown_total")):
            pairs.append(("abandoned legacy_unknown (total)",
                          _count_text(abandoned.get("legacy_unknown_total"))))
    else:
        pairs.append(("abandoned", "unavailable"))
    missions = econ.get("missions_latest_day")
    if type(missions) is dict:
        for key in _MISSION_KEYS:
            pairs.append((f"missions latest day {key}", _count_text(missions.get(key))
                          + (" (filtered, not failed)" if key == "system_filtered" else "")))
    shadow = econ.get("shadow")
    if type(shadow) is list:
        for row in shadow[:MAX_ITEMS]:
            row = _dict(row)
            label = "/".join(_text(row.get(k)) or "-" for k in
                             ("decision", "actor", "reject_code", "state", "result_code"))
            pairs.append((f"shadow {label}",
                          f"n={_count_text(row.get('n'))} avg_pnl_pips={_num(row.get('avg_pnl_pips'))}"))
        omitted = (econ.get("shadow_omitted") if type(econ.get("shadow_omitted")) is int
                   else 0) + max(0, len(shadow) - MAX_ITEMS)
        if omitted > 0:
            pairs.append(("shadow 省略", f"{omitted} 組"))
    capacity = econ.get("slice_capacity")
    if type(capacity) is dict:
        for key in _CAPACITY_KEYS:
            if key in _CAPACITY_BREAKDOWN:
                continue
            value = capacity.get(key)
            if key.endswith("_level") or key == "level":
                shown = _word(value, _LEVELS)
            elif key == "slice_bytes_measured_at":
                shown = (_time_text(value, tz) if type(value) is str else _text(value)) or "-"
            elif key == "slice_bytes_stale":
                shown = "古い" if value is True else ("-" if value is None else "新しい")
            elif key == "measure_outcome":
                shown = _word(value, _MEASURE_OUTCOMES)
            elif key == "measure_elapsed_sec":
                shown = _num(value) if type(value) in (int, float) else "-"
            else:
                shown = _count_text(value)
            if key == "slice_bytes" and value is None:
                shown = "未測定"
            pairs.append((f"slice {key}", shown))
        below = capacity.get("capacity_override_below_producer")
        pairs.append(("slice capacity_override_below_producer", _text(below) or "-"))
        if capacity.get("level") in ("warn", "full"):
            pairs.append(("slice capacity warning", _word(capacity.get("level"), _LEVELS)))
    else:
        pairs.append(("slice capacity", "unavailable"))
    return "<section><h2>Economic signal</h2>" + _rows(pairs) + "</section>"


# 反応統計の表。窓は daemon の定義 (分) の写し、床の既定も同じ。
_REACTION_WINDOWS = ((15, "15m"), (60, "1h"), (240, "4h"))
_REACTION_KINDS = (("importance", "importance 別"), ("name", "name 別"))
_REACTION_FLOOR = 20
# page 全体の上限 (MAX_RESPONSE_BYTES) に収めるため、表ごとの行数に上限を置く
_REACTION_TABLE_ROWS = {"importance": 24, "name": 24}
_REACTION_HEAD = ("通貨", "{kind}", "n", "unique_ts_n", "未算定",
                  "range 中央値 / p75", "abs 中央値 / p75", "net 中央値 / p75",
                  "close_change 中央値", "印")


def _pair_cell(median: object, p75: object) -> str:
    return f"{_num(median)} / {_num(p75)}"


def _reaction_cells(row: dict, floor: int) -> list[str]:
    n = row.get("n")
    mark = f"n<{floor}" if type(n) is not int or n < floor else ""
    return [_text(row.get("currency")), _text(row.get("key")), _count_text(n),
            _count_text(row.get("unique_ts_n")), _count_text(row.get("unresolvable_n")),
            _pair_cell(row.get("range_median"), row.get("range_p75")),
            _pair_cell(row.get("abs_median"), row.get("abs_p75")),
            _pair_cell(row.get("net_median"), row.get("net_p75")),
            _num(row.get("close_change_median")), html.escape(mark, quote=True)]


def _reaction_table(rows: list[dict], kind: str, floor: int) -> tuple[str, int]:
    limit = _REACTION_TABLE_ROWS[kind]
    head = "".join(f"<th>{item.format(kind=kind)}</th>" for item in _REACTION_HEAD)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in _reaction_cells(row, floor))
                   + "</tr>" for row in rows[:limit])
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>", max(0, len(rows) - limit)


def _reaction_section(data: Mapping[str, object]) -> str:
    econ = data.get("econ_signal")
    if type(econ) is not dict or "reaction" not in econ:
        return ""
    reaction = econ["reaction"]
    title = "<h2>Economic reactions</h2>"
    if type(reaction) is not dict or type(reaction.get("bundles")) is not list:
        return "<section>" + title + _rows([("reaction", "unavailable")]) + "</section>"
    floor = reaction.get("floor") if type(reaction.get("floor")) is int else _REACTION_FLOOR
    bundles = [row for row in reaction["bundles"] if type(row) is dict]
    parts = [title, f"<p>数値は pips (窓は指標時刻から)。n&lt;{floor} の行は参考値。"
                    "中央値 / p75 の順。</p>"]
    omitted = (reaction.get("bundles_omitted") if type(reaction.get("bundles_omitted")) is int
               else 0)
    for kind, label in _REACTION_KINDS:
        parts.append(f"<h3>{label}</h3>")
        for window, window_label in _REACTION_WINDOWS:
            rows = [row for row in bundles
                    if row.get("kind") == kind and row.get("window_min") == window]
            table, cut = _reaction_table(rows, kind, floor)
            omitted += cut
            parts.append(f"<h4>{window_label}</h4>" + table)
    pairs: list[tuple[str, str]] = []
    if omitted > 0:
        pairs.append(("reaction 省略", f"{omitted} 行"))
    backlog = _dict(reaction.get("backlog"))
    pairs.append(("reaction sets_without_values", _count_text(backlog.get("sets_without_values"))))
    pairs.append(("reaction events_without_currency_key",
                  _count_text(backlog.get("events_without_currency_key"))))
    return "<section>" + "".join(parts) + _rows(pairs) + "</section>"


def _detail_link(item: dict) -> str:
    """id が詳細 route に載る正の整数のときだけ、その詳細への link を付ける。"""
    approval_id = item.get("id")
    if type(approval_id) is not int:
        return ""
    path = f"/approvals/{approval_id}"
    if resolve_route(path) is None:
        return ""
    return f'<p><a href="{path}">Details #{approval_id}</a></p>'


def _approval_row(item: Mapping[str, object], tz: str) -> str:
    """承認待ちの 1 行。欠けた値・型違いは「-」。hash は先頭 12 文字。"""
    approval_id = item.get("id")
    link = _detail_link(item)
    if link:
        first = f'<a href="/approvals/{approval_id}">#{approval_id}</a>'
    else:
        first = _text(approval_id) or "-"
    created = item.get("created_at")
    cells = [first, _text(item.get("kind")) or "-", _text(item.get("name")) or "-",
             _time_text(created, tz) if type(created) is str and created else "-",
             _hash12(item.get("content_hash"))]
    return "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"


def render_approvals(data: Mapping[str, object]) -> bytes:
    approvals = data.get("approvals")
    entries = approvals if isinstance(approvals, list) else []
    tz = zone_of(data)
    rows = [_approval_row(item, tz) for item in entries[:MAX_ITEMS] if type(item) is dict]
    body = ("<section><table><thead><tr>" + "".join(f"<th>{name}</th>" for name in _APPROVAL_COLUMNS)
            + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></section>") if rows else ""
    journals = data.get("open_journals")
    if isinstance(journals, list):
        body += "".join("<section>" + _pairs(item, JOURNAL_FIELDS, tz=tz) + "</section>"
                        for item in journals[:MAX_ITEMS] if type(item) is dict)
    return _page("Pending approvals", body, tz, current="/plugins",
                 tabs=tab_table.bar("/plugins", "approvals"))


def _change_is_well_formed(change: Mapping[str, object]) -> bool:
    """daemon の応答が約束の型か。外れた応答 (版の食い違い・不具合) はページごと固定文に倒す。"""
    classification, reason = change.get("classification"), change.get("reason")
    if type(classification) is not str or (reason is not None and type(reason) is not str):
        return False
    params = change.get("params")
    if params is None:
        return True
    return type(params) is dict and type(params.get("items", [])) is list


def _baseline_section(data: Mapping[str, object]) -> str:
    baseline = data.get("baseline")
    if type(baseline) is not dict or type(baseline.get("comparison")) is not dict:
        return ""
    comparison = baseline["comparison"]
    stages = comparison.get("stages")
    if type(stages) is not dict:
        return ""
    verdict = comparison.get("verdict")
    if verdict == "not_improved" and comparison.get("mode") == "shadow":
        label = "未改善 (観測のみ)"
    elif verdict == "unavailable":
        label = "比較不能 (" + _text(comparison.get("reason")) + ")"
    elif verdict == "improved":
        label = "改善"
    else:
        label = _text(verdict)
    rows = ["<p>判定: " + label + "</p>"]
    value = baseline.get("base_content_hash")
    if type(value) is str:
        rows.append("<p>base_content_hash: " + _text(value[:12]) + "</p>")
    warning = data.get("baseline_hash_warning")
    if type(warning) is dict and type(warning.get("expected")) is str and type(warning.get("actual")) is str:
        rows.append("<p>警告: 配備中の版の hash が比較時と異なります (比較時="
                    + _text(warning["expected"]) + " 現在=" + _text(warning["actual"]) + ")</p>")
    for stage in ("in_sample", "holdout"):
        pairs = stages.get(stage)
        if type(pairs) is not dict:
            continue
        rows.append("<h3>" + html.escape(stage) + "</h3><table><thead><tr>"
                    "<th>pair</th><th>version</th><th>pf</th><th>trades</th>"
                    "<th>win_rate</th><th>avg_r</th><th>max_drawdown</th>"
                    "<th>total_pnl</th><th>kill_switch_latches</th><th>evaluable</th>"
                    "<th>verdict</th></tr></thead><tbody>")
        for pair, entry in list(pairs.items())[:MAX_ITEMS]:
            if type(entry) is not dict:
                continue
            for version, label_ in (("candidate", "候補"), ("deployed", "配備中の版")):
                metrics = entry.get(version)
                if type(metrics) is not dict:
                    continue
                cells = "".join("<td>" + _text(metrics.get(key)) + "</td>" for key in
                                ("pf", "trades", "win_rate", "avg_r", "max_drawdown",
                                 "total_pnl", "kill_switch_latches", "evaluable"))
                rows.append("<tr><td>" + _text(pair) + "</td><td>" + label_ + "</td>" + cells
                            + "<td>" + _text(entry.get("verdict")) + "</td></tr>")
        rows.append("</tbody></table>")
    return "<section><h2>配備中の版 (同条件で再評価)</h2>" + "".join(rows) + "</section>"


# indicator gate の警告は daemon が描画した行 (``gate_lines``) をそのまま表示する。
# 語・文の表はここに持たない。
MAX_GATE_LINES = 16


def _gate_section(data: Mapping[str, object]) -> str:
    lines = data.get("gate_lines")
    if type(lines) is not list:
        return ""
    items = [text for text in (_text(line) for line in lines[:MAX_GATE_LINES] if type(line) is str) if text]
    if not items:
        return ""
    return ("<section><h2>indicator gate の警告</h2><ul>"
            + "".join("<li>" + text + "</li>" for text in items)
            + "</ul></section>")


def _reevaluation_section(data: Mapping[str, object]) -> str:
    """再評価の依頼の節 (前回 → 今回の比較)。再評価でない承認・古い daemon の応答では出さない。"""
    tz = zone_of(data)
    described = reevaluation_text.describe(
        data, lambda value: _time_text(value, tz))
    if described is None:
        return ""
    notes = [f"<p>{note}</p>" if note.startswith("評価時点") else "<p>" + _text(note) + "</p>"
             for note in described.notes]
    name = reevaluation_text.safe_name((data.get("reevaluation") or {}).get("name")
                                       if type(data.get("reevaluation")) is dict else None)
    if data.get("status") == "rejected" and name is not None:
        outcome = reevaluation_text.reject_text(name, data.get("deploy_phase"))
        if outcome is not None:
            notes.append("<p>" + _text(outcome) + "</p>")
    rows = "".join("<tr><td>" + _text(label) + "</td><td>" + _text(text) + "</td></tr>"
                   for label, text in described.rows)
    table = ("<table><thead><tr><th>pair / 区間</th><th>前回 → 今回</th></tr></thead><tbody>"
             + rows + "</tbody></table>") if rows else ""
    return "<section><h2>" + _text(described.heading) + "</h2>" + "".join(notes) + table + "</section>"


def _history_section(data: Mapping[str, object]) -> str:
    """履歴証跡で止まった依頼 (固定 reason) の節。理由と次の操作を出す。"""
    text = history_text.text_for(data.get("reason"))
    return "" if text is None else "<section><h2>履歴証跡</h2><p>" + _text(text) + "</p></section>"


def _base_section(data: Mapping[str, object]) -> str:
    """土台の節。比較成績とは独立で、警告が要る状態 (不一致・土台なし・読めない) のときだけ出す。"""
    line = base_text.warning_line(data.get("base_check"))
    return "" if line is None else "<section><h2>土台</h2><p>" + _text(line) + "</p></section>"


# 追加節の builder と、それを出すタブ。省くときは先頭の節から
_EXTRA_BUILDERS = (("result", _baseline_section), ("overview", _gate_section),
                   ("overview", _reevaluation_section), ("overview", _base_section),
                   ("overview", _history_section))


def _tagged_extras(data: Mapping[str, object]) -> list[tuple[str, str]]:
    """承認ページ末尾の節を (タブ, html) で。重要でないものが先 (収まらないときは先頭から省く)。"""
    tagged = [(tab, build(data)) for tab, build in _EXTRA_BUILDERS]
    return [(tab, section) for tab, section in tagged if section]


def _extra_sections(data: Mapping[str, object]) -> list[str]:
    return [section for _tab, section in _tagged_extras(data)]


class ApprovalView:
    """承認詳細 1 タブの描画結果。``block`` は概要で form の前に足す決定のブロック (足さないなら空)。"""

    __slots__ = ("page", "tab", "clipped", "block")

    def __init__(self, page: bytes, tab: str, clipped: bool, block: str) -> None:
        self.page = page
        self.tab = tab
        self.clipped = clipped
        self.block = block


def _approval_at(data: Mapping[str, object], approval_id: int | None) -> str:
    number = approval_id if approval_id is not None else data.get("id")
    return f"/approvals/{number}" if type(number) is int and number >= 1 else "/approvals"


def render_approval_view(data: Mapping[str, object], change_data: Mapping[str, object] | None = None,
                         tab: str = "overview", approval_id: int | None = None) -> ApprovalView:
    """承認 #N の 1 タブ。``change_data`` は daemon の変更内容 endpoint の応答 (取れなければ None)。

    どのタブも同じ仮想の 1 ページ (全タブの節) を 1 回計算し、その結果のうち自分の節だけを出す。
    """
    change = data.get("change") if type(data.get("change")) is dict else None
    if type(change_data) is dict and type(change_data.get("change")) is dict:
        change = change_data["change"]
    at = _approval_at(data, approval_id)
    tagged = _tagged_extras(data)
    if change is None:
        virtual = _unchanged_virtual(data, REASON_TEXT["unavailable"], tagged, at)
    elif not _change_is_well_formed(change):
        virtual = _unchanged_virtual(data, INVALID_CHANGE_TEXT, tagged, at)
    else:
        virtual = compose_approval_page(data, change, change_data if type(change_data) is dict else None,
                                        extra_sections=tagged, approval_id=approval_id)
    return virtual.view(data, tab, at)


def render_approval(data: Mapping[str, object], change_data: Mapping[str, object] | None = None,
                    tab: str = "overview", approval_id: int | None = None) -> bytes:
    return render_approval_view(data, change_data, tab, approval_id).page


# ---------------------------------------------------------------- 承認の変更内容

CLASS_LABELS = {"new": "新規", "updated": "更新", "same": "同一版", "unknown": "不明"}
# 表示面はこの文だけを出し、自由文を足さない (daemon の定義と同じ)
REASON_TEXT = {
    "unavailable": "変更内容を取得できません",
    "hash_mismatch": "内容を検証できません",
    "too_large": "内容を検証できません",
    "unsafe_source": "内容を検証できません",
    "redacted": "機微情報の可能性があるためローカルで確認してください",
}
INVALID_CHANGE_TEXT = "変更内容を表示できません"
DIFF_DEGRADED_TEXT = "差分の計算を省略 (行数が多いため)"
_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_DIFF_FILES = ("plugin.py", "config.yaml", "test_plugin.py")
_REMOVED = frozenset({"Cc", "Cf", "Cs"})
_MARK = "…"


def _omitted_mark(count: int) -> str:
    return f"…({count} 行省略)"


def _clean_inline(value: object) -> str:
    """1 行の値: 改行・タブは空白にし、Cc / Cf / Cs を除いて空白を畳む。"""
    if type(value) is not str:
        return ""
    text = value.translate({ord("\r"): " ", ord("\n"): " ", ord("\t"): " "})
    text = "".join(ch for ch in text if unicodedata.category(ch) not in _REMOVED)
    return " ".join(text.split())


def _clean_block(value: object) -> str:
    """複数行の本文: 改行だけを正規化して残し、Cc / Cf / Cs を除く。"""
    if type(value) is not str:
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    return "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch) not in _REMOVED)


def _clean_line(value: object) -> str:
    text = _clean_block(value)
    return text.replace("\n", " ")


def _cut(value: object, limit: int, *, block: bool = False) -> str:
    """除去 → escape → 表示上限の順。上限は escape 後の文字数で、escape の単位 (``&amp;`` など
    1 記号分) の途中では切らない。切ったときは末尾に「…」を付ける。"""
    text = _clean_block(value) if block else _clean_inline(value)
    out: list[str] = []
    used = 0
    for ch in text:
        piece = html.escape(ch, quote=True)
        if used + len(piece) > limit:
            return "".join(out) + _MARK
        out.append(piece)
        used += len(piece)
    return "".join(out)


def _hash12(value: object) -> str:
    return value[:12] if type(value) is str and _HASH.match(value) else "-"


def _num(value: object) -> str:
    if type(value) in (int, float) and value == value and abs(value) != float("inf"):
        return html.escape(str(value), quote=True)
    return "-"


def _rows(pairs: list[tuple[str, str]]) -> str:
    return "<dl>" + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in pairs) + "</dl>"


def _computed_at(value: object, tz: str) -> str:
    """計算時刻の手書きの行。表示 tz に換算し、換算できなければ従来どおりの原文 (40 文字まで)。"""
    if type(value) is not str:
        return _cut(value, 40)
    return _cut(display_time.convert_or_raw(value, tz, display_time.PRECISION_SECOND,
                                            raw=value, utc_fallback=True), 40)


# 承認詳細の概要の群と群の中の並び。allowlist (DETAIL_FIELDS / OVERVIEW_FIELDS) と同じ集合を並べる。
# 「対象」の群は change 由来の手書きの行で、allowlist の key を持たない
APPROVAL_GROUPS = (
    ("承認", ("id", "kind", "status", "decided_by", "decided_at")),
    ("内部値", ("payload_sha256", "baseline", "parent_facts", "asserted_actor", "in_sample", "holdout")),
)
TARGET_GROUP = "対象"


def _approval_groups(data: Mapping[str, object], tz: str, *, parent: bool = True,
                     target: str = "", metrics: bool = True) -> str:
    """承認の dl を群の小見出しに分けて並べる。値の無い行は出さず、行の無い群は小見出しも出さない。

    ``target`` は「対象」の群の本文 (変更内容が無いときは空)。``metrics`` が偽なら成績の欄を外す。
    """
    blocked = set()
    if not parent:
        blocked.add("parent_facts")
    if not metrics:
        blocked |= {"in_sample", "holdout"}
    parts = []
    for title, keys in APPROVAL_GROUPS:
        if title == "内部値" and target:
            parts.append(f"<h3>{TARGET_GROUP}</h3>{target}")
            target = ""
        rows = _pairs(data, DETAIL_FIELDS, nested_allowed=NESTED_FIELDS, tz=tz,
                      order=[key for key in keys if key not in blocked])
        if rows:
            parts.append(f"<h3>{title}</h3>{rows}")
    if target:
        parts.append(f"<h3>{TARGET_GROUP}</h3>{target}")
    return "".join(parts)


def _approval_cards(data: Mapping[str, object], name: str | None = None,
                    classification: str | None = None) -> str:
    """承認詳細の概要の先頭のカード。成績の判定は出さない (承認・却下の form と同じタブなので)。"""
    level, word = marks.classify_word(marks.APPROVAL_STATUS, data.get("status"))
    items = [("status", marks.mark(level, plugin_text.APPROVAL_STATUS[word]
                                   if level != marks.UNKNOWN else word))]
    if name is not None and name != "-":
        items.append(("plugin", html.escape(name)))
    if classification is not None:
        items.append(("分類", classification))
    return marks.cards(items)


def _overview_section(data: Mapping[str, object], change: Mapping[str, object], parent: bool,
                      tz: str = "UTC") -> str:
    name = change.get("name")
    name = name if type(name) is str and _NAME.match(name) else "-"
    kind = change.get("kind") if change.get("kind") in ("strategy", "indicator", "signal") else "-"
    label = CLASS_LABELS.get(change.get("classification"), CLASS_LABELS["unknown"])
    pairs = [("plugin", html.escape(name)), ("kind", kind), ("分類", label),
             ("候補", _hash12(change.get("candidate_hash"))),
             ("比較対象", _hash12(change.get("baseline_hash"))),
             ("計算時刻", _computed_at(change.get("computed_at"), tz))]
    target = _rows(pairs)
    if change.get("classification") == "new":
        target += "<p>前版なし</p>"
    reason = change.get("reason")
    if reason is not None:
        target += "<p>" + REASON_TEXT.get(reason, REASON_TEXT["unsafe_source"]) + "</p>"
    head = _approval_cards(data, name, label)
    return ("<section><h2>概要</h2>" + head
            + _approval_groups(data, tz, parent=parent, target=target, metrics=False) + "</section>")


def _params_rows(change: Mapping[str, object]) -> tuple[list[str], str]:
    params = change.get("params") if type(change.get("params")) is dict else {}
    rows = []
    items = params.get("items") if type(params.get("items")) is list else []
    for item in items[:PARAMS_MAX]:
        if type(item) is not dict:
            continue
        old, new = item.get("old"), item.get("new")
        old_text = "(なし)" if old is None else _cut(old, VALUE_CHARS)
        new_text = "(なし)" if new is None else _cut(new, VALUE_CHARS)
        rows.append(f"<dt>{_cut(item.get('key'), VALUE_CHARS)}</dt><dd>{old_text} → {new_text}</dd>")
    notes = []
    for label, key in (("ほか", "omitted"), ("形に合わない key", "invalid_keys"),
                       ("比較していない key", "flatten_omitted")):
        count = params.get(key)
        if type(count) is int and count > 0:
            notes.append(f"<p>{label} {count} 件</p>")
    if params.get("reason") == "redacted":
        notes.append("<p>" + REASON_TEXT["redacted"] + "</p>")
    return rows, "".join(notes)


def _params_section(change: Mapping[str, object], shown: int | None = None) -> str:
    rows, notes = _params_rows(change)
    if not rows and not notes:
        if change.get("classification") == "updated":
            notes = "<p>差分はありません</p>"
        elif change.get("classification") == "new":
            notes = "<p>前版なし</p>"
        else:
            notes = "<p>比較対象がないため差分は出せません</p>"
    kept = rows if shown is None else rows[:shown]
    cut = ""
    if shown is not None and shown < len(rows):
        cut = f"<p>…({len(rows) - shown} 件省略)</p>"
    return ("<section><h2>params の差分</h2>" + ("<dl>" + "".join(kept) + "</dl>" if kept else "")
            + cut + notes + "</section>"), len(rows)


def _performance_section(change: Mapping[str, object]) -> str:
    perf = change.get("performance") if type(change.get("performance")) is dict else {}
    pairs = []
    for scope in ("in_sample", "holdout"):
        rows = perf.get(scope) if type(perf.get(scope)) is list else []
        if not rows:
            pairs.append((scope, "-"))
        for row in rows[:16]:
            if type(row) is not dict:
                continue
            pair = row.get("pair")
            tag = f"{scope} {_cut(pair, 16)}" if pair else scope
            pairs.append((tag, f"trades={_num(row.get('trades'))} PF={_num(row.get('pf'))} "
                               f"avg_r={_num(row.get('avg_r'))}"
                               + "".join(" " + _text(item) for item in metric_items(row))))
    return "<section><h2>成績</h2>" + _rows(pairs) + "</section>"


def _claims_section(summary: object, rationale: object, limit: int) -> str:
    if limit <= 0:
        return ("<section><h2>申告 (agent の申告・未検証)</h2><p>…(省略)</p></section>")
    body = ""
    for title, value in (("summary", summary), ("selection_rationale", rationale)):
        if type(value) is str and value:
            body += f"<h3>{title}</h3><pre>{_cut(value, limit, block=True)}</pre>"
    return "<section><h2>申告 (agent の申告・未検証)</h2>" + (body or "<p>-</p>") + "</section>"


def _valid_diff_files(diff: object) -> tuple[list[dict], int]:
    if type(diff) is not dict or type(diff.get("files")) is not list:
        return [], 0
    files = []
    for entry in diff["files"]:
        if (type(entry) is dict and entry.get("path") in _DIFF_FILES
                and entry.get("status") in ("added", "changed", "unchanged", "redacted")):
            files.append(entry)
    files.sort(key=lambda e: _DIFF_FILES.index(e["path"]))
    return files, 0


def _file_lines(entry: Mapping[str, object]) -> list[str]:
    """escape 済みの表示行 (hunk の header と本文)。"""
    out = []
    hunks = entry.get("hunks") if type(entry.get("hunks")) is list else []
    for hunk in hunks:
        if type(hunk) is not dict:
            continue
        if type(hunk.get("header")) is str:
            out.append(html.escape(_clean_line(hunk["header"]), quote=True))
        for line in hunk.get("lines") if type(hunk.get("lines")) is list else []:
            if type(line) is str:
                out.append(html.escape(_clean_line(line), quote=True))
    return out


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


class FilePlan:
    """diff 1 ファイルの配分結果。``need`` は全行を出すのに要る byte、``alloc`` は割当 byte。"""

    __slots__ = ("path", "kind", "head", "tail", "lines", "need", "alloc", "shown", "omitted",
                 "api_omitted", "html")

    def __init__(self, entry: Mapping[str, object]) -> None:
        self.path = entry["path"]
        status = entry["status"]
        self.kind = "pre" if status in ("added", "changed") else status
        api = entry.get("omitted_lines")
        self.api_omitted = api if type(api) is int and api > 0 else 0
        self.lines = _file_lines(entry) if self.kind == "pre" else []
        if self.kind == "pre" and not self.lines and not self.api_omitted:
            self.kind = "unchanged"
        self.head = f"<h3>{html.escape(self.path)}</h3>"
        self.tail = ""
        if self.kind == "pre":
            if entry.get("diff_degraded") == "too_many_lines":
                self.head += "<p>" + DIFF_DEGRADED_TEXT + "</p>"
            self.head += "<pre>"
            self.tail = "</pre>"
        elif self.kind == "unchanged":
            self.head += "<p>差分なし</p>"
        elif self.kind == "redacted":
            self.head += "<p>" + REASON_TEXT["redacted"] + "</p>"
        self.shown = len(self.lines)
        self.omitted = self.api_omitted
        self.html = ""
        self.need = _bytes(self.head) + _bytes(self.tail)
        if self.kind == "pre":
            self.need += sum(_bytes(line) + 1 for line in self.lines)
            if self.api_omitted:
                self.need += _bytes(_omitted_mark(self.api_omitted)) + 1
        self.alloc = self.need

    def render(self) -> None:
        if self.kind != "pre":
            self.html = self.head
            return
        body = "".join(line + "\n" for line in self.lines[:self.shown])
        if self.omitted:
            body += _omitted_mark(self.omitted) + "\n"
        self.html = self.head + body + self.tail


def plan_diff(diff: object) -> list[FilePlan]:
    """48 KiB の配分 (決定的)。各ファイルに quota を与え、未使用分を、quota を超えるファイルへ
    plugin.py、config.yaml、test_plugin.py の順に不足分まで割り当てる。"""
    entries, _ = _valid_diff_files(diff)
    plans = [FilePlan(entry) for entry in entries]
    used = [min(plan.need, FILE_QUOTA_BYTES) for plan in plans]
    spare = DIFF_BUDGET_BYTES - sum(used)
    for plan, base in zip(plans, used):
        if plan.need > FILE_QUOTA_BYTES:
            extra = min(spare, plan.need - FILE_QUOTA_BYTES)
            plan.alloc = FILE_QUOTA_BYTES + extra
            spare -= extra
        else:
            plan.alloc = plan.need
        if plan.kind == "pre" and plan.need > plan.alloc:
            # 省略印の byte は先に割当から確保する (件数の桁数は最大で見積もる)
            total = len(plan.lines) + plan.api_omitted
            room = plan.alloc - _bytes(plan.head) - _bytes(plan.tail) - _bytes(_omitted_mark(total)) - 1
            shown = 0
            for line in plan.lines:
                cost = _bytes(line) + 1
                if cost > room:
                    break
                room -= cost
                shown += 1
            plan.shown = shown
            plan.omitted = len(plan.lines) - shown + plan.api_omitted
        plan.render()
    return plans


def _diff_section(diff: object) -> tuple[str, int, list[FilePlan]]:
    plans = plan_diff(diff)
    body = "".join(plan.html for plan in plans)
    return body, _bytes(body), plans


EXTRA_OMITTED_SECTION = "<section><p>省略: 一部の節は表示容量の都合で省きました</p></section>"
# 再読込を残すタブ。長い diff や申告を読んでいる間に読み直さないよう、変更と申告は外す
REFRESH_TABS = frozenset({"overview", "result"})
UNCHANGED_BLOCK_TEXT = "変更内容を取得できないため、比較と規模は出せません"
DECISION_BASIS_TEXT = ("承認が結び付くのは承認の内容 (payload_sha256。候補の artifact・成績・申告・gate の警告を含む) で、"
                       "比較対象は含まない。差分は表示した時点の配備中の版との比較で、配備中の版が変わると変わる。")


def _fit_extra_sections(used: int, extra_sections: Sequence[str]) -> list[str]:
    """``used`` バイトの上に extra 節を足して 15 KiB に収める。収まらないときは先頭から節単位で
    省き、省いたら「省略」行を先頭に残す。"""
    kept = list(extra_sections)
    omitted = EXTRA_OMITTED_SECTION
    if used + sum(_bytes(x) for x in kept) <= OTHER_BUDGET_BYTES:
        return kept
    while kept and used + sum(_bytes(x) for x in kept) + _bytes(omitted) > OTHER_BUDGET_BYTES:
        kept.pop(0)
    kept.insert(0, omitted)
    return kept


def _fit_tagged_extras(used: int, tagged: Sequence[tuple[str, str]]) -> tuple[list[tuple[str, str]], frozenset[str]]:
    """``_fit_extra_sections`` の結果を (タブ, html) に戻す。省いた節が属するタブも返す。"""
    kept = _fit_extra_sections(used, [section for _tab, section in tagged])
    if len(kept) == len(tagged) and (not kept or kept[0] is not EXTRA_OMITTED_SECTION):
        return list(tagged), frozenset()
    survivors = len(kept) - 1
    dropped = len(tagged) - survivors
    return list(tagged[dropped:]), frozenset(tab for tab, _section in tagged[:dropped])


def _scale_params(change: Mapping[str, object], total: int) -> str:
    params = change.get("params")
    if type(params) is not dict:
        return "params の差分: 取得できない"
    if params.get("reason") == "redacted":
        return "params の差分: 非表示"
    notes = []
    for label, key in (("ほか", "omitted"), ("形に合わない key", "invalid_keys"),
                       ("比較していない key", "flatten_omitted")):
        count = params.get(key)
        if type(count) is int and count > 0:
            notes.append(f"{label} {count} 件")
    if total == 0 and not notes:
        if change.get("classification") == "updated":
            return "params の差分 0 件"
        if change.get("classification") == "new":
            return "params の差分: 前版なし"
        return "params の差分: 取得できない"
    return f"params の差分 {total} 件" + (" (" + "、".join(notes) + ")" if notes else "")


def _scale_diff(change_data: Mapping[str, object] | None, plans: Sequence[FilePlan]) -> str:
    if change_data is None or type(change_data.get("diff")) is not dict:
        return "diff: 取得できない"
    hidden = sum(1 for plan in plans if plan.kind == "redacted")
    seen = [plan for plan in plans if plan.kind != "redacted"]
    parts = []
    if seen or not hidden:
        parts.append(f"表示 {sum(plan.shown for plan in seen if plan.kind == 'pre')} 行・省略 "
                     f"{sum(plan.omitted for plan in seen if plan.kind == 'pre')} 行")
    if hidden:
        parts.append(f"非表示 {hidden} ファイル")
    return "diff " + "・".join(parts)


def _scale_claims(change_data: Mapping[str, object] | None, summary: object, rationale: object) -> str:
    if change_data is None:
        return "申告: 取得できない"
    given = any(type(value) is str and value for value in (summary, rationale))
    return "申告: " + ("あり" if given else "なし")


def _decision_block(data: Mapping[str, object], at: str, *, change: Mapping[str, object] | None = None,
                    scale: Sequence[str] = (), tz: str = "UTC") -> str:
    """概要の末尾に form と一緒に足す決定のブロック。変更内容が無いときは比較と規模の代わりに固定文。"""
    digest = data.get("payload_sha256")
    rows = [("承認の内容 (payload_sha256)", _hash12(digest))]
    body = ""
    if change is not None:
        label = CLASS_LABELS.get(change.get("classification"), CLASS_LABELS["unknown"])
        rows += [("候補 (artifact)", _hash12(change.get("candidate_hash"))),
                 ("比較対象", _hash12(change.get("baseline_hash"))), ("分類", label),
                 ("計算時刻", _computed_at(change.get("computed_at"), tz))]
    body += _rows(rows)
    if change is None:
        body += "<p>" + UNCHANGED_BLOCK_TEXT + "</p>"
    else:
        reason = change.get("reason")
        if reason is not None:
            body += "<p>" + REASON_TEXT.get(reason, REASON_TEXT["unsafe_source"]) + "</p>"
        body += ("<h3>見ていないタブの規模</h3><ul>"
                 + "".join(f"<li>{html.escape(text, quote=True)}</li>" for text in scale) + "</ul>"
                 + "<p>" + " / ".join(f'<a href="{html.escape(at, quote=True)}?tab={name}">{label}</a>'
                                      for name, label in (("result", "成績"), ("change", "変更"),
                                                          ("claims", "申告"))) + "</p>")
    return body + "<p>" + DECISION_BASIS_TEXT + "</p>"


class ApprovalPage:
    """承認詳細の仮想の 1 ページ (全タブの節を並べたもの) の計算結果。各タブはこの結果から自分の節を出す。

    ``total_bytes`` が 64 KiB 以上なら全タブが「Output clipped」になる。``other_bytes`` は diff 以外 (骨格を含む)。
    """

    __slots__ = ("sections", "extras", "omitted_tabs", "total_bytes", "diff_bytes", "other_bytes",
                 "plans", "params_total", "scale", "block")

    def __init__(self, sections: Mapping[str, str], extras: Sequence[tuple[str, str]],
                 omitted_tabs: frozenset[str], total_bytes: int, diff_bytes: int, other_bytes: int,
                 plans: list[FilePlan], params_total: int, scale: Sequence[str], block: str) -> None:
        self.sections = dict(sections)
        self.extras = list(extras)
        self.omitted_tabs = omitted_tabs
        self.total_bytes = total_bytes
        self.diff_bytes = diff_bytes
        self.other_bytes = other_bytes
        self.plans = plans
        self.params_total = params_total
        self.scale = tuple(scale)
        self.block = block

    @property
    def clipped(self) -> bool:
        return self.total_bytes >= PAGE_LIMIT_BYTES

    def body(self, tab: str) -> str:
        tail = "".join(section for owner, section in self.extras if owner == tab)
        note = EXTRA_OMITTED_SECTION if tab in self.omitted_tabs else ""
        return (self.sections.get(tab, "") + note + tail) or "<p>なし</p>"

    def view(self, data: Mapping[str, object], tab: str, at: str) -> ApprovalView:
        tz = zone_of(data)
        bar = tab_table.bar(tab_table.APPROVAL_DETAIL, tab, at=at)
        refresh = tab in REFRESH_TABS
        if self.clipped:
            page = _document("Approval", "<p>Output clipped</p>", tz, refresh=refresh,
                             current="/plugins", tabs=bar)
            return ApprovalView(page, tab, True, "")
        page = _document("Approval", self.body(tab), tz, refresh=refresh, current="/plugins", tabs=bar)
        return ApprovalView(page, tab, False, self.block if tab == "overview" else "")


def _approval_skeleton(tz: str | None, at: str) -> int:
    """仮想ページの骨格 (nav・帯・再読込を含む) の長さ。帯の長さはどのタブでも同じ。"""
    return len(_document("Approval", "", tz, current="/plugins",
                         tabs=tab_table.bar(tab_table.APPROVAL_DETAIL, "overview", at=at)))


def _virtual_page(data: Mapping[str, object], at: str, sections: Mapping[str, str], extras_fit,
                  *, diff_bytes: int = 0, plans: Sequence[FilePlan] = (), params_total: int = 0,
                  scale: Sequence[str] = (), block_change: Mapping[str, object] | None = None,
                  tz: str = "UTC") -> ApprovalPage:
    kept, omitted_tabs = extras_fit
    extra_bytes = sum(_bytes(section) for _tab, section in kept)
    if omitted_tabs:
        extra_bytes += _bytes(EXTRA_OMITTED_SECTION)
    skeleton = _approval_skeleton(tz, at)
    total = skeleton + sum(_bytes(section) for section in sections.values()) + extra_bytes
    block = _decision_block(data, at, change=block_change, scale=scale, tz=tz)
    return ApprovalPage(sections, kept, omitted_tabs, total, diff_bytes, total - diff_bytes,
                        list(plans), params_total, scale, block)


def _unchanged_virtual(data: Mapping[str, object], message: str,
                       extra_sections: Sequence[tuple[str, str]], at: str) -> ApprovalPage:
    """変更内容の無い承認詳細。概要に dl と固定文、追加節は通常と同じタブ、変更と申告は「なし」。"""
    tz = zone_of(data)
    overview = ("<section><h2>概要</h2>" + _approval_cards(data) + _approval_groups(data, tz)
                + "</section>"
                "<section><h2>変更内容</h2><p>" + message + "</p></section>")
    skeleton = _approval_skeleton(tz, at)
    fit = _fit_tagged_extras(skeleton + _bytes(overview), extra_sections)
    return _virtual_page(data, at, {"overview": overview}, fit, tz=tz)


def compose_approval_page(data: Mapping[str, object], change: Mapping[str, object],
                          change_data: Mapping[str, object] | None,
                          extra_sections: Sequence[tuple[str, str]] = (),
                          approval_id: int | None = None) -> ApprovalPage:
    """全タブの節を並べた仮想の 1 ページを組み、64 KiB 未満に収める。
    diff は 48 KiB 以下、それ以外 (帯を含む骨格を含む) は 15 KiB 以下。超えるときは params の差分 →
    申告 → 親の記録 の順で省略する。``extra_sections`` は (タブ, 節) で、配備中の版の比較・indicator gate
    の警告などの末尾に足す節 (重要でない順)。他の節と同じ 15 KiB の中に数え、他の節を縮め切ってもなお
    収まらないときだけ、先頭から節単位で省いて「省略」行を残す。骨格は常に再読込付きで測る。"""
    claims = change_data.get("claims") if change_data and type(change_data.get("claims")) is dict else {}
    summary = claims.get("summary", change.get("summary"))
    rationale = claims.get("selection_rationale")
    if change_data is not None and "diff" in change_data:
        diff_body, diff_bytes, plans = _diff_section(change_data.get("diff"))
    else:
        diff_body = "<p>" + REASON_TEXT["unavailable"] + "</p>"
        diff_bytes, plans = _bytes(diff_body), []
    diff_html = "<section><h2>変更 diff</h2>" + diff_body + "</section>"
    frame = _bytes(diff_html) - diff_bytes
    tz = zone_of(data)
    at = _approval_at(data, approval_id)
    skeleton = _approval_skeleton(tz, at)
    params_total = len(_params_rows(change)[0])

    def build(show_params: int | None, claim_limit: int, parent: bool) -> dict[str, str]:
        params_html, _ = _params_section(change, show_params)
        return {"overview": _overview_section(data, change, parent, tz),
                "result": _performance_section(change),
                "change": params_html,
                "claims": _claims_section(summary, rationale, claim_limit)}

    candidates = [(None, CLAIM_CHARS, True)]
    candidates += [(k, CLAIM_CHARS, True) for k in range(params_total - 1, -1, -1)]
    candidates += [(0, limit, True) for limit in (1000, 500, 250, 100, 0)]
    candidates += [(0, 0, False)]
    fixed = skeleton + frame
    extra_bytes = sum(_bytes(section) for _tab, section in extra_sections)
    others: dict[str, str] = {}
    for show, limit, parent in candidates:
        others = build(show, limit, parent)
        if fixed + sum(_bytes(section) for section in others.values()) + extra_bytes <= OTHER_BUDGET_BYTES:
            break
    fit = _fit_tagged_extras(fixed + sum(_bytes(section) for section in others.values()), extra_sections)
    others["change"] += diff_html if diff_body else ""
    scale = (_scale_params(change, params_total), _scale_diff(change_data, plans),
             _scale_claims(change_data, summary, rationale))
    return _virtual_page(data, at, others, fit, diff_bytes=diff_bytes, plans=plans,
                         params_total=params_total, scale=scale, block_change=change, tz=tz)


def render_log(data: Mapping[str, object]) -> bytes:
    lines = data.get("lines")
    if not isinstance(lines, list):
        lines = data.get("log")
    tz = zone_of(data)
    values = ([_text(display_time.convert_log_line(line, tz)) for line in lines[-MAX_ITEMS:]]
              if isinstance(lines, list) else [])
    return _page("Log", "<pre>" + "\n".join(item for item in values if item) + "</pre>", tz, current="/logs")


def render_activity(data: Mapping[str, object], category: str | None = None,
                    n: int | None = None) -> bytes:
    lines = data.get("lines")
    tz = zone_of(data)
    values = ([_text(display_time.convert_activity_line(line, tz)) for line in lines[-MAX_ITEMS:]]
              if isinstance(lines, list) else [])
    body = "<pre>" + "\n".join(item for item in values if item) + "</pre>"
    bar = tab_table.bar("/activity", category or tab_table.ALL,
                        carry={} if n is None else {"n": n})
    return _page("Activity", body, tz, current="/activity", tabs=bar)


def render_error(status: int) -> bytes:
    labels = {400: "Bad request", 401: "Authentication required", 403: "Forbidden",
              404: "Not found", 405: "Method not allowed", 409: "Already used",
              410: "Expired", 413: "Request too large", 415: "Unsupported media type",
              429: "Too many attempts", 502: "Service unavailable"}
    return _page(labels.get(status, "Error"), "")
