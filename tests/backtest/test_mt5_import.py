from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from agentic_fx.backtest import mt5_import
from agentic_fx.backtest.mt5_import import (
    ImportConflictError, _default_fetch, compare_sources, import_mt5)
from agentic_fx.store import ohlcv
from tests.backtest.factories import H, SETTINGS, _conn


def test_import_mt5_pages_daily_and_imports(tmp_path):
    conn = _conn(tmp_path)
    calls = []

    def fetch(url):
        calls.append(url)
        # F5 (最終レビュー codex I2) の窓検証を満たすため、返すバーの time
        # は要求された窓の開始 (= "from") に合わせる (窓ごとに異なる)。
        window_start = _query_param(url, "from")
        return {"symbol": "USDJPY", "interval": "1m", "bars": [
            {"time": window_start, "open": 148.0, "high": 148.2,
             "low": 147.9, "close": 148.1, "volume": 10}]}

    r = import_mt5(conn, "USDJPY", H, H + timedelta(days=2),
                   base_url="http://x", fetch=fetch)
    assert r.inserted >= 1
    bars = ohlcv.load_history_bars(conn, "USDJPY", "1m", source="mt5")
    assert bars and bars[0].close == 148.1
    # 上書き §5: 2 日指定で fetch は 1 日窓 x 2 回呼ばれる
    assert len(calls) == 2
    for u in calls:
        assert "/ohlcv/USDJPY?" in u and "interval=1m" in u
    # MT5 は bid 系列を mid 近似として保存する — spread は必ず None (0.0 等の
    # 既知値を装うと「spread 不明」と「spread ゼロ」の区別が消え、バックテスト
    # のコストモデルが無料取引を読み込む実害になる。Task 4 の source パラメータ
    # 変異と同種の無音故障源)。
    assert ohlcv.load_history_spread(conn, "USDJPY", "1m", H.isoformat(),
                             source="mt5") is None


def test_import_mt5_uses_default_fetch_when_none(monkeypatch, tmp_path):
    """F1 (fix round 1, sonnet Important-1): fetch=None のとき実際に
    _default_fetch (httpx 経由) が使われ、その結果が DB まで届くこと。
    以前は `fetch = _default_fetch` の配線自体を検証するテストが無く、
    そこを `fetch = lambda url: {"bars": []}` に変異させても 14/14 PASS
    だった (SURVIVED)。httpx.get を monkeypatch し実 HTTP は行わない。"""
    conn = _conn(tmp_path)

    def fake_get(url, headers=None, timeout=30):
        request = httpx.Request("GET", url)
        return httpx.Response(
            200, request=request,
            json={"symbol": "USDJPY", "interval": "1m", "bars": [
                {"time": H.isoformat(), "open": 148.0, "high": 148.2,
                 "low": 147.9, "close": 148.1, "volume": 10}]})

    monkeypatch.setattr(mt5_import.httpx, "get", fake_get)
    r = import_mt5(conn, "USDJPY", H, H + timedelta(days=1),
                   base_url="http://x", fetch=None)
    assert r.inserted == 1
    bars = ohlcv.load_history_bars(conn, "USDJPY", "1m", source="mt5")
    assert bars and bars[0].close == 148.1


def test_import_mt5_last_window_clipped_to_end(tmp_path):
    """半端な最終窓 (< 1 日) でも to= が end に丸められる (over-fetch しない)。"""
    conn = _conn(tmp_path)
    calls = []

    def fetch(url):
        calls.append(url)
        return {"symbol": "USDJPY", "interval": "1m", "bars": []}

    end = H + timedelta(days=1, hours=3)
    import_mt5(conn, "USDJPY", H, end, base_url="http://x", fetch=fetch)
    assert len(calls) == 2
    # 2 本目の窓は end (H+1d3h) で打ち切られ、+2d の窓終端は含まれない
    to_param = _query_param(calls[1], "to")
    assert datetime.fromisoformat(to_param) == end
    assert datetime.fromisoformat(to_param) != H + timedelta(days=2)


def _query_param(url, name):
    return parse_qs(urlsplit(url).query)[name][0]


