"""変更 endpoint の要求を 1 本実行し、指定の点で process ごと落ちる子 (os._exit)。

argv: root endpoint point
- endpoint: approve | retry | reject | killswitch_reset | killswitch_reconcile
  | backlog_reject | backlog_reopen | job_cancel
- point:
  before_accept   対象要求の accepted を書く前
  after_accept    対象要求の accepted を commit した直後 (副作用の前)
  during:<name>   副作用の途中 (endpoint ごとの点。下の _DURING を参照)
  after_effect    副作用の後、対象要求の終端行を書く前
  after_terminal  対象要求の終端行を commit した後
  journal_<phase> / after_switch_live  approve・retry の切替 journal の各 phase の直後
crash 点は「対象 endpoint の要求」にだけ掛ける (準備で送る別要求には掛けない)。
落ちずに最後まで来たら rc=0。
"""
from __future__ import annotations

import json
import os
import resource
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

from agentic_fx.activity import ActivityLog  # noqa: E402
from agentic_fx.config import load_settings  # noqa: E402
from agentic_fx.ops.audit import AuditStore  # noqa: E402
from agentic_fx.ops.contracts import Principal  # noqa: E402
from agentic_fx.ops.service import ENDPOINTS, OpsService  # noqa: E402
from agentic_fx.plugin import switch  # noqa: E402
from agentic_fx.store import backlog, db  # noqa: E402
from agentic_fx.store.state import StateStore  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
CRASH_RC = 17


def _crash() -> None:
    os._exit(CRASH_RC)


class _Target:
    def __init__(self, route: str) -> None:
        self.route = route
        self.audit_id: int | None = None
        self.accepted_by_route: dict[str, list[int]] = {}


def _install_audit_hooks(target: _Target, point: str) -> None:
    original_accept = AuditStore.accept
    original_idem = AuditStore.accept_idempotent
    original_terminal = AuditStore.terminal

    def accept(self, **kwargs):
        route = kwargs.get("endpoint")
        committed = kwargs.get("commit", True)
        if route == target.route and committed and point == "before_accept":
            _crash()
        result = original_accept(self, **kwargs)
        if committed:
            target.accepted_by_route.setdefault(route, []).append(result.audit_id)
            if route == target.route:
                target.audit_id = result.audit_id
                if point == "after_accept":
                    _crash()
        return result

    def accept_idempotent(self, **kwargs):
        if kwargs.get("endpoint") == target.route and point == "before_accept":
            _crash()
        result = original_idem(self, **kwargs)
        if kwargs.get("endpoint") == target.route and hasattr(result, "audit_id"):
            target.audit_id = result.audit_id
            if point == "after_accept":
                _crash()
        return result

    def terminal(self, audit_id, **kwargs):
        if point == "after_effect" and audit_id == target.audit_id:
            _crash()
        if point == "during:cancelled_job_terminal" \
                and audit_id in target.accepted_by_route.get(ENDPOINTS["reject"].route, []):
            _crash()
        return original_terminal(self, audit_id, **kwargs)

    AuditStore.accept = accept
    AuditStore.accept_idempotent = accept_idempotent
    AuditStore.terminal = terminal

    original_project = OpsService._project

    def project(self, audit_id, *args, **kwargs):
        result = original_project(self, audit_id, *args, **kwargs)
        if point == "after_terminal" and audit_id == target.audit_id:
            _crash()
        return result

    OpsService._project = project


