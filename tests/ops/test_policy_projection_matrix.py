"""policy show / remove / reconcile の principal・上限・crash・純読取・収束の網羅。"""
from __future__ import annotations

import io
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_fx import policy_projection
from agentic_fx.activity import ActivityLog
from agentic_fx.ops import ErrorCode, OpsError
from agentic_fx.ops import client as client_module
from agentic_fx.ops import service as service_module
from agentic_fx.ops.client import ClientError, Reply, Runner, parse
from agentic_fx.ops.contracts import Principal
from agentic_fx.service import _recover_ops

from .conftest import APPROVER, OPERATOR
from .test_ac56_57_ops_principals import H, ACTOR, OPS_BOT, OPS_NOTIFY, OPS_WEB

CHILD = Path(__file__).with_name("_policy_crash_child.py")
REMOVE = "POST /v1/policy/{id}/remove"
RECONCILE = "POST /v1/policy/reconcile"
ADD = "POST /v1/policy"


def _block(identifier: int, text: str) -> bytes:
    return (f"<!-- afx:managed-policy id={identifier} -->\n- {text}\n"
            f"<!-- /afx:managed-policy id={identifier} -->\n").encode()


def _seed(service, *texts: str) -> None:
    for number, text in enumerate(texts, 1):
        service.add_policy(OPERATOR, f"seed-{number}", text)


def _boom() -> None:
    raise OSError("injected")


def _activity(env) -> str:
    return env.activity_path.read_text(encoding="utf-8") if env.activity_path.exists() else ""


def _count(env, event: str) -> int:
    return _activity(env).count(f"\t{event}\t")


def _table(env, sql: str) -> list[tuple]:
    return [tuple(row) for row in env.conn.execute(sql).fetchall()]


def _migration_rows(env) -> list[tuple]:
    return _table(env, "SELECT * FROM data_migrations WHERE name='policy_directives_managed_v1'")


def _snapshot(env) -> dict:
    return {
        "file": env.policy_path.read_bytes() if env.policy_path.exists() else None,
        "policies": _table(env, "SELECT * FROM ops_policies ORDER BY id"),
        "migrations": _table(env, "SELECT * FROM data_migrations ORDER BY name"),
        "requests": _table(env, "SELECT * FROM ops_requests ORDER BY id"),
        "idempotency": _table(env, "SELECT * FROM ops_idempotency ORDER BY rowid"),
        "activity_raw": env.activity_path.read_bytes() if env.activity_path.exists() else None,
        "activity": [line for line in _activity(env).splitlines() if "\tpolicy_" in line],
        "ops_activity": [line for line in _activity(env).splitlines() if "\tops_" in line],
    }


def _phases(env, endpoint: str) -> list[tuple]:
    return [(row["phase"], row["result_code"]) for row in env.conn.execute(
        "SELECT phase,result_code FROM ops_requests WHERE endpoint=? ORDER BY id", (endpoint,))]


def _write_unreadable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe not utf-8\n")


# ------------------------------------------------------------ principal x endpoint

@pytest.mark.parametrize("principal", [OPERATOR, APPROVER, OPS_BOT, OPS_WEB, OPS_NOTIFY])
def test_policy_endpoints_admit_only_operator_and_approver_over_uds(api5, principal):
    env = api5.env
    _seed(api5.service, "secret directive alpha", "secret directive beta")
    env.policy_path.write_bytes(env.policy_path.read_bytes() + b"hand written secret\n")
    allowed = principal in (OPERATOR, APPROVER)
    client = api5.client(principal)
    headers = {H: ACTOR} if principal is OPS_BOT else {}
    before_rows = len(_table(env, "SELECT * FROM ops_requests"))

    show = client.get("/v1/policy", headers=headers)
    remove = client.post("/v1/policy/1/remove", json={},
                         headers={"Idempotency-Key": "p-remove", **headers})
    reconcile = client.post("/v1/policy/reconcile", json={},
                            headers={"Idempotency-Key": "p-reconcile", **headers})

    if allowed:
        assert show.status_code == 200
        assert show.json()["data"]["managed"][0]["directive"] == "secret directive alpha"
        assert remove.status_code == 200 and remove.json()["data"] == {"id": 1}
        assert reconcile.status_code == 200
        assert reconcile.json()["data"]["projection"] == {"status": "ok", "reasons": []}
        assert [row[0] for row in _table(env, "SELECT id FROM ops_policies ORDER BY id")] == [2]
    else:
        for response in (show, remove, reconcile):
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "forbidden"
            assert "secret" not in response.text and "directive" not in response.text
        assert [row[0] for row in _table(env, "SELECT id FROM ops_policies ORDER BY id")] == [1, 2]
        assert len(_table(env, "SELECT * FROM ops_requests")) == before_rows


# ------------------------------------------------------------ show の合計上限

