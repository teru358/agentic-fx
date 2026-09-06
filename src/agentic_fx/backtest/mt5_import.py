"""MT5 bridge 一括インポータ + 価格系差照合。

MT5 bridge (`GET {base}/ohlcv/{sym}?from=ISO&to=ISO&interval=1m`) から
1 分足を 1 日窓でページングして取り込み (`import_history_bars(source="mt5")`)、
Dukascopy 等の他 source と重複期間の close 差を照合する。

MT5 は bid 系列 — mid 近似としてそのまま保存する (spec §6 の但し書きどおり。
spread=None)。照合時は `compare_sources` 側で assumed half-spread を引いて
Dukascopy 側 (mid 系列) と揃える。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from agentic_fx.datafeed.price_provider import _SPECS
from agentic_fx.datafeed.sources import _mt5_headers
from agentic_fx.store.ohlcv import ImportResult, import_history_bars

_log = logging.getLogger(__name__)


class ImportConflictError(ValueError):
    """An MT5 window contained values conflicting with immutable history."""

    def __init__(self, conflicts, partial: ImportResult, message=None):
        self.conflicts = conflicts
        self.partial = partial
        super().__init__(message or f"MT5 import conflicts: {len(conflicts)} row(s)")


def _default_fetch(url: str) -> dict:
    """既定 fetch。httpx.get + `X-Bridge-Api-Key` ヘッダ + `raise_for_status`。

    Dukascopy と異なり 404 は MT5 bridge では想定外 (欠損日は
    `bars: []` の 200 で返る実測仕様) — 素通しで raise する。
    URL・ヘッダ (API キー) はログに出さない。
    """
    resp = httpx.get(url, headers=_mt5_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


_INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900}
_BAR_FIELDS = ("time", "open", "high", "low", "close", "volume")


def _is_grid_aligned(dt: datetime, width_sec: int) -> bool:
    return ((dt - datetime(1970, 1, 1, tzinfo=timezone.utc))
            % timedelta(seconds=width_sec) == timedelta(0))


def _window_url(base_url: str, symbol: str, start: datetime, end: datetime,
                interval: str = "1m") -> str:
    return (f"{base_url}/ohlcv/{symbol}?from={quote(start.isoformat())}"
            f"&to={quote(end.isoformat())}&interval={interval}")


def _normalize_bar_time(raw: str) -> tuple[str, bool]:
    """bridge の "time" は naive ISO の可能性がある。naive なら UTC とみなし
    (実測仕様。§ 上書き 3)、aware isoformat 文字列に正規化する。

    これを怠ると bar_time の文字列表現が Dukascopy 行 ("+00:00" 付き) と
    食い違い、compare_sources の bar_time JOIN が 0 件になる無音故障を
    起こす。

    Returns:
        (正規化済み isoformat 文字列, naive だったか)。呼び出し側 (import_mt5)
        は naive 件数を集計し 1 回だけ warning を出す (F2, fix round 1)。
        同じ bridge の時刻系統が実際に破損した実績があり、将来 bridge が
        naive 応答を返し始めたときに無音で UTC 仮定され続けるのを防ぐ。
    """
    dt = datetime.fromisoformat(raw)
    was_naive = dt.tzinfo is None
    if was_naive:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat(), was_naive


def _validated_window_rows(payload, *, symbol: str, interval: str,
                           current: datetime, window_end: datetime
                           ) -> tuple[list[tuple], int, int]:
    """Validate a complete bridge response before returning import rows."""
    if not isinstance(payload, dict):
        raise ValueError("import_mt5: payload must be a dict")
    for field in ("symbol", "interval", "bars"):
        if field not in payload:
            raise ValueError(f"import_mt5: payload missing required {field!r}")
    if not isinstance(payload["bars"], list):
        raise ValueError("import_mt5: payload 'bars' must be a list")
    if payload["symbol"] != symbol:
        raise ValueError(
            f"import_mt5: payload symbol={payload['symbol']!r} does not match "
            f"requested symbol={symbol!r}")
    if payload["interval"] != interval:
        raise ValueError(
            f"import_mt5: payload interval={payload['interval']!r} does not "
            f"match requested interval={interval!r}")

    normalized = []
    naive_count = 0
    for idx, bar in enumerate(payload["bars"]):
        if not isinstance(bar, dict):
            raise ValueError(f"import_mt5: bars[{idx}] must be a dict")
        for field in _BAR_FIELDS:
            if field not in bar:
                raise ValueError(
                    f"import_mt5: bars[{idx}] missing required {field!r}")
        for field in ("open", "high", "low", "close", "volume"):
            value = bar[field]
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value)):
                raise ValueError(
                    f"import_mt5: bars[{idx}] {field} must be a finite number")
        try:
            bar_time_iso, was_naive = _normalize_bar_time(bar["time"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"import_mt5: bars[{idx}] time is not parseable: "
                f"{bar['time']!r}") from exc
        naive_count += int(was_naive)

        o, h, low, close, volume = (
            bar["open"], bar["high"], bar["low"], bar["close"],
            bar["volume"])
        if any(price <= 0 for price in (o, h, low, close)):
            raise ValueError(f"import_mt5: bars[{idx}] prices must be > 0")
        if low > min(o, close):
            raise ValueError(
                f"import_mt5: bars[{idx}] low must be <= min(open, close)")
        if h < max(o, close):
            raise ValueError(
                f"import_mt5: bars[{idx}] high must be >= max(open, close)")
        if volume < 0:
            raise ValueError(f"import_mt5: bars[{idx}] volume must be >= 0")
        normalized.append((bar, datetime.fromisoformat(bar_time_iso),
                           bar_time_iso, o, h, low, close, volume))

    right_edge_count = sum(item[1] == window_end for item in normalized)
    if right_edge_count > 1:
        raise ValueError(
            "import_mt5: more than one right-edge bar at window_end")
    remaining = [item for item in normalized if item[1] != window_end]
    for bar, bar_dt, *_rest in remaining:
        if not (current <= bar_dt < window_end):
            raise ValueError(
                f"import_mt5: symbol={symbol!r} のバー time={bar['time']!r} "
                f"が要求窓 [{current.isoformat()}, {window_end.isoformat()}) "
                "の外です (bridge の不具合の可能性)")

    width_sec = _INTERVAL_SECONDS[interval]
    capacity = int((window_end - current).total_seconds()) // width_sec
    if len(remaining) > capacity:
        raise ValueError(
            f"import_mt5: bar count {len(remaining)} exceeds window capacity "
            f"{capacity} for interval={interval}")
    times = [item[1] for item in remaining]
    if len(set(times)) != len(times):
        raise ValueError("import_mt5: bar times must be unique")
    if any(left >= right for left, right in zip(times, times[1:])):
        raise ValueError("import_mt5: bar times must be strictly increasing")
    for bar_dt in times:
        if not _is_grid_aligned(bar_dt, width_sec):
            raise ValueError(
                f"import_mt5: bar time {bar_dt.isoformat()} is off interval grid")

    rows = [(symbol, interval, bar_time_iso, o, h, low, close, volume, None)
            for _bar, _dt, bar_time_iso, o, h, low, close, volume in remaining]
    return rows, naive_count, right_edge_count


def _conflict_details(conn, rows: list[tuple]) -> list[tuple]:
    conflicts = []
    for symbol, interval, bar_time, o, h, low, close, volume, spread in rows:
        existing = conn.execute(
            "SELECT open, high, low, close, volume, spread FROM ohlcv_history "
            "WHERE source='mt5' AND symbol=? AND interval=? AND bar_time=?",
            (symbol, interval, bar_time)).fetchone()
        if existing is None:
            continue
        existing_values = tuple(existing[name] for name in
                                ("open", "high", "low", "close", "volume",
                                 "spread"))
        incoming_values = (o, h, low, close, volume, spread)
        if any(not (a is None and b is None)
               and (a is None or b is None or abs(a - b) >= 1e-9)
               for a, b in zip(existing_values, incoming_values)):
            conflicts.append((symbol, interval, bar_time, existing_values,
                              incoming_values))
    return conflicts


def import_mt5(conn, symbol: str, start: datetime, end: datetime, *,
               base_url: str, fetch=None, interval: str = "1m") -> ImportResult:
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
            または bridge が返したバーの time が要求窓の外にある場合
            (F5, 最終レビュー codex I2 — fail loud)。**窓終端ちょうどは
            例外にせず黙って落とす** — bridge の `to` は inclusive で
            (実機実測)、その 1 本は次のページング窓が取り直すため欠損しない。
            最終窓の右端だけは落ちるが `end` は exclusive なのでそれが正しい。
    """
    for dt, name in [(start, "start"), (end, "end")]:
        if dt.tzinfo is None:
            raise ValueError(
                f"{name} must be timezone-aware; got naive datetime")
        if dt.tzinfo != timezone.utc:
            raise ValueError(f"{name} must be UTC; got {dt.tzinfo}")
    if interval not in _INTERVAL_SECONDS:
        raise ValueError(
            f"interval must be one of {sorted(_INTERVAL_SECONDS)}; got {interval!r}")
    if start >= end:
        raise ValueError("start must be earlier than end")
    width_sec = _INTERVAL_SECONDS[interval]
    for dt, name in ((start, "start"), (end, "end")):
        if not _is_grid_aligned(dt, width_sec):
            raise ValueError(f"{name} must be aligned to the {interval} grid")

    if fetch is None:
        fetch = _default_fetch

    total_inserted = 0
    total_unchanged = 0
    total_conflicted = 0
    naive_count = 0
    right_edge_drops = 0

    current = start
    while current < end:
        window_end = min(current + timedelta(days=1), end)
        url = _window_url(base_url, symbol, current, window_end, interval)
        payload = fetch(url)
        try:
            rows, window_naive, window_right_edges = _validated_window_rows(
                payload, symbol=symbol, interval=interval, current=current,
                window_end=window_end)
        except ValueError as exc:
            raise ValueError(
                f"import_mt5: failed window [{current.isoformat()}, "
                f"{window_end.isoformat()}); last successful window end="
                f"{current.isoformat()}: {exc}") from exc
        naive_count += window_naive
        right_edge_drops += window_right_edges
        if rows:
            result = import_history_bars(conn, rows, source="mt5")
            total_inserted += result.inserted
            total_unchanged += result.unchanged
            total_conflicted += result.conflicted
            if result.conflicted > 0:
                conflicts = _conflict_details(conn, rows)
                message = None
                if not conflicts:
                    message = (f"MT5 import conflicted={result.conflicted} "
                               "but no detail rows matched")
                partial = ImportResult(
                    total_inserted, total_unchanged, total_conflicted)
                raise ImportConflictError(conflicts, partial, message)

        current = window_end

    if right_edge_drops > 0:
        # 期待どおりの挙動なので debug。ただし**件数は残す** — bridge が
        # exclusive へ変わればここが 0 になり、逆に窓数より大きく増えれば
        # bridge 側の異常を疑える。
        _log.debug(
            "import_mt5: dropped %d right-edge bar(s) at window_end for "
            "symbol=%s (bridge returns \"to\" inclusive; picked up by the "
            "next window)", right_edge_drops, symbol)

    if naive_count > 0:
        # F2 (fix round 1, sonnet): 同じ bridge の時刻系統が実際に破損した
        # 直後であり、将来 bridge が naive 応答を返し始めたときに無音で UTC
        # 仮定され続けるのを防ぐ。毎バーではうるさいので import_mt5 呼び出し
        # 単位で 1 回だけ warning する。
        _log.warning(
            "import_mt5: %d/%d bar(s) had naive \"time\" (assumed UTC) for "
            "symbol=%s — bridge may be returning tz-less timestamps",
            naive_count, total_inserted + total_unchanged + total_conflicted,
            symbol)

    return ImportResult(total_inserted, total_unchanged, total_conflicted)


