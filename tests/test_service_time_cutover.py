"""Startup wiring of the time-rule cutover."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_fx.core import time_cutover
from agentic_fx.core.contracts import FixedClock, Quote
from agentic_fx.service import build_app, run_init, run_service
from agentic_fx.setup.preflight import PreflightFailure
from agentic_fx.store.instance_lock import InstanceAlreadyRunning, acquire_instance_lock

from tests.store.test_rag import FakeEmbedding

NOW = datetime(2026, 11, 3, 21, 30, tzinfo=timezone.utc)


def _init(root: Path) -> None:
    (root / "config").mkdir()
    example = Path(__file__).parents[1] / "config" / "settings.yaml.example"
    (root / "config" / "settings.yaml.example").write_text(
        example.read_text(encoding="utf-8"), encoding="utf-8")
    with patch("agentic_fx.service.PriceProvider") as provider, \
         patch("agentic_fx.service._check_llama_swap"):
        provider.return_value.healthcheck.return_value = "yfinance"
        assert run_init(root) == 0


def _build(root: Path, now: datetime = NOW):
    return build_app(
        root, clock=FixedClock(now), embedding_fn=FakeEmbedding(),
        quote_fn=lambda pair: Quote(pair, 156.69, 156.71, now, "test"),
        bars_fn=lambda pair: None)


def _rows(root: Path):
    conn = sqlite3.connect(root / "data" / "agentic.db")
    try:
        marker = conn.execute(
            "SELECT applied_at FROM data_migrations WHERE name=?",
            (time_cutover.MARKER_NAME,)).fetchall()
        capture = conn.execute(
            "SELECT value FROM alert_state WHERE key=?",
            (time_cutover.CAPTURE_KEY,)).fetchall()
    finally:
        conn.close()
    return marker, capture


def test_first_start_writes_marker_capture_and_activity_before_returning(tmp_path):
    _init(tmp_path)
    app = _build(tmp_path)
    try:
        marker, capture = _rows(tmp_path)
        assert marker == [(NOW.isoformat(),)] and len(capture) == 1
        activity = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
        assert activity.count("\ttime_rule_cutover\t") == 1
    finally:
        app.close()
    app2 = _build(tmp_path, NOW.replace(hour=23))
    try:
        marker, _ = _rows(tmp_path)
        assert marker == [(NOW.isoformat(),)]
        activity = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
        assert activity.count("\ttime_rule_cutover\t") == 1
    finally:
        app2.close()


def test_cutover_is_decided_while_the_instance_lock_is_held(tmp_path):
    _init(tmp_path)
    seen: list[str] = []
    real = time_cutover.ensure_cutover

    def probing(conn, **kwargs):
        try:
            acquire_instance_lock(tmp_path / "data").close()
        except InstanceAlreadyRunning:
            seen.append("locked")
        return real(conn, **kwargs)

    with patch("agentic_fx.service.time_cutover.ensure_cutover", side_effect=probing):
        app = _build(tmp_path)
    app.close()
    assert seen == ["locked"]


def _remove_marker(root: Path) -> None:
    conn = sqlite3.connect(root / "data" / "agentic.db")
    conn.execute("DELETE FROM data_migrations WHERE name=?", (time_cutover.MARKER_NAME,))
    conn.commit()
    conn.close()


def test_capture_without_marker_stops_the_start_before_any_tick(tmp_path):
    _init(tmp_path)
    _build(tmp_path).close()
    _remove_marker(tmp_path)
    with patch("agentic_fx.core.scheduler.Scheduler.tick") as tick:
        with pytest.raises(PreflightFailure) as caught:
            _build(tmp_path, NOW.replace(hour=23))
    assert caught.value.code == "time_rule_marker_missing"
    tick.assert_not_called()
    acquire_instance_lock(tmp_path / "data").close()  # the lock was released
    marker, _ = _rows(tmp_path)
    assert marker == []


def _run_with_tick_that_stops(root: Path):
    stopped = threading.Event()

    def tick(self, *args, **kwargs):
        stopped.set()

    with patch("agentic_fx.core.scheduler.Scheduler.tick", autospec=True,
               side_effect=tick) as mock_tick:
        code = run_service(root, daemon=True, _stop_event=stopped)
    return code, mock_tick


def test_run_service_reports_the_missing_marker_with_a_dedicated_exit_code(tmp_path, capsys):
    _init(tmp_path)
    _build(tmp_path).close()
    _remove_marker(tmp_path)
    capsys.readouterr()
    code, tick = _run_with_tick_that_stops(tmp_path)
    assert code == 78 == time_cutover.EXIT_MARKER_MISSING
    tick.assert_not_called()
    assert "time_rule_marker_missing" in capsys.readouterr().err


def test_other_preflight_failures_keep_exit_code_one(tmp_path, capsys):
    _init(tmp_path)
    with patch("agentic_fx.service.build_app",
               side_effect=PreflightFailure("db_unreadable", "x")):
        code = run_service(tmp_path, daemon=True, _stop_event=threading.Event())
    assert code == 1
    assert "db_unreadable" in capsys.readouterr().err


def test_run_service_with_the_marker_does_reach_the_first_tick(tmp_path):
    _init(tmp_path)
    _build(tmp_path).close()
    code, tick = _run_with_tick_that_stops(tmp_path)
    assert code == 0
    assert tick.call_count >= 1
