"""Collect read-only observations used by setup guidance renderers."""
from __future__ import annotations

import logging
import os
import sqlite3

from agentic_fx.core import landlock
from agentic_fx.config import Settings
from agentic_fx.setup.checks import present_env_keys
from agentic_fx.setup.pending_steps import MAX_PENDING_STEPS, PendingFacts
from agentic_fx.store import plugin_switch_journal
from agentic_fx.store.setup_record import read_setup_record

_log = logging.getLogger(__name__)
_COMPUTE = object()


def _landlock_now() -> bool | None:
    try:
        return bool(landlock.is_available())
    except Exception:  # noqa: BLE001 — display-only; unknown is not a step
        return None


def _landlock_abi_now() -> int:
    try:
        return int(landlock.landlock_abi())
    except Exception:  # noqa: BLE001 — display-only; unknown is not a step
        return 0


def collect_pending_facts(root, settings: Settings | None, conn: sqlite3.Connection | None, *, deployed_plugins,
                          outage, clock, landlock_available: object = _COMPUTE,
                          landlock_abi: object = _COMPUTE,
                          data_state: str | None = None,
                          service_active: bool | None = True) -> PendingFacts:
    """Read the shared setup observations without probing external services."""
    setup_read = read_setup_record(root)
    deployed = tuple((plugin.name, plugin.kind) for plugin in deployed_plugins)
    source = settings.backtest.eval_source if settings is not None else "none"
    history_counts: dict[str, int] = {}
    if settings is not None and conn is not None and source != "none":
        for pair in settings.pairs[:MAX_PENDING_STEPS]:
            found = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM ohlcv_history WHERE symbol=? AND interval=? AND source=? LIMIT 1)",
                (pair, settings.backtest.base_interval, source)).fetchone()[0]
            history_counts[pair] = int(found)
    required_env_keys = (() if settings is None or settings.datafeed.primary == "yfinance" else
                         ("TWELVEDATA_API_KEY",) if settings.datafeed.primary == "twelvedata" else
                         ("MT5_BRIDGE_API_KEY",))
    env_names = present_env_keys(root / ".env", environ=os.environ) if settings is not None else set()
    env_present_keys = frozenset(key for key in required_env_keys if key in env_names)
    found_ids = (plugin_switch_journal.list_non_terminal_ids(conn, MAX_PENDING_STEPS + 1)
                 if conn is not None else [])
    if len(found_ids) > MAX_PENDING_STEPS:
        _log.info("stalled approvals exceed %d; showing the newest", MAX_PENDING_STEPS)
    approval_ids = tuple(found_ids[:MAX_PENDING_STEPS])
    return PendingFacts(
        record_state=setup_read.state, record=setup_read.record, settings=settings,
        deployed_plugins=deployed, history_counts=history_counts,
        env_present_keys=env_present_keys, required_env_keys=required_env_keys,
        approval_ids=approval_ids, landlock_available=(_landlock_now() if landlock_available is _COMPUTE
                            else landlock_available),  # type: ignore[arg-type]
        landlock_abi=(_landlock_abi_now() if landlock_abi is _COMPUTE else int(landlock_abi)),
        data_state=(data_state if data_state is not None
                    else (outage.state if outage is not None else None)),
        service_active=service_active, now=clock.now(),
    )
