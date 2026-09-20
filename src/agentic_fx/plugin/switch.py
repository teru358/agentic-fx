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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

import yaml

from agentic_fx._safe_error import safe_error_text  # 検収 m10: per-row fault isolation の ERROR 記録に使う
from agentic_fx.activity import ActivityLog, Category  # B-2: journal_store 層に activity を書かせない
from agentic_fx.plugin import (
    approval, history_git, loader, strategy_gate, version_store,
)
from agentic_fx.plugin.gate_pytest import (  # M-8: モジュールレベル import (11d/11e/11g の monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", ...) seam が効くために必須)
    check_candidate_snapshot, hashes_of, run_gate_pytest,
)
from agentic_fx.plugin.resolve import (  # [indicator-consumption-wiring] T4b Step 4-4
    IndicatorResolutionError, resolve_indicator_deps,
)
from agentic_fx.plugin.sandbox import SandboxError, check_source
from agentic_fx.tools import plugin_loader as tools_plugin_loader
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import backtest_runs as backtest_runs_store  # [profitability-floor] T1 Step 1-6: 人間 corridor の gate 行保存
from agentic_fx.store import candidate_archives as candidate_archives_store  # T4 §1 L4 の archive GC が使う
from agentic_fx.store import plugin_switch_journal as journal_store  # Task 8 produces

_PHASE_ORDER = ["preparing", "versioned", "recorded", "switched", "decided", "reverted"]

_TERMINAL_PHASES = ("decided", "reverted")

# [switch-ops-hardening] T5: lock の内側で確定した結果 (設計書 §3.5)。
APPROVAL_OUTCOMES = frozenset({
    "deployed", "deployed_after_rollback", "already_decided", "foreign_waiting",
    "still_pending", "legacy_plain_present", "invalidated",
})


@dataclass(frozen=True)
class ApprovalOutcome:
    """`approve_candidate` / `retry_approval` が **plugin flock の内側で確定**
    した結果 (設計書 §3.5)。呼び出し元はこの値を文言に写すだけで、
    lock の外で DB / FS を読み直さない (読み直すと、別プロセスの後続の
    正規配備を「契約違反」と誤報する — r2 Important 4)。"""
    outcome: str
    name: str
    status: str
    op_id: int | None = None
    rolled_back_op_id: int | None = None
    target: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in APPROVAL_OUTCOMES:
            raise ValueError(f"unknown approval outcome: {self.outcome!r}")


def classify_live(plugins_root: Path, row) -> str:
    """[switch-ops-hardening] T1 (設計書 §3.1): `switched` で止まった journal 行
    に対し、**live symlink が実際にどこを指しているか**を 3 値で返す。

    比較は `readlink` の生文字列で行う (`resolve()` は使わない) — journal の
    `new_target`/`old_target` は plugins_root 相対の文字列として書かれ、
    `switch_live` がそれをそのまま symlink の中身にするため。`resolve()` に
    すると版ディレクトリが消えた dangling symlink で比較が壊れる。

    **読み取り専用** (IV-5)。`phase='switched'` かつ `switch_required=1` の
    行にしか意味が無いので、それ以外で呼ばれたら fail closed。"""
    if row["phase"] != "switched" or not row["switch_required"]:
        raise ValueError(
            f"classify_live: op_id={row['op_id']} is phase={row['phase']!r} "
            f"switch_required={row['switch_required']} — 分類器は "
            "phase='switched' かつ switch_required=1 の行にのみ適用する")
    live = plugins_root / row["name"]
    live_target = live.readlink().as_posix() if live.is_symlink() else None
    if live_target == row["new_target"]:
        return "switched"
    if live_target == row["old_target"] or (
            row["old_kind"] == "absent" and live_target is None):
        return "not_switched"
    return "foreign"


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


def _revert_under_lock(conn: sqlite3.Connection, row: dict, *, plugins_root: Path,
                       now: datetime, activity: "ActivityLog | None",
                       expect_class: str | None) -> bool:
    """[switch-ops-hardening] T4 (設計書 §3.3.2): 巻き戻しは必ず
    (1) name lock を取り (2) `op_id` で journal 行を取り直し (3) (分類を使う枝なら)
    live を読み直して分類をやり直し、(4) すべて一致したときだけ `_revert_one` する。

    lock を取るだけでは足りない — lock 待ちの間に別プロセスの `approval retry` が
    同じ行を畳んで配備を完了していると、手元の古い行で live を巻き戻して
    「approved だが未配備」を作ってしまう (r1 Critical 1、probe で実測)。

    `expect_class=None` は `force_revert_op_id` 経路 — 「phase に依らず巻き戻す
    割込」という既存の意味論を保つため、**行の再取得だけ**を行い分類は見ない。"""
    with _plugin_lock(plugins_root, row["name"]):
        fresh = journal_store.get(conn, row["op_id"])
        phase_now = fresh["phase"] if fresh is not None else None
        if fresh is None or phase_now in _TERMINAL_PHASES or phase_now != row["phase"]:
            if activity is not None:
                activity.write(
                    Category.APPROVAL, "switch_reconcile_skipped_stale_row",
                    f"name={row['name']} op_id={row['op_id']} "
                    f"phase_before={row['phase']} phase_now={phase_now}")
            return False
        if expect_class is not None:
            class_now = classify_live(plugins_root, fresh)
            if class_now != expect_class:
                if activity is not None:
                    activity.write(
                        Category.APPROVAL, "switch_reconcile_skipped_stale_row",
                        f"name={row['name']} op_id={row['op_id']} "
                        f"phase_before={row['phase']} phase_now={phase_now} "
                        f"class_before={expect_class} class_now={class_now}")
                return False
        _revert_one(conn, fresh, plugins_root=plugins_root, now=now, activity=activity)
        conn.commit()
        return True


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
                # [switch-ops-hardening] T4: lock + 行の再取得を通す
                # (分類の一致は求めない — 割込の意味論を保つ、§3.3.3)。
                _revert_under_lock(conn, row, plugins_root=plugins_root, now=now,
                                  activity=activity, expect_class=None)
                continue
            if row["phase"] != "switched":
                # preparing/versioned/recorded: 「同じ操作の再試行」は 11d の
                # P2/P3 入口 (approve_candidate/bless_candidate) が頭から
                # 冪等に再実行する。ここ (起動時 reconcile) では FS 効果が
                # まだ無いので触らずスキップ — 実際の完了は次回の approve/
                # approval retry が担う (§5.1-1 (a))。
                continue
            # [switch-ops-hardening] T1: 分類は 1 つの関数に寄せる (§3.1)。
            live_class = classify_live(plugins_root, row)
            if live_class == "switched":
                # [indicator-consumption-wiring] §2.4 (codex r5 I3):
                # switched (symlink 切替済・DB decided 前) のまま停止し、
                # 再起動までに indicator が更新されて pin が破れた場合、
                # そのまま decided にすると **live に未解決の strategy が
                # 残る**。当該 strategy + 依存名の lock 下で `require`
                # 解決を再試行し、失敗なら旧 target へ原子的に戻して
                # journal を `reverted`、approval は `pending` のまま残す
                # (人間が再ロック → 再承認)。新 version dir は回収しない
                # (現行 reconcile も version store を回収しない — GC は
                # 既存の archive/versions 規律に任せる、codex r8 I2)。
                unresolved = _unresolved_after_switch(
                    conn, row, plugins_root=plugins_root, settings=settings)
                if unresolved is not None:
                    alias, reason = unresolved
                    if not _revert_under_lock(
                            conn, row, plugins_root=plugins_root, now=now,
                            activity=activity, expect_class="switched"):
                        continue  # stale — 次回の reconcile が再評価する
                    if activity is not None:
                        # Global Constraints の固定文言 (逐語):
                        # `switch_reverted reason=indicator_unresolved
                        # alias=<alias> cause=<reason>`。
                        activity.write(
                            Category.APPROVAL, "switch_reverted",
                            f"reason=indicator_unresolved alias={alias} "
                            f"cause={reason}")
                    continue
                # switched は完遂しているが、decided への遷移は apply_decision と
                # 同一 tx で行う契約 (§4.3) — reconcile 自身は phase を書き換え
                # ない。11d/11g の再試行入口 (retry_approval → approve_candidate)
                # にそのまま委譲する (B-1 是正: plugins_root/settings を渡す)。
                # kind='bless' の行も retry_approval が approve_candidate へ
                # 委譲するので同じ入口で良い (11e 新規命名の retry_approval)。
                retry_approval(conn, row["approval_id"], decided_by="system_reconcile",
                                now=now, plugins_root=plugins_root, settings=settings,
                                activity=activity)
            elif live_class == "not_switched":
                _revert_under_lock(conn, row, plugins_root=plugins_root, now=now,
                                  activity=activity, expect_class="not_switched")
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


