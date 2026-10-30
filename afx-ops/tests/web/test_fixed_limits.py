from __future__ import annotations

import asyncio

import pytest

from afx_ops.api_client import Reply
from afx_ops.web import runtime
from afx_ops.web.headers import SECURITY_HEADERS
from afx_ops.web.request import Request, handle_request
from afx_ops.web.routes import resolve_route


class Recorder:
    """読み取り側の fake。呼ばれた route を記録し、reply は差し替えられる。"""

    def __init__(self, reply=None, data=None):
        self.calls = []
        self._reply = reply
        self._data = data

    def _answer(self, name, default):
        self.calls.append(name)
        if self._reply is not None:
            return self._reply
        return Reply(200, self._data if self._data is not None else default, None)

    def status(self):
        return self._answer("status", {"mode": "paper"})

    def approvals(self, *, limit):
        return self._answer(("approvals", limit), {"approvals": []})

    def approval(self, approval_id):
        return self._answer(("approval", approval_id), {"id": approval_id})

    def log(self, *, n):
        return self._answer(("log", n), {"lines": ["l"]})

    def activity(self, *, n, category):
        return self._answer(("activity", n, category), {"lines": ["a"]})


def test_a_dot_segment_is_rejected_before_routing():
    client = Recorder()
    response = handle_request(b"GET /./ HTTP/1.1\r\nHost: x\r\n\r\n", client)

    assert response.status == 400
    assert client.calls == []


def test_a_request_line_over_2048_bytes_is_413():
    client = Recorder()
    response = handle_request(b"GET /" + b"a" * 2100 + b" HTTP/1.1\r\nHost: x\r\n\r\n", client)

    assert response.status == 413
    assert client.calls == []


def test_headers_over_8192_bytes_in_total_are_413_for_a_built_request():
    client = Recorder()
    response = handle_request(Request("GET", "/", {}, headers=(("X-Pad", "y" * 9000),)), client)

    assert response.status == 413
    assert client.calls == []


@pytest.mark.parametrize("target, query", [
    ("/approvals", {"limit": "3", "extra": "1"}),
    ("/logs", {"n": "3", "extra": "1"}),
    ("/", {"extra": "1"}),
])
def test_unknown_query_names_are_400_without_daemon_call(target, query):
    client = Recorder()
    response = handle_request(Request("GET", target, query), client)

    assert response.status == 400
    assert client.calls == []


@pytest.mark.parametrize("value", ["0", "05"])
def test_integer_query_values_must_not_have_leading_zeros(value):
    client = Recorder()
    response = handle_request(Request("GET", "/approvals", {"limit": value}), client)

    assert response.status == 400
    assert client.calls == []


@pytest.mark.parametrize("reply", [
    Reply(200, {"mode": "paper"}, "E_DAEMON"),
    Reply(502, {"mode": "paper"}, None),
])
def test_a_daemon_reply_that_is_not_a_clean_200_is_502(reply):
    response = handle_request(Request("GET", "/", {}), Recorder(reply=reply))

    assert response.status == 502


def test_control_characters_become_spaces_in_shown_values():
    page = handle_request(Request("GET", "/", {}), Recorder(data={"mode": "a\x01b\x7f c"})).body.decode()

    assert "<dd>a b  c</dd>" in page


def test_list_values_are_capped_at_50_items():
    data = {"jobs": [f"j{i}" for i in range(60)]}
    page = handle_request(Request("GET", "/", {}), Recorder(data=data)).body.decode()

    assert page.count("<li>") == 50
    assert "<li>j49</li>" in page
    assert "j50" not in page


def test_log_lines_are_capped_at_50():
    client = Recorder(reply=Reply(200, {"lines": [f"line{i}" for i in range(60)]}, None))
    body = handle_request(Request("GET", "/logs", {}), client).body.decode()

    assert "line59" in body and "line10" in body
    assert "line9<" not in body


def test_approval_links_are_capped_at_50():
    data = {"approvals": [{"id": i} for i in range(1, 61)]}
    page = handle_request(Request("GET", "/approvals", {}), Recorder(data=data)).body.decode()

    assert page.count("Details #") == 50
    assert "Details #50" in page
    assert "Details #51" not in page


def test_an_oversized_page_is_replaced_by_the_clipped_notice():
    client = Recorder(reply=Reply(200, {"lines": ["<" * 512] * 50}, None))
    response = handle_request(Request("GET", "/logs", {}), client)

    assert b"Output clipped" in response.body
    assert len(response.body) <= 64 * 1024


def test_security_headers_are_the_literal_fixed_set():
    response = handle_request(Request("GET", "/missing", {}), Recorder())

    assert response.headers["Content-Security-Policy"] == (
        "default-src 'none'; style-src 'sha256-dIAw3Rfkv/SAUhYuTRQUAz5UAqeCbdU2MPWMfhMTwaA='; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"
    assert set(SECURITY_HEADERS) <= set(response.headers)


def test_fields_outside_the_status_allowlist_are_not_shown():
    page = handle_request(Request("GET", "/", {}),
                          Recorder(data={"mode": "paper", "api_token": "tok-literal-123"})).body.decode()

    assert "tok-literal-123" not in page
    assert "api_token" not in page


def test_approval_ids_are_at_most_18_digits_without_leading_zero():
    match = resolve_route("/approvals/" + "1" * 18)

    assert match is not None and match.values == {"id": int("1" * 18)}
    assert resolve_route("/approvals/" + "1" * 19) is None
    assert resolve_route("/approvals/0") is None
    assert resolve_route("/approvals/01") is None


def test_trailing_slash_is_not_a_route():
    assert resolve_route("/logs/") is None
    assert resolve_route("/approvals/") is None


def test_runtime_limits_are_the_fixed_values():
    assert runtime.CONNECTION_DEADLINE == 30.0
    assert runtime.MAX_WORKERS == 4
    assert runtime.MAX_HEADERS == 64


def test_loop_errors_print_only_the_exception_type(capsys):
    runtime._loop_error(None, {"exception": ValueError("secret-value-42")})

    assert capsys.readouterr().err == "afx-ops: web_loop_error ValueError\n"


def test_closing_a_slot_cancels_its_deadline_timer():
    loop = asyncio.new_event_loop()
    try:
        slot = runtime._Slot(lambda _slot: None, "127.0.0.1")
        slot.deadline = loop.call_later(60, lambda: None)
        timer = slot.deadline
        slot.close()

        assert timer.cancelled()
        assert slot.deadline is None
    finally:
        loop.close()


def test_loop_errors_for_a_missing_attribute_add_only_fixed_words(capsys):
    error = AttributeError("secret-value-42")
    error.name = "close"
    runtime._loop_error(None, {"exception": error,
                               "message": "Exception in callback X(secret-value-42)"})

    assert capsys.readouterr().err == (
        "afx-ops: web_loop_error AttributeError name=close message=Exception_in_callback\n")


def test_loop_error_attribute_names_must_be_identifiers(capsys):
    error = AttributeError("x")
    error.name = "secret value 42"
    runtime._loop_error(None, {"exception": error})

    assert capsys.readouterr().err == "afx-ops: web_loop_error AttributeError\n"
