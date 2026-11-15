"""`afx status` の history_sync ブロック (同期の永続表の観測、合成 DB)。"""
from __future__ import annotations

import ast
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx import status_history as sh
from agentic_fx.config import load_settings_text
from agentic_fx.store import history_conflicts as hc
from agentic_fx.store import history_sync as sync_store
from agentic_fx.store.db import connect, init_db
from tests.status_support import REPO
from tests.test_status_history import _fill, _settings

pytestmark = pytest.mark.real_history_guard

UTC = timezone.utc
NOW = datetime(2026, 11, 10, 6, 2, 20, tzinfo=UTC)
SRC = REPO / "src" / "agentic_fx"


@pytest.fixture
def world(tmp_path):
    conn = connect(tmp_path / "data" / "agentic.db")
    init_db(conn)
    _fill(conn, "USDJPY")
    yield tmp_path, conn
    conn.close()


def _collect(world, settings=None):
    root, conn = world
    return sh.collect(root, settings or _settings(), conn, "Asia/Tokyo", NOW)


def _text(world, settings=None, tz="Asia/Tokyo"):
    return "\n".join(sh.render(_collect(world, settings), tz))


def _run(conn, run_id, *, status="ok", rc=0, trigger="manual", started=NOW, failure=None,
         requests=3, request_id=None):
    sync_store.insert_run(
        conn, run_id=run_id, owner="o", trigger=trigger, source="mt5", interval="5m",
        calendar_version="c", lookback_days=7, epoch="none", t=started, b=started - timedelta(days=7),
        e=started, started_at=started, request_id=request_id)
    sync_store.finish_run(conn, run_id, status=status, rc=rc, finished_at=started + timedelta(minutes=1),
                          requests=requests, inserted=5, unchanged=7, failure_code=failure)
    conn.commit()


def _gap(conn, symbol, day, kind, *, state="open", detail=None, missing=3):
    sync_store.observe_gap(conn, source="mt5", interval="5m", symbol=symbol, day_utc=day, kind=kind,
                           missing_count=missing, detail=detail, run_id=None, now=NOW)
    if state != "open":
        conn.execute("UPDATE history_sync_gaps SET state=? WHERE symbol=? AND day_utc=? AND kind=?",
                     (state, symbol, day, kind))
    conn.commit()


def _conflict(conn, state="open"):
    hc.upsert(conn, source="mt5", interval="5m", symbol="USDJPY", bar_time="2026-11-09T01:00:00+00:00",
              history_epoch="none", existing=(1.0, 2.0, 0.5, 1.5, 1.0, None),
              incoming=(1.0, 2.0, 0.5, 1.6, 1.0, None), origin="sync", seen_at=NOW.isoformat())
    conn.execute("UPDATE history_conflicts SET state=?", (state,))
    conn.commit()


def test_without_the_sync_tables_the_block_is_not_initialized_and_asks_for_a_restart(world):
    root, conn = world
    for name in ("history_sync_runs", "history_sync_series", "history_sync_series_state",
                 "history_sync_gaps", "history_sync_daily", "history_sync_resume"):
        conn.execute(f"DROP TABLE {name}")
    conn.commit()
    result = _collect(world)
    assert result.history_sync.state is sh.SyncState.NOT_INITIALIZED
    text = _text(world)
    assert "history sync: not initialized" in text
    assert "daemon を再起動して同期の schema を導入" in text
    assert "last run" not in text
    assert result.evaluation_history.state is sh.State.READY


def test_a_fresh_database_has_never_run_and_a_ready_history_has_no_next(world):
    result = _collect(world)
    sync = result.history_sync
    assert sync.state is sh.SyncState.NEVER_RUN and sync.next_actions == ()
    text = "\n".join(sh.render(result, "Asia/Tokyo"))
    assert "history sync: never_run" in text
    assert "対象は holdout 内と直近。in-sample は作らない" in text
    assert "last run: none" in text and "last success: none" in text
    assert "  next:" not in text
    assert "evaluation history: ready" in text


