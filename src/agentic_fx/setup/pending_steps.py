"""Safe, deterministic rendering inputs for incomplete initial-setup work."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from agentic_fx.setup.pending_text import PendingStep, render_pending_step
from agentic_fx.store.setup_record import SetupRecord


MAX_PENDING_STEPS = 32
__all__ = ["MAX_PENDING_STEPS", "PendingFacts", "PendingStep", "pending_steps", "render_pending_step"]
_TRAILING_PENDING_ORDER = (
    "ops_incomplete", "isolation_not_admitted", "api_disabled", "api_start_failed",
    "linger_off", "start_pending", "ops_not_ready",
)


@dataclass(frozen=True, slots=True)
class PendingFacts:
    record_state: str
    record: SetupRecord | None = None
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    settings: object | None = None
    deployed_plugins: tuple[tuple[str, str], ...] = ()
    history_counts: Mapping[str, int] = field(default_factory=dict)
    approval_ids: tuple[int, ...] = ()
    env_present_keys: frozenset[str] = frozenset()
    required_env_keys: tuple[str, ...] = ()
    landlock_available: bool | None = None
    service_active: bool | None = None
    data_state: str | None = None
    extra_steps: list[PendingStep] = field(default_factory=list)


def _get(value: object | None, *names: str, default=None):
    current = value
    for name in names:
        if isinstance(current, Mapping):
            current = current.get(name, default)
        else:
            current = getattr(current, name, default)
        if current is default:
            return default
    return current


def _record_value(record: SetupRecord | None, name: str, default=None):
    return record.data.get(name, default) if record is not None else default


def _at(facts: PendingFacts, value: object = None) -> datetime:
    if isinstance(value, Mapping):
        raw = value.get("at")
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                pass
    return facts.now


def _step(facts: PendingFacts, code: str, severity: str, params: Mapping[str, Any] = {}, *,
          at: object = None, visible_in_status: bool = True) -> PendingStep:
    return PendingStep(code, severity, _at(facts, at), dict(params), visible_in_status)


def pending_steps(facts: PendingFacts, *, for_status: bool = False) -> list[PendingStep]:
    """Return ordered pending work from supplied observations, without I/O or probes."""
    if facts.record_state == "missing":
        steps = [_step(facts, "setup_record_missing", "action")]
        return steps[:MAX_PENDING_STEPS]
    steps: list[PendingStep] = []
    if facts.record_state == "corrupt":
        steps.append(_step(facts, "setup_record_missing", "action", {"state": "corrupt"}))
    record = facts.record
    if record is not None:
        for target in ("trade", "improve"):
            result = _record_value(record, f"llm_{target}", {})
            if not isinstance(result, Mapping) or result.get("result") != "ok":
                reason = result.get("reason_code", "unverified") if isinstance(result, Mapping) else "unverified"
                steps.append(_step(facts, "llm_unverified", "warn",
                                   {"target": target, "reason_code": reason}, at=result))
        landlock_result = _record_value(record, "landlock", {})
        if (isinstance(landlock_result, Mapping)
                and landlock_result.get("reason_code") == "landlock_abi_too_old"):
            steps.append(_step(facts, "improve_isolation_unavailable", "warn", at=landlock_result))
    improve_backend = _get(facts.settings, "improve_backend", default=None)
    if improve_backend is None:
        improve_backend = _get(facts.settings, "runner", "improve", "backend", default=None)
    verified = _get(facts.settings, "llama_swap_verified", default=None)
    if verified is None:
        verified = _get(facts.settings, "improve", "llama_swap_verified", default=True)
    if improve_backend == "opencode" and verified is False:
        steps.append(_step(facts, "opencode_unverified", "action"))
    if _record_value(record, "bridge_plan") == "planned" or facts.data_state == "degraded":
        steps.append(_step(facts, "bridge_not_connected", "action"))
    for key in facts.required_env_keys:
        if key not in facts.env_present_keys:
            steps.append(_step(facts, "env_key_missing", "warn", {"key": key}))
    indicators = sum(1 for _, kind in facts.deployed_plugins if kind == "indicator")
    recorded_indicators = _get(_record_value(record, "indicators", {}), "installed", default=None)
    target_indicators = (recorded_indicators
                         if isinstance(recorded_indicators, int) and not isinstance(recorded_indicators, bool)
                         else 9)
    if indicators < target_indicators:
        steps.append(_step(facts, "indicators_missing", "action",
                           {"installed": indicators, "total": target_indicators}))
    if not any(kind == "strategy" for _, kind in facts.deployed_plugins):
        steps.append(_step(facts, "no_strategy", "info"))
    eval_source = _get(facts.settings, "eval_source", default=None)
    if eval_source is None:
        eval_source = _get(facts.settings, "backtest", "eval_source", default=_get(_record_value(record, "history", {}), "source", default=None))
    if eval_source == "none":
        steps.append(_step(facts, "history_source_unconfigured", "action"))
    for symbol, count in facts.history_counts.items():
        if count == 0:
            steps.append(_step(facts, "history_missing", "action", {"symbol": symbol}))
    for approval_id in facts.approval_ids:
        steps.append(_step(facts, "approval_stalled", "action", {"approval_id": approval_id}))
    if facts.landlock_available is False:
        steps.append(_step(facts, "landlock_unavailable", "action"))
    price_source = _get(facts.settings, "price_source", default=None)
    if price_source is None:
        price_source = _get(facts.settings, "datafeed", "primary", default=None)
    if price_source == "yfinance":
        steps.append(_step(facts, "yfinance_weekend_gap", "info"))
    deferred: dict[str, list[PendingStep]] = {code: [] for code in _TRAILING_PENDING_ORDER}
    ops = _record_value(record, "ops", {})
    if isinstance(ops, Mapping):
        for feature in sorted(ops):
            item = ops[feature]
            if not isinstance(item, Mapping):
                continue
            if item.get("result") == "failed":
                deferred["ops_incomplete"].append(_step(facts, "ops_incomplete", "action", {
                    "feature": feature, "stage": item.get("stage", "unknown"),
                    "reason_code": item.get("reason_code", "unknown")}, at=item))
            if item.get("ready") == "failed":
                deferred["ops_not_ready"].append(_step(facts, "ops_not_ready", "warn", {"feature": feature}, at=item))
    isolation = _record_value(record, "isolation", {})
    if isinstance(isolation, Mapping) and isolation.get("admitted") == "no":
        deferred["isolation_not_admitted"].append(_step(facts, "isolation_not_admitted", "action",
                           {"reason_code": isolation.get("reason_code", "unknown")}, at=isolation))
    api = _record_value(record, "api", {})
    unit = _record_value(record, "unit", {})
    if isinstance(api, Mapping) and (unit == "installed" or
                                     isinstance(unit, Mapping) and unit.get("installed") is True):
        if api.get("enabled") is False:
            deferred["api_disabled"].append(_step(facts, "api_disabled", "action", visible_in_status=False, at=api))
        if api.get("result") == "failed":
            deferred["api_start_failed"].append(_step(facts, "api_start_failed", "warn",
                               {"reason_code": api.get("reason_code", "unknown")}, at=api,
                               visible_in_status=False))
    linger = _record_value(record, "linger", {})
    if isinstance(linger, Mapping) and linger.get("state") in {"declined", "failed"}:
        deferred["linger_off"].append(_step(facts, "linger_off", "info", at=linger))
    start = _record_value(record, "start", {})
    if isinstance(start, Mapping) and start.get("result") == "pending" and facts.service_active is not True:
        deferred["start_pending"].append(_step(facts, "start_pending", "action", at=start))
    for code in _TRAILING_PENDING_ORDER:
        steps.extend(deferred[code])
    steps.extend(facts.extra_steps)
    if for_status:
        steps = [step for step in steps if step.visible_in_status]
    return steps[:MAX_PENDING_STEPS]
