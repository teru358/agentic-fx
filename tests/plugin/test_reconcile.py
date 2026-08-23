"""起動時 reconcile: gc_roots 完成 + journal-first/sweep-last
(プラン 10 Task 11f、設計書 §5.1・§5.3、§8.1-34)。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.plugin import switch, version_store
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import db as db_store

NOW = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    return tmp_path, plugins_dir, conn


def _make_version(plugins_dir, name, test_bytes=b"def test_x():\n    pass\n"):
    a = version_store.artifact_hash_bytes(
        b"def compute(df, params):\n    return {}\n", b"kind: indicator\n",
        test_bytes)
    d = version_store.create_version_dir(
        plugins_dir, name, a,
        plugin_py=b"def compute(df, params):\n    return {}\n",
        config_yaml=b"kind: indicator\n", test_plugin=test_bytes,
        op_identity="1")
    return d, a


def test_gc_roots_includes_approved_symlink_and_journal_and_legacy_plain(env):
    """R-i12 (統合裁定): Task 10 の strategy baseline 判定は「approved な
    approval の最新 payload が指す artifact_hash 版」を参照する。本テストの
    `d1 in roots` の assertion は、その版が `gc_roots()` ①集合 (approved
    payload の artifact_hash 版) に常に含まれ sweep で削除されないことを
    直接確認する — baseline が指す版と gc_roots ①が同じクエリ形状
    (`kind='plugin' AND status='approved'` の payload の `artifact_hash`)
    から導出されるため整合する。旧い approved 行の版が sweep で消えても
    baseline 自体は最新行 (= 常に gc_roots に含まれる行) を指すため問題ない
    という非対称性は、baseline 判定側 (Task 10、strategy_gate.py 相当、この
    worktree には未実装) が「最新の approved 行」を採る設計であることに
    依存する — Task 10 実装後に本コメントの前提を再確認すること。"""
    tmp_path, plugins_dir, conn = env
    d1, a1 = _make_version(plugins_dir, "approved_plugin")
    approvals_store.create(
        conn, kind="plugin",
        payload={"name": "approved_plugin", "artifact_hash": a1,
                 "content_hash": "x", "candidate_origin": "staging",
                 "candidate_path": "plugins/_staging/1/approved_plugin"},
        now=NOW)
    conn.execute("UPDATE approval_requests SET status='approved'")
    conn.commit()

    d2, a2 = _make_version(plugins_dir, "legacy_pending", b"v2\n")
    approvals_store.create(
        conn, kind="plugin",
        payload={"name": "legacy_pending", "artifact_hash": a2,
                 "content_hash": "y", "candidate_origin": "human",
                 "candidate_path": "plugins/_human/legacy_pending"},
        now=NOW)
    conn.execute(
        "UPDATE approval_requests SET reason='legacy_plain_present' "
        "WHERE payload_json LIKE '%legacy_pending%'")
    conn.commit()

    d3, a3 = _make_version(plugins_dir, "journal_only", b"v3\n")
    op_id = switch.begin_switch_journal(
        conn, kind="approve", approval_id=999, name="journal_only",
        old_kind="absent", old_target=None,
        new_target=f".versions/journal_only/{a3}", switch_required=True,
        actor="human", now=NOW, commit=True)

    roots = version_store.gc_roots(conn, plugins_root=plugins_dir)
    assert d1.resolve() in roots
    assert d2.resolve() in roots
    assert d3.resolve() in roots


def test_gc_roots_excludes_unreferenced_version(env):
    tmp_path, plugins_dir, conn = env
    d_orphan, _ = _make_version(plugins_dir, "orphan")
    roots = version_store.gc_roots(conn, plugins_root=plugins_dir)
    assert d_orphan.resolve() not in roots


def test_sweep_orphans_deletes_version_not_in_gc_roots(env):
    tmp_path, plugins_dir, conn = env
    d_orphan, _ = _make_version(plugins_dir, "orphan")
    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)
    assert not d_orphan.exists()


def test_sweep_orphans_preserves_gc_root_version(env):
    tmp_path, plugins_dir, conn = env
    d1, a1 = _make_version(plugins_dir, "kept")
    approvals_store.create(
        conn, kind="plugin",
        payload={"name": "kept", "artifact_hash": a1, "content_hash": "x",
                 "candidate_origin": "staging",
                 "candidate_path": "plugins/_staging/1/kept"}, now=NOW)
    conn.execute("UPDATE approval_requests SET status='approved'")
    conn.commit()
    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)
    assert d1.exists()


# precheck 2026-08-22 wave2: T11-B15 / T11-B16
# B-15 是正: `test_reconcile_runs_before_sweep_and_both_before_approved_plugins`
# と `test_reconcile_failure_does_not_block_startup` は自作 fake を自分で
# 順に呼んで自分で assert するだけで `service.py` を一切読まない (型 3、
# 何を壊しても緑のまま) ため削除した。実配線の順序 pin と失敗非伝播 pin は
# Step 5 (`tests/test_service_app.py`、`unittest.mock.patch` で実関数を
# spy に差し替え `call_args_list`/spy 呼び出しを assert する) に一本化する。


def test_sweep_alone_does_not_delete_version_referenced_by_open_journal(env):
    """B-16 是正: 旧テスト名は「sweep を journal より先に走らせると消える」
    だったが、本体は `assert d3.exists()` (= 消えない) で名前と逆のことを
    検証しており、かつ「消えるはずの危険」を再現してもいなかった。
    `gc_roots` ④ (非終端ジャーナルの new_target/old_target/temp_path) は
    reconcile の実行有無に関わらず参照されるため、sweep 単体を
    reconcile より先に呼んでも journal が参照する版は削除されない、という
    **安全側の性質**として正しく命名し直す。順序 (journal-first/sweep-last)
    そのものの必要性は `test_reconcile_runs_before_sweep_and_both_before_approved_plugins`
    (Step 5、実配線 pin) が別途担保する。"""
    tmp_path, plugins_dir, conn = env
    d3, a3 = _make_version(plugins_dir, "mid_flight", b"v-mid\n")
    switch.begin_switch_journal(
        conn, kind="approve", approval_id=42, name="mid_flight",
        old_kind="absent", old_target=None,
        new_target=f".versions/mid_flight/{a3}", switch_required=True,
        actor="human", now=NOW, commit=True)
    # reconcile を呼ばずに sweep だけ呼ぶ (journal-first を経ない順序)
    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)
    # gc_roots ④ が非終端ジャーナルの new_target を含むため消えない
    assert d3.exists()
