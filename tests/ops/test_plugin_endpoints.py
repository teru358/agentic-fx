"""GET /v1/plugins と GET /v1/plugins/{name}: 認可、名前の文法、世代 key の memo。"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout

import pytest

from agentic_fx.backtest.plugin_deploy_cli import plugin_list
from agentic_fx.core import plugin_files
from agentic_fx.ops.contracts import (PRINCIPAL_SCOPES, ErrorCode, OpsError, Principal,
                                      Scope)
from agentic_fx.ops.service import ENDPOINTS
from tests.backtest.factories import SETTINGS
from tests.plugin.read_model_world import World

OPS_WEB, OPS_NOTIFY = Principal.OPS_WEB, Principal.OPS_NOTIFY


@pytest.fixture
def world(ops_env):
    return World(ops_env.root, ops_env.conn)


def service(env, **kwargs):
    return env.service(**kwargs)


# ---- 形と認可 ----

def test_routes_and_scopes():
    assert ENDPOINTS["plugin_list"].route == "GET /v1/plugins"
    assert ENDPOINTS["plugin_list"].scope is Scope.APPROVALS_LIST
    assert ENDPOINTS["plugin_detail"].route == "GET /v1/plugins/{name}"
    assert ENDPOINTS["plugin_detail"].scope is Scope.APPROVALS_DETAIL
    assert not ENDPOINTS["plugin_list"].mutating and not ENDPOINTS["plugin_detail"].mutating


def test_ops_web_can_read_and_its_scopes_did_not_grow():
    assert PRINCIPAL_SCOPES[OPS_WEB] == frozenset({
        Scope.STATUS_READ, Scope.EVENTS_READ, Scope.APPROVALS_LIST, Scope.APPROVALS_DETAIL,
        Scope.LOGS_READ})
    assert Scope.APPROVALS_LIST not in PRINCIPAL_SCOPES[OPS_NOTIFY]


def test_every_principal_is_authorized_exactly_by_the_scope(ops_env, world):
    world.deploy("rsi")
    svc = service(ops_env)
    for principal in Principal:
        for code, call in (("plugin_list", lambda p: svc.plugin_list(p)),
                           ("plugin_detail", lambda p: svc.plugin_detail(p, "rsi"))):
            if ENDPOINTS[code].scope in PRINCIPAL_SCOPES[principal]:
                assert call(principal)
            else:
                with pytest.raises(OpsError) as raised:
                    call(principal)
                assert raised.value.code is ErrorCode.FORBIDDEN


# ---- 中身 ----

def test_the_listing_and_the_detail(ops_env, world):
    rsi = world.deploy("rsi")
    approval = world.request(rsi, status="approved")
    world.journal("rsi", approval)
    svc = service(ops_env)
    listing = svc.plugin_list(Principal.OPERATOR)
    assert listing["sources"] == {"live": "ok", "pending": "ok", "journal": "ok"}
    [row] = listing["plugins"]
    assert row["name"] == "rsi" and row["kind"] == "indicator"
    assert row["entity"]["state"] == "symlink"
    assert row["admission"]["state"] == "loaded"
    detail = svc.plugin_detail(Principal.OPERATOR, "rsi")
    assert detail["plugin"]["history"][0]["id"] == approval
    assert detail["plugin"]["versions"][0]["is_live"] is True
    json.dumps(listing), json.dumps(detail)


def test_the_name_is_checked_against_the_loader_grammar(ops_env, world):
    world.deploy("rsi")
    svc = service(ops_env)
    for bad in ("RSI", "r-s", "", "a" * 65, "../rsi", "rsi/x", "%2e%2e", "1rsi", "rsi\n"):
        with pytest.raises(OpsError) as raised:
            svc.plugin_detail(Principal.OPERATOR, bad)
        assert raised.value.code is ErrorCode.INVALID_ARGUMENT, repr(bad)
    with pytest.raises(OpsError) as raised:
        svc.plugin_detail(Principal.OPERATOR, "nothing")
    assert raised.value.code is ErrorCode.NOT_FOUND


def test_without_a_plugins_root_the_service_says_unavailable(ops_env):
    svc = service(ops_env, plugins_root=None)
    with pytest.raises(OpsError) as raised:
        svc.plugin_list(Principal.OPERATOR)
    assert raised.value.code is ErrorCode.UNAVAILABLE


def test_it_takes_no_plugin_lock(ops_env, world):
    from agentic_fx.plugin import switch
    world.deploy("rsi")
    svc = service(ops_env)

    def refuse(*args, **kwargs):
        raise AssertionError("must not lock")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(switch, "_plugin_lock", refuse)
        assert svc.plugin_list(Principal.OPERATOR)["plugins"]


# ---- 世代 key の memo ----

@pytest.fixture
def hash_calls():
    calls = []
    real = plugin_files.content_hash_of_dir

    def counting(directory, *args, **kwargs):
        calls.append(directory)
        return real(directory, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(plugin_files, "content_hash_of_dir", counting)
        yield calls


def test_repeated_requests_do_not_hash_the_plugins_again(ops_env, world, hash_calls):
    for name in ("a1", "a2", "a3"):
        world.deploy(name)
    svc = service(ops_env)
    svc.plugin_list(Principal.OPERATOR)
    first = len(hash_calls)
    assert first >= 3
    for _ in range(5):
        svc.plugin_list(Principal.OPERATOR)
        svc.plugin_detail(Principal.OPERATOR, "a1")
    assert len(hash_calls) == first
    assert svc._plugin_views.builds == 1


@pytest.mark.parametrize("change", ["approval_row", "approval_status", "journal", "deploy_op",
                                    "symlink", "plain_edit", "new_version_dir"])
def test_each_kind_of_deployment_change_rebuilds_once(ops_env, world, hash_calls, change):
    meta = world.deploy("rsi")
    plain = world.deploy("pln", plain=True)
    svc = service(ops_env)
    svc.plugin_list(Principal.OPERATOR)
    base = svc._plugin_views.builds
    if change == "approval_row":
        world.request(meta)
    elif change == "approval_status":
        approval = world.request(meta)
        svc.plugin_list(Principal.OPERATOR)
        base = svc._plugin_views.builds
        world.conn.execute("UPDATE approval_requests SET status='expired' WHERE id=?",
                           (approval,))
        world.conn.commit()
    elif change == "journal":
        world.journal("rsi", 1, phase="recorded")
    elif change == "deploy_op":
        world.deploy_op("rsi", op_kind="rollback_symlink", phase="intent")
    elif change == "symlink":
        world.undeploy("rsi")
    elif change == "plain_edit":
        with open(plain.path / "config.yaml", "a") as handle:
            handle.write("# edited\n")
    else:
        world.version_only("fresh")
    svc.plugin_list(Principal.OPERATOR)
    assert svc._plugin_views.builds == base + 1
    svc.plugin_list(Principal.OPERATOR)
    assert svc._plugin_views.builds == base + 1


@pytest.mark.parametrize("plain", [False, True])
def test_an_extra_py_file_in_the_plugin_folder_flips_the_cached_verdict(ops_env, world, plain):
    meta = world.deploy("rsi", plain=plain)
    world.request(meta, status="approved")
    svc = service(ops_env)

    def state():
        [row] = svc.plugin_list(Principal.OPERATOR)["plugins"]
        return row["entity"]["state"], row["admission"]["state"]

    before = state()
    assert before[0] in ("symlink", "plain") and before[1] == "loaded"
    base = svc._plugin_views.builds
    folder = meta.path.resolve()
    folder.chmod(0o755)
    extra = folder / "conftest.py"
    extra.write_text("# collected by pytest\n")
    during = state()
    assert during != before
    assert svc._plugin_views.builds == base + 1
    extra.unlink()
    assert state() == before
    assert svc._plugin_views.builds == base + 2


def test_a_phase_change_of_an_open_journal_is_seen(ops_env, world):
    meta = world.deploy("rsi")
    approval = world.request(meta, status="approved")
    op_id = world.journal("rsi", approval, phase="recorded")
    svc = service(ops_env)
    [row] = svc.plugin_list(Principal.OPERATOR)["plugins"]
    assert [op["op_id"] for op in row["open_ops"]] == [op_id]
    world.conn.execute("UPDATE plugin_switch_journal SET phase='decided', updated_at=? "
                       "WHERE op_id=?", ("2026-11-02T00:00:00+00:00", op_id))
    world.conn.commit()
    [row] = svc.plugin_list(Principal.OPERATOR)["plugins"]
    assert row["open_ops"] == []


def test_requests_with_wrongly_typed_fields_do_not_break_the_list_the_cli_or_the_detail(
        ops_env, world):
    rsi = world.deploy("rsi")
    world.request(rsi, status="approved")
    base = {"name": "mix", "content_hash": "a" * 64, "artifact_hash": "b" * 64}
    world.raw_request({**base, "kind": "strategy", "candidate_origin": ["staging"]})
    world.raw_request({**base, "kind": {"x": 1}, "candidate_origin": {"y": 2}})
    svc = service(ops_env)
    names = {row["name"] for row in svc.plugin_list(Principal.OPERATOR)["plugins"]}
    assert names == {"rsi", "mix"}
    detail = svc.plugin_detail(Principal.OPERATOR, "mix")
    assert [item["origin"] for item in detail["plugin"]["history"]] == [None, None]
    assert {row[0] for row in _cli_rows(ops_env.root)} == {"rsi", "mix"}


# ---- CLI との一致 ----

def _cli_rows(root, capsys_buffer=None):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        plugin_list(root, settings=SETTINGS)
    lines = buffer.getvalue().splitlines()[4:]
    return [line.split() for line in lines]


def test_the_cli_and_the_api_agree_on_the_same_inputs(ops_env, world):
    rsi = world.deploy("rsi")
    world.request(rsi, status="approved")
    strategy = world.deploy("mom", "strategy",
                            f"indicators:\n  a: {{plugin: rsi, pin: '{rsi.content_hash}'}}\n")
    world.request(strategy, inventory=(rsi,))
    world.request(strategy, kind="indicator", inventory=(rsi,))      # kind_mismatch
    brk = world.deploy("brk", "signal")
    world.deploy_op("gone", op_kind="rollback_symlink", phase="intent")
    (world.plugins / "junk").mkdir()
    svc = service(ops_env)
    api_rows = {row["name"]: row for row in svc.plugin_list(Principal.OPERATOR)["plugins"]}
    cli_rows = {row[0]: row for row in _cli_rows(ops_env.root)}
    assert set(api_rows) == set(cli_rows) == {"rsi", "mom", "brk", "gone", "junk"}
    for name, cli in cli_rows.items():
        api = api_rows[name]
        assert cli[1] == api["kind"], name
        live = api["entity"]["content_hash"]
        assert (cli[2] == "-") is (live is None)
        if live is not None:
            assert live.startswith(cli[2])
        assert (cli[3] == "-") is (api["pending_count"] == 0)
        assert cli[-1] == (",".join(api["issues"]) or "-")
    assert api_rows["brk"]["kind"] == "signal" and cli_rows["brk"][1] == "signal"
    assert api_rows["mom"]["issues"] == ["duplicate_pending", "kind_mismatch"]
    assert api_rows["junk"]["kind"] == "unknown"
    assert brk.kind == "signal"


# ---- wire ----

def test_over_the_wire_ops_web_reads_and_a_bad_name_is_a_400(api5):
    env = api5.env
    World(env.root, env.conn).deploy("rsi")
    web = api5.client(OPS_WEB)
    notify = api5.client(OPS_NOTIFY)
    listing = web.get("/v1/plugins")
    assert listing.status_code == 200
    assert [row["name"] for row in listing.json()["data"]["plugins"]] == ["rsi"]
    assert web.get("/v1/plugins/rsi").status_code == 200
    assert web.get("/v1/plugins/missing").status_code == 404
    for bad in ("%2e%2e", "RSI", "rsi%2Fx", "a" * 65):
        response = web.get(f"/v1/plugins/{bad}")
        assert response.status_code == 400, bad
        assert response.json()["error"]["code"] == "invalid_argument"
    assert notify.get("/v1/plugins").status_code == 403
    assert notify.get("/v1/plugins/rsi").status_code == 403
    assert os.path.exists(env.plugins_root / "rsi")
