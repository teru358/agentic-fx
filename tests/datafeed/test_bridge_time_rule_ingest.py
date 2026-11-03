from datetime import datetime, timezone
from types import SimpleNamespace

from agentic_fx.datafeed.ingest import Ingest
from agentic_fx.datafeed.sources import BridgeTimeUnverified
from agentic_fx.store.db import connect, init_db


def _settings():
    return SimpleNamespace(
        pairs=["USDJPY"],
        datafeed=SimpleNamespace(primary="mt5", intervals=["1m"], primary_intervals=["1m"],
                                 ingest_budget_sec=10, closed_bar_grace_sec=0,
                                 mt5=SimpleNamespace(bridge_url="http://bridge")),
    )


def test_unverified_bridge_time_leaves_the_key_out_of_the_tick_report(tmp_path):
    conn = connect(tmp_path / "bars.db")
    init_db(conn)
    ingest = Ingest(_settings(), fetch=lambda *args, **kwargs: (_ for _ in ()).throw(
        BridgeTimeUnverified("MT5 bridge time rule is unverified")))

    _count, report = ingest.prepare(datetime(2026, 11, 8, 22, 1, 30, tzinfo=timezone.utc), conn)

    key = ("USDJPY", "1m")
    assert key not in report.attempted
    assert key not in report.succeeded
    assert key not in dict(report.failed)
    assert key not in ingest.last_errors
    conn.close()
