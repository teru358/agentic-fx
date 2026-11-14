"""bot の log と asyncio の例外を固定語に閉じる。

record・例外文に置いた秘密 (token・Authorization・URL・ID・本文・改行・traceback) が出力の byte に
1 つも現れないこと、gateway の遷移が 1 回だけ固定語で出て反復は件数に畳まれること、task の例外が
未回収で残らないことを確かめる。
"""
from __future__ import annotations

import asyncio
import gc
import logging
import re
import threading
from types import SimpleNamespace

import pytest

from afx_ops.bot import logsafe, runtime
from afx_ops.bot.adapter import BotFailure
from afx_ops.bot.logsafe import LoopErrors, SafeLogging
from afx_ops.bot.progress import ProgressTracker
from test_bot_progress import Harness

TOKEN = ".".join(("MTIzNDU2Nzg5MDEyMzQ1Njc4", "GabCdE", "abcdefghijklmnopqrstuvwxyz0123456789"))
CANARIES = (TOKEN, "Authorization", "Bot ", "https://discord.com/api/v10/channels",
            "123456789012345678", "response-body-secret", "Traceback", "secret-line-2")
FIXED = re.compile(r"(log_record family=(asyncio|aiohttp|discord|other)"
                   r"|log_repeated family=(asyncio|aiohttp|discord|other) count=\d+)\Z")


def _poison(name: str, level: int = logging.WARNING) -> None:
    try:
        raise RuntimeError(f"{TOKEN} 123456789012345678")
    except RuntimeError:
        logging.getLogger(name).log(
            level, "Authorization: Bot %s at https://discord.com/api/v10/channels/%s "
            "body=response-body-secret\nsecret-line-2", TOKEN, 123456789012345678,
            exc_info=True)


@pytest.fixture
def safe():
    lines: list[str] = []
    installed = SafeLogging(lines.append)
    yield installed, lines
    installed.restore()


def _assert_clean(text: str) -> None:
    for canary in CANARIES:
        assert canary not in text, canary


@pytest.mark.parametrize("name", ["discord", "discord.gateway", "discord.http", "", "root.x",
                                  "asyncio", "aiohttp.server", "aiohttp.client", "other.lib"])
def test_a_poisoned_record_never_reaches_the_output(safe, capfd, name):
    installed, lines = safe
    _poison(name)
    _poison(name, logging.CRITICAL)
    installed.flush_counts()
    seen = capfd.readouterr()
    _assert_clean("".join(lines) + seen.out + seen.err)
    assert seen.out == "" and seen.err == ""
    assert all(FIXED.fullmatch(line) for line in lines), lines


def test_the_discord_logger_stays_closed(safe):
    _installed, lines = safe
    for _ in range(500):
        _poison("discord.gateway")
    assert lines == []


def test_heartbeat_warnings_do_not_grow_the_output(safe):
    installed, lines = safe
    heartbeat = logging.getLogger("discord.gateway")
    for _ in range(1000):
        heartbeat.warning("Shard ID %s heartbeat blocked for more than %s seconds.", None, 10 * 7)
    installed.flush_counts()
    assert lines == []


def test_repeated_records_collapse_to_one_line_and_a_count(safe):
    installed, lines = safe
    for _ in range(300):
        _poison("asyncio")
    assert lines == ["log_record family=asyncio"]
    installed.flush_counts()
    assert lines[1:] == ["log_repeated family=asyncio count=299"]
    installed.flush_counts()
    assert len(lines) == 2


def test_last_resort_and_raise_exceptions_are_off_and_restored():
    before = (logging.lastResort, logging.raiseExceptions, list(logging.getLogger().handlers))
    installed = SafeLogging(lambda text: None)
    try:
        assert logging.lastResort is None
        assert logging.raiseExceptions is False
    finally:
        installed.restore()
    assert (logging.lastResort, logging.raiseExceptions, list(logging.getLogger().handlers)) == before


def test_a_logger_without_a_handler_does_not_fall_back_to_the_raw_stderr(safe, capfd):
    lonely = logging.getLogger("lonely.logger")
    lonely.propagate = False
    try:
        _poison("lonely.logger", logging.CRITICAL)
    finally:
        lonely.propagate = True
    seen = capfd.readouterr()
    assert seen.err == "" and seen.out == ""


def test_a_failing_safe_handler_does_not_print_the_raw_record(capfd):
    def broken(text):
        raise OSError(TOKEN)
    installed = SafeLogging(broken)
    try:
        _poison("asyncio", logging.CRITICAL)
        installed.flush_counts()
    finally:
        installed.restore()
    seen = capfd.readouterr()
    assert seen.err == "" and seen.out == ""


