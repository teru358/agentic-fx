"""AC-36: 変更系 15 本の append-only 監査と自由文の不保存。
AC-38: event の固定 schema、cursor 再配送、best-effort 生成。"""
from __future__ import annotations

import concurrent.futures
import sqlite3

import pytest

from agentic_fx.ops import ErrorCode, OpsError
from agentic_fx.ops.audit import AuditStore
from agentic_fx.ops.events import EventCursor, EventPage, EventSchemaError, EventStore
from agentic_fx.ops.service import ENDPOINTS
from agentic_fx.store import backlog

from .conftest import APPROVER, OPERATOR, digest_of, wait_job
from .test_ac13_23_backlog_and_autopilot import _closed_order

REASON = "却下理由の秘密テキスト"
IDEA = "バックログの秘密アイデア"
QUESTION = "質問の秘密テキスト"
POLICY = "方針の秘密テキスト"


class _Improve:
    calls = 0

    def submit_manual(self, *, on_prepared=None):
        _Improve.calls += 1
        return 7


def test_ac38_equal_high_watermark_is_not_a_rewind():
    cursor = EventCursor(position=4, high_watermark=8)

    assert cursor.consume(EventPage([], 8)) == []
    assert cursor.position == 4
    assert cursor.high_watermark == 8
    assert cursor.health_error is False

    assert cursor.consume(EventPage([], 7)) == []
    assert cursor.position == 4
    assert cursor.health_error is True


def _drive_all_mutations(env, service):
    """15 本の変更 endpoint をそれぞれ 1 回ずつ呼び、呼んだ code を返す。"""
    called = []

    def run(code, call):
        called.append(code)
        try:
            reply = call()
        except OpsError:
            return None
        if isinstance(reply, dict) and "job_id" in reply:
            wait_job(service, reply["job_id"])
        return reply

    approve_id = env.make_plugin_approval(name="a1", mission_id=1)
    reject_id = env.make_plugin_approval(name="a2", mission_id=2)
    retry_id = env.make_plugin_approval(name="a3", mission_id=3)
    backlog_id = backlog.add(env.conn, "seed", "user", env.wall())
    order_id = _closed_order(env)
    env.state.update(kill_switch_latched=True)
    generation = env.state.load().kill_switch_generation
    run("ask", lambda: service.ask(OPERATOR, "k-ask", QUESTION))
    run("data_resume", lambda: service.resume_data(APPROVER, acknowledge=True))
    run("approve", lambda: service.approve(APPROVER, approve_id, digest_of(env, approve_id)))
    run("reject", lambda: service.reject(APPROVER, reject_id, REASON))
    run("retry", lambda: service.retry(APPROVER, retry_id, digest_of(env, retry_id)))
    run("killswitch_reset", lambda: service.reset_kill_switch(APPROVER, generation))
    run("killswitch_reconcile", lambda: service.reconcile_kill_switch(APPROVER))
    run("reflection_retry", lambda: service.retry_reflection(
        OPERATOR, "k-ref", order_id, expected_attempts=0, expected_last_attempt_at=None))
    run("improve", lambda: service.improve(OPERATOR, "k-imp"))
    run("backlog_add", lambda: service.add_backlog(OPERATOR, "k-bl", IDEA))
    run("backlog_note", lambda: service.transition_backlog(OPERATOR, backlog_id, "note"))
    run("backlog_reopen", lambda: service.transition_backlog(OPERATOR, backlog_id, "reopen"))
    run("backlog_reject", lambda: service.transition_backlog(OPERATOR, backlog_id, "reject"))
    run("policy", lambda: service.add_policy(OPERATOR, "k-pol", POLICY))
    run("job_cancel", lambda: service.cancel_job(OPERATOR, "missing-job"))
    return called


