"""The adoption gate refuses a reference strategy whose reference has no history."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentic_fx.backtest.dataset import HistoryDataset
from agentic_fx.backtest.holdout import NoHistoryError, ReferenceHistoryMissingError
from agentic_fx.plugin.resolve import ResolvedIndicatorSet
from agentic_fx.plugin.strategy_gate import evaluate_strategy_adoption_gate
from tests.backtest.factories import _conn
from tests.plugin.test_signal_producer_reference import _item, _meta
from tests.plugin.test_strategy_gate import _SETTINGS, _metrics

NOW = datetime(2026, 8, 22, tzinfo=timezone.utc)


def _resolved(*items):
    return ResolvedIndicatorSet(inventory_root=Path("/nonexistent"), items=tuple(items),
                                all_pinned=True)


def _call(conn, resolved, *, record=None):
    return evaluate_strategy_adoption_gate(
        conn, meta=_meta(), name="myst", pairs=["USDJPY"], timeframe="1h",
        resolved=resolved, content_hash="h1", now=NOW, settings=_SETTINGS,
        record_fn=record)


def test_a_reference_with_no_history_stops_the_gate_before_any_run_or_row(
        tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    ran, rows = [], []
    monkeypatch.setattr("agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
                        lambda *a, **kw: ran.append(1) or _metrics(trades=40, pf=1.5))
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.strategy_adapter.build_intent_source",
        lambda meta, **kw: ran.append("built") or MagicMock(close=lambda: None))
    with pytest.raises(ReferenceHistoryMissingError,
                       match=r"^reference_history_missing\(JP225, 1h, no_rows\)$"):
        _call(conn, _resolved(_item()), record=rows.append)
    assert ran == [] and rows == []
    assert conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 0
    assert issubclass(ReferenceHistoryMissingError, NoHistoryError)
    conn.close()


def test_the_gate_runs_when_the_reference_has_any_history(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO ohlcv_history (symbol, interval, bar_time, open, high, low, close,"
        " volume, source) VALUES ('JP225', '1m', '2026-01-01T00:00:00+00:00', 1, 1, 1, 1,"
        " 0, 'dukascopy')")
    monkeypatch.setattr("agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
                        lambda *a, **kw: _metrics(trades=5, pf=1.0))
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.strategy_adapter.build_intent_source",
        lambda meta, **kw: MagicMock(close=lambda: None))
    verdict = _call(conn, _resolved(_item()))
    assert verdict.evaluable is False and verdict.observation_reason.startswith(
        "insufficient_trades")
    conn.close()


def test_a_self_only_strategy_is_not_checked(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr("agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
                        lambda *a, **kw: _metrics(trades=5, pf=1.0))
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.strategy_adapter.build_intent_source",
        lambda meta, **kw: MagicMock(close=lambda: None))
    assert _call(conn, _resolved(_item("s", symbol="self", mode="self"))).evaluable is False
    conn.close()
