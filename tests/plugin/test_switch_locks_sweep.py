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

from agentic_fx.activity import ActivityLog
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


def test_sweep_orphans_keeps_lock_held_with_shared_lock_even_without_pending_approval(env):
    """ローカル 1 周目 pin (P3, test-hygiene 2026-09-12):
    `test_sweep_orphans_keeps_lock_held_by_flock_even_without_pending_
    approval` (上記) は外部保持を `LOCK_EX` (排他) にしているため、
    sweep 側が `LOCK_EX|LOCK_NB` を `LOCK_SH|LOCK_NB` (共有) へ緩める
    変異を殺せない — `LOCK_SH` の保持者に対して sweep が `LOCK_SH|
    LOCK_NB` を試みると (SH は SH 同士で両立するため) 取得に**成功**し、
    誤って unlink してしまう。ここでは外部保持を `LOCK_SH` にし、正しく
    `LOCK_EX|LOCK_NB` を試みる sweep なら (EX は既存の SH と非両立のため)
    取得に失敗して残ることを確認する。"""
    tmp_path, plugins_dir, conn = env
    lock_path = _make_lock(plugins_dir, "shared_held_candidate")

    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_SH)
        switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)
        assert lock_path.exists(), (
            "外部が LOCK_SH で保持中のロックが sweep で消えた — "
            "sweep 側が LOCK_EX ではなく LOCK_SH で試行している疑い")
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


def test_sweep_orphans_recovers_empty_name_lock_despite_empty_string_pending_row(env):
    """codex 2 周目 Minor M1 (test-hygiene 2026-09-12): `isinstance(name,
    str)` だけでは、`name=""` (空文字、plugin 名の正規形
    `^[a-z][a-z0-9_]{0,63}$` に合致しない) が `pending_names` にそのまま
    入ってしまう。`.locks/.lock` (ファイル名が単に `.lock` — 拡張子を
    剥がすと空文字になる孤児ロック) が「pending」と誤認され、回収され
    ないまま残留することが codex により実測された (指揮者のローカル
    1 周目では「空文字は実害なし」と却下したが、この残留実測を受けて
    採用に変更)。plugin 名の正規形と `fullmatch` しない `name` は
    corrupt 行として skip し、`.locks/.lock` は孤児ロックとして正しく
    回収されることを確認する。"""
    tmp_path, plugins_dir, conn = env
    empty_name_lock = _make_lock(plugins_dir, "")
    approvals_store.create(
        conn, kind="plugin", payload={"name": ""}, now=NOW, commit=True)

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)

    assert not empty_name_lock.exists(), (
        "name=\"\" の pending 行が誤って正規の名前として扱われ、"
        ".locks/.lock が回収されずに残った")


def test_sweep_orphans_records_activity_when_pending_name_has_a_path_separator(env):
    """codex 2 周目 Minor M1: `name` が str であっても plugin 名の正規形
    (パス区切りを含んではならない) に合致しなければ corrupt 扱いで
    activity に記録されることを確認する。"""
    tmp_path, plugins_dir, conn = env
    activity = ActivityLog(tmp_path / "activity.log")
    # `.locks/` ディレクトリ自体が無いと ⑧ branch そのものが skip される
    # (`locks_dir.is_dir()` ガード) ため、無関係なロックを 1 つ置いて
    # branch を実行させる。
    _make_lock(plugins_dir, "unrelated")
    approvals_store.create(
        conn, kind="plugin", payload={"name": "sub/name"}, now=NOW, commit=True)

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW,
                         activity=activity)

    log = (tmp_path / "activity.log").read_text()
    assert "sweep_locks_payload_corrupt" in log
    assert "sub/name" in log
