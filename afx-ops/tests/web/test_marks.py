"""状態の印とカード: 割当表、不明の扱い、画面での位置。"""
from __future__ import annotations

import re

import pytest

from afx_ops.api_client import Reply
from afx_ops.web import marks, render
from afx_ops.web.request import Request, handle_request

UNKNOWN_MARK = '<span class="st">? 不明</span>'
JUNK = ["paused", "", 1, 0, None, [], {}, True, ["healthy"], 1.5]


class Client:
    def __init__(self, status):
        self._status = status

    def status(self):
        return Reply(200, self._status, None)


def _page(status, tab="overview") -> str:
    return handle_request(Request("GET", "/", {"tab": tab}), Client(status)).body.decode()


def _body(page: str) -> str:
    return page.split("</nav>")[-1]


# --- mark ------------------------------------------------------------------------------

@pytest.mark.parametrize(("level", "symbol", "cls"), [
    ("ok", "○", "st ok"), ("warn", "△", "st warn"), ("danger", "✕", "st danger"),
    ("neutral", "–", "st")])
def test_a_mark_always_carries_a_symbol_and_a_word(level, symbol, cls):
    assert marks.mark(level, "語") == f'<span class="{cls}">{symbol} 語</span>'


def test_a_mark_without_a_word_or_with_a_bad_level_is_unknown_not_normal():
    assert marks.mark("ok", "") == UNKNOWN_MARK
    assert marks.mark("fine", "x") == UNKNOWN_MARK
    assert marks.mark("unknown", "") == UNKNOWN_MARK


def test_a_mark_escapes_its_word():
    assert "<b>" not in marks.mark("ok", "<b>x</b>")


# --- 割当表 ----------------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "expected"), [
    ({"latched": False, "reconcile_required": False}, ("ok", "解除")),
    ({"latched": False, "reconcile_required": True}, ("warn", "照合待ち")),
    ({"latched": True, "reconcile_required": False}, ("danger", "作動中")),
    ({"latched": True}, ("danger", "作動中")),
    ({"latched": False}, ("unknown", "不明")),
    ({"latched": False, "reconcile_required": None}, ("unknown", "不明")),
    ({"latched": False, "reconcile_required": 0}, ("unknown", "不明")),
    ({"latched": 0, "reconcile_required": False}, ("unknown", "不明")),
    ({"latched": "false", "reconcile_required": False}, ("unknown", "不明")),
    ({"latched": None, "reconcile_required": False}, ("unknown", "不明")),
    ({}, ("unknown", "不明")), (None, ("unknown", "不明")), (True, ("unknown", "不明")),
    ([], ("unknown", "不明")), ("latched", ("unknown", "不明"))])
def test_kill_switch_levels(raw, expected):
    assert marks.classify_kill_switch(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [
    ({"latched": False, "failures": 0}, ("ok", "正常")),
    ({"latched": False, "failures": 2}, ("warn", "異常の記録 2 件")),
    ({"latched": True, "failures": 0}, ("warn", "異常の記録")),
    ({"latched": True, "failures": 3}, ("warn", "異常の記録 3 件")),
    ({"latched": True}, ("warn", "異常の記録")),
    ({"latched": False}, ("unknown", "不明")),
    ({"failures": 0}, ("unknown", "不明")),
    ({"latched": False, "failures": True}, ("unknown", "不明")),
    ({"latched": False, "failures": -1}, ("unknown", "不明")),
    ({"latched": False, "failures": "0"}, ("unknown", "不明")),
    ({"latched": False, "failures": 0.0}, ("unknown", "不明")),
    ({"latched": 0, "failures": 0}, ("unknown", "不明")),
    ({"latched": None, "failures": 0}, ("unknown", "不明")),
    ({"latched": "false", "failures": 0}, ("unknown", "不明")),
    ({}, ("unknown", "不明")), (None, ("unknown", "不明")), ([], ("unknown", "不明"))])
def test_health_levels(raw, expected):
    assert marks.classify_health(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [
    (True, ("warn", "ON")), (False, ("neutral", "OFF")),
    (None, ("unknown", "不明")), (1, ("unknown", "不明")), (0, ("unknown", "不明")),
    ("false", ("unknown", "不明")), ("true", ("unknown", "不明")), ([], ("unknown", "不明"))])
