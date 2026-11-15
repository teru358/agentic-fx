"""kill switch 解除後の HWM 付け直しの適格判定。

preview と POST の commit 時再検査は同じ :func:`evaluate_rebase` を通る。
入口ごとに検査を複写せず、reason と token をここで決める。DB は読むだけで、
書込みは :mod:`agentic_fx.ops.rebase_accounting` が持つ。
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from agentic_fx.config import Settings
from agentic_fx.core import market_hours
from agentic_fx.core.accounting import daily_start_equity, drawdown_pct
from agentic_fx.core.contracts import Clock
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.timeutil import as_utc
from agentic_fx.core.transitions import TERMINAL
from agentic_fx.ops.contracts import ErrorCode
from agentic_fx.ops.rebase_accounting import (paper_equity, read_rebase_totals,
                                              rebase_figures)
from agentic_fx.store import snapshots
from agentic_fx.store.state import AppState

DIGEST_VERSION = 1
SNAPSHOT_MAX_AGE = timedelta(minutes=10)
PAPER_SOURCE = "paper"
REBASE_SOURCE = "manual_ks_rebase"
#: 累積率 (各回の切下げ率の合計) の警告倍率。拒否や上限には使わない。
WARNING_MULTIPLE = 3

REASONS = frozenset({
    "rebase_unavailable", "broker_not_supported", "state_unavailable",
    "generation_mismatch", "generation_unavailable", "market_closed",
    "account_snapshot_unavailable", "account_snapshot_changed", "exposure_present",
    "rebase_not_needed", "accounting_inconsistent", "preview_changed"})

_TERMINAL_VALUES = tuple(sorted(status.value for status in TERMINAL))


@dataclass(frozen=True, slots=True)
class RebaseWiring:
    """OpsService へ注入する、rebase だけが使う依存。未注入なら rebase だけ使えない。"""
    core_lock: Any
    broker: object
    clock: Clock


@dataclass(frozen=True, slots=True)
class RebaseEvaluation:
    reason: str | None
    fields: dict[str, Any]
    token: str | None
    source_snapshot_id: int | None
    latched: bool | None


_DTO_KEYS = (
    "generation", "latched", "latched_at", "source_snapshot_id", "source_snapshot_ts",
    "source_snapshot_source", "hwm", "balance", "equity", "new_hwm", "drawdown_pct",
    "threshold_pct", "cut", "cut_pct", "past_cut", "past_cut_pct", "past_count",
    "cumulative_cut_after", "cumulative_cut_pct_after", "count_after",
    "additional_headroom", "flat", "cumulative_warning", "warning_threshold_pct")


def empty_fields() -> dict[str, Any]:
    return {key: None for key in _DTO_KEYS}


def dto(evaluation: RebaseEvaluation) -> dict[str, Any]:
    """preview の応答本文。不適格なら token は null、取れない値は null のまま。"""
    return {"eligible": evaluation.reason is None, "reason": evaluation.reason,
            "preview_token": evaluation.token, **evaluation.fields}


def error_code_name(reason: str) -> ErrorCode:
    """POST が返す固定エラーコード。"""
    if reason in ("rebase_unavailable", "broker_not_supported"):
        return ErrorCode.UNAVAILABLE
    if reason == "state_unavailable":
        return ErrorCode.INVALID_STATE
    if reason == "generation_mismatch":
        return ErrorCode.GENERATION_MISMATCH
    if reason == "market_closed":
        return ErrorCode.MARKET_CLOSED
    return ErrorCode.PREVIEW_CHANGED


def _canonical(value: object) -> object:
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("digest value must be finite")
        return value + 0.0  # -0.0 を 0.0 にそろえる
    if type(value) in (int, bool, str) or value is None:
        return value
    raise TypeError("unsupported digest value")


def _token(parts: dict[str, object]) -> str:
    body = json.dumps({key: _canonical(value) for key, value in parts.items()},
                      sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _finite_positive(*values: object) -> bool:
    return all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in values)


def _reject(reason: str, fields: dict[str, Any],
            latched: bool | None = None) -> RebaseEvaluation:
    return RebaseEvaluation(reason, fields, None, None, latched)


def evaluate_rebase(conn: sqlite3.Connection, *, wiring: RebaseWiring | None,
                    state: AppState | None, marker_pending: bool,
                    expected_generation: int, settings: Settings,
                    now: datetime) -> RebaseEvaluation:
    """適格性の唯一の判定。呼び出し側が保持する 1 本の DB transaction の中で呼ぶ。"""
    fields = empty_fields()
    if wiring is None:
        return _reject("rebase_unavailable", fields)
    if type(wiring.broker) is not PaperBroker:
        return _reject("broker_not_supported", fields)
    if state is None or marker_pending:
        return _reject("state_unavailable", fields)
    latched = state.kill_switch_latched
    fields.update(generation=state.kill_switch_generation, latched=latched,
                  latched_at=state.kill_switch_latched_at)
    if state.kill_switch_generation != expected_generation:
        return _reject("generation_mismatch", fields, latched)
    if state.kill_switch_generation <= 0:
        return _reject("generation_unavailable", fields, latched)
    now = as_utc(now)
    if not market_hours.is_market_open(now):
        return _reject("market_closed", fields, latched)

    row = snapshots.latest(conn)
    if row is None:
        return _reject("account_snapshot_unavailable", fields, latched)
    try:
        taken = datetime.fromisoformat(row["ts"])
        if taken.tzinfo is None:
            raise ValueError("naive timestamp")
        age = now - taken
    except (TypeError, ValueError):
        return _reject("account_snapshot_unavailable", fields, latched)
    fields.update(source_snapshot_id=row["id"], source_snapshot_ts=row["ts"],
                  source_snapshot_source=row["source"])
    if not timedelta(0) <= age <= SNAPSHOT_MAX_AGE:
        return _reject("account_snapshot_unavailable", fields, latched)
    if row["source"] == REBASE_SOURCE:
        return _reject("rebase_not_needed", fields, latched)
    if row["source"] != PAPER_SOURCE:
        return _reject("account_snapshot_unavailable", fields, latched)
    hwm, equity, balance = row["hwm"], row["equity"], row["balance"]
    if not _finite_positive(hwm, equity, balance):
        return _reject("account_snapshot_unavailable", fields, latched)
    fields.update(hwm=hwm, equity=equity, balance=balance)
    threshold = settings.risk.drawdown_kill_pct
    fields["threshold_pct"] = threshold
    if row["cashflow"] != 0:
        return _reject("accounting_inconsistent", fields, latched)

    non_terminal = conn.execute(
        "SELECT 1 FROM orders WHERE status IS NULL OR status NOT IN (%s) LIMIT 1"
        % ",".join("?" * len(_TERMINAL_VALUES)), _TERMINAL_VALUES).fetchone()
    fields["flat"] = non_terminal is None
    if non_terminal is not None:
        return _reject("exposure_present", fields, latched)

    broker_balance, broker_equity = paper_equity(conn, settings, wiring.clock)
    if broker_balance != balance or broker_equity != equity:
        return _reject("account_snapshot_changed", fields, latched)

    fields["drawdown_pct"] = drawdown_pct(equity, hwm)
    if fields["drawdown_pct"] < threshold or equity >= hwm:
        return _reject("rebase_not_needed", fields, latched)
    try:
        figures = rebase_figures(hwm, equity, threshold)
    except ValueError:
        return _reject("accounting_inconsistent", fields, latched)
    if daily_start_equity(conn, now) is None:
        return _reject("accounting_inconsistent", fields, latched)

    totals = read_rebase_totals(conn)
    cumulative_cut = totals["cut"] + figures.cut
    cumulative_pct = totals["cut_pct"] + figures.cut_pct
    warn_limit = WARNING_MULTIPLE * threshold
    fields.update(
        new_hwm=equity, cut=figures.cut, cut_pct=figures.cut_pct,
        past_cut=totals["cut"], past_cut_pct=totals["cut_pct"],
        past_count=totals["count"], cumulative_cut_after=cumulative_cut,
        cumulative_cut_pct_after=cumulative_pct, count_after=totals["count"] + 1,
        additional_headroom=figures.additional_headroom,
        cumulative_warning=cumulative_pct > warn_limit,
        warning_threshold_pct=warn_limit)
    try:
        token = _token({
            "version": DIGEST_VERSION, "generation": state.kill_switch_generation,
            "latched": latched, "latched_at": state.kill_switch_latched_at,
            "hwm": hwm, "equity": equity, "balance": balance, "flat": True,
            "threshold_pct": threshold, "broker": "paper",
            "starting_balance": settings.paper.starting_balance,
            "ledger_last_id": totals["last_id"], "ledger_count": totals["count"],
            "ledger_cut": totals["cut"], "ledger_cut_pct": totals["cut_pct"]})
        for key in ("cut", "cut_pct", "cumulative_cut_after",
                    "cumulative_cut_pct_after", "additional_headroom"):
            _canonical(fields[key])
    except (TypeError, ValueError):
        return _reject("accounting_inconsistent", fields, latched)
    return RebaseEvaluation(None, fields, token, row["id"], latched)
