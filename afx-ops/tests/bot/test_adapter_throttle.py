"""429 / 503 の後は Retry-After に従って record 単位で次の問合せを先送りする。"""
from __future__ import annotations

import asyncio
import os

import pytest

from afx_ops.api_client import Reply, _parse_response, _reply
from afx_ops.bot.adapter import BotAdapter, BotSettings
from afx_ops.gate.service import GateService
from afx_ops.gate.state import ApprovalRecord, BotState

GUILD = 12345678901234567
CHANNEL = 12345678901234568
SETTINGS = BotSettings(guild_id=GUILD, channel_id=CHANNEL, mention_role_id=None,
                       mention_user_ids=())


def test_retry_after_is_kept_only_in_its_integer_form():
    def parse(header: bytes):
        return _parse_response(b"HTTP/1.1 429 X\r\nContent-Length: 2\r\n" + header + b"\r\n{}")

    assert parse(b"Retry-After: 30\r\n") == (429, b"{}", 30)
    assert parse(b"") == (429, b"{}", None)
    assert parse(b"Retry-After: Wed, 21 Oct 2026 07:28:00 GMT\r\n")[2] is None
    assert parse(b"Retry-After: -1\r\n")[2] is None
    assert _reply(429, b'{"ok":false,"error":{"code":"rate"}}', 30) == Reply(429, None, "rate", 30)


class _Client:
    def __init__(self, status: int, retry_after: int | None) -> None:
        self.status, self.retry_after = status, retry_after
        self.polls = 0

    def events(self, *, after: int, limit: int) -> Reply:
        return Reply(200, {"events": [], "high_watermark": 0}, None)

    def approvals(self, *, limit: int) -> Reply:
        return Reply(200, {"approvals": [{"id": 7}]}, None)

    def poll(self, job) -> Reply:
        self.polls += 1
        return Reply(self.status, None, "rate", self.retry_after)


@pytest.fixture
def rounds(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    made = []

    def build(status: int, retry_after: int | None):
        record = ApprovalRecord(
            approval_id=7, message_id=101, guild_id=GUILD, channel_id=CHANNEL,
            payload_sha256="a" * 64, display_generation=1, state="in_flight",
            actor="discord:12345678901234569", action="approve", nonce="n", expiry=10**10,
            job_id="0" * 32)
        client = _Client(status, retry_after)
        gate = GateService(BotState.empty().with_record(record), guild_id=GUILD,
                           channel_id=CHANNEL, allowlist_user_ids=(), client=client,
                           state_dir_fd=fd)
        clock = [1000.0]
        adapter = BotAdapter(gate=gate, client=client, dir_fd=fd, settings=SETTINGS,
                             discord_client=None, now=lambda: clock[0])
        made.append(adapter)

        async def poll_at(offset: float) -> int:
            clock[0] = 1000.0 + offset
            await adapter.poll_once()
            return client.polls
        return poll_at

    yield build
    for adapter in made:
        adapter.close()
    os.close(fd)


def test_a_429_with_retry_after_defers_that_record_until_the_stated_time(rounds):
    poll_at = rounds(429, 30)

    async def go() -> list[int]:
        return [await poll_at(0), await poll_at(2), await poll_at(4), await poll_at(29),
                await poll_at(31)]

    assert asyncio.run(go()) == [1, 1, 1, 1, 2]


def test_a_503_without_retry_after_waits_the_default_and_the_wait_is_capped(rounds):
    from afx_ops.bot import adapter as adapter_module
    poll_at = rounds(503, None)
    default = adapter_module.THROTTLE_DEFAULT_SECONDS

    async def go() -> list[int]:
        return [await poll_at(0), await poll_at(default - 1), await poll_at(default + 1)]

    assert asyncio.run(go()) == [1, 1, 2]
    poll_at = rounds(429, 100000)

    async def capped() -> list[int]:
        return [await poll_at(0), await poll_at(299), await poll_at(301)]

    assert asyncio.run(capped()) == [1, 1, 2]


class _DecisionClient:
    def __init__(self, status: int, retry_after: int | None) -> None:
        self.reply = Reply(status, None, "rate", retry_after)
        self.detail_calls = 0

    def approval(self, approval_id: int) -> Reply:
        self.detail_calls += 1
        return Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None)

    def approve(self, actor, approval_id: int, *, payload_sha256: str):
        from afx_ops.api_client import Decision
        return Decision(self.reply, None)


@pytest.mark.parametrize("status", [429, 503])
def test_a_throttled_decision_post_defers_the_next_lookup_until_the_stated_time(tmp_path, status):
    from afx_ops.gate.types import Interaction, encode_custom_id
    user = 12345678901234569
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    client = _DecisionClient(status, 30)
    record = ApprovalRecord(approval_id=7, message_id=101, guild_id=GUILD, channel_id=CHANNEL,
                            payload_sha256="a" * 64, display_generation=1, state="fresh")
    gate = GateService(BotState.empty().with_record(record), guild_id=GUILD, channel_id=CHANNEL,
                       allowlist_user_ids=(user,), client=client, state_dir_fd=fd,
                       nonce_factory=lambda: "n" * 32)
    clock = [1000.0]
    adapter = BotAdapter(gate=gate, client=client, dir_fd=fd, settings=SETTINGS,
                         discord_client=None, now=lambda: clock[0])
    try:
        def press(kind: str, message: int):
            return adapter._decide(Interaction(
                custom_id=encode_custom_id(kind, 7), guild_id=GUILD, channel_id=CHANNEL,
                user_id=user, message_id=message, display_generation=1))
        press("approve", 101)
        assert gate.bind_confirm(7, 201)
        assert "refused" not in press("confirm", 201).text
        # 決定の POST が 429 / 503 のとき record は従来どおり (in_flight、正本の再照合待ち) で、
        # 次の再照合だけが先送りされる
        assert gate.state.record(7).state == "in_flight"
        calls = client.detail_calls
        adapter._reconcile_in_flight()
        assert client.detail_calls == calls and not adapter._poll_due(7)
        clock[0] += 29
        assert not adapter._poll_due(7)
        clock[0] += 2
        assert adapter._poll_due(7)
    finally:
        adapter.close()
        os.close(fd)
