"""web は描画する応答の display_timezone で時刻を換算し、全ページの上部に tz を出す。"""
from __future__ import annotations

import html
import re

import pytest

from afx_ops import display_time, display_zone
from afx_ops.api_client import Reply
from afx_ops.web import render
from afx_ops.web.request import Request, handle_request

TOKYO = "Asia/Tokyo"


@pytest.fixture(autouse=True)
def _fresh_zone_state(monkeypatch):
    monkeypatch.setattr(display_zone, "_warned", False)
    display_time.clear_zone_cache()
    yield
    display_time.clear_zone_cache()


def _status(tz=TOKYO, **extra):
    data = {"mode": "paper",
            "kill_switch": {"latched": True, "latched_at": "2026-11-06T10:04:05+00:00",
                            "generation": 2},
            "mission": {"state": "running", "started_at": "2026-11-06T10:11:00+00:00"},
            "jobs": [{"id": 1, "state": "running", "started_at": "2026-11-06T10:22:00+00:00"},
                     {"id": 2, "state": "done", "started_at": "2026-11-06T10:33:00+00:00"}],
            "econ_signal": {"slice_capacity": {
                "slice_bytes_measured_at": "2026-11-06T10:44:00+00:00"}}}
    if tz is not None:
        data["display_timezone"] = tz
    data.update(extra)
    return data


class Client:
    def __init__(self, **replies):
        self.replies = replies

    def status(self):
        return Reply(200, self.replies["status"], None)

    def approvals(self, *, limit):
        return Reply(200, self.replies["approvals"], None)

    def approval(self, approval_id):
        return Reply(200, self.replies["approval"], None)

    def approval_change(self, approval_id):
        return Reply(200, self.replies["change"], None)

    def log(self, *, n):
        return Reply(200, self.replies["log"], None)

    def activity(self, *, n, category):
        return Reply(200, self.replies["activity"], None)


def _get(client, path):
    response = handle_request(Request("GET", path, {}), client)
    assert response.status == 200
    return response.body.decode()


def test_every_status_time_at_every_depth_is_converted_to_the_zone_of_the_response():
    text = _get(Client(status=_status()), "/")
    for shown in ("2026-11-06 19:04:05 +09:00", "2026-11-06 19:11:00 +09:00",
                  "2026-11-06 19:22:00 +09:00", "2026-11-06 19:33:00 +09:00",
                  "2026-11-06 19:44:00 +09:00"):
        assert shown in text
    assert "T10:" not in text


def test_the_offset_follows_daylight_saving_time_of_each_instant():
    summer = _get(Client(status=_status("America/New_York", mission={
        "started_at": "2026-07-01T00:00:00+00:00"})), "/")
    winter = _get(Client(status=_status("America/New_York", mission={
        "started_at": "2026-12-01T00:00:00+00:00"})), "/")
    assert "2026-06-30 20:00:00 -04:00" in summer
    assert "2026-11-30 19:00:00 -05:00" in winter


def test_a_response_without_the_field_is_shown_in_utc_without_logging(capsys):
    text = _get(Client(status=_status(None)), "/")
    assert "2026-11-06 10:04:05 +00:00" in text
    assert "<p>表示 tz: UTC (+00:00)</p>" in text
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("bad", ["Not/AZone", "A" * 300, "", None, 5, ["Asia/Tokyo"],
                                 "<script>x</script>", "Asia/Tokyo ", "../../etc/passwd"])
def test_an_unusable_zone_falls_back_to_utc_and_the_wire_value_never_reaches_page_or_log(
        bad, capsys):
    text = _get(Client(status=_status(display_timezone=bad)), "/")
    assert "2026-11-06 10:04:05 +00:00" in text
    assert "<p>表示 tz: UTC (+00:00)</p>" in text
    if isinstance(bad, str) and len(bad) > 3:
        assert bad not in text and html.escape(bad) not in text
    err = capsys.readouterr().err
    assert err == "afx-ops: display_timezone_unusable (UTC で表示)\n"


def test_the_fallback_is_logged_once_per_process(capsys):
    client = Client(status=_status("Not/AZone"))
    _get(client, "/")
    _get(client, "/")
    assert capsys.readouterr().err.count("display_timezone_unusable") == 1


