from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from agentic_fx.core.contracts import Bar

_log = logging.getLogger("agentic_fx.store.ohlcv")

_FLOAT_TOL = 1e-9

# F5 (fix round 1, codex+sonnet Important): upsert_bars は live キャッシュ
# 専用の書き込み口。live source 以外 (例: 一括インポータの "dukascopy") を
# ここから書けてしまうと import_bars が保証する「既存行不変」を上書きで
# 素通りできてしまう。live/import の境界を型ではなく API 契約 (allowlist)
# で強制する。"mt5" ではなく "mt5-live" (price_provider の永続化用 source ID
# — datafeed/price_provider.py の _STORAGE_SOURCE 参照)。
LIVE_SOURCES = frozenset({"yfinance", "twelvedata", "mt5-live"})

# プラン 8 B 束: ohlcv.source 列に実際に書き込まれる値の正規列挙
# (price_provider.py:_STORAGE_SOURCE / backtest/importer.py /
# backtest/mt5_import.py / backtest/analysis.py:ANALYSIS_SOURCE /
# plugin/approval.py:_EVAL_SOURCE の実値を集約)。起動時の
# producer_source typo 検出 (service.py:_validate_startup) が参照する。
KNOWN_OHLCV_SOURCES = frozenset(
    {"yfinance", "mt5", "mt5-live", "twelvedata", "dukascopy"})


def _iso_utc(ts: datetime) -> str:
    """aware datetime を UTC へ正規化してから isoformat する。naive は
    ValueError (fail closed)。

    fix round 1 advisor 指摘: import_bars 側 (F4) は bar_time を UTC に
    正規化するのに upsert_bars 側は `b.ts.isoformat()` のまま非対称だった。
    `load_bars`/`import_bars` の bar_time 比較・`ORDER BY bar_time` は
    文字列の辞書順比較なので、offset が混在すると窓 (`since`/`until`) や
    順序が静かに壊れる。現状 `datafeed/sources.py` の全ソース (`_to_utc`)
    は `Bar.ts` を必ず UTC aware で返す (実測で確認済み) ため実害は無いが、
    対称性を保ち将来のソース追加でも壊れないようにする。

    F7 (最終レビュー codex Minor2): 当初は naive をそのまま通していたが
    (「防御的に勝手な tz 付与はしない」という判断)、これでは
    live 書き込み口 (`upsert_bars`) だけ契約が弱いまま残る —
    `import_bars`/`ReplayClock` は naive を fail closed 済み。ここも同じ
    規律に揃え、naive は ValueError で拒否する。
    """
    if ts.tzinfo is None:
        raise ValueError(
            "_iso_utc: naive datetime is rejected (tz-aware UTC required)")
    return ts.astimezone(timezone.utc).isoformat()


def upsert_bars(conn: sqlite3.Connection, bars: list[Bar], *,
                source: str) -> int:
    """live キャッシュ用。形成中バーの更新があるため常に上書きする
    (既存行不変の import_bars とは別契約 — spec §6 の live 例外)。

    `source` は live allowlist (`LIVE_SOURCES`) のいずれかでなければならない
    (F5)。一括インポート由来の source (例: "dukascopy") をここから書くと
    import_bars が保証する「既存行不変」を上書きで破れてしまうため。
    """
    if source not in LIVE_SOURCES:
        raise ValueError(
            f"upsert_bars: source={source!r} は live allowlist "
            f"{sorted(LIVE_SOURCES)} に含まれません "
            "(一括インポート由来の source は import_bars を使うこと)")
    conn.executemany(
        "INSERT INTO ohlcv (symbol, interval, bar_time, open, high, low, "
        "close, volume, source) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol, interval, bar_time, source) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, "
        "close=excluded.close, volume=excluded.volume",
        [(b.symbol, b.interval, _iso_utc(b.ts), b.open, b.high, b.low,
          b.close, b.volume, source) for b in bars])
    conn.commit()
    return len(bars)


def _require_aware_utc(dt: datetime, label: str) -> datetime:
    """F6 (最終レビュー opus M-2 = codex Minor1): naive は ValueError、aware
    は UTC へ正規化する。DB の bar_time は UTC ISO 文字列に正規化済みで
    文字列辞書順比較を使う (``_iso_utc``) ため、読み側の窓引数だけ非対称に
    naive/非 UTC offset を許すと窓や順序が静かにずれる。"""
    if dt.tzinfo is None:
        raise ValueError(f"{label} is naive; tz-aware UTC datetime required "
                          "(cannot safely assume UTC)")
    return dt.astimezone(timezone.utc)


def load_bars(conn: sqlite3.Connection, symbol: str, interval: str, *,
              source: str, since: datetime | None = None,
              until: datetime | None = None) -> list[Bar]:
    q = "SELECT * FROM ohlcv WHERE symbol=? AND interval=? AND source=?"
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


def load_spread(conn: sqlite3.Connection, symbol: str, interval: str,
                bar_time_iso: str, *, source: str) -> float | None:
    """指定 (symbol, interval, bar_time, source) — ohlcv の PK と同じ 4 列 —
    の spread。

    F7 (fix round 1, sonnet Important): 当初 brief のシグネチャは `interval`
    を取らなかったが、ohlcv の PK は (symbol, interval, bar_time, source) の
    4 列であり、`interval` を省くと複数 interval が同じ bar_time を共有する
    場合に一意に絞り込めない。呼び出し元がまだ存在しない今のうちに
    `interval` を必須引数へ追加し、PK と一致する一意 lookup にする。
    """
    row = conn.execute(
        "SELECT spread FROM ohlcv WHERE symbol=? AND interval=? "
        "AND bar_time=? AND source=?",
        (symbol, interval, bar_time_iso, source)).fetchone()
    return row["spread"] if row is not None else None


