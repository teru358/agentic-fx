"""GET /v1/improve/runs/{id}: 終端 snapshot と run 行・承認・床未達レポートの合成。"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx import improve_notice_codes as codes
from agentic_fx.ops.contracts import (PRINCIPAL_SCOPES, ErrorCode, OpsError, Principal,
                                      Scope)
from agentic_fx.ops.service import ENDPOINTS
from agentic_fx.store import missions

from ._improve_seed import END, START, seed

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "improve_reports"
OPERATOR = Principal.OPERATOR


@pytest.fixture
def reports(ops_env):
    directory = ops_env.root / "data" / "improve_reports"
    directory.mkdir(parents=True)
    return directory


def service(env, reports=None, **kwargs):
    return env.service(improve_reports_dir=reports, **kwargs)


def put_report(reports, mission_id, source="improve-2026-10-25-2043.md", rewrite=True):
    name = f"improve-2026-10-25-{mission_id}.md"
    text = (FIXTURES / source).read_text(encoding="utf-8")
    if rewrite:
        text = text.replace("Mission 2043", f"Mission {mission_id}").replace(
            "Mission 2045", f"Mission {mission_id}")
    (reports / name).write_text(text, encoding="utf-8")
    return str(reports / name)


# ---- 形 ----

def test_scope_is_approvals_detail_and_the_endpoint_is_a_plain_read():
    endpoint = ENDPOINTS["improve_run"]
    assert endpoint.scope is Scope.APPROVALS_DETAIL
    assert endpoint.mutating is False and endpoint.idempotent is False
    assert endpoint.route == "GET /v1/improve/runs/{id}"


def test_every_principal_is_authorized_exactly_by_the_scope(ops_env):
    svc = service(ops_env)
    mission_id = seed(ops_env)
    for principal in Principal:
        if Scope.APPROVALS_DETAIL in PRINCIPAL_SCOPES[principal]:
            assert svc.improve_run(principal, mission_id)["id"] == mission_id
        else:
            with pytest.raises(OpsError) as raised:
                svc.improve_run(principal, mission_id)
            assert raised.value.code is ErrorCode.FORBIDDEN
    assert Scope.APPROVALS_DETAIL not in PRINCIPAL_SCOPES[Principal.OPS_NOTIFY]


def test_full_dto_for_a_terminal_mission(ops_env, reports):
    approval = ops_env.make_plugin_approval("sma", mission_id=1)
    path = put_report(reports, 0, rewrite=True)  # 別 mission 用の写し (読まれない)
    mission_id = seed(
        ops_env, notice=codes.NOTICE_SUBMITTED, approval_id=approval, tool_calls=11,
        report_state="published", report_path=path)
    data = service(ops_env, reports).improve_run(OPERATOR, mission_id)
    assert data == {
        "id": mission_id, "backend": "local", "model": "qwen", "elapsed_sec": 261,
        "snapshot": {"trigger": "scheduled", "result_code": "submitted",
                     "reason_code": None, "tool_calls": 11, "observation_text": None},
        "report_state": "published",
        "approval": {"id": approval, "plugin": "sma", "kind": "plugin"},
        "floor": None}
    json.dumps(data)


def test_unfinished_mission_is_a_200_with_nulls_but_keeps_backend_and_model(ops_env):
    mission_id = seed(ops_env, terminal=False, runner="claude", model="opus")
    assert service(ops_env).improve_run(OPERATOR, mission_id) == {
        "id": mission_id, "backend": "claude", "model": "opus", "elapsed_sec": None,
        "snapshot": None, "report_state": "none", "approval": None,
        "floor": None}


def test_unknown_and_non_improve_missions_are_404(ops_env):
    svc = service(ops_env)
    trade = seed(ops_env, loop="trade", terminal=False)
    for mission_id in (trade, 424242, 0, -1):
        with pytest.raises(OpsError) as raised:
            svc.improve_run(OPERATOR, mission_id)
        assert raised.value.code is ErrorCode.NOT_FOUND


def test_mission_without_a_run_row_reports_state_none(ops_env):
    mission_id = missions.start(ops_env.conn, "improve", "local", "m", START)
    data = service(ops_env).improve_run(OPERATOR, mission_id)
    assert data["report_state"] == "none" and data["snapshot"] is None


@pytest.mark.parametrize("state", ["none", "prepared", "published", "failed"])
def test_report_state_values_are_returned(ops_env, state):
    mission_id = seed(ops_env, notice=codes.NOTICE_REPORT_PUBLISHED, report_state=state)
    assert service(ops_env).improve_run(OPERATOR, mission_id)["report_state"] == state


# ---- elapsed_sec ----

def _elapsed(env, started, ended, notice=codes.NOTICE_SUBMITTED):
    mission_id = seed(env, notice=notice, started=started, ended=ended)
    return service(env).improve_run(OPERATOR, mission_id)["elapsed_sec"]


@pytest.mark.parametrize("seconds, expected", [
    (289.109, 289), (261.148, 261), (400.402, 400), (447.457, 447), (405.472, 405),
    (0.5, 1), (1.5, 2), (0.499999, 0), (0.0, 0), (289.5, 290), (289.499999, 289),
    (3600.5, 3601)])
def test_elapsed_rounds_half_up_in_integer_microseconds(ops_env, seconds, expected):
    micros = round(seconds * 1_000_000)
    assert _elapsed(ops_env, START, START + timedelta(microseconds=micros)) == expected


def test_elapsed_uses_microsecond_integers_not_floats(ops_env):
    # 0.5 秒ちょうどを float の誤差に任せると切り捨てになり得る値。
    start = datetime(2026, 10, 25, 0, 0, 0, 100000, tzinfo=timezone.utc)
    assert _elapsed(ops_env, start, start + timedelta(microseconds=500000)) == 1
    assert _elapsed(ops_env, start, start + timedelta(days=1, microseconds=500000)) == 86401


def test_elapsed_is_null_for_interrupted_end_before_start_and_unparsable(ops_env):
    assert _elapsed(ops_env, START, END, notice=codes.NOTICE_INTERRUPTED) is None
    assert _elapsed(ops_env, END, START) is None
    mission_id = seed(ops_env, notice=codes.NOTICE_SUBMITTED)
    ops_env.conn.execute("UPDATE missions SET started_at='not a time' WHERE id=?",
                         (mission_id,))
    ops_env.conn.commit()
    assert service(ops_env).improve_run(OPERATOR, mission_id)["elapsed_sec"] is None


def test_elapsed_mixed_naive_and_aware_times_do_not_crash(ops_env):
    naive = START.replace(tzinfo=None)
    assert _elapsed(ops_env, naive, END) == 261


# ---- snapshot ----

def test_observation_text_is_folded_to_one_line(ops_env):
    mission_id = seed(ops_env, notice=codes.NOTICE_OBSERVATION,
                      observation_text="line1\nline2\t\x07 end\r\n")
    snapshot = service(ops_env).improve_run(OPERATOR, mission_id)["snapshot"]
    assert snapshot["observation_text"] == "line1 line2 end"


def test_observation_text_blank_is_empty_and_null_stays_null(ops_env):
    blank = seed(ops_env, notice=codes.NOTICE_OBSERVATION, observation_text="  \n ")
    none = seed(ops_env, notice=codes.NOTICE_OBSERVATION, observation_text=None)
    literal = seed(ops_env, notice=codes.NOTICE_OBSERVATION, observation_text="-")
    svc = service(ops_env)
    assert svc.improve_run(OPERATOR, blank)["snapshot"]["observation_text"] == ""
    assert svc.improve_run(OPERATOR, none)["snapshot"]["observation_text"] is None
    assert svc.improve_run(OPERATOR, literal)["snapshot"]["observation_text"] == "-"


def test_observation_text_is_returned_whole_up_to_the_stored_limit(ops_env):
    mission_id = seed(ops_env, notice=codes.NOTICE_OBSERVATION,
                      observation_text="あ" * 2500)
    text = service(ops_env).improve_run(OPERATOR, mission_id)["snapshot"]["observation_text"]
    assert text == "あ" * codes.OBSERVATION_TEXT_MAX


def test_backend_and_model_are_folded(ops_env):
    mission_id = seed(ops_env, runner="lo\ncal", model="m\x00odel")
    data = service(ops_env).improve_run(OPERATOR, mission_id)
    assert (data["backend"], data["model"]) == ("lo cal", "model")


# ---- approval ----

def test_approval_missing_row_is_null(ops_env):
    mission_id = seed(ops_env, notice=codes.NOTICE_SUBMITTED, approval_id=999)
    assert service(ops_env).improve_run(OPERATOR, mission_id)["approval"] is None


def test_approval_without_plugin_name_has_null_plugin(ops_env):
    approval = ops_env.raw_approval("plugin", {"not_name": 1})
    mission_id = seed(ops_env, notice=codes.NOTICE_SUBMITTED, approval_id=approval)
    assert service(ops_env).improve_run(OPERATOR, mission_id)["approval"] == {
        "id": approval, "plugin": None, "kind": "plugin"}


def test_approval_plugin_name_outside_the_grammar_is_not_returned(ops_env):
    approval = ops_env.raw_approval("plugin", {"name": "Bad Name\nhttps://x.example/?k=1"})
    mission_id = seed(ops_env, notice=codes.NOTICE_SUBMITTED, approval_id=approval)
    assert service(ops_env).improve_run(OPERATOR, mission_id)["approval"]["plugin"] is None


def test_approval_with_corrupt_payload_keeps_id_and_kind(ops_env):
    cur = ops_env.conn.execute(
        "INSERT INTO approval_requests(kind,payload_json,created_at) VALUES (?,?,?)",
        ("plugin", "{not json", ops_env.wall().isoformat()))
    ops_env.conn.commit()
    mission_id = seed(ops_env, notice=codes.NOTICE_SUBMITTED, approval_id=cur.lastrowid)
    assert service(ops_env).improve_run(OPERATOR, mission_id)["approval"] == {
        "id": cur.lastrowid, "plugin": None, "kind": "plugin"}


# ---- floor ----

def _floor_mission(env, reports, source="improve-2026-10-25-2043.md", **kwargs):
    mission_id = seed(env, report_state=kwargs.pop("report_state", "published"),
                      report_path="placeholder", **kwargs)
    path = put_report(reports, mission_id, source)
    env.conn.execute("UPDATE improvement_runs SET report_path=? WHERE mission_id=?",
                     (path, mission_id))
    env.conn.commit()
    return mission_id


def test_floor_from_the_real_2043_report(ops_env, reports):
    mission_id = _floor_mission(ops_env, reports)
    assert service(ops_env, reports).improve_run(OPERATOR, mission_id)["floor"] == {
        "stages": [{"stage": "holdout", "pairs": [
            {"pair": "USDJPY", "trades": 13, "pf": 1.8458377992140615,
             "avg_r": 0.3029234868346611, "evaluable": False}]}]}


def test_floor_from_the_real_2045_report_returns_raw_floats(ops_env, reports):
    mission_id = _floor_mission(ops_env, reports, "improve-2026-10-25-2045.md")
    floor = service(ops_env, reports).improve_run(OPERATOR, mission_id)["floor"]
    assert floor["stages"] == [{"stage": "holdout", "pairs": [
        {"pair": "USDJPY", "trades": 24, "pf": 0.5145606198869912,
         "avg_r": -0.2772392430083411, "evaluable": False}]}]


@pytest.mark.parametrize("state", ["none", "prepared", "failed"])
def test_report_is_not_opened_unless_published(ops_env, reports, state, monkeypatch):
    from agentic_fx.ops import safe_open

    def boom(*args, **kwargs):
        raise AssertionError("report must not be opened")

    monkeypatch.setattr(safe_open, "read_improve_report", boom)
    mission_id = _floor_mission(ops_env, reports, report_state=state)
    data = service(ops_env, reports).improve_run(OPERATOR, mission_id)
    assert data["floor"] is None and data["report_state"] == state


@pytest.mark.parametrize("notice", [
    codes.NOTICE_INSUFFICIENT_TRADES, codes.NOTICE_SUBMITTED,
    codes.NOTICE_REPORT_PUBLISHED, codes.NOTICE_COMMIT_CRASHED])
def test_report_is_not_opened_for_other_results(ops_env, reports, notice, monkeypatch):
    from agentic_fx.ops import safe_open
    monkeypatch.setattr(safe_open, "read_improve_report",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened")))
    mission_id = _floor_mission(ops_env, reports, notice=notice)
    assert service(ops_env, reports).improve_run(OPERATOR, mission_id)["floor"] is None


def test_floor_is_null_without_a_configured_reports_directory(ops_env, reports):
    mission_id = _floor_mission(ops_env, reports)
    assert service(ops_env, None).improve_run(OPERATOR, mission_id)["floor"] is None


def test_unreadable_reports_give_null_floor(ops_env, reports, tmp_path):
    # 欠落
    gone = seed(ops_env, report_state="published",
                report_path=str(reports / "improve-2026-10-25-999.md"))
    # symlink (先に正しい内容がある)
    linked = seed(ops_env, report_state="published",
                  report_path=str(reports / "improve-2026-10-25-1000.md"))
    real = tmp_path / "real.md"
    real.write_text((FIXTURES / "improve-2026-10-25-2043.md").read_text().replace(
        "Mission 2043", f"Mission {linked}"))
    os.symlink(real, reports / f"improve-2026-10-25-{linked}.md")
    ops_env.conn.execute("UPDATE improvement_runs SET report_path=? WHERE mission_id=?",
                         (str(reports / f"improve-2026-10-25-{linked}.md"), linked))
    # basename が別 mission
    other = seed(ops_env, report_state="published", report_path="x")
    put_report(reports, other + 1)
    ops_env.conn.execute("UPDATE improvement_runs SET report_path=? WHERE mission_id=?",
                         (str(reports / f"improve-2026-10-25-{other + 1}.md"), other))
    # 配下外
    outside = tmp_path / "outside"
    outside.mkdir()
    out_id = seed(ops_env, report_state="published", report_path="x")
    ops_env.conn.execute("UPDATE improvement_runs SET report_path=? WHERE mission_id=?",
                         (str(outside / f"improve-2026-10-25-{out_id}.md"), out_id))
    (outside / f"improve-2026-10-25-{out_id}.md").write_text("x")
    ops_env.conn.commit()
    svc = service(ops_env, reports)
    for mission_id in (gone, linked, other, out_id):
        assert svc.improve_run(OPERATOR, mission_id)["floor"] is None


def test_oversized_and_non_utf8_and_malformed_reports_give_null_floor(ops_env, reports):
    big = _floor_mission(ops_env, reports)
    path = Path(ops_env.conn.execute(
        "SELECT report_path FROM improvement_runs WHERE mission_id=?", (big,)
    ).fetchone()[0])
    path.write_bytes(path.read_bytes() + b"x" * (64 * 1024))
    bad_utf8 = _floor_mission(ops_env, reports)
    Path(ops_env.conn.execute(
        "SELECT report_path FROM improvement_runs WHERE mission_id=?", (bad_utf8,)
    ).fetchone()[0]).write_bytes(b"\xff\xfe")
    malformed = _floor_mission(ops_env, reports)
    mpath = Path(ops_env.conn.execute(
        "SELECT report_path FROM improvement_runs WHERE mission_id=?", (malformed,)
    ).fetchone()[0])
    mpath.write_text(mpath.read_text().replace("failed_stage: holdout",
                                               "failed_stage: in_sample, holdout"))  # 旧形式で 2 段
    svc = service(ops_env, reports)
    for mission_id in (big, bad_utf8, malformed):
        assert svc.improve_run(OPERATOR, mission_id)["floor"] is None


def test_report_is_read_inside_the_ops_lane(ops_env, reports, monkeypatch):
    from agentic_fx.ops import safe_open
    svc = service(ops_env, reports)
    seen = {}
    real = safe_open.read_improve_report

    def spy(*args, **kwargs):
        seen["locked"] = svc._ops_lock._is_owned()
        return real(*args, **kwargs)

    monkeypatch.setattr(safe_open, "read_improve_report", spy)
    mission_id = _floor_mission(ops_env, reports)
    assert svc.improve_run(OPERATOR, mission_id)["floor"] is not None
    assert seen == {"locked": True}


def test_improve_run_past_its_deadline_is_a_request_timeout(ops_env, reports):
    import threading
    import time
    svc = service(ops_env, reports)
    mission_id = _floor_mission(ops_env, reports)
    holding, release = threading.Event(), threading.Event()

    def hold():
        with svc._ops_lane(time.monotonic() + 60):
            holding.set()
            release.wait(30)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert holding.wait(5)
    try:
        with pytest.raises(OpsError) as raised:
            svc.improve_run(OPERATOR, mission_id, deadline=time.monotonic() + 0.2)
    finally:
        release.set()
        thread.join(5)
    assert raised.value.code is ErrorCode.REQUEST_TIMEOUT


def _warnings(caplog, mission_id):
    return [r.getMessage() for r in caplog.records
            if "floor unavailable" in r.getMessage()]


def _set_path(env, mission_id, path):
    env.conn.execute("UPDATE improvement_runs SET report_path=? WHERE mission_id=?",
                     (str(path), mission_id))
    env.conn.commit()


@pytest.mark.parametrize("reason", ["missing", "malformed", "oversized", "unsafe",
                                    "unpublished"])
def test_floor_warning_names_mission_and_a_fixed_reason(ops_env, reports, caplog,
                                                        tmp_path, reason):
    import logging
    if reason == "unpublished":
        mission_id = _floor_mission(ops_env, reports, report_state="failed")
    else:
        mission_id = _floor_mission(ops_env, reports)
        path = Path(ops_env.conn.execute(
            "SELECT report_path FROM improvement_runs WHERE mission_id=?",
            (mission_id,)).fetchone()[0])
        if reason == "missing":
            path.unlink()
        elif reason == "malformed":
            path.write_text(path.read_text().replace("thresholds:", "limits:"))
        elif reason == "oversized":
            path.write_bytes(path.read_bytes() + b"x" * (64 * 1024))
        else:
            real = tmp_path / "real.md"
            real.write_bytes(path.read_bytes())
            path.unlink()
            os.symlink(real, path)
    with caplog.at_level(logging.WARNING):
        assert service(ops_env, reports).improve_run(OPERATOR, mission_id)["floor"] is None
    assert _warnings(caplog, mission_id) == [
        f"improve_run floor unavailable mission={mission_id} reason={reason}"]


def test_gate_failed_with_failed_publish_has_null_floor_and_unchanged_snapshot(
        ops_env, reports):
    mission_id = _floor_mission(ops_env, reports, report_state="failed")
    data = service(ops_env, reports).improve_run(OPERATOR, mission_id)
    assert data["report_state"] == "failed" and data["floor"] is None
    assert (data["snapshot"]["result_code"], data["snapshot"]["reason_code"]) == (
        "gate_failed", "unprofitable")


def test_report_that_fails_to_publish_keeps_the_report_snapshot(ops_env):
    mission_id = seed(ops_env, notice=codes.NOTICE_REPORT_PUBLISHED, report_state="failed")
    data = service(ops_env).improve_run(OPERATOR, mission_id)
    assert data["report_state"] == "failed"
    assert (data["snapshot"]["result_code"], data["snapshot"]["reason_code"]) == (
        "report", None)


# ---- 漏れと監査 ----

def test_response_carries_no_path_key_or_exception_text(ops_env, reports):
    approval = ops_env.make_plugin_approval("sma", mission_id=1)
    mission_id = _floor_mission(ops_env, reports, approval_id=approval)
    text = json.dumps(service(ops_env, reports).improve_run(OPERATOR, mission_id))
    assert str(ops_env.root) not in text and "/improve_reports" not in text
    assert "Traceback" not in text and "report_path" not in text


def test_read_writes_no_audit_rows_and_no_idempotency_rows(ops_env, reports):
    mission_id = _floor_mission(ops_env, reports)
    svc = service(ops_env, reports)

    def counts():
        return tuple(ops_env.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                     for t in ("ops_requests", "ops_idempotency", "ops_events"))

    before = counts()
    svc.improve_run(OPERATOR, mission_id)
    with pytest.raises(OpsError):
        svc.improve_run(OPERATOR, 99999)
    assert counts() == before


def test_the_endpoint_needs_no_pidfd_and_notify_cannot_read_it():
    from agentic_fx.ops.api_server import _PIDFD_REQUIRED
    assert ENDPOINTS["improve_run"].scope not in _PIDFD_REQUIRED
    assert Scope.APPROVALS_DETAIL not in PRINCIPAL_SCOPES[Principal.OPS_NOTIFY]
    readers = {p for p, scopes in PRINCIPAL_SCOPES.items() if Scope.APPROVALS_DETAIL in scopes}
    assert readers == {Principal.OPERATOR, Principal.APPROVER, Principal.OPS_BOT,
                       Principal.OPS_WEB}


# ---- HTTP 層 ----

def test_http_round_trip(api5, ops_env):
    reports = ops_env.root / "data" / "improve_reports"
    reports.mkdir(parents=True, exist_ok=True)
    api5.service._improve_reports_dir = reports
    mission_id = _floor_mission(ops_env, reports)
    audit_before = len(ops_env.ops_rows())
    client = api5.client(Principal.OPS_BOT)
    response = client.get(f"/v1/improve/runs/{mission_id}")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["id"] == mission_id and data["floor"]["stages"][0]["pairs"][0]["trades"] == 13
    assert client.get("/v1/improve/runs/987654").status_code == 404
    assert client.get("/v1/improve/runs/abc").status_code == 400
    assert api5.client(Principal.OPS_NOTIFY).get(
        f"/v1/improve/runs/{mission_id}").status_code == 403
    assert api5.client().get(f"/v1/improve/runs/{mission_id}").status_code == 401
    assert len(ops_env.ops_rows()) == audit_before
