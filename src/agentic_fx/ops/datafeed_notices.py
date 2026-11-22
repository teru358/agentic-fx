"""データの停止・再開待ちの通知記録 (datafeed_outage_notices) を ops_events へ出す。

呼び出しは ops lane の中だけ。event の INSERT と記録への event_id 保存は同じ transaction で
確定し、途中で失敗すれば両方戻す。重複は記録の event_id (終端結果は要求キーごと) と、通常遷移は
code・ref・guard_kind・generation・state の照合で抑える。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from agentic_fx.datafeed import notice_rules
from agentic_fx.store import alert_state

from .events import EventSchemaError, EventStore, MAX_POLL_LIMIT, data_feed_guard_event_id

_log = logging.getLogger(__name__)

PRODUCER_KEY = alert_state.DATAFEED_NOTICE_PRODUCER_KEY
# 接触・不明の停止通知の記録は通常の停止と同じ notice_state で、request_key だけが異なる。
# event では別の語にして、同じ epoch に通常の停止があっても抑止・重複排除されないようにする
CONTACT_NOTICE_KEY = "contact"
CONTACT_EVENT_STATE = "contact_stopped"
# 1 回の回収で扱う記録の上限。残りは次の tick へ送る
BATCH_LIMIT = min(50, MAX_POLL_LIMIT)

# 同時刻の記録の並び: 停止 → 待ち → 終端結果 → 復帰
_RANK = {"stopped": 0, "awaiting_resume": 1, "resume_accepted": 2, "resume_rejected": 2,
         "resume_already_ready": 2, "resumed": 3}
_TERMINAL = frozenset({"resume_accepted", "resume_rejected", "resume_already_ready"})
_SKIP_LOG_LIMIT = 256
_skip_logged: set[tuple] = set()
# 1 回の回収で許す失敗の件数 (DB 全体の障害で同じ回に無駄打ちしない)
_FAILURES_PER_PASS = 3
_RETRY_BASE_SEC = 60
_RETRY_MAX_SEC = 3600


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


class Retries:
    """保存に失敗した記録ごとの再試行時刻 (メモリだけ)。1 回目は次の回収で試し、2 回目以降は
    指数で間隔を伸ばして上限で止める。その時刻までは飛ばして後続を通す。"""

    def __init__(self) -> None:
        self._failures: dict[tuple, tuple[int, datetime]] = {}

    def due(self, key: tuple, now: datetime) -> bool:
        item = self._failures.get(key)
        return item is None or now >= item[1]

    def failed(self, key: tuple, now: datetime) -> None:
        count = (self._failures[key][0] if key in self._failures else 0) + 1
        delay = 0 if count == 1 else min(_RETRY_BASE_SEC * 2 ** (count - 2), _RETRY_MAX_SEC)
        if len(self._failures) >= _SKIP_LOG_LIMIT * 4 and key not in self._failures:
            self._failures.clear()
        self._failures[key] = (count, now + timedelta(seconds=delay))

    def succeeded(self, key: tuple) -> None:
        self._failures.pop(key, None)


def producer_enabled_at(conn, now: datetime) -> datetime:
    """通知生成を有効にした daemon を初めて起動した時刻。無い (または読めない) ときだけ今を置く。

    置いた値は再起動・再導入でも更新しない。"""
    return _enabled_at(conn, now)[0]


def sweep(conn, events: EventStore, now: datetime, *, retries: Retries | None = None,
          enabled_hint: datetime | None = None) -> int:
    """下限以後の未 event 化の記録を、有限 batch で順に event にする。出した (または対応を付けた) 件数。

    失敗した記録は残し、記録ごとの再試行時刻まで飛ばして後続を通す。1 回の回収の失敗は
    ``_FAILURES_PER_PASS`` 件まで。`enabled_hint` は構築時刻で、下限が保存できていないときの
    置く値 (回収時刻で置き換えない)。"""
    retries = retries if retries is not None else Retries()
    floor = producer_enabled_at(conn, enabled_hint or now)
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
    done = failures = 0
    for occurred, epoch, _rank, key, state in due:
        if done >= BATCH_LIMIT:
            break
        if not retries.due((epoch, state, key), now):
            continue
        event_state = CONTACT_EVENT_STATE if (
            state == "stopped" and key == CONTACT_NOTICE_KEY) else state
        try:
            event_id = None if state in _TERMINAL else data_feed_guard_event_id(
                conn, generation=epoch, state=event_state)
            if event_id is None:
                event_id = events.insert_data_feed_guard(event_state, epoch, not_before=occurred)
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
            retries.failed((epoch, state, key), now)
            _log.warning("datafeed_notice_deferred")
            failures += 1
            if failures >= _FAILURES_PER_PASS:
                return done
            continue
        retries.succeeded((epoch, state, key))
        done += 1
    return done


def _rollback(conn) -> None:
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001
        pass


def snapshot_target(conn, now: datetime) -> tuple[int, str] | None:
    """いま補完の対象になる記録 (epoch, 状態)。優先順: 要求あり = なし / 待ちの印あり = 待ち /
    confirmed または開場時間の停止条件 = 停止 / それ以外 (ready・restricted・条件未到達) = なし。
    読むだけ。"""
    row = conn.execute(
        "SELECT state, epoch, confirmed, entered_degraded_at, resume_requested_at, "
        "resume_last_result_at, recovered_notified_epoch FROM datafeed_outage_state WHERE id=1").fetchone()
    if row is None or row["state"] != "degraded" or row["resume_requested_at"] is not None:
        return None
    epoch = row["epoch"]
    if type(epoch) is not int or epoch < 0:
        return None
    if row["recovered_notified_epoch"] == epoch:
        return epoch, "awaiting_resume"
    if row["confirmed"] or notice_rules.stop_after_open_minutes(
            notice_rules.timer_start(row["entered_degraded_at"], row["resume_last_result_at"]),
            now.astimezone(timezone.utc)):
        if conn.execute(
                "SELECT 1 FROM datafeed_outage_notices WHERE epoch=? "
                "AND notice_state='awaiting_resume'", (epoch,)).fetchone() is not None:
            return None
        return epoch, "stopped"
    return None


def supplement(conn, now: datetime, *, snapshot: tuple[int, str] | None = None,
               enabled_hint: datetime | None = None) -> int:
    """導入時点で停止中の episode から、通常キーの記録を 0〜1 件だけ補う (対象は `snapshot_target`)。

    補った記録の発生時刻は、有効化の snapshot の時点で既に成り立っていた対象 (`snapshot` と
    同じ epoch・状態) だけ導入時刻、それ以外 (snapshot の後に成り立った停止、後の再起動の補完) は
    実際に補った時刻。導入前の同じキーの未 event 化の記録は、その一件だけ導入時刻へ移す。
    導入後の記録・event 化済みの記録は動かさないので、再実行しても増えず時刻も動かない。"""
    floor = producer_enabled_at(conn, enabled_hint or now)
    found_target = snapshot_target(conn, now)
    if found_target is None:
        return 0
    epoch, target = found_target
    made_at = floor if snapshot == found_target else now.astimezone(timezone.utc)
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
