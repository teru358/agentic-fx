"""評価用履歴の同期の永続層: run / series / 系列状態 / gap queue / 保持。

表は `history_sync_runs`, `history_sync_series`, `history_sync_series_state`,
`history_sync_gaps` (DDL は `store/db.py`)。run への参照は論理参照で FK を張らない。
ここの関数は transaction を開閉しない (呼出し側が `write_txn` で短い transaction にする)。
価格・自由な理由はこの層に保存しない。
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable

CONTROL_SYMBOL = "__control__"
RETENTION_DAYS = 90
GAP_KINDS = ("primary_exact", "reference_suspected", "unprocessed")
GAP_OPEN, GAP_RESOLVED, GAP_EXPIRED = "open", "resolved", "expired"

# 失敗の連続数ごとの待ち (分)。4 回目以降は 60 分のまま。
FAILURE_BACKOFF_MINUTES = (5, 15, 30, 60)
# 欠損や疑いが改善しない取得の連続数。3 回で 24 時間、6 回で 7 日。
NO_IMPROVE_DAY_AT = 3
NO_IMPROVE_WEEK_AT = 6

_BUSY_STEPS = (0.010, 0.020, 0.040, 0.050)
BUSY_BUDGET_SEC = 0.25


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _is_busy(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc).lower()
        return "locked" in text or "busy" in text
    return False


def write_txn(conn: sqlite3.Connection, work: Callable[[sqlite3.Connection], object], *,
              sleep: Callable[[float], None] = time.sleep,
              budget: float = BUSY_BUDGET_SEC):
    """`work(conn)` を 1 つの短い `BEGIN IMMEDIATE` transaction で確定する。

    BUSY は transaction の外で 10/20/40/50ms と待って再試行し、累積が予算を超えたら
    例外を上げる。失敗した transaction は rollback する。
    """
    if conn.in_transaction:
        raise ValueError("write_txn: connection is already inside a transaction")
    spent = 0.0
    step = 0
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_busy(exc) and spent + 1e-9 < budget:
                pause = _BUSY_STEPS[min(step, len(_BUSY_STEPS) - 1)]
                step += 1
                sleep(pause)
                spent += pause
                continue
            raise
        try:
            result = work(conn)
            conn.commit()
            return result
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise


# ---- run ------------------------------------------------------------------

def insert_run(conn, *, run_id: str, owner: str, trigger: str, source: str, interval: str,
               calendar_version: str, lookback_days: int, epoch: str, t: datetime,
               b: datetime, e: datetime, started_at: datetime,
               request_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO history_sync_runs (id, request_id, owner, trigger, status, source, "
        "interval, calendar_version, lookback_days, history_epoch, t_utc, b_utc, e_utc, "
        "started_at) VALUES (?,?,?,?,'running',?,?,?,?,?,?,?,?,?)",
        (run_id, request_id, owner, trigger, source, interval, calendar_version,
         lookback_days, epoch, iso(t), iso(b), iso(e), iso(started_at)))


def finish_run(conn, run_id: str, *, status: str, rc: int, finished_at: datetime,
               series_total: int = 0, series_failed: int = 0, inserted: int = 0,
               unchanged: int = 0, conflicted_new: int = 0, conflicted_known: int = 0,
               requests: int = 0, failure_code: str | None = None,
               deferred_reason: str | None = None, unprocessed: object = None,
               unrecorded_series: int = 0, health: str | None = None) -> None:
    conn.execute(
        "UPDATE history_sync_runs SET status=?, rc=?, finished_at=?, series_total=?, "
        "series_failed=?, inserted=?, unchanged=?, conflicted_new=?, conflicted_known=?, "
        "requests=?, failure_code=?, deferred_reason=?, unprocessed_json=?, "
        "unrecorded_series=?, health=? WHERE id=?",
        (status, rc, iso(finished_at), series_total, series_failed, inserted, unchanged,
         conflicted_new, conflicted_known, requests, failure_code, deferred_reason,
         None if unprocessed is None else json.dumps(unprocessed, separators=(",", ":"),
                                                     sort_keys=True),
         unrecorded_series, health, run_id))


def mark_interrupted(conn, *, owner_alive: Callable[[str], bool], now: datetime) -> int:
    """終了を確認できた owner の running を interrupted にする (生きた run は触らない)。"""
    rows = conn.execute("SELECT id, owner FROM history_sync_runs WHERE status='running'"
                        ).fetchall()
    changed = 0
    for row in rows:
        if owner_alive(row["owner"]):
            continue
        conn.execute("UPDATE history_sync_runs SET status='interrupted', finished_at=? "
                     "WHERE id=? AND status='running'", (iso(now), row["id"]))
        conn.execute("UPDATE history_sync_series SET status='failed', "
                     "error_code=COALESCE(error_code,'interrupted'), finished_at=? "
                     "WHERE run_id=? AND status='running'", (iso(now), row["id"]))
        changed += 1
    return changed


# ---- series ---------------------------------------------------------------

def insert_series(conn, *, run_id: str, op_seq: int, operation: str, symbol: str,
                  range_kind: str, range_start: datetime, range_end: datetime,
                  day_utc: str | None, started_at: datetime, requests: int = 0,
                  generation: int = 1) -> int:
    cur = conn.execute(
        "INSERT INTO history_sync_series (run_id, op_seq, operation, generation, symbol, "
        "range_kind, range_start, range_end, day_utc, status, requests, started_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,'running',?,?)",
        (run_id, op_seq, operation, generation, symbol, range_kind, iso(range_start),
         iso(range_end), day_utc, requests, iso(started_at)))
    return int(cur.lastrowid)


def update_series_progress(conn, series_id: int, *, committed_prefix_end: str | None,
                           next_cursor: str | None, last_committed_bar_time: str | None) -> None:
    """batch と同じ transaction で呼ぶ。確定 prefix と cursor を進める。"""
    conn.execute(
        "UPDATE history_sync_series SET committed_prefix_end=?, next_cursor=?, "
        "last_committed_bar_time=? WHERE id=?",
        (committed_prefix_end, next_cursor, last_committed_bar_time, series_id))


def finish_series(conn, series_id: int, *, status: str, finished_at: datetime,
                  inserted: int = 0, unchanged: int = 0, conflicted_new: int = 0,
                  conflicted_known: int = 0, error_code: str | None = None,
                  last_committed_bar_time: str | None = None,
                  next_cursor: str | None = None) -> None:
    conn.execute(
        "UPDATE history_sync_series SET status=?, finished_at=?, inserted=?, unchanged=?, "
        "conflicted_new=?, conflicted_known=?, error_code=?, "
        "last_committed_bar_time=COALESCE(?, last_committed_bar_time), "
        "next_cursor=COALESCE(?, next_cursor) WHERE id=?",
        (status, iso(finished_at), inserted, unchanged, conflicted_new, conflicted_known,
         error_code, last_committed_bar_time, next_cursor, series_id))


# ---- 系列状態 -------------------------------------------------------------

def get_state(conn, source: str, interval: str, symbol: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM history_sync_series_state WHERE source=? AND interval=? AND symbol=?",
        (source, interval, symbol)).fetchone()


def _ensure_state(conn, source, interval, symbol, now: datetime) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO history_sync_series_state (source, interval, symbol, "
        "updated_at) VALUES (?,?,?,?)", (source, interval, symbol, iso(now)))


def advance_cursor(conn, source, interval, symbol, *, last_processed_e: datetime,
                   run_id: str, now: datetime) -> None:
    """確定した終端だけを進める。後ろへは戻さない。成功なので失敗の連続数と待ちを解く。"""
    _ensure_state(conn, source, interval, symbol, now)
    conn.execute(
        "UPDATE history_sync_series_state SET last_processed_e=CASE "
        "WHEN last_processed_e IS NULL OR last_processed_e < ? THEN ? "
        "ELSE last_processed_e END, attempts=0, next_retry_at=NULL, last_health='ok', "
        "last_error=NULL, last_run_id=?, updated_at=? "
        "WHERE source=? AND interval=? AND symbol=?",
        (iso(last_processed_e), iso(last_processed_e), run_id, iso(now), source, interval,
         symbol))


def record_series_failure(conn, source, interval, symbol, *, error_code: str, run_id: str,
                          now: datetime, manual: bool = True) -> None:
    """失敗を数える。待ちは延ばすだけで、手動の失敗が daemon の待ちを短くすることはない。"""
    _ensure_state(conn, source, interval, symbol, now)
    row = get_state(conn, source, interval, symbol)
    attempts = int(row["attempts"]) + 1
    minutes = FAILURE_BACKOFF_MINUTES[min(attempts, len(FAILURE_BACKOFF_MINUTES)) - 1]
    proposed = iso(now + timedelta(minutes=minutes))
    current = row["next_retry_at"]
    next_retry = proposed if current is None or current < proposed else current
    conn.execute(
        "UPDATE history_sync_series_state SET attempts=?, next_retry_at=?, "
        "last_health='failed', last_error=?, last_run_id=?, updated_at=? "
        "WHERE source=? AND interval=? AND symbol=?",
        (attempts, next_retry, error_code, run_id, iso(now), source, interval, symbol))


def set_rr_cursor(conn, source, interval, symbol: str | None, *, now: datetime) -> None:
    _ensure_state(conn, source, interval, CONTROL_SYMBOL, now)
    conn.execute("UPDATE history_sync_series_state SET rr_cursor=?, updated_at=? "
                 "WHERE source=? AND interval=? AND symbol=?",
                 (symbol, iso(now), source, interval, CONTROL_SYMBOL))


def get_rr_cursor(conn, source, interval) -> str | None:
    row = get_state(conn, source, interval, CONTROL_SYMBOL)
    return None if row is None else row["rr_cursor"]


# ---- 再開点 ---------------------------------------------------------------

def get_resume(conn, source, interval, symbol, op_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM history_sync_resume WHERE source=? AND interval=? AND symbol=? "
        "AND op_key=?", (source, interval, symbol, op_key)).fetchone()


def save_resume(conn, *, source, interval, symbol, op_key: str, range_start: datetime,
                range_end: datetime, tail_start: datetime, prefix_end: datetime,
                now: datetime) -> None:
    conn.execute(
        "INSERT INTO history_sync_resume (source, interval, symbol, op_key, range_start, "
        "range_end, tail_start, prefix_end, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source, interval, symbol, op_key) DO UPDATE SET "
        "range_start=excluded.range_start, range_end=excluded.range_end, "
        "tail_start=excluded.tail_start, prefix_end=excluded.prefix_end, "
        "updated_at=excluded.updated_at",
        (source, interval, symbol, op_key, iso(range_start), iso(range_end), iso(tail_start),
         iso(prefix_end), iso(now)))


def clear_resume(conn, source, interval, symbol, op_key: str) -> None:
    conn.execute("DELETE FROM history_sync_resume WHERE source=? AND interval=? AND symbol=? "
                 "AND op_key=?", (source, interval, symbol, op_key))


# ---- gap queue ------------------------------------------------------------

def _day_end(day: str) -> datetime:
    d = date.fromisoformat(day)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1)


def observe_gap(conn, *, source, interval, symbol, day_utc: str, kind: str,
                missing_count: int | None, detail: object = None, run_id: str | None,
                now: datetime) -> str:
    """観測した欠損・疑いを queue に反映する。返り値は 'new' / 'reopened' / 'updated' / 'expired_kept'。

    expired は通常の観測では戻さない (戻すのは `reopen_expired`)。resolved は再観測で open に戻す。
    """
    if kind not in GAP_KINDS:
        raise ValueError(f"unknown gap kind: {kind!r}")
    detail_json = None if detail is None else json.dumps(detail, separators=(",", ":"),
                                                         sort_keys=True)
    row = conn.execute(
        "SELECT state, missing_count FROM history_sync_gaps WHERE source=? AND interval=? "
        "AND symbol=? AND day_utc=? AND kind=?", (source, interval, symbol, day_utc, kind)
    ).fetchone()
    stamp = iso(now)
    if row is None:
        conn.execute(
            "INSERT INTO history_sync_gaps (source, interval, symbol, day_utc, kind, state, "
            "missing_count, detail_json, first_seen_at, last_observed_at, first_run_id, "
            "last_run_id) VALUES (?,?,?,?,?,'open',?,?,?,?,?,?)",
            (source, interval, symbol, day_utc, kind, missing_count, detail_json, stamp,
             stamp, run_id, run_id))
        return "new"
    if row["state"] == GAP_EXPIRED:
        return "expired_kept"
    reopened = row["state"] == GAP_RESOLVED
    conn.execute(
        "UPDATE history_sync_gaps SET state='open', missing_count=?, detail_json=?, "
        "last_observed_at=?, last_run_id=?, resolution=NULL"
        + (", attempts=0, no_improve_count=0, next_retry_at=NULL" if reopened else "")
        + " WHERE source=? AND interval=? AND symbol=? AND day_utc=? AND kind=?",
        (missing_count, detail_json, stamp, run_id, source, interval, symbol, day_utc, kind))
    return "reopened" if reopened else "updated"


def resolve_gap(conn, *, source, interval, symbol, day_utc, kind, resolution: str,
                run_id: str | None, now: datetime) -> None:
    conn.execute(
        "UPDATE history_sync_gaps SET state='resolved', resolution=?, attempts=0, "
        "no_improve_count=0, next_retry_at=NULL, last_observed_at=?, last_run_id=? "
        "WHERE source=? AND interval=? AND symbol=? AND day_utc=? AND kind=? AND state='open'",
        (resolution, iso(now), run_id, source, interval, symbol, day_utc, kind))


def expire_gaps(conn, *, source, interval, boundary: datetime, now: datetime) -> int:
    """日の終端が B 以前の open を expired にする。要求や再試行の予算は使わない。"""
    rows = conn.execute(
        "SELECT symbol, day_utc, kind FROM history_sync_gaps WHERE source=? AND interval=? "
        "AND state='open'", (source, interval)).fetchall()
    expired = 0
    for row in rows:
        if _day_end(row["day_utc"]) <= boundary:
            conn.execute(
                "UPDATE history_sync_gaps SET state='expired', next_retry_at=NULL, "
                "resolution='holdout_passed', last_observed_at=? WHERE source=? AND "
                "interval=? AND symbol=? AND day_utc=? AND kind=?",
                (iso(now), source, interval, row["symbol"], row["day_utc"], row["kind"]))
            expired += 1
    return expired


def reopen_expired(conn, *, source, interval, boundary: datetime, now: datetime) -> int:
    """holdout が過去へ戻ったとき (rescan のときだけ)、日の終端が B より後の expired を open に戻す。"""
    rows = conn.execute(
        "SELECT symbol, day_utc, kind FROM history_sync_gaps WHERE source=? AND interval=? "
        "AND state='expired'", (source, interval)).fetchall()
    reopened = 0
    for row in rows:
        if _day_end(row["day_utc"]) > boundary:
            conn.execute(
                "UPDATE history_sync_gaps SET state='open', resolution=NULL, attempts=0, "
                "no_improve_count=0, next_retry_at=NULL, last_observed_at=? WHERE source=? "
                "AND interval=? AND symbol=? AND day_utc=? AND kind=?",
                (iso(now), source, interval, row["symbol"], row["day_utc"], row["kind"]))
            reopened += 1
    return reopened


def record_gap_attempt(conn, *, source, interval, symbol, day_utc, kind,
                       missing_before: int | None, missing_after: int | None,
                       run_id: str | None, now: datetime) -> str:
    """取得を 1 回試した結果を数える。返り値は 'resolved' / 'improved' / 'no_improvement'。

    解消 (欠損 0) なら resolved。減ったら通常周期へ戻す。減らないなら連続数を増やし、
    3 回で 24 時間、6 回で 7 日の待ちにする。
    """
    if missing_after is not None and missing_after <= 0:
        resolve_gap(conn, source=source, interval=interval, symbol=symbol, day_utc=day_utc,
                    kind=kind, resolution="refetched", run_id=run_id, now=now)
        return "resolved"
    row = conn.execute(
        "SELECT attempts, no_improve_count FROM history_sync_gaps WHERE source=? AND "
        "interval=? AND symbol=? AND day_utc=? AND kind=?",
        (source, interval, symbol, day_utc, kind)).fetchone()
    attempts = int(row["attempts"]) + 1
    improved = (missing_before is not None and missing_after is not None
                and missing_after < missing_before)
    if improved:
        conn.execute(
            "UPDATE history_sync_gaps SET attempts=?, no_improve_count=0, next_retry_at=NULL, "
            "missing_count=?, last_observed_at=?, last_run_id=? WHERE source=? AND interval=? "
            "AND symbol=? AND day_utc=? AND kind=?",
            (attempts, missing_after, iso(now), run_id, source, interval, symbol, day_utc,
             kind))
        return "improved"
    streak = int(row["no_improve_count"]) + 1
    if streak >= NO_IMPROVE_WEEK_AT:
        retry = iso(now + timedelta(days=7))
    elif streak >= NO_IMPROVE_DAY_AT:
        retry = iso(now + timedelta(hours=24))
    else:
        retry = None
    conn.execute(
        "UPDATE history_sync_gaps SET attempts=?, no_improve_count=?, next_retry_at=?, "
        "missing_count=COALESCE(?, missing_count), last_observed_at=?, last_run_id=? "
        "WHERE source=? AND interval=? AND symbol=? AND day_utc=? AND kind=?",
        (attempts, streak, retry, missing_after, iso(now), run_id, source, interval, symbol,
         day_utc, kind))
    return "no_improvement"


def due_gaps(conn, *, source, interval, now: datetime, kinds=("primary_exact", "unprocessed"),
             symbols: list[str] | None = None) -> list[sqlite3.Row]:
    """取得してよい open の gap (待ちが無い・待ちが過ぎた)。古い日が先。"""
    marks = ",".join("?" * len(kinds))
    rows = conn.execute(
        f"SELECT * FROM history_sync_gaps WHERE source=? AND interval=? AND state='open' "
        f"AND kind IN ({marks}) AND (next_retry_at IS NULL OR next_retry_at <= ?) "
        f"ORDER BY day_utc, symbol, kind", (source, interval, *kinds, iso(now))).fetchall()
    if symbols is not None:
        allowed = set(symbols)
        rows = [r for r in rows if r["symbol"] in allowed]
    return rows


def order_round_robin(rows: list[sqlite3.Row], symbols: list[str],
                      cursor: str | None) -> list[sqlite3.Row]:
    """銘柄を設定順の循環で回し、各銘柄の古い日から 1 件ずつ取り出す並びにする。

    cursor は直前に選んだ銘柄で、その次の銘柄から始める。1 銘柄の古い日が予算を独占しない。
    """
    if not rows:
        return []
    order = list(symbols)
    start = (order.index(cursor) + 1) % len(order) if cursor in order else 0
    rotated = order[start:] + order[:start]
    queues = {s: [r for r in rows if r["symbol"] == s] for s in rotated}
    out: list[sqlite3.Row] = []
    while any(queues.values()):
        for symbol in rotated:
            if queues[symbol]:
                out.append(queues[symbol].pop(0))
    return out


def count_gaps(conn, *, source, interval) -> dict[str, int]:
    counts = {"open": 0, "resolved": 0, "expired": 0}
    for row in conn.execute(
            "SELECT state, COUNT(*) AS n FROM history_sync_gaps WHERE source=? AND interval=? "
            "GROUP BY state", (source, interval)):
        counts[row["state"]] = row["n"]
    return counts


# ---- 保持 -----------------------------------------------------------------

def collect_garbage(conn, *, now: datetime, retention_days: int = RETENTION_DAYS) -> dict[str, int]:
    """詳細 (series) を 90 日で消す。run の行は集計として残し、参照されているものは
    最小 identity (tombstone) として必ず残す。未完 run・gap・系列状態・衝突から参照される run は
    series 詳細も含めて消さない。衝突は解決済みも消さない。"""
    cutoff = iso(now - timedelta(days=retention_days))
    referenced = set()
    for sql in ("SELECT first_run_id AS r FROM history_sync_gaps UNION "
                "SELECT last_run_id FROM history_sync_gaps UNION "
                "SELECT last_run_id FROM history_sync_series_state UNION "
                "SELECT first_run_id FROM history_conflicts UNION "
                "SELECT last_run_id FROM history_conflicts UNION "
                "SELECT bridge_verified_run_id FROM history_conflicts",):
        referenced.update(r[0] for r in conn.execute(sql) if r[0])
    old = [r["id"] for r in conn.execute(
        "SELECT id FROM history_sync_runs WHERE status != 'running' AND "
        "COALESCE(finished_at, started_at) < ?", (cutoff,))]
    series_deleted = tombstoned = 0
    for run_id in old:
        if run_id in referenced:
            conn.execute("UPDATE history_sync_runs SET tombstone=1, request_id=NULL, "
                         "unprocessed_json=NULL WHERE id=?", (run_id,))
            tombstoned += 1
            continue
        series_deleted += conn.execute("DELETE FROM history_sync_series WHERE run_id=?",
                                       (run_id,)).rowcount
        conn.execute("UPDATE history_sync_runs SET tombstone=1, request_id=NULL, "
                     "unprocessed_json=NULL WHERE id=?", (run_id,))
        tombstoned += 1
    return {"series_deleted": series_deleted, "runs_tombstoned": tombstoned}

