from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from agentic_fx.core.contracts import Bar

_log = logging.getLogger("agentic_fx.store.ohlcv")

_FLOAT_TOL = 1e-9

# ライブチェーンの永続化名のみ。"mt5" ではなく "mt5-live"
# (datafeed/price_provider.py の _STORAGE_SOURCE 参照)。
LIVE_SOURCES = frozenset({"yfinance", "twelvedata", "mt5-live"})

# 一括インポータの source 名のみ (backtest/importer.py / backtest/mt5_import.py)。
IMPORT_SOURCES = frozenset({"dukascopy", "mt5"})

# プラン 9 Task 16: KNOWN_OHLCV_SOURCES は両集合の union として定義する
# (以前は独立したリテラルだったが、LIVE_SOURCES/IMPORT_SOURCES を単一の
# 真実の源にする — service.py:_validate_startup の producer_source typo
# 検出が参照する)。
KNOWN_OHLCV_SOURCES = LIVE_SOURCES | IMPORT_SOURCES


def _iso_utc(ts: datetime) -> str:
    """aware datetime を UTC へ正規化してから isoformat する。naive は
    ValueError (fail closed)。"""
    if ts.tzinfo is None:
        raise ValueError(
            "_iso_utc: naive datetime is rejected (tz-aware UTC required)")
    return ts.astimezone(timezone.utc).isoformat()


def _require_aware_utc(dt: datetime, label: str) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"{label} is naive; tz-aware UTC datetime required "
                          "(cannot safely assume UTC)")
    return dt.astimezone(timezone.utc)


# ---- キャッシュ (ohlcv_cache) — ライブ逐次専用 ------------------------------


def upsert_cache_bars(conn: sqlite3.Connection, bars: list[Bar], *,
                      source: str) -> int:
    """live キャッシュ用。形成中バーの更新があるため常に上書きする。

    `source` は `LIVE_SOURCES` のいずれかでなければならない — 一括インポート
    由来の source (例: "dukascopy") をここから書くと `ohlcv_history` の
    「既存行不変」契約を迂回できてしまうテーブル越境が生じるため
    (テーブルが分かれているので実際には書き込み自体が別テーブルへ向かうが、
    source の取り違えという設定ミスを早期に検出する)。
    """
    if source not in LIVE_SOURCES:
        raise ValueError(
            f"upsert_cache_bars: source={source!r} は LIVE_SOURCES "
            f"{sorted(LIVE_SOURCES)} に含まれません "
            "(一括インポート由来の source は import_history_bars を使うこと)")
    conn.executemany(
        "INSERT INTO ohlcv_cache (symbol, interval, bar_time, open, high, "
        "low, close, volume, source) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol, interval, bar_time, source) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, "
        "close=excluded.close, volume=excluded.volume",
        [(b.symbol, b.interval, _iso_utc(b.ts), b.open, b.high, b.low,
          b.close, b.volume, source) for b in bars])
    conn.commit()
    return len(bars)


def load_cache_bars(conn: sqlite3.Connection, symbol: str, interval: str, *,
                    source: str, since: datetime | None = None,
                    until: datetime | None = None) -> list[Bar]:
    if source not in LIVE_SOURCES:
        raise ValueError(
            f"load_cache_bars: source={source!r} は LIVE_SOURCES "
            f"{sorted(LIVE_SOURCES)} に含まれません")
    q = "SELECT * FROM ohlcv_cache WHERE symbol=? AND interval=? AND source=?"
    args: list = [symbol, interval, source]
    if since is not None:
        since_utc = _require_aware_utc(since, "since")
        q += " AND bar_time >= ?"
        args.append(since_utc.isoformat())
    if until is not None:
        until_utc = _require_aware_utc(until, "until")
        q += " AND bar_time <= ?"
        args.append(until_utc.isoformat())
    rows = conn.execute(q + " ORDER BY bar_time", args).fetchall()
    return [Bar(r["symbol"], r["interval"],
                datetime.fromisoformat(r["bar_time"]),
                r["open"], r["high"], r["low"], r["close"], r["volume"])
            for r in rows]