def test_autopilot_levels(raw, expected):
    assert marks.classify_autopilot(raw) == expected


@pytest.mark.parametrize(("table", "levels"), [
    (marks.DATA_STATES, {"ready": "ok", "degraded": "warn", "restricted": "warn"}),
    (marks.BOT_HEALTH, {"healthy": "ok", "stale": "warn", "unknown": "warn", "unhealthy": "danger",
                        "stalled": "danger", "permanent-failure": "danger",
                        "not_configured": "neutral"}),
    (marks.CALENDAR, {"fresh": "ok", "stale": "warn", "unknown": "neutral"}),
    (marks.WORKER, {"alive": "ok", "stopped": "danger", "start_failed": "danger",
                    "not_started": "neutral", "unknown": "neutral"}),
    (marks.CAPACITY, {"ok": "ok", "warn": "warn", "full": "danger"})])
def test_the_assignment_tables_are_exactly_the_agreed_ones(table, levels):
    assert dict(table) == levels
    for word, level in levels.items():
        assert marks.classify_word(table, word) == (level, word)


@pytest.mark.parametrize("table", [marks.DATA_STATES, marks.BOT_HEALTH, marks.CALENDAR,
                                   marks.WORKER, marks.CAPACITY])
@pytest.mark.parametrize("raw", JUNK + ["READY", " ready", "healthy "])
def test_a_word_outside_the_table_or_of_the_wrong_type_is_unknown(table, raw):
    if type(raw) is str and raw in table:
        pytest.skip("known")
    assert marks.classify_word(table, raw) == ("unknown", "不明")


@pytest.mark.parametrize("raw", JUNK)
def test_data_with_a_non_dict_or_a_bad_state_is_unknown(raw):
    assert marks.classify_data(raw) == ("unknown", "不明")
    assert marks.classify_data({"state": raw}) == ("unknown", "不明")
    assert marks.classify_data({}) == ("unknown", "不明")


def test_each_vocabulary_copy_in_the_renderer_matches_its_assignment_table():
    assert set(marks.BOT_HEALTH) == set(render.BOT_HEALTH_STATES)
    assert set(marks.CALENDAR) == set(render._CALENDAR_STATES)
    assert set(marks.WORKER) == set(render._WORKER_STATES)
    assert set(marks.CAPACITY) == set(render._LEVELS)


# --- cards -----------------------------------------------------------------------------

def test_cards_skip_empty_values_and_vanish_when_all_are_empty():
    assert marks.cards([("a", ""), ("b", "")]) == ""
    assert marks.cards([]) == ""
    assert marks.cards([("a", ""), ("b", "<i>1</i>")]) == '<div class="kpi"><div><strong>b</strong><i>1</i></div></div>'


def test_a_card_heading_is_escaped():
    assert "<script>" not in marks.cards([("<script>", "v")])


# --- 画面 ------------------------------------------------------------------------------

FULL = {"mode": "paper", "equity": 10000.5, "balance": 9990, "autopilot": False,
        "kill_switch": {"generation": 3, "latched": False, "reconcile_required": False},
        "health": {"latched": False, "failures": 0}, "data": {"state": "ready"}}


def test_the_status_overview_leads_with_the_cards_after_the_tz_line():
    body = _body(_page(FULL))
    assert body.startswith("<p>")
    after_tz = body.split("</p>", 1)[1]
    assert after_tz.startswith('<div class="kpi">')
    row = after_tz.split("</div></div>", 0)[0]
    for text in ("○ 解除", "○ 正常", "– OFF", "○ ready", "paper", "10000.5", "9990"):
        assert text in row
    assert after_tz.index('class="kpi"') < after_tz.index("<section>")


def test_the_status_overview_card_row_fits_its_byte_budget():
    worst = dict(FULL, health={"latched": True, "failures": 12}, autopilot=True,
                 kill_switch={"latched": False, "reconcile_required": True},
                 data={"state": "restricted"})
    for status in (FULL, worst):
        row = re.search(r'<div class="kpi">.*?</div></div>', _body(_page(status))).group(0)
        assert len(row.encode()) <= 512, len(row.encode())


