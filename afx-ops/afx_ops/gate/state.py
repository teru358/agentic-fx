"""bot.snapshot の schema と単一 writer 用の遷移。"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

from afx_ops.snapshot import SnapshotError, read_snapshot, write_snapshot

SNAPSHOT_NAME = "bot.snapshot"
SNAPSHOT_VERSION = 5
# 直前の版。flag を持たないので、読むときに明示的に migrate する
PREVIOUS_VERSION = 4
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ACTOR = re.compile(r"discord:[0-9]{17,20}\Z")
_JOB = re.compile(r"[0-9a-f]{32}\Z")
# 期限切れ・無効化は状態ではなく、世代を進めて fresh に戻す遷移。終端は done だけ
_STATES = frozenset({"fresh", "claimed", "in_flight", "done"})
_ACTIONS = frozenset({"approve", "reject", "retry"})


class StateError(Exception):
    reason = "bot_state_invalid"

    def __init__(self) -> None:
        super().__init__(self.reason)


def _id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise StateError()
    return value


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: int
    message_id: int
    guild_id: int
    channel_id: int
    payload_sha256: str
    display_generation: int
    state: str
    actor: str | None = None
    action: str | None = None
    nonce: str | None = None
    expiry: int | None = None
    confirm_message_id: int | None = None
    nonce_used: bool = False
    job_id: str | None = None
    # 投稿の表示 (View の付け直し・取り外し) を bot token の edit でまだ反映できていない
    edit_pending: bool = False
    # 投稿を出し直す意図 (出し直した投稿が持つ世代)。送信の前に確定し、投稿が届いたことを確かめた
    # 後の付け替えで消す。残っていれば、送信後・保存前に止まったかもしれない
    repost_generation: int | None = None

    def __post_init__(self) -> None:
        for value in (self.approval_id, self.message_id, self.guild_id, self.channel_id):
            _id(value)
        if (type(self.payload_sha256) is not str or _DIGEST.fullmatch(self.payload_sha256) is None
                or type(self.display_generation) is not int or self.display_generation < 0
                or self.state not in _STATES or type(self.edit_pending) is not bool
                or (self.repost_generation is not None
                    and (type(self.repost_generation) is not int or self.repost_generation <= 0))):
            raise StateError()
        claimed = self.state in {"claimed", "in_flight"}
        values = (self.actor, self.action, self.nonce, self.expiry,
                  self.confirm_message_id, self.job_id)
        if claimed:
            if (type(self.actor) is not str or _ACTOR.fullmatch(self.actor) is None
                    or self.action not in _ACTIONS or type(self.nonce) is not str or not self.nonce
                    or type(self.expiry) is not int or self.expiry < 0
                    or (self.confirm_message_id is not None
                        and (type(self.confirm_message_id) is not int or self.confirm_message_id <= 0))
                    or type(self.nonce_used) is not bool
                    or (self.job_id is not None
                        and (type(self.job_id) is not str or _JOB.fullmatch(self.job_id) is None))):
                raise StateError()
        elif (any(value is not None for value in values)
              or type(self.nonce_used) is not bool):
            raise StateError()

    def fresh(self) -> "ApprovalRecord":
        return replace(self, state="fresh", actor=None, action=None, nonce=None, expiry=None,
                       confirm_message_id=None, nonce_used=True, job_id=None)


@dataclass(frozen=True, slots=True)
class BotState:
    cursor: int
    high_watermark: int
    approvals: tuple[ApprovalRecord, ...]
    # false の間は cursor と high watermark に意味が無く、event を配送しない。最初の正常な
    # /events 応答で cursor と同じ保存で true になる。snapshot の不在と保存済みの cursor=0 を分ける
    event_cursor_initialized: bool = False

    def __post_init__(self) -> None:
        if (type(self.cursor) is not int or self.cursor < 0 or type(self.high_watermark) is not int
                or self.high_watermark < self.cursor
                or type(self.event_cursor_initialized) is not bool
                or (not self.event_cursor_initialized
                    and (self.cursor != 0 or self.high_watermark != 0))):
            raise StateError()
        ids = [record.approval_id for record in self.approvals]
        messages = [record.message_id for record in self.approvals]
        if len(ids) != len(set(ids)) or len(messages) != len(set(messages)):
            raise StateError()

    @classmethod
    def empty(cls) -> "BotState":
        return cls(cursor=0, high_watermark=0, approvals=())

    def with_record(self, record: ApprovalRecord) -> "BotState":
        if not isinstance(record, ApprovalRecord):
            raise StateError()
        return replace(self, approvals=(*self.approvals, record))

    def replace_record(self, record: ApprovalRecord) -> "BotState":
        found = False
        records = []
        for old in self.approvals:
            if old.approval_id == record.approval_id:
                records.append(record)
                found = True
            else:
                records.append(old)
        if not found:
            raise StateError()
        return replace(self, approvals=tuple(records))

    def without(self, approval_id: int) -> "BotState":
        if self.record(approval_id) is None:
            raise StateError()
        return replace(self, approvals=tuple(record for record in self.approvals
                                             if record.approval_id != approval_id))

    def record(self, approval_id: int) -> ApprovalRecord | None:
        return next((record for record in self.approvals if record.approval_id == approval_id), None)


def advance_event(state: BotState, event_id: int, *, high_watermark: int | None = None) -> BotState:
    if (not isinstance(state, BotState) or not state.event_cursor_initialized
            or type(event_id) is not int or event_id <= state.cursor):
        raise StateError()
    high = event_id if high_watermark is None else high_watermark
    if type(high) is not int or high < event_id or high < state.high_watermark:
        raise StateError()
    return replace(state, cursor=event_id, high_watermark=high)


def observe_high(state: BotState, high_watermark: int) -> BotState:
    if not isinstance(state, BotState) or type(high_watermark) is not int:
        raise StateError()
    if high_watermark < state.high_watermark:
        raise StateError()
    return replace(state, high_watermark=max(state.high_watermark, high_watermark))


def initialize_event_cursor(state: BotState, high_watermark: int) -> BotState:
    """最初の正常な応答の末尾を cursor にして flag を立てる (呼び側は 1 回の保存で確定する)。"""
    if (not isinstance(state, BotState) or state.event_cursor_initialized
            or type(high_watermark) is not int or high_watermark < 0):
        raise StateError()
    return replace(state, cursor=high_watermark, high_watermark=high_watermark,
                   event_cursor_initialized=True)


def _record_object(record: ApprovalRecord) -> dict[str, object]:
    return {"approval_id": record.approval_id, "message_id": record.message_id,
            "guild_id": record.guild_id, "channel_id": record.channel_id,
            "payload_sha256": record.payload_sha256, "display_generation": record.display_generation,
            "state": record.state, "actor": record.actor, "action": record.action,
            "nonce": record.nonce, "expiry": record.expiry,
            "confirm_message_id": record.confirm_message_id, "nonce_used": record.nonce_used,
            "job_id": record.job_id, "edit_pending": record.edit_pending,
            "repost_generation": record.repost_generation}


_KEYS_V4 = {"version", "cursor", "high_watermark", "approvals"}


def migrate_v4(obj: object) -> dict[str, object]:
    """v4 の object を v5 の object にする。

    配送実績は cursor が正のときだけ (true、続きから)。v4 は event を投稿しない周回でも high watermark
    だけを進めて保存するので、cursor が 0 なら watermark が正でも未配送 (false) とし、false の
    不変条件 (cursor も watermark も 0) に合わせて watermark を捨てる。最初の正常な page の末尾から始まる。
    """
    if (type(obj) is not dict or set(obj) != _KEYS_V4 or obj["version"] != PREVIOUS_VERSION
            or type(obj["cursor"]) is not int or type(obj["high_watermark"]) is not int):
        raise StateError()
    delivered = obj["cursor"] > 0
    if not delivered:
        obj = {**obj, "cursor": 0, "high_watermark": 0}
    return {**obj, "version": SNAPSHOT_VERSION, "event_cursor_initialized": delivered}


def _from_object(obj: object) -> BotState:
    if type(obj) is dict and obj.get("version") == PREVIOUS_VERSION:
        obj = migrate_v4(obj)
    if type(obj) is not dict or set(obj) != {*_KEYS_V4, "event_cursor_initialized"}:
        raise StateError()
    if obj["version"] != SNAPSHOT_VERSION or type(obj["approvals"]) is not list:
        raise StateError()
    keys = {"approval_id", "message_id", "guild_id", "channel_id", "payload_sha256",
            "display_generation", "state", "actor", "action", "nonce", "expiry",
            "confirm_message_id", "nonce_used", "job_id", "edit_pending", "repost_generation"}
    try:
        records = tuple(ApprovalRecord(**item) if type(item) is dict and set(item) == keys
                        else (_ for _ in ()).throw(StateError()) for item in obj["approvals"])
        return BotState(cursor=obj["cursor"], high_watermark=obj["high_watermark"], approvals=records,
                        event_cursor_initialized=obj["event_cursor_initialized"])
    except (TypeError, StateError):
        raise StateError() from None


def load(dir_fd: int) -> BotState:
    try:
        obj = read_snapshot(dir_fd, SNAPSHOT_NAME, SNAPSHOT_VERSION, older=(PREVIOUS_VERSION,))
    except SnapshotError:
        raise StateError() from None
    return BotState.empty() if obj is None else _from_object(obj)


def save(dir_fd: int, state: BotState) -> None:
    if not isinstance(state, BotState):
        raise StateError()
    obj = {"version": SNAPSHOT_VERSION, "cursor": state.cursor,
           "high_watermark": state.high_watermark,
           "event_cursor_initialized": state.event_cursor_initialized,
           "approvals": [_record_object(record) for record in state.approvals]}
    try:
        write_snapshot(dir_fd, SNAPSHOT_NAME, obj)
    except SnapshotError:
        raise StateError() from None
