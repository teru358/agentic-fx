import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Bar, FixedClock, Quote
from agentic_fx.datafeed import sources
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.store import ohlcv
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"


def _provider(tmp_path, mt5=False):
    s = load_settings(EXAMPLE)
    if mt5:
        s = s.model_copy(deep=True)
        s.datafeed.mt5.enabled = True
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn, PriceProvider(conn, s, FixedClock(NOW))


def _fresh_bars(pair="USDJPY", n=30, interval="1m"):
    """直近 n 本の健全なバー。interval を変えると足の幅も追従する。"""
    step = timedelta(minutes=sources.INTERVAL_MIN[interval])
    start = NOW - step * n
    return [Bar(pair, interval, start + step * i,
                148.0, 148.1, 147.9, 148.05, 10) for i in range(n)]


def test_default_uses_yfinance(tmp_path):
    _, p = _provider(tmp_path)
    q = Quote("USDJPY", 148.5, 148.5, NOW, "yfinance")
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               return_value=q):
        assert p.get_quote("USDJPY").source == "yfinance"


def test_mt5_preferred_when_enabled(tmp_path):
    _, p = _provider(tmp_path, mt5=True)
    mq = Quote("USDJPY", 148.49, 148.51, NOW, "mt5")
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_quote",
               return_value=mq) as m:
        q = p.get_quote("USDJPY")
    assert q.source == "mt5"
    m.assert_called_once()


def test_falls_back_on_mt5_failure(tmp_path):
    _, p = _provider(tmp_path, mt5=True)
    yq = Quote("USDJPY", 148.5, 148.5, NOW, "yfinance")
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_quote",
               side_effect=OSError("bridge down")), \
         patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               return_value=yq):
        assert p.get_quote("USDJPY").source == "yfinance"


def test_unhealthy_quote_falls_through(tmp_path):
    _, p = _provider(tmp_path, mt5=True)
    stale = Quote("USDJPY", 148.49, 148.51, NOW - timedelta(hours=2), "mt5")
    yq = Quote("USDJPY", 148.5, 148.5, NOW, "yfinance")
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_quote",
               return_value=stale), \
         patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               return_value=yq):
        assert p.get_quote("USDJPY").source == "yfinance"


def test_all_sources_dead_raises(tmp_path):
    _, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               side_effect=OSError("down")):
        with pytest.raises(DataUnhealthy):
            p.get_quote("USDJPY")


def test_naive_datetime_from_source_falls_through(tmp_path):
    """sources 側の naive 拒否 (ValueError) でもフォールバックが効くこと。

    ソース毎の except を DataUnhealthy/HTTPError に絞ると ValueError が
    漏れて get_quote ごと落ちる (フォールバックのはずが全滅する)。
    """
    _, p = _provider(tmp_path, mt5=True)
    yq = Quote("USDJPY", 148.5, 148.5, NOW, "yfinance")
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_quote",
               side_effect=ValueError("mt5 returned a naive datetime")), \
         patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               return_value=yq):
        assert p.get_quote("USDJPY").source == "yfinance"


def test_naive_datetime_from_bars_source_falls_through(tmp_path):
    _, p = _provider(tmp_path, mt5=True)
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_bars",
               side_effect=ValueError("mt5 returned a naive datetime")), \
         patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_fresh_bars()):
        bars = p.get_bars("USDJPY", "1m", 1)
    assert len(bars) == 30
    assert p.last_bars_source("USDJPY", "1m") == "yfinance"


def test_get_bars_caches(tmp_path):
    conn, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_fresh_bars()):
        bars = p.get_bars("USDJPY", "1m", 1)
    assert len(bars) == 30
    assert len(ohlcv.load_bars(conn, "USDJPY", "1m")) == 30


