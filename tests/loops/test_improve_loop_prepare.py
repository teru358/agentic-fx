"""ImproveLoop.prepare (設計書 §4 冒頭, プラン §8.1-6/11/23)。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from agentic_fx.loops.improve_run_context import ImproveRunContext
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger


def test_improve_run_context_is_frozen_dataclass():
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    ctx = ImproveRunContext(
        mission_id=1, run_id=2, staging_dir=Path("/tmp/staging"),
        source_snapshot_dir=Path("/tmp/source"),
        allowed_backlog_ids=frozenset({1, 2}), slot_key=("2026-W34", 0),
        ledger=ledger, rpc_handlers={})
    assert ctx.mission_id == 1
    with pytest.raises(AttributeError):
        ctx.mission_id = 99  # frozen
