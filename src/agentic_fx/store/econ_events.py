from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Collection, Iterable, NamedTuple

from agentic_fx.core.econ_window import (
    ALL_COUNTRY, NEXT_EVENT_HORIZON, EconEvent, WindowSettings)
from agentic_fx.store.short_tx import Begin, TxResult, begin_immediate, short_tx

_log = logging.getLogger("agentic_fx.econ")

# julianday は µs を区別しない版があるので、SQL は余裕を持たせた前段の絞り込みに
# だけ使い、境界の判定は datetime で行う。
_SQL_SLACK = timedelta(seconds=1)

# 時刻が変わった event の旧 row を置き換える範囲 (新 row との時刻差)。
SUPERSEDE_RANGE = timedelta(days=7)


class Upserted(NamedTuple):
    """`upsert` の結果。

    `inserted` は新しい row、`revived` は superseded だった row が取得結果に
    再び現れて有効に戻ったもの。どちらも「今回の取得で新しく入った row」。
    """
    id: int
    inserted: bool
    revived: bool


def upsert(conn: sqlite3.Connection, *, ts: datetime, country: str, name: str,
           importance: int, actual: str | None = None,
           forecast: str | None = None, previous: str | None = None,
           ) -> Upserted:
    key = (ts.isoformat(), country, name)
    prev = conn.execute(
        "SELECT id, superseded_by FROM econ_events "
        "WHERE ts=? AND country=? AND name=?", key).fetchone()
    cur = conn.execute(
        "INSERT INTO econ_events (ts, country, name, importance, actual, "
        "forecast, previous) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(ts, country, name) DO UPDATE SET importance=excluded.importance, "
        "actual=excluded.actual, forecast=excluded.forecast, "
        "previous=excluded.previous, superseded_by=NULL",
        (*key, importance, actual, forecast, previous))
    row_id = cur.lastrowid if prev is None else prev[0]
    conn.commit()
    return Upserted(row_id, prev is None, prev is not None and prev[1] is not None)


def upcoming(conn: sqlite3.Connection, now: datetime, hours: int) -> list[dict]:
    end = now + timedelta(hours=hours)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM econ_events WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (now.isoformat(), end.isoformat()))]


# --- 帯の候補 ---------------------------------------------------------------