def test_get_bars_falls_back_to_cache(tmp_path):
    conn, p = _provider(tmp_path)
    ohlcv.upsert_bars(conn, _fresh_bars())
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               side_effect=OSError("down")):
        bars = p.get_bars("USDJPY", "1m", 1)
    assert len(bars) == 30


def test_stale_cache_is_not_used(tmp_path):
    """キャッシュも健全性検証を通ること (古いバーを黙って返さない)。"""
    conn, p = _provider(tmp_path)
    stale = [b.__class__(b.symbol, b.interval, b.ts - timedelta(days=3),
                         b.open, b.high, b.low, b.close, b.volume)
             for b in _fresh_bars()]
    ohlcv.upsert_bars(conn, stale)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               side_effect=OSError("down")):
        with pytest.raises(DataUnhealthy):
            p.get_bars("USDJPY", "1m", 1)


def test_latest_1m_bar_none_on_unhealthy(tmp_path):
    _, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               side_effect=OSError("down")):
        assert p.latest_1m_bar("USDJPY") is None


def test_latest_1m_bar_returns_last(tmp_path):
    _, p = _provider(tmp_path)
    fresh = _fresh_bars()
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=fresh):
        assert p.latest_1m_bar("USDJPY").ts == fresh[-1].ts


def test_spec_builtin(tmp_path):
    _, p = _provider(tmp_path)
    assert p.spec("USDJPY").pip_size == 0.01
    assert p.spec("EURUSD").pip_size == 0.0001
    assert p.spec("USDJPY").contract_size == 100_000


def test_spec_unknown_pair_raises(tmp_path):
    _, p = _provider(tmp_path)
    with pytest.raises(DataUnhealthy):
        p.spec("GBPAUD")


def test_td_quote_used_when_mt5_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("TWELVEDATA_API_KEY", "k")
    s = load_settings(EXAMPLE).model_copy(deep=True)
    s.datafeed.mt5.enabled = True
    s.datafeed.twelvedata.enabled = True
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    p = PriceProvider(conn, s, FixedClock(NOW))
    tdq = Quote("USDJPY", 148.49, 148.51, NOW, "twelvedata")
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_quote",
               side_effect=OSError("down")), \
         patch("agentic_fx.datafeed.price_provider.sources.td_quote",
               return_value=tdq):
        assert p.get_quote("USDJPY").source == "twelvedata"


def _td_only_provider(tmp_path):
    """TD のみ enabled の provider (失敗経路のメッセージを単離するため)。"""
    s = load_settings(EXAMPLE).model_copy(deep=True)
    s.datafeed.twelvedata.enabled = True
    s.datafeed.yfinance.enabled = False
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return PriceProvider(conn, s, FixedClock(NOW))


def test_td_http_error_does_not_leak_api_key(tmp_path, monkeypatch, caplog):
    """TD の 401 で API キーが DataUnhealthy にもログにも出ないこと。

    td_quote/td_bars は Twelve Data の仕様上 apikey をクエリで送るため、
    httpx の例外文字列は URL ごとキーを含む。集約するこの層で潰す。
    401 という診断情報は残すこと。
    """
    monkeypatch.setenv("TWELVEDATA_API_KEY", "SECRET_KEY_123")
    p = _td_only_provider(tmp_path)
    url = ("https://api.twelvedata.com/quote"
           "?symbol=USD%2FJPY&apikey=SECRET_KEY_123")
    req = httpx.Request("GET", url)
    err = httpx.HTTPStatusError(
        f"Client error '401 Unauthorized' for url '{url}'",
        request=req, response=httpx.Response(401, request=req))
    with caplog.at_level(logging.WARNING, logger="agentic_fx.price"), \
         patch("agentic_fx.datafeed.price_provider.sources.td_quote",
               side_effect=err):
        with pytest.raises(DataUnhealthy) as ei:
            p.get_quote("USDJPY")
    assert "SECRET_KEY_123" not in str(ei.value)
    assert "SECRET_KEY_123" not in caplog.text
    assert "401" in str(ei.value) and "401" in caplog.text


