"""DTO の許可された小さな投影だけを HTML にする。"""
from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Mapping

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


def _document(title: str, body: str) -> bytes:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            + f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}"><title>'
            + html.escape(title, quote=True) + "</title><style>" + STYLE + "</style></head><body>"
            + _NAV + "<h1>" + html.escape(title, quote=True) + "</h1>" + body
            + "</body></html>").encode("utf-8")


def _page(title: str, body: str) -> bytes:
    document = _document(title, body)
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


def render_approval(data: Mapping[str, object], change_data: Mapping[str, object] | None = None) -> bytes:
    """承認 #N。``change_data`` は daemon の変更内容 endpoint の応答 (取れなければ None)。"""
    change = data.get("change") if type(data.get("change")) is dict else None
    if type(change_data) is dict and type(change_data.get("change")) is dict:
        change = change_data["change"]
    if change is None:
        body = (_pairs(data, DETAIL_FIELDS, nested_allowed=NESTED_FIELDS)
                + "<section><h2>変更内容</h2><p>" + REASON_TEXT["unavailable"] + "</p></section>"
                + _baseline_section(data))
        return _page("Approval", body)
    if not _change_is_well_formed(change):
        body = (_pairs(data, DETAIL_FIELDS, nested_allowed=NESTED_FIELDS)
                + "<section><h2>変更内容</h2><p>" + INVALID_CHANGE_TEXT + "</p></section>"
                + _baseline_section(data))
        return _page("Approval", body)
    rendered = compose_approval_page(data, change, change_data if type(change_data) is dict else None).page
    return rendered.replace(b"</body></html>", _baseline_section(data).encode("utf-8") + b"</body></html>")


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


def _overview_section(data: Mapping[str, object], change: Mapping[str, object], parent: bool) -> str:
    name = change.get("name")
    name = name if type(name) is str and _NAME.match(name) else "-"
    kind = change.get("kind") if change.get("kind") in ("strategy", "indicator", "signal") else "-"
    label = CLASS_LABELS.get(change.get("classification"), CLASS_LABELS["unknown"])
    pairs = [("plugin", html.escape(name)), ("kind", kind), ("分類", label),
             ("候補", _hash12(change.get("candidate_hash"))),
             ("比較対象", _hash12(change.get("baseline_hash"))),
             ("計算時刻", _cut(change.get("computed_at"), 40))]
    fields = OVERVIEW_FIELDS if parent else OVERVIEW_FIELDS - {"parent_facts"}
    body = _pairs(data, fields, nested_allowed=NESTED_FIELDS) + _rows(pairs)
    if change.get("classification") == "new":
        body += "<p>前版なし</p>"
    reason = change.get("reason")
    if reason is not None:
        body += "<p>" + REASON_TEXT.get(reason, REASON_TEXT["unsafe_source"]) + "</p>"
    return "<section><h2>概要</h2>" + body + "</section>"


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
                               f"avg_r={_num(row.get('avg_r'))}"))
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


class ApprovalPage:
    __slots__ = ("page", "diff_bytes", "other_bytes", "plans")

    def __init__(self, page: bytes, diff_bytes: int, other_bytes: int, plans: list[FilePlan]) -> None:
        self.page = page
        self.diff_bytes = diff_bytes
        self.other_bytes = other_bytes
        self.plans = plans


def compose_approval_page(data: Mapping[str, object], change: Mapping[str, object],
                          change_data: Mapping[str, object] | None) -> ApprovalPage:
    """5 節 (概要・params の差分・成績・申告・変更 diff) を組み、ページ全体を 64 KiB 未満に収める。
    diff は 48 KiB 以下、それ以外 (骨格を含む) は 15 KiB 以下。超えるときは params の差分 →
    申告 → 親の記録 の順で省略する。"""
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
    skeleton = len(_document("Approval", ""))
    params_total = len(_params_rows(change)[0])

    def build(show_params: int | None, claim_limit: int, parent: bool) -> str:
        params_html, _ = _params_section(change, show_params)
        return (_overview_section(data, change, parent) + params_html
                + _performance_section(change) + _claims_section(summary, rationale, claim_limit))

    candidates = [(None, CLAIM_CHARS, True)]
    candidates += [(k, CLAIM_CHARS, True) for k in range(params_total - 1, -1, -1)]
    candidates += [(0, limit, True) for limit in (1000, 500, 250, 100, 0)]
    candidates += [(0, 0, False)]
    others = ""
    for show, limit, parent in candidates:
        others = build(show, limit, parent)
        if skeleton + _bytes(others) + frame <= OTHER_BUDGET_BYTES:
            break
    body = others + (diff_html if diff_body else "")
    page = _document("Approval", body)
    other_bytes = len(page) - diff_bytes
    if len(page) >= PAGE_LIMIT_BYTES:
        page = _page("Approval", "<p>Output clipped</p>")
    return ApprovalPage(page, diff_bytes, other_bytes, plans)


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
    labels = {400: "Bad request", 401: "Authentication required", 404: "Not found",
              405: "Method not allowed", 413: "Request too large", 429: "Too many attempts",
              502: "Service unavailable"}
    return _page(labels.get(status, "Error"), "")
