"""materialize / retire / approval retry の legacy plain E2E
(プラン 10 Task 11e、設計書 §2.3・§5.1、§8.1-33)。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import load_settings
from agentic_fx.plugin import loader, switch
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import db as db_store

NOW = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)

INDICATOR_PY = "def compute(df, params):\n    return {'v': 1.0}\n"
CONFIG_YAML = "kind: indicator\n"
TEST_PY_OK = "def test_x():\n    pass\n"


def _write(dirpath: Path) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "plugin.py").write_text(INDICATOR_PY)
    (dirpath / "config.yaml").write_text(CONFIG_YAML)
    (dirpath / "test_plugin.py").write_text(TEST_PY_OK)


@pytest.fixture
def env(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / ".locks").mkdir()
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    settings = load_settings(Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
    return tmp_path, plugins_dir, conn, settings


def test_materialize_copies_live_plain_to_human_readonly_by_human(env):
    tmp_path, plugins_dir, conn, settings = env
    _write(plugins_dir / "sma")

    dest = switch.materialize_plugin(plugins_dir, "sma")

    assert dest == plugins_dir / "_human" / "sma"
    assert (dest / "plugin.py").read_text() == INDICATOR_PY
    import stat
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    assert stat.S_IMODE((dest / "plugin.py").stat().st_mode) == 0o600


def test_materialize_rejects_if_human_dir_already_exists(env):
    tmp_path, plugins_dir, conn, settings = env
    _write(plugins_dir / "sma")
    switch.materialize_plugin(plugins_dir, "sma")
    with pytest.raises(FileExistsError):
        switch.materialize_plugin(plugins_dir, "sma")


def test_retire_rejects_when_unresolved_journal_exists(env):
    tmp_path, plugins_dir, conn, settings = env
    _write(plugins_dir / "sma")
    switch.begin_switch_journal(
        conn, kind="approve", approval_id=1, name="sma", old_kind="symlink",
        old_target=f".versions/sma/{'a' * 64}",
        new_target=f".versions/sma/{'b' * 64}",
        switch_required=True, actor="human", now=NOW, commit=True)
    with pytest.raises(switch.UnresolvedJournalError):
        switch.retire_plugin(conn, plugins_dir, "sma", now=NOW)


def test_retire_rejects_when_live_is_a_symlink(env):
    """段 0 M15 の killer: `retire_plugin` のガード
    (`if not live.is_dir() or live.is_symlink():`) から `or
    live.is_symlink()` を落とすと、正規管理下 (live symlink) の plugin を
    `plugin retire` が例外なく `_retired/` へ動かせてしまう — `_retired/`
    に移るのは版ストアを指す dangling symlink 本体なので、後続の
    `sweep_orphans` ③ で参照元 (live) を失った版が GC されうる復旧不能な
    状態になる (retire は legacy plain live にのみ適用される、§5.1)。"""
    tmp_path, plugins_dir, conn, settings = env
    (plugins_dir / ".versions" / "sma" / ("a" * 64)).mkdir(parents=True)
    live = plugins_dir / "sma"
    live.symlink_to(f".versions/sma/{'a' * 64}")

    with pytest.raises(ValueError, match="not a plain directory"):
        switch.retire_plugin(conn, plugins_dir, "sma", now=NOW)

    assert live.is_symlink()
    assert live.readlink().as_posix() == f".versions/sma/{'a' * 64}"
    assert not (plugins_dir / "_retired").exists()


def test_retire_renames_plain_dir_to_retired_with_timestamp(env):
    tmp_path, plugins_dir, conn, settings = env
    _write(plugins_dir / "sma")

    switch.retire_plugin(conn, plugins_dir, "sma", now=NOW)

    assert not (plugins_dir / "sma").exists()
    retired = list((plugins_dir / "_retired").iterdir())
    assert len(retired) == 1
    assert retired[0].name.startswith("sma-")
    assert (retired[0] / "plugin.py").read_text() == INDICATOR_PY


def test_retire_does_not_delete_content(env):
    """killer: retire が dir を削除する変異を殺す (§5 変異リスト逐語)。"""
    tmp_path, plugins_dir, conn, settings = env
    _write(plugins_dir / "sma")
    switch.retire_plugin(conn, plugins_dir, "sma", now=NOW)
    retired_dir = next((plugins_dir / "_retired").iterdir())
    assert (retired_dir / "config.yaml").exists()
    assert (retired_dir / "test_plugin.py").exists()


def test_retire_writes_plugin_retired_activity(env):
    """検収 B2 の pin: 設計書 §5.1 手順 0 / §5.1-1 は `plugin retire` に
    activity `plugin_retired` を要求する (旧稿は `switch.py` にコメントの
    みで未実装だった)。activity インスタンスを渡すと、event=`plugin_retired`
    の行が書かれ、name と retired_path を含むことを確認する。"""
    tmp_path, plugins_dir, conn, settings = env
    _write(plugins_dir / "sma")
    activity = ActivityLog(tmp_path / "logs" / "activity.log")

    switch.retire_plugin(conn, plugins_dir, "sma", now=NOW, activity=activity)

    lines = (tmp_path / "logs" / "activity.log").read_text().splitlines()
    assert len(lines) == 1
    ts, category, event, summary, ref_id = lines[0].split("\t")
    assert category == Category.APPROVAL.value
    assert event == "plugin_retired"
    assert "name=sma" in summary
    retired_dir = next((plugins_dir / "_retired").iterdir())
    assert f"retired_path={retired_dir}" in summary


def test_retire_without_activity_arg_still_succeeds(env):
    """`activity=None` (既定値) の後方互換 pin — CLI 以外の呼び出し元
    (テスト等) が activity を渡さなくても retire は成立する。"""
    tmp_path, plugins_dir, conn, settings = env
    _write(plugins_dir / "sma")
    switch.retire_plugin(conn, plugins_dir, "sma", now=NOW)
    assert not (plugins_dir / "sma").exists()


def test_retire_plugin_takes_plugin_flock_and_blocks_until_released(env):
    """killer (11e M3、統合裁定 簡略化 6 の解消): `retire_plugin` が
    `plugins/.locks/<name>.lock` の flock を取らない変異を殺す — lock を
    別ファイルディスクリプタで先に握っておくと `retire_plugin` はブロック
    し、解放後に完了することを実測する (multi-process test (11g) は
    approve/reject の組でしか flock を検証していないため、retire 単体の
    専用テストとして追加)。"""
    import fcntl
    import threading
    import time

    tmp_path, plugins_dir, conn, settings = env
    _write(plugins_dir / "sma")
    lock_path = plugins_dir / ".locks" / "sma.lock"
    lock_path.parent.mkdir(exist_ok=True)

    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)  # 先に flock を握る (retire を締め出す)

    started = threading.Event()
    finished = threading.Event()

    def _run_retire():
        started.set()
        switch.retire_plugin(conn, plugins_dir, "sma", now=NOW)
        finished.set()

    t = threading.Thread(target=_run_retire)
    t.start()
    started.wait(timeout=2)
    time.sleep(0.3)  # retire が flock を取っていれば、ここではまだブロック中
    assert not finished.is_set(), (
        "retire_plugin が flock を取らずに進んでしまった (M3 の変異が生きている)")
    assert (plugins_dir / "sma").is_dir() and not (plugins_dir / "sma").is_symlink()

    fcntl.flock(holder, fcntl.LOCK_UN)  # 解放すると retire が進む
    holder.close()
    t.join(timeout=2)
    assert finished.is_set()
    assert not (plugins_dir / "sma").exists()


def test_legacy_plain_e2e_pending_retire_retry_completes_switch(env, monkeypatch):
    """§8.1-33 の中核 E2E: legacy_plain_present pending →
    afx plugin retire → approval retry → absent→symlink 完了。"""
    tmp_path, plugins_dir, conn, settings = env
    _write(plugins_dir / "sma")  # legacy plain live
    _write(plugins_dir / "_human" / "sma_v2")  # 中身は同じでよい (別 test 内容にして artifact_hash を変える)
    (plugins_dir / "_human" / "sma_v2" / "test_plugin.py").write_text(
        TEST_PY_OK + "# v2\n")

    def _fake_pytest_ok(plugin_dir, *, settings):
        from agentic_fx.plugin.gate_pytest import GateResult
        return GateResult(passed=True, returncode=0, stdout_tail="ok", duration_sec=0.1)

    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)

    # 事前観察: discover の PluginMeta.path が legacy plain (plugins/sma) を指す
    meta_before = next(m for m in loader.discover(plugins_dir) if m.name == "sma")
    assert meta_before.path == plugins_dir / "sma"

    # bless --from _human 相当 (name="sma" を再利用して同名候補を承認)
    human_dir = plugins_dir / "_human" / "sma_v2"
    human_dir.rename(plugins_dir / "_human" / "sma")
    approval_id = switch.bless_candidate(
        conn, name="sma", human_dir=plugins_dir / "_human" / "sma",
        settings=settings, now=NOW, decided_by="human_cli")

    row = conn.execute("SELECT status, reason FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "pending"
    assert "legacy_plain_present" in (row["reason"] or "")
    # live はまだ変わっていない (plain のまま)
    assert (plugins_dir / "sma").is_dir() and not (plugins_dir / "sma").is_symlink()

    # 「再起動まで旧 PluginMeta.path を使う」— retire するまで discover は
    # plain のまま (再 discover しても plain の meta しか出ない、症状の pin)
    meta_mid = next(m for m in loader.discover(plugins_dir) if m.name == "sma")
    assert meta_mid.path == plugins_dir / "sma"

    switch.retire_plugin(conn, plugins_dir, "sma", now=NOW)

    # retire 直後: PluginMeta.path (旧プロセスがメモリに保持している値) は
    # まだ 'plugins/sma' という文字列のままだが、そのパスは物理的に
    # 存在しない (dangling) — hot reload しないので次回実行が失敗しうる
    # ことの構造的確認 (§5.1 の pin)。
    assert not meta_mid.path.exists()

    switch.retry_approval(conn, approval_id, decided_by="human", now=NOW,
                          plugins_root=plugins_dir, settings=settings)  # B-1

    row2 = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                        (approval_id,)).fetchone()
    assert row2["status"] == "approved"
    live = plugins_dir / "sma"
    assert live.is_symlink()

    # 再 discover (= 「次回起動」相当) して初めて新しい symlink 版を拾う
    meta_after = next(m for m in loader.discover(plugins_dir) if m.name == "sma")
    assert meta_after.path != meta_before.path
    assert meta_after.path.is_relative_to(plugins_dir / ".versions" / "sma")
