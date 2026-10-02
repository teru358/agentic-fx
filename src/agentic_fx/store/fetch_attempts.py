"""外部取得 (経済指標・ニュース) の「最後に試みた時刻」の永続化。

メモリにしか無いと、サービスを再起動するたびに取得が走り、再起動の連打が
そのまま先方への連打になる (HTTP 429)。成功・失敗を問わず「試みた」時点を
alert_state (key-value) に残し、再起動をまたいで間隔を守る。

値は JSON: attempted_at (tz-aware UTC の ISO) と retry_after_sec
(先方が Retry-After で待てと言った秒数。無ければ 0)。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

ECON_KEY = "fetch_attempt.econ"
NEWS_KEY = "fetch_attempt.news"
NEWS_SOURCE_KEY_PREFIX = "fetch_attempt.news."

# 先方の Retry-After をそのまま信じると、異常な値で取得が事実上止まる。
MAX_RETRY_AFTER = timedelta(hours=24)


def news_source_key(name: str) -> str:
    return NEWS_SOURCE_KEY_PREFIX + name


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("fetch_attempts: naive datetime は受け付けない")
    return value.astimezone(timezone.utc)


def _write(conn: sqlite3.Connection, key: str, attempted_at: datetime,
           retry_after: timedelta, *, now: datetime) -> None:
    value = json.dumps({"attempted_at": _utc(attempted_at).isoformat(),
                        "retry_after_sec": retry_after.total_seconds()})
    # with conn: 例外時は ROLLBACK し、呼び出し側の接続に未確定の
    # transaction を残さない。
    with conn:
        conn.execute(
            "INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            "updated_at=excluded.updated_at",
            (key, value, _utc(now).isoformat()))


def record_attempt(conn: sqlite3.Connection, key: str, now: datetime) -> None:
    """取得を試みた時刻を残す (前回の Retry-After は消える)。"""
    _write(conn, key, now, timedelta(0), now=now)


def record_retry_after(conn: sqlite3.Connection, key: str, now: datetime,
                       retry_after: timedelta) -> None:
    """先方が待つよう求めた時間を、直前の試行に紐づけて残す。"""
    loaded = load(conn, key)
    attempted = loaded[0] if loaded is not None else now
    _write(conn, key, attempted, min(retry_after, MAX_RETRY_AFTER), now=now)


def load(conn: sqlite3.Connection,
         key: str) -> tuple[datetime, timedelta] | None:
    """(試行時刻, Retry-After)。行が無ければ None、壊れていれば ValueError。"""
    row = conn.execute(
        "SELECT value FROM alert_state WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row["value"])
        attempted = datetime.fromisoformat(data["attempted_at"])
        retry = timedelta(seconds=float(data["retry_after_sec"]))
        return _utc(attempted), max(retry, timedelta(0))
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(f"fetch_attempts: {key} の値が壊れている") from e


def due_anchor(conn: sqlite3.Connection, key: str, *, now: datetime,
               interval: timedelta) -> datetime | None:
    """「次の取得は anchor + interval」となる anchor を返す。記録が無ければ None。

    Retry-After が interval より長ければ、その分だけ anchor を先へ進める。
    永続値が未来 (時計の巻き戻し) か壊れている場合は「取得してよい」ではなく
    今を試行時刻として記録し直し、interval 1 回分だけ待たせる (連打を避ける)。
    """
    now = _utc(now)
    try:
        loaded = load(conn, key)
    except ValueError:
        loaded = (now + timedelta(days=36500), timedelta(0))
    if loaded is None:
        return None
    attempted, retry = loaded
    if attempted > now:
        record_attempt(conn, key, now)
        return now
    return attempted + max(retry - interval, timedelta(0))


def blocked_until(conn: sqlite3.Connection, key: str,
                  now: datetime) -> datetime | None:
    """Retry-After により now より先まで待つべきなら、その時刻。無ければ None。

    間隔そのものは呼び出し側 (scheduler) が持つので、ここでは先方の要求だけ
    を見る。値が未来・壊れている場合は待たせない (その場合の連打防止は
    scheduler の間隔側が担う)。
    """
    try:
        loaded = load(conn, key)
    except ValueError:
        return None
    if loaded is None:
        return None
    attempted, retry = loaded
    until = attempted + retry
    return until if retry > timedelta(0) and until > _utc(now) else None
