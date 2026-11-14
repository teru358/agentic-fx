"""bot の進捗の監視: gateway / 配送 / poll 周回の 10 分、monitor、login の再試行。

時計は全部 fake の monotonic。窓のちょうど手前 (9 分 59 秒) は生存、ちょうど (10 分) で一時の失敗。
"""
from __future__ import annotations

import asyncio
import os
import threading
from types import SimpleNamespace

import discord
import pytest

from afx_ops.api_client import Reply
from afx_ops.bot import adapter as adapter_module
from afx_ops.bot import runtime
from afx_ops.bot.adapter import BotAdapter, BotFailure, BotSettings
from afx_ops.bot.progress import WINDOW_SECONDS, ProgressTracker
from afx_ops.gate.service import GateService
from afx_ops.gate.state import BotState, initialize_event_cursor, load, save

WINDOW = 600.0
GUILD = 12345678901234567
CHANNEL = 12345678901234568


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


def tracker(clock: Clock) -> ProgressTracker:
    return ProgressTracker(clock)


def settle(progress: ProgressTracker) -> None:
    """gateway が健全で、poll が一周済みの状態 (配送だけを見たいとき)。"""
    progress.gateway_connected()
    progress.poll_begin()
    progress.poll_end()


def test_the_window_is_ten_minutes():
    assert WINDOW_SECONDS == 600.0


# ------------------------------------------------------------------ gateway

def test_starting_counts_from_process_start_and_expires_at_exactly_ten_minutes():
    clock = Clock()
    progress = tracker(clock)
    clock.t += 599
    assert progress.expired() is None
    clock.t += 1
    assert progress.expired() == "gateway_unhealthy"


@pytest.mark.parametrize("resume", [False, True])
def test_ready_and_resumed_clear_the_unhealthy_timer(resume):
    clock = Clock()
    progress = tracker(clock)
    clock.t += 599
    (progress.gateway_resumed if resume else progress.gateway_connected)()
    assert progress.gateway_state == "healthy" and progress.gateway_unhealthy_since is None
    clock.t += 5000
    # poll が止まっているわけではないことにして gateway だけを見る
    progress.poll_begin()
    progress.poll_end()
    assert progress.expired() is None


def test_disconnect_after_ready_starts_a_window_from_the_first_disconnect_only():
    clock = Clock()
    progress = tracker(clock)
    settle(progress)
    clock.t += 100
    progress.gateway_disconnected()
    first = progress.gateway_unhealthy_since
    assert progress.gateway_state == "unhealthy" and first == clock.t
    for _ in range(50):
        clock.t += 10
        progress.gateway_disconnected()
        assert progress.gateway_unhealthy_since == first
    # 最初の切断から 10 分 (連打の途中の時刻からではない)
    clock.t = first + 599
    progress.poll_begin()
    progress.poll_end()
    assert progress.expired() is None
    clock.t = first + 600
    assert progress.expired() == "gateway_unhealthy"


def test_disconnect_while_still_starting_does_not_restart_the_window():
    clock = Clock()
    progress = tracker(clock)
    clock.t += 300
    progress.gateway_disconnected()
    assert progress.gateway_state == "starting"
    clock.t += 300
    assert progress.expired() == "gateway_unhealthy"


def test_reconnect_then_resumed_clears_and_a_later_disconnect_opens_a_new_window():
    clock = Clock()
    progress = tracker(clock)
    settle(progress)
    clock.t += 50
    progress.gateway_disconnected()
    clock.t += 500
    progress.gateway_resumed()
    clock.t += 500
    progress.poll_begin()
    progress.poll_end()
    assert progress.expired() is None
    progress.gateway_disconnected()
    clock.t += 599
    assert progress.expired() is None
    clock.t += 1
    assert progress.expired() == "gateway_unhealthy"


def test_an_unhealthy_gateway_does_not_touch_the_delivery_or_poll_timers():
    clock = Clock()
    progress = tracker(clock)
    progress.gateway_connected()
    progress.gateway_disconnected()
    assert progress.delivery_stalled_since is None and progress.poll_started_at is None


# ------------------------------------------------------------------ 配送

