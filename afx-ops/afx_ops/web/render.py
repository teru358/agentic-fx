"""DTO の許可された小さな投影だけを HTML にする。"""
from __future__ import annotations

import html
import re
from collections.abc import Mapping

from afx_ops.web.routes import ROUTE_TABLE, resolve_route

MAX_TEXT = 512
MAX_ITEMS = 50
MAX_RESPONSE_BYTES = 64 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# 読み取り専用の表示なので、開いたままの画面を定期的に読み直させても状態は変わらない
REFRESH_SECONDS = 30

# 埋め込む唯一の CSS。CSP は hash (headers.STYLE_HASH) で これだけを許すので、1 byte でも
# 変えたら hash も変える。強調は値の属性ではなく renderer が付ける class で行う
STYLE = """
body { margin: 0; padding: 0 1.5rem 2rem; color: #1f2328; background: #fff;
  font-family: system-ui, -apple-system, "Segoe UI", "Hiragino Sans", "Noto Sans JP", sans-serif;
  line-height: 1.5; }
nav { display: flex; flex-wrap: wrap; gap: 1.25rem; padding: 0.75rem 0;
  border-bottom: 1px solid #d0d7de; margin-bottom: 1rem; }
nav a { color: #0969da; text-decoration: none; font-weight: 600; }
nav a:hover { text-decoration: underline; }
h1 { font-size: 1.4rem; margin: 0.5rem 0 1rem; }
section { border-bottom: 1px solid #eaeef2; padding: 0.5rem 0; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 0.25rem 1.5rem; margin: 0; }
dt { font-weight: 600; color: #57606a; }
dd { margin: 0; overflow-wrap: anywhere; }
dd dl { padding-left: 0.5rem; border-left: 2px solid #eaeef2; }
ul { margin: 0; padding-left: 1.25rem; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #f6f8fa; padding: 0.75rem;
  border-radius: 6px; }
.danger { color: #cf222e; }
dd.danger { font-weight: 700; background: #ffebe9; }
.warn { color: #9a6700; }
dd.warn { font-weight: 700; background: #fff8c5; }
"""

_NAV_LABELS = {"/": "Status", "/approvals": "Approvals", "/logs": "Log", "/activity": "Activity"}
# 静的な route (詳細は一覧の各項目から辿る)
_NAV = "<nav>" + "".join(
    f'<a href="{route.path}">{_NAV_LABELS[route.path]}</a>'
    for route in ROUTE_TABLE if "{" not in route.path) + "</nav>"
# 真のとき強調する status の field と class
_EMPHASIS = {"kill_switch": "danger", "autopilot": "warn"}

# server DTO の新 field を表示面へ自動的に広げないための allowlist。
STATUS_FIELDS = frozenset({"mode", "autopilot", "kill_switch", "equity", "balance", "pnl",
                           "performance", "orders", "mission", "health", "data", "jobs"})
APPROVAL_FIELDS = frozenset({"id", "kind", "name", "content_hash", "created_at"})
JOURNAL_FIELDS = frozenset({"approval_id", "name", "phase"})
DETAIL_FIELDS = frozenset({"id", "kind", "status", "in_sample", "holdout", "payload_sha256",
                           "decided_by", "decided_at", "asserted_actor", "parent_facts"})
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


def _pairs(data: object, allowed: frozenset[str], *, nested_allowed: frozenset[str] = frozenset(),
           emphasis: Mapping[str, str] | None = None) -> str:
    if type(data) is not dict:
        return ""
    rows = []
    for key in sorted(set(data) & allowed):
        value = data[key]
        shown = _value(value, nested_allowed)
        if shown:
            mark = (emphasis or {}).get(key) if _is_on(value) else None
            attr = f' class="{mark}"' if mark else ""
            rows.append(f"<dt{attr}>{html.escape(key, quote=True)}</dt><dd{attr}>{shown}</dd>")
    return "<dl>" + "".join(rows) + "</dl>" if rows else ""


def _value(value: object, nested_allowed: frozenset[str]) -> str:
    scalar = _text(value)
    if scalar:
        return scalar
    if type(value) is dict and nested_allowed:
        return _pairs(value, nested_allowed)
    if isinstance(value, list):
        values = [_value(item, nested_allowed) for item in value[:MAX_ITEMS]]
        return "<ul>" + "".join(f"<li>{item}</li>" for item in values if item) + "</ul>"
    return ""


def _page(title: str, body: str) -> bytes:
    document = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                + f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}"><title>'
                + html.escape(title, quote=True) + "</title><style>" + STYLE + "</style></head><body>"
                + _NAV + "<h1>" + html.escape(title, quote=True) + "</h1>" + body
                + "</body></html>").encode("utf-8")
    if len(document) <= MAX_RESPONSE_BYTES:
        return document
    return _page(title, "<p>Output clipped</p>")


def render_status(data: Mapping[str, object]) -> bytes:
    return _page("Status", _pairs(data, STATUS_FIELDS, nested_allowed=NESTED_FIELDS,
                                  emphasis=_EMPHASIS))


def _detail_link(item: dict) -> str:
    """id が詳細 route に載る正の整数のときだけ、その詳細への link を付ける。"""
    approval_id = item.get("id")
    if type(approval_id) is not int:
        return ""
    path = f"/approvals/{approval_id}"
    if resolve_route(path) is None:
        return ""
    return f'<p><a href="{path}">Details #{approval_id}</a></p>'


def render_approvals(data: Mapping[str, object]) -> bytes:
    approvals = data.get("approvals")
    entries = approvals if isinstance(approvals, list) else []
    body = "".join("<section>" + _detail_link(item) + _pairs(item, APPROVAL_FIELDS) + "</section>"
                   for item in entries[:MAX_ITEMS] if type(item) is dict)
    journals = data.get("open_journals")
    if isinstance(journals, list):
        body += "".join("<section>" + _pairs(item, JOURNAL_FIELDS) + "</section>"
                        for item in journals[:MAX_ITEMS] if type(item) is dict)
    return _page("Pending approvals", body)


def render_approval(data: Mapping[str, object]) -> bytes:
    return _page("Approval", _pairs(data, DETAIL_FIELDS, nested_allowed=NESTED_FIELDS))


def render_log(data: Mapping[str, object]) -> bytes:
    lines = data.get("lines")
    if not isinstance(lines, list):
        lines = data.get("log")
    values = [_text(line) for line in lines[-MAX_ITEMS:]] if isinstance(lines, list) else []
    return _page("Log", "<pre>" + "\n".join(item for item in values if item) + "</pre>")


def render_activity(data: Mapping[str, object]) -> bytes:
    lines = data.get("lines")
    values = [_text(line) for line in lines[-MAX_ITEMS:]] if isinstance(lines, list) else []
    body = "<pre>" + "\n".join(item for item in values if item) + "</pre>"
    return _page("Activity", body)


def render_error(status: int) -> bytes:
    labels = {400: "Bad request", 404: "Not found", 405: "Method not allowed", 413: "Request too large",
              502: "Service unavailable"}
    return _page(labels.get(status, "Error"), "")
