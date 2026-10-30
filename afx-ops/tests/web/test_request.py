from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from afx_ops.api_client import Reply
from afx_ops.web.request import MAX_BODY_BYTES, Request, handle_request, parse_http_request, parse_request_line


@dataclass
class FakeWebClient:
    calls: list[tuple[str, object]] = field(default_factory=list)

    def status(self):
        self.calls.append(("status", None))
        return Reply(200, {"mode": "safe", "equity": 123.4, "secret": "do-not-show"}, None)

    def approvals(self, *, limit: int):
        self.calls.append(("approvals", limit))
        return Reply(200, {"approvals": [{"id": 7, "state": "pending", "reason": "hidden"}]}, None)

    def approval(self, approval_id: int):
        self.calls.append(("approval", approval_id))
        return Reply(200, {"id": approval_id, "state": "pending", "parent_facts": {"trades": 8}}, None)

    def log(self, *, n: int):
        self.calls.append(("log", n))
        return Reply(200, {"lines": ["started"]}, None)

    def activity(self, *, n: int, category: str | None):
        self.calls.append(("activity", (n, category)))
        return Reply(200, {"events": [{"code": "service_health_changed", "state": "ok"}]}, None)


@pytest.mark.parametrize(("target", "expected"), [
    ("/", ("status", None)),
    ("/approvals?limit=3", ("approvals", 3)),
    ("/approvals/7", ("approval", 7)),
    ("/logs?n=3", ("log", 3)),
    ("/activity?n=3&category=SYSTEM", ("activity", (3, "SYSTEM"))),
])
def test_each_route_calls_only_its_read_method(target, expected):
    client = FakeWebClient()
    response = handle_request(parse_http_request(f"GET {target} HTTP/1.1\r\nHost: x\r\n\r\n".encode()), client)

    assert response.status == 200
    assert client.calls == [expected]


def test_head_has_get_headers_and_no_body():
    client = FakeWebClient()
    get = handle_request(Request("GET", "/", {}), client)
    head = handle_request(Request("HEAD", "/", {}), client)

    assert head.status == get.status
    assert head.headers == get.headers
    assert head.body == b""


def test_request_line_parser_accepts_only_ascii_origin_form_http11():
    assert parse_request_line(b"GET /activity?n=2 HTTP/1.1") == ("GET", "/activity", {"n": "2"})
    with pytest.raises(ValueError):
        parse_request_line(b"GET http://localhost/ HTTP/1.1")


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_known_route_mutation_is_405_without_daemon_call(method):
    client = FakeWebClient()
    response = handle_request(Request(method, "/approvals", {}), client)

    assert response.status == 405
    assert response.headers["Allow"] == "GET, HEAD"
    assert client.calls == []


@pytest.mark.parametrize("target", ["/decide", "/jobs", "/events", "/unknown", "/approvals/7/approve"])
def test_unknown_or_mutating_paths_are_404_without_daemon_call(target):
    client = FakeWebClient()
    response = handle_request(Request("GET", target, {}), client)

    assert response.status == 404
    assert client.calls == []


@pytest.mark.parametrize("raw, status", [
    (b"GET http://example/ HTTP/1.1\r\n\r\n", 400),
    (b"GET /../x HTTP/1.1\r\n\r\n", 400),
    (b"G\xffT / HTTP/1.1\r\n\r\n", 400),
    (b"GET / HTTP/1.0\r\n\r\n", 400),
    (b"GET /?x=1 HTTP/1.1\r\n\r\n", 400),
    (b"GET / HTTP/1.1\r\nX: " + b"x" * 9000 + b"\r\n\r\n", 413),
    (b"GET / HTTP/1.1\r\nContent-Length: 65537\r\n\r\n", 413),
])
def test_malformed_or_oversized_http_never_reaches_daemon(raw, status):
    client = FakeWebClient()
    response = handle_request(raw, client)

    assert response.status == status
    assert client.calls == []


def test_actor_header_is_rejected_without_reaching_daemon():
    client = FakeWebClient()
    response = handle_request(
        b"GET / HTTP/1.1\r\nHost: x\r\nX-Afx-Asserted-Actor: discord:12345678901234567\r\n\r\n",
        client,
    )

    assert response.status == 400
    assert client.calls == []


def test_adapter_rechecks_a_transport_supplied_oversized_body():
    client = FakeWebClient()
    response = handle_request(Request("GET", "/", {}, body=b"x" * (MAX_BODY_BYTES + 1)), client)

    assert response.status == 413
    assert client.calls == []