def test_ac36_every_mutating_endpoint_leaves_accepted_and_one_terminal(ops_env):
    future = concurrent.futures.Future()
    future.set_result("answer")
    service = ops_env.service(ask_submitter=lambda q: future,
                              data_resume=lambda ack: {"gap": 0},
                              improve_supervisor=_Improve())
    called = _drive_all_mutations(ops_env, service)
    mutating = sorted(code for code, e in ENDPOINTS.items() if e.mutating)
    assert sorted(called) == mutating and len(mutating) == 15
    rows = ops_env.ops_rows()
    accepted = {r["id"]: r for r in rows if r["phase"] == "accepted"}
    terminals = [r for r in rows if r["phase"] != "accepted"]
    routes = {e.route: code for code, e in ENDPOINTS.items()}
    assert sorted(routes[r["endpoint"]] for r in accepted.values()) == mutating
    by_target = {}
    for row in terminals:
        by_target.setdefault(row["target_ref"], []).append(row)
    for audit_id in accepted:
        assert len(by_target.get(f"audit:{audit_id}", [])) == 1
    # 失敗した要求 (存在しない job の cancel、marker の無い reconcile) も終端を持つ。
    phases = {routes[accepted[int(t["target_ref"].split(':')[1])]["endpoint"]]: t["phase"]
              for t in terminals}
    assert phases["job_cancel"] == "failed"
    assert phases["killswitch_reconcile"] == "failed"
    assert phases["approve"] == "succeeded"
    with pytest.raises(sqlite3.DatabaseError):
        ops_env.conn.execute("UPDATE ops_requests SET phase='failed'")
    with pytest.raises(sqlite3.DatabaseError):
        ops_env.conn.execute("DELETE FROM ops_requests")
    ops_env.conn.rollback()


def test_ac36_free_text_appears_only_where_the_spec_allows(ops_env):
    future = concurrent.futures.Future()
    future.set_result("answer")
    service = ops_env.service(ask_submitter=lambda q: future,
                              data_resume=lambda ack: {"gap": 0},
                              improve_supervisor=_Improve())
    _drive_all_mutations(ops_env, service)
    projections = {
        "ops_requests": str([tuple(r) for r in ops_env.ops_rows()]),
        "ops_events": str([tuple(r) for r in ops_env.conn.execute(
            "SELECT * FROM ops_events").fetchall()]),
        "jobs": str([(j.kind, j.state, j.result, j.error_code)
                     for j in service.jobs._jobs.values()]),
        "activity": ops_env.activity_path.read_text(encoding="utf-8"),
    }
    for name, text in projections.items():
        for secret in (REASON, IDEA, QUESTION):
            assert secret not in text, (name, secret)
        if name != "activity":
            assert POLICY not in text, name
    activity_lines = [line for line in projections["activity"].splitlines() if POLICY in line]
    assert len(activity_lines) == 1 and "\tpolicy_added\t" in activity_lines[0]
    reasons = ops_env.conn.execute(
        "SELECT COUNT(*) AS n FROM approval_requests WHERE reason=?", (REASON,)).fetchone()
    assert reasons["n"] == 1


def test_ac36_accepted_commit_failure_means_no_side_effect(ops_env):
    service = ops_env.service()
    ops_env.conn.execute(
        "CREATE TRIGGER refuse_accept BEFORE INSERT ON ops_requests "
        "WHEN NEW.phase='accepted' BEGIN SELECT RAISE(ABORT,'disk full'); END")
    ops_env.conn.commit()
    with pytest.raises(sqlite3.DatabaseError):
        service.add_backlog(OPERATOR, "k", "idea")
    with pytest.raises(sqlite3.DatabaseError):
        service.add_policy(OPERATOR, "k", "policy")
    backlog_id = backlog.add(ops_env.conn, "seed", "user", ops_env.wall())
    with pytest.raises(sqlite3.DatabaseError):
        service.transition_backlog(OPERATOR, backlog_id, "note")
    assert ops_env.conn.execute(
        "SELECT COUNT(*) AS n FROM improvement_backlog").fetchone()["n"] == 1
    assert ops_env.conn.execute("SELECT status FROM improvement_backlog").fetchone()[0] == "open"
    assert not ops_env.policy_path.exists()
    assert ops_env.conn.execute("SELECT COUNT(*) AS n FROM ops_policies").fetchone()["n"] == 0


def test_ac36_activity_failure_does_not_fail_the_request(ops_env):
    class Broken:
        def write(self, *args, **kwargs):
            raise OSError("disk")

    service = ops_env.service(activity_log=Broken())
    assert service.add_backlog(OPERATOR, "k", "idea")["id"] > 0
    assert [r["phase"] for r in ops_env.ops_rows()] == ["accepted", "succeeded"]


def test_ac36_audit_connections_commit_with_synchronous_full(ops_env):
    # 呼び出し元が throughput 寄りの設定にしていても、監査の commit 点は FULL にする。
    ops_env.conn.execute("PRAGMA synchronous=NORMAL")
    decide = ops_env.connect()
    decide.execute("PRAGMA synchronous=OFF")
    service = ops_env.service(decide_conn=decide)
    assert ops_env.conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert service._decide_conn.execute("PRAGMA synchronous").fetchone()[0] == 2


# ------------------------------------------------------------------ AC-38


def _emit_terminal(store, n):
    return store.emit("ops_request_terminal", f"audit:{n}", {
        "endpoint_code": "backlog_add", "result_code": "ok",
        "authenticated_principal": "operator"})


