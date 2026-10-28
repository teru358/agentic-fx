"""bot の改善通知: 本文の完全一致、詳細の取得失敗、設定、送信失敗、重複、所有期間。

Discord は fake channel、操作 API は fake client。本文 (観察文・数値) は投稿の content にだけ現れる。
"""
from __future__ import annotations

import ast
import asyncio
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from afx_ops.api_client import ApiError, Reply
from afx_ops.bot import adapter as adapter_module
from afx_ops.bot.adapter import BotAdapter, BotFailure, BotSettings
from afx_ops.event_outcome import SkipKnown
from afx_ops.gate.service import GateService
from afx_ops.gate.state import BotState, initialize_event_cursor, load, save
from afx_ops.gate.types import EventPlan

TS = "2026-10-25T00:00:00+00:00"
ROLE = 623456789012345678
USER = 523456789012345671
NOTE = ("session_ranges によるロンドン・NY開始足のレンジ外ブレイク後回帰を実装し、固定出口と短縮出口で"
        "USDJPY 1hを検証した。どちらも不採算判定となり、提出しなかった。次回は時間帯回帰以外の条件、"
        "または別の市場仮説を優先する。")
NOTE_ESCAPED = NOTE.replace("session_ranges", r"session\_ranges")


def _improve(event_id: int, mission: int, state: str = "finished", result=None, reason=None,
             tool=None, trigger: str = "manual") -> dict:
    return {"id": event_id, "ts": TS, "code": "improve_run_changed", "ref": f"improve:{mission}",
            "fields": {"state": state, "trigger": trigger, "result_code": result,
                       "reason_code": reason, "tool_calls": tool}}


def _started(event_id: int, mission: int, trigger: str = "manual") -> dict:
    return _improve(event_id, mission, "started", trigger=trigger)


def _approval_event(event_id: int, approval_id: int = 7) -> dict:
    return {"id": event_id, "ts": TS, "code": "approval_state_changed",
            "ref": f"approval:{approval_id}",
            "fields": {"state": "approved", "decision_code": "approve"}}


def _pair(name="USDJPY", trades=13, pf=1.8458377992140615, avg_r=0.3029234868346611,
          evaluable=False) -> dict:
    return {"pair": name, "trades": trades, "pf": pf, "avg_r": avg_r, "evaluable": evaluable}


def _dto(mission: int, *, result=None, reason=None, tool=None, elapsed=None, text=None,
         report_state="none", approval=None, floor=None, trigger="manual", terminal=True,
         backend="codex", model="gpt-5.6-luna") -> dict:
    snapshot = ({"trigger": trigger, "result_code": result, "reason_code": reason,
                 "tool_calls": tool, "observation_text": text} if terminal else None)
    return {"id": mission, "backend": backend, "model": model, "elapsed_sec": elapsed,
            "snapshot": snapshot, "report_state": report_state, "approval": approval,
            "floor": floor}


class _Client:
    """events と improve_run を scripted に返す。"""

    def __init__(self, *pages, runs=None) -> None:
        self.pages = list(pages)
        self.afters: list[int] = []
        self.runs = dict(runs or {})
        self.run_calls: list[tuple[int, float]] = []

    def events(self, *, after: int, limit: int) -> Reply:
        self.afters.append(after)
        page = self.pages.pop(0) if self.pages else {"events": [], "high_watermark": after}
        return Reply(200, page, None)

    def approvals(self, *, limit: int) -> Reply:
        return Reply(200, {"approvals": []}, None)

    def approval(self, approval_id: int, *, timeout=None):
        return Reply(200, {"status": "pending"}, None)

    def improve_run(self, mission_id: int, *, deadline: float) -> Reply:
        self.run_calls.append((mission_id, deadline - time.monotonic()))
        outcome = self.runs.get(mission_id, Reply(404, None, "not_found"))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _ok(data: dict) -> Reply:
    return Reply(200, data, None)


class _Channel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.failures: list[BaseException] = []

    async def send(self, content, *, allowed_mentions) -> None:
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((content, allowed_mentions.to_dict()))


