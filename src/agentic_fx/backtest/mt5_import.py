"""MT5 bridge 一括インポータ + 価格系差照合。

MT5 bridge (`GET {base}/ohlcv/{sym}?from=ISO&to=ISO&interval=1m`) から
1 分足を 1 日窓でページングして取り込み (`import_bars(source="mt5")`)、
Dukascopy 等の他 source と重複期間の close 差を照合する。

MT5 は bid 系列 — mid 近似としてそのまま保存する (spec §6 の但し書きどおり。
spread=None)。照合時は `compare_sources` 側で assumed half-spread を引いて
Dukascopy 側 (mid 系列) と揃える。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from agentic_fx.datafeed.price_provider import _SPECS
from agentic_fx.datafeed.sources import _mt5_headers
from agentic_fx.store.ohlcv import ImportResult, import_bars

_log = logging.getLogger(__name__)


def _default_fetch(url: str) -> dict:
    """既定 fetch。httpx.get + `X-Bridge-Api-Key` ヘッダ + `raise_for_status`。

    Dukascopy と異なり 404 は MT5 bridge では想定外 (欠損日は
    `bars: []` の 200 で返る実測仕様) — 素通しで raise する。
    URL・ヘッダ (API キー) はログに出さない。
    """
    resp = httpx.get(url, headers=_mt5_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def _window_url(base_url: str, symbol: str, start: datetime, end: datetime) -> str:
    return (f"{base_url}/ohlcv/{symbol}?from={quote(start.isoformat())}"
            f"&to={quote(end.isoformat())}&interval=1m")


def _normalize_bar_time(raw: str) -> str:
    """bridge の "time" は naive ISO の可能性がある。naive なら UTC とみなし
    (実測仕様。§ 上書き 3)、aware isoformat 文字列に正規化する。

    これを怠ると bar_time の文字列表現が Dukascopy 行 ("+00:00" 付き) と
    食い違い、compare_sources の bar_time JOIN が 0 件になる無音故障を
    起こす。
    """
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def import_mt5(conn, symbol: str, start: datetime, end: datetime, *,
               base_url: str, fetch=None) -> ImportResult:
    """MT5 bridge から 1 分足を 1 日窓でページングして取り込む。

    Args:
        conn: sqlite3 connection
        symbol: 取引ペア
        start: 開始 (inclusive) — aware UTC 必須 (naive は ValueError)
        end: 終了 (exclusive) — aware UTC 必須 (naive は ValueError)
        base_url: bridge のベース URL
        fetch: Optional fetch function (url: str) -> dict。既定は
            `_default_fetch` (httpx + raise_for_status)。

    Returns:
        ImportResult (inserted/unchanged/conflicted 合算)。

    Raises:
        ValueError: start/end が naive、または UTC 以外の場合 (正時境界は
            不問 — Task 4 の hour boundary 要件はここには適用しない)。
    """
    for dt, name in [(start, "start"), (end, "end")]:
        if dt.tzinfo is None:
            raise ValueError(
                f"{name} must be timezone-aware; got naive datetime")
        if dt.tzinfo != timezone.utc:
            raise ValueError(f"{name} must be UTC; got {dt.tzinfo}")

    if fetch is None:
        fetch = _default_fetch

    total_inserted = 0
    total_unchanged = 0
    total_conflicted = 0

    current = start
    while current < end:
        window_end = min(current + timedelta(days=1), end)
        url = _window_url(base_url, symbol, current, window_end)
        payload = fetch(url)

        # payload["bars"] を必須で読む (F: sources.py:mt5_bars_range と同じ
        # fail-loud パターン)。.get(..., []) にすると HTTP 200 でエラー body
        # を返す bridge 障害時に「0 件」と区別が付かなくなる。
        rows = [
            (symbol, "1m", _normalize_bar_time(b["time"]),
             float(b["open"]), float(b["high"]), float(b["low"]),
             float(b["close"]), float(b["volume"]), None)
            for b in payload["bars"]
        ]
        if rows:
            result = import_bars(conn, rows, source="mt5")
            total_inserted += result.inserted
            total_unchanged += result.unchanged
            total_conflicted += result.conflicted

        current = window_end

    return ImportResult(total_inserted, total_unchanged, total_conflicted)


def compare_sources(conn, symbol: str, settings, *,
                    a: str = "dukascopy", b: str = "mt5") -> dict:
    """両 source が重複する期間の close 差を集計する (人間 CLI / 報告用)。

    `a` は mid 系列 (Dukascopy) を仮定し、`assumed_half_spread` を引いてから
    `b` (MT5, bid 系列) と比較する。`assumed_half_spread` は
    `settings.risk.pair_rules[symbol].assumed_spread_pips` から
    `_SPECS[symbol].pip_size` で換算する。symbol が pair_rules または _SPECS
    に無い場合は KeyError (文字列推測禁止)。

    Returns:
        {"count", "mean", "std", "max_abs"}。重複期間が 0 件なら
        {"count": 0, "mean": None, "std": None, "max_abs": None}。
    """
    pip_size = _SPECS[symbol].pip_size
    assumed_spread_pips = settings.risk.pair_rules[symbol].assumed_spread_pips
    half_spread = assumed_spread_pips * pip_size / 2

    rows = conn.execute(
        "SELECT ta.close AS a_close, tb.close AS b_close "
        "FROM ohlcv ta JOIN ohlcv tb "
        "ON ta.symbol = tb.symbol AND ta.interval = tb.interval "
        "AND ta.bar_time = tb.bar_time "
        "WHERE ta.symbol = ? AND ta.interval = '1m' "
        "AND ta.source = ? AND tb.source = ?",
        (symbol, a, b)).fetchall()

    diffs = [(r["a_close"] - half_spread) - r["b_close"] for r in rows]
    count = len(diffs)
    if count == 0:
        return {"count": 0, "mean": None, "std": None, "max_abs": None}

    mean = sum(diffs) / count
    if count == 1:
        std = 0.0
    else:
        variance = sum((d - mean) ** 2 for d in diffs) / count
        std = variance ** 0.5
    max_abs = max(abs(d) for d in diffs)
    return {"count": count, "mean": mean, "std": std, "max_abs": max_abs}
