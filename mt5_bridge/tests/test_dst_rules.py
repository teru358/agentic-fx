from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mt5_client import server_epoch_to_utc, utc_to_server_epoch

UTC = timezone.utc
TABLE = json.loads((Path(__file__).parent / "fixtures" / "dst_rule_table.json").read_text())


def _epoch(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp())


_YEAR_END_DAYS = ["2025-12-24", "2025-12-25", "2025-12-26", "2025-12-31", "2026-01-01", "2026-01-02",
                  "2026-12-24", "2026-12-25", "2026-12-26", "2026-12-31", "2027-01-01", "2027-01-02"]

# 設計の必須の件。表から行が消えても、ここに残るので検出できる。
REQUIRED_CONVERSION_IDS = {
    "summer-fri-close-1m-before", "summer-fri-close",
    "summer-sunday-open-1m-before", "summer-sunday-open",
    "winter-fri-close-1m-before", "winter-fri-close",
    "winter-sunday-open-1m-before", "winter-sunday-open",
    "fall-2026-sunday-open-1m-before", "fall-2026-sunday-open",
    "spring-2026-sunday-open-1m-before", "spring-2026-sunday-open",
    "spring-2027-sunday-open-1m-before", "spring-2027-sunday-open",
    "spring-2026-1m-before", "spring-2026-nonexistent-0200", "spring-2026-nonexistent-0230",
    "spring-2026-first-real-0300",
    "fall-2026-1m-before", "fall-2026-ambiguous-0130", "fall-2026-0200-est",
    "spring-2027-1m-before", "spring-2027-nonexistent-0200", "spring-2027-nonexistent-0230",
    "spring-2027-first-real-0300",
} | {f"yearend-{day}-{hhmm}" for day in _YEAR_END_DAYS for hhmm in ("0000", "1200", "2359")}
REQUIRED_SESSION_IDS = {
    "session-summer-2026-07-24", "session-winter-2026-11-06", "session-winter-2026-03-06",
    "session-spring-2026-03-09", "session-winter-2027-03-12", "session-spring-2027-03-15",
} | {f"session-yearend-{day}" for day in _YEAR_END_DAYS}


def test_shared_table_has_every_required_case():
    conversion_ids = [row[0] for row in TABLE["server_to_utc"]]
    session_ids = [row[0] for row in TABLE["session_boundaries"]]
    assert len(set(conversion_ids)) == len(conversion_ids)
    assert len(set(session_ids)) == len(session_ids)
    assert REQUIRED_CONVERSION_IDS <= set(conversion_ids)
    assert REQUIRED_SESSION_IDS <= set(session_ids)


@pytest.mark.parametrize(("case_id", "server_wall", "expected"), TABLE["server_to_utc"],
                         ids=[row[0] for row in TABLE["server_to_utc"]])
def test_server_clock_rules_match_shared_dst_table(case_id, server_wall, expected):
    assert server_epoch_to_utc(_epoch(server_wall)) == datetime.fromisoformat(expected)


def test_server_clock_rule_rejects_non_utc_instants():
    with pytest.raises(TypeError):
        utc_to_server_epoch(datetime(2026, 7, 24, 20, 59))
    with pytest.raises(TypeError):
        utc_to_server_epoch(datetime(2026, 7, 25, 5, 59,
                                     tzinfo=timezone(timedelta(hours=9))))


def test_server_clock_rule_round_trips_a_regular_utc_instant():
    instant = datetime(2026, 11, 2, 22, 1, tzinfo=UTC)
    assert server_epoch_to_utc(utc_to_server_epoch(instant)) == instant
