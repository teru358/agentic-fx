"""Dukascopy importer — fetch hourly tick data, aggregate to 1-minute bars, store."""
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from agentic_fx.backtest.dukascopy import Tick, decode_bi5, hour_url, point_of
from agentic_fx.store.ohlcv import ImportResult, import_history_bars

_log = logging.getLogger(__name__)

# ---- 応急封鎖 (2026-08-12) — 外向きリクエストの抑制 ------------------------
# **設計の欠陥への暫定対処。** 設計書は Dukascopy を「長期 1m の一次ソース
# (無料公開、10 年超)」と位置づけている (agentic-fx-design.md §データ) 一方で、
# **外向きリクエストの予算という制約クラスが設計に存在しない**。この関数は
# 1 時間 1 リクエストを間隔なしで連射するため、設計が想定する 10 年分の取得は
# 約 87,600 リクエストになる。実際にこの開発機は Dukascopy に遮断された
# (別 IP では同一 URL が通ることを対照実験で確認済み)。
# **リポジトリは公開済みであり、クローンした人が同じ目に遭う** — 他人に損害を
# 与える不具合なので、本設計 (第三者向け出口の集約: 間隔・バックオフ・総量上限・
# 素性を名乗る UA) が入るまでの応急封鎖としてここに最小限を置く。
_MIN_INTERVAL_SEC = 1.0     # 連続リクエストの最小間隔 (無料公開データへの礼儀)
_MAX_REQUESTS = 500         # 1 回の呼び出しで投げる既定上限 (≈ 3 週間分)
_THROTTLE_STATUS = (429, 503)   # 実測: UA 無しで 429、UA 付きで 503


def _default_fetch(url: str) -> bytes:
    """Default fetch implementation using httpx.

    Returns b"" for 404 errors, raises HTTPStatusError for other HTTP errors,
    and raises for network errors.
    """
    try:
        resp = httpx.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return b""
        raise


def ticks_to_1m(ticks: list[Tick], symbol: str) -> list[tuple]:
    """Aggregate ticks to 1-minute bars.

    Converts mid=(bid+ask)/2 prices to OHLC, calculates mean spread,
    and returns import_history_bars row tuples.

    Args:
        ticks: List of Tick(ts, bid, ask) objects
        symbol: Trading pair symbol

    Returns:
        List of tuples: (symbol, "1m", bar_time_iso, o, h, l, c, volume, spread)
        One row per minute, sorted by bar_time.
    """
    if not ticks:
        return []

    # F3: Defensive sort by timestamp (open/close depend on input order)
    ticks = sorted(ticks, key=lambda t: t.ts)

    # Group ticks by minute (hour and minute components)
    minute_groups = {}
    for tick in ticks:
        # Key: (year, month, day, hour, minute) to group by minute
        key = (tick.ts.year, tick.ts.month, tick.ts.day, tick.ts.hour, tick.ts.minute)
        if key not in minute_groups:
            minute_groups[key] = []
        minute_groups[key].append(tick)

    # Sort keys to maintain chronological order
    sorted_keys = sorted(minute_groups.keys())
    rows = []

    for key in sorted_keys:
        ticks_in_minute = minute_groups[key]

        # Calculate mid price for each tick
        mids = [(tick.bid + tick.ask) / 2.0 for tick in ticks_in_minute]

        # OHLC from mid prices
        o = mids[0]  # open: first mid
        h = max(mids)  # high: max mid
        l = min(mids)  # low: min mid
        c = mids[-1]  # close: last mid

        # Volume: number of ticks
        v = len(ticks_in_minute)

        # Mean spread: average of (ask - bid) for all ticks
        spreads = [tick.ask - tick.bid for tick in ticks_in_minute]
        mean_spread = sum(spreads) / len(spreads)

        # Bar timestamp: the minute boundary (first tick's hour:minute)
        bar_ts = ticks_in_minute[0].ts.replace(second=0, microsecond=0)

        # Row tuple: (symbol, interval, bar_time_iso, o, h, l, c, volume, spread)
        row = (symbol, "1m", bar_ts.isoformat(), o, h, l, c, v, mean_spread)
        rows.append(row)

    return rows


