"""live symlink 切替と switch ジャーナルの状態機械 (プラン 10 Task 11c/11d/11e/11f、
設計書 §5.1)。"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal

from agentic_fx.activity import ActivityLog, Category  # B-2: journal_store 層に activity を書かせない
from agentic_fx.plugin.gate_pytest import run_gate_pytest  # M-8: モジュールレベル import (11d/11e/11g の monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", ...) seam が効くために必須)
from agentic_fx.store import plugin_switch_journal as journal_store  # Task 8 produces

_PHASE_ORDER = ["preparing", "versioned", "recorded", "switched", "decided", "reverted"]


# precheck 2026-08-22 wave2: T11-B2 / T11-B3 / T11-B1
def begin_switch_journal(
    conn: sqlite3.Connection, *, kind: Literal["approve", "bless"],
    approval_id: int, name: str, old_kind: Literal["absent", "symlink"],
    old_target: str | None, new_target: str, switch_required: bool,
    actor: str, now: datetime, commit: bool = False,
) -> int:
    """B-2 是正: `store/plugin_switch_journal.py` (Task 8 現物) が公開するのは
    `insert`/`get`/`set_phase`/`get_open_by_name`/`list_non_terminal` の 5 本
    のみ (`insert_preparing`/`update_phase`/`list_non_terminal_for_name` は
    非実在)。`insert` は DDL `temp_path TEXT NOT NULL` により INSERT 時の
    必須引数だが、temp_path は採番される op_id から導出する値なので、
    `insert(..., temp_path=<placeholder>)` でひとまず空文字を書き、確定した
    op_id で `set_temp_path` により正しい値へ UPDATE する 2 段構成にする。
    `set_temp_path(conn, op_id, temp_path) -> None` は本 task (Task 11) が
    `store/plugin_switch_journal.py` に**追加**する (Task 8 のシグネチャは
    変えない、追加のみ — 統合裁定 R-i4)。DDL 上 `temp_path` は NOT NULL だが
    空文字は許容される (CHECK 制約は無い) ため placeholder 挿入は安全。"""
    op_id = journal_store.insert(
        conn, kind=kind, approval_id=approval_id, name=name, old_kind=old_kind,
        old_target=old_target, new_target=new_target,
        switch_required=switch_required, actor=actor, now=now,
        temp_path="", commit=False)  # placeholder — 直後に set_temp_path で確定値へ更新
    temp_path = f"plugins/.{name}.link-{op_id}"
    journal_store.set_temp_path(conn, op_id, temp_path)  # Task 11 が追加する新関数
    if commit:
        conn.commit()
    return op_id


def advance_switch_journal(
    conn: sqlite3.Connection, op_id: int, *,
    phase: Literal["versioned", "recorded", "switched", "decided", "reverted"],
    now: datetime, commit: bool = False,
) -> None:
    row = journal_store.get(conn, op_id)
    if phase == "switched" and not row["switch_required"]:
        raise ValueError(
            f"op_id={op_id}: switch_required=0 の行は 'switched' phase を "
            "経ない (recorded から直接 decided へ進む — §5.1 手順 1)")
    journal_store.set_phase(conn, op_id, phase=phase, now=now, commit=False)  # B-2: Task 8 現物名
    if commit:
        conn.commit()


def switch_live(plugins_root: Path, name: str, *, new_target: str,
               op_id: int) -> None:
    """temp symlink 経由の 1 rename。live がプレーン dir ならこの関数を
    呼ばない (呼び出し元が事前に absent/symlink であることを確認する)。"""
    temp = plugins_root / f".{name}.link-{op_id}"
    if temp.exists() or temp.is_symlink():
        temp.unlink()
    temp.symlink_to(new_target)
    os.rename(temp, plugins_root / name)


def _revert_one(conn: sqlite3.Connection, row: dict, *, plugins_root: Path,
                now: datetime, activity: "ActivityLog | None" = None) -> None:
    live = plugins_root / row["name"]
    if row["phase"] == "switched" and row["switch_required"]:
        if row["old_kind"] == "absent":
            if live.is_symlink():
                live.unlink()
        else:  # symlink
            temp = plugins_root / f".{row['name']}.link-{row['op_id']}"
            if temp.exists() or temp.is_symlink():
                temp.unlink()
            temp.symlink_to(row["old_target"])
            os.rename(temp, live)
    journal_store.set_phase(conn, row["op_id"], phase="reverted", now=now, commit=False)  # B-2
    if activity is not None:
        activity.write(Category.APPROVAL, "switch_reverted",
                       f"name={row['name']} op_id={row['op_id']}")


def reconcile_switch_journals(conn: sqlite3.Connection, *,
                              plugins_root: Path, now: datetime,
                              settings,
                              activity: "ActivityLog | None" = None,
                              force_revert_op_id: int | None = None) -> None:
    """起動時 reconcile。非終端行に §5.1-1 の収束規則を適用する。

    B-3 是正: `force_revert_op_id` が指定された行は phase に依らず
    `_revert_one` を通す (= 割込操作の巻き戻しは switched の収束規則
    (live_target 判定) より優先する、表 2 の意味論)。旧稿は switched かつ
    force_revert_op_id 指定の行でも `live_target == new_norm` 判定に落ちて
    未定義の `retry_approval` を呼んでしまい (11c 単体では NameError)、
    巻き戻しが一切起きなかった — `test_interrupt_reverts_absent_by_removing_live_symlink`
    / `test_interrupt_reverts_symlink_by_restoring_old_target` が red のまま
    実装完了に至れない自己矛盾があった (B-3)。
    """
    for row in journal_store.list_non_terminal(conn):
        if force_revert_op_id is not None and row["op_id"] != force_revert_op_id:
            continue
        if force_revert_op_id is not None:
            # 割込操作の巻き戻しは収束規則より優先する (B-3)。phase が
            # switched でなくても _revert_one は非 FS 操作 (phase="reverted"
            # への書き換えのみ) に閉じるので安全。
            _revert_one(conn, row, plugins_root=plugins_root, now=now, activity=activity)
            conn.commit()
            continue
        if row["phase"] != "switched":
            # preparing/versioned/recorded: 「同じ操作の再試行」は 11d の
            # P2/P3 入口 (approve_candidate/bless_candidate) が頭から
            # 冪等に再実行する。ここ (起動時 reconcile) では FS 効果が
            # まだ無いので触らずスキップ — 実際の完了は次回の approve/
            # approval retry が担う (§5.1-1 (a))。
            continue
        live = plugins_root / row["name"]
        live_target = live.readlink().as_posix() if live.is_symlink() else None
        new_norm = row["new_target"]
        old_norm = row["old_target"]
        if live_target == new_norm:
            # switched は完遂しているが、decided への遷移は apply_decision と
            # 同一 tx で行う契約 (§4.3) — reconcile 自身は phase を書き換え
            # ない。11d/11g の再試行入口 (retry_approval → approve_candidate)
            # にそのまま委譲する (B-1 是正: plugins_root/settings を渡す)。
            # kind='bless' の行も retry_approval が approve_candidate へ
            # 委譲するので同じ入口で良い (11e 新規命名の retry_approval)。
            retry_approval(conn, row["approval_id"], decided_by="system_reconcile",
                            now=now, plugins_root=plugins_root, settings=settings)
        elif live_target == old_norm or (row["old_kind"] == "absent" and live_target is None):
            _revert_one(conn, row, plugins_root=plugins_root, now=now, activity=activity)
            conn.commit()
        else:
            # 第三者に触られた — activity ERROR、人間待ち。触らない。
            # B-2: journal_store 層に activity を書かせない (層違反) —
            # 呼び出し元がここで直接 activity.write する。
            if activity is not None:
                activity.write(Category.APPROVAL,
                               "switch_reconcile_unrecognized_live_target",
                               f"name={row['name']} op_id={row['op_id']}")
