"""plugin の一覧と詳細の画面。daemon の応答のうち、ここで名指しした field だけを表示する。"""
from __future__ import annotations

import html
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from afx_ops import plugin_text
from afx_ops.display_zone import zone_of
from afx_ops.plugin_text import label
from afx_ops.web import render, tabs
from afx_ops.web.common import approval_link, display_time, pick, plugin_link

# server DTO の新 field を表示面へ自動的に広げないための allowlist (入れ子の object ごと)。
LISTING_FIELDS = frozenset({"sources", "plugins", "invalid_pending", "unknown_submissions"})
ROW_FIELDS = frozenset({"name", "kind", "entity", "admission", "pending_count", "pending",
                        "open_ops", "issues", "dependencies", "dependencies_set_state",
                        "dependents", "usefulness", "history", "history_total", "versions"})
ENTITY_FIELDS = frozenset({"state", "artifact_hash", "content_hash", "reason"})
ADMISSION_FIELDS = frozenset({"state", "reason", "alias", "detail"})
PENDING_FIELDS = frozenset({"id", "content_hash", "kind"})
OPEN_OP_FIELDS = frozenset({"source", "op_id", "kind", "target"})
DEPENDENCY_FIELDS = frozenset({"alias", "plugin", "pin", "state"})
DEPENDENT_FIELDS = frozenset({"strategy", "alias", "pin", "state"})
USEFULNESS_FIELDS = frozenset({"submitted", "deployed", "by_pin"})
BY_PIN_FIELDS = frozenset({"content_hash", "submitted", "deployed"})
HISTORY_FIELDS = frozenset({"id", "status", "kind", "content_hash", "origin", "mission_id",
                            "decided_at", "created_at"})
VERSION_FIELDS = frozenset({"artifact_hash", "is_live", "approval_id", "status", "content_hash"})

# 一覧の 1 行に出す、参照する strategy・途中の操作・注意の件数の上限
CELL_ITEMS = 5
LIST_ITEMS = 200
# 詳細の節ごとの byte の上限 (合計と枠・nav・style が 64 KiB に収まる値)
SECTION_BUDGET = {"overview": 6 * 1024, "pending": 3 * 1024, "dependencies": 12 * 1024,
                  "dependents": 10 * 1024, "usefulness": 4 * 1024, "versions": 8 * 1024,
                  "history": 14 * 1024}
OMIT_CLI = "全件は afx plugin list で確認してください"


def _items(value: object, allowed: frozenset[str]) -> list[dict]:
    """list の dict を allowlist で絞る。LIST_ITEMS を超える分は整形しない (件数は _hidden が数える)。"""
    if type(value) is not list:
        return []
    return [pick(item, allowed) for item in value[:LIST_ITEMS] if type(item) is dict]


def _hidden(value: object) -> int:
    """整形の対象にしなかった件数 (LIST_ITEMS を超えた分)。"""
    return max(0, len(value) - LIST_ITEMS) if type(value) is list else 0


def _count(value: object) -> str:
    return render._count_text(value)


def _hash(value: object) -> str:
    return render._hash12(value)


def _pin(value: object) -> str:
    return "pin なし" if value is None else _hash(value)


def _entity_text(entity: dict) -> str:
    text = label(plugin_text.ENTITY, entity.get("state"))
    if entity.get("reason") is not None:
        text += f" ({label(plugin_text.ENTITY_REASON, entity['reason'])})"
    return html.escape(text, quote=True)


def _admission_text(admission: dict) -> str:
    text = label(plugin_text.ADMISSION, admission.get("state"))
    if admission.get("reason") is not None:
        text += f" ({label(plugin_text.ADMISSION_REASON, admission['reason'])})"
    escaped = html.escape(text, quote=True)
    if admission.get("detail") is not None:
        alias = render._text(admission.get("alias")) or "-"
        escaped += (f" [{alias}: "
                    + html.escape(label(plugin_text.DEPENDENCY, admission["detail"]), quote=True)
                    + "]")
    return escaped


def _issues_text(issues: object) -> str:
    if type(issues) is not list:
        return "-"
    shown = [html.escape(label(plugin_text.ISSUE, word), quote=True)
             for word in issues[:CELL_ITEMS]]
    omitted = len(issues) - len(shown)
    if omitted > 0:
        shown.append(f"ほか {omitted} 件")
    return " / ".join(shown) or "-"


