from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from agentic_fx.core.fx_time import server_epoch_to_utc, session_boundary_utc

TABLE = json.loads((Path(__file__).parents[2] / "mt5_bridge" / "tests" / "fixtures" /
                    "dst_rule_table.json").read_text())


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
def test_fx_time_matches_shared_dst_table(case_id, server_wall, expected):
    assert server_epoch_to_utc(int(datetime.fromisoformat(server_wall).timestamp())) == (
        datetime.fromisoformat(expected))


@pytest.mark.parametrize(("case_id", "day", "expected"), TABLE["session_boundaries"],
                         ids=[row[0] for row in TABLE["session_boundaries"]])
def test_session_boundaries_match_shared_dst_table(case_id, day, expected):
    assert session_boundary_utc(datetime.fromisoformat(day).date()) == datetime.fromisoformat(expected)
