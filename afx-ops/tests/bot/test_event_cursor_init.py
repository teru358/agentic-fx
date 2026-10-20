"""bot の event cursor の初期化 (AC-25)。

snapshot の存在と cursor の初期化を分け、最初の正常な /events 応答の時点を「開始」とする。
それまでは event を配送せず、過去の履歴は流れない。
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from afx_ops.api_client import ApiError, Reply
from afx_ops.bot.adapter import BotAdapter, BotFailure, BotSettings
from afx_ops.gate import state as gate_state
from afx_ops.gate.service import GateService
from afx_ops.gate.state import BotState, load

SETTINGS = BotSettings(guild_id=12345678901234567, channel_id=12345678901234568,
                       mention_role_id=None, mention_user_ids=())
TS = "2026-10-11T00:00:00+00:00"
JOB = "0123456789abcdef" * 2


def _approve(event_id: int) -> dict:
    return {"id": event_id, "ts": TS, "code": "approval_state_changed", "ref": "approval:3",
            "fields": {"state": "approved", "decision_code": "approve", "decided_by": "operator"}}


def _improve(event_id: int) -> dict:
    return {"id": event_id, "ts": TS, "code": "job_state_changed", "ref": "job:" + JOB,
            "fields": {"job_kind": "improve", "state": "done", "result_code": "ok"}}


def _health(event_id: int) -> dict:
    return {"id": event_id, "ts": TS, "code": "service_health_changed", "ref": "instance",
            "fields": {"component_code": "api", "health_code": "ok"}}


def _trade(event_id: int) -> dict:
    return {"id": event_id, "ts": TS, "code": "order_state_changed", "ref": "order:1",
            "fields": {"state": "filled"}}


def _history() -> list[dict]:
    return [_approve(1), _improve(2), _health(3), _trade(4), _approve(5)]


class _Events:
    """scripted な /events。応答の列を順に返し、各呼び出しの after を覚える。"""

    def __init__(self, *pages) -> None:
        self.pages = list(pages)
        self.afters: list[int] = []

    def events(self, *, after: int, limit: int) -> Reply:
        self.afters.append(after)
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        if isinstance(page, Reply):
            return page
        return Reply(200, page, None)

    def approvals(self, *, limit: int) -> Reply:
        return Reply(200, {"approvals": []}, None)


class _Channel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content, *, allowed_mentions) -> None:
        self.sent.append(content)


def _page(events: list[dict], high: int | None = None) -> dict:
    return {"events": events, "high_watermark": events[-1]["id"] if high is None else high}


class _Process:
    """runtime と同じ構成 (state_dir_fd なし、adapter が snapshot の writer)。"""

    def __init__(self, directory, client) -> None:
        self.fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        gate = GateService(BotState.empty(), guild_id=SETTINGS.guild_id,
                           channel_id=SETTINGS.channel_id, client=client)
        self.channel = _Channel()
        discord_client = SimpleNamespace(get_partial_messageable=lambda *a, **k: self.channel)
        self.adapter = BotAdapter(gate=gate, client=client, dir_fd=self.fd, settings=SETTINGS,
                                  discord_client=discord_client)

    def start(self) -> None:
        asyncio.run(self.adapter.prepare())
        asyncio.run(self.adapter.reconcile_startup())

    def poll(self) -> None:
        asyncio.run(self.adapter.deliver_events())

    def snapshot(self) -> dict:
        return json.loads(self._path.read_text(encoding="ascii"))

    def close(self) -> None:
        if self.fd is not None:
            self.adapter.close()
            os.close(self.fd)
            self.fd = None


@pytest.fixture
def process(tmp_path):
    made: list[_Process] = []

    def make(client) -> _Process:
        proc = _Process(tmp_path, client)
        proc._path = tmp_path / "bot.snapshot"
        made.append(proc)
        return proc

    yield make
    for proc in made:
        proc.close()


def _write_v4(tmp_path, *, cursor: int, high: int) -> None:
    (tmp_path / "bot.snapshot").write_text(
        json.dumps({"version": 4, "cursor": cursor, "high_watermark": high, "approvals": []},
                   sort_keys=True), encoding="ascii")


def test_ac25_a_mixed_history_without_a_snapshot_posts_nothing_and_starts_at_the_end(process):
    client = _Events(_page(_history()))
    proc = process(client)
    proc.start()
    proc.poll()
    assert proc.channel.sent == []
    state = load(proc.fd)
    assert (state.cursor, state.high_watermark, state.event_cursor_initialized) == (5, 5, True)
    assert proc.snapshot()["event_cursor_initialized"] is True


def test_ac25_an_event_after_the_start_is_posted(process):
    client = _Events(_page(_history()), _page([_health(6)]))
    proc = process(client)
    proc.start()
    proc.poll()
    proc.poll()
    assert client.afters == [0, 5]
    assert len(proc.channel.sent) == 1 and proc.channel.sent[0].startswith("service_health_changed")
    assert load(proc.fd).cursor == 6


def test_ac25_an_empty_history_still_starts_and_a_saved_cursor_zero_is_not_absence(process):
    client = _Events({"events": [], "high_watermark": 0}, _page([_health(1)]))
    proc = process(client)
    proc.start()
    assert load(proc.fd).event_cursor_initialized is False
    proc.poll()
    state = load(proc.fd)
    assert (state.cursor, state.high_watermark, state.event_cursor_initialized) == (0, 0, True)
    proc.poll()
    assert len(proc.channel.sent) == 1


@pytest.mark.parametrize("failure", [ApiError("unreachable"), Reply(503, None, "unavailable")])
def test_ac25_a_failed_first_poll_leaves_the_flag_false_and_posts_nothing(process, failure):
    client = _Events(failure)
    proc = process(client)
    proc.start()
    with pytest.raises((ApiError, BotFailure)):
        proc.poll()
    assert proc.channel.sent == []
    assert proc.snapshot()["event_cursor_initialized"] is False


def test_ac25_startup_saves_then_a_failed_first_poll_then_restart_starts_at_the_end(process):
    first = process(_Events(ApiError("unreachable")))
    first.start()
    assert first.snapshot()["event_cursor_initialized"] is False
    with pytest.raises(ApiError):
        first.poll()
    first.close()
    second = process(_Events(_page(_history()), _page([_health(6)])))
    second.start()
    assert second.snapshot()["event_cursor_initialized"] is False
    second.poll()
    assert second.channel.sent == []
    assert (load(second.fd).cursor, load(second.fd).event_cursor_initialized) == (5, True)
    second.poll()
    assert len(second.channel.sent) == 1


def test_ac25_startup_saves_never_change_an_initialized_flag(process):
    proc = process(_Events(_page(_history())))
    proc.start()
    proc.poll()
    proc.close()
    again = process(_Events({"events": [], "high_watermark": 5}))
    again.start()
    state = load(again.fd)
    assert (state.cursor, state.high_watermark, state.event_cursor_initialized) == (5, 5, True)
    assert again.snapshot()["event_cursor_initialized"] is True


def test_ac25_the_first_success_writes_cursor_and_flag_in_one_replacement(process, monkeypatch):
    writes: list[tuple[int, int, bool]] = []
    real = gate_state.save

    def spy(dir_fd, state):
        real(dir_fd, state)
        writes.append((state.cursor, state.high_watermark, state.event_cursor_initialized))

    monkeypatch.setattr(gate_state, "save", spy)
    proc = process(_Events(_page(_history())))
    proc.start()
    before = list(writes)
    proc.poll()
    assert writes[len(before):] == [(5, 5, True)]
    # 起動時の保存は、どれも flag を立てず cursor も動かさない
    assert all(item == (0, 0, False) for item in before)


def test_ac25_a_v4_snapshot_with_delivery_history_migrates_to_true_and_continues(process, tmp_path):
    _write_v4(tmp_path, cursor=3, high=3)
    client = _Events(_page([_health(4), _health(5)]))
    proc = process(client)
    proc.start()
    assert proc.snapshot()["version"] == 5 and proc.snapshot()["event_cursor_initialized"] is True
    proc.poll()
    assert client.afters == [3]
    assert len(proc.channel.sent) == 2
    assert load(proc.fd).cursor == 5


def test_ac25_a_v4_snapshot_at_zero_zero_migrates_to_false_and_does_not_replay(process, tmp_path):
    _write_v4(tmp_path, cursor=0, high=0)
    proc = process(_Events(_page(_history())))
    proc.start()
    assert proc.snapshot()["version"] == 5 and proc.snapshot()["event_cursor_initialized"] is False
    proc.poll()
    assert proc.channel.sent == []
    assert (load(proc.fd).cursor, load(proc.fd).event_cursor_initialized) == (5, True)


def test_ac25_an_unreadable_event_in_the_skipped_history_does_not_block_the_start(process):
    client = _Events({"events": [_health(1), {"id": "x"}], "high_watermark": 2})
    proc = process(client)
    proc.start()
    proc.poll()
    assert proc.channel.sent == []
    assert load(proc.fd).event_cursor_initialized is True


def test_a_first_page_whose_watermark_is_below_its_event_ids_starts_at_the_largest_id(process):
    client = _Events(_page([_health(100)], 50), _page([_health(100)], 100),
                     {"events": [], "high_watermark": 100})
    proc = process(client)
    proc.start()
    proc.poll()
    assert proc.channel.sent == []
    state = load(proc.fd)
    assert (state.cursor, state.high_watermark, state.event_cursor_initialized) == (100, 100, True)
    proc.poll()
    assert client.afters == [0, 100]
    assert proc.channel.sent == []


def test_an_initialized_page_whose_watermark_is_below_its_event_ids_posts_and_advances(process):
    client = _Events(_page(_history()), _page([_health(200)], 150))
    proc = process(client)
    proc.start()
    proc.poll()
    proc.poll()
    assert len(proc.channel.sent) == 1
    state = load(proc.fd)
    assert (state.cursor, state.high_watermark) == (200, 200)


_BAD_PAGES = {
    "no watermark": {"events": []},
    "negative watermark": {"events": [], "high_watermark": -1},
    "boolean watermark": {"events": [], "high_watermark": True},
}


@pytest.mark.parametrize("name", list(_BAD_PAGES))
def test_a_malformed_first_page_starts_nothing(process, name):
    proc = process(_Events(_BAD_PAGES[name]))
    proc.start()
    with pytest.raises(BotFailure, match="events_invalid"):
        proc.poll()
    assert proc.channel.sent == []
    assert proc.snapshot()["event_cursor_initialized"] is False


@pytest.mark.parametrize("name", list(_BAD_PAGES))
def test_a_malformed_page_after_the_start_changes_nothing(process, name):
    proc = process(_Events(_page([_health(1)]), _BAD_PAGES[name]))
    proc.start()
    proc.poll()
    before = proc.snapshot()
    with pytest.raises(BotFailure, match="events_invalid"):
        proc.poll()
    assert proc.channel.sent == [] and proc.snapshot() == before


def test_a_page_without_events_still_persists_a_higher_watermark_and_a_later_rollback_stops(process):
    proc = process(_Events(_page([_health(1)]), {"events": [], "high_watermark": 9},
                           {"events": [], "high_watermark": 4}))
    proc.start()
    proc.poll()
    proc.poll()
    assert proc.snapshot()["high_watermark"] == 9 and proc.snapshot()["cursor"] == 1
    with pytest.raises(BotFailure, match="high_watermark_regressed"):
        proc.poll()
    assert proc.snapshot()["high_watermark"] == 9


def test_the_regression_check_runs_before_the_uninitialised_branch(process):
    # 矛盾した state は load が拒否するので、ここでは作為的に作って検査の順序だけを見る
    proc = process(_Events({"events": [], "high_watermark": 50}))
    for name, value in (("cursor", 100), ("high_watermark", 100)):
        object.__setattr__(proc.adapter.gate.state, name, value)
    with pytest.raises(BotFailure, match="high_watermark_regressed"):
        proc.poll()
    assert proc.adapter.gate.state.cursor == 100


def test_unreadable_events_dropped_from_the_first_page_are_logged_with_count_and_id_range(
        process, capsys):
    page = {"events": [_health(1), _health(2), {"id": "x"}, _health(9)], "high_watermark": 2}
    proc = process(_Events(page))
    proc.start()
    proc.poll()
    assert capsys.readouterr().err == (
        "afx-ops bot: initial_page_unreadable skipped=2 readable_ids=1-2\n")
    assert load(proc.fd).event_cursor_initialized is True


def test_a_first_page_without_unreadable_events_logs_nothing(process, capsys):
    proc = process(_Events(_page(_history())))
    proc.start()
    proc.poll()
    assert capsys.readouterr().err == ""