def _open_ops_text(ops: list[dict]) -> str:
    shown = []
    for op in ops[:CELL_ITEMS]:
        source = label(plugin_text.OPEN_OP_SOURCE, op.get("source"))
        kind = label(plugin_text.OPEN_OP_KIND, op.get("kind"))
        shown.append(html.escape(f"{source} {kind} #{op['op_id']}"
                                 if type(op.get("op_id")) is int else f"{source} {kind}",
                                 quote=True))
    omitted = len(ops) - len(shown)
    if omitted > 0:
        shown.append(f"ほか {omitted} 件")
    return " / ".join(shown) or "-"


def _dependents_text(dependents: list[dict]) -> str:
    shown = []
    for item in dependents[:CELL_ITEMS]:
        text = plugin_link(item.get("strategy"))
        if item.get("state") != "match":
            text += " (" + html.escape(label(plugin_text.DEPENDENCY, item.get("state")),
                                       quote=True) + ")"
        shown.append(text)
    omitted = len(dependents) - len(shown)
    if omitted > 0:
        shown.append(f"ほか {omitted} 件")
    return " / ".join(shown) or "-"


def _usefulness_text(usefulness: object) -> str:
    useful = pick(usefulness, USEFULNESS_FIELDS)
    if not useful:
        return "-"
    return f"提出 {_count(useful.get('submitted'))} / 配備成功 {_count(useful.get('deployed'))}"


def table_with_count(headers: tuple[str, ...], rows: list[list[str]], budget: int, hidden: int = 0,
           cli: str = OMIT_CLI) -> tuple[str, int]:
    """cell は escape 済み。budget の byte に収まる行までを出し、残り (hidden を含む) は件数だけ示す。"""
    head = ("<table><thead><tr>" + "".join(f"<th>{item}</th>" for item in headers)
            + "</tr></thead><tbody>")
    tail = "</tbody></table>"
    omit = _omitted_note(len(rows) + hidden, cli)
    used = render._bytes(head) + render._bytes(tail) + render._bytes(omit)
    kept: list[str] = []
    for cells in rows:
        row = "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"
        if used + render._bytes(row) > budget:
            break
        used += render._bytes(row)
        kept.append(row)
    omitted = len(rows) - len(kept) + hidden
    note = _omitted_note(omitted, cli) if omitted else ""
    return head + "".join(kept) + tail + note, len(kept)


def _table(headers: tuple[str, ...], rows: list[list[str]], budget: int, hidden: int = 0,
           cli: str = OMIT_CLI) -> str:
    return table_with_count(headers, rows, budget, hidden, cli)[0]


def _omitted_note(count: int, cli: str = OMIT_CLI) -> str:
    return f"<p>…({count} 件省略。{cli})</p>"


# ---------------------------------------------------------------- 一覧

def _list_row(raw: dict) -> list[str]:
    row = pick(raw, ROW_FIELDS)
    entity = pick(row.get("entity"), ENTITY_FIELDS)
    admission = pick(row.get("admission"), ADMISSION_FIELDS)
    ops = _items(row.get("open_ops"), OPEN_OP_FIELDS)
    dependents = _items(row.get("dependents"), DEPENDENT_FIELDS)
    is_indicator = row.get("kind") == "indicator"
    return [
        plugin_link(row.get("name")),
        html.escape(label(plugin_text.KIND, row.get("kind")), quote=True),
        _entity_text(entity),
        _admission_text(admission),
        _count(row.get("pending_count")),
        _hash(entity.get("content_hash")),
        _open_ops_text(ops),
        _dependents_text(dependents) if is_indicator else "-",
        _usefulness_text(row.get("usefulness")) if is_indicator else "-",
        _issues_text(row.get("issues")),
    ]


@dataclass(frozen=True)
class SortColumn:
    """並べ替えの列。`first` は初めの向き、`value` は行から比べる値を取り出す (無い・型違いは None)。

    `then` を持つ列は、同じ値の中を第 2 の値で並べる (第 2 の値が無い行は、その中の末尾)。
    """

    first: str
    value: Callable[[dict], object]
    then: Callable[[dict], object] | None = None


def _text_value(raw: object) -> str | None:
    return raw if type(raw) is str else None


def _int_value(raw: object) -> int | None:
    return raw if type(raw) is int else None


def _usefulness_value(row: dict, name: str) -> int | None:
    if row.get("kind") != "indicator":
        return None
    return _int_value(pick(row.get("usefulness"), USEFULNESS_FIELDS).get(name))