def test_the_previous_root_handler_is_replaced_not_kept(capfd):
    root = logging.getLogger()
    plain = logging.StreamHandler()
    root.addHandler(plain)
    installed = SafeLogging(lambda text: None)
    try:
        assert plain not in root.handlers
        _poison("asyncio")
    finally:
        installed.restore()
        root.removeHandler(plain)
    seen = capfd.readouterr()
    _assert_clean(seen.out + seen.err)
    assert seen.err == ""


# ------------------------------------------------------------------ loop の例外

def test_the_loop_handler_prints_only_a_type_name_and_a_fixed_phrase():
    lines: list[str] = []
    handler = LoopErrors(lines.append)
    handler(None, {"exception": ValueError(TOKEN), "message": f"Exception in callback {TOKEN}"})
    handler(None, {"message": "Task exception was never retrieved\n" + TOKEN,
                   "exception": type("Bad Name " + TOKEN, (Exception,), {})(TOKEN)})
    handler(None, {"message": TOKEN})
    assert lines == ["bot_loop_error ValueError message=exception_in_callback",
                     "bot_loop_error other message=task_exception_was_never_retrieved",
                     "bot_loop_error none message=other"]
    _assert_clean("".join(lines))


def test_loop_errors_beyond_the_limit_collapse_to_a_count():
    lines: list[str] = []
    handler = LoopErrors(lines.append)
    for _ in range(logsafe.LOOP_ERROR_LINES + 30):
        handler(None, {"exception": KeyError("x")})
    assert len(lines) == logsafe.LOOP_ERROR_LINES
    handler.flush_counts()
    assert lines[-1] == "bot_loop_error_repeated count=30"


def test_a_failing_loop_output_does_not_raise():
    def broken(text):
        raise OSError(TOKEN)
    LoopErrors(broken)(None, {"exception": KeyError("x")})


def _drop_failing_task(handler) -> None:
    async def go():
        asyncio.get_running_loop().set_exception_handler(handler)

        async def boom():
            raise RuntimeError(TOKEN)
        asyncio.ensure_future(boom())  # 参照を持たない
        await asyncio.sleep(0.01)
        gc.collect()
    asyncio.run(go())


def test_an_unretrieved_task_exception_goes_to_the_loop_handler():
    lines: list[str] = []
    _drop_failing_task(LoopErrors(lines.append))
    assert lines == ["bot_loop_error RuntimeError message=task_exception_was_never_retrieved"]


def test_spawn_retrieves_the_exception_of_a_task_nobody_awaits():
    calls: list[dict] = []

    async def go():
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: calls.append(ctx))

        async def boom():
            raise RuntimeError(TOKEN)
        runtime._spawn(boom())
        await asyncio.sleep(0.01)
        gc.collect()
    asyncio.run(go())
    assert calls == []


def test_main_installs_the_loop_handler_and_leaves_no_unretrieved_task(tmp_path, monkeypatch):
    lines: list[str] = []

    async def connect(bot):
        async def boom():
            raise RuntimeError(TOKEN)
        asyncio.ensure_future(boom())
        await asyncio.sleep(0.01)
        gc.collect()
        raise ValueError(TOKEN)
    harness = Harness(tmp_path, monkeypatch, connect=connect)
    harness.sleeper.limit = 1000.0 + 100
    with pytest.raises(BotFailure) as failure:
        harness.run(loop_errors=LoopErrors(lines.append))
    assert failure.value.reason == "gateway_failed"
    # 意図して落とした task の 1 件だけ。monitor・gateway・poller・待機の task の例外は回収済み
    assert lines == ["bot_loop_error RuntimeError message=task_exception_was_never_retrieved"]


# ------------------------------------------------------------------ run の配線

class _Out:
    def __init__(self) -> None:
        self.text = ""

    def write(self, data) -> int:
        self.text += data
        return len(data)

    def flush(self) -> None:
        pass


