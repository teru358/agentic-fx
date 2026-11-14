from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from agentic_fx.backtest import mt5_import
from agentic_fx.backtest.mt5_import import (
    _default_fetch, compare_sources, import_mt5 as _import_mt5)
from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.sources import BridgeTimeRuleMismatch, BridgeTimeUnverified
from agentic_fx.datafeed.closed_bars import normalize_closed_range
from agentic_fx.store import ohlcv
from tests.backtest.factories import H, SETTINGS, _conn


def import_mt5(*args, **kwargs):
    """検証系の test 用。取得・検証の拒否は結果の failure (履歴 0 件) になるので、
    ここでは ValueError に読み替えて、拒否の理由を `match` で確かめる。
    結果の契約そのものは下の test が `_import_mt5` で直接見る。"""
    outcome = _import_mt5(*args, **kwargs)
    if outcome.status == "failed" and outcome.failure in ("invalid_response", "too_large"):
        raise ValueError(outcome.detail or outcome.failure)
    return outcome


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


def test_importer_window_filter_matches_shared_closed_normalizer(tmp_path):
    """bridge time を UTC 化した後は live と同じ [start,end) 判定を使う。"""
    conn = _conn(tmp_path)
    start = H
    end = H + timedelta(minutes=3)
    raw = [_bar(start),
           _bar(start + timedelta(minutes=1)),
           _bar(start + timedelta(minutes=2)), _bar(end)]
    result = import_mt5(conn, "USDJPY", start, end, base_url="http://x",
                        fetch=lambda _url: _payload(*raw))
    expected = normalize_closed_range(
        [Bar("USDJPY", "1m", start + timedelta(minutes=i),
             148.0, 148.2, 147.9, 148.1, 10)
         for i in range(3)], interval="1m", start=start, end=end,
        cutoff=end, grace=timedelta(0))
    got = ohlcv.load_history_bars(conn, "USDJPY", "1m", source="mt5")
    assert result.inserted == 3
    assert [bar.ts for bar in got] == [bar.ts for bar in expected]


def test_importer_uses_normalized_boundary_rows_with_the_fixed_clock(monkeypatch):
    start = H
    end = H + timedelta(minutes=2)
    now = H + timedelta(days=1)
    grace = timedelta(seconds=45)
    closed = _bar(start)
    forming_before_cutoff = _bar(start + timedelta(minutes=1))
    inclusive_right_edge = _bar(end)
    received = []

    def normalized_only_closed(raw, **kwargs):
        received.append((raw, kwargs))
        assert kwargs["start"] == start
        assert kwargs["end"] == end
        assert kwargs["cutoff"] == now
        assert kwargs["grace"] == grace
        return [raw[0]]

    monkeypatch.setattr(mt5_import, "normalize_closed_range", normalized_only_closed)
    rows, right_edge_count = mt5_import._validated_window_rows(
        _payload(closed, forming_before_cutoff, inclusive_right_edge),
        symbol="USDJPY", interval="1m", current=start, window_end=end,
        now=now, grace=grace)

    assert [bar.ts for bar in received[0][0]] == [start, start + timedelta(minutes=1)]
    assert right_edge_count == 1
    assert [row[2] for row in rows] == [start.isoformat()]


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


def test_import_mt5_inserts_when_same_bar_exists_from_another_source(tmp_path):
    conn = _conn(tmp_path)
    dukascopy_row = (
        "USDJPY", "1m", H.isoformat(), 147.0, 147.2, 146.9, 147.1, 20, None)
    ohlcv.import_history_bars(conn, [dukascopy_row], source="dukascopy")

    fetch = lambda _url: _payload(_bar())
    result = import_mt5(
        conn, "USDJPY", H, H + timedelta(minutes=1),
        base_url="http://x", fetch=fetch)
    repeated = import_mt5(
        conn, "USDJPY", H, H + timedelta(minutes=1),
        base_url="http://x", fetch=fetch)

    assert (result.inserted, result.unchanged, result.conflicted) == (1, 0, 0)
    assert (repeated.inserted, repeated.unchanged, repeated.conflicted) == (0, 1, 0)
    mt5_bars = ohlcv.load_history_bars(conn, "USDJPY", "1m", source="mt5")
    assert len(mt5_bars) == 1
    assert mt5_bars[0].close == 148.1


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