def test_the_delivery_timer_starts_at_the_first_attempt_and_is_not_moved_by_later_ones():
    clock = Clock()
    progress = tracker(clock)
    settle(progress)
    progress.delivery_begin(5)
    first = progress.delivery_stalled_since
    for _ in range(20):
        clock.t += 25
        progress.delivery_begin(5)
        assert progress.delivery_stalled_since == first
        progress.poll_begin()
        progress.poll_end()
    clock.t = first + 599
    assert progress.expired() is None
    clock.t = first + 600
    assert progress.expired() == "delivery_stalled"


def test_only_a_committed_event_clears_the_delivery_timer():
    clock = Clock()
    progress = tracker(clock)
    settle(progress)
    progress.delivery_begin(5)
    started = progress.delivery_stalled_since
    # send が成功しただけ、別の REST / gateway / poll の成功では消えない
    progress.gateway_connected()
    progress.gateway_resumed()
    progress.poll_begin()
    progress.poll_end()
    assert progress.delivery_stalled_since == started
    clock.t += 600
    progress.poll_begin()
    progress.poll_end()
    assert progress.expired() == "delivery_stalled"
    progress.delivery_committed(5)
    assert progress.delivery_stalled_since is None and progress.expired() is None
    assert progress.last_delivery_success_at == clock.t


def test_a_commit_of_an_earlier_event_does_not_clear_the_timer_of_a_later_one():
    clock = Clock()
    progress = tracker(clock)
    settle(progress)
    progress.delivery_begin(7)
    progress.delivery_committed(6, delivered=False)
    assert progress.delivery_stalled_since is not None


def test_the_next_event_after_a_commit_opens_a_new_window():
    clock = Clock()
    progress = tracker(clock)
    settle(progress)
    progress.delivery_begin(1)
    clock.t += 590
    progress.delivery_committed(1)
    clock.t += 5
    progress.delivery_begin(2)
    clock.t += 595
    progress.poll_begin()
    progress.poll_end()
    assert progress.expired() is None
    clock.t += 5
    assert progress.expired() == "delivery_stalled"


# ------------------------------------------------------------------ poller

def test_the_first_poll_has_ten_minutes_from_process_start():
    clock = Clock()
    progress = tracker(clock)
    progress.gateway_connected()
    clock.t += 599
    assert progress.expired() is None
    clock.t += 1
    assert progress.expired() == "poller_stalled"


def test_a_poll_cycle_has_ten_minutes_from_its_own_start_and_idle_time_between_cycles_is_free():
    clock = Clock()
    progress = tracker(clock)
    settle(progress)
    clock.t += 100000
    assert progress.expired() is None
    progress.poll_begin()
    clock.t += 599
    assert progress.expired() is None
    clock.t += 1
    assert progress.expired() == "poller_stalled"
    progress.poll_end()
    assert progress.expired() is None
    assert progress.last_poll_completed_at == clock.t


# ------------------------------------------------------------------ monitor / login

class Sleeper:
    """fake の sleep: 時計を進め、実時間では少しだけ譲る。limit を超えては進めない。"""

    def __init__(self, clock: Clock, on_tick=None) -> None:
        self.clock = clock
        self.on_tick = on_tick
        self.limit: float | None = None
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)
        step = delay if self.limit is None else max(0.0, min(delay, self.limit - self.clock.t))
        self.clock.t += step
        if self.on_tick is not None:
            self.on_tick(self.clock.t)
        await asyncio.sleep(0.002)


def test_the_monitor_raises_the_reason_at_the_window_and_not_before():
    clock = Clock()
    progress = tracker(clock)
    sleeper = Sleeper(clock)
    with pytest.raises(BotFailure) as failure:
        asyncio.run(runtime._monitor(progress, 1.0, sleeper))
    assert failure.value.reason == "gateway_unhealthy"
    assert clock.t == 1000.0 + 600


def test_the_monitor_keeps_running_while_everything_progresses():
    clock = Clock()
    progress = tracker(clock)
    progress.gateway_connected()
    sleeper = Sleeper(clock)

    def tick(now: float) -> None:
        progress.poll_begin()
        progress.poll_end()
        if now >= 1000.0 + 3000:
            raise KeyboardInterrupt
    sleeper.on_tick = tick
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(runtime._monitor(progress, 10.0, sleeper))