def _isolated(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    token = "A" * 24 + "." + "B" * 6 + "." + "C" * 27
    return SimpleNamespace(state_dir=state, secrets={"discord.token": token.encode()},
                           config=SimpleNamespace())


def test_run_closes_the_loggers_for_the_whole_run_and_restores_them(tmp_path, monkeypatch, capfd):
    before = (logging.lastResort, logging.raiseExceptions, list(logging.getLogger().handlers))
    out = _Out()

    async def main(*args, **kwargs):
        for name in ("discord.gateway", "asyncio", "aiohttp.client", ""):
            _poison(name, logging.CRITICAL)
        assert logging.lastResort is None
        asyncio.get_running_loop()
    monkeypatch.setattr(runtime, "_main", main)
    assert runtime.run(_isolated(tmp_path), client=object(), stderr=out) == 0
    seen = capfd.readouterr()
    _assert_clean(out.text + seen.out + seen.err)
    assert seen.err == ""
    assert "log_record family=asyncio" in out.text
    assert (logging.lastResort, logging.raiseExceptions, list(logging.getLogger().handlers)) == before


def test_run_passes_the_loop_handler_and_the_gateway_emit_to_main(tmp_path, monkeypatch):
    seen = {}

    async def main(*args, **kwargs):
        seen.update(kwargs)
    monkeypatch.setattr(runtime, "_main", main)
    runtime.run(_isolated(tmp_path), client=object(), stderr=_Out())
    assert isinstance(seen["loop_errors"], LoopErrors)
    assert seen["progress"]._emit is not None


# ------------------------------------------------------------------ gateway の遷移

class _Clock:
    t = 100.0

    def __call__(self) -> float:
        return self.t


def _tracker():
    lines: list[str] = []
    return ProgressTracker(_Clock(), emit=lines.append), lines


def test_a_gateway_transition_is_said_once_and_repeats_collapse_until_recovery():
    tracker, lines = _tracker()
    tracker.gateway_connected()
    assert lines == []
    tracker.gateway_disconnected()
    assert lines == ["gateway_unhealthy"]
    for _ in range(50):
        tracker.gateway_disconnected()
    assert lines == ["gateway_unhealthy"]
    tracker.gateway_resumed()
    assert lines == ["gateway_unhealthy", "gateway_recovered repeats=50"]
    tracker.gateway_connected()
    assert len(lines) == 2


def test_each_new_outage_says_unhealthy_again_with_its_own_count():
    tracker, lines = _tracker()
    tracker.gateway_connected()
    for repeats in (2, 0):
        tracker.gateway_disconnected()
        for _ in range(repeats):
            tracker.gateway_disconnected()
        tracker.gateway_connected()
    assert lines == ["gateway_unhealthy", "gateway_recovered repeats=2",
                     "gateway_unhealthy", "gateway_recovered repeats=0"]


def test_disconnects_before_the_first_ready_are_counted_not_said():
    tracker, lines = _tracker()
    for _ in range(3):
        tracker.gateway_disconnected()
    assert lines == []
    tracker.gateway_connected()
    assert lines == ["gateway_started repeats=3"]


def test_the_collapsed_count_is_bounded():
    tracker, lines = _tracker()
    tracker.gateway_connected()
    tracker.gateway_disconnected()
    for _ in range(20000):
        tracker.gateway_disconnected()
    tracker.gateway_connected()
    assert lines[-1] == "gateway_recovered repeats=9999"


def test_a_failing_emit_does_not_stop_the_tracker():
    def broken(text):
        raise OSError(TOKEN)
    tracker = ProgressTracker(_Clock(), emit=broken)
    tracker.gateway_connected()
    tracker.gateway_disconnected()
    assert tracker.gateway_state == "unhealthy"


def test_the_client_callbacks_drive_the_transition_lines():
    tracker, lines = _tracker()

    class Bot(runtime._Client):
        pass
    bot = Bot.__new__(Bot)
    bot.progress = tracker

    async def go():
        await bot.on_connect()
        await bot.on_disconnect()
        await bot.on_disconnect()
        await bot.on_resumed()
    asyncio.run(go())
    assert lines == ["gateway_unhealthy", "gateway_recovered repeats=1"]


# ------------------------------------------------------------------ 是正の確認

def test_a_type_name_shaped_like_an_identifier_but_not_a_known_type_is_not_printed():
    hostile = type("secret_token_123", (Exception,), {})
    handler = LoopErrors(lambda text: lines.append(text))
    lines: list[str] = []
    handler(None, {"exception": hostile(TOKEN), "message": "Task exception was never retrieved"})
    handler(None, {"exception": ValueError(TOKEN)})
    handler(None, {"exception": asyncio.TimeoutError()})
    assert lines[0] == "bot_loop_error other message=task_exception_was_never_retrieved"
    assert lines[1] == "bot_loop_error ValueError"
    assert "secret_token_123" not in "".join(lines)
    # 本物の module の名前を名乗る偽の型も、その module が公開する型でなければ出さない
    fake = type("secret_token_123", (Exception,), {"__module__": "builtins"})
    assert logsafe._type_name(fake()) == "other"


def test_a_type_published_later_under_a_known_module_is_not_printed():
    import aiohttp
    hostile = type("secret_token_456", (aiohttp.ClientError,), {"__module__": "aiohttp"})
    aiohttp.secret_token_456 = hostile
    try:
        assert logsafe._type_name(hostile()) == "other"
    finally:
        del aiohttp.secret_token_456
    assert logsafe._type_name(aiohttp.ClientConnectionError()) == "ClientConnectionError"


def test_existing_handlers_on_discord_and_its_children_are_removed_and_restored():
    import io
    stream = io.StringIO()
    stream_child = io.StringIO()
    parent = logging.getLogger("discord")
    child = logging.getLogger("discord.gateway")
    parent_handler, child_handler = logging.StreamHandler(stream), logging.StreamHandler(stream_child)
    parent.addHandler(parent_handler)
    child.addHandler(child_handler)
    before = (list(parent.handlers), list(child.handlers), parent.propagate, child.propagate)
    installed = SafeLogging(lambda text: None)
    try:
        _poison("discord", logging.CRITICAL)
        _poison("discord.gateway", logging.CRITICAL)
        _poison("discord.http", logging.CRITICAL)
        assert stream.getvalue() == "" and stream_child.getvalue() == ""
    finally:
        installed.restore()
        try:
            assert (list(parent.handlers), list(child.handlers), parent.propagate,
                    child.propagate) == before
        finally:
            parent.removeHandler(parent_handler)
            child.removeHandler(child_handler)


def test_a_partly_built_safe_logging_restores_what_it_changed(monkeypatch):
    root = logging.getLogger()
    before = (list(root.handlers), root.level, logging.lastResort, logging.raiseExceptions)

    def boom(name):
        raise KeyboardInterrupt
    monkeypatch.setattr(logsafe, "_family_loggers", boom)
    with pytest.raises(KeyboardInterrupt):
        SafeLogging(lambda text: None)
    assert (list(root.handlers), root.level, logging.lastResort, logging.raiseExceptions) == before


class _FakeWatchdog:
    def __init__(self) -> None:
        self.started = False
        self.shutdown = 0

    def start(self) -> None:
        self.started = True

    def attach(self, loop) -> None:
        pass

    def begin_shutdown(self, **kw) -> None:
        self.shutdown += 1


def test_an_interrupt_while_building_the_safe_logging_still_records_the_shutdown(tmp_path, monkeypatch):
    def boom(write):
        raise KeyboardInterrupt
    monkeypatch.setattr(runtime, "SafeLogging", boom)
    dog = _FakeWatchdog()
    with pytest.raises(KeyboardInterrupt):
        runtime.run(_isolated(tmp_path), client=object(), stderr=_Out(), watchdog=dog)
    assert dog.started and dog.shutdown >= 1


def test_loop_errors_beyond_the_limit_are_summarised_by_run(tmp_path, monkeypatch):
    out = _Out()

    async def main(*args, loop_errors=None, **kwargs):
        for _ in range(logsafe.LOOP_ERROR_LINES + 5):
            loop_errors(None, {"exception": RuntimeError(TOKEN)})
    monkeypatch.setattr(runtime, "_main", main)
    assert runtime.run(_isolated(tmp_path), client=object(), stderr=out) == 0
    assert "bot_loop_error_repeated count=5" in out.text
    _assert_clean(out.text)


def test_records_emitted_before_run_are_closed_by_the_early_install(tmp_path, monkeypatch, capfd):
    from afx_ops import cli
    root = logging.getLogger()
    before = (list(root.handlers), logging.lastResort)

    def start(feature):
        # 設定の読込み・import の段で出る record。run の前
        _poison("discord.client")
        _poison("asyncio", logging.CRITICAL)
        return 0
    monkeypatch.setattr(cli, "_start", start)
    assert cli.main(["bot"]) == 0
    seen = capfd.readouterr()
    _assert_clean(seen.out + seen.err)
    assert "log_record family=asyncio" in seen.err
    assert "family=discord" not in seen.err
    assert (list(root.handlers), logging.lastResort) == before
    assert logsafe.take_early() is None


def test_run_takes_over_the_early_install_and_restores_it(tmp_path, monkeypatch):
    installed = logsafe.install_early(lambda text: None)
    out = _Out()

    async def main(*args, **kwargs):
        _poison("asyncio", logging.CRITICAL)
    monkeypatch.setattr(runtime, "_main", main)
    root = logging.getLogger()
    assert installed.handler in root.handlers
    runtime.run(_isolated(tmp_path), client=object(), stderr=out)
    assert installed.handler not in root.handlers
    assert "log_record family=asyncio" in out.text
    assert logsafe.take_early() is None
