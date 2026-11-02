"""signal 専用 mission の起動条件と、待機上限の静的保証・実測 warning。

- `signal_due`: 起動条件は「eligible な pending が鮮度内にある」かつ「signal 起動の
  rate 内」。建玉・約定待ち注文の有無は見ない。eligible と鮮度は
  `signals.pending_exists` が claim と同じ分類式で判定し、rate は
  `missions.signals_rate_ok` (帯内の `system_filtered` は数えない)。
- `static_guarantee`: 最大値の式による「鮮度内の採否を保証できない」の恒常表示。
  activity は増やさない。
- `WaitBudgetEvaluator`: 直近 14 運用日の実測 (または標本不足時の代用式) で
  `signal_wait_budget_warning` を設定 fingerprint × 運用日で 1 回だけ出す。評価は
  別 thread (mission の commit 後) からも呼ばれるので、自分の接続と lock だけを使う。
- `CapacityMonitor`: signal の日次容量 S と、承認済み strategy から導く
  `S_producer` の比較。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Sequence

from agentic_fx._safe_error import safe_error_text
from agentic_fx.activity import ActivityLog, Category
from agentic_fx.backtest.timeframes import TF_MINUTES
from agentic_fx.core import econ_window, market_hours, mission_ceiling
from agentic_fx.core.scheduler import TICK_INTERVAL_SEC
from agentic_fx.core.writer_admission import WriterAdmission
from agentic_fx.store import (
    alert_state, econ_events, fetch_attempts, missions, ohlcv_slices, signals,
)

_log = logging.getLogger("agentic_fx.signal_admission")

WAIT_WINDOW_DAYS = 14
DIRECT_MIN_SAMPLES = 100
# 配備済み strategy の取り直しが失敗し続けるときの間隔 (指数、上限 15 分)
REFRESH_BACKOFF_BASE_SEC = 60.0
REFRESH_BACKOFF_MAX_SEC = 900.0
FRESHNESS_ABANDON_REASONS = (signals.REASON_FRESHNESS_EXPIRED,
                             signals.REASON_REQUEUED_EXPIRED)

TERMINAL_DECISION = "decision"
TERMINAL_FRESHNESS_ABANDONED = "freshness_abandoned"
TERMINAL_KINDS = (TERMINAL_DECISION, TERMINAL_FRESHNESS_ABANDONED)

BASIS_DIRECT = "direct"
BASIS_PROXY = "proxy"
STATE_INACTIVE = "inactive"
STATE_OK = "ok"
STATE_ACTIVE = "active"

WARNING_EVENT = "signal_wait_budget_warning"
CALENDAR_STALE_EVENT = "econ_calendar_stale"
CALENDAR_FRESH_EVENT = "econ_calendar_fresh"


# --- 起動条件 ------------------------------------------------------------

def signal_due(conn: sqlite3.Connection, settings, now: datetime) -> bool:
    """専用 signal mission を起動してよいか。

    eligible (kind='strategy' かつ契約版あり) な pending が鮮度内にあり、signal 起動
    の rate (最短間隔・日次上限) に収まるときだけ真。建玉・注文の有無は見ない。
    """
    if not signals.pending_exists(
            conn, now=now, freshness_bars=settings.plugin.signal_freshness_bars):
        return False
    return missions.signals_rate_ok(conn, now, settings)


# --- 静的保証 ------------------------------------------------------------

def strategy_widths_sec(strategies: Sequence) -> list[int]:
    """承認済み strategy の足幅 (秒)。timeframe を持たないものは数えない。"""
    return [TF_MINUTES[m.timeframe] * 60 for m in strategies
            if getattr(m, "kind", None) == "strategy"
            and getattr(m, "timeframe", None) in TF_MINUTES]


@dataclass(frozen=True, slots=True)
class StaticGuarantee:
    """`unavailable` が None なら承認済み strategy が 0 本 (n/a)。"""
    unavailable: bool | None
    min_width_sec: int | None
    freshness_bars: int
    dispatch_sec: float
    tick_sec: float
    rate_sec: float
    worker_sec: float
    plugin_lock_sec: float
    decision_width_sec: float
    total_sec: float
    budget_sec: float | None
    by_budget: bool
    by_decision_width: bool

    @property
    def status(self):
        return "n/a" if self.unavailable is None else self.unavailable

    def to_dict(self) -> dict:
        return {"worst_case_guarantee_unavailable": self.status,
                "min_width_sec": self.min_width_sec, "total_sec": self.total_sec,
                "budget_sec": self.budget_sec, "dispatch_sec": self.dispatch_sec,
                "decision_width_sec": self.decision_width_sec}


def static_guarantee(settings, strategies: Sequence) -> StaticGuarantee:
    """`Cd + 3T + R + Cw + Pl >= w(k-1)` または `Cd + T >= 判断足幅` なら保証不能。

    w は承認済み strategy の最小足幅。承認済み strategy が 0 本なら n/a。
    """
    cd = mission_ceiling.dispatch_ceiling_sec(settings)
    cw = mission_ceiling.worker_ceiling(settings).total
    tick = float(TICK_INTERVAL_SEC)
    rate = settings.plugin.signal_min_interval_min * 60.0
    pl = float(settings.worker.plugin_lock_timeout_sec)
    k = settings.plugin.signal_freshness_bars
    decision_width = settings.datafeed.decision_timeframe_width.total_seconds()
    total = cd + 3 * tick + rate + cw + pl
    widths = strategy_widths_sec(strategies)
    if not widths:
        return StaticGuarantee(
            None, None, k, cd, tick, rate, cw, pl, decision_width, total, None,
            False, False)
    w = min(widths)
    budget = float(w * (k - 1))
    by_budget = total >= budget
    by_decision = cd + tick >= decision_width
    return StaticGuarantee(
        by_budget or by_decision, w, k, cd, tick, rate, cw, pl, decision_width,
        total, budget, by_budget, by_decision)


# --- 配備済み strategy の実行時の正本 -------------------------------------

class DeployedStrategies:
    """S と評価器が見る「いま配備済みの strategy 集合」。

    `current()` は直近に読んだ集合 (起動時は起動時の承認集合) を返すだけで、DB に
    触れない。`refresh(now)` は scheduler thread が毎 tick 呼び、運用日の境界を
    越えたとき、または deploy op の表が前回から変わったとき (最大 op_id と最大
    updated_at を見る 1 本の SQL) にだけ `load_fn` で集合を取り直す。取り直しに失敗
    したら前の集合を保ち、次の tick で再び試す。
    """

    def __init__(self, initial: Sequence, *, load_fn: Callable[[], Sequence],
                 version_fn: Callable[[], object]) -> None:
        self._current = tuple(initial)
        self._load_fn = load_fn
        self._version_fn = version_fn
        self._seen: tuple[str, object] | None = None
        self._lock = threading.Lock()
        self._failures = 0
        self._retry_at: datetime | None = None
        self._last_error: str | None = None

    def current(self) -> list:
        with self._lock:
            return list(self._current)

    def refresh(self, now: datetime) -> bool:
        """集合を取り直したら True。"""
        if self._retry_at is not None and now < self._retry_at:
            return False
        try:
            marker = (operating_day(now), self._version_fn())
            if self._seen == marker:
                return False
            loaded = tuple(self._load_fn())
        except Exception as exc:  # noqa: BLE001 — 取り直しの失敗で tick を止めない
            self._failures += 1
            self._retry_at = now + timedelta(
                seconds=min(REFRESH_BACKOFF_BASE_SEC * 2 ** (self._failures - 1),
                            REFRESH_BACKOFF_MAX_SEC))
            text = safe_error_text(exc)
            if text != self._last_error:
                _log.warning("deployed strategies refresh failed: %s", text)
            self._last_error = text
            return False
        self._failures, self._retry_at, self._last_error = 0, None, None
        with self._lock:
            self._current = loaded
        self._seen = marker
        return True


# --- 容量 ----------------------------------------------------------------

class CapacityMonitor:
    """signal の日次容量 S と `S_producer` の比較。

    承認済み strategy は呼ぶたびに `strategies_fn` から読み直す。起動時に上書きが
    `S_producer` を下回れば起動を拒否し、実行中に下回れば S を `S_producer` に切り
    上げて `capacity_override_below_producer` を立てる。
    """

    def __init__(self, settings, strategies_fn: Callable[[], Sequence]) -> None:
        self._settings = settings
        self._strategies_fn = strategies_fn

    def plan(self) -> ohlcv_slices.CapacityPlan:
        s = self._settings
        return ohlcv_slices.capacity_plan(
            override=s.econ.signal_capacity_daily_max,
            strategies=self._strategies_fn(), settings_pairs=s.pairs,
            importance_min=s.econ.window.importance_min)

    def check_startup(self) -> None:
        """上書きが `S_producer` 未満なら `CapacityOverrideBelowProducer`。"""
        s = self._settings
        ohlcv_slices.check_startup_capacity(
            override=s.econ.signal_capacity_daily_max,
            producer=ohlcv_slices.producer_daily_signal_max(
                self._strategies_fn(), list(s.pairs)))

    def snapshot(self) -> dict:
        plan = self.plan()
        cap = plan.signals
        return {"capacity_override_below_producer": cap.below_producer,
                "signal_daily": cap.value, "producer_daily": cap.producer,
                "override": cap.override}


# --- 実測 warning --------------------------------------------------------

def percentile95(values: Sequence[float]) -> float | None:
    """nearest-rank の p95。標本が無ければ None。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = (95 * len(ordered) + 99) // 100
    return ordered[max(rank, 1) - 1]