@dataclass(frozen=True)
class ImportResult:
    inserted: int
    unchanged: int
    conflicted: int


def _close_enough(a: float | None, b: float | None) -> bool:
    """既存行 vs 入力行の 1 列比較。spread の NULL 同士は一致、NULL vs 数値は
    conflicted (spec §6)。"""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < _FLOAT_TOL


def _validate_and_normalize_row(idx: int, row: tuple) -> tuple:
    """F4 (fix round 1, codex M1/M2 + sonnet Minor-1): 投入前検証。

    - 有限値であること (NaN/inf を弾く)
    - `low <= min(open, close) <= max(open, close) <= high` (壊れた OHLC を
      弾く)
    - `bar_time` は aware datetime として parse できること。表記ゆれ
      (naive/aware・タイムゾーン表記の違い) で同じ瞬間が別 PK になるのを
      防ぐため、**UTC へ変換してから** `isoformat()` した文字列に置き換えて
      返す (同じ offset 内の表記ゆれだけでなく、+09:00 等の別 offset で
      書かれた同一瞬間も同一 PK に正規化する)。

    不正行はここで ValueError を送出する。呼び出し側 (import_bars) は
    **全行をここで検証してから** 書き込みを始めるので、後半の 1 行が
    不正なら先行する正常行も一切 INSERT されない (トランザクション開始
    前の reject — SAVEPOINT より前の防御線)。
    """
    if len(row) != 9:
        raise ValueError(
            f"import_bars: rows[{idx}] has {len(row)} fields, expected 9 "
            "(symbol, interval, bar_time_iso, o, h, l, c, volume, spread)")
    symbol, interval, bar_time_iso, o, h, l, c, volume, spread = row  # noqa: E741
    if not symbol or not interval:
        raise ValueError(f"import_bars: rows[{idx}] has empty symbol/interval")
    try:
        dt = datetime.fromisoformat(bar_time_iso)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"import_bars: rows[{idx}] bar_time not parseable: "
            f"{bar_time_iso!r}") from e
    if dt.tzinfo is None:
        raise ValueError(
            f"import_bars: rows[{idx}] bar_time is naive (aware datetime "
            f"required): {bar_time_iso!r}")
    normalized_bar_time = dt.astimezone(timezone.utc).isoformat()
    for name, val in (("open", o), ("high", h), ("low", l), ("close", c),
                      ("volume", volume)):
        if not isinstance(val, (int, float)) or isinstance(val, bool) \
                or not math.isfinite(val):
            raise ValueError(
                f"import_bars: rows[{idx}] {name} is not a finite number: "
                f"{val!r}")
    if spread is not None:
        if not isinstance(spread, (int, float)) or isinstance(spread, bool) \
                or not math.isfinite(spread):
            raise ValueError(
                f"import_bars: rows[{idx}] spread is not a finite number: "
                f"{spread!r}")
    lo_bound, hi_bound = min(o, c), max(o, c)
    if not (l <= lo_bound <= hi_bound <= h):
        raise ValueError(
            f"import_bars: rows[{idx}] violates low<=min(open,close)<="
            f"max(open,close)<=high: open={o} high={h} low={l} close={c}")
    return (symbol, interval, normalized_bar_time, o, h, l, c, volume, spread)


def import_bars(conn: sqlite3.Connection, rows: list[tuple], *,
                source: str) -> ImportResult:
    """インポータ用。既存行は不変 — 同一キー同一値は無変更 (unchanged)、
    値が異なる既存行は入力をスキップして件数だけ計上する (conflicted)。

    rows: (symbol, interval, bar_time_iso, o, h, l, c, volume, spread|None)

    F4 (fix round 1, codex Important + sonnet 実測一致): バッチ原子性。
    以前は途中で例外が起きても明示ロールバックが無く、後続の無関係な
    `conn.commit()` で部分挿入がそのまま永続化されてしまった (sqlite3 の
    暗黙トランザクションは接続をまたいで残る)。ここでは (1) 書き込み前に
    全行を検証・正規化し (`_validate_and_normalize_row`)、(2) 実際の書き込み
    は `SAVEPOINT` で明示的に囲み、例外時は `ROLLBACK TO SAVEPOINT` してから
    再送出する。
    """
    if not source:
        raise ValueError("import_bars: source must be non-empty")
    normalized_rows = [_validate_and_normalize_row(i, row)
                       for i, row in enumerate(rows)]

    inserted = 0
    unchanged = 0
    conflicted = 0
    conn.execute("SAVEPOINT import_bars")
    try:
        for (symbol, interval, bar_time_iso, o, h, l, c, volume,  # noqa: E741
             spread) in normalized_rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO ohlcv (symbol, interval, bar_time, "
                "open, high, low, close, volume, source, spread) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (symbol, interval, bar_time_iso, o, h, l, c, volume, source,
                 spread))
            if cur.rowcount == 1:
                inserted += 1
                continue
            # 既に同一キーの行がある (INSERT OR IGNORE が無視した) → 既存行と
            # 値を比較する。行は変更しない。
            existing = conn.execute(
                "SELECT open, high, low, close, volume, spread FROM ohlcv "
                "WHERE symbol=? AND interval=? AND bar_time=? AND source=?",
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
        conn.execute("RELEASE import_bars")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT import_bars")
        conn.execute("RELEASE import_bars")
        raise
    conn.commit()
    if conflicted > 0:
        _log.warning("import_bars: %d row(s) conflicted with existing data "
                     "for source=%s (existing rows left unchanged)",
                     conflicted, source)
    return ImportResult(inserted, unchanged, conflicted)
