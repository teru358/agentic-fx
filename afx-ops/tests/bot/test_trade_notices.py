"""bot の取引通知 (AC-20、AC-21、AC-23)。

完全検証の後に `POST` / `SKIP_KNOWN` を分け、設定で出さない既知の event は何も投稿せずに cursor と
high watermark を進める。取引通知には設定済みの mention を付けない。
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import discord
import pytest

from afx_ops.api_client import Reply
from afx_ops.bot.adapter import BotAdapter, BotFailure, BotSettings
from afx_ops.event_outcome import SkipKnown
from afx_ops.gate.service import GateService
from afx_ops.gate.state import BotState, initialize_event_cursor, load, save
from afx_ops.gate.types import EventPlan

TS = "2026-10-20T00:00:00+00:00"
ROLE = 623456789012345678
USER = 523456789012345671


def _order(event_id: int, state: str, **fields) -> dict:
    base = {"state": state, "pair": "USDJPY", "side": "long", "entry_type": "limit",
            "qty": "0.1", "price": "150.123", "sl": "149.8", "tp": "150.9",
            "origin_code": "signal:rsi_div", "mode": "paper", "state_mode": None,
            "reason_code": "market_close" if state == "cancelled" else None}
    base.update(fields)
    return {"id": event_id, "ts": TS, "code": "order_state_changed", "ref": "order:12",
            "fields": base}


def _close(event_id: int, **fields) -> dict:
    base = {"pair": "USDJPY", "side": "long", "qty": "0.1", "entry_price": "150.123",
            "close_price": "150.9", "pnl": "7770.00", "ccy": "JPY", "pnl_r": "2.40",
            "held_sec": 11520, "reason_code": "tp", "origin_code": "signal:rsi_div",
            "mode": "paper", "state_mode": None}
    base.update(fields)
    return {"id": event_id, "ts": TS, "code": "position_closed", "ref": "order:12",
            "fields": base}


def _health(event_id: int) -> dict:
    return {"id": event_id, "ts": TS, "code": "service_health_changed", "ref": "instance",
            "fields": {"component_code": "api", "health_code": "ok"}}


def _all_kinds(start: int = 1) -> list[dict]:
    return [_order(start, "placed"), _order(start + 1, "filled"),
            _order(start + 2, "rejected"), _order(start + 3, "cancelled"),
            _order(start + 4, "expired"), _close(start + 5)]


class _Events:
    def __init__(self, *pages) -> None:
        self.pages = list(pages)
        self.afters: list[int] = []

    def events(self, *, after: int, limit: int) -> Reply:
        self.afters.append(after)
        page = self.pages.pop(0) if self.pages else {"events": [], "high_watermark": after}
        return Reply(200, page, None)

    def approvals(self, *, limit: int) -> Reply:
        return Reply(200, {"approvals": []}, None)

    def approval(self, approval_id: int):
        return Reply(200, {"status": "pending"}, None)


class _Channel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send(self, content, *, allowed_mentions) -> None:
        self.sent.append((content, allowed_mentions.to_dict()))


def _page(events: list[dict], high: int | None = None) -> dict:
    return {"events": events, "high_watermark": events[-1]["id"] if high is None else high}


class _Bot:
    def __init__(self, directory, client, **settings) -> None:
        self.settings = BotSettings(guild_id=12345678901234567, channel_id=12345678901234568,
                                    mention_role_id=settings.pop("mention_role_id", None),
                                    mention_user_ids=settings.pop("mention_user_ids", ()),
                                    **settings)
        self.fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        save(self.fd, initialize_event_cursor(BotState.empty(), 0))
        gate = GateService(BotState.empty(), guild_id=self.settings.guild_id,
                           channel_id=self.settings.channel_id, client=client)
        self.channel = _Channel()
        discord_client = SimpleNamespace(get_partial_messageable=lambda *a, **k: self.channel)
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

    def make(*pages, **settings) -> _Bot:
        directory = tmp_path / f"state{len(made)}"
        directory.mkdir()
        bot = _Bot(directory, _Events(*pages), **settings)
        made.append(bot)
        return bot

    yield make
    for bot in made:
        bot.close()


def _posted_kinds(bot: _Bot) -> list[str]:
    kinds = []
    for text in bot.texts:
        head = text.splitlines()[0]
        for key, kind in (("指値を入れ", "placed"), ("約定", "filled"), ("拒否", "rejected"),
                          ("取り消", "cancelled"), ("失効", "expired"), ("決済", "closed")):
            if key in head:
                kinds.append(kind)
    return kinds


@pytest.mark.parametrize("setting,expected", [
    (None, ["placed", "filled", "rejected", "cancelled", "expired", "closed"]),
    ("all", ["placed", "filled", "rejected", "cancelled", "expired", "closed"]),
    ("results", ["filled", "rejected", "closed"]),
    ("fills_closes", ["filled", "closed"]),
    ("off", []),
])
def test_ac23_trade_notices_selects_the_posted_kinds(make_bot, setting, expected):
    options = {} if setting is None else {"trade_notices": setting}
    bot = make_bot(_page(_all_kinds()), **options)

    bot.poll()

    assert _posted_kinds(bot) == expected
    assert len(bot.texts) == len(expected)
    # 出さない event も cursor は末尾まで進む
    assert (bot.state().cursor, bot.state().high_watermark) == (6, 6)


def test_ac23_skip_known_advances_cursor_and_high_watermark_in_one_save(make_bot, monkeypatch):
    bot = make_bot(_page([_order(1, "placed"), _order(2, "expired")], high=9),
                   trade_notices="off")
    saves: list[tuple[int, int]] = []
    from afx_ops.gate import state as gate_state
    real = gate_state.save

    def spy(fd, state):
        saves.append((state.cursor, state.high_watermark))
        real(fd, state)

    monkeypatch.setattr(gate_state, "save", spy)

    bot.poll()

    assert bot.texts == []
    assert saves[-2:] == [(1, 9), (2, 9)]
    assert saves.count((2, 9)) == 1
    assert (bot.state().cursor, bot.state().high_watermark) == (2, 9)


def test_a_posted_event_page_saves_once_with_cursor_and_high_together(make_bot, monkeypatch):
    bot = make_bot(_page([_order(1, "placed")], high=9))
    saves: list[tuple[int, int]] = []
    from afx_ops.gate import state as gate_state
    real = gate_state.save

    def spy(fd, state):
        saves.append((state.cursor, state.high_watermark))
        real(fd, state)

    monkeypatch.setattr(gate_state, "save", spy)

    bot.poll()

    assert len(bot.texts) == 1
    assert saves == [(1, 9)]


def test_a_page_without_new_events_still_records_the_high_watermark(make_bot):
    bot = make_bot(_page([], high=9))

    bot.poll()

    assert (bot.state().cursor, bot.state().high_watermark) == (0, 9)


def test_ac23_a_skipped_event_does_not_block_the_posts_after_it(make_bot):
    bot = make_bot(_page([_order(1, "placed"), _health(2), _order(3, "expired"), _health(4)]),
                   trade_notices="off")

    bot.poll()

    assert [t.split(":")[0] for t in bot.texts] == ["service_health_changed"] * 2
    assert bot.state().cursor == 4


def test_ac23_the_next_poll_resumes_after_a_skip_only_page(make_bot):
    bot = make_bot(_page([_order(1, "placed"), _order(2, "placed")]), _page([_health(3)]),
                   trade_notices="off")

    bot.poll()
    bot.poll()

    assert bot.adapter.client.afters == [0, 2]
    assert len(bot.texts) == 1


def test_ac23_commit_skip_is_a_separate_gate_operation_that_never_posts(make_bot):
    bot = make_bot(_page([_order(1, "placed")]), trade_notices="off")
    plan = bot.adapter.gate.plan_event(_order(1, "placed"), trade_notices="off")
    assert isinstance(plan, SkipKnown) and plan.event_id == 1

    bot.adapter.gate.commit_skip(1, high_watermark=1)

    assert bot.state().cursor == 1
    assert bot.texts == []


def test_ac23_plan_none_means_below_the_cursor_and_is_not_the_settings_filter(make_bot):
    bot = make_bot(trade_notices="off")
    gate = bot.adapter.gate
    first = gate.plan_event(_order(5, "placed"), trade_notices="off")
    assert isinstance(first, SkipKnown)
    gate.commit_skip(5, high_watermark=5)
    # cursor 以下は None、設定で出さないものは SkipKnown
    assert gate.plan_event(_order(5, "placed"), trade_notices="off") is None
    assert gate.plan_event(_order(4, "placed"), trade_notices="off") is None
    assert isinstance(gate.plan_event(_order(6, "placed"), trade_notices="off"), SkipKnown)
    posted = gate.plan_event(_order(6, "placed"), trade_notices="all")
    assert isinstance(posted, EventPlan)


def test_ac23_the_filter_does_not_continue_past_a_skipped_event_without_advancing(make_bot):
    # 設定で出さない event を continue だけで飛ばすと、cursor が進まず同じ event を毎回読み直す
    bot = make_bot(_page([_order(1, "placed")]), trade_notices="off")

    bot.poll()
    bot.poll()

    assert bot.adapter.client.afters == [0, 1]


def test_ac23_approval_events_are_unaffected_by_trade_notices(make_bot):
    approval = {"id": 2, "ts": TS, "code": "approval_state_changed", "ref": "approval:3",
                "fields": {"state": "approved", "decision_code": "approve",
                           "decided_by": "operator"}}
    for setting in ("all", "results", "fills_closes", "off"):
        bot = make_bot(_page([_order(1, "placed"), approval]), trade_notices=setting,
                       mention_role_id=ROLE, mention_user_ids=(USER,))
        bot.poll()
        approvals = [text for text in bot.texts if "approval_state_changed" in text]
        assert len(approvals) == 1
        # 承認側の event は従来どおり設定済みの mention を付ける
        assert approvals[0].startswith(f"<@&{ROLE}> <@{USER}> ")
        assert bot.state().cursor == 2


def test_ac20_trade_posts_carry_no_configured_mention_and_allow_none(make_bot):
    bot = make_bot(_page([_order(1, "filled"), _close(2)]), mention_role_id=ROLE,
                   mention_user_ids=(USER,))

    bot.poll()

    assert len(bot.channel.sent) == 2
    for content, allowed in bot.channel.sent:
        assert str(ROLE) not in content and str(USER) not in content and "<@" not in content
        assert allowed == discord.AllowedMentions.none().to_dict()


def test_ac20_the_posted_text_is_the_golden_text(make_bot):
    bot = make_bot(_page([_order(1, "filled", state_mode="learning"), _close(2)]))

    bot.poll()

    assert bot.texts == [
        "[ペーパー] 学習中 約定しました #12 USDJPY 買い\n"
        "数量 0.1 lot / 価格 150.123\n"
        "SL 149.8 / TP 150.9\n"
        "起点 signal\\:rsi\\_div",
        "[ペーパー] 決済しました #12 USDJPY 買い (利確到達)\n"
        "決済価格 150.9 (約定 150.123) / 数量 0.1 lot\n"
        "損益 +7,770 JPY (+2.40R) / 保有 3 時間 12 分\n"
        "起点 signal\\:rsi\\_div",
    ]


def test_ac20_trade_amounts_relative_removes_the_quantity_and_amount(make_bot):
    bot = make_bot(_page([_order(1, "filled"), _order(2, "cancelled"), _close(3)]),
                   trade_amounts="relative")

    bot.poll()

    assert len(bot.texts) == 3
    for text in bot.texts:
        assert "lot" not in text and "数量" not in text and "JPY" not in text.replace("USDJPY", "")
    assert "損益 +2.40R (価格 +0.52%)" in bot.texts[2]


def test_ac20_trade_amounts_does_not_change_which_events_are_posted(make_bot):
    bot = make_bot(_page(_all_kinds()), trade_notices="results", trade_amounts="relative")

    bot.poll()

    assert _posted_kinds(bot) == ["filled", "rejected", "closed"]


@pytest.mark.parametrize("bad", [
    _order(2, "filled", price="0001.2"), _order(2, "filled", qty=0.1),
    _order(2, "filled", origin_code="@everyone"),
    {**_order(2, "filled"), "code": "order_state_cancelled"},
    {**_order(2, "filled"), "ref": "order:0"},
    _close(2, pnl="1e999"),
])
@pytest.mark.parametrize("setting", ["all", "off"])
def test_ac21_an_invalid_trade_event_stops_whatever_the_setting(make_bot, bad, setting):
    bot = make_bot(_page([_health(1), bad, _health(3)]), trade_notices=setting)

    with pytest.raises(BotFailure) as failure:
        bot.poll()

    assert failure.value.reason == "payload_invalid"
    assert len(bot.texts) == 1
    assert bot.state().cursor == 1


def test_ac22_an_adversarial_origin_never_reaches_the_channel(make_bot):
    for origin in ("@everyone", "<@123456789012345678>", "signal:x\n@here", "**bold**"):
        bot = make_bot(_page([_order(1, "filled", origin_code=origin)]))
        with pytest.raises(BotFailure):
            bot.poll()
        assert bot.texts == []
