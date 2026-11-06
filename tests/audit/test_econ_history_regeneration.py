"""履歴 snapshot から回避帯の母集団別件数を再生成する (bench)。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(FIXTURES))
import build_econ_window_history_fixture as builder  # noqa: E402

SNAPSHOT = FIXTURES / "econ_window_history_20261016_1031.sqlite"
SNAPSHOT_SHA256 = "5009523f568d4ae0048b44a081aeae8d256a6f086f7bb6796dbe3cfa232700ba"
SUMMARY = ROOT / "tmp" / "econ-window-acceptance" / "history" / "summary.json"

EXPECTED_COLUMNS = {
    "missions": ["id", "loop", "trigger", "status", "started_at"],
    "trade_intents": ["id", "mission_id", "action"],
    "signals": ["id", "status", "kind", "claimed_by_mission_id"],
}

QUERIES = {
    "intents_by_action": (
        "SELECT ti.action, COUNT(*) FROM trade_intents ti "
        "JOIN missions m ON m.id = ti.mission_id "
        "WHERE m.started_at >= ? AND m.started_at < ? "
        "GROUP BY ti.action ORDER BY ti.action"
    ),
    "trade_missions": (
        "SELECT COUNT(*) FROM missions "
        "WHERE loop = 'trade' AND started_at >= ? AND started_at < ?"
    ),
    "signal_linked": (
        "SELECT s.status, m.trigger, COUNT(*) FROM signals s "
        "JOIN missions m ON m.id = s.claimed_by_mission_id "
        "WHERE m.started_at >= ? AND m.started_at < ? "
        "GROUP BY s.status, m.trigger ORDER BY s.status, m.trigger"
    ),
}

pytestmark = pytest.mark.bench


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _open_ro() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{SNAPSHOT}?mode=ro", uri=True)


def test_extractor_allowlist_is_pinned() -> None:
    assert {k: list(v) for k, v in builder.ALLOWED_COLUMNS.items()} == EXPECTED_COLUMNS


def test_snapshot_schema_has_only_allowlisted_tables_and_columns() -> None:
    con = _open_ro()
    try:
        tables = sorted(
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        )
        assert tables == sorted(EXPECTED_COLUMNS)
        for table, cols in EXPECTED_COLUMNS.items():
            got = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
            assert got == cols
        assert (
            con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('view','trigger')"
            ).fetchone()[0]
            == 0
        )
    finally:
        con.close()


def test_extractor_copies_only_allowlisted_columns(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    con = sqlite3.connect(src)
    con.executescript(
        """
        CREATE TABLE missions (id INTEGER PRIMARY KEY, loop TEXT, trigger TEXT,
          status TEXT, started_at TEXT, output_json TEXT, transcript_json TEXT);
        CREATE TABLE trade_intents (id INTEGER PRIMARY KEY, mission_id INTEGER,
          payload_json TEXT, action TEXT);
        CREATE TABLE signals (id INTEGER PRIMARY KEY, status TEXT, kind TEXT,
          claimed_by_mission_id INTEGER, payload_json TEXT);
        CREATE TABLE secrets (id INTEGER PRIMARY KEY, v TEXT);
        INSERT INTO missions VALUES
          (1,'trade','cron','completed','2026-10-20T00:00:00+00:00','SECRET','SECRET'),
          (2,'trade','cron','completed','2026-11-20T00:00:00+00:00','x','x');
        INSERT INTO trade_intents VALUES (1,1,'SECRET','hold'),(2,2,'SECRET','open');
        INSERT INTO signals VALUES (1,'consumed','k',1,'SECRET'),(2,'consumed','k',2,'S');
        INSERT INTO secrets VALUES (1,'SECRET');
        """
    )
    con.commit()
    con.close()
    out = tmp_path / "out.sqlite"
    counts = builder.build(src, out)
    assert counts == {"missions": 1, "trade_intents": 1, "signals": 1}
    assert b"SECRET" not in out.read_bytes()
    dst = sqlite3.connect(out)
    try:
        for table, cols in EXPECTED_COLUMNS.items():
            assert [r[1] for r in dst.execute(f"PRAGMA table_info({table})")] == cols
        assert dst.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 3
    finally:
        dst.close()


def test_extractor_does_not_write_source(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    con = sqlite3.connect(src)
    con.executescript(
        "CREATE TABLE missions (id INTEGER PRIMARY KEY, loop TEXT, trigger TEXT,"
        " status TEXT, started_at TEXT);"
        "CREATE TABLE trade_intents (id INTEGER PRIMARY KEY, mission_id INTEGER,"
        " action TEXT);"
        "CREATE TABLE signals (id INTEGER PRIMARY KEY, status TEXT, kind TEXT,"
        " claimed_by_mission_id INTEGER);"
    )
    con.commit()
    con.close()
    before = _sha256(src)
    builder.build(src, tmp_path / "out.sqlite")
    assert _sha256(src) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.sqlite", "src.db"]


def test_window_populations_regenerate_from_snapshot() -> None:
    sha_before = _sha256(SNAPSHOT)
    assert sha_before == SNAPSHOT_SHA256
    bounds = (builder.WINDOW_START, builder.WINDOW_END)
    con = _open_ro()
    try:
        by_action = dict(con.execute(QUERIES["intents_by_action"], bounds).fetchall())
        trade_missions = con.execute(QUERIES["trade_missions"], bounds).fetchone()[0]
        linked = con.execute(QUERIES["signal_linked"], bounds).fetchall()
    finally:
        con.close()
    assert by_action == {"hold": 970, "open": 3}
    assert sum(by_action.values()) == 973
    assert trade_missions == 1033
    assert linked == [("consumed", "cron", 57)]
    assert sum(n for _, _, n in linked) == 57
    # 移行前の cron 由来 consumed が全件で、新分類の件数は 0
    new_classes = {
        "system_reject": 0,
        "llm_accept": 0,
        "llm_reject": 0,
        "abandoned": 0,
    }
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY.write_text(
        json.dumps(
            {
                "window": {"start": bounds[0], "end": bounds[1]},
                "fixture": SNAPSHOT.name,
                "fixture_sha256": sha_before,
                "queries": QUERIES,
                "intents_by_action": by_action,
                "intents_total": 973,
                "trade_missions": trade_missions,
                "signal_linked": 57,
                "signal_pre_migration_cron_consumed": 57,
                "signal_new_classes": new_classes,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    assert _sha256(SNAPSHOT) == sha_before