def _http_error() -> discord.HTTPException:
    return discord.HTTPException(SimpleNamespace(status=503, reason="unavailable"), "boom")


def _page(events: list[dict], high: int | None = None) -> dict:
    return {"events": events, "high_watermark": events[-1]["id"] if high is None else high}


class _Bot:
    def __init__(self, directory, client, **settings) -> None:
        self.settings = BotSettings(guild_id=12345678901234567, channel_id=12345678901234568,
                                    mention_role_id=settings.pop("mention_role_id", None),
                                    mention_user_ids=settings.pop("mention_user_ids", ()),
                                    **settings)
        self.directory = directory
        self.fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        save(self.fd, initialize_event_cursor(BotState.empty(), 0))
        gate = GateService(BotState.empty(), guild_id=self.settings.guild_id,
                           channel_id=self.settings.channel_id, client=client)
        self.channel = _Channel()
        discord_client = SimpleNamespace(get_partial_messageable=lambda *a, **k: self.channel)
        self.client = client
        self.adapter = BotAdapter(gate=gate, client=client, dir_fd=self.fd,
                                  settings=self.settings, discord_client=discord_client)
        asyncio.run(self.adapter.prepare())

    def poll(self) -> None:
        asyncio.run(self.adapter.deliver_events())

    def state(self):
        return load(self.fd)

    @property
    def texts(self) -> list[str]:
        return [content for content, _ in self.channel.sent]

    def close(self) -> None:
        self.adapter.close()
        os.close(self.fd)


@pytest.fixture
def make_bot(tmp_path):
    made: list[_Bot] = []

    def make(*pages, runs=None, **settings) -> _Bot:
        directory = tmp_path / f"state{len(made)}"
        directory.mkdir()
        bot = _Bot(directory, _Client(*pages, runs=runs), **settings)
        made.append(bot)
        return bot

    yield make
    for bot in made:
        bot.close()


# ------------------------------------------------------------ 本文の完全一致

def _real_missions():
    """2026-10-25 の実 mission (#2040 / #2043 / #2044 / #2045 / #2046) の event と詳細。"""
    events = [
        _improve(1, 2040, result="observation", tool=11),
        _improve(2, 2043, result="gate_failed", reason="unprofitable", tool=10,
                 trigger="scheduled"),
        _improve(3, 2044, result="gate_failed", reason="insufficient_trades", tool=13),
        _improve(4, 2045, result="gate_failed", reason="unprofitable", tool=14),
        _improve(5, 2046, result="failed", reason="commit_crashed", tool=None),
    ]
    runs = {
        2040: _ok(_dto(2040, result="observation", tool=11, elapsed=289, text=NOTE)),
        2043: _ok(_dto(2043, result="gate_failed", reason="unprofitable", tool=10, elapsed=261,
                       report_state="published", trigger="scheduled",
                       floor={"stages": [{"stage": "holdout", "pairs": [_pair()]}]})),
        2044: _ok(_dto(2044, result="gate_failed", reason="insufficient_trades", tool=13,
                       elapsed=400)),
        2045: _ok(_dto(2045, result="gate_failed", reason="unprofitable", tool=14, elapsed=447,
                       report_state="published",
                       floor={"stages": [{"stage": "holdout", "pairs": [
                           _pair(trades=24, pf=0.5145606198869912, avg_r=-0.2772392430083411)]}]})),
        2046: _ok(_dto(2046, result="failed", reason="commit_crashed", tool=None, elapsed=405)),
    }
    return events, runs


def test_the_posted_bodies_of_the_real_missions_match_the_documented_text(make_bot):
    events, runs = _real_missions()
    bot = make_bot(_page(events), runs=runs)

    bot.poll()

    assert bot.texts == [
        "[改善] 提出なし (agent の申告) #2040\n"
        f"agent の申告: {NOTE_ESCAPED}\n"
        "所要 4分49秒 / tool 11 回",
        "[改善] 床未達で見送り #2043 (unprofitable)\n"
        "落ちた段: holdout\n"
        "holdout: USDJPY pf 1.85 avg_r +0.30 取引 13 (評価不能)\n"
        "所要 4分21秒 / tool 10 回",
        "[改善] 見送り #2044 (insufficient\\_trades)\n"
        "所要 6分40秒 / tool 13 回",
        "[改善] 床未達で見送り #2045 (unprofitable)\n"
        "落ちた段: holdout\n"
        "holdout: USDJPY pf 0.51 avg_r -0.28 取引 24 (評価不能)\n"
        "所要 7分27秒 / tool 14 回",
        "[改善] 失敗 #2046 (commit\\_crashed)\n"
        "所要 6分45秒 / tool -",
    ]
    assert bot.state().cursor == 5