@pytest.mark.parametrize("count,has_more", [(199, False), (200, False), (201, True)])
def test_policy_show_total_limit_with_only_managed_rows(ops_env, count, has_more):
    service = ops_env.service()
    ops_env.conn.executemany(
        "INSERT INTO ops_policies(request_id,directive,created_at) VALUES (?,?,?)",
        [(f"r{n}", f"d{n}", "2026-10-23T00:00:00+00:00") for n in range(1, count + 1)])
    ops_env.conn.commit()

    result = service.show_policy(OPERATOR)

    assert [row["id"] for row in result["managed"]] == list(range(1, min(count, 200) + 1))
    assert result["unmanaged"] == []
    assert result["has_more"] is has_more
    assert result["limit"] == 200


@pytest.mark.parametrize("manual,listed,has_more", [(49, 49, False), (50, 50, False),
                                                    (51, 50, True), (60, 50, True)])
def test_policy_show_gives_managed_rows_the_budget_first(ops_env, manual, listed, has_more):
    service = ops_env.service()
    ops_env.conn.executemany(
        "INSERT INTO ops_policies(request_id,directive,created_at) VALUES (?,?,?)",
        [(f"r{n}", f"d{n}", "2026-10-23T00:00:00+00:00") for n in range(1, 151)])
    ops_env.conn.commit()
    ops_env.policy_path.parent.mkdir(parents=True)
    ops_env.policy_path.write_text("".join(f"manual {n}\n" for n in range(1, manual + 1)),
                                   encoding="utf-8")

    result = service.show_policy(OPERATOR)

    assert [row["id"] for row in result["managed"]] == list(range(1, 151))
    assert [row["line"] for row in result["unmanaged"]] == list(range(1, listed + 1))
    assert [row["directive"] for row in result["unmanaged"]][:2] == [
        "manual 1", "manual 2"][:min(2, listed)]
    assert len(result["managed"]) + len(result["unmanaged"]) == min(150 + manual, 200)
    assert result["has_more"] is has_more


def test_policy_show_limit_and_order_over_real_uds(api):
    env = api.env
    env.conn.executemany(
        "INSERT INTO ops_policies(request_id,directive,created_at) VALUES (?,?,?)",
        [(f"r{n}", f"d{n}", "2026-10-23T00:00:00+00:00") for n in range(1, 151)])
    env.conn.commit()
    env.policy_path.parent.mkdir(parents=True)
    env.policy_path.write_text("".join(f"manual {n}\n" for n in range(1, 61)), encoding="utf-8")

    data = api.client(Principal.OPERATOR).get("/v1/policy").json()["data"]

    assert [row["id"] for row in data["managed"]] == list(range(1, 151))
    assert [row["line"] for row in data["unmanaged"]] == list(range(1, 51))
    assert data["has_more"] is True and data["limit"] == 200


class _FixedShowApi:
    root = None
    principal = Principal.OPERATOR

    def __init__(self, data):
        self.data = data

    def request(self, method, path, *, body=None, params=None, idempotent=False):
        assert (method, path, idempotent) == ("GET", "/v1/policy", False)
        return Reply(200, self.data, None)


def _show_data(directive, *, unmanaged=(), status="ok", reasons=()):
    return {"managed": [{"id": 1, "created_at": "at", "directive": directive}],
            "unmanaged": list(unmanaged), "limit": 200, "has_more": False,
            "projection": {"status": status, "reasons": list(reasons)}}


def _run_show(data):
    out = io.StringIO()
    rc = Runner(_FixedShowApi(data), stdin=io.StringIO(), stdout=out).run(parse(["policy", "show"]))
    return rc, out.getvalue()


@pytest.mark.parametrize("length,shown", [(199, 199), (200, 200), (201, 199)])
def test_policy_show_cli_cuts_directive_at_200_characters(length, shown):
    rc, text = _run_show(_show_data("x" * length))

    assert rc == 0
    cell = text.splitlines()[1].removeprefix("  #1 at ")
    assert cell == "x" * shown + ("…" if length > 200 else "")
    assert len(cell) <= 200


def test_policy_show_cli_cuts_unmanaged_directive_the_same_way():
    rc, text = _run_show(_show_data("d", unmanaged=[
        {"line": 2, "directive": "y" * 500, "removable": False}]))

    assert rc == 0
    cell = text.splitlines()[3].removeprefix("  line 2 ")
    assert cell == "y" * 199 + "…" and len(cell) == 200


@pytest.mark.parametrize("reasons", [None, [], "missing", {"a": 1}])
def test_policy_show_cli_stale_without_a_usable_reason_list_uses_the_generic_sentence(reasons):
    data = _show_data("d", status="stale")
    data["projection"]["reasons"] = reasons
    rc, text = _run_show(data)

    assert rc == 0
    assert text.splitlines()[-1] == (
        "投影: stale (unknown)。Mission に入る方針が DB と一致していません。"
        "afx ctl policy reconcile で DB に合わせられます。")


