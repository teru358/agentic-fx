from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from agentic_fx.core import market_hours

if TYPE_CHECKING:
    from agentic_fx.config import Settings


def start(conn: sqlite3.Connection, loop: str, runner: str, model: str,
          now: datetime, trigger: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO missions (loop, runner, model, status, started_at, trigger) "
        "VALUES (?,?,?,'running',?,?)", (loop, runner, model, now.isoformat(), trigger))
    conn.commit()
    return cur.lastrowid


def finish(conn: sqlite3.Connection, mission_id: int, status: str,
           output: dict | None, transcript: list, now: datetime) -> None:
    conn.execute(
        "UPDATE missions SET status=?, output_json=?, transcript_json=?, "
        "finished_at=? WHERE id=?",
        (status,
         json.dumps(output, ensure_ascii=False) if output is not None else None,
         json.dumps(transcript, ensure_ascii=False),
         now.isoformat(), mission_id))
    conn.commit()


def set_trigger(conn: sqlite3.Connection, mission_id: int, trigger: str) -> None:
    """既存 Mission 行の trigger 列を上書きする (プラン 7 Task 8)。

    signal-aware lifecycle 専用: `TradeLoop` は claim より前に暫定値
    ``"signal"`` で `start()` した Mission 行を、claim 成功後に
    ``f"signal:{plugin}"`` へ確定させるために使う。暫定値のまま NULL 窓を
    作らない (§12「loop='trade' 以外は NULL」不変条件は維持したまま、
    trade Mission は常に非 NULL の trigger を持つ)。
    """
    conn.execute("UPDATE missions SET trigger=? WHERE id=?", (trigger, mission_id))
    conn.commit()


def signals_rate_ok(conn: sqlite3.Connection, now: datetime,
                     settings: "Settings") -> bool:
    """signal トリガーの取引判断 Mission 起動レート制限 (プラン 7 Task 8)。

    別テーブルのカウンタは持たない — ``missions.trigger LIKE 'signal%'`` の
    行 (暫定値 ``"signal"`` と確定値 ``"signal:<plugin>"`` の両方にヒット
    する) が **DB 永続カウンタそのもの**。claim に失敗し ``status="skipped"``
    で終端した Mission 行もこの LIKE にヒットする (= レート制限に算入
    される) — 「シグナル起動の試行そのもの」を数える設計であり、claim の
    成否では区別しない (`signal_due_fn` が `signals.pending_exists` を先に
    見るため、claim 失敗が頻発する状況は鮮度切れ等の異常系であり、そこでも
    レート制限が効くのは望ましい)。

    最短間隔: 直近の signal% 行 (started_at 最大) から
    ``settings.plugin.signal_min_interval_min`` 分未満なら False。
    日次上限: ``market_hours.trading_day_start(now)`` 以降の signal% 行数が
    ``settings.plugin.signal_daily_max`` 以上なら False。
    """
    rows = conn.execute(
        "SELECT started_at FROM missions WHERE trigger LIKE 'signal%'"
    ).fetchall()
    if not rows:
        return True
    started_ats = sorted(datetime.fromisoformat(r["started_at"]) for r in rows)
    plugin = settings.plugin
    if now - started_ats[-1] < timedelta(minutes=plugin.signal_min_interval_min):
        return False
    day_start = market_hours.trading_day_start(now)
    today_count = sum(1 for ts in started_ats if ts >= day_start)
    return today_count < plugin.signal_daily_max


def loop_of(conn: sqlite3.Connection, mission_id: int) -> str | None:
    """mission の loop 種別。存在しなければ None。

    executor が「その intent は取引判断 Mission の出力か」を DB で照合する
    ために使う (設計書 §5)。origin は呼び出し側が渡す enum 値に過ぎず、
    任意の内部コードが Origin.SCHEDULER を構成できてしまうため、origin 検証
    だけでは「scheduler が起動した取引判断 Mission だけ」という性質を担保
    できない (codex レビュー 4)。
    """
    row = conn.execute(
        "SELECT loop FROM missions WHERE id=?", (mission_id,)).fetchone()
    return row["loop"] if row is not None else None


def recent(conn: sqlite3.Connection, n: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM missions ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [dict(r) for r in rows]
