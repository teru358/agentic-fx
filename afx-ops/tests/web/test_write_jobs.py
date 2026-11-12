"""job の閲覧と取消: 閲覧は GET だけで固定語の画面、取消は確認の 2 段を通り、write=false では存在しない。"""
from __future__ import annotations

import pytest

from afx_ops.api_client import Reply
from afx_ops.web import write

from _write_support import JOB, Rig, api_error, token_of

OTHER_JOB = "f" * 32
VIEW = f"/jobs/{JOB}"
CANCEL = f"/jobs/{JOB}/cancel"


def _rig() -> Rig:
    return Rig()


# --- write=false では存在しない ---------------------------------------------------------------

@pytest.mark.parametrize("path", [VIEW, CANCEL])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_with_write_off_the_job_paths_are_404_and_the_daemon_is_not_called(path, method):
    rig = _rig()
    response = rig.send(rig.request(method, path, {} if method == "POST" else None),
                        writable=False)
    assert response.status == 404 and rig.client.calls == []


# --- route ------------------------------------------------------------------------------------

def test_the_job_routes_are_exactly_a_view_and_a_cancel_for_a_32_hex_id():
    view = write.resolve_write_route(VIEW)
    cancel = write.resolve_write_route(CANCEL)
    assert (view.kind, view.methods, view.job_id) == ("job_view", ("GET", "HEAD"), JOB)
    assert (cancel.kind, cancel.methods, cancel.job_id) == ("cancel", ("POST",), JOB)
    for path in ["/jobs", "/jobs/", f"/jobs/{JOB}/", f"/jobs/{JOB.upper()}", "/jobs/abc",
                 f"/jobs/{JOB}0", f"/jobs/{JOB}/cancel/x", f"/jobs/{JOB}/retry",
                 f"/jobs/{JOB[:-1]}"]:
        assert write.resolve_write_route(path) is None, path


def test_a_wrong_method_on_a_job_route_is_405_with_its_allow_list():
    rig = _rig()
    assert rig.send(rig.request("POST", VIEW, {})).headers["Allow"] == "GET, HEAD"
    assert rig.send(rig.request("GET", CANCEL)).headers["Allow"] == "POST"
    assert rig.send(rig.request("PUT", CANCEL, {})).status == 405
    assert rig.client.calls == []


# --- 閲覧 -------------------------------------------------------------------------------------

def test_the_view_asks_the_daemon_with_the_configured_user_as_the_actor():
    rig = _rig()
    response = rig.send(rig.request("GET", VIEW))
    assert response.status == 200
    assert rig.client.calls == [("job", "web:afx", JOB)]
    body = response.body.decode("utf-8")
    assert JOB in body and "実行中" in body and f'action="{CANCEL}"' not in body
    assert 'name="token"' not in body


def test_the_actor_is_never_taken_from_the_request():
    rig = _rig()
    request = rig.request("GET", VIEW, extra=(("X-Afx-Asserted-Actor", "web:mallory"),))
    assert rig.send(request).status == 400
    assert rig.client.calls == []
    request = rig.request("GET", VIEW)
    request = type(request)(request.method, request.path, {"actor": "web:mallory"},
                            request.headers, request.body)
    assert rig.send(request).status == 400
    assert rig.client.calls == []


def test_head_has_no_body_and_a_non_loopback_context_refuses_the_view():
    rig = _rig()
    head = rig.send(rig.request("HEAD", VIEW))
    assert head.status == 200 and head.body == b""
    lan = Rig(loopback=False)
    assert lan.send(lan.request("GET", VIEW)).status == 403
    assert lan.client.calls == []


@pytest.mark.parametrize("state,result,button", [
    ("queued", None, True), ("queued", {"stop_requested": False}, True),
    ("running", {"stop_requested": False}, True),
    ("running", None, False), ("running", {}, False), ("running", {"runs": 1}, False),
    ("running", {"stop_requested": "no"}, False),
    ("done", {"stop_requested": False}, False), ("failed", None, False),
    ("cancelled", None, False), ("shutdown", None, False)])
