"""``X-Afx-Asserted-Actor`` の送信側: ops-bot の決定とその job 追跡だけが付け、
形式外は送る前に止める。daemon が受け取った bytes で header の有無と値を確かめる。
"""
from __future__ import annotations

import inspect

import pytest
from _oracle_daemon import JOB_ID
from _ops_harness import daemon, make_layout, pidfd_supported, run_client

from afx_ops import api_client
from afx_ops.api_client import (Actor, ApiError, BotClient, DecisionJob, NotifyClient,
                                WebClient)

pytestmark = pytest.mark.skipif(not pidfd_supported(),
                                reason="SO_PEERPIDFD の無い kernel (< 6.5)")
ACTOR = "discord:123456789012345678"
OTHER = "discord:876543210987654321"


def _actor_headers(chunks: list[bytes]) -> list[str | None]:
    found = []
    for chunk in chunks:
        head = chunk.split(b"\r\n\r\n", 1)[0].decode("ascii")
        values = [line.split(":", 1)[1].strip() for line in head.split("\r\n")[1:]
                  if line.split(":", 1)[0].lower() == "x-afx-asserted-actor"]
        assert len(values) <= 1
        found.append(values[0] if values else None)
    return found


def test_actor_header_only_from_ops_bot_client(tmp_path, child_env):
    layout = make_layout(tmp_path)
    with daemon(layout, "answer_job"):
        rc, lines, err = run_client(layout, layout.spec("ops-bot", [
            {"op": "call", "call": "events"}, {"op": "call", "call": "approvals"},
            {"op": "call", "call": "approve", "actor": ACTOR}]), child_env)
        assert rc == 0, err
        rc, _, err = run_client(layout, layout.spec("ops-web", [
            {"op": "call", "call": "status"}, {"op": "call", "call": "approvals"}]), child_env)
        assert rc == 0, err
        rc, _, err = run_client(layout, layout.spec("ops-notify", [
            {"op": "call", "call": "events"}]), child_env)
        assert rc == 0, err
        got = layout.received_at_least(6)
    assert lines[-1] == {"op": "call", "ok": True, "status": 200, "job": JOB_ID}
    # bot の読取りは付けず、決定だけ付ける。web / notify は付けない。
    assert _actor_headers(got) == [None, None, ACTOR, None, None, None]


def test_web_and_notify_clients_have_no_way_to_attach_an_actor():
    for cls in (WebClient, NotifyClient):
        for name, member in inspect.getmembers(cls, inspect.isfunction):
            if name.startswith("_"):
                continue
            params = inspect.signature(member).parameters
            assert "actor" not in params, (cls.__name__, name)
            assert all(p.annotation not in (Actor, "Actor") for p in params.values())
    # 決定と job 追跡は ops-bot だけが持つ。
    for name in ("approve", "reject", "retry", "poll", "cancel"):
        assert not hasattr(WebClient, name) and not hasattr(NotifyClient, name)
        assert hasattr(BotClient, name)


MALFORMED = {
    "16_digits": "discord:1234567890123456",
    "21_digits": "discord:123456789012345678901",
    "space": "discord: 12345678901234567",
    "tab": "discord:\t12345678901234567",
    "non_ascii_digits": "discord:１２３４５６７８９０１２３４５６７",
    "crlf_injection": "discord:12345678901234567\r\nX-Evil: 1",
    "trailing_newline": "discord:12345678901234567\n",
    "empty": "",
    "other_prefix": "slack:12345678901234567",
}


@pytest.mark.parametrize("value", list(MALFORMED.values()), ids=list(MALFORMED))
def test_malformed_actor_is_not_sent(tmp_path, child_env, value):
    with pytest.raises(ApiError) as info:
        Actor(value)
    assert info.value.reason == "invalid_actor"
    assert info.value.token_sent is False
    # 公開の決定経路に生の文字列を渡しても、接続する前に止まる。
    layout = make_layout(tmp_path)
    with daemon(layout, "answer_job"):
        rc, lines, err = run_client(layout, layout.spec("ops-bot", [
            {"op": "call", "call": "approve", "raw_actor": value}]), child_env)
        assert rc == api_client.RC_UNREACHABLE
        assert err == "afx-ops: invalid_actor\n"
        assert layout.received(expect_any=False) == []


def test_well_formed_actor_bounds_are_accepted():
    assert Actor("discord:" + "1" * 17).value == "discord:" + "1" * 17
    assert Actor("discord:" + "9" * 20).value == "discord:" + "9" * 20
    assert len(("discord:" + "9" * 20).encode()) == api_client.ACTOR_MAX_BYTES


