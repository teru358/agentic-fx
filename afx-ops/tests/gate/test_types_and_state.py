from __future__ import annotations

import os
from typing import get_args

import pytest

from afx_ops.gate.state import ApprovalRecord, BotState, StateError, advance_event, load, save
from afx_ops.gate.types import Action, CustomIdError, _ACTIONS, decode_custom_id, encode_custom_id


def _record(**changes):
    values = dict(approval_id=7, message_id=101, guild_id=12345678901234567,
                  channel_id=12345678901234568, payload_sha256="a" * 64,
                  display_generation=1, state="fresh")
    values.update(changes)
    return ApprovalRecord(**values)


def test_custom_id_is_a_complete_four_part_grammar():
    assert encode_custom_id("approve", 7) == "afx:v1:approve:7"
    assert encode_custom_id("claim", 7) == "afx:v1:claim:7"
    assert decode_custom_id("afx:v1:retry:7") == ("retry", 7)
    for value in ("afx:v1:claim:07", "afx:v1:claim:7:x",
                  "afx:v2:claim:7", "afx:v1:claim:-7"):
        with pytest.raises(CustomIdError):
            decode_custom_id(value)


def test_custom_id_actions_are_exactly_the_supported_component_actions():
    assert _ACTIONS == frozenset({"approve", "claim", "confirm", "cancel", "reject", "retry"})


def test_decision_actions_are_exactly_the_supported_decisions():
    assert frozenset(get_args(Action)) == frozenset({"approve", "reject", "retry"})


def test_snapshot_round_trip_and_event_cursor_have_no_delivered_id_list(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        state = advance_event(BotState.empty().with_record(_record()), 3)
        save(fd, state)
        assert load(fd) == state
        raw = (tmp_path / "bot.snapshot").read_text(encoding="ascii")
        assert "delivered" not in raw
    finally:
        os.close(fd)


def test_state_rejects_duplicate_ids_and_a_rewound_watermark():
    with pytest.raises(StateError):
        BotState(cursor=0, high_watermark=0, approvals=(_record(), _record()))
    with pytest.raises(StateError):
        BotState(cursor=4, high_watermark=3, approvals=())


def test_state_rejects_an_invalid_persisted_discord_actor():
    with pytest.raises(StateError):
        _record(state="claimed", actor="discord:not-a-snowflake", action="approve",
                nonce="n", expiry=1)


def test_ac8_snapshot_load_rejects_an_invalid_persisted_discord_actor(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        snapshot = (tmp_path / "bot.snapshot")
        snapshot.write_text(
            '{"version":4,"cursor":0,"high_watermark":0,"approvals":['
            '{"approval_id":7,"message_id":101,"guild_id":12345678901234567,'
            '"channel_id":12345678901234568,"payload_sha256":"' + "a" * 64 + '",'
            '"display_generation":1,"state":"claimed","actor":"discord:bad",'
            '"action":"approve","nonce":"n","expiry":1,"confirm_message_id":null,'
            '"nonce_used":false,"job_id":null,"edit_pending":false,"repost_generation":null}]}', encoding="ascii")
        with pytest.raises(StateError):
            load(fd)
    finally:
        os.close(fd)


def test_custom_id_refuses_unknown_actions_other_separators_and_non_strings():
    for value in ("afx:v1:foo:7", "afx:v1:approvex:7", "afx:v1:claim;7", "afx:v1;claim:7",
                  "afx:v1:claim 7"):
        with pytest.raises(CustomIdError):
            decode_custom_id(value)
    with pytest.raises(CustomIdError):
        decode_custom_id(7)
    with pytest.raises(CustomIdError):
        encode_custom_id("approve", 0)


def test_state_refuses_a_duplicate_approval_id_and_a_duplicate_message_id_on_their_own():
    with pytest.raises(StateError):
        BotState(cursor=0, high_watermark=0, approvals=(_record(), _record(message_id=102)))
    with pytest.raises(StateError):
        BotState(cursor=0, high_watermark=0, approvals=(_record(), _record(approval_id=8)))


def test_a_corrupt_snapshot_file_is_refused_and_never_read_as_an_empty_state(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        (tmp_path / "bot.snapshot").write_text("{not json", encoding="ascii")
        with pytest.raises(StateError):
            load(fd)
    finally:
        os.close(fd)


def test_a_pending_display_edit_survives_the_snapshot_and_must_be_a_bool(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        state = BotState.empty().with_record(_record(state="done", edit_pending=True))
        save(fd, state)
        assert load(fd) == state and load(fd).approvals[0].edit_pending is True
    finally:
        os.close(fd)
    with pytest.raises(StateError):
        _record(edit_pending=1)
