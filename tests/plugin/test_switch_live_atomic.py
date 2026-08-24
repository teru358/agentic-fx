"""switch_live の原子性 pin (検収 B1、設計書 §5.1 手順 6)。

設計は「切替 (原子操作 1 回)」を要求する: temp symlink を作り、
`os.rename(temp, plugins/<name>)` の 1 rename だけで旧 symlink/absent から
新 symlink へ移る。「live が無い瞬間」が存在しないことが不変条件 (§5.3)。
段 0 変異 (i) は `switch_live` を `live.unlink() → live.symlink_to(new_target)
→ temp.unlink()` の非 atomic な 2 段に置き換えたが、既存テストは終状態が
rename と同一であるため区別できず全緑のまま survive した
(検収 acceptance-task11.md B1)。

本テストは (1) `os.rename` がちょうど 1 回、`(temp_path, live_path)` の
引数で呼ばれること (2) live パス自身に対する `unlink` が一切呼ばれない
こと の 2 点を pin する。2 段化変異は `live.unlink()` を呼び、かつ最終
遷移を `os.rename` ではなく `temp.unlink()` (自身への unlink であり live
ではない) で終えるため、(1)(2) いずれの assert も red になる。
"""
from __future__ import annotations

import os
from pathlib import Path

from agentic_fx.plugin import switch


def test_switch_live_is_a_single_atomic_rename_with_no_live_unlink(tmp_path, monkeypatch):
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    name = "sma"
    old_hash = "a" * 64
    new_hash = "b" * 64
    (plugins_root / ".versions" / name / old_hash).mkdir(parents=True)
    (plugins_root / ".versions" / name / new_hash).mkdir(parents=True)
    live = plugins_root / name
    live.symlink_to(f".versions/{name}/{old_hash}")

    rename_calls = []
    real_rename = os.rename

    def spy_rename(src, dst, *a, **kw):
        rename_calls.append((os.fspath(src), os.fspath(dst)))
        return real_rename(src, dst, *a, **kw)

    unlink_calls = []
    real_unlink = Path.unlink

    def spy_unlink(self, *a, **kw):
        unlink_calls.append(str(self))
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(switch.os, "rename", spy_rename)
    monkeypatch.setattr(Path, "unlink", spy_unlink)

    new_target = f".versions/{name}/{new_hash}"
    switch.switch_live(plugins_root, name, new_target=new_target, op_id=1)

    assert live.is_symlink()
    assert live.readlink().as_posix() == new_target

    temp_path_str = str(plugins_root / f".{name}.link-1")
    assert rename_calls == [(temp_path_str, str(live))], (
        "switch_live は temp symlink → live への os.rename をちょうど 1 回 "
        "だけ呼ぶこと (段 0 変異 (i): 2 段化すると unlink→symlink→temp.unlink "
        "になり、この形の rename 呼び出しが消える)")
    assert str(live) not in unlink_calls, (
        "live パス自身への unlink が発生した — 「live が無い瞬間」が生じる "
        "非 atomic な切替 (段 0 変異 (i))")


def test_switch_live_from_absent_is_also_a_single_rename(tmp_path, monkeypatch):
    """live が absent (初回切替) のときも同じ 1 rename 経路を通ること。"""
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    name = "sma"
    new_hash = "c" * 64
    (plugins_root / ".versions" / name / new_hash).mkdir(parents=True)
    live = plugins_root / name

    rename_calls = []
    real_rename = os.rename

    def spy_rename(src, dst, *a, **kw):
        rename_calls.append((os.fspath(src), os.fspath(dst)))
        return real_rename(src, dst, *a, **kw)

    monkeypatch.setattr(switch.os, "rename", spy_rename)

    new_target = f".versions/{name}/{new_hash}"
    switch.switch_live(plugins_root, name, new_target=new_target, op_id=7)

    temp_path_str = str(plugins_root / f".{name}.link-7")
    assert rename_calls == [(temp_path_str, str(live))]
    assert live.is_symlink()


