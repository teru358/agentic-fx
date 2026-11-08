"""GET /v1/plugins と GET /v1/plugins/{name}: 認可、名前の文法、世代 key の memo。"""
from __future__ import annotations

import io
import json
import os
import time
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


# ---- 再構築は lane の外、劣化した結果は memo しない ----

def _rows_of(svc):
    return svc.plugin_list(Principal.OPERATOR)["plugins"]


@pytest.mark.parametrize("fault", ["approval_table", "journal_table", "live_scan"])
def test_a_degraded_read_is_shown_but_not_remembered(ops_env, world, fault):
    from agentic_fx.plugin import read_model, read_model_inputs
    world.deploy("rsi")
    svc = service(ops_env)
    real_db, real_scan = read_model_inputs.read_db_inputs, read_model_inputs.scan_live
    calls = {"n": 0}

    def flaky_db(conn):
        rows = real_db(conn)
        if calls["n"] == 0 and fault == "approval_table":
            rows = {**rows, "approvals": (), "pending_status": read_model.SOURCE_UNAVAILABLE}
        if calls["n"] == 0 and fault == "journal_table":
            rows = {**rows, "journals": (), "journal_status": read_model.SOURCE_UNAVAILABLE}
        calls["n"] += 1
        return rows

    def flaky_scan(root):
        calls["scans"] = calls.get("scans", 0) + 1
        if calls["scans"] == 1 and fault == "live_scan":
            return read_model.LiveScan(read_model.SOURCE_UNAVAILABLE)
        return real_scan(root)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(read_model_inputs, "read_db_inputs", flaky_db)
        patch.setattr(read_model_inputs, "scan_live", flaky_scan)
        first = svc.plugin_list(Principal.OPERATOR)
        assert "unavailable" in first["sources"].values()
        second = svc.plugin_list(Principal.OPERATOR)
    assert set(second["sources"].values()) == {"ok"}
    assert [row["name"] for row in second["plugins"]] == ["rsi"]
    assert svc._plugin_views.builds == 2
    svc.plugin_list(Principal.OPERATOR)
    assert svc._plugin_views.builds == 2


def test_an_unreadable_entry_with_an_io_error_is_not_remembered(ops_env, world):
    from agentic_fx.plugin import loader
    world.deploy("rsi")
    svc = service(ops_env)
    real = loader.discover_one_with_reason
    state = {"fail": True}

    def discover(directory, name):
        if state["fail"]:
            raise OSError("transient")
        return real(directory, name)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(loader, "discover_one_with_reason", discover)
        [row] = _rows_of(svc)
        assert row["entity"]["reason"] == "io_error"
        state["fail"] = False
        [row] = _rows_of(svc)
    assert row["entity"]["state"] == "symlink"


def _lock_down(path):
    previous = path.stat().st_mode & 0o777
    os.chmod(path, 0o000)
    return previous


def test_a_file_that_cannot_be_read_is_shown_and_read_again_once_it_recovers(ops_env, world):
    meta = world.deploy("rsi")
    svc = service(ops_env)
    target = meta.path / "config.yaml"
    previous = _lock_down(target)
    try:
        [row] = _rows_of(svc)
        assert row["entity"]["reason"] == "io_error"
        [row] = _rows_of(svc)
        assert row["entity"]["reason"] == "io_error"
        assert svc._plugin_views.builds == 2
    finally:
        os.chmod(target, previous)
    [row] = _rows_of(svc)
    assert row["entity"]["state"] == "symlink" and row["entity"].get("reason") is None
    assert svc._plugin_views.builds == 3
    _rows_of(svc)
    assert svc._plugin_views.builds == 3


def test_a_plugin_py_that_cannot_be_read_is_read_again_once_its_mode_recovers(ops_env, world):
    meta = world.deploy("rsi")
    svc = service(ops_env)
    target = meta.path / "plugin.py"
    previous = _lock_down(target)
    try:
        [row] = _rows_of(svc)
        assert row["entity"]["state"] == "unreadable"
    finally:
        os.chmod(target, previous)
    [row] = _rows_of(svc)
    assert row["entity"]["state"] == "symlink" and row["entity"].get("reason") is None


