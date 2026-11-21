"""データの停止・再開待ちの通知記録 (datafeed_outage_notices) を ops_events へ出す。

呼び出しは ops lane の中だけ。event の INSERT と記録への event_id 保存は同じ transaction で
確定し、途中で失敗すれば両方戻す。重複は記録の event_id (終端結果は要求キーごと) と、通常遷移は
code・ref・guard_kind・generation・state の照合で抑える。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from agentic_fx.datafeed import notice_rules
from agentic_fx.store import alert_state

from .events import EventSchemaError, EventStore, MAX_POLL_LIMIT, data_feed_guard_event_id

_log = logging.getLogger(__name__)

PRODUCER_KEY = alert_state.DATAFEED_NOTICE_PRODUCER_KEY
# 1 回の回収で扱う記録の上限。残りは次の tick へ送る
BATCH_LIMIT = min(50, MAX_POLL_LIMIT)

# 同時刻の記録の並び: 停止 → 待ち → 終端結果 → 復帰
_RANK = {"stopped": 0, "awaiting_resume": 1, "resume_accepted": 2, "resume_rejected": 2,
         "resume_already_ready": 2, "resumed": 3}
_TERMINAL = frozenset({"resume_accepted", "resume_rejected", "resume_already_ready"})
_SKIP_LOG_LIMIT = 256
_skip_logged: set[tuple] = set()
# 保存に失敗した記録ごとの回数 (メモリだけ。次の tick で再試行する)
_failed_records: dict[tuple, int] = {}
_FAILURE_STREAK_LIMIT = 3


def _parse_utc(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _skip_schema(key: tuple) -> None:
    """schema に収まらない記録は固定語で飛ばす。同じ記録を毎 tick ログに出さない。"""
    if key in _skip_logged:
        return
    if len(_skip_logged) < _SKIP_LOG_LIMIT:
        _skip_logged.add(key)
    _log.warning("datafeed_notice_skipped reason=schema")


def _enabled_at(conn, now: datetime) -> tuple[datetime, bool]:
    stored = _parse_utc(alert_state.get(conn, PRODUCER_KEY))
    if stored is not None:
        return stored, False
    now = now.astimezone(timezone.utc)
    alert_state.set(conn, PRODUCER_KEY, now.isoformat(), now=now)
    return now, True


def ensure_enabled(conn, now: datetime) -> bool:
    """有効化の時刻を (無ければ) 置く。この呼出しで初めて置いたときだけ真。

    daemon の構築時に呼ぶ。ops lane の成否に依らないので、その後に作られる記録は下限より前にならない。"""
    return _enabled_at(conn, now)[1]


def producer_enabled_at(conn, now: datetime) -> datetime:
    """通知生成を有効にした daemon を初めて起動した時刻。無い (または読めない) ときだけ今を置く。

    置いた値は再起動・再導入でも更新しない。"""
    return _enabled_at(conn, now)[0]


def sweep(conn, events: EventStore, now: datetime) -> int:
    """下限以後の未 event 化の記録を、有限 batch で順に event にする。出した (または対応を付けた) 件数。

    失敗した記録は残して次の tick に回し、最初の失敗でこの回の回収を止める。"""
    floor = producer_enabled_at(conn, now)
    due = []
    for row in conn.execute(
            "SELECT epoch, notice_state, request_key, occurred_at FROM datafeed_outage_notices "
            "WHERE event_id IS NULL").fetchall():
        epoch, state, key = row["epoch"], row["notice_state"], row["request_key"]
        occurred = _parse_utc(row["occurred_at"])
        if (occurred is None or type(epoch) is not int or epoch < 0
                or state not in _RANK or not isinstance(key, str)):
            _skip_schema((epoch, state, key))
            continue
        if occurred >= floor:
            due.append((occurred, epoch, _RANK[state], key, state))
    due.sort()
    done = streak = 0
    for occurred, epoch, _rank, key, state in due:
        if done >= BATCH_LIMIT:
            break
        try:
            event_id = None if state in _TERMINAL else data_feed_guard_event_id(
                conn, generation=epoch, state=state)
            if event_id is None:
                event_id = events.insert_data_feed_guard(state, epoch, not_before=occurred)
            cur = conn.execute(
                "UPDATE datafeed_outage_notices SET event_id=? "
                "WHERE epoch=? AND notice_state=? AND request_key=? AND event_id IS NULL",
                (event_id, epoch, state, key))
            if cur.rowcount != 1:
                raise sqlite3.IntegrityError("notice record changed")
            conn.commit()
        except EventSchemaError:
            _rollback(conn)
            _skip_schema((epoch, state, key))
            continue
        except Exception:  # noqa: BLE001 -- 通知の失敗で状態更新・保護処理を止めない
            _rollback(conn)
            _failed_records[(epoch, state, key)] = _failed_records.get((epoch, state, key), 0) + 1
            if len(_failed_records) > _SKIP_LOG_LIMIT:
                _failed_records.clear()
            _log.warning("datafeed_notice_deferred")
            # 1 件の失敗は飛ばして後続を続ける。続けて失敗するなら DB 全体の障害とみなして止める
            streak += 1
            if streak >= _FAILURE_STREAK_LIMIT:
                return done
            continue
        _failed_records.pop((epoch, state, key), None)
        streak = 0
        done += 1
    return done


def _rollback(conn) -> None:
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001
        pass


def supplement(conn, now: datetime, *, introduced: bool = False) -> int:
    """導入時点で停止中の episode から、通常キーの記録を 0〜1 件だけ補う。

    優先順: 要求あり = 補完しない / 待ちの印あり = 待ち / confirmed または開場時間の停止条件 = 停止 /
    それ以外 (ready・restricted・条件未到達) = 補完しない。補った記録の発生時刻は導入時刻。
    `introduced` は daemon の構築時に有効化の時刻を初めて置いたことを表す。
    導入前の同じキーの未 event 化の記録は、その一件だけ導入時刻へ移す。導入後の記録・
    event 化済みの記録は動かさないので、再実行しても増えず時刻も動かない。"""
    floor = producer_enabled_at(conn, now)
    # 補った記録の発生時刻: 初回の導入時 (`introduced`) は導入時刻、後の再起動では実際に補った時刻
    # (条件が後から成立した停止を導入時刻へ遡らせない)
    made_at = floor if introduced else now.astimezone(timezone.utc)
    row = conn.execute(
        "SELECT state, epoch, confirmed, entered_degraded_at, resume_requested_at, "
        "resume_last_result_at, recovered_notified_epoch FROM datafeed_outage_state WHERE id=1").fetchone()
    if row is None or row["state"] != "degraded" or row["resume_requested_at"] is not None:
        return 0
    epoch = row["epoch"]
    if type(epoch) is not int or epoch < 0:
        return 0
    if row["recovered_notified_epoch"] == epoch:
        target = "awaiting_resume"
    elif row["confirmed"] or notice_rules.stop_after_open_minutes(
            notice_rules.timer_start(row["entered_degraded_at"], row["resume_last_result_at"]),
            now.astimezone(timezone.utc)):
        target = "stopped"
        if conn.execute(
                "SELECT 1 FROM datafeed_outage_notices WHERE epoch=? "
                "AND notice_state='awaiting_resume'", (epoch,)).fetchone() is not None:
            return 0
    else:
        return 0
    found = conn.execute(
        "SELECT occurred_at, event_id FROM datafeed_outage_notices "
        "WHERE epoch=? AND notice_state=? AND request_key=''", (epoch, target)).fetchone()
    try:
        if found is None:
            conn.execute(
                "INSERT INTO datafeed_outage_notices "
                "(epoch, notice_state, request_key, occurred_at, event_id) "
                "VALUES (?, ?, '', ?, NULL) ON CONFLICT (epoch, notice_state, request_key) "
                "DO NOTHING", (epoch, target, made_at.isoformat()))
        else:
            occurred = _parse_utc(found["occurred_at"])
            if found["event_id"] is not None or (occurred is not None and occurred >= floor):
                return 0
            conn.execute(
                "UPDATE datafeed_outage_notices SET occurred_at=? WHERE epoch=? "
                "AND notice_state=? AND request_key='' AND event_id IS NULL",
                (floor.isoformat(), epoch, target))
        conn.commit()
    except Exception:
        _rollback(conn)
        raise
    return 1
