"""AC-35: 冪等 key 5 本の状態機械。AC-52: reflect retry の key と試行識別子。"""
from __future__ import annotations

import concurrent.futures
import threading

import pytest

from agentic_fx.ops import ErrorCode, OpsError
from agentic_fx.ops.contracts import Principal
from agentic_fx.store import backlog

from .conftest import OPERATOR, APPROVER, wait_job
from .test_ac13_23_backlog_and_autopilot import _closed_order


class _Improve:
    def __init__(self):
        self.calls = 0
        self.release = threading.Event()
        self.entered = threading.Event()

    def submit_manual(self, *, on_prepared=None):
        self.calls += 1
        self.entered.set()
        if on_prepared is not None:
            on_prepared(41)
        self.release.wait(10)
        return 41


class _Asks:
    def __init__(self):
        self.calls = 0
        self.futures = []

    def __call__(self, question):
        self.calls += 1
        future = concurrent.futures.Future()
        self.futures.append(future)
        return future


def _attempt(env, order_id, attempts, at):
    env.conn.execute(
        "INSERT OR REPLACE INTO reflection_attempts(order_id,attempts,last_attempt_at,last_reason) "
        "VALUES (?,?,?,?)", (order_id, attempts, at, "private reason"))
    env.conn.commit()


def _endpoints(env, service, improve, asks):
    order_id = _closed_order(env)
    return {
        "policy": (lambda body: service.add_policy(OPERATOR, "key-1", body),
                   lambda: env.conn.execute("SELECT COUNT(*) AS n FROM ops_policies").fetchone()["n"]),
        "backlog": (lambda body: service.add_backlog(OPERATOR, "key-1", body),
                    lambda: env.conn.execute(
                        "SELECT COUNT(*) AS n FROM improvement_backlog").fetchone()["n"]),
        "ask": (lambda body: service.ask(OPERATOR, "key-1", body), lambda: asks.calls),
        "improve": (lambda body: service.improve(OPERATOR, "key-1"), lambda: improve.calls),
        "reflection": (lambda body: service.retry_reflection(
            OPERATOR, "key-1", order_id, expected_attempts=0 if body == "one" else 1,
            expected_last_attempt_at=None if body == "one" else "2026-10-05T00:00:00+00:00"),
            lambda: len(env.conn.execute(
                "SELECT 1 FROM ops_requests WHERE phase='succeeded' "
                "AND endpoint LIKE 'POST /v1/reflections/%'").fetchall())),
    }


@pytest.mark.parametrize("name", ["policy", "backlog", "ask", "improve", "reflection"])
def test_ac35_same_key_same_body_has_one_side_effect_and_same_reply(ops_env, name):
    improve, asks = _Improve(), _Asks()
    service = ops_env.service(improve_supervisor=improve, ask_submitter=asks)
    call, effects = _endpoints(ops_env, service, improve, asks)[name]
    first = call("one")
    again = call("one")
    assert again.get("job_id") == first.get("job_id")
    if "job_id" not in first:
        assert again == first
    if name == "improve":
        # improve の副作用は job thread が起こすので、入口に着くまで待ってから数える。
        assert improve.entered.wait(5)
    assert effects() == 1
    if name != "improve":
        with pytest.raises(OpsError) as raised:
            call("two")
        assert raised.value.code is ErrorCode.IDEMPOTENCY_MISMATCH
    improve.release.set()
    for future in asks.futures:
        future.set_result("answer")
    if "job_id" in first:
        wait_job(service, first["job_id"])
        assert call("one")["job_id"] == first["job_id"]
    assert effects() == 1


@pytest.mark.parametrize("key", ["", None])
def test_ac35_missing_key_is_400_without_acceptance(ops_env, key):
    service = ops_env.service()
    with pytest.raises(OpsError) as raised:
        service.add_policy(OPERATOR, key, "text")
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert ops_env.ops_rows() == []


def test_ac35_key_expires_after_24_hours_of_wall_clock_not_monotonic(ops_env):
    service = ops_env.service()
    first = service.add_backlog(OPERATOR, "key-1", "idea")
    ops_env.mono.advance(48 * 3600)
    assert service.add_backlog(OPERATOR, "key-1", "idea") == first
    ops_env.wall.advance(hours=23, minutes=59)
    assert service.add_backlog(OPERATOR, "key-1", "idea") == first
    ops_env.wall.advance(minutes=2)
    second = service.add_backlog(OPERATOR, "key-1", "idea")
    assert second != first
    assert ops_env.conn.execute(
        "SELECT COUNT(*) AS n FROM improvement_backlog").fetchone()["n"] == 2


