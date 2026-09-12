"""T2 (test-hygiene 設計書 2026-09-12): `plugin.switch.sweep_orphans` の
`.locks/*.lock` startup 回収 (⑧)。指揮者裁定: startup sweep で回収
(decide 直後 unlink の flock+unlink TOCTOU を避ける)。ブリーフ完了条件:
pending が無い名前のロックは消える / pending がある名前のロックは残る /
`.locks/` ディレクトリ自体が無ければ何もしない。
"""
from __future__ import annotations

import fcntl
from datetime import datetime, timezone

import pytest

from agentic_fx.plugin import switch
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import db as db_store

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    return tmp_path, plugins_dir, conn


def _make_lock(plugins_dir, name):
    locks_dir = plugins_dir / ".locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = locks_dir / f"{name}.lock"
    lock_path.write_text("")
    return lock_path


def _pending_approval(conn, name):
    return approvals_store.create(
        conn, kind="plugin", payload={"name": name}, now=NOW, commit=True)


def test_sweep_orphans_removes_lock_with_no_pending_approval(env):
    tmp_path, plugins_dir, conn = env
    lock_path = _make_lock(plugins_dir, "orphaned_candidate")

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)

    assert not lock_path.exists()


def test_sweep_orphans_keeps_lock_with_pending_approval(env):
    tmp_path, plugins_dir, conn = env
    lock_path = _make_lock(plugins_dir, "in_progress_candidate")
    _pending_approval(conn, "in_progress_candidate")

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)

    assert lock_path.exists()


def test_sweep_orphans_keeps_lock_held_by_flock_even_without_pending_approval(env):
    """逆変異対象: 「pending 突合」を外して無条件削除にする変異は、進行中
    (別プロセスが flock 保持中) のロックまで消してしまう事故になる。ここで
    は pending 行が **無い**状態でロックを外部から flock 保持させ、それでも
    sweep が消さないことを確認する (fail closed — 保持中は必ず残す)。"""
    tmp_path, plugins_dir, conn = env
    lock_path = _make_lock(plugins_dir, "held_candidate")

    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)
        assert lock_path.exists()
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def test_sweep_orphans_does_nothing_when_locks_dir_absent(env):
    tmp_path, plugins_dir, conn = env
    assert not (plugins_dir / ".locks").exists()

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)

    assert not (plugins_dir / ".locks").exists()


def test_sweep_orphans_ignores_non_lock_entries_in_locks_dir(env):
    tmp_path, plugins_dir, conn = env
    locks_dir = plugins_dir / ".locks"
    locks_dir.mkdir(parents=True)
    stray = locks_dir / "README.txt"
    stray.write_text("not a lock file")

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)

    assert stray.exists()


def test_sweep_orphans_skips_corrupt_payload_row_without_aborting_the_sweep(env):
    """検収是正 C2 (test-hygiene 2026-09-12): 旧実装は集合内包表記で全
    `approval_requests` 行を一括 `json.loads` していたため、1 行でも
    `payload_json` が壊れている (不正 JSON・非 dict) と例外がそのまま
    送出され `.locks/` 回収そのものが止まっていた。壊れた行を混在させても
    (a) 正常な pending 行に対応するロックは残り、(b) 対応する pending が
    無いロックは回収される、の両方を確認する (= 例外が握り潰されて
    sweep 全体が継続していることの間接証拠)。"""
    tmp_path, plugins_dir, conn = env
    pending_lock = _make_lock(plugins_dir, "healthy_pending")
    orphan_lock = _make_lock(plugins_dir, "orphaned_candidate")
    _pending_approval(conn, "healthy_pending")
    # 壊れた行 (不正 JSON) を直接挿入する — approvals_store.create は
    # 常に valid JSON を書くため、ここだけ生 SQL で汚す。
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, created_at) "
        "VALUES ('plugin', ?, ?)", ("{not valid json", NOW.isoformat()))
    # 非 dict payload (リスト) の行も混在させる。
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, created_at) "
        "VALUES ('plugin', ?, ?)", ("[1, 2, 3]", NOW.isoformat()))
    conn.commit()

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)

    assert pending_lock.exists(), "壊れた行の混在で正常な pending ロックまで消えた"
    assert not orphan_lock.exists(), "壊れた行の例外で sweep が途中停止し孤児ロックが残った"


def test_sweep_orphans_skips_pending_row_whose_name_is_not_a_string(env):
    """検収是正 C5 (codex 1 周目 Important, test-hygiene 2026-09-12):
    C2 の per-entry 隔離は JSON 解析エラー/非 dict payload までしか
    カバーしておらず、`payload["name"]` 自体が非 str (list/dict 等) だと
    `pending_names.add(name)` が `TypeError: unhashable type` で例外を
    送出し、sweep 全体が再び止まっていた。`{"name": ["x"]}` という
    pending 行が混在していても、他の孤児ロックは正しく回収されることを
    確認する。"""
    tmp_path, plugins_dir, conn = env
    orphan_lock = _make_lock(plugins_dir, "orphaned_candidate")
    another_pending_lock = _make_lock(plugins_dir, "healthy_pending")
    _pending_approval(conn, "healthy_pending")
    # name が非 str (list) の pending 行。
    approvals_store.create(
        conn, kind="plugin", payload={"name": ["x"]}, now=NOW, commit=True)

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)

    assert not orphan_lock.exists(), (
        "name が非 str の pending 行が混在すると sweep が途中停止し "
        "孤児ロックが残った")
    assert another_pending_lock.exists(), "正常な pending ロックまで消えた"