@pytest.mark.parametrize(("status", "word"), [
    (dict(FULL, kill_switch={"latched": True}), "✕ 作動中"),
    (dict(FULL, kill_switch={"latched": False, "reconcile_required": True}), "△ 照合待ち"),
    (dict(FULL, health={"latched": False, "failures": 2}), "△ 異常の記録 2 件"),
    (dict(FULL, autopilot=True), "△ ON"),
    (dict(FULL, data={"state": "degraded"}), "△ degraded")])
def test_an_abnormal_status_is_marked_with_a_symbol_and_a_word(status, word):
    assert word in _body(_page(status))


@pytest.mark.parametrize("bad", JUNK)
def test_a_bad_status_value_is_unknown_and_never_normal(bad):
    for key in ("kill_switch", "health", "autopilot", "data"):
        body = _body(_page({"mode": "paper", key: bad}))
        card = re.search(rf"<strong>{key.replace('_', ' ')}</strong>(.*?)</div>", body)
        if card is None:  # 値を出さない型 (None・空) はカードも出ない
            continue
        if key == "autopilot" and bad is True:  # 真偽値そのものは分類できる (注意)
            assert "△ ON" in card.group(1)
            continue
        assert UNKNOWN_MARK in card.group(1), (key, bad)
        assert "○" not in card.group(1)


def test_a_status_without_a_field_has_no_card_for_it():
    body = _body(_page({"mode": "paper"}))
    assert "kill switch" not in body and "<strong>health</strong>" not in body


def test_a_value_card_for_a_non_scalar_is_not_shown():
    body = _body(_page({"mode": ["x"], "equity": {"a": 1}}))
    assert "<strong>mode</strong>" not in body and "<strong>equity</strong>" not in body


def test_the_dl_values_carry_no_mark_markup():
    for dl in re.findall(r"<dl>.*?</dl>", _body(_page(FULL))):
        assert "class=" not in dl


def test_card_values_are_escaped():
    body = _body(_page({"mode": "<script>x</script>"}))
    assert "<script>" not in body and "&lt;script&gt;" in body


BOT = {"state": "healthy", "alternate_push": "configured"}


@pytest.mark.parametrize(("state", "text"), [
    ("healthy", "○ healthy"), ("stale", "△ stale"), ("unknown", "△ unknown"),
    ("unhealthy", "✕ unhealthy"), ("stalled", "✕ stalled"),
    ("permanent-failure", "✕ permanent-failure"), ("not_configured", "– not_configured")])
def test_the_bot_card_marks_each_known_gateway_state(state, text):
    body = _body(_page({"mode": "paper", "bot_health": dict(BOT, state=state)}, "bot"))
    assert body.split("</p>", 1)[1].startswith('<div class="kpi"><div><strong>gateway health</strong>')
    assert text in body.split("<section>")[0]


@pytest.mark.parametrize("state", ["paused", None, 1, [], True])
def test_an_unrecognised_gateway_state_is_an_unknown_mark_but_the_word_stays_unknown(state):
    body = _body(_page({"mode": "paper", "bot_health": dict(BOT, state=state)}, "bot"))
    assert UNKNOWN_MARK in body.split("<section>")[0]
    assert "<dd>unknown</dd>" in body


def test_a_real_unknown_gateway_state_is_a_warning_not_an_unknown_mark():
    body = _body(_page({"mode": "paper", "bot_health": dict(BOT, state="unknown")}, "bot"))
    assert "△ unknown" in body and UNKNOWN_MARK not in body


def _econ_status(**over):
    econ = {"slice_worker": "alive", "slice_capacity": {"level": "ok"}}
    econ.update(over)
    return {"mode": "paper", "window": {"freshness": "fresh"}, "econ_signal": econ}


