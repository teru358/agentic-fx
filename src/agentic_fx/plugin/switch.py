"""live symlink 切替と switch ジャーナルの状態機械 (プラン 10 Task 11c/11d/11e/11f、
設計書 §5.1)。"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import sqlite3
import stat
from datetime import datetime
from pathlib import Path
from typing import Literal

from agentic_fx._safe_error import safe_error_text  # 検収 m10: per-row fault isolation の ERROR 記録に使う
from agentic_fx.activity import ActivityLog, Category  # B-2: journal_store 層に activity を書かせない
from agentic_fx.plugin import approval, history_git, loader, version_store
from agentic_fx.plugin.gate_pytest import (  # M-8: モジュールレベル import (11d/11e/11g の monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", ...) seam が効くために必須)
    check_candidate_snapshot, hashes_of, run_gate_pytest,
)
from agentic_fx.plugin.sandbox import SandboxError, check_source
from agentic_fx.store import approvals as approvals_store
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
    if row is None:
        # E2 裁定 (2026-08-25): 全呼び出し元は同一 tx 内で存在確認済みの
        # op_id を渡すため通常到達しないが、`row["phase"]` の不透明な
        # TypeError より明示的な fail closed を選ぶ。
        raise ValueError(f"op_id={op_id} not found")
    # 段 0 独立発見 (1) 是正: `_PHASE_ORDER` は定義行以外に参照が無い死に
    # コードだった。ここで単調性を実行時に強制する — 既に到達済みの phase
    # より前 (または同じ) へ `advance` しようとすると ValueError (fail
    # closed)。`_advance_to_decided` の 0d 再試行 (preparing/versioned/
    # recorded で止まったジャーナルを頭から再実行する経路) はこの関数を
    # 呼ぶ前に到達済み phase をスキップするガードを自前で持つ (呼び出し元
    # 側の責務 — この関数は「呼ばれたら必ず前進でなければならない」という
    # 単純な不変条件だけを守る)。
    current_idx = _PHASE_ORDER.index(row["phase"])
    new_idx = _PHASE_ORDER.index(phase)
    if new_idx <= current_idx:
        raise ValueError(
            f"op_id={op_id}: phase order violation ({row['phase']!r} -> "
            f"{phase!r}) — _PHASE_ORDER requires strictly forward progress "
            "(設計 §5.1 phase 表)")
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
    _atomic_symlink_swap(plugins_root, plugins_root / name, target=new_target,
                         temp_name=f".{name}.link-{op_id}")


def _atomic_symlink_swap(plugins_root: Path, live: Path, *, target: str,
                        temp_name: str) -> None:
    """`live` を `target` へ 1 rename で切り替える (§5.1 手順 6 の原子性)。
    `switch_live`/`_revert_one` の共通経路 — 段 0 M10 是正: 原子性 pin
    (検収 B1) が `switch_live` にしか無く、鏡像の `_revert_one` の symlink
    復元が非 atomic 2 段 (unlink→symlink_to) になり得た欠陥を、実装を
    1 本化することで再発させない形にする。`live` への `unlink` は一切
    行わない (「live が無い瞬間」を作らない)。"""
    temp = plugins_root / temp_name
    if temp.exists() or temp.is_symlink():
        temp.unlink()
    temp.symlink_to(target)
    os.rename(temp, live)


def _revert_one(conn: sqlite3.Connection, row: dict, *, plugins_root: Path,
                now: datetime, activity: "ActivityLog | None" = None) -> None:
    live = plugins_root / row["name"]
    if row["phase"] == "switched" and row["switch_required"]:
        if row["old_kind"] == "absent":
            if live.is_symlink():
                live.unlink()
        else:  # symlink
            _atomic_symlink_swap(plugins_root, live, target=row["old_target"],
                                temp_name=f".{row['name']}.link-{row['op_id']}")
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
        try:
            # 検収 m10 是正: 1 行の異常 (§5.1-1 の収束規則が「行ごとの規則」
            # であるにも関わらず、旧稿はループに per-row try/except が無く
            # 1 行の例外が他の name の収束を止め、かつ service.py 側の
            # 単一 try が reconcile/sweep/expire の 3 者を道連れにしていた
            # — この関数側の是正。呼び出し元 (service.py) の 3 者分離は
            # そちらで行う)。
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
                                now=now, plugins_root=plugins_root, settings=settings,
                                activity=activity)
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
        except Exception as exc:
            # m10: この行の異常で他の name の収束を止めない。DB tx は
            # 各分岐が自前で commit/rollback しているため、ここでの
            # rollback は不要 (未commit の書込は次回 reconcile が再試行する)。
            if activity is not None:
                activity.write(Category.APPROVAL, "switch_reconcile_row_failed",
                               f"name={row['name']} op_id={row['op_id']} "
                               f"error={safe_error_text(exc)}")


# precheck 2026-08-22 wave2: T11-B2
def sweep_orphans(conn: sqlite3.Connection, *, plugins_root: Path, now: datetime,
                  activity: "ActivityLog | None" = None) -> None:
    """§5.3 起動時 reconcile ①〜⑦ (journal-first の後に呼ぶこと)。B-2 是正:
    activity 記録は journal_store 層 (非実在の `record_activity_error`) では
    なく、呼び出し元であるこの関数が直接 `activity.write` する (層違反の
    解消)。M-10 是正: tmp スキップ条件は 1 桁の op_identity にしか一致しない
    バグがあったため `".tmp-" in name` の部分一致に変える。"""
    roots = version_store.gc_roots(conn, plugins_root=plugins_root)

    # ① 孤児 staging (対応する pending approval_request の候補パスが無いもの)
    staging_root = plugins_root / "_staging"
    if staging_root.is_dir():
        referenced = {
            json.loads(r["payload_json"]).get("candidate_path")
            for r in conn.execute(
                "SELECT payload_json FROM approval_requests "
                "WHERE kind='plugin' AND status='pending'")
        }
        for mission_dir in staging_root.iterdir():
            for candidate in mission_dir.iterdir():
                rel = f"plugins/_staging/{mission_dir.name}/{candidate.name}"
                if rel not in referenced:
                    shutil.rmtree(candidate, ignore_errors=True)

    # ② tmp-* の削除 (どの版にも成長していない残骸)
    versions_root = plugins_root / ".versions"
    if versions_root.is_dir():
        for name_dir in versions_root.iterdir():
            for tmp in name_dir.glob("*.tmp-*"):
                shutil.rmtree(tmp, ignore_errors=True)

        # ③ gc_roots に含まれない版ディレクトリの削除
        for name_dir in versions_root.iterdir():
            for version_dir in name_dir.iterdir():
                if ".tmp-" in version_dir.name:  # M-10: 部分一致 (② が既に消しているが、②③の順序が入れ替わっても安全にする)
                    continue
                if version_dir.resolve() not in roots:
                    # 版ストアは不変 (0500/0400、version_store.create_version_dir)
                    # のため、削除前に書き込み可能へ chmod し直す必要がある
                    # (プラン骨子の素の `shutil.rmtree(..., ignore_errors=True)`
                    # のままだと権限不足で silently 残ってしまう — 実測で
                    # 判明した逸脱、11f 実装時に追加)。
                    # 検収 m10 付随: 1 個の削除不能な孤児版で ⑤ (dangling
                    # symlink 検出) 以降が止まらないよう per-entry で隔離する。
                    try:
                        for f in version_dir.iterdir():
                            f.chmod(0o600)
                        version_dir.chmod(0o700)
                        shutil.rmtree(version_dir, ignore_errors=True)
                    except OSError as exc:
                        if activity is not None:
                            activity.write(
                                Category.APPROVAL, "sweep_orphan_version_failed",
                                f"path={version_dir} error={safe_error_text(exc)}")

    # ④ temp link の残骸削除 (gc_roots の temp_path は除く)
    for entry in plugins_root.glob(".*.link-*"):
        if entry.resolve() not in roots:
            entry.unlink(missing_ok=True)

    # ⑤ dangling symlink は activity ERROR に留める (触らない) — B-2: 直接 activity.write
    for entry in plugins_root.iterdir():
        if entry.name.startswith((".", "_")):
            continue
        if entry.is_symlink() and not entry.exists():
            if activity is not None:
                activity.write(Category.APPROVAL, "dangling_live_symlink",
                               f"name={entry.name}")

    # ⑥ _retired/_human には触れない (何もしない)
    # ⑦ legacy_plain_present の件数を activity へ (再試行はしない) — B-2: 直接 activity.write
    if activity is not None:
        legacy_count = conn.execute(
            "SELECT COUNT(*) c FROM approval_requests "
            "WHERE kind='plugin' AND status='pending' "
            "AND reason='legacy_plain_present'").fetchone()["c"]
        if legacy_count:
            activity.write(Category.APPROVAL, "legacy_plain_present_pending_count",
                           f"count={legacy_count}")


# ============================================================
# candidate_origin/candidate_path payload validator (§8.1-29、プラン10 Task 11d 逐語)
# ============================================================

_CANDIDATE_PATH_RE = {
    "staging": re.compile(r"^plugins/_staging/(\d+)/([a-z][a-z0-9_]{0,63})$"),
    "human": re.compile(r"^plugins/_human/([a-z][a-z0-9_]{0,63})$"),
}


class CandidateMissingError(Exception):
    """candidate_origin/candidate_path が指す候補が存在しない (§5.1 手順 3、
    codex 11 周目 I4)。承認は pending のまま + activity + 通知。"""


def resolve_candidate_dir(plugins_root: Path, *, candidate_origin: str,
                          candidate_path: str, name: str) -> Path:
    """payload の locator を検証し (`resolve()` は使わない — 字句上の
    正規形検査のみ、§2.3)、候補ディレクトリの絶対パスを返す。存在しなければ
    `CandidateMissingError`。"""
    pattern = _CANDIDATE_PATH_RE.get(candidate_origin)
    if pattern is None:
        raise ValueError(f"unknown candidate_origin: {candidate_origin!r}")
    m = pattern.match(candidate_path)
    if m is None or m.group(m.lastindex) != name:
        raise ValueError(
            f"candidate_path {candidate_path!r} does not match the "
            f"canonical form for candidate_origin={candidate_origin!r} "
            f"and name={name!r}")
    candidate_dir = plugins_root.parent / candidate_path  # candidate_path は "plugins/..." 形 (root 相対)
    # I2 是正 (verified-codex-round1.md / 設計 §2.3): `Path.is_dir()` は
    # symlink を追従するため、候補ディレクトリ自身が外部ディレクトリへの
    # symlink でも通ってしまっていた。`os.lstat` + `S_ISDIR` で symlink を
    # 追従せず「通常ディレクトリか」だけを見る (最終成分は
    # {不存在/通常ディレクトリ/正規形相対symlink} のいずれかに限る — ここは
    # 「正規形相対symlink」を許さない候補パスの検査なので通常ディレクトリ
    # のみを受理する)。
    try:
        st = os.lstat(candidate_dir)
    except OSError as exc:
        raise CandidateMissingError(
            f"candidate not found: {candidate_path} (origin={candidate_origin})"
        ) from exc
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError(
            f"candidate path {candidate_path!r} is not a regular directory "
            "(symlink/file are rejected — §2.3)")
    return candidate_dir


def _drop_staging_candidate(plugins_root: Path, payload: dict) -> None:
    """終端決定 (approved/rejected/expired/invalidated) の tx 直後に staging
    候補を削除する (§5.1 手順 3 の掃除所有表、verified-codex-round1.md I1)。
    `candidate_origin='human'` は人間所有なので触らない (自動削除しない)。"""
    if payload.get("candidate_origin") != "staging":
        return
    try:
        candidate_dir = resolve_candidate_dir(
            plugins_root, candidate_origin="staging",
            candidate_path=payload["candidate_path"], name=payload["name"])
    except (CandidateMissingError, ValueError, KeyError):
        return
    shutil.rmtree(candidate_dir, ignore_errors=True)


# ============================================================
# 承認 3 経路 (P1 submit / P2 approve / P3 bless) — §5.1
# ============================================================

@contextlib.contextmanager
def _plugin_lock(plugins_root: Path, name: str):
    """`plugins/.locks/<name>.lock` を blocking `flock(LOCK_EX)` で取る
    (§5.1 手順 1・11g の multi-process 期待どおり、`LOCK_NB` は使わない —
    reject/approve の競合は待ち合わせで解決する)。"""
    lock_dir = plugins_root / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{name}.lock"
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _run_full_gate(conn: sqlite3.Connection, candidate_dir: Path, *, name: str,
                   settings, now: datetime):
    """P1 手順 2〜7 / P3 手順 1〜2 の共有ゲート本体 (§8.1-41 pin: P1 と P3
    は同じゲートを通る — 二重実装しない)。合格すれば
    `(meta, content_hash, artifact_hash, metrics, evaluable)` を返す。
    不合格 (スナップショット不正・AST 不合格・pytest 不合格・hash 不一致・
    kind 別ゲート不合格) は ValueError — 何も作らない (fail closed)。"""
    check_candidate_snapshot(candidate_dir)  # 手順 2 (B-5: 名指し再利用)
    before_content, before_artifact = hashes_of(candidate_dir)  # 手順 3

    meta = loader._discover_one(candidate_dir, name)
    if meta is None:
        raise ValueError(
            f"plugin {name!r}: candidate at {candidate_dir} failed discovery "
            "validation (config/AST ゲート不合格 — ログ参照)")

    try:
        check_source(candidate_dir / "plugin.py")  # 手順 4
        check_source(candidate_dir / "test_plugin.py",
                    extra_allowed=frozenset({"pytest", "plugin"}))

        gate_result = run_gate_pytest(candidate_dir, settings=settings)  # 手順 5
        if not gate_result.passed:
            raise ValueError(
                f"plugin {name!r}: test_plugin.py failed pytest gate "
                f"(returncode={gate_result.returncode})")

        after_content, after_artifact = hashes_of(candidate_dir)  # 手順 6
        if after_content != before_content or after_artifact != before_artifact:
            raise ValueError(
                f"plugin {name!r}: candidate content changed during gate "
                "(hash mismatch) — refusing")

        metrics, evaluable = approval.run_kind_gate(  # 手順 7
            conn, meta, settings=settings, now=now)
    except SandboxError as exc:
        raise ValueError(f"plugin {name!r}: {exc}") from exc

    return meta, after_content, after_artifact, metrics, evaluable


def _candidate_locator(*, staging_dir: Path, candidate_origin: str, name: str,
                       mission_id: int | None) -> tuple[Path, str]:
    """`staging_dir` (P1 呼び出し元が渡す候補の親ディレクトリ) から
    `(plugins_root, candidate_path)` を逆算する。`staging_dir` は origin=
    staging なら `plugins/_staging/<mission_id>`、origin=human なら
    `plugins/_human` (B-1 是正の一環 — Interfaces は単一 `staging_dir`
    引数で両 origin を表す)。"""
    if candidate_origin == "staging":
        plugins_root = staging_dir.parent.parent
        candidate_path = f"plugins/_staging/{mission_id}/{name}"
    elif candidate_origin == "human":
        plugins_root = staging_dir.parent
        candidate_path = f"plugins/_human/{name}"
    else:
        raise ValueError(f"unknown candidate_origin: {candidate_origin!r}")
    return plugins_root, candidate_path


def submit_candidate(
    conn: sqlite3.Connection, *, name: str, staging_dir: Path,
    candidate_origin: Literal["staging", "human"], mission_id: int | None,
    backlog_id: int | None, settings, now: datetime,
) -> int:
    """P1 (submit) の入口。kind 別ゲートを通し、1 つの短い tx で pending
    approval 行を作る。ジャーナルは作らない (§5.1 冒頭の pin)。"""
    plugins_root, candidate_path = _candidate_locator(
        staging_dir=staging_dir, candidate_origin=candidate_origin,
        name=name, mission_id=mission_id)
    candidate_dir = staging_dir / name

    with _plugin_lock(plugins_root, name):
        meta, content_hash, artifact_hash, metrics, evaluable = _run_full_gate(
            conn, candidate_dir, name=name, settings=settings, now=now)

        payload = {
            "name": name, "kind": meta.kind,
            "candidate_origin": candidate_origin,
            "candidate_path": candidate_path,
            "content_hash": content_hash, "artifact_hash": artifact_hash,
            "metrics": metrics, "evaluable": evaluable,
            "mission_id": mission_id, "backlog_id": backlog_id,
        }
        conn.execute("BEGIN IMMEDIATE")
        try:
            approval_id = approvals_store.create(
                conn, kind="plugin", payload=payload, now=now, commit=False)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return approval_id


def _is_superseded(conn: sqlite3.Connection, row: dict, payload: dict) -> bool:
    """P2 手順 0c 後発決定確認 (ⓓ、key=`(name, content_hash)`): 同名で
    自分より新しい `approved` 決定が既に付いていて `content_hash` が
    異なるなら、この pending 行は陳腐化している (§8.1-39 の D4 pin — key
    は `(name, content_hash)` であり `name` 単独ではない。別 content_hash
    の他候補が reject/expire されても本関数は影響しない)。"""
    name = payload["name"]
    content_hash = payload["content_hash"]
    rows = conn.execute(
        "SELECT id, payload_json FROM approval_requests WHERE kind='plugin' "
        "AND status='approved' AND id > ?", (row["id"],)).fetchall()
    for r in rows:
        other_payload = json.loads(r["payload_json"])
        if (other_payload.get("name") == name
                and other_payload.get("content_hash") != content_hash):
            return True
    return False


def _finalize_decision(conn: sqlite3.Connection, approval_id: int, *,
                       op_id: int, decided_by: str, now: datetime) -> None:
    """§4.3 手順 9: `apply_decision(approved)` + ジャーナル `decided` 化を
    1 tx で行う (`apply_decision` 自身はジャーナルに触らない — B-2/§申し
    送り⑤どおり、Task 11 側がここで拡張する拡張点)。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        approvals_store.apply_decision(
            conn, approval_id, "approved", decided_by=decided_by, now=now,
            commit=False)
        journal_store.set_phase(conn, op_id, phase="decided", now=now, commit=False)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _advance_to_decided(
    conn: sqlite3.Connection, approval_id: int, *, op_id: int, name: str,
    plugins_root: Path, candidate_dir: Path, content_hash: str,
    artifact_hash: str, switch_required: bool, decided_by: str, now: datetime,
) -> None:
    """preparing 済みジャーナルを versioned → recorded → (switched →
    切替) → decided まで進める (P2 手順 5〜9 / P3 手順 6A〜11A の共有部)。

    段 0 独立発見 (1) 是正: 0d の「この approval 自身の再試行」経路は、
    'versioned'/'recorded' まで進んだジャーナルを頭から再実行するために
    この関数を再度呼ぶ (create_version_dir/record_version はどちらも
    冪等)。`advance_switch_journal` は段 0 で単調性チェックを獲得したため、
    既に到達済みの phase へ**後方**の advance を投げると ValueError になる
    — ここで到達済み phase をスキップするガードを持ち、その後方 advance
    自体を発生させない (§5.1-1 (a) の再試行冪等性を壊さない)。"""
    current_idx = _PHASE_ORDER.index(journal_store.get(conn, op_id)["phase"])

    version_dir = version_store.create_version_dir(
        plugins_root, name, artifact_hash,
        plugin_py=(candidate_dir / "plugin.py").read_bytes(),
        config_yaml=(candidate_dir / "config.yaml").read_bytes(),
        test_plugin=(candidate_dir / "test_plugin.py").read_bytes(),
        op_identity=str(op_id))
    if current_idx < _PHASE_ORDER.index("versioned"):
        advance_switch_journal(conn, op_id, phase="versioned", now=now, commit=True)
        current_idx = _PHASE_ORDER.index("versioned")

    history_git.record_version(
        plugins_root / ".history.git", name=name, artifact_hash=artifact_hash,
        content_hash=content_hash, approval_id=approval_id, version_dir=version_dir)
    if current_idx < _PHASE_ORDER.index("recorded"):
        advance_switch_journal(conn, op_id, phase="recorded", now=now, commit=True)
        current_idx = _PHASE_ORDER.index("recorded")

    if switch_required:
        if current_idx < _PHASE_ORDER.index("switched"):
            advance_switch_journal(conn, op_id, phase="switched", now=now, commit=True)
        new_target = f".versions/{name}/{artifact_hash}"
        switch_live(plugins_root, name, new_target=new_target, op_id=op_id)
        # 手順 8a: 切替後の再照合 (fail closed — 自動巻き戻しは
        # reconcile_switch_journals の switched 収束規則に委ねる。ここでは
        # 例外を送出して手順 9 (decide) へ進ませない)。確定-7: 旧稿は
        # content_hash のみを見ており、resume 経路
        # (`_reverify_switched_journal._new_target_hash_ok`) が持つ
        # artifact_hash 照合 + ディレクトリ名照合 (in-place 編集検出) が
        # 無かった (経路の非対称) — `_version_dir_hashes_ok` を共有する形へ
        # 揃える。
        resolved_after = (plugins_root / new_target).resolve()
        if not _version_dir_hashes_ok(
                resolved_after, content_hash=content_hash, artifact_hash=artifact_hash):
            # メッセージ中の "content_hash mismatch" 部分文字列は既存テスト
            # `test_approve_upgrade_reverifies_content_hash_after_switch`
            # の `pytest.raises(match=...)` が pin している (既存テスト
            # 書き換え禁止のため文言を維持)。
            raise RuntimeError(
                f"plugin {name!r}: live content_hash mismatch after switch "
                f"(expected content_hash={content_hash} "
                f"artifact_hash={artifact_hash})")

    _finalize_decision(conn, approval_id, op_id=op_id, decided_by=decided_by, now=now)