def _install_effect_hooks(point: str) -> None:
    if point == "during:reject_decided_before_staging_drop":
        # 決定 (rejected) は commit 済み、staging 候補の掃除の前
        switch._drop_staging_candidate = lambda *a, **k: _crash()
    elif point == "during:backlog_updated_not_committed":
        # UPDATE は実行済み、commit の前
        original = backlog.transition

        class _CrashOnCommit:
            # 行を変えた UPDATE の commit でだけ落ちる (遷移元の合わない試行は素通し)。
            def __init__(self, conn):
                self._conn = conn
                self._changed = False

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def execute(self, *args):
                cur = self._conn.execute(*args)
                self._changed = cur.rowcount == 1
                return cur

            def commit(self):
                if self._changed:
                    _crash()
                self._conn.commit()

        backlog.transition = lambda conn, *a, **k: original(_CrashOnCommit(conn), *a, **k)
    elif point in ("during:reset_marker_written", "during:reconcile_before_replace"):
        StateStore.save = lambda *a, **k: _crash()
    elif point in ("during:reset_replaced", "during:reconcile_replaced"):
        StateStore._remove_marker = lambda *a, **k: _crash()
    elif point == "during:reset_marker_removed":
        original_remove = StateStore._remove_marker

        def remove_then_crash(self, *a, **k):
            original_remove(self, *a, **k)
            _crash()
        StateStore._remove_marker = remove_then_crash
    elif point == "journal_preparing":
        original_begin = switch.begin_switch_journal

        def begin(*args, **kwargs):
            result = original_begin(*args, **kwargs)
            (args[0] if args else kwargs["conn"]).commit()
            _crash()
            return result
        switch.begin_switch_journal = begin
    elif point.startswith("journal_"):
        wanted = point.removeprefix("journal_")
        original_advance = switch.advance_switch_journal

        def advance(conn_, op_id, *, phase: str, **kwargs):
            result = original_advance(conn_, op_id, phase=phase, **kwargs)
            if phase == wanted:
                _crash()
            return result
        switch.advance_switch_journal = advance
    elif point == "after_switch_live":
        original_switch = switch.switch_live

        def switch_live(*args, **kwargs):
            original_switch(*args, **kwargs)
            _crash()
        switch.switch_live = switch_live


def main(root: Path, endpoint: str, point: str) -> None:
    target = _Target(ENDPOINTS[endpoint].route)
    _install_audit_hooks(target, point)
    _install_effect_hooks(point)
    conn = db.connect(root / "data" / "agentic.db")
    service = OpsService(
        conn, wall_clock=lambda: datetime.now(timezone.utc),
        policy_path=root / "policy" / "directives.md",
        state_store=StateStore(root / "data" / "app_state.json"),
        plugins_root=root / "plugins",
        settings=load_settings(REPO / "config" / "settings.yaml.example"),
        activity_log=ActivityLog(root / "logs" / "activity.log"))
    args = json.loads((root / "args.json").read_text())
    ap, op = Principal.APPROVER, Principal.OPERATOR
    if endpoint in ("approve", "retry"):
        call = service.approve if endpoint == "approve" else service.retry
        reply = call(ap, args["approval_id"], args["digest"])
        service.wait_for_job(reply["job_id"], timeout=30)
    elif endpoint == "reject":
        reply = service.reject(ap, args["approval_id"], "not now")
        service.wait_for_job(reply["job_id"], timeout=30)
    elif endpoint == "killswitch_reset":
        service.reset_kill_switch(ap, args["generation"])
    elif endpoint == "killswitch_reconcile":
        service.reconcile_kill_switch(ap)
    elif endpoint in ("backlog_reject", "backlog_reopen"):
        service.transition_backlog(op, args["backlog_id"], endpoint.split("_", 1)[1])
    elif endpoint == "job_cancel":
        # 決定 worker を起こさず、取り消す対象の job を queued のまま置く。
        service.start_decision_worker = lambda: None
        queued = service.reject(ap, args["approval_id"], "queued")
        service.cancel_job(ap, queued["job_id"])
    else:
        raise SystemExit(f"unknown endpoint {endpoint}")
    # job の終端処理は別 thread のことがあるので、終わるまで待ってから抜ける。
    end = time.monotonic() + 10
    for thread in list(service._operation_threads):
        thread.join(max(0.0, end - time.monotonic()))
    if service._decision_thread is not None:
        service.shutdown(join_timeout=5)
    os._exit(0)


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
