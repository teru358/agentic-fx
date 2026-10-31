"""The indicator candidate gate shared by the improve loop and the human corridor.

Two stages: ``check_static`` (name and parameter hygiene, no execution) and
``run_fixture_stage`` (contract run, analysis probe, scale class, comparison with
the deployed and pending indicators through the parent-only fixture cache).  The
improve loop, ``submit`` and ``bless`` call the same functions; the lane decides
which findings reject and which stay warnings.

Fixture values never leave this module except as the closed vocabulary in
``IndicatorReport`` and a parent-only ``detail`` text for the report.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Sequence

import numpy as np
import pandas as pd

from agentic_fx import improve_notice_codes as notices
from agentic_fx.plugin import indicator_fixture_cache as cache
from agentic_fx.plugin import indicator_gate as gate
from agentic_fx.plugin import indicator_probe as probe_mod
from agentic_fx.plugin.canonical_fixture import (
    aggregate_to, build_canonical_5m, fixture_data_hash)

if TYPE_CHECKING:
    from agentic_fx.config import Settings
    from agentic_fx.plugin.loader import PluginMeta

Lane = Literal["loop", "submit", "bless"]
MAX_WARNINGS = 8
MAX_SIMILAR = 3
DUPLICATE_UNAVAILABLE = "duplicate_unavailable"
DUPLICATE_AT_APPROVE = "duplicate_at_approve"
SCALE_DEPENDENT = "scale_dependent"
SPARSE_OUTPUT = "sparse_output"
OUTPUT_CONSTANT_SUSPECT = "output_constant_suspect"
INDICATOR_REDEFINED = "indicator_redefined"
REDEFINED_CORR = 0.5


class IndicatorGateUnavailable(Exception):
    """The environment could not run the check (sandbox, cache, inventory).

    The candidate's quality is unknown; the caller fails retryably and creates no
    approval request."""


@dataclass(frozen=True)
class IndicatorReport:
    ok: bool = True
    code: str = ""
    warnings: tuple[str, ...] = ()
    similar_to: tuple[dict[str, str], ...] = ()
    detail: str = ""
    built: int = 0
    gc_removed: int = 0
    gc_skipped: int = 0
    fixture_data_hash: str = ""
    fixture_timeframe: str = ""


def _clean_warnings(items: Sequence[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for item in items:
        if item in notices.INDICATOR_PAYLOAD_WARNING_CODES and item not in seen:
            seen.append(item)
    return tuple(seen[:MAX_WARNINGS])


def payload_fields(report: IndicatorReport) -> dict[str, Any]:
    """The approval payload keys every entry point (improve mission, submit, bless)
    writes for an indicator, in one shape."""
    return {
        "gate_warnings": list(_clean_warnings(report.warnings)),
        "similar_to": [dict(row) for row in report.similar_to[:MAX_SIMILAR]],
        "fixture_data_hash": report.fixture_data_hash,
        "fixture_timeframe": report.fixture_timeframe,
    }


_KIND_RANK = {"duplicate": 4, "monotone": 3, "overlap": 2, "similar": 1}


def merge_warnings(old: Any, new: Sequence[str]) -> list[str]:
    """Existing payload warnings followed by new ones; closed vocabulary only."""
    base = [w for w in old if isinstance(w, str)] if isinstance(old, list) else []
    return list(_clean_warnings([*base, *new]))


def merge_similar(old: Any, new: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    """Strongest kinds first, at most three rows, rows of the exact shape only."""
    rows: list[dict[str, str]] = []
    for row in [*new, *(old if isinstance(old, list) else [])]:
        if (isinstance(row, dict) and set(row) == {"name", "column", "kind"}
                and all(isinstance(v, str) for v in row.values())
                and row["kind"] in _KIND_RANK and dict(row) not in rows):
            rows.append(dict(row))
    rows.sort(key=lambda r: -_KIND_RANK[r["kind"]])
    return rows[:MAX_SIMILAR]


def effective_mode(lane: Lane, mode: str) -> str:
    """bless never turns the monotone check into a rejection."""
    return "warn" if lane == "bless" else mode


# --- static checks -----------------------------------------------------------

def _read_identity_texts(directory: Path) -> tuple[str, str] | None:
    try:
        return ((directory / "plugin.py").read_text(encoding="utf-8"),
                (directory / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _static_detail(code: str, hits: list[gate.Hit], symbols: frozenset[str],
                   params: Any) -> str:
    """What tripped the check, for the parent-only report."""
    if code == "symbol_in_name":
        return ", ".join(sorted({f"{hit.place}:{hit.token}"
                                 for hit in hits if hit.token in symbols}))
    for key in gate._param_keys(params):
        if not gate.check_param_price_unit({key: 0}).ok:
            return f"param_key:{key}"
    return ""


def check_static(meta: "PluginMeta", plugin_dir: Path, *, settings: "Settings",
                 deployed: "PluginMeta | None", lane: Lane) -> IndicatorReport:
    """Name and parameter hygiene.  A same-name update of a deployed indicator that
    already carried the symbol is a ``legacy_symbol_name`` warning, not a rejection."""
    texts = _read_identity_texts(plugin_dir)
    if texts is None:
        return IndicatorReport(False, "fixture_run_failed",
                               detail="candidate source unreadable")
    symbols = gate.symbol_tokens(
        [*settings.pairs, *settings.datafeed.watch_symbols])
    hits = gate.candidate_tokens(meta, *texts)
    legacy_hits = None
    if deployed is not None:
        old_texts = _read_identity_texts(Path(deployed.path))
        if old_texts is not None:
            try:
                legacy_hits = gate.candidate_tokens(deployed, *old_texts)
            except Exception:  # noqa: BLE001 — an unreadable old version grants no exemption
                legacy_hits = None
    warnings: list[str] = []
    for verdict in (gate.check_symbol_in_name(hits, symbols,
                                              grandfather_hits=legacy_hits),
                    gate.check_param_price_unit(meta.params)):
        warnings.extend(verdict.warnings)
        if verdict.ok:
            continue
        if lane == "bless":
            warnings.append(verdict.code)
            continue
        return IndicatorReport(False, verdict.code,
                               detail=_static_detail(verdict.code, hits, symbols,
                                                     meta.params),
                               warnings=_clean_warnings(warnings))
    return IndicatorReport(warnings=_clean_warnings(warnings))


# --- fixture stage -----------------------------------------------------------

@lru_cache(maxsize=4)
def canonical_bars(timeframe: str) -> tuple[pd.DataFrame, str]:
    bars = aggregate_to(build_canonical_5m(), timeframe)
    return bars, fixture_data_hash(bars)


def _series_frame(outputs: probe_mod.Outputs) -> pd.DataFrame:
    return pd.DataFrame({k: v for k, v in outputs.items()
                         if isinstance(v, pd.Series)})


def _constant_columns(outputs: probe_mod.Outputs) -> set[str]:
    frame = _series_frame(outputs)
    if frame.empty:
        return set()
    return {col for col, (finite, unique) in gate.finite_report(frame, 0).items()
            if finite > 0 and unique <= 1}


def _sparse_columns(outputs: probe_mod.Outputs) -> set[str]:
    frame = _series_frame(outputs)
    if frame.empty:
        return set()
    return {col for col, rep in gate.finite_report(frame, 0).items()
            if gate.is_sparse(rep)}


def _probe_entry(meta: "PluginMeta", settings: "Settings", bars: pd.DataFrame,
                 wall_clock: float) -> cache.CacheEntry:
    """Run one indicator through the probe and return the cache entry; any failure
    is a build failure (the caller decides what that means)."""
    try:
        with probe_mod.open_session(meta, settings.plugin) as session:
            result = probe_mod.run_probe(session, meta, bars,
                                         wall_clock_max_sec=wall_clock)
    except (probe_mod.IndicatorEnvironmentError, probe_mod.FixtureFailure) as exc:
        raise _BuildFailed(str(exc)) from exc
    if result.failure:
        raise _BuildFailed(result.failure)
    return probe_mod.fixture_cache_entry(result)


class _BuildFailed(Exception):
    pass


def _entry_for(data_dir: Path, key: cache.CacheKey, builder: Any, counter: list[int]
               ) -> cache.CacheEntry:
    def counted() -> cache.CacheEntry:
        counter[0] += 1
        return builder()

    try:
        entry, _built = cache.get_or_build(data_dir, key, counted)
    except (cache.IndicatorCacheError, OSError, ValueError) as exc:
        raise _BuildFailed(type(exc).__name__) from exc
    return entry


def _indicator_metas(inventory_metas: Sequence["PluginMeta"], exclude: str
                     ) -> list["PluginMeta"]:
    return [m for m in inventory_metas if m.kind == "indicator" and m.name != exclude]


def pending_indicator_refs(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """(label, content_hash, name) of every pending indicator approval.  The label is
    the approval id: a pending candidate's name stays out of the agent surfaces."""
    from agentic_fx.store import approvals as approvals_store
    refs: list[tuple[str, str, str]] = []
    for row in approvals_store.pending(conn, "plugin"):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        if (isinstance(payload, dict) and payload.get("kind") == "indicator"
                and isinstance(payload.get("content_hash"), str)
                and isinstance(payload.get("name"), str)):
            refs.append((f"pending-{row['id']}", payload["content_hash"],
                         payload["name"]))
    return refs