@pytest.mark.parametrize("reasons,advice", [
    (["unreadable"], "policy/directives.md を読める状態 (権限、UTF-8) に直してから "
                     "afx ctl policy reconcile を実行してください。"),
    (["malformed"], "marker を手修正してから afx ctl policy reconcile を実行してください。"),
    (["missing", "order"], "Mission に入る方針が DB と一致していません。"
                           "afx ctl policy reconcile で DB に合わせられます。")])
def test_policy_show_cli_stale_advice_by_reason(reasons, advice):
    rc, text = _run_show(_show_data("d", status="stale", reasons=reasons))

    assert rc == 0
    assert text.splitlines()[-1] == f"投影: stale ({', '.join(reasons)})。" + advice


@pytest.mark.parametrize("directive", [
    "first\n  #99 2026-01-01 forged", "a\r\n  line 7 forged", "x\x1b[2J\x00y z\u0085w",
    "tab\there\vvt\fff"])
def test_policy_show_cli_cannot_forge_a_line_with_control_characters(directive):
    rc, text = _run_show(_show_data(directive, unmanaged=[
        {"line": 2, "directive": directive, "removable": False}]))

    assert rc == 0
    assert len(text.splitlines()) == 5
    assert text.splitlines()[1].startswith("  #1 at ")
    assert text.splitlines()[3].startswith("  line 2 ")
    for forbidden in ("\x1b", "\x00", " ", "\u0085", "\r", "\v", "\f"):
        assert forbidden not in text


# ------------------------------------------------------------ remove の hard crash

def _crash_remove(env, policy_id: int) -> None:
    proc = subprocess.Popen([sys.executable, str(CHILD), str(env.root), str(policy_id)],
                            stdin=subprocess.DEVNULL)
    assert proc.wait(60) == -signal.SIGKILL


def _restart(env):
    service = env.service()
    activity = ActivityLog(env.activity_path)
    assert _recover_ops(service, env.conn, activity) is True
    return service


def test_remove_killed_between_delete_and_replace_becomes_outcome_unknown_and_converges(ops_env):
    service = ops_env.service()
    _seed(service, "A", "B")
    assert ops_env.policy_path.read_bytes() == _block(1, "A") + _block(2, "B")

    _crash_remove(ops_env, 1)

    assert [row[0] for row in _table(ops_env, "SELECT id FROM ops_policies ORDER BY id")] == [2]
    assert ops_env.policy_path.read_bytes() == _block(1, "A") + _block(2, "B")
    assert _phases(ops_env, REMOVE) == [("accepted", None)]

    restarted = _restart(ops_env)

    assert _phases(ops_env, REMOVE) == [("accepted", None),
                                        ("outcome_unknown", "outcome_unknown")]
    assert ops_env.policy_path.read_bytes() == _block(2, "B")
    with pytest.raises(OpsError) as replayed:
        restarted.remove_policy(OPERATOR, "key-r", 1)
    assert replayed.value.code is ErrorCode.OUTCOME_UNKNOWN
    assert _count(ops_env, "policy_removed") == 0


def test_removing_the_last_managed_row_and_dying_leaves_no_block_after_restart(ops_env):
    service = ops_env.service()
    _seed(service, "only")
    ops_env.policy_path.write_bytes(b"hand\n" + ops_env.policy_path.read_bytes())

    _crash_remove(ops_env, 1)

    assert _table(ops_env, "SELECT id FROM ops_policies ORDER BY id") == []
    assert b"managed-policy" in ops_env.policy_path.read_bytes()

    _restart(ops_env)

    assert ops_env.policy_path.read_bytes() == b"hand\n"
    assert _phases(ops_env, REMOVE)[-1] == ("outcome_unknown", "outcome_unknown")


# ------------------------------------------------------------ remove の収束と not_found

def test_failed_replace_then_another_key_remove_converges_before_not_found(ops_env):
    service = ops_env.service()
    _seed(service, "A", "B")
    service._regenerate_policy = _boom
    with pytest.raises(OpsError) as first:
        service.remove_policy(OPERATOR, "remove-a", 1)
    assert first.value.code is ErrorCode.OUTCOME_UNKNOWN
    del service._regenerate_policy
    assert ops_env.policy_path.read_bytes() == _block(1, "A") + _block(2, "B")
    assert _count(ops_env, "policy_reconciled") == 0

    with pytest.raises(OpsError) as second:
        service.remove_policy(OPERATOR, "remove-b", 1)

    assert second.value.code is ErrorCode.NOT_FOUND
    assert ops_env.policy_path.read_bytes() == _block(2, "B")
    assert _count(ops_env, "policy_reconciled") == 1
    assert _count(ops_env, "policy_removed") == 0
    assert _phases(ops_env, REMOVE) == [
        ("accepted", None), ("outcome_unknown", "outcome_unknown"),
        ("accepted", None), ("failed", "not_found")]


