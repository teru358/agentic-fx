"""書込みの adapter: 書込みが無効なら今のまま、有効でも検査 1〜5 を欠く要求は daemon を呼ばない。"""
from __future__ import annotations

import threading
from urllib.parse import urlencode

import pytest

from afx_ops.api_client import Reply
from afx_ops.web import write
from afx_ops.web.headers import WRITABLE_CSP
from afx_ops.web.request import Request, handle_request

from _write_support import (
    BASIC, DIGEST, FORM, JOB, OTHER_DIGEST, PORT, FakeClient, Rig, api_error, token_of,
)

READ_ONLY_CSP_ACTION = "form-action 'none'"
WRITE_PATHS = ["/approvals/7/approve", "/approvals/7/reject", "/improve/runs", "/confirm",
               "/confirm/check", "/improve"]


def _rig(**kwargs) -> Rig:
    return Rig(**kwargs)


# --- 書込みが無効 (既定) --------------------------------------------------------------------

@pytest.mark.parametrize("path", WRITE_PATHS)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_with_write_off_every_write_path_is_404_and_no_daemon_call(path, method):
    rig = _rig()
    response = rig.send(rig.request(method, path, {"payload_sha256": DIGEST}), writable=False)
    assert response.status == 404
    assert rig.client.calls == []
    assert READ_ONLY_CSP_ACTION in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["/", "/approvals", "/approvals/7", "/plugins", "/logs"])
def test_with_write_off_a_known_route_is_still_405(method, path):
    rig = _rig()
    response = rig.send(rig.request(method, path, None), writable=False)
    assert response.status == 405 and response.headers["Allow"] == "GET, HEAD"
    assert rig.client.calls == []


def test_with_write_off_the_approval_page_has_no_form_and_keeps_the_read_only_csp():
    rig = _rig()
    response = rig.send(rig.request("GET", "/approvals/7"), writable=False)
    assert response.status == 200
    assert b"<form" not in response.body
    assert READ_ONLY_CSP_ACTION in response.headers["Content-Security-Policy"]
    assert rig.client.calls == [("approval", 7), ("approval_change", 7)]


def test_write_context_needs_host_policy_and_authentication():
    rig = _rig()
    request = rig.request("GET", "/")
    with pytest.raises(ValueError):
        handle_request(request, rig.client, host_policy=rig.host_policy,
                       write_context=rig.context)
    with pytest.raises(ValueError):
        handle_request(request, rig.client, authenticator=rig.authenticator, peer="127.0.0.1",
                       write_context=rig.context)


# --- 書込みが有効: 画面と CSP ---------------------------------------------------------------

def test_with_write_on_every_response_relaxes_form_action_to_self_only():
    rig = _rig()
    responses = [
        rig.send(rig.request("GET", "/")),
        rig.send(rig.request("GET", "/nowhere")),
        rig.send(rig.request("POST", "/")),
        rig.send(rig.request("GET", "/", authorization=None)),
        rig.send(rig.request("GET", "/improve")),
        rig.post("/confirm", {"token": "A" * 43}),
    ]
    assert [r.status for r in responses] == [200, 404, 405, 401, 200, 403]
    for response in responses:
        assert response.headers["Content-Security-Policy"] == WRITABLE_CSP
        assert "form-action 'self'" in WRITABLE_CSP and "form-action 'none'" not in WRITABLE_CSP


def test_the_pending_plugin_approval_page_gets_decision_forms_without_auto_refresh():
    rig = _rig()
    page = rig.send(rig.request("GET", "/approvals/7")).body.decode("utf-8")
    assert 'action="/approvals/7/approve"' in page and 'action="/approvals/7/reject"' in page
    assert page.count(f'name="payload_sha256" value="{DIGEST}"') == 2
    assert 'http-equiv="refresh"' not in page
    assert page.endswith("</body></html>")


