"""Persistent snapshots and canonical event-to-currency links for reactions."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Mapping, Sequence

from agentic_fx.analytics.econ_reactions import (RULE_VERSION, ReactionSample, WindowValue,
                                                  canonical_pair, event_currencies)
from agentic_fx.store.ohlcv_slices import (STATE_COMPLETE, STATE_UNRECOVERABLE,
                                            Begin, Checkpoint, Conflict, TxResult,
                                            begin_immediate, short_tx)
from agentic_fx.store.signals import utc_iso


def pending_sets(conn: sqlite3.Connection, *, pair_currencies: Mapping[str, tuple[str, str]],
                 limit: int, after: int | None = None) -> list[dict]:
    if limit <= 0:
        return []
    canonical = {}
    for pair, currencies in pair_currencies.items():
        for currency in currencies:
            canonical.setdefault(currency, pair)
    params = []
    if canonical:
        values_sql = ",".join("(?,?,?)" for _ in canonical)
        params.extend(value for ordinal, item in enumerate(canonical.items())
                      for value in (*item, ordinal))
        keys_cte = f"canonical_keys(currency,pair,ordinal) AS (VALUES {values_sql}),"
    else:
        keys_cte = "canonical_keys(currency,pair,ordinal) AS (SELECT NULL,NULL,NULL WHERE 0),"
    params.extend((STATE_COMPLETE, STATE_UNRECOVERABLE,
                   -1 if after is None else int(after), int(limit)))
    rows = conn.execute(
        f"WITH {keys_cte} value_counts AS ("
        " SELECT set_id,COUNT(*) AS n FROM econ_reaction_values GROUP BY set_id) "
        "SELECT ss.*,e.ts,e.country,e.name,e.importance,e.actual,e.superseded_by,"
        " COALESCE(vc.n,0) AS value_n,"
        " (SELECT group_concat(m.currency, char(31)) FROM ("
        "   SELECT ck.currency FROM canonical_keys ck WHERE ck.pair=ss.pair "
        "   AND (e.country='All' OR e.country=ck.currency) "
        "   AND NOT EXISTS (SELECT 1 FROM econ_reaction_keys k "
        "       WHERE k.event_id=e.id AND k.currency=ck.currency)"
            "   ORDER BY ck.ordinal) m) AS missing_currencies "
        "FROM ohlcv_slice_sets ss JOIN econ_events e ON e.id=ss.event_id "
        "LEFT JOIN value_counts vc ON vc.set_id=ss.id "
        "WHERE ss.event_id IS NOT NULL AND ss.state IN (?,?) AND ss.id>? "
        "AND (COALESCE(vc.n,0)=0 OR (vc.n=3 AND EXISTS ("
        " SELECT 1 FROM canonical_keys ck WHERE ck.pair=ss.pair "
        " AND (e.country='All' OR e.country=ck.currency) "
        " AND NOT EXISTS (SELECT 1 FROM econ_reaction_keys k "
        "     WHERE k.event_id=e.id AND k.currency=ck.currency)))) "
        "ORDER BY ss.id LIMIT ?", params).fetchall()
    out = []
    for raw in rows:
        row = dict(raw)
        if row.pop("value_n") == 3:
            todo = (row.pop("missing_currencies") or "").split(chr(31))
            # Preserve the same settings-order canonical decision made by the SQL.
            currencies = event_currencies(row["country"], pair_currencies)
            row["missing_keys"] = [c for c in currencies
                if canonical_pair(c, pair_currencies) == row["pair"] and c in todo]
        else:
            row.pop("missing_currencies", None)
        out.append(row)
    return out


def write_set_tx(conn: sqlite3.Connection, *, set_id: int, values: Sequence[WindowValue],
                 keys: Sequence[tuple[int, str]], now: datetime,
                 begin: Begin = begin_immediate, checkpoint: Checkpoint | None = None) -> TxResult:
    def body():
        count = conn.execute("SELECT COUNT(*) FROM econ_reaction_values WHERE set_id=?", (set_id,)).fetchone()[0]
        if count not in (0, 3): raise Conflict
        if count == 0:
            if len(values) != 3: raise ValueError("exactly three window values required")
            conn.executemany("INSERT INTO econ_reaction_values(set_id,window_min,status,range_pips,close_change_pips,abs_close_change_pips,net_abs_close_change_pips,rule_version,computed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                [(set_id, v.window_min, v.status, v.range_pips, v.close_change_pips,
                  v.abs_close_change_pips, v.net_abs_close_change_pips, RULE_VERSION, utc_iso(now)) for v in values])
        for event_id, currency in keys:
            conn.execute("INSERT OR IGNORE INTO econ_reaction_keys(event_id,currency,set_id,decided_at) VALUES(?,?,?,?)",
                         (event_id, currency, set_id, utc_iso(now)))
        return {"set_id": set_id}
    return short_tx(conn, begin, body, checkpoint)


def load_samples(conn: sqlite3.Connection) -> tuple[ReactionSample, ...]:
    rows = conn.execute("SELECT k.event_id,k.currency,e.importance,e.name,e.ts,v.window_min,v.status,v.range_pips,v.close_change_pips,v.abs_close_change_pips,v.net_abs_close_change_pips FROM econ_reaction_keys k JOIN econ_events e ON e.id=k.event_id JOIN econ_reaction_values v ON v.set_id=k.set_id JOIN ohlcv_slice_sets ss ON ss.id=k.set_id WHERE e.superseded_by IS NULL ORDER BY k.event_id,k.currency,v.window_min").fetchall()
    return tuple(ReactionSample(r[0], r[1], r[2], r[3], datetime.fromisoformat(r[4]),
        WindowValue(r[5], r[6], r[7], r[8], r[9], r[10])) for r in rows)


def backlog_counts(conn: sqlite3.Connection, *, pair_currencies: Mapping[str, tuple[str, str]]) -> dict[str, int]:
    sets = conn.execute(
        "WITH value_counts AS (SELECT set_id,COUNT(*) AS n FROM econ_reaction_values GROUP BY set_id) "
        "SELECT ss.event_id,e.country,COALESCE(vc.n,0) AS value_n "
        "FROM ohlcv_slice_sets ss JOIN econ_events e ON e.id=ss.event_id "
        "LEFT JOIN value_counts vc ON vc.set_id=ss.id "
        "WHERE ss.event_id IS NOT NULL AND ss.state IN (?,?) AND e.superseded_by IS NULL",
        (STATE_COMPLETE, STATE_UNRECOVERABLE)).fetchall()
    no_values = sum(1 for row in sets if row["value_n"] != 3)
    keyless_events = set()
    for row in sets:
        currencies = event_currencies(row[1], pair_currencies)
        if not currencies: keyless_events.add(row[0])
    return {"sets_without_values": no_values, "events_without_currency_key": len(keyless_events)}
