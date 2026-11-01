from __future__ import annotations
import sqlite3
from datetime import datetime

GATE_REJECT_STREAK_KEY = "gate_reject.last_notified_streak_id"
# 抑止中の limit の通知済み成分: `limit_suppress.<order_id>` (JSON)
LIMIT_SUPPRESS_PREFIX = "limit_suppress."
# 親 report hook の最後に起動した取引日: JSON {"day", "done"}
DAILY_REPORT_KEY = "daily_report.state"
# 稼働 warning の dedupe と直近の評価結果 (JSON)
WAIT_BUDGET_NOTIFIED_KEY = "signal_wait_budget.notified"
WAIT_BUDGET_SNAPSHOT_KEY = "signal_wait_budget.snapshot"
# 最後に通知した calendar の鮮度 (stale / fresh)
CALENDAR_FRESHNESS_KEY = "econ_calendar.notified_freshness"

_KNOWN_KEYS = frozenset({
    GATE_REJECT_STREAK_KEY, DAILY_REPORT_KEY, WAIT_BUDGET_NOTIFIED_KEY,
    WAIT_BUDGET_SNAPSHOT_KEY, CALENDAR_FRESHNESS_KEY})
_KNOWN_PREFIXES = (LIMIT_SUPPRESS_PREFIX,)


def _check(key: str) -> None:
    if key in _KNOWN_KEYS:
        return
    if any(key.startswith(p) and len(key) > len(p) for p in _KNOWN_PREFIXES):
        return
    raise ValueError(f"unknown alert_state key: {key}")


def limit_suppress_key(order_id: int) -> str:
    return f"{LIMIT_SUPPRESS_PREFIX}{int(order_id)}"


def get(conn: sqlite3.Connection, key: str) -> str | None:
    _check(key)
    row = conn.execute("SELECT value FROM alert_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set(conn: sqlite3.Connection, key: str, value: str, *, now: datetime) -> None:
    _check(key)
    conn.execute(
        "INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
        "updated_at=excluded.updated_at", (key, value, now.isoformat()))
    conn.commit()


def delete(conn: sqlite3.Connection, key: str) -> None:
    _check(key)
    conn.execute("DELETE FROM alert_state WHERE key=?", (key,))
    conn.commit()


def keys_with_prefix(conn: sqlite3.Connection, prefix: str) -> list[str]:
    """既知の prefix の key を昇順で返す。"""
    if prefix not in _KNOWN_PREFIXES:
        raise ValueError(f"unknown alert_state prefix: {prefix}")
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return [r[0] for r in conn.execute(
        "SELECT key FROM alert_state WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
        (escaped + "%",))]
