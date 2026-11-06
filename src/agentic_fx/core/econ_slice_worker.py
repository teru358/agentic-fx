"""slice worker: shadow の終端と event 足の退避を、短い transaction の列で進める。

1 cycle (既定 30 秒) は次の順に進む。

1. 発表前 spread の記録 (25 owner の枠の外。slice の滞留で quote を取り逃さない)
2. `cancelled` set に残る slice 行の掃除 (状態は変えない)
3. 成熟した owner (event set と shadow) を最大 25 件: 正規化 (read-only) →
   staging 書込み → 読戻し → 完備 CAS
4. 保持期間を過ぎた set の slice 行の削除 (set 行・shadow 終端は残す)

書込みは専用の write 接続 (busy timeout 0) で行う。どの transaction も writer
admission の lease の中で始め、`SQLITE_BUSY` は即 rollback して 10 / 20 / 40 /
50ms の間隔で再試行する。DB 作業の累計が 250ms に達したら次の transaction を
始めず cycle を終える。完備 CAS だけは core lock の中で行う。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator, Sequence

from agentic_fx.core.paper_fills import (
    ShadowResolution, day_close_deadline, resolve_shadow, select_limit_entry)
from agentic_fx.core.writer_admission import WriterAdmission
from agentic_fx.datafeed.econ_bars import NormalizedBars, normalize_1m
from agentic_fx.store import ohlcv_slices, signal_shadow
from agentic_fx.store.ohlcv_slices import (
    FINALIZE_GRACE, MAX_BATCH_ROWS, ShadowCas, SliceRow)
from agentic_fx.store.short_tx import OverBudget, TxResult
from agentic_fx.store.signals import utc_iso

_log = logging.getLogger("agentic_fx.econ_slice_worker")

CYCLE_INTERVAL_SEC = 30.0
CYCLE_BUDGET_SEC = 0.25
TX_BUDGET_SEC = 0.05
MAX_OWNERS_PER_CYCLE = 25
SHADOW_CANDIDATE_LIMIT = 100
_LIMIT_WINDOW = timedelta(hours=4)
_BUSY_BACKOFF_SEC = (0.01, 0.02, 0.04)
_BUSY_BACKOFF_STEADY_SEC = 0.05
OWNER_MEMO_MAX = 64
LIMIT_MEMO_MAX = 256


def _backoff() -> Iterator[float]:
    yield from _BUSY_BACKOFF_SEC
    while True:
        yield _BUSY_BACKOFF_STEADY_SEC


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    text = str(exc).lower()
    return "locked" in text or "busy" in text


class _Memo:
    """件数に上限のある保持。超えたら古いものから捨てる。"""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._items: OrderedDict = OrderedDict()

    def get(self, key):
        return self._items.get(key)

    def put(self, key, value) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self._limit:
            self._items.popitem(last=False)

    def drop(self, key) -> None:
        self._items.pop(key, None)

    def __len__(self) -> int:
        return len(self._items)


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp).astimezone(timezone.utc)


@dataclass(slots=True)
class CycleReport:
    """1 cycle の観測。"""
    spreads: int = 0
    swept: int = 0
    purged: int = 0
    owners: int = 0
    completed: list[int] = field(default_factory=list)
    unrecoverable: int = 0
    discarded: int = 0
    expired_empty: int = 0
    yielded: bool = False
    budget_exhausted: bool = False
    spent_sec: float = 0.0


@dataclass(slots=True)
class _Attempt:
    leased: bool = False
    yielded: bool = False
    tx_start: float = 0.0


@dataclass(frozen=True, slots=True)
class _ShadowPlan:
    t0: datetime
    range_end: datetime
    resolution: ShadowResolution
    outcome: signal_shadow.ShadowOutcome
    want: tuple[SliceRow, ...]
    unrecoverable: bool
    kind: str
    direction: str
    requested_price: float | None
    stop_loss: float
    take_profit: float | None
    spread: float
    pip_size: float


@dataclass(frozen=True, slots=True)
class _EmptyPlan:
    """区間が空の market。set を作らず `expired` / `no_fill` を 1 回書く。"""
    outcome: signal_shadow.ShadowOutcome


def _outcome(res: ShadowResolution) -> signal_shadow.ShadowOutcome:
    if res.state == signal_shadow.STATE_EXPIRED:
        return signal_shadow.ShadowOutcome.no_fill()
    if res.result_code == signal_shadow.RESULT_DATA_INSUFFICIENT:
        return signal_shadow.ShadowOutcome.data_insufficient(
            entry_price=res.entry_price, entry_at=res.entry_at)
    return signal_shadow.ShadowOutcome.resolved(
        res.result_code, exit_price=res.exit_price, pnl_pips=res.pnl_pips,
        resolved_at=res.exit_at, entry_price=res.entry_price,
        entry_at=res.entry_at)


def _classify(staged: Sequence[SliceRow], want: Sequence[SliceRow]) -> str:
    """staging の行を期待の行と比べる。`match` / `partial` (期待の部分集合) /
    `mismatch` (期待にない minute か値の違いがある)。"""
    expected = {row.bar_time: row for row in want}
    for row in staged:
        if expected.get(row.bar_time) != row:
            return "mismatch"
    return "match" if len(staged) == len(want) else "partial"


class EconSliceWorker:
    def __init__(self, *, read_conn: sqlite3.Connection,
                 write_conn: sqlite3.Connection, core_lock: threading.RLock,
                 admission: WriterAdmission,
                 quote_fn: Callable[[str], object],
                 pip_size_fn: Callable[[str], float],
                 assumed_spread_pips_fn: Callable[[str], float],
                 now_fn: Callable[[], datetime],
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 on_complete: Callable[[dict], None] | None = None,
                 interval_sec: float = CYCLE_INTERVAL_SEC) -> None:
        self._read = read_conn
        self._write_conn = write_conn
        self._core_lock = core_lock
        self._admission = admission
        self._quote_fn = quote_fn
        self._pip_size_fn = pip_size_fn
        self._assumed_fn = assumed_spread_pips_fn
        self._now = now_fn
        self._monotonic = monotonic
        self._sleep = sleep
        self._on_complete = on_complete
        self._interval = interval_sec
        self._generation: int | None = None
        self._commit_seconds: deque[float] = deque(maxlen=256)
        self._spent = 0.0
        self._bounded = True
        self._abort = False
        self._report = CycleReport()
        self._shadow_cursor: tuple[str, int] | None = None
        self._event_cursor: tuple[str, int] | None = None
        self._event_norms = _Memo(OWNER_MEMO_MAX)
        self._shadow_plans = _Memo(OWNER_MEMO_MAX)
        self._limit_first = _Memo(LIMIT_MEMO_MAX)

    # --- 観測 ---------------------------------------------------------------

    @property
    def generation(self) -> int | None:
        return self._generation

    @property
    def commit_seconds(self) -> tuple[float, ...]:
        """直近の COMMIT 所要 (観測値)。"""
        return tuple(self._commit_seconds)

    # --- 起動と loop ----------------------------------------------------------

    def start(self, stop: threading.Event | None = None) -> bool:
        """世代を登録し、旧世代の staging を回収する。`stop` が立てば `False`。"""
        self._begin_unbounded()
        try:
            while True:
                result = self._write(lambda begin, checkpoint:
                                     ohlcv_slices.new_generation_tx(
                                         self._write_conn, now=self._now(),
                                         begin=begin, checkpoint=checkpoint),
                                     stop=stop)
                if isinstance(result, TxResult) and result.committed:
                    self._generation = result.value
                    break
                if not self._wait_retry(stop):
                    return False
            stale = self._reading(lambda c: ohlcv_slices.stale_staging_sets(
                c, current_generation=self._generation))
            for row in stale:
                if not self._discard(row["id"], row["generation"], stop=stop):
                    return False
            return True
        finally:
            self._bounded = True

    def run(self, stop: threading.Event) -> None:
        """停止されるまで `interval_sec` ごとに cycle を回す。"""
        while self._generation is None:
            try:
                if not self.start(stop):
                    return
            except Exception:
                _log.exception("econ slice worker start failed")
                self._generation = None
                if stop.wait(self._interval):
                    return
        while not stop.is_set():
            try:
                self.run_cycle()
            except Exception:
                _log.exception("econ slice worker cycle failed")
            if stop.wait(self._interval):
                break

    def _wait_retry(self, stop: threading.Event | None) -> bool:
        if stop is not None and stop.is_set():
            return False
        self._abort = False
        self._sleep(_BUSY_BACKOFF_STEADY_SEC)
        return True

    def _begin_unbounded(self) -> None:
        self._bounded = False
        self._abort = False

    # --- 1 cycle ------------------------------------------------------------

    def run_cycle(self) -> CycleReport:
        if self._generation is None:
            raise RuntimeError("start() must run before run_cycle()")
        self._spent = 0.0
        self._abort = False
        self._report = CycleReport()
        now = self._now()
        self._record_spreads(now)
        self._sweep_cancelled()
        self._resolve_owners(now)
        self._purge(now)
        self._report.yielded = self._abort and not self._exhausted()
        self._report.budget_exhausted = self._exhausted()
        self._report.spent_sec = self._spent
        return self._report

    def _exhausted(self) -> bool:
        return self._bounded and self._spent >= CYCLE_BUDGET_SEC

    def _stopped(self) -> bool:
        return self._abort or self._exhausted()

    # --- DB の入口 ------------------------------------------------------------

    def _reading(self, fn):
        started = self._monotonic()
        try:
            return fn(self._read)
        finally:
            if self._read.in_transaction:
                self._read.rollback()
            self._spent += max(0.0, self._monotonic() - started)

    def _write(self, op, *, locked: bool = False,
               stop: threading.Event | None = None):
        """書込み 1 本を admission の lease の中で行う。

        `op(begin, checkpoint)` の結果を返す。予算切れ・見送り (書込み要求中・
        core lock が取れない) は `None`。BUSY は rollback して backoff し、予算の
        中で再試行する。lease は BEGIN の前から commit / rollback の後まで保つ。
        `stop` が立てば、試行の前と backoff の前で抜けて `None`。
        """
        delays = _backoff()
        while True:
            if self._exhausted():
                return None
            if stop is not None and stop.is_set():
                return None
            if locked:
                if self._admission.requested():
                    return None
                if not self._core_lock.acquire(blocking=False):
                    return None
            attempt = _Attempt()
            started = self._monotonic()
            busy = False
            result = None
            try:
                def begin(conn: sqlite3.Connection) -> bool:
                    attempt.tx_start = self._monotonic()
                    admitted = self._admission.admit(
                        lambda: conn.execute("BEGIN IMMEDIATE"))
                    attempt.leased = admitted
                    attempt.yielded = not admitted
                    return admitted

                def checkpoint() -> None:
                    if self._monotonic() - attempt.tx_start > TX_BUDGET_SEC:
                        raise OverBudget

                try:
                    result = op(begin, checkpoint)
                except sqlite3.OperationalError as exc:
                    if not _is_busy(exc):
                        raise
                    busy = True
                finally:
                    if attempt.leased:
                        self._admission.end()
            finally:
                if locked:
                    self._core_lock.release()
            self._spent += max(0.0, self._monotonic() - started)
            if busy:
                if stop is not None and stop.is_set():
                    return None
                delay = next(delays)
                self._sleep(delay)
                self._spent += delay
                continue
            if attempt.yielded:
                self._abort = True
                return None
            if isinstance(result, TxResult) and result.commit_seconds is not None:
                self._commit_seconds.append(result.commit_seconds)
            return result

    @staticmethod
    def _committed(result) -> bool:
        if isinstance(result, TxResult):
            return result.committed
        return result is True

    # --- 1. 発表前 spread --------------------------------------------------------

    def _record_spreads(self, now: datetime) -> None:
        candidates = self._reading(lambda c: ohlcv_slices.spread_candidates(
            c, now=now))
        quotes: dict[str, object] = {}
        for cand in candidates:
            if self._stopped():
                return
            try:
                if self._record_spread(cand, quotes):
                    self._report.spreads += 1
            except Exception:
                _log.exception("econ spread record failed: set %s",
                               cand["set_id"])

    def _record_spread(self, cand: dict, quotes: dict[str, object]) -> bool:
        """候補ごとに現在時刻で判定する。発表時刻 (ts) 以後に取った quote は
        発表前の値と言えないので捨て、assumed を記録する。quote の取得は 1 cycle に
        pair ごと 1 回 (失敗も同じ cycle では再試行しない)。"""
        pair = cand["pair"]
        pip_size = self._pip_size_fn(pair)
        assumed = self._assumed_fn(pair)
        ts = _parse(cand["ts"])
        quote = None
        if self._now() < ts:
            if pair not in quotes:
                try:
                    quotes[pair] = self._quote_fn(pair)
                except Exception:
                    _log.warning("econ spread quote failed for %s; retry next "
                                 "cycle", pair, exc_info=True)
                    quotes[pair] = None
            quote = quotes[pair]
            if self._now() >= ts:
                # 取得の成否にかかわらず ts を越えたら発表前の値は取れない。
                quote = None
            elif quote is None:
                return False
        if quote is not None:
            spread = signal_shadow.spread_from_quote(
                quote.bid, quote.ask, pip_size=pip_size,
                assumed_spread_pips=assumed)
        else:
            spread = signal_shadow.spread_from_quote(
                None, None, pip_size=pip_size, assumed_spread_pips=assumed)
        result = self._write(lambda begin, checkpoint:
                             ohlcv_slices.record_event_spread_tx(
                                 self._write_conn, cand["set_id"],
                                 spread_pips=spread.spread_pips,
                                 spread_source=spread.source, begin=begin,
                                 checkpoint=checkpoint))
        return self._committed(result)

    # --- 2. cancelled の掃除 ----------------------------------------------------------

    def _sweep_cancelled(self) -> None:
        if self._stopped():
            return
        set_ids = self._reading(lambda c: ohlcv_slices.cancelled_sets_with_rows(
            c, limit=MAX_OWNERS_PER_CYCLE))
        for set_id in set_ids:
            size = MAX_BATCH_ROWS
            while not self._stopped():
                result = self._write(lambda begin, checkpoint:
                                     ohlcv_slices.sweep_cancelled_step(
                                         self._write_conn, set_id=set_id,
                                         max_rows=size, begin=begin,
                                         checkpoint=checkpoint))
                if not isinstance(result, TxResult):
                    break
                if result.status == "over_budget" and size > 1:
                    size = max(1, size // 2)
                    continue
                if not result.committed:
                    break
                self._report.swept += result.rows
                if result.done:
                    break

    # --- 保持期間を過ぎた set ---------------------------------------------------------

    def _purge(self, now: datetime) -> None:
        if self._stopped():
            return
        set_ids = self._reading(lambda c: ohlcv_slices.purgeable_sets(
            c, now=now, limit=MAX_OWNERS_PER_CYCLE))
        for set_id in set_ids:
            size = MAX_BATCH_ROWS
            while not self._stopped():
                result = self._write(lambda begin, checkpoint:
                                     ohlcv_slices.purge_step(
                                         self._write_conn, set_id=set_id,
                                         now=now, max_rows=size, begin=begin,
                                         checkpoint=checkpoint))
                if not isinstance(result, TxResult):
                    break
                if result.status == "over_budget" and size > 1:
                    size = max(1, size // 2)
                    continue
                if not result.committed:
                    break
                self._report.purged += result.rows
                if result.done:
                    break

    # --- 3. 成熟した owner -------------------------------------------------------------

    def _resolve_owners(self, now: datetime) -> None:
        if self._stopped():
            return
        events = self._event_candidates(now)
        shadows = self._shadow_candidates(now)
        queue: list[tuple[str, dict]] = []
        for i in range(max(len(events), len(shadows))):
            if i < len(events):
                queue.append(("event", events[i]))
            if i < len(shadows):
                queue.append(("shadow", shadows[i]))
        for kind, row in queue:
            if self._stopped() or self._report.owners >= MAX_OWNERS_PER_CYCLE:
                return
            keep = False
            try:
                if kind == "event":
                    keep = self._event_owner(row, now)
                else:
                    keep = self._shadow_owner(row, now)
            except Exception:
                _log.exception("econ slice owner failed: %s %s", kind,
                               row.get("id"))
            if not keep:
                self._forget(kind, row["id"])
            if not self._stopped():
                if kind == "shadow":
                    self._shadow_cursor = (row["decided_at"], row["id"])
                else:
                    self._event_cursor = (row["ts"], row["id"])

    def _forget(self, kind: str, owner_id: int) -> None:
        if kind == "event":
            self._event_norms.drop(owner_id)
        else:
            self._shadow_plans.drop(owner_id)
            self._limit_first.drop(owner_id)

    def _event_candidates(self, now: datetime) -> list[dict]:
        """shadow と同じく、前の cycle が見た最後の key の次から読み、末尾まで来たら
        先頭へ戻る。"""
        after = self._event_cursor
        rows = self._reading(lambda c: ohlcv_slices.matured_event_sets(
            c, now=now, limit=MAX_OWNERS_PER_CYCLE, after=after))
        if not rows and after is not None:
            self._event_cursor = None
            rows = self._reading(lambda c: ohlcv_slices.matured_event_sets(
                c, now=now, limit=MAX_OWNERS_PER_CYCLE))
        return rows

    def _shadow_candidates(self, now: datetime) -> list[dict]:
        """前の cycle が見た最後の key の次から読み、末尾まで来たら先頭へ戻る。
        壊れた行や未成熟の行が固定枠を占めても、走査は先へ進む。"""
        after = self._shadow_cursor
        rows = self._reading(lambda c: signal_shadow.unresolved_candidates(
            c, decided_before=now, limit=SHADOW_CANDIDATE_LIMIT, after=after))
        if not rows and after is not None:
            self._shadow_cursor = None
            rows = self._reading(lambda c: signal_shadow.unresolved_candidates(
                c, decided_before=now, limit=SHADOW_CANDIDATE_LIMIT))
        return rows

    # --- event ------------------------------------------------------------------------

    def _event_owner(self, row: dict, now: datetime) -> bool:
        """進行中で次の cycle に続くなら `True` (正規化の結果を保持する)。"""
        set_id, pair = row["id"], row["pair"]
        staging = row["state"] == ohlcv_slices.STATE_STAGING
        if staging and row["generation"] != self._generation:
            return False
        start, end = _parse(row["range_start"]), _parse(row["range_end"])
        self._report.owners += 1
        kept = self._event_norms.get(set_id)
        if kept is not None and kept[0] == (start, end):
            norm = kept[1]
        else:
            norm = self._reading(lambda c: normalize_1m(c, pair, start, end))
        if norm.unrecoverable:
            if staging:
                self._discard_counted(set_id, self._generation)
            elif self._finish(lambda begin, checkpoint:
                              ohlcv_slices.mark_event_unrecoverable(
                                  self._write_conn, set_id=set_id,
                                  now=self._now(), begin=begin,
                                  checkpoint=checkpoint)):
                self._report.unrecoverable += 1
            return False
        self._event_norms.put(set_id, ((start, end), norm))
        want = list(norm.rows)
        assumed = self._assumed_fn(pair)

        def write_batch(batch, size, begin, checkpoint):
            return ohlcv_slices.stage_event_rows(
                self._write_conn, set_id=set_id, generation=self._generation,
                rows=batch, assumed_spread_pips=assumed, max_rows=size,
                begin=begin, checkpoint=checkpoint)

        status, _, _ = self._stage(set_id=set_id, created=staging, want=want,
                                   write_batch=write_batch)
        if status == "pending":
            return True
        if status == "staged":
            self._complete(set_id, shadow=None)
        return False

    # --- shadow -----------------------------------------------------------------------

    def _shadow_owner(self, row: dict, now: datetime) -> bool:
        """進行中・待機中で次の cycle に続くなら `True` (判定を保持する)。"""
        if row["set_id"] is not None and row["set_state"] != \
                ohlcv_slices.STATE_STAGING:
            return False
        plan = self._shadow_plan(row, now)
        if plan is None:
            return True
        self._report.owners += 1
        cas = ShadowCas(row["id"], row["state"], row["updated_at"],
                        plan.outcome)
        if isinstance(plan, _EmptyPlan):
            if self._finish(lambda begin, checkpoint:
                            signal_shadow.transition_tx(
                                self._write_conn, row["id"],
                                expected_state=cas.expected_state,
                                expected_updated_at=cas.expected_updated_at,
                                outcome=cas.outcome, now=self._now(),
                                begin=begin, checkpoint=checkpoint)):
                self._report.expired_empty += 1
            return False
        if plan.unrecoverable:
            set_id = row["set_id"]
            if set_id is not None:
                stored = self._reading(lambda c: ohlcv_slices.get_set(c, set_id))
                if stored is not None:
                    if stored["generation"] != self._generation:
                        return False
                    if not self._discard(set_id, self._generation):
                        return False
                    self._report.discarded += 1
            if self._finish(lambda begin, checkpoint:
                            ohlcv_slices.mark_shadow_unrecoverable(
                                self._write_conn, shadow=cas, pair=row["pair"],
                                range_start=plan.t0, range_end=plan.range_end,
                                now=self._now(), begin=begin,
                                checkpoint=checkpoint)):
                self._report.unrecoverable += 1
            return False
        set_id = row["set_id"]
        if set_id is not None:
            stored = self._reading(lambda c: ohlcv_slices.get_set(c, set_id))
            if stored is None or stored["generation"] != self._generation:
                return False
            if (_parse(stored["range_start"]) != plan.t0
                    or _parse(stored["range_end"]) != plan.range_end):
                self._discard_counted(set_id, self._generation)
                return False

        def write_batch(batch, size, begin, checkpoint):
            return ohlcv_slices.stage_shadow_rows(
                self._write_conn, shadow_id=row["id"], pair=row["pair"],
                range_start=plan.t0, range_end=plan.range_end,
                generation=self._generation, rows=batch, now=self._now(),
                max_rows=size, begin=begin, checkpoint=checkpoint)

        status, set_id, staged = self._stage(
            set_id=set_id, created=set_id is not None, want=list(plan.want),
            write_batch=write_batch)
        if status == "pending":
            return True
        if status != "staged":
            return False
        again = resolve_shadow(
            staged, direction=plan.direction,
            kind=plan.kind, requested_price=plan.requested_price,
            stop_loss=plan.stop_loss, take_profit=plan.take_profit,
            spread=plan.spread, t0=plan.t0, pip_size=plan.pip_size)
        if again != plan.resolution:
            self._discard_counted(set_id, self._generation)
            return False
        self._complete(set_id, shadow=cas)
        return False

    def _shadow_plan(self, row: dict, now: datetime):
        shadow_id = row["id"]
        payload = json.loads(row["payload_json"])
        direction = payload["direction"]
        kind = payload["entry_type"]
        requested = payload.get("limit_price") if kind == "limit" else None
        stop_loss = payload["stop_loss"]
        take_profit = payload.get("take_profit")
        pip_size = row["pip_size"]
        spread = row["spread_pips"] * pip_size
        pair = row["pair"]
        t0 = signal_shadow.observation_start(_parse(row["decided_at"]))

        def resolve(rows):
            return resolve_shadow(
                rows, direction=direction, kind=kind, requested_price=requested,
                stop_loss=stop_loss, take_profit=take_profit, spread=spread,
                t0=t0, pip_size=pip_size)

        def plan(inp: NormalizedBars, end: datetime) -> _ShadowPlan:
            res = resolve(inp)
            if res.range_end != end:
                raise ValueError(
                    f"resolution range_end {res.range_end} != planned {end}")
            rows = inp.rows
            built = _ShadowPlan(
                t0, end, res, _outcome(res),
                tuple(r for r in rows if r.bar_time < end),
                inp.unrecoverable, kind, direction, requested, stop_loss,
                take_profit, spread, pip_size)
            self._shadow_plans.put(shadow_id, ((t0, end), built))
            return built

        def kept_plan(end: datetime):
            kept = self._shadow_plans.get(shadow_id)
            if kept is not None and kept[0] == (t0, end):
                return kept[1]
            return None

        if kind == "market":
            end = day_close_deadline(t0)
            if t0 >= end:
                return _EmptyPlan(signal_shadow.ShadowOutcome.no_fill())
            if now < end + FINALIZE_GRACE:
                return None
            return kept_plan(end) or plan(
                self._reading(lambda c: normalize_1m(c, pair, t0, end)), end)
        first_end = t0 + _LIMIT_WINDOW
        if now < first_end + FINALIZE_GRACE:
            return None
        stage = self._limit_first.get(shadow_id)
        if stage is None or stage[0] != (t0, first_end):
            first = self._reading(lambda c: normalize_1m(c, pair, t0, first_end))
            if first.unrecoverable:
                end = first_end
            else:
                end = select_limit_entry(
                    first, t0=t0, requested_price=requested,
                    direction=direction, spread=spread).range_end
            stage = ((t0, first_end), first, end)
            self._limit_first.put(shadow_id, stage)
        _, first, end = stage
        if now < end + FINALIZE_GRACE:
            return None
        found = kept_plan(end)
        if found is not None:
            return found
        if end <= first_end:
            # 前段の正規化が [t0,end) を覆う (staging するのは [t0,end) だけ)。
            return plan(first, end)
        return plan(self._reading(lambda c: normalize_1m(c, pair, t0, end)),
                    end)

    # --- staging の共通部 --------------------------------------------------------------

    def _stage(self, *, set_id: int | None, created: bool,
               want: Sequence[SliceRow], write_batch):
        """期待の行を batch に分けて staging し、読み戻して照合する。

        `(status, set_id, staged)`。status は `staged` (照合が一致、完備 CAS へ)、
        `pending` (予算・見送りで次 cycle に続く)、`discarded` (不一致で全部捨てた)、
        `conflict` (他の worker か状態の変化)。
        """
        size = MAX_BATCH_ROWS
        have: set[datetime] = set()
        check = created
        while True:
            if self._stopped():
                return "pending", set_id, []
            if check:
                staged = self._reading(lambda c: ohlcv_slices.staging_rows(
                    c, set_id=set_id, generation=self._generation)) or []
                verdict = _classify(staged, want)
                if verdict == "mismatch":
                    self._discard_counted(set_id, self._generation)
                    return "discarded", set_id, staged
                if verdict == "match":
                    return "staged", set_id, staged
                have = {row.bar_time for row in staged}
                check = False
            missing = [row for row in want if row.bar_time not in have]
            batch = missing[:size]
            result = self._write(lambda begin, checkpoint: write_batch(
                batch, size, begin, checkpoint))
            if not isinstance(result, TxResult):
                return "pending", set_id, []
            if result.status == "over_budget":
                if size == 1:
                    return "pending", set_id, []
                size = max(1, size // 2)
                continue
            if not result.committed:
                return ("conflict" if result.status == "conflict" else
                        "pending"), set_id, []
            set_id = result.set_id
            have.update(row.bar_time for row in batch)
            if len(batch) == len(missing):
                self._before_readback(set_id)
                check = True

    def _before_readback(self, set_id: int) -> None:
        """最後の batch を commit した後、読戻しの前。"""

    def _discard(self, set_id: int, generation: int, *,
                 stop: threading.Event | None = None) -> bool:
        """staging の行を batch ごとに消し尽くす (event は awaiting に戻り、shadow は
        set ごと消える)。他が先に片付けていれば成功扱い。`stop` で中断すれば `False`。"""
        size = MAX_BATCH_ROWS
        while True:
            if self._bounded and self._stopped():
                return False
            result = self._write(lambda begin, checkpoint:
                                 ohlcv_slices.discard_staging_step(
                                     self._write_conn, set_id=set_id,
                                     generation=generation, max_rows=size,
                                     begin=begin, checkpoint=checkpoint),
                                 stop=stop)
            if not isinstance(result, TxResult):
                if self._bounded:
                    return False
                if not self._wait_retry(stop):
                    return False
                continue
            if result.status == "over_budget" and size > 1:
                size = max(1, size // 2)
                continue
            if result.status == "conflict":
                return True
            if not result.committed:
                if self._bounded:
                    return False
                if not self._wait_retry(stop):
                    return False
                continue
            if result.done:
                return True

    def _discard_counted(self, set_id: int, generation: int) -> None:
        if self._discard(set_id, generation):
            self._report.discarded += 1

    # --- 完備 CAS ------------------------------------------------------------------------

    def _finish(self, op) -> bool:
        """core lock の中で終端の 1 transaction を行い、commit できたかを返す。"""
        return self._committed(self._write(op, locked=True))

    def _complete(self, set_id: int, shadow: ShadowCas | None) -> None:
        committed = self._finish(lambda begin, checkpoint:
                                 ohlcv_slices.complete_set(
                                     self._write_conn, set_id=set_id,
                                     generation=self._generation,
                                     now=self._now(), shadow=shadow,
                                     begin=begin, checkpoint=checkpoint))
        if not committed:
            return
        self._report.completed.append(set_id)
        if self._on_complete is None:
            return
        try:
            row = self._reading(lambda c: ohlcv_slices.get_set(c, set_id))
            self._on_complete(row)
        except Exception:
            _log.exception("econ slice on_complete failed: set %s", set_id)