@pytest.mark.parametrize("detail", [
    {"id": 7, "kind": "plugin", "status": "approved", "payload_sha256": DIGEST},
    {"id": 7, "kind": "disable", "status": "pending", "payload_sha256": DIGEST},
    {"id": 7, "kind": "plugin", "status": "pending", "payload_sha256": "xyz"},
    {"id": 7, "kind": "plugin", "status": "pending"},
])
def test_other_approval_pages_get_no_form_and_keep_the_refresh(detail):
    rig = _rig()
    rig.client.detail = Reply(200, detail, None)
    page = rig.send(rig.request("GET", "/approvals/7")).body.decode("utf-8")
    assert "<form" not in page and 'http-equiv="refresh"' in page


def test_the_improve_form_is_a_static_page_that_posts_to_the_first_step():
    rig = _rig()
    response = rig.send(rig.request("GET", "/improve"))
    assert response.status == 200 and rig.client.calls == []
    body = response.body.decode("utf-8")
    assert 'action="/improve/runs"' in body and "token" not in body
    head = rig.send(rig.request("HEAD", "/improve"))
    assert head.status == 200 and head.body == b""


def test_a_wrong_method_on_a_write_route_is_405_with_its_allow_list():
    rig = _rig()
    assert rig.send(rig.request("GET", "/confirm")).headers["Allow"] == "POST"
    assert rig.send(rig.request("GET", "/approvals/7/approve")).status == 405
    assert rig.send(rig.request("POST", "/improve", {})).headers["Allow"] == "GET, HEAD"
    assert rig.send(rig.request("PUT", "/improve/runs", {})).status == 405
    assert rig.client.calls == []


def test_non_matching_paths_stay_404():
    rig = _rig()
    for path in ("/approvals/7/retry", "/approvals/0/approve", "/approvals/7/approve/x",
                 "/confirm/", "/improve/run", "/Approvals/7/approve"):
        assert rig.post(path, {"payload_sha256": DIGEST}).status == 404, path
    assert rig.client.calls == []


# --- 検査 1〜5: 欠ければ 4xx で daemon を呼ばない ------------------------------------------

def _bad_requests(rig: Rig):
    """(名前, status, 要求) の一覧。1 回目の POST と 2 回目の POST のどちらにも掛かる検査。"""
    host = rig.host()
    return [
        ("no-auth", 401, dict(authorization=None)),
        ("wrong-auth", 401, dict(authorization="Basic " + "AAAA")),
        ("origin-missing", 403, dict(origin=None)),
        ("origin-null", 403, dict(origin="null")),
        ("origin-empty", 403, dict(origin="")),
        ("origin-other-host", 403, dict(origin="http://evil.example")),
        ("origin-lan-host", 403, dict(origin=f"http://192.168.1.5:{PORT}")),
        ("origin-other-port", 403, dict(origin="http://127.0.0.1:9999")),
        ("origin-no-port", 403, dict(origin="http://127.0.0.1")),
        ("origin-https", 403, dict(origin=f"https://{host}")),
        ("origin-trailing-slash", 403, dict(origin=f"http://{host}/")),
        ("origin-with-path", 403, dict(origin=f"http://{host}/approvals")),
        ("origin-uppercase-scheme", 403, dict(origin=f"HTTP://{host}")),
        ("origin-garbage", 403, dict(origin="%%%")),
        ("origin-two-values", 403, dict(origin=f"http://{host}, http://{host}")),
        ("origin-duplicated", 403, dict(extra=(("Origin", f"http://{host}"),))),
        ("referer-only", 403, dict(origin=None, extra=(("Referer", f"http://{host}/"),))),
        ("content-type-missing", 415, dict(content_type=None)),
        ("content-type-json", 415, dict(content_type="application/json")),
        ("content-type-multipart", 415, dict(content_type="multipart/form-data; boundary=x")),
        ("content-type-text", 415, dict(content_type="text/plain")),
        ("content-type-charset-other", 415, dict(content_type=FORM + "; charset=latin-1")),
        ("content-type-duplicated", 415, dict(extra=(("Content-Type", FORM),))),
    ]


def _first_steps():
    return [
        ("/approvals/7/approve", {"payload_sha256": DIGEST}),
        ("/approvals/7/reject", {"payload_sha256": DIGEST, "reason": "x"}),
        ("/improve/runs", {"max_runs": "2"}),
    ]