def _redefined(meta: "PluginMeta", old: "PluginMeta", entry: cache.CacheEntry,
               old_entry: cache.CacheEntry | None) -> bool:
    if tuple(meta.outputs or ()) != tuple(old.outputs or ()):
        return True
    if old_entry is None:
        return False
    pairs = 0
    for col, series in entry.outputs.items():
        other = old_entry.outputs.get(col)
        if not isinstance(series, pd.Series) or not isinstance(other, pd.Series):
            continue
        a, b = gate._common(series, other)
        if len(a) < gate.MIN_CORRELATION_ROWS:
            continue
        pairs += 1
        if abs(gate._pearson(a, b)) >= REDEFINED_CORR:
            return False
    return pairs > 0


def run_fixture_stage(
        meta: "PluginMeta", *, settings: "Settings", data_dir: Path,
        deployed_metas: Sequence["PluginMeta"],
        pending: Sequence[tuple[str, str, str]], mode: str, lane: Lane,
        static_warnings: Sequence[str] = ()) -> IndicatorReport:
    """Contract run, analysis probe, scale class, duplicate and similarity checks.

    Raises ``IndicatorGateUnavailable`` when the environment prevents the check."""
    timeframe = settings.datafeed.decision_timeframes[0]
    bars, data_hash = canonical_bars(timeframe)
    wall_clock = settings.improve.indicator_fixture_wall_clock_max_sec
    mode = effective_mode(lane, mode)
    base = dict(fixture_data_hash=data_hash, fixture_timeframe=timeframe)
    counter = [0]

    def reject(code: str, detail: str = "", **extra: Any) -> IndicatorReport:
        return IndicatorReport(False, code, detail=detail, built=counter[0],
                               **base, **extra)

    try:
        with probe_mod.open_session(meta, settings.plugin) as session:
            contract = probe_mod.run_contract(session, meta, bars)
            if contract.failure:
                return reject("fixture_run_failed", contract.failure)
            if contract.all_nan:
                return reject("output_not_finite", "contract run")
            probe = probe_mod.run_probe(session, meta, bars,
                                        wall_clock_max_sec=wall_clock)
    except probe_mod.IndicatorEnvironmentError as exc:
        raise IndicatorGateUnavailable("sandbox_unavailable") from exc
    except probe_mod.FixtureFailure as exc:
        return reject("fixture_run_failed", exc.detail)
    if probe.failure:
        return reject("fixture_run_failed", probe.failure)
    if probe.all_nan_runs:
        return reject("output_not_finite", "probe " + ",".join(probe.all_nan_runs))

    assert contract.outputs is not None
    const_contract = _constant_columns(contract.outputs)
    const_probe = _constant_columns(probe.runs["k1"])
    if const_contract & const_probe:
        return reject("output_constant", ",".join(sorted(const_contract & const_probe)))
    warnings: list[str] = list(static_warnings)
    if const_contract ^ const_probe:
        warnings.append(OUTPUT_CONSTANT_SUSPECT)
    if _sparse_columns(contract.outputs):
        warnings.append(SPARSE_OUTPUT)

    entry_built = probe_mod.fixture_cache_entry(probe)
    key = cache.CacheKey(meta.content_hash, data_hash, timeframe)
    try:
        entry, written = cache.get_or_build(data_dir, key, lambda: entry_built)
        counter[0] += int(written)
    except (cache.IndicatorCacheError, OSError) as exc:
        raise IndicatorGateUnavailable("fixture_cache_unavailable") from exc
    if any("neither" in cls for cls in entry.classifications.values()):
        warnings.append(SCALE_DEPENDENT)

    def build_for(other: "PluginMeta") -> cache.CacheEntry:
        return _entry_for(
            data_dir, cache.CacheKey(other.content_hash, data_hash, timeframe),
            lambda: _probe_entry(other, settings, bars, wall_clock), counter)

    others: dict[str, dict[str, Any]] = {}
    for other in _indicator_metas(deployed_metas, meta.name):
        try:
            others[other.name] = build_for(other).outputs
        except _BuildFailed:
            if lane != "bless":
                raise IndicatorGateUnavailable("fixture_cache_unavailable") from None
            warnings.append(DUPLICATE_UNAVAILABLE)
    for label, content_hash, pending_name in pending:
        if content_hash == meta.content_hash and pending_name == meta.name:
            continue    # an earlier submission of this very candidate
        try:
            found = cache.read(data_dir, cache.CacheKey(content_hash, data_hash, timeframe))
        except (cache.IndicatorCacheError, OSError):
            found = None
        if found is not None:
            others[label] = found.outputs

    result = gate.compare_outputs(
        entry.outputs, others, mode=mode,
        similar_corr=settings.improve.indicator_similar_corr,
        classifications=entry.classifications)
    warnings.extend(result.warnings)

    old = next((m for m in deployed_metas
                if m.kind == "indicator" and m.name == meta.name), None)
    if old is not None:
        try:
            old_entry = build_for(old)
        except _BuildFailed:
            old_entry = None
        if _redefined(meta, old, entry, old_entry):
            warnings.append(INDICATOR_REDEFINED)

    try:
        collected = cache.gc_stale(data_dir, data_hash)
        gc_removed, gc_skipped = len(collected), len(collected.skipped)
    except (cache.IndicatorCacheError, OSError):
        gc_removed, gc_skipped = 0, 1
    common = dict(built=counter[0], gc_removed=gc_removed, gc_skipped=gc_skipped,
                  similar_to=tuple(result.similar_to[:MAX_SIMILAR]), **base)
    if not result.ok:
        return IndicatorReport(
            False, result.code, warnings=_clean_warnings(warnings),
            detail=_relation_detail(result), **common)
    return IndicatorReport(warnings=_clean_warnings(warnings),
                           detail=_relation_detail(result), **common)