def test_td_bars_http_error_does_not_leak_api_key(tmp_path, monkeypatch, caplog):
    """bars 側の集約も同じ扱い (quote だけ塞いでも意味がない)。"""
    monkeypatch.setenv("TWELVEDATA_API_KEY", "SECRET_KEY_123")
    p = _td_only_provider(tmp_path)
    url = ("https://api.twelvedata.com/time_series"
           "?symbol=USD%2FJPY&apikey=SECRET_KEY_123")
    req = httpx.Request("GET", url)
    err = httpx.HTTPStatusError(
        f"Client error '429 Too Many Requests' for url '{url}'",
        request=req, response=httpx.Response(429, request=req))
    with caplog.at_level(logging.WARNING, logger="agentic_fx.price"), \
         patch("agentic_fx.datafeed.price_provider.sources.td_bars",
               side_effect=err):
        with pytest.raises(DataUnhealthy) as ei:
            p.get_bars("USDJPY", "1m", 1)
    assert "SECRET_KEY_123" not in str(ei.value)
    assert "SECRET_KEY_123" not in caplog.text
    assert "429" in str(ei.value) and "429" in caplog.text


def test_secret_is_redacted_from_non_httpx_messages(tmp_path, monkeypatch, caplog):
    """多層防御: httpx 以外の経路で apikey が混入しても伏字にする。"""
    monkeypatch.setenv("TWELVEDATA_API_KEY", "SECRET_KEY_123")
    p = _td_only_provider(tmp_path)
    with caplog.at_level(logging.WARNING, logger="agentic_fx.price"), \
         patch("agentic_fx.datafeed.price_provider.sources.td_quote",
               side_effect=RuntimeError(
                   "boom while calling ...&apikey=SECRET_KEY_123")):
        with pytest.raises(DataUnhealthy) as ei:
            p.get_quote("USDJPY")
    assert "SECRET_KEY_123" not in str(ei.value)
    assert "SECRET_KEY_123" not in caplog.text
    assert "boom" in str(ei.value)          # 自前メッセージの情報量は落とさない


def test_our_own_error_messages_keep_detail(tmp_path):
    """DataUnhealthy/ValueError など自前のメッセージは削らない (診断に必要)。"""
    _, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_gappy_1h_base()):
        with pytest.raises(DataUnhealthy, match="gap"):
            p.get_bars("USDJPY", "4h")


def test_td_skipped_without_api_key(tmp_path, monkeypatch):
    """enabled でも API キーが無ければ試さない (呼べば必ず失敗するため)。"""
    monkeypatch.delenv("TWELVEDATA_API_KEY", raising=False)
    s = load_settings(EXAMPLE).model_copy(deep=True)
    s.datafeed.twelvedata.enabled = True
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    p = PriceProvider(conn, s, FixedClock(NOW))
    yq = Quote("USDJPY", 148.5, 148.5, NOW, "yfinance")
    with patch("agentic_fx.datafeed.price_provider.sources.td_quote") as td, \
         patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               return_value=yq):
        assert p.get_quote("USDJPY").source == "yfinance"
    td.assert_not_called()


def test_yfinance_disabled_is_respected(tmp_path):
    s = load_settings(EXAMPLE).model_copy(deep=True)
    s.datafeed.yfinance.enabled = False
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    p = PriceProvider(conn, s, FixedClock(NOW))
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote") as yq:
        with pytest.raises(DataUnhealthy):
            p.get_quote("USDJPY")
        yq.assert_not_called()