def _aware(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def operating_day(now: datetime) -> str:
    """signal の日次上限と同じ境界 (取引日の開始) の運用日ラベル。"""
    return market_hours.trading_day_start(now).isoformat()


def window_start(now: datetime) -> datetime:
    """直近 14 運用日の始点 (当日を含む 14 取引日の先頭)。"""
    return market_hours.trading_day_start(now) - timedelta(days=WAIT_WINDOW_DAYS - 1)


@dataclass(frozen=True, slots=True)
class WaitBudgetSnapshot:
    state: str
    basis: str | None
    samples: int
    direct_samples: int
    claim_p95_sec: float | None
    decision_p95_sec: float | None
    dispatch_p95_sec: float | None
    estimate_sec: float | None
    budget_sec: float | None
    freshness_abandoned: int
    reasons: tuple[str, ...]
    fingerprint: str | None
    op_day: str
    evaluated_at: str
    trigger: str

    def to_dict(self) -> dict:
        return {"state": self.state, "basis": self.basis, "samples": self.samples,
                "direct_samples": self.direct_samples,
                "claim_p95_sec": self.claim_p95_sec,
                "decision_p95_sec": self.decision_p95_sec,
                "dispatch_p95_sec": self.dispatch_p95_sec,
                "estimate_sec": self.estimate_sec, "budget_sec": self.budget_sec,
                "freshness_abandoned": self.freshness_abandoned,
                "reasons": list(self.reasons), "fingerprint": self.fingerprint,
                "op_day": self.op_day, "evaluated_at": self.evaluated_at,
                "trigger": self.trigger}


def settings_fingerprint(*, width_sec: int, freshness_bars: int, rate_sec: float,
                         worker_sec: float) -> str:
    """判定に使う設定値の canonical fingerprint。値が変われば別の warning。"""
    payload = json.dumps(
        {"w": width_sec, "k": freshness_bars, "R": rate_sec, "Cw": worker_sec,
         "days": WAIT_WINDOW_DAYS, "min_samples": DIRECT_MIN_SAMPLES},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class WaitBudgetEvaluator:
    """`signal_wait_budget_warning` の評価器。

    評価時点は 4 つ: 起動時 (`evaluate_wait_budget`)、運用日境界と承認 strategy 集合の
    変更 (`tick`)、signal の終端確定時 (`on_signal_terminal`)。どれも例外を漏らさず、
    自分の接続 `conn` を自分の lock の下でだけ使う (呼び出し側の core lock は不要)。
    """

    def __init__(self, *, conn: sqlite3.Connection, settings,
                 strategies_fn: Callable[[], Sequence], activity: ActivityLog,
                 now_fn: Callable[[], datetime],
                 writer_admission: WriterAdmission | None = None) -> None:
        self._conn = conn
        self._writer = writer_admission
        self._settings = settings
        self._strategies_fn = strategies_fn
        self._activity = activity
        self._now_fn = now_fn
        self._lock = threading.Lock()
        self._seen: tuple[str, frozenset] | None = None
        # 書けずに取り直す評価の、元の trigger
        self._retry_trigger: str | None = None
        # BUSY の技術 warning を出した運用日
        self._busy_warned_day: str | None = None

    # --- 公開 API ---

    def evaluate_wait_budget(self, now: datetime | None = None,
                             *, trigger: str = "startup") -> WaitBudgetSnapshot | None:
        """全評価時点の共通入口。失敗したら None (技術ログのみ)。"""
        now = self._now_fn() if now is None else now
        try:
            with self._lock:
                return self._evaluate(now, trigger)
        except Exception as exc:  # noqa: BLE001 — 評価の失敗で取引を止めない
            _log.warning("wait budget evaluation failed: %s", safe_error_text(exc))
            return None

    def on_signal_terminal(self, signal_id: int | None, kind: str,
                           ) -> WaitBudgetSnapshot | None:
        """signal の終端 (最終 decision か freshness 系 abandoned) の commit 後に
        core lock の外から呼ぶ。未知の kind は何もしない。"""
        if kind not in TERMINAL_KINDS:
            return None
        return self.evaluate_wait_budget(trigger=kind)

    def tick(self, now: datetime) -> WaitBudgetSnapshot | None:
        """運用日境界の通過と承認 strategy 集合の変更で再評価する。"""
        current = (operating_day(now), self._identity())
        if self._seen is not None and self._seen == current:
            return None
        return self.evaluate_wait_budget(
            now, trigger=self._retry_trigger or "boundary")

    # --- 内部 ---

    def _identity(self) -> frozenset:
        return frozenset((m.name, m.content_hash) for m in self._strategies_fn()
                         if getattr(m, "kind", None) == "strategy")

    def _evaluate(self, now: datetime, trigger: str) -> WaitBudgetSnapshot:
        snapshot = self._compute(now, trigger)
        self._seen = (snapshot.op_day, self._identity())
        try:
            saved, notify = self._persist(snapshot, now)
        except Exception as exc:  # noqa: BLE001 — 記録できなければ通知しない
            if not self._is_busy(exc) or self._busy_warned_day != snapshot.op_day:
                _log.warning("wait budget not recorded: %s", safe_error_text(exc))
            if self._is_busy(exc):
                # BUSY の再評価は毎 tick 続けるが、技術ログは運用日に 1 回
                self._busy_warned_day = snapshot.op_day
            saved, notify = False, False
        if not saved:
            # scheduler の書込み要求中、または書込みの失敗。次の tick の `tick()` で、
            # 元の trigger のまま取り直す
            self._seen = None
            self._retry_trigger = self._retry_trigger or trigger
            return snapshot
        self._retry_trigger = None
        if notify:
            self._write_warning(snapshot)
        return snapshot

    @staticmethod
    def _is_busy(exc: BaseException) -> bool:
        text = str(exc).lower()
        return isinstance(exc, sqlite3.OperationalError) and (
            "locked" in text or "busy" in text)

    def _persist(self, snapshot: WaitBudgetSnapshot, now: datetime,
                 ) -> tuple[bool, bool]:
        """snapshot と通知済みの記録を 1 つの transaction で書く。

        戻り値は (書いた, 今回初めて通知する)。scheduler の書込み要求フラグが立って
        いれば `BEGIN IMMEDIATE` を出さず (False, False)。
        """
        conn = self._conn

        def begin() -> None:
            conn.execute("BEGIN IMMEDIATE")

        if self._writer is None:
            return self._write(snapshot, now, begin)
        with self._writer.lease(begin) as admitted:
            if not admitted:
                return False, False
            return self._write_in_tx(snapshot, now)

    def _write(self, snapshot: WaitBudgetSnapshot, now: datetime,
               begin: Callable[[], None]) -> tuple[bool, bool]:
        begin()
        return self._write_in_tx(snapshot, now)

    def _write_in_tx(self, snapshot: WaitBudgetSnapshot, now: datetime,
                     ) -> tuple[bool, bool]:
        conn = self._conn
        try:
            alert_state.set(conn, alert_state.WAIT_BUDGET_SNAPSHOT_KEY,
                            json.dumps(snapshot.to_dict(), sort_keys=True),
                            now=now, commit=False)
            notify = (snapshot.state == STATE_ACTIVE
                      and self._record_notice(snapshot, now))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return True, notify

    def _record_notice(self, snapshot: WaitBudgetSnapshot, now: datetime) -> bool:
        """(運用日, fingerprint) 1 組ごとの永続 key で dedupe する。初めての組なら
        記録して True。過去の運用日の key はここで消す。"""
        conn = self._conn
        key = alert_state.wait_budget_notified_key(snapshot.op_day, snapshot.fingerprint)
        if alert_state.get(conn, key) is not None:
            return False
        stale = [k for k in alert_state.keys_with_prefix(
            conn, alert_state.WAIT_BUDGET_NOTIFIED_PREFIX)
            if not k.startswith(
                alert_state.WAIT_BUDGET_NOTIFIED_PREFIX + snapshot.op_day + "|")]
        for old in stale:
            alert_state.delete(conn, old, commit=False)
        record = json.dumps({"fingerprint": snapshot.fingerprint,
                             "day": snapshot.op_day, "at": now.isoformat()},
                            sort_keys=True)
        alert_state.set(conn, key, record, now=now, commit=False)
        alert_state.set(conn, alert_state.WAIT_BUDGET_NOTIFIED_KEY, record,
                        now=now, commit=False)
        return True

    def _compute(self, now: datetime, trigger: str) -> WaitBudgetSnapshot:
        s = self._settings
        widths = strategy_widths_sec(self._strategies_fn())
        day = operating_day(now)
        stamp = now.isoformat()
        if not widths:
            return WaitBudgetSnapshot(
                STATE_INACTIVE, None, 0, 0, None, None, None, None, None, 0, (),
                None, day, stamp, trigger)
        w = min(widths)
        k = s.plugin.signal_freshness_bars
        budget = float(w * (k - 1))
        rate = s.plugin.signal_min_interval_min * 60.0
        cw = mission_ceiling.worker_ceiling(s).total
        since = window_start(now)
        claim, decide = self._llm_latencies(since)
        abandoned = self._freshness_abandoned(since)
        reasons: list[str] = []
        if len(claim) >= DIRECT_MIN_SAMPLES:
            basis, samples = BASIS_DIRECT, len(claim)
            p_claim, p_decide = percentile95(claim), percentile95(decide)
            estimate = p_claim + p_decide
            dispatch = None
        else:
            basis = BASIS_PROXY
            p_claim = p_decide = None
            dispatches = self._dispatch_seconds(since)
            samples = len(dispatches)
            dispatch = percentile95(dispatches)
            estimate = None if dispatch is None else dispatch + rate + cw
        if estimate is not None and estimate >= budget:
            reasons.append("latency")
        if abandoned >= 1:
            reasons.append("freshness_abandoned")
        return WaitBudgetSnapshot(
            STATE_ACTIVE if reasons else STATE_OK, basis, samples, len(claim),
            p_claim, p_decide, dispatch, estimate, budget, abandoned,
            tuple(reasons),
            settings_fingerprint(width_sec=w, freshness_bars=k, rate_sec=rate,
                                 worker_sec=cw),
            day, stamp, trigger)

    def _llm_latencies(self, since: datetime) -> tuple[list[float], list[float]]:
        rows = self._conn.execute(
            "SELECT s.created_at, s.claimed_at, d.decided_at "
            "FROM signal_decisions d JOIN signals s ON s.id=d.signal_id "
            "WHERE d.actor='llm' AND d.decided_at >= ?",
            ((since - timedelta(days=1)).isoformat(),)).fetchall()
        claim: list[float] = []
        decide: list[float] = []
        for created_raw, claimed_raw, decided_raw in rows:
            created, claimed, decided = (
                _aware(created_raw), _aware(claimed_raw), _aware(decided_raw))
            if created is None or claimed is None or decided is None or decided < since:
                continue
            first, second = (claimed - created).total_seconds(), (
                decided - claimed).total_seconds()
            if first < 0 or second < 0:
                continue
            claim.append(first)
            decide.append(second)
        return claim, decide

    def _freshness_abandoned(self, since: datetime) -> int:
        marks = ",".join("?" * len(FRESHNESS_ABANDON_REASONS))
        rows = self._conn.execute(
            f"SELECT abandoned_at FROM signals WHERE status='abandoned' "
            f"AND abandon_reason IN ({marks}) AND abandoned_at >= ?",
            (*FRESHNESS_ABANDON_REASONS, (since - timedelta(days=1)).isoformat()),
        ).fetchall()
        count = 0
        for (raw,) in rows:
            at = _aware(raw)
            if at is not None and at >= since:
                count += 1
        return count

    def _dispatch_seconds(self, since: datetime) -> list[float]:
        """終了済み trade mission の開始から、同じ dispatch の最後の reflection の
        終了 (無ければ trade 自身の終了) までの秒数。

        reflection は capacity 1 の直列性により、対象 trade の終了以後かつ次の trade
        の開始より前に始まったものを、その trade に結ぶ。skipped (LLM を呼ばない) は
        標本に入れないが、次の trade の開始の境界には使う。
        """
        margin = (since - timedelta(days=1)).isoformat()
        trades = []
        for mid, status, started_raw, finished_raw in self._conn.execute(
                "SELECT id, status, started_at, finished_at FROM missions "
                "WHERE loop='trade' AND started_at >= ? ORDER BY started_at, id",
                (margin,)):
            started = _aware(started_raw)
            if started is not None:
                trades.append((started, status, _aware(finished_raw)))
        reflections = []
        for started_raw, finished_raw in self._conn.execute(
                "SELECT started_at, finished_at FROM missions "
                "WHERE loop='reflection' AND started_at >= ? ORDER BY started_at, id",
                (margin,)):
            started = _aware(started_raw)
            if started is not None:
                reflections.append((started, _aware(finished_raw)))
        out: list[float] = []
        for index, (started, status, finished) in enumerate(trades):
            if (started < since or finished is None
                    or status in ("running", "skipped")):
                continue
            next_start = trades[index + 1][0] if index + 1 < len(trades) else None
            end = finished
            for r_start, r_finish in reflections:
                if r_start < finished:
                    continue
                if next_start is not None and r_start >= next_start:
                    break
                if r_finish is not None and r_finish > end:
                    end = r_finish
            seconds = (end - started).total_seconds()
            if seconds >= 0:
                out.append(seconds)
        return out

    def _write_warning(self, snapshot: WaitBudgetSnapshot) -> None:
        try:
            self._activity.write(
                Category.SYSTEM, WARNING_EVENT,
                f"採否の待機が鮮度の余地に収まりません: 判定={snapshot.basis} "
                f"標本={snapshot.samples} 推定={_fmt(snapshot.estimate_sec)} 秒 / "
                f"余地={_fmt(snapshot.budget_sec)} 秒 / freshness 系 abandoned="
                f"{snapshot.freshness_abandoned} ({', '.join(snapshot.reasons)})")
        except Exception as exc:  # noqa: BLE001 — 記録は済んでいる。再送はしない
            _log.warning("wait budget warning not written: %s", safe_error_text(exc))


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


def read_wait_budget(conn: sqlite3.Connection) -> dict | None:
    """status 用: 直近の評価結果と最後の通知時刻。評価がまだなら None。"""
    snap = conn.execute("SELECT value FROM alert_state WHERE key=?",
                        (alert_state.WAIT_BUDGET_SNAPSHOT_KEY,)).fetchone()
    if snap is None:
        return None
    try:
        data = json.loads(snap[0])
    except ValueError:
        return None
    note = conn.execute("SELECT value FROM alert_state WHERE key=?",
                        (alert_state.WAIT_BUDGET_NOTIFIED_KEY,)).fetchone()
    last_notified = None
    if note is not None:
        try:
            last_notified = json.loads(note[0]).get("at")
        except (ValueError, AttributeError):
            last_notified = None
    if isinstance(data, dict):
        data["last_notified_at"] = last_notified
    return data


# --- calendar の鮮度 -----------------------------------------------------

class CalendarFreshnessNotice:
    """鮮度が stale に変わったときと stale から fresh に戻ったときだけ activity。

    最後に通知した状態を `alert_state` に残すので、再起動しても同じ状態では書かない。
    unknown は通知せず、前の状態も変えない。
    """

    def __init__(self, *, conn: sqlite3.Connection,
                 freshness_fn: Callable[[datetime], str],
                 activity: ActivityLog) -> None:
        self._conn = conn
        self._freshness_fn = freshness_fn
        self._activity = activity

    def observe(self, now: datetime) -> str | None:
        """通知した event 名 (無ければ None)。"""
        state = self._freshness_fn(now)
        if state not in (econ_window.FRESHNESS_STALE, econ_window.FRESHNESS_FRESH):
            return None
        last = alert_state.get(self._conn, alert_state.CALENDAR_FRESHNESS_KEY)
        if state == last:
            return None
        alert_state.set(self._conn, alert_state.CALENDAR_FRESHNESS_KEY, state, now=now)
        if state == econ_window.FRESHNESS_STALE:
            self._activity.write(
                Category.NEWS, CALENDAR_STALE_EVENT,
                "経済指標カレンダーが 12 時間を超えて更新されていません (保存済みの "
                "event では帯を作り、未取得の event は仮定しません)")
            return CALENDAR_STALE_EVENT
        if last == econ_window.FRESHNESS_STALE:
            self._activity.write(Category.NEWS, CALENDAR_FRESH_EVENT,
                                 "経済指標カレンダーの更新が回復しました")
            return CALENDAR_FRESH_EVENT
        return None


# --- status --------------------------------------------------------------

def window_status(conn: sqlite3.Connection, now: datetime, *,
                  window: econ_window.WindowSettings,
                  pair_currencies: Mapping[str, tuple[str, str]]) -> dict:
    """読み取り接続だけで、いまの回避帯 (pair 別と全体)。"""
    pairs: dict[str, dict] = {}
    active_ids: set[int] = set()
    for pair, currencies in pair_currencies.items():
        rule = econ_window.WindowRule.for_currencies(window, currencies)
        candidates = econ_events.window_candidates(
            conn, now, settings=window, currencies=sorted(rule.currencies))
        active = econ_window.active_events(now, candidates, rule)
        active_ids.update(e.id for e in active)
        pairs[pair] = {"in_window": bool(active), "active_events": len(active)}
    freshness = fetch_attempts.freshness(conn, fetch_attempts.ECON_KEY, now)
    return {"in_window": bool(active_ids), "active_events": len(active_ids),
            "freshness": freshness, "pairs": pairs}


class SignalAdmission:
    """起動条件に関わる観測の束。status はここから組み立てる。"""

    def __init__(self, *, settings, strategies_fn: Callable[[], Sequence],
                 pair_currencies: Mapping[str, tuple[str, str]],
                 capacity: CapacityMonitor,
                 wait_budget: WaitBudgetEvaluator | None = None,
                 calendar_notice: CalendarFreshnessNotice | None = None,
                 suppressed_limits_fn: Callable[[], int] | None = None) -> None:
        self.settings = settings
        self.suppressed_limits_fn = suppressed_limits_fn
        self.strategies_fn = strategies_fn
        self.pair_currencies = dict(pair_currencies)
        self.capacity = capacity
        self.wait_budget = wait_budget
        self.calendar_notice = calendar_notice

    def static_guarantee(self) -> StaticGuarantee:
        return static_guarantee(self.settings, self.strategies_fn())

    def status(self, conn: sqlite3.Connection, now: datetime) -> dict:
        """`afx ctl status` に足すキー。読み取り接続だけを使い、節ごとに失敗を隔離する。"""
        out: dict = {}
        try:
            out["worst_case_guarantee_unavailable"] = self.static_guarantee().status
        except Exception as exc:  # noqa: BLE001
            _log.warning("static guarantee unavailable: %s", safe_error_text(exc))
            out["worst_case_guarantee_unavailable"] = None
        try:
            cap = self.capacity.snapshot()
            out["capacity_override_below_producer"] = cap[
                "capacity_override_below_producer"]
            out["signal_capacity"] = {
                "daily": cap["signal_daily"], "producer": cap["producer_daily"],
                "override": cap["override"]}
        except Exception as exc:  # noqa: BLE001
            _log.warning("capacity unavailable: %s", safe_error_text(exc))
            out["capacity_override_below_producer"] = None
        try:
            out["wait_budget"] = read_wait_budget(conn)
        except Exception as exc:  # noqa: BLE001
            _log.warning("wait budget unavailable: %s", safe_error_text(exc))
            out["wait_budget"] = {"unavailable": True}
        try:
            out["window"] = window_status(
                conn, now, window=econ_window.WindowSettings.from_config(
                    self.settings.econ.window),
                pair_currencies=self.pair_currencies)
        except Exception as exc:  # noqa: BLE001
            _log.warning("window status unavailable: %s", safe_error_text(exc))
            out["window"] = {"unavailable": True}
        try:
            out["suppressed_limits"] = (
                None if self.suppressed_limits_fn is None
                else self.suppressed_limits_fn())
        except Exception as exc:  # noqa: BLE001
            _log.warning("suppressed limits unavailable: %s", safe_error_text(exc))
            out["suppressed_limits"] = None
        return out
