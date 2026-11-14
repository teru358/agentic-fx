"""`afx status` の評価用履歴ブロック。

`collect` は人間専用の構造化結果 (`HistoryStatus`) を返し、`render` が表示 tz の文字列へ
変換する。判定 (verdict・閾値) は `history_health` の結果をそのまま運び、ここで再計算しない。
主 pair の鮮度・充足・日別の欠損、参照銘柄 (`datafeed.watch_symbols`) の 0 本の日、検査の
対象範囲の無足日一覧を扱う。履歴の取得・修復はしない。読めなければこのブロックだけ
固定理由 (`ReasonCode`) 付きの `unavailable` とし、例外の本文・型は出さない。
`history_sync` と `eval_window` は同期・評価区間の設計が充填する別スロットで、
本件では `NOT_COLLECTED` のまま運ぶ (成功や空結果に偽装しない)。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any

from agentic_fx.backtest import history_evidence, history_health as hh
from agentic_fx.core.display_time import convert_or_raw

UNAVAILABLE_LINE = "evaluation history: unavailable"
NOT_COLLECTED = "not_collected"
REFERENCES_HEADING = "holdout 区間の 0 本日 (評価に使えるかは eval_window の表示を見る)"
NO_REFERENCE_LINE = "no reference watch symbols"
REFERENCE_NOTE = "商品休場を含む疑い。評価可否は eval_window 参照"
_GITIGNORE_ENTRY = "config/history-no-bars.yaml"
_MAX_DAYS_SHOWN = 10


class State(str, Enum):
    READY = hh.HISTORY_READY
    STALE = "stale"
    GAP = "gap"
    UNAVAILABLE = "unavailable"
    NOT_CONFIGURED = "not_configured"


_VERDICT_STATE = {hh.HISTORY_READY: State.READY, hh.HISTORY_STALE: State.STALE,
                  hh.HISTORY_GAP: State.GAP, hh.HISTORY_UNAVAILABLE: State.UNAVAILABLE}
# 表示の語は従来どおり guard の判定語 (history_stale / history_gap)。DTO の値とは分ける。
_STATE_SHOWN = {State.STALE: hh.HISTORY_STALE, State.GAP: hh.HISTORY_GAP}


class ReasonCode(str, Enum):
    SETTINGS_UNAVAILABLE = "settings_unavailable"
    DATABASE_UNAVAILABLE = "database_unavailable"
    HISTORY_CHECK_UNAVAILABLE = "history_check_unavailable"
    REFERENCE_QUERY_FAILED = "reference_query_failed"


class NextAction(str, Enum):
    STOP_DAEMON_THEN_IMPORT = "stop_daemon_then_import"
    CHECK_INPUTS_AND_RERUN = "check_inputs_and_rerun"


@dataclass(frozen=True)
class PairDiagnosis:
    symbol: str
    reasons: tuple[str, ...]
    latest_stored: datetime | None
    latest_confirmed: datetime | None
    wall_gap: timedelta | None
    open_gap: timedelta | None
    present: int
    expected: int
    fill: Fraction | None
    worst_day: tuple[date, int] | None
    missing_days: tuple[date, ...]


@dataclass(frozen=True)
class NoBarsInfo:
    path: Path
    file_present: bool
    gitignored: str
    example_present: bool
    load_error: bool
    applied: tuple[Any, ...] = ()
    digest: str = ""


@dataclass(frozen=True)
class ReferenceSymbol:
    symbol: str
    unavailable: bool
    zero_bar_dates: tuple[date, ...] = ()
    reason_code: ReasonCode | None = None


@dataclass(frozen=True)
class References:
    """参照の 0 本日の観測。疑いの表示だけで、評価可否・ready を持たない。"""
    kind: str
    observed_at: datetime
    start: datetime | None
    end: datetime | None
    unavailable: bool
    reason_code: ReasonCode | None
    symbols: tuple[ReferenceSymbol, ...] = ()


@dataclass(frozen=True)
class EvaluationHistory:
    state: State
    reason_code: ReasonCode | None
    reasons: tuple[str, ...] = ()
    source: str | None = None
    base_interval: str | None = None
    checked_at: datetime | None = None
    holdout_start: datetime | None = None
    holdout_end: datetime | None = None
    guard_version: Any = None
    calendar_version: Any = None
    thresholds: dict = field(default_factory=dict)
    pairs: tuple[PairDiagnosis, ...] = ()
    width_minutes: float = 0.0
    no_bars: NoBarsInfo | None = None
    references: References | None = None
    next_actions: tuple[NextAction, ...] = ()


@dataclass(frozen=True)
class HistoryStatus:
    observed_at: datetime
    evaluation_history: EvaluationHistory
    history_sync: Any = NOT_COLLECTED
    eval_window: Any = NOT_COLLECTED


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


def _no_bars_info(root: Path, health: hh.HistoryHealth | None, load_error: bool) -> NoBarsInfo:
    path = history_evidence.no_bars_path(root)
    example = path.with_name(path.name + ".example")
    info = NoBarsInfo(path, path.is_file(), _gitignored(root), example.is_file(), load_error)
    if load_error or health is None or health.holdout_start is None or health.holdout_end is None:
        return info
    scoped = history_evidence.scope_no_bars(
        health.applied_no_bars, dataset=health.dataset,
        pairs=[p.symbol for p in health.pairs], start=health.holdout_start,
        end=health.holdout_end)
    return NoBarsInfo(path, info.file_present, info.gitignored, info.example_present, False,
                      tuple(scoped), history_evidence.scoped_digest(scoped))


def _no_bars_lines(info: NoBarsInfo, tz: str) -> list[str]:
    head = (f"  no-bars list: path={info.path} file={'present' if info.file_present else 'absent'} "
            f"gitignored={info.gitignored} example={'present' if info.example_present else 'absent'}")
    if info.load_error:
        return [head, "  no-bars list: unavailable"]
    if not info.digest:
        return [head]
    lines = [head, f"  no-bars applied: {len(info.applied)} entries digest={info.digest[:12]}"]
    for entry in info.applied:
        lines.append(f"    - {entry.symbol} {entry.date_utc.isoformat()} by {entry.approved_by} "
                     f"at {_shown(entry.approved_at, tz)}: {entry.reason}")
    return lines


def _reference_symbols(settings: Any, primary: set[str]) -> list[str]:
    """設定順に重複を除いた `watch_symbols` から主 pair を除く。plugin は探索しない。"""
    watch = getattr(getattr(settings, "datafeed", None), "watch_symbols", None) or []
    return [s for s in dict.fromkeys(watch) if s not in primary]


def _bar_days(conn: sqlite3.Connection, dataset, symbol: str, start: datetime,
              end: datetime) -> set[date]:
    rows = conn.execute(
        "SELECT DISTINCT substr(bar_time, 1, 10) FROM ohlcv_history WHERE symbol=? "
        "AND interval=? AND source=? AND bar_time >= ? AND bar_time < ?",
        (symbol, dataset.base_interval, dataset.source, start.isoformat(), end.isoformat()))
    return {date.fromisoformat(r[0]) for r in rows}


def _references(conn: sqlite3.Connection, settings: Any, health: hh.HistoryHealth,
                observed_at: datetime) -> References:
    """主 pair に足がある完了日なのに 0 本だった参照の日。疑いの表示で、採用停止には使わない。"""
    kind = "holdout_zero_bar_days"
    start, end = health.holdout_start, health.holdout_end
    primary = {p.symbol for p in health.pairs}
    refs = _reference_symbols(settings, primary)
    if not refs:
        return References(kind, observed_at, start, end, False, None)
    try:
        primary_days: set[date] = set()
        for pair in sorted(primary):
            primary_days |= _bar_days(conn, health.dataset, pair, start, end)
    except Exception:  # noqa: BLE001 - the comparison source failed: the whole section is unavailable
        return References(kind, observed_at, start, end, True, ReasonCode.REFERENCE_QUERY_FAILED)
    symbols: list[ReferenceSymbol] = []
    for symbol in refs:
        try:
            have = _bar_days(conn, health.dataset, symbol, start, end)
            missing = tuple(sorted(d for d in primary_days if d not in have and d < end.date()))
            symbols.append(ReferenceSymbol(symbol, False, missing))
        except Exception:  # noqa: BLE001 - one symbol must not hide the others
            symbols.append(ReferenceSymbol(symbol, True, (), ReasonCode.REFERENCE_QUERY_FAILED))
    return References(kind, observed_at, start, end, False, None, tuple(symbols))


def _unavailable(now: datetime, reason: ReasonCode) -> HistoryStatus:
    return HistoryStatus(now, EvaluationHistory(State.UNAVAILABLE, reason))


def _next_actions(state: State) -> tuple[NextAction, ...]:
    if state in (State.STALE, State.GAP):
        return (NextAction.STOP_DAEMON_THEN_IMPORT,)
    if state is State.UNAVAILABLE:
        return (NextAction.CHECK_INPUTS_AND_RERUN,)
    return ()


def collect(root: Path, settings: Any, conn: sqlite3.Connection | None, tz: str,
            now: datetime, *, unavailable_reason: ReasonCode | None = None) -> HistoryStatus:
    """評価履歴の構造化結果。計算できなければ固定理由つきの unavailable。送出しない。

    `unavailable_reason` は呼出側が既に分かっている理由 (例: DB を開けなかった) を渡す。
    `tz` は表示専用で、ここでは使わない (renderer が換算する)。"""
    root = Path(root)
    try:
        if settings is None:
            return _unavailable(now, ReasonCode.SETTINGS_UNAVAILABLE)
        dataset = settings.backtest.dataset()
        if dataset.unconfigured:
            return HistoryStatus(now, EvaluationHistory(
                State.NOT_CONFIGURED, None, source=dataset.source,
                base_interval=dataset.base_interval))
        if conn is None:
            return _unavailable(now, unavailable_reason or ReasonCode.DATABASE_UNAVAILABLE)
        grace = timedelta(seconds=settings.datafeed.closed_bar_grace_sec)
        path = history_evidence.no_bars_path(root)
        health = hh.evaluate_history(
            conn, dataset=dataset, pairs=list(settings.pairs), now=now,
            holdout_months=settings.backtest.holdout_months, grace=grace, no_bars_path=path)
        load_error = bool(health.detail and health.detail.startswith("NoBarsConfigError"))
        state = _VERDICT_STATE[health.verdict]
        reasons = tuple("unavailable" if r == hh.HISTORY_UNAVAILABLE else r for r in health.reasons)
        pairs = tuple(PairDiagnosis(
            p.symbol, tuple("unavailable" if r == hh.HISTORY_UNAVAILABLE else r
                            for r in p.reasons or (hh.HISTORY_READY,)),
            p.latest_stored, p.latest_confirmed, p.wall_gap, p.open_gap,
            p.market_present, p.market_expected, p.fill, p.worst_day,
            tuple(d for d, _m in p.missing_days)) for p in health.pairs)
        references = None
        if health.holdout_start is not None:
            try:
                references = _references(conn, settings, health, now)
            except Exception:  # noqa: BLE001 - one section must not hide the others
                references = References("holdout_zero_bar_days", now, health.holdout_start,
                                        health.holdout_end, True, ReasonCode.REFERENCE_QUERY_FAILED)
        return HistoryStatus(now, EvaluationHistory(
            state, ReasonCode.HISTORY_CHECK_UNAVAILABLE if state is State.UNAVAILABLE else None,
            reasons, dataset.source, dataset.base_interval, health.checked_at,
            health.holdout_start, health.holdout_end, hh.GUARD_VERSION, hh.CALENDAR_VERSION,
            hh.threshold_snapshot(), pairs, dataset.width.total_seconds() / 60,
            _no_bars_info(root, health, load_error), references, _next_actions(state)))
    except Exception:  # noqa: BLE001 - the block alone becomes unavailable
        return _unavailable(now, ReasonCode.HISTORY_CHECK_UNAVAILABLE)


_NEXT_TEXT = {
    NextAction.STOP_DAEMON_THEN_IMPORT:
        "不足 pair・範囲を確認し、daemon を止めて `afx history import --source {source}` "
        "を実行、その後 `afx status`",
    NextAction.CHECK_INPUTS_AND_RERUN:
        "上の reason に沿って設定・DB・no-bars 一覧を確認し、`afx status` を再実行",
}


def _reference_lines(refs: References, tz: str) -> list[str]:
    if refs.unavailable:
        return [f"  references: unavailable ({refs.reason_code.value})"]
    if not refs.symbols:
        return [f"  references: {NO_REFERENCE_LINE}"]
    lines = [f"  references: {REFERENCES_HEADING}"]
    suspected = False
    for item in refs.symbols:
        if item.unavailable:
            lines.append(f"    {item.symbol}: unavailable ({item.reason_code.value})")
            continue
        suspected = suspected or bool(item.zero_bar_dates)
        lines.append(f"    {item.symbol}: {_days(list(item.zero_bar_dates))}")
    if suspected:
        lines.append(f"  note: {REFERENCE_NOTE}")
    return lines


def render(status: HistoryStatus, tz: str) -> tuple[str, ...]:
    """表示 tz の文字列行。構造化結果の値を並べるだけで判定はしない。"""
    h = status.evaluation_history
    if h.state is State.NOT_CONFIGURED:
        return ("evaluation history: not configured (backtest.eval_source is none)",)
    if h.state is State.UNAVAILABLE and h.source is None:
        return (UNAVAILABLE_LINE, f"  reason: {h.reason_code.value}",
                *(f"  next: {_NEXT_TEXT[a]}" for a in h.next_actions or _next_actions(h.state)))
    lines = [f"evaluation history: {_STATE_SHOWN.get(h.state, h.state.value)}" + (f" ({','.join(h.reasons)})" if len(h.reasons) > 1 else ""),
             f"  dataset: {h.source} {h.base_interval}  checked at {_shown(h.checked_at, tz)}",
             f"  rules: guard v{h.guard_version} calendar {h.calendar_version} thresholds "
             f"{h.thresholds}"]
    if h.reason_code is not None:
        lines.append(f"  reason: {h.reason_code.value}")
    if h.holdout_start is not None:
        lines.append(f"  window: {_shown(h.holdout_start, tz)} .. {_shown(h.holdout_end, tz)}")
    for pair in h.pairs:
        lines.append(f"  pair {pair.symbol}: " + ",".join(pair.reasons))
        stored = "unavailable" if pair.latest_stored is None else _shown(pair.latest_stored, tz)
        lines.append(f"    latest stored {stored}; latest confirmed "
                     f"{_shown(pair.latest_confirmed, tz)}; wall gap {_hours(pair.wall_gap)}; "
                     f"open-time gap {_hours(pair.open_gap)}")
        if pair.fill is not None:
            worst = ("none" if pair.worst_day is None else
                     f"{pair.worst_day[0].isoformat()} missing {pair.worst_day[1]} bars "
                     f"({pair.worst_day[1] * h.width_minutes:.0f} min)")
            lines.append(f"    holdout present {pair.present} / expected "
                         f"{pair.expected} = {float(pair.fill) * 100:.2f}%; worst day {worst}")
            lines.append("    days missing over 60 min: " + _days(list(pair.missing_days)))
    if h.references is not None:
        lines += _reference_lines(h.references, tz)
    if h.no_bars is not None:
        lines += _no_bars_lines(h.no_bars, tz)
    lines += [f"  next: {_NEXT_TEXT[a].format(source=h.source)}" for a in h.next_actions]
    return tuple(lines)
