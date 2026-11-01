"""外部取得 (経済指標・ニュース) の「最後に試みた時刻」の永続化。

メモリにしか無いと、サービスを再起動するたびに取得が走り、再起動の連打が
そのまま先方への連打になる (HTTP 429)。成功・失敗を問わず「試みた」時点を
alert_state (key-value) に残し、再起動をまたいで間隔を守る。

値は JSON: attempted_at (tz-aware UTC の ISO) と retry_after_sec
(先方が Retry-After で待てと言った秒数。無ければ 0)。

経済指標カレンダーは取得の結果 (成功・失敗) も同じ値に残す: last_outcome
("success" / "failure")、last_outcome_at、last_success_at。試行の記録
(record_attempt / record_retry_after) はこれらを書き換えない。
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone

from agentic_fx.core.econ_window import (
    FRESHNESS_FRESH, FRESHNESS_STALE, FRESHNESS_UNKNOWN)

ECON_KEY = "fetch_attempt.econ"
NEWS_KEY = "fetch_attempt.news"
NEWS_SOURCE_KEY_PREFIX = "fetch_attempt.news."

# 経済指標カレンダーの取得周期。scheduler の取得間隔と同じ値で、鮮度の閾値
# (周期 × 2) の基になる。
ECON_REFRESH_INTERVAL = timedelta(hours=6)
_OUTCOME_FIELDS = ("last_outcome", "last_outcome_at", "last_success_at")
OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"

# 先方の Retry-After をそのまま信じると、異常な値で取得が事実上止まる。
MAX_RETRY_AFTER = timedelta(hours=24)


def news_source_key(name: str) -> str:
    return NEWS_SOURCE_KEY_PREFIX + name


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("fetch_attempts: naive datetime は受け付けない")
    return value.astimezone(timezone.utc)


def _preserved_outcome(conn: sqlite3.Connection, key: str) -> dict:
    """既存の値に残っている取得結果の項目。読めなければ空。"""
    row = conn.execute(
        "SELECT value FROM alert_state WHERE key=?", (key,)).fetchone()
    if row is None:
        return {}
    try:
        data = json.loads(row[0])
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in _OUTCOME_FIELDS if k in data}


def _write(conn: sqlite3.Connection, key: str, attempted_at: datetime,
           retry_after: timedelta, *, now: datetime,
           outcome: dict | None = None) -> None:
    fields = {"attempted_at": _utc(attempted_at).isoformat(),
              "retry_after_sec": retry_after.total_seconds()}
    fields.update(_preserved_outcome(conn, key) if outcome is None else outcome)
    value = json.dumps(fields)
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


def record_outcome(conn: sqlite3.Connection, key: str, now: datetime, *,
                   ok: bool) -> None:
    """取得の結果を残す。試行時刻と Retry-After は変えない。

    成功は last_success_at も更新する。失敗は最後の成功の時刻を保つ。
    値が壊れていれば、今を試行時刻にした新しい値から始める。
    """
    now = _utc(now)
    try:
        attempted, retry = load(conn, key) or (now, timedelta(0))
    except ValueError:
        attempted, retry = now, timedelta(0)
    outcome = _preserved_outcome(conn, key)
    outcome["last_outcome"] = OUTCOME_SUCCESS if ok else OUTCOME_FAILURE
    outcome["last_outcome_at"] = now.isoformat()
    if ok:
        outcome["last_success_at"] = now.isoformat()
    _write(conn, key, attempted, retry, now=now, outcome=outcome)


def freshness(conn: sqlite3.Connection, key: str, now: datetime, *,
              refresh_interval: timedelta = ECON_REFRESH_INTERVAL) -> str:
    """取得結果の鮮度: unknown / fresh / stale。

    成功が一度も記録されていなければ unknown。直近の取得が失敗、または最後の
    成功から `refresh_interval` の 2 倍を超えていれば stale (ちょうどは fresh)。
    それ以外は fresh。値が読めないときも unknown (新しいとは言えない)。
    """
    now = _utc(now)
    row = conn.execute(
        "SELECT value FROM alert_state WHERE key=?", (key,)).fetchone()
    if row is None:
        return FRESHNESS_UNKNOWN
    try:
        data = json.loads(row[0])
        raw_success = data.get("last_success_at")
        if raw_success is None:
            return FRESHNESS_UNKNOWN
        success = _utc(datetime.fromisoformat(raw_success))
        outcome = data.get("last_outcome")
    except (ValueError, TypeError, AttributeError):
        return FRESHNESS_UNKNOWN
    if outcome == OUTCOME_FAILURE or now - success > refresh_interval * 2:
        return FRESHNESS_STALE
    return FRESHNESS_FRESH