def test_head_error_responses_carry_no_body_for_every_status():
    class Client:
        def log(self, *, n):
            return Reply(200, {"lines": []}, None)

        def status(self):
            raise ConnectionError("daemon is down")

    cases = {
        400: Request("HEAD", "/logs", {"x": "1"}),
        404: Request("HEAD", "/nowhere", {}),
        413: b"HEAD / HTTP/1.1\r\nHost: x\r\nX-Pad: " + b"a" * 9000 + b"\r\n\r\n",
        502: Request("HEAD", "/", {}),
    }
    for status, request in cases.items():
        response = handle_request(request, Client())
        assert response.status == status, status
        assert response.body == b"", status
        assert response.headers["Content-Length"] != "0", status


def test_compact_headers_at_the_wire_limit_are_not_rejected_after_rebuilding():
    class Client:
        def status(self):
            return Reply(200, {"mode": "safe"}, None)

    pad = b"".join(b"X-Pad-%02d:" % i + b"a" * 190 + b"\r\n" for i in range(40))
    head = b"GET / HTTP/1.1\r\nHost:x\r\n" + pad
    raw = head + b"X-Fill:" + b"b" * (8192 - len(head) - 4 - 7) + b"\r\n\r\n"
    assert len(raw) == 8192
    assert handle_request(raw, Client()).status == 200
    request = parse_http_request(raw)
    assert handle_request(request, Client()).status == 200
    assert handle_request(raw + b"", Client()).status == 200


def test_a_built_request_is_counted_like_the_wire_including_request_line_and_final_crlf():
    class Client:
        def status(self):
            return Reply(200, {"mode": "safe"}, None)

    def build(total: int) -> Request:
        # 'GET / HTTP/1.1\r\n' + 'Host:x\r\n' + 'X-Fill:<n>\r\n' + '\r\n'
        fixed = len("GET / HTTP/1.1\r\n") + len("Host:x\r\n") + len("X-Fill:") + 2 + 2
        return Request("GET", "/", {}, (("Host", "x"), ("X-Fill", "b" * (total - fixed))))

    assert handle_request(build(8192), Client()).status == 200
    assert handle_request(build(8193), Client()).status == 413


class _RaisingClient:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def status(self):
        raise self.error


class _ReplyClient:
    def __init__(self, reply) -> None:
        self.reply = reply

    def status(self):
        return self.reply


def _upstream_lines(client, capsys) -> tuple[int, str]:
    response = handle_request(Request("GET", "/", {}), client)
    return response.status, capsys.readouterr().err


def test_an_api_error_reason_is_reported_as_a_fixed_word(capsys):
    from afx_ops.api_client import ApiError

    status, err = _upstream_lines(_RaisingClient(ApiError("socket_missing")), capsys)

    assert status == 502
    assert err == "afx-ops: web_upstream reason=socket_missing route=status\n"


def test_an_api_error_reason_outside_the_fixed_shape_is_other(capsys):
    from afx_ops.api_client import ApiError

    status, err = _upstream_lines(_RaisingClient(ApiError("/home/u/secret.sock Bad")), capsys)

    assert status == 502
    assert err == "afx-ops: web_upstream reason=other route=status\n"


def test_other_exceptions_report_only_the_type_name(capsys):
    status, err = _upstream_lines(_RaisingClient(OSError("/run/user/1000/afx/secret.sock")), capsys)

    assert status == 502
    assert err == "afx-ops: web_upstream error=OSError route=status\n"


def test_a_non_200_reply_reports_the_status_and_a_fixed_code(capsys):
    status, err = _upstream_lines(_ReplyClient(Reply(401, None, "unauthorized")), capsys)

    assert status == 502
    assert err == "afx-ops: web_upstream status=401 code=unauthorized route=status\n"


def test_a_non_200_reply_code_outside_the_fixed_shape_is_left_out(capsys):
    status, err = _upstream_lines(_ReplyClient(Reply(500, None, "Bad /path?x=1")), capsys)

    assert status == 502
    assert err == "afx-ops: web_upstream status=500 route=status\n"


def test_a_reply_of_the_wrong_shape_reports_one_fixed_line(capsys):
    status, err = _upstream_lines(_ReplyClient(Reply(200, {"mode": 5}, None)), capsys)

    assert status == 502
    assert err == "afx-ops: web_upstream shape=invalid route=status\n"


def test_an_abandoned_call_reports_nothing_and_a_live_one_reports_one_line(capsys):
    from afx_ops.api_client import ApiError

    for live, expected in ((False, ""), (True, "afx-ops: web_upstream reason=timeout route=status\n")):
        response = handle_request(Request("GET", "/", {}), _RaisingClient(ApiError("timeout")),
                                  report_upstream=lambda live=live: live)
        assert response.status == 502
        assert capsys.readouterr().err == expected


def test_the_route_of_each_upstream_line_is_the_matched_client_method(capsys):
    class Client:
        def log(self, n):
            raise OSError("x")

    response = handle_request(Request("GET", "/logs", {}), Client())
    assert response.status == 502
    assert capsys.readouterr().err == "afx-ops: web_upstream error=OSError route=log\n"
