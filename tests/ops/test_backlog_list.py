"""B1 backlog list contracts at the store, service, route, and client boundaries."""
from __future__ import annotations

import io

import pytest

from agentic_fx.ops import client
from agentic_fx.ops.api_server import Request, _call, _match
from agentic_fx.ops.contracts import ErrorCode, OpsError, Principal

class _StubApi:
    """Records the client boundary while returning prepared replies."""

    def __init__(self, root, replies):
        self.root, self.principal, self.replies = root, Principal.OPERATOR, list(replies)
        self.sent = []

    def request(self, method, path, *, body=None, params=None, idempotent=False):
        self.sent.append((method, path, body, params, idempotent))
        return self.replies.pop(0)


def _ok(data):
    return client.Reply(200, data, None)


def _insert(env, *, idea, status="open", last_result=None, updated_at="2026-10-05T00:00:00+00:00"):
    env.conn.execute(
        "INSERT INTO improvement_backlog "
        "(idea,source,status,created_at,updated_at,idea_norm,last_result) VALUES (?,?,?,?,?,?,?)",
        (idea, "user", status, updated_at, updated_at, idea, last_result))
    env.conn.commit()


def test_backlog_list_all_includes_terminal_and_unknown_statuses(ops_env):
    for status in ("done", "rejected", "archived"):
        _insert(ops_env, idea=status, status=status)
    service = ops_env.service()
    assert service.list_backlog(Principal.OPERATOR)["items"] == []
    data = service.list_backlog(Principal.OPERATOR, all_statuses=True, limit=500)
    assert {item["status"] for item in data["items"]} == {"done", "rejected", "archived"}


