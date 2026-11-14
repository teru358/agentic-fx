"""Discord REST の失敗の分類を、呼出しの文脈ごとに確かめる (adapter の全 REST 呼出し)。

恒久 (設定・認証・権限の拒否) は ``BotFailure("discord_rejected")``、一時 (429 / 5xx / network /
timeout) は既存の再試行、投稿の消失 (404 かつ code 10008) は fetch / edit に限り既存の回復、
利用者の操作一回への応答 (defer / followup) の失敗は bot を止めない。
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import aiohttp
import discord
import pytest

from afx_ops.api_client import Reply
from afx_ops.bot import adapter as adapter_module
from afx_ops.bot import rest
from afx_ops.bot.adapter import BotAdapter, BotFailure, BotSettings
from afx_ops.bot.progress import ProgressTracker
from afx_ops.gate.render import footer
from afx_ops.gate.service import GateService
from afx_ops.gate.state import ApprovalRecord, BotState, initialize_event_cursor, load, save

GUILD = 12345678901234567
CHANNEL = 12345678901234568
USER = 12345678901234569
DIGEST = "a" * 64
SETTINGS = BotSettings(guild_id=GUILD, channel_id=CHANNEL, mention_role_id=None, mention_user_ids=())
APPROVED = {"id": 1, "ts": "2026-10-22T00:00:00+00:00", "code": "approval_state_changed",
            "ref": "approval:7", "fields": {"state": "approved", "decision_code": "approve"}}


def http(status: int, code: int = 0) -> discord.HTTPException:
    message = {"code": code, "message": "x"} if code else "x"
    return discord.HTTPException(SimpleNamespace(status=status, reason="r"), message)


# 設定・認証・権限の拒否 (恒久)
PERMANENT = {
    "400": http(400),
    "401": http(401),
    "403/50001": http(403, 50001),
    "403/50013": http(403, 50013),
    "404/10003": http(404, 10003),
    "404/no code": http(404),
    "404/other code": http(404, 10004),
    # status が message 局所の 400 でも、共有の配送先・権限の code は恒久
    "400/10003": http(400, 10003),
    "400/50001": http(400, 50001),
    "400/50013": http(400, 50013),
}
# 429 / 5xx / network / timeout (一時)
TRANSIENT = {
    "429": http(429),
    "500": http(500),
    "503": http(503),
    "oserror": OSError("down"),
    "timeout": asyncio.TimeoutError(),
    "client error": aiohttp.ClientConnectionError(),
    # Discord の JSON code が無い 403 (前段の proxy や Cloudflare) と 408 は一時
    "403/no code": http(403),
    "408": http(408),
}
GONE = http(404, 10008)


def permanent():
    return [pytest.param(v, id=k) for k, v in PERMANENT.items()]


def transient():
    return [pytest.param(v, id=k) for k, v in TRANSIENT.items()]


def test_the_classifier_separates_gone_permanent_and_transient():
    assert rest.is_message_gone(GONE)
    for exc in [*PERMANENT.values(), *TRANSIENT.values(), http(403, 10008), http(400, 10008)]:
        assert not rest.is_message_gone(exc)
    assert all(rest.classify(e) == rest.PERMANENT for e in PERMANENT.values())
    assert all(rest.classify(e) == rest.TRANSIENT for e in TRANSIENT.values())
    assert rest.classify(discord.LoginFailure("x")) == rest.PERMANENT


def test_a_shared_target_code_wins_over_the_status():
    for code in (10003, 50001, 50013):
        for status in (400, 401, 403, 404, 500):
            assert rest.classify(http(status, code)) == rest.PERMANENT
            assert rest.is_shared_rejection(http(status, code))
    # message 局所の 400 は共有の拒否ではない。一時 (429 / 5xx / network) もそうではない
    assert not rest.is_shared_rejection(http(400))
    assert not rest.is_shared_rejection(http(400, 50035))
    for exc in TRANSIENT.values():
        assert not rest.is_shared_rejection(exc)


class Channel:
    """send / edit / fetch / history を、呼出しごとの script (例外を返す) で動かす。"""

    def __init__(self) -> None:
        self.script = {"send": [], "edit": [], "fetch": [], "history": []}
        self.calls = {"send": 0, "edit": 0, "fetch": 0, "history": 0}
        self.sent: list[dict] = []
        self.edits: list[tuple[int, dict]] = []
        self.fetched: dict[int, object] = {}
        self.history_items: list = []
        self._next = 900

    def _step(self, name: str):
        self.calls[name] += 1
        step = self.script[name].pop(0) if self.script[name] else None
        return step

    async def _play(self, name: str) -> None:
        step = self._step(name)
        if step == "hang":
            await asyncio.Event().wait()
        if step is not None:
            raise step

    async def send(self, content=None, **kwargs):
        await self._play("send")
        self._next += 1
        self.sent.append({"content": content, **kwargs})
        return SimpleNamespace(id=self._next)

    def get_partial_message(self, message_id):
        channel = self

        class _Partial:
            async def edit(self, **kwargs):
                await channel._play("edit")
                channel.edits.append((message_id, kwargs))
        return _Partial()

    async def fetch_message(self, message_id):
        await self._play("fetch")
        return self.fetched[message_id]

    async def history(self, limit=None):
        await self._play("history")
        for item in self.history_items:
            yield item


class Discord:
    user = SimpleNamespace(id=5)

    def __init__(self, channel) -> None:
        self.channel = channel

    def get_partial_messageable(self, channel_id, *, guild_id=None):
        return self.channel

    def add_view(self, view, *, message_id=None) -> None:
        return None


class Client:
    def __init__(self, *, approvals=(), pages=()) -> None:
        self.listed = approvals
        self.pages = list(pages)
        self.calls: list[str] = []

    def approval(self, approval_id, *, timeout=None):
        self.calls.append("approval")
        return Reply(200, {"status": "pending", "payload_sha256": DIGEST}, None)

    def approvals(self, *, limit):
        self.calls.append("approvals")
        return Reply(200, {"approvals": [{"id": i} for i in self.listed]}, None)

    def events(self, *, after, limit):
        page = self.pages[0] if len(self.pages) == 1 else self.pages.pop(0)
        return Reply(200, page, None)


def _record(**changes):
    values = dict(approval_id=7, message_id=101, guild_id=GUILD, channel_id=CHANNEL,
                  payload_sha256=DIGEST, display_generation=1, state="fresh")
    values.update(changes)
    return ApprovalRecord(**values)


class Rig:
    def __init__(self, tmp_path, *, record=None, client=None, state=None, **adapter_kwargs) -> None:
        self.fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        self.channel = Channel()
        self.client = client or Client()
        initial = state if state is not None else (
            BotState.empty().with_record(record) if record is not None else BotState.empty())
        self.gate = GateService(initial, guild_id=GUILD, channel_id=CHANNEL,
                                allowlist_user_ids=(USER,), client=self.client)
        self.clock = [1000.0]
        self.progress = ProgressTracker(lambda: self.clock[0])
        self.adapter = BotAdapter(gate=self.gate, client=self.client, dir_fd=self.fd,
                                  settings=SETTINGS, discord_client=Discord(self.channel),
                                  now=lambda: self.clock[0], progress=self.progress,
                                  **adapter_kwargs)

    def close(self) -> None:
        self.adapter.close()
        os.close(self.fd)


@pytest.fixture
def rig(tmp_path):
    made = []

    def build(**kwargs):
        made.append(Rig(tmp_path, **kwargs))
        return made[-1]
    yield build
    for item in made:
        item.close()


def _delivery_rig(rig, pages):
    built = rig(client=Client(pages=pages), state=initialize_event_cursor(BotState.empty(), 0))
    save(built.fd, built.gate.state)
    built.client.approval = lambda approval_id, *, timeout=None: (_ for _ in ()).throw(TimeoutError())
    return built


def _page(*events):
    return {"events": list(events), "high_watermark": events[-1]["id"]}


# ------------------------------------------------------------------ event の send

@pytest.mark.parametrize("failure", permanent())
def test_an_event_send_rejection_is_permanent_and_the_cursor_stays_before_the_event(rig, failure):
    built = _delivery_rig(rig, [_page(APPROVED, {**APPROVED, "id": 2})])
    built.channel.script["send"] = [failure]
    with pytest.raises(BotFailure) as info:
        asyncio.run(built.adapter.deliver_events())
    assert info.value.reason == "discord_rejected"
    assert load(built.fd).cursor == 0 and built.channel.calls["send"] == 1


@pytest.mark.parametrize("failure", transient())
def test_an_event_send_transient_failure_retries_from_the_same_event(rig, failure):
    built = _delivery_rig(rig, [_page(APPROVED, {**APPROVED, "id": 2})])
    built.channel.script["send"] = [failure]
    asyncio.run(built.adapter.deliver_events())
    assert load(built.fd).cursor == 0 and built.channel.calls["send"] == 1
    assert built.progress.delivery_stalled_since is not None
    asyncio.run(built.adapter.deliver_events())
    assert load(built.fd).cursor == 2 and len(built.channel.sent) == 2


SUBSTITUTE = "この通知は表示できませんでした (event 1)"


def test_a_message_local_400_sends_a_fixed_substitute_once_and_moves_on(rig):
    built = _delivery_rig(rig, [_page(APPROVED, {**APPROVED, "id": 2})])
    built.channel.script["send"] = [http(400, 50035)]
    asyncio.run(built.adapter.deliver_events())
    # 1 件目は代替文で確定し、2 件目も通常どおり投稿される
    assert load(built.fd).cursor == 2 and built.channel.calls["send"] == 3
    assert built.channel.sent[0]["content"] == SUBSTITUTE
    assert built.channel.sent[0]["allowed_mentions"].to_dict() == {"parse": []}
    assert built.channel.sent[1]["content"] != SUBSTITUTE
    assert built.progress.delivery_stalled_since is None


@pytest.mark.parametrize("second", [http(400, 50035), http(400), http(403, 50013), http(401)])
def test_a_rejected_substitute_is_permanent_and_the_cursor_stays(rig, second):
    built = _delivery_rig(rig, [_page(APPROVED, {**APPROVED, "id": 2})])
    built.channel.script["send"] = [http(400, 50035), second]
    with pytest.raises(BotFailure) as info:
        asyncio.run(built.adapter.deliver_events())
    assert info.value.reason == "discord_rejected"
    assert load(built.fd).cursor == 0 and built.channel.calls["send"] == 2


@pytest.mark.parametrize("second", transient())
def test_a_transiently_failed_substitute_retries_from_the_same_event(rig, second):
    built = _delivery_rig(rig, [_page(APPROVED, {**APPROVED, "id": 2})])
    built.channel.script["send"] = [http(400, 50035), second]
    asyncio.run(built.adapter.deliver_events())
    assert load(built.fd).cursor == 0 and built.channel.calls["send"] == 2
    assert built.progress.delivery_stalled_since is not None


@pytest.mark.parametrize("second", transient())
def test_after_a_transient_substitute_failure_the_next_round_advances_the_cursor_once(rig, second):
    built = _delivery_rig(rig, [_page(APPROVED, {**APPROVED, "id": 2})])
    built.channel.script["send"] = [http(400, 50035), second, http(400, 50035)]
    asyncio.run(built.adapter.deliver_events())
    assert load(built.fd).cursor == 0
    asyncio.run(built.adapter.deliver_events())
    assert load(built.fd).cursor == 2 and built.channel.calls["send"] == 5
    contents = [sent["content"] for sent in built.channel.sent]
    assert contents.count(SUBSTITUTE) == 1 and contents[0] == SUBSTITUTE
    assert built.progress.delivery_stalled_since is None


def test_the_substitute_carries_no_event_text_or_exception_text(rig):
    built = _delivery_rig(rig, [_page(APPROVED)])
    built.channel.script["send"] = [http(400, 50035)]
    asyncio.run(built.adapter.deliver_events())
    assert built.channel.sent[0]["content"] == SUBSTITUTE


@pytest.mark.parametrize("failure", [http(403), http(408), http(408, 50035), http(408, 40002)])
def test_a_codeless_403_and_a_408_on_an_event_send_are_retried_not_stopped(rig, failure):
    built = _delivery_rig(rig, [_page(APPROVED)])
    built.channel.script["send"] = [failure]
    asyncio.run(built.adapter.deliver_events())
    assert load(built.fd).cursor == 0 and built.channel.sent == []


def test_an_approval_send_keeps_a_message_local_400_permanent(rig):
    # 承認の投稿は View が本体で、代わりの文では押せない。固定の組立てが拒否されるのは
    # 設定か実装の欠陥なので、代替で隠さず停止して知らせる
    built = _pending_rig(rig)
    built.channel.script["send"] = [http(400, 50035)]
    with pytest.raises(BotFailure) as info:
        asyncio.run(built.adapter.sync_pending())
    assert info.value.reason == "discord_rejected"


# ------------------------------------------------------------------ 承認の send (新規 / 出し直し)

def _pending_rig(rig, **kwargs):
    return rig(client=Client(approvals=(7,)), **kwargs)


@pytest.mark.parametrize("failure", permanent())
def test_a_new_approval_post_rejection_is_permanent_and_records_nothing(rig, failure):
    built = _pending_rig(rig)
    built.channel.script["send"] = [failure]
    with pytest.raises(BotFailure) as info:
        asyncio.run(built.adapter.sync_pending())
    assert info.value.reason == "discord_rejected"
    assert built.gate.state.record(7) is None and built.adapter._routes == {}


@pytest.mark.parametrize("failure", transient())
def test_a_new_approval_post_transient_failure_records_nothing_and_does_not_stop(rig, failure):
    built = _pending_rig(rig)
    built.channel.script["send"] = [failure]
    asyncio.run(built.adapter.sync_pending())
    assert built.gate.state.record(7) is None
    asyncio.run(built.adapter.sync_pending())
    assert built.gate.state.record(7) is not None


@pytest.mark.parametrize("failure", permanent())
def test_a_repost_send_rejection_is_permanent_and_does_not_move_the_record(rig, failure):
    built = rig(record=_record(edit_pending=True))
    built.channel.script["send"] = [failure]
    with pytest.raises(BotFailure) as info:
        asyncio.run(built.adapter._repost(built.gate.state.record(7)))
    assert info.value.reason == "discord_rejected"
    assert built.gate.state.record(7).message_id == 101


@pytest.mark.parametrize("failure", transient())
def test_a_repost_send_transient_failure_returns_false(rig, failure):
    built = rig(record=_record(edit_pending=True))
    built.channel.script["send"] = [failure]
    assert asyncio.run(built.adapter._repost(built.gate.state.record(7))) is False
    assert built.gate.state.record(7).message_id == 101


# ------------------------------------------------------------------ history

@pytest.mark.parametrize("failure", [*permanent(), pytest.param(GONE, id="404/10008")])
def test_a_history_permission_failure_is_permanent_and_is_not_read_as_an_empty_history(rig, failure):
    built = _pending_rig(rig)
    built.channel.script["history"] = [failure]
    with pytest.raises(BotFailure) as info:
        asyncio.run(built.adapter.sync_pending())
    assert info.value.reason == "discord_rejected"
    assert built.channel.calls["send"] == 0


class Sleeps:
    """adapter の待ちを記録するだけの sleep (実時間は進めない)。"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.mark.parametrize("failure", transient())