def compare_sources(conn, symbol: str, settings, *,
                    a: str = "dukascopy", b: str = "mt5",
                    base_interval: str = "1m") -> dict:
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

    cur = conn.execute(
        "SELECT ta.close AS a_close, tb.close AS b_close "
        "FROM ohlcv_history ta JOIN ohlcv_history tb "
        "ON ta.symbol = tb.symbol AND ta.interval = tb.interval "
        "AND ta.bar_time = tb.bar_time "
        "WHERE ta.symbol = ? AND ta.interval = ? "
        "AND ta.source = ? AND tb.source = ?",
        (symbol, base_interval, a, b))

    # F3 (fix round 1, codex Important-1): ストリーミング集計 (Welford 法)。
    # 従来は全行を rows/diffs の 2 本の list に丸ごと実体化していたため、
    # 数年分の 1m 照合ではメモリを不要に食う。カーソルを 1 行ずつ回し
    # count/mean/m2 (分散の中間値)/max_abs を逐次更新する。返り値の契約
    # (count/mean/std/max_abs、count=0→全 None、std は母標準偏差) は不変
    # — count=1 のとき Welford は m2=0.0 を厳密に生成するため std=0.0 も
    # 特別扱い無しで成立する (数学的に等価)。
    count = 0
    mean = 0.0
    m2 = 0.0
    max_abs = 0.0
    for r in cur:
        d = (r["a_close"] - half_spread) - r["b_close"]
        count += 1
        delta = d - mean
        mean += delta / count
        m2 += delta * (d - mean)
        ad = abs(d)
        if ad > max_abs:
            max_abs = ad

    if count == 0:
        return {"count": 0, "mean": None, "std": None, "max_abs": None}

    std = (m2 / count) ** 0.5
    return {"count": count, "mean": mean, "std": std, "max_abs": max_abs}
