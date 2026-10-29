"""Read-only view of reference series for ``status`` (API thread, no locks)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentic_fx.datafeed import sources
from agentic_fx.plugin.reference_time import DEFAULT_MAX_AGE_FACTOR

REFERENCE_STATES = ("ok", "stale", "missing", "fetch_failed", "budget_deferred",
                    "source_unavailable")
_FETCH_PROBLEMS = ("source_unavailable", "fetch_failed", "budget_deferred")
MAX_ROWS = 64


def reference_rows(conn, ingest, now: datetime, *, watch_symbols,
                   storage_source: str) -> list[dict]:
    """One row per ``(symbol, native interval, source)`` of the watched symbols.

    ``ingest.reference_fetch_state`` is an immutable snapshot replaced by the
    scheduler thread; the rest is read through ``conn``.  A stale row carries
    ``age_seconds`` measured from the end of the newest closed bar.
    """
    watch = set(watch_symbols)
    registry = ingest.registry
    fetch_state = ingest.reference_fetch_state
    now = now.astimezone(timezone.utc)
    rows = []
    for symbol, interval in registry.keys():
        if symbol not in watch:
            continue
        width = timedelta(minutes=sources.INTERVAL_MIN[interval])
        newest = conn.execute(
            "SELECT MAX(bar_time) FROM ohlcv_cache WHERE symbol=? AND interval=? AND source=?",
            (symbol, interval, storage_source)).fetchone()[0]
        problem = fetch_state.get((symbol, interval))
        problem = problem if problem in _FETCH_PROBLEMS else None
        age = None
        if newest is None:
            state = problem or "missing"
        else:
            end = datetime.fromisoformat(newest).astimezone(timezone.utc) + width
            age = max(0, int((now - end).total_seconds()))
            limit = registry.max_age_seconds(symbol, interval)
            if limit is None:
                limit = DEFAULT_MAX_AGE_FACTOR * int(width.total_seconds())
            state = "ok" if age <= limit else (problem or "stale")
        rows.append({"symbol": symbol, "interval": interval, "source": storage_source,
                     "state": state, "age_seconds": age if state == "stale" else None})
        if len(rows) >= MAX_ROWS:
            break
    return rows
