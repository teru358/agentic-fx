from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from agentic_fx.core.fx_time import server_epoch_to_utc, session_boundary_utc

TABLE = json.loads((Path(__file__).parents[2] / "mt5_bridge" / "tests" / "data" /
                    "dst_rule_table.json").read_text())


@pytest.mark.parametrize(("server_wall", "expected"), TABLE["server_to_utc"])
def test_fx_time_matches_shared_dst_table(server_wall, expected):
    assert server_epoch_to_utc(int(datetime.fromisoformat(server_wall).timestamp())) == (
        datetime.fromisoformat(expected))


@pytest.mark.parametrize(("day", "expected"), TABLE["session_boundaries"])
def test_session_boundaries_match_shared_dst_table(day, expected):
    assert session_boundary_utc(datetime.fromisoformat(day).date()) == datetime.fromisoformat(expected)