SORT_COLUMNS: dict[str, SortColumn] = {
    "name": SortColumn("asc", lambda row: _text_value(row.get("name"))),
    "kind": SortColumn("asc", lambda row: _text_value(row.get("kind"))),
    "state": SortColumn("asc", lambda row: _text_value(
        pick(row.get("entity"), ENTITY_FIELDS).get("state"))),
    "pending": SortColumn("desc", lambda row: _int_value(row.get("pending_count"))),
    "submitted": SortColumn("desc", lambda row: _usefulness_value(row, "submitted")),
    "deployed": SortColumn("desc", lambda row: _usefulness_value(row, "deployed")),
}
DIRECTIONS = ("asc", "desc")
_MARKS = {"asc": "▲", "desc": "▼"}


def sort_items(items: list[dict], columns: Mapping[str, SortColumn], sort: str | None,
               direction: str | None, project: Callable[[dict], dict]) -> list[dict]:
    """sort 列で並べ替える。同じ値は元の順のまま (desc でも反転しない)、値の無い行は向きによらず末尾。

    `project` は行から、列が読む field だけの dict を作る。
    """
    if sort is None:
        return items
    column = columns[sort]
    reverse = (direction or column.first) == "desc"
    valued: list[tuple[object, dict]] = []
    missing: list[dict] = []
    for raw in items:
        value = column.value(project(raw))
        if value is None:
            missing.append(raw)
        else:
            valued.append((value, raw))
    if column.then is not None:
        # 第 2 の値で先に並べ (安定)、第 1 の値の安定 sort で同じ値の中の順を保つ
        keyed = [(column.then(project(raw)), (value, raw)) for value, raw in valued]
        second = sorted([item for item in keyed if item[0] is not None],
                        key=lambda item: item[0], reverse=reverse)
        valued = [pair for _, pair in second] + [pair for key, pair in keyed if key is None]
    valued.sort(key=lambda pair: pair[0], reverse=reverse)
    return [raw for _, raw in valued] + missing


def sort_rows(plugins: list[dict], sort: str | None, direction: str | None) -> list[dict]:
    return sort_items(plugins, SORT_COLUMNS, sort, direction, lambda raw: pick(raw, ROW_FIELDS))


def sort_link(label_text: str, key: str, sort: str | None, direction: str | None, *,
              columns: Mapping[str, SortColumn] = SORT_COLUMNS, path: str = "/plugins",
              carry: tuple[tuple[str, str], ...] = ()) -> str:
    """見出しの link。今の列なら向きを逆にし、ほかの列はその列の初めの向きにする。

    `carry` は引き継ぐ (key, 検査済みの値) の組で、sort と dir の後ろに付ける。
    """
    column = columns[key]
    current = (direction or column.first) if key == sort else None
    target = column.first if current is None else DIRECTIONS[1 - DIRECTIONS.index(current)]
    query = "&".join([f"sort={key}", f"dir={target}"] + [f"{name}={value}" for name, value in carry])
    mark = _MARKS[current] if current else ""
    return f'<a href="{html.escape(f"{path}?{query}", quote=True)}">{html.escape(label_text, quote=True)}{mark}</a>'


def _list_headers(sort: str | None, direction: str | None) -> tuple[str, ...]:
    def link(text: str, key: str) -> str:
        return sort_link(text, key, sort, direction)

    return (link("名前", "name"), link("kind", "kind"), link("配備", "state"), "承認と読込",
            link("承認待ち", "pending"), "配備中の版", "途中の操作", "参照する strategy",
            f'有用性 ({link("提出", "submitted")} / {link("配備成功", "deployed")})', "注意")


def _sources_note(sources: object) -> str:
    states = pick(sources, frozenset({"live", "pending", "journal"}))
    bad = [(name, state) for name, state in sorted(states.items()) if state != "ok"]
    if not bad:
        return ""
    names = {"live": "配備", "pending": "承認待ち", "journal": "切替の記録"}
    parts = [f"{names[name]}: " + label(plugin_text.SOURCE_STATE, state) for name, state in bad]
    return '<p class="warn">' + html.escape(" / ".join(parts), quote=True) + "</p>"