def test_removing_an_already_removed_id_changes_nothing_but_the_audit_rows(ops_env):
    service = ops_env.service()
    _seed(service, "A", "B")
    service.remove_policy(OPERATOR, "remove-1", 1)
    before = _snapshot(ops_env)
    mtime = ops_env.policy_path.stat().st_mtime_ns

    with pytest.raises(OpsError) as raised:
        service.remove_policy(OPERATOR, "remove-2", 1)

    after = _snapshot(ops_env)
    assert raised.value.code is ErrorCode.NOT_FOUND
    for key in ("file", "policies", "migrations", "activity"):
        assert after[key] == before[key], key
    assert len(after["ops_activity"]) == len(before["ops_activity"]) + 1
    assert ops_env.policy_path.stat().st_mtime_ns == mtime
    assert len(after["requests"]) == len(before["requests"]) + 2


@pytest.mark.parametrize("variant", ["duplicate", "content", "order"])
def test_failed_replace_then_extra_drift_still_converges_on_next_remove(ops_env, variant):
    service = ops_env.service()
    _seed(service, "A", "B")
    service._regenerate_policy = _boom
    with pytest.raises(OpsError):
        service.remove_policy(OPERATOR, "remove-a", 1)
    del service._regenerate_policy
    drifted = {
        "duplicate": b"hand\n" + _block(1, "A") + _block(2, "B") + _block(2, "B"),
        "content": b"hand\n" + _block(1, "A") + _block(2, "C"),
        "order": b"hand\n" + _block(2, "B") + _block(1, "A"),
    }[variant]
    ops_env.policy_path.write_bytes(drifted)

    with pytest.raises(OpsError) as raised:
        service.remove_policy(OPERATOR, "remove-b", 1)

    assert raised.value.code is ErrorCode.NOT_FOUND
    assert ops_env.policy_path.read_bytes() == b"hand\n" + _block(2, "B")
    assert _count(ops_env, "policy_reconciled") == 1


@pytest.mark.parametrize("argv", [["policy", "remove", "0"], ["policy", "remove", "-1"],
                                  ["policy", "remove", "abc"], ["policy", "remove", "1.5"],
                                  ["policy", "remove", ""], ["policy", "remove"],
                                  ["policy", "remove", "1", "2"]])
def test_remove_with_an_unusable_id_never_reads_a_key_or_connects(tmp_path, monkeypatch, argv):
    calls = []

    def spy(name):
        def inner(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(name)
        return inner

    monkeypatch.setattr(client_module.keys, "read_token", spy("read_token"))
    monkeypatch.setattr(client_module.ApiClient, "request", spy("request"))
    monkeypatch.setattr(client_module.ApiClient, "_send", spy("_send"))
    monkeypatch.setattr(socket.socket, "connect", spy("connect"))
    err = io.StringIO()

    rc = client_module.main(argv, root=tmp_path, stdin=io.StringIO(), stdout=io.StringIO(),
                            stderr=err)

    assert rc == 2 and calls == []
    assert err.getvalue().startswith("afx ctl:")


@pytest.mark.parametrize("argv", [["policy", "show", "x"], ["policy", "reconcile", "x"]])
def test_show_and_reconcile_reject_extra_arguments_before_connecting(tmp_path, monkeypatch, argv):
    calls = []
    monkeypatch.setattr(client_module.keys, "read_token",
                        lambda *a, **k: calls.append("read_token"))
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: calls.append("connect"))

    rc = client_module.main(argv, root=tmp_path, stdin=io.StringIO(), stdout=io.StringIO(),
                            stderr=io.StringIO())

    assert rc == 2 and calls == []


# ------------------------------------------------------------ remove の byte 保存

def test_remove_preserves_handwritten_and_shell_lines_and_writes_two_audit_rows(ops_env):
    service = ops_env.service()
    ops_env.policy_path.parent.mkdir(parents=True)
    ops_env.policy_path.write_bytes(b"hand\r\n\n- shell line not in db\n")
    _seed(service, "A", "B")

    service.remove_policy(OPERATOR, "remove-1", 1)

    assert ops_env.policy_path.read_bytes() == (
        b"hand\r\n\n- shell line not in db\n" + _block(2, "B"))
    assert [row[0] for row in _table(ops_env, "SELECT id FROM ops_policies ORDER BY id")] == [2]
    assert _count(ops_env, "policy_removed") == 1
    assert _phases(ops_env, REMOVE) == [("accepted", None), ("succeeded", "ok")]
    assert {row[0] for row in _table(ops_env, "SELECT DISTINCT phase FROM ops_requests")} <= {
        "accepted", "succeeded", "failed", "outcome_unknown"}


