"""bot の event-loop / 終了処理の watchdog。

時計は fake、thread の試験は短い窓の注入。`os._exit` は記録する関数に差し替える。どの待ちにも
実時間の期限を付け、無限には待たない。
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import subprocess
import sys
import threading
import time

import pytest

from afx_ops.bot import runtime
from afx_ops.bot.adapter import BotFailure
from afx_ops.bot.exits import RC_BOT_LOOP_STALLED, RC_BOT_TRANSIENT
from afx_ops.bot.watchdog import (CHECK_INTERVAL, SHUTDOWN_LIMIT, SHUTDOWN_LINE, STALL_LINE,
                                  STALL_WINDOW, Watchdog, emit_line)
from test_bot_progress import DaemonClient, Harness

DEADLINE = 10.0


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


class Pipe:
    """fd 2 の代わりの pipe。write 側 (非 blocking でない) を渡す。"""

    def __init__(self) -> None:
        self.r, self.w = os.pipe()

    def drain(self) -> bytes:
        os.set_blocking(self.r, False)
        out = b""
        try:
            while True:
                chunk = os.read(self.r, 65536)
                if not chunk:
                    break
                out += chunk
        except BlockingIOError:
            pass
        return out

    def fill(self) -> None:
        os.set_blocking(self.w, False)
        try:
            while True:
                os.write(self.w, b"x" * 4096)
        except BlockingIOError:
            pass
        os.set_blocking(self.w, True)

    def close(self) -> None:
        for fd in (self.r, self.w):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.fixture
def pipe():
    made = Pipe()
    yield made
    made.close()


def _dog(clock, exits, pipe, **kwargs) -> Watchdog:
    return Watchdog(clock=clock, exit_fn=exits.append, fd=pipe.w, **kwargs)


def test_the_constants_are_ten_minutes_and_sixty_seconds():
    assert (STALL_WINDOW, SHUTDOWN_LIMIT) == (600.0, 60.0)
    assert CHECK_INTERVAL < STALL_WINDOW
    assert b"\n" == STALL_LINE[-1:] == SHUTDOWN_LINE[-1:]
    assert len(STALL_LINE) < select_pipe_buf() and len(SHUTDOWN_LINE) < select_pipe_buf()
    assert STALL_LINE.isascii() and SHUTDOWN_LINE.isascii()


def select_pipe_buf() -> int:
    import select
    return select.PIPE_BUF


# ------------------------------------------------------------------ loop stall

def test_an_unacknowledged_loop_stalls_exactly_at_the_window(pipe):
    clock, exits = Clock(), []
    loop = asyncio.new_event_loop()  # 回さない = callback は実行されない
    try:
        dog = _dog(clock, exits, pipe)
        dog.attach(loop)
        clock.t += 599
        assert dog.tick() is False and exits == []
        clock.t += 1
        assert dog.tick() is True
        assert exits == [RC_BOT_LOOP_STALLED]
        assert pipe.drain() == STALL_LINE
    finally:
        loop.close()


def test_a_responsive_loop_never_stalls_however_long_it_runs(pipe):
    clock, exits = Clock(), []
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        dog = _dog(clock, exits, pipe)
        dog.attach(loop)
        for _ in range(40):
            clock.t += 100  # 40 周で 4000 秒 (窓の 6 倍超)
            assert dog.tick() is False
            done = threading.Event()
            loop.call_soon_threadsafe(done.set)
            assert done.wait(DEADLINE)  # 先に積んだ acknowledgement も実行済み
        assert exits == []
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(DEADLINE)
        loop.close()


def test_a_loop_that_resumes_before_the_window_is_not_stalled(pipe):
    clock, exits = Clock(), []
    loop = asyncio.new_event_loop()
    try:
        dog = _dog(clock, exits, pipe)
        dog.attach(loop)
        clock.t += 400
        assert dog.tick() is False
        loop.call_soon(loop.stop)
        loop.run_forever()  # 積まれた acknowledgement が走る
        clock.t += 400  # 起点から 800 秒だが、直近の応答からは 400 秒
        assert dog.tick() is False and exits == []
    finally:
        loop.close()


def test_a_closed_loop_does_not_break_the_check(pipe):
    clock, exits = Clock(), []
    loop = asyncio.new_event_loop()
    dog = _dog(clock, exits, pipe)
    dog.attach(loop)
    loop.close()
    assert dog.tick() is False and exits == []


# ------------------------------------------------------------------ stderr の規則

def test_the_line_is_not_written_to_a_full_pipe_but_the_exit_still_happens(pipe):
    pipe.fill()
    before = len(pipe.drain())
    pipe.fill()
    clock, exits = Clock(), []
    loop = asyncio.new_event_loop()
    try:
        dog = _dog(clock, exits, pipe)
        dog.attach(loop)
        clock.t += 600
        finished = threading.Event()

        def go():
            dog.tick()
            finished.set()
        threading.Thread(target=go, daemon=True).start()
        assert finished.wait(DEADLINE), "a blocked write held the watchdog"
        assert exits == [RC_BOT_LOOP_STALLED]
        assert before > 0
    finally:
        loop.close()


def test_a_closed_stderr_still_reaches_the_exit(pipe):
    clock, exits = Clock(), []
    loop = asyncio.new_event_loop()
    try:
        dog = _dog(clock, exits, pipe)
        dog.attach(loop)
        os.close(pipe.w)
        clock.t += 600
        assert dog.tick() is True and exits == [RC_BOT_LOOP_STALLED]
    finally:
        loop.close()
        pipe.w = os.dup(0)  # fixture の後始末が閉じる fd


def test_a_failing_fcntl_drops_the_line_and_still_exits(pipe, monkeypatch):
    def boom(*_args):
        raise OSError(1, "no")
    monkeypatch.setattr(fcntl, "fcntl", boom)
    clock, exits = Clock(), []
    loop = asyncio.new_event_loop()
    try:
        dog = _dog(clock, exits, pipe)
        dog.attach(loop)
        clock.t += 600
        assert dog.tick() is True and exits == [RC_BOT_LOOP_STALLED]
        assert pipe.drain() == b""
    finally:
        loop.close()


def test_existing_fd_flags_are_kept_and_nonblocking_is_added(pipe):
    fcntl.fcntl(pipe.w, fcntl.F_SETFL, fcntl.fcntl(pipe.w, fcntl.F_GETFL) | os.O_APPEND)
    before = fcntl.fcntl(pipe.w, fcntl.F_GETFL)
    assert not before & os.O_NONBLOCK
    emit_line(STALL_LINE, fd=pipe.w)
    after = fcntl.fcntl(pipe.w, fcntl.F_GETFL)
    assert after & os.O_NONBLOCK
    assert after & ~os.O_NONBLOCK == before
    assert pipe.drain() == STALL_LINE


def test_a_write_that_races_past_poll_does_not_block(pipe):
    pipe.fill()
    finished = threading.Event()

    def go():
        emit_line(STALL_LINE, fd=pipe.w, poll=lambda _fd: True)  # poll が書けると誤答した
        finished.set()
    threading.Thread(target=go, daemon=True).start()
    assert finished.wait(DEADLINE), "write blocked: O_NONBLOCK was not applied"


def test_poll_not_ready_skips_the_write(pipe):
    emit_line(STALL_LINE, fd=pipe.w, poll=lambda _fd: False)
    assert pipe.drain() == b""


# ------------------------------------------------------------------ shutdown 上限

def test_shutdown_limit_is_measured_from_the_first_begin(pipe):
    clock, exits = Clock(), []
    dog = _dog(clock, exits, pipe)
    dog.begin_shutdown()
    clock.t += 30
    dog.begin_shutdown()  # 2 回目は起点を動かさない
    clock.t += 29
    assert dog.tick() is False and exits == []
    clock.t += 1
    assert dog.tick() is True
    assert exits == [RC_BOT_TRANSIENT]
    assert pipe.drain() == SHUTDOWN_LINE


def test_the_watchdog_keeps_watching_after_shutdown_starts_and_ignores_the_stale_acknowledgement(pipe):
    clock, exits = Clock(), []
    loop = asyncio.new_event_loop()
    try:
        dog = _dog(clock, exits, pipe)
        dog.attach(loop)
        clock.t += 590
        dog.begin_shutdown()
        clock.t += 30  # 応答は 620 秒途絶えたが、終了中は loop の stall では判定しない
        assert dog.tick() is False and exits == []
        clock.t += 30
        assert dog.tick() is True and exits == [RC_BOT_TRANSIENT]
    finally:
        loop.close()


# ------------------------------------------------------------------ 実 thread

def _real(exits_event, rcs, **kwargs) -> Watchdog:
    def record(rc):
        rcs.append(rc)
        exits_event.set()
    return Watchdog(interval=0.01, exit_fn=record, **kwargs)


def test_a_blocked_loop_is_killed_by_the_real_thread(pipe):
    fired, rcs = threading.Event(), []
    dog = _real(fired, rcs, window=0.3, shutdown_limit=60, fd=pipe.w)
    loop = asyncio.new_event_loop()
    release = threading.Event()

    async def blocker():
        dog.attach(loop)
        dog.start()
        release.wait(DEADLINE)  # loop thread を止める

    def killed():
        fired.wait(DEADLINE)
        release.set()
    watcher = threading.Thread(target=killed, daemon=True)
    watcher.start()
    try:
        loop.run_until_complete(blocker())
        assert rcs == [RC_BOT_LOOP_STALLED]
        assert pipe.drain() == STALL_LINE
    finally:
        dog.stop()
        loop.close()


def test_a_live_loop_is_left_alone_by_the_real_thread(pipe):
    fired, rcs = threading.Event(), []
    dog = _real(fired, rcs, window=0.3, shutdown_limit=60, fd=pipe.w)
    loop = asyncio.new_event_loop()

    async def alive():
        dog.attach(loop)
        dog.start()
        await asyncio.sleep(1.0)  # 窓の 3 倍以上
    try:
        loop.run_until_complete(alive())
        assert rcs == []
    finally:
        dog.stop()
        loop.close()


def test_the_real_thread_ends_the_process_when_shutdown_hangs_after_the_loop_closed(pipe):
    fired, rcs = threading.Event(), []
    dog = _real(fired, rcs, window=60, shutdown_limit=0.3, fd=pipe.w)
    loop = asyncio.new_event_loop()
    dog.attach(loop)
    dog.start()
    loop.close()  # asyncio.run の完了後
    dog.begin_shutdown()
    try:
        assert fired.wait(DEADLINE)
        assert rcs == [RC_BOT_TRANSIENT]
    finally:
        dog.stop()


def test_stop_ends_the_thread():
    dog = Watchdog(interval=0.01, exit_fn=lambda rc: None)
    dog.start()
    thread = dog._thread
    assert thread is not None and thread.daemon and thread.is_alive()
    dog.stop()
    assert not thread.is_alive()


def test_start_launches_only_one_thread():
    dog = Watchdog(interval=0.01, exit_fn=lambda rc: None)
    before = threading.active_count()
    try:
        dog.start()
        dog.start()
        assert threading.active_count() == before + 1
    finally:
        dog.stop()


def test_a_normal_end_leaves_no_thread_that_holds_the_process():
    code = (
        "import asyncio, sys\n"
        "from afx_ops.bot.watchdog import Watchdog\n"
        "dog = Watchdog(interval=0.01, shutdown_limit=60)\n"
        "dog.start()\n"
        "async def main():\n"
        "    dog.attach(asyncio.get_running_loop())\n"
        "    await asyncio.sleep(0.05)\n"
        "asyncio.run(main())\n"
        "dog.begin_shutdown()\n"
    )
    started = time.monotonic()
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=DEADLINE)
    assert done.returncode == 0 and done.stderr == b""
    assert time.monotonic() - started < DEADLINE - 1


# ------------------------------------------------------------------ runtime への配線

class _Out:
    def __init__(self, gate: threading.Event | None = None) -> None:
        self.text = ""
        self.gate = gate

    def write(self, data) -> int:
        if self.gate is not None:
            self.gate.wait(DEADLINE)
        self.text += data
        return len(data)

    def flush(self) -> None:
        pass


class _Spy(Watchdog):
    def __init__(self) -> None:
        super().__init__(exit_fn=lambda rc: None)
        self.events: list[str] = []

    def start(self) -> None:
        self.events.append("start")

    def attach(self, loop) -> None:
        self.events.append("attach")
        super().attach(loop)

    def begin_shutdown(self) -> None:
        self.events.append("shutdown")
        super().begin_shutdown()


def _isolated(tmp_path):
    from types import SimpleNamespace
    state = tmp_path / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    token = "A" * 24 + "." + "B" * 6 + "." + "C" * 27
    return SimpleNamespace(state_dir=state, secrets={"discord.token": token.encode()},
                           config=SimpleNamespace())


@pytest.mark.parametrize("exc, rc", [(None, 0), (BotFailure("discord_rejected"), 78),
                                     (ValueError("x"), 79)])
def test_run_starts_the_watchdog_and_marks_shutdown_on_every_exit(tmp_path, monkeypatch, exc, rc):
    async def main(*args, **kwargs):
        kwargs["watchdog"].events.append("main")
        if exc is not None:
            raise exc
    monkeypatch.setattr(runtime, "_main", main)
    spy = _Spy()
    assert runtime.run(_isolated(tmp_path), client=object(), stderr=_Out(), watchdog=spy) == rc
    assert spy.events[0] == "start" and "main" in spy.events and spy.events[-1] == "shutdown"


def test_run_marks_shutdown_before_a_pre_loop_failure_prints(tmp_path):
    spy = _Spy()
    iso = _isolated(tmp_path)
    iso.secrets["discord.token"] = b"bad"

    class Out(_Out):
        def write(self, data):
            spy.events.append("print")
            return super().write(data)
    assert runtime.run(iso, client=object(), stderr=Out(), watchdog=spy) == 78
    assert spy.events[-2:] == ["shutdown", "print"] or spy.events[-3:-1] == ["shutdown", "print"]


def test_run_uses_a_real_watchdog_by_default_when_not_replaced(tmp_path, monkeypatch):
    made = []
    monkeypatch.setattr(runtime, "new_watchdog", lambda: made.append(_Spy()) or made[-1])

    async def main(*args, **kwargs):
        return None
    monkeypatch.setattr(runtime, "_main", main)
    runtime.run(_isolated(tmp_path), client=object(), stderr=_Out())
    assert len(made) == 1 and made[0].events[0] == "start"


@pytest.mark.real_watchdog
def test_the_production_factory_builds_an_unstarted_watchdog_with_the_spec_limits():
    dog = runtime.new_watchdog()
    try:
        assert type(dog) is Watchdog
        assert (dog._interval, dog._window, dog._shutdown_limit) == (
            CHECK_INTERVAL, STALL_WINDOW, SHUTDOWN_LIMIT)
        assert (CHECK_INTERVAL, STALL_WINDOW, SHUTDOWN_LIMIT) == (5.0, 600.0, 60.0)
    finally:
        dog.stop()


def test_other_tests_get_the_unstarted_stand_in():
    assert type(runtime.new_watchdog()) is not Watchdog


def test_a_blocked_stderr_write_after_asyncio_run_is_still_bounded(tmp_path, monkeypatch):
    async def main(*args, **kwargs):
        raise BotFailure("discord_rejected")
    monkeypatch.setattr(runtime, "_main", main)
    release = threading.Event()
    fired, rcs = threading.Event(), []

    def record(rc):
        rcs.append(rc)
        release.set()
        fired.set()
    dog = Watchdog(interval=0.01, shutdown_limit=0.3, exit_fn=record, fd=os.dup(2))
    out = _Out(gate=release)
    try:
        assert runtime.run(_isolated(tmp_path), client=object(), stderr=out, watchdog=dog) == 78
        assert fired.is_set() and rcs == [RC_BOT_TRANSIENT]
    finally:
        dog.stop()


def _shutdown_dog(harness: Harness, rcs: list[int], release) -> Watchdog:
    def record(rc):
        rcs.append(rc)
        release()
    return Watchdog(interval=0.01, window=600, shutdown_limit=0.3, exit_fn=record, fd=os.dup(2))


def test_a_worker_that_never_returns_cannot_hold_the_shutdown_forever(tmp_path, monkeypatch):
    gate = threading.Event()
    client = DaemonClient(events_gate=gate)
    harness = Harness(tmp_path, monkeypatch, client=client)
    harness.sleeper.limit = 1000.0 + 100  # fake の時計は進捗の窓の手前で止める
    rcs: list[int] = []
    dog = _shutdown_dog(harness, rcs, gate.set)
    dog.start()
    threading.Timer(0.3, harness.stop.set).start()
    # watchdog が退行しても無限に待たないよう、独立した期限で gate を解放して明示的に失敗させる
    backstop = threading.Timer(DEADLINE / 2, gate.set)
    backstop.start()
    try:
        harness.run(watchdog=dog)  # asyncio.run の default executor の join で詰まる
        assert rcs == [RC_BOT_TRANSIENT]
    finally:
        backstop.cancel()
        gate.set()
        dog.stop()


def test_a_bot_close_that_never_returns_cannot_hold_the_shutdown_forever(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    release = threading.Event()
    harness.close_gate = release
    harness.sleeper.limit = 1000.0 + 100  # fake の時計は進捗の窓の手前で止める
    rcs: list[int] = []
    dog = _shutdown_dog(harness, rcs, release.set)
    dog.start()
    threading.Timer(0.3, harness.stop.set).start()
    backstop = threading.Timer(DEADLINE / 2, release.set)
    backstop.start()
    try:
        harness.run(watchdog=dog)
        assert rcs == [RC_BOT_TRANSIENT]
    finally:
        backstop.cancel()
        release.set()
        dog.stop()


def test_a_prompt_shutdown_does_not_fire(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.sleeper.limit = 1000.0 + 100
    rcs: list[int] = []
    dog = Watchdog(interval=0.01, shutdown_limit=5, exit_fn=rcs.append, fd=os.dup(2))
    dog.start()
    threading.Timer(0.2, harness.stop.set).start()
    try:
        harness.run(watchdog=dog)
        time.sleep(0.1)
        assert rcs == []
    finally:
        dog.stop()


# ------------------------------------------------------------------ 中断・期限・結合

@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit, asyncio.CancelledError])
def test_run_marks_shutdown_when_the_main_is_interrupted_and_reraises(tmp_path, monkeypatch, exc):
    spy = _Spy()

    async def main(*args, **kwargs):
        raise exc()
    monkeypatch.setattr(runtime, "_main", main)
    with pytest.raises(BaseException) as caught:
        runtime.run(_isolated(tmp_path), client=object(), stderr=_Out(), watchdog=spy)
    assert caught.type in (exc, KeyboardInterrupt, SystemExit, asyncio.CancelledError)
    assert "shutdown" in spy.events


def test_run_marks_shutdown_when_interrupted_before_the_loop_starts(tmp_path, monkeypatch):
    spy = _Spy()

    def boom(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(runtime, "open_state_dir", boom)
    with pytest.raises(KeyboardInterrupt):
        runtime.run(_isolated(tmp_path), client=object(), stderr=_Out(), watchdog=spy)
    assert "shutdown" in spy.events


def test_main_marks_shutdown_when_interrupted_during_initialisation(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    spy = _Spy()

    def boom(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(runtime, "BotAdapter", boom)
    with pytest.raises(KeyboardInterrupt):
        harness.run(watchdog=spy)
    assert "shutdown" in spy.events


def test_next_wait_never_exceeds_the_remaining_shutdown_time():
    clock = Clock()
    dog = Watchdog(clock=clock, interval=5.0, shutdown_limit=60.0, exit_fn=lambda rc: None)
    assert dog.next_wait() == 5.0
    dog.begin_shutdown()
    clock.t += 57.0
    assert dog.next_wait() == pytest.approx(3.0)
    clock.t += 2.5
    assert dog.next_wait() == pytest.approx(0.5)
    clock.t += 10
    assert dog.next_wait() == 0.0


def test_shutdown_fires_at_the_limit_not_at_the_next_periodic_check():
    rcs: list[int] = []
    fired = threading.Event()

    def record(rc):
        rcs.append(rc)
        fired.set()
    dog = Watchdog(interval=30.0, shutdown_limit=0.3, exit_fn=record, fd=os.dup(2))
    dog.start()
    try:
        time.sleep(0.05)  # 周期 30 秒の待ちに入った後で終了を始める
        started = time.monotonic()
        dog.begin_shutdown()
        assert fired.wait(DEADLINE)
        assert time.monotonic() - started < 0.3 + 1.0
        assert rcs == [RC_BOT_TRANSIENT]
    finally:
        dog.stop()


@pytest.mark.real_watchdog
def test_the_default_run_starts_a_real_watchdog_thread_and_the_process_ends(tmp_path):
    code = (
        "import sys, threading\n"
        "from types import SimpleNamespace\n"
        "from afx_ops.bot import runtime\n"
        "async def main(*a, **k):\n"
        "    names = [t.name for t in threading.enumerate()]\n"
        "    assert 'afx-ops-watchdog' in names, names\n"
        "runtime._main = main\n"
        "tok = 'A' * 24 + '.' + 'B' * 6 + '.' + 'C' * 27\n"
        "iso = SimpleNamespace(state_dir=sys.argv[1], secrets={'discord.token': tok.encode()},\n"
        "                      config=SimpleNamespace())\n"
        "rc = runtime.run(iso, client=object())\n"
        "assert rc == 0, rc\n"
        "assert any(t.name == 'afx-ops-watchdog' and t.daemon for t in threading.enumerate())\n"
    )
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    started = time.monotonic()
    done = subprocess.run([sys.executable, "-c", code, str(state)], capture_output=True,
                          timeout=DEADLINE)
    assert done.returncode == 0, done.stderr
    assert time.monotonic() - started < DEADLINE - 1
