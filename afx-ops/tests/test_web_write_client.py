"""``WebWriteClient`` (ops-web-write): 送る request line・header・body と、actor の規則。

実 UDS の fake daemon が受け取った bytes で確かめる。actor は呼出しごとに ``web:<user>`` を
受けて検査し、形式外は鍵を 1 byte も送らずに止まる。
"""
from __future__ import annotations

import inspect
import json

import pytest
from _keydir import token_for
from _oracle_daemon import JOB_ID
from _ops_harness import daemon, make_layout, pidfd_supported

from afx_ops import api_client
from afx_ops.api_client import (Actor, ApiError, BotClient, NotifyClient, WebClient,
                                WebWriteClient)
from afx_ops.web.write import WriteClient

pytestmark = pytest.mark.skipif(not pidfd_supported(),
                                reason="SO_PEERPIDFD の無い kernel (< 6.5)")
ALICE = "web:alice"
DIGEST = "a" * 64
KEY = "afxweb-" + "b" * 64


def _setup(tmp_path):
    layout = make_layout(tmp_path)
    keys = layout.keys
    endpoint = api_client.Endpoint(layout.socket_path, layout.lock_path)
    client = WebWriteClient.from_key_files(
        endpoint, key_file=keys / "ops-web-write.token", ready_file=keys / ".ready",
        _for_tests=True)
    return layout, client


def _parse(chunk: bytes) -> tuple[str, dict[str, str], bytes]:
    head, _, body = chunk.partition(b"\r\n\r\n")
    lines = head.decode("ascii").split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        assert name.lower() not in headers, name
        headers[name.lower()] = value.strip()
    return lines[0], headers, body


def test_approve_sends_the_decision_with_the_web_actor(tmp_path):
    layout, client = _setup(tmp_path)
    with daemon(layout, "answer_job"):
        reply = client.approve(ALICE, 7, payload_sha256=DIGEST)
        got = layout.received_at_least(1)
    assert reply.status == 200 and reply.data["job_id"] == JOB_ID
    line, headers, body = _parse(got[0])
    assert line == "POST /v1/approvals/7/approve HTTP/1.1"
    assert headers["x-afx-asserted-actor"] == ALICE
    assert headers["authorization"] == "Bearer " + token_for("ops-web-write")
    assert "idempotency-key" not in headers
    assert json.loads(body) == {"payload_sha256": DIGEST}
    assert int(headers["content-length"]) == len(body)


def test_reject_sends_an_empty_body_without_a_reason_and_the_reason_when_given(tmp_path):
    layout, client = _setup(tmp_path)
    with daemon(layout, "answer_job"):
        client.reject(ALICE, 8, reason=None)
        client.reject(ALICE, 9, reason="手動")
        got = layout.received_at_least(2)
    parsed = [_parse(chunk) for chunk in got]
    assert [p[0] for p in parsed] == ["POST /v1/approvals/8/reject HTTP/1.1",
                                      "POST /v1/approvals/9/reject HTTP/1.1"]
    assert [json.loads(p[2]) for p in parsed] == [{}, {"reason": "手動"}]
    assert all(p[1]["x-afx-asserted-actor"] == ALICE for p in parsed)


def test_improve_run_sends_the_body_the_actor_and_the_idempotency_key(tmp_path):
    layout, client = _setup(tmp_path)
    body = {"duration_sec": 600, "max_runs": 3, "comment": "メモ"}
    with daemon(layout, "answer_job"):
        reply = client.improve_run(ALICE, body, idempotency_key=KEY)
        client.improve_run(ALICE, {}, idempotency_key=KEY)
        got = layout.received_at_least(2)
    assert reply.status == 200
    line, headers, raw = _parse(got[0])
    assert line == "POST /v1/improve/runs HTTP/1.1"
    assert headers["x-afx-asserted-actor"] == ALICE
    assert headers["idempotency-key"] == KEY
    assert json.loads(raw) == body
    # 空の body は本文なしの呼び出しではなく {} を送る
    assert json.loads(_parse(got[1])[2]) == {}


