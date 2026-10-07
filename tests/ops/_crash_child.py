"""変更要求の途中で process ごと落ちる子。親は再起動相当の回復を検査する。

argv: root endpoint point
- point = before_accept | after_accept | after_effect | after_terminal | sigkill_after_accept
  | journal_<phase> (approve の切替 journal がその phase に進んだ直後)
副作用の回数は root/effects/<endpoint> に 1 行ずつ追記する (親が数える)。
"""
from __future__ import annotations

import concurrent.futures
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
from agentic_fx.ops.service import OpsService  # noqa: E402
from agentic_fx.plugin import switch  # noqa: E402
from agentic_fx.store import db  # noqa: E402
from agentic_fx.store.state import StateStore  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def _effect(root: Path, name: str) -> None:
    effects = root / "effects"
    effects.mkdir(exist_ok=True)
    with (effects / name).open("a") as file:
        file.write("1\n")
        file.flush()
        os.fsync(file.fileno())


def _crash() -> None:
    os._exit(17)


def _wrap_after(cls, name, hook):
    original = getattr(cls, name)

    def wrapped(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        # accept_idempotent は内部で accept(commit=False) を呼ぶ。commit 前は対象外。
        if kwargs.get("commit", True):
            hook()
        return result
    setattr(cls, name, wrapped)


def main(root: Path, endpoint: str, point: str) -> None:
    conn = db.connect(root / "data" / "agentic.db")
    state = StateStore(root / "data" / "app_state.json")

    def ask_submitter(question):
        _effect(root, "ask")
        future = concurrent.futures.Future()
        future.set_result("answer")
        return future

    class Improve:
        def submit_manual(self, *, on_prepared=None):
            _effect(root, "improve")
            return 5

    def data_resume(acknowledge):
        _effect(root, "data_resume")
        return {"gap": 0}

    service = OpsService(
        conn, wall_clock=lambda: datetime.now(timezone.utc),
        policy_path=root / "policy" / "directives.md", state_store=state,
        plugins_root=root / "plugins",
        settings=load_settings(REPO / "config" / "settings.yaml.example"),
        activity_log=ActivityLog(root / "logs" / "activity.log"),
        ask_submitter=ask_submitter, improve_supervisor=Improve(), data_resume=data_resume)

    if point == "before_accept":
        AuditStore.accept = lambda self, **kw: _crash()
        AuditStore.accept_idempotent = lambda self, **kw: _crash()
    elif point == "after_accept":
        _wrap_after(AuditStore, "accept", _crash)
        _wrap_after(AuditStore, "accept_idempotent", _crash)
    elif point == "sigkill_after_accept":
        def wait_for_kill():
            (root / "child-ready").write_text("ready")
            time.sleep(60)
        _wrap_after(AuditStore, "accept", wait_for_kill)
        _wrap_after(AuditStore, "accept_idempotent", wait_for_kill)
    elif point == "after_effect":
        AuditStore.terminal = lambda self, *a, **kw: _crash()
    elif point == "after_terminal":
        _wrap_after(OpsService, "_project", _crash)
    elif point == "after_switch_live":
        _original_switch = switch.switch_live

        def switch_live(*args, **kwargs):
            _original_switch(*args, **kwargs)
            _crash()
        switch.switch_live = switch_live
    elif point == "journal_preparing":
        _original_begin = switch.begin_switch_journal

        def begin(*args, **kwargs):
            result = _original_begin(*args, **kwargs)
            conn_ = args[0] if args else kwargs["conn"]
            conn_.commit()
            _crash()
            return result
        switch.begin_switch_journal = begin
    elif point.startswith("journal_"):
        phase = point.removeprefix("journal_")
        original = switch.advance_switch_journal

        def advance(conn_, op_id, *, phase: str, **kwargs):
            result = original(conn_, op_id, phase=phase, **kwargs)
            if phase == wanted:
                _crash()
            return result
        wanted = phase
        switch.advance_switch_journal = advance
    else:
        raise SystemExit(f"unknown point {point}")

    args = json.loads((root / "args.json").read_text())
    op = Principal.OPERATOR
    ap = Principal.APPROVER
    if endpoint == "policy":
        service.add_policy(op, "key-1", "protect capital")
    elif endpoint == "backlog_add":
        service.add_backlog(op, "key-1", "idea")
    elif endpoint == "reflection_retry":
        service.retry_reflection(op, "key-1", args["order_id"], expected_attempts=1,
                                 expected_last_attempt_at=args["attempt_at"])
    elif endpoint == "ask":
        reply = service.ask(op, "key-1", "question")
        service.wait_for_job(reply["job_id"], timeout=10)
    elif endpoint == "improve":
        reply = service.improve(op, "key-1")
        service.wait_for_job(reply["job_id"], timeout=10)
    elif endpoint == "backlog_note":
        service.transition_backlog(op, args["backlog_id"], "note")
    elif endpoint == "killswitch_reset":
        service.reset_kill_switch(ap, args["generation"])
    elif endpoint == "data_resume":
        service.resume_data(ap, acknowledge=True)
    elif endpoint == "approve":
        reply = service.approve(ap, args["approval_id"], args["digest"])
        service.wait_for_job(reply["job_id"], timeout=20)
    else:
        raise SystemExit(f"unknown endpoint {endpoint}")
    # job の終端処理は別 thread なので、終わるまで待ってから抜ける。
    for thread in list(service._operation_threads):
        thread.join(10)
    if service._decision_thread is not None:
        service.shutdown(join_timeout=5)
    # ここまで来たら crash 点を通らなかった。
    os._exit(0)


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