def test_remove_refuses_a_file_with_an_unpaired_marker_and_keeps_every_byte(ops_env):
    service = ops_env.service()
    _seed(service, "A")
    broken = (b"<!-- afx:managed-policy id=3 -->\n- A\n<!-- /afx:managed-policy id=5 -->\n")
    ops_env.policy_path.write_bytes(broken)
    before = _snapshot(ops_env)

    with pytest.raises(policy_projection.ProjectionMalformedError):
        service.remove_policy(OPERATOR, "remove-1", 1)

    after = _snapshot(ops_env)
    for key in ("file", "policies", "migrations", "activity"):
        assert after[key] == before[key], key
    assert len(after["ops_activity"]) == len(before["ops_activity"]) + 1
    assert _phases(ops_env, REMOVE) == [("accepted", None), ("failed", "internal")]


# ------------------------------------------------------------ add の commit 後失敗

@pytest.mark.parametrize("follow", ["add", "remove", "reconcile"])
def test_add_that_fails_after_insert_is_unknown_stale_until_the_next_mutation(ops_env, follow):
    service = ops_env.service()
    _seed(service, "A")
    service._regenerate_policy = _boom
    with pytest.raises(OpsError) as first:
        service.add_policy(OPERATOR, "same-key", "B")
    assert first.value.code is ErrorCode.OUTCOME_UNKNOWN
    del service._regenerate_policy
    assert _count(ops_env, "policy_added") == 1
    assert _phases(ops_env, ADD)[-2:] == [("accepted", None),
                                          ("outcome_unknown", "outcome_unknown")]
    with pytest.raises(OpsError) as replayed:
        service.add_policy(OPERATOR, "same-key", "B")
    assert replayed.value.code is ErrorCode.OUTCOME_UNKNOWN

    before = _snapshot(ops_env)
    shown = service.show_policy(OPERATOR)
    assert shown["projection"] == {"status": "stale", "reasons": ["missing"]}
    assert [row["directive"] for row in shown["managed"]] == ["A", "B"]
    assert _snapshot(ops_env) == before
    assert ops_env.policy_path.read_bytes() == _block(1, "A")

    if follow == "add":
        service.add_policy(OPERATOR, "next", "C")
        expected = _block(1, "A") + _block(2, "B") + _block(3, "C")
    elif follow == "remove":
        service.remove_policy(OPERATOR, "next", 1)
        expected = _block(2, "B")
    else:
        assert service.reconcile_policy(OPERATOR, "next")["changed"] is True
        expected = _block(1, "A") + _block(2, "B")
    assert ops_env.policy_path.read_bytes() == expected
    assert service.show_policy(OPERATOR)["projection"] == {"status": "ok", "reasons": []}
    assert _count(ops_env, "policy_added") == (2 if follow == "add" else 1)


def _break_file(env, how: str) -> None:
    if how == "unreadable":
        _write_unreadable(env.policy_path)
    else:
        env.policy_path.write_bytes(
            b"<!-- afx:managed-policy id=3 -->\n- A\n<!-- /afx:managed-policy id=5 -->\n")


@pytest.mark.parametrize("how", ["unreadable", "malformed"])
@pytest.mark.parametrize("operation", ["add", "remove"])
def test_mutation_with_an_unsafe_file_fails_internal_without_touching_the_database(
        ops_env, how, operation):
    service = ops_env.service()
    _seed(service, "A")
    _break_file(ops_env, how)
    before = _snapshot(ops_env)

    expected = (policy_projection.ProjectionUnreadableError if how == "unreadable"
                else policy_projection.ProjectionMalformedError)
    with pytest.raises(expected):
        if operation == "add":
            service.add_policy(OPERATOR, "next", "B")
        else:
            service.remove_policy(OPERATOR, "next", 1)

    after = _snapshot(ops_env)
    for key in ("file", "policies", "migrations", "activity"):
        assert after[key] == before[key], key
    assert len(after["ops_activity"]) == len(before["ops_activity"]) + 1
    endpoint = ADD if operation == "add" else REMOVE
    assert _phases(ops_env, endpoint)[-2:] == [("accepted", None), ("failed", "internal")]
    assert all(row["phase"] != "outcome_unknown" for row in ops_env.ops_rows())


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode 000 files")
def test_add_with_a_mode_000_file_fails_internal_and_keeps_inode_and_mode(ops_env):
    service = ops_env.service()
    _seed(service, "A")
    ops_env.policy_path.chmod(0)
    stat = ops_env.policy_path.stat()
    try:
        with pytest.raises(policy_projection.ProjectionUnreadableError):
            service.add_policy(OPERATOR, "next", "B")
    finally:
        ops_env.policy_path.chmod(0o600)
    assert ops_env.policy_path.stat().st_ino == stat.st_ino
    assert [row[0] for row in _table(ops_env, "SELECT id FROM ops_policies ORDER BY id")] == [1]