def test_import_mt5_logs_right_edge_count_once(tmp_path, caplog):
    conn = _conn(tmp_path)
    window_end = H + timedelta(minutes=1)

    with caplog.at_level("DEBUG", logger="agentic_fx.backtest.mt5_import"):
        result = import_mt5(
            conn, "USDJPY", H, window_end, base_url="http://x",
            fetch=lambda _url: _payload(_bar(), _bar(window_end)))

    assert result.inserted == 1
    right_edge_debugs = [
        record for record in caplog.records
        if record.levelname == "DEBUG" and "right-edge" in record.message]
    assert len(right_edge_debugs) == 1
    assert "dropped 1 right-edge bar(s)" in right_edge_debugs[0].message


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


def test_import_mt5_accepts_zero_volume_bar(tmp_path):
    conn = _conn(tmp_path)

    result = import_mt5(
        conn, "USDJPY", H, H + timedelta(minutes=1),
        base_url="http://x", fetch=lambda _url: _payload(_bar(volume=0)))

    assert (result.inserted, result.unchanged, result.conflicted) == (1, 0, 0)
    bars = ohlcv.load_history_bars(conn, "USDJPY", "1m", source="mt5")
    assert len(bars) == 1
    assert bars[0].volume == 0


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


@pytest.mark.parametrize("kind", ["duplicates", "off_grid"])
def test_import_mt5_rejects_oversized_window_before_validating_its_rows(tmp_path, kind):
    conn = _conn(tmp_path)
    end = H + timedelta(days=2)
    capacity = 1440
    if kind == "duplicates":
        oversized = [_bar(H + timedelta(days=1)) for _ in range(capacity + 2)]
    else:
        oversized = [_bar(H + timedelta(days=1, seconds=1 + i))
                     for i in range(capacity + 2)]
    calls = []

    def fetch(url):
        start = datetime.fromisoformat(_query_param(url, "from"))
        calls.append(start)
        if start == H:
            return _payload(_bar(H))
        # 先頭に壊れた行を置く: 上限の検査が行の検査より先なら、上限の理由で拒否される。
        return _payload(None, *oversized)

    with pytest.raises(ValueError, match="exceeds window capacity"):
        import_mt5(conn, "USDJPY", H, end, base_url="http://x", fetch=fetch)
    assert len(calls) == 2
    assert _stored_row_count(conn) == 0


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


def test_import_mt5_two_window_failure_keeps_no_window(tmp_path):
    conn = _conn(tmp_path)

    def fetch(url):
        start = datetime.fromisoformat(_query_param(url, "from"))
        if start == H:
            return _payload(_bar(H))
        return _payload(_bar(start, open=0))

    with pytest.raises(ValueError):
        import_mt5(conn, "USDJPY", H, H + timedelta(days=2),
                   base_url="http://x", fetch=fetch)
    assert ohlcv.load_history_bars(conn, "USDJPY", "1m", source="mt5") == []


def test_import_mt5_validation_error_reports_failed_window_and_that_nothing_was_written(
        tmp_path):
    conn = _conn(tmp_path)

    def fetch(url):
        start = datetime.fromisoformat(_query_param(url, "from"))
        if start == H:
            return _payload(_bar(H))
        return _payload(_bar(start, open=0))

    out = _import_mt5(conn, "USDJPY", H, H + timedelta(days=2),
                      base_url="http://x", fetch=fetch)
    assert (out.status, out.failure) == ("failed", "invalid_response")
    assert f"failed window [{(H + timedelta(days=1)).isoformat()}" in out.detail
    assert out.resume_from == H.isoformat()
    assert _stored_row_count(conn) == 0


def test_import_mt5_same_payload_rerun_is_unchanged_only(tmp_path):
    conn = _conn(tmp_path)
    fetch = lambda _url: _payload(_bar(H))
    first = import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1),
                       base_url="http://x", fetch=fetch)
    second = import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1),
                        base_url="http://x", fetch=fetch)
    assert first.inserted == 1
    assert (second.inserted, second.unchanged, second.conflicted) == (0, 1, 0)


def _seed_mt5(conn, *rows):
    ohlcv.import_history_bars(conn, list(rows), source="mt5")


def _mt5_row(ts, o=148.0, h=148.2, low=147.9, c=148.1, v=10, spread=None):
    return ("USDJPY", "1m", ts.isoformat(), o, h, low, c, v, spread)


def _unchanged_fillers(conn, n, start=1):
    """遮断の分母 (unchanged) を作る既存行。"""
    _seed_mt5(conn, *[_mt5_row(H + timedelta(minutes=i)) for i in range(start, start + n)])


