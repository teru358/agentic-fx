"""Human-only command surface for managed news sources (``afx news``).

The entry point deliberately avoids the root initialisation, settings and
database-creation steps of the general CLI dispatch: it must never create a
database, and it must never print an exception, because a source URL can carry
a secret.  Retrieval failures arrive as fixed reason words from ``news_probe``;
anything else unexpected is folded into one fixed sentence.
"""
from __future__ import annotations

import getpass
import logging
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import load_env_file, load_settings
from agentic_fx.datafeed import news_probe
from agentic_fx.datafeed.news_url import NewsInputError, fold_url, normalize_url, validate_name, validate_url
from agentic_fx.store import news_health, news_sources
from agentic_fx.store.db import connect_existing_rw, connect_observer_readonly

_log = logging.getLogger("agentic_fx.news_cli")

_DB_MISSING = "ニュース源を読めません (DB がありません)。"
_DB_BUSY = "DB が使用中です。もう一度実行してください。"
_DB_FAILED = "DB を操作できません。"
_SETTINGS_UNREADABLE = "設定を読めません。"
_UNEXPECTED = "予期しない失敗のため中止しました。"
_BAD_INPUT = "入力が正しくありません"
_INTERRUPTED = "中断しました。"
_EMPTY_STDIN = "標準入力の URL が正しくありません"
_REFLECT = "反映は次の収集周期 (最長 30 分) です。"
_KNOWN_INPUT_ERRORS = frozenset({
    "名前の形式が正しくありません",
    "URL の形式が正しくありません",
    "標準入力の URL が正しくありません",
    "取得方式が正しくありません",
})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_HTTP_REASON = re.compile(r"HTTP [0-9]{3}")
_LAST_REASONS = frozenset({news_probe.REASON_TIMEOUT, news_probe.REASON_CONNECT, news_probe.REASON_OTHER})
_WAIT_LABELS = {"failures": "連続失敗", "refusals": "先方の拒否が続く", "directive": "先方の指示"}
_WRITE_COMMANDS = frozenset({"add", "enable", "disable"})


class _Stop(Exception):
    """Ends the command with a fixed message (stderr) and a return code."""

    def __init__(self, rc: int, message: str) -> None:
        super().__init__(message)
        self.rc = rc
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plain(text: str) -> str:
    return _CONTROL.sub("?", text)


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1 for char in text)


def _pad(text: str, width: int) -> str:
    return text + " " * (width - _width(text))


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _check_existing_name(name: str) -> None:
    if _CONTROL.search(name):
        raise NewsInputError("名前の形式が正しくありません")


def _open(root: Path, *, write: bool) -> sqlite3.Connection:
    path = root / "data" / "agentic.db"
    missing = False
    try:
        return connect_existing_rw(path) if write else connect_observer_readonly(path)
    except FileNotFoundError:
        missing = True
    except sqlite3.OperationalError as exc:
        missing = "unable to open" in str(exc).lower()
        if not missing:
            raise
    raise _Stop(1, _DB_MISSING)


def _timeout(root: Path) -> float:
    try:
        load_env_file(root / ".env")
        return load_settings(root / "config" / "settings.yaml").worker.data_hook_timeout_sec
    except Exception:  # noqa: BLE001 - settings text must not reach the output
        pass
    raise _Stop(1, _SETTINGS_UNREADABLE)


def _write_activity(root: Path, event: str, name: str, fetcher: str | None = None) -> None:
    summary = name if fetcher is None else f"{name} ({fetcher})"
    try:
        ActivityLog(root / "logs" / "activity.log").write(Category.NEWS, event, summary)
    except Exception:  # noqa: BLE001 - the record is best-effort, the change stands
        _log.warning("news activity record failed (%s)", event)


def _print_result(result: news_probe.ProbeResult) -> None:
    print(f"取得: {result.count} 件 (うち本文・要約あり {result.with_body} 件)")