def _version_dir_hashes_ok(version_dir: Path, *, content_hash: str | None,
                          artifact_hash: str | None) -> bool:
    """確定-7: 版ディレクトリの内容が期待する `(content_hash, artifact_hash)`
    と一致し、かつディレクトリ名自体も `artifact_hash` と一致することを
    確認する (in-place 編集の検出 — §2.3・§7.1-37)。
    `_reverify_switched_journal` (resume 経路) と `_advance_to_decided`
    (fresh approve 経路) の切替後再照合が同じ規則を共有する
    (確定-7: 旧稿は fresh 経路が content_hash 1 値しか見ておらず、
    resume 経路だけが artifact_hash 2 値 + dir 名照合を持つ非対称だった)。"""
    if not version_dir.is_dir():
        return False
    try:
        # `loader.content_hash` (モジュール属性参照) を直接呼ぶ — 旧稿の
        # `_advance_to_decided` が `loader.content_hash(resolved_after)` を
        # 直呼びしていたのと同じ経路にする (`test_approve_upgrade_
        # reverifies_content_hash_after_switch` が `monkeypatch.setattr(
        # "agentic_fx.plugin.switch.loader.content_hash", ...)` でこの
        # 経路を差し替える契約を持つ — `gate_pytest.hashes_of` 内部の
        # `_content_hash` は import 時に束縛済みでこの monkeypatch の対象
        # にならない)。
        actual_content = loader.content_hash(version_dir)
        plugin_py = (version_dir / "plugin.py").read_bytes()
        config_yaml = (version_dir / "config.yaml").read_bytes()
        test_plugin = (version_dir / "test_plugin.py").read_bytes()
        actual_artifact = loader.artifact_hash_bytes(plugin_py, config_yaml, test_plugin)
    except OSError:
        return False
    return (actual_content == content_hash and actual_artifact == artifact_hash
            and version_dir.name == artifact_hash)


