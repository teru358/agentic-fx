from datetime import datetime, timedelta, timezone

from agentic_fx.backtest.dukascopy import Tick
from agentic_fx.backtest.importer import ticks_to_1m, import_dukascopy
from tests.backtest.conftest import _conn, _bi5, H


def test_ticks_to_1m_mid_and_mean_spread():
    ticks = [Tick(H, 148.000, 148.010),
             Tick(H.replace(second=30), 148.020, 148.040),
             Tick(H.replace(minute=1), 149.000, 149.020)]
    rows = ticks_to_1m(ticks, "USDJPY")
    assert len(rows) == 2
    sym, iv, ts, o, h, l, c, v, spread = rows[0]
    assert (sym, iv, ts) == ("USDJPY", "1m", H.isoformat())
    assert o == 148.005 and c == 148.030 and v == 2
    assert h == 148.030 and l == 148.005  # max and min of mids
    assert abs(spread - 0.015) < 1e-9  # (0.010+0.020)/2


def test_import_dukascopy_uses_injected_fetch_and_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    payload = _bi5([(0, 148_010, 148_000, 1, 1)])
    calls = []

    def fetch(url):
        calls.append(url); return payload

    r1 = import_dukascopy(conn, "USDJPY", H, H + timedelta(hours=1), fetch=fetch)
    r2 = import_dukascopy(conn, "USDJPY", H, H + timedelta(hours=1), fetch=fetch)
    assert r1.inserted == 1 and r2.unchanged == 1 and r2.conflicted == 0
    assert all("datafeed.dukascopy.com" in u for u in calls)


def test_import_skips_empty_hours(tmp_path):
    conn = _conn(tmp_path)
    r = import_dukascopy(conn, "USDJPY", H, H + timedelta(hours=1),
                         fetch=lambda url: b"")
    assert r.inserted == 0
