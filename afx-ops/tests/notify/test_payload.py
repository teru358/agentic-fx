from __future__ import annotations

import pytest

from afx_ops.notify.payload import PayloadError, encode_event


EVENTS = {
    "ops_request_terminal": ("audit:12", {
        "endpoint_code": "policy", "result_code": "ok",
        "authenticated_principal": "operator",
    }),
    "approval_state_changed": ("approval:3", {
        "state": "rejected", "decision_code": "reject", "decided_by": "api:approver",
    }),
    "job_state_changed": ("job:" + "0123456789abcdef" * 2, {
        "job_kind": "ask", "state": "done", "result_code": "ok",
    }),
    "guard_state_changed": ("instance", {
        "guard_kind": "kill_switch", "state": "released", "generation": 2,
    }),
    "service_health_changed": ("instance", {
        "component_code": "api", "health_code": "ok",
    }),
    "event_stream_health": ("instance", {
        "failure_count": 1, "first_failed_at": None, "last_failed_at": None,
    }),
}


@pytest.mark.parametrize("code", sorted(EVENTS))
def test_each_event_code_projects_only_its_allowed_fields(code):
    ref, fields = EVENTS[code]
    event = {"id": 9, "ts": "2026-10-11T00:00:00+00:00", "code": code,
             "ref": ref, "fields": fields}

    payload = encode_event(event)

    expected = {"event_id": 9, "ts": "2026-10-11T00:00:00+00:00", "code": code,
                "ref": ref}
    expected.update({key: value for key, value in fields.items() if key != "decided_by"})
    assert payload == expected
    assert "decided_by" not in payload


def test_approval_event_without_decided_by_is_still_delivered():
    event = {"id": 4, "ts": "2026-10-11T00:00:00+00:00", "code": "approval_state_changed",
             "ref": "approval:3", "fields": {"state": "pending", "decision_code": "none"}}
    assert encode_event(event) == {"event_id": 4, "ts": "2026-10-11T00:00:00+00:00",
                                   "code": "approval_state_changed", "ref": "approval:3",
                                   "state": "pending", "decision_code": "none"}


def test_decided_by_is_checked_when_present_but_never_sent():
    event = {"id": 4, "ts": "2026-10-11T00:00:00+00:00", "code": "approval_state_changed",
             "ref": "approval:3", "fields": {"state": "pending", "decision_code": "none",
                                             "decided_by": "has spaces"}}
    with pytest.raises(PayloadError):
        encode_event(event)


@pytest.mark.parametrize("event", [
    {"id": 1, "ts": "2026-10-11T00:00:00+00:00", "code": "unknown", "ref": "x", "fields": {}},
    {"id": 1, "ts": "2026-10-11T00:00:00+00:00", "code": "service_health_changed",
     "ref": "instance", "fields": {"component_code": "api", "health_code": "ok", "extra": "x"}},
    {"id": True, "ts": "2026-10-11T00:00:00+00:00", "code": "service_health_changed",
     "ref": "instance", "fields": {"component_code": "api", "health_code": "ok"}},
    {"id": 1, "ts": "free text", "code": "service_health_changed",
     "ref": "instance", "fields": {"component_code": "api", "health_code": "ok"}},
    {"id": 1, "ts": "2026-10-11T00:00:00+00:00", "code": "service_health_changed",
     "ref": "instance", "fields": {"component_code": "api"}},
    {"id": 1, "ts": "2026-10-11T00:00:00+00:00", "code": "unknown_code",
     "ref": "audit:12", "fields": {"endpoint_code": "policy", "result_code": "ok",
                                   "authenticated_principal": "operator"}},
    {"id": 1, "ts": "2026-10-11T00:00:00+00:00", "code": "job_state_changed",
     "ref": "job:0123", "fields": {"job_kind": "ask", "state": "done", "result_code": "ok"}},
    {"id": 1, "ts": "2026-10-11T00:00:00+00:00", "code": "event_stream_health",
     "ref": "instance", "fields": {"failure_count": -1, "first_failed_at": None,
                                   "last_failed_at": None}},
])
def test_invalid_or_extra_event_content_fails_closed(event):
    with pytest.raises(PayloadError):
        encode_event(event)