def test_import_mt5_rejects_bar_time_outside_requested_window(tmp_path):
    """F5 (最終レビュー codex I2): bridge が要求窓 [current, window_end) の
    外のバーを返した場合は無言混入させず ValueError (fail loud)。

    ⚠ **旧版は「窓終端ちょうど」を fail loud の対象にしていたが、実測で
    bridge は `to` を inclusive 解釈すると確定したため、終端ちょうどだけを
    良性の落とし物として除外した** (下の
    `test_import_mt5_drops_inclusive_right_edge_bar_without_error` が担当)。
    **fail loud 自体は弱めていない** — ここでは終端を「1 分超えた」バーを
    使い、真に窓外のものは従来どおり例外になることを固定する。
    """
    conn = _conn(tmp_path)
    window_end = H + timedelta(days=1)
    outside = window_end + timedelta(minutes=1)   # 終端ちょうどではなく明確に外

    def fetch(url):
        return {"symbol": "USDJPY", "interval": "1m", "bars": [
            {"time": outside.isoformat(), "open": 148.0, "high": 148.2,
             "low": 147.9, "close": 148.1, "volume": 10}]}

    with pytest.raises(ValueError, match="USDJPY"):
        import_mt5(conn, "USDJPY", H, window_end,
                   base_url="http://x", fetch=fetch)
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 0


def test_import_mt5_rejects_bar_time_before_window_start(tmp_path):
    """窓の**左**外側も従来どおり fail loud (右端の例外化で左が緩まない)。"""
    conn = _conn(tmp_path)
    window_end = H + timedelta(days=1)
    before = H - timedelta(minutes=1)

    def fetch(url):
        return {"symbol": "USDJPY", "interval": "1m", "bars": [
            {"time": before.isoformat(), "open": 148.0, "high": 148.2,
             "low": 147.9, "close": 148.1, "volume": 10}]}

    with pytest.raises(ValueError, match="USDJPY"):
        import_mt5(conn, "USDJPY", H, window_end,
                   base_url="http://x", fetch=fetch)
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 0


def test_import_mt5_drops_inclusive_right_edge_bar_without_error(tmp_path):
    """bridge は `to` を **inclusive** で返す (実機 :8812 で実測 —
    1 日窓に対し先頭 `T00:00`・末尾は翌 `T00:00` が含まれる)。

    終端ちょうどのバーは「次窓の開始」であって bridge の不具合ではないので、
    **例外にせず落とす**。旧実装はここで ValueError を投げており、
    境界に 1 本でも乗ると**取り込み全体が失敗**していた
    (実機で 1440 本が 1 本も入らなかった)。
    """
    conn = _conn(tmp_path)
    window_end = H + timedelta(days=1)

    def fetch(url):
        return {"symbol": "USDJPY", "interval": "1m", "bars": [
            {"time": H.isoformat(), "open": 148.0, "high": 148.2,
             "low": 147.9, "close": 148.1, "volume": 10},
            {"time": window_end.isoformat(), "open": 149.0, "high": 149.2,
             "low": 148.9, "close": 149.1, "volume": 20}]}

    r = import_mt5(conn, "USDJPY", H, window_end,
                   base_url="http://x", fetch=fetch)

    assert r.inserted == 1                       # 窓内の 1 本だけ入る
    bars = ohlcv.load_history_bars(conn, "USDJPY", "1m", source="mt5")
    assert [b.ts for b in bars] == [H]           # 終端の 1 本は入っていない


def test_import_mt5_right_edge_bar_is_picked_up_by_the_next_window(tmp_path):
    """落とした右端は**次窓の左端として取り込まれる** — 欠損しない。

    これが無いと「右端を落とす」判断が単なるデータ欠損になる。
    ページング境界をまたいで 1 本も失われないことを固定する。
    """
    conn = _conn(tmp_path)
    boundary = H + timedelta(days=1)             # 窓 1 の終端 = 窓 2 の開始

    def fetch(url):
        ws = datetime.fromisoformat(_query_param(url, "from"))
        we = datetime.fromisoformat(_query_param(url, "to"))
        # bridge の実挙動を模す: 窓の両端を inclusive で返す
        return {"symbol": "USDJPY", "interval": "1m", "bars": [
            {"time": ws.isoformat(), "open": 148.0, "high": 148.2,
             "low": 147.9, "close": 148.1, "volume": 10},
            {"time": we.isoformat(), "open": 149.0, "high": 149.2,
             "low": 148.9, "close": 149.1, "volume": 20}]}

    import_mt5(conn, "USDJPY", H, H + timedelta(days=2),
               base_url="http://x", fetch=fetch)

    bars = ohlcv.load_history_bars(conn, "USDJPY", "1m", source="mt5")
    # 窓 1 の右端 (= boundary) は窓 2 の左端として入る。最終窓の右端だけが
    # 落ちる — end は exclusive なので正しい
    assert [b.ts for b in bars] == [H, boundary]


