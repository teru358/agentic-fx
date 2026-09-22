"""Bounded, closed-bar ingestion for the scheduler thread."""
from __future__ import annotations

import inspect
import logging
import time
from datetime import datetime, timedelta, timezone

from agentic_fx.core import market_hours
from agentic_fx.datafeed.closed_bars import normalize_closed_range
from agentic_fx.datafeed.planner import RequirementPlanner
from agentic_fx.datafeed.requirements import build_registry
from agentic_fx.datafeed import sources
from agentic_fx.store.ohlcv import upsert_cache_bars

_log = logging.getLogger("agentic_fx.ingest")


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
        self.last_request_count = 0
        self.budget_exhausted = False
        self.last_errors: dict[tuple[str, str], str] = {}

    @property
    def keys(self):
        return tuple((pair, interval) for pair in self.settings.pairs
                     for interval in sorted(self.registry.required_intervals))

    def _priority(self, key):
        _, interval = key
        if interval == "1m":
            return 0
        return 1 if interval in self.settings.datafeed.primary_intervals else 2

    def _ordered_keys(self):
        normal = sorted(self.keys, key=lambda key: (self._priority(key), key))
        ones = [key for key in normal if self._priority(key) == 0]
        decisions = [key for key in normal if self._priority(key) == 1]
        others = [key for key in normal if self._priority(key) == 2]
        deferred_decisions = [key for key in self._deferred if key in decisions]
        return tuple(dict.fromkeys([*deferred_decisions, *ones, *decisions, *others]))

    def _watermark(self, conn, key):
        row = conn.execute("SELECT MAX(bar_time) FROM ohlcv_cache WHERE symbol=? AND interval=? AND source=?",
                           (key[0], key[1], self.storage_source)).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def _request(self, pair, interval, start, end, timeout):
        params = inspect.signature(self.fetch).parameters
        if "timeout" in params:
            return self.fetch(pair, interval, start, end, timeout=timeout)
        return self.fetch(pair, interval, start, end)

    def prepare(self, now: datetime, conn=None) -> int:
        """Collect due keys without writing.  ``conn`` is read-only in production."""
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
            return 0
        started = self.monotonic()
        budget = self.settings.datafeed.ingest_budget_sec
        deferred = []
        for key in self._ordered_keys():
            if self.next_probe_at.get(key, now) > now:
                continue
            if self.monotonic() - started >= budget:
                self.budget_exhausted = True
                if self._priority(key) == 1:
                    deferred.append(key)
                continue
            pair, interval = key
            watermark = self._watermark(conn, key)
            required = self.registry.required_closed_bars(pair, interval)
            plan = self.planner.plan(self.source, interval, required)
            if not plan.ok:
                continue
            start = (now - timedelta(days=plan.days) if watermark is None
                     else watermark - timedelta(minutes=sources.INTERVAL_MIN[interval]))
            try:
                raw = self._request(pair, interval, start, now, max(0.01, budget - (self.monotonic() - started)))
                bars = normalize_closed_range(raw, interval=interval, start=start,
                                              end=now, cutoff=now,
                                              grace=timedelta(seconds=self.settings.datafeed.closed_bar_grace_sec))
                self._pending[key] = bars
                self.last_request_count += 1
                # 次に取りに行くのは「次の足が確定する時刻」= 最新の足の開始 + 足幅 2 つ + 猶予。
                # 取得時刻 + 足幅にすると、10:22 に 09:00 の 1h 足を取ったあと 11:22 まで
                # 取りに行かず、10:00 の足が 22 分遅れて判断も遅れる (2026-09-22 実機)。
                # 期待時刻を過ぎても足が無いときだけ、足幅を上限に指数 backoff で再試行
                width = timedelta(minutes=sources.INTERVAL_MIN[interval])
                grace = timedelta(seconds=self.settings.datafeed.closed_bar_grace_sec)
                newest = max((b.ts for b in bars), default=watermark)
                expected = None if newest is None else newest + 2 * width + grace
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
                self.next_probe_at[key] = now
                _log.warning("ingest failed for %s %s: %s", pair, interval, exc)
        self._deferred = deferred
        _log.info("ingest requests=%d budget_exhausted=%s", self.last_request_count,
                  self.budget_exhausted)
        return self.last_request_count

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