def test_a_zone_the_environment_cannot_read_is_shown_in_utc_and_logged_once(monkeypatch, capsys):
    def unreadable(name):
        raise OSError("no data")

    monkeypatch.setattr(display_time, "ZoneInfo", unreadable)
    display_time.clear_zone_cache()
    text = _get(Client(status=_status()), "/")
    assert "2026-11-06 10:04:05 +00:00" in text and "表示 tz: UTC (+00:00)" in text
    assert capsys.readouterr().err.count("display_timezone_unusable") == 1


@pytest.mark.parametrize("path,key", [("/", "status"), ("/approvals", "approvals"),
                                       ("/logs", "log"), ("/activity", "activity")])
def test_every_page_has_the_zone_line_at_the_top(path, key):
    replies = {"status": _status(), "approvals": {"approvals": [], "display_timezone": TOKYO},
               "log": {"lines": [], "display_timezone": TOKYO},
               "activity": {"lines": [], "display_timezone": TOKYO}}
    text = _get(Client(**replies), path)
    head = text.split("</nav>", 1)[1]
    assert re.match(r"<h1>[^<]*</h1><p>表示 tz: Asia/Tokyo \(\+09:00\)</p>", head)


def test_the_approval_pages_have_the_zone_line_and_a_converted_created_at():
    listing = {"display_timezone": TOKYO, "approvals": [
        {"id": 4, "kind": "plugin", "name": "p", "content_hash": "abc",
         "created_at": "2026-11-06T10:55:00+00:00"}]}
    text = _get(Client(approvals=listing), "/approvals")
    assert "<dd>2026-11-06 19:55:00 +09:00</dd>" in text
    assert "<p>表示 tz: Asia/Tokyo (+09:00)</p>" in text


def _detail(tz=TOKYO):
    detail = {"id": 7, "kind": "plugin", "status": "approved", "payload_sha256": "cd" * 32,
              "decided_at": "2026-11-06T10:05:00+00:00"}
    if tz is not None:
        detail["display_timezone"] = tz
    return detail


def _change(computed="2026-11-06T10:06:00+00:00", summary="LLM 要約 2026-11-06T10:07:00Z"):
    return {"name": "sma_cross", "kind": "strategy", "classification": "updated",
            "candidate_hash": "a1" * 32, "baseline_hash": "b2" * 32, "computed_at": computed,
            "reason": None, "params": {"items": [], "omitted": 0, "invalid_keys": 0,
                                       "flatten_omitted": 0, "reason": None},
            "performance": {"in_sample": [], "holdout": []}, "summary": summary}


def _approval_text(tz=TOKYO, **change):
    change_data = {"id": 7, "change": _change(**change),
                   "claims": {"summary": "要約 2026-11-06T10:07:00Z",
                              "selection_rationale": "根拠 2026-11-06T10:08:00+00:00"},
                   "diff": {"files": [], "omitted_lines": 0}, "display_timezone": tz}
    return _get(Client(approval=_detail(tz), change=change_data), "/approvals/7")


def test_the_approval_page_converts_decided_at_and_the_hand_written_computed_time_row():
    text = _approval_text()
    assert "<dt>decided_at</dt><dd>2026-11-06 19:05:00 +09:00</dd>" in text
    assert "<dt>計算時刻</dt><dd>2026-11-06 19:06:00 +09:00</dd>" in text
    assert "<p>表示 tz: Asia/Tokyo (+09:00)</p>" in text


def test_llm_text_and_identifiers_are_not_converted():
    text = _approval_text()
    assert "要約 2026-11-06T10:07:00Z" in text
    assert "根拠 2026-11-06T10:08:00+00:00" in text
    assert "a1a1a1a1a1a1" in text


def test_an_unreadable_computed_time_stays_as_written_and_does_not_fail_the_page():
    assert "<dt>計算時刻</dt><dd>garbage</dd>" in _approval_text(computed="garbage")
    assert "<dt>計算時刻</dt><dd>2026-11-06T10:06:00</dd>" in _approval_text(
        computed="2026-11-06T10:06:00")


def test_the_approval_page_without_a_zone_field_is_in_utc():
    text = _approval_text(None)
    assert "<dt>計算時刻</dt><dd>2026-11-06 10:06:00 +00:00</dd>" in text
    assert "<dd>2026-11-06 10:05:00 +00:00</dd>" in text
    assert "<p>表示 tz: UTC (+00:00)</p>" in text


