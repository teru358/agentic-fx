"""gate が外部へ出してよい固定文字列と View model。"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from afx_ops.notify.payload import encode_event

from .state import ApprovalRecord
from .types import ViewComponent, ViewModel, encode_custom_id


@dataclass(frozen=True, slots=True)
class RenderedEvent:
    content: str
    mentions: tuple[str, ...]


def footer(approval_id: int, payload_sha256: str, generation: int) -> str:
    """投稿の識別子。世代は、その投稿を出した時点の View 世代 (過去世代の投稿と区別する)。"""
    return f"{footer_identity(approval_id, payload_sha256)}{generation}"


def footer_identity(approval_id: int, payload_sha256: str) -> str:
    """世代を除いた承認の識別 (世代の前までの固定部分)。"""
    return f"afx-approval:{approval_id}:{payload_sha256}:"


_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")

# 表示面の上限と固定文。daemon 側の定義 (change_limits) と値を揃える (afx-ops は本体を import しない)
SUMMARY_CHARS = 300
BODY_CHARS = 2000
PARAM_VALUE_CHARS = 80
PARAM_ROWS = 10
PERFORMANCE_ROWS = 4
_MARK = "…"

CLASS_LABELS = {"new": "新規", "updated": "更新", "same": "同一版", "unknown": "不明"}
KIND_LABELS = {"strategy": "戦略", "indicator": "指標", "signal": "シグナル"}
REASON_TEXT = {
    "unavailable": "変更内容を取得できません",
    "hash_mismatch": "内容を検証できません",
    "too_large": "内容を検証できません",
    "unsafe_source": "内容を検証できません",
    "redacted": "機微情報の可能性があるためローカルで確認してください",
    "detail_unavailable": "詳細を取得できません",
    "legacy_payload": "旧形式のため要約なし",
}
STATUS_LABELS = {"approved": "承認済み", "rejected": "却下", "expired": "失効"}
OMITTED_NOTE = "…(一部省略)"

_REMOVED = frozenset({"Cc", "Cf", "Cs"})
# Discord の markdown と記法を無効化する単位。@ は後ろに空白を足し (mention 記法を成立させない)、
# 他は直前に \ を置く。< は <@ <# <:> <t:> など山括弧で始まる記法全般を壊す
_ESCAPES = {"@": "@ "}
_ESCAPES.update({ch: "\\" + ch for ch in "\\*_~|`>#[]()<:"})


def _clean_inline(value: object) -> str:
    """1 行の値: 改行・タブは空白にし、Cc / Cf / Cs を除いて空白を畳む。"""
    if type(value) is not str:
        return ""
    text = value.translate({ord("\r"): " ", ord("\n"): " ", ord("\t"): " "})
    text = "".join(ch for ch in text if unicodedata.category(ch) not in _REMOVED)
    return " ".join(text.split())


def escape_discord(value: object) -> str:
    """非信頼値を Discord の本文に置ける形にする (整形、mention 記法・markdown・コードフェンス・
    URL の autolink の無効化)。文字は足すだけで、ゼロ幅文字は使わない。"""
    return "".join(_ESCAPES.get(ch, ch) for ch in _clean_inline(value))


def cut_escaped(value: object, limit: int) -> str:
    """整形 → escape → 表示上限の順。上限は escape 後の文字数で、escape の単位 (``\\*`` など)
    の途中では切らない。切ったときは末尾に「…」を付ける。"""
    out: list[str] = []
    used = 0
    for ch in _clean_inline(value):
        piece = _ESCAPES.get(ch, ch)
        if used + len(piece) > limit:
            return "".join(out) + _MARK
        out.append(piece)
        used += len(piece)
    return "".join(out)


def _number(value: object) -> str | None:
    if type(value) is int and not isinstance(value, bool):
        return str(value)
    if type(value) is float and math.isfinite(value):
        return f"{value:.4g}"
    return None


def _moment(value: object) -> str | None:
    """ISO 8601 の時刻を分までの固定形にする。読めなければ None (出さない)。"""
    if type(value) is not str or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.strftime("%Y-%m-%d %H:%M")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _heading(approval_id: int, change: object) -> str:
    head = f"承認 #{approval_id}"
    if type(change) is not dict:
        return head
    kind = KIND_LABELS.get(change.get("kind"), "plugin")
    name = change.get("name")
    name = escape_discord(name) if type(name) is str and _NAME.fullmatch(name) else "(名前不明)"
    label = CLASS_LABELS.get(change.get("classification"), CLASS_LABELS["unknown"])
    return f"{head} / {kind} {name} ({label})"


def _params_paragraph(change: dict) -> str | None:
    params = change.get("params")
    if type(params) is not dict:
        return None
    if change.get("classification") == "new":
        return "変更: 前版なし"
    lines: list[str] = []
    items = params.get("items") if type(params.get("items")) is list else []
    for item in items[:PARAM_ROWS]:
        if type(item) is not dict:
            continue
        old, new = item.get("old"), item.get("new")
        old_text = "(なし)" if old is None else cut_escaped(old, PARAM_VALUE_CHARS)
        new_text = "(なし)" if new is None else cut_escaped(new, PARAM_VALUE_CHARS)
        lines.append(f"{cut_escaped(item.get('key'), PARAM_VALUE_CHARS)}: {old_text} → {new_text}")
    extra = max(0, len(items) - PARAM_ROWS)
    omitted = params.get("omitted")
    extra += omitted if type(omitted) is int and 0 < omitted < 10 ** 9 else 0
    if extra:
        lines.append(f"ほか {extra} 件")
    if params.get("reason") == "redacted":
        lines.append(REASON_TEXT["redacted"])
    return "変更:\n" + "\n".join(lines) if lines else None


def _performance_paragraph(change: dict) -> str:
    performance = change.get("performance")
    rows = performance.get("in_sample") if type(performance) is dict else None
    rows = rows if type(rows) is list else []
    lines: list[str] = []
    for row in rows[:PERFORMANCE_ROWS]:
        if type(row) is not dict:
            continue
        parts = [escape_discord(row["pair"])] if type(row.get("pair")) is str else []
        for label, key in (("trades", "trades"), ("PF", "pf"), ("avg R", "avg_r")):
            shown = _number(row.get(key))
            parts.append(f"{label} {shown if shown is not None else '-'}")
        lines.append(" / ".join(parts))
    if len(rows) > PERFORMANCE_ROWS:
        lines.append(f"ほか {len(rows) - PERFORMANCE_ROWS} 件")
    return "成績 (in-sample):\n" + "\n".join(lines) if lines else "成績 (in-sample): なし"


def _claim_paragraph(change: dict) -> str | None:
    summary = change.get("summary")
    if type(summary) is not str or not _clean_inline(summary):
        return None
    return "agent の申告 (未検証):\n> " + cut_escaped(summary, SUMMARY_CHARS)


def _guide(approval_id: int) -> str:
    return f"詳細: web の承認 #{approval_id} / `afx ctl approval {approval_id}`"


def _assemble(heading: str, head_extra: list[str], optional: list[str]) -> str:
    """段落 (空行区切り) を組み、完成本文が BODY_CHARS を超えるうちは後ろの任意段落から落とす。

    ``head_extra`` は常に残す段落 (見出しと同格)、``optional`` は優先順の高い順。
    escape で長さが増えるので、上限は完成した本文に対して最後に掛ける。
    """
    kept = list(optional)
    dropped = False
    while True:
        paragraphs = [heading, *head_extra, *kept] + ([OMITTED_NOTE] if dropped else [])
        body = "\n\n".join(paragraphs)
        if len(body) <= BODY_CHARS or not kept:
            return body[:BODY_CHARS]
        kept.pop()
        dropped = True


def _detail_parts(detail: object) -> tuple[dict | None, str | None]:
    """(detail, 代替の reason code)。detail が取れない / 旧形式なら code を返す。"""
    if not isinstance(detail, dict):
        return None, "detail_unavailable"
    if type(detail.get("change")) is not dict:
        return detail, "legacy_payload"
    return detail, None


def _fallback(approval_id: int, code: str) -> str:
    return f"承認 #{approval_id}\n{REASON_TEXT[code]}"


def render_approval(record: ApprovalRecord, detail: object | None = None) -> str:
    """承認 detail の表示許可済み投影だけを Discord 文面にする。

    材料は daemon が redaction 済みで返す ``change`` だけ (生の ``agent_claims``・holdout・
    ``selection_rationale`` は読まない)。detail が取れない / 旧形式のときは見出しと固定文の 2 行。
    """
    data, code = _detail_parts(detail)
    if code is not None:
        return _fallback(record.approval_id, code)
    change = data["change"]
    heading = _heading(record.approval_id, change)
    reason = change.get("reason")
    reason_line = REASON_TEXT.get(reason, REASON_TEXT["unsafe_source"]) if reason is not None else None
    expires = _moment(data.get("expires_at"))
    deadline = f"期限: {expires}" if expires is not None else "期限: なし"
    optional = [deadline]
    for paragraph in (_params_paragraph(change), _performance_paragraph(change),
                      _claim_paragraph(change)):
        if paragraph is not None:
            optional.append(paragraph)
    extra = [reason_line] if reason_line is not None else []
    return _assemble(heading, extra + [_guide(record.approval_id)], optional)


def render_terminal(record: ApprovalRecord, detail: object | None) -> str | None:
    """終端の投稿に残す文面 (要約 + 状態と決定時刻)。決定者は出さない。

    決定結果が取れない (detail が無い、状態が終端でない) ときは None を返し、呼び出し側が
    固定の代替文にする。
    """
    data, code = _detail_parts(detail)
    if data is None:
        return None
    label = STATUS_LABELS.get(data.get("status"))
    if label is None:
        return None
    decided = _moment(data.get("decided_at"))
    status_line = f"状態: {label}" + (f" ({decided})" if decided is not None else "")
    if code is not None:
        return f"承認 #{record.approval_id}\n\n{status_line}\n\n{REASON_TEXT[code]}"
    change = data["change"]
    optional = []
    for paragraph in (_params_paragraph(change), _performance_paragraph(change),
                      _claim_paragraph(change)):
        if paragraph is not None:
            optional.append(paragraph)
    return _assemble(_heading(record.approval_id, change),
                     [status_line, _guide(record.approval_id)], optional)


def primary_view(record: ApprovalRecord) -> ViewModel:
    return ViewModel((
        ViewComponent(encode_custom_id("approve", record.approval_id), "Approve"),
        ViewComponent(encode_custom_id("reject", record.approval_id), "Reject", True),
        ViewComponent(encode_custom_id("retry", record.approval_id), "Retry"),
    ))


def confirm_view(record: ApprovalRecord) -> ViewModel:
    return ViewModel((
        ViewComponent(encode_custom_id("confirm", record.approval_id), "Confirm", True),
        ViewComponent(encode_custom_id("cancel", record.approval_id), "Cancel"),
    ), ephemeral=True)


def render_event(event: object, *, mention_role_id: int | None = None,
                 mention_user_ids: tuple[int, ...] = ()) -> RenderedEvent:
    """notify と同じ allowlist 検証を通った code と fields だけを写す。"""
    payload = encode_event(event)
    fields = tuple(f"{key}={payload[key]}" for key in sorted(payload)
                   if key not in {"event_id", "ts", "code", "ref"})
    text = payload["code"] + (": " + ", ".join(fields) if fields else "")
    mentions: list[str] = []
    if type(mention_role_id) is int:
        mentions.append(f"<@&{mention_role_id}>")
    mentions.extend(f"<@{user_id}>" for user_id in mention_user_ids if type(user_id) is int)
    return RenderedEvent(content=text, mentions=tuple(mentions))