def test_a_history_transient_failure_is_held_and_retried_never_read_as_empty(rig, failure):
    sleeps = Sleeps()
    built = _pending_rig(rig, sleep=sleeps)
    built.channel.script["history"] = [failure, failure]
    built.channel.script["send"] = []
    sent_at_failure = []
    original = built.channel._play

    async def play(name):
        if name == "history":
            sent_at_failure.append(built.channel.calls["send"])
        await original(name)
    built.channel._play = play
    asyncio.run(built.adapter.sync_pending())
    # 履歴が読めるまで投稿せず、読めた後に (履歴に無いので) 一度だけ投稿する
    assert sent_at_failure == [0, 0, 0]
    assert built.channel.calls["history"] == 3 and built.channel.calls["send"] == 1
    assert sleeps.delays == [2.0, 4.0]
    assert built.gate.state.record(7) is not None


def test_a_history_retry_backs_off_up_to_the_cap_and_a_permanent_failure_ends_it(rig):
    sleeps = Sleeps()
    built = _pending_rig(rig, sleep=sleeps)
    built.channel.script["history"] = [http(503)] * 8 + [http(403, 50013)]
    with pytest.raises(BotFailure) as info:
        asyncio.run(built.adapter.sync_pending())
    assert info.value.reason == "discord_rejected"
    assert sleeps.delays == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]
    assert built.channel.calls["send"] == 0


