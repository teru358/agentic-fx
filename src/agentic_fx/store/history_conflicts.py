"""Human-facing history conflict identity and persistence helpers."""
from __future__ import annotations

import hashlib
import json
import math

from agentic_fx.store.db import values_match

_FIELDS = ("open", "high", "low", "close", "volume", "spread")


def values_equal(existing, incoming):
    return all(values_match(a, b) for a, b in zip(existing, incoming, strict=True))


def canonical_values(values):
    if len(values) != len(_FIELDS):
        raise ValueError("expected six values")
    normalized = []
    for value in values:
        if value is None:
            normalized.append(None)
        else:
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("non-finite value")
            normalized.append(0.0 if number == 0 else number)
    payload = json.dumps(dict(zip(_FIELDS, normalized)), separators=(",", ":"), allow_nan=False)
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upsert(conn, *, source, interval, symbol, bar_time, history_epoch,
           existing, incoming, origin, seen_at, run_id=None):
    existing_json, existing_digest = canonical_values(existing)
    incoming_json, incoming_digest = canonical_values(incoming)
    conn.execute("""
        INSERT INTO history_conflicts
          (source, interval, symbol, bar_time, history_epoch,
           existing_json, incoming_json, existing_digest, incoming_digest,
           origin, last_origin, first_seen_at, last_seen_at, first_run_id, last_run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, interval, symbol, bar_time, history_epoch,
                    existing_digest, incoming_digest) DO UPDATE SET
          last_seen_at = max(history_conflicts.last_seen_at, excluded.last_seen_at),
          seen_count = history_conflicts.seen_count + 1,
          last_origin = excluded.last_origin, last_run_id = excluded.last_run_id
    """, (source, interval, symbol, bar_time, history_epoch, existing_json,
          incoming_json, existing_digest, incoming_digest, origin, origin,
          seen_at, seen_at, run_id, run_id))



def is_known(conn, *, source, interval, symbol, bar_time, history_epoch,
             existing, incoming):
    """Whether the exact same observation is already an open or kept record.

    Replaced and superseded records are never known: a re-observation of them
    counts as new for the circuit breaker.
    """
    _, existing_digest = canonical_values(existing)
    _, incoming_digest = canonical_values(incoming)
    row = conn.execute(
        "SELECT 1 FROM history_conflicts WHERE source=? AND interval=? AND symbol=? "
        "AND bar_time=? AND history_epoch=? AND existing_digest=? AND incoming_digest=? "
        "AND state IN ('open','kept')",
        (source, interval, symbol, bar_time, history_epoch,
         existing_digest, incoming_digest)).fetchone()
    return row is not None
