"""adapter の結線を、偽の gate と偽の REST client で直接確かめる (discord の接続は開かない)。"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import discord
import pytest

from afx_ops.api_client import Reply
from afx_ops.bot.adapter import BotAdapter, BotFailure, BotSettings, _post_effect
from afx_ops.bot.views import GateView
from afx_ops.gate.render import primary_view
from afx_ops.gate.service import GateService
from afx_ops.gate.state import ApprovalRecord, BotState, load, observe_high
from afx_ops.gate.types import GateResult

SETTINGS = BotSettings(guild_id=12345678901234567, channel_id=12345678901234568,
                       mention_role_id=None, mention_user_ids=())


class _NoMentions:
    """edit に渡った allowed_mentions が mention を一切許さないことを == で表す。"""

    def __eq__(self, other):
        return isinstance(other, discord.AllowedMentions) and other.to_dict() == {"parse": []}

    __hash__ = None


_NO_MENTIONS = _NoMentions()



def _record(**changes):
    values = dict(approval_id=7, message_id=101, guild_id=SETTINGS.guild_id,
                  channel_id=SETTINGS.channel_id, payload_sha256="a" * 64,
                  display_generation=1, state="fresh")
    values.update(changes)
    return ApprovalRecord(**values)


# event cursor の初期化が済んだ空の状態
_STARTED = BotState(cursor=0, high_watermark=0, approvals=(), event_cursor_initialized=True)


class _Gate:
    def __init__(self, state: BotState) -> None:
        self.state = state
        self.reconciled: list[int] = []
        self.persist = None
        self.throttled: dict[int, int | None] = {}

    def observe_high_watermark(self, high_watermark: int) -> None:
        self.state = observe_high(self.state, high_watermark)

    def reconcile_in_flight(self, approval_id: int) -> None:
        self.reconciled.append(approval_id)

    def expire_due(self, now: int) -> None:
        return None


class _Client:
    def __init__(self, detail=None, events=None, approvals=None) -> None:
        self._detail = detail
        self._events = events
        self._approvals = approvals

    def approval(self, approval_id: int, *, timeout=None) -> Reply:
        return self._detail

    def events(self, *, after: int, limit: int) -> Reply:
        return self._events

    def approvals(self, *, limit: int) -> Reply:
        return self._approvals


def _adapter(gate, client, dir_fd: int = -1) -> BotAdapter:
    return BotAdapter(gate=gate, client=client, dir_fd=dir_fd, settings=SETTINGS, discord_client=None)


def test_a_restart_or_interaction_reconciles_every_in_flight_record_through_the_gate():
    in_flight = _record(state="in_flight", actor="discord:12345678901234569", action="approve",
                        nonce="n", expiry=600, job_id="0" * 32)
    gate = _Gate(BotState.empty().with_record(in_flight))
    adapter = _adapter(gate, _Client())
    try:
        adapter._reconcile_in_flight()
        assert gate.reconciled == [7]
    finally:
        adapter.close()


def test_an_event_naming_a_still_pending_approval_leaves_its_record_active(tmp_path):
    gate = _Gate(BotState.empty().with_record(_record()))
    client = _Client(detail=Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None))
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    adapter = _adapter(gate, client, dir_fd=fd)
    try:
        asyncio.run(adapter._settle(7))
        assert gate.state.approvals[0].state == "fresh"
    finally:
        adapter.close()
        os.close(fd)


class _EventClient(_Client):
    def __init__(self, detail, events) -> None:
        super().__init__(detail=detail, events=events)
        self.approval_calls = 0
        self.timeouts = []

    def approval(self, approval_id: int, *, timeout=None) -> Reply:
        self.approval_calls += 1
        self.timeouts.append(timeout)
        if isinstance(self._detail, Exception):
            raise self._detail
        return self._detail


class _EventDiscord:
    def __init__(self, sent) -> None:
        self.sent = sent

    def get_partial_messageable(self, channel_id, *, guild_id):
        owner = self

        class _Channel:
            async def send(self, content, **kwargs):
                owner.sent.append(content)
        return _Channel()


def test_event_name_timeout_posts_without_a_name_and_uses_the_short_deadline(tmp_path):
    event = {"id": 1, "ts": "2026-10-22T00:00:00+00:00", "code": "approval_state_changed",
             "ref": "approval:7", "fields": {"state": "approved", "decision_code": "approve"}}
    client = _EventClient(TimeoutError(), Reply(200, {"events": [event], "high_watermark": 1}, None))
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    gate = GateService(_STARTED, guild_id=SETTINGS.guild_id, channel_id=SETTINGS.channel_id,
                       state_dir_fd=fd)
    sent = []
    adapter = _adapter(gate, client, dir_fd=fd)
    adapter.discord = _EventDiscord(sent)
    async def direct(fn, *args):
        return fn(*args)
    adapter._run = direct
    try:
        asyncio.run(adapter.deliver_events())
        assert client.timeouts == [3.0]
        assert sent == ["承認 #7 が承認・配備されました。"]
    finally:
        adapter.close()
        os.close(fd)


@pytest.mark.parametrize(("reply", "expected_calls", "terminal"), [
    (Reply(429, None, "rate", 300), 1, False),
    (Reply(200, {"status": "approved", "payload_sha256": "a" * 64}, None), 1, True),
])
def test_event_name_lookup_is_observed_and_reused_by_settle(tmp_path, reply, expected_calls, terminal):
    event = {"id": 1, "ts": "2026-10-22T00:00:00+00:00", "code": "approval_state_changed",
             "ref": "approval:7", "fields": {"state": "approved", "decision_code": "approve"}}
    client = _EventClient(reply, Reply(200, {"events": [event], "high_watermark": 1}, None))
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    gate = GateService(_STARTED.with_record(_record()), guild_id=SETTINGS.guild_id,
                       channel_id=SETTINGS.channel_id, state_dir_fd=fd)
    adapter = _adapter(gate, client, dir_fd=fd)
    async def direct(fn, *args):
        return fn(*args)
    adapter._run = direct
    adapter.discord = _EventDiscord([])
    adapter._sync_views = lambda: asyncio.sleep(0)
    try:
        asyncio.run(adapter.deliver_events())
        assert client.approval_calls == expected_calls
        assert (gate.state.record(7).state == "done") is terminal
    finally:
        adapter.close()
        os.close(fd)


def test_an_unreadable_event_is_a_payload_failure_and_never_a_silent_skip(tmp_path):
    gate = _Gate(_STARTED)
    client = _Client(events=Reply(200, {"events": [{"id": "x"}], "high_watermark": 1}, None))
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    adapter = _adapter(gate, client, dir_fd=fd)
    try:
        with pytest.raises(BotFailure) as failure:
            asyncio.run(adapter.deliver_events())
        assert failure.value.reason == "payload_invalid"
    finally:
        adapter.close()
        os.close(fd)


def test_reject_is_drawn_as_a_danger_button_and_the_others_as_secondary():
    async def build() -> GateView:
        async def noop(interaction) -> None:
            return None
        return GateView(primary_view(_record()), noop)

    view = asyncio.run(build())
    styles = {item.label: item.style for item in view.children}
    assert styles["Reject"] is discord.ButtonStyle.danger
    assert styles["Approve"] is discord.ButtonStyle.secondary


def test_an_interaction_ends_by_retiring_the_views_of_terminal_records():
    gate = _Gate(BotState.empty())
    gate.handle = lambda request, now: GateResult("Nothing to do.")
    adapter = _adapter(gate, _Client())
    calls: list[str] = []

    async def retire() -> None:
        calls.append("retire")

    async def defer(**kwargs) -> None:
        return None

    async def send(*args, **kwargs) -> None:
        return None

    interaction = SimpleNamespace(user=SimpleNamespace(id=1), data={"custom_id": "afx:v1:approve:7"},
                                  guild_id=SETTINGS.guild_id, channel_id=SETTINGS.channel_id,
                                  message=None, response=SimpleNamespace(defer=defer),
                                  followup=SimpleNamespace(send=send))
    adapter._sync_views = retire
    try:
        asyncio.run(adapter.on_component(interaction, message_id=None, generation=None))
        assert calls == ["retire"]
    finally:
        adapter.close()


def test_the_adapter_gives_the_gate_its_snapshot_writer_for_persisting_before_a_poll():
    gate = _Gate(BotState.empty())
    adapter = _adapter(gate, _Client())
    try:
        assert gate.persist == adapter._save
    finally:
        adapter.close()


def test_an_empty_events_page_still_records_the_high_watermark_and_a_rollback_stops(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    gate = GateService(BotState.empty(), guild_id=SETTINGS.guild_id, channel_id=SETTINGS.channel_id,
                       state_dir_fd=fd)
    client = _Client(events=Reply(200, {"events": [], "high_watermark": 9}, None))
    adapter = _adapter(gate, client, dir_fd=fd)
    try:
        asyncio.run(adapter.deliver_events())
        assert load(fd).high_watermark == 9
        client._events = Reply(200, {"events": [], "high_watermark": 3}, None)
        with pytest.raises(BotFailure) as failure:
            asyncio.run(adapter.deliver_events())
        assert failure.value.reason == "high_watermark_regressed"
        assert load(fd).high_watermark == 9
    finally:
        adapter.close()
        os.close(fd)


def test_the_posted_approval_shows_the_change_summary_the_adapter_fetched():
    reply = Reply(200, {"status": "pending", "payload_sha256": "a" * 64,
                        "change": {"name": "sma_cross", "kind": "strategy",
                                   "classification": "new", "params": {"items": []},
                                   "performance": {"in_sample": [], "holdout": []},
                                   "summary": "申告"}}, None)
    content = _post_effect(_record(), reply).content
    assert "承認 #7 / 戦略 sma\\_cross (新規)" in content and "変更: 前版なし" in content
    assert _post_effect(_record(), None).content == "承認 #7\n詳細を取得できません"


def test_an_approval_ref_with_non_ascii_digits_is_not_an_approval_id():
    from afx_ops.bot.adapter import _approval_id_of
    for ref in ("approval:²", "approval:٣", "approval:07", "approval:1" + "0" * 18):
        assert _approval_id_of({"code": "approval_state_changed", "ref": ref}) is None
    assert _approval_id_of({"code": "approval_state_changed", "ref": "approval:7"}) == 7


def test_a_rewound_watermark_or_cursor_in_a_commit_is_a_fixed_reason(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    gate = GateService(BotState(cursor=5, high_watermark=9, approvals=(),
                              event_cursor_initialized=True),
                       guild_id=SETTINGS.guild_id, channel_id=SETTINGS.channel_id)
    adapter = _adapter(gate, _Client(), dir_fd=fd)
    try:
        for call, reason in ((lambda: adapter._observe_high(3), "high_watermark_regressed"),
                             (lambda: adapter._commit_post(6, 3), "high_watermark_regressed"),
                             (lambda: adapter._commit_post(5, 9), "bot_state_invalid")):
            with pytest.raises(BotFailure) as failure:
                call()
            assert failure.value.reason == reason
        assert (gate.state.cursor, gate.state.high_watermark) == (5, 9)
    finally:
        adapter.close()
        os.close(fd)


class _JobDoneClient(_Client):
    def __init__(self) -> None:
        super().__init__(detail=Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None))

    def poll(self, job) -> Reply:
        return Reply(200, {"state": "done"}, None)


def test_a_transition_made_only_by_the_in_flight_reconcile_reaches_the_snapshot(tmp_path):
    from afx_ops.gate.types import Interaction
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    in_flight = _record(state="in_flight", actor="discord:12345678901234569", action="approve",
                        nonce="n", expiry=600, confirm_message_id=201, job_id="0" * 32)
    gate = GateService(BotState.empty().with_record(in_flight), guild_id=SETTINGS.guild_id,
                       channel_id=SETTINGS.channel_id, allowlist_user_ids=(),
                       client=_JobDoneClient())
    adapter = _adapter(gate, gate.client, dir_fd=fd)
    try:
        adapter._save()
        # allowlist 外の利用者の click: handle は状態を変えないが、その前の再照合が done にする
        result = adapter._decide(Interaction(custom_id="afx:v1:approve:7",
                                             guild_id=SETTINGS.guild_id,
                                             channel_id=SETTINGS.channel_id,
                                             user_id=12345678901234570, message_id=101,
                                             display_generation=1))
        assert result.decision is None
        assert load(fd).approvals[0].state == "done"
    finally:
        adapter.close()
        os.close(fd)


class _JobClient(_Client):
    def __init__(self, job_state: str) -> None:
        super().__init__(detail=Reply(200, {"status": "approved", "payload_sha256": "a" * 64}, None))
        self.job_state = job_state

    def poll(self, job) -> Reply:
        return Reply(200, {"state": self.job_state}, None)


def test_settling_an_in_flight_job_follows_the_job_and_not_the_approval_status(tmp_path, monkeypatch):
    from afx_ops.gate import service as gate_service
    monkeypatch.setattr(gate_service, "JOB_POLL_INTERVAL_SECONDS", 0)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    in_flight = _record(state="in_flight", actor="discord:12345678901234569", action="approve",
                        nonce="n", expiry=600, confirm_message_id=201, job_id="0" * 32)
    client = _JobClient("running")
    gate = GateService(BotState.empty().with_record(in_flight), guild_id=SETTINGS.guild_id,
                       channel_id=SETTINGS.channel_id, client=client)
    adapter = _adapter(gate, client, dir_fd=fd)
    adapter._sync_views = _no_retire
    try:
        asyncio.run(adapter._settle(7))
        assert (gate.state.approvals[0].state, gate.state.approvals[0].job_id) == (
            "in_flight", "0" * 32)
        client.job_state = "done"
        asyncio.run(adapter._settle(7))
        assert load(fd).approvals[0].state == "done"
    finally:
        adapter.close()
        os.close(fd)


async def _no_retire() -> None:
    return None


class _FakeChannel:
    """bot token の message edit だけを記録する (failures 回だけ 503 を返す)。"""

    def __init__(self) -> None:
        self.edits: list[tuple[int, dict]] = []
        self.failures = 0

    def get_partial_message(self, message_id: int):
        channel = self

        class _Message:
            async def edit(self, **kwargs) -> None:
                if channel.failures:
                    channel.failures -= 1
                    raise discord.HTTPException(SimpleNamespace(status=503, reason="unavailable"),
                                                "unavailable")
                channel.edits.append((message_id, kwargs))
        return _Message()


class _FakeDiscord:
    user = None

    def __init__(self) -> None:
        self.channel = _FakeChannel()
        self.views: list[tuple[int | None, object]] = []

    def get_partial_messageable(self, channel_id: int, *, guild_id: int | None = None):
        return self.channel

    def add_view(self, view, *, message_id: int | None = None) -> None:
        self.views.append((message_id, view))


def _live_adapter(gate, client, fd) -> BotAdapter:
    return BotAdapter(gate=gate, client=client, dir_fd=fd, settings=SETTINGS,
                      discord_client=_FakeDiscord())


def test_an_expired_claim_keeps_the_view_and_moves_its_dispatch_to_the_new_generation(tmp_path):
    from afx_ops.gate.types import Interaction
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    gate = GateService(BotState.empty().with_record(_record()), guild_id=SETTINGS.guild_id,
                       channel_id=SETTINGS.channel_id, allowlist_user_ids=(12345678901234569,))
    adapter = _live_adapter(gate, _Client(), fd)

    async def scenario() -> None:
        adapter._register(gate.state.approvals[0])
        click = Interaction(custom_id="afx:v1:approve:7", guild_id=SETTINGS.guild_id,
                            channel_id=SETTINGS.channel_id, user_id=12345678901234569,
                            message_id=101, display_generation=1)
        gate.handle(click, now=0)
        gate.handle(click, now=600)
        await adapter._sync_views()

    try:
        asyncio.run(scenario())
        record = load(fd).approvals[0]
        assert (record.state, record.display_generation) == ("fresh", 2)
        # View は外さず (daemon ではまだ pending)、新しい世代の primary View を付け直す
        assert adapter._routes[101].generation == 2
        assert [(mid, sorted(kw)) for mid, kw in adapter.discord.channel.edits] == [(101, ["view"])]
    finally:
        adapter.close()
        os.close(fd)


USER = 12345678901234569


class _Followups:
    """ephemeral followup を id 付きの message として返す。"""

    def __init__(self) -> None:
        self.next_id = 300
        self.sent: list[dict] = []

    async def send(self, content, **kwargs):
        self.sent.append({"content": content, **kwargs})
        if kwargs.get("wait"):
            self.next_id += 1
            return SimpleNamespace(id=self.next_id)
        return None


def _component(custom_id: str, followups: _Followups):
    async def defer(**kwargs) -> None:
        return None
    return SimpleNamespace(user=SimpleNamespace(id=USER), data={"custom_id": custom_id},
                           guild_id=SETTINGS.guild_id, channel_id=SETTINGS.channel_id,
                           message=None, response=SimpleNamespace(defer=defer),
                           followup=followups)


def _confirm_routes(adapter) -> list[int]:
    return [message_id for message_id in adapter._routes if message_id != 101]


def test_approve_then_cancel_three_times_leaves_no_confirm_route(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    gate = GateService(BotState.empty().with_record(_record()), guild_id=SETTINGS.guild_id,
                       channel_id=SETTINGS.channel_id, allowlist_user_ids=(USER,))
    adapter = _live_adapter(gate, _Client(), fd)
    adapter._now = lambda: 0
    followups = _Followups()

    async def scenario() -> list:
        adapter._register(gate.state.approvals[0])
        views = []
        for _ in range(3):
            await adapter.on_component(_component("afx:v1:approve:7", followups),
                                       message_id=101, generation=1)
            confirm = followups.next_id
            assert _confirm_routes(adapter) == [confirm]
            views.append(adapter._routes[confirm].view)
            await adapter.on_component(_component("afx:v1:cancel:7", followups),
                                       message_id=confirm, generation=1)
            assert gate.state.approvals[0].state == "fresh"
            assert _confirm_routes(adapter) == []
        return views

    try:
        views = asyncio.run(scenario())
        assert list(adapter._routes) == [101]
        assert all(view.is_finished() for view in views)
    finally:
        adapter.close()
        os.close(fd)


class _QuietClient(_Client):
    def __init__(self) -> None:
        super().__init__(detail=Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None),
                         events=Reply(200, {"events": [], "high_watermark": 0}, None))

    def approvals(self, *, limit: int) -> Reply:
        return Reply(200, {"approvals": [{"id": 7}]}, None)


def test_a_claim_that_expires_without_a_click_drops_its_confirm_route_on_the_next_poll(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    gate = GateService(BotState.empty().with_record(_record()), guild_id=SETTINGS.guild_id,
                       channel_id=SETTINGS.channel_id, allowlist_user_ids=(USER,))
    adapter = _live_adapter(gate, _QuietClient(), fd)
    clock = [0]
    adapter._now = lambda: clock[0]
    followups = _Followups()

    async def scenario() -> None:
        adapter._register(gate.state.approvals[0])
        await adapter.on_component(_component("afx:v1:approve:7", followups),
                                   message_id=101, generation=1)
        assert len(_confirm_routes(adapter)) == 1
        await adapter.poll_once()
        assert len(_confirm_routes(adapter)) == 1
        clock[0] = 600
        await adapter.poll_once()

    try:
        asyncio.run(scenario())
        record = load(fd).approvals[0]
        assert (record.state, record.display_generation) == ("fresh", 2)
        assert list(adapter._routes) == [101] and adapter._routes[101].generation == 2
    finally:
        adapter.close()
        os.close(fd)


def test_a_failed_view_removal_is_kept_and_retried_after_the_backoff(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    gate = GateService(BotState.empty().with_record(_record()), guild_id=SETTINGS.guild_id,
                       channel_id=SETTINGS.channel_id, allowlist_user_ids=(USER,))
    adapter = _live_adapter(gate, _QuietClient(), fd)
    clock = [1000.0]
    adapter._now = lambda: clock[0]
    channel = adapter.discord.channel
    channel.failures = 1

    async def scenario() -> None:
        adapter._register(gate.state.approvals[0])
        await adapter._run(gate.settle, 7)
        await adapter._sync_views()
        # 503: 表示更新は記録に残し、route も残す (次の周回で再試行する)
        assert channel.edits == [] and 101 in adapter._routes
        assert load(fd).approvals[0].edit_pending is True
        await adapter._sync_views()
        assert channel.edits == []
        clock[0] += 60
        await adapter._sync_views()

    try:
        asyncio.run(scenario())
        assert channel.edits == [(101, {"content": "Approval is no longer active.", "view": None,
             "allowed_mentions": _NO_MENTIONS})]
        assert 101 not in adapter._routes
    finally:
        adapter.close()
        os.close(fd)


def test_a_refresh_edit_that_gave_up_does_not_block_the_later_terminal_edit(tmp_path):
    from afx_ops.bot import adapter as bot_adapter
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    gate = GateService(BotState.empty().with_record(_record()), guild_id=SETTINGS.guild_id,
                       channel_id=SETTINGS.channel_id, allowlist_user_ids=(USER,))
    adapter = _live_adapter(gate, _QuietClient(), fd)
    clock = [0.0]
    adapter._now = lambda: clock[0]
    channel = adapter.discord.channel
    channel.failures = bot_adapter.EDIT_RETRY_LIMIT

    async def scenario() -> None:
        adapter._register(gate.state.approvals[0])
        await adapter.on_component(_component("afx:v1:approve:7", _Followups()),
                                   message_id=101, generation=1)
        await adapter._run(gate.expire_due, 600)
        for _ in range(bot_adapter.EDIT_RETRY_LIMIT + 2):
            clock[0] += bot_adapter.EDIT_BACKOFF_MAX_SECONDS
            await adapter._sync_views()
        assert channel.failures == 0 and channel.edits == []
        await adapter._run(gate.settle, 7)
        await adapter._sync_views()

    try:
        asyncio.run(scenario())
        assert channel.edits == [(101, {"content": "Approval is no longer active.", "view": None,
             "allowed_mentions": _NO_MENTIONS})]
        assert load(fd).approvals == ()
    finally:
        adapter.close()
        os.close(fd)


def test_every_poll_round_reconciles_each_in_flight_record_even_when_it_is_still_listed(tmp_path):
    lost = _record(state="in_flight", actor="discord:12345678901234569", action="approve",
                   nonce="n", expiry=600)
    gate = _Gate(_STARTED.with_record(lost))
    client = _Client(events=Reply(200, {"events": [], "high_watermark": 0}, None),
                     approvals=Reply(200, {"approvals": [{"id": 7}]}, None))
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    adapter = _adapter(gate, client, dir_fd=fd)
    try:
        asyncio.run(adapter.poll_once())
        assert gate.reconciled == [7]
    finally:
        adapter.close()
        os.close(fd)