def test_ac38_cursor_redelivers_same_ids_and_client_deduplicates(ops_env):
    store = EventStore(ops_env.conn, wall_clock=ops_env.wall)
    ids = [_emit_terminal(store, n) for n in range(5)]
    page = store.poll(after=0, limit=3)
    assert [e["id"] for e in page.events] == ids[:3]
    assert store.poll(after=0, limit=3).events == page.events
    assert [e["id"] for e in store.poll(after=ids[2], limit=10).events] == ids[3:]
    cursor = EventCursor()
    assert [e["id"] for e in cursor.consume(page)] == ids[:3]
    assert cursor.consume(page) == []
    assert [e["id"] for e in cursor.consume(store.poll(after=0, limit=10))] == ids[3:]
    assert cursor.position == ids[-1]


@pytest.mark.parametrize("limit", [0, 501, "10"])
def test_ac38_limit_is_bounded(ops_env, limit):
    store = EventStore(ops_env.conn, wall_clock=ops_env.wall)
    with pytest.raises(OpsError) as raised:
        store.poll(after=0, limit=limit)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert len(store.poll(after=0, limit=500).events) == 0


def test_ac38_high_watermark_regression_does_not_advance_cursor():
    cursor = EventCursor()
    cursor.consume(EventPage([{"id": 5, "ts": "", "code": "x", "ref": "", "fields": {}}], 5))
    assert cursor.consume(EventPage([{"id": 6, "ts": "", "code": "x", "ref": "", "fields": {}}],
                                    3)) == []
    assert cursor.health_error is True
    assert cursor.position == 5


def test_ac38_insert_failure_keeps_operation_and_reports_health_later(ops_env):
    service = ops_env.service()
    ops_env.conn.execute(
        "CREATE TRIGGER refuse_event BEFORE INSERT ON ops_events "
        "BEGIN SELECT RAISE(ABORT,'events down'); END")
    ops_env.conn.commit()
    first = service.add_backlog(OPERATOR, "k1", "idea")
    assert first["id"] > 0
    assert ops_env.conn.execute("SELECT COUNT(*) AS n FROM ops_events").fetchone()["n"] == 0
    ops_env.conn.execute("DROP TRIGGER refuse_event")
    ops_env.conn.commit()
    service.add_backlog(OPERATOR, "k2", "idea 2")
    codes = [r["code"] for r in ops_env.conn.execute("SELECT code FROM ops_events ORDER BY id")]
    assert codes == ["ops_request_terminal", "event_stream_health"]
    health = ops_env.conn.execute(
        "SELECT fields_json FROM ops_events WHERE code='event_stream_health'").fetchone()
    assert '"failure_count":1' in health["fields_json"]
    # 生成されなかった event は ops_requests との突き合わせでのみ分かる。
    assert len([r for r in ops_env.ops_rows() if r["phase"] == "succeeded"]) == 2


@pytest.mark.parametrize("code,fields", [
    ("ops_request_terminal", {"endpoint_code": "x", "result_code": "ok",
                              "authenticated_principal": "operator", "extra": "1"}),
    ("approval_state_changed", {"state": "rejected", "decision_code": "reject",
                                "decided_by": "理由を含む自由文"}),
    ("job_state_changed", {"job_kind": "ask", "state": "done", "result_code": "two words"}),
    ("guard_state_changed", {"guard_kind": "kill_switch", "state": "x", "generation": 1.5}),
    ("service_health_changed", {"component_code": "api"}),
    ("event_stream_health", {"failure_count": 1, "first_failed_at": "a" * 65,
                             "last_failed_at": None}),
    ("free_text", {}),
])
def test_ac38_every_code_rejects_out_of_schema_fields_and_free_text(ops_env, code, fields):
    store = EventStore(ops_env.conn, wall_clock=ops_env.wall)
    with pytest.raises(EventSchemaError):
        store.emit(code, "ref", fields)
    with pytest.raises(EventSchemaError):
        store.emit_best_effort(code, "ref", fields)
    assert ops_env.conn.execute("SELECT COUNT(*) AS n FROM ops_events").fetchone()["n"] == 0


def test_terminal_is_appended_only_once_per_accepted_request(ops_env):
    store = AuditStore(ops_env.conn, wall_clock=ops_env.wall)
    accepted = store.accept(endpoint="POST /v1/backlog", principal=OPERATOR, asserted_actor=None,
                            peer_pid=None, peer_exe=None, body={}, target_ref="backlog")
    assert store.terminal(accepted.audit_id, code="ok", response={}) is True
    assert store.terminal(accepted.audit_id, code="internal", response={}) is False
    terminals = [(r["target_ref"], r["result_code"]) for r in ops_env.ops_rows()
                 if r["phase"] != "accepted"]
    assert terminals == [(f"audit:{accepted.audit_id}", "ok")]


