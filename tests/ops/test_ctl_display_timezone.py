"""ctl の時刻欄は応答の `display_timezone` で換算し、古い daemon (field なし) では UTC。"""
from __future__ import annotations

import pytest

from agentic_fx.ops import client
from agentic_fx.store.instance_lock import acquire_instance_lock

from ._api import make_api
from ._ctl import run_ctl
from .conftest import OPERATOR

TOKYO = "Asia/Tokyo"


@pytest.fixture
def tokyo_api(ops_env):
    log_path = ops_env.root / "logs" / "agentic.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    api_env = make_api(ops_env, service_pid=0, display_timezone=TOKYO,
                       service_kwargs={"log_path": log_path})
    lock = acquire_instance_lock(ops_env.root / "data")
    yield api_env
    for http in api_env.clients:
        http.close()
    api_env.server.stop(5.0)
    lock.close()


@pytest.fixture
def est_api(ops_env):
    api_env = make_api(ops_env, service_pid=0, display_timezone="EST")
    lock = acquire_instance_lock(ops_env.root / "data")
    yield api_env
    for http in api_env.clients:
        http.close()
    api_env.server.stop(5.0)
    lock.close()


def _old_daemon(monkeypatch):
    """応答の data から display_timezone を落とす (古い daemon の応答)。"""
    real = client.ApiClient.request

    def request(self, *args, **kwargs):
        reply = real(self, *args, **kwargs)
        if isinstance(reply.data, dict):
            reply.data.pop("display_timezone", None)
        return reply

    monkeypatch.setattr(client.ApiClient, "request", request)


# ---- API の応答 ---------------------------------------------------------------

def test_every_success_response_carries_the_display_timezone(tokyo_api):
    http = tokyo_api.client(OPERATOR)
    status = http.get("/v1/status")
    assert status.status_code == 200
    assert status.json()["data"]["display_timezone"] == TOKYO
    posted = http.post("/v1/improve/runs", json={}, headers={"Idempotency-Key": "tz-1"})
    assert posted.status_code in (200, 202)
    assert posted.json()["data"]["display_timezone"] == TOKYO
    job = http.get(f"/v1/jobs/{posted.json()['data']['job_id']}")
    assert job.json()["data"]["display_timezone"] == TOKYO
    events = http.get("/v1/events", params={"after": 0, "limit": 10})
    assert events.json()["data"]["display_timezone"] == TOKYO


def test_error_envelopes_are_unchanged(tokyo_api):
    missing = tokyo_api.client(OPERATOR).get("/v1/no-such-route")
    assert missing.status_code == 404
    assert missing.json()["ok"] is False
    assert "data" not in missing.json()


def test_the_default_display_timezone_is_utc(ops_env):
    api_env = make_api(ops_env, service_pid=0)
    try:
        data = api_env.client(OPERATOR).get("/v1/status").json()["data"]
        assert data["display_timezone"] == "UTC"
    finally:
        for http in api_env.clients:
            http.close()
        api_env.server.stop(5.0)


# ---- log / activity -----------------------------------------------------------

_ACTIVITY = (
    "2026-11-06T10:04:05+00:00\tNEWS\tnews_fetch_skipped\t"
    "前回の取得から間もないので次回は 2026-11-06T10:30:00Z\t-\n"
    "2026-11-06T10:05:00+00:00\tAGGREGATE\tdecision\t"
    "2026-11-06T10:00:00Z 時点の判断\t-\n"
    "Traceback (most recent call last): 2026-11-06T10:00:00Z\n"
    "2026-11-06T10:06:00+00:00\tSYSTEM\tfake\ta\tb\tc\n")


def test_ctl_activity_converts_the_head_and_only_builder_bodies(tokyo_api):
    env = tokyo_api.env
    env.activity_path.parent.mkdir(parents=True, exist_ok=True)
    env.activity_path.write_text(_ACTIVITY, encoding="utf-8")
    rc, out, err = run_ctl(env.root, ["activity", "10"])
    assert rc == 0, err
    assert out.splitlines() == [
        "表示 tz: Asia/Tokyo (+09:00)",
        "2026-11-06 19:04:05 +09:00 NEWS news_fetch_skipped "
        "前回の取得から間もないので次回は 2026-11-06 19:30:00 +09:00 -",
        "2026-11-06 19:05:00 +09:00 AGGREGATE decision 2026-11-06T10:00:00Z 時点の判断 -",
        "Traceback (most recent call last): 2026-11-06T10:00:00Z",
        "2026-11-06T10:06:00+00:00 SYSTEM fake a b c",
    ]