class LoginBot:
    def __init__(self, *failures) -> None:
        self.failures = list(failures)
        self.attempts = 0

    async def login(self, token) -> None:
        self.attempts += 1
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure


def http(status: int, code: int = 0) -> discord.HTTPException:
    message = {"code": code, "message": "x"} if code else "x"
    return discord.HTTPException(SimpleNamespace(status=status, reason="r"), message)


async def _no_sleep(delay: float) -> None:
    return None


@pytest.mark.parametrize("failure", [http(429), http(500), http(503), OSError("down"),
                                      asyncio.TimeoutError()])
def test_a_retryable_login_failure_is_retried_in_process(failure):
    bot = LoginBot(failure, failure, None)
    asyncio.run(runtime._login(bot, "token", _no_sleep))
    assert bot.attempts == 3


def test_login_retries_back_off_doubling_up_to_the_cap():
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
    bot = LoginBot(*[OSError() for _ in range(9)], None)
    asyncio.run(runtime._login(bot, "token", sleep))
    assert delays == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0, 60.0]


@pytest.mark.parametrize("failure", [discord.LoginFailure("bad"), http(401), http(403),
                                      http(400), http(404, 10003)])
def test_a_login_rejection_is_permanent_and_not_retried(failure):
    bot = LoginBot(failure, None)
    with pytest.raises(BotFailure) as info:
        asyncio.run(runtime._login(bot, "token", _no_sleep))
    assert info.value.reason == "discord_login_failed" and bot.attempts == 1


# ------------------------------------------------------------------ _main (fake gateway / clock)

CONFIG = SimpleNamespace(guild_id=GUILD, channel_id=CHANNEL, mention_role_id=None,
                         mention_user_ids=(), trade_notices="all", trade_amounts="absolute",
                         ops_notices="default", improve_notices="all", allowlist_user_ids=())


class DaemonClient:
    """events / approvals を静かに返す。events を block する指定もできる。"""

    def __init__(self, events_gate: threading.Event | None = None, pages=()) -> None:
        self.events_gate = events_gate
        self.pages = list(pages)
        self.event_calls = 0

    def events(self, *, after: int, limit: int) -> Reply:
        self.event_calls += 1
        if self.events_gate is not None:
            self.events_gate.wait()
        page = self.pages.pop(0) if self.pages else {"events": [], "high_watermark": after}
        return Reply(200, page, None)

    def approvals(self, *, limit: int) -> Reply:
        return Reply(200, {"approvals": []}, None)

    def approval(self, approval_id: int, *, timeout=None) -> Reply:
        raise TimeoutError()


class Channel:
    def __init__(self, hang: bool = False) -> None:
        self.hang = hang
        self.sent: list[str] = []

    async def send(self, content, **kwargs):
        if self.hang:
            await asyncio.Event().wait()
        self.sent.append(content)


class Harness:
    """fake の gateway と時計で `_main` を回す。"""

    def __init__(self, tmp_path, monkeypatch, *, login=None, connect=None, channel=None,
                 client=None) -> None:
        self.clock = Clock()
        self.sleeper = Sleeper(self.clock)
        self.channel = channel or Channel()
        self.client = client or DaemonClient()
        self.fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        save(self.fd, initialize_event_cursor(BotState.empty(), 0))
        self.stop = threading.Event()
        self.login = login
        self.connect = connect
        self.close_error = False
        self.progress = ProgressTracker(self.clock)
        harness = self

        class FakeBot(runtime._Client):
            async def login(self, token):
                if harness.login is not None:
                    await harness.login(self)

            async def connect(self, *, reconnect=True):
                if harness.connect is not None:
                    await harness.connect(self)
                await asyncio.Event().wait()

            async def close(self):
                if harness.close_error:
                    raise RuntimeError("close failed")

            def get_partial_messageable(self, channel_id, *, guild_id=None):
                return harness.channel

        monkeypatch.setattr(runtime, "_Client", FakeBot)

    def run(self, **kwargs) -> None:
        try:
            asyncio.run(runtime._main(
                SimpleNamespace(config=CONFIG), "token", self.client, self.fd,
                connector_factory=None, stop=self.stop, poll_interval=0.01, now=lambda: 0.0,
                progress=self.progress, sleep=self.sleeper,
                monitor_interval=kwargs.pop("monitor_interval", 10.0)))
        finally:
            os.close(self.fd)