def test_import_mt5_rejects_naive_start(tmp_path):
    conn = _conn(tmp_path)
    naive = datetime(2026, 7, 22, 12, 0)
    with pytest.raises(ValueError, match="start must be timezone-aware"):
        import_mt5(conn, "USDJPY", naive, naive + timedelta(days=1),
                   base_url="http://x", fetch=lambda url: {"bars": []})


def test_import_mt5_rejects_naive_end(tmp_path):
    conn = _conn(tmp_path)
    naive_end = datetime(2026, 7, 23, 12, 0)
    with pytest.raises(ValueError, match="end must be timezone-aware"):
        import_mt5(conn, "USDJPY", H, naive_end,
                   base_url="http://x", fetch=lambda url: {"bars": []})


def test_import_mt5_rejects_non_utc_start(tmp_path):
    conn = _conn(tmp_path)
    non_utc_start = H.replace(tzinfo=timezone(timedelta(hours=9)))
    with pytest.raises(ValueError, match="start must be UTC"):
        import_mt5(conn, "USDJPY", non_utc_start, H + timedelta(days=1),
                   base_url="http://x", fetch=lambda url: {"bars": []})


def test_import_mt5_rejects_non_utc_end(tmp_path):
    conn = _conn(tmp_path)
    non_utc_end = (H + timedelta(days=1)).replace(
        tzinfo=timezone(timedelta(hours=9)))
    with pytest.raises(ValueError, match="end must be UTC"):
        import_mt5(conn, "USDJPY", H, non_utc_end,
                   base_url="http://x", fetch=lambda url: {"bars": []})


def test_import_mt5_requires_bars_key(tmp_path):
    """payload の bars 欠落を ValueError で fail loud にする。"""
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="bars"):
        import_mt5(conn, "USDJPY", H, H + timedelta(days=1),
                   base_url="http://x", fetch=lambda url: {
                       "symbol": "USDJPY", "interval": "1m"})


def _bar(ts=H, **changes):
    bar = {"time": ts.isoformat(), "open": 148.0, "high": 148.2,
           "low": 147.9, "close": 148.1, "volume": 10}
    bar.update(changes)
    return bar


def _payload(*bars, symbol="USDJPY", interval="1m"):
    return {"symbol": symbol, "interval": interval, "bars": list(bars)}


@pytest.mark.parametrize("payload,match", [
    ([], "payload"),                                      # S1
    ({"interval": "1m", "bars": []}, "symbol"),         # S2a
    ({"symbol": "USDJPY", "bars": []}, "interval"),     # S2b
    ({"symbol": "USDJPY", "interval": "1m"}, "bars"),  # S2c
    ({"symbol": "USDJPY", "interval": "1m", "bars": None}, "bars"),
    ({"symbol": "USDJPY", "interval": "1m", "bars": {}}, "bars"),
], ids=["S1", "S2a", "S2b", "S2c", "S3-null", "S3-dict"])
def test_import_mt5_rejects_invalid_payload_structure(tmp_path, payload, match):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match=match):
        import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1),
                   base_url="http://x", fetch=lambda _url: payload)
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 0


@pytest.mark.parametrize("missing", [
    "time", "open", "high", "low", "close", "volume",
], ids=["S4a-time", "S4b-open", "S4c-high", "S4d-low", "S4e-close",
        "S4f-volume"])
def test_import_mt5_rejects_bar_missing_required_field(tmp_path, missing):
    conn = _conn(tmp_path)
    bar = _bar()
    del bar[missing]
    with pytest.raises(ValueError, match=missing):
        import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1),
                   base_url="http://x", fetch=lambda _url: _payload(bar))


def test_import_mt5_rejects_non_dict_bar(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="bar"):
        import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1),
                   base_url="http://x", fetch=lambda _url: _payload([]))