def test_job_polling_reuses_the_decision_actor(tmp_path, child_env):
    layout = make_layout(tmp_path)
    with daemon(layout, "answer_job"):
        rc, lines, err = run_client(layout, layout.spec("ops-bot", [
            {"op": "call", "call": "approve", "actor": ACTOR},
            {"op": "call", "call": "poll", "job": JOB_ID, "actor": ACTOR},
            {"op": "call", "call": "cancel", "job": JOB_ID, "actor": ACTOR}]), child_env)
        got = layout.received_at_least(3)
    assert rc == 0, err
    assert [line["status"] for line in lines] == [200, 200, 200]
    assert _actor_headers(got) == [ACTOR, ACTOR, ACTOR]
    heads = [chunk.split(b"\r\n", 1)[0] for chunk in got]
    assert heads == [b"POST /v1/approvals/7/approve HTTP/1.1",
                     f"GET /v1/jobs/{JOB_ID} HTTP/1.1".encode(),
                     f"POST /v1/jobs/{JOB_ID}/cancel HTTP/1.1".encode()]


def test_decision_job_carries_the_actor_it_was_created_with(tmp_path):
    layout = make_layout(tmp_path)
    endpoint = api_client.Endpoint(layout.socket_path, layout.lock_path)
    client = BotClient.from_key_files(endpoint, key_file=layout.keys / "ops-bot.token",
                                      ready_file=layout.keys / ".ready")
    with daemon(layout, "answer_job"):
        decision = client.approve(Actor(ACTOR), 7, payload_sha256="a" * 64)
        assert decision.job == DecisionJob(JOB_ID, Actor(ACTOR))
        # 追跡は job が持つ actor で送られ、別 actor を差し込む引数は無い。
        assert list(inspect.signature(client.poll).parameters) == ["job"]
        assert client.poll(decision.job).status == 200
        with pytest.raises(ApiError):
            client.poll(DecisionJob(JOB_ID, OTHER))  # type: ignore[arg-type]
        got = layout.received_at_least(2)
    assert _actor_headers(got) == [ACTOR, ACTOR]


# ------------------------------------------------ actor を送る経路は BotClient だけ

def _offline_client(cls, tmp_path):
    """接続できない endpoint の client。接続前に止まるかどうかを reason で見分ける。"""
    endpoint = api_client.Endpoint(tmp_path / "none.sock", tmp_path / "none.lock")
    return cls(endpoint, "token-0123456789")


def test_base_request_path_takes_no_actor(tmp_path):
    assert "actor" not in inspect.signature(api_client._Client._call).parameters
    for cls in (WebClient, NotifyClient):
        client = _offline_client(cls, tmp_path)
        with pytest.raises(TypeError):
            client._call("GET", "/v1/status", actor=Actor(ACTOR))
        # 継承で届く名前にも、actor を受ける関数・属性が無い。
        for name in dir(cls):
            if name.startswith("__"):
                continue
            member = getattr(cls, name)
            assert "actor" not in name.lower(), (cls.__name__, name)
            if inspect.isfunction(member):
                assert not any("actor" in p.lower()
                               for p in inspect.signature(member).parameters), (cls.__name__, name)


class _CrlfActor(Actor):
    """形式を通った Actor を継承して .value だけ差し替える偽物。"""

    def __init__(self, value: str) -> None:
        self._fake = value

    @property
    def value(self) -> str:  # type: ignore[override]
        return self._fake


class _DuckActor:
    def __init__(self, value) -> None:
        self.value = value


FAKE_VALUES = [
    "discord:12345678901234567\r\nX-Evil: 1",
    "discord:12345678901234567\n",
    "slack:12345678901234567",
    "discord:１２３４５６７８９０１２３４５６７",
    b"discord:12345678901234567",
    object(),
    None,
]


@pytest.mark.parametrize("value", FAKE_VALUES, ids=range(len(FAKE_VALUES)))
@pytest.mark.parametrize("fake_cls", [_CrlfActor, _DuckActor], ids=["subclass", "duck"])
def test_encoder_rechecks_the_actor_before_building_a_header(value, fake_cls):
    with pytest.raises(ApiError) as info:
        api_client._encode("GET", "/v1/jobs/x", "token", None, fake_cls(value))
    assert info.value.reason == "invalid_actor"
    assert info.value.token_sent is False


@pytest.mark.parametrize("value", FAKE_VALUES[:4], ids=range(4))
def test_bot_client_rejects_a_fake_actor_before_connecting(tmp_path, value):
    client = _offline_client(BotClient, tmp_path)
    job_id = "0" * 32
    # 接続しようとしていれば no_socket になる。invalid_actor なら接続前に止まっている。
    for call in (lambda: client.poll(DecisionJob(job_id, _CrlfActor(value))),
                 lambda: client.cancel(DecisionJob(job_id, _CrlfActor(value))),
                 lambda: client.approve(_CrlfActor(value), 7, payload_sha256="a" * 64),
                 lambda: client.reject(_CrlfActor(value), 7)):
        with pytest.raises(ApiError) as info:
            call()
        assert info.value.reason == "invalid_actor"
