"""§5.5 既存 approval 行との互換 migration (§8.1-38)。

混在 DB fixture: legacy pending (3 フィールド欠損) / legacy 終端 (保持) /
新形式 pending (3 フィールド完備、影響なし) の 3 種を同一 DB に用意する。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _seed_legacy_schema(db_path):
    """プラン10 導入前の approval_requests 相当の行を直接 INSERT する
    (payload に candidate_origin/candidate_path/artifact_hash を含めない)。
    `init_db` 未実行の生 DB に対して素の CREATE TABLE + INSERT を行う —
    §5.5 は「プラン10 導入前に作られた行」を模すため、`init_db` (= 本
    プランのスキーマ込み) を先に走らせてから legacy 相当の payload を
    後付けで INSERT する (列自体は元から存在するため、これで実質的に
    「プラン10 導入前の payload 形」を再現できる)。
    """
    conn = connect(db_path)
    init_db(conn)  # まず通常どおり初期化 (新スキーマ) される
    return conn


def test_legacy_pending_plugin_approval_becomes_invalidated_on_init_db(tmp_path):
    db_path = tmp_path / "t.db"
    conn = _seed_legacy_schema(db_path)
    # legacy pending: candidate_origin/candidate_path/artifact_hash が
    # payload に無い (3 フィールドすべて欠損のケース)。init_db は既に
    # 一度走っているため、この行は「本プランの migration がまだ見ていない
    # 行」を模すべく migration 適用前の状態として直接書き込む。
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, created_at) "
        "VALUES ('plugin', ?, 'pending', ?)",
        (json.dumps({"name": "legacy_plugin", "content_hash": "abc"}),
         NOW.isoformat()))
    conn.commit()
    conn.close()

    # 2 回目の起動 (= init_db 再実行) で migration が適用される。
    conn2 = connect(db_path)
    init_db(conn2)
    row = conn2.execute(
        "SELECT status, reason FROM approval_requests WHERE kind='plugin' "
        "AND json_extract(payload_json, '$.name')='legacy_plugin'").fetchone()
    assert row["status"] == "invalidated"
    assert row["reason"] == "legacy_payload_requires_resubmit"


def test_legacy_pending_missing_only_artifact_hash_also_invalidated(tmp_path):
    """3 フィールドの**いずれか**を欠く行が対象 (全欠損でなくてもよい)。"""
    db_path = tmp_path / "t.db"
    conn = _seed_legacy_schema(db_path)
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, created_at) "
        "VALUES ('plugin', ?, 'pending', ?)",
        (json.dumps({"name": "partial", "candidate_origin": "staging",
                    "candidate_path": "plugins/_staging/1/partial"}),  # artifact_hash 欠損
         NOW.isoformat()))
    conn.commit()
    conn.close()
    conn2 = connect(db_path)
    init_db(conn2)
    row = conn2.execute(
        "SELECT status FROM approval_requests WHERE "
        "json_extract(payload_json, '$.name')='partial'").fetchone()
    assert row["status"] == "invalidated"


@pytest.mark.parametrize("missing_key", [
    "candidate_origin", "candidate_path", "artifact_hash"])
def test_legacy_pending_missing_single_required_key_invalidated(tmp_path, missing_key):
    """M15 (段 0 Important): `_LEGACY_PLUGIN_APPROVAL_REQUIRED_KEYS` の
    必須キー 3 種から**どれか 1 つ**を落とす変異が red になる pin — 既存
    テストは `artifact_hash` 欠落のみを踏んでいたため、
    `candidate_origin`/`candidate_path` 単独欠落の変異は生存していた。"""
    db_path = tmp_path / "t.db"
    conn = _seed_legacy_schema(db_path)
    all_fields = {"candidate_origin": "staging",
                 "candidate_path": "plugins/_staging/1/partial",
                 "artifact_hash": "f" * 64}
    payload = {"name": "partial", **{k: v for k, v in all_fields.items()
                                     if k != missing_key}}
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, created_at) "
        "VALUES ('plugin', ?, 'pending', ?)",
        (json.dumps(payload), NOW.isoformat()))
    conn.commit()
    conn.close()
    conn2 = connect(db_path)
    init_db(conn2)
    row = conn2.execute(
        "SELECT status FROM approval_requests WHERE "
        "json_extract(payload_json, '$.name')='partial'").fetchone()
    assert row["status"] == "invalidated"


def test_legacy_terminal_approved_row_is_preserved(tmp_path):
    """終端済み (approved) の行はそのまま保持 — D4 admission に影響しない。"""
    db_path = tmp_path / "t.db"
    conn = _seed_legacy_schema(db_path)
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, "
        "decided_by, decided_at, created_at) "
        "VALUES ('plugin', ?, 'approved', 'shell', ?, ?)",
        (json.dumps({"name": "old_approved", "content_hash": "deadbeef"}),
         NOW.isoformat(), NOW.isoformat()))
    conn.commit()
    conn.close()
    conn2 = connect(db_path)
    init_db(conn2)
    row = conn2.execute(
        "SELECT status FROM approval_requests WHERE "
        "json_extract(payload_json, '$.name')='old_approved'").fetchone()
    assert row["status"] == "approved"  # 変化なし


def test_migration_is_idempotent_across_repeated_init_db(tmp_path):
    """2 回目以降の init_db 呼び出しは同じ行を再度 invalidate しようとせず
    no-op (invalidated 行は既に pending でないので対象外)。"""
    db_path = tmp_path / "t.db"
    conn = _seed_legacy_schema(db_path)
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, created_at) "
        "VALUES ('plugin', ?, 'pending', ?)",
        (json.dumps({"name": "legacy2"}), NOW.isoformat()))
    conn.commit()
    conn.close()
    conn2 = connect(db_path)
    init_db(conn2)  # 1 回目の migration
    conn2.close()
    conn3 = connect(db_path)
    init_db(conn3)  # 2 回目 — 例外にならず、状態も変わらない
    row = conn3.execute(
        "SELECT status FROM approval_requests WHERE "
        "json_extract(payload_json, '$.name')='legacy2'").fetchone()
    assert row["status"] == "invalidated"


def test_new_format_pending_row_with_all_three_fields_untouched(tmp_path):
    """3 フィールドを完備した pending 行は migration の対象外。"""
    db_path = tmp_path / "t.db"
    conn = _seed_legacy_schema(db_path)
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, created_at) "
        "VALUES ('plugin', ?, 'pending', ?)",
        (json.dumps({"name": "new_fmt", "candidate_origin": "staging",
                    "candidate_path": "plugins/_staging/1/new_fmt",
                    "artifact_hash": "f" * 64}),
         NOW.isoformat()))
    conn.commit()
    conn.close()
    conn2 = connect(db_path)
    init_db(conn2)
    row = conn2.execute(
        "SELECT status FROM approval_requests WHERE "
        "json_extract(payload_json, '$.name')='new_fmt'").fetchone()
    assert row["status"] == "pending"


def test_non_plugin_kind_pending_row_untouched(tmp_path):
    """`kind != 'plugin'` の pending 行 (tech_plugin/news_source/live_trade)
    は対象外 — §5.5 は `kind='plugin'` の payload 契約変更に限定した migration。"""
    db_path = tmp_path / "t.db"
    conn = _seed_legacy_schema(db_path)
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, created_at) "
        "VALUES ('news_source', ?, 'pending', ?)",
        (json.dumps({"name": "some_feed"}), NOW.isoformat()))
    conn.commit()
    conn.close()
    conn2 = connect(db_path)
    init_db(conn2)
    row = conn2.execute(
        "SELECT status FROM approval_requests WHERE kind='news_source'").fetchone()
    assert row["status"] == "pending"


# precheck 2026-08-22 pass2: RM4
def test_legacy_migration_logs_warning_per_invalidated_row(tmp_path, caplog):
    """(差分再検証 RM4) R10 裁定「migration は対象行ごとに WARNING ログを
    出す」の実装確認 — ログ出力を削除する変異を殺す。"""
    import logging
    db_path = tmp_path / "t.db"
    conn = _seed_legacy_schema(db_path)
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, created_at) "
        "VALUES ('plugin', ?, 'pending', ?)",
        (json.dumps({"name": "legacy_warn"}), NOW.isoformat()))
    conn.commit()
    conn.close()
    conn2 = connect(db_path)
    with caplog.at_level(logging.WARNING):
        init_db(conn2)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("invalidated by legacy-payload migration" in r.getMessage()
              for r in warnings)