def test_get_bars_derives_4h_from_1h_on_yfinance(tmp_path):
    """yfinance は 4h をネイティブに持たないので 1h から導出する。

    足を固定しない方針のため、4h の要求を拒否せず導出で満たす。
    由来を記録して「ネイティブ 4h」と区別できることも併せて確認する。
    """
    _, p = _provider(tmp_path)   # yfinance のみ enabled
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_fresh_bars(interval="1h", n=100)) as yb:
        bars = p.get_bars("USDJPY", "4h")
    yb.assert_called_once()
    assert yb.call_args.args[1] == "1h"          # 1h を取りに行っている
    assert all(b.interval == "4h" for b in bars)  # 返るのは 4h
    assert "derived" in p.bars_origin("USDJPY", "4h")
    assert p.last_bars_source("USDJPY", "4h") == "yfinance"  # 品質フラグは素の名前


def test_only_base_bars_are_cached_for_derive_only_intervals(tmp_path):
    """導出足は保存せず base 足を保存する (境界がソース依存の足を残さない)。

    ohlcv の PK は (symbol, interval, bar_time) で由来を区別できないため、
    格子の違う 4h が同じ系列に混ざると重複した足を 1 本の系列として返す。
    """
    conn, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_fresh_bars(interval="1h", n=100)):
        bars = p.get_bars("USDJPY", "4h")
    assert bars and all(b.interval == "4h" for b in bars)
    assert ohlcv.load_bars(conn, "USDJPY", "4h") == []      # 導出足は保存しない
    assert len(ohlcv.load_bars(conn, "USDJPY", "1h")) == 100  # base を保存する


def test_get_bars_1d_derives_from_1h_not_4h(tmp_path):
    """1d の base は 1h。4h を base にすると格子問題が裏口から入る。"""
    _, p = _provider(tmp_path, mt5=True)
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_bars",
               return_value=_fresh_bars(interval="1h", n=100)) as mb:
        bars = p.get_bars("USDJPY", "1d")
    assert mb.call_args.args[2] == "1h"
    assert all(b.interval == "1d" for b in bars)
    assert p.bars_origin("USDJPY", "1d") == "mt5(1h→1d derived)"


def test_get_bars_derives_30m_from_15m_on_yfinance(tmp_path):
    """分足の導出 (yfinance に 30m は無い)。pandas の freq alias 写像の実地確認。"""
    _, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_fresh_bars(interval="15m", n=200)) as yb:
        bars = p.get_bars("USDJPY", "30m")
    assert yb.call_args.args[1] == "15m"
    assert all(b.interval == "30m" for b in bars)
    assert all(b.ts.minute % 30 == 0 for b in bars)


def _gappy_1h_base():
    """開場中 (水曜 02:00-06:00 UTC) の 1h 足が 5 本欠けた base 足。

    導出後の 4h では穴がバケット内に吸収されて連続に見える (00-04 バケットは
    00,01 が残り、04-08 バケットは 07 が残るため、4h 側に gap は現れない)。
    base 足の段階で検査しないと素通りする — これが塞ぐべき fail closed の穴。
    """
    dropped = {NOW - timedelta(hours=h) for h in range(6, 11)}
    return [b for b in _fresh_bars(interval="1h", n=100) if b.ts not in dropped]


def test_derive_rejects_source_with_gappy_base_bars(tmp_path):
    """base 足に開場中の欠損があるソースは採用しない (導出足では見えない)。"""
    _, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_gappy_1h_base()):
        with pytest.raises(DataUnhealthy, match="gap"):
            p.get_bars("USDJPY", "4h")


def test_derive_gappy_base_falls_through_to_next_candidate(tmp_path):
    """base 不健全の DataUnhealthy は get_bars ごと落とさずフォールバックに流す。"""
    conn, p = _provider(tmp_path)
    ohlcv.upsert_bars(conn, _fresh_bars(interval="1h", n=100))
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_gappy_1h_base()):
        bars = p.get_bars("USDJPY", "4h")
    assert bars and all(b.interval == "4h" for b in bars)
    assert p.last_bars_source("USDJPY", "4h") == "cache"