def test_start_notices_show_the_trigger_and_the_backend_line(make_bot):
    bot = make_bot(_page([_started(1, 2043, "manual"), _started(2, 2044, "scheduled")]),
                   runs={2043: _ok(_dto(2043, terminal=False)),
                         2044: _ok(_dto(2044, terminal=False, backend="local",
                                        model="qwen3.6-35b"))})

    bot.poll()

    assert bot.texts == ["[改善] 開始 #2043 (手動)\nbackend codex / gpt-5.6-luna",
                         "[改善] 開始 #2044 (週次)\nbackend local / qwen3.6-35b"]


def test_repeat_notices_add_the_fixed_progress_line(make_bot):
    event = _started(1, 2043)
    event["fields"].update({"repeat_run": 2, "repeat_max_runs": 10,
                            "repeat_elapsed_sec": 305, "repeat_duration_sec": 1800})
    detail = _dto(2043, terminal=False)
    detail["repeat"] = {"run": 2, "max_runs": 10, "elapsed_sec": 305,
                        "duration_sec": 1800}
    bot = make_bot(_page([event]), runs={2043: _ok(detail)})

    bot.poll()

    assert bot.texts == ["[改善] 開始 #2043 (手動)\nbackend codex / gpt-5.6-luna\n"
                         "繰り返し 2/10 回 · 5m/30m"]


def test_a_start_notice_for_a_mission_that_already_finished_still_shows_the_backend(make_bot):
    bot = make_bot(_page([_started(1, 2043)]),
                   runs={2043: _ok(_dto(2043, result="observation", tool=3, elapsed=10))})

    bot.poll()

    assert bot.texts == ["[改善] 開始 #2043 (手動)\nbackend codex / gpt-5.6-luna"]


# ------------------------------------------------------------ 取得失敗の全文

FAILURES = {
    "404": lambda mission: Reply(404, None, "not_found"),
    "500": lambda mission: Reply(500, None, "internal"),
    "503": lambda mission: Reply(503, None, "unavailable", 5),
    "deadline": lambda mission: TimeoutError(),
    "api-timeout": lambda mission: ApiError("timeout"),
    "refused": lambda mission: ApiError("refused"),
    "oserror": lambda mission: OSError(),
    "no-body": lambda mission: Reply(200, None, "internal"),
    "wrong-id": lambda mission: _ok(_dto(mission + 1, result="observation", tool=1)),
    "bad-field": lambda mission: _ok({**_dto(mission, result="observation", tool=1),
                                      "report_state": "weird"}),
    "bad-floor": lambda mission: _ok(_dto(mission, result="observation", tool=1,
                                          floor={"stages": [{"stage": "x", "pairs": []}]})),
}

FULL_TEXTS = {
    "started": ("[改善] 開始 #2040 (手動)", _started(1, 2040)),
    "submitted": ("[改善] 候補を提出 #2040\ntool 14 回\n(詳細を取得できませんでした)",
                  _improve(1, 2040, result="submitted", tool=14)),
    "observation": ("[改善] 提出なし (agent の申告) #2040\ntool 11 回\n"
                    "(詳細を取得できませんでした)",
                    _improve(1, 2040, result="observation", tool=11)),
    "gate_failed": ("[改善] 床未達で見送り #2040 (unprofitable)\ntool 10 回\n"
                    "(詳細を取得できませんでした)",
                    _improve(1, 2040, result="gate_failed", reason="unprofitable", tool=10)),
    "failed": ("[改善] 失敗 #2040 (commit\\_crashed)\n(詳細を取得できませんでした)",
               _improve(1, 2040, result="failed", reason="commit_crashed", tool=None)),
    "report": ("[改善] レポートを公開 #2040\ntool 2 回\n(詳細を取得できませんでした)",
               _improve(1, 2040, result="report", tool=2)),
}