def import_dukascopy(conn, symbol: str, start: datetime, end: datetime, *,
                     fetch=None, progress=None, sleep=None,
                     max_requests: int = _MAX_REQUESTS) -> ImportResult:
    """Import Dukascopy tick data and aggregate to 1-minute bars.

    Fetches hourly tick data files from Dukascopy, decodes them, aggregates
    to 1-minute OHLC bars, and imports into database. Empty hours (404 or
    empty payload) are skipped.

    Args:
        conn: sqlite3 connection
        symbol: Trading pair symbol
        start: Start time (inclusive) — aware UTC datetime on hour boundary
        end: End time (exclusive) — aware UTC datetime on hour boundary
        fetch: Optional fetch function (url: str) -> bytes. Defaults to _default_fetch.
               404 errors return empty bytes, other errors raise.
        progress: Optional callback (url_or_time) -> None. Called for each hour processed.
        sleep: Optional sleep function (seconds: float) -> None. Defaults to
               time.sleep. 連続リクエストの**間**にのみ挟む (先頭の前には入れない)。
        max_requests: この呼び出しで投げる上限 (既定 500 ≈ 3 週間分)。
               長期取得は**明示的に引き上げて意図を表明する**。

    Returns:
        ImportResult with inserted, unchanged, conflicted counts.

    Raises:
        ValueError: start/end が naive・非 UTC・正時境界でない場合。
            または要求範囲が `max_requests` を超える場合 (**1 本も投げずに拒否**)。
        RuntimeError: 相手が 429/503 を返した場合。**再試行せず即座に中止**する
            — 既に絞られている相手に再試行を重ねると状況を悪化させる
            (リトライ/バックオフの設計は本設計の担当)。
    """
    # F2: Validate start/end are aware UTC and on hour boundary
    for dt, name in [(start, "start"), (end, "end")]:
        if dt.tzinfo is None:
            raise ValueError(f"{name} must be timezone-aware; got naive datetime")
        if dt.tzinfo != timezone.utc:
            raise ValueError(f"{name} must be UTC; got {dt.tzinfo}")
        if dt.minute != 0 or dt.second != 0 or dt.microsecond != 0:
            raise ValueError(
                f"{name} must be on hour boundary (minute=0, second=0, microsecond=0); "
                f"got {dt.isoformat()}")

    if fetch is None:
        fetch = _default_fetch
    if sleep is None:
        sleep = time.sleep

    # **1 本も投げる前に**範囲を検査する。投げてから気付いても遅い。
    planned = int((end - start).total_seconds() // 3600) if end > start else 0
    if planned > max_requests:
        raise ValueError(
            f"import_dukascopy: 要求範囲が {planned} リクエストになり "
            f"max_requests={max_requests} を超えます。範囲を分けるか、"
            f"意図的なら max_requests を明示的に引き上げてください "
            f"(相手は無料公開サービスです — {_MIN_INTERVAL_SEC}s 間隔で "
            f"{planned} 本なら約 {planned * _MIN_INTERVAL_SEC / 60:.0f} 分かかります)")

    # Get point value for the symbol
    point = point_of(symbol)

    # Initialize ImportResult counters
    total_inserted = 0
    total_unchanged = 0
    total_conflicted = 0
    sent = 0

    # Iterate through hours in [start, end)
    current = start
    while current < end:
        # Progress callback
        if progress is not None:
            progress(current)

        # Fetch hourly data
        url = hour_url(symbol, current)
        if sent > 0:
            # 連続リクエストの**間**にのみ挟む (先頭の前に待つ意味は無い)
            sleep(_MIN_INTERVAL_SEC)
        try:
            payload = fetch(url)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in _THROTTLE_STATUS:
                raise RuntimeError(
                    f"import_dukascopy: HTTP {e.response.status_code} — "
                    "相手にリクエストを絞られています。**再試行しません**。"
                    f"{sent} 本目で中止しました。時間を空けてから、範囲を狭めて "
                    "再開してください (連射すると遮断が長引きます)") from e
            raise
        sent += 1

        # Skip empty payloads (404, no data) — only legitimate skip reasons
        if not payload:
            current += timedelta(hours=1)
            continue

        # F1: Decode bi5 payload — exceptions propagate (corrupt data should fail, not silently skip)
        ticks = decode_bi5(payload, point=point, hour_start_utc=current)

        # Skip if no valid ticks (all records rejected or incomplete)
        if not ticks:
            current += timedelta(hours=1)
            continue

        # Aggregate to 1-minute bars
        rows = ticks_to_1m(ticks, symbol)

        # Import bars into database
        if rows:
            result = import_history_bars(conn, rows, source="dukascopy")
            total_inserted += result.inserted
            total_unchanged += result.unchanged
            total_conflicted += result.conflicted

        current += timedelta(hours=1)

    return ImportResult(total_inserted, total_unchanged, total_conflicted)