def _legacy_pending(env):
    env.policy_path.parent.mkdir(parents=True, exist_ok=True)
    env.policy_path.write_bytes(b"- A\n")
    env.conn.execute("INSERT INTO ops_policies(request_id,directive,created_at) VALUES "
                     "('seed','A','2026-10-23T00:00:00+00:00')")
    env.conn.commit()


@pytest.mark.parametrize("operation", ["add", "remove"])
def test_unknown_migration_record_before_a_mutation_fails_internal_not_unknown(
        ops_env, monkeypatch, operation):
    service = ops_env.service()
    _legacy_pending(ops_env)

    def refuse(*_args, **_kwargs):
        raise RuntimeError("injected record failure")

    monkeypatch.setattr(policy_projection, "_record_migration", refuse)
    rows_before = _table(ops_env, "SELECT * FROM ops_policies")

    with pytest.raises(policy_projection.MigrationRecordUnknown):
        if operation == "add":
            service.add_policy(OPERATOR, "next", "B")
        else:
            service.remove_policy(OPERATOR, "next", 1)

    assert _table(ops_env, "SELECT * FROM ops_policies") == rows_before
    assert _phases(ops_env, ADD if operation == "add" else REMOVE)[-1] == ("failed", "internal")
    assert all(row["phase"] != "outcome_unknown" for row in ops_env.ops_rows())


# ------------------------------------------------------------ show は純読取

def _build_projection(env, service, name: str) -> None:
    _seed(service, "A", "B")
    path = env.policy_path
    if name == "missing":
        path.write_bytes(_block(1, "A"))
    elif name == "orphan":
        path.write_bytes(_block(1, "A") + _block(2, "B") + _block(9, "Z"))
    elif name == "duplicate":
        path.write_bytes(_block(1, "A") + _block(1, "A") + _block(2, "B"))
    elif name == "content":
        path.write_bytes(_block(1, "A") + _block(2, "C"))
    elif name == "order":
        path.write_bytes(_block(2, "B") + _block(1, "A"))
    elif name == "migration_pending":
        env.conn.execute("DELETE FROM data_migrations")
        env.conn.commit()
    elif name == "all_reasons":
        env.conn.execute("DELETE FROM data_migrations")
        env.conn.commit()
        path.write_bytes(_block(9, "Z"))
    else:
        assert name == "ok"


_EXPECTED_REASONS = {
    "ok": ("ok", []), "missing": ("stale", ["missing"]), "orphan": ("stale", ["orphan"]),
    "duplicate": ("stale", ["duplicate"]), "content": ("stale", ["content"]),
    "order": ("stale", ["order"]), "migration_pending": ("stale", ["migration_pending"]),
    "all_reasons": ("stale", ["migration_pending", "missing", "orphan"]),
}


@pytest.mark.parametrize("name", list(_EXPECTED_REASONS))
def test_policy_show_is_a_pure_read_over_uds(api, monkeypatch, name):
    env = api.env
    _build_projection(env, api.service, name)
    calls = []

    def spy(label):
        def inner(*_args, **_kwargs):
            calls.append(label)
            raise AssertionError(label)
        return inner

    monkeypatch.setattr(api.service, "_reconcile_policy", spy("service._reconcile_policy"))
    monkeypatch.setattr(api.service, "_regenerate_policy", spy("service._regenerate_policy"))
    monkeypatch.setattr(service_module, "reconcile_policy_file", spy("svc.reconcile"))
    monkeypatch.setattr(service_module, "regenerate_policy_file", spy("svc.regenerate"))
    for attribute in ("reconcile_policy_file", "regenerate_policy_file", "_atomic_replace",
                      "_record_migration", "insert_policy"):
        monkeypatch.setattr(policy_projection, attribute, spy(attribute))
    before = _snapshot(env)
    mtime = env.policy_path.stat().st_mtime_ns

    response = api.client(Principal.OPERATOR).get("/v1/policy")

    assert response.status_code == 200
    status, reasons = _EXPECTED_REASONS[name]
    assert response.json()["data"]["projection"] == {"status": status, "reasons": reasons}
    assert calls == []
    assert _snapshot(env) == before
    assert env.policy_path.stat().st_mtime_ns == mtime


class _ServiceApi:
    root = None
    principal = Principal.OPERATOR

    def __init__(self, service):
        self.service = service

    def request(self, method, path, *, body=None, params=None, idempotent=False):
        assert (method, path) == ("GET", "/v1/policy")
        return Reply(200, self.service.show_policy(OPERATOR), None)


def _cli_show(service) -> tuple[int, list[str]]:
    out = io.StringIO()
    rc = Runner(_ServiceApi(service), stdin=io.StringIO(), stdout=out).run(parse(["policy", "show"]))
    return rc, out.getvalue().splitlines()


_GENERAL = ("。Mission に入る方針が DB と一致していません。"
            "afx ctl policy reconcile で DB に合わせられます。")