@pytest.mark.parametrize("failure", sorted(FAILURES))
@pytest.mark.parametrize("kind", sorted(FULL_TEXTS))
def test_a_failed_detail_fetch_still_posts_the_event_only_text(make_bot, kind, failure):
    expected, event = FULL_TEXTS[kind]
    bot = make_bot(_page([event]), runs={2040: FAILURES[failure](2040)})

    bot.poll()

    assert bot.texts == [expected]
    assert bot.state().cursor == 1
    assert len(bot.client.run_calls) == 1


def test_the_detail_fetch_is_given_a_three_second_deadline(make_bot):
    bot = make_bot(_page([_started(1, 2043)]), runs={2043: _ok(_dto(2043, terminal=False))})

    bot.poll()

    ((mission, left),) = bot.client.run_calls
    assert mission == 2043 and 2.0 < left <= 3.0


def test_a_failed_fetch_does_not_stop_the_events_after_it(make_bot):
    events = [_improve(1, 2040, result="observation", tool=1),
              _improve(2, 2041, result="failed", reason="interrupted")]
    bot = make_bot(_page(events), runs={2040: TimeoutError(),
                                        2041: _ok(_dto(2041, result="failed",
                                                       reason="interrupted"))})

    bot.poll()

    assert [t.splitlines()[0] for t in bot.texts] == [
        "[改善] 提出なし (agent の申告) #2040", "[改善] 失敗 #2041 (interrupted)"]
    assert bot.texts[1].splitlines()[-1] == "所要 -"


def test_an_unknown_reason_word_is_rendered_in_parentheses_and_the_cursor_advances(make_bot):
    bot = make_bot(_page([_improve(1, 2040, result="failed", reason="zzz_new_word", tool=2)]),
                   runs={2040: _ok(_dto(2040, result="failed", reason="zzz_new_word", tool=2,
                                        elapsed=5))})

    bot.poll()

    assert bot.texts == ["[改善] 失敗 #2040 (zzz\\_new\\_word)\n所要 0分05秒 / tool 2 回"]
    assert bot.state().cursor == 1


@pytest.mark.parametrize("bad", [
    _improve(2, 2040, result="failed", reason="pf:7.7777"),
    _improve(2, 2040, result="nope"),
    _improve(2, 2040, state="done"),
    _improve(2, 2040, trigger="auto"),
    _improve(2, 2040, tool=True),
    _improve(2, 2040, tool=1_000_001),
    {**_improve(2, 2040), "ref": "order:1"},
    {**_improve(2, 2040), "ref": "improve:1234567890123456789"},
])
def test_an_improve_event_outside_the_grammar_is_skipped_and_the_events_after_it_are_posted(
        make_bot, bad, capsys):
    after = _improve(3, 2041, result="failed", reason="interrupted")
    bot = make_bot(_page([_improve(1, 2039, result="failed", reason="x"), bad, after]),
                   runs={2039: _ok(_dto(2039, result="failed", reason="x")),
                         2041: _ok(_dto(2041, result="failed", reason="interrupted"))})

    bot.poll()

    assert [t.splitlines()[0] for t in bot.texts] == ["[改善] 失敗 #2039 (x)",
                                                      "[改善] 失敗 #2041 (interrupted)"]
    assert bot.state().cursor == 3
    assert "improve_event_invalid event=2" in capsys.readouterr().err
    assert [call[0] for call in bot.client.run_calls] == [2039, 2041]


def test_an_invalid_improve_event_does_not_hold_back_an_approval_event_after_it(make_bot):
    bad = _improve(2, 2040, result="failed", reason="pf:7.7777")
    bot = make_bot(_page([bad, _approval_event(3)]))

    bot.poll()

    assert bot.texts == ["承認 #7 が承認・配備されました。"]
    assert bot.state().cursor == 3


