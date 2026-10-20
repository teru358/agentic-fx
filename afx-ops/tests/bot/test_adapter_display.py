"""表示の更新・再投稿・View の世代の結線を、偽の channel で直接確かめる (discord の接続は開かない)。"""
from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace

import discord
import pytest

from afx_ops.api_client import Reply
from afx_ops.bot import adapter as adapter_module
from afx_ops.bot.adapter import BotAdapter, BotFailure, BotSettings
from afx_ops.gate.render import footer
from afx_ops.gate.service import GateService
from afx_ops.gate.state import ApprovalRecord, BotState
from afx_ops.gate.types import DecisionPlan, GateResult

GUILD = 12345678901234567
CHANNEL = 12345678901234568
SETTINGS = BotSettings(guild_id=GUILD, channel_id=CHANNEL, mention_role_id=None,
                       mention_user_ids=())
DIGEST = "a" * 64


class _NoMentions:
    """edit に渡った allowed_mentions が mention を一切許さないことを == で表す。"""

    def __eq__(self, other):
        return isinstance(other, discord.AllowedMentions) and other.to_dict() == {"parse": []}

    __hash__ = None


_NO_MENTIONS = _NoMentions()



def _record(**changes):
    values = dict(approval_id=7, message_id=101, guild_id=GUILD, channel_id=CHANNEL,
                  payload_sha256=DIGEST, display_generation=1, state="fresh")
    values.update(changes)
    return ApprovalRecord(**values)


def _not_found() -> discord.NotFound:
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")


class _Partial:
    def __init__(self, channel, message_id) -> None:
        self._channel, self._id = channel, message_id

    async def edit(self, **kwargs) -> None:
        self._channel.edits.append((self._id, kwargs))
        outcomes = self._channel.edit_outcomes.get(self._id)
        outcome = outcomes.pop(0) if outcomes else "ok"
        if outcome == "gone":
            raise _not_found()
        if outcome == "fail":
            raise OSError("down")


class _Channel:
    def __init__(self) -> None:
        self.edits: list[tuple[int, dict]] = []
        self.edit_outcomes: dict[int, list[str]] = {}
        self.sent: list[dict] = []
        self.fetched: dict[int, object] = {}
        self.send_fails = False
        self.history_items: list = []
        self._next = 900

    def get_partial_message(self, message_id):
        return _Partial(self, message_id)

    async def send(self, content=None, **kwargs):
        if self.send_fails:
            raise OSError("down")
        self._next += 1
        self.sent.append({"content": content, **kwargs})
        return SimpleNamespace(id=self._next)

    async def history(self, limit=None):
        for item in self.history_items[:limit]:
            yield item

    async def fetch_message(self, message_id):
        found = self.fetched.get(message_id)
        if found is None:
            raise _not_found()
        return found


class _Discord:
    user = SimpleNamespace(id=5)

    def __init__(self, channel) -> None:
        self.channel = channel
        self.views: list[tuple[object, int | None]] = []

    def get_partial_messageable(self, channel_id, guild_id=None):
        return self.channel

    def add_view(self, view, message_id=None) -> None:
        self.views.append((view, message_id))


class _Client:
    def __init__(self, listed=(7,)) -> None:
        self.listed = listed

    def approval(self, approval_id: int) -> Reply:
        return Reply(200, {"status": "pending", "payload_sha256": DIGEST}, None)

    def approvals(self, *, limit: int) -> Reply:
        return Reply(200, {"approvals": [{"id": i} for i in self.listed]}, None)


def _posted(approval_id=7):
    return SimpleNamespace(
        id=101, embeds=[SimpleNamespace(footer=SimpleNamespace(text=footer(approval_id, DIGEST, 1)))],
        channel=SimpleNamespace(id=CHANNEL), author=SimpleNamespace(id=_Discord.user.id))