@pytest.mark.parametrize("name", [name for name, _s, _k in _bad_requests(_rig())])
@pytest.mark.parametrize("path", [path for path, _f in _first_steps()] + ["/confirm", "/confirm/check"])
def test_each_failed_check_is_4xx_with_no_daemon_call_and_no_record(name, path):
    rig = _rig()
    token = rig.first_post("approve")
    rig.client.calls.clear()
    status, kwargs = next((s, k) for n, s, k in _bad_requests(rig) if n == name)
    form = dict(_first_steps())[path] if path not in ("/confirm", "/confirm/check") else {"token": token}
    before = rig.store.counts()
    response = rig.post(path, form, **kwargs)
    assert response.status == status
    assert rig.client.calls == []
    assert rig.store.counts() == before
    assert token.encode() not in response.body


def test_a_non_loopback_non_tls_context_refuses_every_write_post_and_the_form():
    rig = _rig(loopback=False)
    token_rig = _rig()
    token = token_rig.first_post("approve")
    for path, form in _first_steps() + [("/confirm", {"token": token}),
                                        ("/confirm/check", {"token": token})]:
        response = rig.post(path, form)
        assert response.status == 403, path
    assert rig.send(rig.request("GET", "/improve")).status == 403
    assert rig.client.calls == [] and rig.store.counts() == (0, 0)


def test_a_tls_context_without_loopback_is_allowed_to_write():
    rig = _rig(loopback=False, tls=True)
    assert rig.context.transport_ok
    assert rig.post("/approvals/7/approve", {"payload_sha256": DIGEST}).status == 200


def test_a_rejected_check_does_not_leak_whether_the_token_exists():
    rig = _rig()
    assert rig.post("/confirm", {"token": "A" * 43}, origin="null").status == 403
    assert rig.client.calls == []


# --- Origin の組み立て ----------------------------------------------------------------------

def test_the_origin_is_built_from_the_checked_host_and_omits_the_default_port():
    rig = _rig(port=80)
    assert rig.origin() == "http://127.0.0.1"
    assert rig.post("/approvals/7/approve", {"payload_sha256": DIGEST}).status == 200
    explicit = rig.post("/approvals/7/approve", {"payload_sha256": DIGEST},
                        origin="http://127.0.0.1:80")
    assert explicit.status == 403
    other = rig.post("/approvals/7/approve", {"payload_sha256": DIGEST},
                     origin="http://127.0.0.1:8788")
    assert other.status == 403


def test_a_localhost_host_header_needs_a_localhost_origin():
    rig = _rig()
    good = rig.post("/approvals/7/approve", {"payload_sha256": DIGEST}, host=f"localhost:{PORT}",
                    origin=f"http://localhost:{PORT}")
    assert good.status == 200
    cross = rig.post("/approvals/7/approve", {"payload_sha256": DIGEST}, host=f"localhost:{PORT}",
                     origin=f"http://127.0.0.1:{PORT}")
    assert cross.status == 403
    mixed = rig.post("/approvals/7/approve", {"payload_sha256": DIGEST}, host=f"LocalHost:{PORT}",
                     origin=f"http://localhost:{PORT}")
    assert mixed.status == 200


def test_a_host_the_policy_refuses_is_400_before_origin_is_looked_at():
    rig = _rig()
    response = rig.post("/approvals/7/approve", {"payload_sha256": DIGEST},
                        host="evil.example", origin="http://evil.example")
    assert response.status == 400 and rig.client.calls == []


# --- 入力の厳密な parse ---------------------------------------------------------------------

