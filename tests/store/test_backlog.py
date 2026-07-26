from datetime import datetime, timezone

from agentic_fx.store import backlog, improve_runs
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def test_backlog_and_run(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    bid = backlog.add(c, "ATR ベースの SL 幅", "user", NOW)
    assert backlog.list_open(c)[0]["idea"].startswith("ATR")
    backlog.set_status(c, bid, "selected", NOW)
    assert backlog.list_open(c) == []
    rid = improve_runs.start(c, bid, NOW)
    improve_runs.finish(c, rid, result="report", now=NOW,
                        report_path="reports/improve-2026-07-26.md")
