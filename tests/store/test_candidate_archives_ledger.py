"""候補台帳の分類 (outcome_class) と検出属性 (quality_flags)。"""
from __future__ import annotations

import ast
import itertools
import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.store import candidate_archives as archives
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)
SRC = Path(__file__).resolve().parents[2] / "src" / "agentic_fx"
EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples" / "plugins"

OUTCOMES = list(archives.OUTCOME_CLASSES)
ALLOWED = (
    [("not_submitted", to) for to in (
        "pending", "unprofitable", "insufficient_trades", "gate_failed",
        "noop_copy", "duplicate_logic", "duplicate_metrics",
        "family_redirect")]
    + [("pending", to) for to in (
        "approved", "rejected_by_human", "expired", "invalidated")]
    + [("approved", "revoked")])

_OLD_DDL = """
CREATE TABLE candidate_archives (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  artifact_hash TEXT NOT NULL,
  archive_path TEXT,
  pair TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(mission_id, artifact_hash)
);
"""


def _conn(tmp_path, name="ledger.db"):
    conn = connect(tmp_path / name)
    init_db(conn)
    return conn


def _insert(conn, **overrides):
    values = dict(mission_id=3, name="rsi_v2", content_hash="content-a",
                  artifact_hash="artifact-a", archive_path=None,
                  pair="USDJPY", metrics={"pf": 1.25}, now=NOW, commit=True)
    values.update(overrides)
    return archives.insert(conn, **values)


def _outcome(conn, archive_id):
    return conn.execute("SELECT outcome_class FROM candidate_archives WHERE id=?",
                        (archive_id,)).fetchone()[0]


def _force(conn, archive_id, outcome):
    # 検査対象の外から現在値を直接置く (テスト用の初期化)
    conn.execute("UPDATE candidate_archives SET outcome_class=? WHERE id=?",
                 (outcome, archive_id))


# --- AC-14 -----------------------------------------------------------------

def test_ac14_initial_outcome_is_not_submitted_and_flags_empty(tmp_path):
    conn = _conn(tmp_path)
    row = archives.get(conn, _insert(conn))
    assert row["outcome_class"] == "not_submitted"
    assert row["quality_flags"] == []
    assert row["logic_hash"] is None and row["family_key"] is None


@pytest.mark.parametrize(
    "src,dst", list(itertools.product(OUTCOMES, OUTCOMES)),
    ids=lambda v: v)
def test_ac14_transition_matrix_all_196_pairs(tmp_path, src, dst):
    conn = _conn(tmp_path)
    archive_id = _insert(conn)
    _force(conn, archive_id, src)
    if (src, dst) in ALLOWED:
        assert archives.settle(conn, archive_id, dst, expected_from=src) is True
        assert _outcome(conn, archive_id) == dst
    else:
        with pytest.raises(archives.InvalidTransitionError):
            archives.settle(conn, archive_id, dst, expected_from=src)
        assert _outcome(conn, archive_id) == src


def test_ac14_allowed_set_has_13_and_rejects_183():
    pairs = list(itertools.product(OUTCOMES, OUTCOMES))
    assert len(OUTCOMES) == 14 and len(pairs) == 196
    allowed = [p for p in pairs
               if p[1] in archives.ALLOWED_TRANSITIONS.get(p[0], ())]
    assert sorted(allowed) == sorted(ALLOWED) and len(allowed) == 13


def test_ac14_settle_cas_does_not_update_when_current_differs(tmp_path):
    conn = _conn(tmp_path)
    archive_id = _insert(conn)
    assert archives.settle(conn, archive_id, "pending") is True
    # 既に pending: 同じ not_submitted 起点の settle は更新しない
    assert archives.settle(conn, archive_id, "gate_failed") is False
    assert _outcome(conn, archive_id) == "pending"


def test_ac14_check_clause_rejects_value_outside_14(tmp_path):
    conn = _conn(tmp_path)
    archive_id = _insert(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE candidate_archives SET outcome_class='bogus' "
                     "WHERE id=?", (archive_id,))
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name="
                       "'candidate_archives'").fetchone()[0]
    for value in OUTCOMES:
        assert f"'{value}'" in ddl
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO candidate_archives (mission_id,name,content_hash,"
            "artifact_hash,metrics_json,created_at,outcome_class) VALUES "
            "(9,'n','c','a','{}','t','bogus')")