def test_an_invalid_event_that_is_not_an_improve_event_still_stops_the_bot(make_bot):
    bot = make_bot(_page([_improve(1, 2039, result="failed", reason="x"),
                          {**_approval_event(2), "ref": "approval:0x"}]),
                   runs={2039: _ok(_dto(2039, result="failed", reason="x"))})

    with pytest.raises(BotFailure):
        bot.poll()

    assert bot.state().cursor == 1 and len(bot.texts) == 1


def test_a_very_long_backend_and_model_still_post_within_the_discord_limit(make_bot):
    long_name = "ab_*" * 400
    events = [_started(1, 2043), _improve(2, 2044, result="submitted", tool=1)]
    bot = make_bot(_page(events), runs={
        2043: _ok(_dto(2043, terminal=False, backend=long_name, model=long_name)),
        2044: _ok(_dto(2044, result="submitted", tool=1, backend=long_name, model=long_name,
                       approval={"id": 3, "plugin": "p" * 40, "kind": long_name}))})

    bot.poll()

    assert len(bot.texts) == 2 and all(len(text) <= 2000 for text in bot.texts)
    assert bot.state().cursor == 2


# ------------------------------------------------------------ 取得の thread と設定の分岐

def test_a_slow_detail_fetch_does_not_hold_the_gate_thread(make_bot):
    entered, release, seen = threading.Event(), threading.Event(), {}
    bot = make_bot(_page([_started(1, 2043)]))

    def slow(mission_id, *, deadline):
        seen["thread"] = threading.current_thread().name
        entered.set()
        assert release.wait(10)
        return _ok(_dto(2043, terminal=False))

    bot.client.improve_run = slow

    async def scenario():
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(bot.adapter.deliver_events())
        assert await loop.run_in_executor(None, entered.wait, 10)
        try:
            return await asyncio.wait_for(
                bot.adapter._run(lambda: threading.current_thread().name), 2)
        finally:
            release.set()
            await task

    gate_thread = asyncio.run(scenario())

    assert gate_thread.startswith("afx-bot-gate")
    assert seen["thread"].startswith("afx-bot-improve")
    assert len(bot.texts) == 1 and bot.state().cursor == 1


def test_every_plan_of_an_improve_event_carries_the_same_settings(make_bot):
    bot = make_bot(_page([_improve(1, 2040, result="observation", tool=1)]),
                   runs={2040: _ok(_dto(2040, result="observation", tool=1))},
                   improve_notices="results", trade_notices="all")
    calls: list[dict] = []
    real = bot.adapter.gate.plan_event

    def spy(event, **kwargs):
        calls.append(kwargs)
        return real(event, **kwargs)

    bot.adapter.gate.plan_event = spy

    bot.poll()

    assert len(bot.texts) == 1 and calls
    assert {call["improve_notices"] for call in calls} == {"results"}
    assert {call["trade_notices"] for call in calls} == {"all"}
    assert [("improve_detail" in call) for call in calls] == [False, True]


# ------------------------------------------------------------ mention

def test_improve_plans_never_carry_a_mention_and_allow_none():
    gate = GateService(BotState.empty(), guild_id=1, channel_id=2, client=None)
    plan = gate.plan_event(_improve(1, 2040, result="observation", tool=1),
                           mention_role_id=ROLE, mention_user_ids=(USER,))
    assert isinstance(plan, EventPlan)
    assert plan.allow_mentions is False and plan.mentions == ()
    start = gate.plan_event(_started(2, 2040), mention_role_id=ROLE, mention_user_ids=(USER,))
    assert start.allow_mentions is False and start.mentions == ()


def test_improve_posts_carry_no_configured_mention_even_with_a_hostile_note(make_bot):
    hostile = "@everyone <@123> <@&456> **x** ```y``` https://evil.example/a_b"
    bot = make_bot(_page([_improve(1, 2040, result="observation", tool=1), _started(2, 2041)]),
                   runs={2040: _ok(_dto(2040, result="observation", tool=1, elapsed=3,
                                        text=hostile)),
                         2041: _ok(_dto(2041, terminal=False, backend="@everyone",
                                        model="<@&1>"))},
                   mention_role_id=ROLE, mention_user_ids=(USER,))

    bot.poll()

    assert len(bot.channel.sent) == 2
    for content, allowed in bot.channel.sent:
        assert allowed == discord.AllowedMentions.none().to_dict()
        assert str(ROLE) not in content and str(USER) not in content
        for banned in ("@everyone", "<@123>", "<@&456>", "```", "https://", "**x**"):
            assert banned not in content