# ------------------------------------------------------------------ fetch

def test_a_fetch_of_a_vanished_message_returns_none_and_startup_reposts_it(rig):
    built = rig(record=_record())
    built.channel.script["fetch"] = [GONE]
    asyncio.run(built.adapter.reconcile_startup())
    assert len(built.channel.sent) == 1
    assert built.gate.state.record(7).message_id == 901


def test_a_fetch_none_for_a_gone_message(rig):
    built = rig(record=_record())
    built.channel.script["fetch"] = [GONE]
    assert asyncio.run(built.adapter._fetch(101)) is None


@pytest.mark.parametrize("failure", permanent())
def test_a_fetch_rejection_is_permanent_never_none(rig, failure):
    built = rig(record=_record())
    built.channel.script["fetch"] = [failure]
    with pytest.raises(BotFailure) as info:
        asyncio.run(built.adapter._fetch(101))
    assert info.value.reason == "discord_rejected"


def _stored_message(message_id):
    return SimpleNamespace(id=message_id, author=Discord.user, channel=SimpleNamespace(id=CHANNEL),
                           embeds=[])


@pytest.mark.parametrize("failure", transient())
def test_a_fetch_transient_failure_is_held_and_retried_never_a_vanished_message(rig, failure):
    sleeps = Sleeps()
    built = rig(record=_record(), sleep=sleeps)
    built.channel.fetched[101] = _stored_message(101)
    built.channel.script["fetch"] = [failure, failure, failure]
    found = asyncio.run(built.adapter._fetch(101))
    assert found is not None and found.message_id == 101
    assert built.channel.calls["fetch"] == 4 and sleeps.delays == [2.0, 4.0, 8.0]
    assert built.channel.sent == []