def _reverify_switched_journal(
    conn: sqlite3.Connection, journal_row: dict, *, plugins_root: Path,
    payload: dict, now: datetime, activity: "ActivityLog | None",
) -> bool:
    """検収 B3 是正 (設計 §5.1-1 (a)): 「再開前に参照物 (`new_target` の版・
    `old_target` の版・temp link) の存在と hash を再検証」する。旧稿は
    0d early-exit がこの再検証を一切行わず、`switched` まで進んだ行を
    無条件に `decided`+`approved` へ進めていた (検収 B3 実測 — crash probe
    C-HEALTHY で 8a の不一致検出が「次回起動で自動的に approve に化ける」
    ことが確認された)。

    `new_target` の版ディレクトリが欠損/hash 不一致なら、保持している
    候補 (`candidate_origin`/`candidate_path`、staging は pending の間残る・
    human は自動削除されない — §5.1 手順 3 の掃除所有表) から冪等に
    再作成を試みる。再作成できなければ journal を `reverted` で閉じて
    (live を旧状態へ 1 rename で戻す — `_revert_one` と同じ経路) activity
    ERROR (`switch_reverify_failed`) を書き、`False` を返す (呼び出し元は
    approval を pending のまま return する — apply_decision は一切呼ばない)。
    """
    name = journal_row["name"]
    new_target = journal_row["new_target"]
    expected_content_hash = payload.get("content_hash")
    expected_artifact_hash = payload.get("artifact_hash")

    def _new_target_hash_ok() -> bool:
        # I3 是正 (verified-codex-round1.md / 設計 §2.3・§7.1-37): reconcile
        # も loader と同じ規則で「版ディレクトリ名 == 実 artifact_hash」を
        # 照合する (in-place 編集の検出)。content_hash (2 本) だけでは
        # test_plugin.py だけの改変を見逃し、approved だが起動時ロード
        # 不能という不収束状態を作れた。確定-7: `_advance_to_decided` と
        # 実装を共有する (`_version_dir_hashes_ok`)。
        return _version_dir_hashes_ok(
            plugins_root / new_target, content_hash=expected_content_hash,
            artifact_hash=expected_artifact_hash)

    if _new_target_hash_ok():
        return True

    try:
        candidate_dir = resolve_candidate_dir(
            plugins_root, candidate_origin=payload["candidate_origin"],
            candidate_path=payload["candidate_path"], name=name)
        check_candidate_snapshot(candidate_dir)
        content_hash, artifact_hash = hashes_of(candidate_dir)
        if (content_hash != payload["content_hash"]
                or artifact_hash != payload["artifact_hash"]):
            raise ValueError(
                f"plugin {name!r}: candidate hash no longer matches payload "
                "during switched-journal reverify")
        # I3 是正: version_dir が既に new_target の名前 (=artifact_hash) で
        # 存在するが中身が改変されている場合、`create_version_dir` は
        # 冪等の早期 return (`final_dir.is_dir(): return final_dir`) で
        # 上書きしない (版ストア不変の設計と整合)。ここまで到達したのは
        # `_new_target_hash_ok()` が False (中身が壊れている) と分かって
        # いる場合のみなので、候補から再作成する前に stale な版を掃除する。
        version_dir = plugins_root / new_target
        if version_dir.is_dir():
            # advisor 指摘: in-place 編集の検出は設計 §2.3 が loader 側に
            # 要求する `plugin_artifact_hash_mismatch` ERROR と対称の記録を
            # 要する — ここで無言で差し替えると tampering の痕跡が残らない。
            if activity is not None:
                activity.write(
                    Category.APPROVAL, "switch_reverify_version_mismatch",
                    f"name={name} op_id={journal_row['op_id']} "
                    f"version_dir={version_dir} expected_artifact_hash="
                    f"{expected_artifact_hash} — 版ディレクトリの内容が "
                    "in-place 編集されていた可能性 (candidate から再構築する)")
            os.chmod(version_dir, 0o700)
            for f in version_dir.iterdir():
                os.chmod(f, 0o600)
            shutil.rmtree(version_dir)
        version_store.create_version_dir(
            plugins_root, name, artifact_hash,
            plugin_py=(candidate_dir / "plugin.py").read_bytes(),
            config_yaml=(candidate_dir / "config.yaml").read_bytes(),
            test_plugin=(candidate_dir / "test_plugin.py").read_bytes(),
            op_identity=str(journal_row["op_id"]))
    except (CandidateMissingError, ValueError, OSError):
        _revert_one(conn, journal_row, plugins_root=plugins_root, now=now, activity=activity)
        conn.commit()
        if activity is not None:
            activity.write(
                Category.APPROVAL, "switch_reverify_failed",
                f"name={name} op_id={journal_row['op_id']} "
                "new_target 版が欠損/hash 不一致で候補からの再作成も不可 "
                "— reverted で閉じた (§5.1-1 (a))")
        return False

    return _new_target_hash_ok()