_MALFORMED = "。marker を手修正してから afx ctl policy reconcile を実行してください。"
_UNREADABLE = ("。policy/directives.md を読める状態 (権限、UTF-8) に直してから "
               "afx ctl policy reconcile を実行してください。")


def test_policy_show_cli_prints_the_fixed_stale_sentences_with_rc_zero(ops_env):
    service = ops_env.service()
    _seed(service, "A", "B")

    rc, lines = _cli_show(service)
    assert rc == 0 and lines[-1] == "投影: ok"
    assert lines[0] == "管理対象:" and lines[1].startswith("  #1 ")

    ops_env.policy_path.write_bytes(_block(9, "Z") + _block(1, "A"))
    rc, lines = _cli_show(service)
    assert rc == 0
    assert lines[-1] == "投影: stale (missing, orphan)" + _GENERAL

    ops_env.policy_path.write_bytes(
        b"<!-- afx:managed-policy id=3 -->\n- A\n<!-- /afx:managed-policy id=5 -->\n")
    rc, lines = _cli_show(service)
    assert rc == 0
    assert lines[-1].startswith("投影: stale (") and lines[-1].endswith(_MALFORMED)
    assert "malformed" in lines[-1]
    assert "  line 1 <!-- afx:managed-policy id=3 -->" in lines

    _write_unreadable(ops_env.policy_path)
    rc, lines = _cli_show(service)
    assert rc == 0
    assert lines[-1] == "投影: stale (unreadable)" + _UNREADABLE
    assert lines[lines.index("管理外 (API から削除できません):") + 1] == "  0 件"


def test_policy_show_cli_prefers_the_unreadable_sentence_when_both_reasons_exist():
    rc, text = _run_show(_show_data("d", status="stale", reasons=["unreadable", "malformed"]))
    assert rc == 0
    assert text.splitlines()[-1] == "投影: stale (unreadable, malformed)" + _UNREADABLE


# ------------------------------------------------------------ reconcile

def _stale_canonical(env, service):
    _seed(service, "A", "B")
    env.policy_path.write_bytes(b"hand\r\n" + _block(2, "B") + _block(1, "Z"))


def test_reconcile_changes_a_stale_file_to_canonical_bytes_with_one_activity_row(ops_env):
    service = ops_env.service()
    _stale_canonical(ops_env, service)
    activity_before = _count(ops_env, "policy_reconciled")

    reply = service.reconcile_policy(OPERATOR, "rec-1")

    assert reply == {"changed": True, "projection": {"status": "ok", "reasons": []}}
    assert ops_env.policy_path.read_bytes() == b"hand\r\n" + _block(1, "A") + _block(2, "B")
    assert _count(ops_env, "policy_reconciled") == activity_before + 1
    assert _phases(ops_env, RECONCILE) == [("accepted", None), ("succeeded", "ok")]
    before = _snapshot(ops_env)

    assert service.reconcile_policy(OPERATOR, "rec-1") == reply
    assert _snapshot(ops_env) == before


def test_reconcile_of_an_ok_file_touches_nothing_and_leaves_no_activity(ops_env):
    service = ops_env.service()
    _seed(service, "A")
    stat = ops_env.policy_path.stat()
    activity = _snapshot(ops_env)["activity"]

    reply = service.reconcile_policy(OPERATOR, "rec-1")

    assert reply == {"changed": False, "projection": {"status": "ok", "reasons": []}}
    after = ops_env.policy_path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (stat.st_ino, stat.st_mtime_ns)
    assert _snapshot(ops_env)["activity"] == activity
    assert _count(ops_env, "policy_reconciled") == 0
    assert _count(ops_env, "ops_policy_reconcile") == 1
    assert _phases(ops_env, RECONCILE) == [("accepted", None), ("succeeded", "ok")]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode 000 files")
def test_reconcile_of_a_mode_000_file_fails_internal_and_keeps_inode_and_mode(ops_env):
    service = ops_env.service()
    _seed(service, "A")
    ops_env.policy_path.chmod(0)
    stat = ops_env.policy_path.stat()
    try:
        with pytest.raises(policy_projection.ProjectionUnreadableError):
            service.reconcile_policy(OPERATOR, "rec-1")
        after = ops_env.policy_path.stat()
    finally:
        ops_env.policy_path.chmod(0o600)

    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (
        stat.st_ino, stat.st_mode, stat.st_mtime_ns)
    assert _phases(ops_env, RECONCILE) == [("accepted", None), ("failed", "internal")]