def test_ac35_keys_are_scoped_by_principal_and_endpoint(ops_env):
    service = ops_env.service()
    a = service.add_backlog(OPERATOR, "shared", "idea")
    b = service.add_backlog(APPROVER, "shared", "idea")
    c = service.add_policy(OPERATOR, "shared", "idea")
    assert a != b and "id" in c
    assert ops_env.conn.execute(
        "SELECT COUNT(*) AS n FROM improvement_backlog").fetchone()["n"] == 2


def test_ac35_stored_failure_is_replayed_as_the_same_error(ops_env):
    service = ops_env.service()
    order_id = _closed_order(ops_env)
    _attempt(ops_env, order_id, 2, "2026-10-05T00:00:00+00:00")
    with pytest.raises(OpsError) as raised:
        service.retry_reflection(OPERATOR, "k", order_id, expected_attempts=1,
                                 expected_last_attempt_at="2026-10-05T00:00:00+00:00")
    assert raised.value.code is ErrorCode.ATTEMPT_CHANGED
    with pytest.raises(OpsError) as raised:
        service.retry_reflection(OPERATOR, "k", order_id, expected_attempts=1,
                                 expected_last_attempt_at="2026-10-05T00:00:00+00:00")
    assert raised.value.code is ErrorCode.ATTEMPT_CHANGED


def test_ac35_policy_file_is_regenerated_and_hand_written_lines_survive(ops_env):
    ops_env.policy_path.parent.mkdir(parents=True)
    ops_env.policy_path.write_text("# 方針\n- 手で書いた方針\n", encoding="utf-8")
    service = ops_env.service()
    service.add_policy(OPERATOR, "k1", "always protect capital")
    service.add_policy(OPERATOR, "k1", "always protect capital")
    service.add_policy(OPERATOR, "k2", "no news trading")
    service.regenerate_policy()
    assert ops_env.policy_path.read_text(encoding="utf-8") == (
        "# 方針\n- 手で書いた方針\n- always protect capital\n- no news trading\n")


def test_ac52_reflect_retry_requires_key_and_identifier(ops_env):
    service = ops_env.service()
    order_id = _closed_order(ops_env)
    with pytest.raises(OpsError) as raised:
        service.retry_reflection(OPERATOR, "", order_id, expected_attempts=0,
                                 expected_last_attempt_at=None)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    for attempts, at in ((None, None), (1, None), (0, "2026-10-05T00:00:00+00:00"), (-1, None)):
        with pytest.raises(OpsError) as raised:
            service.retry_reflection(OPERATOR, "k", order_id, expected_attempts=attempts,
                                     expected_last_attempt_at=at)
        assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert ops_env.ops_rows() == []


def test_ac52_resend_clears_once_and_newer_attempt_survives_old_identifier(ops_env):
    service = ops_env.service()
    order_id = _closed_order(ops_env)
    first_at = "2026-10-05T00:00:00+00:00"
    _attempt(ops_env, order_id, 1, first_at)
    seen = service.reflection_status(OPERATOR, order_id)
    assert (seen["attempts"], seen["last_attempt_at"]) == (1, first_at)
    assert "last_reason" not in seen
    reply = service.retry_reflection(OPERATOR, "k1", order_id, expected_attempts=1,
                                     expected_last_attempt_at=first_at)
    # clear 後に reflection 処理が新しい失敗を積んだ (attempts は同じ 1 に戻る)。
    newer_at = "2026-10-05T00:05:00+00:00"
    _attempt(ops_env, order_id, 1, newer_at)
    assert service.retry_reflection(OPERATOR, "k1", order_id, expected_attempts=1,
                                    expected_last_attempt_at=first_at) == reply
    with pytest.raises(OpsError) as raised:
        service.retry_reflection(OPERATOR, "k2", order_id, expected_attempts=1,
                                 expected_last_attempt_at=first_at)
    assert raised.value.code is ErrorCode.ATTEMPT_CHANGED
    row = ops_env.conn.execute("SELECT attempts,last_attempt_at FROM reflection_attempts "
                               "WHERE order_id=?", (order_id,)).fetchone()
    assert (row["attempts"], row["last_attempt_at"]) == (1, newer_at)


