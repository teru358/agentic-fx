from __future__ import annotations

import dataclasses
import os

import pytest

from afx_ops.notify.state import (NotifyState, StateError, advance_delivery,
                                  initialize_event_cursor, load, migrate_v2, observe_high, save)


_STARTED = NotifyState(cursor=0, high_watermark=0, event_cursor_initialized=True)


def _open_dir(path):
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def test_snapshot_round_trip_uses_the_state_directory_fd(tmp_path):
    fd = _open_dir(tmp_path)
    try:
        state = advance_delivery(_STARTED, 7)
        save(fd, state)
        assert load(fd) == state
    finally:
        os.close(fd)


def test_delivery_advances_the_cursor_only():
    state = _STARTED
    for event_id in (1, 2, 5):
        state = advance_delivery(state, event_id)
    assert (state.cursor, state.high_watermark) == (5, 5)
    assert [f.name for f in dataclasses.fields(NotifyState)] == [
        "cursor", "high_watermark", "event_cursor_initialized"]


def test_state_rejects_corruption_and_non_monotonic_delivery(tmp_path):
    fd = _open_dir(tmp_path)
    try:
        (tmp_path / "notify.snapshot").write_text(
            '{"version":3,"cursor":4}', encoding="ascii")
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
        advance_delivery(_STARTED, 0)


def test_high_watermark_is_kept_saved_and_never_below_the_cursor(tmp_path):
    state = observe_high(advance_delivery(_STARTED, 7), 30)
    assert (state.cursor, state.high_watermark) == (7, 30)
    assert advance_delivery(state, 9).high_watermark == 30
    assert advance_delivery(_STARTED, 7).high_watermark == 7
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


# ---------------------------------------------------------------- 初回 cursor (AC-25)

_V2 = b'{"cursor": 7, "high_watermark": 30, "version": 2}'


def _snap(tmp_path) -> bytes:
    return (tmp_path / "notify.snapshot").read_bytes()


def test_ac25_a_new_state_is_not_started_and_absence_differs_from_a_saved_zero(tmp_path):
    fd = _open_dir(tmp_path)
    try:
        assert NotifyState.empty().event_cursor_initialized is False
        assert load(fd).event_cursor_initialized is False
        save(fd, initialize_event_cursor(NotifyState.empty(), 0))
        assert load(fd).event_cursor_initialized is True
        assert load(fd) != NotifyState.empty()
        save(fd, NotifyState.empty())
        assert _snap(tmp_path) == (b'{"cursor": 0, "event_cursor_initialized": false, '
                                   b'"high_watermark": 0, "version": 3}')
    finally:
        os.close(fd)


def test_ac25_v2_migrates_to_true_byte_for_byte(tmp_path):
    (tmp_path / "notify.snapshot").write_bytes(_V2)
    fd = _open_dir(tmp_path)
    try:
        state = load(fd)
        assert state == NotifyState(cursor=7, high_watermark=30, event_cursor_initialized=True)
        save(fd, state)
        raw = (b'{"cursor": 7, "event_cursor_initialized": true, "high_watermark": 30, '
               b'"version": 3}')
        assert _snap(tmp_path) == raw
        save(fd, load(fd))
        assert _snap(tmp_path) == raw
    finally:
        os.close(fd)


def test_ac25_a_v2_snapshot_at_zero_zero_is_also_true(tmp_path):
    # notify は最初の取得の前に保存しないので、v2 は常に配送の実績の上にある
    (tmp_path / "notify.snapshot").write_bytes(b'{"cursor": 0, "high_watermark": 0, "version": 2}')
    fd = _open_dir(tmp_path)
    try:
        assert load(fd).event_cursor_initialized is True
    finally:
        os.close(fd)


@pytest.mark.parametrize("raw", [
    b'{"cursor": 1, "high_watermark": 1, "version": 3}',
    b'{"cursor": 1, "event_cursor_initialized": 1, "high_watermark": 1, "version": 3}',
    b'{"cursor": 1, "event_cursor_initialized": true, "high_watermark": 1, "version": 2}',
    b'{"cursor": 1, "high_watermark": 1, "version": 4}',
])
def test_ac25_other_shapes_and_versions_are_refused(tmp_path, raw):
    (tmp_path / "notify.snapshot").write_bytes(raw)
    fd = _open_dir(tmp_path)
    try:
        with pytest.raises(StateError):
            load(fd)
    finally:
        os.close(fd)


def test_ac25_the_start_sets_cursor_high_and_flag_together_once_and_gates_delivery():
    started = initialize_event_cursor(NotifyState.empty(), 9)
    assert (started.cursor, started.high_watermark, started.event_cursor_initialized) == (9, 9, True)
    with pytest.raises(StateError):
        initialize_event_cursor(started, 12)
    with pytest.raises(StateError):
        advance_delivery(NotifyState.empty(), 1)
    assert observe_high(started, 20).event_cursor_initialized is True
    assert advance_delivery(started, 10).event_cursor_initialized is True


def test_ac25_migrate_v2_accepts_only_a_v2_object():
    for version in (1, 3, 4, "2", None):
        with pytest.raises(StateError):
            migrate_v2({"version": version, "cursor": 1, "high_watermark": 1})


def test_ac25_a_delivery_never_repeats_an_id_and_the_watermark_never_falls():
    state = advance_delivery(initialize_event_cursor(NotifyState.empty(), 5), 6)
    with pytest.raises(StateError):
        advance_delivery(state, 6)
    with pytest.raises(StateError):
        advance_delivery(state, 5)