def _unresolved_after_switch(conn: sqlite3.Connection, row: dict, *,
                             plugins_root: Path, settings
                             ) -> "tuple[str, str] | None":
    """switched 行の新 target (= live が既に指している版) を `require` で
    解決し直す。解決できれば `None`、できなければ `(alias, reason)`。
    strategy 以外・meta を読めない場合は `None` (従来の収束規則に委ねる)。"""
    version_dir = (plugins_root / row["new_target"]).resolve()
    if not version_dir.is_dir():
        # meta を読めない (版ディレクトリが未実在 — 単体テストの sparse
        # fixture 等) — 従来の収束規則 (retry_approval 経由) に委ねる。
        return None
    meta = loader._discover_one(version_dir, row["name"])
    if meta is None or meta.kind != "strategy":
        return None
    # **TOCTOU 窓の明記 (opus r1 M13)**: 自己デッドロックは起きない —
    # 定義 1 + 入口 4 (submit / approve / bless ほか) で入れ子は無く、
    # `retry_approval` → `approve_candidate` も lock を取るのは 1 回。
    # そのため本 helper は with を**抜けてから** `retry_approval` を呼ぶ
    # 設計で正しい。ただし **lock 解放から `retry_approval` が lock を
    # 取り直すまでの窓**で、別プロセスが依存 indicator を承認して pin を
    # 破り得る。その場合は `approve_candidate` 側の決定時 `require` 解決
    # (Step 4-4) が `pin_mismatch` で弾き、approval は pending のまま残る
    # (= 多層防御で fail closed)。**この窓を塞ぐために本 helper と
    # `retry_approval` を同一 lock 内へまとめてはならない** (approve 経路の
    # lock 取得と二重になる)。
    with _plugin_locks(plugins_root, _dependency_names(version_dir, row["name"])):
        inventory = tools_plugin_loader.approved_plugins(
            conn, plugins_root, settings=settings)
        try:
            resolve_indicator_deps(meta, inventory.inventory, settings=settings,
                                   pin_mode="require")
        except IndicatorResolutionError as exc:
            return (exc.alias or "-", exc.reason)
    return None


# T4 §1 L4: readonly (0500/0400) な archive final ディレクトリを削除前に
# 書込可能へ戻す (改善ループ側の `_remove_archive_tmp`/`_delete_staging` と
# 同じ os.walk 流儀、symlink は chmod しない)。
def _chmod_tree_writable(path: Path) -> None:
    version_store.chmod_tree_writable(path)