def prune_cache(conn: sqlite3.Connection, *, cutoff: datetime,
                limit: int) -> int:
    """`ohlcv_cache` の保持ポリシー。`bar_time < cutoff` の行を最大 `limit`
    件だけ削除する (有界バッチ — core_lock 保持窓を無制限に伸ばさないため)。

    `DELETE FROM ohlcv_cache` のみで完結する — source 絞りも「未知 source
    は消さない」fail-safe 分岐も**不要** (`ohlcv_history` はテーブルが
    違うため、この DELETE は原理的に到達できない)。呼び出し側 (maintenance
    hook) が毎 tick 呼ぶことで、有界バッチのまま日次増分に追いつく
    (設計書 D2「追いつくまで毎 maintenance」)。

    削除件数を返す (呼び出し側は使わなくてよいが、テスト・ログで有用)。
    """
    cutoff_utc = _require_aware_utc(cutoff, "cutoff")
    if limit < 1:
        raise ValueError(f"prune_cache: limit must be >= 1, got {limit}")
    cur = conn.execute(
        "DELETE FROM ohlcv_cache WHERE rowid IN ("
        "SELECT rowid FROM ohlcv_cache WHERE bar_time < ? LIMIT ?)",
        (cutoff_utc.isoformat(), limit))
    conn.commit()
    return cur.rowcount


# ---- 履歴 (ohlcv_history) — バックテスト・一括インポート専用 ----------------


@dataclass(frozen=True)
class ImportResult:
    inserted: int
    unchanged: int
    conflicted: int


def _close_enough(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < _FLOAT_TOL


def _validate_and_normalize_row(idx: int, row: tuple) -> tuple:
    """投入前検証 (import_history_bars 用)。

    - 有限値であること (NaN/inf を弾く)
    - `low <= min(open, close) <= max(open, close) <= high`
    - `bar_time` は aware datetime として parse できること (UTC へ正規化
      してから isoformat した文字列に置き換えて返す)

    不正行はここで ValueError を送出する。呼び出し側は**全行をここで
    検証してから**書き込みを始める。
    """
    if len(row) != 9:
        raise ValueError(
            f"import_history_bars: rows[{idx}] has {len(row)} fields, "
            "expected 9 (symbol, interval, bar_time_iso, o, h, l, c, "
            "volume, spread)")
    symbol, interval, bar_time_iso, o, h, l, c, volume, spread = row  # noqa: E741
    if not symbol or not interval:
        raise ValueError(
            f"import_history_bars: rows[{idx}] has empty symbol/interval")
    try:
        dt = datetime.fromisoformat(bar_time_iso)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"import_history_bars: rows[{idx}] bar_time not parseable: "
            f"{bar_time_iso!r}") from e
    if dt.tzinfo is None:
        raise ValueError(
            f"import_history_bars: rows[{idx}] bar_time is naive (aware "
            f"datetime required): {bar_time_iso!r}")
    normalized_bar_time = dt.astimezone(timezone.utc).isoformat()
    for name, val in (("open", o), ("high", h), ("low", l), ("close", c),
                      ("volume", volume)):
        if not isinstance(val, (int, float)) or isinstance(val, bool) \
                or not math.isfinite(val):
            raise ValueError(
                f"import_history_bars: rows[{idx}] {name} is not a finite "
                f"number: {val!r}")
    if spread is not None:
        if not isinstance(spread, (int, float)) or isinstance(spread, bool) \
                or not math.isfinite(spread):
            raise ValueError(
                f"import_history_bars: rows[{idx}] spread is not a finite "
                f"number: {spread!r}")
    lo_bound, hi_bound = min(o, c), max(o, c)
    if not (l <= lo_bound <= hi_bound <= h):
        raise ValueError(
            f"import_history_bars: rows[{idx}] violates low<=min(open,close)"
            f"<=max(open,close)<=high: open={o} high={h} low={l} close={c}")
    return (symbol, interval, normalized_bar_time, o, h, l, c, volume, spread)