@pytest.mark.parametrize("failure", transient())
def test_startup_holds_on_a_transient_fetch_failure_without_reposting(rig, failure):
    sleeps = Sleeps()
    built = rig(record=_record(), sleep=sleeps)
    built.channel.fetched[101] = _stored_message(101)
    built.channel.script["fetch"] = [failure]
    sent_at_fetch = []
    original = built.channel._play

    async def play(name):
        if name == "fetch":
            sent_at_fetch.append(len(built.channel.sent))
        await original(name)
    built.channel._play = play
    asyncio.run(built.adapter.reconcile_startup())
    # 失敗のあいだは投稿せず、読めた後の判断 (この投稿は footer が無いので出し直し) に進む
    assert sent_at_fetch == [0, 0] and sleeps.delays == [2.0]


def test_a_fetch_retry_ends_at_once_on_a_permanent_rejection_and_on_a_vanished_message(rig):
    sleeps = Sleeps()
    built = rig(record=_record(), sleep=sleeps)
    built.channel.script["fetch"] = [http(503), GONE]
    assert asyncio.run(built.adapter._fetch(101)) is None
    built.channel.script["fetch"] = [OSError(), http(401)]
    with pytest.raises(BotFailure) as info:
        asyncio.run(built.adapter._fetch(101))
    assert info.value.reason == "discord_rejected"