def test_the_activity_file_itself_is_not_rewritten(tokyo_api):
    env = tokyo_api.env
    env.activity_path.parent.mkdir(parents=True, exist_ok=True)
    env.activity_path.write_text(_ACTIVITY, encoding="utf-8")
    run_ctl(env.root, ["activity", "10"])
    assert env.activity_path.read_text(encoding="utf-8") == _ACTIVITY


def test_ctl_log_keeps_the_milliseconds(tokyo_api):
    log_path = tokyo_api.env.root / "logs" / "agentic.log"
    log_path.write_text(
        "2026-11-06 10:14:23,090 UTC INFO agentic_fx.scheduler: tick 2026-11-06T10:00:00Z\n"
        "  File \"x.py\", line 1\n", encoding="utf-8")
    rc, out, err = run_ctl(tokyo_api.env.root, ["log", "10"])
    assert rc == 0, err
    assert out.splitlines() == [
        "表示 tz: Asia/Tokyo (+09:00)",
        "2026-11-06 19:14:23,090 +09:00 INFO agentic_fx.scheduler: tick 2026-11-06T10:00:00Z",
        "File \"x.py\", line 1",
    ]


def test_a_control_character_in_a_line_is_still_removed(tokyo_api):
    env = tokyo_api.env
    env.activity_path.parent.mkdir(parents=True, exist_ok=True)
    env.activity_path.write_text(
        "2026-11-06T10:04:05+00:00\tNEWS\tnews_fetch_skipped\t\x1b[31mred\x1b[0m\t-\n",
        encoding="utf-8")
    rc, out, _ = run_ctl(env.root, ["activity", "5"])
    assert rc == 0
    assert "\x1b" not in out
    assert "2026-11-06 19:04:05 +09:00 NEWS news_fetch_skipped" in out


# ---- status / approvals -------------------------------------------------------

def test_ctl_status_starts_with_the_zone_and_offset(tokyo_api):
    rc, out, err = run_ctl(tokyo_api.env.root, ["status"])
    assert rc == 0, err
    lines = out.splitlines()
    assert lines[0] == "表示 tz: Asia/Tokyo (+09:00)"
    assert lines[1] == "mode: learning / autopilot: off / kill switch: ok"


def test_ctl_status_warns_when_the_zone_is_an_abbreviation_and_keeps_its_fixed_offset(est_api):
    env = est_api.env
    approval_id = env.make_plugin_approval()
    rc, status, err = run_ctl(env.root, ["status"])
    assert rc == 0, err
    lines = status.splitlines()
    assert lines[0] == "表示 tz: EST (-05:00)"
    assert lines[1].startswith("警告: display_timezone 'EST' は地域名ではありません。")
    assert "America/New_York" in lines[1]
    assert lines[2] == "mode: learning / autopilot: off / kill switch: ok"
    # 2026-10-05 は夏時間の期間だが、略称の EST は固定の -05:00 で換算される
    _, listing, _ = run_ctl(env.root, ["approval", "list"])
    assert f"#{approval_id} kind=plugin name=sma created_at=2026-10-04 19:00:00 -05:00 " in listing


def test_a_region_name_or_a_missing_field_gives_no_zone_warning(tokyo_api, monkeypatch):
    _, status, _ = run_ctl(tokyo_api.env.root, ["status"])
    assert "警告" not in status
    _old_daemon(monkeypatch)
    _, old, _ = run_ctl(tokyo_api.env.root, ["status"])
    assert "警告" not in old


def test_the_zone_warning_is_sanitised():
    text = client._region_warning({"display_timezone": "E\x1b[31mST\nspoof"})
    assert "\x1b" not in text and "\n" not in text
    assert text.startswith("警告: ")


def test_ctl_approval_list_shows_the_creation_time_in_the_display_zone(tokyo_api):
    env = tokyo_api.env
    approval_id = env.make_plugin_approval()
    rc, out, _ = run_ctl(env.root, ["approval", "list"])
    assert rc == 0
    assert f"#{approval_id} kind=plugin name=sma created_at=2026-10-05 09:00:00 +09:00 " in out


def test_an_old_daemon_without_the_field_is_shown_in_utc(tokyo_api, monkeypatch):
    env = tokyo_api.env
    approval_id = env.make_plugin_approval()
    env.activity_path.parent.mkdir(parents=True, exist_ok=True)
    env.activity_path.write_text(_ACTIVITY, encoding="utf-8")
    _old_daemon(monkeypatch)
    _, status, _ = run_ctl(env.root, ["status"])
    assert status.splitlines()[0] == "表示 tz: UTC (+00:00)"
    _, listing, _ = run_ctl(env.root, ["approval", "list"])
    assert f"#{approval_id} kind=plugin name=sma created_at=2026-10-05 00:00:00 +00:00 " in listing
    _, activity, _ = run_ctl(env.root, ["activity", "10"])
    assert activity.splitlines()[1].startswith("2026-11-06 10:04:05 +00:00 NEWS news_fetch_skipped")
    assert "次回は 2026-11-06 10:30:00 +00:00" in activity


