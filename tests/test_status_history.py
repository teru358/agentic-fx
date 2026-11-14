"""`afx status` の評価用履歴ブロック (合成 DB。本番の data/ は使わない)。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from agentic_fx import status, status_history
from agentic_fx.backtest import history_health as hh
from agentic_fx.config import load_settings_text
from agentic_fx.core.market_hours import is_market_open
from agentic_fx.store.db import connect, init_db
from tests.status_support import REPO, make_run, write_settings

pytestmark = pytest.mark.real_history_guard

UTC = timezone.utc
W = timedelta(minutes=5)
NOW = datetime(2026, 11, 10, 6, 2, 20, tzinfo=UTC)


def _settings(**backtest):
    text = (REPO / "config" / "settings.yaml.example").read_text(encoding="utf-8")
    settings = load_settings_text(text)
    settings.backtest.eval_source = "mt5"
    settings.backtest.base_interval = "5m"
    settings.backtest.holdout_months = 1
    settings.pairs = ["USDJPY"]
    for key, value in backtest.items():
        setattr(settings.backtest, key, value)
    return settings


def _fill(conn, symbol, *, until=datetime(2026, 11, 10, 6, 0, tzinfo=UTC), skip=lambda t: False,
          start=datetime(2026, 10, 1, tzinfo=UTC)):
    t, rows = start, []
    while t < until:
        if is_market_open(t) and not skip(t):
            rows.append((symbol, "5m", t.isoformat(), 148.0, 148.2, 147.9, 148.1, 1.0, "mt5"))
        t += W
    conn.executemany(
        "INSERT OR IGNORE INTO ohlcv_history (symbol, interval, bar_time, open, high, low, "
        "close, volume, source, spread) VALUES (?,?,?,?,?,?,?,?,?,NULL)", rows)
    conn.commit()


@pytest.fixture
def world(tmp_path):
    conn = connect(tmp_path / "data" / "agentic.db")
    init_db(conn)
    yield tmp_path, conn
    conn.close()


def _collect(world, settings=None):
    root, conn = world
    return status_history.collect(root, settings or _settings(), conn, "Asia/Tokyo", NOW)


def _block(world, settings=None, tz="Asia/Tokyo"):
    return "\n".join(status_history.render(_collect(world, settings), tz))


def test_a_healthy_history_is_ready_and_shows_times_in_the_display_zone(world):
    _fill(world[1], "USDJPY")
    text = _block(world)
    assert text.startswith("evaluation history: ready")
    assert "pair USDJPY: ready" in text
    assert "latest confirmed 2026-11-10 14:55:00 +09:00" in text       # 05:55Z を JST で
    assert "checked at 2026-11-10 15:02:20 +09:00" in text
    assert "present" in text and "expected" in text and "%" in text
    assert f"guard v{hh.GUARD_VERSION}" in text and hh.CALENDAR_VERSION in text


def test_a_stopped_update_shows_the_stale_reason_and_the_open_time_gap(world):
    _fill(world[1], "USDJPY", until=datetime(2026, 11, 4, tzinfo=UTC))
    text = _block(world)
    assert text.startswith("evaluation history: history_stale")
    assert "open-time gap" in text and "history_gap" in text


def test_a_day_missing_over_sixty_minutes_is_listed(world):
    _fill(world[1], "USDJPY", skip=lambda t: t.date().isoformat() == "2026-11-04"
          and t.hour < 6)
    text = _block(world)
    assert "history_gap" in text
    assert "days missing over 60 min: 2026-11-04" in text
    assert "worst day 2026-11-04 missing 72 bars (360 min)" in text


def _watching(*symbols):
    settings = _settings()
    settings.datafeed.watch_symbols = list(symbols)
    return settings


def test_reference_days_with_no_bars_are_shown_as_a_suspicion_only(world):
    conn = world[1]
    _fill(conn, "USDJPY")
    _fill(conn, "JP225", skip=lambda t: "2026-11-04" <= t.date().isoformat() <= "2026-11-09")
    text = _block(world, _watching("JP225"))
    assert f"references: {status_history.REFERENCES_HEADING}" in text
    assert status_history.REFERENCES_HEADING == "holdout 区間の 0 本日 (評価に使えるかは eval_window の表示を見る)"
    assert f"note: {status_history.REFERENCE_NOTE}" in text
    line = next(l for l in text.splitlines() if l.strip().startswith("JP225:"))
    for day in ("2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09"):
        assert day in line
    assert "2026-11-07" not in line                                   # 週末は主 pair にも足が無い
    assert text.startswith("evaluation history: ready")               # 採用停止には使わない
    assert "next:" not in text                                        # ready は参照 0 本日でも案内なし


def test_references_come_from_watch_symbols_without_touching_plugins(world, monkeypatch):
    from agentic_fx.tools import plugin_loader
    monkeypatch.setattr(plugin_loader, "approved_plugins",
                        lambda *a, **k: pytest.fail("plugin discovery must not run"))
    _fill(world[1], "USDJPY")
    _fill(world[1], "JP225")
    (world[0] / "plugins").mkdir()
    text = _block(world, _watching("JP225"))
    assert "JP225: none" in text
    assert "note:" not in text                                         # 0 本日が無ければ情報行も無い


def test_references_exclude_the_main_pairs_and_dedupe_in_settings_order(world):
    _fill(world[1], "USDJPY")
    _fill(world[1], "JP225")
    _fill(world[1], "US500")
    refs = _collect(world, _watching("US500", "USDJPY", "JP225", "US500")
                    ).evaluation_history.references
    assert [s.symbol for s in refs.symbols] == ["US500", "JP225"]


def test_a_watch_list_of_only_the_main_pair_or_empty_says_no_reference_watch_symbols(world):
    _fill(world[1], "USDJPY")
    for settings in (_watching(), _watching("USDJPY")):
        text = _block(world, settings)
        assert "references: no reference watch symbols" in text
        assert "none declared" not in text


def test_one_failing_reference_symbol_is_unavailable_alone(world, monkeypatch):
    _fill(world[1], "USDJPY")
    _fill(world[1], "JP225")
    _fill(world[1], "US500", skip=lambda t: t.date().isoformat() == "2026-11-04")
    real = status_history._bar_days

    def flaky(conn, dataset, symbol, start, end):
        if symbol == "JP225":
            raise sqlite3.OperationalError("secret-canary")
        return real(conn, dataset, symbol, start, end)

    monkeypatch.setattr(status_history, "_bar_days", flaky)
    text = _block(world, _watching("JP225", "US500"))
    assert "JP225: unavailable (reference_query_failed)" in text
    assert "US500: 2026-11-04" in text
    assert "secret-canary" not in text and "OperationalError" not in text
    assert text.startswith("evaluation history: ready")


def test_a_failing_main_pair_comparison_makes_only_the_references_unavailable(world, monkeypatch):
    _fill(world[1], "USDJPY")
    _fill(world[1], "JP225")
    real = status_history._bar_days

    def flaky(conn, dataset, symbol, start, end):
        if symbol == "USDJPY":
            raise sqlite3.OperationalError("boom")
        return real(conn, dataset, symbol, start, end)

    monkeypatch.setattr(status_history, "_bar_days", flaky)
    text = _block(world, _watching("JP225"))
    assert "references: unavailable (reference_query_failed)" in text
    assert text.startswith("evaluation history: ready") and "pair USDJPY: ready" in text


def test_next_is_conditional_on_the_current_verdict(world):
    _fill(world[1], "USDJPY", until=datetime(2026, 11, 4, tzinfo=UTC))
    stale = _block(world)
    assert "next: " in stale and "daemon を止めて `afx history import --source mt5`" in stale
    assert "reevaluate" not in stale
    root, conn = world
    path = root / "config" / "history-no-bars.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("entries: [", encoding="utf-8")
    broken = _block(world)
    assert "next: " in broken and "afx history import" not in broken
    assert "reevaluate" not in broken
    path.unlink()
    unconfigured = _block(world, _settings(eval_source="none"))
    assert "next:" not in unconfigured


def test_ready_has_no_next_and_never_recommends_reevaluation(world):
    _fill(world[1], "USDJPY")
    text = _block(world)
    assert text.startswith("evaluation history: ready")
    assert "next:" not in text and "reevaluate" not in text


def test_the_no_bars_section_shows_the_root_based_path_and_applied_entries(world):
    root, conn = world
    _fill(conn, "USDJPY", skip=lambda t: t.date().isoformat() == "2026-11-04")
    path = root / "config" / "history-no-bars.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"entries": [
        {"source": "mt5", "base_interval": "5m", "symbol": "USDJPY", "date_utc": "2026-11-04",
         "reason": "休場を確認", "approved_by": "operator",
         "approved_at": "2026-11-05T00:00:00+00:00"},
        {"source": "mt5", "base_interval": "5m", "symbol": "EURUSD", "date_utc": "2026-11-04",
         "reason": "別 pair", "approved_by": "operator",
         "approved_at": "2026-11-05T00:00:00+00:00"}]}), encoding="utf-8")
    text = _block(world)
    assert f"path={path}" in text and "file=present" in text
    assert "no-bars applied: 1 entries" in text and "休場を確認" in text and "別 pair" not in text
    assert "operator at 2026-11-05 09:00:00 +09:00" in text
    assert text.startswith("evaluation history: ready")


def test_the_no_bars_path_does_not_depend_on_the_working_directory(world, monkeypatch, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert f"path={world[0] / 'config' / 'history-no-bars.yaml'}" in _block(world)


def test_the_repo_ignores_the_real_list_and_tracks_an_example_that_loads_as_empty():
    lines = status_history._no_bars_lines(status_history._no_bars_info(REPO, None, False), "UTC")
    assert "gitignored=yes" in lines[0] and "example=present" in lines[0]
    example = REPO / "config" / "history-no-bars.yaml.example"
    assert hh.load_no_bars(example).entries == ()


def test_a_broken_list_makes_the_block_unavailable_and_says_why(world):
    root, conn = world
    _fill(conn, "USDJPY")
    path = root / "config" / "history-no-bars.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("entries: [", encoding="utf-8")
    text = _block(world)
    assert text.startswith("evaluation history: unavailable")
    assert "no-bars list: unavailable" in text
    assert "reason: history_check_unavailable" in text
    assert "NoBarsConfigError" not in text


@pytest.mark.parametrize("settings,use_conn", [(None, True), ("ok", False)])
def test_unreadable_inputs_make_only_this_block_unavailable(world, settings, use_conn):
    root, conn = world
    result = status_history.collect(root, None if settings is None else _settings(),
                                    conn if use_conn else None, "UTC", NOW)
    lines = status_history.render(result, "UTC")
    assert lines[0] == status_history.UNAVAILABLE_LINE
    expected = "settings_unavailable" if settings is None else "database_unavailable"
    assert lines[1] == f"  reason: {expected}"


def test_an_unconfigured_source_is_reported_not_failed(world):
    text = _block(world, _settings(eval_source="none"))
    assert text.startswith("evaluation history: not configured")


def test_an_unexpected_failure_is_contained_in_the_block(world, monkeypatch):
    monkeypatch.setattr(hh, "evaluate_history",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    lines = _block(world).splitlines()
    assert lines[0] == status_history.UNAVAILABLE_LINE
    assert lines[1] == "  reason: history_check_unavailable"
    assert "boom" not in "\n".join(lines) and "RuntimeError" not in "\n".join(lines)


def test_status_without_a_daemon_renders_the_block_and_keeps_the_rest(tmp_path):
    write_settings(tmp_path, "ok")
    text = (tmp_path / "config" / "settings.yaml").read_text(encoding="utf-8")
    (tmp_path / "config" / "settings.yaml").write_text(
        text.replace('eval_source: "none"', 'eval_source: "mt5"', 1), encoding="utf-8")
    conn = connect(tmp_path / "data" / "agentic.db")
    init_db(conn)
    _fill(conn, "USDJPY")
    conn.close()
    data = status.collect_status(tmp_path, run=make_run(), instance="free", now=NOW)
    out = status.render_status(data, root=tmp_path)
    assert "evaluation history:" in out and "agentic-fx offline status" in out
    assert out.index("evaluation history:") < out.index("note:")
    assert "database: ok" in out


def test_status_survives_a_missing_database(tmp_path):
    write_settings(tmp_path, "ok")
    data = status.collect_status(tmp_path, run=make_run(), instance="free", now=NOW)
    assert data.evaluation_history[0].startswith("evaluation history: unavailable")
    assert "instance: free" in status.render_status(data, root=tmp_path)


def test_a_stored_bar_without_a_timezone_makes_latest_stored_unavailable(world):
    _fill(world[1], "USDJPY")
    world[1].execute(
        "INSERT INTO ohlcv_history (symbol, interval, bar_time, open, high, low, close, "
        "volume, source, spread) VALUES ('USDJPY','5m','2026-11-10T09:00:00',1,1,1,1,1,'mt5',NULL)")
    world[1].commit()
    text = _block(world)
    assert "latest stored unavailable; latest confirmed 2026-11-10 14:55:00 +09:00" in text


def test_the_result_is_a_typed_dto_with_separate_slots(world):
    _fill(world[1], "USDJPY")
    result = _collect(world)
    assert isinstance(result, status_history.HistoryStatus)
    assert result.observed_at == NOW
    assert result.history_sync == status_history.NOT_COLLECTED
    assert result.eval_window == status_history.NOT_COLLECTED
    block = result.evaluation_history
    assert block.state is status_history.State.READY
    assert block.holdout_start.tzinfo is not None and block.checked_at == NOW
    assert block.references.kind == "holdout_zero_bar_days"
    assert not hasattr(block.references, "ready") and not hasattr(block.references, "evaluable")
    assert block.next_actions == ()


def test_the_dto_keeps_utc_values_and_the_renderer_converts(world):
    _fill(world[1], "USDJPY")
    result = _collect(world)
    assert result.evaluation_history.pairs[0].latest_confirmed.utcoffset() == timedelta(0)
    assert "+09:00" in "\n".join(status_history.render(result, "Asia/Tokyo"))
    assert "+09:00" not in "\n".join(status_history.render(result, "UTC"))