def _utc(value: datetime, what: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{what}: naive datetime is not accepted")
    return value.astimezone(timezone.utc)


def _parse(row) -> EconEvent | None:
    """row を event にする。時刻が読めない・tz 無しの row は警告して捨てる。"""
    try:
        return EconEvent.from_row(row)
    except (ValueError, TypeError) as e:
        _log.warning("econ event %s skipped: unreadable ts (%s)", row["id"], e)
        return None


def events_between(conn: sqlite3.Connection, *, lo: datetime, hi: datetime,
                   importance_min: int, currencies: Collection[str],
                   ) -> list[EconEvent]:
    """`lo <= ts <= hi` (両端を含む) の対象 event。ts 昇順。

    対象は importance が `importance_min` 以上、country が `currencies` か
    `All`、`superseded_by IS NULL`。superseded の row と対象外の通貨は返さない。
    """
    lo, hi = _utc(lo, "lo"), _utc(hi, "hi")
    ccys = sorted(set(currencies))
    marks = ",".join("?" * len(ccys))
    where_country = (f"(country=? OR country IN ({marks}))" if ccys
                     else "country=?")
    rows = conn.execute(
        "SELECT id, ts, country, name, importance, superseded_by "
        "FROM econ_events WHERE superseded_by IS NULL AND importance>=? "
        f"AND {where_country} "
        "AND julianday(ts) BETWEEN julianday(?) AND julianday(?)",
        [importance_min, ALL_COUNTRY, *ccys,
         (lo - _SQL_SLACK).isoformat(), (hi + _SQL_SLACK).isoformat()])
    out = []
    for row in rows:
        event = _parse(row)
        if event is not None and lo <= event.ts <= hi:
            out.append(event)
    out.sort(key=lambda e: (e.ts, e.id))
    return out


def window_candidates(conn: sqlite3.Connection, now: datetime, *,
                      settings: WindowSettings, currencies: Collection[str],
                      ) -> list[EconEvent]:
    """時刻 `now` で帯に入りうる event: ts が `[now - after, now + before]`。

    発表後の帯のために、過去の event も返す。
    """
    now = _utc(now, "now")
    return events_between(conn, lo=now - settings.after, hi=now + settings.before,
                          importance_min=settings.importance_min,
                          currencies=currencies)


def bar_candidates(conn: sqlite3.Connection, bar_open: datetime,
                   bar_close: datetime, *, settings: WindowSettings,
                   currencies: Collection[str]) -> list[EconEvent]:
    """足 `[bar_open, bar_close]` と帯が交差しうる event。

    ts が `[bar_open - after, bar_close + before]`。`now` を使わない。
    """
    bar_open, bar_close = _utc(bar_open, "bar_open"), _utc(bar_close, "bar_close")
    return events_between(
        conn, lo=bar_open - settings.after, hi=bar_close + settings.before,
        importance_min=settings.importance_min, currencies=currencies)


def upcoming_targets(conn: sqlite3.Connection, now: datetime, *,
                     settings: WindowSettings, currencies: Collection[str],
                     horizon: timedelta = NEXT_EVENT_HORIZON,
                     ) -> list[EconEvent]:
    """次 event の検索用: ts が `[now, now + horizon]` の対象 event。"""
    now = _utc(now, "now")
    return events_between(conn, lo=now, hi=now + horizon,
                          importance_min=settings.importance_min,
                          currencies=currencies)


# --- 時刻が変わった event の置換 -------------------------------------------

def supersede_in_tx(conn: sqlite3.Connection, *, new_ids: Iterable[int],
                    fetched_keys: Collection[tuple[datetime, str, str]],
                    now: datetime) -> list[tuple[int, int]]:
    """今回の取得で新しく入った row が置き換えた旧 row に `superseded_by` を書く。

    commit しない。置換されるのは次をすべて満たす旧 row だけ:
    新 row と同じ `(country, name)`、`superseded_by IS NULL`、`ts > now`
    (未発表)、新 row との時刻差が `SUPERSEDE_RANGE` 以内、
    `(ts, country, name)` が今回の取得結果 `fetched_keys` に無い。
    既発表の同名 row、7 日を超えて離れた同名 row、同じ取得結果に並ぶ同名 row は
    別インスタンスとして残る。

    新 row は旧 row を 1 行だけ、旧 row は新 row 1 行にだけ結ばれる。割当ては
    時刻差の小さい順 (同差は旧 row id、新 row id の小さい順)。戻り値は
    `(旧 row id, 新 row id)`。削除はしない。
    """
    now = _utc(now, "now")
    fetched = {(_utc(ts, "fetched ts"), country, name)
               for ts, country, name in fetched_keys}
    ids = set(new_ids)
    if not ids:
        return []
    news = []
    ordered = sorted(ids)
    for i in range(0, len(ordered), 500):
        chunk = ordered[i:i + 500]
        for row in conn.execute(
                "SELECT id, ts, country, name, importance, superseded_by "
                f"FROM econ_events WHERE id IN ({','.join('?' * len(chunk))})",
                chunk):
            event = _parse(row)
            if event is not None:
                news.append(event)
    olds: dict[tuple[str, str], list[EconEvent]] = {}
    for country, name in sorted({(e.country, e.name) for e in news}):
        for row in conn.execute(
                "SELECT id, ts, country, name, importance, superseded_by "
                "FROM econ_events WHERE country=? AND name=? "
                "AND superseded_by IS NULL", (country, name)):
            event = _parse(row)
            if (event is not None and event.id not in ids and event.ts > now
                    and (event.ts, event.country, event.name) not in fetched):
                olds.setdefault((country, name), []).append(event)
    pairs = []
    for new in news:
        for old in olds.get((new.country, new.name), ()):
            diff = abs(old.ts - new.ts)
            if diff <= SUPERSEDE_RANGE:
                pairs.append((diff, old.id, new.id))
    pairs.sort()
    used_old: set[int] = set()
    used_new: set[int] = set()
    done: list[tuple[int, int]] = []
    for _, old_id, new_id in pairs:
        if old_id in used_old or new_id in used_new:
            continue
        cur = conn.execute(
            "UPDATE econ_events SET superseded_by=? "
            "WHERE id=? AND superseded_by IS NULL", (new_id, old_id))
        if cur.rowcount == 1:
            used_old.add(old_id)
            used_new.add(new_id)
            done.append((old_id, new_id))
    return done


def supersede_tx(conn: sqlite3.Connection, *, new_ids: Iterable[int],
                 fetched_keys: Collection[tuple[datetime, str, str]],
                 now: datetime, begin: Begin = begin_immediate) -> TxResult:
    """`supersede_in_tx` を 1 短 transaction で commit する。`value` が結果。"""
    def body():
        return {"value": supersede_in_tx(
            conn, new_ids=new_ids, fetched_keys=fetched_keys, now=now)}

    return short_tx(conn, begin, body)