@pytest.mark.parametrize("how", ["unreadable", "malformed"])
def test_reconcile_of_an_unsafe_file_fails_internal_and_keeps_every_byte(ops_env, how):
    service = ops_env.service()
    _seed(service, "A")
    _break_file(ops_env, how)
    stat = ops_env.policy_path.stat()
    before = _snapshot(ops_env)

    expected = (policy_projection.ProjectionUnreadableError if how == "unreadable"
                else policy_projection.ProjectionMalformedError)
    with pytest.raises(expected):
        service.reconcile_policy(OPERATOR, "rec-1")

    after = _snapshot(ops_env)
    assert after["file"] == before["file"]
    assert ops_env.policy_path.stat().st_ino == stat.st_ino
    assert after["policies"] == before["policies"] and after["activity"] == before["activity"]
    assert _phases(ops_env, RECONCILE) == [("accepted", None), ("failed", "internal")]
    with pytest.raises(OpsError) as replayed:
        service.reconcile_policy(OPERATOR, "rec-1")
    assert replayed.value.code is ErrorCode.INTERNAL
    assert _phases(ops_env, RECONCILE) == [("accepted", None), ("failed", "internal")]


def test_reconcile_unknown_after_migration_replace_replays_unknown_then_records_only(
        ops_env, monkeypatch):
    service = ops_env.service()
    _legacy_pending(ops_env)

    def refuse(*_args, **_kwargs):
        raise RuntimeError("injected record failure")

    with monkeypatch.context() as patched:
        patched.setattr(policy_projection, "_record_migration", refuse)
        with pytest.raises(OpsError) as raised:
            service.reconcile_policy(OPERATOR, "rec-1")
    assert raised.value.code is ErrorCode.OUTCOME_UNKNOWN
    assert ops_env.policy_path.read_bytes() == _block(1, "A")
    assert _migration_rows(ops_env) == []
    assert _phases(ops_env, RECONCILE) == [("accepted", None),
                                           ("outcome_unknown", "outcome_unknown")]
    with pytest.raises(OpsError) as replayed:
        service.reconcile_policy(OPERATOR, "rec-1")
    assert replayed.value.code is ErrorCode.OUTCOME_UNKNOWN

    again = service.reconcile_policy(OPERATOR, "rec-2")

    assert again == {"changed": False, "projection": {"status": "ok", "reasons": []}}
    assert len(_migration_rows(ops_env)) == 1
    assert ops_env.policy_path.read_bytes() == _block(1, "A")


def test_reconcile_unknown_when_the_inspect_after_replace_fails(ops_env, monkeypatch):
    service = ops_env.service()
    _stale_canonical(ops_env, service)

    def refuse(*_args, **_kwargs):
        raise OSError("injected inspect failure")

    with monkeypatch.context() as patched:
        patched.setattr(service_module, "inspect_policy_file", refuse)
        with pytest.raises(OpsError) as raised:
            service.reconcile_policy(OPERATOR, "rec-1")

    assert raised.value.code is ErrorCode.OUTCOME_UNKNOWN
    assert ops_env.policy_path.read_bytes() == b"hand\r\n" + _block(1, "A") + _block(2, "B")
    assert _phases(ops_env, RECONCILE) == [("accepted", None),
                                           ("outcome_unknown", "outcome_unknown")]


# ------------------------------------------------------------ 起動時 reconcile

def test_startup_reconcile_failure_leaves_an_activity_row_and_still_opens_the_listener(ops_env):
    service = ops_env.service()
    _seed(service, "A")
    _write_unreadable(ops_env.policy_path)
    activity = ActivityLog(ops_env.activity_path)

    assert _recover_ops(service, ops_env.conn, activity) is True

    assert _count(ops_env, "policy_reconcile_failed") == 1


def test_only_a_journal_recovery_failure_keeps_the_listener_closed(ops_env, monkeypatch):
    service = ops_env.service()
    activity = ActivityLog(ops_env.activity_path)

    def refuse():
        raise RuntimeError("injected")

    monkeypatch.setattr(service, "recover_after_journal", refuse)

    assert _recover_ops(service, ops_env.conn, activity) is False
    assert _count(ops_env, "ops_recover_failed") == 1
    assert _count(ops_env, "policy_reconcile_failed") == 0




@pytest.mark.parametrize("how", ["unreadable", "malformed"])
def test_unsafe_file_surfaces_as_internal_for_every_mutating_policy_endpoint(api, how):
    env = api.env
    _seed(api.service, "A")
    _break_file(env, how)
    before = _snapshot(env)
    client = api.client(Principal.OPERATOR)

    for number, (path, body) in enumerate((
            ("/v1/policy", {"text": "B"}), ("/v1/policy/1/remove", {}),
            ("/v1/policy/reconcile", {}))):
        response = client.post(path, json=body, headers={"Idempotency-Key": f"unsafe-{number}"})
        assert response.status_code == 500, path
        assert response.json()["error"]["code"] == "internal", path

    after = _snapshot(env)
    assert after["file"] == before["file"] and after["policies"] == before["policies"]
    assert all(row["phase"] != "outcome_unknown" for row in env.ops_rows())