@pytest.mark.parametrize("failure", permanent())
def test_startup_with_a_permanent_fetch_rejection_does_not_repost_or_adopt(rig, failure):
    built = rig(record=_record())
    built.channel.script["fetch"] = [failure]
    with pytest.raises(BotFailure):
        asyncio.run(built.adapter.reconcile_startup())
    assert built.channel.sent == [] and built.gate.state.record(7).message_id == 101


# ------------------------------------------------------------------ edit

def test_a_pending_edit_of_a_vanished_message_is_reposted_and_the_record_follows(rig):
    built = rig(record=_record(display_generation=2, edit_pending=True))
    built.channel.script["edit"] = [GONE]
    asyncio.run(built.adapter._update_display(built.gate.state.record(7)))
    record = built.gate.state.record(7)
    assert len(built.channel.sent) == 1
    assert (record.message_id, record.display_generation, record.edit_pending) == (901, 3, False)


def test_a_terminal_edit_of_a_vanished_message_completes_without_a_repost(rig):
    built = rig(record=_record(state="done", edit_pending=True))
    built.channel.script["edit"] = [GONE]
    asyncio.run(built.adapter._update_display(built.gate.state.record(7)))
    assert built.channel.sent == [] and built.gate.state.record(7) is None


@pytest.mark.parametrize("failure", permanent())
def test_a_pending_edit_rejection_is_permanent_and_never_a_repost(rig, failure):
    built = rig(record=_record(display_generation=2, edit_pending=True))
    built.channel.script["edit"] = [failure]
    with pytest.raises(BotFailure) as info:
        asyncio.run(built.adapter._update_display(built.gate.state.record(7)))
    assert info.value.reason == "discord_rejected"
    assert built.channel.sent == [] and built.gate.state.record(7).message_id == 101


