"""同期だけを導入したときの導入順、settings v5 / v6 の互換、旧コードへ戻す手順 (rollback)。"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from agentic_fx import status_history as sh
from agentic_fx.backtest import history_sync_cli as cli
from agentic_fx.config import Settings, load_settings_text
from agentic_fx.setup.settings_migrate import migrate
from agentic_fx.store.db import TABLE_NAMES, connect, connect_observer_readonly, init_db
from agentic_fx.store.instance_lock import acquire_instance_lock

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "settings_examples"
SYNC_TABLES = ("history_sync_runs", "history_sync_series", "history_sync_series_state",
               "history_sync_gaps", "history_sync_daily", "history_sync_resume")
NEW_KEYS = {"backtest.history_sync", "backtest.history_sync.auto_enabled",
            "backtest.history_sync.lookback_days"}
NOW = datetime(2026, 11, 10, 6, 2, 20, tzinfo=timezone.utc)


def _keys(doc, prefix=""):
    out = set()
    if isinstance(doc, dict):
        for key, value in doc.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.add(path)
            out |= _keys(value, path)
    return out


def _example_text() -> str:
    return (REPO / "config" / "settings.yaml.example").read_text(encoding="utf-8")


def _rollback_text(text: str) -> str:
    """runbook の手順を文字列で再現する: 新キーを取り除き、marker を v5 へ戻す。"""
    kept = [line for line in text.splitlines(keepends=True)
            if not re.match(r"\s*history_sync:", line)
            and not re.match(r"\s*#?\s*mt5\.history_executor_url:", line)
            and not re.match(r"\s*history_executor_url:", line)]
    return re.sub(r"schema_version: 6", "schema_version: 5", "".join(kept), count=1)


# ---- settings v5 / v6 ------------------------------------------------------

def test_v6_adds_only_the_sync_keys_to_the_v5_key_set():
    v5 = yaml.safe_load((FIXTURES / "v5.yaml").read_text(encoding="utf-8"))
    v6 = yaml.safe_load(_example_text())
    assert _keys(v6) - _keys(v5) == NEW_KEYS
    assert _keys(v5) - _keys(v6) == set()
    assert v6["schema_version"] == 6 and v5["schema_version"] == 5


def test_a_v5_settings_runs_on_the_new_code_with_the_model_defaults():
    migrated = migrate((FIXTURES / "v5.yaml").read_text(encoding="utf-8"))
    assert migrated.ok
    settings = load_settings_text(migrated.text)
    assert settings.backtest.history_sync.auto_enabled is True
    assert settings.backtest.history_sync.lookback_days == 7
    assert settings.datafeed.mt5.history_executor() is None
    assert "history_sync" not in migrated.text


def test_a_v6_settings_with_explicit_keys_round_trips_without_losing_the_executor_secret_rule():
    raw = yaml.safe_load(_example_text())
    raw["backtest"]["history_sync"] = {"auto_enabled": False, "lookback_days": 14}
    raw["datafeed"]["mt5"]["history_executor_url"] = "https://u:p@history.example.invalid:1"
    settings = Settings.model_validate(raw)
    assert settings.backtest.history_sync.auto_enabled is False
    dumped = settings.model_dump_json()
    assert "history_executor_url" not in dumped and "example.invalid" not in dumped


def test_unknown_keys_are_rejected_so_an_older_parser_would_reject_the_new_ones():
    raw = yaml.safe_load(_example_text())
    raw["backtest"]["history_sync_unknown"] = True
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


# ---- rollback の手順 -------------------------------------------------------

_OUTAGE_KEY_COMMENT = re.compile(
    r"^(\s+(?:flat_stall_max_sec|ready_confirm_ticks|auto_resume_when_flat):\s*\S+)\s+#.*$", re.M)


def _without_outage_comments(text: str) -> str:
    """停止の判定の設定 3 キーの行末コメントを外す。説明文は設定の意味の変更で書き直すので、
    history_sync の新キーの rollback を見るこの比較には含めない (キーと値は比較する)。"""
    return _OUTAGE_KEY_COMMENT.sub(r"\1", text)


def test_rolling_the_example_back_restores_the_v5_bytes_exactly():
    assert _without_outage_comments(_rollback_text(_example_text())) == _without_outage_comments(
        (FIXTURES / "v5.yaml").read_text(encoding="utf-8"))


def test_a_config_with_the_new_keys_set_has_exactly_the_v5_key_set_after_the_rollback_steps():
    raw = yaml.safe_load(_example_text())
    raw["backtest"]["history_sync"] = {"auto_enabled": False, "lookback_days": 14}
    raw["datafeed"]["mt5"]["history_executor_url"] = "https://history.example.invalid:1"
    assert _keys(raw) - _keys(yaml.safe_load((FIXTURES / "v5.yaml").read_text(encoding="utf-8"))) \
        >= NEW_KEYS | {"datafeed.mt5.history_executor_url"}
    raw["backtest"].pop("history_sync")
    raw["datafeed"]["mt5"].pop("history_executor_url")
    raw["schema_version"] = 5
    v5 = yaml.safe_load((FIXTURES / "v5.yaml").read_text(encoding="utf-8"))
    assert _keys(raw) == _keys(v5)


def test_the_rollback_leaves_the_database_tables_and_never_drops_an_unknown_table():
    source = (REPO / "src" / "agentic_fx" / "store" / "db.py").read_text(encoding="utf-8")
    drops = re.findall(r"DROP TABLE (?:IF EXISTS )?([A-Za-z_{}\"']+)", source)
    assert drops and not [d for d in drops if d.startswith("history_") or "{" in d]
    assert set(SYNC_TABLES) <= TABLE_NAMES


# ---- 同期だけを導入したときの順序 ------------------------------------------

def _old_database(tmp_path) -> Path:
    path = tmp_path / "data" / "agentic.db"
    path.parent.mkdir(parents=True)
    legacy = sqlite3.connect(path)
    legacy.executescript((REPO / "tests" / "fixtures" / "db_schema_v5.sql").read_text(encoding="utf-8"))
    legacy.commit()
    legacy.close()
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _status_settings():
    from tests.test_status_history import _settings
    return _settings()


def test_new_code_on_an_old_database_reports_not_initialized_without_creating_tables(tmp_path):
    db = _old_database(tmp_path)
    before = _sha(db)
    conn = connect_observer_readonly(db, force_ro=True)
    try:
        result = sh.collect(tmp_path, _status_settings(), conn, "UTC", NOW)
    finally:
        conn.close()
    assert result.history_sync.state is sh.SyncState.NOT_INITIALIZED
    assert result.history_sync.next_actions == (sh.NextAction.RESTART_TO_INTRODUCE_SYNC_SCHEMA,)
    assert _sha(db) == before
    check = sqlite3.connect(db)
    names = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check.close()
    assert not names & set(SYNC_TABLES)


def test_the_restart_introduces_the_tables_once_and_a_second_start_changes_nothing(tmp_path):
    db = _old_database(tmp_path)
    conn = connect(db)
    init_db(conn)
    first = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master")}
    init_db(conn)
    second = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master")}
    conn.close()
    assert set(SYNC_TABLES) <= set(first) and first == second
    ro = connect_observer_readonly(db, force_ro=True)
    try:
        result = sh.collect(tmp_path, _status_settings(), ro, "UTC", NOW)
    finally:
        ro.close()
    assert result.history_sync.state is sh.SyncState.NEVER_RUN


def test_the_order_is_enforced_by_the_instance_lock_not_by_the_caller(tmp_path, capsys):
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "config" / "settings.yaml").write_text(_example_text().replace(
        'eval_source: "none"', 'eval_source: "mt5"', 1), encoding="utf-8")
    db = root / "data" / "agentic.db"
    conn = connect(db)
    init_db(conn)
    conn.close()
    before = _sha(db)
    settings = load_settings_text((root / "config" / "settings.yaml").read_text(encoding="utf-8"))
    daemon = acquire_instance_lock(root / "data")
    try:
        class Args:
            rescan_holdout = False
            until_done = False
        assert cli.run_sync_command(Args, root, settings) == 1
    finally:
        daemon.close()
    assert cli.LOCKED_CODE in capsys.readouterr().err
    assert _sha(db) == before


def test_only_the_sync_tables_and_keys_are_introduced_no_other_shared_marker_content():
    other = {name for name in TABLE_NAMES if "eval_window" in name or "evalwin" in name}
    assert other == set()
    v6 = yaml.safe_load(_example_text())
    assert not [k for k in _keys(v6) if "eval_window" in k or "evalwin" in k]