def test_import_mt5_isolated_conflict_is_recorded_and_import_continues(tmp_path):
    conn = _conn(tmp_path)
    _seed_mt5(conn, _mt5_row(H))
    calls = []

    def fetch(url):
        calls.append(url)
        start = datetime.fromisoformat(_query_param(url, "from"))
        bars = [_bar(start, open=149.0, high=149.2, low=148.9, close=149.1, volume=20)]
        if start != H:
            bars = [_bar(start)]
        return _payload(*bars)

    out = _import_mt5(conn, "USDJPY", H, H + timedelta(days=2),
                      base_url="http://x", fetch=fetch)
    assert len(calls) == 2
    assert out.status == "completed_with_conflicts"
    assert (out.inserted, out.unchanged, out.conflicted_new) == (1, 0, 1)
    stored = ohlcv.load_history_bars(conn, "USDJPY", "1m", source="mt5")
    assert [b.ts for b in stored] == [H, H + timedelta(days=1)]
    assert stored[0].close == 148.1                       # 既存値は不変
    line = out.conflict_lines[0]
    assert line.existing == (148.0, 148.2, 147.9, 148.1, 10.0, None)
    assert line.incoming == (149.0, 149.2, 148.9, 149.1, 20, None)
    assert (line.id, line.state, line.known) == (1, "open", False)
    assert conn.execute("SELECT COUNT(*) FROM history_conflicts").fetchone()[0] == 1


def test_import_mt5_spread_only_conflict_is_recorded(tmp_path):
    conn = _conn(tmp_path)
    _seed_mt5(conn, _mt5_row(H, spread=0.001))
    out = _import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1),
                      base_url="http://x", fetch=lambda _url: _payload(_bar(H)))
    assert out.conflicted_new == 1
    assert out.conflict_lines[0].existing == (148.0, 148.2, 147.9, 148.1, 10.0, 0.001)
    assert out.conflict_lines[0].incoming == (148.0, 148.2, 147.9, 148.1, 10, None)


def test_import_mt5_mixed_new_and_conflicting_rows_records_only_the_conflict(tmp_path):
    conn = _conn(tmp_path)
    conflict_time = H + timedelta(minutes=1)
    _seed_mt5(conn, _mt5_row(conflict_time))
    out = _import_mt5(
        conn, "USDJPY", H, H + timedelta(minutes=2), base_url="http://x",
        fetch=lambda _url: _payload(
            _bar(H), _bar(conflict_time, open=149.0, high=149.2, low=148.9,
                          close=149.1, volume=20)))
    assert [line.bar_time for line in out.conflict_lines] == [conflict_time.isoformat()]
    assert out.inserted == 1
    assert [b.ts for b in ohlcv.load_history_bars(conn, "USDJPY", "1m", source="mt5")] \
        == [H, conflict_time]


@pytest.mark.parametrize("delta,conflicted", [
    (5e-10, False),
    (2e-9, True),
], ids=["below-tolerance", "above-tolerance"])
def test_import_mt5_conflicts_use_store_float_tolerance(tmp_path, delta, conflicted):
    conn = _conn(tmp_path)
    _seed_mt5(conn, _mt5_row(H))
    out = _import_mt5(conn, "USDJPY", H, H + timedelta(minutes=1), base_url="http://x",
                      fetch=lambda _url: _payload(_bar(H, close=148.1 + delta)))
    assert (out.conflicted, out.unchanged) == ((1, 0) if conflicted else (0, 1))


def test_import_mt5_systematic_shift_is_blocked_with_zero_writes(tmp_path):
    conn = _conn(tmp_path)
    _seed_mt5(conn, *[_mt5_row(H + timedelta(minutes=i)) for i in range(30)])
    shifted = [_bar(H + timedelta(minutes=i), close=148.15) for i in range(30)]
    shifted += [_bar(H + timedelta(minutes=i)) for i in range(30, 40)]    # 新規の正常な足
    out = _import_mt5(conn, "USDJPY", H, H + timedelta(minutes=40), base_url="http://x",
                      fetch=lambda _url: _payload(*shifted))
    assert out.status == "conflict_circuit_open"
    assert (out.conflicted_new, out.n_test) == (30, 30)
    assert _stored_row_count(conn) == 30
    assert conn.execute("SELECT COUNT(*) FROM history_conflicts").fetchone()[0] == 0
    assert len(out.conflict_lines) == 30