def _archive_dir_size(path: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for filename in filenames:
            fp = Path(dirpath) / filename
            if fp.is_symlink():
                continue
            try:
                total += fp.stat().st_size
            except OSError:
                pass
    return total


# precheck 2026-08-22 wave2: T11-B2
def sweep_orphans(conn: sqlite3.Connection, *, plugins_root: Path, now: datetime,
                  activity: "ActivityLog | None" = None,
                  archive_max_bytes: int | None = None,
                  archive_max_missions: int | None = None) -> None:
    """§5.3 起動時 reconcile ①〜⑦ (journal-first の後に呼ぶこと)。B-2 是正:
    activity 記録は journal_store 層 (非実在の `record_activity_error`) では
    なく、呼び出し元であるこの関数が直接 `activity.write` する (層違反の
    解消)。M-10 是正: tmp スキップ条件は 1 桁の op_identity にしか一致しない
    バグがあったため `".tmp-" in name` の部分一致に変える。

    T4 §1 L4 (このドキュメントの ①〜⑦ とは別建ての番号): `plugins/_archive/`
    に対して archive GC ⑥ (startup 限定 tmp 回収)・⑦ (バイト/mission 数上限
    GC)・⑦′ (path 不存在の再収束) を行う。**⑥ は runner 起動前の startup
    sweep だけが呼ぶ前提** — この関数の呼び出し元は `service.py` の起動時
    reconcile 一箇所のみで、前プロセスの handler は存在しない。runtime GC
    (mission 実行中) に転用するなら age cutoff が要る (在走中 handler の
    `.tmp-*` を消してしまう)。"""
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
            # dir symlink は追わない — is_dir()/os.walk が外部 dir を
            # chmod/rmtree してしまう (2 周目 codex I1)。link 自体だけ消す。
            if mission_dir.is_symlink():
                try:
                    mission_dir.unlink()
                except OSError:
                    pass
                continue
            for candidate in mission_dir.iterdir():
                rel = f"plugins/_staging/{mission_dir.name}/{candidate.name}"
                if rel not in referenced and candidate.is_symlink():
                    try:
                        candidate.unlink()
                    except OSError:
                        pass
                    continue
                if rel not in referenced:
                    # Source snapshots are immutable (0500/0400), just like
                    # version-store entries handled in branch ③ below.
                    # Restore owner write permission before recursive removal.
                    # _snapshot_src は入れ子 (_examples/<name>/… の 3 階層、
                    # dir はすべて 0500) — 1 階層だけの chmod では内側の
                    # 0500 dir で rmtree(ignore_errors=True) が黙って失敗
                    # するため、os.walk で全階層に降りる。
                    try:
                        if candidate.is_dir():
                            for dirpath, _dirs, files in os.walk(candidate):
                                p = Path(dirpath)
                                if not p.is_symlink():
                                    p.chmod(0o700)
                                for fn in files:
                                    p = Path(dirpath) / fn
                                    if p.is_symlink():
                                        continue
                                    p.chmod(0o600)
                            shutil.rmtree(candidate, ignore_errors=True)
                        else:
                            candidate.chmod(0o600)
                            candidate.unlink()
                    except OSError as exc:
                        if activity is not None:
                            activity.write(
                                Category.APPROVAL, "sweep_orphan_staging_failed",
                                f"path={candidate} error={safe_error_text(exc)}")

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

    # archive GC ⑥ (T4 §1 L4、docstring 参照): startup 限定 tmp 回収。
    # publish されなかった `.tmp-*` を無条件に chmod → rmtree する。
    archive_root = plugins_root / "_archive"
    if archive_root.is_dir():
        for mission_dir in archive_root.iterdir():
            if mission_dir.is_symlink() or not mission_dir.is_dir():
                continue
            if not mission_dir.name.isdigit():
                continue
            for tmp_dir in mission_dir.glob(".tmp-*"):
                if tmp_dir.is_symlink():
                    try:
                        tmp_dir.unlink()
                    except OSError:
                        pass
                    continue
                try:
                    _chmod_tree_writable(tmp_dir)
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except OSError as exc:
                    if activity is not None:
                        activity.write(
                            Category.APPROVAL, "sweep_archive_tmp_failed",
                            f"path={tmp_dir} error={safe_error_text(exc)}")
                    continue
                # /code-review 2 周目 CR6 (2026-09-11): 黙って残った tmp は
                # 報告する (⑦ の rmtree_incomplete と同型。残骸のバイトが
                # ⑦ の上限計算に混ざり無音で余計な mission を消す)。
                if tmp_dir.exists() and activity is not None:
                    activity.write(
                        Category.APPROVAL, "sweep_archive_tmp_failed",
                        f"path={tmp_dir} error=rmtree_incomplete")

    # T4 変更点6: 孤立 final (`candidate_archives` に (mission_id,
    # artifact_hash) の行が無い final ディレクトリ) を activity に出す。
    # 削除しない (2 回目補償が拾えるようにする)。
    if archive_root.is_dir():
        for mission_dir in archive_root.iterdir():
            if mission_dir.is_symlink() or not mission_dir.is_dir():
                continue
            if not mission_dir.name.isdigit():
                continue
            mission_id = int(mission_dir.name)
            known_hashes = {
                r["artifact_hash"] for r in conn.execute(
                    "SELECT artifact_hash FROM candidate_archives "
                    "WHERE mission_id=? AND archive_path IS NOT NULL",
                    (mission_id,))}
            for entry in mission_dir.iterdir():
                if entry.is_symlink() or not entry.is_dir():
                    continue
                if entry.name.startswith(".tmp-"):
                    continue
                if entry.name not in known_hashes and activity is not None:
                    activity.write(
                        Category.APPROVAL, "archive_orphan_final",
                        f"path={entry}")

    # archive GC ⑦ (T4 §1 L4): バイト/mission 数上限。数字名の mission dir
    # だけを対象に mission_id 昇順 (古い順) に削除する。symlink は追わず
    # link 自体も触らない。INDEX.md・数字でない名前は無視 (上の iterdir
    # フィルタで既に除外済み)。削除できたことを確認してから
    # `candidate_archives.clear_path` する — branch 全体を 1 tx で commit。
    if archive_root.is_dir() and (archive_max_bytes is not None or
                                  archive_max_missions is not None):
        mission_dirs = sorted(
            (entry for entry in archive_root.iterdir()
             if not entry.is_symlink() and entry.is_dir()
             and entry.name.isdigit()),
            key=lambda p: int(p.name))
        sizes = {p: _archive_dir_size(p) for p in mission_dirs}
        total_bytes = sum(sizes.values())
        n_missions = len(mission_dirs)
        committed_any = False
        while mission_dirs and (
                (archive_max_bytes is not None and
                 total_bytes > archive_max_bytes) or
                (archive_max_missions is not None and
                 n_missions > archive_max_missions)):
            victim = mission_dirs.pop(0)
            victim_size = sizes.pop(victim)
            try:
                _chmod_tree_writable(victim)
                shutil.rmtree(victim, ignore_errors=True)
            except OSError as exc:
                if activity is not None:
                    activity.write(
                        Category.APPROVAL, "sweep_archive_failed",
                        f"path={victim} error={safe_error_text(exc)}")
                continue
            if victim.exists():
                if activity is not None:
                    activity.write(
                        Category.APPROVAL, "sweep_archive_failed",
                        f"path={victim} error=rmtree_incomplete")
                continue
            total_bytes -= victim_size
            n_missions -= 1
            mission_id = int(victim.name)
            # codex 1 周目 (T3+T4) Important 2 (2026-09-11): DB 更新の失敗は
            # 呼び出し元の connection に未完了 transaction を残さない —
            # rollback して activity、行は次回 sweep の ⑦′ で再収束する。
            try:
                for row in candidate_archives_store.list_by_mission(
                        conn, mission_id):
                    if row["archive_path"] is not None:
                        candidate_archives_store.clear_path(
                            conn, row["id"], commit=False)
                        committed_any = True
            except sqlite3.Error as exc:
                conn.rollback()
                committed_any = False
                if activity is not None:
                    activity.write(
                        Category.APPROVAL, "sweep_archive_db_failed",
                        f"mission={mission_id} error={safe_error_text(exc)}")
        if committed_any:
            try:
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                if activity is not None:
                    activity.write(
                        Category.APPROVAL, "sweep_archive_db_failed",
                        f"phase=commit error={safe_error_text(exc)}")

    # archive GC ⑦′ (T4 §1 L4): 前回 DB 更新失敗の再収束。`archive_path` が
    # 非 NULL なのに dir が無い行を `clear_path` する (上の ⑦ が消した分は
    # 既に NULL なので対象外、対象は別プロセス/前回起動でのクラッシュ跡)。
    root_dir = plugins_root.parent
    stale_missions = {
        r["mission_id"] for r in conn.execute(
            "SELECT DISTINCT mission_id FROM candidate_archives "
            "WHERE archive_path IS NOT NULL")}
    cleared_any = False
    try:
        for mission_id in stale_missions:
            for row in candidate_archives_store.list_by_mission(conn, mission_id):
                path = row["archive_path"]
                if path is None:
                    continue
                target = root_dir / path
                # /code-review 2 周目 CR8 (2026-09-11): exists() は symlink を
                # 辿る。⑥⑦ と同じく symlink / 非 dir は「無い」扱い。
                if target.is_symlink() or not target.is_dir():
                    candidate_archives_store.clear_path(
                        conn, row["id"], commit=False)
                    cleared_any = True
        if cleared_any:
            conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        if activity is not None:
            activity.write(
                Category.APPROVAL, "sweep_archive_db_failed",
                f"phase=reconcile error={safe_error_text(exc)}")

    # ⑧ (T2, test-hygiene 設計書 2026-09-12、指揮者裁定: startup sweep で
    # 回収): `_plugin_lock` (行 615 付近) は flock を取って yield するだけで
    # `plugins/.locks/<name>.lock` を unlink する経路がどこにも無く、
    # approve/reject/bless のたびに 1 ファイルずつ純増していた
    # (実測: reject 4 件で 3 → 7 件)。対応する pending な approval_request
    # が存在しない名前のロックファイルだけを、`flock(LOCK_EX|LOCK_NB)` が
    # 取れる (= 誰も保持していない) ことを確認してから削除する。
    # `sweep_orphans` は「runner 起動前の単一プロセス、他プロセスからの
    # 同時アクセスが無い」前提 (本関数冒頭の docstring) で動くため、
    # decide (approve/reject/bless) 完了直後 unlink 案 (裁定候補 1) の
    # ような flock+unlink の TOCTOU 穴が構造的に無い。
    locks_dir = plugins_root / ".locks"
    if locks_dir.is_dir():
        # 検収是正 C2 (2026-09-12): 旧実装は集合内包表記で全行を一括
        # `json.loads` していたため、1 行でも payload_json が壊れている
        # (不正 JSON・非 dict) と例外がそのまま送出され、sweep_orphans
        # 全体 (このあとに続く ⑦′ 等も含む) が止まっていた。他の branch
        # (⑦ 等) と同じ「1 行の失敗を隔離する」規律に合わせ、行ごとに
        # try/except で読み、壊れた行は skip して activity に 1 行残す。
        pending_names: set[str] = set()
        for r in conn.execute(
                "SELECT payload_json FROM approval_requests "
                "WHERE kind='plugin' AND status='pending'"):
            try:
                payload = json.loads(r["payload_json"])
            except (ValueError, TypeError) as exc:
                if activity is not None:
                    activity.write(
                        Category.APPROVAL, "sweep_locks_payload_corrupt",
                        f"error={safe_error_text(exc)}")
                continue
            if not isinstance(payload, dict):
                if activity is not None:
                    activity.write(
                        Category.APPROVAL, "sweep_locks_payload_corrupt",
                        f"error=payload not a dict (type={type(payload).__name__})")
                continue
            name = payload.get("name")
            # 検収是正 C5 (codex 1 周目 Important, 2026-09-12): `name` が
            # 非 str (list/dict 等) だと `pending_names.add(name)` 自体は
            # 例外を出さない (unhashable でない限り) が、後段の
            # `lock_file.name[:-len(".lock")] == name` 比較が常に False
            # になるだけでなく、`payload.get("name")` が unhashable な
            # 値 (list/dict) だった場合は `set.add` で `TypeError` に
            # なり、C2 で足した per-entry 隔離が **型のずれまでは** カバー
            # していなかった (この 1 行の型異常だけで sweep 全体が再び
            # 止まる)。
            #
            # ローカル 1 周目是正 M1 (codex 2 周目 Minor, 2026-09-12):
            # `isinstance(name, str)` だけでは不十分 — `name=""` (空文字)
            # のような plugin 名の正規形に合致しない文字列も pending_names
            # に入ってしまい、`.locks/.lock` (`lock_file.name[:-len(".lock")]`
            # が空文字になる、ファイル名が単に `.lock` のケース) を
            # 「pending」誤認して回収し損なう実害が実測された (指揮者の
            # ローカル 1 周目では「空文字は実害なし」と却下していたが、
            # codex がこの残留を実測したため採用に変更)。plugin 名の正規形
            # (`loader._PLUGIN_NAME_RE` = `^[a-z][a-z0-9_]{0,63}$`) を
            # `fullmatch` で流用し、不一致 (空文字・`sub/name` のような
            # パス区切り混入・大文字等) も corrupt 扱いで skip する。
            if name is not None and not (
                    isinstance(name, str)
                    and loader._PLUGIN_NAME_RE.fullmatch(name)):
                if activity is not None:
                    activity.write(
                        Category.APPROVAL, "sweep_locks_payload_corrupt",
                        f"error=name is not a valid plugin name ({name!r})")
                continue
            if name is not None:
                pending_names.add(name)
        for lock_file in locks_dir.iterdir():
            if lock_file.is_symlink() or not lock_file.is_file():
                continue
            if not lock_file.name.endswith(".lock"):
                continue
            name = lock_file.name[: -len(".lock")]
            if name in pending_names:
                continue  # 進行中の候補 — 残す
            try:
                fh = open(lock_file, "a+")
            except OSError:
                continue
            try:
                # ローカル 1 周目 P3 (test-hygiene 2026-09-12): `LOCK_EX`
                # (排他) を要求する — `LOCK_SH` (共有) に緩めると、外部が
                # `LOCK_SH` で保持中でも取得に成功してしまい誤って unlink
                # する (`tests/plugin/test_switch_locks_sweep.py::
                # test_sweep_orphans_keeps_lock_held_with_shared_lock_
                # even_without_pending_approval` が pin)。**`LOCK_NB` を
                # 外す変異はここでは検査しない** — 本関数は「runner 起動前
                # の単一プロセス、他プロセスからの同時アクセスが無い」
                # 前提 (関数冒頭の docstring) で動くため、`LOCK_NB` を
                # 外すとブロッキング待ちになり得るが、これはテストで
                # 再現するとタイムアウトなしにはハングし得る類の変異
                # (単体テストでは実行しない、設計上の既知の非検査対象)。
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue  # 誰かが保持中 — 残す (fail closed)
                try:
                    lock_file.unlink()
                except OSError:
                    pass
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
            finally:
                fh.close()

# ============================================================
# candidate_origin/candidate_path payload validator (§8.1-29、プラン10 Task 11d 逐語)
# ============================================================

_CANDIDATE_PATH_RE = {
    "staging": re.compile(r"^plugins/_staging/(\d+)/(?P<name>[a-z][a-z0-9_]{0,63})$"),
    "human": re.compile(r"^plugins/_human/(?P<name>[a-z][a-z0-9_]{0,63})$"),
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
    # round2 M1 是正 (2026-08-29、verified-round2.md M1): fullmatch に揃える
    # (`.match()` + `$` は末尾改行を受理する穴があるが、末尾改行付き
    # candidate_path は capture group が name と一致せず後段の等値検査
    # (`m.group(...) != name`) で fail closed する — この関数は「後段は
    # fail closed」の実例。fullmatch にしても意味論は変わらず、コード層
    # 全体で規約を揃える)。
    # round2 最終是正 裁定D (2026-08-29): `m.group(m.lastindex)` は
    # 「最後にマッチしたグループが name」という前提に依存し、将来
    # オプショナルなグループが増えると lastindex がずれる脆さがある。
    # 名前付きグループ (`?P<name>`) + `m.group("name")` に置き換え、
    # 挙動は不変のまま前提を明示する。
    m = pattern.fullmatch(candidate_path)
    if m is None or m.group("name") != name:
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


def _drop_staging_candidate(plugins_root: Path, payload: dict, *,
                            activity: "ActivityLog | None" = None) -> None:
    """終端決定 (approved/rejected/expired/invalidated) の tx 直後に staging
    候補を削除する (§5.1 手順 3 の掃除所有表、verified-codex-round1.md I1)。
    `candidate_origin='human'` は人間所有なので触らない (自動削除しない)。

    E4/確定-12 裁定 (2026-08-25): `CandidateMissingError` (候補が既に無い
    — 正常系、記録不要) と `ValueError`/`KeyError` (payload の正規形違反・
    必須キー欠落 = 契約違反) を分ける。後者は黙って進まず activity ERROR
    (`staging_drop_skipped`) を残す — この関数は既に終端決定の後に呼ばれる
    掃除ステップなので、決定そのものを pending 留置に戻すことはできない
    (掃除が漏れたことの可視化のみ)。"""
    if payload.get("candidate_origin") != "staging":
        return
    try:
        candidate_dir = resolve_candidate_dir(
            plugins_root, candidate_origin="staging",
            candidate_path=payload["candidate_path"], name=payload["name"])
    except CandidateMissingError:
        return
    except (ValueError, KeyError) as exc:
        if activity is not None:
            activity.write(Category.APPROVAL, "staging_drop_skipped",
                           f"payload contract violation: {safe_error_text(exc)}")
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
    # /code-review 2 周目 CR1 是正 (2026-09-18): `name` はここで
    # `.locks/<name>.lock` というパスに連結される **sink** なので、
    # 呼び出し元が何を渡しても `.locks/` の外に出ないことをこの 1 箇所で
    # 保証する。実測 (probe): 検証が無いと `plugin: ../evil` で
    # `plugins/evil.lock` が、`plugin: /tmp/x` で `/tmp/x.lock` が
    # `open(lock_path, "a+")` によって実際に作られた (候補 `config.yaml`
    # は改善 loop の agent が書く = 信頼境界の外)。`sweep_locks`
    # (switch.py:649) が既に同じ正規形で pending 名を検証しているので
    # 語彙を揃える。
    if not (isinstance(name, str) and loader._PLUGIN_NAME_RE.fullmatch(name)):
        raise ValueError(f"invalid plugin name for lock: {name!r}")
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


@contextlib.contextmanager
def _plugin_locks(plugins_root: Path, names):
    """[indicator-consumption-wiring] §2.3 (codex r4 I1 / r5 I2):
    strategy + 依存 indicator 名を `sorted(set(names))` の順に取る。

    - **重複排除**: 同一 indicator を複数 alias から参照しても 1 回だけ取る。

      /code-review 2 周目 (2026-09-18、「flock 再入」の docstring 主張の
      事実確認): 旧稿は「二重取得は同一プロセス内の flock 再入で無害」と
      書いていたが**これは誤り**。`flock(2)` のロックは open file
      description に紐づくので、同じパスを 2 回 `open()` すれば別 ofd に
      なり、2 本目の `LOCK_EX` は自プロセスのロックで待たされる
      (probe 実測: 2 本目の `LOCK_EX|LOCK_NB` が `BlockingIOError`)。
      `_plugin_lock` は blocking (`LOCK_NB` を使わない) なので、
      重複排除を外すと同一 indicator を 2 alias で参照する候補の
      submit/approve/bless は**自己デッドロックで永久に止まる**。
      つまりこの `set()` は spy の「取得回数」契約 (P2'') のためだけの
      ものではなく、正しさのために必須。非再入性は
      `test_plugin_lock_is_not_reentrant_within_one_process` で pin する。
    - **名前昇順**: 逆順で取る呼び出し元が現れても deadlock しないよう
      順序を 1 箇所に固定する (P2')。
    """
    with contextlib.ExitStack() as stack:
        for name in sorted(set(names)):
            stack.enter_context(_plugin_lock(plugins_root, name))
        yield


def _dependency_names(candidate_dir: Path, name: str) -> list[str]:
    """候補 `config.yaml` から依存 indicator 名を読む (lock 集合の材料)。
    読めない/依存なしなら `[name]` だけを返す。

    /code-review 2 周目 CR1 是正 (2026-09-18): ここは **gate より前**に
    走る (lock を取るために先に読む必要がある) ので、`config.yaml` の
    形状も plugin 名の形式も一切保証されていない。従来は
    `isinstance(str)` だけを見ていたため、

    - `indicators: [a, b]` (mapping でない) で `.values()` の
      `AttributeError` が gate に到達する前にクラッシュした
      (`submit`/`approve`/`bless` の 3 経路すべて)、
    - `plugin: a/b` で `.locks/a/b.lock` の `FileNotFoundError`、
      `plugin: ../evil` / `/tmp/x` では `.locks/` の外に実際に
      lock ファイルが作られた (probe 実測)

    という 2 つの実害があった。**不正形は依存名を足さないだけ**にし、
    形状・名前の拒否は直後の `_run_full_gate` → `loader._discover_one`
    (= `discover` の正規 parse) に任せる — ここでクラッシュさせない。
    縮退して自分の名前だけを lock しても守るべき書き込みは起きない:
    submit / bless は lock 内側の最初の手順が `_run_full_gate` で、
    parse 不能な `config.yaml` は `_discover_one` が必ず `None` を返す
    (approval 行も版も作らない)。approve は payload の `content_hash`
    との照合で pending 留置になる。この 2 点は
    `test_unparseable_candidate_config_is_rejected_by_the_gate_not_the_
    lock_set` で pin してある。

    `bless_candidate` の `pre_deps` 再読比較 (TOCTOU) は lock の前後で
    **同じこの関数**を使うので、不正形を落とす挙動は対称であり
    `candidate_changed` の判定を壊さない。
    """
    try:
        config = yaml.safe_load(
            (candidate_dir / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return [name]
    if not isinstance(config, dict):
        return [name]
    refs = config.get("indicators")
    if not isinstance(refs, dict):
        return [name]
    deps = [ref["plugin"] for ref in refs.values()
            if isinstance(ref, dict) and isinstance(ref.get("plugin"), str)
            and loader._PLUGIN_NAME_RE.fullmatch(ref["plugin"])]
    return [name, *deps]


def _write_gate_rows(conn: sqlite3.Connection, rows, *,
                     mission_outcome: str) -> None:
    """[profitability-floor] codex 2 周目レビュー CR5 (2026-09-13):
    `outcome.gate_rows` を `backtest_runs` へ書く生ループを 1 箇所に
    集約する。**tx 管理は呼び出し元の責務** (このループ自体は
    `BEGIN`/`COMMIT` に触れない) — 既に開いている tx の中で呼ぶ場合
    (成功パス、`submit_candidate`/`bless_candidate` 2 分岐) と、専用の
    短い tx を新設する場合 (`_persist_human_gate_rows`、失敗パス) の
    両方から共有する。以前はこの `for row in rows: save_harness_run(...)`
    が 3 箇所 (成功パス 1 + bless の 2 分岐) に手書きでコピーされて
    おり、将来の変更 (activity 追加・mission_outcome の意味変更等) を
    3 箇所そろえて直す必要があった。"""
    for row in rows:
        backtest_runs_store.save_harness_run(
            conn, commit=False, mission_id=None,
            mission_outcome=mission_outcome, **row)


def _persist_human_gate_rows(conn: sqlite3.Connection, rows: list[dict], *,
                             mission_outcome: str, now: datetime) -> None:
    """[profitability-floor] T1 Step 1-6 (2026-09-13、設計書 §3 T1-f):
    `run_kind_gate` (経由 `evaluate_strategy_adoption_gate`) が
    `record_fn` sink に積んだ行 (`GateOutcome.gate_rows`) を、
    `_run_full_gate` が `ValueError` を投げる分岐で**短い専用 tx**として
    保存してから raise する (証跡を失わない)。分類は呼び出し元が渡す
    `mission_outcome` のみに従う (`GateOutcome.verdict_kind` から一意に
    決まる値を渡すこと — ここで例外メッセージの部分一致は見ない)。
    `rows` が空 (indicator/signal、または strategy ゲートの手前で落ちた)
    なら no-op。"""
    if not rows:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        _write_gate_rows(conn, rows, mission_outcome=mission_outcome)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _run_full_gate(conn: sqlite3.Connection, candidate_dir: Path, *, name: str,
                   settings, now: datetime,
                   floor_mode: Literal["enforce", "warn"] = "enforce",
                   plugins_root: Path,
                   activity: "ActivityLog | None" = None):
    """P1 手順 2〜7 / P3 手順 1〜2 の共有ゲート本体 (§8.1-41 pin: P1 と P3
    は同じゲートを通る — 二重実装しない)。合格すれば
    `(meta, content_hash, artifact_hash, outcome: approval.GateOutcome)`
    を返す (**4 要素固定** — tuple をこれ以上伸ばさない、設計書 §3 T1-d)。
    不合格 (スナップショット不正・AST 不合格・pytest 不合格・hash 不一致・
    kind 別ゲート不合格) は ValueError — 何も作らない (fail closed)。

    [profitability-floor] T1 Step 1-4/1-6 (2026-09-13、codex R2-I1):
    `run_kind_gate` はゲート判定で例外を投げず常に `GateOutcome` を返す
    — raise するかどうかは**ここ**が `verdict_kind` だけを見て決める
    (`str(exc)` の部分一致による分類は禁止、pin F6-10)。捕捉済みの
    `gate_rows` は raise の直前に `_persist_human_gate_rows` で保存する
    (F6-12: `run_kind_gate` を抜けてくる想定外例外の経路でも同様に
    `gate_failed` で保存してから元例外を伝播する — 行を捨てない/
    `unprofitable` で保存しない)。"""
    check_candidate_snapshot(candidate_dir)  # 手順 2 (B-5: 名指し再利用)
    before_content, before_artifact = hashes_of(candidate_dir)  # 手順 3

    meta = loader._discover_one(candidate_dir, name)
    if meta is None:
        raise ValueError(
            f"plugin {name!r}: candidate at {candidate_dir} failed discovery "
            "validation (config/AST ゲート不合格 — ログ参照)")

    # [indicator-consumption-wiring] U4 (2026-09-14): 新規承認では
    # kind=indicator の `outputs` 宣言を必須にする。固定文言のみ
    # (候補名も理由も足さない — Global Constraints の語彙一覧)。
    # 位置は `assert_max_bars_within_limit` と同じ「ゲート本体の手前」。
    # /code-review 2 周目 CR7 (2026-09-18): 条件式は
    # `approval.outputs_required_violation` に一本化 (improve_loop の
    # commit gate と共有 — 挙動不変)。
    if approval.outputs_required_violation(meta):
        raise ValueError("outputs_required")

    rows: list[dict] = []
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

        # round2 #2 是正 (2026-08-29、verified-round2.md #2): F1 の
        # max_bars_limit ゲートは `approval.submit_plugin` にしか付かず、
        # プラン10で新設された本 corridor には無かった
        # (`grep -rn max_bars switch.py` → 0 件だった)。手順 7 の直前に置く
        # — 手順 7 (run_kind_gate、strategy は run_in_sample を呼ぶ) の
        # 前で fail closed する。
        approval.assert_max_bars_within_limit(meta, settings=settings)

        # [indicator-consumption-wiring] §2.3: 承認回廊の inventory は
        # **ここで 1 回だけ**構築する (submit / bless の両方が通る唯一の
        # 場所)。`run_kind_gate` は `pin_mode="require"` で解決し、
        # 失敗は判別子で返る (raise しない)。
        inventory = tools_plugin_loader.approved_plugins(
            conn, plugins_root, settings=settings)
        outcome = approval.run_kind_gate(  # 手順 7
            conn, meta, settings=settings, now=now, floor_mode=floor_mode,
            sink=rows, inventory=inventory, pin_mode="require")
        # CR7 (2026-09-13): `sink=rows` を渡したので `outcome.gate_rows`
        # は `rows` と同一オブジェクト — 二重の list を作らない。
        assert outcome.gate_rows is rows
    except SandboxError as exc:
        _persist_human_gate_rows(conn, rows, mission_outcome="gate_failed", now=now)
        raise ValueError(f"plugin {name!r}: {exc}") from exc
    except BaseException:
        # F6-12 (codex R3-M1): run_kind_gate を抜けてくる想定外例外
        # (holdout.NoHistoryError・履歴空の ValueError 等) も含め、
        # 捕捉済みの行を gate_failed で保存してから元例外を伝播する。
        _persist_human_gate_rows(conn, rows, mission_outcome="gate_failed", now=now)
        raise

    # [indicator-consumption-wiring] §2.8: 判別子から固定 ValueError。
    # 例外メッセージの部分一致による分類は禁止 — ここも判別子だけを見る。
    if outcome.verdict_kind == "indicator_unresolved":
        _persist_human_gate_rows(conn, rows, mission_outcome="gate_failed", now=now)
        raise ValueError(
            f"indicator_unresolved:{outcome.indicator_alias or '-'}:"
            f"{outcome.indicator_reason}")
    if outcome.verdict_kind == "insufficient_trades":
        _persist_human_gate_rows(conn, rows, mission_outcome="gate_failed", now=now)
        raise ValueError(
            f"plugin {name!r}: strategy not evaluable "
            f"({outcome.insufficient_trades_reason})")
    if outcome.verdict_kind == "floor" and floor_mode == "enforce":
        _persist_human_gate_rows(conn, rows, mission_outcome="unprofitable", now=now)
        g = settings.improve.gate
        # [profitability-floor] codex 2 周目レビュー CR1/CR3 (2026-09-13):
        # `strategy_gate.floor_rule_text(audience="human")` を使う —
        # `require_holdout_evaluable=True` のとき holdout 条件も条件節に
        # 含める (CR1、人間向けは遮断 8 の対象外)。既存の key=value 形式
        # (`min_pf=`/`require_positive_avg_r=`/`require_holdout_evaluable=`)
        # は既存 pin (F6-11a) が厳密一致で読むため残し、条件節を前段に
        # 追加する形にする。
        condition = strategy_gate.floor_rule_text(g, audience="human")
        # [profitability-floor-fix] G1 (2026-09-13): key=value 3 値の
        # 組み立ては `strategy_gate.floor_settings_kv` の唯一の場所に
        # 集約 (以前はここで 2 回手書きで複製していた)。フォーマットは
        # 既存 pin (F6-11a) と厳密一致 (変更なし)。
        settings_kv = strategy_gate.floor_settings_kv(g)
        # [profitability-floor] T1 Step 1-7/T1-g (2026-09-13、codex R2-I2):
        # `submit_candidate` のフロア不合格 activity 行はここで書く —
        # bless は常に floor_mode="warn" でこの分岐に到達しないため、
        # ここへの到達 = submit 経路のフロア拒否と同値。
        if activity is not None:
            activity.write(
                Category.APPROVAL, "submit_floor_rejected",
                f"name={name} unprofitable ({condition}) {settings_kv}")
        raise ValueError(
            f"plugin {name!r}: unprofitable ({condition}) ({settings_kv})")

    return meta, after_content, after_artifact, outcome


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
    activity: "ActivityLog | None" = None,
) -> int:
    """P1 (submit) の入口。kind 別ゲートを通し、1 つの短い tx で pending
    approval 行を作る。ジャーナルは作らない (§5.1 冒頭の pin)。

    [profitability-floor] T1 Step 1-7/T1-g (2026-09-13): 常に
    `floor_mode="enforce"` で `_run_full_gate` を呼ぶ (フロア不合格候補は
    approval 行を作らせない)。`activity` はフロア拒否 activity 行
    (`submit_floor_rejected`、`_run_full_gate` 内部で書く) の配線用。"""
    plugins_root, candidate_path = _candidate_locator(
        staging_dir=staging_dir, candidate_origin=candidate_origin,
        name=name, mission_id=mission_id)
    candidate_dir = staging_dir / name

    with _plugin_locks(plugins_root, _dependency_names(candidate_dir, name)):
        meta, content_hash, artifact_hash, outcome = _run_full_gate(
            conn, candidate_dir, name=name, settings=settings, now=now,
            floor_mode="enforce", plugins_root=plugins_root, activity=activity)

        g = settings.improve.gate
        payload = {
            "name": name, "kind": meta.kind,
            "candidate_origin": candidate_origin,
            "candidate_path": candidate_path,
            "content_hash": content_hash, "artifact_hash": artifact_hash,
            "metrics": outcome.metrics, "evaluable": outcome.evaluable,
            "eval_source": (settings.backtest.eval_source
                            if meta.kind == "strategy" else None),
            "base_interval": (settings.backtest.dataset().base_interval
                              if meta.kind == "strategy" else None),
            "eval_timeframe": (strategy_gate._eval_timeframe(meta.timeframe)
                              if meta.kind == "strategy" else None),
            "live_source": settings.plugin.producer_source,
            "mission_id": mission_id, "backlog_id": backlog_id,
            # [profitability-floor] T1 Step 1-8 (codex I3): 適用した閾値
            # snapshot (approval 行を作る 3 箇所すべてに載せる)。CR4
            # (2026-09-13): `ImproveGateSettings.snapshot()` に一本化。
            "profitability_floor": g.snapshot(),
            # [indicator-consumption-wiring] §2.7: 表示・監査用 (identity には
            # 使わない — identity は既存の `content_hash` のまま)。
            # **`outcome.resolved` から作る** — ここで再解決しない (P3')。
            "indicator_deps": (outcome.resolved.pin_object()
                               if outcome.resolved is not None else {}),
        }
        conn.execute("BEGIN IMMEDIATE")
        try:
            approval_id = approvals_store.create(
                conn, kind="plugin", payload=payload, now=now, commit=False)
            # [profitability-floor] T1 Step 1-6 (設計書 §3 T1-f):
            # `outcome.gate_rows` (record_fn sink に積まれた行、Step 1-4 で
            # 即時 commit が無効化された) を承認と同じ tx で保存する —
            # ここに保存しないと strategy 候補の in_sample/holdout 実測が
            # backtest_runs に一切残らなくなる (auto-commit の代替)。
            _write_gate_rows(conn, outcome.gate_rows, mission_outcome="approval")
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
    送り⑤どおり、Task 11 側がここで拡張する拡張点)。

    [switch-ops-hardening] T2 (設計書 §3.2): **単調性 API を迂回する唯一の
    `set_phase` 呼び出し**なので、ここで期待 phase を検査する。期待値は
    `switch_required` で決まる — `phase in ("switched", "recorded")` と
    書くと `switch_required=1` の行が `recorded` のまま (= 切替をしていない
    のに) 決定できてしまい IV-3 を破る fail open になる。"""
    guard_row = journal_store.get(conn, op_id)
    if guard_row is None:
        raise ValueError(f"op_id={op_id}: _finalize_decision found no journal row")
    expected_phase = "switched" if guard_row["switch_required"] else "recorded"
    if guard_row["phase"] != expected_phase:
        raise ValueError(
            f"op_id={op_id}: _finalize_decision expects phase="
            f"{expected_phase!r} but found {guard_row['phase']!r}")
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
    自体を発生させない (§5.1-1 (a) の再試行冪等性を壊さない)。

    [switch-ops-hardening] T2 (設計書 §3.2): **入口で終端行を弾く**。
    終端行を渡されると全 advance が guard でスキップされる一方 `switch_live`
    は走ってしまい、「approval は pending なのに live だけ新 target へ進み、
    行は終端なので次回 reconcile も拾わない」という誰も直さない状態が残る
    (probe 実測)。FS に触る前に落とすのが fail closed。"""
    entry_row = journal_store.get(conn, op_id)
    if entry_row is None or entry_row["phase"] in _TERMINAL_PHASES:
        raise ValueError(
            f"op_id={op_id}: _advance_to_decided called on a terminal/missing "
            f"journal row (phase={entry_row['phase'] if entry_row else None}) "
            "— 新しい操作は新しい journal 行に載せること (設計書 §3.2)")
    current_idx = _PHASE_ORDER.index(entry_row["phase"])

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
) -> "ApprovalOutcome":
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

    payload_pre = json.loads(row["payload_json"])
    candidate_origin_pre = payload_pre["candidate_origin"]
    candidate_path_pre = payload_pre["candidate_path"]
    candidate_dir_pre = None
    try:
        candidate_dir_pre = resolve_candidate_dir(
            plugins_root, candidate_origin=candidate_origin_pre,
            candidate_path=candidate_path_pre, name=name)
        dep_names = _dependency_names(candidate_dir_pre, name)
    except CandidateMissingError:
        dep_names = [name]

    with _plugin_locks(plugins_root, dep_names):  # 0b
        row = conn.execute(
            "SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
        if row["status"] != "pending":
            # [switch-ops-hardening] T5: status の出所は **read** (この行の SELECT)。
            return ApprovalOutcome(outcome="already_decided", name=name,
                                  status=row["status"])
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
            _drop_staging_candidate(plugins_root, payload, activity=activity)
            # T5: status の出所は **decision 結果** (同 lock 内で成功した
            # apply_decision に渡したリテラル — 再読しない)。
            return ApprovalOutcome(outcome="invalidated", name=name,
                                  status="invalidated")

        # [indicator-consumption-wiring] §2.3: P2 (決定時) に `require` で
        # **解決だけ**行う (gate は再実行しない)。submit → approve の間に
        # indicator が更新されていれば pending のまま拒否する — 版作成・
        # symlink 切替に進まない。inventory はこの lock の内側で構築する
        # (lock の外で読むと切替との間に別ロックの承認が割り込む)。
        meta_for_deps = (loader._discover_one(candidate_dir_pre, name)
                        if candidate_dir_pre is not None else None)
        if meta_for_deps is not None and meta_for_deps.kind == "strategy":
            inventory = tools_plugin_loader.approved_plugins(
                conn, plugins_root, settings=settings)
            try:
                resolve_indicator_deps(
                    meta_for_deps, inventory.inventory, settings=settings,
                    pin_mode="require")
            except IndicatorResolutionError as exc:
                raise ValueError(str(exc)) from exc

        # 0d: 同名の未完ジャーナルが「この approval 自身の再試行」であれば
        # その op_id から再開する。switched まで進んでいれば decide のみ
        # (§5.1 codex 12 周目の early-exit — 未定義の retry_approval を
        # 経由せずここで直接処理する、11c の NameError landmine を踏まない)。
        existing_journal = journal_store.get_open_by_name(conn, name)
        op_id = None
        rolled_back_op_id = None  # [switch-ops-hardening] T3 (§3.5)
        if existing_journal is not None and existing_journal["approval_id"] == approval_id:
            op_id = existing_journal["op_id"]
            if existing_journal["phase"] == "switched":
                # B3 是正: decide (=`_finalize_decision`) の前に、必ず
                # 参照物 (new_target の版) の存在と hash を再検証する
                # (§5.1-1 (a))。再検証失敗時は `_reverify_switched_journal`
                # が journal を reverted で閉じ activity ERROR を書く —
                # ここでは pending のまま return するだけでよい。
                # [switch-ops-hardening] T3 (設計書 §3.2、案 C): journal が
                # switched でも **live が本当に切り替わっているとは限らない**
                # (`advance(switched)` を commit してから `switch_live` を
                # 呼ぶ journal-first の順序ゆえ)。分類してから枝を選ぶ。
                live_class = classify_live(plugins_root, existing_journal)
                if live_class == "foreign":
                    # 0d-2b: 第三者に触られた — 触らず人間待ち (fail closed)。
                    if activity is not None:
                        activity.write(
                            Category.APPROVAL,
                            "switch_retry_unrecognized_live_target",
                            f"name={name} op_id={op_id}")
                    return ApprovalOutcome(outcome="foreign_waiting", name=name,
                                          status="pending", op_id=op_id)
                if live_class == "switched":
                    # 0d-2a: 切替は完了している — 従来どおり再検証して決定。
                    if not _reverify_switched_journal(
                            conn, existing_journal, plugins_root=plugins_root,
                            payload=payload, now=now, activity=activity):
                        return ApprovalOutcome(
                            outcome="still_pending", name=name, status="pending",
                            op_id=op_id, reason="reverify_failed")
                    _finalize_decision(conn, approval_id, op_id=op_id,
                                       decided_by=decided_by, now=now)
                    return ApprovalOutcome(
                        outcome="deployed", name=name, status="approved",
                        op_id=op_id, target=existing_journal["new_target"])
                # 0d-2c (not_switched): 停止行を巻き戻して閉じ、**新しい
                # journal 行で手順を頭から流す** (§5.3 契機③「手順を頭から
                # 流す」+ §5.1-1 (b)「巻き戻してから本来の操作を適用する」)。
                # `op_id` を None に戻さないと下流が終端行を使い回してしまう。
                _revert_one(conn, existing_journal, plugins_root=plugins_root,
                           now=now, activity=activity)
                conn.commit()
                rolled_back_op_id = op_id
                op_id = None
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

        def _close_own_unfinished_journal_if_any() -> int | None:
            # 確定-1 (Critical): この approval 自身の未完ジャーナル
            # (preparing/versioned/recorded。**switched のうち
            # `not_switched` は 0d-2c が既に閉じて op_id=None にしている**
            # ので、ここに残る switched 行は無い — [switch-ops-hardening] T3
            # で前提が変わった箇所) が残っていれば、候補が壊れて
            # pending 留置する前に `_revert_one` で閉じる。閉じないと name が
            # 永久に `UnresolvedJournalError` で封鎖される
            # (verified-local-round1.md 確定-1)。`_revert_one` は非 switched
            # 行に対して FS 副作用を一切持たない (`switch.py:156-158` と
            # 同じ論拠 — 安全)。
            # v1.7 / v1.8: 再開の前提 (3 つ組) が崩れた行もここで閉じる。
            if op_id is None:
                return None
            journal_row = journal_store.get(conn, op_id)
            if journal_row is not None and journal_row["phase"] != "switched":
                _revert_one(conn, journal_row, plugins_root=plugins_root,
                           now=now, activity=activity)
                conn.commit()
                return op_id
            return None

        candidate_origin = payload["candidate_origin"]
        candidate_path = payload["candidate_path"]
        try:
            candidate_dir = resolve_candidate_dir(
                plugins_root, candidate_origin=candidate_origin,
                candidate_path=candidate_path, name=name)
        except CandidateMissingError:
            _close_own_unfinished_journal_if_any()
            return ApprovalOutcome(  # pending のまま (§8.1-29)
                outcome="still_pending", name=name, status="pending",
                op_id=op_id, rolled_back_op_id=rolled_back_op_id,
                reason="candidate_missing")

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
            return ApprovalOutcome(  # 候補が壊れている/検査失敗 → pending のまま
                outcome="still_pending", name=name, status="pending",
                op_id=op_id, rolled_back_op_id=rolled_back_op_id,
                reason="snapshot_invalid")
        if (content_hash != payload["content_hash"]
                or artifact_hash != payload["artifact_hash"]):
            # 確定-1: ⓐ 不一致でも同様に自分の未完ジャーナルを閉じる。
            _close_own_unfinished_journal_if_any()
            return ApprovalOutcome(  # ⓐ 不一致 → pending のまま
                outcome="still_pending", name=name, status="pending",
                op_id=op_id, rolled_back_op_id=rolled_back_op_id,
                reason="hash_mismatch")

        live = plugins_root / name
        if live.is_symlink():
            old_kind, old_target = "symlink", os.readlink(live)
        elif live.exists():
            old_kind, old_target = "plain", None
        else:
            old_kind, old_target = "absent", None

        new_target = f".versions/{name}/{artifact_hash}"
        switch_required = not (old_kind == "symlink" and old_target == new_target)

        if op_id is not None and (
                bool(existing_journal["switch_required"]),
                existing_journal["old_kind"],
                existing_journal["old_target"]) != (
                    switch_required, old_kind, old_target):
            # [switch-ops-hardening] T11 (設計書 §3.2 / R13): 再開の前提が
            # resume 時点で崩れている。journal の (switch_required, old_kind,
            # old_target) は**行の作成時**の live から決まった値で、store 層に
            # 更新 API は無い。同じ行を使い回すと (i)「切替の要否・実際の切替・
            # finalize の期待」が別々の値を見て詰まり、(ii) old_target が
            # 古いままなので後の巻き戻しが第三者の変更を上書きして戻す。
            # 0d-2c と同形に、停止行を巻き戻して閉じ、新しい op_id で頭から
            # 流し直す。live が plain に化けた場合も old_kind の不一致として
            # ここで拾われる (journal の old_kind は absent/symlink のみ)。
            if activity is not None:
                activity.write(
                    Category.APPROVAL, "switch_resume_precondition_changed",
                    f"name={name} op_id={op_id} "
                    f"phase={existing_journal['phase']} "
                    f"stored=({int(bool(existing_journal['switch_required']))},"
                    f"{existing_journal['old_kind']},"
                    f"{existing_journal['old_target']}) "
                    f"now=({int(switch_required)},{old_kind},{old_target}) "
                    f"stored_switch_required={int(bool(existing_journal['switch_required']))} "
                    f"recomputed={int(switch_required)}")
            closed = _close_own_unfinished_journal_if_any()
            if closed is not None:
                rolled_back_op_id = closed
                op_id = None

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
            return ApprovalOutcome(outcome="legacy_plain_present", name=name,
                                  status="pending")

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

        # [switch-ops-hardening] T5: lock を抜ける前に結果を確定する (§3.5)。
        return ApprovalOutcome(
            outcome=("deployed_after_rollback" if rolled_back_op_id is not None
                     else "deployed"),
            name=name, status="approved", op_id=op_id,
            rolled_back_op_id=rolled_back_op_id,
            target=new_target)


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
        # E3 裁定 (2026-08-25、B-20): live symlink の実体が `root` (=
        # plugins_root) の外に出る場合は拒否する (containment 検査 —
        # loader._resolve_entity の同じ軸の是正と対称)。
        root_real = root.resolve()
        try:
            src.relative_to(root_real)
        except ValueError:
            raise ValueError(
                f"materialize_plugin: live symlink target escapes "
                f"plugins_root: {src}") from None
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
                   activity: "ActivityLog | None" = None) -> "ApprovalOutcome":
    """§5.3 契機③: 手順を頭から流す (lock → plain 検出 → ⓓ → ⓐ → 版(冪等) →
    git(no-op) → 切替(no-op なら済み) → apply_decision)。approve_candidate と
    同じ実装を呼ぶだけ (retry は「approve をもう一度呼ぶ」と同義 — §5.3 本文)。
    B-1 是正で plugins_root/settings を追加した (approve_candidate へそのまま
    透過する)。B3 是正: `activity` も同様に透過する (0d の再検証失敗時の
    ERROR 記録用)。[switch-ops-hardening] T5: `approve_candidate` が lock 内で
    確定した `ApprovalOutcome` を**そのまま透過する** (§3.5)。"""
    return approve_candidate(conn, approval_id, decided_by=decided_by, now=now,
                             plugins_root=plugins_root, settings=settings,
                             activity=activity)


def reject_candidate(conn: sqlite3.Connection, approval_id: int, *,
                     decided_by: str, reason: str, now: datetime,
                     plugins_root: Path,
                     activity: "ActivityLog | None" = None) -> None:
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
        # E4 裁定 (2026-08-25、確定-14): payload に name が無いのは plugin
        # kind の契約違反。旧稿は flock を取る対象が特定できないとして
        # `apply_decision` のみ直接通し reject を成立させていたが、これは
        # staging 候補を掃除しないまま決定を確定させてしまう (掃除漏れが
        # 無言のまま)。E4 の方針に合わせ、契約違反行は activity ERROR を
        # 残した上で **決定させず pending 留置**にする。
        if activity is not None:
            activity.write(
                Category.APPROVAL, "reject_rejected_payload_missing_name",
                f"approval_id={approval_id}: plugin payload に name が無く "
                "契約違反のため reject を実行せず pending 留置")
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
        _drop_staging_candidate(plugins_root, payload, activity=activity)


def process_expired_approvals(conn: sqlite3.Connection, *, plugins_root: Path,
                              now: datetime,
                              activity: "ActivityLog | None" = None) -> None:
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
            # E4 裁定 (2026-08-25、確定-14 と同じ軸): payload に name が
            # 無いのは plugin kind の契約違反。flock を取る対象が特定
            # できないので処理をスキップする (= pending 留置のまま) が、
            # 黙って進まず activity ERROR を残す。
            if activity is not None:
                activity.write(
                    Category.APPROVAL,
                    "expire_skipped_payload_missing_name",
                    f"approval_id={row['id']}: plugin payload に name が "
                    "無く契約違反のため expire を実行せず pending 留置")
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
                _drop_staging_candidate(plugins_root, payload, activity=activity)
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)


