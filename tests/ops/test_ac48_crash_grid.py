"""AC-48: 変更 endpoint × crash 点の格子のうち、他のファイルで埋まっていない組。

子 process を対象要求の各点で os._exit させ (unwind で消える痕跡を残すため)、親で
再起動相当の順序 (切替 journal の reconcile → ops の回復 → listener) を踏む。

- 未終端の accepted には listener を開く前に 1 件だけ終端が追記され、副作用は再実行されない
- journal が決めた決定はその結果、それ以外は outcome_unknown (成功らしく見えても推測しない)
- 現状態は監査ではなく approval GET / status (reconcile_required) / generation CAS で確かめる

他の点の組は test_ac48_50_crash_recovery.py (policy・backlog add / note・reflection retry・
ask・improve・killswitch reset・data resume と approve の journal phase) にある。
"""
from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.ops import ErrorCode, OpsError
from agentic_fx.ops.contracts import Principal
from agentic_fx.plugin import switch
from agentic_fx.store import backlog

from ._api import make_api
from .conftest import APPROVER, OPERATOR, digest_of
from .test_ac51_killswitch_api import _crash_reset

CHILD = Path(__file__).with_name("_crash_grid_child.py")
COMMON = ["before_accept", "after_accept", "after_effect", "after_terminal"]
JOURNAL = ["journal_preparing", "journal_versioned", "journal_recorded", "journal_switched",
           "after_switch_live"]
DURING = {
    "approve": JOURNAL,
    "retry": JOURNAL,
    "reject": ["during:reject_decided_before_staging_drop"],
    "killswitch_reset": ["during:reset_marker_written", "during:reset_replaced",
                         "during:reset_marker_removed"],
    "killswitch_reconcile": ["during:reconcile_before_replace", "during:reconcile_replaced"],
    "backlog_reject": ["during:backlog_updated_not_committed"],
    "backlog_reopen": ["during:backlog_updated_not_committed"],
    "job_cancel": ["during:cancelled_job_terminal"],
}
GRID = [(endpoint, point) for endpoint, during in DURING.items()
        for point in COMMON + during]
_ROUTES = {
    "approve": "POST /v1/approvals/{id}/approve", "retry": "POST /v1/approvals/{id}/retry",
    "reject": "POST /v1/approvals/{id}/reject", "killswitch_reset": "POST /v1/killswitch/reset",
    "killswitch_reconcile": "POST /v1/killswitch/reconcile",
    "backlog_reject": "POST /v1/backlog/{id}/reject",
    "backlog_reopen": "POST /v1/backlog/{id}/reopen",
    "job_cancel": "POST /v1/jobs/{id}/cancel",
}


def _prepare(env, endpoint: str) -> dict:
    args: dict = {}
    if endpoint in ("approve", "retry", "reject", "job_cancel"):
        approval_id = env.make_plugin_approval()
        args = {"approval_id": approval_id, "digest": digest_of(env, approval_id)}
    elif endpoint == "killswitch_reset":
        args["generation"] = env.state.update(kill_switch_latched=True).kill_switch_generation
    elif endpoint == "killswitch_reconcile":
        # 解除が state replace の後・marker 削除の前で止まった状態 (ファイル上は解除済み)
        generation = env.state.update(kill_switch_latched=True).kill_switch_generation
        _crash_reset(env, generation, "after_replace")
        assert env.state.reconcile_marker() is not None
    elif endpoint in ("backlog_reject", "backlog_reopen"):
        backlog_id = backlog.add(env.conn, "seed", "user", env.wall())
        if endpoint == "backlog_reopen":
            env.conn.execute("UPDATE improvement_backlog SET status='rejected' WHERE id=?",
                             (backlog_id,))
            env.conn.commit()
        args["backlog_id"] = backlog_id
    (env.root / "args.json").write_text(json.dumps(args))
    return args


def _run_child(env, endpoint: str, point: str) -> None:
    result = subprocess.run([sys.executable, str(CHILD), str(env.root), endpoint, point],
                            capture_output=True, text=True, timeout=120,
                            stdin=subprocess.DEVNULL,
                            preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE,
                                                                  (0, 0)))
    assert result.returncode == 17, (
        f"child did not crash at {point}: rc={result.returncode}\n{result.stderr[-3000:]}")


