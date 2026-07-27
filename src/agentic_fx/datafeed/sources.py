"""ソース別 fetcher (yfinance / MT5 bridge / Twelve Data)。例外は送出、選択は provider。

論理シンボル → vendor 別シンボルは VENDOR_SYMBOLS で明示解決する
(関連指標 — DXY 等 — の追加はこの表への行追加、設計書 §5)。"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd
import yfinance

from agentic_fx.core.contracts import Bar, Quote

INTERVAL_MIN: dict[str, float] = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}

# _TD_INTERVAL は NATIVE_INTERVALS より先に定義すること (後者が参照する)
_TD_INTERVAL = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
                "1h": "1h", "4h": "4h", "1d": "1day"}
_TD_MAX_OUTPUTSIZE = 5000

# ソース毎のネイティブ対応足。取引の時間軸は固定しないため、足の可否は
# 「システムの仕様」ではなく「ソースの能力」として持つ。ネイティブに無い
# 足は PriceProvider がより細かい足から resample で導出する。
NATIVE_INTERVALS: dict[str, frozenset[str]] = {
    "mt5": frozenset({"1m", "5m", "15m", "30m", "1h", "4h", "1d"}),
    "twelvedata": frozenset(_TD_INTERVAL),
    "yfinance": frozenset({"1m", "5m", "15m", "1h", "1d"}),
}

VENDOR_SYMBOLS: dict[str, dict[str, str]] = {
    "USDJPY": {"yf": "USDJPY=X", "td": "USD/JPY", "mt5": "USDJPY"},
    "EURUSD": {"yf": "EURUSD=X", "td": "EUR/USD", "mt5": "EURUSD"},
    # 関連指標の拡張例 (Phase 1 では未使用):
    "DXY": {"yf": "DX-Y.NYB", "td": "DXY"},
}


def vendor_symbol(logical: str, vendor: str) -> str:
    return VENDOR_SYMBOLS[logical][vendor]


def _flatten_yf_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    return df


def yf_bars(pair: str, interval: str, lookback_days: int) -> list[Bar]:
    df = yfinance.download(
        vendor_symbol(pair, "yf"), interval=interval,
        period=f"{lookback_days}d", progress=False, auto_adjust=False,
        multi_level_index=False)
    df = _flatten_yf_columns(df)
    bars: list[Bar] = []
    for ts, row in df.iterrows():
        t = ts.to_pydatetime()
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        volume = row["Volume"] if "Volume" in row else 0.0
        bars.append(Bar(pair, interval, t, float(row["Open"]),
                        float(row["High"]), float(row["Low"]),
                        float(row["Close"]), float(volume or 0)))
    return bars


def yf_quote(pair: str) -> Quote:
    bars = yf_bars(pair, "1m", 1)
    if not bars:
        raise RuntimeError(f"yfinance returned no data for {pair}")
    last = bars[-1]
    return Quote(pair, last.close, last.close, last.ts, "yfinance")


def _mt5_headers() -> dict[str, str]:
    """bridge の API キー。auth_required が偽なら未設定でも通る。"""
    key = os.environ.get("MT5_BRIDGE_API_KEY")
    return {"X-Bridge-Api-Key": key} if key else {}


def mt5_quote(bridge_url: str, pair: str) -> Quote:
    # symbol はパスパラメータ (クエリではない — 実 bridge 仕様)
    sym = vendor_symbol(pair, "mt5")
    r = httpx.get(f"{bridge_url}/quote/{sym}",
                  headers=_mt5_headers(), timeout=10)
    r.raise_for_status()
    d = r.json()   # {symbol, bid, ask, spread_points, time}
    return Quote(pair, float(d["bid"]), float(d["ask"]),
                 datetime.fromisoformat(d["time"]), "mt5")


def mt5_bars_range(bridge_url: str, pair: str, interval: str,
                   start: datetime, end: datetime) -> list[Bar]:
    """期間指定でバーを取る (bridge の本来の形。copy_rates_range ベース)。

    Phase 2 の長期 backfill もこの関数をそのまま使う。
    """
    sym = vendor_symbol(pair, "mt5")
    r = httpx.get(f"{bridge_url}/ohlcv/{sym}",
                  params={"from": start.isoformat(), "to": end.isoformat(),
                          "interval": interval},
                  headers=_mt5_headers(), timeout=30)
    r.raise_for_status()
    payload = r.json()          # {symbol, interval, bars: [...]}
    # bar の出来高キーは "volume" (bridge が MT5 の tick_volume を変換済み)。
    # "tick_volume" を読むと全バーが 0 になる
    return [Bar(pair, interval, datetime.fromisoformat(x["time"]),
                float(x["open"]), float(x["high"]), float(x["low"]),
                float(x["close"]), float(x["volume"]))
            for x in payload["bars"]]


def mt5_bars(bridge_url: str, pair: str, interval: str,
             lookback_days: int) -> list[Bar]:
    """lookback_days 形式の薄いラッパー (他ソースと同じ呼び口を保つため)。"""
    end = datetime.now(timezone.utc)
    return mt5_bars_range(bridge_url, pair, interval,
                          end - timedelta(days=lookback_days), end)


def td_quote(api_key: str, pair: str) -> Quote:
    r = httpx.get("https://api.twelvedata.com/quote",
                  params={"symbol": vendor_symbol(pair, "td"),
                          "apikey": api_key}, timeout=10)
    r.raise_for_status()
    d = r.json()
    ts = datetime.fromtimestamp(int(d["timestamp"]), tz=timezone.utc)
    return Quote(pair, float(d["bid"]), float(d["ask"]), ts, "twelvedata")


def td_bars(api_key: str, pair: str, interval: str,
            lookback_days: int) -> list[Bar]:
    size = min(_TD_MAX_OUTPUTSIZE,
               int(lookback_days * 1440 / INTERVAL_MIN[interval]))
    r = httpx.get("https://api.twelvedata.com/time_series",
                  params={"symbol": vendor_symbol(pair, "td"),
                          "interval": _TD_INTERVAL[interval],
                          "outputsize": size, "apikey": api_key}, timeout=30)
    r.raise_for_status()
    values = r.json().get("values", [])
    bars = [Bar(pair, interval,
                datetime.fromisoformat(v["datetime"]).replace(
                    tzinfo=timezone.utc),
                float(v["open"]), float(v["high"]), float(v["low"]),
                float(v["close"]), 0.0)
            for v in values]
    bars.sort(key=lambda b: b.ts)
    return bars
