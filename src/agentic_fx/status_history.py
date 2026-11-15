"""`afx status` の評価用履歴ブロック。

`collect` は人間専用の構造化結果 (`HistoryStatus`) を返し、`render` が表示 tz の文字列へ
変換する。判定 (verdict・閾値) は `history_health` の結果をそのまま運び、ここで再計算しない。
主 pair の鮮度・充足・日別の欠損、参照銘柄 (`datafeed.watch_symbols`) の 0 本の日、検査の
対象範囲の無足日一覧を扱う。履歴の取得・修復はしない。読めなければこのブロックだけ
固定理由 (`ReasonCode`) 付きの `unavailable` とし、例外の本文・型は出さない。
`history_sync` は同期の永続表 (run・系列状態・gap queue・衝突の件数) の観測で、現在の guard 判定から
同期の成否を推測しない。`eval_window` は評価区間の設計が充填する別スロットで、記録の読取が
無い間は `NOT_COLLECTED` のまま運ぶ (成功や空結果に偽装せず、同期から再生成もしない)。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any

from agentic_fx.backtest import history_evidence, history_health as hh
from agentic_fx.core.display_time import convert_or_raw
from agentic_fx.store import history_sync as sync_store

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
    SYNC_QUERY_FAILED = "sync_query_failed"


class NextAction(str, Enum):
    STOP_DAEMON_THEN_IMPORT = "stop_daemon_then_import"
    CHECK_INPUTS_AND_RERUN = "check_inputs_and_rerun"
    RESTART_TO_INTRODUCE_SYNC_SCHEMA = "restart_to_introduce_sync_schema"
    STOP_DAEMON_THEN_SYNC = "stop_daemon_then_sync"
    LIST_GAPS = "list_gaps"
    LIST_CONFLICTS = "list_conflicts"
    UPDATE_BRIDGE = "update_bridge"
    CHECK_COMPLETION_PENDING = "check_completion_pending"


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


class SyncState(str, Enum):
    OK = "ok"
    NEVER_RUN = "never_run"
    ATTENTION = "attention"
    NOT_INITIALIZED = "not_initialized"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"


# 同期の run が要対応を示す結果。running は実行中の観測で、要対応に数えない。
_RUN_OK = frozenset({"ok", "running"})
_FIXED_WORD = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
NOT_INITIALIZED_LINE = "history sync: not initialized"
SYNC_SCOPE_NOTE = "対象は holdout 内と直近。in-sample は作らない"
EXECUTOR_UNSUPPORTED_LINE = ("history_executor_url: 設定あり。3b (履歴専用の別 bridge) は未実装・未対応で、"
                             "取得先には使いません (値は表示しません)")


@dataclass(frozen=True)
class SyncRun:
    run_id: str | None
    request_id: str | None
    trigger: str | None
    status: str
    rc: int | None
    started_at: datetime | None
    finished_at: datetime | None
    range_start: datetime | None
    range_end: datetime | None
    requests: int
    inserted: int
    unchanged: int
    conflicted_new: int
    conflicted_known: int
    failure_code: str | None
    deferred_reason: str | None


@dataclass(frozen=True)
class SyncSeries:
    symbol: str
    last_processed_e: datetime | None
    attempts: int
    next_retry_at: datetime | None
    last_error: str | None
    last_health: str | None


@dataclass(frozen=True)
class SuspectedDay:
    symbol: str
    day: str
    missing_count: int | None
    detail: sync_store.GapDetail


@dataclass(frozen=True)
class HistorySync:
    """同期の永続表の観測。人間専用の構造化結果で、agent の tool・prompt には出さない。"""
    state: SyncState
    observed_at: datetime
    reason_code: ReasonCode | None = None
    source: str | None = None
    base_interval: str | None = None
    auto_enabled: bool | None = None
    lookback_days: int | None = None
    effective_enabled: bool = False
    effective_reason: str | None = None
    measurement: str = "not_measured"
    worker: str = "not_running"
    executor_unsupported: bool = False
    last_run: SyncRun | None = None
    last_success: SyncRun | None = None
    series: tuple[SyncSeries, ...] = ()
    open_gaps: tuple[tuple[str, int], ...] = ()
    expired_days: int = 0
    suspected: tuple[SuspectedDay, ...] = ()
    open_conflicts: int = 0
    next_actions: tuple[NextAction, ...] = ()


@dataclass(frozen=True)
class HistoryStatus:
    observed_at: datetime
    evaluation_history: EvaluationHistory
    history_sync: HistorySync | str = NOT_COLLECTED
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


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _word(value: Any) -> str | None:
    """表に保存された固定語だけを通す。自由な文字列は出さない。"""
    return value if isinstance(value, str) and _FIXED_WORD.match(value) else None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _sync_run(row: sqlite3.Row) -> SyncRun:
    return SyncRun(
        _word(row["id"]), _word(row["request_id"]), _word(row["trigger"]),
        _word(row["status"]) or "unrecognized", row["rc"] if isinstance(row["rc"], int) else None,
        _utc(row["started_at"]), _utc(row["finished_at"]), _utc(row["b_utc"]), _utc(row["e_utc"]),
        _count(row["requests"]), _count(row["inserted"]), _count(row["unchanged"]),
        _count(row["conflicted_new"]), _count(row["conflicted_known"]),
        _word(row["failure_code"]), _word(row["deferred_reason"]))


_RUN_COLUMNS = ("id, request_id, trigger, status, rc, started_at, finished_at, b_utc, e_utc, "
                "requests, inserted, unchanged, conflicted_new, conflicted_known, "
                "failure_code, deferred_reason")


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _sync_next(sync: HistorySync) -> tuple[NextAction, ...]:
    actions: list[NextAction] = []
    primary_open = dict(sync.open_gaps)
    run = sync.last_run
    if run and run.status in ("failed", "partial", "interrupted"):
        if run.failure_code == "history_executor_unsupported":
            actions.append(NextAction.UPDATE_BRIDGE)
        elif run.failure_code == "history_completion_pending":
            actions.append(NextAction.CHECK_COMPLETION_PENDING)
        else:
            actions.append(NextAction.STOP_DAEMON_THEN_SYNC)
    if (primary_open.get("primary_exact") or primary_open.get("unprocessed")) \
            and NextAction.STOP_DAEMON_THEN_SYNC not in actions \
            and NextAction.CHECK_COMPLETION_PENDING not in actions:
        actions.append(NextAction.STOP_DAEMON_THEN_SYNC)
    if primary_open.get("primary_exact") or primary_open.get("unprocessed") \
            or primary_open.get("reference_suspected"):
        actions.append(NextAction.LIST_GAPS)
    if sync.open_conflicts or (run and run.status in ("completed_with_conflicts", "conflict_circuit_open")):
        actions.append(NextAction.LIST_CONFLICTS)
    return tuple(actions)


def _collect_sync(conn: sqlite3.Connection, settings: Any, dataset: Any,
                  now: datetime) -> HistorySync:
    """同期の永続表を最小列だけ読む。読取専用接続で、表が無ければ not_initialized。"""
    source, interval = dataset.source, dataset.base_interval
    try:
        executor = getattr(settings.datafeed.mt5, "history_executor_url", None) is not None
    except Exception:  # noqa: BLE001 - the flag alone is lost, never the block
        executor = False
    try:
        cfg = settings.backtest.history_sync
        auto, lookback = bool(cfg.auto_enabled), int(cfg.lookback_days)
    except Exception:  # noqa: BLE001
        auto, lookback = None, None
    common = dict(observed_at=now, source=source, base_interval=interval, auto_enabled=auto,
                  lookback_days=lookback, executor_unsupported=executor)
    if source != "mt5":
        return HistorySync(SyncState.NOT_APPLICABLE, **common, effective_reason="source_not_mt5")
    # 自動の取得は worker (段 3) が担う。worker も計測も受入も無い間は常に実効無効。
    reason = "auto_disabled" if auto is False else "measurement_not_accepted"
    try:
        present = _tables(conn)
        needed = {"history_sync_runs", "history_sync_series_state", "history_sync_gaps"}
        if not needed <= present:
            return HistorySync(SyncState.NOT_INITIALIZED, **common, effective_reason=reason,
                               next_actions=(NextAction.RESTART_TO_INTRODUCE_SYNC_SCHEMA,))
        where = "WHERE source=? AND interval=?"
        params = (source, interval)
        last = conn.execute(f"SELECT {_RUN_COLUMNS} FROM history_sync_runs {where} "
                            "ORDER BY started_at DESC, id DESC LIMIT 1", params).fetchone()
        success = conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM history_sync_runs {where} AND status='ok' "
            "ORDER BY started_at DESC, id DESC LIMIT 1", params).fetchone()
        series = tuple(
            SyncSeries(r["symbol"], _utc(r["last_processed_e"]), _count(r["attempts"]),
                       _utc(r["next_retry_at"]), _word(r["last_error"]), _word(r["last_health"]))
            for r in conn.execute(
                "SELECT symbol, last_processed_e, attempts, next_retry_at, last_error, last_health "
                f"FROM history_sync_series_state {where} AND symbol <> ? ORDER BY symbol",
                (*params, sync_store.CONTROL_SYMBOL)))
        gap_counts = {k: 0 for k in sync_store.GAP_KINDS}
        for r in conn.execute(f"SELECT kind, COUNT(*) FROM history_sync_gaps {where} "
                              "AND state='open' GROUP BY kind", params):
            if r[0] in gap_counts:
                gap_counts[r[0]] = _count(r[1])
        expired = conn.execute(
            f"SELECT COUNT(DISTINCT day_utc) FROM history_sync_gaps {where} AND state='expired'",
            params).fetchone()[0]
        suspected = tuple(
            SuspectedDay(r["symbol"], r["day_utc"], r["missing_count"],
                         sync_store.read_gap_detail(r["detail_json"]))
            for r in conn.execute(
                "SELECT symbol, day_utc, missing_count, detail_json FROM history_sync_gaps "
                f"{where} AND state='open' AND kind='reference_suspected' "
                "ORDER BY symbol, day_utc", params))
        conflicts = 0
        if "history_conflicts" in present:
            conflicts = _count(conn.execute(
                f"SELECT COUNT(*) FROM history_conflicts {where} AND state='open'",
                params).fetchone()[0])
    except Exception:  # noqa: BLE001 - this block alone becomes unavailable
        return HistorySync(SyncState.UNAVAILABLE, **common, reason_code=ReasonCode.SYNC_QUERY_FAILED,
                           effective_reason=reason)
    last_run = None if last is None else _sync_run(last)
    attention = last_run is not None and last_run.status not in _RUN_OK
    state = (SyncState.NEVER_RUN if last_run is None
             else SyncState.ATTENTION if attention else SyncState.OK)
    sync = HistorySync(
        state, **common, effective_reason=reason, last_run=last_run,
        last_success=None if success is None else _sync_run(success), series=series,
        open_gaps=tuple(gap_counts.items()), expired_days=_count(expired), suspected=suspected,
        open_conflicts=conflicts)
    return replace(sync, next_actions=_sync_next(sync))


def unavailable(now: datetime, reason: ReasonCode) -> HistoryStatus:
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
            return unavailable(now, ReasonCode.SETTINGS_UNAVAILABLE)
        dataset = settings.backtest.dataset()
        if dataset.unconfigured:
            return HistoryStatus(now, EvaluationHistory(
                State.NOT_CONFIGURED, None, source=dataset.source,
                base_interval=dataset.base_interval),
                HistorySync(SyncState.NOT_APPLICABLE, now, source=dataset.source,
                            base_interval=dataset.base_interval,
                            effective_reason="source_not_mt5",
                            executor_unsupported=getattr(settings.datafeed.mt5, "history_executor_url", None) is not None))
        if conn is None:
            return unavailable(now, unavailable_reason or ReasonCode.DATABASE_UNAVAILABLE)
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
        try:
            sync = _collect_sync(conn, settings, dataset, now)
        except Exception:  # noqa: BLE001 - one section must not hide the others
            sync = HistorySync(SyncState.UNAVAILABLE, now, reason_code=ReasonCode.SYNC_QUERY_FAILED,
                               source=dataset.source, base_interval=dataset.base_interval)
        return HistoryStatus(now, EvaluationHistory(
            state, ReasonCode.HISTORY_CHECK_UNAVAILABLE if state is State.UNAVAILABLE else None,
            reasons, dataset.source, dataset.base_interval, health.checked_at,
            health.holdout_start, health.holdout_end, hh.GUARD_VERSION, hh.CALENDAR_VERSION,
            hh.threshold_snapshot(), pairs, dataset.width.total_seconds() / 60,
            _no_bars_info(root, health, load_error), references, _next_actions(state)), sync)
    except Exception:  # noqa: BLE001 - the block alone becomes unavailable
        return unavailable(now, ReasonCode.HISTORY_CHECK_UNAVAILABLE)


_NEXT_TEXT = {
    NextAction.STOP_DAEMON_THEN_IMPORT:
        "不足 pair・範囲を確認し、daemon を止めて `afx history import --source {source}` "
        "(初回・holdout より古い履歴) か `afx history sync` (holdout 内の更新・穴修復) "
        "を実行、その後 `afx status`",
    NextAction.CHECK_INPUTS_AND_RERUN:
        "上の reason に沿って設定・DB・no-bars 一覧を確認し、`afx status` を再実行",
    NextAction.RESTART_TO_INTRODUCE_SYNC_SCHEMA:
        "daemon を再起動して同期の schema を導入し、`afx status` を再実行",
    NextAction.STOP_DAEMON_THEN_SYNC:
        "daemon を止めて `afx history sync` を実行 (停止中は kill switch の監視も止まります。"
        "自動同期はまだありません)、その後 `afx status`",
    NextAction.LIST_GAPS:
        "`afx history gaps list` で範囲を確認 (自動修復の予定はありません)",
    NextAction.UPDATE_BRIDGE:
        "bridge を更新し、前提成立後に daemon の自動自己計測を待つ (取得は停止中)",
    NextAction.CHECK_COMPLETION_PENDING:
        "完了証拠を確認できるまで取得を停止し、bridge の保守復旧手順を確認",
    NextAction.LIST_CONFLICTS:
        "`afx history conflicts list` / `show <id>` で確認 (置換には daemon の停止が必要)",
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


def _sync_run_line(label: str, run: SyncRun | None, tz: str) -> str:
    if run is None:
        return f"  {label}: none"
    span = f"{_shown(run.range_start, tz)} .. {_shown(run.range_end, tz)}"
    fixed = [f"status={run.status}", f"rc={'-' if run.rc is None else run.rc}"]
    for key, value in (("run", run.run_id), ("request", run.request_id),
                       ("trigger", run.trigger), ("error", run.failure_code),
                       ("deferred", run.deferred_reason)):
        if value:
            fixed.append(f"{key}={value}")
    return (f"  {label}: {' '.join(fixed)} started {_shown(run.started_at, tz)} "
            f"finished {_shown(run.finished_at, tz)} range {span} requests={run.requests} "
            f"inserted={run.inserted} unchanged={run.unchanged} "
            f"conflicted={run.conflicted_new}+{run.conflicted_known}")


def _suspect_text(item: SuspectedDay) -> str:
    detail = sync_store.gap_detail_text(item.detail)
    missing = "" if item.missing_count is None else f" bars={item.missing_count}"
    return f"    {item.symbol} {item.day}{missing}" + (f" {detail}" if detail else "")


def render_sync(sync: HistorySync | str, tz: str) -> tuple[str, ...]:
    """同期の観測を並べる。固定語と件数・時刻だけで、自由な文字列・URL は出さない。"""
    if not isinstance(sync, HistorySync):
        return (f"history sync: {NOT_COLLECTED}",)
    if sync.state is SyncState.NOT_INITIALIZED:
        lines = [NOT_INITIALIZED_LINE]
    elif sync.state is SyncState.NOT_APPLICABLE:
        lines = [f"history sync: not_applicable ({sync.effective_reason})"]
    elif sync.state is SyncState.UNAVAILABLE:
        lines = ["history sync: unavailable", f"  reason: {sync.reason_code.value}"]
    else:
        lines = [f"history sync: {sync.state.value}"]
    if sync.state not in (SyncState.NOT_APPLICABLE, SyncState.NOT_INITIALIZED,
                          SyncState.UNAVAILABLE):
        lines.append(f"  {SYNC_SCOPE_NOTE}")
    if sync.state is not SyncState.NOT_APPLICABLE:
        cfg = ("unknown" if sync.auto_enabled is None else
               f"auto_enabled={'true' if sync.auto_enabled else 'false'} "
               f"lookback_days={sync.lookback_days}")
        lines.append(f"  config: {cfg}; effective: disabled ({sync.effective_reason}); "
                     f"self-measurement: {sync.measurement}; worker: {sync.worker}")
    if sync.state in (SyncState.OK, SyncState.NEVER_RUN, SyncState.ATTENTION):
        lines.append(_sync_run_line("last run", sync.last_run, tz))
        lines.append(_sync_run_line("last success", sync.last_success, tz))
        for item in sync.series:
            retry = "-" if item.next_retry_at is None else _shown(item.next_retry_at, tz)
            cursor = ("uninitialized" if item.last_processed_e is None
                      else _shown(item.last_processed_e, tz))
            lines.append(f"  series {item.symbol}: cursor {cursor} attempts={item.attempts} "
                         f"next retry {retry} error={item.last_error or '-'} "
                         f"health={item.last_health or '-'}")
        gaps = " ".join(f"{k}={n}" for k, n in sync.open_gaps)
        lines.append(f"  open gaps: {gaps}")
        if sync.expired_days:
            lines.append(f"  expired: holdout 外、自動修復なし {sync.expired_days} 日")
        lines.append(f"  open conflicts: {sync.open_conflicts}")
        if sync.suspected:
            lines.append("  reference_suspected (商品休場を含む疑い。採用可能とは解釈しない):")
            lines += [_suspect_text(item) for item in sync.suspected[:_MAX_DAYS_SHOWN]]
            more = len(sync.suspected) - _MAX_DAYS_SHOWN
            if more > 0:
                lines.append(f"    (+{more} more)")
        lines.append(f"  observed at {_shown(sync.observed_at, tz)}")
    if sync.executor_unsupported:
        lines.append(f"  {EXECUTOR_UNSUPPORTED_LINE}")
    lines += [f"  next: {_NEXT_TEXT[a]}" for a in sync.next_actions]
    return tuple(lines)


def render_eval_window(value: Any) -> tuple[str, ...]:
    """評価区間の最後の run の記録。記録の読取が無い間は未収集のまま示し、同期からは再生成しない。"""
    if value == NOT_COLLECTED:
        return (f"eval_window: {NOT_COLLECTED}",)
    return ()


def render(status: HistoryStatus, tz: str) -> tuple[str, ...]:
    """表示 tz の文字列行。構造化結果の値を並べるだけで判定はしない。"""
    h = status.evaluation_history
    if h.state is State.NOT_CONFIGURED:
        return ("evaluation history: not configured (backtest.eval_source is none)",
                *(render_sync(status.history_sync, tz) if isinstance(status.history_sync, HistorySync)
                  and status.history_sync.executor_unsupported else ()))
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
    lines += render_sync(status.history_sync, tz)
    lines += render_eval_window(status.eval_window)
    return tuple(lines)