def test_derive_accepts_healthy_base_bars(tmp_path):
    """base 足が健全なら従来どおり導出足を返す (非退行)。"""
    _, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_fresh_bars(interval="1h", n=100)):
        bars = p.get_bars("USDJPY", "4h")
    assert bars and all(b.interval == "4h" for b in bars)
    assert p.last_bars_source("USDJPY", "4h") == "yfinance"


def test_native_4h_is_never_requested_even_when_source_has_it(tmp_path):
    """MT5 は 4h をネイティブに持つが要求しない (境界がソース依存のため)。

    実測 (2026-07-28、稼働中の bridge): MT5 ネイティブ H4 の境界は真 UTC の
    21/01/05/09/13/17 時で、1h から epoch 導出した 4h (00/04/08/12/16/20) と
    1 本も時刻を共有しない。混ざると重複した足が 1 本の系列として返る。
    """
    _, p = _provider(tmp_path, mt5=True)
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_bars",
               return_value=_fresh_bars(interval="1h", n=100)) as mb:
        bars = p.get_bars("USDJPY", "4h")
    assert mb.call_args.args[2] == "1h"      # ネイティブ 4h を要求しない
    assert all(b.interval == "4h" for b in bars)
    assert p.bars_origin("USDJPY", "4h") == "mt5(1h→4h derived)"
    assert p.last_bars_source("USDJPY", "4h") == "mt5"   # 品質フラグは素の名前


def test_cache_fallback_derives_4h_from_cached_1h(tmp_path):
    """全ソース失敗時、キャッシュの base 足から導出する (4h の行は読まない)。"""
    conn, p = _provider(tmp_path)
    ohlcv.upsert_bars(conn, _fresh_bars(interval="1h", n=100))
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               side_effect=OSError("down")):
        bars = p.get_bars("USDJPY", "4h")
    assert bars and all(b.interval == "4h" for b in bars)
    assert all(b.ts.hour % 4 == 0 for b in bars)          # epoch 錨の格子
    assert p.last_bars_source("USDJPY", "4h") == "cache"  # 品質フラグは "cache"
    assert p.bars_origin("USDJPY", "4h") == "cache(1h→4h derived)"


def test_cache_fallback_rejects_unhealthy_base(tmp_path):
    """キャッシュの base 足が不健全なら DataUnhealthy (fail closed の非退行)。"""
    conn, p = _provider(tmp_path)
    ohlcv.upsert_bars(conn, _gappy_1h_base())
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               side_effect=OSError("down")):
        with pytest.raises(DataUnhealthy):
            p.get_bars("USDJPY", "4h")


def test_get_bars_rejects_unknown_interval(tmp_path):
    _, p = _provider(tmp_path)
    with pytest.raises(ValueError, match="unknown interval"):
        p.get_bars("USDJPY", "3h")


def test_last_bars_source_tracks_quality_flag(tmp_path):
    conn, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_fresh_bars()):
        p.get_bars("USDJPY", "1m", 1)
    assert p.last_bars_source("USDJPY", "1m") == "yfinance"
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               side_effect=OSError("down")):
        p.get_bars("USDJPY", "1m", 1)  # キャッシュフォールバック
    assert p.last_bars_source("USDJPY", "1m") == "cache"


def test_healthcheck_requires_bars_too(tmp_path):
    _, p = _provider(tmp_path)
    q = Quote("USDJPY", 148.5, 148.5, NOW, "yfinance")
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               return_value=q), \
         patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               side_effect=OSError("down")):
        with pytest.raises(DataUnhealthy):
            p.healthcheck("USDJPY")  # quote 健全でも bars 全滅なら fail closed


def test_healthcheck_returns_quote_source(tmp_path):
    _, p = _provider(tmp_path)
    q = Quote("USDJPY", 148.5, 148.5, NOW, "yfinance")
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               return_value=q), \
         patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_fresh_bars(interval="1h", n=50)):
        assert p.healthcheck("USDJPY") == "yfinance"