@pytest.mark.parametrize("path, body", [
    ("/approvals/7/approve", b""),
    ("/approvals/7/approve", b"payload_sha256="),
    ("/approvals/7/approve", b"payload_sha256=" + b"AB" * 32),
    ("/approvals/7/approve", b"payload_sha256=" + b"ab" * 31),
    ("/approvals/7/approve", b"payload_sha256=" + b"ab" * 33),
    ("/approvals/7/approve", f"payload_sha256={DIGEST}&payload_sha256={DIGEST}".encode()),
    ("/approvals/7/approve", f"payload_sha256={DIGEST}&reason=x".encode()),
    ("/approvals/7/approve", f"payload_sha256={DIGEST}&approval_id=8".encode()),
    ("/approvals/7/approve", f"payload_sha256={DIGEST}&".encode()),
    ("/approvals/7/approve", f"&payload_sha256={DIGEST}".encode()),
    ("/approvals/7/approve", f"payload_sha256={DIGEST};x=1".encode()),
    ("/approvals/7/approve", f"payload_sha256".encode()),
    ("/approvals/7/approve", f"payload_sha256={DIGEST}\n".encode()),
    ("/approvals/7/approve", f"payload_sha256={DIGEST}&x=%ff".encode()),
    ("/approvals/7/approve", f"payload_sha256={DIGEST}&x=é".encode("utf-8")),
    ("/approvals/7/approve", (f"payload_sha256={DIGEST}&" + "x=1&" * 40).encode()),
    ("/approvals/7/reject", f"payload_sha256={DIGEST}&reason=a%00b".encode()),
    ("/approvals/7/reject", f"payload_sha256={DIGEST}&reason=a%0Ab".encode()),
    ("/approvals/7/reject", f"payload_sha256={DIGEST}&reason=a%E2%80%8Fb".encode()),
    ("/approvals/7/reject", (f"payload_sha256={DIGEST}&reason=" + "x" * 501).encode()),
    ("/approvals/7/reject", f"payload_sha256={DIGEST}&reason=a&reason=b".encode()),
    ("/improve/runs", b"duration_sec=59"),
    ("/improve/runs", b"duration_sec=86401"),
    ("/improve/runs", b"duration_sec=-1"),
    ("/improve/runs", b"duration_sec=1e3"),
    ("/improve/runs", b"duration_sec=0060"),
    ("/improve/runs", b"duration_sec=%2B60"),
    ("/improve/runs", b"max_runs=0"),
    ("/improve/runs", b"max_runs=25"),
    ("/improve/runs", b"max_runs=true"),
    ("/improve/runs", b"runner_choice=x"),
    ("/improve/runs", b"comment=" + b"x" * 1001),
    ("/improve/runs", b"comment=a%00b"),
    ("/improve/runs", b"max_runs=1&max_runs=2"),
    ("/improve/runs", b"max_runs=2&"),
    ("/improve/runs", b"comment=a&&max_runs=2"),
    ("/improve/runs", b"comment"),
    ("/improve/runs", "comment=\u00e9".encode("utf-8")),
    ("/confirm", b""),
    ("/confirm", b"token=" + b"A" * 42),
    ("/confirm", b"token=" + b"A" * 44),
    ("/confirm", b"token=" + b"A" * 42 + b"%2F"),
    ("/confirm", b"token=" + b"A" * 43 + b"&approval_id=8"),
    ("/confirm", b"token=" + b"A" * 43 + b"&token=" + b"A" * 43),
    ("/confirm/check", b"payload_sha256=" + DIGEST.encode()),
])
def test_a_malformed_body_is_400_with_no_daemon_call_and_no_record(path, body):
    rig = _rig()
    response = rig.post(path, body)
    assert response.status == 400, (path, body)
    assert rig.client.calls == [] and rig.store.counts() == (0, 0)


def test_a_body_over_the_write_limit_is_413():
    rig = _rig()
    big = b"comment=" + b"x" * write.MAX_WRITE_BODY
    assert rig.post("/improve/runs", big).status == 413
    assert rig.client.calls == [] and rig.store.counts() == (0, 0)


def test_an_empty_improve_form_is_accepted_and_blank_optional_fields_are_dropped():
    rig = _rig()
    token = rig.first_improve()
    rig.confirm(token)
    token = rig.first_improve(duration_sec="", max_runs="", comment="   ")
    rig.confirm(token)
    assert [call[1:3] for call in rig.client.calls if call[0] == "improve_run"] == [
        ("web:afx", {}), ("web:afx", {})]


