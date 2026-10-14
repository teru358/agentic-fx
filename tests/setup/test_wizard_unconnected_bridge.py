"""価格源に bridge を選んで未接続のまま完了した root が、実際の取り込みでどう振る舞うか。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed import sources
from agentic_fx.datafeed.ingest import Ingest
from agentic_fx.datafeed.outage import OutageStateMachine
from agentic_fx.setup.wizard import run_wizard
from agentic_fx.store.db import connect

from tests.setup.wizard_support import ScriptedWizardIO, fresh_responder, make_deps, make_root

UTC = timezone.utc
TUESDAY = datetime(2026, 10, 6, 10, 0, 4, tzinfo=UTC)


def latest_closed_bar(pair, interval, end):
    width = timedelta(minutes=sources.INTERVAL_MIN[interval])
    boundary = datetime.fromtimestamp((end - width - timedelta(seconds=30)).timestamp() // (width.total_seconds())
                                      * width.total_seconds(), UTC)
    return Bar(pair, interval, boundary, 1, 1, 1, 1, 1)


def test_a_root_finished_without_a_bridge_degrades_then_recovers_when_the_bridge_answers(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("価格源", "2")))
    assert run_wizard(root, io, deps) == 0, io.text
    settings = load_settings(root / "config" / "settings.yaml")
    assert settings.datafeed.primary == "mt5"

    bridge_up = {"value": False}

    def fetch(pair, interval, start, end, *, timeout):
        if not bridge_up["value"]:
            raise httpx.ConnectError("refused")
        return [latest_closed_bar(pair, interval, end)]

    ingest = Ingest(settings, fetch=fetch)
    conn = connect(root / "data" / "agentic.db")
    hard_keys = frozenset((pair, req.interval) for req in ingest.registry.entries if req.hard
                          for pair in req.pairs)
    outage = OutageStateMachine(
        conn, hard_keys=hard_keys,
        interval_widths={interval: timedelta(minutes=sources.INTERVAL_MIN[interval]) for _, interval in hard_keys},
        grace=timedelta(seconds=settings.datafeed.closed_bar_grace_sec),
        storage_source=ingest.storage_source, activity=ActivityLog(tmp_path / "activity.log"),
        ready_confirm_ticks=settings.datafeed.outage.ready_confirm_ticks,
        auto_resume_when_flat=settings.datafeed.outage.auto_resume_when_flat,
        flat_stall_max_sec=settings.datafeed.outage.flat_stall_max_sec)

    now = TUESDAY
    _, report = ingest.prepare(now, conn)
    ingest.commit(conn)
    assert outage.observe(now, report) == "degraded"

    bridge_up["value"] = True
    states = []
    for minute in range(1, 9):
        now = TUESDAY + timedelta(minutes=minute)
        _, report = ingest.prepare(now, conn)
        ingest.commit(conn)
        states.append(outage.observe(now, report))
    assert states[-1] == "ready"
    assert states.index("ready") >= settings.datafeed.outage.ready_confirm_ticks - 1
    assert "ready" not in states[: settings.datafeed.outage.ready_confirm_ticks - 1]
    conn.close()