@pytest.fixture
def harness(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    clock = [1000.0]
    made = []

    def build(record, channel=None):
        channel = _Channel() if channel is None else channel
        gate = GateService(BotState.empty().with_record(record), guild_id=GUILD, channel_id=CHANNEL,
                           allowlist_user_ids=(), client=_Client(), state_dir_fd=fd)
        adapter = BotAdapter(gate=gate, client=gate.client, dir_fd=fd, settings=SETTINGS,
                             discord_client=_Discord(channel), now=lambda: clock[0])
        made.append(adapter)
        return adapter, channel, clock

    yield build
    for adapter in made:
        adapter.close()
    os.close(fd)


def test_a_deleted_pending_post_is_reposted_and_the_record_follows_the_new_message(harness):
    adapter, channel, _ = harness(_record(display_generation=2, edit_pending=True))
    channel.edit_outcomes[101] = ["gone"]

    async def go():
        await adapter._update_display(adapter.gate.state.record(7))

    asyncio.run(go())
    assert len(channel.sent) == 1
    record = adapter.gate.state.record(7)
    assert (record.message_id, record.display_generation, record.edit_pending) == (901, 3, False)
    assert 101 not in adapter._routes and 901 in adapter._routes
    assert adapter.gate._read_state().record(7) == record


def _crash_after_send(adapter, channel):
    """send の後、置換の保存で止まる (process の死を、保存の失敗で再現する)。"""
    real = adapter._save
    sent_before = len(channel.sent)

    def save() -> None:
        if len(channel.sent) > sent_before:
            raise BotFailure("bot_state_invalid")
        real()
    adapter._save = save


def test_a_repost_that_stopped_after_sending_is_adopted_after_a_restart_not_posted_twice(harness):
    adapter, channel, _ = harness(_record())
    channel.edit_outcomes[101] = ["gone"]
    _crash_after_send(adapter, channel)
    with pytest.raises(BotFailure):
        asyncio.run(adapter._update_display(adapter.gate.state.record(7)))
    assert len(channel.sent) == 1
    # 意図は送信の前に保存されている。再起動後はその snapshot から始まる
    persisted = adapter.gate._read_state().record(7)
    assert (persisted.message_id, persisted.repost_generation) == (101, 2)
    channel.history_items = [SimpleNamespace(
        id=901, embeds=[SimpleNamespace(footer=SimpleNamespace(text=footer(7, DIGEST, 2)))],
        channel=SimpleNamespace(id=CHANNEL), author=SimpleNamespace(id=_Discord.user.id))]
    restarted, _, _ = harness(persisted, channel)
    asyncio.run(restarted.reconcile_startup())
    assert len(channel.sent) == 1
    record = restarted.gate.state.record(7)
    assert (record.message_id, record.display_generation, record.repost_generation) == (901, 2, None)
    assert 901 in restarted._routes


def test_a_repost_whose_send_never_arrived_is_posted_once_after_a_restart(harness):
    adapter, channel, _ = harness(_record())
    channel.edit_outcomes[101] = ["gone"]
    channel.send_fails = True
    asyncio.run(adapter._update_display(adapter.gate.state.record(7)))
    persisted = adapter.gate._read_state().record(7)
    assert persisted.repost_generation == 2 and channel.sent == []
    channel.send_fails = False
    restarted, _, _ = harness(persisted, channel)
    asyncio.run(restarted.reconcile_startup())
    assert len(channel.sent) == 1
    record = restarted.gate.state.record(7)
    assert (record.message_id, record.display_generation, record.repost_generation) == (901, 2, None)


def test_a_post_that_became_terminal_while_the_repost_was_sending_is_shown_as_terminal(harness):
    adapter, channel, clock = harness(_record())
    channel.edit_outcomes[101] = ["gone"]
    channel.edit_outcomes[901] = ["fail"]
    real_send = channel.send

    async def send(content=None, **kwargs):
        message = await real_send(content, **kwargs)
        adapter.gate.settle(7)
        return message
    channel.send = send

    async def go():
        await adapter._update_display(adapter.gate.state.record(7))
        record = adapter.gate.state.record(7)
        # 終端のまま新しい投稿を指し、View を外す表示は未反映として残る
        assert (record.state, record.message_id, record.edit_pending) == ("done", 901, True)
        assert 901 not in adapter._routes and 101 not in adapter._routes
        clock[0] += 1000
        await adapter._update_display(adapter.gate.state.record(7))

    asyncio.run(go())
    shown = {"content": "Approval is no longer active.", "view": None,
             "allowed_mentions": _NO_MENTIONS}
    assert [edit for edit in channel.edits if edit[0] == 901] == [(901, shown), (901, shown)]
    assert adapter.gate.state.record(7) is None


def test_two_concurrent_reposts_of_one_record_leave_one_post_whose_footer_matches_the_generation(harness):
    adapter, channel, _ = harness(_record())
    record = adapter.gate.state.record(7)

    async def go():
        return await asyncio.gather(adapter._repost(record), adapter._repost(record))

    outcomes = asyncio.run(go())
    committed = adapter.gate.state.record(7)
    assert sorted(outcomes) == [False, True] and len(channel.sent) == 1
    assert channel.sent[0]["embed"].footer.text == footer(7, DIGEST, committed.display_generation)
    assert (committed.message_id, committed.display_generation, committed.repost_generation) == (901, 2, None)
    assert [route.generation for route in adapter._routes.values()] == [2]


def test_a_display_update_and_a_repost_of_one_record_do_not_interleave(harness):
    adapter, channel, _ = harness(_record())
    channel.edit_outcomes[101] = ["gone"]
    record = adapter.gate.state.record(7)

    async def go():
        await asyncio.gather(adapter._update_display(record), adapter._repost(record))

    asyncio.run(go())
    committed = adapter.gate.state.record(7)
    assert len(channel.sent) == 1
    assert channel.sent[0]["embed"].footer.text == footer(7, DIGEST, committed.display_generation)
    assert [route.generation for route in adapter._routes.values()] == [committed.display_generation]


def test_a_repost_whose_intent_changed_while_sending_is_not_committed_and_its_post_is_closed(harness):
    adapter, channel, _ = harness(_record())
    real_send = channel.send

    async def send(content=None, **kwargs):
        message = await real_send(content, **kwargs)
        record = adapter.gate.state.record(7)
        adapter.gate.state = adapter.gate.state.replace_record(
            replace(record, repost_generation=record.display_generation + 5))
        return message
    channel.send = send

    assert asyncio.run(adapter._repost(adapter.gate.state.record(7))) is False
    record = adapter.gate.state.record(7)
    assert (record.message_id, record.display_generation) == (101, 1)
    assert 901 not in adapter._routes
    assert channel.edits == [(901, {"content": "Approval is no longer active.", "view": None,
             "allowed_mentions": _NO_MENTIONS})]


def test_a_failed_repost_of_a_deleted_post_is_retried_with_backoff(harness):
    adapter, channel, clock = harness(_record(edit_pending=True))
    channel.edit_outcomes[101] = ["gone", "gone"]
    channel.send_fails = True

    async def go():
        await adapter._update_display(adapter.gate.state.record(7))
        await adapter._update_display(adapter.gate.state.record(7))
        clock[0] += 1000
        channel.send_fails = False
        await adapter._update_display(adapter.gate.state.record(7))

    asyncio.run(go())
    assert len(channel.edits) == 2 and len(channel.sent) == 1
    assert adapter.gate.state.record(7).message_id == 901


def test_a_deleted_post_of_a_terminal_record_drops_the_record_without_reposting(harness):
    adapter, channel, _ = harness(_record(state="done", edit_pending=True))
    channel.edit_outcomes[101] = ["gone"]
    asyncio.run(adapter._update_display(adapter.gate.state.record(7)))
    assert channel.sent == []
    assert adapter.gate.state.record(7) is None
    assert adapter.gate._read_state().record(7) is None


def test_a_startup_adopt_edit_failure_is_counted_and_backed_off(harness):
    adapter, channel, clock = harness(_record())
    channel.fetched[101] = _posted()
    channel.edit_outcomes[101] = ["fail"]

    async def go():
        await adapter.reconcile_startup()
        assert len(channel.edits) == 1
        # backoff の間は同じ表示の edit を繰り返さない
        await adapter._update_display(adapter.gate.state.record(7))
        assert len(channel.edits) == 1
        clock[0] += adapter_module.EDIT_BACKOFF_SECONDS + 1
        await adapter._update_display(adapter.gate.state.record(7))

    asyncio.run(go())
    assert len(channel.edits) == 2
    assert adapter._edit_retry == {}


def test_a_startup_adopt_gives_up_after_the_retry_limit(harness):
    adapter, channel, clock = harness(_record())
    channel.fetched[101] = _posted()
    channel.edit_outcomes[101] = ["fail"] * 100

    async def go():
        await adapter.reconcile_startup()
        for _ in range(adapter_module.EDIT_RETRY_LIMIT + 3):
            clock[0] += adapter_module.EDIT_BACKOFF_MAX_SECONDS + 1
            await adapter._update_display(adapter.gate.state.record(7))

    asyncio.run(go())
    assert len(channel.edits) == adapter_module.EDIT_RETRY_LIMIT


def test_a_post_missing_at_startup_is_reposted_instead_of_adopted(harness):
    adapter, channel, _ = harness(_record())
    asyncio.run(adapter.reconcile_startup())
    assert len(channel.sent) == 1
    record = adapter.gate.state.record(7)
    assert (record.message_id, record.display_generation) == (901, 2)


def test_an_adopted_post_deleted_before_its_edit_is_reposted(harness):
    adapter, channel, _ = harness(_record())
    channel.fetched[101] = _posted()
    channel.edit_outcomes[101] = ["gone"]
    asyncio.run(adapter.reconcile_startup())
    assert len(channel.sent) == 1
    assert adapter.gate.state.record(7).message_id == 901


def test_a_view_keeps_the_generation_it_was_made_for(harness):
    adapter, _, _ = harness(_record(display_generation=1))
    seen: list[int | None] = []

    async def on_component(interaction, *, message_id, generation) -> None:
        seen.append(generation)

    adapter.on_component = on_component

    async def go():
        old = adapter._register(adapter.gate.state.record(7))
        adapter._register(_record(display_generation=2))
        click = SimpleNamespace(message=SimpleNamespace(id=101))
        await old.children[0].callback(click)
        new = adapter._routes[101].view
        await new.children[0].callback(click)

    asyncio.run(go())
    assert seen == [1, 2]


def test_a_post_from_a_past_generation_is_never_adopted_by_a_repost_recovery(harness):
    adapter, channel, _ = harness(_record(repost_generation=2))
    stale = SimpleNamespace(
        id=800, embeds=[SimpleNamespace(footer=SimpleNamespace(text=footer(7, DIGEST, 1)))],
        channel=SimpleNamespace(id=CHANNEL), author=SimpleNamespace(id=_Discord.user.id))
    channel.history_items = [stale]
    assert asyncio.run(adapter._repost(adapter.gate.state.record(7))) is True
    assert len(channel.sent) == 1
    assert channel.sent[0]["embed"].footer.text == footer(7, DIGEST, 2)
    assert adapter.gate.state.record(7).message_id == 901


def test_only_the_post_of_the_new_generation_is_adopted_by_a_repost_recovery(harness):
    adapter, channel, _ = harness(_record(repost_generation=2))
    stale = SimpleNamespace(
        id=800, embeds=[SimpleNamespace(footer=SimpleNamespace(text=footer(7, DIGEST, 1)))],
        channel=SimpleNamespace(id=CHANNEL), author=SimpleNamespace(id=_Discord.user.id))
    fresh = SimpleNamespace(
        id=850, embeds=[SimpleNamespace(footer=SimpleNamespace(text=footer(7, DIGEST, 2)))],
        channel=SimpleNamespace(id=CHANNEL), author=SimpleNamespace(id=_Discord.user.id))
    channel.history_items = [stale, fresh]
    asyncio.run(adapter._repost(adapter.gate.state.record(7)))
    assert channel.sent == [] and adapter.gate.state.record(7).message_id == 850


def test_a_429_seen_by_the_decision_precheck_defers_the_next_poll(harness):
    adapter, _, clock = harness(_record(state="in_flight", actor="discord:12345678901234569",
                                        action="approve", nonce="n", expiry=5000))
    adapter.gate.allowlist_user_ids = (12345678901234569,)

    class Throttled(_Client):
        def approval(self, approval_id):
            return Reply(429, None, "rate", 30)

    adapter.gate.client = Throttled()
    plan = DecisionPlan(7, "approve", "discord:12345678901234569", DIGEST)
    adapter.gate.handle = lambda request, now: GateResult("x", decision=plan)
    adapter._decide(SimpleNamespace(custom_id=None))
    assert not adapter._poll_due(7)
    clock[0] += 31
    assert adapter._poll_due(7)


def test_a_repost_does_not_send_before_the_retry_after_deadline(harness):
    adapter, channel, clock = harness(_record())
    adapter._next_poll_at[7] = clock[0] + 10
    assert asyncio.run(adapter._repost(adapter.gate.state.record(7))) is False
    assert channel.sent == []
    assert adapter.gate.state.record(7).repost_generation is None
    clock[0] += 11
    assert asyncio.run(adapter._repost(adapter.gate.state.record(7))) is True
    assert len(channel.sent) == 1


def test_a_terminal_record_whose_edit_keeps_failing_is_dropped_at_the_retry_limit(harness):
    adapter, channel, clock = harness(_record(state="done", edit_pending=True))
    channel.edit_outcomes[101] = ["fail"] * 100

    async def go():
        for _ in range(adapter_module.EDIT_RETRY_LIMIT):
            assert adapter.gate.state.record(7) is not None
            clock[0] += adapter_module.EDIT_BACKOFF_MAX_SECONDS + 1
            await adapter._update_display(adapter.gate.state.record(7))

    asyncio.run(go())
    assert len(channel.edits) == adapter_module.EDIT_RETRY_LIMIT
    assert adapter.gate.state.record(7) is None
    assert adapter.gate._read_state().record(7) is None
    assert adapter._edit_retry == {}


ROLE = 12345678901234569
USER = 12345678901234570


def test_an_approval_post_never_pings_the_configured_role_or_users_even_with_their_notation(harness):
    adapter, channel, _ = harness(_record())
    adapter.settings = BotSettings(guild_id=GUILD, channel_id=CHANNEL, mention_role_id=ROLE,
                                   mention_user_ids=(USER,))
    hostile = {"status": "pending", "payload_sha256": DIGEST, "change": {
        "name": "sma_cross", "kind": "strategy", "classification": "new",
        "params": {"items": []}, "performance": {"in_sample": []},
        "summary": f"<@&{ROLE}> <@{USER}> @everyone"}}

    class Client(_Client):
        def approval(self, approval_id):
            return Reply(200, hostile, None)
    adapter.client = Client()
    channel.edit_outcomes[101] = ["gone"]

    asyncio.run(adapter._repost(adapter.gate.state.record(7)))
    sent = channel.sent[0]
    assert sent["allowed_mentions"].to_dict() == {"parse": []}
    assert f"<@&{ROLE}>" not in sent["content"] and f"<@{USER}>" not in sent["content"]
    assert "@everyone" not in sent["content"]


class _DecidedClient(_Client):
    def __init__(self, reply=None, error=None) -> None:
        super().__init__()
        self.reply, self.error, self.calls = reply, error, []

    def approval(self, approval_id: int) -> Reply:
        self.calls.append(approval_id)
        if self.error is not None:
            raise self.error
        return self.reply


def _decided(status="approved", **fields):
    detail = {"status": status, "payload_sha256": DIGEST, "decided_by": "api:ops-bot",
              "decided_at": "2026-10-19T14:16:00+00:00",
              "change": {"name": "sma_cross", "kind": "strategy", "classification": "updated",
                         "params": {"items": [{"key": "params.period", "old": "14", "new": "10"}]},
                         "performance": {"in_sample": [{"pair": "USDJPY", "trades": 120, "pf": 1.25,
                                                        "avg_r": 0.05}]},
                         "summary": "短い申告です"}}
    detail.update(fields)
    return detail


@pytest.mark.parametrize("status,label", [("approved", "承認済み"), ("rejected", "却下"),
                                           ("expired", "失効")])
def test_ac30_the_terminal_edit_keeps_the_summary_adds_the_state_and_removes_the_view(
        harness, status, label):
    adapter, channel, _ = harness(_record(state="done", edit_pending=True))
    adapter.client = _DecidedClient(Reply(200, _decided(status), None))
    asyncio.run(adapter._update_display(adapter.gate.state.record(7)))
    assert len(channel.edits) == 1
    message_id, kwargs = channel.edits[0]
    assert message_id == 101 and kwargs["view"] is None
    content = kwargs["content"]
    assert f"状態: {label} (2026-10-19 14:16 UTC)" in content
    assert "承認 #7 / 戦略 sma\\_cross (更新)" in content and "params.period: 14 → 10" in content
    assert "USDJPY" in content and "短い申告です" in content
    assert "ops-bot" not in content and "api:" not in content
    assert adapter.gate.state.record(7) is None


@pytest.mark.parametrize("client", [
    _DecidedClient(Reply(404, None, "not_found")),
    _DecidedClient(Reply(503, None, "unavailable")),
    _DecidedClient(Reply(200, _decided("pending"), None)),
    _DecidedClient(Reply(200, _decided("invalidated"), None)),
    _DecidedClient(error=OSError("daemon down")),
])
def test_ac30_without_a_readable_result_the_view_is_still_removed_with_the_fixed_sentence(
        harness, client):
    adapter, channel, _ = harness(_record(state="done", edit_pending=True))
    adapter.client = client
    asyncio.run(adapter._update_display(adapter.gate.state.record(7)))
    assert channel.edits == [(101, {"content": "Approval is no longer active.", "view": None,
             "allowed_mentions": _NO_MENTIONS})]
    assert adapter.gate.state.record(7) is None


CANARIES = ("HOLDOUT_CANARY_PAIR", "31337.5", "RATIONALE_CANARY_ZX81", "canary-host.example.test",
            "/srv/canary/abs/path", "https://canary-url.example.test/x")


def _canary_detail(status="pending"):
    detail = _decided(status)
    detail["change"]["performance"]["holdout"] = [
        {"pair": "HOLDOUT_CANARY_PAIR", "trades": 99, "pf": 31337.5, "avg_r": 1.0}]
    detail["change"]["selection_rationale"] = "RATIONALE_CANARY_ZX81"
    detail["change"]["agent_claims"] = {"selection_rationale": "RATIONALE_CANARY_ZX81"}
    detail["host"] = "canary-host.example.test"
    detail["path"] = "/srv/canary/abs/path"
    detail["url"] = "https://canary-url.example.test/x"
    return detail


class _CanaryClient(_Client):
    def __init__(self, detail) -> None:
        super().__init__()
        self.detail = detail

    def approval(self, approval_id: int) -> Reply:
        return Reply(200, self.detail, None)


def _snapshot_text(tmp_path) -> str:
    names = sorted(os.listdir(tmp_path))
    assert "bot.snapshot" in names
    return "\n".join((tmp_path / name).read_text(errors="replace") for name in names
                     if (tmp_path / name).is_file())


def _assert_clean(bodies, tmp_path):
    for body in bodies:
        assert body
        for canary in CANARIES:
            assert canary not in body
    state = _snapshot_text(tmp_path)
    for canary in CANARIES + ("短い申告です", "params.period", '"change"', "claims"):
        assert canary not in state


def _path_first_post(adapter, channel):
    channel.history_items = []
    adapter.client = _CanaryClient(_canary_detail("pending"))
    asyncio.run(adapter._post_pending(7))
    return [s["content"] for s in channel.sent], [s["allowed_mentions"] for s in channel.sent]


def _path_repost(adapter, channel):
    adapter.client = _CanaryClient(_canary_detail("pending"))
    channel.edit_outcomes[101] = ["gone"]
    asyncio.run(adapter._update_display(adapter.gate.state.record(7)))
    return ([s["content"] for s in channel.sent], [s["allowed_mentions"] for s in channel.sent])


def _path_terminal_edit(adapter, channel):
    adapter.client = _CanaryClient(_canary_detail("approved"))
    asyncio.run(adapter._update_display(adapter.gate.state.record(7)))
    return ([k["content"] for _, k in channel.edits],
            [k["allowed_mentions"] for _, k in channel.edits if "allowed_mentions" in k])


_PATHS = {
    "first_post": (_path_first_post, None),
    "repost": (_path_repost, _record(display_generation=2, edit_pending=True)),
    "terminal_edit": (_path_terminal_edit, _record(state="done", edit_pending=True)),
}


@pytest.mark.parametrize("path", sorted(_PATHS))
def test_every_post_and_edit_route_sends_no_mentions_and_no_unlisted_values(harness, tmp_path, path):
    run, record = _PATHS[path]
    adapter, channel, _ = harness(_record() if record is None else record)
    if record is None:
        adapter.gate.state = BotState.empty()
    adapter.settings = BotSettings(guild_id=GUILD, channel_id=CHANNEL, mention_role_id=ROLE,
                                   mention_user_ids=(USER,))
    # 入力側に canary があること (画面に出ないのは入力が無いからではない)
    source = repr(_canary_detail())
    for canary in CANARIES:
        assert canary in source
    bodies, mentions = run(adapter, channel)
    assert len(bodies) == 1 and len(mentions) == 1
    assert all(m.to_dict() == {"parse": []} for m in mentions)
    _assert_clean(bodies, tmp_path)