def test_ac14_quality_flags_accept_only_two_elements(tmp_path):
    conn = _conn(tmp_path)
    for flag in ("family_detected", "legacy_unlinked"):
        archive_id = _insert(conn, artifact_hash=f"a-{flag}",
                             quality_flags=[flag])
        assert archives.get(conn, archive_id)["quality_flags"] == [flag]
    with pytest.raises(ValueError):
        _insert(conn, artifact_hash="bad", quality_flags=["whatever"])
    archive_id = _insert(conn, artifact_hash="settle-flags")
    with pytest.raises(ValueError):
        archives.settle(conn, archive_id, "pending", add_flags=["other"])
    assert _outcome(conn, archive_id) == "not_submitted"
    assert archives.settle(conn, archive_id, "pending",
                           add_flags=["family_detected"]) is True
    assert archives.get(conn, archive_id)["quality_flags"] == ["family_detected"]
    # 決定後もフラグは残る
    assert archives.settle(conn, archive_id, "approved",
                           expected_from="pending") is True
    assert archives.get(conn, archive_id)["quality_flags"] == ["family_detected"]


def _function_spans(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans.append((node.lineno, node.end_lineno, node.name))
    return spans


def test_ac14_outcome_class_writers_are_pinned_by_scan():
    """`outcome_class` を書く SQL は settle と migration の照合関数だけ。
    `settle` の呼び出し元は終端の記録・apply_decision・append_revocation だけ。"""
    writers: set[tuple[str, str]] = set()
    pattern = re.compile(r"outcome_class\s*=\s*[?'\"a-z]")
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "candidate_archives" not in text:
            continue
        lines = text.splitlines()
        spans = _function_spans(path)
        for number, line in enumerate(lines, 1):
            if "UPDATE candidate_archives" not in line:
                continue
            statement = " ".join(lines[number - 1:number + 3])
            if not pattern.search(statement.split("WHERE")[0]):
                continue
            owner = min((s for s in spans if s[0] <= number <= s[1]),
                        key=lambda s: s[1] - s[0], default=(0, 0, "?"))[2]
            writers.add((path.relative_to(SRC).as_posix(), owner))
    assert writers == {
        ("store/candidate_archives.py", "settle"),
        ("store/candidate_archives.py", "backfill_outcome_from_approvals"),
    }
    callers: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "candidate_archives.py":
            continue
        text = path.read_text(encoding="utf-8")
        if not re.search(r"candidate_archives(_mod|_store)?\.settle\(|"
                         r"archives_mod\.settle\(", text):
            continue
        spans = _function_spans(path)
        for number, line in enumerate(text.splitlines(), 1):
            if re.search(r"\.settle\(", line) and "archives" in line:
                owner = min((s for s in spans if s[0] <= number <= s[1]),
                            key=lambda s: s[1] - s[0])[2]
                callers.add((path.relative_to(SRC).as_posix(), owner))
    assert {c[1] for c in callers} <= {
        "_settle_archive", "_settle_candidate_in_tx"}
    assert {c[0] for c in callers} <= {
        "store/approvals.py", "loops/improve_loop.py"}


# --- pair NULL / schema ----------------------------------------------------

def test_pair_null_round_trip_for_indicator_candidate(tmp_path):
    conn = _conn(tmp_path)
    archive_id = _insert(conn, name="rsi_indicator", pair=None,
                         family_key="indicator:rsi_indicator@1h")
    row = archives.get(conn, archive_id)
    assert row["pair"] is None
    assert row["family_key"] == "indicator:rsi_indicator@1h"


def _table_info(conn):
    return [tuple(r) for r in conn.execute(
        "PRAGMA table_info(candidate_archives)")]


def _legacy_db(tmp_path, name="legacy.db"):
    path = tmp_path / name
    conn = connect(path)
    init_db(conn)
    conn.execute("DROP TABLE candidate_archives")
    conn.executescript(_OLD_DDL)
    conn.commit()
    return conn, path


def test_migration_table_info_matches_fresh_and_is_idempotent(tmp_path):
    fresh = _table_info(_conn(tmp_path, "fresh.db"))
    legacy, path = _legacy_db(tmp_path)
    assert _table_info(legacy) != fresh
    legacy.close()
    migrated = connect(path)
    init_db(migrated)
    assert _table_info(migrated) == fresh
    before = migrated.execute("SELECT sql FROM sqlite_master WHERE "
                              "name='candidate_archives'").fetchone()[0]
    init_db(migrated)
    after = migrated.execute("SELECT sql FROM sqlite_master WHERE "
                             "name='candidate_archives'").fetchone()[0]
    assert before == after
    assert _table_info(migrated) == fresh
    # UNIQUE(mission_id, artifact_hash) は移行後も効く
    first = _insert(migrated)
    assert _insert(migrated) == first


# --- AC-19 -----------------------------------------------------------------

def _approval(conn, status, payload, *, decided=True):
    cur = conn.execute(
        "INSERT INTO approval_requests (kind,payload_json,status,reason,"
        "decided_by,decided_at,created_at) VALUES ('plugin',?,?,?,?,?,?)",
        (json.dumps(payload), status, None,
         "human_cli" if decided else None,
         NOW.isoformat() if decided else None, NOW.isoformat()))
    return cur.lastrowid


def _old_row(conn, mission_id, artifact, content, *, name="n", path=None,
             pair="USDJPY", metrics=None):
    conn.execute(
        "INSERT INTO candidate_archives (mission_id,name,content_hash,"
        "artifact_hash,archive_path,pair,metrics_json,created_at) VALUES "
        "(?,?,?,?,?,?,?,?)",
        (mission_id, name, content, artifact, path, pair,
         json.dumps(metrics or {"pf": 1.1}, sort_keys=True), NOW.isoformat()))


def _legacy_fixture(tmp_path):
    root = tmp_path / "root"
    for mission, example, key in ((1, "sma_cross", "h1"), (2, "rsi_indicator", "h2")):
        target = root / "plugins" / "_archive" / str(mission) / key
        shutil.copytree(EXAMPLES / example, target)
    (root / "plugins" / "_archive" / "7" / "broken").mkdir(parents=True)
    (root / "plugins" / "_archive" / "7" / "broken" / "plugin.py").write_text(
        "def (:\n", encoding="utf-8")
    (root / "plugins" / "_archive" / "7" / "broken" / "config.yaml").write_text(
        "kind: strategy\n", encoding="utf-8")
    conn, path = _legacy_db(tmp_path)
    # (1) 完全一致 + 承認
    _old_row(conn, 1, "h1", "c1", name="sma_cross",
             path="plugins/_archive/1/h1")
    a1 = _approval(conn, "approved", {
        "mission_id": 1, "artifact_hash": "h1", "content_hash": "c1",
        "name": "sma_cross"})
    # (2) artifact_hash が違い (mission, content) が 1 行 + 却下
    _old_row(conn, 2, "h2", "c2", name="rsi_indicator", pair="USDJPY",
             path="plugins/_archive/2/h2")
    _approval(conn, "rejected", {
        "mission_id": 2, "artifact_hash": "other", "content_hash": "c2"})
    # (3) (mission, content) が 2 行で曖昧
    _old_row(conn, 3, "x1", "c3")
    _old_row(conn, 3, "x2", "c3")
    _approval(conn, "approved", {
        "mission_id": 3, "artifact_hash": "zz", "content_hash": "c3"})
    # (4) 行が無い
    _approval(conn, "pending", {
        "mission_id": 4, "artifact_hash": "none", "content_hash": "c4"},
        decided=False)
    # (5) 撤回済みの承認
    _old_row(conn, 5, "h5", "c5")
    a5 = _approval(conn, "approved", {
        "mission_id": 5, "artifact_hash": "h5", "content_hash": "c5"})
    _approval(conn, "rejected", {
        "name": "n", "content_hash": "c5", "artifact_hash": "h5",
        "revokes_approval_id": a5})
    # (6) 提出に至らない行
    _old_row(conn, 6, "h6", "c6")
    # 計算不能な archive (parse 不能) と期限切れ
    _old_row(conn, 7, "broken", "c7", path="plugins/_archive/7/broken")
    _approval(conn, "expired", {
        "mission_id": 7, "artifact_hash": "broken", "content_hash": "c7"})
    conn.commit()
    snapshot_rows = [dict(r) for r in conn.execute(
        "SELECT id,mission_id,name,content_hash,artifact_hash,archive_path,"
        "pair,metrics_json,created_at FROM candidate_archives ORDER BY id")]
    snapshot_payloads = [tuple(r) for r in conn.execute(
        "SELECT id,payload_json FROM approval_requests ORDER BY id")]
    conn.close()
    return root, path, snapshot_rows, snapshot_payloads, a1


def test_ac19_legacy_migration_preserves_existing_columns_and_payload_bytes(
        tmp_path):
    root, path, rows_before, payloads_before, _ = _legacy_fixture(tmp_path)
    migrated = connect(path)
    init_db(migrated, root=root)
    cols = [r["name"] for r in migrated.execute(
        "PRAGMA table_info(candidate_archives)")]
    assert cols[:9] == list(rows_before[0].keys())
    assert cols[9:] == ["logic_hash", "family_key", "outcome_class",
                        "quality_flags"]
    rows_after = [dict(r) for r in migrated.execute(
        "SELECT id,mission_id,name,content_hash,artifact_hash,archive_path,"
        "pair,metrics_json,created_at FROM candidate_archives ORDER BY id")]
    assert rows_after == rows_before
    assert [tuple(r) for r in migrated.execute(
        "SELECT id,payload_json FROM approval_requests ORDER BY id")
    ] == payloads_before


def _by_artifact(conn):
    return {r["artifact_hash"]: dict(r) for r in conn.execute(
        "SELECT * FROM candidate_archives")}


def test_ac19_legacy_outcome_resolution_and_unlinked_flags(tmp_path):
    root, path, *_ = _legacy_fixture(tmp_path)
    migrated = connect(path)
    init_db(migrated, root=root)
    rows = _by_artifact(migrated)
    assert rows["h1"]["outcome_class"] == "approved"           # (1)
    assert rows["h2"]["outcome_class"] == "rejected_by_human"  # (2)
    for key in ("x1", "x2"):                                    # (3)
        assert rows[key]["outcome_class"] == "not_submitted"
        assert json.loads(rows[key]["quality_flags"]) == ["legacy_unlinked"]
    assert rows["h5"]["outcome_class"] == "revoked"            # (5)
    assert rows["h6"]["outcome_class"] == "not_submitted"      # (6)
    assert json.loads(rows["h6"]["quality_flags"]) == []
    assert rows["broken"]["outcome_class"] == "expired"
    # (4) 行が無い承認は他の行を approved にしない
    assert [r["artifact_hash"] for r in rows.values()
            if r["outcome_class"] == "approved"] == ["h1"]


def test_ac19_legacy_logic_hash_and_family_key_backfilled_or_null(tmp_path):
    root, path, *_ = _legacy_fixture(tmp_path)
    migrated = connect(path)
    init_db(migrated, root=root)
    rows = _by_artifact(migrated)
    assert rows["h1"]["logic_hash"] and len(rows["h1"]["logic_hash"]) == 64
    assert rows["h1"]["family_key"] is None      # indicator 依存の無い戦略
    assert rows["h2"]["family_key"] == "indicator:rsi_indicator@1h"
    assert rows["broken"]["logic_hash"] is None  # parse 不能は NULL
    assert rows["h6"]["logic_hash"] is None      # 現物が無い
    again = {k: dict(v) for k, v in rows.items()}
    init_db(migrated, root=root)
    assert {k: dict(v) for k, v in _by_artifact(migrated).items()} == again


def test_ac19_without_root_hashes_stay_null_until_root_given(tmp_path):
    root, path, *_ = _legacy_fixture(tmp_path)
    migrated = connect(path)
    init_db(migrated)
    assert _by_artifact(migrated)["h1"]["logic_hash"] is None
    init_db(migrated, root=root)
    assert _by_artifact(migrated)["h1"]["logic_hash"] is not None


def test_resolve_priority_and_ambiguity(tmp_path):
    conn = _conn(tmp_path)
    first = _insert(conn, artifact_hash="a1", content_hash="c")
    _insert(conn, artifact_hash="a2", content_hash="c")
    assert archives.resolve(conn, {"candidate_archive_id": first,
                                   "mission_id": 3}) == first
    assert archives.resolve(conn, {"mission_id": 3, "artifact_hash": "a1",
                                   "content_hash": "c"}) == first
    assert archives.resolve(conn, {"mission_id": 3, "artifact_hash": "zz",
                                   "content_hash": "c"}) is None
    assert archives.resolve(conn, {"candidate_archive_id": 999}) is None
    assert archives.resolve(conn, None) is None


def test_legacy_pending_approval_migration_runs_after_the_ledger_rebuild(
        tmp_path):
    """旧形式の台帳と旧 payload の pending 承認を持つ DB でも、起動時の
    migration 同士が衝突せず、旧 payload は invalidated になる。"""
    conn, path = _legacy_db(tmp_path)
    _old_row(conn, 1, "h1", "c1")
    legacy_id = _approval(conn, "pending", {"name": "old"}, decided=False)
    linked_id = _approval(conn, "pending", {
        "name": "n", "content_hash": "c1", "artifact_hash": "h1",
        "kind": "indicator", "mission_id": 1,
        "candidate_path": "p", "candidate_origin": "staging"}, decided=False)
    conn.commit()
    conn.close()
    migrated = connect(path)
    init_db(migrated)
    statuses = dict(migrated.execute(
        "SELECT id, status FROM approval_requests").fetchall())
    assert statuses[legacy_id] == "invalidated"
    assert statuses[linked_id] == "pending"
    assert _by_artifact(migrated)["h1"]["outcome_class"] == "pending"
