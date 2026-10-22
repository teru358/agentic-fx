from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from afx_ops.api_client import Reply
from afx_ops.bot.adapter import BotAdapter, BotSettings, _plugin_name
from afx_ops.event_outcome import SkipKnown
from afx_ops.gate.service import GateService
from afx_ops.gate.state import ApprovalRecord, BotState, initialize_event_cursor, load, save


TS = "2026-10-22T00:00:00+00:00"


def event(event_id: int, code: str, fields: dict, ref: str) -> dict:
    return {"id": event_id, "ts": TS, "code": code, "ref": ref, "fields": fields}


class _Client:
    def __init__(self, events, detail=Reply(200, {"change": {"name": "sma_cross"}}, None)):
        self.events_reply = Reply(200, {"events": events, "high_watermark": len(events)}, None)
        self.detail = detail
        self.approval_calls = 0
        self.approval_timeouts = []

    def events(self, *, after, limit):
        return self.events_reply

    def approval(self, approval_id, *, timeout=None):
        self.approval_calls += 1
        self.approval_timeouts.append(timeout)
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
        assert client.approval_timeouts == [3.0]
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


_APPROVED = {"state": "approved", "decision_code": "approve", "decided_by": "api:ops-bot"}


def _approved(event_id: int = 1, approval_id: int = 8) -> dict:
    return event(event_id, "approval_state_changed", _APPROVED, f"approval:{approval_id}")


def test_the_posted_approval_sentence_carries_the_escaped_strategy_name(tmp_path):
    client = _Client([_approved()])
    adapter, channel, fd = _adapter(tmp_path, client)
    try:
        asyncio.run(_deliver(adapter))
        assert channel.sent == [r"承認 #8 (戦略 sma\_cross) が承認・配備されました。"]
        assert client.approval_calls == 1
        assert client.approval_timeouts == [3.0]
    finally:
        adapter.close(); os.close(fd)


@pytest.mark.parametrize("failure", [RuntimeError("down"), OSError("down"), KeyError("x")])
def test_a_failing_name_lookup_still_posts_the_sentence_without_a_name(tmp_path, failure):
    client = _Client([_approved()])

    def broken(approval_id, *, timeout=None):
        raise failure

    client.approval = broken
    adapter, channel, fd = _adapter(tmp_path, client)
    try:
        asyncio.run(_deliver(adapter))
        assert channel.sent == ["承認 #8 が承認・配備されました。"]
        assert (load(fd).cursor, load(fd).high_watermark) == (1, 1)
    finally:
        adapter.close(); os.close(fd)


def test_a_failed_post_leaves_the_cursor_and_the_next_round_resends(tmp_path):
    events = [event(1, "service_health_changed", {"component_code": "api", "health_code": "ok"}, "instance")]
    client = _Client(events)
    adapter, channel, fd = _adapter(tmp_path, client)
    try:
        good = channel.send

        async def broken(content, *, allowed_mentions):
            raise OSError("down")

        channel.send = broken
        asyncio.run(_deliver(adapter))
        assert (load(fd).cursor, load(fd).high_watermark) == (0, 0)
        channel.send = good
        asyncio.run(_deliver(adapter))
        assert channel.sent == ["api の健全性が ok になりました。"]
        assert (load(fd).cursor, load(fd).high_watermark) == (1, 1)
    finally:
        adapter.close(); os.close(fd)


def test_an_approval_event_is_settled_after_its_post_and_only_then(tmp_path):
    client = _Client([_approved(), event(2, "service_health_changed",
                                         {"component_code": "api", "health_code": "ok"}, "instance")])
    adapter, channel, fd = _adapter(tmp_path, client)
    settled = []

    async def settle(approval_id, *, detail=None):
        settled.append((approval_id, list(channel.sent), detail))

    adapter._settle = settle
    try:
        asyncio.run(_deliver(adapter))
        assert [(approval_id, sent) for approval_id, sent, _ in settled] == [
            (8, ["承認 #8 (戦略 sma\\_cross) が承認・配備されました。"])]
        assert settled[0][2] is client.detail
        assert client.approval_calls == 1
    finally:
        adapter.close(); os.close(fd)


def test_an_approval_event_whose_post_fails_is_not_settled(tmp_path):
    client = _Client([_approved()])
    adapter, channel, fd = _adapter(tmp_path, client)
    settled = []

    async def settle(approval_id):
        settled.append(approval_id)

    async def broken(content, *, allowed_mentions):
        raise OSError("down")

    adapter._settle = settle
    channel.send = broken
    try:
        asyncio.run(_deliver(adapter))
        assert settled == []
    finally:
        adapter.close(); os.close(fd)


