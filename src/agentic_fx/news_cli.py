"""Human-only command surface for managed news sources."""
from __future__ import annotations

import getpass
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import ConfigError, load_settings
from agentic_fx.datafeed import news_probe
from agentic_fx.datafeed.news_url import NewsInputError, fold_url, validate_name, validate_url
from agentic_fx.store import news_health, news_sources
from agentic_fx.store.db import connect_existing_rw, connect_observer_readonly

_log = logging.getLogger("agentic_fx.news_cli")
_DB_MISSING = "ニュース源を読めません (DB がありません)。"
_UNEXPECTED = "予期しない失敗のため中止しました。"
_REFLECT = "反映は次の収集周期 (最長 30 分) です。"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db(root: Path, *, write: bool):
    path = root / "data" / "agentic.db"
    return connect_existing_rw(path) if write else connect_observer_readonly(path)


def _timeout(root: Path) -> float:
    return load_settings(root / "config" / "settings.yaml").worker.data_hook_timeout_sec


def _reason_label(cause: str) -> str:
    return {"failures": "連続失敗", "refusals": "先方の拒否が続く", "directive": "先方の指示"}[cause]


def _print_result(result: news_probe.ProbeResult) -> None:
    print(f"取得: {result.count} 件 (うち本文・要約あり {result.with_body} 件)")


def _write_activity(root: Path, event: str, name: str, fetcher: str | None = None) -> None:
    summary = name if fetcher is None else f"{name} ({fetcher})"
    ActivityLog(root / "logs" / "activity.log").write(Category.NEWS, event, summary)


def _list(conn) -> int:
    rows = news_sources.list_all(conn)
    if not rows:
        print("ニュース源はありません。")
        return 0
    enabled = sum(bool(row["enabled"]) for row in rows)
    print(f"ニュース源: {len(rows)} 件 (有効 {enabled} / 無効 {len(rows) - enabled})")
    now = _now()
    for row in rows:
        state = "有効" if row["enabled"] else "無効"
        print(f"- {row['name']} ({row['fetcher']}, {state})")
        print(f"  {fold_url(row['url'])}")
        try:
            observed = news_health.read(conn, row["name"])
            if observed.corrupt:
                detail = "読めません"
            elif observed.record is None or observed.record.last_outcome is None:
                detail = "未取得"
            elif observed.record.last_outcome == "ok":
                detail = f"成功 {observed.record.last_count or 0} 件"
            else:
                detail = f"失敗 ({observed.record.last_reason or '取得できません'})"
            print(f"  直近: {detail}")
            wait = news_health.read_effective_wait(conn, row["name"], now)
            if wait:
                until, cause = wait
                print(f"  待機: {until.strftime('%Y-%m-%d %H:%M UTC')} ({_reason_label(cause)})")
            elif observed.record and max(observed.record.consecutive_failures,
                                         observed.record.retry_after_streak) >= news_health.N:
                print("  連続 %d 回" % max(observed.record.consecutive_failures,
                                           observed.record.retry_after_streak))
        except sqlite3.Error:
            print("  直近: 読めません")
    return 0


