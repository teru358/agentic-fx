from __future__ import annotations

import io

import pytest

from agentic_fx.ops import ErrorCode, OpsError
from agentic_fx.ops.client import ClientError, Op, Reply, Runner, parse
from agentic_fx.ops.contracts import Principal

from .conftest import OPERATOR


def test_policy_show_lists_canonical_rows_without_writing_projection(ops_env):
    service = ops_env.service()
    ops_env.conn.execute(
        "INSERT INTO ops_policies(request_id,directive,created_at) VALUES (?,?,?)",
        ("seed", "retain capital", "2026-10-23T00:00:00+00:00"),
    )
    ops_env.conn.commit()
    before = ops_env.policy_path.exists()

    result = service.show_policy(OPERATOR)

    assert result == {
        "managed": [{"id": 1, "created_at": "2026-10-23T00:00:00+00:00",
                     "directive": "retain capital"}],
        "unmanaged": [], "limit": 200, "has_more": False,
        "projection": {"status": "stale", "reasons": ["migration_pending", "missing"]},
    }
    assert ops_env.policy_path.exists() is before


def test_policy_remove_missing_row_is_not_found_after_accepted_audit(ops_env):
    service = ops_env.service()

    with pytest.raises(OpsError) as raised:
        service.remove_policy(OPERATOR, "key", 1)

    assert raised.value.code is ErrorCode.NOT_FOUND
    assert [(row["phase"], row["result_code"]) for row in ops_env.ops_rows()] == [
        ("accepted", None), ("failed", "not_found")]


def test_policy_client_parse_accepts_only_the_new_exact_forms():
    assert parse(["policy", "show"]) == Op("policy_show", [])
    assert parse(["policy", "remove", "12"]) == Op("policy_remove", [12])
    assert parse(["policy", "reconcile"]) == Op("policy_reconcile", [])
    for argv in (["policy", "show", "x"], ["policy", "reconcile", "x"],
                 ["policy", "remove"], ["policy", "remove", "0"],
                 ["policy", "remove", "-1"], ["policy", "remove", "abc"],
                 ["policy", "remove", "1.5"]):
        with pytest.raises(ClientError) as raised:
            parse(argv)
        assert raised.value.rc == 2


@pytest.mark.parametrize("count,has_more", [(199, False), (200, True), (201, True)])
def test_policy_show_limits_managed_rows_before_unmanaged_rows(ops_env, count, has_more):
    service = ops_env.service()
    ops_env.conn.executemany(
        "INSERT INTO ops_policies(request_id,directive,created_at) VALUES (?,?,?)",
        [(f"seed-{n}", f"directive-{n}", "2026-10-23T00:00:00+00:00")
         for n in range(1, count + 1)],
    )
    ops_env.conn.commit()
    ops_env.policy_path.parent.mkdir(parents=True)
    ops_env.policy_path.write_text("manual\n", encoding="utf-8")

    result = service.show_policy(OPERATOR)

    assert [row["id"] for row in result["managed"]] == list(range(1, min(count, 200) + 1))
    assert result["unmanaged"] == ([] if count >= 200 else [
        {"line": 1, "directive": "manual", "removable": False}])
    assert result["has_more"] is has_more
    assert result["limit"] == 200


def test_policy_remove_deletes_one_row_and_preserves_unmanaged_bytes(ops_env):
    service = ops_env.service()
    ops_env.policy_path.parent.mkdir(parents=True)
    ops_env.policy_path.write_bytes(b"outside\r\n")
    first = service.add_policy(OPERATOR, "add-1", "first")
    second = service.add_policy(OPERATOR, "add-2", "second")

    result = service.remove_policy(OPERATOR, "remove-1", 1)

    assert result == {"id": 1}
    assert [row["id"] for row in ops_env.conn.execute(
        "SELECT id FROM ops_policies ORDER BY id")] == [2]
    assert ops_env.policy_path.read_bytes() == (
        b"outside\r\n<!-- afx:managed-policy id=2 -->\n- second\n"
        b"<!-- /afx:managed-policy id=2 -->\n")
    lines = ops_env.activity_path.read_text(encoding="utf-8").splitlines()
    assert sum("\tpolicy_removed\tid=1" in line for line in lines) == 1
    rows = ops_env.conn.execute(
        "SELECT phase FROM ops_requests WHERE endpoint='POST /v1/policy/{id}/remove' ORDER BY id"
    ).fetchall()
    assert [row["phase"] for row in rows] == ["accepted", "succeeded"]
    assert first["id"] != second["id"]