def test_bot_settings_default_to_the_default_ops_filter():
    settings = BotSettings(guild_id=12345678901234567, channel_id=12345678901234568,
                           mention_role_id=None, mention_user_ids=())
    assert settings.ops_notices == "default"


_JOB = "a" * 32
_OTHER_JOB = "a" * 31 + "b"
_FAILED = {"job_kind": "approve", "state": "failed", "result_code": "unavailable"}


def _in_flight(job_id, state="in_flight") -> ApprovalRecord:
    if state == "in_flight":
        return ApprovalRecord(approval_id=7, message_id=101, guild_id=1, channel_id=2,
                              payload_sha256="a" * 64, display_generation=1, state="in_flight",
                              actor="discord:12345678901234569", action="approve", nonce="n",
                              expiry=10, job_id=job_id)
    return ApprovalRecord(approval_id=7, message_id=101, guild_id=1, channel_id=2,
                          payload_sha256="a" * 64, display_generation=1, state=state)


def _gate_with(record) -> GateService:
    base = BotState(cursor=0, high_watermark=0, approvals=(record,))
    return GateService(initialize_event_cursor(base, 0), guild_id=1, channel_id=2)


@pytest.mark.parametrize(("record", "fields", "expected"), [
    (_in_flight(_JOB), _FAILED, "承認 #7 の配備ジョブが失敗しました (理由 code: unavailable)。"),
    (_in_flight(_OTHER_JOB), _FAILED, "ジョブ approve が失敗しました (理由 code: unavailable)。"),
    (_in_flight(_JOB, "fresh"), _FAILED, "ジョブ approve が失敗しました (理由 code: unavailable)。"),
    (_in_flight(_JOB, "done"), _FAILED, "ジョブ approve が失敗しました (理由 code: unavailable)。"),
    (_in_flight(_JOB), {**_FAILED, "state": "cancelled"},
     "ジョブ approve が取り消されました (理由 code: unavailable)。"),
])
def test_only_the_failure_of_an_in_flight_approvals_own_job_names_the_approval(
        record, fields, expected):
    plan = _gate_with(record).plan_event(event(1, "job_state_changed", fields, "job:" + _JOB))
    assert plan.content == expected


def test_plan_event_hands_the_strategy_name_to_the_sentence():
    gate = GateService(initialize_event_cursor(BotState.empty(), 0), guild_id=1, channel_id=2)
    plan = gate.plan_event(_approved(), plugin_name="sma_cross")
    assert plan.content == r"承認 #8 (戦略 sma\_cross) が承認・配備されました。"


@pytest.mark.parametrize("state", ["failed", "cancelled", "shutdown"])
def test_default_posts_the_unsuccessful_job_endings_and_skips_the_rest(state):
    gate = GateService(initialize_event_cursor(BotState.empty(), 0), guild_id=1, channel_id=2)
    posted = gate.plan_event(event(1, "job_state_changed", {
        "job_kind": "approve", "state": state, "result_code": "x"}, "job:" + _JOB))
    assert not isinstance(posted, SkipKnown)
    for rest in ("done", "queued", "running"):
        skipped = gate.plan_event(event(2, "job_state_changed", {
            "job_kind": "approve", "state": rest, "result_code": "ok"}, "job:" + _JOB))
        assert skipped == SkipKnown(2)


def test_the_ops_setting_does_not_widen_what_the_trade_setting_hides():
    gate = GateService(initialize_event_cursor(BotState.empty(), 0), guild_id=1, channel_id=2)
    order = {"id": 1, "ts": TS, "code": "order_state_changed", "ref": "order:12", "fields": {
        "state": "placed", "pair": "USDJPY", "side": "long", "entry_type": "limit", "qty": "0.1",
        "price": "150.123", "sl": "149.8", "tp": "150.9", "origin_code": "signal:rsi_div",
        "mode": "paper", "state_mode": None, "reason_code": None}}
    assert gate.plan_event(order, trade_notices="off", ops_notices="all") == SkipKnown(1)
    done = event(2, "job_state_changed", {"job_kind": "ask", "state": "done", "result_code": "ok"},
                 "job:" + _JOB)
    assert gate.plan_event(done, trade_notices="off", ops_notices="all").event_id == 2
