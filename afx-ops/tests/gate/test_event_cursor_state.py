"""bot snapshot の event cursor flag と v4 → v5 の migration (AC-25)。"""
from __future__ import annotations

import os

import pytest

from afx_ops.gate.state import (BotState, StateError, advance_event, initialize_event_cursor,
                                load, migrate_v4, observe_high, save)

_RECORD = ('{"action": null, "actor": null, "approval_id": 7, "channel_id": 12345678901234568, '
           '"confirm_message_id": null, "display_generation": 1, "edit_pending": false, '
           '"expiry": null, "guild_id": 12345678901234567, "job_id": null, "message_id": 101, '
           '"nonce": null, "nonce_used": false, "payload_sha256": "' + "a" * 64 + '", '
           '"repost_generation": null, "state": "fresh"}')


def _v4(cursor: int, high: int, approvals: str) -> bytes:
    return (f'{{"approvals": [{approvals}], "cursor": {cursor}, "high_watermark": {high}, '
            '"version": 4}').encode("ascii")


def _v5(cursor: int, high: int, approvals: str, flag: str) -> bytes:
    return (f'{{"approvals": [{approvals}], "cursor": {cursor}, "event_cursor_initialized": '
            f'{flag}, "high_watermark": {high}, "version": 5}}').encode("ascii")


@pytest.fixture
def fd(tmp_path):
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    yield descriptor
    os.close(descriptor)


def _bytes(tmp_path) -> bytes:
    return (tmp_path / "bot.snapshot").read_bytes()


def test_ac25_a_new_state_is_not_started_and_an_absent_snapshot_is_not_a_saved_zero(tmp_path, fd):
    assert BotState.empty().event_cursor_initialized is False
    assert load(fd).event_cursor_initialized is False
    save(fd, BotState(cursor=0, high_watermark=0, approvals=(), event_cursor_initialized=True))
    assert load(fd).event_cursor_initialized is True
    assert load(fd) != BotState.empty()


def test_ac25_saving_an_empty_state_writes_flag_false_in_v5(tmp_path, fd):
    save(fd, BotState.empty())
    assert _bytes(tmp_path) == _v5(0, 0, "", "false")


@pytest.mark.parametrize("cursor, high, approvals, flag", [
    (3, 5, _RECORD, "true"),
    (2, 2, "", "true"),
    (0, 0, "", "false"),
    (0, 0, _RECORD, "false"),
])
def test_ac25_v4_migrates_byte_for_byte_and_the_zero_zero_shape_becomes_false(
        tmp_path, fd, cursor, high, approvals, flag):
    (tmp_path / "bot.snapshot").write_bytes(_v4(cursor, high, approvals))
    state = load(fd)
    assert state.event_cursor_initialized is (flag == "true")
    assert (state.cursor, state.high_watermark) == (cursor, high)
    save(fd, state)
    assert _bytes(tmp_path) == _v5(cursor, high, approvals, flag)
    # 保存後は v5 として読める (migration は 1 回きり)
    assert load(fd) == state


def test_ac25_a_v5_snapshot_round_trips_byte_for_byte(tmp_path, fd):
    raw = _v5(3, 5, _RECORD, "true")
    (tmp_path / "bot.snapshot").write_bytes(raw)
    save(fd, load(fd))
    assert _bytes(tmp_path) == raw


@pytest.mark.parametrize("raw", [
    b'{"approvals": [], "cursor": 0, "high_watermark": 0, "version": 5}',
    b'{"approvals": [], "cursor": 0, "event_cursor_initialized": 1, "high_watermark": 0, '
    b'"version": 5}',
    b'{"approvals": [], "cursor": 0, "event_cursor_initialized": true, "high_watermark": 0, '
    b'"version": 4}',
    b'{"approvals": [], "cursor": 0, "event_cursor_initialized": false, "high_watermark": 0, '
    b'"version": 3}',
    b'{"approvals": [], "cursor": 0, "high_watermark": 0, "version": 6}',
    b'{"approvals": [], "cursor": "0", "high_watermark": 0, "version": 4}',
])
def test_ac25_other_shapes_and_versions_are_refused(tmp_path, fd, raw):
    (tmp_path / "bot.snapshot").write_bytes(raw)
    with pytest.raises(StateError):
        load(fd)


def test_ac25_the_start_transition_sets_cursor_high_and_flag_together_once():
    started = initialize_event_cursor(BotState.empty(), 9)
    assert (started.cursor, started.high_watermark, started.event_cursor_initialized) == (9, 9, True)
    with pytest.raises(StateError):
        initialize_event_cursor(started, 12)
    with pytest.raises(StateError):
        initialize_event_cursor(BotState.empty(), -1)
    assert initialize_event_cursor(BotState.empty(), 0).event_cursor_initialized is True


def test_ac25_a_cursor_cannot_advance_before_the_start():
    with pytest.raises(StateError):
        advance_event(BotState.empty(), 1)
    assert observe_high(initialize_event_cursor(BotState.empty(), 2), 5).event_cursor_initialized


@pytest.mark.parametrize("cursor, high", [(1, 1), (1, 2)])
def test_ac25_the_smallest_delivery_footprints_migrate_to_true(cursor, high):
    obj = {"version": 4, "cursor": cursor, "high_watermark": high, "approvals": []}
    assert migrate_v4(obj)["event_cursor_initialized"] is True


@pytest.mark.parametrize("version", [3, 5, 6, "4", None])
def test_ac25_migrate_v4_accepts_only_a_v4_object(version):
    obj = {"version": version, "cursor": 1, "high_watermark": 1, "approvals": []}
    with pytest.raises(StateError):
        migrate_v4(obj)


def test_ac25_advance_and_observe_never_move_the_cursor_or_watermark_backwards():
    started = advance_event(initialize_event_cursor(BotState.empty(), 5), 6, high_watermark=9)
    for event_id, high in ((6, 9), (5, 9)):
        with pytest.raises(StateError):
            advance_event(started, event_id, high_watermark=high)
    with pytest.raises(StateError):
        advance_event(started, 7, high_watermark=8)
    with pytest.raises(StateError):
        observe_high(started, 8)
    assert observe_high(started, 9).high_watermark == 9


@pytest.mark.parametrize("high", [1, 40])
def test_a_v4_snapshot_with_a_watermark_but_no_cursor_is_not_delivered_and_loses_the_watermark(
        tmp_path, fd, high):
    # v4 は event を配送しない周回でも watermark だけを進めて保存する。cursor が 0 のままなら未配送
    obj = {"version": 4, "cursor": 0, "high_watermark": high, "approvals": []}
    migrated = migrate_v4(obj)
    assert migrated["event_cursor_initialized"] is False
    assert (migrated["cursor"], migrated["high_watermark"]) == (0, 0)
    (tmp_path / "bot.snapshot").write_bytes(_v4(0, high, ""))
    state = load(fd)
    assert (state.cursor, state.high_watermark, state.event_cursor_initialized) == (0, 0, False)


@pytest.mark.parametrize("cursor, high", [(100, 100), (0, 5), (3, 7)])
def test_a_state_that_is_not_started_must_have_cursor_and_watermark_zero(tmp_path, fd, cursor, high):
    with pytest.raises(StateError):
        BotState(cursor=cursor, high_watermark=high, approvals=())
    (tmp_path / "bot.snapshot").write_bytes(_v5(cursor, high, "", "false"))
    with pytest.raises(StateError):
        load(fd)
