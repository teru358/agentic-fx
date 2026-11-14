"""snapshot 層の I/O の失敗と内容の不正を別の型に分け、bot の終了コード (79 / 78) に写す。"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from afx_ops.api_client import Reply
from afx_ops.bot import exits
from afx_ops.bot.adapter import BotAdapter, BotFailure, BotSettings, open_state_dir
from afx_ops.gate import state as gate_state
from afx_ops.gate.service import GateService
from afx_ops.gate.state import BotState, StateError, StateIOError
from afx_ops.snapshot import (SnapshotContentError, SnapshotError, SnapshotIOError, read_snapshot,
                              write_snapshot)

GUILD = 12345678901234567
CHANNEL = 12345678901234568
SETTINGS = BotSettings(guild_id=GUILD, channel_id=CHANNEL, mention_role_id=None, mention_user_ids=())


def _fd(path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def _closed_fd(tmp_path) -> int:
    fd = _fd(tmp_path)
    os.close(fd)
    return fd


def test_the_snapshot_error_types_are_distinct_siblings_of_one_base():
    assert issubclass(SnapshotIOError, SnapshotError) and issubclass(SnapshotContentError, SnapshotError)
    assert not issubclass(SnapshotIOError, SnapshotContentError)
    assert not issubclass(SnapshotContentError, SnapshotIOError)


@pytest.mark.parametrize("payload", [b"not json", b'{"version": 99}', b"[]", b"\xff\xfe",
                                      b'{"version": true}'])
def test_unreadable_content_is_a_content_error(tmp_path, payload):
    (tmp_path / "x.snapshot").write_bytes(payload)
    fd = _fd(tmp_path)
    try:
        with pytest.raises(SnapshotContentError):
            read_snapshot(fd, "x.snapshot", 1)
    finally:
        os.close(fd)


def test_a_symlink_a_directory_and_a_fifo_are_content_errors_not_io_failures(tmp_path):
    (tmp_path / "real").write_text("{}")
    os.symlink("real", tmp_path / "link.snapshot")
    os.mkdir(tmp_path / "dir.snapshot")
    os.mkfifo(tmp_path / "fifo.snapshot")
    fd = _fd(tmp_path)
    try:
        for name in ("link.snapshot", "dir.snapshot", "fifo.snapshot"):
            with pytest.raises(SnapshotContentError):
                read_snapshot(fd, name, 1)
    finally:
        os.close(fd)


def test_an_os_failure_on_read_or_write_is_an_io_error(tmp_path):
    bad = _closed_fd(tmp_path)
    with pytest.raises(SnapshotIOError):
        read_snapshot(bad, "x.snapshot", 1)
    with pytest.raises(SnapshotIOError):
        write_snapshot(bad, "x.snapshot", {"version": 1})


def test_a_failing_read_or_fsync_after_a_successful_open_is_an_io_error(tmp_path, monkeypatch):
    fd = _fd(tmp_path)
    try:
        write_snapshot(fd, "x.snapshot", {"version": 1})

        def boom(*args, **kwargs):
            raise OSError(5, "input/output error")
        with monkeypatch.context() as patch:
            patch.setattr(os, "read", boom)
            with pytest.raises(SnapshotIOError):
                read_snapshot(fd, "x.snapshot", 1)
        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", boom)
            with pytest.raises(SnapshotIOError):
                write_snapshot(fd, "y.snapshot", {"version": 1})
        assert not [n for n in os.listdir(tmp_path) if n.startswith(".y.snapshot.tmp-")]
    finally:
        os.close(fd)


def test_a_missing_snapshot_is_none_and_an_unencodable_object_is_a_content_error(tmp_path):
    fd = _fd(tmp_path)
    try:
        assert read_snapshot(fd, "none.snapshot", 1) is None
        with pytest.raises(SnapshotContentError):
            write_snapshot(fd, "x.snapshot", {"version": float("nan")})
    finally:
        os.close(fd)


def test_the_gate_state_maps_the_two_types_to_two_state_errors(tmp_path):
    (tmp_path / gate_state.SNAPSHOT_NAME).write_text("{broken")
    fd = _fd(tmp_path)
    try:
        with pytest.raises(StateError) as content:
            gate_state.load(fd)
        assert not isinstance(content.value, StateIOError)
        assert content.value.reason == "bot_state_invalid"
    finally:
        os.close(fd)
    bad = _closed_fd(tmp_path)
    with pytest.raises(StateIOError) as io:
        gate_state.load(bad)
    assert io.value.reason == "bot_state_unavailable"
    with pytest.raises(StateIOError):
        gate_state.save(bad, BotState.empty())


def _adapter(fd):
    gate = GateService(BotState.empty(), guild_id=GUILD, channel_id=CHANNEL)
    return BotAdapter(gate=gate, client=None, dir_fd=fd, settings=SETTINGS, discord_client=None)


def test_a_content_failure_on_the_cursor_snapshot_is_bot_state_invalid_and_rc_78(tmp_path):
    (tmp_path / gate_state.SNAPSHOT_NAME).write_text("{broken")
    fd = _fd(tmp_path)
    adapter = _adapter(fd)
    try:
        with pytest.raises(BotFailure) as info:
            asyncio.run(adapter.prepare())
    finally:
        adapter.close()
        os.close(fd)
    assert info.value.reason == "bot_state_invalid"
    assert exits.rc_for_reason(info.value.reason) == exits.RC_BOT_PERMANENT


def test_an_io_failure_on_the_cursor_snapshot_is_transient_for_load_and_for_save(tmp_path):
    bad = _closed_fd(tmp_path)
    adapter = _adapter(bad)
    try:
        with pytest.raises(BotFailure) as load_failure:
            asyncio.run(adapter.prepare())
        with pytest.raises(BotFailure) as save_failure:
            adapter._save()
    finally:
        adapter.close()
    for failure in (load_failure, save_failure):
        assert failure.value.reason == "bot_state_unavailable"
        assert exits.rc_for_reason(failure.value.reason) == exits.RC_BOT_TRANSIENT


def test_open_state_dir_failure_is_transient_and_distinct_from_content(tmp_path):
    with pytest.raises(BotFailure) as info:
        open_state_dir(tmp_path / "missing")
    assert info.value.reason == "bot_state_unavailable"


# ------------------------------------------------------------------ 決定的に再発する入力は恒久

class _Client:
    def __init__(self, page) -> None:
        self.page = page

    def events(self, *, after, limit):
        return self.page

    def approvals(self, *, limit):
        return Reply(200, {"approvals": []}, None)

    def approval(self, approval_id, *, timeout=None):
        return Reply(200, {"status": "pending"}, None)


def _poll_failure(tmp_path, page, *, state=None):
    fd = _fd(tmp_path)
    client = _Client(page)
    gate = GateService(state or gate_state.initialize_event_cursor(BotState.empty(), 5),
                       guild_id=GUILD, channel_id=CHANNEL, client=client)
    adapter = BotAdapter(gate=gate, client=client, dir_fd=fd, settings=SETTINGS,
                         discord_client=SimpleNamespace(
                             get_partial_messageable=lambda *a, **k: None))
    try:
        with pytest.raises(BotFailure) as info:
            asyncio.run(adapter.poll_once())
    finally:
        adapter.close()
        os.close(fd)
    return info.value.reason


def test_a_malformed_event_page_is_events_invalid_rc_78(tmp_path):
    reason = _poll_failure(tmp_path, Reply(200, {"events": "x", "high_watermark": 5}, None))
    assert reason == "events_invalid" and exits.rc_for_reason(reason) == 78


def test_a_high_watermark_that_went_backwards_is_rc_78(tmp_path):
    state = gate_state.observe_high(gate_state.initialize_event_cursor(BotState.empty(), 9), 9)
    reason = _poll_failure(tmp_path, Reply(200, {"events": [], "high_watermark": 3}, None),
                           state=state)
    assert reason == "high_watermark_regressed" and exits.rc_for_reason(reason) == 78


def test_an_event_that_cannot_be_planned_is_payload_invalid_rc_78(tmp_path):
    event = {"id": 6, "ts": "2026-10-22T00:00:00+00:00", "code": "approval_state_changed",
             "ref": "approval:7", "fields": {"state": "approved"}}

    def broken_plan(*args, **kwargs):
        raise ValueError("bad")
    fd = _fd(tmp_path)
    client = _Client(Reply(200, {"events": [event], "high_watermark": 6}, None))
    gate = GateService(gate_state.initialize_event_cursor(BotState.empty(), 5), guild_id=GUILD,
                       channel_id=CHANNEL, client=client)
    gate.plan_event = broken_plan
    adapter = BotAdapter(gate=gate, client=client, dir_fd=fd, settings=SETTINGS,
                         discord_client=SimpleNamespace(
                             get_partial_messageable=lambda *a, **k: None))
    try:
        with pytest.raises(BotFailure) as info:
            asyncio.run(adapter.deliver_events())
    finally:
        adapter.close()
        os.close(fd)
    assert info.value.reason == "payload_invalid" and exits.rc_for_reason("payload_invalid") == 78