def test_effective_state_is_disabled_until_a_worker_and_the_measurement_exist(world):
    on = _collect(world).history_sync
    assert on.auto_enabled is True and on.effective_enabled is False
    assert on.effective_reason == "measurement_not_accepted"
    assert on.measurement == "not_measured" and on.worker == "not_running"
    settings = _settings()
    settings.backtest.history_sync.auto_enabled = False
    off = _collect(world, settings).history_sync
    assert off.auto_enabled is False and off.effective_enabled is False
    assert off.effective_reason == "auto_disabled"
    text = _text(world, settings)
    assert "auto_enabled=false lookback_days=7; effective: disabled (auto_disabled)" in text
    assert "self-measurement: not_measured; worker: not_running" in text


def test_a_non_mt5_dataset_is_not_applicable(world):
    result = _collect(world, _settings(eval_source="dukascopy"))
    assert result.history_sync.state is sh.SyncState.NOT_APPLICABLE
    none = sh.collect(world[0], _settings(eval_source="none"), world[1], "UTC", NOW)
    assert none.history_sync.state is sh.SyncState.NOT_APPLICABLE
    assert "last run" not in "\n".join(sh.render(result, "UTC"))


def test_the_last_run_and_the_last_success_are_shown_separately_in_the_display_zone(world):
    root, conn = world
    _run(conn, "run-ok", started=NOW - timedelta(hours=3), request_id="req-1", trigger="manual")
    _run(conn, "run-bad", status="failed", rc=1, started=NOW - timedelta(hours=1),
         failure="bridge_unavailable")
    sync = _collect(world).history_sync
    assert sync.state is sh.SyncState.ATTENTION
    assert sync.last_run.run_id == "run-bad" and sync.last_success.run_id == "run-ok"
    text = _text(world)
    last = next(l for l in text.splitlines() if l.startswith("  last run:"))
    ok = next(l for l in text.splitlines() if l.startswith("  last success:"))
    assert "status=failed rc=1 run=run-bad trigger=manual error=bridge_unavailable" in last
    assert "started 2026-11-10 14:02:20 +09:00" in last and "requests=3" in last
    assert "run=run-ok request=req-1" in ok
    assert "daemon を止めて `afx history sync` を実行" in text


def test_series_cursors_gaps_expired_days_and_conflicts_are_counted_for_this_dataset_only(world):
    root, conn = world
    _run(conn, "r1")
    sync_store.advance_cursor(conn, "mt5", "5m", "USDJPY", last_processed_e=NOW - timedelta(hours=2),
                              run_id="r1", now=NOW)
    sync_store.record_series_failure(conn, "mt5", "5m", "JP225", error_code="bridge_unavailable",
                                     run_id="r1", now=NOW)
    sync_store.set_rr_cursor(conn, "mt5", "5m", "USDJPY", now=NOW)
    _gap(conn, "USDJPY", "2026-11-05", "primary_exact")
    _gap(conn, "USDJPY", "2026-11-06", "unprocessed")
    _gap(conn, "JP225", "2026-11-04", "reference_suspected", detail={"median_fill_ppm": 987000})
    _gap(conn, "USDJPY", "2026-10-01", "primary_exact", state="expired")
    _gap(conn, "JP225", "2026-10-01", "reference_suspected", state="expired")
    _gap(conn, "USDJPY", "2026-11-01", "primary_exact", state="resolved")
    _gap(conn, "USDJPY", "2026-11-02", "primary_exact")
    conn.execute("UPDATE history_sync_gaps SET source='dukascopy' WHERE day_utc='2026-11-02'")
    _conflict(conn)
    _conflict(conn, state="kept")
    sync = _collect(world).history_sync
    assert dict(sync.open_gaps) == {"primary_exact": 1, "reference_suspected": 1, "unprocessed": 1}
    assert sync.expired_days == 1 and sync.open_conflicts == 0
    by_symbol = {s.symbol: s for s in sync.series}
    assert set(by_symbol) == {"USDJPY", "JP225"}                       # __control__ は出さない
    assert by_symbol["JP225"].last_error == "bridge_unavailable"
    assert by_symbol["JP225"].next_retry_at is not None
    text = _text(world)
    assert "series USDJPY: cursor 2026-11-10 13:02:20 +09:00 attempts=0" in text
    assert "series JP225: cursor uninitialized attempts=1" in text
    assert "open gaps: primary_exact=1 reference_suspected=1 unprocessed=1" in text
    assert "expired: holdout 外、自動修復なし 1 日" in text
    assert "`afx history gaps list` で範囲を確認" in text
    assert text.count("  next:") == 2                                     # sync と gaps のみ