def test_policy_add_projection_failure_is_outcome_unknown_and_replays(ops_env):
    service = ops_env.service()

    def fail_replace():
        raise OSError("injected")

    service._regenerate_policy = fail_replace
    with pytest.raises(OpsError) as raised:
        service.add_policy(OPERATOR, "unknown-add", "directive")
    assert raised.value.code is ErrorCode.OUTCOME_UNKNOWN
    with pytest.raises(OpsError) as replayed:
        service.add_policy(OPERATOR, "unknown-add", "directive")
    assert replayed.value.code is ErrorCode.OUTCOME_UNKNOWN
    assert [row["phase"] for row in ops_env.ops_rows()] == ["accepted", "outcome_unknown"]
    text = ops_env.activity_path.read_text(encoding="utf-8")
    assert "\tpolicy_added\t" not in text


def test_policy_remove_projection_failure_is_outcome_unknown_after_delete(ops_env):
    service = ops_env.service()
    service.add_policy(OPERATOR, "seed", "directive")

    def fail_replace():
        raise OSError("injected")

    service._regenerate_policy = fail_replace
    with pytest.raises(OpsError) as raised:
        service.remove_policy(OPERATOR, "unknown-remove", 1)

    assert raised.value.code is ErrorCode.OUTCOME_UNKNOWN
    assert ops_env.conn.execute("SELECT COUNT(*) FROM ops_policies").fetchone()[0] == 0
    rows = ops_env.conn.execute(
        "SELECT phase,result_code FROM ops_requests "
        "WHERE endpoint='POST /v1/policy/{id}/remove' ORDER BY id").fetchall()
    assert [(row["phase"], row["result_code"]) for row in rows] == [
        ("accepted", None), ("outcome_unknown", "outcome_unknown")]


def test_policy_reconcile_converges_then_is_unchanged(ops_env):
    service = ops_env.service()
    ops_env.conn.execute(
        "INSERT INTO ops_policies(request_id,directive,created_at) VALUES (?,?,?)",
        ("seed", "directive", "2026-10-23T00:00:00+00:00"),
    )
    ops_env.conn.commit()

    changed = service.reconcile_policy(OPERATOR, "reconcile-1")
    unchanged = service.reconcile_policy(OPERATOR, "reconcile-2")

    assert changed == {"changed": True, "projection": {"status": "ok", "reasons": []}}
    assert unchanged == {"changed": False, "projection": {"status": "ok", "reasons": []}}
    assert "\tpolicy_reconciled\t" in ops_env.activity_path.read_text(encoding="utf-8")


class _ShowApi:
    root = None
    principal = Principal.OPERATOR

    def request(self, method, path, *, body=None, params=None, idempotent=False):
        assert (method, path, body, idempotent) == ("GET", "/v1/policy", None, False)
        return Reply(200, {
            "managed": [{"id": 1, "created_at": "at", "directive": "x" * 201}],
            "unmanaged": [{"line": 3, "directive": "hand\nwritten", "removable": False}],
            "limit": 200, "has_more": False,
            "projection": {"status": "stale", "reasons": ["missing"]},
        }, None)


def test_policy_show_client_uses_fixed_headings_and_safe_200_character_cells():
    output = io.StringIO()

    assert Runner(_ShowApi(), stdin=io.StringIO(), stdout=output).run(parse(["policy", "show"])) == 0

    lines = output.getvalue().splitlines()
    assert lines[0] == "管理対象:"
    assert lines[1] == "  #1 at " + "x" * 200 + "…"
    assert lines[2] == "管理外 (API から削除できません):"
    assert lines[3] == "  line 3 hand written"
    assert lines[4] == ("投影: stale (missing)。Mission に入る方針が DB と一致していません。"
                        "afx ctl policy reconcile で DB に合わせられます。")