def _snapshot(env, endpoint: str, args: dict) -> tuple:
    """業務上の現状態 (監査ではなく正の表・ファイル)。"""
    if endpoint in ("approve", "retry", "reject", "job_cancel"):
        row = env.approval(args["approval_id"])
        return (row["status"], row["decided_by"],
                os.path.lexists(env.plugins_root / "sma"))
    if endpoint.startswith("killswitch"):
        raw = env.state.file_value()
        return (raw.kill_switch_latched, raw.kill_switch_generation,
                env.state.reconcile_marker() is not None)
    status = env.conn.execute("SELECT status FROM improvement_backlog WHERE id=?",
                              (args["backlog_id"],)).fetchone()["status"]
    return (status,)


def _expected_effect(endpoint: str, point: str, generation: int) -> tuple:
    early = point in ("before_accept", "after_accept")
    if endpoint == "job_cancel":
        return ("pending", None, False)  # 取り消した job は一度も走らない
    if endpoint == "reject":
        return ("pending", None, False) if early else ("rejected", "api:approver", False)
    if endpoint in ("approve", "retry"):
        if early or point.startswith("journal_"):
            return ("pending", None, False)
        if point == "after_switch_live":
            return ("pending", None, True)
        return ("approved", "api:approver", True)
    if endpoint.startswith("backlog"):
        before, after = (("open", "rejected") if endpoint == "backlog_reject"
                         else ("rejected", "open"))
        return (before,) if early or point.startswith("during") else (after,)
    if endpoint == "killswitch_reset":
        return {"before_accept": (True, generation, False),
                "after_accept": (True, generation, False),
                "during:reset_marker_written": (True, generation, True),
                "during:reset_replaced": (False, generation, True),
                "during:reset_marker_removed": (False, generation, False)}.get(
                    point, (False, generation, False))
    # killswitch_reconcile: 準備でファイル上は解除済み + marker
    return {"during:reconcile_replaced": (True, generation + 1, True)}.get(
        point, (False, generation, True) if early or point.startswith("during")
        else (True, generation + 1, False))


def _target_rows(env, endpoint: str) -> list:
    return [r for r in env.ops_rows() if r["endpoint"] == _ROUTES[endpoint]]


def _terminal_of(env, audit_id: int):
    rows = [r for r in env.ops_rows() if r["target_ref"] == f"audit:{audit_id}"]
    assert len(rows) <= 1
    return rows[0] if rows else None


def _reconcile_journals(env) -> None:
    switch.reconcile_switch_journals(env.conn, plugins_root=env.plugins_root,
                                     now=datetime.now(timezone.utc), settings=env.settings)


def _expected_terminal(endpoint: str, point: str) -> tuple[str, str] | None:
    if point == "before_accept":
        return None
    if point == "after_terminal":
        return ("succeeded", "ok")
    if endpoint in ("approve", "retry") and point == "after_switch_live":
        return ("succeeded", "ok")  # journal reconcile が system_reconcile で完遂した
    if endpoint in ("approve", "retry") and point == "journal_switched":
        return ("failed", "invalid_state")  # journal reconcile が巻き戻した
    return ("outcome_unknown", "outcome_unknown")


@pytest.mark.parametrize("endpoint,point", GRID)
def test_ac48_crash_point_is_recovered_once_and_never_replayed(ops_env, endpoint, point):
    args = _prepare(ops_env, endpoint)
    generation = ops_env.state.file_value().kill_switch_generation
    _run_child(ops_env, endpoint, point)
    assert _snapshot(ops_env, endpoint, args) == _expected_effect(endpoint, point, generation)

    # 再起動の順序: 切替 journal の reconcile → ops の回復 → listener
    _reconcile_journals(ops_env)
    reconciled = _snapshot(ops_env, endpoint, args)
    api = make_api(ops_env, start=False)
    try:
        recovered = api.service.recover_after_journal()
        assert api.service.recover_after_journal() == []
        api.server.start()

        # 全ての accepted に終端がちょうど 1 件ずつある (listener を開く前に揃っている)
        accepted = [r for r in ops_env.ops_rows() if r["phase"] == "accepted"]
        assert all(_terminal_of(ops_env, r["id"]) is not None for r in accepted)
        target = [r for r in _target_rows(ops_env, endpoint) if r["phase"] == "accepted"]
        expected = _expected_terminal(endpoint, point)
        if expected is None:
            assert target == []
        else:
            assert len(target) == 1
            terminal = _terminal_of(ops_env, target[0]["id"])
            assert (terminal["phase"], terminal["result_code"]) == expected
            assert (target[0]["id"] in recovered) == (point != "after_terminal")
        # 回復は副作用を再実行しない
        assert _snapshot(ops_env, endpoint, args) == reconciled
        _check_current_state(api, ops_env, endpoint, args)
    finally:
        for client in api.clients:
            client.close()
        api.server.stop(5.0)


