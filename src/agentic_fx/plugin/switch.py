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
from datetime import datetime
from pathlib import Path
from typing import Literal

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
    try:
        if not candidate_dir.is_dir():
            raise CandidateMissingError(
                f"candidate not found: {candidate_path} (origin={candidate_origin})")
    except OSError as exc:
        raise CandidateMissingError(str(exc)) from exc
    return candidate_dir


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
    切替) → decided まで進める (P2 手順 5〜9 / P3 手順 6A〜11A の共有部)。"""
    version_dir = version_store.create_version_dir(
        plugins_root, name, artifact_hash,
        plugin_py=(candidate_dir / "plugin.py").read_bytes(),
        config_yaml=(candidate_dir / "config.yaml").read_bytes(),
        test_plugin=(candidate_dir / "test_plugin.py").read_bytes(),
        op_identity=str(op_id))
    advance_switch_journal(conn, op_id, phase="versioned", now=now, commit=True)

    history_git.record_version(
        plugins_root / ".history.git", name=name, artifact_hash=artifact_hash,
        content_hash=content_hash, approval_id=approval_id, version_dir=version_dir)
    advance_switch_journal(conn, op_id, phase="recorded", now=now, commit=True)

    if switch_required:
        advance_switch_journal(conn, op_id, phase="switched", now=now, commit=True)
        new_target = f".versions/{name}/{artifact_hash}"
        switch_live(plugins_root, name, new_target=new_target, op_id=op_id)
        # 手順 8a: 切替後の再照合 (fail closed — 自動巻き戻しは
        # reconcile_switch_journals の switched 収束規則に委ねる。ここでは
        # 例外を送出して手順 9 (decide) へ進ませない)
        resolved_after = (plugins_root / new_target).resolve()
        after_hash = loader.content_hash(resolved_after)
        if after_hash != content_hash:
            raise RuntimeError(
                f"plugin {name!r}: live content_hash mismatch after switch "
                f"(expected {content_hash}, got {after_hash})")

    _finalize_decision(conn, approval_id, op_id=op_id, decided_by=decided_by, now=now)


def approve_candidate(
    conn: sqlite3.Connection, approval_id: int, *,
    decided_by: str, now: datetime, plugins_root: Path, settings,
) -> None:
    """P2 (approve) の入口。plugins_root は FS 操作 (版・git・切替) の起点、
    settings は将来のゲート再検証・kind 別分岐のために渡す (現行 P2 手順は
    gate を再実行しないが、シグネチャで揃えておくことで retry_approval/
    reconcile 経由の呼び出しと submit_candidate/bless_candidate の引数構成
    を統一する)。"""
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
                _finalize_decision(conn, approval_id, op_id=op_id,
                                   decided_by=decided_by, now=now)
                return

        candidate_origin = payload["candidate_origin"]
        candidate_path = payload["candidate_path"]
        try:
            candidate_dir = resolve_candidate_dir(
                plugins_root, candidate_origin=candidate_origin,
                candidate_path=candidate_path, name=name)
        except CandidateMissingError:
            return  # pending のまま (§8.1-29)

        try:
            check_candidate_snapshot(candidate_dir)
            content_hash, artifact_hash = hashes_of(candidate_dir)
        except (OSError, ValueError):
            return  # 候補が壊れている/検査失敗 → pending のまま
        if (content_hash != payload["content_hash"]
                or artifact_hash != payload["artifact_hash"]):
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