def test_the_raw_bytes_path_applies_the_same_checks():
    rig = _rig()
    body = urlencode({"payload_sha256": DIGEST}).encode()
    head = (f"POST /approvals/7/approve HTTP/1.1\r\nHost: {rig.host()}\r\n"
            f"Authorization: {BASIC}\r\nOrigin: {rig.origin()}\r\n"
            f"Content-Type: {FORM}\r\nContent-Length: {len(body)}\r\n\r\n").encode()
    ok = handle_request(head + body, rig.client, host_policy=rig.host_policy,
                        authenticator=rig.authenticator, peer="127.0.0.1",
                        write_context=rig.context)
    assert ok.status == 200
    bad = handle_request(head.replace(b"Origin: http", b"Origin: ftp") + body, rig.client,
                         host_policy=rig.host_policy, authenticator=rig.authenticator,
                         peer="127.0.0.1", write_context=rig.context)
    assert bad.status == 403 and rig.client.calls == []


# --- 確認の 2 段 ----------------------------------------------------------------------------

def test_the_first_step_only_shows_a_confirmation_and_never_calls_the_daemon():
    rig = _rig()
    response = rig.post("/approvals/7/reject", {"payload_sha256": DIGEST, "reason": "<b>x</b>"})
    assert response.status == 200 and rig.client.calls == []
    body = response.body.decode("utf-8")
    assert "&lt;b&gt;x&lt;/b&gt;" in body and "<b>x</b>" not in body
    assert DIGEST[:12] in body and DIGEST not in body
    assert 'action="/confirm"' in body and 'http-equiv="refresh"' not in body
    assert rig.store.counts() == (1, 0)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_the_token_appears_only_in_the_hidden_field_of_the_confirmation():
    rig = _rig()
    response = rig.post("/approvals/7/approve", {"payload_sha256": DIGEST})
    token = token_of(response)
    assert response.body.decode().count(token) == 1
    assert all(token not in value for value in response.headers.values())


def test_approve_runs_once_with_the_stored_values_and_the_web_actor():
    rig = _rig()
    token = rig.first_post("approve")
    response = rig.confirm(token)
    assert response.status == 200 and JOB in response.body.decode()
    assert rig.client.calls == [("approve", "web:afx", 7, DIGEST)]
    again = rig.confirm(token)
    assert again.status == 409 and len(rig.client.calls) == 1


def test_the_final_post_cannot_change_what_is_executed():
    rig = _rig()
    token = rig.first_post("approve", approval_id=7)
    for extra in ({"approval_id": "8"}, {"payload_sha256": OTHER_DIGEST}, {"action": "reject"}):
        response = rig.post("/confirm", {"token": token, **extra})
        assert response.status == 400
    assert rig.client.calls == []
    rig.confirm(token)
    assert rig.client.calls == [("approve", "web:afx", 7, DIGEST)]


def test_a_token_is_bound_to_its_own_action_and_target():
    rig = _rig()
    approve_token = rig.first_post("approve", approval_id=7)
    reject_token = rig.first_post("reject", approval_id=9, reason="no")
    rig.client.detail = Reply(200, {"id": 9, "payload_sha256": DIGEST}, None)
    rig.confirm(reject_token)
    assert [c[0] for c in rig.client.calls] == ["approval", "reject"]
    assert rig.client.calls[1] == ("reject", "web:afx", 9, "no")
    rig.confirm(approve_token)
    assert rig.client.calls[-1] == ("approve", "web:afx", 7, DIGEST)


def test_an_expired_token_is_410_and_a_token_for_another_user_is_403_with_no_call():
    rig = _rig()
    token = rig.first_post("approve")
    rig.clock.now += write.UNUSED_TTL
    assert rig.confirm(token).status == 410
    assert rig.client.calls == []
    other = rig.first_post("approve")
    record = next(iter(rig.store._records.values()))
    record.user = "someone-else"
    assert rig.confirm(other).status == 403 and rig.client.calls == []


def test_the_33rd_confirmation_is_refused_and_the_existing_tokens_still_work():
    rig = _rig()
    tokens = [rig.first_post("approve") for _ in range(write.UNUSED_LIMIT)]
    response = rig.post("/approvals/7/approve", {"payload_sha256": DIGEST})
    assert response.status == 429 and rig.client.calls == []
    assert rig.confirm(tokens[0]).status == 200