def test_reads_use_the_same_key_and_carry_no_actor(tmp_path):
    layout, client = _setup(tmp_path)
    with daemon(layout, "answer_job"):
        client.status()
        client.approvals()
        client.approval(5)
        client.approval_change(5)
        client.plugins()
        client.plugin("sma")
        client.log(n=3)
        client.activity(n=2, category="IMPROVE")
        got = layout.received_at_least(8)
    parsed = [_parse(chunk) for chunk in got]
    assert [p[0] for p in parsed] == [
        "GET /v1/status HTTP/1.1", "GET /v1/approvals?status=pending&limit=20 HTTP/1.1",
        "GET /v1/approvals/5 HTTP/1.1", "GET /v1/approvals/5/change HTTP/1.1",
        "GET /v1/plugins HTTP/1.1", "GET /v1/plugins/sma HTTP/1.1",
        "GET /v1/log?n=3 HTTP/1.1", "GET /v1/activity?n=2&category=IMPROVE HTTP/1.1"]
    for _, headers, _ in parsed:
        assert "x-afx-asserted-actor" not in headers
        assert "idempotency-key" not in headers
        assert headers["authorization"] == "Bearer " + token_for("ops-web-write")


MALFORMED = [
    "", "web:", "web:A", "web:1a", "web:-a", "web:a b", "web:a\tb", "web:alice\n",
    "web:alice\r\nX-Evil: 1", "web:" + "a" * 33, "Web:alice", "web:ａ", "web:a.b",
    "discord:123456789012345678", "slack:alice", "alice", None, 5, b"web:alice",
]


@pytest.mark.parametrize("value", MALFORMED, ids=[repr(v)[:30] for v in MALFORMED])
def test_a_malformed_actor_is_not_sent_by_any_write(tmp_path, value):
    layout, client = _setup(tmp_path)
    with daemon(layout, "answer_job"):
        for call in (lambda: client.approve(value, 7, payload_sha256=DIGEST),
                     lambda: client.reject(value, 7, reason=None),
                     lambda: client.improve_run(value, {}, idempotency_key=KEY)):
            with pytest.raises(ApiError) as info:
                call()
            assert info.value.reason == "invalid_actor"
            assert info.value.token_sent is False
        assert layout.received(expect_any=False) == []


@pytest.mark.parametrize("key", ["", "a b", "k\r\nX: 1", "k" * 201, "é", None, 5])
def test_a_malformed_idempotency_key_is_not_sent(tmp_path, key):
    layout, client = _setup(tmp_path)
    with daemon(layout, "answer_job"):
        with pytest.raises(ApiError) as info:
            client.improve_run(ALICE, {}, idempotency_key=key)
        assert info.value.reason == "invalid_idempotency_key"
        assert info.value.token_sent is False
        assert layout.received(expect_any=False) == []


def test_a_bot_actor_object_cannot_be_used_by_the_web_write_client(tmp_path):
    layout, client = _setup(tmp_path)
    with daemon(layout, "answer_job"):
        with pytest.raises(ApiError):
            client.approve(Actor("discord:123456789012345678"), 7,  # type: ignore[arg-type]
                           payload_sha256=DIGEST)
        assert layout.received(expect_any=False) == []


def test_a_web_actor_object_cannot_be_used_by_the_bot_client(tmp_path):
    layout = make_layout(tmp_path)
    endpoint = api_client.Endpoint(layout.socket_path, layout.lock_path)
    bot = BotClient.from_key_files(endpoint, key_file=layout.keys / "ops-bot.token",
                                   ready_file=layout.keys / ".ready", _for_tests=True)
    web_actor = Actor(ALICE, "ops-web-write")
    with daemon(layout, "answer_job"):
        with pytest.raises(ApiError) as info:
            bot.approve(web_actor, 7, payload_sha256=DIGEST)
        assert info.value.reason == "invalid_actor"
        assert layout.received(expect_any=False) == []


