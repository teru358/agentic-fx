"""起動時 reconcile: gc_roots 完成 + journal-first/sweep-last
(プラン 10 Task 11f、設計書 §5.1・§5.3、§8.1-34)。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.plugin import switch
from agentic_fx.plugin import switch as plugin_switch
from agentic_fx.plugin import version_store
from agentic_fx.activity import ActivityLog
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import db as db_store
from tests.fixtures.wiring_envs import (
    SETTINGS_FIXTURE as SETTINGS,
    activity_text as _activity_text,
    bump_indicator_version as _bump_indicator_version,
    reconcile_env as _reconcile_env,
    stage_switched_journal as _stage_switched_journal,
)

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


def test_gc_roots_includes_manually_placed_live_symlink_without_approval_row(env):
    """段 0 M04 の killer: `gc_roots` ③ (live symlink の指す先) を丸ごと
    無効化しても緑になった。既存の
    `test_gc_roots_includes_approved_symlink_and_journal_and_legacy_plain`
    は symlink 先を ① (approved payload の artifact_hash) 経由でも満たせて
    しまう恒真テストなので、本テストは対応する approval 行 (approved でも
    legacy_plain_present pending でも) を一切持たない live symlink (DB
    移行後・手動配置想定) だけを作り、①②④ のいずれも満たさない構成で
    ③ 単独の効果を確かめる。"""
    tmp_path, plugins_dir, conn = env
    d, a = _make_version(plugins_dir, "manual")
    (plugins_dir / "manual").symlink_to(f".versions/manual/{a}")

    roots = version_store.gc_roots(conn, plugins_root=plugins_dir)

    assert d.resolve() in roots


def test_gc_roots_includes_temp_link_path_itself(env):
    """段 0 M05 の killer: `gc_roots` ④ のうち `temp_path` の項
    (`if temp_path: roots.add(...)`) だけを落としても緑になった。temp
    symlink 自体のパス (`plugins/.<name>.link-<op_id>`) は
    new_target/old_target からは導出できない別のパスであり、これを ④ が
    個別に root 集合へ含めることを直接確かめる (temp link を実際には disk
    に作らないことで、new_target/old_target 経由の `.resolve()` に巻き
    込まれて偶然一致する恒真化を避ける)。"""
    tmp_path, plugins_dir, conn = env
    op_id = switch.begin_switch_journal(
        conn, kind="approve", approval_id=1, name="tmp_only", old_kind="absent",
        old_target=None, new_target=f".versions/tmp_only/{'a' * 64}",
        switch_required=True, actor="human", now=NOW, commit=True)

    roots = version_store.gc_roots(conn, plugins_root=plugins_dir)

    expected = (plugins_dir / f".tmp_only.link-{op_id}").resolve()
    assert expected in roots


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


def test_sweep_orphans_deletes_staging_candidate_without_pending_approval(env):
    """確定-10: `sweep_orphans` ① (孤児 staging 削除) には既存テストが
    1 本も無かった (`tests/plugin/test_reconcile.py` の sweep テストは
    版ディレクトリ ②③ だけを扱う)。対応する pending approval_request の
    候補パスが無い staging 候補は削除される。"""
    tmp_path, plugins_dir, conn = env
    candidate_dir = plugins_dir / "_staging" / "1" / "orphan"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "plugin.py").write_text("x")

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)

    assert not candidate_dir.exists()


def test_sweep_orphans_does_not_chmod_file_symlink_target(env):
    tmp_path, plugins_dir, conn = env
    external = tmp_path / "external.txt"
    external.write_text("outside")
    external.chmod(0o400)
    candidate_dir = plugins_dir / "_staging" / "1" / "orphan"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "external-link").symlink_to(external)

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)

    assert external.stat().st_mode & 0o777 == 0o400
    assert not candidate_dir.exists()


def test_sweep_orphans_does_not_follow_dir_symlink_candidate(env):
    """2 周目 codex I1 (R01): 候補や mission dir が **dir symlink** のとき
    os.walk/is_dir が追って外部 dir を chmod/rmtree する。link 自体だけ消す。"""
    tmp_path, plugins_dir, conn = env
    external_dir = tmp_path / "external_dir"
    external_dir.mkdir()
    external_file = external_dir / "keep.txt"
    external_file.write_text("outside")
    external_file.chmod(0o400)
    external_dir.chmod(0o500)
    mission_dir = plugins_dir / "_staging" / "1"
    mission_dir.mkdir(parents=True)
    (mission_dir / "linked-candidate").symlink_to(external_dir)
    (plugins_dir / "_staging" / "2").symlink_to(external_dir)

    try:
        switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)
    finally:
        external_dir.chmod(0o700)

    assert external_file.exists()
    assert external_file.stat().st_mode & 0o777 == 0o400
    assert not (mission_dir / "linked-candidate").exists()
    assert not (plugins_dir / "_staging" / "2").is_symlink()


def test_sweep_orphans_logs_rmtree_failure_and_continues(env, monkeypatch):
    """L29: 1候補の削除失敗を記録し、後続候補を処理する。"""
    tmp_path, plugins_dir, conn = env
    first = plugins_dir / "_staging" / "1" / "first"
    second = plugins_dir / "_staging" / "1" / "second"
    first.mkdir(parents=True)
    second.mkdir()
    activity = ActivityLog(tmp_path / "activity.log")
    original_rmtree = switch.shutil.rmtree
    seen = []

    def flaky_rmtree(path, *args, **kwargs):
        seen.append(Path(path).name)
        if len(seen) == 1:
            raise OSError("simulated rmtree failure")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(switch.shutil, "rmtree", flaky_rmtree)

    switch.sweep_orphans(
        conn, plugins_root=plugins_dir, now=NOW, activity=activity)

    assert sorted(seen) == ["first", "second"]
    assert "sweep_orphan_staging_failed" in (tmp_path / "activity.log").read_text()
    assert sum(path.exists() for path in (first, second)) == 1


def test_sweep_orphans_deletes_readonly_staging_snapshot(env):
    """実機の _snapshot_src の形状 (2026-09-02 実測、_staging/38):
    _snapshot_src/_examples/<name>/ の 3 階層で dir はすべて 0500、
    file は 0400。1 階層だけ chmod する実装だと内側の 0500 dir で
    rmtree(ignore_errors=True) が黙って失敗しリークが再発するため、
    入れ子のまま pin する。"""
    tmp_path, plugins_dir, conn = env
    snapshot_dir = plugins_dir / "_staging" / "38" / "_snapshot_src"
    example_dir = snapshot_dir / "_examples" / "rsi_indicator"
    example_dir.mkdir(parents=True)
    plugin_file = example_dir / "plugin.py"
    plugin_file.write_text("x")
    plugin_file.chmod(0o400)
    example_dir.chmod(0o500)
    (snapshot_dir / "_examples").chmod(0o500)
    snapshot_dir.chmod(0o500)

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)

    assert not snapshot_dir.exists()


def test_sweep_orphans_deletes_readonly_stray_file_in_staging(env):
    """段 0 変異 M8 (2026-09-03) の pin: _staging/<id>/ 直下の候補が
    ディレクトリでなく読み取り専用ファイル (残骸) でも sweep が chmod +
    unlink で消す (dir 分岐しか検証していないと unlink 除去が生存する)。"""
    tmp_path, plugins_dir, conn = env
    mission_dir = plugins_dir / "_staging" / "39"
    mission_dir.mkdir(parents=True)
    stray = mission_dir / "leftover.txt"
    stray.write_text("x")
    stray.chmod(0o400)

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)

    assert not stray.exists()


def test_sweep_orphans_preserves_staging_candidate_referenced_by_pending_approval(env):
    """確定-10 の対称側: pending approval が参照している staging 候補は
    削除されない。"""
    tmp_path, plugins_dir, conn = env
    candidate_dir = plugins_dir / "_staging" / "1" / "kept"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "plugin.py").write_text("x")
    approvals_store.create(
        conn, kind="plugin",
        payload={"name": "kept", "artifact_hash": "a" * 64, "content_hash": "x",
                 "candidate_origin": "staging",
                 "candidate_path": "plugins/_staging/1/kept"}, now=NOW)

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)

    assert candidate_dir.exists()


def test_sweep_orphans_keeps_symlink_candidate_referenced_by_pending_approval(env):
    """3 周目 I1 (2026-09-05): symlink guard は `rel not in referenced` と
    組み合わさっていなければならない — pending approval が参照する候補が
    symlink でも link を消してはいけない (guard を `is_symlink()` 単独に
    する変異が全テスト緑で生存していた)。"""
    tmp_path, plugins_dir, conn = env
    real_dir = tmp_path / "real_candidate"
    real_dir.mkdir()
    (real_dir / "plugin.py").write_text("x")
    mission_dir = plugins_dir / "_staging" / "1"
    mission_dir.mkdir(parents=True)
    link = mission_dir / "kept"
    link.symlink_to(real_dir)
    approvals_store.create(
        conn, kind="plugin",
        payload={"name": "kept", "artifact_hash": "a" * 64, "content_hash": "x",
                 "candidate_origin": "staging",
                 "candidate_path": "plugins/_staging/1/kept"}, now=NOW)

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)

    assert link.is_symlink()
    assert (real_dir / "plugin.py").exists()


def test_sweep_orphans_preserves_temp_link_of_open_journal(env):
    """確定-11: `sweep_orphans` ④ の `gc_roots` 除外が pin されていな
    かった (`test_gc_roots_includes_temp_link_path_itself` は gc_roots の
    戻り値しか見ておらず、sweep_orphans がそれを尊重するかを見ていない)。
    非終端ジャーナルの temp link は gc_roots に含まれるため sweep で
    消えないことを直接確認する。"""
    tmp_path, plugins_dir, conn = env
    op_id = switch.begin_switch_journal(
        conn, kind="approve", approval_id=1, name="mid", old_kind="absent",
        old_target=None, new_target=f".versions/mid/{'a' * 64}",
        switch_required=True, actor="human", now=NOW, commit=True)
    temp_path = plugins_dir / f".mid.link-{op_id}"
    temp_path.symlink_to(".versions/mid/" + "a" * 64)  # 実体は無くて良い (dangling)

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)

    assert temp_path.is_symlink(), (
        "非終端ジャーナルが参照する temp link が sweep_orphans ④ で "
        "消されてしまった (確定-11 の欠陥)")


# --- [indicator-consumption-wiring] T4b Step 4-7: reconcile pin 破れ (R2) ---

def test_switched_journal_with_broken_pin_is_reverted(tmp_path):
    """R2: switch 直後 (journal `switched`、新 version dir 作成済) で停止 →
    依存 indicator を更新・承認 → 再起動 → `require` 解決に失敗 →
    live symlink は旧 target、journal `reverted`、approval は `pending`、
    **新 version dir は `.versions` に残る**、activity 逐語。"""
    conn, plugins_root, activity = _reconcile_env(tmp_path)
    from tests.fixtures import indicator_wiring as fx
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    old_target, new_target, approval_id, op_id = _stage_switched_journal(
        conn, plugins_root, name="rsi_pullback",
        pins={"rsi": hashes["rsi"]}, now=fx.NOW)
    assert (plugins_root / "rsi_pullback").readlink().as_posix() == new_target
    new_version_dir = plugins_root / new_target
    assert new_version_dir.is_dir()

    _bump_indicator_version(conn, plugins_root, "rsi", now=fx.NOW)  # I1 -> I2

    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=fx.NOW, settings=SETTINGS,
        activity=activity)

    assert (plugins_root / "rsi_pullback").readlink().as_posix() == old_target
    assert conn.execute(
        "SELECT phase FROM plugin_switch_journal WHERE op_id=?",
        (op_id,)).fetchone()["phase"] == "reverted"
    assert conn.execute(
        "SELECT status FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()["status"] == "pending"
    assert new_version_dir.is_dir()      # 回収は本束の範囲外 (codex r8 I2)
    # [indicator-consumption-wiring] T4b 逸脱: `_activity_text` の docstring
    # (tests/fixtures/wiring_envs.py) どおり「行全体の完全一致ではなく
    # event と summary 部分の一致」で見る — `ActivityLog.write` は
    # `event`/`summary` をタブ区切りで別列に書くため、プラン本文の
    # 単一文字列 in 判定 (空白連結) は実装のタブ区切りと食い違い、正しい
    # 実装でも必ず赤になる判別力ゼロの pin だった (着手前検証の取りこぼし)。
    text = _activity_text(activity)
    assert "switch_reverted" in text
    assert "reason=indicator_unresolved alias=rsi cause=pin_mismatch" in text


def test_switched_journal_with_intact_pin_proceeds_to_decided(tmp_path):
    """R2 の裏: pin が破れていなければ従来どおり `decided` まで進む。"""
    conn, plugins_root, activity = _reconcile_env(tmp_path)
    from tests.fixtures import indicator_wiring as fx
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    _old, _new, approval_id, op_id = _stage_switched_journal(
        conn, plugins_root, name="rsi_pullback",
        pins={"rsi": hashes["rsi"]}, now=fx.NOW)
    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=fx.NOW, settings=SETTINGS,
        activity=activity)
    assert conn.execute(
        "SELECT phase FROM plugin_switch_journal WHERE op_id=?",
        (op_id,)).fetchone()["phase"] == "decided"
    assert conn.execute(
        "SELECT status FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()["status"] == "approved"


def test_reconcile_resolution_holds_the_dependency_locks(tmp_path, monkeypatch):
    """段 0 束 2 M16 (SURVIVED) の pin: `_unresolved_after_switch` の
    `require` 再解決は **strategy + 依存 indicator の lock の内側**で行う。

    `with _plugin_locks(...)` を `if True:` へ潰す変異 (lock を取らずに
    inventory を読む) は R2 の 2 本を含め既存 pin を 1 本も落とさなかった。
    lock が無いと「reconcile が inventory を読む間に別プロセスが依存
    indicator を承認する」窓が開き、reconcile が古い inventory で
    「解決できた」と判断して live に未解決 strategy を残し得る (§2.4)。"""
    acquired: list[str] = []
    import contextlib as _ctx
    real = plugin_switch._plugin_lock

    @_ctx.contextmanager
    def _spy(root, name):
        acquired.append(name)
        with real(root, name):
            yield

    conn, plugins_root, activity = _reconcile_env(tmp_path)
    from tests.fixtures import indicator_wiring as fx
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    _stage_switched_journal(
        conn, plugins_root, name="rsi_pullback",
        pins={"rsi": hashes["rsi"]}, now=fx.NOW)
    _bump_indicator_version(conn, plugins_root, "rsi", now=fx.NOW)

    monkeypatch.setattr(plugin_switch, "_plugin_lock", _spy)
    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=fx.NOW, settings=SETTINGS,
        activity=activity)

    # [switch-ops-hardening] T4: pin 破れの巻き戻しも name lock の内側で
    # 行うようになった (§3.3.3 の 3 箇所目) ため、解決時の 2 本に続いて
    # **巻き戻しの 1 本**が増える。この pin の主旨 (解決が依存 lock の
    # 内側であること) は不変で、増えた 1 本は巻き戻し専用。
    assert acquired == [*sorted({"rsi_pullback", "rsi"}), "rsi_pullback"]