# ------------------------------------------------------------ 設定

def _mixed_events() -> list[dict]:
    return [_started(1, 2040), _improve(2, 2040, result="observation", tool=1),
            _started(3, 2041, "scheduled"), _improve(4, 2041, result="report", tool=2)]


def _mixed_runs() -> dict:
    return {2040: _ok(_dto(2040, result="observation", tool=1, elapsed=1)),
            2041: _ok(_dto(2041, result="report", tool=2, elapsed=2, trigger="scheduled"))}


@pytest.mark.parametrize("setting,expected", [
    (None, ["開始 #2040 (手動)", "提出なし (agent の申告) #2040", "開始 #2041 (週次)", "レポートを公開 #2041"]),
    ("all", ["開始 #2040 (手動)", "提出なし (agent の申告) #2040", "開始 #2041 (週次)", "レポートを公開 #2041"]),
    ("results", ["提出なし (agent の申告) #2040", "レポートを公開 #2041"]),
    ("off", []),
])
def test_improve_notices_selects_started_and_finished_posts(make_bot, setting, expected):
    options = {} if setting is None else {"improve_notices": setting}
    bot = make_bot(_page(_mixed_events()), runs=_mixed_runs(), **options)

    bot.poll()

    assert [t.splitlines()[0].removeprefix("[改善] ") for t in bot.texts] == expected
    # 出さない event も cursor は末尾まで進む
    assert (bot.state().cursor, bot.state().high_watermark) == (4, 4)


def test_events_the_setting_hides_are_not_fetched(make_bot):
    bot = make_bot(_page(_mixed_events()), runs=_mixed_runs(), improve_notices="off")

    bot.poll()

    assert bot.client.run_calls == []


def test_a_hidden_improve_event_is_a_skip_known_and_never_a_post():
    gate = GateService(BotState.empty(), guild_id=1, channel_id=2, client=None)
    assert isinstance(gate.plan_event(_started(1, 5), improve_notices="results"), SkipKnown)
    assert isinstance(gate.plan_event(_improve(2, 5, result="failed", reason="x"),
                                      improve_notices="off"), SkipKnown)
    assert isinstance(gate.plan_event(_started(3, 5), improve_notices="all"), EventPlan)


