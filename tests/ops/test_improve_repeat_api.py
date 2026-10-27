"""POST /v1/improve/runs の本文拡張: routing、型、未知 key。"""
from __future__ import annotations

import threading

import pytest

from agentic_fx.core.improve_supervisor import RepeatResult
from agentic_fx.ops.contracts import Principal
from agentic_fx.ops.service import ENDPOINTS

from ._api import make_api
from ._improve_repeat_fake import FakeSupervisor


@pytest.fixture
def repeat_api(ops_env):
    gate = threading.Event()
    sup = FakeSupervisor(lambda **kw: (gate.wait(10), RepeatResult("max_runs", 1, 3))[1])
    api_env = make_api(ops_env, service_kwargs={"improve_supervisor": sup})
    api_env.sup = sup
    yield api_env
    gate.set()
    for client in api_env.clients:
        client.close()
    api_env.server.stop(5.0)


def _post(api, body, key="k"):
    client = api.client(Principal.OPERATOR)
    return client.post("/v1/improve/runs", json=body, headers={"Idempotency-Key": key})


def test_the_endpoint_table_is_unchanged():
    endpoint = ENDPOINTS["improve"]
    assert (endpoint.route, endpoint.scope.value, endpoint.idempotent) == (
        "POST /v1/improve/runs", "operate", True)
    assert len(ENDPOINTS) == 30


def test_an_empty_body_is_the_old_call(repeat_api):
    response = _post(repeat_api, {})
    assert response.status_code == 202, response.text
    assert repeat_api.sup.repeat_calls == []


def test_a_request_without_any_body_is_the_old_call(repeat_api):
    client = repeat_api.client(Principal.OPERATOR)
    response = client.post("/v1/improve/runs", headers={"Idempotency-Key": "k"})
    assert response.status_code == 202, response.text


@pytest.mark.parametrize("body,mode", [
    ({"max_runs": 2}, "repeat"), ({"duration_sec": 600}, "repeat"),
    ({"duration_sec": 600, "max_runs": 2, "comment": "メモ"}, "repeat"),
    ({"comment": "メモ"}, "one-shot")])
def test_valid_bodies_reach_the_service(repeat_api, body, mode):
    response = _post(repeat_api, body)
    assert response.status_code == 202, response.text
    if mode == "repeat":
        assert repeat_api.sup.entered.wait(5)
        call = repeat_api.sup.repeat_calls[0]
        assert call["max_runs"] == body.get("max_runs")
        assert call["duration_sec"] == body.get("duration_sec")
    else:
        deadline = threading.Event()
        deadline.wait(0.3)
        assert repeat_api.sup.repeat_calls == []
        assert repeat_api.sup.manual_calls[0]["comment"] == "メモ"


@pytest.mark.parametrize("body", [
    {"unknown": 1}, {"deadline": 1}, {"principal": "x"}, {"idempotency_key": "x"}, {"max_runs": 1, "extra": True}, {"max_runs": "2"}, {"max_runs": None},
    {"max_runs": 0}, {"max_runs": 25}, {"max_runs": True}, {"duration_sec": 59},
    {"duration_sec": 86401}, {"duration_sec": "600"}, {"duration_sec": None},
    {"duration_sec": 600.5}, {"comment": 5}, {"comment": ""}, {"comment": None},
    {"comment": "a\u0000b"}, {"comment": ["x"]}])
def test_bad_bodies_are_400_invalid_argument_and_leave_no_audit_row(repeat_api, ops_env, body):
    response = _post(repeat_api, body)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_argument"
    assert ops_env.ops_rows() == []
    assert repeat_api.sup.repeat_calls == [] and repeat_api.sup.manual_calls == []
