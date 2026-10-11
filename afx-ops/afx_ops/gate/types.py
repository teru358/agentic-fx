"""Discord adapter と gate の間だけで使う値 object。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_ACTIONS = frozenset({"approve", "claim", "confirm", "cancel", "reject", "retry"})
_CUSTOM_ID = re.compile(r"afx:v1:(approve|claim|confirm|cancel|reject|retry):([1-9][0-9]*)\Z")

Action = Literal["approve", "reject", "retry"]


class CustomIdError(ValueError):
    """component ID が gate の所有する形式でない。"""


def encode_custom_id(action: str, approval_id: int) -> str:
    if action not in _ACTIONS or type(approval_id) is not int or approval_id <= 0:
        raise CustomIdError("custom_id_invalid")
    return f"afx:v1:{action}:{approval_id}"


def decode_custom_id(value: object) -> tuple[str, int]:
    if type(value) is not str:
        raise CustomIdError("custom_id_invalid")
    matched = _CUSTOM_ID.fullmatch(value)
    if matched is None:
        raise CustomIdError("custom_id_invalid")
    return matched.group(1), int(matched.group(2))


@dataclass(frozen=True, slots=True)
class Interaction:
    custom_id: str
    guild_id: int | None
    channel_id: int | None
    user_id: int | None
    message_id: int | None
    display_generation: int | None


@dataclass(frozen=True, slots=True)
class ViewComponent:
    custom_id: str
    label: str
    destructive: bool = False


@dataclass(frozen=True, slots=True)
class ViewModel:
    components: tuple[ViewComponent, ...]
    ephemeral: bool = False


@dataclass(frozen=True, slots=True)
class SideEffect:
    """adapter が Discord REST / persistent View へそのまま写す操作。"""

    kind: str
    approval_id: int | None = None
    message_id: int | None = None
    content: str | None = None
    footer: str | None = None
    view: ViewModel | None = None


@dataclass(frozen=True, slots=True)
class DecisionPlan:
    """worker thread で ``GateService.perform`` に渡す決定操作。"""

    approval_id: int
    action: Action
    actor: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class EventPlan:
    """投稿が成功した後にだけ cursor を commit する通知投稿。"""

    event_id: int
    content: str
    mentions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GateResult:
    text: str
    effects: tuple[SideEffect, ...] = ()
    confirm_view: ViewModel | None = None
    decision: DecisionPlan | None = None
