"""完全検証を通った event の扱いを 2 値で表す (bot と notify で共通)。

`POST` は投稿・配送してから cursor と high watermark を進める。`SKIP_KNOWN` は設定で出さない既知の
event で、何も出さずに同じ更新で cursor と high watermark を進める。完全検証に通らない event は
どちらでもなく、`PayloadError` で止まる (cursor を進めない)。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from afx_ops.notify.payload import encode_event
from afx_ops.improve_events import CODE as IMPROVE_CODE
from afx_ops.trade_events import TRADE_CODES


@dataclass(frozen=True, slots=True)
class Post:
    """投稿する event。payload は完全検証を通った射影。"""

    payload: dict


@dataclass(frozen=True, slots=True)
class SkipKnown:
    """検証は通ったが設定で出さない event。"""

    event_id: int


def decide(event: object, wants: Callable[[dict], bool]) -> Post | SkipKnown:
    """完全検証 (`PayloadError` を投げる) の後に、設定の filter を当てる。"""
    payload = encode_event(event)
    if wants(payload):
        return Post(payload)
    return SkipKnown(payload["event_id"])


def is_trade(payload: dict) -> bool:
    return payload["code"] in TRADE_CODES


def is_improve(payload: dict) -> bool:
    return payload["code"] == IMPROVE_CODE
