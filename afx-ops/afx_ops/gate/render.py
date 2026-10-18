"""gate が外部へ出してよい固定文字列と View model。"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

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


_WORD = re.compile(r"[a-z_]{1,24}\Z")
_HASH8 = re.compile(r"[0-9a-f]{8}\Z")
_MAX_ROWS = 10


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value < 10 ** 9 else None


def _number(value: object) -> float | int | None:
    if type(value) is int:
        return value
    return value if type(value) is float and math.isfinite(value) else None


def _word(value: object) -> str | None:
    return value if type(value) is str and _WORD.fullmatch(value) else None


def _facts(detail: dict) -> str:
    """親が記録した構造化 facts のうち、field 名と値の形を固定したものだけを一行にする。

    daemon の整形済み facts_lines には agent の自己申告が混ざり得るので使わない。
    """
    parent = detail.get("parent_facts")
    if type(parent) is not dict:
        return "none"
    parts: list[str] = []
    backlog = parent.get("backlog")
    if type(backlog) is dict:
        for key, check in (("id", _count), ("attempts", _count), ("status", _word)):
            value = check(backlog.get(key))
            if value is not None:
                parts.append(f"backlog.{key}={value}")
    runs = parent.get("prior_runs")
    for row in (runs[:_MAX_ROWS] if type(runs) is list else ()):
        if type(row) is not dict or _count(row.get("run_id")) is None:
            continue
        result = _word(row.get("result"))
        approval = _count(row.get("approval_id"))
        text = result if result is not None else "none"
        if approval is not None:
            text += f":{approval}"
        parts.append(f"run{row['run_id']}={text}")
    trials = parent.get("trials")
    for row in (trials[:_MAX_ROWS] if type(trials) is list else ()):
        if type(row) is not dict or _count(row.get("n")) is None:
            continue
        fields = [row["content_hash8"] if type(row.get("content_hash8")) is str
                  and _HASH8.fullmatch(row["content_hash8"]) else None]
        same = row.get("same_hash_as_submitted")
        fields.append(None if type(same) is not bool else f"same={str(same).lower()}")
        for key, label, check in (("trades", "trades", _count), ("pf", "pf", _number),
                                  ("avg_r", "avg_r", _number), ("max_drawdown", "mdd", _number)):
            value = check(row.get(key))
            fields.append(None if value is None else f"{label}={value}")
        parts.append(f"trial{row['n']}=" + ",".join(f for f in fields if f is not None))
    for key, label in (("trials_omitted", "trials_omitted"), ("analysis_call_count", "analysis_calls")):
        value = _count(parent.get(key))
        if value is not None:
            parts.append(f"{label}={value}")
    return "; ".join(parts) if parts else "none"


def render_approval(record: ApprovalRecord, detail: object | None = None) -> str:
    """承認 detail の表示許可済み投影だけを Discord 文面にする。"""
    data = detail if isinstance(detail, dict) else {}
    kind = _word(data.get("kind")) or "plugin"
    decision = _word(data.get("status")) or record.state
    facts_text = _facts(data)
    claims = "present" if bool(data.get("agent_claims")) else "absent"
    expiry = str(record.expiry) if record.expiry is not None else "unclaimed"
    claimed = "yes" if record.actor is not None else "no"
    lines = [f"approval={record.approval_id}", f"kind={kind}", f"facts={facts_text}",
             f"claims={claims}", f"digest={record.payload_sha256[:8]}",
             f"expires={expiry}", f"decision={decision}", f"claimed={claimed}"]
    reason = _word(data.get("reason"))
    if decision == "rejected" and reason is not None:
        lines.append(f"reason={reason}")
    return "\n".join(lines)


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
