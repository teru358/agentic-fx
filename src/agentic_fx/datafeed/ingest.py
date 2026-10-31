"""Bounded, closed-bar ingestion for the scheduler thread."""
from __future__ import annotations

import inspect
import logging
import time
from types import MappingProxyType
from typing import Mapping
from datetime import datetime, timedelta, timezone

import httpx

from agentic_fx.core import market_hours
from agentic_fx.datafeed.closed_bars import normalize_closed_range
from agentic_fx.datafeed.outage import EMPTY_REPORT, IngestTickReport
from agentic_fx.datafeed.planner import RequirementPlanner
from agentic_fx.datafeed.requirements import build_registry
from agentic_fx.datafeed import sources
from agentic_fx.store.ohlcv import upsert_cache_bars

_log = logging.getLogger("agentic_fx.ingest")


# Soft (reference) keys back off from this many seconds, doubling per consecutive
# empty / failed / not-advancing attempt, and never beyond one bar width.
SOFT_BACKOFF_BASE_SEC = 60


def soft_backoff_delay(attempt: int, width_sec: float) -> float:
    """Capped exponential delay after the ``attempt``-th consecutive miss (1-based)."""
    return min(width_sec, SOFT_BACKOFF_BASE_SEC * 2 ** (attempt - 1))


def classify_fetch_error(exc: BaseException) -> str:
    """``source_unavailable`` for transport faults and 5xx, otherwise ``fetch_failed``."""
    if isinstance(exc, httpx.HTTPStatusError):
        return ("source_unavailable" if exc.response.status_code >= 500
                else "fetch_failed")
    if isinstance(exc, (httpx.TransportError, OSError)):
        return "source_unavailable"
    return "fetch_failed"


