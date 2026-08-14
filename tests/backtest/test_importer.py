from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest

from agentic_fx.backtest.dukascopy import Tick
from agentic_fx.backtest import importer
from agentic_fx.backtest.importer import _default_fetch, ticks_to_1m, import_dukascopy
from agentic_fx.store.ohlcv import load_history_bars
from tests.backtest.factories import _conn, _bi5, H


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
    bars = load_history_bars(conn, "USDJPY", "1m", source="dukascopy")
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


# ---- 応急封鎖 (2026-08-12): 外向きリクエストの抑制 -------------------------
# 設計書は Dukascopy を「長期 1m の一次ソース (無料公開、10 年超)」と位置づけて
# いるが、**外向きリクエストの予算という制約が設計に存在しなかった**。
# import_dukascopy は 1 時間 1 リクエストを間隔なしで連射するため、10 年分の
# 取得は約 87,600 リクエストになる。実際にこの開発機は Dukascopy に遮断された。
# **リポジトリは公開済みで、これは他人に損害を与える不具合**。
# 本設計 (第三者向け出口の集約) が入るまでの応急封鎖。

def test_import_dukascopy_sleeps_between_requests(tmp_path):
    """連続リクエストの間に最小間隔を挟む。**これが無いと連射になる。**"""
    conn = _conn(tmp_path)
    slept: list[float] = []

    r = import_dukascopy(conn, "USDJPY", H, H + timedelta(hours=3),
                         fetch=lambda url: b"",
                         sleep=slept.append)

    assert r.inserted == 0
    # 3 リクエストの「間」は 2 回 (先頭の前には入れない)
    assert len(slept) == 2
    assert all(s >= 1.0 for s in slept), slept


def test_import_dukascopy_aborts_on_429_without_retrying(tmp_path):
    """429 を受けたら**即座に中止**する。既に絞られている相手に再試行を
    重ねると状況を悪化させる (リトライ/バックオフは本設計の担当)。"""
    conn = _conn(tmp_path)
    calls = []

    def fetch(url):
        calls.append(url)
        raise httpx.HTTPStatusError(
            "429", request=httpx.Request("GET", url),
            response=httpx.Response(429, request=httpx.Request("GET", url)))

    with pytest.raises(RuntimeError, match="429"):
        import_dukascopy(conn, "USDJPY", H, H + timedelta(hours=5),
                         fetch=fetch, sleep=lambda s: None)

    assert len(calls) == 1, "429 の後も叩き続けている"


def test_import_dukascopy_aborts_on_503_without_retrying(tmp_path):
    """503 も同様 (実測で Dukascopy は UA 次第で 429/503 を出し分けた)。"""
    conn = _conn(tmp_path)
    calls = []

    def fetch(url):
        calls.append(url)
        raise httpx.HTTPStatusError(
            "503", request=httpx.Request("GET", url),
            response=httpx.Response(503, request=httpx.Request("GET", url)))

    with pytest.raises(RuntimeError, match="503"):
        import_dukascopy(conn, "USDJPY", H, H + timedelta(hours=5),
                         fetch=fetch, sleep=lambda s: None)

    assert len(calls) == 1


def test_import_dukascopy_refuses_range_over_request_cap(tmp_path):
    """**1 回の呼び出しで投げる本数に上限を置く。** 上限は「うっかり 10 年分を
    投げる」ことの防止であって、意図した長期取得を禁じるものではない
    (`max_requests` で明示的に上げられる)。"""
    conn = _conn(tmp_path)
    calls = []

    with pytest.raises(ValueError, match="max_requests"):
        import_dukascopy(conn, "USDJPY", H, H + timedelta(hours=1000),
                         fetch=lambda url: calls.append(url) or b"",
                         sleep=lambda s: None)

    assert calls == [], "上限判定の前に 1 本でも投げている"


def test_import_dukascopy_allows_explicit_larger_cap(tmp_path):
    """上限は明示的に引き上げられる (禁止ではなく「意図の表明」を要求する)。"""
    conn = _conn(tmp_path)
    calls = []

    r = import_dukascopy(conn, "USDJPY", H, H + timedelta(hours=600),
                         fetch=lambda url: calls.append(url) or b"",
                         sleep=lambda s: None, max_requests=600)

    assert r.inserted == 0
    assert len(calls) == 600
