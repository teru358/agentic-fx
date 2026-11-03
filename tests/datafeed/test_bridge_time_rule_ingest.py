from datetime import datetime, timedelta, timezone
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


def _unverified_ingest():
    return Ingest(_settings(), fetch=lambda *args, **kwargs: (_ for _ in ()).throw(
        BridgeTimeUnverified("MT5 bridge time rule is unverified")))


def test_unverified_for_five_minutes_while_open_warns_once_and_records_the_reason(
        tmp_path, caplog):
    conn = connect(tmp_path / "bars.db")
    init_db(conn)
    ingest = _unverified_ingest()
    key = ("USDJPY", "1m")
    t0 = datetime(2026, 11, 8, 22, 1, 30, tzinfo=timezone.utc)

    def tick(seconds):
        ingest.next_probe_at.clear()
        return ingest.prepare(t0 + timedelta(seconds=seconds), conn)

    tick(0)
    tick(299)
    assert key not in ingest.last_errors
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    with caplog.at_level("WARNING"):
        _count, report = tick(300)
        tick(360)
        tick(420)
    assert ingest.last_errors[key] == "bridge time rule unverified while market open"
    assert key not in dict(report.failed)
    assert key not in report.attempted
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    conn.close()


def test_unverified_episode_restarts_after_a_successful_fetch(tmp_path, caplog):
    conn = connect(tmp_path / "bars.db")
    init_db(conn)
    mode = {"unverified": True}

    def fetch(*args, **kwargs):
        if mode["unverified"]:
            raise BridgeTimeUnverified()
        return []

    ingest = Ingest(_settings(), fetch=fetch)
    key = ("USDJPY", "1m")
    t0 = datetime(2026, 11, 8, 22, 1, 30, tzinfo=timezone.utc)
    ingest.prepare(t0, conn)
    mode["unverified"] = False
    ingest.next_probe_at.clear()
    ingest.prepare(t0 + timedelta(seconds=200), conn)
    mode["unverified"] = True
    ingest.next_probe_at.clear()
    ingest.prepare(t0 + timedelta(seconds=400), conn)
    ingest.next_probe_at.clear()
    ingest.prepare(t0 + timedelta(seconds=600), conn)
    assert key not in ingest.last_errors
    conn.close()