def test_the_economic_cards_come_before_the_sections_and_show_each_level():
    body = _body(_page(_econ_status(), "econ")).split("</p>", 1)[1]
    assert body.startswith('<div class="kpi">')
    head = body.split("<section>")[0]
    for text in ("<strong>calendar</strong>", "○ fresh", "<strong>worker</strong>", "○ alive", "○ ok"):
        assert text in head
    status = _econ_status(slice_worker="stopped", slice_capacity={"level": "full"})
    status["window"] = {"freshness": "stale"}
    head = _body(_page(status, "econ")).split("<section>")[0]
    assert "△ stale" in head and "✕ stopped" in head and "✕ full" in head


@pytest.mark.parametrize("bad", ["paused", None, 1, [], True])
def test_unrecognised_economic_words_are_unknown_marks(bad):
    status = _econ_status(slice_worker=bad, slice_capacity={"level": bad})
    status["window"] = {"freshness": bad}
    head = _body(_page(status, "econ")).split("<section>")[0]
    assert head.count(UNKNOWN_MARK) == 3 and "○" not in head


def test_a_real_unknown_calendar_and_worker_are_neutral_with_their_word():
    status = _econ_status(slice_worker="unknown")
    status["window"] = {"freshness": "unknown"}
    head = _body(_page(status, "econ")).split("<section>")[0]
    assert head.count('<span class="st">– unknown</span>') == 2


def test_the_reaction_tab_has_no_cards():
    body = _body(_page({"mode": "paper"}, "reaction"))
    assert 'class="kpi"' not in body


# --- plugin・improve・承認詳細の印とカード --------------------------------------------------

from afx_ops import plugin_text  # noqa: E402
from afx_ops.web import improve_pages  # noqa: E402


def test_the_plugin_improve_and_approval_tables_cover_exactly_the_words_the_screens_know():
    assert set(marks.DEPLOY) == set(plugin_text.ENTITY)
    assert set(marks.ADMISSION) == set(plugin_text.ADMISSION)
    assert set(marks.APPROVAL_STATUS) == set(plugin_text.APPROVAL_STATUS)
    assert set(marks.IMPROVE_RESULT) == set(improve_pages.RESULT)


def test_the_plugin_improve_and_approval_levels_are_the_agreed_ones():
    assert dict(marks.DEPLOY) == {"symlink": "ok", "plain": "ok", "unreadable": "danger",
                                  "absent": "neutral"}
    assert dict(marks.ADMISSION) == {"loaded": "ok", "excluded": "warn", "unapproved": "warn",
                                     "none": "neutral", "unknown": "neutral"}
    assert dict(marks.IMPROVE_RESULT) == {"submitted": "ok", "report": "ok", "gate_failed": "warn",
                                          "failed": "danger", "observation": "neutral"}
    assert dict(marks.APPROVAL_STATUS) == {"approved": "ok", "pending": "warn", "rejected": "neutral",
                                           "expired": "neutral", "invalidated": "neutral"}


@pytest.mark.parametrize("table", [marks.DEPLOY, marks.ADMISSION, marks.IMPROVE_RESULT,
                                   marks.APPROVAL_STATUS])
@pytest.mark.parametrize("raw", JUNK + ["SYMLINK", " ok"])
def test_a_word_outside_these_tables_or_of_the_wrong_type_has_no_level(table, raw):
    assert marks.level_of(table, raw) == "unknown"


def test_an_unknown_mark_keeps_the_given_word():
    assert marks.mark("unknown", "不明 (x)") == '<span class="st">? 不明 (x)</span>'


def test_the_renderer_vocabulary_is_derived_from_the_assignment_tables_in_the_same_order():
    assert render.BOT_HEALTH_STATES == tuple(marks.BOT_HEALTH)
    assert render._CALENDAR_STATES == tuple(marks.CALENDAR)
    assert render._WORKER_STATES == tuple(marks.WORKER)
    assert render._LEVELS == tuple(marks.CAPACITY)


def test_each_status_card_and_its_hidden_row_come_from_one_classification(monkeypatch):
    calls = []
    real = marks.classify_kill_switch
    monkeypatch.setattr(marks, "classify_kill_switch", lambda raw: calls.append(raw) or real(raw))
    _page(FULL)
    assert len(calls) == 1
