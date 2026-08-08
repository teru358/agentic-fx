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
           output: dict | None, transcript: list, now: datetime) -> bool:
    """missions 行を終端状態へ CAS 更新する (設計書 §4.7 codex C-4)。

    `WHERE status='running'` を満たさない (= 既に終端済み) 場合は影響行数
    0 のまま何もしない — 無条件 UPDATE による「後勝ち上書き」を構造的に
    封鎖する (finalize 所有権は commit 相の finally 一箇所のみ、という
    設計書 §4.7 の不変条件をこの CAS が実装レベルで強制する)。

    戻り値: `True` = この呼び出しが終端を書いた。`False` = 既に終端済み
    だった (二重終端防止) — **呼び出し元はこの場合 activity 警告を残し、
    この結果を上書きしたと誤認しないこと**。
    """
    cur = conn.execute(
        "UPDATE missions SET status=?, output_json=?, transcript_json=?, "
        "finished_at=? WHERE id=? AND status='running'",
        (status,
         json.dumps(output, ensure_ascii=False) if output is not None else None,
         json.dumps(transcript, ensure_ascii=False),
         now.isoformat(), mission_id))
    conn.commit()
    return cur.rowcount > 0


def recover_interrupted(conn: sqlite3.Connection, *, now: datetime,
                        max_requeue: int) -> dict:
    """起動時回収 (設計書 §4.8 codex C-5): 前回停止時に `running` のまま
    残った missions 行を `'interrupted'` へ終端し、それらを claim して
    いた `claimed` signals を**同一トランザクション**で requeue (上限
    超過は abandoned) する。

    分離すると「mission は終端済みなのに signal は lease 満了 (最大 15
    分) まで不可視」の不整合窓が生じる — signal の `claimed_at` は直近
    (プロセス生存中の claim) であり得るため、通常の lease ベース回収
    (`signals.reclaim_expired`) では長時間拾われない。

    `'interrupted'` は DB 回収専用の状態値であり `MissionResult.status`
    の 4 値契約 (`runners/base.py:40`) には現れない — status 表示・集計
    はこの区別を明記すること (codex M-1)。

    signals の requeue/abandon 判定は `signals.py` の
    `_REQUEUE_STATUS_EXPR`/`_REQUEUE_COUNT_EXPR` と同じ規則 (現在の
    requeue_count が max_requeue 以上なら abandoned、未満なら pending +
    +1) を Python 側で再現する — `signals.reclaim_expired` を呼ぶと
    それ自身が `conn.commit()` するため、missions の更新と同一トランザ
    クションを構成できない (この関数専用に単一トランザクションで完結
    させる必要がある)。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        running_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM missions WHERE status='running'").fetchall()]
        for mid in running_ids:
            conn.execute(
                "UPDATE missions SET status='interrupted', finished_at=? "
                "WHERE id=?", (now.isoformat(), mid))

        signals_requeued = signals_abandoned = 0
        if running_ids:
            placeholders = ",".join("?" * len(running_ids))
            claimed_rows = conn.execute(
                "SELECT id, requeue_count FROM signals WHERE status='claimed' "
                f"AND claimed_by_mission_id IN ({placeholders})",
                running_ids).fetchall()
            for row in claimed_rows:
                if row["requeue_count"] >= max_requeue:
                    conn.execute(
                        "UPDATE signals SET status='abandoned', "
                        "claimed_by_mission_id=NULL, claimed_at=NULL "
                        "WHERE id=?", (row["id"],))
                    signals_abandoned += 1
                else:
                    conn.execute(
                        "UPDATE signals SET status='pending', "
                        "requeue_count=requeue_count+1, "
                        "claimed_by_mission_id=NULL, claimed_at=NULL "
                        "WHERE id=?", (row["id"],))
                    signals_requeued += 1
        conn.commit()
        return {"missions_recovered": len(running_ids),
                "signals_requeued": signals_requeued,
                "signals_abandoned": signals_abandoned}
    except BaseException:
        conn.rollback()
        raise


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
