"""Run an indicator candidate on the canonical fixture through the production sandbox.

The contract run mirrors a production call (one call over the last ``max_bars``
rows).  The analysis probe runs four scaled copies of a longer tail, splitting the
output columns into small groups so a wide indicator stays under the output limit.
Both reuse ``PluginSession``; no second runner exists.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pandas as pd

from agentic_fx.plugin import indicator_gate as gate
from agentic_fx.plugin.indicator_fixture_cache import CacheEntry
from agentic_fx.plugin.sandbox import OUTPUTS_MISMATCH_DETAIL, PluginSession, SandboxError

if TYPE_CHECKING:
    from agentic_fx.config import PluginSettings
    from agentic_fx.plugin.loader import PluginMeta

PROBE_COLUMNS_PER_CALL = 8
PROBE_BARS = 4096
DEFAULT_WALL_CLOCK_MAX_SEC = 60.0
PRICE_COLUMNS = ("open", "high", "low", "close")
# (label, price multiplier, volume multiplier)
PROBE_RUNS = (("k1", 1.0, 1.0), ("k100", 100.0, 1.0), ("k001", 0.01, 1.0),
              ("vol100", 1.0, 100.0))

Outputs = dict[str, "pd.Series | float"]


class IndicatorEnvironmentError(Exception):
    """The sandbox could not run the plugin at all (nothing of the candidate ran)."""


class FixtureFailure(Exception):
    """The candidate failed while loading in the sandbox."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class ContractResult:
    outputs: Outputs | None = None
    failure: str | None = None

    @property
    def all_nan(self) -> bool:
        return self.outputs is not None and outputs_all_nan(self.outputs)


@dataclass(frozen=True)
class ProbeResult:
    runs: dict[str, Outputs] = field(default_factory=dict)
    failure: str | None = None
    all_nan_runs: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.failure is None


def classify_sandbox_error(exc: SandboxError, *, probe: bool = False) -> str:
    """Map a SandboxError raised after the plugin was loaded to a
    ``fixture_run_failed`` detail.  Everything here is the candidate's doing; only a
    failure before the load (see ``open_session``) is the environment's."""
    if probe:
        return "probe_timeout" if exc.code in ("timeout", "cpu_limit") else "probe_exception"
    if exc.code in ("cpu_limit", "timeout"):
        return exc.code
    if exc.detail == OUTPUTS_MISMATCH_DETAIL:
        return "outputs_mismatch"
    return "exception"


@contextmanager
def open_session(meta: "PluginMeta", settings: "PluginSettings") -> Iterator[PluginSession]:
    """Enter a production session; split start-up failures into environment and candidate."""
    session = PluginSession(meta, settings=settings)
    try:
        session.__enter__()
    except SandboxError as exc:
        session.close()
        if exc.code == "sandbox_unavailable":
            raise IndicatorEnvironmentError(str(exc)) from exc
        raise FixtureFailure(classify_sandbox_error(exc)) from exc
    try:
        yield session
    finally:
        session.close()


def _to_outputs(result: dict[str, Any], index: pd.Index) -> Outputs:
    out: Outputs = {}
    for key, value in result.items():
        if isinstance(value, list):
            out[key] = pd.Series(value, index=index, dtype="float64")
        else:
            out[key] = float("nan") if value is None else float(value)
    return out


def outputs_all_nan(outputs: Outputs) -> bool:
    """True when no output (series or scalar) holds a single finite value."""
    if not outputs:
        return True
    for value in outputs.values():
        if isinstance(value, pd.Series):
            if not gate.is_all_nan(gate.finite_report(value.to_frame("v"), 0)["v"]):
                return False
        elif pd.notna(value) and abs(value) != float("inf"):
            return False
    return True


