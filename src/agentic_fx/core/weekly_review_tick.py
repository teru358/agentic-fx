"""週次の確定の独自 tick。

scheduler の tick からは `poll` を呼ぶだけで、重い処理は専用の 1 本の thread が行う
(発注の tick・日次 report の hook を塞がない)。thread は、運用開始週から猶予が過ぎた
未確定の週を暦から数え、古い順に 1 週ずつ処理する。

- 試行は「対象週 × UTC 日」で最大 3 回。投入の前に、回数・一意な試行 ID・実行状態を
  `alert_state` に確定保存する。例外で終わった試行は failed、前のプロセスが実行中のまま
  止まった試行は、起動後の最初の実行の冒頭で interrupted に直す (回数は足さない)。
- 窓の終端から 14 日を過ぎても確定していない週は、打切りとして記録し、以後は試行しない。
- 試行の key は、窓の終端から 14 日を過ぎたら消す。
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from agentic_fx._safe_error import safe_error_text
from agentic_fx.core import review_weeks
from agentic_fx.core.writer_admission import WriteDeferred
from agentic_fx.store import alert_state
from agentic_fx.store import weekly_review as store

_log = logging.getLogger(__name__)

MAX_ATTEMPTS_PER_DAY = 3
ABORT_AFTER = review_weeks.ABORT_AFTER
POLL_INTERVAL = timedelta(minutes=15)

RUNNING = "running"
DONE = "done"
FAILED = "failed"
INTERRUPTED = "interrupted"

Finalize = Callable[[sqlite3.Connection, date, datetime, datetime], object]
Report = Callable[[sqlite3.Connection, str], object]


def _day(now: datetime) -> str:
    return now.astimezone(timezone.utc).date().isoformat()


_STATES = frozenset({RUNNING, DONE, FAILED, INTERRUPTED})
CANCEL = "cancel"


def _parse_record(raw: object) -> dict | None:
    """試行記録。回数は 0 以上・上限以下で履歴の件数と一致し、ID は一意、状態は既知の
    ものだけ。外れる記録は None (呼び出し側はその日を上限到達として扱う)。"""
    try:
        record = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return None
    if not (isinstance(record, dict) and type(record.get("count")) is int
            and isinstance(record.get("attempts"), list)):
        return None
    attempts = record["attempts"]
    if not 0 <= record["count"] <= MAX_ATTEMPTS_PER_DAY or len(attempts) != record["count"]:
        return None
    if not all(isinstance(a, dict) and isinstance(a.get("id"), str)
               and isinstance(a.get("state"), str) and a["state"] in _STATES
               for a in attempts):
        return None
    if len({a["id"] for a in attempts}) != len(attempts):
        return None
    return record


class WeeklyReviewTick:
    def __init__(self, *, open_conn: Callable[[], sqlite3.Connection], finalize: Finalize,
                 report: Report | None = None, write_scope: Callable | None = None,
                 stop_event: threading.Event | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._open_conn = open_conn
        self._finalize = finalize
        self._report = report
        self._write_scope = write_scope
        self._stop_event = stop_event
        self._clock = clock
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_poll: datetime | None = None
        self._recovered = False
        # この process の試行 ID と、書込みを譲られて保留した記録の更新 (key, 試行 ID, 更新)
        self._own: set[str] = set()
        self._pending: list[tuple[str, str, str]] = []

    # ---- scheduler tick から -------------------------------------------------

    def poll(self, now: datetime) -> None:
        """thread が止まっていて前回の起動から `POLL_INTERVAL` 以上たっていれば起こす。
        待たず、送出しない。"""
        try:
            with self._lock:
                if self._thread is not None and self._thread.is_alive():
                    return
                if self._last_poll is not None and now - self._last_poll < POLL_INTERVAL:
                    return
                self._last_poll = now
                thread = threading.Thread(target=self._main, args=(now,),
                                          name="weekly-review", daemon=True)
                self._thread = thread
                thread.start()
        except Exception as exc:  # noqa: BLE001 - 発注の tick を巻き込まない
            _log.warning("weekly review poll failed: %s", safe_error_text(exc))

    def join(self, timeout: float | None = None) -> bool:
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _main(self, now: datetime) -> None:
        try:
            self.run_once(now)
        except Exception as exc:  # noqa: BLE001
            _log.warning("weekly review run failed: %s", safe_error_text(exc))

    # ---- 1 回の実行 --------------------------------------------------------------

    def _scope(self):
        return self._write_scope() if self._write_scope is not None else nullcontext()

    def _now(self, fallback: datetime) -> datetime:
        """週ごとに時計を読み直す (長い追いつきの間に UTC 日をまたぎうる)。"""
        return self._clock() if self._clock is not None else fallback

    def _stopped(self) -> bool:
        return self._stop_event is not None and self._stop_event.is_set()

    def run_once(self, now: datetime) -> None:
        """中断の回収 → 運用開始週 → 未確定の週の列挙と処理 → 古い key の掃除 → report。"""
        conn = self._open_conn()
        try:
            self._flush_pending(conn, now)
            if not self._recovered:
                self._recover(conn, now)
                self._recovered = True
            self._recover_own(conn, now)
            start = self._start_week(conn, now)
            recorded = store.recorded_weeks(conn)
            for label, close in review_weeks.due_weeks(now, first=start):
                if label.isoformat() in recorded:
                    continue
                current = self._now(now)
                if current > close + ABORT_AFTER:
                    self._abort(conn, label, close, current)
                    continue
                if self._stopped():
                    break
                self._attempt(conn, label, close, current)
            self._prune(conn, now)
            self._reports(conn, now)
        except WriteDeferred:
            _log.info("weekly review deferred: writer busy")
        finally:
            conn.close()

    def _start_week(self, conn: sqlite3.Connection, now: datetime) -> date:
        raw = alert_state.get(conn, alert_state.WEEKLY_REVIEW_START_KEY)
        try:
            saved = review_weeks.parse_label(json.loads(raw)["week"]) if raw else None
        except (ValueError, KeyError, TypeError):
            saved = None
        if saved is not None:
            return saved
        label = None
        if raw:
            # 保存済みの開始週が読めない。未確定の週を飛ばさない側に倒す。
            _log.warning("weekly review start week unreadable; re-deriving")
            recorded = [day for day in map(review_weeks.parse_label,
                                           store.recorded_weeks(conn)) if day is not None]
            label = min(recorded) if recorded else None
        if label is None:
            label = review_weeks.latest_due_week(now)
        if label is None:
            raise RuntimeError("no due week found")
        with self._scope():
            alert_state.set(conn, alert_state.WEEKLY_REVIEW_START_KEY,
                            json.dumps({"week": label.isoformat()}), now=now)
        return label

    def _abort(self, conn: sqlite3.Connection, label: date, close: datetime,
               now: datetime) -> None:
        with self._scope():
            store.commit_aborted(conn, label.isoformat(), window_end=close, finalized_at=now,
                                 reason=store.ABORT_PERIOD_EXCEEDED)

    # ---- 試行 -----------------------------------------------------------------------

    def _load(self, conn: sqlite3.Connection, key: str) -> dict | None:
        """試行記録。無ければ空の記録、あっても読めなければ None。"""
        raw = alert_state.get(conn, key)
        if raw is None:
            return {"count": 0, "attempts": []}
        return _parse_record(raw)

    def _save(self, conn: sqlite3.Connection, key: str, record: dict, now: datetime) -> None:
        with self._scope():
            alert_state.set(conn, key, json.dumps(record, sort_keys=True), now=now)

    def _update(self, conn: sqlite3.Connection, key: str, attempt: str, change: str,
                now: datetime) -> None:
        """試行 1 件の記録を更新する。`CANCEL` は記録から外して回数を戻す。"""
        current = self._load(conn, key)
        if current is None:
            return
        if change == CANCEL:
            current["attempts"] = [a for a in current["attempts"] if a["id"] != attempt]
            current["count"] = max(0, current["count"] - 1)
        else:
            for item in current["attempts"]:
                if item["id"] == attempt:
                    item["state"] = change
        self._save(conn, key, current, now)

    def _settle(self, conn: sqlite3.Connection, key: str, attempt: str, change: str,
                now: datetime) -> None:
        """更新が譲られたら保留に持ち、次の実行の冒頭で再適用する。"""
        try:
            self._update(conn, key, attempt, change, now)
        except WriteDeferred:
            self._pending.append((key, attempt, change))
            raise

    def _flush_pending(self, conn: sqlite3.Connection, now: datetime) -> None:
        while self._pending:
            key, attempt, change = self._pending[0]
            self._update(conn, key, attempt, change, now)
            self._pending.pop(0)

    def _attempt(self, conn: sqlite3.Connection, label: date, close: datetime,
                 now: datetime) -> None:
        key = alert_state.weekly_review_attempt_key(label.isoformat(), _day(now))
        record = self._load(conn, key)
        if record is None:
            # 壊れた記録で上限を 0 に戻さない。その週 x UTC 日は上限到達とみなす。
            _log.warning("weekly review attempt record unreadable; skipping this day")
            return
        if record["count"] >= MAX_ATTEMPTS_PER_DAY:
            return
        attempt = secrets.token_hex(8)
        record["count"] += 1
        record["attempts"].append({"id": attempt, "state": RUNNING})
        self._own.add(attempt)
        self._save(conn, key, record, now)
        state = DONE
        try:
            self._finalize(conn, label, close, now)
        except WriteDeferred:
            # 書込みを始められなかっただけで、試行には数えない。
            if conn.in_transaction:
                conn.rollback()
            self._settle(conn, key, attempt, CANCEL, now)
            raise
        except Exception as exc:  # noqa: BLE001 - 失敗は記録して次へ
            state = FAILED
            _log.warning("weekly review %s failed: %s", label, safe_error_text(exc))
            if conn.in_transaction:
                conn.rollback()
        self._settle(conn, key, attempt, state, now)

    def _recover(self, conn: sqlite3.Connection, now: datetime) -> None:
        for key in alert_state.keys_with_prefix(conn, alert_state.WEEKLY_REVIEW_ATTEMPT_PREFIX):
            record = _parse_record(alert_state.get(conn, key))
            if record is None:
                continue
            changed = False
            for item in record["attempts"]:
                if item["state"] == RUNNING:
                    item["state"] = INTERRUPTED
                    changed = True
            if changed:
                self._save(conn, key, record, now)

    def _recover_own(self, conn: sqlite3.Connection, now: datetime) -> None:
        """この process の試行で、更新できずに `running` のまま残ったものを回収する。
        実行の冒頭 (thread は 1 本だけなので、実行中の試行は無い)。"""
        if not self._own:
            return
        for key in alert_state.keys_with_prefix(conn, alert_state.WEEKLY_REVIEW_ATTEMPT_PREFIX):
            record = _parse_record(alert_state.get(conn, key))
            if record is None:
                continue
            changed = False
            for item in record["attempts"]:
                if item["state"] == RUNNING and item["id"] in self._own:
                    item["state"] = INTERRUPTED
                    changed = True
            if changed:
                self._save(conn, key, record, now)

    def _prune(self, conn: sqlite3.Connection, now: datetime) -> None:
        prefix = alert_state.WEEKLY_REVIEW_ATTEMPT_PREFIX
        for key in alert_state.keys_with_prefix(conn, prefix):
            label = review_weeks.parse_label(key[len(prefix):].split("|", 1)[0])
            close = review_weeks.week_close_for_label(label) if label is not None else None
            if close is None or now > close + ABORT_AFTER:
                with self._scope():
                    alert_state.delete(conn, key)

    def _reports(self, conn: sqlite3.Connection, now: datetime) -> None:
        """直近 14 日に確定・打切りした週の report が無ければ作り直す (冪等)。失敗は試行に数えない。"""
        if self._report is None:
            return
        for label in store.recent_weeks(conn, since=now - ABORT_AFTER):
            try:
                self._report(conn, label)
            except Exception as exc:  # noqa: BLE001
                _log.warning("weekly review report %s failed: %s", label, safe_error_text(exc))