def import_history_bars(conn: sqlite3.Connection, rows: list[tuple], *,
                        source: str) -> ImportResult:
    """一括インポータ用。既存行は不変 — 同一キー同一値は無変更 (unchanged)、
    値が異なる既存行は入力をスキップして件数だけ計上する (conflicted)。

    rows: (symbol, interval, bar_time_iso, o, h, l, c, volume, spread|None)

    `source` は `IMPORT_SOURCES` のいずれかでなければならない — ライブ
    source 名 (例: "yfinance") をここから書くと `ohlcv_history` の
    「削除しない」保証をライブキャッシュの上書きセマンティクスで穢すため。

    バッチ原子性: (1) 書き込み前に全行を検証・正規化し、(2) 実際の書き込みは
    `SAVEPOINT` で明示的に囲み、例外時は `ROLLBACK TO SAVEPOINT` してから
    再送出する。
    """
    if source not in IMPORT_SOURCES:
        raise ValueError(
            f"import_history_bars: source={source!r} は IMPORT_SOURCES "
            f"{sorted(IMPORT_SOURCES)} に含まれません "
            "(ライブ由来の source は upsert_cache_bars を使うこと)")
    normalized_rows = [_validate_and_normalize_row(i, row)
                       for i, row in enumerate(rows)]

    inserted = 0
    unchanged = 0
    conflicted = 0
    conn.execute("SAVEPOINT import_history_bars")
    try:
        # 行単位ループは意図的 — inserted/unchanged/conflicted の三分計上は
        # 集合演算 (executemany + 一括 diff) では再現できない。migration 側を
        # 集合演算にしたのはロック保持時間の制約によるもので、ここはプロファイル
        # が別 (前景・窓分割済み・SAVEPOINT 直後に commit)。1 コールで 10 万行
        # 超を流す呼び出し元が生まれたら再評価する (実測 10 万行 0.33s)。
        for (symbol, interval, bar_time_iso, o, h, l, c, volume,  # noqa: E741
             spread) in normalized_rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO ohlcv_history (symbol, interval, "
                "bar_time, open, high, low, close, volume, source, spread) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (symbol, interval, bar_time_iso, o, h, l, c, volume, source,
                 spread))
            if cur.rowcount == 1:
                inserted += 1
                continue
            existing = conn.execute(
                "SELECT open, high, low, close, volume, spread FROM "
                "ohlcv_history WHERE symbol=? AND interval=? AND bar_time=? "
                "AND source=?",
                (symbol, interval, bar_time_iso, source)).fetchone()
            same = (
                _close_enough(existing["open"], o)
                and _close_enough(existing["high"], h)
                and _close_enough(existing["low"], l)
                and _close_enough(existing["close"], c)
                and _close_enough(existing["volume"], volume)
                and _close_enough(existing["spread"], spread)
            )
            if same:
                unchanged += 1
            else:
                conflicted += 1
        conn.execute("RELEASE import_history_bars")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT import_history_bars")
        conn.execute("RELEASE import_history_bars")
        raise
    conn.commit()
    if conflicted > 0:
        _log.warning(
            "import_history_bars: %d row(s) conflicted with existing data "
            "for source=%s (existing rows left unchanged)",
            conflicted, source)
    return ImportResult(inserted, unchanged, conflicted)


def load_history_bars(conn: sqlite3.Connection, symbol: str, interval: str,
                      *, source: str, since: datetime | None = None,
                      until: datetime | None = None) -> list[Bar]:
    if source not in IMPORT_SOURCES:
        raise ValueError(
            f"load_history_bars: source={source!r} は IMPORT_SOURCES "
            f"{sorted(IMPORT_SOURCES)} に含まれません "
            "(履歴はバックテスト・分析専用 — ライブ source は "
            "load_cache_bars を使うこと)")
    q = ("SELECT * FROM ohlcv_history WHERE symbol=? AND interval=? "
        "AND source=?")
    args: list = [symbol, interval, source]
    if since is not None:
        since_utc = _require_aware_utc(since, "since")
        q += " AND bar_time >= ?"
        args.append(since_utc.isoformat())
    if until is not None:
        until_utc = _require_aware_utc(until, "until")
        q += " AND bar_time <= ?"
        args.append(until_utc.isoformat())
    rows = conn.execute(q + " ORDER BY bar_time", args).fetchall()
    return [Bar(r["symbol"], r["interval"],
                datetime.fromisoformat(r["bar_time"]),
                r["open"], r["high"], r["low"], r["close"], r["volume"])
            for r in rows]


def load_history_spread(conn: sqlite3.Connection, symbol: str, interval: str,
                        bar_time_iso: str, *, source: str) -> float | None:
    if source not in IMPORT_SOURCES:
        raise ValueError(
            f"load_history_spread: source={source!r} は IMPORT_SOURCES "
            f"{sorted(IMPORT_SOURCES)} に含まれません")
    row = conn.execute(
        "SELECT spread FROM ohlcv_history WHERE symbol=? AND interval=? "
        "AND bar_time=? AND source=?",
        (symbol, interval, bar_time_iso, source)).fetchone()
    return row["spread"] if row is not None else None
