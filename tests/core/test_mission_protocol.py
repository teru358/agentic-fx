"""mission_protocol の seq 検証 + フレーム I/O (プラン8, 設計書 §4.3)。"""
from __future__ import annotations

import io

import pytest

from agentic_fx.core.mission_protocol import (
    ProtocolError, SeqTracker, read_frame, write_frame,
)


def test_seq_tracker_accepts_monotonic_sequence():
    t = SeqTracker()
    t.check(1)
    t.check(2)
    t.check(3)


def test_seq_tracker_rejects_duplicate():
    t = SeqTracker()
    t.check(1)
    with pytest.raises(ProtocolError, match="seq"):
        t.check(1)


def test_seq_tracker_rejects_gap():
    t = SeqTracker()
    t.check(1)
    with pytest.raises(ProtocolError, match="seq"):
        t.check(3)


def test_seq_tracker_rejects_regression():
    t = SeqTracker()
    t.check(1)
    t.check(2)
    with pytest.raises(ProtocolError, match="seq"):
        t.check(1)


def test_write_then_read_frame_roundtrip():
    buf = io.BytesIO()
    write_frame(buf, {"type": "ready", "seq": 1, "ok": True})
    buf.seek(0)
    frame = read_frame(buf)
    assert frame == {"type": "ready", "seq": 1, "ok": True}


def test_read_frame_returns_none_on_eof():
    buf = io.BytesIO(b"")
    assert read_frame(buf) is None