async def _wait_until(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0.001)


def test_main_login_that_never_succeeds_ends_gateway_unhealthy_after_ten_minutes(tmp_path, monkeypatch):
    async def login(bot):
        raise http(503)
    harness = Harness(tmp_path, monkeypatch, login=login)
    with pytest.raises(BotFailure) as failure:
        harness.run()
    assert failure.value.reason == "gateway_unhealthy"
    assert harness.clock.t >= 1000.0 + WINDOW


def test_main_a_login_rejection_stops_at_once_without_waiting_for_the_window(tmp_path, monkeypatch):
    async def login(bot):
        raise discord.LoginFailure("no")
    harness = Harness(tmp_path, monkeypatch, login=login)
    with pytest.raises(BotFailure) as failure:
        harness.run()
    assert failure.value.reason == "discord_login_failed"
    assert harness.clock.t < 1000.0 + WINDOW


def test_main_no_ready_survives_599_seconds_and_dies_at_600(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.sleeper.limit = 1000.0 + 599

    def tick(now: float) -> None:
        if now >= 1000.0 + 599:
            harness.stop.set()
    harness.sleeper.on_tick = tick
    harness.run()
    assert harness.clock.t == 1000.0 + 599
    # 上限を外すと 600 で一時の失敗
    other = Harness(tmp_path, monkeypatch)
    with pytest.raises(BotFailure) as failure:
        other.run()
    assert failure.value.reason == "gateway_unhealthy" and other.clock.t == 1000.0 + 600


def _disconnect_script(harness, *, resume_after: float | None, cap: bool = False):
    async def connect(bot):
        await bot.on_connect()
        await asyncio.sleep(0.01)
        await bot.on_disconnect()
        # 連打しても起点は動かない
        for _ in range(5):
            await bot.on_disconnect()
        since = harness.progress.gateway_unhealthy_since
        if resume_after is None:
            if cap:
                harness.sleeper.limit = since + 599
            return
        await _wait_until(lambda: harness.clock.t >= since + resume_after)
        await bot.on_resumed()
    return connect


def test_main_a_disconnect_that_never_recovers_dies_ten_minutes_after_the_first_disconnect(
        tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.connect = _disconnect_script(harness, resume_after=None, cap=True)
    harness.sleeper.on_tick = lambda now: (
        harness.stop.set() if harness.sleeper.limit is not None and now >= harness.sleeper.limit
        else None)
    harness.run()
    since = harness.progress.gateway_unhealthy_since
    assert harness.clock.t == since + 599
    assert harness.progress.gateway_state == "unhealthy"

    second = Harness(tmp_path, monkeypatch)
    second.connect = _disconnect_script(second, resume_after=None)
    with pytest.raises(BotFailure) as failure:
        second.run()
    assert failure.value.reason == "gateway_unhealthy"
    assert second.clock.t == second.progress.gateway_unhealthy_since + 600


def test_main_a_resume_clears_the_window_so_a_long_run_stays_alive(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.connect = _disconnect_script(harness, resume_after=599)

    def tick(now: float) -> None:
        if now >= 1000.0 + 2000:
            harness.stop.set()
    harness.sleeper.on_tick = tick
    harness.sleeper.limit = 1000.0 + 2000
    harness.run()
    assert harness.progress.gateway_state == "healthy"
    assert harness.clock.t == 1000.0 + 2000


def test_main_a_blocked_gate_executor_stalls_the_poll_even_with_a_healthy_gateway(tmp_path, monkeypatch):
    release = threading.Event()
    harness = Harness(tmp_path, monkeypatch, client=DaemonClient(events_gate=release))

    async def connect(bot):
        await bot.on_connect()
    harness.connect = connect
    timers = []

    def tick(now: float) -> None:
        # 判定のあとで放す (放した直後に poll が完了して判定が外れないよう、実時間で遅らせる)
        if now >= 1000.0 + WINDOW and not timers:
            timers.append(threading.Timer(0.5, release.set))
            timers[0].start()
    harness.sleeper.on_tick = tick
    with pytest.raises(BotFailure) as failure:
        harness.run()
    timers[0].join()
    assert failure.value.reason == "poller_stalled"
    assert harness.progress.gateway_state == "healthy"
    assert harness.clock.t == 1000.0 + WINDOW


def test_main_completed_polls_keep_the_bot_alive_past_ten_minutes(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)

    async def connect(bot):
        await bot.on_connect()
    harness.connect = connect
    harness.sleeper.limit = 1000.0 + 1500
    harness.sleeper.on_tick = lambda now: harness.stop.set() if now >= 1000.0 + 1500 else None
    harness.run()
    assert harness.client.event_calls > 3 and harness.clock.t == 1000.0 + 1500


def test_main_a_send_that_never_returns_ends_the_process_with_a_transient_reason(tmp_path, monkeypatch):
    event = {"id": 1, "ts": "2026-10-22T00:00:00+00:00", "code": "approval_state_changed",
             "ref": "approval:7", "fields": {"state": "approved", "decision_code": "approve"}}
    client = DaemonClient(pages=[{"events": [], "high_watermark": 0},
                                 {"events": [event], "high_watermark": 1}])
    harness = Harness(tmp_path, monkeypatch, channel=Channel(hang=True), client=client)

    async def connect(bot):
        await bot.on_connect()
    harness.connect = connect
    with pytest.raises(BotFailure) as failure:
        harness.run()
    from afx_ops.bot.exits import rc_for_reason
    assert rc_for_reason(failure.value.reason) == 79
    assert failure.value.reason in {"delivery_stalled", "poller_stalled"}
    assert harness.progress.delivery_stalled_since is not None


def test_the_real_client_routes_discord_dispatch_names_to_the_tracker():
    clock = Clock()
    progress = ProgressTracker(clock)
    bot = runtime._Client(intents=discord.Intents.none())
    bot.progress = progress

    async def go() -> None:
        await bot._async_setup_hook()
        bot.dispatch("connect")
        await asyncio.sleep(0.01)
        assert progress.gateway_state == "healthy"
        bot.dispatch("disconnect")
        await asyncio.sleep(0.01)
        assert progress.gateway_state == "unhealthy"
        bot.dispatch("resumed")
        await asyncio.sleep(0.01)
        assert progress.gateway_state == "healthy"
    asyncio.run(go())


def test_a_failing_close_does_not_replace_the_stop_reason(tmp_path, monkeypatch):
    async def login(bot):
        raise discord.LoginFailure("no")
    harness = Harness(tmp_path, monkeypatch, login=login)
    harness.close_error = True
    with pytest.raises(BotFailure) as failure:
        harness.run()
    assert failure.value.reason == "discord_login_failed"


# ------------------------------------------------------------------ 配送 (adapter)

SETTINGS = BotSettings(guild_id=GUILD, channel_id=CHANNEL, mention_role_id=None, mention_user_ids=())
APPROVED = {"id": 1, "ts": "2026-10-22T00:00:00+00:00", "code": "approval_state_changed",
            "ref": "approval:7", "fields": {"state": "approved", "decision_code": "approve"}}


class _Detail:
    def approval(self, approval_id, *, timeout=None):
        raise TimeoutError()


class _EventsClient(_Detail):
    def __init__(self, pages) -> None:
        self.pages = list(pages)

    def events(self, *, after, limit):
        page = self.pages[0] if len(self.pages) == 1 else self.pages.pop(0)
        return Reply(200, page, None)

    def approvals(self, *, limit):
        return Reply(200, {"approvals": []}, None)


class _FailingChannel:
    def __init__(self) -> None:
        self.script: list = []
        self.sent: list[str] = []

    async def send(self, content, **kwargs):
        step = self.script.pop(0) if self.script else None
        if step == "hang":
            await asyncio.Event().wait()
        if step is not None:
            raise step
        self.sent.append(content)


def _delivery(tmp_path, pages, clock):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    save(fd, initialize_event_cursor(BotState.empty(), 0))
    channel = _FailingChannel()
    client = _EventsClient(pages)
    gate = GateService(BotState.empty(), guild_id=GUILD, channel_id=CHANNEL, client=client)
    progress = ProgressTracker(clock)
    settle(progress)
    discord_client = SimpleNamespace(get_partial_messageable=lambda *a, **k: channel)
    adapter = BotAdapter(gate=gate, client=client, dir_fd=fd, settings=SETTINGS,
                         discord_client=discord_client, progress=progress)
    asyncio.run(adapter.prepare())
    return adapter, channel, progress, fd


def _page(*events):
    return {"events": list(events), "high_watermark": events[-1]["id"]}


def test_a_hung_send_starts_the_delivery_timer_before_the_send_and_it_expires_at_ten_minutes(tmp_path):
    clock = Clock()
    adapter, channel, progress, fd = _delivery(tmp_path, [_page(APPROVED)], clock)
    channel.script = ["hang"]
    try:
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(asyncio.wait_for(adapter.deliver_events(), 0.05))
        started = progress.delivery_stalled_since
        assert started == clock.t
        clock.t += 599
        progress.poll_begin()
        progress.poll_end()
        assert progress.expired() is None
        clock.t += 1
        assert progress.expired() == "delivery_stalled"
        assert load(fd).cursor == 0
    finally:
        adapter.close()
        os.close(fd)


def test_retryable_send_failures_keep_the_first_attempt_as_the_origin_until_the_event_commits(tmp_path):
    clock = Clock()
    adapter, channel, progress, fd = _delivery(tmp_path, [_page(APPROVED)], clock)
    channel.script = [OSError("down"), http(503), http(429), asyncio.TimeoutError(), OSError()]
    try:
        asyncio.run(adapter.deliver_events())
        origin = progress.delivery_stalled_since
        assert origin == clock.t
        for _ in range(4):
            clock.t += 100
            asyncio.run(adapter.deliver_events())
            # 別の REST (承認一覧) の成功も、gateway の READY も消さない
            asyncio.run(adapter.sync_pending())
            progress.gateway_connected()
            assert progress.delivery_stalled_since == origin
            assert load(fd).cursor == 0 and channel.sent == []
        clock.t = origin + 599
        assert progress.expired() is None
        clock.t = origin + 600
        progress.poll_begin()
        progress.poll_end()
        assert progress.expired() == "delivery_stalled"
        # 次の周回で成功し、cursor が保存されると消える
        asyncio.run(adapter.deliver_events())
        assert len(channel.sent) == 1 and load(fd).cursor == 1
        assert progress.delivery_stalled_since is None and progress.expired() is None
    finally:
        adapter.close()
        os.close(fd)


def test_a_send_success_without_the_cursor_save_does_not_clear_the_timer(tmp_path):
    clock = Clock()
    adapter, channel, progress, fd = _delivery(tmp_path, [_page(APPROVED)], clock)

    def broken_commit(event_id, high):
        raise BotFailure("bot_state_unavailable")
    adapter._commit_post = broken_commit
    try:
        with pytest.raises(BotFailure):
            asyncio.run(adapter.deliver_events())
        assert len(channel.sent) == 1
        assert progress.delivery_stalled_since is not None
        clock.t += 600
        progress.poll_begin()
        progress.poll_end()
        assert progress.expired() == "delivery_stalled"
    finally:
        adapter.close()
        os.close(fd)


def test_a_committed_send_clears_the_timer_and_the_next_event_gets_its_own_window(tmp_path):
    clock = Clock()
    second = {**APPROVED, "id": 2}
    adapter, channel, progress, fd = _delivery(tmp_path, [_page(APPROVED, second)], clock)
    channel.script = [None, OSError("down")]
    try:
        clock.t += 7
        asyncio.run(adapter.deliver_events())
        assert load(fd).cursor == 1
        # 一件目は消え、二件目の失敗の時点で新しい起点になる
        assert progress.delivery_stalled_since == clock.t
        assert progress.delivery_event_id == 2
    finally:
        adapter.close()
        os.close(fd)