def _latest(observed: news_health.HealthRead) -> tuple[str, str]:
    """(detail, time text) of the last retrieval, from the health record only."""
    record = observed.record
    if observed.corrupt:
        return "読めません", ""
    if record is None or record.last_outcome is None:
        return "未取得", ""
    when = _utc_text(record.last_at) if record.last_at else ""
    if record.last_outcome == "ok":
        return f"成功 {record.last_count or 0} 件", when
    reason = record.last_reason
    if not (reason in _LAST_REASONS or (reason and _HTTP_REASON.fullmatch(reason))):
        reason = news_probe.REASON_OTHER
    return f"失敗 ({reason})", when


def _list(conn: sqlite3.Connection) -> int:
    rows = news_sources.list_all(conn)
    if not rows:
        print("ニュース源はありません。")
        return 0
    enabled = sum(bool(row["enabled"]) for row in rows)
    print(f"ニュース源: {len(rows)} 件 (有効 {enabled} / 無効 {len(rows) - enabled})")
    now = _now()
    lines: list[tuple[str, str, str, str, str, str]] = []
    for row in rows:
        name = _plain(row["name"])
        state = "有効" if row["enabled"] else "無効"
        try:
            observed = news_health.read(conn, row["name"])
            detail, when = _latest(observed)
            wait = news_health.read_effective_wait(conn, row["name"], now)
            streak = (max(observed.record.consecutive_failures, observed.record.retry_after_streak)
                      if observed.record else 0)
            if wait:
                until, cause = wait
                prefix = "" if cause == "directive" else f"連続 {streak} 回、"
                note = f"{prefix}{_utc_text(until)} まで待機 ({_WAIT_LABELS[cause]})"
            elif streak >= news_health.N:
                note = f"連続 {streak} 回"
            else:
                note = ""
        except (sqlite3.Error, ValueError, TypeError, OverflowError):
            detail, when, note = "読めません", "", ""
        try:
            folded = fold_url(row["url"])
        except ValueError:
            folded = "(URL を表示できません)"
        tail = "  ".join(part for part in (when, note) if part)
        lines.append((name, state, row["fetcher"], f"直近: {detail}", tail, folded))
    name_width = max(_width(line[0]) for line in lines)
    fetcher_width = max(_width(line[2]) for line in lines)
    detail_width = max(_width(line[3]) for line in lines)
    for name, state, fetcher, detail, tail, folded in lines:
        text = f"{_pad(name, name_width)}  {state}  {_pad(fetcher, fetcher_width)}  {_pad(detail, detail_width)}  {tail}"
        print(text.rstrip())
        print(f"  {folded}")
    return 0


def _add_input(args) -> tuple[str, str]:
    url = news_probe.read_url_from_stdin(sys.stdin, getpass_fn=getpass.getpass) if args.url == "-" else args.url
    validate_name(args.name)
    validate_url(url)
    return args.name, url


def _duplicate_stop(conn: sqlite3.Connection, name: str, url: str) -> None:
    duplicate = news_sources.find_duplicate(conn, name, url)
    if duplicate:
        kind, existing = duplicate
        raise _Stop(1, f"名前 {_plain(existing)} は既に使われています。" if kind == "name"
                    else f"URL が源 {_plain(existing)} と重複")


def _add(args, root: Path, conn: sqlite3.Connection, name: str, url: str) -> int:
    _duplicate_stop(conn, name, url)
    stored = normalize_url(url)
    timeout = _timeout(root)
    try:
        result = news_probe.probe(args.fetcher, stored, name, timeout_sec=timeout)
    except news_probe.ProbeFailure as exc:
        raise _Stop(1, f"取得に失敗したので追加しません: {exc.reason}") from None
    _print_result(result)
    if result.count == 0 and not args.yes:
        if args.url == "-" or not sys.stdin.isatty():
            raise _Stop(1, "0 件のため追加しません。追加するには --yes を付けてください。")
        print("0 件でした。それでも追加しますか? [y/N]", end=" ", flush=True)
        if sys.stdin.readline().strip() != "y":
            raise _Stop(1, "追加しませんでした。")
    try:
        news_sources.add(conn, name=name, fetcher=args.fetcher, url=url,
                         added_by="user", now=_now(), enabled=True)
    except sqlite3.IntegrityError:
        conn.rollback()
        try:
            _duplicate_stop(conn, name, url)
        except _Stop as stop:
            raise stop from None
        raise _Stop(1, "既存の源と重複しています。") from None
    _write_activity(root, "news_source_added", name, args.fetcher)
    print(f"追加しました: {name} ({args.fetcher})")
    print(_REFLECT)
    if result.count == 0 and args.fetcher == "feed":
        print("`--fetcher web` を試せます。")
    return 0