class Ingest:
    """Fetch required native bars before the core critical section, then save them."""

    def __init__(self, settings, deployed_plugins=(), *, fetch=None, planner=None,
                 monotonic=time.monotonic, read_conn=None) -> None:
        self.settings = settings
        self.source = settings.datafeed.primary
        self.storage_source = "mt5-live" if self.source == "mt5" else self.source
        self.registry = build_registry(settings, deployed_plugins)
        self.planner = planner or RequirementPlanner()
        self.fetch = fetch or self._fetch_primary
        self.monotonic = monotonic
        self.read_conn = read_conn
        self.next_probe_at: dict[tuple[str, str], datetime] = {}
        self._backoff: dict[tuple[str, str], int] = {}
        self._pending: dict[tuple[str, str], list] = {}
        self._deferred: list[tuple[str, str]] = []
        self._soft_deferred: list[tuple[str, str]] = []
        self._soft_last_attempt: dict[tuple[str, str], int] = {}
        self._soft_last_request_at: dict[tuple[str, str], datetime] = {}
        self._rejected_soft: set[tuple[str, str]] = set()
        self._soft_state: dict[tuple[str, str], str] = {}
        self._attempt_serial = 0
        self.last_request_count = 0
        self.budget_exhausted = False
        self.last_errors: dict[tuple[str, str], str] = {}
        # Replaced wholesale by the scheduler thread each prepare; other threads
        # only read the reference, so no lock is needed.
        self.reference_fetch_state: Mapping[tuple[str, str], str] = MappingProxyType({})

    @property
    def keys(self):
        return self.registry.keys()

    def _is_hard(self, key) -> bool:
        """Trading-pair keys keep the per-tick schedule whatever asked for them; only other reference keys are soft."""
        return key[0] in self.settings.pairs or not self.registry.is_reference(*key)

    def _priority(self, key):
        _, interval = key
        if interval == "1m":
            return 0
        return 1 if interval in self.settings.datafeed.primary_intervals else 2

    def _ordered_hard_keys(self):
        normal = sorted((key for key in self.keys if self._is_hard(key)),
                        key=lambda key: (self._priority(key), key))
        ones = [key for key in normal if self._priority(key) == 0]
        decisions = [key for key in normal if self._priority(key) == 1]
        others = [key for key in normal if self._priority(key) == 2]
        deferred_decisions = [key for key in self._deferred if key in decisions]
        return tuple(dict.fromkeys([*deferred_decisions, *ones, *decisions, *others]))

    def _ordered_soft_keys(self):
        """Previously deferred keys first, then the least recently attempted."""
        soft = [key for key in self.keys if not self._is_hard(key)]
        deferred = set(self._soft_deferred)
        return tuple(sorted(soft, key=lambda key: (
            0 if key in deferred else 1, self._soft_last_attempt.get(key, -1), key)))

    def _ordered_keys(self):
        return (*self._ordered_hard_keys(), *self._ordered_soft_keys())

    def _watermark(self, conn, key):
        row = conn.execute("SELECT MAX(bar_time) FROM ohlcv_cache WHERE symbol=? AND interval=? AND source=?",
                           (key[0], key[1], self.storage_source)).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def _request(self, pair, interval, start, end, timeout):
        params = inspect.signature(self.fetch).parameters
        if "timeout" in params:
            return self.fetch(pair, interval, start, end, timeout=timeout)
        return self.fetch(pair, interval, start, end)

    def _reference_symbol_allowed(self, symbol: str) -> bool:
        """A non-trading symbol is fetched only when it is a valid watched raw name."""
        if symbol in self.settings.pairs:
            return True
        try:
            sources.MT5RawSymbol.parse(symbol)
        except ValueError:
            return False
        return (self.source == "mt5"
                and symbol in (getattr(self.settings.datafeed, "watch_symbols", ()) or ()))

    def prepare(self, now: datetime, conn=None) -> tuple[int, IngestTickReport]:
        """Collect due keys without writing.  ``conn`` is read-only in production.

        戻り値は ``(count, report)``。``report`` はこの tick 限りの immutable
        なスナップショットであり、``self.last_errors`` (失敗した key が後で
        成功してもクリアされずに残り続ける永続的な再試行制御) には依存しない
        — 一度失敗した key が次 tick で成功すれば ``report.succeeded`` に載る。

        hard key (取引 pair の価格) を全て先に試し、soft key (参照系列) は残りの
        budget だけを使う。soft の結果は ``report.attempted`` 以下の集合に入れず
        ``soft_*`` に分ける。
        """
        conn = conn or self.read_conn
        if conn is None:
            raise ValueError("prepare requires a read connection")
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        now = now.astimezone(timezone.utc)
        self._pending.clear()
        self.last_request_count = 0
        self.budget_exhausted = False
        if not market_hours.is_market_open(now):
            return 0, EMPTY_REPORT
        started = self.monotonic()
        budget = self.settings.datafeed.ingest_budget_sec
        grace = timedelta(seconds=self.settings.datafeed.closed_bar_grace_sec)
        deferred = []
        attempted: set[tuple[str, str]] = set()
        succeeded: set[tuple[str, str]] = set()
        empty: set[tuple[str, str]] = set()
        failed_errors: dict[tuple[str, str], str] = {}
        soft_deferred: list[tuple[str, str]] = []
        soft_attempted: set[tuple[str, str]] = set()
        soft_succeeded: set[tuple[str, str]] = set()
        soft_empty: set[tuple[str, str]] = set()
        soft_failed: dict[tuple[str, str], str] = {}
        soft_waiting: set[tuple[str, str]] = set()
        soft_keys = {key for key in self.keys if not self._is_hard(key)}
        for key in self._ordered_keys():
            soft = key in soft_keys
            if soft and key in self._rejected_soft:
                continue
            if self.next_probe_at.get(key, now) > now:
                if soft:
                    soft_waiting.add(key)
                continue
            if self.monotonic() - started >= budget:
                self.budget_exhausted = True
                if soft:
                    soft_deferred.append(key)
                    self._soft_state[key] = "budget_deferred"
                elif self._priority(key) == 1:
                    deferred.append(key)
                continue
            pair, interval = key
            if soft and not self._reference_symbol_allowed(pair):
                self._rejected_soft.add(key)
                soft_failed[key] = "symbol_rejected"
                self._soft_state[key] = "symbol_rejected"
                _log.warning("reference symbol rejected for %s %s", pair, interval)
                continue
            watermark = self._watermark(conn, key)
            required = self.registry.required_closed_bars(pair, interval)
            plan = self.planner.plan(self.source, interval, required)
            if not plan.ok:
                if soft:
                    if self._soft_state.get(key) != "plan_failed":
                        _log.warning("reference plan failed for %s %s", pair, interval)
                    self._soft_state[key] = "plan_failed"
                continue
            (soft_attempted if soft else attempted).add(key)
            start = (now - timedelta(days=plan.days) if watermark is None
                     else watermark - timedelta(minutes=sources.INTERVAL_MIN[interval]))
            remaining = max(0.01, budget - (self.monotonic() - started))
            timeout = min(remaining, max(0.01, budget / 2)) if soft else remaining
            if soft:
                self._attempt_serial += 1
                self._soft_last_attempt[key] = self._attempt_serial
                self._soft_last_request_at[key] = now
            width = timedelta(minutes=sources.INTERVAL_MIN[interval])
            try:
                raw = self._request(pair, interval, start, now, timeout)
                bars = normalize_closed_range(raw, interval=interval, start=start,
                                              end=now, cutoff=now, grace=grace)
                self._pending[key] = bars
                self.last_request_count += 1
                (soft_succeeded if soft else succeeded).add(key)
                if not bars:
                    (soft_empty if soft else empty).add(key)
                newest = max((b.ts for b in bars), default=watermark)
                if soft:
                    # The next bar closes one width after the newest one's end; when
                    # that moment has already passed (market closed, late bar) the
                    # source is polled with capped backoff instead.
                    expected = None if newest is None else newest + 2 * width + grace
                    self._soft_state[key] = "ok"
                    if expected is not None and expected > now:
                        self._backoff[key] = 0
                        self.next_probe_at[key] = expected
                    else:
                        self._soft_miss(key, now, width)
                    continue
                # 次に取りに行くのは次に取引される足の確定時刻 (閉場は飛ばす)。
                # 取得時刻 + 足幅にすると、10:22 に 09:00 の 1h 足を取ったあと 11:22 まで
                # 取りに行かず、10:00 の足が 22 分遅れて判断も遅れる (2026-09-22 実機)。
                # 期待時刻を過ぎても足が無いときだけ、足幅を上限に指数 backoff で再試行
                expected = None if newest is None else market_hours.next_bar_confirmation(
                    newest, width, grace)
                if expected is not None and expected > now:
                    self._backoff[key] = 0
                    self.next_probe_at[key] = expected
                else:
                    attempt = self._backoff.get(key, 0) + 1
                    self._backoff[key] = attempt
                    delay = min(sources.INTERVAL_MIN[interval] * 60, 2 ** attempt)
                    self.next_probe_at[key] = now + timedelta(seconds=delay)
            except Exception as exc:  # one broken key must not suppress protection
                self.last_request_count += 1
                self.last_errors[key] = str(exc)
                if soft:
                    soft_failed[key] = str(exc)
                    self._soft_state[key] = classify_fetch_error(exc)
                    self._soft_miss(key, now, width)
                else:
                    self.next_probe_at[key] = now
                    failed_errors[key] = str(exc)
                _log.warning("ingest failed for %s %s: %s", pair, interval, exc)
        self._deferred = deferred
        self._soft_deferred = soft_deferred
        self.reference_fetch_state = MappingProxyType(
            {key: state for key, state in self._soft_state.items() if key in soft_keys})
        _log.info("ingest requests=%d budget_exhausted=%s", self.last_request_count,
                  self.budget_exhausted)
        report = IngestTickReport(
            attempted=frozenset(attempted),
            succeeded=frozenset(succeeded),
            failed=frozenset(failed_errors.items()),
            deferred=frozenset(deferred),
            empty=frozenset(empty),
            soft_attempted=frozenset(soft_attempted),
            soft_succeeded=frozenset(soft_succeeded),
            soft_failed=frozenset(soft_failed.items()),
            soft_deferred=frozenset(soft_deferred),
            soft_empty=frozenset(soft_empty),
            soft_backoff_waiting=frozenset(soft_waiting))
        return self.last_request_count, report

    def _soft_miss(self, key, now: datetime, width: timedelta) -> None:
        attempt = self._backoff.get(key, 0) + 1
        self._backoff[key] = attempt
        backoff_due = now + timedelta(
            seconds=soft_backoff_delay(attempt, width.total_seconds()))
        rate_due = self._soft_last_request_at.get(key, now) + width
        self.next_probe_at[key] = max(backoff_due, rate_due)

    def commit(self, conn) -> int:
        written = 0
        for bars in self._pending.values():
            written += upsert_cache_bars(conn, bars, source=self.storage_source)
        self._pending.clear()
        return written

    def _fetch_primary(self, pair, interval, start, end, *, timeout):
        # The public source adapters all carry explicit transport timeouts.  MT5
        # additionally supports an exact range; other adapters receive the
        # planner's bounded calendar window and are normalized above.
        source = self.source
        if source == "mt5":
            if pair not in self.settings.pairs:
                if not self._reference_symbol_allowed(pair):
                    raise ValueError("reference symbol rejected")
                return sources.mt5_raw_bars_range(
                    self.settings.datafeed.mt5.bridge_url,
                    sources.MT5RawSymbol.parse(pair), interval, start, end,
                    timeout=timeout)
            return sources.mt5_bars_range(self.settings.datafeed.mt5.bridge_url,
                                           pair, interval, start, end,
                                           timeout=timeout)
        days = max(1, int((end - start).total_seconds() / 86400) + 1)
        if source == "yfinance":
            return sources.yf_bars(pair, interval, days, timeout=timeout)
        if source == "twelvedata":
            import os
            key = os.environ.get("TWELVEDATA_API_KEY")
            if not key:
                raise RuntimeError("TWELVEDATA_API_KEY is unset")
            return sources.td_bars(key, pair, interval, days, timeout=timeout)
        raise ValueError(f"unsupported primary source: {source}")
