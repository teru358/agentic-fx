from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.econ_window import EventView, WindowView
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.loops.summary import build_state_summary
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def test_summary_renders_only_the_pair_window_view_and_never_all_currency_events(tmp_path):
    conn = connect(tmp_path / "summary.db")
    init_db(conn)
    econ = MagicMock()
    econ.pair_currencies = {"USDJPY": ("USD", "JPY")}
    econ.window_view.return_value = WindowView(
        pair="USDJPY",
        currencies=("JPY", "USD"),
        in_window=True,
        active=(EventView("USD", "US high", 3, NOW),),
        next_event=EventView("USD", "US next", 3, NOW),
        freshness="fresh",
    )

    summary = build_state_summary(
        conn, PaperBroker(conn, SETTINGS, FixedClock(NOW)), econ,
        FixedClock(NOW), SETTINGS.paper.starting_balance)

    econ.window_view.assert_called_once_with("USDJPY", NOW)
    assert "USDJPY" in summary
    assert "US high" in summary
    assert "US next" in summary
    assert "CAD event" not in summary
    assert "USD low" not in summary
    assert "24h 以内の経済指標" not in summary


def test_summary_pair_view_keeps_freshness_and_no_event_state_visible(tmp_path):
    conn = connect(tmp_path / "summary.db")
    init_db(conn)
    econ = MagicMock()
    econ.pair_currencies = {"USDJPY": ("USD", "JPY")}
    econ.window_view.return_value = WindowView(
        pair="USDJPY",
        currencies=("JPY", "USD"),
        in_window=False,
        active=(),
        next_event=None,
        freshness="stale",
    )

    summary = build_state_summary(
        conn, PaperBroker(conn, SETTINGS, FixedClock(NOW)), econ,
        FixedClock(NOW), SETTINGS.paper.starting_balance)

    assert "freshness=stale" in summary
    assert "active=なし" in summary
    assert "next=なし" in summary


def test_a_failing_window_view_does_not_stop_the_summary(tmp_path, caplog):
    conn = connect(tmp_path / "summary.db")
    init_db(conn)
    econ = MagicMock()
    econ.pair_currencies = {"USDJPY": ("USD", "JPY"), "EURUSD": ("EUR", "USD")}
    ok = WindowView(pair="EURUSD", currencies=("EUR", "USD"), in_window=False,
                    active=(), next_event=None, freshness="fresh")
    econ.window_view.side_effect = [RuntimeError("db is locked"), ok]

    summary = build_state_summary(
        conn, PaperBroker(conn, SETTINGS, FixedClock(NOW)), econ,
        FixedClock(NOW), SETTINGS.paper.starting_balance)

    assert "USDJPY freshness=unknown" in summary
    assert "EURUSD currencies=EUR/USD" in summary
    assert "window_view failed for USDJPY" in caplog.text
