from __future__ import annotations

import dataclasses
import os

import pytest

from afx_ops.notify.state import (NotifyState, StateError, advance_delivery, load,
                                  observe_high, save)


def _open_dir(path):
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def test_snapshot_round_trip_uses_the_state_directory_fd(tmp_path):
    fd = _open_dir(tmp_path)
    try:
        state = advance_delivery(NotifyState.empty(), 7)
        save(fd, state)
        assert load(fd) == state
    finally:
        os.close(fd)


def test_delivery_advances_the_cursor_only():
    state = NotifyState.empty()
    for event_id in (1, 2, 5):
        state = advance_delivery(state, event_id)
    assert (state.cursor, state.high_watermark) == (5, 5)
    assert [f.name for f in dataclasses.fields(NotifyState)] == ["cursor", "high_watermark"]


def test_state_rejects_corruption_and_non_monotonic_delivery(tmp_path):
    fd = _open_dir(tmp_path)
    try:
        (tmp_path / "notify.snapshot").write_text(
            '{"version":2,"cursor":4}', encoding="ascii")
        with pytest.raises(StateError):
            load(fd)
        # 配送済み id の一覧を持つ旧い形は読まない
        (tmp_path / "notify.snapshot").write_text(
            '{"version":1,"cursor":4,"high_watermark":4,"delivered_event_ids":[4]}',
            encoding="ascii")
        with pytest.raises(StateError):
            load(fd)
    finally:
        os.close(fd)
    with pytest.raises(StateError):
        advance_delivery(NotifyState.empty(), 0)


def test_high_watermark_is_kept_saved_and_never_below_the_cursor(tmp_path):
    state = observe_high(advance_delivery(NotifyState.empty(), 7), 30)
    assert (state.cursor, state.high_watermark) == (7, 30)
    assert advance_delivery(state, 9).high_watermark == 30
    assert advance_delivery(NotifyState.empty(), 7).high_watermark == 7
    assert observe_high(state, 10).high_watermark == 30
    fd = _open_dir(tmp_path)
    try:
        save(fd, state)
        assert load(fd) == state
        (tmp_path / "notify.snapshot").write_text(
            '{"version":2,"cursor":4,"high_watermark":3}',
            encoding="ascii")
        with pytest.raises(StateError):
            load(fd)
    finally:
        os.close(fd)


@pytest.mark.parametrize("cursor", ["2", True])
def test_state_rejects_a_non_integer_cursor_when_built_directly(cursor):
    with pytest.raises(StateError):
        NotifyState(cursor=cursor, high_watermark=2)