def test_ac52_closed_and_unreflected_condition_is_kept(ops_env):
    service = ops_env.service()
    now = ops_env.wall().isoformat()
    cur = ops_env.conn.execute(
        "INSERT INTO orders(pair,direction,entry_type,horizon,status,created_at,updated_at) "
        "VALUES ('EURUSD','long','market','x','open',?,?)", (now, now))
    ops_env.conn.commit()
    with pytest.raises(OpsError) as raised:
        service.retry_reflection(OPERATOR, "k", int(cur.lastrowid), expected_attempts=0,
                                 expected_last_attempt_at=None)
    assert raised.value.code is ErrorCode.INVALID_STATE
    with pytest.raises(OpsError) as raised:
        service.retry_reflection(OPERATOR, "k2", 9999, expected_attempts=0,
                                 expected_last_attempt_at=None)
    assert raised.value.code is ErrorCode.NOT_FOUND


def test_ac52_attempt_bumped_between_the_check_and_the_clear_is_kept(ops_env, monkeypatch):
    service = ops_env.service()
    order_id = _closed_order(ops_env)
    first_at, newer_at = "2026-10-05T00:00:00+00:00", "2026-10-05T00:05:00+00:00"
    _attempt(ops_env, order_id, 1, first_at)
    read = service._reflection_status

    def read_then_bump(oid):
        seen = read(oid)
        # 照合の直後、clear の前に reflection 処理が新しい失敗を積んだ。
        _attempt(ops_env, order_id, 1, newer_at)
        return seen

    monkeypatch.setattr(service, "_reflection_status", read_then_bump)
    with pytest.raises(OpsError) as raised:
        service.retry_reflection(OPERATOR, "k", order_id, expected_attempts=1,
                                 expected_last_attempt_at=first_at)
    assert raised.value.code is ErrorCode.ATTEMPT_CHANGED
    row = ops_env.conn.execute("SELECT attempts,last_attempt_at FROM reflection_attempts "
                               "WHERE order_id=?", (order_id,)).fetchone()
    assert (row["attempts"], row["last_attempt_at"]) == (1, newer_at)


def test_ac52_reflected_order_is_not_requeued(ops_env):
    service = ops_env.service()
    order_id = _closed_order(ops_env)
    at = "2026-10-05T00:00:00+00:00"
    _attempt(ops_env, order_id, 1, at)
    ops_env.conn.execute("INSERT INTO reflections(order_id,content,created_at) VALUES (?,?,?)",
                         (order_id, "done", at))
    ops_env.conn.commit()
    with pytest.raises(OpsError) as raised:
        service.retry_reflection(OPERATOR, "k", order_id, expected_attempts=1,
                                 expected_last_attempt_at=at)
    assert raised.value.code is ErrorCode.INVALID_STATE
    assert ops_env.conn.execute("SELECT attempts FROM reflection_attempts WHERE order_id=?",
                                (order_id,)).fetchone()["attempts"] == 1


_TRANSIENT = [ErrorCode.DATABASE_BUSY, ErrorCode.REQUEST_TIMEOUT, ErrorCode.UNAVAILABLE,
              ErrorCode.MISSION_BUSY, ErrorCode.DECISION_IN_PROGRESS,
              ErrorCode.IMPROVE_RUNNING, ErrorCode.PLUGIN_BUSY]


@pytest.mark.parametrize("code", _TRANSIENT)
def test_ac35_transient_failure_keeps_the_audit_row_but_the_key_can_be_resent(ops_env, code):
    service = ops_env.service()
    real = service._regenerate_policy
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OpsError(code)
        real()

    service._regenerate_policy = flaky
    with pytest.raises(OpsError) as raised:
        service.add_policy(OPERATOR, "retry-key", "risk small")
    assert raised.value.code is code
    failed = ops_env.conn.execute(
        "SELECT result_code FROM ops_requests WHERE phase='failed' "
        "AND endpoint='POST /v1/policy'").fetchall()
    assert [row["result_code"] for row in failed] == [code.value]
    assert ops_env.conn.execute("SELECT COUNT(*) AS n FROM ops_idempotency").fetchone()["n"] == 0
    reply = service.add_policy(OPERATOR, "retry-key", "risk small")
    assert "id" in reply and calls["n"] == 2
    assert ops_env.conn.execute(
        "SELECT state FROM ops_idempotency").fetchone()["state"] == "succeeded"


def test_ac35_permanent_failure_stays_stored_and_does_not_rerun_the_action(ops_env):
    service = ops_env.service()
    calls = {"n": 0}

    def refuse():
        calls["n"] += 1
        raise OpsError(ErrorCode.INVALID_STATE)

    service._regenerate_policy = refuse
    for _ in range(2):
        with pytest.raises(OpsError) as raised:
            service.add_policy(OPERATOR, "perm-key", "risk small")
        assert raised.value.code is ErrorCode.INVALID_STATE
    assert calls["n"] == 1
    assert ops_env.conn.execute(
        "SELECT state FROM ops_idempotency").fetchone()["state"] == "failed"
