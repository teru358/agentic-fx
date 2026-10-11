"""snapshot と Discord message metadata から adapter の REST 操作を決める。"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .render import footer, footer_identity, primary_view, render_approval
from .state import ApprovalRecord, BotState
from .types import SideEffect

NO_LONGER_ACTIVE = "Approval is no longer active."

@dataclass(frozen=True, slots=True)
class MessageMetadata:
    message_id: int
    author_is_bot: bool
    guild_id: int
    channel_id: int
    footer: str | None


@dataclass(frozen=True, slots=True)
class ReconcilePlan:
    state: BotState
    effects: tuple[SideEffect, ...]


def mark_repost_intent(state: BotState, approval_id: int) -> BotState:
    """投稿を出し直す意図 (次の世代) を、送信の前に確定する。既にあれば変えない。"""
    record = state.record(approval_id)
    if record is None:
        raise ValueError("repost metadata")
    if record.repost_generation is not None:
        return state
    return state.replace_record(replace(record, repost_generation=record.display_generation + 1))


def record_reposted(state: BotState, approval_id: int, message_id: int, *,
                    generation: int) -> BotState | None:
    """Discord の投稿が確かめられた後、対応と新しい View 世代を commit する。

    generation は送信時に footer へ書いた世代で、record の出し直しの意図と一致するときだけ採用する
    (一致しなければ None、commit しない)。送信の間に終端へ遷移していたら世代は進めず、新しい投稿の
    View を外す edit を残す。
    """
    record = state.record(approval_id)
    if record is None or type(message_id) is not int or message_id <= 0:
        raise ValueError("repost metadata")
    if record.repost_generation is None or record.repost_generation != generation:
        return None
    if record.state == "done":
        return state.replace_record(replace(record, message_id=message_id, edit_pending=True,
                                            repost_generation=None))
    return state.replace_record(replace(record, message_id=message_id,
                                        display_generation=generation,
                                        edit_pending=False, repost_generation=None))


def _matches(record: ApprovalRecord, message: MessageMetadata) -> bool:
    return (message.author_is_bot and message.guild_id == record.guild_id
            and message.channel_id == record.channel_id
            and type(message.footer) is str
            and message.footer.startswith(footer_identity(record.approval_id, record.payload_sha256)))


def invalidate_restart(state: BotState) -> BotState:
    """再起動前の確認を一 replace で失効させる次 state。

    claim と、job ID の無い in_flight は世代を進めて fresh に戻す (daemon ではまだ pending)。
    job ID のある in_flight は決定が daemon に届いているので残し、job の poll を続ける。
    """
    records = []
    for record in state.approvals:
        if record.state == "claimed" or (record.state == "in_flight" and record.job_id is None):
            records.append(replace(record.fresh(), display_generation=record.display_generation + 1,
                                   edit_pending=True))
        else:
            records.append(record)
    return replace(state, approvals=tuple(records))


def plan_reconcile(state: BotState, messages: tuple[MessageMetadata, ...]) -> ReconcilePlan:
    """adopt は footer と bot author と投稿先が全て一致するときだけ行う。"""
    effects: list[SideEffect] = []
    next_state = state
    for record in state.approvals:
        if record.state == "done":
            if not record.edit_pending:
                continue
            effects.append(SideEffect("edit", approval_id=record.approval_id,
                                      message_id=record.message_id,
                                      content=NO_LONGER_ACTIVE))
            continue
        message = next((item for item in messages if _matches(record, item)), None)
        if message is None:
            effects.append(SideEffect("repost", approval_id=record.approval_id,
                                      content=render_approval(record),
                                      footer=footer(record.approval_id, record.payload_sha256,
                                                    record.display_generation + 1),
                                      view=primary_view(record)))
            continue
        if message.message_id != record.message_id:
            record = replace(record, message_id=message.message_id,
                             display_generation=record.display_generation + 1)
            next_state = next_state.replace_record(record)
        effects.append(SideEffect("adopt", approval_id=record.approval_id,
                                  message_id=record.message_id,
                                  view=primary_view(record)))
    return ReconcilePlan(next_state, tuple(effects))