def approve_candidate(
    conn: sqlite3.Connection, approval_id: int, *,
    decided_by: str, now: datetime, plugins_root: Path, settings,
    activity: "ActivityLog | None" = None,
) -> None:
    """P2 (approve) の入口。plugins_root は FS 操作 (版・git・切替) の起点、
    settings は将来のゲート再検証・kind 別分岐のために渡す (現行 P2 手順は
    gate を再実行しないが、シグネチャで揃えておくことで retry_approval/
    reconcile 経由の呼び出しと submit_candidate/bless_candidate の引数構成
    を統一する)。`activity` は B3 是正 (再開前の参照物再検証) が失敗した
    ときの ERROR 記録に使う (既定 None — 既存呼び出し元との後方互換)。"""
    approvals_store.expire_due(conn, now, commit=True)  # 0a: 独立 tx

    row = conn.execute(
        "SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
    if row is None:
        raise ValueError(f"approval {approval_id} not found")
    payload = json.loads(row["payload_json"])
    name = payload["name"]

    with _plugin_lock(plugins_root, name):  # 0b
        row = conn.execute(
            "SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
        if row["status"] != "pending":
            return
        payload = json.loads(row["payload_json"])

        if _is_superseded(conn, row, payload):  # 0c
            conn.execute("BEGIN IMMEDIATE")
            try:
                approvals_store.apply_decision(
                    conn, approval_id, "invalidated", decided_by="system",
                    now=now, commit=False)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            # I1 是正: 終端決定 (invalidated) の tx 直後に staging 候補を
            # 削除する (§5.1 手順 3)。
            _drop_staging_candidate(plugins_root, payload)
            return

        # 0d: 同名の未完ジャーナルが「この approval 自身の再試行」であれば
        # その op_id から再開する。switched まで進んでいれば decide のみ
        # (§5.1 codex 12 周目の early-exit — 未定義の retry_approval を
        # 経由せずここで直接処理する、11c の NameError landmine を踏まない)。
        existing_journal = journal_store.get_open_by_name(conn, name)
        op_id = None
        if existing_journal is not None and existing_journal["approval_id"] == approval_id:
            op_id = existing_journal["op_id"]
            if existing_journal["phase"] == "switched":
                # B3 是正: decide (=`_finalize_decision`) の前に、必ず
                # 参照物 (new_target の版) の存在と hash を再検証する
                # (§5.1-1 (a))。再検証失敗時は `_reverify_switched_journal`
                # が journal を reverted で閉じ activity ERROR を書く —
                # ここでは pending のまま return するだけでよい。
                if not _reverify_switched_journal(
                        conn, existing_journal, plugins_root=plugins_root,
                        payload=payload, now=now, activity=activity):
                    return
                _finalize_decision(conn, approval_id, op_id=op_id,
                                   decided_by=decided_by, now=now)
                return
        elif existing_journal is not None:
            # 別 approval_id の未完ジャーナルが同名に存在する場合、この
            # approve は進められない — `plugin_switch_journal` の部分
            # UNIQUE index (name ごとに非終端 1 件、11c) に反して
            # `begin_switch_journal` を呼ぶと生の `sqlite3.IntegrityError`
            # が漏れてしまう。検収 m6 是正: `UnresolvedJournalError` は
            # 同ファイル (`retire_plugin` が使う型) に既に実在するため、
            # ここでも同じ型に統一する (旧稿の「本 task はそのクラスを
            # 新設しない」という逸脱メモは 11e で新設済みになった後も
            # 更新されずに残っていた — CLI `_plugin_retire` が
            # `UnresolvedJournalError` を catch するのと型を揃える)。
            # ここでは呼び出し元が `reconcile_switch_journals`/
            # `approval retry` で先に収束させることを期待し、fail closed
            # にする。
            raise UnresolvedJournalError(
                f"plugin {name!r}: an unresolved switch journal "
                f"(op_id={existing_journal['op_id']}, "
                f"approval_id={existing_journal['approval_id']}) blocks "
                f"approval {approval_id} — resolve it first (reconcile or "
                "approval retry)")

        def _close_own_unfinished_journal_if_any() -> None:
            # 確定-1 (Critical): この approval 自身の未完ジャーナル
            # (preparing/versioned/recorded — switched はここに来ない、上の
            # 0d 分岐で先に処理・return 済み) が残っていれば、候補が壊れて
            # pending 留置する前に `_revert_one` で閉じる。閉じないと name が
            # 永久に `UnresolvedJournalError` で封鎖される
            # (verified-local-round1.md 確定-1)。`_revert_one` は非 switched
            # 行に対して FS 副作用を一切持たない (`switch.py:156-158` と
            # 同じ論拠 — 安全)。
            if op_id is None:
                return
            journal_row = journal_store.get(conn, op_id)
            if journal_row is not None and journal_row["phase"] != "switched":
                _revert_one(conn, journal_row, plugins_root=plugins_root,
                           now=now, activity=activity)
                conn.commit()

        candidate_origin = payload["candidate_origin"]
        candidate_path = payload["candidate_path"]
        try:
            candidate_dir = resolve_candidate_dir(
                plugins_root, candidate_origin=candidate_origin,
                candidate_path=candidate_path, name=name)
        except CandidateMissingError:
            _close_own_unfinished_journal_if_any()
            return  # pending のまま (§8.1-29)

        try:
            check_candidate_snapshot(candidate_dir)
            content_hash, artifact_hash = hashes_of(candidate_dir)
        except ValueError:
            # `CandidateSnapshotError` は `ValueError` のサブクラス
            # (gate_pytest.py) — スナップショット不正はここで fail closed
            # に pending 留置する。`OSError` はこの try からは意図的に
            # 除外する: `resolve_candidate_dir` が既に存在確認を済ませて
            # いるため、ここでの `OSError` は「確認直後に候補が消えた」
            # ような予期しない事象であり、`CandidateMissingError` の
            # fail-closed 経路と取り違えない (M7 是正 — `OSError` を握り
            # つぶすと `candidate_missing` 検出そのものを削る変異が
            # `test_approve_candidate_missing_stays_pending` で red に
            # ならず survive してしまう)。
            _close_own_unfinished_journal_if_any()
            return  # 候補が壊れている/検査失敗 → pending のまま
        if (content_hash != payload["content_hash"]
                or artifact_hash != payload["artifact_hash"]):
            # 確定-1: ⓐ 不一致でも同様に自分の未完ジャーナルを閉じる。
            _close_own_unfinished_journal_if_any()
            return  # ⓐ 不一致 → pending のまま

        live = plugins_root / name
        if live.is_symlink():
            old_kind, old_target = "symlink", os.readlink(live)
        elif live.exists():
            old_kind, old_target = "plain", None
        else:
            old_kind, old_target = "absent", None

        new_target = f".versions/{name}/{artifact_hash}"

        if old_kind == "plain":
            # 2a: plain 分岐 — 版+git のみ進め、切替と decide はスキップ
            version_dir = version_store.create_version_dir(
                plugins_root, name, artifact_hash,
                plugin_py=(candidate_dir / "plugin.py").read_bytes(),
                config_yaml=(candidate_dir / "config.yaml").read_bytes(),
                test_plugin=(candidate_dir / "test_plugin.py").read_bytes(),
                op_identity=f"approval-{approval_id}")
            history_git.record_version(
                plugins_root / ".history.git", name=name, artifact_hash=artifact_hash,
                content_hash=content_hash, approval_id=approval_id,
                version_dir=version_dir)
            approvals_store.set_reason(
                conn, approval_id, "legacy_plain_present", commit=True)
            return

        switch_required = not (old_kind == "symlink" and old_target == new_target)

        if op_id is None:
            op_id = begin_switch_journal(
                conn, kind="approve", approval_id=approval_id, name=name,
                old_kind=old_kind, old_target=old_target, new_target=new_target,
                switch_required=switch_required, actor=decided_by, now=now,
                commit=True)

        _advance_to_decided(
            conn, approval_id, op_id=op_id, name=name, plugins_root=plugins_root,
            candidate_dir=candidate_dir, content_hash=content_hash,
            artifact_hash=artifact_hash, switch_required=switch_required,
            decided_by=decided_by, now=now)

        if candidate_origin == "staging":
            shutil.rmtree(candidate_dir, ignore_errors=True)


class UnresolvedJournalError(Exception):
    """retire は未完 switch ジャーナルがあれば拒否する (§5.1)。"""


def materialize_plugin(root: Path, name: str) -> Path:
    live = root / name
    dest = root / "_human" / name
    if dest.exists():
        raise FileExistsError(f"plugins/_human/{name} already exists — refusing "
                              "to overwrite (human-owned, auto-delete しない)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if live.is_symlink():
        src = (live.parent / live.readlink()).resolve()
    else:
        src = live
    shutil.copytree(src, dest)
    dest.chmod(0o700)
    for f in dest.iterdir():
        f.chmod(0o600)
    return dest


# precheck 2026-08-22 wave2: T11-B2
def retire_plugin(conn: sqlite3.Connection, root: Path, name: str, *,
                  now: datetime, activity: "ActivityLog | None" = None) -> None:
    """統合裁定 R-i5 で骨格 Interfaces 節が `conn` 必須へ更新済み
    (未完ジャーナルの確認に DB 接続が要るため)。

    検収 B2 是正: 設計書 §5.1 手順 0 / §5.1-1 は `plugin retire` に
    activity `plugin_retired` を要求する。旧稿はコメントのみで未実装
    だった (呼び出し元に activity インスタンスを渡す形へ拡張)。"""
    lock_path = root / ".locks" / f"{name}.lock"
    lock_path.parent.mkdir(exist_ok=True)
    with open(lock_path, "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            # B-2: 現物名は `get_open_by_name` (単一行 dict | None を返す —
            # 非実在の `list_non_terminal_for_name` からの置換)
            unresolved = journal_store.get_open_by_name(conn, name)
            if unresolved is not None:
                raise UnresolvedJournalError(
                    f"plugin {name!r} has an unresolved switch journal "
                    f"(op_id={unresolved['op_id']}) — resolve it first "
                    "(reconcile or approval retry)")
            live = root / name
            if not live.is_dir() or live.is_symlink():
                raise ValueError(f"plugins/{name} is not a plain directory "
                                 "(retire only applies to legacy plain live)")
            ts = now.strftime("%Y%m%dT%H%M%SZ")
            dest = root / "_retired" / f"{name}-{ts}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.rename(live, dest)
            if activity is not None:
                activity.write(Category.APPROVAL, "plugin_retired",
                               f"name={name} retired_path={dest}")
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)


# precheck 2026-08-22 wave2: T11-B1
def retry_approval(conn: sqlite3.Connection, approval_id: int, *,
                   decided_by: str, now: datetime, plugins_root: Path,
                   settings: "Settings",
                   activity: "ActivityLog | None" = None) -> None:
    """§5.3 契機③: 手順を頭から流す (lock → plain 検出 → ⓓ → ⓐ → 版(冪等) →
    git(no-op) → 切替(no-op なら済み) → apply_decision)。approve_candidate と
    同じ実装を呼ぶだけ (retry は「approve をもう一度呼ぶ」と同義 — §5.3 本文)。
    B-1 是正で plugins_root/settings を追加した (approve_candidate へそのまま
    透過する)。B3 是正: `activity` も同様に透過する (0d の再検証失敗時の
    ERROR 記録用)。"""
    approve_candidate(conn, approval_id, decided_by=decided_by, now=now,
                      plugins_root=plugins_root, settings=settings,
                      activity=activity)


def reject_candidate(conn: sqlite3.Connection, approval_id: int, *,
                     decided_by: str, reason: str, now: datetime,
                     plugins_root: Path) -> None:
    """§8.1-32: 全 terminal decision (approve/reject/expire/reconcile) が
    同じ plugin flock を通る。本 task (11g) が新規命名 (骨格 Interfaces 節
    に無い — submit/approve/bless の 3 関数しか列挙されていないが、reject
    経路も `apply_decision` を通り plugin flock を取る必要があるため新設)。
    name は approval payload から取得する (submit/bless が payload に name
    を含める契約 — 11d 参照)。"""
    row = conn.execute(
        "SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
    if row is None:
        # E1 裁定 (2026-08-25): 「ID 不存在」は「CAS 失敗 (決定済み)」と
        # 別例外にする — `ApprovalNotFoundError` は `AlreadyDecidedError`
        # のサブクラスなので既存の broad catch との後方互換を保つ。
        raise approvals_store.ApprovalNotFoundError(
            f"approval {approval_id} not found")
    payload = json.loads(row["payload_json"])
    name = payload.get("name")
    if name is None:
        # payload に name が無い (plugin kind の契約違反) — flock を取る
        # 対象が特定できないので apply_decision のみ直接通す。
        approvals_store.apply_decision(
            conn, approval_id, "rejected", decided_by=decided_by, now=now,
            reason=reason, commit=True)
        return

    with _plugin_lock(plugins_root, name):
        # 未完ジャーナル収束: この approval 自身の未完ジャーナルが残って
        # いれば (preparing 等で中断した再試行分) reject 前に巻き戻す
        # (§5.1-1 の収束規則と同じ精神 — reject は「この承認を成立させ
        # ない」決定なので、途中まで進んだ switch 効果を戻してから decide
        # する)。
        existing_journal = journal_store.get_open_by_name(conn, name)
        if existing_journal is not None and existing_journal["approval_id"] == approval_id:
            _revert_one(conn, existing_journal, plugins_root=plugins_root, now=now)
            conn.commit()

        approvals_store.apply_decision(
            conn, approval_id, "rejected", decided_by=decided_by, now=now,
            reason=reason, commit=True)
        # I1 是正: 終端決定 (rejected) の tx 直後に staging 候補を削除する
        # (§5.1 手順 3)。
        _drop_staging_candidate(plugins_root, payload)


def process_expired_approvals(conn: sqlite3.Connection, *, plugins_root: Path,
                              now: datetime) -> None:
    """裁定 1 (統合裁定 R-i8) の意味論を実装する (プラン10 Task11g、11f の
    service.py 起動時 reconcile 配線が本関数の存在に依存するため実装を前倒し
    — 最終報告の「逸脱」に明記):
    - 非 plugin kind: `approvals_store.expire_due` の直接 expired 化変種を
      そのまま使う (FS 副作用を持たない決定 — flock 不要)。
    - plugin kind: `approvals_store.list_due_for_expiry` (列挙のみ、状態を
      変えない) で期限到来 pending 行を洗い出し、行ごとに name の plugin
      flock 下で「未完 switch ジャーナルが無い」ことを確認してから
      `apply_decision(status="expired")` を呼ぶ。未完ジャーナルがある行は
      スキップし、次回の呼び出し (または起動時 reconcile 後の再試行) に
      委ねる。
    """
    # ① 非 plugin kind は従来どおり一括で直接 expired 化 (flock 不要)
    approvals_store.expire_due(conn, now=now, exclude_kinds=("plugin",))

    # ② plugin kind は列挙のみ (状態を変えない) — 行ごとに flock 下で処理
    due_rows = approvals_store.list_due_for_expiry(conn, now=now, kind="plugin")
    for row in due_rows:
        payload = json.loads(row["payload_json"])
        name = payload.get("name")
        if not name:
            continue
        lock_path = plugins_root / ".locks" / f"{name}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                if journal_store.get_open_by_name(conn, name) is not None:  # B-2: 現物名
                    # 未完ジャーナルあり → 今回はスキップ、次回再試行
                    continue
                # 他プロセスが flock 待ちの間に既に決定済みかもしれない
                # (二重呼び出し・並行 approve など) — 再確認してから決定する
                current = conn.execute(
                    "SELECT status, expires_at FROM approval_requests WHERE id=?",
                    (row["id"],)).fetchone()
                if current is None or current["status"] != "pending":
                    continue
                if current["expires_at"] is None or current["expires_at"] >= now.isoformat():
                    continue  # 期限が (並行更新等で) もはや到来していない
                approvals_store.apply_decision(
                    conn, row["id"], status="expired", decided_by="system",
                    now=now, reason="expired", commit=True)
                # I1 是正: 終端決定 (expired) の tx 直後に staging 候補を
                # 削除する (§5.1 手順 3)。
                _drop_staging_candidate(plugins_root, payload)
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)


def bless_candidate(
    conn: sqlite3.Connection, *, name: str, human_dir: Path,
    settings, now: datetime, decided_by: str,
) -> int:
    """P3 (bless --from _human) の入口。live の形で二分:
    absent/symlink なら 1 tx で pending+証跡+preparing ジャーナル → 版/git/
    切替 → apply_decision。プレーンなら pending+証跡のみ →
    legacy_plain_present。"""
    plugins_root = human_dir.parent.parent  # human_dir = plugins/_human/<name>

    approvals_store.expire_due(conn, now, commit=True)  # 0a

    with _plugin_lock(plugins_root, name):  # 0b
        # 確定-2: `approve_candidate` (switch.py:728-733) と同じガードを
        # `bless_candidate` にも置く。無ければ `begin_switch_journal` が
        # 部分 UNIQUE index に当たり、生の `sqlite3.IntegrityError` が
        # catch サイト (CLI) を素通りしてしまう。
        existing_journal = journal_store.get_open_by_name(conn, name)
        if existing_journal is not None:
            raise UnresolvedJournalError(
                f"plugin {name!r}: an unresolved switch journal "
                f"(op_id={existing_journal['op_id']}, "
                f"approval_id={existing_journal['approval_id']}) blocks "
                f"bless — resolve it first (reconcile or approval retry)")

        meta, content_hash, artifact_hash, metrics, evaluable = _run_full_gate(
            conn, human_dir, name=name, settings=settings, now=now)

        live = plugins_root / name
        if live.is_symlink():
            old_kind, old_target = "symlink", os.readlink(live)
        elif live.exists():
            old_kind, old_target = "plain", None
        else:
            old_kind, old_target = "absent", None

        payload = {
            "name": name, "kind": meta.kind,
            "candidate_origin": "human",
            "candidate_path": f"plugins/_human/{name}",
            "content_hash": content_hash, "artifact_hash": artifact_hash,
            "metrics": metrics, "evaluable": evaluable,
            "mission_id": None, "backlog_id": None,
        }

        if old_kind == "plain":
            # 3-B: ジャーナル無し、pending+証跡のみの 1 tx
            conn.execute("BEGIN IMMEDIATE")
            try:
                approval_id = approvals_store.create(
                    conn, kind="plugin", payload=payload, now=now, commit=False)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            version_dir = version_store.create_version_dir(
                plugins_root, name, artifact_hash,
                plugin_py=(human_dir / "plugin.py").read_bytes(),
                config_yaml=(human_dir / "config.yaml").read_bytes(),
                test_plugin=(human_dir / "test_plugin.py").read_bytes(),
                op_identity=f"approval-{approval_id}")
            history_git.record_version(
                plugins_root / ".history.git", name=name, artifact_hash=artifact_hash,
                content_hash=content_hash, approval_id=approval_id,
                version_dir=version_dir)
            approvals_store.set_reason(
                conn, approval_id, "legacy_plain_present", commit=True)
            return approval_id

        # 3-A: absent/symlink — pending+証跡+preparing ジャーナルを 1 tx で
        new_target = f".versions/{name}/{artifact_hash}"
        switch_required = not (old_kind == "symlink" and old_target == new_target)

        conn.execute("BEGIN IMMEDIATE")
        try:
            approval_id = approvals_store.create(
                conn, kind="plugin", payload=payload, now=now, commit=False)
            op_id = begin_switch_journal(
                conn, kind="bless", approval_id=approval_id, name=name,
                old_kind=old_kind, old_target=old_target, new_target=new_target,
                switch_required=switch_required, actor=decided_by, now=now,
                commit=False)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

        _advance_to_decided(
            conn, approval_id, op_id=op_id, name=name, plugins_root=plugins_root,
            candidate_dir=human_dir, content_hash=content_hash,
            artifact_hash=artifact_hash, switch_required=switch_required,
            decided_by=decided_by, now=now)
        return approval_id