@pytest.mark.parametrize("bad", ["Not/AZone", "", None, 7, "A" * 300, "../../etc/passwd"])
def test_an_unusable_zone_in_the_response_falls_back_to_utc(bad):
    data = {"display_timezone": bad, "approvals": [
        {"id": 1, "kind": "plugin", "name": "x", "created_at": "2026-11-06T10:04:05+00:00",
         "content_hash": "-"}]}
    text = client.format_approval_list(data)
    assert "created_at=2026-11-06 10:04:05 +00:00" in text


# ---- 部品の組み合わせ (表示の関数) ----------------------------------------------

def test_status_times_follow_the_response_zone():
    data = {"display_timezone": TOKYO,
            "kill_switch": {"latched": True, "generation": 3,
                            "latched_at": "2026-11-06T10:04:05+00:00"},
            "mission": {"loop": "trade", "status": "ok",
                        "started_at": "2026-11-06T01:00:00+00:00"}}
    text = client.format_status(data)
    assert "since=2026-11-06 19:04:05 +09:00" in text
    assert "trade:ok (2026-11-06 10:00:00 +09:00)" in text


def test_summer_time_zones_use_the_offset_in_force_at_that_instant():
    data = {"display_timezone": "America/New_York", "approvals": [
        {"id": 1, "kind": "k", "name": "n", "created_at": "2026-07-01T12:00:00+00:00"},
        {"id": 2, "kind": "k", "name": "n", "created_at": "2026-12-01T12:00:00+00:00"}]}
    text = client.format_approval_list(data)
    assert "created_at=2026-07-01 08:00:00 -04:00" in text
    assert "created_at=2026-12-01 07:00:00 -05:00" in text


def test_event_ts_and_every_declared_time_field_are_converted_and_others_are_not():
    event = {"id": 5, "ts": "2026-11-06T10:04:05+00:00", "code": "stream_health", "ref": "-",
             "fields": {"first_failed_at": "2026-11-06T09:00:00+00:00",
                        "last_failed_at": "2026-11-06T09:30:00+00:00",
                        "reason": "2026-11-06T09:00:00+00:00", "count": 3}}
    text = client.format_event(event, TOKYO)
    assert text == ("#5 2026-11-06 19:04:05 +09:00 stream_health - "
                    "count=3 first_failed_at=2026-11-06 18:00:00 +09:00 "
                    "last_failed_at=2026-11-06 18:30:00 +09:00 "
                    "reason=2026-11-06T09:00:00+00:00")


def test_a_new_name_in_the_declared_set_is_converted_without_extra_wiring(monkeypatch):
    from agentic_fx.core import display_time
    monkeypatch.setattr(client, "TIME_FIELDS", display_time.TIME_FIELDS | {"brand_new_at"})
    event = {"id": 1, "ts": "2026-11-06T10:04:05+00:00", "code": "c", "ref": "-",
             "fields": {"brand_new_at": "2026-11-06T10:00:00+00:00"}}
    assert "brand_new_at=2026-11-06 19:00:00 +09:00" in client.format_event(event, TOKYO)


def test_job_result_cells_use_the_declared_time_fields():
    job = {"id": "a" * 32, "kind": "other", "state": "done", "display_timezone": TOKYO,
           "result": {"deadline_at": "2026-11-06T10:04:05+00:00", "mission_id": 4,
                      "note": "2026-11-06T10:04:05+00:00"}}
    text = client.Runner._job_line(job)
    assert "deadline_at=2026-11-06 19:04:05 +09:00" in text
    assert "mission_id=4" in text
    assert "note=2026-11-06T10:04:05+00:00" in text


def test_approval_detail_decision_time_and_change_time_are_converted():
    data = {"id": 1, "kind": "plugin", "status": "approved", "display_timezone": TOKYO,
            "decided_by": "operator", "decided_at": "2026-11-06T10:04:05+00:00",
            "change": {"classification": "new", "name": "sma", "kind": "indicator",
                       "computed_at": "2026-11-06T10:00:00+00:00"}}
    text = client.format_approval_detail(data)
    assert "decided_at=2026-11-06 19:04:05 +09:00" in text
    assert "計算時刻: 2026-11-06 19:00:00 +09:00" in text