@pytest.mark.parametrize("field,value", [
    ("open", True), ("high", float("nan")), ("low", float("inf")),
    ("close", "148.1"), ("volume", False),
], ids=["S5a-bool", "S5b-nan", "S5c-inf", "S5d-str", "S5a-volume-bool"])
def test_import_mt5_rejects_non_finite_or_non_numeric_ohlcv(
        tmp_path, field, value):
    conn = _conn(tmp_path)
    with pytest.raises(
            ValueError,
            match=rf"bars\[0\] {field} must be a finite number"):
        import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1),
                   base_url="http://x",
                   fetch=lambda _url: _payload(_bar(**{field: value})))


def test_import_mt5_rejects_unparseable_time(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="time"):
        import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1),
                   base_url="http://x",
                   fetch=lambda _url: _payload(_bar(time="not-iso")))


@pytest.mark.parametrize("payload", [
    _payload(_bar(), symbol="EURUSD"),
    _payload(_bar(), interval="5m"),
], ids=["R1-symbol", "R2-interval"])
def test_import_mt5_rejects_payload_identity_mismatch(tmp_path, payload):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1),
                   base_url="http://x", fetch=lambda _url: payload)


@pytest.mark.parametrize("changes", [
    {"close": -1}, {"low": 148.05}, {"high": 148.05},
    {"volume": -1},
], ids=["R3-negative-price", "R4a-low", "R4b-high", "R5-volume"])
def test_import_mt5_rejects_invalid_ohlcv_relationships(tmp_path, changes):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1),
                   base_url="http://x",
                   fetch=lambda _url: _payload(_bar(**changes)))


@pytest.mark.parametrize("field,value,accepted", [
    ("low", 148.0, True),
    ("low", 148.0 + 1e-6, False),
    ("high", 148.1, True),
    ("high", 148.1 - 1e-6, False),
], ids=["low-equal", "low-above", "high-equal", "high-below"])
def test_import_mt5_ohlc_inclusion_uses_exact_boundaries(
        tmp_path, field, value, accepted):
    conn = _conn(tmp_path)
    fetch = lambda _url: _payload(_bar(**{field: value}))
    if accepted:
        result = import_mt5(
            conn, "USDJPY", H, H + timedelta(minutes=1),
            base_url="http://x", fetch=fetch)
        assert result.inserted == 1
    else:
        with pytest.raises(ValueError, match=field):
            import_mt5(
                conn, "USDJPY", H, H + timedelta(minutes=1),
                base_url="http://x", fetch=fetch)


def test_import_mt5_validates_right_edge_before_dropping(tmp_path):
    conn = _conn(tmp_path)
    end = H + timedelta(minutes=1)
    with pytest.raises(ValueError, match="prices must be > 0"):
        import_mt5(conn, "USDJPY", H, end, base_url="http://x",
                   fetch=lambda _url: _payload(
                       _bar(end, open=0, high=0, low=0, close=0)))


def test_import_mt5_rejects_two_right_edge_bars(tmp_path):
    conn = _conn(tmp_path)
    end = H + timedelta(minutes=1)
    with pytest.raises(ValueError, match="right-edge|window_end|right edge"):
        import_mt5(conn, "USDJPY", H, end, base_url="http://x",
                   fetch=lambda _url: _payload(_bar(end), _bar(end)))


@pytest.mark.parametrize("times", [
    [H, H],
    [H + timedelta(minutes=1), H],
], ids=["T2-duplicate", "T3-descending"])
def test_import_mt5_rejects_non_unique_or_non_increasing_times(tmp_path, times):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        import_mt5(conn, "USDJPY", H, H + timedelta(minutes=2),
                   base_url="http://x",
                   fetch=lambda _url: _payload(*[_bar(ts) for ts in times]))


@pytest.mark.parametrize("interval,offset", [
    ("5m", timedelta(minutes=1)),
    ("15m", timedelta(minutes=1)),
], ids=["T4a-5m-grid", "T4b-aware-offset-normalized-grid"])
def test_import_mt5_rejects_off_grid_bar_time(tmp_path, interval, offset):
    conn = _conn(tmp_path)
    ts = H + offset
    if interval == "15m":
        ts = ts.astimezone(timezone(timedelta(hours=9)))
    with pytest.raises(ValueError, match="grid|格子"):
        import_mt5(conn, "USDJPY", H, H + timedelta(minutes=15),
                   base_url="http://x", interval=interval,
                   fetch=lambda _url: _payload(_bar(ts), interval=interval))


