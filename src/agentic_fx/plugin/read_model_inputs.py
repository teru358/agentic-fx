"""plugin read model の入力を FS と DB から集める (読取専用)。

`read_model.build_read_model` に渡す `ReadInputs` を作る。daemon は自分の接続で、
CLI は observer の読取専用接続で呼ぶ。何も書かず、lock も取らない。1 件の壊れた
plugin や 1 つの表の失敗で全体を止めず、source ごとの状態 (ok / missing /
unavailable) に落とす。loader の自由文は読み取らず、読めない理由は構造から固定語を決める。
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from agentic_fx.plugin import loader
from agentic_fx.plugin.read_model import (
    ENTITY_PLAIN, ENTITY_SYMLINK, ENTITY_UNREADABLE, SOURCE_MISSING, SOURCE_OK,
    SOURCE_UNAVAILABLE, ApprovalRow, DeployOpRow, JournalRow, LiveEntry, LiveScan, ReadInputs,
)
from agentic_fx.store.db import connect_observer_readonly

_VERSION_DIR = re.compile(r"[0-9a-f]{64}")


def _skip_entry(name: str) -> bool:
    return name.startswith((".", "_"))


def _classify(entry: Path) -> LiveEntry:
    """1 entry を読む。例外は固定語の理由に落とし、本文は持ち出さない。"""
    name = entry.name
    if not loader.is_canonical_plugin_name(name):
        return LiveEntry(name, ENTITY_UNREADABLE, reason="non_canonical_name")
    artifact = None
    state = ENTITY_PLAIN
    try:
        if entry.is_symlink():
            state = ENTITY_SYMLINK
            target = os.readlink(entry)
            if not loader.is_canonical_symlink_target(name, target):
                return LiveEntry(name, ENTITY_UNREADABLE, reason="invalid_symlink")
            artifact = target.rsplit("/", 1)[-1]
            if loader._resolve_entity(entry.parent, name) is None:
                return LiveEntry(name, ENTITY_UNREADABLE, artifact, reason="invalid_symlink")
        missing = [item for item in loader.REQUIRED_FILES if not (entry / item).is_file()]
        meta, loader_reason = loader.discover_one_with_reason(entry, name)
    except OSError:
        return LiveEntry(name, ENTITY_UNREADABLE, artifact, reason="io_error")
    except Exception:  # noqa: BLE001 — 診断は 1 件の壊れた plugin で止めない
        return LiveEntry(name, ENTITY_UNREADABLE, artifact, reason="invalid_manifest")
    if meta is None:
        if loader_reason is not None and loader_reason.startswith(loader.IO_ERROR_REASON):
            return LiveEntry(name, ENTITY_UNREADABLE, artifact, reason="io_error")
        return LiveEntry(name, ENTITY_UNREADABLE, artifact,
                         reason="missing_files" if missing else "invalid_manifest")
    if state == ENTITY_SYMLINK and meta.artifact_hash != artifact:
        return LiveEntry(name, ENTITY_UNREADABLE, artifact, reason="version_mismatch")
    return LiveEntry(name, state, artifact, meta=meta)


def _versions(plugins_root: Path) -> tuple[dict[str, tuple[str, ...]], bool]:
    """``.versions`` の一覧と、読み損ねがあったか。dir が無いのは読み損ねではない。"""
    base = plugins_root / ".versions"
    result: dict[str, tuple[str, ...]] = {}
    unread = False
    try:
        names = sorted(item for item in base.iterdir() if item.is_dir())
    except FileNotFoundError:
        return result, False
    except OSError:
        return result, True
    for directory in names:
        if not loader.is_canonical_plugin_name(directory.name):
            continue
        try:
            hashes = tuple(sorted(
                child.name for child in directory.iterdir()
                if child.is_dir() and _VERSION_DIR.fullmatch(child.name)))
        except OSError:
            hashes = ()
            unread = True
        result[directory.name] = hashes
    return result, unread


def scan_live(plugins_root: Path) -> LiveScan:
    """``plugins/`` の entry と ``.versions`` を読む。"""
    if not plugins_root.exists():
        return LiveScan(SOURCE_MISSING)
    if not plugins_root.is_dir():
        return LiveScan(SOURCE_UNAVAILABLE)
    try:
        names = sorted(item for item in plugins_root.iterdir() if not _skip_entry(item.name))
    except OSError:
        return LiveScan(SOURCE_UNAVAILABLE)
    entries = []
    for entry in names:
        try:
            if not (entry.is_dir() or entry.is_symlink()):
                continue
        except OSError:
            continue
        entries.append(_classify(entry))
    try:
        root = plugins_root.resolve()
    except OSError:
        root = plugins_root
    versions, unread = _versions(plugins_root)
    return LiveScan(SOURCE_OK, root, tuple(entries), versions, unread)


def live_signature(plugins_root: Path) -> tuple:
    """``plugins/`` の見た目の署名。hash を計算せず stat と readlink だけで作り、
    entry の追加・削除・差し替え・編集が起きたときに変わる。"""
    items: list[tuple] = []
    try:
        names = sorted(item for item in plugins_root.iterdir() if not _skip_entry(item.name))
    except OSError:
        return ("unavailable",)
    for entry in names:
        try:
            if entry.is_symlink():
                target = os.readlink(entry)
                items.append((entry.name, "link", target, _file_stamps(entry)))
            elif entry.is_dir():
                items.append((entry.name, "dir", None, _file_stamps(entry)))
        except OSError:
            items.append((entry.name, "error", None, ()))
    versions = []
    try:
        for directory in sorted((plugins_root / ".versions").iterdir()):
            try:
                versions.append((directory.name, tuple(sorted(
                    child.name for child in directory.iterdir()))))
            except OSError:
                versions.append((directory.name, ()))
    except OSError:
        pass
    return ("ok", tuple(items), tuple(versions))


def _file_stamps(entry: Path) -> tuple:
    """必須 3 ファイルの stat と、loader が直下で検査する entry の名前の構成。

    ctime と mode も含める。権限の変更 (chmod) は mtime を変えないが ctime を変えるので、
    一時的に読めなかった file の権限が戻れば key が変わり、作り直す。"""
    try:
        listing = tuple(sorted(child.name for child in entry.iterdir()))
    except OSError:
        listing = None
    stamps: list = [("entries", listing)]
    for name in loader.REQUIRED_FILES:
        try:
            info = (entry / name).stat()
            stamps.append((name, info.st_mtime_ns, info.st_ctime_ns, info.st_mode,
                           info.st_size, info.st_ino))
        except OSError:
            stamps.append((name, None))
    return tuple(stamps)


def read_db_inputs(conn: sqlite3.Connection) -> dict:
    """承認・配備 op・switch journal の行を読む。表ごとに失敗を状態に落とす。"""
    result: dict = {"approvals": (), "deploy_ops": (), "journals": (),
                    "pending_status": SOURCE_OK, "journal_status": SOURCE_OK}
    try:
        result["approvals"] = tuple(
            ApprovalRow(row["id"], row["status"], row["decided_at"],
                        row["created_at"], row["payload_json"])
            for row in conn.execute(
                "SELECT id,status,decided_at,created_at,payload_json FROM approval_requests "
                "WHERE kind='plugin' ORDER BY id").fetchall())
    except sqlite3.Error:
        result["pending_status"] = SOURCE_UNAVAILABLE
    try:
        result["deploy_ops"] = tuple(
            DeployOpRow(row["op_id"], row["op_kind"], row["name"], row["phase"],
                        row["fail_reason"], row["subject_content_hash"],
                        row["target_content_hash"])
            for row in conn.execute(
                "SELECT op_id,op_kind,name,phase,fail_reason,subject_content_hash,"
                "target_content_hash FROM plugin_deploy_ops ORDER BY op_id").fetchall())
    except sqlite3.Error:
        result["journal_status"] = SOURCE_UNAVAILABLE
    try:
        result["journals"] = tuple(
            JournalRow(row["op_id"], row["kind"], row["approval_id"], row["name"],
                       row["phase"], row["new_target"])
            for row in conn.execute(
                "SELECT op_id,kind,approval_id,name,phase,new_target "
                "FROM plugin_switch_journal ORDER BY op_id").fetchall())
    except sqlite3.Error:
        result["journal_status"] = SOURCE_UNAVAILABLE
    return result


def db_generation(conn: sqlite3.Connection) -> tuple:
    """承認の行と配備・切替の行の最終更新の署名。中身は読まず、件数・最大 id・状態別の
    件数・最後の更新時刻だけで作る。表が読めないときは None を含む。"""
    parts: list = []
    queries = (
        "SELECT COUNT(*), COALESCE(MAX(id),0), COALESCE(MAX(decided_at),''), "
        "COALESCE(SUM(status='pending'),0), COALESCE(SUM(status='approved'),0), "
        "COALESCE(SUM(status='rejected'),0), COALESCE(SUM(status='expired'),0), "
        "COALESCE(SUM(status='invalidated'),0) FROM approval_requests WHERE kind='plugin'",
        "SELECT COUNT(*), COALESCE(MAX(op_id),0), COALESCE(MAX(updated_at),''), "
        "COALESCE(SUM(phase IN ('decided','reverted')),0) FROM plugin_switch_journal",
        "SELECT COUNT(*), COALESCE(MAX(op_id),0), COALESCE(MAX(updated_at),''), "
        "COALESCE(SUM(phase='intent'),0), COALESCE(SUM(phase='needs_retry'),0) "
        "FROM plugin_deploy_ops",
    )
    for query in queries:
        try:
            parts.append(tuple(conn.execute(query).fetchone()))
        except sqlite3.Error:
            parts.append(None)
    return tuple(parts)


def collect_inputs(plugins_root: Path, conn: sqlite3.Connection | None, *,
                   db_status: str = SOURCE_OK) -> ReadInputs:
    """FS と DB から入力を集める。``conn`` が None のとき DB の 2 source は ``db_status``。"""
    live = scan_live(plugins_root)
    if conn is None:
        return ReadInputs(live=live, pending_status=db_status, journal_status=db_status)
    return assemble_inputs(live, read_db_inputs(conn))


def assemble_inputs(live: LiveScan, db_rows: dict) -> ReadInputs:
    """FS の走査結果と、``read_db_inputs`` が返した DB の行から入力を組む。"""
    return ReadInputs(live=live, **db_rows)


def is_degraded(inputs: ReadInputs) -> bool:
    """読み損ねた入力を含むか。一時的な失敗かもしれないので、結果を覚えてはいけない。"""
    return (inputs.live.status == SOURCE_UNAVAILABLE
            or SOURCE_UNAVAILABLE in (inputs.pending_status, inputs.journal_status)
            or inputs.live.versions_unread
            or any(entry.reason == "io_error" for entry in inputs.live.entries))


def open_observer(root: Path) -> tuple[sqlite3.Connection | None, str]:
    """CLI 用の読取専用接続 (DB を作らず、移行もしない)。"""
    db_path = root / "data" / "agentic.db"
    if not db_path.is_file():
        return None, SOURCE_MISSING
    try:
        return connect_observer_readonly(db_path), SOURCE_OK
    except (sqlite3.Error, OSError):
        return None, SOURCE_UNAVAILABLE