_VALID_FIELDS = {
    "ops_request_terminal": {"endpoint_code": "policy", "result_code": "ok",
                             "authenticated_principal": "operator"},
    "approval_state_changed": {"state": "rejected", "decision_code": "reject",
                               "decided_by": "api:approver"},
    "job_state_changed": {"job_kind": "ask", "state": "done", "result_code": "ok"},
    "guard_state_changed": {"guard_kind": "kill_switch", "state": "released",
                            "generation": 2},
    "service_health_changed": {"component_code": "api", "health_code": "ok"},
    "event_stream_health": {"failure_count": 1, "first_failed_at": None,
                            "last_failed_at": None},
}
_VALID_REF = {
    "ops_request_terminal": "audit:12",
    "approval_state_changed": "approval:3",
    "job_state_changed": "job:" + "0123456789abcdef" * 2,
    "guard_state_changed": "instance",
    "service_health_changed": "instance",
    "event_stream_health": "instance",
}


@pytest.mark.parametrize("code", sorted(_VALID_FIELDS))
def test_ac38_each_code_accepts_only_its_identifier_as_ref(ops_env, code):
    store = EventStore(ops_env.conn, wall_clock=ops_env.wall)
    assert store.emit(code, _VALID_REF[code], _VALID_FIELDS[code]) >= 1
    others = [ref for other, ref in _VALID_REF.items() if ref != _VALID_REF[code]]
    for ref in ["却下理由の秘密テキスト", "two words", "audit:", "x" * 65, "",
                "audit:1\n", "Audit:1", "job:" + "g" * 32, *others]:
        with pytest.raises(EventSchemaError):
            store.emit(code, ref, _VALID_FIELDS[code])
        with pytest.raises(EventSchemaError):
            store.emit_best_effort(code, ref, _VALID_FIELDS[code])
    assert ops_env.conn.execute("SELECT COUNT(*) AS n FROM ops_events").fetchone()["n"] == 1


def test_ac38_stream_health_after_a_failure_is_recorded_against_the_instance(ops_env):
    store = EventStore(ops_env.conn, wall_clock=ops_env.wall)
    original = store.emit
    calls = {"n": 0}

    def flaky(code, ref, fields):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return original(code, ref, fields)

    store.emit = flaky
    assert store.emit_best_effort("ops_request_terminal", "audit:1",
                                  _VALID_FIELDS["ops_request_terminal"]) is None
    assert store.emit_best_effort("ops_request_terminal", "audit:2",
                                  _VALID_FIELDS["ops_request_terminal"]) is not None
    refs = [(r["code"], r["ref"]) for r in ops_env.conn.execute(
        "SELECT code,ref FROM ops_events ORDER BY id")]
    assert refs == [("ops_request_terminal", "audit:2"), ("event_stream_health", "instance")]


class _SeamState:
    """kill switch の副作用の入口で hook を呼ぶ StateStore 包み。"""

    def __init__(self, inner, hook):
        self.inner, self.hook = inner, hook

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def reset_kill_switch(self, *args, **kwargs):
        self.hook("killswitch_reset")
        return self.inner.reset_kill_switch(*args, **kwargs)

    def reconcile_marker(self):
        return {"requested_generation": 1}

    def confirm_latched(self, **kwargs):
        self.hook("killswitch_reconcile")
        return self.inner.load()


class _Seams:
    """副作用の入口で、別接続から accepted 行が既に読めるかを記録する。"""

    def __init__(self, env):
        self.env = env
        self.seen: dict[str, list[int]] = {}

    def __call__(self, code: str) -> None:
        reader = sqlite3.connect(self.env.db_path)
        try:
            count = reader.execute(
                "SELECT COUNT(*) FROM ops_requests WHERE endpoint=? AND phase='accepted'",
                (ENDPOINTS[code].route,)).fetchone()[0]
        finally:
            reader.close()
        self.seen.setdefault(code, []).append(count)