def test_import_mt5_accepts_aware_non_utc_bar_on_utc_grid(tmp_path):
    conn = _conn(tmp_path)
    same_instant = H.astimezone(timezone(timedelta(hours=9)))
    result = import_mt5(
        conn, "USDJPY", H, H + timedelta(minutes=5), base_url="http://x",
        interval="5m",
        fetch=lambda _url: _payload(_bar(same_instant), interval="5m"))
    assert result.inserted == 1
    assert ohlcv.load_history_bars(conn, "USDJPY", "5m", source="mt5")[0].ts == H


def test_import_mt5_rejects_count_over_interval_capacity(tmp_path):
    conn = _conn(tmp_path)
    bars = [_bar(H + timedelta(seconds=i)) for i in range(61)]
    with pytest.raises(ValueError, match="count|本数|capacity"):
        import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1),
                   base_url="http://x", fetch=lambda _url: _payload(*bars))


def test_import_mt5_final_partial_window_uses_partial_capacity(tmp_path):
    conn = _conn(tmp_path)
    end = H + timedelta(days=1, minutes=5)

    def fetch(url):
        start = datetime.fromisoformat(_query_param(url, "from"))
        stop = datetime.fromisoformat(_query_param(url, "to"))
        if stop - start == timedelta(minutes=5):
            return _payload(*[_bar(start + timedelta(seconds=i))
                              for i in range(6)])
        return _payload()

    with pytest.raises(ValueError, match="count|本数|capacity"):
        import_mt5(conn, "USDJPY", H, end, base_url="http://x", fetch=fetch)


def test_import_mt5_right_edge_is_removed_before_capacity_check(tmp_path):
    conn = _conn(tmp_path)
    end = H + timedelta(minutes=1)
    result = import_mt5(conn, "USDJPY", H, end, base_url="http://x",
                        fetch=lambda _url: _payload(_bar(H), _bar(end)))
    assert result.inserted == 1


def test_import_mt5_5m_propagates_url_storage_and_daily_capacity(tmp_path):
    conn = _conn(tmp_path)
    seen = []
    bars = [_bar(H + timedelta(minutes=5 * i)) for i in range(288)]

    def fetch(url):
        seen.append(url)
        return _payload(*bars, interval="5m")

    result = import_mt5(conn, "USDJPY", H, H + timedelta(days=1),
                        base_url="http://x", fetch=fetch, interval="5m")
    assert result.inserted == 288
    assert _query_param(seen[0], "interval") == "5m"
    assert len(ohlcv.load_history_bars(
        conn, "USDJPY", "5m", source="mt5")) == 288


def test_import_mt5_rejects_unsupported_interval_before_fetch(tmp_path):
    conn = _conn(tmp_path)
    calls = []
    with pytest.raises(ValueError, match="interval"):
        import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1),
                   base_url="http://x", interval="2m",
                   fetch=lambda url: calls.append(url))
    assert calls == []


@pytest.mark.parametrize("start,end", [
    (H, H), (H + timedelta(minutes=1), H),
], ids=["W1a-equal", "W1a-reversed"])
def test_import_mt5_rejects_non_increasing_window_before_fetch(
        tmp_path, start, end):
    conn = _conn(tmp_path)
    calls = []
    with pytest.raises(ValueError, match="start|end"):
        import_mt5(conn, "USDJPY", start, end, base_url="http://x",
                   fetch=lambda url: calls.append(url))
    assert calls == []


def test_import_mt5_rejects_misaligned_window_before_fetch(tmp_path):
    conn = _conn(tmp_path)
    calls = []
    with pytest.raises(ValueError, match="grid|格子"):
        import_mt5(conn, "USDJPY", H + timedelta(minutes=1),
                   H + timedelta(minutes=11), base_url="http://x",
                   interval="5m", fetch=lambda url: calls.append(url))
    assert calls == []


def test_grid_alignment_preserves_microseconds_at_datetime_max_year():
    dt = datetime(9999, 1, 1, 0, 0, 0, 1, tzinfo=timezone.utc)
    assert mt5_import._is_grid_aligned(dt, 60) is False


def test_grid_alignment_accepts_aligned_time_before_epoch():
    dt = datetime(1969, 12, 31, 23, 59, tzinfo=timezone.utc)
    assert mt5_import._is_grid_aligned(dt, 60) is True


