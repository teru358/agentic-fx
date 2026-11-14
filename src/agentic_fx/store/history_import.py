"""履歴取込の共通の流れ: 系列ごとに全取得 → 事前遮断 → batch 確定。

MT5 と Dukascopy は同じこの流れに乗る。一操作・一系列 (source, interval, symbol) の
全要求窓を取得・検証し終えてから (その間は DB の lock も lease も持たない)、
履歴 mutation lease を取り、分類 → 遮断判定 → 短い batch での確定を行う。
取得や検証に失敗した系列は 0 件で、他の系列は続ける。結果は系列別に持ち、操作全体の
rc は `summarize` が決める。
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from contextlib import ExitStack
from typing import Callable

from agentic_fx.store import history_conflicts, ohlcv
from agentic_fx.store.history_lease import HistoryLeaseBusy, history_mutation_lease

# 1 回の取り込みでメモリに溜める行数の上限 (取得・検証済みの行は書込み前にすべて保持する)。
# 1 行 (9 要素の tuple、float 5 個、ISO 文字列) は約 0.4 KB なので、100 万行で約 0.4 GB。
MAX_IMPORT_ROWS = 1_000_000

# stage の寿命。取得が終わって stage ができた時点から数える (取得の長さは各 source の要求予算が抑える)。
STAGE_TTL_SEC = 600.0
BUSY_BUDGET_SEC = 0.25
BUSY_RETRY_SLEEP_SEC = 0.025

RC_OK, RC_FAILURE, RC_INVALID, RC_CONFLICTS, RC_CIRCUIT = 0, 1, 2, 3, 4


class ImportTooLargeError(ValueError):
    """The requested import would hold more rows than MAX_IMPORT_ROWS."""


class SeriesFetchError(Exception):
    """取得・検証の失敗。code は固定語、detail は人間向けの安全な説明。"""

    def __init__(self, code: str, detail: str = "", error_type: str | None = None):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.error_type = error_type


@dataclass(frozen=True)
class SeriesSpec:
    source: str
    interval: str
    symbol: str
    requested_start: datetime
    requested_end: datetime
    effective_end: datetime
    fetch: Callable[[], list[tuple]]


@dataclass(frozen=True)
class ConflictLine:
    id: int | None
    state: str | None
    known: bool
    bar_time: str
    epoch: str
    existing: tuple
    incoming: tuple


@dataclass(frozen=True)
class SeriesOutcome:
    source: str
    interval: str
    symbol: str
    status: str            # ok | completed_with_conflicts | conflict_circuit_open | partial | failed
    requested_start: str
    requested_end: str
    effective_end: str
    operation_id: str
    inserted: int = 0
    unchanged: int = 0
    conflicted_new: int = 0
    conflicted_known: int = 0
    last_committed_bar_time: str | None = None
    resume_from: str | None = None
    failure: str | None = None
    error_type: str | None = None
    detail: str = ""
    n_overlap: int = 0
    n_test: int = 0
    conflict_lines: tuple[ConflictLine, ...] = ()
    generation: int = 1

    @property
    def conflicted(self) -> int:
        return self.conflicted_new + self.conflicted_known

    @property
    def confirmed(self) -> bool:
        return self.status in ("ok", "completed_with_conflicts") or (
            self.status == "partial" and self.last_committed_bar_time is not None)

    @property
    def technical_failure(self) -> bool:
        return self.status in ("partial", "failed")


@dataclass(frozen=True)
class ImportOutcome:
    series: tuple[SeriesOutcome, ...]
    result: str
    rc: int

    @property
    def inserted(self) -> int:
        return sum(s.inserted for s in self.series)

    @property
    def unchanged(self) -> int:
        return sum(s.unchanged for s in self.series)

    @property
    def conflicted(self) -> int:
        return sum(s.conflicted for s in self.series)


def summarize(series: list[SeriesOutcome] | tuple[SeriesOutcome, ...]) -> ImportOutcome:
    """系列結果から全体の結果を決める。優先順位は 技術失敗 > 遮断 > 衝突 > ok。"""
    series = tuple(series)
    if any(s.technical_failure for s in series):
        confirmed = any(s.confirmed for s in series)
        return ImportOutcome(series, "partial" if confirmed else "failed", RC_FAILURE)
    if any(s.status == "conflict_circuit_open" for s in series):
        return ImportOutcome(series, "conflict_circuit_open", RC_CIRCUIT)
    if any(s.status == "completed_with_conflicts" for s in series):
        return ImportOutcome(series, "completed_with_conflicts", RC_CONFLICTS)
    return ImportOutcome(series, "ok", RC_OK)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _failed(spec: SeriesSpec, op: str, code: str, *, detail: str = "",
            error_type: str | None = None) -> SeriesOutcome:
    return SeriesOutcome(
        spec.source, spec.interval, spec.symbol, "failed", _iso(spec.requested_start),
        _iso(spec.requested_end), _iso(spec.effective_end), op, failure=code,
        error_type=error_type, detail=detail,
        resume_from=_iso(spec.requested_start))


def empty_outcome(spec: SeriesSpec) -> SeriesOutcome:
    """取り込む範囲が無い系列。取得も書込みも lease も使わない。"""
    return SeriesOutcome(
        spec.source, spec.interval, spec.symbol, "ok", _iso(spec.requested_start),
        _iso(spec.requested_end), _iso(spec.effective_end), uuid.uuid4().hex)


def _conflict_lines(conn, classification, *, upto: str | None) -> tuple[ConflictLine, ...]:
    lines = []
    for entry in classification.entries:
        if entry.kind != "conflict":
            continue
        if upto is not None and entry.bar_time > upto:
            continue
        _ej, existing_digest = history_conflicts.canonical_values(entry.existing)
        _ij, incoming_digest = history_conflicts.canonical_values(entry.incoming)
        row = conn.execute(
            "SELECT id, state FROM history_conflicts WHERE source=? AND interval=? "
            "AND symbol=? AND bar_time=? AND history_epoch=? AND existing_digest=? "
            "AND incoming_digest=?",
            (classification.source, entry.interval, entry.symbol, entry.bar_time,
             classification.epoch, existing_digest, incoming_digest)).fetchone()
        lines.append(ConflictLine(
            row["id"] if row else None, row["state"] if row else None, entry.known,
            entry.bar_time, classification.epoch, entry.existing, entry.incoming))
    return tuple(lines)


def _is_busy(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc).lower()
        return "locked" in text or "busy" in text
    return False


def run_series(conn: sqlite3.Connection, spec: SeriesSpec, *,
               origin: str = "import", run_id: str | None = None,
               now: Callable[[], datetime] | None = None,
               monotonic: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], None] = time.sleep,
               stage_ttl: float = STAGE_TTL_SEC,
               busy_budget: float = BUSY_BUDGET_SEC,
               admission=None, max_rows: int = ohlcv.BATCH_MAX_ROWS,
               budget_seconds: float = ohlcv.BATCH_BUDGET_SECONDS) -> SeriesOutcome:
    """1 系列を取得 → 遮断判定 → 確定する。失敗は例外にせず結果に畳む。"""
    if conn.in_transaction:
        raise ValueError("run_series: connection must not be inside a transaction")
    op = uuid.uuid4().hex
    clock_now = now or (lambda: datetime.now(timezone.utc))
    try:
        rows = spec.fetch()
    except SeriesFetchError as exc:
        return _failed(spec, op, exc.code, detail=exc.detail, error_type=exc.error_type)
    except Exception as exc:   # 取得側の想定外も当該系列の失敗にとどめる
        return _failed(spec, op, "fetch_failed", error_type=type(exc).__name__)
    if len(rows) > MAX_IMPORT_ROWS:
        return _failed(spec, op, "too_large")
    stage_started = monotonic()

    previous_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    with ExitStack() as stack:
        try:
            stack.enter_context(history_mutation_lease(conn))
        except HistoryLeaseBusy:
            return _failed(spec, op, "database_busy")
        except OSError as exc:
            return _failed(spec, op, "lease_unavailable", error_type=type(exc).__name__)
        conn.execute("PRAGMA busy_timeout=0")
        try:
            return _classify_and_apply(
                conn, spec, op, rows, origin=origin, run_id=run_id,
                seen_at=clock_now(), monotonic=monotonic, sleep=sleep,
                stage_started=stage_started, stage_ttl=stage_ttl,
                busy_budget=busy_budget, admission=admission, max_rows=max_rows,
                budget_seconds=budget_seconds)
        finally:
            conn.execute(f"PRAGMA busy_timeout={int(previous_timeout)}")


def _classify_and_apply(conn, spec, op, rows, *, origin, run_id, seen_at, monotonic,
                        sleep, stage_started, stage_ttl, busy_budget, admission,
                        max_rows, budget_seconds) -> SeriesOutcome:
    busy_spent = 0.0
    while True:
        try:
            classification = ohlcv.classify_history_rows(conn, rows, source=spec.source)
            break
        except ohlcv.InconsistentInputError as exc:
            return _failed(spec, op, "invalid_response", detail=str(exc),
                           error_type=type(exc).__name__)
        except Exception as exc:
            if _is_busy(exc) and busy_spent + 1e-9 < busy_budget:
                sleep(BUSY_RETRY_SLEEP_SEC)
                busy_spent += BUSY_RETRY_SLEEP_SEC
                continue
            code = "database_busy" if _is_busy(exc) else "classify_failed"
            return _failed(spec, op, code, error_type=type(exc).__name__)
    common = dict(
        source=spec.source, interval=spec.interval, symbol=spec.symbol,
        requested_start=_iso(spec.requested_start), requested_end=_iso(spec.requested_end),
        effective_end=_iso(spec.effective_end), operation_id=op,
        n_overlap=classification.n_overlap, n_test=classification.n_test)
    if classification.circuit_open:
        try:
            lines = _conflict_lines(conn, classification, upto=None)
        except Exception as exc:
            code = "database_busy" if _is_busy(exc) else "conflict_lines_failed"
            return _failed(spec, op, code, error_type=type(exc).__name__)
        return SeriesOutcome(
            status="conflict_circuit_open",
            conflicted_new=classification.conflicts_new,
            conflicted_known=classification.conflicts_known,
            resume_from=_iso(spec.requested_start), conflict_lines=lines, **common)

    entries = list(classification.entries)
    totals = dict(inserted=0, unchanged=0, new=0, known=0)
    last_committed = None
    failure = error_type = None

    def before_batch():
        if monotonic() - stage_started >= stage_ttl:
            return "stage_expired"
        return None

    while True:
        sub = ohlcv.HistoryClassification(
            classification.source, classification.epoch, tuple(entries))
        res = ohlcv.apply_classified_history(
            conn, sub, origin=origin, seen_at=seen_at, run_id=run_id,
            max_rows=max_rows, budget_seconds=budget_seconds,
            admission=admission, before_batch=before_batch,
            before_commit=before_batch)
        totals["inserted"] += res.inserted
        totals["unchanged"] += res.unchanged
        totals["new"] += res.conflicted_new
        totals["known"] += res.conflicted_known
        if res.last_committed_bar_time is not None:
            last_committed = res.last_committed_bar_time
        if res.status == "ok":
            break
        failure, error_type = res.failure, res.error_type
        if res.resume_from is not None:
            entries = [e for e in entries if e.bar_time >= res.resume_from]
        if failure == "database_busy" and busy_spent + 1e-9 < busy_budget:
            sleep(BUSY_RETRY_SLEEP_SEC)
            busy_spent += BUSY_RETRY_SLEEP_SEC
            failure = error_type = None
            continue
        break

    lines: tuple[ConflictLine, ...] = ()
    lines_failed = False
    if totals["new"] + totals["known"]:
        try:
            lines = _conflict_lines(conn, classification, upto=last_committed)
        except Exception as exc:
            lines_failed = True
            if failure is None:
                failure = "database_busy" if _is_busy(exc) else "conflict_lines_failed"
                error_type = type(exc).__name__
    if failure is None:
        status = "completed_with_conflicts" if (totals["new"] + totals["known"]) else "ok"
        return SeriesOutcome(
            status=status, inserted=totals["inserted"], unchanged=totals["unchanged"],
            conflicted_new=totals["new"], conflicted_known=totals["known"],
            last_committed_bar_time=last_committed, conflict_lines=lines, **common)
    committed = last_committed is not None
    return SeriesOutcome(
        status="partial" if committed else "failed", inserted=totals["inserted"],
        unchanged=totals["unchanged"], conflicted_new=totals["new"],
        conflicted_known=totals["known"], last_committed_bar_time=last_committed,
        resume_from=None if lines_failed and res.status == "ok"
        else (entries[0].bar_time if entries else None), failure=failure,
        error_type=error_type, conflict_lines=lines, **common)


def run_import(conn: sqlite3.Connection, specs: list[SeriesSpec], **kw) -> ImportOutcome:
    """系列を渡された安定順に 1 つずつ処理する。失敗した系列があっても続ける。"""
    return summarize([run_series(conn, spec, **kw) for spec in specs])
