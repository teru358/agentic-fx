"""notify の cursor と high watermark を snapshot 一枚に保存する。"""
from __future__ import annotations

from dataclasses import dataclass

from afx_ops.snapshot import SnapshotError, read_snapshot, write_snapshot

SNAPSHOT_NAME = "notify.snapshot"
SNAPSHOT_VERSION = 2


class StateError(Exception):
    """notify snapshot が契約どおりでない。"""

    reason = "notify_state_invalid"

    def __init__(self) -> None:
        super().__init__(self.reason)


def _event_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise StateError()
    return value


@dataclass(frozen=True, slots=True)
class NotifyState:
    cursor: int
    # これまでに API が返した最大の watermark。cursor だけでは見えない DB の巻き戻りを再起動後も検出する
    high_watermark: int

    @classmethod
    def empty(cls) -> "NotifyState":
        return cls(cursor=0, high_watermark=0)

    def __post_init__(self) -> None:
        if type(self.cursor) is not int or self.cursor < 0:
            raise StateError()
        if type(self.high_watermark) is not int or self.high_watermark < self.cursor:
            raise StateError()


def _as_object(state: NotifyState) -> dict[str, object]:
    return {"version": SNAPSHOT_VERSION, "cursor": state.cursor,
            "high_watermark": state.high_watermark}


def _from_object(obj: object) -> NotifyState:
    if type(obj) is not dict or set(obj) != {"version", "cursor", "high_watermark"}:
        raise StateError()
    if obj["version"] != SNAPSHOT_VERSION:
        raise StateError()
    return NotifyState(cursor=obj["cursor"], high_watermark=obj["high_watermark"])


def load(dir_fd: int) -> NotifyState:
    """state dir fd だけから snapshot を読む。未作成なら空状態。"""
    try:
        obj = read_snapshot(dir_fd, SNAPSHOT_NAME, SNAPSHOT_VERSION)
    except SnapshotError:
        raise StateError() from None
    return NotifyState.empty() if obj is None else _from_object(obj)


def save(dir_fd: int, state: NotifyState) -> None:
    """配送成功と cursor advance を同じ atomic replace で確定する。"""
    if not isinstance(state, NotifyState):
        raise StateError()
    try:
        write_snapshot(dir_fd, SNAPSHOT_NAME, _as_object(state))
    except SnapshotError:
        raise StateError() from None


def advance_delivery(state: NotifyState, event_id: int) -> NotifyState:
    """未配送 event を成功として確定する次の状態 (cursor を進める) を返す。"""
    if not isinstance(state, NotifyState):
        raise StateError()
    event_id = _event_id(event_id)
    if event_id <= state.cursor:
        raise StateError()
    return NotifyState(cursor=event_id, high_watermark=max(state.high_watermark, event_id))


def observe_high(state: NotifyState, high: int) -> NotifyState:
    """API が返した watermark を取り込む。後退はしない (後退の検出は呼び側)。"""
    if not isinstance(state, NotifyState) or type(high) is not int:
        raise StateError()
    if high <= state.high_watermark:
        return state
    return NotifyState(cursor=state.cursor, high_watermark=high)