def test_sanitising_comes_before_converting_and_the_limit_comes_last():
    hostile = "2026-11-06T10:04:05+00:00\x1b[31m"
    shown = client._when(hostile, TOKYO)
    assert "\x1b" not in shown
    assert shown == "2026-11-06T10:04:05+00:00[31m"
    assert client._when("2026-11-06T10:04:05+00:00", TOKYO, limit=10) == "2026-11-06…"
    assert client._when(None, TOKYO) == "-"
    assert client._when("2026-11-06T10:04:05", TOKYO) == "2026-11-06T10:04:05"


# ---- 期限 ----------------------------------------------------------------------

@pytest.mark.parametrize(("value", "tz", "expected"), [
    ("2026-11-06T10:04:05+00:00", "UTC", "2026-11-06 10:04 +00:00"),
    ("2026-11-06T10:04:59+00:00", TOKYO, "2026-11-06 19:04 +09:00"),
    ("2026-11-06T10:04:05", TOKYO, "2026-11-06 19:04 +09:00"),
    ("2026-11-06T10:04:05Z", "America/New_York", "2026-11-06 05:04 -05:00"),
    ("9999-12-31T23:59:59+00:00", TOKYO, "9999-12-31 23:59 +00:00"),
    ("garbage", TOKYO, "garbage"),
])
def test_format_deadline(value, tz, expected):
    assert client._format_deadline(value, tz) == expected


def test_format_deadline_keeps_the_utc_default_and_non_strings():
    assert client._format_deadline("2026-11-06T10:04:05+00:00") == "2026-11-06 10:04 +00:00"
    assert client._format_deadline(None) == "-"


# ---- 時刻表示の網羅 -------------------------------------------------------------

def _stamp(minute: int) -> str:
    return f"2026-11-06T10:{minute:02d}:00+00:00"


def _local(minute: int) -> str:
    return f"2026-11-06 19:{minute:02d}:00 +09:00"


def test_every_displayed_time_field_shows_its_own_label_and_converted_value():
    # 欄ごとに別の分を使うので、取り違えや欄の消失は値の不一致になる。
    status = client.format_status({
        "display_timezone": TOKYO,
        "kill_switch": {"latched": True, "generation": 1, "latched_at": _stamp(1)},
        "mission": {"loop": "trade", "status": "ok", "started_at": _stamp(2)}})
    assert f"since={_local(1)}" in status
    assert f"trade:ok ({_local(2)})" in status

    approvals = client.format_approval_list({"display_timezone": TOKYO, "approvals": [
        {"id": 1, "kind": "plugin", "name": "n", "created_at": _stamp(3)}]})
    assert f"created_at={_local(3)}" in approvals

    detail = client.format_approval_detail({
        "display_timezone": TOKYO, "id": 1, "kind": "plugin", "status": "approved",
        "decided_at": _stamp(4),
        "change": {"classification": "new", "computed_at": _stamp(5)}})
    assert f"decided_at={_local(4)}" in detail
    assert f"計算時刻: {_local(5)}" in detail

    event = client.format_event({"id": 1, "ts": _stamp(6), "code": "c", "ref": "-", "fields": {
        "first_failed_at": _stamp(7), "last_failed_at": _stamp(8), "until": _stamp(9),
        "since": _stamp(10), "deadline": _stamp(11)}}, TOKYO)
    assert f"#1 {_local(6)} c -" in event
    for label, minute in (("first_failed_at", 7), ("last_failed_at", 8), ("until", 9),
                          ("since", 10), ("deadline", 11)):
        assert f"{label}={_local(minute)}" in event

    job = client.Runner._job_line({"id": "j", "kind": "x", "state": "done",
                                   "display_timezone": TOKYO,
                                   "result": {"deadline_at": _stamp(12), "started_at": _stamp(13),
                                              "expires_at": _stamp(14)}})
    for label, minute in (("deadline_at", 12), ("started_at", 13), ("expires_at", 14)):
        assert f"{label}={_local(minute)}" in job


def test_dto_times_that_no_renderer_shows_are_not_shown_in_any_form():
    # 表示の網羅の証拠ではなく、「表示されない」ことの確認。
    status = client.format_status({
        "display_timezone": TOKYO,
        "kill_switch": {"latched": True, "generation": 1, "latched_at": _stamp(1),
                        "started_at": _stamp(20)},
        "gaps": [{"pair": "USDJPY", "since": _stamp(21)}],
        "setup": {"record_state": "ok",
                  "pending_steps": [{"code": "x", "observed_at": _stamp(22)}]}})
    detail = client.format_approval_detail({
        "display_timezone": TOKYO, "id": 1, "kind": "plugin", "status": "approved",
        "expires_at": _stamp(23)})
    for minute in (20, 21, 22, 23):
        for text in (status, detail):
            assert f":{minute:02d}:00" not in text, text
