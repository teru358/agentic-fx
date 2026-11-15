"""停止中の手動同期 (`afx history sync`)。

対象は設定順に重複を除いた `pairs + datafeed.watch_symbols`、source は mt5、足幅は
`backtest.base_interval`。取得は共通の取込 (`store.history_import.run_series`、
`origin=sync`) に乗せ、比較・監査・遮断・batch・rc は import と同じ契約を使う。
呼出し側が instance lock を持っている間だけ呼ぶこと (稼働中の worker とは排他)。

1 回の同期 (subrun) は、系列ごとの recent 操作 1 本 (後ろ窓 + 検査尾) と、queue の古い gap
(UTC 日ごとの独立操作、全系列合計 32 日窓まで) からなる。`until_done` は上限付きの subrun を
連続させ、due の gap 取得が尽きる・失敗・進捗なしのいずれかで止まる。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from agentic_fx.backtest import history_sync_plan as plan
from agentic_fx.backtest.dataset import HistoryDataset
from agentic_fx.backtest.history_health import (
    CALENDAR_VERSION, EMPTY_NO_BARS, MAX_DAY_MISSING, NoBarsList, expected_grid)
from agentic_fx.backtest.holdout import in_sample_until
from agentic_fx.backtest.mt5_import import _default_fetch, import_mt5
from agentic_fx.core.history_epoch import history_epoch
from agentic_fx.core.market_hours import is_market_open
from agentic_fx.store import history_sync as store
from agentic_fx.store.history_import import (
    RC_FAILURE, RC_OK, SeriesOutcome, summarize)
from agentic_fx.store.ohlcv import inspection_tail_bars

SOURCE = "mt5"
REQUEST_SPACING_SEC = 5.0
OLD_GAP_WINDOWS = 32
#: recent の要求窓の上限は、後ろ窓 D 日 + 日をまたぐ検査尾の分の 2 窓
RECENT_EXTRA_WINDOWS = 2
MAX_SUBRUNS = 64
SUSPECT_MEDIAN_DAYS = 20

RC_COMPLETED_WITH_GAPS = 3
REASON_NOT_APPLICABLE = "not_applicable_eval_source"
REASON_NO_BRIDGE = "bridge_not_configured"
REASON_REQUEST_BUDGET = "request_budget"
REASON_NO_RANGE = "empty_range"


class SyncRefused(Exception):
    """I/O の前に拒否する。code は固定語、rc は終了コード (引数・前提の不備は 2)。"""

    def __init__(self, code: str, rc: int):
        super().__init__(code)
        self.code = code
        self.rc = rc


def preflight(settings) -> None:
    if settings.backtest.eval_source != SOURCE:
        raise SyncRefused(REASON_NOT_APPLICABLE, 2)
    if not settings.datafeed.mt5.bridge_url:
        raise SyncRefused(REASON_NO_BRIDGE, 1)


def split_targets(settings) -> tuple[list[str], list[str]]:
    """(主 pair, 参照銘柄)。いずれも設定順で重複を除き、参照から主 pair を外す。"""
    primary = list(dict.fromkeys(settings.pairs))
    seen = set(primary)
    references = []
    for symbol in settings.datafeed.watch_symbols:
        if symbol not in seen:
            seen.add(symbol)
            references.append(symbol)
    return primary, references


class RequestGate:
    """要求を単一 in-flight・開始間隔 5 秒以上に絞り、回数を数える (失敗した要求も数える)。"""

    def __init__(self, fetch: Callable[[str], dict], *, spacing: float = REQUEST_SPACING_SEC,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._fetch = fetch
        self._spacing = spacing
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_start: float | None = None
        self.count = 0

    def __call__(self, url: str) -> dict:
        if self._last_start is not None:
            wait = self._spacing - (self._monotonic() - self._last_start)
            if wait > 0:
                self._sleep(wait)
        self._last_start = self._monotonic()
        self.count += 1
        return self._fetch(url)


@dataclass
class OpResult:
    op_seq: int
    operation: str            # recent | gap
    symbol: str
    range_kind: str
    start: datetime
    end: datetime
    day: str | None
    status: str               # SeriesOutcome.status か deferred
    reason: str | None = None
    inserted: int = 0
    unchanged: int = 0
    conflicted_new: int = 0
    conflicted_known: int = 0
    requests: int = 0
    recorded: bool = True
    outcome: SeriesOutcome | None = None


@dataclass
class SyncResult:
    run_id: str
    result: str
    rc: int
    t: datetime
    b: datetime
    e: datetime
    ops: list[OpResult] = field(default_factory=list)
    requests: int = 0
    subruns: int = 0
    cursors: dict[str, str | None] = field(default_factory=dict)
    gap_counts: dict[str, int] = field(default_factory=dict)
    primary_open_gaps: int = 0
    new_reference_suspects: int = 0
    deferred_reasons: list[str] = field(default_factory=list)
    stopped: str | None = None
    unrecorded_series: int = 0
    required_write_failures: list[str] = field(default_factory=list)
    unprocessed: list[str] = field(default_factory=list)

    @property
    def inserted(self) -> int:
        return sum(o.inserted for o in self.ops)

    @property
    def unchanged(self) -> int:
        return sum(o.unchanged for o in self.ops)

    @property
    def conflicted(self) -> int:
        return sum(o.conflicted_new + o.conflicted_known for o in self.ops)


# ---- 観測 (gap・参照の疑い) ---------------------------------------------------

def _parse_time(raw: str, width: timedelta) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    parsed = parsed.astimezone(timezone.utc)
    if (parsed - plan._EPOCH) % width:
        return None
    return parsed


def present_times(conn, dataset: HistoryDataset, symbol: str, start: datetime,
                  end: datetime) -> list[datetime]:
    out = []
    for (raw,) in conn.execute(
            "SELECT bar_time FROM ohlcv_history WHERE symbol=? AND interval=? AND source=? "
            "AND bar_time >= ? AND bar_time < ?",
            (symbol, dataset.base_interval, dataset.source, start.isoformat(),
             end.isoformat())):
        parsed = _parse_time(raw, dataset.width)
        if parsed is not None:
            out.append(parsed)
    return out


def primary_day_missing(conn, dataset: HistoryDataset, symbol: str, *, start: datetime,
                        end: datetime, now: datetime, grace: timedelta,
                        no_bars: NoBarsList, is_open=is_market_open
                        ) -> dict[date, int]:
    """[start, end) の完了 UTC 日ごとの欠損本数 (期待される足 − 存在する足)。

    期待される足は guard と同じ `expected_grid` (暦・確定の猶予・無足日)。end は E を渡し、
    E 以後の余裕 1 本を欠損にしない。"""
    excluded = no_bars.dates_for(dataset.source, dataset.base_interval, symbol)
    expected = expected_grid(start, end, width=dataset.width, now=now, grace=grace,
                             is_open=is_open, excluded_dates=excluded)
    have = set(present_times(conn, dataset, symbol, start, end))
    out: dict[date, int] = {}
    for t in expected:
        if t.date() >= now.date():
            continue
        if t not in have:
            out[t.date()] = out.get(t.date(), 0) + 1
    return out


def guard_relevant(missing: dict[date, int], width: timedelta) -> dict[date, int]:
    """guard の gap 判定に効く日 (欠損が 60 分を超える日) だけ。"""
    return {day: n for day, n in missing.items() if n * width > MAX_DAY_MISSING}


def _day_counts(conn, dataset: HistoryDataset, symbol: str, start: datetime,
                end: datetime) -> dict[date, int]:
    out: dict[date, int] = {}
    for raw, n in conn.execute(
            "SELECT substr(bar_time,1,10), COUNT(*) FROM ohlcv_history WHERE symbol=? AND "
            "interval=? AND source=? AND bar_time >= ? AND bar_time < ? "
            "GROUP BY substr(bar_time,1,10)",
            (symbol, dataset.base_interval, dataset.source, start.isoformat(),
             end.isoformat())):
        try:
            out[date.fromisoformat(raw)] = n
        except ValueError:
            continue
    return out


def reference_suspects(conn, dataset: HistoryDataset, reference: str,
                       primaries: list[str], *, boundary: datetime, now: datetime,
                       no_bars: NoBarsList) -> dict[date, tuple[int, int | None]]:
    """参照の疑い日 → (その日の本数, 直前 20 有足日の中央値の 2 倍 (比較不能なら None))。

    主 pair のどれかに足がある完了 UTC 日で、参照が 0 本、または直前 20 有足日の中央値の
    50% 未満。20 有足日に満たなければ 0 本だけを疑う。50% ちょうどは対象外 (有理数で比較)。
    商品固有の休場は欠損と断定できないので、ここは「疑い」に留める。"""
    horizon = boundary - timedelta(days=90)
    ref_counts = _day_counts(conn, dataset, reference, horizon, now)
    excluded = no_bars.dates_for(dataset.source, dataset.base_interval, reference)
    primary_days: set[date] = set()
    for symbol in primaries:
        primary_days |= set(_day_counts(conn, dataset, symbol, boundary, now))
    out: dict[date, tuple[int, int | None]] = {}
    prior_days = sorted(d for d in ref_counts if ref_counts[d] > 0)
    for day in sorted(primary_days):
        if day >= now.date() or day < boundary.date() or day in excluded:
            continue
        count = ref_counts.get(day, 0)
        history = [d for d in prior_days if d < day][-SUSPECT_MEDIAN_DAYS:]
        if count == 0:
            out[day] = (0, None)
            continue
        if len(history) < SUSPECT_MEDIAN_DAYS:
            continue
        values = sorted(ref_counts[d] for d in history)
        # 偶数個の中央値は中央 2 値の平均。2 倍して整数にする。
        twice_median = values[len(values) // 2 - 1] + values[len(values) // 2]
        if 4 * count < twice_median:
            out[day] = (count, twice_median)
    return out


# ---- 本体 ---------------------------------------------------------------------

def run_sync(conn, settings, *, now: datetime, no_bars: NoBarsList = EMPTY_NO_BARS,
             fetch: Callable[[str], dict] | None = None, rescan: bool = False,
             until_done: bool = False, owner: str = "offline", run_id: str | None = None,
             is_open=is_market_open, monotonic: Callable[[], float] = time.monotonic,
             sleep: Callable[[float], None] = time.sleep,
             spacing: float = REQUEST_SPACING_SEC, max_subruns: int = MAX_SUBRUNS,
             run_kw: dict | None = None) -> SyncResult:
    """停止中の手動同期を 1 回 (until_done なら複数 subrun) 行う。

    呼出し側は preflight 済みで、instance lock を保持した状態であること。`fetch` は bridge への
    1 要求 (URL → 応答 dict)。`now` は固定する T (全範囲・時刻の記録に同じ値を使う)。
    """
    primary, references = split_targets(settings)
    targets = [*primary, *references]
    dataset = settings.backtest.dataset()
    width = dataset.width
    grace = timedelta(seconds=settings.datafeed.closed_bar_grace_sec)
    lookback = settings.backtest.history_sync.lookback_days
    base_url = settings.datafeed.mt5.bridge_url
    now = now.astimezone(timezone.utc)
    boundary = in_sample_until(now, settings.backtest.holdout_months,
                               base_interval=dataset.base_interval)
    limit = plan.effective_end(now, now, grace=grace, width=width)
    gate = RequestGate(fetch or _default_fetch, spacing=spacing, monotonic=monotonic,
                       sleep=sleep)
    tail_k = inspection_tail_bars(int(width.total_seconds()))
    run_id = run_id or uuid.uuid4().hex
    result = SyncResult(run_id, "running", RC_OK, now, boundary, limit)
    state = _Run(conn=conn, settings=settings, dataset=dataset, width=width, grace=grace,
                 lookback=lookback, base_url=base_url, now=now, boundary=boundary,
                 limit=limit, gate=gate, tail_k=tail_k, run_id=run_id, no_bars=no_bars,
                 is_open=is_open, primary=primary, references=references, targets=targets,
                 sleep=sleep, result=result, run_kw=run_kw or {})
    state.open_run(owner)
    try:
        state.prepare(rescan)
        state.recent_phase()
        state.reconcile()
        subrun = 1
        state.gap_phase()
        while until_done and subrun < max_subruns and state.can_continue():
            subrun += 1
            if state.gap_phase() == 0:
                break
        result.subruns = subrun
        state.finalize()
    except BaseException:
        state.abort()
        raise
    return result


class _Run:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.op_seq = 0
        self.executed_last = 0

    # -- 記録 --
    def _write(self, work) -> bool:
        try:
            store.write_txn(self.conn, work, sleep=self.sleep)
            return True
        except Exception:  # 記録の失敗は当該の記録だけを落とし、取得結果は消さない
            return False

    def _write_required(self, what: str, work) -> bool:
        """状態の整合に要る書込み。失敗は run に累積し、最終結果を技術失敗にする。"""
        if self._write(work):
            return True
        self.result.required_write_failures.append(what)
        return False

    def open_run(self, owner: str) -> None:
        def work(c):
            store.mark_interrupted(c, owner_alive=lambda _o: False, now=self.now)
            store.insert_run(
                c, run_id=self.run_id, owner=owner, trigger="manual", source=SOURCE,
                interval=self.dataset.base_interval, calendar_version=CALENDAR_VERSION,
                lookback_days=self.lookback, epoch=history_epoch(c), t=self.now,
                b=self.boundary, e=self.limit, started_at=self.now)
        store.write_txn(self.conn, work, sleep=self.sleep)

    def prepare(self, rescan: bool) -> None:
        interval = self.dataset.base_interval

        def work(c):
            if rescan:
                store.reopen_expired(c, source=SOURCE, interval=interval,
                                     boundary=self.boundary, now=self.now)
            store.expire_gaps(c, source=SOURCE, interval=interval,
                              boundary=self.boundary, now=self.now)
        self._write(work)

    # -- 取得 1 操作 --
    def _existing_before(self, symbol: str, anchor: datetime) -> list[datetime]:
        try:
            lo = plan.retreat_open(anchor, plan.TAIL_MAX_OPEN + plan.TAIL_BASE_OPEN,
                                   is_open=self.is_open)
        except plan.PlanError:
            return []
        return present_times(self.conn, self.dataset, symbol, max(lo, self.boundary), anchor)

    def _run_op(self, *, operation: str, symbol: str, range_kind: str, start: datetime,
                end: datetime, day: str | None, window_cap: int | None,
                cap_used: int = 0) -> OpResult:
        """1 系列・1 範囲の取得操作。取得前に尾と要求数を固定し、上限外は 0 要求で後送りにする。"""
        self.op_seq += 1
        op = OpResult(self.op_seq, operation, symbol, range_kind, start, end, day, "running")
        excluded = self.no_bars.dates_for(self.dataset.source, self.dataset.base_interval,
                                          symbol)
        tail = plan.select_tail(self._existing_before(symbol, start), start,
                                width=self.width, is_open=self.is_open,
                                excluded_dates=excluded, boundary=self.boundary)
        windows = 0
        if tail.ok:
            request_start = max(tail.tail.start, self.boundary)
            windows = plan.request_count([(request_start, end)], is_open=self.is_open)
            if window_cap is not None and cap_used + windows > window_cap:
                tail = plan.TailResult(None, REASON_REQUEST_BUDGET)
        series_id = self._start_series(op)
        if not tail.ok:
            op.status, op.reason = "deferred", tail.reason
            self._finish_series(series_id, op)
            return op
        before = self.gate.count
        outcome = import_mt5(
            self.conn, symbol, request_start, end, base_url=self.base_url, fetch=self.gate,
            interval=self.dataset.base_interval, clock=lambda: self.now, grace=self.grace,
            skip_window=lambda ws, we: not plan.has_open_time(ws, we, is_open=self.is_open),
            utc_day_windows=True, origin="sync", run_id=self.run_id,
            inspection_tail_bars=self.tail_k, on_batch=self._progress(series_id),
            **self.run_kw)
        op.requests = self.gate.count - before
        op.outcome = outcome
        op.status = outcome.status
        op.reason = outcome.failure or outcome.deferred_reason
        op.inserted, op.unchanged = outcome.inserted, outcome.unchanged
        op.conflicted_new, op.conflicted_known = outcome.conflicted_new, outcome.conflicted_known
        self._finish_series(series_id, op)
        return op

    def _start_series(self, op: OpResult) -> int | None:
        holder = {}

        def work(c):
            holder["id"] = store.insert_series(
                c, run_id=self.run_id, op_seq=op.op_seq, operation=op.operation,
                symbol=op.symbol, range_kind=op.range_kind, range_start=op.start,
                range_end=op.end, day_utc=op.day, started_at=self.now)
        if self._write(work):
            return holder["id"]
        op.recorded = False
        return None

    def _progress(self, series_id: int | None):
        if series_id is None:
            return None

        def on_batch(conn, progress):
            last = progress.batch_bar_time_end
            prefix_end = store.iso(datetime.fromisoformat(last) + self.width)
            store.update_series_progress(conn, series_id, committed_prefix_end=prefix_end,
                                         next_cursor=progress.resume_from,
                                         last_committed_bar_time=last)
        return on_batch

    def _finish_series(self, series_id: int | None, op: OpResult) -> None:
        if series_id is None:
            self.result.unrecorded_series += 1
            return
        outcome = op.outcome
        status = op.status if op.status != "running" else "failed"

        def work(c):
            store.finish_series(
                c, series_id, status=status, finished_at=self.now, inserted=op.inserted,
                unchanged=op.unchanged, conflicted_new=op.conflicted_new,
                conflicted_known=op.conflicted_known, error_code=op.reason,
                last_committed_bar_time=getattr(outcome, "last_committed_bar_time", None),
                next_cursor=getattr(outcome, "resume_from", None))
            c.execute("UPDATE history_sync_series SET requests=? WHERE id=?",
                      (op.requests, series_id))
        if not self._write(work):
            op.recorded = False
            self.result.unrecorded_series += 1

    def _after_op(self, op: OpResult, *, advance_to: datetime | None,
                  unprocessed: dict | None = None) -> None:
        """取得の結果を系列状態へ反映する。成功だけ cursor を進め、失敗は待ちを延ばす。

        cursor より前の未取得 (unprocessed) の保存と cursor の前進は同じ transaction で、
        保存できなければ cursor は進まない。"""
        interval = self.dataset.base_interval
        confirmed = op.status in ("ok", "completed_with_conflicts")
        failed = op.status in ("partial", "failed")

        def work(c):
            self._save_unprocessed(c, op.symbol, unprocessed or {})
            if confirmed and advance_to is not None:
                store.advance_cursor(c, SOURCE, interval, op.symbol,
                                     last_processed_e=advance_to, run_id=self.run_id,
                                     now=self.now)
            elif failed:
                store.record_series_failure(c, SOURCE, interval, op.symbol,
                                            error_code=op.reason or op.status,
                                            run_id=self.run_id, now=self.now)
        self._write_required(f"series_state:{op.symbol}", work)
        self.result.ops.append(op)

    # -- recent --
    def recent_phase(self) -> None:
        interval = self.dataset.base_interval
        if self.limit <= self.boundary:
            self.result.deferred_reasons.append(REASON_NO_RANGE)
            return
        for symbol in self.targets:
            start = plan.lookback_start(self.limit, self.lookback, self.boundary)
            row = store.get_state(self.conn, SOURCE, interval, symbol)
            prior = None
            if row is not None and row["last_processed_e"]:
                prior = datetime.fromisoformat(row["last_processed_e"])
            unprocessed = None
            if prior is not None and prior < start:
                unprocessed = self._unprocessed_days(symbol, max(prior, self.boundary), start)
            op = self._run_op(operation="recent", symbol=symbol, range_kind="recent",
                              start=start, end=self.limit, day=None,
                              window_cap=self.lookback + RECENT_EXTRA_WINDOWS)
            if op.status == "deferred" and op.reason == REASON_REQUEST_BUDGET:
                # 上限外の範囲は黙って落とさず、日ごとの未取得として queue に残す。
                first = start if prior is None else min(max(prior, self.boundary), start)
                unprocessed = self._unprocessed_days(symbol, first, self.limit)
            self._after_op(op, advance_to=self.limit, unprocessed=unprocessed)

    def _unprocessed_days(self, symbol: str, start: datetime, end: datetime
                          ) -> dict[str, list[list[str]]]:
        """cursor から後ろ窓の手前までの未取得を、日ごとの範囲として数える (書込みはしない)。"""
        excluded = self.no_bars.dates_for(self.dataset.source, self.dataset.base_interval,
                                          symbol)
        days: dict[str, list[list[str]]] = {}
        for ws, we in plan.fetch_windows(start, end, is_open=self.is_open):
            slots = plan.open_slots(ws, we, width=self.width, is_open=self.is_open,
                                    excluded_dates=excluded)
            if slots:
                days.setdefault(ws.date().isoformat(), []).append(
                    [store.iso(ws), store.iso(we)])
        return days

    def _save_unprocessed(self, c, symbol: str, days: dict) -> None:
        interval = self.dataset.base_interval
        for day, ranges in days.items():
            store.observe_gap(c, source=SOURCE, interval=interval, symbol=symbol,
                              day_utc=day, kind="unprocessed", missing_count=None,
                              detail={"ranges": ranges}, run_id=self.run_id, now=self.now)

    # -- 古い gap --
    def can_continue(self) -> bool:
        return self.executed_last > 0 and not any(
            o.status in ("partial", "failed", "conflict_circuit_open") for o in self.result.ops)

    def gap_phase(self) -> int:
        interval = self.dataset.base_interval
        due = store.due_gaps(self.conn, source=SOURCE, interval=interval, now=self.now,
                             kinds=store.GAP_KINDS, symbols=self.targets)
        ordered = store.order_round_robin(
            due, self.targets, store.get_rr_cursor(self.conn, SOURCE, interval))
        used = executed = 0
        last_symbol = None
        for row in ordered:
            day = date.fromisoformat(row["day_utc"])
            day0 = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            clipped = plan.clip(day0, day0 + timedelta(days=1), boundary=self.boundary,
                                limit=self.limit)
            if clipped is None:
                continue
            op = self._run_op(operation="gap", symbol=row["symbol"], range_kind=row["kind"],
                              start=clipped[0], end=clipped[1], day=row["day_utc"],
                              window_cap=OLD_GAP_WINDOWS, cap_used=used)
            if op.status == "deferred":
                if op.reason == REASON_REQUEST_BUDGET:
                    self.result.deferred_reasons.append(REASON_REQUEST_BUDGET)
                    self.result.ops.append(op)
                    break
                self.result.ops.append(op)
                if op.reason not in self.result.deferred_reasons:
                    self.result.deferred_reasons.append(op.reason)
                continue
            used += max(op.requests, 0)
            executed += 1
            last_symbol = row["symbol"]
            self._after_op(op, advance_to=None)
            self._record_gap_attempt(row, op)
        if last_symbol is not None:
            self._write(lambda c: store.set_rr_cursor(c, SOURCE, interval, last_symbol,
                                                      now=self.now))
        self.executed_last = executed
        return executed

    def _record_gap_attempt(self, row, op: OpResult) -> None:
        if op.status not in ("ok", "completed_with_conflicts"):
            return
        interval = self.dataset.base_interval
        symbol, day_s, kind = row["symbol"], row["day_utc"], row["kind"]
        day = date.fromisoformat(day_s)
        day0 = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        if kind == "reference_suspected":
            suspects = reference_suspects(
                self.conn, self.dataset, symbol, self.primary, boundary=self.boundary,
                now=self.now, no_bars=self.no_bars)
            # 疑いの本数は「その日の足の数」で 0 もあり得るので、解消は疑いが外れたときだけ。
            after = 0 if day not in suspects else None
        else:
            missing = primary_day_missing(
                self.conn, self.dataset, symbol, start=max(day0, self.boundary),
                end=min(day0 + timedelta(days=1), self.limit), now=self.now,
                grace=self.grace, no_bars=self.no_bars, is_open=self.is_open)
            after = missing.get(day, 0)
            if kind != "unprocessed" and after and after * self.width <= MAX_DAY_MISSING:
                after = 0

        def work(c):
            if kind == "unprocessed":
                store.resolve_gap(c, source=SOURCE, interval=interval, symbol=symbol,
                                  day_utc=day_s, kind=kind, resolution="processed",
                                  run_id=self.run_id, now=self.now)
            else:
                store.record_gap_attempt(
                    c, source=SOURCE, interval=interval, symbol=symbol, day_utc=day_s,
                    kind=kind, missing_before=row["missing_count"], missing_after=after,
                    run_id=self.run_id, now=self.now)
        self._write_required(f"gap_attempt:{symbol}", work)

    # -- 後処理 --
    def reconcile(self) -> None:
        """DB を読み直して gap queue を実態に合わせる (主 pair は guard と同じ判定)。"""
        interval = self.dataset.base_interval
        found_primary: dict[str, dict[date, int]] = {}
        found_refs: dict[str, dict[date, tuple[int, int | None]]] = {}
        if self.limit > self.boundary:
            for symbol in self.primary:
                found_primary[symbol] = guard_relevant(primary_day_missing(
                    self.conn, self.dataset, symbol, start=self.boundary, end=self.limit,
                    now=self.now, grace=self.grace, no_bars=self.no_bars,
                    is_open=self.is_open), self.width)
            for symbol in self.references:
                found_refs[symbol] = reference_suspects(
                    self.conn, self.dataset, symbol, self.primary, boundary=self.boundary,
                    now=self.now, no_bars=self.no_bars)

        def work(c):
            for symbol, days in found_primary.items():
                self._reconcile_kind(c, symbol, "primary_exact",
                                     {d: (n, None) for d, n in days.items()})
            for symbol, days in found_refs.items():
                self._reconcile_kind(c, symbol, "reference_suspected", days)
        self._write_required("reconcile", work)

    def _reconcile_kind(self, c, symbol: str, kind: str,
                        observed: dict[date, tuple[int, int | None]]) -> None:
        interval = self.dataset.base_interval
        seen = set()
        for day, (count, twice_median) in sorted(observed.items()):
            seen.add(day.isoformat())
            detail = None if twice_median is None else {"twice_median": twice_median}
            outcome = store.observe_gap(
                c, source=SOURCE, interval=interval, symbol=symbol, day_utc=day.isoformat(),
                kind=kind, missing_count=count, detail=detail, run_id=self.run_id,
                now=self.now)
            if kind == "reference_suspected" and outcome in ("new", "reopened"):
                self.result.new_reference_suspects += 1
        for row in c.execute(
                "SELECT day_utc FROM history_sync_gaps WHERE source=? AND interval=? AND "
                "symbol=? AND kind=? AND state='open'", (SOURCE, interval, symbol,
                                                         kind)).fetchall():
            if row["day_utc"] not in seen:
                store.resolve_gap(c, source=SOURCE, interval=interval, symbol=symbol,
                                  day_utc=row["day_utc"], kind=kind, resolution="rescan_clear",
                                  run_id=self.run_id, now=self.now)

    def finalize(self) -> None:
        interval = self.dataset.base_interval
        self.reconcile()
        result = self.result
        result.gap_counts = store.count_gaps(self.conn, source=SOURCE, interval=interval)
        result.primary_open_gaps = self.conn.execute(
            "SELECT COUNT(*) FROM history_sync_gaps WHERE source=? AND interval=? AND "
            "state='open' AND kind IN ('primary_exact','unprocessed') AND symbol IN (%s)"
            % ",".join("?" * len(self.primary)), (SOURCE, interval, *self.primary)
        ).fetchone()[0] if self.primary else 0
        result.unprocessed = [
            f"{r['symbol']} {r['day_utc']}" for r in self.conn.execute(
                "SELECT symbol, day_utc FROM history_sync_gaps WHERE source=? AND interval=? "
                "AND state='open' AND kind='unprocessed' ORDER BY day_utc, symbol",
                (SOURCE, interval))]
        for symbol in self.targets:
            row = store.get_state(self.conn, SOURCE, interval, symbol)
            result.cursors[symbol] = None if row is None else row["last_processed_e"]
        result.requests = self.gate.count
        outcomes = [o.outcome for o in result.ops if o.outcome is not None]
        deferred_ops = [o for o in result.ops if o.status == "deferred"]
        summary = summarize(outcomes) if outcomes else None
        name, rc = (summary.result, summary.rc) if summary else ("ok", RC_OK)
        if (not summary or name in ("ok", "deferred")) and deferred_ops:
            name, rc = "deferred", RC_OK
        if name in ("ok", "deferred") and (result.primary_open_gaps
                                           or result.new_reference_suspects):
            name, rc = "completed_with_gaps", RC_COMPLETED_WITH_GAPS
        if result.unrecorded_series or result.required_write_failures:
            # 状態の記録に失敗した run は、衝突・遮断・後送りより技術失敗を優先する。
            confirmed = any(o.outcome is not None and o.outcome.confirmed for o in result.ops)
            name, rc = ("partial" if confirmed else "failed"), RC_FAILURE
        result.result, result.rc = name, rc
        failures = [o.reason for o in result.ops if o.status in ("partial", "failed")]
        if not failures and result.required_write_failures:
            failures = ["state_write_failed"]

        def work(c):
            store.finish_run(
                c, self.run_id, status=name, rc=rc, finished_at=self.now,
                series_total=len(result.ops),
                series_failed=sum(o.status in ("partial", "failed") for o in result.ops),
                inserted=result.inserted, unchanged=result.unchanged,
                conflicted_new=sum(o.conflicted_new for o in result.ops),
                conflicted_known=sum(o.conflicted_known for o in result.ops),
                requests=result.requests, failure_code=failures[0] if failures else None,
                deferred_reason=(result.deferred_reasons[0] if result.deferred_reasons
                                 else None),
                unprocessed=result.unprocessed or None,
                unrecorded_series=result.unrecorded_series)
            store.collect_garbage(c, now=self.now)
        if not self._write(work):
            result.result, result.rc = "failed", RC_FAILURE
            result.stopped = "run_record_failed"

    def abort(self) -> None:
        """例外で中断するとき、run を running のまま残さない (次回 owner 回収でも拾える)。"""
        def work(c):
            store.finish_run(c, self.run_id, status="interrupted", rc=RC_FAILURE,
                             finished_at=self.now, requests=self.gate.count)
        self._write(work)


def render(result: SyncResult, dataset: HistoryDataset, *, until_done: bool) -> list[str]:
    """人間向けの結果。rc=0 の後送りでも理由・未処理範囲・cursor・次の操作を必ず出す。"""
    lines = [f"history sync: dataset={dataset.source}/{dataset.base_interval} "
             f"T={result.t.isoformat()} B={result.b.isoformat()} E={result.e.isoformat()} "
             f"requests={result.requests} subruns={result.subruns}"]
    shown_deferred = 0
    omitted_deferred = 0
    for op in result.ops:
        if op.status == "deferred":
            shown_deferred += 1
            if shown_deferred > 10:
                omitted_deferred += 1
                continue
        extra = f" reason={op.reason}" if op.reason else ""
        target = f"day={op.day}" if op.day else "window"
        lines.append(
            f"series symbol={op.symbol} op={op.operation}/{op.range_kind} {target} "
            f"range=[{op.start.isoformat()},{op.end.isoformat()}) result={op.status}{extra} "
            f"inserted={op.inserted} unchanged={op.unchanged} "
            f"conflicted={op.conflicted_new + op.conflicted_known} requests={op.requests}")
    if omitted_deferred:
        lines.append(f"series ... (+{omitted_deferred} deferred)")
    for symbol, cursor in result.cursors.items():
        lines.append(f"cursor symbol={symbol} last_processed_e={cursor or 'uninitialized'}")
    counts = result.gap_counts
    lines.append(f"gaps open={counts.get('open', 0)} resolved={counts.get('resolved', 0)} "
                 f"expired={counts.get('expired', 0)} "
                 f"primary_open={result.primary_open_gaps} (afx history gaps list)")
    deferred = [o for o in result.ops if o.status == "deferred"]
    if deferred or result.deferred_reasons:
        reasons = sorted({r for r in [*(o.reason for o in deferred),
                                      *result.deferred_reasons] if r})
        remaining = "; ".join(f"{o.symbol} [{o.start.isoformat()},{o.end.isoformat()})"
                              for o in deferred) or "-"
        lines.append(f"deferred reason={','.join(reasons)} remaining={remaining}")
        lines.append(_next_text(reasons, until_done))
    elif result.primary_open_gaps and not until_done:
        lines.append("next: 古い gap が残っています。`afx history sync --until-done` で上限付きの"
                     "取得を続けるか、`afx history gaps list` で範囲を確認してください")
    if result.unprocessed:
        lines.append("unprocessed " + ", ".join(result.unprocessed[:20])
                     + (f" ... (+{len(result.unprocessed) - 20})"
                        if len(result.unprocessed) > 20 else ""))
    if result.unrecorded_series:
        lines.append(f"unrecorded_series={result.unrecorded_series} "
                     "(記録に失敗した操作があります。次回の同期で再確認されます)")
    if result.required_write_failures:
        lines.append("write_failures=" + ",".join(sorted(set(result.required_write_failures)))
                     + " (状態の保存に失敗しました。次回の同期で再確認されます)")
    lines.append(f"result={result.result} rc={result.rc}")
    lines.append(f"inserted={result.inserted} unchanged={result.unchanged} "
                 f"conflicted={result.conflicted}")
    return lines


def _next_text(reasons: list[str], until_done: bool) -> str:
    if "inspection_insufficient" in reasons or "tail_exceeds_limit" in reasons:
        return ("next: 比較できる既存の足が足りません。主 pair と参照を同じ開始日から "
                "`afx history import` で導入し、その後 `afx history sync` を再実行してください")
    if REASON_REQUEST_BUDGET in reasons:
        return ("next: 1 回の上限に達しました。`afx history sync --until-done` で続けるか、"
                "再実行してください")
    return "next: 条件を確認して `afx history sync` を再実行してください"