def test_import_mt5_two_window_failure_keeps_first_window(tmp_path):
    conn = _conn(tmp_path)

    def fetch(url):
        start = datetime.fromisoformat(_query_param(url, "from"))
        if start == H:
            return _payload(_bar(H))
        return _payload(_bar(start, open=0))

    with pytest.raises(ValueError):
        import_mt5(conn, "USDJPY", H, H + timedelta(days=2),
                   base_url="http://x", fetch=fetch)
    bars = ohlcv.load_history_bars(conn, "USDJPY", "1m", source="mt5")
    assert [bar.ts for bar in bars] == [H]


def test_import_mt5_validation_error_reports_failed_window_and_last_success(
        tmp_path):
    conn = _conn(tmp_path)

    def fetch(url):
        start = datetime.fromisoformat(_query_param(url, "from"))
        if start == H:
            return _payload(_bar(H))
        return _payload(_bar(start, open=0))

    with pytest.raises(ValueError) as raised:
        import_mt5(conn, "USDJPY", H, H + timedelta(days=2),
                   base_url="http://x", fetch=fetch)
    message = str(raised.value)
    assert f"failed window [{(H + timedelta(days=1)).isoformat()}" in message
    assert f"last successful window end={ (H + timedelta(days=1)).isoformat()}" in message


def test_import_mt5_same_payload_rerun_is_unchanged_only(tmp_path):
    conn = _conn(tmp_path)
    fetch = lambda _url: _payload(_bar(H))
    first = import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1),
                       base_url="http://x", fetch=fetch)
    second = import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1),
                        base_url="http://x", fetch=fetch)
    assert first.inserted == 1
    assert second == ohlcv.ImportResult(inserted=0, unchanged=1, conflicted=0)


def test_import_mt5_conflict_raises_with_existing_and_incoming_and_stops(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(
        conn, [("USDJPY", "1m", H.isoformat(), 148.0, 148.2, 147.9,
                148.1, 10, None)], source="mt5")
    calls = []

    def fetch(url):
        calls.append(url)
        start = datetime.fromisoformat(_query_param(url, "from"))
        return _payload(_bar(start, open=149.0, high=149.2, low=148.9,
                                 close=149.1, volume=20))

    with pytest.raises(ImportConflictError) as raised:
        import_mt5(conn, "USDJPY", H, H + timedelta(days=2),
                   base_url="http://x", fetch=fetch)
    assert len(calls) == 1
    assert raised.value.conflicts == [
        ("USDJPY", "1m", H.isoformat(),
         (148.0, 148.2, 147.9, 148.1, 10.0, None),
         (149.0, 149.2, 148.9, 149.1, 20, None)),
    ]
    stored = ohlcv.load_history_bars(conn, "USDJPY", "1m", source="mt5")
    assert stored[0].close == 148.1


def test_import_mt5_spread_only_conflict_is_reported(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO ohlcv_history "
        "(symbol, interval, bar_time, open, high, low, close, volume, "
        "source, spread) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("USDJPY", "1m", H.isoformat(), 148.0, 148.2, 147.9, 148.1,
         10, "mt5", 0.001))
    conn.commit()

    with pytest.raises(ImportConflictError) as raised:
        import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1),
                   base_url="http://x", fetch=lambda _url: _payload(_bar(H)))

    assert raised.value.conflicts == [
        ("USDJPY", "1m", H.isoformat(),
         (148.0, 148.2, 147.9, 148.1, 10.0, 0.001),
         (148.0, 148.2, 147.9, 148.1, 10, None)),
    ]


def test_conflict_details_mixed_new_and_conflicting_rows_reports_only_conflict(
        tmp_path):
    conn = _conn(tmp_path)
    conflict_time = H + timedelta(minutes=1)
    ohlcv.import_history_bars(
        conn, [("USDJPY", "1m", conflict_time.isoformat(), 148.0, 148.2,
                147.9, 148.1, 10, None)], source="mt5")

    rows = [
        ("USDJPY", "1m", H.isoformat(), 148.0, 148.2, 147.9,
         148.1, 10, None),
        ("USDJPY", "1m", conflict_time.isoformat(), 149.0, 149.2,
         148.9, 149.1, 20, None),
    ]

    conflicts = mt5_import._conflict_details(conn, rows)

    assert [item[2] for item in conflicts] == [conflict_time.isoformat()]


