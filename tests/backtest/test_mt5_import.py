from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from agentic_fx.backtest import mt5_import
from agentic_fx.backtest.mt5_import import (
    _default_fetch, compare_sources, import_mt5)
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
    """payload に "bars" キーが無い (bridge の障害応答等) 場合は KeyError で
    fail-loud する — .get(..., []) で「0 件取り込み」と静かに区別が付かなく
    なることを防ぐ (sources.py:mt5_bars_range と同じ契約)。"""
    conn = _conn(tmp_path)
    with pytest.raises(KeyError):
        import_mt5(conn, "USDJPY", H, H + timedelta(days=1),
                   base_url="http://x", fetch=lambda url: {"error": "oops"})


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
