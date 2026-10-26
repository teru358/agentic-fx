"""改善 mission の終端状態を DB に直接作る test 用の部品。"""
from __future__ import annotations

from datetime import datetime, timezone

from agentic_fx import improve_notice_codes as codes
from agentic_fx.store import improve_runs, missions

START = datetime(2026, 10, 25, 7, 7, 14, 852000, tzinfo=timezone.utc)
END = datetime(2026, 10, 25, 7, 11, 36, tzinfo=timezone.utc)


def seed(env, *, notice=codes.NOTICE_UNPROFITABLE, started=START, ended=END,
         trigger="scheduled", tool_calls=10, observation_text=None, approval_id=None,
         report_state="none", report_path=None, loop="improve", terminal=True,
         runner="local", model="qwen"):
    conn = env.conn
    mission_id = missions.start(conn, loop, runner, model, started)
    run_id = improve_runs.start(conn, None, started, mission_id=mission_id, trigger=trigger)
    if terminal:
        missions.finish_improve_mission(
            conn, mission_id=mission_id, run_id=run_id, slot_key=None,
            mission_status="completed", run_result="report" if report_path else None,
            backlog_transition=None, now=ended, notice=notice, tool_calls=tool_calls,
            observation_text=observation_text, approval_id=approval_id,
            report_path=report_path, report_state=report_state)
    return mission_id