@pytest.mark.parametrize("delta,conflicted", [
    (5e-10, False),
    (2e-9, True),
], ids=["below-tolerance", "above-tolerance"])
def test_import_mt5_conflict_details_use_float_tolerance(
        tmp_path, delta, conflicted):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(
        conn, [("USDJPY", "1m", H.isoformat(), 148.0, 148.2, 147.9,
                148.1, 10, None)], source="mt5")
    fetch = lambda _url: _payload(_bar(H, close=148.1 + delta))

    if conflicted:
        with pytest.raises(ImportConflictError) as raised:
            import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1),
                       base_url="http://x", fetch=fetch)
        assert len(raised.value.conflicts) == 1
    else:
        result = import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1),
                            base_url="http://x", fetch=fetch)
        assert result == ohlcv.ImportResult(0, 1, 0)


def test_conflict_details_uses_store_float_tolerance():
    assert mt5_import._FLOAT_TOL == ohlcv._FLOAT_TOL


def test_import_mt5_reports_each_conflict_in_same_window(tmp_path):
    conn = _conn(tmp_path)
    second = H + timedelta(minutes=1)
    ohlcv.import_history_bars(
        conn, [
            ("USDJPY", "1m", H.isoformat(), 148.0, 148.2, 147.9,
             148.1, 10, None),
            ("USDJPY", "1m", second.isoformat(), 149.0, 149.2, 148.9,
             149.1, 20, None),
        ], source="mt5")

    with pytest.raises(ImportConflictError) as raised:
        import_mt5(
            conn, "USDJPY", H, H + timedelta(minutes=2),
            base_url="http://x",
            fetch=lambda _url: _payload(
                _bar(H, open=150.0, high=150.2, low=149.9, close=150.1),
                _bar(second, open=151.0, high=151.2, low=150.9,
                     close=151.1)))

    assert len(raised.value.conflicts) == 2


def test_import_mt5_second_window_conflict_exposes_committed_partial_result(
        tmp_path):
    conn = _conn(tmp_path)
    second_start = H + timedelta(days=1)
    ohlcv.import_history_bars(
        conn, [("USDJPY", "1m", second_start.isoformat(), 149.0, 149.2,
                148.9, 149.1, 20, None)], source="mt5")

    def fetch(url):
        start = datetime.fromisoformat(_query_param(url, "from"))
        return _payload(_bar(start))

    with pytest.raises(ImportConflictError) as raised:
        import_mt5(conn, "USDJPY", H, H + timedelta(days=2),
                   base_url="http://x", fetch=fetch)

    assert raised.value.partial == ohlcv.ImportResult(
        inserted=1, unchanged=0, conflicted=1)


def test_import_mt5_conflicted_count_without_details_fails_loudly(
        tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        mt5_import, "import_history_bars",
        lambda *_args, **_kwargs: ohlcv.ImportResult(0, 0, 2))

    with pytest.raises(
            ImportConflictError,
            match="conflicted=2 but no detail rows matched") as raised:
        import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1),
                   base_url="http://x", fetch=lambda _url: _payload(_bar(H)))

    assert raised.value.conflicts == []


def test_import_mt5_normalizes_naive_bar_time_for_join(tmp_path):
    """上書き §3: bridge が naive time を返しても Dukascopy 行と JOIN できる
    こと (正規化を怠ると compare_sources が 0 件の無音故障になる)。"""
    conn = _conn(tmp_path)
    naive_time = H.replace(tzinfo=None).isoformat()

    def fetch(url):
        return {"symbol": "USDJPY", "interval": "1m", "bars": [
            {"time": naive_time, "open": 148.0, "high": 148.2,
             "low": 147.9, "close": 148.10, "volume": 10}]}

    import_mt5(conn, "USDJPY", H, H + timedelta(days=1),
              base_url="http://x", fetch=fetch)
    ohlcv.import_history_bars(conn, [("USDJPY", "1m", H.isoformat(),
                              148.005, 148.2, 147.9, 148.105, 5, 0.01)],
                      source="dukascopy")
    rep = compare_sources(conn, "USDJPY", SETTINGS)
    assert rep["count"] == 1