def test_open_conflicts_are_counted_and_point_to_list_and_show(world):
    root, conn = world
    _conflict(conn)
    sync = _collect(world).history_sync
    assert sync.open_conflicts == 1 and sh.NextAction.LIST_CONFLICTS in sync.next_actions
    text = _text(world)
    assert "open conflicts: 1" in text
    assert "`afx history conflicts list` / `show <id>`" in text and "置換には daemon の停止が必要" in text
    conn.execute("UPDATE history_conflicts SET state='kept'")
    conn.commit()
    assert _collect(world).history_sync.open_conflicts == 0


def test_a_reference_suspect_reads_both_the_new_and_the_legacy_detail_keys(world):
    root, conn = world
    _gap(conn, "JP225", "2026-11-04", "reference_suspected", detail={"median_fill_ppm": 987000}, missing=12)
    _gap(conn, "JP225", "2026-11-05", "reference_suspected", detail={"twice_median": 247}, missing=0)
    conn.execute("UPDATE history_sync_gaps SET detail_json='{\"twice_median\":247}' WHERE day_utc='2026-11-05'")
    _gap(conn, "JP225", "2026-11-06", "reference_suspected", detail={"unknown": 1})
    conn.commit()
    items = {d.day: d.detail for d in _collect(world).history_sync.suspected}
    assert items["2026-11-04"] == sync_store.GapDetail(median_fill_ppm=987000)
    assert items["2026-11-05"] == sync_store.GapDetail(legacy_median_bars=123.5)
    assert items["2026-11-06"] == sync_store.GapDetail()
    text = _text(world)
    assert "JP225 2026-11-04 bars=12 median_fill=98.70%" in text
    assert "JP225 2026-11-05 bars=0 median_bars=123.5 (旧形式)" in text
    assert "商品休場を含む疑い。採用可能とは解釈しない" in text
    assert "\n  next:" not in text.split("reference_suspected")[0]


@pytest.mark.parametrize("raw,expected", [
    (None, sync_store.GapDetail()), ("", sync_store.GapDetail()), ("not json", sync_store.GapDetail()),
    ("[1]", sync_store.GapDetail()), ('{"median_fill_ppm":true}', sync_store.GapDetail()),
    ('{"median_fill_ppm":-1}', sync_store.GapDetail()), ('{"twice_median":"9"}', sync_store.GapDetail()),
    ('{"median_fill_ppm":5,"twice_median":9}', sync_store.GapDetail(median_fill_ppm=5)),
])
def test_read_gap_detail_rejects_malformed_values(raw, expected):
    assert sync_store.read_gap_detail(raw) == expected


def test_the_writer_never_emits_the_legacy_key(world):
    root, conn = world
    _gap(conn, "JP225", "2026-11-04", "reference_suspected", detail={"median_fill_ppm": 5})
    stored = conn.execute("SELECT detail_json FROM history_sync_gaps").fetchone()[0]
    assert "twice_median" not in stored
    src = (SRC / "backtest" / "history_sync.py").read_text(encoding="utf-8")
    assert "twice_median" not in src


def test_a_configured_history_executor_url_is_reported_without_its_value(world):
    canary = "cnr" + "-user:" + "pw" + "-secret@"
    text = (REPO / "config" / "settings.yaml.example").read_text(encoding="utf-8")
    settings = load_settings_text(text)
    settings.backtest.eval_source = "mt5"
    settings.backtest.base_interval = "5m"
    settings.pairs = ["USDJPY"]
    settings.datafeed.mt5.history_executor_url = f"https://{canary}history.example.invalid:8812"
    result = _collect(world, settings)
    assert result.history_sync.executor_unsupported is True
    rendered = "\n".join(sh.render(result, "UTC"))
    assert "3b (履歴専用の別 bridge) は未実装・未対応" in rendered and "値は表示しません" in rendered
    for secret in (canary, "cnr-user", "pw-secret", "history.example.invalid"):
        assert secret not in rendered and secret not in repr(result)
    clean = "\n".join(sh.render(_collect(world), "UTC"))
    assert "未実装・未対応" not in clean and not _collect(world).history_sync.executor_unsupported


