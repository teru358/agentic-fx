from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest

from agentic_fx.backtest.dukascopy import Tick
from agentic_fx.backtest import importer
from agentic_fx.backtest.importer import _default_fetch, ticks_to_1m, import_dukascopy
from agentic_fx.store.ohlcv import load_bars
from tests.backtest.conftest import _conn, _bi5, H


# F4: _default_fetch (production fetch path) — 404 -> b"", other HTTP errors
# and connection errors -> raise. httpx.get is monkeypatched so no real HTTP
# request is made.

def test_default_fetch_returns_empty_bytes_on_404(monkeypatch):
    def fake_get(url, timeout=30):
        request = httpx.Request("GET", url)
        return httpx.Response(404, request=request)

    monkeypatch.setattr(importer.httpx, "get", fake_get)
    assert _default_fetch("http://example.com/missing") == b""


def test_default_fetch_raises_http_status_error_on_500():
    def fake_get(url, timeout=30):
        request = httpx.Request("GET", url)
        return httpx.Response(500, request=request)

    with patch.object(importer.httpx, "get", fake_get):
        with pytest.raises(httpx.HTTPStatusError):
            _default_fetch("http://example.com/broken")


def test_default_fetch_raises_connect_error_on_network_failure(monkeypatch):
    def fake_get(url, timeout=30):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(importer.httpx, "get", fake_get)
    with pytest.raises(httpx.ConnectError):
        _default_fetch("http://example.com/unreachable")


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

    # Verify data is readable with correct source (catches source parameter mutations)
    bars = load_bars(conn, "USDJPY", "1m", source="dukascopy")
    assert len(bars) == 1
    assert bars[0].symbol == "USDJPY"
    assert bars[0].interval == "1m"


def test_import_skips_empty_hours(tmp_path):
    conn = _conn(tmp_path)
    r = import_dukascopy(conn, "USDJPY", H, H + timedelta(hours=1),
                         fetch=lambda url: b"")
    assert r.inserted == 0


# F2: Validate start/end parameters (hour boundary, aware UTC)
def test_import_dukascopy_rejects_naive_start(tmp_path):
    conn = _conn(tmp_path)
    naive_start = datetime(2026, 7, 22, 12, 0)  # naive
    with pytest.raises(ValueError, match="start must be timezone-aware"):
        import_dukascopy(conn, "USDJPY", naive_start, naive_start + timedelta(hours=1),
                        fetch=lambda url: b"")


def test_import_dukascopy_rejects_naive_end(tmp_path):
    conn = _conn(tmp_path)
    naive_end = datetime(2026, 7, 22, 13, 0)  # naive
    with pytest.raises(ValueError, match="end must be timezone-aware"):
        import_dukascopy(conn, "USDJPY", H, naive_end, fetch=lambda url: b"")


def test_import_dukascopy_rejects_non_utc_start(tmp_path):
    conn = _conn(tmp_path)
    from datetime import timezone as tz
    non_utc_start = H.replace(tzinfo=tz(timedelta(hours=9)))
    with pytest.raises(ValueError, match="start must be UTC"):
        import_dukascopy(conn, "USDJPY", non_utc_start, H + timedelta(hours=1),
                        fetch=lambda url: b"")


def test_import_dukascopy_rejects_non_hour_boundary_start(tmp_path):
    conn = _conn(tmp_path)
    off_hour = H.replace(minute=30)  # 12:30 instead of 12:00
    with pytest.raises(ValueError, match="must be on hour boundary"):
        import_dukascopy(conn, "USDJPY", off_hour, H + timedelta(hours=1),
                        fetch=lambda url: b"")


def test_import_dukascopy_rejects_non_hour_boundary_end(tmp_path):
    conn = _conn(tmp_path)
    off_end = (H + timedelta(hours=1)).replace(second=30)  # 13:00:30 instead of 13:00:00
    with pytest.raises(ValueError, match="must be on hour boundary"):
        import_dukascopy(conn, "USDJPY", H, off_end, fetch=lambda url: b"")


# F3: Defensive sort in ticks_to_1m (open/close independent of input order)
def test_ticks_to_1m_sorts_by_timestamp():
    """Test that ticks are sorted by timestamp before calculating open/close."""
    # Ticks in reverse chronological order within the same minute
    ticks = [Tick(H.replace(second=40), 148.020, 148.040),
             Tick(H.replace(second=30), 148.010, 148.020),
             Tick(H.replace(second=10), 148.000, 148.010)]
    rows = ticks_to_1m(ticks, "USDJPY")
    assert len(rows) == 1
    sym, iv, ts, o, h, l, c, v, spread = rows[0]
    # After sorting by time: open = 148.005 (first), close = 148.030 (last)
    assert o == 148.005 and c == 148.030
    # High and low are max/min of all mids
    mids = [148.005, 148.015, 148.030]
    assert h == max(mids) and l == min(mids)
