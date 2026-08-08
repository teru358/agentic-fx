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


# --- レビュー 1 周目 (codex I-1 / sonnet C1・B) の反映で追加 ---------------


def test_read_frame_raises_protocol_error_on_malformed_json():
    """`ProtocolError` は「プロトコル契約に反する入力全般の単一表現」と
    定義されている。旧実装は `json.JSONDecodeError` を素通ししており、
    親 (Task 10) が `except ProtocolError` で統一的に扱えなかった。"""
    buf = io.BytesIO(b"{not-json}\n")
    with pytest.raises(ProtocolError, match="malformed frame line"):
        read_frame(buf)


def test_read_frame_raises_protocol_error_on_invalid_utf8():
    """不正 UTF-8 も同じ単一表現に正規化する (`UnicodeDecodeError` は
    `ValueError` の派生なので同じ except で捕まる)。"""
    buf = io.BytesIO(b'"\xff\xfe"\n')
    with pytest.raises(ProtocolError, match="malformed frame line"):
        read_frame(buf)


def test_read_frame_rejects_non_object_frames():
    """JSON としては妥当でもフレーム契約 (JSON object) に反する入力。
    型注釈上の `dict` を裏切ったまま返すと受け手の `.get()` が
    `AttributeError` になり、単一表現が崩れる。"""
    for payload in (b"[1, 2]\n", b'"ready"\n', b"42\n", b"null\n"):
        with pytest.raises(ProtocolError, match="must be a JSON object"):
            read_frame(io.BytesIO(payload))


def test_write_frame_flushes_immediately():
    """`flush()` が無いと `BufferedWriter` の内部バッファに留まり、相手側の
    fd からは読めない。実 subprocess を spawn せず `os.pipe()` 越しの実 fd
    で観測できる (変異 追加C の pin — 1 周目レビューで「後続 task 送りは
    過度に保守的」と両レビュアーから指摘された)。"""
    import os

    r, w = os.pipe()
    os.set_blocking(r, False)
    stream = os.fdopen(w, "wb")  # 既定バッファリング
    try:
        write_frame(stream, {"type": "ready", "seq": 1})
        assert os.read(r, 4096) == b'{"type": "ready", "seq": 1}\n'
    finally:
        stream.close()
        os.close(r)
