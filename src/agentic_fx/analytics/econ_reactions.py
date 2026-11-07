"""Economic announcement reaction values and their aggregate views."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import statistics
from typing import Iterable, Mapping, Sequence

from agentic_fx.store.ohlcv_slices import SliceRow
from agentic_fx.core import market_hours

WINDOWS = (15, 60, 240)
RULE_VERSION = 1
STATUS_RESOLVED = "resolved"
STATUS_UNRESOLVABLE = "unresolvable"
AGENT_FLOOR = 20


@dataclass(frozen=True, slots=True)
class WindowValue:
    window_min: int
    status: str
    range_pips: float | None
    close_change_pips: float | None
    abs_close_change_pips: float | None
    net_abs_close_change_pips: float | None


def window_values(rows: Sequence[SliceRow], *, event_ts: datetime, pip_size: float,
                  spread_pips: float | None, set_state: str) -> tuple[WindowValue, ...]:
    """Return independent 15/60/240-minute values.

    A window is unresolvable if the set is unrecoverable, spread or a required
    bar is missing, a required row is an outage marker, or any minute in the
    baseline-through-window interval is market-closed. Resolved values use the
    baseline close, target high/low range, terminal close, and full spread.
    """
    if (set_state != "complete" or spread_pips is None or pip_size <= 0
            or not math.isfinite(pip_size) or not math.isfinite(spread_pips)):
        return tuple(WindowValue(w, STATUS_UNRESOLVABLE, None, None, None, None) for w in WINDOWS)
    ts = event_ts.astimezone(timezone.utc)
    by_time = {r.bar_time.astimezone(timezone.utc): r for r in rows}
    result = []
    for window in WINDOWS:
        expected = [ts - timedelta(minutes=1), *(
            ts + timedelta(minutes=i) for i in range(window))]
        seq = [by_time.get(t) for t in expected]
        ok = all(market_hours.is_market_open(t) for t in expected)
        ok = ok and all(r is not None and r.quality == "observed" and
                        all(x is not None and math.isfinite(x) for x in
                            (r.open, r.high, r.low, r.close)) for r in seq)
        if not ok:
            result.append(WindowValue(window, STATUS_UNRESOLVABLE, None, None, None, None)); continue
        baseline = seq[0].close
        target = seq[1:]
        change = (target[-1].close - baseline) / pip_size
        rng = (max(r.high for r in target) - min(r.low for r in target)) / pip_size
        result.append(WindowValue(window, STATUS_RESOLVED, rng, change, abs(change),
                                  abs(change) - spread_pips))
    return tuple(result)


def event_currencies(country: str,
                     pair_currencies: Mapping[str, tuple[str, str]]) -> tuple[str, ...]:
    """Return settings-order distinct currencies for All, the country itself if
    present in any pair, or an empty tuple.
    """
    if country == "All":
        return tuple(dict.fromkeys(c for pair in pair_currencies.values() for c in pair))
    return (country,) if any(country in pair for pair in pair_currencies.values()) else ()


def canonical_pair(currency: str,
                   pair_currencies: Mapping[str, tuple[str, str]]) -> str | None:
    """Return the first settings-order pair containing currency, or None."""
    return next((pair for pair, currencies in pair_currencies.items() if currency in currencies), None)


@dataclass(frozen=True, slots=True)
class ReactionSample:
    """One (event_id, currency) sample for a single window."""
    event_id: int
    currency: str
    importance: int
    name: str
    ts: datetime
    value: WindowValue


@dataclass(frozen=True, slots=True)
class BundleStats:
    """Statistics for one currency/category/key/window bundle."""
    currency: str
    kind: str
    key: str
    window_min: int
    n: int
    unique_ts_n: int
    unresolvable_n: int
    range_median: float | None
    range_p75: float | None
    abs_median: float | None
    abs_p75: float | None
    net_median: float | None
    net_p75: float | None
    close_change_median: float | None


def aggregate(samples: Iterable[ReactionSample]) -> tuple[BundleStats, ...]:
    """Aggregate by importance/name and window using median and linear p75.

    Empty resolved bundles have None statistics.
    """
    groups = {}
    for sample in samples:
        for kind, key in (("importance", str(sample.importance)), ("name", sample.name)):
            groups.setdefault((sample.currency, kind, key, sample.value.window_min), []).append(sample)
    def percentile(values, p):
        if not values: return None
        ordered = sorted(values); pos = (len(ordered)-1)*p
        lo = math.floor(pos); hi = math.ceil(pos)
        return ordered[lo] + (ordered[hi]-ordered[lo])*(pos-lo)
    out = []
    for (currency, kind, key, window), group in sorted(groups.items()):
        resolved = [s.value for s in group if s.value.status == STATUS_RESOLVED]
        def vals(attr): return [getattr(v, attr) for v in resolved]
        def med(attr): return statistics.median(vals(attr)) if resolved else None
        def p75(attr): return percentile(vals(attr), .75)
        out.append(BundleStats(currency, kind, key, window, len(resolved),
            len({s.ts for s in group if s.value.status == STATUS_RESOLVED}),
            len(group)-len(resolved),
            med("range_pips"), p75("range_pips"), med("abs_close_change_pips"),
            p75("abs_close_change_pips"), med("net_abs_close_change_pips"),
            p75("net_abs_close_change_pips"), med("close_change_pips")))
    return tuple(out)


def agent_view(stats: Iterable[BundleStats], *, pair: str,
               pair_currencies: Mapping[str, tuple[str, str]],
               floor: int = AGENT_FLOOR) -> list[dict]:
    """Return base/quote bundles, hiding counts and values below floor.

    Unknown pairs raise ValueError. Adequate bundles have status='ok'; below
    floor only identity and status='insufficient' are returned.
    """
    if pair not in pair_currencies: raise ValueError(f"unknown pair: {pair}")
    wanted = pair_currencies[pair]
    out = []
    for row in stats:
        if row.currency not in wanted: continue
        identity = {"currency": row.currency, "kind": row.kind, "key": row.key,
                    "window_min": row.window_min}
        if row.n < floor: out.append({**identity, "status": "insufficient"})
        else: out.append({**identity, "status": "ok", "n": row.n,
            "unique_ts_n": row.unique_ts_n, "unresolvable_n": row.unresolvable_n,
            "range_median": row.range_median, "range_p75": row.range_p75,
            "abs_median": row.abs_median, "abs_p75": row.abs_p75,
            "net_median": row.net_median, "net_p75": row.net_p75,
            "close_change_median": row.close_change_median})
    return out