def _relation_detail(result: gate.CompareResult) -> str:
    """One line per relation, with the numbers behind it: parent-only report text."""
    measures = [*result.measures, *[""] * len(result.similar_to)]
    return "\n".join(
        f"- {row['kind']}: {row['name']}.{row['column']}"
        + (f" ({measure})" if measure else "")
        for row, measure in zip(result.similar_to, measures))


# --- approval-time re-judgement ---------------------------------------------

@dataclass(frozen=True)
class Rejudgement:
    warnings: tuple[str, ...] = ()
    similar_to: tuple[dict[str, str], ...] = ()
    rejected_code: str = ""
    changed: bool = False


def rejudge_at_approval(
        payload: dict, *, settings: "Settings", data_dir: Path,
        deployed_metas: Sequence["PluginMeta"], mode: str) -> Rejudgement | None:
    """Exact and monotone duplicates against the inventory as it is now.

    ``None`` when the payload carries no probe output reference (an older payload) or
    its cache entry is gone: nothing is judged."""
    data_hash = payload.get("fixture_data_hash")
    timeframe = payload.get("fixture_timeframe")
    content_hash = payload.get("content_hash")
    name = payload.get("name")
    if not all(isinstance(v, str) for v in (data_hash, timeframe, content_hash, name)):
        return None
    try:
        entry = cache.read(data_dir, cache.CacheKey(content_hash, data_hash, timeframe))
    except (cache.IndicatorCacheError, OSError, ValueError):
        return None
    if entry is None:
        return None
    try:
        bars, _ = canonical_bars(timeframe)
    except (ValueError, KeyError):
        return None
    wall_clock = settings.improve.indicator_fixture_wall_clock_max_sec
    counter = [0]
    others: dict[str, dict[str, Any]] = {}
    for other in _indicator_metas(deployed_metas, name):
        try:
            others[other.name] = _entry_for(
                data_dir, cache.CacheKey(other.content_hash, data_hash, timeframe),
                lambda other=other: _probe_entry(other, settings, bars, wall_clock),
                counter).outputs
        except _BuildFailed:
            raise IndicatorGateUnavailable("fixture_cache_unavailable") from None
    result = gate.compare_outputs(
        entry.outputs, others, mode=mode,
        similar_corr=settings.improve.indicator_similar_corr,
        classifications=entry.classifications)
    # only the two findings that approval re-judges
    keep = [row for row in result.similar_to if row["kind"] in ("duplicate", "monotone")]
    warnings: list[str] = []
    if keep:
        warnings.append(DUPLICATE_AT_APPROVE)
    if any(row["kind"] == "monotone" for row in keep):
        warnings.append(gate.MONOTONE_DUPLICATE)
    rejected = ""
    if mode == "enforce" and (not result.ok or keep):
        rejected = DUPLICATE_AT_APPROVE
    return Rejudgement(warnings=_clean_warnings(warnings),
                       similar_to=tuple(keep[:MAX_SIMILAR]),
                       rejected_code=rejected, changed=bool(keep))
