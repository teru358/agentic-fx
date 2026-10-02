"""外部取得 (経済指標・ニュース) の「最後に試みた時刻」の永続化。

メモリにしか無いと、サービスを再起動するたびに取得が走り、再起動の連打が
そのまま先方への連打になる (HTTP 429)。成功・失敗を問わず「試みた」時点を
alert_state (key-value) に残し、再起動をまたいで間隔を守る。

値は JSON: attempted_at (tz-aware UTC の ISO) と retry_after_sec
(先方が Retry-After で待てと言った秒数。無ければ 0)。
"""
from __future__ import annotations

import json
import math
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
    """(試行時刻, Retry-After)。行が無ければ None、壊れていれば ValueError。

    永続層そのものが壊れて読めない間は、呼び出し側 (scheduler / news) が
    fail-open で取得を続ける。再起動のたびに取得することになるが、この機能が
    無かった頃と同じ頻度でそれ以上には増えない。取得を止める側に倒すと、DB の
    一時的な不調で経済指標が更新されなくなり、取引判断が古い指標で動く。
    """
    row = conn.execute(
        "SELECT value FROM alert_state WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row["value"])
        attempted = datetime.fromisoformat(data["attempted_at"])
        raw = data["retry_after_sec"]
        # inf / nan / 負値 / 型違い / 記録側の上限超えは、書いた覚えのない値
        # (破損)。保守的な待機に倒すため、ここで弾いて呼び出し側に任せる。
        if (isinstance(raw, bool) or not isinstance(raw, (int, float))
                or not math.isfinite(raw) or raw < 0
                or raw > MAX_RETRY_AFTER.total_seconds()):
            raise ValueError("retry_after_sec が範囲外")
        return _utc(attempted), timedelta(seconds=raw)
    except (ValueError, KeyError, TypeError, OverflowError) as e:
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
    # 壊れた値は Retry-After 無しとして扱う。news は source ごとの値で、
    # 全体の間隔 (scheduler) が先に取得頻度を縛り、この source の記録は
    # 直後の試行で正常な値に書き直される。
    try:
        loaded = load(conn, key)
    except ValueError:
        return None
    if loaded is None:
        return None
    attempted, retry = loaded
    try:
        until = attempted + retry
    except OverflowError:
        return None
    return until if retry > timedelta(0) and until > _utc(now) else None