def test_parallel_final_posts_with_one_token_execute_once():
    rig = _rig()
    token = rig.first_post("approve")
    original = rig.client.approve

    def slow(actor, approval_id, *, payload_sha256):
        import time
        time.sleep(0.05)
        return original(actor, approval_id, payload_sha256=payload_sha256)

    rig.client.approve = slow
    barrier = threading.Barrier(6)
    statuses: list[int] = []

    def send():
        barrier.wait()
        statuses.append(rig.confirm(token).status)

    threads = [threading.Thread(target=send) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(statuses) == [200] + [409] * 5
    assert [c for c in rig.client.calls if c[0] == "approve"] == [("approve", "web:afx", 7, DIGEST)]


# --- 却下の直前の照合 -----------------------------------------------------------------------

def test_reject_re_reads_the_detail_and_sends_the_reason_when_the_digest_is_unchanged():
    rig = _rig()
    token = rig.first_post("reject", reason=" because ")
    response = rig.confirm(token)
    assert response.status == 200
    assert rig.client.calls == [("approval", 7), ("reject", "web:afx", 7, "because")]


def test_reject_without_a_reason_sends_none():
    rig = _rig()
    rig.confirm(rig.first_post("reject"))
    assert rig.client.calls[-1] == ("reject", "web:afx", 7, None)


def test_reject_is_not_sent_when_the_digest_changed_after_it_was_shown():
    rig = _rig()
    token = rig.first_post("reject", reason="no")
    rig.client.detail = Reply(200, {"id": 7, "payload_sha256": OTHER_DIGEST}, None)
    response = rig.confirm(token)
    assert response.status == 409
    assert rig.client.calls == [("approval", 7)]
    assert "変わった" in response.body.decode("utf-8")
    assert rig.confirm(token).status == 409 and rig.client.calls == [("approval", 7)]


@pytest.mark.parametrize("detail", [
    Reply(404, None, "not_found"), Reply(200, None, None), Reply(200, {"id": 7}, None),
    Reply(200, {"payload_sha256": 5}, None), Reply(500, {"payload_sha256": DIGEST}, None)])
def test_reject_is_not_sent_when_the_detail_cannot_be_verified(detail):
    rig = _rig()
    token = rig.first_post("reject")
    rig.client.detail = detail
    assert rig.confirm(token).status == 409
    assert rig.client.calls == [("approval", 7)]


def test_reject_is_not_sent_when_re_reading_the_detail_fails():
    rig = _rig()
    token = rig.first_post("reject")
    rig.client.fail["approval"] = ConnectionError("down")
    assert rig.confirm(token).status == 409
    assert [c[0] for c in rig.client.calls] == ["approval"]


# --- daemon の応答 --------------------------------------------------------------------------

def test_a_daemon_refusal_is_a_known_result_shown_with_its_fixed_code_only():
    rig = _rig()
    rig.client.approve_reply = Reply(409, None, "digest_mismatch")
    token = rig.first_post("approve")
    response = rig.confirm(token)
    assert response.status == 409
    assert "digest_mismatch" in response.body.decode()
    assert rig.confirm(token).status == 409 and len(rig.client.calls) == 1


def test_a_free_text_code_from_the_daemon_is_not_shown():
    rig = _rig()
    rig.client.approve_reply = Reply(422, None, "Bad Thing <script>")
    response = rig.confirm(rig.first_post("approve"))
    assert b"script" not in response.body and b"Bad Thing" not in response.body


@pytest.mark.parametrize("reply", [Reply(500, None, "internal"), Reply(503, None, "busy"),
                                   object(), None])
def test_a_server_error_or_an_unreadable_reply_is_an_unknown_result(reply):
    rig = _rig()
    rig.client.approve_reply = reply
    token = rig.first_post("approve")
    response = rig.confirm(token)
    assert response.status == 502
    body = response.body.decode()
    assert "分かりません" in body and 'href="/approvals/7"' in body
    assert "/confirm/check" not in body


def test_a_lost_decision_response_is_unknown_and_check_never_replays_a_decision():
    rig = _rig()
    rig.client.fail["approve"] = ConnectionError("lost")
    token = rig.first_post("approve")
    assert rig.confirm(token).status == 502
    assert rig.post("/confirm/check", {"token": token}).status == 502
    assert [c[0] for c in rig.client.calls] == ["approve"]
    assert rig.confirm(token).status == 409


def test_an_error_before_anything_was_sent_is_a_known_non_execution():
    rig = _rig()
    rig.client.fail["approve"] = api_error("server_auth_failed", token_sent=False)
    token = rig.first_post("approve")
    response = rig.confirm(token)
    assert response.status == 409 and "実行していません" in response.body.decode()
    rig.client.fail["improve_run"] = api_error("connect_failed", token_sent=True)
    token = rig.first_improve(max_runs="2")
    assert rig.confirm(token).status == 502


# --- 改善と「結果を確かめる」 ------------------------------------------------------------------

def test_improve_sends_the_stored_conditions_with_a_key_derived_from_the_token():
    rig = _rig()
    token = rig.first_improve(duration_sec="3600", max_runs="3", comment="  look at rsi ")
    response = rig.confirm(token)
    assert response.status == 200 and JOB in response.body.decode()
    key = write.idempotency_key_of(token)
    assert rig.client.calls == [("improve_run", "web:afx",
                                 {"duration_sec": 3600, "max_runs": 3, "comment": "look at rsi"},
                                 key)]
    assert token not in key and token not in repr(rig.client.calls)


def test_a_lost_improve_response_is_recovered_by_replaying_the_stored_body_and_key():
    rig = _rig()
    token = rig.first_improve(max_runs="2", comment="x")
    rig.client.fail["improve_run"] = ConnectionError("lost")
    lost = rig.confirm(token)
    assert lost.status == 502
    assert token_of(lost) == token and "/confirm/check" in lost.body.decode()
    first_call = rig.client.calls[0]
    rig.client.fail.clear()
    recovered = rig.post("/confirm/check", {"token": token})
    assert recovered.status == 200 and JOB in recovered.body.decode()
    assert rig.client.calls == [first_call, first_call]
    # 結果が分かった後は、確かめても daemon を呼ばず、同じ結果を返す
    again = rig.post("/confirm/check", {"token": token})
    assert again.status == 200 and JOB in again.body.decode()
    assert len(rig.client.calls) == 2
    # 消費し直せない
    assert rig.confirm(token).status == 409 and len(rig.client.calls) == 2


def test_check_that_is_still_lost_stays_unknown_and_can_be_tried_again():
    rig = _rig()
    token = rig.first_improve()
    rig.client.fail["improve_run"] = ConnectionError("lost")
    assert rig.confirm(token).status == 502
    assert rig.post("/confirm/check", {"token": token}).status == 502
    rig.client.fail.clear()
    assert rig.post("/confirm/check", {"token": token}).status == 200
    assert len(rig.client.calls) == 3


def test_check_before_the_final_post_is_refused_without_a_call():
    rig = _rig()
    token = rig.first_improve()
    assert rig.post("/confirm/check", {"token": token}).status == 409
    assert rig.client.calls == []
    assert rig.confirm(token).status == 200


def test_parallel_checks_of_an_unknown_improve_replay_once():
    rig = _rig()
    token = rig.first_improve()
    rig.client.fail["improve_run"] = ConnectionError("lost")
    rig.confirm(token)
    rig.client.fail.clear()
    original = rig.client.improve_run

    def slow(actor, body, *, idempotency_key):
        import time
        time.sleep(0.05)
        return original(actor, body, idempotency_key=idempotency_key)

    rig.client.improve_run = slow
    barrier = threading.Barrier(5)
    statuses: list[int] = []

    def send():
        barrier.wait()
        statuses.append(rig.post("/confirm/check", {"token": token}).status)

    threads = [threading.Thread(target=send) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(statuses) == [200] + [409] * 4
    assert len([c for c in rig.client.calls if c[0] == "improve_run"]) == 2


# --- token を残さない -----------------------------------------------------------------------

def test_no_token_reaches_stderr_or_the_logs_on_success_or_failure(capsys):
    rig = _rig()
    tokens = []
    for failure in (None, ConnectionError("lost")):
        rig.client.fail.clear()
        if failure is not None:
            rig.client.fail["improve_run"] = failure
        token = rig.first_improve(max_runs="2")
        tokens.append(token)
        rig.confirm(token)
        rig.post("/confirm/check", {"token": token})
    rig.post("/confirm", {"token": "B" * 43})
    captured = capsys.readouterr()
    assert not any(token in captured.out + captured.err for token in tokens)
    assert captured.err == ""


def test_the_store_and_context_reprs_do_not_carry_a_token():
    rig = _rig()
    token = rig.first_post("approve")
    rig.confirm(token)
    assert token not in repr(rig.context) and token not in repr(rig.store)
    assert token not in repr(list(rig.store._records.values()))


def test_a_get_never_changes_the_store():
    rig = _rig()
    for path in ("/", "/approvals", "/approvals/7", "/improve", "/plugins"):
        rig.send(rig.request("GET", path))
    assert rig.store.counts() == (0, 0)
    assert all(call[0] in {"status", "approvals", "approval", "approval_change", "plugins"}
               for call in rig.client.calls)


def test_a_check_after_the_hour_is_refused_before_the_daemon_is_called():
    rig = _rig()
    token = rig.first_improve(max_runs="2")
    rig.client.fail["improve_run"] = ConnectionError("lost")
    assert rig.confirm(token).status == 502
    rig.client.fail.clear()
    rig.clock.now += write.CONSUMED_TTL
    response = rig.post("/confirm/check", {"token": token})
    assert response.status == 410
    assert [c[0] for c in rig.client.calls] == ["improve_run"]


@pytest.mark.parametrize("status", [409, 200, 400, 500])
def test_an_outcome_unknown_code_is_unknown_whatever_the_http_status(status):
    rig = _rig()
    rig.client.improve_reply = Reply(status, None, "outcome_unknown")
    token = rig.first_improve()
    response = rig.confirm(token)
    body = response.body.decode()
    assert response.status == 502
    assert "分かりません" in body and "受け付けませんでした" not in body
    assert "/confirm/check" in body and token_of(response) == token


def test_an_outcome_unknown_409_from_the_daemon_can_be_checked_and_recovers():
    rig = _rig()
    rig.client.improve_reply = Reply(409, None, "outcome_unknown")
    token = rig.first_improve(max_runs="2")
    assert rig.confirm(token).status == 502
    rig.client.improve_reply = Reply(200, {"job_id": JOB}, None)
    recovered = rig.post("/confirm/check", {"token": token})
    assert recovered.status == 200 and JOB in recovered.body.decode()
    assert len(rig.client.calls) == 2


def test_an_outcome_unknown_for_a_decision_links_to_the_approval_instead_of_replaying():
    rig = _rig()
    rig.client.approve_reply = Reply(409, None, "outcome_unknown")
    response = rig.confirm(rig.first_post("approve"))
    body = response.body.decode()
    assert response.status == 502 and 'href="/approvals/7"' in body
    assert "/confirm/check" not in body


def test_the_longest_screen_inputs_fit_the_body_limit_even_in_four_byte_characters():
    rig = _rig()
    reason = "\U0001F600" * 500
    token = rig.first_post("reject", reason=reason)
    assert rig.confirm(token).status == 200
    assert rig.client.calls[-1] == ("reject", "web:afx", 7, reason)
    comment = "\U0001F600" * 1000
    token = rig.first_improve(duration_sec="86400", max_runs="24", comment=comment)
    assert rig.confirm(token).status == 200
    assert rig.client.calls[-1][1:2] == ("web:afx",)


def test_japanese_input_at_the_screen_limits_is_accepted_and_one_char_more_is_400():
    rig = _rig()
    assert rig.post("/approvals/7/reject", {"payload_sha256": DIGEST,
                                            "reason": "あ" * 500}).status == 200
    assert rig.post("/approvals/7/reject", {"payload_sha256": DIGEST,
                                            "reason": "あ" * 501}).status == 400
    assert rig.post("/improve/runs", {"comment": "あ" * 1000}).status == 200
    assert rig.post("/improve/runs", {"comment": "あ" * 1001}).status == 400


def test_the_body_limit_boundary_is_exact():
    rig = _rig()
    at = b"comment=" + b"x" * (write.MAX_WRITE_BODY - len(b"comment="))
    assert rig.post("/improve/runs", at).status != 413
    assert rig.post("/improve/runs", at + b"x").status == 413
