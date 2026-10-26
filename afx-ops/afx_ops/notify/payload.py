"""server event を外部へ出してよい固定 payload に射影する。

裁定: 外部 (webhook の受け手) への送信に、操作者の識別子 (`decided_by`) を出さない。server の
event は持っていてよいが、検査はしても payload には写さず、無くても配送は止めない (任意 field)。
"""
from __future__ import annotations

import re
from types import MappingProxyType
from typing import Mapping

from afx_ops import improve_events, trade_events

_SAFE_VALUE = re.compile(r"[A-Za-z0-9_.:+\-]{0,64}\Z")
_TIMESTAMP = re.compile(r"[A-Za-z0-9_.:+\-]{1,64}\Z")

ALLOWED_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType({
    "ops_request_terminal": frozenset({"endpoint_code", "result_code", "authenticated_principal"}),
    "approval_state_changed": frozenset({"state", "decision_code", "decided_by"}),
    "job_state_changed": frozenset({"job_kind", "state", "result_code"}),
    "guard_state_changed": frozenset({"guard_kind", "state", "generation"}),
    "service_health_changed": frozenset({"component_code", "health_code"}),
    "event_stream_health": frozenset({"failure_count", "first_failed_at", "last_failed_at"}),
    trade_events.ORDER_CODE: trade_events.ORDER_FIELDS,
    trade_events.CLOSE_CODE: trade_events.CLOSE_FIELDS,
    improve_events.CODE: improve_events.FIELDS,
})
# 外へ出さない field。event にあっても無くてもよい
_OPTIONAL_FIELDS = frozenset({"decided_by"})
_REF = MappingProxyType({
    "ops_request_terminal": re.compile(r"audit:[0-9]{1,18}\Z"),
    "approval_state_changed": re.compile(r"approval:[0-9]{1,18}\Z"),
    "job_state_changed": re.compile(r"job:[0-9a-f]{32}\Z"),
    "guard_state_changed": re.compile(r"instance\Z"),
    "service_health_changed": re.compile(r"instance\Z"),
    "event_stream_health": re.compile(r"instance\Z"),
    trade_events.ORDER_CODE: trade_events.REF_PATTERN,
    trade_events.CLOSE_CODE: trade_events.REF_PATTERN,
    improve_events.CODE: improve_events.REF_PATTERN,
})


class PayloadError(Exception):
    """event が API の固定 schema でない。"""

    reason = "payload_invalid"

    def __init__(self) -> None:
        super().__init__(self.reason)


def _safe_value(value: object) -> bool:
    return (value is None or (type(value) is int and value >= 0)
            or (type(value) is str and _SAFE_VALUE.fullmatch(value) is not None))


def _fields_match(code: str, fields: dict) -> bool:
    allowed = ALLOWED_FIELDS[code]
    return allowed - _OPTIONAL_FIELDS <= set(fields) <= allowed


def _event_valid(code: str, ref: str, fields: dict) -> bool:
    if code in trade_events.TRADE_CODES:
        # 取引 event は field ごとの grammar と canonical で見る。範囲は ref も整数で確かめる
        return trade_events.valid_ref(ref) and trade_events.valid_fields(code, fields)
    if code == improve_events.CODE:
        # 改善 event は field ごとの文法だけで見る (語は列挙しない)
        return improve_events.valid_fields(fields)
    return (_fields_match(code, fields)
            and all(_safe_value(value) for value in fields.values()))


def encode_event(event: object) -> dict[str, object]:
    """event を webhook 用の object にする。任意 text と未知 field は渡さない。"""
    if type(event) is not dict or set(event) != {"id", "ts", "code", "ref", "fields"}:
        raise PayloadError()
    event_id, ts, code, ref, fields = (event["id"], event["ts"], event["code"],
                                       event["ref"], event["fields"])
    if (type(event_id) is not int or event_id <= 0 or type(ts) is not str
            or _TIMESTAMP.fullmatch(ts) is None or type(code) is not str
            or code not in ALLOWED_FIELDS or type(ref) is not str
            or _REF[code].fullmatch(ref) is None or type(fields) is not dict
            or not _event_valid(code, ref, fields)):
        raise PayloadError()
    payload: dict[str, object] = {"event_id": event_id, "ts": ts, "code": code, "ref": ref}
    payload.update({key: value for key, value in fields.items() if key not in _OPTIONAL_FIELDS})
    return payload