@pytest.mark.parametrize("failure", transient())
def test_a_pending_edit_transient_failure_backs_off_and_neither_reposts_nor_stops(rig, failure):
    built = rig(record=_record(display_generation=2, edit_pending=True))
    built.channel.script["edit"] = [failure]
    asyncio.run(built.adapter._update_display(built.gate.state.record(7)))
    assert built.channel.sent == [] and built.gate.state.record(7).edit_pending is True
    assert built.adapter._edit_retry[7][1] == 1


# 終端の View の取り外しは cleanup: 投稿一つに閉じた 400 は有限の失敗として既存の再試行に回す。
# channel・認証・権限の拒否は cleanup でも恒久
_TERMINAL_PERMANENT = [k for k in PERMANENT if k != "400"]


@pytest.mark.parametrize("key", _TERMINAL_PERMANENT)
def test_a_terminal_edit_channel_or_permission_rejection_is_permanent(rig, key):
    built = rig(record=_record(state="done", edit_pending=True))
    built.channel.script["edit"] = [PERMANENT[key]]
    with pytest.raises(BotFailure) as info:
        asyncio.run(built.adapter._update_display(built.gate.state.record(7)))
    assert info.value.reason == "discord_rejected"
    assert built.gate.state.record(7) is not None


def test_a_terminal_edit_message_local_400_is_a_finite_failure_with_the_existing_limit(rig):
    built = rig(record=_record(state="done", edit_pending=True))
    built.channel.script["edit"] = [http(400)] * 20

    async def go():
        for _ in range(adapter_module.EDIT_RETRY_LIMIT + 2):
            record = built.gate.state.record(7)
            if record is None:
                break
            built.clock[0] += 1000
            await built.adapter._update_display(record)
    asyncio.run(go())
    # 8 回で諦め (abandon)、それ以上は試さない
    assert built.channel.calls["edit"] == adapter_module.EDIT_RETRY_LIMIT
    assert built.adapter.failure is None


@pytest.mark.parametrize("failure", transient())
def test_a_terminal_edit_transient_failure_stays_on_the_retry_path(rig, failure):
    built = rig(record=_record(state="done", edit_pending=True))
    built.channel.script["edit"] = [failure]
    asyncio.run(built.adapter._update_display(built.gate.state.record(7)))
    assert built.adapter._edit_retry[7][1] == 1 and built.gate.state.record(7) is not None


# ------------------------------------------------------------------ 不採用の再投稿の cleanup (単発)

