"""The scheduled improvement period is derived in one place, shared by the
supervisor and initial setup."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from agentic_fx.core.improve_supervisor import ImproveSupervisor
from agentic_fx.core.scheduler import scheduled_period
from agentic_fx.store import db as db_mod


def _settings(*, cadence: str, at: str, timezone: str, parallel: int = 1):
    return SimpleNamespace(
        schedule=SimpleNamespace(improve=cadence, improve_at=at),
        improve=SimpleNamespace(parallel=parallel),
        display_timezone=timezone,
    )


class _FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _supervisor_period_key(settings, now, tmp_path, monkeypatch) -> str:
    """Run a real supervisor tick and return the period_key of the wave it made."""
    db_path = tmp_path / "agentic.db"
    conn = db_mod.connect(db_path)
    db_mod.init_db(conn)
    sup = ImproveSupervisor(capacity=1, root=tmp_path, settings=settings,
                            clock=_FixedClock(now), db_path=db_path,
                            stop_event=threading.Event())
    sup._conn_for_test = conn
    monkeypatch.setattr(sup, "_spawn_slot_thread", lambda period_key, k: None)
    sup.tick(now)
    rows = conn.execute("SELECT period_key FROM improve_waves").fetchall()
    assert len(rows) == 1
    return rows[0]["period_key"]


_TOKYO = ZoneInfo("Asia/Tokyo")
_NY = ZoneInfo("America/New_York")
_WEEKLY_TOKYO = dict(cadence="weekly", at="Sat 03:00", timezone="Asia/Tokyo")
_WEEKLY_NY = dict(cadence="weekly", at="Sun 01:30", timezone="America/New_York")


@pytest.mark.parametrize(
    ("kwargs", "now", "expected_occurrence", "expected_key"),
    [
        (_WEEKLY_TOKYO, datetime(2026, 10, 2, 15, 0, tzinfo=_TOKYO),
         datetime(2026, 9, 26, 3, 0, tzinfo=_TOKYO), "2026-W39"),
        (_WEEKLY_TOKYO, datetime(2026, 10, 3, 3, 0, tzinfo=_TOKYO),
         datetime(2026, 10, 3, 3, 0, tzinfo=_TOKYO), "2026-W40"),
        (_WEEKLY_TOKYO, datetime(2026, 10, 3, 10, 0, tzinfo=_TOKYO),
         datetime(2026, 10, 3, 3, 0, tzinfo=_TOKYO), "2026-W40"),
        (dict(cadence="daily", at="03:00", timezone="Asia/Tokyo"),
         datetime(2026, 10, 2, 2, 59, tzinfo=_TOKYO),
         datetime(2026, 10, 1, 3, 0, tzinfo=_TOKYO), "2026-10-01"),
        # 米国の秋の DST 終了日: 01:30 が 2 回ある。fold が曖昧時刻の前後を分ける。
        (_WEEKLY_NY, datetime(2026, 11, 1, 1, 30, tzinfo=_NY, fold=0),
         datetime(2026, 11, 1, 1, 30, tzinfo=_NY, fold=0), "2026-W44"),
        (_WEEKLY_NY, datetime(2026, 11, 1, 1, 30, tzinfo=_NY, fold=1),
         datetime(2026, 11, 1, 1, 30, tzinfo=_NY, fold=0), "2026-W44"),
        # 米国の春の DST 開始日: 02:30 は存在しない。
        (dict(cadence="daily", at="02:30", timezone="America/New_York"),
         datetime(2026, 3, 8, 12, 0, tzinfo=_NY),
         datetime(2026, 3, 8, 2, 30, tzinfo=_NY), "2026-03-08"),
        # 米国の秋の DST 終了日: 01:30 は曖昧。
        (dict(cadence="daily", at="01:30", timezone="America/New_York"),
         datetime(2026, 11, 1, 12, 0, tzinfo=_NY),
         datetime(2026, 11, 1, 1, 30, tzinfo=_NY), "2026-11-01"),
    ],
)
def test_scheduled_period_equals_the_period_the_supervisor_tick_creates(
        kwargs, now, expected_occurrence, expected_key, tmp_path, monkeypatch):
    settings = _settings(**kwargs)

    period = scheduled_period(settings, now)

    assert period.occurrence == expected_occurrence
    assert period.period_key == expected_key
    assert _supervisor_period_key(settings, now, tmp_path, monkeypatch) == period.period_key


def test_scheduled_period_resolves_an_ambiguous_time_to_the_first_instant():
    """曖昧な局所時刻 (秋の DST 終了日の 01:30) では、now が前後どちらの
    瞬間でも occurrence は最初の瞬間 (UTC-4) になる。"""
    before = datetime(2026, 11, 1, 1, 30, tzinfo=_NY, fold=0)
    after = datetime(2026, 11, 1, 1, 30, tzinfo=_NY, fold=1)
    assert before.astimezone(timezone.utc) != after.astimezone(timezone.utc)

    for now in (before, after):
        period = scheduled_period(_settings(**_WEEKLY_NY), now)
        assert period.occurrence.utcoffset().total_seconds() == -4 * 3600
        assert period.occurrence.astimezone(timezone.utc) == datetime(
            2026, 11, 1, 5, 30, tzinfo=timezone.utc)


def test_scheduled_period_keeps_a_nonexistent_local_time_as_its_calendar_day():
    """春の DST 開始日に存在しない 02:30 でも、その日の期キーになる。"""
    period = scheduled_period(
        _settings(cadence="daily", at="02:30", timezone="America/New_York"),
        datetime(2026, 3, 8, 12, 0, tzinfo=_NY))

    assert period.period_key == "2026-03-08"
    assert (period.occurrence.hour, period.occurrence.minute) == (2, 30)
    assert period.occurrence.utcoffset().total_seconds() == -5 * 3600
