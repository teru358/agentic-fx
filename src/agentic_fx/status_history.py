"""`afx status` の評価用履歴ブロック (`evaluation_history`)。

主 pair の鮮度・充足・日別の欠損と、参照銘柄の 0 本の日、検査の対象範囲の無足日一覧を
表示する。履歴の取得・修復はしない。読めなければこのブロックだけ `unavailable` とし、
status 全体の表示は保つ。日時は利用者の表示 tz (`YYYY-MM-DD HH:MM:SS +09:00`)。
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agentic_fx.backtest import history_evidence, history_health as hh
from agentic_fx.backtest.dataset import literal_reference_coordinates
from agentic_fx.backtest.holdout import _normalize_now, in_sample_until
from agentic_fx.core.display_time import convert_or_raw

UNAVAILABLE_LINE = "evaluation history: unavailable"
_GITIGNORE_ENTRY = "config/history-no-bars.yaml"
_MAX_DAYS_SHOWN = 10


def _shown(value: datetime | None, tz: str) -> str:
    return "-" if value is None else convert_or_raw(value.isoformat(), tz)


def _hours(delta: timedelta | None) -> str:
    return "-" if delta is None else f"{delta.total_seconds() / 3600:.1f}h"


def _days(items: list[date]) -> str:
    if not items:
        return "none"
    shown = ", ".join(d.isoformat() for d in items[:_MAX_DAYS_SHOWN])
    more = len(items) - _MAX_DAYS_SHOWN
    return shown + (f" (+{more} more)" if more > 0 else "")


def _gitignored(root: Path) -> str:
    try:
        lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return "no"
    except (OSError, UnicodeDecodeError):
        return "unreadable"
    return "yes" if any(line.strip() in (_GITIGNORE_ENTRY, "/" + _GITIGNORE_ENTRY)
                        for line in lines) else "no"


def _no_bars_lines(root: Path, health: hh.HistoryHealth | None, tz: str,
                   load_error: str | None) -> list[str]:
    path = history_evidence.no_bars_path(root)
    example = path.with_name(path.name + ".example")
    head = (f"  no-bars list: path={path} file={'present' if path.is_file() else 'absent'} "
            f"gitignored={_gitignored(root)} example={'present' if example.is_file() else 'absent'}")
    if load_error is not None:
        return [head, f"  no-bars list: unavailable ({load_error})"]
    if health is None or health.holdout_start is None or health.holdout_end is None:
        return [head]
    scoped = history_evidence.scope_no_bars(
        health.applied_no_bars, dataset=health.dataset,
        pairs=[p.symbol for p in health.pairs], start=health.holdout_start,
        end=health.holdout_end)
    lines = [head, f"  no-bars applied: {len(scoped)} entries digest={history_evidence.scoped_digest(scoped)[:12]}"]
    for entry in scoped:
        lines.append(f"    - {entry.symbol} {entry.date_utc.isoformat()} by {entry.approved_by} "
                     f"at {_shown(entry.approved_at, tz)}: {entry.reason}")
    return lines


def _reference_symbols(conn: sqlite3.Connection, root: Path, settings: Any) -> list[str]:
    from agentic_fx.tools import plugin_loader
    plugins_dir = Path(root) / "plugins"
    if not plugins_dir.is_dir():
        return []
    inventory = plugin_loader.approved_plugins(conn, plugins_dir, settings=settings)
    symbols: dict[str, None] = {}
    for resolved in inventory.resolved.values():
        for symbol, _timeframe in literal_reference_coordinates(resolved):
            symbols[symbol] = None
    return list(symbols)


def _bar_days(conn: sqlite3.Connection, dataset, symbol: str, start: datetime,
              end: datetime) -> set[date]:
    rows = conn.execute(
        "SELECT DISTINCT substr(bar_time, 1, 10) FROM ohlcv_history WHERE symbol=? "
        "AND interval=? AND source=? AND bar_time >= ? AND bar_time < ?",
        (symbol, dataset.base_interval, dataset.source, start.isoformat(), end.isoformat()))
    return {date.fromisoformat(r[0]) for r in rows}


def _reference_lines(conn: sqlite3.Connection, root: Path, settings: Any,
                     health: hh.HistoryHealth) -> list[str]:
    """主 pair に足がある日なのに 0 本だった参照の日。疑いの表示で、採用停止には使わない。"""
    refs = _reference_symbols(conn, root, settings)
    primary = {p.symbol for p in health.pairs}
    refs = [r for r in refs if r not in primary]
    if not refs:
        return ["  references: none declared by deployed strategies"]
    start, end = health.holdout_start, health.holdout_end
    primary_days: set[date] = set()
    for pair in primary:
        primary_days |= _bar_days(conn, health.dataset, pair, start, end)
    lines = ["  references: days where a main pair has bars but the reference has none "
             "(suspicion only; not a stop condition)"]
    for symbol in refs:
        have = _bar_days(conn, health.dataset, symbol, start, end)
        missing = sorted(d for d in primary_days if d not in have and d < end.date())
        lines.append(f"    {symbol}: {_days(missing)}")
    return lines


def collect(root: Path, settings: Any, conn: sqlite3.Connection | None, tz: str,
            now: datetime) -> tuple[str, ...]:
    """ブロックの行。計算できなければ `(UNAVAILABLE_LINE, ...)`。送出しない。"""
    root = Path(root)
    try:
        if settings is None or conn is None:
            return (UNAVAILABLE_LINE, "  reason: settings or database cannot be read")
        dataset = settings.backtest.dataset()
        if dataset.unconfigured:
            return ("evaluation history: not configured (backtest.eval_source is none)",)
        grace = timedelta(seconds=settings.datafeed.closed_bar_grace_sec)
        path = history_evidence.no_bars_path(root)
        health = hh.evaluate_history(
            conn, dataset=dataset, pairs=list(settings.pairs), now=now,
            holdout_months=settings.backtest.holdout_months, grace=grace, no_bars_path=path)
        load_error = health.detail if (
            health.detail and health.detail.startswith("NoBarsConfigError")) else None
        verdict = "unavailable" if health.verdict == hh.HISTORY_UNAVAILABLE else health.verdict
        reasons = ",".join("unavailable" if r == hh.HISTORY_UNAVAILABLE else r
                           for r in health.reasons)
        lines = [f"evaluation history: {verdict}" + (f" ({reasons})" if len(health.reasons) > 1 else ""),
                 f"  dataset: {dataset.source} {dataset.base_interval}  checked at "
                 f"{_shown(health.checked_at, tz)}",
                 f"  rules: guard v{hh.GUARD_VERSION} calendar {hh.CALENDAR_VERSION} thresholds "
                 f"{hh.threshold_snapshot()}"]
        if health.holdout_start is not None:
            lines.append(f"  window: {_shown(health.holdout_start, tz)} .. "
                         f"{_shown(health.holdout_end, tz)}")
        if health.detail and load_error is None:
            lines.append(f"  detail: {health.detail}")
        for pair in health.pairs:
            lines.append(f"  pair {pair.symbol}: " + ",".join(
                "unavailable" if r == hh.HISTORY_UNAVAILABLE else r
                for r in pair.reasons or (hh.HISTORY_READY,)))
            stored = "unavailable" if pair.latest_stored is None else _shown(
                pair.latest_stored, tz)
            lines.append(f"    latest stored {stored}; latest confirmed "
                         f"{_shown(pair.latest_confirmed, tz)}; wall gap {_hours(pair.wall_gap)}; "
                         f"open-time gap {_hours(pair.open_gap)}")
            if pair.fill is not None:
                worst = ("none" if pair.worst_day is None else
                         f"{pair.worst_day[0].isoformat()} missing {pair.worst_day[1]} bars "
                         f"({pair.worst_day[1] * dataset.width.total_seconds() / 60:.0f} min)")
                lines.append(f"    holdout present {pair.market_present} / expected "
                             f"{pair.market_expected} = {float(pair.fill) * 100:.2f}%; "
                             f"worst day {worst}")
                lines.append("    days missing over 60 min: "
                             + _days([d for d, _m in pair.missing_days]))
        try:
            lines += _reference_lines(conn, root, settings, health) \
                if health.holdout_start is not None else []
        except Exception:  # noqa: BLE001 - one section must not hide the others
            lines.append("  references: unavailable")
        lines += _no_bars_lines(root, health, tz, load_error)
        lines.append("  next: import history while the market is open "
                     "(`afx history import --source mt5`), then `afx plugin reevaluate --all-deployed`")
        return tuple(lines)
    except Exception:  # noqa: BLE001 - the block alone becomes unavailable
        return (UNAVAILABLE_LINE,)