def test_import_mt5_second_window_conflict_keeps_running_and_counts_both_windows(tmp_path):
    conn = _conn(tmp_path)
    second_start = H + timedelta(days=1)
    _seed_mt5(conn, _mt5_row(second_start, o=149.0, h=149.2, low=148.9, c=149.1, v=20))

    def fetch(url):
        return _payload(_bar(datetime.fromisoformat(_query_param(url, "from"))))

    out = _import_mt5(conn, "USDJPY", H, H + timedelta(days=2),
                      base_url="http://x", fetch=fetch)
    assert (out.inserted, out.unchanged, out.conflicted) == (1, 0, 1)
    assert out.status == "completed_with_conflicts"


def test_import_mt5_normalizes_aware_non_utc_bar_time_for_join(tmp_path):
    """bridge が UTC 以外の aware time を返しても Dukascopy 行と JOIN できること
    (正規化を怠ると compare_sources が 0 件の無音故障になる)。"""
    conn = _conn(tmp_path)
    jst_time = H.astimezone(timezone(timedelta(hours=9))).isoformat()

    def fetch(url):
        return {"symbol": "USDJPY", "interval": "1m", "bars": [
            {"time": jst_time, "open": 148.0, "high": 148.2,
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


def _stored_row_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0]


@pytest.mark.parametrize("error,expected_failure", [
    (BridgeTimeRuleMismatch("time_rule_mismatch"), "time_rule_mismatch"),
    (BridgeTimeUnverified("time_rule_unverified"), "time_rule_unverified"),
    (ValueError("broken window"), "fetch_failed"),
], ids=["mismatch", "unverified", "invalid"])
def test_import_mt5_keeps_nothing_when_a_later_window_fails(tmp_path, error, expected_failure):
    conn = _conn(tmp_path)
    calls = []

    def fetch(url):
        calls.append(url)
        if len(calls) == 2:
            raise error
        start = datetime.fromisoformat(_query_param(url, "from"))
        return _payload(_bar(start))

    out = _import_mt5(conn, "USDJPY", H, H + timedelta(days=3),
                      base_url="http://x", fetch=fetch)

    assert (out.status, out.failure) == ("failed", expected_failure)
    assert len(calls) == 2
    assert _stored_row_count(conn) == 0
    assert not conn.in_transaction


def test_import_mt5_commits_the_whole_stage_in_one_batch(tmp_path):
    conn = _conn(tmp_path)
    commits = []

    class _Spy:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def commit(self):
            commits.append(self._inner.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0])
            self._inner.commit()

    result = import_mt5(
        _Spy(conn), "USDJPY", H, H + timedelta(days=3), base_url="http://x",
        fetch=lambda url: _payload(_bar(datetime.fromisoformat(_query_param(url, "from")))))

    assert result.inserted == 3
    assert commits == [3]


def test_import_history_bars_without_commit_needs_a_transaction(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="open transaction"):
        ohlcv.import_history_bars(
            conn, [("USDJPY", "1m", H.isoformat(), 148.0, 148.2, 147.9, 148.1, 10, None)],
            source="mt5", commit=False)
    assert _stored_row_count(conn) == 0


def test_import_mt5_does_not_hold_the_write_lock_while_fetching(tmp_path):
    from agentic_fx.store.db import connect
    conn = _conn(tmp_path)
    other = connect(tmp_path / "t.db")
    other.execute("PRAGMA busy_timeout=0")
    seen = []

    def fetch(url):
        start = datetime.fromisoformat(_query_param(url, "from"))
        assert not conn.in_transaction
        other.execute("BEGIN IMMEDIATE")
        other.rollback()
        seen.append(start)
        return _payload(_bar(start))

    result = import_mt5(conn, "USDJPY", H, H + timedelta(days=3),
                        base_url="http://x", fetch=fetch)
    assert len(seen) == 3
    assert result.inserted == 3


def test_import_mt5_refuses_a_period_too_large_before_fetching(tmp_path, monkeypatch):
    monkeypatch.setattr(mt5_import, "MAX_IMPORT_ROWS", 100)
    conn = _conn(tmp_path)
    calls = []

    with pytest.raises(mt5_import.ImportTooLargeError, match="期間を分けて"):
        import_mt5(conn, "USDJPY", H, H + timedelta(minutes=101),
                   base_url="http://x", fetch=lambda url: calls.append(url))

    assert calls == []
    assert ohlcv.load_history_bars(conn, "USDJPY", "1m", source="mt5") == []


def test_import_mt5_accepts_a_period_exactly_at_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(mt5_import, "MAX_IMPORT_ROWS", 100)
    conn = _conn(tmp_path)
    r = import_mt5(conn, "USDJPY", H, H + timedelta(minutes=100), base_url="http://x",
                   fetch=lambda url: {"symbol": "USDJPY", "interval": "1m", "bars": []})
    assert r.inserted == 0