def test_compare_sources_reports_distribution(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [("USDJPY", "1m", H.isoformat(),
                              148.005, 148.2, 147.9, 148.105, 5, 0.01)],
                      source="dukascopy")
    ohlcv.import_history_bars(conn, [("USDJPY", "1m", H.isoformat(),
                              148.0, 148.2, 147.9, 148.10, 5, None)],
                      source="mt5")
    rep = compare_sources(conn, "USDJPY", SETTINGS)
    # a_mid=148.105, assumed_spread_pips=1.0 -> half_spread=0.005 (USDJPY
    # pip_size=0.01) -> (148.105-0.005) - 148.10 == 0.0 ちょうど。
    # brief の `< 0.01` はスモークチェックに過ぎず half_spread の /2 抜け・
    # 符号反転どちらの mutant も通してしまうため、厳密値でピンする。
    assert rep["count"] == 1
    assert rep["mean"] == pytest.approx(0.0, abs=1e-9)
    assert rep["std"] == 0.0
    assert rep["max_abs"] == pytest.approx(0.0, abs=1e-9)


def test_compare_sources_reports_three_point_population_distribution(tmp_path):
    conn = _conn(tmp_path)
    times = [H + timedelta(minutes=i) for i in range(3)]
    # half_spread=0.005; resulting differences are exactly -0.01, 0.02, 0.05.
    a_closes = [148.095, 148.125, 148.155]
    b_closes = [148.10, 148.10, 148.10]
    ohlcv.import_history_bars(
        conn, [("USDJPY", "1m", ts.isoformat(), close, close, close,
                close, 5, 0.01) for ts, close in zip(times, a_closes)],
        source="dukascopy")
    ohlcv.import_history_bars(
        conn, [("USDJPY", "1m", ts.isoformat(), close, close, close,
                close, 5, None) for ts, close in zip(times, b_closes)],
        source="mt5")

    rep = compare_sources(conn, "USDJPY", SETTINGS)

    assert rep["count"] == 3
    assert rep["mean"] == pytest.approx(0.02)
    assert rep["std"] == pytest.approx((0.0006) ** 0.5)
    assert rep["max_abs"] == pytest.approx(0.05)


def test_compare_sources_empty_overlap_returns_none_fields(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [("USDJPY", "1m", H.isoformat(),
                              148.005, 148.2, 147.9, 148.105, 5, 0.01)],
                      source="dukascopy")
    rep = compare_sources(conn, "USDJPY", SETTINGS)
    assert rep == {"count": 0, "mean": None, "std": None, "max_abs": None}


def test_compare_sources_unknown_symbol_raises_keyerror(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(KeyError):
        compare_sources(conn, "GBPUSD", SETTINGS)


# 既定 fetch (_default_fetch) — httpx.get を monkeypatch し実 HTTP は行わない。
# Dukascopy と異なり 404 は素通し raise (MT5 では想定外)。

def test_default_fetch_sends_headers_and_returns_json(monkeypatch):
    seen = {}

    def fake_get(url, headers=None, timeout=30):
        seen["url"] = url
        seen["headers"] = headers
        seen["timeout"] = timeout
        request = httpx.Request("GET", url)
        return httpx.Response(
            200, request=request,
            json={"symbol": "USDJPY", "interval": "1m", "bars": []})

    monkeypatch.setenv("MT5_BRIDGE_API_KEY", "secret-key")
    monkeypatch.setattr(mt5_import.httpx, "get", fake_get)
    result = _default_fetch("http://x/ohlcv/USDJPY?from=a&to=b&interval=1m")
    assert result == {"symbol": "USDJPY", "interval": "1m", "bars": []}
    assert seen["headers"] == {"X-Bridge-Api-Key": "secret-key"}
    assert seen["timeout"] == 30


def test_default_fetch_raises_on_404(monkeypatch):
    def fake_get(url, headers=None, timeout=30):
        request = httpx.Request("GET", url)
        return httpx.Response(404, request=request)

    monkeypatch.setattr(mt5_import.httpx, "get", fake_get)
    with pytest.raises(httpx.HTTPStatusError):
        _default_fetch("http://x/ohlcv/USDJPY?from=a&to=b&interval=1m")


def test_default_fetch_raises_on_500(monkeypatch):
    def fake_get(url, headers=None, timeout=30):
        request = httpx.Request("GET", url)
        return httpx.Response(500, request=request)

    monkeypatch.setattr(mt5_import.httpx, "get", fake_get)
    with pytest.raises(httpx.HTTPStatusError):
        _default_fetch("http://x/ohlcv/USDJPY?from=a&to=b&interval=1m")