def test_revert_one_symlink_restore_is_a_single_atomic_rename_with_no_live_unlink(
        tmp_path, monkeypatch):
    """段 0 M10 の killer: 検収 B1 の原子性 pin (上の 2 本) は `switch_live`
    にしか無く、まったく同じ temp symlink + 1 rename パターンを持つ
    `_revert_one` の symlink 復元分岐 (`old_kind == "symlink"`) には pin が
    無かった。`_revert_one` を直接呼び、`os.rename` がちょうど 1 回・
    `(temp_path, live_path)` の引数で呼ばれること、live パス自身への
    `unlink` が一切発生しないことを、上の `switch_live` テストと同じ手法で
    確かめる (実害は switch_live の原子性欠落より重い — `_revert_one` は
    「切替失敗後の後始末」なので、非 atomic な 2 段化の途中で落ちると live
    が存在しない状態が残り、かつ phase を書く**前**に FS を触るため中断時の
    ジャーナルは 'switched' のまま・live は absent という自動収束しない
    終状態になる)。"""
    from datetime import datetime, timezone

    from agentic_fx.store import db as db_store
    from agentic_fx.store import plugin_switch_journal as journal_store

    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    name = "sma"
    old_hash = "a" * 64
    new_hash = "b" * 64
    (plugins_root / ".versions" / name / old_hash).mkdir(parents=True)
    (plugins_root / ".versions" / name / new_hash).mkdir(parents=True)
    old_rel = f".versions/{name}/{old_hash}"
    new_rel = f".versions/{name}/{new_hash}"
    live = plugins_root / name
    live.symlink_to(new_rel)  # 切替 (rename) は完了しているが decide はまだ

    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    now = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
    op_id = switch.begin_switch_journal(
        conn, kind="approve", approval_id=1, name=name, old_kind="symlink",
        old_target=old_rel, new_target=new_rel, switch_required=True,
        actor="human", now=now, commit=True)
    switch.advance_switch_journal(conn, op_id, phase="versioned", now=now, commit=True)
    switch.advance_switch_journal(conn, op_id, phase="recorded", now=now, commit=True)
    switch.advance_switch_journal(conn, op_id, phase="switched", now=now, commit=True)
    row = journal_store.get(conn, op_id)

    rename_calls = []
    real_rename = os.rename

    def spy_rename(src, dst, *a, **kw):
        rename_calls.append((os.fspath(src), os.fspath(dst)))
        return real_rename(src, dst, *a, **kw)

    unlink_calls = []
    real_unlink = Path.unlink

    def spy_unlink(self, *a, **kw):
        unlink_calls.append(str(self))
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(switch.os, "rename", spy_rename)
    monkeypatch.setattr(Path, "unlink", spy_unlink)

    switch._revert_one(conn, row, plugins_root=plugins_root, now=now)

    assert live.is_symlink()
    assert live.readlink().as_posix() == old_rel

    temp_path_str = str(plugins_root / f".{name}.link-{op_id}")
    assert rename_calls == [(temp_path_str, str(live))], (
        "_revert_one の symlink 復元は switch_live と同じ temp symlink → "
        "os.rename の 1 rename だけで行うこと (段 0 M10: unlink→symlink_to の "
        "非 atomic 2 段化するとこの形の rename 呼び出しが消える)")
    assert str(live) not in unlink_calls, (
        "live パス自身への unlink が発生した — 「live が無い瞬間」が生じる "
        "非 atomic な復元 (段 0 M10)")


def test_switch_live_replaces_stale_dangling_temp_symlink(tmp_path):
    """確定-9: temp 後始末ガード (`temp.exists() or temp.is_symlink()`) は
    dangling symlink (`symlink_to` 直後・rename 直前のクラッシュで残った
    temp) を検出して消してから作り直す必要がある。`temp.exists()` だけ
    (`or temp.is_symlink()` が無い) だと dangling symlink には False を
    返すため、`temp.symlink_to(target)` が `FileExistsError` で恒久失敗
    する (verified-local-round1.md 確定-9)。"""
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    name = "sma"
    new_hash = "d" * 64
    (plugins_root / ".versions" / name / new_hash).mkdir(parents=True)

    # 前回のクラッシュで残った dangling temp symlink (symlink_to 直後・
    # rename 直前で落ちた想定 — リンク先が存在しない)。
    temp_path = plugins_root / f".{name}.link-9"
    temp_path.symlink_to(".versions/sma/" + "0" * 64)  # 存在しない target
    assert temp_path.is_symlink() and not temp_path.exists()

    new_target = f".versions/{name}/{new_hash}"
    switch.switch_live(plugins_root, name, new_target=new_target, op_id=9)

    live = plugins_root / name
    assert live.is_symlink()
    assert live.readlink().as_posix() == new_target
