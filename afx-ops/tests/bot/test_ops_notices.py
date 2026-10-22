from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from afx_ops.api_client import Reply
from afx_ops.bot.adapter import BotAdapter, BotSettings, _plugin_name
from afx_ops.event_outcome import SkipKnown
from afx_ops.gate.service import GateService
from afx_ops.gate.state import BotState, initialize_event_cursor, load, save


TS = "2026-10-22T00:00:00+00:00"


def event(event_id: int, code: str, fields: dict, ref: str) -> dict:
    return {"id": event_id, "ts": TS, "code": code, "ref": ref, "fields": fields}


class _Client:
    def __init__(self, events, detail=Reply(200, {"change": {"name": "sma_cross"}}, None)):
        self.events_reply = Reply(200, {"events": events, "high_watermark": len(events)}, None)
        self.detail = detail
        self.approval_calls = 0

    def events(self, *, after, limit):
        return self.events_reply

    def approval(self, approval_id):
        self.approval_calls += 1
        return self.detail

    def approvals(self, *, limit):
        return Reply(200, {"approvals": []}, None)


class _Channel:
    def __init__(self): self.sent = []
    async def send(self, content, *, allowed_mentions): self.sent.append(content)


def _adapter(tmp_path, client, *, ops_notices="default"):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    save(fd, initialize_event_cursor(BotState.empty(), 0))
    settings = BotSettings(guild_id=12345678901234567, channel_id=12345678901234568,
                           mention_role_id=None, mention_user_ids=(), ops_notices=ops_notices)
    gate = GateService(initialize_event_cursor(BotState.empty(), 0), guild_id=settings.guild_id, channel_id=settings.channel_id,
                       client=client)
    channel = _Channel()
    discord_client = SimpleNamespace(get_partial_messageable=lambda *a, **k: channel)
    return BotAdapter(gate=gate, client=client, dir_fd=fd, settings=settings,
                      discord_client=discord_client), channel, fd


async def _deliver(adapter):
    await adapter.deliver_events()


@pytest.mark.parametrize("ops_notices, count", [("default", 4), ("all", 6)])
def test_ac18_default_filters_known_events_and_advances_cursor(tmp_path, ops_notices, count):
    events = [
        event(1, "job_state_changed", {"job_kind": "approve", "state": "done", "result_code": "ok"}, "job:" + "a" * 32),
        event(2, "ops_request_terminal", {"endpoint_code": "approve", "result_code": "ok", "authenticated_principal": "api:ops-bot"}, "audit:1"),
        event(3, "job_state_changed", {"job_kind": "approve", "state": "failed", "result_code": "unavailable"}, "job:" + "b" * 32),
        event(4, "guard_state_changed", {"guard_kind": "kill_switch", "state": "latched", "generation": 1}, "instance"),
        event(5, "service_health_changed", {"component_code": "api", "health_code": "degraded"}, "instance"),
        event(6, "approval_state_changed", {"state": "approved", "decision_code": "approve", "decided_by": "api:ops-bot"}, "approval:8"),
    ]
    client = _Client(events)
    adapter, channel, fd = _adapter(tmp_path, client, ops_notices=ops_notices)
    try:
        asyncio.run(_deliver(adapter))
        assert len(channel.sent) == count
        assert (load(fd).cursor, load(fd).high_watermark) == (6, 6)
        assert client.approval_calls == 1
    finally:
        adapter.close(); os.close(fd)


@pytest.mark.parametrize("detail", [
    Reply(200, {"change": {"name": "../x"}}, None), Reply(200, {}, None), Reply(200, None, None),
])
def test_ac17_bad_or_legacy_name_detail_posts_without_a_name(tmp_path, detail):
    client = _Client([event(1, "approval_state_changed", {
        "state": "approved", "decision_code": "approve", "decided_by": "api:ops-bot"}, "approval:8")], detail)
    adapter, channel, fd = _adapter(tmp_path, client)
    try:
        asyncio.run(_deliver(adapter))
        assert channel.sent == ["承認 #8 が承認・配備されました。"]
    finally:
        adapter.close(); os.close(fd)


@pytest.mark.parametrize(("reply", "expected"), [
    (Reply(200, {"change": {"name": "sma_cross"}}, None), "sma_cross"),
    (Reply(200, {"change": {"name": "../x"}}, None), None),
    (Reply(200, {"change": {"name": "@everyone"}}, None), None),
    (Reply(200, {"change": {"name": "with space"}}, None), None),
    (Reply(200, {}, None), None),
    (Reply(200, None, None), None),
])
def test_ac17_plugin_name_is_taken_only_from_a_valid_detail_change(reply, expected):
    assert _plugin_name(reply) == expected


@pytest.mark.parametrize(("setting", "posted"), [("default", [3, 4, 5, 6]),
                                                       ("all", [1, 2, 3, 4, 5, 6])])
def test_ac18_plan_event_uses_skip_known_for_default_ops_filters(setting, posted):
    gate = GateService(initialize_event_cursor(BotState.empty(), 0), guild_id=1, channel_id=2)
    rows = [
        event(1, "job_state_changed", {"job_kind": "approve", "state": "done", "result_code": "ok"}, "job:" + "a" * 32),
        event(2, "ops_request_terminal", {"endpoint_code": "approve", "result_code": "ok", "authenticated_principal": "api:ops-bot"}, "audit:1"),
        event(3, "job_state_changed", {"job_kind": "approve", "state": "failed", "result_code": "unavailable"}, "job:" + "b" * 32),
        event(4, "guard_state_changed", {"guard_kind": "kill_switch", "state": "latched", "generation": 1}, "instance"),
        event(5, "service_health_changed", {"component_code": "api", "health_code": "degraded"}, "instance"),
        event(6, "approval_state_changed", {"state": "approved", "decision_code": "approve", "decided_by": "api:ops-bot"}, "approval:8"),
    ]
    outcomes = [gate.plan_event(row, ops_notices=setting) for row in rows]
    assert [outcome.event_id for outcome in outcomes if not isinstance(outcome, SkipKnown)] == posted
    assert [outcome.event_id for outcome in outcomes if isinstance(outcome, SkipKnown)] == [
        event_id for event_id in range(1, 7) if event_id not in posted]