def test_a_time_field_that_is_not_a_time_is_shown_as_written_and_escaped():
    data = _status(mission={"started_at": "<b>soon</b>"})
    text = _get(Client(status=data), "/")
    assert "&lt;b&gt;soon&lt;/b&gt;" in text and "<b>" not in text


def test_a_time_field_added_to_the_set_is_converted_without_any_other_wiring(monkeypatch):
    shown = render._pairs({"zz_stamp": "2026-11-06T10:00:00+00:00"}, frozenset({"zz_stamp"}),
                          tz=TOKYO)
    assert "2026-11-06T10:00:00" in shown
    monkeypatch.setattr(display_time, "TIME_FIELDS",
                        display_time.TIME_FIELDS | {"zz_stamp"})
    shown = render._pairs({"zz_stamp": "2026-11-06T10:00:00+00:00"}, frozenset({"zz_stamp"}),
                          tz=TOKYO)
    assert "2026-11-06 19:00:00 +09:00" in shown


def test_every_time_like_name_the_web_can_show_is_declared_in_the_time_field_set():
    shown = (render.STATUS_FIELDS | render.APPROVAL_FIELDS | render.JOURNAL_FIELDS
             | render.DETAIL_FIELDS | render.ACTIVITY_FIELDS | render.NESTED_FIELDS
             | set(render._CAPACITY_KEYS))
    time_like = {name for name in shown if re.search(r"_at$|^ts$|deadline|until|since", name)}
    assert time_like  # 空振りでないこと
    assert time_like <= display_time.TIME_FIELDS


_ACTIVITY = "2026-11-06T10:09:00+00:00\tNEWS\tnews_fetch_skipped\t次回は 2026-11-06T10:30:00Z\tref"
_LLM = "2026-11-06T10:10:00+00:00\tAGGREGATE\tdecision\t判断 2026-11-06T10:30:00Z まで様子見\tref"


def test_activity_rows_are_converted_at_the_head_and_in_builder_bodies_only():
    reply = {"display_timezone": TOKYO,
             "lines": [_ACTIVITY, _LLM, "2026-11-06T10:11:00+00:00 not a row",
                       "  File x\t2026-11-06T10:12:00+00:00"]}
    text = _get(Client(activity=reply), "/activity")
    assert "2026-11-06 19:09:00 +09:00 NEWS news_fetch_skipped 次回は 2026-11-06 19:30:00 +09:00 ref" in text
    assert "2026-11-06 19:10:00 +09:00 AGGREGATE decision 判断 2026-11-06T10:30:00Z まで" in text
    assert "2026-11-06T10:11:00+00:00 not a row" in text
    assert "File x 2026-11-06T10:12:00+00:00" in text


def test_log_rows_are_converted_with_milliseconds_and_other_rows_are_not():
    reply = {"display_timezone": TOKYO,
             "lines": ["2026-11-06 10:14:23,090 UTC INFO agentic_fx.x: started 10:14 UTC",
                       "Traceback 2026-11-06 10:14:23,090 UTC INFO x: y"]}
    text = _get(Client(log=reply), "/logs")
    assert "2026-11-06 19:14:23,090 +09:00 INFO agentic_fx.x: started 10:14 UTC" in text
    assert "Traceback 2026-11-06 10:14:23,090 UTC INFO x: y" in text


def test_log_and_activity_without_the_field_are_converted_in_utc():
    text = _get(Client(activity={"lines": [_ACTIVITY]}), "/activity")
    assert "2026-11-06 10:09:00 +00:00 NEWS news_fetch_skipped 次回は 2026-11-06 10:30:00 +00:00 ref" in text
    assert "<p>表示 tz: UTC (+00:00)</p>" in text


def test_error_pages_carry_no_zone_line():
    assert "表示 tz" not in render.render_error(404).decode()


def test_a_time_the_zone_cannot_convert_is_shown_in_utc_instead_of_as_written():
    # 19 世紀の日付は地方平均時の offset が分で割り切れず、その zone では換算できない
    data = _status("Europe/Amsterdam", mission={"started_at": "1850-01-01T00:00:00+00:00"})
    assert "1850-01-01 00:00:00 +00:00" in _get(Client(status=data), "/")
    assert "1850-01-01 00:00:00 +00:00" in _approval_text(
        "Europe/Amsterdam", computed="1850-01-01T00:00:00+00:00")