def test_healthcheck_covers_all_primary_intervals(tmp_path):
    """primary_intervals を増やせば healthcheck の対象も増える (1h 決め打ちでない)。"""
    s = load_settings(EXAMPLE).model_copy(deep=True)
    s.datafeed.primary_intervals = ["1h", "15m"]
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    p = PriceProvider(conn, s, FixedClock(NOW))
    q = Quote("USDJPY", 148.5, 148.5, NOW, "yfinance")

    def _bars(pair, interval, days):
        if interval == "15m":
            raise OSError("down")
        return _fresh_bars(interval=interval, n=50)

    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               return_value=q), \
         patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               side_effect=_bars):
        with pytest.raises(DataUnhealthy):
            p.healthcheck("USDJPY")


# ---- quote_to_account_rate ------------------------------------------------


def _quote_map(**prices):
    """pair → (bid, ask) を返す yf_quote のスタブを作る。"""
    def _fn(pair):
        if pair not in prices:
            raise OSError(f"no data for {pair}")
        bid, ask = prices[pair]
        return Quote(pair, bid, ask, NOW, "yfinance")
    return _fn


def test_quote_to_account_rate_same_currency_is_one(tmp_path):
    """同一通貨はレート取得を試みない (無用な外部アクセス・失敗点を作らない)。"""
    _, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote") as yq:
        assert p.quote_to_account_rate("JPY", "JPY") == 1.0
    yq.assert_not_called()


def test_quote_to_account_rate_direct_pair(tmp_path):
    """quote=USD / account=JPY → USDJPY の mid をそのまま使う。"""
    _, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               side_effect=_quote_map(USDJPY=(148.0, 149.0))):
        assert p.quote_to_account_rate("USD", "JPY") == pytest.approx(148.5)


def test_quote_to_account_rate_inverse_pair(tmp_path):
    """quote=JPY / account=USD → USDJPY の逆数。"""
    _, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               side_effect=_quote_map(USDJPY=(148.0, 149.0))):
        rate = p.quote_to_account_rate("JPY", "USD")
    assert rate == pytest.approx(1 / 148.5)
    assert rate * 148.5 == pytest.approx(1.0)   # 逆数であることの実測


def test_quote_to_account_rate_via_usd_cross(tmp_path):
    """直接・逆ペアとも無い場合は USD 経由 (EUR→USD→JPY)。"""
    _, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               side_effect=_quote_map(EURUSD=(1.08, 1.08),
                                      USDJPY=(148.0, 149.0))):
        assert p.quote_to_account_rate("EUR", "JPY") == pytest.approx(
            1.08 * 148.5)


def test_quote_to_account_rate_stale_quote_raises(tmp_path):
    """レート用 quote が陳腐なら DataUnhealthy (握りつぶさない)。

    古いレートでのサイジングは無音の過大建玉になる。
    """
    _, p = _provider(tmp_path)
    stale = Quote("USDJPY", 148.0, 149.0, NOW - timedelta(hours=5), "yfinance")
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               return_value=stale):
        with pytest.raises(DataUnhealthy):
            p.quote_to_account_rate("USD", "JPY")


def test_quote_to_account_rate_unknown_symbol_raises(tmp_path):
    """VENDOR_SYMBOLS 未登録の通貨対は DataUnhealthy。"""
    _, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote") as yq:
        with pytest.raises(DataUnhealthy, match="GBP"):
            p.quote_to_account_rate("GBP", "JPY")
    yq.assert_not_called()   # 存在しないシンボルを取りに行かない


def test_quote_to_account_rate_cross_missing_leg_raises(tmp_path):
    """USD 経由の片脚が取れなければ DataUnhealthy (推測で埋めない)。"""
    _, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               side_effect=_quote_map(EURUSD=(1.08, 1.08))):
        with pytest.raises(DataUnhealthy):
            p.quote_to_account_rate("EUR", "JPY")