def _seamed_call(env, monkeypatch, code):
    from agentic_fx.plugin import switch
    seams = _Seams(env)
    approval_id = env.make_plugin_approval()
    env.state.update(kill_switch_latched=True)
    generation = env.state.load().kill_switch_generation
    order_id = _closed_order(env)
    env.conn.execute(
        "INSERT INTO reflection_attempts(order_id,attempts,last_attempt_at,last_reason) "
        "VALUES (?,?,?,?)", (order_id, 1, "2026-10-05T00:00:00+00:00", "r"))
    backlog_id = backlog.add(env.conn, "seed", "user", env.wall())
    env.conn.commit()

    future = concurrent.futures.Future()
    future.set_result("answer")

    def ask_submitter(_question):
        seams("ask")
        return future

    class _Improve:
        def submit_manual(self, *, on_prepared=None):
            seams("improve")
            return 1

    def data_resume(_ack):
        seams("data_resume")
        return {"ok": 1}

    service = env.service(ask_submitter=ask_submitter, improve_supervisor=_Improve(),
                          data_resume=data_resume, state_store=_SeamState(env.state, seams))
    # 決定は approve / retry が approve_candidate、reject が reject_candidate を通る。
    for name, seam in (("approve_candidate", None), ("reject_candidate", "reject")):
        original = getattr(switch, name)

        def wrapped(*args, _original=original, _seam=seam, **kwargs):
            seams(_seam or service.jobs.lookup(_current_job[0]).kind)
            return _original(*args, **kwargs)
        monkeypatch.setattr(switch, name, wrapped)
    for name in ("add", "transition"):
        original = getattr(backlog, name)

        def wrapped(*args, _original=original, _name=name, **kwargs):
            seams("backlog_add" if _name == "add" else code)
            return _original(*args, **kwargs)
        monkeypatch.setattr(backlog, name, wrapped)
    original_cancel = service.jobs.cancel

    def cancel(*args, **kwargs):
        seams("job_cancel")
        return original_cancel(*args, **kwargs)
    monkeypatch.setattr(service.jobs, "cancel", cancel)
    # policy と reflection retry の最初の副作用は DB 書込みなので trigger で入口を捕まえる。
    env.conn.create_function("seam", 1, lambda code: seams(code) or 0)
    env.conn.execute("CREATE TEMP TRIGGER seam_policy BEFORE INSERT ON ops_policies "
                     "BEGIN SELECT seam('policy'); END")
    env.conn.execute("CREATE TEMP TRIGGER seam_reflection BEFORE DELETE ON reflection_attempts "
                     "BEGIN SELECT seam('reflection_retry'); END")
    env.conn.commit()

    _current_job: list[str] = [""]
    calls = {
        "ask": lambda: service.ask(OPERATOR, "k", "q"),
        "improve": lambda: service.improve(OPERATOR, "k"),
        "data_resume": lambda: service.resume_data(APPROVER, acknowledge=True),
        "approve": lambda: service.approve(APPROVER, approval_id, digest_of(env, approval_id)),
        "retry": lambda: service.retry(APPROVER, approval_id, digest_of(env, approval_id)),
        "reject": lambda: service.reject(APPROVER, approval_id, "x"),
        "killswitch_reset": lambda: service.reset_kill_switch(APPROVER, generation),
        "killswitch_reconcile": lambda: service.reconcile_kill_switch(APPROVER),
        "reflection_retry": lambda: service.retry_reflection(
            OPERATOR, "k", order_id, expected_attempts=1,
            expected_last_attempt_at="2026-10-05T00:00:00+00:00"),
        "backlog_add": lambda: service.add_backlog(OPERATOR, "k", "idea"),
        "backlog_note": lambda: service.transition_backlog(OPERATOR, backlog_id, "note"),
        "backlog_reject": lambda: service.transition_backlog(OPERATOR, backlog_id, "reject"),
        "backlog_reopen": lambda: service.transition_backlog(OPERATOR, backlog_id, "reopen"),
        "policy": lambda: service.add_policy(OPERATOR, "k", "方針"),
        "job_cancel": lambda: service.cancel_job(OPERATOR, "missing"),
    }
    original_take = service.jobs.take_next_decision

    def take():
        job = original_take()
        if job is not None:
            _current_job[0] = job.id
        return job
    monkeypatch.setattr(service.jobs, "take_next_decision", take)
    try:
        reply = calls[code]()
    except OpsError:
        reply = None
    if isinstance(reply, dict) and "job_id" in reply:
        wait_job(service, reply["job_id"])
    return seams, calls


@pytest.mark.parametrize("code", sorted(c for c, e in ENDPOINTS.items() if e.mutating))
def test_ac36_accepted_is_durable_before_the_first_side_effect(ops_env, monkeypatch, code):
    seams, calls = _seamed_call(ops_env, monkeypatch, code)
    assert set(calls) == {c for c, e in ENDPOINTS.items() if e.mutating}
    assert seams.seen.get(code), f"side-effect seam for {code} was not reached"
    assert seams.seen[code] == [1] * len(seams.seen[code]), seams.seen
