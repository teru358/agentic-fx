"""asyncio の socket transport を sendmsg なしで書かせる切替の互換性。"""
from __future__ import annotations

import asyncio
import socket
from asyncio import selector_events

import pytest

from afx_ops.web import runtime


def test_missing_switch_fails_closed_before_serving(monkeypatch):
    monkeypatch.delattr(selector_events, "_HAS_SENDMSG")
    called = []
    with pytest.raises(RuntimeError, match="web_transport_incompatible"):
        runtime.run(object(), client=object(), on_serving=lambda: called.append(1))
    assert called == []


def test_switch_is_set_on_a_present_flag(monkeypatch):
    monkeypatch.setattr(selector_events, "_HAS_SENDMSG", True)
    runtime.use_send_only_transport()
    assert selector_events._HAS_SENDMSG is False


def test_buffered_write_goes_through_send_without_sendmsg(monkeypatch):
    monkeypatch.setattr(selector_events, "_HAS_SENDMSG", True)
    runtime.use_send_only_transport()
    payload = b"z" * (4 * 1024 * 1024)

    async def main() -> bytes:
        loop = asyncio.get_running_loop()
        left, right = socket.socketpair()
        left.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        transport, _ = await loop.connect_accepted_socket(asyncio.Protocol, left)
        # 送信 buffer に収まらない量を書いて、transport の buffer 経路を通す
        transport.write(payload)
        assert transport.get_write_buffer_size() > 0
        assert "sendmsg" not in transport._write_ready.__name__
        right.setblocking(False)
        received = bytearray()
        while len(received) < len(payload):
            try:
                chunk = right.recv(1 << 20)
            except BlockingIOError:
                await asyncio.sleep(0.001)
                continue
            if not chunk:
                break
            received += chunk
        transport.close()
        right.close()
        return bytes(received)

    assert asyncio.run(main()) == payload