def test_only_a_job_the_daemon_can_still_cancel_offers_the_cancel_form(state, result, button):
    """daemon が取り消せるのは、待機中の job と、停止を要求できる繰り返しの実行中の job だけ。"""
    rig = _rig()
    rig.client.job_reply = Reply(200, {"id": JOB, "kind": "improve", "state": state,
                                       "result": result, "result_code": None}, None)
    body = rig.send(rig.request("GET", VIEW)).body.decode("utf-8")
    assert (f'action="{CANCEL}"' in body) is button


def test_a_stop_already_requested_shows_it_and_offers_no_second_cancel():
    rig = _rig()
    rig.client.job_reply = Reply(200, {"id": JOB, "kind": "improve", "state": "running",
                                       "result": {"stop_requested": True}}, None)
    body = rig.send(rig.request("GET", VIEW)).body.decode("utf-8")
    assert "あり" in body and "<form" not in body


def test_the_view_shows_only_fixed_words_from_the_daemon_reply():
    rig = _rig()
    rig.client.job_reply = Reply(200, {
        "id": JOB, "kind": "<script>x</script>", "state": "running",
        "result": {"note": "SECRET-FREE-TEXT", "stop_requested": False},
        "result_code": "free text <b>"}, None)
    body = rig.send(rig.request("GET", VIEW)).body.decode("utf-8")
    assert "SECRET-FREE-TEXT" not in body and "<script>" not in body and "free text" not in body


@pytest.mark.parametrize("data", [
    {"id": OTHER_JOB, "kind": "improve", "state": "running"},
    {"id": JOB, "kind": "improve", "state": "<b>weird</b>"},
    {"id": JOB, "kind": "improve"},
])
def test_a_reply_that_does_not_match_the_job_is_a_502(data):
    rig = _rig()
    rig.client.job_reply = Reply(200, data, None)
    assert rig.send(rig.request("GET", VIEW)).status == 502


@pytest.mark.parametrize("status,code,text", [(403, "forbidden", "許可しませんでした"),
                                             (404, "not_found", "見つかりません"),
                                             (403, "scope_denied", "見せませんでした"),
                                             (404, None, "見せませんでした")])
def test_a_job_the_daemon_refuses_is_a_fixed_screen_without_the_daemon_words(status, code, text):
    rig = _rig()
    rig.client.job_reply = Reply(status, None, code)
    response = rig.send(rig.request("GET", VIEW))
    assert response.status == status
    body = response.body.decode("utf-8")
    assert text in body and "scope_denied" not in body and "forbidden" not in body and "web:" not in body


def test_a_403_is_not_worded_as_only_somebody_elses_job():
    """daemon は持ち主の不一致も権限の不足も 403 forbidden で返す。文は原因を 1 つに決めつけない。"""
    rig = _rig()
    rig.client.job_reply = Reply(403, None, "forbidden")
    body = rig.send(rig.request("GET", VIEW)).body.decode("utf-8")
    assert "権限" in body and "主体" in body


def test_a_failed_job_view_reports_one_fixed_line_unless_the_call_was_abandoned(capsys):
    from afx_ops.web.request import handle_request

    def send(rig, live):
        request = rig.request("GET", VIEW)
        return handle_request(request, rig.client, host_policy=rig.host_policy,
                              authenticator=rig.authenticator, peer="127.0.0.1",
                              write_context=rig.context, report_upstream=lambda: live)

    cases = [({"fail": api_error("timeout")}, "afx-ops: web_upstream reason=timeout route=job\n"),
             ({"reply": Reply(500, None, "internal")}, "afx-ops: web_upstream status=500 code=internal route=job\n"),
             ({"reply": Reply(200, {"id": OTHER_JOB, "kind": "improve", "state": "running"}, None)},
              "afx-ops: web_upstream shape=invalid route=job\n")]
    for setup, line in cases:
        for live, expected in ((True, line), (False, "")):
            rig = _rig()
            if "fail" in setup:
                rig.client.fail["job"] = setup["fail"]
            else:
                rig.client.job_reply = setup["reply"]
            assert send(rig, live).status == 502
            assert capsys.readouterr().err == expected


def test_other_daemon_failures_on_the_view_are_502():
    rig = _rig()
    rig.client.job_reply = Reply(500, None, "internal")
    assert rig.send(rig.request("GET", VIEW)).status == 502
    rig.client.job_reply = Reply(200, {"id": JOB, "kind": "improve", "state": "done"}, "x")
    assert rig.send(rig.request("GET", VIEW)).status == 502
    rig.client.fail["job"] = api_error()
    assert rig.send(rig.request("GET", VIEW)).status == 502


