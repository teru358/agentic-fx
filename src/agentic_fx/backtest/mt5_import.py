"""MT5 bridge 一括インポータ + 価格系差照合。

MT5 bridge (`GET {base}/ohlcv/{sym}?from=ISO&to=ISO&interval=1m`) から
1 分足を 1 日窓でページングして全取得し、共通の取込の流れ
(`store.history_import`: 事前遮断 → batch 確定) で取り込む。
Dukascopy 等の他 source と重複期間の close 差を照合する。

MT5 は bid 系列 — mid 近似としてそのまま保存する (spec §6 の但し書きどおり。
spread=None)。照合時は `compare_sources` 側で assumed half-spread を引いて
Dukascopy 側 (mid 系列) と揃える。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import quote

import httpx

from agentic_fx.datafeed.price_provider import _SPECS
from agentic_fx.datafeed.closed_bars import normalize_closed_range
from agentic_fx.core.contracts import Bar
from agentic_fx._safe_error import safe_error_text
from agentic_fx.datafeed.sources import (
    BridgeTimeRuleMismatch, BridgeTimeUnverified, _mt5_headers,
    _raise_mt5_time_rule_error)
from agentic_fx.store.history_import import (
    MAX_IMPORT_ROWS, ImportTooLargeError, SeriesFetchError, SeriesOutcome, SeriesSpec,
    empty_outcome, run_series)

_log = logging.getLogger(__name__)


def _too_large_message(start: datetime, end: datetime, detail: str) -> str:
    return (f"import_mt5: {detail} (上限 {MAX_IMPORT_ROWS} 行)。何も書き込んでいません。"
            f"期間を分けて取り込んでください (開始 {start.isoformat()})")


def _default_fetch(url: str) -> dict:
    """既定 fetch。httpx.get + `X-Bridge-Api-Key` ヘッダ + `raise_for_status`。

    bridge の 503 `time_rule_mismatch` / `time_rule_unverified` は型つきの例外
    (`BridgeTimeRuleMismatch` / `BridgeTimeUnverified`) にして上げる。

    Dukascopy と異なり 404 は MT5 bridge では想定外 (欠損日は
    `bars: []` の 200 で返る実測仕様) — 素通しで raise する。
    URL・ヘッダ (API キー) はログに出さない。
    """
    resp = httpx.get(url, headers=_mt5_headers(), timeout=30)
    _raise_mt5_time_rule_error(resp)
    return resp.json()


_INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900}
# ローカル・bridge・MT5 の時計差に対し、grace とは別に確定足 1 本分の余裕を取る。
# 無制限な時計差への保証ではない。
CLOCK_MARGIN_BARS = 1
DEFAULT_GRACE = timedelta(seconds=30)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_BAR_FIELDS = ("time", "open", "high", "low", "close", "volume")


def _is_grid_aligned(dt: datetime, width_sec: int) -> bool:
    return ((dt - datetime(1970, 1, 1, tzinfo=timezone.utc))
            % timedelta(seconds=width_sec) == timedelta(0))


def _floor_grid(dt: datetime, width: timedelta) -> datetime:
    return _EPOCH + ((dt - _EPOCH) // width) * width


def effective_import_end(requested_end: datetime, now: datetime, *,
                         grace: timedelta, interval: str) -> datetime:
    """取り込みの排他的な実効終端 ``min(要求終端, floor_grid(now - grace - 余裕))``。

    ``now`` は呼び出し側が一度だけ読んだ aware 時刻。確定済みでも余裕の
    1 本は保存しない。
    """
    if interval not in _INTERVAL_SECONDS:
        raise ValueError(
            f"interval must be one of {sorted(_INTERVAL_SECONDS)}; got {interval!r}")
    width = timedelta(seconds=_INTERVAL_SECONDS[interval])
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware; got naive datetime")
    now = now.astimezone(timezone.utc)
    margin = width * CLOCK_MARGIN_BARS
    return min(requested_end, _floor_grid(now - grace - margin, width))


def _window_url(base_url: str, symbol: str, start: datetime, end: datetime,
                interval: str = "1m") -> str:
    return (f"{base_url}/ohlcv/{symbol}?from={quote(start.isoformat())}"
            f"&to={quote(end.isoformat())}&interval={interval}")


def _normalize_bar_time(raw: str) -> str:
    """bridge の "time" を UTC の aware isoformat 文字列に正規化する。

    naive な時刻は UTC かどうか判断できないので拒否する (bridge は aware UTC
    を返す)。時刻の表現が行ごとに食い違うと、他 source の行との
    bar_time JOIN が 0 件になる無音故障を起こす。
    """
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        raise ValueError("naive time (timezone required)")
    return dt.astimezone(timezone.utc).isoformat()


def _validated_window_rows(payload, *, symbol: str, interval: str,
                           current: datetime, window_end: datetime,
                           now: datetime, grace: timedelta
                           ) -> tuple[list[tuple], int]:
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

    width_sec = _INTERVAL_SECONDS[interval]
    capacity = int((window_end - current).total_seconds()) // width_sec
    if len(payload["bars"]) > capacity + 1:       # 格子数 + 右端 1 行
        raise ValueError(
            f"import_mt5: bar count {len(payload['bars'])} exceeds window capacity "
            f"{capacity} for interval={interval}")

    normalized = []
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
            bar_time_iso = _normalize_bar_time(bar["time"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"import_mt5: bars[{idx}] time is not parseable or is naive "
                f"(aware time required): {bar['time']!r}") from exc

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

    closed_times = {
        bar.ts for bar in normalize_closed_range(
            [Bar(symbol, interval, bar_dt, o, h, low, close, volume)
             for _bar, bar_dt, _iso, o, h, low, close, volume in remaining],
            interval=interval, start=current, end=window_end,
            cutoff=now, grace=grace)
    }
    rows = [(symbol, interval, bar_time_iso, o, h, low, close, volume, None)
            for _bar, bar_dt, bar_time_iso, o, h, low, close, volume in remaining
            if bar_dt in closed_times]
    return rows, right_edge_count


def _fetch_all_windows(*, symbol, interval, start, end, base_url, fetch, now, grace):
    """全窓を取得・検証して行を返す。DB には触れない。失敗は SeriesFetchError。"""
    rows: list[tuple] = []
    right_edge_drops = 0
    current = start
    while current < end:
        window_end = min(current + timedelta(days=1), end)
        url = _window_url(base_url, symbol, current, window_end, interval)
        try:
            payload = fetch(url)
        except BridgeTimeRuleMismatch as exc:
            raise SeriesFetchError("time_rule_mismatch", error_type=type(exc).__name__)
        except BridgeTimeUnverified as exc:
            raise SeriesFetchError("time_rule_unverified", error_type=type(exc).__name__)
        except Exception as exc:
            raise SeriesFetchError(
                "fetch_failed", safe_error_text(exc), type(exc).__name__) from exc
        try:
            window_rows, window_right_edges = _validated_window_rows(
                payload, symbol=symbol, interval=interval, current=current,
                window_end=window_end, now=now, grace=grace)
        except ValueError as exc:
            raise SeriesFetchError(
                "invalid_response",
                f"failed window [{current.isoformat()}, {window_end.isoformat()}): {exc}",
                type(exc).__name__) from exc
        right_edge_drops += window_right_edges
        rows.extend(window_rows)
        if len(rows) > MAX_IMPORT_ROWS:
            raise SeriesFetchError("too_large")
        current = window_end
    if right_edge_drops > 0:
        # 期待どおりの挙動なので debug。ただし件数は残す — bridge が exclusive へ変われば
        # ここが 0 になり、窓数より大きく増えれば bridge 側の異常を疑える。
        _log.debug(
            "import_mt5: dropped %d right-edge bar(s) at window_end for "
            "symbol=%s (bridge returns \"to\" inclusive; picked up by the "
            "next window)", right_edge_drops, symbol)
    return rows


def import_mt5(conn, symbol: str, start: datetime, end: datetime, *,
               base_url: str, fetch=None, interval: str = "1m",
               clock: Callable[[], datetime] | None = None,
               grace: timedelta = DEFAULT_GRACE, **run_kw) -> SeriesOutcome:
    """MT5 bridge から足を 1 日窓でページングして全取得し、遮断判定のうえ取り込む。

    Args:
        conn: sqlite3 connection (transaction の外)
        symbol: 取引ペア
        start: 開始 (inclusive) — aware UTC 必須
        end: 終了 (exclusive) — aware UTC 必須
        base_url: bridge のベース URL
        fetch: (url: str) -> dict。既定は `_default_fetch` (httpx + raise_for_status)。
        clock: 現在時刻 (aware) を返す関数。処理の最初に 1 回だけ読み、全窓の
            確定判定と実効終端に同じ値を使う。既定は実時計。
        grace: 確定判定の猶予。ライブの closed_bar_grace_sec と同じ値を渡す。
        run_kw: `history_import.run_series` へ渡す (origin / run_id / admission 等)。

    終端: 保存するのは ``start <= t < E`` かつ ``t + 幅 + grace <= T`` の足だけ。
    ``E = effective_import_end(end, T, ...)`` なので未来の ``end`` は切り詰められ、
    確定済みでも直近 1 本分は余裕として保存しない。``start >= E`` なら
    取得も書込みもせず ok の結果 (全件 0) を返す。

    全窓の取得・検証が終わってから、履歴 mutation lease の下で分類・遮断判定・batch 確定を行う。
    取得・検証・遮断・書込みの失敗は例外にせず、返す `SeriesOutcome` の status / failure に
    畳む (確定済みの batch は残る)。窓終端ちょうどの 1 本 (bridge の ``to`` は inclusive) は
    次の窓が取り直すので黙って落とす。

    Raises:
        ValueError: start/end が naive・UTC 以外・格子外・start >= end、interval 不正、
            clock が naive の場合 (何も取得しない)。
        ImportTooLargeError: 期間の格子数が MAX_IMPORT_ROWS を超える場合 (何も取得しない)。
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
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if now.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    now = now.astimezone(timezone.utc)
    requested_end = end
    end = effective_import_end(end, now, grace=grace, interval=interval)

    # 窓ごとの行数は窓の格子数を超えられない (検証済み) ので、期間の格子数で保持行数の上限が決まる。
    if start < end:
        slots = int((end - start).total_seconds()) // width_sec
        if slots > MAX_IMPORT_ROWS:
            raise ImportTooLargeError(_too_large_message(
                start, end, f"期間が最大 {slots} 本分で多すぎます"))
    else:
        _log.info("import_mt5: no confirmed bars to import for symbol=%s "
                  "(effective end %s)", symbol, end.isoformat())

    def fetch_rows() -> list[tuple]:
        return _fetch_all_windows(
            symbol=symbol, interval=interval, start=start, end=end,
            base_url=base_url, fetch=fetch, now=now, grace=grace)

    spec = SeriesSpec("mt5", interval, symbol, start, requested_end,
                      max(start, end), fetch_rows)
    if start >= end:
        return empty_outcome(spec)
    return run_series(conn, spec, now=lambda: now, **run_kw)


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