@pytest.mark.parametrize("size,truncated", [(4095, False), (4096, False), (4097, True)])
def test_backlog_list_limits_redacted_utf8_text_without_breaking_characters(ops_env, size, truncated):
    _insert(ops_env, idea="あ" * (size // 3) + "a" * (size % 3), last_result="b" * size)
    item = ops_env.service().list_backlog(Principal.OPERATOR)["items"][0]
    for key in ("idea", "last_result"):
        value = item[key]
        assert value.encode("utf-8").decode("utf-8") == value
        assert len(value.encode("utf-8")) <= 4096
        assert item[f"{key}_truncated"] is truncated
        assert value.endswith("…[truncated]") is truncated


def test_backlog_list_redacts_and_normalizes_free_text(ops_env):
    _insert(ops_env, idea="line one\napi_key=secret-value", last_result="x\x1b[31my")
    item = ops_env.service().list_backlog(Principal.OPERATOR)["items"][0]
    assert item["idea"] == "line one api_key=[redacted]"
    assert item["last_result"] == "x[31my"


def test_backlog_list_route_dispatches_only_valid_query_values():
    """The GET route must pass the parsed bool and bounded limit to the service."""
    calls = []

    class _Ops:
        def list_backlog(self, principal, *, all_statuses, limit, deadline):
            calls.append((principal, all_statuses, limit, deadline))
            return {"items": [], "limit": limit, "has_more": False}

    route, params = _match("GET", "/v1/backlog", "uds")
    request = Request("GET", "/v1/backlog", {"all": "true", "limit": "500"}, {}, b"")
    assert _call(_Ops(), route, params, request, Principal.OPERATOR, 12.0) == {
        "items": [], "limit": 500, "has_more": False}
    assert calls == [(Principal.OPERATOR, True, 500, 12.0)]
    bad = Request("GET", "/v1/backlog", {"all": "yes"}, {}, b"")
    with pytest.raises(OpsError) as caught:
        _call(_Ops(), route, params, bad, Principal.OPERATOR, 12.0)
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT


def test_backlog_list_cli_uses_one_safe_line_per_value_and_200_character_limit(tmp_path):
    data = {"items": [{"id": 1, "status": "open", "source": "user", "updated_at": "now",
                       "idea": "x" * 201 + "\nforged", "idea_truncated": False,
                       "last_result": "y" * 201 + "\x1b[31mforged", "last_result_truncated": False}],
            "limit": 100, "has_more": False}
    api = _StubApi(tmp_path, [_ok(data)])
    out = io.StringIO()
    assert client.Runner(api, stdin=io.StringIO(), stdout=out).run(
        client.parse(["backlog", "list"])) == 0
    lines = out.getvalue().splitlines()
    assert len(lines) == 2 and all("forged" not in line and "\x1b" not in line for line in lines)
    assert lines[0].endswith("x" * 199 + "…") and lines[1].endswith("y" * 199 + "…")


@pytest.mark.parametrize("size,expected", [(199, "x" * 199), (200, "x" * 200),
                                             (201, "x" * 199 + "…")])
def test_backlog_list_cli_boundary_sizes(tmp_path, size, expected):
    api = _StubApi(tmp_path, [_ok({"items": [{"id": 1, "status": "open", "source": "user",
                                                "updated_at": "now", "idea": "x" * size,
                                                "idea_truncated": False, "last_result": None,
                                                "last_result_truncated": False}],
                                  "limit": 100, "has_more": False})])
    out = io.StringIO()
    client.Runner(api, stdin=io.StringIO(), stdout=out).run(client.parse(["backlog", "list"]))
    assert out.getvalue().rstrip().endswith(expected)


def test_backlog_add_alias_is_display_only_and_does_not_change_the_request(tmp_path):
    canonical = client.parse(["backlog", "add", "x"])
    alias = client.parse(["improve", "add", "x"])
    assert (canonical.name, canonical.args, canonical.via_alias) == ("backlog_add", ["x"], None)
    assert (alias.name, alias.args, alias.via_alias) == ("backlog_add", ["x"], "improve add")
    canonical_api, alias_api = _StubApi(tmp_path, [_ok({"id": 1})]), _StubApi(tmp_path, [_ok({"id": 1})])
    client.Runner(canonical_api, stdin=io.StringIO(), stdout=io.StringIO()).run(canonical)
    alias_out, alias_err = io.StringIO(), io.StringIO()
    client.Runner(alias_api, stdin=io.StringIO(), stdout=alias_out, stderr=alias_err).run(alias)
    assert canonical_api.sent == alias_api.sent
    assert "via_alias" not in repr(alias_api.sent)
    assert "afx ctl improve add" in alias_err.getvalue()




def test_backlog_list_parse_defaults_and_all_flag():
    assert client.parse(["backlog", "list"]).args == [False, 100]
    assert client.parse(["backlog", "list", "--all"]).args == [True, 100]
    assert client.parse(["backlog", "list", "--limit", "7", "--all"]).args == [True, 7]


@pytest.mark.parametrize("limit,ok", [("0", False), ("1", True), ("499", True), ("500", True),
                                      ("501", False), ("x", False)])
def test_backlog_list_parse_limit_boundaries(limit, ok):
    tokens = ["backlog", "list", "--limit", limit]
    if ok:
        assert client.parse(tokens).args == [False, int(limit)]
    else:
        with pytest.raises(client.ClientError):
            client.parse(tokens)


@pytest.mark.parametrize("tokens", [["backlog", "list", "--all", "--all"],
                                    ["backlog", "list", "--limit"],
                                    ["backlog", "list", "extra"]])
def test_backlog_list_parse_rejects_malformed_flags(tokens):
    with pytest.raises(client.ClientError):
        client.parse(tokens)


@pytest.mark.parametrize("tokens,expected", [
    (["backlog", "list"], {"all": "false", "limit": 100}),
    (["backlog", "list", "--all", "--limit", "500"], {"all": "true", "limit": 500}),
])
def test_backlog_list_cli_sends_the_query_to_the_api(tmp_path, tokens, expected):
    api = _StubApi(tmp_path, [_ok({"items": [], "limit": 100, "has_more": False})])
    client.Runner(api, stdin=io.StringIO(), stdout=io.StringIO()).run(client.parse(tokens))
    assert api.sent == [("GET", "/v1/backlog", None, expected, False)]


@pytest.mark.parametrize("limit", [0, 501, True, "5"])
def test_backlog_list_service_rejects_out_of_range_limits(ops_env, limit):
    with pytest.raises(OpsError) as caught:
        ops_env.service().list_backlog(Principal.OPERATOR, limit=limit)
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize("rows,limit,count,has_more", [(499, 499, 499, False), (500, 499, 499, True),
                                                       (500, 500, 500, False), (501, 500, 500, True)])
def test_backlog_list_service_limit_and_has_more_at_the_boundaries(ops_env, rows, limit, count, has_more):
    ops_env.conn.executemany(
        "INSERT INTO improvement_backlog "
        "(idea,source,status,created_at,updated_at,idea_norm) VALUES (?,?,?,?,?,?)",
        [(str(i), "user", "open", "2026-10-05T00:00:00+00:00", "2026-10-05T00:00:00+00:00", str(i))
         for i in range(rows)])
    ops_env.conn.commit()
    data = ops_env.service().list_backlog(Principal.OPERATOR, limit=limit)
    assert (len(data["items"]), data["has_more"], data["limit"]) == (count, has_more, limit)
    ids = [item["id"] for item in data["items"]]
    assert ids == sorted(ids, reverse=True)


def test_backlog_list_service_issues_one_select_for_the_page(ops_env):
    _insert(ops_env, idea="a")
    statements = []
    ops_env.conn.set_trace_callback(statements.append)
    try:
        ops_env.service().list_backlog(Principal.OPERATOR, limit=5)
    finally:
        ops_env.conn.set_trace_callback(None)
    selects = [s for s in statements if "improvement_backlog" in s]
    assert len(selects) == 1 and "LIMIT 6" in selects[0]


def test_backlog_list_collapses_every_control_character_into_one_line(ops_env):
    _insert(ops_env, idea="a\r\nb\tc\x00d\x1b[2Je f\x85g")
    item = ops_env.service().list_backlog(Principal.OPERATOR)["items"][0]
    assert len(item["idea"].splitlines()) == 1
    assert not any(ord(ch) < 32 or 0x7F <= ord(ch) < 0xA0 for ch in item["idea"])


def test_backlog_list_truncation_applies_after_redaction(ops_env):
    _insert(ops_env, idea="x" * 5000 + " api_key=secret-value")
    item = ops_env.service().list_backlog(Principal.OPERATOR)["items"][0]
    assert item["idea_truncated"] is True and "secret-value" not in item["idea"]
    assert len(item["idea"].encode("utf-8")) <= 4096


def test_alias_message_comes_from_the_surface_table_and_stays_off_stdout(tmp_path):
    from agentic_fx.cli_surface import ALIASES
    alias = next(a for a in ALIASES if a.parser_alias == "improve add")
    err, out = io.StringIO(), io.StringIO()
    api = _StubApi(tmp_path, [_ok({"id": 1})])
    client.Runner(api, stdin=io.StringIO(), stdout=out, stderr=err).run(
        client.parse(["improve", "add", "x"]))
    assert alias.canonical_usage.split(" <", 1)[0] in err.getvalue()
    assert "旧名" not in out.getvalue()
    quiet = io.StringIO()
    client.Runner(_StubApi(tmp_path, [_ok({"id": 1})]), stdin=io.StringIO(), stdout=io.StringIO(),
                  stderr=quiet).run(client.parse(["backlog", "add", "x"]))
    assert quiet.getvalue() == ""