# --- 取消 (確認の 2 段) -------------------------------------------------------------------------

def test_the_first_cancel_post_only_shows_a_confirmation_and_never_calls_the_daemon():
    rig = _rig()
    response = rig.post(CANCEL, {})
    assert response.status == 200 and rig.client.calls == []
    body = response.body.decode("utf-8")
    assert JOB in body and "取消" in body
    assert len(token_of(response)) == 43


def test_the_final_post_cancels_the_stored_job_with_the_web_actor_once():
    rig = _rig()
    token = token_of(rig.post(CANCEL, {}))
    done = rig.confirm(token)
    assert done.status == 200
    assert rig.client.calls == [("cancel_job", "web:afx", JOB)]
    assert f'href="{VIEW}"' in done.body.decode("utf-8")
    assert rig.confirm(token).status == 409 and len(rig.client.calls) == 1


def test_a_cancel_token_made_for_one_job_cannot_cancel_another():
    rig = _rig()
    token = token_of(rig.post(CANCEL, {}))
    other = token_of(rig.post(f"/jobs/{OTHER_JOB}/cancel", {}))
    rig.confirm(other)
    rig.confirm(token)
    assert rig.client.calls == [("cancel_job", "web:afx", OTHER_JOB),
                                ("cancel_job", "web:afx", JOB)]


def test_the_cancel_form_takes_no_field_and_a_body_is_refused():
    rig = _rig()
    assert rig.post(CANCEL, {"job_id": OTHER_JOB}).status == 400
    assert rig.post(CANCEL, {"actor": "web:mallory"}).status == 400
    assert rig.client.calls == []


@pytest.mark.parametrize("name", ["origin", "auth", "type", "loopback"])
def test_each_failed_request_check_on_a_cancel_is_refused_without_a_call_or_a_record(name):
    rig = Rig(loopback=False) if name == "loopback" else _rig()
    kwargs = {"origin": {"origin": None}, "auth": {"authorization": None},
              "type": {"content_type": "text/plain"}, "loopback": {}}[name]
    response = rig.post(CANCEL, {}, **kwargs)
    assert response.status in (401, 403, 415)
    assert rig.client.calls == [] and rig.store.counts() == (0, 0)


def test_a_daemon_refusal_of_a_cancel_is_shown_with_its_fixed_code_only():
    rig = _rig()
    rig.client.cancel_reply = Reply(409, None, "job_running")
    response = rig.confirm(token_of(rig.post(CANCEL, {})))
    assert response.status == 409
    assert "job_running" in response.body.decode("utf-8")


def test_an_unknown_code_on_a_cancel_is_not_shown():
    rig = _rig()
    rig.client.cancel_reply = Reply(409, None, "private_customer_note")
    response = rig.confirm(token_of(rig.post(CANCEL, {})))
    assert b"private_customer_note" not in response.body


@pytest.mark.parametrize("data", [{"id": OTHER_JOB, "state": "running"}, {"state": "running"},
                                  {"id": None}, None, [], {"id": 5}])
def test_a_cancel_success_for_another_or_no_job_is_an_unknown_result(data):
    rig = _rig()
    rig.client.cancel_reply = Reply(200, data, None)
    response = rig.confirm(token_of(rig.post(CANCEL, {})))
    assert response.status == 502
    text = response.body.decode("utf-8")
    assert "分かりません" in text and "受け付けました" not in text


def test_a_lost_cancel_response_is_unknown_is_not_replayed_and_points_at_the_job():
    rig = _rig()
    token = token_of(rig.post(CANCEL, {}))
    rig.client.fail["cancel_job"] = ConnectionError("lost")
    lost = rig.confirm(token)
    assert lost.status == 502
    text = lost.body.decode("utf-8")
    assert f'href="{VIEW}"' in text and "/confirm/check" not in text
    rig.client.fail.clear()
    again = rig.post("/confirm/check", {"token": token})
    assert again.status == 502
    assert [call[0] for call in rig.client.calls] == ["cancel_job"]


def test_a_started_improve_result_links_to_its_job_page():
    rig = _rig()
    done = rig.confirm(token_of(rig.post("/improve/runs", {})))
    assert f'href="{VIEW}"' in done.body.decode("utf-8")