def test_cleanup_edit_outcomes(rig):
    built = rig(record=_record())

    async def edit(failure):
        built.channel.script["edit"] = [failure]
        return await built.adapter._edit(901, cleanup=True, view=None)

    assert asyncio.run(edit(http(400))) == "failed"
    assert asyncio.run(edit(GONE)) == "gone"
    for key in _TERMINAL_PERMANENT:
        with pytest.raises(BotFailure) as info:
            asyncio.run(edit(PERMANENT[key]))
        assert info.value.reason == "discord_rejected"
    for failure in TRANSIENT.values():
        assert asyncio.run(edit(failure)) == "failed"


def test_cleanup_edit_with_a_deadline_gives_up_on_a_hung_await(rig):
    built = rig(record=_record())
    built.channel.script["edit"] = ["hang"]
    got = asyncio.run(asyncio.wait_for(
        built.adapter._edit(901, cleanup=True, deadline=0.05, view=None), 2))
    assert got == "failed"


def _unadopted_repost(built, edit_failure):
    """送信の間に意図が変わり、不採用の投稿を cleanup で閉じる経路。"""
    real_send = built.channel.send

    async def send(content=None, **kwargs):
        message = await real_send(content, **kwargs)
        record = built.gate.state.record(7)
        from dataclasses import replace
        built.gate.state = built.gate.state.replace_record(
            replace(record, repost_generation=record.display_generation + 5))
        return message
    built.channel.send = send
    built.channel.script["edit"] = [edit_failure]
    return asyncio.run(built.adapter._repost(built.gate.state.record(7)))


def test_an_unadopted_repost_cleanup_with_a_400_ends_once_without_stopping_the_bot(rig):
    built = rig(record=_record())
    assert _unadopted_repost(built, http(400)) is False
    assert built.channel.calls["edit"] == 1 and built.adapter.failure is None


@pytest.mark.parametrize("key", ["403/50013", "400/50013", "400/50001", "400/10003", "401"])
def test_an_unadopted_repost_cleanup_does_not_swallow_a_permission_rejection(rig, key):
    built = rig(record=_record())
    with pytest.raises(BotFailure) as info:
        _unadopted_repost(built, PERMANENT[key])
    assert info.value.reason == "discord_rejected"


# ------------------------------------------------------------------ defer / followup

def _component(custom_id, *, defer, followup, message=None):
    return SimpleNamespace(user=SimpleNamespace(id=USER), data={"custom_id": custom_id},
                           guild_id=GUILD, channel_id=CHANNEL, message=message,
                           response=SimpleNamespace(defer=defer),
                           followup=SimpleNamespace(send=followup))


class Recorder:
    def __init__(self, *, defer_script=(), followup_script=()) -> None:
        self.defer_script = list(defer_script)
        self.followup_script = list(followup_script)
        self.defers = 0
        self.followups: list[dict] = []
        self.next_id = 300

    async def defer(self, **kwargs) -> None:
        self.defers += 1
        step = self.defer_script.pop(0) if self.defer_script else None
        if step == "hang":
            await asyncio.Event().wait()
        if step is not None:
            raise step

    async def followup(self, content, **kwargs):
        step = self.followup_script.pop(0) if self.followup_script else None
        if step == "hang":
            await asyncio.Event().wait()
        if step is not None:
            raise step
        self.followups.append({"content": content, **kwargs})
        if kwargs.get("wait"):
            self.next_id += 1
            return SimpleNamespace(id=self.next_id)
        return None

    def interaction(self, custom_id="afx:v1:approve:7"):
        return _component(custom_id, defer=self.defer, followup=self.followup)


# interaction token の失効・二重 ACK 等 (一操作で終わる)
INTERACTION_FAILURES = {
    "10062": http(404, 10062), "10015": http(404, 10015), "50027": http(403, 50027),
    "40060": http(400, 40060), "401": http(401), "403": http(403),
    "interaction responded": discord.InteractionResponded(SimpleNamespace()),
    **{f"transient {k}": v for k, v in TRANSIENT.items()},
}


def _interaction_params():
    return [pytest.param(v, id=k) for k, v in INTERACTION_FAILURES.items()]


def _click_rig(rig, **kwargs):
    built = rig(record=_record(), **kwargs)
    built.adapter._register(built.gate.state.record(7))
    return built


