"""Economic announcement reaction values and their aggregate views."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping, Sequence

from agentic_fx.store.ohlcv_slices import SliceRow

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
    raise NotImplementedError


def event_currencies(country: str,
                     pair_currencies: Mapping[str, tuple[str, str]]) -> tuple[str, ...]:
    """Return settings-order distinct currencies for All, the country itself if
    present in any pair, or an empty tuple.
    """
    raise NotImplementedError


def canonical_pair(currency: str,
                   pair_currencies: Mapping[str, tuple[str, str]]) -> str | None:
    """Return the first settings-order pair containing currency, or None."""
    raise NotImplementedError


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
    raise NotImplementedError


def agent_view(stats: Iterable[BundleStats], *, pair: str,
               pair_currencies: Mapping[str, tuple[str, str]],
               floor: int = AGENT_FLOOR) -> list[dict]:
    """Return base/quote bundles, hiding counts and values below floor.

    Unknown pairs raise ValueError. Adequate bundles have status='ok'; below
    floor only identity and status='insufficient' are returned.
    """
    raise NotImplementedError