def _header_section(data: Mapping[str, object]) -> str:
    listing = pick(data, LISTING_FIELDS)
    parts = ['<p><a href="/approvals">承認待ちの一覧 (Pending approvals)</a></p>',
             _sources_note(listing.get("sources"))]
    unknown = listing.get("unknown_submissions")
    if type(unknown) is int and unknown > 0:
        parts.append("<p>indicator の依存を記録していない古い strategy の提出: "
                     f"{unknown} 件 (有用性の数には含めない)</p>")
    invalid = listing.get("invalid_pending")
    if type(invalid) is list and invalid:
        links = " ".join(approval_link(item) for item in invalid[:CELL_ITEMS])
        more = f" ほか {len(invalid) - CELL_ITEMS} 件" if len(invalid) > CELL_ITEMS else ""
        parts.append(f'<p class="warn">読めない承認待ち: {len(invalid)} 件 ({links}{more})</p>')
    return "".join(parts)


def render_plugins(data: Mapping[str, object], sort: str | None = None,
                   direction: str | None = None) -> bytes:
    tz = zone_of(data)
    listing = pick(data, LISTING_FIELDS)
    plugins = listing.get("plugins")
    given = [row for row in plugins if type(row) is dict] if type(plugins) is list else []
    rows = [_list_row(row) for row in sort_rows(given, sort, direction)]
    head = _header_section(data)
    bar = tabs.bar("/plugins", "list")
    frame = '<div class="scroll"></div>'
    budget = render.MAX_RESPONSE_BYTES - len(
        render._document("Plugins", head + frame, tz, current="/plugins", tabs=bar))
    if rows:
        table = '<div class="scroll">' + _table(_list_headers(sort, direction), rows, budget) + "</div>"
    else:
        table = "<p>plugin はありません</p>"
    return render._page("Plugins", head + table, tz, current="/plugins", tabs=bar)


# ---------------------------------------------------------------- 詳細

def _section(title: str, body: str) -> str:
    return f"<section><h2>{title}</h2>{body}</section>"


def _overview(row: dict, entity: dict, admission: dict, tz: str) -> str:
    ops = _items(row.get("open_ops"), OPEN_OP_FIELDS)
    pairs = [
        ("kind", html.escape(label(plugin_text.KIND, row.get("kind")), quote=True)),
        ("配備", _entity_text(entity)),
        ("配備中の版 (内容)", _hash(entity.get("content_hash"))),
        ("配備中の版 (artifact)", _hash(entity.get("artifact_hash"))),
        ("承認と読込", _admission_text(admission)),
        ("承認待ち", _count(row.get("pending_count"))),
        ("途中の操作", _open_ops_text(ops)),
        ("注意", _issues_text(row.get("issues"))),
    ]
    return _section("概要", render._rows(pairs))


def _pending_section(row: dict) -> str:
    pending = _items(row.get("pending"), PENDING_FIELDS)
    if not pending:
        return ""
    rows = [[approval_link(item.get("id")), _hash(item.get("content_hash")),
             html.escape(label(plugin_text.KIND, item.get("kind")), quote=True)]
            for item in pending]
    return _section("承認待ち", _table(("承認", "内容", "kind"), rows,
                                       SECTION_BUDGET["pending"], _hidden(row.get("pending"))))


def _dependencies_section(row: dict) -> str:
    deps = _items(row.get("dependencies"), DEPENDENCY_FIELDS)
    set_state = row.get("dependencies_set_state")
    if not deps and set_state is None:
        return ""
    body = ""
    if set_state is not None:
        body += ("<p class=\"warn\">依存全体の状態: "
                 + html.escape(label(plugin_text.DEPENDENCY, set_state), quote=True) + "</p>")
    rows = [[render._text(item.get("alias")) or "-", plugin_link(item.get("plugin")),
             _pin(item.get("pin")),
             html.escape(label(plugin_text.DEPENDENCY, item.get("state")), quote=True)]
            for item in deps]
    if rows:
        body += _table(("alias", "plugin", "pin", "状態"), rows, SECTION_BUDGET["dependencies"],
                       _hidden(row.get("dependencies")))
    return _section("使う indicator", body)


def _dependents_section(row: dict) -> str:
    dependents = _items(row.get("dependents"), DEPENDENT_FIELDS)
    if not dependents and row.get("kind") != "indicator":
        return ""
    if not dependents:
        return _section("参照する strategy", "<p>なし</p>")
    rows = [[plugin_link(item.get("strategy")), render._text(item.get("alias")) or "-",
             _pin(item.get("pin")),
             html.escape(label(plugin_text.DEPENDENCY, item.get("state")), quote=True)]
            for item in dependents]
    return _section("参照する strategy",
                    _table(("strategy", "alias", "pin", "状態"), rows,
                           SECTION_BUDGET["dependents"], _hidden(row.get("dependents"))))