def test_free_text_in_the_tables_never_reaches_the_block(world):
    root, conn = world
    canary = "cnr-" + "free text secret/https://x.invalid?k=v"
    _run(conn, "run-1", status="failed", rc=1, failure="bridge_unavailable")
    conn.execute("UPDATE history_sync_runs SET failure_code=?, request_id=?, deferred_reason=?",
                 (canary, canary, canary))
    sync_store.record_series_failure(conn, "mt5", "5m", "JP225", error_code="x", run_id="run-1", now=NOW)
    conn.execute("UPDATE history_sync_series_state SET last_error=?, last_health=?", (canary, canary))
    conn.commit()
    result = _collect(world)
    assert result.history_sync.last_run.failure_code is None
    text = "\n".join(sh.render(result, "UTC"))
    assert "cnr-" not in text and "secret" not in text and "invalid" not in text
    assert "cnr-" not in repr(result)


def test_a_read_failure_makes_only_this_block_unavailable(world):
    root, conn = world
    conn.execute("ALTER TABLE history_sync_gaps RENAME COLUMN kind TO kind_x")
    result = _collect(world)
    assert result.history_sync.state is sh.SyncState.UNAVAILABLE
    assert result.history_sync.reason_code is sh.ReasonCode.SYNC_QUERY_FAILED
    assert result.evaluation_history.state is sh.State.READY
    text = "\n".join(sh.render(result, "UTC"))
    assert "history sync: unavailable" in text and "reason: sync_query_failed" in text


def test_eval_window_is_a_separate_slot_that_sync_never_fills(world):
    root, conn = world
    _run(conn, "run-1")
    result = _collect(world)
    assert result.eval_window == sh.NOT_COLLECTED
    text = "\n".join(sh.render(result, "UTC"))
    assert "eval_window: not_collected" in text
    for word in ("theta", "θ", "in_sample", "evaluable", "not_evaluated"):
        assert word not in text
    assert not hasattr(result.history_sync, "eval_window")


def test_the_unknown_path_reads_only_the_allowed_tables_and_no_prices_of_conflicts(world):
    root, conn = world
    _run(conn, "run-1")
    _conflict(conn)
    _gap(conn, "JP225", "2026-11-04", "reference_suspected", detail={"median_fill_ppm": 1})
    seen: list[tuple[str, str]] = []

    def authorizer(action, arg1, arg2, _db, _trigger):
        if action == sqlite3.SQLITE_READ:
            seen.append((arg1, arg2))
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    try:
        sh.collect(root, _settings(), conn, "UTC", NOW)
    finally:
        conn.set_authorizer(None)
    tables = {t for t, _c in seen}
    allowed = {"sqlite_master", "sqlite_schema", "ohlcv_history", "history_sync_runs",
               "history_sync_series_state", "history_sync_gaps", "history_conflicts", "history_revisions"}
    assert tables <= allowed, tables - allowed
    conflict_columns = {c for t, c in seen if t == "history_conflicts"}
    assert conflict_columns <= {"state", "source", "interval", ""}, conflict_columns
    assert not {"existing_json", "incoming_json", "reason_ref"} & {c for _t, c in seen}


def _importers_of(module: str) -> set[str]:
    found = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any(n == f"agentic_fx.{module}" or n.endswith(f".{module}") for n in names):
                found.add(path.relative_to(SRC).as_posix())
    return found


def test_the_structured_block_is_only_used_by_the_human_status_surface():
    allowed = {"status.py"}
    assert _importers_of("status_history") - {"status_history.py"} <= allowed
    for agent_facing in ("loops", "runners", "tools", "mission_worker.py", "policy_projection.py"):
        for importer in _importers_of("status_history"):
            assert not importer.startswith(agent_facing)

