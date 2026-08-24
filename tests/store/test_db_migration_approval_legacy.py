"""§5.5 既存 approval 行との互換 migration (§8.1-38)。

混在 DB fixture: legacy pending (3 フィールド欠損) / legacy 終端 (保持) /
新形式 pending (3 フィールド完備、影響なし) の 3 種を同一 DB に用意する。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from agentic_fx.store import backlog
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


# --- codex I4 (2026-08-24): §5.5 migration が apply_decision を経由する ---

def test_legacy_migration_uses_apply_decision_and_transitions_bound_backlog(
        tmp_path):
    """codex I4 killer: 設計書 §5.5 は逐語で
    `apply_decision(status='invalidated', reason='legacy_payload_requires_
    resubmit')` (+ activity + 通知、通知/activity は D1〜D4 裁定の対象外
    なので本テストの対象外) を要求している。旧実装は `UPDATE
    approval_requests` を直打ちしており、単一 API 規律 (§4.3) の外側で
    状態を書き換えていた — payload に `backlog_id` を持つ legacy 行が
    あれば、対応する backlog も同一 tx で
    `observation`/`last_result=='invalidated'` に収束するはずだが、直打ち
    ではこの遷移が起きない。"""
    db_path = tmp_path / "t.db"
    conn = _seed_legacy_schema(db_path)
    bid = backlog.add(conn, "idea", "user", NOW)
    backlog.select_for_mission(conn, bid, now=NOW)  # -> selected
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, created_at) "
        "VALUES ('plugin', ?, 'pending', ?)",
        (json.dumps({"name": "legacy_with_backlog", "backlog_id": bid}),
         NOW.isoformat()))
    conn.commit()
    conn.close()

    conn2 = connect(db_path)
    init_db(conn2)

    approval = conn2.execute(
        "SELECT status, reason FROM approval_requests WHERE "
        "json_extract(payload_json, '$.name')='legacy_with_backlog'").fetchone()
    assert approval["status"] == "invalidated"
    assert approval["reason"] == "legacy_payload_requires_resubmit"
    backlog_row = conn2.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?",
        (bid,)).fetchone()
    assert backlog_row["status"] == "observation"
    assert backlog_row["last_result"] == "invalidated"


def test_legacy_migration_treats_empty_string_field_as_missing(tmp_path):
    """L43: 必須キー判定が `k in payload` の存在のみ → `candidate_origin:
    ""` のような空値行を新形式と誤認して素通りする。空文字は「欠損」
    として扱われるべき (真値判定へ)。"""
    db_path = tmp_path / "t.db"
    conn = _seed_legacy_schema(db_path)
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, created_at) "
        "VALUES ('plugin', ?, 'pending', ?)",
        (json.dumps({"name": "empty_fields", "candidate_origin": "",
                    "candidate_path": "", "artifact_hash": ""}),
         NOW.isoformat()))
    conn.commit()
    conn.close()
    conn2 = connect(db_path)
    init_db(conn2)
    row = conn2.execute(
        "SELECT status FROM approval_requests WHERE "
        "json_extract(payload_json, '$.name')='empty_fields'").fetchone()
    assert row["status"] == "invalidated"


def test_legacy_migration_decided_by_is_system_migration(tmp_path):
    """L44: `decided_by='system:migration'` を検証するテストが無かった。"""
    db_path = tmp_path / "t.db"
    conn = _seed_legacy_schema(db_path)
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, created_at) "
        "VALUES ('plugin', ?, 'pending', ?)",
        (json.dumps({"name": "check_decided_by"}), NOW.isoformat()))
    conn.commit()
    conn.close()
    conn2 = connect(db_path)
    init_db(conn2)
    row = conn2.execute(
        "SELECT decided_by FROM approval_requests WHERE "
        "json_extract(payload_json, '$.name')='check_decided_by'").fetchone()
    assert row["decided_by"] == "system:migration"


def test_legacy_migration_warning_count_matches_invalidated_rows(tmp_path, caplog):
    """L45: WARNING 検証が `any(...)` + 部分文字列で `len(warnings)` を
    見ていなかった。2 行分の legacy を仕込み件数を確認する。"""
    import logging
    db_path = tmp_path / "t.db"
    conn = _seed_legacy_schema(db_path)
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, created_at) "
        "VALUES ('plugin', ?, 'pending', ?)",
        (json.dumps({"name": "warn1"}), NOW.isoformat()))
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, created_at) "
        "VALUES ('plugin', ?, 'pending', ?)",
        (json.dumps({"name": "warn2", "candidate_origin": "staging"}),
         NOW.isoformat()))
    conn.commit()
    conn.close()
    conn2 = connect(db_path)
    with caplog.at_level(logging.WARNING):
        init_db(conn2)
    warnings = [r for r in caplog.records
               if r.levelno == logging.WARNING
               and "invalidated by legacy-payload migration" in r.getMessage()]
    assert len(warnings) == 2
    assert "candidate_origin" not in warnings[1].getMessage()  # warn2 は origin だけ埋まっている
    assert "candidate_path" in warnings[1].getMessage()


def test_legacy_migration_decided_at_is_utc_aware(tmp_path):
    """L46: `decided_at` (`_now_utc_isoformat`) が未検証。"""
    db_path = tmp_path / "t.db"
    conn = _seed_legacy_schema(db_path)
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, created_at) "
        "VALUES ('plugin', ?, 'pending', ?)",
        (json.dumps({"name": "tz_check"}), NOW.isoformat()))
    conn.commit()
    conn.close()
    conn2 = connect(db_path)
    init_db(conn2)
    row = conn2.execute(
        "SELECT decided_at FROM approval_requests WHERE "
        "json_extract(payload_json, '$.name')='tz_check'").fetchone()
    parsed = datetime.fromisoformat(row["decided_at"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_legacy_migration_survives_malformed_payload_json(tmp_path):
    """L47: `json.loads` 失敗時の `payload = {}` フォールバックが未テスト
    — 不正 JSON 行を投入しても `init_db` が例外なく完走して行が
    `invalidated` になることを pin する。"""
    db_path = tmp_path / "t.db"
    conn = _seed_legacy_schema(db_path)
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, created_at) "
        "VALUES ('plugin', 'not json', 'pending', ?)", (NOW.isoformat(),))
    conn.commit()
    conn.close()
    conn2 = connect(db_path)
    init_db(conn2)  # 例外なく完走すること自体が pin
    row = conn2.execute(
        "SELECT status FROM approval_requests WHERE payload_json='not json'"
    ).fetchone()
    assert row["status"] == "invalidated"


def test_legacy_migration_second_run_does_not_change_decided_at(tmp_path):
    """L48: 冪等性テストが「1 回目の結果が維持される」しか見ていなかった
    — 2 回目の init_db 後に decided_at が変化していないことを確認する。"""
    db_path = tmp_path / "t.db"
    conn = _seed_legacy_schema(db_path)
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, created_at) "
        "VALUES ('plugin', ?, 'pending', ?)",
        (json.dumps({"name": "idempotent_decided_at"}), NOW.isoformat()))
    conn.commit()
    conn.close()
    conn2 = connect(db_path)
    init_db(conn2)
    first = conn2.execute(
        "SELECT decided_at FROM approval_requests WHERE "
        "json_extract(payload_json, '$.name')='idempotent_decided_at'"
    ).fetchone()["decided_at"]
    conn2.close()
    conn3 = connect(db_path)
    init_db(conn3)
    second = conn3.execute(
        "SELECT decided_at FROM approval_requests WHERE "
        "json_extract(payload_json, '$.name')='idempotent_decided_at'"
    ).fetchone()["decided_at"]
    assert first == second
