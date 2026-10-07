"""拒否記録の集約は、複数 thread が同時に触っても欠落・二重計上・6 件目の個別記録を出さない。"""
from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime, timezone

from agentic_fx.ops.activity import SuppressedActivity

CATEGORY = "authentication_failed"


class _RacyCounts(defaultdict):
    """読んだ直後に他 thread と待ち合わせ、read-modify-write の重なりを決定的に起こす。"""

    def __init__(self, parties: int) -> None:
        super().__init__(int)
        self.barrier = threading.Barrier(parties)

    def __getitem__(self, key):
        value = super().__getitem__(key)
        try:
            self.barrier.wait(0.3)
        except threading.BrokenBarrierError:
            pass
        return value


def _activity(writes: list):
    return SuppressedActivity(
        wall_clock=lambda: datetime(2026, 10, 5, 0, 0, 10, tzinfo=timezone.utc),
        write=lambda category, event, summary: writes.append((event, summary)))


def _run_together(*calls) -> None:
    threads = [threading.Thread(target=call) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)
        assert not thread.is_alive()


def test_concurrent_records_do_not_lose_a_count():
    writes: list = []
    activity = _activity(writes)
    activity._counts = _RacyCounts(2)
    _run_together(lambda: activity.record(CATEGORY), lambda: activity.record(CATEGORY))
    assert activity._counts[CATEGORY] == 2


def test_concurrent_records_at_the_limit_emit_exactly_five_details():
    writes: list = []
    activity = _activity(writes)
    for _ in range(4):
        activity.record(CATEGORY)
    racy = _RacyCounts(2)
    dict.__setitem__(racy, CATEGORY, activity._counts[CATEGORY])
    activity._counts = racy
    _run_together(lambda: activity.record(CATEGORY), lambda: activity.record(CATEGORY))
    details = [w for w in writes if w[0] == CATEGORY]
    assert len(details) == 5


def test_record_during_a_flush_is_neither_lost_nor_counted_twice():
    writes: list = []
    total = 8

    def write(category, event, summary):
        writes.append((event, summary))
        if event == "suppressed" and not extra:
            thread = threading.Thread(target=lambda: activity.record(CATEGORY))
            extra.append(thread)
            thread.start()
            thread.join(2.0)

    extra: list = []
    activity = SuppressedActivity(
        wall_clock=lambda: datetime(2026, 10, 5, 0, 0, 10, tzinfo=timezone.utc), write=write)
    for _ in range(total - 1):
        activity.record(CATEGORY)
    activity.shutdown()
    activity.shutdown()
    details = len([w for w in writes if w[0] == CATEGORY])
    suppressed = sum(int(w[1].split(": ")[1].split()[0]) for w in writes
                     if w[0] == "suppressed")
    assert details + suppressed == total