def run_contract(session: PluginSession, candidate: "PluginMeta",
                 bars: pd.DataFrame) -> ContractResult:
    """One production-shaped call over the last ``candidate.max_bars`` fixture rows."""
    df = bars.tail(candidate.max_bars).copy()
    try:
        result = session.call({"df": df, "params": dict(candidate.params)})
    except SandboxError as exc:
        return ContractResult(failure=classify_sandbox_error(exc))
    return ContractResult(outputs=_to_outputs(result, df.index))


def _groups(outputs: tuple[str, ...] | None, size: int) -> list[tuple[str, ...] | None]:
    if outputs is None:
        return [None]
    return [tuple(outputs[i:i + size]) for i in range(0, len(outputs), size)]


def _scaled(bars: pd.DataFrame, price: float, volume: float) -> pd.DataFrame:
    df = bars.copy()
    for col in PRICE_COLUMNS:
        df[col] = df[col] * price
    df["volume"] = df["volume"] * volume
    return df


def _shape(outputs: Outputs) -> dict[str, bool]:
    return {key: isinstance(value, pd.Series) for key, value in outputs.items()}


def run_probe(session: PluginSession, candidate: "PluginMeta", bars: pd.DataFrame, *,
              wall_clock_max_sec: float = DEFAULT_WALL_CLOCK_MAX_SEC,
              columns_per_call: int = PROBE_COLUMNS_PER_CALL,
              clock: Any = time.monotonic) -> ProbeResult:
    """Four scaled runs (k=1/100/0.01 on OHLC, volume x100) in output groups.

    Each scaled run computes the indicator once; the output groups are served from
    the result the worker retained for that run.

    The total wall clock of all calls is capped; running out, an overrun call or an
    exception is a candidate failure (``probe_timeout`` / ``probe_exception``)."""
    deadline = clock() + wall_clock_max_sec
    bars = bars.tail(PROBE_BARS)
    groups = _groups(candidate.outputs, columns_per_call)
    runs: dict[str, Outputs] = {}
    for label, price, volume in PROBE_RUNS:
        df = _scaled(bars, price, volume)
        merged: Outputs = {}
        for gi, group in enumerate(groups):
            remaining = deadline - clock()
            if remaining <= 0:
                return ProbeResult(runs=runs, failure="probe_timeout")
            payload: dict[str, Any] = {"df": df, "params": dict(candidate.params)}
            if group is not None:
                payload["select"] = group
                if len(groups) > 1:
                    # compute and validate once per scaling; later groups only read the
                    # retained result
                    payload["reuse" if gi else "retain"] = True
                    if gi == len(groups) - 1:
                        payload["release"] = True
            try:
                result = session.call(payload, timeout_sec=remaining)
            except SandboxError as exc:
                return ProbeResult(runs=runs, failure=classify_sandbox_error(exc, probe=True))
            merged.update(_to_outputs(result, df.index))
        if label != "k1" and _shape(merged) != _shape(runs["k1"]):
            # the same indicator must return the same outputs, in the same form, at
            # every scaling; anything else cannot be compared or cached
            return ProbeResult(runs=runs, failure="probe_exception")
        runs[label] = merged
    nan_runs = tuple(label for label, outs in runs.items() if outputs_all_nan(outs))
    return ProbeResult(runs=runs, all_nan_runs=nan_runs)


def fixture_cache_entry(probe: ProbeResult) -> CacheEntry:
    """The k=1 outputs and their scale classes, in the form the cache stores.

    A scalar is classified like a series of one value: unchanged by the scaling,
    scaled with it, or neither."""
    base = probe.runs["k1"]
    classes: dict[str, tuple[str, str]] = {}
    for is_series in (True, False):
        keys = [k for k, v in base.items() if isinstance(v, pd.Series) == is_series]
        if not keys:
            continue
        frames = [pd.DataFrame({k: (probe.runs[label][k] if is_series
                                    else [probe.runs[label][k]]) for k in keys})
                  for label in ("k1", "k100", "k001", "vol100")]
        classes.update(gate.classify_scale(*frames))
    return CacheEntry(dict(base), classes)
