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


@pytest.mark.parametrize(("server_wall", "expected"), TABLE["server_to_utc"])
def test_server_clock_rules_match_shared_dst_table(server_wall, expected):
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