@pytest.mark.parametrize("failure", _interaction_params())
def test_a_failed_defer_ends_that_operation_without_touching_gate_or_daemon_and_the_bot_lives(rig, failure):
    built = _click_rig(rig)
    handled = []
    real_handle = built.gate.handle
    built.gate.handle = lambda *a, **k: handled.append(a) or real_handle(*a, **k)
    first = Recorder(defer_script=[failure])

    asyncio.run(built.adapter.on_component(first.interaction(), message_id=101, generation=1))

    assert handled == [] and first.followups == [] and built.client.calls == []
    assert built.adapter.failure is None and not built.adapter.failed.is_set()
    # 次の interaction は通常どおり処理される
    second = Recorder()
    asyncio.run(built.adapter.on_component(second.interaction(), message_id=101, generation=1))
    assert len(handled) == 1 and len(second.followups) == 1


@pytest.mark.parametrize("failure", _interaction_params())
def test_a_failed_followup_with_a_view_stops_the_view_and_binds_nothing(rig, failure):
    built = _click_rig(rig)
    recorder = Recorder(followup_script=[failure])
    asyncio.run(built.adapter.on_component(recorder.interaction(), message_id=101, generation=1))
    assert built.adapter.failure is None
    assert [m for m in built.adapter._routes if m != 101] == []
    record = built.gate.state.record(7)
    assert record.confirm_message_id is None
    # bot は生きていて、次の poll も回る
    built.client.pages = [{"events": [], "high_watermark": 0}]
    asyncio.run(built.adapter.poll_once())


@pytest.mark.parametrize("failure", _interaction_params())
def test_a_failed_followup_without_a_view_keeps_the_decided_result_and_is_not_resent(rig, failure):
    built = _click_rig(rig)
    built.gate.handle = lambda request, now: adapter_module.GateResult("Nothing to do.")
    recorder = Recorder(followup_script=[failure])
    asyncio.run(built.adapter.on_component(recorder.interaction(), message_id=101, generation=1))
    assert built.adapter.failure is None and recorder.followups == []


def test_a_successful_followup_with_a_view_still_binds_the_confirm_message(rig):
    built = _click_rig(rig)
    recorder = Recorder()
    asyncio.run(built.adapter.on_component(recorder.interaction(), message_id=101, generation=1))
    confirm = [m for m in built.adapter._routes if m != 101]
    assert confirm == [301] and built.gate.state.record(7).confirm_message_id == 301


def test_the_response_deadline_is_ten_seconds_by_default():
    assert adapter_module.RESPONSE_DEADLINE_SECONDS == 10.0


def test_a_defer_that_never_returns_is_cut_off_at_the_deadline(rig):
    built = _click_rig(rig, response_deadline=0.05)
    recorder = Recorder(defer_script=["hang"])
    asyncio.run(asyncio.wait_for(
        built.adapter.on_component(recorder.interaction(), message_id=101, generation=1), 3))
    assert built.client.calls == [] and built.adapter.failure is None


def test_a_followup_that_never_returns_is_cut_off_and_its_view_is_stopped(rig):
    built = _click_rig(rig, response_deadline=0.05)
    recorder = Recorder(followup_script=["hang"])
    asyncio.run(asyncio.wait_for(
        built.adapter.on_component(recorder.interaction(), message_id=101, generation=1), 3))
    assert [m for m in built.adapter._routes if m != 101] == []
    assert built.gate.state.record(7).confirm_message_id is None


def test_a_permanent_rejection_found_while_syncing_views_after_a_click_stops_the_bot(rig):
    built = rig(record=_record(display_generation=2, edit_pending=True))
    built.adapter._register(built.gate.state.record(7))
    built.channel.script["edit"] = [http(403, 50013)]
    built.gate.handle = lambda request, now: adapter_module.GateResult("Nothing to do.")
    recorder = Recorder()
    asyncio.run(built.adapter.on_component(recorder.interaction(), message_id=101, generation=1))
    assert built.adapter.failure is not None and built.adapter.failure.reason == "discord_rejected"
    assert built.adapter.failed.is_set()
