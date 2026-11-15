"""`afx history sync / gaps list / conflicts list|show|resolve` の CLI。

sync と resolve は停止中の local CHANGE: DB を開く前に instance lock を取り、取れなければ
rc=1・固定理由 `instance_locked` で何も開かない。gaps list と conflicts list/show は
読み取り専用で、DB を作らず `mode=ro` で開く (setup/enroll の保持者がいるときは読まない)。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from agentic_fx.backtest import history_resolve, history_sync
from agentic_fx.backtest.history_health import NO_BARS_RELPATH, NoBarsConfigError, load_no_bars
from agentic_fx.config import load_settings
from agentic_fx.store import history_sync as sync_store
from agentic_fx.store.db import connect, connect_observer_readonly, init_db
from agentic_fx.store.instance_lock import InstanceAlreadyRunning, acquire_instance_lock

LOCKED_CODE = "instance_locked"
LOCKED_TEXT = (f"{LOCKED_CODE}: daemon などが稼働中のため実行できません "
               "(保持者は特定できません)。daemon を止めてから再実行してください。"
               "その間は kill switch の監視も止まります")
NOT_INITIALIZED = "history_sync: not initialized (daemon を再起動して schema を導入してください)"

READONLY_COMMANDS = {("gaps", "list"), ("conflicts", "list"), ("conflicts", "show")}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def is_readonly(args: argparse.Namespace) -> bool:
    return (getattr(args, "history_command", None), _sub(args)) in READONLY_COMMANDS


def _sub(args: argparse.Namespace) -> str | None:
    return (getattr(args, "gaps_command", None) or getattr(args, "conflicts_command", None))


def needs_instance_lock(args: argparse.Namespace) -> bool:
    command = getattr(args, "history_command", None)
    if command in ("import", "sync"):
        return True
    return command == "conflicts" and getattr(args, "conflicts_command", None) == "resolve"


def acquire_or_report(root: Path):
    """instance lock を取る。取れなければ固定理由を出して None。DB は開かない。"""
    try:
        return acquire_instance_lock(root / "data")
    except InstanceAlreadyRunning:
        print(LOCKED_TEXT, file=sys.stderr)
        return None


def register(history_sub, surface_kw) -> None:
    """`history` 配下に sync / gaps / conflicts を登録する。help は cli_surface から作る。"""
    sync = history_sub.add_parser("sync", **surface_kw("history sync", leaf=True))
    sync.add_argument("--rescan-holdout", action="store_true", dest="rescan_holdout",
                      help="現在の holdout を再検査して gap queue を作り直す (取得範囲は広げない)")
    sync.add_argument("--until-done", action="store_true", dest="until_done",
                      help="due の gap 取得が尽きる・失敗・進捗なしまで、上限付きの回を続ける")

    gaps = history_sub.add_parser("gaps", **surface_kw("history gaps"))
    gaps_sub = gaps.add_subparsers(dest="gaps_command", required=True)
    gaps_sub.add_parser("list", **surface_kw("history gaps list", leaf=True))

    conflicts = history_sub.add_parser("conflicts", **surface_kw("history conflicts"))
    conflicts_sub = conflicts.add_subparsers(dest="conflicts_command", required=True)
    lst = conflicts_sub.add_parser("list", **surface_kw("history conflicts list", leaf=True))
    lst.add_argument("--state", choices=("open", "kept", "replaced", "superseded"),
                     help="この state の記録だけを表示する")
    show = conflicts_sub.add_parser("show", **surface_kw("history conflicts show", leaf=True))
    show.add_argument("conflict_id", type=int)
    resolve = conflicts_sub.add_parser("resolve", **surface_kw("history conflicts resolve",
                                                               leaf=True))
    resolve.add_argument("conflict_id", type=int)
    resolve.add_argument("--action", required=True, choices=history_resolve.ACTIONS,
                         help="keep-existing: 既存値を維持する判断を記録 / "
                              "replace-with-incoming: incoming 値で履歴を置き換える (epoch が変わる)")
    resolve.add_argument("--reason", required=True,
                         help="判断の理由 (人間専用の記録。ログや event には出さない)")


# ---- 読み取り専用 -------------------------------------------------------------

def _open_readonly(root: Path):
    """保持者を確かめて読み取り専用で開く。読まないときは (None, 固定の説明)。"""
    from agentic_fx.status import UNKNOWN_HOLDER, _locked_by, probe_instance_lock

    if probe_instance_lock(root) == "locked" and _locked_by(root) != UNKNOWN_HOLDER:
        return None, "setup / enroll の実行中のため読みません。終わってから再実行してください"
    db_path = root / "data" / "agentic.db"
    try:
        return connect_observer_readonly(db_path, force_ro=True), None
    except Exception:  # noqa: BLE001 - 作成も修復もせず、固定理由だけ
        return None, "database_unavailable: 履歴 DB を読み取り専用で開けませんでした"


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def dispatch_readonly(args: argparse.Namespace, root: Path) -> int:
    conn, message = _open_readonly(root)
    if conn is None:
        print(message)
        return 1
    try:
        if args.history_command == "gaps":
            return _gaps_list(conn, root)
        if args.conflicts_command == "list":
            return _conflicts_list(conn, args.state)
        return _conflicts_show(conn, args.conflict_id)
    finally:
        conn.close()


def _dataset_filter(root: Path):
    """現 dataset。settings が読めなければ全件 (None)。"""
    try:
        settings = load_settings(root / "config" / "settings.yaml")
        dataset = settings.backtest.dataset()
        return (dataset.source, dataset.base_interval)
    except Exception:  # noqa: BLE001
        return None


def _gaps_list(conn, root: Path) -> int:
    if not _has_table(conn, "history_sync_gaps"):
        print(NOT_INITIALIZED)
        return 0
    flt = _dataset_filter(root)
    where, params = ("WHERE source=? AND interval=?", list(flt)) if flt else ("", [])
    print("対象は holdout 内と直近です。in-sample は作りません。")
    cur = conn.execute(
        f"SELECT * FROM history_sync_gaps {where} ORDER BY symbol, day_utc, kind", params)
    rows = cur.fetchall()
    expired = 0
    shown = 0
    for r in rows:
        if r["state"] == "expired":
            expired += 1
            continue
        if r["state"] != "open":
            continue
        shown += 1
        print(f"gap symbol={r['symbol']} day={r['day_utc']} kind={r['kind']} "
              f"missing={r['missing_count'] if r['missing_count'] is not None else '-'} "
              f"attempts={r['attempts']} no_improve={r['no_improve_count']} "
              f"next_retry={r['next_retry_at'] or 'due'} first_seen={r['first_seen_at']}")
    if not shown:
        print("open gap はありません")
    if expired:
        print(f"expired: holdout 外、自動修復なし {expired} 日")
    scope = "WHERE source=? AND interval=? AND " if flt else "WHERE "
    states = conn.execute(
        f"SELECT * FROM history_sync_series_state {scope}symbol <> ? ORDER BY symbol",
        [*params, sync_store.CONTROL_SYMBOL]).fetchall()
    for s in states:
        print(f"series symbol={s['symbol']} last_processed_e={s['last_processed_e'] or 'uninitialized'} "
              f"attempts={s['attempts']} next_retry={s['next_retry_at'] or '-'} "
              f"last_error={s['last_error'] or '-'}")
    return 0


def _conflicts_list(conn, state: str | None) -> int:
    if not _has_table(conn, "history_conflicts"):
        print(NOT_INITIALIZED)
        return 0
    rows = conn.execute(
        "SELECT * FROM history_conflicts" + (" WHERE state=?" if state else "")
        + " ORDER BY id", ([state] if state else [])).fetchall()
    if not rows:
        print("conflict はありません")
    for r in rows:
        print(f"conflict id={r['id']} state={r['state']} source={r['source']} "
              f"interval={r['interval']} symbol={r['symbol']} bar_time={r['bar_time']} "
              f"seen={r['seen_count']} origin={r['origin']}/{r['last_origin']} "
              f"first_seen={r['first_seen_at']} last_seen={r['last_seen_at']}")
    return 0


def _conflicts_show(conn, conflict_id: int) -> int:
    if not _has_table(conn, "history_conflicts"):
        print(NOT_INITIALIZED)
        return 0
    r = conn.execute("SELECT * FROM history_conflicts WHERE id=?", (conflict_id,)).fetchone()
    if r is None:
        print(f"conflict_not_found: id={conflict_id}", file=sys.stderr)
        return 2
    for key in ("id", "state", "source", "interval", "symbol", "bar_time", "history_epoch",
                "origin", "last_origin", "seen_count", "first_seen_at", "last_seen_at",
                "first_run_id", "last_run_id", "existing_json", "incoming_json",
                "existing_digest", "incoming_digest", "resolved_at", "resolved_by",
                "resolution_action", "revision_id", "reason_ref"):
        print(f"{key}: {r[key]}")
    return 0


# ---- 停止中の変更 -------------------------------------------------------------

def run_sync_command(args: argparse.Namespace, root: Path, settings) -> int:
    """preflight は I/O の前 (lock も取らない)。以降は instance lock を保持して実行する。"""
    try:
        history_sync.preflight(settings)
    except history_sync.SyncRefused as exc:
        print(f"{exc.code}: 同期できません", file=sys.stderr)
        return exc.rc
    try:
        no_bars = load_no_bars(root / NO_BARS_RELPATH)
    except NoBarsConfigError as exc:
        print(f"no_bars_unavailable: {exc}", file=sys.stderr)
        return 1
    lock = acquire_or_report(root)
    if lock is None:
        return 1
    conn = None
    try:
        conn = connect(root / "data" / "agentic.db")
        init_db(conn)
        result = history_sync.run_sync(
            conn, settings, now=_utc_now(), no_bars=no_bars, rescan=args.rescan_holdout,
            until_done=args.until_done)
        for line in history_sync.render(result, settings.backtest.dataset(),
                                        until_done=args.until_done):
            print(line)
        return result.rc
    finally:
        if conn is not None:
            conn.close()
        lock.close()


def run_resolve_command(args: argparse.Namespace, root: Path) -> int:
    lock = acquire_or_report(root)
    if lock is None:
        return 1
    db_path = root / "data" / "agentic.db"
    conn = None
    try:
        conn = connect(db_path)
        init_db(conn)
        try:
            done = history_resolve.resolve(
                conn, db_path, conflict_id=args.conflict_id, action=args.action,
                reason=args.reason)
        except history_resolve.ResolveError as exc:
            print(f"{exc.code}: 解決しませんでした (conflict id={args.conflict_id})",
                  file=sys.stderr)
            if exc.hint:
                print(exc.hint, file=sys.stderr)
            return exc.rc
        note = "" if done.changed else " (すでに同じ状態です。変更なし)"
        print(f"resolved id={done.conflict_id} action={done.action} state={done.state}{note}")
        if done.revision_id is not None:
            print(f"revision={done.revision_id} backup={done.backup}")
            print("注意: 履歴の置換で epoch が変わります。旧 epoch に結びついた pending の承認・"
                  "rollback は照合不一致になり得ます (売買・配備は自動では止まりません)。"
                  "daemon を再開すると kill switch の監視も戻ります")
        return 0
    finally:
        if conn is not None:
            conn.close()
        lock.close()