def test_an_unreadable_versions_folder_is_not_remembered(ops_env, world):
    world.deploy("rsi")
    world.version_only("other")
    svc = service(ops_env)
    folder = ops_env.plugins_root / ".versions" / "other"
    previous = _lock_down(folder)
    try:
        svc.plugin_list(Principal.OPERATOR)
        svc.plugin_list(Principal.OPERATOR)
        assert svc._plugin_views.builds == 2
    finally:
        os.chmod(folder, previous)
    listing = svc.plugin_list(Principal.OPERATOR)
    assert svc._plugin_views.builds == 3
    assert "other" in [row["name"] for row in listing["plugins"]]
    svc.plugin_list(Principal.OPERATOR)
    assert svc._plugin_views.builds == 3


class _SlowScan:
    """scan_live を、合図があるまで止める差し替え。"""

    def __init__(self, real):
        import threading
        self.real, self.entered, self.release = real, threading.Event(), threading.Event()
        self.calls = 0

    def __call__(self, root):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(10)
        return self.real(root)


def test_a_slow_rebuild_does_not_hold_the_ops_lane(ops_env, world):
    import threading
    from agentic_fx.plugin import read_model_inputs
    world.deploy("rsi")
    svc = service(ops_env)
    slow = _SlowScan(read_model_inputs.scan_live)
    results: list = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(read_model_inputs, "scan_live", slow)
        thread = threading.Thread(
            target=lambda: results.append(svc.plugin_list(Principal.OPERATOR)))
        thread.start()
        try:
            assert slow.entered.wait(10)
            # 再構築の最中でも、ほかの ops 呼び出しは lane を取れる。
            assert svc.approval_list(Principal.OPERATOR)["approvals"] == []
            assert svc.status(Principal.OPERATOR) is not None
        finally:
            slow.release.set()
            thread.join(10)
    assert results and results[0]["plugins"][0]["name"] == "rsi"


def test_concurrent_requests_share_one_rebuild(ops_env, world):
    import threading
    from agentic_fx.plugin import read_model_inputs
    world.deploy("rsi")
    svc = service(ops_env)
    slow = _SlowScan(read_model_inputs.scan_live)
    results: list = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(read_model_inputs, "scan_live", slow)
        threads = [threading.Thread(
            target=lambda: results.append(svc.plugin_list(Principal.OPERATOR)))
            for _ in range(4)]
        for thread in threads:
            thread.start()
        assert slow.entered.wait(10)
        time.sleep(0.3)      # 残りの thread が memo の入口まで来るのを待つ
        slow.release.set()
        for thread in threads:
            thread.join(10)
    assert len(results) == 4
    assert slow.calls == 1 and svc._plugin_views.builds == 1


def test_a_request_waiting_for_the_rebuild_gives_up_at_its_deadline(ops_env, world):
    import threading
    from agentic_fx.plugin import read_model_inputs
    world.deploy("rsi")
    svc = service(ops_env)
    slow = _SlowScan(read_model_inputs.scan_live)
    results: list = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(read_model_inputs, "scan_live", slow)
        thread = threading.Thread(
            target=lambda: results.append(svc.plugin_list(Principal.OPERATOR)))
        thread.start()
        try:
            assert slow.entered.wait(10)
            started = time.monotonic()
            with pytest.raises(OpsError) as caught:
                svc.plugin_list(Principal.OPERATOR, deadline=ops_env.mono() + 0.3)
            assert caught.value.code is ErrorCode.UNAVAILABLE
            assert time.monotonic() - started < 3.0
            with pytest.raises(OpsError) as caught:
                svc.plugin_detail(Principal.OPERATOR, "rsi", deadline=ops_env.mono() + 0.2)
            assert caught.value.code is ErrorCode.UNAVAILABLE
            assert svc.status(Principal.OPERATOR) is not None
            assert svc.approval_list(Principal.OPERATOR)["approvals"] == []
        finally:
            slow.release.set()
            thread.join(10)
    assert results and results[0]["plugins"][0]["name"] == "rsi"
    assert slow.calls == 1


def test_a_waiting_request_stops_when_the_service_stops(ops_env, world):
    import threading
    from agentic_fx.plugin import read_model_inputs
    world.deploy("rsi")
    svc = service(ops_env)
    slow = _SlowScan(read_model_inputs.scan_live)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(read_model_inputs, "scan_live", slow)
        thread = threading.Thread(target=lambda: svc.plugin_list(Principal.OPERATOR))
        thread.start()
        try:
            assert slow.entered.wait(10)
            svc.stop_event.set()
            with pytest.raises(OpsError) as caught:
                svc.plugin_list(Principal.OPERATOR)
            assert caught.value.code is ErrorCode.UNAVAILABLE
        finally:
            slow.release.set()
            thread.join(10)