def _add(args, root: Path, conn) -> int:
    url = news_probe.read_url_from_stdin(sys.stdin, getpass_fn=getpass.getpass) if args.url == "-" else args.url
    validate_name(args.name)
    validate_url(url)
    duplicate = news_sources.find_duplicate(conn, args.name, url)
    if duplicate:
        kind, existing = duplicate
        print((f"名前 {existing} は既に使われています。" if kind == "name"
               else f"URL が源 {existing} と重複"), file=sys.stderr)
        return 1
    try:
        result = news_probe.probe(args.fetcher, url, args.name, timeout_sec=_timeout(root))
    except news_probe.ProbeFailure as exc:
        print(f"取得に失敗したので追加しません: {exc.reason}", file=sys.stderr)
        return 1
    _print_result(result)
    if result.count == 0 and not args.yes:
        if args.url == "-" or not sys.stdin.isatty():
            print("0 件のため追加しません。", file=sys.stderr)
            return 1
        print("0 件でした。それでも追加しますか? [y/N]", end=" ", flush=True)
        if sys.stdin.readline().strip() != "y":
            print("追加しません。")
            return 1
    try:
        news_sources.add(conn, name=args.name, fetcher=args.fetcher, url=url,
                         added_by="user", now=_now(), enabled=True)
    except sqlite3.IntegrityError:
        print(f"名前 {args.name} は既に使われています。", file=sys.stderr)
        return 1
    _write_activity(root, "news_source_added", args.name, args.fetcher)
    print(f"追加しました: {args.name} ({args.fetcher})")
    print(_REFLECT)
    if result.count == 0 and args.fetcher == "feed":
        print("`--fetcher web` を試せます。")
    return 0


def _set_enabled(args, root: Path, conn, enabled: bool) -> int:
    if any(ord(char) < 32 or ord(char) == 127 for char in args.name):
        print("名前の形式が正しくありません", file=sys.stderr)
        return 2
    row = conn.execute("SELECT * FROM news_sources WHERE name=?", (args.name,)).fetchone()
    if row is None:
        print(f"ニュース源 {args.name} はありません。", file=sys.stderr)
        return 1
    if bool(row["enabled"]) == enabled:
        print("既に有効です" if enabled else "既に無効です")
        return 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        news_sources.set_enabled(conn, row["id"], enabled, commit=False)
        if enabled:
            news_health.initialize_for_enable(conn, args.name, _now())
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    _write_activity(root, "news_source_enabled" if enabled else "news_source_disabled", args.name)
    print(f"{'有効' if enabled else '無効'}にしました: {args.name}")
    print(_REFLECT)
    return 0


def _test(args, root: Path, conn) -> int:
    row = conn.execute("SELECT * FROM news_sources WHERE name=?", (args.name,)).fetchone()
    if row is None:
        print(f"ニュース源 {args.name} はありません。", file=sys.stderr)
        return 1
    try:
        wait = news_health.read_effective_wait(conn, args.name, _now())
        if wait:
            until, cause = wait
            print(f"この源は {until.strftime('%Y-%m-%d %H:%M UTC')} まで待機中です ({_reason_label(cause)}、収集器は取得しません)。")
        result = news_probe.probe(row["fetcher"], row["url"], row["name"], timeout_sec=_timeout(root))
    except news_probe.ProbeFailure as exc:
        print(f"取得に失敗しました: {exc.reason}", file=sys.stderr)
        return 1
    _print_result(result)
    return 0


def dispatch_news(args, root: Path) -> int:
    """Run a parsed ``afx news`` command without initialising the root."""
    command = args.news_command
    try:
        conn = _db(root, write=command in {"add", "enable", "disable"})
    except FileNotFoundError:
        print(_DB_MISSING, file=sys.stderr)
        return 1
    except sqlite3.Error:
        print("DB を開けません。", file=sys.stderr)
        return 1
    try:
        if command == "list":
            return _list(conn)
        if command == "add":
            return _add(args, root, conn)
        if command == "enable":
            return _set_enabled(args, root, conn, True)
        if command == "disable":
            return _set_enabled(args, root, conn, False)
        if command == "test":
            return _test(args, root, conn)
        return 2
    except NewsInputError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ConfigError:
        print("設定を読めません。", file=sys.stderr)
        return 1
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            print("DB が使用中です。もう一度実行してください。", file=sys.stderr)
        else:
            print("DB を操作できません。", file=sys.stderr)
        return 1
    except sqlite3.Error:
        print("DB を操作できません。", file=sys.stderr)
        return 1
    except SystemExit:
        raise
    except Exception:  # no exception data is safe at this boundary
        print(_UNEXPECTED, file=sys.stderr)
        return 1
    finally:
        conn.close()