def _usefulness_section(row: dict, unknown_submissions: object) -> str:
    useful = pick(row.get("usefulness"), USEFULNESS_FIELDS)
    if not useful:
        return ""
    body = render._rows([("提出", _count(useful.get("submitted"))),
                         ("配備成功", _count(useful.get("deployed")))])
    pins = _items(useful.get("by_pin"), BY_PIN_FIELDS)
    if pins:
        rows = [[_pin(item.get("content_hash")), _count(item.get("submitted")),
                 _count(item.get("deployed"))] for item in pins]
        body += _table(("pin", "提出", "配備成功"), rows, SECTION_BUDGET["usefulness"],
                       _hidden(useful.get("by_pin")))
    if type(unknown_submissions) is int and unknown_submissions > 0:
        body += (f"<p>indicator の依存を記録していない古い strategy の提出 "
                 f"{unknown_submissions} 件は、上の数に含めていません</p>")
    return _section("有用性", body)


def _versions_section(row: dict) -> str:
    versions = _items(row.get("versions"), VERSION_FIELDS)
    if not versions:
        return ""
    rows = [[_hash(item.get("artifact_hash")), "配備中" if item.get("is_live") is True else "-",
             approval_link(item.get("approval_id")),
             html.escape(label(plugin_text.APPROVAL_STATUS, item["status"]), quote=True)
             if item.get("status") is not None else "-",
             _hash(item.get("content_hash"))] for item in versions]
    return _section("版の履歴", _table(("artifact", "配備", "承認", "承認の結果", "内容"), rows,
                                       SECTION_BUDGET["versions"], _hidden(row.get("versions"))))


def _history_section(row: dict, tz: str) -> str:
    history = _items(row.get("history"), HISTORY_FIELDS)
    if not history:
        return _section("承認の履歴", "<p>なし</p>")
    rows = []
    for item in history:
        origin = item.get("origin")
        rows.append([
            approval_link(item.get("id")),
            html.escape(label(plugin_text.APPROVAL_STATUS, item.get("status")), quote=True),
            html.escape(label(plugin_text.KIND, item.get("kind")), quote=True),
            html.escape(label(plugin_text.ORIGIN, origin), quote=True) if origin is not None else "-",
            _count(item.get("mission_id")),
            _hash(item.get("content_hash")),
            display_time(item.get("created_at"), tz), display_time(item.get("decided_at"), tz)])
    table = _table(("承認", "状態", "kind", "由来", "mission", "内容", "作成", "決定"), rows,
                   SECTION_BUDGET["history"], _hidden(row.get("history")))
    total = row.get("history_total")
    if type(total) is int and total > len(history):
        table += f"<p>最新 {len(history)} 件 (全 {render._count_text(total)} 件)</p>"
    return _section("承認の履歴", table)


# plugin 詳細のタブごとの節。キーは tabs.TABS[tabs.PLUGIN_DETAIL] の名前と一致させる
DETAIL_TAB_SECTIONS: dict[str, Callable[[dict, dict, dict, Mapping[str, object], str], str]] = {
    "overview": lambda row, entity, admission, data, tz: _overview(row, entity, admission, tz),
    "approvals": lambda row, entity, admission, data, tz: (
        _pending_section(row) + _history_section(row, tz)),
    "deps": lambda row, entity, admission, data, tz: (
        _dependencies_section(row) + _dependents_section(row)),
    "usefulness": lambda row, entity, admission, data, tz: _usefulness_section(
        row, data.get("unknown_submissions")),
    "versions": lambda row, entity, admission, data, tz: _versions_section(row),
}


def render_plugin(data: Mapping[str, object], tab: str = "overview",
                  name: str | None = None) -> bytes:
    tz = zone_of(data)
    row = pick(data.get("plugin"), ROW_FIELDS)
    entity = pick(row.get("entity"), ENTITY_FIELDS)
    admission = pick(row.get("admission"), ADMISSION_FIELDS)
    route_name = name
    name = row.get("name")
    shown = name if type(name) is str and render._NAME.match(name) else "-"
    nav = '<p><a href="/plugins">Plugins</a> / <a href="/approvals">Pending approvals</a></p>'
    body = DETAIL_TAB_SECTIONS[tab](row, entity, admission, data, tz) or "<p>なし</p>"
    link_name = route_name if type(route_name) is str and render._NAME.match(route_name) else shown
    bar = tabs.bar(tabs.PLUGIN_DETAIL, tab, at=f"/plugins/{link_name}")
    return render._page(f"Plugin {shown}", body, tz, current="/plugins", tabs=nav + bar)
