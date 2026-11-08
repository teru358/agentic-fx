"""`afx ctl improve runs [--limit N]`: 構文、問い合わせ、表示、route。"""
from __future__ import annotations

import io

import pytest

from agentic_fx.ops import client
from agentic_fx.ops.api_server import Request, _call, _match
from agentic_fx.ops.contracts import ErrorCode, OpsError, Principal


class _StubApi:
    def __init__(self, root, replies):
        self.root, self.principal, self.replies = root, Principal.OPERATOR, list(replies)
        self.sent = []

    def request(self, method, path, *, body=None, params=None, idempotent=False):
        self.sent.append((method, path, body, params, idempotent))
        return self.replies.pop(0)


def _ok(data):
    return client.Reply(200, data, None)


def _run(tmp_path, data, tokens=("improve", "runs")):
    api = _StubApi(tmp_path, [_ok(data)])
    out = io.StringIO()
    rc = client.Runner(api, stdin=io.StringIO(), stdout=out).run(
        client.parse(list(tokens)))
    return rc, out.getvalue(), api


ROW = {"id": 812, "started_at": "2026-11-08T03:00:00+00:00",
       "finished_at": "2026-11-08T03:06:12+00:00", "trigger": "scheduled",
       "result_code": "gate_failed", "reason_code": "unprofitable",
       "plugin": "london_pullback", "failed_stage": "in_sample",
       "failed_conditions": ["pf"], "failed_pair_count": 1, "approval_id": None}


def test_parse_defaults_and_limit():
    assert client.parse(["improve", "runs"]).name == "improve_runs"
    assert client.parse(["improve", "runs"]).args == [50]
    assert client.parse(["improve", "runs", "--limit", "7"]).args == [7]
    assert client.parse(["improve", "runs", "--limit", "100"]).args == [100]


@pytest.mark.parametrize("tokens", [
    ["improve", "runs", "--limit", "0"], ["improve", "runs", "--limit", "101"],
    ["improve", "runs", "--limit"], ["improve", "runs", "--limit", "x"],
    ["improve", "runs", "--limit", "3", "--limit", "4"],
    ["improve", "runs", "extra"], ["improve", "runs", "--all"]])
def test_parse_rejects_malformed_input_before_sending(tokens):
    with pytest.raises(client.ClientError):
        client.parse(tokens)


def test_the_improve_start_command_is_unchanged():
    assert client.parse(["improve"]).name == "improve"
    assert client.parse(["improve", "-n", "2"]).name == "improve"
    assert client.parse(["improve", "add", "idea"]).name == "backlog_add"


@pytest.mark.parametrize("tokens,expected", [
    (("improve", "runs"), {"limit": 50}),
    (("improve", "runs", "--limit", "9"), {"limit": 9})])
def test_the_query_is_sent_to_the_list_route(tmp_path, tokens, expected):
    rc, _, api = _run(tmp_path, {"runs": [], "has_more": False}, tokens)
    assert rc == 0
    assert api.sent == [("GET", "/v1/improve/runs", None, expected, False)]


def test_one_safe_line_per_run(tmp_path):
    evil = {**ROW, "plugin": "p\x1b[31mforged\nline2",
            "reason_code": "x" * 500, "trigger": "a\nb"}
    rc, text, _ = _run(tmp_path, {"runs": [ROW, evil], "has_more": True})
    lines = text.splitlines()
    assert rc == 0 and len(lines) == 3
    assert "\x1b" not in text and "forged\nline2" not in text
    assert lines[0].startswith("#812 ")
    assert "gate_failed/unprofitable" in lines[0]
    assert "london_pullback" in lines[0] and "床未達 in_sample [pf] 1 pair" in lines[0]
    assert lines[2].startswith("…")


def test_an_approval_id_is_shown_and_no_floor_text_without_a_failed_stage(
        tmp_path):
    row = {**ROW, "failed_stage": None, "failed_conditions": [],
           "failed_pair_count": None, "approval_id": 41,
           "result_code": "submitted", "reason_code": None}
    _, text, _ = _run(tmp_path, {"runs": [row], "has_more": False})
    assert "承認 #41" in text and "床未達" not in text


def test_zero_rows(tmp_path):
    rc, text, _ = _run(tmp_path, {"runs": [], "has_more": False})
    assert (rc, text) == (0, "improve: 0 件\n")


def test_times_use_the_display_timezone(tmp_path):
    _, text, _ = _run(tmp_path, {"runs": [ROW], "has_more": False,
                                 "display_timezone": "Asia/Tokyo"})
    assert "2026-11-08 12:06:12 +09:00" in text


def test_route_dispatch_validates_the_limit_before_the_service(ops_env):
    request = Request("GET", "/v1/improve/runs", {"limit": "0"}, {}, b"")
    route, params = _match("GET", "/v1/improve/runs", "uds")
    assert route.handler == "improve_runs" and params == {}
    svc = ops_env.service()
    with pytest.raises(OpsError) as raised:
        _call(svc, route, params, request, Principal.OPERATOR, 5.0)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    bad = Request("GET", "/v1/improve/runs", {"limit": "abc"}, {}, b"")
    with pytest.raises(OpsError):
        _call(svc, route, params, bad, Principal.OPERATOR, 5.0)
    good = Request("GET", "/v1/improve/runs", {"limit": "3"}, {}, b"")
    assert _call(svc, route, params, good, Principal.OPERATOR, 5.0) == {
        "runs": [], "has_more": False}
    default = Request("GET", "/v1/improve/runs", {}, {}, b"")
    assert _call(svc, route, params, default, Principal.OPERATOR, 5.0)[
        "has_more"] is False


def test_the_post_route_on_the_same_path_is_still_the_improve_start():
    route, _ = _match("POST", "/v1/improve/runs", "uds")
    assert route.handler == "improve"
