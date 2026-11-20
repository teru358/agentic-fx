"""週次の確定の配線: build_app が scheduler へ登録し、実際の DB・plugins・report 保存先で 1 週を確定する。"""
from __future__ import annotations

import stat
from datetime import datetime, timezone

from agentic_fx.core.contracts import FixedClock
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import _scheduler_tick_once, build_app
from agentic_fx.store import alert_state
from agentic_fx.store import weekly_review as store
from tests.store.test_rag import FakeEmbedding
from tests.test_service_app import _init

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)       # 水曜
LAST_WEEK = "2026-07-17"                                      # 直前の週の印 (金曜)


def _app(tmp_path):
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                    embedding_fn=FakeEmbedding())
    app.scheduler.on_improve_tick = None
    return app


def test_build_app_registers_the_weekly_poll_on_the_scheduler(tmp_path):
    app = _app(tmp_path)
    try:
        assert app.weekly_review_tick is not None
        assert app.scheduler._weekly_review == app.weekly_review_tick.poll
    finally:
        app.close()


def test_a_scheduler_tick_finalizes_the_latest_due_week_and_writes_its_report(tmp_path):
    app = _app(tmp_path)
    try:
        _scheduler_tick_once(app)
        assert app.weekly_review_tick.join(30)
        week = store.get_week(app.conn_core, LAST_WEEK)
        assert week is not None and week["status"] == "completed"
        assert week["evaluation"] == "normal"
        saved = alert_state.get(app.conn_core, alert_state.WEEKLY_REVIEW_START_KEY)
        assert LAST_WEEK in saved
        report = tmp_path / "data" / "improve_reports" / "weekly-review" / (
            f"weekly-review-{LAST_WEEK}.md")
        assert report.is_file() and stat.S_IMODE(report.stat().st_mode) == 0o600
        assert LAST_WEEK in report.read_text()
    finally:
        app.close()


def test_the_attempt_is_recorded_as_done(tmp_path):
    import json
    app = _app(tmp_path)
    try:
        _scheduler_tick_once(app)
        assert app.weekly_review_tick.join(30)
        key = alert_state.weekly_review_attempt_key(LAST_WEEK, NOW.date().isoformat())
        record = json.loads(alert_state.get(app.conn_core, key))
        assert record["count"] == 1 and record["attempts"][0]["state"] == "done"
    finally:
        app.close()
