"""退避した 1m 足 (slice) の set 台帳、staging / 完備 CAS、世代、purge と容量算定。

set は event × pair か shadow 1 行を owner とする。状態は次だけを動く。

- event set: 作成は `awaiting`。`awaiting` → `staging` (現世代) → `complete`、
  `awaiting` → `unrecoverable`、`awaiting` / `staging` → `cancelled`、
  設定由来の `cancelled` → `awaiting` (slice 0 行のときだけ)。staging の破棄・
  旧世代の回収は spread を残したまま `awaiting` に戻す。
- shadow set: `staging` (現世代) で作る → `complete`。作る前なら
  `unrecoverable` で作る。staging の破棄・回収は set 行ごと消す。

worker の書込みは `begin` 引数の関数で transaction を始める。既定の
`begin_immediate` は `BEGIN IMMEDIATE` だけを行う。writer admission (書込み要求
フラグの確認) は呼び出し側が `begin` に包んで渡し、`False` を返せば transaction を
始めずに `yielded` で戻る。`SQLITE_BUSY` は rollback して例外のまま上げる。
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from typing import Callable, Iterable, Mapping, Sequence

from agentic_fx.core import market_hours
from agentic_fx.store import signal_shadow
from agentic_fx.store.short_tx import (  # noqa: F401
    Begin, Checkpoint, Conflict, OverBudget, TxResult, begin_immediate, short_tx)
from agentic_fx.store.signals import producer_daily_signal_max, utc_iso

RULE_VERSION = 1

STATE_AWAITING = "awaiting"
STATE_STAGING = "staging"
STATE_COMPLETE = "complete"
STATE_UNRECOVERABLE = "unrecoverable"
STATE_CANCELLED = "cancelled"

QUALITY_OBSERVED = "observed"
QUALITY_OUTAGE = "datafeed_outage_gap"
QUALITIES = (QUALITY_OBSERVED, QUALITY_OUTAGE)

EVENT_RANGE_BEFORE = timedelta(minutes=15)
EVENT_RANGE_AFTER = timedelta(hours=4)
EVENT_SPREAD_LEAD = timedelta(minutes=5)
FINALIZE_GRACE = timedelta(days=1)
MAX_BATCH_ROWS = 240
RETENTION_DAYS = 400

# --- 1m 行と事前検証 ---------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SliceRow:
    bar_time: datetime
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    source: str
    quality: str


class InvalidSliceRow(ValueError):
    """DB の CHECK に当たる前に弾く不正な 1m 行。"""


# DDL の CHECK が価格 (high) と volume に課す上限。
_DDL_VALUE_LIMIT = 1e308


def _positive_finite(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 < value < _DDL_VALUE_LIMIT)


def slice_row_problem(row: SliceRow) -> str | None:
    """行が `ohlcv_slices` の制約を満たさない理由。満たせば `None`。"""
    if row.bar_time.tzinfo is None or row.bar_time.utcoffset() is None:
        return "bar_time is naive"
    if row.bar_time.second or row.bar_time.microsecond:
        return "bar_time is not on a minute boundary"
    if not isinstance(row.source, str) or not row.source:
        return "source is empty"
    prices = (row.open, row.high, row.low, row.close)
    if row.quality == QUALITY_OUTAGE:
        if any(v is not None for v in (*prices, row.volume)):
            return "an outage marker carries no OHLC or volume"
        return None
    if row.quality != QUALITY_OBSERVED:
        return f"unknown quality {row.quality!r}"
    if not all(_positive_finite(v) for v in prices):
        return "OHLC must be finite and positive"
    if not (isinstance(row.volume, (int, float)) and math.isfinite(row.volume)
            and 0 <= row.volume < _DDL_VALUE_LIMIT):
        return "volume must be finite and >= 0"
    if not (row.low <= row.open <= row.high and row.low <= row.close <= row.high):
        return "OHLC violates low <= open, close <= high"
    return None


def validate_slice_rows(rows: Sequence[SliceRow]) -> None:
    """全行の制約と minute の重複を検査する。最初の違反で `InvalidSliceRow`。"""
    seen: set[datetime] = set()
    for row in rows:
        problem = slice_row_problem(row)
        if problem is not None:
            raise InvalidSliceRow(f"{row.bar_time!r}: {problem}")
        key = row.bar_time.astimezone(timezone.utc)
        if key in seen:
            raise InvalidSliceRow(f"{row.bar_time!r}: duplicate minute")
        seen.add(key)


def _require_in_range(rows: Sequence[SliceRow], start: str, end: str) -> None:
    lo, hi = datetime.fromisoformat(start), datetime.fromisoformat(end)
    for row in rows:
        if not lo <= row.bar_time < hi:
            raise InvalidSliceRow(
                f"{row.bar_time!r} is outside the set range [{start}, {end})")
        if not market_hours.is_market_open(row.bar_time.astimezone(timezone.utc)):
            raise InvalidSliceRow(f"{row.bar_time!r} is in a closed minute")


IN_CHUNK = 500


def _chunks(values: Sequence, size: int = IN_CHUNK):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _check_spread_pips(value, label: str) -> float:
    if not (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0):
        raise ValueError(f"{label} must be finite and >= 0: {value!r}")
    return float(value)


def _range_minutes(start: datetime, end: datetime) -> int:
    for label, dt in (("range_start", start), ("range_end", end)):
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValueError(f"{label} is naive")
        if dt.second or dt.microsecond:
            raise ValueError(f"{label} is not on a minute boundary")
    if not start < end:
        raise ValueError("range_start must be before range_end")
    return int((end - start) / timedelta(minutes=1))


def open_minutes(start: datetime, end: datetime) -> list[datetime]:
    """Return open UTC minute starts in the half-open interval [start, end)."""
    _range_minutes(start, end)
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    minutes = []
    current = start
    while current < end:
        if market_hours.is_market_open(current):
            minutes.append(current)
        current += timedelta(minutes=1)
    return minutes


def open_minute_count(start: datetime, end: datetime) -> int:
    return len(open_minutes(start, end))


def _step(checkpoint: Checkpoint | None) -> None:
    """文と文の間で予算超過を見る。`None` なら何もしない。"""
    if checkpoint is not None:
        checkpoint()


def _insert_rows(conn: sqlite3.Connection, set_id: int,
                 rows: Sequence[SliceRow]) -> None:
    if not rows:
        return
    marks = ",".join(["(?,?,?,?,?,?,?,?,?)"] * len(rows))
    params: list = []
    for r in rows:
        params.extend((set_id, utc_iso(r.bar_time), r.open, r.high, r.low,
                       r.close, r.volume, r.source, r.quality))
    conn.execute(
        "INSERT INTO ohlcv_slices (set_id,bar_time,open,high,low,close,volume,"
        f"source,quality) VALUES {marks}", params)


def _check_batch(rows: Sequence[SliceRow], max_rows: int) -> None:
    if not 1 <= max_rows <= MAX_BATCH_ROWS:
        raise ValueError(f"max_rows must be in 1..{MAX_BATCH_ROWS}")
    if len(rows) > max_rows:
        raise ValueError(f"{len(rows)} rows exceed the batch limit {max_rows}")
    validate_slice_rows(rows)


def _row_from_db(r) -> SliceRow:
    return SliceRow(datetime.fromisoformat(r["bar_time"]), r["open"], r["high"],
                    r["low"], r["close"], r["volume"], r["source"], r["quality"])


# --- event set の台帳 --------------------------------------------------------

def event_set_targets(conn: sqlite3.Connection, *, importance_min: int,
                      pair_currencies: Mapping[str, Iterable[str]],
                      event_ids: Iterable[int] | None = None,
                      ) -> list[tuple[int, str]]:
    """退避対象の `(event_id, pair)`。

    importance が `importance_min` 以上、`superseded_by IS NULL`、country が
    pair の通貨か `All` の event について、対象 pair ごとに 1 組。`event_ids` を
    渡せばその event だけを見る。pair は `pair_currencies` の並び順で返す。
    """
    sql = ("SELECT id, country FROM econ_events "
           "WHERE superseded_by IS NULL AND importance >= ?")
    if event_ids is None:
        rows = conn.execute(sql + " ORDER BY id", [importance_min]).fetchall()
    else:
        ids = sorted(set(event_ids))
        rows = []
        for chunk in _chunks(ids):
            rows.extend(conn.execute(
                sql + f" AND id IN ({','.join('?' * len(chunk))}) ORDER BY id",
                [importance_min, *chunk]).fetchall())
    currencies = {pair: set(c) for pair, c in pair_currencies.items()}
    out: list[tuple[int, str]] = []
    for row in rows:
        for pair, ccys in currencies.items():
            if row["country"] == "All" or row["country"] in ccys:
                out.append((row["id"], pair))
    return out


@dataclass(frozen=True, slots=True)
class SyncResult:
    """`skipped` は ts が分境界でないため set を作れなかった event id。"""
    created: tuple[int, ...]
    cancelled: tuple[int, ...]
    reactivated: tuple[int, ...]
    deferred: tuple[int, ...]
    skipped: tuple[int, ...] = ()


class EventTsNotOnMinute(ValueError):
    """event の ts が分境界でなく、1m 足の区間を作れない。"""


def create_event_set_in_tx(conn: sqlite3.Connection, *, event_id: int,
                           pair: str, now: datetime) -> int | None:
    """`awaiting` の event set を作る。既にあれば何もせず `None` (冪等)。

    区間は event `ts` の `[ts-15m, ts+4h)`。superseded の event には作らない。
    """
    row = conn.execute(
        "SELECT ts FROM econ_events WHERE id=? AND superseded_by IS NULL",
        (event_id,)).fetchone()
    if row is None:
        return None
    ts = datetime.fromisoformat(row[0])
    if ts.tzinfo is None or ts.second or ts.microsecond:
        raise EventTsNotOnMinute(f"event {event_id}: ts is not on a minute boundary")
    start, end = ts - EVENT_RANGE_BEFORE, ts + EVENT_RANGE_AFTER
    cur = conn.execute(
        "INSERT INTO ohlcv_slice_sets (event_id,pair,range_start,range_end,"
        "expected_rows,state,created_at,rule_version) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(event_id,pair) DO NOTHING",
        (event_id, pair, utc_iso(start), utc_iso(end),
         open_minute_count(start, end), STATE_AWAITING, utc_iso(now),
         RULE_VERSION))
    return cur.lastrowid if cur.rowcount == 1 else None


def cancel_set_in_tx(conn: sqlite3.Connection, set_id: int, *,
                     now: datetime) -> bool:
    """未完 (`awaiting` / `staging`) の event set を `cancelled` にする。

    slice 行は残り、毎 cycle の掃除 (`sweep_cancelled_step`) が消す。
    """
    cur = conn.execute(
        "UPDATE ohlcv_slice_sets SET state=?, generation=NULL, completed_at=? "
        "WHERE id=? AND event_id IS NOT NULL AND state IN (?,?)",
        (STATE_CANCELLED, utc_iso(now), set_id, STATE_AWAITING, STATE_STAGING))
    return cur.rowcount == 1


def reactivate_in_tx(conn: sqlite3.Connection, set_id: int) -> bool:
    """`cancelled` の event set を同じ行のまま `awaiting` に戻す。

    slice 行が 0 件で、event が superseded でないときだけ。spread は残す。
    """
    cur = conn.execute(
        "UPDATE ohlcv_slice_sets SET state=?, completed_at=NULL, "
        "generation=NULL, purged_at=NULL "
        "WHERE id=? AND state=? AND event_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM ohlcv_slices WHERE set_id=?) "
        "AND (SELECT superseded_by FROM econ_events "
        "     WHERE econ_events.id=ohlcv_slice_sets.event_id) IS NULL",
        (STATE_AWAITING, set_id, STATE_CANCELLED, set_id))
    return cur.rowcount == 1


def sync_event_sets_in_tx(conn: sqlite3.Connection, *,
                          targets: Iterable[tuple[int, str]],
                          scope_event_ids: Iterable[int] | None,
                          now: datetime) -> SyncResult:
    """対象集合と set 台帳を揃える (commit しない)。

    対象の `(event_id, pair)` は無ければ `awaiting` で作り、`cancelled` なら
    再有効化を試みる (slice 行が残る・superseded なら見送り `deferred`)。
    `scope_event_ids` の event (None なら全 event) の未完 set で対象外のものは
    `cancelled` にする。
    """
    wanted = list(dict.fromkeys(targets))
    wanted_set = set(wanted)
    existing: dict[tuple[int, str], tuple[int, str]] = {}
    for chunk in _chunks(sorted({event_id for event_id, _ in wanted})):
        for row in conn.execute(
                "SELECT id, event_id, pair, state FROM ohlcv_slice_sets "
                f"WHERE event_id IN ({','.join('?' * len(chunk))})", chunk):
            existing[(row["event_id"], row["pair"])] = (row["id"], row["state"])
    created, reactivated, deferred, skipped = [], [], [], []
    for event_id, pair in wanted:
        found = existing.get((event_id, pair))
        if found is None:
            try:
                new_id = create_event_set_in_tx(conn, event_id=event_id,
                                                pair=pair, now=now)
            except EventTsNotOnMinute:
                if event_id not in skipped:
                    skipped.append(event_id)
                continue
            if new_id is not None:
                created.append(new_id)
            continue
        set_id, state = found
        if state != STATE_CANCELLED:
            continue
        if reactivate_in_tx(conn, set_id):
            reactivated.append(set_id)
        else:
            deferred.append(set_id)
    sql = ("SELECT id, event_id, pair FROM ohlcv_slice_sets "
           "WHERE event_id IS NOT NULL AND state IN (?,?)")
    params: list = [STATE_AWAITING, STATE_STAGING]
    if scope_event_ids is None:
        open_rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    else:
        open_rows = []
        for chunk in _chunks(sorted(set(scope_event_ids))):
            open_rows.extend(conn.execute(
                sql + f" AND event_id IN ({','.join('?' * len(chunk))})",
                [*params, *chunk]).fetchall())
        open_rows.sort(key=lambda r: r["id"])
    cancelled = []
    for row in open_rows:
        if (row["event_id"], row["pair"]) in wanted_set:
            continue
        if cancel_set_in_tx(conn, row["id"], now=now):
            cancelled.append(row["id"])
    return SyncResult(tuple(created), tuple(cancelled), tuple(reactivated),
                      tuple(deferred), tuple(skipped))


def sync_event_sets_tx(conn: sqlite3.Connection, *,
                       begin: Begin = begin_immediate,
                       checkpoint: Checkpoint | None = None,
                       **kwargs) -> TxResult:
    """`sync_event_sets_in_tx` を 1 短 transaction で commit する。`value` が結果。"""
    def body():
        return {"value": sync_event_sets_in_tx(conn, **kwargs)}

    return short_tx(conn, begin, body, checkpoint)


def sync_event_sets(conn: sqlite3.Connection, **kwargs) -> SyncResult | None:
    """`sync_event_sets_tx` の結果。commit されなければ `None`。"""
    out = sync_event_sets_tx(conn, **kwargs)
    return out.value if out.committed else None


def get_set(conn: sqlite3.Connection, set_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM ohlcv_slice_sets WHERE id=?",
                       (set_id,)).fetchone()
    return dict(row) if row else None


def spread_candidates(conn: sqlite3.Connection, *, now: datetime) -> list[dict]:
    """spread 未記録の `awaiting` event set のうち `ts-5m` 以後のもの。

    `now < ts` なら quote、`now >= ts` なら assumed を記録するのは worker。
    event の ts が早い順。
    """
    return [dict(r) for r in conn.execute(
        "SELECT ss.id AS set_id, ss.event_id, ss.pair, e.ts "
        "FROM ohlcv_slice_sets ss JOIN econ_events e ON e.id = ss.event_id "
        "WHERE ss.state=? AND ss.spread_pips IS NULL "
        "AND julianday(e.ts) <= julianday(?) "
        "ORDER BY julianday(e.ts), ss.id",
        (STATE_AWAITING, utc_iso(now + EVENT_SPREAD_LEAD)))]


def record_event_spread(conn: sqlite3.Connection, set_id: int, *,
                        spread_pips: float, spread_source: str,
                        begin: Begin = begin_immediate,
                        checkpoint: Checkpoint | None = None) -> bool:
    """spread が NULL の `awaiting` event set に 1 回だけ保存し commit する。

    保存できなければ (記録済み・状態違い・`begin` が見送り・予算超過) `False`。
    """
    return record_event_spread_tx(
        conn, set_id, spread_pips=spread_pips, spread_source=spread_source,
        begin=begin, checkpoint=checkpoint).committed


def record_event_spread_tx(conn: sqlite3.Connection, set_id: int, *,
                           spread_pips: float, spread_source: str,
                           begin: Begin = begin_immediate,
                           checkpoint: Checkpoint | None = None) -> TxResult:
    """`record_event_spread` の transaction 結果 (COMMIT 所要を含む)。"""
    if spread_source not in (signal_shadow.SPREAD_QUOTE,
                             signal_shadow.SPREAD_ASSUMED):
        raise ValueError(f"unknown spread_source: {spread_source!r}")
    _check_spread_pips(spread_pips, "spread_pips")

    def body():
        cur = conn.execute(
            "UPDATE ohlcv_slice_sets SET spread_pips=?, spread_source=? "
            "WHERE id=? AND event_id IS NOT NULL AND state=? "
            "AND spread_pips IS NULL",
            (float(spread_pips), spread_source, set_id, STATE_AWAITING))
        if cur.rowcount != 1:
            raise Conflict
        return {"set_id": set_id}

    return short_tx(conn, begin, body, checkpoint)


def matured_event_sets(conn: sqlite3.Connection, *, now: datetime,
                       limit: int,
                       after: tuple[str, int] | None = None) -> list[dict]:
    """`ts+4h+1 日` を過ぎて complete / unrecoverable / cancelled でない event set。

    `after` = (event の ts, set id) を渡すと、その key の次から返す。"""
    cutoff = utc_iso(now - EVENT_RANGE_AFTER - FINALIZE_GRACE)
    sql = ("SELECT ss.*, e.ts FROM ohlcv_slice_sets ss "
           "JOIN econ_events e ON e.id = ss.event_id "
           "WHERE ss.state IN (?,?) AND julianday(e.ts) <= julianday(?) ")
    args: list = [STATE_AWAITING, STATE_STAGING, cutoff]
    if after is not None:
        sql += "AND (julianday(e.ts), ss.id) > (julianday(?), ?) "
        args += [after[0], int(after[1])]
    sql += "ORDER BY julianday(e.ts), ss.id LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(sql, args)]


# --- 世代 ----------------------------------------------------------------

def new_generation_tx(conn: sqlite3.Connection, *, now: datetime,
                      begin: Begin = begin_immediate,
                      checkpoint: Checkpoint | None = None) -> TxResult:
    """worker 起動ごとに 1 行を commit する。`value` が現世代の id。"""
    def body():
        cur = conn.execute(
            "INSERT INTO ohlcv_slice_generations (started_at) VALUES (?)",
            (utc_iso(now),))
        return {"value": cur.lastrowid}

    return short_tx(conn, begin, body, checkpoint)


def new_generation(conn: sqlite3.Connection, *, now: datetime,
                   begin: Begin = begin_immediate,
                   checkpoint: Checkpoint | None = None) -> int | None:
    """`new_generation_tx` の現世代 id。commit されなければ `None`。"""
    out = new_generation_tx(conn, now=now, begin=begin, checkpoint=checkpoint)
    return out.value if out.committed else None


def stale_staging_sets(conn: sqlite3.Connection, *,
                       current_generation: int) -> list[dict]:
    """現世代より古い `staging` set (起動時の回収対象)。新しい世代は触らない。"""
    return [dict(r) for r in conn.execute(
        "SELECT id, event_id, shadow_id, generation FROM ohlcv_slice_sets "
        "WHERE state=? AND generation<? ORDER BY id",
        (STATE_STAGING, current_generation))]


# --- staging と完備 ---------------------------------------------------------

def stage_event_rows(conn: sqlite3.Connection, *, set_id: int, generation: int,
                     rows: Sequence[SliceRow], assumed_spread_pips: float,
                     max_rows: int = MAX_BATCH_ROWS,
                     begin: Begin = begin_immediate,
                     checkpoint: Checkpoint | None = None) -> TxResult:
    """event set に slice 行の 1 batch を書く。

    最初の batch は `awaiting` を現世代の `staging` へ CAS で移す。spread が
    記録済みならそのまま残し、未記録なら同じ transaction で
    `assumed_spread_pips` を `assumed` として固定する (取り逃しは後から quote へ
    置換しない)。以後の batch は `staging` かつ現世代のときだけ書く。それ以外は
    `conflict`。
    """
    _check_batch(rows, max_rows)
    assumed = _check_spread_pips(assumed_spread_pips, "assumed_spread_pips")

    def body():
        conn.execute(
            "UPDATE ohlcv_slice_sets SET state=?, generation=?, "
            "spread_source=CASE WHEN spread_pips IS NULL THEN ? "
            "ELSE spread_source END, spread_pips=COALESCE(spread_pips, ?) "
            "WHERE id=? AND event_id IS NOT NULL AND state=?",
            (STATE_STAGING, generation, signal_shadow.SPREAD_ASSUMED, assumed,
             set_id, STATE_AWAITING))
        _step(checkpoint)
        row = conn.execute(
            "SELECT range_start, range_end FROM ohlcv_slice_sets "
            "WHERE id=? AND event_id IS NOT NULL AND state=? AND generation=?",
            (set_id, STATE_STAGING, generation)).fetchone()
        if row is None:
            raise Conflict
        _require_in_range(rows, row[0], row[1])
        _step(checkpoint)
        _insert_rows(conn, set_id, rows)
        return {"set_id": set_id, "rows": len(rows)}

    return short_tx(conn, begin, body, checkpoint)


def _shadow_range_minutes(start: datetime, end: datetime) -> int:
    minutes = _range_minutes(start, end)
    if minutes > SHADOW_ROWS_PER_OWNER_MAX:
        raise ValueError(f"a shadow range holds at most "
                         f"{SHADOW_ROWS_PER_OWNER_MAX} minutes, got {minutes}")
    return open_minute_count(start, end)


def _require_live_shadow(conn: sqlite3.Connection, shadow_id: int,
                         pair: str, checkpoint: Checkpoint | None = None) -> None:
    """set を作る前に shadow が在り、終端でなく、signal と同じ pair かを確かめる。"""
    _step(checkpoint)
    row = conn.execute(
        "SELECT s.state, g.pair FROM signal_shadow s "
        "JOIN signals g ON g.id = s.signal_id WHERE s.id=?",
        (shadow_id,)).fetchone()
    if row is None:
        raise ValueError(f"no such shadow: {shadow_id}")
    if row["pair"] != pair:
        raise ValueError(f"pair {pair!r} differs from the shadow's "
                         f"signal pair {row['pair']!r}")
    if row["state"] not in (signal_shadow.STATE_PENDING_ENTRY,
                            signal_shadow.STATE_FILLED):
        raise Conflict


def stage_shadow_rows(conn: sqlite3.Connection, *, shadow_id: int, pair: str,
                      range_start: datetime, range_end: datetime,
                      generation: int, rows: Sequence[SliceRow],
                      now: datetime, max_rows: int = MAX_BATCH_ROWS,
                      begin: Begin = begin_immediate,
                      checkpoint: Checkpoint | None = None) -> TxResult:
    """shadow set に slice 行の 1 batch を書く。

    set が無ければ現世代の `staging` で作る (行数 = 開場 minute 数)。あれば
    `staging`・現世代・同じ区間のときだけ書く。それ以外は `conflict`。
    """
    _check_batch(rows, max_rows)
    expected = _shadow_range_minutes(range_start, range_end)
    start_s, end_s = utc_iso(range_start), utc_iso(range_end)
    _require_in_range(rows, start_s, end_s)

    def body():
        row = conn.execute(
            "SELECT id, state, generation, range_start, range_end, pair "
            "FROM ohlcv_slice_sets WHERE shadow_id=?", (shadow_id,)).fetchone()
        if row is None:
            _require_live_shadow(conn, shadow_id, pair, checkpoint)
            _step(checkpoint)
            set_id = conn.execute(
                "INSERT INTO ohlcv_slice_sets (shadow_id,pair,range_start,"
                "range_end,expected_rows,state,generation,created_at,"
                "rule_version) VALUES (?,?,?,?,?,?,?,?,?)",
                (shadow_id, pair, start_s, end_s, expected, STATE_STAGING,
                 generation, utc_iso(now),
                 RULE_VERSION)).lastrowid
        elif (row["state"] == STATE_STAGING and row["generation"] == generation
              and row["range_start"] == start_s and row["range_end"] == end_s
              and row["pair"] == pair):
            set_id = row["id"]
        else:
            raise Conflict
        _step(checkpoint)
        _insert_rows(conn, set_id, rows)
        return {"set_id": set_id, "rows": len(rows)}

    return short_tx(conn, begin, body, checkpoint)


def staging_rows(conn: sqlite3.Connection, *, set_id: int,
                 generation: int) -> list[SliceRow] | None:
    """読戻し用。set が `staging` かつ `generation` のときだけ行を返す。"""
    ok = conn.execute(
        "SELECT 1 FROM ohlcv_slice_sets WHERE id=? AND state=? AND generation=?",
        (set_id, STATE_STAGING, generation)).fetchone()
    if ok is None:
        return None
    return [_row_from_db(r) for r in conn.execute(
        "SELECT * FROM ohlcv_slices WHERE set_id=? ORDER BY bar_time", (set_id,))]


def complete_rows(conn: sqlite3.Connection, set_id: int) -> list[SliceRow] | None:
    """reader 用。set が `complete` で purge 前のときだけ行を返す (それ以外は
    `None`。purge 済みの set は行 0 本の完備と区別できるよう `None`)。"""
    ok = conn.execute(
        "SELECT 1 FROM ohlcv_slice_sets WHERE id=? AND state=? "
        "AND purged_at IS NULL",
        (set_id, STATE_COMPLETE)).fetchone()
    if ok is None:
        return None
    return [_row_from_db(r) for r in conn.execute(
        "SELECT * FROM ohlcv_slices WHERE set_id=? ORDER BY bar_time", (set_id,))]


@dataclass(frozen=True, slots=True)
class ShadowCas:
    """完備と同じ transaction で書く shadow の終端。"""
    shadow_id: int
    expected_state: str
    expected_updated_at: str
    outcome: signal_shadow.ShadowOutcome


def complete_set(conn: sqlite3.Connection, *, set_id: int, generation: int,
                 now: datetime, shadow: ShadowCas | None = None,
                 begin: Begin = begin_immediate,
                 checkpoint: Checkpoint | None = None) -> TxResult:
    """`staging`・現世代・行数 = `expected_rows` の set を `complete` にする。

    shadow 所有の set は `shadow` (終端 outcome) が必須、event 所有の set は渡せ
    ない (違反は `ValueError` / `ShadowTransitionError`)。shadow の終端は同じ
    transaction の CAS で書き、どちらかの CAS が合わなければ何も書かず `conflict`。
    """
    if shadow is not None:
        # 遷移表と終端の検査は transaction の外で先に済ませる。
        signal_shadow.check_transition(shadow.expected_state, shadow.outcome)
        if shadow.outcome.state not in (signal_shadow.STATE_RESOLVED,
                                        signal_shadow.STATE_EXPIRED):
            raise signal_shadow.ShadowTransitionError(
                "a set completes only together with a terminal shadow outcome")

    def body():
        owner = conn.execute(
            "SELECT shadow_id FROM ohlcv_slice_sets WHERE id=?",
            (set_id,)).fetchone()
        if owner is None:
            raise Conflict
        if owner[0] is not None and shadow is None:
            raise ValueError("a shadow-owned set completes only with its "
                             "shadow end")
        if owner[0] is None and shadow is not None:
            raise ValueError("an event-owned set has no shadow to end")
        if shadow is not None and owner[0] != shadow.shadow_id:
            raise Conflict
        _step(checkpoint)
        cur = conn.execute(
            "UPDATE ohlcv_slice_sets SET state=?, generation=NULL, "
            "completed_at=? WHERE id=? AND state=? AND generation=? "
            "AND expected_rows = "
            "(SELECT COUNT(*) FROM ohlcv_slices WHERE set_id=?)",
            (STATE_COMPLETE, utc_iso(now), set_id, STATE_STAGING, generation,
             set_id))
        if cur.rowcount != 1:
            raise Conflict
        if shadow is not None and not signal_shadow.transition_in_tx(
                conn, shadow.shadow_id, expected_state=shadow.expected_state,
                expected_updated_at=shadow.expected_updated_at,
                outcome=shadow.outcome, now=now, checkpoint=checkpoint):
            raise Conflict
        return {"set_id": set_id, "done": True}

    return short_tx(conn, begin, body, checkpoint)


def mark_event_unrecoverable(conn: sqlite3.Connection, *, set_id: int,
                             now: datetime,
                             begin: Begin = begin_immediate,
                             checkpoint: Checkpoint | None = None) -> TxResult:
    """元足が保持範囲より前の `awaiting` event set を `unrecoverable` で終える。"""
    def body():
        cur = conn.execute(
            "UPDATE ohlcv_slice_sets SET state=?, completed_at=? "
            "WHERE id=? AND event_id IS NOT NULL AND state=?",
            (STATE_UNRECOVERABLE, utc_iso(now), set_id, STATE_AWAITING))
        if cur.rowcount != 1:
            raise Conflict
        return {"set_id": set_id, "done": True}

    return short_tx(conn, begin, body, checkpoint)


def mark_shadow_unrecoverable(conn: sqlite3.Connection, *, shadow: ShadowCas,
                              pair: str, range_start: datetime,
                              range_end: datetime, now: datetime,
                              begin: Begin = begin_immediate,
                              checkpoint: Checkpoint | None = None) -> TxResult:
    """set の無い shadow に `unrecoverable` の set 行を作り、同じ transaction で
    shadow を `data_insufficient` に終える。set が既にあれば `conflict`。"""
    if shadow.outcome.result_code != signal_shadow.RESULT_DATA_INSUFFICIENT:
        raise ValueError("an unrecoverable shadow ends as data_insufficient")
    signal_shadow.check_transition(shadow.expected_state, shadow.outcome)
    expected = _shadow_range_minutes(range_start, range_end)

    def body():
        _require_live_shadow(conn, shadow.shadow_id, pair, checkpoint)
        _step(checkpoint)
        if conn.execute("SELECT 1 FROM ohlcv_slice_sets WHERE shadow_id=?",
                        (shadow.shadow_id,)).fetchone() is not None:
            raise Conflict
        _step(checkpoint)
        set_id = conn.execute(
            "INSERT INTO ohlcv_slice_sets (shadow_id,pair,range_start,range_end,"
            "expected_rows,state,created_at,completed_at,rule_version) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (shadow.shadow_id, pair, utc_iso(range_start), utc_iso(range_end),
             expected, STATE_UNRECOVERABLE, utc_iso(now), utc_iso(now),
             RULE_VERSION)).lastrowid
        if not signal_shadow.transition_in_tx(
                conn, shadow.shadow_id, expected_state=shadow.expected_state,
                expected_updated_at=shadow.expected_updated_at,
                outcome=shadow.outcome, now=now, checkpoint=checkpoint):
            raise Conflict
        return {"set_id": set_id, "done": True}

    return short_tx(conn, begin, body, checkpoint)


# --- 破棄・回収・掃除・purge (行は batch ごとに消す) --------------------------

def _delete_rows(conn: sqlite3.Connection, set_id: int, max_rows: int) -> int:
    if not 1 <= max_rows <= MAX_BATCH_ROWS:
        raise ValueError(f"max_rows must be in 1..{MAX_BATCH_ROWS}")
    return conn.execute(
        "DELETE FROM ohlcv_slices WHERE rowid IN (SELECT rowid FROM "
        "ohlcv_slices WHERE set_id=? LIMIT ?)", (set_id, max_rows)).rowcount


def _rows_left(conn: sqlite3.Connection, set_id: int) -> bool:
    return conn.execute("SELECT 1 FROM ohlcv_slices WHERE set_id=? LIMIT 1",
                        (set_id,)).fetchone() is not None


def discard_staging_step(conn: sqlite3.Connection, *, set_id: int,
                         generation: int, max_rows: int = MAX_BATCH_ROWS,
                         begin: Begin = begin_immediate,
                         checkpoint: Checkpoint | None = None) -> TxResult:
    """`staging`・`generation` の set の行を最大 `max_rows` 消す。

    行が尽きた transaction で、event set は spread を残して `awaiting`
    (世代 NULL) に戻し、shadow set は set 行を消す (`done=True`)。読戻し不一致の
    破棄と旧世代の回収に使う。
    """
    def body():
        row = conn.execute(
            "SELECT event_id FROM ohlcv_slice_sets "
            "WHERE id=? AND state=? AND generation=?",
            (set_id, STATE_STAGING, generation)).fetchone()
        if row is None:
            raise Conflict
        _step(checkpoint)
        deleted = _delete_rows(conn, set_id, max_rows)
        _step(checkpoint)
        if _rows_left(conn, set_id):
            return {"set_id": set_id, "rows": deleted}
        _step(checkpoint)
        if row[0] is not None:
            conn.execute(
                "UPDATE ohlcv_slice_sets SET state=?, generation=NULL "
                "WHERE id=?", (STATE_AWAITING, set_id))
        else:
            conn.execute("DELETE FROM ohlcv_slice_sets WHERE id=?", (set_id,))
        return {"set_id": set_id, "rows": deleted, "done": True}

    return short_tx(conn, begin, body, checkpoint)


def discard_staging(conn: sqlite3.Connection, *, set_id: int, generation: int,
                    max_rows: int = MAX_BATCH_ROWS,
                    begin: Begin = begin_immediate,
                    checkpoint: Checkpoint | None = None) -> TxResult:
    """`discard_staging_step` を終わるか commit されなくなるまで繰り返す。"""
    while True:
        result = discard_staging_step(conn, set_id=set_id, generation=generation,
                                      max_rows=max_rows, begin=begin,
                                      checkpoint=checkpoint)
        if not result.committed or result.done:
            return result


def cancelled_sets_with_rows(conn: sqlite3.Connection, *,
                             limit: int) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT ss.id FROM ohlcv_slice_sets ss WHERE ss.state=? "
        "AND EXISTS (SELECT 1 FROM ohlcv_slices WHERE set_id=ss.id) "
        "ORDER BY ss.id LIMIT ?", (STATE_CANCELLED, int(limit)))]


def sweep_cancelled_step(conn: sqlite3.Connection, *, set_id: int,
                         max_rows: int = MAX_BATCH_ROWS,
                         begin: Begin = begin_immediate,
                         checkpoint: Checkpoint | None = None) -> TxResult:
    """`cancelled` set に残る slice 行を最大 `max_rows` 消す。状態は変えない。"""
    def body():
        if conn.execute("SELECT 1 FROM ohlcv_slice_sets WHERE id=? AND state=?",
                        (set_id, STATE_CANCELLED)).fetchone() is None:
            raise Conflict
        _step(checkpoint)
        deleted = _delete_rows(conn, set_id, max_rows)
        _step(checkpoint)
        return {"set_id": set_id, "rows": deleted,
                "done": not _rows_left(conn, set_id)}

    return short_tx(conn, begin, body, checkpoint)


def _purge_cutoff(now: datetime, retention_days: int) -> str:
    # created_at はこの module が utc_iso で書くので、同じ形の文字列比較が
    # 時刻順と一致する (SQLite の julianday は版によって 1 ms 未満を丸める)。
    if retention_days < RETENTION_DAYS:
        raise ValueError(f"retention must be at least {RETENTION_DAYS} days")
    return utc_iso(now - timedelta(days=retention_days))


def purgeable_sets(conn: sqlite3.Connection, *, now: datetime, limit: int,
                   retention_days: int = RETENTION_DAYS,
                   after: int | None = None) -> list[int]:
    """`created_at` から保持日数を過ぎ、まだ purge していない complete /
    unrecoverable set。`after` を与えると、その set id より後ろだけを id 順に返す。"""
    return [r[0] for r in conn.execute(
        "SELECT id FROM ohlcv_slice_sets WHERE state IN (?,?) "
        "AND purged_at IS NULL AND created_at < ? AND id > ? "
        "ORDER BY id LIMIT ?",
        (STATE_COMPLETE, STATE_UNRECOVERABLE,
         _purge_cutoff(now, retention_days),
         -1 if after is None else int(after), int(limit)))]


def purge_step(conn: sqlite3.Connection, *, set_id: int, now: datetime,
               max_rows: int = MAX_BATCH_ROWS,
               retention_days: int = RETENTION_DAYS,
               begin: Begin = begin_immediate,
               checkpoint: Checkpoint | None = None) -> TxResult:
    """保持期限を過ぎた set の slice 行を最大 `max_rows` 消し、尽きたら
    `purged_at` を付ける。set 行・shadow 終端・反応値は触らない。"""
    cutoff = _purge_cutoff(now, retention_days)

    def body():
        if conn.execute(
                "SELECT 1 FROM ohlcv_slice_sets WHERE id=? AND state IN (?,?) "
                "AND purged_at IS NULL AND created_at < ?",
                (set_id, STATE_COMPLETE, STATE_UNRECOVERABLE,
                 cutoff)).fetchone() is None:
            raise Conflict
        _step(checkpoint)
        deleted = _delete_rows(conn, set_id, max_rows)
        _step(checkpoint)
        if _rows_left(conn, set_id):
            return {"set_id": set_id, "rows": deleted}
        _step(checkpoint)
        conn.execute("UPDATE ohlcv_slice_sets SET purged_at=? WHERE id=?",
                     (utc_iso(now), set_id))
        return {"set_id": set_id, "rows": deleted, "done": True}

    return short_tx(conn, begin, body, checkpoint)


def slice_row_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM ohlcv_slices").fetchone()[0]


# --- 容量 ----------------------------------------------------------------

SHADOW_ROWS_PER_OWNER_MAX = 240 + 1440
EVENT_ROWS_PER_SET = 255
# importance_min ごとの全保存 event 行数と観測日数。月換算 (1 月 = 365/12 日)
# して切り上げた値を E (event / 月) とする。
EVENT_ROWS_OBSERVED = {3: 117, 2: 238, 1: 1004}
EVENT_OBSERVED_DAYS = 66
# slice 関連 7 表の空 DDL の bytes (B0) と 1 行あたりの上側 bytes (β)。
# 実ディスクの測定で上包絡を確かめた値に置き換えるまでの仮値で、式だけを
# test で固定している。
SLICE_BASE_BYTES = 1_048_576
SLICE_BYTES_PER_ROW = 128
WARN_RATIO = Fraction(8, 10)
FULL_RATIO = Fraction(1)


def events_per_month(importance_min: int) -> int:
    rows = EVENT_ROWS_OBSERVED[importance_min]
    return math.ceil(Fraction(rows, EVENT_OBSERVED_DAYS) * Fraction(365, 12))


class CapacityOverrideBelowProducer(ValueError):
    """容量上書きが承認済み strategy から導く日次上限を下回る (起動拒否)。"""


@dataclass(frozen=True, slots=True)
class SignalCapacity:
    """日次 signal 容量 S。`below_producer` は実行中に上書きが下回った印。"""
    value: int
    producer: int
    override: int | None
    below_producer: bool


def effective_signal_capacity(*, override: int | None,
                              producer: int) -> SignalCapacity:
    """S は上書きがあればその値、無ければ S_producer。上書きが S_producer を
    下回れば S_producer に切り上げ、`below_producer` を立てる。"""
    if override is None:
        return SignalCapacity(producer, producer, None, False)
    if override < producer:
        return SignalCapacity(producer, producer, override, True)
    return SignalCapacity(override, producer, override, False)


def check_startup_capacity(*, override: int | None, producer: int) -> None:
    """起動時に上書きが S_producer を下回れば `CapacityOverrideBelowProducer`。"""
    if override is not None and override < producer:
        raise CapacityOverrideBelowProducer(
            f"econ.signal_capacity_daily_max={override} is below the "
            f"producer daily maximum {producer}")


def row_capacity(*, daily_signals: int, importance_min: int, pair_count: int,
                 retention_days: int = RETENTION_DAYS) -> int:
    """C = ceil(D × (S × 1680 + E × 12 / 365 × 255 × P))。"""
    per_day = (Fraction(daily_signals * SHADOW_ROWS_PER_OWNER_MAX)
               + Fraction(events_per_month(importance_min) * 12, 365)
               * EVENT_ROWS_PER_SET * pair_count)
    return math.ceil(retention_days * per_day)


def byte_capacity(rows: int, *, base_bytes: int = SLICE_BASE_BYTES,
                  bytes_per_row: int = SLICE_BYTES_PER_ROW) -> int:
    """B = B0 + C × β。"""
    return base_bytes + rows * bytes_per_row


def capacity_level(used: int, capacity: int) -> str:
    """`ok` / `warn` (80% 以上) / `full` (100% 以上)。"""
    if capacity <= 0:
        raise ValueError("capacity must be positive")
    ratio = Fraction(used, capacity)
    if ratio >= FULL_RATIO:
        return "full"
    if ratio >= WARN_RATIO:
        return "warn"
    return "ok"


@dataclass(frozen=True, slots=True)
class CapacityPlan:
    signals: SignalCapacity
    rows: int
    bytes: int


def capacity_plan(*, override: int | None, strategies, settings_pairs,
                  importance_min: int) -> CapacityPlan:
    """承認済み strategy・settings pairs・importance_min と上書きから S / C / B。

    LLM の呼出枠 (signal の最短間隔・日次上限) は入力に取らない。
    """
    pairs = list(settings_pairs)
    s = effective_signal_capacity(
        override=override,
        producer=producer_daily_signal_max(strategies, pairs))
    rows = row_capacity(daily_signals=s.value, importance_min=importance_min,
                        pair_count=len(pairs))
    return CapacityPlan(s, rows, byte_capacity(rows))
