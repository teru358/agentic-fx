"""価格源の停止の判定が使う facts の保存と読取り。

どの関数も commit しない。呼び出し側の transaction (observe の遷移の保存、通常取得の
cache 保存) の中で呼ぶので、facts は状態・cache と同じ単位で確定し、同じ単位で巻き戻る。
金額・価格以外の口座情報は持たない。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from agentic_fx.core.contracts import Bar
from agentic_fx.core.timeutil import as_utc


def _iso(value: datetime) -> str:
    return as_utc(value).isoformat()


def _parse(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def _flag(value) -> bool | None:
    return None if value is None else bool(value)


@dataclass(frozen=True, slots=True)
class KeyFacts:
    """key/source ごとの facts。行が無いことと、列が NULL (まだ見ていない・停滞なし・未確認) は別。

    `observed_through`: observe が最後に見た水位。`stall_start`: 連続未到着の起点 S。
    `last_attempt_ok`: 直近に確認できた試行の成否 (未確認は None)。
    """

    observed_through: datetime | None
    stall_start: datetime | None
    last_attempt_ok: bool | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Health:
    """observe が計算した自動復帰・手動再開の健全の真偽と、その観測時刻 (未観測は None)。"""

    auto_healthy: bool | None
    auto_healthy_at: datetime | None
    manual_healthy: bool | None
    manual_healthy_at: datetime | None


@dataclass(frozen=True, slots=True)
class Contact:
    epoch: int
    order_id: int
    pair: str
    bar_time: datetime
    kind: str
    detected_at: datetime


def save_key_facts(conn: sqlite3.Connection, pair: str, interval: str, source: str, *,
                   observed_through: datetime | None, stall_start: datetime | None,
                   last_attempt_ok: bool | None, now: datetime) -> None:
    conn.execute(
        "INSERT INTO datafeed_outage_key_facts (pair, interval, source, observed_through, "
        "stall_start, last_attempt_ok, updated_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(pair, interval, source) DO UPDATE SET "
        "observed_through=excluded.observed_through, stall_start=excluded.stall_start, "
        "last_attempt_ok=excluded.last_attempt_ok, updated_at=excluded.updated_at",
        (pair, interval, source,
         None if observed_through is None else _iso(observed_through),
         None if stall_start is None else _iso(stall_start),
         None if last_attempt_ok is None else int(bool(last_attempt_ok)),
         _iso(now)))


def load_key_facts(conn: sqlite3.Connection, source: str) -> dict[tuple[str, str], KeyFacts]:
    rows = conn.execute(
        "SELECT pair, interval, observed_through, stall_start, last_attempt_ok, updated_at "
        "FROM datafeed_outage_key_facts WHERE source=?", (source,)).fetchall()
    return {(r["pair"], r["interval"]): KeyFacts(
        _parse(r["observed_through"]), _parse(r["stall_start"]),
        _flag(r["last_attempt_ok"]), _parse(r["updated_at"])) for r in rows}


def save_health(conn: sqlite3.Connection, *, auto_healthy: bool, manual_healthy: bool,
                now: datetime) -> None:
    """state の行 (id=1) が先にあること。無ければ何も書かない。"""
    stamp = _iso(now)
    conn.execute(
        "UPDATE datafeed_outage_state SET auto_healthy=?, auto_healthy_at=?, "
        "manual_healthy=?, manual_healthy_at=? WHERE id=1",
        (int(bool(auto_healthy)), stamp, int(bool(manual_healthy)), stamp))


def load_health(conn: sqlite3.Connection) -> Health:
    row = conn.execute(
        "SELECT auto_healthy, auto_healthy_at, manual_healthy, manual_healthy_at "
        "FROM datafeed_outage_state WHERE id=1").fetchone()
    if row is None:
        return Health(None, None, None, None)
    return Health(_flag(row["auto_healthy"]), _parse(row["auto_healthy_at"]),
                  _flag(row["manual_healthy"]), _parse(row["manual_healthy_at"]))


def add_late_bars(conn: sqlite3.Connection, bars: Iterable[Bar], source: str, *,
                  detected_at: datetime) -> int:
    """未検査の後着足を足す。同じ key/source/足時刻の行は最初の検出を残す。"""
    rows = [(b.symbol, b.interval, source, _iso(b.ts), b.open, b.high, b.low, b.close,
             b.volume, _iso(detected_at)) for b in bars]
    conn.executemany(
        "INSERT INTO datafeed_late_bars (pair, interval, source, bar_time, open, high, "
        "low, close, volume, detected_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(pair, interval, source, bar_time) DO NOTHING", rows)
    return len(rows)


def list_late_bars(conn: sqlite3.Connection, source: str) -> list[tuple[Bar, datetime]]:
    """未検査の後着足と検出時刻を、足時刻順 (同時刻は key 順) に返す。"""
    rows = conn.execute(
        "SELECT * FROM datafeed_late_bars WHERE source=? "
        "ORDER BY bar_time, pair, interval", (source,)).fetchall()
    return [(Bar(r["pair"], r["interval"], datetime.fromisoformat(r["bar_time"]),
                 r["open"], r["high"], r["low"], r["close"], r["volume"]),
             datetime.fromisoformat(r["detected_at"])) for r in rows]


def delete_late_bars(conn: sqlite3.Connection, source: str,
                     keys: Iterable[tuple[str, str, datetime]]) -> None:
    conn.executemany(
        "DELETE FROM datafeed_late_bars WHERE source=? AND pair=? AND interval=? "
        "AND bar_time=?", [(source, p, i, _iso(t)) for p, i, t in keys])


def add_contact(conn: sqlite3.Connection, *, epoch: int, order_id: int, pair: str,
                bar_time: datetime, kind: str, detected_at: datetime) -> None:
    """接触待ちの参照 (注文・足時刻・種別)。同じ組は最初の記録を残す。"""
    conn.execute(
        "INSERT INTO datafeed_outage_contacts (epoch, order_id, bar_time, pair, kind, "
        "detected_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(epoch, order_id, bar_time) DO NOTHING",
        (epoch, order_id, _iso(bar_time), pair, kind, _iso(detected_at)))


def list_contacts(conn: sqlite3.Connection, *, epoch: int) -> list[Contact]:
    rows = conn.execute(
        "SELECT * FROM datafeed_outage_contacts WHERE epoch=? "
        "ORDER BY bar_time, order_id", (epoch,)).fetchall()
    return [Contact(r["epoch"], r["order_id"], r["pair"],
                    datetime.fromisoformat(r["bar_time"]), r["kind"],
                    datetime.fromisoformat(r["detected_at"])) for r in rows]