def _set_enabled(args, root: Path, conn: sqlite3.Connection, enabled: bool) -> int:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT id, enabled FROM news_sources WHERE name=?", (args.name,)).fetchone()
        changed = row is not None and bool(row["enabled"]) != enabled
        if changed:
            news_sources.set_enabled(conn, row["id"], enabled, commit=False)
            if enabled:
                news_health.initialize_for_enable(conn, args.name, _now())
            conn.commit()
        else:
            conn.rollback()
    except BaseException:
        conn.rollback()
        raise
    if row is None:
        raise _Stop(1, f"ニュース源 {_plain(args.name)} はありません。")
    if not changed:
        print("既に有効です" if enabled else "既に無効です")
        return 0
    _write_activity(root, "news_source_enabled" if enabled else "news_source_disabled", args.name)
    print(f"{'有効' if enabled else '無効'}にしました: {_plain(args.name)}")
    print(_REFLECT)
    return 0


def _test(args, root: Path, conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT name, fetcher, url FROM news_sources WHERE name=?", (args.name,)).fetchone()
    if row is None:
        raise _Stop(1, f"ニュース源 {_plain(args.name)} はありません。")
    timeout = _timeout(root)
    wait = news_health.read_effective_wait(conn, row["name"], _now())
    if wait:
        until, cause = wait
        print(f"この源は {_utc_text(until)} まで待機中です ({_WAIT_LABELS[cause]}、収集器は取得しません)。")
    try:
        result = news_probe.probe(row["fetcher"], row["url"], row["name"], timeout_sec=timeout)
    except (news_probe.ProbeFailure, NewsInputError) as exc:
        reason = exc.reason if isinstance(exc, news_probe.ProbeFailure) else news_probe.REASON_OTHER
        raise _Stop(1, f"取得に失敗しました: {reason}") from None
    _print_result(result)
    return 0


def _run(args, root: Path) -> int:
    command = args.news_command
    add_input = None
    if command == "add":
        add_input = _add_input(args)
    elif command in {"enable", "disable", "test"}:
        _check_existing_name(args.name)
    conn = _open(root, write=command in _WRITE_COMMANDS)
    try:
        if command == "list":
            return _list(conn)
        if command == "add":
            return _add(args, root, conn, *add_input)
        if command == "test":
            return _test(args, root, conn)
        return _set_enabled(args, root, conn, command == "enable")
    finally:
        conn.close()


def dispatch_news(args, root: Path) -> int:
    """Run a parsed ``afx news`` command without initialising the root."""
    try:
        return _run(args, root)
    except _Stop as stop:
        print(stop.message, file=sys.stderr)
        return stop.rc
    except KeyboardInterrupt:
        print(_INTERRUPTED, file=sys.stderr)
        return 130
    except EOFError:
        print(_EMPTY_STDIN, file=sys.stderr)
        return 2
    except NewsInputError as exc:
        text = str(exc)
        print(text if text in _KNOWN_INPUT_ERRORS else _BAD_INPUT, file=sys.stderr)
        return 2
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        print(_DB_BUSY if "locked" in text or "busy" in text else _DB_FAILED, file=sys.stderr)
        return 1
    except sqlite3.Error:
        print(_DB_FAILED, file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - no exception data is safe at this boundary
        print(_UNEXPECTED, file=sys.stderr)
        return 1