def test_actor_rules_per_principal():
    assert Actor("discord:" + "9" * 20).value == "discord:" + "9" * 20
    with pytest.raises(ApiError):
        Actor(ALICE)                      # 既定は bot の規則
    assert Actor(ALICE, "ops-web-write").value == ALICE
    assert Actor("web:" + "a" * 32, "ops-web-write").value == "web:" + "a" * 32
    assert len(("web:" + "a" * 32).encode()) == api_client.ACTOR_RULES["ops-web-write"][1]
    assert api_client.ACTOR_RULES["ops-bot"][1] == api_client.ACTOR_MAX_BYTES == 28
    for principal in ("operator", "approver", "ops-web", "ops-notify", "", None):
        with pytest.raises(ApiError):
            Actor(ALICE, principal)       # type: ignore[arg-type]


def test_the_write_actor_pattern_is_the_web_user_pattern():
    from afx_ops.config import WEB_USER_PATTERN
    pattern = api_client.ACTOR_RULES["ops-web-write"][0]
    assert pattern.pattern == "web:" + WEB_USER_PATTERN.pattern


# ------------------------------------------------ 公開の形

def test_write_client_satisfies_the_write_client_protocol():
    for name in ("approval", "approve", "reject", "improve_run"):
        mine = inspect.signature(getattr(WebWriteClient, name))
        spec = inspect.signature(getattr(WriteClient, name))
        assert list(mine.parameters) == list(spec.parameters), name
        for key, param in spec.parameters.items():
            assert mine.parameters[key].kind == param.kind, (name, key)


def test_write_client_public_methods_are_the_read_set_plus_three_writes():
    reads = {name for name, member in WebClient.__dict__.items()
             if not name.startswith("_") and inspect.isfunction(member)}
    own = {name for name, member in WebWriteClient.__dict__.items()
           if not name.startswith("_") and inspect.isfunction(member)}
    assert own == {"approve", "reject", "improve_run"}
    assert WebWriteClient.principal == "ops-web-write"
    assert reads <= {name for name in dir(WebWriteClient)}
    # retry・job の追跡と取消・actor を足す口は持たない
    for name in ("retry", "poll", "cancel", "_call_as", "decide"):
        assert not hasattr(WebWriteClient, name), name


def test_actor_is_never_stored_and_only_the_three_writes_accept_one():
    for name, member in inspect.getmembers(WebWriteClient, inspect.isfunction):
        params = inspect.signature(member).parameters
        has_actor = any("actor" in p.lower() for p in params)
        assert has_actor is (name in {"approve", "reject", "improve_run", "_write"}), name
    assert "actor" not in inspect.signature(api_client._Client._call).parameters
    assert not any("actor" in slot for slot in getattr(WebWriteClient, "__slots__", ()))
    assert "actor" not in WebWriteClient.from_token.__func__.__code__.co_varnames


def test_the_read_only_clients_are_unchanged():
    for cls in (WebClient, NotifyClient):
        for name in ("approve", "reject", "improve_run"):
            assert not hasattr(cls, name)
    assert WebClient.principal == "ops-web" and NotifyClient.principal == "ops-notify"


def test_the_key_is_read_for_ops_web_write_only_from_its_own_file(tmp_path):
    from afx_ops.keys import KeyReadError
    layout, _ = _setup(tmp_path)
    endpoint = api_client.Endpoint(layout.socket_path, layout.lock_path)
    keys = layout.keys
    with pytest.raises(KeyReadError):
        WebWriteClient.from_key_files(endpoint, key_file=keys / "ops-web.token",
                                      ready_file=keys / ".ready", _for_tests=True)
    with pytest.raises(KeyReadError):
        WebClient.from_key_files(endpoint, key_file=keys / "ops-web-write.token",
                                 ready_file=keys / ".ready", _for_tests=True)