def test_the_setting_is_fixed_at_start_and_never_read_again():
    settings = BotSettings(guild_id=1, channel_id=2, mention_role_id=None, mention_user_ids=())
    assert settings.improve_notices == "all"
    with pytest.raises(Exception):
        settings.improve_notices = "off"  # type: ignore[misc]
    bot_dir = Path(adapter_module.__file__).parent
    # probe.py は隔離の前に設定を読む入口。稼働中に動く module は設定を読まない
    for path in (bot_dir / name for name in ("adapter.py", "runtime.py", "views.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "afx_ops.config" not in imported and "read_config" not in names, path.name
        assert "ops.toml" not in text, path.name


def test_other_notice_settings_do_not_change_improve_posts(make_bot):
    bot = make_bot(_page(_mixed_events()), runs=_mixed_runs(), trade_notices="off",
                   ops_notices="all")

    bot.poll()

    assert len(bot.texts) == 4


# ------------------------------------------------------------ 送信失敗

@pytest.mark.parametrize("error", [_http_error, OSError, asyncio.TimeoutError])
def test_a_send_failure_leaves_the_cursor_and_resends_from_a_fresh_fetch(make_bot, error):
    bot = make_bot(_page([_improve(1, 2040, result="observation", tool=1)]),
                   _page([_improve(1, 2040, result="observation", tool=1)]),
                   runs={2040: _ok(_dto(2040, result="observation", tool=1, elapsed=3,
                                        text="memo"))})
    bot.channel.failures = [error()]

    bot.poll()

    assert bot.texts == []
    assert (bot.state().cursor, bot.state().high_watermark) == (0, 0)
    assert len(bot.client.run_calls) == 1

    bot.poll()

    assert len(bot.texts) == 1 and "memo" in bot.texts[0]
    assert (bot.state().cursor, bot.state().high_watermark) == (1, 1)
    assert len(bot.client.run_calls) == 2


def test_a_send_failure_stops_the_events_after_it_for_this_poll(make_bot):
    events = [_improve(1, 2040, result="failed", reason="x"),
              _improve(2, 2041, result="failed", reason="y")]
    bot = make_bot(_page(events), runs={2040: _ok(_dto(2040, result="failed", reason="x")),
                                        2041: _ok(_dto(2041, result="failed", reason="y"))})
    bot.channel.failures = [OSError()]

    bot.poll()

    assert bot.texts == [] and bot.state().cursor == 0
    assert [call[0] for call in bot.client.run_calls] == [2040]


# ------------------------------------------------------------ 重複 (at-least-once)

def _finished(event_id: int, mission: int = 2040) -> dict:
    return _improve(event_id, mission, result="observation", tool=1)


_RUNS = {2040: _ok(_dto(2040, result="observation", tool=1, elapsed=1)),
         2041: _ok(_dto(2041, result="observation", tool=1, elapsed=1))}


def test_a_duplicate_finished_in_one_page_posts_once_and_the_cursor_reaches_the_second(make_bot):
    bot = make_bot(_page([_finished(1), _finished(2)]), runs=_RUNS)

    bot.poll()

    assert len(bot.texts) == 1
    assert (bot.state().cursor, bot.state().high_watermark) == (2, 2)
    assert len(bot.client.run_calls) == 1


def test_a_duplicate_finished_across_pages_posts_once(make_bot):
    bot = make_bot(_page([_finished(1)]), _page([_finished(2)]), runs=_RUNS)

    bot.poll()
    bot.poll()

    assert len(bot.texts) == 1 and bot.state().cursor == 2


def test_a_resend_after_a_failed_send_is_not_a_duplicate(make_bot):
    bot = make_bot(_page([_finished(1)]), _page([_finished(1)]), _page([_finished(2)]),
                   runs=_RUNS)
    bot.channel.failures = [OSError()]

    bot.poll()
    assert bot.texts == []
    bot.poll()
    assert len(bot.texts) == 1
    bot.poll()
    assert len(bot.texts) == 1 and bot.state().cursor == 2


def test_started_and_finished_of_the_same_mission_are_both_posted(make_bot):
    bot = make_bot(_page([_started(1, 2040), _finished(2), _started(3, 2040), _finished(4)]),
                   runs=_RUNS)

    bot.poll()

    assert [t.splitlines()[0] for t in bot.texts] == [
        "[改善] 開始 #2040 (手動)", "[改善] 提出なし (agent の申告) #2040"]
    assert bot.state().cursor == 4


def test_different_missions_are_not_folded(make_bot):
    bot = make_bot(_page([_finished(1, 2040), _finished(2, 2041)]), runs=_RUNS)

    bot.poll()

    assert len(bot.texts) == 2


def test_the_posted_set_is_only_in_the_process_so_a_restart_may_post_again(make_bot):
    first = make_bot(_page([_finished(1)]), runs=_RUNS)
    first.poll()
    second = make_bot(_page([_finished(1)]), runs=_RUNS)

    second.poll()

    assert len(first.texts) == 1 and len(second.texts) == 1
    assert "posted" not in "".join(os.listdir(first.directory))


def test_the_posted_set_is_bounded_and_drops_the_oldest(make_bot):
    bot = make_bot()
    limit = adapter_module.POSTED_IMPROVE_LIMIT
    for index in range(limit + 5):
        bot.adapter._remember_improve((f"improve:{index}", "finished"))

    kept = bot.adapter._posted_improve
    assert len(kept) == limit
    assert ("improve:0", "finished") not in kept and ("improve:4", "finished") not in kept
    assert (f"improve:{limit + 4}", "finished") in kept


# ------------------------------------------------------------ 所有期間

CANARY = "7.7777"
URL_CANARY = "https://canary.example/secret_path"


def _reachable_strings(root, seen=None, depth=0):
    """afx_ops の object・dict・list・tuple・set から届く文字列を集める (Discord の fake は除く)。"""
    seen = set() if seen is None else seen
    if id(root) in seen or depth > 8:
        return
    seen.add(id(root))
    if isinstance(root, (str, bytes)):
        yield root if isinstance(root, str) else root.decode("utf-8", "replace")
    elif isinstance(root, dict):
        for key, value in root.items():
            yield from _reachable_strings(key, seen, depth + 1)
            yield from _reachable_strings(value, seen, depth + 1)
    elif isinstance(root, (list, tuple, set, frozenset)):
        for item in root:
            yield from _reachable_strings(item, seen, depth + 1)
    elif type(root).__module__.startswith("afx_ops") and hasattr(root, "__dict__"):
        for name, value in vars(root).items():
            if name != "discord":
                yield from _reachable_strings(value, seen, depth + 1)
        yield repr(root)
    elif type(root).__module__.startswith("afx_ops") and hasattr(type(root), "__slots__"):
        for name in type(root).__slots__:
            if hasattr(root, name):
                yield from _reachable_strings(getattr(root, name), seen, depth + 1)


def _canary_runs() -> dict:
    return {
        2040: _ok(_dto(2040, result="observation", tool=1, elapsed=3,
                       text=f"値 {CANARY} と {URL_CANARY}")),
        2043: _ok(_dto(2043, result="gate_failed", reason="unprofitable", tool=1, elapsed=3,
                       report_state="published",
                       floor={"stages": [{"stage": "holdout", "pairs": [
                           _pair(pf=7.7777, avg_r=7.7777)]}]})),
    }


def test_the_body_values_reach_the_post_and_nowhere_else_the_bot_holds(make_bot, capsys, caplog):
    events = [_improve(1, 2040, result="observation", tool=1),
              _improve(2, 2043, result="gate_failed", reason="unprofitable", tool=1)]
    bot = make_bot(_page(events), runs=_canary_runs())

    bot.poll()

    # 観察文の数字は content にだけ出る。床の数値は丸めて content にだけ出る
    assert CANARY in bot.texts[0] and "pf 7.78 avg_r +7.78" in bot.texts[1]
    held = "\n".join(_reachable_strings(bot.adapter)) + "\n".join(
        _reachable_strings(bot.adapter.gate)) + repr(bot.adapter) + repr(bot.adapter.gate.state)
    assert CANARY not in held and "canary.example" not in held
    out = capsys.readouterr()
    assert CANARY not in out.out + out.err + caplog.text


def test_the_state_file_holds_only_the_cursor_and_ids(make_bot):
    events = [_improve(1, 2040, result="observation", tool=1)]
    bot = make_bot(_page(events), runs=_canary_runs())

    bot.poll()

    state = bot.state()
    assert (state.cursor, state.high_watermark) == (1, 1)
    for path in Path(bot.directory).iterdir():
        data = path.read_bytes().decode("utf-8", "replace")
        assert CANARY not in data and "canary" not in data and "session" not in data


def test_a_failed_fetch_leaks_nothing_through_exceptions_or_logs(make_bot, capsys, caplog):
    bot = make_bot(_page([_improve(1, 2040, result="observation", tool=1)]),
                   runs={2040: ApiError("response_lost", token_sent=True)})

    bot.poll()

    out = capsys.readouterr()
    assert out.out == "" and out.err == "" and caplog.text == ""
    assert len(bot.texts) == 1


def test_a_send_failure_leaves_no_body_in_the_bot_or_its_exception(make_bot):
    bot = make_bot(_page([_improve(1, 2040, result="observation", tool=1)]),
                   runs=_canary_runs())
    bot.channel.failures = [OSError()]

    bot.poll()

    held = "\n".join(_reachable_strings(bot.adapter)) + repr(bot.adapter)
    assert CANARY not in held and "canary.example" not in held
