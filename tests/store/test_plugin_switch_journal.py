"""plugin_switch_journal の行レベル CRUD (設計書 §5.1-1)。

**Task 8 が保証する範囲**: テーブルの存在、INSERT (op_id 採番)、phase
更新、name ごとの open (非終端) 行の検索、部分 UNIQUE (name ごと非終端
1 件) の DB レベル強制。`begin_switch_journal`/`advance_switch_journal`/
`reconcile_switch_journals` の意味論 (temp_path 命名規則・収束規則) は
Task 11 の `plugin/switch.py` が実装する — ここではテーブルの素の CRUD
だけを検証する。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import sqlite3

from agentic_fx.store import plugin_switch_journal as psj
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 8, 20, 13, 0, tzinfo=timezone.utc)


def test_insert_row_assigns_op_id(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    op_id = psj.insert(
        c, kind="approve", approval_id=1, name="rsi_v2", old_kind="absent",
        old_target=None, temp_path="plugins/.rsi_v2.link-1",
        new_target=".versions/rsi_v2/deadbeef", switch_required=True,
        actor="shell", now=NOW)
    assert isinstance(op_id, int)
    row = psj.get(c, op_id)
    assert row["phase"] == "preparing"
    assert row["name"] == "rsi_v2"


def test_set_phase_updates_phase_and_updated_at(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    op_id = psj.insert(c, kind="approve", approval_id=1, name="a",
                       old_kind="absent", old_target=None,
                       temp_path="plugins/.a.link-1",
                       new_target=".versions/a/x", switch_required=True,
                       actor="shell", now=NOW)
    psj.set_phase(c, op_id, "versioned", now=LATER)
    row = psj.get(c, op_id)
    assert row["phase"] == "versioned"
    assert row["updated_at"] == LATER.isoformat()  # L67: updated_at 未検証だった


def test_get_open_by_name_returns_only_non_terminal(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    op_id = psj.insert(c, kind="approve", approval_id=1, name="a",
                       old_kind="absent", old_target=None,
                       temp_path="plugins/.a.link-1",
                       new_target=".versions/a/x", switch_required=True,
                       actor="shell", now=NOW)
    assert psj.get_open_by_name(c, "a") is not None
    psj.set_phase(c, op_id, "decided", now=NOW)
    assert psj.get_open_by_name(c, "a") is None


def test_partial_unique_rejects_second_open_row_for_same_name(tmp_path):
    """部分 UNIQUE: 非終端 phase の行は name ごとに高々 1 件 (§8.1-31)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    psj.insert(c, kind="approve", approval_id=1, name="a", old_kind="absent",
              old_target=None, temp_path="plugins/.a.link-1",
              new_target=".versions/a/x", switch_required=True,
              actor="shell", now=NOW)
    with pytest.raises(sqlite3.IntegrityError,
                       match="UNIQUE constraint failed"):
        psj.insert(c, kind="approve", approval_id=2, name="a",
                  old_kind="absent", old_target=None,
                  temp_path="plugins/.a.link-2",
                  new_target=".versions/a/y", switch_required=True,
                  actor="shell", now=NOW)


def test_partial_unique_allows_new_row_after_terminal(tmp_path):
    """終端 (decided/reverted) 後は同名で新しい行を作れる。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    op_id1 = psj.insert(c, kind="approve", approval_id=1, name="a",
                        old_kind="absent", old_target=None,
                        temp_path="plugins/.a.link-1",
                        new_target=".versions/a/x", switch_required=True,
                        actor="shell", now=NOW)
    psj.set_phase(c, op_id1, "decided", now=NOW)
    op_id2 = psj.insert(c, kind="approve", approval_id=3, name="a",
                        old_kind="symlink", old_target=".versions/a/x",
                        temp_path="plugins/.a.link-2",
                        new_target=".versions/a/y", switch_required=True,
                        actor="shell", now=NOW)
    assert op_id2 != op_id1


def test_old_kind_check_constraint_rejects_plain(tmp_path):
    """old_kind は absent|symlink のみ (§4.2-3・§5.1 逐語 — プレーンは
    ジャーナルを作らない設計であり、DB 制約でも 'plain' を拒否する)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        psj.insert(c, kind="approve", approval_id=1, name="a",
                  old_kind="plain", old_target=None,
                  temp_path="plugins/.a.link-1", new_target=".versions/a/x",
                  switch_required=True, actor="shell", now=NOW)


def test_list_non_terminal_for_reconcile(tmp_path):
    """起動時 reconcile (Task 11) が「journal-first」で使う入口: 非終端
    行の一覧。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    psj.insert(c, kind="approve", approval_id=1, name="a", old_kind="absent",
              old_target=None, temp_path="plugins/.a.link-1",
              new_target=".versions/a/x", switch_required=True,
              actor="shell", now=NOW)
    psj.insert(c, kind="bless", approval_id=2, name="b", old_kind="absent",
              old_target=None, temp_path="plugins/.b.link-1",
              new_target=".versions/b/y", switch_required=True,
              actor="human", now=NOW)
    rows = psj.list_non_terminal(c)
    # L68: 集合比較は ORDER BY op_id の削除・逆転を検出しない — 順序付き
    # リストで確認する。
    assert [r["name"] for r in rows] == ["a", "b"]
    assert len(rows) == 2


def test_terminal_phase_constant_governs_open_and_non_terminal_queries(tmp_path):
    """L69 killer: `_TERMINAL_PHASES` は死に定数ではなく、
    `get_open_by_name`/`list_non_terminal` の実クエリを実際に駆動する。
    定数を増やすと (例えば 'versioned' を終端扱いに加える) 挙動が変わる
    ことを確認する — SQL に終端 phase の文字列リテラルが直書きされて
    いれば、定数を変えても挙動は変わらず本テストは検出できない。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    op_id = psj.insert(c, kind="approve", approval_id=1, name="a",
                       old_kind="absent", old_target=None,
                       temp_path="plugins/.a.link-1",
                       new_target=".versions/a/x", switch_required=True,
                       actor="shell", now=NOW)
    psj.set_phase(c, op_id, "versioned", now=NOW)
    assert psj.get_open_by_name(c, "a") is not None  # 現行定数では非終端

    original = psj._TERMINAL_PHASES
    try:
        psj._TERMINAL_PHASES = frozenset(original | {"versioned"})
        assert psj.get_open_by_name(c, "a") is None  # 定数変更が実クエリに反映
        assert psj.list_non_terminal(c) == []
    finally:
        psj._TERMINAL_PHASES = original