def bless_candidate(
    conn: sqlite3.Connection, *, name: str, human_dir: Path,
    settings, now: datetime, decided_by: str,
    on_floor_warning: "Callable[[str, str], None] | None" = None,
    activity: "ActivityLog | None" = None,
) -> int:
    """P3 (bless --from _human) の入口。live の形で二分:
    absent/symlink なら 1 tx で pending+証跡+preparing ジャーナル → 版/git/
    切替 → apply_decision。プレーンなら pending+証跡のみ →
    legacy_plain_present。

    [profitability-floor] T1 Step 1-5 (2026-09-13、codex I5): 常に
    `floor_mode="warn"` で `_run_full_gate` を呼ぶ — bless は人間裁定で
    フロア不合格を強行できる (承認は成立する)。**戻り値は `int`
    (approval_id) のまま変えない** (既存呼び出し元 11 箇所が無変更で
    通る、pin F6-7)。警告は `on_floor_warning(label, detail)` コールバック
    (`None` なら何もしない) と `activity` (`bless_floor_warning`) の
    2 経路 + payload の `floor_warning`/`floor_detail`/
    `profitability_floor` (永続側) で伝える。"""
    plugins_root = human_dir.parent.parent  # human_dir = plugins/_human/<name>

    approvals_store.expire_due(conn, now, commit=True)  # 0a

    # [indicator-consumption-wiring] §2.3 (codex r5 I2): bless は候補
    # (mutable な `_human`) から依存名を lock **前**に読むので、lock 取得後に
    # 候補の content_hash と依存名集合を再読し、事前読取と完全一致しなければ
    # 全解放して固定文言 `candidate_changed` で失敗する (retry しない)。
    pre_hash = loader.content_hash(human_dir)
    pre_deps = sorted(set(_dependency_names(human_dir, name)))

    with _plugin_locks(plugins_root, pre_deps):  # 0b
        if (loader.content_hash(human_dir) != pre_hash
                or sorted(set(_dependency_names(human_dir, name))) != pre_deps):
            raise ValueError("candidate_changed")
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

        meta, content_hash, artifact_hash, outcome = _run_full_gate(
            conn, human_dir, name=name, settings=settings, now=now,
            floor_mode="warn", plugins_root=plugins_root)

        if outcome.floor_warning:
            if on_floor_warning is not None:
                on_floor_warning(outcome.floor_warning, outcome.floor_detail)
            if activity is not None:
                activity.write(
                    Category.APPROVAL, "bless_floor_warning",
                    f"name={name} unprofitable")

        live = plugins_root / name
        if live.is_symlink():
            old_kind, old_target = "symlink", os.readlink(live)
        elif live.exists():
            old_kind, old_target = "plain", None
        else:
            old_kind, old_target = "absent", None

        g = settings.improve.gate
        payload = {
            "name": name, "kind": meta.kind,
            "candidate_origin": "human",
            "candidate_path": f"plugins/_human/{name}",
            "content_hash": content_hash, "artifact_hash": artifact_hash,
            "metrics": outcome.metrics, "evaluable": outcome.evaluable,
            "eval_source": (settings.backtest.eval_source
                            if meta.kind == "strategy" else None),
            "base_interval": (settings.backtest.dataset().base_interval
                              if meta.kind == "strategy" else None),
            "eval_timeframe": (strategy_gate._eval_timeframe(meta.timeframe)
                              if meta.kind == "strategy" else None),
            "live_source": settings.plugin.producer_source,
            "mission_id": None, "backlog_id": None,
            "floor_warning": outcome.floor_warning,
            "floor_detail": outcome.floor_detail,
            "profitability_floor": g.snapshot(),
            # [indicator-consumption-wiring] §2.7: submit と同形。
            "indicator_deps": (outcome.resolved.pin_object()
                               if outcome.resolved is not None else {}),
        }

        if old_kind == "plain":
            # 3-B: ジャーナル無し、pending+証跡のみの 1 tx
            conn.execute("BEGIN IMMEDIATE")
            try:
                approval_id = approvals_store.create(
                    conn, kind="plugin", payload=payload, now=now, commit=False)
                _write_gate_rows(conn, outcome.gate_rows, mission_outcome="approval")
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
            _write_gate_rows(conn, outcome.gate_rows, mission_outcome="approval")
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