def _check_current_state(api, env, endpoint: str, args: dict) -> None:
    """監査に頼らず、GET と CAS で現状態を確かめられる。"""
    approver = api.client(Principal.APPROVER)
    if endpoint in ("approve", "retry", "reject", "job_cancel"):
        detail = approver.get(f"/v1/approvals/{args['approval_id']}").json()["data"]
        row = env.approval(args["approval_id"])
        assert (detail["status"], detail["decided_by"]) == (row["status"], row["decided_by"])
        if row["status"] != "pending":
            # 決定済みの再決定は既存 CAS が 409 で拒否する (二重決定にならない)
            again = approver.post(f"/v1/approvals/{args['approval_id']}/reject", json={})
            assert again.status_code == 409
            assert again.json()["error"]["code"] == "already_decided"
        return
    if endpoint.startswith("killswitch"):
        kill = approver.get("/v1/status").json()["data"]["kill_switch"]
        marker = env.state.reconcile_marker() is not None
        assert kill["reconcile_required"] is marker
        if marker:
            same = approver.post("/v1/killswitch/reset",
                                 json={"expected_generation": kill["generation"]})
            assert same.json()["error"]["code"] == "invalid_state"
            assert approver.post("/v1/killswitch/reconcile", json={}).status_code == 200
            kill = approver.get("/v1/status").json()["data"]["kill_switch"]
            assert kill["reconcile_required"] is False and kill["latched"] is True
        if kill["latched"]:
            stale = approver.post("/v1/killswitch/reset",
                                  json={"expected_generation": kill["generation"] - 1})
            assert stale.json()["error"]["code"] == "generation_mismatch"
            released = approver.post("/v1/killswitch/reset",
                                     json={"expected_generation": kill["generation"]})
            assert released.status_code == 200
        else:
            assert approver.post("/v1/killswitch/reconcile",
                                 json={}).json()["error"]["code"] == "invalid_state"


def test_ac48_retry_after_an_interrupted_approve_follows_the_same_rules(ops_env):
    """approve が journal preparing で落ち、回復後の retry が recorded で落ちる連鎖。"""
    args = _prepare(ops_env, "approve")
    _run_child(ops_env, "approve", "journal_preparing")
    _reconcile_journals(ops_env)
    service = ops_env.service()
    service.recover_after_journal()
    _run_child(ops_env, "retry", "journal_recorded")
    _reconcile_journals(ops_env)
    before = _snapshot(ops_env, "retry", args)
    service.recover_after_journal()
    retry_rows = [r for r in _target_rows(ops_env, "retry") if r["phase"] == "accepted"]
    assert len(retry_rows) == 1
    terminal = _terminal_of(ops_env, retry_rows[0]["id"])
    assert (terminal["phase"], terminal["result_code"]) == ("outcome_unknown",
                                                            "outcome_unknown")
    assert _snapshot(ops_env, "retry", args) == before
    assert ops_env.approval(args["approval_id"])["status"] == "pending"
    # 明示の retry は改めて受理され、決定は 1 回だけ成立する
    reply = service.retry(APPROVER, args["approval_id"], args["digest"])
    job = service.wait_for_job(reply["job_id"], timeout=30)
    assert job["state"] == "done", job
    assert ops_env.approval(args["approval_id"])["status"] == "approved"
    with pytest.raises(OpsError) as raised:
        service.retry(APPROVER, args["approval_id"], args["digest"])
    assert raised.value.code is ErrorCode.ALREADY_DECIDED
